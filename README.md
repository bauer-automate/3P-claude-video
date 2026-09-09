# /watch

**Give Claude the ability to watch any video.**

Claude Code (recommended — auto-updates via marketplace):
```
/plugin marketplace add bradautomates/claude-video
/plugin install watch@claude-video
```

Codex, Cursor, Copilot, Gemini CLI, or any of 50+ [Agent Skills](https://agentskills.io) hosts:
```bash
npx skills add bradautomates/claude-video -g
```
(`-g` installs globally for your user, available across all projects. Drop it to scope per-project.)

More install options (claude.ai web, manual) in the [Install](#install) section below.

Zero config to start — `yt-dlp` and `ffmpeg` install on first run via `brew` on macOS (Linux/Windows print exact commands). Captions cover most public videos for free. Whisper is only needed when a video has no captions — bring a Groq/OpenAI key, or point at a local server and keep the audio on your own machine.

---

Claude can read a webpage, run a script, browse a repo. What it can't do, out of the box, is *watch a video*. You paste a YouTube link and it has to either guess from the title or pull a transcript that's missing 90% of what's on screen.

With Claude Video `/watch` you can paste a URL or a local path, ask a question, and Claude fetches captions first, downloads only what it needs, extracts frames (scene-aware, or fast keyframes at `efficient` detail), pulls a timestamped transcript (free captions when available, Whisper as fallback — local server first if you've set one up, then Groq/OpenAI), and `Read`s every frame as an image. By the time it answers, it has *seen* the video and *heard* the audio.

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
```

## What people actually use it for

**Analyze someone else's content.** `/watch https://youtu.be/<viral-video> what hook did they open with?` Claude looks at the first frames, reads the opening transcript, breaks down the structure. Same for ad creative, competitor launches, podcast intros, anything where the *how* matters as much as the *what*.

**Diagnose a bug from a video.** Someone sends you a screen recording of something broken. `/watch bug-repro.mov what's going wrong?` Claude watches the recording, finds the frame where the issue appears, describes what's on screen, often catches the cause without you ever opening the file.

**Summarize a video.** `/watch https://youtu.be/<long-thing> summarize this` does the obvious thing — pulls the structure, the key moments, what was actually said and shown. Faster than watching at 2x.

**Cut the hype out of an update video.** `/watch https://youtu.be/<launch-video> what's actually new — skip the hype` Strip a "game-changer" feature drop down to the few things that matter, so you get the substance without ten minutes of intro and overselling.

**Turn a playlist into notes.** `/watch https://youtu.be/<video> summarize this to a note` Run it across a series and file a per-video summary, so a channel or course becomes a searchable set of notes instead of hours you have to sit through.

## How it works

1. **You paste a video and a question.** URL (anything yt-dlp supports — YouTube, Loom, TikTok, X, Instagram, plus a few hundred more) or a local path (`.mp4`, `.mov`, `.mkv`, `.webm`).
2. **`yt-dlp` checks captions first.** At `transcript` detail, captioned URLs return without downloading video. Otherwise, or when Whisper needs audio, it downloads only what the run needs.
3. **`ffmpeg` extracts frames at the chosen detail.** `efficient` decodes keyframes only (near-instant); `balanced`/`token-burner` prefer scene-change frames and fall back to the duration-aware uniform sampler when they under-produce. JPEGs are 512px wide by default and clamped to 1998px tall for Claude Read compatibility.
4. **The transcript comes from one of two places.** First try: `yt-dlp` pulls native captions (manual or auto-generated) from the source. Free, instant, accurate-ish. Fallback: extract a mono 16 kHz 64 kbps mp3 audio clip (~480 kB/min) and send it to Whisper — a local OpenAI-compatible server (`WATCH_WHISPER_URL`, no key, audio never leaves that machine) if one is configured, otherwise Groq's `whisper-large-v3` (preferred cloud option — cheaper and faster) or OpenAI's `whisper-1`.
5. **Frames + transcript are handed to Claude.** The script prints frame paths with `t=MM:SS` markers and the transcript with timestamps. Claude `Read`s each frame in parallel — JPEGs render directly as images in its context.
6. **Claude answers grounded in what's actually on screen and in the audio.** Not "based on the description" or "according to the title." It saw the frames. It heard the transcript. It answers the way someone who watched the video would.
7. **Cleanup.** The script prints a working directory at the end. If you're not asking follow-ups, Claude removes it.

## Frame budget — why it matters

Token cost is dominated by frames. Every frame is an image; image tokens add up fast. The script's auto-fps logic exists so you don't blow your context budget on a sparse scan of a 30-minute video that would have been better answered by a focused 30-second window.

| Duration | Default frame budget | What you get |
|----------|---------------------|--------------|
| ≤30 s | ~30 frames | Dense — basically every key moment |
| 30 s - 1 min | ~40 frames | Still dense |
| 1 - 3 min | ~60 frames | Comfortable |
| 3 - 10 min | ~80 frames | Sparse but workable |
| > 10 min | 100 frames (capped modes) | "Sparse scan" warning — re-run focused, or `--detail token-burner` for full uncapped coverage |

When the user names a moment ("around 2:30", "the last 30 seconds", "from 0:45 to 1:00"), pass `--start` / `--end`. Focused mode gets denser per-second budgets, capped at 2 fps. Far more useful than a sparse pass over the whole thing.

## Frame deduplication

Frame selection — keyframes (`efficient`), scene-change detection (`balanced`/`token-burner`), or the uniform sampler it falls back to — can still surface near-identical frames: a screen recording that holds one slide for 90 seconds produces a dozen, each billed as a separate image. A dedup pass drops them before frames reach Claude. It runs by default on every frame mode (`--no-dedup` turns it off):

1. One `ffmpeg` call scales each extracted JPEG to a 16×16 grayscale thumbnail. Everything after is pure-stdlib Python — no image libraries.
2. For each frame, compute the **mean absolute difference** against the *last frame that was kept* (average per-pixel brightness change, 0–255 scale).
3. If that difference is at or below the threshold (`2.0`), the frame is a near-duplicate and is dropped. Otherwise it's kept and becomes the new reference.
4. The frame-budget cap applies *after* dedup, so the budget is spent on distinct frames.

Comparing against the last *kept* frame (not the previous one) catches slow fades that never trip a frame-to-frame threshold. The threshold is deliberately low and measures absolute brightness rather than structure, so a one-line code diff, a terminal scrolling a row, or two differently-colored flat slides all survive.

The **Frames** line reports what was collapsed, e.g. `6 selected from 14 candidates (… 8 near-duplicates dropped …)`. On always-moving footage nothing is dropped and you pay what you would have anyway.

## Detail modes — measured

The `--detail` dial trades speed and token cost for visual fidelity. Numbers below are from a real run against a **49:08** YouTube video (1280×720, English auto-captions) — a long, mostly-static screen recording, the case that stresses the caps hardest. Extraction times are local CPU against a pre-downloaded copy; the one-time download was **~37 s** / 76 MB, shared by the three frame modes.

| Mode | Engine | Frames | Cap | Extraction time | Temporal coverage | Est. image tokens |
|------|--------|--------|-----|-----------------|-------------------|-------------------|
| `transcript` | none (captions) | 0 | — | **~4.5 s** (one yt-dlp call, no download) | full (text) | 0 (≈26.6k text tokens) |
| `efficient` | keyframe (`-skip_frame nokey`) | 50 | 50 | **~0.5 s** | 0:00 → 49:04 (full) | **~9.8k** |
| `balanced` | scene-change | 100 | 100 | **~20.9 s** | 0:00 → 48:38 (full) | **~19.7k** |
| `token-burner` | scene-change | 116 | uncapped | **~21.0 s** | 0:00 → 48:38 (full) | **~22.8k** |

- **Image tokens** use Anthropic's `(width × height) / 750` — at the default 512px width these 720p frames are 512×288, **≈197 tokens/frame**; `--resolution 1024` roughly 4×s that. The transcript is surfaced in every captioned mode and on long videos is often the larger cost.
- **One sampling rule across frame modes.** Each detects all candidates across the full range, then even-samples (first + last always kept) down to its cap. The modes differ only in candidate *source* (keyframes vs. scene cuts) and cap, never in how coverage is spread — so the last frame always lands at the end, not partway through.
- **`efficient` is the speed tier** (~0.5 s) — it only reconstructs keyframes, so it's ~40× faster than the scene modes, which decode every frame to find cuts. It can also return *more* frames than `balanced` on low-motion footage (keyframes outnumber scene cuts); "efficient" means fast extraction, not fewer frames.
- **`token-burner` only diverges from `balanced` past the cap.** This clip had 116 cuts, so `balanced` sampled 100 and `token-burner` kept all 116. On high-motion video with hundreds of cuts, `token-burner` keeps everything (and trips the >250-frame token warning) while `balanced` thins to 100.

End-to-end from a cold URL, `transcript` is the cheapest mode by far; the frame modes add the shared ~37 s download on top of the extraction times above.

## Install

| Surface | Install |
|---------|---------|
| **Claude Code** | `/plugin marketplace add bradautomates/claude-video` then `/plugin install watch@claude-video` |
| **Codex, Cursor, Copilot, Gemini CLI, +50 more** | `npx skills add bradautomates/claude-video -g` |
| **claude.ai** (web) | [Download `watch.skill`](https://github.com/bradautomates/claude-video/releases/latest) → Settings → Capabilities → Skills → `+` |
| **Manual / dev** | `git clone` then symlink `skills/watch` into your host's skills dir (see below) |

### Claude Code

```
/plugin marketplace add bradautomates/claude-video
/plugin install watch@claude-video
```

Update later with `/plugin update watch@claude-video`.

### Codex, Cursor, Copilot, Gemini CLI, and 50+ other hosts

The [Agent Skills](https://agentskills.io) CLI installs the skill into whatever agents it detects:

```bash
npx skills add bradautomates/claude-video -g
```

`-g` installs globally for your user (`~/.codex/skills`, `~/.cursor/skills`, etc.); drop it to install into the current project instead. Useful flags:

- `-a, --agent <names…>` — target specific hosts, e.g. `-a codex -a cursor`
- `-l, --list` — list the skills in this repo without installing
- `--copy` — copy files instead of symlinking (for filesystems without symlink support)

The CLI discovers the skill from `skills/watch/SKILL.md` and copies the whole folder — `SKILL.md` plus its `scripts/` runtime — as a self-contained unit. `SKILL.md` resolves its own scripts relative to wherever it was installed, so it works the same on every host.

Update later with `npx skills update watch -g`.

### claude.ai (web)

1. [Download `watch.skill`](https://github.com/bradautomates/claude-video/releases/latest) from the latest release.
2. Go to Settings → Capabilities → Skills.
3. Click `+` and drop the file in.

Enable "Code execution and file creation" under Capabilities first — the skill shells out to `ffmpeg` and `yt-dlp`, so it won't run without it.

### Manual (developer)

Clone the repo and symlink the self-contained skill folder into your host's skills directory — the symlink keeps the install in sync with your working tree as you edit:

```bash
git clone https://github.com/bradautomates/claude-video.git
ln -s "$(pwd)/claude-video/skills/watch" ~/.claude/skills/watch   # or ~/.codex/skills/watch
```

For claude.ai, build the `.skill` bundle from source: `bash skills/watch/scripts/build-skill.sh` produces `dist/watch.skill`.

## First run

On the first `/watch` call, the skill runs `scripts/setup.py --check`. If `ffmpeg` / `yt-dlp` aren't on your PATH, or no Whisper backend is configured, it walks you through fixing it:

- **macOS** — auto-runs `brew install ffmpeg yt-dlp`.
- **Linux** — auto-installs `yt-dlp` via `pipx` (or `pip install --user`, retrying with `--break-system-packages` if the distro's pip refuses otherwise) since that's a user-space install with no sudo; prints the exact `apt`/`dnf` command for `ffmpeg`, which does need sudo, so it's never run automatically.
- **Windows** — prints the `winget` / `pip` commands.
- **Whisper backend** — scaffolds `~/.config/watch/.env` (mode `0600`) with commented placeholders for `GROQ_API_KEY` (preferred cloud key) and `OPENAI_API_KEY`, plus a `WATCH_WHISPER_URL` block for pointing at a local server instead.

After setup, preflight is silent and `/watch` just works. The check is a sub-100ms lookup, so it doesn't slow you down on subsequent runs.

## Bring your own keys

Captions cover the majority of public videos for free. The Whisper fallback only kicks in when a video genuinely has no caption track — typically local files, TikToks, some Vimeos, and the occasional caption-less YouTube upload.

| Capability | What you need | Cost |
|------------|---------------|------|
| Download + native captions | `yt-dlp` + `ffmpeg` | Free |
| Whisper fallback (local, no key) | `WATCH_WHISPER_URL` pointed at your own OpenAI-compatible server, e.g. `http://127.0.0.1:8321` | Free — audio never leaves that machine |
| Whisper fallback (cloud, preferred) | [Groq API key](https://console.groq.com/keys) — `whisper-large-v3` | Cheap, fast |
| Whisper fallback (cloud, alt) | [OpenAI API key](https://platform.openai.com/api-keys) — `whisper-1` | Standard pricing |
| Disable Whisper entirely | `--no-whisper` | Free, frames-only when no captions |

## Usage

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
/watch https://www.tiktok.com/@user/video/123 summarize this
/watch ~/Movies/screen-recording.mp4 when does the UI break?
/watch https://vimeo.com/123 what tools does she mention?
```

Focused on a specific section — denser frame budget, lower token cost:
```
/watch https://youtu.be/abc --start 2:15 --end 2:45
/watch video.mp4 --start 50 --end 60
/watch "$URL" --start 1:12:00            # from 1h12m to end
```

Other knobs (passed to `scripts/watch.py`):

- `--detail transcript|efficient|balanced|token-burner` — fidelity/speed dial. `transcript` skips frames (transcript only); `efficient` uses fast keyframes (cap 50); `balanced` uses scene-aware frames (cap 100); `token-burner` is scene-aware and uncapped.
- `--timestamps T1,T2,…` — grab a frame at each absolute timestamp (`SS`/`MM:SS`/`HH:MM:SS`). Claude reads the transcript first, then targets the moments the presenter flags ("look here", "as you can see"). Added on top of the detail frames (reserved against the cap); out-of-window cues are dropped in focus mode; with `--detail transcript` these become the only frames.
- `--max-frames N` — lower the frame cap for a tighter token budget.
- `--resolution W` — bump frame width to 1024 px when Claude needs to read on-screen text (slides, terminals, code).
- `--fps F` — override the auto-fps calculation (still capped at 2 fps).
- `--whisper groq|openai|local` — force a specific Whisper backend (default: prefer a local server if `WATCH_WHISPER_URL` is set, then Groq, then OpenAI).
- `--no-whisper` — disable transcription entirely; frames only.
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the one before them (held slides, static screen recordings, paused video), so the frame budget is spent on distinct content; this flag turns that off.
- `--out-dir DIR` — keep working files somewhere specific (default: auto-generated tmp dir).
- `--proxy URL` — route yt-dlp through an HTTP/HTTPS/SOCKS proxy (e.g. `socks5://127.0.0.1:1080`). Default: `WATCH_PROXY` in `~/.config/watch/.env`. See [Running in a blocked network](#running-in-a-blocked-network).
- `--cookies PATH` — path to a Netscape-format `cookies.txt` exported from a logged-in YouTube session. Default: `WATCH_COOKIES` in `~/.config/watch/.env`. See [Running in a blocked network](#running-in-a-blocked-network) — this is the first thing to try for a YouTube bot-check.

## Running in a blocked network

Three different things can break a download, and they need different fixes — `/watch` detects each from yt-dlp's own error and tells you which:

**YouTube is bot-checking this IP** (`Sign in to confirm you're not a bot`) — common for sandboxed/cloud agent environments, since YouTube flags whole ranges of datacenter IPs regardless of yt-dlp version or anything this skill does; `youtube.com`/`googlevideo.com` being fully reachable doesn't help, it's IP reputation, not domain access. Try cookies first:

1. Log into YouTube in a private/incognito window, export cookies to a `cookies.txt` file (Netscape format — a browser extension, or see the [yt-dlp wiki](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)), then close the window **without logging out** (logging out invalidates the export).
2. Set `WATCH_COOKIES=/path/to/cookies.txt` in `~/.config/watch/.env`, or pass `--cookies` for a single call.

Cookies help but aren't a guarantee on datacenter IPs — an authenticated session can still get bot-checked from a flagged IP range. For heavy or recurring use, a proxy from a network YouTube doesn't block tends to be more reliable:

```bash
# On the workstation: a local SOCKS5 listener via SSH (or microsocks/tinyproxy)
ssh -N -D 1080 localhost
```

Make it reachable from wherever `/watch` runs (Tailscale, an SSH reverse tunnel, `ngrok`/`cloudflared`), then set:

```
WATCH_PROXY=socks5://127.0.0.1:1080
```

in `~/.config/watch/.env`, or pass `--proxy` for a single call. `/watch` doesn't ship or manage the proxy — point it at infrastructure you already control.

**Your own network is blocking the request** (yt-dlp's error mentions "not in allowlist" / "blocked by policy" / a TLS `CERTIFICATE_VERIFY_FAILED`) — a different problem from the bot-check above; `/watch` detects both of these too:
- An **egress/allowlist denial** means your environment's own network policy needs the reported host added — for YouTube, both the page host and `*.googlevideo.com` (video segments are served from a different host than the page).
- A **TLS certificate error** usually means a TLS-intercepting proxy whose CA is in the OS trust store but not in yt-dlp's bundled `certifi` CA file (`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` don't affect yt-dlp). `/watch` fixes this itself on the first `CERTIFICATE_VERIFY_FAILED` — it merges the OS trust store into `certifi`'s bundle (same as `--merge-ca` below, backing up the original first) and retries once, logging what it did to stderr either way. You only need to run these yourself if that auto-remediation couldn't apply or didn't fix it:
  ```bash
  python3 skills/watch/scripts/setup.py --merge-ca      # merges the OS trust store in; backs up the original first
  python3 skills/watch/scripts/setup.py --restore-ca    # undoes it
  ```

## Limits

- **Long-video accuracy depends on the detail mode.** On the capped modes (`efficient`, default `balanced`) coverage thins out past ~10 minutes — the frame cap spreads across the whole clip, so the script prints a "sparse scan" warning and you're better off re-running focused with `--start`/`--end`. `token-burner` lifts the cap and keeps *every* scene-change frame across the full video, so it stays complete on longer clips at the cost of more image tokens. The 10-minute mark is guidance for the capped modes, not a hard ceiling.
- **Detail is one dial.** Defaults are balanced: scene-aware frames, 2 fps max, 100-frame cap. Use `--detail efficient` for a fast 50-frame keyframe pass, or `--detail token-burner` for uncapped scene candidates. Set `WATCH_DETAIL` in `~/.config/watch/.env` to change the default.

## Structure

```
.
├── skills/watch/                 # self-contained skill — copied as a unit by every installer
│   ├── SKILL.md                  # skill contract — the source of truth across all surfaces
│   └── scripts/
│       ├── watch.py              # entry point — orchestrates download → frames → transcript
│       ├── download.py           # yt-dlp wrapper
│       ├── frames.py             # ffmpeg frame extraction + auto-fps logic
│       ├── transcribe.py         # VTT parsing + dedupe + Whisper orchestration
│       ├── whisper.py            # local server / Groq / OpenAI clients (pure stdlib)
│       ├── config.py             # shared config (~/.config/watch/.env)
│       ├── setup.py              # preflight + installer (+ --merge-ca/--restore-ca)
│       ├── tls_fix.py            # OS trust store ↔ certifi CA bundle merge/restore
│       └── build-skill.sh        # build dist/watch.skill for claude.ai upload (dev-only)
├── hooks/                        # SessionStart status hook (Claude Code only)
├── .claude-plugin/               # plugin.json + marketplace.json (Claude Code)
├── .codex-plugin/                # plugin.json — Codex/agents manifest ("skills": "./skills/")
├── .agents/plugins/              # marketplace.json — Agent Skills marketplace listing
├── AGENTS.md → CLAUDE.md         # generic-agent entry point
├── tests/                        # pytest suite (ffmpeg-synthesized clips, no network)
└── .github/workflows/            # release.yml — auto-builds watch.skill on tag push
```

## Develop

```bash
# Run the test suite (stdlib + pytest; ffmpeg required for frame tests):
python3 -m pytest -q

# Build the claude.ai upload bundle:
bash skills/watch/scripts/build-skill.sh      # → dist/watch.skill
```

Releasing: tag `vX.Y.Z`, push the tag. The workflow builds `dist/watch.skill` and attaches it to the GitHub release. Keep the version in sync across `skills/watch/SKILL.md`, `.claude-plugin/plugin.json`, and `.codex-plugin/plugin.json`.

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Open source

MIT license.

Built on `yt-dlp`, `ffmpeg`, and Claude's multimodal `Read` tool. Whisper transcription via a local OpenAI-compatible server, [Groq](https://groq.com), or [OpenAI](https://openai.com).

Built by Brad Bonanno — I make content about building with AI on [YouTube (@bradbonanno)](https://www.youtube.com/@bradbonanno), and build AI operating systems for businesses at [Solaris Automation](https://www.solarisautomation.io/). If `/watch` saves you from scrubbing through a video, come say hi on the channel.

## Star History

<a href="https://www.star-history.com/?repos=bradautomates%2Fclaude-video&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=bradautomates/claude-video&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=bradautomates/claude-video&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=bradautomates/claude-video&type=date&legend=top-left" />
 </picture>
</a>

---

[github.com/bradautomates/claude-video](https://github.com/bradautomates/claude-video) · [@bradbonanno](https://www.youtube.com/@bradbonanno) · [Solaris Automation](https://www.solarisautomation.io/) · [LICENSE](LICENSE)
