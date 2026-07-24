# syntax=docker/dockerfile:1.7

# Ubuntu 24.04 (noble) -- deliberately NOT the official python:3.12-slim (Debian bookworm) image.
# ffmpeg/ffprobe are self-built (scripts/build_ffmpeg_static.sh) on GH Actions' ubuntu-latest
# (also noble) with FFmpeg's own libraries statically linked, but the external codec/TLS
# libraries link dynamically against that host's exact package versions -- see that script's
# header comment for why (gnutls's static dependency chain isn't resolvable via apt). Ubuntu
# noble and Debian bookworm ship different SONAMEs for some of those libraries (e.g. noble's
# libvpx9/libdav1d7 vs. bookworm's libvpx7/libdav1d6), so a bookworm base can't load this binary
# -- the base here has to match the build host, not just be "a recent Debian-family image".
# TODO: pin by digest once resolved from a real build log (review/22 base-image-immutability
# convention) -- Docker Hub isn't reachable from the sandbox this was authored in to look it up
# ahead of time.
FROM ubuntu:24.04

ARG FFMPEG_URL
ARG FFMPEG_SHA256

ENV PATH="/opt/citypods-ffmpeg/bin:${PATH}" \
    PYTHONPATH="/workspace" \
    PYTHONUNBUFFERED="1" \
    PIP_NO_CACHE_DIR="1" \
    DEBIAN_FRONTEND="noninteractive"

# python3.12 (noble's default python3), python-is-python3 (so `python` resolves, matching the
# ENTRYPOINT below), and the runtime (non -dev) packages for build_ffmpeg_static.sh's
# dynamically-linked codec/TLS libs -- exact SONAME match for the noble build host, see above.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.12 python3-pip python-is-python3 ca-certificates \
      libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0 \
    && rm -rf /var/lib/apt/lists/*

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
# Pin transitive deps to the compiled version-pinned constraints (review/22). A future
# hardening step can switch to a hash-verified install here (see constraints/README.md).
# --break-system-packages: Ubuntu's system python3-pip refuses a bare `pip install` (PEP 668) --
# fine here, this image has no other Python workload to protect from an unmanaged pip install.
RUN pip install --no-cache-dir --break-system-packages \
      -c /opt/citypods-src/constraints/prod.txt "/opt/citypods-src[storage]"

WORKDIR /workspace
ENTRYPOINT ["python", "-m", "citypods.cli"]
