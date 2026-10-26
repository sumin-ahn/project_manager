"""완료 기록·종결의 값 타입·경로/상태 판정·pytest/UTC 규칙·메시지 (층: domain)."""
from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from pm_engine.domain import pytest_report, review_diff_rules


_CLOSE_CODE_TREE_MISSING = (
    "묶음 기록 {key} {branch} 를 체크아웃한 작업 트리가 없다 — 대여 기록의 작업 폴더 {folders}곳이 "
    "속한 저장소의 작업 트리를 모두 보았다. 재배치가 충돌로 멈춘 트리는 목록에 브랜치 없이 "
    "나오므로 그 브랜치를 쥔 것으로 보이지 않는다 — 그 트리의 재배치를 끝내거나 되돌린 뒤 다시 "
    "실행하라."
)


_CLOSE_TREE_AMBIGUOUS = (
    "{branch} 를 체크아웃한 작업 트리가 {count}개다 ({trees}) — 어느 트리에서 돌지 고르지 "
    "않는다. 한 트리만 그 브랜치를 쥐게 한 뒤 다시 실행하라."
)


class _CloseObservationFailure(Exception):
    """close 단계의 **관측 자체가 실패**했다 — 그 단계에서 정지한다.

    "관측했는데 대상이 없다"(커밋할 변경 없음·서브모듈 아님)와 구별한다: 전자는
    건너뛰기이고 이쪽은 판정 불능이다. 판정 불능을 완료·clean·무대상으로 접으면 그 단계가
    실제로 끝났다는 증거 없이 다음 단계로 넘어가고, 마지막에 남는 것은 근거 없는 종결 기록뿐
    이다. 관측이 신뢰의 뿌리라는 불변식은 이 구분으로만 지켜진다."""


_UTC_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)


def _parse_utc_iso(value: object) -> datetime.datetime | None:
    """writer 형식의 ISO-8601 UTC 시각만 datetime 으로 파싱한다 (아니면 None).

    `datetime.fromisoformat` 단독은 공백 separator·offset 생략 등도 받아들인다. 장부 선후는 보안
    경계라 writer 가 내는 `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`(`Z`도 동치)만 받고, 실제 달력값과
    UTC offset 을 다시 확인한다. 문자열 사전순 비교는 손상값 `zzz`가 미래로 통하는 구멍이라 쓰지
    않는다."""
    if not isinstance(value, str) or _UTC_ISO_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == datetime.timedelta(0) else None


# ── pytest 출력 파서 ────────────────────────────────────────────────────
# 요약행 탐색은 `pytest_report._pytest_summary_line`(끝에서-탐색)이 소유한다.
# 출력 전체를 `re.search` 하면 캡처된 로그의 `3 passed`/`1 failed` 를 요약으로 먼저 만나
# 마감 판정을 뒤집는다(false-green·false-RED 양방향·자기 사본 금지).


def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, deselected) 를 파싱한다.

    반환: (passed, deselected) — 파싱 실패 시 None (요약행 부재 포함).

    pytest -q 요약 라인 형식 예:
      "1472 passed, 24 deselected in 12.34s"
      "1472 passed in 12.34s"
      "5 failed, 1467 passed, 24 deselected in 10.00s"

    red (failed > 0) 여부 판단은 호출 측이 한다 (failed 수 포함 파싱은 하지 않음).
    반환값 (passed, deselected) 만 추출한다.
    """
    line = pytest_report._pytest_summary_line(output)
    if line is None:
        return None
    passed = pytest_report._pytest_outcome_count(line, "passed")
    if passed is None:
        return None
    deselected = pytest_report._pytest_outcome_count(line, "deselected") or 0
    return passed, deselected


class TicketFrontmatterSnapshot(NamedTuple):
    """현재 티켓 frontmatter 읽기 결과 — 미발견과 티켓 읽기 실패를 분리한다."""
    frontmatter: dict
    error: str | None


def _clean_touches(value: object) -> list[str]:
    """frontmatter touches 값을 정규 문자열 목록으로 접는다."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value
                if isinstance(item, str) and item.strip()]
    return []


_PM_DIRECT_CODE_SUFFIXES: frozenset[str] = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".kt", ".kts",
    ".php", ".py", ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx",
})


