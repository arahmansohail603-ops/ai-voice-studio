"""The publisher and the app must agree on the catalog format.

``tools/publish_model.py`` is the only thing allowed to touch the signing key,
and it writes the catalog the desktop app then downloads and verifies. If the
two ever drift -- asset naming, part ordering, digest fields, the signed
envelope -- models silently fail to install in production. This test runs the
real publisher in ``--dry-run`` mode and feeds its entry through the real
verifier and downloader.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from typing import ClassVar

from app.core.errors import ModelError
from app.core.model_catalog import (
    parse_public_keys,
    sign_catalog,
    verify_catalog,
)
from app.core.model_manager import ModelManager

_ROOT = Path(__file__).resolve().parent.parent
_PUBLISHER = _ROOT / "tools" / "publish_model.py"

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _CRYPTO = True
except ImportError:  # pragma: no cover
    _CRYPTO = False


class _FakeResponse:
    """Just enough of an http.client response for ModelManager._download_part."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._pos = 0
        self.status = 200

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._pos = self._body[self._pos :], len(self._body)
            return chunk
        chunk = self._body[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _load_publisher():
    """Import tools/publish_model.py without requiring tools to be a package."""
    spec = importlib.util.spec_from_file_location("publish_model", _PUBLISHER)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules, so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(_CRYPTO, "cryptography is required for signing tests")
@unittest.skipUnless(_PUBLISHER.is_file(), "tools/publish_model.py is missing")
class PublishToAppContractTests(unittest.TestCase):
    publisher: ClassVar = None
    key: ClassVar = None
    key_id: ClassVar = "model-test-2026"

    @classmethod
    def setUpClass(cls):
        cls.publisher = _load_publisher()
        cls.key = Ed25519PrivateKey.generate()
        cls.key_id = "model-test-2026"

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        # The tool is chatty on purpose; keep the test output readable.
        quiet = unittest.mock.patch.object(
            self.publisher, "print", lambda *a, **k: None
        )
        quiet.start()
        self.addCleanup(quiet.stop)
        self.source = self._tmp / "model"
        (self.source / "am").mkdir(parents=True)
        (self.source / "graph").mkdir(parents=True)
        for name, seed in (
            ("am/final.mdl", 1),
            ("graph/words.fst", 2),
            ("conf.json", 3),
        ):
            (self.source / name).write_bytes(random.Random(seed).randbytes(40000))

    def _run_publisher(self, **overrides) -> dict:
        """Run --dry-run and return the entry the tool would publish."""
        args = {
            "--key": str(self._tmp / "unused.key"),
            "--key-id": self.key_id,
            "--repo": "owner/repo",
            "--tag": "models-test",
            "--id": "stt-vosk-en",
            "--kind": "stt",
            "--name": "Vosk English small",
            "--version": "0.15",
            "--engine": "vosk",
            "--languages": "en-US",
            "--install-dir": "vosk-models/small_en-us",
            "--marker": ["am", "graph"],
            "--source": str(self.source),
            "--catalog": str(self._tmp / "catalog.json"),
            "--part-size": "30000",
            "--dry-run": True,
        }
        args.update(overrides)

        argv = []
        for key, value in args.items():
            if value is True:
                argv.append(key)
            elif isinstance(value, (list, tuple)) and any(
                isinstance(item, (list, tuple)) for item in value
            ):
                # A repeated-flag form: emit "--key v1 --key v2 ...".
                for group in value:
                    argv.append(key)
                    argv.extend(str(item) for item in group)
            elif isinstance(value, (list, tuple)):
                argv.append(key)
                argv.extend(str(item) for item in value)
            else:
                argv.extend([key, str(value)])

        captured = []

        def fake_print(*parts, **kwargs):
            captured.append(" ".join(str(p) for p in parts))

        with unittest.mock.patch.object(
            self.publisher, "load_private_key", return_value=self.key
        ), unittest.mock.patch.object(
            self.publisher, "print", side_effect=fake_print
        ):
            self.assertEqual(self.publisher.main(argv), 0)

        text = "\n".join(captured)
        start = text.index("{\n")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])

    # ------------------------------------------------------------------ tests
    def test_every_marker_flag_reaches_the_catalog(self):
        """The documented ``--marker am --marker graph`` form must keep both.

        ``--marker`` used to be declared with ``nargs="*"`` and no ``append``,
        so a repeated flag silently dropped every value but the last -- quietly
        weakening the app's post-extraction integrity check.
        """
        for overrides in (
            {"--marker": ["am", "graph"]},
            {"--marker": [["am"], ["graph"]]},
            {"--marker": [["am", "graph"]]},
        ):
            with self.subTest(overrides=overrides):
                entry = self._run_publisher(**overrides)
                self.assertEqual(entry["marker_files"], ["am", "graph"])

    def test_published_entry_parses_as_a_catalog_model(self):
        entry = self._run_publisher()
        from app.core.model_catalog import parse_catalog

        catalog = parse_catalog(
            {
                "type": "model_catalog",
                "version": 1,
                "generated_at": "2026-01-01T00:00:00Z",
                "models": [entry],
            }
        )
        spec = catalog.require("stt-vosk-en")
        self.assertEqual(spec.install_dir, "vosk-models/small_en-us")
        self.assertTrue(spec.is_chunked)
        self.assertEqual([a.part for a in spec.assets], list(range(len(entry["assets"]))))

    def test_signed_catalog_from_the_publisher_verifies_in_the_app(self):
        entry = self._run_publisher()
        payload = {
            "type": "model_catalog",
            "version": 1,
            "generated_at": "2026-01-01T00:00:00Z",
            "models": [entry],
        }
        envelope = sign_catalog(payload, self.key, self.key_id)
        public_hex = self.publisher.public_key_hex(self.key)
        keys = parse_public_keys({self.key_id: public_hex})

        verified = verify_catalog(envelope, keys)
        self.assertEqual([spec.id for spec in verified.models], ["stt-vosk-en"])
        self.assertEqual(verified.key_id, self.key_id)

        # A tampered catalog must not verify with the same key.
        tampered = json.loads(json.dumps(envelope))
        tampered["payload"]["models"][0]["version"] = "9.99"
        with self.assertRaises(ModelError):
            verify_catalog(tampered, keys)

        # ...and neither must an unknown key.
        other = Ed25519PrivateKey.generate()
        with self.assertRaises(ModelError):
            verify_catalog(
                envelope, parse_public_keys({"other": self.publisher.public_key_hex(other)})
            )

    def test_published_asset_digests_match_a_real_download(self):
        """Re-pack the same tree the way the tool does and check the digests."""
        entry = self._run_publisher()
        # The tool deleted its temp archive, so rebuild the identical zip here
        # using its own helper and compare digests and per-part hashes.
        archive = self.publisher.pack_zip(self.source)
        self.addCleanup(archive.unlink, True)
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            entry["archive"]["sha256"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            parts = self.publisher.split_file(archive, 30000, Path(tmp))
            self.assertEqual(len(parts), len(entry["assets"]))
            for part, asset in zip(parts, entry["assets"]):
                self.assertEqual(part.stat().st_size, asset["size_bytes"])
                self.assertEqual(
                    self.publisher.file_digest(part), asset["sha256"], asset["name"]
                )
            # The parts must reassemble into the archive the catalog advertises.
            joined = b"".join(path.read_bytes() for path in parts)
            self.assertEqual(
                hashlib.sha256(joined).hexdigest(), entry["archive"]["sha256"]
            )

    def test_chunked_archive_installs_through_the_manager(self):
        """End to end: publisher entry + real asset bytes -> installed model."""
        from app.core.model_catalog import parse_catalog

        archive = self.publisher.pack_zip(self.source)
        self.addCleanup(archive.unlink, True)
        blob = archive.read_bytes()
        part_bytes = 30000
        with tempfile.TemporaryDirectory() as tmp:
            parts = self.publisher.split_file(archive, part_bytes, Path(tmp))
            entry = self._run_publisher()
            self.assertEqual(len(parts), len(entry["assets"]))

            assets = []
            for index, (part, name) in enumerate(
                zip(parts, [a["name"] for a in entry["assets"]])
            ):
                (self._tmp / f"dl{index}").write_bytes(part.read_bytes())
                assets.append(
                    {
                        "name": name,
                        "url": f"https://github.com/owner/repo/{name}",
                        "size_bytes": part.stat().st_size,
                        "sha256": self.publisher.file_digest(part),
                        "part": index,
                    }
                )

            spec = dict(entry)
            spec["assets"] = assets
            spec["archive"] = {
                "format": "zip",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "strip_single_root": False,
            }
            manager = ModelManager(self._tmp / "models", self._tmp / "staging")
            manager.set_catalog(
                parse_catalog(
                    {
                        "type": "model_catalog",
                        "version": 1,
                        "generated_at": "2026-01-01T00:00:00Z",
                        "models": [spec],
                    }
                )
            )

            served = 0

            def fake_open(request, timeout):
                nonlocal served
                name = request.full_url.rsplit("/", 1)[-1]
                data = {
                    asset["name"]: (self._tmp / f"dl{index}").read_bytes()
                    for index, asset in enumerate(assets)
                }[name]
                served += 1
                return _FakeResponse(data)

            manager._open = fake_open
            result = manager.install("stt-vosk-en", lambda *a: None)

            self.assertTrue(result.installed)
            self.assertEqual(served, len(assets))
            target = manager.models_root / "vosk-models" / "small_en-us"
            self.assertTrue((target / "am" / "final.mdl").is_file())
            self.assertTrue((target / "graph" / "words.fst").is_file())
            self.assertEqual(
                json.loads((target / ".installed.json").read_text())["version"],
                "0.15",
            )

    def test_publisher_cleans_up_its_temp_archive(self):
        before = set(Path(tempfile.gettempdir()).glob("*.zip"))
        self._run_publisher()
        after = set(Path(tempfile.gettempdir()).glob("*.zip"))
        # The publisher builds a full multi-GB zip in the temp dir; it must not
        # survive a publish.
        self.assertEqual(after - before, set())

    def test_packer_does_not_read_whole_files_into_memory(self):
        """A huge single file must stream; regressing to read_bytes() would OOM."""
        big = self.source / "huge.bin"
        big.write_bytes(os.urandom(3 * 1024 * 1024))
        reads: list[int] = []
        real_open = io.open

        def spy(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            name = str(file)
            if "b" in mode and name.endswith("huge.bin"):
                inner = handle.read

                def read(size=-1):
                    reads.append(size)
                    return inner(size)

                handle.read = read
            return handle

        with unittest.mock.patch("builtins.open", spy):
            archive = self.publisher.pack_zip(self.source)
        self.addCleanup(archive.unlink, True)
        # Every read must be bounded; nothing may pull the whole file at once.
        self.assertTrue(reads, "the packer did not stream the file")
        self.assertLessEqual(max(reads), self.publisher.CHUNK)
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(zf.getinfo("huge.bin").file_size, big.stat().st_size)
