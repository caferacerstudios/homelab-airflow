# Fan Zone — Adding a Team to the Shared Template Preview

**Owner:** Laura · **Host:** `wkr` / `192.168.88.3` · **User:** `laurawkr`  
**Updated:** September 13, 2026 UTC (September 12 Pacific)  
**Worked example:** New England Patriots, the sixth team after Seahawks, Broncos, Packers, Vikings and Chiefs.

This runbook records the Patriots addition and provides the checklist for adding the next team. A team is added to the existing template checkout, shared data pipelines and preview. The result is the same command pattern used for Broncos and Packers:

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh patriots
```

A successful build changes the team displayed by the shared preview at `http://192.168.88.3:4326/`. Public hosting requires a separate deployment decision. Confluence documentation updates were approved after the successful Patriots activation.

## 1. Quick sequence and completion criteria

| Step | Action | Evidence needed before continuing |
| --- | --- | --- |
| 1 | Review the current repositories, live site Variable and preview configuration. | Correct checkout roots, current branch/changes understood, existing site values and mounts recorded. |
| 2 | Add the selected team's source configuration, presentation, history, personnel sources and EventSpy coverage. | Reviewed changes applied to both repositories; supported build slug; focused tests pass. |
| 3 | Commit only the reviewed source. | Source used by the host runners is committed and passes their clean-source checks. |
| 4 | Prepare the new host paths and registration. | Additive host check succeeds; previous registry backed up; existing outputs preserved. |
| 5 | Validate the actual Airflow imports and initial SSH checks. | Six DAGs import with the candidate team; NFL, news and roster checks succeed without collection. |
| 6 | Add the read-only ticket feed to the existing preview. | Candidate nginx syntax passes; original mounts/settings retained; previous homepage still served. |
| 7 | Enable only the new entry in the live Variable. | Fresh-value comparison and readback pass; existing site values retained. |
| 8 | Seed NFL through the existing DAG. | The new team's NFL work task and receipt succeed; valid `-nfl/current` exists. |
| 9 | Prepare the EventSpy schedule cache and check dependent inputs. | Real NFL identities bind to coverage; recap/guide preflight passes; enabled guide builds have a guide publication. |
| 10 | Build, inspect and record the result. | The selected team renders correctly; requested data sections have their own successful publications. |

**Source applied, host prepared, team enabled, data published and website built are separate milestones.** A source update cannot create an NFL snapshot. A source-free check cannot create an article, roster or guide. A successful DAG does not rebuild the static site automatically.

## 2. Established architecture and locations

| Purpose | Current location or behavior |
| --- | --- |
| Shared template repository | `caferacerstudios/template-fan-zone` |
| Template preview checkout | `/home/laurawkr/templatefanzone` |
| Airflow repository and checkout | `caferacerstudios/homelab-airflow` at `/home/laurawkr/homelab-airflow` |
| Existing Seattle production checkout | `/home/laurawkr/seahawksfanzone`; separate from the preview |
| Airflow platform | Docker Compose, Airflow 3.3.1 / Python 3.12, existing scheduler/DAG processor/API server/triggerer/PostgreSQL services |
| Runtime team configuration | Airflow Variable `fan_zone_active_sites`, edited through the existing Variables UI at `http://localhost:8085/variables` |
| Checked-in team configuration | `config/active-sites.json` in both repositories |
| Approved photo metadata | Airflow Variable `fan_zone_photo_credits`, with the existing checked-in catalog fallback |
| Root-owned host identity/path registry | `/opt/fanzone-shared/settings.json` |
| Preview container | `templatefanzone-web`, serving the checkout mount `/site` with nginx `root /site/dist` |
| Preview indexing | Existing `X-Robots-Tag: noindex, nofollow` policy retained |

The present `fan_zone_active_sites` Variable is the runtime authority and overrides the checked-in file. Only when it is absent does the DAG code fall back to `/opt/airflow/config/active-sites.json`. The build helper reads the template checkout's own configuration; an explicit build does not require the checked-in scheduling flag to be enabled.

