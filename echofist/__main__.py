#!/usr/bin/env python3
"""
EchoFist 主入口文件
AI辅助等幅电报（CW）通讯软件
"""

import asyncio
import math
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

import click
import numpy as np
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from echofist import __version__
from echofist.config import load_config
from echofist.core.kiwi_client import (
    KiwiReachabilityResult,
    KiwiSDRClient,
    scan_kiwi_reachability,
)
from echofist.core.kiwi_sources import (
    KiwiScanHistoryRow,
    KiwiSourceRegistry,
    KiwiSourceSummary,
    fetch_public_kiwi_sources,
    parse_kiwi_public_text,
)
from echofist.core.morse_decoder import MorseDecoder
from echofist.core.qso_state import QSOStateMachine
from echofist.logger import setup_logger
from echofist.ui.dashboard import Dashboard

console = Console()
logger = setup_logger()

T = TypeVar("T")


class _KeyPoller:
    def __init__(self) -> None:
        self._is_windows = sys.platform.startswith("win")
        self._enabled = sys.stdin.isatty() and sys.stdout.isatty()
        self._old_term_settings: Any | None = None
        self._fd: int | None = None
        self._buffer: deque[str] = deque()

    def __enter__(self) -> "_KeyPoller":
        if not self._enabled:
            return self
        if self._is_windows:
            return self
        import termios
        import tty

        fd = sys.stdin.fileno()
        self._fd = fd
        self._old_term_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None:
        if not self._enabled:
            return
        if self._is_windows:
            return
        if self._old_term_settings is None:
            return
        import termios

        termios.tcsetattr(
            sys.stdin.fileno(),
            termios.TCSADRAIN,
            self._old_term_settings,
        )
        self._old_term_settings = None
        self._fd = None
        self._buffer.clear()

    def _read_char(self, timeout_seconds: float = 0.0) -> str | None:
        if not self._enabled:
            return None
        if self._is_windows:
            import msvcrt

            if not msvcrt.kbhit():
                return None
            return msvcrt.getwch()
        if self._buffer:
            return self._buffer.popleft()
        if self._fd is None:
            return None
        import os
        import select

        r, _, _ = select.select([self._fd], [], [], float(timeout_seconds))
        if not r:
            return None
        try:
            data = os.read(self._fd, 32)
        except BlockingIOError:
            return None
        if not data:
            return None
        text = data.decode("latin-1")
        self._buffer.extend(text)
        return self._buffer.popleft() if self._buffer else None

    def _get_event_windows(
        self,
    ) -> Literal["up", "down", "left", "right", "enter", "esc", "q", "b"] | None:
        ch = self._read_char()
        if ch is None:
            return None
        mapping: dict[str, Literal["enter", "esc", "q", "b"]] = {
            "q": "q",
            "Q": "q",
            "b": "b",
            "B": "b",
            "\r": "enter",
            "\x1b": "esc",
        }
        mapped = mapping.get(ch)
        if mapped is not None:
            return mapped
        if ch not in {"\x00", "\xe0"}:
            return None
        ch2 = self._read_char()
        if ch2 is None:
            return None
        arrow_map: dict[int, Literal["up", "down", "left", "right"]] = {
            72: "up",
            80: "down",
            75: "left",
            77: "right",
        }
        return arrow_map.get(ord(ch2))

    def _read_escape_sequence(self) -> str:
        seq = "\x1b"
        deadline = time.monotonic() + 0.12
        while len(seq) < 8 and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ch_next = self._read_char(timeout_seconds=min(0.02, remaining))
            if ch_next is None:
                break
            seq += ch_next
            if seq.startswith("\x1b[") and len(seq) >= 3 and ("@" <= seq[-1] <= "~"):
                break
        return seq

    def _parse_escape_sequence(
        self,
        seq: str,
    ) -> Literal["up", "down", "left", "right", "esc"] | None:
        if seq == "\x1b":
            return "esc"
        if seq.startswith("\x1b[") or seq.startswith("\x1bO"):
            mapping: dict[str, Literal["up", "down", "left", "right"]] = {
                "A": "up",
                "B": "down",
                "C": "right",
                "D": "left",
            }
            return mapping.get(seq[-1])
        return None

    def _get_event_posix(
        self,
    ) -> Literal["up", "down", "left", "right", "enter", "esc", "q", "b"] | None:
        ch = self._read_char()
        if ch is None:
            return None
        mapping: dict[str, Literal["enter", "q", "b"]] = {
            "q": "q",
            "Q": "q",
            "b": "b",
            "B": "b",
            "\n": "enter",
            "\r": "enter",
        }
        mapped = mapping.get(ch)
        if mapped is not None:
            return mapped
        if ch != "\x1b":
            return None
        seq = self._read_escape_sequence()
        return self._parse_escape_sequence(seq)

    def get_event(
        self,
    ) -> (
        Literal[
            "up",
            "down",
            "left",
            "right",
            "enter",
            "esc",
            "q",
            "b",
        ]
        | None
    ):
        if not self._enabled:
            return None
        if self._is_windows:
            return self._get_event_windows()
        return self._get_event_posix()


def _default_server_candidates() -> list[str]:
    return [
        "85.147.201.225:8073",
        "db0ovp.de:8073",
        "ve3hoa.ddns.net:8074",
        "kiwisdr.sdrham.com:8073",
        "sdr.oe3xbu.at:8073",
        "sdr.oe3xwu.at:8073",
        "kiwisdr.ka7u.net:8073",
        "sdr.k5qax.org:8073",
    ]


def _band_presets() -> dict[str, list[tuple[str, float]]]:
    return {
        "40m": [
            ("7.030 QRP Calling", 7.030),
            ("7.023 常用", 7.023),
        ],
        "20m": [
            ("14.060 QRP Calling", 14.060),
            ("14.100 NCDXF 信标", 14.100),
        ],
        "17m": [
            ("18.110 NCDXF 信标", 18.110),
        ],
        "15m": [
            ("21.060 QRP Calling", 21.060),
            ("21.150 NCDXF 信标", 21.150),
        ],
        "12m": [
            ("24.930 NCDXF 信标", 24.930),
        ],
        "10m": [
            ("28.060 QRP Calling", 28.060),
            ("28.200 NCDXF 信标", 28.200),
        ],
    }


