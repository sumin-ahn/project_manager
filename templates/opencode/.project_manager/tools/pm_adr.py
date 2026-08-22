#!/usr/bin/env python3
"""ADR 발행/개정 명령어化 backbone — 흩어진 손 단계(채번·frontmatter·lifecycle back-ref·
README 색인·log decide entry)를 한 명령으로 원자화한다.

사용:
    python3 .project_manager/tools/pm_adr.py new \\
      --title "제목" --slug short-slug --scope internal-process \\
      --author "user/pm-slot" \\
      [--amends ADR-0061 --amends ADR-0062] [--supersedes ADR-0043] [--refines ADR-0033] \\
      [--related ADR-0019,ADR-0050] [--tags board,ticket-id] \\
      [--status accepted] [--dry-run]

동작 (하나라도 실패하면 이후 단계 미착수·apply 는 마지막에 한 번에):
  1. 채번 — decisions/ 스캔·다음 NNNN.
  2. 신규 ADR 파일 scaffold — `decisions/NNNN-slug.md`(frontmatter + 본문 골격).
  3. lifecycle back-ref 기록 — `--amends`/`--supersedes` 대상 ADR frontmatter 에 status(amended/
     superseded) + amended_by/superseded_by 기록(발행 시점 충족·사후 lint 아님).
  4. README 색인 — Accepted 표에 신규 행 추가 + amends/supersedes 대상 행 Accepted→Amended/
     Superseded 표 이동(또는 이미 이동됐으면 back-ref cell 에 append).
  5. log/current.md decide entry skeleton append.

설계:
  - 경로 해소는 **인스턴스-상대**(self-location·`Path(__file__).parents[2]`) — ADR 실경로는
    채택자 `.project_manager/wiki/decisions/`. 엔진은 어느 인스턴스에서 돌든 자기 위치에서 해소한다.
  - 신규 파일 frontmatter 는 flow-list 문자열 템플릿으로 쓴다(기존 ADR 스타일 보존·yaml round-trip
    reformat 회피). 대상 ADR 개정은 **surgical 정규식 치환**(frontmatter 블록만·본문·포맷 불변).
  - 판단 요소(개정 요약·본문 서술)는 placeholder(`<... PM 서술>`) — 기계는 파생 가능한 것만
    채운다(pm_handoff/ticket_finish skeleton 철학 계승).
  - DI seam — decisions_dir/readme_file/log_file/date 를 주입해 hermetic 테스트(실 wiki 미접촉).
  - README 조작은 fail-soft — 구조(표) 불일치 시 crash 아니라 warning + 해당 단계 skip
    (frontmatter back-ref = load-bearing 은 항상 수행·lint 정합 보장).
  - LLM 미호출 — stdlib + PyYAML(대상 frontmatter 파싱)만.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

import yaml

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise

REPO = Path(__file__).resolve().parents[2]
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
README_FILE = DECISIONS_DIR / "README.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"

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

# ADR id 정규화 — `ADR-0061`·`adr-61`·`61`·`0061` 모두 정수로.
_ADR_ID_RE = re.compile(r"(?:ADR-)?0*(\d+)$", re.IGNORECASE)


def adr_id(n: int) -> str:
    """정수 → canonical `ADR-NNNN` (4자리 zero-pad)."""
    return f"ADR-{n:04d}"


def parse_adr_num(token: str) -> int:
    """`ADR-0061`·`61`·`0061` → 정수 61. 형식 어긋나면 ValueError(발행 인자는 fail-loud)."""
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


# ── 1. 채번 ──────────────────────────────────────────────────────────────────

def next_adr_number(decisions_dir: Path = DECISIONS_DIR) -> int:
    """decisions/ 의 `NNNN-*.md` 를 스캔해 다음 번호를 반환한다.

    decisions/ 부재(신규 clone)면 1. 파일이 있으면 max+1. `.stem` 앞 숫자 토큰만 본다
    (README.md·_template.md 등 비-숫자 파일 자연 제외)."""
    if not decisions_dir.is_dir():
        return 1
    nums = []
    for p in decisions_dir.glob("[0-9]*.md"):
        m = re.match(r"^(\d+)", p.stem)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


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
    같은 **정상** 제목의 `:`·`#`·따옴표가 frontmatter YAML 을 깨서, `board.py` load_ticket 이 그 ADR 을
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


# ── 3. lifecycle back-ref 기록 (대상 ADR frontmatter surgical 치환) ────────────

def _find_target_file(decisions_dir: Path, num: int) -> Path | None:
    """대상 ADR 번호 → `decisions/NNNN-*.md` 파일 경로 (없으면 None)."""
    matches = sorted(decisions_dir.glob(f"{num:04d}-*.md"))
    return matches[0] if matches else None


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """`---\\n...\\n---\\n` frontmatter 블록과 나머지를 분리한다 (없으면 None).

    board.load_ticket 동형 — 첫 닫는 `---` 로 분리. 반환은 (frontmatter_텍스트, 나머지)."""
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
## [{date}] decide | {adr_id} — {title}

- <!-- PM: 결정 요약·발단·게이트·메타 서술 -->
"""


