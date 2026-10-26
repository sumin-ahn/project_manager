"""핸드오프 프롬프트 구성."""
from __future__ import annotations

from pm_engine.infra import lease_store
import re
from pm_engine.app import session_identity


_HANDOFF_PROMPT_SECTION_ANCHOR = "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)"


_CODE_BLOCK_RE = re.compile(r"```\n(.+?)```", re.DOTALL)


def extract_handoff_prompt_template(pm_playbook_text: str) -> str | None:
    """pm_playbook.md 에서 인계 프롬프트 템플릿 코드블록을 추출한다.

    반환: 코드블록 내용 문자열 또는 None (앵커 불일치).
    """
    anchor_idx = pm_playbook_text.find(_HANDOFF_PROMPT_SECTION_ANCHOR)
    if anchor_idx == -1:
        return None

    # 섹션 이후에서 다음 ## 헤더 전까지
    after_anchor = pm_playbook_text[anchor_idx + len(_HANDOFF_PROMPT_SECTION_ANCHOR):]
    next_header = re.search(r"^## ", after_anchor, re.MULTILINE)
    if next_header:
        section_text = after_anchor[:next_header.start()]
    else:
        section_text = after_anchor

    match = _CODE_BLOCK_RE.search(section_text)
    if match is None:
        return None
    return match.group(1)


_BARE_BOOTSTRAP_TRIGGER = "/pm-bootstrap"


_BARE_BOOTSTRAP_TRIGGER_RE = re.compile(
    r"(?<![A-Za-z0-9_.>/\-])(?P<trigger>(?:[/$])?pm-bootstrap)"
    r"(?![A-Za-z0-9_.>/\-])"
)


_MULTI_HARNESS_BOOTSTRAP_RE = re.compile(
    r"`(?P<first_prefix>[/$])pm-bootstrap`\((?P<first_labels>[^)]+)\)"
    r"(?: / `(?P<second_prefix>[/$])pm-bootstrap`\((?P<second_labels>[^)]+)\))"
)


def _select_runtime_bootstrap_notation(template: str) -> str:
    """공유 wiki의 다중-harness 병기를 현재 하네스 표기 하나로 축약한다."""
    prefix = lease_store._runtime_skill_entry_prefix()

    def select(match: re.Match[str]) -> str:
        for key in ("first", "second"):
            if match.group(f"{key}_prefix") == prefix:
                return f"{prefix}pm-bootstrap"
        return f"{prefix}pm-bootstrap"

    selected = _MULTI_HARNESS_BOOTSTRAP_RE.sub(select, template)
    return _BARE_BOOTSTRAP_TRIGGER_RE.sub(
        lambda _match: f"{prefix}pm-bootstrap",
        selected,
    )


def _inject_task_into_template(template: str, task: str) -> str:
    """복사 블록 템플릿의 bare ``pm-bootstrap`` 호출을 task-qualified 로 치환한다.

    task 모드 핸드오프면 트리거를 `/pm-bootstrap --task <task>` 로 치환한다 — 다음 세션이
    task 앵커로 재개(차수 추론·per-task pm_state 포인터·clean resume 실링크)하게 연속성을
    보존한다. task가 진입 정체성의 단일 축이고
    보유 슬롯 집합을 자동 수령하므로 트리거에 슬롯을 열거하지 않는다
    (트리거 = 재개 명령 1:1).
    task 이름은 호출부(`build_handoff_prompt_output`)가 `validate_task_name_engine` 로 삽입 전
    검증하므로 단일 안전 토큰(공백·괄호·path 문자·예약패턴 불가)이 보장돼 quoting 이 불필요하다.
    """
    return _BARE_BOOTSTRAP_TRIGGER_RE.sub(
        lambda match: f"{match.group('trigger')} --task {task}",
        template,
    )


def build_handoff_prompt_output(home, pm_playbook_text: str, session_num: int | str, wave_summary: str, date_str: str, task: str) -> str:
    """인계 프롬프트 stdout 출력 문자열을 빌드한다.

    pm_playbook.md §부트스트랩 프롬프트 템플릿(역할 framing + `/pm-bootstrap` 트리거)을 그대로
    포함한다. 부트스트랩이 인계 본문(박제 entries·메타 학습·pending intent·회귀/incident)을
    log handoff entry 에서 자동 dump 하므로, 프롬프트는 더 이상 `<핵심 인계 사항>` 손-채움을
    싣지 않는다

    트리거는 `/pm-bootstrap --task <task>` 로 주입한다 — 세션 정체성이 task 하나이고, 다음
    세션은 진입 시 보유 작업 폴더 집합을 장부에서 자동 수령하므로 트리거에 슬롯을 열거하지
    않는다. pm_playbook.md 템플릿 파일은 건드리지 않는다 — 치환은 추출된 텍스트 안에서만
    contained.
    """
    template = extract_handoff_prompt_template(pm_playbook_text)
    if template is None:
        return (
            "[경고] pm_playbook.md 에서 인계 프롬프트 템플릿을 찾지 못했다. "
            f"앵커: '{_HANDOFF_PROMPT_SECTION_ANCHOR}'\n"
            "pm_playbook.md §'다음 PM 세션 부트스트랩 프롬프트 (템플릿)' 을 직접 복사하라."
        )
    template = _select_runtime_bootstrap_notation(template)
    # 삽입 전 공유 validator 로 fail-loud — build_handoff_prompt_output() 직접 호출도 우회 못 하게
    # 트리거 경계에서 닫는다. run() 은 진입에서 이미 검증하므로 멱등 재검증.
    session_identity.validate_task_name_engine(home, task)
    template = _inject_task_into_template(template, task)

    header = (
        f"=== 인계 프롬프트 (PM {session_num}차 → 다음 PM 세션) ===\n"
        f"--- 아래를 복사해 다음 PM 세션에 붙여넣기 ---\n"
        f"(인계 본문은 {lease_store._runtime_skill_entry('pm-bootstrap')} 이 log entry 에서 자동 dump — "
        f"프롬프트는 트리거만)\n\n"
    )
    footer = (
        f"\n--- 복사 끝 ---\n"
        f"[참고] 날짜: {date_str}, wave summary: {wave_summary}\n"
    )
    return header + "```\n" + template + "```" + footer
