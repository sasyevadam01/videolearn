from __future__ import annotations

import argparse
import sys

from .core import process, read_glossary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="videolearn",
        description="Turn a narrated screen recording into a Markdown report Claude can read "
                    "(speech transcript + frames + mouse-pointer position, synchronized).")
    p.add_argument("video")
    p.add_argument("-o", "--out", help="output folder (default: <video>_report next to the video)")
    p.add_argument("-m", "--model", default="large-v3-turbo",
                   help="faster-whisper model: tiny, base, small, medium, large-v3, large-v3-turbo "
                        "(default: large-v3-turbo, about as accurate as large-v3 and ~3x faster)")
    p.add_argument("-l", "--language", help="speech language code, e.g. it, en (default: auto-detect)")
    p.add_argument("--engine", default="auto", choices=["auto", "cuda", "vulkan", "cpu"],
                   help="auto = NVIDIA GPU (faster-whisper + CUDA) if its libraries are installed, else an AMD/Intel GPU "
                        "(whisper.cpp + Vulkan) if installed, else the CPU (default). A forced engine that cannot run is an error.")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="old name of --engine (cpu / cuda)")
    p.add_argument("--fps", type=float, default=2.0, help="frames analysed per second (default: 2)")
    p.add_argument("--glossary", help="text file with domain terms (machine names, jargon): "
                                      "helps the transcription spell them correctly")
    p.add_argument("--uncertain", type=float, default=0.5,
                   help="words below this confidence (0-1) are highlighted in EDITOR.html (default: 0.5)")
    p.add_argument("--keep-frames", action="store_true", help="also save all analysed frames")
    a = p.parse_args(argv)
    prompt = None
    if a.glossary:
        with open(a.glossary, encoding="utf-8") as f:
            prompt = read_glossary(f.read())
    try:
        report = process(a.video, a.out, model=a.model, language=a.language, device=a.device, engine=a.engine,
                         fps=a.fps, prompt=prompt, uncertain=a.uncertain, keep_frames=a.keep_frames,
                         log=lambda m: print(m, file=sys.stderr))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
