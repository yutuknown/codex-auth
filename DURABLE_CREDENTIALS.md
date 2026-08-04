# Hosted beta credential durability

The hosted M365 beta is fail-closed when `CODEX_AUTH_M365_BETA_REQUIRE_DURABLE=1`.
Configure these Render secrets on `codex-auth-beta` before importing or
refreshing a credential:

```text
CODEX_AUTH_M365_BETA_DATABASE_URL=<reachable PostgreSQL URL>
CODEX_AUTH_M365_BETA_CREDENTIAL_KEY=<high-entropy AES-GCM key>
CODEX_AUTH_M365_BETA_REQUIRE_DURABLE=1
```

Keep API/admin keys and the optional `CODEX_AUTH_M365_BETA_AUTH_JSON` bootstrap
response in Render secrets. The bootstrap response seeds an empty encrypted
record only; once a record exists it is never used to overwrite it. Never put
these values in Git, logs, screenshots, or proof bundles.

After deployment, `GET /health` must report `source:
encrypted_external_postgres`, `restart_durable: true`, and a numeric
`credential_version`. If the database, encryption key, or driver is missing,
the service reports `durability_unavailable` and blocks import, refresh, and
generation instead of silently falling back to process memory.

The database stores one encrypted M365 OAuth bundle plus encrypted rollback
records. Refreshes use a PostgreSQL advisory transaction lock and
compare-and-swap versioning so concurrent workers and Render restarts cannot
activate an older refresh token.
