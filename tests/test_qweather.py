from __future__ import annotations

import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from argus.qweather import QWeatherError, QWeatherProvider
from argus.weather import WeatherSubscription


NOW = 1_790_000_000


class QWeatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.key = Ed25519PrivateKey.generate()
        path = Path(self.temp.name) / "private.pem"
        path.write_bytes(self.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        path.chmod(0o600)
        self.provider = QWeatherProvider("test.xy.qweatherapi.com", "Q123456789", "P123456789",
                                        "C123456789", str(path))
        self.subscription = WeatherSubscription("home", "Test", 40.16, 116.28,
                                                "Asia/Shanghai", "07:00", True, True, 1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_jwt_signature_expiry_and_identity(self) -> None:
        with patch("argus.qweather.time.time", return_value=NOW):
            token = self.provider._token()
        header, payload, signature = token.split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        self.assertEqual({"alg": "EdDSA", "kid": "C123456789"}, json.loads(decode(header)))
        self.assertEqual({"iss": "Q123456789", "sub": "P123456789", "iat": NOW,
                          "exp": NOW + 900}, json.loads(decode(payload)))
        self.key.public_key().verify(decode(signature), f"{header}.{payload}".encode())

    def test_bounded_gzip_json_request_hides_authentication_from_url(self) -> None:
        body = gzip.compress(json.dumps({"code": "200", "minutely": []}).encode())
        response = Mock(status=200, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.provider.opener = Mock()
        self.provider.opener.open.return_value = response
        self.assertEqual("200", self.provider._request("/v7/minutely/5m")["code"])
        request = self.provider.opener.open.call_args.args[0]
        self.assertEqual("https://test.xy.qweatherapi.com/v7/minutely/5m", request.full_url)
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))
        self.assertEqual(12, self.provider.opener.open.call_args.kwargs["timeout"])
        response.read.return_value = gzip.compress(b" " * 512_001)
        with self.assertRaisesRegex(QWeatherError, "too large"):
            self.provider._request("/v7/minutely/5m")

    def test_minutely_normalizes_and_rejects_stale_series(self) -> None:
        from datetime import UTC, datetime
        iso = lambda at: datetime.fromtimestamp(at, UTC).isoformat()
        payload = {"code": "200", "updateTime": iso(NOW), "minutely": [
            {"fxTime": iso(NOW + index * 300), "precip": "0.1", "type": "rain"}
            for index in range(24)
        ]}
        with patch.object(self.provider, "_request", return_value=payload), patch("argus.qweather.time.time", return_value=NOW):
            result = self.provider.fetch_minutely(self.subscription)
            self.assertEqual(24, len(result.slots))
            self.assertEqual(0.1, result.slots[0].precipitation)
            payload["updateTime"] = iso(NOW - 1801)
            with self.assertRaisesRegex(QWeatherError, "stale"):
                self.provider.fetch_minutely(self.subscription)

    def test_official_alerts_preserve_supersedes_and_issuer(self) -> None:
        payload = {"alerts": [{
            "id": "warning", "issuedTime": "2026-09-26T07:00Z", "expireTime": "2026-09-27T07:00Z",
            "messageType": {"code": "update", "supersedes": ["previous"]},
            "eventType": {"name": "wind"}, "color": {"code": "orange"},
            "severity": "severe", "headline": "Strong wind", "description": "Be careful",
            "senderName": "Local weather authority",
        }]}
        with patch.object(self.provider, "_request", return_value=payload):
            alerts = self.provider.fetch_alerts(self.subscription)
        self.assertEqual(("previous",), alerts[0].supersedes)
        self.assertEqual("Local weather authority", alerts[0].sender)
        with patch.object(self.provider, "_request", return_value={}):
            with self.assertRaises(QWeatherError):
                self.provider.fetch_alerts(self.subscription)

    def test_host_is_not_an_arbitrary_credential_destination(self) -> None:
        with self.assertRaisesRegex(QWeatherError, "host"):
            QWeatherProvider("evil.example", "Q123456789", "P123456789", "C123456789", "/secret.pem")
        with patch.dict("os.environ", {"QWEATHER_API_HOST": "test.qweatherapi.com"}, clear=True):
            with self.assertRaisesRegex(QWeatherError, "incomplete"):
                QWeatherProvider.from_environment()
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(QWeatherProvider.from_environment())


if __name__ == "__main__":
    unittest.main()
