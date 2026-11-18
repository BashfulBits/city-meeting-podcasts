# review/60 Phase 3 eval image: the Phase 1 streaming image (audited Karpo source at the pinned
# commit, GLiDE source at the pinned revision) plus this repo's citypods and the frozen inputs.
# Only manifest.json is copied: gold.json and production_reference.json never enter the image.
# Required: pass --build-arg BASE_IMAGE=<registry path of the Phase 1 streaming image, pinned by digest>.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PYTHONPATH=/app:/opt/karpo/src:/opt/glide-src PYTHONUNBUFFERED=1
COPY citypods /app/citypods
COPY scripts /app/scripts
COPY evals/open-weight-decision/manifest.json /app/evals/open-weight-decision/manifest.json
COPY workers/decision-batch/open_weight_eval_worker.py /app/open_weight_eval_worker.py
RUN python -c "import citypods.judging.open_weight_eval, citypods.judging.karpo_stream; from glide.model import Glide; import karpo_decision.decider" && ! test -e /app/evals/open-weight-decision/gold.json \
    && ! test -e /app/evals/open-weight-decision/production_reference.json
ENTRYPOINT ["python", "/app/open_weight_eval_worker.py"]
