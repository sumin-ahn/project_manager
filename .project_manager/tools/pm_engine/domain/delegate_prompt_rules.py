"""위임 프롬프트 문법과 역할 안내 (층: domain)."""
from __future__ import annotations

from pathlib import PurePosixPath
from collections.abc import Sequence
import re
from pm_engine.domain import review_form

# fix 라운드 delta 꼬리에 부착하는 수정 범위 제약 문구 단일 진실 — 스킬·카드·playbook은
# 이 상수를 복제하지 않고 "출력을 그대로 전달"만 지시한다(no-hand-retyping 원칙). 빈틈 보고
# 형식이 verify 블록·사유 값을 인용하므로 그 상수들 **뒤에** 둔다(문언 drift 0).
PM_REVIEW_FIX_SCOPE_NOTICE = f"""\
## 수정 범위 제약 (이 delta 공통)

- 위에 나열한 finding ID 와 각 `허용 수정 범위` 안에서만 고친다. 그 밖의 코드·테스트·문서는
  더 나은 방법이 보여도 이번 라운드에서 건드리지 않는다.
- 처방이나 명세에 빈틈이 있으면 스스로 메우지 않는다. 처방대로 따르면 다른 결함이 생기는
  상호작용도 빈틈이다. 구현을 멈추고 아래 형식으로 라운드 파일에 적은 뒤 종료한다.
  보강 처방이 필요하면 현재 티켓을 멈추고 사용자에게 보고한다.
- 빈틈 보고로 끝난 라운드는 정상 산출이고 성공 종료다. 빈 손으로 끝내지 않으려고
  처방 밖 구현을 얹지 않는다.
- `허용 수정 범위`가 `{review_form.PM_REVIEW_PM_OWNED_SCOPE_PREFIX}`로 시작하는 finding은 PM 소유다.
  developer는 구현하지 않고 해당 verify 행을 machine_verifiable=false ·
  reason={review_form.PM_REVIEW_VERIFY_PM_OWNED_REASON} · expected=<PM 완료 기준 실값>으로 채운다. scope 표식과
  verify 선언 중 하나만 있으면 회수가 거부된다.

빈틈 보고 형식:
- 대상: 어느 finding ID 의 어느 처방인가
- 빈틈: 처방이 정하지 않은 지점은 무엇인가
- 충돌: 처방대로 하면 무엇이 깨지는가 (재현 커맨드·파일:라인·관측값)
- 대안: 검토한 선택지와 각각의 영향 (권고까지만 쓰고 적용하지 않는다)
- 기계 선언: 그 ID 의 {review_form.PM_REVIEW_VERIFY_BLOCK} 행을 **지우지 말고** machine_verifiable=false ·
  reason={review_form.PM_REVIEW_VERIFY_GAP_REASON} 로 채운다(expected 에 빈틈 요지 한 줄). 이 행이 없으면
  엔진은 빈틈 보고와 태만을 구별하지 못해 PM 의 기계 확인이 통째로 막힌다.
"""


def _prohibition(adapter_directories: Sequence[str]) -> str:
    adapter_scope = "/".join(adapter_directories)
    adapter_definition = (
        f"엔진 등록 통합 루트 전체: {adapter_scope}"
        if adapter_scope else
        "엔진 등록 통합 루트 전체"
    )
    return (
        "금지: commit/push/force/reset/rm 등 git 비가역 조작·board 조작·어댑터 디렉토리"
        f"({adapter_definition}) 수정을 하지 마라(PM 이 결과 회수 후 담당). "
        "결과는 최종 텍스트로 보고하라."
    )


_ROLE_IDENTITIES: dict[str, str] = {
    "developer":
        "너는 이 프로젝트의 developer 서브에이전트다 — 단일 작업을 구현하고 테스트까지 낸다.",
    "researcher":
        "너는 이 프로젝트의 researcher 서브에이전트다 — 조사·분석만 하고 코드를 수정하지 않는다.",
    "architect":
        "너는 이 프로젝트의 architect 서브에이전트다 — 설계 초안을 낸다(발행은 PM/사용자 게이트).",
    "code-reviewer":
        "너는 이 프로젝트의 code-reviewer 서브에이전트다 — 변경을 검토하고 테스트를 실행해 판정한다"
        "(코드를 수정하지 않는다).",
}

