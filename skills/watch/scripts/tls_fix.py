#!/usr/bin/env python3
"""Merge the OS trust store into yt-dlp's bundled certifi CA bundle.

yt-dlp verifies TLS against `certifi.where()` unconditionally — its
networking backend never reads SSL_CERT_FILE / REQUESTS_CA_BUNDLE /
CURL_CA_BUNDLE. Behind a TLS-intercepting egress proxy (common in
sandboxed/enterprise networks), the proxy's CA lands in the OS trust store
but never in certifi's bundle, so every yt-dlp request fails
CERTIFICATE_VERIFY_FAILED even though `curl` succeeds against the same host.

This is opt-in and reversible: `merge_system_ca()` backs up the original
cacert.pem once, then appends the OS trust store's PEM certs.
`restore_certifi()` undoes it.

Invoke via `setup.py --merge-ca` / `setup.py --restore-ca` — this module has
no CLI of its own beyond a thin dev entry point at the bottom.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = "# --- appended by /watch setup.py --merge-ca (OS trust store) ---\n"

# Checked in order; the first that exists on disk wins. Covers the common
# Linux distro families, Alpine/musl, and macOS (system + Homebrew openssl).
SYSTEM_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",    # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",      # RHEL, Fedora, CentOS
    "/etc/ssl/ca-bundle.pem",                # openSUSE
    "/etc/pki/tls/cacert.pem",               # OpenELEC
    "/etc/ssl/cert.pem",                     # Alpine, macOS system
    "/usr/local/etc/openssl/cert.pem",       # macOS Homebrew openssl (Intel)
    "/opt/homebrew/etc/openssl@3/cert.pem",  # macOS Homebrew openssl (Apple Silicon)
)


def find_certifi_cacert() -> Path | None:
    try:
        import certifi
    except ImportError:
        return None
    return Path(certifi.where())


def find_system_ca_bundle() -> Path | None:
    env_path = os.environ.get("SSL_CERT_FILE")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for candidate in SYSTEM_CA_CANDIDATES:
        p = Path(candidate)
        if p.is_file():
            return p
    return None


def _backup_path(cacert: Path) -> Path:
    return cacert.with_name(cacert.name + ".watch-orig")


def merge_status(cacert: Path) -> bool:
    """True if the OS trust store has already been merged into `cacert`."""
    try:
        return MARKER in cacert.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def merge_system_ca(
    *,
    cacert: Path | None = None,
    system_bundle: Path | None = None,
    force: bool = False,
) -> dict:
    """Merge the OS trust store into certifi's cacert.pem.

    Backs up the pristine bundle on first run (never overwritten on
    subsequent calls) and always rebuilds the merged file from that
    pristine backup, so re-running (even with `force`) never duplicates
    certs. Returns a result dict with `ok` and a human-readable `reason`.
    """
    cacert = cacert or find_certifi_cacert()
    if cacert is None:
        return {"ok": False, "reason": "certifi is not installed"}
    if not cacert.is_file():
        return {"ok": False, "reason": f"certifi bundle not found at {cacert}"}

    system_bundle = system_bundle or find_system_ca_bundle()
    if system_bundle is None:
        return {
            "ok": False,
            "reason": "no OS trust store found in known locations "
            "(set SSL_CERT_FILE to point at one and retry)",
        }

    if merge_status(cacert) and not force:
        return {
            "ok": True,
            "reason": "already merged",
            "cacert": str(cacert),
            "system_bundle": str(system_bundle),
        }

    backup = _backup_path(cacert)
    if not backup.exists():
        backup.write_bytes(cacert.read_bytes())

    original = backup.read_text(encoding="utf-8", errors="ignore")
    system_pem = system_bundle.read_text(encoding="utf-8", errors="ignore")
    merged = original.rstrip("\n") + "\n\n" + MARKER + system_pem
    cacert.write_text(merged, encoding="utf-8")

    return {
        "ok": True,
        "reason": "merged",
        "cacert": str(cacert),
        "backup": str(backup),
        "system_bundle": str(system_bundle),
    }


def restore_certifi(cacert: Path | None = None) -> dict:
    """Restore certifi's cacert.pem from the pre-merge backup, if any."""
    cacert = cacert or find_certifi_cacert()
    if cacert is None:
        return {"ok": False, "reason": "certifi is not installed"}
    backup = _backup_path(cacert)
    if not backup.exists():
        return {
            "ok": False,
            "reason": f"no backup found at {backup} (was --merge-ca ever run?)",
        }
    cacert.write_bytes(backup.read_bytes())
    backup.unlink()
    return {"ok": True, "reason": "restored", "cacert": str(cacert)}


if __name__ == "__main__":
    action = restore_certifi if "--restore" in sys.argv else (
        lambda: merge_system_ca(force="--force" in sys.argv)
    )
    outcome = action()
    print(outcome.get("reason", ""), file=sys.stderr)
    for key in ("cacert", "backup", "system_bundle"):
        if key in outcome:
            print(f"  {key}: {outcome[key]}", file=sys.stderr)
    raise SystemExit(0 if outcome.get("ok") else 1)
