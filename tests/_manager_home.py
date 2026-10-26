"""관리자 테스트의 실물 픽스처 — 실 `pm_import --new` PM 홈과 실 로컬 bare(네트워크 0).

옛 세대 사본은 흉내가 아니라 **실물**로 만든다: 진짜 PM 홈의 `pm_config.py` 를 `--json` 을
모르는 실제 스크립트로 갈아 끼우거나 지운다(가짜 runner·monkeypatch 로 rc 를 지어내지 않는다).
"""
from __future__ import annotations
from pm_engine.app import worktree_pool
from pm_engine.infra import pm_paths, pool_store

import contextlib
import hashlib
import importlib.util
import json
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


# 선택지·권장값 이전 세대(schema 1)의 실제 등가물 — `--keys --json` 은 알지만 행에 `choices`·
# `recommended` 가 없다. 화면이 그 필드를 읽으므로 관리자는 이 산출을 반쯤 그리지 않는다.
PREVIOUS_GENERATION_KEY_REGISTRY = '''#!/usr/bin/env python3
"""한 세대 앞선 local_conf.py — 키 목록은 기계 판독인데 선택지·권장값이 없다."""
import json
import sys

if sys.argv[1:] == ["--keys", "--json"]:
    print(json.dumps({
        "schema": 1,
        "groups": ["runtime"],
        "keys": [{"key": "runtime.py", "group": "runtime", "description": "사본을 부를 인터프리터",
                  "default": None, "required": True, "kind": "key", "members": None}],
    }, ensure_ascii=False))
    raise SystemExit(0)
print("사용법: local_conf.py [--keys [--json]]", file=sys.stderr)
raise SystemExit(2)
'''


def _run(argv: list[str], *, cwd: Path | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv, cwd=None if cwd is None else str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env)
    assert completed.returncode == 0, (
        f"{' '.join(argv)} rc={completed.returncode}\n{completed.stdout}\n{completed.stderr}")
    return completed


# 모델 한도 조회 교체 지점 두 곳(`_read_harness_ctx_metadata`·`_ctx_claude_threshold`)의 고정 값.
# 단일 출처는 환경 주입 폴더의 `sitecustomize.py` 다 — 같은 프로세스 교체(`known_ctx_seams`·
# `install_known_ctx_io`)와 `-c` 자식 스크립트도 그 본문을 그대로 쓴다(교체 설치는 `sitecustomize` 로
# 읽힐 때만 일어난다).
_KNOWN_CTX_SITE = Path(__file__).resolve().parent / "_known_ctx_site"
_KNOWN_CTX_READER_PY = (_KNOWN_CTX_SITE / "sitecustomize.py").read_text(encoding="utf-8")


def known_ctx_seams(*, capacity=200000, context=1000000, model="fixture-model") -> dict:
    """모델 한도 조회 교체 지점 이름 → 고정 값 함수 (monkeypatch 로 붙인다)."""
    namespace = {}
    exec(_KNOWN_CTX_READER_PY, namespace)
    return {"_read_harness_ctx_metadata": namespace["fixture_reader"](capacity, context, model),
            "_ctx_claude_threshold": namespace["fixture_threshold"]}


