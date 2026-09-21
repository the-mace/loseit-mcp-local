"""send-alert.sh: logs attempts and invokes sendmail (no real SMTP)."""
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEND_ALERT = REPO / "scripts" / "send-alert.sh"


def _run(home: Path, env_extra: dict, args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(SEND_ALERT), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _fake_sendmail(path: Path, exit_code: int = 0) -> Path:
    path.write_text(
        f"""#!/bin/bash
cat > "${{SENT_FILE:?}}"
echo "sendmail-args: $*" >&2
exit {exit_code}
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_skips_when_email_unset(tmp_path):
    result = _run(tmp_path, {"LOSEIT_ALERT_EMAIL": ""}, ["hello"], stdin="body\n")
    assert result.returncode == 1
    log = (tmp_path / ".loseit-data" / "logs" / "alert.log").read_text()
    assert "SKIP" in log
    assert "LOSEIT_ALERT_EMAIL unset" in log


def test_sends_via_sendmail_and_logs(tmp_path):
    sent = tmp_path / "sent.eml"
    fake = _fake_sendmail(tmp_path / "sendmail")
    result = _run(
        tmp_path,
        {
            "LOSEIT_ALERT_EMAIL": "rob@example.com",
            "LOSEIT_SENDMAIL": str(fake),
            "SENT_FILE": str(sent),
        },
        ["loseit-scraper FAILED"],
        stdin="the body\n",
    )
    assert result.returncode == 0, result.stderr
    raw = sent.read_text()
    assert "Subject: loseit-scraper FAILED" in raw
    assert "From: rob@example.com" in raw
    assert "To: rob@example.com" in raw
    assert "the body" in raw
    log = (tmp_path / ".loseit-data" / "logs" / "alert.log").read_text()
    assert "SENT exit=0" in log
    assert "rob@example.com" in log
    assert "loseit-scraper FAILED" in log


def test_logs_sendmail_failure(tmp_path):
    sent = tmp_path / "sent.eml"
    fake = _fake_sendmail(tmp_path / "sendmail", exit_code=42)
    result = _run(
        tmp_path,
        {
            "LOSEIT_ALERT_EMAIL": "rob@example.com",
            "LOSEIT_SENDMAIL": str(fake),
            "SENT_FILE": str(sent),
        },
        ["nope"],
        stdin="x\n",
    )
    assert result.returncode == 42
    log = (tmp_path / ".loseit-data" / "logs" / "alert.log").read_text()
    assert "FAIL exit=42" in log
    assert "WARNING: alert send failed" in result.stderr
