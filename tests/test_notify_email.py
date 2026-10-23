"""
Tests for v1.2.0 Part 6 — e-mail, through an SMTP server.

``EmailNotifier`` sits on the notifier core and sends with the standard library's
``smtplib``: HTML with a plain-text alternative, pictures inline, files attached, trade
messages gathered into a digest (Q6.3 b), and threads by ``In-Reply-To`` without one (Q6.5
a).  The server is written here — Python 3.12 has no ``smtpd`` — on 127.0.0.1: EHLO,
STARTTLS, AUTH, MAIL, RCPT, DATA and QUIT.  Its TLS runs with a throwaway certificate made by
``cryptography``, and those tests skip without it.  No real mail server is ever touched.
"""
from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import ipaddress
import socket
import socketserver
import ssl
import threading
import time

import pytest
from test_notify import FakeClock, settle
from test_notify_events import (
    LIVE,
    LONG,
    SEED,
    FastClock,
    Plan,
    SteppedBroker,
    at,
    candle,
    drained,
    ms,
    paper_cfg,
)
from test_notify_pictures import PICTURE, FakeEngine, backtest
from test_trader import _PaperBroker

from AlgoTradeKit.notify import (
    KIND_CLOSE,
    KIND_OPEN,
    KIND_SIGNAL,
    EmailError,
    EmailNotifier,
    EmailRecipient,
)
from AlgoTradeKit.trader import TraderPair, run_live
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module
from AlgoTradeKit.visual import _snapshot

USER = "ada@example.com"
PASSWORD = "app-pass-1234"
SIGNAL_TEXT = ("<b>🟢 LONG BTCUSDT · 1h</b>\nEntry 64,250 · SL 63,800 · TP 65,150\nRR 2.00\n"
               "🕑 2026-10-14 14:00 UTC · SIM")


# ---------------------------------------------------------------------------
# The fake SMTP server
# ---------------------------------------------------------------------------