def build_decide_log_entry(number: int, title: str, date: str) -> str:
    """log/current.md 에 append 할 decide entry skeleton (placeholder 본문)."""
    return _DECIDE_LOG_TEMPLATE.format(date=date, adr_id=adr_id(number), title=title)


# ── 오케스트레이션 ────────────────────────────────────────────────────────────

class AdrIssuer:
    """ADR 발행/개정 원자화 핵심 로직.

    경로는 DI 로 주입해 hermetic 테스트(실 wiki 미접촉). apply 는 모든 계획 산출을 모아
    마지막에 파일 쓰기 — 중간 실패 시 부분 상태를 남기지 않는다(채번→scaffold→back-ref→
    README→log 순으로 계획하고, dry-run 이면 미리보기만)."""

    def __init__(
        self,
        *,
        decisions_dir: Path = DECISIONS_DIR,
        readme_file: Path | None = None,
        log_file: Path = LOG_FILE,
        date: str | None = None,
    ) -> None:
        self._decisions_dir = decisions_dir
        self._readme_file = readme_file if readme_file is not None else decisions_dir / "README.md"
        self._log_file = log_file
        self._date = date or datetime.date.today().isoformat()

    def plan(
        self,
        *,
        title: str,
        slug: str,
        scope: str,
        author: str,
        status: str,
        amends: list[int],
        supersedes: list[int],
        refines: list[int],
        related: list[str],
        tags: list[str],
    ) -> dict:
        """발행 계획을 산출한다 — 실제 쓰기 없이 (경로, 내용, warnings)."""
        number = next_adr_number(self._decisions_dir)
        warnings: list[str] = []

        adr_path = self._decisions_dir / adr_filename(number, slug)
        adr_text = build_adr_file(
            number=number, title=title, scope=scope, author=author, date=self._date,
            status=status, amends=amends, supersedes=supersedes, refines=refines,
            related=related, tags=tags,
        )

        # 대상 ADR frontmatter back-ref (load-bearing·lint 정합).
        target_edits: list[tuple[Path, str]] = []
        for verb, nums in (("amends", amends), ("supersedes", supersedes)):
            for tnum in nums:
                tpath = _find_target_file(self._decisions_dir, tnum)
                if tpath is None:
                    warnings.append(f"{verb} 대상 {adr_id(tnum)} 파일을 decisions/ 에서 못 찾음 — back-ref skip")
                    continue
                original = _load_file_lock().read_text_shared(tpath, encoding="utf-8")
                edited = apply_lifecycle_backref(original, adr_id(number), verb)
                if edited == original and _split_frontmatter(original) is None:
                    warnings.append(f"{adr_id(tnum)} frontmatter 파싱 실패 — back-ref skip")
                    continue
                target_edits.append((tpath, edited))

        # README 색인.
        readme_text = None
        if self._readme_file.exists():
            readme_text = _load_file_lock().read_text_shared(self._readme_file, encoding="utf-8")
            readme_text, w = insert_accepted_row(
                readme_text, number=number, slug=slug, title=title, date=self._date, tags=tags,
            )
            if w:
                warnings.append(w)
            for verb, nums in (("amends", amends), ("supersedes", supersedes)):
                for tnum in nums:
                    readme_text, w = move_or_append_backref_row(
                        readme_text, target_num=tnum, new_id=adr_id(number), verb=verb,
                    )
                    if w:
                        warnings.append(w)
        else:
            warnings.append(f"README({self._readme_file}) 부재 — 색인 skip")

        # log decide entry.
        log_entry = build_decide_log_entry(number, title, self._date)

        return {
            "number": number,
            "adr_path": adr_path,
            "adr_text": adr_text,
            "target_edits": target_edits,
            "readme_text": readme_text,
            "log_entry": log_entry,
            "warnings": warnings,
        }

    def apply(self, plan: dict) -> None:
        """계획을 파일에 쓴다 — **신규 ADR 파일을 맨 먼저**, 이어 대상 back-ref·README·log 순차 적용.

        full staged-write(전 파일 tmp→원자 rename)는 과설계로 보류한다 —
        ADR 발행은 단일-PM·저빈도 수동 작업이라 다중 파일 트랜잭션이 과하다. 대신 **부분 실패 시 어느
        단계까지 됐는지(수행/미수행) 명시**해 사람이 복구하게 한다. 신규 ADR 파일을 **가장 먼저** 써서
        최악의 부분 실패에서도 결정 본문 자체는 디스크에 보존되게 한다(주 산출물 우선)."""
        self._decisions_dir.mkdir(parents=True, exist_ok=True)
        steps: list[tuple[str, "callable"]] = [
            ("신규 ADR 파일", lambda: plan["adr_path"].write_text(
                plan["adr_text"], encoding="utf-8", newline="\n")),
            ("대상 back-ref", lambda: self._write_targets(plan)),
            ("README 색인", lambda: self._write_readme(plan)),
            ("log decide entry", lambda: self._append_log(plan)),
        ]
        done: list[str] = []
        for label, fn in steps:
            try:
                fn()
            except OSError as exc:
                remaining = [lbl for lbl, _ in steps[len(done):]]
                raise RuntimeError(
                    f"ADR 발행 부분 실패 — '{label}' 단계에서 중단({exc}). "
                    f"수행 완료: {done or ['없음']} · 미수행: {remaining}. "
                    "미수행 단계를 손으로 마저 적용하라 (신규 ADR 파일은 가장 먼저 기록되므로 "
                    "결정 본문은 보존됨)."
                ) from exc
            done.append(label)

    def _write_targets(self, plan: dict) -> None:
        for tpath, edited in plan["target_edits"]:
            tpath.write_text(edited, encoding="utf-8", newline="\n")

    def _write_readme(self, plan: dict) -> None:
        if plan["readme_text"] is not None:
            self._readme_file.write_text(
                plan["readme_text"], encoding="utf-8", newline="\n")

    def _append_log(self, plan: dict) -> None:
        pm_log = _load_pm_log()
        payload = "\n" + plan["log_entry"]
        pm_log.append_log(self._log_file, payload)


