"""AI 量化交易系统 - 命令行入口。

用法：
    python main.py check                 # 环境与连通性自检
    python main.py scan                  # 只分析不下单
    python main.py run                   # 启动（模拟盘）
    python main.py run --once            # 只跑一轮
    python main.py run --i-understand-the-risk   # 实盘（需 config 同时开启）
    python main.py backtest --symbols 600519,000001
    python main.py dashboard             # 启动 Web 监控面板
    python main.py reset-risk            # 清除当日风控暂停
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

from qtrader.ai.deepseek import AIError
from qtrader.config import AppConfig, ConfigError, load_config, validate_for_run
from qtrader.engine import TradingEngine
from qtrader.logging_setup import console, force_utf8_console, setup_logging
from qtrader.market import calendar
from qtrader.market.quotes import MarketData
from qtrader.paths import app_root, ensure_user_files, is_frozen
from qtrader.risk.manager import RiskManager
from qtrader.runtime import Runtime
from qtrader.storage import Storage

log = logging.getLogger("main")


def fail(message: str) -> None:
    console.print(f"[bold red]{message}[/bold red]")
    sys.exit(1)


# ============================ check ============================
def cmd_check(args: argparse.Namespace) -> None:
    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))
    rt = Runtime(cfg)
    market, ai, broker = rt.market, rt.ai, rt.broker

    console.rule("自检")
    console.print(f"配置文件  : {cfg.root / args.config}")
    console.print(f"运行模式  : {cfg.mode}（live_trading={cfg.live_trading}）")
    console.print(f"券商适配器: {broker.name}")
    console.print(f"自选池    : {len(cfg.universe)} 只")
    console.print()

    problems: list[str] = []

    # 1. 交易日历
    source = calendar.holiday_source()
    today = calendar.now_cn()
    console.print(
        f"[1/4] 交易日历  : 来源 {source}，今日 "
        f"{'可交易' if calendar.is_trading_day() else '休市'}，"
        f"当前{'处于' if calendar.is_trading_time(today, include_auction=True) else '不在'}交易时段"
    )
    if source == "fallback":
        problems.append("交易日历使用内置兜底表，建议 pip install akshare 获得准确日历")

    # 2. 行情
    symbol = cfg.universe[0]
    quote = market.quote(symbol)
    if quote and quote.valid:
        bars = market.daily_bars(symbol, limit=60)
        console.print(
            f"[2/4] 行情接口  : 正常，{symbol} {quote.name} "
            f"{quote.price:.2f} ({quote.pct_change:+.2f}%)，日线 {len(bars)} 根"
        )
        if len(bars) < 60:
            problems.append(f"{symbol} 日线数据偏少（{len(bars)} 根）")
    else:
        console.print("[2/4] 行情接口  : [red]失败[/red]")
        problems.append("行情接口不可用，检查网络或接口是否变更")

    # 3. DeepSeek
    try:
        msg = ai.ping()
        console.print(f"[3/4] DeepSeek  : 正常，{msg}")
    except (AIError, Exception) as exc:  # noqa: BLE001
        console.print(f"[3/4] DeepSeek  : [red]失败[/red] {exc}")
        problems.append(f"DeepSeek 不可用：{exc}")

    # 4. 券商
    try:
        broker.connect()
        account = broker.get_account()
        positions = broker.get_positions()
        console.print(
            f"[4/4] 券商连接  : 正常，总资产 {account.total_asset:,.2f}，"
            f"可用 {account.cash:,.2f}，持仓 {len(positions)} 只"
        )
        broker.close()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[4/4] 券商连接  : [red]失败[/red] {exc}")
        problems.append(f"券商连接失败：{exc}")

    console.print()
    if problems:
        console.print("[yellow]发现以下问题：[/yellow]")
        for p in problems:
            console.print(f"  - {p}")
    else:
        console.print("[green]全部检查通过，可以启动。[/green]")
    rt.close()


# ============================ scan ============================
def cmd_scan(args: argparse.Namespace) -> None:
    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))

    with Runtime(cfg) as rt:
        engine = TradingEngine(cfg, rt.store, rt.market, rt.ai, rt.broker, rt.risk)
        engine.scan_once()


# ============================ run ============================
def cmd_run(args: argparse.Namespace) -> None:
    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))

    errors = validate_for_run(cfg, allow_live=args.live_ack)
    if errors:
        for e in errors:
            console.print(f"[bold red]✗ {e}[/bold red]")
        sys.exit(1)

    with Runtime(cfg) as rt:
        engine = TradingEngine(cfg, rt.store, rt.market, rt.ai, rt.broker, rt.risk)
        engine.run(once=args.once)


# ============================ backtest ============================
def cmd_backtest(args: argparse.Namespace) -> None:
    from qtrader.backtest import backtest_symbol, format_report
    from qtrader.config import normalize_symbol

    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))

    symbols = [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        fail("未指定回测标的")

    market = MarketData(retries=3)
    reports = []
    for symbol in symbols:
        bars = market.daily_bars(symbol, limit=args.days)
        if len(bars) < 60:
            console.print(f"[yellow]{symbol} 数据不足（{len(bars)} 根），跳过[/yellow]")
            continue
        report = backtest_symbol(symbol, bars, cfg)
        reports.append(report)
        console.print(format_report(report))
        console.print()

    if not reports:
        fail("没有可回测的标的")


# ============================ web 控制台 ============================
def _open_browser_later(port: int, delay: float = 2.0) -> None:
    def _open() -> None:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:  # noqa: BLE001
            pass

    threading.Timer(delay, _open).start()


def cmd_web(args: argparse.Namespace) -> None:
    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))

    try:
        from qtrader.web.server import serve
    except ImportError:
        fail("缺少依赖：pip install fastapi uvicorn")
        return

    url = f"http://{args.host}:{args.port}"
    console.print()
    console.rule("[bold]A 股 AI 量化交易控制台[/bold]")
    console.print(f"  地址　: [blue]{url}[/blue]")
    console.print(f"  工作目录: {app_root()}")
    console.print("  关闭本窗口即退出服务\n")

    if args.host in ("127.0.0.1", "localhost") and not args.no_browser:
        _open_browser_later(args.port)

    serve(app_root(), host=args.host, port=args.port)


# ============================ reset-risk ============================
def cmd_reset_risk(args: argparse.Namespace) -> None:
    cfg = _load(args)
    setup_logging(cfg.resolve(cfg.storage.log_dir))
    store = Storage(cfg.resolve(cfg.storage.db_path))
    risk = RiskManager(cfg.risk, cfg.strategy, store)
    risk.reset()
    console.print("[green]风控暂停状态已清除[/green]")
    store.close()


# ============================ 共用 ============================
def _load(args: argparse.Namespace) -> AppConfig:
    try:
        cfg = load_config(args.config, root=app_root())
    except ConfigError as exc:
        fail(f"配置错误：{exc}")
        raise

    for warning in getattr(cfg, "_warnings", []):
        console.print(f"[yellow]警告：{warning}[/yellow]")
    return cfg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qtrader",
        description="A 股 AI 量化交易系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="环境与连通性自检")
    p_check.add_argument("--config", default=argparse.SUPPRESS)
    p_check.set_defaults(func=cmd_check)

    p_scan = sub.add_parser("scan", help="只分析不下单")
    p_scan.add_argument("--config", default=argparse.SUPPRESS)
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="启动盘中主循环")
    p_run.add_argument("--config", default=argparse.SUPPRESS)
    p_run.add_argument("--once", action="store_true", help="只执行一轮后退出")
    p_run.add_argument(
        "--i-understand-the-risk",
        dest="live_ack",
        action="store_true",
        help="确认已了解实盘风险（实盘模式必填）",
    )
    p_run.set_defaults(func=cmd_run)

    p_bt = sub.add_parser("backtest", help="基于日线的规则回测")
    p_bt.add_argument("--config", default=argparse.SUPPRESS)
    p_bt.add_argument("--symbols", default=",".join(["600519"]), help="逗号分隔，如 600519,000001")
    p_bt.add_argument("--days", type=int, default=500, help="回看 K 线数量")
    p_bt.set_defaults(func=cmd_backtest)

    for name, alias in (("web", "启动浏览器控制台"), ("dashboard", "web 的别名")):
        p = sub.add_parser(name, help=alias, aliases=[] if name == "dashboard" else [])
        p.add_argument("--config", default=argparse.SUPPRESS)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
        p.add_argument("--no-browser", dest="no_browser", action="store_true",
                       help="不自动打开浏览器")
        p.set_defaults(func=cmd_web)

    p_menu = sub.add_parser("menu", help="命令行交互菜单")
    p_menu.add_argument("--config", default=argparse.SUPPRESS)
    p_menu.set_defaults(func=lambda _a: interactive_menu())

    p_reset = sub.add_parser("reset-risk", help="清除风控暂停状态")
    p_reset.add_argument("--config", default=argparse.SUPPRESS)
    p_reset.set_defaults(func=cmd_reset_risk)

    return parser


def _ask(prompt: str) -> str:
    """读取输入。去掉 BOM 与首尾空白——管道/重定向输入常带 BOM。"""
    return input(prompt).replace("\ufeff", "").strip()


def _ns(**kwargs) -> argparse.Namespace:
    """构造命令行参数命名空间，供交互菜单复用各命令。"""
    base = {
        "config": "config.yaml",
        "once": False,
        "live_ack": False,
        "symbols": "600519",
        "days": 500,
        "host": "127.0.0.1",
        "port": 8080,
        "no_browser": True,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


MENU = """
[bold]1[/bold]. 环境自检             检查行情接口、DeepSeek、券商连接
[bold]2[/bold]. 扫描分析             只分析不下单，看信号是否符合预期
[bold]3[/bold]. 启动模拟盘           虚拟资金，不涉及真实账户
[bold]4[/bold]. 启动实盘             [red]真实资金下单，需二次确认[/red]
[bold]5[/bold]. 回测                 用历史日线检验规则层
[bold]6[/bold]. 监控面板             浏览器查看持仓与决策记录
[bold]7[/bold]. 清除风控暂停         当日亏损/回撤熔断后手动解除
[bold]0[/bold]. 退出
"""


def interactive_menu() -> None:
    """双击 exe 时的入口。"""
    root = app_root()

    while True:
        console.print()
        console.rule("[bold]A 股 AI 量化交易系统[/bold]")
        console.print(f"[dim]工作目录：{root}[/dim]")

        mode_text = "未知"
        try:
            cfg = load_config("config.yaml", root=root)
            mode_text = "实盘 LIVE" if cfg.is_live else "模拟盘 PAPER"
            if not cfg.ai.api_key:
                mode_text += "  [yellow]（未配置 DEEPSEEK_API_KEY）[/yellow]"
        except ConfigError as exc:
            console.print(f"[red]配置读取失败：{exc}[/red]")

        console.print(f"[dim]当前模式：{mode_text}[/dim]")
        console.print(MENU)

        try:
            choice = _ask("请选择 [0-7]: ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        if choice in ("0", "q", "quit", "exit", ""):
            console.print("[dim]已退出[/dim]")
            return

        try:
            if choice == "1":
                cmd_check(_ns())
            elif choice == "2":
                cmd_scan(_ns())
            elif choice == "3":
                cmd_run(_ns())
            elif choice == "4":
                console.print("\n[bold red]即将使用真实资金交易。[/bold red]")
                console.print("请确认：已在 QMT 模拟账号跑通过全流程、已了解亏损风险。")
                token = _ask("输入 I UNDERSTAND 继续，其他任意键返回: ")
                if token == "I UNDERSTAND":
                    cmd_run(_ns(live_ack=True))
                else:
                    console.print("[yellow]已取消[/yellow]")
            elif choice == "5":
                symbols = _ask("回测标的（逗号分隔，回车默认 600519）: ")
                cmd_backtest(_ns(symbols=symbols or "600519"))
            elif choice == "6":
                port_text = _ask("端口（回车默认 8080）: ")
                port = int(port_text) if port_text.isdigit() else 8080
                cmd_web(_ns(port=port, no_browser=False))
            elif choice == "7":
                cmd_reset_risk(_ns())
            else:
                console.print("[yellow]无效选项[/yellow]")
        except KeyboardInterrupt:
            console.print("\n[yellow]已中断，返回菜单[/yellow]")
        except SystemExit:
            pass
        except Exception:  # noqa: BLE001
            log.exception("命令执行失败")
            console.print("[red]执行失败，详见上方日志[/red]")


def main() -> None:
    # 必须在任何输出之前执行：避免打包后中文乱码
    force_utf8_console()

    # 打包成 exe 后首次运行，把模板配置释放到 exe 旁边
    root = app_root()
    created = ensure_user_files(root)
    if created:
        console.print(f"[green]已生成默认配置文件：{', '.join(created)}[/green]")

    if len(sys.argv) == 1:
        # 双击 exe：直接进入浏览器控制台
        cmd_web(_ns(no_browser=False))
        return

    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "config"):
        args.config = "config.yaml"
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        sys.exit(130)
    except SystemExit:
        if is_frozen():
            input("\n按回车键退出...")
        raise


if __name__ == "__main__":
    main()
