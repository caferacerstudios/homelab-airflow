"""Read genuine BALLDONTLIE schedules for EventSpy's additional teams.

Seattle keeps using its existing NFL snapshot. Additional teams prefer the
validated NFL refresh output and otherwise use a bounded API fallback. This
small cache supplies game identity/status only, never player or archive data.
API contract: https://nfl.balldontlie.io/#get-all-games (reviewed 2026-09-12).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import warnings
from zoneinfo import ZoneInfo


API_BASE = "https://api.balldontlie.io/nfl/v1"
CACHE_ROOT = Path("/var/lib/fanzone-eventspy/schedules")
API_KEY_PATH = Path("/home/laurawkr/seahawksfanzone/.env")
MAX_AGE = timedelta(hours=6)
MAX_PAGES = 4
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class ScheduleFetchError(RuntimeError):
    """A bounded request failed; the exception never contains credentials/body."""


def read_api_key(path: Path) -> str:
    """Read one literal dotenv value; never source or expand shell input."""
    found = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?BALLDONTLIE_API_KEY\s*=(.*)$", line)
        if not match:
            continue
        raw = match.group(1).strip()
        if raw.startswith(("'", '"')):
            literal = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", raw)
            if not literal:
                raise ValueError("BALLDONTLIE_API_KEY must be a single literal dotenv value")
            value = literal.group(2)
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if re.search(r"\s", value):
                raise ValueError("Quote a BALLDONTLIE_API_KEY value containing spaces")
        if not value or any(char in value for char in ("$", "`", "\\", "\x00")):
            raise ValueError("BALLDONTLIE_API_KEY must be nonempty and literal; shell expansion is unsupported")
        found.append(value)
    if len(found) != 1:
        raise ValueError("Expected exactly one BALLDONTLIE_API_KEY entry in the production .env")
    return found[0]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScheduleFetchError("BALLDONTLIE returned an unexpected redirect")


def request_json(url: str, api_key: str) -> dict:
    """One request, no retries or redirects, with a response-size/time bound."""
    if not url.startswith(API_BASE + "/"):
        raise ValueError("Unexpected schedule API origin")
    request = Request(url, headers={"Authorization": api_key, "Accept": "application/json"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise ScheduleFetchError(f"BALLDONTLIE HTTP {error.code}; no retry was attempted") from None
    except (URLError, TimeoutError, OSError):
        raise ScheduleFetchError("BALLDONTLIE network request failed; no retry was attempted") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ScheduleFetchError("BALLDONTLIE response exceeded the size limit")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise ScheduleFetchError("BALLDONTLIE returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise ScheduleFetchError("BALLDONTLIE response must be a JSON object")
    return payload


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Schedule timestamp must be a string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid schedule timestamp") from None
    if result.tzinfo is None:
        raise ValueError("Schedule timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def _identity(site: dict, coverage: list[dict]) -> tuple[str, str, int]:
    slug = site.get("slug")
    abbreviation = site.get("abbreviation")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", slug):
        raise ValueError("Invalid schedule site slug")
    if not isinstance(abbreviation, str) or not re.fullmatch(r"[A-Z]{2,3}", abbreviation):
        raise ValueError("Invalid schedule team abbreviation")
    if not isinstance(coverage, list) or len(coverage) != 17 or any(not isinstance(row, dict) for row in coverage):
        raise ValueError("Reviewed coverage must contain all 17 regular-season games")
    seasons = {row.get("season") for row in coverage}
    if len(seasons) != 1 or any(type(season) is not int or not 2021 <= season <= 2200 for season in seasons):
        raise ValueError("Coverage must identify one supported season")
    weeks = [row.get("week") for row in coverage]
    if any(type(week) is not int or not 1 <= week <= 18 for week in weeks) or len(set(weeks)) != 17:
        raise ValueError("Coverage must have 17 unique regular-season weeks")
    return slug, abbreviation, next(iter(seasons))


def _abbreviation(value) -> str:
    """Compare provider aliases without rewriting stored team identities."""
    abbreviation = str(value or "").strip().upper()
    return "WSH" if abbreviation == "WAS" else abbreviation


def validate_schedule(payload: dict, site: dict, coverage: list[dict]) -> None:
    """Reject incomplete/wrong-team schedules and unreviewed identity/date changes."""
    slug, abbreviation, season = _identity(site, coverage)
    if not isinstance(payload, dict) or payload.get("fixture") is not False or payload.get("source") != "balldontlie":
        raise ValueError("Expected a genuine BALLDONTLIE schedule cache")
    if payload.get("season") != season:
        raise ValueError("Schedule season does not match reviewed coverage")
    _timestamp(payload.get("updatedAt"))
    team = payload.get("team")
    if not isinstance(team, dict) or _abbreviation(team.get("abbreviation")) != _abbreviation(abbreviation) or type(team.get("id")) is not int or team["id"] < 1:
        raise ValueError("Schedule team identity does not match the configured site")
    configured_id = site.get("balldontlie_team_id")
    if configured_id is not None and configured_id != team["id"]:
        raise ValueError("Configured BALLDONTLIE team ID does not match its abbreviation")
    games = payload.get("gamesRegular")
    if not isinstance(games, list) or len(games) != 17 or payload.get("games") != games:
        raise ValueError("Expected exactly 17 regular-season schedule games")
    if any(payload.get(key) != [] for key in ("gamesPreseason", "gamesPostseason", "currentRoster", "playerSeasonStats")):
        raise ValueError("EventSpy's schedule-only cache must not contain unrelated NFL data")
    by_week = {row["week"]: row for row in coverage}
    identifiers, seen_weeks = set(), set()
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("Invalid schedule game")
        identifier, week = game.get("id"), game.get("week")
        if type(identifier) is not int or identifier < 1 or identifier in identifiers:
            raise ValueError("Schedule must contain unique positive BALLDONTLIE game IDs")
        if type(week) is not int or week not in by_week or week in seen_weeks:
            raise ValueError("Schedule week does not match reviewed coverage")
        identifiers.add(identifier)
        seen_weeks.add(week)
        entry = by_week[week]
        if game.get("season") != season or game.get("season_type") != "regular" or game.get("postseason") is True:
            raise ValueError("Schedule game is not in the reviewed regular season")
        home, away = game.get("home_team"), game.get("visitor_team")
        if not isinstance(home, dict) or not isinstance(away, dict):
            raise ValueError("Schedule game must identify both NFL teams")
        actual = (_abbreviation(home.get("abbreviation")), _abbreviation(away.get("abbreviation")))
        reviewed = (_abbreviation(entry.get("homeTeamAbbreviation")), _abbreviation(entry.get("awayTeamAbbreviation")))
        if actual != reviewed:
            raise ValueError(
                f"{slug} Week {week}: API matchup does not match reviewed EventSpy coverage "
                f"(API {actual[1]} at {actual[0]}; reviewed {reviewed[1]} at {reviewed[0]})"
            )
        participants = [participant for participant in (home, away) if _abbreviation(participant.get("abbreviation")) == _abbreviation(abbreviation)]
        if len(participants) != 1 or participants[0].get("id") != team["id"]:
            raise ValueError("Schedule game does not include the configured team ID")
        if entry.get("gameId") is not None and str(identifier) != str(entry["gameId"]):
            raise ValueError(f"Week {week}: API game ID differs from the reviewed game ID")
        # Full NFL output preserves a calendar date separately from the kickoff
        # instant. Prefer the instant for venue-local matching; never turn a TBD
        # time into an invented midnight kickoff.
        date = game.get("startsAt") or game.get("date")
        if game.get("dateConfirmed") is False or game.get("date_confirmed") is False or game.get("date_tbd") is True or game.get("dateTbd") is True:
            date = None
        if date is not None:
            if isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                if game.get("timeConfirmed") is True or game.get("time_confirmed") is True:
                    raise ValueError("A confirmed schedule kickoff must include a timezone")
                local_date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
            else:
                local_date = _timestamp(date).astimezone(ZoneInfo(entry["timeZone"])).date().isoformat()
            if entry.get("localDate") is not None and local_date != entry["localDate"]:
                raise ValueError(f"Week {week}: API date changed; review the EventSpy event before replacing this schedule")
        elif entry.get("localDate") is not None:
            raise ValueError(f"Week {week}: API kickoff is missing for a dated EventSpy event")
        if not isinstance(game.get("status"), str) and not isinstance(game.get("status_state"), str):
            raise ValueError("Schedule game must include API lifecycle status")


def schedule_from_nfl_snapshot(site: dict, coverage: list[dict], now: datetime) -> dict | None:
    """Pin one published NFL run, verify it, and derive a schedule-only cache.

    A missing or valid-but-stale snapshot permits the existing API fallback.
    An existing invalid snapshot is an error, even if an older cache is usable;
    do not hide cross-team bindings or corrupt producer output with a new fetch.
    """
    configured = site.get("nfl_snapshot_dir")
    if configured is None:
        return None
    slug, abbreviation, season = _identity(site, coverage)
    if not isinstance(configured, str) or not Path(configured).is_absolute() or Path(configured).name != "current":
        raise ValueError("NFL snapshot directory must be an absolute current path")
    current = Path(configured)
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ValueError("NFL current must be a published snapshot symlink")
    try:
        root = current.parent.resolve(strict=True)
        snapshot = current.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("NFL current symlink does not resolve to a published snapshot") from None
    if snapshot == root or not snapshot.is_relative_to(root) or not snapshot.is_dir():
        raise ValueError("NFL current snapshot must remain inside its own team root")

    def read_file(name: str) -> bytes:
        candidate = snapshot / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"NFL snapshot requires a regular file: {name}")
        if candidate.stat().st_size > MAX_RESPONSE_BYTES:
            raise ValueError(f"NFL snapshot file exceeded the size limit: {name}")
        return candidate.read_bytes()

    def object_from(body: bytes, name: str) -> dict:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise ValueError(f"Invalid NFL snapshot JSON: {name}") from None
        if not isinstance(payload, dict):
            raise ValueError(f"NFL snapshot must be an object: {name}")
        return payload

    manifest = object_from(read_file("manifest.json"), "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("team") != slug or manifest.get("season") != season:
        raise ValueError("NFL manifest team or season does not match the selected site")
    primary = {f"{slug}.json", "players.json", "standings.json"}
    files = manifest.get("files")
    if not isinstance(files, dict) or not primary <= set(files) or not set(files) <= primary | {"gameRecaps.json"}:
        raise ValueError("NFL manifest file list is invalid")
    contents = {}
    for name, digest in files.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("NFL manifest checksum is invalid")
        body = read_file(name)
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError(f"NFL snapshot checksum mismatch: {name}")
        if name in primary:
            contents[name] = object_from(body, name)
    combined = contents[f"{slug}.json"]
    updated = _timestamp(manifest.get("updatedAt"))
    if updated > now:
        raise ValueError("NFL snapshot timestamp is in the future")
    for name, payload in contents.items():
        if payload.get("fixture") is True or (name == f"{slug}.json" and payload.get("fixture") is not False):
            raise ValueError("NFL snapshot must not contain fixture data")
        if payload.get("season") != season or payload.get("updatedAt") != manifest.get("updatedAt"):
            raise ValueError("NFL snapshot freshness or season does not match its manifest")
    team = combined.get("team")
    if not isinstance(team, dict) or type(team.get("id")) is not int or team["id"] < 1 or _abbreviation(team.get("abbreviation")) != _abbreviation(abbreviation):
        raise ValueError("NFL snapshot team identity does not match the selected site")
    if site.get("balldontlie_team_id") is not None and site["balldontlie_team_id"] != team["id"]:
        raise ValueError("NFL snapshot team ID does not match the selected site")
    player_team = contents["players.json"].get("team", {})
    if not isinstance(player_team, dict) or player_team.get("id") != team["id"] or _abbreviation(player_team.get("abbreviation")) != _abbreviation(abbreviation):
        raise ValueError("NFL players snapshot belongs to a different team")
    if combined.get("sourceSeason", season) != season:
        raise ValueError("NFL snapshot is using a different source season")
    rows = combined.get("gamesRegular")
    if not isinstance(rows, list):
        raise ValueError("NFL snapshot has no regular-season games")
    games = []
    for original in rows:
        if not isinstance(original, dict):
            raise ValueError("NFL snapshot has an invalid game row")
        if original.get("bye") is True:
            continue
        game = dict(original)
        identifier = game.get("id")
        if type(identifier) is int:
            pass
        elif isinstance(identifier, str) and re.fullmatch(r"[1-9][0-9]{0,15}", identifier):
            game["id"] = int(identifier)
        else:
            raise ValueError("NFL snapshot game ID is not a real positive identifier")
        if game.get("season_type") not in (None, 2, "2", "regular") or game.get("phase") not in (None, "regular"):
            raise ValueError("NFL snapshot includes a non-regular-season game")
        game["season_type"] = "regular"
        game["home_team"] = game.get("home_team") or game.get("homeTeam")
        game["visitor_team"] = game.get("visitor_team") or game.get("awayTeam")
        if "date" not in game:
            game["date"] = game.get("startsAt")
        if not isinstance(game.get("status"), str) and not isinstance(game.get("status_state"), str):
            game["status_state"] = game.get("state")
        games.append(game)
    payload = {"fixture": False, "source": "balldontlie", "team": dict(team), "season": season,
               "updatedAt": manifest["updatedAt"], "games": games, "gamesRegular": games,
               "gamesPreseason": [], "gamesPostseason": [], "playerSeasonStats": [], "currentRoster": []}
    validate_schedule(payload, site, coverage)
    if now - updated >= MAX_AGE:
        return None
    return payload


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_schedule(site: dict, coverage: list[dict], *, now: datetime | None = None,
                    request=None, sleep=time.sleep, cache_root: Path = CACHE_ROOT,
                    api_key_path: Path = API_KEY_PATH, force_refresh: bool = False,
                    allow_stale: bool = True) -> Path | None:
    """Return the additional team's cache path; Seattle's existing path is untouched.

    A fresh, validated full NFL snapshot takes priority, including forced
    refreshes. It is copied into the schedule-only cache by the root caller.
    `request(url, api_key)` is injectable for offline tests. Fresh snapshot/cache
    reads do not access credentials. Only transient request failures may retain
    a valid older cache; mismatched published identities/dates fail for review.
    Installers use force_refresh=True, allow_stale=False for schedule handoff.
    """
    if site.get("slug") == "seahawks":
        return None
    slug, abbreviation, season = _identity(site, coverage)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Schedule clock must include a timezone")
    now = now.astimezone(timezone.utc)
    path = Path(cache_root) / f"{slug}.json"
    published = schedule_from_nfl_snapshot(site, coverage, now)
    if published is not None:
        # Root owns this small cache; the NFL producer only writes its own root.
        _atomic_write(path, published)
        return path
    cached = None
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            validate_schedule(cached, site, coverage)
            updated = _timestamp(cached["updatedAt"])
            if updated > now:
                raise ValueError("Schedule cache timestamp is in the future")
        except (ValueError, KeyError, TypeError, OSError):
            cached = None
        if cached is not None and not force_refresh and updated.date() == now.date() and now - updated < MAX_AGE:
            return path

    api_key = read_api_key(Path(api_key_path))
    fetch = request or request_json
    calls = 0

    def paged(endpoint, parameters):
        nonlocal calls
        rows, seen, cursor = [], set(), None
        for _ in range(MAX_PAGES):
            query = list(parameters) + [("per_page", 100)]
            if cursor is not None:
                query.append(("cursor", cursor))
            if calls:
                sleep(15)
            calls += 1
            response = fetch(API_BASE + endpoint + "?" + urlencode(query), api_key)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise ScheduleFetchError("BALLDONTLIE response is missing its data array")
            rows.extend(response["data"])
            meta = response.get("meta", {})
            if not isinstance(meta, dict):
                raise ScheduleFetchError("BALLDONTLIE returned invalid pagination metadata")
            cursor = meta.get("next_cursor")
            if cursor is None:
                return rows
            if type(cursor) not in (str, int) or not re.fullmatch(r"[0-9]{1,20}", str(cursor)) or str(cursor) in seen:
                raise ScheduleFetchError("BALLDONTLIE returned an invalid or repeated cursor")
            seen.add(str(cursor))
        raise ScheduleFetchError("BALLDONTLIE schedule pagination limit reached")

    try:
        teams = paged("/teams", [])
        matches = [team for team in teams if isinstance(team, dict) and _abbreviation(team.get("abbreviation")) == _abbreviation(abbreviation)]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] < 1:
            raise ValueError("BALLDONTLIE must return exactly one matching team")
        team = matches[0]
        if site.get("balldontlie_team_id") is not None and team["id"] != site["balldontlie_team_id"]:
            raise ValueError("Configured BALLDONTLIE team ID does not match its abbreviation")
        raw_games = paged("/games", [("seasons[]", season), ("team_ids[]", team["id"]), ("season_types[]", 2)])
    except ScheduleFetchError:
        if cached is not None and allow_stale:
            warnings.warn(f"{slug}: BALLDONTLIE refresh failed; retaining validated schedule from {cached['updatedAt']}", RuntimeWarning)
            return path
        raise

    games = []
    for row in raw_games:
        if not isinstance(row, dict):
            raise ValueError("BALLDONTLIE returned an invalid game row")
        # Explicit filter + reviewed matchup/week provides regular-season
        # provenance when the API omits this field. Reject contradictory values.
        if row.get("season_type") not in (None, 2, "2", "regular") or row.get("postseason") is True:
            raise ValueError("BALLDONTLIE returned a non-regular-season game")
        games.append(dict(row, season_type="regular"))
    payload = {"fixture": False, "source": "balldontlie", "team": dict(team), "season": season,
               "updatedAt": now.isoformat().replace("+00:00", "Z"), "games": games,
               "gamesRegular": games, "gamesPreseason": [], "gamesPostseason": [],
               "playerSeasonStats": [], "currentRoster": []}
    validate_schedule(payload, site, coverage)
    _atomic_write(path, payload)
    return path
