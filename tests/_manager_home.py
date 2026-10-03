"""관리자 테스트의 실물 픽스처 — 실 `pm_import --new` PM 홈과 실 로컬 bare(네트워크 0).

옛 세대 사본은 흉내가 아니라 **실물**로 만든다: 진짜 PM 홈의 `pm_config.py` 를 `--json` 을
모르는 실제 스크립트로 갈아 끼우거나 지운다(가짜 runner·monkeypatch 로 rc 를 지어내지 않는다).
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
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


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv, cwd=None if cwd is None else str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert completed.returncode == 0, (
        f"{' '.join(argv)} rc={completed.returncode}\n{completed.stdout}\n{completed.stderr}")
    return completed


# 자식 fixture는 HOME이 격리돼도 pytest 설치 위치에 의존하지 않는 stdlib mock이다.
_KNOWN_CTX_READER_PY = "_CTX_FIXTURE_SOURCE_ROOT=" + repr(str(REPO.resolve())) + "\n" + r"""
from pathlib import Path
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

@contextmanager
def fixture_native_installation(folder=None):
    inherited=os.environ.get('_KNOWN_CTX_FIXTURE_BIN')
    if inherited and folder is None:
        yield Path(inherited)
        return
    with tempfile.TemporaryDirectory(prefix='known-ctx-native-') as temporary:
        native=Path(folder or temporary)
        native.mkdir(parents=True,exist_ok=True)
        for harness in ('claude','codex','opencode'):
            command=native/(harness+'.exe' if os.name=='nt' else harness)
            if not command.exists():
                command.write_text('#!/bin/sh\nexit 91\n',encoding='utf-8')
                command.chmod(0o755)
        with patch.dict(os.environ, {'PATH':str(native)+os.pathsep+os.environ.get('PATH',''),
                                    '_KNOWN_CTX_FIXTURE_BIN':str(native)}):
            yield native

def fixture_reader(module,capacity=200000,context=1000000):
    def reader(root,harness,documents,models):
        if Path(root).resolve() == Path(_CTX_FIXTURE_SOURCE_ROOT).resolve():
            raise ValueError('fixture source root write refused')
        source=Path(root)/'.project_manager/independent-runtime-fixture.json'
        source.parent.mkdir(parents=True,exist_ok=True)
        if not source.exists():
            source.write_text(json.dumps({'generation':'fixture-generation','usable':capacity,'context':context}),encoding='utf-8')
        identities=[{'model':m,'provider':p,'profile':''} for p,m in models] if harness=='opencode' else [{'model':'fixture-model','provider':'fixture-provider','profile':''}]
        rows=[]
        for identity in identities:
            metadata={**identity,'source':'independent-runtime-fixture','generation':'fixture-generation',
                'observed_at':'2026-10-02T00:00:00+00:00','valid_until':'2099-01-01T00:00:00+00:00',
                'independent':True,'semantics':'usable','usable':capacity,'context':context,'output':128000,
                'effective_output':32000,'native_formula':'input-reserved'}
            rows.append({'identity':identity,'metadata':metadata})
        # native 설치는 호출 범위의 PATH로 제공하며 전역 which와 목적지 트리는 수정하지 않는다.
        native=Path(shutil.which(harness) or '')
        if not native.is_file():
            raise ValueError('fixture native installation missing')
        native=native.resolve()
        evidence={str(source.resolve()):module.content_sha256(source.read_bytes())}
        if harness=='claude':
            settings=[str(source.resolve()),str(Path(root)/'.claude/settings.local.json'),str(Path(root)/'.project_manager/mock-managed.json')]
            roles={'settings':settings,'managed_bundle':'/etc/claude-code/managed-settings.d'}
            for path in settings:evidence[path]=module._ctx_file_digest(Path(path))
        elif harness=='codex':
            auth=str(Path(root)/'.project_manager/mock-auth.json');evidence[auth]=None
            roles={'manifest':str(source.resolve()),'selection':str(source.resolve()),'catalog':str(source.resolve()),'auth':auth}
        else:roles={'manifest':str(source.resolve())}
        return {'models':rows,'evidence':evidence,'roles':roles,
                'native_commands':{harness:str(native)},'native_files':{str(native):module._ctx_native_stat(native)}}
    return reader
