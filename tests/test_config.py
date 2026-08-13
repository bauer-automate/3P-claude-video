"""WATCH_DETAIL resolution and frame_cap mapping."""
from __future__ import annotations

import config


def test_default_detail_is_balanced(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_DETAIL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "balanced"


def test_env_overrides_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_DETAIL", "efficient")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "efficient"


def test_invalid_detail_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_DETAIL", "bogus")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "balanced"


def test_get_config_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_DETAIL", raising=False)
    monkeypatch.delenv("WATCH_PROXY", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    cfg = config.get_config()
    assert set(cfg) == {"detail", "proxy", "config_file"}


def test_proxy_defaults_to_none(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_PROXY", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["proxy"] is None


def test_env_overrides_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["proxy"] == "socks5://127.0.0.1:1080"


def test_proxy_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCH_PROXY", raising=False)
    env_file = tmp_path / "watch.env"
    env_file.write_text("WATCH_PROXY=http://127.0.0.1:8080\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", env_file)
    assert config.get_config()["proxy"] == "http://127.0.0.1:8080"


def test_frame_cap_mapping():
    assert config.frame_cap("efficient") == 50
    assert config.frame_cap("balanced") == 100
    assert config.frame_cap("token-burner") is None
    assert config.frame_cap("transcript") is None
    assert config.frame_cap("anything-else") == 100
