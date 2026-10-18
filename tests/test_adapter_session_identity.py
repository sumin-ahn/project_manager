"""출하 어댑터 카드·진입문서의 세션 정체성 서술은 엔진과 맞아야 한다 (ADR-0043 D5·T-0263·T-0962 가드).

배경 — 귀속 쓰기(new·claim·cluster new 등)의 정체성은 task 이름 하나다(T-0962). 엔진은 세션 토큰
(`<repo>_<N>`)에서 repo·prefix 를 분해하지 않으며, `--task` 없는 귀속 쓰기는 첫 쓰기 앞에서 멈춘다.
진입문서나 카드가 `--task` 없는 board 호출 줄을 지시하거나 슬롯 세션 정체성을 서술하면, 그 줄을
그대로 따른 PM 이 귀속 쓰기에서 막힌다.

이 파일은 그 불변식을 두 백스톱으로 lock-in 한다:
  - **board 귀속 호출 줄의 `--task`**(T-0962 AT-011) — 정체성 지시 표면에서
    `.project_manager/tools/board.py` 의 claim·new·cluster new 를 부르는 줄은 모두 `--task` 를 갖고,
    슬롯 세션·세션명 canonical 서술이 없다. 옛 감싼 홑 토큰 pm 가드(세션명을 `_repo_from_session`
    으로 파싱해 prefix 를 유도한다는 전제)를 이 규칙 하나로 교체했다.
  - **세션 식별 chain 서술이 stale 하지 않음**(ADR-0040·T-0263 B) — 옛 hostname-pid 폴백 표기·
    단일-lease 유도층 부재를 잡는다.

옛 `--session <값>` 재유입은 존재 가드([[test_skill_command_existence]]·T-0347)와
`test_flag_unification_parity` group 2 가 잡는다.
"""
from __future__ import annotations

import re
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]

# 정체성이 *지시되는* 표면만 스캔한다. RENDER_SCOPED_DIRS(test_adapter_free_form_free.py)
# 동형 어댑터 4곳 + ① canonical `.claude` 사본(이번 잔재가 사는 곳)만 — wiki/ 는 worker-라벨
# 이라 제외(모듈 docstring 참고).
_SCANNED_DIRS = (
    "templates/claude_code/.claude/agents",
    "templates/claude_code/.claude/skills",
    "templates/opencode/.opencode/agents",
    "templates/opencode/.claude/skills",
    "templates/opencode/.opencode/command",  # T-0674 사람 슬래시 표면
    ".claude/skills",   # ① canonical — pm_update --target 로 양 하네스 미러로 전파된다
    ".claude/agents",
)

# 양 하네스 root 진입문서 (@render 밖·어댑터 dir 아님) — 존재하는 것만 스캔.
_SCANNED_ENTRY_DOCS = (
    "templates/claude_code/CLAUDE.md",
    "templates/claude_code/CLAUDE.lite.md",
    "templates/opencode/AGENTS.md",
    "templates/opencode/AGENTS.lite.md",
)

# 세션 식별 chain 을 *서술*하는 출하 표면 — 진입문서 + tickets README 3벌. `_SCANNED_DIRS`
# (어댑터 카드)엔 tickets README 가 없어 B갈래(chain ↔ 코드 divergence)를 기계가 못 잡던 갭을
# 메운다(T-0263 리뷰 must-fix 2). AGENTS.lite.md 는 chain 을 서술하지 않아 제외.
_CHAIN_SURFACES = (
    "templates/claude_code/CLAUDE.md",
    "templates/claude_code/CLAUDE.lite.md",
    "templates/opencode/AGENTS.md",
    ".project_manager/wiki/tickets/README.md",
    "templates/claude_code/.project_manager/wiki/tickets/README.md",
    "templates/opencode/.project_manager/wiki/tickets/README.md",
)

