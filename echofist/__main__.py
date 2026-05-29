#!/usr/bin/env python3
"""
EchoFist 主入口文件
AI辅助等幅电报（CW）通讯软件
"""

import asyncio
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

try:
    import click
    import numpy as np
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ModuleNotFoundError as e:
    print(
        "缺少运行依赖，EchoFist 无法启动。\n"
        f"错误：{e}\n\n"
        "建议使用虚拟环境并安装依赖：\n"
        "  python3 -m venv .venv\n"
        "  source .venv/bin/activate\n"
        "  pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from e

try:
    from echofist import __version__
    from echofist.config import load_config
    from echofist.core.audio_playback import AudioPlayer
    from echofist.core.kiwi_client import (
        KiwiReachabilityResult,
        KiwiSDRClient,
        scan_kiwi_reachability,
    )
    from echofist.core.kiwi_sources import (
        KiwiScanHistoryRow,
        KiwiSourceRegistry,
        KiwiSourceSummary,
        extract_servers_from_lines,
        extract_servers_from_text,
        fetch_public_kiwi_sources,
        parse_kiwi_public_text,
    )
    from echofist.core.morse_decoder import MorseDecoder
    from echofist.core.qso_state import QSOStateMachine
    from echofist.logger import setup_logger
    from echofist.ui.dashboard import Dashboard
    from echofist.ui.i18n import UILocalizer, get_ui_localizer
except ModuleNotFoundError as e:
    print(
        "EchoFist 启动失败：缺少依赖或环境未正确安装。\n"
        f"错误：{e}\n\n"
        "建议：\n"
        "1) 使用 Python 3.10+ 的虚拟环境\n"
        "2) 安装依赖：pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from e

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
    ) -> Literal["up", "down", "left", "right", "enter", "esc", "q", "b", "l"] | None:
        ch = self._read_char()
        if ch is None:
            return None
        mapping: dict[str, Literal["enter", "esc", "q", "b", "l"]] = {
            "q": "q",
            "Q": "q",
            "b": "b",
            "B": "b",
            "l": "l",
            "L": "l",
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
    ) -> Literal["up", "down", "left", "right", "enter", "esc", "q", "b", "l"] | None:
        ch = self._read_char()
        if ch is None:
            return None
        mapping: dict[str, Literal["enter", "q", "b", "l"]] = {
            "q": "q",
            "Q": "q",
            "b": "b",
            "B": "b",
            "l": "l",
            "L": "l",
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
            "l",
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
            ("qrp_calling", 7.030),
            ("common", 7.023),
        ],
        "20m": [
            ("qrp_calling", 14.060),
            ("ncdxf_beacon", 14.100),
        ],
        "17m": [
            ("ncdxf_beacon", 18.110),
        ],
        "15m": [
            ("qrp_calling", 21.060),
            ("ncdxf_beacon", 21.150),
        ],
        "12m": [
            ("ncdxf_beacon", 24.930),
        ],
        "10m": [
            ("qrp_calling", 28.060),
            ("ncdxf_beacon", 28.200),
        ],
    }


def _select_menu(
    *,
    title: str,
    options: Sequence[T],
    render_option: "callable[[T], str]",
    default_index: int = 0,
    help_text: str,
) -> T | None:
    if not options:
        return None
    index = max(0, min(int(default_index), len(options) - 1))

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


def _select_language(
    *,
    localizer: UILocalizer,
    supported_languages: tuple[str, ...],
) -> UILocalizer:
    options = list(supported_languages) if supported_languages else ["zh", "en", "ja"]
    t = localizer.t
    labels: dict[str, str] = {
        "zh": t("common.language_zh"),
        "en": t("common.language_en"),
        "ja": t("common.language_ja"),
    }
    default_index = (
        options.index(localizer.language) if localizer.language in options else 0
    )
    selected = _select_menu(
        title=t("common.language_select_title"),
        options=options,
        render_option=lambda s: labels.get(str(s), str(s)),
        default_index=default_index,
        help_text=t("common.menu_help"),
    )
    if selected is None:
        return localizer
    return localizer.with_language(str(selected))


