# truncate-tables (scaffold)

Composite action that truncates an explicit, reviewed allowlist of database
tables (MySQL or Postgres), as part of a production data retention/purge
policy. Built under BHPL-3313.

**This is a scaffold.** It is safe to merge as-is (default `dry_run: true`,
example config only), but it must not be wired up against a real production
database until every item in [Before enabling against prod](#before-enabling-against-prod)
is done.

## Safety model

- **Explicit allowlist only.** Tables are listed by name (with their own
  database/schema) in a YAML config checked into the calling repo. There is
  no wildcard or dynamic discovery.
- **Dry-run by default.** `dry_run` defaults to `"true"`. In dry-run mode the
  action logs which tables and row counts would be affected and truncates
  nothing.
- **Foreign key / trigger checks.** Before touching a table, the action checks
  for foreign keys in either direction (the table's own FKs, and other tables
  that reference it as a child), plus any triggers on the table. If either is
  found, that table is marked `BLOCKED` and skipped rather than truncated.
- **Fail-closed.** If `fail_on_block: true` (default) and any table is
  blocked, or a listed table doesn't exist, the job exits non-zero.
- **Slack alerting.** The action itself does not call Slack - it emits
  `status` (`success`/`failure`) and `summary` outputs. The caller workflow
  posts these to Slack via [`rtcamp/action-slack-notify`](https://github.com/rtcamp/action-slack-notify),
  the same pattern used elsewhere in GYG's workflows (see
  [`selfServiceGrantDBAccess.yml`](https://github.com/guzmanygomez/bhyve-platform-management/blob/main/.github/workflows/selfServiceGrantDBAccess.yml#L175)).
  This keeps Slack config/credentials entirely in the caller workflow, not in
  this action or its Python code.

## Inputs

| Input                | Required | Default | Notes                                                        |
|-----------------------|----------|---------|---------------------------------------------------------------|
| `db_engine`           | yes      | -       | `mysql` or `postgres`                                        |
| `db_host`             | yes      | -       | pass via `secrets.*` in the caller workflow                  |
| `db_port`             | yes      | -       |                                                                 |
| `db_username`         | yes      | -       | pass via `secrets.*` - one login used for every table below   |
| `db_password`         | yes      | -       | pass via `secrets.*`                                          |
| `config_path`         | yes      | -       | path to the allowlist YAML, in the caller repo's checkout      |
| `dry_run`             | no       | `"true"`| set to `"false"` only after the checklist below is complete   |

There is no `db_name`/`db_schema` input. Each table entry in the allowlist
config names its own `database` (and `schema` for Postgres), so one run - one
login - can cover multiple databases/schemas on the same server. If your
target tables live under different logins entirely, run the action once per
login/config.

## Outputs

| Output    | Description                                                                 |
|-----------|-------------------------------------------------------------------------------|
| `status`  | `success` or `failure` - use it to pick the Slack message color/pass/fail icon |
| `summary` | One line per allowlisted table: its result (dry-run/truncated/blocked/error) and detail |

## Allowlist config format

Every table entry declares its own `database` (required) and, for Postgres,
`schema` (optional, defaults to `public`) - it's a Postgres-only concept and
is omitted for MySQL. The action opens one connection per table using that
table's `database`, so a single config (and single `db_engine`/login) can
span multiple databases/schemas on the same server.

Postgres - see [`config/tables.postgres.example.yml`](./config/tables.postgres.example.yml):

```yaml
fail_on_block: true
tables:
  - database: analytics
    schema: public
    name: example_audit_log
  - database: analytics
    schema: public
    name: example_session_logs
  - database: reporting
    schema: staging
    name: example_stale_exports
```

MySQL - see [`config/tables.mysql.example.yml`](./config/tables.mysql.example.yml):

```yaml
fail_on_block: true
tables:
  - database: gyg
    name: example_audit_log
  - database: gyg
    name: example_session_logs
  - database: gyg_reporting
    name: example_stale_exports
```

## Sample workflow (in the target application repo, not here)

This is a complete, runnable example of a caller workflow: it schedules the
job, checks out the config, calls this action, and reports the result to
Slack whether the run succeeds or fails.

### Postgres example

`.github/workflows/purge-expired-records.yml`:

```yaml
name: Purge expired records

on:
  # Every Monday at 03:00 UTC.
  schedule:
    - cron: "0 3 * * 1"
  # Allows a manual, on-demand run (e.g. to re-check after fixing a blocked table).
  workflow_dispatch: {}

jobs:
  purge:
    runs-on: ubuntu-latest
    # Required-reviewer gate lives on this GitHub Environment, configured in
    # the target repo's settings - this action does not enforce it itself.
    environment: prod-db-purge
    env:
      # Flip to "false" only once the "Before enabling against prod"
      # checklist below is fully done for this repo/environment.
      DRY_RUN: "true"
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Truncate allowlisted tables
        id: truncate
        uses: gyg/github-actions/truncate-tables@main
        with:
          db_engine: postgres
          db_host: ${{ secrets.RETENTION_DB_HOST }}
          db_port: "5432"
          db_username: ${{ secrets.RETENTION_DB_USERNAME }}
          db_password: ${{ secrets.RETENTION_DB_PASSWORD }}
          config_path: .github/truncate-tables.yml
          dry_run: ${{ env.DRY_RUN }}

      # `if: always()` so this still runs (and reports failure) even if the
      # truncate step above failed or was blocked.
      - name: Notify Slack
        if: always()
        uses: rtcamp/action-slack-notify@v2
        env:
          SLACK_CHANNEL: data-retention-alerts
          SLACK_COLOR: ${{ steps.truncate.outputs.status == 'success' && 'good' || 'danger' }}
          SLACK_TITLE: "Purge expired records - ${{ steps.truncate.outputs.status || 'failure' }}"
          SLACK_MESSAGE: |
            *Workflow:* ${{ github.workflow }}
            *Repo:* ${{ github.repository }}
            *Dry run:* ${{ env.DRY_RUN }}
            *Run:* ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}

            ${{ steps.truncate.outputs.summary }}
          SLACK_USERNAME: Github
          SLACK_WEBHOOK: ${{ secrets.SLACK_RETENTION_WEBHOOK_URL }}
```

`.github/truncate-tables.yml` (the allowlist config referenced above - can
span multiple databases/schemas, see [Allowlist config format](#allowlist-config-format)):

```yaml
fail_on_block: true
tables:
  - database: analytics
    schema: public
    name: example_audit_log
  - database: analytics
    schema: public
    name: example_session_logs
```

### MySQL example

Same workflow shape, just point it at a MySQL server and config instead:

`.github/workflows/purge-expired-records.yml`:

```yaml
name: Purge expired records

on:
  # Every Monday at 03:00 UTC.
  schedule:
    - cron: "0 3 * * 1"
  # Allows a manual, on-demand run (e.g. to re-check after fixing a blocked table).
  workflow_dispatch: {}

jobs:
  purge:
    runs-on: ubuntu-latest
    # Required-reviewer gate lives on this GitHub Environment, configured in
    # the target repo's settings - this action does not enforce it itself.
    environment: prod-db-purge
    env:
      # Flip to "false" only once the "Before enabling against prod"
      # checklist below is fully done for this repo/environment.
      DRY_RUN: "true"
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Truncate allowlisted tables
        id: truncate
        uses: gyg/github-actions/truncate-tables@main
        with:
          db_engine: mysql
          db_host: ${{ secrets.RETENTION_DB_HOST }}
          db_port: "3306"
          db_username: ${{ secrets.RETENTION_DB_USERNAME }}
          db_password: ${{ secrets.RETENTION_DB_PASSWORD }}
          config_path: .github/truncate-tables.yml
          dry_run: ${{ env.DRY_RUN }}

      # `if: always()` so this still runs (and reports failure) even if the
      # truncate step above failed or was blocked.
      - name: Notify Slack
        if: always()
        uses: rtcamp/action-slack-notify@v2
        env:
          SLACK_CHANNEL: data-retention-alerts
          SLACK_COLOR: ${{ steps.truncate.outputs.status == 'success' && 'good' || 'danger' }}
          SLACK_TITLE: "Purge expired records - ${{ steps.truncate.outputs.status || 'failure' }}"
          SLACK_MESSAGE: |
            *Workflow:* ${{ github.workflow }}
            *Repo:* ${{ github.repository }}
            *Dry run:* ${{ env.DRY_RUN }}
            *Run:* ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}

            ${{ steps.truncate.outputs.summary }}
          SLACK_USERNAME: Github
          SLACK_WEBHOOK: ${{ secrets.SLACK_RETENTION_WEBHOOK_URL }}
```

`.github/truncate-tables.yml` (the allowlist config referenced above):

```yaml
fail_on_block: true
tables:
  - database: gyg
    name: example_audit_log
  - database: gyg
    name: example_session_logs
```

### Other things a real deployment needs

Beyond the workflow and config files above:

- `RETENTION_DB_HOST`, `RETENTION_DB_USERNAME`, `RETENTION_DB_PASSWORD` and
  `SLACK_RETENTION_WEBHOOK_URL` registered as repo/environment secrets -
  never hardcoded in the workflow.
- The `prod-db-purge` GitHub Environment created in the target repo's
  settings, with required reviewers, before the workflow can run against
  prod.
- A dedicated, least-privilege DB user for `RETENTION_DB_USERNAME` that can
  only `SELECT` (for the checks) and `TRUNCATE` on the allowlisted tables -
  not an admin/app credential.

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
