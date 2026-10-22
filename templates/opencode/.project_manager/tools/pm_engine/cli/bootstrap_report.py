"""부트스트랩 측정값의 Markdown·JSON 출력."""
from __future__ import annotations
from pm_engine.infra import pre_push_hook
from pm_engine.domain import git_freshness
from pathlib import PurePath
import re
from pm_engine.infra import task_store
from pm_engine.cli import bootstrap_card
from pm_engine.domain import bootstrap_rules


def _display_path_text(path) -> str:
    """경로를 사람/비교용 표기(**POSIX 단일**)로 직렬화한다 — 문자열화 지점만 고정한다.

    Windows 에서 `str(WindowsPath(...))` 는 `\\` 구분자를 만들어 같은 경로가 OS 를 건너 다른
    문자열로 읽힌다. task pm_state 포인터는 부트스트랩 dump 를 읽는 쪽(PM·스킬·`--json` 소비자)이
    문자열로 대조하는 값이라 표기가 갈리면 안 된다. 파일시스템 호출에 넘기는 자리는 `Path` 를
    그대로 두고 **문자열화 지점만** POSIX 로 고정한다.

    같은 규칙의 lease 슬롯 축 구현이 `pm_handoff._lease_slot_path_text`다. 그쪽은
    repo 상대화(장부 슬롯 키 산출)까지 하는 lease 전용 해소라 여기(표시 경로 직렬화)와 스코프가
    다르고, 재사용하면 표시 경로가 repo 상대로 바뀐다 — 규칙(POSIX 단일)만 공유한다.

    `PurePath` 가 아닌 값(이미 문자열화된 경로)은 그대로 돌려준다 — 구분자를 추측해 치환하면
    POSIX 파일명에 정당하게 들어간 `\\` 를 망가뜨린다.
    """
    if isinstance(path, PurePath):
        return path.as_posix()
    return str(path)


_TASK_PM_STATE_POINTER_LABEL = "pm_state (이 task·서술)"


def _render_task_pm_state_pointer(path) -> str:
    """task 서술 pm_state 포인터 줄을 렌더한다 — 경로 표기는 POSIX 단일."""
    return f"- {_TASK_PM_STATE_POINTER_LABEL}: `{_display_path_text(path)}`"


def _hook_generation_notice(lint_output: str) -> str | None:
    """lint gate 출력이 훅 세대 차단이면 그 줄, 아니면 None.

    판정 접두는 `pre_push_hook.PM_HOOK_NOTICE_PREFIX` 가 소유한다 — 여기 사본을 두면 엔진이
    문구를 바꿀 때 조용히 어긋난다.
    """
    prefix = pre_push_hook.PM_HOOK_NOTICE_PREFIX
    for line in lint_output.splitlines():
        if line.startswith(prefix):
            return line.strip()
    return None


def format_blocking_lint_warning(
    lint_output: str,
    *,
    representative_limit: int = bootstrap_rules._LINT_REPRESENTATIVE_LIMIT,
    lint_rc: int | None = None,
) -> str:
    """blocking lint의 red 신호는 유지하면서 기본 stderr를 bounded 요약으로 만든다.

    전체 gate 전문은 이미 `lint_output`에 보존되어 있으나 기본 bootstrap 출력에는 총계,
    blocking 수, 대표 항목만 싣는다. 전체 진단은 안내한 `board.py lint --gate`를 사용자가
    명시 실행할 때만 출력한다.
    """
    if representative_limit < 0:
        raise ValueError("representative_limit은 0 이상이어야 한다")
    counts = bootstrap_rules.parse_lint_counts(lint_output)
    opt_in = "전체 진단(opt-in): `python3 .project_manager/tools/board.py lint --gate`"
    if counts is None:
        raw_nonblank = [
            f"  {line}" for line in lint_output.splitlines() if line.strip()
        ]
        raw_excerpt = raw_nonblank[: bootstrap_rules._LINT_ERROR_EXCERPT_LIMIT]
        if not raw_excerpt:
            raw_excerpt = ["  <출력 없음>"]
        rc_label = str(lint_rc) if lint_rc is not None else "nonzero"

        def build_error_lines(selected: list[str]) -> list[str]:
            omitted = max(len(raw_nonblank) - len(selected), 0)
            lines = [
                f"[경고] board lint gate 실행/출력 파싱 실패 (rc={rc_label}) — blocking 비0 신호는 유지됨",
                f"오류 excerpt {len(selected)}줄:",
                *selected,
            ]
            if omitted:
                lines.append(f"  … 추가 excerpt {omitted}줄 생략")
            lines.append(opt_in)
            return lines

        return bootstrap_rules._bounded_warning_with_details(
            raw_excerpt, build_error_lines
        )
    total, blocking = counts
    issues = bootstrap_rules._lint_issue_lines(lint_output)
    candidates = (
        [line for line in issues if bootstrap_rules._LINT_BLOCKING_LINE.match(line)]
        + [
            line
            for line in issues
            if not bootstrap_rules._LINT_BLOCKING_LINE.match(line)
        ]
    )[:representative_limit]

    def build_summary_lines(selected: list[str]) -> list[str]:
        lines = [
            f"[경고] board lint 차단(blocking) 이슈 — 위 dump 는 정상 출력됨: 총계 {total}건 · blocking {blocking}건",
            f"대표 항목 {len(selected)}건:",
            *selected,
        ]
        omitted = max(total - len(selected), 0)
        if omitted:
            lines.append(f"  … 나머지 {omitted}건 생략")
        lines.append(opt_in)
        return lines

    return bootstrap_rules._bounded_warning_with_details(
        candidates, build_summary_lines
    )


