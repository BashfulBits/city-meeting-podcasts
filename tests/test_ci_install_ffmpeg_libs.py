"""Behavioural tests for scripts/ci/install_ffmpeg_libs.sh and compile_constraints.sh.

The scripts run on GitHub runners with sudo/apt; here every external command is a stub on PATH
(getent, dig, sudo, timeout, apt-get, dpkg-query) and the system files are redirected through the
script's test hooks, so nothing touches the developer machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "install_ffmpeg_libs.sh"
COMPILE = ROOT / "scripts" / "compile_constraints.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")

ALL_SONAMES = ("libgnutls.so.30 libopus.so.0 libvpx.so.9 libdav1d.so.7 libmp3lame.so.0").split()


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    unresolvable = tmp_path / "unresolvable.txt"
    unresolvable.write_text("")
    # getent fails for every host listed in unresolvable.txt.
    _stub(
        bin_dir / "getent",
        f'grep -qx -- "$2" "{unresolvable}" && exit 2\necho "1.2.3.4 $2"\n',
    )
    _stub(bin_dir / "dig", 'echo "9.9.9.9"\n')
    _stub(bin_dir / "sudo", 'exec "$@"\n')
    _stub(bin_dir / "timeout", 'shift\nexec "$@"\n')
    _stub(bin_dir / "apt-get", f'echo "apt-get $*" >> "{log}"\n')
    _stub(bin_dir / "dpkg-query", "exit 1\n")
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text(
        "http://azure.archive.ubuntu.com/ubuntu/\nhttp://archive.ubuntu.com/ubuntu/\n"
    )
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    cache = tmp_path / "ldconfig.txt"
    cache.write_text("\n".join(f"\t{so} (libc6,x86-64) => /lib/{so}" for so in ALL_SONAMES))
    environ = dict(os.environ)
    environ.update(
        PATH=f"{bin_dir}:{environ['PATH']}",
        FFMPEG_LIBS_LDCONFIG_CACHE=str(cache),
        FFMPEG_LIBS_MIRRORS_FILE=str(mirrors),
        FFMPEG_LIBS_HOSTS_FILE=str(hosts),
    )
    return {
        "env": environ,
        "log": log,
        "mirrors": mirrors,
        "hosts": hosts,
        "cache": cache,
        "unresolvable": unresolvable,
        "bin": bin_dir,
    }


def _run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env["env"], capture_output=True, text=True, timeout=30
    )


def test_skips_apt_when_every_library_is_present(env):
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "skipping apt" in result.stdout
    assert not env["log"].exists()


def test_installs_when_a_library_is_missing_and_leaves_healthy_mirrors_alone(env):
    env["cache"].write_text("\tlibopus.so.0 (libc6,x86-64) => /lib/libopus.so.0\n")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    calls = env["log"].read_text().splitlines()
    assert len(calls) == 2 and calls[0].endswith("update")
    assert "install -y -qq --no-install-recommends libgnutls30 libopus0 libvpx9" in calls[1]
    assert "Acquire::Retries=2" in calls[0]
    assert "azure.archive.ubuntu.com" in env["mirrors"].read_text()
    assert env["hosts"].read_text() == "127.0.0.1 localhost\n"


def test_drops_unresolvable_azure_mirror_only(env):
    env["cache"].write_text("")
    env["unresolvable"].write_text("azure.archive.ubuntu.com\n")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert env["mirrors"].read_text() == "http://archive.ubuntu.com/ubuntu/\n"
    assert "::warning::azure.archive.ubuntu.com does not resolve" in result.stdout


def test_pins_archive_hosts_via_public_resolver_when_runner_cannot_resolve(env):
    env["cache"].write_text("")
    env["unresolvable"].write_text("archive.ubuntu.com\nsecurity.ubuntu.com\n")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    hosts = env["hosts"].read_text()
    assert "9.9.9.9 archive.ubuntu.com" in hosts
    assert "9.9.9.9 security.ubuntu.com" in hosts


def test_extra_packages_are_installed_and_checked(env):
    # Libraries present, but the extra package is not installed (dpkg-query stub fails).
    result = _run(env, "poppler-utils", "tesseract-ocr")
    assert result.returncode == 0, result.stderr
    assert "Missing: poppler-utils tesseract-ocr" in result.stdout
    install = env["log"].read_text().splitlines()[-1]
    assert install.endswith("libmp3lame0 poppler-utils tesseract-ocr")


def test_apt_failure_prints_diagnostics_and_fails(env):
    env["cache"].write_text("")
    _stub(env["bin"] / "apt-get", "exit 100\n")
    result = _run(env)
    assert result.returncode == 1
    assert "::error::apt could not install the FFmpeg runtime libraries" in result.stderr
    assert "http://archive.ubuntu.com/ubuntu/" in result.stdout


def test_compile_constraints_timeout_is_a_failure_not_a_skip(tmp_path):
    """A pip-compile that hangs is killed, retried once, and then fails the script: pip-compile
    only writes output on success, so reporting success here would pass the drift diff."""
    if shutil.which("timeout") is None and shutil.which("gtimeout") is None:
        pytest.skip("GNU timeout required")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = shutil.which("timeout") or shutil.which("gtimeout")
    _stub(bin_dir / "timeout", f'exec "{runner}" "$@"\n')
    _stub(bin_dir / "python", "exit 0\n")
    _stub(bin_dir / "pip-compile", "sleep 30\n")
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(COMPILE, work / "scripts" / "compile_constraints.sh")
    environ = dict(os.environ)
    environ.update(PATH=f"{bin_dir}:{environ['PATH']}", IN_CONTAINER="1", LANE_TIMEOUT="1")
    result = subprocess.run(
        ["bash", str(work / "scripts" / "compile_constraints.sh")],
        cwd=work,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "retrying prod once" in result.stdout
    assert "timed out twice" in result.stderr
    assert "compiling constraints/asr.txt" not in result.stdout


def test_compile_constraints_real_resolution_failure_is_not_retried(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "timeout", 'shift 2\nexec "$@"\n')
    _stub(bin_dir / "python", "exit 0\n")
    _stub(bin_dir / "pip-compile", 'echo "No matching distribution"; exit 2\n')
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(COMPILE, work / "scripts" / "compile_constraints.sh")
    environ = dict(os.environ)
    environ.update(PATH=f"{bin_dir}:{environ['PATH']}", IN_CONTAINER="1")
    result = subprocess.run(
        ["bash", str(work / "scripts" / "compile_constraints.sh")],
        cwd=work,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "retrying" not in result.stdout
    assert "No matching distribution" in result.stdout
    assert "pip-compile failed for constraints/prod.txt (exit 2)" in result.stderr
