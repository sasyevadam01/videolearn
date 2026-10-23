"""Second speech-to-text engine: whisper.cpp with Vulkan (AMD / Intel graphics).

faster-whisper (CTranslate2) only accelerates NVIDIA cards. On a PC with an AMD or Intel GPU,
`whisper-cli` (whisper.cpp built with Vulkan) is faster than the processor, so core.transcribe()
picks it when it is installed and a short probe shows that Vulkan really runs on the GPU.

Why we cut the audio ourselves: whisper.cpp's own `--vad` shifts word times cumulatively, and without it
the first words after a long silence are dated back to the end of the previous sentence (measured: up to
26 s too early). So the speech is found here (same Silero VAD that faster-whisper uses), the audio is split
at long silences, whisper-cli transcribes each piece as its own file (one process, the model is loaded
once) and the start of the piece is added to every time of that piece.

Files live in ~/.videolearn/whispercpp (or $VIDEOLEARN_WHISPERCPP_DIR):
    whisper-cli.exe (+ its DLLs)
    models/ggml-large-v3-turbo.bin  (or ggml-large-v3-turbo-q5_0.bin, ggml-small.bin, ...)
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
CUT_GAP = 5.0          # silences at least this long (s) split the audio into separate pieces
PAD = 0.4              # margin (s) around the speech of a piece
PROMPT_MAX_CHARS = 800  # whisper.cpp keeps only the last n_text_ctx/2 tokens of the prompt anyway


class EngineError(RuntimeError):
    """The engine cannot run here; the message is the reason (it goes to the log)."""


def whispercpp_dir() -> Path:
    env = os.environ.get("VIDEOLEARN_WHISPERCPP_DIR")
    return Path(env) if env else Path.home() / ".videolearn" / "whispercpp"


# ---------- what is installed ----------
def classify_gpu(name: str) -> str | None:
    """'nvidia' | 'amd' | 'intel' for a real graphics card name, None for virtual/basic adapters."""
    n = name.lower()
    if any(x in n for x in ("remote display", "basic render", "basic display", "virtual", "vmware", "parsec", "indirect")):
        return None
    if "nvidia" in n or "geforce" in n or "quadro" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n or "advanced micro" in n:
        return "amd"
    if "intel" in n:
        return "intel"
    return None


@functools.lru_cache(maxsize=1)
def gpu_vendors() -> tuple[str, ...]:
    """Vendors of the graphics adapters Windows reports (empty when it cannot be read)."""
    if os.name != "nt":
        return ()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
            capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    found = [classify_gpu(ln.strip()) for ln in out.splitlines() if ln.strip()]
    return tuple(dict.fromkeys(v for v in found if v))


def model_file(model: str) -> Path | None:
    """ggml file for a faster-whisper style model name ("large-v3-turbo", "small", ...)."""
    d = whispercpp_dir() / "models"
    names = [f"ggml-{model}.bin", f"ggml-{model}-q5_0.bin"] if model != "large-v3-turbo" else \
        ["ggml-large-v3-turbo.bin", "ggml-large-v3-turbo-q8_0.bin", "ggml-large-v3-turbo-q5_0.bin"]
    return next((d / n for n in names if (d / n).is_file()), None)


def cli_path() -> Path | None:
    p = whispercpp_dir() / "whisper-cli.exe"
    return p if p.is_file() else None


def vulkan_candidate(model: str = "large-v3-turbo") -> bool:
    """Cheap check (no process started): an AMD/Intel GPU, whisper-cli and the model are all present."""
    return bool(cli_path() and model_file(model) and set(gpu_vendors()) & {"amd", "intel"})


# ---------- running whisper-cli ----------
def _tail(path: Path, n: int = 600) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except OSError:
        return ""


def _run(cmd: list[str], out_base: Path, timeout: float, progress=None) -> tuple[int, str]:
    """Run a command with stdout/stderr going to files (pipes that nobody reads freeze whisper-cli).

    `progress(fraction_of_current_file, file_index)` is called as whisper-cli reports it.
    """
    err_path = out_base.with_suffix(".stderr")
    with open(out_base.with_suffix(".stdout"), "wb") as so, open(err_path, "wb") as se:
        proc = subprocess.Popen(cmd, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == "nt" else 0)
        t0, last, k = time.time(), -1, 0
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.time() - t0 > timeout:
                proc.kill()
                proc.wait()
                raise EngineError(f"whisper-cli did not finish within {timeout:.0f} s (killed)")
            if progress:
                m = re.findall(r"progress =\s*(\d+)%", _tail(err_path, 400))
                if m and int(m[-1]) != last:
                    if int(m[-1]) < last - 30:       # the percentage started again: next file
                        k += 1
                    last = int(m[-1])
                    progress(last / 100, k)
    return proc.returncode, err_path.read_text(encoding="utf-8", errors="replace")


_probe_failed: dict[str, str] = {}      # key -> reason, for the life of this process (a server must not retry on every video)


def _probe_key(cli: Path, mf: Path) -> str:
    return f"{cli.stat().st_size}|{cli.stat().st_mtime_ns}|{mf.name}|{mf.stat().st_size}|{','.join(gpu_vendors())}"


def probe_vulkan(model: str, timeout: float = 90.0) -> None:
    """Short run of whisper-cli on 2 s of audio: raises EngineError unless Vulkan really runs on a GPU.

    A success is remembered in ~/.videolearn/vulkan_probe.json for the same whisper-cli, model and GPU, so the
    probe (about 2 s) runs once, not for every video; a failure is remembered until the process ends.
    """
    cli, mf = cli_path(), model_file(model)
    if not cli:
        raise EngineError(f"whisper-cli.exe not found in {whispercpp_dir()}")
    if not mf:
        raise EngineError(f"no ggml model for '{model}' in {whispercpp_dir() / 'models'}")
    key = _probe_key(cli, mf)
    if key in _probe_failed:
        raise EngineError(_probe_failed[key])
    cache = Path.home() / ".videolearn" / "vulkan_probe.json"
    try:
        if json.loads(cache.read_text(encoding="utf-8")).get("ok") == key:
            return
    except (OSError, ValueError):
        pass
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "probe.wav"
        t = np.arange(SR * 2) / SR
        write_wav(wav, (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        try:
            rc, err = _run([str(cli), "-m", str(mf), "-f", str(wav), "-l", "en", "-nt", "-t", "4"],
                           Path(tmp) / "probe", timeout)
            check_probe_output(rc, err)
        except EngineError as e:
            _probe_failed[key] = str(e)
            raise
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ok": key}), encoding="utf-8")
    except OSError:
        pass


def check_probe_output(rc: int, err: str) -> None:
    if rc != 0:
        raise EngineError(f"whisper-cli probe failed (exit code {rc}): {err[-300:].strip()}")
    dev = re.search(r"ggml_vulkan: 0 = (.+?) \|", err)
    if "ggml_vulkan: Found" not in err or not dev:
        raise EngineError("whisper-cli found no Vulkan device (is the graphics driver up to date?)")
    if re.search(r"llvmpipe|swiftshader|software", dev.group(1), re.I):
        raise EngineError(f"the only Vulkan device is a software renderer ({dev.group(1)})")
    if not re.search(r"using Vulkan\d* backend", err):
        raise EngineError("whisper-cli started but did not use the Vulkan backend (it would run on the processor)")


# ---------- audio helpers ----------
def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise EngineError("expected 16 kHz mono 16-bit audio")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def speech_pieces(chunks: list[tuple[float, float]], total: float, cut_gap: float = CUT_GAP,
                  pad: float = PAD) -> list[tuple[float, float]]:
    """Speech chunks (s) -> pieces to transcribe: chunks closer than `cut_gap` are joined, each piece padded.

    Silences shorter than `cut_gap` stay inside the audio, so only the long ones are removed.
    """
    pieces: list[list[float]] = []
    for s, e in sorted(chunks):
        if pieces and s - pieces[-1][1] < cut_gap:
            pieces[-1][1] = max(pieces[-1][1], e)
        else:
            pieces.append([s, e])
    out: list[tuple[float, float]] = []
    for s, e in pieces:
        s, e = max(0.0, s - pad), min(total, e + pad)
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def find_speech(audio: np.ndarray) -> list[tuple[float, float]]:
    """Speech chunks (s) found with the same Silero VAD (and options) that faster-whisper uses."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    ts = get_speech_timestamps(audio, VadOptions())
    return [(c["start"] / SR, c["end"] / SR) for c in ts]


