"""ADR 문서 형식 규칙 (층: domain).

ADR ID·파일명·slug, 새 ADR frontmatter·본문 골격, 대상 ADR 의 수명주기 back-ref 치환, decisions/README.md
색인 표 갱신, log decide 항목 골격을 소유한다.
"""
from __future__ import annotations

import re

import yaml


# 개정 동사 → (대상 status, back-ref 필드) 매핑 (lint_adr_lifecycle 와 동형).
# `refines`(추가·대상 불변)는 back-ref 대상이 아니다 — related 링크만 남기고 대상 미개정.
_LIFECYCLE_VERBS: dict[str, tuple[str, str]] = {
    "amends": ("amended", "amended_by"),
    "supersedes": ("superseded", "superseded_by"),
}

# README 섹션 헤더 부분일치 앵커 — `## Accepted (...)` 등. 헤더 뒤 괄호 부연은 무시.
_ACCEPTED_HEADER_RE = re.compile(r"^##\s+Accepted\b", re.IGNORECASE)
_AMENDED_HEADER_RE = re.compile(r"^##\s+Amended\b", re.IGNORECASE)
_SUPERSEDED_HEADER_RE = re.compile(r"^##\s+Superseded\b", re.IGNORECASE)
_ANY_H2_RE = re.compile(r"^##\s+")

# 표 데이터 행: `| [NNNN](...) | ... |`.
_TABLE_ROW_RE = re.compile(r"^\|\s*\[(\d{3,4})\]")

# ADR id 정규화 — `ADR-NNNN`·소문자 `adr-` 접두·숫자만(선행 0 유무 무관) 모두 정수로.
_ADR_ID_RE = re.compile(r"(?:ADR-)?0*(\d+)$", re.IGNORECASE)


def adr_id(n: int) -> str:
    """정수 → canonical `ADR-NNNN` (4자리 zero-pad)."""
    return f"ADR-{n:04d}"


def parse_adr_num(token: str) -> int:
    """`ADR-NNNN`·숫자만(선행 0 유무 무관) → 정수. 형식 어긋나면 ValueError(발행 인자는 fail-loud)."""
    m = _ADR_ID_RE.match(str(token).strip())
    if not m:
        raise ValueError(f"ADR id 형식 어긋남: {token!r} (기대 'ADR-NNNN' 또는 정수)")
    return int(m.group(1))


# slug 는 `NNNN-<slug>.md` 파일명 컴포넌트가 된다 — 무검증 값이 파일명 경계로 새면 path 주입/
# traversal(디렉토리 밖 쓰기·`..`) 위험이라 CLI 입력 단계에서 부작용 이전 거부한다(identity_args.
# validate_task_name 파일명 안전 validator 클래스). 허용 = 영문 소문자로 시작·이어서
# 소문자/숫자/하이픈/언더스코어(slug 관례·파일명·URL 안전). 한글/공백/`:`/대문자/dot 는 slug 밖(제목은
# --title 에 자유·slug 는 협소).
_VALID_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_slug(slug: str) -> None:
    """ADR slug(파일명 컴포넌트) 검증 — 위반 시 ValueError(fail-loud·validator 클래스 동형).

    path separator(`/`·`\\`)·`..`·공백·선행 `.`·대문자·dot·특수문자를 거부한다(파일 주입/traversal
    방지). `_VALID_SLUG_RE`(영문 소문자 시작·소문자/숫자/하이픈/언더스코어)가 그 도메인을 협소화한다 —
    거부 케이스 전부(`../x`·`a/b`·`a b`·`.hidden`·`A`·`a.b`)가 첫 글자 또는 후속 문자에서 미매치."""
    if not slug or not slug.strip():
        raise ValueError("slug 빈 값(공백 포함) — `NNNN-<slug>.md` 파일명 컴포넌트 필수")
    if not _VALID_SLUG_RE.match(slug):
        raise ValueError(
            f"slug '{slug}' 형식 어긋남 — 영문 소문자로 시작·소문자/숫자/하이픈/언더스코어만 "
            "(path separator·`..`·공백·선행 `.`·대문자·dot·특수문자 불가·파일 주입/traversal 방지)")


# ── 2. 신규 ADR 파일 scaffold ───────────────────────────────────────────────

