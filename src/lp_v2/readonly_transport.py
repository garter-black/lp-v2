"""Instance-local audit at httpx's final synchronous dispatch boundary.

Pinned PublicClient 0.2.0 has no transport constructor argument. Private SDK
and httpx access is isolated here; incompatible wiring disables adapter reads.
"""
from collections import Counter
from dataclasses import fields
from datetime import UTC, datetime
from threading import Lock

import httpx

UNKNOWN = 'UNKNOWN'


class ReadOnlyTransportAudit:
    def __init__(self, public, http):
        self.__lock = Lock()
        self.__started_at = datetime.now(UTC)
        self.__dispatched = Counter()
        self.__rejected = 0
        self.__ready = False
        try:
            from polymarket import PublicClient
            from polymarket.clients._transport import SyncTransport
            if not isinstance(public, PublicClient):
                return
            context = public._ctx
            if {f.name for f in fields(context)} != {'environment', 'gamma', 'data', 'rfq', 'clob'}:
                return
            clients = [http]
            for name in ('gamma', 'data', 'rfq', 'clob'):
                transport = getattr(context, name)
                if not isinstance(transport, SyncTransport):
                    return
                clients.append(transport._client)
            clients = list({id(client): client for client in clients}.values())
            for client in clients:
                if (not isinstance(client, httpx.Client) or not isinstance(client._mounts, dict)
                        or not self.__supported(client._transport)
                        or any(t is not None and not self.__supported(t) for t in client._mounts.values())):
                    return
            wrappers = {}
            def guard(transport):
                if id(transport) not in wrappers:
                    wrappers[id(transport)] = _AuditedTransport(transport, self)
                return wrappers[id(transport)]
            for client in clients:
                client._transport = guard(client._transport)
                client._mounts = {pattern: guard(t) if t is not None else None
                                  for pattern, t in client._mounts.items()}
            self.__ready = True
        except (AttributeError, TypeError, ValueError):
            # No partially known instrumentation may look measured clean.
            pass

    @staticmethod
    def __supported(transport):
        return isinstance(transport, httpx.BaseTransport) and not isinstance(transport, _AuditedTransport)

    def require_ready(self):
        if not self.__ready:
            raise RuntimeError('read-only transport audit unavailable')

    def dispatch(self, request):
        self.require_ready()
        with self.__lock:
            if request.method != 'GET':
                self.__rejected += 1
                raise RuntimeError('read-only transport rejected non-GET')
            self.__dispatched[request.method] += 1

    def snapshot(self):
        with self.__lock:
            return dict(get_attempts=self.__dispatched['GET'] if self.__ready else UNKNOWN,
                        rejected_non_get_attempts=self.__rejected if self.__ready else UNKNOWN,
                        status=('violation' if self.__rejected else 'ok') if self.__ready else UNKNOWN,
                        monitor_started_at=self.__started_at if self.__ready else UNKNOWN)

    def safety(self, since):
        with self.__lock:
            return dict(orders_sent=self.__dispatched['POST'] if self.__ready else UNKNOWN,
                        cancels_sent=self.__dispatched['DELETE'] if self.__ready else UNKNOWN,
                        since=since)


class _AuditedTransport(httpx.BaseTransport):
    def __init__(self, inner, audit):
        self.__inner, self.__audit = inner, audit

    def handle_request(self, request):
        self.__audit.dispatch(request)
        return self.__inner.handle_request(request)

    def close(self):
        self.__inner.close()
