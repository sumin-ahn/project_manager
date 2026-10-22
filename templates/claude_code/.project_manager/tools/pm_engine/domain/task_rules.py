"""task 이름·todo 스키마 검증과 초기 상태 렌더."""
from __future__ import annotations

import re
from pathlib import Path

from pm_engine.domain import identity_args, worktree_records


def _validate_task_name(name: str, registered_repos: "list[str] | set[str] | None" = None) -> None:
    """task 명을 안전한 단일 path 컴포넌트로 검증한다 — 위반 시 `InvalidTaskName`(fail-loud·must-fix).

    `task_dir(name)` 이 무검증 조인이라 traversal/절대경로/빈 문자열이 작업트리 밖에 디렉토리를
    만들고 장부를 오염시킨다(reviewer 실측 `--task ../../evil` → git-tracked `.project_manager/evil`).
    **엔진 진입점(bind_task)**에서 검증해 CLI 우회(직접 소비)까지 닫는다. mkdir·장부 write
    **이전**에 raise 한다(부작용 0). `registered_repos` 주면 `<repo>_<N>` 예약도 거부(primitive
    자기완결·CLI 의 빠른 거부와 이중화·should-fix). 예약 판정 정규식은 CLI 의
    `identity_args.is_reserved_task_name` 과 동형(모듈 격리라 inline).

    **문자 도메인 = 하류 구문 표면에 맞춘 협소화**(단일 불변식·per-surface 이스케이프
    회피). 거부 순서와 표면 근거:
      - 빈/공백-only → 빈 이름.
      - **모든 whitespace**(스페이스·탭 등·선행/후행/내부 전부) → task 명은 하류 *인자 경계*다: CLI
        `--task <task>`(shell word)·relay 재진입 프롬프트 `/pm-bootstrap --task <task>`(slash 인자
        경계)이 공백에서 쪼개져 정체성이 파손된다(codex 실증 `my task` → 인자 경계 깨짐).
      - **괄호 `(`·`)`** → log 헤더 태그 `(task:<task>)`(pm_handoff `_TASK_TAG_PREFIX`)의 delimiter 다:
        `)` 포함 명은 태그를 조기 종료시켜 파서(`task:[^)]+`)의 bound_session 매칭이 어긋나 task 연속성
        추론/본문 추출이 파손된다(codex 실증 `foo)bar` → 태그 `(task:foo)bar)` 조기 매칭).
      - path separator(`/`·`\\`) → 선행 `.`(숨김/`.`/`..` 상대) → 단일 컴포넌트 아님(traversal).
    한글·하이픈·언더스코어·숫자는 유지 — 어느 표면(CLI·log 태그·path)과도 무충돌."""
    if not name or not name.strip():
        raise worktree_records.InvalidTaskName(name, "빈 이름(공백 포함)")
    if any(ch.isspace() for ch in name):
        raise worktree_records.InvalidTaskName(
            name, "공백·탭 등 whitespace 불가 (CLI/relay `--task <task>` slash 인자 경계 파손 방지)"
        )
    if "(" in name or ")" in name:
        raise worktree_records.InvalidTaskName(
            name, "괄호 `(`·`)` 불가 (log 헤더 태그 `(task:<task>)` delimiter 파손 방지)"
        )
    if "/" in name or "\\" in name:
        raise worktree_records.InvalidTaskName(name, "path separator(`/`·`\\`) 불가 — 단일 이름이어야")
    if name.startswith("."):
        raise worktree_records.InvalidTaskName(name, "선행 `.` 불가(숨김/`.`/`..` 상대경로 traversal 방지)")
    if Path(name).name != name:
        raise worktree_records.InvalidTaskName(name, "단일 path 컴포넌트가 아님")
    if registered_repos:
        for repo in registered_repos:
            if re.match(rf"^{re.escape(repo)}_\d+$", name):
                raise worktree_records.InvalidTaskName(name, f"슬롯 폴더 이름 패턴(<{repo}>_<N>)")


