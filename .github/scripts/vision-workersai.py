#!/usr/bin/env python3
"""Run one Cloudflare Workers AI vision audit for every visual-gate PNG."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"
MAX_TOKENS = 4096
MAX_WORKERS = 4
MAX_RETRIES = 3
MAX_EMPTY_RESPONSE_RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 300
CHECK_NAMES = (
    "legible",
    "centred",
    "no_overflow",
    "no_raw_markdown",
)
PAIR_CHECK_NAMES = (
    "text_colour",
    "font_family",
    "font_weight",
    "alignment",
)


class AuditResponseError(ValueError):
    """A model answer that can be retried because it is not usable."""


class EmptyAuditResponse(AuditResponseError):
    """A model response that can be retried because it contains no controls."""


class InvalidAuditResponse(AuditResponseError):
    """A model response that can be retried because its schema is invalid."""


def load_state_metadata(config_path: Path) -> dict[str, dict[str, Any]]:
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
                    "pairs": state["pairs"],
                }
    return metadata


def build_prompt(image_name: str, metadata: dict[str, Any]) -> str:
    declared_pairs = [
        {
            "name": pair["name"],
            "controls": [control["name"] for control in pair["controls"]],
        }
        for pair in metadata["pairs"]
    ]
    return "\n".join(
        (
            "Audit this UI screenshot control by control.",
            f"Image: {image_name}",
            f"Route: {metadata['route']}",
            f"Viewport: {metadata['viewport']}",
            f"State: {metadata['state']}",
            "Declared sibling-control pairs from ui-states.json (pair name and control names):",
            json.dumps(declared_pairs, separators=(",", ":")),
            "For every visible button, link, CTA, and input, report whether:",
            "1. legible: its text has adequate contrast by eye;",
            "2. centred: its label plus any accompanying icon are visually centred as one group; an arrow or icon beside text is not by itself off-centre;",
            "3. no_overflow: no text is overflowing or clipped;",
            "4. no_raw_markdown: it shows no raw Markdown or code fences.",
            "Also return one advisory pairs entry for every declared pair above and no other pairs.",
            "For each declared pair, compare only text_colour, font_family (serif vs sans), font_weight, and alignment.",
            "Size and width differences are expected and must not affect those four pair checks.",
            "Pair observations are advisory and must not affect any control verdict or the image verdict.",
            "If either named control is not visible, set both_visible false, but all four checks must still be booleans.",
            "If there are no declared pairs, return an empty pairs array.",
            "Return only one JSON object with exactly this shape:",
            '{"controls":[{"name":"visible control name","checks":{"legible":true,"centred":true,"no_overflow":true,"no_raw_markdown":true},"verdict":"PASS","reason":"concise visual evidence"}],"pairs":[{"name":"declared pair name","both_visible":true,"checks":{"text_colour":true,"font_family":true,"font_weight":true,"alignment":true},"reason":"concise visual evidence"}],"verdict":"PASS"}',
            "Use only PASS or FAIL for control and image verdicts. The image verdict must be FAIL when any control check fails; advisory pair checks never change it. Do not wrap JSON in Markdown or code fences.",
        )
    )


def extract_first_json_object(content: str) -> dict[str, Any]:
    text = re.sub(r"^\s*```(?:json)?\s*", "", content, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text, count=1)
    decoder = json.JSONDecoder()
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise InvalidAuditResponse(
        "model result.response does not contain a JSON object"
    )


def parse_result_response(result: dict[str, Any]) -> dict[str, Any]:
    content = result.get("response")
    result_keys = sorted(str(key) for key in result)
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        if content.strip():
            return extract_first_json_object(content)
        raise EmptyAuditResponse(
            "Workers AI result.response is empty "
            f"(type=str; result top-level keys={result_keys})"
        )

    message = (
        "Workers AI result.response has unsupported "
        f"type={type(content).__name__}; result top-level keys={result_keys}"
    )
    if content is None:
        raise EmptyAuditResponse(message)
    raise InvalidAuditResponse(message)


def retry_delay(retry: int, retry_after: str | None = None) -> float:
    try:
        delay = float(retry_after) if retry_after else 2**retry
    except ValueError:
        delay = 2**retry
    return min(max(delay, 1), 30)


def request_audit(
    account_id: str,
    api_token: str,
    image_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/ai/run/{MODEL}"
    )
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(image_path.name, metadata)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        "max_tokens": MAX_TOKENS,
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )

    for retry in range(MAX_RETRIES + 1):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw_body = response.read()
            try:
                response_body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                preview = raw_body[:500].decode("utf-8", errors="replace")
                raise ValueError(f"Workers AI response is not JSON: {preview}") from error
            if not isinstance(response_body, dict):
                raise ValueError("Workers AI response is not an object")
            if response_body.get("success") is not True:
                raise ValueError(
                    "Workers AI response reported failure: "
                    f"{json.dumps(response_body.get('errors', []), separators=(',', ':'))}"
                )
            result = response_body.get("result")
            if not isinstance(result, dict):
                raise ValueError(
                    "Workers AI response result has "
                    f"type={type(result).__name__}; top-level keys unavailable"
                )
            return parse_result_response(result)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            retryable = error.code == 429 or 500 <= error.code < 600
            if retryable and retry < MAX_RETRIES:
                time.sleep(retry_delay(retry, error.headers.get("Retry-After")))
                continue
            raise RuntimeError(f"Workers AI HTTP {error.code}: {detail}") from error
        except (TimeoutError, socket.timeout) as error:
            if retry < MAX_RETRIES:
                time.sleep(retry_delay(retry))
                continue
            raise RuntimeError(
                f"Workers AI request timed out after {MAX_RETRIES + 1} attempts: {error}"
            ) from error
        except URLError as error:
            timed_out = isinstance(error.reason, (TimeoutError, socket.timeout)) or (
                "timed out" in str(error.reason).lower()
            )
            if timed_out and retry < MAX_RETRIES:
                time.sleep(retry_delay(retry))
                continue
            if timed_out:
                raise RuntimeError(
                    "Workers AI request timed out after "
                    f"{MAX_RETRIES + 1} attempts: {error.reason}"
                ) from error
            raise RuntimeError(f"Workers AI network error: {error.reason}") from error

    raise RuntimeError("Workers AI request failed after retries")


def normalize_audit(
    raw: Any, declared_pairs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if declared_pairs is None:
        declared_pairs = []
    if not isinstance(raw, dict):
        raise InvalidAuditResponse("response is not an object")
    controls = raw.get("controls")
    if not isinstance(controls, list) or not controls:
        raise EmptyAuditResponse("controls must be a non-empty list")

    normalized_controls: list[dict[str, Any]] = []
    has_failure = False
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise InvalidAuditResponse(f"control {index} is not an object")
        name = control.get("name")
        reason = control.get("reason")
        checks = control.get("checks")
        if not isinstance(name, str) or not name.strip():
            raise InvalidAuditResponse(f"control {index} has no name")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidAuditResponse(f"control {index} has no reason")
        if not isinstance(checks, dict):
            raise InvalidAuditResponse(f"control {index} checks is not an object")

        normalized_checks = {}
        for check_name in CHECK_NAMES:
            value = checks.get(check_name)
            if not isinstance(value, bool):
                raise InvalidAuditResponse(
                    f"control {index} check {check_name} is not boolean"
                )
            normalized_checks[check_name] = value

        control_failed = not all(normalized_checks.values())
        has_failure = has_failure or control_failed
        normalized_controls.append(
            {
                "name": name,
                "checks": normalized_checks,
                "verdict": "FAIL" if control_failed else "PASS",
                "reason": reason,
            }
        )

    expected_pairs: dict[str, str] = {}
    for index, pair in enumerate(declared_pairs):
        if not isinstance(pair, dict):
            raise InvalidAuditResponse(f"declared pair {index} is not an object")
        name = pair.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidAuditResponse(f"declared pair {index} has no name")
        name_key = name.strip().casefold()
        if name_key in expected_pairs:
            raise InvalidAuditResponse(f"declared pair {name!r} is duplicated")
        expected_pairs[name_key] = name.strip()

    pairs = raw.get("pairs")
    if not isinstance(pairs, list):
        raise InvalidAuditResponse("pairs must be a list")

    normalized_pairs: list[dict[str, Any]] = []
    seen_pairs: set[str] = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise InvalidAuditResponse(f"pair {index} is not an object")
        name = pair.get("name")
        reason = pair.get("reason")
        both_visible = pair.get("both_visible")
        checks = pair.get("checks")
        if not isinstance(name, str) or not name.strip():
            raise InvalidAuditResponse(f"pair {index} has no name")
        name_key = name.strip().casefold()
        if name_key not in expected_pairs:
            raise InvalidAuditResponse(f"pair {name!r} is not declared for this state")
        if name_key in seen_pairs:
            raise InvalidAuditResponse(f"pair {name!r} is duplicated")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidAuditResponse(f"pair {index} has no reason")
        if not isinstance(both_visible, bool):
            raise InvalidAuditResponse(f"pair {index} both_visible is not boolean")
        if not isinstance(checks, dict):
            raise InvalidAuditResponse(f"pair {index} checks is not an object")

        normalized_checks = {}
        for check_name in PAIR_CHECK_NAMES:
            value = checks.get(check_name)
            if not isinstance(value, bool):
                raise InvalidAuditResponse(
                    f"pair {index} check {check_name} is not boolean"
                )
            normalized_checks[check_name] = value

        pair_failed = both_visible and not all(normalized_checks.values())
        pair_verdict = (
            "NOT_ASSESSED"
            if not both_visible
            else "FAIL" if pair_failed else "PASS"
        )
        normalized_pairs.append(
            {
                "name": expected_pairs[name_key],
                "both_visible": both_visible,
                "checks": normalized_checks,
                "verdict": pair_verdict,
                "reason": reason,
            }
        )
        seen_pairs.add(name_key)

    missing_pairs = [
        expected_pairs[name_key]
        for name_key in expected_pairs.keys() - seen_pairs
    ]
    if missing_pairs:
        raise InvalidAuditResponse(
            "missing declared pair entries: " + ", ".join(sorted(missing_pairs))
        )

    return {
        "controls": normalized_controls,
        "pairs": normalized_pairs,
        "verdict": "FAIL" if has_failure else "PASS",
    }


def request_normalized_audit(
    account_id: str,
    api_token: str,
    image_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    attempts = MAX_EMPTY_RESPONSE_RETRIES + 1
    for attempt in range(attempts):
        try:
            return normalize_audit(
                request_audit(account_id, api_token, image_path, metadata),
                metadata.get("pairs", []),
            )
        except AuditResponseError as error:
            if attempt == attempts - 1:
                if isinstance(error, EmptyAuditResponse):
                    raise EmptyAuditResponse(
                        f"{error}; empty audit persisted after {attempts} attempts"
                    ) from error
                raise InvalidAuditResponse(
                    f"{error}; schema-invalid audit persisted after {attempts} attempts"
                ) from error
    raise RuntimeError("audit response retry loop ended unexpectedly")


def control_name_key(control: dict[str, Any]) -> str:
    return control["name"].strip().casefold()


def effective_recheck_items(
    first_items: list[dict[str, Any]], second_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    second_passes = {
        control_name_key(item): item
        for item in second_items
        if item["verdict"] == "PASS"
    }
    effective: list[dict[str, Any]] = []
    included_names: set[str] = set()
    for item in first_items:
        name_key = control_name_key(item)
        if item["verdict"] == "FAIL" and name_key in second_passes:
            effective.append(second_passes[name_key])
        else:
            effective.append(item)
        included_names.add(name_key)

    for item in second_items:
        name_key = control_name_key(item)
        if item["verdict"] == "PASS" and name_key not in included_names:
            effective.append(item)
            included_names.add(name_key)
    return effective


def adjudicate_recheck(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    controls = effective_recheck_items(first["controls"], second["controls"])
    pairs = effective_recheck_items(first["pairs"], second["pairs"])

    if not controls:
        raise ValueError(
            "Recheck adjudication produced no effective controls; image fails closed."
        )

    has_failure = any(item["verdict"] == "FAIL" for item in controls)
    return {
        "controls": controls,
        "pairs": pairs,
        "verdict": "FAIL" if has_failure else "PASS",
        "rechecks": [first, second],
    }


def audit_image(
    account_id: str,
    api_token: str,
    image_path: Path,
    metadata_by_image: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = metadata_by_image.get(
        image_path.name,
        {"route": "unknown", "viewport": "unknown", "state": "unknown", "pairs": []},
    )
    try:
        missing_variables = [
            name
            for name, value in (
                ("WORKERS_AI_TOKEN", api_token),
                ("WORKERS_AI_ACCOUNT_ID", account_id),
            )
            if not value
        ]
        if missing_variables:
            raise RuntimeError(f"{', '.join(missing_variables)} required")
        normalized = request_normalized_audit(
            account_id, api_token, image_path, metadata
        )
        if normalized["verdict"] == "FAIL":
            recheck = request_normalized_audit(
                account_id, api_token, image_path, metadata
            )
            normalized = adjudicate_recheck(normalized, recheck)
        result = {
            "image": image_path.name,
            "route": metadata["route"],
            "viewport": metadata["viewport"],
            "state": metadata["state"],
            **normalized,
        }
        if image_path.name not in metadata_by_image:
            result["verdict"] = "FAIL"
            result["error"] = "Screenshot has no matching ui-states.json entry."
    except Exception as error:  # Every request error must be recorded and fail the gate.
        result = {
            "image": image_path.name,
            "route": metadata["route"],
            "viewport": metadata["viewport"],
            "state": metadata["state"],
            "controls": [],
            "pairs": [],
            "verdict": "FAIL",
            "error": str(error),
        }
    return result


def table_reason(result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result["error"])
    failed = [control for control in result["controls"] if control["verdict"] == "FAIL"]
    if failed:
        return f"{failed[0]['name']}: {failed[0]['reason']}"
    return "All reported controls passed; pair observations are advisory."


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def pair_summary(result: dict[str, Any]) -> str:
    pairs = result.get("pairs", [])
    passed = sum(pair.get("verdict") == "PASS" for pair in pairs)
    failed = sum(pair.get("verdict") == "FAIL" for pair in pairs)
    not_assessed = sum(pair.get("verdict") == "NOT_ASSESSED" for pair in pairs)
    return f"{passed} pass / {failed} fail / {not_assessed} not assessed"


def write_summary(results: list[dict[str, Any]], verdict: str) -> None:
    lines = [
        "## Workers AI visual audit",
        "",
        "| Image | Controls | Pairs (advisory) | Verdict | Result |",
        "| --- | ---: | --- | :---: | --- |",
    ]
    for result in results:
        lines.append(
            "| {image} | {count} | {pairs} | {verdict} | {reason} |".format(
                image=markdown_cell(result["image"]),
                count=len(result.get("controls", [])),
                pairs=pair_summary(result),
                verdict=result["verdict"],
                reason=markdown_cell(table_reason(result)),
            )
        )
    lines.extend(("", f"Overall verdict: **{verdict}**", ""))
    summary = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)


def main() -> int:
    account_id = os.environ.get("WORKERS_AI_ACCOUNT_ID", "").strip()
    api_token = os.environ.get("WORKERS_AI_TOKEN", "").strip()
    missing_variables = [
        name
        for name, value in (
            ("WORKERS_AI_TOKEN", api_token),
            ("WORKERS_AI_ACCOUNT_ID", account_id),
        )
        if not value
    ]
    if missing_variables:
        print(f"ERROR: {', '.join(missing_variables)} required", file=sys.stderr)

    metadata_by_image = load_state_metadata(Path("ui-states.json"))
    images = sorted(Path("visual-out").rglob("*.png"))
    actual_names = {image.name for image in images}
    results: list[dict[str, Any]] = []
    must: list[dict[str, str]] = []

    for missing_name in sorted(set(metadata_by_image) - actual_names):
        metadata = metadata_by_image[missing_name]
        reason = "Expected screenshot is missing from visual-out."
        results.append(
            {
                "image": missing_name,
                "route": metadata["route"],
                "viewport": metadata["viewport"],
                "state": metadata["state"],
                "pairs": [],
                "controls": [],
                "verdict": "FAIL",
                "error": reason,
            }
        )
        must.append({"image": missing_name, "control": "capture", "reason": reason})

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                audit_image,
                account_id,
                api_token,
                image_path,
                metadata_by_image,
            )
            for image_path in images
        ]
        audited_results = [future.result() for future in futures]

    for result in audited_results:
        results.append(result)
        print(f"{result['image']}: {result['verdict']}")

        if result["verdict"] == "FAIL":
            failed_controls = [
                control for control in result.get("controls", []) if control["verdict"] == "FAIL"
            ]
            for control in failed_controls:
                must.append(
                    {
                        "image": result["image"],
                        "control": control["name"],
                        "reason": control["reason"],
                    }
                )
            if not failed_controls:
                must.append(
                    {
                        "image": result["image"],
                        "control": "audit",
                        "reason": result.get("error", "Image verdict was FAIL."),
                    }
                )

    if not images:
        reason = "No PNG files were found in visual-out; zero images were audited."
        print(f"ERROR: {reason}", file=sys.stderr)
        if not results:
            results.append(
                {
                    "image": "(none)",
                    "route": "unknown",
                    "viewport": "unknown",
                    "state": "unknown",
                    "pairs": [],
                    "controls": [],
                    "verdict": "FAIL",
                    "error": reason,
                }
            )
        must.append({"image": "(none)", "control": "capture", "reason": reason})

    verdict = "FAIL" if must or any(result["verdict"] == "FAIL" for result in results) else "PASS"
    output = {"model": MODEL, "results": results, "must": must, "verdict": verdict}
    Path("vision-workersai.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_summary(results, verdict)
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
