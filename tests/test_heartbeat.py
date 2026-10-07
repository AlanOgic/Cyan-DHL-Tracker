import os
import time

import pytest
import requests

from heartbeat import MAX_SILENCE, PING_TIMEOUT_SECONDS, Heartbeat, HeartbeatConfigError, build_from_env, main

PING_URL = "https://hc.example.com/ping/secret-token"


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.gets = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.gets.append((url, timeout, allow_redirects))
        if self.error:
            raise self.error
        return self.response


def test_success_is_recorded_in_the_success_file(tmp_path):
    success_file = tmp_path / "last-success"

    Heartbeat(success_file=success_file, session=FakeSession()).record_success()

    assert success_file.exists()


def test_success_pings_the_external_monitor(tmp_path):
    session = FakeSession()

    Heartbeat(PING_URL, tmp_path / "last-success", session).record_success()

    # No redirects: a redirect would hand the secret ping URL's request to another host
    assert session.gets == [(PING_URL, PING_TIMEOUT_SECONDS, False)]


def test_no_ping_without_a_monitor_url(tmp_path):
    session = FakeSession()

    Heartbeat(None, tmp_path / "last-success", session).record_success()

    assert session.gets == []


@pytest.mark.parametrize("session", [
    FakeSession(error=requests.ConnectionError("Max retries exceeded with url: /ping/secret-token")),
    FakeSession(FakeResponse(404)),
    FakeSession(FakeResponse(302)),
])
def test_failed_ping_is_logged_without_the_url(tmp_path, caplog, session):
    success_file = tmp_path / "last-success"

    Heartbeat(PING_URL, success_file, session).record_success()

    assert "Heartbeat ping" in caplog.text
    assert "secret-token" not in caplog.text
    assert success_file.exists()


def test_unwritable_success_file_is_logged_and_the_monitor_still_pinged(tmp_path, caplog):
    session = FakeSession()

    Heartbeat(PING_URL, tmp_path / "missing-dir" / "last-success", session).record_success()

    assert "Could not record" in caplog.text
    assert len(session.gets) == 1


def test_success_is_never_written_through_a_symlink(tmp_path, caplog):
    target = tmp_path / "target"
    target.touch()
    old = time.time() - 3600
    os.utime(target, (old, old))
    success_file = tmp_path / "last-success"
    success_file.symlink_to(target)

    Heartbeat(success_file=success_file, session=FakeSession()).record_success()

    assert "Could not record" in caplog.text
    assert target.stat().st_mtime == old


def test_healthcheck_ignores_a_symlinked_success_file(tmp_path, capsys):
    target = tmp_path / "target"
    target.touch()
    success_file = tmp_path / "last-success"
    success_file.symlink_to(target)

    assert main(success_file) == 1
    assert "not a regular file" in capsys.readouterr().err


def test_build_from_env_reads_the_monitor_url():
    assert build_from_env({}).ping_url is None
    assert build_from_env({"HEARTBEAT_URL": f" {PING_URL} "}).ping_url == PING_URL


def test_build_from_env_rejects_a_non_https_monitor_url():
    with pytest.raises(HeartbeatConfigError, match="HEARTBEAT_URL"):
        build_from_env({"HEARTBEAT_URL": "http://hc.example.com/ping/secret-token"})


def test_healthcheck_passes_after_a_recent_success(tmp_path):
    success_file = tmp_path / "last-success"
    success_file.touch()

    assert main(success_file) == 0


def test_healthcheck_fails_when_the_last_success_is_too_old(tmp_path, capsys):
    success_file = tmp_path / "last-success"
    success_file.touch()
    too_old = time.time() - MAX_SILENCE.total_seconds() - 60
    os.utime(success_file, (too_old, too_old))

    assert main(success_file) == 1
    assert "unhealthy" in capsys.readouterr().err


def test_healthcheck_fails_before_any_success(tmp_path, capsys):
    assert main(tmp_path / "last-success") == 1
    assert "no successful check yet" in capsys.readouterr().err
