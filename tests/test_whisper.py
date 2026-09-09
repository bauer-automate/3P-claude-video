"""Whisper backends: chunk planning, API-key resolution, and the local server."""
from __future__ import annotations

import contextlib
import http.server
import json
import math
import subprocess
import threading
import time
from pathlib import Path

import pytest

import whisper


@contextlib.contextmanager
def _running_server(handler_cls: type[http.server.BaseHTTPRequestHandler]):
    """Start handler_cls on 127.0.0.1:<ephemeral port> in a background thread.

    Yields the server's base URL (no path). Used to exercise whisper.py's
    local-backend HTTP client against a real socket without any network
    access or a real Whisper server — mirrors what a WATCH_WHISPER_URL
    server looks like from _post_whisper's point of view.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _verbose_json_handler(recorded: list[dict], *, status: int = 200):
    """Build a BaseHTTPRequestHandler that records each POST (headers + path)
    and replies with a canned verbose_json transcription (or `status` if
    non-200, to simulate a server-side failure)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            recorded.append({"headers": dict(self.headers.items()), "path": self.path})
            if status != 200:
                body = json.dumps({"error": "boom"}).encode()
            else:
                body = json.dumps({
                    "task": "transcribe",
                    "language": "en",
                    "duration": 1.2,
                    "text": "hello from the test server",
                    "segments": [
                        {"id": 0, "start": 0.0, "end": 1.2, "text": "hello from the test server"},
                    ],
                }).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # silence request logging
            pass

    return Handler


def _forbidden(*_args, **_kwargs):
    raise AssertionError("should not be called for the local backend (no upload cap)")


def _fake_extract_audio(size_bytes: int):
    """A stand-in for whisper.extract_audio() that writes `size_bytes` of
    filler instead of shelling out to ffmpeg, returning out_path like the
    real function does (Path.write_bytes() returns a byte count, not the
    path, so this can't be a one-line `... or out_path` lambda)."""

    def _extract(video_path: str, out_path: Path) -> Path:
        out_path.write_bytes(b"x" * size_bytes)
        return out_path

    return _extract


@pytest.fixture(autouse=True)
def _no_default_dotenv_files(monkeypatch):
    """Block the fallback to a real ~/.config/watch/.env or ./.env.

    conftest's autouse fixture scrubs the WATCH_WHISPER_* environment
    variables; this covers the other half of _lookup()'s search order so
    _local_token()/_local_model()/_local_timeout() (which don't take a
    dotenv_paths override) and any load_api_key() call made without one stay
    hermetic regardless of what's on the machine running the tests. Tests
    that want dotenv-file behavior pass their own dotenv_paths= explicitly,
    which always wins over this.
    """
    monkeypatch.setattr(whisper, "_default_dotenv_paths", lambda: [])


MB = 1024 * 1024


class TestPlanChunks:
    def test_under_limit_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=600.0, total_bytes=5 * MB, max_bytes=24 * MB)
        assert plan == [(0.0, 600.0)]

    def test_at_limit_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=600.0, total_bytes=24 * MB, max_bytes=24 * MB)
        assert plan == [(0.0, 600.0)]

    def test_over_limit_splits_into_enough_chunks(self):
        # 71 MB against a 24 MB cap → ceil(71/24) = 3 chunks.
        plan = whisper.plan_chunks(total_seconds=3600.0, total_bytes=71 * MB, max_bytes=24 * MB)
        assert len(plan) == 3

    def test_chunks_are_contiguous_and_cover_full_duration(self):
        total = 3600.0
        plan = whisper.plan_chunks(total_seconds=total, total_bytes=71 * MB, max_bytes=24 * MB)
        # Offsets start at 0 and each picks up where the previous ended.
        assert plan[0][0] == 0.0
        for (off, dur), (next_off, _) in zip(plan, plan[1:]):
            assert math.isclose(off + dur, next_off)
        last_off, last_dur = plan[-1]
        assert math.isclose(last_off + last_dur, total)

    def test_each_chunk_estimated_under_limit(self):
        total_seconds, total_bytes, cap = 3600.0, 71 * MB, 24 * MB
        plan = whisper.plan_chunks(total_seconds, total_bytes, cap)
        bytes_per_second = total_bytes / total_seconds
        for _off, dur in plan:
            assert dur * bytes_per_second <= cap

    def test_zero_duration_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=0.0, total_bytes=0, max_bytes=24 * MB)
        assert plan == [(0.0, 0.0)]