# 라운드 중 회귀 범위는 호출 프롬프트가 소유한다. stage-exit 전체 회귀를 직접 기록할 책임이
# 있는 developer 역할에만 기계 주입한다. code-reviewer는 계약을 설계하지만 구현 단계를 종료하지 않는다.
REGRESSION_SCOPE_PREAMBLE = (
    "구현 중 inner-loop는 프롬프트가 지정한 targeted tests만 실행하라. "
    "developer 단계 종료 직전에 해소된 프로젝트 `test_cmd`의 전체 회귀를 직접 정확히 1회 실행하고 "
    "라운드 파일 `## 회귀`에 정확한 커맨드와 `rc=0` 결과를 기록하라. 전체 회귀가 red면 이미 "
    "수집한 실패를 공통 원인으로 batch 수정하고 targeted tests를 확인한 뒤 terminal 확인용 전체 "
    "회귀를 정확히 1회만 다시 실행하라. `-x` full 반복이나 serial fallback은 금지다. 규범은 "
    "`.project_manager/wiki/pm_principles.md` §티켓과 위임을 따른다."
)
REGRESSION_SCOPE_ROLES: frozenset[str] = frozenset({"developer"})


# ── 쓰기-타깃 axis 재앵커  ─────────────────────────────────────────────

_ENGINE_PATH_CANDIDATE_RE = re.compile(
    r"[A-Za-z0-9._/\\-]*\.project_manager[A-Za-z0-9._/\\-]*"
)
_PATH_LAYOUT_CHAR_CLASS = r"A-Za-z0-9._/\\-"
_PATH_LAYOUT_CHAR_RE = re.compile(rf"[{_PATH_LAYOUT_CHAR_CLASS}]")
_PATH_LAYOUT_GAP_RE = re.compile(
    r"[ \t]*(?:\\[ \t]*)?(?:\r\n|\r|\n)[ \t]*|[ \t]+"
)
_PATH_WRAPPER_RE = r"""[`'"()\[\]{}<>]*"""
_KOREAN_PATH_PARTICLE_RE = r"(?:은|는|을|를|에|에는|만|만은)?"
_KOREAN_WRITE_VERBS = (
    "수정", "편집", "변경", "고치", "고쳐", "건드리", "건드려", "손보", "손봐",
    "손대", "지우", "지워", "바꾸", "바꿔", "삭제", "추가", "구현",
    "덮어쓰", "덮어써", "대체", "재작성", "패치", "리팩터",
)
_ASCII_WRITE_VERBS = (
    "modify", "modified", "edit", "edited", "touch", "touched",
    "change", "changed", "rewrite", "rewritten", "replace", "replaced",
    "overwrite", "overwritten", "update", "updated", "fix", "fixed",
    "write", "written", "delete", "deleted", "alter", "altered",
    "implement", "implemented", "patch", "refactor",
)
# 한국어 write stem은 활용어미 앞에서도 잡되, `미수정`·`무변경`·`재수정`처럼 상태/반복을
# 나타내는 접두 합성어 안의 부분 문자열은 write 지시로 보지 않는다.
_KOREAN_NON_COMMAND_PREFIX_CHARS = "미무비불재"
_WRITE_VERB_PATTERN = (
    rf"(?:(?<![{_KOREAN_NON_COMMAND_PREFIX_CHARS}])(?:"
    + "|".join(
        re.escape(verb)
        for verb in sorted(_KOREAN_WRITE_VERBS, key=len, reverse=True)
    )
    + r")|\b(?:"
    + "|".join(
        re.escape(verb)
        for verb in sorted(_ASCII_WRITE_VERBS, key=len, reverse=True)
    )
    + r")\b)"
)
_KOREAN_DIRECT_NEGATION_AFTER_RE = re.compile(
    rf"^\s*{_PATH_WRAPPER_RE}\s*{_KOREAN_PATH_PARTICLE_RE}\s*"
    r"(?:"
    r"(?:건드리지|손대지|수정하지|편집하지|변경하지)\s*"
    r"(?:마라|말라|마세요|말\s*것)"
    r"|수정\s*금지"
    r")",
    re.IGNORECASE,
)
_ENGLISH_DIRECT_NEGATION_AFTER_RE = re.compile(
    rf"^\s*{_PATH_WRAPPER_RE}\s*"
    r"(?:must\s+not|should\s+not|do\s+not|don't|never)\s+"
    rf"(?:be\s+)?{_WRITE_VERB_PATTERN}",
    re.IGNORECASE,
)
_DIRECT_NEGATION_BEFORE_RE = re.compile(
    rf"(?:"
    r"수정\s*금지\s*[:：]?"
    rf"|(?:do\s+not|don't|must\s+not|should\s+not|never)\s+{_WRITE_VERB_PATTERN}"
    r")"
    rf"\s*{_PATH_WRAPPER_RE}\s*$",
    re.IGNORECASE,
)
_POST_NEGATION_CLAUSE_PREFIX = (
    r"""^\s*[`'")\]}>]*\s*(?:[;；。！？!?.]\s*)?"""
)
_NEGATION_IGNORE_MARKER_RE = re.compile(
    r"(?:는\s*말은\s*)?(?:무시|\b(?:ignore|disregard|override)\b)",
    re.IGNORECASE,
)
_NEGATION_FOLLOWUP_PRONOUN_WRITE_RE = re.compile(
    _POST_NEGATION_CLAUSE_PREFIX
    + rf"(?:and|but)\s+"
    rf"{_WRITE_VERB_PATTERN}\s+(?:it|this|that)\b",
    re.IGNORECASE,
)
_NEGATION_INSTEAD_RE = re.compile(
    _POST_NEGATION_CLAUSE_PREFIX
    + r"(?:그\s*)?(?:대신|instead(?:\s+of\s+(?:that|this))?)",
    re.IGNORECASE,
)
_OVERRIDE_CLAUSE_END_RE = re.compile(
    r"[;；。！？!?\r\n]|\.(?=\s|$)"
)
_EXPLICIT_PATH_RE = re.compile(
    r"(?:\.{0,2}[/\\])?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+|\bwiki\b",
    re.IGNORECASE,
)
_WRITE_NEAR_READ_ONLY_CALL_RE = re.compile(
    _WRITE_VERB_PATTERN,
    re.IGNORECASE,
)
_READ_ONLY_CALL_FILE_REFERENCE_WRITE_RE = re.compile(
    r"(?:"
    r"(?:위|이)\s*(?:스크립트|파일)(?:을|를|은|는)?"
    r"[\s\S]{0,40}?"
    rf"{_WRITE_VERB_PATTERN}"
    r"|"
    rf"{_WRITE_VERB_PATTERN}"
    r"[\s\S]{0,40}?"
    r"(?:"
    r"that\s+(?:script|file)(?:\s+above)?"
    r"|the\s+(?:(?:script|file)\s+above|above\s+(?:script|file))"
    r")"
    r")",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"[。！？!?]|\.(?=\s|$)"
)
_READ_ONLY_CALL_SHELL_TAIL_RE = re.compile(
    r"(?:&&|\|\||[|;&])"
)
_READ_ONLY_CALL_INVALID_TICKET_ARG_RE = re.compile(
    r"""^[ \t]*[`'")\]}>.,!?。！？]*[ \t]*T-(?:[A-Za-z0-9_-]+-\d+|\d+)\b""",
    re.IGNORECASE,
)
_PYTHON_LAUNCHER_BEFORE_PATH_RE = re.compile(
    r"\b(?:python3|python|py(?:[ \t]+-\d+(?:\.\d+)?)?)[ \t]+$",
    re.IGNORECASE,
)
_READ_ONLY_BOARD_CALL_RE = re.compile(
    r"""
    [ \t]+(?:
        show[ \t]+(?:T-NNNN|<T-NNNN>|T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\d+|\d+))
        |
        list(?:
            [ \t]+(?:
                --(?:mine|all)
                |--status[ \t]+(?:open|claimed|blocked|done|all)
                |--(?:tag|repo|task|user)[ \t]+[^\s`'"()\[\]{}<>|&;]+
                |--slot[ \t]+\d+
            )
        )*
        |
        lint(?:[ \t]+--gate)?
        |
        idea[ \t]+list
        |
        prefix[ \t]+list
        |
        regression[ \t]+check
        |
        livegate[ \t]+check
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _clean_prompt_path_token(raw: str) -> str:
    """프롬프트의 공백 토큰에서 경로 바깥 인용·구두점만 제거한다."""
    return raw.strip("\"'`()[]{}<>,;:!?").rstrip(".")


def _engine_path_parts(raw: str) -> tuple[str, ...] | None:
    """토큰이 `.project_manager/tools` 경로면 정규화한 성분을 반환한다."""
    token = _clean_prompt_path_token(raw)
    if not token or ".project_manager" not in token:
        return None
    parts = PurePosixPath(token.replace("\\", "/")).parts
    for i in range(len(parts) - 1):
        if parts[i] == ".project_manager" and parts[i + 1] == "tools":
            return parts
    return None


def _path_layout_gap_is_internal(text: str, start: int, end: int) -> bool:
    """layout gap 이 이미 시작된 경로 후보 내부의 접힘 지점인가."""
    if start == 0 or end >= len(text):
        return False
    next_char = text[end]
    if _PATH_LAYOUT_CHAR_RE.fullmatch(next_char) is None:
        return False
    if text[start - 1] in "/\\":
        return True
    if next_char not in "/\\":
        return False

    # separator 앞 gap 은 왼쪽 토큰이 이미 `.project_manager` 경로일 때만 접는다.
    # 따라서 `Modify\n.project_manager/...` 같은 일반 단어→경로 경계는 보존된다.
    left_match = re.search(rf"[{_PATH_LAYOUT_CHAR_CLASS}]+$", text[:start])
    if left_match is None:
        return False
    left_parts = PurePosixPath(left_match.group().replace("\\", "/")).parts
    return ".project_manager" in left_parts


def _normalize_prompt_path_layout(prompt: str) -> tuple[str, list[int]]:
    """긴 경로의 자연 줄바꿈을 제거한 매칭 뷰와 원문 offset map 을 반환한다.

    직전 조각이 separator 로 끝나거나 이미 `.project_manager` 경로이고 다음 조각이 separator 로
    시작할 때만 개행(plain 또는 shell식 ``\\\n`` 연속)/수평 공백을 접는다. 일반 단어와 경로 시작
    사이 및 독립된 여러 경로 사이의 layout 은 보존한다.
    """
    remove = [False] * len(prompt)
    for match in _PATH_LAYOUT_GAP_RE.finditer(prompt):
        remove_start = match.start()
        if not _path_layout_gap_is_internal(prompt, remove_start, match.end()):
            # Windows식 경로 separator 자체가 줄끝에 온
            # `.project_manager\\\ntools\\x.py`에서는 `\`를 보존하고 개행만 접는다.
            # `/\\\n`의 두 번째 `\`는 shell 연속 문자이므로 기존처럼 gap 전체를 제거한다.
            backslash = prompt.rfind("\\", match.start(), match.end())
            if (backslash < 0
                    or not _path_layout_gap_is_internal(
                        prompt, backslash + 1, match.end()
                    )):
                continue
            remove_start = backslash + 1
        remove[remove_start:match.end()] = [True] * (
            match.end() - remove_start
        )
    kept = [i for i, discarded in enumerate(remove) if not discarded]
    return (
        "".join(prompt[i] for i in kept),
        kept,
    )


def _engine_path_occurrences(
    prompt: str,
) -> list[tuple[int, int, tuple[str, ...]]]:
    """엔진 경로 후보를 공백 토큰이 아닌 실제 경로 span 단위로 반환한다.

    한국어 조사·공백 없는 문장부호가 경로 토큰에 붙어도 span 은 경로 끝에서 닫힌다. 따라서
    바로 뒤의 금지/쓰기 표현을 해당 출현에만 결합할 수 있고, 같은 절의 타 경로로 전파하지 않는다.
    """
    occurrences: list[tuple[int, int, tuple[str, ...]]] = []
    normalized, original_offsets = _normalize_prompt_path_layout(prompt)
    for match in _ENGINE_PATH_CANDIDATE_RE.finditer(normalized):
        raw = match.group().rstrip(".")
        parts = _engine_path_parts(raw)
        if parts is not None:
            raw_end = match.start() + len(raw)
            occurrences.append((
                original_offsets[match.start()],
                original_offsets[raw_end - 1] + 1,
                parts,
            ))
    return occurrences


def _is_pure_read_only_board_call(
    prompt: str, start: int, end: int, path_parts: tuple[str, ...],
) -> bool:
    """현재 경로 span 이 독립된 read-only board 호출인가(A).

    각 서브커맨드의 실제 read-only 인자만 받고, 명령 뒤 같은 줄의 자연어 tail은 write 동사가
    없을 때 허용한다. 면제 범위는 이 명령 span 하나뿐이다. shell 연산/후속 명령, 같은 줄의
    수정 지시, 호출을 목적어로 삼은 앞쪽 write 동사는 모호한 write 로 보아 면제하지 않는다.
    """
    if path_parts[-1] != "board.py":
        return False

    line_start = max(prompt.rfind("\n", 0, start), prompt.rfind("\r", 0, start)) + 1
    newline_positions = [pos for pos in (prompt.find("\n", end), prompt.find("\r", end))
                         if pos >= 0]
    line_end = min(newline_positions) if newline_positions else len(prompt)
    before_path = prompt[line_start:start]
    python_match = _PYTHON_LAUNCHER_BEFORE_PATH_RE.search(before_path)
    if python_match is None:
        return False

    after_path = prompt[end:line_end]
    call_match = _READ_ONLY_BOARD_CALL_RE.match(after_path)
    if call_match is None:
        return False
    command_tail = after_path[call_match.end():]
    if (_READ_ONLY_CALL_SHELL_TAIL_RE.search(command_tail) is not None
            or _READ_ONLY_CALL_INVALID_TICKET_ARG_RE.match(command_tail) is not None
            or _WRITE_NEAR_READ_ONLY_CALL_RE.search(command_tail) is not None):
        return False
    command_start = line_start + python_match.start()
    command_end = end + call_match.end()

    # `modify "python3 ... show T-NNNN"`처럼 명령 span 자체가 앞선 write 동사의
    # 목적어인 경우를 닫는다. 앞쪽 별도 경로를 특정한 write 는 이 출현의 목적어가 아니다.
    before_command = before_path[:python_match.start()]
    nearby_prefix = before_command[-120:]
    if (_WRITE_NEAR_READ_ONLY_CALL_RE.search(nearby_prefix) is not None
            and not re.search(r"[A-Za-z0-9._/\\-]+\.[A-Za-z0-9_-]+"
                              r"[\s\S]{0,40}$", nearby_prefix)):
        return False
    reference_prefix = prompt[max(0, command_start - 240):command_start]
    prefix_boundaries = list(_SENTENCE_BOUNDARY_RE.finditer(reference_prefix))
    if prefix_boundaries:
        reference_prefix = reference_prefix[prefix_boundaries[-1].end():]
    reference_suffix = prompt[
        command_end:min(len(prompt), command_end + 240)
    ]
    suffix_boundary = _SENTENCE_BOUNDARY_RE.search(reference_suffix)
    if suffix_boundary is not None:
        reference_suffix = reference_suffix[:suffix_boundary.start()]
    reference_window = (
        reference_prefix
        + "\n<READ_ONLY_CALL>\n"
        + reference_suffix
    )
    if _READ_ONLY_CALL_FILE_REFERENCE_WRITE_RE.search(reference_window) is not None:
        return False
    return True


def _same_engine_path(
    candidate: str, path_parts: tuple[str, ...],
) -> bool:
    """명시 경로가 현재 금지된 엔진 경로와 같은 engine-relative 경로인가."""
    candidate_parts = _engine_path_parts(candidate)
    if candidate_parts is None:
        return False
    current_idx = path_parts.index(".project_manager")
    candidate_idx = candidate_parts.index(".project_manager")
    return (
        path_parts[current_idx:] == candidate_parts[candidate_idx:]
    )


def _redirect_targets_current_engine_path(
    redirect: str, path_parts: tuple[str, ...],
) -> bool:
    """redirect의 명시 대상이 없거나 현재 엔진 경로를 다시 가리키는가.

    `wiki 문서`처럼 최상위 문서 영역만 적은 형태도 명시 비-엔진 대상으로 본다. 대명사나
    목적어 생략은 기존처럼 금지 경로를 가리킬 수 있으므로 보수적으로 True다.
    """
    explicit_paths = _EXPLICIT_PATH_RE.findall(redirect)
    if not explicit_paths:
        return True
    return any(
        _same_engine_path(candidate, path_parts)
        for candidate in explicit_paths
    )


def _negation_is_overridden_for_path(
    prompt: str, start: int, negation_end: int, path_parts: tuple[str, ...],
) -> bool:
    """금지 뒤 write override가 현재 엔진 경로를 다시 대상으로 삼는가.

    ignore/disregard/override 마커는 인용된 금지 앞에도 올 수 있어 pre+post에서 찾되, 실제 write
    동사는 반드시 금지 뒤에 있어야 한다. ignore 및 `대신`/`instead` 모두 후속 write 절에 명시
    경로가 없거나 같은 엔진 경로일 때만 현재 금지를 폐기한다. 명시된 비-엔진 경로(예:
    wiki/roadmap.md)로 redirect하면 현재 엔진 경로의 금지는 유지한다.
    """
    post_window = prompt[negation_end:min(len(prompt), negation_end + 240)]
    if _NEGATION_FOLLOWUP_PRONOUN_WRITE_RE.search(post_window) is not None:
        return True

    write_match = re.search(_WRITE_VERB_PATTERN, post_window, re.IGNORECASE)
    marker_window = prompt[
        max(0, start - 160):min(len(prompt), negation_end + 240)
    ]
    if (write_match is not None
            and _NEGATION_IGNORE_MARKER_RE.search(marker_window) is not None):
        clause_end = _OVERRIDE_CLAUSE_END_RE.search(
            post_window, write_match.end(),
        )
        redirect = (
            post_window[:clause_end.start()]
            if clause_end is not None else post_window
        )
        return _redirect_targets_current_engine_path(redirect, path_parts)

    instead = _NEGATION_INSTEAD_RE.search(post_window)
    if instead is None:
        return False

    clause_tail = post_window[instead.end():]
    clause_end = _OVERRIDE_CLAUSE_END_RE.search(clause_tail)
    if clause_end is not None:
        clause_tail = clause_tail[:clause_end.start()]
    redirect = (
        post_window[instead.start():instead.end()]
        + clause_tail
    )
    if re.search(_WRITE_VERB_PATTERN, redirect, re.IGNORECASE) is None:
        return False
    return _redirect_targets_current_engine_path(redirect, path_parts)


def _path_has_direct_negative_write_context(
    prompt: str, start: int, end: int, path_parts: tuple[str, ...],
) -> bool:
    """금지 표현이 현재 경로 출현에 직접 결합하며 뒤에서 폐기되지 않았는가(B).

    앞/뒤 1개 결합 패턴만 인정한다. 절 전체 검색을 하지 않으므로 타 경로의 금지 표현은 전파되지
    않으며, 경로와 금지 사이에 write 동사·타 경로가 끼면 자연스럽게 매치되지 않는다.
    """
    before = prompt[max(0, start - 160):start]
    after = prompt[end:min(len(prompt), end + 320)]
    after_match = _KOREAN_DIRECT_NEGATION_AFTER_RE.match(after)
    if after_match is None:
        after_match = _ENGLISH_DIRECT_NEGATION_AFTER_RE.match(after)

    if after_match is not None:
        negation_end = end + after_match.end()
    elif _DIRECT_NEGATION_BEFORE_RE.search(before) is not None:
        negation_end = end
    else:
        return False

    # “금지라는 말은 무시하고 수정하라” / “ignore that and edit it”는 금지가 아니다.
    return not _negation_is_overridden_for_path(
        prompt, start, negation_end, path_parts,
    )


def _prompt_targets_engine_code(prompt: str) -> bool:
    """위임 프롬프트가 엔진 코드 경로(`.project_manager/tools/`)를 write 대상으로 하는가.

    정확 문자열 매칭이 아니라 **경로를 정규화해 성분 시퀀스**로 판정한다(codex must-fix) — 각 토큰을
    PurePosixPath 로 정규화(`.`/중복 슬래시 접힘)하고 긴 경로의 공백·개행/``\\\n`` 연속을 먼저
    접어 `.project_manager` 직후 `tools` 성분이 오면 True. 이로써 trailing slash 없음·`./`·자연
    줄바꿈 우회를 닫는다. 단, 독립된 순수 read-only `board.py show|list|lint` 호출(A)과 경로에 직접
    결합한 수정 금지(B)의 **그 경로 span만** 제외한다. 절/문서의 금지를 공유하지 않으며, 다른
    출현이 실제 write 지시면 True를 유지한다.

    위협 모델은 적대 프롬프트 의미 분석이 아니라 PM의 실수 방지다. 따라서 read-only 호출 다음 줄의
    대명사/일반 지시(예: ``본문대로 구현하라``)만으로 그 경로를 write 대상으로 추론하지 않는다
    (정당 관용구와 텍스트만으로 구분 불가). 명시 경로 재등장은 계속 차단한다. 잔여 경계는
    (1) 같은 엔진 경로에 금지와 write를 함께 붙인 자기모순 shape, (2) 경로 separator 없이
    `.project_manager``와 ``tools``를 개행 분할한 shape, (3) ASCII slash 대신 유니코드 동형
    slash를 쓴 shape다. 이 잔여 신형/적대 표현은 role preamble 금지
    변경 감지가 닫는다.
    write 역할 + PM 홈 cwd 조합에서만 재앵커 게이트로 쓰인다(PM-doc/wiki write 는 PM 홈 정당)."""
    for start, end, parts in _engine_path_occurrences(prompt):
        if _is_pure_read_only_board_call(prompt, start, end, parts):
            continue
        if _path_has_direct_negative_write_context(
            prompt, start, end, parts,
        ):
            continue
        return True
    return False
