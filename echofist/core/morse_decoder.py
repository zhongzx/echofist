"""
摩尔斯解码器模块
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

from echofist.config import load_config
from echofist.logger import get_logger


@dataclass
class MorseSymbol:
    """摩尔斯符号"""

    symbol: str  # "." 或 "-"
    start_time: float
    end_time: float
    duration: float
    confidence: float


@dataclass
class MorseCharacter:
    """摩尔斯字符"""

    character: str
    symbols: list[MorseSymbol]
    start_time: float
    end_time: float
    confidence: float


class MorseDecoder:
    """摩尔斯解码器"""

    # 标准摩尔斯电码表
    MORSE_CODE: dict[str, str] = {
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
        ".": ".-.-.-",
        ",": "--..--",
        "?": "..--..",
        "'": ".----.",
        "!": "-.-.--",
        "/": "-..-.",
        "(": "-.--.",
        ")": "-.--.-",
        "&": ".-...",
        ":": "---...",
        ";": "-.-.-.",
        "=": "-...-",
        "+": ".-.-.",
        "-": "-....-",
        "_": "..--.-",
        '"': ".-..-.",
        "$": "...-..-",
        "@": ".--.-.",
        " ": " ",  # 空格表示字符间隔
    }

    # 反向查找表
    REVERSE_MORSE: dict[str, str] = {v: k for k, v in MORSE_CODE.items()}

    def __init__(self):
        self.logger = get_logger("morse_decoder")
        self.config = load_config().morse_decoder

        # 状态变量
        self.sample_rate = self.config.sample_rate
        self.smoothing_window = self.config.smoothing_window

        # 信号处理参数
        self.noise_floor = 0.01
        self.signal_threshold = 0.0
        self.adaptive_threshold = True

        # 解码状态
        self.current_symbols: list[MorseSymbol] = []
        self.current_character_symbols: list[MorseSymbol] = []
        self.last_signal_time = 0.0
        self.last_noise_time = 0.0
        self.is_in_signal = False

        # 统计信息
        self.dit_length_estimate = self.config.min_dit_length
        self.dah_length_estimate = self.dit_length_estimate * self.config.dit_dah_ratio
        self.symbol_gap_estimate = self.dit_length_estimate
        self.character_gap_estimate = self.dit_length_estimate * 3
        self.word_gap_estimate = self.dit_length_estimate * self.config.word_space_ratio

    def preprocess_audio(self, audio_data: np.ndarray) -> np.ndarray:
        """
        预处理音频数据

        Args:
            audio_data: 原始音频数据

        Returns:
            处理后的音频数据
        """
        # 转换为单声道（如果立体声）
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)

        # 归一化
        if np.max(np.abs(audio_data)) > 0:
            audio_data = audio_data / np.max(np.abs(audio_data))

        # 带通滤波（300-3000 Hz，适合 CW）
        nyquist = self.sample_rate / 2
        low = 300 / nyquist
        high = 3000 / nyquist

        b, a = signal.butter(4, [low, high], btype="band")
        filtered = signal.filtfilt(b, a, audio_data)

        # 包络检测
        envelope = np.abs(signal.hilbert(filtered))

        # 平滑
        if self.smoothing_window > 1:
            window = np.ones(self.smoothing_window) / self.smoothing_window
            envelope = np.convolve(envelope, window, mode="same")

        return envelope

    def detect_signal(
        self, envelope: np.ndarray, time_offset: float = 0.0
    ) -> list[MorseSymbol]:
        """
        检测信号中的摩尔斯符号

        Args:
            envelope: 音频包络
            time_offset: 时间偏移量

        Returns:
            检测到的摩尔斯符号列表
        """
        symbols: list[MorseSymbol] = []

        # 自适应阈值
        if self.adaptive_threshold:
            self.signal_threshold = (
                np.percentile(envelope, 70) * self.config.threshold_ratio
            )
            self.noise_floor = np.percentile(envelope, 30)

        # 确保阈值合理
        self.signal_threshold = max(self.signal_threshold, self.noise_floor * 1.5)

        # 检测信号开始和结束
        above_threshold = envelope > self.signal_threshold
        below_threshold = envelope <= self.signal_threshold

        # 找到信号段的开始和结束索引
        diff = np.diff(above_threshold.astype(int))
        signal_starts = np.where(diff == 1)[0]
        signal_ends = np.where(diff == -1)[0]

        # 处理边界情况
        if above_threshold[0]:
            signal_starts = np.insert(signal_starts, 0, 0)

        if above_threshold[-1]:
            signal_ends = np.append(signal_ends, len(envelope) - 1)

        # 确保开始和结束配对
        if len(signal_starts) > len(signal_ends):
            signal_starts = signal_starts[: len(signal_ends)]
        elif len(signal_ends) > len(signal_starts):
            signal_ends = signal_ends[: len(signal_starts)]

        # 创建符号
        for start_idx, end_idx in zip(signal_starts, signal_ends):
            duration = (end_idx - start_idx) / self.sample_rate

            # 过滤太短的信号（可能是噪声）
            if duration < self.config.min_dit_length * 0.5:
                continue

            # 确定符号类型（点或划）
            if duration < self.dit_length_estimate * 1.5:
                symbol_type = "."
            else:
                symbol_type = "-"

            # 计算置信度
            signal_strength = np.mean(envelope[start_idx:end_idx])
            confidence = min(1.0, signal_strength / (self.signal_threshold * 2))

            # 创建符号
            symbol = MorseSymbol(
                symbol=symbol_type,
                start_time=time_offset + start_idx / self.sample_rate,
                end_time=time_offset + end_idx / self.sample_rate,
                duration=duration,
                confidence=confidence,
            )

            symbols.append(symbol)

            # 更新长度估计
            if symbol_type == ".":
                self.dit_length_estimate = (
                    0.9 * self.dit_length_estimate + 0.1 * duration
                )
                self.dah_length_estimate = (
                    self.dit_length_estimate * self.config.dit_dah_ratio
                )
            else:
                self.dah_length_estimate = (
                    0.9 * self.dah_length_estimate + 0.1 * duration
                )
                self.dit_length_estimate = (
                    self.dah_length_estimate / self.config.dit_dah_ratio
                )

        return symbols

    def symbols_to_characters(self, symbols: list[MorseSymbol]) -> list[MorseCharacter]:
        """
        将符号序列转换为字符

        Args:
            symbols: 摩尔斯符号列表

        Returns:
            摩尔斯字符列表
        """
        characters: list[MorseCharacter] = []
        current_symbols: list[MorseSymbol] = []

        if not symbols:
            return characters

        # 按时间排序
        sorted_symbols = sorted(symbols, key=lambda s: s.start_time)

        for i, symbol in enumerate(sorted_symbols):
            current_symbols.append(symbol)

            # 检查是否是字符结束
            is_last_symbol = i == len(sorted_symbols) - 1
            next_symbol_gap = float("inf")

            if not is_last_symbol:
                next_symbol = sorted_symbols[i + 1]
                next_symbol_gap = next_symbol.start_time - symbol.end_time

            # 如果间隔大于字符间隔阈值，或者这是最后一个符号
            char_gap_threshold = self.character_gap_estimate * 0.7
            if next_symbol_gap > char_gap_threshold or is_last_symbol:
                # 将当前符号序列转换为字符
                if current_symbols:
                    character = self._create_character(current_symbols)
                    if character:
                        characters.append(character)

                    # 重置当前符号序列
                    current_symbols = []

        return characters

    def _create_character(self, symbols: list[MorseSymbol]) -> MorseCharacter | None:
        """从符号序列创建字符"""
        if not symbols:
            return None

        # 构建摩尔斯模式
        pattern = "".join(s.symbol for s in symbols)

        # 查找对应的字符
        character = self.REVERSE_MORSE.get(pattern)

        if character is None:
            # 尝试模糊匹配
            character = self._fuzzy_match(pattern)

        # 计算整体置信度
        confidence = np.mean([s.confidence for s in symbols])

        # 创建字符对象
        return MorseCharacter(
            character=character if character else "?",
            symbols=symbols,
            start_time=symbols[0].start_time,
            end_time=symbols[-1].end_time,
            confidence=confidence,
        )

    def _fuzzy_match(self, pattern: str) -> str | None:
        """模糊匹配摩尔斯模式"""
        if not pattern:
            return None

        # 简单的模糊匹配：允许一个符号的错误
        for code_pattern, char in self.REVERSE_MORSE.items():
            if code_pattern == " ":  # 跳过空格
                continue

            # 计算编辑距离（简单版）
            if len(pattern) == len(code_pattern):
                diff_count = sum(1 for a, b in zip(pattern, code_pattern) if a != b)
                if diff_count <= 1:
                    return char

        return None

    def characters_to_text(self, characters: list[MorseCharacter]) -> tuple[str, float]:
        """
        将字符列表转换为文本

        Args:
            characters: 摩尔斯字符列表

        Returns:
            (文本, 平均置信度)
        """
        if not characters:
            return "", 0.0

        text_parts = []
        confidences = []

        for i, char in enumerate(characters):
            text_parts.append(char.character)
            confidences.append(char.confidence)

            # 检查是否需要添加空格
            if i < len(characters) - 1:
                next_char = characters[i + 1]
                gap = next_char.start_time - char.end_time

                if gap > self.word_gap_estimate * 0.7:
                    text_parts.append(" ")

        text = "".join(text_parts).strip()
        avg_confidence = np.mean(confidences) if confidences else 0.0

        return text, avg_confidence

    def decode(
        self, audio_data: np.ndarray, time_offset: float = 0.0
    ) -> tuple[str, float]:
        """
        解码音频数据中的摩尔斯电码

        Args:
            audio_data: 音频数据
            time_offset: 时间偏移量

        Returns:
            (解码文本, 置信度)
        """
        try:
            # 预处理音频
            envelope = self.preprocess_audio(audio_data)

            # 检测符号
            symbols = self.detect_signal(envelope, time_offset)

            # 将符号转换为字符
            characters = self.symbols_to_characters(symbols)

            # 将字符转换为文本
            text, confidence = self.characters_to_text(characters)

            # 记录解码结果
            if text:
                self.logger.debug(f"解码结果: '{text}' (置信度: {confidence:.2f})")

            return text, confidence

        except Exception as e:
            self.logger.error(f"解码过程中出错: {e}")
            return "", 0.0

    def reset(self) -> None:
        """重置解码器状态"""
        self.current_symbols = []
        self.current_character_symbols = []
        self.last_signal_time = 0.0
        self.last_noise_time = 0.0
        self.is_in_signal = False

        # 重置长度估计
        self.dit_length_estimate = self.config.min_dit_length
        self.dah_length_estimate = self.dit_length_estimate * self.config.dit_dah_ratio
        self.symbol_gap_estimate = self.dit_length_estimate
        self.character_gap_estimate = self.dit_length_estimate * 3
        self.word_gap_estimate = self.dit_length_estimate * self.config.word_space_ratio
