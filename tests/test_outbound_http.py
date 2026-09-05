from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import Mock, patch

from argus.heartbeat import HeartbeatConfig, HeartbeatError, HeartbeatSender, _NoRedirect, main
from argus.notifier import NtfyNotifier, NotifyError, delivery_error_details


class OutboundHttpTests(unittest.TestCase):
    def heartbeat(self, url="https://monitor.example/ping?id=private", **kwargs):
        return HeartbeatSender(HeartbeatConfig("URL", "TOKEN", **kwargs),
                               {"URL": url, "TOKEN": "test-token"})

    def test_heartbeat_rejects_unsafe_urls_and_timeouts(self):
        for url in ("", "http://example.org", "https:///path", "https://a:b@example.org",
                    "https://example.org/#fragment", "https://example.org:bad", "https://example.org/\n"):
            with self.subTest(url=url), self.assertRaises(HeartbeatError):
                self.heartbeat(url)
        with self.assertRaises(HeartbeatError):
            self.heartbeat(timeout_seconds=0)
        with self.assertRaises(HeartbeatError):
            HeartbeatSender(HeartbeatConfig("URL", "TOKEN"),
                            {"URL": "https://example.org", "TOKEN": "x\r\nInjected: yes"})

    def test_heartbeat_preserves_query_and_rejects_redirects(self):
        sender = self.heartbeat()
        response = Mock(status=204)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(sender._opener, "open", return_value=response) as opened:
            sender.ping("start")
        request = opened.call_args.args[0]
        self.assertEqual("https://monitor.example/ping/start?id=private", request.full_url)
        self.assertEqual("Bearer test-token", request.get_header("Authorization"))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))
        for suffix in ("../fail", "//other.example", "fail?x=1", "a/b"):
            with self.assertRaises(HeartbeatError):
                sender.ping(suffix)

    def test_heartbeat_errors_do_not_echo_secrets(self):
        sender = self.heartbeat()
        for error in (urllib.error.HTTPError(sender.url, 302, "private", {}, None),
                      urllib.error.URLError("private"), TimeoutError("private")):
            with patch.object(sender._opener, "open", side_effect=error), self.assertRaises(HeartbeatError) as raised:
                sender.ping()
            self.assertNotIn("private", str(raised.exception))
        with patch.dict("os.environ", {"TEST_PING": "https://example.org"}), patch.object(HeartbeatSender, "ping") as ping:
            self.assertEqual(0, main(["--url-env", "TEST_PING", "--suffix", "fail"]))
            ping.assert_called_once_with("fail")

    def test_notifier_http_retry_classification(self):
        notifier = NtfyNotifier("https://ntfy.example", "test-token", 5)
        alert = Mock(topic="eos", title="test", message="test", priority=3, tags=(), click_url=None)
        for code in (301, 400, 401, 403, 404, 408, 425, 429, 500, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(notifier.base_url, code, "private", {}, None)
                with patch.object(notifier._opener, "open", side_effect=error), self.assertRaises(NotifyError) as raised:
                    notifier.publish(alert)
                self.assertEqual(code in (408, 425, 429, 500, 503), raised.exception.retryable)
                self.assertNotIn("private", str(raised.exception))
        with patch.object(notifier._opener, "open", side_effect=TimeoutError("private")), self.assertRaises(NotifyError) as raised:
            notifier.publish(alert)
        self.assertEqual((True, "transport"), delivery_error_details(raised.exception))
        self.assertEqual((True, "transient_unknown"), delivery_error_details(ValueError()))

    def test_notifier_rejects_invalid_configuration_and_topics(self):
        for url in ("http://ntfy.example", "https://user:pass@ntfy.example", "https://ntfy.example?x=1", "https://ntfy.example#frag"):
            with self.assertRaises(NotifyError):
                NtfyNotifier(url, "token", 5)
        with self.assertRaises(NotifyError):
            NtfyNotifier("https://ntfy.example", "", 5)
        notifier = NtfyNotifier("https://ntfy.example", "token", 5)
        with self.assertRaises(NotifyError):
            notifier.publish(Mock(topic="../admin"))
