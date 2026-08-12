"""Non-secret cloud usage telemetry, cost estimation, and in-memory budgets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from butters.assistant_config import CloudSettings
from butters.cloud.model import CloudTokenUsage, ReasoningConfiguration


@dataclass(frozen=True, slots=True)
class CloudUsageRecord:
    timestamp: str
    request_category: str
    model: str
    reasoning_effort: str
    escalation_level: int
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_rounds: int
    wall_seconds: float
    estimated_cost_usd: float
    success: bool
    escalation_occurred: bool
    error_code: str | None = None


class UsageLedger:
    """Process-local budgets; no user text or evidence is retained."""

    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings
        self.records: list[CloudUsageRecord] = []

    def estimated_cost(self, model: str, usage: CloudTokenUsage) -> float:
        price = self.settings.pricing.get(model)
        if price is None:
            return float("inf")
        uncached = max(0, usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens)
        return (
            uncached * price.input_per_million_usd
            + usage.cached_tokens * price.cached_input_per_million_usd
            + usage.cache_write_tokens * price.input_per_million_usd * 1.25
            + usage.output_tokens * price.output_per_million_usd
        ) / 1_000_000

    def conservative_request_estimate(
        self, model: str, evidence_bytes: int, max_output_tokens: int
    ) -> float:
        approximate_input = max(512, evidence_bytes // 3 + 1500)
        return self.estimated_cost(
            model, CloudTokenUsage(input_tokens=approximate_input, output_tokens=max_output_tokens)
        )

    def permits(self, estimate: float) -> bool:
        if estimate > self.settings.max_estimated_cost_per_request_usd:
            return False
        now = datetime.now(timezone.utc)
        day_prefix = now.date().isoformat()
        month_prefix = day_prefix[:7]
        daily = sum(record.estimated_cost_usd for record in self.records if record.timestamp.startswith(day_prefix))
        monthly = sum(record.estimated_cost_usd for record in self.records if record.timestamp.startswith(month_prefix))
        return daily + estimate <= self.settings.daily_budget_usd and monthly + estimate <= self.settings.monthly_budget_usd

    def record(
        self,
        category: str,
        configuration: ReasoningConfiguration,
        usage: CloudTokenUsage,
        *,
        tool_rounds: int,
        wall_seconds: float,
        success: bool,
        escalation_occurred: bool,
        error_code: str | None = None,
    ) -> CloudUsageRecord:
        record = CloudUsageRecord(
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            category,
            configuration.model,
            configuration.effort,
            int(configuration.level),
            usage.input_tokens,
            usage.cached_tokens,
            usage.cache_write_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
            tool_rounds,
            wall_seconds,
            self.estimated_cost(configuration.model, usage),
            success,
            escalation_occurred,
            error_code,
        )
        self.records.append(record)
        return record