def _prompt_monitor_wizard(
    *,
    servers: tuple[str, ...],
    band: str | None,
    freq: float | None,
    with_default_servers: bool,
    localizer: UILocalizer,
    supported_languages: tuple[str, ...],
) -> tuple[tuple[str, ...], str, float, UILocalizer]:
    return _prompt_monitor_wizard_internal(
        servers=servers,
        band=band,
        freq=freq,
        with_default_servers=with_default_servers,
        localizer=localizer,
        supported_languages=supported_languages,
        select_language=True,
    )


def _prompt_monitor_wizard_internal(
    *,
    servers: tuple[str, ...],
    band: str | None,
    freq: float | None,
    with_default_servers: bool,
    localizer: UILocalizer,
    supported_languages: tuple[str, ...],
    select_language: bool,
) -> tuple[tuple[str, ...], str, float, UILocalizer]:
    if select_language:
        localizer = _select_language(
            localizer=localizer,
            supported_languages=supported_languages,
        )
    t = localizer.t

    presets = _band_presets()
    band_choices = list(presets.keys())

    resolved_servers = servers
    if not resolved_servers:
        candidates = _default_server_candidates() if with_default_servers else []
        primary = _select_menu(
            title=t("wizard.select_primary_server"),
            options=candidates,
            render_option=lambda s: str(s),
            default_index=0,
            help_text=t("common.menu_help"),
        )
        if primary is None:
            raise click.Abort()
        resolved: list[str] = [str(primary)]
        remaining = [s for s in candidates if s != primary]
        if remaining:
            add_backup = _select_menu(
                title=t("wizard.add_backup_server"),
                options=["not_add", "add"],
                render_option=lambda s: t(f"wizard.{s}"),
                default_index=0,
                help_text=t("common.menu_help"),
            )
            if add_backup == "add":
                backup = _select_menu(
                    title=t("wizard.select_backup_server"),
                    options=remaining,
                    render_option=lambda s: str(s),
                    default_index=0,
                    help_text=t("common.menu_help"),
                )
                if backup is None:
                    raise click.Abort()
                resolved.append(str(backup))
        resolved_servers = tuple(resolved)

    resolved_band = band
    if resolved_band is None and freq is None:
        selected_band = _select_menu(
            title=t("wizard.select_band"),
            options=band_choices,
            render_option=lambda s: str(s),
            default_index=(band_choices.index("40m") if "40m" in band_choices else 0),
            help_text=t("common.menu_help"),
        )
        if selected_band is None:
            raise click.Abort()
        resolved_band = str(selected_band)
    if resolved_band is None:
        resolved_band = "40m"

    resolved_freq = freq
    if resolved_freq is None:
        options = presets.get(resolved_band, presets["40m"])
        preset_ids = [preset_id for preset_id, _ in options]
        preset_freqs = dict(options)
        choice = _select_menu(
            title=t("wizard.select_freq"),
            options=preset_ids,
            render_option=lambda s: t(
                f"band_presets.{resolved_band}.{s}",
                freq=float(preset_freqs[str(s)]),
            ),
            default_index=0,
            help_text=t("common.menu_help"),
        )
        if choice is None:
            raise click.Abort()
        resolved_freq = preset_freqs[str(choice)]

    return resolved_servers, resolved_band, float(resolved_freq), localizer


