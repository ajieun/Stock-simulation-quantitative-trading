"""配置加载：YAML + .env，映射为强类型 dataclass。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any, get_type_hints

import yaml

try:  # python-dotenv 可选
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False


class ConfigError(RuntimeError):
    """配置错误。"""


# ----------------------------- 分区配置 -----------------------------

@dataclass
class StrategyConfig:
    poll_interval_seconds: int = 30
    bar_refresh_seconds: int = 300
    min_confidence: float = 0.66
    min_technical_score: float = 0.60
    max_candidates_per_cycle: int = 3
    ai_min_interval_minutes: int = 30
    ai_price_trigger_pct: float = 0.02
    no_new_position_after: str = "14:45"
    allow_ai_sell: bool = True

    @property
    def no_new_position_after_time(self) -> dtime:
        return parse_hhmm(self.no_new_position_after)


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20
    max_total_exposure_pct: float = 0.80
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    max_daily_trades: int = 20
    min_order_amount: float = 5000.0
    stop_loss_pct: float = 0.07
    take_profit_pct: float = 0.15
    max_holding_days: int = 0
    forbid_st: bool = True
    forbid_limit_up_buy: bool = True
    limit_up_buffer_pct: float = 0.002
    forbid_limit_down_sell: bool = True


@dataclass
class AIConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.2
    max_tokens: int = 900
    timeout_seconds: int = 60
    max_retries: int = 3
    daily_token_budget: int = 400_000
    api_key: str = ""


@dataclass
class PaperBrokerConfig:
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.001
    transfer_fee_rate: float = 0.00001
    slippage_bps: float = 5.0


@dataclass
class QMTBrokerConfig:
    account_id: str = ""
    account_type: str = "STOCK"
    mini_qmt_path: str = ""
    session_id: int = 20260101


@dataclass
class BrokerConfig:
    name: str = "paper"
    paper: PaperBrokerConfig = field(default_factory=PaperBrokerConfig)
    qmt: QMTBrokerConfig = field(default_factory=QMTBrokerConfig)


@dataclass
class StorageConfig:
    db_path: str = "data/trader.db"
    log_dir: str = "logs"
    paper_state_path: str = "data/paper_account.json"


@dataclass
class AppConfig:
    mode: str = "paper"
    live_trading: bool = False
    # auto   = 程序自动下单（需要券商接口）
    # manual = 只生成委托建议，由人工在券商客户端下单后回填
    execution_mode: str = "auto"
    universe: list[str] = field(default_factory=list)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    # 运行期派生（不是配置项）
    root: Path = field(default_factory=Path.cwd, compare=False)

    @property
    def is_live(self) -> bool:
        """实盘判定：必须同时满足 mode=live 与 live_trading=true。

        做成属性而不是字段，避免手工构造 AppConfig 时出现「模式是 live 但
        is_live 仍是 False」的不一致状态，绕过实盘校验。
        """
        return self.mode == "live" and self.live_trading

    @property
    def is_manual(self) -> bool:
        return self.execution_mode == "manual"

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


# ----------------------------- 工具函数 -----------------------------

def parse_hhmm(text: str) -> dtime:
    try:
        hh, mm = text.strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"时间格式应为 HH:MM，收到 {text!r}") from exc


def _coerce(value: Any, annotation: Any) -> Any:
    """按 dataclass 字段注解做轻量类型转换（YAML 已给出大部分正确类型）。"""
    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    try:
        if name == "int":
            return int(value)
        if name == "float":
            return float(value)
        if name == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(value)
        if name == "str":
            return str(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置值类型错误：{value!r} 无法转换为 {name}") from exc
    return value


def _field_types(cls: type) -> dict[str, Any]:
    """解析字段真实类型。

    模块启用了 `from __future__ import annotations`，dataclasses.fields() 里的
    f.type 是字符串（如 "AIConfig"），必须用 get_type_hints 还原成真实类，
    否则嵌套配置不会被实例化。
    """
    try:
        return get_type_hints(cls)
    except Exception:  # noqa: BLE001
        return {f.name: f.type for f in fields(cls)}


def _build(cls: type, data: dict[str, Any] | None) -> Any:
    """把嵌套 dict 递归构造成 dataclass，未知键直接忽略并收集告警。"""
    data = data or {}
    if not is_dataclass(cls):
        return data

    types = _field_types(cls)
    known = {f.name for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            continue
        annotation = types.get(key)
        if is_dataclass(annotation) and isinstance(value, dict):
            kwargs[key] = _build(annotation, value)  # type: ignore[arg-type]
        else:
            kwargs[key] = _coerce(value, annotation)
    return cls(**kwargs)


def unknown_keys(cls: type, data: dict[str, Any] | None, prefix: str = "") -> list[str]:
    """列出配置里拼错 / 不存在的键，避免静默失效。"""
    data = data or {}
    if not is_dataclass(cls):
        return []
    types = _field_types(cls)
    known = {f.name for f in fields(cls)}
    bad: list[str] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in known:
            bad.append(path)
            continue
        annotation = types.get(key)
        if is_dataclass(annotation) and isinstance(value, dict):
            bad.extend(unknown_keys(annotation, value, prefix=f"{path}."))
    return bad


# ----------------------------- 加载入口 -----------------------------

def load_config(path: str | Path = "config.yaml", root: Path | None = None) -> AppConfig:
    root = (root or Path.cwd()).resolve()
    load_dotenv(root / ".env")

    cfg_path = Path(path)
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    if not cfg_path.exists():
        raise ConfigError(f"找不到配置文件：{cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是映射（key: value）")

    known = {f.name for f in fields(AppConfig)} - {"root"}
    extras = [k for k in raw if k not in known]
    raw = {k: v for k, v in raw.items() if k in known}

    cfg: AppConfig = _build(AppConfig, raw)
    cfg.root = root

    warnings = [f"未知配置项（已忽略）：{k}" for k in extras]
    warnings += [f"未知配置项（已忽略）：{k}" for k in unknown_keys(AppConfig, raw)]

    # 环境变量注入密钥，避免明文写入仓库
    cfg.ai.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip() or cfg.ai.api_key
    cfg.ai.model = os.getenv("DEEPSEEK_MODEL", "").strip() or cfg.ai.model
    cfg.broker.qmt.account_id = os.getenv("QMT_ACCOUNT_ID", "").strip() or cfg.broker.qmt.account_id

    cfg.mode = (cfg.mode or "paper").strip().lower()

    cfg.universe = [normalize_symbol(s) for s in (cfg.universe or []) if str(s).strip()]
    cfg._warnings = warnings  # type: ignore[attr-defined]
    return cfg


def normalize_symbol(symbol: str) -> str:
    """统一成 6 位裸代码：600519 / sh600519 / 600519.SH -> 600519。"""
    s = str(symbol).strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if s.endswith((".SH", ".SZ", ".BJ")):
        s = s[:-3]
    s = s.lstrip(".").strip()
    return s.zfill(6) if s.isdigit() else s


def validate_for_run(cfg: AppConfig, allow_live: bool) -> list[str]:
    """启动前校验，返回致命错误列表。"""
    errors: list[str] = []

    if not cfg.universe:
        errors.append("universe 为空，至少配置一只股票")

    bad = [s for s in cfg.universe if not (s.isdigit() and len(s) == 6)]
    if bad:
        errors.append(f"股票代码格式不正确：{', '.join(bad)}")

    if cfg.execution_mode not in ("auto", "manual"):
        errors.append(
            f"execution_mode 只能是 auto 或 manual，当前为 {cfg.execution_mode!r}"
        )

    if cfg.is_live and not allow_live:
        errors.append(
            "配置要求实盘（mode=live 且 live_trading=true），但未确认风险声明。"
            " 命令行需传 --i-understand-the-risk，网页需勾选风险确认。已阻止启动。"
        )

    # 全自动实盘：必须真的能下单，否则就是假实盘
    if cfg.is_live and not cfg.is_manual:
        if cfg.broker.name != "qmt":
            errors.append("自动实盘必须使用 broker.name=qmt，paper 适配器不会真实下单")
        if not cfg.broker.qmt.mini_qmt_path:
            errors.append("broker.qmt.mini_qmt_path 未配置（需指向 QMT 的 userdata_mini 目录）")
        if not cfg.broker.qmt.account_id:
            errors.append("broker.qmt.account_id 未配置（可在 .env 设置 QMT_ACCOUNT_ID）")

    # 半自动实盘：程序只出信号，用 paper 适配器做本地记账
    if cfg.is_live and cfg.is_manual and cfg.broker.name != "paper":
        errors.append(
            "半自动模式下 broker 应保持 paper（仅本地记账），"
            "实际委托请在券商客户端手动执行"
        )

    if cfg.mode == "live" and not cfg.live_trading:
        errors.append(
            "mode=live 但 live_trading=false，存在配置矛盾。"
            " 请显式把 live_trading 改为 true 后再启动实盘。"
        )

    if not cfg.ai.api_key:
        errors.append("未设置 DEEPSEEK_API_KEY（复制 .env.example 为 .env 并填写）")

    if cfg.risk.max_position_pct <= 0 or cfg.risk.max_position_pct > 1:
        errors.append("risk.max_position_pct 必须在 (0, 1] 区间")
    if cfg.risk.max_total_exposure_pct <= 0 or cfg.risk.max_total_exposure_pct > 1:
        errors.append("risk.max_total_exposure_pct 必须在 (0, 1] 区间")

    return errors
