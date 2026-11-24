import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class LinkRecord:
    slug: str
    target_url: str
    expires_at: int
    revoked: int
    filename: Optional[str]
    max_uses: Optional[int]
    use_count: int
    created_at: int
    last_access_at: Optional[int]
    mode: str = "file"
    options: Optional[dict[str, Any]] = None


class LinkStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS links (
                    slug TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    filename TEXT,
                    max_uses INTEGER,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_access_at INTEGER,
                    mode TEXT NOT NULL DEFAULT 'file',
                    options_json TEXT
                )
                """
            )
            # Migración automática para bases de datos existentes
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
            if "mode" not in existing_cols:
                conn.execute("ALTER TABLE links ADD COLUMN mode TEXT NOT NULL DEFAULT 'file'")
            if "options_json" not in existing_cols:
                conn.execute("ALTER TABLE links ADD COLUMN options_json TEXT")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_expires_at ON links(expires_at)")
            conn.commit()

    def create_link(
        self,
        slug: str,
        target_url: str,
        expires_at: int,
        filename: Optional[str],
        max_uses: Optional[int],
        mode: str = "file",
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        now = int(time.time())
        options_json = json.dumps(options, ensure_ascii=False) if options else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO links (slug, target_url, expires_at, filename, max_uses, created_at, mode, options_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, target_url, expires_at, filename, max_uses, now, mode, options_json),
            )
            conn.commit()

    def get_link(self, slug: str) -> Optional[LinkRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_valid_link(self, slug: str, now: int) -> Optional[LinkRecord]:
        rec = self.get_link(slug)
        if not rec or not self._is_valid(rec, now):
            return None
        return rec

    def revoke_link(self, slug: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE links SET revoked = 1 WHERE slug = ?", (slug,))
            conn.commit()
            return cur.rowcount > 0

    def register_use_if_valid(self, slug: str, now: int) -> Optional[LinkRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
            if not row:
                return None

            rec = self._row_to_record(row)
            if not self._is_valid(rec, now):
                return None

            conn.execute(
                "UPDATE links SET use_count = use_count + 1, last_access_at = ? WHERE slug = ?",
                (now, slug),
            )
            conn.commit()

            updated = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
            return self._row_to_record(updated) if updated else None

    def cleanup_expired(self, now: int) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM links WHERE (expires_at > 0 AND expires_at < ?) OR revoked = 1 OR (max_uses IS NOT NULL AND use_count >= max_uses)",
                (now,),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def _is_valid(rec: LinkRecord, now: int) -> bool:
        if rec.revoked:
            return False
        if rec.expires_at > 0 and now > rec.expires_at:
            return False
        if rec.max_uses is not None and rec.use_count >= rec.max_uses:
            return False
        return True

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> LinkRecord:
        raw_options = row["options_json"] if "options_json" in row.keys() else None
        options = json.loads(raw_options) if raw_options else None
        mode = row["mode"] if "mode" in row.keys() and row["mode"] else "file"
        return LinkRecord(
            slug=row["slug"],
            target_url=row["target_url"],
            expires_at=row["expires_at"],
            revoked=row["revoked"],
            filename=row["filename"],
            max_uses=row["max_uses"],
            use_count=row["use_count"],
            created_at=row["created_at"],
            last_access_at=row["last_access_at"],
            mode=mode,
            options=options,
        )
