#!/usr/bin/env python3
"""worktree 풀 엔진 — 슬롯 리스 alloc/release/reclaim_stale/force_release (ADR-0013).

repo별 git worktree 풀로 *코드*를 격리한다(병렬 브랜치·나중 git merge). 슬롯은
브랜치-무관 재사용 컨테이너(`work/<repo>_<N>`)이고, 브랜치는 슬롯 worktree 의 git HEAD 에서
**live** 로 읽는다(ADR-0013 amend T-0072 — git=단일 진실·장부 저장 폐지·드리프트 불가능·
`current_branch(slot)`). 코드 동시성의 격리 레이어 — 보드(공유 `.project_manager`)
동시성은 board.py 가 따로 책임진다(별 모듈·여기선 import 하지 않는다).

설계 (ADR-0013 / sealed spike §8-2·8-6·8-4(d)):
  - 슬롯 = `work/<repo>_<N>`(repo + 번호·브랜치 무관·전이적 물리자원). 폴더명에 브랜치를
    안 박는다(박으면 stale — 사용자 통찰 §8-6).
  - 브랜치 = 슬롯 worktree 의 git HEAD 에서 live 조회(`current_branch(slot)`·ADR-0013
    amend T-0072 — 장부 저장 폐지·git=진실). 브랜치 변경 = 같은 슬롯 재체크아웃(리스 유지).
  - 리스 = 작업스트림(브랜치) 단위. alloc@bootstrap · release@작업완료(세션종료/회전 ≠
    release). 회전은 리스 유지 + 같은 슬롯 재부착.
  - stale 회수 = pid 생존만(타임아웃/heartbeat 기각·조기회수 위험). dirty 면 stash 보존.
  - git 연동 = DI seam(주입 가능 runner) — `git worktree add/remove`·dirty 검사·stash·
    submodule init 을 seam 통해 호출 → hermetic 테스트(mock 또는 실 임시 repo).

장부 동시쓰기 보호 = **자체 파일락**(stdlib fcntl/msvcrt·둘 다 없으면 단일-머신 폴백).
board.py 의 `board_lock` 과 *같은 패턴*이지만 **독립 구현**이다 — 병렬 작업 충돌 회피 +
worktree 풀이 board 모듈에 의존하지 않게 하기 위함(import 금지·ADR-0013 touches 격리).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·pm_*.py 와 동일 앵커 관례(sealed spike §8-4).
# multi-PM 모델에서 이 도구가 어느 worktree cwd 에서 호출돼도 자기 위치(multi-PM 루트 .project_manager)를
# 자동 타깃한다.
REPO = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO / ".project_manager" / ".local"               # per-clone scratch (git-ignored)
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"               # 리스 장부 (ADR-0013)
LEASES_LOCK = LOCAL_DIR / "worktree-leases.lock"               # 장부 read-modify-write 직렬화 락
WORK_DIR = REPO / "work"                                        # worktree 풀 루트 (multi-PM 루트 gitignore)
REPOS_DIR = REPO / ".repos"                                     # worktree 의 공유 .git 원 (bare·ADR-0011 §31)
REPO_HOOKS_DIR = LOCAL_DIR / "repo-hooks"                       # per-repo pre-push 보호훅(프레임워크 소유·gitignore·T-0076)

GIT_TIMEOUT_SECONDS = 120

# submodule init 인터랙티브 러너의 timeout (T-0070). 짧은 git(status·worktree add·
# checkout)은 GIT_TIMEOUT_SECONDS(=120) 로 충분하지만, submodule clone 은 대형 family
# repo + VPN 에서 600s 도 초과(실 Windows multi-PM 파일럿 "10분 아슬" 실증) → TimeoutExpired
# 로 죽었다. 인터랙티브 러너는 stdio 를 콘솔에 상속(진행상황·credential 프롬프트 작동)하고
# 이 대폭 확대된 timeout(또는 None=무제한)으로 큰 clone 을 끝까지 돌린다. 수동 콘솔 실행과
# 동일 거동. None 으로 두면 timeout 자체를 끈다(완전 무제한·hang 위험은 콘솔에 가시).
#
# env override (T-0070·reviewer): 극단적 대형 repo·느린 VPN 에서 1h 도 모자라면 코드 수정 없이
#   `PM_SUBMODULE_TIMEOUT` 로 재조정한다 — `0`/`none`/`unlimited` → None(무제한), 양의 정수 → 그 초.
#   미설정/비정상 → 기본 3600.
def _resolve_submodule_timeout() -> "int | None":
    raw = os.environ.get("PM_SUBMODULE_TIMEOUT")
    if raw is None:
        return 3600
    raw = raw.strip().lower()
    if raw in ("0", "none", "unlimited", ""):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return 3600


SUBMODULE_TIMEOUT = _resolve_submodule_timeout()

# git CLI argv → (returncode, stdout). DI seam 의 타입(pm_import.GitRunner 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


# ── 예외 / 데이터 ─────────────────────────────────────────────────────────


class NeedsCreate(Exception):
    """풀 소진 — idle 슬롯이 없어 새 슬롯 생성이 필요하다(호출부 = bootstrap 사용자 게이트).

    `git worktree add` 는 fs 행위라 자동으로 안 한다(ADR-0013·사용자 게이트 유지) —
    alloc 호출부(pm-bootstrap)가 이 신호를 받아 사용자에게 슬롯 생성을 묻는다.
    """

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"worktree pool exhausted for repo {repo!r} — needs `git worktree add`")


class ReleaseRefused(Exception):
    """dirty worktree 를 require_clean=True 로 release 하려 함 (수동 정리 또는 자동경로 stash 필요)."""

    def __init__(self, slot: str):
        self.slot = slot
        super().__init__(f"refusing to release dirty slot {slot!r} (require_clean=True)")


class CheckoutFailed(Exception):
    """슬롯 worktree 의 branch checkout 실패 (ADR-0013).

    fail-soft 로 무시하면 리스 장부의 state/session 이 실제 worktree 상태와 어긋난다
    (부분 leased 전이). alloc 은 checkout 성공 시에만 장부를 갱신하고, 실패하면 이를
    raise 해 기존 리스 상태를 보존한다(부분 갱신 차단). 브랜치 자체는 더는 장부에
    저장하지 않는다(git=진실·ADR-0013 amend T-0072) — checkout 은 git HEAD 를 바꾼다.
    """

    def __init__(self, slot: str, branch: str, output: str):
        self.slot = slot
        self.branch = branch
        self.output = output
        super().__init__(
            f"git checkout {branch!r} failed for slot {slot!r}: {output!r}"
        )


class BareRepoMissing(RuntimeError):
    """worktree 의 공유 .git 원(`.repos/<repo>.git` bare)이 없다 (ADR-0011 §31).

    [[ADR-0011]] §31 = `.repos/<repo>.git` 가 worktree 슬롯의 공유 .git 원(canonical).
    이 bare mirror 가 없으면 `git worktree add` 의 base 가 없다 — `pm-config repo add <repo>` 가
    bare clone 으로 mirror 를 (재)생성해야 한다([[T-0061]]). **repo 가 areas.md 에 이미
    등록됐으면**(하나의 채택 폴더를 여러 사람이 clone 한 2번째 사용자·`.repos/` 는 gitignore·
    per-clone 이라 공유 안 됨) `pm-config repo add <repo>` 를 **`--git` 없이** 실행하면 areas
    등록 URL 로 mirror 를 hydrate 한다([[T-0291]]). 침묵 폴백으로 multi-PM 루트 자신의 worktree 를
    만들면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가 깨진다([[ADR-0013]]
    fail-soft 규율) → 명시 raise 로 선행 명령을 안내한다.

    **`RuntimeError` 서브클래스**인 이유: 파사드 `pm_config.cmd_worktree_add` 가
    `create_slot` 의 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로 surface 한다
    ([[T-0061]]). 베이스를 `Exception` 으로 두면 그 가드를 빠져나가 traceback 이 노출된다
    (cross-module 규격 — codex T-0063 게이트 포착).
    """

    def __init__(self, repo: str, bare_path: "Path"):
        self.repo = repo
        self.bare_path = bare_path
        super().__init__(
            f"bare mirror for {repo!r} not found at {str(bare_path)!r} — "
            f"`.repos/{repo}.git`(worktree 공유 .git 원)가 없다. areas.md 에 이미 등록됐으면 "
            f"(multi-user: `.repos/` 는 gitignore·per-clone 이라 공유 안 됨) "
            f"`pm-config repo add {repo}`(--git 불요)가 areas 등록 URL 로 mirror 를 hydrate 한다; "
            f"미등록 신규 repo 면 `pm-config repo add {repo} --git <url>` (ADR-0011 §31·T-0291)"
        )


class Lease:
    """리스 장부 한 엔트리 (ADR-0013 스키마·sealed spike §3b·amend T-0072).

    슬롯=브랜치-무관 컨테이너·session/pid=점유 주체·state=leased|idle. **브랜치는 권위
    필드가 아니다** — git 이 단일 진실(ADR-0013 amend T-0072)이라 장부에 저장하지 않고
    `current_branch(slot)` 로 슬롯 worktree 의 live HEAD 에서 읽는다(드리프트 불가능).
    (dataclass 미사용 — 엔진 도구는 `spec_from_file_location` 으로 로드되는데 sys.modules
    미등록 시 dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, slot: str, repo: str, session: str,
                 pid: int, started: str, state: str, test_cmd: str | None = None):
        self.slot = slot          # "work/<repo>_<N>" (브랜치 무관)
        self.repo = repo          # repo 이름 (per-repo 네임스페이스)
        self.session = session    # 점유 세션 식별자
        self.pid = pid            # 점유 프로세스 pid (stale 회수 판정)
        self.started = started    # 리스 시작 시각 (UTC ISO)
        self.state = state        # "leased" | "idle"
        self.test_cmd = test_cmd  # 슬롯 바인딩 회귀/빌드명령 (T-0066·ADR-0014 amend·None=미지정)

    def __repr__(self) -> str:
        return (f"Lease(slot={self.slot!r}, repo={self.repo!r}, "
                f"session={self.session!r}, pid={self.pid!r}, state={self.state!r}, "
                f"test_cmd={self.test_cmd!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Lease):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "repo": self.repo,
            "session": self.session,
            "pid": self.pid,
            "started": self.started,
            "state": self.state,
            "test_cmd": self.test_cmd,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        # 하위호환 read: test_cmd 부재(구 장부)는 None. 구 장부의 legacy `branch` 키는
        # 관용적으로 *무시*한다(ADR-0013 amend T-0072 — branch 는 권위 필드가 아니다·
        # git 에서만 온다). 키가 있어도 d.get 으로 읽지 않을 뿐 로드는 깨지지 않는다.
        return cls(
            slot=d["slot"],
            repo=d["repo"],
            session=d.get("session", ""),
            pid=int(d.get("pid", 0)),
            started=d.get("started", ""),
            state=d.get("state", "leased"),
            test_cmd=d.get("test_cmd"),
        )


class SubmoduleStatus:
    """슬롯 worktree 한 submodule 의 상태 — 역할(live HEAD) + pin 비교 판정 (ADR-0051 §Decision 4·T-0276).

    T-0275(`_resync_submodules_selective`)의 *역할 판별*을 표시층으로 재사용한다 — 역할은
    별도 장부 없이 submodule 의 live git HEAD 로 정한다(ADR-0051 §Decision 1·무스키마):

      - `kind="dev-ahead"` — **on-branch**(=dev 역할·`symbolic-ref -q HEAD` rc0). 사용자가 그
        submodule 에서 브랜치를 파 작업 중 → **정보**(경고 아님). detached pin 으로 낚아채지
        않는 selective resync 의 보호 대상(전역 recurse 가 파괴하던 크럭스 A).
      - `kind="drift"` — **detached & pin ≠ working**(`git submodule status` flag `+`/`U`) →
        **경고**. superproject pin 과 어긋난 detached. 재-alloc 시 T-0275 가 재동기하나 dirty 면
        잔존한다(그래서 dirty 도 실어 *왜* 안 풀렸는지 surface).
      - `kind="pinned"` — **detached & pin == working**(flag 공백) → 정상(pinned).
      - `kind="uninitialized"` — submodule 미초기화(flag `-`) → 경고(슬롯 init 비정상).

    `warning` = kind in {"drift", "uninitialized"}(dev-ahead/pinned 은 경고 아님 — 이 구별이
    ADR-0051 §Decision 4 의 핵심). `dirty` = 워킹트리 미커밋 변경(`_submodule_dirty` 재사용).
    (dataclass 미사용 — Lease 와 같은 이유: `spec_from_file_location` 로드 시 dataclass
    forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, path: str, kind: str, *, warning: bool, dirty: bool):
        self.path = path
        self.kind = kind
        self.warning = warning
        self.dirty = dirty

    def __repr__(self) -> str:
        return (f"SubmoduleStatus(path={self.path!r}, kind={self.kind!r}, "
                f"warning={self.warning!r}, dirty={self.dirty!r})")


class SlotStatus:
    """슬롯 worktree 한 줄 상태 — branch + upstream + submodule 역할 (ADR-0051 파일럿 T-β·T-0276).

    부트스트랩이 현재 슬롯의 상태를 1회 surface 하는 데 쓰는 구조(표시는 pm_bootstrap 이 담당).

      - `branch` — `current_branch(slot)` live(None=detached/조회불가).
      - `upstream` — `@{upstream}` 해소명(예 `origin/a5`·None=미해소).
      - `upstream_ok` — `@{upstream}` 해소 여부. 미해소=경고(T-0273/0274 로 슬롯 tracking 이
        설정돼야 정상 — 미해소면 origin-freshness 판정 불가).
      - `submodules` — 각 submodule `SubmoduleStatus` 리스트. **빈 리스트 = submodule 없는
        슬롯**(부트스트랩이 submodule 줄을 생략).

    (dataclass 미사용 — Lease/SubmoduleStatus 와 동일 이유.)
    """

    def __init__(self, slot: str, *, branch: str | None, upstream: str | None,
                 upstream_ok: bool, submodules: "list[SubmoduleStatus]"):
        self.slot = slot
        self.branch = branch
        self.upstream = upstream
        self.upstream_ok = upstream_ok
        self.submodules = submodules

    def __repr__(self) -> str:
        return (f"SlotStatus(slot={self.slot!r}, branch={self.branch!r}, "
                f"upstream={self.upstream!r}, upstream_ok={self.upstream_ok!r}, "
                f"submodules={self.submodules!r})")


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ── 자체 파일락 (board.py 와 같은 패턴·독립 구현·import 금지) ───────────────────
# 장부 read-modify-write 를 직렬화한다. POSIX=fcntl.flock·Windows=msvcrt.locking·둘 다
# 없으면 단일-머신 전제의 무락 폴백(락 파일만 생성). 프로세스가 죽으면 OS 가 락을 자동
# 해제(stale-lock 없음). stdlib 만 사용(외부 filelock 의존 금지·런타임 의존은 stdlib+git).


def _flock_acquire(fd: int) -> None:
    """OS 배타락 획득 (블로킹). POSIX=fcntl.flock·Windows=msvcrt.locking·폴백 no-op."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    except ImportError:
        pass
    # 폴백: 락 프리미티브 없음 — 단일-머신 전제로 무락 진행(락 파일만 존재).


def _flock_release(fd: int) -> None:
    """OS 배타락 해제. close 시 OS 가 자동 해제하지만 명시적으로 풀어 둔다."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass


@contextlib.contextmanager
def _lease_lock() -> Iterator[None]:
    """리스 장부 write 를 직렬화하는 OS 파일락 컨텍스트매니저 (ADR-0013).

    `.project_manager/.local/worktree-leases.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가
    자동 해제(stale-lock 없음). **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면
    안 된다(flock 재진입 동작은 OS 별로 다름). 장부의 모든 read-modify-write 가 이 한
    구간 안에서 일어난다.
    """
    LEASES_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LEASES_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock_acquire(fd)
        try:
            yield
        finally:
            _flock_release(fd)
    finally:
        os.close(fd)  # close 만으로도 OS 가 락을 해제 (크래시 시 안전망)


# ── 장부 읽기/쓰기 (락 보유 전제) ────────────────────────────────────────────


def _read_ledger() -> list[Lease]:
    """리스 장부를 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    if not LEASES_FILE.exists():
        return []
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Lease.from_dict(d) for d in data.get("leases", [])]


def _write_ledger(leases: list[Lease]) -> None:
    """리스 장부를 atomic replace 로 쓴다. **_lease_lock 보유 전제**.

    tmp 파일에 쓰고 os.replace 로 교체 — 부분쓰기로 장부가 깨지는 것을 막는다(원자 교체).
    """
    LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"leases": [l.to_dict() for l in leases]},
                         ensure_ascii=False, indent=2)
    tmp = LEASES_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(str(tmp), str(LEASES_FILE))


# ── git DI seam ────────────────────────────────────────────────────────────


def _real_git_runner(cwd: Path) -> GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner 를 만든다 (pm_import._real_git_runner 선례).

    반환 callable: argv(list) → (returncode, stdout+stderr). git 바이너리 부재(shutil.which)
    면 (1, msg)·예외는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부에 위임). `git -C
    <cwd> <argv...>` 형태로 항상 그 work tree/repo 에 묶는다. 인코딩은 엔진 관례대로 UTF-8
    (한글 경로·메시지 안전).

    **stdout+stderr 결합 반환 (T-0070·pm_config._real_clone_runner 정합)**: 옛 코드는
    `result.stdout` 만 돌려 stderr 를 버려 — TimeoutExpired/실패 시 out='' 가 돼 진단이
    불가능했다(`git submodule init failed: ''`). stderr 를 합쳐 에러를 가시화한다.
    ⚠️ `_is_dirty` 는 결합된 출력을 `_porcelain_status_lines`(porcelain 형식 라인만 추림·아래)로
    필터하므로 stderr 경고가 섞여도 dirty 오탐이 없다 — 이 결합은 진단용이고 dirty 판정은
    porcelain 라인만 보는 경로로 분리돼 있다.
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외 메시지를 surface.
            return 1, str(exc)

    return runner


def _real_git_runner_interactive(cwd: Path) -> GitRunner:
    """submodule init 전용 인터랙티브 git runner — stdio 콘솔 상속·대폭 확대 timeout (T-0070).

    `_real_git_runner` 와 달리 **capture 하지 않는다** — stdout/stderr/stdin 을 부모 콘솔에
    그대로 상속한다(`subprocess.run(..., capture_output 안 줌`). 그래서:
      - 대형 submodule clone 의 진행상황이 화면에 실시간 표시된다(긴 침묵 대신).
      - git credential/auth 프롬프트가 작동한다(수동 콘솔 실행과 동일).
      - timeout 이 `SUBMODULE_TIMEOUT`(3600s·또는 None=무제한)이라 600s 초과 대형 clone 이
        TimeoutExpired 로 죽지 않는다(실 Windows multi-PM 파일럿 블로커 해소).

    반환 `(rc, "")` — 출력은 콘솔로 직접 갔으므로 캡처 문자열은 없다(rc 로만 성공/실패 판정).
    git 부재/예외는 `(1, str(exc))`(또는 부재 메시지) — `_real_git_runner` 와 같은 fail-soft.
    `create_slot` 의 submodule 단계가 `git_runner is None` 인 실경로에서만 이걸 쓴다 — 주입된
    git_runner(테스트 mock)가 있으면 그대로(DI seam 보존·인터랙티브 안 탐).

    ⚠️ 비-tty(CI/pytest)서도 안전: 테스트는 git_runner 를 주입하므로 이 실 인터랙티브
    경로를 타지 않는다. 이 함수 자체의 단위테스트는 짧은 비-네트워크 git 명령(stdin 블록
    없음)으로만 호출한다(submodule clone 은 실행하지 않음).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            # capture_output 미지정 = stdout/stderr/stdin 부모 콘솔 상속(인터랙티브).
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                timeout=SUBMODULE_TIMEOUT,
            )
            return result.returncode, ""
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외 메시지 surface.
            return 1, str(exc)

    return runner


def _porcelain_status_lines(out: str) -> list[str]:
    """`git status --porcelain` 출력에서 *실제 status 엔트리* 라인만 추린다 (T-0070).

    porcelain v1 엔트리 형식 = `XY <path>`(X·Y = 2글자 status code·세 번째가 공백). git
    경고(stderr·`warning: ...`)가 stdout 캡처에 섞여도 그 형식이 아니므로 걸러진다 —
    `_real_git_runner` 가 stdout+stderr 를 합치게 바뀐 뒤(T-0070) dirty 오탐을 막는 가드.
    빈 줄 무시. 형식이 맞는 라인만 dirty 신호로 본다(보수성은 호출부 rc 가드가 유지).
    """
    lines: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain v1 엔트리: 2글자 status code + 공백 구분자 (예 " M file", "?? new").
        if len(line) >= 3 and line[2] == " ":
            lines.append(line)
    return lines


def _is_dirty(slot_path: Path, *, git_runner: GitRunner | None = None) -> bool:
    """슬롯 worktree 에 미커밋 변경(untracked 포함)이 있는지. git 오류 → 보수적으로 dirty.

    `git status --porcelain` 의 porcelain 엔트리 라인이 하나라도 있으면 dirty. git 호출
    실패(rc!=0)는 상태를 모르므로 **보수적으로 dirty 로 본다** — clean 으로 오판해 stash
    없이 날리는 것보다 안전.

    ⚠️ stderr 오탐 방어(T-0070): `_real_git_runner` 가 stdout+stderr 를 합쳐 반환하게
    바뀌어, status 출력에 stderr 경고(`warning: ...`)가 섞일 수 있다. `out.strip()!=""`
    로 보면 그 경고만 있어도 dirty 오탐이 난다 → porcelain 엔트리 형식 라인만 보아
    경고에 안 흔들리게 한다(`_porcelain_status_lines`).
    """
    runner = git_runner or _real_git_runner(slot_path)
    rc, out = runner(["status", "--porcelain"])
    if rc != 0:
        return True
    return len(_porcelain_status_lines(out)) > 0


def _stash(slot_path: Path, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 dirty 변경을 stash 보존(untracked 포함). (rc, stdout) 반환."""
    runner = git_runner or _real_git_runner(slot_path)
    return runner(["stash", "push", "--include-untracked",
                   "-m", f"worktree_pool auto-stash {_now_utc()}"])


# ── 슬롯 네이밍 ──────────────────────────────────────────────────────────────


def _slot_for(repo: str, n: int) -> str:
    """슬롯 식별자 `work/<repo>_<N>` (브랜치 무관·ADR-0013·sealed spike §8-6)."""
    return f"work/{repo}_{n}"


def slot_path(slot: str) -> Path:
    """슬롯 식별자(`work/<repo>_<N>`) → 절대 경로 (REPO 기준)."""
    return REPO / slot


def bare_repo_path(repo: str) -> Path:
    """repo 이름 → 그 repo 의 공유 .git 원 경로 `.repos/<repo>.git` (bare·ADR-0011 §31).

    worktree 슬롯이 add/remove 되는 git 컨텍스트. `pm_config.REPOS_DIR / f"{repo}.git"` 와
    같은 관례([[T-0061]]) — worktree 풀이 import 격리(board·pm_config 미import)라 자체 해소한다.
    """
    return REPOS_DIR / f"{repo}.git"


# ── 보호 브랜치 pre-push 훅 (T-0076·하드·회사 repo 무영향 / T-0223·라이브 게이트 승격) ────
# 훅 = `.project_manager/.local/repo-hooks/<repo>/pre-push`(프레임워크 소유·gitignore).
# bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리로 set → 슬롯 push 가 이 훅에
# 게이트된다. **회사 repo 서버/사용자 클론 무변경** — client-side·우리 bare 미러 config 1줄만.
#
# 훅은 *generic* 이다 — 보호목록을 sidecar 파일(`protected`·줄당 1브랜치·훅과 같은 디렉토리)
# 에서 읽는다. 설치(install_protected_hook)가 그 sidecar 를 채우므로(목록 변경 = 재설치로
# 갱신), 훅 본문 자체는 repo 무관하게 동일하다. POSIX sh — Windows git 번들 sh 로도 동작.
#
# 로직: stdin 의 `<localref> <localsha> <remoteref> <remotesha>` 줄들(=이 push 의 모든 ref)을 순회한다.
# pre-push 는 push 전체에 한 번 발화하는 all-or-nothing 게이트라 **보호 ref 를 전부 검증**하고 하나라도
# 실패하면 push 전체를 거부한다(예: `git push main release` 서 main 만 green 이어도 release 가 미검증이면
# 편승 차단). remote ref (`refs/heads/<b>`)의 `<b>` 가 sidecar 보호목록에 있으면 (그 ref 의 localsha 로) —
#   - `PM_ALLOW_PROTECTED_PUSH=1` 아니면 하드 차단(T-0076·echo 안내·exit 1).
#   - `PM_ALLOW_PROTECTED_PUSH=1` 이면 **라이브 게이트 승격**(T-0223·ADR-0039 D2/D3): push 되는
#     sha 로 `board.py livegate check --rev <sha>` rc0 을 추가 요구한다(릴리즈 라이브 wave 가
#     그 커밋에서 green 이어야 함). rc≠0 → 거부(2분기 안내). `PM_SKIP_LIVE_GATE=1` 이면 check
#     만 생략(라이브-무관·긴급 변경 한정·승인/protected 시맨틱 불변). python/board.py 를 못
#     돌리면 fail-closed 거부(게이트 무력화 방지). board.py 는 **PM 홈 엔진**을 쓴다 — 슬롯
#     worktree 는 회사/family repo checkout 이라 PM 엔진 파일이 없으므로(T-0076 회사 repo 무영향),
#     설치자가 훅 옆에 쓴 sidecar `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다
#     (livegate.json 도 그 PM 홈 .local 소유라 기록 위치와 정합)·인터프리터는 T-0209 실행검증 폴백.
# feature(비보호) 브랜치·tag push 는 통과(exit 0·PM_ALLOW/라이브 게이트 무관).
#
# **멱등 자가치유 배포**: install_protected_hook 이 매 호출 이 본문을 덮어쓰므로(repo add·
# worktree add), 엔진 update 후 다음 재설치가 이 신 버전을 자동 배포한다.
_PROTECTED_PRE_PUSH_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-push 가드 (T-0076·T-0223) — PM 이 보호 브랜치(main 등)에 자율 push 못 하게 +
# 승인(PM_ALLOW_PROTECTED_PUSH=1)된 protected push 도 릴리즈 라이브 게이트 green 을 추가 요구.
# install_protected_hook() 가 설치. 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치).
hook_dir=$(dirname "$0")
protected_file="$hook_dir/protected"
engine_root_file="$hook_dir/engine-root"
[ -f "$protected_file" ] || exit 0

# stdin(<localref> <localsha> <remoteref> <remotesha> 줄들)의 각 push ref 를 순회한다. pre-push 는
# push 전체에 한 번 발화(all-or-nothing) — 보호 ref 를 *전부* 검증하고, 하나라도 실패하면(하드 차단
# or 라이브 게이트 미green) exit 1 로 push 전체를 거부한다(한 push 에 보호 ref 여러 개면 각 ref 의
# localsha 로 각각 게이트·미검증 ref 가 green ref 에 편승해 올라가는 것 차단). board.py/인터프리터는
# 첫 게이트 검증 때 1회 지연 해소한다. stdin 은 파이프 아닌 fd0 직독이라 루프가 현재 셸에서 돌아
# exit·변수 누적이 훅 전체에 반영된다(서브셸 아님).
board=""
py=""
resolved=0
while read -r _local_ref local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
        refs/heads/*) branch=${remote_ref#refs/heads/} ;;
        *) continue ;;
    esac

    # 이 ref 가 sidecar 보호목록에 있나?
    is_protected=0
    while IFS= read -r protected_branch; do
        [ -n "$protected_branch" ] || continue
        if [ "$branch" = "$protected_branch" ]; then
            is_protected=1
            break
        fi
    done < "$protected_file"
    [ "$is_protected" = "1" ] || continue   # 비보호 ref(feature)·tag → 이 ref 통과·다음 ref.

    # 보호 ref — 승인(PM_ALLOW_PROTECTED_PUSH=1) 없으면 하드 차단 (T-0076·즉시 push 전체 거부).
    if [ "$PM_ALLOW_PROTECTED_PUSH" != "1" ]; then
        echo "[pm 보호 가드] 보호 브랜치 '$branch' 로의 push 거부 (T-0076)." >&2
        echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — feature 브랜치로 작업하고" >&2
        echo "  main 갱신은 사용자에게 맡긴다(PR/머지). 사용자 명시 OK 면:" >&2
        echo "    PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi

    # 승인됨(PM_ALLOW=1) — 라이브 게이트 승격 (T-0223·ADR-0039 D2/D3). 승인과 검증 스킵은 별개
    # 스위치(감사 가능). PM_SKIP_LIVE_GATE=1 이면 이 ref 의 라이브 check 만 생략(승인·protected 불변).
    [ "$PM_SKIP_LIVE_GATE" = "1" ] && continue

    # 라이브 게이트 검증 자원 1회 지연 해소. board.py 는 **PM 홈 엔진**에 있다 — 슬롯 worktree(회사/
    # family checkout)엔 PM 엔진 파일이 없으므로(T-0076 회사 repo 무영향), 설치자가 훅 옆에 쓴 sidecar
    # `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다. livegate.json 도 그 PM 홈 .local
    # 소유라 기록 위치와 정합. 인터프리터는 실행검증 폴백(python3->python->py·T-0209·WindowsApps 가짜
    # shim 회피). sidecar 부재/경로 무효/인터프리터 부재 = fail-closed 거부(무력화 방지·ADR-0039 D3).
    if [ "$resolved" != "1" ]; then
        engine_root=""
        [ -f "$engine_root_file" ] && IFS= read -r engine_root < "$engine_root_file"
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/board.py" ]; then
            board="$engine_root/.project_manager/tools/board.py"
        fi
        for _cand in python3 python py; do
            if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
                py="$_cand"
                break
            fi
        done
        if [ -z "$board" ] || [ -z "$py" ]; then
            echo "[pm 라이브 게이트] 게이트 검증 실행 불가 — fail-closed 거부 (T-0223)." >&2
            echo "  보호 브랜치 push 는 라이브 게이트 green 을 요구하는데 PM 엔진 board.py 를 못 찾았다" >&2
            echo "  (engine-root sidecar='${engine_root}', py='${py}'). 게이트를 못 돌리면 무력화 방지로 거부한다." >&2
            echo "  라이브-무관 변경(docs 등)·긴급 hotfix 면 우회:" >&2
            echo "    PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
            exit 1
        fi
        resolved=1
    fi

    # 이 보호 ref 의 push sha 로 라이브 게이트 green 검증 (기록 존재·pass·HEAD 일치=rc0·board.py 사유 surface).
    # </dev/null — 다중 ref 루프가 stdin(fd0) 직독이라 board.py 가 stdin 을 소비하면
    # all-or-nothing 순회가 깨진다(현재 안 읽지만 향후 변경 방어).
    "$py" "$board" livegate check --rev "$local_sha" </dev/null
    gate_rc=$?
    if [ "$gate_rc" -ne 0 ]; then
        # rc!=0 — 이 보호 ref 가 미green → push 전체 거부 + 2분기 안내 (환경 복구 vs 우회·ADR-0039 D3).
        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 라이브 게이트 미green (T-0223·ADR-0039)." >&2
        echo "  릴리즈(main 머지)는 릴리즈 라이브 wave 가 이 커밋에서 green 이어야 한다(위 사유 참조)." >&2
        echo "  - 환경 문제(오프라인·LLM 서비스 장애/한도·게이트 오작동)면 — 우회 아님·환경 복구 후 재실행:" >&2
        echo "      board.py livegate record" >&2
        echo "  - 라이브-무관 변경(docs 등)·긴급 hotfix 면 — 우회:" >&2
        echo "      PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi
done

# 모든 보호 ref 가 게이트 통과(또는 skip)·또는 보호 ref 없음(비보호/tag) → push 통과.
exit 0
"""


def install_protected_hook(
    repo: str,
    protected: list[str],
    *,
    git_runner: GitRunner | None = None,
) -> bool:
    """보호 브랜치 pre-push 훅 + sidecar 를 (재)설치하고 bare `core.hooksPath` 를 wiring 한다 (T-0076).

    **멱등·자가치유** — `pm-config repo add`·`worktree add` 가 매번 호출(이미 있으면 갱신).
    세 가지를 한다:
      1. 훅 디렉토리 `.project_manager/.local/repo-hooks/<repo>/` 생성(프레임워크 소유·gitignore).
      2. `pre-push` 훅(generic·POSIX sh·LF) + sidecar 2종: `protected`(보호목록·줄당 1브랜치)와
         `engine-root`(PM 홈 REPO 절대경로 1줄·T-0223 라이브 게이트 board.py 해소용). 목록/루트가
         바뀌면 재설치가 sidecar 를 덮어 갱신한다(훅 본문은 불변).
      3. bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리(절대경로)로 set
         → 슬롯 push 가 이 훅에 게이트된다.

    **bare 부재 = no-op·False**(가드) — bare 가 없으면 게이트할 대상이 없다(repo add 가 아직
    clone 안 함·솔로(단일 repo)). 훅/sidecar 도 쓰지 않고 조용히 False(설치 안 함). bare 존재면 설치
    후 True. **회사 repo 무영향** — 모든 write 는 `.project_manager/.local/` + bare config 1줄
    (client-side)·서버 ref/사용자 클론 무변경.

    `git_runner` 주입 시 `core.hooksPath` config 호출을 그 runner 로(테스트 hermetic·`git -C
    <bare>` 컨텍스트는 `_real_git_runner(bare)` 가 묶는다). LF 줄바꿈 명시(Windows 에서도 sh
    가 읽도록·newline="\\n").
    """
    bare = bare_repo_path(repo)
    if not bare.exists():
        return False  # 게이트할 bare 없음 — no-op(repo add 선행 전·솔로).

    hook_dir = REPO_HOOKS_DIR / repo
    hook_dir.mkdir(parents=True, exist_ok=True)

    # 1) pre-push 훅 (generic·POSIX sh·LF). 멱등 — 매 호출 덮어쓰기(엔진 update 자가치유).
    hook = hook_dir / "pre-push"
    hook.write_text(_PROTECTED_PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    # 2) sidecar `protected` — 보호목록(줄당 1브랜치). 목록 변경 시 재설치가 갱신.
    sidecar = hook_dir / "protected"
    sidecar.write_text(
        "".join(f"{b}\n" for b in protected), encoding="utf-8", newline="\n")

    # 2.5) sidecar `engine-root` — PM 홈 REPO 절대경로 1줄 (T-0223 라이브 게이트 board.py 해소용).
    # 설치자는 PM 홈 컨텍스트에서 도므로 REPO(=engine root)를 안다. 훅은 슬롯 worktree(회사/family
    # checkout·PM 엔진 파일 없음)에서 발화하므로 self-locate 불가 — 이 sidecar 로 PM 홈 board.py 를
    # 해소한다(livegate.json 도 PM 홈 .local 소유·기록 위치 정합). protected sidecar 와 동형·멱등.
    (hook_dir / "engine-root").write_text(
        f"{REPO.resolve()}\n", encoding="utf-8", newline="\n")

    # 3) bare core.hooksPath wiring (절대경로) — client-side·우리 미러 config 1줄.
    # **rc 검사(codex T-0076)**: config 실패면 훅이 실제로 wiring 안 됐는데 성공 보고하면 보호
    # 가드가 *침묵 무력화* 된다(하드 차단 보장 위반). rc≠0 → False 반환(호출부가 경고 surface).
    runner = git_runner or _real_git_runner(bare)
    rc, _out = runner(["config", "core.hooksPath", str(hook_dir.resolve())])
    return rc == 0


def _existing_slot_numbers(repo: str, leases: list[Lease]) -> set[int]:
    """장부에 이미 있는 이 repo 의 슬롯 번호 집합."""
    nums: set[int] = set()
    prefix = f"work/{repo}_"
    for lease in leases:
        if lease.slot.startswith(prefix):
            tail = lease.slot[len(prefix):]
            if tail.isdigit():
                nums.add(int(tail))
    return nums


# ── pid 생존 판정 (stale 회수) ───────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """pid 가 살아있는지 (stale 회수 판정·ADR-0013 — 타임아웃/heartbeat 기각·pid 생존만).

    POSIX: `os.kill(pid, 0)` — ESRCH=죽음·EPERM=살아있으나 권한 없음(=살아있음으로 간주).
    Windows: OpenProcess 로 핸들 획득 가능 여부. pid<=0 은 죽음으로 본다.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover — POSIX 테스트 환경에선 미실행
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 살아있으나 시그널 권한 없음 — 생존으로 간주(보수적·조기회수 방지)


# ── 공개 API ─────────────────────────────────────────────────────────────────


def reclaim_stale(*, git_runner: GitRunner | None = None) -> list[str]:
    """pid 죽은 leased 슬롯을 회수한다. 회수된 슬롯 식별자 리스트 반환 (ADR-0013).

    stale = `state==leased && pid 죽음`. 회수 시 dirty 면 stash 로 보존(작업 유실 방지)하고
    idle 로 전이한다(슬롯=재사용 컨테이너·worktree 폴더는 유지). alloc 진입 시 자동 호출된다.
    타임아웃/heartbeat 회수는 ADR-0013 에서 기각(조용하지만 작업 중 오판) — pid 생존만 본다.
    """
    reclaimed: list[str] = []
    with _lease_lock():
        leases = _read_ledger()
        changed = False
        for lease in leases:
            if lease.state != "leased":
                continue
            if _pid_alive(lease.pid):
                continue
            # stale — pid 죽음. dirty 면 stash 로 보존하고 idle 화.
            path = slot_path(lease.slot)
            if path.exists() and _is_dirty(path, git_runner=git_runner):
                _stash(path, git_runner=git_runner)
            lease.state = "idle"
            lease.session = ""
            lease.pid = 0
            reclaimed.append(lease.slot)
            changed = True
        if changed:
            _write_ledger(leases)
    return reclaimed


def alloc(
    repo: str,
    *,
    branch: str | None = None,
    resume: str | None = None,
    session: str | None = None,
    git_runner: GitRunner | None = None,
) -> Lease:
    """repo 슬롯을 리스한다 (ADR-0013·sealed spike §8-6).

    - **idempotent** — 이 세션(session)이 이 repo 에 이미 leased 슬롯을 갖고 있으면 그걸
      반환한다(get-or-create-my-lease). branch 가 주어지고 슬롯의 live HEAD 와 다르면 같은
      슬롯에서 재체크아웃한다(리스 유지·슬롯=브랜치-무관 컨테이너·git=진실·ADR-0013 amend T-0072).
    - **branch/resume 우선 re-alloc** — resume(또는 branch)으로 *이전 작업스트림*의 슬롯을
      찾으면(슬롯 live HEAD 가 그 브랜치) 같은 슬롯을 re-alloc 한다(회전 연속성·dirty 파일 보존 재부착).
    - **idle 슬롯 리스** — 위에 안 걸리면 idle 슬롯을 leased 로 전이(필요 시 branch checkout).
    - **풀 소진 → `NeedsCreate`** — idle 슬롯이 없으면 raise(호출부 bootstrap 사용자 게이트).

    진입 시 `reclaim_stale` 을 먼저 호출해 pid 죽은 슬롯을 회수한다(풀 가용성 회복).
    `branch` 와 `resume` 은 동의어 역할(둘 다 작업스트림 식별) — 명시된 쪽을 쓴다.
    """
    sess = session or _default_session()
    target_branch = branch if branch is not None else resume

    # alloc 진입 시 stale 회수 (풀 가용성 회복·ADR-0013).
    reclaim_stale(git_runner=git_runner)

    with _lease_lock():
        leases = _read_ledger()

        # 1) idempotent — 이 세션의 기존 leased 슬롯 (같은 repo).
        for lease in leases:
            if lease.repo == repo and lease.state == "leased" and lease.session == sess:
                # 슬롯이 이미 target_branch 인가 = 슬롯 worktree 의 live HEAD 로 판정(ADR-0013
                # amend T-0072 — git=진실·저장 복사본 미사용). 아니면 재체크아웃(git 이 권위).
                if (target_branch is not None
                        and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                    # checkout 실패면 raise — git=진실이므로 부분 실패 시 호출부에 위임(ADR-0013).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                return lease

        # 2) resume/branch 우선 re-alloc — 같은 작업스트림(브랜치)의 슬롯 재부착(연속성).
        if target_branch is not None:
            for lease in leases:
                # 이 슬롯이 target_branch 를 체크아웃 중인가 = live HEAD 로 매칭(저장 필드 아님·
                # ADR-0013 amend T-0072). 드리프트 불가능 — git 이 단일 진실.
                if (lease.repo == repo
                        and current_branch(lease.slot, git_runner=git_runner) == target_branch):
                    # checkout 을 먼저 — 실패하면 raise 해 in-memory lease·장부 모두 미변경(기존 리스 보존).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                    lease.state = "leased"
                    lease.session = sess
                    lease.pid = os.getpid()
                    lease.started = _now_utc()
                    _write_ledger(leases)
                    return lease

        # 3) idle 슬롯 리스 (브랜치 무관 재사용 컨테이너).
        for lease in leases:
            if lease.repo == repo and lease.state == "idle":
                # 슬롯이 이미 target_branch 가 아니면 재체크아웃(live HEAD 비교·ADR-0013 amend
                # T-0072). git 이 브랜치를 만든다 — 장부엔 branch 를 쓰지 않는다.
                if (target_branch is not None
                        and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                    # checkout 을 먼저 — 실패하면 raise(idle 슬롯 상태 보존·부분 leased 전이 차단).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                lease.state = "leased"
                lease.session = sess
                lease.pid = os.getpid()
                lease.started = _now_utc()
                _write_ledger(leases)
                return lease

        # 4) 풀 소진 — idle 슬롯 없음. 새 슬롯 생성은 fs 행위라 사용자 게이트(호출부).
        raise NeedsCreate(repo)


def release(
    slot: str,
    *,
    require_clean: bool = True,
    git_runner: GitRunner | None = None,
) -> Lease:
    """슬롯을 반납한다 — 작업완료 시(ADR-0013). idle 로 전이한 Lease 반환.

    - **dirty + require_clean=True → `ReleaseRefused`** — 수동 정리 요구(작업 유실 방지).
    - **require_clean=False(자동경로) → dirty 면 stash 보존 후 idle 화** — 자동화에서 막힘 방지.

    슬롯은 idle 로 전이(재사용 컨테이너로 풀에 반납)하고 session/pid 를 비운다 —
    worktree 폴더 자체는 유지(다음 리스가 재사용·remove 는 force_release/수동).
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")

        path = slot_path(slot)
        if path.exists() and _is_dirty(path, git_runner=git_runner):
            if require_clean:
                raise ReleaseRefused(slot)
            _stash(path, git_runner=git_runner)  # 자동경로 — dirty 를 stash 보존.

        target.state = "idle"
        target.session = ""
        target.pid = 0
        _write_ledger(leases)
        return target


def force_release(slot: str, *, git_runner: GitRunner | None = None) -> Lease | None:
    """수동 백스톱 — dirty/leased 여부 무시하고 슬롯을 강제로 idle 화 (ADR-0013).

    dirty 면 stash 로 보존은 시도하되(작업 유실 최소화) 거부하지 않는다. 장부에 슬롯이
    없으면 None 반환(이미 정리됨·무해). `pm-config release --force` 백스톱의 엔진 진입점.
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        path = slot_path(slot)
        if path.exists() and _is_dirty(path, git_runner=git_runner):
            _stash(path, git_runner=git_runner)  # 강제라도 작업은 보존 시도.
        target.state = "idle"
        target.session = ""
        target.pid = 0
        _write_ledger(leases)
        return target


def create_slot(
    repo: str,
    *,
    branch: str | None = None,
    base: str | None = None,
    session: str | None = None,
    init_submodules: bool = True,
    git_runner: GitRunner | None = None,
    test_cmd: str | None = None,
) -> Lease:
    """새 슬롯을 *생성*하고 leased 로 리스한다 — 풀 확장 (NeedsCreate 게이트 통과 후·ADR-0013).

    `test_cmd` 가 주어지면 그 슬롯 리스에 회귀/빌드명령을 바인딩한다(T-0066·ADR-0014
    amend) — 같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 등)을 가질 수 있게.
    board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다(미지정=None·현행).

    `git worktree add` 는 fs 행위라 사용자 게이트(NeedsCreate) 통과 후에만 불린다 —
    pm-config worktree add / bootstrap 사용자 승인이 호출부. 다음을 한다:
      1. **bare 부재 가드** — `.repos/<repo>.git` 가 없으면 `BareRepoMissing` raise(multi-PM
         worktree 침묵 폴백 금지·ADR-0011 §31·ADR-0013 fail-soft 규율).
      2. 다음 슬롯 번호 결정(`<repo>_<N>`·기존 번호 회피).
      3. `git worktree add [-B <branch>] [-b <slot> <path> <base> | <path>]` —
         **`.repos/<repo>.git` bare 컨텍스트**에서 실행해 슬롯이 그 family repo 의 worktree 가
         되게 한다(ADR-0011 §31). 분기:
           - `branch` 면 그 브랜치를 create-or-reset 으로 체크아웃(`-B <branch> <path>`).
             branch 가 신규든 기존이든 한 호출로 처리(`add <path> <ref>` 는 ref 가 *기존*이어야
             해 신규 작업스트림 브랜치엔 못 씀 → `-B` 로 통일).
           - `base` 면(branch 미지정) 먼저 `git fetch origin`(best-effort·T-0274) 후 슬롯 브랜치
             `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <repo>_<N> <path>
             origin/<base>`). repo 등록 base(areas.md·`pm-config worktree add` 가 전달)에서 일관되게
             따게 한다 — bare HEAD 가 아닌 의도한 base(develop 등). fetch 실패/origin ref 미해소면
             로컬 `<base>`(동결 head) 폴백(T-0152 refspec 은 origin/* 만 갱신·로컬 heads 는 동결·
             fail-soft). `--no-track` = 슬롯 브랜치에 origin/<base> upstream 자동설정 억제(슬롯=작업스트림).
           - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
      4. submodule init — `git worktree add` 는 submodule 자동 init 안 함(ADR-0013·spike
         §8-4(d)) → `git submodule update --init --recursive --force`(슬롯 worktree cwd).
         `--force` 는 worktree+submodule edge(bare 에서 만든 fresh 슬롯)서 plain `--init` 이
         체크아웃 못 하는 상태를 강제 init — fresh 슬롯이라 잃을 로컬 변경 0(T-0067).
      5. 장부에 leased 엔트리 등록.

    git_runner 가 주입되면 그 runner 로 모든 git 호출(테스트 hermetic). 미주입이면
    `.repos/<repo>.git` bare 컨텍스트의 실 git 으로 worktree add 후, 슬롯 경로 컨텍스트로
    submodule init.
    """
    sess = session or _default_session()

    # bare 부재 가드 — worktree 의 공유 .git 원이 없으면 base 가 없다(ADR-0011 §31). 침묵
    # 폴백(multi-PM 루트 worktree)으로 가면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가
    # 깨진다 → 명시 raise 로 `pm-config repo add` 선행 안내(ADR-0013 fail-soft 규율). 주입된
    # git_runner(테스트 mock·bare base 도 그 runner 가 모킹)도 같은 가드를 거친다 — bare
    # 부재 가드는 *경로 존재*에 대한 확인이지 실 git 호출이 아니므로 mock 모드에서도 유효.
    bare = bare_repo_path(repo)
    if not bare.exists():
        raise BareRepoMissing(repo, bare)

    with _lease_lock():
        leases = _read_ledger()
        used = _existing_slot_numbers(repo, leases)
        n = 1
        while n in used:
            n += 1
        slot = _slot_for(repo, n)
        path = slot_path(slot)

        # worktree add 는 `.repos/<repo>.git` bare 컨텍스트에서 — 슬롯이 그 family repo 의
        # worktree 가 되게 한다(ADR-0011 §31). bare repo 도 `git -C <bare> worktree add <abs
        # path>` 가 동작한다(슬롯 path 는 절대).
        #   - branch 면 `-B`(create-or-reset)로 체크아웃 — `add <path> <ref>` 는 ref 가
        #     기존이어야 하므로 신규 작업스트림 브랜치엔 못 쓴다. `-B` 가 신규/기존 모두
        #     안전(슬롯=브랜치-무관 컨테이너·ADR-0013).
        #   - base 면(branch 미지정·T-0075) 먼저 `fetch origin`(T-0274) 후 슬롯 브랜치
        #     `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <slot> <path>
        #     origin/<base>`). 슬롯 브랜치 이름은 슬롯 식별자(`<repo>_<N>`·T-0072 live-branch 정합)
        #     이고 base 만 의도한 분기점(develop 등). `add <path> <ref>` 가 아니라 `-b`(브랜치 생성)인
        #     이유: ref 만 주면 detached 거나 base 브랜치 자체에 붙어 슬롯 작업이 base 를 오염한다 →
        #     슬롯 전용 브랜치를 base 에서 새로 판다. `--no-track` = origin/<base> upstream 자동설정
        #     억제(슬롯=작업스트림). (파생 기준·fetch·--no-track 상세는 아래 base 분기 주석.)
        #   - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
        add_runner = git_runner or _real_git_runner(bare)
        if branch is not None:
            add_argv = ["worktree", "add", "-B", branch, str(path)]
        elif base is not None:
            # base 파생 — 슬롯을 *origin 최신*에서 시작한다 (T-0274). T-0152 refspec
            # (`+refs/heads/*:refs/remotes/origin/*`)은 origin/* 만 갱신하고 로컬 `refs/heads/<base>`
            # 는 clone 시점 동결이라, 로컬 base 에서 파면 슬롯이 origin 보다 stale 하게 시작한다.
            # worktree add 전에 origin 을 fetch 하고(best-effort) `origin/<base>`(존재 시)에서 판다.
            #   - fetch 실패(오프라인)면 경고 후 로컬 `<base>` 폴백 — 슬롯 생성은 막지 않는다
            #     (worktree add 핵심 부작용 보존·`_set_bare_fetch_refspec` fail-soft 규율 정합).
            #   - 파생 기준 ref = fetch 성공 + `refs/remotes/origin/<base>` resolvable → `origin/<base>`
            #     (fetch 로 갱신된 최신), 아니면 로컬 `<base>`(폴백). 로컬 `refs/heads/<base>` 동결은
            #     손대지 않는다(pool 슬롯은 자기 슬롯 브랜치를 파므로 로컬 base 직접 체크아웃 안 함·
            #     fast-forward 는 범위 밖·리스크 회피).
            #   - **`--no-track` 필수**: 슬롯 브랜치 `<repo>_<N>` 는 새 작업 브랜치(슬롯=작업스트림)라
            #     origin/<base> tracking 을 걸지 않는다. 그런데 remote-tracking ref(origin/<base>)에서
            #     `-b` 로 브랜치를 파면 git 기본 `branch.autoSetupMerge=true` 가 그 슬롯 브랜치에
            #     upstream 을 *자동* 설정한다(A_1@{upstream}=origin/<base>·`git status`/무인자 `git pull`
            #     이 슬롯 작업을 base 에 묶음). `--no-track` 으로 그 자동설정을 억제한다(로컬 <base>
            #     폴백 경로에서도 안전 — no-op·codex 게이트 포착 결함).
            ref = base
            rc, out = add_runner(["fetch", "origin"])
            if rc != 0:
                print(
                    f"[경고] `git -C {bare} fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
                    f"  로컬 `{base}`(동결 head)에서 슬롯을 판다 — 네트워크 복구 후 새 슬롯은 "
                    "origin 최신에서 시작한다 (T-0274·fail-soft).",
                    file=sys.stderr,
                )
            else:
                rc, _ = add_runner(
                    ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}"]
                )
                if rc == 0:
                    ref = f"origin/{base}"
            add_argv = ["worktree", "add", "--no-track", "-b", f"{repo}_{n}", str(path), ref]
        else:
            add_argv = ["worktree", "add", str(path)]
        rc, out = add_runner(add_argv)
        if rc != 0:
            raise RuntimeError(f"git worktree add failed for {slot!r}: {out!r}")

        # submodule init — worktree add 는 submodule 자동 init 안 함(ADR-0013·spike §8-4(d)).
        # `--force`: bare 에서 만든 fresh 슬롯의 worktree+submodule edge 에서 plain `--init` 이
        # 체크아웃 못 하는 상태(`git submodule init failed: ''` — 실 Windows multi-PM 파일럿서 빈
        # 에러로 죽음)를 강제 init 한다(T-0067). create_slot 은 *새 슬롯 생성 때만* 호출되고
        # (기존 슬롯 재사용은 alloc·재init 안 함) fresh worktree 라 잃을 로컬 변경이 없으므로
        # `--force` 안전. 솔로/submodule 없는 repo 는 `--init --recursive --force` 가 no-op rc 0.
        #
        # **인터랙티브 러너 (T-0070)**: 실경로(git_runner 미주입)에선 capture 러너 대신
        # `_real_git_runner_interactive`(stdio 콘솔 상속·SUBMODULE_TIMEOUT 3600s)로 돈다 —
        # 대형 submodule clone 이 600s 초과해 TimeoutExpired→(1,"")로 죽던 블로커 해소(진행
        # 상황 화면 표시·credential 프롬프트·대형 clone 완주). 짧은 git(worktree add 등)은
        # capture 러너 그대로. **DI seam 보존**: 주입된 git_runner(테스트 mock)가 있으면 그걸
        # 쓴다(현행 테스트 무영향) — 인터랙티브는 `git_runner is None` 실경로만.
        #
        # **원자적 롤백 (T-0070)**: 실패(rc≠0)면 leased 장부 등록 *전에* raise — 불완전 슬롯
        # 차단(기존 동작). 단 worktree add 는 *이미 성공*했으므로 raise 전에 그 worktree 를
        # 롤백한다(`_rollback_worktree`) — 안 지우면 댕글링 worktree("슬롯 없음"+재시도 "이미
        # 존재")가 남는다. 롤백은 best-effort(2차 예외 삼킴 금지·원래 에러로 raise). 빈 out
        # (Windows 인코딩 캡처 유실·인터랙티브는 항상 빈 out)에도 막히지 않게 rc + argv surface.
        if init_submodules:
            sub_runner = git_runner or _real_git_runner_interactive(path)
            sub_argv = ["submodule", "update", "--init", "--recursive", "--force"]
            rc, out = sub_runner(sub_argv)
            if rc != 0:
                _rollback_worktree(repo, path, git_runner=git_runner)
                raise RuntimeError(
                    f"git submodule init failed for {slot!r}: "
                    f"rc={rc}, argv={sub_argv!r}, out={out!r}"
                )

        # branch 는 장부에 저장하지 않는다(ADR-0013 amend T-0072) — `git worktree add -B`
        # 로 이미 그 브랜치를 슬롯 worktree 에 체크아웃했고(git=진실), 조회는 live
        # current_branch(slot) 로 한다. `branch` 파라미터는 위 worktree add 구동용으로만 쓴다.
        lease = Lease(
            slot=slot,
            repo=repo,
            session=sess,
            pid=os.getpid(),
            started=_now_utc(),
            state="leased",
            test_cmd=test_cmd,
        )
        leases.append(lease)
        _write_ledger(leases)
        return lease


def bind_slot(slot: str, repo: str, session: str, *, git_runner: GitRunner | None = None) -> Lease:
    """슬롯을 세션에 **직접 바인딩**한다 — 사람 발의 멀티-PM 정체성 선언(T-0074·lean).

    `/pm-bootstrap <repo> --slot <N>` 의 엔진 진입점. 사람이 "내가 슬롯 <N>"을 선언하면
    그 슬롯 리스를 이 세션으로 갱신(있으면) 또는 생성(없으면)한다 — **pool alloc 이 아니다**
    (풀에서 골라잡지 않는다·slot-pinned/supervise 불필요). `alloc` 의 idle-탐색/풀-소진
    `NeedsCreate`/checkout 분기 어느 것도 안 탄다(직접 바인딩).

    **`reclaim_stale` 를 절대 호출하지 않는다** — 사람 경로는 pid-회수를 하지 않는다(R4 근원
    제거·[[ADR-0013]] Amendment(T-0074)). `alloc` 은 진입 시 `reclaim_stale` 로 풀 가용성을
    회복하지만, `bind_slot` 은 슬롯을 직접 지정받으므로 회수가 필요 없다 — pid 는 정보용으로만
    기록(`os.getpid()`)하고 liveness 판정에 쓰지 않는다(명시 `release` 로만 반납).

    ⚠️ **cross-path 한계(reviewer 게이트·[[ADR-0013]] Amendment(T-0074))**: 여기 적는 pid 는
    *ephemeral bootstrap 프로세스* pid 라 bootstrap 종료 후 죽는다. **사람 경로는 회수를 안 하지만
    (위), 자동 relay 경로의 `alloc` 은 진입 시 `reclaim_stale` 를 부른다 — 같은 장부를
    공유하므로, relay 가 가동 중이면 이 bind 엔트리를 `state==leased && pid 죽음` 으로 보고
    idle 화(session 비움)할 수 있다.** 즉 사람 bind 와 자동 alloc 이 *동시 가동*하면 사람 정체성이
    회수될 수 있다("무영향" 아님). 현 사람-only 파일럿엔 relay 미가동이라 **dormant**.
    relay+사람 공존을 실제로 쓸 때 사람 bind 보호(reclaim 제외 마커 등)가 후속 필요하다.

    **branch 는 건드리지 않는다** — 브랜치는 git=단일 진실이라 슬롯 worktree HEAD 에서 live
    조회(`current_branch(slot)`·ADR-0013 amend T-0072). bind 는 리스 장부의 점유 메타(session/
    state/started/pid)만 갱신한다. `git_runner` 파라미터는 DI seam 시그니처 정합(현 구현은 git
    호출이 없어 미사용)·향후 확장 여지를 위해 유지한다.

    `_lease_lock` + `_write_ledger`(atomic) — 기존 alloc/release/set_test_cmd 와 동일한
    read-modify-write 직렬화. board.py 를 import 하지 않는다(ADR-0013 isolation·touches 격리).
    갱신/생성된 Lease 를 반환한다.
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            # 없으면 새 Lease append (직접 바인딩 — 풀 탐색/생성 게이트 없음).
            target = Lease(
                slot=slot,
                repo=repo,
                session=session,
                pid=os.getpid(),
                started=_now_utc(),
                state="leased",
            )
            leases.append(target)
        else:
            # 있으면 점유 메타만 갱신 (branch·test_cmd 는 보존). reclaim 안 거침.
            target.repo = repo
            target.session = session
            target.state = "leased"
            target.pid = os.getpid()
            target.started = _now_utc()
        _write_ledger(leases)
        return target


def list_leases() -> list[Lease]:
    """현재 리스 장부 전체를 읽어 반환한다 (조회·진단용·pm-config status)."""
    with _lease_lock():
        return _read_ledger()


def current_branch(slot: str, *, git_runner: GitRunner | None = None) -> str | None:
    """슬롯 worktree 의 git HEAD 에서 현재 브랜치를 **live** 로 읽는다 (ADR-0013 amend T-0072).

    `git symbolic-ref --short HEAD` → 브랜치명. 브랜치가 git 의 단일 진실 —
    장부에 저장된 복사본이 아니라 슬롯 worktree 의 실제 HEAD 를 매번 조회한다(사용자가
    슬롯서 직접 `git checkout` 해도 즉시 반영·드리프트 불가능).

    **`symbolic-ref --short HEAD` 를 쓰는 이유 (codex T-0072 게이트)**: `rev-parse --abbrev-ref
    HEAD` 는 (a) detached 를 `"HEAD"` 문자열로, (b) **unborn 브랜치**(아직 커밋 0 인 새 브랜치)를
    rc≠0 에러로 줘서 — *이름이 있는* unborn 브랜치를 detached/조회불가로 오판한다(→ "(미지정)").
    `symbolic-ref --short HEAD` 는 unborn 브랜치도 그 이름을 rc=0 으로 주고, detached 일 때만
    "ref HEAD is not a symbolic ref" 로 rc≠0 이라 — "현재 브랜치명 or 브랜치 아님"의 정석
    primitive 다(git=진실·ADR-0013 amend 정합). 우리 풀 슬롯은 bare(커밋 보유)에서 만들어 unborn
    이 드물지만, git 이 이름을 주면 보여야 한다.

    `None` 반환(전부 fail-soft·예외 raise 금지·표시층이 "(detached/조회불가)" 등으로 변환):
      - detached HEAD — `symbolic-ref` 가 rc≠0(symbolic ref 아님).
      - git 호출 실패 (rc≠0) — 손상/락/git 부재 등.
      - 슬롯 경로 부재 — worktree 폴더가 아직 없거나 지워짐.

    `git_runner` 미주입 시 실경로는 `_real_git_runner(slot_path(slot))` 로 해소한다
    (기존 DI seam 패턴 — 테스트는 mock runner 주입으로 hermetic·실 git 불요). **슬롯 경로
    부재 가드는 실경로(미주입)에서만 본다** — git_runner 주입 시엔 그 runner 가 존재/rc/HEAD
    를 전부 모델링하는 권위이므로 fs 가드를 건너뛴다(hermetic 테스트가 슬롯 폴더 없이 동작).
    주입 runner 가 예외를 던져도 None 으로 흡수한다(docstring 의 "raise 금지" 규칙을 DI seam
    까지 보장 — 실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸므로 실경로는 영향 없음).
    """
    runner = git_runner
    if runner is None:
        path = slot_path(slot)
        if not path.exists():
            return None
        runner = _real_git_runner(path)
    try:
        rc, out = runner(["symbolic-ref", "--short", "HEAD"])
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 None(규칙: raise 금지).
        return None
    if rc != 0:  # detached(symbolic ref 아님)·git 부재/실패 → 브랜치 없음.
        return None
    return out.strip() or None


def slot_status(slot: str, *, git_runner: GitRunner | None = None) -> SlotStatus:
    """슬롯 worktree 의 상태(branch + upstream + submodule 역할)를 live 로 읽는다 (ADR-0051 파일럿 T-β·T-0276).

    부트스트랩이 현재 슬롯 상태를 1회 surface 하는 backbone. **T-0275 의 submodule 역할
    판별을 재사용**한다(중복 구현 금지):
      - `current_branch(slot)` — 브랜치(live·`symbolic-ref --short HEAD`·ADR-0013 amend T-0072).
      - `_upstream_status` — `@{upstream}` 해소 여부(T-0273/0274 로 슬롯 tracking 설정 → 해소).
      - `_submodule_statuses` — `git submodule status`(`_parse_submodule_entries`) + submodule 당
        `git -C <sub> symbolic-ref -q HEAD`(on-branch/detached) + `_submodule_dirty` 로 역할 판정
        (`_resync_submodules_selective` 와 *같은* primitive). detached & pin≠working=drift(경고)·
        on-branch=dev-ahead(정보)·detached & pin==working=pinned.

    **전부 fail-soft — 예외 raise 안 함**(형제 backbone `current_branch`·`_resync_submodules_
    selective`·`_rollback_worktree` 와 같은 규율·미래 호출부[on-demand 전체-풀 status·sync]가
    이 계약에 기댄다): `current_branch` 는 자체 흡수. upstream/submodule 은 rc≠0 이면 미해소/
    빈목록이고, `git_runner` 자체가 **예외를 던져도** 본문 try/except 로 흡수한다(upstream 미해소·
    submodule 빈목록으로 보수적 degrade — 각각 독립 흡수라 한쪽 실패가 다른쪽을 안 가린다).
    실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸 rc≠0 경로로 오지만, 주입 runner
    (DI seam·테스트/미래 호출부)가 raise 해도 이 계약이 지켜진다. `git_runner` 미주입 시 슬롯
    worktree 바인딩 `_real_git_runner(slot_path(slot))` 로 해소(hermetic 테스트는 mock 주입).
    슬롯 경로 부재 가드는 **실경로(미주입)에서만** 본다 — 주입 runner 는 존재/rc/HEAD 를
    전부 모델링하는 권위이므로 fs 가드를 건너뛴다(`current_branch` 동형).
    """
    runner = git_runner
    if runner is None:
        path = slot_path(slot)
        if not path.exists():
            return SlotStatus(slot, branch=None, upstream=None, upstream_ok=False, submodules=[])
        runner = _real_git_runner(path)
    branch = current_branch(slot, git_runner=runner)
    # upstream/submodule 조회는 runner 예외까지 흡수한다(docstring "예외 raise 안 함" 계약).
    # 각각 독립 try/except — 한쪽 raise 가 다른쪽 정보를 가리지 않게(부분 degrade).
    try:
        upstream, upstream_ok = _upstream_status(runner)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 미해소로 흡수(경고 surface).
        upstream, upstream_ok = None, False
    try:
        submodules = _submodule_statuses(runner)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 빈목록(부트스트랩 줄 생략).
        submodules = []
    return SlotStatus(
        slot, branch=branch, upstream=upstream, upstream_ok=upstream_ok, submodules=submodules
    )


def set_test_cmd(slot: str, cmd: str | None) -> Lease:
    """기존 슬롯 리스의 test_cmd 를 갱신한다 (T-0069·ADR-0014 amend·idle/leased 무관).

    `pm-config` 콘솔의 `[b]`(슬롯 빌드명령 설정/변경)·worktree add 후의 "나중에 변경"
    경로가 부르는 setter — 슬롯에 바인딩된 회귀/빌드명령(HIL config 등)을 사후에 바꾼다
    (board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다·T-0066). 별도
    CLI 서브커맨드는 만들지 않는다(콘솔 `[b]` + worktree add 프롬프트로 충분·결정 §setter 단순화).

    create_slot 의 lease test_cmd 바인딩과 *같은* flock + atomic write 패턴을 재사용한다
    (`_lease_lock` 으로 read-modify-write 직렬화 → `_write_ledger` atomic replace). 장부에
    슬롯이 없으면 **`KeyError`** raise(침묵 무력화 금지 — 호출부가 명시 안내). 갱신된
    Lease 를 반환한다. `cmd=None` 이면 바인딩 해제(repo areas/local.conf 로 폴백·현행).
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")
        target.test_cmd = cmd
        _write_ledger(leases)
        return target


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────


def _rollback_worktree(repo: str, slot_path_: Path, *, git_runner: GitRunner | None = None) -> None:
    """`git worktree add` 성공 후 단계가 실패했을 때 만든 worktree 를 롤백한다 (T-0070).

    bare 컨텍스트(`.repos/<repo>.git`)에서 `git worktree remove <slot_path> --force` 를
    부른다 — `add` 가 거기서 일어났으므로 `remove` 도 같은 컨텍스트라야 한다(공유 .git 원
    = bare·ADR-0011 §31). 실패하면 best-effort 로 `worktree prune` 폴백.

    **best-effort·2차 예외 삼킴 금지**: 이 함수는 절대 raise 하지 않는다(롤백 자체가 실패해도
    원래 에러로 raise 되도록·호출부가 finally/except 에서 부른다). 댕글링 worktree("슬롯
    없음"+재시도 "이미 존재")가 안 생기게 fs 를 정리하는 게 목적이고, 정리 실패는 원래
    에러를 가린다(2차 예외)는 더 나쁜 결과를 부르므로 조용히 best-effort 한다.
    """
    runner = git_runner or _real_git_runner(bare_repo_path(repo))
    try:
        rc, _out = runner(["worktree", "remove", str(slot_path_), "--force"])
        if rc != 0:
            runner(["worktree", "prune"])  # 폴백 — 등록 메타만이라도 정리.
    except Exception:  # noqa: BLE001 — best-effort: 롤백 실패가 원래 에러를 가리면 안 됨.
        pass


def _checkout(slot_path_: Path, branch: str, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 에서 브랜치 체크아웃 (브랜치 변경 = 같은 슬롯 재체크아웃·ADR-0013).

    `git checkout --no-recurse-submodules <branch>`. 브랜치가 없으면 새로 만든다(`-B`) — 풀
    슬롯에 새 작업스트림을 붙이는 정상 경로. (같은 브랜치 동시 2-worktree checkout 은 git 이
    거부 — ADR-0013 §8-6.)

    **`--no-recurse-submodules` (ADR-0051 크럭스 A·codex 게이트)**: 사용자 환경(전역
    `~/.gitconfig` 또는 repo config)에 `submodule.recurse=true` 가 설정돼 있으면 plain
    `git checkout` 이 *selective resync 전에* submodule 을 재귀 갱신해 on-branch(dev) submodule
    을 detached pin 으로 낚아챈다 — ADR-0051 이 selective resync 로 막으려던 바로 그 dev 파괴.
    양 checkout 호출에 `--no-recurse-submodules`(git 2.13+·2.43 확인)를 박아 ambient config 를
    override → checkout 은 submodule 을 절대 안 건드리고 `_resync_submodules_selective` 가
    submodule 상태의 **유일 권위**가 된다(ADR-0051 "전역 recurse 대신 selective" 정신 정합).
    """
    runner = git_runner or _real_git_runner(slot_path_)
    rc, out = runner(["checkout", "--no-recurse-submodules", branch])
    if rc != 0:
        rc, out = runner(["checkout", "--no-recurse-submodules", "-B", branch])
    return rc, out


def _parse_submodule_entries(status_out: str) -> list[tuple[str, str]]:
    """`git submodule status` 출력에서 `(flag, path)` 를 뽑는다 (git 2.43).

    각 라인 형식 = `<flag><40-hex-sha> <path>[ (<describe>)]` — **선두 1글자가 status 플래그**
    (`' '`=index pin 과 일치·`'+'`=working≠pin·`'-'`=미초기화·`'U'`=충돌)다. 플래그는 항상
    라인 첫 글자(`line[0]`)이고(공백 플래그도 포함), **두 번째 whitespace 토큰이 경로**다
    (`line.split()[1]` — 선두 공백은 split 이 무시·플래그 문자는 sha 에 붙어 토큰이 안 쪼개짐).
    `len(parts) >= 2` 를 만족하면 라인은 비어있지 않으므로 `line[0]` 접근이 안전하다. 빈 출력
    (submodule 없음)·토큰 2개 미만 라인은 건너뛴다.

    `flag` 는 pin↔working 판정에 필요하다(T-0276 slot_status — `+`=drift·`' '`=pinned). 경로만
    필요한 호출부(`_resync_submodules_selective`)는 `_parse_submodule_paths` 를 쓴다(경로만 뽑음).
    """
    entries: list[tuple[str, str]] = []
    for line in status_out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append((line[0], parts[1]))
    return entries


def _parse_submodule_paths(status_out: str) -> list[str]:
    """`git submodule status` 출력에서 submodule 경로(superproject-상대)만 뽑는다 (git 2.43).

    `_parse_submodule_entries` 의 경로만 반환하는 얇은 래퍼 — 플래그가 불필요한 호출부
    (`_resync_submodules_selective`)의 기존 인터페이스를 그대로 보존한다(행동 불변).
    """
    return [path for _flag, path in _parse_submodule_entries(status_out)]


def _submodule_dirty(sub: str, runner: GitRunner) -> bool:
    """submodule 워킹트리에 미커밋 변경(untracked 포함)이 있는지 — `_is_dirty` 동형(sub 컨텍스트).

    superproject-바인딩 runner 로 `git -C <sub> status --porcelain` 을 돌려 그 submodule
    워킹트리를 본다(다중 `-C` = superproject cwd 기준 sub 로 재진입). porcelain 엔트리 라인이
    하나라도 있으면 dirty. rc≠0(상태 조회 실패)은 **보수적으로 dirty** 로 본다 — `_is_dirty`
    와 같은 규율(모르면 안 날린다·미커밋 작업 유실 방지). stderr 경고 오탐은
    `_porcelain_status_lines` 로 걸러진다(`_is_dirty` 와 동일 가드).
    """
    rc, out = runner(["-C", sub, "status", "--porcelain"])
    if rc != 0:
        return True
    return len(_porcelain_status_lines(out)) > 0


def _upstream_status(runner: GitRunner) -> tuple[str | None, bool]:
    """슬롯 브랜치의 `@{upstream}` 추적 브랜치명 + 해소 여부 (T-0276·T-0273/0274 로 설정돼야 정상).

    `git rev-parse --abbrev-ref @{upstream}` — 해소되면 rc0 + 추적 브랜치명(예 `origin/a5`),
    미설정이면 rc≠0(`fatal: no upstream configured …`)이다. **rc 를 먼저 본다** — `_real_git_runner`
    가 stdout+stderr 를 합쳐 돌려주므로(T-0070) 미해소 시 out 이 fatal 메시지로 *비어있지 않다*.
    rc≠0 또는 빈 이름이면 `(None, False)`(미해소·부트스트랩이 경고), 해소면 `(name, True)`.
    """
    rc, out = runner(["rev-parse", "--abbrev-ref", "@{upstream}"])
    name = out.strip()
    if rc != 0 or not name:
        return None, False
    return name, True


def _submodule_statuses(runner: GitRunner) -> list[SubmoduleStatus]:
    """각 submodule 을 역할별로 판정한 `SubmoduleStatus` 리스트 (T-0276·T-0275 판별 재사용).

    `_resync_submodules_selective` 와 *같은* primitive 로 역할을 정한다(중복 판별 구현 금지):
    `git submodule status`(`_parse_submodule_entries` — flag+path) + submodule 당
    `git -C <sub> symbolic-ref -q HEAD`(rc0=on-branch/dev·rc≠0=detached) + `_submodule_dirty`.

      - on-branch(dev) → `"dev-ahead"`(정보). 사용자가 그 submodule 에서 브랜치를 파 작업 중.
      - detached & flag `-`(미초기화) → `"uninitialized"`(경고·슬롯 init 비정상).
      - detached & flag 공백(pin==working) → `"pinned"`(정상).
      - detached & 그 외 flag(`+`/`U`·pin≠working) → `"drift"`(경고).

    fail-soft: `git submodule status` rc≠0(조회 불가/submodule 없음)이면 **빈 리스트**
    (부트스트랩이 submodule 줄 생략). dirty 는 *왜* drift 가 안 풀렸는지 surface 용(T-0275 는
    dirty detached 를 재동기 skip → drift 잔존).
    """
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return []  # 조회 불가(대개 submodule 없음/손상) → 빈 목록(부트스트랩 줄 생략).
    statuses: list[SubmoduleStatus] = []
    for flag, sub in _parse_submodule_entries(out):
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            kind = "dev-ahead"       # on-branch = dev 역할(정보·경고 아님).
        elif flag == "-":
            kind = "uninitialized"   # 미초기화(경고).
        elif flag == " ":
            kind = "pinned"          # detached & pin == working(정상).
        else:
            kind = "drift"           # detached & pin ≠ working('+'/'U' → 경고).
        # dirty 는 미초기화 submodule 엔 무의미하다(워킹트리 부재 → `status --porcelain` rc≠0 을
        # `_submodule_dirty` 가 보수적 True 로 → `⚠ uninitialized ·dirty` 잉여 렌더). uninitialized
        # 는 skip(False)·나머지만 `_submodule_dirty` 재사용(불필요한 `-C status` 호출도 절약·reviewer).
        dirty = False if kind == "uninitialized" else _submodule_dirty(sub, runner)
        statuses.append(
            SubmoduleStatus(sub, kind, warning=kind in ("drift", "uninitialized"), dirty=dirty)
        )
    return statuses


def _resync_submodules_selective(slot_path_: Path, *, git_runner: GitRunner | None = None) -> None:
    """브랜치 전환(`_checkout`) 성공 후 submodule 을 **선택적으로** superproject pin 에 재동기 (ADR-0051).

    worktree 풀 슬롯의 브랜치를 바꾸면 superproject 는 새 브랜치의 submodule pin 을 가리키지만
    submodule 워킹트리는 이전 pin 그대로라 drift 가 생긴다(ADR-0051 §Context). 브랜치 전환
    직후 각 submodule 을 **역할별로** 재동기한다 — 역할은 별도 장부 없이 submodule 의 live git
    HEAD 로 판별한다(ADR-0051 §Decision 1·무스키마·기본 A):

      - **on-branch submodule (= dev 역할·`symbolic-ref -q HEAD` rc0)**: **skip**. 사용자가 그
        submodule 에서 브랜치를 파 작업 중이므로 detached pin 으로 낚아채지 않는다(전역
        `submodule.recurse=true` 가 dev 브랜치를 파괴하던 크럭스 A → *selective* 인 이유).
      - **detached submodule (= consume 역할·`symbolic-ref -q HEAD` rc≠0)**: superproject pin
        으로 `git submodule update --init --recursive --force -- <sub>` 재동기. 단 워킹트리가
        **dirty(미커밋 변경)면 skip + 경고** — 재동기가 미커밋 작업을 날리지 않게 보호.
      - **submodule 없는 repo**: `git submodule status` 가 rc0·빈 출력 → no-op.

    fail-soft(브랜치 전환은 이미 성공했고 submodule 재동기 실패가 checkout 을 되돌리지 않는다·
    raise 금지): `submodule status` rc≠0(조회 불가)면 no-op 반환. update 실패는 경고만 하고
    drift 잔존을 surface 한다(침묵 무력화 금지). `git_runner` 미주입 시 슬롯 worktree 바인딩
    `_real_git_runner(slot_path_)` 로 해소(기존 DI seam 패턴·`_is_dirty` 동형) — 모든 submodule
    git 호출을 이 superproject-바인딩 runner + `-C <sub>` 로 돌려 테스트 mock 가능(hermetic).
    """
    runner = git_runner or _real_git_runner(slot_path_)
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return  # 조회 불가(대개 submodule 없음/손상) → no-op fail-soft(checkout 은 이미 성공).
    for sub in _parse_submodule_paths(out):
        # 역할 = live git HEAD(ADR-0051 §Decision 1·장부 없음). on-branch(dev) → 보호(skip).
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            continue
        # detached(consume) → pin 재동기. 단 dirty(미커밋)면 작업 유실 방지 위해 skip + 경고.
        if _submodule_dirty(sub, runner):
            print(
                f"[경고] submodule {sub!r} 이 detached 이나 미커밋 변경으로 dirty — pin "
                f"재동기 skip (작업 보호·ADR-0051). 정리 후 재-alloc 하면 재동기된다.",
                file=sys.stderr,
            )
            continue
        rc_up, out_up = runner(
            ["submodule", "update", "--init", "--recursive", "--force", "--", sub]
        )
        if rc_up != 0:
            print(
                f"[경고] submodule {sub!r} pin 재동기 실패 (rc={rc_up}): "
                f"{str(out_up).strip()[:200]} — drift 잔존 가능(ADR-0051·fail-soft·checkout 은 성공).",
                file=sys.stderr,
            )


def _checkout_required(slot: str, branch: str, *, git_runner: GitRunner | None = None) -> None:
    """`_checkout` 을 부르고 실패(rc≠0)면 `CheckoutFailed` raise (ADR-0013).

    fail-soft 로 무시하면 호출부가 장부 branch/state 를 성공처럼 갱신해 장부↔실제 worktree
    branch 가 어긋난다. 성공해야만 호출부가 장부를 갱신하도록 강제하는 가드.

    체크아웃 성공 직후 `_resync_submodules_selective` 로 submodule 을 새 브랜치 pin 에 selective
    재동기한다(ADR-0051 파일럿 T-α — detached=consume 만 재동기·on-branch=dev skip·dirty
    skip+경고). *브랜치 전환* 경로(alloc 세 checkout 분기)에만 붙는다 — `create_slot` 최초 init
    은 이 함수를 안 타고 자체 `submodule update --init --recursive --force`(fresh=전부 detached)를
    유지한다. 재동기는 fail-soft(raise 안 함)라 checkout 성공을 되돌리지 않는다.
    """
    rc, out = _checkout(slot_path(slot), branch, git_runner=git_runner)
    if rc != 0:
        raise CheckoutFailed(slot, branch, out)
    _resync_submodules_selective(slot_path(slot), git_runner=git_runner)


def _local_conf_session() -> str | None:
    """`.project_manager/local.conf` 의 `session=` (없거나 OSError → None).

    board.py 를 import 하지 않으므로(ADR-0013 isolation·touches 격리·병렬충돌 회피)
    `board.local_config().get("session")` 와 *동일 의미*를 stdlib 로 자체 구현한다 —
    plain `KEY=value`·`#` 주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
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


def _leased_sessions() -> list[str]:
    """lease 장부 state=="leased" 행들의 session 목록 (count-based 유도·ADR-0040 D1).

    `_default_session` 이 lease 취득 *전*(lock 밖)에 호출하므로 lock 없는 point-read 로 장부
    파일(`LEASES_FILE`)을 직접 읽는다 — `_read_ledger`(lock 보유 전제)와 별도. 리스는
    atomic-replace(`os.replace`)로 쓰므로 lock 없는 read 도 일관 스냅샷을 본다(board.py
    `_leased_sessions` 와 *동형*). 장부 부재/파싱실패/손상은 빈 리스트(fail-soft). session 이
    빈/None 인 행은 제외.
    """
    if not LEASES_FILE.exists():
        return []
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("leases", [])
    if not isinstance(rows, list):
        return []
    sessions: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == "leased":
            sess = row.get("session")
            if sess:
                sessions.append(sess)
    return sessions


def _default_session() -> str:
    """세션 식별자 기본값 — board.py `session_name()` 과 *동형* 우선순위 (ADR-0040 D1·T-0073):
    `$PM_SESSION_NAME` env > `$CLAUDE_SESSION_NAME` env(deprecated alias·silent) >
    lease 장부 state=="leased" 행이 정확히 1개면 그 session (단일-lease 유도) >
    (장부 부재·leased 0 = solo) `local.conf session=` > `<host>-<pid>`.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias
    (둘 다면 PM 승·조용히 동작). **leased ≥2 (모호)면 local.conf 층을 건너뛰고 `<host>-<pid>` 로
    간다**(board.session_name 과 동형 — 저장 쪽지로 남의 세션 행세 차단·ADR-0040).

    board.session_name 과 **tail 만 다르다**: 여기는 lease *취득*의 국소 임시 명명이라 미해소를
    None/fail 로 두지 않고 `<host>-<pid>` 로 폴백한다(ADR-0040 — host-pid 최종 폴백은 세션-귀속
    아닌 국소 용처에만 잔존). board.py 를 import 하지 않으므로([[ADR-0013]] isolation·touches
    격리·병렬충돌 회피) 같은 해소를 자체 구현한다. 저장측(여기)과 매칭측(board.session_name)이
    어긋나면 per-slot test_cmd 가 미스되므로(T-0066 must-fix) 세 모듈을 같은 우선순위로 통일한다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    leased = _leased_sessions()
    if len(leased) == 1:
        return leased[0]
    if not leased:
        # 장부 부재·leased 0 = solo → legacy local.conf 폴백 (후방호환).
        conf_sess = _local_conf_session()
        if conf_sess:
            return conf_sess
    # leased ≥2 (모호) 또는 solo 무바인딩 → 국소 임시 명명 host-pid (lease 취득 전용 잔존).
    import socket
    return f"{socket.gethostname()}-{os.getpid()}"


# ── 운영중 관리 backbone: dev/sync + CLI (ADR-0049/0051 파일럿 T-γ·T-0277) ─────────
# worktree/submodule 운영 *중* 관리를 명령어化 하기 위한 두 backbone + argparse 진입점.
# pm-worktree 스킬(어댑터·PM authoring)이 이 커맨드를 얇게 래핑한다 — 백본 로직은 전부 여기.
#   - dev <sub> <branch> : 슬롯 worktree 의 submodule 을 dev 브랜치로 지정(on-branch 화) → 이후
#       selective resync(`_resync_submodules_selective`)가 그 submodule 을 dev 로 판별해 skip
#       (detached pin 으로 낚아채지 않음·ADR-0051 §D1 live-HEAD 모델). "내가 작업 중" 선언.
#   - sync              : 현재 슬롯의 `_resync_submodules_selective` 를 수동 트리거(브랜치 전환
#       없이 명시 재동기·detached=pin 재동기·on-branch=skip·dirty=skip+경고). T-0275 백본 공유.
# 얇게 — 기존 primitive(`_resync_submodules_selective`·submodule 판별)를 재사용한다(중복 금지).


def dev(slot: str, sub: str, branch: str, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 submodule 을 dev 브랜치로 지정한다 — on-branch 화 (ADR-0051 §D1·T-0277).

    `git -C <sub> checkout -b <branch>`(신규 생성)로 submodule 을 on-branch 로 만든다. 브랜치가
    이미 있으면 `-b` 가 rc≠0 이므로 `git -C <sub> checkout <branch>`(전환)로 폴백한다(`_checkout`
    의 create-or-switch 정신과 정합·단 submodule 컨텍스트). 결과적으로 그 submodule 의 live git
    HEAD 가 symbolic ref(on-branch)가 되어, 이후 `_resync_submodules_selective`(T-0275)가 그
    submodule 을 **dev 역할로 판별해 skip** 한다(detached pin 으로 낚아채지 않음·ADR-0051 크럭스 A)
    — 즉 "이 submodule 은 내가 작업 중이니 pin 재동기로 건드리지 마" 선언(무스키마·역할은 별도
    장부 없이 submodule HEAD 로 정함·ADR-0051 §Decision 1).

    **`sub` 슬롯-경계 검증(codex must-fix·T-0277)**: `dev` 는 실 git side-effect(`checkout`)를
    caller-공급 경로에 낸다. 이 도구는 LLM-구동 pm-internal 이라 `sub` 가 자연어에서 구성된다
    (hallucination/오타) — 절대경로(`/etc/...`)·`..` traversal·목록 밖 경로를 그대로 `git -C <sub>
    checkout` 에 넘기면 **슬롯 밖 다른 git repo 에 checkout** 을 실행해 "대상 슬롯 운영" 경계를
    깬다. 그래서 checkout 전에 `sub` 를 그 슬롯의 실제 submodule 목록(`git submodule status` →
    `_parse_submodule_paths`)과 **대조(allowlist)** 하고, 목록 밖이면 `SubmoduleNotInSlot` raise
    (fail-closed — status 조회 실패/submodule 없음도 빈 목록 → 거부). allowlist 라 절대경로·
    traversal 은 자동 거부(목록의 값은 항상 슬롯-상대 submodule 경로).

    `sub` 는 **슬롯 worktree 상대 submodule 경로**(`git submodule status` 가 나열하는 경로·예
    `vendor/sub`). runner 는 슬롯 worktree 바인딩(`_real_git_runner(slot_path(slot))`) + 다중 `-C`
    (`["-C", sub, ...]`)로 superproject cwd 기준 submodule 에 재진입한다(`_submodule_dirty`·
    `_resync_submodules_selective` 와 동형 배선). `(rc, out)` 반환 — 호출부(CLI)가 rc≠0 를 명시
    에러로 surface 한다. `git_runner` 주입 시 그 runner(테스트 mock·DI seam 보존).
    """
    runner = git_runner or _real_git_runner(slot_path(slot))
    # 슬롯 경계 검증(must-fix 1) — sub 를 슬롯 실제 submodule 목록과 대조(allowlist·fail-closed).
    rc_st, out_st = runner(["submodule", "status"])
    known = _parse_submodule_paths(out_st) if rc_st == 0 else []
    if sub not in known:
        raise SubmoduleNotInSlot(slot, sub, known)
    rc, out = runner(["-C", sub, "checkout", "-b", branch])
    if rc != 0:
        # `-b` 실패 원인 판별(codex bundle) — 브랜치가 *이미 존재*할 때만 전환 폴백한다. 그 외
        # (충돌/lock/잘못된 브랜치명)의 `-b` 실패까지 폴백하면 진단이 흐려지므로, `show-ref
        # --verify --quiet refs/heads/<branch>` rc0(존재)일 때만 plain `checkout <branch>` 로
        # 전환하고, 미존재(rc≠0)면 원 `-b` 실패(rc/out)를 그대로 전파한다(원인 명확).
        rc_ref, _out_ref = runner(
            ["-C", sub, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        )
        if rc_ref == 0:
            rc, out = runner(["-C", sub, "checkout", branch])
    return rc, out


def sync(slot: str, *, git_runner: GitRunner | None = None) -> None:
    """현재 슬롯의 submodule 을 superproject pin 에 selective 재동기(수동 트리거) — (ADR-0051·T-0277).

    T-0275 의 `_resync_submodules_selective` 를 **브랜치 전환 없이** 수동으로 부른다 — 브랜치를
    바꾸지 않고 명시적으로 submodule 상태를 pin 에 맞추고 싶을 때의 진입(부트스트랩/checkout 경로
    밖). 판별·거동은 전부 그 backbone 이 소유한다(중복 구현 금지·얇은 트리거):
      - detached(consume) & clean → `git submodule update --init --recursive --force -- <sub>` 로 pin 재동기.
      - on-branch(dev) → skip(dev 작업 보호·크럭스 A).
      - detached & dirty → skip + 경고(미커밋 작업 유실 방지).
      - submodule 없는 슬롯 → no-op.
    fail-soft(raise 금지·경고는 stderr) — `_resync_submodules_selective` 계약 상속. `git_runner`
    주입 시 그 runner(테스트 mock·DI seam), 미주입이면 슬롯 worktree 바인딩 실 runner.
    """
    _resync_submodules_selective(slot_path(slot), git_runner=git_runner)


class SlotResolutionError(RuntimeError):
    """CLI dev/sync 의 현재-슬롯 자동해소 실패 — 매칭 leased 슬롯 0개/≥2(모호) 또는 슬롯 형식 오류.

    메시지는 세션명 + 후보 슬롯(또는 형식 위반) + `--slot` 안내를 담는다(호출부 main 이 그대로
    stderr surface). (`RuntimeError` 서브클래스 — CLI main 이 `except SlotResolutionError` 로 rc 1.)
    """


class SubmoduleNotInSlot(ValueError):
    """dev 대상 submodule 이 슬롯의 실제 submodule 목록에 없다 — 슬롯 경계 밖 checkout 차단 (must-fix·T-0277).

    LLM-구동 pm-internal 도구가 실 checkout side-effect 를 caller-공급 경로에 내므로, 목록 밖
    경로(절대경로·`..` traversal·오타)를 fail-closed 로 거부한다. `known` = 검증에 쓴 슬롯의
    실제 submodule 경로 목록(진단용·CLI main 이 메시지에 surface).
    """

    def __init__(self, slot: str, sub: str, known: "list[str]"):
        self.slot = slot
        self.sub = sub
        self.known = known
        super().__init__(
            f"submodule {sub!r} 이 슬롯 {slot} 의 submodule 목록에 없다 "
            f"(등록: {known if known else '(없음/조회 실패)'}) — 슬롯 밖 경로·오타 거부(경계 보호)."
        )


# 슬롯 식별자 형식 — `work/<repo>_<N>`(<repo>=선두 alnum 슬러그·<N>=숫자). 앵커 + `/` 불허라
# `slot_path` 결합이 슬롯 루트를 벗어날 수 없다(traversal·빈값·`work/` 단독 거부·must-fix 2).
_SLOT_ID_RE = re.compile(r"^work/[A-Za-z0-9][A-Za-z0-9_.-]*_\d+$")


def _normalize_slot(slot_arg: str) -> str:
    """`--slot` 값을 슬롯 식별자 정규형(`work/<repo>_<N>`)으로 정규화 + 형식 검증 (must-fix 2·T-0277).

    스킬/사용자가 `work/A_1`(정규형) 또는 `A_1`(접두 생략) 어느 쪽으로 줘도 받는다 —
    `slot_path`/장부의 슬롯 식별자 관례(`work/<repo>_<N>`·`_slot_for`)와 정합. **`slot_path` 로
    `REPO / slot` 결합하기 전에** `_SLOT_ID_RE` 로 형식을 검증한다 — caller-공급 문자열이라
    `../x`·`work/../x`·빈값·`work/` 단독 같은 traversal/부적격 값이 슬롯 루트 밖을 가리키면
    `SlotResolutionError` 로 중단한다(side-effect 를 슬롯 경계 안에 가둠).
    """
    s = slot_arg.strip()
    slot = s if s.startswith("work/") else f"work/{s}"
    if not _SLOT_ID_RE.match(slot):
        raise SlotResolutionError(
            f"슬롯 식별자 {slot_arg!r} 형식 오류 — `work/<repo>_<N>`(또는 접두 생략 `<repo>_<N>`) "
            "형식만 허용한다(traversal·빈값·`work/` 단독·경로구분자 거부·슬롯 경계 보호)."
        )
    return slot


def _slot_from_cwd() -> str | None:
    """cwd 가 `<WORK_DIR>/<repo>_<N>[/...]` 안이면 그 슬롯 식별자(`work/<repo>_<N>`), 아니면 None.

    슬롯 worktree(또는 그 하위 submodule) cwd 에서 커맨드를 부르면 정체성 인자 없이도 그 슬롯을
    타깃하게 한다(pm_bootstrap `_worktree_cwd`/`_auto_slot` 의 cwd-유입 정신과 정합). WORK_DIR 밖
    (예: multi-PM 루트)이면 None(→ session 해소로 폴백). 경로 해석 실패는 None(fail-soft).
    """
    try:
        rel = Path(os.getcwd()).resolve().relative_to(WORK_DIR.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    return f"work/{parts[0]}" if parts else None


def _resolve_current_slot(slot_arg: str | None) -> str:
    """dev/sync 의 대상 슬롯을 해소한다 — `--slot` > cwd > 세션 leased (T-0277).

    우선순위(ticket: `--slot <name>` 인자 또는 cwd/local.conf/env):
      1. `--slot <name>` 명시 → `_normalize_slot`(스킬이 정체성 명시 전달·권장 경로).
      2. cwd 가 슬롯 worktree 안이면 그 슬롯(`_slot_from_cwd`).
      3. 세션(`_default_session`·env/local.conf 유입)이 보유한 leased 슬롯 — 정확히 1개면 그것.

    3에서 매칭 leased 슬롯이 0개(무바인딩)이거나 ≥2(모호)면 `SlotResolutionError` raise — CLI
    main 이 rc 1 + `--slot` 안내로 surface 한다(침묵 오타깃 금지). `list_leases`(flock read)·
    `_default_session`(board.session_name 동형)을 재사용한다(기존 관례).

    **형식 검증(must-fix 2·T-0277)**: `--slot` 명시는 `_normalize_slot` 이, 그 외 유입도 반환 전
    `_SLOT_ID_RE`(`_normalize_slot` 재적용)로 최종 슬롯이 `work/<repo>_<N>` 형식임을 강제한다 —
    어느 source(--slot/cwd/session)든 부적격/traversal 값이 `slot_path` 결합으로 슬롯 경계를
    벗어나지 못하게 하는 단일 불변식. `--slot` 은 **빈 문자열도 명시로 취급**(`is not None`)해
    형식 검증에 태운다(빈값 거부).
    """
    if slot_arg is not None:
        return _normalize_slot(slot_arg)  # 명시(빈값 포함) → 정규화 + 형식 검증.
    cwd_slot = _slot_from_cwd()
    if cwd_slot:
        return _normalize_slot(cwd_slot)  # cwd 유입도 최종 형식 검증(단일 불변식).
    sess = _default_session()
    mine = [l for l in list_leases() if l.state == "leased" and l.session == sess]
    if len(mine) == 1:
        return _normalize_slot(mine[0].slot)  # 장부 유입도 최종 형식 검증(단일 불변식).
    if not mine:
        raise SlotResolutionError(
            f"세션 {sess!r} 의 leased 슬롯이 없다 — `--slot work/<repo>_<N>` 으로 대상 슬롯을 명시하라."
        )
    raise SlotResolutionError(
        f"세션 {sess!r} 의 leased 슬롯이 {len(mine)}개"
        f"({', '.join(l.slot for l in mine)}) — `--slot` 으로 어느 슬롯인지 명시하라."
    )


def main(argv: "list[str] | None" = None) -> int:
    """argparse 진입점 — pm-worktree 스킬이 래핑할 `dev`/`sync` 커맨드 (ADR-0049 파일럿 T-γ·T-0277).

    라이브러리 모듈에 얇은 CLI 를 얹는다(`if __name__ == "__main__"` 가드로 import 안전 — 다른
    도구의 `spec_from_file_location` import 를 안 깬다). 스킬이
    `python3 .project_manager/tools/worktree_pool.py dev <sub> <branch> [--slot ...]` /
    `... worktree_pool.py sync [--slot ...]` 로 부른다. CLI 는 **실경로 wiring**(git_runner 미주입)
    이고, 함수 레벨 DI seam(`dev`/`sync`/`_resync_submodules_selective` 의 git_runner)은 테스트가
    쓴다. 사람이 읽는 stdout(무엇을 했는지)·skip/경고 사유는 backbone 이 stderr·실패는 rc 1 + 메시지.
    """
    parser = argparse.ArgumentParser(
        prog="worktree_pool.py",
        description="worktree/submodule 운영중 관리 backbone (dev/sync·ADR-0049/0051 파일럿 T-γ).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_dev = subparsers.add_parser(
        "dev", help="submodule 을 dev 브랜치로 지정(on-branch 화 → selective resync 가 skip).")
    p_dev.add_argument("submodule", help="슬롯 worktree 상대 submodule 경로(예 vendor/sub).")
    p_dev.add_argument("branch", help="지정할 dev 브랜치명(없으면 생성·있으면 전환).")
    p_dev.add_argument(
        "--slot", default=None,
        help="대상 슬롯 work/<repo>_<N> (미지정=cwd/세션 자동해소).")

    p_sync = subparsers.add_parser(
        "sync", help="현재 슬롯 submodule 을 pin 에 selective 재동기(수동 트리거).")
    p_sync.add_argument(
        "--slot", default=None,
        help="대상 슬롯 work/<repo>_<N> (미지정=cwd/세션 자동해소).")

    args = parser.parse_args(argv)

    try:
        slot = _resolve_current_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1

    if args.command == "dev":
        try:
            rc, out = dev(slot, args.submodule, args.branch)
        except SubmoduleNotInSlot as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        if rc != 0:
            print(
                f"[중단] 슬롯 {slot} · submodule {args.submodule!r} 을 dev 브랜치 "
                f"{args.branch!r} 로 지정 실패 (rc={rc}): {str(out).strip()[:200]}",
                file=sys.stderr,
            )
            return 1
        print(
            f"✓ 슬롯 {slot} · submodule {args.submodule} → dev 브랜치 {args.branch!r} "
            "(on-branch·이후 selective resync 가 이 submodule 을 skip)."
        )
        return 0

    # args.command == "sync"
    print(
        f"# 슬롯 {slot} submodule selective 재동기 "
        "(detached=pin 재동기·on-branch=skip·dirty=skip+경고)"
    )
    sync(slot)
    print(f"✓ 슬롯 {slot} 재동기 완료 (skip/경고 사유는 위 stderr 참조).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
