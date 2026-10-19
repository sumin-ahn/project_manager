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
    python3 .project_manager/tools/pm_log.py ctx-guidance --band nudge|nudge2|final|precompact [--json]
    python3 .project_manager/tools/pm_log.py checkpoint --task NAME [--trigger compaction|manual]
    python3 .project_manager/tools/pm_log.py snapshot [--session-id ID] [--json]

명령:
  tail                  — current.md 의 마지막 `## [..]` entry 만 출력 (의미단위 읽기 헬퍼).
  archive --before DATE — current.md 에서 DATE *미만* 날짜의 entry 들을 archive/ 새 슬라이스로
                          이동하고 current.md 는 최근만 남긴다. 멱등 (옮길 게 없으면 no-op).
  archive --keep-last N — current.md 에서 최근 N entry 만 남기고 나머지(오래된 쪽)를 archive/
                          새 슬라이스로 봉인한다. 날짜 계산 없이 개수로 자른다. N ≥ entry 수면 no-op.
                          `--before` 와 상호배타 — 정확히 하나만 지정한다.
  migrate               — 기존 단일 `log.md` → `log/archive/0000-legacy.md` 로 봉인 +
                          `log/current.md` 생성. 일회성·멱등 (current.md 가 이미 있으면 no-op).
  ctx-guidance          — 3하네스가 공유하는 ctx 연속성 안내 출력(쓰기 0).
  checkpoint            — compaction/manual 경계의 보충 박제 골격을 current.md 에 append.
                          호출마다 신규 entry 를 만들며 서사는 PM 이 채운다.
  snapshot              — compaction 뒤 재주입할 정체성·장부 포인터를 stdout 에 출력.

결정:
  - 쓰기 대상은 current.md 단일 경로다. legacy `log.md` 는 migrate 로 봉인만 한다 — 런타임 fallback 없음.
  - 편집은 entry(`## [YYYY-MM-DD] ...`) 경계 기준·멱등·실패 시 비편집 (ticket_finish.py 패턴 계승).
  - LLM 미호출 — stdlib 만.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.domain import identity_args, ticket_rules  # noqa: E402
from pm_engine.infra import areas_store, console, delegate_ledger, fs, pm_paths, raw_output, repo_files  # noqa: E402
_load_module_from_path = repo_files.load_module


# ── 엔진 사본 rev 스탬프 (pm_bootstrap deep-import target) ────────────────
ENGINE_REV = "v1.8.1"


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


def _is_engine_rev_skew(exc) -> bool:
    """fail-soft 로더에서도 엔진 사본 skew만은 삼키지 않게 식별한다."""
    return getattr(exc, "_engine_rev_skew", False)


def _write_machine_line(text: str) -> None:
    """기계 판독 한 줄(JSON 페이로드)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    사람 출력(``print``)은 PowerShell 캡처에서 콘솔 codepage(cp949 등)로 강등될 수 있고 그
    치환은 되돌릴 수 없다 — 다른 프로세스가 파싱하는 출력은 공용 seam 으로 UTF-8 bytes 를
    직접 쓴다(콘솔 코덱 전환과 독립).
    """
    console.write_machine_line(text)


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "inflight_ledger_query": (
        "compaction snapshot 은 hook 경계다 — 진행 중 작업 절의 장부 조회(pm_delegate·raw_output "
        "in-process 호출)가 재-raise 하면 payload 전체가 죽는다. 손상/로드 실패/사본 skew 를 "
        "흡수하되 조용하지 않게 사유를 stderr 로 남기고 그 절만 1줄로 접는다(다른 절은 온전)"
    ),
}


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """판독 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (사유 등록 필수).

    반환값으로 일반 실패와 사본 불일치를 구분해 호출부가 진단 문구를 달리한다 — 흡수는 하되
    조용하지는 않다."""
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


REPO = Path(__file__).resolve().parents[2]
WIKI_DIR = REPO / ".project_manager" / "wiki"
LOG_DIR = WIKI_DIR / "log"
CURRENT_FILE = LOG_DIR / "current.md"
ARCHIVE_DIR = LOG_DIR / "archive"
LEGACY_LOG = WIKI_DIR / "log.md"

# task 태그 sentinel은 ticket_finish.py·pm_handoff.py의 동명 상수와 미러한다.
# 모듈 격리를 유지하려고 각 생산자가 상수를 소유한다.
_TASK_TAG_PREFIX = "task:"

# compaction snapshot은 모델 컨텍스트를 다시 채우지 않도록 포인터 중심·고정 상한이다.
# 호출자도 3초 subprocess timeout을 강제하지만, 빌더 자체도 절 사이에서 같은 deadline을 확인한다.
SNAPSHOT_TIMEOUT_SECONDS = 3.0
SNAPSHOT_MAX_CHARS = 8_000
SNAPSHOT_MAX_BYTES = 24_000
# 재주입에 펼칠 열린 todo 최대 줄 수 — 그 뒤는 "외 N건" 한 줄로 접고 목록 명령을 가리킨다.
_TODO_MAX_SHOWN = 8
_SNAPSHOT_IDENTITY_HEADING = "## PM 정체성 (compaction 복구)"
# ctx 가드의 세션 연속성 정책 단일 진실. Claude/OpenCode/Codex 어댑터는 문구를 복제하지
# 않고 ``pm_log.py ctx-guidance`` 또는 snapshot을 통해 이 값을 소비한다. checkpoint는
# compaction 생존 장치일 뿐 세션 수명 전환 명령이 아니라는 경계를 한 문자열로 고정한다.
CTX_GUARD_REQUIRED_EXPRESSIONS = (
    "압축은 자동이고 세션은 그대로 이어진다",
    "핸드오프는 사용자 명시 지시로만 한다",
    "컨텍스트 잔량은 작업 범위·중단 결정의 입력이 아니며 checkpoint 기록은 진행 중 작업과 "
    "병행하고 진행 중 작업은 계속한다. 세션 종료·작업 축소는 사용자 지시로만 한다",
)
# 금지 literal은 가드의 검사 상수로 의도적으로 존재하며, 판정 대상은 소스 grep이 아니라 밴드/fallback 출력이다.
CTX_GUARD_FORBIDDEN_EXPRESSIONS = (
    "새 큰 작업보다 현재 서사 기록을 우선",
)
CTX_GUARD_CONTINUITY_GUIDANCE = (
    f"{CTX_GUARD_REQUIRED_EXPRESSIONS[0]}. checkpoint는 압축 후 서사 복구용이지 "
    f"종료 신호가 아니다. {CTX_GUARD_REQUIRED_EXPRESSIONS[1]}. "
    f"{CTX_GUARD_REQUIRED_EXPRESSIONS[2]}."
)
_PRECOMPACT_BREADCRUMB = (
    f"\n> ⚠ auto-compact 발생 — {CTX_GUARD_CONTINUITY_GUIDANCE}\n"
)

# ── 전언 경고 + 진행 중 작업 절 (컴팩션 복구 주입 보강) ─────────────
# 컴팩션 요약 속 단언("불가·없음·누구 몫·이미 됨")은 근거가 지워진 전언이다 — 실측 없이
# 행동 전제로 상속되지 않게 always-keep 접두로 싣는다(문안은 architect 라운드 확정 verbatim).
# CTX_GUARD 금지표현·FORBIDDEN 정규식 3종(tests/test_ctx_continuity_guidance.py) 0 hit 대상이다.
SNAPSHOT_HEARSAY_WARNING = (
    "## 전언 경고 (요약 속 단언은 미검증)\n"
    "- 압축 요약이 남긴 \"불가·없음·누구 몫·이미 됨\"은 근거가 지워진 전언이다. "
    "행동 전제로 삼기 전에 실측 1회.\n"
    "- 접근 경로·환경·선례는 CLAUDE.md 와 `python3 .project_manager/tools/pm_log.py tail`, "
    "log/current.md 검색으로 확인한다.\n"
    "- 이 절은 판단 재료다. 진행 중 작업은 계속한다.\n"
)

