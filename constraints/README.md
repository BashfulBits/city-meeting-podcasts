# `constraints/` — compiled, hash-pinned Python dependency locks

`pyproject.toml` is the **abstract** declaration (`>=` floors only). The files here are the **concrete**,
fully-resolved pins that make installs reproducible. Policy: [`review/22`](../review/22-dependency-and-reproducibility-policy.md).

| File | Profile (extras) | Consumed by |
|---|---|---|
| `prod.txt` | core + `storage` | `deploy.yml`, `audit.yml`, `audio.yml`, … and the audio-runner image |
| `asr.txt`  | core + `storage` + `asr-*` | `asr.yml`, `asr-bench.yml`, Modal/Beam worker images |
| `dev.txt`  | core + `storage` + `dev` | `ci.yml` |

## Consuming them

- **Editable CI path:** `pip install -e ".[dev]" -c constraints/dev.txt` — versions are pinned; the
  editable local package cannot be hash-checked, so hash mode is not forced here.
- **Immutable images:** install deps hash-verified first, then the local package without re-resolving:
  ```dockerfile
  pip install --require-hashes -r constraints/prod.txt
  pip install --no-deps "/opt/citypods-src[storage]"
  ```

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
