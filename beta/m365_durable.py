"""Encrypted, singleton credential persistence for a hosted M365 beta.

The module is inert unless the operator configures both DATABASE_URL and an
encryption key.  It never serializes decrypted credentials into status data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATABASE_URL_ENV = "CODEX_AUTH_M365_BETA_DATABASE_URL"
CREDENTIAL_KEY_ENV = "CODEX_AUTH_M365_BETA_CREDENTIAL_KEY"
RECORD_NAME = "m365-copilot"


class DurableCredentialError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get(DATABASE_URL_ENV) and os.environ.get(CREDENTIAL_KEY_ENV))


def _key() -> bytes:
    value = os.environ.get(CREDENTIAL_KEY_ENV, "")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise DurableCredentialError("credential encryption key is invalid") from exc
    if len(raw) not in {16, 24, 32}:
        # Permit an operator-provided high-entropy passphrase without storing it.
        raw = hashlib.sha256(value.encode()).digest()
    return raw


class PostgresCredentialStore:
    """Single encrypted record guarded by PostgreSQL transaction locks."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.environ.get(DATABASE_URL_ENV, "")
        if not self.database_url:
            raise DurableCredentialError("external credential database is not configured")
        self._cipher = AESGCM(_key())

    @staticmethod
    def _driver() -> Any:
        try:
            import psycopg
        except ImportError as exc:
            raise DurableCredentialError("psycopg is required for durable credential storage") from exc
        return psycopg

    def _connect(self) -> Any:
        return self._driver().connect(self.database_url)

    def _ensure(self, connection: Any) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS codex_auth_m365_credential "
            "(name text PRIMARY KEY, version bigint NOT NULL, payload bytea NOT NULL)"
        )

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return nonce + self._cipher.encrypt(nonce, plaintext, RECORD_NAME.encode())

    def _decrypt(self, value: bytes) -> dict[str, Any]:
        if len(value) < 13:
            raise DurableCredentialError("encrypted credential record is invalid")
        try:
            plain = self._cipher.decrypt(value[:12], value[12:], RECORD_NAME.encode())
            decoded = json.loads(plain)
        except Exception as exc:
            raise DurableCredentialError("encrypted credential record cannot be opened") from exc
        if not isinstance(decoded, dict):
            raise DurableCredentialError("encrypted credential record is invalid")
        return decoded

    def load(self) -> tuple[dict[str, Any] | None, int | None]:
        with self._connect() as connection:
            self._ensure(connection)
            row = connection.execute(
                "SELECT version, payload FROM codex_auth_m365_credential WHERE name=%s", (RECORD_NAME,)
            ).fetchone()
            if row is None:
                return None, None
            return self._decrypt(bytes(row[1])), int(row[0])

    def save(self, value: dict[str, Any], expected_version: int | None = None) -> int:
        """Atomically replace the active encrypted bundle using a DB lock."""

        encrypted = self._encrypt(value)
        with self._connect() as connection:
            self._ensure(connection)
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (RECORD_NAME,))
            row = connection.execute(
                "SELECT version FROM codex_auth_m365_credential WHERE name=%s FOR UPDATE", (RECORD_NAME,)
            ).fetchone()
            current = int(row[0]) if row else None
            if expected_version is not None and current != expected_version:
                raise DurableCredentialError("credential_version_conflict")
            next_version = (current or 0) + 1
            connection.execute(
                "INSERT INTO codex_auth_m365_credential(name, version, payload) VALUES (%s,%s,%s) "
                "ON CONFLICT (name) DO UPDATE SET version=EXCLUDED.version,payload=EXCLUDED.payload",
                (RECORD_NAME, next_version, encrypted),
            )
            return next_version

    @staticmethod
    def safe_status() -> dict[str, Any]:
        return {"source": "encrypted_external_postgres", "rotation_persistence": "database_atomic", "restart_durable": True}
