"""Publish a model to a GitHub Release and update the signed catalog.

This is the only place a private signing key is used. The desktop app never
sees it: it only embeds the matching public key and verifies what this tool
produces.

Typical one-off use::

    python tools/publish_model.py \\
        --repo owner/repo --tag models-2026-01 \\
        --id vosk-small-en-us-0.15 --kind stt --name "Vosk English (US) small" \\
        --version 0.15 --engine vosk --languages en-US \\
        --install-dir vosk-models/small_en-us \\
        --marker am --marker graph \\
        --source ./vosk-model-small-en-us-0.15 \\
        --key ~/.keys/model-catalog.key --key-id model-2026

The script then, in order:

1. packs ``--source`` into a deterministic zip,
2. splits it into ``--part-size`` chunks that fit under GitHub's 2 GB per-asset
   limit (models larger than 2 GB are therefore still publishable),
3. uploads each chunk to a GitHub Release with a stable, sorted name,
4. computes the SHA-256 of the archive and of every chunk,
5. inserts or updates the model entry in ``catalog.json``,
6. re-signs the whole catalog and writes ``catalog.json``.

Re-running the command for the same ``--id`` replaces that entry, so publishing
a new version of a model is the same command with a new ``--version``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "tools" / "catalog.json"
CATALOG_VERSION = 1

#: GitHub rejects a single Release asset above this size.
GITHUB_MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 32 * 1024 * 1024
DEFAULT_PART_BYTES = 1_900_000_000
CHUNK = 1024 * 1024
API = "https://api.github.com"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_private_key(path: Path):
    from cryptography.hazmat.primitives import serialization

    raw = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        key = serialization.load_ssh_private_key(raw, password=None)
    if key.__class__.__name__ != "Ed25519PrivateKey":
        raise SystemExit("The catalog signing key must be an Ed25519 private key.")
    return key


def public_key_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(
            f"'{name}' was not found on PATH. Install the GitHub CLI "
            "(https://cli.github.com) and run 'gh auth login' first."
        )
    return path


def gh_json(args: list[str]) -> Any:
    """Run a ``gh api`` call and return the decoded JSON, if any."""
    result = subprocess.run(
        [require_tool("gh"), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(args[:2])} failed:\n{result.stderr.strip()}"
        )
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout


# --------------------------------------------------------------------------- #
# Packing and chunking
# --------------------------------------------------------------------------- #
def pack_zip(source: Path) -> Path:
    """Create a deterministic zip of ``source``'s contents.

    Timestamps are pinned so republishing the same tree yields the same digest,
    which keeps the catalog stable and diffable. Files are streamed into the
    archive one at a time, because a single Qwen3-TTS safetensors shard is
    several gigabytes and must never be read into memory.
    """
    source = source.resolve()
    if not source.is_dir():
        raise SystemExit(f"--source must be a directory: {source}")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        archive = Path(handle.name)

    entries: list[tuple[str, Path]] = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        entries.append((path.relative_to(source).as_posix(), path))

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, path in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(path, "rb") as source_handle, zf.open(info, "w") as target:
                shutil.copyfileobj(source_handle, target, CHUNK)
            print(f"  packed {name} ({human_size(path.stat().st_size)})")
    return archive


def split_file(archive: Path, part_bytes: int, workdir: Path) -> list[Path]:
    if part_bytes <= 0 or part_bytes > GITHUB_MAX_ASSET_BYTES:
        raise SystemExit(
            f"--part-size must be between 1 and {GITHUB_MAX_ASSET_BYTES} bytes."
        )
    parts: list[Path] = []
    with open(archive, "rb") as handle:
        index = 0
        while True:
            part = workdir / f"part{index:03d}.bin"
            written = 0
            with open(part, "wb") as sink:
                while written < part_bytes:
                    chunk = handle.read(min(CHUNK, part_bytes - written))
                    if not chunk:
                        break
                    sink.write(chunk)
                    written += len(chunk)
            if written == 0:
                # Nothing left after the previous part: drop the empty tail.
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            index += 1
            print(f"  split {index}: {human_size(written)} -> {part.name}")
    if not parts:
        raise SystemExit("The archive is empty; nothing to publish.")
    if len(parts) == 1:
        print("  single asset (no chunking needed)")
    return parts


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
@dataclass
class Release:
    repo: str
    tag: str
    id: int
    upload_url: str


def ensure_release(repo: str, tag: str, name: str, notes: str) -> Release:
    existing = gh_json(
        ["api", f"repos/{repo}/releases/tags/{tag}", "--silent"]
    )
    if isinstance(existing, dict) and existing.get("upload_url"):
        print(f"  reusing release {tag}")
        return Release(repo, tag, int(existing["id"]), existing["upload_url"])

    print(f"  creating release {tag} in {repo}")
    created = gh_json(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/releases",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"name={name}",
            "-f",
            f"body={notes}",
            "-f",
            "draft=false",
            "-f",
            "prerelease=false",
        ]
    )
    if not isinstance(created, dict) or "upload_url" not in created:
        raise SystemExit("GitHub did not return a release to upload to.")
    return Release(repo, tag, int(created["id"]), created["upload_url"])


def existing_asset_ids(release: Release) -> dict[str, int]:
    listing = gh_json(
        ["api", f"repos/{release.repo}/releases/{release.id}/assets", "--paginate"]
    )
    if not isinstance(listing, list):
        return {}
    return {str(item.get("name")): int(item.get("id", 0)) for item in listing}


def upload_asset(release: Release, asset_name: str, path: Path, asset_id: int | None) -> str:
    url = f"{release.upload_url.split('{')[0]}"
    if asset_id:
        print(f"  replacing asset {asset_name}")
        deleted = subprocess.run(
            [
                require_tool("gh"),
                "api",
                "--method",
                "DELETE",
                f"repos/{release.repo}/releases/assets/{asset_id}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if deleted.returncode != 0:
            raise SystemExit(f"Could not delete the old asset {asset_name}.")

    print(f"  uploading {asset_name} ({human_size(path.stat().st_size)})")
    # gh api handles the binary upload and its own retries, so we do not have
    # to stream a multi-gigabyte body through Python.
    result = subprocess.run(
        [
            require_tool("gh"),
            "api",
            "--method",
            "POST",
            url,
            "-H",
            "Content-Type: application/octet-stream",
            "--input",
            str(path),
            "-F",
            f"name={asset_name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Uploading {asset_name} failed:\n{result.stderr.strip()}")
    return f"{url}/{asset_name}"


def download_url(repo: str, tag: str, asset_name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def load_catalog(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "type": "model_catalog",
            "version": CATALOG_VERSION,
            "generated_at": "",
            "models": [],
        }
    # Accept both a bare payload and a previously signed envelope.
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return raw["payload"]
    return raw


def upsert_entry(
    catalog: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    models = [m for m in catalog.get("models", []) if m.get("id") != entry["id"]]
    models.append(entry)
    models.sort(key=lambda item: (item.get("kind", ""), item.get("name", "").lower()))
    catalog["models"] = models
    return catalog


def sign_catalog(catalog: dict[str, Any], key, key_id: str) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from app.core.model_catalog import sign_catalog as _sign

    return _sign(catalog, key, key_id)


def write_catalog(path: Path, envelope: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a model to GitHub Releases and sign the catalog"
    )
    # --print-public-key short-circuits before anything else runs, so it must not
    # force the caller to supply the publishing arguments.
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument("--tag", help="Release tag for the assets")
    parser.add_argument("--release-name", default="", help="Release title")
    parser.add_argument("--notes", default="", help="Release notes")
    parser.add_argument("--id", help="stable catalog id")
    parser.add_argument("--kind", choices=["stt", "tts", "translation", "clone"])
    parser.add_argument("--name", help="display name")
    parser.add_argument("--version")
    parser.add_argument("--description", default="")
    parser.add_argument("--engine", default="")
    parser.add_argument("--languages", nargs="*", default=[])
    parser.add_argument("--requires", nargs="*", default=[])
    parser.add_argument(
        "--marker",
        nargs="+",
        action="append",
        default=[],
        dest="marker_groups",
        help="marker file/directory that must exist after extraction; repeatable",
    )
    parser.add_argument(
        "--install-dir", help="destination under the models root"
    )
    parser.add_argument(
        "--no-strip-root",
        action="store_true",
        help="keep the archive's top-level folder instead of stripping it",
    )
    parser.add_argument(
        "--max-extracted-bytes",
        type=int,
        default=0,
        help="hard cap on the decompressed size (0 = tool default)",
    )
    parser.add_argument("--optional", action="store_true")
    parser.add_argument("--source", help="directory to publish")
    parser.add_argument(
        "--part-size", type=int, default=DEFAULT_PART_BYTES, help="bytes per part"
    )
    parser.add_argument("--key", required=True, help="Ed25519 private key (PEM/SSH)")
    parser.add_argument("--key-id", required=True, help="signing key id")
    parser.add_argument(
        "--catalog", default=str(DEFAULT_CATALOG), help="catalog.json to update"
    )
    parser.add_argument(
        "--print-public-key",
        action="store_true",
        help="print the public key and exit (no upload)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="pack and compute digests, but do not upload or sign",
    )
    return parser


#: Arguments that are only needed for a real publish, not for --print-public-key.
_PUBLISH_REQUIRED = (
    "repo",
    "tag",
    "id",
    "kind",
    "name",
    "version",
    "install_dir",
    "source",
)


def _check_publish_args(args: argparse.Namespace) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in _PUBLISH_REQUIRED if not getattr(args, name, None)]
    if missing:
        raise SystemExit(
            "Publishing needs these arguments: " + ", ".join(missing) + "\n"
            "(pass --print-public-key to only show the public key)"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    key = load_private_key(Path(args.key).expanduser())

    if args.print_public_key:
        print(f"key_id: {args.key_id}")
        print(f"public_key (hex): {public_key_hex(key)}")
        print()
        print("Embed in the build as AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS:")
        print(f'  {{"{args.key_id}": "{public_key_hex(key)}"}}')
        return 0

    _check_publish_args(args)
    archive = pack_zip(Path(args.source).expanduser())
    try:
        return publish(args, key, archive)
    finally:
        # pack_zip writes to a NamedTemporaryFile; without this a multi-gigabyte
        # archive would stay in %TEMP% after every publish.
        archive.unlink(missing_ok=True)


def publish(args: argparse.Namespace, key, archive: Path) -> int:
    archive_digest = file_digest(archive)
    archive_size = archive.stat().st_size
    print(f"archive: {human_size(archive_size)}  sha256={archive_digest}")

    catalog_path = Path(args.catalog).expanduser()
    catalog = load_catalog(catalog_path)

    with tempfile.TemporaryDirectory(prefix="publish-model-") as tmp:
        workdir = Path(tmp)
        parts = split_file(archive, args.part_size, workdir)
        asset_prefix = f"{args.id}.zip"
        names = (
            [asset_prefix]
            if len(parts) == 1
            else [f"{asset_prefix}.{index:03d}" for index in range(len(parts))]
        )

        assets: list[dict[str, Any]] = []
        release_id: int | None = None
        if not args.dry_run:
            release = ensure_release(
                args.repo,
                args.tag,
                args.release_name or f"Models {args.tag}",
                args.notes or f"Model assets for {args.name}.",
            )
            release_id = release.id
            known = existing_asset_ids(release)
            for part, name in zip(parts, names):
                upload_asset(release, name, part, known.get(name))
        else:
            print("  dry run: skipping upload")

        for index, (part, name) in enumerate(zip(parts, names)):
            assets.append(
                {
                    "name": name,
                    "url": download_url(args.repo, args.tag, name),
                    "size_bytes": part.stat().st_size,
                    "sha256": file_digest(part),
                    "part": index,
                }
            )

    entry: dict[str, Any] = {
        "id": args.id,
        "kind": args.kind,
        "name": args.name,
        "version": args.version,
        "install_dir": args.install_dir,
        "size_bytes": archive_size,
        "archive": {
            "format": "zip",
            "sha256": archive_digest,
            "strip_single_root": not args.no_strip_root,
        },
        "assets": assets,
    }
    if args.max_extracted_bytes:
        entry["archive"]["max_extracted_bytes"] = args.max_extracted_bytes
    if args.description:
        entry["description"] = args.description
    if args.engine:
        entry["engine"] = args.engine
    if args.languages:
        entry["languages"] = list(args.languages)
    if args.requires:
        entry["requires"] = list(args.requires)
    markers = [marker for group in args.marker_groups for marker in group]
    if markers:
        entry["marker_files"] = markers
    if args.optional:
        entry["optional"] = True

    upsert_entry(catalog, entry)
    catalog["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if args.dry_run:
        print()
        print("Dry run. The entry that would be published:")
        print(json.dumps(entry, indent=2, sort_keys=True))
        print()
        print(f"Catalog would be signed with key id '{args.key_id}'.")
        return 0

    envelope = sign_catalog(catalog, key, args.key_id)
    write_catalog(catalog_path, envelope)

    print()
    print(f"Published '{args.name}' v{args.version} to {args.repo}@{args.tag}")
    print(f"  {len(assets)} asset(s), {human_size(archive_size)} total")
    print(f"  catalog: {catalog_path}")
    print(f"  public key id: {args.key_id}")
    if release_id:
        print(f"  https://github.com/{args.repo}/releases/tag/{args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
