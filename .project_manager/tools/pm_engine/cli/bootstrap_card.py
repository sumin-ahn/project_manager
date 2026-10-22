"""환경·task 값에 따른 부트스트랩 명령 카드."""
from __future__ import annotations
from typing import NamedTuple
import os
from pm_engine.infra import python_env


def _render_python_argv(argv: tuple[str, ...]) -> str:
    """Render already-resolved launcher argv only at the Markdown boundary."""
    if not argv:
        return "python3"
    return " ".join(argv)


def _render_environment_line(environment: python_env._CommandEnvironment) -> str:
    """Human dump line; chain policy is explicitly OS-safe, not shell detection."""
    return f"현재 환경: {environment.os_label} · python={_render_python_argv(environment.python_argv)} · chain_policy={environment.chain_policy} (OS-safe 표기 정책·실제 셸 감지 아님)"


def _resolve_card_tool_invoke(
    home, environment: python_env._CommandEnvironment | None = None
) -> str:
    """커맨드 카드 도구 접두를 per-clone `local.conf`의 `runtime.py=` 값으로 완성한다.

    설정 파일 부재·읽기 실패·키 부재·빈 값은 OS 기본으로 조용히 폴백한다. 인터프리터의 실행
    가능 여부와 지원 버전은 설정을 기록하는 초기화 단계가 검증하므로 여기서는 선택을 반복하지
    않고 표기만 일치시킨다.
    """
    resolved = environment or python_env._detect_command_environment(home)
    return f"{_render_python_argv(resolved.python_argv)} .project_manager/tools"


class _CardCmd(NamedTuple):
    tool: str
    render: str
    subpath: tuple
    flags: tuple


_C_BOARD_LIST_MINE = _CardCmd("board.py", "list --mine", ("list",), ("--mine",))
_C_BOARD_LIST = _CardCmd("board.py", "list", ("list",), ())
_C_BOARD_NEW = _CardCmd("board.py", 'new "<제목>"', ("new",), ())
_C_BOARD_PROMOTE = _CardCmd("board.py", "promote T-NNNN", ("promote",), ())
_C_BOARD_SHOW = _CardCmd("board.py", "show T-NNNN", ("show",), ())
_C_BOARD_LINT = _CardCmd("board.py", "lint --gate", ("lint",), ("--gate",))
_C_BOARD_CLAIM = _CardCmd("board.py", "claim T-NNNN", ("claim",), ())
_C_BOARD_CLUSTER_NEW = _CardCmd(
    "board.py",
    "cluster new <ticket-group> --tickets T-NNNN",
    ("cluster", "new"),
    ("--tickets",),
)
_C_BOARD_TIER = _CardCmd("board.py", "tier T-NNNN pm-direct", ("tier",), ())
_C_WP_SWITCH = _CardCmd(
    "worktree_pool.py", "switch <slot> task/<ticket-group>", ("switch",), ()
)
_C_BOARD_REGRESSION = _CardCmd("board.py", "regression run", ("regression",), ())
_C_TICKET_FINISH = _CardCmd("ticket_finish.py", "<T-NNNN>", (), ())
_C_ADDITIONAL_REVIEWER = _CardCmd(
    "additional_reviewer.py",
    "--ticket T-NNNN --adr ADR-NNNN",
    (),
    ("--ticket", "--adr"),
)
_C_PM_HANDOFF_TASK = _CardCmd("pm_handoff.py", "", (), ())
_C_PM_LOG_TAIL = _CardCmd("pm_log.py", "tail", ("tail",), ())
_C_PC_ALLOC = _CardCmd("pm_config.py", "alloc <repo>", ("alloc",), ())
_C_PC_RELEASE = _CardCmd("pm_config.py", "release <slot>", ("release",), ())
_C_PC_TASK_END = _CardCmd("pm_config.py", "task end <task>", ("task", "end"), ())
_C_PC_TASK_REOPEN = _CardCmd(
    "pm_config.py", "task reopen <task> [--archive <basename>]", ("task", "reopen"), ()
)
_C_PC_TASK_PREFIX = _CardCmd(
    "pm_config.py", "task prefix <task> <p|none>", ("task", "prefix"), ()
)
_CARD_TASK_CLI = (
    _C_BOARD_LIST,
    _C_BOARD_LIST_MINE,
    _C_PC_ALLOC,
    _C_PC_RELEASE,
    _C_PC_TASK_PREFIX,
    _C_PC_TASK_END,
    _C_PC_TASK_REOPEN,
    _C_BOARD_NEW,
    _C_BOARD_PROMOTE,
    _C_BOARD_TIER,
    _C_BOARD_CLUSTER_NEW,
    _C_BOARD_CLAIM,
    _C_BOARD_SHOW,
    _C_BOARD_LINT,
    _C_WP_SWITCH,
    _C_BOARD_REGRESSION,
    _C_ADDITIONAL_REVIEWER,
    _C_TICKET_FINISH,
    _C_PM_HANDOFF_TASK,
    _C_PM_LOG_TAIL,
)
_CODEX_HARNESS_SESSION_MARKERS: tuple[str, ...] = ("CODEX_THREAD_ID", "CODEX_CI")


