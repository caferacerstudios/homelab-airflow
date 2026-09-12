"""Shared active-site configuration for Airflow and restricted host requests."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import PurePosixPath
import re
from zoneinfo import ZoneInfo

VARIABLE_NAME = "fan_zone_active_sites"
WEBSITE_ROOTS = ("/home/laurawkr/seahawksfanzone", "/home/laurawkr/templatefanzone")


def text(value, field, limit=160, multiline=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 and not (multiline and char in "\n\t") for char in value)):
        raise ValueError(f"Invalid {field}")
    return value.strip()


def validate_site(slug, site):
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", slug) or not isinstance(site, dict):
        raise ValueError("Each site must have a lowercase team slug and a JSON object")
    if site.get("slug", slug) != slug:
        raise ValueError("Site slug does not match its key")
    allowed = {"slug", "enabled", "name", "city", "abbreviation", "balldontlie_team_id",
               "website_root", "news_snapshot_dir", "news_photos_dir", "source_domains",
               "prompts", "timezone", "division", "eventspy", "nfl_snapshot_dir", "recap_snapshot_dir"}
    if set(site) - allowed:
        raise ValueError(f"{slug}: unsupported site configuration fields")
    result = dict(site, slug=slug)
    for field in ("name", "city"):
        result[field] = text(site.get(field), field)
    if type(site.get("enabled")) is not bool:
        raise ValueError(f"{slug}: enabled must be true or false")
    if "abbreviation" in site and (not isinstance(site["abbreviation"], str) or not re.fullmatch(r"[A-Z]{2,3}", site["abbreviation"])):
        raise ValueError(f"{slug}: invalid abbreviation")
    identifier = site.get("balldontlie_team_id")
    if identifier is not None and (type(identifier) is not int or identifier < 1):
        raise ValueError(f"{slug}: balldontlie_team_id must be a positive integer or null")
    # News does not call balldontlie. Preserve this optional field for site metadata.
    if "balldontlie_team_id" in site:
        result["balldontlie_team_id"] = identifier
    result["website_root"] = site.get("website_root", WEBSITE_ROOTS[0] if slug == "seahawks" else WEBSITE_ROOTS[1])
    if result["website_root"] not in WEBSITE_ROOTS or (slug != "seahawks" and result["website_root"] == WEBSITE_ROOTS[0]):
        raise ValueError(f"{slug}: invalid website_root for this team")
    snapshot = site.get("news_snapshot_dir")
    if not isinstance(snapshot, str) or not re.fullmatch(r"/var/lib/[a-z][a-z0-9-]{0,59}-news/current", snapshot):
        raise ValueError(f"{slug}: use /var/lib/<site>-news/current for news_snapshot_dir")
    root = str(PurePosixPath(snapshot).parent)
    if slug != "seahawks" and root == "/var/lib/sfz-news":
        raise ValueError("/var/lib/sfz-news belongs to seahawks")
    if site.get("news_photos_dir") != root + "/photos":
        raise ValueError(f"{slug}: news_photos_dir must be beside current in the news directory")
    domains = site.get("source_domains")
    if not isinstance(domains, list) or not 1 <= len(domains) <= 12:
        raise ValueError(f"{slug}: source_domains must be a nonempty list")
    if any(not isinstance(domain, str) or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", domain) for domain in domains):
        raise ValueError(f"{slug}: source_domains must contain hostnames, not URLs")
    result["source_domains"] = list(dict.fromkeys(domains))
    prompts = site.get("prompts")
    if not isinstance(prompts, dict) or set(prompts) - {"article", "recap"}:
        raise ValueError(f"{slug}: prompts must include article")
    result["prompts"] = {"article": text(prompts.get("article"), "prompts.article", 8000, multiline=True)}
    if "recap" in prompts:
        # Compatible with the existing website JSON; daily news never uses this prompt.
        result["prompts"]["recap"] = text(prompts["recap"], "prompts.recap", 8000, multiline=True)
    result["timezone"] = site.get("timezone", "America/Los_Angeles")
    ZoneInfo(result["timezone"])
    if "division" in site and site["division"] not in [f"{c} {d}" for c in ("AFC", "NFC") for d in ("East", "West", "North", "South")]:
        raise ValueError(f"{slug}: invalid division")
    if "eventspy" in site:
        result["eventspy"] = validate_eventspy(slug, site["eventspy"])
    for field, suffix in (("nfl_snapshot_dir", "nfl"), ("recap_snapshot_dir", "recaps")):
        if field not in site:
            continue  # Legacy daily-news requests remain valid.
        destination = site[field]
        if not isinstance(destination, str) or not re.fullmatch(r"/var/lib/[a-z][a-z0-9-]{0,59}-" + suffix + r"/current", destination):
            raise ValueError(f"{slug}: invalid {field}")
        if (slug == "seahawks") != (destination == f"/var/lib/sfz-{suffix}/current"):
            raise ValueError(f"The existing Seattle {suffix} output belongs only to seahawks and must be preserved")
    if len(json.dumps(result).encode()) > 23000:
        raise ValueError(f"{slug}: configuration is too large")
    return result


def validate_sites(value):
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and set(value) == {VARIABLE_NAME}:
        value = value[VARIABLE_NAME]
        if isinstance(value, str):
            value = json.loads(value)
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("Active sites must be a JSON object keyed by team slug (up to 32 teams)")
    sites = {slug: validate_site(slug, site) for slug, site in value.items()}
    owners = {}
    for slug, site in sites.items():
        path = site["news_snapshot_dir"]
        if path in owners:
            raise ValueError(f"{slug} and {owners[path]} share news output; each team needs its own directory")
        owners[path] = slug
    ticket_owners = {}
    for slug, site in sites.items():
        if "eventspy" not in site:
            continue
        path = site["eventspy"]["output_dir"]
        if path in ticket_owners:
            raise ValueError(f"{slug} and {ticket_owners[path]} share ticket output")
        ticket_owners[path] = slug
    for field in ("nfl_snapshot_dir", "recap_snapshot_dir"):
        owners = {}
        for slug, site in sites.items():
            if field not in site:
                continue
            destination = site[field]
            if destination in owners:
                raise ValueError(f"{slug} and {owners[destination]} share {field}")
            owners[destination] = slug
    return sites


def validate_eventspy(slug, value):
    """Keep destinations separate and source paths limited to site NFL snapshots."""
    if not isinstance(value, dict) or set(value) != {"output_dir", "coverage_file", "schedule_file"}:
        raise ValueError(f"{slug}: eventspy requires output_dir, coverage_file and schedule_file")
    output = value["output_dir"]
    if not isinstance(output, str) or not re.fullmatch(r"/var/lib/[a-z][a-z0-9-]{0,59}-eventspy-mirror/dev/public", output):
        raise ValueError(f"{slug}: invalid EventSpy output_dir")
    seattle_output = "/var/lib/sfz-eventspy-mirror/dev/public"
    if (slug == "seahawks") != (output == seattle_output):
        raise ValueError("The existing Seattle EventSpy output belongs only to seahawks and must be preserved")
    coverage = value["coverage_file"]
    if not isinstance(coverage, str) or not re.fullmatch(re.escape(slug) + r"(?:-20[0-9]{2})?\.json", coverage):
        raise ValueError(f"{slug}: coverage_file must be a reviewed <team>.json or <team>-<season>.json basename")
    schedule = value["schedule_file"]
    expected_schedule = ("/var/lib/sfz-nfl/current/seahawks.json" if slug == "seahawks"
                         else f"/var/lib/fanzone-eventspy/schedules/{slug}.json")
    if schedule != expected_schedule:
        raise ValueError(f"{slug}: EventSpy schedule_file must be {expected_schedule}")
    return dict(value)


def eventspy_slot(value):
    """One exact UTC representation shared with JavaScript Date.toISOString()."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid EventSpy UTC slot")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.microsecond:
        raise ValueError("EventSpy slot must be a whole-second UTC timestamp")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def eventspy_request_id(slot, site):
    """Stable request identity; changing configuration never authorizes a slot retry."""
    slot = eventspy_slot(slot)
    digest = hashlib.sha256(json.dumps(site, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return hashlib.sha256(f"collect:{site['slug']}:{slot}:{digest}".encode()).hexdigest()
