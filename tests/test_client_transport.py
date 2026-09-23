"""Transport errors (timeouts) — no network. Regression for snapshot_id=6
(2026-09-23 07:00Z): one ReadTimeout escaped _post and aborted the whole pass.
A timeout must retry once, then raise RuntimeError (the poller's dead-letter
type), be metered each attempt, and never deadlock on the global flock."""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prelude import api_usage, nansen_client as nc  # noqa: E402


def _client(tmp_path, monkeypatch, seq):
    monkeypatch.setattr(api_usage, "SHARED_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("PRELUDE_DB_DIR", str(tmp_path))   # metering fallback DB
    monkeypatch.setattr(nc, "_LOCK_PATH", str(tmp_path / "q.lock"))
    monkeypatch.setattr(nc, "_STATE_PATH", str(tmp_path / "q.lock.state"))
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    it = iter(seq)

    def handler(req):
        if next(it) == "T":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"data": []},
                              headers={"x-nansen-credits-remaining": "105000"})
    c = nc.NansenClient(key="k")
    c.client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def _statuses(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "prelude_usage_fallback.db"))
    return [r[0] for r in conn.execute("SELECT http_status FROM api_usage ORDER BY id")]


def test_timeout_then_ok_retries(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, ["T", "OK"])
    assert c.current_balance("A", "solana") == {"data": []}
    assert _statuses(tmp_path) == [None, 200]


def test_timeout_twice_raises_dead_letter_type(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, ["T", "T"])
    try:
        c.current_balance("A", "solana")
    except RuntimeError as e:
        assert "ReadTimeout" in str(e)
    else:
        raise AssertionError("timeout x2 must raise RuntimeError")
    assert _statuses(tmp_path) == [None, None]
