"""
TwistedTransport end-to-end against a LOCAL TLS server (127.0.0.1, no internet).

Proves the reactor-thread isolation actually works with the real SDK:
Deferred→Future bridging, the auth sequence over the wire, heartbeats every
`heartbeat_interval`, certificate verification, and reconnect + re-auth after
the server drops the connection.
"""

from __future__ import annotations

import datetime as _dt
import logging
import socket
import ssl as pyssl
import struct
import threading
import time

import pytest

pytest.importorskip("ctrader_open_api")
pytest.importorskip("twisted")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from ctrader_open_api import Protobuf  # noqa: E402
from ctrader_open_api.messages import OpenApiCommonMessages_pb2 as C  # noqa: E402
from ctrader_open_api.messages import OpenApiMessages_pb2 as M  # noqa: E402
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as MM  # noqa: E402

from core.broker import BrokerConnectivityError  # noqa: E402
from core.brokers.ctrader import CTraderAdapter, CTraderCredentials, TwistedTransport  # noqa: E402

ACC = 777001
ENV = {"CTRADER_CLIENT_ID": "id", "CTRADER_CLIENT_SECRET": "sec", "CTRADER_ACCESS_TOKEN": "tok",
       "CTRADER_REFRESH_TOKEN": "ref", "CTRADER_ACCOUNT_ID": str(ACC)}


def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "c.pem").write_bytes(cert_pem)
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_pem, str(tmp_path / "c.pem"), str(tmp_path / "k.pem")


