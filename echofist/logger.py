"""
日志配置模块
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loguru import Logger

from loguru import logger


def setup_logger(
    log_level: str = "INFO",
    log_file: str | None = None,
    rotation: str = "10 MB",
    retention: str = "30 days",
) -> "Logger":
    """
    配置日志系统

    Args:
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
        log_file: 日志文件路径，如果为None则不写入文件
        rotation: 日志轮转条件
        retention: 日志保留时间

    Returns:
        配置好的logger实例
    """
    # 移除默认的handler
    logger.remove()

    # 控制台输出配置
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    logger.add(
        sys.stderr,
        format=console_format,
        level=log_level,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # 文件输出配置（如果指定了日志文件）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_format = (
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        )

        logger.add(
            str(log_path),
            format=file_format,
            level=log_level,
            rotation=rotation,
            retention=retention,
            compression="zip",
            backtrace=True,
            diagnose=True,
            enqueue=True,  # 线程安全
        )

    # 添加自定义级别（如果尚未存在）
    try:
        logger.level("SUCCESS", no=25, color="<green>")
    except ValueError:
        # SUCCESS级别已经存在，忽略错误
        pass

    return logger


def get_default_log_path() -> Path:
    """获取默认日志文件路径"""
    # 优先使用XDG状态目录
    xdg_state_home = Path.home() / ".local" / "state"
    if xdg_state_home.exists():
        log_dir = xdg_state_home / "echofist"
    else:
        # 回退到用户主目录
        log_dir = Path.home() / ".echofist" / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "echofist.log"


class LogContext:
    """日志上下文管理器"""

    def __init__(self, component: str):
        """
        初始化日志上下文

        Args:
            component: 组件名称
        """
        self.component = component
        self.logger = logger.bind(component=component)

    def debug(self, message: str, **kwargs: Any) -> None:
        """记录调试信息"""
        self.logger.debug(message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """记录普通信息"""
        self.logger.info(message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """记录警告信息"""
        self.logger.warning(message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """记录错误信息"""
        self.logger.error(message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        """记录异常信息"""
        self.logger.exception(message, **kwargs)

    def success(self, message: str, **kwargs: Any) -> None:
        """记录成功信息"""
        self.logger.log("SUCCESS", message, **kwargs)

    def with_fields(self, **kwargs: Any) -> "Logger":
        """创建带有额外字段的logger"""
        return self.logger.bind(**kwargs)


# 创建全局logger实例
_default_logger = setup_logger()


def get_logger(component: str | None = None) -> LogContext:
    """
    获取logger实例

    Args:
        component: 组件名称，如果为None则返回全局logger

    Returns:
        LogContext实例
    """
    if component is None:
        return LogContext("global")
    return LogContext(component)


# 导出常用函数
debug = _default_logger.debug
info = _default_logger.info
warning = _default_logger.warning
error = _default_logger.error
exception = _default_logger.exception


def success(msg: str, **kwargs: Any) -> None:
    """记录成功日志"""
    _default_logger.log("SUCCESS", msg, **kwargs)
