"""
配置管理模块
"""

import base64
import hashlib
import secrets
from pathlib import Path
from typing import Any

import toml
from pydantic import BaseModel, Field


class KiwiSDRConfig(BaseModel):
    """KiwiSDR配置"""

    default_server: str = Field("sdr.example.com:8073", description="默认服务器")
    timeout: int = Field(30, description="连接超时时间（秒）")
    reconnect_attempts: int = Field(3, description="重连尝试次数")
    reconnect_delay: float = Field(5.0, description="重连延迟（秒）")


class MorseDecoderConfig(BaseModel):
    """摩尔斯解码器配置"""

    sample_rate: int = Field(12000, description="采样率（Hz）")
    threshold_ratio: float = Field(0.3, description="信号检测阈值比例")
    min_dit_length: float = Field(0.05, description="最小点长度（秒）")
    max_dit_length: float = Field(0.2, description="最大点长度（秒）")
    dit_dah_ratio: float = Field(3.0, description="点划长度比例")
    word_space_ratio: float = Field(7.0, description="字间距比例")
    smoothing_window: int = Field(5, description="平滑窗口大小")


class QSOConfig(BaseModel):
    """QSO配置"""

    default_callsign: str | None = Field(None, description="默认呼号")
    default_locator: str | None = Field(None, description="默认网格定位")
    operator_email: str | None = Field(None, description="默认邮箱")
    auto_log: bool = Field(True, description="自动记录日志")
    log_format: str = Field("ADIF", description="日志格式")
    contest_mode: bool = Field(False, description="比赛模式")


class SecurityConfig(BaseModel):
    """安全配置"""

    api_key_salt_b64: str | None = Field(None, description="API密钥盐（Base64）")
    api_key_hash_b64: str | None = Field(None, description="API密钥哈希（Base64）")


class UIConfig(BaseModel):
    """界面配置"""

    theme: str = Field("dark", description="主题")
    refresh_rate: float = Field(4.0, description="刷新率（Hz）")
    show_waterfall: bool = Field(True, description="显示瀑布图")
    show_signal_strength: bool = Field(True, description="显示信号强度")
    show_confidence: bool = Field(True, description="显示置信度")


class AppConfig(BaseModel):
    """应用配置"""

    kiwi_sdr: KiwiSDRConfig = Field(default_factory=KiwiSDRConfig)
    morse_decoder: MorseDecoderConfig = Field(
        default_factory=MorseDecoderConfig,
    )
    qso: QSOConfig = Field(default_factory=QSOConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_dir: str | None = None):
        """
        初始化配置管理器

        Args:
            config_dir: 配置目录路径，如果为None则使用默认目录
        """
        if config_dir is None:
            self.config_file = self.get_default_config_file()
            self.config_dir = self.config_file.parent
        else:
            self.config_dir = Path(config_dir)
            self.config_file = self.config_dir / "config.toml"
        self._config: AppConfig | None = None

        # 确保配置目录存在
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_default_config_file() -> Path:
        current_file = Path.cwd() / "echofist.toml"
        if current_file.exists():
            return current_file

        home = Path.home()
        new_file = home / ".echofist" / "config.toml"

        legacy_file = home / ".config" / "echofist" / "config.toml"
        if legacy_file.exists() and not new_file.exists():
            try:
                config_data = toml.load(legacy_file)
                new_file.parent.mkdir(parents=True, exist_ok=True)
                with open(new_file, "w", encoding="utf-8") as f:
                    toml.dump(config_data, f)
            except Exception:
                pass

        return new_file

    def load_config(self) -> AppConfig:
        """加载配置"""
        if self._config is not None:
            return self._config

        if self.config_file.exists():
            try:
                config_data = toml.load(self.config_file)
                self._config = AppConfig(**config_data)
                return self._config
            except Exception as e:
                print(f"加载配置文件失败: {e}")
                print("使用默认配置")

        # 创建默认配置
        self._config = AppConfig()
        self.save_config(self._config)
        return self._config

    def save_config(self, config: AppConfig) -> None:
        """保存配置"""
        try:
            config_dict = config.model_dump()
            with open(self.config_file, "w", encoding="utf-8") as f:
                toml.dump(config_dict, f)
            self._config = config
        except Exception as e:
            print(f"保存配置文件失败: {e}")

    def update_config(self, updates: dict[str, Any]) -> AppConfig:
        """更新配置"""
        config = self.load_config()
        config_dict = config.model_dump()

        # 递归更新配置字典
        def update_dict(
            d: dict[str, Any],
            u: dict[str, Any],
        ) -> dict[str, Any]:
            for k, v in u.items():
                if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                    d[k] = update_dict(d[k], v)
                else:
                    d[k] = v
            return d

        updated_dict = update_dict(config_dict, updates)
        updated_config = AppConfig(**updated_dict)
        self.save_config(updated_config)
        return updated_config

    def get_config_path(self) -> Path:
        """获取配置文件路径"""
        return self.config_file

    def reset_to_defaults(self) -> AppConfig:
        """重置为默认配置"""
        self._config = AppConfig()
        self.save_config(self._config)
        return self._config


# 全局配置管理器实例
_config_manager: ConfigManager | None = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def load_config() -> AppConfig:
    """加载配置（便捷函数）"""
    return get_config_manager().load_config()


def save_config(config: AppConfig) -> None:
    """保存配置（便捷函数）"""
    get_config_manager().save_config(config)


def update_config(updates: dict[str, Any]) -> AppConfig:
    """更新配置（便捷函数）"""
    return get_config_manager().update_config(updates)


def get_config_path() -> Path:
    """获取配置文件路径（便捷函数）"""
    return get_config_manager().get_config_path()


def reset_config() -> AppConfig:
    """重置配置（便捷函数）"""
    return get_config_manager().reset_to_defaults()


def generate_api_key() -> str:
    raw = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii")
    return raw.rstrip("=")


def hash_api_key(api_key: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        salt,
        200_000,
    )


def generate_api_key_record() -> tuple[str, str, str]:
    api_key = generate_api_key()
    salt = secrets.token_bytes(16)
    digest = hash_api_key(api_key, salt)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return api_key, salt_b64, digest_b64
