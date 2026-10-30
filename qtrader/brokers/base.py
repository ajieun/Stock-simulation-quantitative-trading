"""券商适配器抽象接口 + A 股交易规则工具。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


def board_of(symbol: str) -> str:
    s = str(symbol)
    if s.startswith(("688", "689")):
        return "STAR"      # 科创板
    if s.startswith(("300", "301")):
        return "GEM"       # 创业板
    if s.startswith(("8", "4", "92")):
        return "BSE"       # 北交所
    return "MAIN"          # 主板


def min_order_qty(symbol: str) -> int:
    """最低申报数量。"""
    return 200 if board_of(symbol) == "STAR" else 100


def round_lot(symbol: str, qty: float) -> int:
    """向下取整到合法申报数量。"""
    qty = int(qty)
    if qty <= 0:
        return 0
    board = board_of(symbol)
    if board == "STAR":
        # 科创板：200 股起，之后可按 1 股递增
        return qty if qty >= 200 else 0
    return (qty // 100) * 100


def can_sell_qty(symbol: str, qty: int) -> int:
    """卖出允许零股（清仓时可卖不足一手的剩余）。"""
    return max(0, int(qty))


@dataclass
class Account:
    total_asset: float = 0.0
    cash: float = 0.0            # 可用资金
    frozen_cash: float = 0.0
    market_value: float = 0.0

    @property
    def exposure(self) -> float:
        return (self.market_value / self.total_asset) if self.total_asset > 0 else 0.0


@dataclass
class Position:
    symbol: str
    name: str = ""
    qty: int = 0                # 总持仓
    available: int = 0          # 可卖（T+1 限制后）
    avg_cost: float = 0.0
    last_price: float = 0.0
    buy_date: str = ""          # 建仓日期 YYYY-MM-DD

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def pnl_pct(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return (self.last_price / self.avg_cost - 1) * 100

    @property
    def holding_days(self) -> int:
        if not self.buy_date:
            return 0
        try:
            start = date.fromisoformat(self.buy_date)
        except ValueError:
            return 0
        return (date.today() - start).days


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    message: str = ""
    filled_price: float = 0.0
    filled_qty: int = 0
    fee: float = 0.0
    raw: dict = field(default_factory=dict)


class BrokerAdapter(ABC):
    """所有券商实现必须满足的接口。"""

    name: str = "base"

    @abstractmethod
    def connect(self) -> None:
        """建立连接 / 加载账户状态。"""

    @abstractmethod
    def get_account(self) -> Account:
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def buy(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        """限价买入。price 为期望成交价。"""

    @abstractmethod
    def sell(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        """限价卖出。"""

    def update_market(self, prices: dict[str, float], names: dict[str, str] | None = None) -> None:
        """用最新行情更新持仓估值（模拟盘用于计算市值，实盘账户由券商提供）。"""

    def close(self) -> None:
        """释放资源。"""
