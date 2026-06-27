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
        REPO / "templates/opencode/.opencode/command/pm-bootstrap.md",
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
