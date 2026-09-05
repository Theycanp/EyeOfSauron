from __future__ import annotations

import os
import urllib.error
import urllib.request
import argparse
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from . import __version__


class HeartbeatError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class HeartbeatConfig:
    url_env: str
    token_env: str | None = None
    timeout_seconds: int = 15


class HeartbeatSender:
    """Outbound-only heartbeat sender. It never includes host data in a ping."""

    def __init__(self, config: HeartbeatConfig, environment: Mapping[str, str] | None = None) -> None:
        env = os.environ if environment is None else environment
        self.url = env.get(config.url_env, "")
        self.token = env.get(config.token_env, "") if config.token_env else ""
        self.timeout_seconds = config.timeout_seconds
        if not self.url:
            raise HeartbeatError(f"required environment variable {config.url_env} is missing")
        try:
            parsed = urlsplit(self.url)
            valid = (parsed.scheme == "https" and parsed.hostname and
                     not parsed.username and not parsed.password and not parsed.fragment)
            parsed.port
        except ValueError:
            valid = False
        if not valid or any(ord(char) < 33 for char in self.url):
            raise HeartbeatError("heartbeat URL must be HTTPS without credentials or fragment")
        if not 1 <= self.timeout_seconds <= 60:
            raise HeartbeatError("heartbeat timeout must be between 1 and 60 seconds")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.token):
            raise HeartbeatError("heartbeat token contains invalid characters")
        self._opener = urllib.request.build_opener(_NoRedirect())

    def ping(self, suffix: str = "") -> None:
        if suffix and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", suffix):
            raise HeartbeatError("heartbeat suffix must be a single path segment")
        parsed = urlsplit(self.url)
        path = parsed.path.rstrip("/") + ("/" + suffix if suffix else "")
        url = urlunsplit(parsed._replace(path=path))
        headers = {"User-Agent": f"Argus/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            url,
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                response.read(1024)
                if not 200 <= response.status < 300:
                    raise HeartbeatError(f"heartbeat returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise HeartbeatError(f"heartbeat returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HeartbeatError(f"heartbeat request failed: {type(exc).__name__}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argus-heartbeat")
    parser.add_argument("--url-env", required=True)
    parser.add_argument("--token-env")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args(argv)
    HeartbeatSender(HeartbeatConfig(args.url_env, args.token_env)).ping(args.suffix)
    return 0