def _select_menu(
    *,
    title: str,
    options: Sequence[T],
    render_option: "callable[[T], str]",
    default_index: int = 0,
) -> T | None:
    if not options:
        return None
    index = max(0, min(int(default_index), len(options) - 1))
    help_text = "↑↓ 选择 | Enter 确认 | Esc 取消"

    def build_panel() -> Panel:
        body = Text()
        for i, opt in enumerate(options):
            prefix = "› " if i == index else "  "
            style = "bold cyan" if i == index else "dim"
            body.append(prefix + render_option(opt) + "\n", style=style)
        body.append("\n" + help_text, style="dim")
        return Panel(body, title=title, border_style="cyan")

    with (
        _KeyPoller() as poller,
        Live(
            build_panel(),
            console=console,
            screen=False,
            refresh_per_second=20,
        ) as live,
    ):
        while True:
            event = poller.get_event()
            if event is None:
                continue
            if event == "esc":
                return None
            if event == "enter":
                return options[index]
            if event in {"up", "left"}:
                index = (index - 1) % len(options)
                live.update(build_panel())
            elif event in {"down", "right"}:
                index = (index + 1) % len(options)
                live.update(build_panel())


def _prompt_monitor_wizard(
    *,
    servers: tuple[str, ...],
    band: str | None,
    freq: float | None,
    with_default_servers: bool,
) -> tuple[tuple[str, ...], str, float]:
    presets = _band_presets()
    band_choices = list(presets.keys())

    resolved_servers = servers
    if not resolved_servers:
        candidates = _default_server_candidates() if with_default_servers else []
        primary = _select_menu(
            title="选择主服务器",
            options=candidates,
            render_option=lambda s: str(s),
            default_index=0,
        )
        if primary is None:
            raise click.Abort()
        resolved: list[str] = [str(primary)]
        remaining = [s for s in candidates if s != primary]
        if remaining:
            add_backup = _select_menu(
                title="添加备用服务器？",
                options=["不添加", "添加"],
                render_option=lambda s: str(s),
                default_index=0,
            )
            if add_backup == "添加":
                backup = _select_menu(
                    title="选择备用服务器",
                    options=remaining,
                    render_option=lambda s: str(s),
                    default_index=0,
                )
                if backup is None:
                    raise click.Abort()
                resolved.append(str(backup))
        resolved_servers = tuple(resolved)

    resolved_band = band
    if resolved_band is None and freq is None:
        selected_band = _select_menu(
            title="选择频段",
            options=band_choices,
            render_option=lambda s: str(s),
            default_index=(band_choices.index("40m") if "40m" in band_choices else 0),
        )
        if selected_band is None:
            raise click.Abort()
        resolved_band = str(selected_band)
    if resolved_band is None:
        resolved_band = "40m"

    resolved_freq = freq
    if resolved_freq is None:
        options = presets.get(resolved_band, presets["40m"])
        labels = [label for label, _ in options]
        choice = _select_menu(
            title="选择守听频点",
            options=labels,
            render_option=lambda s: str(s),
            default_index=0,
        )
        if choice is None:
            raise click.Abort()
        resolved_freq = dict(options)[str(choice)]

    return resolved_servers, resolved_band, float(resolved_freq)


class AudioPlayer:
    def __init__(
        self,
        input_rate: int,
        *,
        output_rate: int = 48000,
        gain: float = 0.4,
        max_buffer_seconds: float = 2.0,
    ) -> None:
        try:
            import sounddevice as sd
            from scipy.signal import resample_poly
        except ImportError as e:
            raise ImportError(
                "Audio playback requires sounddevice and scipy. "
                "Please install them with: pip install sounddevice scipy"
            ) from e

        self._sd = sd
        self._resample_poly = resample_poly
        self._input_rate = int(input_rate)
        self._output_rate = int(output_rate)
        self._gain = float(gain)
        self._lock = threading.Lock()
        self._buffer: deque[float] = deque()
        self._max_samples = int(self._output_rate * max_buffer_seconds)
        self._stream: Any | None = None
        self._logger = setup_logger()

        g = math.gcd(self._input_rate, self._output_rate)
        self._up = self._output_rate // g
        self._down = self._input_rate // g

    def start(self) -> None:
        def callback(
            outdata: Any,
            frames: int,
            _time: Any,
            status: Any,
        ) -> None:
            if status:
                self._logger.warning(f"Audio callback status: {status}")
            out = np.zeros(frames, dtype=np.float32)
            with self._lock:
                for i in range(frames):
                    if not self._buffer:
                        break
                    out[i] = self._buffer.popleft()
            outdata[:] = out.reshape(-1, 1)

        self._stream = self._sd.OutputStream(
            samplerate=self._output_rate,
            channels=1,
            dtype="float32",
            callback=callback,
            blocksize=1024,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            self._logger.error(f"Error stopping audio stream: {e}")
        finally:
            self._stream = None

    def buffered_ms(self) -> float:
        with self._lock:
            n = len(self._buffer)
        return (n / float(self._output_rate)) * 1000.0

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def feed(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return

        x = np.asarray(samples, dtype=np.float32)
        y = self._resample_poly(x, self._up, self._down).astype(
            np.float32,
            copy=False,
        )
        y = np.clip(y * self._gain, -1.0, 1.0)

        with self._lock:
            # 更高效地添加和裁剪
            self._buffer.extend(float(v) for v in y)
            overflow = len(self._buffer) - self._max_samples
            if overflow > 0:
                # 一次性移除多余的样本
                for _ in range(overflow):
                    self._buffer.popleft()


def print_banner() -> None:
    """打印应用横幅"""
    banner = Text()
    banner.append("EchoFist ", style="bold cyan")
    banner.append("(回声手迹)", style="italic")
    banner.append("\n")
    banner.append("AI辅助等幅电报（CW）通讯软件", style="dim")
    banner.append(f"\n版本: {__version__}", style="dim")

    console.print(Panel(banner, border_style="cyan"))
    console.print()


def print_kiwi_servers(servers: list) -> None:
    """显示可用的KiwiSDR服务器列表"""
    if not servers:
        console.print("[yellow]未找到可用的KiwiSDR服务器[/yellow]")
        return

    table = Table(
        title="可用的KiwiSDR服务器", show_header=True, header_style="bold magenta"
    )
    table.add_column("名称", style="cyan")
    table.add_column("位置", style="green")
    table.add_column("频率范围", style="yellow")
    table.add_column("用户数", style="blue")
    table.add_column("SNR", style="red")

    for server in servers[:10]:  # 只显示前10个
        table.add_row(
            server.get("name", "未知"),
            server.get("location", "未知"),
            server.get("freq_range", "0-30MHz"),
            f"{server.get('users', 0)}/{server.get('max_users', 4)}",
            f"{server.get('snr', 0)}dB",
        )

    console.print(table)


def print_kiwi_scan_results(results: list[KiwiReachabilityResult]) -> None:
    if not results:
        console.print("[yellow]未提供扫描目标[/yellow]")
        return

    table = Table(title="KiwiSDR 可达性扫描", show_header=True, header_style="bold")
    table.add_column("服务器", style="cyan", no_wrap=True)
    table.add_column("TCP", style="green", justify="center", no_wrap=True)
    table.add_column("延迟", style="yellow", justify="right", no_wrap=True)
    table.add_column("/VER", style="magenta", justify="right", no_wrap=True)
    table.add_column("TS", style="blue", justify="right", no_wrap=True)
    table.add_column("错误", style="red")

    def fmt_latency(v: float | None) -> str:
        if v is None:
            return "-"
        if v >= 1000.0:
            return f"{v/1000.0:.2f}s"
        return f"{v:.0f}ms"

    for r in sorted(
        results,
        key=lambda x: (0 if x.tcp_ok else 1, x.latency_ms or 10_000.0, x.server),
    ):
        tcp_text = "✓" if r.tcp_ok else "✗"
        http_text = "-" if r.http_status is None else str(r.http_status)
        ts_text = "-" if r.kiwi_ts is None else str(r.kiwi_ts)
        err = r.error or ""
        table.add_row(
            r.server,
            tcp_text,
            fmt_latency(r.latency_ms),
            http_text,
            ts_text,
            err,
        )

    console.print(table)


def print_kiwi_registry_summaries(items: list[KiwiSourceSummary]) -> None:
    if not items:
        console.print("[yellow]注册表为空[/yellow]")
        return

    table = Table(title="KiwiSDR 源注册表", show_header=True, header_style="bold")
    table.add_column("服务器", style="cyan", no_wrap=True)
    table.add_column("启用", style="green", justify="center", no_wrap=True)
    table.add_column("评分", style="yellow", justify="right", no_wrap=True)
    table.add_column("成功/失败", style="magenta", justify="right", no_wrap=True)
    table.add_column("连败", style="red", justify="right", no_wrap=True)
    table.add_column("延迟", style="blue", justify="right", no_wrap=True)

    def fmt_latency(v: float | None) -> str:
        if v is None:
            return "-"
        if v >= 1000.0:
            return f"{v/1000.0:.2f}s"
        return f"{v:.0f}ms"

    for it in items:
        table.add_row(
            it.server,
            "✓" if it.enabled else "✗",
            f"{it.score:.3f}",
            f"{it.successes}/{it.failures}",
            str(it.consecutive_failures),
            fmt_latency(it.last_latency_ms),
        )

    console.print(table)


def print_kiwi_history(rows: list[KiwiScanHistoryRow]) -> None:
    if not rows:
        console.print("[yellow]暂无历史记录[/yellow]")
        return

    table = Table(title="扫描历史", show_header=True, header_style="bold")
    table.add_column("时间", style="cyan", no_wrap=True)
    table.add_column("TCP", style="green", justify="center", no_wrap=True)
    table.add_column("延迟", style="yellow", justify="right", no_wrap=True)
    table.add_column("/VER", style="magenta", justify="right", no_wrap=True)
    table.add_column("TS", style="blue", justify="right", no_wrap=True)
    table.add_column("错误", style="red")

    def fmt_latency(v: float | None) -> str:
        if v is None:
            return "-"
        if v >= 1000.0:
            return f"{v/1000.0:.2f}s"
        return f"{v:.0f}ms"

    for r in rows:
        table.add_row(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts)),
            "✓" if r.tcp_ok else "✗",
            fmt_latency(r.latency_ms),
            "-" if r.http_status is None else str(r.http_status),
            "-" if r.kiwi_ts is None else str(r.kiwi_ts),
            r.error or "",
        )

    console.print(table)