def _validate_todo_doc(path: Path, doc: object) -> None:
    """할 일 파일 스키마 검증 — 어긋나면 어디가 어긋났는지 지목하고 올린다.

    이 파일을 쓰는 코드는 `_write_todo_doc` 하나뿐이라, 모양이 어긋났다면 손편집이거나 깨진
    파일이다. 어긋난 행을 조용히 버리지 않는다 — 버리면 열린 일이 소리 없이 사라지고, 그러면
    쓰기 판정이 "열린 할 일 없음" 으로 보고 경고를 멈춘다(실패가 눈에 안 보인다).
    `_read_tasks_strict` 가 장부 행에 하는 것과 같은 규율이다.
    """
    if not isinstance(doc, dict):
        raise ValueError(f"할 일 파일이 object 가 아니다: {path}")
    todos = doc.get("todos")
    if not isinstance(todos, list):
        raise ValueError(f"할 일 파일의 'todos' 값이 list 가 아니다: {path}")
    for index, todo in enumerate(todos):
        if (not isinstance(todo, dict)
                or not isinstance(todo.get("id"), int) or isinstance(todo.get("id"), bool)
                or not isinstance(todo.get("text"), str)
                or not isinstance(todo.get("created"), str)):
            raise ValueError(f"할 일 파일의 todos[{index}] 형식이 어긋난다: {path}")
    next_id = doc.get("next_id")
    if not isinstance(next_id, int) or isinstance(next_id, bool):
        raise ValueError(f"할 일 파일의 'next_id' 값이 정수가 아니다: {path}")
    highest = max((todo["id"] for todo in todos), default=0)
    if next_id <= highest:
        raise ValueError(
            f"할 일 파일의 next_id({next_id})가 이미 쓴 ID({highest}) 이하다 — "
            f"그대로 두면 한 번 쓴 번호를 다시 내준다: {path}"
        )


def _render_initial_task_pm_state(template_text: str, date_str: str) -> str:
    """공용 pm_state template을 task 첫 세션용 state로 렌더한다.

    공용 template의 예시 `1차`는 완료된 세션 기록이 아니다. task 생성 시 그대로 복사하면 첫
    bootstrap이 2차로 오인하므로, 세션 entry·이전 차 포인터를 비우고 명시 marker를 둔다.
    `pm_handoff.update_session_window`가 첫 핸드오프 때 이 marker를 실제 1차 entry로 교체한다.
    """
    rendered = template_text.replace("{{DATE}}", date_str)
    rendered = rendered.replace(
        "`pm_state.template.md` 에서 `pm-init` 이 생성.",
        "`pm_state.template.md` 에서 task 생성/재개 시 자동 생성.",
        1,
    )
    anchor = "## 세션 식별 (현재까지 사용된 이름)"
    start = rendered.find(anchor)
    if start < 0:
        raise ValueError(f"pm_state template에 세션 식별 앵커가 없다: {anchor}")
    next_header = re.search(r"^##(?!#) ", rendered[start + len(anchor):], re.MULTILINE)
    end = (
        len(rendered)
        if next_header is None
        else start + len(anchor) + next_header.start()
    )
    section = rendered[start:end]
    first_entry = re.search(r"^  - \*\*\d+차\*\*.*(?:\n|$)", section, re.MULTILINE)
    if first_entry is not None:
        section = (
            section[:first_entry.start()]
            + identity_args.TASK_PM_STATE_EMPTY_MARKER
            + "\n"
            + section[first_entry.end():]
        )
    else:
        session_list = "최근 N 차 (sliding window, 기본 3 차):"
        marker_at = section.find(session_list)
        if marker_at < 0:
            raise ValueError(f"pm_state template에 세션 목록 앵커가 없다: {session_list}")
        insert_at = marker_at + len(session_list)
        section = (
            section[:insert_at]
            + "\n"
            + identity_args.TASK_PM_STATE_EMPTY_MARKER
            + section[insert_at:]
        )
    section = re.sub(
        r"^  - \*\*\d+차\*\*.*(?:\n|$)",
        "",
        section,
        flags=re.MULTILINE,
    )
    section = re.sub(
        r"^  - 이전 차 .*?(?:\n|$)",
        "",
        section,
        flags=re.MULTILINE,
    )
    return rendered[:start] + section + rendered[end:]
