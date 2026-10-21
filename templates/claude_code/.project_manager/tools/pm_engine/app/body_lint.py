"""티켓·idea 본문 양식과 사실성 lint (층: app).

필수 절·placeholder 잔존, touches 경로 실재, 본문 인용 경로 실재, architect 라운드 존재를 판정한다.
발행(`new`)·승격(`promote`) 게이트와 전역 lint 가 같은 판정을 쓴다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from pm_engine.domain import design_rules, markdown_text, ticket_rules
from pm_engine.infra import fs, pm_paths, repo_coordinates, repo_files, ticket_store
from pm_engine.app import round_seed


# Unfilled `_template.md` text — its presence means the ticket is still a stub.
# The `## 메모` placeholder is intentionally NOT listed: that section is a work
# journal filled at completion time, so an empty 메모 is normal for a complete,
# claimable ticket and must not count as "thin".
#
# 각 항목은 `_template.md` 의 절별 뼈대 문장 리터럴이라 *채우면 사라진다* — 실 본문엔 안 나타나
# 오탐 0(placeholder 잔존만 잡는다). promote 게이트가
# 이 집합을 재사용(`_body_lint_issues` 단일 깔때기)하므로, 뼈대만 채운 절(인터페이스·결정·참고
# pattern-reference)까지 걸러야 "자족성 = placeholder 0" 이 성립한다. 목표/DoD 뿐 아니라
# 인터페이스·결정·참고 절의 미충전도 여기서 promote-차단된다.
_PLACEHOLDERS: tuple[str, ...] = (
    "무엇을 만들 / 바꿀 / 검증할지",      # ## 목표 절 미충전
    "이 ticket 이 만들거나 바꾸는",        # ## 인터페이스 절 미충전
    "구현 방향에 대한 확정 사항",           # ## 결정 절 미충전
    "핵심 산출물 (파일, 동작)",            # ## 완료 조건(DoD) 미충전
    "[[architecture]] 관련 절",            # ## 참고 절 미충전
    "[[xxxxx]]",                           # ## 참고 ADR/spec 미충전
    "T-XXXX",                              # ## 참고 pattern-reference 미충전
    "<제목>",                              # 제목 미치환
)
_REQUIRED_SECTIONS: tuple[str, ...] = ("## 목표", "## 완료 조건", "## 참고")

# authoring 게이트(`cmd_new` 발행·`cmd_promote` 승격) 전용 strict 절 집합 — 자족성이
# 요구하는 5절(목표/인터페이스/결정/DoD/참고) 전부의 *존재*를 강제한다. placeholder 검사는 절을
# **통째로 삭제한** 회피(뼈대 문장이 없으니 잔존 토큰도 없음)를 못 잡으므로,
# 절 자체의 부재를 thin 으로 세운다. 전역 lint(`lint_bodies`)는 레거시 blast-radius(인터페이스/결정
# 절 없는 기존 open/claimed 티켓이 blocking 화) 때문에 3절 불변(`_REQUIRED_SECTIONS`)을 유지하고,
# 이 strict 집합은 authoring **두 소비 지점**만 `strict_sections=True` 로 opt-in 한다(단일 깔때기·
# 소비측 파라미터 — 판정 로직은 한 함수).
_STRICT_REQUIRED_SECTIONS: tuple[str, ...] = (
    "## 목표", "## 인터페이스", "## 결정", "## 완료 조건", "## 참고")

# ── 본문 사실성 판정 (승격 순간만·opt-in) ─────────────────────────────────────
#
# 형식 게이트(placeholder·thin·설계 절)는 "뼈대가 남았나"만 본다 — 티켓이 *주장하는 사실*
# (touches 경로가 실재하나 · 인용한 `파일:줄` 이 맞나)과 "초안을 다른 역할이 점검했나"는 그
# 판정으로 알 수 없는 종류다. 그 축을 `promote` 승격 순간에만 opt-in(`content_facts=True`)으로
# 더한다. 전역 lint(`lint_bodies`)는 켜지 않는다 — 이미 open 인 티켓을 소급 판정하지 않고
# `lint --gate` 차단 집합도 그대로다(push 게이트 무오염).
#
#   touches 미해소                                          → 경고 1줄(비차단)
#   repo-relative 인용의 파일 부재·줄 범위 초과              → 차단
#   `design: required|done` 의 architect 점검 라운드 미회수  → 차단
#   판정불능(좌표 미해소·basename 단독 인용)                 → 개수 1줄(통과로 위장하지 않는다)
#
# 심볼 실재는 판정하지 않는다 — 활성 본문의 미발견 심볼이 전부 정당(신설 예정·하네스 스키마·
# 자체 예시)이라 정밀도가 0 이다. touches 겹침 축도 이 게이트의 것이 아니다(never-block 표면화
# 를 소유하는 쪽이 따로 있다).
_TOUCHES_MISSING_KIND = "touches-missing"
_CITATION_KIND = "citation-unresolved"
_ARCHITECT_ROUND_KIND = "architect-review-pending"
_UNVERIFIABLE_KIND = "content-unverifiable"
_ARCHITECT_ROLE = "architect"

# promote 가 **경고 1줄**로만 내는 kind — 그 밖의 판정은 전부 차단이다. 전역 등급
# (`_ADVISORY_LINT_KINDS`)을 여기 재사용하지 않는다: 그 집합엔 authoring 게이트가 *차단*으로
# 쓰는 `design-pending` 이 들어 있어(전역 lint 는 never-block·승격은 차단), 재사용하면 설계
# 게이트가 조용히 경고로 강등된다.
_PROMOTE_ADVISORY_KINDS: frozenset[str] = frozenset(
    {_TOUCHES_MISSING_KIND, _UNVERIFIABLE_KIND})

# 본문 인용 표기 — 백틱 코드 span 안의 `<경로>.<확장자>:<줄>`. 코드 span 은 `_strip_code` 가
# 지우는 대상이라 이 스캔만은 **펜스만** 제거한 텍스트를 본다(펜스 안 예시 블록은 인용이 아니다).
_CITATION_RE = re.compile(r"`(?P<path>[^`\s]+?\.[A-Za-z0-9]{1,6}):(?P<line>\d+)`")

# repo 트리 좌표가 아닌 표기 — 판정 대상이 아니다(절대·홈·상위 경로).
_OFF_TREE_CITATION_PREFIXES: tuple[str, ...] = ("/", "~", "..")

# 하위 디렉터리 기준 인용을 해소할 때 순회에서 빼는 디렉터리 — 티켓이 인용하는 소스가 아니고
# (객체 저장소·캐시·의존성 벤더), 순회 비용의 대부분이 여기서 나온다.
_CITATION_INDEX_PRUNE_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
     "node_modules", ".venv", "venv"})


def _content_search_roots(home) -> tuple[Path, ...]:
    """티켓이 인용한 경로를 해소할 트리 — PM 홈 + 등록 worktree 슬롯.

    board 의 touches 는 한 트리에 있지 않다: PM 홈이 소유하는 자산(`wiki/` 문서)과 슬롯
    worktree 가 소유하는 코드(`tests/`·`templates/`)가 한 목록에 섞인다. 한쪽만 보면 정상
    경로의 상당수가 "부재" 로 읽힌다. 리스 장부는 `_active_slot_path` 와 같은 데이터-결합으로
    읽고(worktree_pool 미import·원자 교체 대상이라 락 없는 point-read 가 일관 스냅샷),
    장부 부재·손상·형식 이상은 PM 홈 단독으로 폴백한다(fail-soft — 슬롯은 *추가* 레이어).
    장부 경로는 호출 시점 `REPO` 로 해소한다(`Home.board_root` 관례·재앵커 추종).
    """
    roots: list[Path] = [home.root]
    ledger = home.root / ".project_manager" / ".local" / "worktree-leases.json"
    try:
        data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return tuple(roots)
    rows = data.get("leases") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return tuple(roots)
    for row in rows:
        if not isinstance(row, dict):
            continue
        slot = row.get("slot")
        if not isinstance(slot, str) or not slot:
            continue
        path = home.root / slot
        if path.is_dir() and path not in roots:
            roots.append(path)
    return tuple(roots)


def _exists_in_roots(relative: str, roots: Sequence[Path]) -> Path | None:
    """`relative` 를 각 트리에 붙여 처음 실재하는 경로 (어디에도 없으면 None)."""
    for root in roots:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


def _touches_existence_issues(
    home,
    tid: str, touches: Any, roots: Sequence[Path],
) -> tuple[list[tuple[str, str, str]], int]:
    """touches 경로 실재 판정 → (경고 0~1줄, 판정불능 개수).

    좌표 정규화는 `repo_coordinates.normalize_repo_path` 단일 소유자를 쓴다 — 슬롯 접두
    (`work/<repo>_<N>/`)는 리스 장부 대조로 **검증된 것만** 벗겨지고, 검증 못 한 접두는
    예외로 온다. 그 경로는 판정불능으로 세고 red 로도 green 으로도 쓰지 않는다(장부 없는
    형상에서 조용히 통과·차단되지 않게).

    부재는 **차단이 아니다**: 신설 예정 파일을 touches 에 먼저 적는 것이 정상 운영이고
    (활성 티켓 실측에서 부재분의 대부분이 그것) 본문 언급 여부로는 구분되지 않아(부재 경로가
    전부 본문에 등장한다) 차단하면 정당한 발행을 막는다. 그래서 경고 1줄이다.
    """
    entries = [str(item).strip() for item in (touches or []) if str(item).strip()]
    if not entries:
        return [], 0
    missing: list[str] = []
    unverifiable = 0
    for entry in entries:
        try:
            normalized = repo_coordinates.normalize_repo_path(entry, pm_root=home.root)
        except repo_coordinates.RepoCoordinateError:
            unverifiable += 1
            continue
        # 접두가 검증된 경로는 그 워크스페이스에서만 찾는다(다른 슬롯의 동명 파일로 해소되면
        # 좌표 선언이 틀렸다는 사실이 가려진다). 접두 없는 경로는 두 소유 트리를 다 본다.
        workspace = getattr(normalized, "workspace", None)
        search = (workspace,) if workspace is not None else roots
        if _exists_in_roots(str(normalized), search) is None:
            missing.append(entry)
    if not missing:
        return [], unverifiable
    return [(tid, _TOUCHES_MISSING_KIND,
             f"touches {len(missing)}건이 어느 소유 트리에도 없다: {' · '.join(missing)} — "
             f"신설 예정이면 그대로 두고, 오타·옛 경로면 고쳐라 (경고·차단 아님)")], unverifiable


def _citation_suffix_index(
    names: set[str], roots: Sequence[Path],
) -> dict[str, list[Path]]:
    """`names` 의 파일명만 모은 basename → 실재 경로 색인 (소유 트리당 열거 1회).

    하위 디렉터리 기준 표기를 해소하려면 "그 경로로 끝나는 파일이 트리에 있나"를 봐야 한다.
    찾는 파일명이 몇 개뿐이라 순회 1회에 필요한 후보만 모은다(인용마다 다시 걷지 않는다).
    소유 트리는 서로 포함 관계일 수 있어(PM 홈 ⊃ 슬롯 worktree) 절대경로로 중복을 지운다 —
    안 지우면 같은 파일이 두 건으로 세어져 유일 해소가 사라진다.

    순회는 `repo_files` 공용 seam(추적분 + 미추적·비무시 파일)에 위임한다 — 직접
    `os.walk` 를 부르면 새 재귀 tree-walk 호출을 전수 감시하는 가드의 사각이
    생긴다. git 부재/비-repo 사본은 그 seam 이 filesystem 전수 순회로 강등해 값은 그대로다.

    그 seam 의 Git 성공 경로는 **working-tree 존재를 재검사하지 않는다**(index-only 경로도
    돌려준다 — symlink·gitlink 를 거짓 탈락시키지 않으려는 그쪽의 의도적 계약). 인용 판정이
    묻는 것은 "이 경로에 실제로 읽을 파일이 있는가" 이므로, 여기서 `is_file()` 로 한 번 더
    걸러 종전 `os.walk` 의 물리 존재 의미를 보존한다 — 안 그러면 tracked-but-working-tree
    -missing 인용이 차단(citation-unresolved) 대신 판정불능으로 강등된다(가드 약화).
    """
    index: dict[str, list[Path]] = {name: [] for name in names}
    seen: set[str] = set()
    for root in roots:
        for rel in repo_files.list_repo_owned_files(
                root, Path("."), mode=repo_files.OWNED):
            rel_path = Path(rel)
            if any(part in _CITATION_INDEX_PRUNE_DIRS for part in rel_path.parts):
                continue
            name = rel_path.name
            if name not in index:
                continue
            path = root / rel_path
            key = str(path)
            if key in seen:
                continue
            if not path.is_file():
                continue
            seen.add(key)
            index[name].append(path)
    return index


def _citation_suffix_matches(path: str, index: dict[str, list[Path]]) -> list[Path]:
    """`path` 로 끝나는 실재 파일 전부 — 하위 디렉터리 기준 표기의 해소 후보."""
    tail = f"/{path}"
    return [found for found in index.get(path.rsplit("/", 1)[-1], ())
            if found.as_posix().endswith(tail)]


def _citation_issues(
    tid: str, prose: str, roots: Sequence[Path],
) -> tuple[list[tuple[str, str, str]], int]:
    """본문 `파일:줄` 인용 판정 → (차단 이슈, 판정불능 개수).

    디렉터리 구분자를 가진 인용만 판정한다 — basename 단독(`board.py:11726` 형태)은 출하
    템플릿·백업 트리에 같은 이름의 사본이 여럿이라 어느 파일인지 확정할 수 없다. 그런 인용은
    판정불능으로 **세어서** 출력에 싣고 통과로 위장하지 않는다.

    해소는 두 단계다. repo 루트 기준(exact)으로 없으면 하위 디렉터리 기준 표기
    (`wiki/decisions/x.md` = `.project_manager/wiki/decisions/x.md`)로 보고 소유 트리에서
    그 경로로 끝나는 파일을 찾는다 — **유일하게** 해소될 때만 그 파일의 줄 범위를 검사하고,
    사본이 여럿이면 어느 파일의 줄 수인지 확정할 수 없어 판정불능이다. 두 표기 어느 쪽으로도
    실재 파일이 없으면 **차단**한다: 있지도 않은 파일을 근거로 단 인용이 이 게이트가 막으려는
    것이라, 표기 편의를 이유로 통과시키면 정탐이 0 이 된다.

    같은 인용이 본문에 여러 번 나오면 한 번만 판정한다(같은 사실을 여러 줄로 내지 않는다).
    """
    issues: list[tuple[str, str, str]] = []
    unverifiable = 0
    seen: set[str] = set()
    scanned: list[tuple[str, str, int, Path | None]] = []
    for match in _CITATION_RE.finditer(prose):
        citation = f"{match.group('path')}:{match.group('line')}"
        if citation in seen:
            continue
        seen.add(citation)
        path = match.group("path").replace("\\", "/")
        if "/" not in path or path.startswith(_OFF_TREE_CITATION_PREFIXES):
            unverifiable += 1
            continue
        scanned.append((citation, path, int(match.group("line")),
                        _exists_in_roots(path, roots)))
    # 트리 순회는 exact 로 못 찾은 인용이 있을 때만 — 정상 인용만 있는 티켓은 비용 0.
    names = {path.rsplit("/", 1)[-1] for _c, path, _l, found in scanned if found is None}
    index = _citation_suffix_index(names, roots) if names else {}
    for citation, path, line, found in scanned:
        if found is None:
            matches = _citation_suffix_matches(path, index)
            if len(matches) > 1:
                unverifiable += 1
                continue
            if not matches:
                issues.append((tid, _CITATION_KIND,
                               f"인용 `{citation}` 의 파일이 소유 트리에 없다 (repo 루트 "
                               f"기준·하위 디렉터리 기준 어느 표기로도 해소되지 않는다) — "
                               f"경로를 실측해 고쳐라"))
                continue
            found = matches[0]
        try:
            total = len(fs.read_text_shared(
                found, encoding="utf-8", errors="replace").splitlines())
        except OSError:
            unverifiable += 1
            continue
        if line < 1 or line > total:
            issues.append((tid, _CITATION_KIND,
                           f"인용 `{citation}` 이 줄 범위를 넘는다 (그 파일은 {total}줄) — "
                           f"실측값으로 고쳐라"))
    return issues, unverifiable


def _architect_round_issues(
    home,
    tid: str, body: str, design: Any,
) -> list[tuple[str, str, str]]:
    """`design: required|done` 티켓의 architect 점검 라운드 회수 판정 (차단).

    초안은 PM 이 쓰고 실측 대조·cross-module 검증은 architect 가 한다(generate ≠ evaluate).
    그 점검이 라운드로 회수되기 전에 승격하면 본문의 사실성을 아무도 안 본 채 공유 보드에
    올라간다 — 형식 게이트가 통과시키는 결함(틀린 인용·티켓 간 충돌·범위 오판)이 그대로 남는다.

    "산출이 있나" 판정은 라운드 사이드카 소유(`load_rounds` 가 매기는 역할·`round_is_pending`
    가 매기는 시드 잔존)를 그대로 쓴다 — 여기서 규칙을 다시 만들지 않는다. `n/a`
    (필드 부재 포함)는 설계 단계 비대상이라 무판정이다.
    """
    state = design_rules._design_state(design)
    if state not in (design_rules.DESIGN_REQUIRED, design_rules.DESIGN_DONE):
        return []
    rounds = round_seed.load_rounds(pm_paths.tickets_dir(home), tid, ticket_text=body)
    architect_rounds = [item for item in rounds if item.role == _ARCHITECT_ROLE]
    if not architect_rounds:
        return [(tid, _ARCHITECT_ROUND_KIND,
                 f"design: {state} 인데 {_ARCHITECT_ROLE} 점검 라운드가 없다 — 초안을 "
                 f"실측 대조(인용·touches·다른 열린 티켓과의 충돌·최소 수단)로 점검받고 "
                 f"본문에 반영한 뒤 승격하라")]
    if all(round_seed.round_is_pending(item, ticket_text=body)
           for item in architect_rounds):
        return [(tid, _ARCHITECT_ROUND_KIND,
                 f"{_ARCHITECT_ROLE} 점검 라운드가 시드 그대로다(산출 없음) — 회수 전에는 "
                 f"승격하지 않는다")]
    return []


def _content_fact_issues(
    home,
    tid: str, body: str, *, touches: Any, design: Any,
) -> list[tuple[str, str, str]]:
    """승격 순간의 사실성 판정 묶음 — 형식 게이트와 같은 `(tid, kind, detail)` 모양."""
    roots = _content_search_roots(home)
    # 인용 자체가 코드 span 표기라 `_strip_code`(span 까지 제거)를 쓰면 판정 대상이 사라진다 —
    # 펜스만 지운다(펜스 안 예시 블록의 경로는 본문 인용이 아니다).
    prose = markdown_text._FENCED_CODE_RE.sub(" ", body)
    issues: list[tuple[str, str, str]] = []
    issues.extend(_architect_round_issues(home, tid, body, design))
    citation_issues, citation_unverifiable = _citation_issues(tid, prose, roots)
    issues.extend(citation_issues)
    touch_issues, touch_unverifiable = _touches_existence_issues(home, tid, touches, roots)
    issues.extend(touch_issues)
    # 축별로 적는다 — 0 인 축까지 세면 "touches 좌표 0건" 이 판정불능 0 이 아니라 touches
    # 인식 0 으로 읽힌다(실제로는 전부 해소된 정상 티켓이다).
    axes = [
        (touch_unverifiable, f"touches 좌표 {touch_unverifiable}건"),
        (citation_unverifiable,
         f"basename 단독/트리 밖 인용 {citation_unverifiable}건"),
    ]
    counted = [detail for count, detail in axes if count]
    if counted:
        issues.append((tid, _UNVERIFIABLE_KIND,
                       "판정불능 — " + " · ".join(counted)
                       + " (게이트 시야 밖 · 통과로 세지 않는다)"))
    return issues


def _body_lint_issues(home, tid: str, body: str, *,
                      strict_sections: bool = False,
                      design: Any = None,
                      touches: Any = None,
                      content_facts: bool = False) -> list[tuple[str, str, str]]:
    """단일 티켓 본문의 self-containment issue — `lint_bodies` 와 authoring 게이트가 공유.

    `lint_bodies` 의 검사 로직(placeholder·thin)을 단일-티켓 단위로 추출한 것 — `board.py new`
    발행 게이트·`board.py promote` 승격 게이트가 방금/승격 대상 티켓 하나를 즉석 검사할 때 재사용한다.

    `strict_sections`: authoring 게이트(발행·승격)만 True — 5절(목표/인터페이스/결정/DoD/참고) 전부의
    존재를 강제한다(절 삭제 회피 차단). False(기본·전역 `lint_bodies` 경로)는 3절 불변
    (`_REQUIRED_SECTIONS`)만 요구 — 인터페이스/결정 절 없는 레거시 티켓의 blast-radius 회피.
    placeholder 검사는 두 모드 공통(단일 규칙).

    `design`: 그 티켓 frontmatter 의 `design:` 값(None=필드 부재=`n/a`). `required`/`done` 일
    때만 `## 설계` 절 충전을 함께 판정한다(kind=`design-pending`) — 값을 안 넘기는 호출자는
    설계 판정 없이 기존 검사만 받는다(레거시 호출 무변경).

    `content_facts`: 승격 게이트(`promote`)만 True — 본문이 *주장하는 사실*(touches 경로 실재·
    `파일:줄` 인용·architect 점검 라운드 회수)을 함께 판정한다(`_content_fact_issues`).
    `touches` 는 그 판정의 입력이다(frontmatter 값·None=빈 목록). 전역 lint 와 발행 게이트는
    켜지 않는다 — 이미 open 인 티켓을 소급 판정하지 않고, 발행 시점 본문은 템플릿이라 인용이
    없다. 형식 판정(placeholder·thin·design)은 이 플래그와 무관하게 그대로다.
    """
    issues: list[tuple[str, str, str]] = []
    prose = markdown_text._strip_code(body)
    for placeholder in _PLACEHOLDERS:
        if placeholder in prose:
            issues.append((tid, "placeholder",
                           f"unfilled template text: {placeholder!r}"))
    required = _STRICT_REQUIRED_SECTIONS if strict_sections else _REQUIRED_SECTIONS
    for section in required:
        if section not in body:
            issues.append((tid, "thin",
                           f"missing standard section: {section}"))
    issues.extend(design_rules._design_issues(tid, body, design))
    if content_facts:
        issues.extend(_content_fact_issues(home, tid, body, touches=touches, design=design))
    return issues


def lint_bodies(home) -> list[tuple[str, str, str]]:
    """Lint open/claimed ticket bodies for self-containment.

    Checks:
      - placeholder: unfilled `_template.md` text still present as prose
      - thin:        a standard section (목표 / 완료 조건 / 참고) is missing
      - design-pending: 설계 단계(`design: required`) 미완 — 설계 절 미충전/필드 미승격
        (advisory·never-block. 차단은 그 티켓을 실제로 집는 claim 게이트가 한다.)
      - design-estimate: `estimate=large` 인데 설계 단계가 비대상(`n/a`) — 발행 후 estimate 를
        올린 티켓의 설계 게이트 재유도 (advisory·never-block).

    done/blocked tickets are skipped — only live, claimable work is gated.
    """
    issues: list[tuple[str, str, str]] = []
    for status in ("open", "claimed"):
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, body = loaded
            tid = fm.get("id") or p.name
            issues.extend(_body_lint_issues(home, tid, body, design=fm.get("design")))
            issues.extend(design_rules._design_estimate_advisory(
                tid, fm.get("estimate"), fm.get("design")))
    return issues


def lint_ideas(home) -> list[tuple[str, str, str]]:
    """Lint ideas for frontmatter `status` ↔ directory agreement.

    The directory is the source of truth; a mismatched frontmatter `status`
    means a manual `mv` bypassed board.py (drift — see ideas/README.md).
    """
    issues: list[tuple[str, str, str]] = []
    for status in ticket_rules.IDEA_STATUS_DIRS:
        for p in sorted((pm_paths.ideas_dir(home) / status).glob("[0-9]*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            iid = fm.get("id") or p.name
            fm_status = fm.get("status")
            if fm_status != status:
                issues.append((iid, "idea-status",
                               f"in {status}/ but frontmatter status={fm_status!r}"))
    return issues