# ── contradiction lint 트리거 ───────────────────────────────
# 결정을 개정(amends/supersedes)하는 바로 이 명령이 모순 lint 의 배선점이다 — 재정의 순간
# (인지 시점)에 옛 결정을 참조하는 문서의 잔여 모순을 표면화한다. **개정에만** 발화한다(신규 plain 발행·
# refines 는 참조 스코프가 없거나 대상 불변이라 잔여 모순을 안 만든다). 탐지=LLM(기본 dry·미호출)·판정=
# 사람(advisory·차단 아님) — 이 트리거는 어떤 경우에도 발행을 막지 않는다(fail-soft·감싸 호출).

CONTRADICTION_LINT_PY = Path(__file__).resolve().parent / "contradiction_lint.py"


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.7.8"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


def _load_contradiction_lint():
    """contradiction_lint.py 를 sibling import 한다 (실패 시 None·graceful·advisory 는 부가 기능)."""
    try:
        mod = _load_module_from_path(
            CONTRADICTION_LINT_PY,
            "contradiction_lint.py",
            verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # contradiction_lint 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def emit_contradiction_advisory(
    *, new_number: int, title: str, adr_text: str, amends: list[int], supersedes: list[int],
) -> None:
    """개정(amends/supersedes) 시 모순 lint advisory 를 stderr 로 표면화한다(인지 시점).

    개정 대상이 없으면 no-op. contradiction_lint 미로드/오류는 조용히 무시(fail-soft — 부가
    advisory 가 발행을 막지 않는다). LLM 은 호출하지 않는다(dry·비용 0) — 스코프+안내만 표면화한다."""
    targets = [adr_id(n) for n in (amends + supersedes)]
    if not targets:
        return
    cl = _load_contradiction_lint()
    if cl is None:
        return
    try:
        new_id = adr_id(new_number)
        result = cl.ContradictionLinter().lint(  # dry (run_fn=None·LLM 미호출)
            new_adr_id=new_id, new_adr_title=title, new_adr_text=adr_text, target_ids=targets,
        )
        print("\n" + cl.format_advisory(new_id, targets, result), file=sys.stderr)
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise
        return  # advisory 는 fail-soft — 어떤 오류도 발행을 막지 않는다.


