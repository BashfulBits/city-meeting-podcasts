#!/usr/bin/env python
"""R2 conditional-write (CAS) spike — acceptance-criteria runner.

Tests boto3/botocore conditional PUT against a Cloudflare R2 bucket and
captures the mechanism, outcomes, and latency numbers needed to gate the §5
RoutingStorage + CAS-extension design in review/17.

Acceptance criteria (review/17 §7):
  1. Create-if-absent: IfNoneMatch="*" succeeds when absent, 412 when present.
  2. CAS update:       IfMatch=<etag> succeeds on current ETag, 412 on stale.
  3. Mechanism:        native boto3 params (boto3 >= 1.35) vs botocore
                       before-send header injection (boto3 1.34).
  4. Latency:          p50/p95 for conditional PUT / GET / HEAD.

Run from a GitHub Actions runner to get realistic GHA latency numbers.
See .github/workflows/spike-r2-cas.yml.

Required env vars (same set as r2_from_env()):
  CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Optional:
  R2_ENDPOINT  override the default S3 endpoint (needed for jurisdiction-specific
               buckets, e.g. https://<id>.eu.r2.cloudflarestorage.com; shown on
               the Cloudflare token-creation page as "jurisdiction-specific endpoint")

All test objects live under "spike-r2-cas/<run-id>/" and are deleted on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

_PREFIX = "spike-r2-cas"
_CTYPE = "application/json"


# ── result types ─────────────────────────────────────────────────────────────


@dataclass
class TestResult:
    test: str
    mechanism: str  # "native" | "inject" | "n/a"
    passed: bool
    detail: str
    http_status: int | None = None
    etag: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class LatencySample:
    operation: str  # "conditional_put" | "get" | "head"
    count: int
    p50_ms: float
    p95_ms: float
    samples_ms: list[float] = field(default_factory=list)


@dataclass
class SpikeReport:
    boto3_version: str
    botocore_version: str
    cas_mechanism: str  # "native" | "inject" | "unknown"
    helper_signature: str
    tests: list[TestResult] = field(default_factory=list)
    latency: list[LatencySample] = field(default_factory=list)
    overall_pass: bool = False
    notes: list[str] = field(default_factory=list)


# ── boto3 helpers ─────────────────────────────────────────────────────────────


def _make_client():
    import boto3

    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    endpoint = os.environ.get("R2_ENDPOINT", "").strip() or (
        f"https://{account}.r2.cloudflarestorage.com"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _is_412(exc) -> bool:
    """True if ClientError is a 412 PreconditionFailed."""
    import botocore.exceptions

    if not isinstance(exc, botocore.exceptions.ClientError):
        return False
    err = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return err.get("Code") in ("PreconditionFailed", "412") or status == 412


def _safe_delete(client, bucket: str, key: str) -> None:
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        pass


# ── CAS put implementations ───────────────────────────────────────────────────


def _put_native(
    client,
    bucket: str,
    key: str,
    body: bytes,
    *,
    if_none_match: str | None = None,
    if_match: str | None = None,
) -> dict:
    """put_object with native IfNoneMatch/IfMatch params (boto3 >= 1.35)."""
    kwargs: dict = dict(Bucket=bucket, Key=key, Body=body, ContentType=_CTYPE)
    if if_none_match:
        kwargs["IfNoneMatch"] = if_none_match
    if if_match:
        kwargs["IfMatch"] = if_match
    return client.put_object(**kwargs)


def _put_inject(
    client,
    bucket: str,
    key: str,
    body: bytes,
    *,
    if_none_match: str | None = None,
    if_match: str | None = None,
) -> dict:
    """put_object with If-Match/If-None-Match injected via botocore before-send."""
    extra: dict[str, str] = {}
    if if_none_match:
        extra["If-None-Match"] = if_none_match
    if if_match:
        extra["If-Match"] = if_match

    fired = [False]

    def _inject(request, **kwargs):
        if not fired[0]:
            request.headers.update(extra)
            fired[0] = True

    client.meta.events.register("before-send.s3.PutObject", _inject)
    try:
        return client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=_CTYPE)
    finally:
        client.meta.events.unregister("before-send.s3.PutObject", _inject)


def _put_cas(mechanism: str, client, bucket: str, key: str, body: bytes, **kw) -> dict:
    if mechanism == "native":
        return _put_native(client, bucket, key, body, **kw)
    return _put_inject(client, bucket, key, body, **kw)


# ── mechanism detection ───────────────────────────────────────────────────────


def detect_mechanism(client, bucket: str, run_id: str) -> str:
    """Return 'native' if boto3 accepts IfNoneMatch natively, else 'inject'."""
    import botocore.exceptions

    probe = f"{_PREFIX}/{run_id}/mechanism-probe"
    try:
        # Key won't exist yet; IfNoneMatch="*" should succeed if native works.
        client.put_object(
            Bucket=bucket,
            Key=probe,
            Body=b"{}",
            ContentType=_CTYPE,
            IfNoneMatch="*",
        )
        _safe_delete(client, bucket, probe)
        return "native"
    except botocore.exceptions.ParamValidationError:
        # botocore rejected the param — native not in this boto3 version's service model.
        return "inject"
    except botocore.exceptions.ClientError as exc:
        # CR2-SC-17: only a 412 (the key unexpectedly existed, but IfNoneMatch was enforced)
        # actually proves native params are wired through — scope this the same way _is_412 is
        # used correctly elsewhere in this file. Any other ClientError (auth/config/throttling)
        # is a real failure, not evidence either way.
        if _is_412(exc):
            _safe_delete(client, bucket, probe)
            return "native"
        raise


# ── CAS test suite ────────────────────────────────────────────────────────────


def run_cas_tests(client, bucket: str, run_id: str, mechanism: str) -> list[TestResult]:
    import botocore.exceptions

    results: list[TestResult] = []
    key = f"{_PREFIX}/{run_id}/cas-test"
    _safe_delete(client, bucket, key)

    # ── 1: create-if-absent succeeds when key is absent ──────────────────────
    t0 = time.perf_counter()
    try:
        resp = _put_cas(mechanism, client, bucket, key, b'{"v":1}', if_none_match="*")
        ms = (time.perf_counter() - t0) * 1000
        etag = resp.get("ETag", "")  # keep surrounding quotes for IfMatch
        results.append(
            TestResult(
                test="create_if_absent_success",
                mechanism=mechanism,
                passed=True,
                detail=f"IfNoneMatch=* on absent key → 200; ETag={etag}",
                http_status=200,
                etag=etag,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("PASS", "create_if_absent_success", f"ETag={etag} {ms:.0f}ms")
        current_etag = etag
    except botocore.exceptions.ClientError as exc:
        ms = (time.perf_counter() - t0) * 1000
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        results.append(
            TestResult(
                test="create_if_absent_success",
                mechanism=mechanism,
                passed=False,
                detail=f"Unexpected error: {exc}",
                http_status=status,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("FAIL", "create_if_absent_success", str(exc))
        # Create unconditionally so remaining tests have an object to work with.
        resp = client.put_object(Bucket=bucket, Key=key, Body=b'{"v":1}', ContentType=_CTYPE)
        current_etag = resp.get("ETag", "")

    # ── 2: create-if-absent conflicts when key exists ─────────────────────────
    t0 = time.perf_counter()
    try:
        _put_cas(mechanism, client, bucket, key, b'{"v":999}', if_none_match="*")
        ms = (time.perf_counter() - t0) * 1000
        results.append(
            TestResult(
                test="create_if_absent_conflict",
                mechanism=mechanism,
                passed=False,
                detail="PUT IfNoneMatch=* on existing key → 200 (expected 412)",
                http_status=200,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("FAIL", "create_if_absent_conflict", "expected 412, got 200")
    except botocore.exceptions.ClientError as exc:
        ms = (time.perf_counter() - t0) * 1000
        if _is_412(exc):
            results.append(
                TestResult(
                    test="create_if_absent_conflict",
                    mechanism=mechanism,
                    passed=True,
                    detail="IfNoneMatch=* on existing key → 412 as expected",
                    http_status=412,
                    elapsed_ms=round(ms, 2),
                )
            )
            _log("PASS", "create_if_absent_conflict", f"412 {ms:.0f}ms")
        else:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            results.append(
                TestResult(
                    test="create_if_absent_conflict",
                    mechanism=mechanism,
                    passed=False,
                    detail=f"Unexpected error (not 412): {exc}",
                    http_status=status,
                    elapsed_ms=round(ms, 2),
                )
            )
            _log("FAIL", "create_if_absent_conflict", str(exc))

    # ── 3: CAS update succeeds with current ETag ─────────────────────────────
    stale_etag = current_etag  # save before update
    t0 = time.perf_counter()
    try:
        resp = _put_cas(mechanism, client, bucket, key, b'{"v":2}', if_match=current_etag)
        ms = (time.perf_counter() - t0) * 1000
        new_etag = resp.get("ETag", "")
        results.append(
            TestResult(
                test="cas_update_success",
                mechanism=mechanism,
                passed=True,
                detail=f"IfMatch=<current> → 200; new ETag={new_etag}",
                http_status=200,
                etag=new_etag,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("PASS", "cas_update_success", f"old={current_etag} new={new_etag} {ms:.0f}ms")
    except botocore.exceptions.ClientError as exc:
        ms = (time.perf_counter() - t0) * 1000
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        results.append(
            TestResult(
                test="cas_update_success",
                mechanism=mechanism,
                passed=False,
                detail=f"Unexpected error: {exc}",
                http_status=status,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("FAIL", "cas_update_success", str(exc))

    # ── 4: CAS update fails with stale ETag ──────────────────────────────────
    t0 = time.perf_counter()
    try:
        _put_cas(mechanism, client, bucket, key, b'{"v":3}', if_match=stale_etag)
        ms = (time.perf_counter() - t0) * 1000
        results.append(
            TestResult(
                test="cas_update_stale",
                mechanism=mechanism,
                passed=False,
                detail="IfMatch=<stale> → 200 (expected 412)",
                http_status=200,
                elapsed_ms=round(ms, 2),
            )
        )
        _log("FAIL", "cas_update_stale", "expected 412, got 200")
    except botocore.exceptions.ClientError as exc:
        ms = (time.perf_counter() - t0) * 1000
        if _is_412(exc):
            results.append(
                TestResult(
                    test="cas_update_stale",
                    mechanism=mechanism,
                    passed=True,
                    detail="IfMatch=<stale ETag> → 412 as expected",
                    http_status=412,
                    elapsed_ms=round(ms, 2),
                )
            )
            _log("PASS", "cas_update_stale", f"412 {ms:.0f}ms")
        else:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            results.append(
                TestResult(
                    test="cas_update_stale",
                    mechanism=mechanism,
                    passed=False,
                    detail=f"Unexpected error (not 412): {exc}",
                    http_status=status,
                    elapsed_ms=round(ms, 2),
                )
            )
            _log("FAIL", "cas_update_stale", str(exc))

    _safe_delete(client, bucket, key)
    return results


# ── latency measurement ───────────────────────────────────────────────────────


def run_latency(
    client, bucket: str, run_id: str, mechanism: str, iterations: int = 20
) -> list[LatencySample]:
    key = f"{_PREFIX}/{run_id}/latency-probe"
    body = b'{"latency":true}'

    # Prime with an unconditional PUT; capture current ETag for CAS iterations.
    resp = client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=_CTYPE)
    current_etag = resp.get("ETag", "")

    samples: list[LatencySample] = []

    # conditional PUT (IfMatch = current ETag, updating value each iteration)
    put_ms: list[float] = []
    for i in range(iterations):
        body_i = json.dumps({"i": i}).encode()
        t0 = time.perf_counter()
        try:
            resp = _put_cas(mechanism, client, bucket, key, body_i, if_match=current_etag)
            put_ms.append((time.perf_counter() - t0) * 1000)
            current_etag = resp.get("ETag", current_etag)
        except Exception as exc:
            print(f"  WARN latency conditional_put i={i}: {exc}", file=sys.stderr)
    if put_ms:
        samples.append(_make_sample("conditional_put", put_ms))

    # GET
    get_ms: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            client.get_object(Bucket=bucket, Key=key)
            get_ms.append((time.perf_counter() - t0) * 1000)
        except Exception as exc:
            print(f"  WARN latency get: {exc}", file=sys.stderr)
    if get_ms:
        samples.append(_make_sample("get", get_ms))

    # HEAD
    head_ms: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            client.head_object(Bucket=bucket, Key=key)
            head_ms.append((time.perf_counter() - t0) * 1000)
        except Exception as exc:
            print(f"  WARN latency head: {exc}", file=sys.stderr)
    if head_ms:
        samples.append(_make_sample("head", head_ms))

    _safe_delete(client, bucket, key)
    return samples


def _make_sample(operation: str, raw_ms: list[float]) -> LatencySample:
    s = sorted(raw_ms)
    return LatencySample(
        operation=operation,
        count=len(s),
        p50_ms=round(_pct(s, 50), 2),
        p95_ms=round(_pct(s, 95), 2),
        samples_ms=[round(x, 2) for x in raw_ms],
    )


def _pct(sorted_data: list[float], p: int) -> float:
    n = len(sorted_data)
    if not n:
        return 0.0
    k = (n - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


# ── helper signatures emitted in the report ───────────────────────────────────

_HELPER_NATIVE = """\
# boto3 >= 1.35 native params — use directly in S3CompatibleStorage.put_cas()
def put_cas(self, key: str, data: bytes, content_type: str, *,
            if_none_match: str | None = None,
            if_match: str | None = None) -> tuple[str, str]:
    kwargs = dict(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
    if if_none_match:
        kwargs["IfNoneMatch"] = if_none_match
    if if_match:
        kwargs["IfMatch"] = if_match          # pass ETag as returned (includes quotes)
    try:
        r = self._client.put_object(**kwargs)
        return self.public_url(key), r["ETag"]
    except botocore.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] == "PreconditionFailed":
            raise CASConflict(key) from exc
        raise"""

_HELPER_INJECT = """\
# boto3 1.34 fallback — header injection via botocore before-send event
def put_cas(self, key: str, data: bytes, content_type: str, *,
            if_none_match: str | None = None,
            if_match: str | None = None) -> tuple[str, str]:
    headers: dict[str, str] = {}
    if if_none_match:
        headers["If-None-Match"] = if_none_match
    if if_match:
        headers["If-Match"] = if_match        # pass ETag as returned (includes quotes)
    fired = [False]
    def _inject(request, **kwargs):
        if not fired[0]:
            request.headers.update(headers)
            fired[0] = True
    self._client.meta.events.register("before-send.s3.PutObject", _inject)
    try:
        r = self._client.put_object(Bucket=self.bucket, Key=key,
                                    Body=data, ContentType=content_type)
        return self.public_url(key), r["ETag"]
    except botocore.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] == "PreconditionFailed":
            raise CASConflict(key) from exc
        raise
    finally:
        self._client.meta.events.unregister("before-send.s3.PutObject", _inject)"""


# ── output helpers ────────────────────────────────────────────────────────────


def _log(status: str, test: str, detail: str) -> None:
    print(f"{status:4s}  {test}: {detail}", flush=True)


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--latency-iterations",
        type=int,
        default=20,
        help="PUT/GET/HEAD iterations for latency measurement (default 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("r2-cas-spike-results.json"),
        help="JSON report path (default: r2-cas-spike-results.json)",
    )
    parser.add_argument(
        "--no-latency",
        action="store_true",
        help="Skip latency measurement (faster for local debugging)",
    )
    args = parser.parse_args()

    for var in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        if not os.environ.get(var):
            print(f"ERROR: env var {var} is required", file=sys.stderr)
            return 2

    import boto3
    import botocore

    bucket = os.environ["R2_BUCKET"]
    run_id = uuid.uuid4().hex[:12]
    print(f"Spike run_id={run_id} bucket={bucket}", flush=True)

    client = _make_client()

    # 1. Mechanism detection
    print("\n── Mechanism detection ──────────────────────────────────")
    mechanism = detect_mechanism(client, bucket, run_id)
    helper = _HELPER_NATIVE if mechanism == "native" else _HELPER_INJECT
    print(f"     mechanism: {mechanism}", flush=True)

    # 2. CAS correctness tests
    print("\n── CAS correctness tests ────────────────────────────────")
    cas_tests = run_cas_tests(client, bucket, run_id, mechanism)

    # 3. Latency measurement
    latency: list[LatencySample] = []
    if not args.no_latency:
        print(f"\n── Latency ({args.latency_iterations} iterations each) ──────────────")
        latency = run_latency(client, bucket, run_id, mechanism, args.latency_iterations)
        for s in latency:
            print(
                f"     {s.operation:20s}  p50={s.p50_ms:.1f}ms  p95={s.p95_ms:.1f}ms  n={s.count}"
            )  # noqa: E501

    # 4. Build report
    latency_incomplete = not args.no_latency and (
        len(latency) < 3  # conditional_put, get, head
        or any(s.count < args.latency_iterations for s in latency)
    )
    cas_failed = any(not t.passed for t in cas_tests)
    all_pass = not cas_failed and not latency_incomplete
    notes: list[str] = []
    if latency_incomplete:
        notes.append("latency measurement incomplete or had dropped samples")
    if cas_failed:
        notes.append("One or more CAS correctness tests failed — review §6 fallbacks.")
    if mechanism == "inject":
        notes.append(
            "boto3 < 1.35 detected: IfNoneMatch/IfMatch not in service model; "
            "inject helper required. Consider pinning boto3>=1.35 to use native params."
        )

    report = SpikeReport(
        boto3_version=boto3.__version__,
        botocore_version=botocore.__version__,
        cas_mechanism=mechanism,
        helper_signature=helper,
        tests=cas_tests,
        latency=latency,
        overall_pass=all_pass,
        notes=notes,
    )

    args.output.write_text(json.dumps(asdict(report), indent=2) + "\n")

    print("\n── Summary ──────────────────────────────────────────────")
    print(f"     mechanism:    {mechanism}")
    print(f"     tests:        {sum(t.passed for t in cas_tests)}/{len(cas_tests)} passed")
    print(f"     overall_pass: {all_pass}")
    if notes:
        for note in notes:
            print(f"     NOTE: {note}")
    print(f"\nReport written to {args.output}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
