"""
音频处理器模块
用于音频信号的处理和增强
"""


import numpy as np


class AudioProcessor:
    """音频处理器"""

    def __init__(self, sample_rate: int = 12000):
        """
        初始化音频处理器

        Args:
            sample_rate: 音频采样率
        """
        self.sample_rate = sample_rate

    def normalize(self, audio_data: np.ndarray) -> np.ndarray:
        """
        归一化音频数据

        Args:
            audio_data: 音频数据

        Returns:
            归一化后的音频数据
        """
        if len(audio_data) == 0:
            return audio_data

        max_val = np.max(np.abs(audio_data))
        if max_val > 0:
            return audio_data / max_val
        return audio_data

    def apply_filter(
        self,
        audio_data: np.ndarray,
        filter_type: str = "bandpass",
        low_cut: float = 500.0,
        high_cut: float = 1500.0,
    ) -> np.ndarray:
        """
        应用滤波器

        Args:
            audio_data: 音频数据
            filter_type: 滤波器类型 ("lowpass", "highpass", "bandpass")
            low_cut: 低截止频率 (Hz)
            high_cut: 高截止频率 (Hz)

        Returns:
            滤波后的音频数据
        """
        if len(audio_data) < 2:
            return audio_data

        # 使用简单的FFT滤波
        spectrum = np.fft.rfft(audio_data)
        freq_bins = np.fft.rfftfreq(len(audio_data), 1 / self.sample_rate)

        # 创建滤波器掩码
        mask = np.ones_like(freq_bins, dtype=float)

        if filter_type == "lowpass":
            mask[freq_bins > high_cut] = 0
        elif filter_type == "highpass":
            mask[freq_bins < low_cut] = 0
        elif filter_type == "bandpass":
            mask[(freq_bins < low_cut) | (freq_bins > high_cut)] = 0

        # 应用滤波器
        filtered_spectrum = spectrum * mask
        filtered_audio = np.fft.irfft(filtered_spectrum, n=len(audio_data))

        return filtered_audio

    def remove_noise(
        self, audio_data: np.ndarray, threshold: float = 0.1
    ) -> np.ndarray:
        """
        去除噪声

        Args:
            audio_data: 音频数据
            threshold: 噪声阈值

        Returns:
            去噪后的音频数据
        """
        if len(audio_data) == 0:
            return audio_data

        # 简单的阈值去噪
        denoised = audio_data.copy()
        denoised[np.abs(denoised) < threshold] = 0

        return denoised

    def amplify(self, audio_data: np.ndarray, gain: float = 2.0) -> np.ndarray:
        """
        放大音频信号

        Args:
            audio_data: 音频数据
            gain: 增益倍数

        Returns:
            放大后的音频数据
        """
        return audio_data * gain

    def detect_silence(
        self, audio_data: np.ndarray, threshold: float = 0.01, min_duration: float = 0.1
    ) -> np.ndarray:
        """
        检测静音段

        Args:
            audio_data: 音频数据
            threshold: 静音检测阈值
            min_duration: 最小静音持续时间 (秒)

        Returns:
            静音掩码，True表示静音
        """
        if len(audio_data) == 0:
            return np.array([], dtype=bool)

        # 计算每个样本是否低于阈值
        below_threshold = np.abs(audio_data) < threshold

        # 找到连续静音段
        diff = np.diff(np.concatenate(([0], below_threshold.astype(int), [0])))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]

        # 创建静音掩码
        silence_mask = np.zeros(len(audio_data), dtype=bool)

        for start, end in zip(starts, ends):
            duration = (end - start) / self.sample_rate
            if duration >= min_duration:
                silence_mask[start:end] = True

        return silence_mask

    def split_by_silence(
        self,
        audio_data: np.ndarray,
        threshold: float = 0.01,
        min_silence: float = 0.2,
        min_segment: float = 0.05,
    ) -> list:
        """
        根据静音分割音频

        Args:
            audio_data: 音频数据
            threshold: 静音检测阈值
            min_silence: 最小静音持续时间 (秒)
            min_segment: 最小音频段持续时间 (秒)

        Returns:
            分割后的音频段列表
        """
        if len(audio_data) == 0:
            return []

        # 检测静音
        silence_mask = self.detect_silence(audio_data, threshold, min_silence)

        # 找到非静音段的起始和结束
        diff = np.diff(np.concatenate(([0], (~silence_mask).astype(int), [0])))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]

        segments = []

        for start, end in zip(starts, ends):
            duration = (end - start) / self.sample_rate
            if duration >= min_segment:
                segments.append(audio_data[start:end])

        return segments

    def compute_snr(
        self, audio_data: np.ndarray, signal_threshold: float = 0.05
    ) -> float:
        """
        计算信噪比 (SNR)

        Args:
            audio_data: 音频数据
            signal_threshold: 信号检测阈值

        Returns:
            信噪比 (dB)
        """
        if len(audio_data) == 0:
            return 0.0

        # 分离信号和噪声
        signal_mask = np.abs(audio_data) > signal_threshold
        noise_mask = ~signal_mask

        if np.any(signal_mask) and np.any(noise_mask):
            signal_power = np.mean(audio_data[signal_mask] ** 2)
            noise_power = np.mean(audio_data[noise_mask] ** 2)

            if noise_power > 0:
                snr = 10 * np.log10(signal_power / noise_power)
                return float(snr)

        return 0.0
