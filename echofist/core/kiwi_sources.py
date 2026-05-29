"""
KiwiSDR 源注册表（SQLite）

目标：
- 温和扫描：只做可达性与稳定性评估，不做高频/大并发压力探测
- 每次运行增量积累：逐日增加新源、记录扫描结果、淘汰不稳定源
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from echofist.config import ConfigManager, load_config
from echofist.core.kiwi_client import KiwiReachabilityResult, parse_kiwi_server_address


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


@dataclass(frozen=True, slots=True)
class KiwiScanHistoryRow:
    ts: int
    tcp_ok: bool
    latency_ms: float | None
    http_status: int | None
    kiwi_ts: int | None
    error: str | None


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
              ts INTEGER NOT NULL,
              server TEXT NOT NULL,
              tcp_ok INTEGER NOT NULL,
              latency_ms REAL,
              http_status INTEGER,
              kiwi_ts INTEGER,
              error TEXT
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
        self._conn.commit()

    def _ensure_sources_columns(self, cur: sqlite3.Cursor) -> None:
        rows = cur.execute("PRAGMA table_info(sources)").fetchall()
        existing = {str(r[1]) for r in rows}
        additions = [
            ("monitor_reconnects", "monitor_reconnects INTEGER NOT NULL DEFAULT 0"),
            ("monitor_switches", "monitor_switches INTEGER NOT NULL DEFAULT 0"),
        ]
        for name, ddl in additions:
            if name in existing:
                continue
            cur.execute(f"ALTER TABLE sources ADD COLUMN {ddl}")

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
              ts,
              server,
              tcp_ok,
              latency_ms,
              http_status,
              kiwi_ts,
              error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                result.server,
                1 if result.tcp_ok else 0,
                result.latency_ms,
                result.http_status,
                result.kiwi_ts,
                result.error,
            ),
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
              score=?
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
                  ts,
                  server,
                  tcp_ok,
                  latency_ms,
                  http_status,
                  kiwi_ts,
                  error
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    server,
                    1 if r.tcp_ok else 0,
                    r.latency_ms,
                    r.http_status,
                    r.kiwi_ts,
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
                  monitor_switches
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

            score = compute_source_score(
                successes=successes,
                failures=failures,
                latency_ms=r.latency_ms,
                monitor_reconnects=monitor_reconnects,
                monitor_switches=monitor_switches,
            )

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
                  score=?
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
            SELECT ts, tcp_ok, latency_ms, http_status, kiwi_ts, error
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
                    http_status=(
                        int(r["http_status"]) if r["http_status"] is not None else None
                    ),
                    kiwi_ts=int(r["kiwi_ts"]) if r["kiwi_ts"] is not None else None,
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
                  last_seen_ts
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
                  last_seen_ts
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
