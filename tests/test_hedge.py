from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lp_v2.hedge import hedge_quote
from lp_v2.models import (
    Balance, Book, Market, Order, OrderRequest, OrderStatus, Outcome, Position, Quote, Token,
)
from tests.fakes.fake_sdk import FakeSDK


AT = datetime(2026, 11, 2, tzinfo=UTC)


def market():
    return Market(
        condition_id="market-1", tokens=(Token("yes", "Yes"), Token("no", "No")),
        tick_size=Decimal("0.01"), min_order_size=Decimal("1"), rewards_min_size=Decimal("10"),
        best_bid=0.70, best_ask=0.80, liquidity_num=2000.0, volume_24hr=10000.0,
        rewards_daily_rate=100.0, rewards_max_spread=5.0, rebate_rate=0.25,
        taker_fee_bps=400, fees_enabled=True, rewards_enabled=True, political=True,
        in_play=False, accepting_orders=True,
    )


def request(request_id, quantity, *, owner="lp-v2:run-1", condition_id="market-1", token_id="no"):
    return OrderRequest(request_id, owner, Quote(condition_id, token_id, Decimal("0.56"), Decimal(quantity)),
                        AT - timedelta(hours=2), AT - timedelta(hours=1))


def live_order(intent):
    return Order("order-" + intent.request_id, intent, Decimal("0"), intent.quote.quantity,
                 OrderStatus.LIVE, AT - timedelta(minutes=90), True)


def test_hedge_uses_fee_inclusive_fill_cost():
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    intent = OrderRequest("yes-fill", "lp-v2:run-1", Quote("market-1", "yes", Decimal("0.40"), Decimal("10")),
                          AT, AT + timedelta(hours=1))
    fake.script_place(intent, Outcome.SUCCESS)
    order = fake.place_order(intent, at=AT).order
    fake.execute_fill(order.order_id, trade_id="fee-inclusive", quantity=Decimal("10"),
                      price=Decimal("0.40"), fee_usdc=Decimal("0.10"), at=AT + timedelta(seconds=1))
    held = next(position for position in fake.read_positions() if position.token_id == "yes")
    assert held.quantity == Decimal("10") and held.cost_usdc == Decimal("4.10")
    opposite = Position("lp-v2:run-1", "market-1", "no", Decimal("0"), Decimal("0"), AT)
    book = Book("market-1", "no", Decimal("0.60"), Decimal("0.61"), AT + timedelta(seconds=2))

    quote = hedge_quote(market(), held, opposite, book, orders=fake.read_orders(), pending=(),
                        safety_cushion=Decimal("0.02"), remaining_fee_per_pair=Decimal("0.01"))

    assert quote == Quote("market-1", "no", Decimal("0.56"), Decimal("10"))
    assert quote.side == "BUY" and quote.post_only is True


@pytest.mark.parametrize("pending_quantity,expected", [("2", "5"), ("12", None)])
def test_existing_and_pending_reduce_quantity_once(pending_quantity, expected):
    held = Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), AT)
    opposite = Position("lp-v2:run-1", "market-1", "no", Decimal("0"), Decimal("0"), AT)
    book = Book("market-1", "no", Decimal("0.60"), Decimal("0.61"), AT)
    existing = live_order(request("covered", "3"))
    old = live_order(request("old-lp", "20", owner="old-lp"))
    other_market = live_order(request("other-market", "20", condition_id="other-market"))
    held_side = live_order(request("held-side", "20", token_id="yes"))
    distinct = request("pending", pending_quantity)

    quote = hedge_quote(
        market(), held, opposite, book,
        orders=(old, other_market, held_side, existing, existing),
        pending=(old.request, other_market.request, held_side.request, existing.request, distinct, distinct),
        safety_cushion=Decimal("0.02"), remaining_fee_per_pair=Decimal("0.01"),
    )

    if expected is None:
        assert quote is None
    else:
        assert quote == Quote("market-1", "no", Decimal("0.56"), Decimal("5"))
        assert quote.side == "BUY" and quote.post_only is True


