"""Apply verified photo credits to saved daily articles without generating content.

Run as laurawkr. The default --check is read-only. --apply backs up accepted
articles, updates only hero credit metadata, and publishes a new immutable news
release. Old releases, images, article prose and research caches are retained.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import news_core as news
from repair_citations import (current_publication, generation_lock, keep_backup,
                              ordinary, private_directory, regular_json, selected_site)

DAGS = str(Path(__file__).resolve().parents[2] / 'dags')
if DAGS not in sys.path:
    sys.path.insert(0, DAGS)
from fan_zone_photo_credits import format_caption, load_catalog, resolve_credit, validate_catalog


IMAGE = re.compile(r'/images/news/generated/([a-f0-9]{64})\.(jpg|png|webp)')


def canonical(article, site):
    """Mirror the existing accepted-history compatibility normalization."""
    result = deepcopy(article)
    news.assert_team(result, site)
    result['team'] = site['slug']
    result['tags'] = [site['slug'], *[tag for tag in result.get('tags', []) if tag.lower() != site['slug']]]
    return result


def credited_article(article, catalog, pool):
    result = deepcopy(article)
    hero = result['hero']
    match = IMAGE.fullmatch(hero.get('src', ''))
    if not match:
        return result, None
    checksum = match[1]
    if hero.get('sha256', checksum) != checksum:
        raise ValueError('Existing hero SHA256 differs from its immutable image filename')
    metadata = pool.get(checksum, (None, None, None, None, {}))[4]
    # Pool metadata can prove the identity of a renamed immutable copy. The
    # exact bytes must match; no player-name or visual guesses are permitted.
    credit = resolve_credit(catalog, sha256=checksum, metadata=metadata)
    if not credit or not credit.get('gettyAssetId'):
        return result, {'slug': result['slug'], 'sha256': checksum, 'src': hero['src']}
    day = article.get('generation', {}).get('publicationDay') or article['publishedAt'][:10]
    hero['caption'] = format_caption(credit, publication_day=day)
    hero['alt'] = credit.get('alt') or (credit['caption'] if len(credit['caption']) <= 1000
                                      else 'Getty Images editorial photograph')
    hero['credit'] = credit['credit']
    if credit.get('eventDate'):
        hero['eventDate'] = credit['eventDate']
    else:
        hero.pop('eventDate', None)
    hero['gettyAssetId'] = credit['gettyAssetId']
    return result, None


def apply_credits(site, catalog, *, apply=False):
    site = news.site_settings(site)
    catalog = validate_catalog(catalog)
    runtime = ordinary(Path(site['news_snapshot_dir']).parent, directory=True)
    ordinary(runtime / 'days', directory=True)
    with generation_lock(runtime):
        snapshot, manifest, published = current_publication(runtime, site)
        records = {}
        for path in sorted((runtime / 'days').glob('*/article.json')):
            ordinary(path.parent, directory=True)
            original_bytes = ordinary(path).read_bytes()
            article = json.loads(original_bytes)
            if article.get('generation', {}).get('publicationDay') != path.parent.name:
                raise ValueError('Accepted article has a mismatched publication day')
            if article.get('slug') in records:
                raise ValueError('Duplicate accepted article slug')
            records[article['slug']] = (path, original_bytes, article)
        # accepted_articles performs the same schema/history checks as publish.
        accepted = news.accepted_articles(runtime, site)
        if [a['slug'] for a in accepted] != [a['slug'] for a in published]:
            raise ValueError('Accepted history and current publication have different articles; nothing was changed')
        photos = Path(site['news_photos_dir'])
        if photos.exists():
            pool, notes = news.photo_pool(runtime, photos, photo_credits=catalog)
        else:
            pool, notes = {}, ['Photo directory is absent; only approved catalog SHA256 mappings can be used.']
        corrected, unmatched = [], []
        for article in published:
            updated, unknown = credited_article(article, catalog, pool)
            corrected.append(updated)
            if unknown:
                unmatched.append(unknown)
            src = article['hero'].get('src', '')
            match = IMAGE.fullmatch(src)
            if match:
                ordinary(runtime / 'assets', directory=True)
                asset = ordinary(runtime / 'assets' / (match[1] + '.' + match[2]))
                if news.digest(asset.read_bytes()) != match[1]:
                    raise ValueError('Retained news image failed its checksum; nothing was changed')
            elif src != news.FALLBACK['src']:
                raise ValueError('Unexpected accepted news image path; nothing was changed')
        catalog_bytes = json.dumps(catalog, sort_keys=True).encode()
        identity = news.digest((manifest['files']['articles.json'] + news.digest(catalog_bytes)).encode())[:24]
        run_id = f"photo-credits__{site['slug']}__{identity}"
        backup = runtime / 'repairs' / run_id
        changes = []
        for old, new, saved in zip(published, corrected, accepted):
            if saved not in (old, new):
                raise ValueError('Accepted history has unrelated unpublished changes; nothing was changed')
            if old == new:
                continue
            path, original_bytes, raw = records[old['slug']]
            original_backup = backup / (path.parent.name + '.article.json')
            if saved != old:
                # Recover only this repair's previously accepted changes. An
                # arbitrary local caption edit is never silently published.
                if canonical(regular_json(original_backup), site) != old:
                    raise ValueError('Interrupted credit update lacks its matching original article backup')
            changes.append((path, original_bytes, raw, new, original_backup, saved != new))
        result = dict(team=site['slug'], status='ready' if changes else 'already-current',
                      articlesUpdated=len(changes), unmatchedPhotos=unmatched, notes=notes,
                      sourceRequests=0, snapshot=str(snapshot))
        if not apply or not changes:
            return result
        commit = news.run(['git', '-C', site['website_root'], 'rev-parse', 'HEAD'])
        if not re.fullmatch(r'[a-f0-9]{40}', commit):
            raise ValueError('Could not identify the website commit for the publication receipt')
        private_directory(private_directory(runtime / 'repairs') / run_id)
        keep_backup(backup / 'manifest.json', (snapshot / 'manifest.json').read_bytes())
        keep_backup(backup / 'photo-credits.json', catalog_bytes)
        # Write all originals before accepting any change. A later retry can
        # distinguish this repair's partial writes from unrelated local edits.
        for path, original_bytes, raw, new, original_backup, needs_write in changes:
            if needs_write:
                keep_backup(original_backup, original_bytes)
        previous_umask = os.umask(0o022)
        try:
            for path, original_bytes, raw, new, original_backup, needs_write in changes:
                if needs_write:
                    updated = deepcopy(raw)
                    updated['hero'] = new['hero']
                    news.atomic_json(path, updated)
            receipt = news.publish(runtime, run_id, manifest['publicationDay'], commit, 0,
                                   {'requestsThisAttempt': 0},
                                   [f'Updated verified photo credits on {len(changes)} accepted articles; '
                                    'article text, images and research caches retained; no API calls.'], site=site)
            result.update(status='updated', runId=receipt['runId'], backup=str(backup),
                          snapshot=str((runtime / 'current').resolve()))
            return result
        finally:
            os.umask(previous_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--team', default='seahawks', help='Team key in config/active-sites.json')
    parser.add_argument('--credits-file', type=Path, help='Credit catalog JSON; defaults to checked-in config/photo-credits.json')
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--check', action='store_true', help='Read-only verification (the default)')
    operation.add_argument('--apply', action='store_true', help='Back up and publish verified credit metadata')
    args = parser.parse_args()
    try:
        if os.getuid() == 0:
            raise ValueError('Run as laurawkr, without sudo')
        result = apply_credits(selected_site(args.team), load_catalog(args.credits_file), apply=args.apply)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'PHOTO CREDIT UPDATE STOPPED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
