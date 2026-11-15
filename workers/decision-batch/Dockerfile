# Supply a separately reviewed digest-pinned CUDA/Python base with GLiDE and all
# citypods runtime dependencies already installed from the shared GPU lane.
# No default is intentional: an unreviewed, moving tag must not become deployable.
ARG GPU_BASE_IMAGE
FROM ${GPU_BASE_IMAGE}
WORKDIR /app
COPY pyproject.toml README.md ./
COPY citypods/ citypods/
COPY workers/decision-batch/cloud_batch.py workers/decision-batch/run_batch.py workers/decision-batch/
RUN python -m pip install --no-deps --no-build-isolation .
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/glide-src
ENTRYPOINT ["python", "workers/decision-batch/cloud_batch.py"]
