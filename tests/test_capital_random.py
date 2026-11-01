"""500 fixed seeds, each >=200 legal events across three repeating market lifecycles.

Exchange execution order is causal; only notification delivery is shuffled. Empty
engine rounds/replays do not count as events. Fees are explicitly .01/share.
"""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json
from pprint import pformat
from random import Random

import pytest

from lp_v2.engine import Engine
from lp_v2.ledger import Ledger
from lp_v2.models import (
    Balance, Book, CancelRequest, Decision, Fill, Market, MarketPlan, MergeRequest, MergeResult,
    Order, OrderResult, OrderStatus, Outcome, Token,
)
from tests.fakes.fake_sdk import FakeSDK


SEEDS = tuple(range(20261102, 20261102 + 500))
START = datetime(2026, 11, 2, tzinfo=UTC)
CAP = D("30")
MIN_EVENTS, MAX_EVENTS = 200, 400
REQUIRED = {
    "decision", "place_success", "place_unknown", "place_rejected", "place_resolved",
    "partial_fill", "full_fill", "late_fill_notice", "duplicate_fill", "stale_order_notice",
    "cancel_unknown", "cancel_confirmed", "merge_success", "merge_failed", "merge_unknown",
    "merge_resolved", "merge_receipt", "reconcile",
}


