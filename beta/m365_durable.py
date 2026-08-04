"""Encrypted, singleton credential persistence for a hosted M365 beta.

The module is inert unless the operator configures both DATABASE_URL and an
encryption key.  It never serializes decrypted credentials into status data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATABASE_URL_ENV = "CODEX_AUTH_M365_BETA_DATABASE_URL"
CREDENTIAL_KEY_ENV = "CODEX_AUTH_M365_BETA_CREDENTIAL_KEY"
REQUIRE_DURABLE_ENV = "CODEX_AUTH_M365_BETA_REQUIRE_DURABLE"
DATABASE_EXPIRES_AT_ENV = "CODEX_AUTH_M365_BETA_DATABASE_EXPIRES_AT"
RECORD_NAME = "m365-copilot"


class DurableCredentialError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get(DATABASE_URL_ENV) and os.environ.get(CREDENTIAL_KEY_ENV))


def required() -> bool:
    """Whether hosted beta must fail closed without durable storage."""

    return os.environ.get(REQUIRE_DURABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def safe_expiry() -> str | None:
    """Return only the operator-provided Free Postgres expiry timestamp."""

    value = os.environ.get(DATABASE_EXPIRES_AT_ENV, "").strip()
    return value or None


def _key() -> bytes:
    value = os.environ.get(CREDENTIAL_KEY_ENV, "")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError, binascii.Error) as exc:
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
        connection.execute(
            "CREATE TABLE IF NOT EXISTS codex_auth_m365_credential_backup "
            "(id bigserial PRIMARY KEY, version bigint NOT NULL, reason text NOT NULL, "
            "payload bytea NOT NULL, created_at bigint NOT NULL)"
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

    @contextmanager
    def locked_record(self) -> Iterator[tuple[Any, dict[str, Any] | None, int | None]]:
        """Hold the authoritative advisory lock for a complete operation.

        Callers may perform an upstream exchange while this context is open,
        then use :meth:`save_locked` on the same transaction.  This prevents
        two hosted workers from exchanging the same refresh token concurrently.
        """

        with self._connect() as connection:
            self._ensure(connection)
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (RECORD_NAME,))
            row = connection.execute(
                "SELECT version, payload FROM codex_auth_m365_credential WHERE name=%s FOR UPDATE",
                (RECORD_NAME,),
            ).fetchone()
            if row is None:
                yield connection, None, None
            else:
                yield connection, self._decrypt(bytes(row[1])), int(row[0])

    def save_locked(
        self,
        connection: Any,
        value: dict[str, Any],
        expected_version: int | None,
    ) -> int:
        """Save while ``locked_record`` owns the transaction and advisory lock."""

        encrypted = self._encrypt(value)
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

    def backup_locked(self, connection: Any, reason: str, row: tuple[Any, ...] | None = None) -> int | None:
        """Write a rollback copy using an already-held transaction lock."""

        safe_reason = reason if reason in {"import", "refresh", "campaign"} else "unspecified"
        current = row or connection.execute(
            "SELECT version, payload FROM codex_auth_m365_credential WHERE name=%s FOR UPDATE",
            (RECORD_NAME,),
        ).fetchone()
        if current is None:
            return None
        connection.execute(
            "INSERT INTO codex_auth_m365_credential_backup(version,reason,payload,created_at) VALUES (%s,%s,%s,%s)",
            (int(current[0]), safe_reason, bytes(current[1]), int(time.time())),
        )
        return int(current[0])

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

    def backup_current(self, reason: str) -> int | None:
        """Create an encrypted rollback record without returning its contents."""

        safe_reason = reason if reason in {"import", "refresh", "campaign"} else "unspecified"
        with self._connect() as connection:
            self._ensure(connection)
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (RECORD_NAME,))
            row = connection.execute(
                "SELECT version, payload FROM codex_auth_m365_credential WHERE name=%s FOR UPDATE", (RECORD_NAME,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "INSERT INTO codex_auth_m365_credential_backup(version,reason,payload,created_at) VALUES (%s,%s,%s,%s)",
                (int(row[0]), safe_reason, bytes(row[1]), int(time.time())),
            )
            return int(row[0])

    @staticmethod
    def safe_status() -> dict[str, Any]:
        return {
            "source": "encrypted_external_postgres",
            "rotation_persistence": "database_atomic",
            "rollback_records": "encrypted_external_postgres",
            "restart_durable": True,
            "durability_state": "durable",
            "database_expires_at": safe_expiry(),
        }
