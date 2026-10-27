import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
                    last_access_at INTEGER
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expires_at ON links(expires_at)")
            conn.commit()

    def create_link(
        self,
        slug: str,
        target_url: str,
        expires_at: int,
        filename: Optional[str],
        max_uses: Optional[int],
    ) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO links (slug, target_url, expires_at, filename, max_uses, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (slug, target_url, expires_at, filename, max_uses, now),
            )
            conn.commit()

    def get_link(self, slug: str) -> Optional[LinkRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
        return self._row_to_record(row) if row else None

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
            if rec.revoked:
                return None
            if rec.expires_at > 0 and now > rec.expires_at:
                return None
            if rec.max_uses is not None and rec.use_count >= rec.max_uses:
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
    def _row_to_record(row: sqlite3.Row) -> LinkRecord:
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
        )
