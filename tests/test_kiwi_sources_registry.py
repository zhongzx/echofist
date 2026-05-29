from __future__ import annotations

import time
from pathlib import Path

from echofist.core import kiwi_sources as ks
from echofist.core.kiwi_client import KiwiReachabilityResult
from echofist.core.kiwi_source_service import KiwiSourceService
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
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
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


def test_sources_schedule_columns_exist(tmp_path: Path) -> None:
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        cur = registry._conn.cursor()
        cols = [str(r[1]) for r in cur.execute("PRAGMA table_info(sources)").fetchall()]
        assert "cooldown_until_ts" in cols
        assert "next_probe_ts" in cols
        assert "backoff_level" in cols
    finally:
        registry.close()


def test_backoff_and_due_logic(monkeypatch, tmp_path: Path) -> None:
    ts0 = 1_700_000_000
    monkeypatch.setattr(ks, "_now_ts", lambda: ts0)
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        server = "sdr.example.com:8073"
        fail = KiwiReachabilityResult(
            server=server,
            host="sdr.example.com",
            port=8073,
            tcp_ok=False,
            latency_ms=None,
            tcp_ms=100.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
            total_ms=110.0,
            error_kind="timeout",
            error="timeout",
        )
        registry.record_scans([fail], max_total=1000, run_id=None)
        cooldown, next_probe, backoff = registry.get_schedule(server)
        assert backoff == 1
        assert next_probe is not None and next_probe > ts0
        assert cooldown is not None and cooldown == next_probe
        assert registry.is_due(server, now_ts=ts0) is False
        assert registry.is_due(server, now_ts=next_probe) is True

        monkeypatch.setattr(ks, "_now_ts", lambda: int(next_probe))
        ok = KiwiReachabilityResult(
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
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
            total_ms=16.0,
            error_kind=None,
            error=None,
        )
        registry.record_scans([ok], max_total=1000, run_id=None)
        cooldown2, next_probe2, backoff2 = registry.get_schedule(server)
        assert backoff2 == 0
        assert cooldown2 is None
        assert next_probe2 is not None and next_probe2 > int(next_probe)
    finally:
        registry.close()


def test_daily_probe_budget(tmp_path: Path) -> None:
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        allowed, day, used, cap = registry.reserve_daily_probe_budget(10, cap=5)
        assert allowed == 5
        assert cap == 5
        assert used == 5
        allowed2, day2, used2, cap2 = registry.reserve_daily_probe_budget(3, cap=5)
        assert day2 == day
        assert cap2 == 5
        assert allowed2 == 0
        assert used2 == 5
    finally:
        registry.close()


def test_record_audio_health(tmp_path: Path, monkeypatch) -> None:
    ts0 = 1_700_000_000
    monkeypatch.setattr(ks, "_now_ts", lambda: ts0)
    registry = KiwiSourceRegistry(path=tmp_path / "kiwi.sqlite3")
    try:
        server = "sdr.example.com:8073"
        registry.record_audio_health(server=server, ok=True, max_total=1000)
        items = registry.list_sources(limit=10, enabled_only=False)
        by_server = {i.server: i for i in items}
        assert by_server[server].last_audio_ok_ts == ts0
        assert by_server[server].audio_dropouts == 0

        monkeypatch.setattr(ks, "_now_ts", lambda: ts0 + 10)
        registry.record_audio_health(server=server, ok=False, max_total=1000)
        items2 = registry.list_sources(limit=10, enabled_only=False)
        by_server2 = {i.server: i for i in items2}
        assert by_server2[server].audio_dropouts == 1
    finally:
        registry.close()


