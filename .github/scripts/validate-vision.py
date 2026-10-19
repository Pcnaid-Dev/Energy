#!/usr/bin/env python3
"""Validate a vision audit result and enforce FAIL/MUST findings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


WORKERS_AI_MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"
CHECK_NAMES = (
    "legible",
    "centred",
    "no_overflow",
    "no_raw_markdown",
)
LEGACY_CHECK_NAMES = (
    "legible",
    "centred",
    "matches_sibling",
    "no_overflow",
    "no_raw_markdown",
)
PAIR_CHECK_NAMES = (
    "text_colour",
    "font_family",
    "font_weight",
    "alignment",
)


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def load_declared_pairs(
    config_path: Path, errors: list[str]
) -> dict[str, dict[str, Any]]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        metadata: dict[str, dict[str, Any]] = {}
        for route in config["routes"]:
            for viewport, states in route["states"].items():
                for state in states:
                    filename = f"{route['id']}-{viewport}-{state['name']}.png"
                    metadata[filename] = {
                        "route": route["path"],
                        "viewport": viewport,
                        "state": state["name"],
                        "pairs": [pair["name"] for pair in state["pairs"]],
                    }
        return metadata
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        errors.append(f"Cannot read valid {config_path}: {error}")
        return {}


def validate_controls(
    answer: dict[str, Any],
    context: str,
    errors: list[str],
    check_names: tuple[str, ...],
) -> tuple[set[str], set[str], bool] | None:
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
        name_is_valid = isinstance(name, str) and bool(name.strip())
        if not name_is_valid:
            errors.append(f"{control_context} has no name")
        if not isinstance(control.get("reason"), str) or not control["reason"].strip():
            errors.append(f"{control_context} has no reason")

        checks = control.get("checks")
        if not isinstance(checks, dict):
            errors.append(f"{control_context} checks is not an object")
            checks = {}
        check_failed = False
        checks_valid = True
        for check_name in check_names:
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
        if checks_valid and name_is_valid:
            key = name.strip().casefold()
            if check_failed:
                failed_names.add(key)
            else:
                passed_names.add(key)
        has_failure = has_failure or check_failed

    return failed_names, passed_names, has_failure


def validate_pairs(
    answer: dict[str, Any],
    declared_names: list[str],
    context: str,
    warnings: list[str],
) -> tuple[set[str], set[str], bool] | None:
    expected_pairs: dict[str, str] = {}
    for index, declared_name in enumerate(declared_names):
        if not isinstance(declared_name, str) or not declared_name.strip():
            warnings.append(f"{context} declared pair {index} has no name")
            continue
        key = declared_name.strip().casefold()
        if key in expected_pairs:
            warnings.append(f"{context} declared pair {declared_name!r} is duplicated")
        expected_pairs[key] = declared_name.strip()

    pairs = answer.get("pairs")
    if not isinstance(pairs, list):
        warnings.append(f"{context} pairs must be a list")
        pairs = []

    failed_names: set[str] = set()
    passed_names: set[str] = set()
    seen_names: set[str] = set()
    for pair_index, pair in enumerate(pairs):
        pair_context = f"{context} pair {pair_index}"
        if not isinstance(pair, dict):
            warnings.append(f"{pair_context} is not an object")
            continue

        pair_name = pair.get("name")
        name_is_valid = isinstance(pair_name, str) and bool(pair_name.strip())
        pair_key = pair_name.strip().casefold() if name_is_valid else ""
        name_is_declared = name_is_valid and pair_key in expected_pairs
        if not name_is_valid:
            warnings.append(f"{pair_context} has no name")
        elif not name_is_declared:
            warnings.append(
                f"{pair_context} name {pair_name!r} is not declared for this state"
            )
        elif pair_key in seen_names:
            warnings.append(f"{pair_context} name {pair_name!r} is duplicated")
        if name_is_declared:
            seen_names.add(pair_key)

        if not isinstance(pair.get("reason"), str) or not pair["reason"].strip():
            warnings.append(f"{pair_context} has no reason")
        both_visible = pair.get("both_visible")
        both_visible_is_valid = isinstance(both_visible, bool)
        if not both_visible_is_valid:
            warnings.append(f"{pair_context} both_visible is not boolean")

        checks = pair.get("checks")
        if not isinstance(checks, dict):
            warnings.append(f"{pair_context} checks is not an object")
            checks = {}
        check_failed = False
        checks_valid = True
        all_checks_null = True
        for check_name in PAIR_CHECK_NAMES:
            value = checks.get(check_name)
            if not isinstance(value, bool):
                warnings.append(f"{pair_context} check {check_name} is not boolean")
                checks_valid = False
            else:
                all_checks_null = False
                if value is False:
                    check_failed = True

        pair_verdict = pair.get("verdict")
        pair_failed = False
        if pair_verdict == "NOT_ASSESSED" and (
            both_visible is False or all_checks_null
        ):
            pass
        else:
            pair_failed = both_visible is True and check_failed
            expected_verdict = (
                "NOT_ASSESSED"
                if both_visible is False
                else "FAIL" if pair_failed else "PASS"
            )
            if pair_verdict != expected_verdict:
                warnings.append(
                    f"{pair_context} verdict is not derived from both_visible and its boolean checks"
                )
        if (
            name_is_declared
            and pair_key not in failed_names
            and pair_key not in passed_names
            and both_visible_is_valid
            and checks_valid
        ):
            if pair_failed:
                failed_names.add(pair_key)
            elif both_visible:
                passed_names.add(pair_key)

    missing_names = [
        expected_pairs[key] for key in expected_pairs.keys() - seen_names
    ]
    if missing_names:
        warnings.append(
            f"{context} missing declared pair entries: "
            + ", ".join(sorted(missing_names))
        )

    return failed_names, passed_names, False


def validate_normalized_answer(
    answer: Any,
    declared_pair_names: list[str],
    context: str,
    errors: list[str],
    warnings: list[str],
    *,
    error_is_failure: bool = False,
) -> tuple[set[str], set[str], set[str], set[str]] | None:
    if not isinstance(answer, dict):
        errors.append(f"{context} is not an object")
        return None

    control_result = validate_controls(answer, context, errors, CHECK_NAMES)
    pair_result = validate_pairs(answer, declared_pair_names, context, warnings)
    if control_result is None or pair_result is None:
        return None

    failed_controls, passed_controls, control_failed = control_result
    failed_pairs, passed_pairs, _ = pair_result
    has_error = bool(answer.get("error")) if error_is_failure else False
    expected_verdict = "FAIL" if control_failed or has_error else "PASS"
    if answer.get("verdict") != expected_verdict:
        errors.append(
            f"{context} verdict is not derived from its controls"
        )
    return failed_controls, passed_controls, failed_pairs, passed_pairs


def pair_summary(pairs: Any) -> str:
    if not isinstance(pairs, list):
        pairs = []
    passed = 0
    failed = 0
    not_assessed = 0
    for pair in pairs:
        verdict = pair.get("verdict") if isinstance(pair, dict) else None
        if verdict == "PASS":
            passed += 1
        elif verdict == "FAIL":
            failed += 1
        else:
            not_assessed += 1
    return f"{passed} pass / {failed} fail / {not_assessed} not assessed"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate-vision.py <result.json>", file=sys.stderr)
        return 2

    result_path = Path(sys.argv[1])
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[tuple[str, int, str, str, str]] = []

    try:
        document = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        document = {}
        errors.append(f"Cannot read valid {result_path}: {error}")
    if not isinstance(document, dict):
        errors.append(f"{result_path} root must be an object")
        document = {}

    expected_images = sorted(path.name for path in Path("visual-out").rglob("*.png"))
    model = document.get("model")
    if not isinstance(model, str) or not model.strip():
        errors.append("model must be a non-empty string")
    workers_schema = model == WORKERS_AI_MODEL
    declared_pairs_by_image = (
        load_declared_pairs(Path("ui-states.json"), errors)
        if workers_schema
        else {}
    )
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
        pairs = result.get("pairs")
        verdict = result.get("verdict")
        row_verdict = verdict if verdict in ("PASS", "FAIL") else "FAIL"
        reason = (
            "All reported controls passed; pair observations are advisory."
            if workers_schema
            else "All reported controls passed."
        )

        route = result.get("route")
        viewport = result.get("viewport")
        state = result.get("state")
        if not isinstance(route, str) or not route.startswith("/"):
            errors.append(f"result {index} has an invalid route")
        if viewport not in ("mobile", "desktop"):
            errors.append(f"result {index} has an invalid viewport")
        if not isinstance(state, str) or not state.strip():
            errors.append(f"result {index} has an invalid state")
        if verdict not in ("PASS", "FAIL"):
            errors.append(f"result {index} has an invalid verdict")

        if workers_schema:
            configured_state = declared_pairs_by_image.get(image)
            if configured_state is None:
                errors.append(
                    f"result {index} image has no matching ui-states.json state"
                )
                declared_pair_names = []
            else:
                declared_pair_names = configured_state["pairs"]
                for field, actual in (
                    ("route", route),
                    ("viewport", viewport),
                    ("state", state),
                ):
                    if actual != configured_state[field]:
                        errors.append(
                            f"result {index} {field} does not match ui-states.json"
                        )

            validate_normalized_answer(
                result,
                declared_pair_names,
                f"result {index}",
                errors,
                warnings,
                error_is_failure=True,
            )
            rechecks = result.get("rechecks")
            if rechecks is not None:
                if not isinstance(rechecks, list) or len(rechecks) != 2:
                    errors.append(
                        f"result {index} rechecks must contain exactly two answers"
                    )
                else:
                    first_names = validate_normalized_answer(
                        rechecks[0],
                        declared_pair_names,
                        f"result {index} recheck 0",
                        errors,
                        warnings,
                    )
                    second_names = validate_normalized_answer(
                        rechecks[1],
                        declared_pair_names,
                        f"result {index} recheck 1",
                        errors,
                        warnings,
                    )
                    if first_names is not None and second_names is not None:
                        (
                            first_failed_controls,
                            _,
                            _,
                            _,
                        ) = first_names
                        (
                            _,
                            second_passed_controls,
                            _,
                            _,
                        ) = second_names
                        if not first_failed_controls:
                            errors.append(
                                f"result {index} recheck 0 has no failed control"
                            )
                        unresolved_controls = (
                            first_failed_controls - second_passed_controls
                        )
                        expected_verdict = "FAIL" if unresolved_controls else "PASS"
                        if not result.get("error") and verdict != expected_verdict:
                            errors.append(
                                f"result {index} verdict does not match its recheck consensus"
                            )

            if isinstance(controls, list):
                failed_controls = [
                    control
                    for control in controls
                    if isinstance(control, dict)
                    and control.get("verdict") == "FAIL"
                ]
                if failed_controls:
                    row_verdict = "FAIL"
                    reason = (
                        f"{failed_controls[0].get('name', 'control')}: "
                        f"{failed_controls[0].get('reason', 'failed check')}"
                    )
        else:
            if not isinstance(pairs, list):
                errors.append(f"result {index} pairs must be a list")
            if not isinstance(controls, list) or not controls:
                errors.append(f"result {index} controls must be a non-empty list")
                controls = []

            for control_index, control in enumerate(controls):
                control_context = f"result {index} control {control_index}"
                if not isinstance(control, dict):
                    errors.append(f"{control_context} is not an object")
                    continue
                if not isinstance(control.get("name"), str) or not control["name"].strip():
                    errors.append(f"{control_context} has no name")
                if not isinstance(control.get("reason"), str) or not control["reason"].strip():
                    errors.append(f"{control_context} has no reason")
                if control.get("verdict") not in ("PASS", "FAIL"):
                    errors.append(f"{control_context} has an invalid verdict")
                checks = control.get("checks")
                if not isinstance(checks, dict):
                    errors.append(f"{control_context} checks is not an object")
                    checks = {}
                for check_name in LEGACY_CHECK_NAMES:
                    if not isinstance(checks.get(check_name), bool):
                        errors.append(
                            f"{control_context} check {check_name} is not boolean"
                        )
                if control.get("verdict") == "FAIL" or any(
                    checks.get(check_name) is False
                    for check_name in LEGACY_CHECK_NAMES
                ):
                    row_verdict = "FAIL"
                    reason = (
                        f"{control.get('name', 'control')}: "
                        f"{control.get('reason', 'failed check')}"
                    )

        if result.get("error"):
            row_verdict = "FAIL"
            reason = str(result["error"])
        control_count = len(controls) if isinstance(controls, list) else 0
        rows.append(
            (
                image or f"result-{index}",
                control_count,
                pair_summary(pairs),
                row_verdict,
                reason,
            )
        )

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
    if any(row[3] == "FAIL" for row in rows):
        errors.append("one or more image verdicts are FAIL")

    lines = [
        "## Workers AI visual audit" if workers_schema else "## Claude visual audit",
        "",
        (
            "| Image | Controls | Pairs (advisory) | Verdict | Result |"
            if workers_schema
            else "| Image | Controls | Pairs | Verdict | Result |"
        ),
        "| --- | ---: | --- | :---: | --- |",
    ]
    for image, count, pairs_text, row_verdict, reason in rows:
        lines.append(
            f"| {markdown_cell(image)} | {count} | {pairs_text} | {row_verdict} | {markdown_cell(reason)} |"
        )
    if errors:
        lines.extend(("", "Validation errors:", ""))
        lines.extend(f"- {markdown_cell(error)}" for error in errors)
    if warnings:
        lines.extend(("", "Pair advisory warnings:", ""))
        lines.extend(f"- {markdown_cell(warning)}" for warning in warnings)
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
