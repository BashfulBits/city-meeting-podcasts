- **Restore FFmpeg runtime package installation.** Pin the remaining host FFmpeg jobs and
  the FFmpeg source-build job to Ubuntu 24.04, matching the existing bundle's codec/TLS ABI.
  GitHub's `ubuntu-latest` migration to Ubuntu 26.04 caused `libvpx9` installation failures.
  CI's test job was already pinned separately. Workflow tests prevent build/consumer drift.
  FFmpeg bytes, pipeline versions and stored artifacts are unchanged; no backfill is triggered.
