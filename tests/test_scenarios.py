from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from lp_v2.capital import projected_unpaired_cost
from lp_v2.engine import Engine
from lp_v2.ledger import Ledger
from lp_v2.models import Balance, Book, CancelRequest, Decision, Market, MarketPlan, OrderRequest, OrderResult, Outcome, Quote, Token
from tests.fakes.fake_sdk import FakeSDK


AT = datetime(2026, 11, 2, tzinfo=UTC)
OWNER = "lp-v2:scenarios"


def plan(cid="m", *, quantity="10", yes="0.40", no="0.55", reward=100.0):
    market = Market(cid, (Token(cid + "-yes", "Yes"), Token(cid + "-no", "No")), D("0.01"), D("1"), D("10"),
                    0.49, 0.51, 2000.0, 10000.0, reward, 5.0, 0.25, 400, True, True, True, False, True)
    books = (Book(cid, cid + "-yes", D(yes), D(yes) + D("0.02"), AT),
             Book(cid, cid + "-no", D(no), D(no) + D("0.02"), AT))
    return MarketPlan(market, books, D(quantity), (D("0.01"), D("0.01")))


def world(*, cap="100"):
    fake = FakeSDK(balance=Balance(D("100"), AT))
    ledger = Ledger(OWNER, D(cap), balance=fake.read_balance())
    return ledger, fake, Engine(ledger)


def submit(ledger, fake, decision, *, outcomes=None):
    results = {}
    for request in decision.place:
        token = request.quote.token_id
        fake.script_place(request, (outcomes or {}).get(token, Outcome.SUCCESS))
        result = fake.place_order(request, at=request.created_at)
        ledger.apply_order(result)
        results[token] = result
    return results


def quotes(decision):
    return {r.quote.token_id: r.quote for r in decision.place}


def test_one_side_filled():
    ledger, fake, engine = world()
    inputs = plan()
    opening = engine.decide((inputs,), now=AT)
    assert quotes(opening) == {"m-yes": Quote("m", "m-yes", D("0.40"), D("10")),
                               "m-no": Quote("m", "m-no", D("0.55"), D("10"))}
    assert opening.cancel == opening.merge == ()
    assert all(r.owner == OWNER and r.expires_at == AT + timedelta(hours=1) for r in opening.place)
    assert ledger.snapshot().capital.total == D("10")
    orders = submit(ledger, fake, opening, outcomes={"m-no": Outcome.FAILED})
    fill = fake.execute_fill(orders["m-yes"].order.order_id, trade_id="yes-full", quantity=D("10"),
                             price=D("0.40"), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1))
    ledger.apply_fill(fill)
    assert ledger.snapshot().positions[0].cost_usdc == D("4.10")
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    decision = engine.decide((inputs,), now=AT + timedelta(seconds=2))
    assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("10"))}
    assert decision.cancel == decision.merge == ()
    assert decision.place[0].quote.side == "BUY" and decision.place[0].quote.post_only is True
    assert decision.place[0].expires_at == AT + timedelta(hours=1, seconds=2)
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((inputs,), now=AT + timedelta(seconds=3)) == Decision()
    assert ledger.snapshot().capital.total == D("10")


def test_both_sides_partial():
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    for token, quantity, price, fee in (("m-yes", "4", "0.40", "0.04"), ("m-no", "2", "0.55", "0.02")):
        ledger.apply_fill(fake.execute_fill(orders[token].order.order_id, trade_id=token + "-partial", quantity=D(quantity),
                                            price=D(price), fee_usdc=D(fee), at=AT + timedelta(seconds=1)))
    state = ledger.snapshot()
    assert {p.token_id: (p.quantity, p.cost_usdc) for p in state.positions} == {
        "m-yes": (D("4"), D("1.64")), "m-no": (D("2"), D("1.12")),
    }
    assert {o.request.quote.token_id: o.remaining_quantity for o in state.orders} == {"m-yes": D("6"), "m-no": D("8")}
    decision = engine.decide((inputs,), now=AT + timedelta(seconds=2))
    assert decision.place == decision.cancel == ()
    assert len(decision.merge) == 1
    merge = decision.merge[0]
    assert (merge.owner, merge.condition_id, merge.yes_token_id, merge.no_token_id, merge.quantity) == (OWNER, "m", "m-yes", "m-no", D("2"))
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((inputs,), now=AT + timedelta(seconds=3)) == Decision()
    fake.script_merge(merge, Outcome.SUCCESS)
    ledger.apply_merge(fake.merge(merge, at=AT + timedelta(seconds=3)))
    assert engine.decide((inputs,), now=AT + timedelta(seconds=4)) == Decision()
    assert ledger.snapshot().capital.total == D("10")


