from __future__ import annotations

from pathlib import Path

from echofist.core.kiwi_client import KiwiReachabilityResult
from echofist.core.kiwi_sources import KiwiSourceRegistry


def test_block_unblock_and_expire(tmp_path: Path) -> None:
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        server = "sdr.example.com:8073"
        assert registry.is_blocked(server) is False

        registry.block_source(server, kind="invalid", reason="test", ttl_days=7)
        assert registry.is_blocked(server) is True
        assert registry.is_blocked(server, kind="invalid") is True

        n = registry.unblock_source(server, kind="invalid")
        assert n == 1
        assert registry.is_blocked(server) is False

        registry.block_source(server, kind="invalid", reason="test", ttl_days=7)
        cur = registry._conn.cursor()
        cur.execute(
            "UPDATE blocked_sources SET expires_ts=? WHERE server=? AND kind=?",
            (0, server, "invalid"),
        )
        registry._conn.commit()
        deleted = registry.expire_blocks()
        assert deleted >= 1
        assert registry.is_blocked(server) is False
    finally:
        registry.close()


def test_count_scan_days(tmp_path: Path) -> None:
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        server = "sdr.example.com:8073"
        ts1 = 1_700_000_000
        ts2 = ts1 + 86400 * 2
        cur = registry._conn.cursor()
        cur.execute(
            """
            INSERT INTO scan_history(
              ts, server, tcp_ok, latency_ms, http_status, kiwi_ts, error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (ts1, server, 1, 10.0, 200, None, None),
        )
        cur.execute(
            """
            INSERT INTO scan_history(
              ts, server, tcp_ok, latency_ms, http_status, kiwi_ts, error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (ts2, server, 1, 12.0, 200, None, None),
        )
        registry._conn.commit()

        assert registry.count_scan_days(server, lookback_days=3650) == 2
        assert registry.count_scan_days(server, lookback_days=1) in {0, 1}
    finally:
        registry.close()


def test_scan_runs_and_run_id(tmp_path: Path) -> None:
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        server = "sdr.example.com:8073"
        run_id = registry.start_scan_run(
            mode="test",
            concurrency=3,
            timeout_seconds=1.2,
            verify_http=True,
            candidate_source="unit_test",
            limit_n=1,
        )
        result = KiwiReachabilityResult(
            server=server,
            host="sdr.example.com",
            port=8073,
            tcp_ok=True,
            latency_ms=10.0,
            tcp_ms=10.0,
            http_status=200,
            http_ok=True,
            http_ms=5.0,
            kiwi_ts=None,
            total_ms=16.0,
            error_kind=None,
            error=None,
        )
        registry.record_scans([result], max_total=1000, run_id=run_id)
        registry.finish_scan_run(
            run_id,
            ok=1,
            fail=0,
            avg_latency_ms=10.0,
            prune_disabled=0,
            expired_blocks=0,
        )

        cur = registry._conn.cursor()
        row = cur.execute(
            "SELECT run_id FROM scan_history WHERE server=? ORDER BY id DESC LIMIT 1",
            (server,),
        ).fetchone()
        assert row is not None
        assert str(row[0]) == run_id

        row2 = cur.execute(
            "SELECT finished_ts, ok, fail FROM scan_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row2 is not None
        assert row2[0] is not None
        assert int(row2[1]) == 1
        assert int(row2[2]) == 0
    finally:
        registry.close()
