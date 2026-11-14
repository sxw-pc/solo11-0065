"""路径与本地运行边界。任何文件访问都经过根目录约束。"""

from __future__ import annotations

import os
from pathlib import Path


class PathSecurityError(ValueError):
    """路径逃逸或文件类型不符合约束。"""


def resolve_under(root: Path | str, candidate: Path | str, *, must_exist: bool = False) -> Path:
    """解析 candidate，并确保其真实路径位于 root 内（含 root 本身）。

    ``Path.resolve`` 会展开符号链接，因此既拦截 ``../`` 也拦截软链接逃逸。
    """
    base = Path(root).resolve()
    raw = Path(candidate)
    target = (base / raw if not raw.is_absolute() else raw).resolve()
    try:
        common = Path(os.path.commonpath([str(base), str(target)]))
    except ValueError as exc:  # Windows 不同盘符
        raise PathSecurityError("路径不在允许的根目录内") from exc
    if common != base:
        raise PathSecurityError(f"路径逃逸：{candidate}")
    if must_exist and not target.exists():
        raise PathSecurityError(f"文件不存在：{candidate}")
    return target


def ensure_output_path(root: Path | str, candidate: Path | str) -> Path:
    """输出文件同样不得离开项目根目录，并自动创建父目录。"""
    target = resolve_under(root, candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def ensure_local_host(host: str) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise PathSecurityError("Web 服务只允许绑定本机回环地址")
    return host
