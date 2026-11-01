from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from random import Random

import pytest

from lp_v2.models import Book, Market, Quote, Token
from lp_v2.quoting import quote_pair


AT = datetime(2026, 11, 2, tzinfo=UTC)


def market():
    return Market(
        condition_id="market-1", tokens=(Token("yes", "Yes"), Token("no", "No")),
        tick_size=Decimal("0.01"), min_order_size=Decimal("5"), rewards_min_size=Decimal("10"),
        best_bid=0.05, best_ask=0.06, liquidity_num=2000.0, volume_24hr=10000.0,
        rewards_daily_rate=100.0, rewards_max_spread=5.0, rebate_rate=0.25,
        taker_fee_bps=400, fees_enabled=True, rewards_enabled=True, political=True,
        in_play=False, accepting_orders=True,
    )


def book(token_id, bid, ask):
    return Book("market-1", token_id, Decimal(bid), Decimal(ask), AT)


def test_pair_is_post_only_in_band():
    pair = quote_pair(
        market(), book("yes", "0.40", "0.42"), book("no", "0.55", "0.57"),
        planned_quantity=Decimal("10"), fee_per_pair=Decimal("0.01"), safety_cushion=Decimal("0.02"),
    )

    assert pair == (
        Quote("market-1", "yes", Decimal("0.40"), Decimal("10")),
        Quote("market-1", "no", Decimal("0.55"), Decimal("10")),
    )
    assert all(quote.side == "BUY" and quote.post_only is True for quote in pair)
    assert Decimal("0.36") <= pair[0].price <= Decimal("0.46")
    assert Decimal("0.51") <= pair[1].price <= Decimal("0.61")
    assert pair[0].price < Decimal("0.42") and pair[1].price < Decimal("0.57")
    assert pair[0].price + pair[1].price + Decimal("0.01") == Decimal("0.96") <= Decimal("0.98")


@pytest.mark.parametrize("scenario", ["fee_adjustment", "awkward_tick"])
def test_prices_and_fees_fit_safety(scenario):
    m = market()
    if scenario == "fee_adjustment":
        yes, no = book("yes", "0.50", "0.52"), book("no", "0.49", "0.51")
        fee = Decimal("0.02")
    else:
        m = replace(m, tick_size=Decimal("0.03"))
        yes, no = book("yes", "0.41", "0.42"), book("no", "0.52", "0.54")
        fee = Decimal("0.01")

    pair = quote_pair(m, yes, no, planned_quantity=Decimal("10"),
                      fee_per_pair=fee, safety_cushion=Decimal("0.02"))

    assert pair is not None
    y, n = pair
    assert y.price <= yes.best_bid and y.price < yes.best_ask
    assert n.price <= no.best_bid and n.price < no.best_ask
    assert y.price % m.tick_size == 0 and n.price % m.tick_size == 0
    assert y.price + n.price + fee <= Decimal("0.98")
    if scenario == "fee_adjustment":
        assert Decimal("0.46") <= y.price <= Decimal("0.50")
        assert Decimal("0.45") <= n.price <= Decimal("0.49")
        assert y.price + n.price <= Decimal("0.96")
        assert y.price < yes.best_bid or n.price < no.best_bid
    else:
        assert y.price == Decimal("0.39")
        assert n.price == Decimal("0.51")


@pytest.mark.parametrize("scenario", ["pair_budget", "below_minimum_size", "inaccessible_reward_band"])
def test_infeasible_pair_returns_none(scenario):
    m = market()
    yes, no = book("yes", "0.40", "0.42"), book("no", "0.55", "0.57")
    planned = Decimal("10")
    if scenario == "pair_budget":
        m = replace(m, tick_size=Decimal("0.001"), rewards_max_spread=0.1)
        yes, no = book("yes", "0.499", "0.501"), book("no", "0.499", "0.501")
        assert Decimal("0.499") + Decimal("0.499") + Decimal("0.01") == Decimal("1.008") > Decimal("0.98")
    elif scenario == "below_minimum_size":
        planned = Decimal("9")
    else:
        yes = book("yes", "0.40", "0.60")

    pair = quote_pair(m, yes, no, planned_quantity=planned,
                      fee_per_pair=Decimal("0.01"), safety_cushion=Decimal("0.02"))

    assert pair is None


