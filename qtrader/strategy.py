"""规则策略：技术面打分 + 上下文构造。

分两层：
1. 规则层先做便宜的技术面筛选，只有得分够高的标的才送去问 AI。
   这样既省 token，又保证 AI 不会在明显的坏形态上乱开仓。
2. AI 层在候选池里做二次判断，给出仓位与止损止盈。
"""

from __future__ import annotations

from dataclasses import dataclass

from .brokers.base import Account, Position
from .market.indicators import Snapshot
from .market.quotes import Quote

# 各因子权重，合计 1.0
WEIGHTS = {
    "price_above_ma20": 0.20,
    "ma_alignment": 0.15,
    "macd_positive": 0.15,
    "rsi_healthy": 0.10,
    "volume_expansion": 0.15,
    "near_breakout": 0.10,
    "not_overextended": 0.10,
    "volatility_ok": 0.05,
}


@dataclass
class TechSignal:
    score: float
    reasons: list[str]
    blockers: list[str]

    @property
    def eligible(self) -> bool:
        return not self.blockers


def technical_score(snapshot: Snapshot, quote: Quote) -> TechSignal:
    """0-1 打分。blockers 非空时直接淘汰，不看分数。"""
    s = snapshot
    reasons: list[str] = []
    blockers: list[str] = []
    score = 0.0

    ma5, ma10, ma20 = s.ma.get(5), s.ma.get(10), s.ma.get(20)

    # ---------- 趋势 ----------
    if ma20 and s.close > ma20:
        score += WEIGHTS["price_above_ma20"]
        reasons.append(f"收盘价站上 MA20（{ma20:.2f}）")
    elif ma20:
        blockers.append(f"收盘价 {s.close:.2f} 跌破 MA20（{ma20:.2f}），处于弱势")

    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        score += WEIGHTS["ma_alignment"]
        reasons.append("均线多头排列 MA5>MA10>MA20")
    elif ma5 and ma10 and ma5 < ma10:
        blockers.append("MA5 下穿 MA10，短期趋势转弱")

    # ---------- 动量 ----------
    if s.macd_hist is not None and s.macd_hist > 0:
        if s.macd_hist_prev is not None and s.macd_hist > s.macd_hist_prev:
            score += WEIGHTS["macd_positive"]
            reasons.append("MACD 红柱放大")
        else:
            score += WEIGHTS["macd_positive"] * 0.5
            reasons.append("MACD 处于多头区间但动能减弱")
    elif s.macd_hist is not None and s.macd_hist < 0 and s.macd_hist_prev is not None:
        if s.macd_hist < s.macd_hist_prev:
            blockers.append("MACD 绿柱放大，下跌动能增强")

    if s.rsi is not None:
        if 45 <= s.rsi <= 72:
            score += WEIGHTS["rsi_healthy"]
            reasons.append(f"RSI {s.rsi:.1f} 处于健康区间")
        elif s.rsi > 80:
            blockers.append(f"RSI {s.rsi:.1f} 严重超买，追高风险大")
        elif s.rsi < 30:
            reasons.append(f"RSI {s.rsi:.1f} 超卖，可能存在反弹（但趋势不明）")

    # ---------- 量价配合（放量必须伴随上涨才算利好） ----------
    vr = s.volume_ratio if s.volume_ratio is not None else quote.volume_ratio
    above_ma20 = bool(ma20) and s.close > (ma20 or 0)
    if vr is not None:
        if vr >= 1.2 and above_ma20:
            score += WEIGHTS["volume_expansion"]
            reasons.append(f"量比 {vr:.2f}，放量上行")
        elif vr >= 2.0 and not above_ma20:
            blockers.append(f"量比 {vr:.2f} 且跌破 MA20，放量下跌")
        elif vr < 0.6:
            blockers.append(f"量比 {vr:.2f}，成交极度萎缩，缺乏资金关注")

    # ---------- 位置 ----------
    if s.high_20 and s.close >= s.high_20 * 0.995:
        score += WEIGHTS["near_breakout"]
        reasons.append(f"接近 20 日新高（{s.high_20:.2f}）")

    if s.ret_5d is not None:
        if s.ret_5d <= 12:
            score += WEIGHTS["not_overextended"]
        else:
            blockers.append(f"5 日涨幅 {s.ret_5d:.1f}%，短线过热")

    # ---------- 波动率 ----------
    if s.atr_pct is not None and 1.0 <= s.atr_pct <= 6.0:
        score += WEIGHTS["volatility_ok"]
    elif s.atr_pct is not None and s.atr_pct > 8.0:
        blockers.append(f"ATR 占比 {s.atr_pct:.1f}%，波动过大")

    return TechSignal(score=round(min(score, 1.0), 3), reasons=reasons, blockers=blockers)


