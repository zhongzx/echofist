from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import aiohttp
import numpy as np

from echofist.config import load_config
from echofist.core.kiwi_client import (
    KiwiReachabilityResult,
    KiwiSDRClient,
    parse_kiwi_server_address,
)
from echofist.core.kiwi_sources import (
    KiwiSourceRegistry,
    extract_servers_from_lines,
    extract_servers_from_text,
)


class SeedRunStatus(str, Enum):
    OK = "ok"
    NO_CANDIDATES = "no_candidates"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SeedRunResult:
    status: SeedRunStatus
    exit_code: int
    registry_path: str
    candidate_source: str
    candidates_requested: int
    candidates_planned: int
    candidates_scanned: int
    ok: int
    fail: int
    avg_latency_ms: float | None
    run_id: str | None
    diagnostics: dict[str, int]
    daily_budget: dict[str, int | str | None]
    message: str

    def to_json(self) -> str:
        payload = dataclasses.asdict(self)
        payload["status"] = str(self.status.value)
        return json.dumps(payload, ensure_ascii=False)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed_kiwi_memory.py",
        description="为 Kiwi 源注册表建立基础记忆：挑选候选源并做一次温和探测写回。",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="注册表路径（默认使用 ~/.echofist/kiwi_sources.sqlite3）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="本次最多探测的源数量（建议小于等于 200）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="并发量（推荐 5，上限 10）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.2,
        help="单个源探测超时（秒）",
    )
    parser.add_argument(
        "--verify-http",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否额外校验 http://host:port/VER",
    )
    parser.add_argument(
        "--fetch-status",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="是否额外拉取 http://host:port/status 以采集 users/users_max（更慢更重）",
    )
    parser.add_argument(
        "--audio-probe",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="是否额外做最小音频可用性探测（更慢更重）",
    )
    parser.add_argument(
        "--audio-probe-limit",
        type=int,
        default=30,
        help="本次最多对多少台（tcp_ok 的）源做音频探测",
    )
    parser.add_argument(
        "--audio-probe-concurrency",
        type=int,
        default=2,
        help="音频探测并发（推荐 2，上限 5）",
    )
    parser.add_argument(
        "--audio-probe-timeout",
        type=float,
        default=3.0,
        help="单台音频探测总超时（秒）",
    )
    parser.add_argument(
        "--audio-probe-min-chunks",
        type=int,
        default=3,
        help="认为音频可用所需的最少音频块数量",
    )
    parser.add_argument(
        "--audio-probe-min-peak",
        type=float,
        default=1e-6,
        help="认为音频块有效的最小峰值(abs max)阈值",
    )
    parser.add_argument(
        "--audio-probe-progress-every",
        type=int,
        default=10,
        help="音频探测每完成 N 台打印一次进度（0 表示关闭）",
    )
    parser.add_argument(
        "--min-interval-days",
        type=int,
        default=None,
        help="同一源最小复检间隔（天），默认使用配置 kiwi_sources.scan_min_interval_days",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="是否包含已禁用源（默认仅从 enabled=1 候选中挑选）",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        default=None,
        help="从文本文件提取 host:port 作为候选源（优先级高于从注册表挑选）",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取文本并提取 host:port 作为候选源（优先级高于注册表）",
    )
    parser.add_argument(
        "--max-total",
        type=int,
        default=None,
        help="注册表总量上限（默认使用配置 kiwi_sources.max_total）",
    )
    parser.add_argument(
        "--daily-cap",
        type=int,
        default=None,
        help="每日探测上限（默认使用配置 kiwi_sources.daily_probe_cap）",
    )
    parser.add_argument(
        "--ignore-daily-budget",
        action="store_true",
        help="忽略每日探测预算（仅用于离线/沙盒验证）",
    )
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help="忽略 sources 内的 next_probe/cooldown 调度字段（仅用于离线/沙盒验证）",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="每完成 N 个探测打印一次进度（0 表示关闭）",
    )
    parser.add_argument(
        "--post-delay-ms",
        type=int,
        default=150,
        help="每次探测结束后额外等待毫秒数（用于温和节流，0 表示关闭）",
    )
    parser.add_argument(
        "--ver-samples",
        type=int,
        default=6,
        help="打印 /VER 样本数量（0 表示关闭）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="仅输出机器可读 JSON 结果（不打印进度与摘要文本）",
    )
    return parser.parse_args(argv)


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.cursor()
    row = cur.execute(f"SELECT COUNT(1) AS n FROM {table}").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _dedupe_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _diagnose_registry_candidates_empty(
    registry: KiwiSourceRegistry,
    *,
    include_disabled: bool,
    min_interval_days: int,
    ignore_schedule: bool,
) -> dict[str, int]:
    now_ts = int(time.time())
    interval_days = max(0, int(min_interval_days))
    cutoff_ts = now_ts - interval_days * 86400 if interval_days > 0 else None
    enabled_cond = "" if include_disabled else "s.enabled=1"
    where_base = "" if not enabled_cond else f"WHERE {enabled_cond}"
    where_join = "AND" if where_base else "WHERE"
    cur = registry._conn.cursor()

    def _count(where_sql: str, params: dict[str, int]) -> int:
        row = cur.execute(where_sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0])

    params: dict[str, int] = {"now_ts": now_ts}
    base = _count(
        f"SELECT COUNT(1) FROM sources s {where_base}",
        params,
    )
    blocked = _count(
        f"""
        SELECT COUNT(1)
        FROM sources s
        JOIN blocked_sources b
          ON b.server=s.server
         AND (b.expires_ts IS NULL OR b.expires_ts > :now_ts)
        {where_base}
        """,
        params,
    )

    if cutoff_ts is not None:
        params["cutoff_ts"] = int(cutoff_ts)
        too_recent = _count(
            f"""
            SELECT COUNT(1)
            FROM sources s
            LEFT JOIN blocked_sources b
              ON b.server=s.server
             AND (b.expires_ts IS NULL OR b.expires_ts > :now_ts)
            {where_base}
              {"AND" if where_base else "WHERE"} b.server IS NULL
              AND s.last_seen_ts IS NOT NULL
              AND s.last_seen_ts > :cutoff_ts
            """,
            params,
        )
    else:
        too_recent = 0

    if ignore_schedule:
        not_due = 0
    else:
        recent_clause = (
            "AND (s.last_seen_ts IS NULL OR s.last_seen_ts <= :cutoff_ts)"
            if cutoff_ts is not None
            else ""
        )
        not_due = _count(
            f"""
            SELECT COUNT(1)
            FROM sources s
            LEFT JOIN blocked_sources b
              ON b.server=s.server
             AND (b.expires_ts IS NULL OR b.expires_ts > :now_ts)
            {where_base}
              {where_join} b.server IS NULL
              {recent_clause}
              AND (
                (s.cooldown_until_ts IS NOT NULL AND s.cooldown_until_ts > :now_ts)
                OR (s.next_probe_ts IS NOT NULL AND s.next_probe_ts > :now_ts)
              )
            """,
            params,
        )

    eligible_recent_clause = (
        "AND (s.last_seen_ts IS NULL OR s.last_seen_ts <= :cutoff_ts)"
        if cutoff_ts is not None
        else ""
    )
    cooldown_clause = (
        "AND (s.cooldown_until_ts IS NULL OR s.cooldown_until_ts <= :now_ts)"
        if not ignore_schedule
        else ""
    )
    next_probe_clause = (
        "AND (s.next_probe_ts IS NULL OR s.next_probe_ts <= :now_ts)"
        if not ignore_schedule
        else ""
    )
    eligible = _count(
        f"""
        SELECT COUNT(1)
        FROM sources s
        LEFT JOIN blocked_sources b
          ON b.server=s.server
         AND (b.expires_ts IS NULL OR b.expires_ts > :now_ts)
        {where_base}
          {where_join} b.server IS NULL
          {eligible_recent_clause}
          {cooldown_clause}
          {next_probe_clause}
        """,
        params,
    )
    return {
        "base": int(base),
        "blocked": int(blocked),
        "too_recent": int(too_recent),
        "not_due": int(not_due),
        "eligible": int(eligible),
    }


