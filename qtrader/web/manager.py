"""引擎生命周期管理：把主循环放到后台线程，供网页启停。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..config import load_config, validate_for_run
from ..engine import TradingEngine
from ..runtime import Runtime

log = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    """把日志环形缓存起来供网页轮询，避免前端另开日志文件。"""

    def __init__(self, capacity: int = 800):
        super().__init__()
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self.setFormatter(logging.Formatter("%(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._seq += 1
            self._items.append(
                {
                    "seq": self._seq,
                    "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "msg": message,
                }
            )

    def since(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            lines = [item for item in self._items if item["seq"] > after]
            return {"seq": self._seq, "lines": lines}


class EngineManager:
    """同一时刻只允许一个引擎实例运行。"""

    def __init__(self, root: Path):
        self.root = root
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "idle"
        self.mode = ""
        self.error = ""
        self.message = ""
        self.started_at: float = 0.0
        self.runtime: Runtime | None = None

    # ----------------------------- 状态 -----------------------------
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        running = self.is_running()
        return {
            "state": self.state if running else ("error" if self.error else "idle"),
            "running": running,
            "mode": self.mode,
            "error": self.error,
            "message": self.message,
            "uptime_seconds": int(time.time() - self.started_at) if running else 0,
        }

    # ----------------------------- 控制 -----------------------------
    def start(self, live_ack: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                raise RuntimeError("引擎已在运行中，请先停止")

            cfg = load_config("config.yaml", root=self.root)
            errors = validate_for_run(cfg, allow_live=live_ack)
            if errors:
                raise RuntimeError("；".join(errors))

            self._stop = threading.Event()
            self.error = ""
            self.message = ""
            self.mode = (
                "半自动 LIVE" if cfg.is_live and cfg.is_manual
                else "实盘 LIVE" if cfg.is_live
                else "模拟盘 PAPER"
            )
            self.state = "starting"
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(cfg,), name="trading-engine", daemon=True
            )
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        if not self.is_running():
            return self.status()
        self.state = "stopping"
        self.message = "正在停止…"
        self._stop.set()
        return self.status()

    def _run(self, cfg) -> None:
        rt: Runtime | None = None
        try:
            rt = Runtime(cfg)
            self.runtime = rt
            engine = TradingEngine(
                cfg, rt.store, rt.market, rt.ai, rt.broker, rt.risk,
                stop_event=self._stop,
            )
            self.state = "running"
            self.message = ""
            engine.run()
            self.message = "已按指令停止" if self._stop.is_set() else "已退出"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.state = "error"
            log.exception("引擎异常退出")
        finally:
            self.runtime = None
            self.state = "idle" if not self.error else "error"
            if rt is not None:
                rt.close()


def execute_pending_order(manager: EngineManager, cfg, order: dict) -> dict:
    """把人工已执行的委托回填到本地账户，保持持仓与净值连续。"""
    symbol = order["symbol"]
    side = order["side"]
    qty = int(order["qty"])
    price = float(order["price"])
    name = order.get("name", "")

    running = manager.is_running() and manager.runtime is not None
    rt = manager.runtime if running else Runtime(cfg)
    try:
        if not running:
            rt.broker.connect()
        if side == "BUY":
            result = rt.broker.buy(symbol, price, qty, name)
        else:
            result = rt.broker.sell(symbol, price, qty, name)
        return {
            "success": result.success,
            "message": result.message,
            "filled_price": result.filled_price,
            "qty": result.filled_qty,
            "fee": result.fee,
        }
    finally:
        if not running:
            rt.close()
