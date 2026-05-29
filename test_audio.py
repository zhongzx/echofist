#!/usr/bin/env python3
"""
音频功能测试脚本
测试 sounddevice 和 pyaudio 是否正常工作
"""

import importlib
import sys
from collections.abc import Sequence

import numpy as np
import sounddevice as sd

from echofist.core.audio_generation import generate_cw_wave, join_words


def test_sounddevice() -> bool:
    """测试 sounddevice 库"""
    print("=== 测试 sounddevice 库 ===")

    # 列出可用的音频设备
    print("可用的音频设备:")
    devices = sd.query_devices()
    print(f"默认输入设备: {sd.default.device[0]}")
    print(f"默认输出设备: {sd.default.device[1]}")

    for i, device in enumerate(devices):
        name = device["name"]
        max_input = device["max_input_channels"]
        max_output = device["max_output_channels"]
        print(f"  {i}: {name} (输入: {max_input}, 输出: {max_output})")

    # 生成测试音频信号 (440Hz 正弦波，持续1秒)
    print("\n生成测试音频信号...")
    sample_rate = 44100
    duration = 1.0  # 秒
    frequency = 440.0  # Hz (A4音)

    t = np.linspace(
        0,
        duration,
        int(sample_rate * duration),
        endpoint=False,
    )
    audio_signal = 0.3 * np.sin(2 * np.pi * frequency * t)

    # 播放音频
    print(f"播放 {frequency}Hz 正弦波 ({duration}秒)...")
    try:
        sd.play(audio_signal, sample_rate)
        sd.wait()  # 等待播放完成
        print("✓ sounddevice 播放成功")
        return True
    except Exception as e:
        print(f"✗ sounddevice 播放失败: {e}")
        return False


def test_pyaudio() -> bool:
    """测试 pyaudio 库"""
    print("\n=== 测试 pyaudio 库 ===")

    try:
        import wave

        import pyaudio

        print("pyaudio 导入成功")

        # 生成测试音频文件
        sample_rate = 44100
        duration = 1.0
        frequency = 440.0

        t = np.linspace(
            0,
            duration,
            int(sample_rate * duration),
            endpoint=False,
        )
        audio_signal = 0.3 * np.sin(2 * np.pi * frequency * t)

        # 将音频信号转换为16位整数
        audio_int16 = (audio_signal * 32767).astype(np.int16)

        # 创建临时WAV文件
        temp_wav = "test_tone.wav"
        with wave.open(temp_wav, "w") as wf:
            wf.setnchannels(1)  # 单声道
            wf.setsampwidth(2)  # 16位 = 2字节
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int16.tobytes())

        print(f"创建测试音频文件: {temp_wav}")

        # 使用 pyaudio 播放
        print("使用 pyaudio 播放...")
        p = pyaudio.PyAudio()

        # 打开音频文件
        wf = wave.open(temp_wav, "rb")

        # 打开音频流
        stream = p.open(
            format=p.get_format_from_width(wf.getsampwidth()),
            channels=wf.getnchannels(),
            rate=wf.getframerate(),
            output=True,
        )

        # 读取并播放数据
        data = wf.readframes(1024)
        while data:
            stream.write(data)
            data = wf.readframes(1024)

        # 清理
        stream.stop_stream()
        stream.close()
        p.terminate()
        wf.close()

        # 删除临时文件
        import os

        os.remove(temp_wav)

        print("✓ pyaudio 播放成功")
        return True

    except ImportError:
        print("✗ 无法导入 pyaudio")
        return False
    except Exception as e:
        print(f"✗ pyaudio 播放失败: {e}")
        return False


def test_audio_recording() -> bool:
    """测试音频录制功能"""
    print("\n=== 测试音频录制功能 ===")

    try:
        print("录制3秒音频...")
        print("请说话或制造一些声音")

        sample_rate = 44100
        duration = 3.0

        # 录制音频
        recording = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )

        print("正在录制...")
        sd.wait()

        # 计算录制音频的RMS值（音量）
        rms = np.sqrt(np.mean(recording**2))
        print(f"录制音频RMS值: {rms:.6f}")

        if rms > 0.001:  # 如果有声音
            print("✓ 音频录制成功检测到声音")

            # 播放录制的音频
            print("播放录制的音频...")
            sd.play(recording, sample_rate)
            sd.wait()
            print("✓ 录制音频播放成功")
            return True
        else:
            print("⚠ 录制成功但未检测到明显声音")
            return True

    except Exception as e:
        print(f"✗ 音频录制失败: {e}")
        return False