def _pick_seed_candidates(
    registry: KiwiSourceRegistry,
    *,
    limit: int,
    include_disabled: bool,
    min_interval_days: int,
    ignore_schedule: bool,
) -> list[str]:
    lim = max(1, int(limit))
    interval_days = max(0, int(min_interval_days))
    now_ts = int(time.time())
    cutoff_ts = now_ts - interval_days * 86400 if interval_days > 0 else None

    enabled_filter = "" if include_disabled else "WHERE s.enabled=1"
    min_interval_filter = (
        ""
        if cutoff_ts is None
        else "AND (s.last_seen_ts IS NULL OR s.last_seen_ts <= :cutoff_ts)"
    )
    due_filter = (
        ""
        if ignore_schedule
        else """
            AND (s.cooldown_until_ts IS NULL OR s.cooldown_until_ts <= :now_ts)
            AND (s.next_probe_ts IS NULL OR s.next_probe_ts <= :now_ts)
        """
    )
    candidates_sql = f"""
        SELECT
          s.server AS server,
          COALESCE(h.days, 0) AS days
        FROM sources s
        LEFT JOIN (
          SELECT server, COUNT(DISTINCT date(ts, 'unixepoch')) AS days
          FROM scan_history
          GROUP BY server
        ) h ON h.server=s.server
        LEFT JOIN (
          SELECT server
          FROM blocked_sources
          WHERE expires_ts IS NULL OR expires_ts > :now_ts
        ) b ON b.server=s.server
        {enabled_filter}
          {min_interval_filter}
          AND b.server IS NULL
          {due_filter}
        ORDER BY
          days ASC,
          s.score DESC,
          s.successes DESC,
          s.failures ASC,
          s.server ASC
        LIMIT :limit
    """
    cur = registry._conn.cursor()
    rows = cur.execute(
        candidates_sql,
        {"limit": lim, "now_ts": now_ts, "cutoff_ts": cutoff_ts},
    ).fetchall()
    servers = [str(r["server"]) for r in rows if r is not None and r["server"]]
    return _dedupe_keep_order(servers)


