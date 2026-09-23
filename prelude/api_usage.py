"""Shared-DB metering. Every Nansen call lands in api_usage (shared remi DB).

Caller names are gate-scoped: 'prelude' (poller), 'prelude_backtest',
'prelude_pin' — separate gates, same service='nansen' evidence query.
"""
import os
import sqlite3
from datetime import datetime, timezone

SHARED_DB = os.environ.get(
    "PRELUDE_USAGE_DB",
    "/home/proxmox/remi-intelligence/remi_intelligence.db",
)


def _connect():
    db = SHARED_DB
    if not os.path.exists(db):
        # local fallback — same shape, only when the shared DB is absent
        db = os.path.join(os.environ.get("PRELUDE_DB_DIR", os.getcwd()), "prelude_usage_fallback.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at TEXT NOT NULL, service TEXT NOT NULL,
                endpoint TEXT NOT NULL, caller TEXT NOT NULL,
                http_status INTEGER, duration_ms INTEGER,
                units REAL, unit_kind TEXT, context TEXT)"""
        )
        conn.commit()
        return conn
    return sqlite3.connect(db)


def log_api_call(service: str, endpoint: str, caller: str, http_status: int | None = None,
                 duration_ms: int | None = None, units: float = 1.0, unit_kind: str = "calls",
                 context: str | None = None):
    if not caller:
        raise ValueError("caller is REQUIRED — no empty values (shared DB contract)")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO api_usage (called_at, service, endpoint, caller, http_status,"
            " duration_ms, units, unit_kind, context)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat().replace("+00:00", ""),
             service, endpoint, caller, http_status, duration_ms, units, unit_kind, context),
        )
        conn.commit()
    finally:
        conn.close()


def usage_counts(caller="prelude", since_days=7):
    """(today_calls, window_calls) for the budget gate."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT SUM(strftime('%Y-%m-%d', called_at) = date('now')), COUNT(*)"
            " FROM api_usage WHERE service='nansen' AND caller=?"
            " AND called_at >= datetime('now', ?)",
            (caller, f"-{since_days} days"),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
    finally:
        conn.close()


def nansen_total_calls(since="2026-09-14"):
    """Buildathon quota evidence count."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE service='nansen' AND called_at >= ?",
            (since,),
        ).fetchone()[0]
    finally:
        conn.close()