# ── CLI ──────────────────────────────────────────────────────────────────────

def _resolve_default_author() -> "str | None":
    """`--author` 생략 시 board `identity_tag()` sibling 재사용 — `<user>/<pm-slot>` 해소.

    board.py 의 기존 identity 해소 체인(local.conf user → git config user.email·세션 토큰)을
    그대로 쓴다(자체 로직 신설 0). 로드/해소 실패는 None
    (fail-soft — 호출부가 현행 빈 값 경로 유지·발행을 못 깨게)."""
    try:
        board_path = Path(__file__).resolve().parent / "board.py"
        mod = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
        return mod.identity_tag()
    except Exception as exc:  # noqa: BLE001 — identity 해소 실패는 advisory 경로로(발행 무영향).
        if _is_engine_rev_skew(exc):
            raise  # board 사본 skew 는 fail-loud(삼키지 않는다).
        return None


def _parse_id_list(tokens: list[str]) -> list[int]:
    """`--amends ADR-0061 --amends 62` 또는 콤마 묶음(`--related a,b`)을 정수/문자열로 파싱."""
    out: list[int] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            if part.strip():
                out.append(parse_adr_num(part))
    return out


def _parse_str_list(tokens: list[str]) -> list[str]:
    """콤마/공백 분리 문자열 리스트 (related·tags 자유 문자열)."""
    out: list[str] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            if part.strip():
                out.append(part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pm_adr.py",
        description="ADR 발행/개정 명령어化 (채번·scaffold·lifecycle back-ref·README 색인·log).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="새 ADR 발행 (개정 시 --amends/--supersedes/--refines)")
    new.add_argument("--title", required=True, help="ADR 제목")
    new.add_argument("--slug", required=True, help="파일명 slug(영문·hyphen) — NNNN-<slug>.md")
    new.add_argument("--scope", default="internal-process",
                     choices=["internal-process", "mission"], help="결정 scope (기본 internal-process)")
    new.add_argument("--author", default="", help="provenance `<user>/<pm-slot>`")
    new.add_argument("--status", default="accepted",
                     choices=["proposed", "accepted"], help="발행 status (기본 accepted)")
    new.add_argument("--amends", action="append", default=[], metavar="ADR-NNNN",
                     help="개정(부분 수정) 대상 — 대상 status→amended·amended_by 기록 (반복 가능)")
    new.add_argument("--supersedes", action="append", default=[], metavar="ADR-NNNN",
                     help="대체 대상 — 대상 status→superseded·superseded_by 기록 (반복 가능)")
    new.add_argument("--refines", action="append", default=[], metavar="ADR-NNNN",
                     help="확장(대상 불변) 대상 — related 링크만 (반복 가능)")
    new.add_argument("--related", action="append", default=[], metavar="ID",
                     help="관련 링크(ADR/spike/ticket) — 콤마/반복 (개정 대상은 자동 편입)")
    new.add_argument("--tags", action="append", default=[], metavar="TAG",
                     help="frontmatter tags — 콤마/반복")
    new.add_argument("--dry-run", action="store_true", help="쓰기 없이 미리보기만")
    return p


def cmd_new(args: argparse.Namespace) -> int:
    # slug 는 파일명이 되므로 부작용(채번·파일 쓰기) 이전에 CLI 입력 단계에서 거부한다(fail-loud·
    # 파일 주입/traversal 방지·validator 클래스 동형).
    try:
        validate_slug(args.slug)
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 2

    # 개정대상 ID 도 부작용 이전 입구에서 rc 2 한 줄 오류로 거부한다
    # ValueError 가 traceback 으로 노출·slug 게이트와 동형 패턴).
    try:
        amends = _parse_id_list(args.amends)
        supersedes = _parse_id_list(args.supersedes)
        refines = _parse_id_list(args.refines)
    except ValueError as exc:
        print(f"[중단] 개정대상 ID 형식 오류 — {exc} (`ADR-NNNN` 또는 숫자만 허용)", file=sys.stderr)
        return 2
    related = _parse_str_list(args.related)
    tags = _parse_str_list(args.tags)

    # --author 생략 시 기존 identity 해소 체인 재사용(board.identity_tag —
    # local.conf user → git config user.email → 세션 토큰). 명시 인자 우선·해소 불가(None)면
    # 현행(빈 값 → lint_adr_author advisory) 유지 — 새 해소 로직 신설 0.
    author = args.author or _resolve_default_author() or ""

    issuer = AdrIssuer(decisions_dir=DECISIONS_DIR, log_file=LOG_FILE)
    plan = issuer.plan(
        title=args.title, slug=args.slug, scope=args.scope, author=author,
        status=args.status, amends=amends, supersedes=supersedes, refines=refines,
        related=related, tags=tags,
    )
    number = plan["number"]

    if args.dry_run:
        print(f"[dry-run] 발행 예정: {adr_id(number)} → {plan['adr_path']}")
        print("\n── 신규 ADR 파일 ──")
        print(plan["adr_text"])
        for tpath, _ in plan["target_edits"]:
            print(f"── back-ref 기록: {tpath.name} ──")
        print("\n── log decide entry ──")
        print(plan["log_entry"])
    else:
        issuer.apply(plan)
        print(f"✓ {adr_id(number)} 발행 → {plan['adr_path'].name}")
        for tpath, _ in plan["target_edits"]:
            print(f"  ✓ back-ref 기록: {tpath.name}")
        if plan["readme_text"] is not None:
            print("  ✓ README 색인 갱신")
        print("  ✓ log/current.md decide entry append")

    for w in plan["warnings"]:
        print(f"  ⚠ {w}", file=sys.stderr)

    # 모순 lint 트리거 — 개정(amends/supersedes)에만 발화. dry-run/apply 공통으로
    # 인지 시점(재정의 명령)에 잔여 모순 스코프를 표면화한다(advisory·fail-soft·차단 아님).
    emit_contradiction_advisory(
        new_number=number, title=args.title, adr_text=plan["adr_text"],
        amends=amends, supersedes=supersedes,
    )

    print("\n남은 PM 손작업: ADR 본문 서술(Context/Decision/Consequences/References) + "
          "README 개정 요약 cell + log decide 본문 + git commit.")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "new":
        return cmd_new(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


def _load_pm_log():
    """공유 log writer seam을 같은 tools/의 pm_log.py에서 로드한다."""
    return _load_module_from_path(
        Path(__file__).resolve().parent / "pm_log.py",
        "pm_log.py",
        verifier=_verify_engine_rev,
    )


def _load_file_lock():
    """공용 파일 프리미티브 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    원자 교체 대상 파일을 읽는 지점은 이 seam 의 공유 읽기를 지난다([[T-0729]]) — 일반 `open`
    리더가 하나라도 잡고 있으면 Windows 는 그 파일의 원자 교체를 WinError 32 로 막는다. 부재/
    손상/rev 불일치는 엔진 사본 손상이므로 흡수하지 않는다(fail-loud·재동기 안내).
    """
    return _load_module_from_path(
        Path(__file__).resolve().parent / "file_lock.py", "file_lock.py",
        verifier=_verify_engine_rev, cache=True,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        _console_encoding = _load_module_from_path(
            Path(__file__).resolve().with_name("console_encoding.py"),
            "console_encoding.py",
            verifier=_verify_engine_rev,
        )
        _console_encoding.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
