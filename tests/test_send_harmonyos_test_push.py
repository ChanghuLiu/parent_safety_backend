import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import pytest

from harmonyos_push import (
    HARMONYOS_PUSH_APP_ID,
    HarmonyOSJWTAuthenticator,
    HarmonyOSNotification,
    HarmonyOSPushProvider,
    load_harmonyos_service_account,
)
from scripts import send_harmonyos_test_push as sender


@pytest.fixture()
def sender_environment(tmp_path: Path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    credential_path = tmp_path / "harmonyos-service-account.json"
    credential_path.write_text(
        json.dumps(
            {
                "project_id": "test-project-123",
                "key_id": "never-print-key-id",
                "private_key": private_pem,
                "sub_account": "test-service-account",
            }
        ),
        encoding="utf-8",
    )
    credential_path.chmod(0o600)
    return {
        "HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE": str(credential_path),
        "HARMONYOS_PUSH_APP_ID": HARMONYOS_PUSH_APP_ID,
        "PUSH_DELIVERY_ENABLED": "false",
        "HARMONYOS_PUSH_ENABLED": "true",
    }


def _apply_environment(monkeypatch, environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _provider(environment, handler):
    authenticator = HarmonyOSJWTAuthenticator(
        credentials_loader=lambda: load_harmonyos_service_account(environment),
        clock=lambda: 1_900_000_000,
    )
    return HarmonyOSPushProvider(
        authenticator=authenticator,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_default_invocation_is_dry_run_with_zero_network_calls(
    sender_environment, capsys
):
    calls = []
    provider = _provider(
        sender_environment,
        lambda request: calls.append(request),
    )

    assert sender.main([], environ=sender_environment, provider=provider) == 0

    output = capsys.readouterr().out
    assert "mode: dry run" in output
    assert "token_length: 0" in output
    assert "network_calls: 0" in output
    assert calls == []


@pytest.mark.parametrize(
    ("changes", "arguments", "expected_blocker"),
    [
        (
            {"PUSH_DELIVERY_ENABLED": "false"},
            ["--send", "--token", "test-token", "--yes"],
            "PUSH_DELIVERY_ENABLED",
        ),
        (
            {
                "PUSH_DELIVERY_ENABLED": "true",
                "HARMONYOS_PUSH_ENABLED": "false",
            },
            ["--send", "--token", "test-token", "--yes"],
            "HARMONYOS_PUSH_ENABLED",
        ),
        (
            {"PUSH_DELIVERY_ENABLED": "true"},
            ["--send", "--yes"],
            "non-empty Push token",
        ),
        (
            {"PUSH_DELIVERY_ENABLED": "true"},
            ["--send", "--token", "", "--yes"],
            "non-empty Push token",
        ),
    ],
)
def test_send_is_blocked_when_a_required_safeguard_is_missing(
    sender_environment, capsys, changes, arguments, expected_blocker
):
    environment = dict(sender_environment)
    environment.update(changes)
    calls = []
    provider = _provider(environment, lambda request: calls.append(request))

    assert sender.main(arguments, environ=environment, provider=provider) == 0

    output = capsys.readouterr().out
    assert "mode: blocked" in output
    assert expected_blocker in output
    assert calls == []


def test_token_file_is_protected_and_never_printed(
    sender_environment, tmp_path: Path, capsys
):
    token = "super-sensitive-token-1234"
    token_path = tmp_path / "push-token"
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    provider = _provider(sender_environment, lambda request: pytest.fail())

    assert (
        sender.main(
            ["--token-file", str(token_path), "--json-output"],
            environ=sender_environment,
            provider=provider,
        )
        == 0
    )

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["token_length"] == len(token)
    assert summary["token_suffix"] == "***1234"
    assert token not in output


def test_interactive_confirmation_decline_blocks_without_network(
    sender_environment, monkeypatch, capsys
):
    environment = dict(sender_environment)
    environment["PUSH_DELIVERY_ENABLED"] = "true"
    _apply_environment(monkeypatch, environment)
    calls = []
    provider = _provider(environment, lambda request: calls.append(request))

    assert (
        sender.main(
            ["--send", "--token", "test-token"],
            environ=environment,
            input_fn=lambda _: "no",
            provider=provider,
        )
        == 0
    )

    assert "interactive confirmation declined" in capsys.readouterr().out
    assert calls == []


def test_yes_bypasses_only_confirmation_not_other_safeguards(
    sender_environment, capsys
):
    calls = []
    provider = _provider(
        sender_environment, lambda request: calls.append(request)
    )

    assert (
        sender.main(
            ["--send", "--token", "test-token", "--yes"],
            environ=sender_environment,
            provider=provider,
        )
        == 0
    )

    assert "PUSH_DELIVERY_ENABLED" in capsys.readouterr().out
    assert calls == []


def test_official_default_url_and_environment_override(monkeypatch):
    provider = HarmonyOSPushProvider()
    assert provider.api_endpoint("project-123") == (
        "https://push-api.cloud.huawei.com/v3/project-123/messages:send"
    )
    monkeypatch.setenv(
        "HARMONYOS_PUSH_API_URL",
        "https://push.example.test/v3/{project_id}/messages:send",
    )
    override = HarmonyOSPushProvider()
    assert override.api_endpoint("project-123") == (
        "https://push.example.test/v3/project-123/messages:send"
    )


def test_dry_run_payload_matches_production_provider_schema():
    provider = HarmonyOSPushProvider()
    notification = HarmonyOSNotification(
        title=sender.DEFAULT_TITLE,
        body=sender.DEFAULT_BODY,
        category="MARKETING",
        event_type=sender.DEFAULT_EVENT_TYPE,
        target_page=sender.DEFAULT_TARGET_PAGE,
    )

    assert provider.build_request_body("test-token", notification) == {
        "payload": {
            "notification": {
                "category": "MARKETING",
                "title": sender.DEFAULT_TITLE,
                "body": sender.DEFAULT_BODY,
                "clickAction": {
                    "actionType": 0,
                    "data": {
                        "event_type": sender.DEFAULT_EVENT_TYPE,
                        "target_page": sender.DEFAULT_TARGET_PAGE,
                    },
                },
            }
        },
        "target": {"token": ["test-token"]},
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                200,
                json={"code": "80000000", "msg": "Success"},
            ),
            "request accepted",
        ),
        (
            httpx.Response(
                400,
                json={
                    "code": "80300007",
                    "msg": json.dumps(
                        {
                            "illegalTokens": {
                                "tokenFormatError": [
                                    "super-sensitive-token-1234"
                                ]
                            }
                        }
                    ),
                },
            ),
            "invalid token",
        ),
        (
            httpx.Response(
                401,
                json={"code": "80200005", "msg": "expired"},
            ),
            "authentication failure",
        ),
        (
            httpx.Response(
                503,
                json={"code": "80300029", "msg": "limited"},
            ),
            "rate limited",
        ),
        (
            httpx.Response(
                400,
                json={"code": "80300008", "msg": "rejected"},
            ),
            "provider rejection",
        ),
    ],
)
def test_mocked_send_results_are_normalized_without_exposing_secrets(
    sender_environment, monkeypatch, capsys, response, expected
):
    environment = dict(sender_environment)
    environment["PUSH_DELIVERY_ENABLED"] = "true"
    _apply_environment(monkeypatch, environment)
    calls = []

    def handler(request):
        calls.append(request)
        return response

    provider = _provider(environment, handler)
    token = "super-sensitive-token-1234"

    assert (
        sender.main(
            ["--send", "--token", token, "--yes"],
            environ=environment,
            provider=provider,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert expected in output
    assert token not in output
    assert "never-print-key-id" not in output
    assert "BEGIN PRIVATE KEY" not in output
    assert len(calls) == 1


def test_mocked_timeout_is_normalized(
    sender_environment, monkeypatch, capsys
):
    environment = dict(sender_environment)
    environment["PUSH_DELIVERY_ENABLED"] = "true"
    _apply_environment(monkeypatch, environment)

    def timeout(request):
        raise httpx.ReadTimeout("mock timeout", request=request)

    provider = _provider(environment, timeout)

    assert (
        sender.main(
            ["--send", "--token", "test-token", "--yes"],
            environ=environment,
            provider=provider,
        )
        == 0
    )

    assert "temporary network failure" in capsys.readouterr().out


def test_unknown_argument_is_rejected():
    with pytest.raises(SystemExit):
        sender.main(["--unknown-field", "value"], environ={})