def _is_changed_test_path(path: str) -> bool:
    """repo-relative 변경 경로가 신규/수정 테스트인지 판정."""
    canonical = path.strip().replace("\\", "/")
    parts = tuple(part for part in canonical.split("/") if part and part != ".")
    name = parts[-1] if parts else ""
    return ("tests" in parts or "test" in parts or name.startswith("test_")
            or name.endswith("_test.py") or name.endswith(".test.js")
            or name.endswith(".test.ts") or name.endswith(".spec.js")
            or name.endswith(".spec.ts"))


def pm_direct_finish_warnings(
    tier: object, touches: Sequence[str], changed_paths: Sequence[str], *,
    touched_file_count: int | None = None,
    unresolved_directories: Sequence[str] = (),
) -> tuple[str, ...]:
    """PM-direct 조건 a·b 재검 경고. 반환값은 오직 안내이며 차단은 없다."""
    if tier != "pm-direct":
        return ()
    warnings: list[str] = []
    file_count = len(touches) if touched_file_count is None else touched_file_count
    if unresolved_directories:
        warnings.append(
            "PM-direct 조건 (a) 재확인: directory touches 파일 수를 해소하지 "
            f"못했습니다(상향 기본값): {', '.join(unresolved_directories)}."
        )
    elif file_count > 2:
        warnings.append(
            f"PM-direct 조건 (a) 위반: touches가 {file_count}개 파일입니다"
            "(2개 이하 필요)."
        )
    code_changed = any(
        Path(path.replace("\\", "/")).suffix.lower() in _PM_DIRECT_CODE_SUFFIXES
        for path in changed_paths
    )
    test_changed = any(_is_changed_test_path(path) for path in changed_paths)
    if code_changed and not test_changed:
        warnings.append(
            "PM-direct 조건 (b) 재확인: 코드 파일 diff가 있지만 "
            "신규/수정 테스트가 보이지 않습니다."
        )
    return tuple(warnings)


class ClaimedTouchesSnapshot(NamedTuple):
    """귀속 창 안 티켓 touches 읽기 결과 — 정상 빈 목록과 읽기 실패를 분리한다."""
    tickets: dict[str, list[str]]
    error: str | None


def _frontmatter_head(text: str) -> str:
    """티켓 원문에서 frontmatter 블록만 잘라낸다 (본문 제외 · YAML 파싱 없음)."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", len("---"))
    return text if end == -1 else text[:end]


def _fallback_frontmatter_scalar(text: str, key: str) -> str | None:
    """보수적 단일-line scalar 복구 — board 모듈 불능 폴백과 창 판정의 머리 판독이 함께 쓴다."""
    match = re.search(rf"(?m)^{re.escape(key)}\s*:\s*(.*?)\s*$", text)
    if match is None:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    if raw[0] in "\"'" and raw.endswith(raw[0]) and len(raw) >= 2:
        return raw[1:-1].strip() or None
    return raw.split("#", 1)[0].strip() or None


_WORKTREE_TOUCH_PREFIX = re.compile(r"^work/[^/]+_\d+(?:/|$)")


def _has_worktree_touch_prefix(path: str) -> bool:
    """touch 가 `work/<repo>_<N>/` 슬롯 접두를 가졌는지 보는 경량 판정."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return _WORKTREE_TOUCH_PREFIX.match(norm) is not None


class StageScope(NamedTuple):
    """stage 스코프 산출 결과 — `pathspec` + `error`(산출 불능 사유·None 이면 정상).

    빈 `pathspec` 을 두 상태로 갈라 쓴다: **선언이 비었다**(error=None·정상)와 **판정기가
    죽었다**(error=사유). 후자를 조용히 no-op 으로 흘리면 stage 0 인데 아무 말이 없어
    "기록 끝" 으로 보인다(reviewer 실측 — board 모듈 로드 실패 형상).
    """
    pathspec: tuple[str, ...]
    error: str | None


class RepoStagePlan(NamedTuple):
    """한 git repo 에서 실행할 좁은 stage 계획 (두-git task-mode 용)."""
    label: str
    cwd: Path
    scope: StageScope


