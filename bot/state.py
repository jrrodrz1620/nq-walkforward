"""
SQLite-backed state + idempotency store  (validation gate 4).

Tracks open positions, contract expirations, order states and transaction
history, and natively de-duplicates identical rapid-fire webhooks via a UNIQUE
constraint on the idempotency key (claimed atomically before any order routes).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return date.today().isoformat()


@dataclass
class Position:
    symbol: str
    contract_month: str
    net_contracts: int          # signed: + long, - short
    avg_price: float
    last_mark: float
    realized_pnl: float
    updated_at: str

    @property
    def direction(self) -> int:
        if self.net_contracts == 0:
            return 0
        return 1 if self.net_contracts > 0 else -1


@dataclass
class OrderRecord:
    id: int
    idempotency_key: str
    symbol: str
    contract_month: str
    action: str
    quantity: int
    price: float
    status: str                 # processing | filled | rejected | failed | duplicate
    broker_order_id: Optional[str]
    fill_price: Optional[float]
    reason: Optional[str]
    created_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    symbol          TEXT NOT NULL,
    contract_month  TEXT NOT NULL,
    action          TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price           REAL NOT NULL,
    status          TEXT NOT NULL,
    broker_order_id TEXT,
    fill_price      REAL,
    reason          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol          TEXT NOT NULL,
    contract_month  TEXT NOT NULL,
    net_contracts   INTEGER NOT NULL DEFAULT 0,
    avg_price       REAL NOT NULL DEFAULT 0,
    last_mark       REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (symbol, contract_month)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    contract_month  TEXT NOT NULL,
    action          TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price           REAL NOT NULL,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    trade_date      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(trade_date);
"""


