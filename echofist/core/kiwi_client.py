"""
KiwiSDR 客户端模块 - 支持全球 700+ 远程接收机接入
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
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


_SND_FLAG_STEREO: Final[int] = 0x08
_SND_FLAG_COMPRESSED: Final[int] = 0x10


class _ImaAdpcmDecoder:
    def __init__(self) -> None:
        self._index = 0
        self._prev = 0

    def preset(self, index: int, prev: int) -> None:
        self._index = max(0, min(index, 88))
        self._prev = int(prev)

    def decode(self, data: bytes) -> np.ndarray:
        step_size_table = (
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            16,
            17,
            19,
            21,
            23,
            25,
            28,
            31,
            34,
            37,
            41,
            45,
            50,
            55,
            60,
            66,
            73,
            80,
            88,
            97,
            107,
            118,
            130,
            143,
            157,
            173,
            190,
            209,
            230,
            253,
            279,
            307,
            337,
            371,
            408,
            449,
            494,
            544,
            598,
            658,
            724,
            796,
            876,
            963,
            1060,
            1166,
            1282,
            1411,
            1552,
            1707,
            1878,
            2066,
            2272,
            2499,
            2749,
            3024,
            3327,
            3660,
            4026,
            4428,
            4871,
            5358,
            5894,
            6484,
            7132,
            7845,
            8630,
            9493,
            10442,
            11487,
            12635,
            13899,
            15289,
            16818,
            18500,
            20350,
            22385,
            24623,
            27086,
            29794,
            32767,
        )
        index_adjust_table = (
            -1,
            -1,
            -1,
            -1,
            2,
            4,
            6,
            8,
            -1,
            -1,
            -1,
            -1,
            2,
            4,
            6,
            8,
        )

        def clamp(x: int, xmin: int, xmax: int) -> int:
            if x < xmin:
                return xmin
            if x > xmax:
                return xmax
            return x

        out = np.empty(len(data) * 2, dtype=np.int16)
        oi = 0
        for b in data:
            for nibble_shift in (0, 4):
                code = (b >> nibble_shift) & 0x0F
                step = step_size_table[self._index]
                self._index = clamp(
                    self._index + index_adjust_table[code],
                    0,
                    len(step_size_table) - 1,
                )
                diff = step >> 3
                if code & 1:
                    diff += step >> 2
                if code & 2:
                    diff += step >> 1
                if code & 4:
                    diff += step
                if code & 8:
                    diff = -diff
                self._prev = clamp(self._prev + diff, -32768, 32767)
                out[oi] = self._prev
                oi += 1
        return out


class KiwiSDRClient:
    """KiwiSDR 客户端"""

    def __init__(
        self,
        server: str,
        *,
        password: str = "",
        ident_user: str | None = None,
    ):
        """
        初始化 KiwiSDR 客户端

        Args:
            server: 服务器地址，格式为 "host:port" 或 "host"
        """
        self.logger = get_logger("kiwi_client")
        app_config = load_config()
        self.config = app_config.kiwi_sdr

        if ":" in server:
            host, port_str = server.split(":", 1)
            port = int(port_str)
        else:
            host = server
            port = 8073  # 默认端口

        self.server = KiwiSDRServer(name=host, host=host, port=port)
        self._password: str = password
        self._ident_user: str | None = ident_user
        self.ws: Any = None
        self.connected: bool = False
        self.current_frequency: float = 7.023
        self.current_mode: str = "cw"
        self._low_cut_hz: int = 300
        self._high_cut_hz: int = 800
        self._decoder = _ImaAdpcmDecoder()
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=8)
        self._ready_event: asyncio.Event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._requested_output_rate: int = app_config.morse_decoder.sample_rate
        self._last_rx_monotonic: float = time.monotonic()
        self._last_snd_monotonic: float = time.monotonic()
        self.signal_strength: float = float("nan")

    async def connect(self) -> None:
        """连接到 KiwiSDR 服务器"""
        if self.connected:
            return

        ts = await self._fetch_server_ts()
        ws_url = f"ws://{self.server.host}:{self.server.port}/ws/kiwi/{ts}/SND"

        for attempt in range(self.config.reconnect_attempts):
            try:
                self.logger.info(f"正在连接到 KiwiSDR 服务器: {ws_url}")
                self.ws = await websocket_connect(
                    ws_url,
                    open_timeout=self.config.timeout,
                )
                self.connected = True
                self._last_rx_monotonic = time.monotonic()
                self._last_snd_monotonic = self._last_rx_monotonic
                self._ready_event.clear()
                self._reader_task = asyncio.create_task(self._reader_loop())
                await asyncio.wait_for(
                    self._ready_event.wait(),
                    timeout=self.config.timeout,
                )
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(),
                )
                self.logger.success("已连接到 KiwiSDR 服务器")
                return

            except Exception as e:
                self.logger.warning(f"连接尝试 {attempt + 1} 失败: {e}")
                await self.disconnect()
                if attempt < self.config.reconnect_attempts - 1:
                    await asyncio.sleep(self.config.reconnect_delay)

        raise ConnectionError(f"无法连接到 KiwiSDR 服务器: {self.server.host}")

    async def disconnect(self) -> None:
        """断开连接"""
        self.connected = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                self.logger.error(f"断开连接时出错: {e}")
            finally:
                self.ws = None

    async def set_frequency(self, freq: float) -> None:
        """设置频率 (MHz)"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        self.current_frequency = freq
        await self._send_mod()

    async def set_mode(self, mode: str) -> None:
        """设置接收模式"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        if mode not in {"am", "cw", "usb", "lsb"}:
            raise ValueError(f"不支持的模式: {mode}")

        self.current_mode = mode
        self._apply_mode_defaults()
        await self._send_mod()

    async def set_bandwidth(self, bandwidth: int) -> None:
        """设置带宽 (Hz)"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")

        bandwidth = max(100, min(bandwidth, 6000))
        if self.current_mode == "cw":
            self._low_cut_hz = 300
            self._high_cut_hz = 300 + bandwidth
        await self._send_mod()

    async def start_audio_stream(self) -> None:
        """启动音频流"""
        if not self.connected:
            raise ConnectionError("未连接到服务器")
        await self._send_mod()

    async def get_audio_chunk(
        self,
        *,
        timeout_seconds: float = 1.0,
    ) -> np.ndarray | None:
        """获取音频数据块"""
        if not self.connected:
            return None

        try:
            return await asyncio.wait_for(
                self._audio_queue.get(),
                timeout=float(timeout_seconds),
            )

        except asyncio.TimeoutError:
            return None
        except Exception as e:
            self.logger.error(f"获取音频数据时出错: {e}")
            return None

    def get_audio_queue_size(self) -> int:
        return int(self._audio_queue.qsize())

    def drain_audio_chunks(self, max_chunks: int = 8) -> list[np.ndarray]:
        chunks: list[np.ndarray] = []
        for _ in range(max_chunks):
            try:
                chunks.append(self._audio_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return chunks

    def get_last_rx_age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_rx_monotonic)

    def get_last_audio_age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_snd_monotonic)

    def get_signal_strength(self) -> float:
        """获取当前信号强度"""
        return self.signal_strength

    async def send_cw(self, text: str, wpm: int = 20) -> None:
        """发送 CW 信号（如果服务器支持）"""
        raise NotImplementedError(f"text={text} wpm={wpm}")

    async def _fetch_server_ts(self) -> int:
        url = f"http://{self.server.host}:{self.server.port}/VER"
        timeout = aiohttp.ClientTimeout(total=float(self.config.timeout))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return int(time.time())
                    data = await resp.json()
                    ts = data.get("ts")
                    if isinstance(ts, int):
                        return ts
                    if isinstance(ts, float):
                        return int(ts)
                    return int(time.time())
            except Exception:
                return int(time.time())

    async def _reader_loop(self) -> None:
        if not self.ws:
            return

        auth_pw = self._password if self._password else "#"
        await self._send_text(f"SET auth t=kiwi p={auth_pw}")
        if self._ident_user:
            await self._send_text(f"SET ident_user={self._ident_user}")
        while self.connected and self.ws:
            try:
                message = await self.ws.recv()
            except ConnectionClosed:
                self.connected = False
                return

            self._last_rx_monotonic = time.monotonic()
            if isinstance(message, str):
                buf = message.encode("utf-8", errors="ignore")
            else:
                buf = bytes(message)

            if len(buf) < 3:
                continue

            tag = buf[0:3].decode("latin-1", errors="ignore")
            body = buf[3:]

            if tag == "MSG":
                body_text = body.decode("utf-8", errors="ignore")
                params = self._parse_msg_body(body_text)
                if "badp" in params and params["badp"] != "0":
                    self.connected = False
                    badp = params["badp"]
                    raise ConnectionError(f"KiwiSDR 拒绝认证 badp={badp}")
                await self._handle_msg(params)
            elif tag == "SND":
                await self._handle_snd(body)

    async def _keepalive_loop(self) -> None:
        while self.connected and self.ws:
            try:
                await self._send_text("SET keepalive")
            except Exception:
                return
            await asyncio.sleep(1.0)

    async def _handle_msg(self, params: dict[str, str]) -> None:
        if not self._ready_event.is_set() and "sample_rate" in params:
            await self._send_text(
                f"SET mod={self.current_mode} "
                f"low_cut={self._low_cut_hz} high_cut={self._high_cut_hz} "
                f"freq={self._frequency_khz():.3f}"
            )
            await self._send_text(
                "SET agc=1 hang=0 thresh=-100 slope=6 decay=500 manGain=49"
            )
            await self._send_text("SET compression=1")
            await self._send_text("SET squelch=0 max=0")
            await self._send_text("SET unmute=1")
            self._ready_event.set()

        if "audio_rate" in params:
            await self._send_text(
                "SET AR OK "
                f"in={params['audio_rate']} "
                f"out={self._requested_output_rate}"
            )

        if "audio_adpcm_state" in params:
            parts = params["audio_adpcm_state"].split(",")
            if len(parts) == 2:
                try:
                    idx = int(float(parts[0]))
                    prev = int(float(parts[1]))
                    self._decoder.preset(idx, prev)
                except ValueError:
                    return

    async def _handle_snd(self, body: bytes) -> None:
        if len(body) < 7:
            return
        flags = body[0]
        sequence = int.from_bytes(body[1:5], "little", signed=False)
        smeter = int.from_bytes(body[5:7], "big", signed=False)
        self.signal_strength = 0.1 * float(smeter) - 127.0

        audio_data = body[7:]
        if flags & _SND_FLAG_STEREO:
            if len(audio_data) < 10:
                return
            audio_data = audio_data[10:]

        if flags & _SND_FLAG_COMPRESSED:
            pcm = self._decoder.decode(audio_data)
        else:
            if len(audio_data) % 2 != 0:
                audio_data = audio_data[:-1]
            pcm = np.frombuffer(audio_data, dtype=">i2")

        if pcm.size == 0:
            return

        self._last_snd_monotonic = time.monotonic()
        _ = sequence
        samples = (pcm.astype(np.float32) / 32768.0).astype(
            np.float32,
            copy=False,
        )
        if self._audio_queue.full():
            try:
                _ = self._audio_queue.get_nowait()
                self.logger.warning("Audio queue full, dropping oldest sample")
            except asyncio.QueueEmpty:
                pass
        await self._audio_queue.put(samples)

    async def _send_text(self, msg: str) -> None:
        if not self.ws:
            raise ConnectionError("未连接到服务器")
        await self.ws.send(msg)

    def _parse_msg_body(self, body: str) -> dict[str, str]:
        params: dict[str, str] = {}
        for token in body.strip().split(" "):
            if not token:
                continue
            eq = token.find("=")
            if eq == -1:
                params[token] = ""
            else:
                key, value = token.split("=", 1)
                params[key] = value
        return params

    def _frequency_khz(self) -> float:
        return float(self.current_frequency) * 1000.0

    async def _send_mod(self) -> None:
        await self._send_text(
            f"SET mod={self.current_mode} low_cut={self._low_cut_hz} "
            f"high_cut={self._high_cut_hz} freq={self._frequency_khz():.3f}"
        )

    def _apply_mode_defaults(self) -> None:
        if self.current_mode == "cw":
            self._low_cut_hz = 300
            self._high_cut_hz = 800
        elif self.current_mode == "am":
            self._low_cut_hz = -4900
            self._high_cut_hz = 4900
        elif self.current_mode == "usb":
            self._low_cut_hz = 300
            self._high_cut_hz = 2700
        elif self.current_mode == "lsb":
            self._low_cut_hz = -2700
            self._high_cut_hz = -300


class KiwiSDRNetwork:
    """KiwiSDR 网络管理"""

    def __init__(self) -> None:
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