# ── 완료 대상 작업 트리 자기 축 회귀 판정 helper (순수 함수) ────────────────
_SELF_AXIS_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# 전-트리 ratchet 가드 — 이 목록의 파일은 대상 집합에 항상 들어간다(파일별 매핑이 아니라
# "완료마다 항상 도는 가드" 정책 목록이다). 실 wave 사고에서 신규 red 를 잡아낸 실측으로
# 고정된 4개다. 새 항목을 빠뜨리면 tests/test_ticket_finish.py 의 목록 커버리지 회귀가 red 로
# 잡는다 — 추가 규율을 산문에 적지 않는다.
_SELF_AXIS_RATCHET_GUARD_FILES = (
    "tests/test_pm_review_delta.py",
    "tests/test_private_context_guard.py",
    "tests/test_public_reference_lint.py",
    "tests/test_upgrade_adopter_e2e.py",
)


def _self_axis_target_paths(path_list_stdout: str) -> set[str]:
    """git 경로 목록 출력(개행 구분)에서 `tests/` 바로 아래 flat `*.py` 만 남긴다.

    서브디렉터리(`tests/fixtures/…`)는 pytest 수집 대상이 아니라 제외한다. `git diff
    --name-only HEAD`·`git ls-files -o --exclude-standard` 양쪽 출력에 공용으로 쓴다.
    """
    files: set[str] = set()
    for line in path_list_stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("tests/") or not candidate.endswith(".py"):
            continue
        if "/" in candidate[len("tests/"):]:
            continue
        files.add(candidate)
    return files


def _self_axis_failed_node_ids(pytest_output: str) -> set[str]:
    """pytest `-q` 출력의 `FAILED`/`ERROR` 요약 줄에서 실패한 **테스트 노드 ID** 전체(파일이
    아니라 함수 단위) — base·dev 양쪽에 실패가 있는 파일에서도 신규분만 비교하려면 함수
    단위가 필요하다(파일 단위 비교는 그 신규분을 지운다).

    노드 ID 의 파일 경로 부분은 POSIX 표기로 고정한다. 자식 pytest 는 rootdir 해소에 따라
    같은 실패를 `os.sep` 표기로 낸다(Windows 실측: 상위 `pytest.ini` 를 rootdir 로 잡으면
    `tests\\test_a.py::test_x`). 표기가 갈리면 작업 트리와 baseline 이 같은 실패를 서로 다른
    노드 ID 로 보고 없는 신규 실패를 만들어 내고, 진단문도 엔진의 다른 경로 출력(`git diff
    --name-only` 산출·`touches`)과 표기가 어긋난다. 정규화는 이 한 곳뿐이다 — 비교도 진단문도
    전부 이 반환값에서 나온다. `::` 뒤(파라미터 ID)는 경로가 아니라 pytest 가 만든 표기라
    그대로 둔다."""
    node_ids: set[str] = set()
    for match in _SELF_AXIS_FAILED_NODE_RE.finditer(pytest_output):
        path, separator, parameters = match.group(1).partition("::")
        node_ids.add(path.replace("\\", "/") + separator + parameters)
    return node_ids


def scope_covers(pathspec: Sequence[str], path: str) -> bool:
    """`path` 가 이번 stage 선언 스코프에 덮이는가 (정확 일치 또는 선언 디렉토리 아래)."""
    return any(path == rel or path.startswith(rel.rstrip("/") + "/") for rel in pathspec)


def _dirty_entry_path(line: str) -> str:
    """`split_dirty` 출력 한 줄(`"XY path"`)에서 경로만 뽑는다 — 코드 2자 + 공백 고정폭."""
    return line[3:]


# 잔여 목록에서 **커밋된** 인구를 dirty 줄과 같은 고정폭으로 싣는 표시 — git 의 XY 코드 자리를
# 쓰되 git 이 내지 않는 값이라 두 인구가 한 목록 안에서 구별된다.
_COMMITTED_ENTRY_CODE = "CM"

_LOCAL_RUNTIME_ROOT = ".project_manager/.local"


