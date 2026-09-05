from __future__ import annotations

import email.message
import io
import json
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argus.adapters import AdapterError, ImapCollector, XCollector, build_collector
from argus.config import SourceConfig
from argus.database import Database
from argus.market import MarketCollector, MarketError
from argus.models import SourceState
from argus.persistence import RuntimeRepository
from argus.safe_regex import UnsafeRegexError, compile_safe_regex


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidvalidity: str = "7") -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.logout_called = False
        self.fetches: list[int] = []

    def login(self, username: str, password: str):
        return "OK", []

    def select(self, mailbox: str, readonly: bool = True):
        return "OK", [b"0"]

    def response(self, name: str):
        return "UIDVALIDITY", [self.uidvalidity.encode()]

    def uid(self, command: str, *args):
        if command == "SEARCH":
            return "OK", [" ".join(map(str, sorted(self.messages))).encode()]
        uid = int(args[0])
        self.fetches.append(uid)
        return "OK", [(b"message", self.messages[uid])]

    def logout(self):
        self.logout_called = True
        return "BYE", []


def mail(subject: str, identifier: str, body: str = "body") -> bytes:
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["Message-ID"] = identifier
    message["Date"] = "Sat, 05 Sep 2026 08:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


def source(kind: str, settings: dict, *, symbols: tuple[str, ...] = ()) -> SourceConfig:
    return SourceConfig(
        id=f"test_{kind}",
        kind=kind,
        publisher="Test",
        section="Test",
        dedupe_scope=f"test_{kind}",
        poll_interval_seconds=60,
        request_timeout_seconds=7,
        request_attempts=1,
        retry_base_seconds=1,
        max_response_bytes=4096,
        allowed_hosts=("api.x.com", "data.alpaca.markets"),
        settings=settings,
    )


def state(cursor: str | None = None) -> SourceState:
    return SourceState("test", True, None, None, None, None, 0, False, None, cursor)


class ImapReliabilityTests(unittest.TestCase):
    def test_batches_from_oldest_and_advances_only_processed_uid(self) -> None:
        fake = FakeImap({1: mail("one", "<1>"), 2: mail("two", "<2>"), 3: mail("three", "<3>")})
        observed_timeout: list[int] = []

        def factory(host: str, port: int, timeout: int):
            observed_timeout.append(timeout)
            return fake

        collector = ImapCollector(
            source("imap", {
                "host": "mail.example",
                "username_env": "MAIL_USER",
                "password_env": "MAIL_PASSWORD",
                "batch_size": 2,
            }),
            {"MAIL_USER": "u", "MAIL_PASSWORD": "p"},
            client_factory=factory,
        )
        first = collector.fetch(state())
        self.assertEqual([1, 2], fake.fetches)
        self.assertEqual(2, json.loads(first.cursor)["last_uid"])
        second = collector.fetch(state(first.cursor))
        self.assertEqual([1, 2, 3], fake.fetches)
        self.assertEqual(["<3>"], [item.external_id for item in second.observations])
        self.assertEqual([7, 7], observed_timeout)
        self.assertTrue(fake.logout_called)

    def test_uidvalidity_change_resets_uid_high_watermark(self) -> None:
        fake = FakeImap({1: mail("new mailbox", "<new>")}, uidvalidity="8")
        collector = ImapCollector(
            source("imap", {"host": "mail.example", "username_env": "U", "password_env": "P"}),
            {"U": "u", "P": "p"},
            client_factory=lambda *args, **kwargs: fake,
        )
        result = collector.fetch(state(json.dumps({"uidvalidity": "7", "last_uid": 500})))
        self.assertEqual([1], fake.fetches)
        self.assertEqual("8", json.loads(result.cursor)["uidvalidity"])

    def test_oversized_message_fails_without_advancing_cursor(self) -> None:
        fake = FakeImap({1: b"x" * 4097})
        collector = ImapCollector(
            source("imap", {"host": "mail.example", "username_env": "U", "password_env": "P"}),
            {"U": "u", "P": "p"},
            client_factory=lambda *args, **kwargs: fake,
        )
        with self.assertRaisesRegex(AdapterError, "size limit"):
            collector.fetch(state())
        self.assertTrue(fake.logout_called)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def open(self, request, timeout: int):
        self.urls.append(request.full_url)
        return FakeResponse(json.dumps(self.pages.pop(0)).encode())


