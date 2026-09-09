"""setup.py --json surfaces the resolved watch detail."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import setup as watch_setup

SETUP = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts" / "setup.py"


def _run(args, *, home=None, extra_env=None):
    env = dict(os.environ)
    env.pop("WATCH_DETAIL", None)
    # Don't let a real key (or local-whisper config) in the developer's shell
    # env leak into the test.
    env.pop("GROQ_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("SETUP_COMPLETE", None)
    env.pop("WATCH_WHISPER_URL", None)
    env.pop("WATCH_WHISPER_TOKEN", None)
    env.pop("WATCH_WHISPER_MODEL", None)
    env.pop("WATCH_WHISPER_TIMEOUT", None)
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SETUP), *args],
        capture_output=True, text=True, env=env,
    )


def _write_env(home: Path, body: str) -> None:
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True, exist_ok=True)
    f = cfg / ".env"
    f.write_text(body, encoding="utf-8")
    f.chmod(0o600)


def test_json_reports_watch_detail():
    proc = _run(["--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["watch_detail"] == "balanced"


def test_keyless_completed_setup_proceeds_silently(tmp_path):
    """A user who finished setup without a key must NOT be nagged forever."""
    _write_env(tmp_path, "GROQ_API_KEY=\nOPENAI_API_KEY=\nSETUP_COMPLETE=true\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 0, f"keyless-complete should pass --check; got {chk.returncode}: {chk.stderr}"
    assert chk.stdout == "" and chk.stderr == ""

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["can_proceed"] is True
    assert js["first_run"] is False
    assert js["setup_complete"] is True
    # status still encourages a key even though we can proceed
    assert js["status"] == "needs_key"


def test_keyless_first_run_is_encouraged(tmp_path):
    """Genuine first run with no key: --check reports exit 3 (encourage a key)."""
    _write_env(tmp_path, "GROQ_API_KEY=\nOPENAI_API_KEY=\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 3, chk.stderr

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["can_proceed"] is False
    assert js["first_run"] is True


def test_key_present_is_ready(tmp_path):
    _write_env(tmp_path, "GROQ_API_KEY=sk-test-abc\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 0, chk.stderr

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["status"] == "ready"
    assert js["can_proceed"] is True
    assert js["whisper_backend"] == "groq"


def test_json_reports_proxy_and_tls_fields():
    proc = _run(["--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "proxy_configured" in data
    assert "tls_patched" in data
    assert "cookies_configured" in data


def test_json_reports_proxy_configured_true(tmp_path):
    js = json.loads(_run(["--json"], home=tmp_path, extra_env={"WATCH_PROXY": "socks5://127.0.0.1:1080"}).stdout)
    assert js["proxy_configured"] is True


def test_json_reports_cookies_configured_true(tmp_path):
    js = json.loads(_run(["--json"], home=tmp_path, extra_env={"WATCH_COOKIES": "/home/user/cookies.txt"}).stdout)
    assert js["cookies_configured"] is True


def test_json_reports_cookies_configured_false_by_default(tmp_path):
    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["cookies_configured"] is False


def test_env_template_mentions_watch_cookies():
    assert "WATCH_COOKIES" in watch_setup.ENV_TEMPLATE


def test_env_template_mentions_watch_whisper_url():
    assert "WATCH_WHISPER_URL" in watch_setup.ENV_TEMPLATE


def test_json_reports_whisper_url_backend_and_configured(tmp_path):
    """WATCH_WHISPER_URL alone (set in .env, not just the environment) is
    enough to report both the local backend and whisper_url_configured —
    it wins priority even with no cloud key present."""
    _write_env(tmp_path, "WATCH_WHISPER_URL=http://127.0.0.1:8321\n")
    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["whisper_backend"] == "local"
    assert js["whisper_url_configured"] is True
    assert js["has_api_key"] is True
    # has_api_key=True rules out needs_key/needs_install_and_key; which of the
    # remaining two depends on whether ffmpeg/yt-dlp are on this machine's PATH.
    assert js["status"] in ("ready", "needs_install")


def test_json_reports_whisper_url_configured_false_by_default(tmp_path):
    _write_env(tmp_path, "GROQ_API_KEY=\nOPENAI_API_KEY=\n")
    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["whisper_url_configured"] is False


def _fake_certifi_env(tmp_path: Path, cacert_text: str) -> tuple[dict, Path]:
    """Build a PYTHONPATH-shadowed fake `certifi` package so --merge-ca /
    --restore-ca can be exercised end-to-end without touching the real
    installed certifi bundle."""
    pkg_root = tmp_path / "fakepkg"
    certifi_dir = pkg_root / "certifi"
    certifi_dir.mkdir(parents=True)
    cacert = certifi_dir / "cacert.pem"
    cacert.write_text(cacert_text, encoding="utf-8")
    (certifi_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "def where():\n"
        "    return str(Path(__file__).resolve().parent / 'cacert.pem')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(pkg_root) + os.pathsep + env.get("PYTHONPATH", "")
    return env, cacert


def test_merge_ca_then_restore_ca_cli(tmp_path):
    pristine = "-----BEGIN CERTIFICATE-----\nORIGINAL\n-----END CERTIFICATE-----\n"
    env, cacert = _fake_certifi_env(tmp_path, pristine)

    merged = subprocess.run(
        [sys.executable, str(SETUP), "--merge-ca"],
        capture_output=True, text=True, env=env,
    )
    assert merged.returncode == 0, merged.stderr
    merged_content = cacert.read_text(encoding="utf-8")
    assert "ORIGINAL" in merged_content
    assert len(merged_content) > len(pristine)  # system bundle appended

    restored = subprocess.run(
        [sys.executable, str(SETUP), "--restore-ca"],
        capture_output=True, text=True, env=env,
    )
    assert restored.returncode == 0, restored.stderr
    assert cacert.read_text(encoding="utf-8") == pristine


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_install_yt_dlp_via_pip_prefers_pipx(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_setup, "_which", lambda name: "/usr/bin/pipx" if name == "pipx" else None)
    monkeypatch.setattr(
        watch_setup.subprocess, "run",
        lambda cmd, *a, **k: calls.append(cmd) or _FakeCompletedProcess(0),
    )
    ok, msg = watch_setup._install_yt_dlp_via_pip()
    assert ok is True
    assert "pipx" in msg
    assert calls[0][:2] == ["pipx", "install"]


def test_install_yt_dlp_via_pip_falls_back_to_pip_user_when_no_pipx(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_setup, "_which", lambda name: None)
    monkeypatch.setattr(
        watch_setup.subprocess, "run",
        lambda cmd, *a, **k: calls.append(cmd) or _FakeCompletedProcess(0),
    )
    ok, msg = watch_setup._install_yt_dlp_via_pip()
    assert ok is True
    assert "pip --user" in msg
    assert calls[0][1:] == ["-m", "pip", "install", "--user", "yt-dlp"]


def test_install_yt_dlp_via_pip_retries_with_break_system_packages(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if "--break-system-packages" in cmd:
            return _FakeCompletedProcess(0)
        return _FakeCompletedProcess(1, stderr="error: externally-managed-environment")

    monkeypatch.setattr(watch_setup, "_which", lambda name: None)
    monkeypatch.setattr(watch_setup.subprocess, "run", fake_run)
    ok, msg = watch_setup._install_yt_dlp_via_pip()
    assert ok is True
    assert "break-system-packages" in msg
    assert len(calls) == 2
    assert "--break-system-packages" in calls[1]


def test_install_yt_dlp_via_pip_fails_cleanly_when_pip_errors(monkeypatch):
    monkeypatch.setattr(watch_setup, "_which", lambda name: None)
    monkeypatch.setattr(
        watch_setup.subprocess, "run",
        lambda cmd, *a, **k: _FakeCompletedProcess(1, stderr="some unrelated pip error"),
    )
    ok, msg = watch_setup._install_yt_dlp_via_pip()
    assert ok is False
    assert "pip install failed" in msg


def test_install_linux_installs_pip_targets_and_flags_manual_targets(monkeypatch):
    monkeypatch.setattr(watch_setup, "_install_yt_dlp_via_pip", lambda: (True, "installed yt-dlp via pip --user"))
    ok, msg = watch_setup._install_linux(["yt-dlp", "ffmpeg", "ffprobe"])
    assert ok is False  # ffmpeg/ffprobe still need a manual sudo install
    assert "installed yt-dlp via pip --user" in msg
    assert "sudo" in msg


def test_install_linux_all_pip_installable_succeeds(monkeypatch):
    monkeypatch.setattr(watch_setup, "_install_yt_dlp_via_pip", lambda: (True, "installed yt-dlp via pip --user"))
    ok, msg = watch_setup._install_linux(["yt-dlp"])
    assert ok is True
    assert msg == "installed yt-dlp via pip --user"


def test_install_linux_reports_pip_install_failure(monkeypatch):
    monkeypatch.setattr(
        watch_setup, "_install_yt_dlp_via_pip",
        lambda: (False, "pip install failed with exit code 1"),
    )
    ok, msg = watch_setup._install_linux(["yt-dlp"])
    assert ok is False
    assert "pip install failed" in msg


def test_install_linux_only_manual_targets_never_calls_pip():
    ok, msg = watch_setup._install_linux(["ffmpeg", "ffprobe"])
    assert ok is False
    assert "nothing pip-installable to do" in msg
    assert "sudo" in msg
