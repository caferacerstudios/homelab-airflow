#!/usr/bin/env python3
"""Research a bounded set of game guides; publish only an isolated snapshot."""
import argparse
import json
import signal
import sys

from guides_core import collect, preflight, runtime_for, site_settings


def interrupted(_signum, _frame):
    raise InterruptedError('Guide research interrupted; inspect the current manifest before retrying')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run-id')
    parser.add_argument('--site-json', required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        site = json.loads(args.site_json)
        selected = site_settings(site)
        if args.check:
            commit = preflight(selected, runtime_for(selected))
            print(json.dumps({'status': 'ready', 'team': selected['slug'], 'sourceCommit': commit,
                              'snapshotPath': str(runtime_for(selected) / 'current'), 'sourceRequests': 0}))
        else:
            print('SFZ_GUIDES_RECEIPT=' + json.dumps(collect(args.run_id, site), separators=(',', ':')))
        return 0
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f'Guide refresh failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
