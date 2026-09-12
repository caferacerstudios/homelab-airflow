# Daily articles controlled by active sites

The existing `sfz_daily_article` DAG reads the Airflow Variable
`fan_zone_active_sites` once each time Airflow parses the DAG. It creates
separate `generate_article_<slug>` and `save_run_receipt_<slug>` tasks for
entries with `enabled: true`. Each pair is a separate branch in the graph.
Display names include the city and team, such as `Generate article: Denver Broncos`. The existing daily 08:00 America/Los_Angeles schedule,
`sfz_news_host` connection, retry policy and DAG pause state are preserved.

| Team | Daily article output | Photo folder |
| --- | --- | --- |
| Seahawks | `/var/lib/sfz-news/current` | `/var/lib/sfz-news/photos/` |
| Broncos | `/var/lib/boncosfz-news/current` | `/var/lib/boncosfz-news/photos/` |

`boncosfz` matches the requested directory spelling; the team slug is `broncos`.
The checked-in `config/active-sites.json` starts with both teams enabled.
An existing Airflow Variable takes precedence and is preserved during installation.

## Configuration

Open <http://localhost:8085/variables> and edit `fan_zone_active_sites`.
Its value is the plain JSON object in `config/active-sites.json`.
Each team supplies `enabled`, `name`, `city`, `source_domains`,
`prompts.article`, `news_snapshot_dir` and `news_photos_dir`.
`abbreviation` and `balldontlie_team_id` are retained as site metadata;
daily articles do not call BALLDONTLIE, so Denver's ID can remain null.
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

Daily articles and game recaps remain separate. This update changes no recap,
NFL-data, ticket DAG or collector.

## Website build

The daily DAG publishes articles; a website build imports them. It does not
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
