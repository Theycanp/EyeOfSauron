from __future__ import annotations

import json
import unittest
import urllib.error
from dataclasses import replace

from argus.models import OutboxMessage
from argus.notifier import NtfyNotifier, NotifyError


class _Response:
    status = 200

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
        return None

    def read(self, amount: int) -> bytes:
        return b'{}'


class _Opener:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.request = None

    def open(self, request, timeout):  # type: ignore[no-untyped-def]
        self.request = request
        if self.error:
            raise self.error
        return _Response()


def _alert() -> OutboxMessage:
    return OutboxMessage(
        id=1,
        topic="eos",
        title="Test",
        message="Message",
        priority=5,
        tags=("warning",),
        click_url="https://www.bloomberg.com/news/articles/test",
        attempts=1,
    )


class NotifierTests(unittest.TestCase):
    def test_builds_ntfy_json_request(self) -> None:
        notifier = NtfyNotifier("https://ntfy.example", "private-token", 10)
        opener = _Opener()
        notifier._opener = opener
        notifier.publish(_alert())
        assert opener.request is not None
        payload = json.loads(opener.request.data)
        self.assertEqual("eos", payload["topic"])
        self.assertEqual(5, payload["priority"])
        self.assertEqual(["warning"], payload["tags"])
        self.assertEqual("Bearer private-token", opener.request.get_header("Authorization"))

    def test_network_error_does_not_expose_token(self) -> None:
        notifier = NtfyNotifier("https://ntfy.example", "do-not-leak", 10)
        notifier._opener = _Opener(urllib.error.URLError("token=do-not-leak"))
        with self.assertRaises(NotifyError) as raised:
            notifier.publish(_alert())
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_bounds_long_multibyte_message_and_preserves_detail_link(self) -> None:
        notifier = NtfyNotifier("https://ntfy.example", "private-token", 10)
        opener = _Opener()
        notifier._opener = opener
        alert = replace(_alert(), message="重要情报" * 1000)
        notifier.publish(alert)
        assert opener.request is not None
        payload = json.loads(opener.request.data)
        self.assertLessEqual(len(payload["message"].encode("utf-8")), 4096)
        self.assertTrue(payload["message"].endswith("[内容较长，完整内容请点击通知查看]"))
        self.assertEqual(alert.click_url, payload["click"])

    def test_rejects_plain_http_endpoint(self) -> None:
        with self.assertRaisesRegex(NotifyError, "must be HTTPS"):
            NtfyNotifier("http://ntfy.example", "token", 10)


if __name__ == "__main__":
    unittest.main()
