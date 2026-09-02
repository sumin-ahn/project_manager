"""커맨드 카드 사용성 라이브 테스트 (release tier · ADR-0046 · ADR-0039 refine · 기본 skip).

기계 e2e(T-0112 계보)는 커맨드가 *존재·rc0* 임은 봐도 "실 LLM 이 부트스트랩 카드(ADR-0045)만
보고 **첫 시도**에 맞는 커맨드를 치는가"(사용성)는 못 본다. 카드의 소비자가 LLM 이므로 그 판정은
LLM 이 load-bearing 이다(사용자 지시: "진짜로 얘네들이 커맨드를 잘 쓰는지"). 이 파일은 그 갭을
릴리즈 라이브 tier 에서 메운다 — fresh import 홈에 **부트스트랩 커맨드 카드만** 컨텍스트로 주고
실 LLM 2하네스(claude + opencode/glm-5.2:cloud)에 표준 티켓 라이프사이클(new→promote→claim→
complete)을 시켜 **각 조작의 첫 시도 커맨드가 그대로 rc0** & 출력에 `--help`/`-h` 호출·재시도
흔적 0 임을 단언한다.

판정 철학(release_wave·runtime_smoke 상속):
- **카드 커맨드가 실제로 *실행*됐는지가 1급 판정**: 기대 lifecycle op {new, promote, claim, complete}가
  **실제 실행된 argv 에서 각 정확히 1회** 관측돼야 통과. side-effect(done/)만으론 부족하다 — LLM 이 카드
  커맨드를 건너뛰고 파일을 직접 옮겨 done/ 을 만들어도 통과시키면 "카드를 첫 시도에 제대로 쓰는가"
  게이트가 false-green 이 된다(이 테스트의 존재 이유가 무너짐). 그래서 op 관측을 side-effect 위에 얹는다.
  나아가 `echo "…board.py claim…"` 같은 **비실행 문자열**(에코·리터럴)을 op 로 오집계하면 그 봉함의
  우회로가 되므로, `_executed_board_ops` 가 셸 파싱으로 *실행 argv 만* 센다(에코/printf/mv 배제·MF-2).
- **첫 시도 *rc0* 를 직접 확증**: claude 는 stream-json 의 tool_result(`is_error`)를 tool_use 와 상관해
  기대 op 각각의 **첫 실행이 rc0**(is_error=False)였는지 단언한다(`_judge_first_try_rc0`·MF-1). 정확-1회 +
  재시도 0 이어도 첫 실행이 rc≠0(실패 후 파일 수동 보정으로 done/ 위조)이면 "첫 시도 rc0" 주장이 거짓
  이므로, side-effect 나 실행-횟수만으로는 부족하고 rc 를 봐야 한다. tool_result 는 콜 *전체*의 rc 하나뿐
  이라, lifecycle op 콜은 **단일-op·rc 마스킹 연산자(`||`/`;`/`|`/`&`) 0** 이어야 그 rc 가 곧 op rc 다
  (`claim … || true` 처럼 실패를 은폐하면 red·`&&` 프리픽스만 허용·R5 구조 규칙으로 shell-파싱 견고화 종결).
- **관측 수단 비대칭(release_wave 위임-관측 비대칭 상속·§test_release_wave.py:170-219)**:
  · **claude = hard** — stream-json 의 Bash tool-call·tool_result 를 파싱해 *실행 커맨드·결과*를 본다.
    각 op 정확-1회·--help(shlex 토큰)·**첫 실행 rc0(is_error)**·side-effect(done)를 전부 hard 단언.
  · **opencode = best-effort** — stream-json 처럼 tool-call/result 를 정밀 노출하지 않아(stdout 스캔뿐)
    정확-1회·rc0 를 **hard-claim 할 수 없다**. side-effect(done) + stdout `--help` 호출 스캔(카드-에코
    무해한 `.py`+help 패턴)이 hard 상한이고, op 실행 관측은 stdout 에코가 있을 때만 best-effort 다. 이
    한계는 판정 로직 주석·실패 메시지·테스트명(`..._opencode_best_effort`)에 명시한다.
- **flake**: fail 시 그 시나리오 단건 1회 재실행 재판정(PM 51 livegate 방식) — 재현 fail 은 red 유지.

게이트 아님 — 사용자가 릴리즈 직전 `PM_ORCH_LIVE_RELEASE=1` 로 occasional 트리거(비용·flaky 감수).
기본 skip(env 미설정·CI green 불변). 기계층(카드 dump 존재·카드↔CLI drift 가드·T-0250)과 상보 —
이 게이트는 **사용성**(실 LLM 첫 시도)만 본다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

# release_wave/runtime_smoke 헬퍼 재사용(중복 인프라 금지·같은 tests/ 디렉토리 import) —
# adopter import(hermetic·models 조회 차단)·LLM env 격리(화이트리스트)·ticket 상태 조회.
from test_fresh_adopter_runtime_smoke import _import_adopter, _live_env, _tickets_in
# release 마커 AST 카운트(자기 파일 pin 가드용) — 마커 소실/개명 방어 도구 재사용.
from test_release_wave import _count_marked_tests

REPO = Path(__file__).resolve().parents[1]

# 릴리즈 트리거 — 사용자가 릴리즈 직전 명시 set(occasional). 미설정이면 전부 skip(CI green 불변).
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# claude: sonnet-4-6(API 과금·env override). opencode: glm-5.2:cloud(ollama cloud·2026-07-07 채택·
# T-0258 이 교체·과금 0·env override). release_wave 와 동일 단일 진실(양쪽 파일 default 일치).
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
# opencode 는 느리고 변동 커 1800s, claude 는 lifecycle 4콜 여유분 600s (release_wave 상속).
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", "600"))

# 표준 조작 시퀀스가 발행하는 probe ticket 의 제목·prefix. prefix 는 `_validate_prefix`
# (ADR-0042·[a-z0-9_]+·소문자)를 통과해야 하므로 소문자 'probe'(대문자면 rc1·multirepo 'repoa'
# 동형). fresh 솔로 adopter 는 등록 repo ≤1 이라 명시 prefix 를 그대로 존중(T-probe-NNN 발행).
PROBE_TITLE = "card usability probe"
PROBE_PREFIX = "probe"

# complete sync-gate(`_complete_gate`)가 읽는 로그 경로 — fresh adopter 실측: board.py `LOG_FILE`
# = `<홈>/.project_manager/wiki/log/current.md`(홈=LLM cwd 기준 이 상대경로가 유일한 current.md).
# 프롬프트가 이 정확한 경로를 지시해야 LLM 의 log append 를 게이트가 읽어 complete 가 첫 시도 rc0
# 된다 — `wiki/log/current.md`(짧은 경로)로 지시하면 게이트가 못 읽어 complete 가 sync-gate 에서
# spurious RED(R4 MF). 단일 진실 상수로 두고 프롬프트↔drift 가드 테스트가 함께 참조한다.
_ADOPTER_LOG_PATH = ".project_manager/wiki/log/current.md"

# 기대 lifecycle 시퀀스(카드 "티켓 조작") — 라이브 프롬프트가 시키는 조작. 판정은 이 4개가 실행
# 커맨드에서 **각 정확히 1회** 관측됐는지 본다: 0회=카드 커맨드 건너뜀(누락·false-green 원천)·
# 2회+=첫 시도 실패 후 재시도. read 조작(list/show/regression/prefix list 등)은 반복 무해·판정 제외.
# (핸드오프는 이 시퀀스에서 제외 — `_usability_prompt` docstring 근거.)
# **완료 축은 묶음 종결(`ticket_finish.py`)이다** — 발행이 모든 티켓을 크기 1 묶음에 귀속시켜
# direct `board.py complete` 는 결속 없이 거부되고 카드도 그 줄을 싣지 않는다. 카드가 처방하지
# 않는 명령을 기대 op 로 남기면 이 게이트는 LLM 에게 없는 커맨드를 요구해 라이브에서만 터진다
# (평시 skip 이라 회귀는 green — 이 파일이 막으려는 false-green 클래스 그대로다).
_EXPECTED_LIFECYCLE_OPS = ("new", "promote", "claim", "finish")

# mutating 조작 분류 집합(핸드오프 포함) — `_board_operation` 이 read/write 구분에 참조(classifier).
# 카드 시퀀스가 낼 수 있는 어휘만 담는다 — `complete` 는 카드가 처방하지 않으므로 여기 없다.
_SEQUENCE_MUTATING_OPS = frozenset({"new", "promote", "claim", "finish", "handoff"})

# --help/-h 토큰 매칭(--tests-pass·--session-seq 등 오탐 방지). 경계를 `[\w-]` 로 잡아 셸 구두점
# (`;`·따옴표)이 인접해도(`--help;`·`"--help"`) 매칭되게 한다 — opencode stdout 라인 스캔
# (`_help_invocation_lines`)용. claude per-커맨드 판정은 아래 `_command_has_help_flag` 가 shlex 토큰으로.
_HELP_FLAG_RE = re.compile(r"(?<![\w-])(?:--help|-h)(?![\w-])")


# ── 부트스트랩 카드 렌더 (LLM 아님·순수 엔진 재현) ──────────────────────────────
def _home_slot_identity(home: Path) -> dict | None:
    """home 의 lease 장부 행 → 카드 정체성 dict (행이 없으면 None).

    fresh 채택자는 import 가 홈 자신을 첫 슬롯 행으로 등록하므로, 라이브 프롬프트가 받는 카드도
    **그 행의 실값**으로 렌더돼야 실제 채택자가 보는 카드와 같다(정체성을 지어내지 않는다).
    """
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    if not ledger.is_file():
        return None
    rows = json.loads(ledger.read_text(encoding="utf-8")).get("leases") or []
    leased = [row for row in rows if row.get("state") == "leased" and row.get("session")]
    if len(leased) != 1:
        return None
    row = leased[0]
    session = row["session"]
    return {
        "session": session,
        "repo": row.get("repo"),
        "slot": row.get("slot"),
        "slot_number": session.rsplit("_", 1)[-1],
        "slot_path": str(home),
    }


def _render_command_card(home: Path) -> str:
    """home 의 pm_bootstrap 엔진으로 이 홈의 커맨드 카드를 렌더한다 (LLM 아님·순수 엔진).

    부트스트랩이 정체성 채워 dump 하는 커맨드 카드(ADR-0045)를 라이브 LLM 에 *유일한 컨텍스트*로
    주기 위해 코드로 재현한다. 정체성은 장부 행에서 오고(행이 없으면 미해소 형태로 분기)
    `_build_command_card_markdown` 은 순수 함수(identity·모듈 상수 `_CARD_TOOL_INVOKE` 만 사용·
    인스턴스 상태 무의존)라 부작용 0 — 그래서 기본 생성자로 인스턴스화해 호출한다.
    """
    path = home / ".project_manager" / "tools" / "pm_bootstrap.py"
    # 유니크 모듈명 — 워크트리 자신의 pm_bootstrap 과 sys.modules 충돌 방지(경로별 격리).
    spec = importlib.util.spec_from_file_location(f"_card_bootstrap_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PmBootstrap()._build_command_card_markdown(_home_slot_identity(home))


def _usability_prompt(card: str) -> str:
    """실 LLM 에 카드만 주고 표준 라이프사이클을 시키는 프롬프트 (카드 = 유일 컨텍스트).

    진입문서(CLAUDE.md/AGENTS.md) 경로를 *주지 않는다* — 카드만으로 커맨드/플래그를 골라야
    통과(= 카드 사용성). complete sync-gate(log entry 요구·`_complete_gate`)는 카드가 명시하지
    않으므로 release_wave 의 검증된 gate-satisfaction 문구를 미러해 housekeeping(로그 한 줄)만
    안내한다 — *board 커맨드 자체*(new/promote/claim/complete)는 카드에서 그대로 옮겨야 한다.
    로그 경로는 게이트가 읽는 정확한 `_ADOPTER_LOG_PATH`(fresh adopter 실측)로 지시한다 — 짧은
    `wiki/log/current.md` 로 지시하면 게이트가 못 읽어 완료 기록이 spurious RED(R4 MF).
    완료 단계는 **카드의 wave-finish 진입**을 시킨다 — 카드 lifecycle 절에 direct complete 줄이
    없고(발행이 만든 크기 1 묶음의 멤버라 결속 없는 direct complete 는 rc=1), 그 진입이 부르는
    엔진이 `ticket_finish.py` 다. 회귀 게이트 만족(`--no-pytest`)도 sync-gate 로그와 같은 축의
    housekeeping 이라 프롬프트가 명시한다 — 채택자 fresh 트리에는 test 스위트가 없다.
    promote 전에 **티켓 본문 채우기** 단계를 명시해 claim=promote 선행 트랩(카드 4대장 ①·본문
    채운 뒤 promote)을 시퀀스에서 실제로 밟게 한다(fresh import 형상에서 이 축 보강·R4 suggestion 2).
    핸드오프는 이 시퀀스에서 제외한다: 세션 종료 op 는 무거운 side-effect(pm_state write·log
    skeleton)라 첫 시도 판정을 흐리고, 핵심 카드 사용성 트랩(claim=promote 선행·complete 플래그)은
    new→(fill)→promote→claim→complete 로 커버된다(spike §4.3 "등/필요시" 재량·핸드오프 라이브
    커버리지는 기존 release full-wave/hard-stop 계보가 보유).
    """
    return (
        "You are the PM for this project. Below is your bootstrap command card — it lists every "
        "command with identity already filled in. Use ONLY this card to decide which command and "
        "flags to run for each step. Do NOT run any command with --help or -h, and do NOT open "
        "other documentation.\n\n"
        "Run this standard ticket lifecycle, one card command per step, using the EXACT commands "
        "and flags shown on the card:\n"
        "  1. List your tickets.\n"
        f'  2. Create a new ticket titled "{PROBE_TITLE}" using the prefix "{PROBE_PREFIX}".\n'
        "  3. Fill in the new ticket's body: the board tool created the ticket file from a template "
        "with placeholder sections — replace them with a brief real description so the ticket is "
        "ready to promote (per the card's warning, claim rejects unfilled/draft tickets, so the "
        "body must be filled and the ticket promoted before you can claim it).\n"
        "  4. Promote it.\n"
        "  5. Claim it.\n"
        "  6. Finish the ticket through the card's wave-finish entry (the ticket lifecycle section "
        "has no direct complete command; that entry's engine command is the one to run for this "
        "ticket id). Two facts the card does not state: the completion sync gate requires a log "
        f"entry, so first append a one-line entry mentioning the new ticket id to "
        f"{_ADOPTER_LOG_PATH}; and this project ships no test suite, so pass --no-pytest.\n\n"
        "Command card:\n"
        "<<<CARD\n"
        f"{card}\n"
        "CARD>>>\n\n"
        "Use the EXACT ticket id printed by the create step in the later steps. Reply with the "
        "ticket id when it is complete."
    )


# ── 관측 헬퍼 (claude stream-json 파싱 · 커맨드 판정) ──────────────────────────
def _collect_bash_commands(stdout: str) -> list[str]:
    """claude stream-json stdout 의 각 라인을 json 파싱 → 재귀 walk 로 Bash tool_use 커맨드 수집.

    `_collect_subagent_types`(release_wave)와 동형 walk — claude 는 Bash 도구 호출을 `{"type":
    "tool_use","name":"Bash","input":{"command": "..."}}` 로 낸다. 형식 변동에 강건하게 *어느
    깊이든* 그 노드를 긁어 `input.command` 를 순서대로 모은다. 파싱 불가 라인(비-json·빈 줄)은
    무시. 이 커맨드 리스트가 첫 시도/재시도/--help 판정의 단일 입력이다.
    """
    commands: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name") == "Bash":
                inp = node.get("input")
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    commands.append(inp["command"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(obj)
    return commands


# 실행 argv 파싱 상수 — python 인터프리터·env 할당(KEY=VAL)·셸 연산자(statement 경계).
_PY_EXECUTABLES = frozenset({"python", "python3", "py"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_SHELL_PUNCT = set("();<>|&;")  # shlex punctuation_chars — statement 를 가르는 연산자 문자.
_OP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# 스크립트 자신이 곧 조작인 도구 — `board.py <op>` 와 달리 서브커맨드가 없다(카드의 강등 엔진 줄).
# `ticket_finish.py` = /pm-wave-finish 의 엔진이고 그것이 카드의 유일한 완료 진입이다.
_SCRIPT_OPS = {"pm_handoff.py": "handoff", "ticket_finish.py": "finish"}

def _op_command(inv: str, op: str, tid: str = "T-1") -> str:
    """그 op 를 실행하는 커맨드 1줄 — 도구·서브커맨드 형태가 op 마다 다르다(board.py <op> vs 스크립트)."""
    if op == "new":
        return f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}'
    if op == "finish":
        return f"{inv}/ticket_finish.py {tid} --no-pytest"
    return f"{inv}/board.py {op} {tid}"


# rc 마스킹(디커플링) 연산자 — 한 Bash 콜의 tool_result rc 를 그 안 lifecycle op 의 rc 와 분리시키는
# 셸 제어흐름. `||`(실패 은폐)·`;`(rc=마지막 statement)·`|`(rc=파이프 마지막)·`&`(백그라운드→즉시 rc0).
# `&&` 는 실패를 *전파*하므로(앞 op 실패 시 뒤가 안 돌고 전체 실패) 마스킹 아님 — 허용. 이 연산자가
# lifecycle op 콜에 있으면 tool_result rc 가 op rc 를 못 증명한다 → red (R5 구조 규칙·클래스 종결).
_RC_MASKING_OPERATORS = frozenset({"||", ";", "|", "&"})


def _shell_tokens(command: str) -> list[str]:
    """command 를 셸 문법으로 토큰화 (shlex·연산자 분리·따옴표 해제) — 실패 시 빈 리스트.

    `--executed_board_ops`/`_command_has_help_flag` 공용. posix 모드로 따옴표를 벗기고
    (`"--help"`→`--help`·`'-h'`→`-h`), punctuation_chars 로 `;`·`|`·`&` 등을 별도 토큰으로 가른다
    (`--help;true`→`--help`,`;`,`true`). 파싱 불가(따옴표 불균형)면 빈 리스트(안전).
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="".join(_SHELL_PUNCT))
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def _shell_statements(command: str) -> list[list[str]]:
    """command 를 statement(연산자로 끊긴 실행 단위) 목록으로 — 파싱 불가면 빈 목록.

    `_executed_board_ops`(실행 op 추출)와 `_commands_leaving_the_sandbox`(이탈 판정)가 같은
    분할을 쓴다 — 규칙이 둘로 갈리면 한쪽이 보는 커맨드를 다른 쪽이 못 본다.
    """
    statements: list[list[str]] = []
    current: list[str] = []
    for tok in _shell_tokens(command):
        if tok and set(tok) <= _SHELL_PUNCT:
            if current:
                statements.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        statements.append(current)
    return statements


