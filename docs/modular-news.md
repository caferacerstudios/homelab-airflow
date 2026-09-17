# Twice-weekly articles controlled by active sites

The existing `sfz_daily_article` DAG reads the Airflow Variable
`fan_zone_active_sites` once each time Airflow parses the DAG. It creates
separate `generate_article_<slug>` and `save_run_receipt_<slug>` tasks for
entries with `enabled: true`. Each pair is a separate branch in the graph.
Display names include the city and team, such as `Generate article: Denver Broncos`.
The schedule is **Tuesday and Friday at 08:00 America/Los_Angeles**
(`0 8 * * 2,5`, using `CronTriggerTimetable`). It stays at 8 a.m. through daylight
saving changes. The existing DAG/task IDs, `sfz_news_host` connection, zero-retry
policy, no-catch-up behavior and DAG pause state are preserved.

| Team | Article output | Photo folder |
| --- | --- | --- |
| Seahawks | `/var/lib/sfz-news/current` | `/var/lib/sfz-news/photos/` |
| Broncos | `/var/lib/boncosfz-news/current` | `/var/lib/boncosfz-news/photos/` |

`boncosfz` matches the requested directory spelling; the team slug is `broncos`.
The checked-in `config/active-sites.json` starts with both teams enabled.
An existing Airflow Variable takes precedence and is preserved during installation.

## Editorial editions

| Publication day | Focus |
| --- | --- |
| Friday | Preview the next confirmed matchup: tactical keys, relevant verified personnel/injury updates and what to watch. |
| Tuesday | Choose one strongest supported angle: team performance recap, major team headline or strategy breakdown. |

The shared generator applies the edition brief to both research and writing.
It uses the run's stable publication date in the team's configured timezone,
not the worker's current weekday. Existing `prompts.article` preferences still
apply within that brief; conflicting old daily/topic instructions do not override
it. No live Variable migration is needed. All currently configured sites use
Pacific time.

Friday research verifies the next game rather than assuming a Sunday matchup;
a completed Thursday game is not presented as upcoming. Bye weeks and offseason
articles explain the context and use a sourced preparation/strategy angle if
there is no meaningful confirmed matchup. Tuesday research verifies that a game
has finished and can select a headline or strategy topic when there is no recent
game. It should add analysis rather than duplicate the separate game recap DAG.

This schedules two article opportunities per week **per enabled team**. Failed
source or content validation can prevent publication. Explicit manual runs on
other days remain available for an extra timely headline or analysis article.
Accepted articles are still reused once per team/publication day, with unchanged
URLs, history, photos and source requirements. Missing past days are not backfilled
with current news. Each new article still uses at most two model requests.

## Configuration

Open <http://localhost:8085/variables> and edit `fan_zone_active_sites`.
Its value is the plain JSON object in `config/active-sites.json`.
Each team supplies `enabled`, `name`, `city`, `source_domains`,
`prompts.article`, `news_snapshot_dir` and `news_photos_dir`.
`abbreviation` and `balldontlie_team_id` are retained as site metadata;
articles do not call BALLDONTLIE, so Denver's ID can remain null.
Optional `website_root` defaults to `/home/laurawkr/seahawksfanzone` for Seattle
and `/home/laurawkr/templatefanzone` for other teams. Publication days default
to the existing America/Los_Angeles timezone.

To add a team, copy an entry, set its slug/name/city, source domains, article
prompt, and separate `/var/lib/<site>-news/current` and sibling `photos` paths.
Create the new parent with user-owned `photos`, `assets`, `days` and `releases`
subfolders before enabling it. Setting `enabled: false` removes its tasks from the graph after the next
DAG parse. New runs use the updated graph. Do not create `current` manually: the runner publishes that
symlink after validating a complete release. The supplied installer initializes
these directories for the configured new teams.

The Variable is read during DAG parsing so all enabled websites appear as
separate named nodes before execution. The small configuration is fetched once
per parse. The checked-in JSON is a fallback only when the Variable is absent. Invalid or overlapping
paths fail validation before any article task starts. Editing the UI updates the graph after Airflow reparses the DAG; it does not
rewrite an already-recorded DAG version or commit a Git file. View the updated
DAG graph instead of an older run when checking the new task names.
After UI edits, export/copy this Variable's value to `config/active-sites.json`
in both checkouts and commit that file. The website also accepts Airflow's
single-variable export envelope via `ACTIVE_SITES_FILE`.

## Existing Seattle behavior

