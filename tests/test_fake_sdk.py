from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lp_v2.models import Balance, Fill, MergeRequest, OrderRequest, OrderStatus, Outcome, Position, Quote
from lp_v2.sdk_port import SDKPort
from tests.fakes.fake_sdk import FakeSDK


T0 = datetime(2026, 11, 1, tzinfo=UTC)
T1 = T0 + timedelta(seconds=1)
T2 = T0 + timedelta(seconds=2)


def make_request(request_id="buy-yes", token_id="yes", owner="lp-v2:run-1"):
    return OrderRequest(
        request_id=request_id,
        owner=owner,
        quote=Quote("market-1", token_id, Decimal("0.40"), Decimal("10")),
        created_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )


@pytest.fixture
def placed_sdk():
    fake = FakeSDK(balance=Balance(Decimal("100"), T0))
    request = make_request()
    fake.script_place(request, Outcome.SUCCESS)
    sdk: SDKPort = fake
    return fake, sdk.place_order(request, at=T0).order


def test_place_order_success():
    fake = FakeSDK(balance=Balance(Decimal("100"), T0))
    sdk: SDKPort = fake
    request = make_request()
    fake.script_place(request, Outcome.SUCCESS)

    result = sdk.place_order(request, at=T0)

    assert result is not None
    assert result.outcome == Outcome.SUCCESS
    assert result.request == request
    order = result.order
    assert order.order_id
    assert order.request == request
    assert order.status == OrderStatus.LIVE
    assert order.authoritative is True
    assert order.filled_quantity == Decimal("0")
    assert order.remaining_quantity == Decimal("10")
    assert order.request.quote.side == "BUY"
    assert order.request.quote.post_only is True
    assert order.observed_at == T0
    assert sdk.read_orders() == (order,)
    assert sdk.place_order(request, at=T1) == result
    assert sdk.read_orders() == (order,)
    with pytest.raises(ValueError, match="request identity"):
        sdk.place_order(replace(request, quote=replace(request.quote, quantity=Decimal("20"))), at=T1)
    with pytest.raises(AttributeError):
        order.remaining_quantity = Decimal("20")


def test_full_fill(placed_sdk):
    fake, order = placed_sdk
    sdk: SDKPort = fake
    fill_args = dict(trade_id="trade-1", quantity=Decimal("10"), price=Decimal("0.40"),
                     fee_usdc=Decimal("0.10"), at=T1)

    fake.execute_fill(order.order_id, **fill_args)

    filled = sdk.read_orders()[0]
    assert filled.status == OrderStatus.FILLED
    assert filled.filled_quantity == Decimal("10")
    assert filled.remaining_quantity == Decimal("0")
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), T1),)
    assert sdk.read_balance() == Balance(Decimal("95.90"), T1)
    expected = Fill("trade-1", order.order_id, "buy-yes", "lp-v2:run-1", "market-1", "yes",
                    Decimal("10"), Decimal("0.40"), Decimal("0.10"), T1)
    assert sdk.read_trades() == (expected,)
    assert fake.execute_fill(order.order_id, **fill_args) == expected
    assert sdk.read_trades() == (expected,)
    assert sdk.read_balance() == Balance(Decimal("95.90"), T1)
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), T1),)