# 진행 중 작업 절의 접기 상한("외 N건") — 각 불릿이 스스로 크기를 제한한다(상한 tail-drop에
# 기대지 않는다). 값은 architect 라운드 §"접기 상한" 확정.
_INFLIGHT_UNHARVESTED_SHOWN = 3
_INFLIGHT_RAW_SHOWN = 3
_INFLIGHT_CLAIMED_SHOWN = 12
_INFLIGHT_SLOT_SHOWN = 4
_INFLIGHT_MAX_LINE_CHARS = 200
_INFLIGHT_MAX_LINES = 8

# 슬롯 WIP git 프로브 — subprocess 0 계약(장부 조회 축)의 유일한 예외. 현재 task leased 슬롯 +
# PM 홈 합계 상한 3회, 개별 timeout은 남은 예산과 1.0s 중 작은 값, `--no-optional-locks`로
# 병렬 dev 슬롯의 git 작업과 경합하지 않는다. 전 예외 흡수(fail-soft) — 장부 조회는 여전히 spawn 0.
_WIP_PROBE_MAX_CALLS = 3
_WIP_PROBE_TIMEOUT_SECONDS = 1.0

# 새 current.md 가 처음 생길 때 얹는 표준 헤더 (log.md 의 기존 헤더와 동일 형식).
CURRENT_HEADER = """\
# Project Log

> 프로젝트 운영 작업의 시간순 기록. Append-only. 활성 로그는 이 파일(`log/current.md`).
> 여러 세션/clone 이 동시에 append 해도 OK — `.gitattributes` 의 union merge 가 양쪽 entry 를 보존한다.
> 오래된 entry 는 `pm_log.py archive` 로 `log/archive/` 에 봉인된다.
> 형식: `## [YYYY-MM-DD] action | subject`
> Actions: create, update, decide (ADR), ticket, spec, split, handoff, checkpoint, lint
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


# ── 공유 log write seam ───────────────────────────────────────────────────

def _log_lock_path(log_path: Path) -> Path:
    """current.md가 속한 `.project_manager/.local/log.lock`을 해소한다.

    운영 경로가 아닌 주입형 테스트 경로는 그 파일의 부모 아래 `.local/`로 격리한다.
    """
    log_path = Path(log_path)
    log_dir = log_path.parent
    if (
        log_dir.name == "log"
        and log_dir.parent.name == "wiki"
        and log_dir.parent.parent.name == ".project_manager"
    ):
        local_dir = log_dir.parent.parent / ".local"
    else:
        local_dir = log_dir / ".local"
    return local_dir / "log.lock"


@contextlib.contextmanager
def log_write_lock(log_path: Path) -> Iterator[None]:
    """모든 `log/current.md` writer를 단일 OS 파일락으로 직렬화한다.

    운영 경로의 잠금 파일은 `.project_manager/.local/log.lock` 하나다. append와
    archive 재작성 모두 이 seam을 거쳐 서로의 갱신을 덮어쓰지 않는다. 프로세스가
    종료되면 OS가 락을 회수하므로 stale lock은 남지 않는다. 재진입은 지원하지 않는다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `fs` seam이 소유하고
    경로 규약만 이 도구가 정한다.
    """
    with fs.exclusive_file_lock(_log_lock_path(Path(log_path))):
        yield


def append_log(path: Path, text: str) -> None:
    """공유 log append 공개 seam — flock 안에서 O_APPEND 단일 write.

    O_APPEND 원자 추가 자체는 공용 `fs` seam이 소유한다 — pm_log는 락 경로
    규약과 부모 디렉토리 생성만 정한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with log_write_lock(path):
        append_log_locked(path, text)


def append_log_locked(path: Path, text: str) -> None:
    """이미 ``log_write_lock(path)``를 보유한 호출자의 O_APPEND primitive.

    read→판정→append 전체를 한 임계구역에 묶어야 하는 소비자용이다. 이 함수 자체는 락을
    잡지 않는다. 공개 기본 경로는 계속 :func:`append_log`이며, 소비자는 반드시 같은
    ``log_write_lock`` 문맥 안에서만 이 함수를 호출한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fs.append_atomic(path, text)


def _replace_atomic(path: Path, text: str) -> None:
    """같은 디렉터리의 임시 파일을 쓴 뒤 `fs.atomic_replace`로 원자 교체한다."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        # newline=""은 입력 문자열의 LF/CRLF/mixed newline bytes를 그대로 인코딩한다.
        # read-modify-write 소비자가 PM 작성 본문을 byte-preserve할 때 플랫폼 개행 변환으로
        # 범위 밖 bytes가 흔들리지 않게 한다.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        fs.atomic_replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_text_exact(path: Path) -> str:
    """UTF-8 파일을 universal-newline 변환 없이 읽어 CRLF/mixed bytes를 보존한다."""
    return fs.read_bytes_shared(Path(path)).decode("utf-8")


# ── 명령 ───────────────────────────────────────────────────────────────────

def cmd_tail(args: argparse.Namespace) -> int:
    if not CURRENT_FILE.exists():
        print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
        return 2
    _preamble, entries = split_entries(_read_text_exact(CURRENT_FILE))
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

    # 존재 확인부터 최신 내용 read, archive index 발행, current 원자 교체까지 한 lock
    # 구간이다. append writer도 같은 lock을 쓰므로 archive가 읽은 뒤 들어온 entry가
    # stale `new_current`에 덮여 유실되는 interleave가 없다.
    with log_write_lock(CURRENT_FILE):
        if not CURRENT_FILE.exists():
            print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
            return 2

        text = _read_text_exact(CURRENT_FILE)
        preamble, entries = split_entries(text)

        if cutoff is not None:
            # 날짜 기반: DATE 미만(strict <)만 봉인, DATE 이상은 유지.
            old = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) < cutoff]
            keep = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) >= cutoff]
            mode_line = f"--before {before}"
            noop_msg = f"옮길 entry 없음 (--before {before} 미만 entry 0개) — no-op."
        else:
            # 개수 기반: 최근 N entry(tail)만 유지, 나머지 오래된 쪽을 봉인. entry 단위.
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
        slice_path.write_text(slice_body, encoding="utf-8", newline="\n")
        _replace_atomic(CURRENT_FILE, new_current)
        print(f"✓ {len(old)} entry → {_rel(slice_path)} 봉인. "
              f"current.md {len(keep)} entry 유지.")
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
        legacy_text = _read_text_exact(LEGACY_LOG)
        sealed = (
            "# Log archive 0000 (legacy — 마이그레이션 이전 단일 log.md)\n\n"
            "> 구조 전환 전의 기존 `log.md` 를 그대로 봉인. 수정 금지. "
            "이후 새 entry 는 `log/current.md`.\n\n"
            + legacy_text
        )
        legacy_dst.write_text(sealed, encoding="utf-8", newline="\n")
        LEGACY_LOG.unlink()
        print(f"✓ {_rel(LEGACY_LOG)} → {_rel(legacy_dst)} 봉인.")
    else:
        print("기존 log.md 없음 — 빈 current.md 만 생성.")

    CURRENT_FILE.write_text(CURRENT_HEADER, encoding="utf-8", newline="\n")
    (ARCHIVE_DIR / ".gitkeep").touch()
    print(f"✓ {_rel(CURRENT_FILE)} 생성.")
    return 0


