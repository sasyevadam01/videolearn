# VideoLearn

**An open-source MCP connector for Claude: give Claude ears for your screen recordings.**

> VideoLearn is an independent, open-source (MIT) project and an **MCP connector *for* Claude** (Claude Desktop, Claude Code),
> plus a small desktop app. It is not made by, affiliated with or endorsed by Anthropic.

Claude can't take video or audio as input. If you record your screen while explaining something out loud
("this is the cutting station, then the parts go *here*..."), all Claude can ever get from the file is a mouse pointer
moving around: the voice, and therefore the explanation, is lost.

VideoLearn recovers both channels and **synchronizes them**. For every sentence you said, it gives Claude the
words, the frame at that moment with **the mouse pointer circled in red**, a zoom on the pointer, and where it was
(in % of the screen). Everything runs **on your own computer**: nothing is uploaded anywhere.

<p align="center">
  <img src="docs/img/editor.png" alt="The editor: each sentence with the frame and the pointer circled in red, doubtful words highlighted" width="780">
</p>

## What you get

For the [demo video](docs/demo/demo.mp4) (an invented workshop floor plan, synthetic voice), this is a real excerpt of
the report Claude receives:

```markdown
### [0:10-0:12] Then they go to the cutting station,
**Mouse**: x=39% y=25% (zone top-center) — moves from (21%,25%) to (39%,25%) while speaking
![full](segments/04_full.jpg) ![zoom](segments/04_zoom.jpg)

### [0:27-0:31] pack. Then the Calvano wrapper closes each pack at the packing area.
**Uncertain words**: pack, Calvano (→ Kelvano?)
**Mouse**: x=71% y=81% (zone bottom-right) — moves from (88%,52%) to (70%,80%) while speaking
```

<p align="center">
  <img src="docs/img/moment-full.jpg" alt="Frame at 0:12 with the pointer circled in red on the cutting station" width="520">
</p>

- **Speech to text** with word-level timestamps ([Whisper](https://github.com/openai/whisper) *large-v3-turbo* through
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper)): ~99 languages, runs offline once the model is downloaded.
- **Mouse tracking from the finished video.** No special recorder needed: it works on any recording (phone, Teams, OBS,
  Windows Game Bar...). It finds the pointer by comparing each frame with the "page", so no cursor template is required.
- **A friendly app** (light, macOS-style, English and Italian) to process a video, replay each sentence and fix mistakes.
- **A correction editor.** Words the model was unsure of are highlighted. A **glossary** of your own jargon (machine names,
  acronyms) improves the transcription *and* flags near-misses with a suggestion (`Calvano → Kelvano?`, double-click to apply).
  Everything autosaves into `REPORT.md`.
- **A Claude connector (MCP server)** so Claude Desktop / Claude Code can call it by itself: *"Watch this video and explain
  the process."* See [Use it from Claude](#use-it-from-claude-connector).
- **Fast.** A 3-minute video takes about **25 seconds** on an NVIDIA GPU (details below).

## Install (Windows 10 / 11)

Open **PowerShell** and paste:

```powershell
irm https://raw.githubusercontent.com/sasyevadam01/videolearn/main/install.ps1 | iex
```

