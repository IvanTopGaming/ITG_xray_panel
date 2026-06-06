from app.services.xray import _extract_reason


def test_extract_reason_takes_segment_after_last_arrow():
    out = (
        b"Failed to start: main: failed to load config files: [/x] > "
        b"infra/conf/serial: failed to read config > open /x: no such file or directory"
    )
    assert _extract_reason(out) == "open /x: no such file or directory"


def test_extract_reason_handles_str_and_plain_failed_line():
    out = "2026/06/03 [Info] reading\nFailed to start: bad outbound protocol 'freedomm'"
    assert _extract_reason(out) == "Failed to start: bad outbound protocol 'freedomm'"


def test_extract_reason_empty_falls_back():
    assert _extract_reason(b"") == "Xray could not parse the configuration"


import subprocess
from unittest.mock import MagicMock, patch
import pytest
from app.services import xray as xray_svc


def test_validate_skips_when_binary_absent():
    with patch("app.services.xray.os.path.exists", return_value=False):
        with patch("app.services.xray.subprocess.run") as run:
            xray_svc._validate_xray_config("/tmp/c.json")  # must not raise
    run.assert_not_called()


def test_validate_ok_on_exit_zero():
    with patch("app.services.xray.os.path.exists", return_value=True):
        with patch(
            "app.services.xray.subprocess.run",
            return_value=MagicMock(returncode=0, stderr=b"Configuration OK.", stdout=b""),
        ):
            xray_svc._validate_xray_config("/tmp/c.json")  # must not raise


def test_validate_raises_on_nonzero_with_reason():
    with patch("app.services.xray.os.path.exists", return_value=True):
        with patch(
            "app.services.xray.subprocess.run",
            return_value=MagicMock(
                returncode=23,
                stderr=b"Failed to start: bad outbound protocol 'freedomm'",
                stdout=b"",
            ),
        ):
            with pytest.raises(ValueError) as exc:
                xray_svc._validate_xray_config("/tmp/c.json")
    assert "freedomm" in str(exc.value)


def test_validate_fail_closed_on_timeout():
    with patch("app.services.xray.os.path.exists", return_value=True):
        with patch(
            "app.services.xray.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="xray", timeout=10),
        ):
            with pytest.raises(ValueError):
                xray_svc._validate_xray_config("/tmp/c.json")


class TestGenerateConfigGate:
    @pytest.fixture(autouse=True)
    def _setup(self, app, db, tmp_path):
        # app + db come from conftest; redirect config file I/O to tmp_path.
        self._ctx = app.app_context()
        self._ctx.push()
        self._patches = [
            patch("app.services.xray.LOCK_PATH", str(tmp_path / "config.lock")),
            patch("app.services.xray.CONFIG_PATH", str(tmp_path / "config.json")),
            patch("app.services.xray.CANDIDATE_PATH", str(tmp_path / "config.json.candidate")),
        ]
        for p in self._patches:
            p.start()
        self.cfg = tmp_path / "config.json"
        self.candidate = tmp_path / "config.json.candidate"
        yield
        for p in self._patches:
            p.stop()
        self._ctx.pop()

    def test_valid_config_is_committed(self):
        from app.services.xray import generate_config_file

        with patch("app.services.xray._validate_xray_config"):  # no-op = valid
            generate_config_file()
        assert self.cfg.exists()
        assert not self.candidate.exists()

    def test_rejected_config_is_not_committed_and_candidate_removed(self):
        from app.services.xray import generate_config_file

        self.cfg.write_text('{"old": "good"}')
        with patch(
            "app.services.xray._validate_xray_config",
            side_effect=ValueError("Xray rejected the config: bad thing"),
        ):
            with pytest.raises(ValueError, match="bad thing"):
                generate_config_file()
        assert self.cfg.read_text() == '{"old": "good"}'  # untouched
        assert not self.candidate.exists()  # cleaned up


def test_candidate_path_has_json_extension():
    """xray detects config format by file extension — candidate must end in .json."""
    from app.services import xray as xray_svc

    assert xray_svc.CANDIDATE_PATH.endswith(".json")
