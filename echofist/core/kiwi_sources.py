"""KiwiSDR 源注册表（SQLite）。"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiohttp

from echofist.config import ConfigManager, load_config
from echofist.core.kiwi_client import KiwiReachabilityResult, parse_kiwi_server_address


def extract_servers_from_text(
    text: str,
    *,
    default_port: int = 8073,
) -> tuple[list[str], int]:
    raw = str(text)
    pattern = re.compile(
        r"(?:(\d{1,3}(?:\.\d{1,3}){3})|([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+))"
        r"(?:\s*:\s*(\d{2,5}))?",
        flags=re.IGNORECASE,
    )
    tokens = pattern.findall(raw)
    servers: list[str] = []
    invalid = 0

    for ip, domain, port_str in tokens:
        host = ip or domain
        if not host:
            continue
        port = int(port_str) if port_str else int(default_port)
        if not (8000 <= port <= 9000):
            invalid += 1
            continue
        server = f"{host}:{port}"
        try:
            parse_kiwi_server_address(server)
        except Exception:
            invalid += 1
            continue
        if server not in servers:
            servers.append(server)

    return servers, int(invalid)


def extract_servers_from_lines(
    lines: Iterable[str],
    *,
    default_port: int = 8073,
    max_servers: int = 5000,
) -> tuple[list[str], int, int, bool]:
    servers: list[str] = []
    invalid = 0
    lines_read = 0
    truncated = False

    pattern = re.compile(
        r"(?:(\d{1,3}(?:\.\d{1,3}){3})|([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+))"
        r"(?:\s*:\s*(\d{2,5}))?",
        flags=re.IGNORECASE,
    )

    for line in lines:
        lines_read += 1
        for ip, domain, port_str in pattern.findall(str(line)):
            host = ip or domain
            if not host:
                continue
            port = int(port_str) if port_str else int(default_port)
            if not (8000 <= port <= 9000):
                invalid += 1
                continue
            server = f"{host}:{port}"
            try:
                parse_kiwi_server_address(server)
            except Exception:
                invalid += 1
                continue
            if server not in servers:
                servers.append(server)
                if len(servers) >= int(max_servers):
                    truncated = True
                    return servers, int(invalid), int(lines_read), truncated

    return servers, int(invalid), int(lines_read), truncated


@dataclass(frozen=True, slots=True)
class KiwiSourceSummary:
    server: str
    enabled: bool
    score: float
    successes: int
    failures: int
    consecutive_failures: int
    last_latency_ms: float | None
    last_seen_ts: int | None
    last_users: int | None
    last_users_max: int | None
    last_status_ts: int | None
    last_audio_ok_ts: int | None
    audio_dropouts: int


@dataclass(frozen=True, slots=True)
class KiwiScanHistoryRow:
    ts: int
    tcp_ok: bool
    latency_ms: float | None
    tcp_ms: float | None
    http_status: int | None
    http_ok: bool | None
    http_ms: float | None
    kiwi_ts: int | None
    total_ms: float | None
    error_kind: str | None
    run_id: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class KiwiBlockedSourceRow:
    server: str
    kind: str
    first_ts: int
    last_ts: int
    expires_ts: int | None
    hits: int
    reason: str | None


def default_registry_path() -> Path:
    config_file = ConfigManager.get_default_config_file()
    return config_file.parent / "kiwi_sources.sqlite3"


def _now_ts() -> int:
    return int(time.time())


def compute_source_score(
    *,
    successes: int,
    failures: int,
    latency_ms: float | None,
    monitor_reconnects: int = 0,
    monitor_switches: int = 0,
) -> float:
    s = max(0, int(successes))
    f = max(0, int(failures))
    base = (s + 1.0) / (s + f + 2.0)
    latency = float(latency_ms) if latency_ms is not None else 2000.0
    latency = max(1.0, latency)
    latency_factor = 1.0 / (1.0 + (latency / 500.0))
    r = max(0, int(monitor_reconnects))
    w = max(0, int(monitor_switches))
    penalty = 1.0 / (1.0 + 0.10 * math.log1p(w) + 0.05 * math.log1p(r))
    return float(base * latency_factor * penalty)


class KiwiSourceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._policy = load_config().kiwi_sources
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
              server TEXT PRIMARY KEY,
              host TEXT NOT NULL,
              port INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              first_seen_ts INTEGER NOT NULL,
              last_seen_ts INTEGER,
              last_ok_ts INTEGER,
              last_fail_ts INTEGER,
              successes INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_latency_ms REAL,
              last_http_status INTEGER,
              last_kiwi_ts INTEGER,
              last_error TEXT,
              score REAL NOT NULL DEFAULT 0,
              monitor_reconnects INTEGER NOT NULL DEFAULT 0,
              monitor_switches INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._ensure_sources_columns(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              server TEXT NOT NULL,
              event_type TEXT NOT NULL,
              from_server TEXT,
              to_server TEXT,
              detail TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT,
              ts INTEGER NOT NULL,
              server TEXT NOT NULL,
              tcp_ok INTEGER NOT NULL,
              latency_ms REAL,
              tcp_ms REAL,
              http_status INTEGER,
              http_ok INTEGER,
              http_ms REAL,
              kiwi_ts INTEGER,
              total_ms REAL,
              error_kind TEXT,
              error TEXT
            )
            """
        )
        self._ensure_scan_history_columns(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
              run_id TEXT PRIMARY KEY,
              started_ts INTEGER NOT NULL,
              finished_ts INTEGER,
              mode TEXT NOT NULL,
              concurrency INTEGER NOT NULL,
              timeout_seconds REAL NOT NULL,
              verify_http INTEGER NOT NULL,
              candidate_source TEXT NOT NULL,
              limit_n INTEGER NOT NULL,
              registry_path TEXT NOT NULL,
              ok INTEGER,
              fail INTEGER,
              avg_latency_ms REAL,
              prune_disabled INTEGER,
              expired_blocks INTEGER,
              note TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS probe_budget_day (
              day TEXT PRIMARY KEY,
              used INTEGER NOT NULL,
              cap INTEGER NOT NULL,
              updated_ts INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_history_server_ts
            ON scan_history(server, ts)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_monitor_events_server_ts
            ON monitor_events(server, ts)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_started_ts
            ON scan_runs(started_ts)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_probe_budget_updated_ts
            ON probe_budget_day(updated_ts)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_sources (
              server TEXT NOT NULL,
              kind TEXT NOT NULL,
              first_ts INTEGER NOT NULL,
              last_ts INTEGER NOT NULL,
              expires_ts INTEGER,
              hits INTEGER NOT NULL DEFAULT 1,
              reason TEXT,
              PRIMARY KEY(server, kind)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_blocked_kind_expires
            ON blocked_sources(kind, expires_ts)
            """
        )
        self._conn.commit()

    def _ensure_sources_columns(self, cur: sqlite3.Cursor) -> None:
        rows = cur.execute("PRAGMA table_info(sources)").fetchall()
        existing = {str(r[1]) for r in rows}
        additions = [
            ("monitor_reconnects", "monitor_reconnects INTEGER NOT NULL DEFAULT 0"),
            ("monitor_switches", "monitor_switches INTEGER NOT NULL DEFAULT 0"),
            ("cooldown_until_ts", "cooldown_until_ts INTEGER"),
            ("next_probe_ts", "next_probe_ts INTEGER"),
            ("backoff_level", "backoff_level INTEGER NOT NULL DEFAULT 0"),
            ("last_users", "last_users INTEGER"),
            ("last_users_max", "last_users_max INTEGER"),
            ("last_status_ts", "last_status_ts INTEGER"),
            ("last_audio_ok_ts", "last_audio_ok_ts INTEGER"),
            ("last_audio_fail_ts", "last_audio_fail_ts INTEGER"),
            ("audio_dropouts", "audio_dropouts INTEGER NOT NULL DEFAULT 0"),
        ]
        for name, ddl in additions:
            if name in existing:
                continue
            cur.execute(f"ALTER TABLE sources ADD COLUMN {ddl}")

    def _ensure_scan_history_columns(self, cur: sqlite3.Cursor) -> None:
        rows = cur.execute("PRAGMA table_info(scan_history)").fetchall()
        existing = {str(r[1]) for r in rows}
        additions = [
            ("run_id", "run_id TEXT"),
            ("tcp_ms", "tcp_ms REAL"),
            ("http_ok", "http_ok INTEGER"),
            ("http_ms", "http_ms REAL"),
            ("total_ms", "total_ms REAL"),
            ("error_kind", "error_kind TEXT"),
        ]
        for name, ddl in additions:
            if name in existing:
                continue
            cur.execute(f"ALTER TABLE scan_history ADD COLUMN {ddl}")

    def start_scan_run(
        self,
        *,
        mode: str,
        concurrency: int,
        timeout_seconds: float,
        verify_http: bool,
        candidate_source: str,
        limit_n: int,
        note: str | None = None,
    ) -> str:
        run_id = uuid4().hex
        ts = _now_ts()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO scan_runs(
              run_id,
              started_ts,
              mode,
              concurrency,
              timeout_seconds,
              verify_http,
              candidate_source,
              limit_n,
              registry_path,
              note
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ts,
                str(mode),
                int(concurrency),
                float(timeout_seconds),
                1 if bool(verify_http) else 0,
                str(candidate_source),
                int(limit_n),
                str(self._path),
                note,
            ),
        )
        self._conn.commit()
        return run_id

    def finish_scan_run(
        self,
        run_id: str,
        *,
        ok: int,
        fail: int,
        avg_latency_ms: float | None,
        prune_disabled: int,
        expired_blocks: int,
    ) -> None:
        text = str(run_id).strip()
        if not text:
            return
        ts = _now_ts()
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE scan_runs SET
              finished_ts=?,
              ok=?,
              fail=?,
              avg_latency_ms=?,
              prune_disabled=?,
              expired_blocks=?
            WHERE run_id=?
            """,
            (
                ts,
                int(ok),
                int(fail),
                float(avg_latency_ms) if avg_latency_ms is not None else None,
                int(prune_disabled),
                int(expired_blocks),
                text,
            ),
        )
        self._conn.commit()

    def _expire_blocks_cur(self, cur: sqlite3.Cursor, *, now_ts: int) -> int:
        return int(
            cur.execute(
                """
                DELETE FROM blocked_sources
                WHERE expires_ts IS NOT NULL AND expires_ts <= ?
                """,
                (int(now_ts),),
            ).rowcount
        )

    def expire_blocks(self) -> int:
        ts = _now_ts()
        cur = self._conn.cursor()
        deleted = self._expire_blocks_cur(cur, now_ts=ts)
        self._conn.commit()
        return int(deleted)

    def block_source(
        self,
        server: str,
        *,
        kind: str,
        reason: str | None = None,
        ttl_days: int | None = None,
        expires_ts: int | None = None,
    ) -> None:
        text = str(server).strip()
        if not text:
            return
        try:
            parse_kiwi_server_address(text)
        except Exception:
            return

        ts = _now_ts()
        ttl = None if ttl_days is None else max(0, int(ttl_days))
        until = int(expires_ts) if expires_ts is not None else None
        if until is None and ttl is not None and ttl > 0:
            until = ts + ttl * 86400

        cur = self._conn.cursor()
        self._expire_blocks_cur(cur, now_ts=ts)
        cur.execute(
            """
            INSERT INTO blocked_sources(
              server,
              kind,
              first_ts,
              last_ts,
              expires_ts,
              hits,
              reason
            )
            VALUES(?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(server, kind) DO UPDATE SET
              last_ts=excluded.last_ts,
              expires_ts=COALESCE(excluded.expires_ts, blocked_sources.expires_ts),
              hits=blocked_sources.hits + 1,
              reason=COALESCE(excluded.reason, blocked_sources.reason)
            """,
            (text, str(kind), ts, ts, until, reason),
        )
        if str(kind) == "blacklist":
            cur.execute(
                "UPDATE sources SET enabled=0 WHERE server=?",
                (text,),
            )
        self._conn.commit()

    def unblock_source(self, server: str, *, kind: str | None = None) -> int:
        text = str(server).strip()
        if not text:
            return 0
        cur = self._conn.cursor()
        if kind is None:
            n = cur.execute(
                "DELETE FROM blocked_sources WHERE server=?",
                (text,),
            ).rowcount
        else:
            n = cur.execute(
                "DELETE FROM blocked_sources WHERE server=? AND kind=?",
                (text, str(kind)),
            ).rowcount
        self._conn.commit()
        return int(n)

    def is_blocked(self, server: str, *, kind: str | None = None) -> bool:
        text = str(server).strip()
        if not text:
            return False
        ts = _now_ts()
        cur = self._conn.cursor()
        if kind is None:
            row = cur.execute(
                """
                SELECT 1 FROM blocked_sources
                WHERE server=?
                  AND (expires_ts IS NULL OR expires_ts > ?)
                LIMIT 1
                """,
                (text, int(ts)),
            ).fetchone()
        else:
            row = cur.execute(
                """
                SELECT 1 FROM blocked_sources
                WHERE server=? AND kind=?
                  AND (expires_ts IS NULL OR expires_ts > ?)
                LIMIT 1
                """,
                (text, str(kind), int(ts)),
            ).fetchone()
        if row is None:
            return False
        return True

    def list_blocked(
        self,
        *,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[KiwiBlockedSourceRow]:
        lim = max(1, int(limit))
        ts = _now_ts()
        cur = self._conn.cursor()
        if kind is None:
            rows = cur.execute(
                """
                SELECT server, kind, first_ts, last_ts, expires_ts, hits, reason
                FROM blocked_sources
                WHERE expires_ts IS NULL OR expires_ts > ?
                ORDER BY kind ASC, last_ts DESC, server ASC
                LIMIT ?
                """,
                (int(ts), lim),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT server, kind, first_ts, last_ts, expires_ts, hits, reason
                FROM blocked_sources
                WHERE kind=?
                  AND (expires_ts IS NULL OR expires_ts > ?)
                ORDER BY last_ts DESC, server ASC
                LIMIT ?
                """,
                (str(kind), int(ts), lim),
            ).fetchall()
        out: list[KiwiBlockedSourceRow] = []
        for r in rows:
            out.append(
                KiwiBlockedSourceRow(
                    server=str(r["server"]),
                    kind=str(r["kind"]),
                    first_ts=int(r["first_ts"]),
                    last_ts=int(r["last_ts"]),
                    expires_ts=(
                        int(r["expires_ts"]) if r["expires_ts"] is not None else None
                    ),
                    hits=int(r["hits"]),
                    reason=str(r["reason"]) if r["reason"] is not None else None,
                )
            )
        return out

    def count_scan_days(
        self,
        server: str,
        *,
        lookback_days: int = 90,
    ) -> int:
        text = str(server).strip()
        if not text:
            return 0
        days = max(1, int(lookback_days))
        cutoff = _now_ts() - days * 86400
        cur = self._conn.cursor()
        row = cur.execute(
            """
            SELECT COUNT(DISTINCT date(ts, 'unixepoch')) AS n
            FROM scan_history
            WHERE server=? AND ts >= ?
            """,
            (text, int(cutoff)),
        ).fetchone()
        return int(row["n"]) if row is not None and row["n"] is not None else 0

    def get_last_seen_ts(self, server: str) -> int | None:
        text = str(server).strip()
        if not text:
            return None
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT last_seen_ts FROM sources WHERE server=?",
            (text,),
        ).fetchone()
        if row is None or row["last_seen_ts"] is None:
            return None
        return int(row["last_seen_ts"])

    def get_schedule(
        self,
        server: str,
    ) -> tuple[int | None, int | None, int]:
        text = str(server).strip()
        if not text:
            return None, None, 0
        cur = self._conn.cursor()
        row = cur.execute(
            """
            SELECT cooldown_until_ts, next_probe_ts, backoff_level
            FROM sources
            WHERE server=?
            """,
            (text,),
        ).fetchone()
        if row is None:
            return None, None, 0
        cooldown = int(row["cooldown_until_ts"]) if row["cooldown_until_ts"] else None
        next_probe = int(row["next_probe_ts"]) if row["next_probe_ts"] else None
        backoff = int(row["backoff_level"] or 0)
        return cooldown, next_probe, backoff

    def is_due(self, server: str, *, now_ts: int | None = None) -> bool:
        now = _now_ts() if now_ts is None else int(now_ts)
        cooldown, next_probe, _backoff = self.get_schedule(server)
        if cooldown is not None and cooldown > now:
            return False
        if next_probe is not None and next_probe > now:
            return False
        return True

    @staticmethod
    def _day_key(ts: int) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))

    def get_daily_probe_budget(
        self,
        *,
        now_ts: int | None = None,
        cap: int | None = None,
    ) -> tuple[str, int, int]:
        now = _now_ts() if now_ts is None else int(now_ts)
        day = self._day_key(now)
        cap_value = (
            int(self._policy.daily_probe_cap) if cap is None else max(0, int(cap))
        )
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT used, cap FROM probe_budget_day WHERE day=?",
            (day,),
        ).fetchone()
        if row is None:
            return day, 0, cap_value
        used = int(row["used"])
        current_cap = int(row["cap"])
        return day, used, cap_value if cap is not None else current_cap

    def reserve_daily_probe_budget(
        self,
        requested: int,
        *,
        now_ts: int | None = None,
        cap: int | None = None,
    ) -> tuple[int, str, int, int]:
        now = _now_ts() if now_ts is None else int(now_ts)
        day = self._day_key(now)
        req = max(0, int(requested))
        cap_value = (
            int(self._policy.daily_probe_cap) if cap is None else max(0, int(cap))
        )
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO probe_budget_day(day, used, cap, updated_ts)
            VALUES(?, 0, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              cap=excluded.cap,
              updated_ts=excluded.updated_ts
            """,
            (day, cap_value, now),
        )
        row = cur.execute(
            "SELECT used, cap FROM probe_budget_day WHERE day=?",
            (day,),
        ).fetchone()
        used = int(row["used"]) if row is not None else 0
        cap_db = int(row["cap"]) if row is not None else cap_value
        remaining = max(0, cap_db - used)
        allowed = min(req, remaining)
        if allowed > 0:
            cur.execute(
                """
                UPDATE probe_budget_day
                SET used=?, updated_ts=?
                WHERE day=?
                """,
                (used + allowed, now, day),
            )
            self._conn.commit()
            return allowed, day, used + allowed, cap_db
        self._conn.commit()
        return 0, day, used, cap_db

    def _stable_jitter_seconds(
        self,
        *,
        server: str,
        seed_ts: int,
        jitter_max_seconds: int,
    ) -> int:
        lim = max(0, int(jitter_max_seconds))
        if lim <= 0:
            return 0
        raw = hashlib.sha256(f"{server}:{seed_ts}".encode()).digest()
        val = int.from_bytes(raw[:4], "big", signed=False)
        span = lim * 2 + 1
        return int(val % span) - lim

    def _schedule_after_probe(
        self,
        *,
        server: str,
        now_ts: int,
        success: bool,
        prior_backoff_level: int,
    ) -> tuple[int | None, int | None, int]:
        min_interval_days = max(0, int(self._policy.scan_min_interval_days))
        base_success = (
            max(3600, min_interval_days * 86400) if min_interval_days > 0 else 3600
        )
        max_days = max(1, int(self._policy.invalid_ttl_days))
        if success:
            next_backoff = max(0, int(prior_backoff_level) - 1)
            jitter = self._stable_jitter_seconds(
                server=server,
                seed_ts=now_ts,
                jitter_max_seconds=min(3600, base_success // 10),
            )
            next_probe = now_ts + base_success + jitter
            return None, int(next_probe), next_backoff

        next_backoff = min(10, max(0, int(prior_backoff_level)) + 1)
        base_fail = (
            max(3600, min_interval_days * 86400) if min_interval_days > 0 else 3600
        )
        delay = min(max_days * 86400, base_fail * (2**next_backoff))
        jitter = self._stable_jitter_seconds(
            server=server,
            seed_ts=now_ts,
            jitter_max_seconds=min(3600, delay // 10),
        )
        until = now_ts + delay + jitter
        return int(until), int(until), next_backoff

    def _get_source_counters(
        self,
        cur: sqlite3.Cursor,
        server: str,
    ) -> tuple[int, int, int, int, int, float | None]:
        row = cur.execute(
            """
            SELECT
              successes,
              failures,
              consecutive_failures,
              monitor_reconnects,
              monitor_switches,
              last_latency_ms
            FROM sources
            WHERE server=?
            """,
            (server,),
        ).fetchone()
        if row is None:
            return 0, 0, 0, 0, 0, None
        latency = (
            float(row["last_latency_ms"])
            if row["last_latency_ms"] is not None
            else None
        )
        return (
            int(row["successes"]),
            int(row["failures"]),
            int(row["consecutive_failures"]),
            int(row["monitor_reconnects"]),
            int(row["monitor_switches"]),
            latency,
        )

    def _upsert_source_cur(self, cur: sqlite3.Cursor, server: str, ts: int) -> None:
        host, port = parse_kiwi_server_address(server)
        cur.execute(
            """
            INSERT INTO sources(server, host, port, enabled, first_seen_ts, score)
            VALUES(?, ?, ?, 1, ?, 0)
            ON CONFLICT(server) DO UPDATE SET
              host=excluded.host,
              port=excluded.port,
              last_seen_ts=COALESCE(sources.last_seen_ts, excluded.first_seen_ts)
            """,
            (server, host, port, ts),
        )

    def _upsert_source(self, server: str) -> None:
        ts = _now_ts()
        cur = self._conn.cursor()
        self._upsert_source_cur(cur, server, ts)
        self._conn.commit()

    def add_sources(
        self,
        servers: Iterable[str],
        *,
        max_total: int = 1000,
        daily_cap: int | None = None,
    ) -> int:
        added = 0
        cap = (
            int(self._policy.daily_fetch_cap)
            if daily_cap is None
            else max(0, int(daily_cap))
        )
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        for raw in servers:
            if cap is not None and added >= cap:
                break
            server = str(raw).strip()
            if not server:
                continue
            try:
                parse_kiwi_server_address(server)
            except Exception:
                continue
            if self.has_source(server):
                continue
            self._upsert_source(server)
            added += 1
        self.prune(max_total=total_limit)
        return added

    def has_source(self, server: str) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM sources WHERE server=? LIMIT 1", (server,))
        return cur.fetchone() is not None

    def record_scan(
        self,
        result: KiwiReachabilityResult,
        *,
        max_total: int = 1000,
        run_id: str | None = None,
    ) -> None:
        if not result.server.strip():
            return
        try:
            self._upsert_source(result.server)
        except Exception:
            return

        ts = _now_ts()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO scan_history(
              run_id,
              ts,
              server,
              tcp_ok,
              latency_ms,
              tcp_ms,
              http_status,
              http_ok,
              http_ms,
              kiwi_ts,
              total_ms,
              error_kind,
              error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run_id) if run_id else None,
                ts,
                result.server,
                1 if result.tcp_ok else 0,
                result.latency_ms,
                result.tcp_ms,
                result.http_status,
                1 if result.http_ok else 0 if result.http_ok is False else None,
                result.http_ms,
                result.kiwi_ts,
                result.total_ms,
                result.error_kind,
                result.error,
            ),
        )

        enable_after_ok = bool(result.tcp_ok) and not self.is_blocked(
            result.server,
            kind="blacklist",
        )
        (
            successes,
            failures,
            consecutive,
            monitor_reconnects,
            monitor_switches,
            _last_latency,
        ) = self._get_source_counters(cur, result.server)

        if result.tcp_ok:
            successes += 1
            consecutive = 0
            last_ok_ts = ts
            last_fail_ts = None
        else:
            failures += 1
            consecutive += 1
            last_ok_ts = None
            last_fail_ts = ts

        score = compute_source_score(
            successes=successes,
            failures=failures,
            latency_ms=result.latency_ms,
            monitor_reconnects=monitor_reconnects,
            monitor_switches=monitor_switches,
        )
        _cooldown, _next_probe, prior_backoff = self.get_schedule(result.server)
        cooldown_until_ts, next_probe_ts, backoff_level = self._schedule_after_probe(
            server=result.server,
            now_ts=ts,
            success=bool(result.tcp_ok),
            prior_backoff_level=int(prior_backoff),
        )
        status_ts = ts if bool(result.status_ok) else None
        last_users = result.users
        last_users_max = result.users_max

        cur.execute(
            """
            UPDATE sources SET
              last_seen_ts=?,
              last_ok_ts=COALESCE(?, last_ok_ts),
              last_fail_ts=COALESCE(?, last_fail_ts),
              successes=?,
              failures=?,
              consecutive_failures=?,
              last_latency_ms=?,
              last_http_status=?,
              last_kiwi_ts=?,
              last_error=?,
              score=?,
              cooldown_until_ts=?,
              next_probe_ts=?,
              backoff_level=?,
              last_users=COALESCE(?, last_users),
              last_users_max=COALESCE(?, last_users_max),
              last_status_ts=COALESCE(?, last_status_ts),
              enabled=CASE WHEN ? THEN 1 ELSE enabled END
            WHERE server=?
            """,
            (
                ts,
                last_ok_ts,
                last_fail_ts,
                successes,
                failures,
                consecutive,
                result.latency_ms,
                result.http_status,
                result.kiwi_ts,
                result.error,
                score,
                cooldown_until_ts,
                next_probe_ts,
                int(backoff_level),
                last_users,
                last_users_max,
                status_ts,
                1 if enable_after_ok else 0,
                result.server,
            ),
        )
        self._conn.commit()
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        self.prune(max_total=total_limit)

    def record_scans(
        self,
        results: Iterable[KiwiReachabilityResult],
        *,
        max_total: int = 1000,
        run_id: str | None = None,
    ) -> tuple[int, int]:
        added = 0
        ts = _now_ts()
        cur = self._conn.cursor()
        for r in results:
            server = str(r.server).strip()
            if not server:
                continue
            try:
                parse_kiwi_server_address(server)
            except Exception:
                continue

            existed = (
                cur.execute(
                    "SELECT 1 FROM sources WHERE server=? LIMIT 1",
                    (server,),
                ).fetchone()
                is not None
            )
            if not existed:
                added += 1

            self._upsert_source_cur(cur, server, ts)

            cur.execute(
                """
                INSERT INTO scan_history(
                  run_id,
                  ts,
                  server,
                  tcp_ok,
                  latency_ms,
                  tcp_ms,
                  http_status,
                  http_ok,
                  http_ms,
                  kiwi_ts,
                  total_ms,
                  error_kind,
                  error
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id) if run_id else None,
                    ts,
                    server,
                    1 if r.tcp_ok else 0,
                    r.latency_ms,
                    r.tcp_ms,
                    r.http_status,
                    1 if r.http_ok else 0 if r.http_ok is False else None,
                    r.http_ms,
                    r.kiwi_ts,
                    r.total_ms,
                    r.error_kind,
                    r.error,
                ),
            )

            row = cur.execute(
                """
                SELECT
                  successes,
                  failures,
                  consecutive_failures,
                  monitor_reconnects,
                  monitor_switches,
                  backoff_level
                FROM sources
                WHERE server=?
                """,
                (server,),
            ).fetchone()
            if row is None:
                continue

            successes = int(row["successes"])
            failures = int(row["failures"])
            consecutive = int(row["consecutive_failures"])
            monitor_reconnects = int(row["monitor_reconnects"])
            monitor_switches = int(row["monitor_switches"])
            prior_backoff = int(row["backoff_level"] or 0)

            if r.tcp_ok:
                successes += 1
                consecutive = 0
                last_ok_ts = ts
                last_fail_ts = None
            else:
                failures += 1
                consecutive += 1
                last_ok_ts = None
                last_fail_ts = ts

            enable_after_ok = bool(r.tcp_ok) and not self.is_blocked(
                server,
                kind="blacklist",
            )
            score = compute_source_score(
                successes=successes,
                failures=failures,
                latency_ms=r.latency_ms,
                monitor_reconnects=monitor_reconnects,
                monitor_switches=monitor_switches,
            )
            (
                cooldown_until_ts,
                next_probe_ts,
                backoff_level,
            ) = self._schedule_after_probe(
                server=server,
                now_ts=ts,
                success=bool(r.tcp_ok),
                prior_backoff_level=prior_backoff,
            )
            status_ts = ts if bool(r.status_ok) else None
            last_users = r.users
            last_users_max = r.users_max

            cur.execute(
                """
                UPDATE sources SET
                  last_seen_ts=?,
                  last_ok_ts=COALESCE(?, last_ok_ts),
                  last_fail_ts=COALESCE(?, last_fail_ts),
                  successes=?,
                  failures=?,
                  consecutive_failures=?,
                  last_latency_ms=?,
                  last_http_status=?,
                  last_kiwi_ts=?,
                  last_error=?,
                  score=?,
                  cooldown_until_ts=?,
                  next_probe_ts=?,
                  backoff_level=?,
                  last_users=COALESCE(?, last_users),
                  last_users_max=COALESCE(?, last_users_max),
                  last_status_ts=COALESCE(?, last_status_ts),
                  enabled=CASE WHEN ? THEN 1 ELSE enabled END
                WHERE server=?
                """,
                (
                    ts,
                    last_ok_ts,
                    last_fail_ts,
                    successes,
                    failures,
                    consecutive,
                    r.latency_ms,
                    r.http_status,
                    r.kiwi_ts,
                    r.error,
                    score,
                    cooldown_until_ts,
                    next_probe_ts,
                    int(backoff_level),
                    last_users,
                    last_users_max,
                    status_ts,
                    1 if enable_after_ok else 0,
                    server,
                ),
            )

        self._conn.commit()
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        disabled = self.prune(max_total=total_limit)
        return added, disabled

    def record_monitor_event(
        self,
        *,
        server: str,
        event_type: str,
        from_server: str | None = None,
        to_server: str | None = None,
        detail: str | None = None,
        max_total: int = 1000,
    ) -> None:
        text = str(server).strip()
        if not text:
            return
        try:
            parse_kiwi_server_address(text)
        except Exception:
            return

        ts = _now_ts()
        cur = self._conn.cursor()
        self._upsert_source_cur(cur, text, ts)
        cur.execute(
            """
            INSERT INTO monitor_events(
              ts,
              server,
              event_type,
              from_server,
              to_server,
              detail
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (ts, text, str(event_type), from_server, to_server, detail),
        )

        (
            successes,
            failures,
            consecutive,
            monitor_reconnects,
            monitor_switches,
            last_latency,
        ) = self._get_source_counters(cur, text)

        if event_type == "reconnect":
            monitor_reconnects += 1
        elif event_type == "switch":
            monitor_switches += 1

        score = compute_source_score(
            successes=successes,
            failures=failures,
            latency_ms=last_latency,
            monitor_reconnects=monitor_reconnects,
            monitor_switches=monitor_switches,
        )
        cur.execute(
            """
            UPDATE sources SET
              monitor_reconnects=?,
              monitor_switches=?,
              score=?
            WHERE server=?
            """,
            (monitor_reconnects, monitor_switches, score, text),
        )
        self._conn.commit()
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        self.prune(max_total=total_limit)

    def record_audio_health(
        self,
        *,
        server: str,
        ok: bool,
        max_total: int = 1000,
    ) -> None:
        text = str(server).strip()
        if not text:
            return
        try:
            parse_kiwi_server_address(text)
        except Exception:
            return
        ts = _now_ts()
        cur = self._conn.cursor()
        self._upsert_source_cur(cur, text, ts)
        if bool(ok):
            cur.execute(
                """
                UPDATE sources SET
                  last_audio_ok_ts=COALESCE(?, last_audio_ok_ts)
                WHERE server=?
                """,
                (ts, text),
            )
        else:
            cur.execute(
                """
                UPDATE sources SET
                  last_audio_fail_ts=COALESCE(?, last_audio_fail_ts),
                  audio_dropouts=audio_dropouts + 1
                WHERE server=?
                """,
                (ts, text),
            )
        self._conn.commit()
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        self.prune(max_total=total_limit)

    def list_history(
        self,
        server: str,
        *,
        limit: int = 50,
    ) -> list[KiwiScanHistoryRow]:
        text = str(server).strip()
        if not text:
            return []
        lim = max(1, int(limit))
        cur = self._conn.cursor()
        rows = cur.execute(
            """
            SELECT
              ts,
              tcp_ok,
              latency_ms,
              tcp_ms,
              http_status,
              http_ok,
              http_ms,
              kiwi_ts,
              total_ms,
              error_kind,
              run_id,
              error
            FROM scan_history
            WHERE server=?
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (text, lim),
        ).fetchall()
        out: list[KiwiScanHistoryRow] = []
        for r in rows:
            out.append(
                KiwiScanHistoryRow(
                    ts=int(r["ts"]),
                    tcp_ok=bool(int(r["tcp_ok"])),
                    latency_ms=(
                        float(r["latency_ms"]) if r["latency_ms"] is not None else None
                    ),
                    tcp_ms=float(r["tcp_ms"]) if r["tcp_ms"] is not None else None,
                    http_status=(
                        int(r["http_status"]) if r["http_status"] is not None else None
                    ),
                    http_ok=(
                        bool(int(r["http_ok"])) if r["http_ok"] is not None else None
                    ),
                    http_ms=float(r["http_ms"]) if r["http_ms"] is not None else None,
                    kiwi_ts=int(r["kiwi_ts"]) if r["kiwi_ts"] is not None else None,
                    total_ms=(
                        float(r["total_ms"]) if r["total_ms"] is not None else None
                    ),
                    error_kind=(
                        str(r["error_kind"]) if r["error_kind"] is not None else None
                    ),
                    run_id=str(r["run_id"]) if r["run_id"] is not None else None,
                    error=str(r["error"]) if r["error"] is not None else None,
                )
            )
        return out

    def list_sources(
        self,
        *,
        limit: int = 50,
        enabled_only: bool = True,
    ) -> list[KiwiSourceSummary]:
        lim = max(1, int(limit))
        cur = self._conn.cursor()
        if enabled_only:
            rows = cur.execute(
                """
                SELECT
                  server,
                  enabled,
                  score,
                  successes,
                  failures,
                  consecutive_failures,
                  last_latency_ms,
                  last_seen_ts,
                  last_users,
                  last_users_max,
                  last_status_ts,
                  last_audio_ok_ts,
                  audio_dropouts
                FROM sources
                WHERE enabled=1
                ORDER BY score DESC, successes DESC, failures ASC, server ASC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT
                  server,
                  enabled,
                  score,
                  successes,
                  failures,
                  consecutive_failures,
                  last_latency_ms,
                  last_seen_ts,
                  last_users,
                  last_users_max,
                  last_status_ts,
                  last_audio_ok_ts,
                  audio_dropouts
                FROM sources
                ORDER BY
                  enabled DESC,
                  score DESC,
                  successes DESC,
                  failures ASC,
                  server ASC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        out: list[KiwiSourceSummary] = []
        for r in rows:
            out.append(
                KiwiSourceSummary(
                    server=str(r["server"]),
                    enabled=bool(int(r["enabled"])),
                    score=float(r["score"]),
                    successes=int(r["successes"]),
                    failures=int(r["failures"]),
                    consecutive_failures=int(r["consecutive_failures"]),
                    last_latency_ms=(
                        float(r["last_latency_ms"])
                        if r["last_latency_ms"] is not None
                        else None
                    ),
                    last_seen_ts=(
                        int(r["last_seen_ts"])
                        if r["last_seen_ts"] is not None
                        else None
                    ),
                    last_users=(
                        int(r["last_users"]) if r["last_users"] is not None else None
                    ),
                    last_users_max=(
                        int(r["last_users_max"])
                        if r["last_users_max"] is not None
                        else None
                    ),
                    last_status_ts=(
                        int(r["last_status_ts"])
                        if r["last_status_ts"] is not None
                        else None
                    ),
                    last_audio_ok_ts=(
                        int(r["last_audio_ok_ts"])
                        if r["last_audio_ok_ts"] is not None
                        else None
                    ),
                    audio_dropouts=int(r["audio_dropouts"] or 0),
                )
            )
        return out

    def pick_servers(
        self,
        *,
        target: int = 50,
        include_disabled: bool = False,
    ) -> list[str]:
        n = max(1, int(target))
        summaries = self.list_sources(limit=n, enabled_only=not include_disabled)
        return [s.server for s in summaries][:n]

    def prune(
        self,
        *,
        max_total: int = 1000,
    ) -> int:
        total_limit = (
            int(self._policy.max_total) if max_total == 1000 else int(max_total)
        )
        limit = max(50, int(total_limit))
        cf = int(self._policy.prune_disable_consecutive_failures)
        min_samples = int(self._policy.prune_disable_min_samples)
        min_ratio = float(self._policy.prune_disable_min_success_ratio)
        cur = self._conn.cursor()
        now_ts = _now_ts()
        self._expire_blocks_cur(cur, now_ts=now_ts)

        to_disable_rows = cur.execute(
            """
            SELECT server
            FROM sources
            WHERE enabled=1
              AND consecutive_failures >= ?
              AND (successes + failures) >= ?
              AND (CAST(successes AS REAL) / NULLIF(successes + failures, 0)) < ?
            """,
            (cf, min_samples, min_ratio),
        ).fetchall()
        to_disable = [str(r["server"]) for r in to_disable_rows]
        disabled = cur.execute(
            """
            UPDATE sources SET enabled=0
            WHERE enabled=1
              AND consecutive_failures >= ?
              AND (successes + failures) >= ?
              AND (CAST(successes AS REAL) / NULLIF(successes + failures, 0)) < ?
            """,
            (cf, min_samples, min_ratio),
        ).rowcount
        invalid_days = max(0, int(self._policy.invalid_ttl_days))
        if to_disable and invalid_days > 0:
            expires_ts = now_ts + invalid_days * 86400
            for server in to_disable:
                if self.is_blocked(server, kind="blacklist"):
                    continue
                cur.execute(
                    """
                    INSERT INTO blocked_sources(
                      server,
                      kind,
                      first_ts,
                      last_ts,
                      expires_ts,
                      hits,
                      reason
                    )
                    VALUES(?, 'invalid', ?, ?, ?, 1, ?)
                    ON CONFLICT(server, kind) DO UPDATE SET
                      last_ts=excluded.last_ts,
                      expires_ts=excluded.expires_ts,
                      hits=blocked_sources.hits + 1,
                      reason=COALESCE(excluded.reason, blocked_sources.reason)
                    """,
                    (
                        server,
                        now_ts,
                        now_ts,
                        int(expires_ts),
                        "auto-prune",
                    ),
                )

        total = cur.execute("SELECT COUNT(1) AS n FROM sources").fetchone()
        n_total = int(total["n"]) if total is not None else 0

        if n_total > limit:
            excess = n_total - limit
            worst = cur.execute(
                """
                SELECT server FROM sources
                ORDER BY
                  enabled ASC,
                  score ASC,
                  successes ASC,
                  failures DESC,
                  server ASC
                LIMIT ?
                """,
                (excess,),
            ).fetchall()
            servers = [str(r["server"]) for r in worst]
            if servers:
                placeholders = ",".join(["?"] * len(servers))
                cur.execute(
                    f"DELETE FROM sources WHERE server IN ({placeholders})",
                    tuple(servers),
                )

        stale_days = max(0, int(self._policy.stale_delete_days))
        if stale_days > 0:
            cutoff = now_ts - stale_days * 86400
            stale = cur.execute(
                """
                SELECT server FROM sources
                WHERE enabled=0
                  AND last_seen_ts IS NOT NULL
                  AND last_seen_ts < ?
                """,
                (int(cutoff),),
            ).fetchall()
            stale_servers = [str(r["server"]) for r in stale]
            if stale_servers:
                placeholders = ",".join(["?"] * len(stale_servers))
                cur.execute(
                    f"DELETE FROM scan_history WHERE server IN ({placeholders})",
                    tuple(stale_servers),
                )
                cur.execute(
                    f"DELETE FROM monitor_events WHERE server IN ({placeholders})",
                    tuple(stale_servers),
                )
                cur.execute(
                    f"DELETE FROM blocked_sources WHERE server IN ({placeholders})",
                    tuple(stale_servers),
                )
                cur.execute(
                    f"DELETE FROM sources WHERE server IN ({placeholders})",
                    tuple(stale_servers),
                )
        self._conn.commit()
        return int(disabled)

    def stats(self) -> dict[str, int]:
        cur = self._conn.cursor()
        total = cur.execute("SELECT COUNT(1) AS n FROM sources").fetchone()
        enabled = cur.execute(
            "SELECT COUNT(1) AS n FROM sources WHERE enabled=1"
        ).fetchone()
        return {
            "total": int(total["n"]) if total is not None else 0,
            "enabled": int(enabled["n"]) if enabled is not None else 0,
        }


async def fetch_public_kiwi_sources(
    *,
    timeout_seconds: float = 8.0,
) -> list[str]:
    # 不尝试绕过验证码/反爬；仅做温和拉取 + 文本提取，失败时走离线导入（parse_kiwi_public_text）。
    urls = [
        "https://rx.kiwisdr.com/",
        "https://rx.kiwisdr.com/public/",
        "https://kiwisdr.com/public/",
        "https://www.kiwisdr.com/public/",
        "http://rx.kiwisdr.com/",
        "http://kiwisdr.com/public/",
        "http://www.kiwisdr.com/public/",
        "http://kiwisdr.com/.public/",
    ]
    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
    }
    found: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in urls:
            try:
                async with session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        continue
                    raw = await resp.content.read(2_000_000)
                    encoding = resp.charset or "utf-8"
                    text = raw.decode(encoding, errors="ignore")
            except Exception:
                continue
            found = parse_kiwi_public_text(text)
            if found:
                break
    return found


def parse_kiwi_public_text(text: str, *, max_results: int = 5000) -> list[str]:
    raw = str(text)
    limit = max(1, int(max_results))
    pattern = re.compile(r"https?://([A-Za-z0-9.-]+):(\d{2,5})")
    found: list[str] = []
    for host, port_str in pattern.findall(raw):
        if len(found) >= limit:
            break
        port = int(port_str)
        if not (8000 <= port <= 9000):
            continue
        server = f"{host}:{port}"
        if server in found:
            continue
        found.append(server)
    return found
