"""行情数据源（东方财富公开接口）。

说明：
- 这是网页端使用的公开 JSON 接口，无需 token，但非官方承诺，字段可能调整。
  所有解析都做了防御，失败返回 None 由上层降级处理。
- 若接口失效，可安装 akshare 后在此处替换实现，上层接口保持不变。
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import requests

log = logging.getLogger(__name__)

UT = "fa5fd1943c7b386f172d6893dbfba10b"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

REALTIME_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_REALTIME_URL = "https://qt.gtimg.cn/q="


def market_prefix(symbol: str) -> str:
    """东方财富 secid 前缀：沪市=1，深市/北交所=0。"""
    s = str(symbol)
    if s.startswith(("6", "9", "5", "68")):
        return "1"
    return "0"


def secid(symbol: str) -> str:
    return f"{market_prefix(symbol)}.{symbol}"


@dataclass
class Quote:
    symbol: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0        # 手
    amount: float = 0.0        # 元
    turnover_rate: float = 0.0  # 换手率 %
    volume_ratio: float = 0.0   # 量比
    pct_change: float = 0.0
    ts: datetime = field(default_factory=datetime.now)

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper()

    @property
    def limit_pct(self) -> float:
        """涨跌停幅度。"""
        if self.is_st:
            return 0.05
        s = self.symbol
        if s.startswith(("300", "301")):      # 创业板
            return 0.20
        if s.startswith(("688", "689")):      # 科创板
            return 0.20
        if s.startswith(("8", "4", "92")):    # 北交所
            return 0.30
        return 0.10

    @property
    def limit_up(self) -> float:
        return round(self.prev_close * (1 + self.limit_pct), 2)

    @property
    def limit_down(self) -> float:
        return round(self.prev_close * (1 - self.limit_pct), 2)

    @property
    def at_limit_up(self) -> bool:
        return self.prev_close > 0 and self.price >= self.limit_up - 0.005

    @property
    def at_limit_down(self) -> bool:
        return self.prev_close > 0 and self.price <= self.limit_down + 0.005

    @property
    def valid(self) -> bool:
        return self.price > 0 and self.prev_close > 0


@dataclass
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float


def _fnum(value, default: float = 0.0) -> float:
    """接口偶发返回 '-' 或 None。"""
    try:
        if value in (None, "-", ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class MarketData:
    """行情访问入口，带内存缓存与重试。"""

    def __init__(self, timeout: int = 10, retries: int = 3, bar_ttl: int = 300):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.bar_ttl = bar_ttl
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._bar_cache: dict[str, tuple[float, list[Bar]]] = {}

    # ----------------------------- HTTP -----------------------------
    def _get_json(self, url: str, params: dict, *, quiet: bool = False) -> dict | None:
        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.retries - 1:
                    _time.sleep(0.6 * (2 ** attempt))
        if quiet:
            log.debug("行情请求失败 %s: %s", url, last_err)
        else:
            log.warning("行情请求失败 %s: %s", url, last_err)
        return None

    # ----------------------------- 实时 -----------------------------
    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        """批量获取实时快照。一次最多 ~50 只。"""
        syms = [str(s) for s in symbols if s]
        out: dict[str, Quote] = {}
        for i in range(0, len(syms), 50):
            chunk = syms[i:i + 50]
            params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f8,f10,f15,f16,f17,f18",
                "secids": ",".join(secid(s) for s in chunk),
                "ut": UT,
            }
            data = self._get_json(REALTIME_URL, params, quiet=True)
            if not data:
                continue
            diff = (data.get("data") or {}).get("diff") or []
            if isinstance(diff, dict):  # 某些返回形态是 dict
                diff = list(diff.values())
            for item in diff:
                code = str(item.get("f12") or "").strip()
                if not code:
                    continue
                q = Quote(
                    symbol=code,
                    name=str(item.get("f14") or ""),
                    price=_fnum(item.get("f2")),
                    pct_change=_fnum(item.get("f3")),
                    volume=_fnum(item.get("f5")),
                    amount=_fnum(item.get("f6")),
                    turnover_rate=_fnum(item.get("f8")),
                    volume_ratio=_fnum(item.get("f10")),
                    high=_fnum(item.get("f15")),
                    low=_fnum(item.get("f16")),
                    open=_fnum(item.get("f17")),
                    prev_close=_fnum(item.get("f18")),
                )
                # 停牌时 f2 可能为 0，用昨收兜底避免误判
                if q.price <= 0 and q.prev_close > 0:
                    q.price = q.prev_close
                out[code] = q

        missing = [s for s in syms if s not in out]
        if missing:
            log.debug("主行情源缺失 %d 只，切换腾讯源", len(missing))
            out.update(self._quotes_tencent(missing))
        return out

    def _quotes_tencent(self, symbols: list[str]) -> dict[str, Quote]:
        """备用实时行情源（腾讯）。返回 GBK 编码的分号分隔文本。"""
        if not symbols:
            return {}
        codes = []
        for s in symbols:
            if s.startswith(("8", "4", "92")):
                codes.append(f"bj{s}")
            elif s.startswith(("6", "9", "5")):
                codes.append(f"sh{s}")
            else:
                codes.append(f"sz{s}")

        try:
            resp = self.session.get(
                TENCENT_REALTIME_URL + ",".join(codes), timeout=self.timeout
            )
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            log.warning("备用行情源失败：%s", exc)
            return {}

        out: dict[str, Quote] = {}
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            _, _, body = line.partition("=")
            parts = body.strip().strip('"').split("~")
            if len(parts) < 50:
                continue
            try:
                code = parts[2].strip()
                if not code:
                    continue
                q = Quote(
                    symbol=code,
                    name=parts[1],
                    price=_fnum(parts[3]),
                    prev_close=_fnum(parts[4]),
                    open=_fnum(parts[5]),
                    volume=_fnum(parts[6]),
                    pct_change=_fnum(parts[32]),
                    high=_fnum(parts[33]),
                    low=_fnum(parts[34]),
                    amount=_fnum(parts[37]) * 10_000,   # 该字段单位为万元
                    turnover_rate=_fnum(parts[38]),
                    volume_ratio=_fnum(parts[49]),
                )
                if q.price <= 0 and q.prev_close > 0:
                    q.price = q.prev_close
                if q.valid:
                    out[code] = q
            except (IndexError, ValueError):
                continue
        return out

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes([symbol]).get(symbol)

    # ----------------------------- K 线 -----------------------------
    def daily_bars(self, symbol: str, limit: int = 120, use_cache: bool = True) -> list[Bar]:
        cached = self._bar_cache.get(symbol)
        if use_cache and cached and _time.time() - cached[0] < self.bar_ttl:
            return cached[1]

        bars = self._bars_eastmoney(symbol, limit)
        if not bars:
            log.debug("%s 东方财富 K 线失败，切换腾讯源", symbol)
            bars = self._bars_tencent(symbol, limit)
        if not bars:
            log.warning("%s 两个 K 线源均失败", symbol)
            return cached[1] if cached else []

        self._bar_cache[symbol] = (_time.time(), bars)
        return bars

    def _bars_eastmoney(self, symbol: str, limit: int) -> list[Bar]:
        params = {
            "secid": secid(symbol),
            "ut": UT,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",     # 日线
            "fqt": "1",       # 前复权
            "end": "20500101",
            "lmt": str(limit),
            "_": str(int(_time.time() * 1000)),
        }
        data = self._get_json(KLINE_URL, params, quiet=True)
        if not data:
            return []

        bars: list[Bar] = []
        for line in ((data.get("data") or {}).get("klines")) or []:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            try:
                bars.append(
                    Bar(
                        date=parts[0],
                        open=float(parts[1]),
                        close=float(parts[2]),
                        high=float(parts[3]),
                        low=float(parts[4]),
                        volume=float(parts[5]),
                        amount=float(parts[6]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return bars

    def _bars_tencent(self, symbol: str, limit: int) -> list[Bar]:
        """备用日线源（腾讯），字段顺序：日期,开,收,高,低,成交量(手)。"""
        if symbol.startswith(("8", "4", "92")):
            prefix = "bj"
        elif symbol.startswith(("6", "9", "5")):
            prefix = "sh"
        else:
            prefix = "sz"
        code = f"{prefix}{symbol}"
        params = {"param": f"{code},day,,,{limit},qfq"}
        data = self._get_json(TENCENT_KLINE_URL, params)
        if not data:
            return []

        node = ((data.get("data") or {}).get(code)) or {}
        rows = node.get("qfqday") or node.get("day") or []
        bars: list[Bar] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                bars.append(
                    Bar(
                        date=str(row[0]),
                        open=float(row[1]),
                        close=float(row[2]),
                        high=float(row[3]),
                        low=float(row[4]),
                        volume=float(row[5]),
                        amount=0.0,
                    )
                )
            except (TypeError, ValueError):
                continue
        return bars

    def bars_with_today(self, symbol: str, quote: Quote | None = None, limit: int = 120) -> list[Bar]:
        """返回含当日（用实时快照合成的）日线序列，供盘中指标计算。"""
        bars = list(self.daily_bars(symbol, limit=limit))
        q = quote or self.quote(symbol)
        if not q or not q.valid:
            return bars

        today = date.today().isoformat()
        if bars and bars[-1].date == today:
            bars.pop()

        bars.append(
            Bar(
                date=today,
                open=q.open or q.price,
                close=q.price,
                high=q.high or q.price,
                low=q.low or q.price,
                volume=q.volume,
                amount=q.amount,
            )
        )
        return bars

    def clear_cache(self) -> None:
        self._bar_cache.clear()