class FakeCTraderServer:
    def __init__(self, certfile, keyfile):
        self.ctx = pyssl.SSLContext(pyssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received: list[str] = []
        self.unanswered: set[str] = set()   # request names the server swallows (hang the client)
        self.heartbeats = 0
        self.connections = 0
        self._conn = None
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop:
            try:
                raw, _ = self.sock.accept()
                conn = self.ctx.wrap_socket(raw, server_side=True)
            except OSError:
                continue
            self.connections += 1
            self._conn = conn
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _recv_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _serve(self, conn):
        try:
            while True:
                (n,) = struct.unpack("!I", self._recv_exact(conn, 4))
                msg = C.ProtoMessage()
                msg.ParseFromString(self._recv_exact(conn, n))
                if msg.payloadType == 51:
                    self.heartbeats += 1
                    continue
                req = Protobuf.extract(msg)
                self.received.append(type(req).__name__)
                res = None if type(req).__name__ in self.unanswered else self._respond(req)
                if res is not None:
                    out = C.ProtoMessage(payloadType=res.payloadType, payload=res.SerializeToString(),
                                         clientMsgId=msg.clientMsgId).SerializeToString()
                    conn.sendall(struct.pack("!I", len(out)) + out)
        except (ConnectionError, OSError, pyssl.SSLError):
            pass

    @staticmethod
    def _respond(req):
        name = type(req).__name__
        if name == "ProtoOAApplicationAuthReq":
            return M.ProtoOAApplicationAuthRes()
        if name == "ProtoOAGetAccountListByAccessTokenReq":
            return M.ProtoOAGetAccountListByAccessTokenRes(accessToken="tok", ctidTraderAccount=[
                MM.ProtoOACtidTraderAccount(ctidTraderAccountId=ACC, isLive=False)])
        if name == "ProtoOAAccountAuthReq":
            return M.ProtoOAAccountAuthRes(ctidTraderAccountId=ACC)
        if name == "ProtoOASymbolsListReq":
            return M.ProtoOASymbolsListRes(ctidTraderAccountId=ACC, symbol=[
                MM.ProtoOALightSymbol(symbolId=1, symbolName="EURUSD")])
        if name == "ProtoOASymbolByIdReq":
            return M.ProtoOASymbolByIdRes(ctidTraderAccountId=ACC, symbol=[
                MM.ProtoOASymbol(symbolId=1, digits=5, pipPosition=4, minVolume=100, stepVolume=100)])
        if name == "ProtoOAReconcileReq":
            return M.ProtoOAReconcileRes(ctidTraderAccountId=ACC)
        return None

    def drop(self):
        if self._conn is not None:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._conn.close()

    def close(self):
        self._stop = True
        self.drop()
        self.sock.close()


def _wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server(tmp_path):
    cert_pem, certfile, keyfile = _self_signed(tmp_path)
    srv = FakeCTraderServer(certfile, keyfile)
    srv.cert_pem = cert_pem
    yield srv
    srv.close()


def test_real_reactor_auth_heartbeat_and_reconnect(server):
    from twisted.internet import ssl as tssl

    opts = tssl.optionsForClientTLS("localhost", trustRoot=tssl.Certificate.loadPEM(server.cert_pem))
    transport = TwistedTransport("localhost", server.port, heartbeat_interval=0.5, tls_options=opts)
    adapter = CTraderAdapter(CTraderCredentials.from_env(ENV), account_equity=4000, transport=transport,
                             connect_timeout=10, request_timeout=5)
    try:
        adapter.connect()
        assert adapter.health()
        assert server.received[:3] == ["ProtoOAApplicationAuthReq", "ProtoOAGetAccountListByAccessTokenReq",
                                       "ProtoOAAccountAuthReq"]
        assert adapter.symbol_map == {"EURUSD": ("EURUSD", 1)}

        assert _wait(lambda: server.heartbeats >= 2, 5), "no heartbeats sent"

        server.drop()
        assert _wait(lambda: server.connections >= 2 and adapter.auth_count >= 2 and adapter.health(), 25), \
            "did not reconnect + re-authorize"
        assert server.received.count("ProtoOAAccountAuthReq") >= 2
    finally:
        adapter.disconnect()


def _connected_adapter(server, request_timeout=5):
    from twisted.internet import ssl as tssl

    opts = tssl.optionsForClientTLS("localhost", trustRoot=tssl.Certificate.loadPEM(server.cert_pem))
    transport = TwistedTransport("localhost", server.port, heartbeat_interval=0.5, tls_options=opts)
    adapter = CTraderAdapter(CTraderCredentials.from_env(ENV), account_equity=4000, transport=transport,
                             connect_timeout=10, request_timeout=request_timeout)
    adapter.connect()
    assert adapter.health()
    return adapter, transport


def _ctrader_errors(caplog):
    return [r for r in caplog.records if r.name == "broker.ctrader" and r.levelno >= logging.ERROR]


def test_disconnect_cancels_reauth_that_is_mid_request(server, caplog):
    """The race behind the smoke test's post-disconnect error: the link drops,
    the SDK reconnects, re-auth starts and waits on the server, and THEN the
    script calls disconnect(). The re-auth must end inside disconnect(), not
    up to request_timeout later, and must not log an error."""
    adapter, transport = _connected_adapter(server, request_timeout=8)
    try:
        server.unanswered.add("ProtoOAApplicationAuthReq")
        server.drop()
        assert _wait(lambda: server.received.count("ProtoOAApplicationAuthReq") >= 2, 15), \
            "re-auth never reached the server"
    finally:
        t0 = time.monotonic()
        adapter.disconnect()
        elapsed = time.monotonic() - t0
    time.sleep(0.5)              # anything still running would log now
    assert elapsed < 3, f"disconnect() took {elapsed:.1f}s"
    assert not adapter._auth_lock.locked(), "re-auth still running after disconnect()"
    assert _ctrader_errors(caplog) == []
    assert adapter.auth_count == 1 and not adapter.authorized


def test_disconnect_cancels_reauth_waiting_for_a_reconnect(server, caplog):
    """Re-auth queued by a server event while the venue is unreachable: its
    request waits for a connection that never comes. disconnect() must cancel
    it (a CancelledError underneath) without an error or a traceback."""
    adapter, transport = _connected_adapter(server)
    server._stop = True
    server.sock.close()          # refuse every reconnect attempt
    server.drop()
    assert _wait(lambda: not transport.is_connected, 5)
    reauth = threading.Thread(
        target=adapter._on_message, args=(M.ProtoOAAccountDisconnectEvent(ctidTraderAccountId=ACC),), daemon=True,
    )
    reauth.start()
    assert _wait(adapter._auth_lock.locked, 5), "re-auth never started"
    time.sleep(0.2)              # let it reach request() and block on the missing connection
    adapter.disconnect()
    reauth.join(3)
    assert not reauth.is_alive(), "re-auth still running after disconnect()"
    assert _ctrader_errors(caplog) == []


def test_untrusted_certificate_is_refused(server):
    # Platform trust does not include our self-signed cert → TLS must fail.
    transport = TwistedTransport("localhost", server.port, heartbeat_interval=0.5)
    adapter = CTraderAdapter(CTraderCredentials.from_env(ENV), account_equity=4000, transport=transport,
                             connect_timeout=3)
    try:
        with pytest.raises(BrokerConnectivityError):
            adapter.connect()
        assert "ProtoOAApplicationAuthReq" not in server.received   # token never sent
    finally:
        adapter.disconnect()