def _prompt_qso_wizard(
    *,
    localizer: UILocalizer,
    supported_languages: tuple[str, ...],
) -> tuple[str, float, str | None, UILocalizer]:
    localizer = _select_language(
        localizer=localizer,
        supported_languages=supported_languages,
    )
    t = localizer.t

    candidates = _default_server_candidates()
    selected_server = _select_menu(
        title=t("home.select_server"),
        options=candidates,
        render_option=lambda s: str(s),
        default_index=0,
        help_text=t("common.menu_help"),
    )
    if selected_server is None:
        raise click.Abort()

    presets = _band_presets()
    band_choices = list(presets.keys())
    selected_band = _select_menu(
        title=t("home.select_band"),
        options=band_choices,
        render_option=lambda s: str(s),
        default_index=(band_choices.index("40m") if "40m" in band_choices else 0),
        help_text=t("common.menu_help"),
    )
    if selected_band is None:
        raise click.Abort()

    band = str(selected_band)
    options = presets.get(band, presets["40m"])
    preset_ids = [preset_id for preset_id, _ in options]
    preset_freqs = dict(options)
    choice = _select_menu(
        title=t("home.select_freq"),
        options=preset_ids,
        render_option=lambda s: t(
            f"band_presets.{band}.{s}",
            freq=float(preset_freqs[str(s)]),
        ),
        default_index=0,
        help_text=t("common.menu_help"),
    )
    if choice is None:
        raise click.Abort()
    freq = float(preset_freqs[str(choice)])

    callsign = click.prompt(
        t("home.callsign_prompt"),
        default=load_config().qso.default_callsign or "",
        show_default=bool(load_config().qso.default_callsign),
    ).strip()
    resolved_callsign = callsign or None
    return str(selected_server), freq, resolved_callsign, localizer


def _run_single_entry() -> None:
    from echofist.config import update_config

    app_config = load_config()
    localizer, supported_languages = get_ui_localizer(app_config.ui.language)
    localizer = print_banner(localizer)

    def pick_action() -> str:
        t = localizer.t
        selected_index = 0
        options: list[str] = ["monitor", "qso", "exit"]

        def render_option(value: str) -> str:
            return t(f"home.{value}")

        def build_panel() -> Panel:
            body = Text()
            for i, opt in enumerate(options):
                prefix = "› " if i == selected_index else "  "
                style = "bold cyan" if i == selected_index else "dim"
                body.append(prefix + render_option(opt) + "\n", style=style)
            body.append("\n" + t("home.help"), style="dim")
            return Panel(body, title=t("home.title"), border_style="cyan")

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
                    return "exit"
                if event == "l":
                    return "language"
                if event == "enter":
                    return options[selected_index]
                if event in {"up", "left"}:
                    selected_index = (selected_index - 1) % len(options)
                    live.update(build_panel())
                    continue
                if event in {"down", "right"}:
                    selected_index = (selected_index + 1) % len(options)
                    live.update(build_panel())
                    continue

    while True:
        action = pick_action()
        if action == "exit":
            return
        if action == "language":
            localizer = _select_language(
                localizer=localizer,
                supported_languages=supported_languages,
            )
            if localizer.language != app_config.ui.language:
                app_config = update_config({"ui": {"language": localizer.language}})
            continue
        if action == "monitor":
            resolved_servers: tuple[str, ...] = ()
            resolved_band: str | None = None
            resolved_freq: float | None = None
            (
                resolved_servers,
                resolved_band,
                resolved_freq,
                localizer,
            ) = _prompt_monitor_wizard_internal(
                servers=resolved_servers,
                band=resolved_band,
                freq=resolved_freq,
                with_default_servers=True,
                localizer=localizer,
                supported_languages=supported_languages,
                select_language=False,
            )
            if localizer.language != app_config.ui.language:
                app_config = update_config({"ui": {"language": localizer.language}})
            result = asyncio.run(
                monitor_mode(
                    resolved_servers,
                    float(resolved_freq),
                    500,
                    "cw",
                    "",
                    None,
                    False,
                    0.4,
                    precheck=True,
                    precheck_concurrency=5,
                    precheck_timeout=1.2,
                    precheck_verify_http=True,
                    with_registry=True,
                    registry_target=50,
                    learn=True,
                    max_store=int(load_config().kiwi_sources.max_total),
                    with_default_servers=True,
                    auto_switch=True,
                    interactive=True,
                    localizer=localizer,
                    supported_languages=supported_languages,
                )
            )
            if result == "restart":
                continue
            return
        if action == "qso":
            server, freq, callsign, localizer = _prompt_qso_wizard(
                localizer=localizer,
                supported_languages=supported_languages,
            )
            if localizer.language != app_config.ui.language:
                app_config = update_config({"ui": {"language": localizer.language}})
            asyncio.run(
                qso_mode(
                    server=server,
                    freq=freq,
                    callsign=callsign,
                    auto_suggest=False,
                    password="",
                    play_audio=False,
                    audio_gain=0.4,
                )
            )
            return