def _is_codex_harness() -> bool:
    """codex 하네스면 True — `CODEX_THREAD_ID` 또는 `CODEX_CI` env 마커(기계 판정·추측 아님)."""
    return any((os.environ.get(marker) for marker in _CODEX_HARNESS_SESSION_MARKERS))


def _runtime_skill_entry(skill: str) -> str:
    """사용자에게 제시할 현재 하네스의 스킬 호출 표기."""
    return f"{('$' if _is_codex_harness() else '/')}{skill}"


_CODEX_CARD_SECTION = "\n".join(
    (
        "# codex 하네스 (실행모델·위임 — 정적 진입 doc 없음)",
        "- **위임** — 설계·구현·리뷰·조사는 target 하네스 == PM 하네스면 native, 다르면 `pm_delegate.py` cross 다. codex 의 native 위임은 `.codex/agents/{architect,code-reviewer,developer,researcher}` 를 codex 가 이 세션 안에서 스폰한다(부모 sandbox 상속·`codex exec --agent` 플래그 부재).",
        "- **trust 2단계** — 대화형 `codex` 1회 열어 프로젝트 trust 수락 `/hooks` 로 hook trust 승인. `-c projects.<path>.trust_level=trusted` CLI override 는 안 먹음(실측).",
        "- **방법론 소재** — 공통 코어 `AGENTS.md`(codex 자동 로드) + 이 카드 + `.agents/skills`(`$<스킬명>` 멘션(예 `$pm-bootstrap`)·auto-trigger). `CLAUDE.md` 는 codex 미로드.",
    )
)


def _safe_command_card(home, task_name, *, environment=None) -> str | None:
    """커맨드 카드를 fail-soft 로 렌더한다 — 실패하면 None(카드 절 생략·부트스트랩 유지).

    카드 렌더 실패(정체성 dict 결손·예기치 못한 예외)는 부트스트랩 자체를 깨뜨리면 안
    된다 — identity surface·board/git dump 는 이미 나갔으므로
    카드만 조용히 생략한다.
    """
    try:
        return _build_command_card_markdown(home, task_name, environment=environment)
    except Exception:
        return None


