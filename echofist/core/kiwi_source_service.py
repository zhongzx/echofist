"""共享 KiwiSDR 电台源：探测编排与消费接口。"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from echofist.config import load_config
from echofist.core.kiwi_client import KiwiReachabilityResult, scan_kiwi_reachability
from echofist.core.kiwi_sources import (
    KiwiBlockedSourceRow,
    KiwiSourceRegistry,
    KiwiSourceSummary,
)

_MAX_TRUSTED_USERS_MAX = 8


@dataclass(frozen=True, slots=True)
class KiwiSourceQuality:
    server: str
    score: float
    effective_score: float
    scan_days: int
    last_seen_ts: int | None
    last_latency_ms: float | None
    last_users: int | None
    last_users_max: int | None
    last_status_ts: int | None
    last_audio_ok_ts: int | None
    audio_dropouts: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class KiwiSourceInsight:
    server: str
    enabled: bool
    score: float
    effective_score: float
    scan_days: int
    last_seen_ts: int | None
    last_users: int | None
    last_users_max: int | None
    last_status_ts: int | None
    last_audio_ok_ts: int | None
    audio_dropouts: int
    blocked: list[KiwiBlockedSourceRow]


@dataclass(frozen=True, slots=True)
class KiwiProbeRunResult:
    run_id: str
    planned: list[str]
    scanned: list[KiwiReachabilityResult]
    ok: int
    fail: int
    avg_latency_ms: float | None
    daily_budget_day: str
    daily_budget_cap: int
    daily_budget_used_before: int
    daily_budget_used_after: int
    prune_disabled: int
    expired_blocks: int


def _compute_effective_score(*, score: float, scan_days: int) -> float:
    base = float(score)
    days = max(0, int(scan_days))
    day_factor = min(1.0, days / 3.0)
    return base * (0.7 + 0.3 * day_factor)


class KiwiSourceService:
    def __init__(self, *, registry_path: Path | None = None) -> None:
        self._registry = KiwiSourceRegistry(path=registry_path)

    def close(self) -> None:
        self._registry.close()

    def maintenance(self, *, max_total: int = 1000) -> dict[str, int]:
        expired = self._registry.expire_blocks()
        disabled = self._registry.prune(max_total=max_total)
        return {"expired_blocks": int(expired), "disabled": int(disabled)}

    def block_blacklist(self, server: str, *, reason: str | None = None) -> None:
        self._registry.block_source(server, kind="blacklist", reason=reason)

    def block_invalid(
        self,
        server: str,
        *,
        reason: str | None = None,
        ttl_days: int | None = None,
    ) -> None:
        self._registry.block_source(
            server,
            kind="invalid",
            reason=reason,
            ttl_days=ttl_days,
        )

    def unblock(self, server: str, *, kind: str | None = None) -> int:
        return self._registry.unblock_source(server, kind=kind)

    def list_blacklist(self, *, limit: int = 200) -> list[KiwiBlockedSourceRow]:
        return self._registry.list_blocked(kind="blacklist", limit=limit)

    def list_invalid(self, *, limit: int = 200) -> list[KiwiBlockedSourceRow]:
        return self._registry.list_blocked(kind="invalid", limit=limit)

    def plan_probes(
        self,
        servers: Iterable[str],
        *,
        min_interval_days: int | None = None,
        limit: int = 200,
    ) -> list[str]:
        cfg = load_config().kiwi_sources
        interval = (
            int(cfg.scan_min_interval_days)
            if min_interval_days is None
            else max(0, int(min_interval_days))
        )
        lim = max(1, int(limit))
        cutoff_ts = int(time.time()) - interval * 86400 if interval > 0 else None

        deduped: list[str] = []
        seen: set[str] = set()
        for raw in servers:
            s = str(raw).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            deduped.append(s)
            if len(deduped) >= lim * 4:
                break

        planned: list[str] = []
        for s in deduped:
            if self._registry.is_blocked(s):
                continue
            if not self._registry.is_due(s):
                continue
            if cutoff_ts is not None:
                last_seen = self._registry.get_last_seen_ts(s)
                if last_seen is not None and int(last_seen) > cutoff_ts:
                    continue
            planned.append(s)
            if len(planned) >= lim:
                break
        return planned

    async def probe_and_learn(
        self,
        servers: Sequence[str],
        *,
        concurrency: int = 5,
        timeout_seconds: float = 1.2,
        verify_http: bool = True,
        fetch_status: bool = False,
        min_interval_days: int | None = None,
        limit: int = 200,
        max_total: int = 1000,
    ) -> list[KiwiReachabilityResult]:
        run = await self.probe_and_learn_run(
            servers,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            verify_http=verify_http,
            fetch_status=fetch_status,
            min_interval_days=min_interval_days,
            limit=limit,
            max_total=max_total,
        )
        return list(run.scanned)

    async def probe_and_learn_run(
        self,
        servers: Sequence[str],
        *,
        concurrency: int = 5,
        timeout_seconds: float = 1.2,
        verify_http: bool = True,
        fetch_status: bool = False,
        min_interval_days: int | None = None,
        limit: int = 200,
        max_total: int = 1000,
    ) -> KiwiProbeRunResult | None:
        planned = self.plan_probes(
            servers,
            min_interval_days=min_interval_days,
            limit=limit,
        )
        if not planned:
            return None

        cfg = load_config().kiwi_sources
        day, used_before, cap = self._registry.get_daily_probe_budget()
        allowed, _day, used_after, cap_db = self._registry.reserve_daily_probe_budget(
            len(planned),
            cap=int(cfg.daily_probe_cap),
        )
        planned = planned[: int(allowed)]
        if not planned:
            return None

        run_id = self._registry.start_scan_run(
            mode="service",
            concurrency=int(concurrency),
            timeout_seconds=float(timeout_seconds),
            verify_http=bool(verify_http),
            candidate_source="service",
            limit_n=len(planned),
            note=(
                f"daily_budget_day={day} "
                f"cap={cap_db} used_before={used_before} used_after={used_after}"
            ),
        )
        results = await scan_kiwi_reachability(
            planned,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            verify_http=verify_http,
            fetch_status=fetch_status,
        )
        _added, disabled = self._registry.record_scans(
            results,
            max_total=max_total,
            run_id=run_id,
        )
        expired = self._registry.expire_blocks()
        ok_results = [r for r in results if r.tcp_ok]
        avg_latency = (
            (sum(float(r.latency_ms or 0.0) for r in ok_results) / len(ok_results))
            if ok_results
            else None
        )
        self._registry.finish_scan_run(
            run_id,
            ok=len(ok_results),
            fail=len(results) - len(ok_results),
            avg_latency_ms=avg_latency,
            prune_disabled=disabled,
            expired_blocks=expired,
        )
        return KiwiProbeRunResult(
            run_id=str(run_id),
            planned=list(planned),
            scanned=list(results),
            ok=int(len(ok_results)),
            fail=int(len(results) - len(ok_results)),
            avg_latency_ms=float(avg_latency) if avg_latency is not None else None,
            daily_budget_day=str(day),
            daily_budget_cap=int(cap_db),
            daily_budget_used_before=int(used_before),
            daily_budget_used_after=int(used_after),
            prune_disabled=int(disabled),
            expired_blocks=int(expired),
        )

    def pick_best(
        self,
        *,
        target: int = 50,
        lookback_days: int = 90,
        min_scan_days: int = 0,
        include_disabled: bool = False,
    ) -> list[KiwiSourceQuality]:
        n = max(1, int(target))
        now_ts = int(time.time())
        summaries = self._registry.list_sources(
            limit=max(50, n * 4),
            enabled_only=not include_disabled,
        )
        min_days = max(0, int(min_scan_days))

        out: list[KiwiSourceQuality] = []
        for s in summaries:
            if self._registry.is_blocked(s.server):
                continue
            if (
                s.last_status_ts is not None
                and now_ts - int(s.last_status_ts) <= 300
                and s.last_users is not None
                and s.last_users_max is not None
                and int(s.last_users_max) <= _MAX_TRUSTED_USERS_MAX
                and int(s.last_users) >= int(s.last_users_max)
            ):
                continue
            scan_days = self._registry.count_scan_days(
                s.server,
                lookback_days=lookback_days,
            )
            if scan_days < min_days:
                continue
            eff = _compute_effective_score(score=s.score, scan_days=scan_days)
            out.append(
                KiwiSourceQuality(
                    server=s.server,
                    score=float(s.score),
                    effective_score=float(eff),
                    scan_days=int(scan_days),
                    last_seen_ts=s.last_seen_ts,
                    last_latency_ms=s.last_latency_ms,
                    last_users=s.last_users,
                    last_users_max=s.last_users_max,
                    last_status_ts=s.last_status_ts,
                    last_audio_ok_ts=s.last_audio_ok_ts,
                    audio_dropouts=int(s.audio_dropouts),
                    enabled=bool(s.enabled),
                )
            )

        out.sort(
            key=lambda x: (
                x.enabled is False,
                -x.effective_score,
                x.last_latency_ms is None,
                x.last_latency_ms if x.last_latency_ms is not None else 10_000.0,
                x.server,
            )
        )
        return out[:n]

    def get_insight(
        self,
        server: str,
        *,
        lookback_days: int = 90,
    ) -> KiwiSourceInsight | None:
        text = str(server).strip()
        if not text:
            return None
        items = self._registry.list_sources(limit=1000, enabled_only=False)
        by_server: dict[str, KiwiSourceSummary] = {i.server: i for i in items}
        summary = by_server.get(text)
        if summary is None:
            return None
        scan_days = self._registry.count_scan_days(text, lookback_days=lookback_days)
        eff = _compute_effective_score(score=summary.score, scan_days=scan_days)
        blocked = [
            b for b in self._registry.list_blocked(limit=500) if b.server == text
        ]
        return KiwiSourceInsight(
            server=text,
            enabled=bool(summary.enabled),
            score=float(summary.score),
            effective_score=float(eff),
            scan_days=int(scan_days),
            last_seen_ts=summary.last_seen_ts,
            last_users=summary.last_users,
            last_users_max=summary.last_users_max,
            last_status_ts=summary.last_status_ts,
            last_audio_ok_ts=summary.last_audio_ok_ts,
            audio_dropouts=int(summary.audio_dropouts),
            blocked=blocked,
        )
