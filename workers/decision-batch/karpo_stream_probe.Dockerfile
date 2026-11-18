# review/60 Phase 1 probe image (Cloud Run job; not the production decision worker).
#
# BASE_IMAGE must be the Phase 0 spike image, pinned by full digest: the decision worker image
# plus the audited Karpo source at commit 6600011eb5cf46232d056ec0717dc726146d29de on PYTHONPATH
# and pybase64. That image also supplies /app/inputs.json, the frozen tag and chapter workload
# the probe answers (probes fail before publishing a result if it is missing).
#   docker build --build-arg BASE_IMAGE=<registry>/<repo>/glide-worker@sha256:<64 hex digits> ...
#
# The build context is a staged directory, not the repository root. Stage it with:
#   cp -R citypods scripts <ctx>/ && cp workers/decision-batch/karpo_stream_probe.py <ctx>/
#   hf-<12B|9B|4B|31B|COP|ELE>.json: Hugging Face `tree?recursive=1` listings of the pinned revisions
#   phase0-12b-l4-fp8.json phase0-9b-l4-bf16.json: the Phase 0 spike results, for comparison
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# torchvision is needed only by Qwen3.5's image processor (Karpo 9B). This probe lane builds on the
# Phase 0 image rather than the compiled decision-gpu constraints, so the exact pin that matches
# torch 2.14 is installed here; the production Karpo worker image must declare it in
# constraints/decision-gpu.in instead (decided in the review/60 Phase 3 PR).
RUN python -m pip install --no-cache-dir --no-deps "torchvision==0.29.0" && python -c "import torchvision"
ENV PYTHONPATH=/app:/opt/karpo/src PYTHONUNBUFFERED=1
COPY citypods /app/citypods
COPY scripts /app/scripts
COPY karpo_stream_probe.py hf-12B.json hf-9B.json hf-4B.json hf-31B.json hf-COP.json hf-ELE.json \
    phase0-12b-l4-fp8.json phase0-9b-l4-bf16.json /app/
RUN python -c "import citypods.judging.karpo_stream, scripts.derive_karpo_manifest"
ENTRYPOINT ["python", "/app/karpo_stream_probe.py"]
