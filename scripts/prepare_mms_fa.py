#!/usr/bin/env python3
"""Prepare the torchaudio MMS_FA CTC forced-alignment model checkpoint.

Cascade (in order):
  1. Local Actions cache hit — ~0 s, verified model.pt is already present.
  2. B2 mirror               — digest-scoped, internal B2/Cloudflare CDN.
  3. Upstream Meta CDN       — https://dl.fbaipublicfiles.com/mms/torchaudio/.../model.pt
                               On success: mirror to B2 so future runs use step 2.

This ensures Layer 2 CTC alignment evaluation in asr-quality-eval.yml never fails on an
Actions cache miss or flaky upstream download.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

# MMS_FA constants canonical in citypods.ctc_align
from citypods.ctc_align import MMS_FA_FILENAME, MMS_FA_SHA256, MMS_FA_URL

# ── Constants ─────────────────────────────────────────────────────────────────

CHECKPOINT_DIR = Path.home() / ".cache" / "torch" / "hub" / "checkpoints"
SENTINEL = MMS_FA_FILENAME
B2_PREFIX = f"models/mms-fa/{MMS_FA_SHA256}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _complete(directory: Path) -> bool:
    """Return whether *directory* has the expected complete, verified checkpoint."""
    target = directory / SENTINEL
    if not target.exists() or target.stat().st_size == 0:
        return False
    digest = hashlib.sha256()
    with target.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == MMS_FA_SHA256


def _discard_invalid(directory: Path) -> None:
    """Remove a checkpoint that did not match the pinned model identity."""
    target = directory / SENTINEL
    if target.exists() and not _complete(directory):
        print(f"  Removing invalid MMS_FA checkpoint: {target}")
        target.unlink()


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


def _b2_upload(client, bucket: str, file_path: Path) -> None:
    """Upload model file to B2 under B2_PREFIX/."""
    key = f"{B2_PREFIX}/{file_path.name}"
    size_mb = file_path.stat().st_size // 1_000_000
    print(f"  Uploading {file_path.name} ({size_mb} MB) to B2 ({key})…")
    client.upload_file(str(file_path), bucket, key)
    print("  B2 upload complete.")


def _b2_download(client, bucket: str, dest_dir: Path) -> bool:
    """Download model file from B2 to dest_dir. Returns True if complete."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / SENTINEL
    tmp_path = dest_dir / f"{SENTINEL}.part"
    key = f"{B2_PREFIX}/{SENTINEL}"
    print(f"  ← {key} to {target}…")
    client.download_file(bucket, key, str(tmp_path))
    tmp_path.replace(target)
    if _complete(dest_dir):
        return True
    _discard_invalid(dest_dir)
    return False


def _download_upstream(url: str, dest_dir: Path, retries: int = 3) -> bool:
    """Download model file directly from upstream URL with retries and atomic writes."""
    import requests

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / SENTINEL
    tmp_path = dest_dir / f"{SENTINEL}.part"

    for attempt in range(1, retries + 1):
        print(f"  Upstream attempt {attempt}/{retries}: {url} → {target}")
        try:
            r = requests.get(url, stream=True, timeout=300)
            r.raise_for_status()
            size_mb = int(r.headers.get("content-length", 0)) // 1_000_000
            print(f"    Downloading {SENTINEL} ({size_mb} MB)…", flush=True)
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            tmp_path.replace(target)
            if _complete(dest_dir):
                print("  Download complete.")
                return True
            print("    Downloaded checkpoint did not match the pinned SHA256.")
            _discard_invalid(dest_dir)
        except Exception as exc:
            print(f"    Attempt {attempt} failed: {exc}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        if attempt < retries:
            wait = 15 * attempt
            print(f"  Waiting {wait}s before retry…")
            time.sleep(wait)

    return False


# ── Main cascade ──────────────────────────────────────────────────────────────


def main() -> int:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Local cache (restored by actions/cache before this script runs) ──
    if _complete(CHECKPOINT_DIR):
        print(f"[1/3] MMS_FA model found in local cache: {CHECKPOINT_DIR / SENTINEL}")
        return 0
    _discard_invalid(CHECKPOINT_DIR)

    client, bucket = _b2_client()

    # ── Step 2: B2 mirror ─────────────────────────────────────────────────────────
    if client:
        print(f"\n[2/3] Trying B2 mirror ({B2_PREFIX}/{SENTINEL})…")
        try:
            if _b2_has_model(client, bucket):
                if _b2_download(client, bucket, CHECKPOINT_DIR):
                    print("  MMS_FA model ready from B2 mirror.")
                    return 0
                print("  B2 download incomplete.")
            else:
                print("  MMS_FA model not yet mirrored to B2.")
        except Exception as exc:
            print(f"  B2 download failed: {exc}")
    else:
        print("\n[2/3] B2 not configured — skipping B2 mirror step.")

    # ── Step 3: Upstream Meta CDN ─────────────────────────────────────────────────
    print(f"\n[3/3] Downloading MMS_FA checkpoint from upstream: {MMS_FA_URL}…")
    if _download_upstream(MMS_FA_URL, CHECKPOINT_DIR):
        if client:
            if _b2_has_model(client, bucket):
                print("  B2 mirror already up-to-date, skipping upload.")
            else:
                print("  Mirroring MMS_FA to B2 for future runs…")
                try:
                    _b2_upload(client, bucket, CHECKPOINT_DIR / SENTINEL)
                except Exception as exc:
                    print(f"  B2 upload failed (non-fatal): {exc}")
        return 0

    # ── All paths exhausted ───────────────────────────────────────────────────────
    print("\nAll MMS_FA download attempts failed. L2 CTC alignment will be skipped gracefully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
