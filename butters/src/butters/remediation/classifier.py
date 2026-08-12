"""Deterministic routing to operational diagnosis or engineering inspection."""

from __future__ import annotations

from butters.diagnostics.model import DiagnosticAssessment
from butters.remediation.model import EngineeringClassification


def classify_remediation(assessment: DiagnosticAssessment) -> EngineeringClassification:
    text = " ".join(
        [
            assessment.root_cause or "",
            *(finding.summary for finding in assessment.findings),
            *(item.error or "" for item in assessment.evidence.items),
            *(item.text_excerpt or "" for item in assessment.evidence.items),
        ]
    ).lower()
    if any(marker in text for marker in ("traceback", "assertionerror", "typeerror", "keyerror", "regression", "application code")):
        return EngineeringClassification.SOFTWARE_DEFECT
    if any(marker in text for marker in ("deployment revision", "deployed version", "unit file mismatch", "repository differs")):
        return EngineeringClassification.DEPLOYMENT_PROBLEM
    if any(marker in text for marker in ("invalid configuration", "configuration mismatch", "config parse", "unknown setting")):
        return EngineeringClassification.CONFIGURATION_PROBLEM
    if any(marker in text for marker in ("inactive", "unreachable", "stale", "offline", "high load", "thermal", "port closed")):
        return EngineeringClassification.OPERATIONAL_DIAGNOSTIC
    return EngineeringClassification.UNKNOWN