The existing news SSH connection and forced command are reused. Old Seattle
requests still work; the new optional `site` payload carries the JSON config.
The installer does not reinstall keys or alter connections, services or DAG
pause settings. It does not invoke the original Airflow setup installer.

Seattle keeps its existing `days`, `assets`, `photos`, `releases`, model config
and `current` snapshot. Accepted days are reused without another paid generation.
A new team gets separate state and photos. Its initial model is copied from
Seattle only when the new team's model config is missing. The existing OpenAI
credential remains in the original production environment file.

Each article, collection, manifest and receipt carries a `team` value. Existing
untagged Seattle history remains valid; new team history requires explicit tags.
The configured article prompt supplements the shared research/writing rules.
Photo selection uses only the selected team's folder, history and visible
front-page articles, retaining the existing hash and no-repeat rules. Add photos
and their normal `metadata.json` captions/credits to the team's photo folder.
An insufficient pool uses the existing illustration fallback. Selected image
bytes remain in accepted snapshots if input photos are later removed.

Editorial articles and game recaps remain separate. This update changes no recap,
NFL-data, ticket DAG or collector.

## Website build

The article DAG publishes articles; a website build imports them. It does not
build or deploy the website itself. The template site's root is
`/home/laurawkr/templatefanzone`. The installer builds its Broncos preview.
After future articles arrive, rebuild it using:

```bash
cd /home/laurawkr/templatefanzone
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/app" -w /app \
  --mount type=bind,src=/var/lib/boncosfz-news,dst=/var/lib/boncosfz-news,readonly \
  -e TEAM=broncos -e NPM_CONFIG_CACHE=/tmp/npm-cache \
  node:22-bookworm npm run build
```

For a Seattle template build use `TEAM=seahawks` and `/var/lib/sfz-news` in
both mount positions. Mount the parent directory because `current` points into
`releases`. The existing preview container serves `dist`; refresh the browser
after a successful build. No preview Docker Compose command is needed.

The website imports the selected team's validated articles and retained images,
then runs offline Astro without article generation or API refreshes. News source
text and citations remain literal; Seattle stories are excluded from Broncos.
A missing first Broncos snapshot produces an empty news page. Malformed,
mixed-team or incomplete snapshots fail the build while preserving served
`dist`. Existing accepted website article history is retained.

The source catalogs `/data/news-front-page.json` and
`scripts/export-news-catalog.mjs` include team tags. The host uses the selected
team's catalog for photo exclusion. Other website datasets remain outside this
news update.

## Checks

The news tests cover legacy Seattle requests and retained history, per-team
prompts/photos/paths, separate named tasks, JSON validation and receipt identity.
The website's `npm run test:template` and news snapshot tests cover team
filtering, checksums and history preservation. They do not call paid APIs.
The installer validates the update and commits/pushes only its listed files
using the server's existing Git credentials.

## Updating an existing installation

After the PR is approved and merged, update the existing
`/home/laurawkr/homelab-airflow` main checkout through the normal preserving Git
workflow. Both the mounted DAG and host `deployment/news` runner need the new
revision. Routine mounted code changes do not need new keys, a platform rebuild,
or the original bootstrap installer. Validate DAG import errors and confirm the
next run is Tuesday or Friday at 08:00 Pacific. Existing accepted articles remain
untouched, and the next normal website build imports successful new snapshots.

A partially completed, unaccepted attempt from the old prompts may have cached
responses that do not match the new editorial settings. The existing cache guard
will stop instead of silently reusing them or making replacement paid requests.
Inspect that attempt using the recovery procedure in `daily-news-airflow.md`;
never delete an accepted `article.json` to regenerate it.

## Configuration edits blocking generation

`Commit or resolve edits to the team/news configuration before generation` means
SSH succeeded, but the configured website checkout has uncommitted changes under
`template-tools`, `src/lib/news-team.mjs` or `config/active-sites.json`. The error
now includes the checkout and exact changed paths, including untracked files.
The related news-source guard likewise identifies changed exporter/importer or
authored-news files. Inspect `git status --short` and `git diff` in the named
checkout, review staged changes with `git diff --cached`, and preserve/review
untracked files before deciding how to commit or resolve them. Do not blindly
reset, stash or auto-commit those edits.

Normal `.team-build` and `dist` output is outside these guards. The supplied
September 17 log does not identify the dirty files, so this diagnostic improvement
does not itself resolve the host edits or prove a successful live run.
