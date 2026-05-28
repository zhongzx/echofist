"""
KiwiSDR 客户端模块
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
from websockets.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from echofist.config import load_config
from echofist.logger import get_logger


@dataclass
class KiwiSDRServer:
    """KiwiSDR 服务器信息"""

    name: str
    host: str
    port: int = 8073
    location: str | None = None
    users: int = 0
    max_users: int = 4
    snr: float = 0.0
    freq_range: tuple[float, float] = (0.0, 30.0)


class KiwiSDRClient:
    """KiwiSDR 客户端"""

    def __init__(self, server: str):
        """
        初始化 KiwiSDR 客户端

        Args:
            server: 服务器地址，格式为 "host:port" 或 "host"
        """
        self.logger = get_logger("kiwi_client")
        self.config = load_config().kiwi_sdr

        # 解析服务器地址
        if ":" in server:
            host, port_str = server.split(":", 1)
            port = int(port_str)
        else:
            host = server
            port = 8073  # 默认端口

        self.server = KiwiSDRServer(name=host, host=host, port=port)
        self.ws: Any | None = None
        self.connected = False
        self.audio_buffer: list[np.ndarray] = []
        self.current_frequency: float = 7.023  # 默认频率
        self.current_mode: str = "cw"
        self.signal_strength: float = 0.0

    async def connect(self) -> None:
        """连接到 KiwiSDR 服务器"""
        if self.connected:
            return

        ws_url = f"ws://{self.server.host}:{self.server.port}/"

        for attempt in range(self.config.reconnect_attempts):
            try:
                self.logger.info(f"正在连接到 KiwiSDR 服务器: {ws_url}")
                self.ws = await websocket_connect(ws_url, timeout=self.config.timeout)
                self.connected = True
                self.logger.success("已连接到 KiwiSDR 服务器")
                return

            except Exception as e:
                self.logger.warning(f"连接尝试 {attempt + 1} 失败: {e}")
                if attempt < self.config.reconnect_attempts - 1:
                    await asyncio.sleep(self.config.reconnect_delay)

        raise ConnectionError(f"无法连接到 KiwiSDR 服务器: {self.server.host}")

    async def disconnect(self) -> None:
        """断开连接"""
        if self.ws and self.connected:
            try:
                await self.ws.close()
                self.logger.info("已断开与 KiwiSDR 服务器的连接")
            except Exception as e:
                self.logger.error(f"断开连接时出错: {e}")
            finally:
                self.connected = False
                self.ws = None

    async def set_frequency(self, freq: float) -> None:
        """设置频率 (MHz)"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        self.current_frequency = freq
        khz = int(freq * 1000)

        command = {"cmd": "set", "freq": khz}

        try:
            await self.ws.send(json.dumps(command))
            self.logger.info(f"频率已设置为 {freq:.3f} MHz ({khz} kHz)")
        except Exception as e:
            self.logger.error(f"设置频率失败: {e}")
            raise

    async def set_mode(self, mode: str) -> None:
        """设置接收模式"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        mode_map = {"am": "am", "cw": "cw", "usb": "usb", "lsb": "lsb"}

        if mode not in mode_map:
            raise ValueError(f"不支持的模式: {mode}")

        self.current_mode = mode
        command = {"cmd": "set", "mode": mode_map[mode]}

        try:
            await self.ws.send(json.dumps(command))
            self.logger.info(f"模式已设置为 {mode.upper()}")
        except Exception as e:
            self.logger.error(f"设置模式失败: {e}")
            raise

    async def set_bandwidth(self, bandwidth: int) -> None:
        """设置带宽 (Hz)"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        command = {"cmd": "set", "bw": bandwidth}

        try:
            await self.ws.send(json.dumps(command))
            self.logger.info(f"带宽已设置为 {bandwidth} Hz")
        except Exception as e:
            self.logger.error(f"设置带宽失败: {e}")
            raise

    async def start_audio_stream(self) -> None:
        """启动音频流"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        command = {"cmd": "audio", "start": True}

        try:
            await self.ws.send(json.dumps(command))
            self.logger.info("音频流已启动")
        except Exception as e:
            self.logger.error(f"启动音频流失败: {e}")
            raise

    async def get_audio_chunk(self) -> np.ndarray | None:
        """获取音频数据块"""
        if not self.connected or not self.ws:
            return None

        try:
            # 设置超时
            message = await asyncio.wait_for(self.ws.recv(), timeout=1.0)

            # 解析消息
            data = json.loads(message)

            if data.get("type") == "audio":
                # 提取音频数据
                samples = data.get("samples", [])
                if samples:
                    audio_array = np.array(samples, dtype=np.float32)

                    # 更新信号强度
                    rssi = data.get("rssi", 0)
                    self.signal_strength = float(rssi)

                    return audio_array

            elif data.get("type") == "status":
                # 处理状态消息
                self.logger.debug(f"状态更新: {data}")

            return None

        except asyncio.TimeoutError:
            return None
        except ConnectionClosed:
            self.logger.warning("WebSocket 连接已关闭")
            self.connected = False
            return None
        except Exception as e:
            self.logger.error(f"获取音频数据时出错: {e}")
            return None

    def get_signal_strength(self) -> float:
        """获取当前信号强度"""
        return self.signal_strength

    async def send_cw(self, text: str, wpm: int = 20) -> None:
        """发送 CW 信号（如果服务器支持）"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        command = {"cmd": "cw", "text": text, "wpm": wpm}

        try:
            await self.ws.send(json.dumps(command))
            self.logger.info(f"已发送 CW: {text} ({wpm} WPM)")
        except Exception as e:
            self.logger.error(f"发送 CW 失败: {e}")
            raise


class KiwiSDRNetwork:
    """KiwiSDR 网络管理"""

    def __init__(self):
        self.logger = get_logger("kiwi_network")
        self.servers: list[KiwiSDRServer] = []

    async def discover_servers(self) -> list[KiwiSDRServer]:
        """发现可用的 KiwiSDR 服务器"""
        self.logger.info("正在发现 KiwiSDR 服务器...")

        # 这里应该从 KiwiSDR 公共服务器列表获取
        # 暂时返回示例数据
        example_servers = [
            KiwiSDRServer(
                name="PA3GJX",
                host="85.147.201.225",
                port=8073,
                location="IJsselmuiden, Netherlands",
                users=2,
                max_users=4,
                snr=20.0,
                freq_range=(0.0, 30.0),
            ),
            KiwiSDRServer(
                name="G0MJW",
                host="example.g0mjw.com",
                port=8073,
                location="UK",
                users=1,
                max_users=4,
                snr=15.0,
                freq_range=(0.0, 30.0),
            ),
        ]

        self.servers = example_servers
        self.logger.info(f"发现 {len(self.servers)} 个服务器")
        return self.servers

    def get_server_by_location(
        self, country: str | None = None, region: str | None = None
    ) -> KiwiSDRServer | None:
        """根据位置获取服务器"""
        if not self.servers:
            return None

        for server in self.servers:
            if server.location:
                location_lower = server.location.lower()

                if country and country.lower() in location_lower:
                    return server

                if region and region.lower() in location_lower:
                    return server

        return None

    def get_best_server(self) -> KiwiSDRServer | None:
        """获取最佳服务器（基于用户数和 SNR）"""
        if not self.servers:
            return None

        # 优先选择用户数少、SNR高的服务器
        return min(self.servers, key=lambda s: (s.users / s.max_users, -s.snr))
