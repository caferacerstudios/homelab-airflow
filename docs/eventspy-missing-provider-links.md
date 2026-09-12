# EventSpy games with an unavailable marketplace link

The September 12 Chiefs collection failed on game `1392253` / EventSpy `374562`
because EventSpy supplied three marketplace links and an empty SeatGeek link.
The collector previously required all four links. It now represents genuinely
missing links as `null`; a nonempty malformed/unsafe link still fails validation.
Prices, price history and the 13 previously successful publications remain intact.

Complete four-link snapshots retain schema `1.0.0`. Missing-link snapshots use
`1.1.0`. Deploy the matching template consumer support to every checkout whose
build imports these ticket snapshots before installing the collector update.
Use the scoped website patch for `/home/laurawkr/seahawksfanzone` so this repair
does not pull unrelated production changes. **The website must be rebuilt and its
updated browser assets published before the collector update.** The game page
also validates freshly fetched snapshots in client-side code, so changing the
source files alone does not update the already served validator. Stage and review
the website build, then use the existing reviewed publish process after confirming
the nginx container mounts the checkout parent. This repair does not invoke that
publish process or recreate the nginx container automatically.

After the Airflow fix is in the normal `/home/laurawkr/homelab-airflow` checkout:

```bash
cd /home/laurawkr/homelab-airflow &&
sudo python3 -B deployment/eventspy/update_collector.py --check &&
sudo python3 -B deployment/eventspy/update_collector.py --install
```

This updater changes only `/opt/fanzone-eventspy/collector.mjs`. It takes the
existing installer and bridge locks without waiting, and refuses to change an
active/unresolved collection or an unrecognized locally modified collector.
Let a busy collection finish, then rerun. Queued requests remain queued while the
lock is held and continue normally afterward.

Both commands validate the actual saved `2026-09-12T16:00:00.000Z` response for
event `374562` using the installed Chiefs schedule and coverage. Docker uses the
already installed image, no network, no credential environment and only read-only
mounts. The check binds the genuine game ID and runs the real normalizer without
collecting or publishing. Output contains metadata only. Keep this cached response
until this focused repair is installed; a missing or no-longer-matching input
stops the update.

Installation preserves the previous collector in a root-owned backup under
`/var/lib/fanzone-eventspy/install-backups/` and prints its exact filename. The new
file is installed atomically with the previous ownership/mode. The updater does
not run the full installer, restart containers, refresh schedules, change timer or
watcher configuration, edit the ledger, or clear any receipt/cache/publication.

Wait for the next eligible scheduled `sfz_eventspy_collect` slot and check the
Chiefs collection/receipt tasks. Manual/backfill runs are intentionally skipped;
clearing the historical failed task returns its existing cached receipt. The old
failed run remains an accurate record of what happened.

For rollback, restore the printed collector backup using the same idle bridge and
installer locks and an atomic same-directory replacement. Do not use the full
EventSpy installer's `--rollback`: that transfers scheduling ownership back to the
legacy timer, which is unrelated to this collector-only repair.
