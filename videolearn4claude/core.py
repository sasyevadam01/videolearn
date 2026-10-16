"""Turn a narrated screen recording into something Claude can read.

Claude cannot take video or audio as input. A screencast with a voice-over
therefore loses its two key channels: *what is said* and *where the mouse
points while it is said*. This module recovers both and pairs them up:

    video -> audio -> speech-to-text (timed words, with confidence)
          -> frames  -> mouse-cursor track
          => REPORT.md (sentence + annotated frame + zoom, per segment)
          => EDITOR.html (uncertain words highlighted, quick correction)
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

LABELS = {
    "en": {
        "title": "Narrated screencast report",
        "intro": (
            "Each section pairs one spoken sentence with the frame at that moment. "
            "The red circle marks the mouse pointer; the second image is a 2x zoom around it. "
            "Coordinates are percentages of the frame (0,0 = top-left). "
            "Transcription is automatic: check names and technical terms."
        ),
        "mouse": "Mouse",
        "zone": "zone",
        "moves": "moves from ({a}) to ({b}) while speaking",
        "none": "mouse not detected in this interval",
        "nospeech": "(no speech)",
        "uncertain": "Uncertain words",
        "v": {"top": "top", "mid": "middle", "bot": "bottom"},
        "h": {"left": "left", "center": "center", "right": "right"},
    },
    "it": {
        "title": "Rapporto del video con voce",
        "intro": (
            "Ogni sezione abbina una frase parlata al fotogramma di quel momento. "
            "Il cerchio rosso indica il puntatore del mouse; la seconda immagine è uno zoom 2x attorno ad esso. "
            "Le coordinate sono percentuali del fotogramma (0,0 = alto-sinistra). "
            "La trascrizione è automatica: controlla nomi e termini tecnici."
        ),
        "mouse": "Mouse",
        "zone": "zona",
        "moves": "si sposta da ({a}) a ({b}) mentre parla",
        "none": "mouse non rilevato in questo intervallo",
        "nospeech": "(nessun parlato)",
        "uncertain": "Parole incerte",
        "v": {"top": "alto", "mid": "medio", "bot": "basso"},
        "h": {"left": "sinistra", "center": "centro", "right": "destra"},
    },
}


def _labels(lang: str | None) -> dict:
    return LABELS.get((lang or "en")[:2], LABELS["en"])


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{p.stderr.strip()}")
    return p.stdout


def _has_audio(video: Path) -> bool:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                "stream=codec_type", "-of", "csv=p=0", str(video)])
    return bool(out.strip())


def extract_audio(video: Path, wav: Path) -> None:
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])


def _best_split(cur: list[dict], min_len: float = 1.5) -> int:
    """Index where to cut an over-long run of words: the longest pause (punctuation helps)."""
    best = None
    for k in range(1, len(cur)):
        if not cur[k]["w"][:1].isspace():       # "c" + "'è": never cut inside a word
            continue
        if cur[k - 1]["end"] - cur[0]["start"] < min_len or cur[-1]["end"] - cur[k]["start"] < 0.5:
            continue
        score = (cur[k]["start"] - cur[k - 1]["end"]) + (0.3 if cur[k - 1]["w"].rstrip().endswith((".", "?", "!", ",")) else 0)
        if best is None or score >= best[0]:
            best = (score, k)
    return best[1] if best else len(cur) - 1


def _group_words(words: list[dict], gap: float = 0.45, max_len: float = 5.0) -> list[dict]:
    """Split the word stream into sentence-sized segments (pauses, punctuation, max length).

    Whisper's own segments can span 30 s, far too coarse to say where the mouse
    was pointing; word timestamps let us cut them where the speaker pauses.
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            length = w["end"] - cur[0]["start"]
            pause = w["start"] - cur[-1]["end"]
            sentence_end = cur[-1]["w"].rstrip().endswith((".", "?", "!"))
            if pause > gap or (sentence_end and length > 2.0):
                groups.append(cur)
                cur = []
            elif length > max_len and len(cur) > 2:
                k = _best_split(cur)
                groups.append(cur[:k])
                cur = cur[k:]
        cur.append(w)
    if cur:
        groups.append(cur)
    return [{"start": g[0]["start"], "end": g[-1]["end"],
             "text": "".join(x["w"] for x in g).strip(), "words": g} for g in groups]