class FakeSMTP:
    """An SMTP server for the tests, on 127.0.0.1.  It records every command and every
    e-mail, and ``when()`` programs an answer for a command.  With ``tls=`` it offers
    STARTTLS; with ``implicit=True`` it speaks TLS from the first byte, as port 465 does."""

    def __init__(self, *, tls: ssl.SSLContext | None = None, implicit: bool = False) -> None:
        self.tls, self.implicit = tls, implicit
        self.users = {USER: PASSWORD}
        self.lock = threading.Lock()
        self.reset()
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    fake._session(self.request)
                except (OSError, ssl.SSLError):
                    pass

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def reset(self) -> None:
        with self.lock:
            self.mails: list[dict] = []
            self.commands: list[str] = []
            self.rules: list[dict] = []
            self.logins = 0

    def when(self, verb: str, code: int = 0, text: str = "", *, times: int = 1,
             delay: float = 0.0) -> None:
        with self.lock:
            self.rules.append({"verb": verb, "code": code, "text": text, "times": times,
                               "delay": delay})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _rule(self, verb: str) -> dict | None:
        for rule in self.rules:
            if rule["verb"] == verb and rule["times"]:
                rule["times"] -= 1
                return rule
        return None

    def messages(self) -> list[email.message.EmailMessage]:
        with self.lock:
            return [mail["message"] for mail in self.mails]

    def _session(self, sock: socket.socket) -> None:
        if self.implicit:
            assert self.tls is not None
            sock = self.tls.wrap_socket(sock, server_side=True)
        state = {"sock": sock, "file": sock.makefile("rb"), "tls": self.implicit}

        def send(line: str) -> None:
            state["sock"].sendall(line.encode() + b"\r\n")

        def read() -> str | None:
            raw = state["file"].readline()
            return None if not raw else raw.decode("utf-8", "replace").rstrip("\r\n")

        send("220 fake.smtp ESMTP ready")
        mail_from, rcpts = None, []
        while True:
            line = read()
            if line is None:
                return
            verb = line.split(" ", 1)[0].upper()
            arg = line[len(verb):].strip()
            with self.lock:
                self.commands.append("AUTH …" if verb == "AUTH" else line)
                rule = self._rule(verb)
            if rule is not None and rule["delay"]:
                time.sleep(rule["delay"])
            if rule is not None and rule["code"]:
                send(f"{rule['code']} {rule['text']}")
                continue
            if verb in ("EHLO", "HELO"):
                lines = ["fake.smtp", "SIZE 35882577", "8BITMIME", "AUTH PLAIN LOGIN"]
                if self.tls is not None and not state["tls"]:
                    lines.append("STARTTLS")
                for index, item in enumerate(lines):
                    send(("250 " if index == len(lines) - 1 else "250-") + item)
            elif verb == "STARTTLS":
                if self.tls is None or state["tls"]:
                    send("502 5.5.1 no TLS here")
                    continue
                send("220 2.0.0 Ready to start TLS")
                wrapped = self.tls.wrap_socket(state["sock"], server_side=True)
                state.update(sock=wrapped, file=wrapped.makefile("rb"), tls=True)
            elif verb == "AUTH":
                mechanism, _, initial = arg.partition(" ")
                if mechanism.upper() == "PLAIN":
                    if not initial:
                        send("334 ")
                        initial = read() or ""
                    _zid, user, password = base64.b64decode(initial).decode().split("\0")
                elif mechanism.upper() == "LOGIN":
                    send("334 VXNlcm5hbWU6")
                    user = base64.b64decode(read() or "").decode()
                    send("334 UGFzc3dvcmQ6")
                    password = base64.b64decode(read() or "").decode()
                else:
                    send("504 5.5.4 unknown mechanism")
                    continue
                if self.users.get(user) == password:
                    with self.lock:
                        self.logins += 1
                    send("235 2.7.0 Accepted")
                else:
                    send("535 5.7.8 Username and Password not accepted")
            elif verb == "MAIL":
                mail_from, rcpts = arg, []
                send("250 2.1.0 OK")
            elif verb == "RCPT":
                rcpts.append(arg)
                send("250 2.1.5 OK")
            elif verb == "DATA":
                send("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    raw = state["file"].readline()
                    if not raw or raw in (b".\r\n", b".\n"):
                        break
                    chunks.append(raw[1:] if raw.startswith(b"..") else raw)
                data = b"".join(chunks)
                parsed = email.message_from_bytes(data, policy=email.policy.default)
                with self.lock:
                    self.mails.append({"from": mail_from, "to": rcpts, "data": data,
                                       "message": parsed})
                send("250 2.0.0 OK queued")
            elif verb == "RSET":
                mail_from, rcpts = None, []
                send("250 2.0.0 OK")
            elif verb == "NOOP":
                send("250 2.0.0 OK")
            elif verb == "QUIT":
                send("221 2.0.0 Bye")
                return
            else:
                send("502 5.5.2 Command not recognized")


class ShortJumps(FastClock):
    """A fast clock that jumps a sender's short waits only — its pacing — and leaves a
    digest's long one to the test, so a stepped feed can still move the clock forward."""

    def wait(self, condition, timeout) -> None:
        if timeout is not None and 0 < timeout <= 5 and condition in self.jump_for:
            self.advance(timeout)
            return
        FakeClock.wait(self, condition, timeout)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    import AlgoTradeKit.trader._trader as trader_module

    monkeypatch.setattr(trader_module, "DEFAULT_STATE_PATH", str(tmp_path / "state.json"))


@pytest.fixture(scope="module")
def server():
    fake = FakeSMTP()
    yield fake
    fake.close()


@pytest.fixture
def smtp(server):
    server.reset()
    return server


@pytest.fixture
def clock():
    return FastClock(at(14, 0, 1))


@pytest.fixture
def make(smtp, clock, monkeypatch):
    """E-mail notifiers on the fake server, on the fast clock; closed after the test."""
    made: list[EmailNotifier] = []

    def build(recipients=None, *, on=None, jump=True, **kwargs) -> EmailNotifier:
        used = clock if on is None else on
        kwargs.setdefault("port", smtp.port)
        kwargs.setdefault("tls", "none")
        kwargs.setdefault("username", USER)
        kwargs.setdefault("password", PASSWORD)
        kwargs.setdefault("timeout", 5.0)
        monkeypatch.setattr(EmailNotifier, "_clock", used)
        bot = EmailNotifier("127.0.0.1", {"me": USER} if recipients is None else recipients,
                            **kwargs)
        bot._clock = used
        if jump and isinstance(used, FastClock):
            used.jump_for.add(bot._cond)
        made.append(bot)
        return bot

    yield build
    for bot in made:
        bot.close(timeout=0.5)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(_snapshot, "snapshot", fake.snapshot)
    monkeypatch.setattr(_snapshot, "check_engine", lambda: None)
    return fake


def lines(out: str) -> list[str]:
    marker = "[AlgoTradeKit] email: "
    return [line.split(marker, 1)[1] for line in out.splitlines() if marker in line]


def parts(message) -> dict[str, str]:
    """The text and HTML bodies of an e-mail, with SMTP's CRLF line ends read as "\\n"."""
    plain = message.get_body(("plain",))
    rich = message.get_body(("html",))
    return {"plain": plain.get_content().replace("\r\n", "\n") if plain else "",
            "html": rich.get_content().replace("\r\n", "\n") if rich else ""}


# ===========================================================================
# Making one
# ===========================================================================

class TestCreating:
    def test_the_check_logs_in(self, make, smtp, capsys):
        make()
        assert smtp.logins == 1 and smtp.mails == []
        assert lines(capsys.readouterr().out) == []

    def test_a_refused_login_raises(self, smtp):
        with pytest.raises(EmailError) as caught:
            EmailNotifier("127.0.0.1", USER, port=smtp.port, tls="none", username=USER,
                          password="wrong")
        assert str(caught.value) == (
            "the SMTP server refused the login (535 5.7.8 Username and Password not accepted) "
            "— check username and password; Gmail takes an app password, not the account's")

    def test_a_server_out_of_reach_is_one_line(self, make, capsys):
        make(port=_free_port())
        assert lines(capsys.readouterr().out) == [
            "127.0.0.1 unreachable (connection refused) — carrying on; e-mails wait in the queue"]

    def test_what_it_takes(self, smtp):
        def notifier(**kwargs):
            options = {"port": smtp.port, "tls": "none", "username": USER, "password": PASSWORD,
                       "check": False}
            options.update(kwargs)
            return EmailNotifier("127.0.0.1", USER, **options)

        with pytest.raises(ValueError, match="sender= is needed"):
            notifier(username="ada")
        with pytest.raises(ValueError, match='tls must be None, "ssl", "starttls" or "none"'):
            notifier(tls="tls")
        with pytest.raises(ValueError, match="port must be a port number"):
            notifier(port=0)
        with pytest.raises(ValueError, match="unknown field {titel}"):
            notifier(subject="{titel}")
        with pytest.raises(ValueError, match="digest_minutes must be 0 or more"):
            notifier(digest_minutes=-1)
        with pytest.raises(ValueError, match="must be an e-mail address"):
            EmailRecipient("ada")
        assert notifier()._tls == "none"
        assert EmailNotifier("smtp.example.com", USER, port=465, username=USER,
                             check=False)._tls == "ssl"
        assert EmailNotifier("smtp.example.com", USER, username=USER, check=False)._tls == (
            "starttls")

    def test_the_password_is_never_said(self, make, smtp, capsys):
        bot = make()
        smtp.when("MAIL", 550, f"5.7.1 not with {PASSWORD}")
        bot.send_text("x", wait=False)
        drained(bot)
        out = capsys.readouterr().out
        assert PASSWORD not in out and PASSWORD not in repr(bot)
        assert "***" in out


# ===========================================================================
# What an e-mail holds
# ===========================================================================

class TestEmails:
    def test_a_text_goes_at_once_as_html_and_text(self, make, smtp):
        bot = make()
        sent = bot.send_text("<b>ATK</b> · watching\nthe 64k level &amp; more")
        (mail,) = smtp.messages()
        assert sent == {"me": mail["Message-ID"]}
        assert mail["From"] == USER and mail["To"] == USER
        assert mail["Subject"] == "ATK · watching"
        body = parts(mail)
        assert body["plain"] == "ATK · watching\nthe 64k level & more\n"
        assert "<div><b>ATK</b> · watching<br>\nthe 64k level &amp; more</div>" in body["html"]

    def test_a_subject_of_your_own(self, make, smtp):
        bot = make(subject="[ATK] {kind} · {symbol} · {title}")
        bot.send_text("Good morning", symbol="BTCUSDT")
        assert smtp.messages()[0]["Subject"] == "[ATK] text · BTCUSDT · Good morning"

    def test_a_picture_is_inline(self, make, smtp):
        bot = make()
        bot.send_photo(PICTURE, "<b>BTCUSDT</b> · 15m")
        (mail,) = smtp.messages()
        html_part = mail.get_body(("html",))
        (image,) = [part for part in mail.walk() if part.get_content_type() == "image/png"]
        cid = image["Content-ID"].strip("<>")
        assert f'src="cid:{cid}"' in html_part.get_content()
        assert image.get_content() == PICTURE
        assert mail["Subject"] == "BTCUSDT · 15m"

    def test_e4_a_report_is_its_text_its_balance_and_its_file(self, make, smtp, engine):
        bot = make()
        bot.send_report(backtest())
        text, balance, page = smtp.messages()
        assert text["Subject"] == "📊 BTCUSDT · Simulate report"
        assert (balance["Subject"], page["Subject"]) == ("balance.png", "report.html")
        assert [part.get_content_type() for part in balance.walk()
                if part.get_content_maintype() == "image"] == ["image/png"]
        (attached,) = list(page.iter_attachments())
        assert attached.get_filename() == "report.html"


# ===========================================================================
# The digest (Q6.3 b), and threads without one (Q6.5 a)
# ===========================================================================

class TestDigest:
    def test_q3b_trade_messages_wait_15_minutes_and_go_as_one(self, make, smtp, capsys):
        clock = FakeClock(at(14, 0, 1))
        bot = make(on=clock)
        dest = bot._destinations["me"]
        for kind, head in ((KIND_SIGNAL, SIGNAL_TEXT), (KIND_OPEN, "<b>✅ OPENED #0</b>"),
                           (KIND_CLOSE, "<b>🏁 CLOSED #0</b>")):
            bot._queue_event(dest, kind, "BTCUSDT", head, queued_at=clock.time(), silent=False)
            clock.advance(60)
        bot.send_text("a text goes at once", wait=False)
        settle(bot)
        assert [mail["Subject"] for mail in smtp.messages()] == ["a text goes at once"]
        clock.advance(15 * 60 - 3 * 60 - 1)          # a second short of the oldest's 15 min
        settle(bot)
        assert len(smtp.messages()) == 1
        clock.advance(1)
        settle(bot)
        digest = smtp.messages()[1]
        assert digest["Subject"] == "🟢 LONG BTCUSDT · 1h · and 2 more"
        body = parts(digest)
        assert body["html"].count("<hr>") == 2
        assert body["plain"].split("\n\n— — —\n\n") == [
            "🟢 LONG BTCUSDT · 1h\nEntry 64,250 · SL 63,800 · TP 65,150\nRR 2.00\n"
            "🕑 2026-10-14 14:00 UTC · SIM", "✅ OPENED #0", "🏁 CLOSED #0\n"]
        assert "late" not in body["plain"]       # the digest's own wait is not lateness

    def test_close_sends_a_held_digest_at_once(self, make, smtp):
        clock = FakeClock(at(14, 0, 1))
        bot = make(on=clock)
        bot._queue_event(bot._destinations["me"], KIND_SIGNAL, "BTCUSDT", SIGNAL_TEXT,
                         queued_at=clock.time(), silent=False)
        settle(bot)
        assert smtp.messages() == []
        bot.close(timeout=5)
        assert [mail["Subject"] for mail in smtp.messages()] == ["🟢 LONG BTCUSDT · 1h"]

    def test_q3b_a_sessions_trade_messages_in_one_digest(self, make, smtp):
        clock = ShortJumps(at(14, 0, 1))
        bot = make(on=clock, digest_minutes=120)
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[])
        run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        subjects = [mail["Subject"] for mail in smtp.messages()]
        assert sorted(subjects) == sorted([
            "▶️ Paper trading started · BTCUSDT",
            "⏹ Paper trading stopped · BTCUSDT · feeds ended · no paper positions open",
            "🟢 LONG BTCUSDT · 1h · and 2 more",
        ])
        assert subjects[0] == "▶️ Paper trading started · BTCUSDT"

    def test_q5a_without_a_digest_a_trade_threads(self, make, smtp, clock):
        bot = make(digest_minutes=0)
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot])
        run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        by_subject = {mail["Subject"]: mail for mail in smtp.messages()}
        opened = by_subject["✅ OPENED #0 · LONG BTCUSDT @ 64,250"]
        closed = by_subject["Re: ✅ OPENED #0 · LONG BTCUSDT @ 64,250"]
        assert closed["In-Reply-To"] == opened["Message-ID"] == closed["References"]
        assert parts(closed)["plain"].startswith(
            "🏁 CLOSED #0 · LONG BTCUSDT @ 65,150 — take profit")


