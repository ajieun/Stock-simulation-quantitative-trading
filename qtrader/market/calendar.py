"""A 股交易日历与交易时段判断。

优先级：akshare（如已安装）> 内置节假日表 > 仅排除周末。
内置表是兜底方案，遇到节假日调整请更新或安装 akshare。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo

log = logging.getLogger(__name__)

# Windows 默认没有 IANA 时区库，缺失 tzdata 时退回固定 UTC+8（中国无夏令时，等价）
try:
    from zoneinfo import ZoneInfo

    CN_TZ: tzinfo = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    CN_TZ = timezone(timedelta(hours=8), name="CST")

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# 集合竞价
AUCTION_OPEN = time(9, 15)
AUCTION_CLOSE = time(9, 25)

# 兜底休市日（周末之外）。仅供离线使用，务必以交易所公告为准。
FALLBACK_HOLIDAYS: set[str] = {
    # ---- 2025 ----
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06",
    "2025-10-07", "2025-10-08",
    # ---- 2026（预估，需以公告校准）----
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

_holidays_cache: set[str] | None = None
_holidays_source: str = "fallback"


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _load_holidays() -> set[str]:
    """尝试从 akshare 拉取交易日历，失败则用内置表。结果进程内缓存。"""
    global _holidays_cache, _holidays_source
    if _holidays_cache is not None:
        return _holidays_cache

    holidays: set[str] = set(FALLBACK_HOLIDAYS)
    try:  # akshare 为可选依赖
        import akshare as ak  # type: ignore

        df = ak.tool_trade_date_hist_sina()
        trade_days = {
            str(d)[:10] for d in df["trade_date"].tolist()
        }
        if trade_days:
            # 用「非交易日 = 未出现在交易日历中」的反推方式覆盖内置表
            start = date(2024, 1, 1)
            end = date(2030, 12, 31)
            cur = start
            acc: set[str] = set()
            while cur <= end:
                key = cur.isoformat()
                if cur.weekday() < 5 and key not in trade_days:
                    acc.add(key)
                cur += timedelta(days=1)
            if acc:
                holidays = acc
                _holidays_source = "akshare"
                log.info("交易日历来源：akshare（%d 个休市日）", len(holidays))
    except Exception as exc:  # noqa: BLE001
        log.debug("akshare 交易日历不可用，使用内置表：%s", exc)
        _holidays_source = "fallback"

    _holidays_cache = holidays
    return holidays


def holiday_source() -> str:
    _load_holidays()
    return _holidays_source


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_cn().date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in _load_holidays()


def is_trading_time(dt: datetime | None = None, include_auction: bool = False) -> bool:
    """是否处于可下单的连续竞价时段。"""
    dt = dt or now_cn()
    if not is_trading_day(dt.date()):
        return False
    t = dt.timetz().replace(tzinfo=None) if dt.tzinfo else dt.time()
    if MORNING_OPEN <= t <= MORNING_CLOSE:
        return True
    if AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE:
        return True
    if include_auction and AUCTION_OPEN <= t <= AUCTION_CLOSE:
        return True
    return False


def is_trading_day_over(dt: datetime | None = None) -> bool:
    dt = dt or now_cn()
    t = dt.timetz().replace(tzinfo=None) if dt.tzinfo else dt.time()
    return is_trading_day(dt.date()) and t > AFTERNOON_CLOSE


def next_session_start(dt: datetime | None = None) -> datetime:
    """下一个可交易时刻（用于非交易时段休眠）。"""
    dt = dt or now_cn()
    d = dt.date()
    t = dt.timetz().replace(tzinfo=None) if dt.tzinfo else dt.time()

    if is_trading_day(d):
        if t < MORNING_OPEN:
            return datetime.combine(d, MORNING_OPEN, tzinfo=CN_TZ)
        if MORNING_CLOSE < t < AFTERNOON_OPEN:
            return datetime.combine(d, AFTERNOON_OPEN, tzinfo=CN_TZ)

    # 往后找最近的交易日
    probe = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(probe):
            return datetime.combine(probe, MORNING_OPEN, tzinfo=CN_TZ)
        probe += timedelta(days=1)
    return datetime.combine(d + timedelta(days=1), MORNING_OPEN, tzinfo=CN_TZ)


def seconds_until(target: datetime) -> float:
    delta = (target - now_cn()).total_seconds()
    return max(0.0, delta)
