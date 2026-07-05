# syntax=docker/dockerfile:1.7

# Multi-architecture index digest for the official Python 3.12 slim-bookworm image. The production
# audio workflow consumes only linux/amd64, but pinning the index keeps the declared base immutable.
FROM python:3.12-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

ARG FFMPEG_URL
ARG FFMPEG_SHA256

ENV PATH="/opt/citypods-ffmpeg/bin:${PATH}" \
    PYTHONPATH="/workspace" \
    PYTHONUNBUFFERED="1"

COPY scripts/install_static_ffmpeg.py /usr/local/bin/install_static_ffmpeg.py
RUN python /usr/local/bin/install_static_ffmpeg.py \
      --url "${FFMPEG_URL}" \
      --sha256 "${FFMPEG_SHA256}" \
      --install-dir /opt/citypods-ffmpeg \
    && ffmpeg -version \
    && ffprobe -version

# Install the audio lane's Python dependencies into the image. At runtime /workspace is the current
# checkout and PYTHONPATH points there, so application code comes from the exact workflow SHA while
# dependency and native-tool setup comes from this prebuilt runtime.
COPY pyproject.toml README.md /opt/citypods-src/
COPY constraints /opt/citypods-src/constraints
COPY citypods /opt/citypods-src/citypods
# Pin transitive deps to the compiled constraints (review/22). Once constraints/*.txt
# are populated by lock.yml, harden this to the hash-verified form:
#   pip install --require-hashes -r /opt/citypods-src/constraints/prod.txt \
#     && pip install --no-deps "/opt/citypods-src[storage]"
RUN pip install --no-cache-dir -c /opt/citypods-src/constraints/prod.txt "/opt/citypods-src[storage]"

WORKDIR /workspace
ENTRYPOINT ["python", "-m", "citypods.cli"]
