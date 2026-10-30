"""DeepSeek 决策客户端。

DeepSeek 提供 OpenAI 兼容协议，这里直接用 requests 调用 /chat/completions，
避免额外 SDK 依赖，同时便于控制超时、重试和 token 预算。
API 文档：https://api-docs.deepseek.com/
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from ..config import AIConfig
from ..storage import Storage

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个专注中国 A 股市场的量化交易决策助手，服务于一套半自动交易系统。

你的职责：根据给定的实时行情快照和技术指标，判断未来 1-5 个交易日的价格倾向，并给出可执行的交易建议。

必须遵守的 A 股规则：
1. T+1 交收：当日买入的股票当日不能卖出。
2. 涨跌停限制：主板 ±10%，创业板/科创板 ±20%，北交所 ±30%，ST 股 ±5%。接近涨停时买入风险极高。
3. 交易时间 9:30-11:30、13:00-15:00，建议需在当日或次日可执行。
4. 交易成本约万分之三（含印花税），过低的目标收益无意义。

分析原则：
- 以趋势和量价关系为主，指标只是辅助，不要机械套用单一指标。
- 明确识别风险：放量下跌、跌破关键均线、指标顶背离、高位滞涨。
- 数据不足或信号矛盾时，必须给出 HOLD 并说明原因，不要为了给出建议而编造观点。
- 置信度要保守：信号模糊时 confidence 不应高于 0.6。

输出要求：只输出一个 JSON 对象，不要输出 markdown 代码块、不要输出任何解释性文字。
JSON 结构：
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0 到 1.0 之间的数字,
  "position_pct": 0.0 到 1.0 之间的数字（占账户总资产比例，BUY 时给出，否则为 0）,
  "stop_loss": 数字（止损价，BUY 时必填，否则为 0）,
  "take_profit": 数字（止盈价，BUY 时必填，否则为 0）,
  "thesis": "不超过 120 字的核心逻辑",
  "risks": ["风险点1", "风险点2"],
  "invalid_if": "什么情况下该判断失效"
}"""


@dataclass
class Decision:
    action: str = "HOLD"
    confidence: float = 0.0
    position_pct: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    thesis: str = ""
    risks: list[str] = field(default_factory=list)
    invalid_if: str = ""
    raw: str = ""
    symbol: str = ""
    name: str = ""
    price: float = 0.0
    source: str = "ai"

    @property
    def is_buy(self) -> bool:
        return self.action == "BUY"

    @property
    def is_sell(self) -> bool:
        return self.action == "SELL"

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "position_pct": self.position_pct,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "thesis": self.thesis,
            "risks": self.risks,
            "invalid_if": self.invalid_if,
        }


class AIError(RuntimeError):
    """AI 调用失败。"""


class BudgetExceeded(AIError):
    """当日 token 预算耗尽。"""


class DeepSeekClient:
    def __init__(self, cfg: AIConfig, store: Storage):
        self.cfg = cfg
        self.store = store
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            }
        )

    @property
    def endpoint(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    # --------------------------- 预算控制 ---------------------------
    def budget_remaining(self) -> int:
        usage = self.store.ai_usage_today()
        used = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        return max(0, self.cfg.daily_token_budget - used)

    # --------------------------- 核心调用 ---------------------------
    def chat(self, system: str, user: str, *, json_mode: bool = True) -> tuple[str, dict]:
        if not self.cfg.api_key:
            raise AIError("未配置 DEEPSEEK_API_KEY")
        if self.budget_remaining() <= 500:
            raise BudgetExceeded("当日 token 预算已耗尽")

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(max(1, self.cfg.max_retries)):
            try:
                resp = self.session.post(
                    self.endpoint, json=payload, timeout=self.cfg.timeout_seconds
                )
                if resp.status_code == 401:
                    raise AIError("DeepSeek 鉴权失败（401），请检查 DEEPSEEK_API_KEY")
                if resp.status_code == 402:
                    raise AIError("DeepSeek 账户余额不足（402）")
                if resp.status_code == 429:
                    raise AIError("DeepSeek 触发限流（429）")
                if resp.status_code >= 500:
                    raise AIError(f"DeepSeek 服务端错误 {resp.status_code}")

                resp.raise_for_status()
                data = resp.json()

                usage = data.get("usage") or {}
                self.store.add_ai_usage(
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                )
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return content or "", usage

            except AIError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                wait = min(2 ** attempt * 1.5, 10)
                log.warning(
                    "DeepSeek 调用失败（第 %d/%d 次）：%s，%.1fs 后重试",
                    attempt + 1, self.cfg.max_retries, exc, wait,
                )
                time.sleep(wait)

        raise AIError(f"DeepSeek 调用失败：{last_err}")

    # --------------------------- 交易决策 ---------------------------
    def decide_trade(self, context: dict) -> Decision:
        """输入结构化上下文，返回校验后的决策。"""
        user = (
            "以下是当前市场的实时快照，请给出交易决策。\n\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )

        content, _usage = self.chat(SYSTEM_PROMPT, user)
        parsed = extract_json(content)
        if parsed is None:
            raise AIError(f"AI 返回内容无法解析为 JSON：{content[:200]}")

        decision = Decision(
            action=str(parsed.get("action", "HOLD")).strip().upper(),
            thesis=str(parsed.get("thesis", ""))[:400],
            risks=[str(r)[:120] for r in (parsed.get("risks") or [])][:6],
            invalid_if=str(parsed.get("invalid_if", ""))[:200],
            raw=content,
            symbol=context.get("symbol", ""),
            name=context.get("name", ""),
            price=float(context.get("quote", {}).get("price") or 0.0),
        )

        if decision.action not in ("BUY", "SELL", "HOLD"):
            log.warning("AI 返回了非法 action=%r，降级为 HOLD", decision.action)
            decision.action = "HOLD"

        decision.confidence = clamp(parsed.get("confidence"), 0.0, 1.0, default=0.0)
        decision.position_pct = clamp(parsed.get("position_pct"), 0.0, 1.0, default=0.0)
        decision.stop_loss = positive_float(parsed.get("stop_loss"))
        decision.take_profit = positive_float(parsed.get("take_profit"))

        # 止损止盈价格合法性检查
        price = decision.price
        if decision.action == "BUY" and price > 0:
            if not (0 < decision.stop_loss < price):
                decision.stop_loss = round(price * 0.93, 2)
            if not (decision.take_profit > price):
                decision.take_profit = round(price * 1.15, 2)

        return decision

    # --------------------------- 连通性自检 ---------------------------
    def ping(self) -> str:
        content, usage = self.chat(
            "你是一个测试端点，只输出 JSON。",
            '请输出 {"ok": true}',
            json_mode=True,
        )
        parsed = extract_json(content) or {}
        total = usage.get("total_tokens", "?")
        return f"模型 {self.cfg.model} 响应正常，返回 {parsed}，消耗 tokens≈{total}"


# ----------------------------- 解析工具 -----------------------------

def clamp(value: Any, low: float, high: float, *, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(low, min(high, v))


def positive_float(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def extract_json(text: str) -> dict | None:
    """从可能带 markdown 包裹的文本里抽出第一个完整 JSON 对象。"""
    if not text:
        return None

    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 花括号配平扫描，跳过字符串字面量里的括号
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cleaned[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    return None
