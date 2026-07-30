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
from pathlib import Path

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
# 스코프 = 사람이 읽는 **가이드 문서**만. 엔진 `.py`·`.gitignore` 는 **제외** — 코드는 T-0298 소관이고
# `pm_handoff.py` 의 `slots/<N>` 은 divergent-bare(`--slot 4` verbatim) 마이그(T-0201)를 *설명*하는
# 정당한 등장이다. `.claude/skills/*/SKILL.md`(양 하네스 단일 소비·ADR-0065·opencode 미러 포함)도
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
    ]
    for g in (
        ".claude/skills/*/SKILL.md",
        "templates/claude_code/.claude/skills/*/SKILL.md",
        "templates/opencode/.claude/skills/*/SKILL.md",  # ADR-0065 단일 소비 미러(command 은퇴)
    ):
        files += [Path(p) for p in glob.glob(str(REPO / g))]
    return [f for f in files if f.is_file() and f.name != _SELF]


def test_slot_key_notation_explicit_in_guide_docs():
    """가이드 문서가 per-slot pm_state 를 명시형 `slots/<repo>_<N>` 로만 가리킨다 (T-0299).

    모호한 `slots/<slot>`(placeholder)·`slots/<N>`(숫자 오독) 금지 — multi-slot PM 이
    `.local/slots/<repo>_<N>/`(예 `project_manager_1`)를 헛찾던 표기 결함 재발 차단. 엔진
    `.py`·`.gitignore` 는 스코프 밖(코드=T-0298·pm_handoff `slots/<N>` 는 T-0201 마이그 설명).
    `.claude`/opencode 하니스 사본은 PM 직접 편집(harness) 후 green.
    """
    offenders = []
    for f in _slot_key_guide_docs():
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for bare in _SLOT_KEY_BARE:
                if bare in line:
                    offenders.append(f"{f.relative_to(REPO).as_posix()}:{lineno} :: {bare}")
    assert not offenders, (
        "모호한 slot-key 표기 잔존 — 명시형 `slots/<repo>_<N>`"
        f"(예 project_manager_1·= worktree basename) 로 정정하라 (T-0299): {offenders}"
    )
