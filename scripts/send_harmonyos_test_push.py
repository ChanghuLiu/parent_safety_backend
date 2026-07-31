#!/usr/bin/env python3
"""Preview a HarmonyOS Push request, with an explicitly guarded send mode."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _ensure_project_python() -> None:
    required_modules = ("httpx", "jwt", "cryptography")
    if all(importlib.util.find_spec(name) is not None for name in required_modules):
        return
    for candidate in (
        PROJECT_ROOT / ".venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "bin" / "python",
    ):
        if candidate.is_file() and Path(sys.executable) != candidate:
            os.execv(
                str(candidate),
                [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
            )


_ensure_project_python()


from harmonyos_push import (  # noqa: E402
    DeliveryStatus,
    HarmonyOSNotification,
    HarmonyOSPushConfigurationError,
    HarmonyOSPushProvider,
    load_harmonyos_service_account,
    validate_harmonyos_push_app_id,
)


DEFAULT_TITLE = "爸妈平安测试通知"
DEFAULT_BODY = "这是一条 HarmonyOS Push 测试消息。"
DEFAULT_EVENT_TYPE = "test_notification"
DEFAULT_TARGET_PAGE = "ChildHomePage"
DRY_RUN_TOKEN = "dry-run-placeholder"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview a sanitized HarmonyOS Push request. Sending is disabled "
            "unless every explicit safeguard is satisfied."
        )
    )
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument("--token", help="Push token (prefer --token-file).")
    token_source.add_argument(
        "--token-file",
        type=Path,
        help="Path to a mode-0600 file containing one Push token.",
    )
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--body", default=DEFAULT_BODY)
    parser.add_argument("--event-type", default=DEFAULT_EVENT_TYPE)
    parser.add_argument("--target-page", default=DEFAULT_TARGET_PAGE)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Request a real send; all environment safeguards still apply.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip only the interactive confirmation.",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Print the sanitized result as JSON.",
    )
    return parser


def _read_token_file(path: Path) -> str:
    if path.is_symlink():
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push token file must not be a symbolic link"
        )
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push token file cannot be read"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o077:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push token file must be a protected regular file"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push token file cannot be read"
        ) from exc
    if not token:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push token file is empty"
        )
    return token


def _masked_token(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else ("***" if token else "none")


def _explicitly_true(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() == "true"


def _credential_file_readable(environ: Mapping[str, str]) -> bool:
    path_value = environ.get("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", "").strip()
    if not path_value:
        return False
    path = Path(path_value).expanduser()
    return path.is_file() and os.access(path, os.R_OK)


def _notification(args: argparse.Namespace) -> HarmonyOSNotification:
    return HarmonyOSNotification(
        title=args.title,
        body=args.body,
        category="MARKETING",
        event_type=args.event_type,
        target_page=args.target_page,
    )


def _emit(summary: Mapping[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    for key, value in summary.items():
        print(f"{key}: {value}")


def _normalized_result(status: DeliveryStatus) -> str:
    return {
        DeliveryStatus.ACCEPTED: "request accepted",
        DeliveryStatus.INVALID_TOKEN: "invalid token",
        DeliveryStatus.AUTHENTICATION_FAILURE: "authentication failure",
        DeliveryStatus.PROVIDER_REJECTED: "provider rejection",
        DeliveryStatus.RATE_LIMITED: "rate limited",
        DeliveryStatus.TEMPORARY_FAILURE: "temporary network failure",
        DeliveryStatus.DELIVERY_DISABLED: "delivery disabled",
        DeliveryStatus.NO_TOKEN: "no token",
        DeliveryStatus.UNSUPPORTED_PROVIDER: "unsupported provider",
    }[status]


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    provider: HarmonyOSPushProvider | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = environ if environ is not None else os.environ
    provider = provider or HarmonyOSPushProvider(
        api_url=env.get("HARMONYOS_PUSH_API_URL", "").strip() or None
    )

    try:
        token = (
            _read_token_file(args.token_file)
            if args.token_file is not None
            else (args.token or "").strip()
        )
    except HarmonyOSPushConfigurationError:
        _emit({"mode": "blocked", "reason": "invalid token file"}, args.json_output)
        return 2

    notification = _notification(args)
    credential_readable = _credential_file_readable(env)
    account = None
    validation_errors: list[str] = []
    phone_like = re.compile(r"\+?\d[\d\s().-]{6,}\d")
    if phone_like.search(notification.title) or phone_like.search(
        notification.body
    ):
        validation_errors.append("test content must not contain a phone number")
    try:
        account = load_harmonyos_service_account(env)
    except HarmonyOSPushConfigurationError:
        validation_errors.append("credentials invalid or unavailable")
    try:
        app_id = validate_harmonyos_push_app_id(env)
    except HarmonyOSPushConfigurationError:
        app_id = "not validated"
        validation_errors.append("APP ID invalid or unavailable")
    try:
        request_body = provider.build_request_body(
            token or DRY_RUN_TOKEN, notification
        )
        json.dumps(request_body, ensure_ascii=False)
    except HarmonyOSPushConfigurationError:
        validation_errors.append("notification payload invalid")
    try:
        endpoint = provider.api_endpoint_preview(
            account.project_id if account is not None else None
        )
        parsed_endpoint = urlparse(endpoint)
    except HarmonyOSPushConfigurationError:
        endpoint = ""
        parsed_endpoint = urlparse("")
        validation_errors.append("API URL invalid")

    summary: dict[str, object] = {
        "mode": "send requested" if args.send else "dry run",
        "api_scheme": parsed_endpoint.scheme or "unavailable",
        "api_host": parsed_endpoint.netloc or "unavailable",
        "api_path": parsed_endpoint.path or "unavailable",
        "api_version": "v3" if parsed_endpoint.path.startswith("/v3/") else "invalid",
        "app_id": app_id,
        "event_type": notification.event_type,
        "title": notification.title,
        "body": notification.body,
        "token_length": len(token),
        "token_suffix": _masked_token(token),
        "credentials_readable": credential_readable,
        "outbound_delivery_disabled": not _explicitly_true(
            env, "PUSH_DELIVERY_ENABLED"
        ),
        "validation_result": "PASS" if not validation_errors else "FAIL",
        "network_calls": 0,
    }

    if not args.send:
        _emit(summary, args.json_output)
        return 0

    blockers = []
    if not _explicitly_true(env, "PUSH_DELIVERY_ENABLED"):
        blockers.append("PUSH_DELIVERY_ENABLED is not explicitly true")
    if not _explicitly_true(env, "HARMONYOS_PUSH_ENABLED"):
        blockers.append("HARMONYOS_PUSH_ENABLED is not explicitly true")
    if not token:
        blockers.append("a non-empty Push token was not supplied")
    if blockers:
        summary["mode"] = "blocked"
        summary["blocked_by"] = blockers
        _emit(summary, args.json_output)
        return 0

    if validation_errors or account is None:
        summary["mode"] = "configuration error"
        summary["configuration_errors"] = validation_errors
        _emit(summary, args.json_output)
        return 2
    try:
        provider.api_endpoint(account.project_id)
    except HarmonyOSPushConfigurationError:
        summary["mode"] = "configuration error"
        summary["configuration_errors"] = [
            "project ID or complete API URL is required for real send"
        ]
        _emit(summary, args.json_output)
        return 2

    if not args.yes:
        try:
            confirmed = input_fn(
                "Send one real HarmonyOS test notification? Type 'send' to confirm: "
            )
        except EOFError:
            confirmed = ""
        if confirmed.strip().lower() != "send":
            summary["mode"] = "blocked"
            summary["blocked_by"] = ["interactive confirmation declined"]
            _emit(summary, args.json_output)
            return 0

    result = provider.send(token, notification)
    summary["mode"] = "send result"
    summary["result"] = _normalized_result(result.status)
    summary["network_calls"] = 1
    if result.status == DeliveryStatus.ACCEPTED:
        summary["delivery_note"] = (
            "Request accepted does not prove that a device displayed the notification."
        )
    _emit(summary, args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
