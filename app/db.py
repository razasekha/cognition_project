"""SQLite persistence layer.

Tables
------
sessions   – one row per Devin session created by the orchestrator
issues     – one row per unique finding produced by Agent 2 (the scanner)
"""

import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id      TEXT PRIMARY KEY,
                role            TEXT NOT NULL,          -- injector | scanner | fixer
                issue_id        INTEGER,                -- FK to issues (nullable)
                run_id          TEXT,                   -- correlation UUID
                status          TEXT NOT NULL DEFAULT 'new',
                status_detail   TEXT,
                devin_url       TEXT,
                pr_url          TEXT,
                acus_consumed   REAL DEFAULT 0,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issues (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint         TEXT UNIQUE NOT NULL,
                severity            TEXT NOT NULL,      -- low|medium|high|critical
                category            TEXT NOT NULL,
                file                TEXT,
                line                INTEGER,
                description         TEXT NOT NULL,
                recommendation      TEXT,
                source_session_id   TEXT,               -- Agent 2 session
                github_issue_url    TEXT,
                status              TEXT NOT NULL DEFAULT 'open',   -- open|fixing|pr_created|fixed|failed
                fix_session_id      TEXT,
                fix_pr_url          TEXT,
                created_at          INTEGER NOT NULL,
                updated_at          INTEGER NOT NULL
            );
            """
        )
    logger.info("Database initialised at %s", settings.db_path)


# ---------------------------------------------------------------------------
# Display helpers (human-friendly status/detail for sessions)
# ---------------------------------------------------------------------------

def _display_status(status: str, status_detail: Optional[str]) -> str:
    """Convert raw Devin status fields into a human-readable status label."""
    if status in ("exit",) and status_detail == "finished":
        return "Complete"
    if status == "running" and status_detail == "waiting_for_user":
        return "Complete"
    if status == "running" and status_detail == "working":
        return "Running"
    if status == "running" and status_detail == "waiting_for_approval":
        return "Running"
    if status in ("error", "suspended"):
        return "Failed"
    if status in ("new", "claimed", "resuming"):
        return "Starting"
    if status == "running":
        return "Running"
    return status.capitalize()


def _display_detail(
    role: str,
    status: str,
    status_detail: Optional[str],
    pr_url: Optional[str],
    issue_count: int,
) -> str:
    """Build a human-readable detail string for a session row."""
    disp = _display_status(status, status_detail)
    if role == "scanner":
        if issue_count > 0:
            return f"{issue_count} Issue{'s' if issue_count != 1 else ''} Created"
        if disp == "Complete":
            return "No issues found"
        return disp
    # injector / fixer
    if pr_url:
        return "PR Created"
    if disp == "Complete":
        return "Complete"
    return (status_detail or status).replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# Sessions helpers
# ---------------------------------------------------------------------------

def upsert_session(
    session_id: str,
    role: str,
    devin_url: str,
    run_id: str,
    issue_id: Optional[int] = None,
    status: str = "new",
    status_detail: Optional[str] = None,
    pr_url: Optional[str] = None,
    acus_consumed: float = 0.0,
) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, role, issue_id, run_id, status, status_detail,
                 devin_url, pr_url, acus_consumed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status        = excluded.status,
                status_detail = excluded.status_detail,
                pr_url        = excluded.pr_url,
                acus_consumed = excluded.acus_consumed,
                updated_at    = excluded.updated_at
            """,
            (
                session_id, role, issue_id, run_id, status, status_detail,
                devin_url, pr_url, acus_consumed, now, now,
            ),
        )


def update_session(
    session_id: str,
    status: str,
    status_detail: Optional[str] = None,
    pr_url: Optional[str] = None,
    acus_consumed: float = 0.0,
) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE sessions SET
                status        = ?,
                status_detail = ?,
                pr_url        = COALESCE(?, pr_url),
                acus_consumed = ?,
                updated_at    = ?
            WHERE session_id = ?
            """,
            (status, status_detail, pr_url, acus_consumed, now, session_id),
        )


def get_all_sessions() -> list[dict[str, Any]]:
    """Return all sessions enriched with display_status and display_detail."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Count issues created by this session (scanner role)
            issue_count = conn.execute(
                "SELECT COUNT(*) FROM issues WHERE source_session_id = ?",
                (d["session_id"],),
            ).fetchone()[0]
            d["display_status"] = _display_status(d["status"], d.get("status_detail"))
            d["display_detail"] = _display_detail(
                d["role"], d["status"], d.get("status_detail"), d.get("pr_url"), issue_count
            )
            result.append(d)
    return result


# ---------------------------------------------------------------------------
# Issues helpers
# ---------------------------------------------------------------------------

