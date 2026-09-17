#!/usr/bin/env python3
"""Generate an article or inspect the installed setup without source calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from news_core import SOURCE, RUNTIME, SEATTLE, api_key, collect, photo_pool, read_config, site_settings, source_catalog, validate_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run-id')
    parser.add_argument('--publication-day')
    parser.add_argument('--site-json', help='Validated site configuration supplied by the Airflow task')
    parser.add_argument('--photo-credits-json', help='Validated photo credits supplied by the Airflow task')
    args = parser.parse_args()
    try:
        site = site_settings(json.loads(args.site_json)) if args.site_json else None
        credits = validate_catalog(json.loads(args.photo_credits_json)) if args.photo_credits_json is not None else None
        source = Path(site['website_root']) if site else SOURCE
        runtime = Path(site['news_snapshot_dir']).parent if site else RUNTIME
        photos_dir = Path(site['news_photos_dir']) if site else runtime / 'photos'
        if args.check:
            if not runtime.is_dir() or not photos_dir.is_dir():
                raise ValueError('Create the news runtime and photo directory using the installer')
            commit, _ = source_catalog(source, site=site)
            api_key(SOURCE if site is not None else source)
            photos, notes = photo_pool(runtime, photos_dir, credits)
            print(json.dumps({'status': 'ready', 'team': site_settings(site)['slug'], 'sourceCommit': commit, 'photoCount': len(photos),
                              'model': read_config(runtime)['model'], 'apiRequests': 0, 'notes': notes}))
        else:
            zone = ZoneInfo(site.get('timezone', 'America/Los_Angeles')) if site else SEATTLE
            day = args.publication_day or datetime.now(timezone.utc).astimezone(zone).date().isoformat()
            receipt = collect(args.run_id, day, site=site, photo_credits=credits)
            print('SFZ_NEWS_RECEIPT=' + json.dumps(receipt, separators=(',', ':')))
        return 0
    except Exception as exc:
        print(f'News generation stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
