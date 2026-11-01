"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class RunConfig:
    capital_usdc: Decimal
    started_at: datetime


def _positive_capital(value: str) -> Decimal:
    try:
        capital = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a finite positive number") from None
    if not capital.is_finite() or capital <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return capital


def parse_run(argv: Sequence[str] | None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="lp-v2")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--capital-usdc", required=True, type=_positive_capital)
    args = parser.parse_args(argv)
    return RunConfig(capital_usdc=args.capital_usdc, started_at=datetime.now(UTC))


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_run(argv)
    # shortcut: returns after validation; step 11 adds the status server.
    print(
        f"Startup capital: {config.capital_usdc} USDC; "
        f"started at {config.started_at.isoformat()}"
    )
    return 0