def build_checkpoint_entry(
    task: str | None,
    trigger: str = "manual",
    date: str | None = None,
) -> str:
    """task 축의 compaction/manual 경계 보충 박제 골격을 만든다."""
    if date is None:
        date = datetime.date.today().isoformat()
    identity_tag = f" | ({_TASK_TAG_PREFIX}{task})" if task else ""
    return (
        # Consumer grammar: pm_handoff._collect_session_entries() 의 task 태그 판별과
        # 동기화한다.
        f"## [{date}] checkpoint{identity_tag} — {trigger}\n\n"
        "- 구간: <직전 박제 경계 이후>\n"
        "- 서사: <PM 손>\n"
    )


# ── compaction 경계 읽기 모델 (snapshot·checkpoint 공통) ───────────────

def _read_json_object(path: Path) -> dict | None:
    """JSON object point-read. 부재·손상·스키마 불일치는 fail-soft ``None``."""
    try:
        value = json.loads(fs.read_text_shared(Path(path), encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_within(path: Path, parent: Path) -> bool:
    """``path``가 ``parent`` 자신/하위인지 lexical+resolve 기준으로 판정한다."""
    try:
        Path(path).resolve(strict=False).relative_to(Path(parent).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _lease_slot_path(pm_home: Path, row: dict) -> Path | None:
    raw = row.get("slot")
    if not isinstance(raw, str) or not raw.strip():
        return None
    slot = Path(raw.strip())
    return slot if slot.is_absolute() else Path(pm_home) / slot


def _lease_rows(pm_home: Path) -> list[dict]:
    data = _read_json_object(
        Path(pm_home) / ".project_manager" / ".local" / "worktree-leases.json"
    )
    rows = data.get("leases") if data else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []




def _active_tasks(pm_home: Path) -> list[str]:
    """task 서술 디렉토리의 활성 이름. 종료 보관소 ``_ended``와 숨김 보조 디렉토리는 제외."""
    tasks_dir = Path(pm_home) / ".project_manager" / ".local" / "tasks"
    try:
        return sorted(
            child.name for child in tasks_dir.iterdir()
            if child.is_dir() and child.name != "_ended" and not child.name.startswith(".")
        )
    except OSError:
        return []


# 정체성 해소 층은 사람이 읽는 진단 문구이면서 pm_handoff 의 실행/미해소 분류 계약이다.
# 소비자가 문자열 리터럴을 복제하지 않도록 생산자가 집합까지 함께 노출한다.
IDENTITY_SOURCE_CWD_LEASE = "cwd→lease"
IDENTITY_SOURCE_SINGLE_ACTIVE_TASK = "단일 활성 task"
IDENTITY_SOURCE_UNRESOLVED = "정체성 미해소"
SNAPSHOT_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
    IDENTITY_SOURCE_UNRESOLVED,
})
HANDOFF_TASK_MODE_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
})


def resolve_snapshot_identity(pm_home: Path, cwd: Path) -> tuple[str | None, str]:
    """cwd lease → 단일 활성 task 순으로 pm_handoff·ticket_finish 무인자 진입의 정체성을 해소한다.

    압축 경로(checkpoint·snapshot)는 이 함수를 부르지 않는다 — 그 경로는 `resolve_session_task`
    하나로 판정한다.

    두 층이 전부다 — "장부가 아무 말도 없으면 이 홈은 단일 정체성"이라는 층은 없다(그 추론은
    실제로 슬롯을 여럿 쓰는 홈에서 오귀속이었다). 세션은 자기 작업 폴더에서 돌고 그 폴더가 곧
    lease 행이라 첫 층이 답을 준다. 작업 폴더가 없는 홈 루트 세션은 이름 붙일 세션이 없으므로
    미해소가 정답이고, 그 사실을 경고로 말한다(조용한 생략 아님).

    per-clone ``local.conf``의 ``session=``은 층이 아니다 — slot 종속 값이 프로젝트 공용
    conf 에 있던 범위 오류라 엔진 어디서도 읽지 않는다."""
    matches: list[tuple[int, str]] = []
    lease_rows = _lease_rows(pm_home)
    for row in lease_rows:
        if row.get("state") != "leased":
            continue
        session = row.get("session")
        slot = _lease_slot_path(pm_home, row)
        if not isinstance(session, str) or not session.strip() or slot is None:
            continue
        if _is_within(cwd, slot):
            matches.append((len(slot.resolve(strict=False).parts), session.strip()))
    if matches:
        deepest = max(depth for depth, _session in matches)
        sessions = sorted({session for depth, session in matches if depth == deepest})
        if len(sessions) == 1:
            return sessions[0], IDENTITY_SOURCE_CWD_LEASE

    active = _active_tasks(pm_home)
    if len(active) == 1:
        return active[0], IDENTITY_SOURCE_SINGLE_ACTIVE_TASK
    return None, IDENTITY_SOURCE_UNRESOLVED


# 압축 경로 세션 판정의 미해소 사유(고정 문구). 하네스 훅이 이 줄을 실패 신호로 사람에게 싣는다.
SESSION_TASK_REASON_NO_SESSION = "세션 ID 없음"
SESSION_TASK_REASON_NO_TASK = "이 세션이 연 task 없음 (세션 {session})"
SESSION_TASK_REASON_MANY_TASKS = "이 세션이 연 task 여럿 (세션 {session}: {names})"


def resolve_session_task(
    pm_home: Path, session_id: str | None,
) -> tuple[str | None, str | None]:
    """압축 경로(checkpoint·snapshot)의 정체성 — 하네스 세션 ID 로 그 세션이 연 task 를 찾는다.

    입력은 세션 ID 하나다. cwd·열린 task 수·위임 환경 변수는 보지 않는다. task 기록
    (`worktree-leases.json` 최상위 `tasks[]`)의 `session` 은 부트스트랩이 남긴 하네스 세션 ID
    원값이고, 그 값이 같은 항목이 정확히 1개일 때만 해소다. 반환은 해소면 ``(task, None)``,
    미해소면 ``(None, 사유)``.

    task 기록 파일이 없는 것만 "task 없음" 이다. 읽기 실패·JSON 파손·모양이 다른 파일은 판정할
    근거가 아니라 고장이므로 예외로 끝난다.
    """
    wanted = session_id.strip() if isinstance(session_id, str) else ""
    if not wanted:
        return None, SESSION_TASK_REASON_NO_SESSION
    ledger = Path(pm_home) / ".project_manager" / ".local" / "worktree-leases.json"
    try:
        text = fs.read_text_shared(ledger, encoding="utf-8")
    except FileNotFoundError:
        return None, SESSION_TASK_REASON_NO_TASK.format(session=wanted)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{ledger}: task 기록 파일 최상위가 JSON 객체가 아님")
    rows = data.get("tasks", [])
    if not isinstance(rows, list):
        raise ValueError(f"{ledger}: task 기록 `tasks` 가 목록이 아님")
    names = sorted(
        row["name"] for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("name"), str) and row["name"].strip()
        and isinstance(row.get("session"), str)
        and row["session"].strip() == wanted
    )
    if not names:
        return None, SESSION_TASK_REASON_NO_TASK.format(session=wanted)
    if len(names) > 1:
        return None, SESSION_TASK_REASON_MANY_TASKS.format(
            session=wanted, names=", ".join(names),
        )
    return names[0], None


def _pm_state_path(pm_home: Path, task: str) -> Path:
    """해소된 task 의 pm_state 경로를 반환한다."""
    return (
        Path(pm_home) / ".project_manager" / ".local" / "tasks" / task / "pm_state.md"
    )


def _identity_section(pm_home: Path, session_id: str, task: str) -> str:
    state_path = _pm_state_path(pm_home, task)
    return (
        f"{_SNAPSHOT_IDENTITY_HEADING}\n"
        f"- task: {task}\n"
        f"- 세션: {session_id.strip()}\n"
        f"- PM 홈: {Path(pm_home).resolve(strict=False)}\n"
        f"- pm_state: {state_path}\n"
    )


