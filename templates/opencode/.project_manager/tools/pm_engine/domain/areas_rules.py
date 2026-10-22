"""areas.md 레지스트리 표 문법 (층: domain).

행 분할·canonical 칼럼 순서·행 dict 매핑. 파일 읽기는 `pm_engine.infra.areas_store` 가 한다.
"""
from __future__ import annotations

import re


# areas.md 신/구 스키마.
#   - 구 스키마: `| prefix | area | owner |`                      (멀티-CLONE)
#   - per-repo: `| repo | prefix | git | test_cmd | owner |`      (per-repo 레지스트리)
#   - base 스키마: `| repo | prefix | git | test_cmd | owner | base |`  (base 브랜치)
#   - protected 스키마: `| repo | prefix | git | test_cmd | owner | base | protected |`  (보호브랜치)
#   - 신 스키마: `| … | protected | area_owner |`                 (user 소유)
#     area_owner = `--mine` 기본 풀 입력의 *user* 소유. 기존 `owner`
#     (per-repo registry registrant)를 overload 하지 않는 *별도* 칼럼이다(codex sug — 의미 충돌 회피).
# 파싱은 **헤더 행을 읽어 칼럼명→인덱스**로 매핑한다(위치-비의존) — 모든 스키마를 같은
# 코드로 읽고, 누락 칼럼은 빈 값으로 떨어뜨려 하위호환을 보장한다(`base`/`protected` 칼럼
# 없는 구 레지스트리 → 행 dict 에 그 키 없음 → `_repo_base`/`_repo_protected` 가 폴백).
_AREAS_SEP_RE = re.compile(r"^\|[\s:|-]+\|?$")  # markdown 구분선 `|---|---|`


def _split_areas_row(line: str) -> list[str] | None:
    """`| a | b | c |` 한 줄을 셀 리스트로. table row 가 아니면 None.

    구분선(`|---|---|`)·빈 줄·비-`|` 줄은 None. 앞뒤 파이프를 벗기고 셀을 strip.
    """
    s = line.strip()
    if not s.startswith("|") or _AREAS_SEP_RE.match(s):
        return None
    # 앞뒤 경계 파이프 제거 후 분할 (내부 셀 사이 파이프로 split).
    inner = s.strip("|")
    return [c.strip() for c in inner.split("|")]


# areas.md canonical 칼럼 순서 (신 스키마). 구 헤더(`base`/
# `protected`/`area_owner` 없음)는 이 순서의 *prefix* 다(`repo|prefix|git|test_cmd|owner` 또는
# …`|base`/…`|protected`). 그래서 헤더보다 셀이 많은 행(구 헤더에 신 칼럼 row 가 append 된
# *업그레이드* 프로젝트 — `repo add` 가 완전 canonical row 를 더 짧은 헤더에 붙인 경우)을 이
# canonical 순서로 이어 매핑해 `base`/`protected`/`area_owner` 유실을 막는다(게이트가
# base 에 대해 건 가드를 protected[7칸]→area_owner[8칸]까지 확장).
_AREAS_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base", "protected",
                  "area_owner")


def _areas_row_dict(header_cells: list[str], cells: list[str]) -> dict[str, str]:
    """데이터 행 셀 → `{칼럼명: 값}` (헤더 매핑 · wider row 는 canonical 순서).

    셀 수가 헤더보다 **많으면**(더 넓은 신 스키마 row 를 더 좁은 구 헤더에 append 한 업그레이드
    프로젝트) 헤더를 무시하고 `_AREAS_COLUMNS` 순서로 매핑한다(base/protected/area_owner 유실
    차단·폭 초과는 `col{i}` 폴백). 그 외는 자기 헤더로 매핑(모자란 셀은 빈 문자열).
    """
    if len(cells) > len(header_cells):
        return {
            (_AREAS_COLUMNS[i] if i < len(_AREAS_COLUMNS) else f"col{i}"): cells[i]
            for i in range(len(cells))
        }
    return {header_cells[i]: (cells[i] if i < len(cells) else "")
            for i in range(len(header_cells))}


# 보호 브랜치 default (엔진 상수) — areas.md `protected` 칼럼이 미지정/미등록일 때
# 폴백. PM 이 자율로 commit/push 못 하는 브랜치(pre-push 훅·bootstrap 경고가 이걸 본다).
# per-repo override 는 areas.md `protected` 칼럼(쉼표분리·예 `main,develop`).
DEFAULT_PROTECTED = ("main", "master", "develop")
