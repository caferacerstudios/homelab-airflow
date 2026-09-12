"""Read independent guide activation without changing other active-site tasks."""
import json
from pathlib import Path
import re

DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "config" / "game-guides.json"


def load_policy(path=None):
    path = Path(path) if path is not None else DEFAULT_POLICY
    if path.stat().st_size > 65536:
        raise ValueError("Guide policy exceeds 64 KiB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Guide policy must be a JSON object")
    enabled = value.get("enabled_sites")
    if (not isinstance(enabled, list) or len(enabled) > 32
            or any(not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", slug) for slug in enabled)
            or len(set(enabled)) != len(enabled)):
        raise ValueError("Guide enabled_sites must be a unique list of team slugs")
    return value


def guide_sites(sites, policy=None):
    """Intersection: guide activation never enables an inactive existing site."""
    enabled = set((load_policy() if policy is None else policy)["enabled_sites"])
    return [site for site in sites if site["slug"] in enabled and site["enabled"]]
