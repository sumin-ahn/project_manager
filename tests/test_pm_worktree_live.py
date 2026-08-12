"""pm-worktree 스킬 라이브 하네스 테스트 — 실 LLM 시나리오 → 실 git 단언 (ADR-0050 파일럿 T-δ).

`pm-worktree` 스킬(T-0277·SKILL.md/opencode command)은 **LLM-facing 프롬프트**라 backbone pytest
(`test_worktree_pool.py`)로는 "스크립트가 옳게 도나"만 검증되고 "**실 LLM 이 스킬 읽고 옳게
관리하나**"는 미검증이다([[ADR-0050]]·[[harness-test-vs-machine-test]]). 이 파일이 그 갭을
릴리즈 라이브 tier 에서 메운다 — fresh import 홈에 실 git 슬롯(2 submodule)을 세우고, **스킬을
유일 컨텍스트**로 준 실 LLM 2하네스(claude + opencode/glm-5.2:cloud·[[opencode-live-model-glm52]])에
운영중-관리 시나리오를 시켜 **실 git 상태**로 단언한다:

  1. **dev submodule no-clobber** — dev 지정한 submodule 이 재동기 후에도 그 dev 브랜치를 보존
     (detached pin 으로 안 낚아채임·ADR-0051 크럭스 A).
  2. **consume submodule drift 재동기** — detached(consume) submodule 은 pin 으로 재동기됨.
  3. **슬롯 브랜치 tracking 없음** — 슬롯 브랜치가 origin/<base> upstream tracking 안 걸림(--no-track).

**mock 맹점 백스톱**(ADR-0050 §Context): T-0274 에서 mock argv 단언이 git 의 *자동* 동작
(autoSetupMerge)을 못 잡아 false-green 났다 — 실 git + 실 LLM 만이 그 백스톱이다
([[verify-real-output-not-just-review]]). 전역 `submodule.recurse` 낚아채임(크럭스 A)도 같은 이유.

판정 철학(release_wave·command_card_usability 상속):
- **side-effect(실 git 상태) 기반** — LLM 출력 phrasing 비결정에 강건. 초기 상태를 *비공허*
  (dev submodule=detached·consume=drift)로 세워 **LLM 이 아무것도 안 하면 3 단언이 fail** → silent
  pass 불가(false-green 가드의 핵심). 명령 미실행(초기 git 상태 그대로)이면 명시 fail.
- **관측 수단 비대칭**(release_wave 위임-관측 비대칭 상속): claude=stream-json 의 Bash tool-call 로
  backbone(dev·sync) 실행을 **hard** 관측 / opencode=stream-json 처럼 정밀 노출 X(stdout 스캔뿐)이라
  side-effect 만 hard·커맨드 관측은 **best-effort**.

게이트 아님 — 사용자가 릴리즈 직전 `PM_ORCH_LIVE_RELEASE=1` 로 occasional 트리거(비용·flaky 감수).
기본 skip(env 미설정·바이너리 부재·CI green 불변). 이 파일은 그 외 **always-run hermetic 가드**도
보유한다 — 라이브 미실행 시에도 (1) 스킬 파일 존재·backbone 참조 (2) 프롬프트 구조 (3) fixture 가
비공허 초기 상태를 만드는지 (4) backbone 이 시나리오를 수준에서 성립시키는지 (5) release 마커 수를
실 git 으로 exercise·pin 한다(setup rot·마커 소실 시 라이브가 가짜 skip/pass 로 숨지 않게).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest

# release_wave/runtime_smoke 헬퍼 재사용(중복 인프라 금지·같은 tests/ 디렉토리 import) —
# adopter import(hermetic·models 조회 차단)·LLM env 격리(화이트리스트)·claude stream-json Bash 파싱.
from test_fresh_adopter_runtime_smoke import _import_adopter, _live_env
from test_command_card_usability import _collect_bash_commands

REPO = Path(__file__).resolve().parents[1]

# 릴리즈 트리거 — 사용자가 릴리즈 직전 명시 set(occasional). 미설정이면 라이브 전부 skip(CI green 불변).
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# claude: sonnet-4-6(API 과금·env override). opencode: glm-5.2:cloud(ollama cloud·2026-07-07 채택·
# 과금 0·env override). release_wave/command_card_usability 와 동일 단일 진실(양쪽 default 일치).
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
# opencode 는 느리고 변동 커 1800s, claude 는 dev+sync 2콜 여유분 600s (release_wave 상속).
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", "600"))

# 검증 대상 스킬(① canonical·T-0277). 이 라이브 케이스는 모델 skill tool 채널을
# 검증하므로 opencode skill 미러를 *유일 컨텍스트*로 임베드한다(진입문서 경로 미제공·스킬
# 사용성). backbone=worktree_pool.py(dev/sync).
_CLAUDE_SKILL = REPO / ".claude" / "skills" / "pm-worktree" / "SKILL.md"
_OPENCODE_SKILL = REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-worktree" / "SKILL.md"

# 시나리오 파라미터(단일 진실) — 슬롯/submodule/dev 브랜치/base. repo 이름은 슬롯 식별자
# 네임스페이스(`work/<repo>_<N>`·소문자·`_SLOT_ID_RE` 통과). dev_sub=작업 중 선언(→ sync skip)·
# consume_sub=detached drift(→ sync 재동기 대상).
_REPO_NAME = "wtlive"
_SLOT = f"work/{_REPO_NAME}_1"
_DEV_SUB = "libs/dev"
_CONSUME_SUB = "vendor/consume"
_DEV_BRANCH = "mywork"
_BASE_BRANCH = "develop"

# 이 파일 release 라이브 케이스 수(claude+opencode) — 마커 소실/개명 시 게이트 selection 에서
# 조용히 빠지는 것 방어(pin·아래 `test_release_markers_pinned`). 라이브를 의도적으로 늘리면 함께 갱신.
_EXPECTED_RELEASE_TESTS = 2

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")

# 테스트-전용 file:// submodule 프로토콜 우회 — 픽스처의 submodule origin 은 로컬 file:// 경로라
# git 이 보안상(CVE-2022-39253) 기본 차단한다. GIT_CONFIG_* 로 `protocol.file.allow=always` 를
# 주입해 그 차단만 푼다(실 ssh/https submodule 엔 무관한 테스트 우회·엔진 코드는 `-c` 를 안 박아
# 실전 동작 영향 0·`test_real_git_submodule_init_in_new_slot` 동형). setup 은 이걸 os.environ 에
# monkeypatch 로 박아(엔진 un-injected 실 runner 도 상속) 라이브 subprocess env 에도 얹는다.
_FILE_PROTO_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


# ── 실 git 헬퍼 (hermetic·임시 git repo·test_worktree_pool 동형) ────────────────
def _git(cwd, *argv):
    """테스트용 실 git 헬퍼 — check=True·UTF-8 캡처·author/committer 고정."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _init_repo(path):
    """초기 커밋 있는 git repo(main·1 commit)를 만든다 (worktree add/clone base)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _load_wp_from_home(home: Path):
    """홈의 `worktree_pool.py` 를 로드한다 — REPO 전역이 자기 위치(=home)로 자동 해소된다.

    `REPO = Path(__file__).resolve().parents[2]` 라, 홈 자신의 tools 에서 로드하면 REPO·
    LEASES_FILE·WORK_DIR·REPOS_DIR 가 전부 home-상대로 굳는다(`_load_wp_bound` 처럼 override
    불요). 라이브 LLM 이 부르는 것과 *같은* 파일이라 슬롯 경로 해소가 일치한다.
    """
    path = home / ".project_manager" / "tools" / "worktree_pool.py"
    spec = importlib.util.spec_from_file_location(f"wp_live_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_Fixture = namedtuple("_Fixture", "home slot_dir consume_pin consume_c1")


def _setup_home_with_submodule_slot(tmp_path: Path, harness: str, monkeypatch) -> _Fixture:
    """라이브 fixture — fresh 홈 + 실 git 슬롯(2 submodule·--no-track·consume drift)을 세운다.

    라이브 LLM 이 pm-worktree 스킬로 운영할 실제 슬롯을 만든다(전부 hermetic·실 git·file:// origin):
      1. `_import_adopter` 로 harness 홈(엔진 tools 포함·worktree_pool.py) import.
      2. submodule origin 2개(consume: 2커밋 v1/v2·dev: 2커밋) + family origin(develop 에 두 submodule
         add·pin=최신 v2) → `.repos/<repo>.git` bare(ADR-0011 §31) clone + refspec + fetch +
         `branch.autoSetupMerge=true`(--no-track 억제 조건 명시).
      3. `create_slot(base=develop, init_submodules=True)` → 슬롯 `work/<repo>_1` 을 *origin/develop*
         에서 `--no-track`(assertion 3 백스톱)로 파고 submodule 을 init(둘 다 detached pin).
      4. consume submodule 을 pin(v2)과 어긋난 detached(v1=c1)로 → **drift**.

    결과 초기 상태는 **비공허**다 — dev submodule=detached(dev 지정 전)·consume=drift(pin 아님).
    라이브 LLM 이 dev/sync 를 실제로 실행해야만 3 단언이 성립한다(아무것도 안 하면 fail·false-green
    가드). create_slot 의 submodule init(엔진 un-injected 실 runner)이 file:// origin 을 clone 하도록
    `_FILE_PROTO_ENV` 를 os.environ 에 monkeypatch(엔진 코드 무영향·테스트 우회). `_Fixture` 반환.
    """
    # file:// submodule clone 차단 해제 — 엔진 un-injected 실 runner(create_slot submodule init)도
    # os.environ 을 상속하므로 여기 박으면 clone 이 통과한다(엔진 `-c` 무개입·테스트 우회).
    for key, val in _FILE_PROTO_ENV.items():
        monkeypatch.setenv(key, val)

    home = _import_adopter(tmp_path, harness)
    assert (home / ".project_manager" / "tools" / "worktree_pool.py").exists(), \
        "import 된 홈에 worktree_pool.py 부재 — 엔진 tools 미복사(라이브 backbone 없음)"
    wp = _load_wp_from_home(home)

    # submodule origin — 각 2커밋(pin 을 뒤로 되돌려 detached drift 를 만들 수 있게). c1=구 커밋.
    def _mk_sub_origin(name):
        o = _init_repo(tmp_path / f"{name}-origin")
        (o / "v.txt").write_text("v1\n", encoding="utf-8")
        _git(o, "add", "v.txt"); _git(o, "commit", "-q", "-m", "v1")
        c1 = _git(o, "rev-parse", "HEAD").stdout.strip()
        (o / "v.txt").write_text("v2\n", encoding="utf-8")
        _git(o, "add", "v.txt"); _git(o, "commit", "-q", "-m", "v2")
        return o, c1

    consume_origin, consume_c1 = _mk_sub_origin("consume")
    dev_origin, _dev_c1 = _mk_sub_origin("dev")

    # family origin: main + develop(두 submodule add·pin=submodule HEAD=v2). origin HEAD=main.
    family = _init_repo(tmp_path / "family-origin")
    _git(family, "checkout", "-q", "-b", _BASE_BRANCH)
    _git(family, "submodule", "add", str(consume_origin), _CONSUME_SUB)
    _git(family, "submodule", "add", str(dev_origin), _DEV_SUB)
    _git(family, "commit", "-q", "-m", "add submodules on develop")
    consume_pin = _git(family / _CONSUME_SUB, "rev-parse", "HEAD").stdout.strip()  # =v2(슬롯 pin).
    _git(family, "checkout", "-q", "main")

    # bare(worktree base·ADR-0011 §31) — clone --bare + refspec 보정(T-0152 동형) + fetch(→ origin/develop)
    # + autoSetupMerge=true(--no-track 없으면 upstream 걸릴 조건·assertion 3 을 비공허하게).
    bare = wp.bare_repo_path(_REPO_NAME)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(family), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    _git(bare, "config", "branch.autoSetupMerge", "true")

    # create_slot(base=develop) — 슬롯 브랜치 <repo>_1 을 origin/develop 에서 --no-track 로 파고
    # submodule init(둘 다 detached pin). submodule init 은 file:// origin 을 clone(proto env 필요).
    lease = wp.create_slot(_REPO_NAME, base=_BASE_BRANCH, session="setup", init_submodules=True)
    assert lease.slot == _SLOT, f"슬롯 식별자={lease.slot!r}(기대 {_SLOT!r})"
    slot_dir = wp.slot_path(_SLOT)
    assert slot_dir.is_dir(), "슬롯 worktree 폴더 미생성"

    # consume 을 pin(v2)과 어긋난 detached(c1=v1)로 → drift(sync 재동기 대상).
    _git(slot_dir / _CONSUME_SUB, "checkout", "-q", consume_c1)
    return _Fixture(home=home, slot_dir=slot_dir, consume_pin=consume_pin, consume_c1=consume_c1)


# ── 실 git 상태 조회 (단언 primitive) ──────────────────────────────────────────
def _sub_branch(slot_dir: Path, sub: str) -> str | None:
    """submodule 의 live 브랜치명 — on-branch 면 이름·detached/조회불가면 None."""
    r = subprocess.run(
        [_GIT, "-C", str(slot_dir / sub), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _sub_head(slot_dir: Path, sub: str) -> str | None:
    """submodule 워킹트리의 현재 커밋 sha — 조회불가면 None."""
    r = subprocess.run(
        [_GIT, "-C", str(slot_dir / sub), "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _slot_branch_has_upstream(slot_dir: Path) -> bool:
    """슬롯 브랜치 `<repo>_1@{upstream}` 가 해소되는가 — 해소=tracking 걸림(--no-track 위반)."""
    r = subprocess.run(
        [_GIT, "-C", str(slot_dir), "rev-parse", "--abbrev-ref", f"{_REPO_NAME}_1@{{upstream}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


def _assert_initial_state(fx: _Fixture) -> None:
    """라이브 실행 *전* 초기 상태가 비공허인지 단언 — setup 오류를 명시 fail(가짜 skip/pass 차단).

    (1) dev submodule=detached(dev 지정 전) — dev 가 실행돼야만 on-branch 가 된다.
    (2) consume=pin(v2)과 어긋난 c1(drift)·c1≠pin — sync 가 실행돼야만 pin 으로 돌아온다.
    (3) 슬롯 브랜치=--no-track 로 생성 → upstream 미설정.
    이 셋이 초기에 성립해야 뒤 3 단언이 *LLM 이 실제로 뭔가 했을 때만* 통과한다(비공허).
    """
    assert _sub_branch(fx.slot_dir, _DEV_SUB) is None, \
        "전제 위반 — dev submodule 이 초기에 이미 on-branch(비공허성 상실)"
    assert _sub_head(fx.slot_dir, _CONSUME_SUB) == fx.consume_c1, \
        "전제 위반 — consume submodule 이 초기에 drift(c1) 상태가 아님"
    assert fx.consume_c1 != fx.consume_pin, "전제 위반 — c1==pin(drift 아님·재동기 비공허성 상실)"
    assert not _slot_branch_has_upstream(fx.slot_dir), \
        "전제 위반 — 슬롯 브랜치에 이미 upstream(setup --no-track 실패)"


def _proc_tail(proc: subprocess.CompletedProcess, harness: str) -> str:
    return (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1200:]}"
    )


def _assert_worktree_git_state(fx: _Fixture, proc: subprocess.CompletedProcess, harness: str) -> None:
    """라이브 실행 *후* 실 git 상태로 3 단언 + false-green 가드 (side-effect 기반·phrasing 무관).

    **false-green 가드(선행)**: LLM 이 dev/sync 를 실제로 실행한 git 부작용이 있는가 — 초기 상태
    그대로(dev=detached·consume=c1)면 명령 미실행 → 명시 fail(silent pass 금지·ADR-0050). 초기가
    비공허(`_assert_initial_state`)라, 아래 3 단언 자체가 이미 "아무것도 안 하면 fail" 이지만, 관측을
    분리해 *왜* red 인지(미실행 vs 실행했으나 오작동)를 가른다.
    """
    tail = _proc_tail(proc, harness)
    dev_after = _sub_branch(fx.slot_dir, _DEV_SUB)
    consume_after = _sub_head(fx.slot_dir, _CONSUME_SUB)

    executed = (dev_after == _DEV_BRANCH) or (consume_after != fx.consume_c1)
    assert executed, (
        f"실 {harness} 가 pm-worktree 스킬 명령을 실행한 git 부작용이 없음 — dev/sync 미실행·초기 "
        f"상태 그대로(false-green 가드·명령 미실행 fail).\n" + tail
    )
    # (1) dev submodule no-clobber — dev 지정 브랜치가 sync 후에도 보존(detached pin 으로 안 낚아채임).
    assert dev_after == _DEV_BRANCH, (
        f"dev submodule {_DEV_SUB} 이 dev 브랜치 {_DEV_BRANCH!r} 로 보존되지 않음(clobber·현재 "
        f"branch={dev_after!r}·ADR-0051 크럭스 A 회귀).\n" + tail
    )
    # (2) consume submodule drift 재동기 — detached consume 이 superproject pin 으로 돌아옴.
    assert consume_after == fx.consume_pin, (
        f"consume submodule {_CONSUME_SUB} 이 pin({fx.consume_pin[:8]})으로 재동기 안 됨(현재 "
        f"HEAD={consume_after}·sync 미작동).\n" + tail
    )
    # (3) 슬롯 브랜치 tracking 없음 — origin/<base> upstream 미설정(--no-track·T-0274 mock 맹점 백스톱).
    assert not _slot_branch_has_upstream(fx.slot_dir), (
        f"슬롯 브랜치 {_REPO_NAME}_1 에 upstream 자동설정됨(--no-track 위반·autoSetupMerge 낚아채임).\n"
        + tail
    )


def _pm_worktree_prompt(skill_text: str) -> str:
    """실 LLM 에 스킬만 주고 운영중-관리 시나리오를 시키는 프롬프트 (스킬 = 유일 컨텍스트).

    진입문서(CLAUDE.md/AGENTS.md) 경로를 *주지 않는다* — 스킬만으로 커맨드/플래그를 골라야 통과
    (= 스킬 사용성·ADR-0050 "실 LLM 이 스킬 읽고 옳게 하나"). 시나리오 파라미터(슬롯·submodule
    경로·dev 브랜치)는 자연어로 주되 *커맨드 구문*(worktree_pool.py dev/sync·--slot)은 스킬에서
    옮겨야 한다. 두 단계: (1) dev submodule 을 dev 브랜치로 지정("작업 중" 선언·pool 보호) (2) 슬롯
    submodule 을 pin 에 재동기. command_card_usability 의 --help 금지 문구를 미러(사용성 판정).
    """
    return (
        "You are the PM operating this project's worktree pool. Below (between <<<SKILL and "
        "SKILL>>>) is the pm-worktree skill — use ONLY it to decide the exact command and flags "
        "for each step. Do NOT run any command with --help or -h, and do NOT open other "
        "documentation.\n\n"
        f"Context: the slot is {_SLOT}. It contains two git submodules — {_DEV_SUB} (which you are "
        f"about to edit yourself) and {_CONSUME_SUB} (which you are NOT editing).\n\n"
        "Do exactly these two steps from the shared root (the directory that contains "
        ".project_manager), one skill command per step, using the EXACT command form the skill "
        "shows:\n"
        f"  1. Declare that you are working in the {_DEV_SUB} submodule by designating it as a dev "
        f"submodule on a branch named {_DEV_BRANCH!r} (so the pool will not clobber your work). "
        f"Pass the slot explicitly as {_SLOT}.\n"
        f"  2. Then resync this slot's submodules to their pins. Pass the slot explicitly as "
        f"{_SLOT}.\n\n"
        "<<<SKILL\n" + skill_text + "\nSKILL>>>\n\n"
        "Run the two skill commands now."
    )


# ── 라이브 테스트 (release tier · 기본 skip · PM_ORCH_LIVE_RELEASE=1 opt-in) ──────────────
@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude") or _GIT is None,
    reason="pm-worktree 라이브 — PM_ORCH_LIVE_RELEASE=1 + claude CLI(API 과금) + git 필요. "
           "기본 skip·사용자 트리거.",
)
def test_pm_worktree_live_claude(tmp_path, monkeypatch):
    """실 claude 가 pm-worktree 스킬만 보고 dev/sync 시나리오를 운영·실 git 3 단언 (hard).

    스킬(SKILL.md)만 컨텍스트로 주고 [dev 지정 → 재동기]를 시킨다 — 진입문서 경로 미제공. claude 는
    subprocess cwd 를 존중(`--dir` 불요)·stream-json 으로 backbone(worktree_pool.py dev·sync) 실행을
    **hard** 관측(false-green 백스톱)하고, 실 git 상태(dev no-clobber·consume 재동기·슬롯 no-track)를
    hard 단언한다. 초기 비공허(`_assert_initial_state`)라 미실행 시 red. API 과금(dev+sync 2콜).
    """
    fx = _setup_home_with_submodule_slot(tmp_path, "claude", monkeypatch)
    _assert_initial_state(fx)

    prompt = _pm_worktree_prompt(_CLAUDE_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions", prompt],
        cwd=str(fx.home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        # _live_env(화이트리스트·모델 누수 차단) + file:// proto 우회(픽스처 submodule origin 이
        # 로컬 file:// 라·엔진 무관 테스트 우회). dev/sync 는 로컬 git 이라 보통 proto 불요지만 안전망.
        env={**_live_env(CLAUDE_MODEL), **_FILE_PROTO_ENV}, timeout=_CLAUDE_TIMEOUT,
    )

    # 커맨드 관측(hard) — stream-json Bash tool-call 에서 backbone dev·sync 실행 둘 다 등장해야 통과.
    commands = _collect_bash_commands(proc.stdout)
    ran_dev = any("worktree_pool.py dev" in c for c in commands)
    ran_sync = any("worktree_pool.py sync" in c for c in commands)
    assert ran_dev and ran_sync, (
        f"claude 가 pm-worktree backbone 커맨드(dev·sync)를 실행하지 않음 "
        f"(dev={ran_dev}·sync={ran_sync}) — 관측 커맨드={commands}\n" + _proc_tail(proc, "claude")
    )
    # side-effect(hard) — 실 git 상태 3 단언 + false-green 가드.
    _assert_worktree_git_state(fx, proc, "claude")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode") or _GIT is None,
    reason="pm-worktree 라이브 — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) + git 필요. "
           "기본 skip·사용자 트리거.",
)
def test_pm_worktree_live_opencode_best_effort(tmp_path, monkeypatch):
    """실 opencode(glm-5.2:cloud)가 스킬만 보고 dev/sync 시나리오를 운영 (best-effort — claude 대비 비대칭).

    claude 와 같은 스킬-only 프롬프트지만 **판정 강도가 낮다**(테스트명 `_best_effort`가 표기).
    opencode 는 stream-json 처럼 tool-call 을 정밀 노출하지 않아(stdout 스캔뿐) backbone 실행을
    hard-claim 못 한다(release_wave 위임-관측 비대칭 상속). 그래서 hard 상한 = **실 git 상태 3 단언**
    (side-effect·초기 비공허라 미실행 시 red)이고, 커맨드 실행 관측은 stdout 에코가 있을 때만
    best-effort 다. claude 경로(`test_pm_worktree_live_claude`)가 커맨드를 hard 로 커버하는 짝이다.
    `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판)·`--dangerously-skip-permissions`(비대화 헤드리스
    auto-reject 회피·throwaway tmp 홈 격리·release_wave 동일 근거). API 과금 0(ollama).
    """
    fx = _setup_home_with_submodule_slot(tmp_path, "opencode", monkeypatch)
    _assert_initial_state(fx)

    prompt = _pm_worktree_prompt(_OPENCODE_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(fx.home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL, prompt],
        cwd=str(fx.home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**_live_env(LIVE_MODEL), **_FILE_PROTO_ENV}, timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — 실 git 상태 3 단언 + false-green 가드(초기 비공허·미실행 fail).
    _assert_worktree_git_state(fx, proc, "opencode")

    # 커맨드 실행 관측은 best-effort·gate 아님 — opencode 는 backbone 커맨드를 정밀 노출하지 않아
    # side-effect(위 _assert_worktree_git_state)로만 판정한다(stdout 에코는 단언하지 않는다).


# ── hermetic 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과) ──────────────
# 위 라이브 2케이스는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 가드는 라이브
# 없이도 돌아 (1) 스킬 파일 존재·backbone 참조 (2) 프롬프트 구조 (3) fixture 가 비공허 초기 상태를
# 만드는지 (4) backbone 이 시나리오를 수준에서 성립시키는지 (5) release 마커 수를 가드한다 — setup
# rot·마커 소실 시 라이브가 가짜 skip/pass 로 숨지 않고 여기서 red 로 잡힌다(false-green 백스톱).


def test_pm_worktree_skill_files_exist_and_reference_backbone():
    """검증 대상 스킬 2종(claude SKILL.md·opencode 스킬 미러)이 존재하고 backbone(dev/sync)을 참조한다.

    라이브 프롬프트가 임베드하는 스킬이 소실/개명되면 라이브가 read 에서 죽거나 가짜가 되므로,
    always-run 가드로 존재 + backbone 커맨드 형태(worktree_pool.py·dev·sync·--slot)를 고정한다.
    """
    for skill in (_CLAUDE_SKILL, _OPENCODE_SKILL):
        assert skill.exists(), f"pm-worktree 스킬 부재: {skill}"
        text = skill.read_text(encoding="utf-8")
        assert "worktree_pool.py" in text, f"{skill.name} 이 backbone worktree_pool.py 를 참조 안 함"
        assert "dev" in text and "sync" in text, f"{skill.name} 에 dev/sync 서브커맨드 부재"
        assert "--slot" in text, f"{skill.name} 에 --slot(정체성 명시 전달) 부재"


def test_pm_worktree_prompt_embeds_skill_and_scenario():
    """프롬프트가 스킬 전문 + 시나리오 파라미터(슬롯·submodule·dev 브랜치·2단계)를 담고 --help 를 금한다."""
    skill_text = _CLAUDE_SKILL.read_text(encoding="utf-8")
    prompt = _pm_worktree_prompt(skill_text)
    # 스킬이 유일 컨텍스트로 임베드된다(진입문서 경로 미제공 — 스킬 사용성).
    assert skill_text in prompt
    assert "CLAUDE.md" not in prompt and "AGENTS.md" not in prompt
    # 시나리오 파라미터 — 슬롯·두 submodule 경로·dev 브랜치.
    assert _SLOT in prompt
    assert _DEV_SUB in prompt and _CONSUME_SUB in prompt
    assert _DEV_BRANCH in prompt
    # --help 사용 금지 명시(사용성 판정 대상·command_card_usability 미러).
    assert "Do NOT run any command with --help or -h" in prompt


@_git_required
def test_setup_creates_nonvacuous_initial_state(tmp_path, monkeypatch):
    """setup 백스톱 — 라이브 fixture 가 비공허 초기 상태(dev detached·consume drift·슬롯 no-track)를 만든다.

    라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라, 이 always-run 가드가 setup 자체를 실
    git 으로 exercise 한다 — setup 이 rot 하면 라이브가 가짜 skip/pass 로 숨지 않고 여기서 잡힌다.
    초기 상태가 *비공허*(LLM 이 아무것도 안 하면 뒤 3 단언이 fail)임 + submodule init 실체를 고정.
    """
    fx = _setup_home_with_submodule_slot(tmp_path, "claude", monkeypatch)
    _assert_initial_state(fx)
    # create_slot 이 submodule 을 실제로 init 했다(작업트리 채워짐·file:// clone 성공).
    assert (fx.slot_dir / _DEV_SUB / "v.txt").exists(), "dev submodule 작업트리 미init"
    assert (fx.slot_dir / _CONSUME_SUB / "v.txt").exists(), "consume submodule 작업트리 미init"


@_git_required
def test_scenario_backbone_produces_expected_final_state(tmp_path, monkeypatch):
    """구조 백스톱 — LLM 없이 backbone(dev+sync)으로 시나리오를 돌리면 3 단언이 성립한다.

    라이브의 시나리오/단언이 *backbone 수준에서 옳은지*(스킬 프롬프트/LLM 사용성과 무관하게)를 실
    git 으로 고정한다 — 라이브가 fail 하면 스킬(LLM) 문제이지 시나리오/단언 문제가 아님을 가른다
    (ADR-0050 "구조적으로 옳은지 기계 확인"·release 마커 밖 always-run). `_assert_worktree_git_state`
    의 side-effect 기대(3 단언)를 backbone 출력으로 재현.
    """
    fx = _setup_home_with_submodule_slot(tmp_path, "claude", monkeypatch)
    _assert_initial_state(fx)
    wp = _load_wp_from_home(fx.home)

    # 실 LLM 대신 backbone 직접 호출 — dev(libs/dev 를 on-branch 화) → sync(selective 재동기).
    rc, out = wp.dev(_SLOT, _DEV_SUB, _DEV_BRANCH)
    assert rc == 0, f"backbone dev 실패(rc={rc}): {out!r}"
    wp.sync(_SLOT)

    # 3 단언 — dev no-clobber·consume 재동기·슬롯 no-track (side-effect 부분·_assert_worktree_git_state 동형).
    assert _sub_branch(fx.slot_dir, _DEV_SUB) == _DEV_BRANCH, "dev submodule 이 on-branch 로 보존 안 됨"
    assert _sub_head(fx.slot_dir, _CONSUME_SUB) == fx.consume_pin, "consume 이 pin 으로 재동기 안 됨"
    assert not _slot_branch_has_upstream(fx.slot_dir), "슬롯 브랜치에 upstream 자동설정됨(--no-track 위반)"


def test_release_markers_pinned():
    """이 파일 release 마커 수(claude+opencode)를 pin — 마커 소실/개명 시 게이트 selection 누락 방어.

    `pytest -m release` 는 마커로 라이브 서브셋을 고른다. 데코레이터 삭제/개명으로 마커가 빠지면 그
    테스트는 조용히 선택에서 빠지고 게이트가 그 부재를 못 본다(pytest strict-marker 는 *오타* 만 잡음).
    이 파일 스코프로 release 마커 함수 수를 고정한다(test_release_wave `test_release_marker_count_is_
    pinned` 동형·라이브를 의도적으로 늘리면 `_EXPECTED_RELEASE_TESTS` 를 함께 갱신). 이 파일은
    test_release_wave 의 `_RELEASE_TEST_FILES` 에도 등록돼(11 합산 pin·T-0278) cross-file 로 커버되고,
    여기 자체 pin(=2)으로 이중 방어한다(그 리스트에서 누락돼도 이 파일 마커 소실을 잡는다).
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    count = 0
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if (isinstance(target, ast.Attribute) and target.attr == "release"
                    and isinstance(target.value, ast.Attribute) and target.value.attr == "mark"
                    and isinstance(target.value.value, ast.Name) and target.value.value.id == "pytest"):
                count += 1
    assert count == _EXPECTED_RELEASE_TESTS, (
        f"이 파일 release 마커 수={count} != 기대 {_EXPECTED_RELEASE_TESTS}(claude+opencode) — 마커 "
        f"소실/개명 의심(게이트 selection 누락 위험). 라이브를 의도적으로 늘렸다면 기대값을 함께 갱신."
    )
