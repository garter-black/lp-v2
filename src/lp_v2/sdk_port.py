"""SDK boundary; implementations must preserve identities and uncertainty."""

from datetime import datetime
from typing import Protocol

from lp_v2.models import (
    Balance, Fill, MergeRequest, MergeResult, Order, OrderRequest, OrderResult, Position,
)


class SDKPort(Protocol):
    def place_order(self, request: OrderRequest, *, at: datetime) -> OrderResult: ...

    def cancel_order(self, order_id: str, *, at: datetime) -> OrderResult: ...

    def read_orders(self) -> tuple[Order, ...]:
        """Authoritative history, including cancelled/filled orders and cumulative fills."""
        ...

    def read_trades(self) -> tuple[Fill, ...]:
        """Authoritative executions, including those whose notification is delayed."""
        ...

    def read_positions(self) -> tuple[Position, ...]: ...

    def read_balance(self) -> Balance: ...

    def merge(self, request: MergeRequest, *, at: datetime) -> MergeResult: ...