def parse_lint_result(lint_output: str) -> str:
    """board lint 출력에서 결과 요약을 반환한다.

    "✓ no lint issues" 이면 "clean" 반환.
    경고가 있으면 해당 줄 수를 "N warnings" 형식으로 반환.

    `--gate` 출력은 헤더 `⚠️  N lint issue(s) (M blocking 차단):` 다음에 각 issue
    줄(`✗`/공백 마크 + `[kind] …`)이 온다. issue 줄만 **양성 매칭**한다. 헤더를
    제외 목록으로 관리하면 새 표시 헤더가 추가될 때마다 같은 off-by-one이 재발한다

    """
    if any(
        (
            re.match("^\\s*✓\\s*no lint issues\\s*$", line)
            for line in lint_output.splitlines()
        )
    ):
        return "clean"
    warning_lines = bootstrap_rules._lint_issue_lines(lint_output)
    count = len(warning_lines)
    if count == 0:
        return "clean"
    return f"{count} warnings"


_SESSION_LABEL_PLACEHOLDER = "PM <?>차"
_COUNTS_LEADING_STATUSES: tuple[str, ...] = ("done",)


def _ordered_count_statuses(counts: dict[str, int]) -> list[str]:
    """카운트 렌더 순서 — 표시 우선 status 를 앞세우고 나머지는 `counts` 삽입 순서 그대로."""
    leading = [status for status in _COUNTS_LEADING_STATUSES if status in counts]
    return [*leading, *(status for status in counts if status not in leading)]


def _board_count_parts(
    counts: dict[str, int], scope_label: str, *, delimiter: str
) -> list[str]:
    """status 별 카운트 조각 — 두 표면(카운트 줄·권장 첫 turn 요약)이 집합·순서를 공유한다.

    `delimiter` 는 status 와 수 사이 표기만 가른다(카운트 줄 `": "` · 요약 문장 `" "`) — 종전
    bytes 를 그대로 두면서 status 집합만 board 파생으로 바꾸기 위한 유일한 차이다.
    """
    return [
        f"{status}{delimiter}{counts[status]} ({scope_label})"
        for status in _ordered_count_statuses(counts)
    ]


def _format_board_counts_line(counts: dict[str, int], scope_label: str = "mine") -> str:
    """board 카운트 한 줄을 만든다 — 수집 스코프 라벨 명확화.

    `counts` 는 `_collect_board` 가 뽑은 스코프 값이다 — status 별로 "내 세션 생성 open" 또는 "내
    세션 claim" 만 센 값이라 실측(예 done 25)이 전체 done(184) 과 크게 다를 수 있다(done/open/
    claimed/blocked 모두 전체보다 훨씬 작을 소지가 큼). `scope_label` 로 그 스코프를 명시해 "전체
    done" 처럼 오독하지 않게 한다 — 라벨은 이 세션의 task(`"task <task>"`)다.

    **open 도 세션 스코프**: 세션 기본 뷰가 open 을 내 세션 생성분만 보이므로
    슬롯무관 공유 backlog 전량·접힘 카운트 폐기) 모든 status 에 같은 `scope_label` 을 붙인다 —
    옛 `_OPEN_SCOPE_LABEL`(공유 backlog 정정) 축이 소멸(open 이 더는 전역 대기열 수가 아님).
    """
    return "- " + " / ".join(_board_count_parts(counts, scope_label, delimiter=": "))