def _glossary_terms(text: str | None) -> dict[str, str]:
    """lower-case term -> the term as the user wrote it (so "Kelvano" is suggested as "Kelvano")."""
    return {t.lower(): t for t in re.findall(r"[^\W\d_]{4,}", text or "")}


def flag_words(segments: list[dict], threshold: float, terms: set[str]) -> None:
    """Mark words worth a human look: low Whisper confidence, or a near-miss of a glossary term.

    A near-miss ("pedale" vs glossary "pedane") is the typical domain-jargon
    error and Whisper is often *confident* about it, so probability alone misses it.
    The glossary term is stored as the suggestion.
    """
    for seg in segments:
        for w in seg["words"]:
            c = _clean(w["w"]).lower()
            w["flag"], w["sug"] = False, None
            if len(c) < 4:
                continue
            if c not in terms:
                best = max(((difflib.SequenceMatcher(None, c, t).ratio(), t) for t in terms),
                           default=(0, ""))
                t = best[1]
                inflection = c[:-1] == t[:-1] or c.startswith(t) or t.startswith(c)   # vowel endings, plurals, -ed/-ing
                # invented machine/product names get misheard in one letter ("Kelvano" -> "Calvano"): be more
                # permissive for longer words, strict for short ones
                if best[0] >= (0.8 if len(c) < 6 else 0.7) and not inflection:
                    w["flag"], w["sug"] = True, terms[best[1]]
                    continue
            if w["p"] < threshold:
                w["flag"] = True


HOME = Path.home() / ".videolearn4claude"      # user data: glossary, history, logs, optional ffmpeg


def _cuda_dirs() -> list[Path]:
    """NVIDIA runtime DLLs (cuBLAS, cuDNN): pip-installed in this environment, or in a dev `cuda_libs` folder."""
    roots = [Path(__file__).resolve().parent.parent / "cuda_libs", Path(sys.prefix) / "Lib" / "site-packages"]
    try:
        import site
        roots += [Path(p) for p in [*site.getsitepackages(), site.getusersitepackages()]]
    except Exception:
        pass
    found: list[Path] = []
    for r in roots:
        found += [p for p in (r / "nvidia").glob("*/bin") if p.is_dir()]
    return list(dict.fromkeys(found))


def ensure_ffmpeg() -> None:
    """Make ffmpeg/ffprobe findable: PATH first, then the copy the installer put next to the app."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    for base in (HOME / "ffmpeg", Path(sys.prefix).parent / "ffmpeg"):
        for cand in [base / "bin", *base.glob("*/bin")]:
            if (cand / "ffmpeg.exe").exists() or (cand / "ffmpeg").exists():
                os.environ["PATH"] = str(cand) + os.pathsep + os.environ["PATH"]
                return


def read_glossary(text: str | None) -> str | None:
    """Glossary file -> Whisper prompt. Lines starting with # are comments."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return " ".join(lines) or None