def play_cw_text(
    text: str,
    *,
    wpm: float = 18.0,
    tone_hz: float = 600.0,
    sample_rate: int = 48000,
    gain: float = 0.25,
) -> None:
    wave = generate_cw_wave(
        text,
        wpm=wpm,
        tone_hz=tone_hz,
        sample_rate=sample_rate,
        gain=gain,
    )
    if wave.size == 0:
        raise ValueError("CW 文本为空或无法生成")
    sd.play(wave, int(sample_rate))
    sd.wait()


def _load_config_callsign() -> str | None:
    try:
        from echofist.config import load_config

        cfg = load_config()
        callsign = cfg.qso.default_callsign
        if isinstance(callsign, str):
            callsign = callsign.strip()
        return callsign or None
    except Exception:
        return None


def _parse_args(
    argv: Sequence[str],
) -> tuple[str | None, float, float, float, int, bool]:
    import argparse

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--cw",
        nargs="+",
        default=None,
        help="播放指定文本的 CW 音频（可包含空格，例如 de bg5dnl cq）",
    )
    parser.add_argument(
        "--cw-callsign",
        action="store_true",
        help="从本机 EchoFist 配置中读取默认呼号并播放 CW",
    )
    parser.add_argument("--wpm", type=float, default=18.0, help="CW 速度 (WPM)")
    parser.add_argument("--tone", type=float, default=600.0, help="音调频率 (Hz)")
    parser.add_argument("--gain", type=float, default=0.25, help="音量增益 (0-1)")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="采样率 (Hz)",
    )
    args = parser.parse_args(list(argv))
    cw_text: str | None
    if args.cw is None:
        cw_text = None
    else:
        cw_text = join_words(args.cw) or None
    return (
        cw_text,
        float(args.wpm),
        float(args.tone),
        float(args.gain),
        int(args.sample_rate),
        bool(args.cw_callsign),
    )


def main() -> int:
    """主函数"""
    cw_text, wpm, tone_hz, gain, sample_rate, cw_callsign = _parse_args(sys.argv[1:])
    if cw_callsign and cw_text is None:
        cw_text = _load_config_callsign()
        if cw_text is None:
            print("未在 EchoFist 配置中找到默认呼号")
            print("可先运行: python -m echofist register")
            print("或直接指定: python test_audio.py --cw <你的呼号>")
            return 1

    if cw_text is not None:
        print("=== CW 播放测试 ===")
        print(f"文本: {cw_text}")
        print(f"WPM: {wpm}")
        print(f"音调: {tone_hz} Hz")
        print(f"采样率: {sample_rate} Hz")
        print("正在播放...")
        play_cw_text(
            cw_text,
            wpm=wpm,
            tone_hz=tone_hz,
            sample_rate=sample_rate,
            gain=gain,
        )
        print("播放完成")
        return 0

    print("EchoFist 音频功能测试")
    print("=" * 40)

    # 检查依赖
    print("检查音频依赖...")
    try:
        importlib.import_module("sounddevice")
        print("✓ sounddevice 已安装")
    except ImportError:
        print("✗ sounddevice 未安装")
        return 1

    try:
        importlib.import_module("pyaudio")
        print("✓ pyaudio 已安装")
    except ImportError:
        print("⚠ pyaudio 未安装，部分测试将跳过")

    print("\n开始音频测试...")

    # 运行测试
    tests_passed = 0
    total_tests = 0

    # 测试1: sounddevice
    total_tests += 1
    if test_sounddevice():
        tests_passed += 1

    # 测试2: pyaudio
    total_tests += 1
    if test_pyaudio():
        tests_passed += 1

    # 测试3: 音频录制
    total_tests += 1
    if test_audio_recording():
        tests_passed += 1

    # 测试结果
    print("\n" + "=" * 40)
    print(f"测试完成: {tests_passed}/{total_tests} 通过")

    if tests_passed == total_tests:
        print("✅ 所有音频测试通过！")
        return 0

    print("⚠ 部分测试失败，请检查音频设备和配置")
    return 1


if __name__ == "__main__":
    sys.exit(main())