class TestShiftSegments:
    def test_adds_offset_to_start_and_end(self):
        segs = [{"start": 0.0, "end": 2.5, "text": "hi"}, {"start": 2.5, "end": 4.0, "text": "there"}]
        shifted = whisper.shift_segments(segs, 1800.0)
        assert shifted == [
            {"start": 1800.0, "end": 1802.5, "text": "hi"},
            {"start": 1802.5, "end": 1804.0, "text": "there"},
        ]

    def test_zero_offset_is_identity(self):
        segs = [{"start": 1.0, "end": 2.0, "text": "x"}]
        assert whisper.shift_segments(segs, 0.0) == segs

    def test_does_not_mutate_input(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "x"}]
        whisper.shift_segments(segs, 10.0)
        assert segs[0]["start"] == 0.0


def _make_mp3(path: Path, seconds: float) -> None:
    """Synthesize a mono 16k 64k mp3 of a sine tone — mirrors extract_audio's format."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-t", str(seconds), "-i", "sine=frequency=440:sample_rate=16000",
            "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1", "-b:a", "64k",
            str(path),
        ],
        check=True,
    )


class TestSplitAudio:
    def test_creates_one_file_per_plan_entry(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        assert len(chunks) == 2
        for chunk_path, _offset in chunks:
            assert chunk_path.exists() and chunk_path.stat().st_size > 0

    def test_returns_plan_offsets(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        assert [offset for _path, offset in chunks] == [0.0, 3.0]

    def test_chunks_are_smaller_than_full(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        full_size = full.stat().st_size
        for chunk_path, _offset in chunks:
            assert chunk_path.stat().st_size < full_size


class TestAudioDuration:
    def test_reads_duration_of_synthesized_clip(self, tmp_path: Path):
        audio = tmp_path / "audio.mp3"
        _make_mp3(audio, 5.0)
        assert whisper.audio_duration(audio) == pytest.approx(5.0, abs=0.5)


class TestTranscribeChunks:
    def test_shifts_and_concatenates_each_chunk(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def fake_transcribe(path: Path) -> list[dict]:
            return [{"start": 0.0, "end": 2.0, "text": path.stem}]

        out = whisper.transcribe_chunks(chunks, fake_transcribe)

        assert out == [
            {"start": 0.0, "end": 2.0, "text": "a"},
            {"start": 100.0, "end": 102.0, "text": "b"},
        ]

    def test_keeps_successful_chunks_when_one_fails(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def flaky(path: Path) -> list[dict]:
            if path.stem == "b":
                raise SystemExit("chunk b failed")
            return [{"start": 1.0, "end": 2.0, "text": "a"}]

        out = whisper.transcribe_chunks(chunks, flaky)

        assert out == [{"start": 1.0, "end": 2.0, "text": "a"}]

    def test_raises_when_every_chunk_fails(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def always_fail(path: Path) -> list[dict]:
            raise SystemExit("boom")

        with pytest.raises(SystemExit):
            whisper.transcribe_chunks(chunks, always_fail)


class TestLocalEndpoint:
    def test_bare_host(self):
        assert whisper.local_endpoint("http://127.0.0.1:8321") == \
            "http://127.0.0.1:8321/v1/audio/transcriptions"

    def test_bare_host_trailing_slash(self):
        assert whisper.local_endpoint("http://127.0.0.1:8321/") == \
            "http://127.0.0.1:8321/v1/audio/transcriptions"

    def test_v1_base(self):
        assert whisper.local_endpoint("http://127.0.0.1:8321/v1") == \
            "http://127.0.0.1:8321/v1/audio/transcriptions"

    def test_v1_base_trailing_slash(self):
        assert whisper.local_endpoint("http://127.0.0.1:8321/v1/") == \
            "http://127.0.0.1:8321/v1/audio/transcriptions"

    def test_full_path_is_unchanged(self):
        url = "http://127.0.0.1:8321/v1/audio/transcriptions"
        assert whisper.local_endpoint(url) == url

    def test_full_path_trailing_slash_is_normalized(self):
        url = "http://127.0.0.1:8321/v1/audio/transcriptions/"
        assert whisper.local_endpoint(url) == "http://127.0.0.1:8321/v1/audio/transcriptions"

    def test_https_host_preserved(self):
        assert whisper.local_endpoint("https://whisper.internal:9000") == \
            "https://whisper.internal:9000/v1/audio/transcriptions"


class TestLoadApiKeyPrecedence:
    """load_api_key()'s "local wins" ordering, exclude=, preferred=, and the
    dotenv_paths test seam — all in-process, no real files or env leakage
    (conftest's autouse fixture scrubs WATCH_WHISPER_* already; GROQ/OPENAI
    are scrubbed explicitly per test since only whisper's own tests touch
    them)."""

    def test_no_candidates_returns_none(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert whisper.load_api_key(dotenv_paths=[]) == (None, None)

    def test_local_wins_over_groq_and_openai(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert whisper.load_api_key(dotenv_paths=[]) == ("local", "http://127.0.0.1:8321")

    def test_groq_wins_over_openai_when_no_local(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert whisper.load_api_key(dotenv_paths=[]) == ("groq", "sk-groq")

    def test_openai_used_when_only_openai_set(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert whisper.load_api_key(dotenv_paths=[]) == ("openai", "sk-openai")

    def test_exclude_local_falls_back_to_groq(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert whisper.load_api_key(exclude="local", dotenv_paths=[]) == ("groq", "sk-groq")

    def test_exclude_local_with_no_cloud_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert whisper.load_api_key(exclude="local", dotenv_paths=[]) == (None, None)

    def test_preferred_local_ignores_cloud_keys(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        assert whisper.load_api_key(preferred="local", dotenv_paths=[]) == \
            ("local", "http://127.0.0.1:8321")

    def test_preferred_local_with_no_url_returns_none_even_with_cloud_key(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        assert whisper.load_api_key(preferred="local", dotenv_paths=[]) == (None, None)

    def test_preferred_groq_ignores_local(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_URL", "http://127.0.0.1:8321")
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq")
        assert whisper.load_api_key(preferred="groq", dotenv_paths=[]) == ("groq", "sk-groq")

    def test_dotenv_paths_override_default_lookup(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_URL", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        env_file = tmp_path / "watch.env"
        env_file.write_text("WATCH_WHISPER_URL=http://127.0.0.1:9000\n", encoding="utf-8")
        assert whisper.load_api_key(dotenv_paths=[env_file]) == ("local", "http://127.0.0.1:9000")

    def test_dotenv_paths_checked_in_order_first_match_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        first = tmp_path / "first.env"
        second = tmp_path / "second.env"
        first.write_text("GROQ_API_KEY=from-first\n", encoding="utf-8")
        second.write_text("GROQ_API_KEY=from-second\n", encoding="utf-8")
        assert whisper.load_api_key(preferred="groq", dotenv_paths=[first, second]) == \
            ("groq", "from-first")

    def test_dotenv_paths_empty_list_disables_file_lookup(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        env_file = tmp_path / "watch.env"
        env_file.write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
        # Passing [] (not None) means "no dotenv files at all", not "use the default paths".
        assert whisper.load_api_key(preferred="groq", dotenv_paths=[]) == (None, None)


class TestLocalHelpers:
    def test_local_model_default(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_MODEL", raising=False)
        assert whisper._local_model() == whisper.LOCAL_MODEL_DEFAULT == "whisper-1"

    def test_local_model_override(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_MODEL", "large-v3")
        assert whisper._local_model() == "large-v3"

    def test_local_timeout_default(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_TIMEOUT", raising=False)
        assert whisper._local_timeout() == whisper.LOCAL_TIMEOUT_DEFAULT == 1800

    def test_local_timeout_override(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_TIMEOUT", "45")
        assert whisper._local_timeout() == 45

    def test_local_timeout_ignores_garbage(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_TIMEOUT", "not-a-number")
        assert whisper._local_timeout() == whisper.LOCAL_TIMEOUT_DEFAULT

    def test_local_token_default_is_none(self, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_TOKEN", raising=False)
        assert whisper._local_token() is None

    def test_local_token_override(self, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_TOKEN", "s3cr3t")
        assert whisper._local_token() == "s3cr3t"


class TestPostWhisperLocalServer:
    """_post_whisper against a throwaway http.server — no network, no ffmpeg."""

    def test_omits_authorization_header_when_key_is_empty(self, tmp_path):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded)) as base_url:
            result = whisper._post_whisper(
                whisper.local_endpoint(base_url), None, "whisper-1", audio,
                attempts=1, timeout=5,
            )

        assert result["text"] == "hello from the test server"
        assert "Authorization" not in recorded[0]["headers"]
        assert recorded[0]["path"] == "/v1/audio/transcriptions"

    def test_omits_authorization_header_when_key_is_empty_string(self, tmp_path):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded)) as base_url:
            whisper._post_whisper(
                whisper.local_endpoint(base_url), "", "whisper-1", audio,
                attempts=1, timeout=5,
            )

        assert "Authorization" not in recorded[0]["headers"]

    def test_sends_bearer_token_when_key_is_set(self, tmp_path):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded)) as base_url:
            whisper._post_whisper(
                whisper.local_endpoint(base_url), "tok123", "whisper-1", audio,
                attempts=1, timeout=5,
            )

        assert recorded[0]["headers"]["Authorization"] == "Bearer tok123"

    def test_attempts_1_is_a_single_attempt_on_connection_refused(self, tmp_path):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")

        start = time.monotonic()
        with pytest.raises(SystemExit, match=r"after 1 attempts"):
            # Port 1 is a privileged, unbound port — connection refused, fast.
            whisper._post_whisper(
                "http://127.0.0.1:1/v1/audio/transcriptions", None, "whisper-1", audio,
                attempts=1, timeout=5,
            )
        # No retry backoff should have been slept through.
        assert time.monotonic() - start < 2.0

    def test_attempts_1_chains_the_connection_error_as_cause(self, tmp_path):
        import urllib.error

        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")

        with pytest.raises(SystemExit) as exc_info:
            whisper._post_whisper(
                "http://127.0.0.1:1/v1/audio/transcriptions", None, "whisper-1", audio,
                attempts=1, timeout=5,
            )
        assert isinstance(
            exc_info.value.__cause__,
            (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError),
        )

    def test_attempts_1_is_a_single_attempt_on_server_error(self, tmp_path):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded, status=500)) as base_url:
            with pytest.raises(SystemExit):
                whisper._post_whisper(
                    whisper.local_endpoint(base_url), None, "whisper-1", audio,
                    attempts=1, timeout=5,
                )

        assert len(recorded) == 1

    def test_default_attempts_still_retries_on_server_error(self, tmp_path, monkeypatch):
        # Sanity check that attempts= only changes behavior when passed —
        # cloud-backend retry behavior (default MAX_ATTEMPTS) is unaffected.
        monkeypatch.setattr(whisper.time, "sleep", lambda *_: None)
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded, status=500)) as base_url:
            with pytest.raises(SystemExit):
                whisper._post_whisper(
                    whisper.local_endpoint(base_url), None, "whisper-1", audio, timeout=5,
                )

        assert len(recorded) == whisper.MAX_ATTEMPTS


class TestTranscribeFileLocal:
    def test_returns_segments_from_local_server(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCH_WHISPER_TOKEN", raising=False)
        monkeypatch.delenv("WATCH_WHISPER_MODEL", raising=False)
        monkeypatch.delenv("WATCH_WHISPER_TIMEOUT", raising=False)
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded)) as base_url:
            # api_key carries the base URL for backend="local" (see
            # load_api_key's docstring) — _transcribe_file resolves it via
            # local_endpoint() internally.
            segments = whisper._transcribe_file("local", base_url, audio)

        assert segments == [{"start": 0.0, "end": 1.2, "text": "hello from the test server"}]
        assert recorded[0]["path"] == "/v1/audio/transcriptions"
        assert "Authorization" not in recorded[0]["headers"]

    def test_uses_configured_token_and_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WATCH_WHISPER_TOKEN", "s3cr3t")
        monkeypatch.setenv("WATCH_WHISPER_MODEL", "large-v3")
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake-audio-bytes")
        recorded: list[dict] = []

        with _running_server(_verbose_json_handler(recorded)) as base_url:
            whisper._transcribe_file("local", base_url, audio)

        assert recorded[0]["headers"]["Authorization"] == "Bearer s3cr3t"
        # model travels in the multipart body, not headers — just confirm no crash
        # and that the request still reached the server with the right content-type.
        assert recorded[0]["headers"]["Content-Type"].startswith("multipart/form-data")


class TestTranscribeVideoLocalSkipsChunking:
    """backend="local" must never touch plan_chunks/split_audio (no upload cap)."""

    def test_local_backend_sends_whole_file_in_one_request(self, tmp_path, monkeypatch):
        monkeypatch.setattr(whisper, "extract_audio", _fake_extract_audio(1024))
        monkeypatch.setattr(whisper, "audio_duration", _forbidden)
        monkeypatch.setattr(whisper, "plan_chunks", _forbidden)
        monkeypatch.setattr(whisper, "split_audio", _forbidden)

        recorded: list[dict] = []
        with _running_server(_verbose_json_handler(recorded)) as base_url:
            segments, backend = whisper.transcribe_video(
                "unused-video-path.mp4", tmp_path / "audio.mp3",
                backend="local", api_key=base_url,
            )

        assert backend == "local"
        assert segments == [{"start": 0.0, "end": 1.2, "text": "hello from the test server"}]
        assert len(recorded) == 1  # one request — never chunked

    def test_local_backend_skips_chunking_even_for_a_huge_file(self, tmp_path, monkeypatch):
        # Bigger than MAX_UPLOAD_BYTES, which would force chunking for groq/openai.
        big = whisper.MAX_UPLOAD_BYTES + 1
        monkeypatch.setattr(whisper, "extract_audio", _fake_extract_audio(big))
        monkeypatch.setattr(whisper, "audio_duration", _forbidden)
        monkeypatch.setattr(whisper, "plan_chunks", _forbidden)
        monkeypatch.setattr(whisper, "split_audio", _forbidden)

        recorded: list[dict] = []
        with _running_server(_verbose_json_handler(recorded)) as base_url:
            segments, backend = whisper.transcribe_video(
                "unused-video-path.mp4", tmp_path / "audio.mp3",
                backend="local", api_key=base_url,
            )

        assert backend == "local"
        assert len(recorded) == 1

    def test_no_whisper_backend_message_mentions_watch_whisper_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(whisper, "load_api_key", lambda *a, **k: (None, None))
        with pytest.raises(SystemExit, match="WATCH_WHISPER_URL"):
            whisper.transcribe_video("unused.mp4", tmp_path / "audio.mp3")