def test_partial_fill(placed_sdk):
    fake, order = placed_sdk
    sdk: SDKPort = fake
    fake.execute_fill(order.order_id, trade_id="partial-1", quantity=Decimal("4"),
                      price=Decimal("0.40"), fee_usdc=Decimal("0"), at=T1)

    partial = sdk.read_orders()[0]
    assert partial.status == OrderStatus.LIVE
    assert partial.filled_quantity == Decimal("4")
    assert partial.remaining_quantity == Decimal("6")
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("4"), Decimal("1.60"), T1),)
    assert sdk.read_balance() == Balance(Decimal("98.40"), T1)
    first = Fill("partial-1", order.order_id, "buy-yes", "lp-v2:run-1", "market-1", "yes",
                 Decimal("4"), Decimal("0.40"), Decimal("0"), T1)
    assert sdk.read_trades() == (first,)

    fake.execute_fill(order.order_id, trade_id="partial-2", quantity=Decimal("2"),
                      price=Decimal("0.40"), fee_usdc=Decimal("0.02"), at=T2)

    assert sdk.read_orders()[0].filled_quantity == Decimal("6")
    assert sdk.read_orders()[0].remaining_quantity == Decimal("4")
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("6"), Decimal("2.42"), T2),)
    second = Fill("partial-2", order.order_id, "buy-yes", "lp-v2:run-1", "market-1", "yes",
                  Decimal("2"), Decimal("0.40"), Decimal("0.02"), T2)
    assert sdk.read_trades() == (first, second)
    assert sdk.read_balance() == Balance(Decimal("97.58"), T2)


def test_cancel_success(placed_sdk):
    fake, order = placed_sdk
    sdk: SDKPort = fake
    fill = fake.execute_fill(order.order_id, trade_id="matched-before-cancel", quantity=Decimal("4"),
                             price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1)
    fake.script_cancel(order.order_id, Outcome.SUCCESS, cancelled=True)

    result = sdk.cancel_order(order.order_id, at=T2)

    assert result.outcome == Outcome.SUCCESS
    cancelled = result.order
    assert cancelled.authoritative is True
    assert cancelled.status == OrderStatus.CANCELLED
    assert cancelled.filled_quantity == Decimal("4")
    assert cancelled.remaining_quantity == Decimal("0")
    assert sdk.read_orders() == (cancelled,)
    assert sdk.read_trades() == (fill,)
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),)
    assert sdk.read_balance() == Balance(Decimal("98.36"), T1)
    with pytest.raises(ValueError, match="live order"):
        fake.execute_fill(order.order_id, trade_id="impossible-new-fill", quantity=Decimal("1"),
                          price=Decimal("0.40"), fee_usdc=Decimal("0"), at=T2 + timedelta(seconds=1))


@pytest.mark.parametrize("cancelled,status,remaining", [
    (False, OrderStatus.LIVE, Decimal("6")),
    (True, OrderStatus.CANCELLED, Decimal("0")),
])
def test_cancel_unknown_then_readback(placed_sdk, cancelled, status, remaining):
    fake, order = placed_sdk
    sdk: SDKPort = fake
    fill = fake.execute_fill(order.order_id, trade_id="before-unknown", quantity=Decimal("4"),
                             price=Decimal("0.40"), fee_usdc=Decimal("0"), at=T1)
    fake.script_cancel(order.order_id, Outcome.UNKNOWN, cancelled=cancelled)

    result = sdk.cancel_order(order.order_id, at=T2)

    assert result.outcome == Outcome.UNKNOWN
    assert result.order.authoritative is False
    assert result.order.status == OrderStatus.LIVE
    assert result.order.remaining_quantity == Decimal("6")
    assert result.order.filled_quantity == Decimal("4")
    facts = sdk.read_orders()
    assert len(facts) == 1
    assert facts[0].order_id == order.order_id
    assert facts[0].authoritative is True
    assert facts[0].status == status
    assert facts[0].remaining_quantity == remaining
    assert facts[0].filled_quantity == Decimal("4")
    assert result.order.authoritative is False
    assert sdk.read_trades() == (fill,)
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("4"), Decimal("1.60"), T1),)
    assert sdk.read_balance() == Balance(Decimal("98.40"), T1)


