"""
Fist Extractor 模块
用于提取和识别CW信号中的特征
"""

import numpy as np


class FistExtractor:
    """Fist特征提取器"""

    def __init__(self, sample_rate: int = 12000):
        """
        初始化Fist提取器

        Args:
            sample_rate: 音频采样率
        """
        self.sample_rate = sample_rate

    def extract_features(self, audio_data: np.ndarray) -> dict:
        """
        从音频数据中提取特征

        Args:
            audio_data: 音频数据数组

        Returns:
            特征字典
        """
        if len(audio_data) == 0:
            return {}

        # 计算基本统计特征
        features = {
            "mean": float(np.mean(audio_data)),
            "std": float(np.std(audio_data)),
            "max": float(np.max(audio_data)),
            "min": float(np.min(audio_data)),
            "rms": float(np.sqrt(np.mean(audio_data**2))),
        }

        # 计算过零率（Zero Crossing Rate）
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio_data)))) / 2
        features["zero_crossing_rate"] = float(
            zero_crossings / len(audio_data),
        )

        # 计算频谱特征
        if len(audio_data) > 1:
            spectrum = np.abs(np.fft.rfft(audio_data))
            freq_bins = np.fft.rfftfreq(len(audio_data), 1 / self.sample_rate)

            # 找到主要频率成分
            if len(spectrum) > 0:
                dominant_idx = np.argmax(spectrum)
                features["dominant_frequency"] = float(freq_bins[dominant_idx])
                features["spectral_centroid"] = float(
                    np.sum(freq_bins * spectrum) / np.sum(spectrum)
                )

        return features

    def detect_cw_pulses(
        self, audio_data: np.ndarray, threshold: float = 0.1
    ) -> list[tuple[int, int]]:
        """
        检测CW脉冲

        Args:
            audio_data: 音频数据
            threshold: 检测阈值

        Returns:
            脉冲列表，每个脉冲为(start, end)索引
        """
        if len(audio_data) == 0:
            return []

        # 应用简单的包络检测
        envelope = np.abs(audio_data)

        # 平滑包络
        window_size = int(0.01 * self.sample_rate)  # 10ms窗口
        if window_size > 0:
            kernel = np.ones(window_size) / window_size
            envelope = np.convolve(envelope, kernel, mode="same")

        # 检测超过阈值的区域
        above_threshold = envelope > threshold
        pulses = []

        if np.any(above_threshold):
            # 找到脉冲的起始和结束
            diff = np.diff(above_threshold.astype(int))
            starts = np.where(diff == 1)[0]
            ends = np.where(diff == -1)[0]

            # 处理边界情况
            if above_threshold[0]:
                starts = np.insert(starts, 0, 0)
            if above_threshold[-1]:
                ends = np.append(ends, len(audio_data) - 1)

            # 确保starts和ends数量匹配
            if len(starts) > len(ends):
                starts = starts[: len(ends)]
            elif len(ends) > len(starts):
                ends = ends[: len(starts)]

            # 创建脉冲列表
            pulses = list(zip(starts, ends, strict=True))

        return pulses

    def analyze_pulse_pattern(self, pulses: list[tuple[int, int]]) -> dict:
        """
        分析脉冲模式

        Args:
            pulses: 脉冲列表

        Returns:
            模式分析结果
        """
        if len(pulses) < 2:
            return {"num_pulses": len(pulses)}

        # 计算脉冲持续时间和间隔
        durations = []
        intervals = []

        for i, (start, end) in enumerate(pulses):
            duration = (end - start) / self.sample_rate
            durations.append(duration)

            if i > 0:
                prev_end = pulses[i - 1][1]
                interval = (start - prev_end) / self.sample_rate
                intervals.append(interval)

        if durations:
            analysis = {
                "num_pulses": len(pulses),
                "avg_duration": float(np.mean(durations)),
                "std_duration": float(np.std(durations)),
                "min_duration": float(np.min(durations)),
                "max_duration": float(np.max(durations)),
            }

            if intervals:
                analysis.update(
                    {
                        "avg_interval": float(np.mean(intervals)),
                        "std_interval": float(np.std(intervals)),
                        "min_interval": float(np.min(intervals)),
                        "max_interval": float(np.max(intervals)),
                    }
                )

            return analysis

        return {"num_pulses": len(pulses)}