# 커맨드가 **어디에 작용하는지** 지시하는 옵션 — 이탈 판정의 대상 자리(값은 다음 토큰).
_DIRECTORY_OPTIONS = frozenset({"--cwd", "--dir", "-C"})


def _directory_targets(command: str) -> list[str]:
    """그 커맨드가 작용 대상으로 **지목한 디렉터리** 절대경로 목록.

    보는 자리는 넷이다 — `cd <경로>` · `git -C <경로>` · `--cwd/--dir <경로>`(붙여 쓴 `=` 형태
    포함) · 절대경로로 실행한 프로그램. 상대경로는 실행 cwd(격리 홈) 기준이라 여기서 세지
    않는다. 커맨드 안의 임의 절대경로(예: 읽기 인자)를 전부 세지 않는 이유는 같다 — 판정하려는
    것은 "어디에서/어디에 대고 도는가" 이지 문자열에 무엇이 들어 있나가 아니다.
    """
    targets: list[str] = []
    for statement in _shell_statements(command):
        index = 0
        while index < len(statement) and _ENV_ASSIGN_RE.match(statement[index]):
            index += 1
        words = statement[index:]
        if not words:
            continue
        if words[0].startswith("/"):
            targets.append(words[0])
        for position, token in enumerate(words):
            if token == "cd" or token in _DIRECTORY_OPTIONS:
                if position + 1 < len(words):
                    targets.append(words[position + 1])
            elif "=" in token and token.split("=", 1)[0] in _DIRECTORY_OPTIONS:
                targets.append(token.split("=", 1)[1])
    return [target for target in targets if target.startswith("/")]


