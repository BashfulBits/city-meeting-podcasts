# Linux amd64 base. The full Bookworm image supplies the compiler and Python headers for Triton.
FROM python:3.12-bookworm@sha256:3c3ba8ad4c85eb63b7568f806434f14e33370056985da7fdf2673621cb1d7e93
WORKDIR /app

COPY constraints/decision-gpu.in constraints/decision-gpu.txt constraints/
RUN python -m pip install --no-cache-dir --only-binary=:all: \
    -r constraints/decision-gpu.in -c constraints/decision-gpu.txt

COPY pyproject.toml README.md ./
COPY citypods/ citypods/
COPY scripts/download_glide_weights.py /app/scripts/download_glide_weights.py
COPY workers/decision-batch/cloud_batch.py workers/decision-batch/run_batch.py workers/decision-batch/
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . && \
    python -m pip check

# The image controls executable model code. Cloud Storage supplies only the pinned weights.
RUN python -c 'from huggingface_hub import snapshot_download; snapshot_download( \
    "fastino/GLiDE-9B", revision="ab54df5acba0eebf7df45560ffdaacd64e32df3b", \
    allow_patterns=["glide/*", "LICENSE", "NOTICE", "licenses/*"], local_dir="/opt/glide-src")' && \
    test -f /opt/glide-src/glide/model.py
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app:/opt/glide-src HF_HUB_OFFLINE=1
RUN python -c 'from glide.model import Glide; from citypods.judging import decision_batch_cloud'

ENTRYPOINT ["python", "workers/decision-batch/cloud_batch.py"]
