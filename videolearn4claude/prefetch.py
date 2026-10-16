"""Download the speech-recognition model once, so the first video does not wait for it.

    videolearn-prefetch [model ...]        (default: large-v3-turbo)
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from faster_whisper.utils import download_model

    models = (argv if argv is not None else sys.argv[1:]) or ["large-v3-turbo"]
    for name in models:
        print(f"Downloading speech model '{name}' (one time only, ~1.6 GB for large-v3-turbo)...", flush=True)
        path = download_model(name)
        print(f"  ready: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