The NFL host runner currently uses the generic fetcher and API-key source in `/home/laurawkr/seahawksfanzone`, including for other teams. It renders selected-team data in private work and publishes to that team's registered output. This is an existing shared-code dependency, not a build or deployment of Seattle production. Its source-free check verifies that dependency.

## 3. Define the team's identity and output contract

Confirm the display name, city or region, abbreviation, division and BALLDONTLIE team identifier against current authoritative sources. Do not infer numeric identifiers from another provider. Patriots uses `patriots`, `Patriots`, `New England`, `NE`, `AFC East` and BALLDONTLIE ID `1`.

Choose the output prefix once. Existing spelling is part of the contract: Seattle uses `sfz`, and Denver continues to use the requested `boncosfz` prefix. Do not rename those directories while adding another team.

| Input/output | Patriots example |
| --- | --- |
| Daily articles | `/var/lib/patriotsfz-news/current` |
| Approved article photos | `/var/lib/patriotsfz-news/photos` |
| NFL schedule, player statistics and standings | `/var/lib/patriotsfz-nfl/current` |
| Final-game recaps | `/var/lib/patriotsfz-recaps/current` |
| Official roster, injuries and transactions | `/var/lib/patriotsfz-roster/current` |
| Game Day Guides and Where to Watch | `/var/lib/patriotsfz-guides/current` |
| EventSpy published feed | `/var/lib/patriotsfz-eventspy-mirror/dev/public` |
| EventSpy schedule cache | `/var/lib/fanzone-eventspy/schedules/patriots.json` |
| Preview container feed mount | `/srv/fanzone-eventspy/patriots` |
| Browser ticket request | `/data/eventspy-mirror/patriots/<realGameId>.json` |

The roster and guide paths derive from the news path by replacing `-news/current` with `-roster/current` or `-guides/current`. The build mounts snapshot **parent directories** read-only so `current` symlinks can resolve into their published run directories. Keep the existing manifest, checksum, team identity and publication formats.

The checked-in Patriots example below is a staging configuration. Its disabled state prevents fallback scheduling before host preparation; the live entry was enabled separately during activation. For the next team, use independently verified identity and sources, not a blind text replacement of this example.

```json
{
  "patriots": {
    "enabled": false,
    "name": "Patriots",
    "city": "New England",
    "abbreviation": "NE",
    "balldontlie_team_id": 1,
    "news_snapshot_dir": "/var/lib/patriotsfz-news/current",
    "news_photos_dir": "/var/lib/patriotsfz-news/photos",
    "source_domains": ["patriots.com", "nfl.com"],
    "prompts": {
      "article": "Write for New England Patriots fans. Explain the most significant verified development and what it means for the team.",
      "recap": "Write a thorough game recap for New England Patriots fans using only the supplied final score, verified game statistics and play-by-play. Explain the turning points, decisive possessions and performances with specific evidence. Aim for 600–900 words when the evidence supports it; keep it shorter when data is limited. Distinguish verified facts from analysis. Never invent quotes, injuries, weather, playoff implications or plays. Preserve the real opponent names and describe the result from the New England perspective."
    },
    "slug": "patriots",
    "website_root": "/home/laurawkr/templatefanzone",
    "timezone": "America/Los_Angeles",
    "eventspy": {
      "output_dir": "/var/lib/patriotsfz-eventspy-mirror/dev/public",
      "coverage_file": "patriots.json",
      "schedule_file": "/var/lib/fanzone-eventspy/schedules/patriots.json"
    },
    "nfl_snapshot_dir": "/var/lib/patriotsfz-nfl/current",
    "recap_snapshot_dir": "/var/lib/patriotsfz-recaps/current",
    "division": "AFC East"
  }
}
```

The example's Pacific timezone follows the shared scheduling policy. It is separate from the official personnel source timezone and each game's venue timezone. Patriots personnel parsing uses `America/New_York`; the Munich fixture uses `Europe/Berlin`. Preserve those distinctions.

## 4. Review and implement every source integration point

### 4.1 Template repository