def test_late_fill_after_cancel():
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    fill = fake.execute_fill(orders["m-yes"].order.order_id, trade_id="late-four", quantity=D("4"), price=D("0.40"),
                             fee_usdc=D("0.04"), at=AT + timedelta(seconds=1), deliver=False)
    for result in orders.values():
        fake.script_cancel(result.order.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(result.order.order_id, at=AT + timedelta(seconds=2)))
    assert ledger.snapshot().capital.total == D("4")
    assert ledger.snapshot().capital.inventory_cost == D("0")
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    assert engine.decide((inputs,), now=AT + timedelta(seconds=3)) == Decision()
    assert ledger.snapshot().capital.total == D("4")
    fake.deliver_fill(fill.trade_id)
    for fact in fake.take_fill_notifications():
        ledger.apply_fill(fact)
    decision = engine.decide((inputs,), now=AT + timedelta(seconds=4))
    assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("4"))}
    assert decision.cancel == decision.merge == ()
    assert ledger.snapshot().capital.total == D("4")
    assert ledger.snapshot().positions[0].cost_usdc == D("1.64")
    ledger.apply_fill(fill)
    assert engine.decide((inputs,), now=AT + timedelta(seconds=5)) == Decision()
    assert ledger.snapshot().capital.total == D("4")


def test_unknown_cancel():
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    ledger.apply_fill(fake.execute_fill(orders["m-yes"].order.order_id, trade_id="yes-ten", quantity=D("10"),
                                        price=D("0.40"), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1)))
    old = orders["m-no"].order
    cancel = CancelRequest(old.order_id, old.request.request_id, OWNER, "m", AT + timedelta(seconds=60))
    assert ledger.register_cancel(cancel)
    fake.script_cancel(old.order_id, Outcome.UNKNOWN, cancelled=False)
    ledger.apply_order(fake.cancel_order(old.order_id, at=cancel.created_at))
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    assert engine.decide((inputs,), now=AT + timedelta(seconds=61)) == Decision()
    assert engine.decide((inputs,), now=AT + timedelta(hours=1, seconds=1)) == Decision()
    assert ledger.snapshot().capital.total == D("10")
    assert ledger.register_cancel(cancel) is False
    fake.script_cancel(old.order_id, Outcome.SUCCESS, cancelled=True)
    ledger.apply_order(fake.cancel_order(old.order_id, at=AT + timedelta(hours=1, seconds=2)))
    decision = engine.decide((inputs,), now=AT + timedelta(hours=1, seconds=3))
    assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("10"))}
    assert decision.cancel == decision.merge == ()
    assert all(o.remaining_quantity == 0 for o in ledger.snapshot().orders)
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((inputs,), now=AT + timedelta(hours=1, seconds=4)) == Decision()


def test_existing_or_pending_hedge():
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT), outcomes={"m-no": Outcome.FAILED})
    ledger.apply_fill(fake.execute_fill(orders["m-yes"].order.order_id, trade_id="held-ten", quantity=D("10"),
                                        price=D("0.40"), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1)))
    terms = ledger.reservation_terms("m")
    for identity, quantity, outcome in (("existing-three", "3", Outcome.SUCCESS), ("pending-two", "2", Outcome.UNKNOWN)):
        request = OrderRequest(identity, OWNER, Quote("m", "m-no", D("0.56"), D(quantity)),
                               AT + timedelta(seconds=2), AT + timedelta(hours=1, seconds=2), terms)
        assert ledger.register_order(request)
        fake.script_place(request, outcome)
        result = fake.place_order(request, at=request.created_at)
        ledger.apply_order(result)
        if outcome == Outcome.SUCCESS:
            ledger.apply_order(OrderResult(request, Outcome.UNKNOWN, None, AT + timedelta(seconds=3)))
            assert ledger.register_order(request) is False  # Pending alias of the same accepted request.
    for cid in ("m", "foreign-market"):
        foreign = OrderRequest("foreign-" + cid, "old-lp", Quote(cid, "m-no", D("0.56"), D("100")),
                               AT, AT + timedelta(hours=1))
        fake.script_place(foreign, Outcome.SUCCESS)
        ledger.apply_order(fake.place_order(foreign, at=AT))
    ledger.reconcile(fake)
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    decision = engine.decide((inputs,), now=AT + timedelta(seconds=4))
    assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("5"))}
    assert decision.cancel == decision.merge == ()
    assert all(r.owner == OWNER and r.quote.condition_id == "m" for r in decision.place)
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((inputs,), now=AT + timedelta(seconds=5)) == Decision()
    assert ledger.snapshot().capital.total == D("10")


