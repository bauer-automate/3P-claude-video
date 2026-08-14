#!/usr/bin/env python3
"""Setup / preflight for /watch.

Modes:
  setup.py --check      Silent preflight. Exit 0 if ready, 2/3/4 on failure.
  setup.py --json       Machine-readable status for Claude to parse.
  setup.py              Installer. Auto-installs deps, scaffolds .env, marks SETUP_COMPLETE.
  setup.py --merge-ca   Merge the OS trust store into yt-dlp's certifi CA bundle
                        (fixes CERTIFICATE_VERIFY_FAILED behind a TLS-intercepting
                        proxy). Opt-in, reversible; backs up the original first.
  setup.py --restore-ca Undo --merge-ca.

Design:
- Silent on success: --check exits 0 with no output when everything's ready so
  that /watch doesn't spam "setup is complete" on every turn.
- Idempotent: re-running the installer is safe — it never clobbers existing
  keys and only appends missing ones.
- SETUP_COMPLETE=true in ~/.config/watch/.env tells us the user has been
  through a successful installer run at least once.
- Never sudo. On macOS, auto-install via brew. Elsewhere, print exact commands.
- Never write an API key to disk automatically — only scaffold placeholders.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from config import get_config  # noqa: E402
import tls_fix  # noqa: E402


REQUIRED_BINARIES = ["ffmpeg", "ffprobe", "yt-dlp"]
CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"
ENV_TEMPLATE = """# /watch API configuration
#
# Whisper transcription fallback — used only when yt-dlp cannot get captions
# (or when you point /watch at a local file with no subtitles).
#
# Groq is preferred: it runs whisper-large-v3 at a fraction of OpenAI's price
# and is faster in practice. OpenAI is the compatible fallback.
#
# Get a Groq key:  https://console.groq.com/keys
# Get an OpenAI key:  https://platform.openai.com/api-keys
#
# Leave both blank to disable Whisper — /watch will still work, but videos
# without native captions will come back frames-only.

GROQ_API_KEY=
OPENAI_API_KEY=

# Default watch behavior (the /watch first-run wizard sets this for you).
# Allowed values: transcript | efficient | balanced | token-burner
# Keep the value on its own line with no trailing comment.
# WATCH_DETAIL=balanced

# Optional proxy for yt-dlp's requests (http://, https://, or socks5://
# [user:pass@]host:port). Leave unset for direct connections.
#
# Some environments — sandboxed/cloud agent environments in particular —
# get blocked straight by YouTube because it blocks whole ranges of
# datacenter IPs, independent of any network policy you control. If that's
# what you're hitting, point this at a proxy running somewhere YouTube
# doesn't block (commonly your own workstation) and route through it:
# WATCH_PROXY=socks5://127.0.0.1:1080

# Optional cookies.txt for yt-dlp's requests (Netscape format, exported from
# a logged-in YouTube session). Leave unset to make unauthenticated requests.
#
# If a video fails with "Sign in to confirm you're not a bot", that's
# YouTube bot-checking this IP (common on sandboxed/datacenter IPs) — not a
# skill or network problem. Export cookies from a private/incognito window
# (then close it without logging out — logging out invalidates the export)
# and point this at the file:
# WATCH_COOKIES=/path/to/cookies.txt
#
# Cookies help but aren't guaranteed on datacenter IPs. For heavy or
# recurring use, WATCH_PROXY pointed at a real workstation IP tends to be
# more reliable than cookies alone.
"""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _check_binaries() -> list[str]:
    return [b for b in REQUIRED_BINARIES if not _which(b)]


_PERM_WARNED: set[str] = set()


def _check_file_permissions(path: Path) -> None:
    """Warn to stderr (once per path per process) if a secrets file is
    world/group readable."""
    key = str(path)
    if key in _PERM_WARNED:
        return
    try:
        mode = path.stat().st_mode
        if mode & 0o044:
            _PERM_WARNED.add(key)
            sys.stderr.write(
                f"[watch] WARNING: {path} is readable by other users. "
                f"Run: chmod 600 {path}\n"
            )
            sys.stderr.flush()
    except OSError:
        pass


def _read_env_key(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if not CONFIG_FILE.exists():
        return None
    _check_file_permissions(CONFIG_FILE)
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() != name:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1]
            return raw or None
    except OSError:
        return None
    return None


def _have_api_key() -> tuple[bool, str | None]:
    if _read_env_key("GROQ_API_KEY"):
        return True, "groq"
    if _read_env_key("OPENAI_API_KEY"):
        return True, "openai"
    return False, None


def is_first_run() -> bool:
    """True if the installer hasn't completed successfully yet."""
    return _read_env_key("SETUP_COMPLETE") != "true"


