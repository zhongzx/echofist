"""
EchoFist - AI-assisted CW communication software for amateur radio

回声手迹 - 面向业余无线电爱好者的跨平台 AI 辅助等幅电报通讯软件
"""

__version__ = "0.1.0"
__author__ = "EchoFist Team"
__license__ = "MIT"

from echofist.config import AppConfig
from echofist.logger import setup_logger

# 导出主要模块
__all__ = [
    "AppConfig",
    "setup_logger",
]
