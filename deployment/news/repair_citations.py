"""Repair proven leaked source markers without rewriting or regenerating an article.

Run as laurawkr. --check is read-only; --apply retains the original release and
publishes the same accepted history after repairing just the verified paragraph
markers. Cached research IDs and writing response are required evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import fcntl
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

import news_core as news


CONFIG = Path(__file__).resolve().parents[2] / 'config/active-sites.json'


def ordinary(path, *, directory=False):
    """Never follow an unexpected link in mutable repair inputs or destinations."""
    path = Path(path)
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'Expected an ordinary path owned by the current user: {path}')
    return path


def regular_json(path):
    return json.loads(ordinary(path).read_text())


def selected_site(team):
    config = news.read_json(CONFIG)
    if not isinstance(config, dict) or team not in config:
        raise ValueError(f'Team {team!r} is not in config/active-sites.json')
    site = news.site_settings(dict(config[team], slug=team))
    if team == 'seahawks' and site['news_snapshot_dir'] != '/var/lib/sfz-news/current':
        raise ValueError('The existing Seattle news output must stay /var/lib/sfz-news/current')
    return site


@contextmanager
def generation_lock(runtime):
    path = ordinary(runtime / 'generation.lock')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid():
            raise ValueError('Invalid existing generation lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Daily news generation is running; retry after that task finishes') from None
        yield
    finally:
        os.close(fd)


def reconstruction(directory, article, site, day):
    request = regular_json(directory / 'article-request.json')
    prompt = request.get('input')
    if not isinstance(prompt, str) or prompt.count('\nSOURCE IDS:\n') != 1:
        raise ValueError('Cached writing request lacks one unambiguous original source registry')
    sources = json.loads(prompt.split('\nSOURCE IDS:\n', 1)[1])
    if not isinstance(sources, list) or not 2 <= len(sources) <= 12:
        raise ValueError('Invalid cached original research source registry')
    by_id = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'id', 'label', 'url'}:
            raise ValueError('Invalid cached research source fields')
        identifier, label, url = source['id'], source['label'], source['url']
        if not isinstance(identifier, str) or not re.fullmatch(r'S[1-9][0-9]*', identifier) or identifier in by_id:
            raise ValueError('Cached research source IDs must be unique')
        if not isinstance(label, str) or not label.strip() or not isinstance(url, str):
            raise ValueError('Invalid cached research source label or URL')
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
                host == domain or host.endswith('.' + domain) for domain in site['source_domains']):
            raise ValueError('Cached source URL is not an official HTTPS source for this team')
        by_id[identifier] = source
    response = regular_json(directory / 'article-response.json')
    draft = json.loads(news.response_text(response))
    if not isinstance(draft, dict):
        raise ValueError('Invalid cached article draft')
    if (article.get('headline') != news.clean_text(draft.get('headline'), 15, 180)
            or article.get('dek') != news.clean_text(draft.get('dek'), 30, 360)
            or article.get('slug') != f"daily-{site['slug']}-{day}-{draft.get('slug')}"):
        raise ValueError('Accepted article identity or prose differs from the cached writing response')
    sections = draft.get('sections')
    if not isinstance(sections, list) or not 2 <= len(sections) <= 8:
        raise ValueError('Invalid cached article sections')
    blocks, used_ids = [], []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError('Invalid cached article section')
        blocks.append({'type': 'heading', 'heading': news.clean_text(section.get('heading'), 1, 150)})
        paragraphs = section.get('paragraphs')
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 5:
            raise ValueError('Invalid cached article paragraphs')
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                raise ValueError('Invalid cached article paragraph')
            ids = paragraph.get('sourceIds')
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 4
                    or any(not isinstance(i, str) or i not in by_id for i in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError('Cached paragraph cites unknown or duplicate research source IDs')
            old_text = news.clean_text(paragraph.get('text'), 30, 5000, allow_source_markers=True)
            new_text = news.normalize_paragraph_text(paragraph.get('text'), ids)
            used_ids.extend(i for i in ids if i not in used_ids)
            blocks.append((old_text, new_text, ids))
    original, corrected = [], []
    for block in blocks:
        if isinstance(block, dict):
            original.append(block)
            corrected.append(dict(block))
        else:
            old_text, new_text, ids = block
            links = ' '.join(f'<a href="{html.escape(by_id[i]["url"], quote=True)}">[{used_ids.index(i) + 1}]</a>' for i in ids)
            original.append({'type': 'paragraph', 'html': html.escape(old_text) + ' ' + links})
            corrected.append({'type': 'paragraph', 'html': html.escape(new_text) + ' ' + links})
    final_sources = [{'label': by_id[i]['label'], 'url': by_id[i]['url']} for i in used_ids]
    if article.get('sources') != final_sources:
        raise ValueError('Accepted article source order or URLs differ from the cached writing response')
    if article.get('body') not in (original, corrected):
        raise ValueError('Accepted article body differs from the original and corrected cached draft; nothing was changed')
    repaired = deepcopy(article)
    repaired['body'] = corrected
    return original, repaired


def current_publication(runtime, site):
    current = runtime / 'current'
    if not current.is_symlink():
        raise ValueError('Expected the existing news current symlink')
    target = os.readlink(current)
    if not re.fullmatch(r'releases/[a-f0-9]{32}', target):
        raise ValueError('News current must point to an immutable release in this runtime')
    ordinary(runtime / 'releases', directory=True)
    snapshot = ordinary(runtime / target, directory=True)
    manifest = regular_json(snapshot / 'manifest.json')
    files = manifest.get('files', {})
    if not isinstance(files, dict):
        raise ValueError('Invalid current news manifest')
    for name in files:
        if name != 'articles.json' and not re.fullmatch(r'images/[a-f0-9]{64}\.(jpg|png|webp)', name):
            raise ValueError('Invalid current snapshot path')
        if name.startswith('images/'):
            ordinary(snapshot / 'images', directory=True)
        ordinary(snapshot / name)
    news.verify_snapshot(current, site)
    return snapshot, manifest, regular_json(snapshot / 'articles.json')['articles']


def private_directory(path):
    if path.exists() or path.is_symlink():
        ordinary(path, directory=True)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f'Existing repair backup directory is not private: {path}')
    else:
        path.mkdir(mode=0o700)
    return path


def keep_backup(path, value):
    if path.exists() or path.is_symlink():
        if regular_json(path) != json.loads(value):
            raise ValueError(f'Existing repair backup differs from the verified original: {path}')
        return
    # Backups contain exact original bytes. The directory is private; no old
    # publication or generation lock permissions are changed.
    news.atomic_bytes(path, value)
    path.chmod(0o600)


def repair(day, site, *, apply=False):
    site = news.site_settings(site)
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError('Use an ISO publication day: YYYY-MM-DD')
    runtime = ordinary(Path(site['news_snapshot_dir']).parent, directory=True)
    ordinary(runtime / 'days', directory=True)
    directory = ordinary(runtime / 'days' / day, directory=True)
    with generation_lock(runtime):
        target = ordinary(directory / 'article.json')
        accepted_bytes = target.read_bytes()
        accepted = json.loads(accepted_bytes)
        news.assert_team(accepted, site)
        if accepted.get('generation', {}).get('publicationDay') != day:
            raise ValueError('Accepted article publication day differs from the requested day')
        original_body, corrected = reconstruction(directory, accepted, site, day)
        snapshot, manifest, incoming = current_publication(runtime, site)
        matches = [a for a in incoming if a.get('slug') == accepted['slug']]
        if len(matches) != 1:
            raise ValueError('Current publication must contain this exact accepted article once')
        published = matches[0]
        published_original = deepcopy(corrected)
        published_original['body'] = original_body
        if published not in (published_original, corrected):
            raise ValueError('Current article differs from the accepted article beyond the verified source markers')
        # Validate every retained accepted input before publication. Unrelated
        # unpublished edits need their own review, not inclusion in this repair.
        for day_dir in sorted((runtime / 'days').iterdir()):
            if day_dir.is_dir() and (day_dir / 'article.json').exists():
                ordinary(day_dir, directory=True)
                ordinary(day_dir / 'article.json')
        accepted_history = news.accepted_articles(runtime, site)
        expected_history = [corrected if a['slug'] == corrected['slug'] else a for a in accepted_history]
        expected_incoming = [corrected if a['slug'] == corrected['slug'] else a for a in incoming]
        if expected_history != expected_incoming:
            raise ValueError('Accepted history and current publication have unrelated differences; nothing was changed')
        for article in expected_history:
            src = article['hero'].get('src')
            match = re.fullmatch(r'/images/news/generated/([a-f0-9]{64}\.(jpg|png|webp))', src or '')
            if match:
                ordinary(runtime / 'assets', directory=True)
                asset = ordinary(runtime / 'assets' / match[1])
                if news.digest(asset.read_bytes()) != match[1].split('.')[0]:
                    raise ValueError('Retained news image failed its checksum; nothing was changed')
            elif src != news.FALLBACK['src']:
                raise ValueError('Unexpected accepted news image path; nothing was changed')
        changed = sum(old != new for old, new in zip(original_body, corrected['body']))
        needs_acceptance = accepted != corrected
        needs_publication = published != corrected
        result = dict(team=site['slug'], day=day, slug=accepted['slug'], paragraphsRepaired=changed,
                      acceptedChange=needs_acceptance, publicationChange=needs_publication,
                      sourceRequests=0, status='ready' if needs_acceptance or needs_publication else 'already-correct')
        if not apply or result['status'] == 'already-correct':
            return result
        commit = news.run(['git', '-C', site['website_root'], 'rev-parse', 'HEAD'])
        if not re.fullmatch(r'[a-f0-9]{40}', commit):
            raise ValueError('Could not identify the website source commit for the publication receipt')
        identity = news.digest(json.dumps({'slug': accepted['slug'], 'body': original_body}, sort_keys=True).encode())[:20]
        run_id = f"citation-repair__{site['slug']}__{day}__{identity}"
        backup = private_directory(private_directory(runtime / 'repairs') / run_id)
        if needs_acceptance:
            keep_backup(backup / 'article.json', accepted_bytes)
        else:
            # A prior attempt accepted the repair but did not publish. Require
            # its original backup, never synthesize evidence of the old record.
            if regular_json(backup / 'article.json') != published_original:
                raise ValueError('Interrupted repair has no matching original accepted-article backup')
        keep_backup(backup / 'manifest.json', (snapshot / 'manifest.json').read_bytes())
        old_umask = os.umask(0o022)
        try:
            if needs_acceptance:
                news.atomic_json(target, corrected)
            if needs_publication:
                receipt = news.publish(runtime, run_id, day, commit, 0, {'requestsThisAttempt': 0},
                    [f'Removed verified redundant research source markers from {accepted["slug"]}; '
                     'cached writing evidence retained; no generation or source requests.'], site=site)
                result['runId'] = receipt['runId']
            result.update(status='repaired', backup=str(backup), snapshot=str((runtime / 'current').resolve()))
            return result
        finally:
            os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--team', default='seahawks', help='Team key in config/active-sites.json (default: seahawks)')
    parser.add_argument('--day', required=True, help='Existing accepted article publication day, YYYY-MM-DD')
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--check', action='store_true', help='Read-only verification (the default)')
    operation.add_argument('--apply', action='store_true', help='Back up and republish the verified correction')
    args = parser.parse_args()
    try:
        if os.getuid() == 0:
            raise ValueError('Run as laurawkr, without sudo')
        result = repair(args.day, selected_site(args.team), apply=args.apply)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'CITATION REPAIR STOPPED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
