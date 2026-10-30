"""交易引擎：盘中主循环。

每轮流程：
1. 拉取自选池 + 持仓的实时行情
2. 更新账户估值，交给风控记录净值 / 检查日亏损与回撤
3. 先处理离场（止损止盈、趋势破坏、AI 离场建议）
4. 再处理进场（技术面筛选 -> AI 决策 -> 风控校验 -> 下单）
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from rich.console import Console

from . import strategy
from .ai.deepseek import AIError, BudgetExceeded, Decision, DeepSeekClient
from .brokers.base import Account, BrokerAdapter, Position
from .config import AppConfig
from .logging_setup import console as _console
from .market import calendar
from .market.indicators import Snapshot, compute_snapshot
from .market.quotes import MarketData, Quote
from .risk.manager import RiskManager
from .storage import Storage

log = logging.getLogger(__name__)
console: Console = _console


class TradingEngine:
    def __init__(
        self,
        cfg: AppConfig,
        store: Storage,
        market: MarketData,
        ai: DeepSeekClient,
        broker: BrokerAdapter,
        risk: RiskManager,
        stop_event: threading.Event | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.ai = ai
        self.broker = broker
        self.risk = risk

        # 由网页控制台注入，用于随时叫停
        self.stop_event = stop_event or threading.Event()
        self._ai_disabled_reason: str = ""
        self._cycle_count = 0

    # =========================== 生命周期 ===========================
    def connect(self) -> None:
        self.broker.connect()

        mode_text = "实盘 LIVE" if self.cfg.is_live else "模拟盘 PAPER"
        console.print()
        console.rule(f"[bold]{mode_text} · {self.broker.name}[/bold]")
        console.print(f"  自选池     : {len(self.cfg.universe)} 只  {', '.join(self.cfg.universe[:8])}"
                      + (" ..." if len(self.cfg.universe) > 8 else ""))
        console.print(f"  AI 模型    : {self.cfg.ai.model}  置信度阈值 {self.cfg.strategy.min_confidence}")
        console.print(f"  技术面阈值 : {self.cfg.strategy.min_technical_score}")
        console.print(f"  单票上限   : {self.cfg.risk.max_position_pct*100:.0f}%   总仓位上限 "
                      f"{self.cfg.risk.max_total_exposure_pct*100:.0f}%")
        console.print(f"  止损/止盈  : -{self.cfg.risk.stop_loss_pct*100:.0f}% / +{self.cfg.risk.take_profit_pct*100:.0f}%")
        console.print(f"  交易日历   : {calendar.holiday_source()}")

        if self.cfg.is_live:
            console.print(
                "\n[bold red]警告：当前为实盘模式，所有成交将使用真实资金。"
                "请先确认已用模拟账号完整验证过流程。[/bold red]\n"
            )
        else:
            console.print("\n[green]当前为模拟盘，不涉及真实资金。[/green]\n")

    def shutdown(self) -> None:
        try:
            self.broker.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭券商连接失败")
        log.info("引擎已停止")

    # =========================== 主循环 ===========================
    def run(self, once: bool = False) -> None:
        self.connect()
        try:
            while not self.stop_event.is_set():
                now = calendar.now_cn()

                if not calendar.is_trading_day(now.date()):
                    nxt = calendar.next_session_start(now)
                    log.info("今日休市（%s），下一个交易时段：%s",
                             calendar.holiday_source(), nxt.strftime("%Y-%m-%d %H:%M"))
                    if once:
                        return
                    self._sleep_until(nxt)
                    continue

                if not calendar.is_trading_time(now):
                    nxt = calendar.next_session_start(now)
                    if calendar.is_trading_day_over(now):
                        self._daily_summary()
                        log.info("已收盘，等待下一个交易时段：%s", nxt.strftime("%Y-%m-%d %H:%M"))
                    else:
                        log.info("非连续竞价时段，等待：%s", nxt.strftime("%H:%M"))
                    if once:
                        return
                    self._sleep_until(nxt)
                    continue

                self.run_cycle(now)
                if once:
                    return
                self.stop_event.wait(timeout=max(5, self.cfg.strategy.poll_interval_seconds))

        except KeyboardInterrupt:
            console.print("\n[yellow]收到中断信号，正在退出[/yellow]")
        finally:
            self.shutdown()

    def _sleep_until(self, target: datetime) -> None:
        """分段休眠，便于 Ctrl+C 与网页「停止」及时响应。"""
        while not self.stop_event.is_set():
            remain = calendar.seconds_until(target)
            if remain <= 1:
                return
            self.stop_event.wait(timeout=min(60.0, remain))

    # =========================== 单轮 ===========================
    def run_cycle(self, now: datetime) -> None:
        self._cycle_count += 1

        held_symbols = [p.symbol for p in self.broker.get_positions()]
        symbols = sorted(set(self.cfg.universe) | set(held_symbols))
        quotes = self.market.quotes(symbols)
        if not quotes:
            log.warning("本轮未获取到任何行情，跳过")
            return

        self.broker.update_market(
            {s: q.price for s, q in quotes.items() if q.valid},
            {s: q.name for s, q in quotes.items()},
        )
        account = self.broker.get_account()
        positions = self.broker.get_positions()

        self.risk.observe(account)

        acted: list[str] = []
        acted += self._manage_exits(now, positions, quotes, account)

        if self.risk.halt_all or self.risk.halt_new_positions:
            if acted:
                self._print_status(now, account, positions)
            return

        if now.time() >= self.cfg.strategy.no_new_position_after_time:
            if acted:
                self._print_status(now, account, positions)
            return

        acted += self._scan_entries(now, positions, quotes, account)

        if acted or self._cycle_count % 10 == 1:
            self._print_status(now, account, positions)

    # --------------------------- 离场 ---------------------------
    def _manage_exits(
        self,
        now: datetime,
        positions: list[Position],
        quotes: dict[str, Quote],
        account: Account,
    ) -> list[str]:
        acted: list[str] = []
        for pos in positions:
            quote = quotes.get(pos.symbol)
            if not quote or not quote.valid:
                continue

            if pos.available <= 0:
                log.debug("%s 当日买入，T+1 不可卖出", pos.symbol)
                continue

            reason = self.risk.hard_exit_reason(pos)
            snapshot: Snapshot | None = None

            if not reason:
                snapshot = self._snapshot(pos.symbol, quote)
                if snapshot:
                    reason = strategy.trend_exit_reason(snapshot)

            if not reason and self._should_consult_ai_exit(pos, quote):
                snapshot = snapshot or self._snapshot(pos.symbol, quote)
                if snapshot:
                    if self._ai_says_exit(pos, quote, snapshot, account):
                        reason = "AI 判断持有理由已消失"

            if reason and self._sell_all(pos, quote, reason):
                acted.append(f"SELL {pos.symbol} {reason}")
        return acted

    def _should_consult_ai_exit(self, pos: Position, quote: Quote) -> bool:
        if not self.cfg.strategy.allow_ai_sell:
            return False
        if self._ai_disabled_reason:
            return False
        # 仅在浮亏但未触发硬止损时征询 AI，避免无意义的 token 消耗
        return -self.cfg.risk.stop_loss_pct * 100 < pos.pnl_pct < 0

    def _ai_says_exit(
        self, pos: Position, quote: Quote, snapshot: Snapshot, account: Account
    ) -> bool:
        if not self._ai_due(pos.symbol, quote.price):
            return False

        context = strategy.build_ai_context(
            symbol=pos.symbol,
            name=pos.name,
            quote=quote,
            snapshot=snapshot,
            tech=strategy.technical_score(snapshot, quote),
            account=account,
            position=pos,
            risk_cfg=self.cfg.risk,
            scenario="exit",
        )
        decision = self._ask_ai(context)
        if decision is None:
            return False

        self.store.record_decision(
            symbol=pos.symbol,
            name=pos.name,
            action=decision.action,
            confidence=decision.confidence,
            source="ai",
            price=quote.price,
            payload=context,
            executed=False,
            reject_reason="" if decision.is_sell else "AI 建议继续持有",
        )
        return decision.is_sell and decision.confidence >= self.cfg.strategy.min_confidence

    def _sell_all(self, pos: Position, quote: Quote, reason: str) -> bool:
        qty = int(pos.available)
        if qty <= 0:
            return False

        limit_price = self._sell_limit(quote)
        verdict = self.risk.check_sell(
            position=pos, price=limit_price, qty=qty, quote=quote
        )
        if not verdict:
            log.warning("卖出被风控拦截 %s：%s", pos.symbol, verdict.reason)
            return False

        # 半自动模式：只生成委托单，等人在券商客户端下单后回填
        if self.cfg.is_manual:
            if self.store.has_pending_order(pos.symbol, "SELL"):
                return False
            self.store.record_order(
                symbol=pos.symbol,
                name=pos.name,
                side="SELL",
                price=limit_price,
                qty=qty,
                amount=limit_price * qty,
                fee=0.0,
                status="PENDING_MANUAL",
                message=reason,
            )
            log.warning(
                "【待手动卖出】%s %s %d 股 限价 %.2f —— 请在券商客户端下单后回填",
                pos.symbol, pos.name, qty, limit_price,
            )
            return True

        result = self.broker.sell(pos.symbol, limit_price, qty, pos.name)
        self.store.record_order(
            symbol=pos.symbol,
            name=pos.name,
            side="SELL",
            price=result.filled_price or limit_price,
            qty=qty,
            amount=(result.filled_price or limit_price) * qty,
            fee=result.fee,
            status="FILLED" if result.success else "REJECTED",
            message=f"{reason} | {result.message}",
            order_id=result.order_id,
        )

        if result.success:
            log.info("卖出成交 %s %s 数量 %d 原因：%s", pos.symbol, pos.name, qty, reason)
            return True

        log.error("卖出失败 %s：%s", pos.symbol, result.message)
        return False

    # --------------------------- 进场 ---------------------------
    def _scan_entries(
        self,
        now: datetime,
        positions: list[Position],
        quotes: dict[str, Quote],
        account: Account,
    ) -> list[str]:
        acted: list[str] = []
        held = {p.symbol for p in positions}

        candidates: list[tuple[float, str, Quote, Snapshot, strategy.TechSignal]] = []
        for symbol in self.cfg.universe:
            if symbol in held:
                continue
            quote = quotes.get(symbol)
            if not quote or not quote.valid:
                continue
            if self.cfg.risk.forbid_st and quote.is_st:
                log.debug("%s 为 ST 股票，跳过", symbol)
                continue
            if quote.at_limit_up:
                log.debug("%s 已涨停，跳过", symbol)
                continue

            snapshot = self._snapshot(symbol, quote)
            if not snapshot:
                continue

            tech = strategy.technical_score(snapshot, quote)
            if not tech.eligible:
                log.debug("%s 技术面淘汰：%s", symbol, "；".join(tech.blockers))
                continue
            if tech.score < self.cfg.strategy.min_technical_score:
                continue

            candidates.append((tech.score, symbol, quote, snapshot, tech))

        if not candidates:
            return acted

        candidates.sort(key=lambda item: item[0], reverse=True)
        limit = max(1, self.cfg.strategy.max_candidates_per_cycle)

        for score, symbol, quote, snapshot, tech in candidates[:limit]:
            if self._ai_disabled_reason:
                break
            if not self._ai_due(symbol, quote.price):
                log.debug("%s AI 分析冷却中，跳过", symbol)
                continue

            context = strategy.build_ai_context(
                symbol=symbol,
                name=quote.name,
                quote=quote,
                snapshot=snapshot,
                tech=tech,
                account=account,
                position=None,
                risk_cfg=self.cfg.risk,
                scenario="entry",
            )
            decision = self._ask_ai(context)
            if decision is None:
                continue

            log.info(
                "AI 决策 %s %s -> %s (置信度 %.2f) %s",
                symbol, quote.name, decision.action, decision.confidence, decision.thesis[:80],
            )

            if not decision.is_buy:
                self.store.record_decision(
                    symbol=symbol, name=quote.name, action=decision.action,
                    confidence=decision.confidence, source="ai", price=quote.price,
                    technical_score=score, payload=context,
                    reject_reason="AI 建议不买入",
                )
                continue

            limit_price = self._buy_limit(quote)
            qty = self.risk.size_order(
                decision=decision, account=account, price=limit_price, symbol=symbol
            )

            verdict = self.risk.check_buy(
                symbol=symbol, name=quote.name, price=limit_price, qty=qty,
                quote=quote, decision=decision, account=account, positions=positions,
            )
            if not verdict:
                log.warning("买入被风控拦截 %s：%s", symbol, verdict.reason)
                self.store.record_decision(
                    symbol=symbol, name=quote.name, action="BUY",
                    confidence=decision.confidence, source="ai", price=quote.price,
                    technical_score=score, payload=context,
                    reject_reason=verdict.reason,
                )
                continue

            # 半自动模式：只生成委托单，等人在券商客户端下单后回填
            if self.cfg.is_manual:
                if self.store.has_pending_order(symbol, "BUY"):
                    continue
                note = (
                    f"{decision.thesis[:60]} | 止损 {decision.stop_loss:.2f} "
                    f"止盈 {decision.take_profit:.2f}"
                )
                self.store.record_order(
                    symbol=symbol, name=quote.name, side="BUY",
                    price=limit_price, qty=qty, amount=limit_price * qty, fee=0.0,
                    status="PENDING_MANUAL", message=note,
                )
                self.store.record_decision(
                    symbol=symbol, name=quote.name, action="BUY",
                    confidence=decision.confidence, source="ai", price=quote.price,
                    technical_score=score, payload=context, executed=False,
                    reject_reason="待手动执行",
                )
                log.warning(
                    "【待手动买入】%s %s %d 股 限价 %.2f —— 请在券商客户端下单后回填",
                    symbol, quote.name, qty, limit_price,
                )
                acted.append(f"PENDING BUY {symbol} x{qty} @ {limit_price:.2f}")
                continue

            result = self.broker.buy(symbol, limit_price, qty, quote.name)
            fill_price = result.filled_price or limit_price
            self.store.record_order(
                symbol=symbol, name=quote.name, side="BUY",
                price=fill_price, qty=qty, amount=fill_price * qty, fee=result.fee,
                status="FILLED" if result.success else "REJECTED",
                message=result.message, order_id=result.order_id,
            )
            self.store.record_decision(
                symbol=symbol, name=quote.name, action="BUY",
                confidence=decision.confidence, source="ai", price=quote.price,
                technical_score=score, payload=context, executed=result.success,
                reject_reason="" if result.success else result.message,
            )

            if not result.success:
                log.error("买入失败 %s：%s", symbol, result.message)
                continue

            log.info(
                "买入成交 %s %s 价格 %.2f 数量 %d 止损 %.2f 止盈 %.2f",
                symbol, quote.name, fill_price, qty,
                decision.stop_loss, decision.take_profit,
            )
            acted.append(f"BUY {symbol} x{qty} @ {fill_price:.2f}")

            # 刷新账户与持仓，供后续候选使用最新额度
            account = self.broker.get_account()
            positions = self.broker.get_positions()

        return acted

    # --------------------------- 辅助 ---------------------------
    def _snapshot(self, symbol: str, quote: Quote) -> Snapshot | None:
        try:
            bars = self.market.bars_with_today(symbol, quote, limit=120)
        except Exception:  # noqa: BLE001
            log.exception("获取 %s K 线失败", symbol)
            return None

        if len(bars) < 30:
            log.warning("%s K 线数据不足（%d 根），跳过", symbol, len(bars))
            return None

        return compute_snapshot(
            symbol,
            [b.close for b in bars],
            [b.high for b in bars],
            [b.low for b in bars],
            [b.volume for b in bars],
            ma_periods=(5, 10, 20, 60),
            rsi_period=14,
            macd_params=(12, 26, 9),
            atr_period=14,
            boll_period=20,
            volume_window=5,
        )

    def _ai_due(self, symbol: str, price: float) -> bool:
        """同一标的的 AI 分析冷却控制。"""
        last = self.store.last_ai_decision(symbol)
        if not last:
            return True
        try:
            last_ts = datetime.fromisoformat(last["ts"])
        except (ValueError, TypeError):
            return True

        elapsed_min = (datetime.now() - last_ts).total_seconds() / 60
        if elapsed_min >= self.cfg.strategy.ai_min_interval_minutes:
            return True

        last_price = float(last.get("price") or 0.0)
        if last_price > 0 and price > 0:
            if abs(price / last_price - 1) >= self.cfg.strategy.ai_price_trigger_pct:
                return True
        return False

    def _ask_ai(self, context: dict) -> Decision | None:
        try:
            return self.ai.decide_trade(context)
        except BudgetExceeded as exc:
            self._ai_disabled_reason = str(exc)
            log.error("AI 已停用：%s", exc)
            return None
        except AIError as exc:
            log.error("AI 决策失败：%s", exc)
            return None
        except Exception:  # noqa: BLE001
            log.exception("AI 决策异常")
            return None

    def _buy_limit(self, quote: Quote) -> float:
        """略高于现价的限价，兼顾成交率与不追高。"""
        price = round(quote.price * 1.002, 2)
        return min(price, quote.limit_up)

    def _sell_limit(self, quote: Quote) -> float:
        price = round(quote.price * 0.998, 2)
        return max(price, quote.limit_down)

    def _print_status(
        self, now: datetime, account: Account, positions: list[Position]
    ) -> None:
        pnl_today = 0.0
        day_start = self.store.day_start_equity()
        if day_start and day_start > 0:
            pnl_today = (account.total_asset / day_start - 1) * 100

        color = "green" if pnl_today >= 0 else "red"
        console.print(
            f"[dim]{now:%H:%M:%S}[/dim] "
            f"总资产 [bold]{account.total_asset:,.2f}[/bold] "
            f"可用 {account.cash:,.2f} "
            f"仓位 {account.exposure*100:.1f}% "
            f"持仓 {len(positions)} 只 "
            f"当日 [{color}]{pnl_today:+.2f}%[/{color}]"
        )

    def _daily_summary(self) -> None:
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception:  # noqa: BLE001
            return

        day_start = self.store.day_start_equity(account.total_asset) or account.total_asset
        pnl = account.total_asset - day_start
        pct = (account.total_asset / day_start - 1) * 100 if day_start else 0.0
        usage = self.store.ai_usage_today()
        trades = self.store.count_orders_today()

        console.print()
        console.rule("收盘结算")
        console.print(f"  总资产   : {account.total_asset:,.2f}")
        console.print(f"  当日盈亏 : {pnl:+,.2f} ({pct:+.2f}%)")
        console.print(f"  持仓     : {len(positions)} 只，市值 {account.market_value:,.2f}")
        console.print(f"  成交笔数 : {trades}")
        console.print(
            f"  AI 使用  : {usage.get('calls', 0)} 次，"
            f"tokens {int(usage.get('prompt_tokens', 0)) + int(usage.get('completion_tokens', 0))}"
        )
        console.print()

    # --------------------------- 单次扫描（不交易） ---------------------------
    def scan_data(self) -> dict:
        """跑一遍完整分析流程，返回结构化结果。只读，不下单。"""
        now = calendar.now_cn()
        held_now = self.broker.get_positions()
        symbols = list(dict.fromkeys(self.cfg.universe + [p.symbol for p in held_now]))
        quotes = self.market.quotes(symbols)
        if not quotes:
            return {"ok": False, "error": "未获取到行情", "rows": []}

        self.broker.update_market(
            {s: q.price for s, q in quotes.items() if q.valid},
            {s: q.name for s, q in quotes.items()},
        )
        account = self.broker.get_account()
        positions = self.broker.get_positions()

        rows = []
        for symbol in symbols:
            quote = quotes.get(symbol)
            if not quote or not quote.valid:
                continue
            snapshot = self._snapshot(symbol, quote)
            if not snapshot:
                continue
            tech = strategy.technical_score(snapshot, quote)
            held = next((p for p in positions if p.symbol == symbol), None)
            candidate = tech.eligible and tech.score >= self.cfg.strategy.min_technical_score
            rows.append(
                {
                    "symbol": symbol,
                    "name": quote.name,
                    "price": round(quote.price, 2),
                    "pct_change": round(quote.pct_change, 2),
                    "score": tech.score,
                    "volume_ratio": round(quote.volume_ratio, 2),
                    "rsi": round(snapshot.rsi, 1) if snapshot.rsi else None,
                    "atr_pct": round(snapshot.atr_pct, 1) if snapshot.atr_pct else None,
                    "eligible": tech.eligible,
                    "candidate": candidate,
                    "held": held is not None,
                    "blockers": tech.blockers,
                    "reasons": tech.reasons[:4],
                }
            )

        rows.sort(key=lambda r: r["score"], reverse=True)
        return {
            "ok": True,
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trading_day": calendar.is_trading_day(now.date()),
            "total_asset": round(account.total_asset, 2),
            "cash": round(account.cash, 2),
            "positions": len(positions),
            "rows": rows,
        }

    def scan_once(self) -> None:
        """命令行版扫描：打印到终端。"""
        data = self.scan_data()
        if not data.get("ok"):
            console.print(f"[red]{data.get('error')}[/red]")
            return

        console.print()
        console.rule(f"扫描 @ {data['ts']}")
        console.print(
            f"总资产 {data['total_asset']:,.2f} | 可用 {data['cash']:,.2f} | "
            f"持仓 {data['positions']} 只 | 交易日 {data['trading_day']}\n"
        )

        for row in data["rows"]:
            mark = "持仓" if row["held"] else ("候选" if row["candidate"] else "观望")
            color = {"持仓": "cyan", "候选": "green", "观望": "dim"}[mark]
            console.print(
                f"[{color}]{mark}[/{color}] {row['symbol']} {row['name']:<6} "
                f"{row['price']:>8.2f} {row['pct_change']:>+6.2f}% "
                f"评分 {row['score']:.2f}  量比 {row['volume_ratio']:.2f}  "
                f"RSI {row['rsi'] or 0:.1f}  ATR% {row['atr_pct'] or 0:.1f}"
            )
            if row["blockers"]:
                console.print(f"       [red]淘汰原因：{'；'.join(row['blockers'])}[/red]")
            elif row["reasons"]:
                console.print(f"       [dim]{'；'.join(row['reasons'][:3])}[/dim]")
        console.print()
