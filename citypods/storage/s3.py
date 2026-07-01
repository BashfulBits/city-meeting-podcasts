"""S3-compatible object storage (Cloudflare R2, Backblaze B2, AWS S3, ...).

All three speak the S3 API via boto3 with a different ``endpoint_url``. Egress is free
on R2 natively, and on B2 when fronted by Cloudflare's CDN (Bandwidth Alliance) — in both
cases ``public_base_url`` is the URL podcast players fetch from.

Requires ``boto3`` (extra: ``citypods[storage]``). Backends are built from env via the
``r2_from_env`` / ``b2_from_env`` presets.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class CASConflict(Exception):
    """Raised when a conditional write (compare-and-swap) is rejected by the backend.

    Maps the S3 ``PreconditionFailed`` (HTTP 412) returned when an ``If-Match`` /
    ``If-None-Match`` precondition no longer holds — i.e. another writer changed the
    object first. Callers re-read the current state and retry (bounded backoff + jitter),
    or treat the conflict as "someone else won the claim" (Stage-2 lease ledger).
    """

    def __init__(self, key: str):
        super().__init__(f"compare-and-swap conflict on {key!r}")
        self.key = key


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
        cas_capable: bool = False,
    ):
        import boto3  # lazy so the package imports without the extra installed

        self.name = name
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        # ``put_cas``/``get_bytes`` exist on every instance, but only R2 *enforces* the conditional
        # headers — B2 silently ignores them, so it is NOT real compare-and-swap. Callers that need
        # atomicity must gate on ``cas_capable`` (set True only for R2 via ``r2_from_env``), so a
        # B2-backed deployment falls back to the bulk-synced local-file path instead.
        self.cas_capable = cas_capable
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

    # --- compare-and-swap (optional StorageBackend capability) ---

    def get_bytes(self, key: str) -> tuple[bytes, str] | None:
        """Read an object's bytes and current ETag, or ``None`` if absent.

        The ETag pairs with :meth:`put_cas`'s ``if_match`` for a compare-and-swap
        read-modify-write. This is a Class-B (read) op on R2.
        """
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("NoSuchKey", "404") or status == 404:
                return None
            raise
        body = resp["Body"]
        try:
            return body.read(), resp["ETag"]
        finally:
            body.close()

    def put_cas(
        self,
        key: str,
        data: bytes,
        content_type: str,
        *,
        if_none_match: str | None = None,
        if_match: str | None = None,
    ) -> tuple[str, str]:
        """Conditional PUT (compare-and-swap). Returns ``(public_url, etag)``.

        ``if_none_match="*"`` creates the object only if absent (claim-if-free).
        ``if_match=<etag>`` updates only if the current ETag matches (lost-update guard).
        Pass the ETag exactly as a prior call returned it (boto3 includes the surrounding
        quotes). Raises :class:`CASConflict` on a 412 ``PreconditionFailed``.

        Native boto3 ``IfNoneMatch``/``IfMatch`` params are used (confirmed against R2 on
        boto3 1.43 by the review/17 §7 spike — no header injection needed).
        """
        from botocore.exceptions import ClientError

        kwargs: dict = dict(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        if if_none_match is not None:
            kwargs["IfNoneMatch"] = if_none_match
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            resp = self._client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("PreconditionFailed", "ConditionalRequestConflict") or status == 412:
                raise CASConflict(key) from exc
            raise
        return self.public_url(key), resp["ETag"]

    def get_file(self, key: str, local_path: Path) -> bool:
        import boto3
        from botocore.exceptions import (
            ClientError,
            ConnectionClosedError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
        from s3transfer.exceptions import RetriesExceededError as TransferRetriesExceededError

        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        transient_errors = (
            boto3.exceptions.RetriesExceededError,
            TransferRetriesExceededError,
            EndpointConnectionError,
            ReadTimeoutError,
            ConnectionClosedError,
        )
        for attempt in range(3):
            try:
                self._client.download_file(self.bucket, key, str(local_path))
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in ("NoSuchKey", "404") or status == 404:
                    return False
                raise
            except transient_errors:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def get_range(self, key: str, start: int, end: int) -> bytes | None:
        """Partial GET via the standard HTTP ``Range`` header — a Class-B op, far cheaper than
        ``get_file`` for a header-only read (e.g. an MP4 ``moov`` duration probe)."""
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("NoSuchKey", "404") or status in (404, 416):
                return None
            raise
        body = resp["Body"]
        try:
            return body.read()
        finally:
            body.close()

    # --- orphan GC support (optional StorageBackend capability) ---

    def list_objects(self, prefix: str = ""):
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"], obj.get("LastModified")

    def iter_objects(self, prefix: str = ""):
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"], obj.get("LastModified"), obj.get("Size")

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


def r2_from_env() -> S3CompatibleStorage | None:
    """Cloudflare R2. Env: CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL.

    Optional: R2_ENDPOINT overrides the default endpoint (needed for
    jurisdiction-specific buckets, e.g. https://<id>.eu.r2.cloudflarestorage.com).
    """
    try:
        account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        endpoint = os.environ.get("R2_ENDPOINT", f"https://{account}.r2.cloudflarestorage.com")
        return S3CompatibleStorage(
            name="r2",
            endpoint_url=endpoint,
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
            public_base_url=os.environ["R2_PUBLIC_BASE_URL"],
            cas_capable=True,  # R2 enforces If-Match/If-None-Match (real compare-and-swap)
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
