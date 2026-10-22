"""커맨드 카드 사용성 라이브 테스트 (release tier · ADR-0046 · ADR-0039 refine · 기본 skip).

기계 e2e(T-0112 계보)는 커맨드가 *존재·rc0* 임은 봐도 "실 LLM 이 부트스트랩 카드(ADR-0045)만
보고 **첫 시도**에 맞는 커맨드를 치는가"(사용성)는 못 본다. 카드의 소비자가 LLM 이므로 그 판정은
LLM 이 load-bearing 이다(사용자 지시: "진짜로 얘네들이 커맨드를 잘 쓰는지"). 이 파일은 그 갭을
릴리즈 라이브 tier 에서 메운다 — fresh import 홈에 **부트스트랩 커맨드 카드만** 컨텍스트로 주고
실 LLM 2하네스(claude + opencode/glm-5.2:cloud)에 표준 티켓 라이프사이클(new→cluster new→promote→
claim→종결)을 시켜 **각 조작의 첫 시도 커맨드가 그대로 rc0** & 출력에 `--help`/`-h` 호출·재시도
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

from pm_engine.domain import cluster_rules


import importlib.util
import json
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

# release_wave/runtime_smoke 헬퍼 재사용(중복 인프라 금지·같은 tests/ 디렉토리 import) —
# adopter import(hermetic·models 조회 차단)·LLM env 격리(화이트리스트)·ticket 상태 조회.
from test_fresh_adopter_runtime_smoke import _live_env, _load_pm_import, _tickets_in
# release 마커 AST 카운트(자기 파일 pin 가드용) — 마커 소실/개명 방어 도구 재사용. 안내 경로로 첫
# 작업 폴더를 만든 채택자 픽스처는 full wave 와 공용이다([[T-0956]]).
from test_release_wave import (
    _MULTIREPO_REPOS, _MULTIREPO_TICKET, _RELEASE_WAVE_TASK, _WAVE_FILE, _WAVE_INTEGRATION_SLOT,
    _WAVE_WORK_SLOT, _assert_multirepo_wave_side_effects, _baseline_adopter, _count_marked_tests,
    _guided_adopter,
    _import_multipm_home, _multirepo_wave_commands, _multirepo_wave_home, _pm_config,
)

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

# 표준 조작 시퀀스가 발행하는 probe ticket 의 제목. 카드의 발행 줄은 task 의 prefix 를 쓰므로
# (`--prefix` 를 따로 받지 않는다) 프롬프트도 prefix 를 지시하지 않는다([[T-0956]]).
PROBE_TITLE = "card usability probe"

# 기대 lifecycle 시퀀스(카드 "티켓 조작") — 라이브 프롬프트가 시키는 조작. 판정은 이 조작들이 실행
# 커맨드에서 **각 정확히 1회** 관측됐는지 본다: 0회=카드 커맨드 건너뜀(누락·false-green 원천)·
# 2회+=첫 시도 실패 후 재시도. read 조작(list/show/regression/prefix list 등)은 반복 무해·판정 제외.
# (핸드오프는 이 시퀀스에서 제외 — `_usability_prompt` docstring 근거.)
# **완료 축은 묶음 종결(`ticket_finish.py`)이다** — 발행이 모든 티켓을 크기 1 묶음에 귀속시켜
# direct `board.py complete` 는 결속 없이 거부되고 카드도 그 줄을 싣지 않는다. 카드가 처방하지
# 않는 명령을 기대 op 로 남기면 이 게이트는 LLM 에게 없는 커맨드를 요구해 라이브에서만 터진다
# (평시 skip 이라 회귀는 green — 이 파일이 막으려는 false-green 클래스 그대로다).
# 종결까지의 조작은 카드가 처방한다([[T-0956]]) — 리뷰 없이 닫는 표시(`tier`)·묶음 결속(`cluster
# new`)·작업 폴더 전환(`worktree_pool.py switch`)이 claim 과 종결 사이의 필수 조작이다.
_EXPECTED_LIFECYCLE_OPS = ("new", "cluster", "promote", "tier", "claim", "switch", "finish")

# mutating 조작 분류 집합(핸드오프 포함) — `_board_operation` 이 read/write 구분에 참조(classifier).
# 카드 시퀀스가 낼 수 있는 어휘만 담는다 — `complete` 는 카드가 처방하지 않으므로 여기 없다.
_SEQUENCE_MUTATING_OPS = frozenset(
    {"new", "promote", "tier", "cluster", "claim", "switch", "finish", "handoff"})

# --help/-h 토큰 매칭(--tests-pass·--session-seq 등 오탐 방지). 경계를 `[\w-]` 로 잡아 셸 구두점
# (`;`·따옴표)이 인접해도(`--help;`·`"--help"`) 매칭되게 한다 — opencode stdout 라인 스캔
# (`_help_invocation_lines`)용. claude per-커맨드 판정은 아래 `_command_has_help_flag` 가 shlex 토큰으로.
_HELP_FLAG_RE = re.compile(r"(?<![\w-])(?:--help|-h)(?![\w-])")


# ── 부트스트랩 카드 렌더 (LLM 아님·순수 엔진 재현) ──────────────────────────────
# 카드 정체성은 task 이름 하나다 — 슬롯은 정체성이 아니라 그 task 가 빌린 작업 폴더다.
CARD_TASK = "demo-task"


def _render_command_card(home: Path, task: str = CARD_TASK) -> str:
    """home 의 pm_bootstrap 엔진으로 이 홈의 커맨드 카드를 렌더한다 (LLM 아님·순수 엔진).

    부트스트랩이 정체성 채워 dump 하는 커맨드 카드(ADR-0045)를 라이브 LLM 에 *유일한 컨텍스트*로
    주기 위해 코드로 재현한다. 렌더는 task 이름과 모듈 상수(`_CARD_TOOL_INVOKE`)만 읽는
    순수 함수라 부작용 0 — 그래서 기본 생성자로 인스턴스화해 호출한다.
    """
    path = home / ".project_manager" / "tools" / "pm_bootstrap.py"
    # 유니크 모듈명 — 워크트리 자신의 pm_bootstrap 과 sys.modules 충돌 방지(경로별 격리).
    spec = importlib.util.spec_from_file_location(f"_card_bootstrap_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    inst = mod.PmBootstrap()
    inst._task_name = task
    return inst._build_command_card_markdown()


def _usability_prompt(card: str) -> str:
    """실 LLM 에 카드만 주고 표준 라이프사이클을 시키는 프롬프트 (카드 = 유일 컨텍스트).

    진입문서(CLAUDE.md/AGENTS.md) 경로를 *주지 않는다* — 카드만으로 커맨드/플래그를 골라야
    통과(= 카드 사용성). 종결까지의 조작(리뷰 없이 닫는 표시·묶음 결속·작업 폴더 전환·완료 조건
    체크)은 **카드가 처방한다**([[T-0956]]) — 프롬프트는 티켓의 성격(코드 변경 없음·리뷰 라운드 없이
    닫음)만 말하고 그 명령과 이름을 쓰지 않는다. 카드만 보는 새 채택자가 닫을 수 있어야 하기
    때문이다. 회귀 게이트 만족(`--no-pytest`)만 카드 밖 사실로 명시한다 — 채택자 fresh 트리에는 test
    스위트가 없다. 로그 한 줄은 지시하지 않는다(종결이 log 골격을 만든다·경계 실측 E17).
    카드 줄 순서대로 실행하기 전에 **티켓 본문 채우기** 단계를 명시해 claim=promote 선행 트랩(본문 채운 뒤
    promote)을 시퀀스에서 실제로 밟게 한다. 승격은 카드 줄 순서(묶음 결속 뒤 · [[T-0960]])를 따르므로
    별도 단계로 떼지 않는다 — 떼면 카드 순서와 어긋나 같은 줄을 두 번 실행하게 된다.
    핸드오프는 이 시퀀스에서 제외한다: 세션 종료 op 는 무거운 side-effect(pm_state write·log
    skeleton)라 첫 시도 판정을 흐리고, 핸드오프 라이브 커버리지는 기존 release full-wave/hard-stop
    계보가 보유한다.
    """
    return (
        "You are the PM for this project. Below is your bootstrap command card — it lists every "
        "command with identity already filled in. Use ONLY this card to decide which command and "
        "flags to run for each step. Do NOT run any command with --help or -h, and do NOT open "
        "other documentation.\n\n"
        "Run this standard ticket lifecycle, one card command per step, using the EXACT commands "
        "and flags shown on the card:\n"
        "  1. List your tickets.\n"
        f'  2. Create a new ticket titled "{PROBE_TITLE}".\n'
        "  3. Fill in the new ticket's body: the board tool created the ticket file from a template "
        "with placeholder sections — replace them with a brief real description so the ticket is "
        "ready to promote (per the card's warning, claim rejects unfilled/draft tickets, so the "
        "body must be filled and the ticket promoted before you can claim it).\n"
        "  4. Promote it, claim it and then finish it: run, in order, the command lines the card lists "
        "under its wave-claim entry (the promote line is among them, in card order) and then under its "
        "wave-finish entry for this ticket, following the card's warnings. This ticket changes no code and is closed without a review round. "
        "One fact the card does not state: this project ships no test suite, so pass --no-pytest "
        "to the finish command.\n\n"
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
# 첫 하위 명령이 곧 조작인 도구 — 카드의 작업 폴더 전환은 `worktree_pool.py switch` 다([[T-0956]]).
# board.py 에는 switch 하위 명령이 없어 조작 이름이 겹치지 않는다.
_SUBCOMMAND_TOOLS = frozenset({"board.py", "worktree_pool.py"})
# `board.py cluster` 는 다음 토큰이 이 이름일 때만 묶음 결속 조작이다 — `cluster show` 는 읽기라 반복 무해.
_CLUSTER_BINDING_SUBCOMMAND = "new"
# 스크립트 조작에 이 플래그가 붙으면 읽기다 — `ticket_finish.py --dry-run` 은 편집·board·git 없이 계획만
# 출력한다(흐름 카드가 처음 도는 묶음에 먼저 보라고 한다·[[T-0958]]). `cluster show` 와 같은 원칙.
_SCRIPT_READ_FLAG = "--dry-run"


def _op_command(inv: str, op: str, tid: str = "T-1") -> str:
    """그 op 를 실행하는 커맨드 1줄 — 도구·서브커맨드 형태가 op 마다 다르다(board.py <op> vs 스크립트)."""
    if op == "new":
        return f'{inv}/board.py new "{PROBE_TITLE}" --task {CARD_TASK}'
    if op == "tier":
        return f"{inv}/board.py tier {tid} pm-direct"
    if op == "cluster":
        return f"{inv}/board.py cluster new card --tickets {tid} --task {CARD_TASK}"
    if op == "switch":
        return f"{inv}/worktree_pool.py switch adopter-claude_1 task/card"
    if op == "finish":
        return f"{inv}/ticket_finish.py {tid} --task {CARD_TASK} --no-pytest"
    return f"{inv}/board.py {op} {tid}"


def _clean_sequence(inv: str, tid: str = "T-0001") -> list[str]:
    """카드대로 첫 티켓을 닫는 정상 명령열 — 기대 조작 순서 그대로 각 1회."""
    return [_op_command(inv, op, tid) for op in _EXPECTED_LIFECYCLE_OPS]


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
        lexer.escape = ""  # 백슬래시를 이스케이프로 먹지 않는다 — Windows 절대경로(C:\...)가 토큰째 살아야 한다.
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

    보는 자리는 셋이다 — `cd <경로>`(statement 의 명령 낱말일 때만 · `echo cd /etc` 는 실행이
    아니다) · `git -C <경로>` · `--cwd/--dir <경로>`(붙여 쓴 `=` 형태 포함). 프로그램 경로는
    대상이 아니다 — 격리 홈 안에서 `/usr/bin/python3` 로 실행하는 것은 정상이다. 상대경로도
    세지 않는다(실행 cwd = 격리 홈 기준). 판정하려는 것은 "어디에 대고 도는가" 이지 문자열에
    무엇이 들어 있나가 아니다.
    """
    targets: list[str] = []
    for statement in _shell_statements(command):
        index = 0
        while index < len(statement) and _ENV_ASSIGN_RE.match(statement[index]):
            index += 1
        words = statement[index:]
        if not words:
            continue
        for position, token in enumerate(words):
            if (position == 0 and token == "cd") or token in _DIRECTORY_OPTIONS:
                if position + 1 < len(words):
                    targets.append(words[position + 1])
            elif "=" in token and token.split("=", 1)[0] in _DIRECTORY_OPTIONS:
                targets.append(token.split("=", 1)[1])
    # 절대경로만 대상이다(실행 cwd=격리 홈 기준의 상대경로는 세지 않는다). POSIX(`/…`)와
    # Windows(`C:\…`·UNC) 를 모두 인정한다 — 한 규약만 보면 다른 플랫폼에서 이탈 탐지가 눈이 먼다.
    return [t for t in targets if posixpath.isabs(t) or ntpath.isabs(t)]


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
        elif cmd_base in _SUBCOMMAND_TOOLS or cmd_base in _SCRIPT_OPS:
            args = words               # 스크립트 직접 실행(chmod+x) — 명령어 자신이 스크립트.
        else:
            continue                   # echo/printf/cat/mv/… (비실행) — 배제.
        for pos, tok in enumerate(args):
            tok_base = tok.rsplit("/", 1)[-1]
            if tok_base in _SUBCOMMAND_TOOLS:
                if pos + 1 < len(args) and _OP_NAME_RE.match(args[pos + 1]):
                    op = args[pos + 1]
                    if op != "cluster" or args[pos + 2:pos + 3] == [_CLUSTER_BINDING_SUBCOMMAND]:
                        ops.append(op)
                break
            if tok_base in _SCRIPT_OPS:
                if _SCRIPT_READ_FLAG not in args[pos + 1:]:
                    ops.append(_SCRIPT_OPS[tok_base])
                break
    return ops