| File or area | Work for the next team |
| --- | --- |
| `config/active-sites.json` | Add the disabled team object while preserving existing entries and paths. |
| `template-tools/build-team.py` | Add the slug to the current explicit CLI choices. The shell wrapper alone does not make a new slug supported. |
| `template-tools/render.mjs` | Add the abbreviation/division identity where currently enumerated; retain literal opponent names and factual data. |
| `template-tools/themes.mjs` | Add the palette, stylesheet, favicon, brand/hero marks and fan tagline. |
| `public/styles/themes/<team>.css` | Author a readable team style using the existing theme system. Keep status/result colors meaningful. |
| `public/favicons/<team>.svg` | Add the fan-site icon and verify the rendered layout links to it. |
| `src/data/history/<team>.json` | Write sourced franchise history, eras, milestones, notable figures, numbers, metadata and banner copy. |
| `config/eventspy/<team>.json` | Add the website's matching copy of reviewed EventSpy coverage. |
| Shared editorial copy | Inspect homepage/history banner, about page, game editorial, taxonomy, newsroom/source links, city and division copy for incorrect inherited Seattle content. |
| Template tests and operator docs | Extend the existing theme, history, renderer and build-helper checks and team build examples. |

Patriots uses a navy/red/silver presentation with `PFZ` and `NE` marks. Its history is authored as Patriots history; Seattle historical facts are never rewritten by changing the team name. The shared history promotional banner also uses selected-team content.

Only the selected history, favicon and theme should enter the published build. History JSON stays under `src/data/history`, outside `public`. Inspect the generated `/history`, metadata and sitemap so a Seattle build does not expose a Packers or Patriots history page. Real opponents and legitimate historical references remain valid content.

### 4.2 Airflow repository

| File or area | Work for the next team |
| --- | --- |
| `config/active-sites.json` | Matching disabled source entry with verified identity, paths, source domains and prompts. |
| `deployment/roster/teams.json` | Add the official team domain, full name, abbreviation and local timezone used by the shared roster/injury/transaction collector. |
| `deployment/eventspy/coverage/<team>.json` | Reviewed EventSpy matchup coverage matching the website copy. |
| Shared host preparation | Prepare an additive, reviewed identity/path registration for the new team. |
| Existing DAG/config/collector tests | Cover the new identity, distinct tasks, path derivation, official personnel sources and EventSpy binding without changing existing team behavior. |
| Operator documentation | Record configured paths, activation state, coverage limitations and verified live results. |

The existing `deployment/shared/prepare_patriots.py` and `fanzone-patriots-activation` scripts are **Patriots-specific**. They are examples of the completed rollout, not universal installers. The next team's helper must be adapted and reviewed against the current registry, source and container. There is no supported generic `--team <new-team>` activation command in these scripts.

### 4.3 Shared DAGs and content responsibilities

| Existing DAG | Named work task pattern | What it publishes |
| --- | --- | --- |
| `sfz_nfl_refresh` | `refresh_nfl_snapshot_<team>` | NFL schedule, player statistics, standings and supporting NFL data. |
| `sfz_roster_refresh` | `refresh_roster_<team>` | Current official membership, injury observations and transactions. |
| `sfz_daily_article` | `generate_article_<team>` | Daily editorial news articles and their selected photo metadata. |
| `sfz_game_recaps` | `generate_recaps_<team>` | Final-game recaps based on the accepted NFL input. |
| `sfz_eventspy_collect` | `collect_ticket_prices_<team>` | Reviewed ticket feeds and existing history/publication artifacts. |
| `sfz_game_guides` | `refresh_guides_<team>` | Researched Game Day Guide and Where to Watch snapshots. |

Each DAG also has `save_run_receipt_<team>`. These are distinct named tasks generated for enabled entries, so the team's identity appears in the graph. Six enabled teams yield twelve tasks per fully expanded DAG. No new per-team DAG, Airflow service, connection or pool was needed for Patriots.

Daily articles and final-game recaps remain different pipelines and collections. Player statistics remain in NFL refresh; current roster membership, injuries and transactions remain in roster refresh. Guide build integration is separately opt-in.

## 5. Review EventSpy coverage before enabling collection

