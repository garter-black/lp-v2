"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from lp_v2.status_server import serve


@dataclass(frozen=True, slots=True)
class RunConfig:
    capital_usdc: Decimal
    started_at: datetime
    code_sha: str = "UNKNOWN"
    credentials: object = field(default=None, repr=False, compare=False)


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
    run.add_argument('--code-sha', default='UNKNOWN')
    run.add_argument('--credentials-file', help='Explicit owner-only JSON with existing L2 credentials; never auto-discovered')
    args = parser.parse_args(argv)
    import re
    if args.code_sha != 'UNKNOWN' and not re.fullmatch('[0-9a-f]{40}', args.code_sha):
        parser.error('--code-sha must be an exact 40-character SHA')
    credentials = None
    if args.credentials_file:
        import json, os, stat
        from lp_v2.readonly_sdk import L2Credentials
        try:
            fd = os.open(args.credentials_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                    raise ValueError('permissions')
                credentials = L2Credentials(**json.load(f))
        except Exception:
            parser.error('cannot load explicit owner-only L2 credential file')
    return RunConfig(capital_usdc=args.capital_usdc, started_at=datetime.now(UTC), code_sha=args.code_sha, credentials=credentials)


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_run(argv)
    print(
        f"Startup capital: {config.capital_usdc} USDC; "
        f"started at {config.started_at.isoformat()}"
    )
    serve(config)
    return 0
