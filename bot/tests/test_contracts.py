import pytest

from bot.contracts import (
    CONTRACTS,
    UnknownContractError,
    get_contract,
    normalize_symbol,
)


def test_known_multipliers_match_spec():
    assert get_contract("ES").multiplier == 50.0
    assert get_contract("MES").multiplier == 5.0
    assert get_contract("NQ").multiplier == 20.0
    assert get_contract("MNQ").multiplier == 2.0


def test_tick_value_is_multiplier_times_tick_size():
    es = get_contract("ES")
    assert es.tick_size == 0.25
    assert es.tick_value == pytest.approx(12.50)        # 50 * 0.25
    assert get_contract("MES").tick_value == pytest.approx(1.25)


@pytest.mark.parametrize("raw,root", [
    ("ES", "ES"),
    ("es", "ES"),
    ("ESM2024", "ES"),
    ("MNQ", "MNQ"),
    ("MNQM2025", "MNQ"),
    ("NQ-Z25", "NQ"),
    ("MES!", "MES"),
])
def test_normalize_symbol(raw, root):
    assert normalize_symbol(raw) == root


def test_unknown_symbol_raises():
    with pytest.raises(UnknownContractError):
        get_contract("ZZZ")
    with pytest.raises(UnknownContractError):
        normalize_symbol("")


def test_all_specs_have_positive_margin():
    for spec in CONTRACTS.values():
        assert spec.initial_margin > 0
        assert spec.multiplier > 0
        assert spec.tick_size > 0
