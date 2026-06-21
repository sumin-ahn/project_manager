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

import contextlib
import datetime
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·pm_*.py 와 동일 앵커 규약(sealed spike §8-4).
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
    이 bare 가 없으면 `git worktree add` 의 base 가 없다 — `pm-config repo add <repo>` 가
    bare clone 을 먼저 만들어야 한다([[T-0061]]). 침묵 폴백으로 multi-PM 루트 자신의 worktree 를
    만들면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가 깨진다([[ADR-0013]]
    fail-soft 규율) → 명시 raise 로 선행 명령을 안내한다.

    **`RuntimeError` 서브클래스**인 이유: 파사드 `pm_config.cmd_worktree_add` 가
    `create_slot` 의 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로 surface 한다
    ([[T-0061]]). 베이스를 `Exception` 으로 두면 그 가드를 빠져나가 traceback 이 노출된다
    (cross-module 계약 — codex T-0063 게이트 포착).
    """

    def __init__(self, repo: str, bare_path: "Path"):
        self.repo = repo
        self.bare_path = bare_path
        super().__init__(
            f"bare repo for {repo!r} not found at {str(bare_path)!r} — "
            f"run `pm-config repo add {repo}` first (ADR-0011 §31)"
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
    <cwd> <argv...>` 형태로 항상 그 work tree/repo 에 묶는다. 인코딩은 엔진 규약대로 UTF-8
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
    같은 규약([[T-0061]]) — worktree 풀이 import 격리(board·pm_config 미import)라 자체 해소한다.
    """
    return REPOS_DIR / f"{repo}.git"


# ── 보호 브랜치 pre-push 훅 (T-0076·하드·회사 repo 무영향) ────────────────────
# 훅 = `.project_manager/.local/repo-hooks/<repo>/pre-push`(프레임워크 소유·gitignore).
# bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리로 set → 슬롯 push 가 이 훅에
# 게이트된다. **회사 repo 서버/사용자 클론 무변경** — client-side·우리 bare 미러 config 1줄만.
#
# 훅은 *generic* 이다 — 보호목록을 sidecar 파일(`protected`·줄당 1브랜치·훅과 같은 디렉토리)
# 에서 읽는다. 설치(install_protected_hook)가 그 sidecar 를 채우므로(목록 변경 = 재설치로
# 갱신), 훅 본문 자체는 repo 무관하게 동일하다. POSIX sh — Windows git 번들 sh 로도 동작.
#
# 로직: stdin 의 `<localref> <localsha> <remoteref> <remotesha>` 줄들에서 remote ref
# (`refs/heads/<b>`)의 `<b>` 가 sidecar 보호목록에 있으면 거부(echo 안내·exit 1).
# `PM_ALLOW_PROTECTED_PUSH=1` 이면 통과(사용자 명시 OK). feature 브랜치 push 는 통과(exit 0).
_PROTECTED_PRE_PUSH_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-push 가드 (T-0076) — PM 이 보호 브랜치(main 등)에 자율 push 못 하게.
# install_protected_hook() 가 설치. 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치).
if [ "$PM_ALLOW_PROTECTED_PUSH" = "1" ]; then
    exit 0