def _board_op_mentions(text: str) -> list[str]:
    """text 에 board/handoff 조작이 *언급*된 것을 loose 스캔 (opencode best-effort 전용·비-gating).

    opencode stdout 은 clean argv 가 아니라 에이전트 혼합 출력이라 실행/비실행 구분이 불가하다 —
    이 scan 은 판정을 gate 하지 않고(opencode 는 side-effect + --help 만 hard) 통과 사유에 관측을
    실어 투명화하는 용도다(에코여도 무해). 실행 여부 hard 판정은 claude `_executed_board_ops` 몫.
    """
    ops = re.findall(r"(?:board|worktree_pool)\.py\s+([a-z][a-z0-9-]*)", text)
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
      2. 기대 lifecycle op(`_EXPECTED_LIFECYCLE_OPS`)가 **각 정확히 1회** 실행 관측. 0회=카드 커맨드
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
    """시도별 fresh adopter — 재실행이 이전 시도 state 를 재사용하지 않게 유니크 디렉토리.

    엔진 안내 경로로 첫 작업 폴더 1개를 만들고 카드 task 가 보유한 채로 시작한다([[T-0956]]).
    """
    attempt_dir = tmp_path / f"{harness}-attempt{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    return _guided_adopter(attempt_dir, harness, task=CARD_TASK, folders=1)


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

    카드(ADR-0045)만 컨텍스트로 주고 카드 순서의 new→cluster new→promote→claim→종결을 시킨다 — 진입문서 경로 미제공.
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
        "board.py tier", "board.py cluster new", "worktree_pool.py switch",
        "ticket_finish.py", "pm_handoff.py",
    ):
        assert token in command_parts, f"카드 실행 줄에 '{token}' 부재"
    assert "board.py complete" not in command_parts, \
        "카드가 direct board complete 를 실행 줄로 되살렸다(항상 rc=1·X-001)"
    # 정체성 헤더는 실값 또는 미해소 명시 — 폐지된 `--session` placeholder 는 어느 쪽에도 없다.
    assert "정체성:" in card and "--session <session>" not in card


def test_usability_prompt_embeds_card_and_sequence():
    """프롬프트가 카드 전문 + 표준 lifecycle 시퀀스(create/fill/카드 순서의 promote·claim/finish)를 담고 --help 를 금한다."""
    card = _render_command_card(REPO)
    prompt = _usability_prompt(card)
    # 카드가 유일 컨텍스트로 임베드된다(진입문서 경로 미제공).
    assert card in prompt
    assert "CLAUDE.md" not in prompt and "AGENTS.md" not in prompt
    # 표준 조작 시퀀스 + probe 식별자.
    assert PROBE_TITLE in prompt
    for step in ("Create a new ticket", "Fill in the new ticket", "Promote it, claim it and then finish it",
                 "the promote line is among them, in card order", "wave-claim entry", "wave-finish entry",
                 "changes no code and is closed without a review round"):
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


def test_t0956_card_prescribes_closing_steps():
    """[[T-0956]] AT-010 — 첫 티켓을 닫는 조작은 카드가 처방하고 프롬프트는 그 명령을 쓰지 않는다.

    기대 조작은 발행·묶음 결속·승격·리뷰 없는 종결 표시·claim·작업 폴더 전환·종결 7개이고(카드 순서·[[T-0960]]) 전부 카드
    실행 줄에서 뽑힌다. 프롬프트 지시부(카드 임베드 앞)에는 그 명령·이름과 옛 prefix·로그 지시가
    없고, 카드 밖 사실인 `--no-pytest` 만 있다.
    """
    assert _EXPECTED_LIFECYCLE_OPS == (
        "new", "cluster", "promote", "tier", "claim", "switch", "finish")
    card = _render_command_card(REPO)
    prescribed: set[str] = set()
    for line in card.splitlines():
        prescribed.update(_executed_board_ops(line.split("#", 1)[0]))
    assert set(_EXPECTED_LIFECYCLE_OPS) <= prescribed, sorted(prescribed)
    instructions = _usability_prompt(card).split("Command card:", 1)[0]
    for banned in ("board.py tier", "cluster new", "worktree_pool.py", "--prefix", "log/current.md"):
        assert banned not in instructions, f"프롬프트 지시부가 카드 처방을 대신 적었다: {banned}"
    assert "--no-pytest" in instructions


# ── 카드 줄 그대로 첫 티켓 닫기 (T-0956 · 비라이브 · 실 채택자 · 실 git) ─────────────────────
# 카드 사용성 라이브 시험이 모델에게 기대하는 것을 결정적으로 먼저 증명한다: 렌더된 카드의 실행 줄을
# 사용자 입력 자리(제목·티켓 번호·묶음 이름·슬롯)만 바꿔 그대로 실행하면 첫 티켓이 done 이 된다.
_CARD_IDENTITY_ENV = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "user.email",
                      "GIT_CONFIG_VALUE_0": "pm-release-fixture@example.invalid",
                      "GIT_CONFIG_KEY_1": "user.name", "GIT_CONFIG_VALUE_1": "PM release fixture"}
_CARD_TOOLS_MARK = ".project_manager/tools/"


def _card_command_lines(card: str) -> dict[tuple[str, str | None], str]:
    """카드 실행 줄 → `(도구, 첫 하위 명령)` 별 첫 줄(주석 제외·인터프리터 포함 그대로).

    인터프리터 접두(`python3`·Windows `py -3`)는 하드코딩하지 않는다 — 도구 경로 표식 앞은 줄째
    보존한다. 하위 명령이 없는 도구(`ticket_finish.py <T-NNNN>`)는 None 키다.
    """
    found: dict[tuple[str, str | None], str] = {}
    for raw in card.splitlines():
        body = raw.split("  #", 1)[0].strip()
        if _CARD_TOOLS_MARK not in body or body.startswith(("/", "$")):
            continue
        rest = body.split(_CARD_TOOLS_MARK, 1)[1].split()
        sub = rest[1] if len(rest) > 1 and rest[1].isalpha() else None
        found.setdefault((rest[0], sub), body)
    return found


def _card_line_notes(card: str) -> dict[tuple[str, str | None], str]:
    """카드 실행 줄 → 그 줄의 주석과 바로 아래 ⚠ 줄(그 줄의 순서·전제 설명 · `_card_command_lines` 와 같은 키).

    같은 키가 다시 나오면(찾아가기 포인터 등) 첫 줄의 설명만 쓴다.
    """
    notes: dict[tuple[str, str | None], str] = {}
    current: tuple[str, str | None] | None = None
    for raw in card.splitlines():
        body = raw.strip()
        if _CARD_TOOLS_MARK in body and not body.startswith(("/", "$")):
            command, _sep, comment = body.partition("  #")
            rest = command.split(_CARD_TOOLS_MARK, 1)[1].split()
            key = (rest[0], rest[1] if len(rest) > 1 and rest[1].isalpha() else None)
            current = key if key not in notes else None
            if current is not None:
                notes[key] = comment.strip()
        elif current is not None and body.startswith("⚠"):
            notes[current] += " " + body
        else:
            current = None
    return notes


def _card_line_process(dest: Path, line: str, fills: dict[str, str],
                       extra: Sequence[str] = ()) -> subprocess.CompletedProcess:
    """카드 줄의 사용자 입력 자리만 채워 그대로 실행한다(cwd=홈 · rc 단언은 호출부)."""
    for placeholder, value in fills.items():
        line = line.replace(placeholder, value)
    argv = shlex.split(line) + list(extra)
    return subprocess.run(argv, cwd=dest, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})


def _run_card_line(dest: Path, line: str, fills: dict[str, str], extra: Sequence[str] = ()):
    """카드 줄을 채워 실행하고 rc 0 을 단언한다."""
    proc = _card_line_process(dest, line, fills, extra)
    assert proc.returncode == 0, f"{proc.args}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
    return proc


# 티켓 폴더 — 기본 형상은 PM 홈 git 안(`wiki/tickets`), 공유 board 형상은 별도 git(`board/tickets`)이다.
_CARD_TICKET_ROOTS = (Path(".project_manager") / "wiki" / "tickets", Path(".project_manager") / "board" / "tickets")


def _card_ticket(dest: Path, tid: str) -> Path:
    (path,) = [found for root in _CARD_TICKET_ROOTS for found in (dest / root).glob(f"*/{tid}-*.md")]
    return path


def _fill_card_ticket(dest: Path, tid: str, touches: Sequence[str] = ()) -> None:
    """promote 전 본문 자리표시자 채우기 — 사람(모델)이 하는 일. touches 는 frontmatter 목록."""
    path = _card_ticket(dest, tid)
    text = path.read_text(encoding="utf-8")
    for heading, body in (("목표", "카드 흐름 확인."), ("인터페이스", "없음."), ("결정", "없음."),
                          ("참고", "- 없음")):
        text = re.sub(rf"## {heading}\n.*?\n\n", f"## {heading}\n{body}\n\n", text, flags=re.S)
    text = re.sub(r"## 완료 조건 \(Definition of Done\)\n.*?\n\n",
                  "## 완료 조건 (Definition of Done)\n- [ ] 카드 흐름 확인\n\n", text, flags=re.S)
    if touches:
        listed = "".join(f"\n- {item}" for item in touches)
        assert "touches: []" in text
        text = text.replace("touches: []", f"touches:{listed}", 1)
    path.write_text(text, encoding="utf-8")


