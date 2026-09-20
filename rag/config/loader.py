"""
配置加载器（rag/config/loader.py）

- YAML → AppConfig，支持 ${ENV_VAR} 环境变量插值
- 热重载：mtime 变化时重新加载（同义词表等运行时热更新）
- 敏感字段保留：保存配置时未修改的密钥不回写空值
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from .models import LEGACY_SECTION_ALIASES, AppConfig

_ENV_PATTERN = re.compile(r"\$\{([^}^{]+)\}")


def _migrate_legacy_sections(raw: dict) -> dict:
    """历史段名 → 现段名（mysql_meta → meta），就地改写并返回

    存量的 customer_config.yaml 与浏览器里缓存的老配置都还带着旧键，
    必须继续能读；这里改键之后，配置页下一次保存就会以新键回写，
    旧键自然消失 —— 用户不必手工编辑配置文件。
    新键已存在时以新键为准，旧键视为残留直接丢弃。
    """
    if not isinstance(raw, dict):
        return raw
    for old, new in LEGACY_SECTION_ALIASES.items():
        if old in raw:
            block = raw.pop(old)
            if new not in raw:
                raw[new] = block
    return raw

_SENSITIVE_FIELDS = {
    "api_key", "password", "secret_key", "jwt_secret",
    "smtp_password", "webhook_secret", "oidc_client_secret",
    "access_key",
}


def _interpolate_env(value: Any) -> Any:
    """递归替换环境变量：
    - ${VAR}          → 环境变量值（未定义保留原样）
    - ${VAR:-default} → 未定义时用默认值（bash 风格）"""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            expr = m.group(1)
            if ":-" in expr:
                var, default = expr.split(":-", 1)
                return os.environ.get(var, default)
            return os.environ.get(expr, m.group(0))
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def load_config(path: str | Path = "customer/customer_config.yaml") -> AppConfig:
    """加载 YAML 配置为 AppConfig（单次加载）"""
    path = Path(path)
    if not path.exists():
        # 无配置文件时使用全默认值（开发模式）
        cfg = AppConfig()
        cfg._config_path = str(path)
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _migrate_legacy_sections(_interpolate_env(raw))
    cfg = AppConfig(**raw)
    cfg._config_path = str(path)
    return cfg


class ConfigLoader:
    """
    带热重载的配置管理器。
    maybe_reload() 在每次请求/定时器中调用，mtime 变化时原子替换配置对象。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._config = load_config(self._path)
        self._mtime = self._file_mtime()
        self._lock = threading.Lock()

    def _file_mtime(self) -> float:
        return self._path.stat().st_mtime if self._path.exists() else 0.0

    @property
    def config(self) -> AppConfig:
        return self._config

    def maybe_reload(self) -> bool:
        """mtime 变化时重载，返回是否发生重载"""
        mtime = self._file_mtime()
        if mtime == self._mtime:
            return False
        try:
            new_cfg = load_config(self._path)
            with self._lock:
                self._config = new_cfg
                self._mtime = mtime
            return True
        except Exception:
            # 配置文件损坏时保留旧配置
            self._mtime = mtime
            return False

    # ── 配置保存（管理界面）─────────────────────────────────

    def save(self, updates: dict, preserve_sensitive: bool = True) -> AppConfig:
        """
        保存配置变更回 YAML。
        preserve_sensitive=True 时，若更新中敏感字段为空字符串，
        则保留文件中的原值（避免界面操作清空密钥）。
        """
        raw: dict = {}
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

        # 旧段名迁移：文件里的旧键与本次更新都先归一到新键，否则写回的文件
        # 会同时留着 mysql_meta 与 meta 两段，下一次加载以 meta 为准、
        # 旧段却一直在文件里误导读者
        raw = _migrate_legacy_sections(raw)
        merged = _deep_merge(raw, _migrate_legacy_sections(dict(updates)))
        if preserve_sensitive:
            _restore_sensitive(raw, updates, merged)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)

        self._config = AppConfig(**_interpolate_env(merged))
        self._config._config_path = str(self._path)
        self._mtime = self._file_mtime()
        return self._config


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(base[k], v)
        else:
            result[k] = v
    return result


def _restore_sensitive(old: dict, new: dict, merged: dict) -> None:
    """merged 中敏感字段若来自 new 的空值，恢复 old 的原值"""
    for k, v in merged.items():
        if isinstance(v, dict) and k in old and isinstance(old[k], dict):
            _restore_sensitive(old[k], new.get(k, {}), v)
        elif k in _SENSITIVE_FIELDS and v in ("", None):
            if old.get(k):
                merged[k] = old[k]
