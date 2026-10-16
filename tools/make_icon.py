"""Draws the VideoLearn 4Claude icon: a screen with a voice waveform and the red tracking ring around the pointer.

Run: python tools/make_icon.py   -> videolearn4claude/ui/icon.png and icon.ico
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "videolearn4claude" / "ui"
S = 2048                      # draw big, shrink for smooth edges


def rrect_mask(box, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, r, fill=255)
    return m


def gradient(top, bottom):
    g = Image.new("RGB", (S, S))
    px = ImageDraw.Draw(g)
    for y in range(S):
        t = y / (S - 1)
        px.line([(0, y), (S, y)], fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g


# 1. app tile (squircle-ish) with a blue -> indigo gradient and a soft top gloss
tile_box = (96, 96, S - 96, S - 96)
tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bg = gradient((86, 178, 255), (48, 70, 226)).convert("RGBA")
tile.paste(bg, (0, 0), rrect_mask(tile_box, 470))
gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(gloss).ellipse((-300, -900, S + 300, 900), fill=(255, 255, 255, 46))
tile = Image.alpha_composite(tile, Image.composite(gloss, Image.new("RGBA", (S, S)), rrect_mask(tile_box, 470)))
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
shadow.paste((10, 20, 90, 120), (0, 40), rrect_mask(tile_box, 470))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(40)))
img = Image.alpha_composite(img, tile)

# 2. the "screen": white rounded card with a soft shadow
scr = (380, 560, S - 380, 1370)
sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sh.paste((15, 25, 110, 150), (0, 36), rrect_mask(scr, 120))
img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(30)))
card = Image.new("RGBA", (S, S), (0, 0, 0, 0))
card.paste((255, 255, 255, 255), (0, 0), rrect_mask(scr, 120))
img = Image.alpha_composite(img, card)

# 3. voice waveform inside the screen (rounded bars, blue -> violet)
d = ImageDraw.Draw(img)
heights = [150, 280, 430, 270, 520, 340, 420, 230, 140]
bar_w, gap = 74, 52
x0 = S // 2 - (len(heights) * bar_w + (len(heights) - 1) * gap) // 2
cy = (scr[1] + scr[3]) // 2 - 10
for i, h in enumerate(heights):
    t = i / (len(heights) - 1)
    col = (round(40 + 110 * t), round(110 - 40 * t), round(240 - 10 * t), 255)
    x = x0 + i * (bar_w + gap)
    d.rounded_rectangle((x, cy - h // 2, x + bar_w, cy + h // 2), 37, fill=col)

# 4. the red tracking ring + mouse pointer (the tool's signature)
cx, cyy = 1300, 1190
ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
rd = ImageDraw.Draw(ring)
R = 215
rd.ellipse((cx - R, cyy - R, cx + R, cyy + R), outline=(255, 59, 48, 255), width=54)
rsh = ring.filter(ImageFilter.GaussianBlur(14))
img = Image.alpha_composite(img, Image.merge("RGBA", (*Image.new("RGB", (S, S), (60, 0, 0)).split(), rsh.split()[3].point(lambda a: a // 3))))
img = Image.alpha_composite(img, ring)
pts = [(0, 0), (0, 17), (4.6, 13.2), (7.6, 20.4), (10.4, 19.2), (7.4, 12.2), (12.8, 12.2)]
k = 24
arrow = [(cx - 20 + x * k, cyy - 20 + y * k) for x, y in pts]
ar = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ad = ImageDraw.Draw(ar)
ad.polygon(arrow, fill=(255, 255, 255, 255), outline=(255, 255, 255, 255), width=46)
ad.line(arrow + [arrow[0]], fill=(255, 255, 255, 255), width=46, joint="curve")
ad.polygon(arrow, fill=(24, 24, 28, 255))
ash = ar.filter(ImageFilter.GaussianBlur(10))
img = Image.alpha_composite(img, Image.merge("RGBA", (*Image.new("RGB", (S, S), (0, 0, 30)).split(), ash.split()[3].point(lambda a: a // 3))))
img = Image.alpha_composite(img, ar)

full = img.resize((1024, 1024), Image.LANCZOS)
full.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
full.save(OUT / "icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ok", OUT)
