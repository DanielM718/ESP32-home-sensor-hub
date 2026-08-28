"""Non-secret configuration for routing, diagnostics, integrations, and TTS."""

from __future__ import annotations

import os
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
class DesktopSettings:
    """Fixed server-side boundary for the one approved Windows desktop."""

    enabled: bool = True
    machine: str = "desktop"
    host: str = "192.168.1.209"
    ssh_port: int = 22
    network_timeout_seconds: float = 45.0
    ssh_timeout_seconds: float = 90.0
    total_timeout_seconds: float = 150.0
    poll_interval_seconds: float = 2.0
    wake_enabled: bool = True
    headless_enabled: bool = True
    monitors_enabled: bool = True
    lock_enabled: bool = False
    sleep_enabled: bool = False
    restart_enabled: bool = False
    shutdown_enabled: bool = False

    def validated(self) -> DesktopSettings:
        if self.machine != "desktop":
            raise ConfigError("desktop.machine must be desktop")
        if not self.host.strip() or not 1 <= self.ssh_port <= 65535:
            raise ConfigError("desktop host/port is invalid")
        if not 1 <= self.poll_interval_seconds <= 10:
            raise ConfigError("desktop.poll_interval_seconds must be 1 to 10")
        if not 5 <= self.network_timeout_seconds <= 180:
            raise ConfigError("desktop.network_timeout_seconds must be 5 to 180")
        if not 5 <= self.ssh_timeout_seconds <= 240:
            raise ConfigError("desktop.ssh_timeout_seconds must be 5 to 240")
        if not 10 <= self.total_timeout_seconds <= 300:
            raise ConfigError("desktop.total_timeout_seconds must be 10 to 300")
        return self


@dataclass(frozen=True, slots=True)
class AuthenticationSettings:
    enabled: bool = True
    rp_id: str = "sensor-pi.tail9644cc.ts.net"
    origin: str = "https://sensor-pi.tail9644cc.ts.net"
    rp_name: str = "Butters"
    elevation_seconds: float = 600.0
    challenge_seconds: float = 120.0
    pending_action_seconds: float = 300.0
    local_console_seconds: float = 45.0
    bootstrap_seconds: float = 600.0
    max_credentials: int = 16
    max_challenges: int = 64

    def validated(self) -> AuthenticationSettings:
        parsed = urlparse(self.origin)
        if parsed.scheme != "https" or parsed.hostname != self.rp_id:
            raise ConfigError("authentication origin must be HTTPS for the exact RP ID")
        if parsed.port is not None or parsed.path not in {"", "/"}:
            raise ConfigError("authentication origin must not include a port or path")
        if not 60 <= self.elevation_seconds <= 3600:
            raise ConfigError("authentication.elevation_seconds must be 60 to 3600")
        if not 30 <= self.challenge_seconds <= 300:
            raise ConfigError("authentication.challenge_seconds must be 30 to 300")
        if not 30 <= self.pending_action_seconds <= 900:
            raise ConfigError("authentication.pending_action_seconds must be 30 to 900")
        if not 10 <= self.local_console_seconds <= 120:
            raise ConfigError("authentication.local_console_seconds must be 10 to 120")
        if not 60 <= self.bootstrap_seconds <= 1800:
            raise ConfigError("authentication.bootstrap_seconds must be 60 to 1800")
        if not 1 <= self.max_credentials <= 64 or not 8 <= self.max_challenges <= 256:
            raise ConfigError("authentication credential/challenge limits are invalid")
        return self


@dataclass(frozen=True, slots=True)
class BrokerSettings:
    enabled: bool = False
    socket_path: Path = Path("/run/butters-action-broker/broker.sock")
    protocol_version: int = 1
    connect_timeout_seconds: float = 2.0
    request_timeout_seconds: float = 30.0
    max_message_bytes: int = 8192

    def validated(self) -> BrokerSettings:
        if not self.socket_path.is_absolute():
            raise ConfigError("broker.socket_path must be absolute")
        if self.protocol_version != 1:
            raise ConfigError("broker.protocol_version must be 1")
        if not 0.1 <= self.connect_timeout_seconds <= 10:
            raise ConfigError("broker.connect_timeout_seconds must be 0.1 to 10")
        if not 1 <= self.request_timeout_seconds <= 300:
            raise ConfigError("broker.request_timeout_seconds must be 1 to 300")
        if not 1024 <= self.max_message_bytes <= 65536:
            raise ConfigError("broker.max_message_bytes must be 1024 to 65536")
        return self