Or [download the ZIP](https://github.com/sasyevadam01/videolearn/archive/refs/heads/main.zip), unpack it and
double-click **`Install VideoLearn.bat`**.

The installer is per-user (no administrator rights) and takes a few minutes. It:

1. installs [`uv`](https://github.com/astral-sh/uv) (only if you don't have it), which provides a private Python 3.12 and installs the packages in seconds;
2. installs the app and the connector into `%LOCALAPPDATA%\VideoLearn`;
3. downloads `ffmpeg` (only if you don't have it);
4. **if an NVIDIA graphics card is present**, installs the CUDA libraries (~2 GB) so the GPU is used;
5. downloads the speech model once (~1.6 GB, stored in the Hugging Face cache);
6. creates **Desktop and Start-menu shortcuts**.

Disk space (measured): about 4.5 GB with the NVIDIA libraries, about 2.5 GB without (the speech model alone is ~1.5 GB). Re-run the installer at any time to update.
Options: `-NoGpu`, `-NoMcp`, `-SkipModel`, `-NoShortcuts`, `-InstallDir <path>` (run `install.ps1` directly to pass them).
To remove everything: run `uninstall.ps1` (add `-RemoveUserData` / `-RemoveModels` to also delete your glossary and the downloaded models).

> The installer is a plain, readable PowerShell script: [`install.ps1`](install.ps1). Read it before you run it, as you
> should with any script you pipe into PowerShell.

## Use it

1. Open **VideoLearn** from the Desktop.
2. Click **Choose video…** (or paste a path). A progress bar shows the steps.
3. The editor opens: click **▶** to replay a sentence, jump between doubtful words with **Next to check** (`Ctrl+Enter`),
   click a thumbnail to see the frame large.
4. Click **Copy for Claude** and paste the text into your chat with Claude, or let the [connector](#use-it-from-claude-connector) do it.

The report lives next to your video, in `<video name>_report/` (`REPORT.md`, the `segments/` images, `report.json`).

<p align="center"><img src="docs/img/home.png" alt="Home screen" width="520"></p>

### Command line

```powershell
videolearn "C:\Videos\walkthrough.mp4"            # in the install folder: venv\Scripts\videolearn.exe
videolearn video.mp4 -l en -m small --glossary glossary.txt --fps 4
```

`-m small` is ~3x faster but less accurate; `--fps 4` follows the mouse more finely; `--uncertain 0.6` highlights more words.
If you run the same video again, the saved transcription is reused (a few seconds instead of a full run).

## Use it from Claude (connector)

The installer puts an MCP server in `%LOCALAPPDATA%\VideoLearn\venv\Scripts\videolearn-mcp.exe`. It gives Claude three tools:

| Tool | What it does |
|---|---|
| `watch_video(video_path, language="auto", quality="precise")` | Transcribes the voice-over and tracks the pointer (progress is reported while it runs). Returns one line per sentence with the time, the words and the pointer position. |
| `read_report(report_dir)` | Re-reads a finished report without reprocessing. |
| `show_moments(report_dir, segments, view="zoom")` | Returns the actual **images** of the moments you pick (pointer circled in red), so Claude can look at what you were pointing at. |

**Claude Code**

```powershell
claude mcp add videolearn -- "%LOCALAPPDATA%\VideoLearn\venv\Scripts\videolearn-mcp.exe"
```

**Claude Desktop**: *Settings → Developer → Edit Config*, then add (use your real path, with doubled backslashes):

```json
{
  "mcpServers": {
    "videolearn": { "command": "C:\\Users\\YOU\\AppData\\Local\\VideoLearn\\venv\\Scripts\\videolearn-mcp.exe" }
  }
}
```

Then ask: *"Watch `C:\Videos\walkthrough.mp4` and explain, step by step, what the person is showing."*

Notes: the connector is a **local** tool, so it works where Claude runs on your own computer (Claude Desktop, Claude Code).
It is not a remote "web connector" for claude.ai, and the video must be on the same machine. I tested it end to end with the
official MCP Python client (tool list, progress, images, errors), and Claude Code connects to it (`claude mcp list` shows ✔ Connected).
I have **not** yet run a full conversation through Claude Desktop or Claude Code, so please tell me if something is off.

## How it works

```mermaid
flowchart LR
  V[video] --> A[ffmpeg: audio]
  V --> F[ffmpeg: frames, 2 per second]
  A --> W["Whisper large-v3-turbo<br/>words + timestamps + confidence"]
  W --> S[cut into sentences<br/>at pauses]
  F --> C[pointer tracker]
  S --> R[REPORT.md<br/>sentence + frame + pointer]
  C --> R
  W --> E[editor: doubtful words<br/>+ glossary suggestions]
```

The pointer tracker needs no template: for each frame it takes the difference from the median of the frames around it
(±20 s), keeps small blobs that contain both dark and light pixels (like a cursor), and follows the one that continues the
previous position. It handles a pointer that rests for a long time (it merges into the median and would leave a "ghost" when
it moves) and slide changes. See [`videolearn/core.py`](videolearn/core.py).

## Speed

Measured on an Intel i7-10700K (8 cores) + NVIDIA RTX 3070, for one 2 min 43 s recording (whole pipeline: transcription,
mouse tracking, report). **Your machine will differ.**

| Setup | Time |
|---|---|
| Whisper `large-v3`, CPU, 4 threads (first version) | ~221 s |
| `large-v3-turbo`, CPU, 8 threads | ~89 s |
| `large-v3-turbo`, **NVIDIA GPU (RTX 3070)** | **~25 s** |

On a sample, `large-v3-turbo` matched `large-v3` at 98% of the words (one language, one recording: don't read more into it).
Without an NVIDIA GPU everything still works on the CPU, just slower. AMD / Intel / Apple GPUs are not used.

## Limits (please read)

- **Mouse tracking works best on mostly static screens** (floor plans, slides, dashboards, documents). When most of the
  screen changes (scrolling, video playback inside the video) the pointer may not be found; the report then says
  *"mouse not detected"*. It was tested on two recordings (a CAD floor plan and the demo here). It does **not** detect clicks.
- **Transcription is automatic.** Whisper can be confidently wrong, especially with invented names and jargon: that is what
  the glossary and the editor are for. Very long videos use a lot of memory (use `--fps 1`).
- **Needs Microsoft Edge or Chrome** to show the app window (Edge ships with Windows); otherwise it opens your default browser.
- **Windows only, for now.** The engine is plain Python and should work elsewhere (`pip install ".[mcp]"`), but the installer,
  the shortcuts and the app-window launcher are Windows-specific and untested on macOS / Linux.
- It reads the spoken language you pick (or detects it). The interface is English and Italian; report labels are English and
  Italian (other languages fall back to English).

## Privacy and security

- Transcription, mouse tracking and the app run **locally**. Downloads happen only at install time (uv, Python, packages,
  ffmpeg, NVIDIA libraries, the model from Hugging Face). Once the model is cached, processing makes no network request.
- The app is a small web server bound to `127.0.0.1` only; it rejects requests with a foreign `Host` header and requires a custom
  header for every action. It exits by itself two minutes after you close its window.
- The connector reads the video path Claude asks for and writes the report next to it. Don't give it to a client you don't trust.
- Your data stays in `%USERPROFILE%\.videolearn` (glossary, recent list, error log).

## Develop

```powershell
git clone https://github.com/sasyevadam01/videolearn
cd videolearn
uv venv; uv pip install -e ".[mcp]"          # add ".[gpu]" for NVIDIA libraries
python -m unittest discover -s tests         # unit tests (no Whisper or ffmpeg needed)
python tools/make_demo.py                    # rebuilds docs/demo/demo.mp4 (Windows voice)
```

Layout: `videolearn/core.py` (pipeline), `app.py` + `ui/index.html` (the app), `mcp_server.py` (connector),
`cli.py`, `prefetch.py`, `install.ps1` / `uninstall.ps1`, `tools/` (icon and demo generators), `tests/`.
Issues and pull requests are welcome.

## Credits

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [CTranslate2](https://github.com/OpenNMT/CTranslate2),
[OpenAI Whisper](https://github.com/openai/whisper) (model), [FFmpeg](https://ffmpeg.org), [uv](https://github.com/astral-sh/uv),
[Pillow](https://python-pillow.org), [NumPy](https://numpy.org), [SciPy](https://scipy.org),
and the [Model Context Protocol](https://modelcontextprotocol.io) Python SDK.

## Disclaimer

VideoLearn is an independent, community project. It is **not affiliated with, endorsed by or sponsored by
Anthropic**. "Claude" is a trademark of Anthropic, PBC and is used here only to say which assistant the connector is for.

Previously published as *VideoLearn 4Claude*; the old repository address redirects here.

## License

[MIT](LICENSE) © 2026 sasyevadam01