def make_fingerprint(file: str, category: str, line: Optional[int]) -> str:
    raw = f"{file or ''}|{category or ''}|{line or 0}"
    return hashlib.sha1(raw.encode()).hexdigest()


def insert_issue_if_new(
    severity: str,
    category: str,
    file: str,
    line: Optional[int],
    description: str,
    recommendation: Optional[str],
    source_session_id: str,
) -> Optional[int]:
    """Insert a new issue. Returns the new row id, or None if already exists."""
    fingerprint = make_fingerprint(file, category, line)
    now = int(time.time())
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM issues WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            return None
        cursor = conn.execute(
            """
            INSERT INTO issues
                (fingerprint, severity, category, file, line,
                 description, recommendation, source_session_id,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                fingerprint, severity, category, file, line,
                description, recommendation, source_session_id,
                now, now,
            ),
        )
        return cursor.lastrowid


def set_issue_github_url(issue_id: int, github_issue_url: str) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE issues SET github_issue_url = ?, updated_at = ? WHERE id = ?",
            (github_issue_url, now, issue_id),
        )


def set_issue_fixing(issue_id: int, fix_session_id: str) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE issues SET status = 'fixing', fix_session_id = ?, updated_at = ? WHERE id = ?",
            (fix_session_id, now, issue_id),
        )


def set_issue_pr_created(issue_id: int, fix_pr_url: str) -> None:
    """Mark issue as having a PR open but not yet merged."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE issues SET status = 'pr_created', fix_pr_url = ?, updated_at = ? WHERE id = ?",
            (fix_pr_url, now, issue_id),
        )


def set_issue_fixed(issue_id: int, fix_pr_url: Optional[str] = None) -> None:
    """Mark issue as fixed (PR merged). Optionally update the PR URL."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE issues SET
                status     = 'fixed',
                fix_pr_url = COALESCE(?, fix_pr_url),
                updated_at = ?
            WHERE id = ?
            """,
            (fix_pr_url, now, issue_id),
        )


def set_issue_failed(issue_id: int) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE issues SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, issue_id),
        )


def get_issue(issue_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def get_all_issues() -> list[dict[str, Any]]:
    """Return all issues sorted by Time Identified (newest first)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM issues ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_issues() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM issues WHERE status = 'open' ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_issues_by_status(status: str) -> list[dict[str, Any]]:
    """Return all issues with the given status, ordered oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM issues WHERE status = ? ORDER BY created_at ASC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Metrics aggregation (ACUs removed)
# ---------------------------------------------------------------------------

def get_metrics() -> dict[str, Any]:
    with get_conn() as conn:
        total_issues = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        fixed = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status = 'fixed'"
        ).fetchone()[0]
        pr_created_cnt = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status = 'pr_created'"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status = 'failed'"
        ).fetchone()[0]
        open_cnt = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status = 'open'"
        ).fetchone()[0]
        fixing_cnt = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status = 'fixing'"
        ).fetchone()[0]

        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        # "successful" = exit/finished OR soft-done waiting_for_user
        success_sessions = conn.execute(
            """SELECT COUNT(*) FROM sessions
               WHERE (status = 'exit' AND status_detail = 'finished')
                  OR (status = 'running' AND status_detail = 'waiting_for_user')"""
        ).fetchone()[0]
        active_sessions = conn.execute(
            """SELECT COUNT(*) FROM sessions
               WHERE status NOT IN ('exit','error','suspended')
                 AND status_detail NOT IN ('finished','waiting_for_user')"""
        ).fetchone()[0]

        # MTTR: average seconds from issue created_at to session completed for fixed issues
        mttr_row = conn.execute(
            """
            SELECT AVG(s.updated_at - i.created_at)
            FROM issues i
            JOIN sessions s ON s.session_id = i.fix_session_id
            WHERE i.status = 'fixed'
            """
        ).fetchone()
        mttr_seconds = mttr_row[0] if mttr_row and mttr_row[0] else 0

    success_rate = (success_sessions / total_sessions * 100) if total_sessions else 0
    fix_rate = ((fixed + pr_created_cnt) / total_issues * 100) if total_issues else 0

    return {
        "issues": {
            "total": total_issues,
            "open": open_cnt,
            "fixing": fixing_cnt,
            "pr_created": pr_created_cnt,
            "fixed": fixed,
            "failed": failed,
            "fix_rate_pct": round(fix_rate, 1),
        },
        "sessions": {
            "total": total_sessions,
            "active": active_sessions,
            "successful": success_sessions,
            "success_rate_pct": round(success_rate, 1),
        },
        "mttr_seconds": round(mttr_seconds),
    }
