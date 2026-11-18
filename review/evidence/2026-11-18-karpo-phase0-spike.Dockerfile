# Phase 0 spike image (registry path redacted). Base: the decision worker image at digest sha256:231790916b0c...
FROM <registry>/<repo>/glide-worker@sha256:231790916b0cda1ee24d37124af04e32f9454d19d93047359c521959431eee0e
ENV PYTHONPATH=/opt/karpo/src
COPY karpo-src.tgz /tmp/karpo-src.tgz
RUN echo "ba81470579aaae548c579acc230542eaca3779b492d8e1db2ddfcc9b04bdefb4  /tmp/karpo-src.tgz" | sha256sum -c - && mkdir -p /opt/karpo && tar -xzf /tmp/karpo-src.tgz -C /opt/karpo --strip-components=1 \
    && python -m pip install --no-cache-dir "pybase64>=1.4" && python -m pip install --no-cache-dir --no-deps "torchvision==0.29.0" && python -c "import karpo_decision.decider, karpo_decision.serve.fp8"
COPY spike.py inputs.json /app/
WORKDIR /app
ENTRYPOINT ["python", "/app/spike.py"]