def trend_exit_reason(snapshot: Snapshot, breach_buffer: float = 0.005) -> str | None:
    """趋势破坏型离场（软止损）。

    breach_buffer 是跌破 MA20 的确认缓冲：价格只在均线附近来回穿越时不算破位，
    否则会「买入次日就卖出」反复打脸，手续费吃光收益。
    """
    ma5, ma10, ma20 = snapshot.ma.get(5), snapshot.ma.get(10), snapshot.ma.get(20)
    if not ma20:
        return None

    below = snapshot.close < ma20 * (1 - breach_buffer)

    if below and ma5 and ma10 and ma5 < ma10:
        return (
            f"趋势破坏：收盘价 {snapshot.close:.2f} 跌破 MA20（{ma20:.2f}）"
            f"{breach_buffer * 100:.1f}% 以上且 MA5<MA10"
        )

    if below and snapshot.macd_hist is not None and snapshot.macd_hist < 0:
        if snapshot.macd_hist_prev is not None and snapshot.macd_hist < snapshot.macd_hist_prev:
            if snapshot.rsi is not None and snapshot.rsi < 40:
                return "指标走弱：跌破 MA20、MACD 绿柱放大且 RSI 低于 40"
    return None


def build_ai_context(
    *,
    symbol: str,
    name: str,
    quote: Quote,
    snapshot: Snapshot,
    tech: TechSignal,
    account: Account,
    position: Position | None,
    risk_cfg,
    scenario: str = "entry",
) -> dict:
    """构造给 AI 的结构化上下文，字段命名尽量自解释。"""
    quote_info = {
        "symbol": symbol,
        "name": name,
        "price": round(quote.price, 2),
        "prev_close": round(quote.prev_close, 2),
        "open": round(quote.open, 2),
        "high": round(quote.high, 2),
        "low": round(quote.low, 2),
        "pct_change": round(quote.pct_change, 2),
        "volume_ratio": round(quote.volume_ratio, 2),
        "turnover_rate_pct": round(quote.turnover_rate, 2),
        "limit_up_price": quote.limit_up,
        "limit_down_price": quote.limit_down,
        "is_st": quote.is_st,
        "at_limit_up": quote.at_limit_up,
    }

    account_info = {
        "total_asset": round(account.total_asset, 2),
        "available_cash": round(account.cash, 2),
        "market_value": round(account.market_value, 2),
        "current_exposure_pct": round(account.exposure * 100, 2),
    }

    position_info = None
    if position:
        position_info = {
            "qty": position.qty,
            "available_to_sell": position.available,
            "avg_cost": round(position.avg_cost, 2),
            "pnl_pct": round(position.pnl_pct, 2),
            "holding_days": position.holding_days,
        }

    constraints = {
        "max_single_position_pct": risk_cfg.max_position_pct * 100,
        "max_total_exposure_pct": risk_cfg.max_total_exposure_pct * 100,
        "hard_stop_loss_pct": risk_cfg.stop_loss_pct * 100,
        "hard_take_profit_pct": risk_cfg.take_profit_pct * 100,
        "t_plus_1": True,
        "note": "当日买入不可卖出；A股最小交易单位 100 股（科创板 200 股）",
    }

    return {
        "scenario": scenario,
        "symbol": symbol,
        "name": name,
        "quote": quote_info,
        "technical": snapshot.to_dict(),
        "technical_score": tech.score,
        "technical_reasons": tech.reasons,
        "account": account_info,
        "position": position_info,
        "constraints": constraints,
        "instruction": (
            "请判断该标的是否值得买入（scenario=entry）或是否应该卖出（scenario=exit）。"
            "若买入，position_pct 不要超过 max_single_position_pct。"
        ),
    }
