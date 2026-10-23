"""Unit tests for the engine choice and the whisper.cpp glue. They need no model, GPU or whisper-cli.
Run: python -m unittest discover -s tests"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from videolearn import cli, core, engines


def tok(text, a, b, p=0.9):
    return {"text": text, "offsets": {"from": int(a * 1000), "to": int(b * 1000)}, "p": p}


def result(*tokens, lang="it"):
    return {"result": {"language": lang}, "transcription": [{"tokens": list(tokens)}]}


class GpuNameTest(unittest.TestCase):
    def test_vendors(self):
        c = engines.classify_gpu
        self.assertEqual(c("NVIDIA GeForce RTX 4060 Laptop GPU"), "nvidia")
        self.assertEqual(c("AMD Radeon 780M Graphics"), "amd")
        self.assertEqual(c("Intel(R) Iris(R) Xe Graphics"), "intel")
        self.assertIsNone(c("Microsoft Remote Display Adapter"))
        self.assertIsNone(c("Microsoft Basic Render Driver"))


class WordsFromJsonTest(unittest.TestCase):
    def test_tokens_become_words_and_confidence_is_the_mean_of_the_tokens(self):
        data = result(tok("[_BEG_]", 0, 0), tok(" Tra", 0.05, 0.13, 0.9), tok(" l", 0.13, 0.17, 0.8), tok("'", 0.17, 0.18, 0.6),
                      tok("altro", 0.22, 0.37, 0.7), tok(" ok", 0.5, 0.6, 0.4), tok("[_TT_300]", 0.6, 0.6))
        words, lang = engines.words_from_json(data)
        self.assertEqual(lang, "it")
        self.assertEqual([w["w"] for w in words], [" Tra", " l'altro", " ok"])    # same shape as faster-whisper words
        self.assertEqual(words[1]["p"], round((0.8 + 0.6 + 0.7) / 3, 2))          # faster-whisper: mean of the token probabilities
        self.assertEqual((words[1]["start"], words[1]["end"]), (0.13, 0.37))
        self.assertEqual(words[2]["p"], 0.4)

    def test_uncertain_words_are_flagged_with_the_same_threshold_as_the_other_engine(self):
        words, _ = engines.words_from_json(result(tok(" certo", 0, 1, 0.95), tok(" dub", 1, 1.4, 0.3), tok("bio", 1.4, 2, 0.5)))
        seg = {"words": words}
        core.flag_words([seg], 0.5, set())
        self.assertEqual([w["flag"] for w in seg["words"]], [False, True])        # (0.3 + 0.5) / 2 = 0.4 < 0.5

    def test_piece_offset_is_added_to_every_time(self):
        words, _ = engines.words_from_json(result(tok(" a", 1.0, 1.5), tok(" b", 2.0, 2.5)), offset=100.0)
        self.assertEqual([(w["start"], w["end"]) for w in words], [(101.0, 101.5), (102.0, 102.5)])

    def test_empty_result(self):
        self.assertEqual(engines.words_from_json({"result": {"language": "en"}, "transcription": []}), ([], "en"))


class SpeechPiecesTest(unittest.TestCase):
    def test_long_silences_split_short_ones_stay_inside(self):
        chunks = [(0.0, 10.0), (13.0, 20.0), (60.0, 70.0)]       # 3 s gap stays, 40 s gap splits
        self.assertEqual(engines.speech_pieces(chunks, 80.0, cut_gap=5.0, pad=0.4), [(0.0, 20.4), (59.6, 70.4)])

    def test_padding_is_clipped_to_the_recording(self):
        self.assertEqual(engines.speech_pieces([(0.1, 9.9)], 10.0, pad=0.4), [(0.0, 10.0)])

    def test_no_speech_no_pieces(self):
        self.assertEqual(engines.speech_pieces([], 10.0), [])


class ProbeOutputTest(unittest.TestCase):
    OK = "ggml_vulkan: Found 1 Vulkan devices:\nggml_vulkan: 0 = AMD Radeon 780M Graphics (AMD proprietary driver) | uma: 1 |\nwhisper_backend_init_gpu: using Vulkan0 backend\n"

    def test_a_real_gpu_passes(self):
        engines.check_probe_output(0, self.OK)

    def test_failures_give_a_reason(self):
        for rc, err, text in [(1, "boom", "exit code 1"),
                              (0, "whisper_init: no GPU found", "no Vulkan device"),
                              (0, self.OK.replace("AMD Radeon 780M Graphics", "llvmpipe (LLVM 15)"), "software renderer"),
                              (0, self.OK.replace("using Vulkan0 backend", "using CPU backend"), "did not use the Vulkan backend")]:
            with self.assertRaises(engines.EngineError) as cm:
                engines.check_probe_output(rc, err)
            self.assertIn(text, str(cm.exception))


class InstalledFilesTest(unittest.TestCase):
    def test_model_and_cli_lookup(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"VIDEOLEARN_WHISPERCPP_DIR": tmp}):
            self.assertIsNone(engines.cli_path())
            self.assertIsNone(engines.model_file("large-v3-turbo"))
            (Path(tmp) / "models").mkdir()
            (Path(tmp) / "whisper-cli.exe").write_bytes(b"x")
            (Path(tmp) / "models" / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
            self.assertEqual(engines.cli_path().name, "whisper-cli.exe")
            self.assertEqual(engines.model_file("large-v3-turbo").name, "ggml-large-v3-turbo-q5_0.bin")
            (Path(tmp) / "models" / "ggml-large-v3-turbo.bin").write_bytes(b"x")      # the full model wins over the quantized one
            self.assertEqual(engines.model_file("large-v3-turbo").name, "ggml-large-v3-turbo.bin")
            (Path(tmp) / "models" / "ggml-small.bin").write_bytes(b"x")
            self.assertEqual(engines.model_file("small").name, "ggml-small.bin")


class EngineChoiceTest(unittest.TestCase):
    """core.transcribe picks the engine: NVIDIA unchanged, then Vulkan, then the processor."""

    def setUp(self):
        self.calls = []
        self.logs = []
        W = ([{"w": " x", "start": 0, "end": 1, "p": 0.9}], "en")
        self.patches = [
            mock.patch.object(core, "_run_whisper", side_effect=lambda wav, m, l, device, p, prog: (self.calls.append(device), W)[1]),
            mock.patch.object(engines, "transcribe_whispercpp", side_effect=lambda *a, **k: (self.calls.append("vulkan"), W)[1]),
            mock.patch.object(engines, "probe_vulkan", side_effect=lambda m: self.calls.append("probe")),
            mock.patch.object(core, "_cuda_dirs", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_t(self, *, nvidia=False, candidate=False, **kw):
        with mock.patch.object(core, "gpu_possible", return_value=nvidia), \
                mock.patch.object(engines, "vulkan_candidate", return_value=candidate):
            return core.transcribe(Path("a.wav"), "large-v3-turbo", None, log=self.logs.append, **kw)

    def test_nvidia_keeps_using_cuda_and_never_touches_vulkan(self):
        self.assertEqual(self.run_t(nvidia=True, candidate=True)[2], "cuda")
        self.assertEqual(self.calls, ["cuda"])

    def test_amd_or_intel_uses_vulkan_after_the_probe(self):
        self.assertEqual(self.run_t(candidate=True)[2], "vulkan")
        self.assertEqual(self.calls, ["probe", "vulkan"])

    def test_no_gpu_engine_uses_the_processor(self):
        self.assertEqual(self.run_t()[2], "cpu")
        self.assertEqual(self.calls, ["cpu"])

    def test_failed_probe_falls_back_to_the_processor_and_says_why(self):
        engines.probe_vulkan.side_effect = engines.EngineError("no Vulkan device")
        self.assertEqual(self.run_t(candidate=True)[2], "cpu")
        self.assertEqual(self.calls, ["cpu"])
        self.assertTrue(any("no Vulkan device" in m for m in self.logs), self.logs)

    def test_vulkan_failing_while_running_falls_back_too(self):
        engines.transcribe_whispercpp.side_effect = engines.EngineError("killed after 300 s")
        self.assertEqual(self.run_t(candidate=True)[2], "cpu")
        self.assertTrue(any("killed after 300 s" in m for m in self.logs), self.logs)

    def test_cuda_failure_still_falls_back_to_the_processor(self):
        def boom(wav, m, l, device, p, prog):
            self.calls.append(device)
            if device == "cuda":
                raise RuntimeError("cublas missing")
            return [], "en"
        core._run_whisper.side_effect = boom
        self.assertEqual(self.run_t(nvidia=True)[2], "cpu")
        self.assertEqual(self.calls, ["cuda", "cpu"])

    def test_forced_engines(self):
        self.assertEqual(self.run_t(candidate=True, engine="cpu")[2], "cpu")
        self.assertEqual(self.calls, ["cpu"])
        self.calls.clear()
        self.assertEqual(self.run_t(engine="cuda")[2], "cuda")
        self.assertEqual(self.run_t(engine="vulkan")[2], "vulkan")      # forced: no candidate check, the probe decides

    def test_forced_vulkan_that_cannot_run_is_an_error_not_a_silent_fallback(self):
        engines.probe_vulkan.side_effect = engines.EngineError("whisper-cli.exe not found")
        with self.assertRaises(RuntimeError) as cm:
            self.run_t(engine="vulkan")
        self.assertIn("whisper-cli.exe not found", str(cm.exception))
        self.assertNotIn("cpu", self.calls)

    def test_old_device_option_still_works(self):
        self.assertEqual(self.run_t(candidate=True, device="cpu")[2], "cpu")
        self.assertEqual(self.run_t(device="cuda")[2], "cuda")

    def test_unknown_engine(self):
        with self.assertRaises(ValueError):
            self.run_t(engine="metal")


class EngineHintTest(unittest.TestCase):
    """The app's time estimate must know which engine will run, for the model it will use."""

    def hint(self, model, *, nvidia=False, vendors=("amd",), models=("ggml-large-v3-turbo.bin",), cli=True):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"VIDEOLEARN_WHISPERCPP_DIR": tmp}),                 mock.patch.object(core, "gpu_possible", return_value=nvidia),                 mock.patch.object(engines, "gpu_vendors", return_value=vendors):
            (Path(tmp) / "models").mkdir()
            for m in models:
                (Path(tmp) / "models" / m).write_bytes(b"x")
            if cli:
                (Path(tmp) / "whisper-cli.exe").write_bytes(b"x")
            return core.engine_hint(model)

    def test_vulkan_only_when_everything_is_installed_for_that_model(self):
        self.assertEqual(self.hint("large-v3-turbo"), "vulkan")
        self.assertEqual(self.hint("small"), "cpu")                       # no ggml-small: the processor will run
        self.assertEqual(self.hint("large-v3-turbo", cli=False), "cpu")
        self.assertEqual(self.hint("large-v3-turbo", vendors=()), "cpu")
        self.assertEqual(self.hint("large-v3-turbo", vendors=("nvidia",)), "cpu")

    def test_nvidia_wins(self):
        self.assertEqual(self.hint("large-v3-turbo", nvidia=True), "cuda")


class CliTest(unittest.TestCase):
    def test_engine_option_reaches_process(self):
        with mock.patch.object(cli, "process", return_value=Path("R.md")) as proc:
            self.assertEqual(cli.main(["video.mp4", "--engine", "vulkan"]), 0)
        self.assertEqual(proc.call_args.kwargs["engine"], "vulkan")
        with mock.patch.object(cli, "process", return_value=Path("R.md")) as proc:
            cli.main(["video.mp4"])
        self.assertEqual(proc.call_args.kwargs["engine"], "auto")

    def test_bad_engine_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["video.mp4", "--engine", "metal"])


if __name__ == "__main__":
    unittest.main()
