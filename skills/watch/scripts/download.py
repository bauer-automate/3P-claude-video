#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# yt-dlp reports both of these as generic extraction failures — pattern-match
# their known phrasing so download failures point at the real cause instead
# of sending the operator on a manual `curl -v` hunt.
_TLS_CERT_MARKERS = (
    "certificate_verify_failed",
    "self-signed certificate in certificate chain",
    "unable to get local issuer certificate",
)
_EGRESS_DENIAL_MARKERS = (
    "not in allowlist",
    "not in the allowlist",
    "blocked by policy",
    "forbidden by proxy",
    "network egress",
    "egress policy",
    "egress gateway",
)


def classify_yt_dlp_failure(output: str) -> str | None:
    """Best-effort classification of a yt-dlp failure from its combined output.

    Returns "tls_cert", "egress_denied", or None (unrecognized — fall back to
    yt-dlp's own generic error).
    """
    lower = output.lower()
    if any(marker in lower for marker in _TLS_CERT_MARKERS):
        return "tls_cert"
    if any(marker in lower for marker in _EGRESS_DENIAL_MARKERS):
        return "egress_denied"
    return None


def _failure_message(kind: str, url: str) -> str:
    if kind == "tls_cert":
        setup_py = Path(__file__).resolve().parent / "setup.py"
        return (
            "yt-dlp's TLS certificate verification failed. This is usually a "
            "TLS-intercepting proxy (common in sandboxed/enterprise networks) whose CA "
            "is in the OS trust store but not in yt-dlp's bundled certifi CA file — "
            "SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE have no effect on yt-dlp's "
            f"networking backend. Run `python3 {setup_py} --merge-ca` to merge your OS "
            "trust store into certifi's bundle (backs up the original first; undo with "
            "`--restore-ca`), then re-run."
        )
    if kind == "egress_denied":
        host = urlparse(url).netloc or url
        return (
            f"This looks like a network/egress policy block, not a video-source problem "
            f"— the request to `{host}` was denied at the network layer. Check your "
            f"environment's outbound allowlist for `{host}`. Note YouTube serves "
            "video/audio segments from `*.googlevideo.com`, a different host than the "
            "page itself, so both need to be allowlisted."
        )
    return "yt-dlp request failed for an unrecognized reason — see the output above."


def _run_yt_dlp(cmd: list[str]) -> tuple[subprocess.CompletedProcess, str]:
    """Run yt-dlp, relaying its output to stderr as before, but capture it so
    callers can classify TLS/network-policy failures instead of surfacing
    yt-dlp's generic extraction error."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (result.stdout or "") + (result.stderr or "")
    if output:
        sys.stderr.write(output)
        if not output.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    return result, output


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def fetch_captions(url: str, out_dir: Path, proxy: str | None = None) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]
    _, output = _run_yt_dlp(cmd)
    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    if subtitle is None and not info:
        kind = classify_yt_dlp_failure(output)
        if kind:
            print(f"[watch] {_failure_message(kind, url)}", file=sys.stderr)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
    proxy: str | None = None,
) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    result, output = _run_yt_dlp(cmd)
    video = _pick_video(out_dir)
    if video is None:
        kind = classify_yt_dlp_failure(output)
        if kind:
            raise SystemExit(_failure_message(kind, url))
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
    proxy: str | None = None,
) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only, proxy=proxy)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