def test_pick_best_ignores_untrusted_users_max(tmp_path: Path, monkeypatch) -> None:
    now_ts = int(time.time())
    monkeypatch.setattr(ks, "_now_ts", lambda: now_ts)
    db_path = tmp_path / "kiwi.sqlite3"
    registry = KiwiSourceRegistry(path=db_path)
    try:
        untrusted_full = KiwiReachabilityResult(
            server="untrusted.example.com:8073",
            host="untrusted.example.com",
            port=8073,
            tcp_ok=True,
            latency_ms=10.0,
            tcp_ms=10.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=True,
            status_ms=5.0,
            users=50,
            users_max=50,
            total_ms=20.0,
            error_kind=None,
            error=None,
        )
        trusted_full = KiwiReachabilityResult(
            server="trusted.example.com:8073",
            host="trusted.example.com",
            port=8073,
            tcp_ok=True,
            latency_ms=10.0,
            tcp_ms=10.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=True,
            status_ms=5.0,
            users=4,
            users_max=4,
            total_ms=20.0,
            error_kind=None,
            error=None,
        )
        registry.record_scans(
            [untrusted_full, trusted_full],
            max_total=1000,
            run_id=None,
        )
    finally:
        registry.close()

    service = KiwiSourceService(registry_path=db_path)
    try:
        picked = service.pick_best(target=50, include_disabled=False)
        servers = {p.server for p in picked}
        assert "untrusted.example.com:8073" in servers
        assert "trusted.example.com:8073" not in servers
    finally:
        service.close()


def test_rollup_scan_daily_trusted_users_max(tmp_path: Path, monkeypatch) -> None:
    ts_day1 = 1_700_000_000
    ts_day2 = ts_day1 + 86400
    db_path = tmp_path / "kiwi.sqlite3"
    registry = KiwiSourceRegistry(path=db_path)
    try:
        monkeypatch.setattr(ks, "_now_ts", lambda: ts_day1)
        day1_ok = KiwiReachabilityResult(
            server="a.example.com:8073",
            host="a.example.com",
            port=8073,
            tcp_ok=True,
            latency_ms=100.0,
            tcp_ms=100.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=True,
            status_ms=5.0,
            users=1,
            users_max=4,
            total_ms=120.0,
            error_kind=None,
            error=None,
        )
        day1_fail = KiwiReachabilityResult(
            server="a.example.com:8073",
            host="a.example.com",
            port=8073,
            tcp_ok=False,
            latency_ms=None,
            tcp_ms=200.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
            total_ms=210.0,
            error_kind="timeout",
            error="timeout",
        )
        registry.record_scans([day1_ok, day1_fail], max_total=1000, run_id=None)

        monkeypatch.setattr(ks, "_now_ts", lambda: ts_day2)
        day2_untrusted = KiwiReachabilityResult(
            server="a.example.com:8073",
            host="a.example.com",
            port=8073,
            tcp_ok=True,
            latency_ms=150.0,
            tcp_ms=150.0,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=True,
            status_ms=5.0,
            users=50,
            users_max=50,
            total_ms=180.0,
            error_kind=None,
            error=None,
        )
        registry.record_scans([day2_untrusted], max_total=1000, run_id=None)

        monkeypatch.setattr(ks, "_now_ts", lambda: ts_day2)
        n = registry.rollup_scan_daily(lookback_days=3650, max_trusted_users_max=8)
        assert n == 2

        rows = registry.list_scan_daily("a.example.com:8073", limit=10)
        assert len(rows) == 2
        by_day = {r.day: r for r in rows}

        day1_key = time.strftime("%Y-%m-%d", time.gmtime(ts_day1))
        day2_key = time.strftime("%Y-%m-%d", time.gmtime(ts_day2))
        d1 = by_day[day1_key]
        assert d1.scans == 2
        assert d1.tcp_ok == 1
        assert d1.tcp_fail == 1
        assert d1.ok_latency_n == 1
        assert d1.ok_latency_sum_ms == 100.0
        assert d1.status_samples == 1
        assert d1.status_samples_trusted == 1
        assert d1.full_trusted == 0
        assert d1.users_sum_trusted == 1
        assert d1.users_max_sum_trusted == 4

        d2 = by_day[day2_key]
        assert d2.scans == 1
        assert d2.tcp_ok == 1
        assert d2.tcp_fail == 0
        assert d2.status_samples == 1
        assert d2.status_samples_trusted == 0
        assert d2.full_trusted == 0
        assert d2.users_sum_trusted == 0
        assert d2.users_max_sum_trusted == 0
    finally:
        registry.close()
