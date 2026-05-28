"""
QSO 状态机模块
"""

import re
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from echofist.config import load_config
from echofist.logger import get_logger


class QSOState(Enum):
    """QSO 状态枚举"""

    IDLE = auto()  # 空闲状态
    LISTENING = auto()  # 监听中
    CQ_DETECTED = auto()  # 检测到 CQ
    CALLING = auto()  # 正在呼叫
    IN_QSO = auto()  # 正在通联
    EXCHANGE_RST = auto()  # 交换 RST 报告
    EXCHANGE_INFO = auto()  # 交换其他信息
    ENDING = auto()  # 结束通联
    LOGGED = auto()  # 已记录日志


@dataclass
class QSOData:
    """QSO 数据"""

    callsign: str | None = None
    rst_sent: str | None = None
    rst_received: str | None = None
    frequency: float | None = None
    mode: str = "CW"
    timestamp: float | None = None
    location: str | None = None
    notes: str | None = None


class QSOStateMachine:
    """QSO 状态机"""

    # 常见 Q 简语
    Q_CODES = {
        "QRL": "频率忙",
        "QRM": "受到干扰",
        "QRN": "天电干扰",
        "QRO": "增加功率",
        "QRP": "减小功率",
        "QRQ": "加快发报",
        "QRS": "减慢发报",
        "QRT": "停止发报",
        "QRU": "无事",
        "QRV": "准备就绪",
        "QRX": "请等待",
        "QRZ": "谁在呼叫我",
        "QSB": "信号衰落",
        "QSL": "确认收妥",
        "QSO": "直接联络",
        "QSY": "改变频率",
        "QTH": "地理位置",
    }

    # 常见通联短语
    COMMON_PHRASES = {
        "CQ": "普遍呼叫",
        "CQ CQ": "普遍呼叫",
        "DE": "来自（呼号前缀）",
        "K": "请回答",
        "KN": "请特定台回答",
        "BK": "中断插入",
        "AR": "结束",
        "SK": "通信结束",
        "73": "美好祝福",
        "88": "爱与吻",
    }

    def __init__(self, callsign: str | None = None):
        self.logger = get_logger("qso_state")
        self.config = load_config().qso

        # 状态变量
        self.state = QSOState.IDLE
        self.previous_state = QSOState.IDLE
        self.state_start_time = time.time()

        # QSO 数据
        self.qso_data = QSOData()
        if callsign:
            self.qso_data.callsign = callsign.upper()

        # 解码历史
        self.decoded_history: list[tuple[str, float]] = []  # (文本, 时间戳)
        self.history_max_length = 100

        # 自动应答建议
        self.suggestion: str | None = None
        self.suggestion_confidence = 0.0

        # 正则表达式模式
        self.callsign_pattern = re.compile(
            r"\b([A-Z]{1,2}\d{1,2}[A-Z]{1,3}|\d[A-Z]{1,3}\d{1,2}[A-Z]?)\b",
            re.IGNORECASE,
        )

        self.rst_pattern = re.compile(r"\b(\d{1,3})\b")

        self.cq_pattern = re.compile(r"\b(CQ\s*(?:CQ\s*)*)\b", re.IGNORECASE)

        # 状态转换规则
        self.transition_rules = self._build_transition_rules()

    def _build_transition_rules(self) -> dict[QSOState, dict[str, Any]]:
        """构建状态转换规则"""
        return {
            QSOState.IDLE: {
                "on_cq": QSOState.CQ_DETECTED,
                "on_call": QSOState.IN_QSO,
                "default": QSOState.LISTENING,
            },
            QSOState.LISTENING: {
                "on_cq": QSOState.CQ_DETECTED,
                "on_call": QSOState.IN_QSO,
                "timeout": QSOState.IDLE,
            },
            QSOState.CQ_DETECTED: {
                "respond": QSOState.CALLING,
                "ignore": QSOState.LISTENING,
                "timeout": QSOState.IDLE,
            },
            QSOState.CALLING: {
                "response": QSOState.IN_QSO,
                "no_response": QSOState.LISTENING,
                "timeout": QSOState.IDLE,
            },
            QSOState.IN_QSO: {
                "exchange_rst": QSOState.EXCHANGE_RST,
                "exchange_info": QSOState.EXCHANGE_INFO,
                "end": QSOState.ENDING,
            },
            QSOState.EXCHANGE_RST: {
                "received_rst": QSOState.EXCHANGE_INFO,
                "no_rst": QSOState.IN_QSO,
                "end": QSOState.ENDING,
            },
            QSOState.EXCHANGE_INFO: {
                "received_info": QSOState.ENDING,
                "no_info": QSOState.IN_QSO,
            },
            QSOState.ENDING: {
                "confirm": QSOState.LOGGED,
                "no_confirm": QSOState.IN_QSO,
            },
            QSOState.LOGGED: {"reset": QSOState.IDLE},
        }

    def process_text(self, text: str) -> None:
        """
        处理解码文本

        Args:
            text: 解码的文本
        """
        if not text or text.isspace():
            return

        # 记录到历史
        self.decoded_history.append((text, time.time()))
        if len(self.decoded_history) > self.history_max_length:
            self.decoded_history.pop(0)

        # 清理文本
        cleaned_text = text.strip().upper()

        # 根据当前状态处理文本
        if self.state == QSOState.IDLE:
            self._handle_idle_state(cleaned_text)
        elif self.state == QSOState.LISTENING:
            self._handle_listening_state(cleaned_text)
        elif self.state == QSOState.CQ_DETECTED:
            self._handle_cq_detected_state(cleaned_text)
        elif self.state == QSOState.CALLING:
            self._handle_calling_state(cleaned_text)
        elif self.state == QSOState.IN_QSO:
            self._handle_in_qso_state(cleaned_text)
        elif self.state == QSOState.EXCHANGE_RST:
            self._handle_exchange_rst_state(cleaned_text)
        elif self.state == QSOState.EXCHANGE_INFO:
            self._handle_exchange_info_state(cleaned_text)
        elif self.state == QSOState.ENDING:
            self._handle_ending_state(cleaned_text)

        # 更新自动应答建议
        self._update_suggestion()

    def _handle_idle_state(self, text: str) -> None:
        """处理空闲状态"""
        if self._is_cq(text):
            self._transition_to(QSOState.CQ_DETECTED, text)
        elif self._is_calling_me(text):
            self._transition_to(QSOState.IN_QSO, text)
        else:
            self._transition_to(QSOState.LISTENING)

    def _handle_listening_state(self, text: str) -> None:
        """处理监听状态"""
        if self._is_cq(text):
            self._transition_to(QSOState.CQ_DETECTED, text)
        elif self._is_calling_me(text):
            self._transition_to(QSOState.IN_QSO, text)

        # 检查超时
        state_duration = time.time() - self.state_start_time
        if state_duration > 30.0:  # 30秒超时
            self._transition_to(QSOState.IDLE)

    def _handle_cq_detected_state(self, text: str) -> None:
        """处理 CQ 检测状态"""
        # 提取呼号
        callsign = self._extract_callsign(text)
        if callsign:
            self.qso_data.callsign = callsign
            self.logger.info(f"检测到 CQ 呼叫来自: {callsign}")

        # 检查是否应该响应
        if self._should_respond_to_cq(text):
            self._transition_to(QSOState.CALLING)
        elif time.time() - self.state_start_time > 10.0:
            self._transition_to(QSOState.LISTENING)

    def _handle_calling_state(self, text: str) -> None:
        """处理呼叫状态"""
        if self._is_response_to_my_call(text):
            self._transition_to(QSOState.IN_QSO, text)
        elif time.time() - self.state_start_time > 15.0:
            self._transition_to(QSOState.LISTENING)

    def _handle_in_qso_state(self, text: str) -> None:
        """处理通联状态"""
        # 检查 RST 报告
        rst = self._extract_rst(text)
        if rst:
            if not self.qso_data.rst_received:
                self.qso_data.rst_received = rst
                self._transition_to(QSOState.EXCHANGE_RST)
            elif not self.qso_data.rst_sent:
                self.qso_data.rst_sent = rst
                self._transition_to(QSOState.EXCHANGE_INFO)

        # 检查结束信号
        if self._is_ending_signal(text):
            self._transition_to(QSOState.ENDING)

    def _handle_exchange_rst_state(self, text: str) -> None:
        """处理交换 RST 状态"""
        # 检查是否收到 RST
        rst = self._extract_rst(text)
        if rst:
            if not self.qso_data.rst_sent:
                self.qso_data.rst_sent = rst
                self._transition_to(QSOState.EXCHANGE_INFO)

        # 检查结束信号
        if self._is_ending_signal(text):
            self._transition_to(QSOState.ENDING)

    def _handle_exchange_info_state(self, text: str) -> None:
        """处理交换信息状态"""
        # 提取位置信息
        location = self._extract_location(text)
        if location:
            self.qso_data.location = location

        # 检查结束信号
        if self._is_ending_signal(text):
            self._transition_to(QSOState.ENDING)

    def _handle_ending_state(self, text: str) -> None:
        """处理结束状态"""
        if self._is_confirmation(text):
            self._transition_to(QSOState.LOGGED)
        elif time.time() - self.state_start_time > 5.0:
            self._transition_to(QSOState.IN_QSO)

    def _is_cq(self, text: str) -> bool:
        """检查是否是 CQ 呼叫"""
        return bool(self.cq_pattern.search(text))

    def _is_calling_me(self, text: str) -> bool:
        """检查是否在呼叫我的呼号"""
        if not self.qso_data.callsign:
            return False

        # 简单检查：文本中是否包含我的呼号
        return self.qso_data.callsign in text

    def _is_response_to_my_call(self, text: str) -> bool:
        """检查是否是对我呼叫的响应"""
        # 这里可以添加更复杂的逻辑
        # 暂时简单处理：包含常见响应短语
        response_indicators = ["RR", "ROGER", "YES", "OK", "COPY"]
        return any(indicator in text for indicator in response_indicators)

    def _is_ending_signal(self, text: str) -> bool:
        """检查是否是结束信号"""
        ending_indicators = ["73", "88", "SK", "AR", "END"]
        return any(indicator in text for indicator in ending_indicators)

    def _is_confirmation(self, text: str) -> bool:
        """检查是否是确认信号"""
        confirmation_indicators = ["QSL", "CONFIRM", "YES", "OK", "ROGER"]
        return any(indicator in text for indicator in confirmation_indicators)

    def _should_respond_to_cq(self, text: str) -> bool:
        """检查是否应该响应 CQ"""
        # 这里可以添加更复杂的逻辑
        # 暂时简单处理：总是响应
        return True

    def _extract_callsign(self, text: str) -> str | None:
        """从文本中提取呼号"""
        match = self.callsign_pattern.search(text)
        if match:
            return match.group(1).upper()
        return None

    def _extract_rst(self, text: str) -> str | None:
        """从文本中提取 RST 报告"""
        # RST 通常是 3 位数字
        matches = self.rst_pattern.findall(text)
        for match in matches:
            if len(match) == 3 and match.isdigit():
                return match
        return None

    def _extract_location(self, text: str) -> str | None:
        """从文本中提取位置信息"""
        # 简单实现：查找常见位置指示词
        location_indicators = ["QTH", "LOC", "LOCATION", "FROM"]

        for indicator in location_indicators:
            if indicator in text:
                # 提取指示词后面的内容
                parts = text.split(indicator, 1)
                if len(parts) > 1:
                    location = parts[1].strip()
                    # 清理常见标点
                    location = location.rstrip(".,;:")
                    return location

        return None

    def _transition_to(self, new_state: QSOState, context: str | None = None) -> None:
        """状态转换"""
        self.previous_state = self.state
        self.state = new_state
        self.state_start_time = time.time()

        self.logger.debug(f"状态转换: {self.previous_state.name} -> {self.state.name}")

        # 根据新状态执行操作
        if new_state == QSOState.LOGGED:
            self._log_qso()
        elif new_state == QSOState.CALLING:
            self._prepare_call(context)

    def _prepare_call(self, cq_text: str | None) -> None:
        """准备呼叫"""
        if not self.qso_data.callsign:
            return

        # 生成标准呼叫
        self.suggestion = f"DE {self.qso_data.callsign} K"
        self.suggestion_confidence = 0.9

    def _log_qso(self) -> None:
        """记录 QSO 日志"""
        self.qso_data.timestamp = time.time()

        self.logger.info("QSO 记录完成:")
        self.logger.info(f"  呼号: {self.qso_data.callsign}")
        self.logger.info(f"  RST 发送: {self.qso_data.rst_sent}")
        self.logger.info(f"  RST 接收: {self.qso_data.rst_received}")
        self.logger.info(f"  位置: {self.qso_data.location}")

        # 这里应该将数据保存到数据库
        # 暂时只记录到日志

    def _update_suggestion(self) -> None:
        """更新自动应答建议"""
        if self.state == QSOState.CQ_DETECTED:
            # 响应 CQ
            if self.qso_data.callsign:
                self.suggestion = f"DE {self.qso_data.callsign} K"
                self.suggestion_confidence = 0.9
            else:
                self.suggestion = None

        elif self.state == QSOState.EXCHANGE_RST:
            # 发送 RST 报告
            if not self.qso_data.rst_sent:
                self.suggestion = "RST 599"
                self.suggestion_confidence = 0.8

        elif self.state == QSOState.EXCHANGE_INFO:
            # 发送位置信息
            if not self.qso_data.location and self.config.default_locator:
                self.suggestion = f"QTH {self.config.default_locator}"
                self.suggestion_confidence = 0.7

        elif self.state == QSOState.ENDING:
            # 结束通联
            self.suggestion = "73"
            self.suggestion_confidence = 0.9

        else:
            self.suggestion = None

    def get_current_state(self) -> QSOState:
        """获取当前状态"""
        return self.state

    def get_qso_data(self) -> QSOData:
        """获取 QSO 数据"""
        return self.qso_data

    def has_suggestion(self) -> bool:
        """检查是否有自动应答建议"""
        return self.suggestion is not None

    def get_suggestion(self) -> str | None:
        """获取自动应答建议"""
        return self.suggestion

    def get_suggestion_confidence(self) -> float:
        """获取建议置信度"""
        return self.suggestion_confidence

    def reset(self) -> None:
        """重置状态机"""
        self.state = QSOState.IDLE
        self.previous_state = QSOState.IDLE
        self.state_start_time = time.time()

        self.qso_data = QSOData()
        if self.config.default_callsign:
            self.qso_data.callsign = self.config.default_callsign.upper()

        self.decoded_history = []
        self.suggestion = None
        self.suggestion_confidence = 0.0

        self.logger.info("QSO 状态机已重置")