@pytest.mark.parametrize("outcome", [Outcome.SUCCESS, Outcome.FAILED, Outcome.UNKNOWN], ids=["success", "failure", "timeout"])
def test_merge_results(outcome):
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    fills = []
    for token, price in (("m-yes", "0.40"), ("m-no", "0.55")):
        fill = fake.execute_fill(orders[token].order.order_id, trade_id=token + "-ten", quantity=D("10"),
                                 price=D(price), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1))
        ledger.apply_fill(fill)
        fills.append(fill)
    assert ledger.snapshot().capital.inventory_cost == D("9.70")
    assert ledger.snapshot().cash == D("90.30")
    decision = engine.decide((inputs,), now=AT + timedelta(seconds=2))
    assert decision.place == decision.cancel == () and len(decision.merge) == 1
    merge = decision.merge[0]
    assert (merge.condition_id, merge.yes_token_id, merge.no_token_id, merge.quantity) == ("m", "m-yes", "m-no", D("10"))
    fake.script_merge(merge, outcome)
    ledger.apply_merge(fake.merge(merge, at=AT + timedelta(seconds=2)))
    assert ledger.snapshot().capital.total == D("10")
    assert ledger.snapshot().realized_profit == D("0")
    assert engine.decide((inputs,), now=AT + timedelta(seconds=3)) == Decision()
    assert ledger.snapshot().capital.total == D("10")
    if outcome == Outcome.SUCCESS:
        receipt = fake.receive_merge(merge, at=AT + timedelta(seconds=4))
        ledger.apply_merge(receipt)
        assert ledger.snapshot().capital.total == D("10")
        ledger.reconcile(fake)
        assert ledger.snapshot().capital.total == D("0")
        assert ledger.snapshot().cash == D("100.30")
        assert ledger.snapshot().realized_profit == D("0.30")
        before = ledger.snapshot()
        ledger.apply_merge(receipt)
        for fill in fills:
            ledger.apply_fill(fill)
        assert engine.decide((), now=AT + timedelta(seconds=5)) == Decision()
        assert ledger.snapshot() == before


@pytest.mark.parametrize("matched", [False, True], ids=["hedge", "merge"])
def test_rank_drop_keeps_recovery(matched):
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT),
                    outcomes={} if matched else {"m-no": Outcome.FAILED})
    for token, price in (("m-yes", "0.40"), ("m-no", "0.55")) if matched else (("m-yes", "0.40"),):
        ledger.apply_fill(fake.execute_fill(orders[token].order.order_id, trade_id=token + "-held", quantity=D("10"),
                                            price=D(price), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1)))
    dropped = replace(inputs, market=replace(inputs.market, rewards_daily_rate=0),
                      books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    decision = engine.decide((dropped,), now=AT + timedelta(seconds=600))
    assert decision.cancel == ()
    if matched:
        assert decision.place == () and len(decision.merge) == 1
        assert decision.merge[0].quantity == D("10") and decision.merge[0].condition_id == "m"
    else:
        assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("10"))}
        assert decision.merge == ()
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((dropped,), now=AT + timedelta(seconds=601)) == Decision()
    empty, _, new_engine = world()
    assert new_engine.decide((dropped,), now=AT + timedelta(seconds=600)) == Decision()
    assert empty.snapshot().capital.total == D("0")


