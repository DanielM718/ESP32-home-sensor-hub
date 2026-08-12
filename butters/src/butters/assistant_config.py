"""Non-secret configuration for routing, diagnostics, integrations, and TTS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomllib

from butters.config import ConfigError, subsystem_root


def default_assistant_config_path() -> Path:
    return subsystem_root() / "config" / "assistant.toml"


@dataclass(frozen=True, slots=True)
class IntegrationSettings:
    dashboard_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = 4.0
    cache_seconds: float = 5.0
    max_response_bytes: int = 2 * 1024 * 1024

    def validated(self) -> IntegrationSettings:
        if not self.dashboard_url.startswith(("http://", "https://")):
            raise ConfigError("integration.dashboard_url must be HTTP or HTTPS")
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ConfigError("integration.timeout_seconds must be 0.1 to 30")
        if not 0.0 <= self.cache_seconds <= 300.0:
            raise ConfigError("integration.cache_seconds must be 0 to 300")
        if not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise ConfigError("integration.max_response_bytes must be 1024 to 16777216")
        return self


@dataclass(frozen=True, slots=True)
class TTSSettings:
    model_dir: Path
    num_threads: int = 2
    speed: float = 1.0
    max_text_chars: int = 500

    def validated(self) -> TTSSettings:
        if not 1 <= self.num_threads <= 4:
            raise ConfigError("tts.num_threads must be between 1 and 4")
        if not 0.5 <= self.speed <= 2.0:
            raise ConfigError("tts.speed must be between 0.5 and 2.0")
        if not 1 <= self.max_text_chars <= 2000:
            raise ConfigError("tts.max_text_chars must be between 1 and 2000")
        return self


@dataclass(frozen=True, slots=True)
class LLMSettings:
    enabled: bool = False
    server_url: str = "http://127.0.0.1:18080"
    model: str = "butters-router"
    profile: str = "lfm2"
    output_mode: str = "native_tools"
    timeout_seconds: float = 12.0

    def validated(self) -> LLMSettings:
        parsed = urlparse(self.server_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ConfigError("llm.server_url must be an HTTP loopback URL")
        if self.profile not in {"generic", "lfm2", "qwen3"}:
            raise ConfigError("llm.profile must be generic, lfm2, or qwen3")
        if self.output_mode not in {"native_tools", "json_schema"}:
            raise ConfigError("llm.output_mode must be native_tools or json_schema")
        if not 0.1 <= self.timeout_seconds <= 120:
            raise ConfigError("llm.timeout_seconds must be between 0.1 and 120")
        if not self.model.strip():
            raise ConfigError("llm.model cannot be empty")
        return self


@dataclass(frozen=True, slots=True)
class DiagnosticSettings:
    enabled: bool = True
    session_ttl_seconds: float = 900.0
    max_evidence_bytes: int = 64 * 1024
    max_log_bytes: int = 8 * 1024

    def validated(self) -> DiagnosticSettings:
        if not 60 <= self.session_ttl_seconds <= 3600:
            raise ConfigError("diagnostics.session_ttl_seconds must be 60 to 3600")
        if not 8192 <= self.max_evidence_bytes <= 1024 * 1024:
            raise ConfigError("diagnostics.max_evidence_bytes must be 8192 to 1048576")
        if not 1024 <= self.max_log_bytes <= 65536:
            raise ConfigError("diagnostics.max_log_bytes must be 1024 to 65536")
        return self


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million_usd: float
    cached_input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True, slots=True)
class CloudSettings:
    enabled: bool = False
    allow_paid_calls: bool = False
    provider: str = "openai"
    base_url: str = "https://api.openai.com"
    store_responses: bool = False
    luna_model: str = "gpt-5.6-luna"
    terra_model: str = "gpt-5.6-terra"
    sol_model: str = "gpt-5.6-sol"
    allow_automatic_maximum: bool = False
    timeout_seconds: float = 45.0
    max_tool_rounds: int = 4
    max_total_tool_calls: int = 8
    max_wall_seconds: float = 90.0
    max_output_tokens: int = 1200
    max_escalation_steps: int = 2
    max_retries: int = 1
    max_cloud_requests_per_diagnostic: int = 5
    max_estimated_cost_per_request_usd: float = 0.50
    daily_budget_usd: float = 2.0
    monthly_budget_usd: float = 20.0
    pricing_source: str = "https://developers.openai.com/api/docs/models/compare"
    pricing_date: str = "2026-08-11"

    def validated(self) -> CloudSettings:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.openai.com":
            raise ConfigError("cloud.base_url must be https://api.openai.com")
        if self.provider != "openai":
            raise ConfigError("cloud.provider currently supports only openai")
        if (
            self.luna_model != "gpt-5.6-luna"
            or self.terra_model != "gpt-5.6-terra"
            or self.sol_model != "gpt-5.6-sol"
        ):
            raise ConfigError("cloud model IDs must be the reviewed GPT-5.6 Luna/Terra/Sol set")
        if not 1 <= self.max_tool_rounds <= 10:
            raise ConfigError("cloud.max_tool_rounds must be 1 to 10")
        if not 1 <= self.max_total_tool_calls <= 32:
            raise ConfigError("cloud.max_total_tool_calls must be 1 to 32")
        if not 5 <= self.timeout_seconds <= 120:
            raise ConfigError("cloud.timeout_seconds must be 5 to 120")
        if not 5 <= self.max_wall_seconds <= 300:
            raise ConfigError("cloud.max_wall_seconds must be 5 to 300")
        if not 64 <= self.max_output_tokens <= 8192:
            raise ConfigError("cloud.max_output_tokens must be 64 to 8192")
        if not 0 <= self.max_retries <= 3:
            raise ConfigError("cloud.max_retries must be 0 to 3")
        if not 1 <= self.max_escalation_steps <= 3:
            raise ConfigError("cloud.max_escalation_steps must be 1 to 3")
        if not 1 <= self.max_cloud_requests_per_diagnostic <= 16:
            raise ConfigError("cloud.max_cloud_requests_per_diagnostic must be 1 to 16")
        for label, value in (
            ("max_estimated_cost_per_request_usd", self.max_estimated_cost_per_request_usd),
            ("daily_budget_usd", self.daily_budget_usd),
            ("monthly_budget_usd", self.monthly_budget_usd),
        ):
            if value < 0:
                raise ConfigError(f"cloud.{label} cannot be negative")
        return self

    @property
    def pricing(self) -> dict[str, ModelPricing]:
        # Verified from the official model comparison on pricing_date. Keeping
        # this separate from routing makes updates reviewable and testable.
        return {
            self.luna_model: ModelPricing(1.00, 0.10, 6.00),
            self.terra_model: ModelPricing(2.50, 0.25, 15.00),
            self.sol_model: ModelPricing(5.00, 0.50, 30.00),
        }


@dataclass(frozen=True, slots=True)
class RemediationSettings:
    allow_codex_execution: bool = False
    timeout_seconds: float = 900.0
    max_output_bytes: int = 128 * 1024
    repository_root: Path = Path(".")
    deployment_roots: tuple[Path, ...] = (Path("/opt/home-sensor"),)

    def validated(self) -> RemediationSettings:
        if not 30 <= self.timeout_seconds <= 3600:
            raise ConfigError("remediation.timeout_seconds must be 30 to 3600")
        if not 4096 <= self.max_output_bytes <= 4 * 1024 * 1024:
            raise ConfigError("remediation.max_output_bytes must be 4096 to 4194304")
        return self


@dataclass(frozen=True, slots=True)
class EntitySettings:
    entity_id: str
    display_name: str
    sensor_type: str
    source_id: str
    aliases: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    integration: IntegrationSettings
    tts: TTSSettings
    llm: LLMSettings
    diagnostics: DiagnosticSettings
    cloud: CloudSettings
    remediation: RemediationSettings
    entities: tuple[EntitySettings, ...]


def load_assistant_settings(path: Path | None = None) -> AssistantSettings:
    config_path = (path or default_assistant_config_path()).expanduser()
    try:
        with config_path.open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError as exc:
        raise ConfigError(f"assistant configuration not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load assistant configuration: {exc}") from exc

    integration_table = _table(data, "integration")
    integration = IntegrationSettings(
        dashboard_url=str(
            integration_table.get("dashboard_url", "http://127.0.0.1:8080")
        ).rstrip("/"),
        timeout_seconds=float(integration_table.get("timeout_seconds", 4.0)),
        cache_seconds=float(integration_table.get("cache_seconds", 5.0)),
        max_response_bytes=int(
            integration_table.get("max_response_bytes", 2 * 1024 * 1024)
        ),
    ).validated()

    tts_table = _table(data, "tts")
    model_value = str(
        tts_table.get("model_dir", "../models/vits-piper-en_US-kathleen-low")
    )
    model_dir = Path(model_value).expanduser()
    if not model_dir.is_absolute():
        model_dir = config_path.resolve().parent / model_dir
    tts = TTSSettings(
        model_dir=model_dir,
        num_threads=int(tts_table.get("num_threads", 2)),
        speed=float(tts_table.get("speed", 1.0)),
        max_text_chars=int(tts_table.get("max_text_chars", 500)),
    ).validated()

    llm_table = _table(data, "llm")
    llm = LLMSettings(
        enabled=bool(llm_table.get("enabled", False)),
        server_url=str(
            llm_table.get("server_url", "http://127.0.0.1:18080")
        ).rstrip("/"),
        model=str(llm_table.get("model", "butters-router")),
        profile=str(llm_table.get("profile", "lfm2")),
        output_mode=str(llm_table.get("output_mode", "native_tools")),
        timeout_seconds=float(llm_table.get("timeout_seconds", 12.0)),
    ).validated()

    diagnostic_table = _table(data, "diagnostics")
    diagnostics = DiagnosticSettings(
        enabled=bool(diagnostic_table.get("enabled", True)),
        session_ttl_seconds=float(diagnostic_table.get("session_ttl_seconds", 900.0)),
        max_evidence_bytes=int(diagnostic_table.get("max_evidence_bytes", 64 * 1024)),
        max_log_bytes=int(diagnostic_table.get("max_log_bytes", 8 * 1024)),
    ).validated()

    cloud_table = _table(data, "cloud")
    cloud = CloudSettings(
        enabled=bool(cloud_table.get("enabled", False)),
        allow_paid_calls=bool(cloud_table.get("allow_paid_calls", False)),
        provider=str(cloud_table.get("provider", "openai")),
        base_url=str(cloud_table.get("base_url", "https://api.openai.com")).rstrip("/"),
        store_responses=bool(cloud_table.get("store_responses", False)),
        luna_model=str(cloud_table.get("luna_model", "gpt-5.6-luna")),
        terra_model=str(cloud_table.get("terra_model", "gpt-5.6-terra")),
        sol_model=str(cloud_table.get("sol_model", "gpt-5.6-sol")),
        allow_automatic_maximum=bool(cloud_table.get("allow_automatic_maximum", False)),
        timeout_seconds=float(cloud_table.get("timeout_seconds", 45.0)),
        max_tool_rounds=int(cloud_table.get("max_tool_rounds", 4)),
        max_total_tool_calls=int(cloud_table.get("max_total_tool_calls", 8)),
        max_wall_seconds=float(cloud_table.get("max_wall_seconds", 90.0)),
        max_output_tokens=int(cloud_table.get("max_output_tokens", 1200)),
        max_escalation_steps=int(cloud_table.get("max_escalation_steps", 2)),
        max_retries=int(cloud_table.get("max_retries", 1)),
        max_cloud_requests_per_diagnostic=int(cloud_table.get("max_cloud_requests_per_diagnostic", 5)),
        max_estimated_cost_per_request_usd=float(cloud_table.get("max_estimated_cost_per_request_usd", 0.50)),
        daily_budget_usd=float(cloud_table.get("daily_budget_usd", 2.0)),
        monthly_budget_usd=float(cloud_table.get("monthly_budget_usd", 20.0)),
        pricing_source=str(cloud_table.get("pricing_source", "https://developers.openai.com/api/docs/models/compare")),
        pricing_date=str(cloud_table.get("pricing_date", "2026-08-11")),
    ).validated()

    remediation_table = _table(data, "remediation")
    repository_value = Path(str(remediation_table.get("repository_root", "../.."))).expanduser()
    if not repository_value.is_absolute():
        repository_value = (config_path.resolve().parent / repository_value).resolve()
    raw_deployment_roots = remediation_table.get("deployment_roots", ["/opt/home-sensor"])
    if not isinstance(raw_deployment_roots, list) or not all(isinstance(item, str) for item in raw_deployment_roots):
        raise ConfigError("remediation.deployment_roots must be an array of paths")
    remediation = RemediationSettings(
        allow_codex_execution=bool(remediation_table.get("allow_codex_execution", False)),
        timeout_seconds=float(remediation_table.get("timeout_seconds", 900.0)),
        max_output_bytes=int(remediation_table.get("max_output_bytes", 128 * 1024)),
        repository_root=repository_value,
        deployment_roots=tuple(Path(item).expanduser().resolve() for item in raw_deployment_roots),
    ).validated()

    raw_entities = data.get("entities", [])
    if not isinstance(raw_entities, list) or not raw_entities:
        raise ConfigError("assistant configuration requires at least one [[entities]]")
    entities = tuple(_entity(item, index) for index, item in enumerate(raw_entities))
    ids = [entity.entity_id for entity in entities]
    if len(ids) != len(set(ids)):
        raise ConfigError("assistant entity IDs must be unique")
    source_keys = [(entity.sensor_type, entity.source_id) for entity in entities]
    if len(source_keys) != len(set(source_keys)):
        raise ConfigError("assistant entity source mappings must be unique")
    return AssistantSettings(
        integration=integration,
        tts=tts,
        llm=llm,
        diagnostics=diagnostics,
        cloud=cloud,
        remediation=remediation,
        entities=entities,
    )


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _entity(value: Any, index: int) -> EntitySettings:
    if not isinstance(value, dict):
        raise ConfigError(f"entities[{index}] must be a TOML table")
    required = ("id", "display_name", "sensor_type", "source_id")
    missing = [name for name in required if not str(value.get(name, "")).strip()]
    if missing:
        raise ConfigError(f"entities[{index}] missing: {', '.join(missing)}")
    sensor_type = str(value["sensor_type"])
    if sensor_type not in {"environment", "air_quality"}:
        raise ConfigError(f"entities[{index}].sensor_type is unsupported")
    aliases = _string_tuple(value.get("aliases", []), f"entities[{index}].aliases")
    groups = _string_tuple(value.get("groups", []), f"entities[{index}].groups")
    return EntitySettings(
        entity_id=str(value["id"]),
        display_name=str(value["display_name"]),
        sensor_type=sensor_type,
        source_id=str(value["source_id"]),
        aliases=aliases,
        groups=groups,
    )


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(f"{label} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)
