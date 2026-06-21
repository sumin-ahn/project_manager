#!/usr/bin/env python3
"""single-user multi-repo (N×M) 셋업/관리 파사드 — 가벼운 디스패처 (ADR-0011·ADR-0014·ADR-0016).

multi-PM = N 세션 × M repo 한 개념(ADR-0016) — *수가 1이냐 더냐*. 이 파사드는 single
user 가 여러 repo(multi-PM 셋업)를 도는 토폴로지의 *셋업·조회·진단* 전용이다(다중-사람
협업 아님). 런타임 worktree alloc/release 자동화(bootstrap/handoff)는 여기 비관여 —
이 도구는 사람이 손으로 하는 셋업·관리·진단만 한다(sealed spike §8-5·§3e).

각 서브커맨드는 엔진(board.py / worktree_pool.py / pm_update.py)을 호출하는 *얇은
배선*이다 — 자체 로직 0. 디스패처가 엔진 호출의 단일 지점이다.

사용:
    pm-config init [<board init 인자>]                     # clone 당 1회 셋업 (board.py init 흡수·T-0065)
    pm-config repo add <name> --git <url> [--test "<cmd>"] # 패밀리에 repo 등록 + .repos clone (--test optional)
    pm-config worktree add <repo>                          # 새 슬롯 생성 + submodule init
    pm-config status | whoami                              # 풀/리스 + 이 세션 repo/슬롯/branch
    pm-config release <slot> [--force]                     # 작업완료 반납 / 수동 강제(백스톱)
    pm-config update [--from <upstream>]                   # 엔진 갱신 (pm-update 흡수·T-0054)

서브커맨드별 엔진 배선:
  - init      → board.main(["init", ...]) verbatim forward (clone 당 1회 셋업·N=1·M=1[solo] ~ N×M 공용).
  - repo add  → board.areas_append(per-repo 레지스트리 줄·ADR-0014) + `git clone --bare`
                로 `.repos/<name>.git`(worktree 풀 공유 .git 원·ADR-0011).
  - worktree add → worktree_pool.create_slot(새 슬롯 + `git submodule update --init`).
  - status|whoami → worktree_pool.list_leases() + 이 세션 식별(repo/슬롯/branch surface).
  - release → worktree_pool.release(--force 면 force_release) — 수동 반납/강제만.
  - update → pm_update.main(argv) verbatim forward (rename 비용 0·중복 구현 금지).

결정 (ADR-0011·ADR-0014·spike §8-5):
  - thin forwarder(`pm-config.sh/.cmd`)는 로직 0 — 이 디스패처가 엔진 배선의 단일 지점.
  - 브랜치 할당은 파사드 아님 — `pm-bootstrap <repo> --branch`(T-0060) 소관(명령표 외).
  - update 는 pm_update 로직을 *위임*(import 호출) — pm_update.py 는 그대로 두고 흡수만.
  - 엔진 호출은 DI seam(주입 가능 callable) — 테스트는 mock 주입으로 hermetic(실 clone/
    worktree 부작용 없이 배선만 검증·pm_bootstrap 의 DI 패턴 동류).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py 와 동일 앵커 규약
# (ADR-0011 — 어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / ".project_manager" / "tools"
REPOS_DIR = REPO / ".repos"   # worktree 풀이 공유하는 bare .git 원 (ADR-0011)

GIT_TIMEOUT_SECONDS = 600   # clone 은 네트워크 — 부트스트랩/worktree git 보다 넉넉히.

# git argv → (returncode, stdout). DI seam 의 타입 (pm_import.GitRunner·worktree_pool 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


# ── 엔진 모듈 동적 로드 (스크립트-위치 앵커·pm_bootstrap 선례) ──────────────────
# board.py·worktree_pool.py 는 같은 tools/ 에 있다. spec_from_file_location 으로
# 로드한다 — 패키지 설치 없이 동작(board.py·pm_*.py 와 같은 로드 규약). 부재/로드
# 실패는 해당 서브커맨드 경로에서만 명시 에러(침묵 무력화 금지·ADR-0013).


def _load_module(name: str, filename: str):
    """tools/<filename> 를 모듈로 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    path = TOOLS_DIR / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 호출부가 명시 에러로 보고.
        return None


def _real_clone_runner() -> GitRunner:
    """`git clone ...` 을 실행하는 GitRunner (fail-soft). argv 는 clone 인자 그대로.

    clone 은 `-C <dir>` 가 아니라 `git clone <url> <dest>` 형태라 별도 runner 로 둔다.
    git 바이너리 부재(shutil.which)·예외는 (1, stderr-or-"") 로 감싼다 — 호출부가 rc 로
    판정. 인코딩은 엔진 규약대로 UTF-8(한글 경로·메시지 안전).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부에 위임.
            return 1, str(exc)

    return runner


def _local_conf_session() -> str | None:
    """`.project_manager/local.conf` 의 `session=` (없거나 OSError → None).

    board.py 를 import 하지 않으므로(ADR-0013 isolation·touches 격리) `board.local_config()
    .get("session")` 와 *동일 의미*를 stdlib 로 자체 구현한다 — plain `KEY=value`·`#`
    주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "session":
            return val.strip() or None
    return None


def _default_session() -> str:
    """세션 식별자 — board.py `session_name()` 과 *동형* 우선순위 (T-0073):
    `$PM_SESSION_NAME` > `$CLAUDE_SESSION_NAME`(deprecated alias·silent) >
    `local.conf session=` > `<host>-<pid>` (board/worktree_pool 동형).

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관). `CLAUDE_SESSION_NAME` 은 구 이름
    alias 만 — 둘 다 설정 시 `PM_SESSION_NAME` 승. alias 는 경고 없이 조용히 동작.

    local.conf `session=` 레이어가 빠지면 `cmd_status`/`whoami` 의 "이 세션의 리스" surface 와
    worktree add 의 lease.session 저장이 board 매칭측과 어긋난다(T-0066 must-fix) — 통일한다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    conf_sess = _local_conf_session()
    if conf_sess:
        return conf_sess
    import socket
    return f"{socket.gethostname()}-{os.getpid()}"


def _local_conf_test_cmd() -> str | None:
    """`.project_manager/local.conf` 의 `test_cmd=` (없거나 OSError → None).

    `_local_conf_session` 과 동형 — board.py 를 import 하지 않으므로(ADR-0013 isolation·
    touches 격리) `board.local_config().get("test_cmd")` 와 *동일 의미*를 stdlib 로 자체
    구현한다. worktree add 빌드명령 프롬프트의 기본값(`board._test_cmd` 솔로 폴백 레이어와
    동형 — 미지정 시 `pytest -q`)을 제시하는 데 쓴다.
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "test_cmd":
            return val.strip() or None
    return None


def _default_test_cmd() -> str:
    """worktree add 빌드명령 프롬프트의 솔로 폴백값 — `local.conf test_cmd` 또는 `pytest -q`.

    board._test_cmd 의 솔로 폴백 레이어(`local_config().get("test_cmd") or "pytest -q"`)와
    동형. `_resolve_repo_test_cmd` 의 마지막 레이어(areas 미등록·빈 값일 때)다 —
    프롬프트 표시값 resolve 의 폴백.
    """
    return _local_conf_test_cmd() or "pytest -q"