class Sequence:
    def __init__(self, seed):
        self.seed, self.rng = seed, Random(seed)
        self.owner = f"lp-v2:seed:{seed}"
        self.fake = FakeSDK(balance=Balance(D("100"), START))
        self.ledger = Ledger(self.owner, CAP, balance=self.fake.read_balance())
        self.engine = Engine(self.ledger)
        self.plans = []
        for cid in ("a", "b", "c"):
            market = Market(cid, (Token(cid + "-yes", "Yes"), Token(cid + "-no", "No")),
                            D("0.01"), D("1"), D("10"), .49, .51, 2000., 10000., 100., 5., .25, 400,
                            True, True, True, False, True)
            bids = (self.rng.choice((D("0.40"), D("0.45"))), self.rng.choice((D("0.40"), D("0.45"))))
            books = tuple(Book(cid, token.token_id, bid, bid + D("0.02"), START) for token, bid in zip(market.tokens, bids))
            self.plans.append(MarketPlan(market, books, D("10"), (D("0.01"), D("0.01"))))
        self.state = self.ledger.snapshot()
        self.tick = 0
        self.history, self.identities = [], []
        self.counts = Counter()
        self.place_queue, self.cancel_queue, self.merge_queue, self.notices = [], [], [], []
        self.acks, self.delivered_fills = [], []
        self.deferred = set()
        self.cancel_followup = set()
        self.seen_actions = {"place": set(), "cancel": set(), "merge": set()}
        self.need_decision = True
        self.receipt_pending = False

    def now(self):
        return START + timedelta(seconds=self.tick)

    def check(self, before, *, conserve=False):
        self.state = self.ledger.snapshot()
        c = self.state.capital
        components = (c.inventory_cost, c.open_buys, c.pending_requests, c.hedge_reserve, c.fee_reserve)
        assert all(v.is_finite() and v >= 0 for v in components)
        assert sum(components, D("0")) == c.total
        assert c.total <= CAP
        if conserve:
            assert c.total == before, "fill or uncertain/unreceived event changed occupied capital"
        actual_orders = [o for o in self.fake.read_orders() if isinstance(o, Order) and o.request.owner == self.owner]
        actual_positions = [p for p in self.fake.read_positions() if p.owner == self.owner]
        lower_bound = sum((p.cost_usdc for p in actual_positions), D("0"))
        lower_bound += sum((o.remaining_quantity * o.request.quote.price for o in actual_orders if o.status == OrderStatus.LIVE), D("0"))
        assert c.total >= lower_bound, "actual inventory plus live BUY notional exceeds reserved funds"
        actual_ids = {o.request.request_id for o in actual_orders}
        actual_receipts = {r.request.merge_id for r in self.fake.read_balance().merge_results if r.received_usdc}
        failed_merges = {r.request.merge_id for r in self.state.merges if r.outcome == Outcome.FAILED}
        for plan in self.plans:
            cid = plan.market.condition_id
            quantities = {t.token_id: D("0") for t in plan.market.tokens}
            for p in actual_positions:
                if p.condition_id == cid:
                    quantities[p.token_id] += p.quantity
            held = dict(quantities)
            for o in actual_orders:
                if o.request.quote.condition_id == cid and o.status == OrderStatus.LIVE:
                    quantities[o.request.quote.token_id] += o.remaining_quantity
            for request in self.state.pending:
                if request.quote.condition_id == cid and request.request_id not in actual_ids:
                    quantities[request.quote.token_id] += request.quote.quantity
            assert max(quantities.values()) <= D("10"), "duplicate or unadmitted per-market BUY coverage"
            reserved_merges = sum((r.quantity for allocation in self.state.reservations if allocation.condition_id == cid
                                   for r in allocation.merge_requests
                                   if r.merge_id not in actual_receipts and r.merge_id not in failed_merges), D("0"))
            assert reserved_merges <= min(held.values()), "two merge identities consume the same actual inventory"
        assert len({f.trade_id for f in self.state.fills}) == len(self.state.fills)
        assert len({r.request_id for r in self.state.requests}) == len(self.state.requests)

    def event(self, kind, details, action, *, conserve=False):
        self.tick += self.rng.randint(1, 4)
        before = self.state.capital.total
        self.history.append((len(self.history), self.tick, kind, details))
        result = action()
        self.check(before, conserve=conserve)
        self.counts[kind] += 1
        return result

    def apply(self, fact):
        if isinstance(fact, Fill):
            self.ledger.apply_fill(fact)
            if fact not in self.delivered_fills:
                self.delivered_fills.append(fact)
        elif isinstance(fact, OrderResult):
            self.ledger.apply_order(fact)
        else:
            self.ledger.apply_merge(fact)
        self.need_decision = True

    def enqueue_decision(self, decision):
        for kind, actions, queue in (("place", decision.place, self.place_queue),
                                     ("cancel", decision.cancel, self.cancel_queue),
                                     ("merge", decision.merge, self.merge_queue)):
            for request in actions:
                identity = request.request_id if kind == "place" else request.order_id if kind == "cancel" else request.merge_id
                assert identity not in self.seen_actions[kind], "engine replayed an action identity"
                self.seen_actions[kind].add(identity)
                self.identities.append((kind, request))
                queue.append(request)

    def decide(self):
        before = self.state.capital.total
        self.history.append((len(self.history), self.tick, "decision", ()))
        decision = self.engine.decide(self.plans, now=self.now())
        self.need_decision = False
        if decision == Decision():
            self.history.pop()
            assert self.ledger.snapshot() == self.state
            return
        self.history[-1] = (*self.history[-1][:3], decision)
        self.enqueue_decision(decision)
        self.check(before)
        assert self.state.capital.total >= before
        self.counts["decision"] += 1
        assert self.engine.decide(self.plans, now=self.now()) == Decision(), "immediate decision replay emitted more actions"
        assert self.ledger.snapshot() == self.state

    def candidates(self):
        actual = [o for o in self.fake.read_orders() if isinstance(o, Order)]
        observed = {o.order_id: o for o in self.state.orders}
        candidates = []
        if self.place_queue:
            candidates.extend([("place", None)] * 5)
        if self.cancel_queue:
            candidates.extend([("cancel", None)] * 5)
        if self.merge_queue:
            candidates.extend([("merge", None)] * 5)
        for result in self.fake.read_orders():
            if isinstance(result, OrderResult) and result.outcome == Outcome.UNKNOWN:
                candidates.extend([("resolve_place", result)] * 4)
        for result in self.fake.read_balance().merge_results:
            if result.outcome == Outcome.UNKNOWN:
                candidates.extend([("resolve_merge", result)] * 4)
            elif result.outcome == Outcome.SUCCESS and not result.received_usdc:
                candidates.extend([("receipt", result)] * 4)
        for order in actual:
            if order.status == OrderStatus.LIVE and self.now() < order.request.expires_at and order.order_id not in self.deferred:
                candidates.extend([("fill", order)] * 3)
            known = observed.get(order.order_id)
            if order.order_id in self.deferred and known is None:
                candidates.append(("order_readback", order))
            if (order.status == OrderStatus.LIVE and known is not None
                    and order.order_id not in {c.order_id for c in self.state.cancels}):
                candidates.extend([("cancel_intent", order)] * (8 if order.order_id in self.deferred else 1))
        candidates.extend(("confirm_cancel", oid) for oid in sorted(self.cancel_followup))
        for index, fact in enumerate(self.notices):
            if isinstance(fact, Fill) and fact.order_id in self.deferred:
                known = observed.get(fact.order_id)
                if known is None or not known.authoritative or known.status != OrderStatus.CANCELLED:
                    continue
            candidates.extend([("notice", index)] * 3)
        if self.receipt_pending:
            candidates.extend([("reconcile", None)] * 5)
        if self.delivered_fills and self.counts["duplicate_fill"] < max(1, len(self.history) // 40):
            candidates.append(("duplicate", self.rng.choice(self.delivered_fills)))
        stale = [r for r in self.acks if r.order is not None and r.order.order_id in observed
                 and observed[r.order.order_id].observed_at > r.order.observed_at]
        if stale and self.counts["stale_order_notice"] < max(1, len(self.history) // 40):
            candidates.append(("stale", self.rng.choice(stale)))
        # A bounded test actor may submit a new ID only after an actual definitive failure.
        for allocation in self.state.reservations:
            if any(r.request.condition_id == allocation.condition_id and r.outcome == Outcome.FAILED for r in self.state.merges):
                quantity = self.ledger.mergeable_quantity(allocation.condition_id)
                if quantity > 0:
                    candidates.append(("merge_intent", (allocation, quantity)))
        return candidates

    def step(self, choice):
        kind, item = choice
        if kind == "place":
            request = self.place_queue.pop(self.rng.randrange(len(self.place_queue)))
            number = self.counts["place_success"] + self.counts["place_unknown"] + self.counts["place_rejected"]
            outcome = (Outcome.SUCCESS, Outcome.SUCCESS, Outcome.UNKNOWN, Outcome.SUCCESS, Outcome.FAILED)[number % 5]
            def submit():
                self.fake.script_place(request, outcome)
                fact = self.fake.place_order(request, at=self.now())
                self.acks.append(fact)
                if self.rng.choice((True, False)):
                    self.apply(fact)
                else:
                    self.notices.append(fact)
            self.event("place_" + {Outcome.SUCCESS: "success", Outcome.UNKNOWN: "unknown", Outcome.FAILED: "rejected"}[outcome],
                       request.request_id, submit, conserve=outcome != Outcome.FAILED)
        elif kind == "resolve_place":
            outcome = Outcome.SUCCESS if self.counts["place_resolved"] % 3 else Outcome.FAILED
            self.event("place_resolved", (item.request.request_id, outcome),
                       lambda: self.apply(self.fake.resolve_place(item.request, outcome, at=self.now())), conserve=outcome != Outcome.FAILED)
        elif kind == "fill":
            remaining = int(item.remaining_quantity)
            partial = remaining > 1 and (not self.counts["partial_fill"] or self.rng.choice((True, False)))
            quantity = D(self.rng.randint(1, remaining - 1) if partial else remaining)
            price = item.request.quote.price - self.rng.choice((D("0"), D("0.01")))
            identity = f"trade-{len(self.fake.read_trades())}"
            delayed = partial and (not self.counts["late_fill_notice"] or self.rng.randrange(4) == 0)
            def execute():
                fact = self.fake.execute_fill(item.order_id, trade_id=identity, quantity=quantity, price=price,
                                              fee_usdc=quantity * D("0.01"), at=self.now(), deliver=False)
                if delayed:
                    self.deferred.add(item.order_id)
                    self.notices.append(fact)
                elif self.rng.choice((True, False)):
                    self.apply(fact)
                else:
                    self.notices.append(fact)
            self.event("partial_fill" if partial else "full_fill", (item.order_id, identity, quantity, price, delayed), execute, conserve=True)
        elif kind == "order_readback":
            self.event("order_readback", item.order_id,
                       lambda: self.apply(OrderResult(item.request, Outcome.SUCCESS, item, self.now())))
        elif kind == "cancel_intent":
            self.tick = max(self.tick, int((item.request.created_at - START).total_seconds()) + 60)
            def register():
                request = CancelRequest(item.order_id, item.request.request_id, self.owner, item.request.quote.condition_id, self.now())
                assert self.ledger.register_cancel(request)
                self.cancel_queue.append(request)
                assert item.order_id not in self.seen_actions["cancel"]
                self.seen_actions["cancel"].add(item.order_id)
                self.identities.append(("cancel", request))
            self.event("cancel_intent", item.order_id, register, conserve=True)
        elif kind == "cancel":
            request = self.cancel_queue.pop(self.rng.randrange(len(self.cancel_queue)))
            number = self.counts["cancel_unknown"] + self.counts["cancel_confirmed"]
            outcome = Outcome.UNKNOWN if number % 3 != 2 else Outcome.SUCCESS
            cancelled = self.rng.choice((True, False)) if outcome == Outcome.UNKNOWN else True
            def execute():
                self.fake.script_cancel(request.order_id, outcome, cancelled=cancelled)
                self.apply(self.fake.cancel_order(request.order_id, at=self.now()))
                if outcome == Outcome.UNKNOWN:
                    self.cancel_followup.add(request.order_id)
            self.event("cancel_unknown" if outcome == Outcome.UNKNOWN else "cancel_confirmed", (request.order_id, cancelled), execute,
                       conserve=outcome == Outcome.UNKNOWN)
        elif kind == "confirm_cancel":
            def confirm():
                self.fake.script_cancel(item, Outcome.SUCCESS, cancelled=True)
                self.apply(self.fake.cancel_order(item, at=self.now()))
                self.cancel_followup.remove(item)
            self.event("cancel_confirmed", item, confirm)
        elif kind == "notice":
            fact = self.notices.pop(item)
            late = isinstance(fact, Fill) and fact.order_id in self.deferred
            if late:
                terminal = next(o for o in self.state.orders if o.order_id == fact.order_id)
                assert fact.executed_at < terminal.observed_at and terminal.status == OrderStatus.CANCELLED
            event_kind = "late_fill_notice" if late else "fill_notice" if isinstance(fact, Fill) else "order_notice"
            conserve = isinstance(fact, Fill) or (isinstance(fact, OrderResult) and fact.outcome == Outcome.UNKNOWN)
            self.event(event_kind, fact.trade_id if isinstance(fact, Fill) else fact.request.request_id, lambda: self.apply(fact), conserve=conserve)
            if late:
                self.deferred.remove(fact.order_id)
        elif kind in ("duplicate", "stale"):
            before = self.state
            self.event("duplicate_fill" if kind == "duplicate" else "stale_order_notice",
                       item.trade_id if kind == "duplicate" else item.request.request_id, lambda: self.apply(item), conserve=True)
            # A first late ACK may upgrade an order inferred solely from a fill.
            expected = before if kind == "duplicate" else replace(before, orders=tuple(
                replace(o, authoritative=True) if o.order_id == item.order.order_id else o for o in before.orders))
            assert self.state == expected
        elif kind == "merge_intent":
            allocation, quantity = item
            def register():
                request = MergeRequest(f"explicit-merge-{len(self.history)}", self.owner, allocation.condition_id,
                                       *allocation.token_ids, quantity, self.now(), self.ledger.reservation_terms(allocation.condition_id).generation)
                assert self.ledger.register_merge(request)
                self.merge_queue.append(request)
                self.identities.append(("merge", request))
            self.event("merge_intent", (allocation.condition_id, quantity), register, conserve=True)
        elif kind == "merge":
            request = self.merge_queue.pop(self.rng.randrange(len(self.merge_queue)))
            number = self.counts["merge_success"] + self.counts["merge_failed"] + self.counts["merge_unknown"]
            outcome = (Outcome.SUCCESS, Outcome.FAILED, Outcome.UNKNOWN)[number % 3]
            def execute():
                self.fake.script_merge(request, outcome)
                self.apply(self.fake.merge(request, at=self.now()))
            self.event("merge_" + outcome.value, (request.merge_id, request.quantity), execute, conserve=True)
        elif kind == "resolve_merge":
            outcome = Outcome.SUCCESS if self.counts["merge_resolved"] % 3 != 2 else Outcome.FAILED
            self.event("merge_resolved", (item.request.merge_id, outcome),
                       lambda: self.apply(self.fake.resolve_merge(item.request, outcome, at=self.now())), conserve=True)
        elif kind == "receipt":
            def receive():
                self.apply(self.fake.receive_merge(item.request, at=self.now()))
                self.receipt_pending = True
            self.event("merge_receipt", item.request.merge_id, receive, conserve=True)
        elif kind == "reconcile":
            self.event("reconcile", "complete SDK facts", lambda: self.ledger.reconcile(self.fake))
            self.receipt_pending = False
            self.need_decision = True
        else:
            raise AssertionError(kind)

    def run(self):
        try:
            while len(self.history) < MAX_EVENTS:
                if self.need_decision:
                    self.decide()
                generations = {(r.quote.condition_id, r.reservation_terms.generation) for r in self.state.requests}
                repeated = all(any(cid == p.market.condition_id and generation >= 1 for cid, generation in generations) for p in self.plans)
                if len(self.history) >= MIN_EVENTS and REQUIRED <= self.counts.keys() and repeated:
                    break
                choices = self.candidates()
                assert choices, "no legal transition remains before required coverage"
                self.step(self.rng.choice(choices))
            assert len(self.history) >= MIN_EVENTS
            assert REQUIRED <= self.counts.keys(), REQUIRED - self.counts.keys()
            assert repeated, "each owned market must execute more than one lifecycle"
            assert self.counts["duplicate_fill"] + self.counts["stale_order_notice"] < len(self.history) // 5
            return (self.state, self.fake.read_orders(), self.fake.read_trades(), self.fake.read_positions(),
                    self.fake.read_balance(), tuple(self.identities), tuple(self.history), tuple(sorted(self.counts.items())))
        except Exception as error:
            error.add_note(f"seed={self.seed}; complete ordered replay history:\n{pformat(self.history, width=140)}")
            raise


def run_sequence(seed):
    return Sequence(seed).run()


@pytest.mark.parametrize("seed", SEEDS, ids=str)
def test_random_capital_invariants(seed, record_property):
    result = run_sequence(seed)
    record_property("seed", seed)
    record_property("events", len(result[-2]))
    record_property("event_families", json.dumps(dict(result[-1]), sort_keys=True))


def test_sequence_is_reproducible():
    assert run_sequence(SEEDS[0]) == run_sequence(SEEDS[0])


def test_cumulative_hedge_fill_does_not_request_duplicate_cover():
    from tests.test_scenarios import plan, quotes, submit, world

    ledger, fake, engine = world()
    inputs = plan(no="0.40")
    opening = engine.decide((inputs,), now=START)
    orders = submit(ledger, fake, opening, outcomes={"m-yes": Outcome.FAILED})
    ledger.apply_fill(fake.execute_fill(orders["m-no"].order.order_id, trade_id="held-eight", quantity=D("8"),
                                        price=D("0.40"), fee_usdc=D("0.08"), at=START + timedelta(seconds=1)))
    fake.script_cancel(orders["m-no"].order.order_id, Outcome.SUCCESS, cancelled=True)
    ledger.apply_order(fake.cancel_order(orders["m-no"].order.order_id, at=START + timedelta(seconds=60)))
    hedge = engine.decide((inputs,), now=START + timedelta(seconds=61))
    assert {q.token_id: q.quantity for q in quotes(hedge).values()} == {"m-yes": D("8")}
    hedge_order = submit(ledger, fake, hedge)["m-yes"].order
    delayed = fake.execute_fill(hedge_order.order_id, trade_id="matched-seven", quantity=D("7"), price=D("0.40"),
                                fee_usdc=D("0.07"), at=START + timedelta(seconds=62), deliver=False)
    fake.script_cancel(hedge_order.order_id, Outcome.UNKNOWN, cancelled=False)
    ledger.apply_order(fake.cancel_order(hedge_order.order_id, at=START + timedelta(seconds=121)))
    assert ledger.snapshot().capital.total == D("8")
    assert all(p.token_id != "m-yes" for p in ledger.snapshot().positions)
    assert engine.decide((inputs,), now=START + timedelta(seconds=122)) == Decision()
    fake.script_cancel(hedge_order.order_id, Outcome.SUCCESS, cancelled=True)
    ledger.apply_order(fake.cancel_order(hedge_order.order_id, at=START + timedelta(seconds=123)))
    remainder = engine.decide((inputs,), now=START + timedelta(seconds=124))
    assert [(q.token_id, q.price, q.quantity) for q in quotes(remainder).values()] == [("m-yes", D("0.40"), D("1"))]
    assert remainder.cancel == remainder.merge == ()
    assert ledger.snapshot().capital.total == D("8")
    assert engine.decide((inputs,), now=START + timedelta(seconds=125)) == Decision()
    ledger.apply_fill(delayed)
    decision = engine.decide((inputs,), now=START + timedelta(seconds=126))
    assert decision.place == decision.cancel == ()
    assert len(decision.merge) == 1 and decision.merge[0].quantity == D("7")
    assert ledger.snapshot().capital.total == D("8")


def test_full_merge_consumes_exact_remaining_cost():
    from tests.test_scenarios import plan, submit, world

    ledger, fake, engine = world()
    inputs = plan(quantity="7", no="0.40")
    inputs = replace(inputs, market=replace(inputs.market, rewards_min_size=D("1")))
    orders = submit(ledger, fake, engine.decide((inputs,), now=START))
    for identity, token, quantity, price, fee, second in (
        ("better-one", "m-yes", "1", "0.39", "0.01", 1),
        ("remaining-six", "m-yes", "6", "0.40", "0.06", 2),
        ("no-seven", "m-no", "7", "0.40", "0.07", 2),
    ):
        ledger.apply_fill(fake.execute_fill(orders[token].order.order_id, trade_id=identity, quantity=D(quantity),
                                            price=D(price), fee_usdc=D(fee), at=START + timedelta(seconds=second)))
    assert ledger.snapshot().capital.inventory_cost == D("5.73")
    for identity, quantity, second in (("merge-one", "1", 3), ("merge-six", "6", 5)):
        request = MergeRequest(identity, ledger.owner, "m", "m-yes", "m-no", D(quantity), START + timedelta(seconds=second))
        assert ledger.register_merge(request)
        fake.script_merge(request, Outcome.SUCCESS)
        ledger.apply_merge(fake.merge(request, at=request.created_at))
        receipt = fake.receive_merge(request, at=request.created_at + timedelta(seconds=1))
        if quantity == "6":
            assert {p.token_id: p.cost_usdc for p in receipt.consumed_positions} == {
                "m-yes": D("2.451428571428571428571428571"), "m-no": D("2.46"),
            }
            assert all(p.quantity == 0 and p.cost_usdc == 0 for p in fake.read_positions())
        ledger.apply_merge(receipt)
        ledger.reconcile(fake)
    assert ledger.snapshot().capital.total == D("0")
    assert ledger.snapshot().cash == D("101.27")
    assert ledger.snapshot().realized_profit == D("1.27")


def test_reopened_inventory_preserves_execution_order_costs():
    from tests.test_scenarios import plan, submit, world

    ledger, fake, engine = world()
    inputs = plan(quantity="7", no="0.40")
    inputs = replace(inputs, market=replace(inputs.market, rewards_min_size=D("1")))
    orders = submit(ledger, fake, engine.decide((inputs,), now=START))
    for identity, token, quantity, price, fee, second in (
        ("yes-one", "m-yes", "1", "0.39", "0.01", 1),
        ("yes-six", "m-yes", "6", "0.40", "0.06", 2),
        ("no-seven", "m-no", "7", "0.40", "0.07", 2),
    ):
        ledger.apply_fill(fake.execute_fill(orders[token].order.order_id, trade_id=identity, quantity=D(quantity),
                                            price=D(price), fee_usdc=D(fee), at=START + timedelta(seconds=second)))
    for identity, quantity, second in (("first", "1", 3), ("last", "6", 5)):
        request = MergeRequest(identity, ledger.owner, "m", "m-yes", "m-no", D(quantity), START + timedelta(seconds=second))
        assert ledger.register_merge(request)
        fake.script_merge(request, Outcome.SUCCESS)
        ledger.apply_merge(fake.merge(request, at=request.created_at))
        ledger.apply_merge(fake.receive_merge(request, at=request.created_at + timedelta(seconds=1)))
        ledger.reconcile(fake)
    assert ledger.snapshot().capital.total == D("0")
    inputs = replace(inputs, quantity=D("20"))
    reopened = submit(ledger, fake, engine.decide((inputs,), now=START + timedelta(seconds=7)))
    fill = fake.execute_fill(reopened["m-yes"].order.order_id, trade_id="reopened-twenty", quantity=D("20"),
                             price=D("0.40"), fee_usdc=D("0.20"), at=START + timedelta(seconds=8))
    ledger.apply_fill(fill)
    assert next(p for p in ledger.snapshot().positions if p.token_id == "m-yes").cost_usdc == D("8.20")
    ledger.reconcile(fake)
    before = ledger.snapshot()
    assert before.capital.total == D("20")
    assert before.cash == D("93.07")
    recovered = Ledger(ledger.owner, D("100"))
    recovered.reconcile(fake)
    assert recovered.snapshot() == before
