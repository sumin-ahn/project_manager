"""슬롯·task 자료형, 예외와 순수 판정 규칙."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from pm_engine.domain import identity_args


# git CLI argv → (returncode, stdout). DI seam 의 타입(pm_import.GitRunner 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


class NeedsCreate(Exception):
    """풀 소진 — idle 슬롯이 없어 새 슬롯 생성이 필요하다.

    `git worktree add` 는 fs 행위라 자동으로 하지 않는다. 호출부 `pm-config alloc` 이 이 신호를
    받아 사용자 승인 명령(`worktree add <repo> --task <task> --user-ack <repo>`)을 안내한다.
    """

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"worktree pool exhausted for repo {repo!r} — needs `git worktree add`")


class ReleaseRefused(Exception):
    """커밋 안 한 변경이 있는 슬롯을 반납하려 함 — `release`·`force_release` 모두 거부한다.

    엔진은 stash 하지 않는다. 사용자가 커밋하거나, 사용자가 요청할 때만 stash 한 뒤 다시 반납한다.
    """

    def __init__(self, slot: str):
        self.slot = slot
        super().__init__(f"refusing to release slot {slot!r} — uncommitted changes")


class RemoveRefused(Exception):
    """remove_slot 거부 — 활성 리스·커밋 안 한 변경·git 미등록 잔재 트리 (사용 중 슬롯·작업 유실 보호).

    `reason` ∈ {"active-lease", "dirty", "unregistered-tree"}. `force=True` 가 무시하는 것은
    "active-lease"(점유) 하나뿐이다. 커밋 안 한 변경은 사용자가 커밋(또는 요청한 stash)으로 치운다.
    git 이 등록하지 않는 트리는 커밋 안 한 변경을 물을 자리가 없어 엔진이 지우지 않는다 — 사용자가
    `path` 를 직접 확인·삭제한다. `state` = active-lease 시 슬롯 상태(leased/creating·진단용).
    """

    def __init__(self, slot: str, reason: str, *, state: "str | None" = None,
                 path: "Path | None" = None):
        self.slot = slot
        self.reason = reason      # "dirty" | "active-lease" | "unregistered-tree"
        self.state = state        # active-lease 시 슬롯 state (leased/creating)
        self.path = path          # unregistered-tree 시 사용자가 확인할 트리 경로
        detail = f" (state={state})" if state else ""
        where = f" (path={path})" if path else ""
        super().__init__(f"refusing to remove slot {slot!r} — {reason}{detail}{where}")


class TaskActiveElsewhere(Exception):
    """같은 task 를 살아있는 다른 세션이 열고 있다 — 동시 세션 거부.

    task 열림 기록 검사(부트스트랩 진입 시): 장부 task 행의 `session` 이 다른 세션
    ID 면(열림 기록이 있고 종료 기록이 없음) 이 예외로 거부한다. 프로세스 조회는 하지 않는다 — 번호 재사용·세션 교체로
    생존을 알 수 없어서다. 해소는 사용자 행위 둘 중 하나다: 그 창을 쓰거나, 기록을 닫거나
    (`pm-config task close <task>`), 강제로 이어받는다(부트스트랩 `--force`).
    `started` = 그 기록의 시작 시각(정체 표시용).
    """

    def __init__(self, name: str, started: str = ""):
        self.name = name
        self.started = started
        detail = f" (task 시작: {started})" if started else ""
        super().__init__(
            f"task {name!r} 에 열린 세션 기록이 있습니다{detail}"
        )


class InvalidTaskName(Exception):
    """task 명이 안전한 단일 path 컴포넌트가 아니거나 예약 패턴이다 — fail-loud (must-fix).

    `task_dir(name)=TASKS_DIR/name` 이 무검증이면 `--task ../../evil`·`/tmp/x`·`a/b`·빈 문자열이
    작업트리 밖/임의 경로에 디렉토리를 만들고 장부를 오염시킨다(reviewer 실측). 검증은 **엔진층
    (bind_task 진입점)**에 둔다 — CLI 검증만으론 bind_task 직접 소비가 우회된다.
    거부: 빈/공백·path separator(`/`·`\\`)·선행 `.`(숨김/상대 traversal)·단일 컴포넌트 아님·(등록
    repo 넘기면) `<repo>_<N>` 슬롯 폴더 이름 패턴 예약. `reason` = 위반 사유(진단용).
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"부적합 task 명 {name!r} — {reason}")


class TaskArchived(Exception):
    """동명 종료 archive가 있어 신규 task 생성을 거부한다."""

    def __init__(self, name: str, archives: "tuple[Path, ...]"):
        self.name = name
        self.archives = archives
        names = ", ".join(path.name for path in archives)
        super().__init__(
            f"task {name!r} 은(는) 종료 archive로 보관됨 ({names}) — task reopen 필요"
        )


class TaskReopenRefused(Exception):
    """task archive 복원을 안전하게 완료할 수 없어 거부한다."""

    def __init__(self, name: str, reason: str, *,
                 archives: "tuple[Path, ...]" = (),
                 moved_from: "Path | None" = None,
                 moved_to: "Path | None" = None):
        self.name = name
        self.reason = reason
        self.archives = archives
        self.moved_from = moved_from
        self.moved_to = moved_to
        paths = ""
        if moved_from is not None or moved_to is not None:
            paths = f" (archive={moved_from}, active={moved_to})"
        super().__init__(f"task {name!r} reopen 거부 — {reason}{paths}")


