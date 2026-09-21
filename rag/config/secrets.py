"""凭据落盘加密（rag/config/secrets.py）

背景（TS-023）：customer_config.yaml 是会被贴进工单、提交进私有仓库、打包进交付物的
文件，`api_key` / `password` / `secret_key` 这类凭据**不能**以明文躺在里面。

做法：
- 值以 `enc:v1:<fernet token>` 存 YAML；运行时内存里始终是明文，适配器无感。
- 主密钥来源优先级：
    1. 环境变量 RAG_SECRET_KEY（多实例 / 容器部署，值为 Fernet key）
    2. <配置文件目录>/.secrets.key（本机首次使用自动生成，权限尽量 0600）
- `${ENV_VAR}` / `${VAR:-default}` 引用原样保留：那是"引用"而不是明文凭据，由
  loader._interpolate_env 解析；这里只碰真正的字面量凭据。
- encrypt 幂等：已加密的值不会被二次加密。
- 解密失败**绝不静默把密文当明文用**：抛 SecretError，由调用方记 ERROR 日志并把该字段
  置空 —— 于是"模型降级"这条既有链路会把原因显示到配置页，而不是拿一串密文去请求接口。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rag.observability.logging import get_logger

_log = get_logger("config.secrets")

try:                      # 依赖缺失时不在这里炸：只有真要加解密时才报错
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except Exception:         # pragma: no cover - 环境缺 cryptography
    Fernet = None                  # type: ignore[assignment]
    InvalidToken = Exception       # type: ignore[assignment,misc]
    _HAVE_CRYPTO = False

# 密文前缀 + 版本号：将来换算法时按前缀区分，老密文仍可解
ENC_PREFIX = "enc:v1:"
# 本机密钥文件名（与配置文件同目录，已被 .gitignore 忽略）
KEY_FILE_NAME = ".secrets.key"
KEY_ENV = "RAG_SECRET_KEY"

# ── 凭据字段名（精确匹配，不做子串匹配）──────────────────────────
# 这份名单同时决定三件事：① 落盘加密 ② UI 打码 ③ 保存时"留空/掩码不覆盖原值"。
# 刻意**不**包含：
#   - access_key：它是身份标识而非密文（MinIO/S3 控制台也明文展示），加密它只会让
#     配置文件难以阅读，安全性却没有提升
#   - sensitive_fields：它本身就是"哪些字段算敏感"的名字清单，加密它就没法读了
#   - max_tokens：名字里含 token 但是数值参数 —— 所以这里只能精确匹配
SENSITIVE_KEYS = frozenset({
    "api_key", "password", "secret", "secret_key", "jwt_secret",
    "smtp_password", "webhook_secret", "oidc_client_secret", "token", "dsn",
})


class SecretError(Exception):
    """凭据加解密失败（密钥文件丢失/损坏、依赖缺失、密钥格式不对）"""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


# UI 脱敏掩码：只用于页面显示。任何"写盘 / 发请求"的路径都必须把它当成
# "未修改"而不是真值，见 is_untouched()。
MASK = "******"


def is_untouched(value: Any) -> bool:
    """界面回传的"这一项我没改"标记：None、空串、或脱敏掩码

    掩码必须算作未修改：特殊配置域是整块 JSON 编辑框，掩码会原样出现在文本里，
    一旦被当成新值保存/发出，真凭据就被 6 个星号覆盖（不可逆），或者变成一次 401。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", MASK)
    return False


# UI 用等量圆点表示"这条已经存着钥匙"（实体值：能选中、能整串删掉，见 config-ui.js
# 的凭据框渲染）。它是显示占位，不是钥匙 —— 一串圆点绝不可能是真凭据，一旦被当成
# 值发出/写盘，真钥匙就被几个圆点换掉（不可逆）。所以凡是要"用值"的地方都得绕开它。
# 模型段的 api_key 现在下发的是解密后的真值（见 web/routes._param_row 的 reveal），框里不再
# 出现这种占位；这里保留判定当兜底：旧页面、或别的凭据传来的圆点串一律按"没改"处理。
DOTS_CHAR = "•"


