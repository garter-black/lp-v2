"""Shared test boundaries."""

import socket

import pytest


class SocketNetworkForbidden(RuntimeError):
    """A test attempted to use socket networking."""


@pytest.fixture(autouse=True)
def no_socket_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise SocketNetworkForbidden("socket networking is forbidden in tests")

    for name in ("__init__", "connect", "connect_ex", "bind", "listen", "accept",
                 "send", "sendall", "sendto", "recv", "recvfrom", "recv_into", "recvfrom_into"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, forbidden)