_FRONTMATTER_TEMPLATE = """\
---
title: {title}
created: {date}
updated: {date}
author: {author}
type: decision
status: {status}
scope: {scope}
{lifecycle}related: {related}
tags: {tags}
---
"""
_BODY_TEMPLATE = """
# {adr_id} — {title}

> <요약 — PM 서술: 무엇을 왜 결정했나 (1~3줄).>

## Context

- <배경·문제 — PM 서술.>

## Decision

- <결정 내용 — PM 서술.>

## Consequences

- <결과(+/−)·후속 — PM 서술.>

## References

- <참고: sealed spike·관련 ADR·메모리 — PM 서술.>
"""


def _flow_list(items: list[str]) -> str:
    """리스트를 flow-style 문자열로 (`a, b, c`) — 기존 ADR frontmatter 스타일."""
    return ", ".join(items)


def _yaml_value(val) -> str:
    """단일 값을 YAML-safe inline 표현으로 직렬화한다 (scalar 는 필요 시 quoting·list 는 flow `[a, b]`).

    frontmatter 를 문자열 템플릿으로 직조할 때 값을 그대로 보간하면(`title: {title}`) `--title "A: B"`
    같은 **정상** 제목의 `:`·`#`·따옴표가 frontmatter YAML 을 깨서, `ticket_store.load_ticket` 이 그 ADR 을
    파싱 못 하고 `lint_adr_lifecycle` 이 조용히 skip 한다(깨진 문서가 lint 를 통과·codex must-fix).
    yaml.safe_dump 이 값에 특수문자가 있을 때만 quoting 하므로 기존 unquoted 스타일(평범한 제목·flow
    list)은 그대로 보존되고, 위험 케이스만 안전하게 quoting 된다. 스칼라 문서 끝 마커(`...`)는 제거해
    순수 값만 남긴다(리스트 flow 표현은 `...` 로 끝나지 않음)."""
    dumped = yaml.safe_dump(val, allow_unicode=True, default_flow_style=True).strip()
    if dumped.endswith("..."):
        dumped = dumped[:-3].strip()
    return dumped


def build_frontmatter(
    *,
    title: str,
    scope: str,
    author: str,
    date: str,
    status: str,
    amends: list[int],
    supersedes: list[int],
    refines: list[int],
    related: list[str],
    tags: list[str],
) -> str:
    """신규 ADR frontmatter 문자열을 빌드한다 (flow-list·기존 스타일 보존).

    개정 동사(amends/supersedes/refines)가 있으면 그 줄을 status/scope 뒤에 flow-list 로 넣고,
    대상 id 를 related 에도 자동 편입한다(dedup·순서 보존). related/tags 는 호출자가 넘긴 그대로."""
    lifecycle_lines = ""
    verb_targets = [("amends", amends), ("supersedes", supersedes), ("refines", refines)]
    auto_related: list[str] = []
    for verb, nums in verb_targets:
        if nums:
            ids = [adr_id(n) for n in nums]
            lifecycle_lines += f"{verb}: {_yaml_value(ids)}\n"
            auto_related.extend(ids)
    # 개정 대상은 related 에도 (dedup·개정 대상 먼저·이미 있으면 skip).
    merged_related: list[str] = []
    for item in auto_related + list(related):
        if item and item not in merged_related:
            merged_related.append(item)
    # 자유-텍스트 필드(title/author/tags/related)만 _yaml_value 로 YAML-safe 직렬화 — 특수문자
    # (`:`·`#`·따옴표·한글) 안전 quoting(codex must-fix). date(ISO)·status/scope(argparse choices)는
    # 통제된 값이라 raw 로 둬 기존 unquoted 하우스 스타일(`created: 2026-07-19`·date 로 로드)을 보존한다.
    return _FRONTMATTER_TEMPLATE.format(
        title=_yaml_value(title),
        date=date,
        author=_yaml_value(author),
        status=status,
        scope=scope,
        lifecycle=lifecycle_lines,
        related=_yaml_value(merged_related),
        tags=_yaml_value(tags),
    )


def build_adr_file(
    *,
    number: int,
    title: str,
    scope: str,
    author: str,
    date: str,
    status: str,
    amends: list[int],
    supersedes: list[int],
    refines: list[int],
    related: list[str],
    tags: list[str],
) -> str:
    """신규 ADR 파일 전체 텍스트(frontmatter + 본문 골격)를 빌드한다."""
    fm = build_frontmatter(
        title=title, scope=scope, author=author, date=date, status=status,
        amends=amends, supersedes=supersedes, refines=refines,
        related=related, tags=tags,
    )
    body = _BODY_TEMPLATE.format(adr_id=adr_id(number), title=title)
    return fm + body