class TaskEndRefused(Exception):
    """등록 task의 durable handoff intent가 없어 종료를 거부한다."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"task {name!r} 종료 거부 — {reason}")


class NotTaskOwner(Exception):
    """release `--task <task>` 이 그 task 명의가 아닌 슬롯을 반납하려 함 — 소유검사 거부.

    슬롯↔task 연결 = lease.session == task 이름 (alloc `--task` 가 session=task 로 리스).
    `--task` 를 준 release 는 이 소유검사를 통과해야 반납한다 — 다른 task/세션이 쓰는 슬롯을
    실수로 idle 화(작업 뺏기)하지 않게. `holder` = 실제 그 슬롯을 leased 중인 session(진단용·
    빈 문자열이면 idle/미점유). slot-only 반납(`--task` 미지정·백스톱)은 이 검사를 안 탄다.
    """

    def __init__(self, slot: str, task: str, holder: str):
        self.slot = slot
        self.task = task
        self.holder = holder
        held = f"session {holder!r}" if holder else "미점유(idle)"
        super().__init__(
            f"슬롯 {slot!r} 은 task {task!r} 소유가 아님 — {held} 이(가) 보유 중"
        )


class CheckoutFailed(Exception):
    """슬롯 worktree 의 branch checkout 실패.

    fail-soft 로 무시하면 리스 장부의 state/session 이 실제 worktree 상태와 어긋난다
    (부분 leased 전이). alloc 은 checkout 성공 시에만 장부를 갱신하고, 실패하면 이를
    raise 해 기존 리스 상태를 보존한다(부분 갱신 차단). 브랜치 자체는 더는 장부에
    저장하지 않는다(git=진실) — checkout 은 git HEAD 를 바꾼다.
    """

    def __init__(self, slot: str, branch: str, output: str):
        self.slot = slot
        self.branch = branch
        self.output = output
        super().__init__(
            f"git checkout {branch!r} failed for slot {slot!r}: {output!r}"
        )


class BareRepoMissing(RuntimeError):
    """worktree 의 공유 .git 원(`.repos/<repo>.git` bare)이 없다.

    `.repos/<repo>.git` 가 worktree 슬롯의 공유 .git 원(canonical).
    이 bare mirror 가 없으면 `git worktree add` 의 base 가 없다 — `pm-config repo add <repo>` 가
    bare clone 으로 mirror 를 (재)생성해야 한다. **repo 가 areas.md 에 이미
    등록됐으면**(하나의 채택 폴더를 여러 사람이 clone 한 2번째 사용자·`.repos/` 는 gitignore·
    per-clone 이라 공유 안 됨) `pm-config repo add <repo>` 를 **`--git` 없이** 실행하면 areas
    등록 URL 로 mirror 를 hydrate 한다. 침묵 폴백으로 multi-PM 루트 자신의 worktree 를
    만들면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가 깨진다
    fail-soft 규율) → 명시 raise 로 선행 명령을 안내한다.

    **`RuntimeError` 서브클래스**인 이유: 파사드 `pm_config.cmd_worktree_add` 가
    `create_slot` 의 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로 surface 한다
    베이스를 `Exception` 으로 두면 그 가드를 빠져나가 traceback 이 노출된다
    (cross-module 규격).
    """

    def __init__(self, repo: str, bare_path: "Path", *, broken: bool = False):
        self.repo = repo
        self.bare_path = bare_path
        # broken=True = 경로는 있으나 유효 bare 가 아님(부분/깨진 bare) — 부재와 구별해
        # exists-but-broken 진단을 부재 케이스 수준으로 안내한다. broken=False = 종전 경로부재.
        self.broken = broken
        if broken:
            super().__init__(
                f"bare mirror for {repo!r} at {str(bare_path)!r} exists but is not a valid bare "
                f"git repo — 부분/깨진 bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/"
                f"Ctrl-C). `.repos/{repo}.git` 경로는 있으나 `git worktree add` 의 base 로 "
                f"못 써 나중 날 git 에러로 죽는다. 자동 삭제는 하지 않는다(사용자 데이터 오판 위험·"
                f"삭제는 사용자 위임) — `.repos/{repo}.git` 를 수동 삭제 후 "
                f"`pm-config repo add {repo}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
                f"`--git <url>`)로 재생성하라 "
            )
        else:
            super().__init__(
                f"bare mirror for {repo!r} not found at {str(bare_path)!r} — "
                f"`.repos/{repo}.git`(worktree 공유 .git 원)가 없다. areas.md 에 이미 등록됐으면 "
                f"(multi-user: `.repos/` 는 gitignore·per-clone 이라 공유 안 됨) "
                f"`pm-config repo add {repo}`(--git 불요)가 areas 등록 URL 로 mirror 를 hydrate 한다; "
                f"미등록 신규 repo 면 `pm-config repo add {repo} --git <url>` "
            )


class SlotBranchExists(RuntimeError):
    """create_slot 이 파려는 슬롯 전용 브랜치 `<repo>_<N>` 가 이미 존재한다 — 미머지-보존 브랜치 충돌.

    `remove_slot` 가 **미머지 전용 브랜치**(`<repo>_<N>`)를 보존(작업 유실 방지)한 뒤, 같은 번호
    슬롯을 재생성하면 `git worktree add` 가 `fatal: a branch named '<repo>_<N>' already exists`
    (rc≠0)로 죽는다. 작업 슬롯 생성은 슬롯 전용 브랜치를 `--no-track -b <repo>_<N> <path>
    origin/<base>` 로 판다. 슬롯번호는 ledger∪git-worktree
    병합으로 회피하지만, 그 병합은 **worktree 없이 잔존하는 브랜치**(보존 브랜치는 worktree 를
    안 가짐·브랜치 축은 슬롯번호 축과 독립)를 못 본다.

    옛 동작은 이 실패를 create_slot 의 already-exists 진단이 "worktree 경로 이미 등록(orphan)"으로
    **오귀인**했다(reviewer 실측 — orphan 정리 안내를 냈지만 지울 orphan worktree 는 없다).
    이 예외가 그 오귀인을 정정한다: **정확한 원인(브랜치 잔존) + 두 갈래 선택**(브랜치 정리 후 새 슬롯
    재생성 / 그 브랜치를 checkout 해 재개)을 fail-loud 로 준다(결정 (b)·데이터 유실 없음 — 현재도 loud 실패였다).

    **왜 (a) 브랜치 재사용-체크아웃이 아니라 (b) fail-loud 인가**: base-경로는 슬롯을 *`base`(origin/
    <base> 최신)에서* 시작하도록 요청한 것이다. 보존 브랜치는 정의상 `base` 에 없는 커밋을 가져
    미머지된 것이라(그래서 remove_slot 가 보존했다) 그 브랜치를 *base-경로에서* 재사용하면 슬롯이
    요청한 base 가 아닌 옛 미머지 작업 위에서 **조용히** 시작한다(base ≠ 브랜치 HEAD·silent 시맨틱
    어긋남). 사용자가 옛 작업 재개를 모른 채 진행할 위험이라 — 재개는 **명시 의사로만**(그 브랜치를
    슬롯에 checkout) 열어 둔다. 이 코드베이스의 fail-loud 규율(`BareRepoMissing`·`RemoveRefused`)과 정합.

    ✅ **재개 경로는 리셋 없이 안전**: 그 브랜치를 checkout 해 미머지 작업을 이어가려면 다른
    슬롯에서 `switch <slot> <repo>_<N>`(기존 브랜치 전환·리셋 없음)한다.

    **`RuntimeError` 서브클래스**인 이유: `BareRepoMissing` 과 동형 — 파사드 `pm_config.
    cmd_worktree_add` 가 `create_slot` 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로
    surface 한다. `base` = 요청 base.
    """

    def __init__(self, slot: str, branch: str, base: str):
        self.slot = slot
        self.branch = branch
        self.base = base
        super().__init__(
            f"슬롯 전용 브랜치 {branch!r} 가 이미 존재한다 — base {base!r} 에서 슬롯 {slot!r} 을 재생성할 수 "
            f"없다(`git worktree add` 가 'a branch named {branch!r} already exists' 로 죽는다). 미머지-"
            f"보존 브랜치(remove_slot 이 작업 유실 방지로 보존) 잔존 가능성 — 다음 중 택1: (1) 사용자가 "
            f"터미널에서 그 브랜치를 정리(머지 또는 폐기 — 세션의 손 git 브랜치 삭제는 가드가 거부한다) 후 "
            f"새 슬롯 재생성, 또는 (2) 그 브랜치의 미머지 작업을 다른 슬롯에서 "
            f"`switch <slot> {branch}` 로 **전환해 재개**(리셋 없이 보존 커밋 tip 에서 checkout). "
            f"(데이터 유실 없음)."
        )


class SlotFoldersRefused(RuntimeError):
    """슬롯 폴더 제한(`folders`)을 걸거나 읽지 못했다. 한 종류로 낸다.

    사유: 잘못된 폴더(형식·없음·파일·submodule)·커밋 안 한 변경이 있는 슬롯의 폴더 변경·git 2.36
    미만·cone 이 아닌 제한(패턴 지정)·git 실패. 생성·대여 거부는 부작용 전에 오거나 그 단계의
    롤백을 탄다(대여는 기록을 쓰기 전이라 슬롯이 idle 그대로다). `target` = 슬롯 또는 repo 표기.
    (`RuntimeError`. 파사드 `[중단]` rc 1.)
    """

    def __init__(self, target: str, reason: str):
        self.target = target
        self.reason = reason
        super().__init__(f"{target} 폴더 제한 거부. {reason}")


# Lease 직렬화 키 분류 (additive 스키마 클래스 폐쇄). from_dict 는 canonical 을 각
# 필드로 소비하고, 그 외 미지 키는 `extra` 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실).
#   - CANONICAL = 이 엔진 버전이 아는 1급 필드(각자 self.* 로 소비·extra 아님).
#   - DROP = legacy 최상위 `branch` — extra 로도 보존하지
#     않는다. 표시는 git=진실(`current_branch`)이라 장부에 branch 를 되살리면 드리프트 원천이
#     재생긴다(테스트 `test_from_dict_ignores_legacy_branch_key` 가 이 무시를 못박음). git 필드
#     안의 `branch` 서브키(작업 브랜치 스냅)와는 다른 축 — 그건 git blob 의 일부로 보존된다.
_LEASE_CANONICAL_KEYS = frozenset(
    {"slot", "repo", "session", "started", "state", "test_cmd", "git", "role", "bound"}
)

# DROP 에 legacy `pid` 를 더한다 — 세션 살아있음을 프로세스 번호로 판정하던 축은 폐기됐고
# (열림 = state==leased + session 있음), extra 로 보존하면 `to_dict` 가 옛 필드를 다시 방출해
# 장부에 되살아난다. 읽을 때 1회 버리고 다시 쓰지 않는다(옛 필드 잔존 0).
_LEASE_DROP_KEYS = frozenset({"branch", "pid"})


class Lease:
    """리스 장부 한 엔트리.

    슬롯=브랜치-무관 컨테이너·session=점유 주체·state=leased|idle|creating. `creating` 은
    create_slot 의 provisional 마커(worktree add 전 선기록·확정 시 leased·중단 시 흔적).
    **브랜치 *표시*는 권위 필드가 아니다** — git 이 단일 진실이라 장부에
    저장하지 않고 `current_branch(slot)` 로 슬롯 worktree 의 live HEAD 에서 읽는다(드리프트 불가능).

    **`git` 필드 (additive·엔진 전용 write·md 아님)**: 슬롯 git 상태를 *기대*
    (drift 감지 기준) 축으로 기계 기록한다 — `{base:{branch,commit}, branch, head, submodules:
    [{path,pin}], recorded_at}`. 뒤집지 않는다: 표시는 여전히 live
    조회(`current_branch`)·기록은 별개의 *기대* 축(submodule pin/drift 모델을 본체로 대칭 확장).
    write 시점 = alloc·핸드오프·create(release 시 정리)·compare 시점 = 0단계
    (`compare_slot_git`). 미기록(구 슬롯) = drift 감지 비활성. raw dict 로
    들고 있어 미지 서브키까지 왕복 보존한다(None=미기록·to_dict 는 None 이면 키 자체를 뺀다).

    **`extra` (미지 키 보존)**: 이 엔진 버전이 모르는 최상위 키(`task`[T-035x] 등 향후
    additive 키)를 `from_dict` 가 `extra` dict 로 보존하고 `to_dict` 가 재방출한다. 장부 지속은
    `_read_ledger`→`_write_ledger`(from_dict→to_dict) 왕복 하나로 수렴하는데, **신규 키를 모르는
    엔진(adopter#0 import 사본 lag 로 버전 skew 실재)이 아무 op 라도 하면 그 read-modify-write
    왕복에서 *모든 슬롯*의 신규 키가 소실**된다 — base/head 기록이 조용히 날아가면 drift 감지가
    가짜 기준 위에서 돌아 무의미해진다([[robustness-value-connections-before-ship]] silent
    degrade). extra 보존이 그 왕복을 무손실로 만들어 additive 전략 전체를 안전하게 닫는다.

    (dataclass 미사용 — 엔진 도구는 `spec_from_file_location` 으로 로드되는데 sys.modules
    미등록 시 dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, slot: str, repo: str, session: str,
                 started: str, state: str, test_cmd: str | None = None,
                 git: "dict | None" = None, role: str = "work",
                 bound: bool = False, extra: "dict | None" = None):
        self.slot = slot          # "work/<repo>_<N>" (브랜치 무관)
        self.repo = repo          # repo 이름 (per-repo 네임스페이스)
        self.session = session    # 점유 세션 식별자 (빈 문자열 = 무소유)
        self.started = started    # 리스 시작 시각 (UTC ISO)
        self.state = state        # "leased" | "idle" | "creating"(provisional)
        self.test_cmd = test_cmd  # 슬롯 바인딩 회귀/빌드명령 (None=미지정)
        self.git = git            # 슬롯 git 스냅 dict(base/branch/head/submodules/recorded_at)·None=미기록
        # 슬롯 role (additive): "work"(기본·배타 대여 작업 슬롯) | "readonly"
        # (research 전용 공유 슬롯·detached·session 없음·alloc/release 대상 제외). role 이
        # 0단계 carve-out(readonly 공유 슬롯) 소유검사 예외(identity_args)·엔진
        # mutation 거부(rebase/switch/dev/sync)의 canonical 판별 축이다.
        self.role = role
        # bind-origin 마커 (additive 동형 조건방출): True 는 지운 옛 사람 바인딩 경로(슬롯 직접 지정
        # 부트스트랩)가 남긴 대여 기록 행이다. 지금 엔진은 True 를 쓰지 않고 옛 기록을 읽을 때만 True 가 된다.
        # alloc·release·force_release·end_task 가 해제한다. 구 장부(키 부재)=False 동치·마이그레이션 0.
        #   alloc 은 idle 슬롯만 대여하므로 leased 행(task 명의·bind 모두)을 다른 명의로 넘기지 않는다.
        self.bound = bound
        # 미지 최상위 키 보존(additive 스키마 클래스 폐쇄). mutable 기본 회피(None 센티넬).
        self.extra = dict(extra) if extra else {}

    def __repr__(self) -> str:
        return (f"Lease(slot={self.slot!r}, repo={self.repo!r}, "
                f"session={self.session!r}, state={self.state!r}, "
                f"test_cmd={self.test_cmd!r}, git={self.git!r}, role={self.role!r}, "
                f"bound={self.bound!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Lease):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict:
        d = {
            "slot": self.slot,
            "repo": self.repo,
            "session": self.session,
            "started": self.started,
            "state": self.state,
            "test_cmd": self.test_cmd,
        }
        # git 은 *있을 때만* 방출한다 — 구 장부(git 필드 부재)를 로드·재기록해도 `git: null` 을
        # 덧붙이지 않아 왕복이 무손실(하위호환·DoD). test_cmd 는 항상 방출.
        if self.git is not None:
            d["git"] = self.git
        # role 은 *기본("work")이 아닐 때만* 방출한다 (git 필드 조건방출과 동형·하위호환).
        # 구 장부(role 부재=work 슬롯)를 로드·재기록해도 `role: "work"` 를 덧붙이지 않아 왕복이
        # byte-무손실이다(from_dict 가 부재 시 "work" 로 read). readonly 슬롯만 role 을 장부에 남긴다.
        if self.role != "work":
            d["role"] = self.role
        # bound 는 *True 일 때만* 방출한다 (git/role 조건방출과 동형·하위호환). 구 장부(bound
        # 부재=False·pool 슬롯)를 로드·재기록해도 `bound: false` 를 덧붙이지 않아 왕복이 byte-무손실
        # 이다(from_dict 가 부재 시 False 로 read). 옛 사람 bind 기록 행만 bound 를 장부에 남긴다.
        if self.bound:
            d["bound"] = True
        # 미지 키 재방출(구·신 엔진 왕복 무손실). extra 엔 canonical/legacy-branch 가 없다
        # (from_dict 가 배제) — canonical 을 덮을 위험 없음.
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        # 하위호환 read: test_cmd 부재(구 장부)는 None·git 부재는 None. 구 장부의 legacy 최상위
        # `branch` 키는 관용적으로 *무시*한다(branch 는 권위 필드가
        # 아니다·표시는 git 에서만 온다). canonical/legacy-branch 를 뺀 나머지 미지 키는 `extra`
        # 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실·additive 스키마 클래스 폐쇄).
        extra = {
            k: v for k, v in d.items()
            if k not in _LEASE_CANONICAL_KEYS and k not in _LEASE_DROP_KEYS
        }
        return cls(
            slot=d["slot"],
            repo=d["repo"],
            session=d.get("session", ""),
            started=d.get("started", ""),
            state=d.get("state", "leased"),
            test_cmd=d.get("test_cmd"),
            git=d.get("git"),
            role=d.get("role", "work"),   # 하위호환 read: 구 장부(role 부재) = "work"(작업 슬롯).
            bound=bool(d.get("bound", False)),  # 하위호환 read: 구 장부(bound 부재) = False(pool 슬롯).
            extra=extra,
        )


# Task 직렬화 키 분류 (Lease 동형 additive 스키마). canonical 을 각 필드로 소비하고 그
# 외 미지 키는 `extra` 로 보존해 왕복 무손실(구·신 엔진 skew·향후 additive task 서브키 대비).
# `open`·`pid`·`todos` 는 옛 이름으로 남겨 둔다 — canonical 에 있어야 `from_dict` 가 그것을
# `extra` 로 보존하지 않고 버린다(다시 쓸 때 옛 필드가 되살아나지 않는다·읽기 1회 이주). 옛 열림
# 기록은 어느 세션이 열었는지 모르므로 닫힘으로 읽는다 — 열린 세션은 부트스트랩을 다시 한다.
_TASK_CANONICAL_KEYS = frozenset(
    {"name", "prefix", "session", "open", "pid", "started", "todos"})


class Task:
    """리스 장부 top-level `tasks` 컬렉션 한 엔트리 — 작업 단위 정체성.

    task = 슬롯과 **직교**한 작업스트림 정체성(슬롯 0개로도 존재 가능). 슬롯-키 lease 행과 별개의
    top-level 컬렉션에 산다(같은 파일·같은 `_lease_lock`/atomic replace 직렬화). 필드:
      - `name`   — task 이름(사람이 정하는 자유 포맷·`<등록 repo>_<N>` 예약 제외·유일성=사람 안).
      - `prefix` — 이 task 세션의 board prefix(기본 None=없음·변경은 `task prefix`).
      - `session`— 이 task 를 연 하네스 세션 ID(빈 값 = 닫힘). 프로세스 ID 가 아니다 — 재개·압축
        뒤에도 같은 값이다(세 하네스 실측). 여는 자리 = `bind_task`, 닫는 자리 =
        `close_task_session`(핸드오프·`task close`)·`end_task`. 슬롯 쓰기 가드가 "이 세션의
        task" 를 이 값으로 찾는다. `open` 은 이 값에서 파생한 읽기 전용 값이다.
      - `started`— task 레코드 생성 시각(UTC ISO).

    저장 위치 = 리스 장부 파일 top-level `tasks`(출처 1개·저장소 신설 불요·단일 파일 스캔).
    **열린 할 일은 여기 없다** — `.local/tasks/<name>/todo.json` 이 소유한다. 할 일은 세션 중
    수시로 바뀌고 슬롯 대여는 거의 안 바뀌는데, 한 파일에 두면 잦은 쓰기가 드문 쪽까지 매번 다시
    쓰고 task 끼리 같은 락을 두고 겨룬다. 미지 최상위 task 키는 `extra` 로 왕복 보존
    (Lease 동형·additive 스키마 클래스 폐쇄). (dataclass 미사용 — Lease 와 같은 forward-ref 회피.)
    """

    def __init__(self, name: str, prefix: "str | None" = None,
                 session: str = "", started: str = "", extra: "dict | None" = None):
        self.name = name
        self.prefix = prefix      # board prefix (None=없음)
        self.session = session    # 연 하네스 세션 ID (빈 값 = 닫힘)
        self.started = started    # 레코드 생성 시각 (UTC ISO)
        self.extra = dict(extra) if extra else {}

    @property
    def open(self) -> bool:
        """열림 여부 — `session` 에서 파생한다(따로 저장하지 않는다)."""
        return bool(self.session)

    def __repr__(self) -> str:
        return (f"Task(name={self.name!r}, prefix={self.prefix!r}, "
                f"session={self.session!r}, started={self.started!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "prefix": self.prefix,
            "session": self.session,
            "started": self.started,
        }
        d.update(self.extra)      # 미지 키 재방출(왕복 무손실·canonical 을 덮을 위험 없음)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        extra = {k: v for k, v in d.items() if k not in _TASK_CANONICAL_KEYS}
        # 읽기 1회 이주 — 옛 `open`·`pid`·`todos` 는 버린다(`to_dict` 가 다시 내보내지 않는다).
        session = d.get("session", "")
        return cls(
            name=d["name"],
            prefix=d.get("prefix"),
            session=session if isinstance(session, str) else "",
            started=d.get("started", ""),
            extra=extra,
        )


class ReopenTaskResult:
    """`reopen_task` 성공 결과."""

    def __init__(self, name: str, moved_from: Path, moved_to: Path, task: Task):
        self.name = name
        self.moved_from = moved_from
        self.moved_to = moved_to
        self.task = task

    def __repr__(self) -> str:
        return (f"ReopenTaskResult(name={self.name!r}, moved_from={self.moved_from!r}, "
                f"moved_to={self.moved_to!r}, task={self.task!r})")


class SubmoduleStatus:
    """슬롯 worktree 한 submodule 의 상태 — 역할(live HEAD) + pin 비교 판정.

    (`_resync_submodules_selective`)의 *역할 판별*을 표시층으로 재사용한다 — 역할은
    별도 장부 없이 submodule 의 live git HEAD 로 정한다(무스키마):

      - `kind="dev-ahead"` — **on-branch**(=dev 역할·`symbolic-ref -q HEAD` rc0). 사용자가 그
        submodule 에서 브랜치를 파 작업 중 → **정보**(경고 아님). detached pin 으로 낚아채지
        않는 selective resync 의 보호 대상(전역 recurse 가 파괴하던 크럭스 A).
      - `kind="drift"` — **detached & pin ≠ working**(`git submodule status` flag `+`/`U`) →
        **경고**. superproject pin 과 어긋난 detached. 재-alloc 시 재동기하나 dirty 면
        잔존한다(그래서 dirty 도 실어 *왜* 안 풀렸는지 surface).
      - `kind="pinned"` — **detached & pin == working**(flag 공백) → 정상(pinned).
      - `kind="uninitialized"` — submodule 미초기화(flag `-`) → 경고(슬롯 init 비정상).

    `warning` = kind in {"drift", "uninitialized"}(dev-ahead/pinned 은 경고 아님 — 이 구별이
    `dirty` = 워킹트리 미커밋 변경(`_submodule_dirty` 재사용).
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
    """슬롯 worktree 한 줄 상태 — branch + upstream + submodule 역할.

    부트스트랩이 현재 슬롯의 상태를 1회 surface 하는 데 쓰는 구조(표시는 pm_bootstrap 이 담당).

      - `branch` — `current_branch(slot)` live(None=detached/조회불가).
      - `upstream` — `@{upstream}` 해소명(예 `origin/a5`·None=미해소).
      - `upstream_ok` — `@{upstream}` 해소 여부. 미해소=경고(슬롯 tracking 이
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


class GitWorktree:
    """`git worktree list --porcelain` 한 엔트리 — 실 git worktree 의 경로/브랜치/상태.

    리스 장부(Lease)와 대조할 *실-git* 소스(`list_git_worktrees`). `slot` = 경로가 WORK_DIR
    (=REPO/work) 바로 아래의 단일 디렉토리면 슬롯 식별자(`work/<repo>_<N>`), 아니면 None
    (bare 원·multi-PM 루트·외부 worktree). `bare` = bare 원 엔트리(슬롯 아님·reconcile/슬롯번호
    에서 제외). (dataclass 미사용 — Lease/SlotStatus 와 동일 이유: `spec_from_file_location`
    로드 시 dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, path: str, slot: "str | None", branch: "str | None",
                 detached: bool, bare: bool):
        self.path = path          # `git worktree list` 가 준 절대경로
        self.slot = slot          # "work/<repo>_<N>" (WORK_DIR 하위 단일 컴포넌트) 또는 None
        self.branch = branch      # 체크아웃 브랜치명 (detached/bare 면 None)
        self.detached = detached  # detached HEAD 여부
        self.bare = bare          # bare 원 엔트리 여부(슬롯 아님)

    def __repr__(self) -> str:
        return (f"GitWorktree(path={self.path!r}, slot={self.slot!r}, "
                f"branch={self.branch!r}, detached={self.detached!r}, bare={self.bare!r})")


class ReconcileResult:
    """리스 장부 × 실 git worktree 정합 결과 — orphan/stale/incomplete drift (조회 전용).

    - **orphans** = git worktree(슬롯 경로·non-bare)인데 장부에 없음(`GitWorktree` 리스트).
      중단된 create/수동 add 잔존(audit #2) — 다음 create_slot 번호 충돌(#4)·status blind(#3).
    - **stale** = 장부 확정 리스(leased/idle)인데 대응 git worktree 없음(`Lease` 리스트).
      worktree dir 삭제/prune 됨(audit #3).
    - **incomplete** = provisional("creating") 리스(`Lease` 리스트). worktree add 후 확정 전에
      중단된 create 의 흔적(SIGKILL 도 커버) — 정리 대상.

    (dataclass 미사용 — 위 클래스들과 동일 이유.)
    """

    def __init__(self, orphans: "list[GitWorktree]", stale: "list[Lease]",
                 incomplete: "list[Lease]"):
        self.orphans = orphans
        self.stale = stale
        self.incomplete = incomplete

    def __repr__(self) -> str:
        return (f"ReconcileResult(orphans={self.orphans!r}, stale={self.stale!r}, "
                f"incomplete={self.incomplete!r})")


class RemoveResult:
    """`remove_slot` 결과 — 무엇을 지웠고 그 슬롯의 브랜치를 어떻게 처리했는지 (보고용).

    - `slot` — 제거한 슬롯 식별자(`work/<repo>_<N>`).
    - `branch` — 제거 당시 슬롯 worktree 의 live 브랜치(`current_branch`·None=detached/조회불가/
      트리 부재).
    - `branch_deletion` — 그 브랜치의 삭제 판정 결과(`BranchDeletion`·`delete_branch_by_rule`).
      브랜치를 판별하지 못했으면 None.
    - `tree_cleaned` — git 이 등록만 지우고 남긴 디렉터리 잔재를 엔진이 마저 지웠는가.
    - `forced_state` — `--force` 로 **활성 리스**(leased/creating·사용 중)를 override 하고 제거했을 때
      그 원래 state(예 "leased"·CLI 가 강제 회수 경고 surface). idle 슬롯이었으면 None(정상 제거·경고 불요).

    (dataclass 미사용 — Lease/ReconcileResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, slot: str, *, branch: "str | None",
                 branch_deletion: "BranchDeletion | None", tree_cleaned: bool,
                 forced_state: "str | None" = None):
        self.slot = slot
        self.branch = branch
        self.branch_deletion = branch_deletion
        self.tree_cleaned = tree_cleaned
        self.forced_state = forced_state   # force 로 override 한 활성 state(leased/creating)·None=idle 정상.

    def __repr__(self) -> str:
        return (f"RemoveResult(slot={self.slot!r}, branch={self.branch!r}, "
                f"branch_deletion={self.branch_deletion!r}, tree_cleaned={self.tree_cleaned!r}, "
                f"forced_state={self.forced_state!r})")


# `fetch origin` 실패 시 폴백 대상 ref 의 정체 — 경고 문구 단일 소스. remote-tracking ref
# (`origin/<branch>`)가 기준이면 폴백 대상은 로컬 브랜치가 아니라 **직전 fetch 시점에 멈춘
# remote-tracking ref** 다. 둘을 "로컬 …(동결 head)" 로 뭉뚱그리면 사용자가 로컬 브랜치를
# 갱신해 해소하려 하지만(그 ref 는 안 움직인다) 실제 해소는 fetch 복구뿐이다.
_REMOTE_TRACKING_PREFIX = "origin/"


def _frozen_fallback_label(ref: str) -> str:
    """fetch 실패 폴백 대상 표기 — 로컬 브랜치 / stale remote-tracking ref 를 구분한다."""
    if ref.startswith(_REMOTE_TRACKING_PREFIX):
        return f"stale remote-tracking `{ref}`(직전 fetch 시점 동결)"
    return f"로컬 `{ref}`(동결 head)"


def _slot_for(repo: str, n: int) -> str:
    """슬롯 식별자 `work/<repo>_<N>`."""
    return f"work/{repo}_{n}"


def _slot_number(repo: str, lease: "Lease") -> float:
    """장부 행이 이 repo 에서 점유한 슬롯 번호 — 정렬 키 (최소 번호 대여 결정론 ⓒ).

    번호는 **행 값**(경로 값 ∪ session 값·`identity_args._row_slot_numbers` 공유 규칙)에서
    계상한다 — 슬롯 식별자 문자열의 마지막 `_` 를 뜯던 자리다. 행이 이 repo 번호를 주지 않으면
    (비-숫자 커스텀 슬롯·경로에 번호가 없는 행) `inf` 로 밀어 숫자 슬롯을 앞세운다(결정론적
    정렬·부재/이상 슬롯이 최소 자리를 차지하지 않게)."""
    numbers = identity_args._row_slot_numbers(repo, lease.slot, lease.session)
    return float(min(numbers)) if numbers else float("inf")


def _existing_slot_numbers(repo: str, leases: list[Lease]) -> set[int]:
    """장부에 이미 있는 이 repo 의 슬롯 번호 집합 — **행 전수**에서 계상.

    번호는 행의 slot 경로(`work/<repo>_<N>`)와 행의 session(`<repo>_<N>`) 양쪽에서 읽는다
    (`identity_args._row_slot_numbers` 공유 규칙·`repo_slot_numbers` 와 같은 축). 경로에 번호가
    없는 행(PM 홈 자신을 가리키는 행 등)도 session 으로 자기 번호를 점유하므로, 다음 `alloc` 이
    이미 쓰이는 이름(`<repo>_<N>` 세션·브랜치)을 다시 파지 않는다. 상태 무관(idle 포함) —
    번호는 대여 상태가 아니라 **이름 점유** 축이다(종전 동형).
    """
    nums: set[int] = set()
    for lease in leases:
        nums |= identity_args._row_slot_numbers(repo, lease.slot, lease.session)
    return nums


# ── 열림/닫힘 판정 (기록 축) ─────────────────────────────────────────────────


def lease_is_open(lease: Lease) -> bool:
    """이 작업 폴더 기록이 **열려 있는가** — 판정 단일 지점.

    열림 = 열림 기록(`state=="leased"` + 점유 `session`)이 있고 그에 대응하는 종료 기록이 아직
    없음. 종료 기록은 `release`/`force_release`/`end_task` 가 `state="idle"` + `session=""` 로
    남긴다. 판정에 운영체제 프로세스 조회는 0회다 — 번호는 재사용되고 같은 세션을 이어 열면
    번호가 바뀌어 살아있음을 말해 주지 못한다. readonly 공유 슬롯은 `session` 이 비어 있어
    자연히 닫힘이다(무소유 — 죽은 세션이 아니라 세션이 *없는* 행이다).
    """
    return lease.state == "leased" and bool(lease.session)


class EndTaskResult:
    """`end_task` 결과 — 반납/이동 요약 또는 dirty 거부.

    `dirty` 가 비어있지 않으면 **거부**(아무것도 반납/이동하지 않음) — 호출부가 목록을 보이고
    사용자 정리를 요구한다. 비어있으면 `released`(idle 반납한 슬롯)·`moved_to`(아카이브 목적지·
    서술 폴더 부재 시 None)가 결과다. dataclass 미사용 — Lease 등과 동일(forward-ref 회피).
    """

    def __init__(self, name: str, *, released: list[str], dirty: list[str],
                 moved_from: "Path | None", moved_to: "Path | None"):
        self.name = name
        self.released = released      # idle 로 반납한 슬롯 식별자(정렬)
        self.dirty = dirty            # dirty 라 거부 사유가 된 슬롯 식별자(정렬·비어있으면 성공)
        self.moved_from = moved_from  # 이동 전 서술 폴더(.local/tasks/<name>/·부재면 None)
        self.moved_to = moved_to      # 아카이브 목적지(.local/tasks/_ended/<name>-<날짜>/·부재면 None)

    @property
    def refused(self) -> bool:
        return bool(self.dirty)

    def __repr__(self) -> str:
        return (f"EndTaskResult(name={self.name!r}, released={self.released!r}, "
                f"dirty={self.dirty!r}, moved_to={self.moved_to!r})")


# head 비교 결과(`merge-base --is-ancestor` 완화) — GitCompareResult.head_relation 값.
HEAD_MATCH = "match"            # 기록 head == live head (같은 branch) → 통과.
HEAD_DESCENDANT = "descendant"  # live 가 기록 head 의 후손 + 같은 branch → crash 후 재개(notice·통과).
HEAD_DIVERGED = "diverged"      # 브랜치 변경 또는 비후손(리셋·되감기) → FAIL-LOUD.
HEAD_UNKNOWN = "unknown"        # 기록/live head 미상 → 판정 불가(통과 취급).


class GitCompareResult:
    """기록된 `lease.git`(기대) vs 슬롯 live 상태 비교 결과 — 부트스트랩 0단계 소비.

    - `unrecorded` — git 미기록(구 슬롯·git 필드 부재/슬롯 없음) → drift 감지 **비활성**. 0단계는
      차단이 아니라 loud 표시 + 사용자 질의. ok/fail 어느 쪽도 아닌 별도 상태.
    - `recorded`/`live` — 기록 git dict / live 스냅 dict(branch·head·submodules).
    - `branch_match` — 기록 branch == live branch. False = 브랜치 변경(사고·FAIL-LOUD·표 branch 축).
    - `head_relation` — `match`/`descendant`/`diverged`/`unknown`(is-ancestor 완화·위 상수).
    - `submodule_drift` — 기록 pin ≠ live pin 인 submodule path 목록(경고).
    - ⚠️ **`recorded["base"]`(readonly 슬롯만 가진다)는 비교축이 아니다**(비교축은
      branch+head+submodule 뿐) — *전달만* 하고 비교/계산을 붙이지 않는다.

    (dataclass 미사용 — Lease/SlotStatus 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)
    """

    def __init__(self, slot: str, *, unrecorded: bool, recorded: "dict | None",
                 live: dict, branch_match: bool, head_relation: str,
                 submodule_drift: "list[str]"):
        self.slot = slot
        self.unrecorded = unrecorded
        self.recorded = recorded
        self.live = live
        self.branch_match = branch_match
        self.head_relation = head_relation
        self.submodule_drift = submodule_drift

    @property
    def fail_loud(self) -> bool:
        """명시적 FAIL-LOUD — branch 변경(사고) 또는 head diverged(리셋·비후손). unrecorded 는
        fail 아님(사용자 질의 대상)."""
        if self.unrecorded:
            return False
        return (not self.branch_match) or self.head_relation == HEAD_DIVERGED

    @property
    def ok(self) -> bool:
        """0단계 통과 여부 — 기록 있고 FAIL-LOUD 아님. unrecorded 는 ok 아님(질의 필요·별도 처리)."""
        return (not self.unrecorded) and (not self.fail_loud)

    def __repr__(self) -> str:
        return (f"GitCompareResult(slot={self.slot!r}, unrecorded={self.unrecorded!r}, "
                f"branch_match={self.branch_match!r}, head_relation={self.head_relation!r}, "
                f"submodule_drift={self.submodule_drift!r})")


BRANCH_PARENT_KEY = "pm-parent"   # branch.<브랜치>.pm-parent = 부모 브랜치 이름.
BRANCH_FORK_KEY = "pm-fork"       # branch.<브랜치>.pm-fork = 갈라진 커밋 sha.
_GIT_CONFIG_MISSING_RC = 1        # `git config --get`/`--get-regexp` 의 "값 없음" rc.
_BRANCH_CONFIG_PREFIX = "branch."
_PARENT_RECORDS_PATTERN = r"^branch\..*\.pm-(parent|fork)$"   # 부모 기록 두 키 한 번 읽기.
_STASH_SUBJECT_PREFIXES = ("WIP on {branch}:", "On {branch}:")   # `stash list --format=%gs` 제목 형식.
BRANCH_DELETED = "deleted"
BRANCH_PRESERVED = "preserved"

# 판정 사유 — 보존 8개 + 삭제 근거 2개. 슬롯 제거 CLI 가 이 값으로 문구를 고른다.
BRANCH_REASON_PROTECTED = "protected"            # repo 보호 브랜치
BRANCH_REASON_DEFAULT = "default-branch"         # 공용 저장소 기본 브랜치
BRANCH_REASON_CHILDREN = "children"              # 자식 브랜치가 있다
BRANCH_REASON_NO_PARENT = "no-parent"            # 부모 기록 없음
BRANCH_REASON_PARENT_GONE = "parent-gone"        # 부모 브랜치가 지워져 합쳐짐을 잴 수 없다
BRANCH_REASON_UNMERGED = "unmerged"              # 합쳐지지 않은 커밋이 있고 승인값이 없다
BRANCH_REASON_TIP_MOVED = "tip-moved"            # 판정 뒤 끝 커밋이 바뀌었다
BRANCH_REASON_DELETE_REFUSED = "delete-refused"  # `git branch -D` rc≠0 (다른 worktree 가 쥠 등)
BRANCH_REASON_MERGED = "merged"                  # 삭제 근거 — 부모에 다 합쳐짐
BRANCH_REASON_ACKED = "acked"                    # 삭제 근거 — 합쳐지지 않았으나 사용자 승인값

# origin 반영 여부 — 합쳐지지 않아 보존할 때만 채운다.
ORIGIN_NO_BRANCH = "no-branch"                   # refs/remotes/origin/<브랜치> 없음
ORIGIN_CONTAINS_TIP = "contains-tip"             # origin 브랜치가 끝 커밋을 담고 있다
ORIGIN_MISSING_TIP = "missing-tip"               # origin 브랜치는 있으나 끝 커밋이 없다
_IS_ANCESTOR_FALSE_RC = 1      # `merge-base --is-ancestor` 의 "조상 아님" rc.
_REV_PARSE_MISSING_RC = 1      # `rev-parse --verify --quiet` 의 "그런 ref 없음" rc.
_ORIGIN_BRANCH_PREFIX = "refs/remotes/origin/"
BRANCH_SLOT_KEY = "pm-slot"                          # branch.<브랜치>.pm-slot = 보존 직전 쓰던 슬롯.
BRANCH_PRESERVED_AT_KEY = "pm-preserved-at"          # branch.<브랜치>.pm-preserved-at = 보존 시각(UTC ISO).
BRANCH_PRESERVED_REASON_KEY = "pm-preserved-reason"  # branch.<브랜치>.pm-preserved-reason = BRANCH_REASON_*.
_PRESERVED_RECORD_KEYS = (BRANCH_SLOT_KEY, BRANCH_PRESERVED_AT_KEY, BRANCH_PRESERVED_REASON_KEY)
_PRESERVED_RECORD_PATTERN = r"^branch\..*\.pm-(slot|preserved-at|preserved-reason)$"
_GIT_CONFIG_UNSET_MISSING_RC = 5   # `git config --unset` 의 "그런 키 없음" rc(git 2.43 실측).

# 보존이어도 기록하지 않는 판정 사유 — 슬롯 수명과 무관한 보호 브랜치·공용 저장소 기본 브랜치.
_UNRECORDED_PRESERVED_REASONS = frozenset({BRANCH_REASON_PROTECTED, BRANCH_REASON_DEFAULT})


class RefreshRefused(RuntimeError):
    """readonly 슬롯 `refresh` 거부 — dirty(누군가 씀·신호) / base 미해소 / non-readonly.

    `reason` ∈ {"dirty", "no-base", "not-readonly"}:
      - **dirty** — read-only 슬롯에 미커밋 변경이 있다 = "누군가 여기 썼다"는 신호. 조용히 reset 하면
        신호가 사라진다 → 거부 + loud(감지=기계·해소=사용자·submodule drift 동형·결정). 사용자가 보고
        판단한다(수동 정리/조사 후 재시도).
      - **no-base** — `--onto` 미지정 + 기록된 base.branch 도 없어 어디로 갱신할지 불명(추론 금지).
      - **not-readonly** — 대상 슬롯이 readonly 가 아니다(refresh 는 detached 공유 슬롯 전용 — 작업
        슬롯을 detach 로 이동하면 브랜치 위치를 잃는다). (`RuntimeError` — 파사드 rc 1.)"""

    def __init__(self, slot: str, reason: str, *, detail: str = ""):
        self.slot = slot
        self.reason = reason
        tail = f" — {detail}" if detail else ""
        msg = {
            "dirty": (f"슬롯 {slot!r} 에 미커밋 변경이 있어 refresh 를 거부한다 — read-only 슬롯의 dirty 는 "
                      f"'누군가 여기 썼다'는 신호다(조용히 reset 안 함). 변경을 확인·정리한 뒤 재시도하라"),
            "no-base": (f"슬롯 {slot!r} 의 기준점(base)이 미기록이고 `--onto` 도 없어 어디로 refresh 할지 "
                        f"불명이다(추론 금지) — `--onto <branch>` 로 갱신 기준을 명시하라"),
            "not-readonly": (f"슬롯 {slot!r} 은 readonly 공유 슬롯이 아니다 — refresh 는 detached read-only "
                             f"슬롯 전용이다(작업 슬롯을 detach 이동하면 브랜치 위치를 잃는다)"),
            "git-error": (f"슬롯 {slot!r} refresh 중 git 오류"),
        }.get(reason, f"슬롯 {slot!r} refresh 거부({reason})")
        super().__init__(msg + tail + f"")


REBASE_REBASED = "rebased"        # 성공 — 장부 원자 갱신 완료.
REBASE_SKIPPED = "skipped"        # 선-검사/거부 스킵(loud) — reason 참조.
REBASE_CONFLICT = "conflict"      # rebase 가 rc≠0(충돌 등) — 그 상태 그대로·장부 미갱신·loud.


class RebaseSlotResult:
    """rebase 한 슬롯 하나의 결과 — outcome + 진단 (일괄 요약 원료).

    - `outcome` ∈ {`rebased`(성공·장부 원자 갱신), `skipped`(선-검사/거부 스킵+loud),
      `conflict`(rebase rc≠0·그 상태 그대로 + fail-loud·장부 미갱신)}.
    - `reason` — skipped/conflict 사유(진단·CLI loud surface). skipped: `readonly`/`not-owner`/
      `no-worktree`/`dirty`/`in-progress`/`no-branch`/`no-parent`/`unresolvable-onto`. conflict: git 출력 요약.
    - `base` — rebase 가 향한 부모 브랜치명(해소된 경우·None=해소 전 스킵).
    - `new_head` — 성공 시 rebase 후 슬롯 HEAD sha(진단·None=미성공).

    (dataclass 미사용 — Lease/RemoveResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)"""

    def __init__(self, slot: str, outcome: str, *, reason: "str | None" = None,
                 base: "str | None" = None, new_head: "str | None" = None):
        self.slot = slot
        self.outcome = outcome
        self.reason = reason
        self.base = base
        self.new_head = new_head

    @property
    def ok(self) -> bool:
        return self.outcome == REBASE_REBASED

    def __repr__(self) -> str:
        return (f"RebaseSlotResult(slot={self.slot!r}, outcome={self.outcome!r}, "
                f"reason={self.reason!r}, base={self.base!r}, new_head={self.new_head!r})")


# board.DEFAULT_PROTECTED 와 *동형* 폴백 (pm_config._DEFAULT_PROTECTED 동형). board 부재/
# 헬퍼 부재/파싱 실패로 areas 를 못 읽어도 보호는 안전 기본값이 있어야 한다(미해소여도 main 류 차단).

# 새 브랜치 생성 안내 — switch 는 브랜치를 만들지 않는다(생성 시점에 부모를 기록하는 task 경로만).
_TASK_BRANCH_REMEDY = "`pm-config task branch <task> <슬롯> <브랜치> --from <부모>`"


class SwitchRefused(RuntimeError):
    """`switch <slot> <branch>` 거부 — rc 1 + 사유.

    ⚠ **부작용 범위는 사유마다 다르다**(내부 리뷰 should-fix — docstring 이 자기 사유와 모순이면
    안 된다):
      - **선-검사 사유**(unregistered·protected·protected-upstream·checked-out-elsewhere·
        no-worktree·dirty·rebase-in-progress·invalid-ref·not-found) — 전환도 기록도 하지 않는다
        (부작용 0).
      - **git-error** — `_checkout_required`(checkout+resync)를 *시도한 뒤* 실패(트리 상태는 git 이
        남긴 그대로·장부 불변·`CheckoutFailed` 를 이 사유로 감싼다).
      - **record-failed** — 전환은 **성공**했고 그 뒤 기록(보존 기록 지우기 또는 장부 스냅 재기록)만
        실패(원자성 파손 → 아래 안내).

    `reason` ∈ {"unregistered", "protected", "protected-upstream", "checked-out-elsewhere",
    "no-worktree", "dirty", "rebase-in-progress", "invalid-ref", "not-found", "git-error",
    "record-failed"}:
      - **unregistered** — 장부에 없는 슬롯(스냅 기록 대상이 아니다·`record` 동형 메시지).
      - **protected** — 대상 브랜치가 그 repo 보호목록. 보호브랜치로 *들어가는* 전환은 이
        커맨드의 목적(main-참조 해소)과 정반대다.
      - **protected-upstream** — 대상(기존) 브랜치가 **보호브랜치 원격을 origin-추적**한다.
        전환은 되지만 다음 0단계가 다시 main-참조로 막으므로 여기서 거부한다(remedy-유발 상태전이).
      - **checked-out-elsewhere** — 대상(기존) 브랜치를 **이 슬롯이 아닌 다른 worktree** 가 이미
        checkout 중이다(통합 브랜치 `task/main` 등). 전환을 강행하면 plain checkout 이 실패하고
        옛 `-B` 폴백이 그 브랜치 ref 를 이 슬롯 HEAD 로 **리셋**했을 공유 브랜치 clobber 클래스다.
        `--force` 류 우회는 없다(슬롯은 dev 브랜치 전용·통합 브랜치는 전용 worktree 소관).
        self(같은 슬롯)가 이미 보유한 브랜치로의 재-switch 는 통과한다.
      - **no-worktree** — 슬롯 worktree 경로 부재(실경로·runner 미주입).
      - **dirty** — 미커밋 변경이 있다. 전환이 WIP 를 흔든다(rebase 선-검사 동형).
      - **rebase-in-progress** — 그 슬롯에 rebase 가 진행 중(`.git/rebase-merge|rebase-apply`).
        해소(continue/abort)가 먼저다 — 중간에 브랜치를 옮기면 진행 중 rebase 가 꼬인다
        (`_rebase_one` 선-검사와 동형·같은 프리미티브 `_rebase_in_progress`).
      - **invalid-ref** — git 브랜치명으로 부적합하거나 **정규화 불가**(`check-ref-format --branch`
        rc≠0·빈/이상 출력·비고정점·호출 실패). 정규화가 안 되면 보호목록 등 안전검사를 그 이름
        기준으로 할 수 없으므로 fail-closed 로 거부한다.
      - **not-found** — 로컬 브랜치가 없다. switch 는 브랜치를 만들지 않는다 — 새 브랜치는 생성
        시점에 부모를 기록하는 task branch 명령으로 만든다.
      - **git-error** — 브랜치 전환(`_checkout_required`) 자체가 실패.
      - **record-failed** — 전환은 됐으나 보존 기록 지우기(`clear_preserved_branch`) 또는 장부 스냅
        재기록이 실패했다(원자성 깨짐 → **loud**: 조용히 성공 보고하면 다음 0단계가 diverged 로
        막힌다 → `record` 안내·보존 기록 실패는 원인 해소 뒤 같은 switch 재실행).
    (`RuntimeError` — `RefreshRefused` 동형·CLI 파사드가 rc 1 로 surface.)"""

    def __init__(self, slot: str, reason: str, *, detail: str = ""):
        self.slot = slot
        self.reason = reason
        tail = f" — {detail}" if detail else ""
        msg = {
            "unregistered": (f"슬롯 {slot!r} 이 리스 장부에 없다 — switch 는 등록된 슬롯에만 "
                             f"(전환+스냅 재기록을) 수행한다"),
            "protected": (f"슬롯 {slot!r} 을 보호 브랜치로 전환하는 것은 거부한다 — 이 커맨드는 "
                          f"main-참조 상태를 *벗어나는* 전환용이다(보호목록=areas.md `protected`)"),
            "protected-upstream": (f"슬롯 {slot!r} 을 **보호브랜치 원격을 추적하는** 브랜치로 전환하는 것은 "
                                   f"거부한다 — 전환은 되지만 다음 부트스트랩 0단계가 다시 main-참조"
                                   f"(origin-추적)로 막는다. upstream 이 없는 작업 브랜치로 전환하라"
                                   f"(새 작업 브랜치는 {_TASK_BRANCH_REMEDY})"),
            "checked-out-elsewhere": (f"슬롯 {slot!r} 을 **다른 worktree 가 이미 checkout 중인** 브랜치로 "
                                      f"전환하는 것은 거부한다 — 전환을 강행하면 그 브랜치 ref 가 이 슬롯 "
                                      f"HEAD 로 리셋된다(공유 브랜치 clobber). 우회 플래그는 없다"),
            "no-worktree": f"슬롯 {slot!r} 의 worktree 경로가 없다 — 전환할 트리가 없다",
            "dirty": (f"슬롯 {slot!r} 에 미커밋 변경이 있어 switch 를 거부한다 — 브랜치 전환이 WIP 를 "
                      f"흔든다(커밋/stash 후 재시도)"),
            "rebase-in-progress": (f"슬롯 {slot!r} 에 rebase 가 진행 중이라 switch 를 거부한다 — 먼저 "
                                   f"슬롯에서 `git rebase --continue`(충돌 해결 후) 또는 "
                                   f"`git rebase --abort`(취소)로 해소하라(rebase 선-검사 동형)"),
            "invalid-ref": (f"슬롯 {slot!r} 에 지정한 브랜치명이 git ref 규칙에 어긋나거나 실 브랜치명으로 "
                            f"정규화할 수 없다(`git check-ref-format --branch`) — 다른 이름을 지정하라"),
            "not-found": (f"슬롯 {slot!r} 에 지정한 브랜치가 없다 — switch 는 기존 브랜치 전환만 한다. "
                          f"새 브랜치는 {_TASK_BRANCH_REMEDY} 로 만든다"),
            "git-error": f"슬롯 {slot!r} 브랜치 전환 중 git 오류",
            "record-failed": (f"슬롯 {slot!r} 브랜치 전환은 됐으나 기록(보존 기록 지우기 또는 장부 스냅 "
                              f"재기록)에 실패했다 — 이대로면 다음 0단계가 '기록↔live diverged' 로 막는다. "
                              f"`worktree_pool.py record {slot}` 로 스냅을 재기록하라(보존 기록 지우기 "
                              f"실패였다면 원인을 해소한 뒤 같은 switch 를 다시 실행한다)"),
        }.get(reason, f"슬롯 {slot!r} switch 거부({reason})")
        super().__init__(msg + tail + "")


class SwitchResult:
    """`switch`·`create_task_branch` 성공 결과 — 어느 브랜치로·재기록된 head (CLI 보고 원료).

    (dataclass 미사용 — Lease/RebaseSlotResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)"""

    def __init__(self, slot: str, branch: str, *, head: "str | None",
                 requested: "str | None" = None):
        self.slot = slot
        self.branch = branch        # **정규화된** 실 브랜치명(검사·전환·장부 기록의 단일 기준).
        self.head = head            # 재기록된 장부 스냅의 head(sha).
        self.requested = requested if requested is not None else branch  # 사용자 원문(revspec 가능).

    @property
    def expanded(self) -> bool:
        """원문이 revspec 이라 다른 이름으로 해소됐는가(`@{-1}`→`main`) — 호출부 loud 보고용."""
        return self.requested != self.branch

    def __repr__(self) -> str:
        return (f"SwitchResult(slot={self.slot!r}, branch={self.branch!r}, "
                f"head={self.head!r}, requested={self.requested!r})")


class TaskBranchRefused(RuntimeError):
    """`create_task_branch` 거부 — rc 1 + 사유 (`SwitchRefused` 형식).

    부작용 범위: **선-검사 사유**(unregistered·not-owner·no-worktree·dirty·rebase-in-progress·
    invalid-ref·protected·exists·ambiguous-ref·df-conflict·parent-missing)는 브랜치도 기록도 만들지
    않는다(부작용 0). **git-error** 는 부모 끝 커밋 해소 또는 `checkout -b` 실패(브랜치 미생성·git 이
    남긴 트리 그대로). **record-failed** 는 브랜치는 **만들어졌고** 부모 기록 또는 장부 스냅 재기록이
    실패한 상태다."""

    def __init__(self, slot: str, reason: str, *, detail: str = ""):
        self.slot = slot
        self.reason = reason
        tail = f" — {detail}" if detail else ""
        msg = {
            "unregistered": (f"슬롯 {slot!r} 이 리스 장부에 없다 — task branch 는 등록된 슬롯에만 "
                             f"브랜치를 만든다"),
            "not-owner": (f"슬롯 {slot!r} 은 이 task 가 대여 중인 슬롯이 아니다 — 새 브랜치는 task 소유 "
                          f"슬롯에서만 만든다"),
            "no-worktree": f"슬롯 {slot!r} 의 worktree 경로가 없다 — 브랜치를 만들 트리가 없다",
            "dirty": (f"슬롯 {slot!r} 에 커밋 안 한 변경이 있어 새 브랜치를 만들지 않는다 — 변경을 커밋한 뒤 "
                      f"다시 실행하라"),
            "rebase-in-progress": (f"슬롯 {slot!r} 에 rebase 가 진행 중이라 새 브랜치를 만들지 않는다 — 먼저 "
                                   f"슬롯에서 `git rebase --continue`(충돌 해결 후) 또는 "
                                   f"`git rebase --abort`(취소)로 해소하라"),
            "invalid-ref": (f"슬롯 {slot!r} 에 만들 브랜치명이 git ref 규칙에 어긋나거나 실 브랜치명으로 "
                            f"정규화할 수 없다(`git check-ref-format --branch`) — 다른 이름을 지정하라"),
            "protected": (f"슬롯 {slot!r} 에 보호 브랜치 이름으로 새 브랜치를 만들지 않는다"
                          f"(보호목록=areas.md `protected`)"),
            "exists": (f"슬롯 {slot!r} 에 만들 브랜치가 이미 로컬 브랜치로 있다 — 기존 브랜치로 옮기려면 "
                       f"`switch {slot} <브랜치>` 를 쓴다"),
            "ambiguous-ref": (f"슬롯 {slot!r} 에 만들 브랜치명이 로컬 브랜치가 아닌 다른 ref(remote-tracking·"
                              f"태그)를 가리킨다 — 새 로컬 브랜치명을 지정하라(예 `task/<작업이름>`)"),
            "df-conflict": (f"슬롯 {slot!r} 에 만들 브랜치명이 기존 브랜치와 D/F 충돌한다 "
                            f"(접두 부모 ref 가 이미 브랜치로 존재 → `checkout -b` 가 `cannot lock ref` "
                            f"로 실패) — 다른 이름을 지정하라"),
            "parent-missing": (f"슬롯 {slot!r} 에 새 브랜치를 만들 부모 브랜치가 로컬에도 origin 에도 없다 — "
                               f"실재하는 부모 브랜치를 지정하라"),
            "git-error": f"슬롯 {slot!r} 새 브랜치 생성 중 git 오류",
            "record-failed": (f"슬롯 {slot!r} 에 브랜치는 만들어졌으나 기록(부모 기록 또는 장부 스냅)에 "
                              f"실패했다 — 이대로면 부모 기록이 빠지거나 다음 0단계가 '기록↔live diverged' "
                              f"로 막는다"),
        }.get(reason, f"슬롯 {slot!r} task branch 거부({reason})")
        super().__init__(msg + tail)


class SlotResolutionError(RuntimeError):
    """CLI dev/sync 의 현재-슬롯 자동해소 실패 — 매칭 leased 슬롯 0개/≥2(모호) 또는 슬롯 형식 오류.

    메시지는 세션명 + 후보 슬롯(또는 형식 위반) + `--slot` 안내를 담는다(호출부 main 이 그대로
    stderr surface). (`RuntimeError` 서브클래스 — CLI main 이 `except SlotResolutionError` 로 rc 1.)
    """


class SubmoduleNotInSlot(ValueError):
    """dev 대상 submodule 이 슬롯의 실제 submodule 목록에 없다 — 슬롯 경계 밖 checkout 차단 (must-fix).

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
