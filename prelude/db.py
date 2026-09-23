"""Local state DB — schema from migrations/001_init.sql. Never creates api_usage here."""
import os
import sqlite3

# repo-relative default (was an absolute path to the build box: a clean clone
# on another machine tried to mkdir it). Same file on the build box.
DB_PATH = os.environ.get(
    "PRELUDE_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "prelude.db"),
)
MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "migrations", "001_init.sql")


def connect(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for mig in (("migrations", "001_init.sql"), ("migrations", "002_balance_history.sql"),
                ("migrations", "003_sm_netflow.sql")):
        mp = os.path.join(_base, *mig)
        if os.path.exists(mp):
            with open(mp) as f:
                conn.executescript(f.read())
    conn.commit()
    # idempotent backfill for DBs created before a schema change:
    # snapshot_id was added 2026-09-22 (group/diff key; ts = display only).
    try:
        conn.execute("ALTER TABLE balance_snapshots ADD COLUMN snapshot_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_sid ON balance_snapshots(snapshot_id)")
        # pre-snapshot_id rows: one snapshot per existing pass ts
        conn.execute("""
            INSERT INTO snapshots (id, ts, wallets)
            SELECT seq, MIN(ts), COUNT(DISTINCT address)
            FROM (SELECT row_number() OVER (ORDER BY MIN(ts)) seq, ts
                  FROM balance_snapshots GROUP BY ts) GROUP BY seq
        """)
        conn.execute("""
            UPDATE balance_snapshots SET snapshot_id =
              (SELECT s.id FROM snapshots s WHERE s.ts = balance_snapshots.ts
               ORDER BY s.id LIMIT 1)
        """)
    except sqlite3.OperationalError:
        pass  # column already exists — fresh schema
    return conn