# 코드(`session_name()`)가 정체성 해소에서 *제거*한 옛 자동 폴백 표기 — 하이픈형 `hostname-pid` 와
# 각괄호형 `<hostname>-<pid>` 둘 다. chain 서술에 이게 남으면 stale(divergence). 얕게 하이픈형만
# grep 하면 각괄호형을 놓친다(이번 리뷰 miss 의 원인) → 두 표기 모두 포착.
_HOSTNAME_PID = re.compile(r"<?hostname>?-<?pid>?")

# ADR-0040 단일-lease 유도층 문구 — chain 서술에 이게 있어야 pre-ADR-0040 stale 이 아니다.
_SINGLE_LEASE_PHRASE = "단일-lease"

def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCANNED_DIRS:
        d = REPO / rel
        if d.is_dir():
            files.extend(
                path
                for path in repo_owned_paths(REPO, rel, mode=OWNED)
                if path.suffix == ".md"
            )
    for rel in _SCANNED_ENTRY_DOCS:
        f = REPO / rel
        if f.is_file():
            files.append(f)
    return files


# AT-011 이 더 보는 정체성 지시 표면 — codex 진입문서·opencode PM 지시문·방법론 문서·수동 import 안내.
_TASK_BINDING_EXTRA_SURFACES = (
    "templates/codex/AGENTS.md",
    "templates/opencode/.opencode/pm-instructions.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/pm_role.md",
    "docs/manual-import.md",
)

# 귀속 쓰기 호출 줄 — 엔진 경로로 board.py 의 claim·new·cluster new 를 인자와 함께 부르는 줄
# (명령 이름만 인용한 산문은 호출이 아니다).
_BOARD_ATTRIBUTION_CALL = re.compile(
    r"\.project_manager/tools/board\.py\s+(?:claim|new|cluster\s+new)\s+[^`\s)]")

# 슬롯 세션 정체성 서술 — 엔진에 더 이상 없는 정체성 형태(T-0962). 슬롯 태그 log 검색·슬롯별 prefix
# 해소·슬롯별 pm_state·환경변수 귀속(claim 플래그 없음)도 지운 동작이다(T-0962 리뷰 F-001).
_SLOT_IDENTITY_PHRASES = ("슬롯 세션", "세션명 canonical", "슬롯 태그", "슬롯별 prefix",
                          "per-slot `pm_state", "claim 플래그 없음")


def _task_binding_surfaces() -> list[Path]:
    files = list(_scanned_files())
    for rel in (*_CHAIN_SURFACES, *_TASK_BINDING_EXTRA_SURFACES):
        path = REPO / rel
        assert path.is_file(), f"스캔 표면 부재(경로 stale): {rel}"
        if path not in files:
            files.append(path)
    return files


def _joined_lines(text: str) -> list[str]:
    """줄 끝 백슬래시로 이어 쓴 셸 명령을 한 줄로 이어 붙인다."""
    joined: list[str] = []
    pending = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def test_t0962_entry_docs_bind_claims_to_task():
    """[[T-0962]] AT-011 — 정체성 지시 표면의 board 귀속 호출 줄은 모두 `--task` 를 갖고 슬롯 세션 서술이 없다."""
    files = _task_binding_surfaces()
    calls = 0
    offenders: list[str] = []
    for path in files:
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for line in _joined_lines(text):
            if _BOARD_ATTRIBUTION_CALL.search(line):
                calls += 1
                if "--task" not in line:
                    offenders.append(f"{rel}: --task 없는 귀속 호출: {line.strip()!r}")
            for phrase in _SLOT_IDENTITY_PHRASES:
                if phrase in line:
                    offenders.append(f"{rel}: 슬롯 세션 정체성 서술 '{phrase}': {line.strip()!r}")
    assert calls, "scope sanity: 귀속 호출 줄을 하나도 찾지 못했다 — 스캔 표면이 stale 이다"
    assert not offenders, "\n  ".join(["정체성 지시 표면이 엔진(task 정체성)과 어긋난다:", *offenders])


