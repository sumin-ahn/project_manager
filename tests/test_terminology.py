"""T-0098 — 폐기 용어 잔존 가드.

ADR-0016 이 solo/team/'우산'/orchestrator 4모드를 **multi-PM(N 세션 × M repo)** 한 개념으로
통합하며 '우산' 을 multi-PM 의 M>1 케이스로 흡수했다(orchestrator→relay 는 ADR-0020). 그 후
용어 sweep 이 누락돼 코드/docs 전반에 '우산'(114건)이 잔존했다(T-0098 에서 제거). 이 가드는
LIVE 코드·동기 methodology 문서에 폐기 용어가 *다시 새어드는* 회귀를 막는다.

**historical 은 의도적으로 제외** — `log/`·`raw/spikes/`(sealed)·`tickets/done/`·`decisions/`
(ADR 의 '옛 우산' 설명)은 term-of-the-time 기록이라 immutable(ADR-0010 정신). 이 가드는 *현재-
기술* 표면(엔진 코드·테스트·pm_role·skill·어댑터 진입)만 본다.

재발 교훈(메모리): 재발하는 용어/규칙은 지식이 아니라 테스트로 못박는다.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]

# 폐기 용어 (ADR-0016) — LIVE 표면에 0 이어야 한다.
# 리터럴 분할: 이 가드 파일 자신이 자기 검사에 안 걸리게.
_RETIRED_TERM = "우" + "산"  # 한국어
# 영어 표면도 동일 폐기 용어(T-0172) — placeholder/지역변수/함수명/fixture 경로에 잔존했었다.
# 대소문자 무관 검출(소문자화 후 비교) — `Umbrella`·`UMBRELLA` 도 잡는다.
_RETIRED_TERM_EN = "umb" + "rella"

# 자기 자신은 제외(이 파일은 폐기 용어를 *논의*하므로 정당히 포함).
_SELF = Path(__file__).name

# 설치자가 읽는 ignore 산문은 문서 확장자가 아니지만 출하 surface다. 두 용어 가드가 이
# 목록을 공유해 한쪽만 제외하는 틈을 만들지 않는다.
_SHIPPED_IGNORE_PROSE = (
    ".project_manager/.gitignore",
    "templates/claude_code/.project_manager/.gitignore",
    "templates/opencode/.project_manager/.gitignore",
    "templates/codex/.project_manager/.gitignore",
    # 루트 판도 세 하네스 전부에서 출하된다(pm_import 실측). 폐기 용어가 실제로
    # 잔존했던 자리가 여기다 — 빠뜨리면 되살린 잔재를 어느 가드도 못 잡는다.
    ".gitignore",
    "templates/claude_code/.gitignore",
    "templates/opencode/.gitignore",
    "templates/codex/.gitignore",
)


def _live_files() -> list[Path]:
    globs = [
        ".project_manager/tools/*.py",
        "tests/*.py",
        "templates/claude_code/.project_manager/tools/*.py",
        "templates/opencode/.project_manager/tools/*.py",
    ]
    files: list[Path] = []
    for g in globs:
        files += [Path(p) for p in glob.glob(str(REPO / g))]
    files += [
        REPO / ".project_manager/wiki/pm_role.md",
        REPO / ".claude/skills/pm-bootstrap/SKILL.md",
        REPO / "templates/claude_code/.project_manager/wiki/pm_role.md",
        REPO / "templates/claude_code/.claude/skills/pm-bootstrap/SKILL.md",
        REPO / "templates/opencode/.project_manager/wiki/pm_role.md",
        REPO / "templates/opencode/.claude/skills/pm-bootstrap/SKILL.md",  # ADR-0065 단일 소비 미러
        REPO / "templates/opencode/.opencode/command/pm-bootstrap.md",  # T-0674 슬래시 사본
        REPO / "templates/claude_code/pm-config.sh",
        REPO / "templates/opencode/pm-config.sh",
        # `.cmd` Windows 등가물 — `.sh` forwarder 의 짝(동형). manifest 밖 facade 라 `--target`
        #   전파 안 됨 → `.sh` 만 보면 `.cmd` 의 잔존을 못 잡는 false-negative (T-0172 must-fix).
        REPO / "templates/claude_code/pm-config.cmd",
        REPO / "templates/opencode/pm-config.cmd",
        # engine.manifest 3곳 + 루트 pm-config 파사드 (T-0171 범위 확장): 폐기 용어 '우산'이
        #   여기 잔존해도 위 glob/list 가 안 봐서 살아남았다. README.md 는 의도적으로 제외 —
        #   "옛 '우산'=…재정의·ADR-0016" 은 용어 *재정의 설명*이라 historical-context 정당.
        REPO / ".project_manager/engine.manifest",
        REPO / "templates/claude_code/.project_manager/engine.manifest",
        REPO / "templates/opencode/.project_manager/engine.manifest",
        # ① worktree 루트 파사드 — 위 list 는 templates/*/pm-config.sh 만 있고 루트 누락이었다.
        #   존재하는 파사드만 검사(미존재는 f.exists() 필터로 자동 제외).
        REPO / "pm-config.sh",
        REPO / "pm-import.sh",
        REPO / "pm-update.sh",
        # 루트 `.cmd` Windows 등가물 (T-0172) — `.sh` 와 동형. 존재하는 것만(f.exists() 필터).
        REPO / "pm-config.cmd",
        REPO / "pm-import.cmd",
        REPO / "pm-update.cmd",
        *(REPO / relpath for relpath in _SHIPPED_IGNORE_PROSE),
    ]
    return [f for f in files if f.exists() and f.name != _SELF]


def test_no_retired_umbrella_term_in_live_surface():
    """LIVE 엔진 코드·동기 docs·어댑터 진입에 폐기 용어('우산') 0 (ADR-0016·T-0098)."""
    offenders = []
    for f in _live_files():
        if _RETIRED_TERM in f.read_text(encoding="utf-8"):
            offenders.append(str(f.relative_to(REPO)))
    assert not offenders, (
        f"폐기 용어 '{_RETIRED_TERM}' 잔존 — ADR-0016 후 multi-PM 으로 (historical 제외): {offenders}"
    )


def test_no_retired_umbrella_term_english_in_live_surface():
    """LIVE 표면에 영어 폐기 용어('umbrella') 0 (ADR-0016·T-0172).

    한국어 '우산' sweep(T-0098) 후에도 영어 'umbrella' 가 pm_bootstrap.py 지역변수
    (umbrella_lean/alloc)·pm-config.{sh,cmd}/README placeholder(<umbrella>)·테스트
    식별자에 잔존했다. 대소문자 무관 검출. `_live_files` 는 `.sh`/`.cmd` facade 페어를
    동형으로 스캔한다 — `.cmd`(Windows 짝)만 빠뜨리면 false-negative (T-0172 must-fix).
    README.md 는 `_live_files` 가 의도적으로 제외하므로 이 가드 범위 밖이다(line327 한국어
    historical 재정의 + placeholder 둘 다 — README 전체 제외는 T-0171 의 한계, 영어
    placeholder 는 T-0172 에서 손으로 sweep 했다).
    """
    offenders = []
    for f in _live_files():
        if _RETIRED_TERM_EN in f.read_text(encoding="utf-8").lower():
            offenders.append(str(f.relative_to(REPO)))
    assert not offenders, (
        f"폐기 용어 '{_RETIRED_TERM_EN}' 잔존 — ADR-0016 후 multi-PM 으로 (historical 제외): {offenders}"
    )


def test_retired_term_scope_includes_shipped_ignore_prose():
    """출하 .gitignore의 산문 주석은 두 폐기 용어 가드 모두의 검사 대상이다."""
    relpaths = {path.relative_to(REPO).as_posix() for path in _live_files()}
    assert {
        *_SHIPPED_IGNORE_PROSE,
    } <= relpaths


# ── T-0268: 'carry' 폐기 용어 가드 (deferred/전달 → 후속·이월) ────────────────
# 규율(메모리 [[avoid-carry-term]]): 'carry'(영어 동사 "log entry 가 …"·절 명칭 "장기 …")를
# 쓰지 않고 '후속/이월' 로 쓴다. '우산'(위)과 달리 gray-zone 이 없다(safety-guarantee 아님) —
# canonical live source 에 무조건 0. 검색 리터럴은 분할("car"+"ry") — 위 `"우"+"산"` 동류(_SELF
# 제외와 이중 방어).
_CARRY_TERM = "car" + "ry"

# 스코프 = canonical **live** 엔진/방법론 source 만.
#  · templates/**(tools·wiki 사본): pm_update 가 canonical 에서 byte-동기(canonical-clean ⟹
#    templates-clean·pm_update --dry-run drift-0 게이트가 전파 강제)라 제외 — 안 그러면 canonical
#    편집과 전파 사이에 false-red. 어댑터 카드(.claude/.opencode)도 별도 편집 표면이라 그쪽
#    정리+전파 후 PM 이 편입(T-0268 follow-up).
#  · wiki 는 **live methodology 파일만** 본다. log/·tickets/(done·claimed·open·blocked)·
#    raw/spikes/(sealed)·ideas/·specs/·decisions/ 는 ADR-0010 immutable 기록/작업항목이라 흔한
#    영어 동사가 자연히 등장해도 고칠 수 없다 → 형제 _live_files 가 historical 을 안 보는 정신과
#    동일하게 제외한다. board.md 는 파생 대시보드(git-untracked·ticket 제목 유입)라 제외. 스캐폴드
#    (_template.md)·domain 지식 페이지는 live methodology 라 포함.
_CARRY_WIKI_FILES = (
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/pm_state.template.md",
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/tickets/_template.md",     # ticket 스캐폴드(빈 틀·done 본문 아님)
    ".project_manager/wiki/raw/spikes/_template.md",  # spike 스캐폴드(sealed spike 아님)
)
_CARRY_WIKI_GLOBS = (
    ".project_manager/wiki/domain/*.md",       # domain 지식 페이지 + _template
    ".project_manager/wiki/_template/**/*.md",  # 방법론 템플릿 트리 (있으면)
)


def _canonical_source_files() -> list[Path]:
    files: list[Path] = []
    for g in (".project_manager/tools/*.py", "tests/*.py"):
        files += [Path(p) for p in glob.glob(str(REPO / g))]
    for g in _CARRY_WIKI_GLOBS:
        if "**" not in g:
            files += [Path(p) for p in glob.glob(str(REPO / g))]
            continue
        subtree = g.split("/**", 1)[0]
        files += [
            path
            for path in repo_owned_paths(REPO, subtree, mode=OWNED)
            if path.suffix == ".md"
        ]
    files += [REPO / rel for rel in _CARRY_WIKI_FILES]
    return [f for f in files if f.is_file() and f.name != _SELF]


def test_canonical_source_files_scans_every_carry_wiki_glob(
        tmp_path, monkeypatch):
    """wiki glob 튜플에 후속 항목이 추가돼도 인덱스 하드코딩 없이 모두 스캔한다."""
    third_glob_file = tmp_path / ".project_manager" / "wiki" / "third" / "live.md"
    third_glob_file.parent.mkdir(parents=True)
    third_glob_file.write_text("live methodology\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    monkeypatch.setitem(
        globals(),
        "_CARRY_WIKI_GLOBS",
        (
            ".project_manager/wiki/first/*.md",
            ".project_manager/wiki/second/*.md",
            ".project_manager/wiki/third/*.md",
        ),
    )
    monkeypatch.setitem(globals(), "_CARRY_WIKI_FILES", ())

    assert third_glob_file in _canonical_source_files()


def test_no_carry_term_in_canonical_source():
    """canonical 엔진/방법론 source 에 폐기 용어 'carry' 0 (후속·이월로 통일·T-0268).

    영어 동사 'carry'(예 'log entry 가 carry')·절 명칭('장기 carry') 모두 후속/이월 로 바꾼다.
    '우산'과 달리 gray-zone 이 없다(safety-guarantee 아님). 대소문자 무관.
    """
    offenders = []
    for f in _canonical_source_files():
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _CARRY_TERM in line.lower():
                offenders.append(f"{f.relative_to(REPO).as_posix()}:{lineno}")
    assert not offenders, (
        f"폐기 용어 '{_CARRY_TERM}' 잔존 — '후속/이월' 로 정정하라 (T-0268): {offenders}"
    )


# ── T-0299: slot-key 표기 가드 (모호한 <slot>/<N> → 명시형 <repo>_<N>) ──────────
# per-slot pm_state 경로는 명시형 `slots/<repo>_<N>/pm_state.md`(예 `slots/project_manager_1/`)로
# 가리킨다. 모호한 `slots/<slot>`(placeholder)·`slots/<N>`(= `--slot <N>` 탓에 "숫자 N"으로 오독)은
# multi-slot PM 이 `.local/slots/<N>/` 를 헛찾게 한다(PM 63 실측). 명시형 = 코드 display 경로
# (`pm_bootstrap._pm_state_display_path` → `slots/{repo}_{n}/`)·spike 와 정합. 재발을 기계로 못박는다
# ([[T-0098]] terminology 가드 선례·재발 용어/규칙은 지식 아닌 테스트로).
#
# 스코프 = 사람이 읽는 가이드 문서와 출하 `.gitignore` 산문. 엔진 `.py` 는 제외 — 코드는 T-0298 소관이고
# `pm_handoff.py` 의 `slots/<N>` 은 divergent-bare(`--slot 4` verbatim) 마이그(T-0201)를 *설명*하는
# 정당한 등장이다. `.claude/skills/*/SKILL.md`와 canonical에서 기계 생성한
# opencode `.opencode/command/*.md`도
# 포함 — 그 사본 편집은 PM 직접(harness: 백그라운드 subagent 는 `.claude/` 쓰기 불가). REPO(=① worktree)
# 밖 사본(② live pm_role/SKILL·②-owned architecture.md)은 이 스코프 밖(별도 sweep).
# 리터럴 분할("slots/<"+…): 이 가드 파일 자신이 자기 검사에 안 걸리게(_SELF 제외와 이중 방어).
_SLOT_KEY_BARE = ("slots/<" + "slot>", "slots/<" + "N>")


def _slot_key_guide_docs() -> list[Path]:
    files: list[Path] = [
        REPO / ".project_manager/wiki/pm_role.md",
        REPO / "templates/claude_code/.project_manager/wiki/pm_role.md",
        REPO / "templates/opencode/.project_manager/wiki/pm_role.md",
        REPO / "templates/opencode/AGENTS.md",
        REPO / "templates/opencode/AGENTS.lite.md",
        REPO / "templates/claude_code/CLAUDE.lite.md",
        *(REPO / relpath for relpath in _SHIPPED_IGNORE_PROSE),
    ]
    for g in (
        ".claude/skills/*/SKILL.md",
        "templates/claude_code/.claude/skills/*/SKILL.md",
        "templates/opencode/.claude/skills/*/SKILL.md",
        "templates/opencode/.opencode/command/*.md",  # T-0674 canonical 기계 사본
    ):
        files += [Path(p) for p in glob.glob(str(REPO / g))]
    return [f for f in files if f.is_file() and f.name != _SELF]


def test_slot_key_notation_explicit_in_guide_docs():
    """가이드 문서가 per-slot pm_state 를 명시형 `slots/<repo>_<N>` 로만 가리킨다 (T-0299).

    모호한 `slots/<slot>`(placeholder)·`slots/<N>`(숫자 오독) 금지 — multi-slot PM 이
    `.local/slots/<repo>_<N>/`(예 `project_manager_1`)를 헛찾던 표기 결함 재발 차단. 엔진
    `.py` 는 스코프 밖(코드=T-0298·pm_handoff `slots/<N>` 는 T-0201 마이그 설명).
    `.claude`/opencode 하니스 사본은 PM 직접 편집(harness) 후 green.
    """
    offenders = _slot_key_offenders(_slot_key_guide_docs())
    assert not offenders, (
        "모호한 slot-key 표기 잔존 — 명시형 `slots/<repo>_<N>`"
        f"(예 project_manager_1·= worktree basename) 로 정정하라 (T-0299): {offenders}"
    )


def _slot_key_offenders(files: list[Path]) -> list[str]:
    """검사 대상의 모호한 slot-key 표기를 줄 단위로 반환한다."""
    offenders = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for bare in _SLOT_KEY_BARE:
                if bare in line:
                    offenders.append(f"{f.relative_to(REPO).as_posix()}:{lineno} :: {bare}")
    return offenders


def test_slot_key_scope_includes_shipped_ignore_prose():
    """slot-key와 폐기 용어 가드가 출하 ignore 산문 목록을 공유한다."""
    relpaths = {path.relative_to(REPO).as_posix() for path in _slot_key_guide_docs()}
    assert set(_SHIPPED_IGNORE_PROSE) <= relpaths


def test_slot_key_guard_detects_retired_notation_in_shipped_ignore_prose(tmp_path, monkeypatch):
    """네 출하 ignore 산문 각각에 폐기 표기를 다시 넣으면 slot-key 검사가 검출한다."""
    ignored = []
    for relpath in _SHIPPED_IGNORE_PROSE:
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# .local/slots/<slot>/pm_state.md\n", encoding="utf-8")
        ignored.append(path)
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    offenders = _slot_key_offenders(ignored)
    assert offenders == [
        f"{relpath}:1 :: slots/<slot>" for relpath in _SHIPPED_IGNORE_PROSE
    ]


# ── T-0557: compaction-native 전환 뒤 폐기된 ctx 안전 표기 가드 ─────────────
# 리터럴은 분할해 이 가드 자체가 자기 검사에 걸리지 않게 한다. CHANGELOG·ADR·sealed spike·
# log·실 ticket은 시점 기록이라 제외한다. 하네스 namespace는 PM이 직접 관리하는 별도 변경
# 표면이고, 이 dev 작업의 출하 가드는 canonical 엔진·동기 문서·README와 그 엔진 미러를 본다.
#
# T-0562: 정확 문자열 매칭이라 표기 변형이 그대로 통과했다 — "hard-stop" 가드가 "hard stop"·
# "hardstop"·"하드스톱" 을 못 잡았다. 이제 각 항목을 **낱말 표기표**로 선언하고 거기서 변형
# 정규식을 기계로 생성한다. 손으로 쓴 정규식을 나열하면 항목마다 규칙이 제각각이 되고(실제로
# ctx-stop 만 한글 음역 "스톱" 이 빠져 미탐), 표기를 늘려도 샘플이 안 따라온다. 금지어 목록
# 자체는 T-0557 그대로다(신규 금지어 추가는 이 가드의 범위가 아니다).
#
# 생성 규칙 세 가지:
#  1. 낱말 사이 구분자 = 항목별 문자 클래스 + `*`. 기본은 `_` 포함(`hard_stop_pct` 처럼 폐기
#     기능이 식별자로 재도입되는 것도 차단한다). `_` 를 뺀 항목은 v1.6.0 이 **의도적으로 남긴**
#     live 런타임 이름과 겹치는 두 개뿐이고, 각각 사유를 선언한다(선언 강제 =
#     `test_v160_underscore_exclusion_declares_reason`).
#  2. 선두 낱말 앞에 합성어 경계를 건다 — 한글 음역(스톱·하드)에는 `(?<![가-힣])`, 영문 표기
#     에는 `(?<![A-Za-z])`. "백스톱"(출하 표면 250줄)·"backstop"(live 표기) 의 꼬리가 각각
#     "스톱 마커"·"stop marker" 로 오탐되는 걸 막는다. 두 문자계에 대칭으로 걸어야 한쪽만
#     보호되는 비대칭이 안 생긴다. 하이픈으로 끊긴 "non-stop marker" 는 합성어가 아니라
#     그대로 차단된다(경계는 낱말 문자 바로 뒤만 막는다).
#  3. 한글 **전용** 표기(재전송 — 음역이 아니라 그 자체가 온전한 낱말)는 붙여 쓴 형태를 경계
#     없이 잡고("자동재전송"), 띄어 쓴 형태에만 경계를 건다("현재 전송"·"잠재 전송" 통과).
#
# 한글 음역을 안 적어서 생기는 미탐(위 ctx-stop 사례)은 표기표 자체에 대한 테스트로 닫는다 —
# 영문 표기가 있으면 한글 음역을 적거나 못 적는 사유를 선언해야 한다
# (`test_v160_retired_term_declares_korean_spelling_or_reason`).
#
# 정규 표기 열은 기존 관례대로 분할 리터럴로 둔다(이 가드가 자기 검사에 안 걸리게).
_HANGUL_SYLLABLE = r"[가-힣]"
_LATIN_LETTER = r"[A-Za-z]"
# 앞 글자가 낱말 문자면 = 합성어의 꼬리(백스톱·현재·backstop·shard_stop)라 폐기 표기가 아니다.
_HANGUL_COMPOUND_BOUNDARY = rf"(?<!{_HANGUL_SYLLABLE})"
_LATIN_COMPOUND_BOUNDARY = rf"(?<!{_LATIN_LETTER})"
_SEPARATORS_WITH_UNDERSCORE = r"[\s_-]"
_SEPARATORS_PROSE_ONLY = r"[\s-]"


class _RetiredCtxTerm(NamedTuple):
    """폐기 표기 한 항목 — 낱말 표기표에서 변형 정규식을 생성한다.

    `words` = 낱말 위치별 표기 후보. 한 표기는 문자열이거나 여러 낱말의 튜플이다
    (예 ("트립", "와이어") → 낱말 사이에도 같은 구분자 클래스가 들어간다).
    """

    canonical: str
    words: tuple[tuple[object, ...], ...]
    pattern: re.Pattern[str]
    separator_chars: str
    runtime_allowances: tuple[str, ...]
    underscore_exemption_reason: str
    korean_exemption_reason: str


def _spelling_words(spelling) -> tuple[str, ...]:
    return (spelling,) if isinstance(spelling, str) else tuple(spelling)


def _iter_spelling_words(words) -> list[str]:
    return [
        word
        for position in words
        for spelling in position
        for word in _spelling_words(spelling)
    ]


def _has_latin_letter(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def _is_hangul_spelling(spelling) -> bool:
    return bool(re.match(_HANGUL_SYLLABLE, _spelling_words(spelling)[0]))


def _spelling_regex(spelling, separators: str) -> str:
    return separators.join(re.escape(word) for word in _spelling_words(spelling))


def _compound_boundary_prefix(spelling) -> str:
    """선두 낱말의 문자계에 맞는 합성어 경계 — 한글·라틴에 대칭으로 건다."""
    head = _spelling_words(spelling)[0]
    if re.match(_HANGUL_SYLLABLE, head):
        return _HANGUL_COMPOUND_BOUNDARY
    if re.match(_LATIN_LETTER, head):
        return _LATIN_COMPOUND_BOUNDARY
    return ""  # 기호로 시작하는 표기(--disable)는 합성어 꼬리가 될 수 없다


def _position_regex(spellings, separators: str, *, boundary: bool) -> str:
    branches = []
    for spelling in spellings:
        prefix = _compound_boundary_prefix(spelling) if boundary else ""
        branches.append(prefix + _spelling_regex(spelling, separators))
    return "(?:" + "|".join(branches) + ")"


def _variant_regex(
    words, separator_chars: str, *, capture_separators: bool = False,
) -> re.Pattern[str]:
    """낱말 표기표 → 변형 정규식 (생성 규칙 1~3은 위 절 주석 참조).

    `capture_separators=True` 면 낱말 **위치 사이**의 구분자를 그룹으로 잡는다(위치 안 낱말은
    그대로). 축 태그 진실성 검사(`_matched_separators`)가 쓰는 같은 조립 규칙 — 정규식을 따로
    손으로 쓰면 표기표와 드리프트한다.
    """
    optional, required = separator_chars + "*", separator_chars + "+"

    def glued(separator: str, parts) -> str:
        return (f"({separator})" if capture_separators else separator).join(parts)

    if not any(_has_latin_letter(word) for word in _iter_spelling_words(words)):
        joined = glued("", [
            _position_regex(position, "", boundary=False) for position in words
        ])
        separated = glued(required, [
            _position_regex(position, optional, boundary=(index == 0))
            for index, position in enumerate(words)
        ])
        return re.compile(f"(?:{joined})|(?:{separated})", re.IGNORECASE)
    return re.compile(
        glued(optional, [
            _position_regex(position, optional, boundary=(index == 0))
            for index, position in enumerate(words)
        ]),
        re.IGNORECASE,
    )


def _retired_ctx_term(
    canonical: str,
    words,
    *,
    separator_chars: str = _SEPARATORS_WITH_UNDERSCORE,
    runtime_allowances: tuple[str, ...] = (),
    underscore_exemption_reason: str = "",
    korean_exemption_reason: str = "",
) -> _RetiredCtxTerm:
    return _RetiredCtxTerm(
        canonical=canonical,
        words=words,
        pattern=_variant_regex(words, separator_chars),
        separator_chars=separator_chars,
        runtime_allowances=runtime_allowances,
        underscore_exemption_reason=underscore_exemption_reason,
        korean_exemption_reason=korean_exemption_reason,
    )


_V160_RETIRED_CTX_TERMS: tuple[_RetiredCtxTerm, ...] = (
    _retired_ctx_term("hard" + "-stop", (("hard", "하드"), ("stop", "스톱"))),
    _retired_ctx_term(
        "stop" + " marker", (("stop", "스톱"), ("marker", "마커")),
        separator_chars=_SEPARATORS_PROSE_ONLY,
        underscore_exemption_reason=(
            "pm_relay 의 live 런타임 이름 stop_marker_present 와 겹친다"
        ),
    ),
    _retired_ctx_term("pre" + "-turn", (("pre", "프리"), ("turn", "턴"))),
    _retired_ctx_term("재" + "전송", (("재",), ("전송",))),
    _retired_ctx_term(
        "ctx" + "-tripwire", (("ctx",), ("tripwire", ("트립", "와이어"))),
    ),
    _retired_ctx_term("break" + "-glass", (("break", "브레이크"), ("glass", "글라스"))),
    _retired_ctx_term(
        "--disable" + " hooks", (("--disable",), ("hooks",)),
        korean_exemption_reason="CLI 플래그 문자열이라 한글 음역형이 없다",
    ),
    # ctx-stop 만 조건부 — 런타임 marker 경로/식별자 문맥이면 허용하고 사람 대상 안내·서술에서만
    # 금지한다. 허용 문맥도 표기표 옆에 둬야 어떤 항목이 왜 느슨한지 한눈에 보인다.
    _retired_ctx_term(
        "ctx" + "-stop", (("ctx",), ("stop", "스톱")),
        separator_chars=_SEPARATORS_PROSE_ONLY,
        underscore_exemption_reason=(
            "live 런타임 이름 ctx.stop_pct·ctx_stop_hook.py 와 겹친다"
        ),
        runtime_allowances=(".local/ctx-stop", "marker_dir"),
    ),
)
_V160_TEXT_SUFFIXES = {
    ".py", ".md", ".sh", ".cmd", ".json", ".jsonc", ".toml", ".manifest",
}
_V160_ADAPTER_DIRS = {".claude", ".opencode", ".codex", ".agents"}

# 어댑터 제외의 예외 — ctx 가드/driver/plugin 파일은 v1.6.0 재설계 표면 그 자체라 스코프에
# 편입한다(T-0557 내부 게이트: 어댑터 일괄 제외는 재유입을 못 잡는다).
_V160_ADAPTER_INCLUDE_NAMES = {
    "ctx_guard.py", "ctx_stop_hook.py", "ctx_stop_hook.sh", "ctx_statusline.py",
    "ctx_statusline.sh", "pm_orch_claude.py", "pm_orch_codex.py", "pm_orch_opencode.py",
    "ctx-guard-core.cjs", "ctx-guard.js", "hooks.json",
}


def _v160_shipping_surface() -> list[Path]:
    files = []
    for path in repo_owned_paths(REPO, ".", mode=OWNED):
        rel = path.relative_to(REPO)
        parts = rel.parts
        if not path.is_file() or path.name == _SELF:
            continue
        if parts[0] == "tests" or rel.as_posix() == "CHANGELOG.md":
            continue
        if (any(part in _V160_ADAPTER_DIRS for part in parts)
                and path.name not in _V160_ADAPTER_INCLUDE_NAMES):
            continue
        rel_text = rel.as_posix()
        if any(segment in rel_text for segment in (
            "/wiki/decisions/", "/wiki/log/", "/wiki/tickets/open/",
            "/wiki/tickets/claimed/", "/wiki/tickets/blocked/", "/wiki/tickets/done/",
        )):
            continue
        if "/wiki/raw/spikes/" in rel_text and not rel_text.endswith("/_template.md"):
            continue
        if path.suffix.lower() in _V160_TEXT_SUFFIXES or path.name in {".gitignore"}:
            files.append(path)
    return files


def _v160_offender(location: str, term: str, found: str) -> str:
    """검출 위치·정규 표기·실제 발견 표기를 한 줄로 만든다.

    변형 매칭이라 정규 표기만 찍으면 파일에서 그 문자열을 찾을 수 없다 — 실제로 걸린
    표기("하드 스톱" 등)를 같이 보여줘야 고칠 자리를 바로 찾는다.
    """
    return f"{location} :: {term} (발견 표기 '{found}')"


def _v160_runtime_allowed(term: _RetiredCtxTerm, lowered_line: str) -> bool:
    """그 줄이 이 항목의 런타임 허용 문맥(marker 경로/식별자)인지 판정한다."""
    return any(allowance in lowered_line for allowance in term.runtime_allowances)


def _v160_ctx_terminology_offenders(files: list[Path]) -> list[str]:
    offenders = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            location = f"{path.relative_to(REPO).as_posix()}:{lineno}"
            lowered = line.lower()
            for term in _V160_RETIRED_CTX_TERMS:
                if _v160_runtime_allowed(term, lowered):
                    continue
                found = term.pattern.search(line)
                if found:
                    offenders.append(
                        _v160_offender(location, term.canonical, found.group(0))
                    )
    return offenders


def test_no_retired_ctx_safety_terminology_in_v160_shipping_surface():
    """compaction-native 출하 표면에 옛 차단·회전 안내가 다시 들어오지 않는다.

    T-0562 이후 정확 표기뿐 아니라 변형(구분자·대소문자·한영 혼용)까지 전수 0 이어야 한다.
    """
    offenders = _v160_ctx_terminology_offenders(_v160_shipping_surface())
    assert not offenders, (
        "폐기된 ctx 안전 표기(변형 포함) 잔존 — compaction-native 서술로 정정하라 "
        f"(T-0557·T-0562): {offenders}"
    )


def test_v160_terminology_scope_covers_readmes_and_canonical_engine():
    relpaths = {path.relative_to(REPO).as_posix() for path in _v160_shipping_surface()}
    assert {
        ".project_manager/tools/pm_relay.py",
        "templates/claude_code/README.md",
        "templates/codex/README.md",
        "templates/opencode/README.md",
    } <= relpaths


# ── T-0562: 변형 클래스 매칭 데모·경계 ─────────────────────────────────────
# 아래 샘플 문자열은 정규 표기를 담지 않은 **변형**이라 분할 리터럴이 필요 없다. 게다가
# `_v160_shipping_surface` 가 `tests/` 를 통째로 제외하므로 이 파일은 자기 검사 대상이 아니다.
_SEPARATOR_AXIS = "구분자"
_LETTER_CASE_AXIS = "대소문자"
_MIXED_LANGUAGE_AXIS = "한영"


def _term_by_canonical(canonical: str) -> _RetiredCtxTerm:
    """정규 표기로 표기표 항목을 찾는다 — 샘플 테이블이 표기표와 어긋나면 즉시 실패."""
    for term in _V160_RETIRED_CTX_TERMS:
        if term.canonical == canonical:
            return term
    raise AssertionError(f"표기표에 없는 정규 표기: {canonical!r}")


def _supported_variant_axes(term: _RetiredCtxTerm) -> tuple[str, ...]:
    """그 항목이 실제로 지원하는 변형 축 — 표기표에서 파생한다(손 선언 아님).

    한글 음역을 표기표에 추가하면 한영 축이 자동으로 생기고, 아래 커버리지 테스트가 그 축의
    샘플을 요구한다. 표기만 늘리고 샘플은 안 늘리는 drift 를 기계로 막는다.
    """
    axes = [_SEPARATOR_AXIS]  # 모든 항목이 낱말 2개 이상이라 구분자 축은 항상 있다
    if any(_has_latin_letter(word) for word in _iter_spelling_words(term.words)):
        axes.append(_LETTER_CASE_AXIS)
    if any(
        any(_has_latin_letter(_spelling_words(s)[0]) for s in position)
        and any(_is_hangul_spelling(s) for s in position)
        for position in term.words
    ):
        axes.append(_MIXED_LANGUAGE_AXIS)
    return tuple(axes)


def _matched_separators(term: _RetiredCtxTerm, text: str) -> tuple[str, ...] | None:
    """그 문장에서 실제로 걸린 표기의 낱말 사이 구분자 (미검출이면 None).

    한글 전용 표기는 붙여 쓴 갈래와 띄어 쓴 갈래가 alternation 이라, 안 걸린 갈래의 그룹은
    None 으로 빠진다 — 걸린 갈래의 구분자만 남긴다(붙여 쓰면 빈 문자열).
    """
    match = _variant_regex(
        term.words, term.separator_chars, capture_separators=True,
    ).search(text)
    if match is None:
        return None
    return tuple(group for group in match.groups() if group is not None)


def _is_separator_variant(term: _RetiredCtxTerm, sample: str) -> bool:
    """샘플이 정규 표기와 **구분자가 다른** 변형인가 — 구분자 축 태그의 진실 조건.

    대소문자·한영 축과 달리 구분자 축은 검사 없이 태그만 달려 있었다. 그러면 태그가 실제
    커버리지와 어긋나도(정규 구분자 그대로인 샘플에 태그를 달아도) 커버리지 테스트가 그 축을
    충족으로 세어 축 하나가 통째로 미검증으로 남는다.
    """
    found = _matched_separators(term, sample)
    return found is not None and found != _matched_separators(term, term.canonical)


# 보강 전(정확 문자열 매칭) 가드를 그대로 통과하던 변형들 — (정규 표기, 그 샘플이 보이는 변형
# 축, 출하 표면에 이렇게 새어들 수 있는 문장). 축 태그는 커버리지 테스트가 소비한다.
_V160_VARIANT_RED_SAMPLES = (
    ("hard" + "-stop", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS),
     "잔여 5% 밴드에서 Hard Stop 으로 세션을 끊는다"),
    ("hard" + "-stop", (_SEPARATOR_AXIS,), "hardstop 밴드에 닿으면 새 세션으로 넘긴다"),
    ("hard" + "-stop", (_SEPARATOR_AXIS,), "hard_stop_pct 임계를 다시 넣는다"),
    ("hard" + "-stop", (_MIXED_LANGUAGE_AXIS,), "하드스톱 이 걸리면 핸드오프를 먼저 쓴다"),
    ("hard" + "-stop", (_SEPARATOR_AXIS, _MIXED_LANGUAGE_AXIS),
     "hard 스톱 안내를 statusline 에 띄운다"),
    ("stop" + " marker", (_SEPARATOR_AXIS,), "stop-marker 를 지운 뒤 relay 를 되살린다"),
    ("stop" + " marker", (_LETTER_CASE_AXIS, _MIXED_LANGUAGE_AXIS),
     "STOP 마커 가 남아 있으면 회전으로 본다"),
    ("pre" + "-turn", (_SEPARATOR_AXIS,), "pre_turn 훅에서 잔여 컨텍스트를 잰다"),
    ("pre" + "-turn", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS), "Pre Turn 훅을 되살린다"),
    ("pre" + "-turn", (_MIXED_LANGUAGE_AXIS,), "프리턴 단계에서 안내를 주입한다"),
    ("재" + "전송", (_SEPARATOR_AXIS,), "직전 프롬프트를 재 전송 해서 복구한다"),
    ("ctx" + "-tripwire", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS),
     "CTX Tripwire 가 발화하면 훅이 막는다"),
    ("ctx" + "-tripwire", (_MIXED_LANGUAGE_AXIS,), "ctx 트립와이어 임계를 conf 로 조정한다"),
    ("break" + "-glass", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS), "Break Glass 절차로 훅을 우회한다"),
    ("break" + "-glass", (_MIXED_LANGUAGE_AXIS,), "브레이크글라스 로 강제 진행한다"),
    ("--disable" + " hooks", (_SEPARATOR_AXIS,), "--disable-hooks 로 가드를 끈다"),
    ("--disable" + " hooks", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS), "--DISABLE_HOOKS 로 우회한다"),
    ("ctx" + "-stop", (_SEPARATOR_AXIS,), "ctx stop 이 걸리면 대화를 새로 연다"),
    ("ctx" + "-stop", (_SEPARATOR_AXIS, _LETTER_CASE_AXIS), "CTXStop 상태를 손으로 해제한다"),
    ("ctx" + "-stop", (_MIXED_LANGUAGE_AXIS,), "ctx 스톱 이 걸리면 새 세션을 연다"),
    ("ctx" + "-stop", (_SEPARATOR_AXIS, _MIXED_LANGUAGE_AXIS), "ctx스톱 안내를 지운다"),
)

# v1.6.0 이후에도 live 인 런타임 이름·marker 경로(canonical 엔진에서 그대로 옮긴 줄). 변형
# 매칭이 이걸 잡기 시작하면 가드가 코드 식별자 규칙까지 지배하는 것이라 오탐이 구조적이 된다.
_V160_RUNTIME_IDENTIFIER_LINES = (
    "CTX_STOP_PCT_DEFAULT = 20   # 잔여 ≤ 이 % → 정지·핸드오프 트리거 임계.",
    '"stop_pct": _ctx_pct("ctx.stop_pct", CTX_STOP_PCT_DEFAULT),',
    "def stop_marker_present(root: Path, session_id: str) -> bool:",
    'exec "$py" "$hook_dir/ctx_stop_hook.py" "$@"',
    'MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"',
    "회전 관측 = ctx 가드가 박는 marker(`.project_manager/.local/ctx-stop/<sid>.done`).",
)

# 표기가 live 합성어의 꼬리와 겹치는 자리 — 가드가 정상 서술을 막으면 안 된다. 한글
# "백스톱"(출하 표면 250줄)·"현재/잠재 전송" 과 영문 "backstop"(live 표기)이 실제 사례다.
_V160_COMPOUND_SAFE_LINES = (
    "현재 전송 중인 프롬프트는 손대지 않는다",
    "잠재 전송량을 미리 계산한다",
    "무진행 판정 + 벽시계 백스톱·전부 선택",
    "벽시계 백스톱 마커 를 남긴다",
    "false-green 백스톱마커 경로",
    "backstop marker 를 남긴다",
    "the backstop marker is stale",
    "shard_stop 값을 읽는다",
)

# 반대로 합성어 경계를 넣어도 이건 여전히 폐기 표기다 — 경계가 미탐 구멍이 되지 않았는지 본다.
# 낱말 문자로 이어붙은 것만 합성어다 — 하이픈으로 끊긴 "non-stop marker" 는 경계 밖이다.
_V160_COMPOUND_BLOCKED_LINES = (
    ("재" + "전송", "프롬프트를 재전송 해서 복구한다"),
    ("재" + "전송", "자동재전송 을 켠다"),
    ("stop" + " marker", "스톱 마커 를 지운 뒤 재개한다"),
    ("hard" + "-stop", "하드 스톱 으로 턴을 끊는다"),
    ("stop" + " marker", "non-stop marker 를 지운다"),
    ("ctx" + "-stop", "ctx-stop marker 를 확인한다"),
)


def _v160_offenders_for_line(line: str, tmp_path, monkeypatch) -> list[str]:
    """한 줄짜리 가짜 출하 파일을 만들어 실제 판정 함수를 그대로 태운다."""
    path = tmp_path / "shipped.md"
    path.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    return _v160_ctx_terminology_offenders([path])


@pytest.mark.parametrize("term", _V160_RETIRED_CTX_TERMS, ids=lambda t: t.canonical)
def test_v160_variant_pattern_matches_its_own_canonical_notation(term):
    """변형 정규식은 자기 정규 표기를 (대소문자 무관) 반드시 잡는다 — 표기표와의 drift 방지."""
    assert term.pattern.search(term.canonical), (
        f"변형 정규식이 정규 표기 '{term.canonical}' 을 못 잡는다: {term.pattern.pattern}"
    )
    assert term.pattern.search(term.canonical.upper()), (
        f"대소문자 변형 '{term.canonical.upper()}' 이 새어나간다"
    )


@pytest.mark.parametrize("term", _V160_RETIRED_CTX_TERMS, ids=lambda t: t.canonical)
def test_v160_retired_term_declares_korean_spelling_or_reason(term):
    """영문 표기가 있는 항목은 한글 음역을 적거나 못 적는 사유를 선언한다.

    ctx-stop 이 "스톱" 을 안 적어 "ctx 스톱" 이 통과하던 미탐의 재발 차단 — 표기 누락은
    조용한 구멍이라 사람 눈이 아니라 표기표 검사로 막는다.
    """
    if not any(_has_latin_letter(word) for word in _iter_spelling_words(term.words)):
        return  # 한글 전용 표기(재전송) — 음역 개념이 없다
    has_korean = any(
        _is_hangul_spelling(spelling)
        for position in term.words for spelling in position
    )
    assert has_korean or term.korean_exemption_reason, (
        f"'{term.canonical}' 에 한글 음역 표기가 없다 — 표기표에 추가하거나 "
        "korean_exemption_reason 을 선언하라"
    )


@pytest.mark.parametrize("term", _V160_RETIRED_CTX_TERMS, ids=lambda t: t.canonical)
def test_v160_underscore_exclusion_declares_reason(term):
    """구분자에서 `_` 를 뺀 항목은 반드시 사유를 선언한다 (미탐 창을 몰래 열지 못하게)."""
    excludes_underscore = "_" not in term.separator_chars
    assert excludes_underscore == bool(term.underscore_exemption_reason), (
        f"'{term.canonical}': `_` 제외 여부({excludes_underscore})와 사유 선언"
        f"({term.underscore_exemption_reason!r})이 어긋난다"
    )


@pytest.mark.parametrize("term", _V160_RETIRED_CTX_TERMS, ids=lambda t: t.canonical)
def test_v160_every_supported_variant_axis_has_red_sample(term):
    """항목이 지원하는 변형 축마다 red 샘플이 최소 1개 있어야 한다 (커버리지 강제).

    샘플이 들쭉날쭉하면 축 하나가 통째로 미검증으로 남는다(ctx-stop 한글 축이 그랬다).
    """
    covered = {
        axis
        for canonical, axes, _ in _V160_VARIANT_RED_SAMPLES
        if canonical == term.canonical
        for axis in axes
    }
    missing = [axis for axis in _supported_variant_axes(term) if axis not in covered]
    assert not missing, f"'{term.canonical}' 의 변형 축 샘플 누락: {missing}"


@pytest.mark.parametrize("term,axes,sample", _V160_VARIANT_RED_SAMPLES)
def test_v160_variant_escapes_exact_match_but_guard_blocks(
        term, axes, sample, tmp_path, monkeypatch):
    """보강 전 정확 매칭이 놓치던 변형을 지금은 차단한다 (T-0562 red 재현).

    첫 assert 가 red 상태의 재현이다 — 이 문장은 옛 정확 문자열 규칙(`term in line.lower()`)에
    안 걸렸다. 이어서 축 태그가 샘플과 맞는지 **세 축 모두** 보고(구분자 축도 기계 확인 —
    태그만 달고 실제로는 정규 구분자인 샘플은 커버리지를 가짜로 채운다), 마지막에 보강 후
    차단을 확인한다.
    """
    assert term not in sample.lower(), (
        f"샘플이 정규 표기 '{term}' 을 그대로 담고 있어 변형 데모가 아니다"
    )
    if _SEPARATOR_AXIS in axes:
        assert _is_separator_variant(_term_by_canonical(term), sample), (
            f"구분자 축 태그인데 샘플 구분자가 정규 표기 '{term}' 과 같다: {sample!r}"
        )
    if _LETTER_CASE_AXIS in axes:
        assert re.search(r"[A-Z]", sample), (
            f"대소문자 축 태그인데 샘플에 대문자가 없다: {sample!r}"
        )
    if _MIXED_LANGUAGE_AXIS in axes:
        korean = [
            word
            for position in _term_by_canonical(term).words for spelling in position
            for word in _spelling_words(spelling)
            if _is_hangul_spelling(word)
        ]
        assert any(word in sample for word in korean), (
            f"한영 축 태그인데 샘플에 한글 표기({korean})가 없다: {sample!r}"
        )
    offenders = _v160_offenders_for_line(sample, tmp_path, monkeypatch)
    assert any(f":: {term} (" in offender for offender in offenders), (
        f"변형 표기가 '{term}' 가드를 통과했다: {sample!r} → {offenders}"
    )


def test_v160_separator_axis_truth_check_rejects_a_mistagged_sample():
    """구분자 축 진실성 검사 자체의 감도 — 정규 구분자 그대로면 그 축 변형이 아니다.

    "STOP 마커" 는 대소문자·한영 변형이지만 구분자는 정규 표기와 같은 공백이다. 이 판정이
    무뎌지면(항상 참) 위 커버리지 강제가 구분자 축을 가짜로 채운 샘플에 속는다.
    """
    term = _term_by_canonical("stop" + " marker")
    assert not _is_separator_variant(term, "STOP 마커 가 남아 있으면 회전으로 본다")
    assert _is_separator_variant(term, "stop-marker 를 지운 뒤 relay 를 되살린다")


@pytest.mark.parametrize("term", _V160_RETIRED_CTX_TERMS, ids=lambda t: t.canonical)
def test_v160_separator_capture_regex_matches_the_same_span(term):
    """구분자 캡처 정규식은 판정 정규식과 **같은 구간**을 잡는다 (조립 규칙 드리프트 방지).

    축 진실성 검사가 판정과 다른 구간을 보면 태그 검증이 엉뚱한 문자열을 근거로 삼는다.
    """
    capture = _variant_regex(
        term.words, term.separator_chars, capture_separators=True,
    )
    samples = [
        term.canonical,
        term.canonical.upper(),
        *(
            sample for canonical, _axes, sample in _V160_VARIANT_RED_SAMPLES
            if canonical == term.canonical
        ),
        *_V160_COMPOUND_SAFE_LINES,
    ]
    for sample in samples:
        expected, found = term.pattern.search(sample), capture.search(sample)
        assert (found is None) == (expected is None), (
            f"'{term.canonical}' 캡처/판정 정규식의 검출 여부가 다르다: {sample!r}"
        )
        if expected is not None:
            assert found.group(0) == expected.group(0), (
                f"'{term.canonical}' 캡처/판정 정규식의 구간이 다르다: {sample!r}"
            )


@pytest.mark.parametrize("line", _V160_RUNTIME_IDENTIFIER_LINES)
def test_v160_guard_tolerates_runtime_snake_case_identifiers(
        line, tmp_path, monkeypatch):
    """snake_case 런타임 이름·marker 경로는 변형 매칭의 대상이 아니다 (경계 못박기).

    `ctx.stop_pct`·`stop_marker_present`·`ctx_stop_hook.py` 는 v1.6.0 이 남긴 live 식별자고,
    `.local/ctx-stop` marker 경로는 런타임 호환 경로다. 그 두 항목의 구분자에 `_` 를 넣거나
    런타임 허용을 지우면 이 테스트가 먼저 red 로 알린다.
    """
    assert _v160_offenders_for_line(line, tmp_path, monkeypatch) == []


@pytest.mark.parametrize("line", _V160_COMPOUND_SAFE_LINES)
def test_v160_guard_tolerates_live_compound_words(line, tmp_path, monkeypatch):
    """live 합성어(백스톱·현재 전송·backstop·shard_stop)를 오탐하지 않는다 (합성어 경계).

    경계는 한글·라틴에 대칭으로 걸린다 — 한쪽만 보호하면 반대 문자계의 정상 서술이 막힌다.
    """
    assert _v160_offenders_for_line(line, tmp_path, monkeypatch) == []


@pytest.mark.parametrize("term,line", _V160_COMPOUND_BLOCKED_LINES)
def test_v160_compound_boundary_does_not_open_a_miss_window(
        term, line, tmp_path, monkeypatch):
    """합성어 경계를 넣어도 폐기 표기 자체는 여전히 차단한다 (경계의 반대편)."""
    offenders = _v160_offenders_for_line(line, tmp_path, monkeypatch)
    assert any(f":: {term} (" in offender for offender in offenders), (
        f"합성어 경계가 '{term}' 미탐 구멍을 냈다: {line!r} → {offenders}"
    )


# ── T-0794: 리뷰 루프 서술 가드 (이중 채널 병행=표준 → 단일 reviewer + opt-in 추가 리뷰어) ──
# v1.7.8 이 리뷰 루프를 reviewer 1회 → PM 판정 delta([[T-0785]]) → PM 기계 확인([[T-0786]]) 으로
# 기계화했고, 추가 리뷰어는 `additional_reviewer.enabled` 로 켜는 opt-in 채널(기본 OFF)이다. 그런데
# 출하 방법론·스킬 산문은 "내부 code-reviewer + 추가 리뷰어 (둘 다)"·"표준 리뷰 게이트"로 병행을
# *표준*으로 서술해 채택자에게 켜지 않은 채널을 필수 단계로 읽혔다. "병행을 표준으로 서술"은 의미
# 판정이 불가능하므로 관측된 표기 4종을 토큰으로 못박는다.
#
# 리터럴 분할: 이 가드 파일 자신이 자기 검사에 안 걸리게(_SELF 제외와 이중 방어).
_REVIEW_LOOP_CHANNEL = "추가 " + "리뷰어"
_REVIEW_LOOP_RETIRED = (
    "병행해 " + _REVIEW_LOOP_CHANNEL,
    "표준 리뷰 " + "게이트",
    "reviewer+" + _REVIEW_LOOP_CHANNEL,
    _REVIEW_LOOP_CHANNEL + " (둘 다)",
)


def _historical_record(rel: str, *, include_tests: bool = False) -> bool:
    """그 상대경로가 term-of-the-time 기록(고칠 수 없는 표면)인지 판정한다.

    `_v160_shipping_surface` 의 historical 규칙과 같은 집합에 pm_import 백업을 더한다 —
    `.pm_import_backups/<날짜>/` 는 흡수 전 스냅샷이라 log·decisions 와 같은 immutable 기록이다.
    `include_tests` 는 산문 축(테스트는 폐기 표기를 *논의*할 수 있다)과 메시지 축(런타임 문자열을
    단언하는 테스트가 같은 표기를 들고 있어야 한다)이 갈리는 자리다.
    """
    if rel == "CHANGELOG.md":
        return True
    if not include_tests and rel.startswith("tests/"):
        return True
    if rel.startswith(".pm_import_backups/"):
        return True
    if any(seg in rel for seg in (
        "/wiki/decisions/", "/wiki/log/", "/wiki/tickets/open/",
        "/wiki/tickets/claimed/", "/wiki/tickets/blocked/", "/wiki/tickets/done/",
    )):
        return True
    if "/wiki/raw/spikes/" in rel and not rel.endswith("/_template.md"):
        return True
    return False


def _shipped_text_surface(*, include_tests: bool = False) -> list[Path]:
    """출하 텍스트 표면 전량(엔진 코드·방법론 문서·어댑터·3타깃 사본·설치자 ignore 산문).

    `_v160_shipping_surface` 와 텍스트 확장자 집합을 공유하되 어댑터 디렉터리(`.claude`·
    `.opencode`·`.codex`·`.agents`)를 **제외하지 않는다** — 방법론 산문이 사는 자리가 바로 스킬·
    커맨드·에이전트 카드라, v1.6.0 ctx 가드처럼 어댑터를 일괄 제외하면 재유입을 못 잡는다(스킬
    `references/*.md`·codex `.agents/skills`·opencode `.opencode/command` 가 그 사각이었다).
    """
    files: list[Path] = []
    for path in repo_owned_paths(REPO, ".", mode=OWNED):
        if not path.is_file() or path.name == _SELF:
            continue
        if _historical_record(path.relative_to(REPO).as_posix(),
                              include_tests=include_tests):
            continue
        if path.suffix.lower() in _V160_TEXT_SUFFIXES or path.name == ".gitignore":
            files.append(path)
    return files


def _review_loop_surface() -> list[Path]:
    """리뷰 루프 서술이 실제로 사는 출하 표면(산문 축 — 테스트 제외)."""
    return _shipped_text_surface()


def _review_loop_offenders(files: list[Path]) -> list[str]:
    """검사 대상에서 폐기된 리뷰 루프 표기를 줄 단위로 반환한다."""
    offenders = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for retired in _REVIEW_LOOP_RETIRED:
                if retired in line:
                    offenders.append(
                        f"{f.relative_to(REPO).as_posix()}:{lineno} :: {retired}"
                    )
    return offenders


def test_no_retired_review_loop_phrasing_in_shipping_surface():
    """출하 표면이 추가 리뷰어 병행을 표준 리뷰 게이트로 서술하지 않는다 (T-0794).

    현행 흐름은 dev → code-reviewer 1회 → PM 판정 delta → dev fix → PM 기계 확인이고, 추가
    리뷰어는 켠 채택자만 병행하는 opt-in 채널이다. 켜지 않은 채택자가 필수 단계로 읽으면 없는
    게이트를 기다리거나 외부 전송 동의 없이 채널을 켠다.
    """
    offenders = _review_loop_offenders(_review_loop_surface())
    assert not offenders, (
        "폐기된 리뷰 루프 표기 잔존 — 추가 리뷰어는 opt-in(기본 OFF) 채널로 서술하라 "
        f"(T-0794): {offenders}"
    )


def test_review_loop_surface_covers_every_live_channel_mention():
    """가드 시야가 추가 리뷰어를 언급하는 live 출하 문서 전량을 덮는다 (시야==표면 독립 대조).

    표면 열거를 손으로 유지하면 새 스킬·새 하네스 사본이 시야 밖에서 표기를 되살린다. 가드가
    보는 집합과, 저장소를 독립으로 훑어 채널을 언급하는 집합을 대조해 차집합 0 을 단언한다.
    """
    view = {path.resolve() for path in _review_loop_surface()}
    mentions = []
    missed = []
    for path in repo_owned_paths(REPO, ".", mode=OWNED):
        if not path.is_file() or path.suffix.lower() != ".md" or path.name == _SELF:
            continue
        rel = path.relative_to(REPO).as_posix()
        if _historical_record(rel):
            continue
        if _REVIEW_LOOP_CHANNEL not in path.read_text(encoding="utf-8"):
            continue
        mentions.append(rel)
        if path.resolve() not in view:
            missed.append(rel)
    assert mentions, (
        "채널을 언급하는 live 문서가 0 — 스캔이 무력화됐다(판정 불능은 통과가 아니다)"
    )
    assert not missed, f"가드 시야 밖에서 채널을 서술하는 출하 문서: {missed}"


@pytest.mark.parametrize("relpath", [
    ".project_manager/wiki/pm_playbook.md",
    ".claude/skills/pm-dev-delegate/SKILL.md",
    ".claude/skills/pm-review/references/operational-details.md",
    "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    "templates/opencode/.opencode/command/pm-dev-delegate.md",
    "templates/claude_code/.project_manager/wiki/pm_playbook.md",
])
def test_review_loop_surface_includes_load_bearing_docs(relpath):
    """표기가 실제로 잔존했던 자리(canonical·3타깃 사본·어댑터 remap)가 시야 안이다."""
    view = {path.relative_to(REPO).as_posix() for path in _review_loop_surface()}
    assert relpath in view


@pytest.mark.parametrize("retired", _REVIEW_LOOP_RETIRED)
def test_review_loop_guard_detects_each_retired_phrase(
        retired, tmp_path, monkeypatch):
    """폐기 표기를 하나라도 다시 넣으면 검사가 그 줄을 검출한다 (sensitivity)."""
    doc = tmp_path / "skill.md"
    doc.write_text(f"리뷰 루프는 {retired} 로 돌린다.\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)

    offenders = _review_loop_offenders([doc])

    assert offenders == [f"skill.md:1 :: {retired}"]


# ── T-0795: '부기'(附記) 폐지 가드 ─────────────────────────────────────────────
# '부기'는 일본식 한자어(附記 덧붙여 적음 / 簿記 장부 기록)로 국립국어원 순화 대상이고, 이 프로젝트
# 문서 규칙(번역체·일본식 공문서체 금지)과 어긋난다. 사용자 결정(2026-08-22)으로 출하 표면 전량에서
# '기록' 계열로 바꿨다 — 합성어는 `완료 기록`·`기록 게이트`, 단독·동사 용법이 의미를 잃으면 '추가
# 기록'. 런타임 메시지(`[완료] T-NNNN 기록 완료.`)를 문자열로 단언하는 테스트가 있어 메시지와 단언이
# 같은 표기를 들고 있어야 하므로 `tests/` 도 이 가드의 시야 안이다(산문 축인 리뷰 루프 가드와 갈린다).
# historical(CHANGELOG·log·decisions·tickets·sealed spike·pm_import 백업)은 그 시점 표기 기록이라
# 제외한다.
#
# 리터럴 분할: 이 가드 파일 자신이 자기 검사에 안 걸리게(_SELF 제외와 이중 방어).
_RETIRED_BOOKKEEPING_TERM = "부" + "기"


def _retired_bookkeeping_offenders(files: list[Path]) -> list[str]:
    """검사 대상의 폐기 용어 잔존을 줄 단위로 반환한다."""
    offenders = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _RETIRED_BOOKKEEPING_TERM in line:
                offenders.append(f"{f.relative_to(REPO).as_posix()}:{lineno}")
    return offenders


def test_no_retired_bookkeeping_term_in_shipping_surface():
    """출하 표면(엔진 메시지·방법론·스킬·3타깃 사본·테스트 단언)에 '부기' 0 (T-0795)."""
    offenders = _retired_bookkeeping_offenders(
        _shipped_text_surface(include_tests=True))
    assert not offenders, (
        f"폐기 용어 '{_RETIRED_BOOKKEEPING_TERM}' 잔존 — '기록' 계열로 정정하라 "
        f"(T-0795): {offenders}"
    )


@pytest.mark.parametrize("relpath", [
    ".project_manager/tools/ticket_finish.py",   # 런타임 메시지 최다 보유
    ".project_manager/engine.manifest",          # 확장자 밖 산문 — 손 sweep 이 빠뜨렸던 자리
    ".project_manager/wiki/pm_playbook.md",
    ".claude/skills/pm-wave-finish/SKILL.md",
    "README.md",
    "tests/test_ticket_finish.py",               # 메시지 문자열 단언 축
    "templates/codex/AGENTS.md",                 # manifest 밖 어댑터 진입(전파 안 됨)
    "templates/opencode/.opencode/agents/pm.md",
    "templates/claude_code/.project_manager/wiki/tickets/README.md",
])
def test_bookkeeping_guard_scope_includes_every_sweep_axis(relpath):
    """sweep 이 실제로 손댄 축(엔진·manifest·방법론·스킬·README·테스트·3타깃 사본)이 시야 안이다.

    manifest·타깃 어댑터 진입·타깃 wiki 스캐폴드는 `pm_update` 전파 대상이 아니라 손 sweep 이
    빠뜨리기 쉬운 자리다(이번 sweep 도 루트 `engine.manifest` 를 1차 스코프에서 놓쳤다).
    """
    view = {path.relative_to(REPO).as_posix()
            for path in _shipped_text_surface(include_tests=True)}
    assert relpath in view


@pytest.mark.parametrize("relpath", [
    "CHANGELOG.md",
    ".pm_import_backups/2026-08-10/AGENTS.md",
])
def test_bookkeeping_guard_leaves_historical_records_alone(relpath):
    """historical 기록은 시야 밖이다 — 그 시점 표기라 고칠 수 없고 red 로 만들면 안 된다."""
    view = {path.relative_to(REPO).as_posix()
            for path in _shipped_text_surface(include_tests=True)}
    assert relpath not in view


def test_bookkeeping_guard_detects_reintroduction(tmp_path, monkeypatch):
    """폐기 용어를 다시 넣으면 검사가 그 줄을 검출한다 (sensitivity)."""
    doc = tmp_path / "tool.py"
    doc.write_text(
        f'print("[완료] {{tid}} {_RETIRED_BOOKKEEPING_TERM} 완료.")\n', encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)

    assert _retired_bookkeeping_offenders([doc]) == ["tool.py:1"]
