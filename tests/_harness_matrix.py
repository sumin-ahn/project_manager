"""공유 하네스 축 — 채택자 산출물-내용 게이트의 단일 출처 (T-0429).

## 왜 이 파일이 있나
채택자 산출물을 *내용까지* 검사하는 게이트들이 하네스별 `["claude","opencode"]` **손-복제**로만
존재해, 세 번째 하네스(codex·[[ADR-0070]])에 도달하지 못했다 — codex `.codex/agents/*.toml` 이
`{{PROJECT_NAME}}` 을 리터럴로 출하했는데 어느 게이트도 codex 트리를 안 봤다(4600 green 뒤 은닉).
[[T-0424]] 가 엔진에서 한 수술(**열거 → 파생**)을 게이트 축에 대칭 적용한다 — 손으로 세 번째를
적어 넣으면 네 번째에서 같은 일이 반복되므로, 축을 엔진/파일시스템에서 파생한다.

## 파생 원칙 (손-열거 금지 · [[ADR-0006]])
  - `pm_import.HARNESS_TEMPLATE_DIRS` = 하네스 → `templates/` 어댑터 트리 디렉토리명(엔진 권위 목록).
  - `templates/<dir>/` 디렉토리 실존 = "디렉토리 존재만으로 발견"(pm_update.resolve_target_root).
`HARNESSES` = 그 둘의 **교집합**(단일-어댑터 하네스만 · combo 키 'both' 제외). 새 하네스가 제대로
추가되면(상수 + 디렉토리) 게이트에 자동 편입되고, `templates/` 밑 stray 디렉토리(빈 dir·파일만
있는 dir·`.hidden`)는 상수에 없어 **하네스로 오인되지 않는다**(파생 로직 자체의 강건성).

어댑터 네임스페이스/루트 문서는 `pm_import.ADD_HARNESS_ADAPTER` 에서 파생한다(claude/opencode 는
단일 어댑터 dir, codex 는 dual `.codex`+`.agents`). 게이트가 스캔할 트리를 손으로 적지 않는다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES = REPO / "templates"


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def derive_harnesses(templates_dir, harness_template_dirs) -> tuple[str, ...]:
    """단일-어댑터 하네스 import 정체성을 파생한다 (손-열거 아님).

    출처 = 엔진 `HARNESS_TEMPLATE_DIRS`(하네스→어댑터 트리 디렉토리 매핑·권위) + 파일시스템
    (`templates/<dir>/` 실존·ADR-0006). 규칙:
      - combo 키('both' = 어댑터 트리 2개)는 단일 하네스가 아니라 **제외**한다.
      - **등록 ⇒ 실존 불변식**: combo 제외한 각 등록 하네스의 `templates/<dir>/` 가 없으면 **loud
        RuntimeError**. 조용히 드롭하면(옛 ∩ 동작) templates/<dir> 소실·개명 시 그 하네스의 전
        parametrize 게이트가 **무음 우회**된다(수집 축소만·red 0)·round3 MF.
      - 디렉토리는 있는데 상수에 없으면 **무시**(stray = 하네스 아님 · 빈 dir·파일만·.hidden 강건).
        이 방향(존재하는데 미등록)은 불변 — 파생은 상수를 iterate 하지 파일시스템을 iterate 하지 않는다.
    순수 함수 — `templates_dir` 와 상수를 인자로 받아 실트리 없이도 파생 로직 자체를 테스트할 수 있다.

    파생 결과가 **비면 loud 하게 죽는다**(RuntimeError·combo 키만 있는 퇴화 상수) — 수집 0
    (vacuous green)으로 게이트가 조용히 통과하는 걸 막는다.
    """
    templates_dir = Path(templates_dir)
    out: list[str] = []
    for harness, template_dirs in harness_template_dirs.items():
        if len(template_dirs) != 1:
            continue  # combo 키('both') — 단일 하네스 아님
        (dirname,) = template_dirs
        if not (templates_dir / dirname).is_dir():
            raise RuntimeError(
                f"등록된 하네스 {harness!r} 의 templates/{dirname}/ 디렉토리가 없다 "
                f"(templates_dir={templates_dir}). '등록 ⇒ 실존' 불변식 위반 — 소실/개명 시 그 하네스의 "
                "전 parametrize 게이트가 무음 우회되므로 조용히 축소하지 않고 loud fail 한다."
            )
        out.append(harness)
    result = tuple(sorted(out))
    if not result:
        raise RuntimeError(
            f"파생된 하네스 0개 (templates_dir={templates_dir}) — 단일-하네스 등록 항목이 없다"
            "(combo 키만?). 게이트가 수집 0(vacuous green)으로 조용히 통과하는 걸 막기 위해 loud fail."
        )
    return result


_PM_IMPORT = _load_pm_import()

# 하네스 축 단일 출처 — 엔진 상수 ∩ templates/ 디렉토리 실존에서 파생(위 derive_harnesses).
#   실트리 = ("claude", "codex", "opencode"). 네 번째가 제대로 추가되면 자동 편입된다.
HARNESSES: tuple[str, ...] = derive_harnesses(TEMPLATES, _PM_IMPORT.HARNESS_TEMPLATE_DIRS)

# 하네스별 어댑터 네임스페이스(채택자가 소비하는 산출물 트리) — 엔진 ADD_HARNESS_ADAPTER 에서 파생.
#   값 = adapter_dirs 튜플. codex 는 dual namespace(.codex + .agents).
HARNESS_ADAPTER_DIRS: dict[str, tuple[str, ...]] = {
    h: dirs for h, (dirs, _doc) in _PM_IMPORT.ADD_HARNESS_ADAPTER.items()
}

# 하네스별 루트 진입 문서 — 엔진 ADD_HARNESS_ADAPTER 에서 파생(claude=CLAUDE.md·opencode/codex=AGENTS.md).
HARNESS_ROOT_DOC: dict[str, str] = {
    h: doc for h, (_dirs, doc) in _PM_IMPORT.ADD_HARNESS_ADAPTER.items()
}


def entry_docs(harness: str) -> list[str]:
    """하네스의 루트 진입 문서 + lite 변형(예 CLAUDE.md·CLAUDE.lite.md). lite 부재는 소비처가
    `is_file()` 로 자연 skip 한다(full 무게축·codex 는 lite 미출하)."""
    root = HARNESS_ROOT_DOC[harness]
    return [root, root[: -len(".md")] + ".lite.md"]