def _scaffold_env() -> bool:
    """Create ~/.config/watch/.env with placeholders if missing."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return True


def _write_setup_complete() -> None:
    """Idempotently append SETUP_COMPLETE=true to .env.

    Used only after a fully successful install (deps + key). Future sessions
    detect this marker to skip wizard-style UI and stay silent.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = ""
    if CONFIG_FILE.exists():
        existing = CONFIG_FILE.read_text(encoding="utf-8")
        for line in existing.splitlines():
            if line.strip().startswith("SETUP_COMPLETE="):
                return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        CONFIG_FILE.write_text(existing + "SETUP_COMPLETE=true\n", encoding="utf-8")
    else:
        CONFIG_FILE.write_text(ENV_TEMPLATE + "\nSETUP_COMPLETE=true\n", encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _brew_pkg(missing: list[str]) -> list[str]:
    pkgs: list[str] = []
    for bin_name in missing:
        if bin_name in ("ffmpeg", "ffprobe"):
            if "ffmpeg" not in pkgs:
                pkgs.append("ffmpeg")
        elif bin_name == "yt-dlp":
            if "yt-dlp" not in pkgs:
                pkgs.append("yt-dlp")
        else:
            pkgs.append(bin_name)
    return pkgs


def _install_macos(missing: list[str]) -> tuple[bool, str]:
    if _which("brew") is None:
        return False, (
            "Homebrew is not installed. Install it from https://brew.sh, then re-run setup. "
            "Or install manually: `brew install " + " ".join(_brew_pkg(missing)) + "`"
        )
    pkgs = _brew_pkg(missing)
    if not pkgs:
        return True, "nothing to install"
    cmd = ["brew", "install", *pkgs]
    print(f"[setup] running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return False, f"brew install failed with exit code {result.returncode}"
    return True, f"installed via brew: {', '.join(pkgs)}"


def _install_hint_linux(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("apt: `sudo apt install ffmpeg` or dnf: `sudo dnf install ffmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("`pipx install yt-dlp` (recommended) or `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


# Binaries auto-installable on Linux without sudo. ffmpeg/ffprobe need a
# system package manager (apt/dnf), which needs sudo — we never run that
# automatically, so those stay print-only via _install_hint_linux.
_PIP_INSTALLABLE_LINUX = {"yt-dlp"}


def _install_yt_dlp_via_pip() -> tuple[bool, str]:
    """Install yt-dlp without sudo: prefer pipx (isolated), fall back to
    `pip install --user`, retrying with --break-system-packages if the
    system pip refuses on a PEP 668 externally-managed environment."""
    if _which("pipx"):
        cmd = ["pipx", "install", "yt-dlp"]
        print(f"[setup] running: {' '.join(cmd)}", file=sys.stderr)
        if subprocess.run(cmd).returncode == 0:
            return True, "installed yt-dlp via pipx"
        print("[setup] pipx install failed — falling back to pip --user", file=sys.stderr)

    base_cmd = [sys.executable, "-m", "pip", "install", "--user", "yt-dlp"]
    print(f"[setup] running: {' '.join(base_cmd)}", file=sys.stderr)
    result = subprocess.run(base_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, "installed yt-dlp via pip --user"

    if "externally-managed-environment" in (result.stderr or ""):
        retry_cmd = base_cmd + ["--break-system-packages"]
        print(
            "[setup] pip environment is externally managed — retrying with "
            "--break-system-packages",
            file=sys.stderr,
        )
        if subprocess.run(retry_cmd).returncode == 0:
            return True, "installed yt-dlp via pip --user --break-system-packages"

    return False, f"pip install failed with exit code {result.returncode}"


def _install_linux(missing: list[str]) -> tuple[bool, str]:
    """Auto-install what's safely user-space installable (yt-dlp via
    pipx/pip, no sudo). Everything else (ffmpeg/ffprobe) needs a system
    package manager and sudo, so it's never run automatically — the caller
    still gets install instructions for those via _install_hint_linux."""
    pip_targets = [b for b in missing if b in _PIP_INSTALLABLE_LINUX]
    manual_targets = [b for b in missing if b not in _PIP_INSTALLABLE_LINUX]

    msg = "nothing pip-installable to do"
    if pip_targets:
        ok, msg = _install_yt_dlp_via_pip()
        if not ok:
            return False, f"{msg}. Install manually:\n  " + _install_hint_linux(pip_targets)

    if manual_targets:
        return False, (
            f"{msg}; the rest needs a system package manager (requires sudo, "
            "not run automatically):\n  " + _install_hint_linux(manual_targets)
        )
    return True, msg


def _install_hint_windows(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("winget: `winget install Gyan.FFmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("winget: `winget install yt-dlp.yt-dlp` or pip: `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


def _status() -> dict:
    """Structured preflight snapshot.

    `status` describes the *ideal* state (a Whisper key is encouraged), so a
    keyless install still reports `needs_key` on the very first run — that's
    the agent's cue to encourage adding one.

    `can_proceed` is the operational gate: /watch can run as long as the
    binaries are present AND the user has either set a key or already finished
    setup (consciously opting out of Whisper). A keyless user who completed
    setup is NOT nagged on every call.
    """
    missing = _check_binaries()
    has_key, backend = _have_api_key()
    setup_complete = not is_first_run()

    if not missing and has_key:
        status = "ready"
    elif missing and not has_key:
        status = "needs_install_and_key"
    elif missing:
        status = "needs_install"
    else:
        status = "needs_key"

    can_proceed = (not missing) and (has_key or setup_complete)

    cfg = get_config()
    cacert = tls_fix.find_certifi_cacert()
    tls_patched = tls_fix.merge_status(cacert) if cacert else False

    return {
        "status": status,
        "can_proceed": can_proceed,
        "first_run": not setup_complete,
        "setup_complete": setup_complete,
        "missing_binaries": missing,
        "whisper_backend": backend,
        "has_api_key": has_key,
        "config_file": str(CONFIG_FILE),
        "watch_detail": cfg["detail"],
        "proxy_configured": bool(cfg.get("proxy")),
        "cookies_configured": bool(cfg.get("cookies")),
        "tls_patched": tls_patched,
        "platform": platform.system(),
    }


def cmd_check() -> int:
    """Silent-on-success preflight.

    Exit 0 with no output when /watch can run. A keyless user who already
    finished setup (SETUP_COMPLETE=true) counts as ready — Whisper is
    encouraged, not required — so they are never nagged on follow-up calls.

    On a state that blocks /watch, print one actionable line to stderr:
      2 → binaries missing
      3 → genuine first run with no API key (encourage one)
      4 → both missing
    """
    s = _status()
    if s["can_proceed"]:
        return 0

    parts = []
    if s["missing_binaries"]:
        parts.append(f"missing binaries: {', '.join(s['missing_binaries'])}")
    if not s["has_api_key"] and not s["setup_complete"]:
        parts.append("no Whisper API key (GROQ_API_KEY or OPENAI_API_KEY)")
    installer = Path(__file__).resolve()
    sys.stderr.write(
        f"[watch] setup incomplete ({'; '.join(parts)}). "
        f"Run: python3 {installer}\n"
    )
    sys.stderr.flush()

    if s["missing_binaries"] and not s["has_api_key"]:
        return 4
    if s["missing_binaries"]:
        return 2
    return 3


def cmd_json() -> int:
    json.dump(_status(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_install() -> int:
    missing = _check_binaries()
    installed_deps = False
    if missing:
        system = platform.system()
        if system == "Darwin":
            ok, msg = _install_macos(missing)
            print(f"[setup] {msg}", file=sys.stderr)
            if not ok:
                return 2
            still_missing = _check_binaries()
            if still_missing:
                print(f"[setup] still missing after install: {', '.join(still_missing)}", file=sys.stderr)
                return 2
            installed_deps = True
        elif system == "Linux":
            ok, msg = _install_linux(missing)
            print(f"[setup] {msg}", file=sys.stderr)
            if not ok:
                return 2
            still_missing = _check_binaries()
            if still_missing:
                print(f"[setup] still missing after install: {', '.join(still_missing)}", file=sys.stderr)
                return 2
            installed_deps = True
        elif system == "Windows":
            print("[setup] dependencies missing on Windows — please install:", file=sys.stderr)
            print("  " + _install_hint_windows(missing), file=sys.stderr)
            return 2
        else:
            print(f"[setup] unsupported platform ({system}) for auto-install. Install manually:", file=sys.stderr)
            print(f"  missing: {', '.join(missing)}", file=sys.stderr)
            return 2

    created = _scaffold_env()
    if created:
        print(f"[setup] created config: {CONFIG_FILE}")
    else:
        print(f"[setup] config exists: {CONFIG_FILE}")

    has_key, backend = _have_api_key()
    if has_key:
        _write_setup_complete()
        print(f"[setup] ready. whisper backend: {backend}")
        if installed_deps:
            print("[setup] installed dependencies; /watch is fully set up.")
        return 0

    print("")
    print("[setup] one step left: add a Whisper API key.")
    print("")
    print(f"  Edit {CONFIG_FILE} and set either:")
    print("    GROQ_API_KEY=...    (preferred — cheaper, faster; get one at console.groq.com/keys)")
    print("    OPENAI_API_KEY=...  (fallback; get one at platform.openai.com/api-keys)")
    print("")
    print("  Without a key, /watch still works but videos without captions come back frames-only.")
    return 3


def cmd_merge_ca() -> int:
    """Merge the OS trust store into yt-dlp's bundled certifi CA bundle.

    Fixes CERTIFICATE_VERIFY_FAILED under a TLS-intercepting egress proxy
    (yt-dlp ignores SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE and
    always verifies against certifi's own bundle). Opt-in and reversible —
    backs up the original bundle before touching it; see --restore-ca.
    """
    result = tls_fix.merge_system_ca(force="--force" in sys.argv)
    if not result["ok"]:
        print(f"[setup] --merge-ca failed: {result['reason']}", file=sys.stderr)
        return 1
    print(f"[setup] {result['reason']}: {result.get('cacert', '')}")
    if "system_bundle" in result:
        print(f"[setup] merged from: {result['system_bundle']}")
    if "backup" in result:
        installer = Path(__file__).resolve()
        print(f"[setup] original backed up at: {result['backup']}")
        print(f"[setup] undo with: python3 {installer} --restore-ca")
    return 0


def cmd_restore_ca() -> int:
    """Restore certifi's original CA bundle from the --merge-ca backup."""
    result = tls_fix.restore_certifi()
    if not result["ok"]:
        print(f"[setup] --restore-ca failed: {result['reason']}", file=sys.stderr)
        return 1
    print(f"[setup] restored original certifi bundle: {result['cacert']}")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--check":
            return cmd_check()
        if arg == "--json":
            return cmd_json()
        if arg == "--merge-ca":
            return cmd_merge_ca()
        if arg == "--restore-ca":
            return cmd_restore_ca()
    return cmd_install()


if __name__ == "__main__":
    raise SystemExit(main())