def gpu_possible() -> bool:
    """True when an NVIDIA GPU is present AND the CUDA runtime libraries can be found."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
    except Exception:
        return False
    return bool(_cuda_dirs()) or shutil.which("cublas64_12.dll") is not None


def _threads() -> int:
    """Physical-core count is the sweet spot for CTranslate2 on CPU (default is only 4)."""
    return max(4, (os.cpu_count() or 8) // 2)


def _run_whisper(wav: Path, model: str, language: str | None, device: str, prompt: str | None, progress):
    from faster_whisper import WhisperModel  # heavy import, only when needed

    kw = ({"device": "cuda", "compute_type": "float16"} if device == "cuda"
          else {"device": "cpu", "compute_type": "int8", "cpu_threads": _threads()})
    try:                                   # model already downloaded: stay fully offline (no Hugging Face call)
        m = WhisperModel(model, local_files_only=True, **kw)
    except Exception:                      # first run: download it
        m = WhisperModel(model, **kw)
    segs, info = m.transcribe(str(wav), language=language, vad_filter=True, beam_size=5,
                              word_timestamps=True, condition_on_previous_text=False,
                              initial_prompt=prompt or None)
    words: list[dict] = []
    total = info.duration or 1.0
    for s in segs:                       # lazy generator: this loop is where the time is spent
        words += [{"w": w.word, "start": round(w.start, 2), "end": round(w.end, 2),
                   "p": round(w.probability, 2)} for w in (s.words or [])]
        if progress:
            progress(min(1.0, s.end / total))
    return words, info.language


def transcribe(wav: Path, model: str, language: str | None, device: str = "auto",
               prompt: str | None = None, progress=None) -> tuple[list[dict], str, str]:
    """Returns (raw timed words, detected language, device used). Slow part: cached by the caller.

    device="auto" uses the NVIDIA GPU when its libraries are available and quietly
    falls back to the CPU otherwise.
    """
    if device == "auto" and gpu_possible():
        for d in _cuda_dirs():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]
        try:
            return (*_run_whisper(wav, model, language, "cuda", prompt, progress), "cuda")
        except Exception:                # missing/incompatible CUDA libs: use the CPU
            pass
    elif device == "cuda":
        return (*_run_whisper(wav, model, language, "cuda", prompt, progress), "cuda")
    return (*_run_whisper(wav, model, language, "cpu", prompt, progress), "cpu")


def extract_frames(video: Path, frames_dir: Path, fps: float) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", f"fps={fps}",
          "-q:v", "2", "-start_number", "0", str(frames_dir / "f_%05d.jpg")])
    return sorted(frames_dir.glob("f_*.jpg"))


def track_cursor(files: list[Path], max_width: int = 640,
                 fps: float = 2.0) -> tuple[list[tuple[float, float] | None], tuple[int, int]]:
    """Find the mouse pointer in every frame.

    The pointer is the only small object that differs from the "page" (median of the frames
    around it), so no cursor template is needed. It works best on mostly-static screens
    (plans, slides, dashboards, documents).

    Two traps are handled:
      * a pointer that rests long enough becomes part of the median; when it moves on, a "ghost"
        is left where it was. The reference window is therefore long (+-20 s), blobs must look like a
        pointer (dark AND light pixels together) and, when a blob sits exactly where the pointer was
        while another one appears elsewhere, the new one wins.
      * a pointer that does not move produces no blob at all: its last position is kept.

    Returns one (x, y) pointer tip per frame (None when unknown) in the coordinates of the
    original frame, plus the frame size.
    """
    with Image.open(files[0]) as first:
        W, H = first.size
    n = len(files)
    if n > 1200:                    # long video: keep memory reasonable
        max_width = min(max_width, 400)
    s = min(1.0, max_width / W)
    size = (round(W * s), round(H * s))

    def load(f: Path) -> np.ndarray:
        with Image.open(f) as im:
            return np.asarray(im.convert("L").resize(size, Image.BILINEAR), dtype=np.uint8)

    gray = np.stack([load(f) for f in files])
    expected = 230 * s * s          # area of a dilated arrow pointer at this scale
    half = max(8, int(20 * fps))    # reference window: +-20 s
    sample = max(1, int(fps))       # one frame per second is plenty for the median
    refresh = max(2, int(2 * fps))  # recompute the reference every ~2 s
    near = 15 * s                   # "same place as before" distance, in downscaled pixels
    track: list[tuple[float, float] | None] = []
    last: tuple[float, float] | None = None     # in downscaled coordinates
    bg = None
    for i in range(n):
        if i % refresh == 0:
            idx = list(range(max(0, i - half), min(n, i + half + 1), sample))
            bg = np.median(gray[idx], axis=0)
        diff = np.abs(gray[i].astype(np.int16) - bg) > 60
        if diff.mean() > 0.15:      # slide/scene change: nothing reliable to say
            track.append(None)
            last = None
            continue
        lab, k = ndimage.label(ndimage.binary_dilation(diff, iterations=2))
        cands = []                  # (x, y, area, pointer-like)
        for j in range(1, k + 1):
            ys, xs = np.where(lab == j)
            if 0.07 * expected <= len(ys) <= 4 * expected:
                region = gray[i][ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                pointer_like = int((region < 70).sum()) >= 2 and int((region > 190).sum()) >= 2
                cands.append((float(xs.min()), float(ys.min()), len(ys), pointer_like))   # tip = top-left of blob
        pool = [c for c in cands if c[3]] or cands
        if pool:
            if last is None:
                pick = max(pool, key=lambda c: c[2])
            else:
                d = lambda c: (c[0] - last[0]) ** 2 + (c[1] - last[1]) ** 2
                far = [c for c in pool if d(c) ** 0.5 > near]
                close = [c for c in pool if d(c) ** 0.5 <= near]
                # a blob right where the pointer was, plus another elsewhere: the first is the ghost
                pick = min(far, key=d) if (far and close) else min(pool, key=d)
            last = (pick[0], pick[1])
        # no candidate: the pointer is probably still (it merged into the reference) -> keep last
        track.append(None if last is None else (last[0] / s, last[1] / s))
    return track, (W, H)


def _zone(x: float, y: float, W: int, H: int, lab: dict) -> str:
    h = lab["h"]["left"] if x < W * 0.38 else (lab["h"]["right"] if x > W * 0.62 else lab["h"]["center"])
    v = lab["v"]["top"] if y < H * 0.33 else (lab["v"]["bot"] if y > H * 0.66 else lab["v"]["mid"])
    return f"{v}-{h}"


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _clean(word: str) -> str:
    """Bare word without punctuation or elision ("l'incollatrice," -> "incollatrice")."""
    return re.split(r"['’]", re.sub(r"[^\w'’-]", "", word.strip()))[-1]


def process(video: str | Path, out_dir: str | Path | None = None, *, model: str = "small",
            language: str | None = None, device: str = "auto", fps: float = 2.0,
            prompt: str | None = None, uncertain: float = 0.5,
            keep_frames: bool = False, log=print, progress=None) -> Path:
    """Run the whole pipeline and return the path of REPORT.md.

    `progress(fraction, step)` is optional; step is one of audio, transcribe, frames, report, done.
    """
    prog = progress or (lambda f, step: None)
    video = Path(video).resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    ensure_ffmpeg()
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not found on PATH (install ffmpeg)")
    out = Path(out_dir) if out_dir else video.with_name(video.stem + "_report")
    out.mkdir(parents=True, exist_ok=True)
    (out / "segments").mkdir(exist_ok=True)

    segments: list[dict] = []
    lang = language
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if _has_audio(video):
            st = video.stat()
            key = hashlib.sha1(f"{st.st_size}|{st.st_mtime_ns}|{model}|{language}|{prompt}".encode()).hexdigest()
            cache = out / "transcript_raw.json"
            cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
            if cached.get("key") == key:
                log("1-2/4 reusing the saved transcription (same video, model, language, glossary)")
                words, lang = cached["words"], cached["language"]
            else:
                log("1/4 extracting audio")
                prog(0.0, "audio")
                extract_audio(video, tmp / "audio.wav")
                log(f"2/4 transcribing (whisper '{model}', device: {device})")
                prog(0.05, "transcribe")
                words, lang, used = transcribe(tmp / "audio.wav", model, language, device, prompt,
                                               progress=lambda fr: prog(0.05 + 0.8 * fr, "transcribe"))
                log(f"    done on {used}")
                cache.write_text(json.dumps({"key": key, "language": lang, "words": words},
                                            ensure_ascii=False), encoding="utf-8")
            segments = _group_words(words)
            flag_words(segments, uncertain, _glossary_terms(prompt))
        else:
            log("1/4 no audio track found, skipping transcription")
        log("3/4 extracting frames and tracking the mouse")
        prog(0.85, "frames")
        files = extract_frames(video, tmp / "frames", fps)
        if not files:
            raise RuntimeError("no frames could be extracted")
        track, (W, H) = track_cursor(files, fps=fps)
        lab = _labels(lang)
        if not segments:  # silent video: one pseudo-segment every 5 s
            dur = len(files) / fps
            segments = [{"start": t, "end": min(t + 5, dur), "text": lab["nospeech"], "words": []}
                        for t in np.arange(0, dur, 5.0).round(2).tolist()]

        log("4/4 building report")
        prog(0.97, "report")
        scale = min(1.0, 1280 / W)
        data = []
        for n, seg in enumerate(segments, 1):
            i0, i1 = int(seg["start"] * fps), int(seg["end"] * fps) + 1
            pts = [p for p in track[i0:i1] if p is not None]
            mid = min(int(round((seg["start"] + seg["end"]) / 2 * fps)), len(files) - 1)
            with Image.open(files[mid]) as src_im:
                img = src_im.convert("RGB")
            entry = {"n": n, **seg, "cursor": None, "tail": ""}
            if pts:
                # the ring must sit where the pointer is in the frame we show (mid-sentence); the median is the fallback
                at_mid = track[mid]
                cx = at_mid[0] if at_mid else statistics.median(p[0] for p in pts)
                cy = at_mid[1] if at_mid else statistics.median(p[1] for p in pts)
                r = max(18, W * 0.05)
                ImageDraw.Draw(img).ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0),
                                            width=max(3, round(W * 0.007)))
                box = (max(0, int(cx - 120)), max(0, int(cy - 120)), min(W, int(cx + 120)), min(H, int(cy + 120)))
                zoom = img.crop(box).resize(((box[2] - box[0]) * 2, (box[3] - box[1]) * 2), Image.LANCZOS)
                zoom.save(out / "segments" / f"{n:02d}_zoom.jpg", quality=88)
                full = img.resize((round(W * scale), round(H * scale)), Image.LANCZOS) if scale < 1 else img
                full.save(out / "segments" / f"{n:02d}_full.jpg", quality=88)
                spread = max(abs(pts[0][0] - pts[-1][0]) / W, abs(pts[0][1] - pts[-1][1]) / H)
                m = f"**{lab['mouse']}**: x={cx / W * 100:.0f}% y={cy / H * 100:.0f}% ({lab['zone']} {_zone(cx, cy, W, H, lab)})"
                if spread > 0.08:
                    a = f"{pts[0][0] / W * 100:.0f}%,{pts[0][1] / H * 100:.0f}%"
                    b = f"{pts[-1][0] / W * 100:.0f}%,{pts[-1][1] / H * 100:.0f}%"
                    m += " — " + lab["moves"].format(a=a, b=b)
                entry["tail"] = f"{m}\n\n![full](segments/{n:02d}_full.jpg) ![zoom](segments/{n:02d}_zoom.jpg)\n"
                entry["cursor"] = {"x_pct": round(cx / W * 100, 1), "y_pct": round(cy / H * 100, 1),
                                   "x": round(cx), "y": round(cy)}
                entry["images"] = {"full": f"segments/{n:02d}_full.jpg", "zoom": f"segments/{n:02d}_zoom.jpg"}
            else:
                img.save(out / "segments" / f"{n:02d}_full.jpg", quality=88)
                entry["tail"] = f"**{lab['mouse']}**: {lab['none']}\n\n![full](segments/{n:02d}_full.jpg)\n"
                entry["images"] = {"full": f"segments/{n:02d}_full.jpg"}
            entry["uncertain"] = [_clean(w["w"]) + (f" (→ {w['sug']}?)" if w.get("sug") else "")
                                  for w in seg["words"] if w.get("flag")]
            data.append(entry)
        if keep_frames:
            shutil.copytree(tmp / "frames", out / "frames", dirs_exist_ok=True)

    pre = f"# {lab['title']}: {video.name}\n\n{lab['intro']}\n"
    lines = [pre]
    for e in data:
        lines.append(f"### [{_mmss(e['start'])}-{_mmss(e['end'])}] {e['text']}\n")
        if e["uncertain"]:
            lines.append(f"**{lab['uncertain']}**: {', '.join(e['uncertain'])}\n")
        lines.append(e["tail"])
    with open(out / "REPORT.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    (out / "report.json").write_text(
        json.dumps({"video": video.name, "video_path": str(video), "language": lang, "frame_size": [W, H],
                    "pre": pre, "labels": {"uncertain": lab["uncertain"]}, "segments": data},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "edits.json").unlink(missing_ok=True)     # a fresh run replaces older manual drafts
    prog(1.0, "done")
    return out / "REPORT.md"