def _extract_candidates_from_sources(
    *,
    source_file: Path | None,
    from_stdin: bool,
) -> list[str]:
    if from_stdin:
        raw = sys.stdin.read()
        servers, _invalid = extract_servers_from_text(raw)
        return servers
    if source_file is None:
        return []
    text = source_file.read_text(encoding="utf-8", errors="ignore")
    servers, _invalid, _lines, _truncated = extract_servers_from_lines(
        text.splitlines()
    )
    return servers


def _truncate_text(text: str, *, limit: int) -> str:
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    lim = max(0, int(limit))
    if lim <= 0:
        return ""
    return s[:lim]


def _parse_ver_json(body: str) -> tuple[bool, int | None]:
    try:
        data = json.loads(body)
    except Exception:
        return False, None
    if not isinstance(data, dict):
        return True, None
    ts = data.get("ts")
    if isinstance(ts, int):
        return True, ts
    if isinstance(ts, float):
        return True, int(ts)
    return True, None


async def _probe_tcp(
    host: str,
    port: int,
    *,
    timeout_seconds: float,
) -> tuple[bool, float | None, str | None, str | None]:
    start = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=float(timeout_seconds),
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        tcp_ms = (time.monotonic() - start) * 1000.0
        return True, float(tcp_ms), None, None
    except asyncio.TimeoutError as e:
        return False, None, "timeout", str(e)
    except Exception as e:
        return False, None, "tcp_error", str(e)


async def _probe_ver(
    session: aiohttp.ClientSession,
    server: str,
    host: str,
    port: int,
    *,
    sample_limit_bytes: int,
) -> tuple[
    int | None,
    bool | None,
    float | None,
    int | None,
    str | None,
    str | None,
    dict[str, str | int | float | None] | None,
]:
    start = time.monotonic()
    url = f"http://{host}:{port}/VER"
    try:
        async with session.get(url) as resp:
            http_status = int(resp.status)
            http_ok = http_status == 200
            ct = str(resp.headers.get("content-type") or "")
            raw = await resp.content.read(max(1, int(sample_limit_bytes)))
            body = raw.decode(resp.charset or "utf-8", errors="replace")
            body_head = _truncate_text(body, limit=220)
            json_ok, kiwi_ts = (False, None)
            if http_status == 200:
                json_ok, kiwi_ts = _parse_ver_json(body)
            sample = {
                "server": server,
                "http_status": http_status,
                "content_type": ct,
                "json_ok": 1 if json_ok else 0,
                "kiwi_ts": kiwi_ts,
                "body_head": body_head,
            }
            http_ms = (time.monotonic() - start) * 1000.0
            return (
                http_status,
                http_ok,
                float(http_ms),
                kiwi_ts,
                None,
                None,
                sample,
            )
    except asyncio.TimeoutError as e:
        http_ms = (time.monotonic() - start) * 1000.0
        return None, False, float(http_ms), None, "http_timeout", str(e), None
    except Exception as e:
        http_ms = (time.monotonic() - start) * 1000.0
        return None, False, float(http_ms), None, "http_error", str(e), None


def _parse_status_kv(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in str(text).splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        if k not in {"users", "users_max"}:
            continue
        try:
            out[k] = int(float(value.strip()))
        except ValueError:
            continue
    return out


async def _probe_status(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
) -> tuple[bool | None, float | None, int | None, int | None]:
    start = time.monotonic()
    url = f"http://{host}:{port}/status"
    status_ok: bool | None = None
    users: int | None = None
    users_max: int | None = None
    try:
        async with session.get(url) as resp:
            status_ok = int(resp.status) == 200
            if status_ok:
                text = await resp.text(errors="replace")
                values = _parse_status_kv(text)
                users = values.get("users")
                users_max = values.get("users_max")
    except Exception:
        status_ok = False
    status_ms = (time.monotonic() - start) * 1000.0
    return status_ok, float(status_ms), users, users_max


@dataclass(frozen=True, slots=True)
class AudioProbeResult:
    server: str
    ok: bool
    connect_ms: float | None
    first_audio_ms: float | None
    chunks: int
    timeouts: int
    bad_chunks: int
    error: str | None


class _SilentLog:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, *_args: object, **_kwargs: object) -> None:
        return None

    def error(self, *_args: object, **_kwargs: object) -> None:
        return None

    def success(self, *_args: object, **_kwargs: object) -> None:
        return None