def print_banner(localizer: UILocalizer | None = None) -> UILocalizer:
    """打印应用横幅"""
    if localizer is None:
        try:
            localizer = get_ui_localizer(load_config().ui.language)[0]
        except Exception:
            localizer = get_ui_localizer()[0]
    t = localizer.t

    banner = Text()
    banner.append("EchoFist ", style="bold cyan")
    banner.append(f'({t("dashboard.app_tagline")})', style="italic")
    banner.append("\n")
    banner.append(t("banner.subtitle"), style="dim")
    banner.append("\n" + t("banner.version", version=__version__), style="dim")

    console.print(Panel(banner, border_style="cyan"))
    console.print()
    return localizer


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


def _server_list_format_help() -> str:
    return (
        "请粘贴服务器列表，格式要求：每行一个 host:port（端口 8000-9000）\n"
        "示例：\n"
        "  db0ovp.de:8073\n"
        "  85.147.201.225:8073\n"
        "\n"
        "粘贴完成后：直接连按两次回车结束（推荐），或输入 END 结束，或 Ctrl-D 结束。"
    )


def _ai_prompt_templates() -> list[str]:
    return [
        (
            "你是一个数据清洗助手。请从我提供的网页复制文本中提取所有 KiwiSDR 服务器地址。\n"
            "输出要求：\n"
            "1) 只输出纯文本，不要解释\n"
            "2) 每行一个 host:port\n"
            "3) 端口必须在 8000-9000\n"
            "4) 去重\n"
            "5) 不要输出任何其它内容（不要标题/序号/代码块标记）\n"
            "\n"
            "原始文本如下：\n"
            "<<<TEXT_START>>>\n"
            "{PASTE_HERE}\n"
            "<<<TEXT_END>>>"
        ),
        (
            "请把下面的原始文本整理成 KiwiSDR 服务器列表。\n"
            "输出要求：每行一个 host:port；如果只出现了 host 没有端口，则默认 :8073。\n"
            "仅输出列表本身，不要解释、不要编号、不要代码块。\n"
            "\n"
            "原始文本：\n"
            "<<<TEXT_START>>>\n"
            "{PASTE_HERE}\n"
            "<<<TEXT_END>>>"
        ),
        (
            "请执行严格抽取并校验：\n"
            "- 只保留形如 domain:port 或 ipv4:port 的条目\n"
            "- 端口范围必须是 8000-9000\n"
            "- 去重\n"
            "- 输出每行一个 host:port，不要任何额外文本\n"
            "\n"
            "原始文本：\n"
            "<<<TEXT_START>>>\n"
            "{PASTE_HERE}\n"
            "<<<TEXT_END>>>"
        ),
    ]


