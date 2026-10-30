"""日志与终端输出。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

LOG_FORMAT = "%(message)s"

console = Console(emoji=False, highlight=False, soft_wrap=False)


_original_cp: int | None = None


def _restore_console_cp() -> None:
    if _original_cp is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(_original_cp)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def force_utf8_console() -> None:
    """把 Windows 控制台代码页和 Python 输出编码统一成 UTF-8。

    打包成 exe 后，Python 的 stdout 编码与 cmd/PowerShell 的代码页经常对不上，
    中文会显示成乱码。这里两边都强制成 65001，必须在任何输出之前调用。

    注意：修改代码页会影响调用方的 cmd 窗口，所以退出时还原。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    if sys.platform != "win32":
        return

    global _original_cp
    try:
        import atexit
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _original_cp = int(kernel32.GetConsoleOutputCP())
        if _original_cp != 65001:
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            atexit.register(_restore_console_cp)
    except Exception:  # noqa: BLE001
        _original_cp = None


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """控制台 + 按日切分的文件日志。"""
    log_dir.mkdir(parents=True, exist_ok=True)

    # Windows 控制台默认 GBK，强制 UTF-8 避免中文/emoji 抛异常
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    rich_handler = RichHandler(
        console=console,
        show_path=False,
        show_time=True,
        log_time_format="%H:%M:%S",
        markup=False,
        rich_tracebacks=True,
    )
    rich_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    rich_handler.setLevel(level)
    root.addHandler(rich_handler)

    logfile = log_dir / f"trader-{datetime.now():%Y%m%d}.log"
    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return logging.getLogger("qtrader")
