#!/usr/bin/env python3
"""Run the deterministic adversarial AEML parser/validator corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "tests" / "fixtures" / "aeml_security_corpus.json"
MAX_CORPUS_BYTES = 1024 * 1024
MAX_CASES = 1_000
_CASE_KEYS = frozenset({"name", "source", "expected", "known_action_ids"})

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from swoon.aeml import AEMLError, AEMLParser, AEMLValidator  # noqa: E402


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """Load a bounded, strictly shaped JSON corpus."""

    payload = path.read_bytes()
    if len(payload) > MAX_CORPUS_BYTES:
        raise ValueError("AEML evaluation corpus exceeds 1 MiB")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("AEML evaluation corpus must be valid UTF-8 JSON") from error
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CASES:
        raise ValueError(f"AEML evaluation corpus must contain 1-{MAX_CASES} cases")

    cases: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or set(item) - _CASE_KEYS:
            raise ValueError(f"AEML evaluation case {index} has an invalid shape")
        name = item.get("name")
        source = item.get("source")
        expected = item.get("expected")
        known = item.get("known_action_ids", [])
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"AEML evaluation case {index} has an invalid/duplicate name")
        if not isinstance(source, str) or not isinstance(expected, str) or not expected:
            raise ValueError(f"AEML evaluation case {name!r} requires source and expected text")
        if not isinstance(known, list) or not all(isinstance(value, str) for value in known):
            raise ValueError(f"AEML evaluation case {name!r} has invalid known action IDs")
        names.add(name)
        cases.append(
            {
                "name": name,
                "source": source,
                "expected": expected,
                "known_action_ids": known,
            }
        )
    return cases


def evaluate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic case outcomes without executing any AEML action."""

    parser = AEMLParser()
    validator = AEMLValidator()
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            message = parser.parse(case["source"])
            validator.validate(
                message,
                expected_turn=1,
                expected_session="sess_eval",
                known_action_ids=case["known_action_ids"],
            )
            observed = "valid"
        except AEMLError as error:
            observed = error.code
        expected = case["expected"]
        results.append(
            {
                "name": case["name"],
                "expected": expected,
                "observed": observed,
                "passed": observed == expected,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args(argv)
    try:
        results = evaluate_cases(load_corpus(args.corpus.resolve(strict=True)))
    except (OSError, ValueError) as error:
        print(f"AEML evaluation setup failed: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"cases": results}, indent=2, sort_keys=True))
    else:
        for result in results:
            status = "ok" if result["passed"] else "FAIL"
            print(
                f"[{status}] {result['name']}: expected={result['expected']} "
                f"observed={result['observed']}"
            )
        passed = sum(bool(result["passed"]) for result in results)
        print(f"AEML adversarial corpus: {passed}/{len(results)} passed.")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
