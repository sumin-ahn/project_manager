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


_PROTECTED_DEFAULT_LITERAL = "default"   # 칼럼 비움 = DEFAULT_PROTECTED 폴백(해제 아님).


def _validate_protected_tokens(value: str) -> tuple[list[str] | None, str]:
    """`"main, release"` → `(["main", "release"], "")` · 거부면 `(None, 사유)`.

    **형식만** 본다 — 브랜치 실재는 검증하지 않는다(아직 없는 `release` 를 미리 보호하는 게
    정상적인 쓰임이다). 거부 대상:
      - 빈 문자열/공백만 — "보호 없음" 은 표현 불가(`default` 리터럴로 안내).
      - 빈 토큰(선/후행 쉼표·연속 쉼표) — areas 셀에 빈 자리를 만든다.
      - 토큰 내부 공백 — areas.md 는 공백-구분 표라 셀/줄이 깨진다.
      - `|` — markdown 표 구분자(줄 corruption).
    """
    if not value.strip():
        return None, (
            "빈 보호목록은 지정할 수 없다 — 보호는 안전 기본값이 있어야 한다(\"보호 없음\"은 "
            f"표현 불가). 기본값(main/master/develop)으로 되돌리려면 `{_PROTECTED_DEFAULT_LITERAL}` "
            "리터럴을 쓰라."
        )
    tokens: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            return None, (
                f"보호목록 {value!r} 에 빈 토큰이 있다(선/후행 쉼표·연속 쉼표) — "
                "`main,develop` 처럼 쉼표로만 구분하라."
            )
        if any(ch.isspace() for ch in token):
            return None, (
                f"보호 브랜치 {token!r} 에 공백이 있다 — areas.md 는 공백-구분 표라 줄이 깨진다."
            )
        if "|" in token:
            return None, (
                f"보호 브랜치 {token!r} 에 `|` 가 있다 — markdown 표 구분자라 areas.md 줄이 깨진다."
            )
        tokens.append(token)
    return tokens, ""


# 허용 repo name = prefix = ticket ID 네임스페이스·areas.md 공백구분 칼럼값.
# 영숫자로 시작(leading `-` = 옵션 오인·빈 문자열 배제), 이후 영숫자/`_`/`-` 만 허용.
# 경로분리자(`/`)·`.`(`..` 폴더탈출)·공백(areas.md 줄 corruption)을 전부 배제한다.
# board._FAMILY_SCOPE_RE(`^[A-Za-z0-9_-]+$`)는 leading `-` 를 막지 않아 재사용 부적합 —
# repo name 은 영숫자 시작을 강제하므로 별도 패턴을 둔다(reuse 확인·중복 정의 회피).
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class AreasRepoNotFound(Exception):
    """areas.md 에 그 repo 행이 없다 — 셀 변경 대상 부재 (부작용 0).

    등록되지 않은 repo(또는 areas.md 부재)의 칼럼을 바꾸려 한 경우다. 등록은 `pm-config repo
    add`(append) 소관이고 setter 는 *기존 셀만* 고친다 — 없는 행을 만들어내지 않는다.
    """

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"areas.md 에 repo {repo!r} 행이 없다")


class AreasDuplicateRepo(Exception):
    """areas.md 에 같은 repo 행이 2개 이상 — 셀 변경 fail-loud (부작용 0).

    first-match 리졸버(`_repo_protected`·`_repo_base`·`_areas_git_url`)가
    첫 행에서 return 하므로 어느 행을 고쳐야 하는지 기계가 정할 수 없다. 추측해서 한쪽만 고치면
    사용자가 바꿨다고 믿는 값과 리졸버가 읽는 값이 갈린다 → **아무것도 쓰지 않고** 수동 정리를
    안내한다(자동 병합 안 함·사람 판정). `count` = 중복 행 수(진단용).
    """

    def __init__(self, repo: str, count: int):
        self.repo = repo
        self.count = count
        super().__init__(f"areas.md 에 repo {repo!r} 행이 {count}개 있다(중복)")


# areas.md 셀에 그대로 들어가면 표 자체가 깨지는 문자 — repo 이름은 폴더명에서 유도되므로
# 이 sanity 를 init 입구에서 한 번 본다(부작용 0 fail-loud·`--repo` 로 우회 가능).
_AREAS_CELL_BREAKERS = ("|", "\n", "\r")


# 사용자 정보(`@` 앞)가 비밀일 수 있는 origin 주소 형식 — 이 형식의 사용자 자리에는 비밀번호나 토큰이
# 들어간다(ssh·scp 형식의 사용자 이름은 비밀이 아니라 그대로 기록한다).
_ORIGIN_SCHEMES_WITH_SECRET_USERINFO = ("http", "https")


# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — 이 접두 **정확히**를 벗겨야 순수 브랜치명이다
# (`worktree_git.current_branch` 와 같은 규칙 · 같은 이름 태그가 있으면 `--short` 는 `heads/` 를 붙인다).
_SYMREF_BRANCH_PREFIX = "refs/heads/"


_SELF_COORDINATES_UNKNOWN = (
    "  ⚠ 자기 행의 git·base 를 기록하지 못했다 (이 폴더가 git 저장소 최상위가 아니거나 HEAD 가 "
    "브랜치를 가리키지 않거나 origin 주소에 자격증명이 들어 있다). areas.md 의 그 행 `git`·`base` 칸을 채운 뒤 `pm-config worktree add` 를 "
    "실행하라.")