def _ticket_counts(pm_home: Path) -> tuple[Path, dict[str, int]]:
    """`pm_home` board 의 상태별 티켓 수 — 버킷은 `ticket_rules.STATUS_DIRS`(단일 진실) 파생이다.

    상태 디렉토리 스캔 자체는 board.py 호출 없이 디렉토리 존재만 본다(집합만 `ticket_rules` 에서 온다).
    """
    manager = Path(pm_home) / ".project_manager"
    # board 분리 형상 우선, legacy는 wiki/tickets.
    board_root = manager / "board"
    tickets = (board_root if board_root.is_dir() else manager / "wiki") / "tickets"
    counts: dict[str, int] = {}
    for status in ticket_rules.STATUS_DIRS:
        try:
            counts[status] = sum(1 for path in (tickets / status).glob("T-*.md") if path.is_file())
        except OSError:
            counts[status] = 0
    return tickets, counts


def _lease_task_slots(pm_home: Path, task: str) -> list[tuple[str, Path]]:
    """현재 task 가 leased 로 보유한 슬롯 — (장부 표기, 해소 경로) 쌍.

    ``_ledger_section``(장부 표기 문자열만)과 슬롯 WIP 프로브(해소 경로가 필요)가 같은
    lease 판정 한 번을 공유한다 — 판정을 두 곳에 복제하면 갈릴 수 있다.
    """
    slots: list[tuple[str, Path]] = []
    for row in _lease_rows(pm_home):
        if row.get("state") == "leased" and row.get("session") == task and row.get("slot"):
            resolved = _lease_slot_path(pm_home, row)
            if resolved is not None:
                slots.append((str(row["slot"]), resolved))
    return slots