def _build_command_card_markdown(home, task_name, *, environment=None) -> str:
    """이 세션이 쓸 전 커맨드를 정체성 채운 완성형으로 dump 하는 커맨드 카드.

    identity surface 뒤에 코드 생성해, PM 이 --help 왕복 없이 세션 전체를 운영하게 한다
    ("--help 자체를 안 가게"·사용자 지시). 정체성(`--task <task>`)은 **실값으로 보간**하고,
    사용자 입력(`T-NNNN`·`<PFX>`·`<요약>` 등)만 placeholder 로 남긴다.

    숨은 전제 4대장(claim=promote 선행·prefix rename/merge=홈 git clean·livegate record=
    release-marked pin·migrate-identity=단일세션) + reid=홈 git clean 을 해당 커맨드 줄 바로
    아래 1줄 ⚠ 경고로 인접 배치한다(별도 절 금지·인접성이 학습 보장).

    wave 운영(claim·regression·finish·qa·dev 위임·handoff·엔진 갱신)은 **스킬(`/pm-…`) 진입을
    primary** 로 올리고 감싸는 backbone 은 강등한다(boundary=래핑 스킬
    유무). 강등 줄은 pm_role skill 카탈로그의 "감싸는 내부 엔진" 열과 정확히 일치시킨다(카드=
    pm_role 표기). **엔진이 Python CLI(`tools/*.py` — board.py/ticket_finish.py/
    pm_handoff.py)일 때만** "직접 금지" 강등 줄로 그리고, 엔진이 Agent 툴(`/pm-dev-delegate`)·
    facade 셸(`/pm-update`=pm-update.sh)이면 python3 줄을 지어내지 않고 skill-only + 평문 note
    로 둔다. additional_reviewer 는 래핑 스킬 없는 별도 추가 리뷰어 게이트라 강등이 아니라 직접-CLI 예외
    (`board.py new`/`promote` 발행 authoring 도 동일 · 완료 진입은 /pm-wave-finish 하나라
    direct `board.py complete` 줄은 카드에 없다). 강등 = 제거 아님: CLI backbone
    줄은 정체성 보간·⚠ 인접·argparse 정합 가드를 위해 남긴다. 규칙·why 는 재설명하지 않고
    카드 상단 1줄 pointer 로 pm_role 규율 절을 가리킨다.
    """
    environment = environment or python_env._detect_command_environment(home)
    tool_invoke = _resolve_card_tool_invoke(home, environment=environment)

    def cmd(
        rec: _CardCmd, comment: str = "", suffix: str = "", prefix: str = ""
    ) -> str:
        """공용 정의서 record → 해소된 인터프리터로 도구를 호출하는 한 줄.

        커맨드 토큰(도구·서브커맨드·플래그)은 `rec`(공용 정의서) 단일 진실에서 온다 —
        손 문자열 하드코딩 제거. `suffix`/`prefix` = 정체성(`--repo/--slot`·task 명) 등 caller 가
        실값 보간하는 꼬리/머리("정체성은 실값·사용자 입력만 placeholder"; pm_handoff 는
        정체성이 앞에 오므로 `prefix`).
        """
        line = f"{tool_invoke}/{rec.tool} {prefix}{rec.render}{suffix}".rstrip()
        return f"{line}  # {comment}" if comment else line

    def skill(invocation: str, comment: str = "") -> str:
        """`/pm-…` 스킬 진입 줄(wave 운영 primary).

        해소된 도구 접두로 시작하지 않으므로 카드↔CLI argparse 정합 가드·정체성 `--repo/--slot`
        검사(불변식 3)의 대상이 아니다 — backbone 은 아래 `engine()` 줄로 종속화한다.
        """
        rendered_invocation = _runtime_skill_entry(invocation.lstrip("/$"))
        return f"{rendered_invocation}  # {comment}" if comment else rendered_invocation

    def engine(
        rec: _CardCmd,
        note: str = "스킬이 부르는 내부 엔진·직접 금지",
        suffix: str = "",
        prefix: str = "",
    ) -> str:
        """스킬에 종속된 backbone 줄 — 2-스페이스 들여쓰기 + '직접 금지' 주석(강등 표기).

        해소된 도구 접두로 시작해(들여쓰기는 strip 됨) 정체성 `--repo/--slot` 보간·카드↔CLI
        argparse 정합 가드의 대상으로 남는다(불변식 1·3 무손상). 스킬 줄(`/pm-…`)만 그
        가드 밖이다.
        """
        return "  " + cmd(rec, f"↳ {note}", suffix, prefix)

    return (
        _task_command_card_lines(task_name, tool_invoke, cmd, skill, engine)
        + _codex_card_section()
    )


def _card_navigation_lines(self_tag: str, tool_invoke: str) -> list[str]:
    """찾아가기 포인터 절 (모드 공용·부트스트랩 dump 에 없는 것 — 평시 안 읽음·필요할 때만)."""
    return [
        "# 찾아가기 (부트스트랩에 없는 것 — 평시 안 읽음·필요할 때만)",
        f"- 내 티켓 상세: `{tool_invoke}/board.py show T-NNNN`",
        f"- 내 과거 세션: `wiki/log/current.md` 에서 자기 태그 {self_tag} 검색(핸드오프 entry)",
        "- 타 PM 현황: 부트스트랩 대시보드(상세는 그 슬롯 태그 log entry)",
        "- 현재-아키텍처: `wiki/architecture.md`(충돌 시 단일 진실)",
        "- 결정 히스토리: `wiki/decisions/README.md` 색인(ADR 상한)",
        "- 방법론·규율: `wiki/pm_role.md`",
    ]


def _codex_card_section() -> str:
    """codex 하네스면 카드 codex 절(앞 공백 1줄 포함)을, 아니면 빈 문자열을 돌려준다.

    모든 모드 카드(슬롯·task·readonly) 렌더 끝에 append 되며, 호출이
    `_build_command_card_markdown`(→ `_safe_command_card` try/except) 안이라 감지/문자열화가
    터져도 fail-soft(절 생략·부트스트랩 무손상). 비-codex 하네스는
    빈 문자열 → 카드 byte 무변(env 미설정=절 부재=정상·회귀 0).
    """
    return f"\n\n{_CODEX_CARD_SECTION}" if _is_codex_harness() else ""