def _read_pasted_block() -> str:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return sys.stdin.read()
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def _read_servers_block(
    *,
    max_lines: int = 50_000,
    max_servers: int = 5_000,
) -> tuple[list[str], int, int, bool]:
    def iter_lines() -> Sequence[str]:
        lines: list[str] = []
        n = 0
        blanks = 0
        started = False
        while True:
            if n >= int(max_lines):
                break
            try:
                line = input()
            except EOFError:
                break
            stripped = line.strip()
            if stripped:
                started = True
                blanks = 0
            else:
                blanks += 1
                if started and blanks >= 2:
                    break
            if stripped.upper() == "END":
                break
            lines.append(line)
            n += 1
        return lines

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        content = sys.stdin.read()
        servers, invalid = extract_servers_from_text(content)
        lines_read = content.count("\n") + (1 if content.strip() else 0)
        return servers, int(invalid), int(lines_read), False

    lines = iter_lines()
    return extract_servers_from_lines(
        lines,
        max_servers=max_servers,
    )


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
    localizer: UILocalizer | None = None,
    supported_languages: tuple[str, ...] | None = None,
) -> Literal["exit", "restart"]:
    """监听模式"""
    from echofist.config import update_config

    if localizer is None or supported_languages is None:
        lang = load_config().ui.language
        localizer, supported_languages = get_ui_localizer(lang)
    t = localizer.t

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
        raise click.ClickException(t("monitor.no_servers"))

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
                console.print(f'[dim]{t("monitor.precheck_best", server=best)}[/dim]')
        if learn and (added > 0 or disabled > 0):
            msg = t(
                "monitor.precheck_learn",
                added=added,
                disabled=disabled,
            )
            console.print(f"[dim]{msg}[/dim]")

    server_index = 0
    current_server = server_candidates[server_index]
    client: KiwiSDRClient | None = None
    decoder = MorseDecoder()
    dashboard = Dashboard(localizer=localizer)
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
    last_audio_ok_recorded_at = 0.0
    audio_ok_record_interval_seconds = 20.0

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
                    connection_state="connecting",
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
                        t("monitor.connect_failed_body"),
                        title=t("monitor.connect_failed_title"),
                        border_style="red",
                    )
                )
                return "restart"
            raise ConnectionError(t("monitor.connect_failed_any"))
        dashboard.update(
            is_connected=True,
            connection_state=None,
            server=current_server,
            play_audio_enabled=play_audio,
            error_message=None,
        )
        console.print(f'[green]{t("monitor.connect_ok")}[/green]')

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
            ui_refresh_interval = 1.0 / float(max(1.0, app_config.ui.refresh_rate))
            last_render_at = time.monotonic() - ui_refresh_interval
            while True:
                event = poller.get_event()
                if event == "q":
                    control = "exit"
                    break
                if event == "b":
                    control = "restart"
                    break
                if event == "l":
                    localizer = _select_language(
                        localizer=localizer,
                        supported_languages=supported_languages,
                    )
                    update_config({"ui": {"language": localizer.language}})
                    t = localizer.t
                    dashboard.localizer = localizer
                    layout["header"].update(dashboard.render_header())
                    layout["waterfall"].update(dashboard.render_waterfall())
                    layout["decoded"].update(dashboard.render_decoded_text())
                    layout["status"].update(dashboard.render_status())
                    continue

                # 获取音频数据
                audio_data = await client.get_audio_chunk(timeout_seconds=0.1)
                now = asyncio.get_running_loop().time()
                audio_age = client.get_last_audio_age_seconds()
                if audio_age < 0.5:
                    last_good_audio_at = time.monotonic()
                    consecutive_reconnects = 0
                    if registry is not None:
                        mono_now = time.monotonic()
                        if (
                            mono_now - last_audio_ok_recorded_at
                            >= audio_ok_record_interval_seconds
                        ):
                            registry.record_audio_health(
                                server=current_server,
                                ok=True,
                                max_total=max_store,
                            )
                            last_audio_ok_recorded_at = mono_now
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
                            connection_state="switching",
                            server=current_server,
                            error_message=t(
                                "monitor.switch_prepare",
                                old_server=old_server,
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
                            registry.record_audio_health(
                                server=old_server,
                                ok=False,
                                max_total=max_store,
                            )
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
                                error_message=t(
                                    "monitor.switch_now",
                                    old_server=old_server,
                                    new_server=candidate,
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
                                error_message=t(
                                    "monitor.switch_failed",
                                    error=last_error,
                                ),
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
                                connection_state="reconnecting",
                                error_message=t(
                                    "monitor.reconnect_audio_stalled",
                                    seconds=audio_age,
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
                                registry.record_audio_health(
                                    server=current_server,
                                    ok=False,
                                    max_total=max_store,
                                )
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
                                        connection_state="switching",
                                        error_message=t(
                                            "monitor.reconnect_failed_switching",
                                            error=str(e),
                                        ),
                                    )
                                else:
                                    dashboard.update(
                                        error_message=t(
                                            "monitor.reconnect_failed",
                                            error=str(e),
                                        ),
                                    )
                    else:
                        if audio_age >= reconnect_warn_seconds:
                            dashboard.update(
                                error_message=t(
                                    "monitor.audio_paused_wait",
                                    seconds=audio_age,
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
                    mono_now = time.monotonic()
                    if mono_now - last_render_at >= ui_refresh_interval:
                        last_render_at = mono_now
                        layout["header"].update(dashboard.render_header())
                        layout["waterfall"].update(dashboard.render_waterfall())
                        layout["decoded"].update(dashboard.render_decoded_text())
                        layout["status"].update(dashboard.render_status())
                    await asyncio.sleep(0.05)
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

                mono_now = time.monotonic()
                if mono_now - last_render_at >= ui_refresh_interval:
                    last_render_at = mono_now
                    layout["header"].update(dashboard.render_header())
                    layout["waterfall"].update(dashboard.render_waterfall())
                    layout["decoded"].update(dashboard.render_decoded_text())
                    layout["status"].update(dashboard.render_status())

                await asyncio.sleep(0)

        return control

    except KeyboardInterrupt:
        console.print(f'\n[yellow]{t("monitor.stop_listening")}[/yellow]')
        return "exit"
    except Exception as e:
        logger.error(t("monitor.listen_error", error=str(e)))
        console.print(f'[red]{t("monitor.error_prefix", error=str(e))}[/red]')
        return "exit"
    finally:
        if client:
            disconnect_task = asyncio.create_task(client.disconnect())
            deadline = time.monotonic() + 3.2
            shown: set[str] = set()
            while True:
                for ev in client.drain_events():
                    if ev.name == "disconnecting" and ev.name not in shown:
                        shown.add(ev.name)
                        console.print(f'[dim]{t("monitor.disconnecting")}[/dim]')
                    if ev.name == "disconnected" and ev.name not in shown:
                        shown.add(ev.name)
                        console.print(f'[dim]{t("monitor.disconnected")}[/dim]')
                if disconnect_task.done():
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)
            try:
                await asyncio.wait_for(disconnect_task, timeout=0.1)
            except Exception:
                pass
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
    localizer = get_ui_localizer(load_config().ui.language)[0]
    t = localizer.t

    console.print(f'[green]{t("qso.start")}[/green]')
    console.print(t("qso.server", server=server))
    console.print(t("qso.freq", freq=freq))
    if callsign:
        console.print(t("qso.callsign", callsign=callsign))
    console.print()

    # 创建状态机
    state_machine = QSOStateMachine(callsign=callsign)
    client = KiwiSDRClient(server, password=password, ident_user=callsign)
    decoder = MorseDecoder()
    player: AudioPlayer | None = None

    try:
        # 连接服务器
        await client.connect()
        console.print(f'[green]{t("monitor.connect_ok")}[/green]')

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

        console.print(f'[cyan]{t("qso.waiting_cw")}[/cyan]')
        console.print(t("common.press_ctrl_c_stop"))
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
                    console.print(t("qso.suggestion", suggestion=suggestion))

                    # 这里可以添加自动发报逻辑
                    # await client.send_cw(suggestion)

            await asyncio.sleep(0.1)

    except KeyboardInterrupt:
        console.print(f'\n[yellow]{t("qso.stopping")}[/yellow]')
    except Exception as e:
        logger.error(t("qso.error", error=str(e)))
        console.print(f'[red]{t("monitor.error_prefix", error=str(e))}[/red]')
    finally:
        if client:
            disconnect_task = asyncio.create_task(client.disconnect())
            deadline = time.monotonic() + 3.2
            shown: set[str] = set()
            while True:
                for ev in client.drain_events():
                    if ev.name == "disconnecting" and ev.name not in shown:
                        shown.add(ev.name)
                        console.print(f'[dim]{t("monitor.disconnecting")}[/dim]')
                    if ev.name == "disconnected" and ev.name not in shown:
                        shown.add(ev.name)
                        console.print(f'[dim]{t("monitor.disconnected")}[/dim]')
                if disconnect_task.done():
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)
            try:
                await asyncio.wait_for(disconnect_task, timeout=0.1)
            except Exception:
                pass
        if player is not None:
            player.stop()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """EchoFist - AI辅助等幅电报（CW）通讯软件"""
    if ctx.invoked_subcommand is not None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        click.echo(ctx.get_help())
        return
    _run_single_entry()


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
                "1) 使用 sources add 按提示粘贴 host:port 列表（推荐）\n"
                "2) 浏览器打开 http://kiwisdr.com/public/ 或 https://rx.kiwisdr.com/ ，"
                "保存页面HTML后用 --html-file 导入\n"
                "3) 将HTML内容粘贴到命令行并用 --html-stdin 导入（Ctrl-D 结束）",
                title="提示",
                border_style="yellow",
            )
        )