async def monitor_mode(
    servers: tuple[str, ...],
    freq: float,
    bandwidth: int = 500,
    mode: str = "am",
    password: str = "",
    ident_user: str | None = None,
    play_audio: bool = False,
    audio_gain: float = 0.4,
    *,
    precheck: bool = False,
    precheck_concurrency: int = 5,
    precheck_timeout: float = 1.2,
    precheck_verify_http: bool = True,
    with_registry: bool = True,
    registry_target: int = 50,
    learn: bool = True,
    max_store: int = 1000,
    with_default_servers: bool = True,
    auto_switch: bool = True,
    interactive: bool = False,
) -> Literal["exit", "restart"]:
    """监听模式"""
    console.print("[green]启动监听模式[/green]")
    console.print(f"服务器: [cyan]{', '.join(servers)}[/cyan]")
    console.print(f"频率: [yellow]{freq} MHz[/yellow]")
    console.print(f"带宽: [blue]{bandwidth} Hz[/blue]")
    console.print(f"模式: [magenta]{mode.upper()}[/magenta]")
    console.print()

    registry: KiwiSourceRegistry | None = None
    registry_candidates: list[str] = []
    if (with_registry and not interactive) or (learn and precheck):
        registry = KiwiSourceRegistry()
    if with_registry and not interactive and registry is not None:
        registry_candidates = registry.pick_servers(target=registry_target)

    default_servers = _default_server_candidates()
    server_candidates: list[str] = []
    if interactive:
        base_candidates = list(servers)
    else:
        defaults = default_servers if with_default_servers else []
        base_candidates = list(servers) + registry_candidates + list(defaults)
    for s in base_candidates:
        if s and s not in server_candidates:
            server_candidates.append(s)
    if not server_candidates:
        raise click.ClickException("未提供可用的 KiwiSDR 服务器")

    if precheck and len(server_candidates) > 1:
        original_candidates = list(server_candidates)
        scan_results = await scan_kiwi_reachability(
            server_candidates,
            concurrency=precheck_concurrency,
            timeout_seconds=precheck_timeout,
            verify_http=precheck_verify_http,
        )
        added = 0
        disabled = 0
        if learn and registry is not None:
            added, disabled = registry.record_scans(
                scan_results,
                max_total=max_store,
            )
        by_server: dict[str, KiwiReachabilityResult] = {
            r.server: r for r in scan_results
        }
        reachable = [
            s for s in server_candidates if by_server.get(s) and by_server[s].tcp_ok
        ]
        reachable_sorted = sorted(
            reachable,
            key=lambda s: (
                by_server[s].latency_ms is None,
                by_server[s].latency_ms or 10_000.0,
                s,
            ),
        )
        reachable_set = set(reachable)
        unreachable = [s for s in server_candidates if s not in reachable_set]
        server_candidates = reachable_sorted + unreachable
        if server_candidates and original_candidates:
            best = server_candidates[0]
            if best != original_candidates[0]:
                console.print(f"[dim]预检：已选最稳源 {best}[/dim]")
        if learn and (added > 0 or disabled > 0):
            console.print(f"[dim]预检学习：新增 {added} 淘汰 {disabled}[/dim]")

    server_index = 0
    current_server = server_candidates[server_index]
    client: KiwiSDRClient | None = None
    decoder = MorseDecoder()
    dashboard = Dashboard()
    player: AudioPlayer | None = None
    audio_chunks_total = 0
    window_start = asyncio.get_running_loop().time()
    window_chunks = 0
    chunks_rate = 0.0
    last_rms = 0.0
    last_reconnect_attempt = 0.0
    reconnect_count = 0
    server_switch_count = 0
    consecutive_reconnects = 0
    last_good_audio_at = time.monotonic()
    last_switch_at = time.monotonic()
    server_fail_until: dict[str, float] = {}

    try:
        app_config = load_config()
        reconnect_warn_seconds = 6.0
        reconnect_after_seconds = 20.0
        switch_after_seconds = 60.0
        server_cooldown_seconds = float(app_config.kiwi_sdr.server_cooldown_seconds)
        min_switch_interval_seconds = 30.0

        async def connect_and_start(
            target_server: str,
            *,
            count_as_reconnect: bool,
        ) -> KiwiSDRClient:
            nonlocal reconnect_count, consecutive_reconnects
            new_client = KiwiSDRClient(
                target_server,
                password=password,
                ident_user=ident_user,
            )
            await new_client.connect()
            await new_client.set_frequency(freq)
            await new_client.set_mode(mode)
            await new_client.set_bandwidth(bandwidth)
            await new_client.start_audio_stream()
            if count_as_reconnect:
                reconnect_count += 1
                consecutive_reconnects += 1
            return new_client

        connected = False
        for idx, candidate in enumerate(server_candidates):
            try:
                server_index = idx
                current_server = candidate
                dashboard.update(
                    is_connected=False,
                    connection_state="连接中",
                    server=current_server,
                    play_audio_enabled=play_audio,
                    error_message=None,
                )
                client = await connect_and_start(
                    current_server,
                    count_as_reconnect=False,
                )
                connected = True
                break
            except Exception as e:
                logger.warning(f"初始连接失败: {candidate} ({e})")
                server_fail_until[candidate] = (
                    time.monotonic() + server_cooldown_seconds
                )
                client = None

        if not connected or client is None:
            if interactive:
                console.print(
                    Panel(
                        "无法连接到所选源，请返回重新选择。",
                        title="连接失败",
                        border_style="red",
                    )
                )
                return "restart"
            raise ConnectionError("无法连接到任意 KiwiSDR 服务器")
        dashboard.update(
            is_connected=True,
            connection_state=None,
            server=current_server,
            play_audio_enabled=play_audio,
            error_message=None,
        )
        console.print("[green]✓ 已连接到KiwiSDR服务器[/green]")

        if play_audio:
            player = AudioPlayer(
                input_rate=decoder.sample_rate,
                gain=audio_gain,
            )
            player.start()

        # 创建实时显示
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="waterfall", ratio=2),
            Layout(name="decoded", ratio=3),
            Layout(name="status", size=13),
        )

        control: Literal["exit", "restart"] = "exit"
        with (
            _KeyPoller() as poller,
            Live(
                layout,
                console=console,
                screen=False,
                refresh_per_second=4,
            ),
        ):
            while True:
                event = poller.get_event()
                if event == "q":
                    control = "exit"
                    break
                if event == "b":
                    control = "restart"
                    break

                # 获取音频数据
                audio_data = await client.get_audio_chunk(timeout_seconds=0.25)
                now = asyncio.get_running_loop().time()
                audio_age = client.get_last_audio_age_seconds()
                if audio_age < 0.5:
                    last_good_audio_at = time.monotonic()
                    consecutive_reconnects = 0
                dt = now - window_start
                if dt >= 1.0:
                    chunks_rate = window_chunks / dt
                    window_start = now
                    window_chunks = 0
                if audio_data is None:
                    stalled_seconds = time.monotonic() - last_good_audio_at
                    should_reconnect = audio_age >= reconnect_after_seconds
                    should_switch = (
                        auto_switch
                        and len(server_candidates) > 1
                        and stalled_seconds >= switch_after_seconds
                        and consecutive_reconnects >= 2
                        and (time.monotonic() - last_switch_at)
                        >= min_switch_interval_seconds
                    )
                    if should_switch:
                        old_server = current_server
                        n_candidates = len(server_candidates)
                        next_index = (server_index + 1) % n_candidates
                        candidate_indices = [
                            (next_index + i) % n_candidates for i in range(n_candidates)
                        ]
                        consecutive_reconnects = 0
                        dashboard.update(
                            is_connected=False,
                            connection_state="切换中",
                            server=current_server,
                            error_message=(
                                f"服务器不稳定，准备切换 {old_server} → ..."
                            ),
                            reconnect_count=reconnect_count,
                            server_switch_count=server_switch_count,
                        )
                        layout["header"].update(dashboard.render_header())
                        layout["waterfall"].update(dashboard.render_waterfall())
                        layout["decoded"].update(dashboard.render_decoded_text())
                        layout["status"].update(dashboard.render_status())
                        if player is not None:
                            player.clear()
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(0.4)
                        switched = False
                        last_error: str | None = None
                        for idx in candidate_indices:
                            candidate = server_candidates[idx]
                            fail_until = server_fail_until.get(candidate, 0.0)
                            if time.monotonic() < fail_until:
                                continue
                            dashboard.update(
                                server=candidate,
                                error_message=(
                                    f"服务器不稳定，切换 {old_server} → {candidate}"
                                ),
                            )
                            layout["header"].update(dashboard.render_header())
                            layout["status"].update(dashboard.render_status())
                            try:
                                client = await connect_and_start(
                                    candidate,
                                    count_as_reconnect=True,
                                )
                                current_server = candidate
                                server_index = idx
                                server_switch_count += 1
                                if registry is not None:
                                    registry.record_monitor_event(
                                        server=old_server,
                                        event_type="switch",
                                        from_server=old_server,
                                        to_server=candidate,
                                        detail=f"stalled={stalled_seconds:.1f}",
                                        max_total=max_store,
                                    )
                                loop_now = asyncio.get_running_loop().time()
                                window_start = loop_now
                                window_chunks = 0
                                dashboard.update(
                                    is_connected=True,
                                    connection_state=None,
                                    server=current_server,
                                    error_message=None,
                                    server_switch_count=server_switch_count,
                                )
                                last_good_audio_at = time.monotonic()
                                last_switch_at = time.monotonic()
                                switched = True
                                break
                            except Exception as e:
                                last_error = str(e)
                                server_fail_until[candidate] = (
                                    time.monotonic() + server_cooldown_seconds
                                )
                                continue

                        if not switched:
                            dashboard.update(
                                error_message=(f"切换失败（{last_error}），稍后重试"),
                                server_switch_count=server_switch_count,
                            )
                        await asyncio.sleep(0.2)
                        continue

                    if should_reconnect:
                        mono_now = time.monotonic()
                        if mono_now - last_reconnect_attempt >= 2.0:
                            last_reconnect_attempt = mono_now
                            dashboard.update(
                                frequency=freq,
                                bandwidth=bandwidth,
                                mode=mode,
                                signal_strength=client.get_signal_strength(),
                                is_connected=False,
                                connection_state="重连中",
                                error_message=(
                                    f"音频断流 {audio_age:.1f}s，正在重连..."
                                ),
                                server=current_server,
                                audio_chunks_total=audio_chunks_total,
                                audio_chunks_rate=chunks_rate,
                                audio_queue_size=client.get_audio_queue_size(),
                                audio_rms=last_rms,
                                audio_last_age=audio_age,
                                playback_buffer_ms=(
                                    player.buffered_ms() if player else 0.0
                                ),
                                reconnect_count=reconnect_count,
                                server_switch_count=server_switch_count,
                            )
                            layout["header"].update(dashboard.render_header())
                            layout["waterfall"].update(dashboard.render_waterfall())
                            layout["decoded"].update(dashboard.render_decoded_text())
                            layout["status"].update(dashboard.render_status())
                            if player is not None:
                                player.clear()
                            if registry is not None:
                                registry.record_monitor_event(
                                    server=current_server,
                                    event_type="reconnect",
                                    detail=f"audio_age={audio_age:.1f}",
                                    max_total=max_store,
                                )
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                            await asyncio.sleep(0.4)
                            try:
                                client = await connect_and_start(
                                    current_server,
                                    count_as_reconnect=True,
                                )
                                loop_now = asyncio.get_running_loop().time()
                                window_start = loop_now
                                window_chunks = 0
                                dashboard.update(
                                    is_connected=True,
                                    connection_state=None,
                                    server=current_server,
                                    error_message=None,
                                )
                                last_good_audio_at = time.monotonic()
                            except Exception as e:
                                server_fail_until[current_server] = (
                                    time.monotonic() + server_cooldown_seconds
                                )
                                if auto_switch and len(server_candidates) > 1:
                                    dashboard.update(
                                        connection_state="切换中",
                                        error_message=(f"重连失败（{e}），尝试切换..."),
                                    )
                                else:
                                    dashboard.update(
                                        error_message=f"重连失败（{e}）",
                                    )
                    else:
                        if audio_age >= reconnect_warn_seconds:
                            dashboard.update(
                                error_message=(
                                    f"音频暂停 {audio_age:.1f}s，等待恢复..."
                                ),
                            )
                    dashboard.update(
                        frequency=freq,
                        bandwidth=bandwidth,
                        mode=mode,
                        signal_strength=client.get_signal_strength(),
                        is_connected=client.connected,
                        connection_state=None,
                        server=current_server,
                        audio_chunks_total=audio_chunks_total,
                        audio_chunks_rate=chunks_rate,
                        audio_queue_size=client.get_audio_queue_size(),
                        audio_rms=last_rms,
                        audio_last_age=audio_age,
                        playback_buffer_ms=(player.buffered_ms() if player else 0.0),
                        reconnect_count=reconnect_count,
                        server_switch_count=server_switch_count,
                    )
                    layout["header"].update(dashboard.render_header())
                    layout["waterfall"].update(dashboard.render_waterfall())
                    layout["decoded"].update(dashboard.render_decoded_text())
                    layout["status"].update(dashboard.render_status())
                    await asyncio.sleep(0.1)
                    continue
                drained = client.drain_audio_chunks(max_chunks=8)
                if drained:
                    drained.insert(0, audio_data)
                    audio_data = drained[-1]
                else:
                    drained = [audio_data]

                if player is not None:
                    for chunk in drained:
                        player.feed(chunk)

                # 解码摩尔斯电码
                decoded_text, confidence = decoder.decode(audio_data)
                audio_chunks_total += len(drained)
                window_chunks += len(drained)
                last_rms = float(np.sqrt(np.mean(audio_data * audio_data)))

                # 更新仪表板
                dashboard.update(
                    frequency=freq,
                    bandwidth=bandwidth,
                    mode=mode,
                    decoded_text=decoded_text,
                    confidence=confidence,
                    signal_strength=client.get_signal_strength(),
                    is_connected=client.connected,
                    connection_state=None,
                    server=current_server,
                    audio_chunks_total=audio_chunks_total,
                    audio_chunks_rate=chunks_rate,
                    audio_queue_size=client.get_audio_queue_size(),
                    audio_rms=last_rms,
                    audio_last_age=audio_age,
                    playback_buffer_ms=player.buffered_ms() if player else 0.0,
                    reconnect_count=reconnect_count,
                    server_switch_count=server_switch_count,
                )

                # 渲染布局
                layout["header"].update(dashboard.render_header())
                layout["waterfall"].update(dashboard.render_waterfall())
                layout["decoded"].update(dashboard.render_decoded_text())
                layout["status"].update(dashboard.render_status())

                await asyncio.sleep(0.25)

        return control

    except KeyboardInterrupt:
        console.print("\n[yellow]正在停止监听...[/yellow]")
        return "exit"
    except Exception as e:
        logger.error(f"监听模式错误: {e}")
        console.print(f"[red]错误: {e}[/red]")
        return "exit"
    finally:
        if client:
            await client.disconnect()
        if player is not None:
            player.stop()
        if registry is not None:
            registry.close()


