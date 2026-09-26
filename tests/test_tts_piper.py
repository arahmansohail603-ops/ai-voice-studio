"""Regression tests for offline Piper synthesis.

The bug this file exists for: the text was piped into Piper's stdin as UTF-8
bytes, while Piper decoded that stdin with the *system* locale -- cp1252 on
this machine. Non-ASCII scripts were therefore undecodable, Piper synthesised
zero audio chunks, and its own ``wave`` writer reported *that* instead. The
``wave`` module raises from ``close()`` when a wav is closed without ever
having a channel count set, so the message named the wav writer and buried the
decode error underneath it: "wave.Error: # channels not specified" for what
was really a text-encoding failure.

Worse, ASCII text cannot tell the broken path from the working one, so the
English voice always passed and the failure only ever appeared on the Urdu and
Korean voices. That is why a real non-ASCII synthesis runs in this suite, and
why there is also a test on the command that gets built: fixing the cause is
not enough if the code can drift back to piping.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from app.config import PIPER_VOICE_DIR
from app.core.async_runner import AsyncRunner
from app.core.errors import TTSGenerationError, module_installed
from app.core.tts_engine import TTSEngine, _explain_piper_failure

#: A line of each script that failed. Keep these non-ASCII on purpose: they are
#: the whole point, and an ASCII placeholder would let the bug pass again.
URDU_TEXT = "اچھ، آج دن کا دل خوش ہے۔"
KOREAN_TEXT = "안녕하세요, 오늘 하루도 좋은 하루입니다."


def _has_voice(stem: str) -> bool:
    return (PIPER_VOICE_DIR / f"{stem}.onnx").exists()


class _EngineCase(unittest.TestCase):
    """Builds an engine pointed at a throwaway output directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = Path(self._tmp.name)
        self.engine = TTSEngine(
            AsyncRunner(), self.out_dir, backend="piper"
        )
        # _piper_synth refuses early without the package; the command-shape
        # tests care about what gets built, not about whether Piper is
        # installed, so pretend it is.
        self.engine._piper_available = True

    def _voice(self, stem: str = "en_US-lessac-medium") -> dict:
        # In the throwaway directory, never in PIPER_VOICE_DIR: a stub .onnx
        # written into the real voice library would look like an installed
        # voice and Piper would then fail to load it for real.
        model = self.out_dir / f"{stem}.onnx"
        model.touch(exist_ok=True)
        return {"path": str(model), "short_name": stem}

    def _out(self, name: str = "out.wav") -> Path:
        return self.out_dir / name


class PiperCommandTests(_EngineCase):
    """How the text is handed to Piper, which is the actual fix."""

    def _run(self, text: str, stem: str = "en_US-lessac-medium"):
        """Run _piper_synth with Piper stubbed, returning the call details."""
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["kwargs"] = kwargs
            # Read the text file while the temp dir still exists.
            flag = cmd.index("--input-file")
            text_path = Path(cmd[flag + 1])
            seen["input_path"] = text_path
            seen["text_bytes"] = text_path.read_bytes()
            # Stand in for a successful run: a wav with a real header.
            out = Path(cmd[cmd.index("--output_file") + 1])
            out.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"", stderr=b"INFO:piper:done"
            )

        with mock.patch(
            "app.core.tts_engine.subprocess.run", side_effect=fake_run
        ):
            out = self.engine._piper_synth(
                text, self._voice(stem), 1.0, self._out()
            )
        seen["out"] = out
        return seen

    def test_text_travels_in_a_file_not_down_the_pipe(self) -> None:
        """The regression guard: no stdin, a --input-file instead."""
        seen = self._run(URDU_TEXT)
        self.assertNotIn(
            "input",
            seen["kwargs"],
            "text is being piped to Piper's stdin, which it decodes with the "
            "system locale -- that is the bug being fixed",
        )
        self.assertIn("--input-file", seen["cmd"])

    def test_input_file_holds_the_text_as_utf8(self) -> None:
        seen = self._run(URDU_TEXT)
        self.assertEqual(seen["text_bytes"], URDU_TEXT.encode("utf-8"))
        # Round-trips, which is what Piper does when it opens the path.
        self.assertEqual(seen["text_bytes"].decode("utf-8"), URDU_TEXT)

    def test_input_file_is_removed_afterwards(self) -> None:
        seen = self._run(URDU_TEXT)
        self.assertFalse(
            seen["input_path"].exists(),
            "the temporary text file is left behind on disk",
        )

    def test_child_decodes_stderr_as_utf8(self) -> None:
        """Piper logs to stderr; a locale fallback would mojibake a real error."""
        seen = self._run(URDU_TEXT)
        env = seen["kwargs"].get("env") or {}
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")

    def test_child_keeps_the_rest_of_the_environment(self) -> None:
        """PATH is what finds onnxruntime and espeak's data; do not drop it."""
        seen = self._run(URDU_TEXT)
        env = seen["kwargs"]["env"]
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))

    def test_ascii_and_non_ascii_take_the_same_path(self) -> None:
        """Otherwise the English voice keeps passing while others break."""
        ascii_seen = self._run("Good morning.")
        non_ascii_seen = self._run(URDU_TEXT)
        for seen in (ascii_seen, non_ascii_seen):
            self.assertIn("--input-file", seen["cmd"])
            self.assertNotIn("input", seen["kwargs"])

    def test_model_and_output_flags_survive(self) -> None:
        seen = self._run(URDU_TEXT, stem="ur_PK-fasih-medium")
        self.assertEqual(
            seen["cmd"][seen["cmd"].index("--model") + 1],
            str(self.out_dir / "ur_PK-fasih-medium.onnx"),
        )
        self.assertEqual(
            seen["cmd"][seen["cmd"].index("--output_file") + 1],
            str(self._out()),
        )


