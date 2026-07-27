# 22 — Dependency Pinning & Update Policy

> **Status:** normative policy (living). Executes the `review/11` Phase-R item
> *"Runtime/dependency maintenance automation"*. Tracked by the umbrella issue for GH#498 (HF model
> revisions) and GH#734 (SHA-pin GitHub Actions).

## Why this exists

This project's identity model is **content-addressed and pipeline-versioned**: an artifact's storage key
is a hash of its *recipe*, and a stage only re-processes when its version bumps (see `AGENTS.md`
"pipeline-version bumps", `ARCHITECTURE.md`). That guarantee is only as strong as the **inputs** to the
recipe — the ffmpeg build, the Python inference libraries, the Whisper model bytes. If those drift
silently, two runs (or the GitHub-Actions runner vs. a Modal/Beam worker) can produce different output
while the store still treats them as the same recipe.

Before this policy the repo had reproducibility holes on every dependency axis at once:

- **Python** — `pyproject.toml` declared only `>=` floors with **no lockfile**; every `pip install -e`
  resolved whatever PyPI served that minute.
- **GitHub Actions** — a mix of SHA-pinned and tag-pinned `uses:` (movable = supply-chain risk, GH#734).
- **HF Whisper models** — `scripts/prepare_whisper.py` downloaded via mutable `main` (GH#498 / CR-SC-38).
- **External workers** — `scripts/compute/modal_app.py` / `beam_app.py` re-declared a hand-maintained
  `>=` dep list, installed `ffmpeg` via unpinned `apt-get`, and loaded the model with no revision pin.

**Goal:** pins are the *default* so builds are reproducible, and an automated bot (Renovate) opens PRs to
move pins forward on a cadence — so we do **not** stall past security or beneficial updates. Determinism
without freezing.

## The contract

Every dependency class has a **source-of-truth pin location**, a **pin granularity**, an **update
mechanism**, and a **determinism class**. The determinism class is the local nuance that ties this policy
to the existing content-addressed model.

| Class | Pin location | Granularity | Update mechanism | Determinism class |
|---|---|---|---|---|
| Python libraries | `constraints/*.txt` compiled from `pyproject.toml`, consumed via `pip install … -c` | exact `==` version pins (hash-verified `--require-hashes` for the immutable images is a documented follow-up) | Renovate (pip) → recompile | **output-affecting:** `faster-whisper`, `ctranslate2`, `stable-ts`, `Pillow`; **hygiene:** everything else |
| GitHub Actions | `.github/workflows/*.yml` | full 40-hex commit SHA + `# vN` comment | Renovate (github-actions) | hygiene (build-time) |
| Base runner image | `.github/audio-runner/Dockerfile` | `@sha256:` digest | Renovate (docker) | output-affecting (toolchain) |
| Static ffmpeg | `FFMPEG_URL` + `FFMPEG_SHA256` (`audio-runner-image.yml`, `audio.yml`, `asr.yml`, `asr-quality-eval.yml`, `ci.yml`, `dep-bump-smoke.yml`) | immutable release URL + SHA256 | Renovate custom regex, **monthly, smoke-gated** | output-affecting (encode) |
| HF Whisper models | `HF_*_REVISION` constants in `scripts/prepare_whisper.py` | pinned commit-SHA revision | Renovate custom regex → **Dashboard approval** | output-affecting (transcripts) |
| Node / Cloudflare Worker | `workers/*/package-lock.json`, `wranglerVersion`, `setup-node` node version | exact / lockfile | Renovate (npm) | hygiene |

### Two rules that make it fit the repo

1. **Output-affecting bumps are deliberate and version-coupled.** A change to an inference lib, ffmpeg,
   the base image, or a model revision must pass the per-source image smoke test and — per the
   `AGENTS.md` pipeline-version-bump contract — the PR must state whether it bumps the relevant version
   (`ASR_PIPELINE_VERSION`, `SilencePlanner.version`, `AUDIO_PIPELINE_VERSION`) and whether stored artifacts are
   invalidated. **Pinning a *current* version is a no-op reproducibility fix: it must NOT bump any
   pipeline version or reprocess artifacts** (explicit in GH#498).
2. **One source of truth per class.** External workers do not re-declare dependencies — they install
   from the same compiled constraints and load the same model revision as the runner.

### Internal ↔ external consistency: what pinning does and doesn't guarantee

Pinning the determinism-critical layer makes the **recipe** identical on the GitHub-Actions runner *and*
the Modal/Beam workers — same exact `faster-whisper` + `ctranslate2` (from the shared constraints) and
the same HF model revision. It does **not** force bit-identical output across *different
hardware/compute_type* (runner CPU `int8` vs. GPU `fp16`); FP kernels differ by device. That axis is
already keyed on purpose: `asr_spec_hash()` includes `compute_type` and `beam_size`, so CPU-int8 and
GPU-fp16 are distinct recipes. Pinning aligns every axis meant to match; the deliberately-different
hardware axis stays explicitly keyed rather than drifting silently.

## Python constraints — how it works

`pyproject.toml` remains the **abstract** declaration (`>=` floors only — never pin exact versions
there). Concrete, fully-resolved pins live in compiled constraint files:

- `constraints/prod.txt` — core runtime + `storage`
- `constraints/asr.txt` — adds the `asr-*` extras (`faster-whisper`, `ctranslate2`, `stable-ts`, `jiwer`)
- `constraints/dev.txt` — adds the `dev` extra (`pytest`, `ruff`, `pip-tools`)

Every install site adds `-c constraints/<profile>.txt`. Constraints are **version-pinned** (exact `==`)
rather than hashed: they are consumed alongside editable `pip install -e .`, and pip's hash-checking
mode is incompatible with an unhashable editable install. Adding hash verification for the immutable
container/worker images (a `--require-hashes -r` install of a non-editable build) is a documented
follow-up hardening, not a blocker for version reproducibility.

**Constraints are compiled in the pinned target environment, not on a contributor laptop.** The deploy
target is linux / CPython 3.12; resolving on a different OS or Python version yields wrong platform
wheels. The canonical compile runs inside `python:3.12-slim-bookworm` (the runner base) via
`scripts/compile_constraints.sh`, driven by the `lock.yml` workflow (and reproducible locally with
Docker). See `constraints/README.md`.

## Manual-approval flow (light-touch, issue-driven)

Hygiene updates flow through with ~zero attention; output-affecting updates cost a human exactly **one
checkbox + one bot comment**, and everything after approval is automated. Built on Renovate's native
**Dependency Dashboard** (one self-updating issue), so pending bumps are unified in one place.

**Hygiene lane** (`ruff`, `pytest`, `boto3`, `requests`, `PyYAML`, `Jinja2`, `defusedxml`, `jiwer`, Node
dev tools): Renovate opens grouped PRs weekly; patch/minor of dev/test tools auto-merge on green CI.
Actions / workflow-file PRs are **never** auto-merged (a compromised update could move a pinned SHA).

**Output-affecting lane** (`faster-whisper`, `ctranslate2`, `stable-ts`, `Pillow`, the ffmpeg URL+SHA,
the base-image digest, the HF model revision): grouped with `dependencyDashboardApproval: true`. Renovate
lists the candidate under **"Pending Approval"** on the Dashboard and opens **no PR** until a human ticks
the box.

**The loop — propose → prove → approve → automate:**

1. **Propose.** Renovate adds a checkbox row to the Dashboard issue. Nothing else happens.
2. **Approve (light touch #1).** A human ticks the checkbox — the entire approval gesture.
3. **PR opens**, labeled `output-affecting`.
4. **Prove (automated artifacts).** `dep-bump-smoke.yml` builds the affected image and runs an
   output-drift smoke over a **per-source golden fixture set — one short clip from every provider
   family** (`granicus`, `swagit`, `civicplus`, `civicclerk`). ffmpeg/base-image bumps change
   *materialization* (extract → concat → trim → encode) and source families differ in
   container/codec/stream quirks, so a single-source clip could miss a regression. For each source the
   smoke compares the full materialize path (encode **hash + duration + loudness**) and, for
   inference-lib/model bumps, the ASR path (the `asr-bench` jiwer harness + `tests/snapshots`). A bot
   comments a **per-source before/after table** whose key column is *did output change: yes/no*, and
   uploads the artifacts.
5. **Decide (light touch #2).** The reviewer reads that one comment:
   - *Output unchanged* → pure reproducibility bump; **merge**, no version change.
   - *Output changed* → the PR-body checklist (from the `AGENTS.md` bump contract) forces the call:
     adopt (add the pipeline-version bump + state invalidation) or hold.
6. **Automate the rest.** On merge, existing triggers rebuild/publish the images
   (`audio-runner-image.yml` and the external-worker deploys) from the same constraints.

Net approver effort per output-affecting bump: **tick one box, read one comment, click merge.**

## Adding or changing a dependency (contract for future contributors & agents)

1. Edit **`pyproject.toml`** only — add the abstract `>=` floor. Never pin exact versions there.
2. **Recompile constraints** (`scripts/compile_constraints.sh`) and commit the updated `constraints/*.txt`.
3. **Classify it.** Hygiene, or output-affecting (touches produced audio/transcript bytes)? If
   output-affecting, add it to the Renovate `output-affecting` group **and** the table above.
4. **Do not** re-list it in `scripts/compute/modal_app.py` / `beam_app.py` — external workers install
   from the same constraints automatically.
5. Open the PR; CI does the rest.

### Enforcement (the anti-staleness mechanism)

Three CI guards keep the policy live rather than aspirational (see `scripts/check_dependency_policy.py`
and the `deps` job in `ci.yml`):

- **Constraints-drift gate** — recompile in the pinned env and `git diff --exit-code`; fails if
  `pyproject.toml` changed without a matching `constraints/*.txt` update, or the lock is stale.
- **Pinned-Actions guard** — every `uses:` in `.github/workflows/*.yml` must match
  `@[0-9a-f]{40} # v…`; any tag-pin fails the build (OpenSSF pinned-dependencies posture).
- **External-worker-dep guard** — `modal_app.py` / `beam_app.py` must not declare ad-hoc
  `pip_install` / `add_python_packages` entries outside the shared constraints.

## References

- GH#498 — pin HF Whisper model revisions (`scripts/prepare_whisper.py`); CR-SC-38 in `review/19`.
- GH#734 — audit and SHA-pin all third-party GitHub Actions.
- `review/11` — Phase-R "Runtime/dependency maintenance automation" (this doc executes it).
- `AGENTS.md` — pipeline-version-bump contract; `SECURITY.md` — supply-chain posture.
- OpenSSF Scorecard *Pinned-Dependencies*; GitHub Docs *Security hardening for GitHub Actions*.