async def probe_mode(
    server: str,
    freq: float,
    bandwidth: int,
    mode: str,
    password: str,
    ident_user: str | None,
    seconds: float,
) -> None:
    client = KiwiSDRClient(server, password=password, ident_user=ident_user)
    decoder = MorseDecoder()

    await client.connect()
    await client.set_frequency(freq)
    await client.set_mode(mode)
    await client.set_bandwidth(bandwidth)
    await client.start_audio_stream()

    start = asyncio.get_running_loop().time()
    chunks = 0
    decoded_lines = 0
    last_rssi = 0.0

    while asyncio.get_running_loop().time() - start < seconds:
        audio_data = await client.get_audio_chunk()
        if audio_data is None:
            await asyncio.sleep(0.05)
            continue
        chunks += 1
        last_rssi = client.get_signal_strength()
        decoded_text, confidence = decoder.decode(audio_data)
        if decoded_text and confidence >= 0.7:
            decoded_lines += 1
            console.print(f"[dim]{decoded_text}[/dim]")

    await client.disconnect()
    console.print(
        Panel(
            "\n".join(
                [
                    f"服务器: {server}",
                    f"模式: {mode.upper()}",
                    f"频率: {freq:.3f} MHz",
                    f"采样块: {chunks}",
                    f"最后信号强度: {last_rssi:.1f} dBm",
                    f"高置信度解码行数: {decoded_lines}",
                ]
            ),
            border_style="cyan",
            title="Probe 结果",
        )
    )


