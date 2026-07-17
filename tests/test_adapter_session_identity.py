"""출하 어댑터 카드·진입문서의 세션 정체성은 canonical 이어야 한다 (ADR-0043 D5·T-0263 가드).

배경 — **실패 모드가 기능적이다(cosmetic 아님)**: board.py 세션 식별은 세션명을
`_repo_from_session` 으로 파싱해 repo prefix 를 유도한다(`<repo>_<N>` → repo·ADR-0040 D3).
어댑터 카드나 진입문서가 세션 인자에 `pm` 처럼 canonical 형태가 아닌 리터럴 세션명을 *지시*하면
`_repo_from_session("pm")` → None → **prefix 유도가 조용히 죽는다(silent skip)**. 이건 오타가
아니라 multi-PM(M>1)에서 ticket id prefix 가 엉키는 기능적 결함이다 — ADR-0043 Context L27-28 이
바로 이 하드코딩 세션명(`pm`)을 지목했다.

이 파일은 그 불변식을 두 백스톱으로 lock-in 한다:
  - **감싼 홑 토큰 `pm` 리터럴 부재**(shape 무관 클래스 규칙·T-0263) — `--session pm`·prose 세션
    표기·claim 괄호 안의 감싼 pm 은 표기만 다른 같은 결함이다(canonical 아닌 세션명 → 유도 죽음).
    유일 예외 = opencode primary agent 이름(구조적 allow-list).
  - **세션 식별 chain 서술이 stale 하지 않음**(ADR-0040·T-0263 B) — 옛 hostname-pid 폴백 표기·
    단일-lease 유도층 부재를 잡는다.

**흡수 노트(T-0347)**: 옛 `--session <값>` canonical *값* 가드는 v1.2.0 이 `--session` 을
제거하며(ADR-0057) 스캔 대상 0 = green 이지만 아무것도 안 지키는 공허 통과가 됐다. 그 값 가드가
지키던 불변식(하드코딩 세션명 재유입 차단)은 **3중 백스톱**이 이어받는다 — 흡수처를 하나로
좁혀 읽지 말 것:
  1. **존재 가드**([[test_skill_command_existence]]·T-0347) — 스킬 md 에 `--session` 을 다시
     쓰면 board 파서에 없는 플래그라 존재 검사에서 먼저 걸린다.
  2. **`test_flag_unification_parity` group 2** — actor `--session`/`--worktree-slot`/
     `--session-num` 토큰이 넓은 shipped .md 표면(skills·agents·root docs)에 재등장하면 red.
  3. **이 파일의 잔존 wrapped-pm 리터럴 가드**(`test_shipped_surfaces_no_bare_pm_session_literal`)
     — `--session` 없이도 감싼 홑 토큰 `pm`(세션값 리터럴)이 새 들어오면 잡는다.
그래서 이 파일의 `--session <값>` 값 스캔·자기검증(`_session_offenders`/`_SESSION_ARG`)만
제거하고, 위 3-①은 새 파일로·3-②는 기존 유지·3-③과 chain 백스톱은 이 파일에 남긴다.

스코프 밖(의도적): 진입문서의 `# 예: --session myproj_1` 처럼 canonical 로 채워진 예시의 값
판별은 위 흡수처(존재 가드)·T-0262/0263 값 표면 몫이지 이 파일이 아니다.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 정체성이 *지시되는* 표면만 스캔한다. RENDER_SCOPED_DIRS(test_adapter_free_form_free.py)
# 동형 어댑터 4곳 + ① canonical `.claude` 사본(이번 잔재가 사는 곳)만 — wiki/ 는 worker-라벨
# 이라 제외(모듈 docstring 참고).
_SCANNED_DIRS = (
    "templates/claude_code/.claude/agents",
    "templates/claude_code/.claude/skills",
    "templates/opencode/.opencode/agents",
    "templates/opencode/.opencode/command",
    ".claude/skills",   # ① canonical 사본 — pm_update 로 templates/claude_code 로 전파된다
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

# 자기검증 합성 입력용 결함 세션값. 이 가드 파일이 DoD residue grep(하드코딩 세션 인자 잔재
# 체크)에 자기매치되지 않도록 리터럴은 런타임 조립한다 (test_terminology.py 의 `"우"+"산"` 동류).
_DEFECT_VALUE = "pm"

# 클래스 규칙(shape 무관) — 정체성 지시 표면의 출하 .md 에서 백틱/따옴표로 감싼 홑 토큰 pm 은
# 세션값 리터럴로 오인·재유입되는 벡터라 금지한다. `--session pm`(T-0262)·prose 세션 표기·
# 괄호형(claim 안의 감싼 pm) 은 표기가 다를 뿐 같은 결함(=canonical 아닌 세션명)이라 shape 별
# 정규식을 쌓지 않고 *한 규칙*으로 닫는다(ADR-0043 D5·T-0263·PM 53 지시). 값형 `--session=pm` 의
# 플래그 존재 자체는 스킬 md ↔ CLI 존재 가드(test_skill_command_existence·T-0347)가 먼저 잡는다
# (구 `--session <값>` 값 스캔 흡수처). 패턴·자기검증 입력의 결함 리터럴은
# _DEFECT_VALUE 로 조립해 이 파일이 출하-표면 residue grep 에 잡히지 않게 한다.
#   매치: 감싼 홑 토큰 — 백틱/작은따옴표/큰따옴표로 pm 을 감싼 것.
#   비매치: 식별자는 감싸도 홑 토큰이 아니다(pm-wave-claim·pm_role·pm_update·pm.md 는 닫는
#          래퍼가 pm 바로 뒤에 없어 자연 제외)·대문자 PM·맨몸 pm(래핑 없음)·canonical <repo>_<N>.
_D = re.escape(_DEFECT_VALUE)
_WRAPPED_PM = re.compile("`" + _D + "`|'" + _D + "'|\"" + _D + "\"")

# 예외(allow-list) 마커 — 감싼 pm 이 세션값이 아니라 opencode primary agent 이름임을 가리키는
# 구조적 조건. 문자열 통째 하드코딩 대신 '에이전트 문맥' 을 나타내는 토큰의 등장으로 판별한다.
_AGENT_MARKERS = ("agent", "에이전트")


def _pm_literal_reason(line: str) -> str | None:
    """감싼 pm 이 이 줄에서 정당하면 사유 문자열, offender 면 None (default-deny).

    예외(allow-list)는 **단 하나** — opencode primary agent 이름으로서의 pm. 세션값이 아니라
    에이전트 식별자다(`.opencode/agents/pm.md` 실재·`opencode run --agent pm`). 판별은 문자열을
    통째 하드코딩하지 않고, 같은 줄에 에이전트 문맥 마커(_AGENT_MARKERS)가 등장하는 **구조적
    조건**으로 한다 — 세션-지시 줄(claim 안의 감싼 pm·prose 세션 표기·자유형 반례)엔 이 마커가
    없다. 예외를 shape 별로 늘리지 않는다(예외가 늘수록 가드가 죽는다·PM 53). 잔여 위험: 마커를
    포함한 줄에서 세션값 pm 을 쓰면 통과하나(드묾) — 그 경우 문서를 고쳐 마커 없는 줄로 분리한다
    (반례는 예외로 두지 말고 정정하는 원칙).
    """
    low = line.lower()
    if any(marker in low for marker in _AGENT_MARKERS):
        return "opencode primary agent 이름 (세션값 아님)"
    return None


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCANNED_DIRS:
        d = REPO / rel
        if d.is_dir():
            files.extend(sorted(d.rglob("*.md")))
    for rel in _SCANNED_ENTRY_DOCS:
        f = REPO / rel
        if f.is_file():
            files.append(f)
    return files


def _pm_literal_offenders(text: str) -> list[str]:
    """text 안 감싼 홑 토큰 pm 중 allow-list 예외가 아닌 offender 줄 목록(줄 원문·strip).

    클래스 규칙(_WRAPPED_PM)이 감싼 pm 을 잡고, `_pm_literal_reason` 이 정당한 예외(opencode
    agent 이름)를 걸러낸다. 식별자 안의 pm·대문자 PM·canonical <repo>_<N> 은 애초에 _WRAPPED_PM
    에 안 걸린다(패턴 docstring 참고).
    """
    offenders: list[str] = []
    for line in text.splitlines():
        if _WRAPPED_PM.search(line) and _pm_literal_reason(line) is None:
            offenders.append(line.strip())
    return offenders


def test_shipped_surfaces_no_bare_pm_session_literal():
    """출하 어댑터 카드·진입문서에 감싼 홑 토큰 pm(세션값 리터럴)이 없는지 (ADR-0043 D5·T-0263).

    클래스 규칙 — shape 무관. `--session pm`(T-0262 인자형)·prose 세션 표기·claim 괄호 안의 감싼
    pm 은 표기만 다른 같은 결함이다(canonical 아닌 세션명 → `_repo_from_session` 미매치 → prefix
    유도 silent skip). shape 별 정규식을 늘리는 대신 "감싼 홑 토큰 pm 금지 + 구조적 allow-list"
    한 규칙으로 재유입을 봉쇄한다. 유일 예외 = opencode primary agent 이름(_pm_literal_reason).
    """
    files = _scanned_files()
    assert files, (
        "scope sanity: 스캔 대상 .md 를 0개 찾음 — 경로 상수(_SCANNED_DIRS/_SCANNED_ENTRY_DOCS)"
        " 가 stale 이다. 실 트리에 맞춰 갱신하라."
    )
    offenders: list[str] = []
    for f in files:
        for line in _pm_literal_offenders(f.read_text(encoding="utf-8")):
            offenders.append(f"{f.relative_to(REPO).as_posix()}: {line!r}")
    assert not offenders, (
        "감싼 홑 토큰 pm(세션값 리터럴) 잔존 — canonical 아닌 세션명이라 prefix 유도가 조용히\n"
        "죽는다(ADR-0043 D5). 세션 표기는 `<repo>_<N>`(솔로 M=1 은 생략)로, agent 이름 참조면\n"
        "같은 줄에 'agent' 문맥을 명시하라(반례는 예외로 두지 말고 정정):\n  "
        + "\n  ".join(offenders)
    )


def test_pm_literal_guard_classifies_known_lines():
    """클래스 규칙 판별 자기검증 — 실 트리와 무관한 합성 줄로(가짜 게이트 방지·PM 53 요구 1·2·3).

    catch: 세션값 pm — claim 괄호형(1)·prose 세션 표기·자유형 반례(3-이전형)·prose 세션.
    pass : opencode agent 이름 pm(2 · 'agent'/'에이전트' 마커)·대문자 PM·식별자 안의 pm
           (pm-wave-claim·pm-dev-delegate·pm_role·pm_update·pm.md)·canonical <repo>_<N>.
    (결함 리터럴은 _DEFECT_VALUE 로 조립 — 이 파일이 출하-표면 residue grep 에 자기매치 안 되게.)
    """
    d = _DEFECT_VALUE
    # catch (1) — claim 괄호 안의 감싼 pm = 세션값 리터럴(마커 없음).
    assert _pm_literal_offenders(f"- **사전조건**: ticket claim(`{d}`)·depends_on done") \
        == [f"- **사전조건**: ticket claim(`{d}`)·depends_on done"]
    # catch — prose 세션 표기(감싼 pm + 세션어). 백틱·작은따옴표 둘 다.
    assert _pm_literal_offenders(f"- ticket 이미 claim (`{d}` 세션명).") \
        == [f"- ticket 이미 claim (`{d}` 세션명)."]
    assert _pm_literal_offenders(f"'{d}' 세션 을 쓴다") == [f"'{d}' 세션 을 쓴다"]
    # catch (3-이전형) — 자유형 반례가 리터럴을 남기면 잡힌다(→ 문서를 고쳐 리터럴 제거).
    assert _pm_literal_offenders(f"(`{d}` 같은 자유형은 M>1 에서 prefix 유도가 죽음)") \
        == [f"(`{d}` 같은 자유형은 M>1 에서 prefix 유도가 죽음)"]
    # catch — agent 문맥 마커 없는 줄의 감싼 pm(예: 반례/폴백 서술에서 리터럴만 남긴 경우).
    assert _pm_literal_offenders(f"미검증이므로 `{d}` 이 안 떠도 부트스트랩") \
        == [f"미검증이므로 `{d}` 이 안 떠도 부트스트랩"]

    # pass (2) — opencode primary agent 이름. 같은 줄 'agent' 마커로 예외 허용.
    assert _pm_literal_offenders(f"- **PM(orchestrator) = `{d}` primary agent (1차)**") == []
    assert _pm_literal_offenders(f"1차 = `{d}` primary (`.opencode/agents/pm.md` · mode: primary)") == []
    assert _pm_literal_offenders(f"`{d}` primary agent 가 안 떠도 부트스트랩이 안 깨지게") == []
    # pass — '에이전트'(한글 마커)도 허용.
    assert _pm_literal_offenders(f"폴백 = `{d}` 에이전트가 안 떠도") == []
    # pass — 대문자 역할표기(감싸도 소문자 아님)·맨몸 pm(래핑 없음).
    assert _pm_literal_offenders("PM 세션 은 역할 표기다") == []
    assert _pm_literal_offenders("pm 세션명 (맨몸·래핑 없음)") == []
    # pass — 식별자는 감싸도 홑 토큰 아님(닫는 래퍼가 pm 바로 뒤에 없음).
    assert _pm_literal_offenders("`pm-wave-claim` 통과 후 세션 정리") == []
    assert _pm_literal_offenders("`pm-dev-delegate` 로 위임") == []
    assert _pm_literal_offenders("`pm_role` 규칙 · `pm_update` 전파 · `pm.md` 카드") == []
    # pass — canonical placeholder(pm 부재).
    assert _pm_literal_offenders("`<repo>_<N>` 세션 로 전달") == []


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
