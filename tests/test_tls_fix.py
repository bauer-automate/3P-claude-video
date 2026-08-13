"""Merge/restore of the OS trust store into a fake certifi cacert.pem.

Uses explicit tmp_path files rather than the real certifi install / real
system CA locations, so the test never touches actual trust stores.
"""
from __future__ import annotations

from pathlib import Path

import tls_fix

PRISTINE = "-----BEGIN CERTIFICATE-----\nORIGINAL\n-----END CERTIFICATE-----\n"
SYSTEM_BUNDLE = "-----BEGIN CERTIFICATE-----\nPROXY-CA\n-----END CERTIFICATE-----\n"


def _fake_cacert(tmp_path: Path) -> Path:
    p = tmp_path / "cacert.pem"
    p.write_text(PRISTINE, encoding="utf-8")
    return p


def _fake_system_bundle(tmp_path: Path) -> Path:
    p = tmp_path / "ca-certificates.crt"
    p.write_text(SYSTEM_BUNDLE, encoding="utf-8")
    return p


def test_merge_appends_system_bundle_and_backs_up(tmp_path):
    cacert = _fake_cacert(tmp_path)
    system_bundle = _fake_system_bundle(tmp_path)

    result = tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)

    assert result["ok"] is True
    assert result["reason"] == "merged"
    merged = cacert.read_text(encoding="utf-8")
    assert "ORIGINAL" in merged
    assert "PROXY-CA" in merged
    assert tls_fix.MARKER in merged

    backup = Path(result["backup"])
    assert backup.read_text(encoding="utf-8") == PRISTINE


def test_merge_is_idempotent_without_force(tmp_path):
    cacert = _fake_cacert(tmp_path)
    system_bundle = _fake_system_bundle(tmp_path)

    first = tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)
    second = tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)

    assert second["reason"] == "already merged"
    # Content unchanged between calls — no duplicate appends.
    assert first is not second
    merged = cacert.read_text(encoding="utf-8")
    assert merged.count("PROXY-CA") == 1


def test_merge_with_force_rebuilds_from_pristine_backup_no_duplication(tmp_path):
    cacert = _fake_cacert(tmp_path)
    system_bundle = _fake_system_bundle(tmp_path)

    tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)
    tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle, force=True)

    merged = cacert.read_text(encoding="utf-8")
    assert merged.count("PROXY-CA") == 1
    assert merged.count("ORIGINAL") == 1


def test_restore_reverts_to_pristine_and_removes_backup(tmp_path):
    cacert = _fake_cacert(tmp_path)
    system_bundle = _fake_system_bundle(tmp_path)

    tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)
    result = tls_fix.restore_certifi(cacert=cacert)

    assert result["ok"] is True
    assert cacert.read_text(encoding="utf-8") == PRISTINE
    assert not tls_fix._backup_path(cacert).exists()


def test_restore_without_prior_merge_fails_cleanly(tmp_path):
    cacert = _fake_cacert(tmp_path)
    result = tls_fix.restore_certifi(cacert=cacert)
    assert result["ok"] is False
    assert "no backup found" in result["reason"]


def test_merge_missing_system_bundle_fails_cleanly(tmp_path, monkeypatch):
    cacert = _fake_cacert(tmp_path)
    monkeypatch.setattr(tls_fix, "SYSTEM_CA_CANDIDATES", ())
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    result = tls_fix.merge_system_ca(cacert=cacert)

    assert result["ok"] is False
    assert "no OS trust store found" in result["reason"]


def test_merge_status_false_before_merge(tmp_path):
    cacert = _fake_cacert(tmp_path)
    assert tls_fix.merge_status(cacert) is False


def test_merge_status_true_after_merge(tmp_path):
    cacert = _fake_cacert(tmp_path)
    system_bundle = _fake_system_bundle(tmp_path)
    tls_fix.merge_system_ca(cacert=cacert, system_bundle=system_bundle)
    assert tls_fix.merge_status(cacert) is True


def test_find_system_ca_bundle_prefers_ssl_cert_file_env(tmp_path, monkeypatch):
    custom = tmp_path / "custom-ca.pem"
    custom.write_text(SYSTEM_BUNDLE, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(custom))
    assert tls_fix.find_system_ca_bundle() == custom