async def qso_mode(
    server: str,
    freq: float,
    callsign: str | None = None,
    auto_suggest: bool = False,
    password: str = "",
    play_audio: bool = False,
    audio_gain: float = 0.4,
) -> None:
    """QSO模式"""
    console.print("[green]启动QSO模式[/green]")
    console.print(f"服务器: [cyan]{server}[/cyan]")
    console.print(f"频率: [yellow]{freq} MHz[/yellow]")
    if callsign:
        console.print(f"呼号: [blue]{callsign}[/blue]")
    console.print()

    # 创建状态机
    state_machine = QSOStateMachine(callsign=callsign)
    client = KiwiSDRClient(server, password=password, ident_user=callsign)
    decoder = MorseDecoder()
    player: AudioPlayer | None = None

    try:
        # 连接服务器
        await client.connect()
        console.print("[green]✓ 已连接到KiwiSDR服务器[/green]")

        # 设置频率
        await client.set_frequency(freq)
        await client.set_mode("cw")

        # 启动音频流
        await client.start_audio_stream()

        if play_audio:
            player = AudioPlayer(
                input_rate=decoder.sample_rate,
                gain=audio_gain,
            )
            player.start()

        console.print("[cyan]等待CW信号...[/cyan]")
        console.print("按 Ctrl+C 停止")
        console.print()

        while True:
            # 获取音频数据
            audio_data = await client.get_audio_chunk()
            if audio_data is None:
                await asyncio.sleep(0.1)
                continue
            drained = client.drain_audio_chunks(max_chunks=8)
            if drained:
                drained.insert(0, audio_data)
                audio_data = drained[-1]
            else:
                drained = [audio_data]

            if player is not None:
                for chunk in drained:
                    player.feed(chunk)

            # 解码摩尔斯电码
            decoded_text, confidence = decoder.decode(audio_data)

            if decoded_text and confidence > 0.7:
                # 处理解码文本
                state_machine.process_text(decoded_text)

                # 显示当前状态
                console.print(f"[dim]{decoded_text}[/dim]")

                # 检查是否需要应答
                if auto_suggest and state_machine.has_suggestion():
                    suggestion = state_machine.get_suggestion()
                    console.print(
                        f"[yellow]建议应答:[/yellow] [green]{suggestion}[/green]"
                    )

                    # 这里可以添加自动发报逻辑
                    # await client.send_cw(suggestion)

            await asyncio.sleep(0.1)

    except KeyboardInterrupt:
        console.print("\n[yellow]正在停止QSO...[/yellow]")
    except Exception as e:
        logger.error(f"QSO模式错误: {e}")
        console.print(f"[red]错误: {e}[/red]")
    finally:
        if client:
            await client.disconnect()
        if player is not None:
            player.stop()


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """EchoFist - AI辅助等幅电报（CW）通讯软件"""
    pass