@dataclass(frozen=True, slots=True)
class KnownDeviceSettings:
    enabled: bool = False
    configured: bool = False
    maximum_duration_minutes: int = 120
    local_console_allowed: bool = False
    require_fresh_sensor: bool = False
    safety_entity: str = ""
    safety_max_age_seconds: float = 120.0

    def validated(self, label: str) -> KnownDeviceSettings:
        if not 1 <= self.maximum_duration_minutes <= 1440:
            raise ConfigError(f"{label}.maximum_duration_minutes must be 1 to 1440")
        if not 10 <= self.safety_max_age_seconds <= 3600:
            raise ConfigError(f"{label}.safety_max_age_seconds must be 10 to 3600")
        # A missing safety entity deliberately leaves the capability
        # unavailable; it must not prevent read-only Butters startup.
        return self


@dataclass(frozen=True, slots=True)
class ActionSettings:
    audit_capacity: int = 1000
    job_capacity: int = 256
    host_restart_butters_enabled: bool = False
    host_reboot_enabled: bool = False
    host_shutdown_enabled: bool = False
    nas: KnownDeviceSettings = KnownDeviceSettings()
    heater: KnownDeviceSettings = KnownDeviceSettings()
    dehumidifier: KnownDeviceSettings = KnownDeviceSettings()
    ventilation: KnownDeviceSettings = KnownDeviceSettings()

    def validated(self) -> ActionSettings:
        if not 100 <= self.audit_capacity <= 10000:
            raise ConfigError("actions.audit_capacity must be 100 to 10000")
        if not 32 <= self.job_capacity <= 2048:
            raise ConfigError("actions.job_capacity must be 32 to 2048")
        self.nas.validated("actions.nas")
        self.heater.validated("actions.heater")
        self.dehumidifier.validated("actions.dehumidifier")
        self.ventilation.validated("actions.ventilation")
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
class WebSettings:
    """Non-secret limits for the separate browser service."""

    host: str = "127.0.0.1"
    port: int = 8090
    state_dir: Path = Path("/var/lib/butters")
    trusted_tailscale_proxy: bool = True
    development_mode: bool = False
    admin_identities: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    max_active_sessions: int = 32
    # Anonymous callers may never consume the whole pool: this many slots stay
    # reserved for callers that already present an authorized administrator
    # identity, so a session flood cannot lock the operator out of /admin.
    admin_session_reserve: int = 4
    max_sessions_per_peer: int = 4
    session_create_rate_per_minute: float = 12.0
    session_create_burst: int = 6
    session_ttl_seconds: float = 1800.0
    max_messages_per_session: int = 24
    max_context_chars: int = 12000
    max_request_bytes: int = 16384
    max_workers: int = 4
    max_queued_requests: int = 12
    trace_capacity: int = 256
    trace_ttl_seconds: float = 900.0
    clarification_timeout_seconds: float = 30.0

    def validated(self) -> WebSettings:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ConfigError("web.host must be loopback")
        if not 1024 <= self.port <= 65535:
            raise ConfigError("web.port must be between 1024 and 65535")
        if not 1 <= self.max_active_sessions <= 256:
            raise ConfigError("web.max_active_sessions must be 1 to 256")
        if not 0 <= self.admin_session_reserve < self.max_active_sessions:
            raise ConfigError(
                "web.admin_session_reserve must be 0 to max_active_sessions - 1"
            )
        if not 1 <= self.max_sessions_per_peer <= self.max_active_sessions:
            raise ConfigError(
                "web.max_sessions_per_peer must be 1 to max_active_sessions"
            )
        if not 1 <= self.session_create_rate_per_minute <= 600:
            raise ConfigError("web.session_create_rate_per_minute must be 1 to 600")
        if not 1 <= self.session_create_burst <= 64:
            raise ConfigError("web.session_create_burst must be 1 to 64")
        if not 60 <= self.session_ttl_seconds <= 86400:
            raise ConfigError("web.session_ttl_seconds must be 60 to 86400")
        if not 2 <= self.max_messages_per_session <= 100:
            raise ConfigError("web.max_messages_per_session must be 2 to 100")
        if not 1000 <= self.max_context_chars <= 100000:
            raise ConfigError("web.max_context_chars must be 1000 to 100000")
        if not 1024 <= self.max_request_bytes <= 1024 * 1024:
            raise ConfigError("web.max_request_bytes must be 1024 to 1048576")
        if not 1 <= self.max_workers <= 16:
            raise ConfigError("web.max_workers must be 1 to 16")
        if not 1 <= self.max_queued_requests <= 128:
            raise ConfigError("web.max_queued_requests must be 1 to 128")
        if not 32 <= self.trace_capacity <= 4096:
            raise ConfigError("web.trace_capacity must be 32 to 4096")
        if not 60 <= self.trace_ttl_seconds <= 86400:
            raise ConfigError("web.trace_ttl_seconds must be 60 to 86400")
        if not 5 <= self.clarification_timeout_seconds <= 300:
            raise ConfigError("web.clarification_timeout_seconds must be 5 to 300")
        return self

    @property
    def production_origin_configured(self) -> bool:
        """Production browsers are only trusted against a server-known origin."""

        return self.development_mode or bool(self.allowed_origins)


