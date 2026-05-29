"""
音频生成层（Generation）

职责：
- 在本地生成可播放的音频波形（例如文本 -> CW 音频）

约束：
- 不依赖任何音频设备（不做播放）
- 不做信号增强（滤波/去噪/归一化等处理应交给 Processing 层）
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def morse_code_map() -> dict[str, str]:
    return {
        "A": ".-",
        "B": "-...",
        "C": "-.-.",
        "D": "-..",
        "E": ".",
        "F": "..-.",
        "G": "--.",
        "H": "....",
        "I": "..",
        "J": ".---",
        "K": "-.-",
        "L": ".-..",
        "M": "--",
        "N": "-.",
        "O": "---",
        "P": ".--.",
        "Q": "--.-",
        "R": ".-.",
        "S": "...",
        "T": "-",
        "U": "..-",
        "V": "...-",
        "W": ".--",
        "X": "-..-",
        "Y": "-.--",
        "Z": "--..",
        "0": "-----",
        "1": ".----",
        "2": "..---",
        "3": "...--",
        "4": "....-",
        "5": ".....",
        "6": "-....",
        "7": "--...",
        "8": "---..",
        "9": "----.",
    }


def morse_segments(
    text: str,
    *,
    wpm: float,
    leading_silence_s: float = 0.2,
    trailing_silence_s: float = 0.4,
) -> list[tuple[bool, float]]:
    dit = 1.2 / float(wpm)
    dah = 3.0 * dit
    intra_element_gap = dit
    inter_char_gap = 3.0 * dit
    word_gap = 7.0 * dit

    out: list[tuple[bool, float]] = [(False, float(leading_silence_s))]

    cleaned = " ".join(text.strip().upper().split())
    if not cleaned:
        out.append((False, float(trailing_silence_s)))
        return out

    code_map = morse_code_map()
    for ch in cleaned:
        if ch == " ":
            out.append((False, word_gap))
            continue
        code = code_map.get(ch)
        if not code:
            continue
        for i, sym in enumerate(code):
            tone_len = dit if sym == "." else dah
            out.append((True, tone_len))
            gap = inter_char_gap if i == len(code) - 1 else intra_element_gap
            out.append((False, gap))

    out.append((False, float(trailing_silence_s)))
    return out


def apply_fade(x: np.ndarray, fade_samples: int) -> np.ndarray:
    n = int(x.size)
    fade = int(max(0, min(int(fade_samples), n // 2)))
    if fade <= 0:
        return x
    env = np.ones(n, dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, fade, endpoint=False, dtype=np.float32)
    env[:fade] = ramp
    env[-fade:] = ramp[::-1]
    return x * env


def generate_cw_wave(
    text: str,
    *,
    wpm: float = 18.0,
    tone_hz: float = 600.0,
    sample_rate: int = 48000,
    gain: float = 0.25,
    fade_ms: float = 4.0,
) -> np.ndarray:
    sr = int(sample_rate)
    if sr <= 0:
        raise ValueError(f"invalid sample_rate: {sample_rate}")
    if float(wpm) <= 0.0:
        raise ValueError(f"invalid wpm: {wpm}")

    segments = morse_segments(text, wpm=float(wpm))
    tone = float(tone_hz)
    amp = float(gain)
    fade_samples = int((float(fade_ms) / 1000.0) * sr)

    parts: list[np.ndarray] = []
    for is_tone, seconds in segments:
        n = int(round(float(seconds) * sr))
        if n <= 0:
            continue
        if not is_tone:
            parts.append(np.zeros(n, dtype=np.float32))
            continue
        t = (np.arange(n, dtype=np.float32) / float(sr)).astype(np.float32, copy=False)
        x = (amp * np.sin(2.0 * np.pi * tone * t)).astype(np.float32, copy=False)
        parts.append(apply_fade(x, fade_samples))

    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


def join_words(parts: Sequence[str]) -> str:
    return " ".join(str(x).strip() for x in parts if str(x).strip())