def _check_card_dod(dest: Path, tid: str) -> None:
    path = _card_ticket(dest, tid)
    path.write_text(path.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "), encoding="utf-8")


def _card_fills(tid: str, cluster: str, slot: str) -> dict[str, str]:
    # `<T-NNNN>` 를 `T-NNNN` 보다 먼저 바꾼다(꺾쇠 자리가 남지 않게).
    return {'"<제목>"': f'"{cluster} card probe"', "<T-NNNN>": tid, "T-NNNN": tid,
            "<ticket-group>": cluster, "<slot>": slot}


# 카드가 묶음 이름 자리(`cluster new <ticket-group>`) 바로 다음 줄에 싣는 규칙 — 받아들여지는 예시와 거부되는
# 티켓 ID 모양. 시험은 이 두 값과 `<slot>` 정의를 카드에서 읽는다(시험이 값을 고르지 않는다).
_CARD_NAME_RULE_RE = re.compile(r"예: `(?P<example>[^`]+)`\).*티켓 ID 모양\(`(?P<reserved>[^`]+)`\)")
_CARD_SLOT_FORM_RE = re.compile(r"<slot>=(?P<form>[^)\s]+)\)")
# pm-wave-claim 흐름 카드의 같은 규칙(bash 주석이라 백틱이 없다).
_SKILL_NAME_RULE_RE = re.compile(r"예: (?P<example>[\w.-]+)\)\. 티켓 ID 모양\((?P<reserved>[^)]+)\)")
_SKILL_REPO_PLACEHOLDER_RE = re.compile(r"--repo (<[^>]+>)")
_WAVE_SKILLS = REPO / ".claude" / "skills"


def _card_name_rule(card: str) -> tuple[str, str]:
    """카드의 `cluster new <ticket-group>` 바로 다음 줄에서 (예시 이름, 거부되는 티켓 ID 모양) 을 읽는다."""
    lines = card.splitlines()
    index = next(i for i, line in enumerate(lines) if "board.py cluster new <ticket-group>" in line)
    match = _CARD_NAME_RULE_RE.search(lines[index + 1])
    assert match, f"카드가 묶음 이름 자리의 규칙·예시를 주지 않는다: {lines[index + 1]!r}"
    return match["example"], match["reserved"]


def _card_slot_value(card: str, repo: str, number: int) -> str:
    """카드의 `<slot>` 정의를 실제 저장소 이름·슬롯 번호로 채운다."""
    match = _CARD_SLOT_FORM_RE.search(card)
    assert match, "카드가 <slot> 형식을 정의하지 않는다"
    return match["form"].replace("<repo>", repo).replace("<N>", str(number))


def _created_ticket_id(proc: subprocess.CompletedProcess) -> str:
    """발행 출력 `created T-NNNN …` 의 티켓 ID."""
    match = re.search(r"^created (T-\S+) ", proc.stdout, flags=re.M)
    assert match, proc.stdout
    return match.group(1)


def _load_board_module(home: Path):
    path = home / ".project_manager" / "tools" / "board.py"
    spec = importlib.util.spec_from_file_location(f"_card_board_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_t0956_card_cluster_name_rule_matches_engine(monkeypatch):
    """[[T-0956]] AT-012 — 카드·흐름 카드의 묶음 이름 예시는 엔진이 받고 티켓 ID 모양은 거부한다.

    카드 두 렌더(codex 표지 없음·있음 — codex 는 스킬 표기 `$`·codex 절이 붙는다)와 pm-wave-claim
    흐름 카드가 같은 규칙을 싣는다. 두 흐름 카드의 `--repo` 자리는 저장소 이름 표기(`<repo>`) 하나뿐이고,
    카드의 `<slot>` 정의는 `release`·`switch` 가 함께 받는 `work/<repo>_<N>` 이다.
    """
    board = _load_board_module(REPO)
    rules = []
    for codex in (False, True):
        for key in ("CODEX_THREAD_ID", "CODEX_CI"):
            monkeypatch.delenv(key, raising=False)
        if codex:
            monkeypatch.setenv("CODEX_CI", "1")
        card = _render_command_card(REPO)
        rules.append(_card_name_rule(card))
        assert _CARD_SLOT_FORM_RE.search(card)["form"] == "work/<repo>_<N>"
    claim_skill = (_WAVE_SKILLS / "pm-wave-claim" / "SKILL.md").read_text(encoding="utf-8")
    skill_rule = _SKILL_NAME_RULE_RE.search(claim_skill)
    assert skill_rule, "pm-wave-claim 흐름 카드가 묶음 이름 규칙을 싣지 않는다"
    rules.append((skill_rule["example"], skill_rule["reserved"]))
    assert len(set(rules)) == 1, rules
    example, reserved = rules[0]
    assert cluster_rules._validate_cluster_name(example) is None
    # 카드 표기 `T-NNNN` 의 N 은 숫자 자리다 — 숫자로 채운 티켓 ID 모양을 엔진이 예약으로 거부한다.
    assert reserved == "T-NNNN"
    instance = reserved.replace("N", "1")
    assert cluster_rules._validate_cluster_name(instance) is not None
    assert cluster_rules.is_auto_cluster_id(instance) == instance
    for name in ("pm-wave-claim", "pm-wave-finish"):
        text = (_WAVE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        placeholders = _SKILL_REPO_PLACEHOLDER_RE.findall(text)
        assert placeholders and set(placeholders) == {"<repo>"}, (name, placeholders)


# 뜻이 섞이던 옛 자리 표기와 그것이 남으면 안 되는 원본(엔진·스킬·방법론·관리자 앱).
_RETIRED_PLACEHOLDERS = ("<이름>", "<C-이름>")
_PLACEHOLDER_SOURCES = (
    (".project_manager/tools", "*.py"),
    (".claude/skills", "**/*.md"),
    (".project_manager/wiki", "**/*.md"),
    ("manager", "*.py"),
    ("manager/ui/src", "**/*.ts*"),
)


def test_t0956_flow_card_rule_scopes_task_name(monkeypatch):
    """[[T-0956]] 리뷰 F-005 — 자리 표기는 뜻별로 `<task>`·`<ticket-group>`·`<repo>` 다.

    원본에 옛 표기 `<이름>`·`<C-이름>` 이 남지 않는다. pm-wave-claim 흐름 카드의 cluster new 줄은
    묶음 이름 자리 `<ticket-group>`, claim 줄은 `--task <task>`·`--repo <repo>` 이고, 카드 두 렌더와
    흐름 카드의 규칙 줄은 `<ticket-group>` 자리를 말하며 AT-012 정규식이 그 줄에서 예시 `login-fix`
    와 거부 모양 `T-NNNN` 을 읽는다(규칙이 task 이름 자리로 번지지 않는다).
    """
    leftovers = []
    for folder, pattern in _PLACEHOLDER_SOURCES:
        files = sorted((REPO / folder).glob(pattern))
        assert files, f"원본 목록이 비었다: {folder}/{pattern}"
        for path in files:
            text = path.read_text(encoding="utf-8")
            leftovers += [(path.relative_to(REPO).as_posix(), token, text.count(token))
                          for token in _RETIRED_PLACEHOLDERS if token in text]
    assert leftovers == []

    lines = (_WAVE_SKILLS / "pm-wave-claim" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    cluster_new = [line for line in lines if "board.py cluster new " in line]
    assert cluster_new and all("board.py cluster new <ticket-group> " in line for line in cluster_new)
    claim = [line for line in lines if "board.py claim " in line and "--task " in line]
    assert claim, "흐름 카드에 task 모드 claim 줄이 없다"
    for line in claim:
        assert re.findall(r"--task (<[^>]+>)", line) == ["<task>"], line
        assert re.findall(r"--repo (<[^>]+>)", line) == ["<repo>"], line
    skill_rule = next(line for line in lines if _SKILL_NAME_RULE_RE.search(line))
    assert "<ticket-group> 은 새로 짓는 묶음 이름" in skill_rule
    match = _SKILL_NAME_RULE_RE.search(skill_rule)
    assert (match["example"], match["reserved"]) == ("login-fix", "T-NNNN")

    for codex in (False, True):
        for key in ("CODEX_THREAD_ID", "CODEX_CI"):
            monkeypatch.delenv(key, raising=False)
        if codex:
            monkeypatch.setenv("CODEX_CI", "1")
        card_lines = _render_command_card(REPO).splitlines()
        index = next(i for i, line in enumerate(card_lines)
                     if "board.py cluster new <ticket-group>" in line)
        assert "<ticket-group> 은 새로 짓는 묶음 이름" in card_lines[index + 1]
        rule = _CARD_NAME_RULE_RE.search(card_lines[index + 1])
        assert rule and (rule["example"], rule["reserved"]) == ("login-fix", "T-NNNN")


def test_t0956_card_commands_close_first_ticket(tmp_path):
    """[[T-0956]] AT-013 — 작업 폴더 1개 채택자에서 카드 줄만으로 코드 변경 없는 첫 티켓이 닫힌다.

    new·cluster new·promote·tier·claim·switch·ticket_finish·release 줄의 입력 자리를 카드 문구(묶음 이름
    예시·`<slot>` 정의)와 발행 출력(티켓 ID)의 값으로만 채워 실행한다. 묶음 이름 자리에 티켓 ID 를 넣은
    줄(라이브에서 모델이 넣은 값)은 카드가 말한 대로 예약으로 거부되고 아무것도 쓰지 않는다. 본문
    채우기와 완료 조건 체크는 사람(모델) 몫이고, 종결 줄의 `--no-pytest` 는 테스트 스위트가 없는
    채택자라 붙인다(프롬프트가 알리는 카드 밖 사실).
    """
    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1)
    card = _render_command_card(dest)
    lines = _card_command_lines(card)
    example, _reserved = _card_name_rule(card)
    slot = _card_slot_value(card, dest.name, 1)
    tid = _created_ticket_id(
        _run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"card probe"'}))
    fills = _card_fills(tid, example, slot)
    _fill_card_ticket(dest, tid)
    clusters = dest / ".project_manager" / "wiki" / "tickets" / "clusters"
    before = sorted(path.name for path in clusters.iterdir())
    refused = _card_line_process(dest, lines[("board.py", "cluster")], _card_fills(tid, tid, slot))
    assert refused.returncode == 1 and "예약" in refused.stderr, refused.stdout + refused.stderr
    assert sorted(path.name for path in clusters.iterdir()) == before
    for key in (("board.py", "cluster"), ("board.py", "promote"), ("board.py", "tier"), ("board.py", "show"),
                ("board.py", "lint"), ("board.py", "claim")):
        _run_card_line(dest, lines[key], fills)
    _check_card_dod(dest, tid)
    # [[T-0958]] 종결 줄은 switch 뒤다 — 카드가 두 줄 옆에 적고, switch 전 종결·dry-run 은 작업 트리 해소에서
    # rc 1 이며 아무것도 쓰지 않는다(라이브 두 시도의 실패 순서).
    notes = _card_line_notes(card)
    assert "switch 뒤" in notes[("ticket_finish.py", None)], notes[("ticket_finish.py", None)]
    assert "코드 변경이 없어도" in notes[("worktree_pool.py", "switch")], notes[("worktree_pool.py", "switch")]
    finish = lines[("ticket_finish.py", None)]
    for extra in (("--no-pytest", "--dry-run"), ("--no-pytest",)):
        early = _card_line_process(dest, finish, fills, extra)
        assert early.returncode == 1 and "작업 트리 해소" in early.stderr, early.stdout + early.stderr
    assert _tickets_in(dest, "claimed") == {_card_ticket(dest, tid).name}
    _run_card_line(dest, lines[("worktree_pool.py", "switch")], fills)
    _run_card_line(dest, finish, fills, extra=("--no-pytest",))
    _run_card_line(dest, lines[("pm_config.py", "release")], fills)

    assert _tickets_in(dest, "done") == {_card_ticket(dest, tid).name}
    assert "status: closed" in (clusters / f"C-{example}.md").read_text(encoding="utf-8")


# 카드 줄 순서 = 실행 순서다([[T-0960]]). board 형상(PM 홈 git 안·공유 board)과 무관한 한 순서이고, 공유
# board 의 promote 가 묶음 결속을 요구하고 tier 가 draft 를 거부하므로 cluster new 가 promote 앞이다.
_CARD_FLOW = (("board.py", "new"), ("board.py", "cluster"), ("board.py", "promote"), ("board.py", "tier"),
              ("board.py", "show"), ("board.py", "lint"), ("board.py", "claim"), ("worktree_pool.py", "switch"))


def _card_line_positions(card: str) -> dict[tuple[str, str | None], int]:
    """카드 실행 줄 키 → 그 줄이 카드에 처음 나오는 줄 번호(`_card_command_lines` 와 같은 키)."""
    positions: dict[tuple[str, str | None], int] = {}
    for index, raw in enumerate(card.splitlines()):
        body = raw.split("  #", 1)[0].strip()
        if _CARD_TOOLS_MARK not in body or body.startswith(("/", "$")):
            continue
        rest = body.split(_CARD_TOOLS_MARK, 1)[1].split()
        positions.setdefault((rest[0], rest[1] if len(rest) > 1 and rest[1].isalpha() else None), index)
    return positions


def test_t0960_card_draft_notes_follow_board_shape(tmp_path):
    """[[T-0960]] AT-001 — 카드의 new·promote·claim 설명과 줄 순서가 두 board 형상에서 같은 한 가지로 참이다.

    기본 형상(board 가 PM 홈 git 안)의 `board.py new` 는 draft 없이 `open/` 에 바로 발행하고, 공유 board
    (별도 git) 형상은 draft 를 만든다. 카드는 형상 조건 문구 없이 두 형상에서 참인 평서문을 쓰고, 줄은
    new → cluster new → promote → tier → show → lint → claim → switch 순서로 출력한다. 기본 형상에서
    그 순서로 new·cluster new·promote 를 실행하면 티켓은 `open/` 에 있고 promote 는 rc 0 으로 그대로 둔다.
    """
    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1)
    card = _render_command_card(dest)
    notes = _card_line_notes(card)
    for key in (("board.py", "new"), ("board.py", "promote"), ("board.py", "claim")):
        assert "공유 board" not in notes[key] and "draft 로 생성" not in notes[key], (key, notes[key])
    assert "이미 open 이면 그대로 둔다" in notes[("board.py", "promote")], notes[("board.py", "promote")]
    assert "cluster new 뒤" in notes[("board.py", "promote")], notes[("board.py", "promote")]
    positions = _card_line_positions(card)
    assert [positions[key] for key in _CARD_FLOW] == sorted(positions[key] for key in _CARD_FLOW), positions

    lines = _card_command_lines(card)
    tid = _created_ticket_id(
        _run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"draft shape probe"'}))
    opened = {_card_ticket(dest, tid).name}
    assert _tickets_in(dest, "open") == opened
    _fill_card_ticket(dest, tid)
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, dest.name, 1))
    for key in (("board.py", "cluster"), ("board.py", "promote")):
        _run_card_line(dest, lines[key], fills)
    assert _tickets_in(dest, "open") == opened


def _shared_board_adopter(tmp_path: Path, monkeypatch, *, folders: int) -> Path:
    """공유 board(별도 git) 형상 채택자 — `_guided_adopter` 와 같은 안내 경로에 `--board-submodule` 만 더한다.

    board 원격은 로컬 bare 저장소다. git 기본값은 submodule 의 로컬 경로 복제를 막으므로
    (`transport 'file' not allowed`) 이 시험의 자식 프로세스에만 `protocol.file.allow=always` 를 준다.
    """
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'protocol.file.allow'='always'")
    bare = tmp_path / "board.git"
    assert subprocess.run(["git", "init", "--bare", "-q", str(bare)], capture_output=True).returncode == 0
    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda source: (False, [])
    dest = tmp_path / "shared-board"
    assert pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual",
                           "--board-submodule", "--board-remote", str(bare)]) == 0
    _baseline_adopter(dest)
    repo = dest.name
    steps = [("worktree", "add", repo, "--user-ack", repo)] * folders
    steps += [("task", "new", CARD_TASK)] + [("alloc", repo, "--task", CARD_TASK)] * folders
    for step in steps:
        proc = subprocess.run([sys.executable, str(dest / ".project_manager" / "tools" / "pm_config.py"), *step],
                              cwd=dest, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})
        assert proc.returncode == 0, (step, proc.stdout[-1200:], proc.stderr[-1200:])
    return dest


@pytest.mark.parametrize("folders", (1, 2))
@pytest.mark.parametrize("shape", ("default", "shared-board"))
def test_t0960_card_flow_runs_first_try_in_both_board_shapes(tmp_path, monkeypatch, shape, folders):
    """[[T-0960]] — 카드 줄을 적힌 순서대로 실행하면 두 board 형상·작업 폴더 1개와 2개에서 new 부터 switch 까지
    전부 첫 시도에 rc 0 이다.

    폴더 2개면 카드 경고대로 발행·claim 은 작업 폴더(2번), cluster new 는 통합 폴더(1번)를 지목한다. 공유
    board 형상의 new 는 draft 를 만들고, cluster new 로 묶은 뒤의 promote 가 open 으로 옮긴다.
    """
    if shape == "default":
        dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=folders)
    else:
        dest = _shared_board_adopter(tmp_path, monkeypatch, folders=folders)
    repo = dest.name
    card = _render_command_card(dest)
    positions = _card_line_positions(card)
    assert [positions[key] for key in _CARD_FLOW] == sorted(positions[key] for key in _CARD_FLOW), positions
    lines = _card_command_lines(card)
    work = ("--repo", repo, "--slot", str(folders)) if folders == 2 else ()
    integration = ("--repo", repo, "--slot", "1") if folders == 2 else ()
    tid = _created_ticket_id(_run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"flow probe"'}, work))
    if shape == "shared-board":
        assert _card_ticket(dest, tid).parent.name == ".drafts", _card_ticket(dest, tid)
    _fill_card_ticket(dest, tid)
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, repo, folders))
    extras = {("board.py", "new"): work, ("board.py", "cluster"): integration, ("board.py", "claim"): work}
    for key in _CARD_FLOW[1:]:
        _run_card_line(dest, lines[key], fills, extras.get(key, ()))
    assert _card_ticket(dest, tid).parent.name == "claimed", _card_ticket(dest, tid)


def test_t0960_pm_env_skill_matches_card_alloc_flow():
    """[[T-0960]] 리뷰 F-001 — pm-env 의 worktree add 절은 카드·ADOPT 와 같은 alloc 대여 흐름을 안내한다.

    카드와 ADOPT 는 PM 이 alloc 으로 작업 폴더를 빌리라고 하므로, 그 안내 뒤에 생성 직후 대여를 금지로 읽히는
    문장(지워진 바인딩 안내에 붙어 있던 것)이 없다. 세션 시작은 사용자가 정하고 `--user-ack` 를 세션이 스스로
    붙이지 않는다는 문장은 남는다. 원본과 채택자에게 깔리는 사본 전부를 본다.
    """
    skills = [_WAVE_SKILLS / "pm-env" / "SKILL.md",
              *sorted((REPO / "templates").glob("*/*/skills/pm-env/SKILL.md")),
              *sorted((REPO / "templates").glob("*/*/command/pm-env.md"))]
    assert len(skills) == 5, skills
    for path in skills:
        section = _markdown_section(path.read_text(encoding="utf-8"), "## worktree add")
        assert "alloc <repo> --task <task>" in section, (path, section)
        assert "worktree add <repo> --task <task> --user-ack <repo>" in section, (path, section)
        assert "은 사용자가 정한다" in section and "스스로 부착하지 않는다" in section, (path, section)
        assert "자기-할당" not in section and "자기 지시로 읽고" not in section, (path, section)


def test_t0960_template_readmes_name_delegate_hard_keys():
    """[[T-0960]] 리뷰 F-003 — codex·opencode README 의 local.conf 키 서술이 developer-hard 의 hard 티어 키를 밝힌다.

    렌더는 developer-hard 카드 값을 `delegate.developer.hard.*` 에서 읽는다(pm_render.DELEGATE_MODEL_CONF_KEYS).
    역할 이름을 그대로 넣은 `delegate.developer-hard.*` 는 읽지 않아 카드의 model 줄이 주석으로 남는다.
    """
    for name in ("codex", "opencode"):
        text = (REPO / "templates" / name / "README.md").read_text(encoding="utf-8")
        for key in ("delegate.developer.hard.model", "delegate.developer.hard.reasoning"):
            assert key in text, (name, key)
        assert "delegate.developer-hard." not in text, name


