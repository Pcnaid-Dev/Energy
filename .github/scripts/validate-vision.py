#!/usr/bin/env python3
"""Validate a vision audit result and enforce FAIL/MUST findings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


CHECK_NAMES = (
    "legible",
    "centred",
    "matches_sibling",
    "no_overflow",
    "no_raw_markdown",
)


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def validate_normalized_answer(
    answer: Any, context: str, errors: list[str]
) -> tuple[set[str], set[str]] | None:
    if not isinstance(answer, dict):
        errors.append(f"{context} is not an object")
        return None
    controls = answer.get("controls")
    if not isinstance(controls, list) or not controls:
        errors.append(f"{context} controls must be a non-empty list")
        return None

    failed_names: set[str] = set()
    passed_names: set[str] = set()
    has_failure = False
    for control_index, control in enumerate(controls):
        control_context = f"{context} control {control_index}"
        if not isinstance(control, dict):
            errors.append(f"{control_context} is not an object")
            continue
        name = control.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{control_context} has no name")
        if not isinstance(control.get("reason"), str) or not control["reason"].strip():
            errors.append(f"{control_context} has no reason")

        checks = control.get("checks")
        if not isinstance(checks, dict):
            errors.append(f"{control_context} checks is not an object")
            checks = {}
        check_failed = False
        checks_valid = True
        for check_name in CHECK_NAMES:
            value = checks.get(check_name)
            if not isinstance(value, bool):
                errors.append(f"{control_context} check {check_name} is not boolean")
                checks_valid = False
            elif value is False:
                check_failed = True

        expected_verdict = "FAIL" if check_failed else "PASS"
        if control.get("verdict") != expected_verdict:
            errors.append(
                f"{control_context} verdict is not derived from its boolean checks"
            )
        if checks_valid:
            name_is_valid = isinstance(name, str) and bool(name.strip())
            if check_failed:
                has_failure = True
                if name_is_valid:
                    failed_names.add(name.strip().casefold())
            elif name_is_valid:
                passed_names.add(name.strip().casefold())

    expected_verdict = "FAIL" if has_failure else "PASS"
    if answer.get("verdict") != expected_verdict:
        errors.append(f"{context} verdict is not derived from its controls")
    return failed_names, passed_names


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate-vision.py <result.json>", file=sys.stderr)
        return 2

    result_path = Path(sys.argv[1])
    errors: list[str] = []
    rows: list[tuple[str, int, str, str]] = []

    try:
        document = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        document = {}
        errors.append(f"Cannot read valid {result_path}: {error}")

    expected_images = sorted(path.name for path in Path("visual-out").rglob("*.png"))
    if not isinstance(document.get("model"), str) or not document["model"].strip():
        errors.append("model must be a non-empty string")
    results = document.get("results")
    if not isinstance(results, list):
        results = []
        errors.append("results must be a list")

    seen_images: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            errors.append(f"result {index} is not an object")
            continue
        image = Path(str(result.get("image", ""))).name
        seen_images.append(image)
        controls = result.get("controls")
        verdict = result.get("verdict")
        reason = "All reported controls passed."
        rechecks = result.get("rechecks")

        if rechecks is not None:
            if not isinstance(rechecks, list) or len(rechecks) != 2:
                errors.append(f"result {index} rechecks must contain exactly two answers")
            else:
                first_names = validate_normalized_answer(
                    rechecks[0], f"result {index} recheck 0", errors
                )
                second_names = validate_normalized_answer(
                    rechecks[1], f"result {index} recheck 1", errors
                )
                if first_names is not None and second_names is not None:
                    first_failed, _ = first_names
                    _, second_passed = second_names
                    if not first_failed:
                        errors.append(f"result {index} recheck 0 has no failed control")
                    expected_verdict = (
                        "FAIL" if first_failed - second_passed else "PASS"
                    )
                    if not result.get("error") and verdict != expected_verdict:
                        errors.append(
                            f"result {index} verdict does not match its recheck consensus"
                        )

        if not isinstance(result.get("route"), str) or not result["route"].startswith("/"):
            errors.append(f"result {index} has an invalid route")
        if result.get("viewport") not in ("mobile", "desktop"):
            errors.append(f"result {index} has an invalid viewport")
        if not isinstance(result.get("state"), str) or not result["state"].strip():
            errors.append(f"result {index} has an invalid state")
        if not isinstance(result.get("pairs"), list):
            errors.append(f"result {index} pairs must be a list")
        if verdict not in ("PASS", "FAIL"):
            errors.append(f"result {index} has an invalid verdict")
        if not isinstance(controls, list) or not controls:
            errors.append(f"result {index} controls must be a non-empty list")
            controls = []

        for control_index, control in enumerate(controls):
            if not isinstance(control, dict):
                errors.append(f"result {index} control {control_index} is not an object")
                continue
            if not isinstance(control.get("name"), str) or not control["name"].strip():
                errors.append(f"result {index} control {control_index} has no name")
            if not isinstance(control.get("reason"), str) or not control["reason"].strip():
                errors.append(f"result {index} control {control_index} has no reason")
            if control.get("verdict") not in ("PASS", "FAIL"):
                errors.append(f"result {index} control {control_index} has an invalid verdict")
            checks = control.get("checks")
            if not isinstance(checks, dict):
                errors.append(f"result {index} control {control_index} checks is not an object")
                checks = {}
            for check_name in CHECK_NAMES:
                if not isinstance(checks.get(check_name), bool):
                    errors.append(
                        f"result {index} control {control_index} check {check_name} is not boolean"
                    )
            if control.get("verdict") == "FAIL" or any(
                checks.get(check_name) is False for check_name in CHECK_NAMES
            ):
                verdict = "FAIL"
                reason = f"{control.get('name', 'control')}: {control.get('reason', 'failed check')}"

        if result.get("error"):
            verdict = "FAIL"
            reason = str(result["error"])
        rows.append((image or f"result-{index}", len(controls), verdict or "FAIL", reason))

    if not expected_images:
        errors.append("No PNG files were found in visual-out")
    if sorted(seen_images) != expected_images:
        errors.append(
            "results images do not exactly match visual-out PNGs: "
            f"expected={expected_images}, actual={sorted(seen_images)}"
        )
    if len(seen_images) != len(set(seen_images)):
        errors.append("results contain duplicate image entries")

    must = document.get("must")
    if not isinstance(must, list):
        errors.append("must must be a list")
        must = []
    for index, finding in enumerate(must):
        if not isinstance(finding, dict):
            errors.append(f"MUST finding {index} is not an object")
            continue
        for field in ("image", "control", "reason"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                errors.append(f"MUST finding {index} has an invalid {field}")
    if must:
        errors.append(f"audit contains {len(must)} MUST finding(s)")
    if document.get("verdict") != "PASS":
        errors.append(f"overall verdict is {document.get('verdict', 'missing')}, not PASS")
    if any(row[2] == "FAIL" for row in rows):
        errors.append("one or more image verdicts are FAIL")

    lines = [
        "## Claude visual audit",
        "",
        "| Image | Controls | Verdict | Result |",
        "| --- | ---: | :---: | --- |",
    ]
    for image, count, verdict, reason in rows:
        lines.append(
            f"| {markdown_cell(image)} | {count} | {verdict} | {markdown_cell(reason)} |"
        )
    if errors:
        lines.extend(("", "Validation errors:", ""))
        lines.extend(f"- {markdown_cell(error)}" for error in errors)
    lines.append("")
    summary = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
