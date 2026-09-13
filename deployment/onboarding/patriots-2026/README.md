# Reviewed Patriots activation tools — September 2026

These are the reviewed tools used to add Patriots to the existing shared preview.
They are preserved alongside their offline regression tests so the implementation
and the corrected Docker mount comparison can be reviewed and reused deliberately.

For the repeatable add-team procedure, use [Adding a team](../../../docs/add-team.md)
and the published
[Confluence onboarding runbook](https://caferacerstudios.atlassian.net/wiki/spaces/TFZ/pages/3080195/Adding+a+Team+to+Fan+Zone+Onboarding+Runbook).

The activation script remains Patriots-specific. It expects the original reviewed
source package at `/home/laurawkr/fanzone-patriots-update`, the existing Airflow
and template checkouts, and the original source manifest. Keep the already
installed `/home/laurawkr/fanzone-patriots-activation` folder for the pending
`--after-nfl` step. Do not blindly run these historical helpers for a different
team or after unrelated source changes.

The corrected preview helper compares Docker mount lists without relying on
their order, while still rejecting real configuration changes. Its SHA256 is
`43f714d02badf5d9b80e38edddc2cf89b3bafe3867fee6eea3d2a9ed8ca8b309`.
The scripts themselves are copied unchanged from their reviewed versions;
`test_activation.py` only adjusts its repository-root lookup for this directory.

From the repository root, run the offline tests:

```bash
python3 -B -m unittest discover -s deployment/onboarding/patriots-2026 -p 'test_*.py'
```

The tests use fixtures and fake Docker/Variable operations. They do not contact
Airflow, Docker, GitHub, SSH hosts, or data providers. Integration tests on `wkr`
already confirmed six DAG imports, source-free NFL/news/roster SSH checks,
Patriots host registration, the existing preview's sixth ticket mount/alias,
unchanged homepage during the preview replacement, and activation of Patriots
without changing the other live team values. Successful first Patriots data
publication and final build are separate checks, not established by those logs.

Preserve the actual runtime configuration directory while nginx mounts it:
`/home/laurawkr/templatefanzone/.sites-runtime/patriots-preview-20260913T044335-02287ee0`.
The retained rollback container is
`templatefanzone-web-before-patriots-20260913T044336197460`.
Those runtime files and receipts are not copied into this source archive.