class XReliabilityTests(unittest.TestCase):
    def test_pagination_cursor_does_not_advance_until_last_page(self) -> None:
        created = "2026-09-05T08:00:00Z"
        first_opener = FakeOpener([{
            "data": [{"id": "105", "text": "new", "created_at": created}, {"id": "104", "text": "newer", "created_at": created}],
            "meta": {"next_token": "page-two"},
        }])
        config = source("x", {
            "user_id": "42",
            "bearer_token_env": "X_TOKEN",
            "api_base_url": "https://api.x.com/2",
            "max_pages_per_poll": 1,
        })
        first = XCollector(config, {"X_TOKEN": "secret"}, opener=first_opener).fetch(state("100"))
        first_cursor = json.loads(first.cursor)
        self.assertEqual("100", first_cursor["since_id"])
        self.assertEqual("page-two", first_cursor["pagination_token"])
        self.assertEqual("105", first_cursor["max_seen_id"])

        second_opener = FakeOpener([{
            "data": [{"id": "103", "text": "older", "created_at": created}],
            "meta": {},
        }])
        second = XCollector(config, {"X_TOKEN": "secret"}, opener=second_opener).fetch(state(first.cursor))
        self.assertEqual("105", json.loads(second.cursor)["since_id"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(second_opener.urls[0]).query)
        self.assertEqual(["100"], query["since_id"])
        self.assertEqual(["page-two"], query["pagination_token"])

    def test_credentials_cannot_be_sent_to_unapproved_host(self) -> None:
        config = source("x", {
            "user_id": "42",
            "bearer_token_env": "X_TOKEN",
            "api_base_url": "https://attacker.example/2",
        })
        with self.assertRaisesRegex(AdapterError, "not allowed"):
            XCollector(config, {"X_TOKEN": "secret"})


class MarketReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
        self.config = source("market", {
            "symbols": ["ABC"],
            "price_change_threshold": 5,
            "volume_multiplier": 3,
            "gap_threshold": 3,
            "cooldown_seconds": 60,
            "max_quote_age_seconds": 300,
        })

    def quote(self, price: float = 110, volume: float = 100) -> dict:
        return {
            "latestTrade": {"p": price, "t": self.now.isoformat()},
            "dailyBar": {"v": volume, "o": 100},
            "prevDailyBar": {"c": 100, "v": 100},
        }

    def test_condition_during_cooldown_is_delayed_not_lost(self) -> None:
        clock = [self.now]
        provider = type("Provider", (), {"latest": lambda _self, symbol: self.quote()})()
        collector = MarketCollector(self.config, provider, now_factory=lambda: clock[0])
        cursor = json.dumps({"ABC": {"price": 100, "volume": 100, "active": [], "last_event": int(self.now.timestamp()) - 10}})
        first = collector.fetch(state(cursor))
        self.assertEqual((), first.observations)
        self.assertFalse(json.loads(first.cursor)["ABC"]["conditions"]["price_move"]["notified"])
        clock[0] += timedelta(seconds=61)
        provider.latest = lambda symbol: {
            **self.quote(),
            "latestTrade": {"p": 110, "t": clock[0].isoformat()},
        }
        second = collector.fetch(state(first.cursor))
        self.assertEqual(["price_move"], second.observations[0].attributes["event_types"])

    def test_partial_condition_recovery_does_not_close_other_condition(self) -> None:
        provider = type("Provider", (), {"latest": lambda _self, symbol: self.quote()})()
        collector = MarketCollector(self.config, provider, now_factory=lambda: self.now)
        cursor = json.dumps({"ABC": {
            "price": 110,
            "volume": 400,
            "conditions": {
                "price_move": {"active": True, "notified": True, "last_notified_at": 1},
                "volume_spike": {"active": True, "notified": True, "last_notified_at": 1},
                "gap": {"active": False, "notified": False, "last_notified_at": 0},
            },
        }})
        result = collector.fetch(state(cursor))
        self.assertEqual(1, len(result.observations))
        recovery = result.observations[0]
        self.assertEqual("symbol:ABC:volume_spike", recovery.attributes["incident_key"])
        self.assertTrue(recovery.attributes["recovery"])
        self.assertTrue(json.loads(result.cursor)["ABC"]["conditions"]["price_move"]["active"])

    def test_one_bad_symbol_does_not_starve_healthy_symbols(self) -> None:
        config = self.config
        config = SourceConfig(
            id=config.id, kind=config.kind, publisher=config.publisher, section=config.section,
            dedupe_scope=config.dedupe_scope, poll_interval_seconds=config.poll_interval_seconds,
            request_timeout_seconds=config.request_timeout_seconds, request_attempts=config.request_attempts,
            retry_base_seconds=config.retry_base_seconds, max_response_bytes=config.max_response_bytes,
            allowed_hosts=config.allowed_hosts, settings={**config.settings, "symbols": ["BAD", "ABC"]},
        )
        class Provider:
            def latest(inner, symbol):
                if symbol == "BAD":
                    raise MarketError("provider failure")
                return self.quote()
        cursor = json.dumps({"ABC": {"price": 100, "volume": 100, "active": []}})
        result = MarketCollector(config, Provider(), now_factory=lambda: self.now).fetch(state(cursor))
        self.assertEqual(("BAD:MarketError",), result.warnings)
        self.assertTrue(result.observations)

    def test_stale_and_non_finite_quotes_are_rejected(self) -> None:
        stale = self.quote()
        stale["latestTrade"]["t"] = (self.now - timedelta(hours=1)).isoformat()
        provider = type("Provider", (), {"latest": lambda _self, symbol: stale})()
        with self.assertRaisesRegex(MarketError, "all configured"):
            MarketCollector(self.config, provider, now_factory=lambda: self.now).fetch(state())
        infinite = self.quote(price=float("inf"))
        provider = type("Provider", (), {"latest": lambda _self, symbol: infinite})()
        with self.assertRaisesRegex(MarketError, "all configured"):
            MarketCollector(self.config, provider, now_factory=lambda: self.now).fetch(state())


class BoundaryTests(unittest.TestCase):
    def test_database_implements_runtime_repository_and_uow_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            self.assertIsInstance(database, RuntimeRepository)
            with self.assertRaises(RuntimeError):
                with database.unit_of_work():
                    database.connection.execute("INSERT INTO collector_state(source_id) VALUES ('rollback')")
                    raise RuntimeError("fault injection")
            count = database.connection.execute(
                "SELECT COUNT(*) FROM collector_state WHERE source_id='rollback'"
            ).fetchone()[0]
            self.assertEqual(0, count)
            database.close()

    def test_unsafe_regex_is_rejected(self) -> None:
        with self.assertRaises(UnsafeRegexError):
            compile_safe_regex(r"(a+)+$")
        self.assertIsNotNone(compile_safe_regex(r"(?:breaking|urgent)\b"))

    def test_enabled_experimental_source_fails_closed(self) -> None:
        config = source("mqtt", {})
        with self.assertRaises(AdapterError):
            build_collector(config)


if __name__ == "__main__":
    unittest.main()
