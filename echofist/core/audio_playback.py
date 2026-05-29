"""
音频播放层（Playback）

职责：
- 将上游提供的单声道浮点音频（np.float32, [-1, 1]）缓冲并输出到系统默认音频设备
- 必要时做采样率转换（resample_poly）

约束：
- 不负责信号生成（例如 CW 音频合成）
- 不负责信号处理/增强（滤波、去噪、归一化等）
"""

import math
import threading
from collections import deque
from typing import Any

import numpy as np

from echofist.logger import get_logger


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
        self._buffer_blocks: deque[np.ndarray] = deque()
        self._head_offset = 0
        self._buffer_samples = 0
        self._max_samples = int(self._output_rate * max_buffer_seconds)
        self._stream: Any | None = None
        self._logger = get_logger("audio_player")

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
                filled = 0
                while filled < frames:
                    if not self._buffer_blocks:
                        break
                    head = self._buffer_blocks[0]
                    if self._head_offset >= int(head.size):
                        self._buffer_blocks.popleft()
                        self._head_offset = 0
                        continue
                    n_avail = int(head.size) - self._head_offset
                    n_take = min(frames - filled, n_avail)
                    out[filled : filled + n_take] = head[
                        self._head_offset : self._head_offset + n_take
                    ]
                    self._head_offset += n_take
                    self._buffer_samples -= n_take
                    filled += n_take
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
            n = self._buffer_samples
        return (n / float(self._output_rate)) * 1000.0

    def clear(self) -> None:
        with self._lock:
            self._buffer_blocks.clear()
            self._head_offset = 0
            self._buffer_samples = 0

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
            if y.size == 0:
                return
            self._buffer_blocks.append(y)
            self._buffer_samples += int(y.size)

            overflow = self._buffer_samples - self._max_samples
            while overflow > 0 and self._buffer_blocks:
                head = self._buffer_blocks[0]
                head_remaining = int(head.size) - self._head_offset
                if head_remaining <= 0:
                    self._buffer_blocks.popleft()
                    self._head_offset = 0
                    continue
                if overflow >= head_remaining:
                    self._buffer_blocks.popleft()
                    self._buffer_samples -= head_remaining
                    overflow -= head_remaining
                    self._head_offset = 0
                    continue
                self._head_offset += overflow
                self._buffer_samples -= overflow
                overflow = 0
