"""areas.md 레지스트리 읽기 (층: infra).

등록 repo·prefix 목록과 활성 prefix 해소. 표 문법은 `pm_engine.domain.areas_rules` 가 소유한다.
"""
from __future__ import annotations

from pathlib import Path

from pm_engine.domain import areas_rules, ticket_rules
from pm_engine.infra import fs, pm_paths

from pm_engine.infra import git


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


def _repo_protected(home, repo: str) -> list[str]:
    """그 repo 의 보호 브랜치 목록. 미지정/미등록/구 스키마 → `DEFAULT_PROTECTED`.

    areas.md `protected` 칼럼(쉼표분리·예 `main,develop`)을 읽는다. pre-push 훅 설치(sidecar
    채움)·bootstrap 보호 경고가 이 목록으로 PM 의 보호 브랜치 commit/push 를 막는다.

    **default 폴백 = `DEFAULT_PROTECTED`(main/master/develop)** (`_repo_base` 의 None 폴백과
    다름 — 보호는 *안전 기본값이 있어야* 한다·미지정 repo 도 main 을 막는다). 다음 모두
    default 로 떨어진다:
      - areas.md 부재 — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `protected` 칼럼 자체가 없는 구 레지스트리(헤더에 protected 없음 → 행 dict 에 키 없음).
      - `protected` 칼럼이 빈 값(부분 등록).
    명시 지정이면 쉼표분리·strip·빈 토큰 제거 후 그 목록(전부 빈 토큰이면 default 폴백).
    """
    _header, rows = _parse_areas(home)
    for row in rows:
        if row.get("repo") == repo:
            raw = row.get("protected") or ""
            branches = [b.strip() for b in raw.split(",") if b.strip()]
            return branches if branches else list(areas_rules.DEFAULT_PROTECTED)
    return list(areas_rules.DEFAULT_PROTECTED)


def _areas_git_url(home, repo: str) -> str | None:
    """그 repo 의 areas.md `git` 칼럼 URL (bare mirror clone 원). 미등록/부재/빈 값 → None.

    multi-user 공유 채택 폴더(= 하나의 git repo 를 여러 사람이 clone)에서 2번째 사용자가
    `.repos/<repo>.git` bare mirror 를 hydrate 할 때(`pm-config repo add <repo>` — `--git`
    불요) clone 원 URL 을 여기서 해소한다. areas.md(git-tracked·공유)엔 URL 이 있으나
    `.repos/`(gitignore·per-clone)엔 mirror 가 없는 상황 — 등록된 URL 을 재제공 없이 재사용한다.

    repo 명은 areas.md `repo` 칼럼과 매칭한다(prefix 칼럼은 비므로 **repo 명 키**).
    중복 행이면 first-match(`_repo_base`·`_areas_row_for_prefix` 동형·같은 repo=같은 URL 이라
    결정론적).

    None 폴백(호출자 cmd_repo_add 가 fail-loud 또는 `--git` 명시 요구로 전환):
      - areas.md 부재 — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `git` 칼럼 자체가 없는 구 레지스트리(헤더에 git 없음 → 행 dict 에 git 키 없음).
      - `git` 칼럼이 빈 값(부분 등록).
    """
    _header, rows = _parse_areas(home)
    for row in rows:
        if row.get("repo") == repo:
            return row.get("git") or None
    return None


def _areas_header_line() -> str:
    """canonical areas.md 헤더 행 (`| repo | prefix | … | area_owner |`·줄바꿈 없음).

    `_AREAS_COLUMNS`(단일 진실)에서 파생한다 — `areas_append` 의 신규 파일 헤더와
    `_migrate_areas_text` 의 구-헤더 업그레이드가 같은 8칼럼 헤더를 쓰도록 한 곳에서 만든다.
    """
    return "| " + " | ".join(areas_rules._AREAS_COLUMNS) + " |"


def _areas_separator_line() -> str:
    """canonical areas.md 구분선 (`|---|---|…|`·칼럼 수만큼 `---`·줄바꿈 없음)."""
    return "|" + "|".join("---" for _ in areas_rules._AREAS_COLUMNS) + "|"