def _task_command_card_lines(
    task_name: str, tool_invoke: str, cmd, skill, engine
) -> str:
    """task 모드 커맨드 카드 — task 세션이 쓸 task-스코프 커맨드만 dump.

    task 세션의 진입 정체성 앵커는 task 명 하나(`--task <name>`)다(log/pm_state 귀속·
    board/lease 소유검사). 보유 슬롯 집합은 장부에서 자동 수령한다. 슬롯-전용 커맨드
    (worktree 관리·prefix rename/merge·livegate)는 안
    뿌린다(신호 대 잡음·컨텍스트 잠식 방지) — task 작업공간 대여/반납·task 정체성·task-스코프
    wave 운영만. 커맨드 토큰은 공용 정의서(`_CARD_TASK_CLI`) 단일 진실에서 온다(손 하드코딩 0).
    """
    ti = f" --task {task_name}"
    lines: list[str] = []
    lines.append(
        "### 이 세션 커맨드 카드 (task 모드·정체성 채움·--help 불요·단일 진실)"
    )
    lines.append(
        f"정체성: task=`{task_name}` 단일 축 — 보유 슬롯은 장부에서 자동 수령·log/pm_state 는 task 앵커. 보드/리스 조작은 `--task {task_name}` 명시(정체성=에이전트 맥락·도구엔 명시 전달)."
    )
    lines.append(
        "> wave 운영은 스킬로 invoke·backbone 직접호출 금지 → pm_role §스킬 우선 운영 규율"
    )
    lines.append("")
    lines.append("# 내 작업 보기 (read-only 조회·직접 — 자기 공간 우선)")
    lines.append(
        cmd(
            _C_BOARD_LIST,
            "이 task 스코프(내 open + 이 task 명의 claim)·task 작업 조회·조회 전용",
            suffix=ti,
        )
    )
    lines.append(
        cmd(
            _C_BOARD_LIST_MINE,
            "내 것 전 슬롯·전 task(user-wide·직교 렌즈)·기본 user 조회",
        )
    )
    lines.append("")
    lines.append("# task 작업공간 (task 명의 슬롯 대여/반납)")
    lines.append(
        cmd(
            _C_PC_ALLOC,
            "idle 최소번호 슬롯 대여(자동 생성 안 함·풀 소진 시 add 요청)",
            suffix=ti,
        )
    )
    lines.append(
        cmd(_C_PC_RELEASE, "작업완료 반납(소유검사 이 task 명의 슬롯만)", suffix=ti)
    )
    lines.append(
        "  ⚠ 코드를 고치는 티켓은 cluster new 전에 같은 저장소의 작업 폴더 2개를 빌려 둔다: 통합 폴더(base_branch 를 계속 든다)와 작업 폴더(묶음 브랜치로 switch 해 고친다). 그 저장소 폴더가 1개뿐이면 그 저장소로 alloc 을 한 번 더 한다(풀이 비면 alloc 이 사용자 승인 명령을 알려 준다)."
    )
    lines.append("")
    lines.append("# task 정체성 (prefix·종료)")
    lines.append(
        cmd(
            _C_PC_TASK_PREFIX,
            "ticket prefix 지정/변경/해제(`none`=해제·중간 변경 자유)",
        )
    )
    lines.append(
        cmd(
            _C_PC_TASK_END,
            "task 종료 — 일괄 idle 반납 + 서술 폴더 _ended 아카이브(worktree 미삭제)",
        )
    )
    lines.append(
        cmd(
            _C_PC_TASK_REOPEN,
            "종료 archive를 같은 task identity로 복원(bootstrap 별도 재진입)",
        )
    )
    lines.append(
        "  ⚠ task end 는 claimed 소진 + 전 슬롯 clean 전제 — 미완 claim/dirty 있으면 거부."
    )
    lines.append("")
    lines.append("# 티켓 발행 (직접 CLI — 래핑 스킬 없음)")
    lines.append(
        cmd(
            _C_BOARD_NEW,
            "새 티켓 발행(prefix 는 이 task 것) · task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가",
            suffix=ti,
        )
    )
    lines.append("")
    lines.append("# wave 운영 (스킬로 invoke — backbone CLI 엔진은 직접 금지)")
    lines.append(
        skill(
            "/pm-wave-claim T-NNNN",
            "묶음 선언 + ticket claim + 작업 폴더 전환 · DoD 자족 검증",
        )
    )
    lines.append(engine(_C_BOARD_CLUSTER_NEW, suffix=ti))
    lines.append(
        "  ⚠ <ticket-group> 은 새로 짓는 묶음 이름이다(영숫자·`.`·`_`·`-`, 예: `login-fix`). 티켓 ID 모양(`T-NNNN`)은 발행이 만든 크기 1 장부(`C-T-NNNN`)의 자리라 거부되고, 그 장부는 새 묶음이 흡수한다."
    )
    lines.append(
        "  ⚠ cluster new 는 지목한 폴더의 현재 브랜치를 base_branch(머지 대상)로 적고 묶음 브랜치 task/<ticket-group> 을 만든다: task 의 폴더가 1개면 그 폴더(`--task` 만), 2개 이상이면 `--repo <repo> --slot <N>` 으로 그 저장소의 통합 폴더를 지목한다."
    )
    lines.append(
        cmd(
            _C_BOARD_PROMOTE,
            "본문 채운 뒤·cluster new 뒤 승격 · draft 는 open 으로 옮기고 이미 open 이면 그대로 둔다",
        )
    )
    lines.append(
        cmd(
            _C_BOARD_TIER,
            "promote 뒤 · 리뷰 없이 종결할 티켓 표시(open/claimed · touches 2개 이하 · 코드 변경엔 테스트 동반)",
        )
    )
    lines.append(engine(_C_BOARD_SHOW))
    lines.append(
        engine(_C_BOARD_LINT, "스킬이 부르는 내부 엔진·직접 금지 · 차단 항목만 rc 1")
    )
    lines.append(
        engine(
            _C_BOARD_CLAIM,
            "스킬이 부르는 내부 엔진·직접 금지 · task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가",
            suffix=ti,
        )
    )
    lines.append("  ⚠ claim 은 draft 티켓을 거부한다 — 위 promote 를 먼저 실행한다.")
    lines.append(
        engine(
            _C_WP_SWITCH,
            f"{_runtime_skill_entry('pm-wave-claim')} 6단계 · 코드 변경이 없어도 실행한다 · claim 뒤, 파일 수정·종결 전 · 작업 폴더(<slot>=work/<repo>_<N>)를 묶음 브랜치로 전환(같은 저장소의 폴더 1개면 그 폴더, 2개면 cluster new 에 지목하지 않은 폴더)",
        )
    )
    lines.append(
        "  ⚠ 파일은 switch 뒤 그 폴더에서 고친다. switch 전에 고치면 switch 가 거부하고, switch 전에 커밋한 변경은 묶음 브랜치에 없어 종결이 rc 0 이어도 머지되지 않는다."
    )
    lines.append(skill("/pm-regression", "비차단 백그라운드 회귀 pre-warm + 완료 알림"))
    lines.append(
        engine(
            _C_BOARD_REGRESSION,
            "스킬이 부르는 내부 엔진·직접 금지 · task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가",
            suffix=ti,
        )
    )
    lines.append(
        skill("/pm-wave-finish T-NNNN", "ticket 완료 기록 — 회귀+log+board+stage")
    )
    lines.append(
        engine(
            _C_TICKET_FINISH,
            "스킬이 부르는 내부 엔진·직접 금지 · switch 뒤에 실행(switch 전이면 작업 트리 해소에서 rc 1) · 내부서 board.py complete 수행",
            suffix=ti,
        )
    )
    lines.append(
        "  ⚠ 종결 선검사: 완료 조건(DoD) 체크박스 전부 `- [x]` · 바뀐 파일은 티켓 `touches` 에 선언 · 돌려받은 리뷰 라운드 또는 tier pm-direct."
    )
    lines.append(
        skill(
            "/pm-handoff",
            f"task 세션 종료 7단계 자동화 — 무인자(스킬은 인자를 안 받음 · PM 이 --task {task_name} 을 채워 backbone 호출)",
        )
    )
    lines.append(engine(_C_PM_HANDOFF_TASK, prefix=f"--task {task_name}"))
    lines.append(
        cmd(
            _C_ADDITIONAL_REVIEWER,
            "추가 리뷰어 교차검증 게이트 — 직접(래핑 스킬 없음)·reviewer 병행",
        )
    )
    lines.append("")
    lines.append("# 정체성 불요 (read-only 조회·직접 — cwd/conf/env 자동 해소)")
    lines.append(cmd(_C_PM_LOG_TAIL))
    lines.append("")
    lines.extend(_card_navigation_lines(f"`(task:{task_name})`", tool_invoke))
    return "\n".join(lines)