def test_partial_pair_and_unknown_coverage():
    held = Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), AT)
    opposite = Position("lp-v2:run-1", "market-1", "no", Decimal("4"), Decimal("2.00"), AT)
    book = Book("market-1", "no", Decimal("0.60"), Decimal("0.61"), AT)
    unknown = replace(live_order(request("uncertain", "2")), authoritative=False)
    pending = (unknown.request, request("pending-one", "1"))
    assert unknown.request.expires_at < book.observed_at

    quote = hedge_quote(market(), held, opposite, book, orders=(unknown,), pending=pending,
                        safety_cushion=Decimal("0.02"), remaining_fee_per_pair=Decimal("0.01"))

    assert quote == Quote("market-1", "no", Decimal("0.56"), Decimal("3"))
    assert quote.quantity < market().rewards_min_size
    cancelled = replace(unknown, status=OrderStatus.CANCELLED, remaining_quantity=Decimal("0"),
                        authoritative=True, observed_at=AT)
    for facts in ((cancelled,), (cancelled, unknown), (unknown, cancelled)):
        quote = hedge_quote(market(), held, opposite, book, orders=facts, pending=pending,
                            safety_cushion=Decimal("0.02"), remaining_fee_per_pair=Decimal("0.01"))
        assert quote == Quote("market-1", "no", Decimal("0.56"), Decimal("5"))
        assert quote.quantity < market().rewards_min_size


@pytest.mark.parametrize("scenario", [
    "cap_tick_floor", "best_bid_tick_floor", "empty", "fully_paired", "zero_cap", "negative_cap",
    "sub_tick_cap", "below_exchange_minimum", "reverse_side", "nan_fee", "negative_fee",
    "wrong_position_owner", "wrong_position_market", "wrong_position_token", "wrong_book_token", "wrong_book_market",
])
def test_hedge_tick_and_infeasibility(scenario):
    m = market()
    held = Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), AT)
    opposite = Position("lp-v2:run-1", "market-1", "no", Decimal("0"), Decimal("0"), AT)
    book = Book("market-1", "no", Decimal("0.60"), Decimal("0.61"), AT)
    fee = Decimal("0.01")
    expected = None
    if scenario == "cap_tick_floor":
        m = replace(m, tick_size=Decimal("0.03"))
        expected = Quote("market-1", "no", Decimal("0.54"), Decimal("10"))
    elif scenario == "best_bid_tick_floor":
        m = replace(m, tick_size=Decimal("0.03"))
        book = replace(book, best_bid=Decimal("0.53"), best_ask=Decimal("0.54"))
        expected = Quote("market-1", "no", Decimal("0.51"), Decimal("10"))
    elif scenario == "empty":
        held = replace(held, quantity=Decimal("0"), cost_usdc=Decimal("0"))
    elif scenario == "fully_paired":
        opposite = replace(opposite, quantity=Decimal("10"), cost_usdc=Decimal("5.00"))
    elif scenario == "zero_cap":
        held = replace(held, cost_usdc=Decimal("9.70"))
    elif scenario == "negative_cap":
        held = replace(held, cost_usdc=Decimal("10.00"))
    elif scenario == "sub_tick_cap":
        m = replace(m, tick_size=Decimal("0.03"))
        held = replace(held, cost_usdc=Decimal("9.60"))
    elif scenario == "below_exchange_minimum":
        opposite = replace(opposite, quantity=Decimal("9.5"), cost_usdc=Decimal("4.75"))
    elif scenario == "reverse_side":
        held = replace(held, token_id="no", cost_usdc=Decimal("5.20"))
        opposite = replace(opposite, token_id="yes")
        book = replace(book, token_id="yes")
        expected = Quote("market-1", "yes", Decimal("0.45"), Decimal("10"))
    elif scenario == "nan_fee":
        fee = Decimal("NaN")
    elif scenario == "negative_fee":
        fee = Decimal("-0.01")
    elif scenario == "wrong_position_owner":
        opposite = replace(opposite, owner="old-lp")
    elif scenario == "wrong_position_market":
        opposite = replace(opposite, condition_id="other-market")
    elif scenario == "wrong_position_token":
        opposite = replace(opposite, token_id="yes")
    elif scenario == "wrong_book_token":
        book = replace(book, token_id="yes")
    elif scenario == "wrong_book_market":
        book = replace(book, condition_id="other-market")

    if scenario in ("nan_fee", "negative_fee") or scenario.startswith("wrong_"):
        with pytest.raises(ValueError):
            hedge_quote(m, held, opposite, book, orders=(), pending=(),
                        safety_cushion=Decimal("0.02"), remaining_fee_per_pair=fee)
    else:
        quote = hedge_quote(m, held, opposite, book, orders=(), pending=(),
                            safety_cushion=Decimal("0.02"), remaining_fee_per_pair=fee)
        assert quote == expected
        if quote is not None:
            assert quote.price <= book.best_bid and quote.price < book.best_ask
            assert quote.price % m.tick_size == 0
            assert quote.side == "BUY" and quote.post_only is True
