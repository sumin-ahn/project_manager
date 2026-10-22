"""PM 홈 경로 해소 (층: infra).

`Home` 을 만드는 곳은 `home_at` 하나다. board 루트 graceful 규칙·areas.md 위치 규칙·대여 장부와 회귀·라이브 게이트
기록 위치·wiki 문서와 채택자 훅 위치·`~` 확장 규칙과 작업 폴더의 소유 PM 홈 유도(`owning_pm_home`)를 소유한다. 설치 깊이는 고정이고 상향 탐색을
하지 않는다.
"""
from __future__ import annotations

from pm_engine.domain import delegate_errors
import subprocess

from typing import NamedTuple
from pm_engine.domain import ticket_rules

from pathlib import Path

from pm_engine.domain import home
from pm_engine.infra import fs


def _board_root_at(repo: Path) -> Path:
    """`repo`가 직접 소유한 board 루트를 graceful 규칙 하나로 해소한다."""
    base = repo / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board"
    return base / "wiki"


def home_at(root: Path, pm_home: Path | None = None, board_root: Path | None = None) -> home.Home:
    """`root` 트리의 실행 기준 경로를 만든다.

    `pm_home` 은 PM 입력(클론별 설정·작업 디렉터리)을 읽을 PM 홈, `board_root` 는 board 루트다.
    둘 다 None 이면 `root` 자신의 것을 쓴다. board read 폴백만 둘을 넘긴다.
    """
    pm_dir = (pm_home if pm_home is not None else root) / ".project_manager"
    return home.Home(
        root=root,
        board_root=board_root if board_root is not None else _board_root_at(root),
        local_dir=pm_dir / ".local",
        local_conf=pm_dir / "local.conf",
    )


def leases_file(home) -> Path:
    """worktree 대여 장부 경로 (`worktree_pool` 의 LEASES_FILE 과 같은 위치)."""
    return home.local_dir / "worktree-leases.json"


def regression_flag(home) -> Path:
    """회귀 기록 경로 (단일-lease 공유 기록)."""
    return home.local_dir / "regression.json"


def livegate_flag(home) -> Path:
    """라이브 게이트 기록 경로 (보호 훅이 없는 형상의 기록 위치)."""
    return home.local_dir / "livegate.json"


def tickets_dir(home) -> Path:
    """ticket 디렉토리 — board 루트/tickets (board/ 분리 추종·legacy=wiki/tickets)."""
    return home.board_root / "tickets"


def template_file(home) -> Path:
    """ticket 본문 템플릿 — tickets_dir()/_template.md (board_root 추종)."""
    return tickets_dir(home) / "_template.md"


def drafts_dir(home) -> Path:
    """미충전 draft 격리 디렉토리 — tickets_dir()/.drafts (board_root 추종).

    `board.py new` 가 board-git 활성(공유) 상태에서 placeholder/thin 본문을 감지하면
    티켓을 `tickets/open/` 이 아니라 이 디렉토리에 둔다 — STATUS_DIRS(`open/claimed/
    blocked/done/discarded`) *밖*이라 STATUS_DIRS 를 순회하는 어떤 mutation(`_board_git_stage_and_commit`
    의 `git add -A`·`_board_git_status_porcelain` 의 `git status --porcelain`·list·lint 등)도
    draft 를 보지 못한다 — 다음 mutation 이 무관 draft 를 실수로 board-git 에 커밋하는 leak
    을 원천 차단한다.
    `_BOARD_GIT_DRAFT_PATHSPEC` 로 board-git 호출에서도 이중으로 명시 제외한다(방어적 이중화).
    본문을 채운 뒤 `board.py promote <id>` 가 `open/` 으로 이동 + board-git 커밋한다.
    """
    return tickets_dir(home) / ".drafts"


