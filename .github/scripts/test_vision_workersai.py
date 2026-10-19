#!/usr/bin/env python3
"""Unit tests for the Cloudflare Workers AI vision reviewer."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("vision-workersai.py")
SPEC = importlib.util.spec_from_file_location("vision_workersai", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT_PATH}")
vision_workersai = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vision_workersai)

VALIDATOR_PATH = Path(__file__).with_name("validate-vision.py")
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_vision", VALIDATOR_PATH
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise RuntimeError(f"cannot load {VALIDATOR_PATH}")
validate_vision = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validate_vision)


DECLARED_PAIR = {
    "name": "drawer CTA vs header CTA",
    "controls": [
        {"name": "drawer CTA", "selector": ".mobile-menu .btn-primary"},
        {"name": "header CTA", "selector": ".nav-cta .btn-primary"},
    ],
}


def make_control(
    name: str = "Primary CTA",
    *,
    failed_check: str | None = None,
    model_verdict: str = "PASS",
    reason: str = "model reason",
) -> dict[str, object]:
    checks = {check_name: True for check_name in vision_workersai.CHECK_NAMES}
    if failed_check is not None:
        checks[failed_check] = False
    return {
        "name": name,
        "checks": checks,
        "verdict": model_verdict,
        "reason": reason,
    }


def make_pair(
    name: str = "drawer CTA vs header CTA",
    *,
    both_visible: bool = True,
    failed_check: str | None = None,
    reason: str = "pair reason",
) -> dict[str, object]:
    checks = {check_name: True for check_name in vision_workersai.PAIR_CHECK_NAMES}
    if failed_check is not None:
        checks[failed_check] = False
    return {
        "name": name,
        "both_visible": both_visible,
        "checks": checks,
        "reason": reason,
    }


def make_raw(
    control: dict[str, object],
    model_verdict: str = "PASS",
    *,
    pairs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "controls": [control],
        "pairs": [] if pairs is None else pairs,
        "verdict": model_verdict,
    }


class FakeResponse:
    def __init__(self, document: dict[str, object]) -> None:
        self.body = json.dumps(document).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class WorkersAIResponseTests(unittest.TestCase):
    def request_with_response(self, response: object) -> dict[str, object]:
        envelope = {"success": True, "result": {"response": response}}
        metadata = {"route": "/", "viewport": "desktop", "state": "default", "pairs": []}
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory, "state.png")
            image_path.write_bytes(b"png")
            with mock.patch.object(
                vision_workersai, "urlopen", return_value=FakeResponse(envelope)
            ):
                return vision_workersai.request_audit(
                    "account", "token", image_path, metadata
                )

    def test_dict_response_is_used_directly(self) -> None:
        raw = make_raw(make_control())
        self.assertEqual(self.request_with_response(raw), raw)

    def test_string_wrapped_json_extracts_first_object(self) -> None:
        raw = make_raw(make_control())
        wrapped = f"```json\n{json.dumps(raw)}\n```"
        self.assertEqual(self.request_with_response(wrapped), raw)

    def test_unsupported_response_reports_type_and_keys_without_values(self) -> None:
        result = {"response": 7, "secret_token": "do-not-print"}
        with self.assertRaises(ValueError) as caught:
            vision_workersai.parse_result_response(result)
        message = str(caught.exception)
        self.assertIn("type=int", message)
        self.assertIn("secret_token", message)
        self.assertNotIn("do-not-print", message)

    def test_prompt_names_only_declared_pairs_and_separates_pair_checks(self) -> None:
        metadata = {
            "route": "/",
            "viewport": "desktop",
            "state": "default",
            "pairs": [DECLARED_PAIR],
        }

        prompt = vision_workersai.build_prompt("state.png", metadata)

        self.assertIn("drawer CTA vs header CTA", prompt)
        self.assertIn('"controls":["drawer CTA","header CTA"]', prompt)
        self.assertIn("both_visible", prompt)
        self.assertIn("Pair observations are advisory", prompt)
        self.assertIn("advisory pair checks never change it", prompt)
        self.assertNotIn("matches_sibling", prompt)
        self.assertNotIn(".mobile-menu .btn-primary", prompt)


class WorkersAINormalizationTests(unittest.TestCase):
    def test_empty_response_retries_twice_then_fail(self) -> None:
        envelope = {"success": True, "result": {"response": ""}}
        metadata = {"route": "/", "viewport": "desktop", "state": "default", "pairs": []}
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory, "state.png")
            image_path.write_bytes(b"png")
            with mock.patch.object(
                vision_workersai, "urlopen", return_value=FakeResponse(envelope)
            ) as request:
                with self.assertRaisesRegex(
                    vision_workersai.EmptyAuditResponse,
                    "empty audit persisted after 3 attempts",
                ):
                    vision_workersai.request_normalized_audit(
                        "account", "token", image_path, metadata
                    )
        self.assertEqual(request.call_count, 3)

    def test_empty_controls_retry_twice_then_fail(self) -> None:
        empty = {"controls": [], "pairs": [], "verdict": "PASS"}
        with mock.patch.object(
            vision_workersai, "request_audit", return_value=empty
        ) as request:
            with self.assertRaisesRegex(
                vision_workersai.EmptyAuditResponse,
                "empty audit persisted after 3 attempts",
            ):
                vision_workersai.request_normalized_audit(
                    "account", "token", Path("state.png"), {}
                )
        self.assertEqual(request.call_count, 3)

    def test_verdicts_are_derived_only_from_checks(self) -> None:
        all_true = vision_workersai.normalize_audit(
            make_raw(make_control(model_verdict="FAIL"), model_verdict="FAIL")
        )
        one_false = vision_workersai.normalize_audit(
            make_raw(make_control(failed_check="centred", model_verdict="PASS"))
        )

        self.assertEqual(all_true["controls"][0]["verdict"], "PASS")
        self.assertEqual(all_true["verdict"], "PASS")
        self.assertEqual(one_false["controls"][0]["verdict"], "FAIL")
        self.assertEqual(one_false["verdict"], "FAIL")
        self.assertEqual(one_false["controls"][0]["reason"], "model reason")

    def test_pair_not_visible_is_not_a_failure(self) -> None:
        normalized = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[
                    make_pair(
                        both_visible=False,
                        failed_check="font_family",
                    )
                ],
            ),
            [DECLARED_PAIR],
        )

        self.assertEqual(normalized["pairs"][0]["verdict"], "NOT_ASSESSED")
        self.assertEqual(normalized["verdict"], "PASS")

    def test_visible_pair_font_family_failure_is_advisory(self) -> None:
        normalized = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )

        self.assertEqual(normalized["pairs"][0]["verdict"], "FAIL")
        self.assertEqual(normalized["verdict"], "PASS")

    def test_summary_labels_pair_failures_advisory(self) -> None:
        normalized = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), \
                contextlib.redirect_stdout(output):
            vision_workersai.write_summary(
                [{"image": "state.png", **normalized}], "PASS"
            )

        self.assertIn("Pairs (advisory)", output.getvalue())
        self.assertIn("0 pass / 1 fail / 0 not assessed", output.getvalue())
        self.assertIn("pair observations are advisory", output.getvalue())

    def test_missing_declared_pair_is_not_assessed_without_retry(self) -> None:
        metadata = {
            "state.png": {
                "route": "/",
                "viewport": "desktop",
                "state": "default",
                "pairs": [DECLARED_PAIR],
            }
        }
        with mock.patch.object(
            vision_workersai,
            "request_audit",
            return_value=make_raw(make_control()),
        ) as request:
            result = vision_workersai.audit_image(
                "account", "token", Path("state.png"), metadata
            )

        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0]["name"], DECLARED_PAIR["name"])
        self.assertEqual(result["pairs"][0]["verdict"], "NOT_ASSESSED")
        self.assertEqual(result["pairs"][0]["reason"], "advisory-missing")
        self.assertIn("advisory-missing", result["pair_notes"][0])

    def test_hidden_pair_with_null_checks_normalizes_to_not_assessed(self) -> None:
        hidden_pair = {
            "name": DECLARED_PAIR["name"],
            "both_visible": False,
            "checks": {
                check_name: None for check_name in vision_workersai.PAIR_CHECK_NAMES
            },
            "reason": "drawer is closed",
        }
        metadata = {
            "route": "/",
            "viewport": "desktop",
            "state": "default",
            "pairs": [DECLARED_PAIR],
        }
        with mock.patch.object(
            vision_workersai,
            "request_audit",
            return_value=make_raw(make_control(), pairs=[hidden_pair]),
        ) as request:
            result = vision_workersai.audit_image(
                "account", "token", Path("state.png"), metadata
            )

        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0]["verdict"], "NOT_ASSESSED")
        self.assertEqual(result["pairs"][0]["both_visible"], False)
        self.assertIsNone(result["pairs"][0]["checks"]["text_colour"])

    def test_schema_invalid_answer_then_valid_answer_passes(self) -> None:
        invalid = make_raw(make_control(reason=""))
        valid = make_raw(make_control())
        metadata = {
            "route": "/",
            "viewport": "desktop",
            "state": "default",
            "pairs": [],
        }
        with mock.patch.object(
            vision_workersai,
            "request_audit",
            side_effect=[invalid, valid],
        ) as request:
            normalized = vision_workersai.request_normalized_audit(
                "account", "token", Path("state.png"), metadata
            )

        self.assertEqual(normalized["verdict"], "PASS")
        self.assertEqual(request.call_count, 2)


class WorkersAIRecheckTests(unittest.TestCase):
    metadata = {
        "state.png": {
            "route": "/",
            "viewport": "desktop",
            "state": "default",
            "pairs": [],
        }
    }

    def audit_with(self, first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
        with mock.patch.object(
            vision_workersai,
            "request_normalized_audit",
            side_effect=[first, second],
        ) as request:
            result = vision_workersai.audit_image(
                "account", "token", Path("state.png"), self.metadata
            )
        self.assertEqual(request.call_count, 2)
        return result

    def test_advisory_pair_failure_does_not_trigger_recheck(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        with mock.patch.object(
            vision_workersai,
            "request_normalized_audit",
            return_value=first,
        ) as request:
            metadata = {
                "state.png": {
                    "route": "/",
                    "viewport": "desktop",
                    "state": "default",
                    "pairs": [DECLARED_PAIR],
                }
            }
            result = vision_workersai.audit_image(
                "account", "token", Path("state.png"), metadata
            )

        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0]["verdict"], "FAIL")

    def test_recheck_passes_when_failure_is_not_repeated(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(failed_check="centred", reason="first reason"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(reason="second reason"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["controls"][0]["verdict"], "PASS")
        self.assertEqual(result["controls"][0]["reason"], "second reason")
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_fails_for_same_control_name_case_insensitively(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="drawer cta", failed_check="centred"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["controls"][0], first["controls"][0])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_fails_when_failed_control_is_omitted(self) -> None:
        first = vision_workersai.normalize_audit(
            {
                "controls": [
                    make_control(
                        name="Drawer CTA", failed_check="centred"
                    ),
                    make_control(name="Header CTA"),
                ],
                "pairs": [],
                "verdict": "FAIL",
            }
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["controls"][0], first["controls"][0])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_fails_when_failed_control_is_renamed(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Open rates"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["controls"][0], first["controls"][0])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_passes_same_control_name_case_insensitively(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="drawer cta", reason="cleared on recheck"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["controls"], second["controls"])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_clears_pair_failure_by_same_name_pass(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(failed_check="centred"),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(name="DRAWER CTA VS HEADER CTA")],
            ),
            [DECLARED_PAIR],
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0]["verdict"], "PASS")
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_keeps_renamed_pair_failure_advisory(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(failed_check="centred"),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(), pairs=[make_pair()]),
            [DECLARED_PAIR],
        )
        second["pairs"][0]["name"] = "different pair"

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0], first["pairs"][0])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_keeps_not_assessed_pair_failure_advisory(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(failed_check="centred"),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(both_visible=False)],
            ),
            [DECLARED_PAIR],
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["pairs"][0], first["pairs"][0])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_pass_does_not_invent_a_control(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["controls"], first["controls"])
        self.assertEqual(result["rechecks"], [first, second])

    def test_empty_effective_controls_fail_with_explicit_reason(self) -> None:
        first = {"controls": [], "pairs": [], "verdict": "FAIL"}
        second = {"controls": [], "pairs": [], "verdict": "PASS"}

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("no effective controls", result["error"])


class WorkersAIValidatorTests(unittest.TestCase):
    def run_validator(
        self,
        first: dict[str, object],
        second: dict[str, object],
        controls: list[dict[str, object]],
        *,
        pairs: list[dict[str, object]] | None = None,
        declared_pairs: list[dict[str, object]] | None = None,
    ) -> tuple[int, str]:
        if pairs is None:
            pairs = []
        if declared_pairs is None:
            declared_pairs = []
        document = {
            "model": vision_workersai.MODEL,
            "results": [
                {
                    "image": "state-desktop-default.png",
                    "route": "/",
                    "viewport": "desktop",
                    "state": "default",
                    "pairs": pairs,
                    "controls": controls,
                    "verdict": "PASS",
                    "rechecks": [first, second],
                }
            ],
            "must": [],
            "verdict": "PASS",
        }

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            visual_out = directory_path / "visual-out"
            visual_out.mkdir()
            (visual_out / "state-desktop-default.png").write_bytes(b"png")
            result_path = directory_path / "vision-workersai.json"
            result_path.write_text(json.dumps(document), encoding="utf-8")
            (directory_path / "ui-states.json").write_text(
                json.dumps(
                    {
                        "routes": [
                            {
                                "id": "state",
                                "path": "/",
                                "states": {
                                    "desktop": [
                                        {
                                            "name": "default",
                                            "actions": [],
                                            "pairs": declared_pairs,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            previous_directory = Path.cwd()
            try:
                os.chdir(directory_path)
                with mock.patch.object(
                    validate_vision.sys,
                    "argv",
                    ["validate-vision.py", str(result_path)],
                ), contextlib.redirect_stdout(output):
                    exit_code = validate_vision.main()
            finally:
                os.chdir(previous_directory)

        return exit_code, output.getvalue()

    def test_validator_rejects_empty_effective_controls(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA"))
        )

        exit_code, output = self.run_validator(first, second, [])

        self.assertEqual(exit_code, 1)
        self.assertIn("controls must be a non-empty list", output)

    def test_validator_rejects_omitted_failed_control(self) -> None:
        first = vision_workersai.normalize_audit(
            {
                "controls": [
                    make_control(
                        name="Drawer CTA", failed_check="centred"
                    ),
                    make_control(name="Header CTA"),
                ],
                "pairs": [],
                "verdict": "FAIL",
            }
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA"))
        )

        exit_code, output = self.run_validator(first, second, second["controls"])

        self.assertEqual(exit_code, 1)
        self.assertIn("verdict does not match its recheck consensus", output)

    def test_validator_rejects_renamed_failed_control(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Open rates"))
        )

        exit_code, output = self.run_validator(first, second, second["controls"])

        self.assertEqual(exit_code, 1)
        self.assertIn("verdict does not match its recheck consensus", output)

    def test_validator_accepts_same_name_pass_case_insensitively(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="drawer cta"))
        )

        exit_code, output = self.run_validator(first, second, second["controls"])

        self.assertEqual(exit_code, 0, output)

    def test_validator_accepts_same_name_pair_pass_case_insensitively(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(failed_check="centred"),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(name="DRAWER CTA VS HEADER CTA")],
            ),
            [DECLARED_PAIR],
        )
        effective = vision_workersai.adjudicate_recheck(first, second)

        exit_code, output = self.run_validator(
            first,
            second,
            effective["controls"],
            pairs=effective["pairs"],
            declared_pairs=[DECLARED_PAIR],
        )

        self.assertEqual(exit_code, 0, output)
        self.assertIn("Pairs (advisory)", output)
        self.assertIn("1 pass / 0 fail / 0 not assessed", output)

    def test_validator_accepts_advisory_pair_failure_when_not_assessed_on_recheck(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(
                make_control(failed_check="centred"),
                pairs=[make_pair(failed_check="font_family")],
            ),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(
                make_control(),
                pairs=[make_pair(both_visible=False)],
            ),
            [DECLARED_PAIR],
        )
        effective = vision_workersai.adjudicate_recheck(first, second)

        exit_code, output = self.run_validator(
            first,
            second,
            effective["controls"],
            pairs=effective["pairs"],
            declared_pairs=[DECLARED_PAIR],
        )

        self.assertEqual(exit_code, 0, output)
        self.assertIn("0 pass / 1 fail / 0 not assessed", output)

    def test_validator_accepts_not_assessed_pair_with_null_checks(self) -> None:
        controls = vision_workersai.normalize_audit(make_raw(make_control()))["controls"]
        hidden_pair = {
            "name": DECLARED_PAIR["name"],
            "both_visible": False,
            "checks": {"text_colour": None, "font_family": None,
                       "font_weight": None, "alignment": None},
            "verdict": "NOT_ASSESSED",
            "reason": "advisory-malformed: check text_colour is not boolean",
        }
        first = vision_workersai.normalize_audit(
            make_raw(make_control(failed_check="centred"), pairs=[make_pair()]),
            [DECLARED_PAIR],
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(), pairs=[hidden_pair]),
            [DECLARED_PAIR],
        )
        effective = vision_workersai.adjudicate_recheck(first, second)

        exit_code, output = self.run_validator(
            first,
            second,
            controls,
            pairs=effective["pairs"],
            declared_pairs=[DECLARED_PAIR],
        )

        self.assertEqual(exit_code, 0, output)


if __name__ == "__main__":
    unittest.main()
