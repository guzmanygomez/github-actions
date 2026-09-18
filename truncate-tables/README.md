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
- **Locked truncation with retry.** Before truncating, the action takes an
  exclusive lock on the table (`LOCK TABLE ... ACCESS EXCLUSIVE` on Postgres,
  `LOCK TABLES ... WRITE` on MySQL), retrying with jittered exponential
  backoff (10 attempts by default) if it's contended. If the lock can't be
  acquired, the table is left untouched and reported as `LOCK_FAILED` -
  it is never truncated without holding the lock. See
  [Locking, retries, and PK sequence preservation](#locking-retries-and-pk-sequence-preservation).
- **PK sequence/AUTO_INCREMENT preservation.** While holding the lock, the
  action captures the table's current PK sequence (Postgres) or
  AUTO_INCREMENT (MySQL) "next value", truncates, and restores that value -
  so the next inserted row doesn't reuse a PK already sent to the datalake.
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
| `summary` | One line per allowlisted table: its result and detail (see statuses below) |

Per-table statuses that can appear in `summary`: `DRY_RUN`, `TRUNCATED`,
`BLOCKED` (FKs/triggers found), `LOCK_FAILED` (couldn't acquire the lock
after all retries - table untouched), `TRUNCATED_SEQUENCE_RESTORE_FAILED`
(truncated, but restoring the PK sequence/AUTO_INCREMENT afterwards failed -
**needs manual follow-up**, see below), or `ERROR` (e.g. table not found).
Every status except `DRY_RUN` and `TRUNCATED` fails the job (`BLOCKED` only
fails it if `fail_on_block: true`, the default).

## Allowlist config format

Every table entry declares its own `database` (required) and, for Postgres,
`schema` (optional, defaults to `public`) - it's a Postgres-only concept and
is omitted for MySQL. The action opens one connection per table using that
table's `database`, so a single config (and single `db_engine`/login) can
span multiple databases/schemas on the same server.

Postgres - see [`config/tables.postgres.example.yml`](./config/tables.postgres.example.yml):

```yaml
fail_on_block: true
lock:
  max_attempts: 10
  base_delay_seconds: 1
  max_delay_seconds: 30
  attempt_timeout_seconds: 3
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
lock:
  max_attempts: 10
  base_delay_seconds: 1
  max_delay_seconds: 30
  attempt_timeout_seconds: 3
tables:
  - database: gyg
    name: example_audit_log
  - database: gyg
    name: example_session_logs
  - database: gyg_reporting
    name: example_stale_exports
```

## Locking, retries, and PK sequence preservation

Truncating a high-transaction table risks a PK clash with the datalake if a
new row is inserted right after truncation and reuses a PK value that was
already sent downstream. To prevent that, for each table (once it's passed
the FK/trigger check and `dry_run` is `"false"`) the action:

1. Takes an exclusive lock on the table - `LOCK TABLE ... IN ACCESS EXCLUSIVE
   MODE` (Postgres) or `LOCK TABLES ... WRITE` (MySQL) - so no other
   transaction can read or write it for the duration.
2. If the lock is contended, retries with full-jitter exponential backoff:
   `lock.max_attempts` tries (default 10), starting at `lock.base_delay_seconds`
   (default 1s) and capped at `lock.max_delay_seconds` (default 30s) between
   tries, with each individual attempt bounded by
   `lock.attempt_timeout_seconds` (default 3s). If every attempt fails, the
   table is reported `LOCK_FAILED` and left completely untouched.
3. Once locked, captures the table's current PK sequence value: for each
   candidate, it takes the **larger** of two readings, since either one
   alone can be stale/wrong:
   - Postgres: every sequence owned by one of the table's columns (via
     `pg_get_serial_sequence`), read as `last_value`/`is_called`.
   - MySQL: `information_schema.tables.AUTO_INCREMENT` - but this can be
     served from a cached stats snapshot up to `information_schema_stats_expiry`
     old (default 24h on MySQL 8/Aurora MySQL), so the action first runs
     `SET SESSION information_schema_stats_expiry = 0` to force a live read.
   - Both engines: `MAX(pk_column)` read directly off the table while the
     lock is held (exact, not cached) - the restored value is never less
     than `MAX(pk_column) + 1`.
4. Truncates the table, then restores the higher of those two values so the
   next inserted row continues from where it left off (MySQL: `ALTER TABLE
   ... AUTO_INCREMENT = ...`; Postgres: `setval(..., is_called = false)` so
   the *next* `nextval()` returns exactly that value).
5. Releases the lock (Postgres: on `COMMIT`, alongside the sequence restore,
   in the same transaction; MySQL: explicit `UNLOCK TABLES` after the DDL,
   since `TRUNCATE`/`ALTER TABLE` auto-commit and aren't part of a Postgres-
   style transaction).

Each table's line in the `summary` output includes rows removed, how long
the lock+truncate+restore took, the reported sequence/AUTO_INCREMENT value,
the observed `MAX(pk)`, and the value it was actually set to - e.g.:

```
kms.oms_kitchen_snapshot: TRUNCATED (18432 rows removed in 0.41s (lock acquired on attempt 1); AUTO_INCREMENT column=id reported_next=1 observed_max=182004 set_to=182005)
```

**Caveat:** table locks don't prevent a session from calling `nextval()`
directly without touching the table, so this specifically protects the
common insert-driven case, not every conceivable way a sequence's value
could change concurrently.

**If you see `TRUNCATED_SEQUENCE_RESTORE_FAILED`:** the table was already
truncated (that part can't be undone) but restoring its PK
sequence/AUTO_INCREMENT afterwards failed - MySQL's `TRUNCATE`/`ALTER TABLE`
aren't transactional, so this can't be rolled back automatically. Treat it
as an incident: manually verify/set the correct next PK value on that table
before any new rows are inserted.

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
  only `SELECT`, `TRUNCATE`, and (for the lock + PK sequence restore)
  `LOCK TABLES`/`ALTER` (MySQL) or nothing extra beyond `TRUNCATE`
  (Postgres, since locking/`setval` don't need separate grants) on the
  allowlisted tables - not an admin/app credential.

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
5. For any table with real transactional traffic, test a live (non-dry-run)
   run against a non-prod replica of that traffic pattern and confirm the
   lock is held only briefly and doesn't cause a noticeable request
   pile-up; if it might, schedule the prod cron for a low-traffic window and
   coordinate with the table's owning team beforehand.
6. Only then flip `dry_run` to `"false"` for the prod schedule.

## Local testing

```bash
cd truncate-tables
pip install -r requirements.txt pytest
python -m pytest tests/
```
