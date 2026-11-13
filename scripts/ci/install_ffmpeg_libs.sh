#!/usr/bin/env bash
# Install the runtime libraries the self-built static ffmpeg links dynamically
# (scripts/build_ffmpeg_static.sh), on a GitHub-hosted ubuntu runner.
#
# Shared by every workflow that installs the pinned ffmpeg, so the hardening lives in one
# place (review/58 R2-B; the original problem was seen 2026-11-11, PR #2652):
#   * skip apt entirely when every SONAME is already present (and any extra packages);
#   * drop azure.archive.ubuntu.com from the mirror list when it does not resolve;
#   * if the runner's resolver cannot see archive.ubuntu.com / security.ubuntu.com, look them
#     up on a public resolver and pin them in /etc/hosts for this job (apt still verifies
#     package signatures);
#   * bound apt with retries and timeouts, and print resolver state if it still fails.
#
# Usage: scripts/ci/install_ffmpeg_libs.sh [extra-apt-package ...]
# Callers must set a step `timeout-minutes` (worst case here is about 5 minutes).
#
# Test hooks (used by tests/test_ci_install_ffmpeg_libs.py, never set in workflows):
#   FFMPEG_LIBS_LDCONFIG_CACHE  file to read instead of `ldconfig -p`
#   FFMPEG_LIBS_MIRRORS_FILE    mirror list to rewrite instead of /etc/apt/apt-mirrors.txt
#   FFMPEG_LIBS_HOSTS_FILE      hosts file to append pins to instead of /etc/hosts
set -euo pipefail

PACKAGES=(libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0)
SONAMES=(libgnutls.so.30 libopus.so.0 libvpx.so.9 libdav1d.so.7 libmp3lame.so.0)
EXTRA_PACKAGES=("$@")

MIRRORS_FILE="${FFMPEG_LIBS_MIRRORS_FILE:-/etc/apt/apt-mirrors.txt}"
HOSTS_FILE="${FFMPEG_LIBS_HOSTS_FILE:-/etc/hosts}"

ldconfig_listing() {
  if [[ -n "${FFMPEG_LIBS_LDCONFIG_CACHE:-}" ]]; then
    cat "${FFMPEG_LIBS_LDCONFIG_CACHE}"
  else
    ldconfig -p
  fi
}

missing=""
listing="$(ldconfig_listing)"
for so in "${SONAMES[@]}"; do
  grep -q -F -- "${so}" <<<"${listing}" || missing="${missing} ${so}"
done
for pkg in ${EXTRA_PACKAGES[@]+"${EXTRA_PACKAGES[@]}"}; do
  if ! dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "install ok installed"; then
    missing="${missing} ${pkg}"
  fi
done
if [[ -z "${missing}" ]]; then
  echo "FFmpeg runtime libraries already present; skipping apt"
  exit 0
fi
echo "Missing:${missing}"

# Rewrite the mirror list when the Azure mirror does not resolve.
if ! getent hosts azure.archive.ubuntu.com >/dev/null 2>&1; then
  echo "::warning::azure.archive.ubuntu.com does not resolve; using the fallback apt mirrors"
  trimmed="$(mktemp)"
  grep -v 'azure\.archive\.ubuntu\.com' "${MIRRORS_FILE}" >"${trimmed}" || true
  sudo cp "${trimmed}" "${MIRRORS_FILE}"
  rm -f "${trimmed}"
fi

# If the runner's resolver cannot see the Ubuntu archive at all, pin public-resolver answers.
for host in archive.ubuntu.com security.ubuntu.com; do
  if ! getent hosts "${host}" >/dev/null 2>&1; then
    ip="$(dig +short +time=3 +tries=2 @1.1.1.1 A "${host}" 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)"
    if [[ -n "${ip}" ]]; then
      echo "::warning::${host} does not resolve on the runner; pinning it to ${ip} for this job"
      echo "${ip} ${host}" | sudo tee -a "${HOSTS_FILE}" >/dev/null
    fi
  fi
done

apt_opts=(-o Acquire::Retries=2 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)

diagnose() {
  echo "::error::apt could not install the FFmpeg runtime libraries" >&2
  cat "${MIRRORS_FILE}" 2>/dev/null || true
  for host in archive.ubuntu.com security.ubuntu.com pypi.org; do
    getent hosts "${host}" || echo "${host} does not resolve"
  done
  { resolvectl status 2>/dev/null | head -20; } || true
  exit 1
}

timeout 90 sudo apt-get "${apt_opts[@]}" update || diagnose
timeout 150 sudo apt-get "${apt_opts[@]}" install -y -qq --no-install-recommends \
  "${PACKAGES[@]}" ${EXTRA_PACKAGES[@]+"${EXTRA_PACKAGES[@]}"} || diagnose
