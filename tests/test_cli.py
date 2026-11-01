from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lp_v2.cli import parse_run


def test_rejects_missing_capital(capsys):
    with pytest.raises(SystemExit) as error:
        parse_run(["run"])

    assert error.value.code != 0
    diagnostic = capsys.readouterr().err
    assert "--capital-usdc" in diagnostic
    assert "required" in diagnostic


@pytest.mark.parametrize("value", ["abc", "0", "-5", "nan", "inf", "-inf"])
def test_rejects_invalid_capital(value, capsys):
    with pytest.raises(SystemExit) as error:
        parse_run(["run", "--capital-usdc", value])

    assert error.value.code != 0
    diagnostic = capsys.readouterr().err
    assert "error: argument --capital-usdc:" in diagnostic
    if value == "-inf":
        assert "expected one argument" in diagnostic
    else:
        assert "finite positive" in diagnostic


def test_accepts_positive_capital():
    before = datetime.now(UTC)
    config = parse_run(["run", "--capital-usdc", "100"])
    after = datetime.now(UTC)

    assert isinstance(config.capital_usdc, Decimal)
    assert config.capital_usdc == Decimal("100")
    assert config.started_at.tzinfo is not None
    assert config.started_at.utcoffset() == timedelta(0)
    assert before <= config.started_at <= after
    with pytest.raises(AttributeError):
        config.capital_usdc = Decimal("200")
    with pytest.raises(AttributeError):
        config.started_at = before