"""

def known_ctx_reader(module, *, capacity=200000, context=1000000):
    namespace={}
    exec(_KNOWN_CTX_READER_PY,namespace)
    return namespace['fixture_reader'](module,capacity,context)


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


def known_ctx_module(module, *, capacity=200000, context=1000000):
    """정상 unit 설치 factory가 명시 호출하는 독립 mock, verified loader는 먼저 실행한다."""
    import os
    import shutil
    from functools import wraps
    from unittest.mock import patch
    namespace = {}
    exec(_KNOWN_CTX_READER_PY, namespace)
    if hasattr(module, '_read_harness_ctx_metadata'):
        module._read_harness_ctx_metadata = namespace['fixture_reader'](module, capacity, context)
    if getattr(module, '_t0932_known_ctx_loader', False):
        return module
    for name in ('main', '_apply_harness_ctx_values'):
        original=getattr(module,name,None)
        if original is None:continue
        @wraps(original)
        def installed(*args,_original=original,**kwargs):
            with namespace['fixture_native_installation']() as folder:
                previous=shutil.which
                def which(command,*args,**kwargs):
                    if command in ('claude','codex','opencode'):
                        return str(folder/(command+'.exe' if os.name=='nt' else command))
                    return previous(command,*args,**kwargs)
                with patch.object(shutil,'which',which):
                    return _original(*args,**kwargs)
        setattr(module,name,installed)
    for name in ('_load_pm_import', '_load_pm_update', '_load_module'):
        original = getattr(module, name, None)
        if original is None:
            continue
        def verified(*args, _original=original, **kwargs):
            loaded = _original(*args, **kwargs)
            if any(hasattr(loaded, key) for key in ('_read_harness_ctx_metadata', '_load_pm_import', '_load_pm_update', '_load_module')):
                known_ctx_module(loaded, capacity=capacity, context=context)
            return loaded
        setattr(module, name, verified)
    module._t0932_known_ctx_loader = True
    return module


_KNOWN_CTX_ENTRY_CHILD = (
    "_KNOWN_CTX_READER_PY=" + repr(_KNOWN_CTX_READER_PY) + "\n"
    + inspect.getsource(known_ctx_module)
    + r"""
import sys,importlib.util
from pathlib import Path
path=Path(sys.argv[1])
sys.argv=sys.argv[1:]
spec=importlib.util.spec_from_file_location('known_ctx_fixture_entry',path)
entry=importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
known_ctx_module(entry)
raise SystemExit(entry.main(sys.argv[1:]))
"""
)


def known_ctx_argv(argv):
    """정상 setup/갱신 fixture 자식 하나에만 독립 mock을 전달한다."""
    for index, part in enumerate(argv):
        if Path(str(part)).name in ('pm_import.py', 'pm_update.py', 'pm_config.py'):
            return [*argv[:index], '-c', _KNOWN_CTX_ENTRY_CHILD, *argv[index:]]
    return argv


# 정상 설치 baseline의 독립 C는 제품 source가 아닌 고정 unit metadata다.
# 주입은 make_pm_home setup 자식 하나에만 적용하며 후속 CLI/실제 환경으로 전파하지 않는다.
_KNOWN_SETUP_CHILD = _KNOWN_CTX_READER_PY + r"""
import importlib.util,sys
from pathlib import Path
path=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('known_setup_importer',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module._read_harness_ctx_metadata=fixture_reader(module)
with fixture_native_installation():
    raise SystemExit(module.main(sys.argv[2:]))
"""


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
    _run([argv[0], "-c", _KNOWN_SETUP_CHILD, argv[1], *argv[2:]], cwd=REPO)
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
    held = [lease.slot for lease in pool.list_leases() if lease.session == session]
    assert held, f"세션 {session!r} 이 잡은 행이 장부에 없다"
    for slot in held:
        pool.force_release(slot)
