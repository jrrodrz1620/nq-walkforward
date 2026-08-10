"""
SQLite persistence: positions, fills, equity curve, and an event log.

Four tables:

* ``positions``        -- one row per position, updated in place as it closes
* ``position_history`` -- append-only snapshots (OPEN / MARK / CLOSE), so the
                          full life of every position is recoverable
* ``fills``            -- every execution attempt, including rejections
* ``equity``           -- periodic mark-to-market snapshots for the P&L curve
* ``events``           -- signals, alerts, kill-switch trips, errors

Writes are small and local, so they run inline on the event loop; the
connection is guarded by a lock because the paper executor and the settlement
loop can both write. WAL mode keeps a concurrent `sqlite3` reader (e.g. an
analysis notebook) from blocking the bot.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import CLOSED, OPEN, Fill, Position, TradeStats

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id          TEXT    NOT NULL,
    market_label          TEXT    NOT NULL,
    asset                 TEXT    NOT NULL,
    window_minutes        INTEGER NOT NULL,
    side                  TEXT    NOT NULL,
    token_id              TEXT    NOT NULL,
    shares                REAL    NOT NULL,
    entry_price           REAL    NOT NULL,
    entry_fees            REAL    NOT NULL DEFAULT 0,
    entry_time            TEXT    NOT NULL,
    close_time            TEXT,
    strike                REAL,
    spot_at_entry         REAL,
    fair_prob             REAL,
    market_mid            REAL,
    divergence            REAL,
    edge                  REAL,
    confidence            REAL,
    kelly_fraction        REAL,
    sigma                 REAL,
    seconds_left_at_entry REAL,
    status                TEXT    NOT NULL,
    mode                  TEXT    NOT NULL,
    entry_order_id        TEXT,
    exit_order_id         TEXT,
    exit_price            REAL,
    exit_time             TEXT,
    exit_reason           TEXT,
    exit_fees             REAL DEFAULT 0,
    settlement_price      REAL,
    realized_pnl          REAL
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_entry  ON positions(entry_time);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(condition_id);

CREATE TABLE IF NOT EXISTS position_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL,
    at           TEXT    NOT NULL,
    event        TEXT    NOT NULL,
    mark_price   REAL,
    spot         REAL,
    unrealized   REAL,
    realized     REAL,
    note         TEXT,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_history_position ON position_history(position_id);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    at          TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    side        TEXT NOT NULL,
    shares      REAL NOT NULL,
    price       REAL NOT NULL,
    fees        REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    mode        TEXT NOT NULL,
    order_id    TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    equity         REAL NOT NULL,
    cash           REAL NOT NULL,
    open_exposure  REAL NOT NULL,
    unrealized     REAL NOT NULL,
    realized_total REAL NOT NULL,
    daily_drawdown REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    halted         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_equity_at ON equity(at);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL,
    message  TEXT NOT NULL,
    payload  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
"""

