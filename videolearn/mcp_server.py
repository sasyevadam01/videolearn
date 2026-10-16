"""VideoLearn as an MCP server (a "connector" for Claude Desktop / Claude Code).

Gives Claude three tools:
  * watch_video   - transcribe a narrated screen recording and track the mouse (runs locally)
  * read_report   - re-read a report made earlier, without reprocessing
  * show_moments  - look at the frames where the speaker was pointing (image content)

Install with `pip install "videolearn[mcp]"` and run `videolearn-mcp` (stdio).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP, Image

from .core import HOME, process, read_glossary

mcp = FastMCP(
    "VideoLearn",
    instructions=(
        "Use watch_video when the user shares a screen recording with a voice-over (a walkthrough, a tutorial, "
        "a layout explained while moving the mouse). It returns what was said, sentence by sentence, with the "
        "position of the mouse pointer at that moment. Call show_moments to actually look at the frames "
        "(the pointer is circled in red) whenever the words depend on what is being pointed at."
    ),
)

MAX_CHARS = 60_000


def _glossary_prompt() -> str | None:
    p = HOME / "glossary.txt"
    return read_glossary(p.read_text(encoding="utf-8")) if p.exists() else None


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _compact(out: Path) -> str:
    """Report as plain text: one line per spoken sentence, mouse position, uncertain words."""
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    segs = rep["segments"]
    head = (f"Video: {rep['video']} | language: {rep.get('language')} | {len(segs)} segments | "
            f"report folder: {out.as_posix()}\n"
            "Mouse positions are % of the frame (0,0 = top-left). '?' marks words the speech recognizer was unsure of.\n")
    lines = []
    for s in segs:
        c = s.get("cursor")
        mouse = f" | mouse x={c['x_pct']:.0f}% y={c['y_pct']:.0f}%" if c else " | mouse not detected"
        unc = f" | unsure: {', '.join(s['uncertain'])}" if s.get("uncertain") else ""
        lines.append(f"#{s['n']} [{_mmss(s['start'])}-{_mmss(s['end'])}] {s['text']}{mouse}{unc}")
    body = "\n".join(lines)
    if len(head) + len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + f"\n... (cut; the full text is in {out.as_posix()}/REPORT.md)"
    return head + body + "\n\nTip: call show_moments(report_dir, [segment numbers]) to see those frames."


def _report_dir(report_dir: str) -> Path:
    out = Path(report_dir).expanduser().resolve()
    if not (out / "report.json").is_file():
        raise ValueError(f"Not a VideoLearn report folder (no report.json): {out}")
    return out


@mcp.tool()
async def watch_video(video_path: str, ctx: Context, language: str = "auto", quality: str = "precise") -> str:
    """Watch a narrated screen recording: transcribe the voice-over and track the mouse pointer.

    Claude cannot take video or audio as input; this tool recovers both. Everything runs on the
    user's computer (nothing is uploaded). A 3-minute video takes about 25 s on an NVIDIA GPU and
    about 90 s on a CPU; the result is saved next to the video in a "<video name>_report" folder.

    Args:
        video_path: absolute path of the video file on this computer.
        language: speech language code ("en", "it", "es", ...) or "auto" to detect it.
        quality: "precise" (default) or "fast" (about 3x quicker, makes more mistakes).
    """
    loop = asyncio.get_running_loop()

    def progress(frac: float, step: str) -> None:
        asyncio.run_coroutine_threadsafe(ctx.report_progress(frac, 1.0, step), loop)

    report = await asyncio.to_thread(
        process, video_path, None, model="small" if quality == "fast" else "large-v3-turbo",
        language=None if language == "auto" else language, prompt=_glossary_prompt(),
        progress=progress, log=lambda m: None)
    return _compact(report.parent)


@mcp.tool()
def read_report(report_dir: str) -> str:
    """Re-read a report that VideoLearn already made (no reprocessing). Pass the "<video>_report" folder."""
    return _compact(_report_dir(report_dir))


@mcp.tool()
def show_moments(report_dir: str, segments: list[int], view: str = "zoom") -> list:
    """Show the frames of specific moments, with the mouse pointer circled in red.

    Args:
        report_dir: the "<video>_report" folder returned by watch_video.
        segments: segment numbers (the #N at the start of each line), at most 6 per call.
        view: "zoom" (close-up around the pointer) or "full" (whole screen with the pointer circled).
    """
    out = _report_dir(report_dir)
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    by_n = {s["n"]: s for s in rep["segments"]}
    content: list = []
    for n in segments[:6]:
        s = by_n.get(n)
        if not s:
            content.append(f"Segment {n}: not found.")
            continue
        rel = s.get("images", {}).get(view) or s.get("images", {}).get("full")
        path = (out / rel).resolve() if rel else None
        if path is None or not path.is_file() or not path.is_relative_to(out):
            content.append(f"Segment {n}: no image available.")
            continue
        content.append(f"#{n} [{_mmss(s['start'])}-{_mmss(s['end'])}] {s['text']}")
        content.append(Image(path=str(path)))
    return content


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