1. Verify the season's real regular-season matchups, designated home/away teams, venue and dates. Exclude preseason and the bye. Do not assume a neutral-site game is a home game because a team page says “VS.”
2. Identify genuine EventSpy event pages. Verify both teams, venue-local date and event ID. Exclude parking, tours, packages and unrelated dates.
3. Reuse an existing reviewed page when the new team plays an already-supported team and the event identity is identical.
4. Keep unknown pages explicitly `SOURCE_PAGE_NOT_AVAILABLE`. That means no reviewed URL is configured; it does not prove the provider has no listing.
5. Keep the Airflow and website coverage files synchronized. Bind real NFL game IDs through the accepted NFL snapshot; do not invent IDs or ticket prices.
6. Test repeated division opponents, neutral venues, date changes, abbreviation aliases and completed-game skipping. Already-started or completed games must be skipped before provider visits.

At the September 13 Patriots review, all 17 regular-season games were represented: **four reviewed URLs in total, including the completed Seattle opener; three future reviewed games; thirteen unavailable entries**. The Munich game is New England at Detroit with Detroit designated home. Weeks 17 and 18 were date/time TBD. These are dated coverage findings, not a promise of full ticket coverage or a permanent schedule.

EventSpy keeps its normal eligible scheduled slots and slot/history rules. Manual and backfill runs intentionally skip collection. Adding a team, creating a mount or building a website cannot create live ticket prices. Validate the next eligible scheduled collection; do not clear a stored slot to force provider recollection.

## 6. Preserve local work and commit the reviewed source

Begin with read-only repository inspection:

```bash
git -C /home/laurawkr/homelab-airflow status --short --branch
git -C /home/laurawkr/homelab-airflow branch -vv
git -C /home/laurawkr/templatefanzone status --short --branch
git -C /home/laurawkr/templatefanzone branch -vv
git -C /home/laurawkr/templatefanzone worktree list
```

Inspect file differences and staged changes before applying or committing. Preserve unrelated working files, file modes, editor settings and existing generated data. Stage an explicit reviewed file list, not `git add .`. Do not commit credentials, runtime outputs, photo buckets or backup exports.

The host runners have clean-source and branch checks. For example, roster verifies its collector, team registry, runner and entrypoint are committed. Applying a package without committing the changed roster registry can therefore pass a source-copy check but fail live preflight.

**Patriots branch lesson:** the template checkout was on `codex/fix-eventspy-missing-links`, exactly two commits ahead of `main` (`main...HEAD` was `0 2`). The existing EventSpy/icon commits had to remain. Local `main` was advanced to the same commit, then the checkout switched to `main` while retaining the uncommitted Patriots files. This worked because ancestry and worktree occupancy were verified first. Do not replay that branch operation for a future rollout without inspecting its new graph and local work.

The activation created local commits:

| Repository | Observed local commit |
| --- | --- |
| `homelab-airflow` | `cbd5c190d8955fc82875519639f76b4292d745f3` — Add Patriots to shared Fan Zone preview pipelines |
| `template-fan-zone` | `f4b30abb39d773ff9cfd8e7defe3a7246c1a020d` — Add Patriots to shared Fan Zone preview pipelines |

The activation script did not push. At the activation checkpoint, remote publication was not established. The current documentation/commit follow-up should record any subsequently verified pushes separately.

Before publishing, fetch the remote and compare its history with the reviewed local commits. The template remote includes additional merged PR history: local and remote parent trees can be identical while their commits differ. The successful local branch advance therefore does **not** establish that a direct push will fast-forward.

Run the following only after the reviewed source is committed, the checkout is on `main`, and `git status` confirms no unfinished merge or uncommitted tracked changes. Keep unrelated untracked files in place. Review the comparison output before the merge step; an ordinary merge retains both histories and will stop rather than overwrite conflicting untracked files.

```bash
cd /home/laurawkr/homelab-airflow &&
git fetch origin &&
git log --oneline --left-right main...origin/main

# After reviewing the fetched history:
git merge --no-edit origin/main &&
git push origin main

cd /home/laurawkr/templatefanzone &&
git fetch origin &&
git log --oneline --left-right main...origin/main

# After reviewing the fetched history:
git merge --no-edit origin/main &&
git push origin main
```

