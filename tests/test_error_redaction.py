import unittest

from argus.util import sanitize_error


class ErrorRedactionTests(unittest.TestCase):
    def test_auth_schemes_url_credentials_and_query_are_redacted(self):
        messages = [
            "Authorization: Bearer credential-value",
            "authorization=Basic credential-value",
            "https://user:credential-value@example.com/feed",
            "https://example.com/feed?access=credential-value",
            "password=credential-value",
        ]
        for message in messages:
            with self.subTest(message=message):
                result = sanitize_error(RuntimeError(message))
                self.assertNotIn("credential-value", result)
                self.assertIn("<redacted>", result)
