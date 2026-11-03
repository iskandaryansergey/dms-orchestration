# ADR 0009 — IAM database authentication for jobs; DMS and the Data API keep secrets

- Status: accepted
- Date: 2026-10-29

## Context

The platform prefers IAM authentication to database passwords. The upstream aurora module declares IAM database users (`iam_database_users`), creates them in the engine through
the Data API (`create_iam_database_users`) and builds one `rds-db:connect` policy per user
(`iam_database_auth.users[<user>].policy_arn`). DMS PostgreSQL endpoints cannot log in with an
IAM token, and the Data API logs in only with a Secrets Manager secret.

## Decision

* A connection gets `auth = "secret" | "iam"` (default `secret`). `iam` needs `engine =
  "postgresql"`, `iam_user`, `host` and `iam_policy_arn`; it has no `secret_arn`.
* The module attaches `iam_policy_arn` to the project's job role (one attachment per IAM
  connection, keyed `<project>/<task>/<connection>`). It writes no `rds-db:connect` policy of its
  own: the upstream module scopes it to the user's `dbuser` ARN.
* The registration carries `auth` and `iamUser`. In the Fargate container `orch_job.py` reads no
  secret for an IAM connection and writes no password into the connections file; `psql-run`
  (and `pg-dump-s3`, `pg-restore-s3`, `verify-target`, which start psql the same way) call
  `generate_db_auth_token` with the task role when the client starts and pass the token only in
  `PGPASSWORD`. A token lives 15 minutes; each client start signs a new one, so long jobs are not
  affected after login. TLS (`sslMode` require, verify-ca, verify-full) is required.
* The SSM and Lambda SQL runtimes take the same connection: on an SSM runner the same wrapper
  signs the token with the standard-library client (`orch_aws.py`), and the Lambda SQL runtime
  (pg8000) signs it with the job role (`shared/sqlclient.py`). Proven live on Fargate
  (`e2e.py s1iam`); the SSM and Lambda paths by unit tests.
* DMS endpoints and the Data API (built-in Lambda jobs) stay on secrets; an IAM connection is a
  second connection to the same database (`target_iam` next to `target` in the sandbox).

## Consequences

* A job of a project, on any runtime, can log in as that project's IAM user without any secret.
* The engine user still needs grants for what the job does; the aurora module only grants
  `rds_iam`. Granting is the platform team's (or a pre-job of a secret connection).
* MySQL IAM login is not supported (refused before the client starts).