def _percentile(values: list[float], p: float) -> float | None:
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    q = max(0.0, min(1.0, float(p)))
    idx = int(round((len(xs) - 1) * q))
    return float(xs[idx])


async def _audio_probe_one(
    server: str,
    *,
    timeout_seconds: float,
    min_chunks: int,
    min_peak: float,
) -> AudioProbeResult:
    total_deadline = time.monotonic() + float(timeout_seconds)
    client = KiwiSDRClient(server)
    client.logger = _SilentLog()
    try:
        client.config.timeout = max(1, int(timeout_seconds))
        client.config.reconnect_attempts = 1
        client.config.reconnect_delay = 0.0
    except Exception:
        pass

    connect_ms: float | None = None
    first_audio_ms: float | None = None
    chunks = 0
    timeouts = 0
    bad_chunks = 0
    start = time.monotonic()
    try:
        remaining = max(0.1, total_deadline - time.monotonic())
        await asyncio.wait_for(client.connect(), timeout=float(remaining))
        connect_ms = (time.monotonic() - start) * 1000.0

        remaining = max(0.1, total_deadline - time.monotonic())
        await asyncio.wait_for(client.set_mode("cw"), timeout=float(remaining))
        remaining = max(0.1, total_deadline - time.monotonic())
        await asyncio.wait_for(client.set_bandwidth(500), timeout=float(remaining))
        remaining = max(0.1, total_deadline - time.monotonic())
        await asyncio.wait_for(client.start_audio_stream(), timeout=float(remaining))

        stream_start = time.monotonic()
        while time.monotonic() < total_deadline:
            per_try = min(0.4, max(0.05, total_deadline - time.monotonic()))
            chunk = await client.get_audio_chunk(timeout_seconds=float(per_try))
            if chunk is None:
                timeouts += 1
                continue
            if first_audio_ms is None:
                first_audio_ms = (time.monotonic() - stream_start) * 1000.0
            chunks += 1
            try:
                peak = float(np.max(np.abs(chunk)))
                if not np.isfinite(peak) or peak < float(min_peak):
                    bad_chunks += 1
            except Exception:
                bad_chunks += 1
            if chunks >= int(min_chunks):
                break
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return AudioProbeResult(
            server=server,
            ok=False,
            connect_ms=connect_ms,
            first_audio_ms=first_audio_ms,
            chunks=int(chunks),
            timeouts=int(timeouts),
            bad_chunks=int(bad_chunks),
            error=str(e),
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    ok = bool(
        chunks >= int(min_chunks) and bad_chunks == 0 and first_audio_ms is not None
    )
    return AudioProbeResult(
        server=server,
        ok=ok,
        connect_ms=connect_ms,
        first_audio_ms=first_audio_ms,
        chunks=int(chunks),
        timeouts=int(timeouts),
        bad_chunks=int(bad_chunks),
        error=None,
    )


async def _audio_probe_many(
    servers: list[str],
    *,
    concurrency: int,
    timeout_seconds: float,
    min_chunks: int,
    min_peak: float,
    progress_every: int,
    quiet: bool,
) -> list[AudioProbeResult]:
    limit = max(1, min(int(concurrency), 5))
    sem = asyncio.Semaphore(limit)

    async def run_one(server: str) -> AudioProbeResult:
        async with sem:
            return await _audio_probe_one(
                server,
                timeout_seconds=float(timeout_seconds),
                min_chunks=int(min_chunks),
                min_peak=float(min_peak),
            )

    tasks = [asyncio.create_task(run_one(s)) for s in servers]
    if not tasks:
        return []
    total = len(tasks)
    every = 0 if bool(quiet) else max(0, int(progress_every))
    done = 0
    ok = 0
    results: list[AudioProbeResult] = []
    for fut in asyncio.as_completed(tasks):
        r = await fut
        results.append(r)
        done += 1
        if r.ok:
            ok += 1
        if not quiet and every > 0 and (done % every == 0 or done == total):
            print(
                f"audio_progress: {done}/{total} ok={ok} fail={done - ok}",
                flush=True,
            )
    return results


async def _probe_one(
    server: str,
    *,
    timeout_seconds: float,
    verify_http: bool,
    fetch_status: bool,
    sample_limit_bytes: int = 2048,
) -> tuple[KiwiReachabilityResult, dict[str, str | int | float | None] | None]:
    start_total = time.monotonic()
    try:
        host, port = parse_kiwi_server_address(server)
    except Exception as e:
        total_ms = (time.monotonic() - start_total) * 1000.0
        r = KiwiReachabilityResult(
            server=server,
            host="",
            port=0,
            tcp_ok=False,
            latency_ms=None,
            tcp_ms=None,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
            total_ms=total_ms,
            error_kind="invalid_address",
            error=str(e),
        )
        return r, None

    tcp_ok, tcp_ms, tcp_error_kind, tcp_error = await _probe_tcp(
        host,
        port,
        timeout_seconds=float(timeout_seconds),
    )
    if not tcp_ok:
        total_ms = (time.monotonic() - start_total) * 1000.0
        r = KiwiReachabilityResult(
            server=server,
            host=host,
            port=port,
            tcp_ok=False,
            latency_ms=None,
            tcp_ms=None,
            http_status=None,
            http_ok=None,
            http_ms=None,
            kiwi_ts=None,
            status_ok=None,
            status_ms=None,
            users=None,
            users_max=None,
            total_ms=total_ms,
            error_kind=tcp_error_kind,
            error=tcp_error,
        )
        return r, None

    http_status: int | None = None
    http_ok: bool | None = None
    http_ms: float | None = None
    kiwi_ts: int | None = None
    error_kind: str | None = None
    error: str | None = None

    sample: dict[str, str | int | float | None] | None = None
    status_ok: bool | None = None
    status_ms: float | None = None
    users: int | None = None
    users_max: int | None = None

    if verify_http or fetch_status:
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if verify_http:
                (
                    http_status,
                    http_ok,
                    http_ms,
                    kiwi_ts,
                    http_error_kind,
                    http_error,
                    sample,
                ) = await _probe_ver(
                    session,
                    server,
                    host,
                    port,
                    sample_limit_bytes=int(sample_limit_bytes),
                )
                error_kind = http_error_kind
                error = http_error
            if fetch_status:
                status_ok, status_ms, users, users_max = await _probe_status(
                    session,
                    host,
                    port,
                )

    total_ms = (time.monotonic() - start_total) * 1000.0
    r = KiwiReachabilityResult(
        server=server,
        host=host,
        port=port,
        tcp_ok=True,
        latency_ms=float(tcp_ms or 0.0),
        tcp_ms=tcp_ms,
        http_status=http_status,
        http_ok=http_ok,
        http_ms=http_ms,
        kiwi_ts=kiwi_ts,
        status_ok=status_ok,
        status_ms=status_ms,
        users=users,
        users_max=users_max,
        total_ms=float(total_ms),
        error_kind=error_kind,
        error=error,
    )
    return r, sample


async def _scan_with_progress(
    servers: list[str],
    *,
    concurrency: int,
    timeout_seconds: float,
    verify_http: bool,
    fetch_status: bool,
    progress_every: int,
    post_delay_ms: int,
    ver_samples: int,
    quiet: bool,
) -> list[KiwiReachabilityResult]:
    total = len(servers)
    if total <= 0:
        return []
    limit = max(1, min(int(concurrency), 10))
    sem = asyncio.Semaphore(limit)

    delay_ms = max(0, int(post_delay_ms))
    sample_target = max(0, int(ver_samples))
    samples: list[dict[str, str | int | float | None]] = []
    stats: dict[str, int] = {
        "ver_http_200": 0,
        "ver_json_ok": 0,
        "ver_non_json_or_fail": 0,
    }

    async def run_one(server: str) -> KiwiReachabilityResult:
        async with sem:
            r, sample = await _probe_one(
                server,
                timeout_seconds=float(timeout_seconds),
                verify_http=bool(verify_http),
                fetch_status=bool(fetch_status),
            )
            if sample is not None:
                if int(sample.get("http_status") or 0) == 200:
                    stats["ver_http_200"] += 1
                if int(sample.get("json_ok") or 0) == 1:
                    stats["ver_json_ok"] += 1
                else:
                    stats["ver_non_json_or_fail"] += 1
                if sample_target > 0 and len(samples) < sample_target:
                    samples.append(sample)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)
            return r

    per_server_worst = float(timeout_seconds) * (
        1.0 + (1.0 if verify_http else 0.0) + (1.0 if fetch_status else 0.0)
    )
    worst = (total / limit) * per_server_worst
    if not quiet:
        print(
            f"开始探测：total={total} concurrency={limit} "
            f"timeout={float(timeout_seconds):.1f}s "
            f"verify_http={bool(verify_http)} fetch_status={bool(fetch_status)} "
            f"(worst≈{worst:.0f}s)",
            flush=True,
        )

    every = max(0, int(progress_every))
    tasks = [asyncio.create_task(run_one(s)) for s in servers]
    done = 0
    ok = 0
    fail = 0
    results: list[KiwiReachabilityResult] = []

    for fut in asyncio.as_completed(tasks):
        r = await fut
        results.append(r)
        done += 1
        if r.tcp_ok:
            ok += 1
        else:
            fail += 1
        if not quiet and every > 0 and (done % every == 0 or done == total):
            print(
                f"进度：{done}/{total} ok={ok} fail={fail}",
                flush=True,
            )
    if not quiet and verify_http:
        print(
            "ver_stats: "
            f"http_200={stats['ver_http_200']} "
            f"json_ok={stats['ver_json_ok']} "
            f"non_json_or_fail={stats['ver_non_json_or_fail']}",
            flush=True,
        )
        if samples:
            print("ver_samples:", flush=True)
            for i, s in enumerate(samples, start=1):
                print(
                    f"[{i}] {s.get('server')} status={s.get('http_status')} "
                    f"ct={s.get('content_type')} json_ok={s.get('json_ok')} "
                    f"kiwi_ts={s.get('kiwi_ts')}",
                    flush=True,
                )
                print(f"     body_head={s.get('body_head')}", flush=True)
    return results


