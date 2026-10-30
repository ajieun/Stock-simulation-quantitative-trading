"""基于日线的规则回测。

刻意不使用 AI：把历史行情喂给大模型既昂贵，又极易引入前视偏差
（模型训练语料里就包含这些股票的历史走势）。这里只验证「规则层」是否有效，
AI 的作用需要在实盘/模拟盘日志里单独评估。

交易假设：
- 当日收盘产生信号，次日开盘成交（避免用当日收盘价决策又用同一价格成交）；
- 单标的满仓进出，按总资金百分比分配；
- 计费：佣金万分之 2.5（最低 5 元）+ 卖出印花税千 1 + 过户费十万分之一；
- T+1：买入当日不可卖，本回测天然满足（次日才可能卖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AppConfig
from .market.indicators import compute_snapshot
from .market.quotes import Bar, Quote
from .strategy import technical_score, trend_exit_reason


@dataclass
class Trade:
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    reason: str = ""


@dataclass
class BacktestReport:
    symbol: str = ""
    name: str = ""
    start_date: str = ""
    end_date: str = ""
    initial_cash: float = 0.0
    final_cash: float = 0.0
    total_return_pct: float = 0.0
    benchmark_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate_pct: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def _fee(amount: float, side: str) -> float:
    fee = max(amount * 0.00025, 5.0) + amount * 0.00001
    if side == "SELL":
        fee += amount * 0.001
    return fee


def backtest_symbol(symbol: str, bars: list[Bar], cfg: AppConfig) -> BacktestReport:
    initial_cash = 100_000.0
    cash = initial_cash
    qty = 0
    entry_price = 0.0
    entry_date = ""
    trades: list[Trade] = []
    equity: list[float] = []

    min_score = cfg.strategy.min_technical_score
    stop_loss = cfg.risk.stop_loss_pct
    take_profit = cfg.risk.take_profit_pct

    n = len(bars)
    start = 30
    pending_action = ""

    for i in range(start, n - 1):
        window = bars[: i + 1]
        close_i = window[-1].close
        next_open = bars[i + 1].open

        # ---- 持仓中：判断是否离场 ----
        if qty > 0:
            pnl = close_i / entry_price - 1 if entry_price > 0 else 0.0
            exit_reason = ""
            if pnl <= -stop_loss:
                exit_reason = "止损"
            elif pnl >= take_profit:
                exit_reason = "止盈"
            else:
                # 与实盘共用同一套离场规则，避免回测和实盘行为不一致
                snap = _snapshot(window)
                if snap and trend_exit_reason(snap):
                    exit_reason = "趋势破坏"

            if exit_reason:
                amount = next_open * qty
                fee = _fee(amount, "SELL")
                cash += amount - fee
                pnl_pct = (next_open / entry_price - 1) * 100
                trades.append(
                    Trade(
                        entry_date=entry_date,
                        entry_price=entry_price,
                        exit_date=bars[i + 1].date,
                        exit_price=next_open,
                        pnl_pct=round(pnl_pct, 2),
                        reason=exit_reason,
                    )
                )
                qty = 0
                entry_price = 0.0
                entry_date = ""
                equity.append(cash)
                continue

        # ---- 空仓：判断是否进场 ----
        if qty == 0 and not pending_action:
            snap = _snapshot(window)
            if snap:
                quote = Quote(
                    symbol=symbol,
                    name="",
                    price=close_i,
                    prev_close=window[-2].close if len(window) > 1 else close_i,
                    volume=window[-1].volume,
                    volume_ratio=snap.volume_ratio or 0.0,
                )
                tech = technical_score(snap, quote)
                if tech.eligible and tech.score >= min_score:
                    budget = cash * 0.98
                    lots = int(budget / next_open / 100)
                    if lots > 0:
                        buy_qty = lots * 100
                        amount = next_open * buy_qty
                        fee = _fee(amount, "BUY")
                        if amount + fee <= cash:
                            cash -= amount + fee
                            qty = buy_qty
                            entry_price = next_open
                            entry_date = bars[i + 1].date

        equity.append(cash + qty * close_i)

    # 强制平仓
    if qty > 0:
        last = bars[-1]
        amount = last.close * qty
        cash += amount - _fee(amount, "SELL")
        trades.append(
            Trade(
                entry_date=entry_date,
                entry_price=entry_price,
                exit_date=last.date,
                exit_price=last.close,
                pnl_pct=round((last.close / entry_price - 1) * 100, 2),
                reason="回测结束",
            )
        )
        qty = 0
        equity.append(cash)

    total_return = (cash / initial_cash - 1) * 100
    benchmark = (bars[-1].close / bars[start].close - 1) * 100 if bars[start].close else 0.0

    peak = -1e18
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1)

    wins = sum(1 for t in trades if t.pnl_pct > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    return BacktestReport(
        symbol=symbol,
        start_date=bars[start].date,
        end_date=bars[-1].date,
        initial_cash=initial_cash,
        final_cash=round(cash, 2),
        total_return_pct=round(total_return, 2),
        benchmark_return_pct=round(benchmark, 2),
        max_drawdown_pct=round(max_dd * 100, 2),
        win_rate_pct=round(win_rate, 1),
        trades=trades,
        equity_curve=equity,
    )


def _snapshot(bars: list[Bar]):
    return compute_snapshot(
        "",
        [b.close for b in bars],
        [b.high for b in bars],
        [b.low for b in bars],
        [b.volume for b in bars],
    )


def format_report(report: BacktestReport) -> str:
    """返回纯文本报告，避免 rich 表格在窄终端下错行。"""
    alpha = report.total_return_pct - report.benchmark_return_pct
    lines = [
        f"═══ 回测 {report.symbol}  {report.start_date} ~ {report.end_date} ═══",
        f"  策略收益   : {report.total_return_pct:+.2f}%",
        f"  基准(持有) : {report.benchmark_return_pct:+.2f}%",
        f"  超额收益   : {alpha:+.2f}%",
        f"  最大回撤   : {report.max_drawdown_pct:.2f}%",
        f"  交易次数   : {len(report.trades)}，胜率 {report.win_rate_pct:.1f}%",
        f"  期末资金   : {report.final_cash:,.2f}（初始 {report.initial_cash:,.2f}）",
    ]
    if report.trades:
        lines.append("  最近交易：")
        for t in report.trades[-5:]:
            lines.append(
                f"    {t.entry_date} 买 {t.entry_price:.2f} -> "
                f"{t.exit_date} 卖 {t.exit_price:.2f}  {t.pnl_pct:+.2f}%  [{t.reason}]"
            )
    return "\n".join(lines)