def _is_local_runtime_path(path: str) -> bool:
    """clone-local 런타임 상태(`.project_manager/.local/`)인가 — 잔여 인구에서 뺀다.

    정상 adopter 에서는 gitignore 지만, 최소·격리 repo 에서도 lock·장부·task 상태가 ticket
    산출물 누락으로 오보되지 않게 접두 클래스 전체를 뺀다. 비슷한 이름의 sibling
    (`.project_manager/.locality/`)은 빼지 않는다. dirty 인구와 커밋 인구가 이 한 규칙을
    공유한다 — 사본을 두면 한쪽만 고쳐진다.
    """
    return path == _LOCAL_RUNTIME_ROOT or path.startswith(_LOCAL_RUNTIME_ROOT + "/")


# ── 사설 참조 완료 기록 preflight 보조 (측정 폭·판정식은 다른 소유자를 그대로 부른다) ──
# 판정식 자체(`prose_token_spans`·`_actionable_matches`)는 domain.private_refs가, 측정 폭
# 원문(0-컨텍스트 unified diff)은 infra.review_diff.measured_diff_text 가 소유한다. 여기 두
# 함수는 그 둘 사이 **형식 변환**만 한다 — 새 판정 로직이 아니다.

_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# blame porcelain 첫 필드 — sha1(40)·sha256(64) 두 오브젝트 형식을 다 받는다.
_BLAME_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
# 차단 목록에서 raw 축 유입에 붙는 사유 — actionable(안전 삭제 단위가 잡힌 참조)과 같은 줄
# 형식을 쓰되, 그 줄이 왜 차단감인지를 값으로 남긴다. 도입 커밋이 아예 없는 줄과, 커밋은
# 됐지만 아직 통합 브랜치에 없는 줄은 둘 다 이 작업이 새로 들인 유입이고 사람이 볼 처방이
# 다르다(전자는 그냥 고치면 되고 후자는 어느 커밋을 고쳐야 하는지가 필요하다).
_PRIVATE_REF_UNCOMMITTED_NOTE = " (미커밋 신규)"
_PRIVATE_REF_UNMERGED_NOTE = " (통합 미반영 {sha})"


def _parsed_diff_added_lines(
    diff_text: str,
) -> tuple[dict[str, set[int]], list[str]]:
    """0-컨텍스트 unified diff 원문 → ({새 파일 상대경로: {추가된 줄 번호}}, 경로 미확정 헤더).

    경로 복원은 `review_diff_rules._diff_block_path` 가 소유한다(사본 0) — 비-ASCII 경로의
    C-quote 8진 표기·rename 목적지·`/dev/null` 을 그 함수가 이미 손실 없이 푼다. 손으로 접두만
    벗기면 quote 된 표기가 그대로 키가 되어 실제 출하 경로와 결합되지 않고, 그 파일의 유입이
    통째로 판정에서 빠진다. 여기서는 block 을 나누고 hunk 헤더의 새-파일 줄 번호만 센다.

    산문 판정 자체는 여기서 하지 않는다 — `prose_token_spans` 는 완전한 파이썬 소스가
    필요해 `+`로 시작하는 diff 조각을 그대로 파싱할 수 없다. 여기서는 **어느 줄이 새로
    추가됐는지**만 뽑고, 판정은 호출부가 작업트리 파일 전문에 건다."""
    blocks: list[list[str]] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    added: dict[str, set[int]] = {}
    unresolved: list[str] = []
    for block in blocks:
        path = review_diff_rules._diff_block_path(block)
        if path is None:
            unresolved.append(block[0].rstrip("\r\n"))
            continue
        current_line: int | None = None
        for line in block[1:]:
            if line.startswith("@@"):
                match = _DIFF_HUNK_HEADER_RE.match(line)
                current_line = int(match.group(1)) if match else None
                continue
            if current_line is None:
                continue  # hunk 이전 메타데이터(`--- `·`+++ `·mode·index)는 줄이 아니다.
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" — 줄이 아니다.
            if line.startswith("+"):
                added.setdefault(path, set()).add(current_line)
                current_line += 1
            elif not line.startswith("-"):
                current_line += 1  # 컨텍스트 줄(0-컨텍스트 diff 에선 드묾) — 줄 번호를 소비한다.
    return added, unresolved


class StatusObservationError(Exception):
    """워킹트리 상태 **관측 자체가 실패**했다 — `strict` 소비자만 받는다.

    완료 기록의 잔여 판정·stage 뒤 보고와 종결의 재배치·머지는 그 실패를 clean 과 구별해 멈춘다:
    빈 목록으로 접으면 dirty 트리가 clean 으로 위장돼 그 위에서 판정·mutation 이 돈다.
    """