def test_t0956_card_integration_folder_merges_code_change(tmp_path):
    """[[T-0956]] AT-014 — 작업 폴더 2개면 카드 경고대로 `--repo --slot` 을 붙여 코드 변경이 머지된다.

    발행·claim 은 작업 폴더(2번), 묶음 선언은 통합 폴더(1번)를 지목하고 switch 는 2번을 묶음 브랜치로
    옮긴다. 묶음 이름·슬롯은 카드에서, 티켓 ID 는 발행 출력에서 읽는다. touches 에 선언한 probe.txt 를
    2번에서 커밋하면 종결이 통합 브랜치 `<repo>_1` 에 머지한다.
    """
    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=2)
    repo = dest.name
    card = _render_command_card(dest)
    lines = _card_command_lines(card)
    work, integration = ("--repo", repo, "--slot", "2"), ("--repo", repo, "--slot", "1")
    # [[T-0958]] 폴더 2개의 지목 규칙은 발행·묶음 선언·claim·회귀 줄 옆에 있고, 지목 없는 발행 줄은 rc 1 이다.
    notes = _card_line_notes(card)
    for key in (("board.py", "new"), ("board.py", "cluster"), ("board.py", "claim"), ("board.py", "regression")):
        assert "`--repo <repo> --slot <N>`" in notes[key], (key, notes[key])
    for phrase in ("cluster new 에 지목하지 않은 폴더", "switch 뒤 그 폴더에서 고친다"):
        assert phrase in notes[("worktree_pool.py", "switch")], notes[("worktree_pool.py", "switch")]
    ambiguous = _card_line_process(dest, lines[("board.py", "new")], {'"<제목>"': '"card probe"'})
    assert ambiguous.returncode == 1 and "모호" in ambiguous.stderr, ambiguous.stdout + ambiguous.stderr
    tid = _created_ticket_id(
        _run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"card probe"'}, extra=work))
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, repo, 2))
    _fill_card_ticket(dest, tid, touches=("probe.txt",))
    _run_card_line(dest, lines[("board.py", "cluster")], fills, extra=integration)
    _run_card_line(dest, lines[("board.py", "promote")], fills)
    _run_card_line(dest, lines[("board.py", "tier")], fills)
    _run_card_line(dest, lines[("board.py", "claim")], fills, extra=work)
    _run_card_line(dest, lines[("worktree_pool.py", "switch")], fills)
    folder = dest / "work" / f"{repo}_2"
    (folder / "probe.txt").write_text("hello from card\n", encoding="utf-8")
    environment = {**os.environ, **_CARD_IDENTITY_ENV}
    for argv in (["add", "probe.txt"], ["commit", "-q", "-m", "probe"]):
        assert subprocess.run(["git", "-C", str(folder), *argv], env=environment,
                              capture_output=True).returncode == 0
    _check_card_dod(dest, tid)
    _run_card_line(dest, lines[("ticket_finish.py", None)], fills, extra=("--no-pytest",))

    merged = subprocess.run(
        ["git", "--git-dir", str(dest / ".repos" / f"{repo}.git"), "show", f"{repo}_1:probe.txt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert merged.returncode == 0, merged.stderr
    assert merged.stdout == "hello from card\n"
    assert _tickets_in(dest, "done") == {_card_ticket(dest, tid).name}


def test_t0958_card_one_folder_code_change_borrows_second_folder(tmp_path):
    """[[T-0958]] — 폴더 1개 채택자의 코드 변경은 카드대로 cluster new 전에 폴더를 하나 더 빌려야 머지된다.

    폴더 1개 그대로 카드 줄을 따르면 종결이 통합 브랜치를 든 폴더를 찾지 못해 선검사 rc 1 이고 아무것도
    쓰지 않는다. 카드 alloc 경고대로 alloc 을 한 번 더 하면 풀 소진 rc 1 과 사용자 승인 명령이 나오고, 그
    명령(사용자 몫)을 실행한 뒤 폴더 2개 흐름으로 touches 의 probe.txt 가 통합 브랜치 `<repo>_1` 에 머지된다.
    """
    one = _guided_adopter(tmp_path / "one", "claude", task=CARD_TASK, folders=1)
    card = _render_command_card(one)
    assert any("cluster new 전에" in line and "alloc" in line
               for line in card.splitlines() if line.strip().startswith("⚠")), "카드가 폴더 2개 전제를 말하지 않는다"
    lines = _card_command_lines(card)
    tid = _created_ticket_id(_run_card_line(one, lines[("board.py", "new")], {'"<제목>"': '"code probe"'}))
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, one.name, 1))
    _fill_card_ticket(one, tid, touches=("probe.txt",))
    for key in (("board.py", "cluster"), ("board.py", "promote"), ("board.py", "tier"), ("board.py", "claim"),
                ("worktree_pool.py", "switch")):
        _run_card_line(one, lines[key], fills)
    (one / "work" / f"{one.name}_1" / "probe.txt").write_text("hello\n", encoding="utf-8")
    _check_card_dod(one, tid)
    blocked = _card_line_process(one, lines[("ticket_finish.py", None)], fills, ("--no-pytest",))
    assert blocked.returncode == 1 and "체크아웃한 작업 트리를 찾지 못했다" in blocked.stderr, blocked.stderr
    assert _tickets_in(one, "claimed") == {_card_ticket(one, tid).name}

    two = _guided_adopter(tmp_path / "two", "claude", task=CARD_TASK, folders=1)
    repo = two.name
    card = _render_command_card(two)
    lines = _card_command_lines(card)
    exhausted = _card_line_process(two, lines[("pm_config.py", "alloc")], {"<repo>": repo})
    approved = re.search(r"`pm-config (worktree add [^`]+)`", exhausted.stdout + exhausted.stderr)
    assert exhausted.returncode == 1 and approved, exhausted.stdout + exhausted.stderr
    added = subprocess.run([sys.executable, str(two / ".project_manager" / "tools" / "pm_config.py"),
                            *shlex.split(approved[1])], cwd=two, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env={**os.environ, **_CARD_IDENTITY_ENV})
    assert added.returncode == 0, added.stdout + added.stderr
    work, integration = ("--repo", repo, "--slot", "2"), ("--repo", repo, "--slot", "1")
    tid = _created_ticket_id(
        _run_card_line(two, lines[("board.py", "new")], {'"<제목>"': '"code probe"'}, extra=work))
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, repo, 2))
    _fill_card_ticket(two, tid, touches=("probe.txt",))
    _run_card_line(two, lines[("board.py", "cluster")], fills, extra=integration)
    _run_card_line(two, lines[("board.py", "promote")], fills)
    _run_card_line(two, lines[("board.py", "tier")], fills)
    _run_card_line(two, lines[("board.py", "claim")], fills, extra=work)
    _run_card_line(two, lines[("worktree_pool.py", "switch")], fills)
    (two / "work" / f"{repo}_2" / "probe.txt").write_text("hello\n", encoding="utf-8")
    _check_card_dod(two, tid)
    _run_card_line(two, lines[("ticket_finish.py", None)], fills, extra=("--no-pytest",))
    merged = subprocess.run(
        ["git", "--git-dir", str(two / ".repos" / f"{repo}.git"), "show", f"{repo}_1:probe.txt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert merged.returncode == 0 and merged.stdout == "hello\n", merged.stderr
    assert _tickets_in(two, "done") == {_card_ticket(two, tid).name}


def test_t0958_card_multirepo_one_folder_each_closes_first_ticket(tmp_path):
    """[[T-0958]] 리뷰 F-003 — 저장소마다 폴더 1개를 빌린 다중 저장소 task 가 카드대로 첫 티켓을 닫는다.

    카드의 폴더 수는 두 범위다. 지목 여부(new·cluster new·claim·regression)는 task 의 폴더 수이고, 통합·작업
    폴더 구분과 코드 변경의 폴더 2개 확보는 같은 저장소의 폴더 수다. task 가 work/repoa_1·work/repob_1 을
    보유하면 `--task` 만의 new·cluster new 는 rc 1 모호이고, repoa 1번을 지목해 발행·선언·claim 한 뒤 switch 는
    같은 저장소의 폴더 1개인 work/repoa_1 이다. 지목하지 않은 다른 저장소 폴더 work/repob_1 은 rc 1 이다.
    """
    home = _import_multipm_home(tmp_path, "claude")
    repo, other = _MULTIREPO_REPOS
    for step in (("task", "new", CARD_TASK), *(("alloc", name, "--task", CARD_TASK) for name in _MULTIREPO_REPOS)):
        proc = _pm_config(home, *step)
        assert proc.returncode == 0, (step, proc.stdout, proc.stderr)
    card = _render_command_card(home)
    alloc_warning = next(line for line in card.splitlines()
                         if line.strip().startswith("⚠") and "cluster new 전에" in line and "alloc" in line)
    assert "같은 저장소의 작업 폴더 2개" in alloc_warning, alloc_warning
    notes = _card_line_notes(card)
    assert "같은 저장소의 폴더 1개면 그 폴더" in notes[("worktree_pool.py", "switch")], notes
    for key in (("board.py", "new"), ("board.py", "cluster"), ("board.py", "claim"), ("board.py", "regression")):
        assert "task 의 폴더가" in notes[key], (key, notes[key])
    lines = _card_command_lines(card)
    pick = ("--repo", repo, "--slot", "1")
    ambiguous = _card_line_process(home, lines[("board.py", "new")], {'"<제목>"': '"card probe"'})
    assert ambiguous.returncode == 1 and "모호" in ambiguous.stderr, ambiguous.stdout + ambiguous.stderr
    tid = _created_ticket_id(
        _run_card_line(home, lines[("board.py", "new")], {'"<제목>"': '"card probe"'}, extra=pick))
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, repo, 1))
    _fill_card_ticket(home, tid)
    ambiguous = _card_line_process(home, lines[("board.py", "cluster")], fills)
    assert ambiguous.returncode == 1 and "모호" in ambiguous.stderr, ambiguous.stdout + ambiguous.stderr
    _run_card_line(home, lines[("board.py", "cluster")], fills, extra=pick)
    _run_card_line(home, lines[("board.py", "promote")], fills)
    _run_card_line(home, lines[("board.py", "tier")], fills)
    _run_card_line(home, lines[("board.py", "show")], fills)
    _run_card_line(home, lines[("board.py", "lint")], fills)
    _run_card_line(home, lines[("board.py", "claim")], fills, extra=pick)
    wrong = _card_line_process(home, lines[("worktree_pool.py", "switch")],
                               {**fills, "<slot>": _card_slot_value(card, other, 1)})
    assert wrong.returncode == 1 and "지정한 브랜치가 없다" in wrong.stderr, wrong.stdout + wrong.stderr
    _run_card_line(home, lines[("worktree_pool.py", "switch")], fills)
    _check_card_dod(home, tid)
    _run_card_line(home, lines[("ticket_finish.py", None)], fills, extra=("--no-pytest",))

    assert _tickets_in(home, "done") == {_card_ticket(home, tid).name}
    clusters = home / ".project_manager" / "wiki" / "tickets" / "clusters"
    assert "status: closed" in (clusters / f"C-{_card_name_rule(card)[0]}.md").read_text(encoding="utf-8")


