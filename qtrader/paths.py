"""路径解析：兼容「源码运行」与「PyInstaller 打包后的 exe」。

打包后 sys._MEIPASS 是每次启动解压出来的临时目录，只读且会被清理，
所以用户数据（配置、数据库、日志）必须放在 exe 所在目录，而不是 _MEIPASS。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 随程序分发、需要复制到用户目录的模板文件
TEMPLATE_FILES = ("config.yaml", ".env.example")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """用户数据目录：源码运行时是项目根，打包后是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_path(name: str) -> Path | None:
    """打包进 exe 的只读资源路径。"""
    base = Path(getattr(sys, "_MEIPASS", app_root())) if is_frozen() else app_root()
    candidate = base / name
    return candidate if candidate.exists() else None


def ensure_user_files(root: Path) -> list[str]:
    """首次运行时把模板配置释放到 exe 旁边，让程序开箱可跑。

    返回本次新创建的文件名列表。
    """
    created: list[str] = []

    for name in TEMPLATE_FILES:
        target = root / name
        if target.exists():
            continue
        source = bundled_path(name)
        if source is None:
            continue
        try:
            if source.resolve() == target.resolve():
                continue
            shutil.copy2(source, target)
            created.append(name)
        except OSError:
            continue

    for sub in ("data", "logs"):
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    return created