def _resolve_repo_test_cmd(repo: str, *, board=None) -> str:
    """프롬프트 `[기본: <X>]` 의 X — 그 repo 가 Enter 시 실제로 폴백할 test_cmd.

    board._test_cmd 의 해소 체인을 *그 repo 한정으로* 재현한다 (board 직접 import 금지·
    `_load_module` DI + areas 파서 `_parse_areas`/`_areas_row_for_prefix` 재사용):
      1. **활성 repo 의 areas.md test_cmd** — 그 repo(=prefix)의 레지스트리 행에 비어
         있지 않은 `test_cmd` 가 있으면 그것(per-repo 스택·`go test ./...` 등).
      2. **솔로 폴백** — areas 미등록·빈 값이면 `local.conf test_cmd` 또는 `pytest -q`.
    (활성 슬롯 레이어는 새 슬롯 생성 *전* 시점이라 표시에 무의미 — 생략.) board 부재/파서
    부재면 솔로 폴백만. 빈입력(Enter) 시 슬롯에 안 박고(None) 이 체인으로 폴백함을 투명하게
    보여주는 게 목적이다(must-fix 1 — 슬롯이 areas 보다 우선이라 잘못 덮으면 안 됨).
    """
    board_mod = board or _load_module("board", "board.py")
    row_for_prefix = getattr(board_mod, "_areas_row_for_prefix", None) if board_mod else None
    if row_for_prefix is not None:
        try:
            row = row_for_prefix(repo)
        except Exception:  # noqa: BLE001 — areas 파싱 실패는 솔로 폴백으로 강등(크래시 0).
            row = None
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    return _default_test_cmd()


def _resolve_repo_base(repo: str, *, board=None) -> str | None:
    """그 repo 의 areas.md `base` 브랜치 (T-0075). 미지정/미등록/구 스키마/board 부재 → None.

    `cmd_worktree_add` 가 `create_slot(base=)` 로 전달할 값을 resolve 한다 — areas.md 의 그
    repo base(`pm-config repo add --base`/clone-time bare HEAD 가 기록)를 읽어 슬롯 브랜치
    `<repo>_<N>` 가 그 base 에서 파생되게 한다. None 폴백이면 create_slot 이 현행 bare HEAD
    동작(회귀 0).

    board 직접 import 금지(ADR-0013 isolation·touches 격리) — `_resolve_repo_test_cmd` 와
    동형으로 `_load_module` DI + board 의 `_repo_base` 헬퍼를 getattr 로 쓴다. board/헬퍼
    부재(구 board)·파싱 실패는 None 으로 강등(크래시 0·현행 동작 폴백).
    """
    board_mod = board or _load_module("board", "board.py")
    repo_base = getattr(board_mod, "_repo_base", None) if board_mod else None
    if repo_base is None:
        return None
    try:
        return repo_base(repo)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 None 폴백(현행 bare HEAD 동작).
        return None


# 보호 브랜치 default (T-0076) — board.DEFAULT_PROTECTED 와 *동형* 폴백. board/헬퍼 부재(구
# board)·파싱 실패 시 board 를 못 읽으므로 여기서 같은 안전 기본값을 보장한다(보호는 안전
# 기본값이 있어야 — 미해소여도 main 류를 막는다). board 가 있으면 board._repo_protected 가
# 권위(areas override 반영)이고, 이 상수는 board 부재 폴백 전용이다.
_DEFAULT_PROTECTED = ("main", "master", "develop")


def _resolve_repo_protected(repo: str, *, board=None) -> list[str]:
    """그 repo 의 보호 브랜치 목록 (T-0076). board 부재/파싱 실패 → `_DEFAULT_PROTECTED`.

    `cmd_repo_add`(sidecar 채움·훅 설치)·`cmd_worktree_add`(재설치)가 `install_protected_hook`
    에 전달할 목록을 resolve 한다 — areas.md 의 그 repo `protected` 칼럼(`board._repo_protected`)
    을 읽어 미지정/구 스키마면 default(main/master/develop) 폴백. board 가 권위(areas override
    반영)이고, board/헬퍼 부재·파싱 실패만 여기 default 로 강등한다(크래시 0).

    board 직접 import 금지(ADR-0013 isolation·touches 격리) — `_resolve_repo_base` 와 동형으로
    `_load_module` DI + board 의 `_repo_protected` 헬퍼를 getattr 로 쓴다.
    """
    board_mod = board or _load_module("board", "board.py")
    repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
    if repo_protected is None:
        return list(_DEFAULT_PROTECTED)
    try:
        return repo_protected(repo)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 default 폴백(보호 기본값 보장).
        return list(_DEFAULT_PROTECTED)