def test_t0958_flow_cards_state_switch_order(tmp_path):
    """[[T-0958]] — 흐름 카드가 switch 필수·순서·폴더 선택과 task 세션의 claim 줄을 실행 줄 옆에 싣는다.

    pm-wave-claim: lint 줄은 `lint --gate`, 5단계 claim 줄은 하나이고 `--task <task>` 를 갖는다(T-0962 — 슬롯
    세션 claim 줄은 없다), 6단계는 코드 변경이 없어도 실행하고 폴더 1개·2개의 대상과 파일 수정 시점을 말한다.
    pm-wave-finish: 실행 블록의 전제와 dry-run 항목이 switch 뒤를 말한다. 옛 슬롯 형식 claim
    (`--repo --slot` 만)은 claim 자체가 rc 1 이고 티켓은 open 에 남는다([[T-0959]] AT-008 · 예전에는 claim
    이 통과하고 종결이 소유 불일치로 막혔다).
    """
    claim_text = (_WAVE_SKILLS / "pm-wave-claim" / "SKILL.md").read_text(encoding="utf-8")
    finish_text = (_WAVE_SKILLS / "pm-wave-finish" / "SKILL.md").read_text(encoding="utf-8")
    block = claim_text.split("```bash", 1)[1].split("```", 1)[0].splitlines()
    assert any(line.strip() == "python3 .project_manager/tools/board.py lint --gate" for line in block)
    claims = [index for index, line in enumerate(block) if "board.py claim " in line]
    assert len(claims) == 1 and "--task <task>" in block[claims[0]], block
    step6 = "\n".join(block[next(i for i, line in enumerate(block) if line.startswith("# 6.")):])
    for phrase in ("코드 변경이 없어도", "폴더 1개면 그 폴더", "cluster new 에 지목하지 않은 폴더",
                   "전환 뒤 그 폴더에서 고친다"):
        assert phrase in step6, phrase
    assert "switch 를 마쳤다" in finish_text.split("```bash", 1)[1].split("```", 1)[0]
    assert "switch 를 마친 뒤" in finish_text

    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1)
    card = _render_command_card(dest)
    lines = _card_command_lines(card)
    tid = _created_ticket_id(_run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"card probe"'}))
    fills = _card_fills(tid, _card_name_rule(card)[0], _card_slot_value(card, dest.name, 1))
    _fill_card_ticket(dest, tid)
    for key in (("board.py", "cluster"), ("board.py", "promote"), ("board.py", "tier")):
        _run_card_line(dest, lines[key], fills)
    slot_claim = [".project_manager/tools/board.py", "claim", tid, "--repo", dest.name, "--slot", "1"]
    refused = subprocess.run([sys.executable, *slot_claim], cwd=dest, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env={**os.environ, **_CARD_IDENTITY_ENV})
    assert refused.returncode == 1 and "위치 인자" in refused.stderr, refused.stdout + refused.stderr
    assert _card_ticket(dest, tid).parent.name == "open"


# 위임 경로 문서가 claim 다음 단계로 싣는 switch 진술의 조각 — 흐름 카드 6단계 줄과 같은 표기 규칙이다.
_DELEGATE_SWITCH_PHRASES = (
    "worktree_pool.py switch <slot> task/<ticket-group>", "<slot>=work/<repo>_<N>", "claim 뒤",
    "코드 변경이 없어도", "종결 전",
)


def _markdown_section(text: str, heading: str) -> str:
    """`heading` 줄부터 같은 수준 이상의 다음 헤딩 전까지."""
    level = heading.split(" ", 1)[0]
    body = text.split(heading + "\n", 1)[1]
    return re.split(rf"^#{{1,{len(level)}}} ", body, maxsplit=1, flags=re.M)[0]


def test_t0958_delegate_docs_state_switch_after_claim():
    """[[T-0958]] — 위임 경로 문서도 claim 뒤 switch(코드 변경이 없어도, 작업·종결 전)를 claim 바로 옆에 싣는다.

    pm_playbook §Wave 구성 2항(묶음 선언 + claim)과 pm-dev-delegate 의 클러스터 단계 표(묶음 선언 바로 다음
    행)·사전 조건(claim 항목 바로 다음 항목)이 대상이다. 원본과 채택자에게 깔리는 사본 전부를 본다.
    """
    playbooks = [REPO / ".project_manager" / "wiki" / "pm_playbook.md",
                 *sorted((REPO / "templates").glob("*/.project_manager/wiki/pm_playbook.md"))]
    cards = [_WAVE_SKILLS / "pm-dev-delegate" / "SKILL.md",
             *sorted((REPO / "templates").glob("*/*/skills/pm-dev-delegate/SKILL.md")),
             *sorted((REPO / "templates").glob("*/*/command/pm-dev-delegate.md"))]
    assert len(playbooks) > 1 and len(cards) > 1, (playbooks, cards)
    for path in playbooks:
        section = _markdown_section(path.read_text(encoding="utf-8"), "### Wave 구성 (묶음 단계)")
        item = section.split("2. **묶음 선언 + claim**", 1)[1].split("\n3. ", 1)[0]
        line = next((row for row in item.splitlines() if _DELEGATE_SWITCH_PHRASES[0] in row), "")
        assert all(phrase in line for phrase in _DELEGATE_SWITCH_PHRASES), (path, item)
    for path in cards:
        text = path.read_text(encoding="utf-8")
        rows = [row for row in _markdown_section(text, "## 클러스터 단계 표 (운영 단위 = 티켓 묶음)").splitlines()
                if row.startswith("| ")]
        declare = next(index for index, row in enumerate(rows) if row.startswith("| 묶음 선언 |"))
        assert all(phrase in rows[declare + 1] for phrase in _DELEGATE_SWITCH_PHRASES), (path, rows)
        items = [row for row in _markdown_section(text, "## 사전 조건").splitlines() if row.startswith("- ")]
        claimed = next(index for index, row in enumerate(items) if "멤버 티켓이 claim 됐다" in row)
        assert all(phrase in items[claimed + 1] for phrase in _DELEGATE_SWITCH_PHRASES), (path, items)


_DELEGATE_LOCATION_HEADING = "## 작업 위치 주입"


def test_t0958_delegate_work_location_names_work_folder():
    """[[T-0958]] 리뷰 F-004 — pm-dev-delegate 의 작업 위치 항목은 모든 사본에서 같은 지목 규칙이다.

    task 가 폴더를 여럿 보유하면 `--repo <repo> --slot <N>` 으로 지정하고, 지목하는 폴더는 switch 로 묶음
    브랜치에 옮긴 작업 폴더다. 잉여 슬롯 반납(`pm_config.py release`)으로 위치를 고르지 않는다. codex 어댑터
    원본(`.agents`)을 포함해 원본과 출하 사본 전부를 본다(codex 절 제목은 꼬리표가 붙는다).
    """
    cards = [_WAVE_SKILLS / "pm-dev-delegate" / "SKILL.md",
             *sorted((REPO / "templates").glob("*/*/skills/pm-dev-delegate/SKILL.md")),
             *sorted((REPO / "templates").glob("*/*/command/pm-dev-delegate.md"))]
    assert any(".agents" in path.parts for path in cards) and len(cards) == 5, cards
    for path in cards:
        text = path.read_text(encoding="utf-8")
        heading = next(line for line in text.splitlines() if line.startswith(_DELEGATE_LOCATION_HEADING))
        # 절 안 bash 블록의 `# 출력:` 주석이 헤딩처럼 보이므로 다음 `## ` 절 전까지를 자른다.
        section = re.split(r"^## ", text.split(heading + "\n", 1)[1], maxsplit=1, flags=re.M)[0]
        assert "pm_config.py release" not in section, path
        assert "--task <task> --repo <repo> --slot <N>" in section, path
        assert "switch 로 묶음 브랜치에 옮긴 작업 폴더" in section, path
        # 예시의 지목 슬롯도 작업 폴더 번호다(카드 흐름의 관례: 통합 1번 · 작업 2번).
        assert re.findall(r"--slot (\d+)`", section) == ["2"], path


def _run_multirepo_wave(home: Path, repo: str, edit=None, *, finish_rc: int = 0) -> subprocess.CompletedProcess:
    """다중 저장소 프롬프트의 그 저장소 실행 줄을 순서대로 실행한다 — 인터프리터만 이 프로세스로 바꾼다.

    맨 앞 부트스트랩 줄은 하네스 가드 훅이 세션을 기록해야 도는 줄이라 뺀다(작업 폴더 쓰기 가드를 여는
    용도 · 비라이브엔 가드 훅이 없다). 종결 줄 앞에서 파일을 쓰고(`edit` · 기본은 작업 폴더에 프롬프트 내용)
    완료 조건을 체크한다. 종결 앞 줄마다 rc 0, 종결 줄은 `finish_rc` 를 단언하고 종결 줄의 실행 결과를 돌려준다.
    """
    commands = _multirepo_wave_commands(repo)
    work = home / "work" / f"{repo}_{_WAVE_WORK_SLOT}"
    tid = _MULTIREPO_TICKET
    for index, command in enumerate(commands):
        last = index == len(commands) - 1
        if last:
            if edit is None:
                (work / _WAVE_FILE).write_text(f"done by {repo}\n", encoding="utf-8")
            else:
                edit()
            _check_card_dod(home, tid)
        argv = shlex.split(command.replace(_MULTIREPO_TICKET, tid))
        proc = subprocess.run([sys.executable, *argv[1:]], cwd=home, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env={**os.environ, **_CARD_IDENTITY_ENV})
        expected = finish_rc if last else 0
        assert proc.returncode == expected, f"{argv}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        if index == 0:
            tid = _created_ticket_id(proc)
    return proc


def test_t0958_multirepo_prompt_follows_card_and_closes_each_repo(tmp_path):
    """[[T-0958]] — 다중 저장소 프롬프트의 실행 줄은 카드 줄에 값을 채운 것이고 카드 순서다. 그대로 실행하면
    저장소마다 티켓 done·묶음 closed·바꾼 파일이 묶음 브랜치와 통합 브랜치 `<repo>_1` 에 있다.

    라이브 단언(`_assert_multirepo_wave_side_effects`)이 이 결과를 통과시킨다. 카드 줄과의 대조는 도구
    경로 표식 뒤만 본다(인터프리터 접두는 플랫폼마다 다르다).
    """
    home = _multirepo_wave_home(tmp_path, "claude")
    card = _render_command_card(home, task=_RELEASE_WAVE_TASK)
    lines = _card_command_lines(card)
    order = list(lines)
    for repo in _MULTIREPO_REPOS:
        fills = {'"<제목>"': f'"wave probe {repo}"', "<T-NNNN>": _MULTIREPO_TICKET,
                 "T-NNNN": _MULTIREPO_TICKET, "<ticket-group>": f"wave-{repo}",
                 "<slot>": _card_slot_value(card, repo, _WAVE_WORK_SLOT)}
        positions = []
        for command in _multirepo_wave_commands(repo):
            rest = command.split(_CARD_TOOLS_MARK, 1)[1]
            words = rest.split()
            key = (words[0], words[1] if len(words) > 1 and words[1].isalpha() else None)
            expected = lines[key].split(_CARD_TOOLS_MARK, 1)[1]
            for placeholder, value in fills.items():
                expected = expected.replace(placeholder, value)
            assert rest == expected or rest.startswith(expected + " "), (rest, expected)
            positions.append(order.index(key))
        assert positions == sorted(positions), positions
        _run_multirepo_wave(home, repo)
    _assert_multirepo_wave_side_effects(home, subprocess.CompletedProcess([], 0, "", ""), "비라이브 재현")


@pytest.mark.parametrize(
    "shape", ["uncommitted-in-integration-folder", "committed-on-integration-branch", "closed-without-merge"])
def test_t0958_multirepo_side_effects_reject_unmerged_change(tmp_path, shape):
    """[[T-0958]] — 바꾼 파일이 종결의 머지로 들어가지 않은 웨이브는 티켓 done·묶음 closed 여도 단언이 거부한다.

    6d1c39c9 라이브의 두 형상: 통합 폴더에 커밋하지 않은 파일(폴더 결과는 opencode 와 같다)과 통합 브랜치
    직접 커밋(claude). 두 형상은 예전에 종결이 rc 0 이었고 통합 폴더에 파일이 있어 옛 단언은 통과시켰다.
    [[T-0959]] 부터 두 형상은 종결 줄이 첫 부작용 전에 "완료 기록 거부" 로 멈추고 티켓 claimed·장부 open 이다 —
    단언은 그 상태도 거부한다. 셋째 형상은 작업 폴더에서 고쳐 묶음 브랜치에는 있으나 통합 브랜치에 머지되지
    않은 결과다(장부만 닫힌 경우 · 종결 뒤 통합 브랜치를 종결 전 커밋으로 되돌려 만든다). 통합 브랜치 확인이
    머지 없는 종결을 거부한다.
    """
    home = _multirepo_wave_home(tmp_path, "claude")
    integration = home / "work" / f"repoa_{_WAVE_INTEGRATION_SLOT}"
    bare = ["git", "--git-dir", str(home / ".repos" / "repoa.git")]
    base = f"repoa_{_WAVE_INTEGRATION_SLOT}"
    before = subprocess.run([*bare, "rev-parse", base], capture_output=True, text=True, check=True).stdout.strip()

    def edit() -> None:
        (integration / _WAVE_FILE).write_text("done by repoa\n", encoding="utf-8")
        if shape == "committed-on-integration-branch":
            for argv in (["add", _WAVE_FILE], ["commit", "-q", "-m", "direct"]):
                assert subprocess.run(["git", "-C", str(integration), *argv], capture_output=True,
                                      env={**os.environ, **_CARD_IDENTITY_ENV}).returncode == 0

    if shape == "closed-without-merge":
        _run_multirepo_wave(home, "repoa")
        subprocess.run([*bare, "update-ref", f"refs/heads/{base}", before], check=True)
    else:
        refused = _run_multirepo_wave(home, "repoa", edit=edit, finish_rc=1)
        assert "완료 기록 거부" in refused.stderr and _WAVE_FILE in refused.stderr, refused.stderr
        clusters = home / ".project_manager" / "wiki" / "tickets" / "clusters"
        assert re.search(r"^status: open$", (clusters / "C-wave-repoa.md").read_text(encoding="utf-8"), flags=re.M)
    _run_multirepo_wave(home, "repob")
    assert (integration / _WAVE_FILE).exists()
    with pytest.raises(AssertionError, match="repo 'repoa'"):
        _assert_multirepo_wave_side_effects(home, subprocess.CompletedProcess([], 0, "", ""), "비라이브 음성")


# ── [[T-0959]] 종결·claim·발행 경로 결함 — 안내 경로 채택자에서 카드 줄 그대로 실행 ────────────────────
_HOME_ONLY_DOC = ".project_manager/wiki/raw/home-only-note.md"   # 홈 git 에만 있는 문서(작업 폴더 저장소엔 없다)
_HOME_ALSO_FILE = "pm-config.sh"            # 기준 커밋에 있어 홈과 작업 폴더 양쪽에 있는 파일
_CODE_ONLY_FILE = "only-code.txt"           # 1차 묶음이 통합 브랜치에 넣는 파일(홈에는 없다)
_PROBE_FILE = "probe.txt"


def _git_out(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **_CARD_IDENTITY_ENV})


def _home_status(dest: Path, where: Path | None = None) -> list[str]:
    """그 트리의 `git status --porcelain --untracked-files=all` 줄(종결 뒤 남은 미커밋)."""
    proc = _git_out("-C", str(where or dest), "status", "--porcelain", "--untracked-files=all")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _commit_in(folder: Path, *paths: str, message: str = "probe") -> None:
    for argv in (["add", "--", *paths], ["commit", "-q", "-m", message]):
        proc = _git_out("-C", str(folder), *argv)
        assert proc.returncode == 0, (argv, proc.stdout, proc.stderr)


def _seed_home_only_doc(dest: Path) -> None:
    """홈 git 에만 있는 문서를 기준 커밋 위에 하나 더 커밋한다(작업 폴더 저장소는 모른다)."""
    doc = dest / _HOME_ONLY_DOC
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("note\n", encoding="utf-8")
    _commit_in(dest, _HOME_ONLY_DOC, message="home-only note")


def _ledger_status(dest: Path, cluster: str) -> str | None:
    path = dest / ".project_manager" / "wiki" / "tickets" / "clusters" / f"C-{cluster}.md"
    match = re.search(r"^status:\s*(\S+)", path.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1) if match else None


def _branch_rev(dest: Path, branch: str) -> str:
    proc = _git_out("--git-dir", str(dest / ".repos" / f"{dest.name}.git"), "rev-parse", branch)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _branch_has(dest: Path, branch: str, path: str) -> bool:
    return _git_out("--git-dir", str(dest / ".repos" / f"{dest.name}.git"), "cat-file", "-e",
                    f"{branch}:{path}").returncode == 0


def _home_head(dest: Path) -> str:
    return _git_out("-C", str(dest), "rev-parse", "HEAD").stdout.strip()


