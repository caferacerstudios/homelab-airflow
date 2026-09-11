# Daily Seahawks articles — installation and operations

This package adds `sfz_daily_article` to the existing Airflow 3.3.1 installation. Default schedule: 08:00 in America/Los_Angeles, no catch-up, no automatic retries. It generates one accepted article per Seattle day using two bounded OpenAI Responses requests: live official-site research and structured article writing. The default model is `gpt-5.4-mini`, matching the existing player-profile model family. No BALLDONTLIE calls are added. OpenAI account/model access is checked by the first live run, not by local fixture tests.

The website imports `/var/lib/sfz-news/current` at normal prebuild. `/news` selects the newest publication as lead and the following six as Latest. Authored articles remain in `src/lib/news.ts`. Generated articles are imported into `src/data/news/generated-articles.json`. Old articles retain their detail routes, sources and images. Existing category/archive/RSS routes remain.

## Install

Run `python3 setup-sfz-news.py prepare` as laurawkr on wkr. It creates an isolated website worktree at `~/sfz-daily-news-setup/website`, based on `origin/main`, on the new branch `sfz/daily-news-v1`. It applies guarded edits to five existing files, adds the importer/data/helper/tests, runs focused tests and an offline build, commits those files and pushes the feature branch. If existing source no longer matches the edit anchors, it stops before changing those source files. It never switches the production or dev checkout.

Open the comparison link it prints, review the changes and merge that PR into main using the normal workflow. This script does not bypass protected main. If prepare finds an unrelated build failure, it retains the working branch and reports the failure; it does not reset or repair unrelated project files.

Run `python3 setup-sfz-news.py install`. It updates the production main checkout with a fast-forward pull, adds only this package's new Airflow files, creates `/var/lib/sfz-news` and its photo/state directories, and installs a dedicated restricted SSH connection using the existing working installer pattern. It asks sudo only to create the runtime root if needed. Run the overall installer as laurawkr, not root. It retains existing keys/settings on repeat installs and rejects a different implementation already occupying its new filenames. It makes no OpenAI requests and leaves a newly created DAG paused.

Add photos, then run `python3 setup-sfz-news.py run` for the first real article. Inspect the returned manifest and article. Run a normal website build (`cd ~/seahawksfanzone` then `npm run build`) to import it and publish HTML through the current host setup. Finally run `python3 setup-sfz-news.py enable` to unpause the daily schedule. These are separate explicit actions: setup does not secretly start paid writing or publish HTML.

## Photos

Drop JPEG, PNG or WebP files into `/var/lib/sfz-news/photos/`. Eight or more distinct photos support rotation away from the seven on the page. Exact duplicate files with different filenames count as one photo. A selected image is copied to retained assets with a content hash, so removing its original input never breaks old articles. Images do not reshuffle on a build.

Selection excludes both the recorded public front-page images and the latest seven accepted/authored articles. If the site has not been built for several days, these sets can differ and more than eight photos may be needed to avoid both. An exhausted or empty pool uses the neutral illustration, records a note, and keeps the article. Damaged/unsupported headers, symlinks and photos over 25 MiB are skipped.

Optional `/var/lib/sfz-news/photos/metadata.json`:

```json
{
  "lumen-field.jpg": {
    "alt": "Lumen Field viewed from the upper seating bowl",
    "caption": "Lumen Field in Seattle. Illustrative file photo.",
    "credit": "Laura McKinney"
  }
}
```

Without metadata, the caption and alt text describe a generic photo from the site's collection. The automatic writer does not infer the photo's subject. Existing custom editorial illustrations are retained. Generated stories use an accurate automatic-generation byline explanation; existing authored byline wording remains.

## Daily operation

- `python3 ~/setup-sfz-news.py status` lists recent Airflow runs and the accepted manifest.
- `python3 ~/setup-sfz-news.py run` triggers one manual run. A day with an accepted article makes no additional writing calls.
- `python3 ~/setup-sfz-news.py pause` pauses future scheduling. It does not terminate an active run.
- `python3 ~/setup-sfz-news.py enable` resumes scheduling.
- `node ~/seahawksfanzone/scripts/import-news-snapshot.mjs --check-only` validates the latest snapshot without changing website inputs.
- `python3 ~/setup-sfz-news.py record` commits and pushes only this package's Airflow files to the existing Airflow main branch after installation. It preserves unrelated staged changes.

