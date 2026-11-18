# review/60 Phase 3 eval image for the elephant-1 adapter: the Phase 1 runtime
# image (author trees pinned by SHA-256, tag r60-rt-v8) plus this repo's worker. Only manifest.json
# is copied; gold.json and production_reference.json never enter the image. Do not add the author
# trees to PYTHONPATH here: the loaders verify file digests and import locations.
# Required: pass --build-arg BASE_IMAGE=<registry path of the Phase 1 runtime image, pinned by digest>.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY citypods /app/citypods
COPY scripts /app/scripts
COPY evals/open-weight-decision/manifest.json /app/evals/open-weight-decision/manifest.json
COPY workers/decision-batch/open_weight_eval_worker.py /app/open_weight_eval_worker.py
RUN python -c "import citypods.judging.open_weight_eval, citypods.judging.open_weight_runtimes" && ! test -e /app/evals/open-weight-decision/gold.json \
    && ! test -e /app/evals/open-weight-decision/production_reference.json
ENTRYPOINT ["python", "/app/open_weight_eval_worker.py"]