def areas_file(home) -> Path:
    """areas 레지스트리 경로 (board_root 추종·*조건분기*).

    areas.md 는 legacy 에서 `.project_manager/areas.md`(wiki *밖*·committed shared registry)에
    산다 — tickets 처럼 wiki/ 안이 아니다. board/ 분리 시엔 board submodule *안*으로 옮겨야
    PM 운영(repo add 가 append)이 코드 git 을 오염하지 않는다. 그래서:
      - board/ 존재 → `home.board_root/areas.md` (= board/areas.md·submodule 안)
      - legacy     → `<REPO>/.project_manager/areas.md` (현 위치·wiki 밖·무변경)
    """
    root = home.board_root
    if root.name == "board" and (root / "tickets").is_dir():
        return root / "areas.md"
    return root.parent / "areas.md"


def wiki_dir(home) -> Path:
    """PM 운영 wiki 루트 — `local_dir` 와 같은 `.project_manager` 아래(read 폴백이면 소유 PM 홈의 것)."""
    return home.local_dir.parent / "wiki"


def decisions_dir(home) -> Path:
    """ADR 디렉토리 — wiki/decisions."""
    return wiki_dir(home) / "decisions"


def specs_dir(home) -> Path:
    """spec 디렉토리 — wiki/specs."""
    return wiki_dir(home) / "specs"


def ideas_dir(home) -> Path:
    """idea 디렉토리 — wiki/ideas."""
    return wiki_dir(home) / "ideas"


def status_file(home) -> Path:
    """모듈 구현 상태 문서 — wiki/status.md."""
    return wiki_dir(home) / "status.md"


def architecture_file(home) -> Path:
    """현재-아키텍처 문서 — wiki/architecture.md."""
    return wiki_dir(home) / "architecture.md"


def domain_dir(home) -> Path:
    """domain 지식 페이지 디렉토리(wiki/domain)."""
    return wiki_dir(home) / "domain"


def engine_manifest(home) -> Path:
    """엔진 출하 manifest 경로(wiki와 같은 PM 홈)."""
    return home.local_dir.parent / "engine.manifest"


def hooks_dir(home) -> Path:
    """채택자 lint 훅 디렉토리 — `.project_manager/hooks`(wiki 와 같은 PM 홈)."""
    return home.local_dir.parent / "hooks"


def _rel_to_repo(home, path: Path) -> str:
    """Best-effort pretty path. Falls back to absolute when path is outside REPO
    (e.g. in unit tests using tmp_path)."""
    try:
        return str(path.relative_to(home.root))
    except ValueError:
        return str(path)


