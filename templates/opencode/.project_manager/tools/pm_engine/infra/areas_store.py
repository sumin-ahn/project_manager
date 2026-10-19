"""areas.md 레지스트리 읽기 (층: infra).

등록 repo·prefix 목록과 활성 prefix 해소. 표 문법은 `pm_engine.domain.areas_rules` 가 소유한다.
"""
from __future__ import annotations

from pathlib import Path

from pm_engine.domain import areas_rules, ticket_rules
from pm_engine.infra import fs, pm_paths


def id_prefix(home, override: str | None = None) -> str | None:
    """Resolve ticket-ID namespace prefix (multi-repo areas·N×M).

    prefix 는 M>1 repo 의 ID 네임스페이스(협업용 아님)다. 해소 체인(count-based
    유도):

        override(--prefix)
          > 등록 repo(prefix) 가 정확히 1개면 그 prefix               (count-based)
          > None

    None → 무prefix `T-NNNN`(`none` 카테고리·1급 형상). Non-None → `T-<PREFIX>-NNN` 네임스페이스.

    세션 이름에서 repo 를 떼어 prefix 를 유도하는 층은 없다 — 엔진은 세션 토큰을 분해하지 않는다.

    **local.conf `prefix=` 층은 없다.** prefix 는 areas.md 의 per-repo 칼럼이 단일 진실이고,
    clone 전역 키로 남의 prefix 를 silent 오네임스페이스하던 클래스를 원천 차단한다. 그 키가
    conf 에 남아 있어도 읽지 않는다. 해소 실패는 `none` 카테고리(`T-NNNN`)이며, multi-repo
    (등록 repo ≥2) 홈에서 count-based 가 실패하면 `cmd_new` 가 fail-loud
    (오네임스페이스 방지).
    """
    if override:
        # override 를 등록된 canonical case 로 해소 (prefix 동일성=case-insensitive fold):
        # 입력 `aaa` 가 등록 `AAA` 로 fold-매치되면 등록 case `AAA` 로 발행(네임스페이스 분할 방지).
        # 미등록이면 입력 그대로 — 최초 사용이 canonical case 를 확립하고, 발행 시 `_next_id` 가
        # 기존 티켓 시리즈 case 로 정련한다(기존 `T-AAA-*` 를 `--prefix aaa` 가 이어감).
        return ticket_rules._fold_lookup(override, registered_prefixes(home)) or override
    # count-based — 등록 repo(prefix) 가 정확히 1개면 그것(단일 self-host·모호성 0).
    #    cmd_new 의 ≥2 fail-loud 가드와 같은 `registered_prefixes()` 를 세어 lockstep 유지.
    registered = registered_prefixes(home)
    if len(registered) == 1:
        return next(iter(registered))
    # 미해소 → None. 등록 0(prefix 칼럼이 빈 repo 행만 있는 홈)은 `none` 카테고리(`T-NNNN`)이고,
    #    등록 ≥2 면 `cmd_new` 가 fail-loud 한다(오귀속 차단).
    return None


def _parse_areas(home, path: Path | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """areas.md 를 (header 칼럼명 리스트, 데이터 행 dict 리스트) 로 파싱한다.

    헤더-인식: 첫 table row 를 칼럼명(소문자)으로 보고, 이후 데이터 행을
    `{칼럼명: 셀값}` 으로 매핑한다. 누락 칼럼은 빈 문자열. 신/구 스키마 공용.

    **신 스키마 행 관용(하위호환)**: `areas_append` 는 *항상* 그 시점의 완전한
    canonical per-repo row 를 쓴다(6칸 base·7칸 protected). 구 헤더(6칸 base / 5칸
    per-repo `repo|…|owner` / 3칸 멀티-clone `prefix|area|owner`)에 그 *더 넓은* row 가 append 된
    업그레이드 프로젝트에서, 헤더 길이만큼만 매핑하면 `protected`/`base`(또는 `repo` 등)가
    유실/오매핑된다 → **셀 수가 헤더보다 많으면**(=더 넓은 신 스키마 row 를 더 좁은 구 헤더에 붙임)
    헤더와 무관하게 `_AREAS_COLUMNS`(canonical) 순서로 매핑한다(append-only 보존·파일 미수정).
    `== canonical폭` 만 보면 *직전 버전*(6칸)이 append 한 row 가 _AREAS_COLUMNS 가 7칸으로 자란 뒤
    헤더 매핑으로 떨어져 `base` 유실 → `> len(header)` 가 6칸·7칸 신 row 둘 다 보존.
    셀 수가 헤더 이하인 행(구 6/5/3칸 데이터 row)은 자기 헤더로 매핑(현행). areas.md 부재 → ([], []).
    """
    af = path if path is not None else pm_paths.areas_file(home)
    if not af.exists():
        return [], []
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in fs.read_text_shared(af, encoding="utf-8").splitlines():
        cells = areas_rules._split_areas_row(line)
        if cells is None:
            continue
        if not header:
            header = [c.lower() for c in cells]
            continue
        # 헤더보다 넓은 행 = 신(더 넓은) 스키마 canonical row 를 더 좁은 구 헤더에 append 한
        # 업그레이드 케이스(6칸·7칸 row 를 5/3칸 헤더 아래) → canonical 순서로
        # 매핑해 base/protected 유실 차단. 매핑 규칙은 `_areas_row_dict` 단일 진실(쓰기측
        # `areas_set_cell`·`_migrate_areas_text` 와 공유).
        rows.append(areas_rules._areas_row_dict(header, cells))
    return header, rows


def _areas_row_for_prefix(home, prefix: str) -> dict[str, str] | None:
    """활성 prefix 의 areas.md 데이터 행(dict). 미등록/부재 → None."""
    _header, rows = _parse_areas(home)
    for row in rows:
        if row.get("prefix") == prefix:
            return row
    return None


def registered_prefixes(home) -> set[str]:
    """Prefixes registered in areas.md (shared registry). Empty set if no registry.

    등록 repo가 2개 이상이면 multi-repo(N×M) 신호로 쓰여 `board.py new`의
    prefix 미해소를 막는다. 단, 명시 prefix는 레지스트리 등록 자체가 필수가 아니며
    3소스에 없는 새 카테고리라면 값-결속 사용자 승인을 지난 뒤 사용할 수 있다.
    prefix 칼럼이 전부 빈 보드는 무prefix(`none` 카테고리)를, 1개인 보드는 그 단일 prefix
    유도를 허용한다.

    헤더-인식 파서(`_parse_areas`)로 `prefix` 칼럼을 읽는다 — 구 스키마
    (`| prefix | … |`)와 신 스키마(`| repo | prefix | … |`) 모두에서
    prefix 칼럼 위치에 상관없이 동작한다.
    """
    _header, rows = _parse_areas(home)
    return {p for row in rows if (p := row.get("prefix"))}


def registered_repos(home) -> set[str]:
    """Repo names registered in areas.md (per-repo registry `repo` 칼럼). 부재→빈 set.

    `registered_prefixes` 의 repo-명 짝. `repo add`(pm_config)가 멱등 재등록을 판별할 때
    쓴다 — repo명 자동시드 폐지후 prefix 칼럼이 비므로 repo 존재 여부를 prefix
    로 셀 수 없다(빈 prefix 는 `registered_prefixes` 에 안 잡힘). repo 칼럼으로 직접 세어
    중복 등록(같은 repo 두 번 append)을 막는다.
    """
    _header, rows = _parse_areas(home)
    return {r for row in rows if (r := row.get("repo"))}