def _install_protected_hook(repo: str, *, board=None, worktree_pool=None) -> bool:
    """그 repo 의 보호 브랜치 pre-push 훅을 (재)설치한다 — repo add·worktree add 공용 (T-0076).

    보호목록을 `_resolve_repo_protected`(areas `protected`→default)로 해소해
    `worktree_pool.install_protected_hook(repo, protected)` 에 전달한다 — 훅+sidecar+bare
    `core.hooksPath` wiring(멱등·자가치유). **회사 repo 무영향** — 모든 write 는 `.project_manager
    /.local` + bare config 1줄(client-side).

    **fail-soft·best-effort** — worktree_pool 부재/`install_protected_hook` 미존재(구 엔진)/
    예외는 조용히 False(보호훅은 *추가 가드*이지 repo add/worktree add 의 핵심 부작용이 아니다
    → 훅 설치 실패가 등록/슬롯 생성을 깨면 안 된다). bare 부재 시 install 이 no-op False.
    설치 성공 시 True. board 직접 import 금지(ADR-0013) — `_load_module` DI.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    install = getattr(wp, "install_protected_hook", None) if wp else None
    if install is None:
        return False  # 구 엔진(헬퍼 부재)/wp 부재 — fail-soft(보호훅은 추가 가드).
    protected = _resolve_repo_protected(repo, board=board)
    try:
        return bool(install(repo, protected))
    except Exception:  # noqa: BLE001 — 훅 설치 실패가 등록/슬롯 생성을 깨면 안 됨(best-effort).
        return False


def _stdin_is_tty() -> bool:
    """무인자 분기·프롬프트 게이트용 tty 판정 — stdin·stdout 둘 다 tty 일 때만 True.

    파이프/CI(둘 중 하나라도 비-tty)면 False → 콘솔/프롬프트로 안 멈춘다(input() 블록
    회피·pm_import 비-tty 폴백 패턴 동류). 테스트는 이 헬퍼를 monkeypatch 해 분기를
    결정적으로 친다(라이브 tty 없이).
    """
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


# ── repo name 입력 검증 (T-0078) ─────────────────────────────────────────────

# 허용 repo name = prefix = ticket ID 네임스페이스(ADR-0011)·areas.md 공백구분 칼럼값.
# 영숫자로 시작(leading `-` = 옵션 오인·빈 문자열 배제), 이후 영숫자/`_`/`-` 만 허용.
# 경로분리자(`/`)·`.`(`..` 폴더탈출)·공백(areas.md 줄 corruption)을 전부 배제한다.
# board._FAMILY_SCOPE_RE(`^[A-Za-z0-9_-]+$`)는 leading `-` 를 막지 않아 재사용 부적합 —
# repo name 은 영숫자 시작을 강제하므로 별도 패턴을 둔다(reuse 확인·중복 정의 회피).
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_repo_name(name: str) -> bool:
    """repo name 이 허용 패턴(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)에 맞는가 (T-0078).

    `cmd_repo_add` 가 **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 부른다 —
    `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-` 옵션 오인·빈
    문자열을 입구에서 막는다(fail-closed). 모듈수준 헬퍼라 테스트 가능(주입 불필요).
    """
    # fullmatch — `re.match` 의 `$` 는 trailing 개행 직전에도 매칭해 `"billing\n"` 이
    # 가드를 통과(bare 폴더명 개행·areas.md 줄 corruption — 막으려던 결함 클래스)한다.
    return bool(_REPO_NAME_RE.fullmatch(name))


# ── base 브랜치 해소 (T-0075) ─────────────────────────────────────────────────

# `--base` 검증 실패(없는 브랜치)를 빈문자열("미해소")과 구별하는 sentinel. None 폴백
# (bare HEAD 해소 실패)은 빈 문자열로 surface 하지만, *명시 base 가 검증 실패*하면 등록을
# 막아야 하므로(잘못된 base 기록 방지) 이 sentinel 로 호출부에 신호한다.
_BASE_INVALID = object()


def _resolve_base(base_arg: str | None, bare_path: Path, *, runner: GitRunner):
    """repo add base 브랜치를 해소한다 (T-0075). bare(`.repos/<name>.git`)는 존재 전제.

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_real_clone_runner` 가 `git <argv>` 형태라 `-C` 를 argv 로 넣으면 그 repo 컨텍스트).

      - `base_arg` 미지정(None) → bare HEAD 해소(`git -C <bare> symbolic-ref --short HEAD`
        = 원격 default 브랜치)를 base 로 명시값화한다. 해소 실패(rc≠0/빈 출력)는 빈 문자열
        ("미해소"·worktree add 가 bare HEAD 폴백·현행 동작) — repo 등록 자체는 막지 않는다.
      - `base_arg` 지정 → **로컬 브랜치** 검증(`git -C <bare> show-ref --verify --quiet
        refs/heads/<b>` rc==0). `show-ref --verify` 는 exact-ref primitive(revision 문법
        미적용)라 태그·SHA·`HEAD`·원격 ref 는 물론 `main~0`·`main^{}` 같은 revision 표현도
        통과하지 못한다(T-0078 — worktree 슬롯 파생[T-0075]은 로컬 브랜치 base 가 전제). 통과면
        반환값은 **기존대로 bare 브랜치명(`base_arg`)**(areas.md base 칼럼 계약 불변), 실패면
        `_BASE_INVALID`(호출부가 명확한 에러 rc 1 로 surface·등록 차단).

    반환: 해소된 base 문자열(빈 문자열 = 미해소·None 동등) 또는 `_BASE_INVALID`(검증 실패).
    """
    if base_arg is None:
        rc, out = runner(["-C", str(bare_path), "symbolic-ref", "--short", "HEAD"])
        if rc != 0:
            return ""  # bare HEAD 해소 실패 → 미해소(worktree add 가 bare HEAD 폴백·현행).
        return out.strip()
    # refs/heads/<b> exact-ref 검증 — 태그·SHA·HEAD·원격 ref·revision 문법(main~0·main^{}) 거부,
    # 로컬 브랜치만 통과(T-0078). show-ref --verify 는 revision 문법을 적용 안 하는 exact-ref primitive.
    rc, _out = runner(
        ["-C", str(bare_path), "show-ref", "--verify", "--quiet", f"refs/heads/{base_arg}"]
    )
    if rc != 0:
        return _BASE_INVALID  # 비-로컬-브랜치(태그/SHA/HEAD/부재) → 등록 차단(잘못된 base 기록 방지).
    return base_arg


# ── 서브커맨드 핸들러 ─────────────────────────────────────────────────────────


def cmd_repo_add(
    args: argparse.Namespace,
    *,
    board=None,
    clone_runner: GitRunner | None = None,
    repos_dir: Path | None = None,
    worktree_pool=None,
) -> int:
    """`repo add <name> --git <url> [--test "<cmd>"] [--base <branch>]` — 패밀리에 repo 등록 (ADR-0014·T-0075).

    1. areas.md 레지스트리에 per-repo 줄을 기록한다(board.areas_append — repo/prefix/
       git/test_cmd/owner/base 칼럼·ADR-0014·T-0075). prefix 는 repo 이름과 동일(per-repo ID
       네임스페이스·ADR-0011). owner = 등록 식별자(registrant·협업 소유자 아님·ADR-0016) —
       기본 현 세션.
    2. `.repos/<name>.git` 로 bare clone 한다 — worktree 풀이 공유하는 .git 원(ADR-0011·
       worktree add 가 이 bare 를 base 로 슬롯을 만든다).

    **base 브랜치 해소 (T-0075)** — clone 후, areas 등록 *전*(등록 줄에 base 를 박아야 하므로):
      - `--base` 미지정 → bare HEAD(`git -C .repos/<name>.git symbolic-ref --short HEAD`
        = 원격 default 브랜치)를 해소해 명시값화·기록. worktree add 가 슬롯 브랜치를 그
        base 에서 파생한다(T-0075). HEAD 해소 실패 시 base 빈 값(현행 bare HEAD 동작 폴백).
      - `--base <b>` 지정 → 로컬 브랜치 검증(`git -C .repos/<name>.git show-ref --verify --quiet refs/heads/<b>` rc==0).
        없으면 명확한 에러 rc 1(clone 은 됐으나 areas 등록은 막아 잘못된 base 기록 방지).

    **멱등·재시도 가능** (두 부작용 — bare clone + areas 등록 — 이 부분 성공할 수 있음):
      - 이미 등록 + bare 존재     → 완전 no-op rc0 (둘 다 이미 됨·친절 메시지).
      - 이미 등록 + bare 부재     → clone *진행*(재시도) 후 등록 건너뜀(append-only).
                                    첫 실행이 등록만 남기고 clone 실패한 경우의 복구 경로다.
      - 미등록                    → clone → base 해소·검증 → areas_append.
    base 해소가 clone 에 의존하므로(bare HEAD 해소/존재 검증) **clone 을 먼저** 한다 —
    미등록 경로는 clone→base 해소→areas_append. 이미 등록 경로는 base 가 이미 박혀 있어
    재해소하지 않는다(append-only·중복 등록 금지). T-0075 이전엔 areas 등록이 clone 앞이라
    clone 실패해도 등록이 남았지만, 이제 clone 성공 후에만 등록한다(재실행이 둘 다 다시 함).

    board / clone_runner / repos_dir 주입으로 hermetic 테스트(실 등록·clone 없이 배선 검증).
    base 해소 git 호출(symbolic-ref/show-ref)은 같은 `clone_runner` 를 `-C <bare>` 로 재사용한다.
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — areas.md 등록 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    # repos_dir 미주입 시 모듈 전역(REPOS_DIR)을 *호출 시점*에 해소한다 — 함수 default 로
    # 굳히면 테스트의 monkeypatch(REPOS_DIR)가 안 먹는다.
    repos_dir = repos_dir if repos_dir is not None else REPOS_DIR

    name = args.name

    # name 가드 (T-0078) — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 검증한다.
    # `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-`·빈 문자열을 입구에서
    # 막는다(fail-closed·부분 부작용 0). CLI·콘솔 두 경로가 결국 이 sink 로 수렴하므로 여기 한 곳.
    if not _validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). 경로분리자(`/`)·`.`·공백·leading `-`·빈 "
            "이름은 금지(폴더탈출·areas.md 줄 corruption 방지). clone/등록/훅 전혀 하지 않았다.",
            file=sys.stderr,
        )
        return 1

    owner = args.owner or _default_session()
    base_arg = getattr(args, "base", None)
    bare_path = repos_dir / f"{name}.git"
    already_registered = name in board_mod.registered_prefixes()
    bare_exists = bare_path.exists()
    runner = clone_runner or _real_clone_runner()

    # 두 부작용(bare clone + areas 등록)을 멱등화한다 — base 해소(T-0075)가 bare 에 의존
    # 하므로(bare HEAD/존재 검증) **clone 을 먼저**, 그 다음 base 해소·areas 등록 순서다.

    # 1) bare clone → .repos/<name>.git (worktree 풀 공유 .git 원·ADR-0011). bare 가 이미
    #    있으면 건너뛴다(재사용·중복 clone/클로버 방지). bare 부재면 clone(재)시도.
    if bare_exists:
        print(f"✓ .repos/{name}.git 이미 존재 — clone 건너뜀 (재사용).")
    else:
        repos_dir.mkdir(parents=True, exist_ok=True)
        rc, out = runner(["clone", "--bare", args.git, str(bare_path)])
        if rc != 0:
            print(
                f"[경고] `git clone --bare {args.git}` 실패 (rc={rc}):\n{out}\n"
                f"  네트워크/URL 확인 후 수동으로 `git clone --bare {args.git} {bare_path}` 하거나 "
                "재시도하라(등록은 clone 성공 후·멱등).",
                file=sys.stderr,
            )
            return 1
        print(f"✓ .repos/{name}.git bare clone 완료.")

    # 2) 이미 등록돼 있으면 base 재해소/등록을 건너뛴다(append-only·중복 등록 금지). base 는
    #    첫 등록 때 박힌 값 그대로(clone 만 실패했던 재시도 경로는 위 clone 으로 복구됨).
    #    보호 훅(T-0076)은 멱등 자가치유라 *재등록 경로에서도* (재)설치한다 — 엔진 update 후
    #    기존 repo 도 다음 repo add/worktree add 에 훅을 얻는다(별도 명령 불요).
    if already_registered:
        print(f"✓ repo {name!r} 이미 areas.md 등록됨 — 등록 건너뜀.")
        if _install_protected_hook(name, board=board_mod, worktree_pool=worktree_pool):
            print(f"✓ 보호 브랜치 pre-push 훅 (재)설치: {name} (T-0076).")
        return 0

    # 3) base 브랜치 해소 (T-0075) — bare 가 존재하는 지금 시점에 해소·검증한다.
    #    --base 지정 → 존재 검증(없으면 rc 1·areas 등록 막음). 미지정 → bare HEAD 명시값화.
    base = _resolve_base(base_arg, bare_path, runner=runner)
    if base is _BASE_INVALID:
        print(
            f"[중단] --base {base_arg!r} 가 `.repos/{name}.git` 에 없다 "
            f"(`git -C {bare_path} show-ref --verify --quiet refs/heads/{base_arg}` 실패). "
            "브랜치명을 확인하거나 `--base` 를 생략(기본 브랜치 사용)하라.",
            file=sys.stderr,
        )
        return 1

    # 4) areas.md 등록 — repo/prefix/git/test_cmd/owner/base/protected 칼럼(ADR-0014·T-0075·T-0076).
    #    protected 는 빈 값으로 등록 — `_repo_protected` 가 DEFAULT_PROTECTED(main/master/develop)
    #    폴백한다(per-repo override 는 areas.md 를 직접 편집·후속 `--protected` 플래그 여지).
    board_mod.areas_append(
        name, "", owner, repo=name, git=args.git, test_cmd=args.test, base=base,
        protected="",
    )
    # --test 미지정(None) 이면 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로
    # 폴백한다(T-0066). 빌드명령은 worktree add 프롬프트·콘솔 [b] 에서 채울 수 있다.
    test_surface = args.test if args.test else "(미지정 — worktree add/콘솔 [b] 에서 설정)"
    base_surface = base if base else "(미해소 — worktree add 가 bare HEAD 사용)"
    print(
        f"✓ areas.md 등록: {name} | git={args.git} | test_cmd={test_surface} | "
        f"owner={owner} | base={base_surface}"
    )
    # 5) 보호 브랜치 pre-push 훅 설치 (T-0076·멱등 자가치유) — 보호목록(areas protected→
    #    default) sidecar + bare core.hooksPath wiring. 회사 repo 무영향(.project_manager/.local).
    if _install_protected_hook(name, board=board_mod, worktree_pool=worktree_pool):
        print(f"✓ 보호 브랜치 pre-push 훅 설치: {name} (T-0076·기본 main/master/develop).")
    return 0


def _prompt_test_cmd(input_fn: Callable[[str], str], *, default: str) -> str | None:
    """worktree add 빌드명령 프롬프트 — `빌드/테스트 명령? [Enter=repo 기본값 유지 <default>]:`.

    **빈 입력(엔터만) → None(슬롯 미바인딩)** — 슬롯 리스 test_cmd 가 board 의 해소 체인서
    areas per-repo test_cmd 보다 *우선*([[T-0066]])이라, 빈입력에 기본값을 박으면 areas 의
    그 repo test_cmd(예: `go test ./...`)를 잘못 덮는다(must-fix 1·codex). 빈입력은 None 으로
    슬롯을 비워 해소 체인이 areas/local.conf 로 폴백하게 한다(기존 동작 보존). `default` 는
    *표시 전용* — Enter 시 적용될 repo 폴백값(areas→local.conf→pytest-q)을 투명하게 보일 뿐
    슬롯엔 안 박는다. 비어있지 않은 입력만 그 값으로 슬롯에 바인딩된다.

    `EOFError`/`KeyboardInterrupt` 는 우아하게 None 으로 흡수(미지정 폴백·크래시 0). input_fn
    주입으로 hermetic 테스트(라이브 input 블록 0·주입 시퀀스로 결정적).
    """
    try:
        raw = input_fn(f"빌드/테스트 명령? [Enter=repo 기본값 유지: {default}]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    cmd = raw.strip()
    return cmd if cmd else None


def cmd_worktree_add(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
    board=None,
    input_fn: Callable[[str], str] = input,
    is_tty: Callable[[], bool] | None = None,
) -> int:
    """`worktree add <repo> [--test "<cmd>"]` — 새 슬롯 생성 + submodule init (ADR-0013).

    worktree_pool.create_slot(repo, base=) 를 호출한다 — `<repo>_<N>`(브랜치 무관 재사용
    컨테이너) 슬롯을 `git worktree add` 로 만들고 `git submodule update --init` 한다.
    브랜치 무관(명령표 외 — 브랜치 할당은 pm-bootstrap 소관·spike §8-5).

    **base 브랜치 (T-0075)**: areas.md 의 그 repo base(`pm-config repo add --base`/clone-time
    bare HEAD 가 기록)를 `_resolve_repo_base` 로 읽어 `create_slot(base=)` 로 전달한다 — 슬롯
    브랜치 `<repo>_<N>` 가 그 base(develop 등)에서 파생된다. areas 에 base 없으면(구 스키마/
    솔로/미지정) None → create_slot 이 현행 bare HEAD 동작(회귀 0).

    test_cmd(슬롯 리스 바인딩·T-0066·ADR-0014 amend) 해소:
      - `--test "<cmd>"` 명시 → 그 값을 바인딩(현행·CLI 정확작업·CI).
      - `--test` 미지정 + **tty** → 슬롯 생성 후 빌드명령 프롬프트. **빈입력(Enter) → None
        (슬롯 미바인딩)** → 해소 체인이 areas/local.conf 로 폴백(기존 동작 보존·must-fix 1).
        프롬프트 `[Enter=repo 기본값 유지: …]` 표시값은 그 repo 의 *실제 폴백*(areas test_cmd
        → local.conf → pytest -q·`_resolve_repo_test_cmd`)이라 Enter 가 무엇을 적용하는지 투명.
      - `--test` 미지정 + 비-tty(CI/파이프) → 프롬프트 생략·None(현행·repo areas/local.conf 로 해소).
    board._test_cmd 가 활성 슬롯의 이 값을 areas 위 레이어로 읽으므로, 빈입력에 기본값을 박으면
    areas per-repo test_cmd 를 잘못 덮는다 → 빈입력은 반드시 None(슬롯 미바인딩)이어야 한다.

    worktree_pool/board/input_fn/is_tty 주입으로 hermetic 테스트(실 worktree add·라이브 input
    없이 배선·분기 검증). board 는 프롬프트 표시값 areas 해소 재사용용(콘솔이 로드한 board 전달).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 생성 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    # test_cmd 해소: --test 명시면 그 값. 미지정 + tty 면 프롬프트(빌드명령). 비-tty 면 None.
    # 프롬프트 표시 기본값은 그 repo 의 실제 폴백(areas→local.conf→pytest-q)을 resolve 해
    # 보여준다(Enter 시 적용될 값 투명화) — 빈입력은 그래도 None 으로 슬롯을 비운다(must-fix 1).
    tty_check = is_tty if is_tty is not None else _stdin_is_tty
    test_cmd = getattr(args, "test", None)
    if test_cmd is None and tty_check():
        display_default = _resolve_repo_test_cmd(args.repo, board=board)
        test_cmd = _prompt_test_cmd(input_fn, default=display_default)

    # base 해소 (T-0075) — areas.md 의 그 repo base 를 읽어 create_slot(base=) 로 전달한다.
    # 슬롯 브랜치 `<repo>_<N>` 가 그 base(repo add 가 기록·develop 등)에서 파생된다. areas 에
    # base 없으면(구 스키마/솔로/미지정) None → create_slot 이 현행 bare HEAD 동작(회귀 0).
    # board 직접 import 금지(ADR-0013 isolation) — 주입/로드된 board 의 `_repo_base` 만 쓴다.
    base = _resolve_repo_base(args.repo, board=board)

    try:
        lease = wp.create_slot(args.repo, base=base, test_cmd=test_cmd)
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 생성 실패: {exc}", file=sys.stderr)
        return 1
    slot_path = wp.slot_path(lease.slot)
    test_line = f"\n  test_cmd 바인딩: {lease.test_cmd!r} (이 슬롯 회귀명령)" if lease.test_cmd else ""
    print(
        f"✓ worktree 슬롯 생성: {lease.slot} (repo={lease.repo}) → {slot_path}{test_line}\n"
        "  코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`."
    )
    # 보호 브랜치 pre-push 훅 (재)설치 (T-0076·멱등 자가치유) — 슬롯 op 마다 (재)설치해 엔진
    # update 후 기존 repo 도 다음 worktree add 에 훅을 얻는다(별도 명령 불요·회사 repo 무영향).
    if _install_protected_hook(args.repo, board=board, worktree_pool=wp):
        print(f"✓ 보호 브랜치 pre-push 훅 (재)설치: {args.repo} (T-0076).")
    return 0


