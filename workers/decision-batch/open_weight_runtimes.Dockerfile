# review/60 Phase 1 follow-up image: the elephant-1 (OneJev qev) author runtime
# as pinned, read-only source next to this repo's adapters (citypods.judging.open_weight_runtimes).
#
# BASE_IMAGE is a required build argument, with no default (--build-arg BASE_IMAGE=<image>@sha256:<digest>).
# It is the Phase 3 evaluation base (the decision worker image plus the audited Karpo and
# GLiDE sources, torchvision), pinned by digest. It already has the compiled decision-gpu stack
# (transformers, torch, pydantic, requests, fla) and HF_HUB_OFFLINE=1, so nothing is pip-installed
# here and the author runtimes never reach the network at run time. The build downloads each
# source once and refuses any byte stream that is not the audited one (SHA-256 below).
#
# The build context is a staged directory:
#   cp -R citypods scripts <ctx>/ && cp workers/decision-batch/runtime_parity.py <ctx>/
#   citypods/judging/open_weight_eval.py   from the Phase 3 branch (request building)
#   manifest.json                          evals/open-weight-decision/manifest.json (inputs only)
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# OneJev qev at 81ce62f, decision-index at f5a6e4f (engine.py's base class), the elephant-1 patch
# and engine at b3f8a93.
RUN set -eux; \
    get() { curl -fsSL --retry 5 "$1" -o "$2"; echo "$3  $2" | sha256sum -c -; }; \
    mkdir -p /opt/onejev /opt/decision-index /opt/elephant /tmp/src; \
    get https://github.com/OmniJev/OneJev/archive/81ce62f1597c91e46767d4d02d6ac2e18534fe94.tar.gz \
        /tmp/src/onejev.tar.gz 01346ed50c451caa6d6dd49d0640dbda0444bc0ad908dbf77603c82056974cf9; \
    get https://github.com/apolinario/decision-index/archive/f5a6e4fd85fed05e77878dfc4966d0568bfb45ec.tar.gz \
        /tmp/src/kit.tar.gz 4e05ac199c3696c7b9f7a0df848519ad826ca2ef2d6763fbd1e3544d13c06fff; \
    E=https://huggingface.co/baselabs/elephant-1/resolve/b3f8a93462fb1051a9de031e94a7ff380e254072; \
    get $E/serve.patch /tmp/src/serve.patch f234c125f1d38d82104b9e1f24e75bde51e4401928acfba2fbf5c59ef2fb46b8; \
    get $E/engine.py /opt/elephant/engine.py 72373353379c53b7cabfb5d05b6fef3c898d48c87cc386e03fc3de9c07e67ca7; \
    tar xzf /tmp/src/onejev.tar.gz -C /opt/onejev --strip-components=1 --no-same-owner; \
    tar xzf /tmp/src/kit.tar.gz -C /opt/decision-index --strip-components=1 --no-same-owner; \
    patch -p1 -d /opt/onejev < /tmp/src/serve.patch; \
    rm -rf /tmp/src /opt/onejev/assets /opt/decision-index/hub /opt/decision-index/decision_index/data; \
    chmod -R a-w /opt/onejev /opt/decision-index /opt/elephant

ENV PYTHONPATH=/app:/opt/karpo/src:/opt/glide-src:/opt/onejev:/opt/decision-index \
    PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
COPY citypods /app/citypods
COPY scripts /app/scripts
COPY manifest.json /app/evals/open-weight-decision/manifest.json
COPY runtime_parity.py /app/runtime_parity.py
RUN python -c "\
from citypods.judging import open_weight_runtimes as r, open_weight_eval; \
[r.verify_sources(k) for k in ('onejev', 'decision_index', 'elephant')]; \
import json; \
m = json.load(open('/app/evals/open-weight-decision/manifest.json')); \
assert open_weight_eval.build_requests(m, 'dev'), 'no dev requests'; \
import qev.engine, decision_index.engines" \
    && ! test -e /app/evals/open-weight-decision/gold.json
ENTRYPOINT ["python", "/app/runtime_parity.py"]