@dataclass(frozen=True, slots=True)
class BrowserAudioSettings:
    max_utterance_seconds: float = 30.0
    max_chunk_bytes: int = 32768
    max_buffered_bytes: int = 1024 * 1024
    idle_timeout_seconds: float = 10.0
    session_timeout_seconds: float = 60.0
    # The selected accurate model uses roughly 240-293 MiB per recognizer on
    # the Pi 4. One warm engine is intentionally serialized; extra callers use
    # the bounded queue instead of multiplying resident models.
    max_concurrent_sessions: int = 1
    max_queue_depth: int = 2
    allowed_sample_rates: tuple[int, ...] = (16000, 44100, 48000, 96000)

    def validated(self) -> BrowserAudioSettings:
        if not 1 <= self.max_utterance_seconds <= 120:
            raise ConfigError("browser_audio.max_utterance_seconds must be 1 to 120")
        if not 640 <= self.max_chunk_bytes <= 256 * 1024:
            raise ConfigError("browser_audio.max_chunk_bytes must be 640 to 262144")
        if not self.max_chunk_bytes <= self.max_buffered_bytes <= 8 * 1024 * 1024:
            raise ConfigError(
                "browser_audio.max_buffered_bytes is outside its safe range"
            )
        if not 1 <= self.idle_timeout_seconds <= 60:
            raise ConfigError("browser_audio.idle_timeout_seconds must be 1 to 60")
        if not self.idle_timeout_seconds <= self.session_timeout_seconds <= 300:
            raise ConfigError(
                "browser_audio.session_timeout_seconds is outside its safe range"
            )
        if not 1 <= self.max_concurrent_sessions <= 8:
            raise ConfigError("browser_audio.max_concurrent_sessions must be 1 to 8")
        if not 0 <= self.max_queue_depth <= 16:
            raise ConfigError("browser_audio.max_queue_depth must be 0 to 16")
        if not self.allowed_sample_rates or any(
            rate < 8000 or rate > 96000 for rate in self.allowed_sample_rates
        ):
            raise ConfigError(
                "browser_audio.allowed_sample_rates contains an unsafe rate"
            )
        return self


