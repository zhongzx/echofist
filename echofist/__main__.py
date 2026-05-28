#!/usr/bin/env python3
"""
EchoFist 主入口文件
AI辅助等幅电报（CW）通讯软件
"""

import asyncio
import sys

import click
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from echofist import __version__
from echofist.core.kiwi_client import KiwiSDRClient
from echofist.core.morse_decoder import MorseDecoder
from echofist.core.qso_state import QSOStateMachine
from echofist.logger import setup_logger
from echofist.ui.dashboard import Dashboard

console = Console()
logger = setup_logger()


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


async def monitor_mode(
    server: str, freq: float, bandwidth: int = 500, mode: str = "am"
) -> None:
    """监听模式"""
    console.print("[green]启动监听模式[/green]")
    console.print(f"服务器: [cyan]{server}[/cyan]")
    console.print(f"频率: [yellow]{freq} MHz[/yellow]")
    console.print(f"带宽: [blue]{bandwidth} Hz[/blue]")
    console.print(f"模式: [magenta]{mode.upper()}[/magenta]")
    console.print()

    # 创建客户端
    client = KiwiSDRClient(server)
    decoder = MorseDecoder()
    dashboard = Dashboard()

    try:
        # 连接服务器
        await client.connect()
        console.print("[green]✓ 已连接到KiwiSDR服务器[/green]")

        # 设置频率和模式
        await client.set_frequency(freq)
        await client.set_mode(mode)
        await client.set_bandwidth(bandwidth)

        # 启动音频流
        await client.start_audio_stream()

        # 创建实时显示
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="waterfall", ratio=2),
            Layout(name="decoded", ratio=3),
            Layout(name="status", size=5),
        )

        with Live(layout, console=console, screen=False, refresh_per_second=4):
            while True:
                # 获取音频数据
                audio_data = await client.get_audio_chunk()
                if audio_data is None:
                    await asyncio.sleep(0.1)
                    continue

                # 解码摩尔斯电码
                decoded_text, confidence = decoder.decode(audio_data)

                # 更新仪表板
                dashboard.update(
                    frequency=freq,
                    bandwidth=bandwidth,
                    mode=mode,
                    decoded_text=decoded_text,
                    confidence=confidence,
                    signal_strength=client.get_signal_strength(),
                )

                # 渲染布局
                layout["header"].update(dashboard.render_header())
                layout["waterfall"].update(dashboard.render_waterfall())
                layout["decoded"].update(dashboard.render_decoded_text())
                layout["status"].update(dashboard.render_status())

                await asyncio.sleep(0.25)

    except KeyboardInterrupt:
        console.print("\n[yellow]正在停止监听...[/yellow]")
    except Exception as e:
        logger.error(f"监听模式错误: {e}")
        console.print(f"[red]错误: {e}[/red]")
    finally:
        if client:
            await client.disconnect()


async def qso_mode(
    server: str, freq: float, callsign: str | None = None, auto_suggest: bool = False
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
    client = KiwiSDRClient(server)
    decoder = MorseDecoder()

    try:
        # 连接服务器
        await client.connect()
        console.print("[green]✓ 已连接到KiwiSDR服务器[/green]")

        # 设置频率
        await client.set_frequency(freq)
        await client.set_mode("cw")

        # 启动音频流
        await client.start_audio_stream()

        console.print("[cyan]等待CW信号...[/cyan]")
        console.print("按 Ctrl+C 停止")
        console.print()

        while True:
            # 获取音频数据
            audio_data = await client.get_audio_chunk()
            if audio_data is None:
                await asyncio.sleep(0.1)
                continue

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
def monitor(server: str, freq: float, bandwidth: int, mode: str) -> None:
    """监听模式 - 实时解码CW信号"""
    print_banner()
    asyncio.run(monitor_mode(server, freq, bandwidth, mode))


@cli.command()
@click.option("--server", "-s", required=True, help="KiwiSDR服务器地址")
@click.option("--freq", "-f", type=float, default=7.023, help="频率 (MHz)")
@click.option("--callsign", "-c", help="您的呼号")
@click.option("--auto-suggest", "-a", is_flag=True, help="自动提供应答建议")
def qso(server: str, freq: float, callsign: str | None, auto_suggest: bool) -> None:
    """QSO模式 - 半自动通联"""
    print_banner()
    asyncio.run(qso_mode(server, freq, callsign, auto_suggest))


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
