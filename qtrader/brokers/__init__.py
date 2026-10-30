"""券商适配层。"""

from __future__ import annotations

from ..config import AppConfig
from .base import Account, BrokerAdapter, OrderResult, Position

__all__ = ["Account", "BrokerAdapter", "OrderResult", "Position", "build_broker"]


def build_broker(cfg: AppConfig) -> BrokerAdapter:
    """按配置构造券商适配器。"""
    name = (cfg.broker.name or "paper").strip().lower()

    if name == "paper":
        from .paper import PaperBroker

        return PaperBroker(
            initial_cash=cfg.broker.paper.initial_cash,
            state_path=cfg.resolve(cfg.storage.paper_state_path),
            fee_cfg=cfg.broker.paper,
        )

    if name == "qmt":
        from .qmt import QMTBroker

        return QMTBroker(
            account_id=cfg.broker.qmt.account_id,
            account_type=cfg.broker.qmt.account_type,
            mini_qmt_path=cfg.broker.qmt.mini_qmt_path,
            session_id=cfg.broker.qmt.session_id,
        )

    raise ValueError(f"未知 broker.name={name!r}，可选值：paper / qmt")
