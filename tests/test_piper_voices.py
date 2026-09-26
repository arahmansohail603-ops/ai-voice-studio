"""Tests for the Piper voice library and the language/voice agreement.

Two regressions live here.

The first is the language list. ``SUPPORTED_CODES`` used to be a hand-written
list of 73 codes, 31 of which had no Argos package behind them, so every one of
those failed the moment a user picked it. It is now pinned to the 50 languages
Argos actually publishes, and the test asserts the exact set.

The second is the difference between "no voice installed" and "no voice
exists". Punjabi can be translated into but Piper publishes no voice for it, so
offering a download button sends the user to a screen that can never help. The
tests pin the three states apart: installed, downloadable, and impossible.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core import piper_voices
from app.core.piper_voices import (
    DOWNLOADABLE,
    INSTALLED,
    UNKNOWN,
    UNPUBLISHED,
    PiperVoiceLibrary,
    VoiceEntry,
    is_speakable,
    published_voice_languages,
    require_voice_for,
)
from app.core.translator import SUPPORTED_CODES

#: The 50 languages Argos Translate publishes packages for.
EXPECTED_CODES = {
    "ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el",
    "en", "eo", "es", "et", "eu", "fa", "fi", "fr", "ga",
    "gl", "he", "hi", "hu", "id", "it", "ja", "ko", "ky",
    "lt", "lv", "ms", "nb", "nl", "pb", "pl", "pt", "ro",
    "ru", "sk", "sl", "sq", "sv", "sw", "th", "tl", "tr",
    "uk", "ur", "vi", "zh", "zt",
}

#: Of those, the ones with no published Piper voice. They are translate-only:
#: the text is produced, but nothing can read it aloud.
TEXT_ONLY_CODES = {
    "az", "eo", "ga", "gl", "ky", "ms", "nb", "pb", "tl", "zt",
}

_ONNX = b"onnx-voice-payload" * 64
_CONFIG = json.dumps(
    {"language": {"code": "ur_PK", "name": "Urdu"}, "audio": {"sample_rate": 22050}}
).encode("utf-8")


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


def _catalog_blob() -> bytes:
    """A catalog shaped exactly like the published ``voices.json``."""
    voices = {
        "ur_PK-fasih-medium": {
            "key": "ur_PK-fasih-medium",
            "name": "fasih",
            "language": {
                "code": "ur_PK",
                "family": "ur",
                "name_native": "اردو",
                "name_english": "Urdu",
                "country_english": "Pakistan",
            },
            "quality": "medium",
            "num_speakers": 1,
            "files": {
                "ur/ur_PK/fasih/medium/ur_PK-fasih-medium.onnx": {
                    "size_bytes": len(_ONNX),
                    "md5_digest": _md5(_ONNX),
                },
                "ur/ur_PK/fasih/medium/ur_PK-fasih-medium.onnx.json": {
                    "size_bytes": len(_CONFIG),
                    "md5_digest": _md5(_CONFIG),
                },
            },
        },
        "hi_IN-pratham-medium": {
            "key": "hi_IN-pratham-medium",
            "name": "pratham",
            "language": {
                "code": "hi_IN",
                "family": "hi",
                "name_native": "हिन्दी",
                "name_english": "Hindi",
                "country_english": "India",
            },
            "quality": "medium",
            "num_speakers": 1,
            "files": {
                "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx": {
                    "size_bytes": len(_ONNX),
                    "md5_digest": _md5(_ONNX),
                },
            },
        },
        # Welsh: Piper publishes a voice, but it is not one of the fifty
        # translatable targets, so the screen must badge it speak-only.
        "cy_GB-gwydion-medium": {
            "key": "cy_GB-gwydion-medium",
            "name": "gwydion",
            "language": {
                "code": "cy_GB",
                "family": "cy",
                "name_native": "Cymraeg",
                "name_english": "Welsh",
                "country_english": "United Kingdom",
            },
            "quality": "medium",
            "num_speakers": 1,
            "files": {
                "cy/cy_GB/gwydion/medium/cy_GB-gwydion-medium.onnx": {
                    "size_bytes": len(_ONNX),
                    "md5_digest": _md5(_ONNX),
                },
            },
        },
        # A voice whose bytes do not match the digest, for the corrupt case.
        "en_US-corrupt-medium": {
            "key": "en_US-corrupt-medium",
            "name": "corrupt",
            "language": {
                "code": "en_US",
                "family": "en",
                "name_native": "English",
                "name_english": "English",
                "country_english": "United States",
            },
            "quality": "medium",
            "num_speakers": 1,
            "files": {
                "en/en_US/corrupt/medium/en_US-corrupt-medium.onnx": {
                    "size_bytes": len(_ONNX),
                    "md5_digest": "0" * 32,
                },
            },
        },
    }
    return json.dumps(voices).encode("utf-8")


class _Fixture:
    """A local HTTP server serving a fake catalog and voice payloads."""

    def __init__(self, truncate: str | None = None) -> None:
        self._blobs = {
            "voices.json": _catalog_blob(),
            "ur/ur_PK/fasih/medium/ur_PK-fasih-medium.onnx": _ONNX,
            "ur/ur_PK/fasih/medium/ur_PK-fasih-medium.onnx.json": _CONFIG,
            "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx": _ONNX,
            "cy/cy_GB/gwydion/medium/cy_GB-gwydion-medium.onnx": _ONNX,
            "en/en_US/corrupt/medium/en_US-corrupt-medium.onnx": _ONNX,
        }
        # Optionally serve a short body so the size check has something to catch.
        self._truncate = truncate
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                # self.path includes the leading slash; the blob keys do not.
                body = fixture._blobs.get(self.path.lstrip("/"))
                if body is None:
                    self.send_error(404)
                    return
                if fixture._truncate and self.path.endswith(fixture._truncate):
                    body = body[: len(body) // 2]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _LibraryTestCase(unittest.TestCase):
    """Base class that serves a fake catalog over HTTP."""

    truncate: str | None = None

    def setUp(self) -> None:
        self._fixture = _Fixture(truncate=self.truncate)
        self._tmp = TemporaryDirectory()
        self.voice_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._fixture.close)
        patches = (
            unittest.mock.patch.object(
                piper_voices, "VOICES_INDEX_URL", self._fixture.base + "voices.json"
            ),
            unittest.mock.patch.object(piper_voices, "_FILE_BASE", self._fixture.base),
            # is_speakable() and require_voice_for() build their own library from
            # the default directory, so redirect it or they would read the real
            # machine's catalog instead of this fixture's.
            unittest.mock.patch.object(
                piper_voices, "PIPER_VOICE_DIR", self.voice_dir
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.library = PiperVoiceLibrary(self.voice_dir)

    def entry(self, key: str) -> VoiceEntry:
        return next(e for e in self.library.catalog() if e.key == key)


class SupportedLanguageTests(unittest.TestCase):
    """The language list the UI offers."""

    def test_exactly_the_argos_fifty(self):
        self.assertEqual(set(SUPPORTED_CODES), EXPECTED_CODES)
        self.assertEqual(len(SUPPORTED_CODES), 50)

    def test_no_duplicates(self):
        self.assertEqual(len(SUPPORTED_CODES), len(set(SUPPORTED_CODES)))

    def test_every_code_has_a_display_name(self):
        from app.config import language_display_name

        unnamed = [
            code
            for code in SUPPORTED_CODES
            if language_display_name(code) == code.upper()
        ]
        self.assertEqual(unnamed, [], f"codes rendering as bare uppercase: {unnamed}")


class VoiceStateTests(_LibraryTestCase):
    """Installed vs downloadable vs impossible must stay distinct."""

    def test_published_languages_come_from_the_catalog(self):
        self.library.catalog()
        self.assertEqual(self.library.published_languages(), {"ur", "hi", "en", "cy"})

    def test_published_but_not_installed(self):
        self.library.catalog()
        self.assertTrue(self.library.has_published_voice("ur"))
        self.assertFalse(self.library.has_installed_voice("ur"))
        self.assertEqual(self.library.voice_availability("ur"), DOWNLOADABLE)

    def test_locale_variants_match_the_family(self):
        self.library.catalog()
        for code in ("ur", "ur_PK", "ur-PK", "UR"):
            self.assertTrue(self.library.has_published_voice(code), code)

    def test_language_absent_from_catalog_is_not_published(self):
        self.library.catalog()
        for code in sorted(TEXT_ONLY_CODES):
            self.assertFalse(
                self.library.has_published_voice(code),
                f"{code} should have no published voice",
            )
            self.assertEqual(self.library.voices_for_language(code), [])
            self.assertTrue(
                self.library.can_never_speak(code),
                f"{code} must be reported as impossible, not downloadable",
            )

    def test_the_three_voice_categories_stay_disjoint(self):
        """Translatable, speak-only and text-only must not leak into each other.

        A language can have a voice but no Argos package (Welsh), or an Argos
        package but no voice (Punjabi). Conflating either direction either hides
        a usable download or offers one that can never succeed.
        """
        self.library.catalog()
        published = self.library.published_languages()
        translatable = set(SUPPORTED_CODES)

        # Welsh has a voice but is not a target: it is speak-only.
        self.assertNotIn("cy", translatable)
        self.assertIn("cy", published)
        # Punjabi is a target with no voice anywhere: it is text-only.
        self.assertIn("pb", translatable)
        self.assertNotIn("pb", published)
        # The declared text-only set is exactly the target/voice gap.
        self.assertEqual(TEXT_ONLY_CODES & published, set())
        self.assertTrue(TEXT_ONLY_CODES <= translatable)

    def test_absent_catalog_is_unknown_not_unpublished(self):
        """A first run has no cached catalog yet.

        Reporting that as "no voice published" would hide the download button for
        Urdu, Hindi and every other language that does have a voice, on the exact
        run where the user most needs to find it.
        """
        self.assertEqual(self.library.published_languages(), set())
        self.assertEqual(self.library.voice_availability("ur"), UNKNOWN)
        self.assertEqual(self.library.voice_availability("pb"), UNKNOWN)
        self.assertFalse(
            self.library.can_never_speak("ur"),
            "an uncached catalog must not be read as 'no voice exists'",
        )
        self.assertFalse(self.library.can_never_speak("pb"))

    def test_installed_wins_over_every_other_state(self):
        self.library.install(self.entry("ur_PK-fasih-medium"))
        self.assertEqual(self.library.voice_availability("ur"), INSTALLED)
        self.assertFalse(self.library.can_never_speak("ur"))

    def test_is_speakable_is_permissive_without_a_catalog(self):
        # An uncached catalog must read as "unknown", never as "impossible",
        # or a first run would mark all fifty languages text-only in the
        # dropdown and hide every download button.
        self.assertEqual(published_voice_languages(), frozenset())
        for code in sorted(SUPPORTED_CODES):
            self.assertTrue(is_speakable(code), code)

    def test_is_speakable_drops_text_only_languages_once_cached(self):
        self.library.catalog()
        for code in sorted(TEXT_ONLY_CODES):
            self.assertFalse(is_speakable(code), code)
        for code in ("ur", "hi", "en"):
            self.assertTrue(is_speakable(code), code)


class RequireVoiceForTests(_LibraryTestCase):
    """The error the synthesis path raises when a voice is missing."""

    def test_names_the_language_and_points_at_the_voices_screen(self):
        from app.core.errors import AppError

        self.library.catalog()
        with self.assertRaises(AppError) as caught:
            require_voice_for("ur")
        message = str(caught.exception)
        self.assertIn("Urdu", message)
        self.assertIn("Voices screen", message)

    def test_stays_quiet_for_a_language_with_no_voice_at_all(self):
        # Nothing to install, so there is nothing for the user to go and do.
        self.library.catalog()
        require_voice_for("pb")


class InstallTests(_LibraryTestCase):
    """Downloading, verifying and publishing a voice."""

    def test_install_writes_both_files_and_verifies_digests(self):
        entry = self.entry("ur_PK-fasih-medium")
        path = self.library.install(entry)

        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), _ONNX)
        self.assertTrue(path.with_suffix(".onnx.json").is_file())
        self.assertIn("ur_PK-fasih-medium", self.library.installed())

    def test_install_reports_monotonic_progress_that_completes(self):
        seen: list[tuple[int, int]] = []
        self.library.install(
            self.entry("ur_PK-fasih-medium"),
            on_progress=lambda stage, got, total, label: seen.append((got, total)),
        )
        self.assertTrue(seen, "no progress reported")
        self.assertEqual(seen[-1][0], seen[-1][1], "progress never reached the total")
        self.assertEqual([p for p in seen], sorted(seen), "progress went backwards")

    def test_install_leaves_no_partial_file(self):
        self.library.install(self.entry("ur_PK-fasih-medium"))
        self.assertEqual(list(self.voice_dir.glob("*.part")), [])

    def test_install_is_idempotent(self):
        entry = self.entry("ur_PK-fasih-medium")
        first = self.library.install(entry)
        mtime = first.stat().st_mtime_ns
        calls: list[int] = []
        self.library.install(entry, on_progress=lambda *a: calls.append(1))
        self.assertEqual(calls, [], "a complete voice was downloaded again")
        self.assertEqual(first.stat().st_mtime_ns, mtime)

    def test_corrupt_payload_is_rejected_and_not_installed(self):
        from app.core.errors import TTSGenerationError

        with self.assertRaises(TTSGenerationError):
            self.library.install(self.entry("en_US-corrupt-medium"))
        self.assertNotIn("en_US-corrupt-medium", self.library.installed())
        self.assertEqual(list(self.voice_dir.glob("*.part")), [])
        self.assertEqual(list(self.voice_dir.glob("*.onnx")), [])

    def test_missing_config_file_leaves_no_usable_voice(self):
        # A voice is only "installed" when the .onnx *and* its .json are present;
        # a lone model would fail at synthesis time with a confusing error.
        self.library.install(self.entry("hi_IN-pratham-medium"))
        self.assertNotIn("hi_IN-pratham-medium", self.library.installed())
        self.assertTrue(self.library.has_published_voice("hi"))
        self.assertFalse(self.library.has_installed_voice("hi"))

    def test_remove_deletes_model_and_config(self):
        entry = self.entry("ur_PK-fasih-medium")
        self.library.install(entry)
        self.library.remove(entry)
        self.assertEqual(self.library.installed(), set())
        self.assertFalse((self.voice_dir / "ur_PK-fasih-medium.onnx").exists())
        self.assertFalse((self.voice_dir / "ur_PK-fasih-medium.onnx.json").exists())

    def test_remove_is_a_no_op_for_absent_voice(self):
        self.library.remove(self.entry("ur_PK-fasih-medium"))

    def test_catalog_is_cached_for_offline_use(self):
        self.library.catalog()
        self.assertTrue(self.library.index_path.is_file())
        with unittest.mock.patch.object(
            piper_voices, "VOICES_INDEX_URL", "http://127.0.0.1:1/none.json"
        ):
            self.assertEqual(len(self.library.catalog()), 4)

    def test_cached_catalog_never_touches_the_network(self):
        self.library.catalog()
        with unittest.mock.patch.object(
            piper_voices.PiperVoiceLibrary, "_fetch_index",
            side_effect=AssertionError("must not fetch"),
        ):
            self.assertEqual(len(self.library.catalog_from_cache()), 4)


class TruncatedDownloadTests(_LibraryTestCase):
    """A short body must fail the size check rather than install a stub."""

    truncate = ".onnx"

    def test_short_payload_is_rejected(self):
        from app.core.errors import TTSGenerationError

        with self.assertRaises(TTSGenerationError):
            self.library.install(self.entry("ur_PK-fasih-medium"))
        self.assertEqual(self.library.installed(), set())
        self.assertEqual(list(self.voice_dir.glob("*.part")), [])


class LanguageGroupingTests(_LibraryTestCase):
    """The per-language rows the screen renders."""

    def test_rows_carry_native_name_and_translatable_flag(self):
        self.library.catalog()
        rows = {row["family"]: row for row in self.library.languages()}
        self.assertEqual(rows["ur"]["native"], "اردو")
        self.assertEqual(rows["ur"]["label"], "Urdu")
        self.assertTrue(rows["ur"]["translatable"])
        # Welsh has a voice but no Argos package, so it cannot be a target.
        self.assertFalse(rows["cy"]["translatable"])
        self.assertEqual(rows["cy"]["native"], "Cymraeg")

    def test_installed_count_tracks_disk(self):
        self.library.install(self.entry("ur_PK-fasih-medium"))
        rows = {row["family"]: row for row in self.library.languages()}
        self.assertEqual(rows["ur"]["installed"], 1)
        self.assertEqual(rows["hi"]["installed"], 0)

    def test_a_language_with_no_voice_is_absent_from_the_rows(self):
        # This is why the screen needs a specific empty state: filtering to
        # Punjabi legitimately matches nothing.
        families = {row["family"] for row in self.library.languages()}
        for code in sorted(TEXT_ONLY_CODES):
            self.assertNotIn(code, families)


class VoiceEntryTests(unittest.TestCase):
    """Parsing and display of a single catalog entry."""

    def setUp(self) -> None:
        raw = json.loads(_catalog_blob().decode("utf-8"))
        self.entry = VoiceEntry(raw["ur_PK-fasih-medium"])

    def test_installed_name_is_the_model_stem(self):
        self.assertEqual(self.entry.installed_name(), "ur_PK-fasih-medium")

    def test_sizes_and_display(self):
        self.assertEqual(self.entry.model_bytes(), len(_ONNX))
        self.assertEqual(self.entry.total_bytes(), len(_ONNX) + len(_CONFIG))
        self.assertIn("Fasih", self.entry.display())
        self.assertIn("Balanced", self.entry.display())

    def test_missing_fields_do_not_raise(self):
        entry = VoiceEntry({"key": "x", "files": {}})
        self.assertIsNone(entry.model_file())
        self.assertEqual(entry.model_bytes(), 0)
        self.assertEqual(entry.total_bytes(), 0)
        self.assertEqual(entry.speakers, 1)

    def test_bad_speaker_count_falls_back_to_one(self):
        self.assertEqual(VoiceEntry({"num_speakers": "many"}).speakers, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
