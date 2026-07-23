#!/usr/bin/env bash
# Build a static ffmpeg/ffprobe from the official upstream source (github.com/FFmpeg/FFmpeg),
# not a third-party redistributor -- see review/22 and CHANGELOG.md for why: both prior pins
# (BtbN/FFmpeg-Builds' dated release tags, then johnvansickle.com) turned out to be unreliable
# hosts, and re-pinning to yet another mirror only moves the same problem. Building from the
# canonical source removes that dependency entirely; the resulting archive is meant to be
# vendored into B2 once per version via vendor_pinned_binary.py --local-file, not fetched by this
# script's caller on every run.
#
# LGPL only -- no --enable-gpl, ever. That keeps the binary under LGPLv2.1+ (no GPLv3 distribution
# notice/source-offer obligations for the published Docker image) while still covering decode
# broadly: FFmpeg's own native decoders (h264, hevc, vp8/vp9, av1, aac, mp3, opus, vorbis, ac3,
# flac, ...) are compiled in by default with a plain `./configure`, no external library needed.
# The four external libraries below are enabled purely to widen/improve *decode* coverage for
# codecs providers might serve (we don't control Granicus/Swagit/CivicPlus's encoding) and are
# all permissively licensed (BSD/LGPL, not GPL), so enabling them never requires --enable-gpl:
#   - libopus    (BSD)  -- better Opus decode/encode than FFmpeg's native decoder
#   - libvpx     (BSD)  -- VP8/VP9
#   - libdav1d   (BSD)  -- AV1 (FFmpeg's native AV1 decoder exists but dav1d is the reference one)
#   - libmp3lame (LGPL) -- real MP3 *encode* (decode is already native); unused today but cheap to
#                          carry so a future encode-side codec change doesn't need a rebuild-config
#                          change too.
#
# citypods' own encode usage (media.py) is exactly `-c:a aac` and `-c:a flac` -- both native to
# FFmpeg, no external library at all. Adding a new *encode* codec to citypods means adding a line
# to ENABLED_EXTERNAL_LIBS below, which is a normal, reviewable diff in this file -- that's the
# "register the dependency" contract for this build.
#
# Usage:
#   scripts/build_ffmpeg_static.sh <version e.g. 7.1.5> <output tar.xz path>
#
# Must run on a Debian/Ubuntu host (uses apt-get) with build tools available; ~15-30 minutes.

set -euo pipefail

VERSION="${1:?usage: build_ffmpeg_static.sh <version> <output-path>}"
OUTPUT_PATH="${2:?usage: build_ffmpeg_static.sh <version> <output-path>}"
GIT_TAG="n${VERSION}"

# The dependency declaration referenced above: edit this list (and re-run) to change what's built
# in. Every entry here must be permissively licensed (not GPL) -- see the header comment.
ENABLED_EXTERNAL_LIBS=(libopus libvpx libdav1d libmp3lame)

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "== Installing build dependencies =="
export DEBIAN_FRONTEND=noninteractive
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
  build-essential yasm nasm pkg-config git zlib1g-dev libgnutls28-dev \
  libopus-dev libvpx-dev libdav1d-dev libmp3lame-dev

echo "== Cloning FFmpeg ${GIT_TAG} =="
git clone --branch "$GIT_TAG" --depth 1 https://github.com/FFmpeg/FFmpeg.git "$WORK_DIR/ffmpeg-src"
cd "$WORK_DIR/ffmpeg-src"

CONFIGURE_ARGS=(
  --pkg-config-flags="--static"
  --enable-static
  --disable-shared
  --disable-doc
  --disable-debug
  --disable-ffplay
  # get_or_fetch() (media.py) feeds ffmpeg remote URLs directly via -i with an
  # http,https,tcp,tls protocol whitelist -- network protocol support (and TLS for https) is
  # load-bearing, not optional. GnuTLS, not OpenSSL: FFmpeg's configure requires
  # --enable-version3 (LGPLv3+, not the LGPLv2.1+ this script packages) to link OpenSSL >=3.0,
  # which is what Debian/Ubuntu ship -- GnuTLS's core library is LGPLv2.1+, no version bump needed.
  --enable-gnutls
)
for lib in "${ENABLED_EXTERNAL_LIBS[@]}"; do
  CONFIGURE_ARGS+=("--enable-${lib}")
done

echo "== Configuring (LGPL only -- no --enable-gpl) =="
./configure "${CONFIGURE_ARGS[@]}"

echo "== Building (this is the slow part) =="
make -j"$(nproc)"

echo "== Packaging =="
PACKAGE_DIR="$WORK_DIR/ffmpeg-${VERSION}-linux64-static"
mkdir -p "$PACKAGE_DIR"
cp ffmpeg "$PACKAGE_DIR/ffmpeg"
cp ffprobe "$PACKAGE_DIR/ffprobe"
# install_static_ffmpeg.py matches this by basename ("LICENSE.txt"), not path.
cp COPYING.LGPLv2.1 "$PACKAGE_DIR/LICENSE.txt"
"$PACKAGE_DIR/ffmpeg" -version
"$PACKAGE_DIR/ffprobe" -version

mkdir -p "$(dirname "$OUTPUT_PATH")"
tar -C "$WORK_DIR" -cJf "$OUTPUT_PATH" "$(basename "$PACKAGE_DIR")"
echo "== Wrote $OUTPUT_PATH =="
