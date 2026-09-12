# Recorded Seattle schedule identity

`seattle-2026-recorded-identity.json` records the identity fields supplied from
`/var/lib/sfz-nfl/current/seahawks.json` on September 12, 2026. Its 18 rows
include the normalizer’s Week 11 bye. Week 3, game `1392256`, uses `WSH`
for Washington; the reviewed EventSpy coverage uses `WAS`.

The game IDs and teams come from that diagnostic output, independently of
the coverage table. The diagnostic did not include kickoff dates or scores.
`fixture: false` and the Seattle team identity are test-envelope fields
required by the production binder; this file is only an offline identity
regression fixture and must never be published as a live schedule.
