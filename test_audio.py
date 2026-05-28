#!/usr/bin/env python3
"""
音频功能测试脚本
测试 sounddevice 和 pyaudio 是否正常工作
"""

import numpy as np
import sounddevice as sd
import time
import sys


def test_sounddevice():
    """测试 sounddevice 库"""
    print("=== 测试 sounddevice 库 ===")
    
    # 列出可用的音频设备
    print("可用的音频设备:")
    devices = sd.query_devices()
    print(f"默认输入设备: {sd.default.device[0]}")
    print(f"默认输出设备: {sd.default.device[1]}")
    
    for i, device in enumerate(devices):
        print(f"  {i}: {device['name']} (输入: {device['max_input_channels']}, 输出: {device['max_output_channels']})")
    
    # 生成测试音频信号 (440Hz 正弦波，持续1秒)
    print("\n生成测试音频信号...")
    sample_rate = 44100
    duration = 1.0  # 秒
    frequency = 440.0  # Hz (A4音)
    
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
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


def test_pyaudio():
    """测试 pyaudio 库"""
    print("\n=== 测试 pyaudio 库 ===")
    
    try:
        import pyaudio
        import wave
        
        print("pyaudio 导入成功")
        
        # 生成测试音频文件
        sample_rate = 44100
        duration = 1.0
        frequency = 440.0
        
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        audio_signal = 0.3 * np.sin(2 * np.pi * frequency * t)
        
        # 将音频信号转换为16位整数
        audio_int16 = (audio_signal * 32767).astype(np.int16)
        
        # 创建临时WAV文件
        temp_wav = "test_tone.wav"
        with wave.open(temp_wav, 'w') as wf:
            wf.setnchannels(1)  # 单声道
            wf.setsampwidth(2)  # 16位 = 2字节
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int16.tobytes())
        
        print(f"创建测试音频文件: {temp_wav}")
        
        # 使用 pyaudio 播放
        print("使用 pyaudio 播放...")
        p = pyaudio.PyAudio()
        
        # 打开音频文件
        wf = wave.open(temp_wav, 'rb')
        
        # 打开音频流
        stream = p.open(
            format=p.get_format_from_width(wf.getsampwidth()),
            channels=wf.getnchannels(),
            rate=wf.getframerate(),
            output=True
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


def test_audio_recording():
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
            dtype='float32'
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


def main():
    """主函数"""
    print("EchoFist 音频功能测试")
    print("=" * 40)
    
    # 检查依赖
    print("检查音频依赖...")
    try:
        import sounddevice
        print("✓ sounddevice 已安装")
    except ImportError:
        print("✗ sounddevice 未安装")
        sys.exit(1)
    
    try:
        import pyaudio
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
    else:
        print("⚠ 部分测试失败，请检查音频设备和配置")
        return 1


if __name__ == "__main__":
    sys.exit(main())