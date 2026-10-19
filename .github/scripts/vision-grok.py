#!/usr/bin/env python3
"""Run one Azure AI Foundry Grok vision audit for every visual-gate PNG."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_WORKERS = 4
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 300
CHECK_NAMES = (
    "legible",
    "centred",
    "matches_sibling",
    "no_overflow",
    "no_raw_markdown",
)


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
    return "\n".join(
        (
            "Audit this UI screenshot control by control.",
            f"Image: {image_name}",
            f"Route: {metadata['route']}",
            f"Viewport: {metadata['viewport']}",
            f"State: {metadata['state']}",
            "Sibling-control pairs from ui-states.json:",
            json.dumps(metadata["pairs"], separators=(",", ":")),
            "For every visible button, link, CTA, and input, report whether:",
            "1. legible: its text has adequate contrast by eye;",
            "2. centred: its label plus any accompanying icon are visually centred as one group; an arrow or icon beside text is not by itself off-centre;",
            "3. matches_sibling: it matches any named sibling in text colour, font family (serif vs sans), font weight, and alignment;",
            "4. no_overflow: no text is overflowing or clipped;",
            "5. no_raw_markdown: it shows no raw Markdown or code fences.",
            "A size difference between a header button and a full-width drawer button is expected and is not a failure.",
            "If a sibling is not visible, set matches_sibling true only when there is no visible mismatch to assess and explain that limitation.",
            "Return only one JSON object with exactly this shape:",
            '{"controls":[{"name":"visible control name","checks":{"legible":true,"centred":true,"matches_sibling":true,"no_overflow":true,"no_raw_markdown":true},"verdict":"PASS","reason":"concise visual evidence"}],"verdict":"PASS"}',
            "Use only PASS or FAIL. The image verdict must be FAIL when any control or check fails. Do not wrap JSON in Markdown or code fences.",
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
    raise ValueError("model response content does not contain a JSON object")


def retry_delay(retry: int, retry_after: str | None = None) -> float:
    try:
        delay = float(retry_after) if retry_after else 2**retry
    except ValueError:
        delay = 2**retry
    return min(max(delay, 1), 30)


def request_audit(
    endpoint: str,
    api_key: str,
    model: str,
    image_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict visual quality gate. Report only facts visible in the supplied screenshot and return valid JSON.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(image_path.name, metadata)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{encoded}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "api-key": api_key,
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
                preview = raw_body[:200].decode("utf-8", errors="replace")
                raise ValueError(f"response is not JSON: {preview}") from error
            content = response_body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("model response content is not a JSON string")
            return extract_first_json_object(content)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            retryable = error.code == 429 or 500 <= error.code < 600
            if retryable and retry < MAX_RETRIES:
                time.sleep(retry_delay(retry, error.headers.get("Retry-After")))
                continue
            raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        except (TimeoutError, socket.timeout) as error:
            if retry < MAX_RETRIES:
                time.sleep(retry_delay(retry))
                continue
            raise RuntimeError(
                f"request timed out after {MAX_RETRIES + 1} attempts: {error}"
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
                    f"request timed out after {MAX_RETRIES + 1} attempts: {error.reason}"
                ) from error
            raise RuntimeError(f"network error: {error.reason}") from error

    raise RuntimeError("Azure AI Foundry request failed after three retries")


def normalize_audit(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("response is not an object")
    controls = raw.get("controls")
    if not isinstance(controls, list) or not controls:
        raise ValueError("controls must be a non-empty list")

    normalized_controls = []
    has_failure = False
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise ValueError(f"control {index} is not an object")
        name = control.get("name")
        reason = control.get("reason")
        checks = control.get("checks")
        verdict = control.get("verdict")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"control {index} has no name")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"control {index} has no reason")
        if not isinstance(checks, dict):
            raise ValueError(f"control {index} checks is not an object")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError(f"control {index} verdict is invalid")

        normalized_checks = {}
        for check_name in CHECK_NAMES:
            value = checks.get(check_name)
            if not isinstance(value, bool):
                raise ValueError(f"control {index} check {check_name} is not boolean")
            normalized_checks[check_name] = value

        control_failed = verdict == "FAIL" or not all(normalized_checks.values())
        has_failure = has_failure or control_failed
        normalized_controls.append(
            {
                "name": name,
                "checks": normalized_checks,
                "verdict": "FAIL" if control_failed else "PASS",
                "reason": reason,
            }
        )

    if raw.get("verdict") not in ("PASS", "FAIL"):
        raise ValueError("image verdict is invalid")
    has_failure = has_failure or raw["verdict"] == "FAIL"
    return {
        "controls": normalized_controls,
        "verdict": "FAIL" if has_failure else "PASS",
    }


def audit_image(
    endpoint: str,
    api_key: str,
    model: str,
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
                ("AZURE_VISION_ENDPOINT", endpoint),
                ("AZURE_VISION_KEY", api_key),
                ("AZURE_VISION_MODEL", model),
            )
            if not value
        ]
        if missing_variables:
            raise RuntimeError(f"{', '.join(missing_variables)} required")
        normalized = normalize_audit(
            request_audit(endpoint, api_key, model, image_path, metadata)
        )
        result = {"image": image_path.name, **metadata, **normalized}
        if image_path.name not in metadata_by_image:
            result["verdict"] = "FAIL"
            result["error"] = "Screenshot has no matching ui-states.json entry."
    except Exception as error:  # Every request error must be recorded and fail the gate.
        result = {
            "image": image_path.name,
            **metadata,
            "controls": [],
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
    return "All reported controls passed."


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_summary(results: list[dict[str, Any]], verdict: str) -> None:
    lines = [
        "## Grok visual audit",
        "",
        "| Image | Controls | Verdict | Result |",
        "| --- | ---: | :---: | --- |",
    ]
    for result in results:
        lines.append(
            "| {image} | {count} | {verdict} | {reason} |".format(
                image=markdown_cell(result["image"]),
                count=len(result.get("controls", [])),
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
    endpoint = os.environ.get("AZURE_VISION_ENDPOINT", "").strip()
    api_key = os.environ.get("AZURE_VISION_KEY", "").strip()
    model = os.environ.get("AZURE_VISION_MODEL", "").strip()
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
                **metadata,
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
                endpoint,
                api_key,
                model,
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
            if failed_controls:
                for control in failed_controls:
                    must.append(
                        {
                            "image": result["image"],
                            "control": control["name"],
                            "reason": control["reason"],
                        }
                    )
            else:
                must.append(
                    {
                        "image": result["image"],
                        "control": "audit",
                        "reason": result.get("error", "Image verdict was FAIL."),
                    }
                )

    if not images:
        reason = "No PNG files were found in visual-out."
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
    output = {"model": model, "results": results, "must": must, "verdict": verdict}
    Path("vision-grok.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_summary(results, verdict)
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