def _commands_leaving_the_sandbox(commands: Sequence[str], sandbox: Path) -> list[str]:
    """샌드박스(테스트 `tmp_path`) 밖 디렉터리를 지목한 커맨드만 골라낸다.

    라이브 격리 홈은 바깥 저장소 트리 **안**에 만들어진다(임시 루트 규약·T-0890). 그래서 홈
    경로의 접두를 프로젝트 루트로 오독하면 모델이 바깥 PM 홈에 대고 backbone 을 돌린다
    (2026-09-02 livegate #2 실측). 경계는 격리 홈이 아니라 **샌드박스**다 — 홈의 형제인
    readonly 좌표도 정당한 홈 밖 절대경로라, 홈으로 경계를 잡으면 그 정상 커맨드가 오탐이 된다.
    """
    root = Path(sandbox).resolve()
    escaped: list[str] = []
    for command in commands:
        for target in _directory_targets(command):
            resolved = Path(target).resolve()
            if resolved != root and root not in resolved.parents:
                escaped.append(command)
                break
    return escaped


def _command_has_help_flag(command: str) -> bool:
    """커맨드에 `--help`/`-h` 가 **셸 토큰**으로 있는가 (MF-2).

    raw regex 는 따옴표/체인(`"--help"`·`--help; true`·`'-h'`)을 놓친다 — shlex 토큰화 후 정확히
    `--help`/`-h` 토큰이 있는지 본다(`_executed_board_ops` 와 동형 파싱). `--tests-pass`·
    `--session-seq` 등은 토큰이 달라 오탐 0.
    """
    return any(tok in ("--help", "-h") for tok in _shell_tokens(command))


def _executed_board_ops(command: str) -> list[str]:
    """**실제 실행된** board/handoff 조작명만 추출 — echo/따옴표 리터럴(비실행)은 배제 (MF-2).

    raw substring 스캔(`board.py <op>`)은 `echo "python3 …/board.py claim …"` 같은 **비실행
    문자열**(카드 에코·주석·프롬프트 인용)도 실행 op 로 오인해 false-green 을 낳는다(MF-1 봉함의
    우회로). 그래서 셸 파서(shlex·연산자 분리)로 command 를 *statement* 단위로 끊고, 각 statement 의
    명령어(command word)가 python 인터프리터(또는 스크립트 자신)일 때만 그 뒤 `board.py <op>` 를
    실 실행으로 카운트한다 — 명령어가 echo/printf/cat/mv 등이면(에코·리터럴) 배제된다. chained
    (`&&`/`;`/`|`)·env prefix(`KEY=VAL`)·리다이렉션(`>>`)·`cd X && …` 도 정확히 처리한다.
    """
    statements = _shell_statements(command)  # 파싱 불가면 [] → 비실행 취급(false-positive 0).

    ops: list[str] = []
    # `board.py <op>` 는 서브커맨드가 op 이고, `_SCRIPT_OPS` 도구는 스크립트 자신이 op 다.
    for stmt in statements:
        idx = 0
        while idx < len(stmt) and _ENV_ASSIGN_RE.match(stmt[idx]):  # 선행 KEY=VAL 스킵.
            idx += 1
        if idx >= len(stmt):
            continue
        words = stmt[idx:]
        cmd_base = words[0].rsplit("/", 1)[-1]
        if cmd_base in _PY_EXECUTABLES:
            args = words[1:]           # `python3 …/board.py <op>` — 스크립트는 인자.
        elif cmd_base == "board.py" or cmd_base in _SCRIPT_OPS:
            args = words               # 스크립트 직접 실행(chmod+x) — 명령어 자신이 스크립트.
        else:
            continue                   # echo/printf/cat/mv/… (비실행) — 배제.
        for pos, tok in enumerate(args):
            tok_base = tok.rsplit("/", 1)[-1]
            if tok_base == "board.py":
                if pos + 1 < len(args) and _OP_NAME_RE.match(args[pos + 1]):
                    ops.append(args[pos + 1])
                break
            if tok_base in _SCRIPT_OPS:
                ops.append(_SCRIPT_OPS[tok_base])
                break
    return ops


def _board_op_mentions(text: str) -> list[str]:
    """text 에 board/handoff 조작이 *언급*된 것을 loose 스캔 (opencode best-effort 전용·비-gating).

    opencode stdout 은 clean argv 가 아니라 에이전트 혼합 출력이라 실행/비실행 구분이 불가하다 —
    이 scan 은 판정을 gate 하지 않고(opencode 는 side-effect + --help 만 hard) 통과 사유에 관측을
    실어 투명화하는 용도다(에코여도 무해). 실행 여부 hard 판정은 claude `_executed_board_ops` 몫.
    """
    ops = re.findall(r"board\.py\s+([a-z][a-z0-9-]*)", text)
    ops.extend(op for script, op in _SCRIPT_OPS.items() if script in text)
    return ops


def _board_operation(command: str) -> str | None:
    """커맨드 → 첫 실행 lifecycle 조작명 ('new'/…/'handoff'/read op/None) — classifier 편의용.

    조작 카운팅(정확-1회 판정)은 `_executed_board_ops` 전수 집계로 한다(에코 배제).
    """
    ops = _executed_board_ops(command)
    return ops[0] if ops else None


def _help_invocation_lines(text: str) -> list[str]:
    """stdout 에서 `--help`/`-h` *호출* 라인을 스캔 (카드-에코 무해·opencode 판정용).

    `.py` 도구 호출과 help 플래그가 *같은 라인*에 있어야 호출로 본다. 카드 텍스트는 `.py` 줄에
    `--help`/`-h` 를 담지 않으므로(header 줄만 "--help 불요" — `.py` 없음), opencode 가 카드를
    에코해도 오탐 0 이고 실제 `board.py ... --help` 호출만 잡힌다.
    """
    hits: list[str] = []
    for line in text.splitlines():
        if ".py" in line and _HELP_FLAG_RE.search(line):
            hits.append(line.strip())
    return hits


def _judge_commands(commands: list[str]) -> tuple[bool, str]:
    """실행 커맨드 리스트로 사용성 판정 (claude hard 경로) — 반환 (통과여부, 사유).

    2중 단언(커맨드 표면):
      1. `--help`/`-h` 호출 0 (카드만으론 부족해 도움말 왕복 = fail).
      2. 기대 lifecycle op {new, promote, claim, complete} 가 **각 정확히 1회** 실행 관측. 0회=카드 커맨드
         건너뜀(누락)·2회+=재시도. side-effect(done/)만 보면 board 커맨드를 안 쓰고 파일만 옮겨도 통과하는
         false-green 을 이 정확-1회가 막는다.
    **첫 시도 *rc0* 은 여기서 안 본다** — 정확-1회 + 재시도 0 이어도 각 op 의 첫 실행이 rc≠0(실패 후 수동
    보정)일 수 있다. rc0 확증은 `_judge_first_try_rc0`(tool_result is_error)가 `_judge_claude` 에서 이어 친다.
    """
    help_cmds = [c for c in commands if _command_has_help_flag(c)]
    if help_cmds:
        return False, f"--help/-h 호출 관측(카드만 보고 첫 시도 실패 신호): {help_cmds}"
    # **실제 실행된** board/handoff 조작만 전수 집계(에코/리터럴 배제·chained 포함) → 정확-1회 검사.
    op_counts: Counter[str] = Counter()
    for command in commands:
        op_counts.update(_executed_board_ops(command))
    problems: list[str] = []
    for op in _EXPECTED_LIFECYCLE_OPS:
        count = op_counts.get(op, 0)
        if count == 0:
            problems.append(f"{op} 미실행(카드 커맨드 건너뜀 — side-effect 만으론 통과 불가)")
        elif count > 1:
            problems.append(f"{op} 재시도 ×{count}(첫 시도 실패 신호)")
    if problems:
        return False, (
            f"lifecycle 조작 판정 실패({'; '.join(problems)}).\n"
            f"관측 op 카운트={dict(op_counts)}\n전체 커맨드={commands}"
        )
    return True, (
        f"clean(기대 op {list(_EXPECTED_LIFECYCLE_OPS)} 각 1회·--help/재시도 0) — 커맨드={commands}"
    )


