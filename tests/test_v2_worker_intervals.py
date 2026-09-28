import pytest

from scripts import run_v2_worker


def test_worker_defaults_keep_scheduler_at_one_minute_but_pushes_fast(monkeypatch):
    monkeypatch.delenv("V2_WORKER_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("V2_NOTIFICATION_INTERVAL_SECONDS", raising=False)
    assert run_v2_worker._read_intervals() == (60, 5)


def test_worker_allows_faster_notification_polling_without_speeding_scheduler(monkeypatch):
    monkeypatch.setenv("V2_WORKER_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("V2_NOTIFICATION_INTERVAL_SECONDS", "2")
    assert run_v2_worker._read_intervals() == (120, 2)


@pytest.mark.parametrize("value", ["0", "61"])
def test_notification_interval_guard(monkeypatch, value):
    monkeypatch.setenv("V2_NOTIFICATION_INTERVAL_SECONDS", value)
    with pytest.raises(SystemExit):
        run_v2_worker._read_intervals()
