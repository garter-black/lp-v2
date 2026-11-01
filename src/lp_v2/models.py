"""Immutable facts and intents; all times are supplied by the caller."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OrderStatus(StrEnum):
    LIVE = "live"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Token:
    token_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    tokens: tuple[Token, ...]
    tick_size: Decimal
    min_order_size: Decimal
    rewards_min_size: Decimal
    # Float references retain the upstream scoring inputs, not executable prices.
    best_bid: float
    best_ask: float
    liquidity_num: float
    volume_24hr: float
    rewards_daily_rate: float
    rewards_max_spread: float
    rebate_rate: float
    taker_fee_bps: int
    fees_enabled: bool
    rewards_enabled: bool
    political: bool
    in_play: bool


@dataclass(frozen=True, slots=True)
class Book:
    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Quote:
    condition_id: str
    token_id: str
    price: Decimal
    quantity: Decimal
    side: Literal["BUY"] = field(default="BUY", init=False)
    post_only: Literal[True] = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    request_id: str
    owner: str
    quote: Quote
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    request: OrderRequest
    filled_quantity: Decimal
    remaining_quantity: Decimal  # Live residual; zero after final cancellation.
    status: OrderStatus
    observed_at: datetime
    authoritative: bool


@dataclass(frozen=True, slots=True)
class OrderResult:
    request: OrderRequest
    outcome: Outcome
    order: Order | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: str
    order_id: str
    request_id: str
    owner: str
    condition_id: str
    token_id: str
    quantity: Decimal
    price: Decimal
    fee_usdc: Decimal
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class Position:
    owner: str
    condition_id: str
    token_id: str
    quantity: Decimal
    cost_usdc: Decimal
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Balance:
    usdc: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MergeRequest:
    merge_id: str
    owner: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    quantity: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MergeResult:
    request: MergeRequest
    outcome: Outcome
    received_usdc: Decimal  # Acknowledgement alone is not a wallet receipt.
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CapitalBreakdown:
    inventory_cost: Decimal
    open_buys: Decimal
    pending_requests: Decimal
    hedge_reserve: Decimal
    fee_reserve: Decimal
