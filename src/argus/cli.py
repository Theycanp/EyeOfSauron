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
from .analysis_orchestrator import AnalysisOrchestrator
from .database import Database, read_active_config
from .content import PublicDocumentFetcher
from .digest import DigestScheduler
from .model_analyzers import AnalyzerSettings, LocalModelAnalyzer, OpenAICompatibleAnalyzer
from .notifier import DEFAULT_NOTIFIER_REGISTRY, NotifyError
from .prompts import PromptTemplate
from .rules import RuleSet
from .service import AlreadyRunningError, ProcessLock, ArgusService
from .util import now_epoch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus")
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


def _build_service(
    config, database: Database, config_revision: int | None = None
) -> ArgusService:  # type: ignore[no-untyped-def]
    collectors = {}
    for source in config.sources:
        try:
            collector = build_collector(source)
        except (AdapterError, ValueError, OSError) as exc:
            logging.getLogger("argus").error(
                "source_initialization_failed source=%s error_type=%s",
                source.id, type(exc).__name__,
            )
            continue
        if collector is not None:
            collectors[source.id] = collector
    rules = RuleSet.from_config(config.rules, config.ntfy.default_topic)
    notifier = (
        DEFAULT_NOTIFIER_REGISTRY.build(
            "ntfy",
            config=config.ntfy,
            detail_base_url=config.admin.public_base_url or config.digest.public_base_url,
        )
        if config.ntfy.enabled else None
    )
    analysis_orchestrator = None
    if config.analysis.enabled:
        stored_prompt = database.get_prompt(
            config.analysis.prompt_id, config.analysis.prompt_version
        )
        if stored_prompt is None:
            raise ConfigError(
                "configured analysis prompt version does not exist: "
                f"{config.analysis.prompt_id}@{config.analysis.prompt_version}"
            )
        prompt = PromptTemplate(
            str(stored_prompt["prompt_id"]),
            int(stored_prompt["version"]),
            str(stored_prompt["system_text"]),
        )
        common = {
            "timeout_seconds": config.analysis.timeout_seconds,
            "max_input_chars": config.analysis.max_input_chars,
            "max_response_bytes": config.analysis.max_response_bytes,
            "max_tokens": config.analysis.max_tokens,
            "prompt_id": config.analysis.prompt_id,
            "prompt_version": config.analysis.prompt_version,
        }
        local_analyzer = (
            LocalModelAnalyzer(
                AnalyzerSettings(
                    config.analysis.local_base_url,
                    config.analysis.local_model,
                    **common,
                ),
                prompt=prompt,
            )
            if config.analysis.local_enabled else None
        )
        api_analyzer = (
            OpenAICompatibleAnalyzer(
                AnalyzerSettings(
                    config.analysis.api_base_url,
                    config.analysis.api_model,
                    **common,
                ),
                api_key_env=config.analysis.api_key_env,
                prompt=prompt,
            )
            if config.analysis.api_enabled else None
        )
        analysis_orchestrator = AnalysisOrchestrator(
            config.analysis,
            database,
            local_analyzer=local_analyzer,
            api_analyzer=api_analyzer,
        )
    digest_scheduler = (
        DigestScheduler(
            config.digest,
            database,
            topic=config.ntfy.default_topic,
            source_ids=[source.id for source in config.sources if source.enabled],
            region_weights=config.analysis.region_weights,
        )
        if config.digest.enabled else None
    )
    return ArgusService(
        config,
        database,
        collectors,
        rules,
        notifier,
        config_revision=config_revision,
        analysis_orchestrator=analysis_orchestrator,
        digest_scheduler=digest_scheduler,
        content_fetcher=PublicDocumentFetcher(),
    )


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
        f"delivered={outbox.get('delivered', 0)} "
        f"dead={outbox.get('dead', 0)} "
        f"cancelled={outbox.get('cancelled', 0)}"
    )
    reminders = status.get("reminders", {})
    print(
        "reminders: "
        f"enabled={reminders.get('enabled', 0)} "
        f"disabled={reminders.get('disabled', 0)}"
    )
    for source in status["sources"]:
        print(
            f"source {source['source_id']}: initialized={bool(source['initialized'])} "
            f"failures={source['consecutive_failures']} "
            f"last_success={source['last_success_at'] or '-'}"
        )


async def _run_service(service: ArgusService) -> bool:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.request_stop)
    return await service.run_forever()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config_path = Path(arguments.config)
        base_config = load_config(config_path, include_managed=False)
        _configure_logging(base_config.service.log_level)
        if arguments.command == "check-config":
            active_payload = read_active_config(base_config.service.database_path)
            config = load_config(config_path, managed_override=active_payload)
            print(
                f"configuration valid: {len(config.sources)} sources, "
                f"{len(config.rules)} rules, schema {config.schema_version}"
            )
            return 0

        database = Database(base_config.service.database_path)
        try:
            managed_path = base_config.service.managed_sources_path or (
                base_config.service.database_path.parent / "managed-sources.json"
            )
            store = ManagedConfigStore(
                managed_path,
                {source.id for source in base_config.sources},
                {rule.id for rule in base_config.rules},
                database,
            )
            active_revision = database.get_active_config_revision()
            config = (
                load_config(
                    config_path,
                    managed_override=active_revision["payload"],
                )
                if active_revision is not None
                else base_config
            )
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
                serve(
                    config.admin,
                    store,
                    database,
                    heartbeat_timeout_seconds=max(
                        60, config.service.heartbeat_interval_seconds * 4
                    ),
                    notification_topic=config.ntfy.default_topic,
                )
                return 0

            service = _build_service(
                config,
                database,
                int(active_revision["revision"]) if active_revision is not None else None,
            )
            with ProcessLock(config.service.lock_path):
                if arguments.command == "run":
                    reload_requested = asyncio.run(_run_service(service))
                    if reload_requested:
                        return 75
                elif arguments.command == "once":
                    asyncio.run(service.run_once(deliver=arguments.deliver))
                else:
                    raise AssertionError(f"unhandled command: {arguments.command}")
            return 0
        finally:
            database.close()
    except (ConfigError, NotifyError, AdapterError, AlreadyRunningError, OSError, RuntimeError) as exc:
        print(f"argus: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
