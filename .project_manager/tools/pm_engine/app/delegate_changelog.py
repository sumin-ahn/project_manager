"""완료 티켓 재료 수집과 리뷰 확인 (층: app)."""
from __future__ import annotations

from typing import Callable
from collections.abc import Mapping
from typing import NamedTuple
from pathlib import Path
from collections.abc import Sequence
import datetime
from pm_engine.infra import delegate_copy_git
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_run_rules
from pm_engine.infra import fs
from pm_engine.domain import markdown_text
from pm_engine.infra import pm_paths
from pm_engine.infra import review_command
from pm_engine.domain import review_contract
from pm_engine.app import review_evidence
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.app import round_seed
import sys
from pm_engine.infra import ticket_store


# ── CHANGELOG 재료 추출 (완료 티켓 본문 → 릴리즈 노트 재료) ────────────────────
#
# 릴리즈 노트의 **문안은 사람이 쓴다**. 이 표면이 내는 것은 재료뿐이다 — 어떤 티켓이 이 릴리즈
# 구간에 들어갔고, 그 티켓이 자기 본문에서 무엇을 하겠다고 선언했는가. 분류(Added/Changed/…)는
# **후보**만 세운다: 확정은 채택자 관점의 판단이고, 기계가 그 판단을 대신하면 아무도 읽고 고르지
# 않은 문장이 그대로 릴리즈 노트가 된다.
#
# 구간의 기준은 **코드 저장소의 rev 시각**이다. board 는 PM 홈에 있고 태그는 코드 저장소에 있어
# 두 좌표를 잇는 규칙이 하나 필요하다 — 태그·rev 를 코드 체크아웃에서 커밋 시각으로 바꾸고, 그
# 시각 이후에 완료된 티켓을 고른다. 코드 체크아웃이나 rev 를 해소하지 못하면 빈 목록이 아니라
# rc≠0 이다(조용한 빈 손은 곧 릴리즈 노트 누락이다).
#
# 이 표면은 board 를 **읽기만** 한다 — 순회는 board 의 공용 strict 로더(그 안에서 공유 읽기
# seam)를 쓰고 어떤 티켓 파일도 쓰지 않는다. done 티켓 하나가 손상돼 있으면 그 경로를 찍고
# 멈춘다: 건너뛴 티켓은 출력에서 '원래 없던 재료'와 구별되지 않는다.

# 코드 체크아웃 좌표를 담은 per-clone conf 키 — 엔진 갱신이 쓰는 그 키 하나다(사본 0).
CHANGELOG_UPSTREAM_KEY = "upstream.path"
# 재료로 싣는 본문 절 — 무엇을(목표)·왜 그렇게(결정)·무엇으로 끝났나(완료 조건).
CHANGELOG_MATERIAL_SECTIONS: tuple[str, ...] = ("## 목표", "## 결정", "## 완료 조건")
# 분류 **후보** 신호. 확정이 아니라 후보라 겹치면 겹친 대로 전부 싣는다(선언 순서 보존).
CHANGELOG_CATEGORY_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Added", ("신설", "추가", "도입")),
    ("Changed", ("교체", "변경", "개편", "전환")),
    ("Removed", ("폐지", "제거", "삭제")),
    ("Fixed", ("결함", "버그", "오류", "회귀")),
)
# 채택자 트리에서 무엇이 달라지는가를 말하는 줄의 신호 — 그 줄을 원문 그대로 인용한다.
CHANGELOG_ADOPTER_SIGNALS: tuple[str, ...] = (
    "채택자", "마이그레이션", "기본값", "호환", "폐지", "출하", "업그레이드",
)
# 인용 상한 — 재료는 사람이 읽는 것이라 티켓 하나가 화면을 통째로 먹으면 안 된다.
CHANGELOG_ADOPTER_QUOTE_MAX = 5
_CHANGELOG_BLOCK_HEADER = "## {ticket} — {title}"
_CHANGELOG_EMPTY = "(없음)"
_CHANGELOG_CATEGORY_JOIN = " · "


