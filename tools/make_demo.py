"""Builds docs/demo/demo.mp4: a neutral, invented "workshop floor plan" screen recording.

A synthetic (Windows SAPI) English voice explains the plan while a mouse pointer moves between the
areas. It is the sample used in the README, and a ready-made test video for VideoLearn.

Run (Windows, needs ffmpeg + Pillow):  python tools/make_demo.py
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "demo" / "demo.mp4"
W, H, FPS = 1280, 720, 30
VOICE = "Microsoft David Desktop"

# (sentence, pointer target)
SCRIPT = [
    ("Welcome. This is a quick tour of our demo workshop.", (640, 60)),
    ("Materials arrive here, at the goods in area on the left.", (180, 185)),
    ("Then they go to the cutting station, where the Zorvex three thousand press cuts the parts.", (500, 185)),
    ("Next is the assembly line. This is the busiest part of the floor.", (670, 385)),
    ("After assembly, every item passes the quality check.", (1130, 385)),
    ("Then the Kelvano wrapper closes each pack at the packing area.", (910, 585)),
    ("And finally the pallets leave from the shipping dock, bottom right.", (1160, 585)),
]
BOXES = {  # name: (x0, y0, x1, y1, fill)
    "GOODS IN": (60, 110, 300, 260, "#cfe6ff"),
    "CUTTING": (360, 110, 640, 260, "#ffe2b0"),
    "ASSEMBLY LINE": (360, 320, 980, 450, "#d4f2d4"),
    "QUALITY CHECK": (1040, 320, 1220, 450, "#f1d4f1"),
    "PACKING": (780, 520, 1040, 650, "#fff3b0"),
    "SHIPPING DOCK": (1090, 520, 1230, 650, "#d9d9e8"),
}
ARROWS = [((300, 185), (360, 185)), ((500, 260), (500, 320)), ((980, 385), (1040, 385)),
          ((1130, 450), (1000, 520)), ((1040, 585), (1090, 585))]


def font(size: int, bold: bool = False):
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def base_image() -> Image.Image:
    img = Image.new("RGB", (W, H), "#f4f5f7")
    d = ImageDraw.Draw(img)
    d.text((60, 36), "DEMO WORKSHOP  -  FLOOR PLAN", fill="#1d1d1f", font=font(26, True))
    for a, b in ARROWS:
        d.line([a, b], fill="#8e8e93", width=4)
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        d.polygon([b, (b[0] - 16 * math.cos(ang - .4), b[1] - 16 * math.sin(ang - .4)),
                   (b[0] - 16 * math.cos(ang + .4), b[1] - 16 * math.sin(ang + .4))], fill="#8e8e93")
    for name, (x0, y0, x1, y1, fill) in BOXES.items():
        d.rounded_rectangle((x0, y0, x1, y1), 14, fill=fill, outline="#6e6e73", width=2)
        d.text((x0 + 14, y0 + 10), name, fill="#1d1d1f", font=font(20, True))
    for i, (x, y) in enumerate([(420, 200), (540, 200)]):                       # two operators at the cutting station
        d.ellipse((x - 11, y - 11, x + 11, y + 11), fill="#0071e3")
    for x in range(430, 960, 90):                                                # operators along the assembly line
        d.ellipse((x - 11, 372, x + 11, 394), fill="#0071e3")
    return img


def speak(text: str, path: Path) -> None:
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SelectVoice('{VOICE}'); $s.SetOutputToWaveFile($env:S2C_OUT); $s.Speak($env:S2C_TEXT); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   env={**os.environ, "S2C_OUT": str(path), "S2C_TEXT": text})


def pointer(d: ImageDraw.ImageDraw, x: float, y: float) -> None:
    pts = [(0, 0), (0, 17), (4.6, 13.2), (7.6, 20.4), (10.4, 19.2), (7.4, 12.2), (12.8, 12.2)]
    poly = [(x + px * 1.35, y + py * 1.35) for px, py in pts]
    d.polygon(poly, fill="#ffffff", outline="#000000")
    d.line(poly + [poly[0]], fill="#000000", width=2)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rate, starts, frames_audio, t = None, [], [], 0.6
        silence_lead = 0.6
        pcm = b""
        for i, (text, _) in enumerate(SCRIPT):
            wav = tmp / f"s{i}.wav"
            speak(text, wav)
            with wave.open(str(wav)) as w:
                rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                data = w.readframes(w.getnframes())
            if i == 0:
                pcm += b"\x00" * int(rate * silence_lead) * sw * ch
            starts.append(len(pcm) / (rate * sw * ch))
            pcm += data + b"\x00" * int(rate * 0.7) * sw * ch
        total = len(pcm) / (rate * sw * ch)
        full = tmp / "voice.wav"
        with wave.open(str(full), "wb") as w:
            w.setnchannels(ch); w.setsampwidth(sw); w.setframerate(rate); w.writeframes(pcm)

        base = base_image()
        enc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
             "-i", "-", "-i", str(full), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(OUT)],
            stdin=subprocess.PIPE)
        pos = (640.0, 360.0)
        for f in range(int(total * FPS)):
            ts = f / FPS
            idx = max(i for i, s in enumerate(starts) if s <= ts + 0.5) if ts + 0.5 >= starts[0] else 0
            target = SCRIPT[idx][1]
            k = min(1.0, max(0.0, (ts - (starts[idx] - 0.5)) / 1.0))        # glide during the second before the sentence
            k = k * k * (3 - 2 * k)
            prev = SCRIPT[idx - 1][1] if idx > 0 else (640, 360)
            x = prev[0] + (target[0] - prev[0]) * k + 2.5 * math.sin(ts * 2.1)
            y = prev[1] + (target[1] - prev[1]) * k + 2.5 * math.cos(ts * 1.7)
            frame = base.copy()
            pointer(ImageDraw.Draw(frame), x, y)
            enc.stdin.write(frame.tobytes())
        enc.stdin.close()
        enc.wait()
    print(f"ok: {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {total:.1f} s)")


if __name__ == "__main__":
    main()