@pytest.mark.parametrize("matched", [False, True], ids=["hedge", "merge"])
def test_restart_uses_exchange_facts(matched):
    original, fake, engine = world()
    inputs = plan()
    orders = submit(original, fake, engine.decide((inputs,), now=AT),
                    outcomes={} if matched else {"m-no": Outcome.FAILED})
    for token, price in (("m-yes", "0.40"), ("m-no", "0.55")) if matched else (("m-yes", "0.40"),):
        original.apply_fill(fake.execute_fill(orders[token].order.order_id, trade_id=token + "-restart", quantity=D("10"),
                                              price=D(price), fee_usdc=D("0.10"), at=AT + timedelta(seconds=1)))
    recovered = Ledger(OWNER, D("5"))
    recovered.reconcile(fake)
    recovered_engine = Engine(recovered)
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), AT)))
    normal = engine.decide((inputs,), now=AT + timedelta(seconds=2))
    restarted = recovered_engine.decide((inputs,), now=AT + timedelta(seconds=2))
    assert restarted == normal
    assert restarted.cancel == ()
    if matched:
        assert restarted.place == () and len(restarted.merge) == 1
        assert restarted.merge[0].quantity == D("10")
    else:
        assert quotes(restarted) == {"m-no": Quote("m", "m-no", D("0.56"), D("10"))}
        assert restarted.merge == ()
    assert original.snapshot().capital.total == recovered.snapshot().capital.total == D("10")
    assert recovered_engine.decide((inputs, plan("new")), now=AT + timedelta(seconds=3)) == Decision()
    assert recovered.snapshot().capital.total == D("10")


def test_requote_min_interval():
    ledger, fake, engine = world()
    inputs = plan(no="0.40")
    inputs = replace(inputs, market=replace(inputs.market, rewards_max_spread=2.0))
    opening = engine.decide((inputs,), now=AT)
    assert {q.price for q in quotes(opening).values()} == {D("0.40")}
    orders = submit(ledger, fake, opening)
    moved = replace(inputs, books=(Book("m", "m-yes", D("0.46"), D("0.48"), AT),
                                   Book("m", "m-no", D("0.46"), D("0.48"), AT)))
    assert engine.decide((moved,), now=AT + timedelta(seconds=59)) == Decision()
    assert ledger.snapshot().capital.total == D("10")
    cancel = engine.decide((moved,), now=AT + timedelta(seconds=60))
    assert cancel.place == cancel.merge == ()
    assert {c.order_id for c in cancel.cancel} == {r.order.order_id for r in orders.values()}
    assert len(cancel.cancel) == 2
    assert all(c.owner == OWNER and c.condition_id == "m" for c in cancel.cancel)
    assert engine.decide((moved,), now=AT + timedelta(seconds=61)) == Decision()
    assert ledger.snapshot().capital.total == D("10")
    for request in cancel.cancel:
        fake.script_cancel(request.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(request.order_id, at=AT + timedelta(seconds=62)))
    assert ledger.snapshot().capital.total == D("0")
    replacement = engine.decide((moved,), now=AT + timedelta(seconds=62))
    assert quotes(replacement) == {"m-yes": Quote("m", "m-yes", D("0.46"), D("10")),
                                   "m-no": Quote("m", "m-no", D("0.46"), D("10"))}
    assert replacement.cancel == replacement.merge == ()
    assert all(r.created_at == AT + timedelta(seconds=62) and r.expires_at == AT + timedelta(hours=1, seconds=62)
               for r in replacement.place)
    new_orders = submit(ledger, fake, replacement)
    assert ledger.snapshot().capital.total == D("10")
    higher = replace(moved, books=(Book("m", "m-yes", D("0.49"), D("0.51"), AT),
                                  Book("m", "m-no", D("0.49"), D("0.51"), AT)))
    assert engine.decide((higher,), now=AT + timedelta(seconds=121)) == Decision()
    later = engine.decide((higher,), now=AT + timedelta(seconds=122))
    assert later.place == later.merge == ()
    assert {c.order_id for c in later.cancel} == {r.order.order_id for r in new_orders.values()}
    assert ledger.snapshot().capital.total == D("10")