@pytest.mark.parametrize("field,value", [
    ("fee_per_pair", Decimal("NaN")),
    ("fee_per_pair", Decimal("Infinity")),
    ("fee_per_pair", Decimal("-0.01")),
    ("tick_size", Decimal("0")),
    ("tick_size", Decimal("-0.01")),
    ("yes_token", "other-token"),
    ("no_token", "other-token"),
    ("book_market", "other-market"),
    ("market_identity", "other-market"),
    ("safety_cushion", Decimal("0.019")),
], ids=["fee_nan", "fee_infinite", "fee_negative", "tick_zero", "tick_negative",
        "wrong_yes_token", "wrong_no_token", "wrong_book_market", "wrong_market_identity", "safety_below_two_cents"])
def test_invalid_quote_inputs(field, value):
    m = market()
    yes, no = book("yes", "0.40", "0.42"), book("no", "0.55", "0.57")
    amounts = dict(planned_quantity=Decimal("10"), fee_per_pair=Decimal("0.01"), safety_cushion=Decimal("0.02"))
    if field in amounts:
        amounts[field] = value
    elif field == "tick_size":
        m = replace(m, tick_size=value)
    elif field == "yes_token":
        yes = replace(yes, token_id=value)
    elif field == "no_token":
        no = replace(no, token_id=value)
    elif field == "book_market":
        yes = replace(yes, condition_id=value)
    else:
        m = replace(m, condition_id=value)

    with pytest.raises(ValueError):
        quote_pair(m, yes, no, **amounts)


def test_random_books_preserve_pair_rules():
    rng = Random(20261102)
    accepted = rejected = 0
    for sample in range(1000):
        m = replace(
            market(), tick_size=rng.choice(tuple(map(Decimal, ("0.001", "0.005", "0.01", "0.03", "0.05")))),
            rewards_max_spread=rng.choice((0.1, 1.0, 2.0, 5.0, 10.0)),
            min_order_size=Decimal(rng.randint(1, 10)), rewards_min_size=Decimal(rng.randint(1, 20)),
        )
        books = {}
        for token in ("yes", "no"):
            bid = Decimal(rng.randint(10, 900)) / 1000
            ask = bid + Decimal(rng.randint(1, 100)) / 1000
            books[token] = Book("market-1", token, bid, ask, AT)
        planned = Decimal(rng.randint(1, 30))
        fee = rng.choice(tuple(map(Decimal, ("0", "0.005", "0.01", "0.02", "0.04"))))
        context = f"seed=20261102 sample={sample} market={m!r} books={books!r} plan={planned} fee={fee}"

        pair = quote_pair(m, books["yes"], books["no"], planned_quantity=planned,
                          fee_per_pair=fee, safety_cushion=Decimal("0.02"))

        if pair is None:
            rejected += 1
            continue
        accepted += 1
        assert len(pair) == 2, context
        assert {quote.token_id for quote in pair} == {"yes", "no"}, context
        assert pair[0].price + pair[1].price + fee <= Decimal("0.98"), context
        band = Decimal(str(m.rewards_max_spread)) / 100
        for quote in pair:
            current = books[quote.token_id]
            midpoint = (current.best_bid + current.best_ask) / 2
            assert quote.condition_id == "market-1", context
            assert quote.side == "BUY" and quote.post_only is True, context
            assert isinstance(quote.price, Decimal) and quote.price > 0, context
            assert quote.price <= current.best_bid and quote.price < current.best_ask, context
            assert quote.price % m.tick_size == 0, context
            assert midpoint - band <= quote.price <= midpoint + band, context
            assert quote.quantity == planned, context
            assert quote.quantity >= m.min_order_size and quote.quantity >= m.rewards_min_size, context
    assert accepted > 0 and rejected > 0
