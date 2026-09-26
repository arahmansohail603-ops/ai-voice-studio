# Publishing models to the catalog

The desktop app installs models from a **signed catalog**. This folder holds the
tooling that produces it; the app itself never has a private key.

## One-time setup

1. Create an Ed25519 signing key and keep it offline or in a secrets manager:

   ```bash
   openssl genpkey -algorithm ed25519 -out model-catalog.key
   ```

2. Print the public half and record the `key_id` you will use:

   ```bash
   python tools/publish_model.py --key model-catalog.key --key-id model-2026 --print-public-key
   ```

3. Give the public key to the build as `AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS`:

   ```
   AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS={"model-2026":"<hex>"}
   ```

   The model key is deliberately separate from the licensing key so that
   rotating one has no effect on the other, and a leaked model key cannot mint
   licenses.

4. Authenticate the GitHub CLI: `gh auth login` (needs `repo` scope for private
   repos; public repos work with no scope).

## Publishing a model

Always dry-run first. It packs, chunks and computes every digest without
uploading anything:

```bash
python tools/publish_model.py \
    --repo OWNER/REPO --tag models-2026-01 \
    --id vosk-small-en-us-0.15 \
    --kind stt --name "Vosk English (US) small" --version 0.15 \
    --engine vosk --languages en-US --requires vosk \
    --install-dir vosk-models/small_en-us \
    --marker am --marker graph \
    --source ./vosk-model-small-en-us-0.15 \
    --key model-catalog.key --key-id model-2026 \
    --dry-run
```

Drop `--dry-run` to publish. The tool will:

1. pack `--source` into a deterministic zip (fixed timestamps, so republishing
   the same tree gives the same digest),
2. split it into parts that fit under GitHub's 2 GB per-asset limit,
3. create the Release if it is missing, else reuse it,
4. replace any existing asset of the same name,
5. upsert the model entry in `tools/catalog.json` by `--id`,
6. re-sign the whole catalog and write it out.

Updating an existing model is the same command with a new `--version`.

### Catalog fields worth getting right

| Flag | Why it matters |
|---|---|
| `--install-dir` | Relative path under `AI_VOICE_STUDIO_DATA/models`. Must be POSIX-style; traversal is rejected. |
| `--marker` | Files that must exist after extraction. A wrong marker makes the install fail loudly instead of installing a broken model. |
| `--no-strip-root` | Use when the zip has no single top-level folder to strip. |
| `--max-extracted-bytes` | Hard cap on the decompressed size. Use it to limit a decompression-bomb risk. |
| `--part-size` | Defaults to 1.9 GB, safely under the GitHub limit. |
| `--optional` | Optional models are excluded from the "still to download" total. |

## Wiring the app to the published catalog

```
AI_VOICE_STUDIO_MODEL_MANIFEST_URL=https://raw.githubusercontent.com/OWNER/REPO/main/catalog.json
AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS={"model-2026":"<hex>"}
```

The app verifies the signature, then every download against the digests inside.
Airgapped deployments can set `AI_VOICE_STUDIO_MODEL_MIRROR` to an internal
HTTPS host that mirrors the same paths.

## `tools/catalog.example.json`

A hand-written example of the schema, useful when writing a catalog by hand or
generating one from another script. It is **not** signed and must not be shipped
as a live catalog — use `tools/publish_model.py` to produce the real file.