def test_replacement_waits_for_cancel():
    ledger, fake, engine = world()
    inputs = plan(no="0.40")
    inputs = replace(inputs, market=replace(inputs.market, rewards_max_spread=2.0))
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    assert engine.decide((inputs,), now=AT + timedelta(seconds=60)) == Decision()
    moved = replace(inputs, books=(Book("m", "m-yes", D("0.46"), D("0.48"), AT),
                                   Book("m", "m-no", D("0.46"), D("0.48"), AT)))
    cancelled = engine.decide((moved,), now=AT + timedelta(seconds=61))
    assert cancelled.place == cancelled.merge == () and len(cancelled.cancel) == 2
    for token, outcome, confirmed in (("m-yes", Outcome.SUCCESS, True), ("m-no", Outcome.UNKNOWN, False)):
        fake.script_cancel(orders[token].order.order_id, outcome, cancelled=confirmed)
        ledger.apply_order(fake.cancel_order(orders[token].order.order_id, at=AT + timedelta(seconds=62)))
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((moved,), now=AT + timedelta(seconds=63)) == Decision()
    recovered = Ledger(OWNER, D("100"))
    recovered.reconcile(fake)
    restarted = Engine(recovered)
    assert restarted.decide((moved,), now=AT + timedelta(hours=1, seconds=1)) == Decision()
    assert recovered.snapshot().capital.total == D("10")
    fake.script_cancel(orders["m-no"].order.order_id, Outcome.SUCCESS, cancelled=True)
    result = fake.cancel_order(orders["m-no"].order.order_id, at=AT + timedelta(hours=1, seconds=2))
    recovered.apply_order(result)
    assert recovered.snapshot().capital.total == D("0")
    replacement = restarted.decide((moved,), now=AT + timedelta(hours=1, seconds=3))
    assert quotes(replacement) == {"m-yes": Quote("m", "m-yes", D("0.46"), D("10")),
                                   "m-no": Quote("m", "m-no", D("0.46"), D("10"))}
    assert replacement.cancel == replacement.merge == ()
    assert all(o.remaining_quantity == 0 for o in recovered.snapshot().orders)
    assert recovered.snapshot().capital.total == D("10")


@pytest.mark.parametrize("reason", ["reward_band", "safety", "crossing", "expiry", "valid"])
def test_requote_reasons(reason):
    ledger, fake, engine = world()
    inputs = plan(no="0.55" if reason in ("safety", "expiry", "valid") else "0.40")
    if reason == "reward_band":
        inputs = replace(inputs, market=replace(inputs.market, rewards_max_spread=2.0))
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    now = AT + timedelta(seconds=60)
    affected = {orders["m-yes"].order.order_id}
    if reason == "reward_band":
        inputs = replace(inputs, books=(Book("m", "m-yes", D("0.46"), D("0.48"), now), inputs.books[1]))
    elif reason == "crossing":
        inputs = replace(inputs, books=(Book("m", "m-yes", D("0.38"), D("0.40"), now), inputs.books[1]))
    elif reason == "safety":
        ledger.apply_fill(fake.execute_fill(orders["m-yes"].order.order_id, trade_id="higher-actual-fee", quantity=D("10"),
                                            price=D("0.40"), fee_usdc=D("0.30"), at=AT + timedelta(seconds=1)))
        assert ledger.snapshot().capital.inventory_cost == D("4.30")
        affected = {orders["m-no"].order.order_id}
    elif reason == "expiry":
        assert engine.decide((inputs,), now=AT + timedelta(seconds=3539)) == Decision()
        now = AT + timedelta(seconds=3540)
        affected = {r.order.order_id for r in orders.values()}
    elif reason == "valid":
        affected = set()
    decision = engine.decide((inputs,), now=now)
    assert decision.place == decision.merge == ()
    assert {c.order_id for c in decision.cancel} == affected
    assert len(decision.cancel) == len(affected)
    assert ledger.snapshot().capital.total == D("10")
    assert engine.decide((inputs,), now=now + timedelta(seconds=1)) == Decision()
    if reason == "safety":
        cancel = decision.cancel[0]
        fake.script_cancel(cancel.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(cancel.order_id, at=now + timedelta(seconds=1)))
        inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), now)))
        replacement = engine.decide((inputs,), now=now + timedelta(seconds=2))
        assert quotes(replacement) == {"m-no": Quote("m", "m-no", D("0.54"), D("10"))}
        assert replacement.cancel == replacement.merge == ()
        assert ledger.snapshot().capital.total == D("10")
        submit(ledger, fake, replacement)
        assert engine.decide((inputs,), now=now + timedelta(seconds=62)) == Decision()
        assert ledger.snapshot().capital.total == D("10")