@pytest.fixture(autouse=True)
def _known_ctx_installation(tmp_path,monkeypatch):
    namespace={}
    exec(_KNOWN_CTX_READER_PY,namespace)
    folder=tmp_path/'known-native-bin'
    with namespace['fixture_native_installation'](folder):
        pass
    monkeypatch.setenv('PATH',str(folder)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('_KNOWN_CTX_FIXTURE_BIN',str(folder))
    return folder


def install_known_ctx_io(monkeypatch) -> None:
    """이 시험 동안 `app.harness_ctx` 의 모델 한도 조회 두 IO 함수를 고정 값으로 바꾼다(teardown 이 복원).

    엔진 package 는 프로세스에 하나라 설치·갱신·설정 어느 진입이 부르든 같은 함수를 지난다.
    conftest 의 `known_ctx_io` fixture 가 이 함수를 부른다."""
    from pm_engine.app import harness_ctx
    for name, seam in known_ctx_seams().items():
        monkeypatch.setattr(harness_ctx, name, seam)


def known_ctx_env(env: dict | None = None, *, native_bin: Path | None = None,
                  capacity: int | None = None, context: int | None = None) -> dict:
    """자식 프로세스 환경 — 그 프로세스와 그 자식(갱신의 완료 프로세스 포함)이 고정 모델 한도를 쓴다.

    `PYTHONPATH` 앞에 `_known_ctx_site`(sitecustomize)를 둔다. 실제 하네스 CLI 자리를 대신할 실행
    파일 폴더(`_KNOWN_CTX_FIXTURE_BIN`)가 환경에 없으면 `native_bin`(테스트 임시 폴더 안 경로)에 만들어
    `PATH` 앞에 붙인다 — 둘 다 없으면 `ValueError`. `capacity`·`context` 를 주면 고정 값을 바꾼다
    (`sitecustomize` 가 읽는 두 변수)."""
    environment = dict(os.environ if env is None else env)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(_KNOWN_CTX_SITE), *filter(None, [environment.get("PYTHONPATH")])])
    if not environment.get("_KNOWN_CTX_FIXTURE_BIN"):
        if native_bin is None:
            raise ValueError("known_ctx_env: 환경에 _KNOWN_CTX_FIXTURE_BIN 이 없으면 native_bin"
                             "(테스트 임시 폴더 안 경로)을 넘겨야 한다")
        namespace: dict = {}
        exec(_KNOWN_CTX_READER_PY, namespace)
        folder = namespace["write_native_commands"](native_bin)
        environment["PATH"] = str(folder) + os.pathsep + environment.get("PATH", "")
        environment["_KNOWN_CTX_FIXTURE_BIN"] = str(folder)
    if capacity is not None:
        environment["_KNOWN_CTX_CAPACITY"] = str(capacity)
    if context is not None:
        environment["_KNOWN_CTX_CONTEXT"] = str(context)
    return environment


def use_known_ctx_env(monkeypatch, native_bin: Path) -> None:
    """이 테스트 프로세스의 환경을 `known_ctx_env` 로 바꾼다 — 환경을 따로 넘기지 않는 자식(관리자
    브리지 등)과 그 자식의 완료 프로세스가 상속한다."""
    for key, value in known_ctx_env(native_bin=native_bin).items():
        if os.environ.get(key) != value:
            monkeypatch.setenv(key, value)


@contextlib.contextmanager
def complete_in_process(monkeypatch):
    """같은 프로세스 테스트의 완료 단계 교체 구간.

    전달 프로세스는 받은 갱신기를 새 프로세스로 띄운다(`app.engine_transfer._launch_completion`). 합성
    dest 에는 그 갱신기가 없으므로 이 구간 동안 그 IO 함수를 바꿔, 인계 값(`PM_UPDATE_COMPLETION`)을
    인계된 dest·source·전달 수로 묶은 채 `cli.update_cmd.main` 을 인계 dest 의 Home 과 원래 argv 로 부르고
    인계 값을 원래대로 돌린다. 구간을 벗어나면 교체도 풀린다(다른 테스트에 남지 않는다). 이 교체가 빠진
    자기 갱신 호출은 dest 에 갱신기가 없어 rc 2 로 실패한다(조용히 지나가지 않는다)."""
    from pm_engine.app import engine_transfer
    from pm_engine.cli import update_cmd
    from pm_engine.infra import pm_paths

    def launch(dest_root, source_root, transferred, argv):
        name = engine_transfer.COMPLETION_ENV
        previous = os.environ.get(name)
        os.environ[name] = json.dumps({"dest": str(dest_root), "source": str(source_root),
                                       "transferred": transferred})
        try:
            return update_cmd.main(pm_paths.home_at(Path(dest_root)), list(argv))
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    with monkeypatch.context() as patch:
        patch.setattr(engine_transfer, "_launch_completion", launch)
        yield


def make_pm_home(destination: Path, *, upstream: Path | str | None = None,
                 source: Path = REPO) -> Path:
    """실 엔진으로 PM 홈을 만든다 (`pm_import.py --new` · 실측 1.6초 · 네트워크 0).

    `source` 는 설치 소스다. 설치 기준 커밋과 대조하는 조회(어댑터 차이)나 상류 clone 을 쓰는
    테스트는 `make_committed_source` 가 만든 저장소를 넘긴다 — 이 트리를 그대로 쓰면 커밋 전
    변경이 있을 때 결과가 이 트리의 커밋 여부에 따라 달라진다.
    """
    argv = [sys.executable, str(TOOLS / "pm_import.py"), "--new", str(destination),
            "--harness", "claude", "--from", str(source)]
    if upstream is not None:
        argv += ["--upstream", str(upstream)]
    _run(argv, cwd=REPO, env=known_ctx_env(native_bin=Path(destination).parent / "known-ctx-native"))
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