def adr_filename(number: int, slug: str) -> str:
    """`NNNN-slug.md` 파일명."""
    return f"{number:04d}-{slug}.md"


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """`---\\n...\\n---\\n` frontmatter 블록과 나머지를 분리한다 (없으면 None).

    ticket_store.load_ticket 동형 — 첫 닫는 `---` 로 분리. 반환은 (frontmatter_텍스트, 나머지)."""
    if not text.startswith("---\n"):
        return None
    after = text[4:]
    end = after.find("\n---\n")
    if end == -1:
        return None
    return after[:end], after[end + 1:]  # (fm_body, "---\n...")


def _apply_backref_to_frontmatter(fm_body: str, new_id: str, want_status: str, back_field: str) -> str:
    """frontmatter 텍스트(블록 내부)에 status + back-ref 를 surgical 하게 기록한다.

    - `status:` 줄 값을 want_status(amended/superseded)로 치환.
    - `<back_field>:` 줄이 있으면 그 flow-list 에 new_id 를 append(중복이면 no-op),
      없으면 status 줄 바로 뒤에 `<back_field>: [new_id]` 를 삽입한다.
    포맷(다른 줄·순서·본문)은 불변 — 정규식 라인 치환만. 멱등(이미 반영이면 무변화)."""
    lines = fm_body.split("\n")
    # status 치환.
    status_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^status:\s*", line):
            lines[i] = f"status: {want_status}"
            status_idx = i
            break
    # back-ref 필드 append or insert.
    back_idx = None
    for i, line in enumerate(lines):
        if re.match(rf"^{back_field}:\s*", line):
            back_idx = i
            break
    if back_idx is not None:
        existing = _parse_flow_list_value(lines[back_idx], back_field)
        if new_id not in existing:
            existing.append(new_id)
        lines[back_idx] = f"{back_field}: [{_flow_list(existing)}]"
    else:
        insert_at = (status_idx + 1) if status_idx is not None else len(lines)
        lines.insert(insert_at, f"{back_field}: [{new_id}]")
    return "\n".join(lines)


def _parse_flow_list_value(line: str, field: str) -> list[str]:
    """`field: [a, b]` 또는 `field: a, b` 줄에서 값 리스트를 파싱한다 (bracket·comma 흡수)."""
    val = line.split(":", 1)[1].strip()
    val = val.strip("[]").strip()
    if not val:
        return []
    return [s.strip() for s in re.split(r"[,\s]+", val) if s.strip()]


def apply_lifecycle_backref(target_text: str, new_id: str, verb: str) -> str:
    """대상 ADR 파일 텍스트에 개정 back-ref 를 기록한 새 텍스트를 반환한다.

    verb = amends/supersedes. frontmatter 블록만 surgical 편집(본문 불변). frontmatter 파싱
    불가(형식 어긋남)면 원문 그대로 반환(호출부가 warning)."""
    want_status, back_field = _LIFECYCLE_VERBS[verb]
    split = _split_frontmatter(target_text)
    if split is None:
        return target_text
    fm_body, rest = split
    new_fm = _apply_backref_to_frontmatter(fm_body, new_id, want_status, back_field)
    return f"---\n{new_fm}\n{rest}"


# ── 4. README 색인 ───────────────────────────────────────────────────────────

def _section_bounds(lines: list[str], header_re: re.Pattern[str]) -> tuple[int, int] | None:
    """README 라인 목록에서 header_re 로 시작하는 h2 섹션의 (start, end) 인덱스.

    start = 헤더 줄 index, end = 다음 `## ` 헤더 직전(또는 파일 끝). 미발견 시 None."""
    start = None
    for i, line in enumerate(lines):
        if header_re.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ANY_H2_RE.match(lines[j]):
            end = j
            break
    return start, end


def _last_table_row_index(lines: list[str], start: int, end: int) -> int | None:
    """[start, end) 구간에서 마지막 표 데이터 행(`| [NNNN]...`)의 index (없으면 None)."""
    last = None
    for i in range(start, end):
        if _TABLE_ROW_RE.match(lines[i]):
            last = i
    return last


def _find_row_index(lines: list[str], start: int, end: int, num: int) -> int | None:
    """[start, end) 구간에서 `| [NNNN]` 로 시작하는 대상 행 index (없으면 None)."""
    for i in range(start, end):
        m = _TABLE_ROW_RE.match(lines[i])
        if m and int(m.group(1)) == num:
            return i
    return None


