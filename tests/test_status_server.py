from email.parser import BytesParser
from dataclasses import FrozenInstanceError
from io import BytesIO
import json
from decimal import Decimal

import pytest

from lp_v2.cli import RunConfig, parse_run
from lp_v2.models import Balance
from tests.fakes.fake_sdk import FakeSDK
from tests.test_ledger import AT, T1, place_pair, prepared_ledger


class MemoryConnection:
    """Only the transport boundary is replaced; the HTTP parser/handler are real."""
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, mode, buffering=-1):
        assert mode == "rb"
        return self.input

    def sendall(self, data):
        self.output.write(data)


def request(handler, method="GET", path="/summary", payload=b""):
    wire = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
    connection = MemoryConnection(wire)
    handler(connection, ("127.0.0.1", 12345), None)
    headers, body = connection.output.getvalue().split(b"\r\n\r\n", 1)
    status, fields = headers.split(b"\r\n", 1)
    headers = BytesParser().parsebytes(fields)
    decoded = json.loads(body) if body and headers.get_content_type() == "application/json" else body or None
    return int(status.split()[1]), headers, decoded


def test_get_status_json():
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, requests = prepared_ledger()
    handler = make_handler(config, ledger)
    before = ledger.snapshot()
    status, headers, body = request(handler, path="/healthz")
    assert status == 200 and headers.get_content_type() == "application/json"
    assert body == {"version": "0.1.0", "status": "ok"}
    status, headers, body = request(handler)
    assert status == 200 and headers.get_content_type() == "application/json"
    assert body == {
        "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
        "capital": {"inventory_cost": "0", "open_buys": "0", "pending_requests": "9.50",
                    "hedge_reserve": "0.30", "fee_reserve": "0.20", "total": "10.00"},
    }
    assert ledger.snapshot() == before

    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    for result in (yes, no):
        ledger.apply_order(result)
    ledger.apply_fill(fake.execute_fill(yes.order.order_id, trade_id="status-four", quantity=Decimal("4"),
                                       price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1))
    before = ledger.snapshot()
    status, headers, current = request(handler)
    assert status == 200 and headers.get_content_type() == "application/json"
    assert current == {
        "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
        "capital": {"inventory_cost": "1.64", "open_buys": "7.90", "pending_requests": "0",
                    "hedge_reserve": "0.30", "fee_reserve": "0.16", "total": "10.00"},
    }
    assert ledger.snapshot() == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "BREW"])
@pytest.mark.parametrize("path", ["/healthz", "/summary"])
def test_rejects_non_get_methods(method, path):
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, _ = prepared_ledger()
    handler = make_handler(config, ledger)
    before = ledger.snapshot()
    for target in (path, path + "?capital_usdc=200"):
        status, headers, body = request(handler, method, target, b'{"capital_usdc": "200"}')
        assert status == 405
        assert headers["Allow"] == "GET"
        assert headers.get_content_type() == "application/json"
        assert body is None if method == "HEAD" else body == {"error": "method not allowed"}
        assert ledger.snapshot() == before
        assert config == RunConfig(Decimal("100"), AT)
        assert request(handler)[2]["capital_usdc"] == "100"


def test_unknown_get_is_not_found():
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, _ = prepared_ledger()
    before = ledger.snapshot()
    handler = make_handler(config, ledger)
    for path in ("/missing", "/config?capital_usdc=200", "/summary?capital_usdc=200"):
        status, headers, body = request(handler, path=path)
        assert status == 404 and headers.get_content_type() == "application/json"
        assert body == {"error": "not found"}
    assert ledger.snapshot() == before
    assert config == RunConfig(Decimal("100"), AT)


@pytest.mark.parametrize("interrupt", [False, True])
def test_loopback_binding(monkeypatch, interrupt):
    from lp_v2 import status_server
    from lp_v2.status_server import serve

    events = []
    config = RunConfig(Decimal("100"), AT)

    class ServerBoundary:
        def __init__(self, address, handler):
            events.append(("bind", address))
            status, _, body = request(handler)
            assert status == 200
            assert body == {
                "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
                "capital": {"inventory_cost": "0", "open_buys": "0", "pending_requests": "0",
                            "hedge_reserve": "0", "fee_reserve": "0", "total": "0"},
            }

        def __enter__(self):
            return self

        def serve_forever(self):
            events.append("serve")
            if interrupt:
                raise KeyboardInterrupt

        def __exit__(self, *exception):
            events.append("close")

    monkeypatch.setattr(status_server, "HTTPServer", ServerBoundary)
    assert serve(config) is None
    assert events == [("bind", ("127.0.0.1", 8765)), "serve", "close"]


def test_startup_config_is_immutable(tmp_path):
    from lp_v2.status_server import make_handler

    path = tmp_path / "capital.txt"
    path.write_text("100")
    config = parse_run(["run", "--capital-usdc", path.read_text()])
    ledger, _ = prepared_ledger()
    handler = make_handler(config, ledger)
    status, _, before = request(handler)
    assert status == 200 and before["capital_usdc"] == "100"
    assert before["started_at"] == config.started_at.isoformat()
    path.write_text("200")
    with pytest.raises(FrozenInstanceError):
        config.capital_usdc = Decimal("200")
    with pytest.raises(FrozenInstanceError):
        config.started_at = AT
    assert request(handler, "POST", "/config?capital_usdc=200", b'{"capital_usdc":"200"}')[0] == 405
    assert request(handler, path="/config?capital_usdc=200")[0] == 404
    status, _, after = request(handler)
    assert status == 200 and after == before