def is_display_dots(value: Any) -> bool:
    """值是不是 UI 画出来的那串圆点（非空且全部由圆点组成）"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(s) and set(s) == {DOTS_CHAR}


def _secret_key(key_dir: Path) -> bytes:
    """取主密钥：环境变量优先，其次本机密钥文件（首次自动生成）"""
    env = str(os.environ.get(KEY_ENV) or "").strip()
    if env:
        return env.encode("utf-8")
    path = key_dir / KEY_FILE_NAME
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    # 首次使用：生成并落盘。注意不要把密钥本身写进日志（日志会进工单/CI 输出）
    key = Fernet.generate_key()
    key_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(key.decode("utf-8") + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)   # Windows 上尽力而为（真实权限由 NTFS ACL 决定）
    except OSError:
        pass
    _log.info("config_secret_key_created", path=str(path))
    return key


def _fernet(key_dir: Path):
    if not _HAVE_CRYPTO:
        raise SecretError("缺少 cryptography 依赖，无法加解密凭据；"
                          "请执行 pip install -r requirements.txt")
    try:
        return Fernet(_secret_key(key_dir))
    except SecretError:
        raise
    except Exception as e:
        raise SecretError(
            f"{KEY_ENV} 不是合法的 Fernet 密钥（应为 44 字符 base64url）: {e}") from e


def encrypt(text: Any, key_dir: str | Path = ".") -> Any:
    """明文 → `enc:v1:...`；空值 / 已是密文 / `${ENV}` 引用时原样返回"""
    if not isinstance(text, str) or not text.strip():
        return text
    if is_encrypted(text) or text.lstrip().startswith("${"):
        return text
    token = _fernet(Path(key_dir)).encrypt(text.encode("utf-8"))
    return ENC_PREFIX + token.decode("utf-8")


def decrypt(text: Any, key_dir: str | Path = ".") -> Any:
    """`enc:v1:...` → 明文；非密文原样返回；解不开抛 SecretError"""
    if not is_encrypted(text):
        return text
    token = text[len(ENC_PREFIX):]
    try:
        return _fernet(Path(key_dir)).decrypt(token.encode("utf-8")).decode("utf-8")
    except SecretError:
        raise
    except InvalidToken as e:
        raise SecretError(
            f"凭据解密失败：密钥与密文不匹配。请确认密钥文件 "
            f"{Path(key_dir) / KEY_FILE_NAME}（或环境变量 {KEY_ENV}）没有被更换、丢失，"
            f"或者把该字段改成明文/环境变量引用后重新保存") from e


def encrypt_tree(obj: Any, key_dir: str | Path = ".") -> Any:
    """写盘前递归加密凭据字段（返回新对象，不动调用方内存里的明文配置）"""
    if isinstance(obj, dict):
        return {k: (encrypt(v, key_dir)
                    if isinstance(v, str) and k in SENSITIVE_KEYS
                    else encrypt_tree(v, key_dir))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [encrypt_tree(v, key_dir) for v in obj]
    return obj


def decrypt_tree(obj: Any, key_dir: str | Path = ".") -> Any:
    """载入后递归解密凭据字段；单个字段解不开时记 ERROR 并置空该字段

    置空（而不是保留密文）是刻意的：密文送到模型服务端必然是一次 401，
    日志里那句"密文当 key 用了"很难排查；置空则走既有的"未配置 → 降级"链路，
    配置页会直接把降级原因显示出来。
    """
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if isinstance(v, str) and k in SENSITIVE_KEYS:
                try:
                    out[k] = decrypt(v, key_dir)
                except SecretError as e:
                    _log.error("config_secret_undecryptable", field=k, error=str(e))
                    out[k] = ""
            else:
                out[k] = decrypt_tree(v, key_dir)
        return out
    if isinstance(obj, list):
        return [decrypt_tree(v, key_dir) for v in obj]
    return obj
