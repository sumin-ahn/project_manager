"""관리자 테스트의 실물 픽스처 — 실 `pm_import --new` PM 홈과 실 로컬 bare(네트워크 0).

옛 세대 사본은 흉내가 아니라 **실물**로 만든다: 진짜 PM 홈의 `pm_config.py` 를 `--json` 을
모르는 실제 스크립트로 갈아 끼우거나 지운다(가짜 runner·monkeypatch 로 rc 를 지어내지 않는다).
"""
from __future__ import annotations

import hashlib
import importlib.util
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

# 끝난 세션이 남긴 대여를 만들 때 쓰는 pid — 이 값이 정말 죽어 있는지 엔진 seam 으로 확인한다.
DEAD_SESSION_PID = 999999

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

# `--keys` 는 알지만 `--json` 은 모르는 실제 등가물 — T-0911 직전 세대의 `main()` 그대로
# (`args == ["--keys"]` 완전 일치만 받고 그 밖은 사용법 rc 2).
OLD_GENERATION_KEY_REGISTRY = '''#!/usr/bin/env python3
"""옛 세대 local_conf.py — 키 표는 있고 기계 표면(`--json`)은 없다."""
import sys

if sys.argv[1:] == ["--keys"]:
    print("local.conf 키 — 사람 표(옛 세대)")
    raise SystemExit(0)
print("사용법: local_conf.py [--keys | --render-adapter-block <python|js|sh>]", file=sys.stderr)
raise SystemExit(2)
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


def install_old_generation_key_registry(pm_home: Path) -> None:
    """항목 목록 축의 옛 세대 — 그 사본의 `local_conf.py` 가 `--keys --json` 을 모른다."""
    (pm_home / ".project_manager" / "tools" / "local_conf.py").write_text(
        OLD_GENERATION_KEY_REGISTRY, encoding="utf-8")


def tree_snapshot(root: Path) -> dict[str, str]:
    """트리의 `{상대경로: 내용 해시}` — 생성·수정·삭제가 전부 키/값 차이로 드러난다.

    관리자의 계약은 "프로젝트 폴더에 직접 쓰는 파일은 `local.conf` 하나" 라, 조회·저장 전후로
    **무엇이 바뀌었는지 이름 단위로** 재야 한다(한 덩이 해시는 무엇이 바뀌었는지 말하지 못한다).
    """
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>")
    return snapshot


def _git(*argv: str) -> subprocess.CompletedProcess:
    """픽스처가 부르는 git — 커밋 정체성을 환경으로 준다(사용자 git config 비의존)."""
    environment = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.invalid")
    completed = subprocess.run(["git", *argv], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=environment)
    assert completed.returncode == 0, f"git {' '.join(argv)}\n{completed.stderr}"
    return completed


def make_bare_with_tags(root: Path, tags: tuple[str, ...] = (
        "v1.7.9", "v1.7.13", "wip/x")) -> Path:
    """로컬 bare 저장소 하나 — 태그는 실 git 이 단다(`v2.0.0` 은 annotated · peeled 행이 생긴다)."""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / "upstream.git"
    source = root / "source"
    _git("init", "-q", "--bare", str(bare))
    _git("init", "-q", str(source))
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git("-C", str(source), "add", "-A")
    _git("-C", str(source), "commit", "-qm", "init")
    for tag in tags:
        _git("-C", str(source), "tag", tag)
    _git("-C", str(source), "tag", "-a", "v2.0.0", "-m", "release")
    branch = _git("-C", str(source), "symbolic-ref", "--short", "HEAD").stdout.strip()
    _git("-C", str(source), "push", "-q", str(bare), "--tags", f"HEAD:refs/heads/{branch}")
    return bare


def make_upstream_checkout(root: Path, *, rev: str = "v9.9.9") -> Path:
    """이 트리의 실 clone 하나 — 엔진 rev 를 `rev` 로 올리고 같은 이름의 태그를 단다.

    갱신 축의 실물 상류다: `pm-config update` 가 파일을 복사해 갈 작업트리이면서
    `git ls-remote --tags` 가 최신 태그를 답하는 저장소다(네트워크 0 · 실측 3.5초라 모듈 단위
    픽스처로 한 번만 만든다). rev 는 엔진 자신의 `engine_rev.py --bump` 로 올린다 — 리터럴을
    손으로 고치면 stamped 형제 29개가 어긋나 그 사본이 skew 로 죽는다(실측).
    """
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / "upstream"
    _git("clone", "-q", "--no-local", str(REPO), str(checkout))
    _run([sys.executable, str(checkout / ".project_manager" / "tools" / "engine_rev.py"),
          "--bump", rev])
    _git("-C", str(checkout), "commit", "-qam", f"engine rev {rev}")
    _git("-C", str(checkout), "tag", rev)
    return checkout


def load_pool(pm_home: Path):
    """그 홈 사본의 `worktree_pool` 을 로드한다 — 경로 전역이 그 홈으로 굳는다(재배선 0)."""
    target = pm_home / ".project_manager" / "tools" / "worktree_pool.py"
    specification = importlib.util.spec_from_file_location(
        f"_worktree_pool_manager_home:{pm_home}", target)
    pool = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = pool
    specification.loader.exec_module(pool)
    return pool


def end_session(pool, session: str) -> None:
    """그 세션이 끝난 상태를 장부에 만든다 — 대여는 남고 pid 만 죽는다.

    장부에 적히는 pid 는 그 행을 만든 **세션**이라(T-0915), "회수 대기" 행은 세션이 끝나야
    생긴다. 테스트 프로세스가 자기 세션을 끝낼 수는 없으므로 그 세션이 잡은 행의 pid 만 죽은
    값으로 바꾼다 — 장부 IO 는 엔진 것을 그대로 쓴다(손으로 JSON 을 쓰지 않는다).
    """
    assert pool._pid_alive(DEAD_SESSION_PID) is False, "픽스처 전제: 이 pid 는 죽어 있어야 한다"
    with pool._lease_lock():
        leases = pool._read_ledger_strict()
        ended = [lease for lease in leases if lease.session == session]
        assert ended, f"세션 {session!r} 이 잡은 행이 장부에 없다"
        for lease in ended:
            lease.pid = DEAD_SESSION_PID
        pool._write_ledger(leases)