def _select_candidates_from_file(
    registry: KiwiSourceRegistry,
    file_candidates: list[str],
    *,
    limit: int,
    min_interval_days: int,
    ignore_schedule: bool,
) -> tuple[list[str], dict[str, int]]:
    seen = _dedupe_keep_order([s for s in file_candidates if str(s).strip()])
    extracted = len(seen)
    cutoff = (
        int(time.time()) - int(min_interval_days) * 86400
        if int(min_interval_days) > 0
        else None
    )

    blocked = 0
    too_recent = 0
    not_due = 0
    filtered: list[str] = []
    for s in seen:
        if registry.is_blocked(s):
            blocked += 1
            continue
        if cutoff is not None:
            last_seen = registry.get_last_seen_ts(s)
            if last_seen is not None and int(last_seen) > int(cutoff):
                too_recent += 1
                continue
        filtered.append(s)

    if ignore_schedule:
        candidates = filtered[:limit]
    else:
        due: list[str] = []
        for s in filtered:
            if registry.is_due(s):
                due.append(s)
            else:
                not_due += 1
        candidates = due[:limit]

    diag = {
        "extracted": int(extracted),
        "blocked": int(blocked),
        "too_recent": int(too_recent),
        "not_due": int(not_due),
        "eligible": int(len(candidates)),
    }
    return candidates, diag


