"""Unit tests for the parts that do not need Whisper or ffmpeg. Run: python -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from videolearn4claude import core


def word(w, start, end, p=0.9):
    return {"w": w, "start": start, "end": end, "p": p}


class GroupWordsTest(unittest.TestCase):
    def test_splits_on_a_pause(self):
        words = [word(" hello", 0.0, 0.4), word(" world", 0.45, 0.9), word(" next", 2.0, 2.4), word(" one", 2.45, 2.8)]
        segs = core._group_words(words)
        self.assertEqual([s["text"] for s in segs], ["hello world", "next one"])
        self.assertEqual((segs[1]["start"], segs[1]["end"]), (2.0, 2.8))

    def test_long_speech_is_cut_at_the_longest_pause_not_inside_a_word(self):
        # continuous speech, 12 words of 0.5 s; the biggest gap is after word 6, and "c" + "'è" must stay together
        words, t = [], 0.0
        for i in range(12):
            gap = 0.3 if i == 6 else 0.02
            w = " c" if i == 4 else ("'è" if i == 5 else f" w{i}")
            words.append(word(w, t, t + 0.5))
            t += 0.5 + gap
        segs = core._group_words(words)
        self.assertGreater(len(segs), 1)
        joined = [s["text"] for s in segs]
        self.assertTrue(any("c'è" in s for s in joined), joined)
        self.assertTrue(all(s["end"] - s["start"] <= 6.5 for s in segs))


class GlossaryTest(unittest.TestCase):
    def test_comments_are_ignored(self):
        self.assertEqual(core.read_glossary("# note\nforklift, pallet\n  # more\nSKU"), "forklift, pallet SKU")
        self.assertIsNone(core.read_glossary("# only comments\n"))
        self.assertIsNone(core.read_glossary(None))

    def test_near_miss_gets_a_suggestion_but_inflections_do_not(self):
        terms = core._glossary_terms("Terms: pedane, operatori, imbustatrice")
        seg = {"words": [word(" pedale", 0, 1), word(" operatore", 1, 2), word(" l'imbustatrice", 2, 3),
                         word(" casa", 3, 4), word(" dubbio", 4, 5, p=0.3)]}
        core.flag_words([seg], 0.5, terms)
        flags = {w["w"].strip(): (w["flag"], w["sug"]) for w in seg["words"]}
        self.assertEqual(flags["pedale"], (True, "pedane"))         # typical jargon error
        self.assertEqual(flags["operatore"], (False, None))          # plain inflection of a known term
        self.assertEqual(flags["l'imbustatrice"], (False, None))     # elision handled
        self.assertEqual(flags["casa"], (False, None))
        self.assertEqual(flags["dubbio"], (True, None))              # low confidence, no suggestion

    def test_misheard_invented_name_is_flagged(self):
        terms = core._glossary_terms("Machines: Zorvex, Kelvano")
        seg = {"words": [word(" Calvano", 0, 1), word(" Zorvex", 1, 2), word(" wrapper", 2, 3)]}
        core.flag_words([seg], 0.5, terms)
        flags = {w["w"].strip(): w["sug"] for w in seg["words"]}
        self.assertEqual(flags, {"Calvano": "Kelvano", "Zorvex": None, "wrapper": None})

    def test_english_plurals_and_suffixes_are_not_flagged(self):
        terms = core._glossary_terms("pallet, forklift, wrap")
        seg = {"words": [word(" pallets", 0, 1), word(" forklifts", 1, 2), word(" wrapped", 2, 3), word(" pallat", 3, 4)]}
        core.flag_words([seg], 0.5, terms)
        flags = {w["w"].strip(): w["sug"] for w in seg["words"]}
        self.assertEqual(flags, {"pallets": None, "forklifts": None, "wrapped": None, "pallat": "pallet"})

    def test_clean_removes_punctuation_and_elision(self):
        self.assertEqual(core._clean(" l'incollatrice,"), "incollatrice")


class CursorTrackingTest(unittest.TestCase):
    def test_finds_a_moving_pointer_on_a_static_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            files, truth = [], []
            for i in range(24):
                img = Image.new("RGB", (400, 300), "white")
                d = ImageDraw.Draw(img)
                d.rectangle((40, 40, 120, 90), fill=(200, 220, 255))       # static content
                x, y = 100 + i * 6, 60 + i * 4
                d.polygon([(x, y), (x, y + 17), (x + 5, y + 13), (x + 12, y + 12)], fill="black")
                p = Path(tmp) / f"f_{i:05d}.jpg"
                img.save(p, quality=95)
                files.append(p)
                truth.append((x, y))
            track, size = core.track_cursor(files)
        self.assertEqual(size, (400, 300))
        hits = [(t, g) for t, g in zip(track, truth) if t is not None]
        self.assertGreaterEqual(len(hits), 20)
        for (tx, ty), (gx, gy) in hits:
            self.assertLess(abs(tx - gx), 6)
            self.assertLess(abs(ty - gy), 6)


class RestingPointerTest(unittest.TestCase):
    """A pointer that rests for a long time must not leave a 'ghost' that is mistaken for it later."""

    def test_follows_the_pointer_after_a_long_rest(self):
        a, b = (80, 60), (300, 200)
        with tempfile.TemporaryDirectory() as tmp:
            files, truth = [], []
            for i in range(90):                                   # 45 s at 2 fps: 30 s resting at A, then B
                img = Image.new("RGB", (400, 300), "white")
                d = ImageDraw.Draw(img)
                d.rectangle((150, 100, 260, 150), fill=(200, 220, 255))
                x, y = a if i < 60 else b
                d.polygon([(x, y), (x, y + 17), (x + 5, y + 13), (x + 12, y + 12)], fill="black")
                p = Path(tmp) / f"f_{i:05d}.jpg"
                img.save(p, quality=95)
                files.append(p)
                truth.append((x, y))
            track, _ = core.track_cursor(files, fps=2.0)
        for i in range(62, 90):                                   # after the move it must be at B, not at the ghost of A
            self.assertIsNotNone(track[i], i)
            self.assertLess(abs(track[i][0] - b[0]), 8, f"frame {i}: {track[i]}")
            self.assertLess(abs(track[i][1] - b[1]), 8, f"frame {i}: {track[i]}")


if __name__ == "__main__":
    unittest.main()