def _collect_bash_tool_calls(stdout: str) -> list[dict]:
    """claude stream-json 에서 Bash tool_use 를 {id, command} 로 순서대로 수집 (rc0 상관용).

    `_collect_bash_commands` 는 command 만 뽑지만, rc0 판정은 각 호출의 `id` 로 tool_result 를
    상관해야 하므로 id 를 함께 보존한다. walk 는 동형(어느 깊이든 tool_use[name=Bash] 노드).
    """
    calls: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name") == "Bash":
                inp = node.get("input")
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    calls.append({"id": node.get("id"), "command": inp["command"]})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(obj)
    return calls


def _collect_tool_results(stdout: str) -> dict[str, bool]:
    """claude stream-json 에서 tool_result 를 {tool_use_id: is_error(bool)} 로 수집 (rc 판정 근거).

    claude Bash 도구는 rc≠0 실행을 tool_result `is_error=true` 로 표면화한다 — 이 플래그로 각 op 의
    첫 실행 성공(rc0) 여부를 판정한다. is_error 키가 없으면 성공(False)으로 간주(일부 포맷은 성공 시
    생략). tool_result 자체가 없으면(id 미수집) 호출부가 'rc0 미확증'으로 처리한다.
    """
    results: dict[str, bool] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_result":
                tid = node.get("tool_use_id")
                if isinstance(tid, str):
                    results[tid] = bool(node.get("is_error"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(obj)
    return results


def _rc_masking_problem(command: str) -> str | None:
    """lifecycle op 콜이 **rc 마스킹 불가한 단일-op 형태**인지 검사 — 위반 사유 or None (R5 구조 규칙).

    tool_result 는 command *전체*의 rc 하나뿐이라, 한 콜에 lifecycle op 이 여럿이거나 rc 를 디커플링하는
    연산자(`||`·`;`·`|`·`&`)가 있으면 그 rc 가 op 의 첫 시도 성공을 증명하지 못한다(`claim … || true` 는
    claim 이 rc≠0 여도 tool_result rc0). 그래서 lifecycle op 담긴 콜은 (1) lifecycle op 정확히 1개 (2)
    마스킹 연산자 0 이어야 한다 — 그러면 tool_result rc 가 곧 그 단일 op 의 rc(마스킹 불가). `&&` 프리픽스
    (`cd X && python3 …/board.py claim …`)는 실패를 전파하므로 허용. lifecycle op 없는 콜은 규칙 대상 아님.
    """
    lifecycle = [op for op in _executed_board_ops(command) if op in _EXPECTED_LIFECYCLE_OPS]
    if not lifecycle:
        return None  # lifecycle op 없음(로그 append 등) — 규칙 대상 아님.
    if len(lifecycle) > 1:
        return (
            f"한 Bash 콜에 lifecycle op {lifecycle} 다수 — 공유 tool_result 로 각 op 첫 시도 rc 개별 "
            f"확증 불가(op 1개/콜 규칙)."
        )
    masking = [tok for tok in _shell_tokens(command) if tok in _RC_MASKING_OPERATORS]
    if masking:
        return (
            f"lifecycle op 콜에 rc 디커플링 연산자 {masking}(비-&& 체인/실패 은폐) — tool_result rc 가 "
            f"op '{lifecycle[0]}' 의 rc 를 마스킹(첫 시도 rc0 증명 불가)."
        )
    return None


def _judge_first_try_rc0(tool_calls: list[dict], results: dict[str, bool]) -> tuple[bool, str]:
    """기대 lifecycle op 각각의 **첫 실행이 rc0**(tool_result is_error=False)였는지 단언 (MF-1 + R5).

    정확-1회·재시도 0 이어도 각 op 첫 실행이 rc≠0 이면 "첫 시도 rc0" 주장이 거짓이다(실패 후 파일 수동
    보정으로 done/ 위조 가능). op 별로 그 op 를 실행한 첫 tool_call 을 찾아: (a) **구조 규칙**(단일-op·rc
    마스킹 연산자 0·`_rc_masking_problem`)을 위반하면 rc 를 신뢰할 수 없으므로 red — `claim … || true`
    같은 마스킹 false-green 을 원천 차단(R5·클래스 종결). (b) 통과하면 tool_result 가 is_error 면 fail·
    미관측이면 'rc0 미확증' fail. (미실행 op 는 `_judge_commands` 가 이미 red.)
    """
    problems: list[str] = []
    for op in _EXPECTED_LIFECYCLE_OPS:
        first_call = next(
            (call for call in tool_calls if op in _executed_board_ops(call["command"])), None
        )
        if first_call is None:
            continue  # 미실행 — _judge_commands 소관.
        # (a) 구조 규칙 — 이 op 콜이 rc 마스킹 불가한 단일-op 형태여야 tool_result rc 가 곧 op rc.
        masking = _rc_masking_problem(first_call["command"])
        if masking:
            problems.append(f"{op}: {masking}")
            continue
        # (b) rc 확증 — 마스킹 없으니 tool_result rc 를 그대로 신뢰.
        tid = first_call.get("id")
        if tid not in results:
            problems.append(f"{op} 첫 실행 tool_result 미관측(rc0 미확증)")
        elif results[tid]:
            problems.append(f"{op} 첫 실행 rc≠0(is_error·실패 후 수동 보정 의심)")
    if problems:
        return False, "첫 시도 rc0 판정 실패(" + "; ".join(problems) + ")"
    return True, "첫 시도 rc0 확증(기대 op 각 첫 실행 단일-op·마스킹 0·is_error=False)"


def _proc_tail(proc: subprocess.CompletedProcess, harness: str) -> str:
    return (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )


def _judge_claude(dest: Path, proc: subprocess.CompletedProcess) -> tuple[bool, str]:
    """claude 판정(hard) — 커맨드 표면(--help/정확-1회) + **첫 시도 rc0**(tool_result) + side-effect(done).

    3게이트: (1) `_judge_commands` — --help 0·기대 op 정확-1회(누락/재시도 red). (2) `_judge_first_try_rc0`
    — 기대 op 각 첫 실행이 단일-op·rc 마스킹 0(`|| true`·`;` 등 red·R5)·rc0(is_error=False)인지(실패 후
    수동 보정 red·MF-1). (3) ticket done/ 도달.
    """
    tail = _proc_tail(proc, "claude")
    commands = _collect_bash_commands(proc.stdout)
    ok_cmd, detail_cmd = _judge_commands(commands)
    if not ok_cmd:
        return False, f"{detail_cmd}\n{tail}"
    # (2) 첫 시도 rc0 — tool_call↔tool_result 상관으로 각 op 첫 실행 성공 확증.
    tool_calls = _collect_bash_tool_calls(proc.stdout)
    results = _collect_tool_results(proc.stdout)
    ok_rc, detail_rc = _judge_first_try_rc0(tool_calls, results)
    if not ok_rc:
        return False, f"{detail_rc}\n{detail_cmd}\n{tail}"
    done = _tickets_in(dest, "done")
    if not done:
        return False, (
            f"ticket 이 done/ 에 미도달 — 첫 시도 lifecycle 미완주.\n"
            f"open={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n"
            f"{detail_cmd}\n{tail}"
        )
    return True, f"{detail_cmd} · {detail_rc} · done={done}"


def _judge_opencode(dest: Path, proc: subprocess.CompletedProcess) -> tuple[bool, str]:
    """opencode 판정 (best-effort 경로) — side-effect(ticket done) hard + stdout --help 스캔 hard.

    **한계(claude 대비 비대칭)**: opencode 는 stream-json 처럼 tool-call 을 정밀 노출하지 않아(stdout
    스캔뿐) 기대 op {new, promote, claim, complete}의 **정확-1회를 hard-claim 할 수 없다** — LLM 이
    board 커맨드를 안 쓰고 파일만 옮겨 done/ 을 만든 false-green 을 claude 만큼 못 막는다(release_wave
    위임-관측 비대칭 상속). 그래서 이 경로의 hard 상한 = side-effect(done) + --help 호출 스캔(카드-에코
    무해한 `.py`+help 패턴)이고, op 실행 관측은 stdout 에 커맨드가 에코될 때만 **best-effort** 다.
    """
    tail = _proc_tail(proc, "opencode")
    help_lines = _help_invocation_lines(proc.stdout)
    if help_lines:
        return False, f"opencode 가 --help/-h 를 호출(카드만 보고 첫 시도 실패 신호): {help_lines}\n{tail}"
    done = _tickets_in(dest, "done")
    if not done:
        return False, (
            f"ticket 이 done/ 에 미도달 — 첫 시도 lifecycle 미완주.\n"
            f"open={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n{tail}"
        )
    # best-effort: opencode stdout 에 board 커맨드가 언급되면 기대 op 를 loose 로 관측(gate 아님).
    # opencode 는 정확-1회·실행여부를 hard-claim 못 하므로(위 한계) 관측 실패여도 fail 시키지 않는다 —
    # 관측된 op 를 통과 사유에 실어 투명하게 남긴다(에코 포함 가능·비-gating·side-effect 로 판정).
    observed_ops = sorted(set(_board_op_mentions(proc.stdout)) & set(_EXPECTED_LIFECYCLE_OPS))
    return True, (
        f"clean(--help 호출 0·done={done}) · op 실행 관측(best-effort·hard 아님)={observed_ops}"
    )


def _attempt_with_single_rerun(run_attempt) -> tuple[bool, str]:
    """flake 흡수 — 1차 판정 pass 면 즉시 반환, fail 이면 단건 1회 재실행 재판정(PM 51 livegate 방식).

    `run_attempt(i) -> (passed, detail)` — i 는 시도 인덱스(0=최초·1=재실행)로, 재실행은 *fresh
    홈*에서 돌려 이전 시도의 side-effect(done ticket)가 재판정을 오염(false-green)하지 않게 한다.
    2차도 fail 이면 재현 fail 로 보고 red 유지(비결정 flake 만 흡수·진짜 결함은 통과 안 시킴).
    """
    passed, detail = run_attempt(0)
    if passed:
        return True, detail
    passed2, detail2 = run_attempt(1)
    if passed2:
        return True, f"1차 fail·단건 재실행 pass(flake 흡수):\n{detail2}"
    return False, f"1차 fail·단건 재실행도 재현 fail(red 유지):\n[1차]\n{detail}\n[2차]\n{detail2}"


def _import_attempt(tmp_path: Path, harness: str, attempt: int) -> Path:
    """시도별 fresh adopter — 재실행이 이전 시도 state 를 재사용하지 않게 유니크 디렉토리."""
    attempt_dir = tmp_path / f"{harness}-attempt{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    return _import_adopter(attempt_dir, harness)


def _run_live_or_timeout(argv: list[str], *, cwd: str, env: dict, timeout: int, harness: str):
    """LLM subprocess 실행 — TimeoutExpired 를 `(None, detail)` 로 변환해 재실행 경로에 태운다 (suggestion).

    `subprocess.run(timeout=)` 의 TimeoutExpired 는 예외라, 그냥 두면 `_attempt_with_single_rerun`
    의 단건 재실행을 못 타고 테스트가 즉시 error 난다. timeout 도 flake 흡수 대상(LLM 지연은 종종
    일시적)이므로 detail fail 로 바꿔 재실행 경로에 넣는다 — 2차도 timeout 이면 red(재현).
    반환 (proc, None) 정상 / (None, timeout_detail) timeout.
    """
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout,
        )
        return proc, None
    except subprocess.TimeoutExpired as exc:
        return None, f"{harness} 라이브 timeout({exc.timeout}s) — flake 로 보고 단건 재실행 대상."


# ── 라이브 테스트 (release tier · 기본 skip · PM_ORCH_LIVE_RELEASE=1 opt-in) ──────────────
@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="command-card usability — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). "
           "기본 skip·사용자 트리거.",
)
def test_command_card_usability_claude(tmp_path):
    """실 claude 가 부트스트랩 카드만 보고 표준 lifecycle 을 첫 시도에 rc0 로 운영한다 (--help/재시도 0).

    카드(ADR-0045)만 컨텍스트로 주고 new→promote→claim→complete 를 시킨다 — 진입문서 경로 미제공.
    stream-json 으로 실행 커맨드·결과를 파싱해 --help/-h 호출·조작 재시도 0·**각 op 첫 실행 rc0**
    (tool_result is_error)를 hard 단언하고, side-effect(ticket done)로 완주를 확증한다. flake·timeout 은
    단건 1회 재실행 흡수. claude 는 subprocess cwd 를 존중(`--dir` 불요). API 과금.
    """
    def run_attempt(attempt: int) -> tuple[bool, str]:
        dest = _import_attempt(tmp_path, "claude", attempt)
        prompt = _usability_prompt(_render_command_card(dest))
        proc, timeout_detail = _run_live_or_timeout(
            ["claude", "-p", "--model", CLAUDE_MODEL,
             "--allowedTools", "Bash",
             "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions",
             prompt],
            cwd=str(dest), env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT, harness="claude",
        )
        if proc is None:
            return False, timeout_detail
        return _judge_claude(dest, proc)

    passed, detail = _attempt_with_single_rerun(run_attempt)
    assert passed, detail


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="command-card usability — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·사용자 트리거.",
)
def test_command_card_usability_opencode_best_effort(tmp_path):
    """실 opencode(glm-5.2:cloud)가 카드만 보고 표준 lifecycle 을 운영한다 (best-effort — claude 대비 비대칭).

    claude 와 같은 카드-only 프롬프트지만 **판정 강도가 낮다** — 테스트명 `_best_effort` 가 그 한계를
    표기한다. opencode 는 stream-json 처럼 tool-call 을 정밀 노출하지 않아(stdout 스캔뿐) 기대 op 각
    정확-1회를 hard-claim 할 수 없다(`_judge_opencode` docstring). 그래서 hard 상한 = side-effect(done)
    + --help 호출 스캔이고, op 실행 관측은 stdout 에코가 있을 때만 best-effort 다. claude 경로
    (`test_command_card_usability_claude`)가 정확-1회를 hard 로 커버하는 짝이다. `--dir` 로 루트 핀
    (opencode 는 PWD 로 루트 오판). `--dangerously-skip-permissions`: 비대화 헤드리스라 --dir 를 external
    로 보고 auto-reject 하지 않게(throwaway tmp adopter 격리·release_wave 동일 근거). API 과금 0(ollama).
    """
    def run_attempt(attempt: int) -> tuple[bool, str]:
        dest = _import_attempt(tmp_path, "opencode", attempt)
        prompt = _usability_prompt(_render_command_card(dest))
        proc, timeout_detail = _run_live_or_timeout(
            ["opencode", "run", "--agent", "build", "--dir", str(dest),
             "--dangerously-skip-permissions", "-m", LIVE_MODEL,
             prompt],
            cwd=str(dest), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT, harness="opencode",
        )
        if proc is None:
            return False, timeout_detail
        return _judge_opencode(dest, proc)

    passed, detail = _attempt_with_single_rerun(run_attempt)
    assert passed, detail


