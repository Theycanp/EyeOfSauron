from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from argus.auth import AdminAuth, AuthError, LoginBlockedError
from argus.database import Database


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "state.db")
        self.auth = AdminAuth(self.database)
        self.admin = self.database.create_admin_user(
            "owner", "Owner", self.auth.hash_password("correct-horse-battery"),
            "admin", "bootstrap", 100,
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_cookie_session_and_csrf_are_bound(self) -> None:
        context, session, csrf = self.auth.login(
            "owner", "correct-horse-battery", "203.0.113.7", now=200
        )
        headers = {"Cookie": f"__Host-eos_session={session}; __Host-eos_csrf={csrf}"}
        restored = self.auth.authenticate(headers, now=201)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(context.user_id, restored.user_id)
        self.assertFalse(self.auth.validate_csrf(restored, headers))
        headers["X-CSRF-Token"] = csrf
        self.assertTrue(self.auth.validate_csrf(restored, headers))

    def test_five_failed_logins_trigger_a_lock(self) -> None:
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.auth.login("owner", "wrong-password-value", "203.0.113.8", now=300)
        with self.assertRaises(LoginBlockedError):
            self.auth.login("owner", "correct-horse-battery", "203.0.113.8", now=301)

    def test_role_change_revokes_existing_session(self) -> None:
        _, session, _ = self.auth.login(
            "owner", "correct-horse-battery", "203.0.113.9", now=400
        )
        second = self.database.create_admin_user(
            "second", "Second", self.auth.hash_password("another-long-password"),
            "admin", "owner", 401,
        )
        self.database.update_admin_user(
            int(self.admin["id"]), "Owner", "viewer", True, "second", 402
        )
        self.assertIsNone(
            self.auth.authenticate({"Cookie": f"__Host-eos_session={session}"}, now=403)
        )
        with self.assertRaises(ValueError):
            self.database.update_admin_user(
                int(second["id"]), "Second", "viewer", True, "second", 404
            )

    def test_password_hash_is_argon2id_and_never_returned(self) -> None:
        row = self.database.get_admin_user_for_auth("owner")
        assert row is not None
        self.assertTrue(str(row["password_hash"]).startswith("$argon2id$"))
        self.assertNotIn("password_hash", self.database.list_admin_users()[0])


if __name__ == "__main__":
    unittest.main()