def cmd_status(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`status | whoami` — 풀/리스 + 이 세션 repo/슬롯/branch surface (ADR-0011·0013).

    worktree_pool.list_leases() 로 전체 리스 장부를 surface 하고, 이 세션($CLAUDE_
    SESSION_NAME)이 보유한 leased 슬롯을 별도로 강조한다(whoami 의 "나" 표면).
    status·whoami 는 같은 데이터·같은 핸들러 — whoami 는 이 세션 줄을 머리에 둔다.

    브랜치는 `worktree_pool.current_branch(slot)` 로 슬롯 worktree 의 git HEAD 에서 **live**
    조회한다(ADR-0013 amend T-0072 — git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git
    checkout` 해도 즉시 반영·드리프트 없음. detached/조회불가는 "(detached/조회불가)".

    worktree_pool 주입으로 hermetic 테스트.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 리스 상태 조회 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    leases = wp.list_leases()
    sess = _default_session()
    mine = [l for l in leases if l.state == "leased" and l.session == sess]

    # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회한다(ADR-0013 amend T-0072 —
    # git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영.
    # detached/조회불가는 None → "(detached/조회불가)" 로 surface.
    def _live_branch(slot: str) -> str:
        return wp.current_branch(slot) or "(detached/조회불가)"

    print(f"# pm-config {args.command} — 세션: {sess}")
    if mine:
        print("## 이 세션의 리스:")
        for l in mine:
            print(f"  - {l.slot} (repo={l.repo} · branch={_live_branch(l.slot)})")
    else:
        print("## 이 세션의 리스: (없음)")

    print("## 풀 전체 리스 장부:")
    if not leases:
        print("  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)")
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · branch={_live_branch(l.slot)} · "
            f"state={l.state} · session={l.session or '-'} · pid={l.pid}"
        )
    return 0


def cmd_set_test_cmd(
    slot: str,
    cmd: str | None,
    *,
    worktree_pool=None,
) -> int:
    """슬롯 빌드/테스트 명령 설정·변경 — 콘솔 `[b]`·"나중에 변경" (T-0069·ADR-0014 amend).

    worktree_pool.set_test_cmd(slot, cmd) 로 기존 슬롯 리스의 test_cmd 를 갱신한다(flock +
    atomic write·worktree_pool 책임). 별도 CLI 서브커맨드는 없다 — 콘솔 `[b]` 와 worktree
    add 프롬프트가 변경 경로를 흡수(결정 §setter 단순화). 장부에 슬롯이 없으면(`KeyError`)
    명시 에러 rc 1(침묵 무력화 금지). `cmd=None`/빈 문자열이면 바인딩 해제(폴백·현행).

    worktree_pool 주입으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 빌드명령 변경 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    normalized = (cmd.strip() or None) if cmd else None
    try:
        lease = wp.set_test_cmd(slot, normalized)
    except KeyError:
        print(
            f"[중단] 슬롯 {slot!r} 에 대한 리스가 없다 — 먼저 `worktree add` 로 슬롯을 만들라.",
            file=sys.stderr,
        )
        return 1
    if lease.test_cmd:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 설정: {lease.test_cmd!r} (이 슬롯 회귀명령).")
    else:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 해제 — repo areas/local.conf 로 폴백(현행).")
    return 0


def cmd_release(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`release <slot> [--force]` — 작업완료 반납 / 수동 강제 백스톱 (ADR-0013).

    - 기본: worktree_pool.release(slot) — dirty 면 ReleaseRefused(수동 정리 요구).
    - --force: worktree_pool.force_release(slot) — dirty/leased 무시 강제 idle 화
      (dirty 는 stash 보존 시도). 장부에 슬롯 없으면 무해 종료.

    런타임 alloc/release 자동화는 파사드 비관여(bootstrap/handoff) — 여기는 수동
    반납/강제만(spike §8-5·§3e). worktree_pool 주입으로 hermetic 테스트.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 반납 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    if args.force:
        lease = wp.force_release(args.slot)
        if lease is None:
            print(f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해).")
        else:
            print(f"✓ 슬롯 {args.slot!r} 강제 반납(idle 화) — dirty 는 stash 보존 시도.")
        return 0

    try:
        wp.release(args.slot)
    except KeyError:
        print(f"[중단] 슬롯 {args.slot!r} 에 대한 리스가 없다.", file=sys.stderr)
        return 1
    except wp.ReleaseRefused:
        print(
            f"[중단] 슬롯 {args.slot!r} 이 dirty — 반납 거부(작업 유실 방지). "
            "수동 정리 후 재시도하거나 `release --force`(stash 보존 강제).",
            file=sys.stderr,
        )
        return 1
    print(f"✓ 슬롯 {args.slot!r} 작업완료 반납(idle 화) — 풀에 재사용 컨테이너로 반환.")
    return 0


def cmd_update(
    forward_args: list[str],
    *,
    pm_update=None,
) -> int:
    """`update [--from ...]` — 엔진 갱신 (pm-update 흡수·T-0054).

    pm_update.main(forward_args) 로 verbatim forward 한다 — pm_update 가 CLI 계약의
    단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0·rename 비용 0).
    forward_args 는 `update` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--from`·`--dry-run` 등 option-like 플래그를 디스패처가 가로채지 않게 `pm_config.main`
    이 (raw[0]=="update" special-case 로) argparse 를 우회해 이 핸들러로 raw 토큰을 넘긴다.
    pm_update.main 은 자체 argparse 로 `--from` 등을 직접 받는다(update 서브커맨드 개념 없음·
    우회 주체는 pm_update 가 아니라 pm_config.main 이다).

    pm_update 주입으로 hermetic 테스트(실 동기화 없이 forward 배선 검증).
    """
    pm_update_mod = pm_update or _load_module("pm_update", "pm_update.py")
    if pm_update_mod is None:
        print(
            "[중단] pm_update.py 엔진을 찾을 수 없다 — 엔진 갱신 불가 "
            f"({TOOLS_DIR / 'pm_update.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    return pm_update_mod.main(forward_args)


def cmd_init(
    forward_args: list[str],
    *,
    board=None,
) -> int:
    """`init [<board init 인자>]` — clone 당 1회 셋업 (board.py init 흡수·T-0065).

    board.main(["init", *forward_args]) 로 verbatim forward 한다 — board.py init 이
    CLI 계약의 단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0).
    forward_args 는 `init` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--prefix`·`--area`·`--owner`·`--session` 등 option-like 플래그를 디스패처가
    가로채지 않게 `pm_config.main` 이 (raw[0]=="init" special-case 로) argparse 를
    우회해 이 핸들러로 raw 토큰을 넘긴다. board.main 은 자체 argparse 의 `init`
    서브커맨드로 그 플래그를 직접 받는다(우회 주체는 board 가 아니라 pm_config.main 이다).

    init 은 N=1·M=1(solo) ~ N×M 공용 보편 셋업 — pm-config init 은 그걸 single-user
    multi-repo front door 로 노출만 한다(동작 불변·새 동작 0·cmd_update 의 위임 패턴 동형).

    board 주입으로 hermetic 테스트(실 셋업 부작용 없이 forward 배선 검증).
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — clone 셋업 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    return board_mod.main(["init", *forward_args])


# ── 대화형 콘솔 (T-0069) ──────────────────────────────────────────────────────
# 무인자(tty) `pm-config` 의 휴먼 프론트엔드. 상태를 렌더하고 메뉴로 액션을 받고
# 입력마다 바뀐 상태를 재렌더한다. 액션은 모두 *기존 핸들러*(cmd_repo_add·
# cmd_worktree_add·cmd_set_test_cmd)로 위임한다 — 콘솔은 얇은 셸(동작 분기 0·중복 0).
# 커맨드형 CLI 와 공존(같은 핸들러). 엔진/입력은 DI seam → hermetic 테스트.


def _render_repos(board_mod) -> None:
    """areas.md per-repo 레지스트리 행을 surface 한다 (board._parse_areas 재사용).

    board 가 없거나 areas 파서가 없으면(부재·로드실패) 안내만 출력(크래시 0). areas.md
    부재(솔로/미배선)면 빈 안내. 중복 파싱 구현 0 — board 의 헤더-인식 파서를 그대로 쓴다.
    """
    print("## repos (areas.md per-repo 레지스트리):")
    parse_areas = getattr(board_mod, "_parse_areas", None) if board_mod else None
    if parse_areas is None:
        print("  (board.py 엔진/areas 파서 없음 — repo 등록 상태 조회 불가)")
        return
    _header, rows = parse_areas()
    if not rows:
        print("  (등록된 repo 없음 — [r] 로 추가하라)")
        return
    for row in rows:
        name = row.get("repo") or row.get("prefix") or "(?)"
        print(
            f"  - {name} · prefix={row.get('prefix') or '-'} · "
            f"git={row.get('git') or '-'} · test_cmd={row.get('test_cmd') or '(미지정)'} · "
            f"owner={row.get('owner') or '-'} · base={row.get('base') or '(bare HEAD)'}"
        )


def _render_slots(wp) -> None:
    """worktree 풀 리스 장부를 slot·repo·build(test_cmd)·state·session 으로 surface.

    worktree_pool.list_leases() 재사용. wp 가 없으면 안내만(크래시 0). cmd_status 의 풀
    surface 와 같은 데이터 — 콘솔은 build(test_cmd) 칼럼을 강조한다(이 콘솔의 1급 관심사).
    """
    print("## slots (worktree 풀 리스):")
    if wp is None:
        print("  (worktree_pool.py 엔진 없음 — 슬롯 상태 조회 불가)")
        return
    leases = wp.list_leases()
    if not leases:
        print("  (슬롯 없음 — [w] 로 추가하라)")
        return
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · build={l.test_cmd or '(미지정)'} · "
            f"state={l.state} · session={l.session or '-'}"
        )


def _render_state(board_mod, wp) -> None:
    """콘솔 상태 1회 렌더 — repos(areas) + slots(리스). 액션마다 재호출(재렌더)."""
    print()
    print(f"# pm-config 콘솔 — 세션: {_default_session()}")
    _render_repos(board_mod)
    _render_slots(wp)


_CONSOLE_MENU = (
    "\n메뉴: [r] repo 추가 · [w] worktree 추가 · [b] 슬롯 빌드명령 설정/변경 · "
    "[u] 엔진 갱신 · [s] 새로고침 · [q] 종료"
)

# 콘솔 프롬프트 중단(EOF/Ctrl-C) sentinel — `_console_input` 이 예외 대신 이걸 돌려준다.
# 호출부(메뉴 루프·각 액션)는 `is _CONSOLE_ABORT` 로 판정해 일관되게 취소/종료한다.
# `None`/`""` 과 구별돼야(그건 정상 빈입력 의미) — 고유 sentinel 객체로 둔다.
_CONSOLE_ABORT = object()


def _console_input(input_fn: Callable[[str], str], prompt: str):
    """콘솔 공유 입력 헬퍼 — `EOFError`/`KeyboardInterrupt` 를 `_CONSOLE_ABORT` 로 흡수.

    **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]` 이름/git/test·`[w]` repo·`[b]` slot/
    빌드명령)가 이 헬퍼를 거친다 — 어느 프롬프트서 Ctrl-C/EOF 가 나도 예외가 전파돼 크래시
    하는 것을 막는다(must-fix 2·codex — "우아 종료/크래시 0" 계약). 반환:
      - 정상 입력 → `str`(strip 안 함 — 호출부가 의미에 맞게 strip; 빈입력 보존).
      - EOF/Ctrl-C → `_CONSOLE_ABORT`(호출부가 액션 취소/메뉴 복귀 또는 루프 종료).
    input_fn 주입으로 hermetic(라이브 input 블록 0).
    """
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return _CONSOLE_ABORT


def _console_repo_add(input_fn, board_mod):
    """`[r]` — repo 이름/git/test/base 를 프롬프트로 받아 cmd_repo_add 위임 (기존 핸들러 재사용).

    각 프롬프트가 `_console_input` 을 거쳐 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 를 받는다 —
    그 경우 이 액션 자체가 `_CONSOLE_ABORT` 를 *반환*해 run_console 이 루프를 우아 종료한다
    (must-fix 2 — 액션 내부 중단도 traceback 0·rc 0). 빈입력은 취소(None 반환·메뉴 복귀).
    """
    name = _console_input(input_fn, "repo 이름 (= prefix): ")
    if name is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    name = name.strip()
    if not name:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    git = _console_input(input_fn, "git URL: ")
    if git is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    git = git.strip()
    if not git:
        print("  (git URL 비어 있음 — 취소)")
        return None
    test = _console_input(
        input_fn, "test 명령 (빈 입력 = 미지정·나중에 worktree/[b] 에서 설정): "
    )
    if test is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    test = test.strip()
    base = _console_input(input_fn, "base 브랜치 (빈 입력 = 기본 브랜치 사용): ")
    if base is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    base = base.strip()
    # 빈입력 → base=None(= CLI `--base` 생략 = bare HEAD 기본·기존 동작). 비어있지 않으면
    # 그 브랜치명을 cmd_repo_add 로 — 형식/존재 검증은 콘솔이 따로 안 하고 `_resolve_base`
    # 의 `show-ref --verify`(T-0078) 단일 sink 가 거른다(중복 검사 0).
    args = argparse.Namespace(
        name=name, git=git, test=(test or None), owner=None, base=(base or None)
    )
    cmd_repo_add(args, board=board_mod)
    return None


def _console_worktree_add(input_fn, wp, board_mod=None):
    """`[w]` — repo 를 받아 cmd_worktree_add 위임. 빌드명령은 그 핸들러가 프롬프트(tty 경로).

    repo 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). 빈입력은 취소.
    board_mod 를 cmd_worktree_add 에 전달해 빌드명령 프롬프트의 표시 기본값(areas 해소)을
    콘솔이 이미 로드한 board 로 재사용한다(중복 로드 0). 빌드명령 프롬프트 내부 중단은
    cmd_worktree_add → _prompt_test_cmd 가 None 으로 흡수(크래시 0·기존 폴백).
    """
    repo = _console_input(input_fn, "슬롯을 만들 repo 이름: ")
    if repo is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    repo = repo.strip()
    if not repo:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    # 콘솔은 항상 대화형(tty 전제) → cmd_worktree_add 가 빌드명령 프롬프트를 띄우게
    # is_tty=lambda: True 로 강제(콘솔 진입 자체가 tty 보장·main 분기). --test 는 미지정.
    args = argparse.Namespace(repo=repo, test=None)
    cmd_worktree_add(
        args, worktree_pool=wp, board=board_mod, input_fn=input_fn, is_tty=lambda: True
    )
    return None


def _console_set_test_cmd(input_fn, wp):
    """`[b]` — slot·새 빌드명령을 받아 cmd_set_test_cmd 위임 (worktree_pool.set_test_cmd).

    slot·빌드명령 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). slot 빈입력은
    취소. 빌드명령 빈입력은 None(바인딩 해제·현행).
    """
    slot = _console_input(input_fn, "빌드명령을 바꿀 슬롯 (work/<repo>_<N>): ")
    if slot is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    slot = slot.strip()
    if not slot:
        print("  (슬롯 비어 있음 — 취소)")
        return None
    cmd = _console_input(input_fn, "새 빌드/테스트 명령 (빈 입력 = 바인딩 해제): ")
    if cmd is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    cmd = cmd.strip()
    cmd_set_test_cmd(slot, cmd or None, worktree_pool=wp)
    return None


def _console_update(pm_update=None) -> None:
    """`[u]` — 엔진 갱신을 `cmd_update([])` 로 위임 (= pm_update.main verbatim·T-0061 흡수).

    `[r]`/`[w]`/`[b]` 와 동형의 얇은 래퍼 — 입력 프롬프트 없이(인자 없는 갱신) 기존 핸들러로
    위임만 한다(중복 0). 갱신은 repos/slots 장부를 안 바꿔도(엔진 파일 동기화) 호출부가
    상태를 재렌더한다(무해·일관). pm_update 주입으로 hermetic(실 동기화 없이 배선 검증).
    """
    cmd_update([], pm_update=pm_update)
    return None


def run_console(
    *,
    input_fn: Callable[[str], str] = input,
    board=None,
    worktree_pool=None,
) -> int:
    """대화형 콘솔 루프 — 상태 렌더 + 메뉴 + 액션 + 재렌더 (T-0069·tty 전용).

    무인자 `pm-config`(tty)가 진입한다(비-tty 는 main 이 help 로 분기). 흐름:
      1. 상태 렌더(repos via areas · slots via 리스) — cmd_status/list_leases/areas 파서 재사용.
      2. 메뉴 프롬프트 → 키 1자.
      3. 액션(`[r]`·`[w]`·`[b]`·`[u]`)은 *기존 핸들러*로 위임 → 상태 재렌더.
      4. `[s]` 새로고침(재렌더만)·`[q]` 종료. (`[u]` 엔진 갱신 = cmd_update·T-0071.)

    입력 견고성: 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0)·`EOFError`/`KeyboardInterrupt`
    → 우아 종료(메시지 + rc 0). **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]`·`[w]`·
    `[b]` 의 이름/git/repo/slot 입력)가 공유 `_console_input` 헬퍼를 거친다 — 어느 프롬프트서
    중단해도 액션이 `_CONSOLE_ABORT` 를 반환하고 루프가 우아 종료한다(must-fix 2·codex —
    "우아 종료/크래시 0" 계약·traceback 0·rc 0). 엔진(board·worktree_pool)/입력(input_fn)
    주입으로 hermetic 테스트(실 clone/worktree·라이브 input 블록 0 — 입력 시퀀스 주입 + 핸들러 mock).
    """
    board_mod = board or _load_module("board", "board.py")
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")

    _render_state(board_mod, wp)
    while True:
        print(_CONSOLE_MENU)
        # 메뉴 선택도 액션 프롬프트와 *같은* 공유 헬퍼로 통일 — 중단(EOF/Ctrl-C) → 우아 종료.
        choice = _console_input(input_fn, "선택: ")
        if choice is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        choice = choice.strip().lower()

        if choice == "q":
            print("콘솔 종료.")
            return 0
        if choice == "s":
            _render_state(board_mod, wp)
            continue
        if choice == "r":
            result = _console_repo_add(input_fn, board_mod)
        elif choice == "w":
            result = _console_worktree_add(input_fn, wp, board_mod)
        elif choice == "b":
            result = _console_set_test_cmd(input_fn, wp)
        elif choice == "u":
            # 엔진 갱신(T-0071) — 입력 프롬프트 없이 cmd_update([]) 위임 후 재렌더.
            result = _console_update()
        else:
            # 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0). 액션 안 함·상태 재렌더 안 함.
            if choice:
                print(f"  (알 수 없는 선택 {choice!r} — r/w/b/u/s/q 중 하나)")
            continue
        # 액션 내부 프롬프트가 중단(EOF/Ctrl-C)됐으면 _CONSOLE_ABORT 를 반환 — 루프 우아 종료
        # (must-fix 2 — 메뉴뿐 아니라 액션 프롬프트 중단도 traceback 0·rc 0).
        if result is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        # 액션 수행 후 바뀐 상태를 재렌더(입력마다 상태 변화 확인·T-0069 핵심).
        _render_state(board_mod, wp)


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """서브커맨드 디스패처 파서 (argparse subparsers·pm_update epilog 단일소스 계승)."""
    parser = argparse.ArgumentParser(
        prog="pm-config",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "각 서브커맨드는 엔진(board.py / worktree_pool.py / pm_update.py)으로 위임하는 "
            "얇은 배선이다. 브랜치 할당은 이 파사드가 아니라 `pm-bootstrap <repo> --branch <B>` "
            "소관(T-0060·idle 슬롯 리스 + checkout). 런타임 worktree alloc/release 자동화는 "
            "bootstrap/handoff 가 하고, 여기 `release` 는 수동 반납/강제(백스톱)만이다."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # repo add <name> --git <url> [--test "<cmd>"]  (--test optional·clone 우선)
    p_repo = sub.add_parser("repo", help="repo 등록 관리 (add)")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<repo-command>")
    p_repo_add = repo_sub.add_parser(
        "add", help="패밀리에 repo 등록 + .repos/<name>.git bare clone (ADR-0014)"
    )
    p_repo_add.add_argument("name", help="repo 이름 (= prefix · per-repo ID 네임스페이스)")
    p_repo_add.add_argument("--git", required=True, metavar="URL",
                            help="repo git URL (bare clone 원)")
    p_repo_add.add_argument("--test", metavar="CMD", default=None,
                            help="per-repo 테스트 명령 (areas.md test_cmd·회귀 게이트가 worktree 에서 실행). "
                                 "미지정 시 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로 폴백(T-0066). "
                                 "빌드명령은 worktree add 프롬프트·콘솔 [b] 에서도 설정 가능.")
    p_repo_add.add_argument("--owner", metavar="이름", default=None,
                            help="등록 owner (기본: 현 세션)")
    p_repo_add.add_argument("--base", metavar="BRANCH", default=None,
                            help="worktree 슬롯 브랜치가 파생될 base 브랜치 (T-0075·develop 등). "
                                 "미지정 시 clone 된 bare 의 기본 브랜치(원격 default)로 해소·기록. "
                                 "지정 시 존재 검증(없는 base 거부). worktree add 가 이 base 에서 슬롯 브랜치를 판다.")
    p_repo_add.set_defaults(func=cmd_repo_add)

    # worktree add <repo>
    p_wt = sub.add_parser("worktree", help="worktree 슬롯 관리 (add)")
    wt_sub = p_wt.add_subparsers(dest="worktree_command", metavar="<worktree-command>")
    p_wt_add = wt_sub.add_parser(
        "add", help="새 슬롯 생성(<repo>_<N>) + git submodule update --init (ADR-0013)"
    )
    p_wt_add.add_argument("repo", help="슬롯을 만들 repo 이름 (areas.md 등록된 것)")
    p_wt_add.add_argument(
        "--test", metavar="<cmd>", default=None,
        help="이 슬롯에 바인딩할 회귀/빌드명령 (T-0066·같은 repo 슬롯별 빌드변형·HIL config). "
             "미지정 시 repo areas/local.conf 로 해소(현행).",
    )
    p_wt_add.set_defaults(func=cmd_worktree_add)

    # status | whoami (같은 핸들러)
    p_status = sub.add_parser("status", help="풀/리스 상태 + 이 세션 repo/슬롯/branch")
    p_status.set_defaults(func=cmd_status)
    p_whoami = sub.add_parser("whoami", help="status 의 별칭 — 이 세션 리스를 머리에 표면")
    p_whoami.set_defaults(func=cmd_status)

    # release <slot> [--force]
    p_release = sub.add_parser("release", help="작업완료 반납 / 수동 강제(백스톱)")
    p_release.add_argument("slot", help="반납할 슬롯 (work/<repo>_<N>)")
    p_release.add_argument("--force", action="store_true",
                           help="dirty/leased 무시 강제 idle 화 (dirty 는 stash 보존 시도)")
    p_release.set_defaults(func=cmd_release)

    # update [--from ...] — pm-update 흡수. 실제 forward 는 main 이 argparse 우회로
    # 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--from·--dry-run)를 이 디스패처가 가로채면 안 되므로 forward
    # 토큰을 subparser 로 파싱하지 않는다.
    sub.add_parser(
        "update",
        help="엔진 갱신 (pm-update 흡수·T-0054) — 뒤 인자는 pm_update 로 verbatim forward",
        add_help=False,
    )

    # init [<board init 인자>] — board.py init 흡수. 실제 forward 는 main 이 argparse
    # 우회로 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--prefix·--area·--owner·--session)를 이 디스패처가 가로채면
    # 안 되므로 forward 토큰을 subparser 로 파싱하지 않는다(update 와 동형).
    sub.add_parser(
        "init",
        help="clone 당 1회 셋업 (board.py init 흡수·T-0065) — 뒤 인자는 board 로 verbatim forward",
        add_help=False,
    )

    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    raw = list(sys.argv[1:] if argv is None else argv)

    # 무인자 분기 (T-0069): 인자 0 일 때
    #   - tty(stdin·stdout 둘 다)  → 대화형 콘솔(run_console). multi-PM이 복잡(다수 repo·슬롯·
    #     빌드변형)해 CLI 플래그를 외우기 어려운 걸 상태 가시화 + 메뉴로 해소.
    #   - 비-tty(파이프/CI)         → 현행 help(아래 argparse 경로). input() 으로 안 멈춘다
    #     (pm_import 비-tty 폴백 패턴 동류·CI 안전). 서브커맨드를 주면 기존 CLI 경로 그대로
    #     (이 분기 미진입) — 커맨드형 동작 0 변경.
    if not raw and _stdin_is_tty():
        return run_console()

    # `update` 는 argparse 를 우회해 뒤 인자를 pm_update 로 *verbatim* forward 한다 —
    # `--from`·`--dry-run` 같은 option-like 플래그를 디스패처가 가로채지 않게 한다
    # (pm_update 가 CLI 계약의 단일 진실·중복 파싱 0). `update` 가 첫 토큰일 때만.
    if raw and raw[0] == "update":
        return cmd_update(raw[1:])

    # `init` 도 동형 — 뒤 인자를 board.py init 으로 *verbatim* forward 한다.
    # `--prefix`·`--area`·`--owner`·`--session` 같은 option-like 플래그를 디스패처가
    # 가로채지 않게 한다(board.py init 이 CLI 계약의 단일 진실·중복 파싱 0). `init` 이
    # 첫 토큰일 때만.
    if raw and raw[0] == "init":
        return cmd_init(raw[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    # 서브커맨드 미지정 — 등록 안내 surface(--help 단일 소스).
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 1

    # `repo`/`worktree` 만 주고 하위 동작(add)을 안 줬으면 그 그룹 도움말 surface.
    if args.command == "repo" and getattr(args, "repo_command", None) is None:
        parser.parse_args(["repo", "--help"])
        return 1
    if args.command == "worktree" and getattr(args, "worktree_command", None) is None:
        parser.parse_args(["worktree", "--help"])
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