def _git_common_dir_from_files(repo: Path) -> Path | None:
    """``repo/.git`` 포인터만으로 공용 Git 디렉토리를 해소한다.

    linked worktree의 ``.git`` 파일은 실제 git-dir을, 그 아래 ``commondir``은 공용
    저장소를 가리킨다. snapshot 빌더의 subprocess 0 계약을 넓히지 않으면서 외부 absolute
    lease slot도 그 저장소를 소유한 PM 홈으로 역추적할 수 있게 하는 순수 파일 seam이다.
    """
    repo = Path(repo).resolve(strict=False)
    try:
        dot_git = repo / ".git"
        if dot_git.is_dir():
            git_dir = dot_git
        elif dot_git.is_file():
            pointer = fs.read_text_shared(dot_git, encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            target = Path(pointer[len("gitdir:"):].strip())
            git_dir = target if target.is_absolute() else repo / target
        else:
            return None

        common_pointer = git_dir / "commondir"
        if not common_pointer.is_file():
            return git_dir.resolve(strict=False)
        raw_common = fs.read_text_shared(common_pointer, encoding="utf-8").strip()
        if not raw_common:
            return None
        common = Path(raw_common)
        return (common if common.is_absolute() else git_dir / common).resolve(
            strict=False
        )
    except (OSError, UnicodeError, ValueError):
        return None


class PmHomeResolutionError(RuntimeError):
    """anchor 자신의 선언에서 소유 PM 홈을 확정하지 못한 오류."""


# 두-git 형상의 공유 bare 저장소는 PM 홈 안 `.repos/<repo>.git` 에 놓인다 — 슬롯을 만든
# 도구(`bare_repo_path` = `pm_config.REPOS_DIR / f"{repo}.git"`)가 써 넣은
# 자리라 조상 추측이 아니라 선언이다.
_GIT_DIR_NAME = ".git"
_BARE_REPOS_DIR_NAME = ".repos"


def _pm_home_from_common_dir(common_dir: Path) -> Path | None:
    """공용 Git 저장소 경로에서 그 저장소를 만든 PM 홈을 되돌린다(선언 형상 2종)."""
    if common_dir.name == _GIT_DIR_NAME:
        # `<X>/.git` — 단일-git PM 홈이 자기 checkout 에서 판 worktree.
        return common_dir.parent
    if (
        common_dir.suffix == _GIT_DIR_NAME
        and common_dir.parent.name == _BARE_REPOS_DIR_NAME
    ):
        # `<X>/.repos/<repo>.git` — worktree 풀이 만든 두-git 슬롯과 그 슬롯에서 판 스냅샷.
        return common_dir.parent.parent
    return None


def owning_pm_home(anchor: Path) -> Path:
    """anchor 자신이 들고 있는 선언만으로 소유 PM 홈을 유도한다 — 답은 하나이거나 예외다.

    입력은 anchor 의 `.git` 포인터(슬롯을 만든 도구가 쓴 값) 하나다. 조상 훑기·cwd·환경·
    subprocess 를 쓰지 않으므로 같은 모양의 트리는 파일시스템 어디에 있어도 같은 답을 낸다.

      - `.git` 없음/디렉터리        → anchor 자신 (아무의 linked worktree 도 아니다)
      - `.git` 파일 → `<X>/.repos/<repo>.git` 또는 `<X>/.git` → `X`
      - 그 밖의 commondir           → 실패

    유도된 `X` 는 `<X>/.project_manager` 실재를 요구하고, 부재는 실패다 — 못 받으면 anchor
    자신으로 강등하지 않는다.

    lease 장부는 판정 입력이 아니다. 그 파일은 어느 슬롯을 누구에게 빌려줬는지 적는 슬롯 배정
    원장이지 소유 증거가 아니며, gitignored·per-clone 이라 anchor 의 함수도 아니다. 소유는
    commondir 형상이 이미 정한다 — `<X>/.repos/<repo>.git` 이면 X 가 그 bare 를 만든 주체이고
    `<X>/.git` 이면 X 가 main checkout 이다.
    """
    anchor = Path(anchor).resolve(strict=False)
    if not (anchor / _GIT_DIR_NAME).is_file():
        # 없음(합성 트리·비-git 채택 폴더) · 디렉터리(PM 홈 main checkout·일반 clone).
        return anchor
    common_dir = _git_common_dir_from_files(anchor)
    if common_dir is None:
        raise PmHomeResolutionError(
            f"{anchor}: `.git` 포인터에서 공용 Git 저장소를 읽지 못했습니다."
        )
    pm_home = _pm_home_from_common_dir(common_dir)
    if pm_home is None:
        raise PmHomeResolutionError(
            f"{anchor}: 공용 Git 저장소 {common_dir} 형상에서 소유 PM 홈을 찾지 못했습니다."
        )
    pm_home = pm_home.resolve(strict=False)
    if not (pm_home / ".project_manager").is_dir():
        raise PmHomeResolutionError(
            f"{anchor}: 공용 Git 저장소 {common_dir} 의 소유 PM 홈을 찾지 못했습니다 — "
            f"{pm_home} 에 .project_manager 가 없습니다."
        )
    return pm_home


def _expanduser_path(path: Path) -> Path:
    """`~`/`~user` 확장의 플랫폼 seam — 회귀가 Windows 확장 동작을 주입해 태울 수 있게 연 자리."""
    return path.expanduser()


def _expanded_user_path(value: str) -> Path:
    """`~`/`~user` 를 확장한다 — **없는 사용자는 플랫폼과 무관하게 해소 실패**(RuntimeError).

    POSIX 의 `expanduser()` 는 `pwd` 조회 실패를 RuntimeError 로 올리지만, Windows 는 존재하지
    않는 사용자도 `C:\\Users\\<name>` 으로 조립해 준다. 그래서 같은 입력이 한쪽에서만 해소되고
    뒤 단계가 다른 분기를 탄다 — 소유 repo 해소는 "해소 실패"에서 멈춰야 하는데 Windows 에서는
    phantom 홈 경로로 내려가 "경로 부재/이동" 같은 **다른 사유**로 갈렸다(보호 훅 설치도 같은
    자리에서 phantom 홈 경로로 raw installer 까지 갔다). `~user` 형태는 **그 사용자 홈의 실재**를
    판정에 넣어 두 플랫폼이 같은 결론에 도달하게 한다.

    `~`(현재 사용자)는 두 플랫폼이 같은 환경변수 축(HOME/USERPROFILE)으로 확장하므로 분기가
    갈리지 않는다 — 실재 검사를 걸지 않는다(홈 디렉터리가 아직 없는 정상 형상까지 막지 않게).
    소유 repo 해소(board)·`core.hooksPath` 확장(pre_push_hook)·보호 훅 설치(pm_config)가 이 함수를 쓴다.
    """
    path = Path(value)
    expanded = _expanduser_path(path)
    first = path.parts[0] if path.parts else ""
    if first.startswith("~") and len(first) > 1:
        home = _expanduser_path(Path(first))
        if not home.is_dir():
            raise RuntimeError(
                f"`{first}` 사용자 홈을 해소하지 못했다(없는 사용자 또는 홈 부재): {home}")
    return expanded


class AnchorResolutionError(RuntimeError):
    """명시 입력에서 diff/board/config 앵커를 유일하게 해소하지 못한 오류."""


def resolve_pm_home_for_repo(anchor: Path) -> Path:
    """repo/worktree가 소속된 PM 홈을 anchor 자신의 선언으로 해소한다 — 답은 하나이거나 예외다.

    판정 입력은 `owning_pm_home` 유도 하나다: anchor 의 `.git` 포인터가 가리키는 공용
    저장소가 소유 PM 홈을 지목하고, 그 홈에 `.project_manager` 가 실재하는지만 확인한다.
    `.git` 이 없거나 디렉터리인 트리(PM 홈 main checkout·일반 clone)는 자기 자신이라는
    확정된 답이다. 미지 commondir·`.project_manager` 부재는 `AnchorResolutionError` 다 —
    자기 앵커로 강등해 계속 진행하지 않는다.

    앵커가 실 board 를 갖는지는 판정 입력이 아니다. 사본 안에 실 ticket 이 있다는 이유로
    조기 반환하면, 소유자를 확정하지 못한 트리가 휘발 장부에 라운드를 기록할 수 있다.
    """
    anchor = anchor.resolve()
    try:
        pm_home = owning_pm_home(anchor)
    except PmHomeResolutionError as exc:
        raise AnchorResolutionError(str(exc)) from exc
    return pm_home


def slot_path(home, slot: str) -> Path:
    """슬롯 식별자(`work/<repo>_<N>`) → 절대 경로 (REPO 기준)."""
    return home.root / slot


def bare_repo_path(home, repo: str) -> Path:
    """repo 이름 → 그 repo 의 공유 .git 원 경로 `.repos/<repo>.git` (bare).

    worktree 슬롯이 add/remove 되는 git 컨텍스트. `pm_config.REPOS_DIR / f"{repo}.git"` 와
    같은 관례 — worktree 풀이 import 격리(board·pm_config 미import)라 자체 해소한다.
    """
    return repos_dir(home) / f"{repo}.git"


def task_dir(home, name: str) -> Path:
    """task 서술 공간 `.local/tasks/<name>/` 경로 (pm_state.md·메타). 기계 상태는 장부·서술만 여기."""
    return tasks_dir(home) / name


def task_pm_state_file(home, name: str) -> Path:
    """task 연속성 단일 앵커 `.local/tasks/<name>/pm_state.md` 경로."""
    return task_dir(home, name) / "pm_state.md"


def task_todo_file(home, name: str) -> Path:
    """이 task 의 열린 할 일 파일 `.local/tasks/<name>/todo.json` 경로."""
    return task_dir(home, name) / "todo.json"


def _task_todo_lock_file(home, name: str) -> Path:
    """열린 할 일 파일의 배타 락 — task 폴더 안이라 task 끼리 경합하지 않는다."""
    return task_dir(home, name) / "todo.lock"


def _task_pm_state_template_file(home) -> Path:
    """task state seed 원천 — 호출 시점 REPO를 따라 hermetic 재배선과 실제 실행을 함께 지원."""
    return home.root / ".project_manager" / "wiki" / "pm_state.template.md"


# 하네스 세션 ID 기록 — 부트스트랩 프로세스는 자기 하네스 세션 ID 를 알 수 없다(codex·opencode 는
# 도구 프로세스 환경에 없다·실측). 세션 ID 를 받는 가드 훅이 `pm_bootstrap.py --task X` 호출을 보고
# 여기에 적고, 부트스트랩이 가져가(`take_task_session_claim`) task 기록에 쓴다.
TASK_SESSION_CLAIMS_DIRNAME = "task-session-claims"


def _task_session_claim_path(home, name: str) -> Path:
    """세션 기록 파일 자리 — `LOCAL_DIR` 을 부를 때마다 읽는다(홈을 바꿔 쓰는 소비자와 같은 자리)."""
    return home.local_dir / TASK_SESSION_CLAIMS_DIRNAME / f"{name}.json"


def leases_lock(home) -> Path:
    return home.local_dir / "worktree-leases.lock"


def tasks_dir(home) -> Path:
    return home.local_dir / "tasks"


def work_dir(home) -> Path:
    return home.root / "work"


def repos_dir(home) -> Path:
    return home.root / ".repos"


def repo_hooks_dir(home) -> Path:
    return home.local_dir / "repo-hooks"


def _has_real_board(pm_dir: Path) -> bool:
    """`.project_manager` 디렉토리(`pm_dir`)가 *실제* board 를 소유하는가.

    board/ 분리(submodule)면 `board/tickets`, legacy 면 `wiki/tickets` 의 상태
    디렉토리에 실 티켓(`T-*.md`)이 하나라도 있으면 True. 빈 scaffold(README/_template/
    `.gitkeep` 만 — worktree 출하 형상)는 False: worktree 자신의 scaffold 를 '실 board' 로
    오인해 가드를 무력화하지 않게 한다.
    """
    for base in (pm_dir / 'board' / 'tickets', pm_dir / 'wiki' / 'tickets'):
        if not base.is_dir():
            continue
        for status in ticket_rules.STATUS_DIRS:
            status_dir = base / status
            if status_dir.is_dir() and any(status_dir.glob('T-*.md')):
                return True
    return False

def _pm_home_worktree_misanchor(home) -> Path | None:
    """`anchor`(도구 자기-앵커=REPO)가 **다른 PM 홈의 등록 worktree** 면 그 PM 홈 경로를,
    아니면 None.

    소유 판정은 `owning_pm_home` 유도 하나가 낸다 — anchor 자신이 실 board 를 가지면
    자기 것이고(자체 board 사용 존중), 그 밖에는 anchor 의 `.git` 선언이 답을 정한다. 유도가
    실패하면 자기 앵커로 강등하지 않고 그대로 올린다(fail-loud).
    """
    if _has_real_board(home.root / '.project_manager'):
        return None
    pm_home = owning_pm_home(home.root)
    return None if pm_home == Path(home.root).resolve() else pm_home

class _ReadBoardResolution(NamedTuple):
    """read dispatch의 board 해소 결과. home이 None이면 자기 앵커의 board를 그대로 쓴다."""
    root: Path
    home: Path | None

def _resolve_read_board(home) -> _ReadBoardResolution:
    """read-only board를 uniform 규칙으로 해소한다.

    자기 앵커가 실제 board를 가지면 그대로 쓴다. 그 밖에는 `owning_pm_home` 유도가
    소유 PM 홈을 낸다 — 유도 결과가 앵커 자신이면 legacy/비분리 graceful 경로를 그대로
    보존하고, 다른 홈이면 그 board를 연다. 유도 실패는 여기서 삼키지 않는다 — 같은 실패가
    mutation 경로에서는 traceback, read 경로에서는 안내로 갈리지 않도록 번역은 `main()`
    한 곳이 한다.
    """
    if _has_real_board(home.root / '.project_manager'):
        return _ReadBoardResolution(_board_root_at(home.root), home.root)
    data_home = owning_pm_home(home.root)
    if data_home == Path(home.root).resolve():
        return _ReadBoardResolution(_board_root_at(home.root), None)
    return _ReadBoardResolution(_board_root_at(data_home), data_home)



def _repo_root_for_cwd(cwd: Path) -> Path:
    """명시 cwd를 prepare/harvest가 공유하는 제품 Git 루트로 정규화한다.

    앱 checkout은 `.project_manager`를 소유하지 않아도 된다. 보드·config의 PM 홈 소유
    해소는 `_ticket_cli_owner`/lease 경계가 별도로 담당한다.
    """
    if not cwd.is_absolute():
        raise delegate_errors.DelegateError("--cwd 는 절대경로여야 합니다")
    resolved = cwd.resolve()
    return Path(repo_root_from_cwd(resolved) or resolved).resolve()




def _path_repo_root(path: Path) -> Path | None:
    """존재 경로(파일이면 부모)에서 가장 가까운 git repo 루트를 반환한다."""
    start = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _scope_from_initial_pm_home(*, ticket_selected: bool, explicit_paths: bool) -> bool:
    """이번 검토 범위가 **최초 PM 홈**에서 파생됐는가 (diff 소유자 대조 대상).

    명시 `--paths` 나 유효 ticket touches 가 범위를 완전 지정하지 않은 실행은 최초 PM 홈 config
    를 읽어 범위를 얻는다. 그 config 가 `additional_reviewer.paths` 를 선언했든 레지스트리 기본
    경로로 떨어졌든 **범위의 출처는 최초 PM 홈 하나**라, 표시 conf 와 실제 호출
    범위가 갈리는 위험은 같다 — 그래서 선언 유무를 판정 입력으로 쓰지 않는다.
    """
    return not ticket_selected and not explicit_paths


# ── PM 홈 앵커 재지정 감지 (adopter#0 false-green 게이트) ────────────────
# additional_reviewer 의 import 사본은 PM 홈에 있어 REPO 가 PM 홈으로
# 해소된다 — 실 코드 변경은 canonical worktree에 있으므로 `git diff` 가 비어 codex 가 "변경
# 없음"을 통과로 판정하는 false-green 이 난다.
# board.py `_pm_home_worktree_misanchor`의 *역방향*: 거긴 worktree 에서 실행된 board 조작을
# 잡고, 여긴 PM 홈에서 실행된 추가 리뷰를 잡아 worktree 로 재지정한다. 순수 filesystem 판정(subprocess
# 불요)이라 hermetic — REPO 를 module-level 로 두어 테스트가 monkeypatch 하고, 헬퍼는 anchor/conf 를
# 명시 인자로 받아 DI seam 이 된다. 판정 *형태*는 board `_has_real_board` 동형이되 상태 집합은
# 복제하지 않고 단일 진실(`ticket_rules.STATUS_DIRS`)을 승계한다 — 사본은 새 상태에서 갈린다.

def _owns_real_board(pm_dir: Path) -> bool:
    """`.project_manager` 디렉토리(`pm_dir`)가 실 티켓(`T-*.md`)을 가진 board 를 소유하는가.

    board/ 분리면 `board/tickets`, legacy 면 `wiki/tickets` 상태 디렉토리에 실 티켓이
    하나라도 있으면 True. 빈 scaffold(README/_template 만 — worktree 출하 형상)는 False (worktree
    자신을 PM 홈으로 오인해 가드 오탐 내지 않게·board.py `_has_real_board` 동형)."""
    for base in (pm_dir / "board" / "tickets", pm_dir / "wiki" / "tickets"):
        if not base.is_dir():
            continue
        for status in ticket_rules.STATUS_DIRS:
            status_dir = base / status
            if status_dir.is_dir() and any(status_dir.glob("T-*.md")):
                return True
    return False


def _canonical_worktree(anchor: Path) -> Path | None:
    """adopter#0 PM 홈 `anchor` 의 canonical 코드 worktree(재지정 대상) 경로 — 없으면 None.

    `<anchor>/work/*` 스캔 중 엔진 사본(`.project_manager/tools/additional_reviewer.py`)을 가진 첫
    디렉토리를 반환한다(슬롯 생성 도구가 쓰는 `work/<name>` 경로 관례와 동형). 없으면
    None(무관 형상·재지정 대상 없음).

    local.conf `upstream.path` 은 재지정 대상 결정에 **쓰지 않는다** — upstream 은 URL 이거나 무관한
    로컬 checkout(`pm_import --from <로컬>`)일 수 있어, 실 board 를
    소유한 정규 채택자에서 stale/무관 checkout 으로 오안내하며 정상 리뷰를 hard-block 한다(빈-diff
    백스톱도 실 diff 가 non-empty 면 무력). `work/` 슬롯 스캔만으로 adopter#0재지정을
    완전 커버하고, upstream 분기는 잉여+오탐만 더한다 — 제거가 동등 커버리지·최소·오탐 0(codex/reviewer
    이중 게이트 수렴 must-fix)."""
    work_dir = anchor / "work"
    if work_dir.is_dir():
        for sub in sorted(work_dir.iterdir()):
            if (sub / ".project_manager" / "tools" / "additional_reviewer.py").is_file():
                return sub
    return None


def _pm_home_reanchor(anchor: Path) -> Path | None:
    """`anchor`(REPO=도구 자기-앵커)가 adopter#0 PM 홈이면 재지정 대상 worktree 를, 아니면 None.

    2중 conjunction (오탐 0 지향·fail-soft): (1) anchor 가 실 board 소유(PM 홈) — worktree(코드
    전용·board 미소유)에서 실행하면 여기서 탈락해 None(정상·재지정 불요), (2) anchor 아래 canonical
    코드 worktree(`work/<name>`) 존재. 일반 채택자(로컬 upstream 포함)는 (1) 또는 (2) 미충족으로
    None(무영향)."""
    # additional_reviewer.main은 명시 selector 기반 diff_root 해소로 전환되어 이 헬퍼를 호출하지 않는다.
    # pm_delegate.check_write_target_reanchor가 adopter write-target 보호에 계속 사용하므로 유지한다.
    if not _owns_real_board(anchor / ".project_manager"):
        return None
    return _canonical_worktree(anchor)


def repo_root_from_cwd(cwd: Path | None) -> Path | None:
    """호출 cwd/`--cwd`에서 가장 가까운 제품 repo 루트를 해소한다.

    Git `rev-parse --show-toplevel`을 써 linked worktree의 `.git` 파일과 일반 checkout의
    `.git` 디렉토리를 같은 근거로 본다. 제품 repo에 `.project_manager`를 요구하지
    않는다. PM 홈은 호출부가 `resolve_pm_home_for_repo`로 lease 장부에서 별도
    해소한다. cwd 미지정/비-repo면 None이며 divergence 가드는 조용히 skip한다.
    """
    if cwd is None:
        return None
    return _path_repo_root(cwd.resolve())
