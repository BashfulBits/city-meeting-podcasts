# Phase 1 probe image: the Phase 0 spike image (audited Karpo source at the pinned commit) plus this repo's citypods.
FROM <registry>/<repo>/glide-worker@sha256:7e8b80764b1a... (the Phase 0 spike image)
RUN python -m pip install --no-cache-dir --no-deps "torchvision==0.29.0" && python -c "import torchvision"
ENV PYTHONPATH=/app:/opt/karpo/src PYTHONUNBUFFERED=1
COPY citypods /app/citypods
COPY scripts /app/scripts
COPY karpo_stream_probe.py hf-12B.json hf-9B.json phase0-12b-l4-fp8.json phase0-9b-l4-bf16.json /app/
RUN python -c "import citypods.judging.karpo_stream, scripts.derive_karpo_manifest"
ENTRYPOINT ["python", "/app/karpo_stream_probe.py"]
