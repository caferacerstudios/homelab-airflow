#!/usr/bin/env python3
"""Run a daily news generation or inspect the installed setup without source calls."""
import argparse
from datetime import datetime, timezone
import json
import sys
from news_core import SOURCE, RUNTIME, SEATTLE, api_key, collect, photo_pool, read_config, source_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run-id')
    parser.add_argument('--publication-day')
    args = parser.parse_args()
    try:
        if args.check:
            if not RUNTIME.is_dir() or not (RUNTIME / 'photos').is_dir():
                raise ValueError('Create the news runtime and photo directory using the installer')
            commit, _ = source_catalog(SOURCE)
            api_key(SOURCE)
            photos, notes = photo_pool(RUNTIME)
            print(json.dumps({'status': 'ready', 'sourceCommit': commit, 'photoCount': len(photos),
                              'model': read_config(RUNTIME)['model'], 'apiRequests': 0, 'notes': notes}))
        else:
            day = args.publication_day or datetime.now(timezone.utc).astimezone(SEATTLE).date().isoformat()
            receipt = collect(args.run_id, day)
            print('SFZ_NEWS_RECEIPT=' + json.dumps(receipt, separators=(',', ':')))
        return 0
    except Exception as exc:
        print(f'News generation stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
