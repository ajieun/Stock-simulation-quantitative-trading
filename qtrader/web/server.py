"""浏览器控制台后端：FastAPI + 后台引擎线程。

安全边界：
- 默认只监听 127.0.0.1，不对外网开放。
- 实盘启动仍需「配置开启 + 请求中带风险确认」，与命令行两道锁一致。
- 面板只读账户数据，写操作只有「启停引擎」「保存配置」「回填委托」四类。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from ..config import ConfigError, load_config, validate_for_run
from ..storage import Storage
from .manager import EngineManager, LogBuffer, execute_pending_order
from .ui import UI_HTML

log = logging.getLogger(__name__)

_config_path = "config.yaml"


def _read_paper_state(cfg) -> tuple[float, list[dict]]:
    """从模拟账户状态文件读取现金与持仓（引擎未运行时用它展示）。"""
    state_file = cfg.resolve(cfg.storage.paper_state_path)
    if not state_file.exists():
        # 首次运行还没有任何成交，按配置的初始资金展示，避免界面显示 0
        initial = cfg.broker.paper.initial_cash if cfg.broker.name == "paper" else 0.0
        return float(initial), []
    try:
        import json

        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0.0, []

    cash = float(data.get("cash", 0.0) or 0.0)
    positions: list[dict] = []
    for symbol, pos in (data.get("positions") or {}).items():
        qty = int(pos.get("qty", 0))
        price = float(pos.get("last_price") or pos.get("avg_cost") or 0.0)
        cost = float(pos.get("avg_cost") or 0.0)
        positions.append(
            {
                "symbol": symbol,
                "name": pos.get("name", ""),
                "qty": qty,
                "available": int(pos.get("available", 0)),
                "avg_cost": cost,
                "last_price": price,
                "market_value": qty * price,
                "pnl_pct": (price / cost - 1) * 100 if cost > 0 else 0.0,
                "holding_days": _holding_days(pos.get("buy_date", "")),
            }
        )
    return cash, positions


def _holding_days(buy_date: str) -> int:
    try:
        return (date.today() - date.fromisoformat(buy_date)).days
    except (ValueError, TypeError):
        return 0


def _collect_state(cfg, manager: EngineManager, store: Storage) -> dict[str, Any]:
    from ..market import calendar

    rt = manager.runtime
    if manager.is_running() and rt is not None:
        try:
            account = rt.broker.get_account()
            raw_positions = rt.broker.get_positions()
            total, cash, market_value = (
                account.total_asset, account.cash, account.market_value
            )
            positions = [
                {
                    "symbol": p.symbol,
                    "name": p.name,
                    "qty": p.qty,
                    "available": p.available,
                    "avg_cost": p.avg_cost,
                    "last_price": p.last_price,
                    "market_value": p.market_value,
                    "pnl_pct": p.pnl_pct,
                    "holding_days": p.holding_days,
                }
                for p in raw_positions
            ]
        except Exception:  # noqa: BLE001
            log.exception("读取运行中账户失败")
            cash, positions = _read_paper_state(cfg)
            market_value = sum(p["market_value"] for p in positions)
            total = cash + market_value
    else:
        cash, positions = _read_paper_state(cfg)
        market_value = sum(p["market_value"] for p in positions)
        total = cash + market_value

    day_start = store.day_start_equity() or 0.0
    day_pnl = (total / day_start - 1) * 100 if day_start > 0 else 0.0
    usage = store.ai_usage_today()

    mode_parts = []
    if cfg.is_live:
        mode_parts.append("半自动" if cfg.is_manual else "全自动")
        mode_parts.append("实盘")
    else:
        mode_parts.append("模拟盘")
    if cfg.is_manual and not cfg.is_live:
        mode_parts.append("· 人工执行")

    return {
        "server_time": calendar.now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "trading_day": calendar.is_trading_day(),
        "in_session": calendar.is_trading_time(),
        "mode_text": " ".join(mode_parts),
        "is_live": cfg.is_live,
        "is_manual": cfg.is_manual,
        "execution_mode": cfg.execution_mode,
        "engine": manager.status(),
        "total_asset": round(total, 2),
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "day_start": round(day_start, 2),
        "day_pnl_pct": round(day_pnl, 2),
        "exposure_pct": round(market_value / total * 100, 2) if total > 0 else 0.0,
        "ai_calls": usage.get("calls", 0),
        "ai_tokens": int(usage.get("prompt_tokens", 0))
        + int(usage.get("completion_tokens", 0)),
        "positions": positions,
        "pending": store.pending_manual_orders(),
        "decisions": store.recent_decisions(40),
        "orders": store.recent_orders(40),
    }


def build_app(root: Path):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="A 股 AI 量化交易控制台", docs_url=None, redoc_url=None)

    manager = EngineManager(root)
    log_buffer = LogBuffer()
    root_logger = logging.getLogger()
    if log_buffer not in root_logger.handlers:
        root_logger.addHandler(log_buffer)
    if root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    def cfg_now():
        return load_config(_config_path, root=root)

    # ----------------------------- 页面 -----------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return UI_HTML

    # ----------------------------- 状态 -----------------------------
    @app.get("/api/state")
    def state() -> JSONResponse:
        cfg = cfg_now()
        store = Storage(cfg.resolve(cfg.storage.db_path))
        try:
            return JSONResponse(_collect_state(cfg, manager, store))
        finally:
            store.close()

    @app.get("/api/logs")
    def logs(after: int = 0) -> JSONResponse:
        return JSONResponse(log_buffer.since(after))

    # ----------------------------- 引擎 -----------------------------
    @app.post("/api/engine/start")
    def engine_start(payload: dict = Body(default={})) -> JSONResponse:
        live_ack = bool(payload.get("live_ack"))
        try:
            status = manager.start(live_ack=live_ack)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(status)

    @app.post("/api/engine/stop")
    def engine_stop() -> JSONResponse:
        return JSONResponse(manager.stop())

    # ----------------------------- 扫描 -----------------------------
    @app.post("/api/scan")
    def scan() -> JSONResponse:
        from ..engine import TradingEngine
        from ..runtime import Runtime

        cfg = cfg_now()
        rt = Runtime(cfg)
        try:
            rt.broker.connect()
            engine = TradingEngine(
                cfg, rt.store, rt.market, rt.ai, rt.broker, rt.risk
            )
            return JSONResponse(engine.scan_data())
        except Exception as exc:  # noqa: BLE001
            log.exception("扫描失败")
            return JSONResponse({"ok": False, "error": str(exc), "rows": []})
        finally:
            rt.close()

    # ----------------------------- 委托回填 -----------------------------
    @app.post("/api/orders/{order_id}/confirm")
    def confirm(order_id: int) -> JSONResponse:
        cfg = cfg_now()
        store = Storage(cfg.resolve(cfg.storage.db_path))
        try:
            order = store.get_order(order_id)
            if not order:
                raise HTTPException(status_code=404, detail="委托不存在")
            if order["status"] != "PENDING_MANUAL":
                raise HTTPException(status_code=400, detail="该委托不在待执行状态")

            result = execute_pending_order(manager, cfg, order)
            if not result["success"]:
                return JSONResponse(result)

            filled = result["filled_price"] or order["price"]
            fee = result["fee"]
            store.update_order(
                order_id,
                status="FILLED",
                message=f"{order.get('message', '')} | 人工执行回填 @ {filled}",
            )
            store.record_order(
                symbol=order["symbol"],
                name=order["name"],
                side=order["side"],
                price=filled,
                qty=result["qty"] or order["qty"],
                amount=filled * (result["qty"] or order["qty"]),
                fee=fee,
                status="FILLED",
                message="人工执行回填",
            )
            return JSONResponse({"success": True, **result})
        finally:
            store.close()

    @app.post("/api/orders/{order_id}/dismiss")
    def dismiss(order_id: int) -> JSONResponse:
        cfg = cfg_now()
        store = Storage(cfg.resolve(cfg.storage.db_path))
        try:
            order = store.get_order(order_id)
            if not order:
                raise HTTPException(status_code=404, detail="委托不存在")
            store.update_order(order_id, status="DISMISSED", message="人工忽略")
            return JSONResponse({"success": True})
        finally:
            store.close()

    # ----------------------------- 配置 -----------------------------
    @app.get("/api/config")
    def get_config() -> JSONResponse:
        path = root / _config_path
        if not path.exists():
            raise HTTPException(status_code=404, detail="config.yaml 不存在")
        return JSONResponse({"content": path.read_text(encoding="utf-8")})

    @app.put("/api/config")
    def put_config(payload: dict = Body(...)) -> JSONResponse:
        content = str(payload.get("content", ""))
        if not content.strip():
            raise HTTPException(status_code=400, detail="配置内容为空")

        path = root / _config_path
        tmp = root / "config.yaml.tmp"
        try:
            tmp.write_text(content, encoding="utf-8")
            try:
                probe = load_config(tmp, root=root)
            except ConfigError as exc:
                raise HTTPException(status_code=400, detail=f"配置无效：{exc}") from exc

            warnings = getattr(probe, "_warnings", [])
            if warnings:
                raise HTTPException(
                    status_code=400,
                    detail="配置含无法识别的键，请检查拼写：" + "；".join(warnings),
                )
        finally:
            if tmp.exists():
                tmp.unlink()

        if path.exists():
            (root / "config.yaml.bak").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        path.write_text(content, encoding="utf-8")
        return JSONResponse({"success": True})

    return app


def serve(root: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(build_app(root), host=host, port=port, log_level="warning")
