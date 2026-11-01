"""Pure capital accounting over complete, owner-scoped reservation snapshots."""

from collections.abc import Sequence
from decimal import Decimal

from lp_v2.models import (
    CapitalBreakdown, CapitalReservation, MergeResult, Order, OrderRequest, OrderStatus, Outcome, Position,
)


def _amounts(*values: Decimal) -> None:
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in values):
        raise ValueError("capital amounts must be finite nonnegative Decimals")


def _unique(records, key):
    facts = {}
    for record in records:
        identity = key(record)
        if identity in facts and facts[identity] != record:
            raise ValueError("conflicting facts for one identity; supply a current snapshot")
        facts[identity] = record
    return facts


def capital_breakdown(
    owner: str,
    *,
    reservations: Sequence[CapitalReservation],
    positions: Sequence[Position],
    orders: Sequence[Order],
    pending: Sequence[OrderRequest],
    merge_results: Sequence[MergeResult],
) -> CapitalBreakdown:
    if not owner:
        raise ValueError("owner is required")
    allocations = _unique((r for r in reservations if r.owner == owner), lambda r: r.condition_id)
    holdings = _unique((p for p in positions if p.owner == owner), lambda p: (p.condition_id, p.token_id))
    known = _unique((o for o in orders if o.request.owner == owner), lambda o: o.request.request_id)
    requests = _unique((r for r in pending if r.owner == owner), lambda r: r.request_id)
    receipts = _unique((r for r in merge_results if r.request.owner == owner), lambda r: r.request.merge_id)
    if len({order.order_id for order in known.values()}) != len(known):
        raise ValueError("order identity cannot identify two requests")
    for reservation in allocations.values():
        _amounts(reservation.planned_quantity, reservation.remaining_fee_usdc)
        if (not reservation.condition_id or len(reservation.token_ids) != 2
                or len(set(reservation.token_ids)) != 2 or not all(reservation.token_ids)
                or not all(reservation.request_ids)
                or len(set(reservation.request_ids)) != len(reservation.request_ids)):
            raise ValueError("invalid reservation identities")

    def allocation_for(condition_id: str, token_id: str) -> CapitalReservation:
        if condition_id not in allocations or token_id not in allocations[condition_id].token_ids:
            raise ValueError("owned fact has no matching reservation")
        return allocations[condition_id]

    for position in holdings.values():
        allocation_for(position.condition_id, position.token_id)
        _amounts(position.quantity, position.cost_usdc)
    for request in (*requests.values(), *(order.request for order in known.values())):
        reservation = allocation_for(request.quote.condition_id, request.quote.token_id)
        if request.request_id not in reservation.request_ids:
            raise ValueError("owned request is not tracked by its reservation")
        _amounts(request.quote.quantity, request.quote.price)
        if not 0 < request.quote.price < 1:
            raise ValueError("invalid order price")
    for key, order in known.items():
        _amounts(order.filled_quantity, order.remaining_quantity)
        if order.status not in (OrderStatus.LIVE, OrderStatus.CANCELLED, OrderStatus.FILLED):
            raise ValueError("unknown order state must be represented explicitly")
        if order.authoritative and order.status != OrderStatus.LIVE and order.remaining_quantity != 0:
            raise ValueError("terminal order cannot retain a live residual")
        if order.filled_quantity + order.remaining_quantity > order.request.quote.quantity:
            raise ValueError("order quantities exceed original intent")
        if key in requests and requests[key] != order.request:
            raise ValueError("pending alias differs from known order intent")
    for result in receipts.values():
        _amounts(result.request.quantity, result.received_usdc)
        if result.received_usdc and (result.outcome != Outcome.SUCCESS or result.received_usdc != result.request.quantity):
            raise ValueError("receipt must confirm the exact successful merge quantity")

    totals = dict.fromkeys(("inventory_cost", "open_buys", "pending_requests", "hedge_reserve", "fee_reserve"), Decimal("0"))
    for cid, reservation in allocations.items():
        market_orders = [o for o in known.values() if o.request.quote.condition_id == cid]
        filled = {token: sum((o.filled_quantity for o in market_orders if o.request.quote.token_id == token), Decimal("0"))
                  for token in reservation.token_ids}
        merged = Decimal("0")
        for merge in _unique(reservation.merge_requests, lambda r: r.merge_id).values():
            _amounts(merge.quantity)
            if (merge.owner != owner or merge.condition_id != cid or merge.quantity == 0
                    or (merge.yes_token_id, merge.no_token_id) != reservation.token_ids):
                raise ValueError("merge intent does not match reservation")
            result = receipts.get(merge.merge_id)
            if result is not None:
                if result.request != merge:
                    raise ValueError("merge receipt identity differs from reserved intent")
                if result.outcome == Outcome.SUCCESS:
                    merged += result.received_usdc
        if merged > min(filled.values()):
            raise ValueError("settlement exceeds recorded matched pairs")
        potential = []
        for token in reservation.token_ids:
            held_quantity = holdings[(cid, token)].quantity if (cid, token) in holdings else Decimal("0")
            live_quantity = sum((o.remaining_quantity for o in market_orders if o.request.quote.token_id == token
                                 and (o.status == OrderStatus.LIVE or not o.authoritative)), Decimal("0"))
            pending_quantity = sum((r.quote.quantity for key, r in requests.items() if key not in known
                                    and r.quote.condition_id == cid and r.quote.token_id == token), Decimal("0"))
            potential.append(max(held_quantity, filled[token] - merged) + live_quantity + pending_quantity)
        remaining_pairs = max(potential)
        tracked = [known.get(key) for key in reservation.request_ids]
        closed = bool(tracked) and all(
            order is not None and order.request.quote.condition_id == cid and order.authoritative
            and order.status in (OrderStatus.CANCELLED, OrderStatus.FILLED) for order in tracked
        )
        closed = closed and {order.request.quote.token_id for order in tracked} == set(reservation.token_ids)
        reserved_pairs = remaining_pairs if closed else max(reservation.planned_quantity, remaining_pairs)
        inventory = sum((p.cost_usdc for p in holdings.values() if p.condition_id == cid), Decimal("0"))
        open_buys = sum((o.remaining_quantity * o.request.quote.price for o in known.values()
                         if o.request.quote.condition_id == cid
                         and (o.status == OrderStatus.LIVE or not o.authoritative)), Decimal("0"))
        pending_cost = sum((r.quote.quantity * r.quote.price for key, r in requests.items()
                            if r.quote.condition_id == cid and key not in known), Decimal("0"))
        fees = reservation.remaining_fee_usdc
        hedge = max(Decimal("0"), reserved_pairs - inventory - open_buys - pending_cost - fees)
        totals["inventory_cost"] += inventory
        totals["open_buys"] += open_buys
        totals["pending_requests"] += pending_cost
        totals["hedge_reserve"] += hedge
        totals["fee_reserve"] += fees
    return CapitalBreakdown(**totals, total=sum(totals.values(), Decimal("0")))


def can_open_market(
    *, startup_cap: Decimal, occupied: Decimal, planned_shares: Decimal,
    projected_market_unpaired: Decimal, current_unpaired_cost: Decimal, proposed_unpaired_cost: Decimal,
) -> bool:
    _amounts(startup_cap, occupied, planned_shares, projected_market_unpaired,
             current_unpaired_cost, proposed_unpaired_cost)
    return (
        startup_cap > 0 and planned_shares > 0
        and occupied + planned_shares <= startup_cap
        and projected_market_unpaired <= planned_shares
        and current_unpaired_cost + proposed_unpaired_cost <= startup_cap / 2
    )


def recovery_action_allowed(action: str, *, covered_hedge: bool) -> bool:
    """Recovery uses existing coverage; it never admits a new allocation."""
    return action in ("merge", "exit") or (action == "hedge" and covered_hedge is True)