class _CardFlow:
    """안내 경로 채택자 하나에서 카드 줄을 이름으로 실행한다 — 폴더 2개면 작업 2번·통합 1번을 지목한다."""

    def __init__(self, tmp_path: Path, *, folders: int, cluster: str | None = None):
        self.dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=folders)
        self.repo = self.dest.name
        self.card = _render_command_card(self.dest)
        self.lines = _card_command_lines(self.card)
        self.folders = folders
        self.cluster = cluster or _card_name_rule(self.card)[0]
        self.work = self.dest / "work" / f"{self.repo}_{folders}"
        self.integration = self.dest / "work" / f"{self.repo}_1"
        self.pick = ("--repo", self.repo, "--slot", str(folders)) if folders == 2 else ()
        self.pick_integration = ("--repo", self.repo, "--slot", "1") if folders == 2 else ()
        self.tid = ""
        self.fills: dict[str, str] = {}

    def publish(self, touches: Sequence[str] = (), title: str = "card probe", cluster: str | None = None) -> str:
        """발행 → 본문 채우기 → promote → tier → cluster new → claim (switch 전까지)."""
        self.publish_only(title)
        self.declare(touches, cluster)
        return self.tid

    def publish_only(self, title: str = "card probe") -> str:
        self.tid = _created_ticket_id(
            _run_card_line(self.dest, self.lines[("board.py", "new")], {'"<제목>"': f'"{title}"'}, extra=self.pick))
        return self.tid

    def declare(self, touches: Sequence[str] = (), cluster: str | None = None) -> None:
        if cluster is not None:
            self.cluster = cluster
        self.fills = _card_fills(self.tid, self.cluster, _card_slot_value(self.card, self.repo, self.folders))
        _fill_card_ticket(self.dest, self.tid, touches=touches)
        for key in (("board.py", "promote"), ("board.py", "tier")):
            _run_card_line(self.dest, self.lines[key], self.fills)
        _run_card_line(self.dest, self.lines[("board.py", "cluster")], self.fills, extra=self.pick_integration)
        _run_card_line(self.dest, self.lines[("board.py", "claim")], self.fills, extra=self.pick)

    def switch(self, *, check: bool = True) -> subprocess.CompletedProcess:
        line = self.lines[("worktree_pool.py", "switch")]
        if check:
            return _run_card_line(self.dest, line, self.fills)
        return _card_line_process(self.dest, line, self.fills)

    def finish(self) -> subprocess.CompletedProcess:
        _check_card_dod(self.dest, self.tid)
        return _card_line_process(self.dest, self.lines[("ticket_finish.py", None)], self.fills, ("--no-pytest",))

    def handoff(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.dest / ".project_manager" / "tools" / "pm_handoff.py"), "--task", CARD_TASK,
             "--user-ack", CARD_TASK, "--no-pytest"],
            cwd=self.dest, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})


def _assert_home_clean_and_handoff(flow: _CardFlow) -> None:
    """[[T-0959]] I1 — 종결 rc 0 뒤 홈에 미커밋이 없고 장부 `closed` 가 홈 HEAD 에 있으며 첫 핸드오프가 통과한다."""
    assert _home_status(flow.dest) == []
    ledger = f".project_manager/wiki/tickets/clusters/C-{flow.cluster}.md"
    shown = _git_out("-C", str(flow.dest), "show", f"HEAD:{ledger}")
    assert shown.returncode == 0 and "status: closed" in shown.stdout, shown.stdout + shown.stderr
    handoff = flow.handoff()
    assert handoff.returncode == 0, handoff.stdout[-1500:] + handoff.stderr[-1500:]


@pytest.mark.parametrize(
    "shape", ["one-folder-no-code", "two-folders-code", "absorbed-size-one-ledger", "home-only-doc-deletion"])
def test_t0959_home_git_finish_leaves_home_clean(tmp_path, shape):
    """[[T-0959]] AT-001 — board 가 홈 git 인 채택자에서 카드 줄로 종결하면 그 묶음의 board 산출이 홈 커밋에 실린다.

    종결은 장부에 `closed` 를 쓰고, 묶음 선언이 흡수한 크기 1 장부·이동한 티켓의 옛 경로·선언한 홈 문서의 삭제를
    남긴다. 이 넷이 홈 커밋에 실리지 않으면 종결은 rc 0 인데 첫 핸드오프가 미커밋 잔여로 멈춘다.
    """
    flow = _CardFlow(tmp_path, folders=2 if shape == "two-folders-code" else 1)
    if shape == "home-only-doc-deletion":
        _seed_home_only_doc(flow.dest)
    if shape == "absorbed-size-one-ledger":
        flow.publish_only()
        _commit_in(flow.dest, ".", message="session outputs")       # 발행이 만든 크기 1 장부를 추적한다
        flow.declare()
    else:
        touches = {"two-folders-code": (_PROBE_FILE,), "home-only-doc-deletion": (_HOME_ONLY_DOC,)}.get(shape, ())
        flow.publish(touches)
    flow.switch()
    if shape == "two-folders-code":
        (flow.work / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
    if shape == "home-only-doc-deletion":
        (flow.dest / _HOME_ONLY_DOC).unlink()
    finished = flow.finish()
    assert finished.returncode == 0, finished.stdout[-1500:] + finished.stderr[-1500:]

    assert _tickets_in(flow.dest, "done") == {_card_ticket(flow.dest, flow.tid).name}
    tracked = _git_out("-C", str(flow.dest), "ls-files").stdout.splitlines()
    tickets = ".project_manager/wiki/tickets"
    assert f"{tickets}/clusters/C-{flow.tid}.md" not in tracked
    assert not any(path.startswith(f"{tickets}/open/{flow.tid}-") for path in tracked), tracked
    assert _HOME_ONLY_DOC not in tracked
    _assert_home_clean_and_handoff(flow)


def test_t0959_multirepo_wave_leaves_home_clean(tmp_path):
    """[[T-0959]] AT-002 — 다중 저장소 홈을 기준 커밋한 뒤 저장소마다 프롬프트 줄을 실행하면 홈이 깨끗하고 핸드오프가 통과한다."""
    home = _multirepo_wave_home(tmp_path, "claude")
    _commit_in(home, ".", message="baseline")
    assert _home_status(home) == []
    for repo in _MULTIREPO_REPOS:
        _run_multirepo_wave(home, repo)
    assert _home_status(home) == []
    handoff = subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_handoff.py"), "--task", _RELEASE_WAVE_TASK,
         "--user-ack", _RELEASE_WAVE_TASK, "--no-pytest"],
        cwd=home, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **_CARD_IDENTITY_ENV})
    assert handoff.returncode == 0, handoff.stdout[-1500:] + handoff.stderr[-1500:]


