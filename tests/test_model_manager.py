"""End-to-end test of the catalog + downloader against a local HTTP server.

Lives in tests/ rather than a throwaway script so the whole pipeline
(verify -> download -> digest -> extract -> install -> delete) stays covered.
"""
from __future__ import annotations

import hashlib
import json
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from app.core import model_catalog as mc
from app.core.errors import AppError, ModelError, ModelNotInstalledError
from app.core.model_manager import (
    STAGE_DOWNLOADING,
    STAGE_INSTALLED,
    ModelManager,
    extract_zip,
    free_bytes,
    human_size,
)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _CRYPTO = True
except ImportError:  # pragma: no cover
    _CRYPTO = False


def b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def build_zip(entries: dict[str, bytes]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


def make_manifest(models: list[dict]) -> dict:
    return {
        "type": "model_catalog",
        "version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "models": models,
    }


def sign(payload: dict, key, key_id: str = "test-key") -> dict:
    from app.licensing.crypto import canonical_json

    return {
        "algorithm": "Ed25519",
        "encoding": "canonical-json",
        "key_id": key_id,
        "signature": b64url(key.sign(canonical_json(payload).encode("utf-8"))),
        "payload": payload,
    }


def public_key_material(key) -> dict:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {"test-key": raw.hex()}


class _Handler(BaseHTTPRequestHandler):
    """Serves the fixture blobs. Supports Range so resume can be tested."""

    blobs: ClassVar[dict[str, bytes]] = {}
    fail_ranges: ClassVar[set[str]] = set()

    def log_message(self, *args):
        return

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/catalog.json":
            body = json.dumps(self.server.catalog_envelope).encode("utf-8")
            self._send(body, "application/json")
            return
        name = path.rsplit("/", 1)[-1]
        body = self.blobs.get(name)
        if body is None:
            self.send_error(404)
            return
        start = 0
        range_header = self.headers.get("Range")
        if (
            range_header
            and name not in self.fail_ranges
            and range_header.startswith("bytes=")
            and "-" in range_header
        ):
            raw = range_header.split("=", 1)[1]
            start = int(raw.split("-", 1)[0] or 0)
        if start:
            chunk = body[start:]
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
            self.end_headers()
            self.wfile.write(chunk)
            return
        self._send(body, "application/octet-stream")

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(_CRYPTO, "cryptography is required")
class ModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.keys = public_key_material(self.key)

    def test_roundtrip_and_lookup(self) -> None:
        spec = {
            "id": "vosk-en-us-0.15",
            "kind": "stt",
            "name": "Vosk English (US)",
            "version": "0.15",
            "install_dir": "vosk-models/small_en-us",
            "engine": "vosk",
            "languages": ["en-US"],
            "marker_files": ["am", "graph"],
            "size_bytes": 100,
            "archive": {"format": "zip", "sha256": "a" * 64},
            "assets": [
                {
                    "name": "m.zip",
                    "url": "https://github.com/o/r/releases/download/v1/m.zip",
                    "size_bytes": 10,
                    "sha256": "b" * 64,
                    "part": 0,
                }
            ],
        }
        catalog = mc.parse_catalog(make_manifest([spec]))
        self.assertEqual(len(catalog.models), 1)
        loaded = catalog.require("vosk-en-us-0.15")
        self.assertEqual(loaded.kind, "stt")
        self.assertFalse(loaded.is_chunked)
        self.assertEqual(loaded.to_dict()["install_dir"], spec["install_dir"])
        self.assertEqual(catalog.find(kind="stt", language="en-US").id, "vosk-en-us-0.15")
        self.assertIsNone(catalog.find(kind="tts"))
        self.assertIsNone(catalog.get("missing"))

    def test_verify_accepts_valid_signature(self) -> None:
        payload = make_manifest([])
        catalog = mc.verify_catalog(sign(payload, self.key), self.keys)
        self.assertEqual(catalog.models, ())
        self.assertEqual(catalog.source, "remote")

    def test_verify_rejects_tampered_payload(self) -> None:
        payload = make_manifest([])
        envelope = sign(payload, self.key)
        envelope["payload"] = make_manifest(
            [
                {
                    "id": "evil",
                    "kind": "stt",
                    "name": "Evil",
                    "version": "1",
                    "install_dir": "evil",
                    "archive": {"sha256": "c" * 64},
                    "assets": [
                        {
                            "name": "e.zip",
                            "url": "https://github.com/o/r/e.zip",
                            "size_bytes": 1,
                            "sha256": "d" * 64,
                        }
                    ],
                }
            ]
        )
        with self.assertRaises(ModelError):
            mc.verify_catalog(envelope, self.keys)

    def test_verify_rejects_unknown_key(self) -> None:
        payload = make_manifest([])
        with self.assertRaises(ModelError):
            mc.verify_catalog(sign(payload, self.key), {"other-key": self.keys["test-key"]})

    def test_rejects_unsafe_install_dir(self) -> None:
        base = {
            "id": "bad",
            "kind": "stt",
            "name": "Bad",
            "version": "1",
            "archive": {"sha256": "a" * 64},
            "assets": [
                {
                    "name": "b.zip",
                    "url": "https://github.com/o/r/b.zip",
                    "size_bytes": 1,
                    "sha256": "b" * 64,
                }
            ],
        }
        for bad in ("../escape", "/abs/path", "a/../../b", "", "C:/win"):
            spec = dict(base, install_dir=bad)
            with self.subTest(install_dir=bad), self.assertRaises(ModelError):
                mc.parse_catalog(make_manifest([spec]))

    def test_rejects_non_https_and_foreign_host(self) -> None:
        base = {
            "id": "m",
            "kind": "stt",
            "name": "M",
            "version": "1",
            "install_dir": "m",
            "archive": {"sha256": "a" * 64},
        }
        asset = {"name": "m.zip", "size_bytes": 1, "sha256": "b" * 64}
        for url in ("http://github.com/o/r/m.zip", "https://evil.example/m.zip"):
            with self.subTest(url=url):
                spec = dict(base, assets=[dict(asset, url=url)])
                with self.assertRaises(ModelError):
                    mc.parse_catalog(make_manifest([spec]))

    def test_rejects_bad_digest_and_unknown_field(self) -> None:
        base = {
            "id": "m",
            "kind": "stt",
            "name": "M",
            "version": "1",
            "install_dir": "m",
            "assets": [
                {
                    "name": "m.zip",
                    "url": "https://github.com/o/r/m.zip",
                    "size_bytes": 1,
                    "sha256": "b" * 64,
                }
            ],
        }
        with self.assertRaises(ModelError):
            mc.parse_catalog(
                make_manifest([dict(base, archive={"sha256": "short"})])
            )
        with self.assertRaises(ModelError):
            mc.parse_catalog(make_manifest([dict(base, archive={"sha256": "a" * 64}, oops=1)]))

    def test_rejects_part_gaps_and_duplicates(self) -> None:
        def asset(part: int) -> dict:
            return {
                "name": f"p{part}.zip",
                "url": f"https://github.com/o/r/p{part}.zip",
                "size_bytes": 1,
                "sha256": "b" * 64,
                "part": part,
            }

        base = {
            "id": "m",
            "kind": "tts",
            "name": "M",
            "version": "1",
            "install_dir": "m",
            "archive": {"sha256": "a" * 64},
        }
        for parts in ([0, 2], [1, 1], [0, 0, 0]):
            with self.subTest(parts=parts), self.assertRaises(ModelError):
                mc.parse_catalog(
                    make_manifest([dict(base, assets=[asset(p) for p in parts])])
                )

    def test_rejects_wrong_version_and_kind(self) -> None:
        payload = make_manifest([])
        payload["version"] = 99
        with self.assertRaises(ModelError):
            mc.parse_catalog(payload)
        with self.assertRaises(ModelError):
            mc.parse_catalog(
                make_manifest(
                    [
                        {
                            "id": "m",
                            "kind": "nonsense",
                            "name": "M",
                            "version": "1",
                            "install_dir": "m",
                            "archive": {"sha256": "a" * 64},
                            "assets": [
                                {
                                    "name": "m.zip",
                                    "url": "https://github.com/o/r/m.zip",
                                    "size_bytes": 1,
                                    "sha256": "b" * 64,
                                }
                            ],
                        }
                    ]
                )
            )

    def test_chunked_part_ordering_is_normalised(self) -> None:
        def asset(part: int) -> dict:
            return {
                "name": f"p{part}.zip",
                "url": f"https://github.com/o/r/p{part}.zip",
                "size_bytes": 10,
                "sha256": "b" * 64,
                "part": part,
            }

        catalog = mc.parse_catalog(
            make_manifest(
                [
                    {
                        "id": "big",
                        "kind": "tts",
                        "name": "Big",
                        "version": "1",
                        "install_dir": "big",
                        "archive": {"sha256": "a" * 64},
                        "assets": [asset(1), asset(0)],
                    }
                ]
            )
        )
        spec = catalog.require("big")
        self.assertTrue(spec.is_chunked)
        self.assertEqual([a.part for a in spec.assets], [0, 1])
        self.assertEqual(spec.download_bytes(), 20)


@unittest.skipUnless(_CRYPTO, "cryptography is required")
class ModelManagerTests(unittest.TestCase):
    """Full pipeline against a real local HTTP server."""

    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.keys = public_key_material(self.key)
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.models_root = root / "models"
        self.staging = root / "staging"
        self.models_root.mkdir(parents=True, exist_ok=True)

        self.small = build_zip(
            {
                "vosk-model-small-en-us-0.15/am/final.mdl": b"x" * 512,
                "vosk-model-small-en-us-0.15/graph/Gr.fst": b"y" * 512,
                "vosk-model-small-en-us-0.15/README": b"hello",
            }
        )
        self.parts = [
            ("a0.zip", self.small[: len(self.small) // 2]),
            ("a1.zip", self.small[len(self.small) // 2 :]),
        ]
        self.blobs = {name: data for name, data in self.parts}

        self.spec_template = {
            "id": "vosk-en-us-0.15",
            "kind": "stt",
            "name": "Vosk English (US)",
            "version": "0.15",
            "description": "Offline English recognition",
            "engine": "vosk",
            "languages": ["en-US"],
            "requires": ["vosk"],
            "install_dir": "vosk-models/small_en-us",
            "marker_files": ["am", "graph"],
            "size_bytes": 1024,
            "archive": {
                "format": "zip",
                "sha256": hashlib.sha256(self.small).hexdigest(),
                "max_extracted_bytes": 64 * 1024 * 1024,
            },
            "assets": [
                {
                    "name": name,
                    "url": f"https://github.com/o/r/releases/download/v1/{name}",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "part": index,
                }
                for index, (name, data) in enumerate(self.parts)
            ],
        }
        # The spec's URLs are the real GitHub Release shape; the test overrides
        # ``_open`` so every request lands on the local fixture server instead.
        # The catalog validator still sees genuine https GitHub URLs.
        self.envelope = sign(make_manifest([self.spec_template]), self.key)

        handler = type("H", (_Handler,), {"blobs": self.blobs})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.catalog_envelope = self.envelope
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.manager = self._make_manager()
        self.manager.manifest_url = f"{self.base}/catalog.json"

    def _make_manager(self, **overrides):
        options = {
            "public_keys": self.keys,
            "catalog_cache": Path(self.tmp.name) / "cache.json",
        }
        options.update(overrides)
        manager = ModelManager(self.models_root, self.staging, **options)

        real_open = manager._open

        def local_open(request, timeout):
            url = request.full_url
            if "github.com/" in url:
                tail = url.split("github.com/", 1)[-1]
                request.full_url = f"{self.base}/{tail}"
            try:
                return real_open(request, timeout)
            finally:
                request.full_url = url

        manager._open = local_open
        return manager

    def _republish(self, spec: dict) -> None:
        """Re-sign a modified spec and hand it to the fixture server."""
        self.server.catalog_envelope = sign(make_manifest([spec]), self.key)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_catalog_is_fetched_and_verified(self) -> None:
        catalog = self.manager.refresh()
        self.assertEqual(catalog.source, "remote")
        self.assertEqual(len(catalog.models), 1)
        self.assertEqual(catalog.key_id, "test-key")

    def test_install_downloads_verifies_and_extracts(self) -> None:
        stages: list[tuple] = []
        result = self.manager.install(
            "vosk-en-us-0.15", lambda *args: stages.append(args)
        )
        self.assertTrue(result.installed)
        target = self.models_root / "vosk-models" / "small_en-us"
        self.assertTrue((target / "am").is_dir())
        self.assertTrue((target / "graph").is_dir())
        self.assertTrue((target / "README").is_file())
        self.assertEqual((target / ".installed.json").exists(), True)

        # Single-root stripping moved the model contents up one level.
        self.assertFalse((target / "vosk-model-small-en-us-0.15").exists())

        kinds = {stage[0] for stage in stages}
        self.assertIn(STAGE_DOWNLOADING, kinds)
        self.assertIn(STAGE_INSTALLED, kinds)
        self.assertTrue(self.manager.is_installed("vosk-en-us-0.15"))
        self.assertIn("Installed", self.manager.status_line(self.manager.spec("vosk-en-us-0.15")))

    def test_second_install_is_a_noop(self) -> None:
        self.manager.install("vosk-en-us-0.15")
        result = self.manager.install("vosk-en-us-0.15")
        self.assertFalse(result.installed)

    def test_require_installed_raises_when_missing(self) -> None:
        with self.assertRaises(ModelNotInstalledError):
            self.manager.require_installed("vosk-en-us-0.15")

    def test_delete_removes_model(self) -> None:
        self.manager.install("vosk-en-us-0.15")
        self.assertTrue(self.manager.is_installed("vosk-en-us-0.15"))
        self.manager.delete("vosk-en-us-0.15")
        self.assertFalse(self.manager.is_installed("vosk-en-us-0.15"))

    def test_wrong_digest_is_rejected_and_nothing_installed(self) -> None:
        bad = dict(self.spec_template)
        bad["assets"] = [dict(asset, sha256="0" * 64) for asset in bad["assets"]]
        self._republish(bad)
        self.manager.refresh()
        with self.assertRaises(ModelError):
            self.manager.install("vosk-en-us-0.15")
        self.assertFalse(self.manager.is_installed("vosk-en-us-0.15"))
        # No partial archive should be left claiming to be valid.
        self.assertFalse(
            any(p.name.endswith(".archive") for p in self.staging.glob("*"))
        )

    def test_missing_marker_files_aborts_install(self) -> None:
        spec = dict(self.spec_template)
        spec["marker_files"] = ["not_present"]
        self._republish(spec)
        self.manager.refresh()
        with self.assertRaises(ModelError):
            self.manager.install("vosk-en-us-0.15")
        self.assertFalse(self.manager.is_installed("vosk-en-us-0.15"))

    def test_tampered_archive_bytes_are_caught(self) -> None:
        """Corrupt a part mid-flight; the per-part digest must reject it."""
        self.blobs["a0.zip"] = b"X" * len(self.blobs["a0.zip"])
        with self.assertRaises(ModelError):
            self.manager.install("vosk-en-us-0.15")
        self.assertFalse(self.manager.is_installed("vosk-en-us-0.15"))

    def test_resume_uses_range_request(self) -> None:
        """A leftover .part file is continued instead of restarted."""
        self.manager.refresh()
        self.staging.mkdir(parents=True, exist_ok=True)
        part0 = self.staging / "vosk-en-us-0.15.a0.zip"
        prefix = self.parts[0][1][:20]
        part0.write_bytes(prefix)
        self.manager.install("vosk-en-us-0.15")
        self.assertTrue(self.manager.is_installed("vosk-en-us-0.15"))
        # The stale part is consumed, not left behind.
        self.assertFalse(part0.exists())

    def test_cancel_stops_the_install(self) -> None:
        self.manager.refresh()
        self.manager.cancel()
        with self.assertRaises(AppError):
            self.manager.install("vosk-en-us-0.15")
        self.assertFalse(self.manager.is_installed("vosk-en-us-0.15"))
        self.manager.reset_cancel()
        self.manager.install("vosk-en-us-0.15")
        self.assertTrue(self.manager.is_installed("vosk-en-us-0.15"))

    def test_keeps_verified_archive_only_when_asked(self) -> None:
        self.manager.install("vosk-en-us-0.15")
        self.assertFalse((self.staging / "vosk-en-us-0.15.archive").exists())
        keep = self._make_manager(keep_archive=True)
        keep.set_catalog(self.manager.catalog)
        keep.delete("vosk-en-us-0.15")
        keep.install("vosk-en-us-0.15")
        self.assertTrue((self.staging / "vosk-en-us-0.15.archive").exists())

    def test_discovery_helpers(self) -> None:
        self.manager.refresh()
        self.assertIsNone(self.manager.find_installed(kind="stt"))
        self.manager.install("vosk-en-us-0.15")
        found = self.manager.find_installed(kind="stt", engine="vosk", language="en-GB")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "vosk-en-us-0.15")
        self.assertIsNone(self.manager.find_installed(kind="tts"))
        self.assertIsNotNone(self.manager.find_any(kind="stt"))
        free, pending = self.manager.disk_report()
        self.assertGreater(free, 0)
        self.assertEqual(pending, 0)

    def test_find_any_language_is_a_preference_by_default(self) -> None:
        """The lenient fallback other callers rely on must keep working."""
        self.manager.refresh()
        fallback = self.manager.find_any(kind="stt", engine="vosk", language="ur")
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.id, "vosk-en-us-0.15")

    def test_find_any_strict_language_refuses_to_guess(self) -> None:
        """"Is there a model for *this* language?" must be able to say no.

        Without strict matching, a language nobody publishes borrows the first
        catalog entry, so callers report an unrelated model as the answer.
        """
        self.manager.refresh()
        self.assertIsNone(
            self.manager.find_any(
                kind="stt", engine="vosk", language="ur", strict_language=True
            )
        )

    def test_find_any_strict_language_still_matches_a_region_prefix(self) -> None:
        self.manager.refresh()
        found = self.manager.find_any(
            kind="stt", engine="vosk", language="en-GB", strict_language=True
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "vosk-en-us-0.15")

    def test_find_any_strict_language_matches_an_exact_tag(self) -> None:
        self.manager.refresh()
        found = self.manager.find_any(
            kind="stt", engine="vosk", language="en-US", strict_language=True
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "vosk-en-us-0.15")

    def test_vosk_status_never_names_an_unrelated_model(self) -> None:
        """A language with no model must not borrow another language's status.

        This reached the user as "The installed 'Vosk English (US)' model is
        missing its expected data - delete it and download it again", for Urdu:
        a language the catalog never published, and a model that was perfectly
        fine. Both the status line and the raised error had to be checked.
        """
        from app.core.vosk_engine import VoskModelManager

        self.manager.refresh()
        self.manager.install("vosk-en-us-0.15")
        engine = VoskModelManager(self.staging / "vosk-layout", "ur", self.manager)

        self.assertIsNone(engine.spec_for("ur"))
        status = engine.catalog_status("ur")
        self.assertIn("no Vosk model published", status)
        self.assertNotIn("Installed", status)
        self.assertNotIn("English", status)

        # A language that *is* published still reports normally.
        self.assertEqual(engine.spec_for("en-US").id, "vosk-en-us-0.15")

    def test_pending_bytes_before_install(self) -> None:
        self.manager.refresh()
        _free, pending = self.manager.disk_report()
        self.assertEqual(pending, len(self.small))


class ExtractionSafetyTests(unittest.TestCase):
    def test_rejects_expansion_bomb(self) -> None:
        blob = build_zip({"big.bin": b"\0" * (6 * 1024 * 1024)})
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.zip"
            archive.write_bytes(blob)
            with self.assertRaises(ModelError):
                extract_zip(archive, Path(tmp) / "out", max_extracted_bytes=1024)

    def test_rejects_empty_archive(self) -> None:
        blob = build_zip({})
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.zip"
            archive.write_bytes(blob)
            with self.assertRaises(ModelError):
                extract_zip(archive, Path(tmp) / "out", max_extracted_bytes=1024 * 1024)

    def test_skips_traversal_members(self) -> None:
        blob = build_zip({"../evil.txt": b"bad", "ok.txt": b"good"})
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.zip"
            archive.write_bytes(blob)
            out = Path(tmp) / "out"
            extract_zip(archive, out, max_extracted_bytes=1024 * 1024)
            self.assertTrue((out / "ok.txt").is_file())
            self.assertFalse((Path(tmp) / "evil.txt").exists())

    def test_rejects_oversized_declared_size(self) -> None:
        blob = build_zip({"a.txt": b"x" * 4096})
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.zip"
            archive.write_bytes(blob)
            with self.assertRaises(ModelError):
                extract_zip(archive, Path(tmp) / "out", max_extracted_bytes=100)


class HelperTests(unittest.TestCase):
    def test_human_size(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1024 * 1024 * 3), "3.0 MB")

    def test_free_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertGreater(free_bytes(Path(tmp)), 0)

    def test_catalog_counts_pending_bytes(self) -> None:
        spec = {
            "id": "m",
            "kind": "stt",
            "name": "M",
            "version": "1",
            "install_dir": "m",
            "optional": True,
            "archive": {"sha256": "a" * 64},
            "assets": [
                {
                    "name": "m.zip",
                    "url": "https://github.com/o/r/m.zip",
                    "size_bytes": 10,
                    "sha256": "b" * 64,
                }
            ],
        }
        catalog = mc.parse_catalog(make_manifest([spec]))
        with TemporaryDirectory() as tmp:
            # Optional models are excluded from "required" totals.
            self.assertEqual(catalog.total_download_bytes(Path(tmp)), 0)
            self.assertEqual(catalog.require("m").download_bytes(), 10)


class TransportSecurityTests(unittest.TestCase):
    """A signed catalog is only as safe as the transport it arrives over."""

    def _manager(self, **kwargs) -> ModelManager:
        with TemporaryDirectory() as tmp:
            return ModelManager(Path(tmp) / "m", Path(tmp) / "s", **kwargs)

    def test_plain_http_manifest_is_refused(self) -> None:
        for url in (
            "http://example.com/catalog.json",
            "ftp://example.com/catalog.json",
            "file:///C:/catalog.json",
        ):
            with self.subTest(url=url), self.assertRaises(ModelError) as ctx:
                self._manager(manifest_url=url)
            self.assertIn("https://", str(ctx.exception))

    def test_plain_http_mirror_is_refused(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._manager(mirror_base="http://mirror.example.com/gh")
        self.assertIn("https://", str(ctx.exception))

    def test_https_manifest_and_mirror_are_accepted(self) -> None:
        manager = self._manager(
            manifest_url="https://example.com/catalog.json",
            mirror_base="https://mirror.example.com/gh/",
        )
        self.assertEqual(manager.manifest_url, "https://example.com/catalog.json")
        self.assertEqual(manager.mirror_base, "https://mirror.example.com/gh")

    def test_mirror_rewrites_github_urls_only(self) -> None:
        manager = self._manager(mirror_base="https://mirror.example.com/gh")
        catalog = mc.parse_catalog(
            make_manifest(
                [
                    {
                        "id": "a",
                        "kind": "stt",
                        "name": "A",
                        "version": "1",
                        "install_dir": "a",
                        "archive": {"sha256": "a" * 64},
                        "assets": [
                            {
                                "name": "a.zip",
                                "url": "https://github.com/o/r/a.zip",
                                "size_bytes": 1,
                                "sha256": "b" * 64,
                            }
                        ],
                    }
                ]
            )
        )
        spec = catalog.require("a")
        self.assertEqual(
            manager._resolve_url(spec, "https://github.com/o/r/a.zip"),
            "https://mirror.example.com/gh/o/r/a.zip",
        )
        # A non-GitHub asset must not be mangled into a nonsense mirror URL.
        self.assertEqual(
            manager._resolve_url(spec, "https://cdn.example.com/a.zip"),
            "https://cdn.example.com/a.zip",
        )


if __name__ == "__main__":
    unittest.main()