class StateStore:
    """Thread-safe SQLite wrapper. A single shared connection + re-entrant lock
    keeps this usable from FastAPI's threadpool and from in-memory tests."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── idempotency ─────────────────────────────────────────────────
    def claim_order(self, key: str, symbol: str, contract_month: str,
                    action: str, quantity: int, price: float,
                    dedup_window_seconds: float = 0.0) -> tuple[bool, OrderRecord]:
        """Atomically claim an idempotency key.

        Returns ``(is_new, record)``. ``is_new`` is ``True`` only for the first
        caller of a given key (status ``processing``); concurrent/duplicate
        callers get ``False`` plus the already-stored record.

        ``dedup_window_seconds`` only affects *new* keys: it is reserved for
        future time-bounded dedup; the UNIQUE constraint already guarantees a
        given key never executes twice.
        """
        now = _utc_now_iso()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO orders
                       (idempotency_key, symbol, contract_month, action,
                        quantity, price, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?, 'processing', ?, ?)""",
                    (key, symbol, contract_month, action, quantity, price, now, now),
                )
                rec = self._get_order_by_id(cur.lastrowid)
                return True, rec
            except sqlite3.IntegrityError:
                rec = self._get_order_by_key(key)
                assert rec is not None  # UNIQUE violation => row exists
                return False, rec

    def finalize_order(self, order_id: int, status: str,
                       broker_order_id: Optional[str] = None,
                       fill_price: Optional[float] = None,
                       reason: Optional[str] = None) -> OrderRecord:
        with self._lock:
            self._conn.execute(
                """UPDATE orders
                   SET status=?, broker_order_id=?, fill_price=?, reason=?,
                       updated_at=?
                   WHERE id=?""",
                (status, broker_order_id, fill_price, reason, _utc_now_iso(), order_id),
            )
            return self._get_order_by_id(order_id)

    def get_order(self, order_id: int) -> Optional[OrderRecord]:
        with self._lock:
            return self._get_order_by_id(order_id)

    def _get_order_by_id(self, order_id: int) -> Optional[OrderRecord]:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        return self._row_to_order(row)

    def _get_order_by_key(self, key: str) -> Optional[OrderRecord]:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._row_to_order(row)

    @staticmethod
    def _row_to_order(row: Optional[sqlite3.Row]) -> Optional[OrderRecord]:
        if row is None:
            return None
        return OrderRecord(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            symbol=row["symbol"],
            contract_month=row["contract_month"],
            action=row["action"],
            quantity=row["quantity"],
            price=row["price"],
            status=row["status"],
            broker_order_id=row["broker_order_id"],
            fill_price=row["fill_price"],
            reason=row["reason"],
            created_at=row["created_at"],
        )

    # ── positions ───────────────────────────────────────────────────
    def get_position(self, symbol: str, contract_month: str) -> Optional[Position]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND contract_month=?",
                (symbol, contract_month),
            ).fetchone()
            return self._row_to_position(row)

    def net_contracts(self, symbol: str, contract_month: str) -> int:
        pos = self.get_position(symbol, contract_month)
        return pos.net_contracts if pos else 0

    def update_mark(self, symbol: str, contract_month: str, mark: float) -> None:
        with self._lock:
            existing = self.get_position(symbol, contract_month)
            now = _utc_now_iso()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO positions
                       (symbol, contract_month, net_contracts, avg_price,
                        last_mark, realized_pnl, updated_at)
                       VALUES (?,?,0,0,?,0,?)""",
                    (symbol, contract_month, mark, now),
                )
            else:
                self._conn.execute(
                    "UPDATE positions SET last_mark=?, updated_at=? "
                    "WHERE symbol=? AND contract_month=?",
                    (mark, now, symbol, contract_month),
                )

    def apply_fill(self, symbol: str, contract_month: str, signed_qty: int,
                   price: float, multiplier: float) -> float:
        """Apply a fill to the running position and return realized PnL for it.

        Handles opening, adding, reducing and flipping. Realized PnL is booked
        on the portion of the fill that *reduces* an existing position.
        """
        with self._lock:
            pos = self.get_position(symbol, contract_month)
            now = _utc_now_iso()
            if pos is None:
                old_net, old_avg, old_real = 0, 0.0, 0.0
                last_mark = price
            else:
                old_net, old_avg, old_real = (
                    pos.net_contracts, pos.avg_price, pos.realized_pnl
                )
                last_mark = pos.last_mark or price

            realized = 0.0
            new_net = old_net + signed_qty

            same_side = (old_net == 0) or (old_net > 0) == (signed_qty > 0)
            if same_side:
                # Adding to (or opening) the position: weighted-average price.
                total_abs = abs(old_net) + abs(signed_qty)
                new_avg = (
                    (abs(old_net) * old_avg + abs(signed_qty) * price) / total_abs
                    if total_abs else 0.0
                )
            else:
                # Reducing / closing / flipping.
                closing_qty = min(abs(signed_qty), abs(old_net))
                direction = 1 if old_net > 0 else -1
                realized = (price - old_avg) * direction * closing_qty * multiplier
                if abs(signed_qty) <= abs(old_net):
                    new_avg = old_avg if new_net != 0 else 0.0
                else:
                    # Flipped past flat: remainder opens new side at fill price.
                    new_avg = price

            self._conn.execute(
                """INSERT INTO positions
                   (symbol, contract_month, net_contracts, avg_price, last_mark,
                    realized_pnl, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, contract_month) DO UPDATE SET
                     net_contracts=excluded.net_contracts,
                     avg_price=excluded.avg_price,
                     last_mark=excluded.last_mark,
                     realized_pnl=excluded.realized_pnl,
                     updated_at=excluded.updated_at""",
                (symbol, contract_month, new_net, new_avg, last_mark,
                 old_real + realized, now),
            )
            return realized

    def open_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE net_contracts != 0"
            ).fetchall()
            return [self._row_to_position(r) for r in rows]

    def all_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
            return [self._row_to_position(r) for r in rows]

    @staticmethod
    def _row_to_position(row: Optional[sqlite3.Row]) -> Optional[Position]:
        if row is None:
            return None
        return Position(
            symbol=row["symbol"],
            contract_month=row["contract_month"],
            net_contracts=row["net_contracts"],
            avg_price=row["avg_price"],
            last_mark=row["last_mark"],
            realized_pnl=row["realized_pnl"],
            updated_at=row["updated_at"],
        )

    # ── transactions / pnl ──────────────────────────────────────────
    def add_transaction(self, symbol: str, contract_month: str, action: str,
                        quantity: int, price: float, realized_pnl: float,
                        trade_date: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO transactions
                   (symbol, contract_month, action, quantity, price,
                    realized_pnl, trade_date, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (symbol, contract_month, action, quantity, price, realized_pnl,
                 trade_date or _today_str(), _utc_now_iso()),
            )

    def realized_pnl_today(self, trade_date: Optional[str] = None) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) AS s "
                "FROM transactions WHERE trade_date=?",
                (trade_date or _today_str(),),
            ).fetchone()
            return float(row["s"])

    def transactions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transactions ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def snapshot(self) -> dict:
        """Full serialisable state snapshot (used by the error logger)."""
        with self._lock:
            return {
                "captured_at": _utc_now_iso(),
                "positions": [vars(p) for p in self.all_positions()],
                "open_orders": [
                    vars(self._row_to_order(r)) for r in self._conn.execute(
                        "SELECT * FROM orders WHERE status='processing'"
                    ).fetchall()
                ],
                "realized_pnl_today": self.realized_pnl_today(),
                "transaction_count": len(self.transactions()),
            }
