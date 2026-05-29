from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

from echofist.config import load_config
from echofist.core.kiwi_client import KiwiReachabilityResult, parse_kiwi_server_address
from echofist.core.kiwi_sources import (
    KiwiSourceRegistry,
    extract_servers_from_lines,
    extract_servers_from_text,
)


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


def _pick_seed_candidates(
    registry: KiwiSourceRegistry,
    *,
    limit: int,
    include_disabled: bool,
    min_interval_days: int,
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
    server: str,
    host: str,
    port: int,
    *,
    timeout_seconds: float,
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
    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    async with aiohttp.ClientSession(timeout=timeout) as session:
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


async def _probe_one(
    server: str,
    *,
    timeout_seconds: float,
    verify_http: bool,
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
            server,
            host,
            port,
            timeout_seconds=float(timeout_seconds),
            sample_limit_bytes=int(sample_limit_bytes),
        )
        error_kind = http_error_kind
        error = http_error

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
    progress_every: int,
    post_delay_ms: int,
    ver_samples: int,
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

    per_server_worst = float(timeout_seconds) * (2.0 if verify_http else 1.0)
    worst = (total / limit) * per_server_worst
    print(
        f"开始探测：total={total} concurrency={limit} "
        f"timeout={float(timeout_seconds):.1f}s verify_http={bool(verify_http)} "
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
        if every > 0 and (done % every == 0 or done == total):
            print(
                f"进度：{done}/{total} ok={ok} fail={fail}",
                flush=True,
            )
    if verify_http:
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


async def _run(args: argparse.Namespace) -> int:
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
        if file_candidates:
            candidates = _dedupe_keep_order(
                [
                    s
                    for s in file_candidates
                    if not registry.is_blocked(s)
                    and (
                        min_interval_days <= 0
                        or registry.get_last_seen_ts(s) is None
                        or int(registry.get_last_seen_ts(s) or 0)
                        <= int(time.time()) - min_interval_days * 86400
                    )
                ]
            )[:limit]
        else:
            candidates = _pick_seed_candidates(
                registry,
                limit=limit,
                include_disabled=bool(args.include_disabled),
                min_interval_days=min_interval_days,
            )

        if not candidates:
            print(f"registry={db_path}")
            print("未找到可用于探测的候选源（可能都被名单过滤/或触发最小复检间隔）。")
            return 2

        candidate_source = "file" if file_candidates else "registry"
        run_id = registry.start_scan_run(
            mode="seed",
            concurrency=int(args.concurrency),
            timeout_seconds=float(args.timeout),
            verify_http=bool(args.verify_http),
            candidate_source=candidate_source,
            limit_n=len(candidates),
        )

        results = await _scan_with_progress(
            candidates,
            concurrency=int(args.concurrency),
            timeout_seconds=float(args.timeout),
            verify_http=bool(args.verify_http),
            progress_every=int(args.progress_every),
            post_delay_ms=int(args.post_delay_ms),
            ver_samples=int(args.ver_samples),
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
            print(
                f"{i:>2}. {s.server} score={s.score:.4f} "
                f"s={s.successes} f={s.failures} cf={s.consecutive_failures} "
                f"latency_ms={s.last_latency_ms}"
            )
        return 0
    finally:
        registry.close()


def main() -> int:
    args = _parse_args(sys.argv[1:])
    return int(asyncio.run(_run(args)))


if __name__ == "__main__":
    raise SystemExit(main())