def _make_no_candidates_result(
    *,
    registry_path: str,
    candidate_source: str,
    candidates_requested: int,
    diagnostics: dict[str, int],
    ignore_schedule: bool,
) -> SeedRunResult:
    parts: list[str] = [f"source={candidate_source}"]
    if "base" in diagnostics:
        parts.append(f"base={diagnostics['base']}")
    if "extracted" in diagnostics:
        parts.append(f"extracted={diagnostics['extracted']}")
    parts.extend(
        [
            f"blocked={diagnostics.get('blocked', 0)}",
            f"too_recent={diagnostics.get('too_recent', 0)}",
            f"not_due={diagnostics.get('not_due', 0)}",
            f"eligible={diagnostics.get('eligible', 0)}",
        ]
    )
    msg = "未找到可用于探测的候选源（过滤统计）： " + " ".join(parts)
    if not ignore_schedule and int(diagnostics.get("not_due", 0)) > 0:
        msg += (
            "\n提示：当前阶段 1 生效，next_probe_ts/cooldown_until_ts 未到期会跳过。"
            "可等待到期后再跑；或仅沙盒验证时加 --ignore-schedule。"
        )
    return SeedRunResult(
        status=SeedRunStatus.NO_CANDIDATES,
        exit_code=2,
        registry_path=registry_path,
        candidate_source=candidate_source,
        candidates_requested=int(candidates_requested),
        candidates_planned=0,
        candidates_scanned=0,
        ok=0,
        fail=0,
        avg_latency_ms=None,
        run_id=None,
        diagnostics={k: int(v) for k, v in diagnostics.items()},
        daily_budget={},
        message=msg,
    )