@cli.command()
def version() -> None:
    """显示版本信息"""
    print_banner()


@cli.command()
@click.option("--callsign", "-c", default=None, help="您的呼号")
@click.option("--email", "-e", default=None, help="您的邮箱")
@click.option("--locator", "-l", default=None, help="您的网格定位（可选）")
@click.option("--new-key/--no-new-key", default=True, help="生成新的本地API密钥")
def register(
    callsign: str | None,
    email: str | None,
    locator: str | None,
    new_key: bool,
) -> None:
    """注册本地用户信息并写入配置文件"""
    from echofist.config import generate_api_key_record, get_config_path, update_config

    config = load_config()

    resolved_callsign = (
        callsign
        or click.prompt(
            "呼号",
            default=config.qso.default_callsign or "",
        ).strip()
    )
    resolved_email = (
        email
        or click.prompt(
            "邮箱",
            default=config.qso.operator_email or "",
        ).strip()
    )
    resolved_locator = locator
    if resolved_locator is None:
        resolved_locator = click.prompt(
            "网格定位（可选）",
            default=config.qso.default_locator or "",
            show_default=bool(config.qso.default_locator),
        ).strip()

    updates: dict[str, Any] = {
        "qso": {
            "default_callsign": resolved_callsign or None,
            "operator_email": resolved_email or None,
            "default_locator": resolved_locator or None,
        }
    }

    api_key: str | None = None
    if new_key:
        api_key, salt_b64, digest_b64 = generate_api_key_record()
        updates["security"] = {
            "api_key_salt_b64": salt_b64,
            "api_key_hash_b64": digest_b64,
        }

    update_config(updates)

    path = get_config_path()
    console.print(Panel(f"已写入配置文件：{path}", border_style="green"))
    if api_key:
        console.print(
            Panel(
                f"本地API密钥（仅显示一次）：\n{api_key}",
                border_style="yellow",
            )
        )


@cli.command()
@click.option("--server", "-s", required=True, help="KiwiSDR服务器地址")
@click.option("--freq", "-f", type=float, default=7.023, help="频率 (MHz)")
@click.option("--bandwidth", "-b", type=int, default=500, help="带宽 (Hz)")
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["am", "cw", "usb", "lsb"]),
    default="cw",
    help="接收模式",
)
@click.option("--password", "-p", default="", help="服务器密码（如有）")
@click.option("--user", "-u", default=None, help="显示在服务器上的用户标识")
@click.option("--seconds", "-t", type=float, default=8.0, help="探测时长（秒）")
def probe(
    server: str,
    freq: float,
    bandwidth: int,
    mode: str,
    password: str,
    user: str | None,
    seconds: float,
) -> None:
    """快速探测：连接并打印音频接收/信号强度（不进入TUI）"""
    print_banner()
    asyncio.run(
        probe_mode(
            server=server,
            freq=freq,
            bandwidth=bandwidth,
            mode=mode,
            password=password,
            ident_user=user,
            seconds=seconds,
        )
    )


@cli.command()
@click.option("--list", "-l", is_flag=True, help="列出可用的KiwiSDR服务器")
def servers(list: bool) -> None:
    """管理KiwiSDR服务器"""
    if list:
        # 这里应该从网络获取服务器列表
        # 暂时使用示例数据
        example_servers = [
            {
                "name": "PA3GJX",
                "location": "IJsselmuiden, Netherlands",
                "freq_range": "0-30MHz",
                "users": 2,
                "max_users": 4,
                "snr": 20,
            },
            {
                "name": "G0MJW",
                "location": "UK",
                "freq_range": "0-30MHz",
                "users": 1,
                "max_users": 4,
                "snr": 15,
            },
        ]
        print_kiwi_servers(example_servers)


@cli.group()
def sources() -> None:
    """管理 KiwiSDR 源注册表（增量积累与淘汰）"""
    pass


