"""출하 어댑터 카드·진입문서의 세션 정체성은 canonical 이어야 한다 (ADR-0043 D5·T-0262 가드).

배경 — **실패 모드가 기능적이다(cosmetic 아님)**: board.py 세션 식별은 세션명을
`_repo_from_session` 으로 파싱해 repo prefix 를 유도한다(`<repo>_<N>` → repo·ADR-0040 D3).
어댑터 카드나 진입문서가 세션 인자에 `pm` 처럼 canonical 형태가 아닌 리터럴 세션명을 *지시*하면
`_repo_from_session("pm")` → None → **prefix 유도가 조용히 죽는다(silent skip)**. 이건 오타가
아니라 multi-PM(M>1)에서 ticket id prefix 가 엉키는 기능적 결함이다 — ADR-0043 Context L27-28 이
바로 이 하드코딩 세션명(`pm`)을 지목했다.

이 가드는 그 불변식을 lock-in 한다 — 정체성이 *지시되는* 표면(출하 어댑터 카드 + 양 하네스
root 진입문서 + ① canonical `.claude` 사본)의 `--session <값>` 이 canonical 이 아닌 리터럴이면
fail. 판별에는 결함의 원인 함수인 `_repo_from_session` 을 그대로 써서(placeholder 는 별도
verbatim 수용) "prefix 유도가 죽는 값" 을 정확히 잡는다. 미래에 하드코딩 세션명이 재유입되면
여기서 걸린다 (feature-ship-needs-fresh-adopter-gate 클래스).

스코프 밖(의도적):
  - `wiki/pm_playbook.md`·`wiki/tickets/README.md` 의 `--session session-<X>`/`session-A` 는
    *사용자가 직접 여는 구현(worker) 세션* 의 임의 라벨이지 PM 정체성이 아니다 → 제외
    (넣으면 false positive·T-0262 §결정).
  - 진입문서의 `# 예: --session myproj_1` 은 canonical 형태로 *채워진* 예시라
    `_repo_from_session("myproj_1")` → "myproj" 로 유도가 살아 있다 → 통과(offender 아님).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# canonical placeholder — 그 자체는 채워질 자리표시라 `_repo_from_session` 이 유도 못 하지만
# (끝 마디 `<N>` 이 숫자 아님) 지시로서는 정답. verbatim 수용한다.
CANONICAL_PLACEHOLDER = "<repo>_<N>"

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

# `--session <값>` 추출. `--session` 직후 공백 **또는 `=`** 를 요구 — argparse 가 `--session foo`
# 와 `--session=foo` 를 동등 수용하므로 등호형도 실 표기다. `--session-seq`·`--session-num`·
# `--session-id`(별개 인자)는 직후 문자가 `-` 라 `[=\s]` 미매치 → 무매치. `` `--session` `` (백틱
# 으로 닫힌 prose)도 무매치.
_SESSION_ARG = re.compile(r"--session[=\s]+(\S+)")

# 캡처 토큰 주변에 붙는 백틱·문장부호 제거(코드스팬 `--session <repo>_<N>` 등).
_WRAP = "`\"'.,;:)]}·"

# 자기검증 합성 입력용 결함 세션값. 이 가드 파일이 DoD residue grep(하드코딩 세션 인자 잔재
# 체크)에 자기매치되지 않도록 리터럴은 런타임 조립한다 (test_terminology.py 의 `"우"+"산"` 동류).
_DEFECT_VALUE = "pm"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _session_offenders(text: str, repo_from_session) -> list[str]:
    """text 안 `--session <값>` 중 canonical 이 아닌 하드코딩 리터럴 값 목록.

    수용:
      - canonical placeholder `<repo>_<N>` (지시 자리표시·verbatim),
      - `_repo_from_session` 이 repo 를 유도하는 채워진 인스턴스(`myproj_1` → "myproj").
    거부(= 반환):
      - `_repo_from_session` 미매치 리터럴(`pm`·`session-B`) — prefix 유도가 죽는 값.
    """
    offenders: list[str] = []
    for line in text.splitlines():
        for match in _SESSION_ARG.finditer(line):
            value = match.group(1).strip(_WRAP)
            if not value or value == CANONICAL_PLACEHOLDER:
                continue
            if repo_from_session(value) is not None:
                continue
            offenders.append(value)
    return offenders


def test_shipped_adapter_cards_use_canonical_session_identity():
    """출하 어댑터 카드·진입문서의 모든 `--session <값>` 이 canonical 인지 (ADR-0043 D5).

    **실패 모드는 기능적**이다: 하드코딩 세션명(세션 인자 값 `pm`) → `_repo_from_session` 미매치
    → prefix 유도 silent skip (ADR-0043 Context 가 지목한 결함). 판별에 결함의 원인 함수를
    그대로 써서 "prefix 유도가 죽는 값" 을 잡는다.
    """
    files = _scanned_files()
    assert files, (
        "scope sanity: 스캔 대상 .md 를 0개 찾음 — 경로 상수(_SCANNED_DIRS/_SCANNED_ENTRY_DOCS)"
        " 가 stale 이다. 실 트리에 맞춰 갱신하라."
    )
    repo_from_session = _load_board()._repo_from_session
    offenders: list[str] = []
    for f in files:
        for value in _session_offenders(f.read_text(encoding="utf-8"), repo_from_session):
            offenders.append(f"{f.relative_to(REPO).as_posix()}: --session {value}")
    assert not offenders, (
        "canonical 이 아닌 하드코딩 세션명 잔존 — `_repo_from_session` 미매치로 prefix 유도가\n"
        "조용히 죽는다(ADR-0043 D5). `--session <repo>_<N>` (솔로 M=1 이면 생략 가능)로 정정하라:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_classifies_known_session_values():
    """가드 판별 로직 자기검증 — 실 트리와 무관한 합성 입력으로.

    이게 없으면 판별이 *무엇이든* 통과시켜도(offenders 항상 빈) 위 테스트가 green 이라 가짜
    게이트가 된다. 결함값(`pm`·`session-B`)은 잡고, placeholder·채워진 canonical·백틱 래핑·
    별개 인자(`--session-seq`/`--session-num`/`--session-id`)·prose `` `--session` `` 은 통과해야
    한다. 공백형·등호형(`--session foo`·`--session=foo`) 둘 다 argparse 실 표기라 커버한다.
    """
    repo_from_session = _load_board()._repo_from_session
    # 잡아야 하는 것 — _repo_from_session 미매치 리터럴. (결함 세션값은 위 _DEFECT_VALUE 로
    # 런타임 조립 — 이 파일이 DoD residue grep 에 자기매치되지 않게.) 공백형·등호형 둘 다.
    assert _session_offenders(
        f"board.py claim T-1 --session {_DEFECT_VALUE}", repo_from_session
    ) == [_DEFECT_VALUE]
    assert _session_offenders(
        f"board.py claim T-1 --session={_DEFECT_VALUE}", repo_from_session
    ) == [_DEFECT_VALUE]
    assert _session_offenders("--session session-B", repo_from_session) == ["session-B"]
    # 통과 — placeholder / 채워진 canonical / 백틱 래핑 (공백형).
    assert _session_offenders("claim T-1 --session <repo>_<N>", repo_from_session) == []
    assert _session_offenders("# 예: --session myproj_1", repo_from_session) == []
    assert _session_offenders("`--session <repo>_<N>` 으로 전달", repo_from_session) == []
    # 통과 — 등호형도 placeholder/채워진 canonical 은 수용.
    assert _session_offenders("--session=<repo>_<N>", repo_from_session) == []
    assert _session_offenders("--session=myproj_1", repo_from_session) == []
    # 통과 — 별개 인자(직후 `-` 라 `[=\\s]` 미매치)·prose(값 없음).
    assert _session_offenders("--session-seq 19 --wave-summary x", repo_from_session) == []
    assert _session_offenders("--session-num 5", repo_from_session) == []
    assert _session_offenders("--session-id abc", repo_from_session) == []
    assert _session_offenders("우선순위: `--session` 인자 > $PM_SESSION_NAME", repo_from_session) == []