def _chain_stale_findings(text: str) -> list[str]:
    """세션 식별 chain 서술이 `session_name()` 과 divergent(stale)한 findings 목록.

    (a) 옛 `hostname-pid`/`<hostname>-<pid>` 폴백 표기가 남아 있으면 — 코드는 이를 정체성
        해소에서 제거했다(미해소→None/fail-loud). 각괄호 표기도 잡는다(이번 miss 원인).
    (b) 단일-lease 유도층 문구(ADR-0040)가 없으면 — chain 이 pre-ADR-0040 stale.
    """
    findings: list[str] = []
    if _HOSTNAME_PID.search(text):
        findings.append("옛 hostname-pid 폴백 표기 잔존")
    if _SINGLE_LEASE_PHRASE not in text:
        findings.append("단일-lease 유도층 문구 부재(ADR-0040 chain stale)")
    return findings


def test_session_identity_chain_not_stale():
    """세션 식별 chain 서술이 `session_name()` 코드와 divergent(stale)하지 않은지 (ADR-0040·T-0263 B).

    B갈래 durable 백스톱 — `_SCANNED_DIRS`(어댑터 카드)가 tickets README 를 안 봐서 chain stale 을
    기계가 못 잡고 리뷰 반려까지 왔다. 진입문서 + tickets README 3벌(_CHAIN_SURFACES)의 chain 이
    옛 hostname-pid 폴백을 정체성 항으로 남기거나 단일-lease 유도층을 빠뜨리면 fail.
    """
    stale: list[str] = []
    for rel in _CHAIN_SURFACES:
        f = REPO / rel
        assert f.is_file(), f"chain surface 부재(경로 stale): {rel} — _CHAIN_SURFACES 갱신하라."
        for finding in _chain_stale_findings(f.read_text(encoding="utf-8")):
            stale.append(f"{rel}: {finding}")
    assert not stale, (
        "세션 식별 chain 이 session_name() 과 divergent(stale) — ADR-0040 canonical chain 으로\n"
        "정합하라(hostname-pid 폴백 제거·단일-lease 유도층 명시):\n  "
        + "\n  ".join(stale)
    )


def test_chain_stale_guard_classifies_known_forms():
    """chain stale 가드 판별 자기검증 — 합성 입력으로(가짜 게이트 방지·각괄호 표기 포착 실증).

    이번 리뷰 miss 의 핵심: `hostname-pid`(하이픈)만 grep 하면 실 표기 `<hostname>-<pid>`(각괄호)를
    놓친다. 그 각괄호 표기를 반드시 잡는지 못박는다.
    """
    # 잡아야 — 각괄호형(이번 miss 형태) · 하이픈형 둘 다.
    assert _HOSTNAME_PID.search("3. 자동 생성 `<hostname>-<pid>`")
    assert _HOSTNAME_PID.search("없으면 hostname-pid 자동")
    # 통과 — hostname/pid 를 정체성 항으로 안 쓰는 chain 서술.
    assert not _HOSTNAME_PID.search("활성 슬롯 lease 가 정확히 1개면 그 세션")
    # 단일-lease 유도층 문구 판별(있으면 pass·없으면 stale).
    assert _SINGLE_LEASE_PHRASE in "3. lease 가 정확히 1개면 그 세션 (단일-lease 유도)"
    assert _SINGLE_LEASE_PHRASE not in "1. $PM_SESSION_NAME 2. local.conf session="
    # 통합 판별 — stale chain(각괄호 폴백 + 단일-lease 부재)은 2 finding.
    stale_chain = "우선순위: 1. $PM_SESSION_NAME 2. local.conf 3. 자동 생성 `<hostname>-<pid>`"
    assert _chain_stale_findings(stale_chain) == [
        "옛 hostname-pid 폴백 표기 잔존",
        "단일-lease 유도층 문구 부재(ADR-0040 chain stale)",
    ]
    # canonical chain(단일-lease 유도·hostname 없음)은 clean.
    good_chain = "3. 활성 슬롯 lease 가 정확히 1개면 그 세션 (단일-lease 유도) 4. local.conf session="
    assert _chain_stale_findings(good_chain) == []
