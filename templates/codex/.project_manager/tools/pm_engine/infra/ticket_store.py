"""티켓 파일 조회·읽기 (층: infra).

canonical ID 정확 일치 조회(`find_ticket_exact`)와 frontmatter 읽기(`load_ticket`·`load_ticket_soft`).
문법은 `pm_engine.domain.ticket_rules` 가 소유한다.
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pm_engine.domain import ticket_rules
from pm_engine.infra import fs, pm_paths


def _ticket_search_dirs(home) -> list[tuple[str, Path]]:
    """티켓 조회 범위 — (status 라벨, 디렉토리) 순서쌍. STATUS_DIRS → draft 격리 디렉토리."""
    return [*((status, pm_paths.tickets_dir(home) / status) for status in ticket_rules.STATUS_DIRS),
            ("draft", pm_paths.drafts_dir(home))]


def find_ticket_exact(
    home, tid: str, *, search_dirs: Sequence[tuple[str, Path]] | None = None,
) -> tuple[str, Path] | None:
    """canonical ID 가 **정확히** `tid` 인 티켓의 (status, path) — 없으면 None (공용 seam).

    `{tid}-*.md` **prefix glob 의 첫 매칭**을 티켓-ID 조회로 쓰면 legacy ID(`T-NNNN`)가 같은
    번호로 시작하는 prefixed ID(`T-NNNN-001`)에 오인 매칭된다 — 어느 쪽이 잡힐지는 디렉토리
    순서가 정하고, 그 결과 회귀 강등 스코프·완료 게이트가 **무관한 티켓의
    frontmatter** 를 보게 된다(전부 조용한 오판). 그래서 후보 전체를 훑어 frontmatter canonical
    ID(`_canonical_ticket_id`)가 정확히 일치하는 것만 인정한다.

    이 판정을 쓰는 표면이 board 밖에도 있어(추가 리뷰어 티켓 조회·`ticket_finish` 티켓 로드)
    공용 seam 으로 둔다 — 사본을 만들면 그 사본이 다시 첫-매칭으로 흘러 half-fix 가 재발한다.
    `search_dirs` 를 주면 그 (status 라벨, 디렉토리) 순서로만 본다 — 다른 PM 홈의 보드를 읽는
    호출부(`additional_reviewer --pm-home`)가 자기 범위를 명시하는 자리다. 미지정이면 이 board 의
    STATUS_DIRS → drafts.
    """
    for status, directory in (search_dirs if search_dirs is not None
                              else _ticket_search_dirs(home)):
        for path in sorted(Path(directory).glob(f"{tid}-*.md")):
            if _canonical_ticket_id(path) == tid:
                return status, path
    return None


def load_ticket(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string)."""
    return ticket_rules._parse_ticket_text(fs.read_text_shared(path, encoding="utf-8"), path)


# 손상 frontmatter fail-soft — **디렉토리 순회** 전용 로더.
#
# 한 티켓의 YAML 이 깨지면(실측: 콜론을 인용 없이 쓴 스칼라 값) 그 파일을
# 읽는 순간 예외가 나고, 순회 소비자(`list`·`lint`·`refresh`)가 통째로 traceback 으로 죽는다 —
# 보드 전체가 한 파일 때문에 조회 불능이 되는 클래스다. 순회에서는 그 티켓만 건너뛰되 **조용히
# 넘기지 않는다**(경고 1줄 + 경로 + 사유). 지정 대상 mutation(claim/complete/block/…)은 그대로
# `load_ticket` 을 써 fail-loud 다 — 고치라고 지목받은 파일이 조용히 무시되면 안 된다.
# PyYAML 은 예외가 났을 때 읽는다(`ticket_rules._parse_ticket_text` 와 같은 이유).
def _load_ticket_soft_errors() -> tuple[type[Exception], ...]:
    import yaml

    return (OSError, UnicodeError, ValueError, yaml.YAMLError)


def load_ticket_soft(home, path: Path) -> tuple[dict[str, Any], str] | None:
    """순회용 `load_ticket` — 읽기/파싱 실패면 경고 1줄 뒤 None (그 티켓만 skip).

    frontmatter 가 dict 가 아닌 형상(스칼라·리스트)도 같은 축으로 접는다 — 뒤따르는 `fm.get(...)`
    이 AttributeError 로 터지면 fail-soft 가 반쪽이다.
    """
    try:
        fm, body = load_ticket(path)
    except _load_ticket_soft_errors() as exc:
        print(f"경고: 티켓을 읽지 못해 건너뜁니다 — {pm_paths._rel_to_repo(home, path)} "
              f"({type(exc).__name__}: {exc})\n"
              "  · frontmatter YAML 문법을 확인하세요 (콜론 포함 값은 인용 필요 — "
              'title: "a: b").', file=sys.stderr)
        return None
    if not isinstance(fm, dict):
        print(f"경고: 티켓 frontmatter 가 매핑이 아니라 건너뜁니다 — {pm_paths._rel_to_repo(home, path)} "
              f"({type(fm).__name__})", file=sys.stderr)
        return None
    return fm, body


def _canonical_ticket_id(path: Path) -> str | None:
    """티켓의 canonical ID — frontmatter `id:` 1차 진실·파일명 파싱은 폴백 (숫자-slug 모호성 해소).

    파일명 파서(`_ticket_id_from_filename`)는 legacy `T-NNNN` + **숫자로 끝나는 slug**
    (`T-0036-fix-123.md`)를 prefixed ID `T-0036-fix-123` 으로 오인한다(하이픈-마디 grammar 모호성·
    `dump_ticket`/`_next_id` 이 발행 시 확정해 쓰는 frontmatter `id:` 가
    권위 진실이므로 이를 우선하고, 부재/읽기 실패(비-UTF-8·frontmatter 깨짐) 시에만 파일명 파싱으로
    폴백한다(fail-soft). `_scan_prefix_tickets` 의 `fm.get("id") or …` 와 동형 semantics — 스캔측과
    rename-planning 측이 같은 canonical 해소를 쓰게 해 파일명↔content 불일치를 원천 차단한다.

    **frontmatter 가 매핑이 아닌 형상**(스칼라·리스트)도 같은 축으로 접는다(`load_ticket_soft`
    규약 — 손상 = 없는 것으로 보고 파일명 폴백). YAML 이 비어 있지 않은 스칼라면 파싱은 성공하고
    `fm.get` 이 AttributeError 로 터지는데, 이 함수는 정확-일치 조회(`find_ticket_exact`)의
    후보 순회 안에서 불리므로 그 예외가 **활성 리뷰 판정·강등 판정보다 먼저** 실행을 죽인다
    (손상 티켓은 skip 하고 FULL 로 남긴다는 규약이 깨진다).
    """
    try:
        fm, _ = load_ticket(path)
    except _load_ticket_soft_errors():  # 읽기/파싱 실패는 파일명 폴백(fail-soft·비-UTF-8 등).
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    fid = fm.get("id")
    if isinstance(fid, str) and fid:
        return fid
    return ticket_rules._ticket_id_from_filename(path.name)


def _all_tickets(home) -> list[tuple[str, dict]]:
    """[(status, frontmatter), ...] for every ticket regardless of dir.

    손상 티켓은 경고 1줄과 함께 빠진다(`load_ticket_soft`) — 한 파일이 lint 전체를 죽이지 않는다.
    """
    out: list[tuple[str, dict]] = []
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = load_ticket_soft(home, p)
            if loaded is None:
                continue
            out.append((status, loaded[0]))
    return out