def _areas_column_index(header_cells: list[str],
                        column: str) -> tuple[int, str | None, int]:
    """헤더에서 `column` 셀 인덱스를 해소한다 — 부재면 구-헤더 업그레이드 계획을 함께 낸다.

    반환 `(idx, new_header_line, sep_cols)`:
      - 헤더에 그 칼럼이 있으면 `(header_cells.index(column), None, 0)` — 업그레이드 없음(헤더
        행 verbatim 보존).
      - 없으면(구 헤더) **업그레이드**한다. 헤더가 canonical prefix(`_AREAS_COLUMNS` 앞 N개와
        일치하는 per-repo 레지스트리 계열 5/6/7칼럼)면 canonical 8칼럼 헤더(`_areas_header_line`)
        로 교체하고 idx=canonical 인덱스. 비호환 구 헤더(멀티-clone `prefix|area|owner`[3] 등 —
        칼럼 의미가 canonical 과 어긋남)면 정렬을 깨지 않게 기존 칼럼 **뒤에 그 칼럼만 append**
        하고 idx=len(header_cells). `sep_cols` = 업그레이드 후 구분선이 가질 칼럼 수.

    `_migrate_areas_text`(area_owner backfill)와 `areas_set_cell`(범용 셀 변경)이 **공유**한다.
    """
    if column in header_cells:
        return header_cells.index(column), None, 0
    if tuple(header_cells) == areas_rules._AREAS_COLUMNS[:len(header_cells)]:
        return (areas_rules._AREAS_COLUMNS.index(column), _areas_header_line(),
                len(areas_rules._AREAS_COLUMNS))
    return (len(header_cells),
            "| " + " | ".join(header_cells + [column]) + " |",
            len(header_cells) + 1)


def _areas_row_cell_index(header_cells: list[str], cells: list[str],
                          header_idx: int, column: str) -> int:
    """데이터 행에서 `column` 셀의 인덱스 — wider row 는 canonical 인덱스로 보정한다.

    셀 수가 헤더보다 많은 행은 `_areas_row_dict`(=`_parse_areas`)가 헤더를 무시하고 canonical
    순서로 매핑하므로, 쓰기측도 **정확히 같은** 인덱스를 잡아야 한다. 그 외는
    `_areas_column_index` 가 준 헤더 인덱스 그대로.
    """
    if len(cells) > len(header_cells) and column in areas_rules._AREAS_COLUMNS:
        return areas_rules._AREAS_COLUMNS.index(column)
    return header_idx


def _repo_base(home, repo: str) -> str | None:
    """그 repo 의 areas.md `base` 브랜치. 미지정/미등록/구 스키마 → None.

    `pm-config repo add --base`(또는 clone-time bare HEAD 해소)가 areas.md `base`
    칼럼에 기록한 값을 읽어, worktree 슬롯 브랜치가 *그 base 에서 파생*되게 한다
    (`pm-config worktree add` → `create_slot(base=)`). repo 명은 areas.md `repo` 칼럼과
    매칭한다(repo add 가 `repo=name·prefix=name` 으로 등록하므로 repo==prefix 가 보통).

    None 폴백(worktree add 가 현행 bare HEAD 동작·회귀 0):
      - areas.md 부재 — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `base` 칼럼 자체가 없는 구 레지스트리(헤더에 base 없음 → 행 dict 에 base 키 없음).
      - `base` 칼럼이 빈 값(부분 등록).
    """
    _header, rows = _parse_areas(home)
    for row in rows:
        if row.get("repo") == repo:
            return row.get("base") or None
    return None


def _areas_append_locked(
    home, prefix: str,
    owner: str,
    *,
    repo: str | None = None,
    git: str | None = None,
    test_cmd: str | None = None,
    base: str | None = None,
    protected: str | None = None,
    area_owner: str | None = None,
) -> None:
    """``board_lock`` 보유 전제의 areas 행 append 변형(cmd_init fresh 판정용)."""
    _repo = repo if repo is not None else prefix
    _git = git or ""
    _test = test_cmd or ""
    _base = base or ""
    _protected = protected or ""
    _area_owner = area_owner or ""
    af = pm_paths.areas_file(home)
    if not af.exists():
        af.write_text(
            "# Area Registry\n\n"
            "> per-repo 레지스트리: repo → prefix → git → "
            "test_cmd → owner → base → protected → area_owner. 멀티-PM ID 네임스페이스 + "
            "per-repo 테스트 경로 + worktree base 브랜치 + 보호 브랜치 + user 소유의 단일 진실. "
            "append-only (`merge=union`).\n"
            "> `board.py init` / `pm-config repo add` 가 등록. "
            "prefix 유일성 = race-free ID 의 전제.\n\n"
            + _areas_header_line() + "\n"
            + _areas_separator_line() + "\n",
            encoding="utf-8", newline="\n")
    # O_APPEND atomic append — areas 는 append-only 레지스트리이므로
    # read-modify-write 가 아니라 OS 가 보장하는 원자 추가로 동시 등록 충돌을 없앤다.
    fs.append_atomic(
        af,
        f"| {_repo} | {prefix} | {_git} | {_test} | {owner} | {_base} "
        f"| {_protected} | {_area_owner} |\n")