# ── hermetic 단위 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과) ──────────────
# 위 라이브 2케이스는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는 라이브
# 없이도 돌아 (1) 카드 렌더가 lifecycle 커맨드를 담는지 (2) 프롬프트가 카드+시퀀스를 담는지 (3)
# 커맨드 파싱/판정 로직(추출·--help·재시도·카드-에코 무해 스캔) (4) flake 단건 재실행 로직을
# 결정적으로 가드한다 — 라이브 미실행 시에도 mechanics 구조를 회귀가 잡는다.


def test_render_command_card_has_lifecycle_commands():
    """카드 렌더가 lifecycle 커맨드(new/promote/claim + 묶음 종결)+핸드오프를 담고 --session 미포함.

    완료 축은 `board.py complete` 가 아니라 묶음 종결이다 — 발행이 모든 티켓을 크기 1 묶음에
    귀속시키므로 direct complete 는 결속 없이 거부되고, 카드는 그 줄을 싣지 않는다(X-001).
    실행 줄만 본다: ticket_finish 강등 줄의 "내부서 board.py complete 수행" 주석이 토큰 검사를
    대신 충족하면 이 가드가 공허해진다.
    """
    card = _render_command_card(REPO)
    command_parts = "\n".join(ln.split("#", 1)[0] for ln in card.splitlines())
    for token in (
        "board.py new", "board.py promote", "board.py claim",
        "ticket_finish.py", "pm_handoff.py",
    ):
        assert token in command_parts, f"카드 실행 줄에 '{token}' 부재"
    assert "board.py complete" not in command_parts, \
        "카드가 direct board complete 를 실행 줄로 되살렸다(항상 rc=1·X-001)"
    # 정체성 헤더는 실값 또는 미해소 명시 — 폐지된 `--session` placeholder 는 어느 쪽에도 없다.
    assert "정체성:" in card and "--session <session>" not in card


def test_usability_prompt_embeds_card_and_sequence():
    """프롬프트가 카드 전문 + 표준 lifecycle 시퀀스(create/fill/promote/claim/finish)를 담고 --help 를 금한다."""
    card = _render_command_card(REPO)
    prompt = _usability_prompt(card)
    # 카드가 유일 컨텍스트로 임베드된다(진입문서 경로 미제공).
    assert card in prompt
    assert "CLAUDE.md" not in prompt and "AGENTS.md" not in prompt
    # 표준 조작 시퀀스 + probe 식별자.
    assert PROBE_TITLE in prompt and PROBE_PREFIX in prompt
    for step in ("Create a new ticket", "Fill in the new ticket", "Promote it", "Claim it",
                 "Finish the ticket"):
        assert step in prompt, f"프롬프트에 '{step}' 단계 부재"
    # 지시부(카드 임베드 앞)는 카드가 처방하지 않는 direct complete 를 시키지 않는다.
    instructions = prompt.split("Command card:", 1)[0]
    assert "board.py complete" not in instructions and "--tests-pass" not in instructions, \
        "프롬프트가 카드에 없는 direct complete 를 시킨다(라이브에서만 red 나는 false-green)"
    # 회귀 게이트 만족(`--no-pytest`)은 sync-gate 로그와 같은 축의 housekeeping 으로 명시된다.
    assert "--no-pytest" in instructions
    # --help 사용 금지가 명시된다(사용성 판정 대상).
    assert "Do NOT run any command with --help or -h" in prompt


