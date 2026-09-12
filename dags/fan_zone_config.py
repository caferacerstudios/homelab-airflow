"""Shared news-site configuration for the Airflow Variable and host requests."""
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
               "prompts", "timezone", "division"}
    if set(site) - allowed:
        raise ValueError(f"{slug}: unsupported news-site configuration fields")
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
    return sites
