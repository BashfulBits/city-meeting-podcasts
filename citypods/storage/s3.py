"""S3-compatible object storage (Cloudflare R2, Backblaze B2, AWS S3, ...).

All three speak the S3 API via boto3 with a different ``endpoint_url``. Egress is free
on R2 natively, and on B2 when fronted by Cloudflare's CDN (Bandwidth Alliance) — in both
cases ``public_base_url`` is the URL podcast players fetch from.

Requires ``boto3`` (extra: ``citypods[storage]``). Backends are built from env via the
``r2_from_env`` / ``b2_from_env`` presets.
"""

from __future__ import annotations

import os
from pathlib import Path


class S3CompatibleStorage:
    def __init__(
        self,
        *,
        name: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
        region: str = "auto",
    ):
        import boto3  # lazy so the package imports without the extra installed

        self.name = name
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def put_file(self, key: str, local_path: Path, content_type: str) -> str:
        self._client.upload_file(
            str(local_path), self.bucket, key, ExtraArgs={"ContentType": content_type}
        )
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"


def r2_from_env() -> S3CompatibleStorage | None:
    """Cloudflare R2. Env: CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL."""
    try:
        account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        return S3CompatibleStorage(
            name="r2",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
            public_base_url=os.environ["R2_PUBLIC_BASE_URL"],
        )
    except KeyError:
        return None


def b2_from_env() -> S3CompatibleStorage | None:
    """Backblaze B2 (S3 API). Env: B2_ENDPOINT (e.g. https://s3.us-west-004.backblazeb2.com),
    B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_PUBLIC_BASE_URL.

    ``B2_PUBLIC_BASE_URL`` should be your Cloudflare-fronted domain for free egress.
    """
    try:
        endpoint = os.environ["B2_ENDPOINT"]
        return S3CompatibleStorage(
            name="b2",
            endpoint_url=endpoint,
            access_key_id=os.environ["B2_KEY_ID"],
            secret_access_key=os.environ["B2_APP_KEY"],
            bucket=os.environ["B2_BUCKET"],
            public_base_url=os.environ["B2_PUBLIC_BASE_URL"],
            region=_region_from_b2_endpoint(endpoint),
        )
    except KeyError:
        return None


def _region_from_b2_endpoint(endpoint: str) -> str:
    # https://s3.us-west-004.backblazeb2.com -> us-west-004
    host = endpoint.split("://", 1)[-1]
    parts = host.split(".")
    return parts[1] if len(parts) > 2 and parts[0] == "s3" else "auto"
