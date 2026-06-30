from bot.state import StateStore


def test_claim_order_is_idempotent(store):
    is_new, rec = store.claim_order("hash:abc", "ES", "2025-06", "buy", 1, 5000.0)
    assert is_new
    assert rec.status == "processing"

    # Rapid-fire identical request: same key → not new, same row.
    is_new2, rec2 = store.claim_order("hash:abc", "ES", "2025-06", "buy", 1, 5000.0)
    assert not is_new2
    assert rec2.id == rec.id


def test_finalize_order(store):
    _, rec = store.claim_order("k1", "ES", "2025-06", "buy", 1, 5000.0)
    out = store.finalize_order(rec.id, "filled", broker_order_id="B1",
                               fill_price=5000.25)
    assert out.status == "filled"
    assert out.broker_order_id == "B1"
    assert out.fill_price == 5000.25


def test_apply_fill_open_and_average(store):
    # Open 1 long @5000, add 1 long @5010 → net 2, avg 5005.
    store.apply_fill("ES", "2025-06", +1, 5000.0, 50.0)
    store.apply_fill("ES", "2025-06", +1, 5010.0, 50.0)
    pos = store.get_position("ES", "2025-06")
    assert pos.net_contracts == 2
    assert pos.avg_price == 5005.0


def test_apply_fill_realizes_pnl_on_close(store):
    store.apply_fill("ES", "2025-06", +2, 5000.0, 50.0)   # long 2 @5000
    realized = store.apply_fill("ES", "2025-06", -1, 5010.0, 50.0)  # sell 1 @5010
    # 10 points * $50 * 1 contract = $500.
    assert realized == 500.0
    pos = store.get_position("ES", "2025-06")
    assert pos.net_contracts == 1
    assert pos.realized_pnl == 500.0


def test_apply_fill_flip_through_flat(store):
    store.apply_fill("ES", "2025-06", +1, 5000.0, 50.0)   # long 1
    realized = store.apply_fill("ES", "2025-06", -2, 5010.0, 50.0)  # sell 2 → short 1
    assert realized == 500.0          # closed the 1 long for +$500
    pos = store.get_position("ES", "2025-06")
    assert pos.net_contracts == -1
    assert pos.avg_price == 5010.0    # new short opened at fill price


def test_realized_pnl_today_sums_transactions(store):
    store.add_transaction("ES", "2025-06", "sell", 1, 5010.0, 500.0)
    store.add_transaction("ES", "2025-06", "sell", 1, 4990.0, -500.0)
    store.add_transaction("NQ", "2025-06", "sell", 1, 18000.0, 250.0)
    assert store.realized_pnl_today() == 250.0


def test_net_contracts_and_open_positions(store):
    store.apply_fill("ES", "2025-06", +2, 5000.0, 50.0)
    store.apply_fill("NQ", "2025-06", -1, 18000.0, 20.0)
    assert store.net_contracts("ES", "2025-06") == 2
    assert store.net_contracts("MNQ", "2025-06") == 0
    assert len(store.open_positions()) == 2


def test_snapshot_shape(store):
    store.apply_fill("ES", "2025-06", +1, 5000.0, 50.0)
    snap = store.snapshot()
    assert "positions" in snap
    assert "realized_pnl_today" in snap
    assert snap["transaction_count"] == 0


def test_file_persistence(tmp_path):
    db = str(tmp_path / "state.db")
    s1 = StateStore(db)
    s1.apply_fill("ES", "2025-06", +1, 5000.0, 50.0)
    s1.claim_order("persist-key", "ES", "2025-06", "buy", 1, 5000.0)
    s1.close()

    s2 = StateStore(db)
    assert s2.net_contracts("ES", "2025-06") == 1
    # Idempotency survives a restart.
    is_new, _ = s2.claim_order("persist-key", "ES", "2025-06", "buy", 1, 5000.0)
    assert not is_new
    s2.close()