# ===========================================================================
# Answers from the server
# ===========================================================================

class TestAnswers:
    def test_a_try_later_is_tried_again(self, make, smtp):
        clock = FakeClock(at(14))
        bot = make(on=clock)
        smtp.when("MAIL", 421, "4.7.0 Try again later")
        bot.send_text("later", wait=False)
        settle(bot)
        assert smtp.messages() == []
        clock.advance(1.0)
        settle(bot)
        assert [mail["Subject"] for mail in smtp.messages()] == ["later"]

    def test_a_refused_address_is_that_recipients_alone(self, make, smtp, capsys):
        bot = make({"me": USER, "boss": "boss@example.com"})
        smtp.when("RCPT", 550, "5.1.1 No such user")
        bot.send_text("hello", wait=False)
        drained(bot)
        assert [mail["To"] for mail in smtp.messages()] == ["boss@example.com"]
        assert lines(capsys.readouterr().out) == [
            "could not send to me (ada@example.com) — 550 5.1.1 No such user "
            "(0 messages waiting)"]


# ===========================================================================
# TLS, with a throwaway certificate
# ===========================================================================

@pytest.fixture(scope="module")
def tls_contexts(tmp_path_factory):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(
            "127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path_factory.mktemp("tls")
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    server_side = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_side.load_cert_chain(cert_path, key_path)
    client_side = ssl.create_default_context(cafile=str(cert_path))
    return server_side, client_side


class TestTls:
    @pytest.mark.parametrize("implicit", [False, True], ids=["starttls", "ssl"])
    def test_tls_by_the_port(self, tls_contexts, monkeypatch, implicit):
        server_side, client_side = tls_contexts
        monkeypatch.setattr(EmailNotifier, "_ssl_context", client_side)
        fake = FakeSMTP(tls=server_side, implicit=implicit)
        try:
            bot = EmailNotifier("127.0.0.1", USER, port=fake.port,
                                tls="ssl" if implicit else "starttls", username=USER,
                                password=PASSWORD)
            try:
                bot.send_text("over TLS")
            finally:
                bot.close(timeout=1)
            assert [mail["Subject"] for mail in fake.messages()] == ["over TLS"]
            if not implicit:
                assert "STARTTLS" in fake.commands
        finally:
            fake.close()

    def test_a_server_without_starttls_is_named(self, smtp, monkeypatch):
        with pytest.raises(EmailError, match="does not offer STARTTLS"):
            EmailNotifier("127.0.0.1", USER, port=smtp.port, tls="starttls", username=USER,
                          password=PASSWORD)


# ===========================================================================
# Trading never waits
# ===========================================================================

class TestTradingNeverWaits:
    """With an SMTP server that takes 5 seconds to take each e-mail, a candle is processed
    within 50 ms of the time it takes with no notifier (Part 2's bound)."""

    BOUND = 0.050

    @pytest.mark.filterwarnings("ignore:.*silent paper run")
    def test_run_live_candle(self, make, smtp):
        bot = make(check=False, on=FakeClock(at(14)), digest_minutes=0)
        smtp.when("DATA", delay=5.0, times=100)
        cost = {}
        for label, bots in (("bare", []), ("email", [bot])):
            broker = _PaperBroker(SEED)
            session = run_live_module._build_session(
                TraderPair(broker=broker, config=paper_cfg(bots, symbol="BTCUSDT"),
                           strategy=Plan(LONG)),
                adapter_module.NotifySession("paper"))
            run_live_module._attach_sim(session, 10_000.0)
            session.sim.seed()
            session.bridge.prime()
            hour = iter(range(13, 40))

            def one_candle(session=session, hour=hour):
                h = next(hour)
                plan = {ms(at(h)): [("long", 64_250.0, 63_800.0, 65_150.0)]}
                session.pair.strategy.signals.update(plan)
                session.sim.process_closed_candle(candle(at(h), 64_250, 65_200, 64_240, 65_100))

            best = float("inf")
            for _ in range(3):
                started = time.perf_counter()
                one_candle()
                best = min(best, time.perf_counter() - started)
            cost[label] = best
        assert cost["email"] - cost["bare"] < self.BOUND, cost
        smtp.reset()
