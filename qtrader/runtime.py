"""运行时装配：把配置组装成可运行的对象图。

CLI 与 Web 控制台共用同一套装配逻辑，避免两条路径行为不一致。
"""

from __future__ import annotations

import logging

from .ai.deepseek import DeepSeekClient
from .brokers import BrokerAdapter, build_broker
from .config import AppConfig
from .market.quotes import MarketData
from .risk.manager import RiskManager
from .storage import Storage

log = logging.getLogger(__name__)


class Runtime:
    """一组配套的对象，负责统一释放。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = Storage(cfg.resolve(cfg.storage.db_path))
        self.market = MarketData(
            timeout=cfg.ai.timeout_seconds,
            retries=cfg.ai.max_retries,
            bar_ttl=cfg.strategy.bar_refresh_seconds,
        )
        self.ai = DeepSeekClient(cfg.ai, self.store)
        self.broker: BrokerAdapter = build_broker(cfg)
        self.risk = RiskManager(cfg.risk, cfg.strategy, self.store)

    def close(self) -> None:
        try:
            self.broker.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭券商连接失败")
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭数据库失败")

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def build_runtime(cfg: AppConfig) -> Runtime:
    return Runtime(cfg)