fi
hook_dir=$(dirname "$0")
protected_file="$hook_dir/protected"
[ -f "$protected_file" ] || exit 0
while read -r _local_ref _local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
        refs/heads/*) branch=${remote_ref#refs/heads/} ;;
        *) continue ;;
    esac
    while IFS= read -r protected_branch; do
        [ -n "$protected_branch" ] || continue
        if [ "$branch" = "$protected_branch" ]; then
            echo "[pm 보호 가드] 보호 브랜치 '$branch' 로의 push 거부 (T-0076)." >&2
            echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — feature 브랜치로 작업하고" >&2
            echo "  main 갱신은 사용자에게 맡긴다(PR/머지). 사용자 명시 OK 면:" >&2
            echo "    PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
            exit 1
        fi
    done < "$protected_file"
done
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
      2. `pre-push` 훅(generic·POSIX sh·LF)과 `protected` sidecar(보호목록·줄당 1브랜치) write.
         목록이 바뀌면 재설치가 sidecar 를 덮어 갱신한다(훅 본문은 불변).
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

    # 3) bare core.hooksPath wiring (절대경로) — client-side·우리 미러 config 1줄.
    # **rc 검사(codex T-0076)**: config 실패면 훅이 실제로 wiring 안 됐는데 성공 보고하면 보호
    # 가드가 *침묵 무력화* 된다(하드 차단 계약 위반). rc≠0 → False 반환(호출부가 경고 surface).
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
           - `base` 면(branch 미지정) 슬롯 브랜치 `<repo>_<N>` 를 *그 base 에서 파생*
             (`-b <repo>_<N> <path> <base>`·T-0075). repo 등록 base(areas.md·`pm-config worktree
             add` 가 전달)에서 일관되게 따게 한다 — bare HEAD 가 아닌 의도한 base(develop 등).
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
    # 부재 가드는 *경로 존재*에 대한 계약이지 실 git 호출이 아니므로 mock 모드에서도 유효.
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
        #   - base 면(branch 미지정·T-0075) 슬롯 브랜치 `<repo>_<N>` 를 *그 base 에서 파생*
        #     (`-b <slot> <path> <base>`). 슬롯 브랜치 이름은 슬롯 식별자(`<repo>_<N>`·T-0072
        #     live-branch 정합)이고 base 만 의도한 분기점(develop 등). `add <path> <base>` 가
        #     아니라 `-b`(브랜치 생성)인 이유: ref 만 주면 detached 거나 base 브랜치 자체에
        #     붙어 슬롯 작업이 base 를 오염한다 → 슬롯 전용 브랜치를 base 에서 새로 판다.
        #   - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
        add_runner = git_runner or _real_git_runner(bare)
        if branch is not None:
            add_argv = ["worktree", "add", "-B", branch, str(path)]
        elif base is not None:
            add_argv = ["worktree", "add", "-b", f"{repo}_{n}", str(path), base]
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
        # 차단(기존 계약). 단 worktree add 는 *이미 성공*했으므로 raise 전에 그 worktree 를
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
    주입 runner 가 예외를 던져도 None 으로 흡수한다(docstring 의 "raise 금지" 계약을 DI seam
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
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 None(계약: raise 금지).
        return None
    if rc != 0:  # detached(symbolic ref 아님)·git 부재/실패 → 브랜치 없음.
        return None
    return out.strip() or None


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

    `git checkout <branch>`. 브랜치가 없으면 새로 만든다(`-B`) — 풀 슬롯에 새 작업스트림을
    붙이는 정상 경로. (같은 브랜치 동시 2-worktree checkout 은 git 이 거부 — ADR-0013 §8-6.)
    """
    runner = git_runner or _real_git_runner(slot_path_)
    rc, out = runner(["checkout", branch])
    if rc != 0:
        rc, out = runner(["checkout", "-B", branch])
    return rc, out


def _checkout_required(slot: str, branch: str, *, git_runner: GitRunner | None = None) -> None:
    """`_checkout` 을 부르고 실패(rc≠0)면 `CheckoutFailed` raise (ADR-0013).

    fail-soft 로 무시하면 호출부가 장부 branch/state 를 성공처럼 갱신해 장부↔실제 worktree
    branch 가 어긋난다. 성공해야만 호출부가 장부를 갱신하도록 강제하는 가드.
    """
    rc, out = _checkout(slot_path(slot), branch, git_runner=git_runner)
    if rc != 0:
        raise CheckoutFailed(slot, branch, out)


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


def _default_session() -> str:
    """세션 식별자 기본값 — board.py `session_name()` 과 *동형* 우선순위 (T-0073):
    `$PM_SESSION_NAME` env > `$CLAUDE_SESSION_NAME` env(deprecated alias·silent) >
    `local.conf session=` > `<host>-<pid>`.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관). `CLAUDE_SESSION_NAME` 은 구 이름
    alias 만 — 둘 다 설정 시 `PM_SESSION_NAME` 승. alias 는 경고 없이 조용히 동작.

    board.py 를 import 하지 않으므로([[ADR-0013]] isolation·touches 격리·병렬충돌 회피)
    같은 해소를 자체 구현한다. local.conf `session=` 레이어가 빠지면 저장측(여기)과
    매칭측(board.session_name)이 어긋나 per-slot test_cmd 가 미스된다(T-0066 must-fix) —
    그래서 세 모듈을 같은 우선순위로 통일한다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    conf_sess = _local_conf_session()
    if conf_sess:
        return conf_sess
    import socket
    return f"{socket.gethostname()}-{os.getpid()}"
