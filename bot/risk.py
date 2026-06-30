"""
Risk management safeguards  (validation gate 2 & 3).

Three hard gates, evaluated *before* an order is routed:

1. **Margin** — initial margin must fit under a safe utilization of equity.
2. **Position cap** — never exceed ``max_contracts_per_instrument`` (net abs).
3. **Daily drawdown** — halt new entries once marked-to-market day PnL <= -limit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import RiskConfig
from .contracts import ContractSpec
from .pricing import PricingEngine
from .state import StateStore


@dataclass
class RiskDecision:
    approved: bool
    reason: Optional[str] = None
    code: Optional[str] = None        # margin | position_cap | daily_drawdown
    day_pnl: Optional[float] = None
    projected_net: Optional[int] = None


class RiskManager:
    def __init__(self, config: RiskConfig, store: StateStore):
        self.config = config
        self.store = store

    def marked_to_market_day_pnl(self) -> float:
        """Realized PnL today + unrealized PnL of open positions at last mark."""
        realized = self.store.realized_pnl_today()
        unrealized = 0.0
        for pos in self.store.open_positions():
            try:
                spec = _spec_for(pos.symbol)
            except Exception:
                continue
            unrealized += (
                (pos.last_mark - pos.avg_price)
                * pos.net_contracts
                * spec.multiplier
            )
        return realized + unrealized

    def evaluate(self, spec: ContractSpec, contract_month: str, action: str,
                 quantity: int, available_equity: float) -> RiskDecision:
        signed = quantity if action == "buy" else -quantity
        current_net = self.store.net_contracts(spec.symbol, contract_month)
        projected_net = current_net + signed

        pricing = PricingEngine(spec)

        # 1) Daily drawdown halt — block any *new* exposure once breached.
        day_pnl = self.marked_to_market_day_pnl()
        increasing = abs(projected_net) > abs(current_net)
        if day_pnl <= -self.config.daily_loss_limit and increasing:
            return RiskDecision(
                approved=False,
                code="daily_drawdown",
                day_pnl=day_pnl,
                projected_net=projected_net,
                reason=(
                    f"daily loss limit hit: marked-to-market PnL ${day_pnl:,.2f} "
                    f"<= -${self.config.daily_loss_limit:,.2f}; new entries halted"
                ),
            )

        # 2) Position cap — absolute net contracts per instrument.
        if abs(projected_net) > self.config.max_contracts_per_instrument:
            return RiskDecision(
                approved=False,
                code="position_cap",
                day_pnl=day_pnl,
                projected_net=projected_net,
                reason=(
                    f"position cap: {action} {quantity} would make net "
                    f"{projected_net} {spec.symbol}, exceeds max "
                    f"{self.config.max_contracts_per_instrument}"
                ),
            )

        # 3) Margin — only when the trade increases exposure.
        if increasing:
            added = abs(projected_net) - abs(current_net)
            check = pricing.check_margin(
                added, available_equity, self.config.max_margin_utilization
            )
            if not check.ok:
                return RiskDecision(
                    approved=False,
                    code="margin",
                    day_pnl=day_pnl,
                    projected_net=projected_net,
                    reason=check.reason,
                )

        return RiskDecision(
            approved=True,
            day_pnl=day_pnl,
            projected_net=projected_net,
        )


def _spec_for(symbol: str) -> ContractSpec:
    # Local import to avoid a hard cycle; contracts has no deps on risk.
    from .contracts import get_contract
    return get_contract(symbol)