@dataclass(frozen=True, slots=True)
class SpeechProviderSettings:
    stt_default: str = "local"
    tts_default: str = "local"
    allow_paid_stt: bool = False
    allow_paid_tts: bool = False
    cloud_stt_model: str = "gpt-4o-mini-transcribe"
    cloud_tts_model: str = "gpt-4o-mini-tts"
    cloud_tts_voice: str = "cedar"
    cloud_tts_speed: float = 1.0
    cloud_tts_instructions: str = "Warm, concise, calm home assistant."
    cloud_stt_price_per_minute_usd: float | None = None
    cloud_tts_price_per_million_characters_usd: float | None = None

    def validated(self) -> SpeechProviderSettings:
        if self.stt_default not in {"local", "openai"}:
            raise ConfigError("providers.stt_default must be local or openai")
        if self.tts_default not in {"local", "openai"}:
            raise ConfigError("providers.tts_default must be local or openai")
        if not self.cloud_stt_model.strip() or not self.cloud_tts_model.strip():
            raise ConfigError("providers cloud model names cannot be empty")
        if not 0.25 <= self.cloud_tts_speed <= 4.0:
            raise ConfigError("providers.cloud_tts_speed must be 0.25 to 4")
        if len(self.cloud_tts_instructions) > 1000:
            raise ConfigError("providers.cloud_tts_instructions is too long")
        for name, value in (
            ("cloud_stt_price_per_minute_usd", self.cloud_stt_price_per_minute_usd),
            (
                "cloud_tts_price_per_million_characters_usd",
                self.cloud_tts_price_per_million_characters_usd,
            ),
        ):
            if value is not None and value <= 0:
                raise ConfigError(f"providers.{name} must be positive when configured")
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
    max_usage_records: int = 50000
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
            raise ConfigError(
                "cloud model IDs must be the reviewed GPT-5.6 Luna/Terra/Sol set"
            )
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
        if not 1000 <= self.max_usage_records <= 1_000_000:
            raise ConfigError("cloud.max_usage_records must be 1000 to 1000000")
        for label, value in (
            (
                "max_estimated_cost_per_request_usd",
                self.max_estimated_cost_per_request_usd,
            ),
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
    jobs_dir: Path = Path("/var/lib/butters/codex-jobs")
    max_patch_bytes: int = 512 * 1024
    max_generated_file_bytes: int = 256 * 1024
    max_retained_jobs: int = 50
    # Read-only repository inspection is a deliberate, separately configured
    # deployment decision. An ordinary deployed daemon has no readable checkout
    # and must report repository_unavailable instead of reaching into a private
    # developer home directory.
    project_inspection_root: Path | None = None

    def validated(self) -> RemediationSettings:
        if not 30 <= self.timeout_seconds <= 3600:
            raise ConfigError("remediation.timeout_seconds must be 30 to 3600")
        if not 4096 <= self.max_output_bytes <= 4 * 1024 * 1024:
            raise ConfigError("remediation.max_output_bytes must be 4096 to 4194304")
        if not 16384 <= self.max_patch_bytes <= 4 * 1024 * 1024:
            raise ConfigError("remediation.max_patch_bytes must be 16384 to 4194304")
        if not 4096 <= self.max_generated_file_bytes <= self.max_patch_bytes:
            raise ConfigError(
                "remediation.max_generated_file_bytes must be 4096 to max_patch_bytes"
            )
        if not 1 <= self.max_retained_jobs <= 500:
            raise ConfigError("remediation.max_retained_jobs must be 1 to 500")
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
    web: WebSettings = WebSettings()
    browser_audio: BrowserAudioSettings = BrowserAudioSettings()
    providers: SpeechProviderSettings = SpeechProviderSettings()
    desktop: DesktopSettings = DesktopSettings()
    authentication: AuthenticationSettings = AuthenticationSettings()
    broker: BrokerSettings = BrokerSettings()
    actions: ActionSettings = ActionSettings()


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

    web_table = _table(data, "web")
    state_dir = Path(
        os.getenv(
            "BUTTERS_STATE_DIR", str(web_table.get("state_dir", "/var/lib/butters"))
        )
    ).expanduser()
    web = WebSettings(
        host=str(web_table.get("host", "127.0.0.1")),
        port=int(web_table.get("port", 8090)),
        state_dir=state_dir,
        trusted_tailscale_proxy=bool(web_table.get("trusted_tailscale_proxy", True)),
        development_mode=bool(web_table.get("development_mode", False)),
        admin_identities=_string_tuple(
            web_table.get("admin_identities", []), "web.admin_identities"
        ),
        allowed_origins=_origin_tuple(web_table.get("allowed_origins", [])),
        max_active_sessions=int(web_table.get("max_active_sessions", 32)),
        admin_session_reserve=int(web_table.get("admin_session_reserve", 4)),
        max_sessions_per_peer=int(web_table.get("max_sessions_per_peer", 4)),
        session_create_rate_per_minute=float(
            web_table.get("session_create_rate_per_minute", 12.0)
        ),
        session_create_burst=int(web_table.get("session_create_burst", 6)),
        session_ttl_seconds=float(web_table.get("session_ttl_seconds", 1800.0)),
        max_messages_per_session=int(web_table.get("max_messages_per_session", 24)),
        max_context_chars=int(web_table.get("max_context_chars", 12000)),
        max_request_bytes=int(web_table.get("max_request_bytes", 16384)),
        max_workers=int(web_table.get("max_workers", 4)),
        max_queued_requests=int(web_table.get("max_queued_requests", 12)),
        trace_capacity=int(web_table.get("trace_capacity", 256)),
        trace_ttl_seconds=float(web_table.get("trace_ttl_seconds", 900.0)),
        clarification_timeout_seconds=float(
            web_table.get("clarification_timeout_seconds", 30.0)
        ),
    ).validated()

    audio_table = _table(data, "browser_audio")
    raw_rates = audio_table.get(
        "allowed_sample_rates", [16000, 44100, 48000, 96000]
    )
    if not isinstance(raw_rates, list) or not all(
        isinstance(item, int) for item in raw_rates
    ):
        raise ConfigError("browser_audio.allowed_sample_rates must be an integer array")
    browser_audio = BrowserAudioSettings(
        max_utterance_seconds=float(audio_table.get("max_utterance_seconds", 30.0)),
        max_chunk_bytes=int(audio_table.get("max_chunk_bytes", 32768)),
        max_buffered_bytes=int(audio_table.get("max_buffered_bytes", 1024 * 1024)),
        idle_timeout_seconds=float(audio_table.get("idle_timeout_seconds", 10.0)),
        session_timeout_seconds=float(audio_table.get("session_timeout_seconds", 60.0)),
        max_concurrent_sessions=int(audio_table.get("max_concurrent_sessions", 1)),
        max_queue_depth=int(audio_table.get("max_queue_depth", 2)),
        allowed_sample_rates=tuple(raw_rates),
    ).validated()

    provider_table = _table(data, "providers")
    providers = SpeechProviderSettings(
        stt_default=str(provider_table.get("stt_default", "local")),
        tts_default=str(provider_table.get("tts_default", "local")),
        allow_paid_stt=bool(provider_table.get("allow_paid_stt", False)),
        allow_paid_tts=bool(provider_table.get("allow_paid_tts", False)),
        cloud_stt_model=str(
            provider_table.get("cloud_stt_model", "gpt-4o-mini-transcribe")
        ),
        cloud_tts_model=str(provider_table.get("cloud_tts_model", "gpt-4o-mini-tts")),
        cloud_tts_voice=str(provider_table.get("cloud_tts_voice", "cedar")),
        cloud_tts_speed=float(provider_table.get("cloud_tts_speed", 1.0)),
        cloud_tts_instructions=str(
            provider_table.get(
                "cloud_tts_instructions", "Warm, concise, calm home assistant."
            )
        ),
        cloud_stt_price_per_minute_usd=_optional_float(
            provider_table.get("cloud_stt_price_per_minute_usd")
        ),
        cloud_tts_price_per_million_characters_usd=_optional_float(
            provider_table.get("cloud_tts_price_per_million_characters_usd")
        ),
    ).validated()

    desktop_table = _table(data, "desktop")
    desktop = DesktopSettings(
        enabled=bool(desktop_table.get("enabled", True)),
        machine=str(desktop_table.get("machine", "desktop")),
        host=str(desktop_table.get("host", "192.168.1.209")),
        ssh_port=int(desktop_table.get("ssh_port", 22)),
        network_timeout_seconds=float(
            desktop_table.get("network_timeout_seconds", 45.0)
        ),
        ssh_timeout_seconds=float(desktop_table.get("ssh_timeout_seconds", 90.0)),
        total_timeout_seconds=float(desktop_table.get("total_timeout_seconds", 150.0)),
        poll_interval_seconds=float(desktop_table.get("poll_interval_seconds", 2.0)),
        wake_enabled=bool(desktop_table.get("wake_enabled", True)),
        headless_enabled=bool(desktop_table.get("headless_enabled", True)),
        monitors_enabled=bool(desktop_table.get("monitors_enabled", True)),
        lock_enabled=bool(desktop_table.get("lock_enabled", False)),
        sleep_enabled=bool(desktop_table.get("sleep_enabled", False)),
        restart_enabled=bool(desktop_table.get("restart_enabled", False)),
        shutdown_enabled=bool(desktop_table.get("shutdown_enabled", False)),
    ).validated()

    authentication_table = _table(data, "authentication")
    authentication = AuthenticationSettings(
        enabled=bool(authentication_table.get("enabled", True)),
        rp_id=str(authentication_table.get("rp_id", "sensor-pi.tail9644cc.ts.net")),
        origin=str(
            authentication_table.get("origin", "https://sensor-pi.tail9644cc.ts.net")
        ).rstrip("/"),
        rp_name=str(authentication_table.get("rp_name", "Butters")),
        elevation_seconds=float(authentication_table.get("elevation_seconds", 600.0)),
        challenge_seconds=float(authentication_table.get("challenge_seconds", 120.0)),
        pending_action_seconds=float(
            authentication_table.get("pending_action_seconds", 300.0)
        ),
        local_console_seconds=float(
            authentication_table.get("local_console_seconds", 45.0)
        ),
        bootstrap_seconds=float(authentication_table.get("bootstrap_seconds", 600.0)),
        max_credentials=int(authentication_table.get("max_credentials", 16)),
        max_challenges=int(authentication_table.get("max_challenges", 64)),
    ).validated()

    broker_table = _table(data, "broker")
    broker = BrokerSettings(
        enabled=bool(broker_table.get("enabled", False)),
        socket_path=Path(
            str(
                broker_table.get(
                    "socket_path", "/run/butters-action-broker/broker.sock"
                )
            )
        ),
        protocol_version=int(broker_table.get("protocol_version", 1)),
        connect_timeout_seconds=float(broker_table.get("connect_timeout_seconds", 2.0)),
        request_timeout_seconds=float(
            broker_table.get("request_timeout_seconds", 30.0)
        ),
        max_message_bytes=int(broker_table.get("max_message_bytes", 8192)),
    ).validated()

    actions_table = _table(data, "actions")

    def device_settings(name: str) -> KnownDeviceSettings:
        table = _table(actions_table, name)
        return KnownDeviceSettings(
            enabled=bool(table.get("enabled", False)),
            configured=bool(table.get("configured", False)),
            maximum_duration_minutes=int(table.get("maximum_duration_minutes", 120)),
            local_console_allowed=bool(table.get("local_console_allowed", False)),
            require_fresh_sensor=bool(table.get("require_fresh_sensor", False)),
            safety_entity=str(table.get("safety_entity", "")),
            safety_max_age_seconds=float(table.get("safety_max_age_seconds", 120.0)),
        ).validated(f"actions.{name}")

    actions = ActionSettings(
        audit_capacity=int(actions_table.get("audit_capacity", 1000)),
        job_capacity=int(actions_table.get("job_capacity", 256)),
        host_restart_butters_enabled=bool(
            actions_table.get("host_restart_butters_enabled", False)
        ),
        host_reboot_enabled=bool(actions_table.get("host_reboot_enabled", False)),
        host_shutdown_enabled=bool(actions_table.get("host_shutdown_enabled", False)),
        nas=device_settings("nas"),
        heater=device_settings("heater"),
        dehumidifier=device_settings("dehumidifier"),
        ventilation=device_settings("ventilation"),
    ).validated()

    llm_table = _table(data, "llm")
    llm = LLMSettings(
        enabled=bool(llm_table.get("enabled", False)),
        server_url=str(llm_table.get("server_url", "http://127.0.0.1:18080")).rstrip(
            "/"
        ),
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
        max_cloud_requests_per_diagnostic=int(
            cloud_table.get("max_cloud_requests_per_diagnostic", 5)
        ),
        max_estimated_cost_per_request_usd=float(
            cloud_table.get("max_estimated_cost_per_request_usd", 0.50)
        ),
        daily_budget_usd=float(cloud_table.get("daily_budget_usd", 2.0)),
        monthly_budget_usd=float(cloud_table.get("monthly_budget_usd", 20.0)),
        max_usage_records=int(cloud_table.get("max_usage_records", 50000)),
        pricing_source=str(
            cloud_table.get(
                "pricing_source",
                "https://developers.openai.com/api/docs/models/compare",
            )
        ),
        pricing_date=str(cloud_table.get("pricing_date", "2026-08-11")),
    ).validated()

    remediation_table = _table(data, "remediation")
    repository_value = Path(
        os.getenv(
            "BUTTERS_REPOSITORY_ROOT",
            str(remediation_table.get("repository_root", "../..")),
        )
    ).expanduser()
    if not repository_value.is_absolute():
        repository_value = (config_path.resolve().parent / repository_value).resolve()
    raw_deployment_roots = remediation_table.get(
        "deployment_roots", ["/opt/home-sensor"]
    )
    if not isinstance(raw_deployment_roots, list) or not all(
        isinstance(item, str) for item in raw_deployment_roots
    ):
        raise ConfigError("remediation.deployment_roots must be an array of paths")
    raw_project_root = os.getenv(
        "BUTTERS_PROJECT_INSPECTION_ROOT",
        str(remediation_table.get("project_inspection_root", "")),
    ).strip()
    project_root: Path | None = None
    if raw_project_root:
        project_root = Path(raw_project_root).expanduser()
        if not project_root.is_absolute():
            project_root = (config_path.resolve().parent / project_root).resolve()
    remediation = RemediationSettings(
        allow_codex_execution=bool(
            remediation_table.get("allow_codex_execution", False)
        ),
        timeout_seconds=float(remediation_table.get("timeout_seconds", 900.0)),
        max_output_bytes=int(remediation_table.get("max_output_bytes", 128 * 1024)),
        repository_root=repository_value,
        deployment_roots=tuple(
            Path(item).expanduser().resolve() for item in raw_deployment_roots
        ),
        jobs_dir=Path(
            os.getenv(
                "BUTTERS_CODEX_JOBS_DIR",
                str(remediation_table.get("jobs_dir", "/var/lib/butters/codex-jobs")),
            )
        ).expanduser(),
        max_patch_bytes=int(remediation_table.get("max_patch_bytes", 512 * 1024)),
        max_generated_file_bytes=int(
            remediation_table.get("max_generated_file_bytes", 256 * 1024)
        ),
        max_retained_jobs=int(remediation_table.get("max_retained_jobs", 50)),
        project_inspection_root=project_root,
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
        web=web,
        browser_audio=browser_audio,
        providers=providers,
        desktop=desktop,
        authentication=authentication,
        broker=broker,
        actions=actions,
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
    if sensor_type not in {"environment", "air_quality", "printer"}:
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


def _origin_tuple(value: Any) -> tuple[str, ...]:
    """Merge configured origins with the deployment override, normalizing form.

    The private Tailscale HTTPS origin is only known after `tailscale serve`
    runs, so the installer records it in the non-secret deployment file rather
    than in the reviewed application config.
    """

    configured = list(_string_tuple(value, "web.allowed_origins"))
    configured.extend(
        item.strip()
        for item in os.getenv("BUTTERS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )
    normalized: list[str] = []
    for item in configured:
        candidate = item.rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
            raise ConfigError(
                "web.allowed_origins entries must be scheme://host[:port] values"
            )
        entry = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if entry not in normalized:
            normalized.append(entry)
    return tuple(normalized)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("optional price values must be numeric")
    return float(value)
