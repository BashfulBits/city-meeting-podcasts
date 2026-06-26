#!/usr/bin/env python3
"""Prepare the Whisper model for ASR transcription.

Cascade (in order):
  1. Local Actions cache hit  — ~0 s, no download needed.
  2. HuggingFace Hub          — mobiuslabsgmbh/faster-whisper-large-v3-turbo
                                 On success: mirror to B2 so future runs use step 3.
  3. B2 mirror                — models/faster-whisper-large-v3-turbo/ in our own bucket.
  4. HuggingFace fallback     — distil-whisper/distil-large-v3 (HF's own org, separate
                                 rate-limit bucket; slightly slower but reliable).

Writes ASR_MODEL_PATH to $GITHUB_ENV so the enrich step picks it up automatically.
The value is either a local directory path (steps 1-4 all produce one) so
citypods.asr.load_model() can call WhisperModel(path) directly.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

PREFERRED_DIR = Path.home() / ".cache" / "faster-whisper-large-v3-turbo"
FALLBACK_DIR = Path.home() / ".cache" / "faster-whisper-distil-large-v3"

HF_PREFERRED = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
HF_FALLBACK = "distil-whisper/distil-large-v3"

B2_PREFIX = "models/faster-whisper-large-v3-turbo"
SENTINEL = "model.bin"  # largest file; if present the download is complete


# ── Helpers ───────────────────────────────────────────────────────────────────


def _set_env(key: str, value: str) -> None:
    """Write KEY=VALUE to $GITHUB_ENV (or just print it locally)."""
    print(f"{key}={value}")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a") as f:
            f.write(f"{key}={value}\n")


# CTranslate2 faster-whisper models always contain these files.
# Required files must download successfully; optional ones are skipped on 404.
_CT2_FILES_REQUIRED = ["config.json", "model.bin", "tokenizer.json", "vocabulary.json"]


def _complete(directory: Path) -> bool:
    return all((directory / filename).exists() for filename in _CT2_FILES_REQUIRED)


_CT2_FILES_OPTIONAL = [
    "special_tokens_map.json",
    "preprocessor_config.json",
    "tokenizer_config.json",
]


def _hf_download_direct(repo: str, dest: Path, token: str | None = None) -> bool:
    """Download model files directly from the HuggingFace CDN.

    ``snapshot_download`` always hits ``/api/models/{repo}/revision/main`` first —
    a metadata endpoint that is aggressively rate-limited on GitHub Actions IPs
    (429 even with an authenticated token).  The actual file download endpoint
    ``https://huggingface.co/{repo}/resolve/main/{filename}`` is served by a
    separate CDN and is not subject to the same API rate limit.

    We hardcode the well-known file list for CTranslate2 faster-whisper models
    (config.json, model.bin, tokenizer.json, vocabulary.json + optional files)
    to bypass the metadata lookup entirely.
    """
    import requests

    dest.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    for filename in _CT2_FILES_REQUIRED + _CT2_FILES_OPTIONAL:
        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
        required = filename in _CT2_FILES_REQUIRED
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=300)
            if r.status_code == 404:
                if not required:
                    continue  # optional — skip silently
                print(f"    {filename}: 404 (required file missing)")
                return False
            r.raise_for_status()
            size_mb = int(r.headers.get("content-length", 0)) // 1_000_000
            print(f"    {filename}  ({size_mb} MB)…", flush=True)
            tmp_path = dest / f"{filename}.part"
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            tmp_path.replace(dest / filename)
        except Exception as exc:
            if required:
                print(f"    {filename} FAILED (required): {exc}")
                return False
            print(f"    {filename} failed (optional, skipping): {exc}")

    return _complete(dest)


def _hf_download(repo: str, dest: Path, retries: int = 3) -> bool:
    """Download repo to *dest* using direct CDN, with snapshot_download fallback.

    Tries the direct CDN approach first (bypasses the rate-limited metadata API),
    then falls back to ``snapshot_download`` in case file layout differs.
    """
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    dest.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        print(f"  CDN attempt {attempt}/{retries}: {repo} → {dest}")
        if _hf_download_direct(repo, dest, token=token):
            print("  Download complete.")
            return True

        # Fall back to snapshot_download (hits metadata API; may 429 but worth one try)
        if attempt == retries:
            try:
                print("  Trying snapshot_download as last resort…")
                from huggingface_hub import snapshot_download

                snapshot_download(repo, local_dir=str(dest))
                if _complete(dest):
                    print("  snapshot_download complete.")
                    return True
            except Exception as exc:
                print(f"  snapshot_download failed: {exc}")

        if attempt < retries:
            wait = 15 * attempt
            print(f"  Waiting {wait}s before retry…")
            time.sleep(wait)

    return False


def _b2_client():
    """Return (boto3_client, bucket_name) from env, or (None, None) if not configured."""
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=os.environ["B2_ENDPOINT"],
            aws_access_key_id=os.environ["B2_KEY_ID"],
            aws_secret_access_key=os.environ["B2_APP_KEY"],
        )
        return client, os.environ["B2_BUCKET"]
    except (ImportError, KeyError):
        return None, None


def _b2_has_model(client, bucket: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=f"{B2_PREFIX}/{SENTINEL}")
        return True
    except Exception:
        return False


def _b2_upload(client, bucket: str, src: Path) -> None:
    """Upload all files in *src* to B2 under B2_PREFIX/."""
    files = [f for f in src.rglob("*") if f.is_file()]
    print(f"  Uploading {len(files)} file(s) to B2 ({B2_PREFIX}/)…")
    for f in sorted(files):
        rel = f.relative_to(src).as_posix()
        key = f"{B2_PREFIX}/{rel}"
        size_mb = f.stat().st_size // 1_000_000
        print(f"    {rel}  ({size_mb} MB)")
        client.upload_file(str(f), bucket, key)
    print("  B2 upload complete.")


def _b2_download(client, bucket: str, dest: Path) -> bool:
    """Download all objects under B2_PREFIX/ from B2 to *dest*.  Returns True if complete."""
    dest.mkdir(parents=True, exist_ok=True)
    paginator = client.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{B2_PREFIX}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(B2_PREFIX) + 1 :]
            if not rel:
                continue
            local = dest / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            size_mb = obj.get("Size", 0) // 1_000_000
            print(f"  ← {rel}  ({size_mb} MB)")
            tmp_local = local.with_name(f"{local.name}.part")
            client.download_file(bucket, key, str(tmp_local))
            tmp_local.replace(local)
            count += 1
    print(f"  Downloaded {count} file(s) from B2.")
    return _complete(dest)


# ── Main cascade ──────────────────────────────────────────────────────────────


def main() -> int:
    PREFERRED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Local cache (restored by actions/cache before this script runs) ──
    if _complete(PREFERRED_DIR):
        print(f"[1/4] Model found in local cache: {PREFERRED_DIR}")
        _set_env("ASR_MODEL_PATH", str(PREFERRED_DIR))
        return 0

    client, bucket = _b2_client()

    # ── Step 2: HuggingFace Hub (preferred quality model) ────────────────────────
    print(f"\n[2/4] Downloading {HF_PREFERRED} from HuggingFace Hub…")
    if _hf_download(HF_PREFERRED, PREFERRED_DIR):
        _set_env("ASR_MODEL_PATH", str(PREFERRED_DIR))
        # Mirror to B2 so future runs use the faster step 3.
        if client:
            if _b2_has_model(client, bucket):
                print("  B2 mirror already up-to-date, skipping upload.")
            else:
                print("  Mirroring to B2 for future runs…")
                try:
                    _b2_upload(client, bucket, PREFERRED_DIR)
                except Exception as exc:
                    print(f"  B2 upload failed (non-fatal): {exc}")
        return 0

    # ── Step 3: B2 mirror ─────────────────────────────────────────────────────────
    if client:
        print(f"\n[3/4] Trying B2 mirror ({B2_PREFIX}/)…")
        try:
            if _b2_has_model(client, bucket):
                if _b2_download(client, bucket, PREFERRED_DIR):
                    print("  Model ready from B2 mirror.")
                    _set_env("ASR_MODEL_PATH", str(PREFERRED_DIR))
                    return 0
                print("  B2 download incomplete (model.bin missing).")
            else:
                print("  Model not yet mirrored to B2.")
        except Exception as exc:
            print(f"  B2 download failed: {exc}")
    else:
        print("\n[3/4] B2 not configured — skipping.")

    # ── Step 4: distil-large-v3 fallback (HF's own org, separate rate-limit bucket) ──
    print(f"\n[4/4] Falling back to {HF_FALLBACK} (slightly slower, more reliable)…")
    FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    if _hf_download(HF_FALLBACK, FALLBACK_DIR, retries=3):
        print("  distil-large-v3 ready.")
        _set_env("ASR_MODEL_PATH", str(FALLBACK_DIR))
        return 0

    # ── All paths exhausted ───────────────────────────────────────────────────────
    print("\nAll download attempts failed. ASR will be skipped gracefully this run.")
    # Don't set ASR_MODEL_PATH — asr.load_model() will use city.asr_model from config
    # and fail gracefully with one error per source. Exit 0: this is the documented
    # graceful-degradation path, not a CI job failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
