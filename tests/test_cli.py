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


@pytest.mark.parametrize("value", [None, "abc", "0", "-5", "nan", "inf", "-inf"])
def test_validation_precedes_server(value, monkeypatch, capsys):
    from lp_v2 import cli, status_server

    constructed = []

    def server_boundary(*args):
        constructed.append(args)
        raise AssertionError("invalid CLI input must not construct a server")

    monkeypatch.setattr(status_server, "HTTPServer", server_boundary)
    argv = ["run"] if value is None else ["run", "--capital-usdc", value]
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code != 0
    assert constructed == []
    diagnostic = capsys.readouterr().err
    assert "--capital-usdc" in diagnostic
    if value is None:
        assert "required" in diagnostic
    elif value == "-inf":
        assert "expected one argument" in diagnostic
    else:
        assert "error: argument --capital-usdc:" in diagnostic
        assert "finite positive" in diagnostic


def test_run_starts_read_only_server(monkeypatch, capsys):
    from lp_v2 import cli, status_server
    from tests.test_status_server import request, original_fields
    from tests.test_readonly_runtime import ScriptedReads

    configs, replies, events = [], [], []

    class ServerBoundary:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8765)
            self.handler = handler
            events.append("construct")

        def __enter__(self):
            return self

        def serve_forever(self):
            events.append("serve")
            replies.append(request(self.handler))

        def __exit__(self, *exception):
            events.append("close")

    def serving_boundary(config):
        configs.append(config)
        status_server.serve(config)

    monkeypatch.setattr(status_server, "create_readonly_sdk", lambda config, **kwargs: ScriptedReads())
    monkeypatch.setattr(status_server, "HTTPServer", ServerBoundary)
    monkeypatch.setattr(cli, "serve", serving_boundary, raising=False)
    before = datetime.now(UTC)
    assert cli.main(["run", "--capital-usdc", "100"]) == 0
    after = datetime.now(UTC)
    assert len(configs) == 1
    config = configs[0]
    assert isinstance(config, cli.RunConfig) and config.capital_usdc == Decimal("100")
    assert before <= config.started_at <= after
    assert config.started_at.utcoffset() == timedelta(0)
    with pytest.raises(AttributeError):
        config.capital_usdc = Decimal("200")
    assert events == ["construct", "serve", "close"]
    assert len(replies) == 1
    status, headers, body = replies[0]
    assert status == 200 and headers.get_content_type() == "application/json"
    assert original_fields(body) == {
        "version": "0.1.0", "capital_usdc": "100", "started_at": config.started_at.isoformat(),
        "capital": {"inventory_cost": "0", "open_buys": "0", "pending_requests": "0",
                    "hedge_reserve": "0", "fee_reserve": "0", "total": "0"},
    }
    assert "Startup capital: 100 USDC" in capsys.readouterr().out