def _format_board_git_freshness(board_git: dict) -> str:
    """board submodule freshness 한 줄을 만든다 — HEAD·dirty·ahead/behind.

    `board_git` = `_collect_board_git()` 반환(`head`·`dirty`·`ahead`·`behind`). ahead/behind
    가 둘 다 None(upstream 미설정/조회불가)이면 그 구간을 생략 — dirty·head 만 있어도
    유의미(부분 degrade). dirty=False 면 "clean", True 면 "dirty".
    """
    parts = [f"HEAD {board_git['head']}", "dirty" if board_git["dirty"] else "clean"]
    ahead, behind = (board_git.get("ahead"), board_git.get("behind"))
    if ahead is not None and behind is not None:
        parts.append(f"{ahead} ahead / {behind} behind")
    return " · ".join(parts)


def _format_session_label(handoff_ctx: dict | None) -> str:
    """차수 announce 머리표를 만든다 — `PM <N>차` / placeholder (crash 금지).

    `handoff_ctx["session_num"]` 가 정수면 `PM <N>차`, `"?"`(entry 부재 placeholder)·None·
    handoff_ctx 부재(pm_state 미해소)면 `PM <?>차` placeholder. self-surface 헤더라 미해소도
    graceful — 부트스트랩 본체를 깨지 않는다.
    """
    if handoff_ctx is None:
        return _SESSION_LABEL_PLACEHOLDER
    num = handoff_ctx.get("session_num")
    if isinstance(num, int):
        return f"PM {num}차"
    return _SESSION_LABEL_PLACEHOLDER


def _format_stale_warning(handoff_ctx: dict | None) -> str | None:
    """pm_state stale 교차검증 경고 1줄 — log-derived 차수가 pm_state 보다 앞설 때.

    `handoff_ctx["session_stale"]` 가 True(=`reconcile_session_num` 이 log 우선 max 를 택함)면
    경고 문자열, 아니면 None(줄 생략). per-slot pm_state 가 git-ignored 라 머신 간 미동기임을
    표면화한다 — final(log 우선) vs state(뒤처진 pm_state 값) 둘 다 담아 진단 가능하게.
    """
    if not handoff_ctx or not handoff_ctx.get("session_stale"):
        return None
    final = handoff_ctx.get("session_num")
    state = handoff_ctx.get("state_session_num")
    return f"⚠ pm_state stale (머신 간 미동기) — log 기준 PM {final}차 우선 (pm_state 는 PM {state}차)"


_SUBMODULE_KIND_LABEL = {
    "dev-ahead": "dev-ahead(정보)",
    "drift": "drift(pin≠working)",
    "pinned": "pinned",
    "uninitialized": "uninitialized",
}