@pytest.mark.parametrize("matched", [False, True], ids=["hedge", "merge"])
def test_hold_limit_keeps_recovery(matched):
    ledger, fake, engine = world()
    inputs = plan()
    orders = submit(ledger, fake, engine.decide((inputs,), now=AT))
    for identity, quantity, fee, at in (("old-four", "4", "0.04", AT + timedelta(seconds=1)),
                                        ("recent-two", "2", "0.02", AT + timedelta(minutes=30))):
        ledger.apply_fill(fake.execute_fill(orders["m-yes"].order.order_id, trade_id=identity, quantity=D(quantity),
                                            price=D("0.40"), fee_usdc=D(fee), at=at))
    if matched:
        ledger.apply_fill(fake.execute_fill(orders["m-no"].order.order_id, trade_id="no-six", quantity=D("6"),
                                            price=D("0.55"), fee_usdc=D("0.06"), at=AT + timedelta(minutes=30)))
    for result in orders.values():
        fake.script_cancel(result.order.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(result.order.order_id, at=AT + timedelta(minutes=31)))
    assert ledger.snapshot().capital.total == D("6")
    assert ledger.oldest_holding("m") == AT + timedelta(seconds=1)
    assert next(p for p in ledger.snapshot().positions if p.token_id == "m-yes").updated_at == AT + timedelta(minutes=30)
    ledger.reconcile(fake)
    assert ledger.oldest_holding("m") == AT + timedelta(seconds=1)
    now = AT + timedelta(hours=24, seconds=2)
    inputs = replace(inputs, books=(inputs.books[0], Book("m", "m-no", D("0.60"), D("0.61"), now)))
    decision = engine.decide((inputs,), now=now)
    assert decision.cancel == ()
    if matched:
        assert decision.place == () and len(decision.merge) == 1
        assert decision.merge[0].quantity == D("6")
    else:
        assert quotes(decision) == {"m-no": Quote("m", "m-no", D("0.56"), D("6"))}
        assert decision.merge == ()
    assert ledger.snapshot().capital.total == D("6")
    assert engine.decide((inputs,), now=now + timedelta(seconds=1)) == Decision()
    assert ledger.oldest_holding("m") == AT + timedelta(seconds=1)
    if matched:
        merge = decision.merge[0]
        fake.script_merge(merge, Outcome.SUCCESS)
        ledger.apply_merge(fake.merge(merge, at=now))
        ledger.apply_merge(fake.receive_merge(merge, at=now + timedelta(seconds=2)))
        ledger.reconcile(fake)
        assert ledger.snapshot().capital.total == D("0")
        assert ledger.oldest_holding("m") is None
        fresh_inputs = plan()
        reopened = engine.decide((fresh_inputs,), now=now + timedelta(seconds=3))
        assert quotes(reopened) == {"m-yes": Quote("m", "m-yes", D("0.40"), D("10")),
                                    "m-no": Quote("m", "m-no", D("0.55"), D("10"))}
        assert reopened.cancel == reopened.merge == ()
        new_orders = submit(ledger, fake, reopened)
        new_at = now + timedelta(seconds=4)
        ledger.apply_fill(fake.execute_fill(new_orders["m-yes"].order.order_id, trade_id="new-generation-one", quantity=D("1"),
                                            price=D("0.40"), fee_usdc=D("0.01"), at=new_at))
        assert ledger.oldest_holding("m") == new_at
        assert ledger.snapshot().capital.total == D("10")