The merge may fast-forward or create a merge commit, depending on the actual history. If it reports conflicts, stop and resolve the reviewed differences before committing and pushing; do not rerun the activation to resolve a Git conflict. If branch protection requires a PR, use the existing review workflow. Do not force-push, reset away local work, or delete unrelated untracked files to make the push succeed. Verify the push result before recording remote publication as complete.

## 7. Prepare the host, preview and live configuration in order

### 7.1 Export and stage the active-site change

Export **only** the effective `fan_zone_active_sites` value into a new timestamped backup. If the Variable is absent, record that fact and the fallback separately. Preserve all existing team values, prompts and enabled flags when preparing disabled and enabled candidates.

Use the deployed Airflow server context for direct model Variable access. The established helper uses the existing `airflow-scheduler` service, `PYTHONPATH=/opt/airflow/dags`, `_AIRFLOW_PROCESS_CONTEXT=server` and `PYTHONDONTWRITEBYTECODE=1`. Parse the helper's explicit export marker rather than assuming all CLI output is raw JSON.

Before writing the live value, re-read it and compare with the saved baseline. If someone changed a prompt or another site while checks ran, restage the additive change from that fresh value. Do not replace the live Variable with the stock checked-in JSON or import the candidate as an all-Variables export.

### 7.2 Register only the new team's host paths

Patriots preparation reused `deployment/shared/prepare_patriots.py` to check and add its root-owned identity/path registration, prepare its own empty output/photo directories and install its reviewed coverage under `/opt/fanzone-eventspy/coverage/`. The previous registry was backed up first. Existing team registrations and outputs were retained.

This step must not manufacture `current` links, replace live snapshots, change a collector format, reset a slot ledger, reinstall shared SSH credentials or rerun the historical EventSpy scheduler handoff. Existing EventSpy settings hold shared image/activation/coverage configuration; adding a team does not require replacing the shared settings.

### 7.3 Run the actual checks

Import all six DAGs with the proposed enabled team in the deployed Airflow image. Confirm the new named work/receipt tasks appear. Then perform source-free SSH `check` requests for `sfz_nfl_host`, `sfz_news_host` and `sfz_roster_host` using the staged site object.

These checks verify real paths, source revisions, credentials/connections and runtime dependencies without provider collection. Recap and guide checks need the first accepted NFL snapshot and are completed afterward. Guide preflight also checks freshness under its existing 72-hour input policy.

### 7.4 Extend the actual preview container

The real preview was a standalone nginx container, not a Compose-managed service. Its labels contained only the nginx maintainer. Inspect `docker inspect templatefanzone-web` and `docker exec templatefanzone-web nginx -T`; do not assume a Compose filename or image tag describes the running service.

Add the new read-only host feed mount and its nginx location using the same settings as the other teams. The Patriots location installed follows the existing pattern:

```nginx
location ^~ /data/eventspy-mirror/patriots/ {
    alias /srv/fanzone-eventspy/patriots/;
    autoindex off;
    default_type application/json;
    types { application/json json; }
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Robots-Tag "noindex" always;
}
```

Docker requires replacing the container to add a bind. Preserve the running immutable image ID, existing mounts and their options, ports, environment, restart/security/logging settings and homepage. Validate the candidate nginx configuration before stopping the old preview, retain the old container for rollback, and verify the unchanged served homepage after the switch. A later normal team build updates `dist` through the parent mount without a container restart.

**Patriots checker lesson:** Docker returned mounts in a different order during reinspection. The original helper treated the ordering difference as a changed preview and correctly stopped before switching, but the comparison was too strict. The repaired helper compares mounts independent of list order while continuing to detect meaningful configuration changes. Keep that repair when adapting the workflow.

### 7.5 Enable the new runtime entry

After host, actual DAG/SSH and preview checks succeed, add or enable only the new team's entry in `fan_zone_active_sites`, preserving all other live site values. Verify the readback. Preserve every existing DAG pause state, connection and pool.

