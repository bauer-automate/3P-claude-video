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
    monkeypatch.delenv("WATCH_COOKIES", raising=False)
    monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
    monkeypatch.delenv("WATCH_WHISPER_TOKEN", raising=False)
    monkeypatch.delenv("WATCH_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    cfg = config.get_config()
    assert set(cfg) == {
        "detail", "proxy", "cookies",
        "whisper_url", "whisper_token", "whisper_model",
        "config_file",
    }


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


def test_cookies_defaults_to_none(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_COOKIES", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["cookies"] is None


def test_env_overrides_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_COOKIES", "/home/user/cookies.txt")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["cookies"] == "/home/user/cookies.txt"


def test_cookies_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCH_COOKIES", raising=False)
    env_file = tmp_path / "watch.env"
    env_file.write_text("WATCH_COOKIES=/home/user/cookies.txt\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", env_file)
    assert config.get_config()["cookies"] == "/home/user/cookies.txt"


def test_whisper_url_defaults_to_none(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_url"] is None


def test_env_overrides_whisper_url(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_url"] == "http://127.0.0.1:8321"


def test_whisper_url_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
    env_file = tmp_path / "watch.env"
    env_file.write_text("WATCH_WHISPER_URL=http://127.0.0.1:8321\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", env_file)
    assert config.get_config()["whisper_url"] == "http://127.0.0.1:8321"


def test_whisper_token_defaults_to_none(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_WHISPER_TOKEN", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_token"] is None


def test_env_overrides_whisper_token(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_WHISPER_TOKEN", "s3cr3t")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_token"] == "s3cr3t"


def test_whisper_model_defaults_to_none(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_model"] is None


def test_env_overrides_whisper_model(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_WHISPER_MODEL", "large-v3")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["whisper_model"] == "large-v3"


def test_frame_cap_mapping():
    assert config.frame_cap("efficient") == 50
    assert config.frame_cap("balanced") == 100
    assert config.frame_cap("token-burner") is None
    assert config.frame_cap("transcript") is None
    assert config.frame_cap("anything-else") == 100
