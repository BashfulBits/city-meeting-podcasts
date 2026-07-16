# `constraints/` — compiled, hash-pinned Python dependency locks

`pyproject.toml` is the **abstract** declaration (`>=` floors only). The files here are the **concrete**,
fully-resolved pins that make installs reproducible. Policy: [`review/22`](../review/22-dependency-and-reproducibility-policy.md).

| File | Profile (extras) | Consumed by |
|---|---|---|
| `prod.txt` | core + `storage` + `wer` + `llm` | `deploy.yml`, `audit.yml`, `audio.yml`, `asr-quality-review.yml`, … and the audio-runner image |
| `asr.txt`  | core + `storage` + `asr-*` (incl. `asr-align2`) | `asr.yml`, `asr-bench.yml`, `asr-quality-eval.yml`, Modal/Beam worker images |
| `dev.txt`  | core + `storage` + `dev` + `wer` + `llm` | `ci.yml` |

`asr-align2` (H15 Layer 2's independent CTC aligner, `torchcodec`) is compiled into `asr.txt`
rather than its own file: `torch`/`torchaudio` are already a transitive pin there via
`stable-ts[fw]`'s own `torch`/`openai-whisper` dependency, so a separate lock file would risk two
constraint files disagreeing on the same package's pinned version.

## Consuming them

These files are **version-pinned** (exact `==`), not hashed. They are consumed via `pip … -c`
alongside editable `pip install -e .`, and pip's hash-checking mode is incompatible with an
unhashable editable install — so hashes are intentionally omitted.

- **Everywhere (`-c`):** `pip install -e ".[dev,llm]" -c constraints/dev.txt` — exact versions pinned.
- **Immutable images (follow-up):** hash-verified installs (`--require-hashes -r` of a non-editable
  build, then `pip install --no-deps` for the local package) are a documented future hardening for
  the runner/worker images; not required for version reproducibility.

## Regenerating (the only correct way)

Constraints MUST be compiled in the deploy target — **linux / CPython 3.12** (the audio-runner base) —
not on a contributor laptop, or the platform wheels/versions will be wrong. Use the helper, which runs
`pip-compile` inside the pinned `python:3.12-slim-bookworm` image:

```bash
scripts/compile_constraints.sh          # requires Docker locally
```

In CI this is the **`lock.yml`** workflow (manual dispatch, and on `pyproject.toml` change). The
`deps` job in `ci.yml` enforces that these files are in sync (recompile → `git diff --exit-code`).

> These files are generated. Do not hand-edit. Change `pyproject.toml`, then recompile.
