# Game-day and where-to-watch research

This is an independent producer and an explicitly enabled website import. It gathers cited game-day logistics and viewing information without changing the NFL, roster, news, recap or ticket publications. The new DAG does not build or deploy a website.

Source added in this change is not evidence that the host installer has run, research has completed or production is using the output. Inspect those three stages separately.

## Flow and existing system boundaries

`sfz_game_guides` creates a named `refresh_guides_<slug>` task followed by `save_run_receipt_<slug>` for every enabled team in `fan_zone_active_sites`, using the same `active_sites()` helper as the other team DAGs. The Variable is the team-selection authority; `config/active-sites.json` is the shared fallback when it is absent. There is no separate guide activation list. `config/game-guides.json` controls the model and research workload only.

The refresh task uses the dedicated `sfz_guides_host` SSH connection. Its Ed25519 key is restricted to `deployment/guides/ssh_entrypoint.py`. The entrypoint accepts only a bounded encoded `check` or `refresh` request; a request cannot select an arbitrary shell command, runtime destination or credential path.

The host reads the existing shared team registration in `/opt/fanzone-shared/settings.json` and the selected team's validated NFL snapshot. It derives its independent runtime from the registered news path. Existing article source domains and prompts remain untouched. Research uses the existing OpenAI Responses helper and host credential; no additional search-provider account or Airflow provider is required.

| Team | Guide publication |
| --- | --- |
| Seahawks | `/var/lib/sfz-guides/current` |
| Broncos | `/var/lib/boncosfz-guides/current` |
| Packers | `/var/lib/packersfz-guides/current` |
| Vikings | `/var/lib/vikingsfz-guides/current` |
| Chiefs | `/var/lib/chiefsfz-guides/current` |

Only enabled active teams are installed and scheduled. Denver's established `boncosfz` spelling is preserved. The runtime is separate from website checkouts, served releases and all other producer roots.

## Schedule and workload

| Setting | Value |
| --- | --- |
| DAG | `sfz_game_guides` |
| Timetable | Daily at 04:45 `America/Los_Angeles` |
| Initial state | Paused |
| Catchup / retries | Disabled / zero |
| Active runs / tasks | One / one |
| Work task / SSH timeout | 35 minutes / 33 minutes |
| Receipt task timeout | Two minutes |
| Pool | Existing `default_pool` |
| Team selection | All enabled teams in `fan_zone_active_sites` |

Global Airflow parallelism still applies. This DAG may queue behind other work; its clock time is not a guaranteed completion time. It does not change other DAGs' schedules, pools or pause states.

The independent `config/game-guides.json` controls research horizon, refresh interval, per-run game budget and model. The policy looks ahead 14 days and limits each team run to three games. With five active teams, a run can research up to 15 games; work remains serialized with one active task. Near-term games receive priority; missing later regular-season games can be filled within the same bounded budget. Completed games and existing historical guide content are retained. A single run is not represented as researching every remaining game.

Only official schedule identities select games. Research must distinguish dated event announcements from general venue guidance. Sounder, event times, closures, watch parties, broadcast availability and regional streaming restrictions require appropriate evidence. Unconfirmed items remain unknown or absent. Source URLs and timestamps accompany accepted facts; retrieval time is not an announcement date.

## Install the new host handoff

First review and merge the Airflow and website changes through the normal repository workflow. On `wkr`, update the clean Airflow checkout to the reviewed revision without overwriting local edits. Review the shared active-site Variable and the research limits in `config/game-guides.json`.

Run as `laurawkr`, without running the whole installer under `sudo`:

```bash
cd /home/laurawkr/homelab-airflow
python3 -B deployment/guides/install.py
```

The installer checks every enabled active team's registration, source and input prerequisites before preparing any new guide runtime. It generates or reuses only `secrets/sfz_guides_ed25519`, appends its restricted public-key entry when absent and configures only `sfz_guides_host`. It verifies the actual Airflow DAG graph and performs a source-free SSH check for each enabled active team.

The installer neither calls the research provider nor publishes a snapshot. It does not unpause or trigger any DAG. It does not change Docker Compose, nginx, Cloudflare, existing provider settings, other SSH connections, the active-site Variable or existing snapshots.

For a direct source-free host check:

```bash
cd /home/laurawkr/homelab-airflow
python3 - <<'PY'
import json
import subprocess
from pathlib import Path
site = json.loads(Path('config/active-sites.json').read_text())['seahawks']
subprocess.run([
    'python3', '-B', 'deployment/guides/refresh_guides.py',
    '--check', '--site-json=' + json.dumps(site),
], check=True)
PY
```