@sources.command("add")
def sources_add() -> None:
    """交互式添加源：粘贴 host:port 列表并写入注册表"""
    localizer = print_banner()
    t = localizer.t
    registry = KiwiSourceRegistry()
    config = load_config()
    max_total = int(config.kiwi_sources.max_total)

    try:
        public_urls = [
            "https://rx.kiwisdr.com/",
            "https://rx.kiwisdr.com/public/",
            "https://kiwisdr.com/public/",
        ]

        def render_step1() -> Panel:
            body = Text.from_markup(
                "第 1 步：去网页复制“原始内容”\n\n"
                "请先打开下面任意一个网址（任选其一即可）：\n"
                f"- [link={public_urls[0]}]{public_urls[0]}[/link]\n"
                f"- [link={public_urls[1]}]{public_urls[1]}[/link]\n"
                f"- [link={public_urls[2]}]{public_urls[2]}[/link]\n\n"
                "在页面里随便滚动一下，然后把页面上能看到的服务器列表相关内容尽量多地复制出来。\n"
                "（可以是一大段文字/链接/杂乱内容都没关系，这一步不需要整理）"
            )
            return Panel(body, title="添加源（第 1/3 步）", border_style="cyan")

        while True:
            console.print(render_step1())
            action = _select_menu(
                title="第 1 步",
                options=["已复制原始内容", "打开网页（自动）", "取消（VY 73）"],
                render_option=lambda s: str(s),
                default_index=0,
                help_text=t("common.menu_help"),
            )
            if action == "取消（VY 73）" or action is None:
                console.print("[dim]VY 73[/dim]")
                return
            if action == "打开网页（自动）":
                try:
                    import webbrowser

                    webbrowser.open(public_urls[0])
                except Exception:
                    pass
                continue
            if action == "已复制原始内容":
                break

        templates = _ai_prompt_templates()
        template_index = 0
        while template_index < len(templates):
            console.print(
                Panel(
                    templates[template_index],
                    title=f"提示词模板 {template_index + 1}/3（第 2/3 步）",
                    border_style="yellow",
                )
            )
            action = _select_menu(
                title="第 2 步",
                options=[
                    "我已在 AI 得到 host:port 列表",
                    "换下一模板",
                    "取消（VY 73）",
                ],
                render_option=lambda s: str(s),
                default_index=0,
                help_text=t("common.menu_help"),
            )
            if action == "取消（VY 73）" or action is None:
                console.print("[dim]VY 73[/dim]")
                return
            if action == "换下一模板":
                template_index += 1
                continue
            if action == "我已在 AI 得到 host:port 列表":
                while True:
                    console.print(
                        Panel(
                            _server_list_format_help(),
                            title="添加源（第 3/3 步）",
                            border_style="cyan",
                        )
                    )
                    servers, invalid, lines_read, truncated = _read_servers_block()
                    if not servers:
                        hint = (
                            "没有收到任何输入。\n"
                            "请把 AI 输出的 host:port 列表粘贴进来，然后连按两次回车结束。"
                            if lines_read <= 0
                            else (
                                f"已读取 {lines_read} 行，但未匹配到服务器条目。\n"
                                "请确认 AI 输出是“每行一个 host:port”，或返回上一页更换提示词模板。"
                            )
                        )
                        console.print(
                            Panel(
                                hint,
                                title="未解析到服务器条目",
                                border_style="yellow",
                            )
                        )
                        action2 = _select_menu(
                            title="未解析到服务器条目",
                            options=["重新粘贴", "返回模板", "取消（VY 73）"],
                            render_option=lambda s: str(s),
                            default_index=0,
                            help_text=t("common.menu_help"),
                        )
                        if action2 == "取消（VY 73）" or action2 is None:
                            console.print("[dim]VY 73[/dim]")
                            return
                        if action2 == "返回模板":
                            template_index += 1
                            break
                        continue

                    preview = "\n".join(servers[:12])
                    console.print(
                        Panel(
                            f"解析到：{len(servers)} 条（疑似无效片段：{invalid}）"
                            f"{'（已截断）' if truncated else ''}\n\n{preview}",
                            title="预览",
                            border_style="green",
                        )
                    )
                    action3 = _select_menu(
                        title="下一步",
                        options=["导入", "重试粘贴", "取消（VY 73）"],
                        render_option=lambda s: str(s),
                        default_index=0,
                        help_text=t("common.menu_help"),
                    )
                    if action3 == "重试粘贴":
                        continue
                    if action3 == "取消（VY 73）" or action3 is None:
                        console.print("[dim]VY 73[/dim]")
                        return

                    added = registry.add_sources(
                        servers,
                        daily_cap=len(servers),
                        max_total=max_total,
                    )
                    stats = registry.stats()
                    console.print(
                        Panel(
                            f"新增：{added}\n启用：{stats['enabled']}\n总量：{stats['total']}",
                            title="导入完成",
                            border_style="green",
                        )
                    )
                    return

                continue

        console.print("[dim]三套模板仍未得到可用列表，VY 73[/dim]")
    finally:
        registry.close()


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
    from echofist.config import update_config

    app_config = load_config()
    localizer, supported_languages = get_ui_localizer(app_config.ui.language)
    localizer = print_banner(localizer)
    resolved_servers = server
    resolved_band = band
    resolved_freq: float | None = freq
    use_wizard = wizard and sys.stdin.isatty() and sys.stdout.isatty()

    if use_wizard:
        while True:
            (
                resolved_servers,
                resolved_band,
                resolved_freq,
                localizer,
            ) = _prompt_monitor_wizard(
                servers=resolved_servers,
                band=resolved_band,
                freq=resolved_freq,
                with_default_servers=with_default_servers,
                localizer=localizer,
                supported_languages=supported_languages,
            )
            if localizer.language != app_config.ui.language:
                app_config = update_config({"ui": {"language": localizer.language}})
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
                    localizer=localizer,
                    supported_languages=supported_languages,
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
            raise click.ClickException(localizer.t("monitor.cli_missing_server"))
        if resolved_freq is None:
            presets = _band_presets()
            if resolved_band and resolved_band in presets:
                resolved_freq = presets[resolved_band][0][1]
            else:
                raise click.ClickException(localizer.t("monitor.cli_missing_freq"))
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
                localizer=localizer,
                supported_languages=supported_languages,
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