# 판정 기준(통합 브랜치)을 해소하지 못한 실행은 멈춘다 — 측정 폭·사설 참조·잔여 인구가 모두
# 그 기준을 입력으로 받으므로, 다른 기준으로 접으면 세 판정이 각자 다른 것을 본다.
_INTEGRATION_TIP_UNDECLARED = (
    "{ticket} 묶음 장부가 통합 브랜치(base_branch)를 선언하지 않았다 — 완료 기록의 측정 폭·"
    "사설 참조 판정·잔여 인구가 그 기준을 입력으로 받는다. `board.py cluster show <묶음>` 으로 "
    "장부를 확인하고 `base_branch` 를 채운 뒤 다시 실행하라."
)
_INTEGRATION_TIP_UNRESOLVED = (
    "{ticket} 장부가 선언한 통합 브랜치 {branch} 가 이 코드 트리에 없다 ({tree}) — 판정 기준을 "
    "해소하지 못했다. 장부의 통합 브랜치 이름과 이 트리의 브랜치를 대조하라."
)
_RESIDUAL_POPULATION_UNRESOLVED = (
    "{ticket} 통합 브랜치 tip 으로 커밋된 인구를 읽지 못했다 ({tip}) — 잔여 판정 인구가 성립"
    "하지 않는다. 이 트리에서 그 브랜치가 해소되는지 확인하라."
)
_RESIDUAL_ATTRIBUTION_SKIP_NOTE = (
    "잔여 판정 귀속 보정 skip — {ticket} 창 안 티켓 touches 를 읽지 못했다 ({error}). 같은 "
    "묶음 다른 멤버가 선언한 경로도 잔여로 잡힐 수 있다."
)
_UNBORN_BASELINE_GUARD_OFF = (
    "{ticket} 기준선 없음 — 장부가 선언한 통합 브랜치 {branch} 가 이 트리가 든 그 브랜치이고 "
    "커밋이 0이다. 잴 표면(기준선·HEAD)이 없어 이 축의 가드를 끕니다(측정 불가 = 가드 off)."
)
_UNBORN_SKELETON_NOTE = (
    "{ticket} 기준선 없음(커밋 0) — 선언 스코프 밖 {count}건은 이 티켓의 잔여가 아니라 아직 "
    "아무도 커밋하지 않은 뼈대다(차단하지 않음). 뼈대를 저장소에 실으려면 `git add -A` 로 "
    "따로 커밋하라."
)


def _canonical_touch_path(path: str) -> str:
    """touches 경로를 귀속 비교용 POSIX 상대 표기로 접는다."""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    return normalized or "."

def _touch_contains(parent: str, child: str) -> bool:
    """parent touches 가 child touches 를 포함하는가(문자열 경계 보존)."""
    parent = _canonical_touch_path(parent)
    child = _canonical_touch_path(child)
    return parent == "." or child == parent or child.startswith(parent + "/")

def _touch_claims_path(touch: str, changed_path: str) -> bool:
    """touches 선언이 변경 파일을 포함하는가.

    경로 magic/glob 은 이 작은 귀속기가 Git 과 완전히 같게 해석할 수 없다. 그런 선언을
    `False` 로 접어 제외하면 현재 티켓 몫을 숨길 수 있으므로, 불확실한 선언은 일치(True)로
    취급한다. 오차는 과다 측정 쪽이다.
    """
    canonical = _canonical_touch_path(touch)
    if canonical.startswith(":(") or any(char in canonical for char in "*?["):
        return True
    return _touch_contains(canonical, changed_path)


def _self_axis_block(ticket_id: str, new_failed: list[str], output: str) -> str:
    """자기 축 신규 red 차단 문자열 — `run()` 이 `[중단]` 접두로 그대로 낸다."""
    lines = [f"자기 축 회귀 신규 실패 — {ticket_id}"]
    lines += [f"  ✗ {node}" for node in new_failed]
    tail = "\n".join(output.rstrip().splitlines()[-20:])
    if tail:
        lines.append("  · 회귀 출력(꼬리 20줄):")
        lines.append(tail)
    return "\n".join(lines)
