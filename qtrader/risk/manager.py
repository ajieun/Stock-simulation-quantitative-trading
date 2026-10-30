"""风控管理器。

设计原则：AI 只提供「建议」，风控是最后一票否决权。
所有限制都是硬约束，任何一条不满足就不下单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..ai.deepseek import Decision
from ..brokers.base import Account, Position, min_order_qty, round_lot
from ..config import StrategyConfig, RiskConfig
from ..market.quotes import Quote
from ..storage import Storage

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class RiskManager:
    def __init__(self, cfg: RiskConfig, strategy_cfg: StrategyConfig, store: Storage):
        self.cfg = cfg
        self.strategy_cfg = strategy_cfg
        self.store = store

        self.halt_new_positions = False
        self.halt_all = False
        self._reasons: list[str] = []
        self._restore_state()

    # --------------------------- 状态持久化 ---------------------------
    def _restore_state(self) -> None:
        state = self.store.get_kv("risk_state") or {}
        if not state:
            return
        from datetime import date

        if state.get("date") == date.today().isoformat():
            self.halt_new_positions = bool(state.get("halt_new"))
            self._reasons = list(state.get("reasons", []))
            if self.halt_new_positions:
                log.warning("恢复当日风控暂停状态：%s", ", ".join(self._reasons))

    def _persist(self) -> None:
        from datetime import date

        self.store.set_kv(
            "risk_state",
            {
                "date": date.today().isoformat(),
                "halt_new": self.halt_new_positions,
                "reasons": self._reasons,
            },
        )

    def reset(self) -> None:
        self.halt_new_positions = False
        self.halt_all = False
        self._reasons.clear()
        self.store.set_kv("risk_state", {})
        log.warning("风控暂停状态已手动清除")

    def _halt(self, reason: str, *, all_trading: bool = False) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)
        self.halt_new_positions = True
        if all_trading:
            self.halt_all = True
        self._persist()
        log.error("风控触发：%s", reason)

    # --------------------------- 每轮观测 ---------------------------
    def observe(self, account: Account) -> None:
        """记录净值并检查组合级风险（日亏损 / 回撤）。"""
        total = account.total_asset
        if total <= 0:
            return

        # 先取日初基准再落库，避免「当日第一条记录」被本轮值污染
        day_start = self.store.day_start_equity(total) or total
        self.store.record_equity(total, account.cash, account.market_value)

        if day_start > 0:
            day_pnl = total / day_start - 1
            if day_pnl <= -self.cfg.max_daily_loss_pct:
                self._halt(
                    f"当日亏损 {day_pnl * 100:.2f}% 超过上限 {self.cfg.max_daily_loss_pct * 100:.1f}%，停止开新仓"
                )

        peak = self.store.peak_equity()
        if peak > 0:
            drawdown = total / peak - 1
            if drawdown <= -self.cfg.max_drawdown_pct:
                self._halt(
                    f"回撤 {drawdown * 100:.2f}% 超过上限 {self.cfg.max_drawdown_pct * 100:.1f}%，全部交易暂停",
                    all_trading=True,
                )

    # --------------------------- 仓位计算 ---------------------------
    def size_order(
        self,
        *,
        decision: Decision,
        account: Account,
        price: float,
        symbol: str,
        held_value: float = 0.0,
    ) -> int:
        """把 AI 给出的目标仓位换算成合法股数，同时受各项上限约束。"""
        if price <= 0 or account.total_asset <= 0:
            return 0

        target_pct = decision.position_pct
        if target_pct <= 0:
            target_pct = self.cfg.max_position_pct * 0.5
        target_pct = min(target_pct, self.cfg.max_position_pct)

        budget = account.total_asset * target_pct - held_value
        # 留 2% 缓冲给手续费与滑点
        budget = min(budget, account.cash * 0.98)
        if budget < self.cfg.min_order_amount:
            return 0

        qty = round_lot(symbol, budget / price)
        if qty > 0:
            return qty

        # 高价股（如茅台一手十几万）常出现「预算不够一手」。
        # 若最小一手仍落在单票硬上限和可用资金之内，就按一手买入，
        # 否则宁可放弃——绝不为凑整突破风控上限。
        lot = min_order_qty(symbol)
        lot_cost = lot * price
        hard_cap = account.total_asset * self.cfg.max_position_pct - held_value
        if lot_cost <= hard_cap and lot_cost <= account.cash * 0.98:
            log.info(
                "%s 按 %.0f%% 目标仓位买不起一手（需 %.0f 元），"
                "按最小一手 %d 股下单，仍在上限内",
                symbol, target_pct * 100, lot_cost, lot,
            )
            return lot
        return 0

    # --------------------------- 买入校验 ---------------------------
    def check_buy(
        self,
        *,
        symbol: str,
        name: str,
        price: float,
        qty: int,
        quote: Quote,
        decision: Decision,
        account: Account,
        positions: list[Position],
    ) -> Verdict:
        c = self.cfg

        if self.halt_all:
            return Verdict(False, "组合风控已暂停全部交易：" + "；".join(self._reasons[-2:]))
        if self.halt_new_positions:
            return Verdict(False, "今日已停止开新仓：" + "；".join(self._reasons[-2:]))

        if qty <= 0:
            return Verdict(
                False,
                "可买数量为 0：资金不足以买入最小一手，或已触及单票/总仓位上限",
            )

        if price <= 0 or not quote.valid:
            return Verdict(False, "行情无效或已停牌")

        if c.forbid_st and (quote.is_st or "ST" in name.upper()):
            return Verdict(False, "ST 股票禁止买入")

        if c.forbid_limit_up_buy and quote.at_limit_up:
            return Verdict(False, f"已触及涨停价 {quote.limit_up}，禁止追高买入")

        if decision.confidence < self.strategy_cfg.min_confidence:
            return Verdict(
                False,
                f"AI 置信度 {decision.confidence:.2f} 低于阈值 {self.strategy_cfg.min_confidence}",
            )

        amount = price * qty
        if amount < c.min_order_amount:
            return Verdict(False, f"委托金额 {amount:.0f} 低于最小下单金额 {c.min_order_amount:.0f}")

        trades_today = self.store.count_orders_today()
        if trades_today >= c.max_daily_trades:
            return Verdict(False, f"当日成交 {trades_today} 笔，已达上限 {c.max_daily_trades}")

        # 单票仓位上限
        held = next((p for p in positions if p.symbol == symbol), None)
        held_value = held.market_value if held else 0.0
        after_value = held_value + amount
        if account.total_asset > 0:
            pct = after_value / account.total_asset
            if pct > c.max_position_pct + 1e-6:
                return Verdict(
                    False,
                    f"单票仓位将达到 {pct * 100:.1f}%，超过上限 {c.max_position_pct * 100:.1f}%",
                )

            total_after = account.market_value + amount
            exposure = total_after / account.total_asset
            if exposure > c.max_total_exposure_pct + 1e-6:
                return Verdict(
                    False,
                    f"总仓位将达到 {exposure * 100:.1f}%，超过上限 {c.max_total_exposure_pct * 100:.1f}%",
                )

        if amount > account.cash:
            return Verdict(False, f"可用资金不足：需 {amount:.0f}，可用 {account.cash:.0f}")

        return Verdict(True, "通过风控")

    # --------------------------- 卖出校验 ---------------------------
    def check_sell(self, *, position: Position, price: float, qty: int, quote: Quote) -> Verdict:
        if self.halt_all:
            # 熔断时仍允许止损离场，但不允许清仓后立即反向操作
            log.warning("组合风控熔断中，允许卖出以降低风险敞口")
        if qty <= 0:
            return Verdict(False, "可卖数量为 0（可能是 T+1 限制）")
        if quote and quote.valid and self.cfg.forbid_limit_down_sell and quote.at_limit_down:
            return Verdict(False, f"已触及跌停价 {quote.limit_down}，卖单难以成交，暂缓")
        return Verdict(True, "通过风控")

    # --------------------------- 规则型离场 ---------------------------
    def hard_exit_reason(self, position: Position) -> str | None:
        """止损 / 止盈 / 持有超期，返回离场原因。"""
        pnl = position.pnl_pct
        if pnl <= -self.cfg.stop_loss_pct * 100:
            return f"触发硬止损（浮亏 {pnl:.2f}%）"
        if pnl >= self.cfg.take_profit_pct * 100:
            return f"触发止盈（浮盈 {pnl:.2f}%）"
        if self.cfg.max_holding_days > 0 and position.holding_days >= self.cfg.max_holding_days:
            return f"持有已 {position.holding_days} 个交易日，超过上限"
        return None
