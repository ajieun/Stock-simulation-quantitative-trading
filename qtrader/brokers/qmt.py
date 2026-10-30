"""迅投 miniQMT 实盘适配器。

前置条件（缺一不可）：
1. 券商开通「QMT / 极简模式」权限，并安装对应交易端（国金、华泰、国盛、东财等）。
2. 启动交易端并登录资金账号（本程序不会、也不应该代替你登录）。
3. 把 <安装目录>/bin.x64/Lib/site-packages 加入 PYTHONPATH，使 `import xtquant` 可用。
4. config.yaml 中 broker.qmt.mini_qmt_path 指向 userdata_mini 目录。

重要：
- 本适配器遵循 xtquant 官方公开接口编写，但每家券商客户端版本存在差异，
  **首次接入必须使用 QMT 内置的模拟账号跑通全流程**，再切换到真实账号。
- 实盘下单为限价单，程序不保证成交；未成交需人工或自行实现撤单逻辑。
- 按《证券市场程序化交易管理规定》，程序化交易投资者需通过券商完成报告手续，
  请事先与客户经理确认合规要求。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import Account, BrokerAdapter, OrderResult, Position

log = logging.getLogger(__name__)


def to_qmt_code(symbol: str) -> str:
    """600519 -> 600519.SH / 000001 -> 000001.SZ / 8xxxxx -> 8xxxxx.BJ"""
    s = str(symbol).strip()
    if "." in s:
        return s.upper()
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    if s.startswith(("8", "4", "92")):
        return f"{s}.BJ"
    return f"{s}.SZ"


def from_qmt_code(code: str) -> str:
    return str(code).split(".")[0]


class QMTBroker(BrokerAdapter):
    name = "qmt"

    def __init__(self, account_id: str, account_type: str, mini_qmt_path: str, session_id: int):
        self.account_id = account_id
        self.account_type = account_type or "STOCK"
        self.mini_qmt_path = mini_qmt_path
        self.session_id = int(session_id)
        self._trader: Any = None
        self._account: Any = None
        self._xtconstant: Any = None
        self._connected = False
        self._order_errors: list[str] = []

    # ----------------------------- 连接 -----------------------------
    def connect(self) -> None:
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
            from xtquant.xttype import StockAccount
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "无法导入 xtquant。请确认已安装券商 QMT 客户端，并把 "
                "<安装目录>/bin.x64/Lib/site-packages 加入 PYTHONPATH。"
            ) from exc

        if not self.mini_qmt_path:
            raise RuntimeError("broker.qmt.mini_qmt_path 未配置")

        broker_self = self

        class _Callback(XtQuantTraderCallback):  # type: ignore[misc]
            def on_disconnected(self):  # noqa: D102
                log.error("QMT 连接断开，实盘下单已停止")
                broker_self._connected = False

            def on_order_error(self, order_error):  # noqa: D102
                msg = f"下单失败 {getattr(order_error, 'order_id', '?')}: {getattr(order_error, 'error_msg', '')}"
                log.error(msg)
                broker_self._order_errors.append(msg)

            def on_cancel_error(self, cancel_error):  # noqa: D102
                log.error("撤单失败：%s", getattr(cancel_error, "error_msg", ""))

            def on_order_stock_async_response(self, response):  # noqa: D102
                log.debug("异步下单回报：%s", response)

            def on_stock_order(self, order):  # noqa: D102
                log.info(
                    "委托回报 %s 状态=%s 成交=%s",
                    getattr(order, "stock_code", ""),
                    getattr(order, "order_status", ""),
                    getattr(order, "traded_volume", ""),
                )

            def on_stock_trade(self, trade):  # noqa: D102
                log.info(
                    "成交回报 %s 价=%s 量=%s",
                    getattr(trade, "stock_code", ""),
                    getattr(trade, "traded_price", ""),
                    getattr(trade, "traded_volume", ""),
                )

        self._xtconstant = xtconstant
        self._trader = XtQuantTrader(self.mini_qmt_path, self.session_id)
        self._trader.register_callback(_Callback())
        self._trader.start()

        result = self._trader.connect()
        if result != 0:
            raise RuntimeError(
                f"QMT 连接失败（返回码 {result}）。请确认交易端已启动并登录，"
                f"且 mini_qmt_path 指向 userdata_mini。"
            )

        self._account = StockAccount(self.account_id, self.account_type)
        self._trader.subscribe(self._account)
        self._connected = True
        log.warning(
            "QMT 实盘连接成功：账号 %s（%s）。所有下单将影响真实资金。",
            self.account_id, self.account_type,
        )

    def _ensure(self) -> None:
        if not self._connected or self._trader is None:
            raise RuntimeError("QMT 未连接，请先调用 connect()")

    def close(self) -> None:
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False

    # ----------------------------- 查询 -----------------------------
    def _to_symbol(self, code: str) -> str:
        return str(code)

    def get_account(self) -> Account:
        self._ensure()
        asset = self._trader.query_stock_asset(self._account)
        if asset is None:
            raise RuntimeError("查询资金失败，可能是 QMT 连接已断开")
        cash = float(getattr(asset, "cash", 0.0))
        total = float(getattr(asset, "total_asset", 0.0))
        market_value = float(getattr(asset, "market_value", 0.0))
        frozen = float(getattr(asset, "frozen_cash", 0.0))
        return Account(
            total_asset=total,
            cash=cash,
            frozen_cash=frozen,
            market_value=market_value,
        )

    def get_positions(self) -> list[Position]:
        self._ensure()
        raw = self._trader.query_stock_positions(self._account) or []
        out: list[Position] = []
        for p in raw:
            qty = int(getattr(p, "volume", 0))
            if qty <= 0:
                continue
            code = str(getattr(p, "stock_code", ""))
            out.append(
                Position(
                    symbol=from_qmt_code(code),
                    name=str(getattr(p, "stock_name", "") or ""),
                    qty=qty,
                    available=int(getattr(p, "can_use_volume", 0)),
                    avg_cost=float(getattr(p, "avg_price", 0.0) or getattr(p, "open_price", 0.0)),
                    last_price=float(getattr(p, "market_value", 0.0)) / qty if qty else 0.0,
                )
            )
        return out

    # ----------------------------- 下单 -----------------------------
    def _place(self, symbol: str, side: str, price: float, qty: int, name: str) -> OrderResult:
        self._ensure()
        if qty <= 0:
            return OrderResult(False, message="数量为 0")
        if price <= 0:
            return OrderResult(False, message="价格非法")

        xt = self._xtconstant
        order_type = xt.STOCK_BUY if side == "BUY" else xt.STOCK_SELL
        code = to_qmt_code(symbol)

        try:
            order_id = self._trader.order_stock(
                self._account,
                code,
                order_type,
                int(qty),
                xt.FIX_PRICE,
                float(price),
                "ai_qtrader",
                f"{side}-{name or symbol}"[:24],
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("QMT 下单异常")
            return OrderResult(False, message=f"下单异常：{exc}")

        if order_id is None or int(order_id) < 0:
            msg = f"QMT 拒单（返回 {order_id}）"
            if self._order_errors:
                msg += f"，最近错误：{self._order_errors[-1]}"
            return OrderResult(False, message=msg)

        log.warning(
            "[实盘%s] %s %s 价格 %.2f 数量 %d，委托号 %s",
            "买入" if side == "BUY" else "卖出", symbol, name, price, qty, order_id,
        )
        return OrderResult(
            success=True,
            order_id=str(order_id),
            message="委托已提交（不保证成交，需确认委托状态）",
            filled_price=price,
            filled_qty=0,   # 实际成交量以成交回报为准
        )

    def buy(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        return self._place(symbol, "BUY", price, qty, name)

    def sell(self, symbol: str, price: float, qty: int, name: str = "") -> OrderResult:
        return self._place(symbol, "SELL", price, qty, name)

    # ----------------------------- 辅助 -----------------------------
    def cancel_all(self) -> None:
        """收盘前撤销全部未成交委托。"""
        self._ensure()
        try:
            orders = self._trader.query_stock_orders(self._account) or []
            for od in orders:
                status = getattr(od, "order_status", None)
                # 50/55 为已成交/已撤，其余视为活动委托
                if status in (50, 53, 55):
                    continue
                self._trader.cancel_order_stock(self._account, getattr(od, "order_id", 0))
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            log.exception("批量撤单失败")