def test_expected_lifecycle_ops_are_all_prescribed_by_the_card():
    """기대 op 는 **카드가 실제로 처방하는 커맨드**에서만 온다 (X-001 낙수 가드).

    카드에 없는 명령을 기대 op 로 남기면 라이브 게이트는 LLM 에게 존재하지 않는 커맨드를 요구해
    반드시 red 가 되는데, 이 파일의 라이브 2건은 평시 skip 이라 회귀는 green 이다 — 정확히 이
    파일이 막으려는 false-green 클래스다. 그래서 카드 실행 줄에서 op 를 추출해 기대 집합과
    대조한다(판정기·프롬프트·카드 3자 정합).
    """
    card = _render_command_card(REPO)
    prescribed: set[str] = set()
    for line in card.splitlines():
        prescribed.update(_executed_board_ops(line.split("#", 1)[0]))
    assert prescribed, "카드에서 실행 op 를 하나도 못 뽑았다(추출기 가정 붕괴)"
    missing = [op for op in _EXPECTED_LIFECYCLE_OPS if op not in prescribed]
    assert not missing, f"카드가 처방하지 않는 기대 op: {missing} (카드 처방={sorted(prescribed)})"
    assert "complete" not in prescribed, \
        "카드가 direct complete 를 되살렸다 — 크기 1 묶음 귀속으로 항상 rc=1 이다"


def test_usability_prompt_uses_gate_log_path():
    """프롬프트의 로그 append 경로가 complete 게이트가 읽는 `.project_manager/wiki/log/current.md` 와 일치.

    R4 MF drift 가드 — 짧은 `wiki/log/current.md` 로 어긋나면 LLM 의 로그가 게이트에 안 읽혀 complete 가
    spurious RED 난다. 정확 경로를 durable 하게 고정(다음 갱신자가 다시 어긋내지 않게)·짧은 경로
    단독 지시가 없는지도 확인.
    """
    prompt = _usability_prompt(_render_command_card(REPO))
    assert _ADOPTER_LOG_PATH == ".project_manager/wiki/log/current.md"  # 게이트 실측 경로(단일 진실).
    assert _ADOPTER_LOG_PATH in prompt, "프롬프트가 게이트 로그 경로를 안 지시(complete spurious RED 위험)"
    # 짧은 경로 단독(`.project_manager` 접두 없는 " wiki/log/current.md")로 지시하지 않는다.
    assert " wiki/log/current.md" not in prompt


def test_collect_bash_commands_extracts_from_stream_json():
    """Bash walk 가 claude stream-json 형 샘플에서 커맨드를 순서대로 정확히 추출한다."""
    inv = "python3 .project_manager/tools"
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": f"{inv}/board.py promote T-probe-001"}}]}}),
        "",  # 빈 줄 — 무시.
        "not json at all",  # 비-json — 무시.
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": f"{inv}/board.py claim T-probe-001"}}]}}),
    ]
    commands = _collect_bash_commands("\n".join(lines))
    assert commands == [
        f"{inv}/board.py promote T-probe-001",
        f"{inv}/board.py claim T-probe-001",
    ]


def test_collect_bash_commands_ignores_non_bash_tools():
    """Bash 아닌 도구(Read 등)·위임 없는 stdout 은 빈 리스트(false-positive 0)."""
    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}}),
        json.dumps({"type": "result", "subtype": "success"}),
    ])
    assert _collect_bash_commands(stdout) == []


def test_command_has_help_flag_detects_shlex_tokens():
    """--help/-h 는 **셸 토큰**으로 탐지 — 따옴표/체인(`"--help"`·`--help; true`·`'-h'`)도 잡고 오탐 0 (MF-2)."""
    # 평문.
    assert _command_has_help_flag("python3 board.py claim T-1 --help")
    assert _command_has_help_flag("python3 board.py claim T-1 -h")
    # 따옴표/체인 — raw regex 는 놓치던 실제 shell help 호출(shlex 토큰화로 잡힘).
    assert _command_has_help_flag("python3 board.py claim T-1 --help; true")
    assert _command_has_help_flag('python3 board.py claim T-1 "--help"')
    assert _command_has_help_flag("python3 board.py claim T-1 '-h'")
    assert _command_has_help_flag("python3 board.py claim --help && echo x")
    # 오탐 0 — 유사 플래그·부분 문자열은 help 토큰이 아니다.
    assert not _command_has_help_flag("python3 board.py complete T-1 --tests-pass")
    assert not _command_has_help_flag("python3 ticket_finish.py T-1 --no-pytest")
    assert not _command_has_help_flag("python3 board.py list --mine")
    assert not _command_has_help_flag("python3 pm_handoff.py --session-seq 1 --wave-summary x")
    assert not _command_has_help_flag("python3 board.py new --help-me")  # --help 아님(경계).


def test_commands_leaving_the_sandbox_are_detected(tmp_path):
    """샌드박스 밖으로 나가는 커맨드만 잡는다 — 홈의 형제(readonly 좌표)는 오탐이 아니다.

    라이브 격리 홈은 바깥 PM 홈 트리 안에 만들어지므로(임시 루트 규약), 모델이 홈 경로의 접두를
    프로젝트 루트로 오독하면 바깥 장부에 대고 backbone 을 돈다(2026-09-02 livegate #2 의 그 한 줄이
    아래 음성 사례다). 경계는 홈이 아니라 샌드박스라, 홈 밖이지만 샌드박스 안인 readonly 좌표를
    위반으로 잡으면 정상 커맨드가 red 가 된다.
    """
    home = tmp_path / "adopter-claude"
    readonly = tmp_path / "adopter-claude-readonly"
    outside = tmp_path.parent / "outer-pm-home"
    record = ("PM_ORCH_LIVE_RELEASE=1 python3 .project_manager/tools/board.py livegate record "
              f"--repo adopter-claude --cwd {readonly}")
    clean = [
        "python3 .project_manager/tools/board.py list",       # 홈 안 상대 실행
        record,                                               # 형제 readonly 절대경로 --cwd
        f"cd {home} && {record}",                             # 격리 홈 절대경로로 cd
    ]
    escaping = f"cd {outside} && {record}"

    assert _commands_leaving_the_sandbox(clean, tmp_path) == []
    assert _commands_leaving_the_sandbox(clean + [escaping], tmp_path) == [escaping]
    assert _commands_leaving_the_sandbox(
        [f"git -C {outside} status"], tmp_path) == [f"git -C {outside} status"]


def test_board_operation_classifies_lifecycle_and_handoff():
    """커맨드 → 조작명 분류 (lifecycle·핸드오프·read op·비-도구)."""
    inv = "python3 .project_manager/tools"
    assert _board_operation(f'{inv}/board.py new "x" --prefix probe') == "new"
    assert _board_operation(f"{inv}/board.py promote T-1") == "promote"
    assert _board_operation(f"{inv}/board.py claim T-1") == "claim"
    assert _board_operation(f"{inv}/ticket_finish.py T-1 --no-pytest") == "finish"
    assert _board_operation(f"{inv}/pm_handoff.py --session-seq 1 --wave-summary x") == "handoff"
    # direct complete 는 여전히 파싱되지만 **카드가 처방하지 않으므로** 시퀀스 어휘가 아니다.
    assert _board_operation(f"{inv}/board.py complete T-1 --tests-pass") == "complete"
    assert "complete" not in _SEQUENCE_MUTATING_OPS
    # read op 는 분류되나 mutating 이 아니라 재시도 판정 대상 아님.
    assert _board_operation(f"{inv}/board.py list --mine") == "list"
    assert _board_operation(f"{inv}/board.py list --mine") not in _SEQUENCE_MUTATING_OPS
    assert _board_operation("echo hi") is None


def test_judge_commands_passes_clean_first_try_sequence():
    """카드대로 첫 시도 rc0(--help/재시도 0) 시퀀스는 통과."""
    inv = "python3 .project_manager/tools"
    commands = [
        f"{inv}/board.py list --mine",
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}',
        f"{inv}/board.py promote T-probe-001",
        f"{inv}/board.py claim T-probe-001",
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest",
    ]
    ok, detail = _judge_commands(commands)
    assert ok, detail


def test_judge_commands_flags_help_invocation():
    """--help 호출이 있으면 fail(사용성 실패 신호)."""
    commands = [
        "python3 board.py claim T-1 --help",  # 카드만으론 부족해 도움말 요청 = fail.
        "python3 board.py claim T-1",
    ]
    ok, detail = _judge_commands(commands)
    assert not ok and "--help" in detail


def test_judge_commands_flags_retried_operation():
    """어떤 op 가 2회+ 실행되면(첫 시도 실패 후 재시도) fail — claim ×2 케이스."""
    inv = "python3 .project_manager/tools"
    commands = [
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}',
        f"{inv}/board.py promote T-probe-001",
        f"{inv}/board.py claim T-probe-001",    # 1차 claim.
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest",
        f"{inv}/board.py claim T-probe-001",    # 재시도 → claim ×2.
    ]
    ok, detail = _judge_commands(commands)
    assert not ok and "claim 재시도" in detail


def test_judge_commands_flags_skipped_lifecycle_op():
    """기대 op(claim)를 건너뛰면 fail — done side-effect 만으론 통과 불가(MF-1 sensitivity).

    카드 커맨드를 다 안 쓰고 일부만 실행(여기선 claim 누락)한 채 파일 직접 조작으로 done/ 을
    만들어도, 판정은 op 누락으로 red 여야 한다(false-green 봉쇄의 핵심 sensitivity).
    """
    inv = "python3 .project_manager/tools"
    commands = [
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}',
        f"{inv}/board.py promote T-probe-001",
        # claim 누락 — 카드 커맨드를 건너뜀.
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest",
    ]
    ok, detail = _judge_commands(commands)
    assert not ok and "claim 미실행" in detail


