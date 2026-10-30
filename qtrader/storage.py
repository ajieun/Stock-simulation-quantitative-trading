"""本地持久化：SQLite 记录决策 / 成交 / 净值曲线 / KV 缓存。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    name          TEXT,
    action        TEXT    NOT NULL,
    confidence    REAL,
    source        TEXT    NOT NULL,       -- ai | rule
    price         REAL,
    technical_score REAL,
    payload       TEXT,                   -- 完整 JSON 快照
    executed      INTEGER DEFAULT 0,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    order_id    TEXT,
    symbol      TEXT NOT NULL,
    name        TEXT,
    side        TEXT NOT NULL,            -- BUY | SELL
    price       REAL NOT NULL,
    qty         INTEGER NOT NULL,
    amount      REAL NOT NULL,
    fee         REAL DEFAULT 0,
    status      TEXT NOT NULL,            -- FILLED | REJECTED | PENDING
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS equity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    total       REAL NOT NULL,
    cash        REAL NOT NULL,
    market_value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS ai_usage (
    day         TEXT PRIMARY KEY,
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    calls       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Storage:
    """线程安全的轻量存储层。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Web 控制台与引擎线程会同时读写同一个库，需要 WAL + 忙等待避免 database is locked
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
        except sqlite3.Error:
            pass
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()

    # ------------------------- 结构迁移 -------------------------
    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS 不会修改已存在的旧表，这里做版本迁移。

        v2：equity 表增加自增主键 id。旧版以 ts（秒级）为主键，
            同一秒内的多次写入会互相覆盖，导致净值曲线和日初净值失真。
        """
        version = int(self.get_kv("schema_version", 1) or 1)
        if version >= SCHEMA_VERSION:
            return

        if version < 2:
            cols = {r["name"] for r in self._query("PRAGMA table_info(equity)")}
            if cols and "id" not in cols:
                self._conn.executescript(
                    """
                    ALTER TABLE equity RENAME TO equity_v1;
                    CREATE TABLE equity (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        total REAL NOT NULL,
                        cash REAL NOT NULL,
                        market_value REAL NOT NULL
                    );
                    INSERT INTO equity(ts, total, cash, market_value)
                        SELECT ts, total, cash, market_value FROM equity_v1 ORDER BY ts;
                    DROP TABLE equity_v1;
                    CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
                    """
                )
                self._conn.commit()
                log.info("数据库已迁移到 v2（净值表增加自增主键）")

        self.set_kv("schema_version", SCHEMA_VERSION)

    # ------------------------- 通用 -------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------- KV -------------------------
    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT v FROM kv WHERE k = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["v"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_kv(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ------------------------- 决策 -------------------------
    def record_decision(
        self,
        *,
        symbol: str,
        name: str,
        action: str,
        confidence: float,
        source: str,
        price: float,
        technical_score: float | None = None,
        payload: dict | None = None,
        executed: bool = False,
        reject_reason: str = "",
    ) -> None:
        self._exec(
            """INSERT INTO decisions
               (ts, symbol, name, action, confidence, source, price,
                technical_score, payload, executed, reject_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                symbol,
                name,
                action,
                float(confidence or 0.0),
                source,
                float(price or 0.0),
                technical_score,
                json.dumps(payload or {}, ensure_ascii=False),
                int(executed),
                reject_reason,
            ),
        )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        rows = self._query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def last_ai_decision(self, symbol: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM decisions WHERE symbol = ? AND source = 'ai' ORDER BY id DESC LIMIT 1",
            (symbol,),
        )
        return dict(rows[0]) if rows else None

    # ------------------------- 订单 -------------------------
    def record_order(
        self,
        *,
        symbol: str,
        name: str,
        side: str,
        price: float,
        qty: int,
        amount: float,
        fee: float,
        status: str,
        message: str = "",
        order_id: str = "",
    ) -> None:
        self._exec(
            """INSERT INTO orders
               (ts, order_id, symbol, name, side, price, qty, amount, fee, status, message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                order_id,
                symbol,
                name,
                side,
                float(price),
                int(qty),
                float(amount),
                float(fee),
                status,
                message,
            ),
        )

    def recent_orders(self, limit: int = 50) -> list[dict]:
        rows = self._query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get_order(self, order_id: int) -> dict | None:
        rows = self._query("SELECT * FROM orders WHERE id = ?", (int(order_id),))
        return dict(rows[0]) if rows else None

    def pending_manual_orders(self) -> list[dict]:
        rows = self._query(
            "SELECT * FROM orders WHERE status = 'PENDING_MANUAL' ORDER BY id DESC"
        )
        return [dict(r) for r in rows]

    def has_pending_order(self, symbol: str, side: str) -> bool:
        """同一标的方向是否已有待人工执行的委托，避免盘中反复提示。"""
        rows = self._query(
            "SELECT 1 FROM orders WHERE status = 'PENDING_MANUAL' "
            "AND symbol = ? AND side = ? LIMIT 1",
            (symbol, side),
        )
        return bool(rows)

    def update_order(
        self, order_id: int, *, status: str, message: str | None = None
    ) -> None:
        if message is None:
            self._exec("UPDATE orders SET status = ? WHERE id = ?", (status, int(order_id)))
        else:
            self._exec(
                "UPDATE orders SET status = ?, message = ? WHERE id = ?",
                (status, message, int(order_id)),
            )

    def count_orders_today(self) -> int:
        today = date.today().isoformat()
        rows = self._query(
            "SELECT COUNT(*) AS c FROM orders WHERE substr(ts, 1, 10) = ? AND status = 'FILLED'",
            (today,),
        )
        return int(rows[0]["c"]) if rows else 0

    # ------------------------- 净值 -------------------------
    def record_equity(self, total: float, cash: float, market_value: float) -> None:
        self._exec(
            "INSERT INTO equity(ts, total, cash, market_value) VALUES(?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                float(total),
                float(cash),
                float(market_value),
            ),
        )

    def latest_equity(self) -> dict | None:
        rows = self._query("SELECT * FROM equity ORDER BY id DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def peak_equity(self) -> float:
        rows = self._query("SELECT MAX(total) AS m FROM equity")
        return float(rows[0]["m"] or 0.0) if rows else 0.0

    def day_start_equity(self, current_total: float | None = None) -> float | None:
        """当日基准净值。

        不能用「当天第一条净值记录」推算：同一秒内的多次写入会互相覆盖，
        盘中首轮记录很可能就是已经缩水后的值，导致日亏损熔断失效。
        这里在 KV 里显式记录当日首个观测值。
        """
        today = date.today().isoformat()
        state = self.get_kv("day_start_equity") or {}
        if state.get("date") == today:
            value = float(state.get("total") or 0.0)
            return value if value > 0 else None

        # 进程在盘中重启时，回退到当天最早的一条净值记录
        rows = self._query(
            "SELECT total FROM equity WHERE substr(ts, 1, 10) = ? ORDER BY id ASC LIMIT 1",
            (today,),
        )
        if rows:
            value = float(rows[0]["total"])
            self.set_kv("day_start_equity", {"date": today, "total": value})
            return value

        if current_total is None or current_total <= 0:
            return None
        self.set_kv("day_start_equity", {"date": today, "total": float(current_total)})
        return float(current_total)

    def equity_curve(self, limit: int = 500) -> list[dict]:
        rows = self._query("SELECT * FROM equity ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    # ------------------------- AI 用量 -------------------------
    def add_ai_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        today = date.today().isoformat()
        self._exec(
            """INSERT INTO ai_usage(day, prompt_tokens, completion_tokens, calls)
               VALUES(?,?,?,1)
               ON CONFLICT(day) DO UPDATE SET
                 prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                 completion_tokens = completion_tokens + excluded.completion_tokens,
                 calls = calls + 1""",
            (today, int(prompt_tokens), int(completion_tokens)),
        )

    def ai_usage_today(self) -> dict:
        today = date.today().isoformat()
        rows = self._query("SELECT * FROM ai_usage WHERE day = ?", (today,))
        if not rows:
            return {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        return dict(rows[0])