@sources.command("fetch")
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=50,
    show_default=True,
    help="本次最多引入的新源数量（温和增量）",
)
@click.option(
    "--timeout",
    type=float,
    default=8.0,
    show_default=True,
    help="拉取公共目录的超时（秒）",
)
@click.option(
    "--html-file",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    default=None,
    help="从本地HTML文件提取（用于被反爬/验证码拦截时的离线导入）",
)
@click.option(
    "--html-stdin",
    is_flag=True,
    help="从标准输入读取HTML并提取（用于被反爬/验证码拦截时的离线导入）",
)
@click.option(
    "--max-total",
    type=click.IntRange(50, 1000),
    default=1000,
    show_default=True,
    help="注册表总量上限（超过将删除最差记录）",
)
def sources_fetch(
    limit: int,
    timeout: float,
    html_file: str | None,
    html_stdin: bool,
    max_total: int,
) -> None:
    """从公共目录增量引入 KiwiSDR 源（去重 + 端口范围过滤）"""
    print_banner()
    registry = KiwiSourceRegistry()
    try:
        if html_stdin:
            html = sys.stdin.read()
            candidates = parse_kiwi_public_text(html)
        elif html_file:
            html = Path(html_file).read_text(encoding="utf-8", errors="ignore")
            candidates = parse_kiwi_public_text(html)
        else:
            candidates = asyncio.run(fetch_public_kiwi_sources(timeout_seconds=timeout))
        added = registry.add_sources(candidates, daily_cap=limit, max_total=max_total)
        stats = registry.stats()
    finally:
        registry.close()
    console.print(
        Panel(
            f"新增：{added}\n启用：{stats['enabled']}\n总量：{stats['total']}",
            title="拉取完成",
            border_style="green",
        )
    )
    if not candidates:
        console.print(
            Panel(
                "未提取到任何服务器条目。\n"
                "可能原因：公共目录返回验证码/反爬页面，或网络环境无法直连。\n\n"
                "可选方案：\n"
                "1) 浏览器打开 http://kiwisdr.com/public/ 或 https://rx.kiwisdr.com/ ，"
                "保存页面HTML后用 --html-file 导入\n"
                "2) 将HTML内容粘贴到命令行并用 --html-stdin 导入（Ctrl-D 结束）",
                title="提示",
                border_style="yellow",
            )
        )


@sources.command("list")
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=50,
    show_default=True,
    help="显示数量",
)
@click.option(
    "--all/--enabled-only",
    default=False,
    help="是否包含已淘汰/禁用源",
)
def sources_list(limit: int, all: bool) -> None:
    """列出注册表中的源（按评分排序）"""
    print_banner()
    registry = KiwiSourceRegistry()
    try:
        items = registry.list_sources(limit=limit, enabled_only=not all)
        stats = registry.stats()
    finally:
        registry.close()
    console.print(
        Panel(
            f"启用：{stats['enabled']} / 总量：{stats['total']}",
            title="注册表统计",
            border_style="cyan",
        )
    )
    print_kiwi_registry_summaries(items)


@sources.command("prune")
@click.option(
    "--max-total",
    type=click.IntRange(50, 1000),
    default=1000,
    show_default=True,
    help="注册表总量上限",
)
def sources_prune(max_total: int) -> None:
    """淘汰不稳定源并维持上限"""
    print_banner()
    registry = KiwiSourceRegistry()
    try:
        disabled = registry.prune(max_total=max_total)
        stats = registry.stats()
    finally:
        registry.close()
    console.print(
        Panel(
            f"本次禁用：{disabled}\n启用：{stats['enabled']}\n总量：{stats['total']}",
            title="淘汰完成",
            border_style="yellow",
        )
    )


@sources.command("history")
@click.option("--server", "-s", required=True, help="KiwiSDR服务器地址")
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=30,
    show_default=True,
    help="显示条数",
)
def sources_history(server: str, limit: int) -> None:
    """查看单个源的扫描历史"""
    print_banner()
    registry = KiwiSourceRegistry()
    try:
        rows = registry.list_history(server, limit=limit)
    finally:
        registry.close()
    console.print(Panel(server, title="目标服务器", border_style="cyan"))
    print_kiwi_history(rows)


@cli.command()
@click.option(
    "--server",
    "-s",
    multiple=True,
    default=(),
    help="KiwiSDR服务器地址（可重复多次，按顺序作为候选）",
)
@click.option(
    "--with-default-servers/--no-default-servers",
    default=True,
    help="是否附加内置候选服务器列表",
)
@click.option(
    "--with-registry/--no-with-registry",
    default=True,
    help="是否附加本地注册表中的源（用于逐日积累的候选池）",
)
@click.option(
    "--registry-target",
    type=click.IntRange(1, 200),
    default=50,
    show_default=True,
    help="从注册表挑选的目标数量（建议 ≥ 50）",
)
@click.option(
    "--concurrency",
    type=click.IntRange(1, 10),
    default=5,
    show_default=True,
    help="并发量（推荐 5，上限 10）",
)
@click.option(
    "--timeout",
    type=float,
    default=1.2,
    show_default=True,
    help="单个目标超时（秒）",
)
@click.option(
    "--verify-http/--no-verify-http",
    default=True,
    help="是否请求 /VER 以获取 KiwiSDR 时间戳信息",
)
@click.option(
    "--learn/--no-learn",
    default=True,
    help="是否把扫描结果写入本地注册表（用于淘汰与累计）",
)
@click.option(
    "--max-store",
    type=click.IntRange(50, 1000),
    default=1000,
    show_default=True,
    help="注册表总量上限",
)
def scan(
    server: tuple[str, ...],
    with_default_servers: bool,
    with_registry: bool,
    registry_target: int,
    concurrency: int,
    timeout: float,
    verify_http: bool,
    learn: bool,
    max_store: int,
) -> None:
    """扫描 KiwiSDR 源的网络可达性（低开销：TCP 直连 + 可选 /VER）"""
    print_banner()
    targets: list[str] = []
    results: list[KiwiReachabilityResult] = []
    registry: KiwiSourceRegistry | None = None
    try:
        if with_registry or learn:
            registry = KiwiSourceRegistry()
        if with_registry and registry is not None:
            for s in registry.pick_servers(target=registry_target):
                if s and s not in targets:
                    targets.append(s)
        defaults = _default_server_candidates() if with_default_servers else []
        for s in list(server) + defaults:
            if s and s not in targets:
                targets.append(s)

        results = asyncio.run(
            scan_kiwi_reachability(
                targets,
                concurrency=concurrency,
                timeout_seconds=timeout,
                verify_http=verify_http,
            )
        )
        if learn and registry is not None:
            added, disabled = registry.record_scans(results, max_total=max_store)
            console.print(
                Panel(
                    f"新增：{added}\n本次禁用：{disabled}",
                    title="学习完成",
                    border_style="green",
                )
            )
    finally:
        if registry is not None:
            registry.close()
    print_kiwi_scan_results(results)