def install_previous_generation_key_registry(pm_home: Path) -> None:
    """항목 목록 축의 한 세대 앞선 사본 — 기계 표면은 있고 선택지·권장값(schema 2)이 없다."""
    (pm_home / ".project_manager" / "tools" / "local_conf.py").write_text(
        PREVIOUS_GENERATION_KEY_REGISTRY, encoding="utf-8")


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


def make_committed_source(root: Path) -> Path:
    """이 트리의 작업 트리를 커밋 하나로 담은 임시 저장소 — 설치 소스·경로 upstream 으로 쓴다.

    설치(`pm_import --new --from <소스>`)는 소스의 **작업 트리** 파일을 깔고 설치 기준으로 소스
    HEAD 를 적는다(엔진 동작). 이 트리를 그대로 소스로 쓰면 커밋 전 변경이 있을 때 두 값이
    갈라져, 기준 커밋과 대조하는 조회와 상류 clone 의 내용이 이 트리의 커밋 여부를 따라간다.
    추적 파일과 무시되지 않은 새 파일(커밋이 담을 집합)을 작업 트리 내용 그대로 복사해 한 커밋으로
    박으면, 설치가 까는 내용과 설치 기준 커밋이 늘 같다.
    """
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source"
    listed = _git("-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard").stdout
    for relpath in listed.split("\0"):
        origin = REPO / relpath
        if not relpath or not origin.is_file():
            continue            # 작업 트리에서 지운 추적 파일은 커밋에도 없다.
        target = source / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
    _git("init", "-q", str(source))
    # 무시 규칙에 걸려도 추적 중인 파일이 있으면 그대로 담는다 — 커밋은 복사한 집합과 같아야 한다.
    _git("-C", str(source), "add", "-A", "--force")
    _git("-C", str(source), "commit", "-qm", "working tree")
    return source.resolve()


def make_upstream_checkout(root: Path, *, source: Path, rev: str = "v9.9.9") -> Path:
    """설치 소스(`make_committed_source`)의 실 clone 하나 — 엔진 rev 를 `rev` 로 올리고 같은
    이름의 태그를 단다.

    갱신 축의 실물 상류다: `pm-config update` 가 파일을 복사해 갈 작업트리이면서
    `git ls-remote --tags` 가 최신 태그를 답하는 저장소다(네트워크 0 · 모듈 단위 픽스처로 한 번만
    만든다). 홈도 같은 `source` 로 설치한다 — 설치 기준 커밋이 이 clone 의 이력에 있고 상류가 홈보다
    rev 하나만 앞선다(이 트리를 직접 clone 하면 커밋 전 변경이 빠져 상류 내용이 홈보다 뒤로 간다).
    rev 는 엔진 자신의 `engine_rev.py --bump` 로 올린다 — 리터럴을 손으로 고치면 stamped 형제
    29개가 어긋나 그 사본이 skew 로 죽는다(실측).
    """
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / "upstream"
    _git("clone", "-q", "--no-local", str(source), str(checkout))
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


def close_session(pool, session: str) -> None:
    """그 세션의 기록을 닫는다 — 종료 기록(release)이 곧 닫힘이다.

    프로세스 번호로 죽음을 흉내 내던 자리다. 이제 열림/닫힘은 기록 축이라 엔진의 반납
    프리미티브를 그대로 부른다(손으로 JSON 을 쓰지 않는다).
    """
    held = [lease.slot for lease in pool_store.list_leases(pm_paths.home_at(pool.REPO)) if lease.session == session]
    assert held, f"세션 {session!r} 이 잡은 행이 장부에 없다"
    for slot in held:
        worktree_pool.force_release(pm_paths.home_at(pool.REPO), slot)


@pytest.fixture
def render_home(tmp_path):
    """실행 코드의 스킬 inventory를 임시 Home에 복제한다."""
    from pm_engine.infra import pm_paths, skill_inventory
    root = tmp_path / "render-engine"
    root.mkdir(exist_ok=True)
    for name in skill_inventory._installed_skill_entry_names(pm_paths.home_at(REPO)):
        copied = root / ".agents/skills" / name / "SKILL.md"
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_text("fixture", encoding="utf-8")
    return pm_paths.home_at(root)