def _lost_change_flow(tmp_path: Path, shape: str) -> tuple[_CardFlow, str, list[str]]:
    """선언한 변경이 종결의 머지 대상 밖에 놓이는 형상을 만든다 — (흐름, 잃은 경로, 종결할 티켓들)."""
    if shape == "cluster-member-lost":
        flow = _CardFlow(tmp_path, folders=2, cluster="grp")
        members = []
        for title, touch in (("member a", "a.txt"), ("member b", "b.txt")):
            flow.publish_only(title)
            flow.fills = _card_fills(flow.tid, flow.cluster, _card_slot_value(flow.card, flow.repo, 2))
            _fill_card_ticket(flow.dest, flow.tid, touches=(touch,))
            for key in (("board.py", "promote"), ("board.py", "tier")):
                _run_card_line(flow.dest, flow.lines[key], flow.fills)
            members.append(flow.tid)
        board = [sys.executable, str(flow.dest / ".project_manager" / "tools" / "board.py")]
        for argv in (["cluster", "new", flow.cluster, "--tickets", ",".join(members), "--task", CARD_TASK,
                      *flow.pick_integration],
                     *(["claim", member, "--task", CARD_TASK, *flow.pick] for member in members)):
            proc = subprocess.run([*board, *argv], cwd=flow.dest, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  env={**os.environ, **_CARD_IDENTITY_ENV})
            assert proc.returncode == 0, (argv, proc.stdout, proc.stderr)
        (flow.work / "b.txt").write_text("b\n", encoding="utf-8")
        _commit_in(flow.work, "b.txt")                              # switch 전 원래 브랜치에 커밋 — 유실
        flow.switch()
        (flow.work / "a.txt").write_text("a\n", encoding="utf-8")
        return flow, "b.txt", members
    flow = _CardFlow(tmp_path, folders=1 if shape == "one-folder-commit-on-base" else 2)
    flow.publish((_PROBE_FILE,))
    if shape == "commit-before-switch":
        (flow.work / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
        _commit_in(flow.work, _PROBE_FILE)
        flow.switch()
    elif shape in ("write-before-switch", "one-folder-commit-on-base"):
        (flow.work / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
        refused = flow.switch(check=False)
        assert refused.returncode != 0, refused.stdout + refused.stderr
        _commit_in(flow.work, _PROBE_FILE)
        flow.switch()
    else:                                                           # write-in-integration-folder
        flow.switch()
        (flow.integration / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
    return flow, _PROBE_FILE, [flow.tid]


@pytest.mark.parametrize("shape", ["commit-before-switch", "write-before-switch", "write-in-integration-folder",
                                   "one-folder-commit-on-base", "cluster-member-lost"])
def test_t0959_finish_refuses_declared_change_outside_merge(tmp_path, shape):
    """[[T-0959]] AT-003 — 선언한 코드 경로의 변경이 이번 종결이 머지할 변경에 하나도 없으면 첫 부작용 전에 멈춘다.

    switch 전 커밋·통합 폴더 작성·통합 브랜치 직접 커밋·묶음 멤버 하나의 유실은 예전에 done·closed 로 닫혔다.
    """
    flow, lost, members = _lost_change_flow(tmp_path, shape)
    for member in members:
        _check_card_dod(flow.dest, member)
    base_before, home_before = _branch_rev(flow.dest, f"{flow.repo}_1"), _home_head(flow.dest)
    if shape == "cluster-member-lost":
        refused = subprocess.run(
            [sys.executable, str(flow.dest / ".project_manager" / "tools" / "ticket_finish.py"), "--cluster",
             f"C-{flow.cluster}", "--task", CARD_TASK, "--no-pytest"],
            cwd=flow.dest, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})
    else:
        refused = flow.finish()

    assert refused.returncode == 1, refused.stdout[-1500:] + refused.stderr[-1500:]
    assert "완료 기록 거부" in refused.stderr and "아직 아무 부작용도 내지 않았다" in refused.stderr, refused.stderr
    assert lost in refused.stderr, refused.stderr
    assert _tickets_in(flow.dest, "claimed") == {_card_ticket(flow.dest, member).name for member in members}
    assert _ledger_status(flow.dest, flow.cluster) == "open"
    assert _branch_rev(flow.dest, f"{flow.repo}_1") == base_before
    assert _home_head(flow.dest) == home_before


def _declared_change_flow(tmp_path: Path, shape: str, *, removal: str = "rm") -> _CardFlow:
    """선언한 변경이 머지 대상에 들어가는 정상 형상을 만들고 종결까지 실행한다(rc 단언은 호출부)."""
    def remove(folder: Path, path: str) -> None:
        if removal == "rm":
            (folder / path).unlink()
        else:
            proc = _git_out("-C", str(folder), "rm", "-q", "--", path)
            assert proc.returncode == 0, proc.stderr

    if shape == "code-only-deletion":
        flow = _CardFlow(tmp_path, folders=2)
        flow.publish((_CODE_ONLY_FILE,), title="add wave", cluster="add-wave")
        flow.switch()
        (flow.work / _CODE_ONLY_FILE).write_text("x\n", encoding="utf-8")
        added = flow.finish()
        assert added.returncode == 0, added.stdout[-1500:] + added.stderr[-1500:]
        assert _branch_has(flow.dest, f"{flow.repo}_1", _CODE_ONLY_FILE)
        flow.publish((_CODE_ONLY_FILE,), title="delete wave", cluster="delete-wave")
        flow.switch()
        remove(flow.work, _CODE_ONLY_FILE)
        flow.removed = _CODE_ONLY_FILE
    else:
        flow = _CardFlow(tmp_path, folders=2 if shape in ("code-in-work-folder", "deletion-of-file-also-in-home")
                         else 1)
        if shape == "home-only-doc-edit":
            _seed_home_only_doc(flow.dest)
        touches = {"code-in-work-folder": (_PROBE_FILE,), "deletion-of-file-also-in-home": (_HOME_ALSO_FILE,),
                   "home-only-doc-edit": (_HOME_ONLY_DOC,)}.get(shape, ())
        flow.publish(touches)
        flow.switch()
        if shape == "code-in-work-folder":
            (flow.work / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
        elif shape == "deletion-of-file-also-in-home":
            assert (flow.dest / _HOME_ALSO_FILE).is_file() and (flow.work / _HOME_ALSO_FILE).is_file()
            remove(flow.work, _HOME_ALSO_FILE)
            flow.removed = _HOME_ALSO_FILE
        elif shape == "home-only-doc-edit":
            doc = flow.dest / _HOME_ONLY_DOC
            doc.write_text(doc.read_text(encoding="utf-8") + "- probe edit\n", encoding="utf-8")
    flow.finished = flow.finish()
    return flow


def _assert_deletion_merged(flow: _CardFlow) -> None:
    """선언한 삭제가 묶음 브랜치와 통합 브랜치에 실리고 작업 폴더에 staged 삭제로 남지 않는다."""
    assert not _branch_has(flow.dest, f"{flow.repo}_1", flow.removed)
    assert not _branch_has(flow.dest, f"task/{flow.cluster}", flow.removed)
    assert _home_status(flow.dest, flow.work) == []


@pytest.mark.parametrize("shape", ["no-touches", "code-in-work-folder", "code-only-deletion",
                                   "deletion-of-file-also-in-home", "home-only-doc-edit"])
def test_t0959_finish_merges_declared_change(tmp_path, shape):
    """[[T-0959]] AT-004 — 선언 변경이 머지 대상에 있는 정상 종결은 rc 0 이고 그 변경이 실체로 남는다(역방향).

    코드 없는 pm-direct(touches 비움)와 홈 전용 문서 편집은 선언 변경 판정의 대상이 아니거나 홈 변경으로 통과한다.
    작업 폴더에서 지운 파일은 홈에 같은 파일이 있어도 작업 폴더 쪽에서 커밋·머지된다.
    """
    flow = _declared_change_flow(tmp_path, shape)
    assert flow.finished.returncode == 0, flow.finished.stdout[-1500:] + flow.finished.stderr[-1500:]
    assert _card_ticket(flow.dest, flow.tid).parent.name == "done"
    assert _ledger_status(flow.dest, flow.cluster) == "closed"
    if shape == "code-in-work-folder":
        shown = _git_out("--git-dir", str(flow.dest / ".repos" / f"{flow.repo}.git"), "show",
                         f"{flow.repo}_1:{_PROBE_FILE}")
        assert shown.returncode == 0 and shown.stdout == "hello\n", shown.stderr
    if shape in ("code-only-deletion", "deletion-of-file-also-in-home"):
        _assert_deletion_merged(flow)
    if shape == "home-only-doc-edit":
        shown = _git_out("-C", str(flow.dest), "show", f"HEAD:{_HOME_ONLY_DOC}")
        assert "- probe edit" in shown.stdout, shown.stdout + shown.stderr


@pytest.mark.parametrize("shape", ["code-only-deletion", "deletion-of-file-also-in-home"])
def test_t0959_finish_commits_git_rm_staged_deletion(tmp_path, shape):
    """[[T-0959]] Q4 — `git rm` 으로 미리 stage 한 선언 삭제도 종결이 커밋·머지한다.

    index 에도 작업 트리에도 없는 그 경로는 `git add` 가 받지 못한다(rc 128). 예전 종결은 그 경로를 stage 범위에서
    빼 "스코프 밖 staged" 로 거부했다. add 는 작업 트리에 있는 파일만, 커밋은 선언 삭제까지 싣는다.
    """
    flow = _declared_change_flow(tmp_path, shape, removal="git-rm")
    assert flow.finished.returncode == 0, flow.finished.stdout[-1500:] + flow.finished.stderr[-1500:]
    assert "스코프 밖 staged" not in flow.finished.stderr
    assert _ledger_status(flow.dest, flow.cluster) == "closed"
    _assert_deletion_merged(flow)


def test_t0959_finish_refuses_clone_doc_edited_in_home(tmp_path):
    """[[T-0959]] Q1 — 홈과 작업 폴더 양쪽에 있는 문서(clone 형상)를 홈에서만 고친 티켓은 종결 전에 멈춘다.

    선언 경로가 작업 폴더에 있어 코드 몫이고, 홈의 편집은 머지 대상이 아니다. 예전 종결은 rc 0 이었지만 그 편집은
    어느 커밋에도 없어 첫 핸드오프가 미커밋 잔여로 멈췄다. 이제 첫 부작용 전에 멈추고 홈 편집은 그대로 남는다.
    """
    doc = ".project_manager/wiki/architecture.md"
    flow = _CardFlow(tmp_path, folders=1)
    assert (flow.dest / doc).is_file() and (flow.work / doc).is_file()
    flow.publish((doc,))
    flow.switch()
    (flow.dest / doc).write_text((flow.dest / doc).read_text(encoding="utf-8") + "- probe edit\n",
                                 encoding="utf-8")
    home_before = _home_head(flow.dest)
    refused = flow.finish()
    assert refused.returncode == 1, refused.stdout[-1500:] + refused.stderr[-1500:]
    assert "완료 기록 거부" in refused.stderr and doc in refused.stderr, refused.stderr
    assert _card_ticket(flow.dest, flow.tid).parent.name == "claimed"
    assert _ledger_status(flow.dest, flow.cluster) == "open"
    assert _home_head(flow.dest) == home_before
    assert "- probe edit" in (flow.dest / doc).read_text(encoding="utf-8")


def test_t0959_finish_refuses_declared_new_file_written_in_home(tmp_path):
    """[[T-0959]] F-005 — 선언한 새 파일을 작업 폴더 대신 홈에 쓰면 종결은 첫 부작용 전에 멈춘다.

    홈에만 있는 선언 경로가 홈 git 미추적 새 파일이고 홈 상태 루트(board·wiki) 밖이면 선언 변경 판정에서 코드 몫이다.
    예전 종결은 그 파일을 홈 몫으로 보내 rc 0 으로 닫았고, 파일은 홈 커밋에만 실려 통합 브랜치에 들어가지 않았다.
    """
    flow = _CardFlow(tmp_path, folders=2)
    flow.publish((_PROBE_FILE,))
    flow.switch()
    (flow.dest / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
    base_before, home_before = _branch_rev(flow.dest, f"{flow.repo}_1"), _home_head(flow.dest)
    refused = flow.finish()
    assert refused.returncode == 1, refused.stdout[-1500:] + refused.stderr[-1500:]
    assert "완료 기록 거부" in refused.stderr and _PROBE_FILE in refused.stderr, refused.stderr
    assert _card_ticket(flow.dest, flow.tid).parent.name == "claimed"
    assert _ledger_status(flow.dest, flow.cluster) == "open"
    assert _branch_rev(flow.dest, f"{flow.repo}_1") == base_before
    assert _home_head(flow.dest) == home_before


def test_t0959_finish_commits_new_doc_inside_home_state_root(tmp_path):
    """[[T-0959]] F-005 역방향 — 홈 상태 루트(wiki) 안에 새로 쓴 선언 문서는 홈 커밋으로 종결된다(판정 대상 아님)."""
    doc = ".project_manager/wiki/raw/new-home-note.md"
    flow = _CardFlow(tmp_path, folders=2)
    flow.publish((doc,))
    flow.switch()
    (flow.dest / doc).write_text("note\n", encoding="utf-8")
    finished = flow.finish()
    assert finished.returncode == 0, finished.stdout[-1500:] + finished.stderr[-1500:]
    assert _card_ticket(flow.dest, flow.tid).parent.name == "done"
    assert _ledger_status(flow.dest, flow.cluster) == "closed"
    shown = _git_out("-C", str(flow.dest), "show", f"HEAD:{doc}")
    assert shown.returncode == 0 and shown.stdout == "note\n", shown.stderr


# board 가 별도 git(서브모듈)인 형상의 git 설정 — 서브모듈 복제(file 프로토콜)를 이 자식 프로세스에서만 연다.
_SUBMODULE_ENV = {**_CARD_IDENTITY_ENV, "GIT_CONFIG_COUNT": "3",
                  "GIT_CONFIG_KEY_2": "protocol.file.allow", "GIT_CONFIG_VALUE_2": "always"}


def _run_in(dest: Path, *argv: str, tool: str | None = None) -> subprocess.CompletedProcess:
    """홈에서 엔진 도구(`tool`) 또는 git 을 실행하고 rc 0 을 단언한다."""
    command = ([sys.executable, str(dest / ".project_manager" / "tools" / tool), *argv] if tool
               else ["git", "-C", str(dest), *argv])
    proc = subprocess.run(command, cwd=dest, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={**os.environ, **_SUBMODULE_ENV})
    assert proc.returncode == 0, (command, proc.stdout[-1500:], proc.stderr[-1500:])
    return proc


def _board_submodule_adopter(tmp_path: Path) -> Path:
    """board 가 별도 git(서브모듈)인 채택자 — 설치 → 기준 커밋 → 안내 경로(폴더 1개를 task 가 보유)."""
    from test_fresh_adopter_runtime_smoke import _load_pm_import

    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda source: (False, [])
    bare = tmp_path / "board.git"
    assert _git_out("init", "--bare", "-q", str(bare)).returncode == 0
    dest = tmp_path / "adopter-sub"
    assert pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Sub",
                           "--board-submodule", "--board-remote", str(bare), "--fill", "manual"]) == 0
    _run_in(dest, "-c", "advice.addEmbeddedRepo=false", "add", "-A")
    _run_in(dest, "commit", "-q", "--no-gpg-sign", "-m", "seed")
    repo = dest.name
    for step in (("worktree", "add", repo, "--user-ack", repo), ("task", "new", CARD_TASK),
                 ("alloc", repo, "--task", CARD_TASK)):
        _run_in(dest, *step, tool="pm_config.py")
    return dest


def _fill_any_ticket(dest: Path, tid: str) -> None:
    """board 위치(홈 git·서브모듈)와 상태(draft·open)에 상관없이 그 티켓 본문을 채운다."""
    roots = [dest / ".project_manager" / "wiki" / "tickets", dest / ".project_manager" / "board" / "tickets"]
    (path,) = [found for root in roots for pattern in (f"open/{tid}-*.md", f".drafts/{tid}-*.md")
               for found in root.glob(pattern)]
    text = path.read_text(encoding="utf-8")
    for heading, body in (("목표", "카드 흐름 확인."), ("인터페이스", "없음."), ("결정", "없음."), ("참고", "- 없음")):
        text = re.sub(rf"## {heading}\n.*?\n\n", f"## {heading}\n{body}\n\n", text, flags=re.S)
    text = re.sub(r"## 완료 조건 \(Definition of Done\)\n.*?\n\n",
                  "## 완료 조건 (Definition of Done)\n- [ ] 카드 흐름 확인\n\n", text, flags=re.S)
    path.write_text(text, encoding="utf-8")


def test_t0959_board_submodule_finish_is_unchanged(tmp_path):
    """[[T-0959]] 역방향(I12) — board 가 별도 git(서브모듈)인 채택자의 종결은 바뀌지 않는다.

    장부·티켓 이동은 board git 이 커밋하고, 홈 커밋은 log 와 board 포인터다. 종결 뒤 홈과 board 가 모두 깨끗하고
    첫 핸드오프가 통과한다. 홈 git 형상에서 장부를 홈 커밋에 싣는 변경이 이 형상에 새지 않는다.
    """
    dest = _board_submodule_adopter(tmp_path)
    environment = {**os.environ, **_SUBMODULE_ENV}
    repo = dest.name

    def run(*argv: str, tool: str | None = None) -> subprocess.CompletedProcess:
        return _run_in(dest, *argv, tool=tool)

    card = _render_command_card(dest)
    lines = _card_command_lines(card)
    tid = _created_ticket_id(run("new", "submodule probe", "--task", CARD_TASK, tool="board.py"))
    cluster = _card_name_rule(card)[0]
    fills = _card_fills(tid, cluster, _card_slot_value(card, repo, 1))
    tickets = dest / ".project_manager" / "board" / "tickets"
    _fill_any_ticket(dest, tid)
    for key in (("board.py", "cluster"), ("board.py", "promote"), ("board.py", "tier"), ("board.py", "claim"),
                ("worktree_pool.py", "switch")):
        _run_card_line(dest, lines[key], fills)
    (claimed,) = list(tickets.glob(f"claimed/{tid}-*.md"))
    claimed.write_text(claimed.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "), encoding="utf-8")
    _run_card_line(dest, lines[("ticket_finish.py", None)], fills, extra=("--no-pytest",))

    assert list(tickets.glob(f"done/{tid}-*.md"))
    assert _home_status(dest) == []
    assert _home_status(dest, dest / ".project_manager" / "board") == []
    committed = _git_out("-C", str(dest / ".project_manager" / "board"), "show", f"HEAD:tickets/clusters/C-{cluster}.md")
    assert "status: closed" in committed.stdout, committed.stdout + committed.stderr
    handoff = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_handoff.py"), "--task", CARD_TASK,
         "--user-ack", CARD_TASK, "--no-pytest"],
        cwd=dest, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment)
    assert handoff.returncode == 0, handoff.stdout[-1500:] + handoff.stderr[-1500:]


@pytest.mark.parametrize("shape", ["home-git", "board-submodule"])
def test_t0959_wave_claim_skill_steps_reach_switch(tmp_path, shape):
    """[[T-0959]] — pm-wave-claim 실행 블록을 단계 순서대로 실행하면 두 board 형상 모두 claim·switch 까지 rc 0 이다.

    블록이 cluster new 다음에 promote 를 싣는다. 공유 board(서브모듈)의 발행은 draft 라 promote 없이는 claim 이
    "currently in draft/" 로 막혔다. 홈 git board 의 발행은 이미 open 이라 promote 는 그대로 두고 rc 0 이다.
    블록의 실행 줄을 전부 순서대로 읽고 자리표시자만 값으로 채운다(claim 줄은 하나이고 `--task` 를 갖는다 ·
    switch 는 6단계 · T-0962 AT-010).
    """
    dest = (_guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1) if shape == "home-git"
            else _board_submodule_adopter(tmp_path))
    repo = dest.name
    tid = _created_ticket_id(_run_in(dest, "new", "skill probe", "--task", CARD_TASK, tool="board.py"))
    _fill_any_ticket(dest, tid)
    claim_text = (_WAVE_SKILLS / "pm-wave-claim" / "SKILL.md").read_text(encoding="utf-8")
    block = claim_text.split("```bash", 1)[1].split("```", 1)[0].splitlines()
    step6 = next(index for index, line in enumerate(block) if line.startswith("# 6."))
    fills = {"<ticket-group>": "wave-probe", "<T-NNNN,T-NNNN>": tid, "T-NNNN": tid,
             "<설계 문서 경로>": ".project_manager/wiki/architecture.md", "<repo>": repo, "<N>": "1",
             "<task>": CARD_TASK, "<slot>": f"work/{repo}_1"}
    executed: list[str] = []
    claim_lines: list[str] = []
    for index, line in enumerate(block):
        command = line.split("  #", 1)[0].strip()
        if not command.startswith("python3 .project_manager/tools/"):
            continue
        if "board.py claim" in command:
            claim_lines.append(command)
        for placeholder, value in fills.items():
            command = command.replace(placeholder, value)
        argv = shlex.split(command)
        _run_in(dest, *argv[2:], tool=Path(argv[1]).name)
        executed.append(f"{index >= step6}:{Path(argv[1]).name} {argv[2]}")
    assert [entry.split(":", 1)[1] for entry in executed] == [
        "board.py cluster", "board.py promote", "board.py tier", "board.py cluster", "board.py show",
        "board.py lint", "board.py claim", "worktree_pool.py switch"], executed
    assert executed[-1].startswith("True:")             # switch 는 6단계 그대로
    assert len(claim_lines) == 1 and "--task <task>" in claim_lines[0], claim_lines
    roots = [dest / ".project_manager" / "wiki" / "tickets", dest / ".project_manager" / "board" / "tickets"]
    assert [found for root in roots for found in root.glob(f"claimed/{tid}-*.md")]


def test_t0959_direct_cluster_close_is_refused_then_finish_closes(tmp_path):
    """[[T-0959]] AT-011 — 내부 결속 플래그로 `board.py complete` 를 직접 부르면 종결 파이프라인 밖이라 거부된다.

    예전에는 티켓만 done 으로 옮기고 장부·머지를 남겨 이어지는 종결이 복구 경로 처방만 냈다. 거부 뒤 카드의 종결 줄이
    그대로 닫는다.
    """
    flow = _CardFlow(tmp_path, folders=2)
    flow.publish((_PROBE_FILE,))
    flow.switch()
    (flow.work / _PROBE_FILE).write_text("hello\n", encoding="utf-8")
    _check_card_dod(flow.dest, flow.tid)
    direct = subprocess.run(
        [sys.executable, str(flow.dest / ".project_manager" / "tools" / "board.py"), "complete", flow.tid,
         "--tests-pass", "--allow-missing-log", "--cluster-close", f"C-{flow.cluster}", "--task", CARD_TASK],
        cwd=flow.dest, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})
    assert direct.returncode == 1, direct.stdout + direct.stderr
    assert "closing" in direct.stderr and f"ticket_finish.py --cluster C-{flow.cluster}" in direct.stderr
    assert _card_ticket(flow.dest, flow.tid).parent.name == "claimed"
    assert _ledger_status(flow.dest, flow.cluster) == "open"

    finished = flow.finish()
    assert finished.returncode == 0, finished.stdout[-1500:] + finished.stderr[-1500:]
    shown = _git_out("--git-dir", str(flow.dest / ".repos" / f"{flow.repo}.git"), "show",
                     f"{flow.repo}_1:{_PROBE_FILE}")
    assert shown.stdout == "hello\n", shown.stderr
    assert _home_status(flow.dest) == []


