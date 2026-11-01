"""SDK boundary; implementations must preserve identities and uncertainty."""

from datetime import datetime
from typing import Protocol

from lp_v2.models import (
    Balance, Fill, MergeRequest, MergeResult, Order, OrderRequest, OrderResult, Position,
)


class SDKPort(Protocol):
    def place_order(self, request: OrderRequest, *, at: datetime) -> OrderResult: ...

    def cancel_order(self, order_id: str, *, at: datetime) -> OrderResult: ...

    def read_orders(self) -> tuple[Order | OrderResult, ...]:
        """History including terminal/cumulative facts and original request provenance.

        A recovery adapter must retain stable ownership, reservation terms and
        request identities; an active-order list alone cannot prove absence.
        Definitively rejected placements are FAILED OrderResults with no Order.
        Unresolved intents may be UNKNOWN OrderResults. A fresh process needs
        those facts too; an adapter unable to prove completeness must fail,
        rather than certify an empty list as absence of in-flight submissions.
        """
        ...

    def read_trades(self) -> tuple[Fill, ...]:
        """Authoritative executions, including those whose notification is delayed."""
        ...

    def read_positions(self) -> tuple[Position, ...]: ...

    def read_balance(self) -> Balance:
        """Actual cash plus complete identity-bound merge attempt/result history.

        Include pending/UNKNOWN, failed, acknowledged and settled attempts.
        None means unavailable proof and prevents recovery. Only actual receipts
        carry received cash/consumed costs, consistent with trade/position history.
        A real adapter must obtain this proof; cash growth/acks cannot supply it.
        """
        ...

    def merge(self, request: MergeRequest, *, at: datetime) -> MergeResult: ...
