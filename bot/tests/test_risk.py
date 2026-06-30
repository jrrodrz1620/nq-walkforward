from bot.config import RiskConfig
from bot.contracts import get_contract
from bot.risk import RiskManager


def test_approves_within_limits(store):
    rm = RiskManager(RiskConfig(), store)
    d = rm.evaluate(get_contract("ES"), "2025-06", "buy", 1,
                    available_equity=100_000)
    assert d.approved
    assert d.projected_net == 1


def test_position_cap_blocks_third_contract(store):
    rm = RiskManager(RiskConfig(), store)
    store.apply_fill("ES", "2025-06", +2, 5000.0, 50.0)   # already at the cap
    d = rm.evaluate(get_contract("ES"), "2025-06", "buy", 1,
                    available_equity=100_000)
    assert not d.approved
    assert d.code == "position_cap"


def test_position_cap_allows_reducing_trade(store):
    rm = RiskManager(RiskConfig(), store)
    store.apply_fill("ES", "2025-06", +2, 5000.0, 50.0)
    # Selling reduces exposure → allowed even though we were at the cap.
    d = rm.evaluate(get_contract("ES"), "2025-06", "sell", 1,
                    available_equity=100_000)
    assert d.approved


def test_margin_rejection(store):
    rm = RiskManager(RiskConfig(max_margin_utilization=0.5), store)
    # ES needs $13,200; $20k equity * 50% = $10k ceiling → reject.
    d = rm.evaluate(get_contract("ES"), "2025-06", "buy", 1,
                    available_equity=20_000)
    assert not d.approved
    assert d.code == "margin"


def test_daily_drawdown_halts_new_entries(store):
    rm = RiskManager(RiskConfig(daily_loss_limit=1_000.0), store)
    # Book a -$1,200 realized loss today.
    store.add_transaction("ES", "2025-06", "sell", 1, 4976.0, -1_200.0)
    d = rm.evaluate(get_contract("ES"), "2025-06", "buy", 1,
                    available_equity=100_000)
    assert not d.approved
    assert d.code == "daily_drawdown"
    assert d.day_pnl <= -1_000.0


def test_daily_drawdown_uses_mark_to_market(store):
    rm = RiskManager(RiskConfig(daily_loss_limit=1_000.0), store)
    # Long 2 ES @5000, now marked at 4988 → unrealized = -12 * 50 * 2 = -$1,200.
    store.apply_fill("ES", "2025-06", +2, 5000.0, 50.0)
    store.update_mark("ES", "2025-06", 4988.0)
    pnl = rm.marked_to_market_day_pnl()
    assert pnl == -1_200.0
    # A reducing sell is still allowed even while halted...
    assert rm.evaluate(get_contract("ES"), "2025-06", "sell", 1,
                       available_equity=100_000).approved
    # ...but adding exposure on another instrument is blocked.
    d = rm.evaluate(get_contract("NQ"), "2025-06", "buy", 1,
                    available_equity=100_000)
    assert not d.approved
    assert d.code == "daily_drawdown"
