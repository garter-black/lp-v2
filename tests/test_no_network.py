import socket

import pytest

from tests.conftest import SocketNetworkForbidden


def test_socket_network_is_forbidden():
    with pytest.raises(SocketNetworkForbidden, match="socket networking is forbidden"):
        with socket.socket():
            pass
