from __future__ import annotations

import os
import urllib.error
import urllib.request
import argparse
from dataclasses import dataclass
from typing import Mapping


class HeartbeatError(RuntimeError):
    pass


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
        if not self.url.startswith("https://"):
            raise HeartbeatError("heartbeat URL must be HTTPS")

    def ping(self, suffix: str = "") -> None:
        url = self.url.rstrip("/") + ("/" + suffix.lstrip("/") if suffix else "")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {"User-Agent": "Argus/0.6"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
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
