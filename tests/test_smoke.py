"""核心逻辑冒烟测试（不联网、不调用 AI）。

运行：
    python tests/test_smoke.py
或
    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qtrader.ai.deepseek import Decision, extract_json  # noqa: E402
from qtrader.backtest import backtest_symbol  # noqa: E402
from qtrader.brokers.base import round_lot  # noqa: E402
from qtrader.brokers.paper import PaperBroker  # noqa: E402
from qtrader.config import PaperBrokerConfig, RiskConfig, StrategyConfig  # noqa: E402
from qtrader.market.indicators import compute_snapshot, ema, rsi, sma  # noqa: E402
from qtrader.market.quotes import Bar, Quote  # noqa: E402
from qtrader.risk.manager import RiskManager  # noqa: E402
from qtrader.storage import Storage  # noqa: E402
from qtrader.strategy import technical_score  # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


# ============================ 指标 ============================
def test_indicators() -> None:
    print("\n[指标计算]")
    values = [1, 2, 3, 4, 5]
    ma = sma(values, 3)
    check("SMA 前两项为 None", ma[0] is None and ma[1] is None)
    check("SMA 最后一值为 4", ma[-1] == 4.0, f"got {ma[-1]}")

    up = list(range(1, 40))
    r = rsi(up, 14)
    check("单调上涨 RSI 接近 100", r[-1] is not None and r[-1] > 99, f"got {r[-1]}")

    down = list(range(40, 1, -1))
    r2 = rsi(down, 14)
    check("单调下跌 RSI 接近 0", r2[-1] is not None and r2[-1] < 1, f"got {r2[-1]}")

    e = ema([10.0] * 20, 5)
    check("常数序列 EMA 不变", all(abs(x - 10.0) < 1e-9 for x in e))

    # 横盘序列应给出中性 RSI，而不是 100
    flat = rsi([10.0] * 30, 14)
    check("横盘 RSI 为中性 50", abs((flat[-1] or 0) - 50.0) < 1e-9, f"got {flat[-1]}")

    # 构造一段「震荡上行」，保证不是单边行情
    bars = []
    price = 10.0
    for i in range(80):
        step = 0.001 + 0.005 * ((-1) ** i)  # 涨 0.6% / 跌 0.4% 交替，整体向上
        price *= 1 + step
        bars.append(Bar(date=(date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                        open=price * 0.998, close=price,
                        high=price * 1.005, low=price * 0.995,
                        volume=10000, amount=0))
    snap = compute_snapshot(
        "600000",
        [b.close for b in bars],
        [b.high for b in bars],
        [b.low for b in bars],
        [b.volume for b in bars],
    )
    check("快照非空", snap is not None)
    if snap:
        check("ATR% 为正", snap.atr_pct is not None and snap.atr_pct > 0)
        check("MA20 存在", snap.ma.get(20) is not None)
        check("收盘价高于 MA20", snap.close > (snap.ma.get(20) or 0))
        check("震荡上行 RSI 未达超买", (snap.rsi or 0) < 80, f"rsi={snap.rsi}")

    # 打分逻辑用构造好的快照直接验证，避免被真实行情的随机性干扰
    from qtrader.market.indicators import Snapshot

    def make_snapshot(**overrides):
        base = dict(
            symbol="600000", close=12.0,
            ma={5: 11.5, 10: 11.0, 20: 10.0},
            rsi=60.0, macd_dif=0.2, macd_dea=0.15, macd_hist=0.10,
            macd_hist_prev=0.05, atr=0.2, atr_pct=2.0,
            boll_mid=11.0, boll_upper=12.5, boll_lower=9.5, boll_pos=0.8,
            volume_ratio=1.5, high_20=12.0, low_20=9.5,
            ret_5d=3.0, ret_20d=8.0, volatility_20d=25.0, drawdown_60d=-5.0,
        )
        base.update(overrides)
        return Snapshot(**base)

    good_quote = Quote(symbol="600000", name="测试", price=12.0, prev_close=11.9,
                       volume=20000, volume_ratio=1.5)
    good = technical_score(make_snapshot(), good_quote)
    check("理想多头形态可通过筛选", good.eligible, str(good.blockers))
    check("理想多头形态得分高", good.score >= 0.9, f"score={good.score}")

    overbought = technical_score(make_snapshot(rsi=88.0), good_quote)
    check("RSI 88 被淘汰", not overbought.eligible
          and any("超买" in b for b in overbought.blockers), str(overbought.blockers))

    downtrend = technical_score(
        make_snapshot(close=9.0, ma={5: 9.2, 10: 9.6, 20: 10.0},
                      macd_hist=-0.15, macd_hist_prev=-0.08, rsi=35.0),
        good_quote,
    )
    check("下跌趋势被淘汰", not downtrend.eligible, str(downtrend.blockers))
    check("下跌趋势得分低", downtrend.score < 0.3, f"score={downtrend.score}")


# ============================ JSON 解析 ============================
def test_extract_json() -> None:
    print("\n[AI 输出解析]")
    check("纯 JSON", extract_json('{"action":"BUY"}') == {"action": "BUY"})
    check("markdown 包裹",
          extract_json('```json\n{"action":"HOLD"}\n```') == {"action": "HOLD"})
    check("前后有多余文字",
          extract_json('好的，我的判断是：{"action":"SELL","confidence":0.8} 以上。')
          == {"action": "SELL", "confidence": 0.8})
    check("嵌套对象", extract_json('{"a":{"b":1},"c":2}') == {"a": {"b": 1}, "c": 2})
    check("字符串内含括号",
          extract_json('{"reason":"包含 } 字符","ok":true}')["ok"] is True)
    check("非法输入返回 None", extract_json("完全不是 JSON") is None)


# ============================ 模拟撮合 ============================
def test_paper_broker() -> None:
    print("\n[模拟盘撮合]")
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "paper.json"
        fee_cfg = PaperBrokerConfig(slippage_bps=0)
        broker = PaperBroker(100_000.0, state, fee_cfg)
        broker.connect()
        broker.update_market({"600519": 100.0}, {"600519": "测试股"})

        acc = broker.get_account()
        check("初始总资产等于初始资金", abs(acc.total_asset - 100_000) < 1e-6)

        res = broker.buy("600519", 100.0, 100, "测试股")
        check("买入成功", res.success, res.message)
        check("成交价等于报价（滑点为 0）", abs(res.filled_price - 100.0) < 1e-9)
        expected_fee = max(100.0 * 100 * 0.00025, 5.0) + 10000 * 0.00001
        check("买入费用正确", abs(res.fee - round(expected_fee, 2)) < 0.02,
              f"got {res.fee}, expect {expected_fee:.2f}")

        acc = broker.get_account()
        check("现金已扣减", abs(acc.cash - (100_000 - 10000 - res.fee)) < 0.02,
              f"cash={acc.cash}")

        pos = broker.get_positions()[0]
        check("持仓数量 100", pos.qty == 100)
        check("T+1 可卖数量为 0", pos.available == 0)

        blocked = broker.sell("600519", 101.0, 100, "测试股")
        check("当日卖出被拒（T+1）", not blocked.success, blocked.message)

        # 模拟跨日
        broker._state_date = "2000-01-01"
        pos = broker.get_positions()[0]
        check("跨日后可卖数量解锁", pos.available == 100)

        broker.update_market({"600519": 110.0})
        sell = broker.sell("600519", 110.0, 100, "测试股")
        check("次日卖出成功", sell.success, sell.message)
        check("卖出收取印花税",
              sell.fee > max(110.0 * 100 * 0.00025, 5.0), f"fee={sell.fee}")
        check("清仓后无持仓", len(broker.get_positions()) == 0)

        final = broker.get_account()
        check("盈利交易后总资产增加", final.total_asset > 100_000,
              f"total={final.total_asset}")
        broker.close()

        # 状态持久化
        broker2 = PaperBroker(100_000.0, state, fee_cfg)
        broker2.connect()
        check("重启后现金恢复", abs(broker2.get_account().cash - final.cash) < 0.01)


def test_ai_decision() -> None:
    """用 mock 替换网络调用，验证决策解析与边界收敛（不消耗真实 token）。"""
    print("\n[AI 决策解析]")
    from qtrader.ai.deepseek import AIConfig, DeepSeekClient

    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(Path(tmp) / "t.db")
        client = DeepSeekClient(AIConfig(api_key="sk-test"), store)

        def fake(payload: str):
            client.chat = lambda system, user, json_mode=True: (payload, {})  # type: ignore[assignment]

        context = {"symbol": "600519", "name": "贵州茅台", "quote": {"price": 100.0}}

        fake('{"action":"buy","confidence":1.7,"position_pct":0.35,'
             '"stop_loss":0,"take_profit":0,"thesis":"突破","risks":["高位"]}')
        d = client.decide_trade(context)
        check("小写 action 归一化为 BUY", d.action == "BUY")
        check("置信度被截断到 1.0", d.confidence == 1.0, f"got {d.confidence}")
        check("仓位比例保留", abs(d.position_pct - 0.35) < 1e-9)
        check("缺失止损时按 -7% 兜底", abs(d.stop_loss - 93.0) < 0.01, f"got {d.stop_loss}")
        check("缺失止盈时按 +15% 兜底", abs(d.take_profit - 115.0) < 0.01,
              f"got {d.take_profit}")
        check("风险点被保留", d.risks == ["高位"])

        fake('{"action":"MOON","confidence":0.9}')
        d = client.decide_trade(context)
        check("非法 action 降级为 HOLD", d.action == "HOLD")

        fake('{"action":"BUY","confidence":"abc"}')
        d = client.decide_trade(context)
        check("非法置信度归零", d.confidence == 0.0)

        # 已持有场景下不应重复兜底止损
        fake('{"action":"HOLD","confidence":0.5,"thesis":"观望"}')
        d = client.decide_trade(context)
        check("HOLD 决策止损保持 0", d.stop_loss == 0.0)

        store.close()


def test_lot_rules() -> None:
    print("\n[下单数量规则]")
    check("主板 100 股整数倍", round_lot("600519", 356) == 300)
    check("主板不足 100 股归零", round_lot("600519", 80) == 0)
    check("科创板 200 股起", round_lot("688981", 180) == 0)
    check("科创板 400 股", round_lot("688981", 456) == 456)
    check("创业板 100 股整数倍", round_lot("300750", 250) == 200)


# ============================ 风控 ============================
def test_risk() -> None:
    print("\n[风控规则]")
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(Path(tmp) / "t.db")
        risk_cfg = RiskConfig(
            max_position_pct=0.2, max_total_exposure_pct=0.8,
            max_daily_loss_pct=0.03, min_order_amount=5000, max_daily_trades=20,
        )
        strat_cfg = StrategyConfig(min_confidence=0.6)
        rm = RiskManager(risk_cfg, strat_cfg, store)

        from qtrader.brokers.base import Account

        account = Account(total_asset=100_000, cash=100_000, market_value=0)

        def quote(price=10.0, prev=10.0, name="正常股", pct=0.0):
            q = Quote(symbol="600000", name=name, price=price, prev_close=prev,
                      volume=1000, volume_ratio=1.0)
            q.pct_change = pct
            return q

        def decision(conf=0.8, pct=0.2):
            return Decision(action="BUY", confidence=conf, position_pct=pct,
                            stop_loss=9.0, take_profit=12.0)

        ok = rm.check_buy(symbol="600000", name="正常股", price=10.0, qty=1000,
                          quote=quote(), decision=decision(), account=account,
                          positions=[])
        check("正常买入通过", ok.allowed, ok.reason)

        st = rm.check_buy(symbol="600000", name="ST测试", price=10.0, qty=1000,
                          quote=quote(name="ST测试"), decision=decision(),
                          account=account, positions=[])
        check("ST 股被拦截", not st.allowed, st.reason)

        # 涨停：昨收 10，现价 11
        limit_up = rm.check_buy(symbol="600000", name="正常股", price=11.0, qty=1000,
                                quote=quote(price=11.0, prev=10.0),
                                decision=decision(), account=account, positions=[])
        check("涨停价买入被拦截", not limit_up.allowed, limit_up.reason)

        low_conf = rm.check_buy(symbol="600000", name="正常股", price=10.0, qty=1000,
                                quote=quote(), decision=decision(conf=0.3),
                                account=account, positions=[])
        check("低置信度被拦截", not low_conf.allowed, low_conf.reason)

        small = rm.check_buy(symbol="600000", name="正常股", price=10.0, qty=100,
                             quote=quote(), decision=decision(), account=account,
                             positions=[])
        check("金额过小被拦截", not small.allowed, small.reason)

        # 单票上限 20%：总资产 10 万，买入 3000 股 * 10 元 = 3 万 > 2 万
        big = rm.check_buy(symbol="600000", name="正常股", price=10.0, qty=3000,
                           quote=quote(), decision=decision(), account=account,
                           positions=[])
        check("超单票上限被拦截", not big.allowed, big.reason)

        # 仓位计算：总资产 10 万，AI 要 20% -> 2 万 / 10 元 = 2000 股，但受现金 98%
        qty = rm.size_order(decision=decision(pct=0.2), account=account,
                            price=10.0, symbol="600000")
        check("仓位换算为 2000 股", qty == 2000, f"got {qty}")

        capped = rm.size_order(decision=decision(pct=0.9), account=account,
                               price=10.0, symbol="600000")
        check("AI 超额仓位被压到单票上限", capped == 2000, f"got {capped}")

        # 日亏损熔断
        store.record_equity(100_000, 100_000, 0)
        rm.observe(Account(total_asset=96_000, cash=96_000, market_value=0))
        check("日亏损 4% 触发停止开仓", rm.halt_new_positions)
        after = rm.check_buy(symbol="600000", name="正常股", price=10.0, qty=1000,
                             quote=quote(), decision=decision(), account=account,
                             positions=[])
        check("熔断后买入被拒", not after.allowed, after.reason)

        rm.reset()
        check("reset 后恢复", not rm.halt_new_positions)

        # 止损判定
        from qtrader.brokers.base import Position

        pos = Position(symbol="600000", qty=1000, available=1000,
                       avg_cost=10.0, last_price=9.2)
        check("浮亏 8% 触发硬止损", rm.hard_exit_reason(pos) is not None)

        pos2 = Position(symbol="600000", qty=1000, available=1000,
                        avg_cost=10.0, last_price=9.5)
        check("浮亏 5% 不触发硬止损", rm.hard_exit_reason(pos2) is None)

        pos3 = Position(symbol="600000", qty=1000, available=1000,
                        avg_cost=10.0, last_price=11.6)
        check("浮盈 16% 触发止盈", rm.hard_exit_reason(pos3) is not None)

        store.close()


def test_manual_orders() -> None:
    """半自动模式：待执行委托的判重与状态流转。"""
    print("\n[半自动委托]")
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(Path(tmp) / "t.db")

        store.record_order(
            symbol="600900", name="长江电力", side="BUY", price=29.4, qty=1000,
            amount=29400.0, fee=0.0, status="PENDING_MANUAL", message="测试",
        )
        check("同标的同方向判重生效", store.has_pending_order("600900", "BUY") is True)
        check("不同方向不判重", store.has_pending_order("600900", "SELL") is False)
        check("待执行列表有 1 条", len(store.pending_manual_orders()) == 1)

        order = store.pending_manual_orders()[0]
        store.update_order(order["id"], status="FILLED", message="已回填")
        check("状态可更新", store.get_order(order["id"])["status"] == "FILLED")
        check("更新后不再出现在待执行列表", len(store.pending_manual_orders()) == 0)
        check("成交笔数统计包含该单", store.count_orders_today() == 1)

        store.close()


# ============================ 回测 ============================
def test_backtest() -> None:
    print("\n[回测]")
    from qtrader.config import AppConfig

    # 三段行情：窄幅震荡 -> 放量突破上行 -> 回落，确保既有买点也有卖点
    bars: list[Bar] = []
    price = 20.0
    for i in range(300):
        if i < 180:
            step = 0.0012 * ((-1) ** i)
            vol = 8000
        elif i < 250:
            step = 0.008 + 0.004 * ((-1) ** i)
            vol = 16000
        else:
            step = -0.006 + 0.003 * ((-1) ** i)
            vol = 12000
        price *= 1 + step
        price = max(price, 5.0)
        bars.append(Bar(
            date=(date(2025, 1, 1) + timedelta(days=i)).isoformat(),
            open=price * 0.996, close=price,
            high=price * 1.008, low=price * 0.992,
            volume=vol, amount=0,
        ))

    cfg = AppConfig()
    report = backtest_symbol("600000", bars, cfg)
    check("回测返回报告", report is not None)
    check("期末资金为正", report.final_cash > 0)
    check("最大回撤为负数或零", report.max_drawdown_pct <= 0,
          f"got {report.max_drawdown_pct}")
    check("报告含日期区间", bool(report.start_date and report.end_date))
    print(f"        -> 策略 {report.total_return_pct:+.2f}% / "
          f"基准 {report.benchmark_return_pct:+.2f}% / "
          f"交易 {len(report.trades)} 笔 / 胜率 {report.win_rate_pct:.0f}%")


# ============================ 配置校验 ============================
def test_config_validation() -> None:
    print("\n[配置校验]")
    from qtrader.config import AppConfig, validate_for_run

    cfg = AppConfig(universe=["600519"], mode="paper")
    cfg.ai.api_key = "sk-test"
    check("模拟盘配置无致命错误", validate_for_run(cfg, allow_live=False) == [])

    live_cfg = AppConfig(universe=["600519"], mode="live", live_trading=True)
    live_cfg.ai.api_key = "sk-test"
    errors = validate_for_run(live_cfg, allow_live=False)
    check("实盘未确认风险时阻止启动", any("i-understand-the-risk" in e for e in errors))

    errors = validate_for_run(live_cfg, allow_live=True)
    check("实盘未配 qmt 时阻止启动", any("qmt" in e.lower() for e in errors))

    no_key = AppConfig(universe=["600519"])
    check("缺少 API Key 时报错",
          any("DEEPSEEK_API_KEY" in e for e in validate_for_run(no_key, allow_live=False)))

    bad_symbol = AppConfig(universe=["ABC"])
    bad_symbol.ai.api_key = "sk-test"
    check("非法代码被拦截",
          any("格式" in e for e in validate_for_run(bad_symbol, allow_live=False)))

    # 半自动实盘：只出信号不真下单，允许用 paper 做本地记账
    manual_live = AppConfig(
        universe=["600519"], mode="live", live_trading=True, execution_mode="manual"
    )
    manual_live.ai.api_key = "sk-test"
    errors = validate_for_run(manual_live, allow_live=True)
    check("半自动实盘允许 paper 记账",
          not any("qmt" in e.lower() for e in errors), errors)

    bad_mode = AppConfig(universe=["600519"], execution_mode="turbo")
    bad_mode.ai.api_key = "sk-test"
    check("非法 execution_mode 被拦截",
          any("execution_mode" in e for e in validate_for_run(bad_mode, allow_live=False)))


def main() -> None:
    print("=" * 60)
    print("核心逻辑冒烟测试")
    print("=" * 60)
    test_indicators()
    test_extract_json()
    test_ai_decision()
    test_paper_broker()
    test_manual_orders()
    test_lot_rules()
    test_risk()
    test_backtest()
    test_config_validation()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
