#!/usr/bin/env python3
"""log 의미단위 읽기 + 아카이브 도구.

구조:
  .project_manager/wiki/log/current.md            — 활성 로그 (모든 새 entry 의 단일 쓰기 대상)
  .project_manager/wiki/log/archive/NNNN-<label>.md — 봉인된 과거 슬라이스

사용:
    python3 .project_manager/tools/pm_log.py tail
    python3 .project_manager/tools/pm_log.py archive --before YYYY-MM-DD [--dry-run]
    python3 .project_manager/tools/pm_log.py archive --keep-last N [--dry-run]
    python3 .project_manager/tools/pm_log.py migrate [--dry-run]

명령:
  tail                  — current.md 의 마지막 `## [..]` entry 만 출력 (의미단위 읽기 헬퍼).
  archive --before DATE — current.md 에서 DATE *미만* 날짜의 entry 들을 archive/ 새 슬라이스로
                          이동하고 current.md 는 최근만 남긴다. 멱등 (옮길 게 없으면 no-op).
  archive --keep-last N — current.md 에서 최근 N entry 만 남기고 나머지(오래된 쪽)를 archive/
                          새 슬라이스로 봉인한다. 날짜 계산 없이 개수로 자른다. N ≥ entry 수면 no-op.
                          `--before` 와 상호배타 — 정확히 하나만 지정한다.
  migrate               — 기존 단일 `log.md` → `log/archive/0000-legacy.md` 로 봉인 +
                          `log/current.md` 생성. 일회성·멱등 (current.md 가 이미 있으면 no-op).

결정:
  - 쓰기 대상은 current.md 단일 경로다. legacy `log.md` 는 migrate 로 봉인만 한다 — 런타임 fallback 없음.
  - 편집은 entry(`## [YYYY-MM-DD] ...`) 경계 기준·멱등·실패 시 비편집 (ticket_finish.py 패턴 계승).
  - LLM 미호출 — stdlib 만.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import importlib.util
import sys
from pathlib import Path

# ── 엔진 사본 rev 스탬프 (pm_bootstrap deep-import target) ────────────────
ENGINE_REV = "v1.5.1"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


REPO = Path(__file__).resolve().parents[2]
WIKI_DIR = REPO / ".project_manager" / "wiki"
LOG_DIR = WIKI_DIR / "log"
CURRENT_FILE = LOG_DIR / "current.md"
ARCHIVE_DIR = LOG_DIR / "archive"
LEGACY_LOG = WIKI_DIR / "log.md"

# 새 current.md 가 처음 생길 때 얹는 표준 헤더 (log.md 의 기존 헤더와 동일 형식).
CURRENT_HEADER = """\
# Project Log