def _build_markdown(
    board: dict,
    pytest_result: dict | None,
    git: dict,
    log_entry: dict | None,
    timestamp: str,
    handoff_ctx: dict | None = None,
    dashboard_others: dict | None = None,
    *,
    task_name,
    state_path,
    environment,
) -> str:
    counts = board["counts"]
    open_tickets = board["open_tickets"]
    lint = board["lint"]
        # 보장된 task pm_state(완료 세션 0개 marker 포함)에서 1차 이상의 정수로 해소된다.
    session_label = _format_session_label(handoff_ctx)
    lines: list[str] = []
    lines.append(f"## {session_label} 부트스트랩 ({timestamp})")
    lines.append(bootstrap_card._render_environment_line(environment))
    stale_warning = _format_stale_warning(handoff_ctx)
    if stale_warning:
        lines.append(stale_warning)
    user_continuity = git.get("user_continuity")
    if user_continuity:
        lines.append(user_continuity)
    lines.append("")
    lines.append("### Board")
    lines.append(_format_board_counts_line(counts, board.get("counts_scope", "mine")))
    if pytest_result is not None:
        scope_count = len(pytest_result.get("scopes", []))
        scope_note = f" (task 작업공간 {scope_count}개 전수)" if scope_count else ""
        lines.append(
            f"- 회귀: {pytest_result['passed']} / {pytest_result['total']} 통과{scope_note}"
        )
    else:
        lines.append("- 회귀: (skip — handoff entry 참조 · --with-pytest 로 재측정)")
    lines.append(f"- lint: {lint}")
    if open_tickets:
        lines.append(f"- open ticket (claim 가능): {', '.join(open_tickets)}")
    else:
        lines.append("- open ticket (claim 가능): (없음)")
    lines.append("")
    lines.append("### Git")
    lines.append(f"- branch: {git['branch']}")
    if git.get("no_commits"):
        lines.append("- commit: (초기 커밋 없음 — fresh clone)")
    elif git["commits"]:
        head_sha, head_subject = git["commits"][0]
        lines.append(f"- commit: {head_sha} {head_subject}")
        lines.append("- 마지막 3 commit:")
        for sha, subject in git["commits"][:3]:
            lines.append(f"  - {sha} {subject}")
    lines.append(f"- working tree: {git['working_tree']}")
    if "task_workspace_count" in git:
        task_count = git["task_workspace_count"]
        task_cwd_slot = git.get("task_cwd_slot")
        if task_cwd_slot:
            lines.append(
                f"- task Git 대표 cwd: `{task_cwd_slot}` (보유 {task_count}개 중 정렬 첫 슬롯 · freshness/회귀는 전수)"
            )
        else:
            lines.append(
                "- task 작업공간: 0개 — Git은 PM 홈 기준 · `--with-pytest`는 대상 없음으로 차단"
            )
    for scope in git.get("freshness", []):
        lines.append(
            f"- freshness ({scope['label']}): {git_freshness._format_freshness(scope)}"
        )
        board_sync = scope.get("board_sync")
        if board_sync is not None:
            lines.append(
                f"  - {board_sync['label']}: {git_freshness._format_freshness(board_sync)}"
            )
    board_git = git.get("board_git")
    if board_git is not None:
        lines.append(f"- board: {_format_board_git_freshness(board_git)}")
    lines.append("")
    lines.append("### log/current.md 마지막 entry")
    if log_entry:
        lines.append(f"- date: {log_entry['date']}")
        lines.append(f"- type: {log_entry['type']}")
        lines.append(f"- title: {log_entry['title']}")
        body = log_entry.get("body")
        if body:
            lines.append("")
            lines.append("<details><summary>본문 (인계 컨텍스트)</summary>")
            lines.append("")
            lines.append(body)
            lines.append("")
            lines.append("</details>")
        else:
            lines.append("- (본문 파싱 실패 — `pm_log.py tail` 로 직접 확인)")
    else:
        lines.append("- (log/current.md 없음 또는 entry 파싱 실패)")
    lines.append("")
    lines.append("### 열린 todo")
    todos = handoff_ctx.get("todos") if handoff_ctx else None
    if todos:
        lines.append("")
        for todo in todos:
            lines.append(f"- {task_store.format_todo_line(todo)}")
    else:
        lines.append(
            f"- 없음 — `pm-config task todo add {task_name} <할 일 한 줄>` 로 등록한다 (등록 전에 파일을 고치면 쓰기 판정이 경고를 붙인다)."
        )
    lines.append("")
    if dashboard_others and dashboard_others.get("others"):
        lines.append("### 다른 활성 PM")
        if dashboard_others.get("mode") == "dashboard":
            lines.append("(수정형 대시보드 — 각 슬롯 자기 기록·상세는 log/current.md)")
            for other in dashboard_others["others"]:
                lines.append("")
                lines.append(f"**{other['session']}**")
                body = other.get("body")
                if body:
                    lines.append(body)
        else:
            lines.append("(대시보드 부재 — 현행 lease 목록 폴백)")
            for other in dashboard_others["others"]:
                lines.append(f"- `{other['session']}` · `{other['slot']}`")
        lines.append("")
    lines.append("### 권장 첫 turn")
    lines.append("PM 세션 시작합니다.")
    _scope = board.get("counts_scope", "mine")
    _summary_parts = _board_count_parts(counts, _scope, delimiter=" ")
    board_summary = " / ".join(_summary_parts) + "."
    if pytest_result is not None:
        regression_summary = (
            f" 회귀 {pytest_result['passed']} / {pytest_result['total']}, lint {lint}."
        )
    else:
        regression_summary = f" 회귀 (handoff entry 참조), lint {lint}."
    lines.append(f"- board: {board_summary}{regression_summary}")
    lines.append(
        f'- (직전 세션 요약은 PM 손 — {state_path} "세션 식별" 절 + log/current.md 마지막 handoff entry 참조)'
    )
    lines.append("- 무엇부터 갈까요? (위 열린 todo + open ticket 목록 보고 옵션 제시)")
    return "\n".join(lines)


