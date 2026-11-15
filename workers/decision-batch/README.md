# Deferred decision worker: offline preparation

This directory supplies the gzip JSONL runner, cloud entrypoint and deployment templates.
The maintainer approved up to three one-task canaries within $4/month gross, subject to project,
billing and IAM verification. The GitHub workflow starts disabled.

## Google Cloud project setup

1. Apply the recurring Google AI Pro credit to a Cloud Billing account. Set **Always use this
   billing account** to apply future grants automatically if desired. Create a dedicated project,
   link it to the same account and confirm the credit is active before provisioning.
   [Google Developer Program credits](https://developers.google.com/profile/help/benefits) ·
   [Cloud Billing project linking](https://docs.cloud.google.com/billing/docs/how-to/modify-project)
2. In the project, enable Cloud Run, Cloud Storage, Artifact Registry, IAM, IAM Credentials and
   Security Token Service. Use `us-central1` for the job, private buckets and registry. Check the Cloud Run
   quota for non-zonal L4. Add a $4 project budget. Use a supported Cloud Run spend cap if
   available and keep the three-attempt launcher guard regardless of budget type.
   [GPU quotas](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu) ·
   [Cloud Run spend controls](https://docs.cloud.google.com/run/docs/jobs-cost-optimization)
3. Create separate private regional buckets for batches and the pinned model. Enable uniform
   bucket-level access and public access prevention. Set a lifecycle on raw batch payloads and
   expired attempts after collection. Preserve `dispatch/admission.json`, receipts and final
   result selections independently of raw payload expiry. Keep only the selected model revision.
   [Object lifecycle](https://docs.cloud.google.com/storage/docs/lifecycle)
4. Set up GitHub Workload Identity Federation using the repository and owner numeric IDs,
   `refs/heads/main`, the `decision-batch.yml` workflow and the protected `decision-batch-canary`
   environment. Use service-account impersonation. Restrict the producer to request and manifest
   creation and reads. The dispatcher needs manifest/chunk reads, admission CAS and read/run with
   overrides on the fixed job. The collector needs execution, manifest, chunk and attempt reads,
   admission CAS, and receipt creation and reads. The task service account needs admission,
   manifest, chunk and model reads as well as attempt-object creation and reads. Keep deploy
   permissions on a separate owner identity.
   [GitHub federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
5. Complete the runtime gates below. Configure repository variables `DECISION_BATCH_WIF_PROVIDER`,
   `DECISION_BATCH_PRODUCER_SA`, `DECISION_BATCH_DISPATCHER_SA`, `DECISION_BATCH_COLLECTOR_SA`,
   `DECISION_BATCH_BUCKET`, `DECISION_BATCH_JOB`, and `DECISION_BATCH_IMAGE_DIGEST`.
   The workflow pins Cloud SDK 593.0.0. The job is a fully qualified resource name and the
   digest has the form `sha256:<64 hex characters>`. Set `DECISION_BATCH_CLOUD_ENABLED=true` only
   when ready. Add a required reviewer to the GitHub environment during the canary.

## Reproducibility gates

1. Build from the dedicated `constraints/decision-gpu.txt` lock for Linux/amd64 and Python 3.12.
   The normal repository lock workflow recompiled the initial upstream GLiDE Torch 2.14.0,
   Triton 3.8.0 and CUDA 13.0 runtime pins. Keep it separate from the WhisperX ASR closure.
   The Docker base digest supplies the C compiler and Python/libc headers for Triton. Cloud Run
   supplies the GPU driver. [Dependency policy](../../review/22-dependency-and-reproducibility-policy.md)
2. Review Fastino's license and notices. Pin the GLiDE-9B source and weights by the reviewed full
   revision `ab54df5acba0eebf7df45560ffdaacd64e32df3b`. Obtain the complete snapshot through an
   approved process, independently verify the snapshot bytes and mount it read-only at the
   configured local path. A directory name is not evidence of model identity.
3. Build with `docker build --platform linux/amd64 -f workers/decision-batch/Dockerfile .` from a
   clean checkout. For a remote build, stage only `pyproject.toml`, `README.md`, `citypods/`, the
   two decision GPU constraints files and the worker's Dockerfile and Python sources. Use a
   private registry and record the result's digest. The Docker build vendors just the pinned
   GLiDE source and attribution. The model bucket supplies the hash-verified weights at runtime.
   Before loading the weights, the worker checks CUDA 13.0, L4 BF16 and the FLA kernel import.
4. Confirm Cloud Run L4 availability, current image/job schema, GPU/kernel path and driver,
   project and region, scoped service identity, private bucket, retention policy, and read-only
   model mount, including its volume and `volumeMounts` configuration. Set
   `CITYPODS_IMAGE_DIGEST` to the deployed image digest; the worker checks it against the manifest.
   The job template has placeholders and is intentionally not deployable as-is.
   Provisioning and image building are separate from this repository's offline tests.
5. Run `prepare` for each mode from the manual workflow. Record its manifest ID. `launch` checks
   the job definition and reserves $0.09 before calling Cloud Run. It prints the attempt ID
   before submission and the operation or execution name after acknowledgement. The ledger holds
   one outstanding attempt and retains three attempts' worth of reservations even across months.
   Use one task, parallelism one, a 300-second timeout and zero task retries.
6. Use `status` and Cloud Run **Executions** to find the fully qualified execution name. Run
   `collect` after a matching execution succeeds. Use `settle-failed` only after the matching
   execution has a terminal failed condition. A launch with no acknowledgement holds its
   reservation. Reconcile it by matching the worker override IDs and provider execution before
   taking another attempt. Compare the regular and delayed billing entries and keep the cloud
   workflow disabled when the canary finishes.

The launcher checks effective Run permissions before making a reservation. If submission still
fails without an execution ID, pause canary dispatch and inspect Cloud Audit Logs and the execution
list. A verified rejection with no execution needs owner reconciliation of the matching
`dispatch/admission.json` generation. Keep the attempt ID and reserved charges, and clear only
that attempt's `outstanding` field with a generation-match write. An empty execution list alone
cannot establish rejection. A generic CLI failure or timeout leaves the reservation fenced.

## Local smoke

Run `python workers/decision-batch/run_batch.py --input input.jsonl.gz --output result.jsonl.gz
--model-dir /local/pinned/glide-9b --token-budget 16384 --max-len 4096` from the repository root.
The worker imports GLiDE lazily after validation, uses its BF16 default and disables vision. It
only publishes complete, validated output. Offline tests use fake inference and make no cloud calls.

A real GPU canary must separately record queue/start, model load, inference and completion times,
peak host/GPU memory, token distribution, validated decisions per second, kernel availability,
and actual total charges. The native API returns one batch as a whole: the worker can report the
start of inference, but cannot observe first result time inside that call without runtime changes.
Compare regular and delayed modes on matched work before selecting a recurring mode. Keep results
private and expiring; collect no evidence text in telemetry. Do not promote this shadow output to
judge votes without the qualification and maintainer decision described in review/59.
