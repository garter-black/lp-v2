"""Read-only local status from frozen startup configuration and current ledger facts."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError, version
import json
from typing import TYPE_CHECKING

from lp_v2.ledger import Ledger

if TYPE_CHECKING:
    from lp_v2.cli import RunConfig

try:
    VERSION = version("lp-v2")
except PackageNotFoundError:
    VERSION = "0+unknown"


def make_handler(config: "RunConfig", ledger: Ledger) -> type[BaseHTTPRequestHandler]:
    class StatusHandler(BaseHTTPRequestHandler):
        def parse_request(self):
            if not super().parse_request():
                return False
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return False
            return True

        def do_GET(self):
            if self.path == "/healthz":
                self._send_json(200, {"version": VERSION, "status": "ok"})
            elif self.path == "/summary":
                self._send_json(200, {
                    "version": VERSION,
                    "capital_usdc": str(config.capital_usdc),
                    "started_at": config.started_at.isoformat(),
                    "capital": {name: str(value) for name, value in asdict(ledger.snapshot().capital).items()},
                })
            else:
                self._send_json(404, {"error": "not found"})

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return StatusHandler


def serve(config: "RunConfig") -> None:
    ledger = Ledger("lp-v2:status", config.capital_usdc)
    with HTTPServer(("127.0.0.1", 8765), make_handler(config, ledger)) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
