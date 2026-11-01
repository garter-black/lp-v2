"""Pure opposite-leg hedge intent from owned inventory and coverage facts."""

from collections.abc import Sequence
from decimal import Decimal, ROUND_FLOOR

from lp_v2.models import Book, Market, Order, OrderRequest, OrderStatus, Position, Quote


def hedge_quote(
    market: Market,
    held: Position,
    opposite: Position,
    opposite_book: Book,
    *,
    orders: Sequence[Order],
    pending: Sequence[OrderRequest],
    safety_cushion: Decimal,
    remaining_fee_per_pair: Decimal,
) -> Quote | None:
    for value in (
        held.quantity, held.cost_usdc, opposite.quantity, opposite.cost_usdc,
        opposite_book.best_bid, opposite_book.best_ask, market.tick_size, market.min_order_size,
        safety_cushion, remaining_fee_per_pair,
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("hedge amounts must be finite nonnegative Decimals")
    if market.tick_size == 0 or safety_cushion < Decimal("0.02"):
        raise ValueError("tick must be positive and safety cushion at least 0.02")
    tokens = {token.token_id for token in market.tokens}
    if (
        not market.condition_id or len(market.tokens) != 2 or len(tokens) != 2 or "" in tokens
        or not held.owner or held.owner != opposite.owner
        or held.condition_id != market.condition_id or opposite.condition_id != market.condition_id
        or {held.token_id, opposite.token_id} != tokens
        or opposite_book.condition_id != market.condition_id or opposite_book.token_id != opposite.token_id
    ):
        raise ValueError("hedge position, owner, market and book identities must match")
    if not opposite_book.best_bid < opposite_book.best_ask <= 1:
        raise ValueError("hedge book must have bid below ask within [0, 1]")

    def matching(request: OrderRequest) -> bool:
        return (request.owner == held.owner and request.quote.condition_id == market.condition_id
                and request.quote.token_id == opposite.token_id)

    def priority(order: Order):
        # Definitive terminal readback cannot be undone by an uncertain notification.
        terminal = order.authoritative and order.status in (OrderStatus.CANCELLED, OrderStatus.FILLED)
        return terminal, order.observed_at, order.authoritative

    known = {}
    for order in orders:
        if matching(order.request):
            key = order.request.request_id
            if key not in known or priority(order) > priority(known[key]):
                known[key] = order
    covered = {key: order.remaining_quantity if order.status == OrderStatus.LIVE or not order.authoritative else Decimal("0")
               for key, order in known.items()}
    for request in pending:
        if matching(request) and request.request_id not in covered:
            covered[request.request_id] = request.quote.quantity
    for value in covered.values():
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("covered quantities must be finite nonnegative Decimals")
    quantity = max(Decimal("0"), held.quantity - opposite.quantity - sum(covered.values(), Decimal("0")))
    if quantity == 0 or quantity < market.min_order_size:
        return None
    cap = 1 - safety_cushion - held.cost_usdc / held.quantity - remaining_fee_per_pair
    if cap <= 0:
        return None
    price = (min(cap, opposite_book.best_bid) / market.tick_size).to_integral_value(rounding=ROUND_FLOOR) * market.tick_size
    if price <= 0:
        return None
    return Quote(market.condition_id, opposite.token_id, price, quantity)