def test_t0959_new_output_lint_command_passes_after_fill(tmp_path):
    """[[T-0959]] AT-012 — 발행 출력이 안내하는 lint 명령은 본문을 채우면 rc 0, 자리표시자가 남으면 rc 1 이다."""
    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1)
    lines = _card_command_lines(_render_command_card(dest))
    created = _run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"lint probe"'})
    tid = _created_ticket_id(created)
    match = re.search(r"then `(board\.py lint[^`]*)`", created.stdout)
    assert match and match.group(1) == "board.py lint --gate", created.stdout
    lint = [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), *match.group(1).split()[1:]]

    def run_lint() -> subprocess.CompletedProcess:
        return subprocess.run(lint, cwd=dest, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env={**os.environ, **_CARD_IDENTITY_ENV})

    unfilled = run_lint()
    assert unfilled.returncode == 1 and "placeholder" in unfilled.stdout + unfilled.stderr, unfilled.stdout
    _fill_card_ticket(dest, tid)
    filled = run_lint()
    assert filled.returncode == 0, filled.stdout + filled.stderr


def test_t0959_promote_output_names_what_happened(tmp_path):
    """[[T-0959]] N2 — board 가 홈 git 인 채택자에서 promote 출력은 board-git 승격을 말하지 않는다.

    이 형상의 발행은 draft 를 만들지 않고 board-git 도 없다 — 이동도 board-git 커밋도 없는데 "board-git 승격
    완료" 라고 말하던 것을 실제로 일어난 일(open/ 그대로, 커밋 없음)로 바꾼다.
    """
    dest = _guided_adopter(tmp_path, "claude", task=CARD_TASK, folders=1)
    lines = _card_command_lines(_render_command_card(dest))
    tid = _created_ticket_id(_run_card_line(dest, lines[("board.py", "new")], {'"<제목>"': '"promote probe"'}))
    _fill_card_ticket(dest, tid)
    promoted = _run_card_line(dest, lines[("board.py", "promote")], _card_fills(tid, "x", "x"))
    assert "board-git" not in promoted.stdout, promoted.stdout
    assert f"promoted {tid} (open/ 그대로 · board 가 홈 git 안이라 커밋 없음)" in promoted.stdout, promoted.stdout
    assert _card_ticket(dest, tid).parent.name == "open"


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
              f"--cwd {readonly}")
    clean = [
        "python3 .project_manager/tools/board.py list",       # 홈 안 상대 실행
        record,                                               # 형제 readonly 절대경로 --cwd
        f"cd {home} && {record}",                             # 격리 홈 절대경로로 cd
        "/usr/bin/python3 .project_manager/tools/board.py list",  # 절대경로 인터프리터(정상 실행)
        f"echo cd {outside}",                                 # 낱말 cd 를 담은 비실행 문자열
    ]
    escaping = f"cd {outside} && {record}"

    assert _commands_leaving_the_sandbox(clean, tmp_path) == []
    assert _commands_leaving_the_sandbox(clean + [escaping], tmp_path) == [escaping]
    assert _commands_leaving_the_sandbox(
        [f"git -C {outside} status"], tmp_path) == [f"git -C {outside} status"]


def test_board_operation_classifies_lifecycle_and_handoff():
    """커맨드 → 조작명 분류 (lifecycle·핸드오프·read op·비-도구)."""
    inv = "python3 .project_manager/tools"
    assert _board_operation(f'{inv}/board.py new "x" --task {CARD_TASK}') == "new"
    assert _board_operation(f"{inv}/board.py tier T-1 pm-direct") == "tier"
    assert _board_operation(f"{inv}/board.py cluster new card --tickets T-1") == "cluster"
    assert _board_operation(f"{inv}/worktree_pool.py switch adopter-claude_1 task/card") == "switch"
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
    commands = [f"{inv}/board.py list --mine", *_clean_sequence(inv)]
    ok, detail = _judge_commands(commands)
    assert ok, detail


def test_judge_commands_ignores_read_only_cluster_show():
    """[[T-0956]] AT-011 — 읽기 전용 `board.py cluster show` 는 묶음 결속 조작으로 세지 않는다.

    모든 조작이 첫 시도에 1회씩 실행된 명령열에 묶음 확인 한 줄이 섞여도 통과하고, `cluster new`
    를 두 번 실행한 명령열은 여전히 재시도로 실패한다.
    """
    inv = "python3 .project_manager/tools"
    show = f"{inv}/board.py cluster show card"
    assert _executed_board_ops(show) == []
    commands = []
    for command in _clean_sequence(inv):
        commands.append(command)
        if _executed_board_ops(command) == ["cluster"]:
            commands.append(show)
    ok, detail = _judge_commands(commands)
    assert ok, detail

    retried = [*_clean_sequence(inv), _op_command(inv, "cluster", "T-0001")]
    ok, detail = _judge_commands(retried)
    assert not ok and "cluster 재시도 ×2" in detail


def test_t0958_judge_ignores_finish_dry_run():
    """[[T-0958]] — 종결 `--dry-run` 은 읽기라 종결 조작으로 세지 않는다(`cluster show` 와 같은 원칙).

    흐름 카드가 처음 도는 묶음에 먼저 보라는 dry-run 이 switch 전에 rc≠0 이어도 switch·종결이 각각 첫 시도
    rc 0 이면 통과한다. 실제 종결을 두 번 실행한 명령열은 여전히 재시도로 실패한다.
    """
    inv = "python3 .project_manager/tools"
    dry = f"{_op_command(inv, 'finish', 'T-0001')} --dry-run"
    assert _executed_board_ops(dry) == []
    clean = _clean_sequence(inv)
    switch_at = clean.index(_op_command(inv, "switch", "T-0001"))
    commands = [*clean[:switch_at], dry, *clean[switch_at:-1], dry, clean[-1]]
    ok, detail = _judge_commands(commands)
    assert ok, detail
    calls = [{"id": f"call-{index}", "command": command} for index, command in enumerate(commands)]
    ok, detail = _judge_first_try_rc0(calls, {call["id"]: index == switch_at
                                              for index, call in enumerate(calls)})
    assert ok, detail

    ok, detail = _judge_commands([*clean, _op_command(inv, "finish", "T-0001")])
    assert not ok and "finish 재시도 ×2" in detail


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
    commands = [*_clean_sequence(inv), _op_command(inv, "claim", "T-0001")]  # 재시도 → claim ×2.
    ok, detail = _judge_commands(commands)
    assert not ok and "claim 재시도" in detail


def test_judge_commands_flags_skipped_lifecycle_op():
    """기대 op(claim)를 건너뛰면 fail — done side-effect 만으론 통과 불가(MF-1 sensitivity).

    카드 커맨드를 다 안 쓰고 일부만 실행(여기선 claim 누락)한 채 파일 직접 조작으로 done/ 을
    만들어도, 판정은 op 누락으로 red 여야 한다(false-green 봉쇄의 핵심 sensitivity).
    """
    inv = "python3 .project_manager/tools"
    # claim 누락 — 카드 커맨드를 건너뜀(나머지 조작은 각 1회).
    commands = [c for c in _clean_sequence(inv) if _executed_board_ops(c) != ["claim"]]
    ok, detail = _judge_commands(commands)
    assert not ok and "claim 미실행" in detail
    assert "switch 미실행" not in detail and "cluster 미실행" not in detail


def test_judge_commands_flags_no_board_commands_used():
    """board 커맨드를 아예 안 쓰고 파일만 직접 조작(mv 등)하면 fail — 게이트 무력화 방어(MF-1).

    카드 사용성 게이트의 존재 이유 = "실 LLM 이 *카드 커맨드*를 첫 시도에 제대로 쓰는가". board
    커맨드 0 개면(예: `mv open/T.md done/T.md` 로 side-effect 만 위조) 반드시 red — 기대 op 전부
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
    """한 Bash 콜에 조작을 체이닝(`new && promote && … && finish`)해도 각각 정확-1회로 센다."""
    inv = "python3 .project_manager/tools"
    chained = " && ".join(_clean_sequence(inv))
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
        f'PM_SESSION_NAME=x {inv}/board.py new "t" --task demo-task ; {inv}/board.py promote T-1'
    ) == ["new", "promote"]


def test_judge_commands_flags_echoed_only_ops():
    """카드 커맨드를 실행 안 하고 echo 로 출력만 하면 op 집계가 안 차 fail — MF-2 false-green 봉쇄.

    substring 방식이면 echo 문자열의 board.py 를 실행 op 로 오인해 통과(=MF-1 봉함의 우회로) — 셸
    파싱으로 실행 argv 만 세므로 4 op 전부 미실행 red 여야 한다(에코 false-green sensitivity).
    """
    inv = "python3 .project_manager/tools"
    commands = [
        f'echo "{inv}/board.py new x --task demo-task"',
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
    stream-json 에는 claim 을 뺀 나머지 op 만 준다. _judge_claude 는 side-effect 만 보면 통과하겠지만,
    op 정확-1회 단언이 앞서 claim 미실행으로 red 를 세운다(side-effect 위조로 못 뚫는다).
    """
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")
    assert _tickets_in(tmp_path, "done"), "사전조건: done side-effect 가 심어져야(테스트 자체 정합)"

    inv = "python3 .project_manager/tools"
    # claim 누락 — 카드 커맨드 건너뜀(파일 직접 이동으로 done/ 위조).
    stdout = _fake_stream_json(
        [c for c in _clean_sequence(inv) if _executed_board_ops(c) != ["claim"]])
    proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    ok, detail = _judge_claude(tmp_path, proc)
    assert not ok and "claim 미실행" in detail


def test_judge_claude_rejects_done_side_effect_when_ops_only_echoed(tmp_path):
    """done/ side-effect 가 실재하고 기대 op 를 전부 echo 로 '출력'만 해도 _judge_claude fail — MF-2 봉쇄 증명.

    LLM 이 카드 커맨드를 실행하지 않고 stream-json Bash 로 `echo "…board.py claim…"` 만 하고 파일
    직접 조작으로 done/ 을 만든 상황. substring 방식이면 에코가 op 로 오집계돼 통과(=MF-2 버그) —
    셸 파싱 실행-argv 판정이 기대 op 전부 미실행으로 red 를 세운다(에코로 못 뚫는다).
    """
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")

    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([f"echo {shlex.quote(c)}" for c in _clean_sequence(inv)])
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
    stdout = _fake_stream_json([(c, True) for c in _clean_sequence(inv)])
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
    assert _rc_masking_problem(f"{inv}/board.py new x --task demo-task && {inv}/board.py promote T-1") is not None
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
    # 기대 op 모두 단일 실행하되 claim 만 `|| true` 로 실패 마스킹(tool_result 는 전부 rc0 성공으로 기록).
    stdout = _fake_stream_json([
        (f"{c} || true" if _executed_board_ops(c) == ["claim"] else c, False)  # 마스킹 — rc0 신뢰 불가.
        for c in _clean_sequence(inv)
    ])
    calls = _collect_bash_tool_calls(stdout)
    results = _collect_tool_results(stdout)
    ok, detail = _judge_first_try_rc0(calls, results)
    assert not ok and "마스킹" in detail and "claim" in detail


def test_judge_first_try_rc0_rejects_semicolon_chain(tmp_path):
    """`claim …; echo done`(세미콜론 체인·rc=마지막)도 red — 마스킹 클래스 종결(per-vector 아님)."""
    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json([
        (f"{c}; echo done" if _executed_board_ops(c) == ["claim"] else c, False)  # rc=echo(마지막).
        for c in _clean_sequence(inv)
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
    stdout = _fake_stream_json([(f"cd home && {c}", False) for c in _clean_sequence(inv)])
    calls = _collect_bash_tool_calls(stdout)
    results = _collect_tool_results(stdout)
    ok, detail = _judge_first_try_rc0(calls, results)
    assert ok, detail


def test_judge_claude_passes_full_op_sequence_with_done(tmp_path):
    """기대 op 정확-1회 + 첫 실행 rc0(is_error=False) + done side-effect 면 통과(정상 경로·non-vacuous)."""
    done_dir = tmp_path / ".project_manager" / "wiki" / "tickets" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "T-probe-001-card-usability-probe.md").write_text("done", encoding="utf-8")

    inv = "python3 .project_manager/tools"
    stdout = _fake_stream_json(_clean_sequence(inv))
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
#     (3) regression.LIVEGATE_RELEASE_PIN            (livegate record 의 수집 게이트 단일 진실)
#     (4) tests/test_board_livegate.py               (하드코딩 fake 출력/assert 여러 곳 — 7→9)
#     (5) templates/{claude_code,opencode}/.project_manager/tools/pm_engine/app/regression.py  (pm_update 전파·LIVEGATE_RELEASE_PIN)
_EXPECTED_LOCAL_RELEASE_TESTS = 2


def test_command_card_release_marker_count_is_pinned():
    """이 파일의 `release` 마커 테스트 수가 고정값과 일치 — 마커 소실/개명 시 게이트 누락 방어."""
    actual = _count_marked_tests(Path(__file__), "release")
    assert actual == _EXPECTED_LOCAL_RELEASE_TESTS, (
        f"이 파일 `release` 마커 수 {actual} != 기대 {_EXPECTED_LOCAL_RELEASE_TESTS} — 마커 "
        f"소실/개명 의심. 라이브 케이스를 의도적으로 늘렸다면 _EXPECTED_LOCAL_RELEASE_TESTS 와 "
        f"전역 커플드-pin 5곳(위 주석: test_release_wave._EXPECTED_RELEASE_TESTS·_RELEASE_TEST_FILES·"
        f"regression.LIVEGATE_RELEASE_PIN·tests/test_board_livegate.py·templates/*/pm_engine/app/regression.py)을 함께 갱신하라."
    )


def test_handoff_guides_name_task_trigger():
    """핸드오프 안내가 다음 세션 진입을 출력된 트리거(`--task` 포함)로 적는다([[T-0960]] 리뷰 F-004 · 자명한 수정).

    부트스트랩은 `--task` 없이 부르면 rc 2 로 거부하므로, 인자 없는 `/pm-bootstrap` 을 "직접" 실행하라는
    안내는 사실과 다르다. 핸드오프 스킬 운영 상세와 pm_role 의 같은 줄을 원본과 채택자 사본 전부에서 본다.
    """
    guides = [REPO / ".claude/skills/pm-handoff/references/operational-details.md",
              *sorted((REPO / "templates").glob("*/*/skills/pm-handoff/references/operational-details.md")),
              REPO / ".project_manager/wiki/pm_role.md",
              *sorted((REPO / "templates").glob("*/.project_manager/wiki/pm_role.md"))]
    assert len(guides) == 8, guides
    for path in guides:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "다음 세션은" in line]
        assert lines, path
        for line in lines:
            assert "--task" in line and "or 직접" not in line, (path, line)
