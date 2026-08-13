"""yt-dlp argv construction for download.py.

Regression guard: ``--sub-langs all`` makes yt-dlp fetch YouTube's hundreds of
auto-translated caption tracks, which can take minutes and stalls before the
video download even starts. We only support English, so the request must stay
bounded to the English-only pattern.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import download  # noqa: E402

URL = "https://www.youtube.com/watch?v=rlOpbu3Enkw"


@pytest.fixture(autouse=True)
def _stub_tls_fix_by_default(monkeypatch):
    """Safety net for every test in this file: stub tls_fix.merge_system_ca
    so a TLS-cert-failure test can never accidentally touch the real
    installed certifi bundle. Tests that want to exercise the auto-merge
    retry path override this explicitly with their own monkeypatch.setattr."""
    monkeypatch.setattr(
        download.tls_fix,
        "merge_system_ca",
        lambda: {"ok": False, "reason": "stubbed in tests — no real merge"},
    )


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub subprocess.run inside download.py and record every argv."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    return calls


def _sub_langs(argv: list[str]) -> str:
    idx = argv.index("--sub-langs")
    return argv[idx + 1]


def _assert_english_only(langs: str) -> None:
    tokens = langs.split(",")
    assert "all" not in tokens, f"sub-langs must not request all languages, got {langs!r}"
    assert all(t.startswith("en") for t in tokens), f"sub-langs must be English-only, got {langs!r}"


def test_fetch_captions_requests_english_only(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    download.fetch_captions(URL, tmp_path / "download")
    _assert_english_only(_sub_langs(calls[0]))


def test_download_url_requests_english_only(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    # _pick_video returns None with no real file, which raises SystemExit after
    # the yt-dlp argv is already built — that's all we need to inspect.
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path / "download")
    _assert_english_only(_sub_langs(calls[0]))


def test_download_url_passes_proxy_when_set(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path / "download", proxy="socks5://127.0.0.1:1080")
    argv = calls[0]
    assert "--proxy" in argv
    assert argv[argv.index("--proxy") + 1] == "socks5://127.0.0.1:1080"


def test_download_url_omits_proxy_when_unset(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path / "download")
    assert "--proxy" not in calls[0]


def test_fetch_captions_passes_proxy_when_set(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    download.fetch_captions(URL, tmp_path / "download", proxy="http://127.0.0.1:8080")
    argv = calls[0]
    assert "--proxy" in argv
    assert argv[argv.index("--proxy") + 1] == "http://127.0.0.1:8080"


class _FakeCompletedProcess:
    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_classify_tls_cert_failure():
    output = (
        "WARNING: [youtube] [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
        "failed: self-signed certificate in certificate chain"
    )
    assert download.classify_yt_dlp_failure(output) == "tls_cert"


def test_classify_egress_denial():
    output = (
        "ERROR: [youtube] Host not in allowlist: youtube.com. Add this host to "
        "your network egress settings to allow access."
    )
    assert download.classify_yt_dlp_failure(output) == "egress_denied"


def test_classify_unknown_failure_returns_none():
    assert download.classify_yt_dlp_failure("ERROR: Video unavailable") is None


def test_download_url_surfaces_tls_cert_failure(monkeypatch, tmp_path):
    """Auto-merge is attempted (and, per the autouse stub, fails) before the
    generic TLS message is raised — this is the "remediation didn't apply"
    path, e.g. no OS trust store found in known locations."""
    monkeypatch.setattr(
        download.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(
            stderr="[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain"
        ),
    )
    with pytest.raises(SystemExit, match="TLS certificate verification still failed"):
        download.download_url(URL, tmp_path / "download")


def test_download_url_auto_merges_ca_and_retries_on_tls_failure(monkeypatch, tmp_path):
    """When the auto-merge succeeds, yt-dlp is retried once and the retry's
    output — not the original TLS failure — is what gets classified."""
    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeCompletedProcess(
                stderr="CERTIFICATE_VERIFY_FAILED self-signed certificate in certificate chain"
            )
        return _FakeCompletedProcess(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    monkeypatch.setattr(download.tls_fix, "merge_system_ca", lambda: {"ok": True, "reason": "merged"})

    # No file is ever written to tmp_path/download, so _pick_video still finds
    # nothing on the retry — but reaching the generic "no video file" message
    # (not the TLS one) proves the retry's clean output was classified,
    # confirming the retry actually happened.
    with pytest.raises(SystemExit, match="did not produce a video file"):
        download.download_url(URL, tmp_path / "download")
    assert calls["n"] == 2


def test_download_url_tls_failure_persists_even_when_merge_succeeds(monkeypatch, tmp_path):
    """The merge can succeed while the real problem isn't a trust-store gap
    at all — the retry's (still-failing) output must still be classified and
    surfaced, not silently swallowed because the merge itself reported ok."""
    monkeypatch.setattr(
        download.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(
            stderr="CERTIFICATE_VERIFY_FAILED self-signed certificate in certificate chain"
        ),
    )
    monkeypatch.setattr(download.tls_fix, "merge_system_ca", lambda: {"ok": True, "reason": "merged"})
    with pytest.raises(SystemExit, match="TLS certificate verification still failed"):
        download.download_url(URL, tmp_path / "download")


def test_egress_denial_does_not_trigger_auto_merge(monkeypatch, tmp_path):
    """The auto-merge retry is TLS-specific — an egress/allowlist denial must
    never trigger it."""
    merge_calls = []
    monkeypatch.setattr(
        download.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(stderr="Host not in allowlist: youtube.com."),
    )
    monkeypatch.setattr(
        download.tls_fix,
        "merge_system_ca",
        lambda: merge_calls.append(1) or {"ok": True, "reason": "merged"},
    )
    with pytest.raises(SystemExit, match="network/egress policy block"):
        download.download_url(URL, tmp_path / "download")
    assert merge_calls == []


def test_download_url_surfaces_egress_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        download.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(stderr="ERROR: Host not in allowlist: youtube.com."),
    )
    with pytest.raises(SystemExit, match="network/egress policy block"):
        download.download_url(URL, tmp_path / "download")


def test_download_url_generic_failure_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(
        download.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(stderr="ERROR: Video unavailable"),
    )
    with pytest.raises(SystemExit, match="did not produce a video file"):
        download.download_url(URL, tmp_path / "download")
