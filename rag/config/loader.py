"""
配置加载器（rag/config/loader.py）

- YAML → AppConfig，支持 ${ENV_VAR} 环境变量插值
- 热重载：mtime 变化时重新加载（同义词表等运行时热更新）
- 敏感字段保留：保存配置时未修改的密钥不回写空值
- 凭据加密落盘：api_key/password/secret_key 以 enc:v1:... 形式存 YAML（见 secrets.py）
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from .models import LEGACY_SECTION_ALIASES, AppConfig
from .secrets import SENSITIVE_KEYS, decrypt_tree, encrypt_tree, is_untouched

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

# "留空不覆盖原值"的字段集：在凭据集合（= 加密集合）基础上再加 access_key。
# access_key 不在加密集合里（那是身份标识，加密只会让配置难读），但它是账号凭据的
# 一半，界面留空同样不该把它抹掉。
_SENSITIVE_FIELDS = SENSITIVE_KEYS | {"access_key"}


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
    # 顺序：环境变量插值 → 老段名迁移 → 凭据解密。解密放最后是因为 ${VAR} 里塞的
    # 也可能是密文，而密文本身不含 ${}，两者互不干扰
    raw = decrypt_tree(_migrate_legacy_sections(_interpolate_env(raw)), path.parent)
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
        preserve_sensitive=True 时，若更新中敏感字段为空字符串（界面留空），
        则保留文件中的原值（避免界面操作清空密钥）。
        """
        merged = merge_config_file(self._path, updates, preserve_sensitive)
        self._config = AppConfig(**_interpolate_env(merged))
        self._config._config_path = str(self._path)
        self._mtime = self._file_mtime()
        return self._config


def merge_config_file(path: str | Path, updates: dict,
                      preserve_sensitive: bool = True) -> dict:
    """读盘 → 解密 → 归一段名 → 合并 → 加密写盘，返回内存用的明文配置字典

    这是配置写盘的**唯一**入口：配置页保存（routes.save_config）与 ConfigLoader.save
    都走这里。曾经配置页自己 `yaml.safe_dump` 直接写盘，跳过了"先解密再合并"和
    "写盘前加密"两步，结果是凭据明文落盘、且未修改的凭据会被界面回传的空值抹掉
    （TS-023）。把三条不变量（解密读 / 保留原值 / 加密写）收在一个函数里，才不会
    再被某一条链路漏掉。
    """
    path = Path(path)
    raw: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # 磁盘上的凭据是密文：先解密再合并。否则 preserve_sensitive 分支会把密文当作
    # "原值"塞回内存配置，运行中的模型立刻变成拿一串 enc:v1:... 去请求接口
    raw = decrypt_tree(raw, path.parent)

    # 旧段名迁移：文件里的旧键与本次更新都先归一到新键，否则写回的文件
    # 会同时留着 mysql_meta 与 meta 两段，下一次加载以 meta 为准、
    # 旧段却一直在文件里误导读者
    raw = _migrate_legacy_sections(raw)
    updates = _migrate_legacy_sections(dict(updates))
    merged = _deep_merge(raw, updates)
    if preserve_sensitive:
        _restore_sensitive(raw, updates, merged)

    # 写盘前加密凭据；内存里的 merged 保持明文，热应用可以直接用
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(encrypt_tree(merged, path.parent), f,
                       allow_unicode=True, sort_keys=False)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(base[k], v)
        else:
            result[k] = v
    return result


def _restore_sensitive(old: dict, new: dict, merged: dict) -> None:
    """merged 中敏感字段若来自 new 的空值，恢复 old 的原值

    "空值"要同时涵盖两种：真·空串（界面留空）与 UI 回传的掩码 "******"。
    后者必须一起兜住 —— 特殊配置域是整块 JSON 编辑框，掩码会原样出现在文本里，
    一次保存就把真凭据覆盖成 6 个星号（不可逆）。
    """
    for k, v in merged.items():
        if isinstance(v, dict) and k in old and isinstance(old[k], dict):
            _restore_sensitive(old[k], new.get(k, {}), v)
        elif k in _SENSITIVE_FIELDS and is_untouched(v):
            if old.get(k):
                merged[k] = old[k]
