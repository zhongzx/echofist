import sys
import types
from typing import Any

import numpy as np

from echofist.core.audio_playback import AudioPlayer


class _DummyOutputStream:
    def __init__(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        callback: Any,
        blocksize: int,
    ) -> None:
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.dtype = str(dtype)
        self.callback = callback
        self.blocksize = int(blocksize)
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def _install_dummy_sounddevice() -> None:
    mod = types.ModuleType("sounddevice")
    mod.OutputStream = _DummyOutputStream
    sys.modules["sounddevice"] = mod


def test_audio_player_feed_and_callback_produces_samples() -> None:
    _install_dummy_sounddevice()

    player = AudioPlayer(
        input_rate=12000,
        output_rate=12000,
        gain=1.0,
        max_buffer_seconds=0.2,
    )
    player.start()

    t = np.linspace(0.0, 0.05, int(12000 * 0.05), endpoint=False)
    samples = (0.2 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    player.feed(samples)

    before_ms = player.buffered_ms()
    assert before_ms > 0.0
    assert player._stream is not None

    out = np.zeros((1024, 1), dtype=np.float32)
    player._stream.callback(out, 1024, None, None)

    assert float(np.max(np.abs(out))) > 0.0
    after_ms = player.buffered_ms()
    assert after_ms <= before_ms

    player.stop()


def test_audio_player_buffer_is_capped_by_max_buffer_seconds() -> None:
    _install_dummy_sounddevice()

    player = AudioPlayer(
        input_rate=12000,
        output_rate=12000,
        gain=1.0,
        max_buffer_seconds=0.1,
    )
    player.start()

    samples = np.ones(12000, dtype=np.float32)
    player.feed(samples)

    buffered = player.buffered_ms()
    assert 0.0 <= buffered <= 101.0

    player.stop()


def test_audio_player_clear_empties_buffer() -> None:
    _install_dummy_sounddevice()

    player = AudioPlayer(
        input_rate=12000,
        output_rate=12000,
        gain=1.0,
        max_buffer_seconds=0.2,
    )
    player.start()
    player.feed(np.ones(2048, dtype=np.float32))
    assert player.buffered_ms() > 0.0

    player.clear()
    assert player.buffered_ms() == 0.0

    player.stop()