def _areas_set_cell_text(text: str, repo: str, column: str,
                         value: str) -> tuple[str, str]:
    """areas.md 텍스트에서 `repo` 행의 `column` 셀을 `value` 로 바꾼 (새 텍스트, 옛 값) 반환.

    순수 변환(IO 없음·`_migrate_areas_text` 동형 관용구):
      - 줄 종결자(LF/CRLF)·주석·빈 줄·표 밖 텍스트·**다른 행**은 verbatim 보존.
      - 헤더에 그 칼럼이 없으면(구 레지스트리) `_areas_column_index` 계획대로 헤더 + 헤더 직후
        구분선 1개를 업그레이드한다(canonical 8칼럼 또는 기존 헤더 끝 append·비파괴).
      - 행 매칭은 `_areas_row_dict`(= `_parse_areas` 와 같은 매핑)의 `repo` 값으로 한다 —
        읽기측이 보는 행과 쓰기측이 고치는 행이 **항상 같다**.
      - 대상 셀 인덱스는 `_areas_row_cell_index`(wider-row canonical 보정)로 잡는다.
      - 셀이 모자라면 빈 칸으로 패딩해 인덱스를 확보한다(비파괴 append).

    매칭 행이 0개면 `AreasRepoNotFound`, 2개 이상이면 `AreasDuplicateRepo` — **호출자가 쓰기
    전에** 터지므로 부작용 0이다.
    """
    lines = text.splitlines(keepends=True)
    header_cells: list[str] | None = None
    col_idx: int | None = None
    upgrade = False
    sep_cols = 0
    sep_replaced = False
    matches = 0
    old_value = ""
    out: list[str] = []
    for line in lines:
        nl = ""
        body = line
        if line.endswith("\r\n"):
            nl, body = "\r\n", line[:-2]
        elif line.endswith("\n"):
            nl, body = "\n", line[:-1]
        cells = areas_rules._split_areas_row(body)
        if cells is None:
            if upgrade and not sep_replaced and areas_rules._AREAS_SEP_RE.match(body.strip()):
                out.append("|" + "|".join("---" for _ in range(sep_cols)) + "|" + nl)
                sep_replaced = True
            else:
                out.append(line)
            continue
        if header_cells is None:
            header_cells = [c.lower() for c in cells]
            col_idx, new_header, sep_cols = _areas_column_index(header_cells, column)
            if new_header is None:
                out.append(line)
            else:
                upgrade = True
                out.append(new_header + nl)
            continue
        if areas_rules._areas_row_dict(header_cells, cells).get("repo") != repo:
            out.append(line)
            continue
        matches += 1
        idx = _areas_row_cell_index(header_cells, cells, col_idx or 0, column)
        if matches == 1:
            old_value = cells[idx] if idx < len(cells) else ""
        while len(cells) <= idx:
            cells.append("")
        cells[idx] = value
        out.append("| " + " | ".join(cells) + " |" + nl)
    if matches == 0:
        raise areas_rules.AreasRepoNotFound(repo)
    if matches > 1:
        raise areas_rules.AreasDuplicateRepo(repo, matches)
    return "".join(out), old_value


def _areas_set_cell_locked(home, repo: str, column: str, value: str) -> tuple[str, str]:
    """``board_lock`` 보유 전제의 셀 교체 변형 — (옛 값, 새 값) 반환.

    검증·원자 write 규칙은 `areas_set_cell` 과 동일하다(그쪽 docstring 이 계약의 단일 진실).
    캐시 무효화는 호출부 몫이다 — 락 안에서 무효화해도 락을 놓기 전엔 새 값이 안 보인다.
    """
    if column not in areas_rules._AREAS_COLUMNS:
        raise ValueError(
            f"알 수 없는 areas.md 칼럼 {column!r} — 허용: {', '.join(areas_rules._AREAS_COLUMNS)}")
    if "|" in value or "\n" in value or "\r" in value:
        raise ValueError(
            f"areas.md 셀 값에 `|`/개행을 쓸 수 없다(표 corruption): {value!r}")
    value = value.strip()
    af = pm_paths.areas_file(home)
    # `newline=""`(universal-newline OFF) 로 읽고 쓴다 — CRLF 채택자(Windows)의 파일을
    # 셀 하나 바꾸려다 LF 로 무단 정규화하지 않게(`rewrite_refs` 와 같은 관용구).
    text = ""
    if af.exists():
        with fs.open_shared(af, binary=False, encoding="utf-8", newline="") as fh:
            text = fh.read()
    new_text, old_value = _areas_set_cell_text(text, repo, column, value)
    if new_text != text:
        # atomic write — 같은 디렉토리 temp + 원자 교체(부분기록/crash 잔재 방지).
        # write 실패(디스크풀·권한)로 replace 에 못 가면 `.tmp` 가 남으므로 finally 로 청소한다
        # (다음 실행이 stale temp 를 만나지 않게·잔재 0).
        tmp = af.with_suffix(af.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
            fs.atomic_replace(tmp, af)
        finally:
            # 성공 시 원자 교체로 tmp 는 이미 사라졌다(missing_ok 로 no-op).
            tmp.unlink(missing_ok=True)
    return old_value, value
