import importlib
import os

import pytest


def _load(monkeypatch, **values):
    keys = [
        "ANDROID_LATEST_VERSION_CODE",
        "ANDROID_CHILD_MIN_VERSION_CODE",
        "ANDROID_PARENT_FORCE_UPDATE",
        "ANDROID_PARENT_MIN_VERSION_CODE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    import v2_routes
    importlib.reload(v2_routes)
    return v2_routes._app_version_policy()


def test_default_policy_keeps_parent_optional_and_child_forced(monkeypatch):
    policy = _load(monkeypatch)
    assert policy.latest_version_code == 11
    assert policy.child_min_version_code == 11
    assert policy.child_force_update is True
    assert policy.parent_force_update is False
    assert policy.parent_min_version_code == 11


def test_child_min_defaults_to_latest_and_parent_switch_can_be_enabled(monkeypatch):
    policy = _load(
        monkeypatch,
        ANDROID_LATEST_VERSION_CODE=52,
        ANDROID_PARENT_FORCE_UPDATE="true",
        ANDROID_PARENT_MIN_VERSION_CODE=49,
    )
    assert policy.child_min_version_code == 52
    assert policy.parent_force_update is True
    assert policy.parent_min_version_code == 49


def test_parent_switch_false_never_changes_child_force_behavior(monkeypatch):
    policy = _load(
        monkeypatch,
        ANDROID_LATEST_VERSION_CODE=60,
        ANDROID_CHILD_MIN_VERSION_CODE=60,
        ANDROID_PARENT_FORCE_UPDATE="false",
        ANDROID_PARENT_MIN_VERSION_CODE=40,
    )
    assert policy.child_force_update is True
    assert policy.child_min_version_code == 60
    assert policy.parent_force_update is False


def test_impossible_policy_is_rejected(monkeypatch):
    with pytest.raises(RuntimeError):
        _load(
            monkeypatch,
            ANDROID_LATEST_VERSION_CODE=40,
            ANDROID_CHILD_MIN_VERSION_CODE=41,
        )