async def _run(args: argparse.Namespace) -> SeedRunResult:
    cfg = load_config().kiwi_sources
    min_interval_days = (
        int(cfg.scan_min_interval_days)
        if args.min_interval_days is None
        else max(0, int(args.min_interval_days))
    )
    max_total = int(cfg.max_total) if args.max_total is None else int(args.max_total)
    limit = max(1, int(args.limit))

    registry = KiwiSourceRegistry(path=args.registry)
    try:
        db_path = registry._path
        before_stats = registry.stats()
        before_history = _count_rows(registry._conn, "scan_history")

        file_candidates = _extract_candidates_from_sources(
            source_file=args.source_file,
            from_stdin=bool(args.stdin),
        )
        candidate_source = "file" if file_candidates else "registry"
        diag: dict[str, int] | None = None
        candidates_requested = int(limit)
        if file_candidates:
            candidates, diag = _select_candidates_from_file(
                registry,
                file_candidates,
                limit=limit,
                min_interval_days=min_interval_days,
                ignore_schedule=bool(args.ignore_schedule),
            )
        else:
            candidates = _pick_seed_candidates(
                registry,
                limit=limit,
                include_disabled=bool(args.include_disabled),
                min_interval_days=min_interval_days,
                ignore_schedule=bool(args.ignore_schedule),
            )

        if not candidates:
            if not file_candidates:
                diag = _diagnose_registry_candidates_empty(
                    registry,
                    include_disabled=bool(args.include_disabled),
                    min_interval_days=min_interval_days,
                    ignore_schedule=bool(args.ignore_schedule),
                )
            return _make_no_candidates_result(
                registry_path=str(db_path),
                candidate_source=str(candidate_source),
                candidates_requested=candidates_requested,
                diagnostics=diag or {},
                ignore_schedule=bool(args.ignore_schedule),
            )

        budget_skipped = 0
        budget_day = None
        budget_used_before = None
        budget_used_after = None
        budget_cap = None
        if not bool(args.ignore_daily_budget):
            day, used_before, cap_before = registry.get_daily_probe_budget(
                cap=args.daily_cap,
            )
            allowed, _day, used_after, cap_db = registry.reserve_daily_probe_budget(
                len(candidates),
                cap=args.daily_cap,
            )
            budget_day = day
            budget_used_before = used_before
            budget_used_after = used_after
            budget_cap = cap_db if args.daily_cap is None else cap_before
            budget_skipped = max(0, len(candidates) - int(allowed))
            candidates = candidates[: int(allowed)]
            if not candidates:
                return SeedRunResult(
                    status=SeedRunStatus.BUDGET_EXHAUSTED,
                    exit_code=2,
                    registry_path=str(db_path),
                    candidate_source=str(candidate_source),
                    candidates_requested=candidates_requested,
                    candidates_planned=0,
                    candidates_scanned=0,
                    ok=0,
                    fail=0,
                    avg_latency_ms=None,
                    run_id=None,
                    diagnostics=diag or {},
                    daily_budget={
                        "day": budget_day,
                        "cap": budget_cap,
                        "used_before": budget_used_before,
                        "used_after": budget_used_after,
                        "skipped": budget_skipped,
                    },
                    message=(
                        "今日探测预算已耗尽（daily_probe_cap）。"
                        "可改用 --ignore-daily-budget（仅沙盒）或降低运行频率。"
                    ),
                )

        note = {
            "min_interval_days": int(min_interval_days),
            "schedule_skipped": int(diag.get("not_due", 0) if diag else 0),
            "daily_budget_day": budget_day,
            "daily_budget_cap": budget_cap,
            "daily_budget_used_before": budget_used_before,
            "daily_budget_used_after": budget_used_after,
            "daily_budget_skipped": int(budget_skipped),
            "ignore_daily_budget": bool(args.ignore_daily_budget),
            "ignore_schedule": bool(args.ignore_schedule),
            "fetch_status": bool(args.fetch_status),
        }
        run_id = registry.start_scan_run(
            mode="seed",
            concurrency=int(args.concurrency),
            timeout_seconds=float(args.timeout),
            verify_http=bool(args.verify_http),
            candidate_source=candidate_source,
            limit_n=len(candidates),
            note=json.dumps(note, ensure_ascii=False),
        )

        quiet = bool(args.json)
        progress_every = 0 if quiet else int(args.progress_every)
        ver_samples = 0 if quiet else int(args.ver_samples)
        results = await _scan_with_progress(
            candidates,
            concurrency=int(args.concurrency),
            timeout_seconds=float(args.timeout),
            verify_http=bool(args.verify_http),
            fetch_status=bool(args.fetch_status),
            progress_every=progress_every,
            post_delay_ms=int(args.post_delay_ms),
            ver_samples=ver_samples,
            quiet=quiet,
        )
        _added, disabled = registry.record_scans(
            results,
            max_total=max_total,
            run_id=run_id,
        )
        expired = registry.expire_blocks()

        ok = [r for r in results if r.tcp_ok]
        fail = [r for r in results if not r.tcp_ok]
        avg_latency = (
            (sum(float(r.latency_ms or 0.0) for r in ok) / len(ok)) if ok else None
        )
        registry.finish_scan_run(
            run_id,
            ok=len(ok),
            fail=len(fail),
            avg_latency_ms=avg_latency,
            prune_disabled=disabled,
            expired_blocks=expired,
        )
        after_stats = registry.stats()
        after_history = _count_rows(registry._conn, "scan_history")

        if not bool(args.json):
            print(f"registry={db_path}")
            print(f"candidates={len(candidates)} scanned={len(results)} ok={len(ok)}")
            if avg_latency is not None:
                print(f"avg_latency_ms={avg_latency:.1f}")
            print(
                "sources: "
                f"{before_stats['enabled']}/{before_stats['total']} -> "
                f"{after_stats['enabled']}/{after_stats['total']}"
            )
            print(f"history: {before_history} -> {after_history}")
            print(f"prune_disabled={disabled} expired_blocks={expired}")

            top = registry.list_sources(limit=10, enabled_only=True)
            print("top10:")
            for i, s in enumerate(top, start=1):
                latency_ms = (
                    str(int(round(float(s.last_latency_ms))))
                    if s.last_latency_ms is not None
                    else "None"
                )
                print(
                    f"{i:>2}. {s.server} score={s.score:.4f} "
                    f"s={s.successes} f={s.failures} cf={s.consecutive_failures} "
                    f"latency_ms={latency_ms}"
                )
            print("top10_legend: s=successes f=failures cf=consecutive_failures")

        if bool(args.audio_probe) and not bool(args.json):
            lim = max(0, int(args.audio_probe_limit))
            probe_servers = [r.server for r in results if r.tcp_ok][:lim]
            if probe_servers:
                audio_results = await _audio_probe_many(
                    probe_servers,
                    concurrency=int(args.audio_probe_concurrency),
                    timeout_seconds=float(args.audio_probe_timeout),
                    min_chunks=int(args.audio_probe_min_chunks),
                    min_peak=float(args.audio_probe_min_peak),
                    progress_every=int(args.audio_probe_progress_every),
                    quiet=quiet,
                )
                ok_audio = [r for r in audio_results if r.ok]
                connect_ms_vals = [r.connect_ms for r in audio_results if r.connect_ms]
                first_ms_vals = [
                    r.first_audio_ms for r in audio_results if r.first_audio_ms
                ]
                for r in audio_results:
                    registry.record_audio_health(
                        server=r.server,
                        ok=bool(r.ok),
                        max_total=max_total,
                    )
                print(
                    "audio_probe: "
                    f"attempted={len(audio_results)} ok={len(ok_audio)} "
                    f"min_chunks={int(args.audio_probe_min_chunks)} "
                    f"timeout={float(args.audio_probe_timeout):.1f}s "
                    f"concurrency={max(1, min(int(args.audio_probe_concurrency), 5))}"
                )
                n_error = sum(1 for r in audio_results if r.error is not None)
                n_insufficient = sum(
                    1
                    for r in audio_results
                    if r.error is None
                    and int(r.chunks) < int(args.audio_probe_min_chunks)
                )
                n_bad = sum(1 for r in audio_results if int(r.bad_chunks) > 0)
                print(
                    "audio_probe_fail: "
                    f"error={n_error} insufficient={n_insufficient} bad_chunks={n_bad}"
                )
                p50_conn = _percentile(connect_ms_vals, 0.50)
                p90_conn = _percentile(connect_ms_vals, 0.90)
                p95_conn = _percentile(connect_ms_vals, 0.95)
                p99_conn = _percentile(connect_ms_vals, 0.99)
                p50_first = _percentile(first_ms_vals, 0.50)
                p90_first = _percentile(first_ms_vals, 0.90)
                p95_first = _percentile(first_ms_vals, 0.95)
                p99_first = _percentile(first_ms_vals, 0.99)
                print(
                    "audio_probe_ms: "
                    f"connect_p50={p50_conn} connect_p90={p90_conn} "
                    f"connect_p95={p95_conn} connect_p99={p99_conn} "
                    f"first_audio_p50={p50_first} first_audio_p90={p90_first} "
                    f"first_audio_p95={p95_first} first_audio_p99={p99_first}"
                )
        return SeedRunResult(
            status=SeedRunStatus.OK,
            exit_code=0,
            registry_path=str(db_path),
            candidate_source=str(candidate_source),
            candidates_requested=candidates_requested,
            candidates_planned=int(len(candidates)),
            candidates_scanned=int(len(results)),
            ok=int(len(ok)),
            fail=int(len(fail)),
            avg_latency_ms=float(avg_latency) if avg_latency is not None else None,
            run_id=str(run_id),
            diagnostics=diag or {},
            daily_budget={
                "day": budget_day,
                "cap": budget_cap,
                "used_before": budget_used_before,
                "used_after": budget_used_after,
                "skipped": budget_skipped,
            },
            message="ok",
        )
    finally:
        registry.close()


def main() -> int:
    args = _parse_args(sys.argv[1:])
    result = asyncio.run(_run(args))
    if bool(args.json):
        print(result.to_json())
        return int(result.exit_code)
    if result.status is not SeedRunStatus.OK:
        print(f"registry={result.registry_path}")
        print(result.message)
    return int(result.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