This example reads the checked-in fallback. Use the live Variable's selected site when its values differ. The installer itself reads the current Variable with fallback and preserves it.

## First research publication and website preview

After installation, unpause `sfz_game_guides`, trigger one manual run in the Airflow UI and inspect each team's refresh and receipt tasks. The five configured enabled teams produce ten tasks. Airflow also requires an unpaused DAG to execute manual tasks. Check for an already queued or running scheduled run before triggering another. If preview review is still pending, pause the DAG after the run finishes. This run makes paid OpenAI requests; the source-free check above does not.

The completed snapshot contains:

| File | Contract |
| --- | --- |
| `game-day-guides.json` | Team/season-tagged guide collection keyed by real regular-season game ID |
| `watch-guide.json` | Team/season-tagged viewing information for those games |
| `manifest.json` | Schema, pipeline, team, season, run ID, timestamp and SHA-256 for both files |

The hook requires a single `SFZ_GUIDES_RECEIPT=` record with the selected team/run, pipeline `guides`, supported season, exact file list and a bounded OpenAI request count. Receipts are saved under `/var/lib/homelab-pipelines/<slug>/guides/<run-hash>/receipt.json` on the host. Successful Airflow execution and snapshot publication do not mean the public site was rebuilt.

In `template-fan-zone`, the importer is disabled by default. Enable `FAN_ZONE_GUIDES_ENABLED=1` only for the selected preview/build using the existing team-build workflow. It reads the separate guide root, verifies manifest checksums and matches records to the selected team's actual NFL schedule before copying data into the isolated build workspace. The importer has no provider calls and no production deployment action.

Review the Game Day Guide and Where to Watch sections on `/games/<gameId>` in preview. Confirm source links, actual opponent/date/home-away matches, unknown fields and retained manually authored records. Only then include the setting in an explicitly chosen production build using the existing reviewed deployment process. The new DAG does not run that process.

## Failure handling and recovery

Failed research or validation retains the previous good guide publication. Do not replace existing JSON with an empty fallback after a provider error. Preserve failed run evidence and read its log before retrying; a provider timeout does not prove the request was unbilled. Completed-run reuse and cached responses reduce duplicate requests but are not an external billing guarantee.

### Detailed reporting and writing prompts (September 13, 2026)

