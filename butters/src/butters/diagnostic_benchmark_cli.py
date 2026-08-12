"""Print reproducible offline diagnostic corpus metrics."""

from __future__ import annotations

import json
from dataclasses import asdict

from butters.diagnostics.evaluation import evaluate_corpus


def main() -> int:
    print(json.dumps(asdict(evaluate_corpus()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