class PiperFailureTests(_EngineCase):
    """Empty input and unreadable reports, both of which used to mislead."""

    def test_empty_text_never_spawns_piper(self) -> None:
        with mock.patch("app.core.tts_engine.subprocess.run") as run:
            with self.assertRaises(TTSGenerationError) as ctx:
                self.engine._piper_synth("", self._voice(), 1.0, self._out())
        run.assert_not_called()
        self.assertIn("no text", str(ctx.exception).lower())

    def test_whitespace_only_text_never_spawns_piper(self) -> None:
        with mock.patch("app.core.tts_engine.subprocess.run") as run:
            with self.assertRaises(TTSGenerationError):
                self.engine._piper_synth(
                    "   \n\t  ", self._voice(), 1.0, self._out()
                )
        run.assert_not_called()

    def test_no_audio_symptom_is_named_rather_than_passed_on(self) -> None:
        """The wave complaint is a symptom; say so instead of echoing it."""
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"piper\\__main__.py\", line 252, in main\n"
            "    lines_to_wav()\n"
            "  File \"wave.py\", line 565, in close\n"
            "    self._ensure_header_written(0)\n"
            "wave.Error: # channels not specified\n"
        ).encode("utf-8")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=stderr)

        with mock.patch(
            "app.core.tts_engine.subprocess.run", side_effect=fake_run
        ):
            with self.assertRaises(TTSGenerationError) as ctx:
                self.engine._piper_synth(
                    URDU_TEXT, self._voice(), 1.0, self._out()
                )
        message = str(ctx.exception)
        self.assertIn("no audio", message.lower())
        self.assertIn("symptom", message.lower())
        self.assertNotIn("line 252", message)

    def test_a_real_error_is_still_shown_verbatim(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"piper\\voice.py\", line 300, in synthesize\n"
            "    raise RuntimeError(\"onnxruntime is unhappy\")\n"
            "RuntimeError: onnxruntime is unhappy\n"
        ).encode("utf-8")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=stderr)

        with mock.patch(
            "app.core.tts_engine.subprocess.run", side_effect=fake_run
        ):
            with self.assertRaises(TTSGenerationError) as ctx:
                self.engine._piper_synth(
                    URDU_TEXT, self._voice(), 1.0, self._out()
                )
        self.assertIn("onnxruntime is unhappy", str(ctx.exception))

    def test_zero_byte_output_is_cleaned_up(self) -> None:
        """A silent file would otherwise be listed as a finished conversion."""

        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with mock.patch(
            "app.core.tts_engine.subprocess.run", side_effect=fake_run
        ):
            with self.assertRaises(TTSGenerationError):
                self.engine._piper_synth(
                    URDU_TEXT, self._voice(), 1.0, self._out()
                )
        self.assertFalse(self._out().exists())


class ExplainPiperFailureTests(unittest.TestCase):
    """The message mapper on its own, including the pass-through case."""

    def test_wave_complaint_is_translated(self) -> None:
        out = _explain_piper_failure(
            "  wave.Error: # channels not specified"
        )
        self.assertIn("no audio", out.lower())
        self.assertIn("symptom", out.lower())

    def test_anything_else_is_left_alone(self) -> None:
        detail = "RuntimeError: something specific went wrong"
        self.assertEqual(_explain_piper_failure(detail), detail)

    def test_empty_detail_does_not_crash(self) -> None:
        self.assertEqual(_explain_piper_failure(""), "")


@unittest.skipUnless(
    module_installed("piper"), "the piper package is not installed"
)
class PiperRealSynthesisTests(_EngineCase):
    """The end-to-end check that ASCII could never have given us."""

    def _synthesise(self, stem: str, text: str) -> Path:
        model = PIPER_VOICE_DIR / f"{stem}.onnx"
        if not model.exists():
            self.skipTest(f"voice {stem} is not installed")
        out = self._out(f"{stem}.wav")
        return self.engine._piper_synth(
            text, {"path": str(model), "short_name": stem}, 1.0, out
        )

    def _assert_real_audio(self, path: Path, label: str) -> None:
        self.assertTrue(path.exists(), f"{label}: no file was written")
        self.assertGreater(path.stat().st_size, 44, f"{label}: file is empty")
        with wave.open(str(path), "rb") as handle:
            self.assertEqual(
                handle.getframerate(), 22050, f"{label}: unexpected sample rate"
            )
            self.assertEqual(handle.getnchannels(), 1, f"{label}: not mono")
            self.assertGreater(
                handle.getnframes(), 0, f"{label}: no audio samples at all"
            )

    def test_urdu_voice_synthesises_real_audio(self) -> None:
        self._assert_real_audio(
            self._synthesise("ur_PK-fasih-medium", URDU_TEXT), "urdu"
        )

    def test_korean_voice_synthesises_real_audio(self) -> None:
        self._assert_real_audio(
            self._synthesise("ko_KR-kss-medium", KOREAN_TEXT), "korean"
        )

    def test_english_voice_still_synthesises(self) -> None:
        self._assert_real_audio(
            self._synthesise("en_US-lessac-medium", "Good morning."), "english"
        )


if __name__ == "__main__":
    unittest.main()