def _build_json(
    board: dict,
    pytest_result: dict | None,
    git: dict,
    log_entry: dict | None,
    timestamp: str,
    handoff_ctx: dict | None = None,
    dashboard_others: dict | None = None,
    *,
    task_name,
    environment,
) -> dict:
    counts = board["counts"]
    counts_scope = board.get("counts_scope", "mine")
    return {
        "timestamp": timestamp,
        "environment": {
            "os": environment.os_label,
            "python": bootstrap_card._render_python_argv(environment.python_argv),
            "python_argv": list(environment.python_argv),
            "python_source": environment.python_source,
            "chain_policy": environment.chain_policy,
            "policy_basis": "os-safe-rendering-not-shell-detection",
        },
        "session_num": handoff_ctx.get("session_num") if handoff_ctx else None,
        "handoff_context": handoff_ctx,
        "board": {
            "done": counts.get("done", 0),
            "open": counts.get("open", 0),
            "claimed": counts.get("claimed", 0),
            "blocked": counts.get("blocked", 0),
            "counts_scope": counts_scope,
            **(
                {"counts_task": dict(counts)}
                if task_name is not None
                else {"counts_mine": dict(counts)} if counts_scope == "mine" else {}
            ),
            "open_tickets": board["open_tickets"],
            "lint": board["lint"],
        },
        "pytest": (
            {
                "passed": pytest_result["passed"],
                "total": pytest_result["total"],
                **(
                    {"scopes": pytest_result["scopes"]}
                    if "scopes" in pytest_result
                    else {}
                ),
            }
            if pytest_result is not None
            else None
        ),
        "git": {
            "branch": git["branch"],
            "commits": [
                {"sha": sha, "subject": subject} for sha, subject in git["commits"]
            ],
            "no_commits": git.get("no_commits", False),
            "working_tree": git["working_tree"],
            **(
                {"task_cwd_slot": git["task_cwd_slot"]}
                if "task_cwd_slot" in git
                else {}
            ),
            **(
                {"task_workspace_count": git["task_workspace_count"]}
                if "task_workspace_count" in git
                else {}
            ),
            "board_git": git.get("board_git"),
            "freshness": git.get("freshness"),
            "user_continuity": git.get("user_continuity"),
        },
        "log_last_entry": log_entry,
        "dashboard_others": dashboard_others,
    }


def _build_task_identity_markdown(task_info: dict) -> str:
    """task identity surface — 정체성·prefix 상태(기본 없음)·서술 pm_state 포인터.

    prefix 는 이 task 세션의 board prefix 상태를 surface 한다(기본 None → "(없음)") → PM 이
    사용자와 확인. 변경 명령은 `task prefix`. 작업공간(슬롯) 연결은 alloc이
    채우므로 여기선 task 정체성·prefix·서술 상태만 보인다(신규는 슬롯 0개로 시작 가능).
    """
    name = task_info["name"]
    prefix = task_info.get("prefix")
    prefix_display = f"`{prefix}`" if prefix else "(없음)"
    action = task_info.get("action")
    action_label = {"created": "신규 task", "resumed": "재개(resume)"}.get(
        action, action or ""
    )
    pm_state_path = task_info.get("pm_state_path")
    lines: list[str] = []
    lines.append("### task identity surface ")
    lines.append(
        f"- 당신은 **task `{name}`** PM · prefix={prefix_display} · 상태={action_label}."
    )
    lines.append(
        f"- **prefix 상태 = {prefix_display}** (기본=없음) — 사용자와 확인. 변경은 `task prefix`."
    )
    slot_set = task_info.get("slot_set") or []
    if slot_set:
        _append_task_slot_matrix(lines, name, slot_set)
    else:
        lines.append(
            f"- 작업 슬롯: (없음) — 코드 작업(repo 파일 수정·커밋) 전에 `{bootstrap_rules._CARD_TOOL_INVOKE}/pm_config.py alloc <repo> --task {name}` 으로 잡는다. 잡지 않은 슬롯에는 쓰지 않는다."
        )
    if pm_state_path:
        lines.append(_render_task_pm_state_pointer(pm_state_path))
    return "\n".join(lines)


_RECORD_LIVE_MARK = {
    "ok": "✓",
    "unrecorded": "미기록",
    "unknown": "미상",
    "diverged": "⚠diverged",
}


def _append_task_slot_matrix(lines: list, task: str, slot_set: list) -> None:
    """보유 슬롯 전수 열거 행렬을 `lines` 에 추가.

    헤더 `작업공간 (task 'X' 보유 N — 전수 검증):` + 슬롯별 `slot · repo · branch · head ·
    기록↔live <mark> · <dirty>` 행(`_render_task_slots`[cli.config_slots]과 같은 ` · ` bullet
    문법)."""
    lines.append(f"- 작업공간 (task {task!r} 보유 {len(slot_set)} — 전수 검증):")
    for row in slot_set:
        dirty_disp = "dirty" if row["dirty"] else "clean"
        mark = _RECORD_LIVE_MARK.get(row["record_live"], row["record_live"])
        lines.append(
            f"    - {row['slot']} · repo={row['repo']} · branch={row['branch']} · head={row['head']} · 기록↔live {mark} · {dirty_disp}"
        )
