"""纯 Python 技术指标（无 numpy / pandas 依赖）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(None if i + 1 < period else total / period)
    return out


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder 平滑 RSI。"""
    n = len(values)
    if n < period + 1:
        return [None] * n

    out: list[float | None] = [None] * n
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)

    for i in range(period + 1, n):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    """RS 公式。完全无波动时返回中性值 50（否则会被误判为极度超买/超卖）。"""
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    ef, es = ema(values, fast), ema(values, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, hist


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> list[float | None]:
    n = len(closes)
    if n == 0:
        return []
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )

    out: list[float | None] = [None] * n
    if n < period:
        return out
    first = sum(trs[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return var ** 0.5


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i, m in enumerate(mid):
        if m is None:
            upper.append(None)
            lower.append(None)
            continue
        sd = stdev(values[i - period + 1: i + 1])
        upper.append(m + mult * sd)
        lower.append(m - mult * sd)
    return mid, upper, lower


def pct_change(values: Sequence[float], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    prev = values[-1 - lookback]
    if prev <= 0:
        return None
    return (values[-1] / prev - 1) * 100


@dataclass
class Snapshot:
    """单只股票某一时刻的完整技术快照，用于喂给 AI 与规则引擎。"""

    symbol: str
    close: float
    ma: dict[int, float | None]
    rsi: float | None
    macd_dif: float | None
    macd_dea: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    atr: float | None
    atr_pct: float | None
    boll_mid: float | None
    boll_upper: float | None
    boll_lower: float | None
    boll_pos: float | None          # 0=下轨 1=上轨
    volume_ratio: float | None      # 当日量 / 前 N 日均量
    high_20: float | None
    low_20: float | None
    ret_5d: float | None
    ret_20d: float | None
    volatility_20d: float | None    # 年化波动率 %
    drawdown_60d: float | None      # 60 日最大回撤 %

    def to_dict(self) -> dict:
        return {
            "close": round(self.close, 2),
            "ma": {k: (round(v, 2) if v else None) for k, v in self.ma.items()},
            "rsi14": _r(self.rsi, 2),
            "macd": {
                "dif": _r(self.macd_dif, 3),
                "dea": _r(self.macd_dea, 3),
                "hist": _r(self.macd_hist, 3),
                "hist_prev": _r(self.macd_hist_prev, 3),
            },
            "atr": _r(self.atr, 3),
            "atr_pct": _r(self.atr_pct, 2),
            "boll": {
                "mid": _r(self.boll_mid, 2),
                "upper": _r(self.boll_upper, 2),
                "lower": _r(self.boll_lower, 2),
                "pos": _r(self.boll_pos, 3),
            },
            "volume_ratio": _r(self.volume_ratio, 2),
            "high_20": _r(self.high_20, 2),
            "low_20": _r(self.low_20, 2),
            "ret_5d_pct": _r(self.ret_5d, 2),
            "ret_20d_pct": _r(self.ret_20d, 2),
            "volatility_20d_pct": _r(self.volatility_20d, 2),
            "max_drawdown_60d_pct": _r(self.drawdown_60d, 2),
        }


def _r(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def compute_snapshot(
    symbol: str,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    ma_periods: Sequence[int] = (5, 10, 20, 60),
    rsi_period: int = 14,
    macd_params: tuple[int, int, int] = (12, 26, 9),
    atr_period: int = 14,
    boll_period: int = 20,
    boll_std: float = 2.0,
    volume_window: int = 5,
) -> Snapshot | None:
    if len(closes) < 30:
        return None

    close = float(closes[-1])
    ma_series = {p: sma(closes, p) for p in ma_periods}
    ma_last = {p: (s[-1] if s and s[-1] is not None else None) for p, s in ma_series.items()}

    rsi_series = rsi(closes, rsi_period)
    dif, dea, hist = macd(closes, *macd_params)
    atr_series = atr(highs, lows, closes, atr_period)
    b_mid, b_up, b_low = bollinger(closes, boll_period, boll_std)

    atr_val = atr_series[-1]
    boll_pos = None
    if b_up[-1] is not None and b_low[-1] is not None and b_up[-1] > b_low[-1]:
        boll_pos = (close - b_low[-1]) / (b_up[-1] - b_low[-1])

    vol_ratio = None
    if len(volumes) > volume_window:
        base = volumes[-1 - volume_window:-1]
        avg = sum(base) / len(base) if base else 0.0
        if avg > 0:
            vol_ratio = volumes[-1] / avg

    high_20 = max(highs[-20:]) if len(highs) >= 20 else None
    low_20 = min(lows[-20:]) if len(lows) >= 20 else None

    returns_20 = [
        closes[i] / closes[i - 1] - 1
        for i in range(max(1, len(closes) - 20), len(closes))
        if closes[i - 1] > 0
    ]
    volatility = stdev(returns_20) * (252 ** 0.5) * 100 if len(returns_20) > 2 else None

    window60 = closes[-60:]
    drawdown = None
    if window60:
        peak = window60[0]
        worst = 0.0
        for c in window60:
            peak = max(peak, c)
            if peak > 0:
                worst = min(worst, c / peak - 1)
        drawdown = worst * 100

    return Snapshot(
        symbol=symbol,
        close=close,
        ma=ma_last,
        rsi=rsi_series[-1],
        macd_dif=dif[-1],
        macd_dea=dea[-1],
        macd_hist=hist[-1],
        macd_hist_prev=hist[-2] if len(hist) > 1 else None,
        atr=atr_val,
        atr_pct=(atr_val / close * 100) if (atr_val and close > 0) else None,
        boll_mid=b_mid[-1],
        boll_upper=b_up[-1],
        boll_lower=b_low[-1],
        boll_pos=boll_pos,
        volume_ratio=vol_ratio,
        high_20=high_20,
        low_20=low_20,
        ret_5d=pct_change(closes, 5),
        ret_20d=pct_change(closes, 20),
        volatility_20d=volatility,
        drawdown_60d=drawdown,
    )