The Seattle run at `2026-09-13T19:50:59Z` failed the existing minimum-content
check after research and writing. Its log does not identify which required
component was missing. The first Seattle guide, game `1392216` in
[the original website collection](https://github.com/caferacerstudios/seahawks-fan-zone/blob/main/src/data/nfl/game-day-guides.json),
provides the editorial benchmark: roughly 1,100 words with distinct transport
choices, parking details, entry preparation, a useful arrival timeline and named
watch-party options. Its event-specific facts are not reusable for another game.

The research and writing prompts in `deployment/guides/guides_core.py` now ask
for a detailed reporting brief and practical local guide instead of a concise
brief and compact prose. Research establishes useful applicable venue, parking
and transport policies first, then investigates dated changes, named events and
viewing options. The writer targets 900–1,300 useful words and a 90–150-word
summary when supported. It preserves concrete routes/stations, return-trip
limitations, reservation/access requirements, bag rules, and documented party
locations and times. These are editorial targets, not quotas or new validators.

Sparse special-event announcements should leave event-only sections sparse;
they should not erase a supported summary and useful ordinary arrival/entry
guidance. Unconfirmed events cannot be relabeled as standing policies. Home and
away locations remain separate, duplicate timeline reminders are discouraged,
and no old game's facts may be copied to fill gaps. Citations, dates, schema,
minimum content, and publication checks are unchanged. The prompt cannot
guarantee a successful guide when the retrieved evidence is insufficient.

`PROMPT_VERSION` is now `fan-zone-guides-v2-detailed`. It changes the evidence
cache identity because cached requests require an exact payload match. Old
research, drafts and validation audits remain untouched. An eligible game with
an old failed draft receives new research and writing under the revised prompts;
it is not a zero-request revalidation of that draft. `WRITING_STAGE` remains
`guide-citations-v2` and validation remains `guide-official-link-v1`. The
historical zero-request recovery notes below describe those earlier fixes alone.

The model, six-call web-search budget, output limits and maximum of one research
plus one writing request per selected game remain unchanged. New prompts add
input text and can produce longer output, so unchanged request limits do not
mean unchanged token cost. A Seattle task can select up to three games (six
Responses requests). Fresh accepted guides and completed runs still reuse;
this change does not force a rewrite of accepted history or every team.

After review and merge, pause only `sfz_game_guides` and let any running guide
task finish before pulling the reviewed main revision into the normal clean
`/home/laurawkr/homelab-airflow` checkout. Clear only the failed Seattle refresh
and its downstream receipt for the affected run, then unpause the DAG. Leave
successful task pairs unchanged. The host uses this source checkout, so this
prompt update needs no installer, container restart or cache deletion.

Inspect the resulting receipt, `validation.json` and accepted guide for depth,
correct locations and source support. A code/test result does not establish
that a live generated guide is better. Review the resulting guide in an enabled
website preview before a separately chosen production build; changing the prompt
or running the DAG does not rebuild deployed static pages.

### Source-ID validation failure

The initial writer schema allowed empty or arbitrary `sourceIds`, while the
publication validator required one to four distinct IDs from the actual cited
source registry. A response could satisfy the provider schema and still fail
with `Every guide fact must cite known retrieved source IDs`. The old writer
response was then reused for the same event, policy and research day.

The writer now receives a schema restricted to the retrieved IDs, with one to
four IDs per fact. Missing information must use null or empty item lists.
Duplicate IDs, unsupported claims and incorrect event dates remain rejected.
Validation errors identify the affected field without logging research bodies.

This citation repair introduced the separate `guide-citations-v2` cache stage.
That repair alone left the research prompt and cache identity unchanged, so an
eligible cached research response was reused and only the corrected writing
step needed a new request. Original
`guide-request.json` and `guide-response.json` files are retained for inspection.
Fresh research still uses at most one research request and one writing request
per selected game; there is no automatic repair loop. A later research day or
changed event/policy can require new research as before.

After committing the fix in `/home/laurawkr/homelab-airflow`, clear the failed
`refresh_guides_seahawks` task and its downstream receipt task for the affected
run, or trigger one new run if none is active. Keep the DAG unpaused while the
tasks run. No installer rerun, container restart, cache deletion or website build
is needed for this correction. Review the first accepted snapshot before using
it in a website build.

### Unconfirmed event evidence in a draft

The writer can return a cited standing policy in an event-only section, such as
`alerts[1]`. A generic policy is not a confirmed alert for the selected game.
The collector now removes individually unconfirmed candidates before validating
the complete guide. It never changes their scope, copies in the game date, or
moves their prose into a different section to make them pass.

This applies to alerts, timelines, tailgates, watch parties, Sounder guidance and
broadcasts that lack required event evidence, and to candidates whose evidence
date does not match the canonical game date. A standing-policy candidate with a
non-null event date is also omitted rather than having that date erased.
Source IDs, URLs, prose and schema
must still be valid. Invalid or duplicate citations and malformed content fail
the run. Publication still requires a supported summary and at least two useful
accepted items citing at least two pages. Insufficient remaining content fails
the run and preserves the previous publication.

Each omission is logged by game and field/index, without logging the prose.
`evidence/<cache-key>/validation.json` records the reason and supplied evidence
metadata, including when the remaining-content check fails. An accepted draft's
`evidence.json` also includes `omittedFacts` and the validation version. The raw
research and writer request/response files are retained unchanged.

The research prompt, writer prompt, schema and cache stages were unchanged by this
validation-only correction. An eligible cached failed draft could be checked again with
zero new provider requests. Uncached games or a changed research day/event/policy
retain the normal request budget. No automatic model repair request is added.

After merging and pulling this fix into the normal Airflow checkout, clear the
failed guide refresh task with its downstream receipt task, keeping the DAG
unpaused while they execute. Read the omission warnings and the receipt. Do not
clear EventSpy tasks as part of this guide repair; that pipeline has separate
receipt semantics. This change does not install services, rebuild a website or
alter other pipelines' data.

### Optional official-game link outside the registered domains

The Broncos retry at `2026-09-13T00:39:57Z` reached writing/publication validation
and failed with `Official game link must belong to the registered team or NFL`.
The supplied log does not include the rejected URL. This is separate from the
earlier missing `/var/lib/boncosfz-guides` installation prerequisite.

The writer can nominate any retrieved source ID for the optional
`officialGameSourceId`. Publication accepts only `nfl.com` or the selected team's
registered `source_domains`, including their subdomains. A retrieved stadium or
opponent page can support useful guidance while failing that optional-link rule.
Validation now sets only a known, safe, but ineligible official-link candidate
to null and records its field, source ID and reason in `validation.json` and
accepted `evidence.json`. It preserves the original response and all supported
facts. It does not invent a replacement link or expand the allowed domains.

Unknown IDs, unsafe source URLs and malformed drafts still fail. A grounded
summary and at least two useful items from two pages are still required; an
omitted link cannot supply an otherwise missing second source. Direct record
creation and snapshot verification both enforce the same official-link domain
rule. Validation version is `guide-official-link-v1`.

The official-link repair alone left research/writer prompts, schemas,
`guide-citations-v2` and the research-policy cache identity unchanged. An eligible
failed cached draft could therefore be revalidated with zero new provider calls. Changed event, day or
policy inputs and other uncached games retain the normal request budget.

After merging, pause only `sfz_game_guides` and let running guide tasks finish.
Then update the normal checkout as `laurawkr`:

```bash
cd /home/laurawkr/homelab-airflow &&
git switch main &&
git pull --ff-only origin main
```

Clear the affected failed guide refresh task and its downstream receipt task in
that run, then unpause the DAG. Leave successful task pairs untouched. Inspect
any other team's error before assuming it has the same cause. This code-only
repair needs no installer rerun or website build. Review the accepted snapshot
and omission audit before a separately enabled website preview/publication.

### Upgrade from Seattle-only guide activation

The initial implementation added a second `enabled_sites: ["seahawks"]` filter.
That filter and its helper have been removed so team selection matches the other
DAGs. Before updating this deployed source, pause only `sfz_game_guides` and let
any running guide task finish. Update the normal Airflow checkout, then rerun
`python3 -B deployment/guides/install.py` as `laurawkr`, without a sudo prefix.
This prepares and checks all enabled teams' guide roots and reuses the dedicated
connection. The installer preserves the current pause state; it does not pause
an existing unpaused DAG for you.

After installation checks pass, unpause this DAG and start a fresh manual run.
An older run can still show its original Seattle-only DAG version. If a team
fails installation checks, correct its registered input or runtime prerequisite
before running the expanded DAG. A newly enabled active team also needs its
registered host setup and guide runtime prepared before collection.

Removing the obsolete policy field changes evidence-cache identities once.
Completed run snapshots and fresh accepted guides still reuse normally, but an
eligible failed draft from the old policy may need new research and writing.
The per-game limit remains one research request and one writing request, with no
automatic repair loop. Future team activation edits occur only in the shared
Variable and do not change this research policy.

Malformed research policy stops host collection. Registered identity and output
paths are still checked independently before any team's guide data is written.

If generated guides need to stop appearing, omit `FAN_ZONE_GUIDES_ENABLED=1` from the next reviewed build and use the existing deployment workflow. Pausing the DAG stops future scheduled research, but it does not rewrite already deployed static HTML. Retain snapshots and receipts for inspection rather than deleting a lock or recursively changing `/var/lib` permissions.

## Verification

Run the new tests with the deployed Airflow 3.3.1 / SSH-provider environment:

```bash
python3 -m pytest -q \
  tests/test_guides_config.py tests/test_guides_core.py tests/test_guides_dag.py \
  tests/test_guides_hook.py tests/test_guides_ssh.py tests/test_guides_install.py \
  tests/test_guides_citations.py tests/test_guides_event_evidence.py \
  tests/test_guides_official_links.py tests/test_guides_prompts.py
```

Producer tests cover payload identity, evidence validation, bounded selection and publication behavior separately. The orchestration tests exercise the actual paused Airflow graph and Pacific DST schedule, shared active-site selection, receipt isolation and restricted SSH command grammar without live source requests. Host installation and rendered preview remain deployment checks to perform on `wkr`.

The detailed-prompt update passed 68 guide tests and 149 subtests under Airflow
3.3.1. Three new tests exercise failed-draft cache migration, preservation of
fresh accepted guides, and home/away request identity, sources, schema and
request limits. These tests use mocked provider responses; they do not evaluate
live prose quality or demonstrate a successful Seattle retry.

The optional official-link repair passed 65 guide tests under Airflow 3.3.1.
Its nine regressions cover registered-domain boundaries, safe candidate omission,
unknown/unsafe hard failures, unchanged cached request/response bytes with zero
new calls, minimum-content preservation and snapshot domain verification. These
are local mocked-provider tests, not a successful live Broncos retry receipt.

The shared-active-team correction passed all 56 guide tests under Airflow 3.3.1,
including the five-team/ten-task graph, disabled-team behavior, existing citation
and event-evidence checks, and installer preflight failure preserving all state.

Local validation on September 12, 2026 passed 32 tests and 38 subtests under Airflow 3.3.1 with SSH provider 6.0.1. A separate integration check used mocked research responses with the real producer, verified NFL fixture files, immutable publication, same-run reuse, the website importer and a full 157-page Astro build. The generated game-page HTML contained the guide and viewing information. This was a fixture test, not a live research run or host deployment.
