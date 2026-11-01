"""Construct one post-only BUY pair from explicit current book facts."""

# Join-bid/tick reference: warproxxx/poly-maker, quoting.py (MIT; THIRD_PARTY_NOTICES.md).
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from lp_v2.models import Book, Market, Quote


def quote_pair(
    market: Market,
    yes_book: Book,
    no_book: Book,
    *,
    planned_quantity: Decimal,
    fee_per_pair: Decimal,
    safety_cushion: Decimal,
) -> tuple[Quote, Quote] | None:
    try:
        spread_cents = Decimal(str(market.rewards_max_spread))
    except InvalidOperation:
        raise ValueError("reward spread must be finite and nonnegative") from None
    for value in (
        planned_quantity, fee_per_pair, safety_cushion, market.tick_size,
        market.min_order_size, market.rewards_min_size, spread_cents,
        yes_book.best_bid, yes_book.best_ask, no_book.best_bid, no_book.best_ask,
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("monetary inputs must be finite nonnegative Decimals")
    if market.tick_size == 0 or safety_cushion < Decimal("0.02"):
        raise ValueError("tick must be positive and safety cushion at least 0.02")
    if (
        not market.condition_id or len(market.tokens) != 2
        or not all(token.token_id for token in market.tokens)
        or market.tokens[0].token_id == market.tokens[1].token_id
        or yes_book.token_id != market.tokens[0].token_id
        or no_book.token_id != market.tokens[1].token_id
        or yes_book.condition_id != market.condition_id
        or no_book.condition_id != market.condition_id
    ):
        raise ValueError("book and binary market identities must match")
    for book in (yes_book, no_book):
        if not book.best_bid < book.best_ask <= 1:
            raise ValueError("books must have bid below ask within [0, 1]")
    if planned_quantity <= 0 or planned_quantity < max(market.min_order_size, market.rewards_min_size):
        return None
    tick = market.tick_size
    band = spread_cents / 100
    lower, upper = [], []
    for book in (yes_book, no_book):
        midpoint = (book.best_bid + book.best_ask) / 2
        lower.append(max(Decimal("1"), ((midpoint - band) / tick).to_integral_value(rounding=ROUND_CEILING)))
        upper.append((book.best_bid / tick).to_integral_value(rounding=ROUND_FLOOR))
    total_ticks = ((1 - safety_cushion - fee_per_pair) / tick).to_integral_value(rounding=ROUND_FLOOR)
    # Reserve the NO minimum before choosing the highest feasible YES bid.
    yes_ticks = min(upper[0], total_ticks - lower[1])
    no_ticks = min(upper[1], total_ticks - yes_ticks)
    if yes_ticks < lower[0] or no_ticks < lower[1]:
        return None
    return (
        Quote(market.condition_id, yes_book.token_id, yes_ticks * tick, planned_quantity),
        Quote(market.condition_id, no_book.token_id, no_ticks * tick, planned_quantity),
    )
