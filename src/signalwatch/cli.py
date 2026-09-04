from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .adapters import AdapterError, build_collector
from .admin import ManagedConfigStore, serve
from .database import Database
from .notifier import NtfyNotifier, NotifyError
from .rss import RssCollector
from .rules import RuleSet
from .service import AlreadyRunningError, ProcessLock, SignalWatchService
from .util import now_epoch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signalwatch")
    parser.add_argument("--config", required=True, help="absolute path to TOML configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="validate configuration without reading secrets")
    commands.add_parser("run", help="run the long-lived service")
    once = commands.add_parser("once", help="poll every source once")
    once.add_argument("--deliver", action="store_true", help="also drain notifications currently due")
    status = commands.add_parser("status", help="show persisted collector and outbox status")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands.add_parser("enqueue-test", help="queue one clearly labeled ntfy test message")
    commands.add_parser("admin", help="run the loopback-only management API")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_service(config, database: Database) -> SignalWatchService:  # type: ignore[no-untyped-def]
    collectors = {}
    for source in config.sources:
        try:
            collector = build_collector(source)
        except AdapterError as exc:
            if source.kind in {"imap", "x", "youtube", "mqtt", "heartbeat"}:
                logging.getLogger("signalwatch").warning(
                    "source_disabled source=%s reason=credentials_or_settings_unavailable",
                    source.id,
                )
                continue
            raise
        if collector is not None:
            collectors[source.id] = collector
    rules = RuleSet.from_config(config.rules, config.ntfy.default_topic)
    notifier = NtfyNotifier.from_config(config.ntfy) if config.ntfy.enabled else None
    return SignalWatchService(config, database, collectors, rules, notifier)


def _print_status(status: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"database schema: {status['database_schema']}")
    print(f"observations: {status['observations']}")
    outbox = status["outbox"]
    print(
        "outbox: "
        f"pending={outbox.get('pending', 0)} "
        f"sending={outbox.get('sending', 0)} "
        f"delivered={outbox.get('delivered', 0)}"
    )
    for source in status["sources"]:
        print(
            f"source {source['source_id']}: initialized={bool(source['initialized'])} "
            f"failures={source['consecutive_failures']} "
            f"last_success={source['last_success_at'] or '-'}"
        )


async def _run_service(service: SignalWatchService) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.request_stop)
    await service.run_forever()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config = load_config(Path(arguments.config))
        _configure_logging(config.service.log_level)
        if arguments.command == "check-config":
            print(
                f"configuration valid: {len(config.sources)} sources, "
                f"{len(config.rules)} rules, schema {config.schema_version}"
            )
            return 0

        database = Database(config.service.database_path)
        try:
            if arguments.command == "status":
                _print_status(database.status(), arguments.json)
                return 0
            if arguments.command == "enqueue-test":
                queued = database.enqueue_test_alert(config.ntfy.default_topic, now_epoch())
                print("test notification queued" if queued else "test notification already queued")
                return 0

            if arguments.command == "admin":
                if not config.admin.enabled:
                    raise ConfigError("admin.enabled is false")
                managed_path = config.service.managed_sources_path or (config.service.database_path.parent / "managed-sources.json")
                base_config = load_config(Path(arguments.config), include_managed=False)
                store = ManagedConfigStore(
                    managed_path,
                    {source.id for source in base_config.sources},
                    {rule.id for rule in base_config.rules},
                )
                serve(config.admin, store, database)
                return 0

            service = _build_service(config, database)
            with ProcessLock(config.service.lock_path):
                if arguments.command == "run":
                    asyncio.run(_run_service(service))
                elif arguments.command == "once":
                    asyncio.run(service.run_once(deliver=arguments.deliver))
                else:
                    raise AssertionError(f"unhandled command: {arguments.command}")
            return 0
        finally:
            database.close()
    except (ConfigError, NotifyError, AdapterError, AlreadyRunningError, OSError, RuntimeError) as exc:
        print(f"signalwatch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