_POSITION_COLUMNS = (
    "condition_id", "market_label", "asset", "window_minutes", "side", "token_id",
    "shares", "entry_price", "entry_fees", "entry_time", "close_time", "strike",
    "spot_at_entry", "fair_prob", "market_mid", "divergence", "edge", "confidence",
    "kelly_fraction", "sigma", "seconds_left_at_entry", "status", "mode",
    "entry_order_id", "exit_order_id", "exit_price", "exit_time", "exit_reason",
    "exit_fees", "settlement_price", "realized_pnl",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ─────────────────────────────────────────────
# STORE
# ─────────────────────────────────────────────

class TradeStore:
    """Thread-safe SQLite store for the bot's full trading history."""

    def __init__(self, path: str | Path = "polymarket_arb.sqlite3"):
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # WAL lets an external reader tail the database while we trade.
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as exc:  # pragma: no cover - filesystem dependent
                log.warning("could not enable WAL mode: %s", exc)
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    def __enter__(self) -> "TradeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── positions ────────────────────────────────────────────

    def insert_position(self, position: Position) -> int:
        """Persist a newly opened position and stamp its database id."""
        row = position.to_row()
        columns = ", ".join(_POSITION_COLUMNS)
        placeholders = ", ".join("?" for _ in _POSITION_COLUMNS)
        values = [row.get(c) for c in _POSITION_COLUMNS]
        with self._lock:
            cursor = self.conn.execute(
                f"INSERT INTO positions ({columns}) VALUES ({placeholders})", values
            )
            self.conn.commit()
            position.id = int(cursor.lastrowid or 0)
        self.record_history(
            position, event="OPEN", mark_price=position.entry_price,
            spot=position.spot_at_entry, note=f"edge={position.edge:.4f} conf={position.confidence:.4f}",
        )
        return position.id or 0

    def update_position(self, position: Position) -> None:
        """Write a position's mutable fields back (used on close)."""
        if position.id is None:
            raise ValueError("position has no id; insert it first")
        row = position.to_row()
        assignments = ", ".join(f"{c} = ?" for c in _POSITION_COLUMNS)
        values = [row.get(c) for c in _POSITION_COLUMNS] + [position.id]
        with self._lock:
            self.conn.execute(f"UPDATE positions SET {assignments} WHERE id = ?", values)
            self.conn.commit()

    def close_position(self, position: Position, *, spot: float | None = None) -> None:
        """Persist a closed position and append the terminal history row."""
        self.update_position(position)
        self.record_history(
            position,
            event="CLOSE",
            mark_price=position.exit_price,
            spot=spot if spot is not None else position.settlement_price,
            note=position.exit_reason,
        )

    def record_history(
        self,
        position: Position,
        *,
        event: str,
        mark_price: float | None = None,
        spot: float | None = None,
        note: str = "",
    ) -> None:
        """Append one immutable snapshot to the position's history."""
        if position.id is None:
            return
        unrealized = (
            position.unrealized_pnl(mark_price) if mark_price is not None and position.is_open
            else None
        )
        with self._lock:
            self.conn.execute(
                "INSERT INTO position_history "
                "(position_id, at, event, mark_price, spot, unrealized, realized, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position.id, _now_iso(), event, mark_price, spot,
                    unrealized, position.realized_pnl, note,
                ),
            )
            self.conn.commit()

    def open_positions(self) -> list[Position]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status = ? ORDER BY entry_time", (OPEN,)
            ).fetchall()
        return [Position.from_row(dict(r)) for r in rows]

    def recent_trades(self, limit: int = 10) -> list[Position]:
        """Most recently closed positions, newest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status = ? "
                "ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT ?",
                (CLOSED, limit),
            ).fetchall()
        return [Position.from_row(dict(r)) for r in rows]

    def position_history(self, position_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM position_history WHERE position_id = ? ORDER BY id",
                (position_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── fills ────────────────────────────────────────────────

    def record_fill(self, fill: Fill, position_id: int | None = None) -> int:
        with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO fills "
                "(position_id, at, token_id, side, shares, price, fees, status, mode, order_id, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position_id, fill.at.isoformat(), fill.token_id, fill.side,
                    fill.shares, fill.price, fill.fees, fill.status, fill.mode,
                    fill.order_id, fill.detail,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0)

    # ── equity & events ──────────────────────────────────────

    def record_equity(
        self,
        *,
        equity: float,
        cash: float,
        open_exposure: float,
        unrealized: float,
        realized_total: float,
        daily_drawdown: float,
        open_positions: int,
        halted: bool,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO equity "
                "(at, equity, cash, open_exposure, unrealized, realized_total, "
                " daily_drawdown, open_positions, halted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), equity, cash, open_exposure, unrealized,
                    realized_total, daily_drawdown, open_positions, int(halted),
                ),
            )
            self.conn.commit()

    def record_event(
        self, kind: str, message: str, *, severity: str = "info", payload: Any = None
    ) -> None:
        blob: str | None = None
        if payload is not None:
            try:
                blob = json.dumps(payload, default=str)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                blob = str(payload)
        with self._lock:
            self.conn.execute(
                "INSERT INTO events (at, kind, severity, message, payload) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), kind, severity, message, blob),
            )
            self.conn.commit()

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT at, equity FROM equity ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── analytics ────────────────────────────────────────────

    def realized_pnls(self, *, since: datetime | None = None) -> list[float]:
        query = "SELECT realized_pnl FROM positions WHERE status = ? AND realized_pnl IS NOT NULL"
        params: list[Any] = [CLOSED]
        if since is not None:
            query += " AND COALESCE(exit_time, entry_time) >= ?"
            params.append(since.isoformat())
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY id", params).fetchall()
        return [float(r[0]) for r in rows]

    def stats(self, *, since: datetime | None = None) -> TradeStats:
        return TradeStats.from_pnls(self.realized_pnls(since=since))

    def total_realized(self) -> float:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status = ?",
                (CLOSED,),
            ).fetchone()
        return float(row[0] or 0.0)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM positions GROUP BY status"
            ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def bulk_record_history(self, entries: Iterable[tuple[Position, float, float]]) -> None:
        """Record a MARK snapshot for several positions at once."""
        for position, mark, spot in entries:
            self.record_history(position, event="MARK", mark_price=mark, spot=spot)
