"""관리자 테스트의 실물 픽스처 — 실 `pm_import --new` PM 홈과 실 로컬 bare(네트워크 0).

옛 세대 사본은 흉내가 아니라 **실물**로 만든다: 진짜 PM 홈의 `pm_config.py` 를 `--json` 을
모르는 실제 스크립트로 갈아 끼우거나 지운다(가짜 runner·monkeypatch 로 rc 를 지어내지 않는다).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 실 PM 홈·실 bare 를 만드는 픽스처는 전부 git 을 부른다(엔진이 부른다).
git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git 바이너리 없음")

# `--json` 을 모르는 옛 세대 사본의 실제 등가물(실측 rc 2 · argparse usage error).
OLD_GENERATION_SCRIPT = '''#!/usr/bin/env python3
"""옛 세대 pm_config.py — `status` 는 알지만 `--json` 은 모른다."""
import argparse
parser = argparse.ArgumentParser(prog="pm-config")
parser.add_subparsers(dest="command").add_parser("status")
parser.parse_args()
print("pool: (텍스트 출력)")
'''

SLOW_SCRIPT = '''#!/usr/bin/env python3
import time
time.sleep(30)
'''


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv, cwd=None if cwd is None else str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert completed.returncode == 0, (
        f"{' '.join(argv)} rc={completed.returncode}\n{completed.stdout}\n{completed.stderr}")
    return completed


def make_pm_home(destination: Path, *, upstream: Path | str | None = None) -> Path:
    """실 엔진으로 PM 홈을 만든다 (`pm_import.py --new` · 실측 1.6초 · 네트워크 0)."""
    argv = [sys.executable, str(TOOLS / "pm_import.py"), "--new", str(destination),
            "--harness", "claude", "--from", str(REPO)]
    if upstream is not None:
        argv += ["--upstream", str(upstream)]
    _run(argv, cwd=REPO)
    return Path(destination)


def pm_config(pm_home: Path, *args: str) -> subprocess.CompletedProcess:
    """그 사본의 `pm-config` 를 서브프로세스로 부른다(rc 0 강제)."""
    return _run([sys.executable, str(pm_home / ".project_manager" / "tools" / "pm_config.py"),
                 *args], cwd=pm_home)


def conf_path(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "local.conf"


def set_conf_key(pm_home: Path, key: str, value: str) -> None:
    """local.conf 의 키 하나를 실제로 바꾼다(중복 키는 last-wins 라 덧붙이면 그 값이 이긴다)."""
    path = conf_path(pm_home)
    text = path.read_text(encoding="utf-8")
    path.write_text(f"{text}\n{key}={value}\n", encoding="utf-8")


def engine_script(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "tools" / "pm_config.py"


def install_old_generation_engine(pm_home: Path) -> None:
    """실패 형태 ①: 서브커맨드는 있으나 `--json` 을 모른다(rc 2)."""
    engine_script(pm_home).write_text(OLD_GENERATION_SCRIPT, encoding="utf-8")


def remove_engine_script(pm_home: Path) -> None:
    """실패 형태 ②: `pm_config.py` 자체가 없다(인터프리터가 rc 2 로 죽는다)."""
    engine_script(pm_home).unlink()


def install_slow_engine(pm_home: Path) -> None:
    """상한을 재는 자리 — 응답하지 않는 사본."""
    engine_script(pm_home).write_text(SLOW_SCRIPT, encoding="utf-8")


def make_bare_with_tags(root: Path, tags: tuple[str, ...] = (
        "v1.7.9", "v1.7.13", "wip/x")) -> Path:
    """로컬 bare 저장소 하나 — 태그는 실 git 이 단다(`v2.0.0` 은 annotated · peeled 행이 생긴다)."""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / "upstream.git"
    source = root / "source"
    environment = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.invalid")

    def git(*argv: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(["git", *argv], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=environment)
        assert completed.returncode == 0, f"git {' '.join(argv)}\n{completed.stderr}"
        return completed

    git("init", "-q", "--bare", str(bare))
    git("init", "-q", str(source))
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    git("-C", str(source), "add", "-A")
    git("-C", str(source), "commit", "-qm", "init")
    for tag in tags:
        git("-C", str(source), "tag", tag)
    git("-C", str(source), "tag", "-a", "v2.0.0", "-m", "release")
    branch = git("-C", str(source), "symbolic-ref", "--short", "HEAD").stdout.strip()
    git("-C", str(source), "push", "-q", str(bare), "--tags", f"HEAD:refs/heads/{branch}")
    return bare