def test_ranking_refresh_interval():
    ledger, fake, engine = world(cap="100")
    a = plan("a", quantity="100", no="0.40", reward=100.0)
    b = plan("b", quantity="100", no="0.40", reward=10.0)
    initial = engine.decide((a, b), now=AT)
    assert quotes(initial) == {"a-yes": Quote("a", "a-yes", D("0.40"), D("100")),
                               "a-no": Quote("a", "a-no", D("0.40"), D("100"))}
    assert initial.cancel == initial.merge == ()
    assert ledger.snapshot().capital.total == D("100")
    orders = submit(ledger, fake, initial)
    for result in orders.values():
        fake.script_cancel(result.order.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(result.order.order_id, at=AT + timedelta(seconds=1)))
    assert ledger.snapshot().capital.total == D("0")
    b = replace(b, market=replace(b.market, rewards_daily_rate=1000.0))
    cached = engine.decide((a, b), now=AT + timedelta(seconds=599))
    assert quotes(cached) == {"a-yes": Quote("a", "a-yes", D("0.40"), D("100")),
                              "a-no": Quote("a", "a-no", D("0.40"), D("100"))}
    assert cached.cancel == cached.merge == ()
    assert ledger.snapshot().capital.total == D("100")
    orders = submit(ledger, fake, cached)
    for result in orders.values():
        fake.script_cancel(result.order.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(result.order.order_id, at=AT + timedelta(seconds=599, microseconds=1)))
    assert ledger.snapshot().capital.total == D("0")
    refreshed = engine.decide((a, b), now=AT + timedelta(seconds=600))
    assert quotes(refreshed) == {"b-yes": Quote("b", "b-yes", D("0.40"), D("100")),
                                 "b-no": Quote("b", "b-no", D("0.40"), D("100"))}
    assert refreshed.cancel == refreshed.merge == ()
    assert ledger.snapshot().capital.total == D("100")
    orders = submit(ledger, fake, refreshed)
    for result in orders.values():
        fake.script_cancel(result.order.order_id, Outcome.SUCCESS, cancelled=True)
        ledger.apply_order(fake.cancel_order(result.order.order_id, at=AT + timedelta(seconds=601)))
    b = replace(b, market=replace(b.market, in_play=True))
    eligible_only = engine.decide((a, b), now=AT + timedelta(seconds=602))
    assert quotes(eligible_only) == {"a-yes": Quote("a", "a-yes", D("0.40"), D("100")),
                                     "a-no": Quote("a", "a-no", D("0.40"), D("100"))}
    assert eligible_only.cancel == eligible_only.merge == ()
    assert ledger.snapshot().capital.total == D("100")
    empty, _, scanner = world()
    nonbinary = replace(a, market=replace(a.market, tokens=(a.market.tokens[0],)))
    assert scanner.decide((nonbinary,), now=AT) == Decision()
    assert empty.snapshot().capital.total == D("0")


@pytest.mark.parametrize("cap,yes,no", [("10", "0.40", "0.40"), ("12", "0.70", "0.25")], ids=["capital", "projected_unpaired"])
def test_global_admission_updates_reservations(cap, yes, no):
    ledger, fake, engine = world(cap=cap)
    first = plan("a", quantity="6", yes=yes, no=no, reward=100.0)
    second = plan("b", quantity="6", yes=yes, no=no, reward=10.0)
    first = replace(first, market=replace(first.market, rewards_min_size=D("5")))
    second = replace(second, market=replace(second.market, rewards_min_size=D("5")))
    decision = engine.decide((first, second), now=AT)
    assert quotes(decision) == {"a-yes": Quote("a", "a-yes", D(yes), D("6")),
                                "a-no": Quote("a", "a-no", D(no), D("6"))}
    assert decision.cancel == decision.merge == ()
    assert ledger.snapshot().capital.total == D("6")
    assert len(ledger.snapshot().pending) == 2
    assert projected_unpaired_cost(ledger.snapshot()) == (D("2.46") if cap == "10" else D("4.26"))
    assert engine.decide((first, second), now=AT + timedelta(seconds=1)) == Decision()
    assert ledger.snapshot().capital.total == D("6")
    orders = submit(ledger, fake, decision)
    assert projected_unpaired_cost(ledger.snapshot()) == (D("2.46") if cap == "10" else D("4.26"))
    assert engine.decide((first, second), now=AT + timedelta(seconds=2)) == Decision()
    ledger.apply_fill(fake.execute_fill(orders["a-yes"].order.order_id, trade_id="partial-risk", quantity=D("2"),
                                        price=D(yes), fee_usdc=D("0.02"), at=AT + timedelta(seconds=3)))
    assert ledger.snapshot().capital.inventory_cost == (D("0.82") if cap == "10" else D("1.42"))
    assert projected_unpaired_cost(ledger.snapshot()) == (D("2.46") if cap == "10" else D("4.26"))
    assert engine.decide((first, second), now=AT + timedelta(seconds=4)) == Decision()
    assert ledger.snapshot().capital.total == D("6")
