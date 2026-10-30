"""模拟盘券商：本地撮合，用于策略验证，不接触任何真实资金。

撮合规则（刻意保守）：
- 以「参考价 × (1 ± 滑点)」成交，不做乐观假设；
- 严格执行 T+1：当日买入部分当日不可卖；
- 按「佣金（含最低 5 元）+ 过户费 + 卖出印花税」计费；
- 涨停价买入 / 跌停价卖出视为无法成交。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..config import PaperBrokerConfig
from .base import Account, BrokerAdapter, OrderResult, Position

log = logging.getLogger(__name__)


class PaperBroker(BrokerAdapter):
    name = "paper"

    def __init__(self, initial_cash: float, state_path: Path, fee_cfg: PaperBrokerConfig):
        self.state_path = state_path
        self.fee = fee_cfg
        self._initial_cash = initial_cash
        self._cash = float(initial_cash)
        self._realized_pnl = 0.0
        self._state_date = date.today().isoformat()
        # symbol -> dict(qty, available, avg_cost, buy_date, name)
        self._positions: dict[str, dict] = {}
        self._prices: dict[str, float] = {}
        self._names: dict[str, str] = {}
        self._order_seq = 0
        self._load()

    # ----------------------------- 持久化 -----------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._cash = float(data.get("cash", self._initial_cash))
            self._realized_pnl = float(data.get("realized_pnl", 0.0))
            self._state_date = data.get("date", date.today().isoformat())
            self._positions = {
                str(k): dict(v) for k, v in (data.get("positions") or {}).items()
            }
            log.info(
                "已恢复模拟账户：现金 %.2f，持仓 %d 只（状态日期 %s）",
                self._cash, len(self._positions), self._state_date,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("模拟账户状态文件损坏，已重置：%s", exc)
            self._cash = float(self._initial_cash)
            self._positions = {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": date.today().isoformat(),
            "cash": round(self._cash, 2),
            "realized_pnl": round(self._realized_pnl, 2),
            "positions": self._positions,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ----------------------------- 生命周期 -----------------------------
    def connect(self) -> None:
        self._roll_day()
        log.info(
            "[模拟盘] 就绪：初始资金 %.2f，当前现金 %.2f，持仓 %d 只",
            self._initial_cash, self._cash, len(self._positions),
        )

    def close(self) -> None:
        self._save()

    def _roll_day(self) -> None:
        """跨日结算：解锁 T+1 冻结的持仓。"""
        today = date.today().isoformat()
        if today == self._state_date:
            return
        unlocked = 0
        for pos in self._positions.values():
            if pos.get("available", 0) != pos.get("qty", 0):
                unlocked += 1
            pos["available"] = pos.get("qty", 0)
        self._state_date = today
        if unlocked:
            log.info("跨日结算：%d 只持仓解除 T+1 冻结", unlocked)
        self._save()

    # ----------------------------- 查询 -----------------------------
    def update_market(self, prices: dict[str, float], names: dict[str, str] | None = None) -> None:
        self._prices.update({k: float(v) for k, v in prices.items() if v and v > 0})
        if names:
            self._names.update(names)

    def get_account(self) -> Account:
        self._roll_day()
        market_value = 0.0
        for symbol, pos in self._positions.items():
            price = self._prices.get(symbol) or pos.get("last_price") or pos.get("avg_cost", 0.0)
            market_value += pos.get("qty", 0) * price
        return Account(
            total_asset=self._cash + market_value,
            cash=self._cash,
            market_value=market_value,
        )

    def get_positions(self) -> list[Position]:
        self._roll_day()
        out: list[Position] = []
        for symbol, pos in self._positions.items():
            price = self._prices.get(symbol) or pos.get("last_price") or pos.get("avg_cost", 0.0)
            pos["last_price"] = price
            out.append(
                Position(
                    symbol=symbol,
                    name=pos.get("name") or self._names.get(symbol, ""),
                    qty=int(pos.get("qty", 0)),
                    available=int(pos.get("available", 0)),
                    avg_cost=float(pos.get("avg_cost", 0.0)),
                    last_price=float(price),
                    buy_date=str(pos.get("buy_date", "")),
                )
            )
        return out

    # ----------------------------- 费用 -----------------------------
    def _commission(self, amount: float) -> float:
        return max(amount * self.fee.commission_rate, self.fee.min_commission)

    def calc_fee(self, amount: float, side: str) -> float:
        fee = self._commission(amount) + amount * self.fee.transfer_fee_rate
        if side.upper() == "SELL":
            fee += amount * self.fee.stamp_duty_rate
        return round(fee, 2)

    def _fill_price(self, price: float, side: str) -> float:
        slip = self.fee.slippage_bps / 10_000.0
        factor = 1 + slip if side.upper() == "BUY" else 1 - slip
        return round(price * factor, 2)

    # ----------------------------- 下单 -----------------------------
    def buy(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        self._roll_day()
        if qty <= 0:
            return OrderResult(False, message="买入数量为 0")
        if price <= 0:
            return OrderResult(False, message="价格非法")

        fill_price = self._fill_price(price, "BUY")
        amount = fill_price * qty
        fee = self.calc_fee(amount, "BUY")
        if amount + fee > self._cash + 1e-6:
            return OrderResult(
                False,
                message=f"可用资金不足：需 {amount + fee:.2f}，可用 {self._cash:.2f}",
            )

        pos = self._positions.get(symbol)
        if pos:
            new_qty = int(pos["qty"]) + qty
            pos["avg_cost"] = round(
                (float(pos["avg_cost"]) * int(pos["qty"]) + amount) / new_qty, 4
            )
            pos["qty"] = new_qty
            pos["last_price"] = fill_price
            if name:
                pos["name"] = name
        else:
            self._positions[symbol] = {
                "qty": qty,
                "available": 0,          # T+1：当日买入不可卖
                "avg_cost": round(fill_price, 4),
                "buy_date": date.today().isoformat(),
                "name": name or self._names.get(symbol, ""),
                "last_price": fill_price,
            }

        self._cash -= amount + fee
        self._save()

        self._order_seq += 1
        order_id = f"P{self._state_date.replace('-', '')}{self._order_seq:05d}"
        log.info("[模拟买入] %s %s 价格 %.2f 数量 %d 金额 %.2f 费用 %.2f",
                 symbol, name, fill_price, qty, amount, fee)
        return OrderResult(
            success=True,
            order_id=order_id,
            message="模拟成交",
            filled_price=fill_price,
            filled_qty=qty,
            fee=fee,
        )

    def sell(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        self._roll_day()
        pos = self._positions.get(symbol)
        if not pos:
            return OrderResult(False, message="无此持仓")
        if qty <= 0:
            return OrderResult(False, message="卖出数量为 0")

        available = int(pos.get("available", 0))
        if qty > available:
            return OrderResult(
                False,
                message=f"可卖数量不足（T+1）：可卖 {available}，申报 {qty}",
            )
        if price <= 0:
            return OrderResult(False, message="价格非法")

        fill_price = self._fill_price(price, "SELL")
        amount = fill_price * qty
        fee = self.calc_fee(amount, "SELL")
        cost = float(pos.get("avg_cost", 0.0)) * qty
        self._realized_pnl += amount - fee - cost

        pos["qty"] = int(pos["qty"]) - qty
        pos["available"] = available - qty
        pos["last_price"] = fill_price
        if pos["qty"] <= 0:
            self._positions.pop(symbol, None)

        self._cash += amount - fee
        self._save()

        self._order_seq += 1
        order_id = f"P{self._state_date.replace('-', '')}{self._order_seq:05d}"
        log.info("[模拟卖出] %s %s 价格 %.2f 数量 %d 金额 %.2f 费用 %.2f",
                 symbol, name, fill_price, qty, amount, fee)
        return OrderResult(
            success=True,
            order_id=order_id,
            message="模拟成交",
            filled_price=fill_price,
            filled_qty=qty,
            fee=fee,
        )
