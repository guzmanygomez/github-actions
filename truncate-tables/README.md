# truncate-tables (scaffold)

Composite action that truncates an explicit, reviewed allowlist of database
tables (MySQL or Postgres), as part of a production data retention/purge
policy. Built under BHPL-3313.

**This is a scaffold.** It is safe to merge as-is (default `dry_run: true`,
example config only), but it must not be wired up against a real production
database until every item in [Before enabling against prod](#before-enabling-against-prod)
is done.

## Safety model

- **Explicit allowlist only.** Tables are listed by name in a YAML config
  checked into the calling repo. There is no wildcard or dynamic discovery.
- **Dry-run by default.** `dry_run` defaults to `"true"`. In dry-run mode the
  action logs which tables and row counts would be affected and truncates
  nothing.
- **Foreign key / trigger checks.** Before touching a table, the action checks
  for foreign keys in either direction (the table's own FKs, and other tables
  that reference it as a child), plus any triggers on the table. If either is
  found, that table is marked `BLOCKED` and skipped rather than truncated.
- **Fail-closed.** If `fail_on_block: true` (default) and any table is
  blocked, or a listed table doesn't exist, the job exits non-zero.
- **Slack alerting.** Posts a summary to the configured Slack channel on both
  success and failure.

## Inputs

| Input                | Required | Default | Notes                                                        |
|-----------------------|----------|---------|---------------------------------------------------------------|
| `db_engine`           | yes      | -       | `mysql` or `postgres`                                        |
| `db_host`             | yes      | -       | pass via `secrets.*` in the caller workflow                  |
| `db_port`             | yes      | -       |                                                                 |
| `db_name`             | yes      | -       | database name (mysql) / database to connect to (postgres)     |
| `db_username`         | yes      | -       | pass via `secrets.*`                                          |
| `db_password`         | yes      | -       | pass via `secrets.*`                                          |
| `config_path`         | yes      | -       | path to the allowlist YAML, in the caller repo's checkout      |
| `dry_run`             | no       | `"true"`| set to `"false"` only after the checklist below is complete   |
| `slack_webhook_url`   | no       | `""`    | pass via `secrets.*`                                          |

## Allowlist config format

See [`config/tables.example.yml`](./config/tables.example.yml):

```yaml
fail_on_block: true
slack:
  channel: "#data-retention-alerts"
tables:
  - schema: public
    name: example_audit_log
  - schema: public
    name: example_session_logs
```

`schema` is used for Postgres; it's ignored for MySQL (MySQL uses `db_name`
from the action inputs as the database/schema).

## Example caller workflow (in the target application repo, not here)

```yaml
name: Purge expired records

on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch: {}

jobs:
  purge:
    runs-on: ubuntu-latest
    # Required-reviewer gate lives on this GitHub Environment, configured in
    # the target repo's settings - this action does not enforce it itself.
    environment: prod-db-purge
    steps:
      - uses: actions/checkout@v4
      - uses: gyg/github-actions/truncate-tables@main
        with:
          db_engine: postgres
          db_host: ${{ secrets.RETENTION_DB_HOST }}
          db_port: "5432"
          db_name: ${{ secrets.RETENTION_DB_NAME }}
          db_username: ${{ secrets.RETENTION_DB_USERNAME }}
          db_password: ${{ secrets.RETENTION_DB_PASSWORD }}
          config_path: .github/truncate-tables.yml
          dry_run: "true"
          slack_webhook_url: ${{ secrets.SLACK_RETENTION_WEBHOOK_URL }}
```

## Before enabling against prod

This scaffold intentionally stops short of being prod-ready. Before any
consuming repo flips `dry_run` to `"false"` against a production database:

1. Replace the example config with a real, ticket-specific table allowlist.
2. Get explicit sign-off from each table's data/schema owner that truncation
   is safe under the current retention policy - record it on the PR that adds
   the real config.
3. Configure a protected GitHub Environment (e.g. `prod-db-purge`) on the
   scheduled workflow, restricted to authorized approvers.
4. Run the workflow with `dry_run: "true"` against a non-prod database first
   and confirm the logged tables/row counts/blocked-table list match
   expectations.
5. Only then flip `dry_run` to `"false"` for the prod schedule.

## Local testing

```bash
cd truncate-tables
pip install -r requirements.txt pytest
python -m pytest tests/
```
