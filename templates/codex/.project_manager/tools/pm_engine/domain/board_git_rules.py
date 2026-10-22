"""보드 git 결과 자료형과 거부 상수 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from pm_engine.domain import gitattributes_rules
from pm_engine.domain import ticket_rules


# board git 에 **엔진이 넣는 텍스트 파일**의 확장자. 지금 실재하는 건 티켓·라운드·areas 의 `.md`
# 뿐이지만 표기 고정을 한 확장자에 매달 이유가 없다 — 엔진이 board 에 넣는 어떤 텍스트도 같은
# 표기로 체크아웃돼야 회수 bytes 보존과 재작성 byte 판정이 개행으로 갈리지 않는다.
# 채택자가 board 에 둔 다른 형식은 대상이 아니다(우리 소유 확장자만 선언한다).
_BOARD_TEXT_ATTR_EXTENSIONS: tuple[str, ...] = ("md", "json", "txt", "jsonl")


# 커버리지 판정 대상 — 상태 디렉토리 × 확장자(중첩 경로) ∪ board 루트 직계(앵커드 선언 탐지).
_BOARD_TEXT_ATTR_TARGETS: tuple[str, ...] = tuple(
    f"tickets/{status}/T-0000-ticket.{extension}"
    for status in (*ticket_rules.STATUS_DIRS, ".drafts")
    for extension in _BOARD_TEXT_ATTR_EXTENSIONS
) + tuple(f"board-root-file.{extension}" for extension in _BOARD_TEXT_ATTR_EXTENSIONS)


_BOARD_GITATTRIBUTES_BLOCK = (
    "# areas.md = 멀티-PM prefix 레지스트리 — 동시 등록(행 append)이 merge 에서 충돌하지 않도록\n"
    "# git 내장 union merge 드라이버로 양쪽 행을 모두 보존한다.\n"
    "# board 는 별도 git 이라 superproject 루트의 같은 선언이 닿지 않는다 — 여기가 그 배포처다.\n"
    f"areas.md merge={gitattributes_rules._AREAS_UNION_DRIVER}\n"
    "# Windows checkout에서도 엔진-소유 텍스트의 논리 개행을 LF로 유지한다(라운드 회수 bytes·\n"
    "# 재작성 byte 판정). 엔진의 표기 보존이 근본이고 이 선언은 그 위의 방어층이다.\n"
    + "".join(
        f"*.{extension} text eol=lf\n" for extension in _BOARD_TEXT_ATTR_EXTENSIONS)
)


# board 루트에 엔진이 배포하는 파일들. ensure/판정/snapshot/pathspec 이 모두 이 seam 을 공유해
# 한 파일만 WIP 보호에서 빠지는 drift 를 막는다. 순서는 pathspec·복구 안내의 나열 순서다.
_BOARD_GIT_ROOT_FILES: tuple[str, ...] = (".gitattributes", ".gitignore")


class _BoardGitRootFileBackfillTarget(NamedTuple):
    """루트 파일 backfill 이 안전하다고 판정된 append 대상."""
    path: Path
    text: str


# `text` 를 켜는 표기 두 가지 — 명시 set(`text`)과 자동 판정(`text=auto`). 후자도 텍스트로
# 판정된 파일엔 `eol` 이 그대로 적용되므로(git 실측) 이미 배포된 것으로 인정한다. 인정하지
# 않으면 `* text=auto eol=lf` 를 쓴 채택자 board 에 같은 규칙을 매번 덧붙인다.
_GITATTRIBUTES_TEXT_ENABLED: frozenset[str] = frozenset({"set", "auto"})


# `tickets/.drafts/`(미충전 draft)는 **설계상 board-git 미커밋**이다(draft authoring flow 결정 —
# placeholder 본문이 공유 board 에 올라가는 것을 막는다). mutation 축엔 그 의도가 이미 박혀
# 있지만(`_BOARD_GIT_SCOPE_EXCLUDE`·`_BOARD_GIT_DRAFT_PATHSPEC`), **관측 축**엔 아무 선언도 없어
# draft 파일이 untracked 로 남는다 — dirty-tree 를 재는 소비자(`pm_handoff` [0/7] 게이트·사람의
# `git status`)가 그걸 매번 "기록 누락 잔여" 로 오탐해 핸드오프마다 `--ack-dirty` override 를
# 요구했다(핸드오프 관측 결과). board git 의 `.gitignore` 에 그 디렉토리를 선언해 오탐을 원천에서
# 없앤다 — 새 판정 규칙이 아니라 이미 있던 의도(미커밋)를 git 에 명시하는 것이다.
_BOARD_GIT_DRAFT_IGNORE_PATTERN = "tickets/.drafts/"


# 위 패턴과 **같은 디렉토리**를 가리키는 것으로 인정하는 정규화 표기. 슬래시 없는 `.drafts` 는
# 비앵커드일 때만 git 규칙상 어느 깊이의 그 디렉토리에도 걸린다.
_BOARD_DRAFT_IGNORE_FORMS: tuple[str, ...] = (
    _BOARD_GIT_DRAFT_IGNORE_PATTERN.rstrip("/"),
    _BOARD_GIT_DRAFT_IGNORE_PATTERN.rstrip("/").rsplit("/", 1)[-1],
)


_BOARD_GITIGNORE_BLOCK = (
    "# tickets/.drafts/ = 미충전 draft 티켓 — 설계상 board-git 미커밋(promote 가 open/ 으로 옮긴다).\n"
    "# 선언하지 않으면 draft 가 untracked 로 남아 dirty-tree 관측이 매번 미커밋 잔여로 오탐한다.\n"
    f"{_BOARD_GIT_DRAFT_IGNORE_PATTERN}\n"
)


class _BoardGitRootFileState(NamedTuple):
    """strict mutation 전 board 루트 파일의 워킹트리 상태."""
    existed: bool
    contents: bytes | None


class _BoardGitRootFilesSnapshot:
    """strict claim 의 루트 backfill 전후 소유 경계를 함께 기록한다."""

    def __init__(self, states: dict[str, _BoardGitRootFileState] | None):
        self.states = states
        self.backfilled: list[str] = []
        self.staged_index: dict[str, tuple[str, str]] | None = None


# claim 차단 사유 코드 — prefetch 가 claim 을 막을 때의 *원인* 이다. 옛
# sentinel 문자열(`\0dirty`·`\0detached`)을 대체한다: 사유가 넷(dirty/rebase 충돌/offline/
# upstream 없음)으로 갈리고 진단에 behind·더러운 파일 목록이 함께 필요해, 반환을 구조체
# (`_ClaimPrefetch`)로 바꾸면서 사유도 코드로 명시했다. 하나의 원인엔 하나의 안내만 나가야
# 한다.
_CLAIM_BLOCK_DETACHED = "detached"          # board HEAD 가 브랜치를 안 가리킴 → checkout 안내


_CLAIM_BLOCK_REBASE = "rebase"              # rebase 진행 중(충돌 미해소) → abort/continue 안내


_CLAIM_BLOCK_RACE_LOST = "race-lost"        # 원격에서 이미 claimed/done/blocked → 중복작업 방지


_CLAIM_BLOCK_DIRTY = "dirty"                # 원격 앞섬 + 미커밋 파일이 통합을 막음(offline 아님)


_CLAIM_BLOCK_INTEGRATION = "integration"    # 네트워크 정상·미커밋 0 인데 통합 실패(원인 미상)


_CLAIM_BLOCK_OFFLINE = "offline"            # fetch 도달 불가(네트워크·auth)


_CLAIM_BLOCK_NO_UPSTREAM = "no-upstream"    # 추적 브랜치 미설정 → 조율할 원격이 없음


_CLAIM_BLOCK_NO_ANCHOR = "no-anchor"        # HEAD 조회 불가 → rollback anchor 없이 진행 금지


# 잔여 차단 진단에 나열할 더러운 파일 표본 상한 — 전부 쏟으면(수십 건) 안내가 묻히고, 0 이면
# "무엇 때문에 막혔나" 를 사용자가 다시 찾아야 한다. 총계는 항상 함께 낸다.
_BOARD_GIT_DIRTY_SAMPLE_MAX = 5


# 진단에 인용할 git 출력 길이 상한 — 원인 미상 통합 실패에서 마지막 stderr 줄을 그대로 보여준다
# (분류에는 안 쓴다·로케일 무관 — 사람이 읽을 단서로만).
_BOARD_GIT_DETAIL_MAX = 200


# 사유 코드 → **짧은 라벨**. "무엇 때문인지" 한 마디만 끼워 넣는 자리(롤백 후 stale 경고)용이다
# — 사유별 *처방* 문장은 `_claim_block_message` 가 소유한다(claim 차단이라는 다른 맥락). 판정
# 자체는 두 곳 모두 `_classify_pull_failure` 하나를 쓴다.
_BOARD_GIT_BLOCK_LABELS: dict[str, str] = {
    _CLAIM_BLOCK_REBASE: "rebase 진행 중·충돌 미해소",
    _CLAIM_BLOCK_DIRTY: "미커밋 파일이 통합을 막음",
    _CLAIM_BLOCK_OFFLINE: "원격 도달 불가",
    _CLAIM_BLOCK_INTEGRATION: "원인 미상·네트워크는 정상",
}


# board-git pathspec — `tickets/.drafts/`(drafts_dir()) 를 `git add`/`git status` 에서 명시
# 제외한다. draft 는 STATUS_DIRS 순회 밖이라 이미 안 보이지만, 이 pathspec 은 방어적
# 이중화다 — draft 경로가 board_root 바로 아래(add -A 의 스캔 범위)에 있는 한, 어떤 향후
# 변경으로 draft 가 STATUS_DIRS 스캔 함수에 실수로 노출되더라도 git 수준에서 한 번 더 막는다.
# pathspec 은 `Home.board_root` 기준 상대경로 리터럴이라 위치 이동(legacy↔board 분리) 무관 동작한다.
_BOARD_GIT_DRAFT_PATHSPEC: tuple[str, ...] = (".", ":!tickets/.drafts")


class _BoardGitUpstream(NamedTuple):
    """board git 이 조율하는 원격 — remote 이름 · 로컬 브랜치 · 원격 브랜치 ref."""

    remote: str        # 예: origin
    branch: str        # 로컬 브랜치명
    merge_ref: str     # 예: refs/heads/main (`ls-remote` 조회 키)

    @property
    def tracking(self) -> str:
        """원격-추적 ref (`refs/remotes/<remote>/<branch>`) — ls-tree/rev-list 의 revision."""
        short = self.merge_ref
        if short.startswith("refs/heads/"):
            short = short[len("refs/heads/"):]
        return f"refs/remotes/{self.remote}/{short}"


# 원격에서 이 티켓이 이미 `open` 을 벗어났으면 선점(claim race-lost)이다. STATUS_DIRS 를
# 추종해 유도한다 — 신규 상태(예: 처분 종결 `discarded`)가 생겨도 "open 이 아니면 선점" 이라는
# 판정이 자동으로 그 상태를 덮는다(손-열거하면 새 상태가 조용히 claim 가능해진다).
_CLAIM_RACE_LOST_STATUSES: tuple[str, ...] = tuple(
    status for status in ticket_rules.STATUS_DIRS if status != "open")


# board-git 고유 금지 구역 — `tickets/.drafts/`(미충전 draft)는 어떤 mutation 도
# 커밋하지 않는다. promote 의 *옛* 경로가 실제로 draft 라 이 규칙이 매번 발동한다.
_BOARD_GIT_SCOPE_EXCLUDE: tuple[str, ...] = ("tickets/.drafts/",)


class _BoardGitUncommittedScope(NamedTuple):
    """local commit 실패 진단에 필요한 실제 상태와 복구 pathspec."""
    entries: tuple[tuple[str, str], ...] = ()
    recovery_paths: tuple[str, ...] = ()


class _ClaimPrefetch(NamedTuple):
    """claim 선점 감지 결과 — anchor · 차단 사유 · 진단 재료.

    옛 sentinel 문자열 반환(5분)을 대체한다. 차단 사유가 넷으로 갈리고(dirty / rebase 충돌 /
    offline / upstream 없음) 진단에 `behind` 와 더러운 파일 목록이 함께 필요해, 값 하나로는
    표현이 안 된다 — 사유와 재료를 **한 구조체로 묶어** 호출부가 메시지를 한 곳에서 렌더한다.
    """

    anchor: str | None = None
    """`<sha>` = 정상 진행(rollback 복귀 지점) · `""` = board-git 비활성(legacy) · None = 차단."""
    block: str | None = None
    """`_CLAIM_BLOCK_*` 사유 코드 (None = 진행 가능)."""
    behind: int | None = 0
    """원격이 로컬보다 앞선 커밋 수 — 잔여 차단 진단의 핵심 수치. None = 계산 불가(판정불가)."""
    dirty: tuple[tuple[str, str], ...] = ()
    """차단 시점의 미커밋 변경 `(코드, 경로)` — 안내에 표본 + 총계로 낸다."""
    detail: str = ""
    """사유별 부가 정보 (race-lost 면 원격에서 그 티켓이 놓인 status 등)."""


class _ClaimFiles(NamedTuple):
    """claim 이 만진 두 경로 + 이동 *전* 원본 바이트 — 스코프 커밋·역이동 복원의 입력."""

    old: Path     # open/ 의 원래 경로 (롤백 복귀 지점)
    new: Path     # claimed/ 로 옮겨진 경로
    original: bytes  # 이동 전 파일 바이트(claimed_by/claimed_at 갱신 전)


# advisory freshness fetch 조율 — `list` 는 대화형으로 세션당 수십 회 도는
# 로컬-only 명령이라, 매 호출 원격 fetch(30s timeout)는 offline/VPN-hang 시 호출당 최대 30s 블록·
# online 도 매번 round-trip 이 advisory 1줄 대비 과하다. fetch 전 FETCH_HEAD mtime TTL 가드로
# 직전 fetch 를 cross-process 재사용(별도 캐시 파일 없음)·advisory fetch timeout 을 5s 로 단축.
_FRESHNESS_FETCH_TTL_SECONDS = 60      # 이 창 안이면 직전 fetch 결과 재사용(fetch 생략).


_FRESHNESS_FETCH_TIMEOUT_SECONDS = 5   # advisory fetch 상한(대화형 hang 완화·기존 30s 경로 불변).


class _BoardFreshness(NamedTuple):
    """freshness 1줄 + 그 줄이 근거로 쓴 원격-추적 스냅샷의 **검증 여부**.

    `verified` 는 "이번 조회가 원격을 실측했나" 다 — TTL 이내 직전 fetch 재사용 또는 이번
    fetch 성공일 때만 True. False 면 남아 있는 원격-추적 ref 는 **검증되지 않은 캐시**라
    그것으로 티켓 단위 stale 을 단정할 수 없다(판정 불능을 판정으로 위장하지 않는다).
    freshness 줄 자체는 그 사실을 "판정불가 — 스냅샷일 수 있음" 으로 이미 말한다.
    """

    line: str | None
    verified: bool