# ---------- reading the result ----------
def words_from_json(data: dict, offset: float = 0.0) -> tuple[list[dict], str]:
    """whisper-cli `-ojf` output -> [{w, start, end, p}] like faster-whisper's words (times + `offset`).

    A word starts at a token that begins with a space; its confidence `p` is the mean probability of
    its tokens, which is how faster-whisper defines a word's probability, so `--uncertain` means the same.
    """
    words: list[dict] = []
    for seg in data.get("transcription", []):
        for tok in seg.get("tokens", []):
            text = tok.get("text", "")
            if text.startswith(("[_", "<|")):
                continue
            s, e = tok["offsets"]["from"] / 1000, tok["offsets"]["to"] / 1000
            if text[:1].isspace() or not words:
                words.append({"w": text, "start": s, "end": e, "ps": [tok["p"]]})
            else:
                w = words[-1]
                w["w"] += text
                w["end"] = e
                w["ps"].append(tok["p"])
    out = [{"w": w["w"], "start": round(w["start"] + offset, 2), "end": round(max(w["end"], w["start"]) + offset, 2),
            "p": round(sum(w["ps"]) / len(w["ps"]), 2)} for w in words if w["w"].strip()]
    return out, data.get("result", {}).get("language", "")


def transcribe_whispercpp(wav: Path, model: str, language: str | None, prompt: str | None,
                          progress=None, log=None, threads: int = 8) -> tuple[list[dict], str]:
    """Words of `wav` (16 kHz mono) with whisper-cli + Vulkan. Raises EngineError when it cannot."""
    cli, mf = cli_path(), model_file(model)
    if not cli or not mf:
        raise EngineError(f"whisper-cli or the ggml model for '{model}' is not installed in {whispercpp_dir()}")
    audio = read_wav(wav)
    total = len(audio) / SR
    pieces = speech_pieces(find_speech(audio), total)
    if not pieces:
        return [], language or ""
    if log and len(pieces) > 1:
        log(f"    {len(pieces)} speech pieces, {total - sum(e - s for s, e in pieces):.0f} s of silence not sent to the engine")
    work = sum(e - s for s, e in pieces)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cmd = [str(cli), "-m", str(mf), "-l", language or "auto", "-ojf", "-t", str(threads), "-pp"]
        if prompt:
            cmd += ["--prompt", prompt[-PROMPT_MAX_CHARS:]]
        for k, (s, e) in enumerate(pieces):
            write_wav(tmp / f"p{k}.wav", audio[int(s * SR):int(e * SR)])
            cmd += ["-f", str(tmp / f"p{k}.wav"), "-of", str(tmp / f"p{k}")]

        def on_progress(frac: float, k: int) -> None:
            if progress:
                progress(min(1.0, (sum(e - s for s, e in pieces[:k]) + frac * (pieces[k][1] - pieces[k][0])) / work))

        rc, err = _run(cmd, tmp / "run", timeout=60 + 1.0 * work, progress=on_progress if progress else None)
        if rc != 0:
            raise EngineError(f"whisper-cli failed (exit code {rc}): {err[-300:].strip()}")
        if not re.search(r"using Vulkan\d* backend", err):
            raise EngineError("whisper-cli did not use the Vulkan backend")
        words: list[dict] = []
        lang = language or ""
        for k, (s, _) in enumerate(pieces):
            out_json = tmp / f"p{k}.json"
            if not out_json.is_file():
                raise EngineError(f"whisper-cli wrote no result for piece {k + 1} of {len(pieces)}")
            w, lg = words_from_json(json.loads(out_json.read_bytes().decode("utf-8", errors="replace")), offset=s)
            words += w
            lang = lang or lg
    return words, lang