def _ledger_section(pm_home: Path, task: str) -> str:
    active = _active_tasks(pm_home)
    leases = _lease_rows(pm_home)
    states: dict[str, int] = {}
    for row in leases:
        state = str(row.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    task_slots = [label for label, _path in _lease_task_slots(pm_home, task)]
    tickets, counts = _ticket_counts(pm_home)
    state_text = ", ".join(f"{key} {states[key]}" for key in sorted(states)) or "장부 없음"
    # 버킷 순서·집합은 `ticket_rules.STATUS_DIRS` 그대로다 — 새 상태는 손대지 않고
    # 뒤에 붙고, 기존 status 표기는 바뀌지 않는다.
    count_text = " / ".join(f"{key} {value}" for key, value in counts.items())
    return (
        "## 장부 포인터\n"
        f"- 활성 tasks ({len(active)}): {', '.join(active) if active else '(없음)'}\n"
        f"- worktree leases: {state_text}\n"
        f"- 현재 task 슬롯: {', '.join(sorted(task_slots)) if task_slots else '(없음)'}\n"
        f"- board tickets: {count_text}\n"
        f"- tickets 경로: {tickets}\n"
    )


def _todo_section(pm_home: Path, task: str) -> str:
    """이 task 의 열린 todo — 매 턴 주입되는 "지금 무엇을 하는 중인가" 의 단일 진실.

    task 별 파일(`.local/tasks/<task>/todo.json`)을 `worktree_pool.read_task_todos` 로 읽고
    `format_todo_line` 으로 렌더한다(표시 문구 사본 0 — CLI `task todo list`·부트스트랩 dump 와
    같은 함수). 열린 todo 가 없으면 그 사실과 등록 경로를 적는다: 그 상태에서는 파일을 고칠
    때마다 쓰기 판정이 경고를 붙인다(막지는 않는다).

    옛 `pm_state 머리` 절을 대체한다 — 그 절은 손으로 쓰는 서술이라 갱신이 밀리면 재주입이
    조용히 낡은 사실을 실어 날랐다. 이 절의 내용은 기계가 쓰는 자리 하나에서만 온다.

    엔진 사본이 없는 홈에서는 이 절만 1줄로 접는다(진행 중 작업 절과 같은 접기 함수) — 이
    재주입은 매 압축마다 도는 자리라 절 하나 때문에 payload 전체가 죽으면 안 된다. 없는 파일은
    예외로 받을 일이 아니라 먼저 보면 되는 조건이다. 사본이 **있는데 낡은** 형상은 그대로
    올린다 — 같은 `pm-update` 가 함께 깔아 놓는 두 파일이라 시대차는 접어 둘 상태가 아니다.
    """
    pool_path = Path(pm_home) / ".project_manager" / "tools" / "worktree_pool.py"
    if not pool_path.is_file():
        return _section_query_failure_line(
            f"엔진 사본 없음 — {pool_path}",
            heading="## 열린 todo (파일 실측)", label="열린 todo 절",
        )
    wp = _load_worktree_pool(pm_home)
    todos = wp.read_task_todos(task)
    if not todos:
        return (
            "## 열린 todo (파일 실측)\n"
            f"- 없음 — 파일 수정 전에 `pm-config task todo add {task} <할 일 한 줄>` 로 등록한다"
            " (등록 전에 파일을 고치면 쓰기 판정이 경고를 붙인다).\n"
        )
    lines = [
        f"- {wp.format_todo_line(todo)}"
        for todo in todos[:_TODO_MAX_SHOWN]
    ]
    hidden = len(todos) - len(lines)
    if hidden > 0:
        lines.append(f"- 외 {hidden}건 — `pm-config task todo list {task}`")
    return "## 열린 todo (파일 실측)\n" + "\n".join(lines) + "\n"


def _recovery_section(task: str) -> str:
    bootstrap = f"python3 .project_manager/tools/pm_bootstrap.py --task {task}"
    return (
        "## 복구 포인터\n"
        f"- `{bootstrap}`로 장부를 다시 펼친다.\n"
        "- 자동 생성된 compaction checkpoint 골격의 구간·서사 불릿은 PM 판단으로 채운다.\n"
        f"- {CTX_GUARD_CONTINUITY_GUIDANCE}\n"
    )


def _hearsay_section() -> str:
    """always-keep 절 — ``build_snapshot`` 의 deadline 폐기 순서에서도 살아남는다."""
    return SNAPSHOT_HEARSAY_WARNING


def _load_worktree_pool(pm_home: Path):
    """그 PM 홈의 worktree_pool 을 in-process 로드 — 열린 todo 조회·표시 전용.

    **그 홈의 사본**을 부른다(형제 사본이 아니라). 할 일 파일 위치는 모듈이 자기 파일 위치에서
    푸는 값이라, 이 파일 옆의 사본을 쓰면 다른 홈의 할 일을 읽는다.

    todo 한 줄의 생김새도 저장소 주인 모듈이 소유한다 — CLI(`task todo list`)·부트스트랩 dump·
    이 재주입이 같은 함수를 부르므로 문구 사본이 없다.
    """
    path = Path(pm_home) / ".project_manager" / "tools" / "worktree_pool.py"
    return _load_module_from_path(path, "worktree_pool.py", verifier=_verify_engine_rev)


# raw 장부 필드·예외 메시지처럼 신뢰 경계 밖 값이 CR/LF·제어문자를 실어 절의 물리 줄 수
# 상한을 우회하거나 가짜 "## " heading 을 주입하지 못하게 렌더 직전에 단일 안전 구분자로
# 정규화한다(소유 모듈은 raw 레코드를 행별 스키마 검증 없이 반환한다).
_INFLIGHT_CONTROL_CHAR_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")


def _inflight_safe_text(value: object) -> str:
    """임의 값을 렌더 안전한 단일 줄 문자열로 정규화한다(CR/LF·제어문자 → 공백 1개)."""
    return _INFLIGHT_CONTROL_CHAR_RE.sub(" ", str(value)).strip()


def _fold_join(items: list[str], shown: int, unit: str) -> str:
    """앞 ``shown`` 개만 나열하고 나머지는 "외 N<unit>" 로 접는다(절 길이 자기 상한 · tail-drop 비의존)."""
    head = items[:shown]
    text = " · ".join(head)
    remaining = len(items) - len(head)
    if remaining > 0:
        text += f" 외 {remaining}{unit}"
    return text


def _inflight_capped_line(*, prefix: str, content: str, suffix: str) -> str:
    """``content`` 만 잘라 줄 상한(``_INFLIGHT_MAX_LINE_CHARS``)을 지킨다(접두·명령 포인터는 보존)."""
    line = f"{prefix}{content}{suffix}"
    if len(line) <= _INFLIGHT_MAX_LINE_CHARS:
        return line
    budget = _INFLIGHT_MAX_LINE_CHARS - len(prefix) - len(suffix) - 1
    if budget <= 0:
        return line[:_INFLIGHT_MAX_LINE_CHARS]
    return f"{prefix}{content[:budget]}…{suffix}"


def _unharvested_copies_line(rows: list[dict]) -> str:
    """``delegate_ledger.ticket_copy_records(unharvested=True)`` 실값 → 미회수 라운드 준비 불릿."""
    prefix = "- 미회수 라운드 준비 "
    suffix = " → `python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested`"
    if not rows:
        return _inflight_capped_line(prefix=prefix, content="0건", suffix=suffix)
    labels = [f"{row['ticket']} {row['role']}#{row['ordinal']}" for row in rows]
    content = f"{len(rows)}건: {_fold_join(labels, _INFLIGHT_UNHARVESTED_SHOWN, '건')}"
    return _inflight_capped_line(prefix=prefix, content=content, suffix=suffix)


def _unfinished_raw_line(rows: list[dict], ledger_path: Path) -> str:
    """``raw_output.unfinished_raw_records(lock=False)`` 실값 → 미마감 위임 raw 불릿(kill 증거)."""
    prefix = "- 미마감 위임 raw "
    suffix = " → `python3 .project_manager/tools/pm_delegate.py raw --unfinished`"
    if not rows:
        return _inflight_capped_line(
            prefix=prefix, content=f"0건 (장부: {ledger_path})", suffix=suffix,
        )
    labels = [
        f"{_inflight_safe_text(row.get('started_at'))} "
        f"{_inflight_safe_text(row.get('surface'))}/{_inflight_safe_text(row.get('harness'))}"
        for row in rows
    ]
    content = (
        f"{len(rows)}건: {_fold_join(labels, _INFLIGHT_RAW_SHOWN, '건')} (장부: {ledger_path})"
    )
    return _inflight_capped_line(prefix=prefix, content=content, suffix=suffix)


def _claimed_tickets_line(ticket_ids: list[str]) -> str:
    """claimed 티켓 디렉터리 glob 실값 → ID 목록 불릿(개수만이 아니라 목록으로 편입)."""
    prefix = "- claimed 티켓 "
    if not ticket_ids:
        return f"{prefix}0건"
    safe_ids = sorted(_inflight_safe_text(tid) for tid in ticket_ids)
    folded = _fold_join(safe_ids, _INFLIGHT_CLAIMED_SHOWN, "건")
    return _inflight_capped_line(prefix=prefix, content=f"{len(ticket_ids)}건: {folded}", suffix="")


def _git_status_counts(path: Path, timeout: float) -> tuple[int, int, int] | None:
    """``git --no-optional-locks status --porcelain`` 1회 — staged/미staged/untracked 개수.

    git 부재·비-repo·timeout·기타 예외는 전부 흡수해 ``None`` — WIP 프로브 계약(전 예외 흡수)의
    실행 지점이다. 이 함수는 절대 예외를 올리지 않는다(호출부가 subprocess 0 장부 조회와 섞이지
    않게 이 함수 하나로 유일한 예외를 가둔다).
    """
    if timeout <= 0 or shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:  # noqa: BLE001 — WIP 프로브 전 예외 흡수 계약.
        return None
    if result.returncode != 0:
        return None
    staged = unstaged = untracked = 0
    for line in result.stdout.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
            continue
        index_state = line[0]
        worktree_state = line[1] if len(line) > 1 else " "
        if index_state not in (" ", "?"):
            staged += 1
        if worktree_state not in (" ", "?"):
            unstaged += 1
    return staged, unstaged, untracked


def _wip_probe_targets(pm_home: Path, task: str) -> tuple[list[tuple[str, Path]], int]:
    """WIP 프로브 대상 — PM 홈 자리를 먼저 예약한 뒤 leased 슬롯으로 나머지를 채운다.

    반환은 ``(프로브할 대상, 상한 때문에 못 본 leased 슬롯 수)``. leased 슬롯이 상한보다 많을
    때 뒤에 append 되는 PM 홈이 slice 로 조용히 밀려나던 결함(4-slot 프로브
    실측: PM 홈·slot-d 소실)을 막는다 — PM 홈이 기존 슬롯과 겹치지 않을 때만 자리를 예약해
    상한을 낭비하지 않는다. 못 본 슬롯은 spawn 없이 개수만 돌려준다(``_wip_slot_line`` 이
    "외 N개(프로브 생략)"로 표면화 — 침묵 누락 금지).
    """
    slots = list(_lease_task_slots(pm_home, task))
    seen = {os.path.normcase(str(path.resolve(strict=False))) for _label, path in slots}
    pm_home_path = Path(pm_home)
    pm_home_key = os.path.normcase(str(pm_home_path.resolve(strict=False)))
    pm_home_distinct = pm_home_key not in seen
    slot_budget = max(0, _WIP_PROBE_MAX_CALLS - (1 if pm_home_distinct else 0))
    probed_slots = slots[:slot_budget]
    skipped = len(slots) - len(probed_slots)
    targets = list(probed_slots)
    if pm_home_distinct:
        targets.append(("PM 홈", pm_home_path))
    return targets, skipped


def _wip_slot_line(
    pm_home: Path, task: str, *, deadline: float, monotonic=time.monotonic,
) -> str:
    """슬롯 WIP 프로브 결과 불릿 — subprocess 0 계약의 유일한 예외(장부 조회는 여전히 spawn 0).

    각 프로브 **직전**에 ``deadline - monotonic()`` 으로 잔여 예산을 다시 계산한다 — 한 번 계산한
    값을 여러 호출에 재사용하면(4-slot·잔여 0.2s 프로브에서 timeout 합계 0.6s
    실측) 앞선 호출의 실제 소요가 뒤 호출 예산을 깎지 않는다는 착시가 생겨 snapshot 자체의
    deadline 을 넘길 수 있다. 잔여가 0 이하면 그 대상부터는 spawn 하지 않고 "예산 소진"으로
    표기한다(장부 조회 subprocess 0 계약과 별개로, 프로브 자신의 시간 계약).
    """
    targets, skipped = _wip_probe_targets(pm_home, task)
    parts: list[str] = []
    for label, path in targets:
        safe_label = _inflight_safe_text(label)
        remaining = deadline - monotonic()
        if remaining <= 0:
            parts.append(f"{safe_label} 예산 소진")
            continue
        timeout = min(remaining, _WIP_PROBE_TIMEOUT_SECONDS)
        counts = _git_status_counts(path, timeout)
        if counts is None:
            parts.append(f"{safe_label} 조회 불가")
        else:
            staged, unstaged, untracked = counts
            parts.append(
                f"{safe_label} staged {staged} / 미staged {unstaged} / untracked {untracked}"
            )
    content = _fold_join(parts, _INFLIGHT_SLOT_SHOWN, "개") if parts else "대상 없음"
    if skipped > 0:
        content += f" 외 {skipped}개(프로브 생략)"
    return _inflight_capped_line(prefix="- 슬롯 WIP: ", content=content, suffix="")


def _section_query_failure_line(cause: str, *, heading: str, label: str) -> str:
    """장부 조회 절의 skew 흡수 경계 표출 — 흡수해도 stderr 1줄 + payload 1줄로 표출한다.

    장부·엔진 사본을 읽는 절(진행 중 작업·열린 todo)이 이 한 함수를 쓴다 — 접는 모양이 절마다
    갈리면 압축 재주입의 줄 상한·heading 주입 방어가 절마다 따로 틀어진다.

    복구 마커 호출(사유 등록 검증)은 흡수 지점인 호출부 except 핸들러가 직접 수행하고
    (`skew` 로 그 판정을 받는다 — 정적 가드의 시야가 핸들러 본문이다), 여기서는 표출만 한다.
    stderr 진단은 원문 그대로(다중행이어도 무방) 남기되, payload 에 실리는 절 본문은
    ``_inflight_safe_text``로 정규화하고 ``_inflight_capped_line``으로 줄 상한을 지킨다
    (예외 메시지에 개행이 섞이면 가짜 heading 을 주입할 수 있다).
    """
    print(f"경고: {label} 조회 실패 ({cause}) — 절을 1줄로 접음", file=sys.stderr)
    line = _inflight_capped_line(
        prefix="- 조회 실패: ",
        content=_inflight_safe_text(cause),
        suffix=" → `python3 .project_manager/tools/pm_bootstrap.py`로 직접 확인",
    )
    return heading + "\n" + line + "\n"


def _inflight_section(
    pm_home: Path, task: str, *, deadline: float, monotonic=time.monotonic,
) -> str:
    """미회수 라운드 준비·미마감 raw·claimed 티켓·슬롯 WIP 을 in-process 조회로 편입한다.

    장부 조회는 subprocess 0(``delegate_ledger.ticket_copy_records``·``raw_output.raw_records``
    in-process 호출) — WIP git 프로브(``_wip_slot_line``)만 유일한 예외(자체 상한·전 예외 흡수).
    네 조회 중 하나라도 예외를 올리면 절 전체를 1줄로 접는다(다른 절은 온전 — 절 단위 fail-soft).
    각 불릿은 ``_inflight_safe_text``/``_inflight_capped_line`` 으로 이미 단일 줄이지만, 물리
    행 수도 조립 단계에서 실제로 강제한다(``_INFLIGHT_MAX_LINES`` — 선언만
    되고 조립에서 미강제였던 결함).
    """
    try:
        unharvested_rows = delegate_ledger.ticket_copy_records(pm_home, unharvested=True)
        _raw_dir, raw_ledger = raw_output.raw_storage_paths(pm_home, "delegate")
        unfinished_raw_rows = raw_output.unfinished_raw_records(raw_ledger, lock=False)
        tickets_dir, _counts = _ticket_counts(pm_home)
        claimed_ids = [
            path.stem for path in (tickets_dir / "claimed").glob("T-*.md")
            if path.is_file()
        ]
    except Exception as exc:  # noqa: BLE001 — 절 단위 fail-soft(등록 경계에서만 skew 흡수).
        skew = _absorb_engine_rev_skew_for_recovery(exc, "inflight_ledger_query")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
        return _section_query_failure_line(
            cause, heading="## 진행 중 작업 (장부 실측)", label="진행 중 작업 절",
        )

    lines = [
        _unharvested_copies_line(unharvested_rows),
        _unfinished_raw_line(unfinished_raw_rows, raw_ledger),
        _claimed_tickets_line(claimed_ids),
        _wip_slot_line(pm_home, task, deadline=deadline, monotonic=monotonic),
    ]
    body_lines = "\n".join(lines).splitlines()
    body = "\n".join(body_lines[:_INFLIGHT_MAX_LINES])
    return "## 진행 중 작업 (장부 실측)\n" + body + "\n"


def build_ctx_guard_guidance(
    band: str,
    *,
    used_pct: int | None = None,
    precompact_at_pct: int | None = None,
) -> str:
    """세 하네스가 공유하는 ctx nudge/compaction 안내문을 렌더한다.

    동적 사용률은 하네스 관측값(사용 한도 대비 쓴 %)을 그대로 표시하되, 세션 연속성 정책은
    :data:`CTX_GUARD_CONTINUITY_GUIDANCE` 한 상수에서만 온다. 밴드는 표현 강도만
    다르고 작업 중단·세션 종료를 유도하지 않는다. final 밴드는 압축 임박 경고 비율
    (`ctx.precompact_at_pct`)을 함께 적는다.
    """
    labels = {
        "nudge": "ctx-nudge",
        "nudge2": "ctx-nudge/강화",
        "final": "ctx-nudge/최종",
        "precompact": "ctx-checkpoint",
    }
    if band not in labels:
        raise ValueError(f"지원하지 않는 ctx guidance band: {band!r}")

    if band == "precompact":
        status = "compaction 경계가 감지됐다. "
        command = (
            "checkpoint 골격이 생성됐다면 현재 구간·서사 불릿을 보충한다. 골격이 없으면 "
            "`python3 .project_manager/tools/pm_log.py checkpoint --task <task> "
            "--trigger compaction`(Windows는 `py -3`)으로 기록한다. "
        )
    else:
        status = ""
        if used_pct is not None:
            status = f"컨텍스트 사용 {used_pct}%"
            if band == "final" and precompact_at_pct is not None:
                status += f" (압축 임박 경고 {precompact_at_pct}% 이상)"
            status += ". "
        checkpoint = "`python3 .project_manager/tools/pm_log.py checkpoint --task <task>"
        if band == "final":
            checkpoint += " --trigger compaction"
        checkpoint += "`"
        command = (
            "checkpoint 기록 권고 구간이다. "
            f"{checkpoint}(Windows는 `py -3`)으로 현재 구간·서사를 기록한다. "
            "`<task>`에는 현재 task 이름을 사용한다. "
        )
    return f"[{labels[band]}] {status}{command}{CTX_GUARD_CONTINUITY_GUIDANCE}"


def cmd_ctx_guidance(args: argparse.Namespace) -> int:
    """ctx 가드 공유 안내를 raw text 또는 Codex hook JSON envelope로 출력한다."""
    text = build_ctx_guard_guidance(
        args.band,
        used_pct=args.used_pct,
        precompact_at_pct=args.precompact_at_pct,
    )
    if args.json:
        _write_machine_line(json.dumps(
            {"systemMessage": text, "suppressOutput": False},
            ensure_ascii=False,
            separators=(",", ":"),
        ))
    else:
        print(text)
    return 0


def _snapshot_within_limits(text: str) -> bool:
    return len(text) <= SNAPSHOT_MAX_CHARS and len(text.encode("utf-8")) <= SNAPSHOT_MAX_BYTES


def _truncate_snapshot_text(text: str) -> str:
    """문자/UTF-8 경계를 깨지 않고 snapshot의 이중 상한 안으로 자른다(머리를 보존)."""
    text = text[:SNAPSHOT_MAX_CHARS]
    if len(text.encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _truncate_identity_keeping_suffix(identity: str, suffix: str) -> str:
    """identity 를 이중 상한 안으로 절단하되 always-keep 접두(``suffix``)는 뒤에 온전히 붙인다."""
    if not suffix:
        return _truncate_snapshot_text(identity.rstrip() + "\n")
    trimmed = identity.rstrip()
    combined = f"{trimmed}\n{suffix}" if trimmed else suffix
    if _snapshot_within_limits(combined):
        return combined
    low, high = 0, len(identity)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = identity[:middle].rstrip()
        probe = f"{candidate}\n{suffix}" if candidate else suffix
        if _snapshot_within_limits(probe):
            low = middle
        else:
            high = middle - 1
    trimmed_identity = identity[:low].rstrip()
    return f"{trimmed_identity}\n{suffix}" if trimmed_identity else suffix


def cap_snapshot_sections(
    identity: str, sections: list[str], *, required: int = 0,
) -> str:
    """뒤 절부터 통째로 덜고, 초대형 정체성 절은 안전 절단해 이중 상한을 지킨다.

    ``required`` 는 ``sections`` 앞 N 개(예: 전언 경고)를 tail-drop 대상에서 제외한다
    — 총량 cap 경로에서도 always-keep 절이 살아남는다(이전엔 identity 가
    거의 상한을 채운 상태에서 필수 접두까지 일반 절처럼 pop 돼 사라졌다 — 7,942-char identity
    프로브에서 hearsay_present=False 로 실측). 필수 접두를 포함해도 넘치면 identity 의 가변
    부분을 절단해 접두를 지킨다(``_truncate_identity_keeping_suffix``).
    """
    kept = list(sections)
    while len(kept) > required:
        text = "\n".join([identity, *kept]).rstrip() + "\n"
        if _snapshot_within_limits(text):
            return text
        kept.pop()
    required_text = "\n".join(kept).rstrip() + "\n" if kept else ""
    return _truncate_identity_keeping_suffix(identity, required_text)


def build_snapshot(
    pm_home: Path,
    session_id: str | None,
    *,
    monotonic=time.monotonic,
) -> tuple[str | None, str | None]:
    """주입 최종 텍스트를 조립한다. 반환은 ``(text, warning)``.

    정체성은 `resolve_session_task`(세션 판정) 하나다. 장부 조회는 subprocess·lock 0 (진행 중
    작업 절의 WIP git 프로브만 상한 3회의 유일한 예외 — `_wip_slot_line` 참고). 절 순서: 전언
    경고(always-keep) → 장부 포인터 → 진행 중 작업 → 열린 todo → 복구 포인터.
    """
    started = monotonic()
    identity_name, reason = resolve_session_task(pm_home, session_id)
    if identity_name is None:
        return None, (
            f"[pm-snapshot] 세션 task 미해소: {reason}. 재주입 생략. "
            "이 세션에서 pm_bootstrap.py --task 이름 으로 task 를 연다."
        )
    identity = _identity_section(pm_home, session_id, identity_name)
    sections: list[str] = []
    # 전언 경고는 always-keep 접두 — 뒤 절부터 폐기하는 timeout 분기·총량 cap 경로 모두에서
    # ``required=always_keep`` 로 cap_snapshot_sections 에 보존 경계를 명시한다.
    sections.append(_hearsay_section())
    always_keep = len(sections)
    # WIP 프로브(F-002)가 절 진입 전이 아니라 **호출 직전마다** 잔여를 재는 절대 deadline.
    deadline = started + SNAPSHOT_TIMEOUT_SECONDS
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(identity, sections, required=always_keep), None

    sections.append(_ledger_section(pm_home, identity_name))
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None

    sections.append(
        _inflight_section(pm_home, identity_name, deadline=deadline, monotonic=monotonic)
    )
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None

    sections.append(_todo_section(pm_home, identity_name))
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None
    sections.append(_recovery_section(identity_name))
    return cap_snapshot_sections(identity, sections, required=always_keep), None


def cmd_snapshot(args: argparse.Namespace) -> int:
    pm_home = pm_paths.owning_pm_home(REPO)
    text, warning = build_snapshot(pm_home, args.session_id)
    if warning:
        print(warning, file=sys.stderr)
    if not text:
        # 주입할 것을 만들지 못한 실행(세션 task 미해소)은 실패다. 실패에 반쪽 답(무응답 엔벨로프)을
        # 내지 않는다: 소비자가 그 한 줄을 "답" 으로 세면 조용한 생략이 되고, 셸 폴백 배선
        # (`snapshot --json || printf …`)에서는 엔벨로프가 두 줄이 된다. stdout 0줄 + rc 1 이면 세 하네스가 같은 실패를 같은 자리에서 본다.
        return 1
    if args.json:
        payload = {"systemMessage": text, "suppressOutput": False}
        _write_machine_line(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )
    else:
        sys.stdout.write(text)
    return 0


def _registered_repos() -> set[str] | None:
    """예약 task 판정용 등록 repo를 fail-soft 해소한다."""
    try:
        return areas_store.registered_repos(pm_paths.home_at(REPO))
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화.
        return None


def _safe_marker_key(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_")[:96] or "unknown"


def _compaction_marker_path(
    task: str,
    session_id: str | None = None,
    boundary_id: str | None = None,
) -> Path:
    key = _safe_marker_key(session_id.strip()) if isinstance(session_id, str) and session_id.strip() \
        else f"task-{_safe_marker_key(task)}"
    boundary = _safe_marker_key(boundary_id.strip()) \
        if isinstance(boundary_id, str) and boundary_id.strip() else "unknown-boundary"
    # test_terminology의 runtime allowance와 같은 한 세그먼트 literal을 유지한다.
    return (
        REPO / ".project_manager" / ".local/ctx-stop" /
        f"compact-checkpoint.{key}.{boundary}"
    )


def claim_compaction_checkpoint(
    marker: Path,
    *,
    phase: str,
) -> bool | None:
    """경계 marker를 단일 ``O_EXCL``로 선점한다.

    ``True``는 선점 성공, ``False``는 같은 경계의 실제 중복, ``None``은 장부 I/O 실패다.
    marker 경로가 boundary id를 포함하므로 시간창·stat·만료 unlink가 전혀 필요 없다.
    """
    marker = Path(marker)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return None
    try:
        os.write(fd, f"compaction checkpoint claimed\nphase={phase}\n".encode("utf-8"))
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return True


def _compaction_scope_key(task: str, session_id: str | None) -> str:
    """compaction 상태 파일이 공유하는 세션 우선 scope key."""
    if isinstance(session_id, str) and session_id.strip():
        return _safe_marker_key(session_id.strip())
    return f"task-{_safe_marker_key(task)}"


def _implicit_boundary_state_path(task: str, session_id: str | None) -> Path:
    """구 어댑터의 scope prefix 또는 경계별 durable pending 파일 경로."""
    key = _compaction_scope_key(task, session_id)
    return REPO / ".project_manager" / ".local/ctx-stop" / f"compact-boundary.{key}"


def _new_implicit_boundary_id() -> str:
    """archive·프로세스 재시작 뒤에도 재사용되지 않는 implicit boundary ID."""
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return f"implicit-{stamp}-{uuid.uuid4().hex}"


def _write_implicit_boundary_state(prefix: Path, boundary_id: str) -> Path:
    """pre 경계를 덮어쓰지 않는 boundary별 pending 파일로 원자 생성한다."""
    path = prefix.with_name(f"{prefix.name}.{_safe_marker_key(boundary_id)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (boundary_id + "\n").encode("utf-8"))
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return path


def _read_implicit_boundary_state(prefix: Path) -> tuple[str, Path] | None:
    """scope의 가장 오래된 유효 pending 경계를 FIFO로 고른다.

    checkpoint log lock 안에서 호출되므로 여러 PostCompact도 서로 다른 파일을 소비한다.
    """
    try:
        pending = sorted(prefix.parent.glob(f"{prefix.name}.*"))
    except OSError:
        return None
    for path in pending:
        try:
            boundary_id = fs.read_text_shared(path, encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if boundary_id.startswith("implicit-"):
            return boundary_id, path
    return None


def _clear_implicit_boundary_state(path: Path | None, boundary_id: str) -> None:
    """동일 ID를 가리킬 때만 pre/post hand-off pointer를 best-effort 소거한다."""
    if path is None:
        return
    try:
        if fs.read_text_shared(path, encoding="utf-8").strip() == boundary_id:
            path.unlink()
    except (OSError, UnicodeError):
        pass


def _resolve_compaction_boundary(
    args: argparse.Namespace,
    *,
    task: str,
    current_text: str,
) -> tuple[str | None, str | None, Path | None]:
    """명시 boundary를 우선하고 구 어댑터 pre/post에는 durable unique ID를 부여한다.

    ``current_text``는 호출 호환을 위해 받지만 ID 재료로 쓰지 않는다. 로그 archive가 entry를
    제거해도 영구 marker와 새 boundary가 충돌하지 않아야 하기 때문이다.
    """
    del current_text
    phase = getattr(args, "phase", None)
    explicit = getattr(args, "boundary_id", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip(), phase or "unspecified", None
    if phase not in {"pre", "post"}:
        return None, None, None

    state_prefix = _implicit_boundary_state_path(
        task, getattr(args, "session_id", None),
    )
    if phase == "pre":
        boundary_id = _new_implicit_boundary_id()
        try:
            state_path = _write_implicit_boundary_state(state_prefix, boundary_id)
        except OSError:
            # pending 장부 I/O 실패여도 pre checkpoint 자체는 고유 ID로 계속 기록한다.
            state_path = None
        return boundary_id, phase, state_path

    pending = _read_implicit_boundary_state(state_prefix)
    if pending is None:
        # pre가 없거나 pending 장부가 손상된 post도 과거 marker와 재충돌하지 않는다.
        boundary_id = _new_implicit_boundary_id()
        state_path = None
    else:
        boundary_id, state_path = pending
    return boundary_id, phase, state_path


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """checkpoint 골격 append. compaction은 boundary/phase marker로 경계당 1건만 허용한다."""
    task = getattr(args, "task", None)
    identity_name = task
    if not task:
        identity_name, reason = resolve_session_task(REPO, getattr(args, "session_id", None))
        if identity_name is None:
            print(
                f"[중단] checkpoint 세션 task 미해소: {reason}. --task 이름 을 주거나 "
                "이 세션에서 pm_bootstrap.py --task 이름 으로 task 를 연다.",
                file=sys.stderr,
            )
            return 1
        task = identity_name
    if task is not None:
        try:
            identity_args.validate_task_name(task, _registered_repos())
        except identity_args.InvalidTaskName as exc:
            print(
                f"[중단] {exc} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
                "(`<repo>_<N>`)은 쓸 수 없다.",
                file=sys.stderr,
            )
            return 1
    if not CURRENT_FILE.exists():
        print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
        return 2
    entry = build_checkpoint_entry(task, args.trigger)
    breadcrumb = _PRECOMPACT_BREADCRUMB if getattr(args, "breadcrumb", False) else ""
    payload = breadcrumb + "\n" + entry
    # 선행 LF는 기존 파일이 trailing newline 없이 끝나도 새 `##` entry 경계를 보장한다.
    # 이미 LF로 끝난 파일에는 빈 줄 하나가 추가될 뿐이며, 전체 payload는 단일 원자 write다.
    if args.trigger != "compaction":
        append_log(CURRENT_FILE, payload)
        print(f"✓ checkpoint append: task={identity_name} · trigger={args.trigger}")
        return 0

    marker = None
    claimed = None
    # boundary 유도→marker 선점→append를 log lock 하나에 묶어 동시 호출도 같은 log 상태를 본다.
    with log_write_lock(CURRENT_FILE):
        current_text = fs.read_text_shared(CURRENT_FILE, encoding="utf-8")
        boundary_id, phase, implicit_state = _resolve_compaction_boundary(
            args, task=identity_name, current_text=current_text,
        )
        if boundary_id is not None and phase is not None:
            marker = _compaction_marker_path(
                identity_name, getattr(args, "session_id", None), boundary_id,
            )
            claimed = claim_compaction_checkpoint(marker, phase=phase)
            if claimed is False:
                if phase == "post":
                    _clear_implicit_boundary_state(implicit_state, boundary_id)
                print(
                    f"✓ checkpoint dedup skip: task={identity_name} · trigger=compaction"
                )
                return 0
            if claimed is None:
                print(
                    "[pm-checkpoint] dedup 장부 I/O 실패 — durable checkpoint 기록은 계속합니다.",
                    file=sys.stderr,
                )
        else:
            print(
                "[pm-checkpoint] boundary/phase 식별자 없음 — dedup 없이 durable checkpoint를 기록합니다.",
                file=sys.stderr,
            )
        try:
            append_log_locked(CURRENT_FILE, payload)
        except Exception:
            # 같은 log lock 안에서만 rollback하므로 후속 선점과 unlink 경쟁이 생기지 않는다.
            if marker is not None and claimed is True:
                with contextlib.suppress(OSError):
                    marker.unlink()
            raise
        if phase == "post":
            _clear_implicit_boundary_state(implicit_state, boundary_id)
    print(f"✓ checkpoint append: task={identity_name} · trigger={args.trigger}")
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

    p = sub.add_parser(
        "ctx-guidance",
        help="세 하네스 공용 ctx 연속성 안내 출력 (쓰기 0)",
    )
    p.add_argument(
        "--band", choices=("nudge", "nudge2", "final", "precompact"), required=True,
        help="안내 강도/경계 종류",
    )
    p.add_argument("--used-pct", type=int, metavar="N", help="관측 컨텍스트 사용률(사용 한도 대비 쓴 %%)")
    p.add_argument("--precompact-at-pct", type=int, metavar="N",
                   help="final 밴드의 압축 임박 경고 비율(ctx.precompact_at_pct)")
    p.add_argument(
        "--json", action="store_true",
        help="Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력",
    )
    p.set_defaults(fn=cmd_ctx_guidance)

    p = sub.add_parser("checkpoint", help="compaction/manual 경계 보충 박제 골격 append")
    p.add_argument("--task", metavar="이름", help="checkpoint 귀속 task 이름 (생략 시 --session-id 로 그 세션이 연 task)")
    p.add_argument(
        "--trigger",
        choices=("compaction", "manual"),
        default="manual",
        help="박제 계기 (기본값: manual)",
    )
    p.add_argument("--session-id", help="하네스 세션 식별자 (task 판정 입력·compaction dedup 1순위 키)")
    p.add_argument("--boundary-id", help="하네스가 관측한 compaction 경계 식별자")
    p.add_argument("--phase", choices=("pre", "post"), help="compaction hook phase")
    p.add_argument(
        "--breadcrumb", action="store_true",
        help="Claude PreCompact breadcrumb를 checkpoint와 같은 PM 홈 append에 포함",
    )
    p.set_defaults(fn=cmd_checkpoint)

    p = sub.add_parser("snapshot", help="compaction 뒤 재주입할 PM 정체성·장부 포인터 출력")
    p.add_argument("--session-id", help="하네스 세션 식별자 (그 세션이 연 task 를 찾는다)")
    p.add_argument(
        "--json", action="store_true",
        help="Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력",
    )
    p.set_defaults(fn=cmd_snapshot)

    return parser


def main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    # snapshot/checkpoint가 등록 worktree에서 발화하면 PM 홈의 동기화된 엔진으로 치환한다.
    # tail/archive 등 기존 명령은 자기 앵커 의미를 보존한다.
    if args.cmd in {"snapshot", "checkpoint"}:
        pm_home = pm_paths.owning_pm_home(REPO)
        if pm_home != REPO.resolve(strict=False):
            canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
            if not canonical.is_file():
                print(
                    f"[pm-{args.cmd}] PM 홈 엔진 부재 — 경계 처리를 생략: {canonical}",
                    file=sys.stderr,
                )
                return 2
            timeout = SNAPSHOT_TIMEOUT_SECONDS if args.cmd == "snapshot" else 5.0
            try:
                result = subprocess.run(
                    [sys.executable, str(canonical), *raw_argv],
                    cwd=str(pm_home),
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[pm-{args.cmd}] PM 홈 엔진 실행 실패: {exc}", file=sys.stderr)
                return 1
            # 자식 rc 를 그대로 돌려준다 — 훅이 실패 사유를 사람에게 보이려면 실패가 실패로 와야 한다.
            return result.returncode
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
