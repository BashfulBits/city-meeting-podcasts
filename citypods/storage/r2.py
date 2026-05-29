"""Cloudflare R2 storage backend (S3-compatible, zero egress fees).

Requires ``boto3`` (install extra: ``citypods[storage]``) and these env vars:
  CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
  R2_BUCKET, R2_PUBLIC_BASE_URL  (the bucket's public dev URL or custom domain)
"""

from __future__ import annotations

import os
from pathlib import Path


class R2Storage:
    name = "r2"

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
    ):
        import boto3  # imported lazily so the package works without the extra

        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    @classmethod
    def from_env(cls) -> R2Storage | None:
        """Build from environment, or return None if not fully configured."""
        try:
            return cls(
                account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
                access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                bucket=os.environ["R2_BUCKET"],
                public_base_url=os.environ["R2_PUBLIC_BASE_URL"],
            )
        except KeyError:
            return None

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def put_file(self, key: str, local_path: Path, content_type: str) -> str:
        self._client.upload_file(
            str(local_path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"
