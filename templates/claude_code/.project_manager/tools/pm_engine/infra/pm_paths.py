"""PM 홈 경로 해소 (층: infra).

`Home` 을 만드는 곳은 `home_at` 하나다. board 루트 graceful 규칙·areas.md 위치 규칙·대여 장부와 회귀·라이브 게이트
기록 위치·wiki 문서와 채택자 훅 위치·`~` 확장 규칙과 작업 폴더의 소유 PM 홈 유도(`owning_pm_home`)를 소유한다. 설치 깊이는 고정이고 상향 탐색을
하지 않는다.
"""
from __future__ import annotations

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
# 도구(`worktree_pool.bare_repo_path` = `pm_config.REPOS_DIR / f"{repo}.git"`)가 써 넣은
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

    판정 입력은 `pm_paths.owning_pm_home` 유도 하나다: anchor 의 `.git` 포인터가 가리키는 공용
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
