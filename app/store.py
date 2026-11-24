from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from .config import DATA_KEY, DB_PATH
from .session_vault import decrypt_bytes, decrypt_json, encrypt_bytes, encrypt_json


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
              api_key_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cache (
              project_id TEXT NOT NULL, resource TEXT NOT NULL, payload TEXT NOT NULL,
              updated_at TEXT NOT NULL, PRIMARY KEY (project_id, resource)
            );
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, resource TEXT NOT NULL,
              entity_id TEXT NOT NULL, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
              before_data TEXT, after_data TEXT
            );
            CREATE TABLE IF NOT EXISTS sale_je_drafts (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              submission_key TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              pdf_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL,
              pdf_payload BLOB NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              approved_at TEXT,
              posted_appfolio_id TEXT,
              UNIQUE(project_id, submission_key),
              UNIQUE(project_id, fingerprint)
            );
        """)
        self.connection.commit()

    def create_project(self, name: str, email_domain: str, service_email: str | None = None, api_key: str | None = None) -> tuple[dict[str, str], str]:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        project_id = secrets.token_urlsafe(12)
        slug = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")[:32] or "appfolio"
        domain = email_domain.strip().lower().lstrip("@")
        if not domain or "." not in domain:
            raise ValueError("emailDomain must be a valid company domain")
        if service_email:
            email = service_email.strip().lower()
            if "@" not in email or email.rsplit("@", 1)[1] != domain:
                raise ValueError("serviceEmail must be a valid address on emailDomain")
        else:
            email = f"appfolio-{slug}-{project_id[:6]}@{domain}"
        api_key = api_key or ("afib_" + secrets.token_urlsafe(32))
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        project = {"id": project_id, "name": name, "email": email, "createdAt": now()}
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                (
                    project_id,
                    project["name"],
                    email,
                    api_key_hash,
                    project["createdAt"],
                ),
            )
        return project, api_key

    def project_by_email(self, email: str):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM projects WHERE email=?",
                (email.strip().lower(),),
            ).fetchone()

    def set_api_key(self, project_id: str, api_key: str) -> None:
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE projects SET api_key_hash=? WHERE id=?",
                (digest, project_id),
            )

    def authenticate(self, api_key: str) -> sqlite3.Row | None:
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        with self.lock:
            for row in self.connection.execute("SELECT * FROM projects"):
                if hmac.compare_digest(row["api_key_hash"], digest):
                    return row
        return None

    def default_project(self) -> sqlite3.Row | None:
        """Return the sole configured project used by the localhost UI."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM projects ORDER BY created_at LIMIT 2"
            ).fetchall()
            return rows[0] if len(rows) == 1 else None

    def put_cache(
        self,
        project_id: str,
        resource: str,
        data: list[dict[str, Any]],
        *,
        record_events: bool = True,
    ) -> str:
        return self.put_caches(
            project_id,
            {resource: data},
            record_events=record_events,
        )[resource]

    def put_caches(
        self,
        project_id: str,
        snapshots: dict[str, list[dict[str, Any]]],
        *,
        record_events: bool = True,
    ) -> dict[str, str]:
        if not snapshots:
            return {}
        updated_at = now()
        with self.lock:
            previous: dict[str, tuple[bool, list[dict[str, Any]]]] = {}
            payloads: dict[str, str] = {}
            for resource, data in snapshots.items():
                previous_row = self.connection.execute(
                    "SELECT payload FROM cache "
                    "WHERE project_id=? AND resource=?",
                    (project_id, resource),
                ).fetchone()
                previous[resource] = (
                    previous_row is not None,
                    (
                        decrypt_json(previous_row["payload"], DATA_KEY)
                        if previous_row
                        else []
                    ),
                )
                payloads[resource] = (
                    encrypt_json(data, DATA_KEY)
                    if DATA_KEY
                    else json.dumps(data)
                )
            with self.connection:
                for resource, data in snapshots.items():
                    self.connection.execute(
                        "INSERT INTO cache VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(project_id, resource) DO UPDATE SET "
                        "payload=excluded.payload, updated_at=excluded.updated_at",
                        (
                            project_id,
                            resource,
                            payloads[resource],
                            updated_at,
                        ),
                    )
                for resource, data in snapshots.items():
                    existed, old_data = previous[resource]
                    if existed and record_events:
                        self._record_events(
                            project_id,
                            resource,
                            old_data,
                            data,
                            updated_at,
                        )
            return {resource: updated_at for resource in snapshots}

    def _record_events(self, project_id: str, resource: str, before: list[dict[str, Any]], after: list[dict[str, Any]], occurred_at: str) -> None:
        def keyed(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {str(item.get("appfolioId") or item.get("id")): item for item in items if item.get("appfolioId") or item.get("id")}
        old, new = keyed(before), keyed(after)
        for entity_id in old.keys() | new.keys():
            left, right = old.get(entity_id), new.get(entity_id)
            if left == right:
                continue
            event_type = f"{resource}.created" if left is None else f"{resource}.removed" if right is None else f"{resource}.updated"
            if resource == "move-out-workflows" and left is None and right:
                if (
                    not right.get("completedMoveOut")
                    and right.get("moveOutWorkflowId")
                ):
                    event_type = "tenant.move_out_scheduled"
            if resource == "tenants" and left and right:
                old_move = (left or {}).get("moveOutOn") or (left or {}).get("move_out_on")
                new_move = right.get("moveOutOn") or right.get("move_out_on")
                old_status = str(left.get("Status") or "").casefold()
                new_status = str(right.get("Status") or "").casefold()
                if new_status == "past" and old_status != "past":
                    event_type = "tenant.moved_out"
                elif new_move and new_move != old_move and new_status != "past":
                    event_type = "tenant.move_out_scheduled"
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (secrets.token_urlsafe(12), project_id, resource, entity_id, event_type, occurred_at,
                 (encrypt_json(left, DATA_KEY) if DATA_KEY else json.dumps(left)) if left else None,
                 (encrypt_json(right, DATA_KEY) if DATA_KEY else json.dumps(right)) if right else None),
            )
    def get_events(
        self,
        project_id: str,
        limit: int = 100,
        *,
        after_cursor: int | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.lock:
            conditions = ["project_id=?"]
            params: list[Any] = [project_id]
            if after_cursor is not None:
                conditions.append("rowid>?")
                params.append(max(after_cursor, 0))
            if event_type:
                conditions.append("event_type=?")
                params.append(event_type)
            order = "ASC" if after_cursor is not None else "DESC"
            params.append(min(max(limit, 1), 500))
            rows = self.connection.execute(
                "SELECT rowid AS cursor_id, * FROM events WHERE "
                + " AND ".join(conditions)
                + f" ORDER BY rowid {order} LIMIT ?",
                params,
            ).fetchall()
            events = []
            for row in rows:
                before = (
                    decrypt_json(row["before_data"], DATA_KEY)
                    if row["before_data"]
                    else None
                )
                after = (
                    decrypt_json(row["after_data"], DATA_KEY)
                    if row["after_data"]
                    else None
                )
                events.append({
                    "cursor": str(row["cursor_id"]),
                    "id": row["id"],
                    "resource": row["resource"],
                    "entityId": row["entity_id"],
                    "type": row["event_type"],
                    "occurredAt": row["occurred_at"],
                    "effectiveOn": (
                        (after or {}).get("moveOutOn")
                        if str(row["event_type"]).startswith("tenant.move")
                        else None
                    ),
                    "before": before,
                    "after": after,
                })
            return events

    def get_cache(self, project_id: str, resource: str) -> tuple[list[dict[str, Any]], str | None]:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload, updated_at FROM cache "
                "WHERE project_id=? AND resource=?",
                (project_id, resource),
            ).fetchone()
            return (
                (decrypt_json(row["payload"], DATA_KEY), row["updated_at"])
                if row
                else ([], None)
            )

    def create_sale_je_draft(
        self,
        project_id: str,
        submission_key: str,
        fingerprint: str,
        pdf_sha256: str,
        payload: dict[str, Any],
        pdf_bytes: bytes,
    ) -> tuple[dict[str, Any], bool]:
        """Create an encrypted sale-JE draft or return the matching duplicate."""
        if not DATA_KEY:
            raise RuntimeError("Sale-JE drafts require BRIDGE_DATA_KEY encryption")
        draft_id = secrets.token_urlsafe(18)
        timestamp = now()
        encrypted_payload = encrypt_json(payload, DATA_KEY)
        encrypted_pdf = encrypt_bytes(pdf_bytes, DATA_KEY)
        with self.lock:
            try:
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO sale_je_drafts "
                        "(id, project_id, submission_key, fingerprint, pdf_sha256, "
                        "status, payload, pdf_payload, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)",
                        (
                            draft_id,
                            project_id,
                            submission_key,
                            fingerprint,
                            pdf_sha256,
                            encrypted_payload,
                            encrypted_pdf,
                            timestamp,
                            timestamp,
                        ),
                    )
                created = True
            except sqlite3.IntegrityError:
                row = self.connection.execute(
                    "SELECT id FROM sale_je_drafts WHERE project_id=? "
                    "AND (submission_key=? OR fingerprint=?) "
                    "ORDER BY created_at LIMIT 1",
                    (project_id, submission_key, fingerprint),
                ).fetchone()
                if not row:
                    raise
                draft_id = row["id"]
                created = False
        draft = self.get_sale_je_draft(project_id, draft_id)
        if not draft:
            raise RuntimeError("The sale-JE draft could not be read after creation")
        return draft, created

    def get_sale_je_draft(
        self,
        project_id: str,
        draft_id: str,
        *,
        include_pdf: bool = False,
    ) -> dict[str, Any] | None:
        if not DATA_KEY:
            raise RuntimeError("Sale-JE drafts require BRIDGE_DATA_KEY encryption")
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM sale_je_drafts WHERE project_id=? AND id=?",
                (project_id, draft_id),
            ).fetchone()
            if not row:
                return None
            payload = decrypt_json(row["payload"], DATA_KEY)
            result = {
                **payload,
                "id": row["id"],
                "status": row["status"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "approvedAt": row["approved_at"],
                "postedAppfolioId": row["posted_appfolio_id"],
                "pdfSha256": row["pdf_sha256"],
            }
            if include_pdf:
                result["pdfBytes"] = decrypt_bytes(row["pdf_payload"], DATA_KEY)
            return result

    def list_sale_je_drafts(
        self,
        project_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not DATA_KEY:
            raise RuntimeError("Sale-JE drafts require BRIDGE_DATA_KEY encryption")
        with self.lock:
            rows = self.connection.execute(
                "SELECT id FROM sale_je_drafts WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (project_id, min(max(limit, 1), 50)),
            ).fetchall()
        return [
            draft
            for row in rows
            if (draft := self.get_sale_je_draft(project_id, row["id"]))
        ]

    def transition_sale_je_draft(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_status: str,
        new_status: str,
    ) -> dict[str, Any] | None:
        allowed = {
            ("draft", "approved"),
        }
        if (expected_status, new_status) not in allowed:
            raise ValueError("Invalid sale-JE draft status transition")
        timestamp = now()
        approved_at = timestamp if new_status == "approved" else None
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE sale_je_drafts SET status=?, updated_at=?, "
                "approved_at=COALESCE(approved_at, ?) "
                "WHERE project_id=? AND id=? AND status=?",
                (
                    new_status,
                    timestamp,
                    approved_at,
                    project_id,
                    draft_id,
                    expected_status,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self.get_sale_je_draft(project_id, draft_id)


store = Store()