Enabling the entry allows tasks to run on already-unpaused DAGs' ordinary schedules. It does not guarantee that every pipeline has its prerequisite input immediately. Seed NFL next; guide or recap work that arrives earlier may fail for missing input and needs a later valid NFL publication.

## 8. First data publication and selected-team build

For Patriots, the supported continuation after successful activation is:

1. Wait for `refresh_nfl_snapshot_patriots` and `save_run_receipt_patriots` to appear in `sfz_nfl_refresh`.
2. Trigger the existing DAG or wait for its scheduled run.
3. Wait for both Patriots tasks to succeed. A normal manual run selects **all enabled teams**; this DAG has no team-only run-conf filter.
4. Run the existing post-NFL step and build:

```bash
cd /home/laurawkr &&
python3 fanzone-patriots-activation/fanzone-patriots-activate.py --after-nfl &&
cd /home/laurawkr/templatefanzone &&
bash template-tools/build-team.sh patriots
```

`--after-nfl` performs the dependent source-free checks, derives the EventSpy schedule cache from the fresh accepted NFL snapshot using the existing validator/atomic writer, and runs the build helper dry run. It does not generate guides, articles or recaps or make a new provider schedule request.

A cautious first visual check can use the existing build modes:

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh patriots --dry-run
bash template-tools/build-team.sh patriots --stage-only
bash template-tools/build-team.sh patriots
```

Dry run validates required paths and prints the command. Stage-only builds without replacing served `dist`. The normal successful build replaces `dist`, changing the one shared preview. Failed input validation or build retains the previous served output.

**Optional guides:** the build helper first reads an exported `FAN_ZONE_GUIDES_ENABLED` value, then this checkout's `.env` only if no exported value exists; the default is off. Accepted flag values are `0` and `1`. An exported `0` is forwarded into the Docker build and overrides a checkout `.env` value of `1`.

If `FAN_ZONE_GUIDES_ENABLED=1`, obtain a successful Patriots guide work/receipt run after NFL before the build. Guide preflight does not create guide content. For an initial preview intentionally excluding guides, explicitly set `FAN_ZONE_GUIDES_ENABLED=0` on both the post-NFL command and the build command. Preserve the checkout's existing preference rather than silently editing its environment.

The first valid NFL snapshot is mandatory for a new team. Daily news, roster and recap sections can remain empty before their first publications; a new team must not inherit Seattle data as a fallback. Once those pipelines publish, rebuild to import them. Existing malformed, wrong-team or broken snapshots fail validation rather than being silently ignored.

## 9. News photos, credits and final acceptance

Add appropriate licensed photos to the new team's own `-news/photos/` bucket. Match those assets to the existing approved photo-credit catalog/Variable so photographer, source and original event details render correctly. Preserve editorial source citations separately from photo credits. An empty Patriots bucket was observed during activation and uses the existing neutral illustration fallback; it does not borrow Seahawks photos.

Check the following before marking a team fully tested:

| Area | Expected result |
| --- | --- |
| Header, homepage and browser tab | Correct city/region, team, colors, brand marks, title and favicon. |
| History and promotion banner | Actual selected-franchise history, fan traditions and citations; no inherited Seattle story. |
| Schedule and standings | Correct team ID, opponent identities, division, venue and kickoff data. |
| Players and roster | Player statistics from NFL; current membership/injury/transaction observations from roster. |
| Daily news | Correct team-tagged daily article collection, selected team photos and verified credits. |
| Game recaps | Correct completed games and perspective; separate from daily news. |
| Guides/watch pages | Accepted selected-team guide data when enabled. |
| Tickets | Browser requests use the selected feed; completed games are skipped by collection; missing reviewed URLs remain explicit. |
| Publication isolation | Selected-team history/assets only in generated output; no alternate hidden team history or sitemap routes. |
| Preview continuity | Existing preview port, noindex policy and other team mounts retained; another supported team still builds with the same helper. |

Record actual task run IDs/receipts, snapshot timestamps, source commits, build result and preview inspection. Unit tests and preflight messages are evidence of checks, not evidence of live content publication.

## 10. Observed Patriots checkpoint and recovery records

The supplied September 13, 2026 **04:43 UTC** activation output establishes:

- All six DAG imports and initial NFL/news/roster SSH checks passed.
- Patriots host paths and identity registration were prepared; existing values were preserved.
- Candidate nginx syntax passed with the running image and all six read-only feeds.
- The Patriots preview mount and alias were installed; the existing homepage was verified unchanged.
- Patriots was enabled in `fan_zone_active_sites`; other live site values were preserved.
- No DAG run was triggered by the helper and no DAG pause state was changed.

| Record | Observed location |
| --- | --- |
| Original active-site export/plan from the successful source/host preparation attempt | `/home/laurawkr/patriots-activation-20260913T043415007382Z` |
| Root-owned registry backup | `/opt/fanzone-shared/backups/patriots-onboarding-20260913T043420546467Z` |
| Retained stopped rollback preview container | `templatefanzone-web-before-patriots-20260913T044336197460` |
| Active preview nginx configuration and recovery receipt directory | `/home/laurawkr/templatefanzone/.sites-runtime/patriots-preview-20260913T044335-02287ee0` |

**Keep the active configuration directory.** The running container binds a file from it. Its location inside the checkout does not make it disposable build scratch. Keep the previous container and recovery receipt until the rollout is accepted; the receipt records the container identities and restart-policy recovery information.

At this documentation checkpoint, the conversation has not supplied a successful first Patriots NFL publication, post-NFL completion, guide publication, final Patriots build or scheduled ticket collection. Those remain verification items, not reported failures. Record later results when observed. The successful activation is not a public Patriots deployment, and Seattle production was not redeployed by this onboarding.

If a step stops, retain completed commits, matching prepared directories and the printed receipts. Diagnose the specific conflict and resume the reviewed helper; do not reset Git, erase snapshots or rerun broad legacy installers to get past it. If a helper reports uncertain container recovery, inspect its receipt and actual Docker state before attempting another replacement.

## 11. Source references and maintenance

Read the current source before reusing this runbook; the Patriots installation packages are dated snapshots of the process.

- [Template repository](https://github.com/caferacerstudios/template-fan-zone): `config/active-sites.json`; `template-tools/build-team.py`; `template-tools/TEAM-BUILDS.md`; `template-tools/render.mjs`; `template-tools/themes.mjs`; `src/data/history/patriots.json`; `config/eventspy/patriots.json`.
- [Airflow repository](https://github.com/caferacerstudios/homelab-airflow): `dags/fan_zone_config.py`; `dags/fan_zone_tasks.py`; six `sfz_*` DAGs; `deployment/shared/prepare_patriots.py`; `deployment/shared/fan_zone_host.py`; NFL/roster/news/recap/guide runners; `deployment/eventspy/schedules.py` and coverage.
- [Patriots source onboarding guide](https://github.com/caferacerstudios/homelab-airflow/blob/main/docs/patriots-onboarding.md) and [Patriots EventSpy review](https://github.com/caferacerstudios/homelab-airflow/blob/main/docs/eventspy-patriots-2026.md). Availability on remote `main` depends on completing the host pushes; the observed local commits are recorded in section 6.
- Local completed activation package: `/home/laurawkr/fanzone-patriots-activation/`; original reviewed source package: `/home/laurawkr/fanzone-patriots-update/`. Retain the repaired preview helper; re-extracting an older activation ZIP would restore its older helper.
- [Homelab Airflow Setup and Operations](https://caferacerstudios.atlassian.net/wiki/spaces/WA/pages/2686983/Homelab_Airflow_Setup_and_Operations+1), [Fan Zone Modular Software Design](https://caferacerstudios.atlassian.net/wiki/spaces/TFZ/pages/2523179/Fan_Zone_Modular_Website_Software_Design), and [Seahawks Production Deployment](https://caferacerstudios.atlassian.net/wiki/spaces/SE/pages/2457611/Seahawks+Fan+Zone+Production+Deployment).

For the next team, update this page's source checklist and maintain a new dated rollout record with that team's real paths, reviewed coverage limitations and observed completion state. Keep reusable instructions separate from one team's historical hashes and rollback names.