def test_judge_commands_flags_no_board_commands_used():
    """board 커맨드를 아예 안 쓰고 파일만 직접 조작(mv 등)하면 fail — 게이트 무력화 방어(MF-1).

    카드 사용성 게이트의 존재 이유 = "실 LLM 이 *카드 커맨드*를 첫 시도에 제대로 쓰는가". board
    커맨드 0 개면(예: `mv open/T.md done/T.md` 로 side-effect 만 위조) 반드시 red — 기대 op 4개 전부
    미실행으로 잡힌다.
    """
    commands = [
        "mv .project_manager/wiki/tickets/open/T-probe-001.md "
        ".project_manager/wiki/tickets/done/T-probe-001.md",
        "echo done",
    ]
    ok, detail = _judge_commands(commands)
    assert not ok
    for op in _EXPECTED_LIFECYCLE_OPS:
        assert f"{op} 미실행" in detail, f"'{op} 미실행' 사유 누락"


def test_judge_commands_counts_chained_board_ops():
    """한 Bash 콜에 board 조작을 체이닝(`new && promote && claim && complete`)해도 각각 정확-1회로 센다."""
    inv = "python3 .project_manager/tools"
    chained = (
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX} && '
        f"{inv}/board.py promote T-probe-001 && "
        f"{inv}/board.py claim T-probe-001 && "
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest"
    )
    ok, detail = _judge_commands([chained])
    assert ok, detail


def test_executed_board_ops_excludes_echoed_strings():
    """echo/printf/따옴표 리터럴(비실행)은 op 로 안 잡히고, 실 python3 실행만 잡힌다 (MF-2 sensitivity).

    현 raw-substring 방식이면 에코 문자열의 `board.py claim` 도 실행 op 로 오인(=버그) — 셸 파서로
    실행 argv 만 세야 에코가 배제된다. 에코 vs 실행 구분의 핵심 재현.
    """
    inv = "python3 .project_manager/tools"
    # 비실행 — 따옴표 안이든(quoted) 밖이든(unquoted) echo/printf 인자의 board.py 는 실행이 아니다.
    assert _executed_board_ops(f'echo "{inv}/board.py claim T-1"') == []
    assert _executed_board_ops(f"echo {inv}/board.py claim T-1") == []
    assert _executed_board_ops(f'printf "%s" "{inv}/board.py new x"') == []
    # 실 실행 — python 인터프리터가 board.py 를 돌린다(--session 인자 포함).
    assert _executed_board_ops(f"{inv}/board.py claim T-probe-001 --session probe_1") == ["claim"]
    # env prefix + 리다이렉션 + chained 실 실행(`cd`/log append 는 op 아님).
    assert _executed_board_ops(
        f'echo "log line" >> wiki/log/current.md && {inv}/ticket_finish.py T-1 --no-pytest'
    ) == ["finish"]
    assert _executed_board_ops(
        f'PM_SESSION_NAME=x {inv}/board.py new "t" --prefix probe ; {inv}/board.py promote T-1'
    ) == ["new", "promote"]


def test_judge_commands_flags_echoed_only_ops():
    """카드 커맨드를 실행 안 하고 echo 로 출력만 하면 op 집계가 안 차 fail — MF-2 false-green 봉쇄.

    substring 방식이면 echo 문자열의 board.py 를 실행 op 로 오인해 통과(=MF-1 봉함의 우회로) — 셸
    파싱으로 실행 argv 만 세므로 4 op 전부 미실행 red 여야 한다(에코 false-green sensitivity).
    """
    inv = "python3 .project_manager/tools"
    commands = [
        f'echo "{inv}/board.py new x --prefix probe"',
        f'echo "{inv}/board.py promote T-1"',
        f'echo "{inv}/board.py claim T-1"',
        f'echo "{inv}/ticket_finish.py T-1 --no-pytest"',
    ]
    ok, detail = _judge_commands(commands)
    assert not ok
    for op in _EXPECTED_LIFECYCLE_OPS:
        assert f"{op} 미실행" in detail, f"에코-only 인데 '{op}' 가 실행으로 오집계됨(MF-2 false-green)"


def _fake_stream_json(entries: list) -> str:
    """커맨드 목록 → claude stream-json 형 stdout — judge sensitivity 용.

    entries 원소는 `command`(str·rc0 성공 기본) 또는 `(command, is_error)` 튜플. 각 커맨드에
    tool_use(고유 id) 라인 + tool_result(같은 tool_use_id·is_error) 라인을 생성해 rc0 판정
    (`_judge_first_try_rc0`)까지 재현한다.
    """
    lines: list[str] = []
    for i, entry in enumerate(entries):
        command, is_error = entry if isinstance(entry, tuple) else (entry, False)
        tid = f"toolu_{i:04d}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "…"}]}}))
    return "\n".join(lines)


def test_judge_claude_rejects_done_side_effect_when_op_skipped(tmp_path):
    """done/ side-effect 가 실재해도 claim 커맨드가 없으면 _judge_claude fail — MF-1 false-green 봉쇄 증명.

    done ticket 을 파일로 직접 심어(=LLM 이 board 안 쓰고 옮긴 상황) side-effect 를 만족시킨 뒤,
    stream-json 에는 claim 을 뺀 3개 op 만 준다. _judge_claude 는 side-effect 만 보면 통과하겠지만,
    op 정확-1회 단언이 앞서 claim 미실행으로 red 를 세운다(side-effect 위조로 못 뚫는다).
    """
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")
    assert _tickets_in(tmp_path, "done"), "사전조건: done side-effect 가 심어져야(테스트 자체 정합)"

    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}',
        f"{inv}/board.py promote T-probe-001",
        # claim 누락 — 카드 커맨드 건너뜀(파일 직접 이동으로 done/ 위조).
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest",
    ])
    proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    ok, detail = _judge_claude(tmp_path, proc)
    assert not ok and "claim 미실행" in detail


def test_judge_claude_rejects_done_side_effect_when_ops_only_echoed(tmp_path):
    """done/ side-effect 가 실재하고 4 op 를 전부 echo 로 '출력'만 해도 _judge_claude fail — MF-2 봉쇄 증명.

    LLM 이 카드 커맨드를 실행하지 않고 stream-json Bash 로 `echo "…board.py claim…"` 만 하고 파일
    직접 조작으로 done/ 을 만든 상황. substring 방식이면 에코가 op 로 오집계돼 통과(=MF-2 버그) —
    셸 파싱 실행-argv 판정이 4 op 전부 미실행으로 red 를 세운다(에코로 못 뚫는다).
    """
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")

    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        f'echo "{inv}/board.py new x --prefix probe"',
        f'echo "{inv}/board.py promote T-probe-001"',
        f'echo "{inv}/board.py claim T-probe-001"',
        f'echo "{inv}/ticket_finish.py T-probe-001 --no-pytest"',
    ])
    proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    ok, detail = _judge_claude(tmp_path, proc)
    assert not ok and "claim 미실행" in detail


def test_judge_claude_rejects_first_try_failures_with_manual_done(tmp_path):
    """각 op 를 첫 실행에서 rc≠0(is_error)로 실패시키고 done/ 을 수동 생성해도 _judge_claude fail — MF-1 rc0.

    op 정확-1회 + 재시도 0 이어도(각 op 딱 한 번 실행) 그 첫 실행이 전부 실패(is_error=True)면 "첫
    시도 rc0" 주장이 거짓 — LLM 이 실패 후 파일 직접 조작으로 done/ 을 만든 상황. 커맨드 표면
    (_judge_commands)만 보면 통과하겠지만(정확-1회·--help 0), rc0 게이트가 red 를 세운다(실패-후-수동
    보정 sensitivity — 현 코드가 rc 를 안 보면 이게 통과 = 버그).
    """
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")

    inv = "python3 .project_manager/tools"
    # 각 op 딱 한 번 실행하되 tool_result is_error=True(rc≠0) — 첫 실행 실패.
    stdout = _fake_stream_json([
        (f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}', True),
        (f"{inv}/board.py promote T-probe-001", True),
        (f"{inv}/board.py claim T-probe-001", True),
        (f"{inv}/ticket_finish.py T-probe-001 --no-pytest", True),
    ])
    proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    ok, detail = _judge_claude(tmp_path, proc)
    assert not ok and "rc≠0" in detail


def test_judge_first_try_rc0_flags_missing_tool_result(tmp_path):
    """tool_result 가 없어 rc0 를 관측 못 하면(id 미상관) 'rc0 미확증' fail — 확증 없는 통과 차단."""
    inv = "python3 .project_manager/tools"
    # tool_use 만 있고 tool_result 라인 없음 → results 비어 rc0 미확증.
    calls = _collect_bash_tool_calls("\n".join(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash",
             "input": {"command": _op_command(inv, op)}}]}})
        for i, op in enumerate(_EXPECTED_LIFECYCLE_OPS)
    ))
    ok, detail = _judge_first_try_rc0(calls, {})  # results 비어있음.
    assert not ok and "미확증" in detail


