"""Password, session, CSRF, and role policy for the public admin plane."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Mapping, Protocol

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError


SESSION_COOKIE = "__Host-eos_session"
CSRF_COOKIE = "__Host-eos_csrf"
SESSION_SECONDS = 14 * 86400
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$")

ROLE_PERMISSIONS = {
    "viewer": frozenset({"read"}),
    "operator": frozenset(
        {"read", "sources:write", "reminders:write", "quality:write", "operations:write"}
    ),
    "admin": frozenset(
        {
            "read", "sources:write", "reminders:write", "quality:write",
            "operations:write", "settings:write", "users:manage",
        }
    ),
}


class AuthRepository(Protocol):
    def get_admin_user_for_auth(self, username: str) -> dict[str, Any] | None: ...
    def record_admin_login_attempt(self, subject_hash: str, success: bool, now: int) -> int: ...
    def admin_login_blocked_until(self, subject_hash: str, now: int) -> int: ...
    def create_admin_session(
        self, session_hash: str, csrf_hash: str, user_id: int, now: int, expires_at: int
    ) -> None: ...
    def resolve_admin_session(self, session_hash: str, now: int) -> dict[str, Any] | None: ...
    def revoke_admin_session(self, session_hash: str, now: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class AuthContext:
    user_id: int | None
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    session_hash: str | None = None
    csrf_hash: str | None = None
    emergency: bool = False

    def allows(self, permission: str) -> bool:
        return permission in self.permissions

    def public(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "emergency": self.emergency,
        }


class AuthError(ValueError):
    pass


class LoginBlockedError(AuthError):
    pass


class AdminAuth:
    def __init__(self, repository: AuthRepository, emergency_token: str | None = None) -> None:
        self.repository = repository
        self.emergency_token = emergency_token or ""
        self.passwords = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=1,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self.passwords.hash("not-a-real-user-password")

    @staticmethod
    def validate_username(username: str) -> str:
        value = username.strip()
        if not USERNAME_RE.fullmatch(value):
            raise AuthError("username must be 3-32 letters, digits, dot, dash, or underscore")
        return value

    @staticmethod
    def validate_password(password: str) -> str:
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise AuthError("password must contain 12-128 characters")
        if password.isspace() or password.strip() != password:
            raise AuthError("password cannot start or end with whitespace")
        return password

    def hash_password(self, password: str) -> str:
        return self.passwords.hash(self.validate_password(password))

    def verify_password(self, encoded: str, password: str) -> bool:
        try:
            return self.passwords.verify(encoded, password)
        except (InvalidHash, VerificationError, VerifyMismatchError):
            return False

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _cookies(raw: str | None) -> Mapping[str, str]:
        cookie = SimpleCookie()
        try:
            cookie.load(raw or "")
        except Exception:
            return {}
        return {key: morsel.value for key, morsel in cookie.items()}

    def authenticate(
        self, headers: Mapping[str, str], *, now: int, allow_emergency: bool = True
    ) -> AuthContext | None:
        supplied = headers.get("Authorization", "")
        if allow_emergency and self.emergency_token and secrets.compare_digest(
            supplied, f"Bearer {self.emergency_token}"
        ):
            return AuthContext(
                None, "emergency-admin", "Emergency administrator", "admin",
                ROLE_PERMISSIONS["admin"], emergency=True,
            )
        token = self._cookies(headers.get("Cookie")).get(SESSION_COOKIE, "")
        if not token or len(token) > 256:
            return None
        session_hash = self._digest(token)
        row = self.repository.resolve_admin_session(session_hash, now)
        if row is None or str(row.get("role")) not in ROLE_PERMISSIONS:
            return None
        return AuthContext(
            int(row["user_id"]), str(row["username"]), str(row["display_name"]),
            str(row["role"]), ROLE_PERMISSIONS[str(row["role"])], session_hash,
            str(row["csrf_hash"]),
        )

    def login(
        self, username: str, password: str, remote: str, *, now: int
    ) -> tuple[AuthContext, str, str]:
        normalized = username.strip()
        subjects = (
            self._digest(f"account\0{normalized.casefold()}\0{remote}"),
            self._digest(f"address\0{remote}"),
        )
        if any(self.repository.admin_login_blocked_until(subject, now) > now for subject in subjects):
            raise LoginBlockedError("too many login attempts; try again later")
        row = self.repository.get_admin_user_for_auth(normalized)
        candidate_hash = str(row["password_hash"]) if row is not None else self._dummy_hash
        password_valid = self.verify_password(candidate_hash, password)
        valid = bool(row and row.get("enabled") and password_valid)
        if not valid:
            for subject in subjects:
                self.repository.record_admin_login_attempt(subject, False, now)
            raise AuthError("invalid username or password")
        for subject in subjects:
            self.repository.record_admin_login_attempt(subject, True, now)
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        session_hash = self._digest(session_token)
        csrf_hash = self._digest(csrf_token)
        self.repository.create_admin_session(
            session_hash, csrf_hash, int(row["id"]), now, now + SESSION_SECONDS
        )
        context = AuthContext(
            int(row["id"]), str(row["username"]), str(row["display_name"]),
            str(row["role"]), ROLE_PERMISSIONS[str(row["role"])], session_hash, csrf_hash,
        )
        return context, session_token, csrf_token

    def validate_csrf(self, context: AuthContext, headers: Mapping[str, str]) -> bool:
        if context.emergency:
            return True
        cookies = self._cookies(headers.get("Cookie"))
        cookie_value = cookies.get(CSRF_COOKIE, "")
        header_value = headers.get("X-CSRF-Token", "")
        if not cookie_value or not header_value or not secrets.compare_digest(cookie_value, header_value):
            return False
        return bool(context.csrf_hash and secrets.compare_digest(self._digest(header_value), context.csrf_hash))

    @staticmethod
    def cookie_headers(session_token: str, csrf_token: str) -> tuple[str, str]:
        common = f"Path=/; Max-Age={SESSION_SECONDS}; Secure; SameSite=Strict"
        return (
            f"{SESSION_COOKIE}={session_token}; {common}; HttpOnly",
            f"{CSRF_COOKIE}={csrf_token}; {common}",
        )

    @staticmethod
    def clear_cookie_headers() -> tuple[str, str]:
        common = "Path=/; Max-Age=0; Secure; SameSite=Strict"
        return (
            f"{SESSION_COOKIE}=; {common}; HttpOnly",
            f"{CSRF_COOKIE}=; {common}",
        )
