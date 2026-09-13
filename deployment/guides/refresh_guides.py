#!/usr/bin/env python3
"""Research game guides; optionally regenerate every upcoming game on the host."""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys

from guides_core import collect, preflight, runtime_for, site_settings


def interrupted(_signum, _frame):
    raise InterruptedError('Guide research interrupted; inspect the current manifest before retrying')


def load_active_sites() -> dict:
    # guides_core imports shared runners that also have an install.py. Load this
    # sibling explicitly, and call only its read-only Variable lookup.
    spec = importlib.util.spec_from_file_location('guides_installer', Path(__file__).with_name('install.py'))
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer.load_active_sites()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run-id')
    sites = parser.add_mutually_exclusive_group(required=True)
    sites.add_argument('--site-json', help='Registered site request supplied by the Airflow SSH hook')
    sites.add_argument('--team', metavar='SLUG', help='One enabled team from the live Airflow Variable')
    sites.add_argument('--all-active', action='store_true', help='All enabled teams from the live Airflow Variable')
    parser.add_argument('--force-all', action='store_true',
                        help='Regenerate every upcoming game, with fresh research for this run ID')
    args = parser.parse_args()
    if args.force_all and args.run_id is None:
        parser.error('--force-all requires --run-id')
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.site_json is not None:
            requests = [json.loads(args.site_json)]
        else:
            active = load_active_sites()
            if args.team is not None:
                if args.team not in active or not active[args.team]['enabled']:
                    raise ValueError(f'Team {args.team!r} is not enabled in the active-site configuration')
                requests = [active[args.team]]
            else:
                requests = [site for _, site in sorted(active.items()) if site['enabled']]
            if not requests:
                raise ValueError('No active teams are enabled')
        for index, site in enumerate(requests, start=1):
            selected = site_settings(site)
            if args.site_json is None:
                action = 'Checking' if args.check else 'Refreshing'
                print(f'{action} {selected["slug"]} ({index}/{len(requests)})', flush=True)
            if args.check:
                commit = preflight(selected, runtime_for(selected))
                print(json.dumps({'status': 'ready', 'team': selected['slug'], 'sourceCommit': commit,
                                  'snapshotPath': str(runtime_for(selected) / 'current'), 'sourceRequests': 0}),
                      flush=True)
            else:
                options = {'force_all': True} if args.force_all else {}
                receipt = collect(args.run_id, site, **options)
                print('SFZ_GUIDES_RECEIPT=' + json.dumps(receipt, separators=(',', ':')), flush=True)
        return 0
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f'Guide refresh failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