class ChangelogMaterial(NamedTuple):
    """완료 티켓 하나의 재료 블록 — 판단이 아니라 인용과 후보만 담는다."""

    ticket: str
    title: str
    completed_at: str
    categories: tuple[str, ...]
    adopter_quotes: tuple[str, ...]
    sections: tuple[tuple[str, str], ...]


def _changelog_instant(value: object, *, source: str) -> datetime.datetime:
    """시각 값 → 비교 가능한 시각 — 판독 불가는 그 자리(`source`)를 찍고 터진다.

    tz 없는 값은 UTC 로 읽는다: offset 이 빠진 값도 시각으로는 읽히므로 판독 실패가 아니다
    (board 가 기록하는 값은 offset 을 달고 있다).
    """
    parsed = (value if isinstance(value, datetime.datetime)
              else delegate_run_rules._parse_ledger_timestamp(value))
    if parsed is None:
        raise delegate_errors.DelegateError(f"시각을 읽지 못했습니다: {source} → {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _changelog_required(value: object, *, field: str, path: Path) -> str:
    """완료 티켓의 필수 값 — 없거나 비었으면 그 경로를 찍고 터진다(파일명 폴백 없음)."""
    text = str(value or "").strip()
    if not text:
        raise delegate_errors.DelegateError(f"완료 티켓의 `{field}` 가 비어 있습니다: {path}")
    return text


def changelog_code_checkout(home, conf: Mapping[str, str], ) -> Path:
    """재료 구간의 rev 를 해소할 코드 체크아웃 — 미해소는 fail-loud."""
    pm_home = home.local_dir.parent.parent
    raw = str(conf.get(CHANGELOG_UPSTREAM_KEY, "") or "").strip()
    if not raw:
        raise delegate_errors.DelegateError(
            f"코드 체크아웃을 해소하지 못했습니다 — {pm_home}/.project_manager/local.conf 의 "
            f"`{CHANGELOG_UPSTREAM_KEY}=` 가 비어 있습니다"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = pm_home / path
    resolved = path.resolve()
    if not resolved.is_dir():
        raise delegate_errors.DelegateError(
            f"코드 체크아웃이 로컬 디렉터리가 아닙니다: {raw} — rev 시각은 로컬 체크아웃에서만 "
            f"해소합니다(`{CHANGELOG_UPSTREAM_KEY}=` 를 코드 체크아웃 경로로 두세요)"
        )
    return resolved


def changelog_since_instant(
    checkout: Path, rev: str, *, git_run_fn: Callable | None = None,
) -> datetime.datetime:
    """`--since` 의 태그·rev → 그 커밋 시각(해소 실패는 fail-loud).

    git 실행은 이 모듈이 이미 가진 조회 seam 하나를 그대로 쓴다(runner 사본 0).
    """
    try:
        raw = delegate_copy_git._cluster_git(
            checkout, "log", "-1", "--format=%cI", rev, git_run_fn=git_run_fn,
        ).strip()
    except delegate_errors.DelegateError as exc:
        raise delegate_errors.DelegateError(
            f"rev 를 해소하지 못했습니다: {rev} (체크아웃 {checkout}) — {exc}"
        ) from exc
    return _changelog_instant(raw, source=f"rev {rev} (체크아웃 {checkout})")


def _changelog_categories(text: str) -> tuple[str, ...]:
    """분류 후보 — 신호가 하나도 없으면 빈 튜플(렌더가 없음으로 표시한다)."""
    return tuple(
        label for label, signals in CHANGELOG_CATEGORY_SIGNALS
        if any(signal in text for signal in signals)
    )


def _changelog_adopter_quotes(text: str) -> tuple[str, ...]:
    """채택자 영향을 말하는 줄의 원문 인용 — 상한까지만."""
    quotes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*+ ").strip()
        if not stripped:
            continue
        if any(signal in stripped for signal in CHANGELOG_ADOPTER_SIGNALS):
            quotes.append(stripped)
        if len(quotes) >= CHANGELOG_ADOPTER_QUOTE_MAX:
            break
    return tuple(quotes)


def _changelog_section_text(body: str, heading: str, *, path: Path) -> str:
    """절 본문 — 절 경계 규칙은 board 의 슬라이서 하나가 소유한다(사본 0).

    근거 절이 없거나 비었으면 그 경로와 절 이름을 찍고 터진다 — 빈 값으로 실으면 그 티켓의
    근거가 사라진 것과 재료가 원래 없는 것이 같은 출력이 된다.
    """
    section = markdown_text._section_text(body, heading)
    text = "" if section is None else "\n".join(
        line.rstrip() for line in section.strip("\n").splitlines()
    ).strip("\n")
    if not text:
        raise delegate_errors.DelegateError(f"완료 티켓의 `{heading}` 절이 없거나 비어 있습니다: {path}")
    return text


def changelog_material(home, *, since: datetime.datetime) -> list[ChangelogMaterial]:
    """`since` 이후에 완료된 done 티켓의 재료 — 완료 시각 오름차순.

    done 티켓은 strict 로더로 읽고 필수 값(완료 시각·id·제목·근거 절)을 전부 요구한다. 하나라도
    없거나 판독 불가면 그 경로를 찍고 터진다 — 건너뛰면 '손상된 완료 티켓'과 '구간에 재료가
    없음'이 같은 빈 stdout·rc 0 이 돼 릴리즈 노트 누락이 조용히 생긴다. 재료에서 빠지는 유일한
    사유는 구간 밖(경계 포함) 완료다.
    """
    materials: list[tuple[datetime.datetime, ChangelogMaterial]] = []
    for path in sorted((pm_paths.tickets_dir(home) / "done").glob("T-*.md")):
        fm, body = ticket_store.load_ticket(path)
        completed = _changelog_instant(
            fm.get("completed_at"), source=f"완료 티켓 `completed_at`: {path}",
        )
        if completed <= since:
            continue
        sections = tuple(
            (heading.removeprefix("## ").strip(),
             _changelog_section_text(body, heading, path=path))
            for heading in CHANGELOG_MATERIAL_SECTIONS
        )
        joined = "\n".join(text for _label, text in sections)
        materials.append((completed, ChangelogMaterial(
            ticket=_changelog_required(fm.get("id"), field="id", path=path),
            title=_changelog_required(fm.get("title"), field="title", path=path),
            completed_at=str(fm.get("completed_at")),
            categories=_changelog_categories(joined),
            adopter_quotes=_changelog_adopter_quotes(joined),
            sections=sections,
        )))
    materials.sort(key=lambda item: (item[0], item[1].ticket))
    return [material for _instant, material in materials]


def render_changelog_material(materials: Sequence[ChangelogMaterial]) -> str:
    """재료 블록 렌더 — 재료가 없으면 빈 문자열(빈 손은 오류가 아니다)."""
    lines: list[str] = []
    for material in materials:
        lines.append(_CHANGELOG_BLOCK_HEADER.format(
            ticket=material.ticket, title=material.title))
        lines.append(f"- 완료: {material.completed_at}")
        categories = (_CHANGELOG_CATEGORY_JOIN.join(material.categories)
                      if material.categories else _CHANGELOG_EMPTY)
        lines.append(f"- 분류 후보: {categories}")
        if material.adopter_quotes:
            lines.append("- 채택자 영향 인용:")
            lines.extend(f"  - {quote}" for quote in material.adopter_quotes)
        else:
            lines.append(f"- 채택자 영향 인용: {_CHANGELOG_EMPTY}")
        for label, text in material.sections:
            lines.append(f"- 근거 · {label}:")
            lines.extend(f"  {line}" if line else "" for line in text.splitlines())
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def _ticket_spec_and_rounds(home, ticket: str) -> tuple[str, list]:
    """확인·처분 판정 입력 — 명세 원문과 그 티켓의 라운드 목록."""
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
    _status, path = found
    try:
        spec_text = fs.read_text_shared(
            path, encoding="utf-8", newline="",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"티켓 명세 읽기 실패: {path}: {exc}") from exc
    return spec_text, round_seed.load_rounds(
        pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
    )


def _resolve_ticket_pm_verified(
    home, ticket: str, *, cluster: str, tree: Path,
    run_fn: Callable | None = None,
) -> int:
    """티켓 하나의 리뷰 확인 입력 preflight → terminal 확인 기입 → 완료 판정 재확인.

    순서가 계약이다: (1) 입력 preflight (2) machine command/PM-owned terminal 확인 생성
    (3) 확인 절 기입 (4) 명세를 **다시 읽어** 완료 판정의 리뷰 잔여가 열렸는지 확인한다.
    판정 입력은 리뷰 라운드 파일과 명세 PM 영역뿐이다(다른 기록을 쓰지 않는다).
    """
    owner = home.local_dir.parent.parent
    try:
        spec_text, rounds = _ticket_spec_and_rounds(home, ticket)
        preflight_problem = review_state.review_resolution_input_problem(spec_text, rounds)
        if preflight_problem is not None:
            raise delegate_errors.DelegateError(f"리뷰 확인 입력 preflight 실패: {preflight_problem}")
        residual = review_state.review_residual(rounds)
        if residual is not None and residual.must_fix == 0:
            print(f"{ticket}: 처분할 리뷰 잔여 없음(must-fix 0건) — 무대상")
            return 0
        template = review_state.pm_review_verify_template(spec_text, rounds)
        rows = review_command.run_pm_review_confirmations(template, cwd=tree, run_fn=run_fn)
        if rows:
            review_evidence.append_pm_review_confirmation(
                home, ticket, review_contract.render_pm_review_confirmation_section(rows), rounds=rounds,
            )
            unresolved = [row.id for _round, row in rows if row.status != "resolved"]
            pm_owned_count = len(template.pm_owned_rows)
            if pm_owned_count:
                machine_count = len(rows) - pm_owned_count
                print(
                    f"{ticket}: 기계 확인 {machine_count}건 + PM-owned terminal 확인 "
                    f"{pm_owned_count}건 기입 (미해소 {len(unresolved)}"
                    f"{': ' + ', '.join(unresolved) if unresolved else ''}) · 실행 트리={tree}"
                )
            else:
                print(
                    f"{ticket}: 기계 확인 {len(rows)}건 기입 "
                    f"(미해소 {len(unresolved)}"
                    f"{': ' + ', '.join(unresolved) if unresolved else ''}) "
                    f"· 실행 트리={tree}"
                )
        else:
            print(f"{ticket}: 기계 확인 대상 0건 — 실행 없이 완료 판정으로 넘어갑니다")
        spec_text, rounds = _ticket_spec_and_rounds(home, ticket)
        completion_problem = review_state.review_completion_problem(spec_text, rounds)
        if completion_problem is not None:
            raise delegate_errors.DelegateError(
                f"확인 기입 뒤에도 리뷰 잔여가 완료 판정을 막습니다: {completion_problem}"
            )
    except review_form.PMReviewError as exc:
        print(f"오류: 리뷰 잔여 확인 실패[{exc.code}]: {ticket}: {exc}", file=sys.stderr)
        return 1
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: 리뷰 잔여 확인 실패: {ticket}: {exc}", file=sys.stderr)
        return 1
    print(f"{ticket}: 리뷰 잔여 확인 완료 — 완료 판정의 리뷰 잔여 없음 · PM 홈={owner}")
    return 0