def test_rc_masking_problem_structural_rule():
    """R5 구조 규칙 단위 — 마스킹 연산자/다중 op = 위반, 단일 op(&& 프리픽스 포함) = OK."""
    inv = "python3 .project_manager/tools"
    # 위반 — rc 디커플링 연산자(마스킹 클래스 전 vector).
    assert _rc_masking_problem(f"{inv}/board.py claim T-1 || true") is not None       # 실패 은폐.
    assert _rc_masking_problem(f"{inv}/board.py claim T-1; echo done") is not None     # 세미콜론.
    assert _rc_masking_problem(f"{inv}/board.py claim T-1 | tee out") is not None       # 파이프.
    assert _rc_masking_problem(f"{inv}/board.py claim T-1 &") is not None               # 백그라운드.
    # 위반 — 한 콜에 lifecycle op 다수(공유 rc 로 개별 확증 불가).
    assert _rc_masking_problem(f"{inv}/board.py new x --prefix probe && {inv}/board.py promote T-1") is not None
    # OK — 단일 op·&& 프리픽스(cd)는 실패 전파라 허용.
    assert _rc_masking_problem(f"cd home && {inv}/board.py claim T-1 --session x") is None
    assert _rc_masking_problem(f"{inv}/board.py claim T-1 --session probe_1") is None
    # lifecycle op 없는 콜(로그 append)은 규칙 대상 아님 — 리다이렉션·연산자 있어도 None.
    assert _rc_masking_problem("echo 'entry T-1' >> .project_manager/wiki/log/current.md") is None


def test_judge_first_try_rc0_rejects_rc_masking_even_when_result_success(tmp_path):
    """`claim … || true`(claim 실패해도 tool_result rc0)는 판정 red — R5 마스킹 false-green 봉쇄.

    구 코드(마스킹 무시)면 is_error=False 라 rc0 통과(=버그) — 구조 규칙이 마스킹 연산자를 잡아 red.
    """
    inv = "python3 .project_manager/tools"
    # 4 op 모두 단일 실행하되 claim 만 `|| true` 로 실패 마스킹(tool_result 는 전부 rc0 성공으로 기록).
    stdout = _fake_stream_json([
        (f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}', False),
        (f"{inv}/board.py promote T-probe-001", False),
        (f"{inv}/board.py claim T-probe-001 || true", False),   # 마스킹 — rc0 로 기록되나 신뢰 불가.
        (f"{inv}/ticket_finish.py T-probe-001 --no-pytest", False),
    ])
    calls = _collect_bash_tool_calls(stdout)
    results = _collect_tool_results(stdout)
    ok, detail = _judge_first_try_rc0(calls, results)
    assert not ok and "마스킹" in detail and "claim" in detail


def test_judge_first_try_rc0_rejects_semicolon_chain(tmp_path):
    """`claim …; echo done`(세미콜론 체인·rc=마지막)도 red — 마스킹 클래스 종결(per-vector 아님)."""
    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        (f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}', False),
        (f"{inv}/board.py promote T-probe-001", False),
        (f"{inv}/board.py claim T-probe-001; echo done", False),  # 세미콜론 — rc=echo(마지막).
        (f"{inv}/ticket_finish.py T-probe-001 --no-pytest", False),
    ])
    calls = _collect_bash_tool_calls(stdout)
    results = _collect_tool_results(stdout)
    ok, detail = _judge_first_try_rc0(calls, results)
    assert not ok and "claim" in detail


def test_judge_first_try_rc0_allows_and_prefix_single_op(tmp_path):
    """`cd home && python3 …/board.py claim … --session x`(단일 op·&& 프리픽스)는 rc0 확증 통과.

    && 는 실패를 전파하므로(cd 실패면 claim 안 돎·claim 실패면 전체 rc≠0) tool_result rc 가 곧 claim rc —
    마스킹 아님. 정상 운영 형태를 오탐하지 않음(non-vacuous·허용 경로 확인).
    """
    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        (f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}', False),
        (f"cd home && {inv}/board.py promote T-probe-001", False),
        (f"cd home && {inv}/board.py claim T-probe-001 --session probe_1", False),
        (f"cd home && {inv}/ticket_finish.py T-probe-001 --no-pytest", False),
    ])
    calls = _collect_bash_tool_calls(stdout)
    results = _collect_tool_results(stdout)
    ok, detail = _judge_first_try_rc0(calls, results)
    assert ok, detail


def test_judge_claude_passes_full_op_sequence_with_done(tmp_path):
    """기대 op 4개 정확-1회 + 첫 실행 rc0(is_error=False) + done side-effect 면 통과(정상 경로·non-vacuous)."""
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")

    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        f'{inv}/board.py new "{PROBE_TITLE}" --prefix {PROBE_PREFIX}',
        f"{inv}/board.py promote T-probe-001",
        f"{inv}/board.py claim T-probe-001",
        f"{inv}/ticket_finish.py T-probe-001 --no-pytest",
    ])
    proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    ok, detail = _judge_claude(tmp_path, proc)
    assert ok, detail


def test_help_invocation_scan_is_card_safe():
    """카드를 에코해도 --help 호출 오탐 0(header 의 '--help 불요'는 `.py` 줄 아님)·실제 호출은 탐지."""
    card = _render_command_card(REPO)
    assert _help_invocation_lines(card) == [], "카드 에코가 --help 호출로 오탐됨(카드-에코 무해 위반)"
    assert _help_invocation_lines("python3 .project_manager/tools/board.py claim T-1 --help")
    assert _help_invocation_lines("python3 .project_manager/tools/board.py claim T-1 -h")


def test_attempt_with_single_rerun_passes_on_first_attempt():
    """1차 pass 면 재실행하지 않는다(단일 시도)."""
    calls: list[int] = []

    def run(attempt: int) -> tuple[bool, str]:
        calls.append(attempt)
        return True, "ok"

    passed, _ = _attempt_with_single_rerun(run)
    assert passed and calls == [0]


def test_attempt_with_single_rerun_recovers_on_single_rerun():
    """1차 fail·2차 pass 면 flake 흡수로 통과(단건 1회 재실행)."""
    results = {0: (False, "1차 fail"), 1: (True, "2차 ok")}
    calls: list[int] = []

    def run(attempt: int) -> tuple[bool, str]:
        calls.append(attempt)
        return results[attempt]

    passed, detail = _attempt_with_single_rerun(run)
    assert passed and calls == [0, 1] and "flake 흡수" in detail


def test_attempt_with_single_rerun_stays_red_on_reproduced_fail():
    """1차·2차 모두 fail 이면 재현 fail 로 red 유지(진짜 결함은 통과 안 시킴)."""
    calls: list[int] = []

    def run(attempt: int) -> tuple[bool, str]:
        calls.append(attempt)
        return False, f"fail{attempt}"

    passed, detail = _attempt_with_single_rerun(run)
    assert not passed and calls == [0, 1] and "재현 fail" in detail


def test_run_live_or_timeout_converts_timeout_to_rerunnable_fail(monkeypatch):
    """subprocess timeout → (None, detail) 로 변환돼 재실행 경로에 태워진다 (suggestion·예외 흡수)."""
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=600)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    proc, detail = _run_live_or_timeout(
        ["claude"], cwd=".", env={}, timeout=600, harness="claude"
    )
    assert proc is None and detail and "timeout" in detail.lower()


def test_run_live_or_timeout_returns_proc_on_success(monkeypatch):
    """정상 실행이면 (proc, None) — timeout 흡수가 성공 경로를 방해하지 않는다."""
    fake = subprocess.CompletedProcess(["claude"], 0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    proc, detail = _run_live_or_timeout(
        ["claude"], cwd=".", env={}, timeout=600, harness="claude"
    )
    assert proc is fake and detail is None


# ── 자기 파일 release 마커 pin (마커 소실/개명 방어 — release_wave._count_marked_tests 재사용) ─────
# release 게이트(`pytest -m release`)는 이 파일의 라이브 2케이스를 selection 에 포함해야 한다.
# 마커가 소실/개명되면 조용히 빠지므로, 이 파일의 release 마커 수를 pin 해 red 로 잡는다.
# ⚠ 커플드-pin: 이 파일 추가로 release 총 수집 수가 7→9 로 늘었다 — 이 파일 touches 밖의 전역 pin
#   *5곳*이 함께 9 로 정합돼야 `livegate record`(수집 N==pin)가 통과한다(orchestrator 가 갱신):
#     (1) test_release_wave._EXPECTED_RELEASE_TESTS  (AST 수집 pin)
#     (2) test_release_wave._RELEASE_TEST_FILES      (스캔 파일 목록 — 이 파일 포함시켜야 함)
#     (3) board.LIVEGATE_RELEASE_PIN                 (livegate record 의 수집 게이트 단일 진실)
#     (4) tests/test_board_livegate.py               (하드코딩 fake 출력/assert 여러 곳 — 7→9)
#     (5) templates/{claude_code,opencode}/.project_manager/tools/board.py  (pm_update 전파·LIVEGATE_RELEASE_PIN)
_EXPECTED_LOCAL_RELEASE_TESTS = 2


def test_command_card_release_marker_count_is_pinned():
    """이 파일의 `release` 마커 테스트 수가 고정값과 일치 — 마커 소실/개명 시 게이트 누락 방어."""
    actual = _count_marked_tests(Path(__file__), "release")
    assert actual == _EXPECTED_LOCAL_RELEASE_TESTS, (
        f"이 파일 `release` 마커 수 {actual} != 기대 {_EXPECTED_LOCAL_RELEASE_TESTS} — 마커 "
        f"소실/개명 의심. 라이브 케이스를 의도적으로 늘렸다면 _EXPECTED_LOCAL_RELEASE_TESTS 와 "
        f"전역 커플드-pin 5곳(위 주석: test_release_wave._EXPECTED_RELEASE_TESTS·_RELEASE_TEST_FILES·"
        f"board.LIVEGATE_RELEASE_PIN·tests/test_board_livegate.py·templates/*/board.py)을 함께 갱신하라."
    )