`NEWS_SNAPSHOT_DIR` overrides the website importer path. Host production builds read the default host directory directly. An isolated dev build must have the parent mounted read-only, for example `--mount type=bind,src=/var/lib/sfz-news,dst=/var/lib/sfz-news,readonly`; mounting only the `current` symlink is insufficient. The script does not modify the host-owned dev deployment wrapper. Use the existing promotion workflow to bring the website changes to dev; its configured checkout stays on dev.

Model configuration is `/var/lib/sfz-news/config.json`. Changing `model` affects future days. A replacement model must support Responses web search, low reasoning and structured outputs. The research request permits at most four tool calls and 5,000 output tokens; writing permits 6,500 output tokens. No automatic retry loop is added. Request/token usage and response IDs are retained for each day's attempt; these records are not a dollar spending cap.

## State, failures and recovery

| Item | Location |
| --- | --- |
| Accepted article | `/var/lib/sfz-news/days/YYYY-MM-DD/article.json` |
| Research/writing responses and usage | Same day's directory |
| Retained selected photos | `/var/lib/sfz-news/assets/` |
| Immutable releases | `/var/lib/sfz-news/releases/` |
| Latest accepted snapshot | `/var/lib/sfz-news/current` |
| Airflow receipt | `/var/lib/homelab-pipelines/seahawks/news/<run-hash>/receipt.json` |
| SSH key | `~/homelab-airflow/secrets/sfz_news_ed25519` |
| Airflow source | `~/homelab-airflow/deployment/news/`, plus the two new DAG/hook files |

Back up the complete news runtime root together with the existing Airflow metadata/Fernet key and dedicated SSH key. No automatic history purge is installed. Releases share retained image inodes rather than duplicating photo bytes per day.

An unsuccessful request, incomplete response or invalid article does not replace the accepted snapshot. Raw successful responses are cached before validation so retries can resume without paying for completed stages again. If an invalid cached response needs replacement, pause the DAG, inspect the day's recorded output, and move only the failed stage's `*-response.json` aside before a deliberate retry. Never remove an accepted `article.json` to force another story. A transport timeout can leave a request's billing outcome unknown; a deliberate new request may incur another charge.

To correct accepted prose, pause future runs, back up that day's `article.json`, and edit that record while keeping its slug, original publishedAt and generation.publicationDay. Set updatedAt to the actual correction time and preserve the restricted paragraph/citation structure. Run the host runner with that day and a new run ID, for example `python3 ~/homelab-airflow/deployment/news/refresh_news.py --run-id manual-correction --publication-day 2026-09-11`. An already accepted day is republished from retained data without model calls. Run the importer check and normal build, then resume. Unsupported edits fail validation at import and do not overwrite the existing website input.

For rollback, pause `sfz_daily_article` and revert the website feature PR through the normal Git workflow. Keep the runtime directory and all image/history files. A previous release can be selected for investigation, but the importer intentionally refuses to erase locally known article history using a shorter snapshot.

Airflow generation and public HTML are separate. A successful daily DAG run becomes visible at the next successful normal build. This feature does not add another deployment timer or fix unrelated build failures. The first live API response, DAG execution and production rendering must be checked on wkr.

## Verification and API references

Local fixture tests cover daily deduplication, acceptance-before-publication recovery, source failure retention, historical retention, photo exclusion, duplicate image bytes, removed input photos, cached responses, safe citations, importer validation and newest-first ordering. The setup command repeats the targeted checks and a full offline Astro build against the real current repository before pushing the website branch.

Preparation checks passed: 13 Python tests, six Node tests, and an offline Astro build producing 171 HTML pages with nine temporary generated-article fixtures. Inspection of the built HTML confirmed exactly seven news cards, the newest lead, retained older article routes, consistent pagination, source links, automatic-generation wording and image captions. Those fixture articles are excluded from the delivered bootstrap. Live Airflow execution and paid API calls were not run from this workspace.

- [OpenAI Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [OpenAI structured output](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-5.4 mini capabilities](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
- [Airflow cron timetables](https://airflow.apache.org/docs/apache-airflow/3.3.1/_api/airflow/timetables/trigger/index.html)

Prepared from the September 10 recap installer/package and the website source read at `67bfff5be65f9a65d00c4f7b27c128a21ed052f2`. The private live Airflow repository and host configuration were not directly accessible during preparation.
