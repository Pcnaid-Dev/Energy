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


def make_raw(control: dict[str, object], model_verdict: str = "PASS") -> dict[str, object]:
    return {"controls": [control], "verdict": model_verdict}


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
        empty = {"controls": [], "verdict": "PASS"}
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

    def test_recheck_passes_when_failure_is_not_repeated(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(failed_check="matches_sibling", reason="first reason"))
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
            make_raw(make_control(name="Drawer CTA", failed_check="matches_sibling"))
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
                        name="Drawer CTA", failed_check="matches_sibling"
                    ),
                    make_control(name="Header CTA"),
                ],
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
            make_raw(make_control(name="Drawer CTA", failed_check="matches_sibling"))
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
            make_raw(make_control(name="Drawer CTA", failed_check="matches_sibling"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="drawer cta", reason="cleared on recheck"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["controls"], second["controls"])
        self.assertEqual(result["rechecks"], [first, second])

    def test_recheck_pass_does_not_invent_a_control(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Header CTA", failed_check="matches_sibling"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="centred"))
        )

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["controls"], first["controls"])
        self.assertEqual(result["rechecks"], [first, second])

    def test_empty_effective_controls_fail_with_explicit_reason(self) -> None:
        first = {"controls": [], "verdict": "FAIL"}
        second = {"controls": [], "verdict": "PASS"}

        result = self.audit_with(first, second)

        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("no effective controls", result["error"])


class WorkersAIValidatorTests(unittest.TestCase):
    def run_validator(
        self,
        first: dict[str, object],
        second: dict[str, object],
        controls: list[dict[str, object]],
    ) -> tuple[int, str]:
        document = {
            "model": vision_workersai.MODEL,
            "results": [
                {
                    "image": "state.png",
                    "route": "/",
                    "viewport": "desktop",
                    "state": "default",
                    "pairs": [],
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
            (visual_out / "state.png").write_bytes(b"png")
            result_path = directory_path / "vision-workersai.json"
            result_path.write_text(json.dumps(document), encoding="utf-8")
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
            make_raw(make_control(name="Header CTA", failed_check="matches_sibling"))
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
                        name="Drawer CTA", failed_check="matches_sibling"
                    ),
                    make_control(name="Header CTA"),
                ],
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
            make_raw(make_control(name="Drawer CTA", failed_check="matches_sibling"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="Open rates"))
        )

        exit_code, output = self.run_validator(first, second, second["controls"])

        self.assertEqual(exit_code, 1)
        self.assertIn("verdict does not match its recheck consensus", output)

    def test_validator_accepts_same_name_pass_case_insensitively(self) -> None:
        first = vision_workersai.normalize_audit(
            make_raw(make_control(name="Drawer CTA", failed_check="matches_sibling"))
        )
        second = vision_workersai.normalize_audit(
            make_raw(make_control(name="drawer cta"))
        )

        exit_code, output = self.run_validator(first, second, second["controls"])

        self.assertEqual(exit_code, 0, output)


if __name__ == "__main__":
    unittest.main()
