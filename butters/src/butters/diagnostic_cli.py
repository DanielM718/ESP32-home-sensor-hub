"""Text-mode local diagnostic CLI with explicit development-only cloud opt-in."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from butters.assistant_config import load_assistant_settings
from butters.cloud.openai_responses import OpenAIResponsesReasoner
from butters.cloud.orchestrator import CloudDiagnosticEscalator
from butters.cloud.routing import EscalationPolicy
from butters.diagnostics.engine import DiagnosticEngine
from butters.diagnostics.model import DiagnosticAnswer, DiagnosticDomain, DiagnosticRequest, RequestDepth
from butters.diagnostics.planner import DiagnosticPlanner
from butters.diagnostics.tools import build_diagnostic_registry
from butters.routing.entities import EntityRegistry


SUBJECT_REQUESTS: dict[str, tuple[DiagnosticDomain, str]] = {
    "stack": (DiagnosticDomain.MONITORING_STACK, "Is the monitoring stack working?"),
    "grafana": (DiagnosticDomain.GRAFANA, "Diagnose why Grafana is not showing current data"),
    "mqtt": (DiagnosticDomain.MQTT, "Diagnose MQTT health"),
    "influx": (DiagnosticDomain.INFLUXDB, "Diagnose InfluxDB health"),
    "server": (DiagnosticDomain.SERVER, "Diagnose server health"),
    "kr260": (DiagnosticDomain.KR260, "Diagnose KR260 basic health"),
    "home-assistant": (DiagnosticDomain.HOME_ASSISTANT, "Diagnose the Home Assistant sensor integration"),
}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = load_assistant_settings()
    entities = EntityRegistry(settings.entities)
    planner = DiagnosticPlanner(entities)
    request = _request(args, planner, parser)
    tools = build_diagnostic_registry(settings)
    cloud = None
    if args.allow_cloud:
        if not settings.cloud.enabled or not settings.cloud.allow_paid_calls:
            parser.error("cloud is disabled in assistant.toml; both cloud flags must be explicitly enabled")
        if not os.getenv("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY is absent; local diagnostics remain available")
        reasoner = OpenAIResponsesReasoner(settings.cloud)
        cloud = CloudDiagnosticEscalator(reasoner, tools, settings.cloud)
    engine = DiagnosticEngine(
        planner,
        tools,
        cloud=cloud,
        session_ttl_seconds=settings.diagnostics.session_ttl_seconds,
        max_evidence_bytes=settings.diagnostics.max_evidence_bytes,
    )
    answer = engine.diagnose(request)
    if args.dry_run_cloud:
        policy = EscalationPolicy(settings.cloud)
        configuration = policy.initial(request, answer.assessment)
        payload = {
            "network_request_sent": False,
            "api_key_included": False,
            "would_escalate": answer.assessment.escalation_required,
            "model": configuration.model,
            "reasoning_effort": configuration.effort,
            "pro_mode": configuration.pro_mode,
            "available_tools": [item.name for item in tools.tools],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.json:
        print(json.dumps(_answer_dict(answer), indent=2, sort_keys=True))
    else:
        _print_text(answer, show_evidence=args.show_evidence, show_route=args.show_route)
    return 0 if answer.assessment.status.value != "failed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded read-only Butters diagnostics")
    parser.add_argument("subject", nargs="?", choices=(*SUBJECT_REQUESTS, "sensor"))
    parser.add_argument("target", nargs="?", help="configured entity ID for the sensor subject")
    parser.add_argument("--request", help="natural-language diagnostic request")
    parser.add_argument("--local-only", action="store_true", help="forbid cloud escalation")
    parser.add_argument("--allow-cloud", action="store_true", help="permit configured paid cloud escalation")
    parser.add_argument("--show-evidence", action="store_true")
    parser.add_argument("--show-route", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-escalation", type=int, choices=range(0, 5), default=3)
    parser.add_argument("--dry-run-cloud", action="store_true", help="show a non-secret route preview; never call the API")
    return parser


def _request(args: argparse.Namespace, planner: DiagnosticPlanner, parser: argparse.ArgumentParser) -> DiagnosticRequest:
    if args.request:
        if args.subject:
            parser.error("use either a subject or --request, not both")
        parsed = planner.request_from_text(
            args.request,
            local_only=args.local_only or not args.allow_cloud or args.max_escalation == 0,
            allow_cloud=args.allow_cloud,
            max_escalation=args.max_escalation,
        )
        if parsed is None:
            parser.error("request does not match a supported diagnostic domain")
        return parsed
    if not args.subject:
        parser.error("a subject or --request is required")
    if args.subject == "sensor":
        if not args.target or planner.entities.get(args.target) is None:
            parser.error("sensor requires one configured entity ID")
        return DiagnosticRequest(
            f"Why is sensor {args.target} not reporting?",
            DiagnosticDomain.SENSOR,
            args.target,
            local_only=args.local_only or not args.allow_cloud or args.max_escalation == 0,
            allow_cloud=args.allow_cloud,
            max_escalation=args.max_escalation,
        )
    if args.target:
        parser.error("target is only valid with the sensor subject")
    domain, text = SUBJECT_REQUESTS[args.subject]
    return DiagnosticRequest(
        text,
        domain,
        depth=RequestDepth.NORMAL,
        local_only=args.local_only or not args.allow_cloud or args.max_escalation == 0,
        allow_cloud=args.allow_cloud,
        max_escalation=args.max_escalation,
    )


def _answer_dict(answer: DiagnosticAnswer) -> dict[str, object]:
    value = asdict(answer)
    assessment = value.get("assessment")
    if isinstance(assessment, dict):
        assessment["evidence"] = answer.assessment.evidence.as_dict()
    return json.loads(json.dumps(value, default=lambda item: getattr(item, "value", str(item))))


def _print_text(answer: object, *, show_evidence: bool, show_route: bool) -> None:
    result = answer  # keep attribute output concise without another protocol type
    if show_route:
        print(f"ROUTE: {result.route}")
        print(f"PLAYBOOK: {result.playbook}")
    print(f"STATUS: {result.assessment.status.value}")
    print(f"CONFIDENCE: {result.assessment.confidence.value}")
    print(f"ROOT_CAUSE: {result.assessment.root_cause or 'unresolved'}")
    print(f"CLOUD_USED: {'yes' if result.cloud_used else 'no'}")
    print(f"STOPPING_REASON: {result.stopping_reason}")
    if show_evidence:
        print("EVIDENCE:")
        for item in result.assessment.evidence.items:
            print(f"  {item.evidence_id}: {item.status.value} {json.dumps(item.values, sort_keys=True)}")
    print("FINAL:")
    print(result.detailed_text)


if __name__ == "__main__":
    sys.exit(main())
