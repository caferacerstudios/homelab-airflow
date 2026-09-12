# Five-team Fan Zone update

This update extends the existing shared daily-news and EventSpy configuration to
the NFL refresh and game-recap pipelines. The active sites are Seahawks, Broncos,
Packers, Vikings, and Chiefs. The `fan_zone_active_sites` Airflow Variable remains
the configuration used to create separately named tasks for each enabled team.

## Version 2 compatibility fixes

This version recognizes the nginx preview created from its pinned image ID and
validates nginx before changing mounts. It also recognizes the exact reviewed
September 11 weekly recap writer and the host runner that stages that local writer.
It preserves the weekly prompt, 600–900 words across 7–10 paragraphs, 6,000-token
output limit, structured player links, and Tuesday schedule. Unknown edits still
stop installation. Touched source and staged files are backed up before updating.

## Install

Save the ZIP in `/home/laurawkr`, verify the checksum supplied with the download,
and extract it with Python's built-in ZIP support. Run `install.py` as `laurawkr`,
not through `sudo`; it requests sudo for the host steps itself.

```bash
cd /home/laurawkr
python3 -m zipfile -e fanzone-teams-update-v2.zip .
python3 fanzone-teams-update-v2/install.py
```

The Git roots must be `/home/laurawkr/homelab-airflow` and
`/home/laurawkr/templatefanzone`, both on `main` with their existing GitHub origins.
The installer commits and pushes only the packaged update files. It preserves
unrelated staged, modified, and untracked files. If an update file contains an
unexpected local change, installation stops before overwriting it.

The installer merges missing fields into the current Variable and adds the new
teams. Existing enable/disable flags, prompts, paths, and configured IDs win.
It checks team IDs against the balldontlie team directory and saves the verified
IDs in both repositories and the Variable. No IDs are guessed. A conflicting
configured ID stops installation.

The four existing DAGs must have no running or queued runs. Their original pause
states are saved, and all four are paused during the source/host handoff. The
exact merged DAG configuration is checked using your installed Airflow image
before source files are written. Original pause states are restored after a
successful handoff.

The known local recap-only change from the older schedule to Tuesday at 06:45
Pacific is accepted, including its explanatory docstring. Unexpected code edits
in that file still stop installation.

The installer performs one balldontlie team-directory lookup and refreshes
EventSpy schedule metadata. It does not generate articles, write recaps, collect
ticket prices, or build a website.

## Initialize the new NFL feeds

After installation, open Airflow and trigger `sfz_nfl_refresh` once. Wait for the
five refresh tasks and receipt tasks to succeed before building each new team.

Each NFL task is named `refresh_nfl_snapshot_<team>`; each recap task is named
`generate_recaps_<team>`. Their visible titles include the city and team name.
The daily article and EventSpy DAGs use the same enabled-site configuration.

NFL refresh keeps its existing ten daily slots. Recaps run Tuesday at 06:45
Pacific. Daily articles retain their existing separate schedule and outputs;
recaps are never substituted for daily articles. EventSpy keeps its existing
collection slots and skips games that have already started or finished according
to the validated schedule. Events without a verified ticket listing are reported
as unavailable rather than assigned another game's URL.

## Build each team from the template

Run the command for the team you want to view:

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh seahawks
```

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh broncos
```

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh packers
```

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh vikings
```

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh chiefs
```

The helper reads `config/active-sites.json`, mounts the selected team's data
directories read-only, and builds with that team's `TEAM` value. Its matching
history and visual theme are selected at build time. Other teams' history pages
are not added to the published site.

Each successful build replaces this template checkout's `dist` directory, so
these commands switch the same preview at `http://192.168.88.3:4326/`. Run them
one at a time. The original production checkout is separate.

For a command preview without building:

```bash
python3 template-tools/build-team.py packers --dry-run
```

The preview nginx configuration receives the five ticket-feed mounts. Ticket
JSON routes include `X-Robots-Tag: noindex`; they are not alternate history pages.
The previous preview container is retained when its mount configuration changes.

## Existing Seattle outputs

| Pipeline | Existing location |
| --- | --- |
| Daily articles | `/var/lib/sfz-news/current` |
| NFL snapshots | `/var/lib/sfz-nfl/current` |
| Game recaps | `/var/lib/sfz-recaps/current` |
| Ticket data | `/var/lib/sfz-eventspy-mirror/dev/public` |
| Daily-article photos | `/var/lib/sfz-news/photos` |

Denver daily articles keep `/var/lib/boncosfz-news/current`, including the
existing `boncos` spelling. Team output locations and photo buckets are recorded
in the active-sites JSON. Existing current snapshots and ticket history remain
in place.

## If installation stops

Read the reported error and rerun the same installer after addressing it. A
completed local update commit is reused if a Git push failed.

If source or host changes already began, the four DAGs remain paused until the
handoff completes. Do not manually unpause them while installation is incomplete.
`teams-update-resume.json` beside `install.py` stores the original pause states,
Variable, touched-file contents, staged versions, and completed commit IDs. It is
written with mode `0600`. Keep it in place for the retry: the installer uses it to
restore the original states rather than treating the temporary paused state as
your preferred configuration.

No reset, force push, stash, or automatic service rollback is performed. The
receipt and existing host backups preserve the information needed to review a
failed handoff before deciding on a rollback.