def _row_cells(row: str) -> list[str]:
    """마크다운 표 행 → 셀 리스트 (앞뒤 `|` 제거·strip)."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def insert_accepted_row(text: str, *, number: int, slug: str, title: str, date: str, tags: list[str]) -> tuple[str, str | None]:
    """README Accepted 표에 신규 ADR 행을 append 한다. 반환 (새 텍스트, warning or None).

    Accepted 섹션·표를 못 찾으면 원문 그대로 + warning(fail-soft)."""
    lines = text.split("\n")
    bounds = _section_bounds(lines, _ACCEPTED_HEADER_RE)
    if bounds is None:
        return text, "README 에 Accepted 섹션을 못 찾음 — 색인 행 미삽입"
    start, end = bounds
    last = _last_table_row_index(lines, start, end)
    if last is None:
        return text, "README Accepted 표에 데이터 행이 없음 — 색인 행 미삽입"
    link = f"[{number:04d}]({adr_filename(number, slug)})"
    row = f"| {link} | {title} | {date} | {_flow_list(tags)} |"
    lines.insert(last + 1, row)
    return "\n".join(lines), None


def move_or_append_backref_row(text: str, *, target_num: int, new_id: str, verb: str) -> tuple[str, str | None]:
    """개정 대상 행을 Accepted→Amended/Superseded 표로 이동(또는 이미 이동됐으면 back-ref append).

    verb = amends → Amended(`amended_by`) / supersedes → Superseded(`superseded_by`).
    반환 (새 텍스트, warning or None). 대상/대상표를 못 찾으면 fail-soft(원문 + warning)."""
    if verb == "amends":
        dst_header_re, dst_label = _AMENDED_HEADER_RE, "Amended"
        summary_placeholder = "<개정 요약 — PM 서술>"
    else:
        dst_header_re, dst_label = _SUPERSEDED_HEADER_RE, "Superseded"
        summary_placeholder = "<대체 요약 — PM 서술>"
    link_ref = f"[[{new_id}]]"

    lines = text.split("\n")
    dst_bounds = _section_bounds(lines, dst_header_re)
    if dst_bounds is None:
        return text, f"README 에 {dst_label} 섹션을 못 찾음 — 대상 {adr_id(target_num)} 이동 skip (frontmatter back-ref 는 반영됨)"

    # 이미 대상표에 있으면 back_ref cell(3번째 열)에 append.
    ds, de = dst_bounds
    existing_idx = _find_row_index(lines, ds, de, target_num)
    if existing_idx is not None:
        cells = _row_cells(lines[existing_idx])
        if len(cells) >= 3:
            refs = cells[2]
            if link_ref not in refs:
                cells[2] = f"{refs}·{link_ref}"
                lines[existing_idx] = "| " + " | ".join(cells) + " |"
        return "\n".join(lines), None

    # Accepted 에서 대상 행을 찾아 셀을 뽑고 제거 → 대상표로 이동.
    acc_bounds = _section_bounds(lines, _ACCEPTED_HEADER_RE)
    if acc_bounds is None:
        return text, f"README Accepted 섹션 부재 — 대상 {adr_id(target_num)} 이동 skip"
    as_, ae = acc_bounds
    row_idx = _find_row_index(lines, as_, ae, target_num)
    if row_idx is None:
        return text, f"README Accepted 표에서 {adr_id(target_num)} 행을 못 찾음 — 이동 skip (frontmatter back-ref 는 반영됨)"
    cells = _row_cells(lines[row_idx])
    if len(cells) < 2:
        return text, f"README {adr_id(target_num)} 행 파싱 실패 — 이동 skip"
    link_col, title_col = cells[0], cells[1]
    del lines[row_idx]

    # 대상표 경계를 재계산(위 삭제로 인덱스 이동) 후 마지막 데이터 행 뒤에 삽입.
    dst_bounds = _section_bounds(lines, dst_header_re)
    ds, de = dst_bounds
    new_row = f"| {link_col} | {title_col} | {link_ref} | {summary_placeholder} |"
    last = _last_table_row_index(lines, ds, de)
    insert_at = (last + 1) if last is not None else de
    lines.insert(insert_at, new_row)
    return "\n".join(lines), None


# ── 5. log decide entry ──────────────────────────────────────────────────────

_DECIDE_LOG_TEMPLATE = """\
## [{date}] decide | [[{adr_id}]] — {title}

- <!-- PM: 결정 요약·발단·게이트·메타 서술 -->
"""


def build_decide_log_entry(number: int, title: str, date: str) -> str:
    """log/current.md 에 append 할 decide entry skeleton (placeholder 본문)."""
    return _DECIDE_LOG_TEMPLATE.format(date=date, adr_id=adr_id(number), title=title)
