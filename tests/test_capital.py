from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lp_v2.capital import capital_breakdown, can_open_market, recovery_action_allowed
from lp_v2.models import (
    CapitalBreakdown, CapitalReservation, MergeRequest, MergeResult, Order, OrderRequest,
    OrderStatus, Outcome, Position, Quote,
)


AT = datetime(2026, 11, 2, tzinfo=UTC)
OWNER = "lp-v2:run-1"


def request(request_id, token, quantity, price, *, owner=OWNER, condition_id="market-1"):
    return OrderRequest(request_id, owner, Quote(condition_id, token, Decimal(price), Decimal(quantity)),
                        AT - timedelta(hours=2), AT - timedelta(hours=1))


def live(intent):
    return Order("order-" + intent.request_id, intent, Decimal("0"), intent.quote.quantity,
                 OrderStatus.LIVE, AT - timedelta(minutes=90), True)


def initial_snapshot():
    yes = request("yes-10", "yes", "10", "0.40")
    no = request("no-6", "no", "6", "0.55")
    pending = request("no-4", "no", "4", "0.55")
    reservation = CapitalReservation(OWNER, "market-1", ("yes", "no"), Decimal("10"), Decimal("0.20"),
                                     (yes.request_id, no.request_id, pending.request_id))
    return reservation, live(yes), live(no), pending


def test_components_count_each_identity_once():
    reservation, yes, no, distinct = initial_snapshot()
    old_order = live(request("old", "yes", "50", "0.40", owner="old-lp"))
    old_position = Position("old-lp", "market-1", "yes", Decimal("100"), Decimal("40"), AT)
    inputs = dict(reservations=(reservation, reservation), positions=(old_position,),
                  orders=(old_order, yes, no, no), pending=(no.request, distinct, distinct), merge_results=())
    before = dict(inputs)

    result = capital_breakdown(OWNER, **inputs)

    assert result == CapitalBreakdown(
        inventory_cost=Decimal("0"), open_buys=Decimal("7.30"), pending_requests=Decimal("2.20"),
        fee_reserve=Decimal("0.20"), hedge_reserve=Decimal("0.30"), total=Decimal("10.00"),
    )
    assert inputs == before


@pytest.mark.parametrize("inventory,headroom", [("4.10", "0.30"), ("4.00", "0.40")],
                         ids=["limit_fill_plus_fee", "better_fill_plus_fee"])
def test_fill_preserves_reserved_capital(inventory, headroom):
    reservation, yes, no, distinct = initial_snapshot()
    reservation = replace(reservation, remaining_fee_usdc=Decimal("0.10"))
    filled = replace(yes, status=OrderStatus.FILLED, filled_quantity=Decimal("10"),
                     remaining_quantity=Decimal("0"), observed_at=AT)
    position = Position(OWNER, "market-1", "yes", Decimal("10"), Decimal(inventory), AT)

    result = capital_breakdown(
        OWNER, reservations=(reservation,), positions=(position, position), orders=(filled, no),
        pending=(filled.request, no.request, distinct), merge_results=(),
    )

    assert result == CapitalBreakdown(
        inventory_cost=Decimal(inventory), open_buys=Decimal("3.30"), pending_requests=Decimal("2.20"),
        fee_reserve=Decimal("0.10"), hedge_reserve=Decimal(headroom), total=Decimal("10.00"),
    )


@pytest.mark.parametrize("scenario", [
    "cancel_unknown", "cancel_requested", "cancelled_delayed_fill", "merge_ack", "merge_failure",
    "merge_timeout", "receipt_positions_stale", "settled_receipt", "duplicate_receipt",
    "unrelated_receipt", "positions_without_receipt",
])
def test_unconfirmed_cancel_and_merge_keep_reserve(scenario):
    yes, no = live(request("yes", "yes", "10", "0.40")), live(request("no", "no", "10", "0.55"))
    reservation = CapitalReservation(OWNER, "market-1", ("yes", "no"), Decimal("10"), Decimal("0.20"), ("yes", "no"))
    positions, results = (), ()
    if scenario in ("cancel_unknown", "cancel_requested"):
        status = OrderStatus.LIVE if scenario == "cancel_unknown" else OrderStatus.CANCELLED
        yes, no = (replace(order, status=status, authoritative=False) for order in (yes, no))
        expected = CapitalBreakdown(Decimal("0"), Decimal("9.50"), Decimal("0"), Decimal("0.30"), Decimal("0.20"), Decimal("10"))
    elif scenario == "cancelled_delayed_fill":
        yes = replace(yes, status=OrderStatus.CANCELLED, filled_quantity=Decimal("4"), remaining_quantity=Decimal("0"), observed_at=AT)
        no = replace(no, status=OrderStatus.CANCELLED, remaining_quantity=Decimal("0"), observed_at=AT)
        reservation = replace(reservation, remaining_fee_usdc=Decimal("0"))
        expected = CapitalBreakdown(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("4"), Decimal("0"), Decimal("4"))
    else:
        yes, no = (replace(order, status=OrderStatus.FILLED, filled_quantity=Decimal("10"),
                           remaining_quantity=Decimal("0"), observed_at=AT) for order in (yes, no))
        merge = MergeRequest("merge-10", OWNER, "market-1", "yes", "no", Decimal("10"), AT)
        reservation = replace(reservation, remaining_fee_usdc=Decimal("0"), merge_requests=(merge,))
        positions = (Position(OWNER, "market-1", "yes", Decimal("10"), Decimal("4.10"), AT),
                     Position(OWNER, "market-1", "no", Decimal("10"), Decimal("5.60"), AT))
        outcome = {"merge_failure": Outcome.FAILED, "merge_timeout": Outcome.UNKNOWN}.get(scenario, Outcome.SUCCESS)
        received = Decimal("10") if scenario in ("receipt_positions_stale", "settled_receipt", "duplicate_receipt", "unrelated_receipt") else Decimal("0")
        receipt_request = replace(merge, merge_id="unrelated-merge") if scenario == "unrelated_receipt" else merge
        result = MergeResult(receipt_request, outcome, received, AT + timedelta(seconds=1))
        results = (result, result) if scenario == "duplicate_receipt" else (result,)
        if scenario in ("settled_receipt", "duplicate_receipt", "unrelated_receipt", "positions_without_receipt"):
            positions = tuple(replace(p, quantity=Decimal("0"), cost_usdc=Decimal("0"), updated_at=AT + timedelta(seconds=1)) for p in positions)
        if scenario in ("settled_receipt", "duplicate_receipt"):
            expected = CapitalBreakdown(*(Decimal("0"),) * 6)
        elif scenario in ("unrelated_receipt", "positions_without_receipt"):
            expected = CapitalBreakdown(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("10"), Decimal("0"), Decimal("10"))
        else:
            expected = CapitalBreakdown(Decimal("9.70"), Decimal("0"), Decimal("0"), Decimal("0.30"), Decimal("0"), Decimal("10"))

    result = capital_breakdown(OWNER, reservations=(reservation,), positions=positions,
                               orders=(yes, no), pending=(), merge_results=results)

    assert result == expected