def test_late_fill_after_cancel(placed_sdk):
    fake, order = placed_sdk
    sdk: SDKPort = fake
    fake.execute_fill(order.order_id, trade_id="late-notice", quantity=Decimal("4"),
                      price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1, deliver=False)
    assert fake.take_fill_notifications() == ()
    fake.script_cancel(order.order_id, Outcome.SUCCESS, cancelled=True)
    cancelled = sdk.cancel_order(order.order_id, at=T2)
    assert cancelled.outcome == Outcome.SUCCESS
    assert cancelled.order.filled_quantity == Decimal("4")
    assert cancelled.order.remaining_quantity == Decimal("0")

    fake.deliver_fill("late-notice")

    expected = Fill("late-notice", order.order_id, "buy-yes", "lp-v2:run-1", "market-1", "yes",
                    Decimal("4"), Decimal("0.40"), Decimal("0.04"), T1)
    assert fake.take_fill_notifications() == (expected,)
    assert expected.executed_at < cancelled.observed_at
    assert sdk.read_trades() == (expected,)
    assert sdk.read_orders() == (cancelled.order,)
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),)
    assert sdk.read_balance() == Balance(Decimal("98.36"), T1)
    fake.deliver_fill("late-notice")
    assert fake.take_fill_notifications() == (expected,)
    assert sdk.read_trades() == (expected,)
    assert sdk.read_positions() == (Position("lp-v2:run-1", "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),)
    assert sdk.read_balance() == Balance(Decimal("98.36"), T1)


@pytest.mark.parametrize("outcome", [Outcome.SUCCESS, Outcome.FAILED, Outcome.UNKNOWN],
                         ids=["success", "failure", "timeout"])
def test_merge_outcomes(outcome):
    owned_yes = Position("lp-v2:run-1", "market-1", "yes", Decimal("10"), Decimal("4.10"), T0)
    owned_no = Position("lp-v2:run-1", "market-1", "no", Decimal("10"), Decimal("5.50"), T0)
    old_yes = Position("old-lp", "market-1", "yes", Decimal("7"), Decimal("2.80"), T0)
    old_no = Position("old-lp", "market-1", "no", Decimal("7"), Decimal("3.50"), T0)
    positions = (owned_yes, owned_no, old_yes, old_no)
    fake = FakeSDK(balance=Balance(Decimal("100"), T0), positions=positions)
    sdk: SDKPort = fake
    request = MergeRequest("merge-1", "lp-v2:run-1", "market-1", "yes", "no", Decimal("4"), T0)
    fake.script_merge(request, outcome)

    result = sdk.merge(request, at=T1)

    assert result is not None
    assert result.request == request
    assert result.outcome == outcome
    assert result.received_usdc == Decimal("0")
    assert sdk.read_balance() == Balance(Decimal("100"), T0)
    assert sdk.read_positions() == positions
    assert sdk.merge(request, at=T2) == result
    with pytest.raises(ValueError, match="merge identity"):
        sdk.merge(replace(request, quantity=Decimal("5")), at=T2)

    if outcome == Outcome.SUCCESS:
        receipt = fake.receive_merge(request, at=T2)
        assert receipt.request == request
        assert receipt.outcome == Outcome.SUCCESS
        assert receipt.received_usdc == Decimal("4")
        assert sdk.read_balance() == Balance(Decimal("104"), T2)
        expected_positions = (
            Position("lp-v2:run-1", "market-1", "yes", Decimal("6"), Decimal("2.46"), T2),
            Position("lp-v2:run-1", "market-1", "no", Decimal("6"), Decimal("3.30"), T2),
            old_yes, old_no,
        )
        assert sdk.read_positions() == expected_positions
        assert fake.receive_merge(request, at=T2 + timedelta(seconds=1)) == receipt
        assert sdk.merge(request, at=T2 + timedelta(seconds=1)) == receipt
        assert sdk.read_balance() == Balance(Decimal("104"), T2)
        assert sdk.read_positions() == expected_positions
    else:
        with pytest.raises(ValueError, match="successful acknowledgement"):
            fake.receive_merge(request, at=T2)
        assert sdk.read_balance() == Balance(Decimal("100"), T0)
        assert sdk.read_positions() == positions