@cli.command()
@click.option(
    "--server",
    "-s",
    multiple=True,
    default=(),
    help="KiwiSDR服务器地址（可重复多次，按顺序作为候选）",
)
@click.option(
    "--band",
    type=click.Choice(["40m", "20m", "17m", "15m", "12m", "10m"]),
    default=None,
    help="频段预设",
)
@click.option("--freq", "-f", type=float, default=None, help="频率 (MHz)")
@click.option("--bandwidth", "-b", type=int, default=500, help="带宽 (Hz)")
@click.option("--password", "-p", default="", help="服务器密码（如有）")
@click.option("--user", "-u", default=None, help="显示在服务器上的用户标识")
@click.option("--play-audio", is_flag=True, help="播放接收到的音频到默认输出设备")
@click.option("--audio-gain", type=float, default=0.4, help="播放音量增益 (0-2)")
@click.option("--wizard/--no-wizard", default=True, help="启动前使用交互式参数向导")
@click.option(
    "--with-default-servers/--no-default-servers",
    default=True,
    help="是否附加内置候选服务器列表作为自动切换的后备",
)
@click.option(
    "--with-registry/--no-with-registry",
    default=True,
    help="是否附加本地注册表的候选源作为自动切换的后备",
)
@click.option(
    "--registry-target",
    type=click.IntRange(1, 200),
    default=50,
    show_default=True,
    help="从注册表挑选的目标数量",
)
@click.option(
    "--auto-switch/--no-auto-switch",
    default=True,
    help="断流时是否自动切换到下一个候选服务器",
)
@click.option(
    "--precheck/--no-precheck",
    default=True,
    help="启动时并发预检候选服务器可达性（降低逐个长超时）",
)
@click.option(
    "--precheck-concurrency",
    type=click.IntRange(1, 10),
    default=5,
    show_default=True,
    help="预检并发量（推荐 5，上限 10）",
)
@click.option(
    "--precheck-timeout",
    type=float,
    default=1.2,
    show_default=True,
    help="预检单个目标超时（秒）",
)
@click.option(
    "--precheck-verify-http/--no-precheck-verify-http",
    default=True,
    help="预检时是否请求 /VER",
)
@click.option(
    "--learn/--no-learn",
    default=True,
    help="启动预检结果是否写入本地注册表（用于增量积累与淘汰）",
)
@click.option(
    "--max-store",
    type=click.IntRange(50, 1000),
    default=1000,
    show_default=True,
    help="注册表总量上限",
)
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["am", "cw", "usb", "lsb"]),
    default="cw",
    help="接收模式",
)
def monitor(
    server: tuple[str, ...],
    band: str | None,
    freq: float | None,
    bandwidth: int,
    password: str,
    user: str | None,
    play_audio: bool,
    audio_gain: float,
    with_default_servers: bool,
    with_registry: bool,
    registry_target: int,
    auto_switch: bool,
    precheck: bool,
    precheck_concurrency: int,
    precheck_timeout: float,
    precheck_verify_http: bool,
    learn: bool,
    max_store: int,
    wizard: bool,
    mode: str,
) -> None:
    """监听模式 - 实时解码CW信号"""
    print_banner()
    resolved_servers = server
    resolved_band = band
    resolved_freq: float | None = freq
    use_wizard = wizard and sys.stdin.isatty() and sys.stdout.isatty()

    if use_wizard:
        while True:
            resolved_servers, resolved_band, resolved_freq = _prompt_monitor_wizard(
                servers=resolved_servers,
                band=resolved_band,
                freq=resolved_freq,
                with_default_servers=with_default_servers,
            )
            action = asyncio.run(
                monitor_mode(
                    resolved_servers,
                    float(resolved_freq),
                    bandwidth,
                    mode,
                    password,
                    user,
                    play_audio,
                    audio_gain,
                    precheck=precheck,
                    precheck_concurrency=precheck_concurrency,
                    precheck_timeout=precheck_timeout,
                    precheck_verify_http=precheck_verify_http,
                    with_registry=with_registry,
                    registry_target=registry_target,
                    learn=learn,
                    max_store=max_store,
                    with_default_servers=with_default_servers,
                    auto_switch=auto_switch,
                    interactive=True,
                )
            )
            if action == "restart":
                resolved_servers = ()
                resolved_band = None
                resolved_freq = None
                continue
            break
    else:
        if not resolved_servers:
            raise click.ClickException("缺少 --server/-s，或启用 --wizard 交互选择")
        if resolved_freq is None:
            presets = _band_presets()
            if resolved_band and resolved_band in presets:
                resolved_freq = presets[resolved_band][0][1]
            else:
                raise click.ClickException("缺少 --freq/-f，或启用 --wizard 交互选择")
        asyncio.run(
            monitor_mode(
                resolved_servers,
                float(resolved_freq),
                bandwidth,
                mode,
                password,
                user,
                play_audio,
                audio_gain,
                precheck=precheck,
                precheck_concurrency=precheck_concurrency,
                precheck_timeout=precheck_timeout,
                precheck_verify_http=precheck_verify_http,
                with_registry=with_registry,
                registry_target=registry_target,
                learn=learn,
                max_store=max_store,
                with_default_servers=with_default_servers,
                auto_switch=auto_switch,
            )
        )


@cli.command()
@click.option("--server", "-s", required=True, help="KiwiSDR服务器地址")
@click.option("--freq", "-f", type=float, default=7.023, help="频率 (MHz)")
@click.option("--callsign", "-c", help="您的呼号")
@click.option("--password", "-p", default="", help="服务器密码（如有）")
@click.option("--play-audio", is_flag=True, help="播放接收到的音频到默认输出设备")
@click.option("--audio-gain", type=float, default=0.4, help="播放音量增益 (0-2)")
@click.option("--auto-suggest", "-a", is_flag=True, help="自动提供应答建议")
def qso(
    server: str,
    freq: float,
    callsign: str | None,
    password: str,
    play_audio: bool,
    audio_gain: float,
    auto_suggest: bool,
) -> None:
    """QSO模式 - 半自动通联"""
    print_banner()
    asyncio.run(
        qso_mode(
            server,
            freq,
            callsign,
            auto_suggest,
            password,
            play_audio,
            audio_gain,
        )
    )


@cli.command()
@click.option("--freq-range", "-r", default="7.000-7.030", help="频率范围 (MHz)")
@click.option("--contest", "-c", is_flag=True, help="比赛模式")
def auto(freq_range: str, contest: bool) -> None:
    """全自动模式 - 自动扫描和应答"""
    print_banner()
    console.print("[yellow]全自动模式正在开发中...[/yellow]")
    console.print(f"频率范围: [cyan]{freq_range}[/cyan]")
    console.print(f"比赛模式: [green]{'是' if contest else '否'}[/green]")


def main() -> None:
    """主函数"""
    try:
        cli()
    except KeyboardInterrupt:
        console.print("\n[yellow]程序已终止[/yellow]")
        sys.exit(0)
    except Exception as e:
        logger.error(f"程序错误: {e}")
        console.print(f"[red]错误: {e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