@pytest.mark.parametrize("invalid_field,invalid_value", [
    (None, None), ("startup_cap", Decimal("-1")), ("startup_cap", Decimal("NaN")),
    ("occupied", Decimal("-1")), ("planned_shares", Decimal("NaN")),
    ("projected_market_unpaired", Decimal("-1")), ("current_unpaired_cost", Decimal("NaN")),
    ("proposed_unpaired_cost", Decimal("-1")),
])
def test_new_market_cannot_exceed_cap(invalid_field, invalid_value):
    args = dict(startup_cap=Decimal("100"), occupied=Decimal("90"), planned_shares=Decimal("10"),
                projected_market_unpaired=Decimal("10"), current_unpaired_cost=Decimal("0"),
                proposed_unpaired_cost=Decimal("5"))
    if invalid_field is not None:
        args[invalid_field] = invalid_value
        with pytest.raises(ValueError):
            can_open_market(**args)
        return

    assert can_open_market(**(args | {"planned_shares": Decimal("11")})) is False
    assert can_open_market(**args) is True
    existing = CapitalReservation(OWNER, "existing-market", ("old-yes", "old-no"), Decimal("90"), Decimal("0"), ())
    admitted = CapitalReservation(OWNER, "new-market", ("new-yes", "new-no"), Decimal("10"), Decimal("0"), ())
    updated = capital_breakdown(OWNER, reservations=(existing, admitted), positions=(), orders=(), pending=(), merge_results=())
    assert updated.total == Decimal("100")
    assert can_open_market(**(args | {"occupied": updated.total, "planned_shares": Decimal("1"),
                                      "projected_market_unpaired": Decimal("1"), "proposed_unpaired_cost": Decimal("0.50")})) is False


@pytest.mark.parametrize("scenario", [
    "lower_cap", "market_at_plan", "market_over_plan", "cost_at_half_cap", "cost_above_half_cap", "held_at_half_cap",
])
def test_restart_below_occupied_is_reduce_only(scenario):
    args = dict(startup_cap=Decimal("100"), occupied=Decimal("0"), planned_shares=Decimal("10"),
                projected_market_unpaired=Decimal("10"), current_unpaired_cost=Decimal("0"),
                proposed_unpaired_cost=Decimal("5"))
    expected = True
    if scenario == "lower_cap":
        reservation, yes, no, distinct = initial_snapshot()
        occupied = capital_breakdown(OWNER, reservations=(reservation,), positions=(), orders=(yes, no),
                                      pending=(distinct,), merge_results=())
        assert occupied.total == Decimal("10")
        args.update(startup_cap=Decimal("5"), occupied=occupied.total, planned_shares=Decimal("1"),
                    projected_market_unpaired=Decimal("1"), proposed_unpaired_cost=Decimal("0.40"))
        expected = False
    elif scenario == "market_over_plan":
        args["projected_market_unpaired"] = Decimal("11")
        expected = False
    elif scenario in ("cost_at_half_cap", "cost_above_half_cap"):
        args.update(occupied=Decimal("40"), planned_shares=Decimal("20"), projected_market_unpaired=Decimal("20"),
                    current_unpaired_cost=Decimal("40"), proposed_unpaired_cost=Decimal("10") if scenario == "cost_at_half_cap" else Decimal("10.01"))
        expected = scenario == "cost_at_half_cap"
    elif scenario == "held_at_half_cap":
        args.update(occupied=Decimal("50"), planned_shares=Decimal("1"), projected_market_unpaired=Decimal("1"),
                    current_unpaired_cost=Decimal("50"), proposed_unpaired_cost=Decimal("0.01"))
        expected = False

    assert can_open_market(**args) is expected
    if scenario == "lower_cap":
        assert recovery_action_allowed("hedge", covered_hedge=True) is True
        assert recovery_action_allowed("hedge", covered_hedge=False) is False
        assert recovery_action_allowed("merge", covered_hedge=False) is True
        assert recovery_action_allowed("exit", covered_hedge=False) is True
        assert recovery_action_allowed("open_market", covered_hedge=True) is False
