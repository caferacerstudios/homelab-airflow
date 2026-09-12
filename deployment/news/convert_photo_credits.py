"""Convert a Getty download CSV to Fan Zone photo credits, excluding account data.

The CSV's Caption supplies the photographer; Contributor is an agency category.
Use --airflow-import to wrap the result for Airflow's JSON Variables import.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import json
from pathlib import Path
import re
import sys

DAGS = str(Path(__file__).resolve().parents[2] / 'dags')
if DAGS not in sys.path:
    sys.path.insert(0, DAGS)


MONTHS = {name: number for number, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'), 1)}
EVENT_DATE = re.compile(r'\bon (' + '|'.join(MONTHS) + r')\s+(\d{1,2}),?\s+(\d{4})\b')
CREDIT = re.compile(r'\s*\((Photo by [^()\r\n]+/Getty Images)\)\s*$')


def convert(csv_path, existing=None):
    from fan_zone_photo_credits import validate_catalog
    catalog = validate_catalog(existing) if existing is not None else {'schema_version': 1, 'assets': {}, 'sha256': {}}
    seen = {}
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as source:
        rows = csv.DictReader(source)
        if not {'Item #', 'Caption', 'Media type', 'Download canceled'} <= set(rows.fieldnames or ()):
            raise ValueError('Expected Getty download CSV columns: Item #, Caption, Media type, Download canceled')
        for number, row in enumerate(rows, 2):
            if row['Download canceled'].strip():
                continue
            if row['Media type'].strip().lower() not in ('photo', 'photos'):
                continue
            identifier = row['Item #'].strip()
            if not re.fullmatch(r'[0-9]{5,20}', identifier):
                raise ValueError(f'CSV row {number}: invalid Getty Item #')
            original = row['Caption'].strip()
            match = CREDIT.search(original)
            if not match:
                raise ValueError(f'CSV row {number}: Caption lacks an explicit photographer/Getty Images credit')
            caption = original[:match.start()].strip()
            if not caption:
                raise ValueError(f'CSV row {number}: empty editorial caption')
            asset = {'caption': caption, 'credit': match[1]}
            dates = {date(int(year), MONTHS[month], int(day)).isoformat()
                     for month, day, year in EVENT_DATE.findall(caption)}
            if len(dates) == 1:
                asset['eventDate'] = next(iter(dates))
            # Keep the supplied factual description as alt text; never inherit
            # an old hand-written description that could identify someone else.
            asset['alt'] = caption
            if identifier in seen and seen[identifier] != asset:
                raise ValueError(f'CSV row {number}: conflicting captions for Getty asset {identifier}')
            seen[identifier] = asset
    if not seen:
        raise ValueError('The CSV contains no uncanceled photos with explicit credits')
    catalog['assets'].update(seen)
    return validate_catalog(catalog)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--existing', type=Path, help='Merge with an existing catalog, retaining approved SHA256 mappings')
    parser.add_argument('--airflow-import', action='store_true', help='Wrap JSON as the fan_zone_photo_credits Airflow Variable')
    args = parser.parse_args()
    try:
        existing = json.loads(args.existing.read_text()) if args.existing else None
        catalog = convert(args.csv, existing)
        output = {'fan_zone_photo_credits': catalog} if args.airflow_import else catalog
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
        print(f'Wrote {len(catalog["assets"])} photo credits and {len(catalog["sha256"])} approved image hashes to {args.output}')
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
        print(f'PHOTO CREDIT CONVERSION STOPPED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