> 프로젝트 운영 작업의 시간순 기록. Append-only. 활성 로그는 이 파일(`log/current.md`).
> 여러 세션/clone 이 동시에 append 해도 OK — `.gitattributes` 의 union merge 가 양쪽 entry 를 보존한다.
> 오래된 entry 는 `pm_log.py archive` 로 `log/archive/` 에 봉인된다.
> 형식: `## [YYYY-MM-DD] action | subject`
> Actions: create, update, decide (ADR), ticket, spec, split, handoff, lint
"""

# entry 시작 앵커: "## [YYYY-MM-DD] ..." 줄.
_ENTRY_RE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\]", re.MULTILINE)


# ── 순수 헬퍼 ──────────────────────────────────────────────────────────────

def split_entries(text: str) -> tuple[str, list[tuple[str, str]]]:
    """log 텍스트를 (preamble, [(date, entry_text), ...]) 로 쪼갠다.

    preamble = 첫 entry 이전의 헤더 블록. 각 entry_text 는 `## [..]` 줄부터
    다음 entry 직전(또는 파일 끝)까지 — 줄바꿈 포함.
    """
    matches = list(_ENTRY_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    entries: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((m.group(1), text[start:end]))
    return preamble, entries


def next_archive_index(archive_dir: Path) -> int:
    """archive/ 의 다음 슬라이스 정수 인덱스. 0000 은 legacy 예약이므로 최소 1."""
    max_idx = 0
    if archive_dir.exists():
        for p in archive_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
            m = re.match(r"(\d{4})-", p.name)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
    return max(max_idx + 1, 1)


# ── 명령 ───────────────────────────────────────────────────────────────────

def cmd_tail(args: argparse.Namespace) -> int:
    if not CURRENT_FILE.exists():
        print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
        return 2
    _preamble, entries = split_entries(CURRENT_FILE.read_text(encoding="utf-8"))
    if not entries:
        print("(entry 없음)")
        return 0
    print(entries[-1][1].rstrip())
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    before = getattr(args, "before", None)
    keep_last = getattr(args, "keep_last", None)

    # --before 와 --keep-last 는 상호배타 — 정확히 하나만 지정한다 (둘 다/둘 다 없음 거부).
    # argparse mutex 그룹이 CLI 에서 "둘 다"를 먼저 걸러내지만, 함수 직접 호출(테스트) 경로에서도
    # "정확히 하나" 를 못박는다.
    if (before is None) == (keep_last is None):
        print("archive: --before DATE 와 --keep-last N 중 정확히 하나를 지정하세요 "
              "(둘 다/둘 다 없음 불가).", file=sys.stderr)
        return 1

    cutoff = None
    if before is not None:
        try:
            cutoff = datetime.date.fromisoformat(before)
        except ValueError:
            print(f"--before 날짜 형식 오류: {before!r} (YYYY-MM-DD)", file=sys.stderr)
            return 1

    if not CURRENT_FILE.exists():
        print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
        return 2

    text = CURRENT_FILE.read_text(encoding="utf-8")
    preamble, entries = split_entries(text)

    if cutoff is not None:
        # 날짜 기반: DATE 미만(strict <)만 봉인, DATE 이상은 유지.
        old = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) < cutoff]
        keep = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) >= cutoff]
        mode_line = f"--before {before}"
        noop_msg = f"옮길 entry 없음 (--before {before} 미만 entry 0개) — no-op."
    else:
        # 개수 기반: 최근 N entry(tail)만 유지, 나머지 오래된 쪽을 봉인. entry 단위(줄/바이트 아님).
        # N ≥ entry 수면 old 0개 → no-op (기존 no-op 경로 재사용).
        n = keep_last
        old = entries[:-n] if n < len(entries) else []
        keep = entries[-n:] if n < len(entries) else entries
        mode_line = f"--keep-last {n}"
        noop_msg = f"옮길 entry 없음 (entry {len(entries)}개 ≤ --keep-last {n}) — no-op."

    if not old:
        print(noop_msg)
        return 0

    idx = next_archive_index(ARCHIVE_DIR)
    first, last = old[0][0], old[-1][0]
    slice_name = f"{idx:04d}-{first}_to_{last}.md"
    slice_path = ARCHIVE_DIR / slice_name
    slice_body = (
        f"# Log archive {idx:04d} ({first} ~ {last})\n\n"
        f"> `pm_log.py archive {mode_line}` 로 current.md 에서 봉인. 수정 금지.\n\n"
        + "".join(e for _d, e in old)
    )
    new_current = preamble + "".join(e for _d, e in keep)

    if args.dry_run:
        print(f"[dry-run] {_rel(slice_path)} 로 {len(old)} entry 봉인, "
              f"current.md 는 {len(keep)} entry 유지.")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    slice_path.write_text(slice_body, encoding="utf-8")
    CURRENT_FILE.write_text(new_current, encoding="utf-8")
    print(f"✓ {len(old)} entry → {_rel(slice_path)} 봉인. current.md {len(keep)} entry 유지.")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """기존 단일 log.md → archive/0000-legacy.md 봉인 + current.md 생성. 멱등."""
    if CURRENT_FILE.exists():
        print(f"이미 마이그레이션됨 ({_rel(CURRENT_FILE)} 존재) — no-op.")
        return 0

    legacy_dst = ARCHIVE_DIR / "0000-legacy.md"
    if args.dry_run:
        if LEGACY_LOG.exists():
            print(f"[dry-run] {_rel(LEGACY_LOG)} → {_rel(legacy_dst)} 봉인 + "
                  f"{_rel(CURRENT_FILE)} 생성.")
        else:
            print(f"[dry-run] 기존 log.md 없음 — 빈 {_rel(CURRENT_FILE)} 만 생성.")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if LEGACY_LOG.exists():
        legacy_text = LEGACY_LOG.read_text(encoding="utf-8")
        sealed = (
            "# Log archive 0000 (legacy — 마이그레이션 이전 단일 log.md)\n\n"
            "> 구조 전환 전의 기존 `log.md` 를 그대로 봉인. 수정 금지. "
            "이후 새 entry 는 `log/current.md`.\n\n"
            + legacy_text
        )
        legacy_dst.write_text(sealed, encoding="utf-8")
        LEGACY_LOG.unlink()
        print(f"✓ {_rel(LEGACY_LOG)} → {_rel(legacy_dst)} 봉인.")
    else:
        print("기존 log.md 없음 — 빈 current.md 만 생성.")

    CURRENT_FILE.write_text(CURRENT_HEADER, encoding="utf-8")
    (ARCHIVE_DIR / ".gitkeep").touch()
    print(f"✓ {_rel(CURRENT_FILE)} 생성.")
    return 0


# ── 유틸 ───────────────────────────────────────────────────────────────────

def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _positive_int(value: str) -> int:
    """argparse type — 양의 정수(≥1)만 허용. 0·음수·비정수는 거부."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아님: {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"양의 정수여야 함 (≥1): {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_log.py",
        description="log 의미단위 읽기 + 아카이브 도구.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tail", help="current.md 의 마지막 entry 만 출력")
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("archive",
                       help="entry 를 archive/ 로 봉인 (--before DATE | --keep-last N)")
    # 상호배타 — 정확히 하나. "둘 다"는 여기서(CLI) 걸리고, "둘 다 없음"은 cmd_archive 가 rc 1.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--before", metavar="YYYY-MM-DD",
                      help="이 날짜 미만의 entry 를 아카이브")
    mode.add_argument("--keep-last", type=_positive_int, metavar="N",
                      help="최근 N entry 만 남기고 나머지(오래된 쪽)를 아카이브")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("migrate", help="기존 log.md → archive/0000-legacy.md + current.md")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _verify_engine_rev(_console_encoding, "console_encoding.py")
    _console_encoding.configure_console_utf8()
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
