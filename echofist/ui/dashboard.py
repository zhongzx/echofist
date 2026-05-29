"""
仪表板组件 - 文本界面主显示
"""

import time
from dataclasses import dataclass

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from echofist.config import load_config
from echofist.logger import get_logger
from echofist.ui.i18n import UILocalizer, get_ui_localizer


@dataclass
class DashboardState:
    """仪表板状态"""

    frequency: float = 7.023
    bandwidth: int = 500
    mode: str = "CW"
    decoded_text: str = ""
    confidence: float = 0.0
    signal_strength: float = 0.0
    is_connected: bool = False
    connection_state: str | None = None
    last_update: float = 0.0
    error_message: str | None = None
    server: str = ""
    reconnect_count: int = 0
    server_switch_count: int = 0
    audio_chunks_total: int = 0
    audio_chunks_rate: float = 0.0
    audio_queue_size: int = 0
    audio_rms: float = 0.0
    audio_last_age: float = 0.0
    playback_buffer_ms: float = 0.0
    play_audio_enabled: bool = False


class Dashboard:
    """文本界面仪表板"""

    def __init__(self, localizer: UILocalizer | None = None):
        self.logger = get_logger("dashboard")
        self.config = load_config().ui
        self.localizer = localizer or get_ui_localizer(self.config.language)[0]

        # 状态
        self.state = DashboardState()
        self.history: list[tuple[str, float]] = []  # (文本, 时间戳)
        self.max_history = 50

        # Rich 组件
        self.console = Console()

        # 瀑布图模拟
        self.waterfall_data: list[list[float]] = []
        self.waterfall_width = 80
        self.waterfall_height = 20

        # 初始化瀑布图
        self._init_waterfall()

    def _init_waterfall(self) -> None:
        """初始化瀑布图数据"""
        self.waterfall_data = []
        for _ in range(self.waterfall_height):
            row = [0.0] * self.waterfall_width
            self.waterfall_data.append(row)

    def update(self, **kwargs) -> None:
        """更新仪表板状态"""
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)

        self.state.last_update = time.time()

        # 记录解码历史
        if self.state.decoded_text and self.state.decoded_text.strip():
            self.history.append((self.state.decoded_text, time.time()))
            if len(self.history) > self.max_history:
                self.history.pop(0)

        # 更新瀑布图
        self._update_waterfall()

    def _update_waterfall(self) -> None:
        """更新瀑布图数据"""
        if not self.config.show_waterfall:
            return

        # 模拟信号强度在瀑布图上的显示
        signal_level = min(1.0, self.state.signal_strength / 100.0)

        # 创建新行（最新的数据在顶部）
        new_row = [0.0] * self.waterfall_width

        # 在中间位置显示信号
        center = self.waterfall_width // 2
        spread = int(self.waterfall_width * 0.1 * signal_level)

        start = max(0, center - spread)
        end = min(self.waterfall_width, center + spread + 1)
        for i in range(start, end):
            # 高斯分布
            distance = abs(i - center) / (spread + 1)
            intensity = signal_level * (1.0 - distance)
            new_row[i] = max(new_row[i], intensity)

        # 添加噪声
        import random

        for i in range(self.waterfall_width):
            if new_row[i] == 0.0:
                new_row[i] = random.random() * 0.1

        # 将新行添加到顶部，移除最旧的行
        self.waterfall_data.insert(0, new_row)
        if len(self.waterfall_data) > self.waterfall_height:
            self.waterfall_data.pop()

    def render_header(self) -> Panel:
        """渲染头部信息"""
        header_text = Text()
        t = self.localizer.t

        # 应用名称
        header_text.append(f'{t("dashboard.app_name")} ', style="bold cyan")
        header_text.append(f'({t("dashboard.app_tagline")})', style="italic")
        header_text.append(" | ", style="dim")

        # 连接状态
        state_code = self.state.connection_state
        if state_code:
            header_text.append("◐ ", style="bold yellow")
            header_text.append(t(f"monitor.{state_code}"), style="yellow")
        elif self.state.is_connected:
            header_text.append("● ", style="bold green")
            header_text.append(t("dashboard.connected"), style="green")
        else:
            header_text.append("○ ", style="bold red")
            header_text.append(t("dashboard.disconnected"), style="red")

        header_text.append(" | ", style="dim")

        # 频率和模式
        freq_text = f"{self.state.frequency:.3f} MHz"
        header_text.append(freq_text, style="bold yellow")
        header_text.append(" | ", style="dim")
        header_text.append(f"{self.state.mode.upper()}", style="bold magenta")
        header_text.append(f" BW:{self.state.bandwidth}Hz", style="dim")

        # 错误信息
        if self.state.error_message:
            header_text.append("\n")
            header_text.append("⚠ ", style="bold red")
            header_text.append(self.state.error_message, style="red")

        return Panel(
            header_text,
            title=t("dashboard.header_title"),
            border_style="cyan",
        )

    def render_waterfall(self) -> Panel:
        """渲染瀑布图（文本版）"""
        t = self.localizer.t
        if not self.config.show_waterfall:
            return Panel(
                Text(t("dashboard.waterfall_disabled")),
                title=t("dashboard.waterfall_title"),
                border_style="blue",
            )

        # 创建瀑布图文本
        waterfall_text = Text()

        for row in self.waterfall_data:
            line = Text()
            for intensity in row:
                # 根据强度选择字符和颜色
                if intensity > 0.8:
                    char = "█"
                    style = "bright_white"
                elif intensity > 0.6:
                    char = "▓"
                    style = "white"
                elif intensity > 0.4:
                    char = "▒"
                    style = "bright_blue"
                elif intensity > 0.2:
                    char = "░"
                    style = "blue"
                else:
                    char = " "
                    style = "black"

                line.append(char, style=style)

            waterfall_text.append(line)
            waterfall_text.append("\n")

        # 添加频率标尺
        ruler = Text()
        ruler.append(
            " " * 10 + t("dashboard.freq_ruler") + " " * 10,
            style="dim",
        )
        waterfall_text.append(ruler)

        return Panel(
            waterfall_text,
            title=t("dashboard.waterfall_title"),
            border_style="blue",
        )

    def render_decoded_text(self) -> Panel:
        """渲染解码文本"""
        t = self.localizer.t
        # 创建解码文本显示
        decoded_panel = Text()

        if not self.history:
            decoded_panel.append(t("dashboard.waiting_decode"), style="dim")
        else:
            # 显示最近的历史记录
            for text, timestamp in reversed(self.history[-10:]):  # 显示最近10条
                time_str = time.strftime("%H:%M:%S", time.localtime(timestamp))
                decoded_panel.append(f"[{time_str}] ", style="dim")

                # 根据置信度着色
                if self.state.confidence > 0.8:
                    style = "green"
                elif self.state.confidence > 0.6:
                    style = "yellow"
                else:
                    style = "red"

                decoded_panel.append(text, style=style)
                decoded_panel.append("\n")

        return Panel(
            decoded_panel,
            title=t("dashboard.decoded_title"),
            border_style="green",
        )

    def render_status(self) -> Panel:
        """渲染状态信息"""
        t = self.localizer.t
        status_table = Table(show_header=False, box=None)

        if self.state.server:
            status_table.add_row(t("dashboard.server"), self.state.server)

        if self.state.error_message:
            status_table.add_row(t("dashboard.hint"), self.state.error_message)

        # 信号强度
        if self.config.show_signal_strength:
            signal_bar = self._create_signal_bar(self.state.signal_strength)
            status_table.add_row(t("dashboard.signal_strength"), signal_bar)

        # 置信度
        if self.config.show_confidence:
            confidence_bar = self._create_confidence_bar(self.state.confidence)
            status_table.add_row(t("dashboard.confidence"), confidence_bar)

        status_table.add_row(
            t("dashboard.audio_chunks"),
            f"{self.state.audio_chunks_total} "
            f"({self.state.audio_chunks_rate:.1f}/s)",
        )
        status_table.add_row(
            t("dashboard.audio_queue"),
            f"{self.state.audio_queue_size}",
        )
        if not self.state.play_audio_enabled:
            status_table.add_row(
                t("dashboard.playback"),
                t("dashboard.playback_off"),
            )
        status_table.add_row(
            t("dashboard.playback_buffer"),
            f"{self.state.playback_buffer_ms:.0f}ms",
        )
        status_table.add_row(
            t("dashboard.audio_rms"),
            f"{self.state.audio_rms:.3f}",
        )
        status_table.add_row(
            t("dashboard.audio_age"),
            f"{self.state.audio_last_age:.2f}s",
        )
        status_table.add_row(
            t("dashboard.reconnect_count"),
            f"{self.state.reconnect_count}",
        )
        status_table.add_row(
            t("dashboard.switch_count"),
            f"{self.state.server_switch_count}",
        )
        status_table.add_row(
            t("dashboard.hotkeys"),
            t("dashboard.hotkeys_value"),
        )

        # 最后更新时间
        if self.state.last_update > 0:
            elapsed = time.time() - self.state.last_update
            status_table.add_row(
                t("dashboard.last_update"),
                t("dashboard.seconds_ago", seconds=elapsed),
            )

        # 历史记录计数
        status_table.add_row(
            t("dashboard.history"),
            t("dashboard.history_count", count=len(self.history)),
        )

        return Panel(
            status_table,
            title=t("dashboard.status_info_title"),
            border_style="magenta",
        )

    def _create_signal_bar(self, strength: float) -> Text:
        """创建信号强度条"""
        bar_text = Text()

        import math

        if math.isnan(strength):
            for _ in range(10):
                bar_text.append("░", style="dim")
            bar_text.append(" N/A", style="dim")
            return bar_text

        normalized = (strength + 130.0) / 100.0
        normalized = max(0.0, min(1.0, normalized))
        bars = int(normalized * 10)

        # 根据强度选择颜色
        if normalized > 0.7:
            color = "green"
        elif normalized > 0.4:
            color = "yellow"
        else:
            color = "red"

        # 添加实心条
        for _ in range(bars):
            bar_text.append("█", style=f"bold {color}")

        # 添加空心条
        for _ in range(10 - bars):
            bar_text.append("░", style="dim")

        bar_text.append(f" {strength:.1f} dBm", style="dim")

        return bar_text

    def _create_confidence_bar(self, confidence: float) -> Text:
        """创建置信度条"""
        bar_text = Text()

        bars = int(confidence * 10)

        # 根据置信度选择颜色
        if confidence > 0.8:
            color = "green"
        elif confidence > 0.6:
            color = "yellow"
        else:
            color = "red"

        # 添加实心条
        for _ in range(bars):
            bar_text.append("█", style=f"bold {color}")

        # 添加空心条
        for _ in range(10 - bars):
            bar_text.append("░", style="dim")

        bar_text.append(f" {confidence:.2f}", style="dim")

        return bar_text

    def render_full_layout(self) -> Layout:
        """渲染完整布局"""
        layout = Layout()

        # 分割布局
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=3),
            Layout(name="status", size=6),
        )

        # 主区域再分割
        layout["main"].split_row(
            Layout(name="waterfall", ratio=2), Layout(name="decoded", ratio=3)
        )

        # 填充内容
        layout["header"].update(self.render_header())
        layout["waterfall"].update(self.render_waterfall())
        layout["decoded"].update(self.render_decoded_text())
        layout["status"].update(self.render_status())

        return layout

    def display_live(self, update_callback, interval: float = 0.25) -> None:
        """
        实时显示仪表板

        Args:
            update_callback: 更新回调函数
            interval: 更新间隔（秒）
        """
        try:
            with Live(
                self.render_full_layout(), console=self.console, screen=False
            ) as live:
                while True:
                    # 调用更新回调
                    update_callback()

                    # 更新显示
                    live.update(self.render_full_layout())

                    # 等待
                    time.sleep(interval)

        except KeyboardInterrupt:
            self.logger.info("仪表板显示已停止")

    def set_error(self, message: str) -> None:
        """设置错误信息"""
        self.state.error_message = message
        self.logger.error(f"仪表板错误: {message}")

    def clear_error(self) -> None:
        """清除错误信息"""
        self.state.error_message = None

    def reset(self) -> None:
        """重置仪表板"""
        self.state = DashboardState()
        self.history = []
        self._init_waterfall()
        self.logger.info("仪表板已重置")
