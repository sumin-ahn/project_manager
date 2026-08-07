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
    python3 .project_manager/tools/pm_log.py checkpoint --task NAME [--trigger compaction|manual]

명령:
  tail                  — current.md 의 마지막 `## [..]` entry 만 출력 (의미단위 읽기 헬퍼).
  archive --before DATE — current.md 에서 DATE *미만* 날짜의 entry 들을 archive/ 새 슬라이스로
                          이동하고 current.md 는 최근만 남긴다. 멱등 (옮길 게 없으면 no-op).
  archive --keep-last N — current.md 에서 최근 N entry 만 남기고 나머지(오래된 쪽)를 archive/
                          새 슬라이스로 봉인한다. 날짜 계산 없이 개수로 자른다. N ≥ entry 수면 no-op.
                          `--before` 와 상호배타 — 정확히 하나만 지정한다.
  migrate               — 기존 단일 `log.md` → `log/archive/0000-legacy.md` 로 봉인 +
                          `log/current.md` 생성. 일회성·멱등 (current.md 가 이미 있으면 no-op).
  checkpoint            — compaction/manual 경계의 보충 박제 골격을 current.md 에 append.
                          호출마다 신규 entry 를 만들며 서사는 PM 이 채운다.

결정:
  - 쓰기 대상은 current.md 단일 경로다. legacy `log.md` 는 migrate 로 봉인만 한다 — 런타임 fallback 없음.
  - 편집은 entry(`## [YYYY-MM-DD] ...`) 경계 기준·멱등·실패 시 비편집 (ticket_finish.py 패턴 계승).
  - LLM 미호출 — stdlib 만.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import re
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

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
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
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


# ── 엔진 사본 rev 스탬프 (pm_bootstrap deep-import target) ────────────────
ENGINE_REV = "v1.6.2"


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


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud).

    부재는 raw `FileNotFoundError`로 터져 복구 방법(pm-update 재동기)을 알려주지 않는다 —
    원인이 부분/수동 복사라는 점은 stale 사본과 같으므로 같은 marked skew로 표출한다
    (board.py `_require_engine_sibling` 동형·self-contained 복제).
    """
    if path.exists():
        return
    err = RuntimeError(
        f"엔진 사본 불완전 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 형제 "
        f"{filename} 을(를) 찾지 못했다: {path} (부분/수동 복사). `pm-update`(또는 "
        "pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
    )
    err._engine_rev_skew = True
    raise err


def _load_identity_args():
    """공용 task 이름 validator를 같은 tools/에서 경로 로드한다."""
    ia_path = Path(__file__).resolve().parent / "identity_args.py"
    return _load_module_from_path(
        ia_path, "identity_args.py", verifier=_verify_engine_rev,
    )


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/에서 경로 로드한다.

    write 경로에서만 지연 로드한다 — pm_bootstrap이 fail-soft로 재사용하는 *읽기* 경로
    (`split_entries`)까지 seam 부재로 무너뜨리지 않기 위해서다. 로드 실패는 흡수하지 않고
    (fail-loud) 캐시하되, 중앙 loader가 소비 때마다 baked rev를 재검증하므로 사본 skew는
    계속 표출된다.
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


REPO = Path(__file__).resolve().parents[2]
WIKI_DIR = REPO / ".project_manager" / "wiki"
LOG_DIR = WIKI_DIR / "log"
CURRENT_FILE = LOG_DIR / "current.md"
ARCHIVE_DIR = LOG_DIR / "archive"
LEGACY_LOG = WIKI_DIR / "log.md"

# task 태그 sentinel은 ticket_finish.py·pm_handoff.py의 동명 상수와 미러한다.
# 모듈 격리를 유지하려고 각 생산자가 상수를 소유한다.
_TASK_TAG_PREFIX = "task:"

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

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `file_lock` seam이 소유하고
    경로 규약만 이 도구가 정한다.
    """
    with _load_file_lock().exclusive_file_lock(_log_lock_path(Path(log_path))):
        yield


def append_log(path: Path, text: str) -> None:
    """공유 log append 공개 seam — flock 안에서 O_APPEND 단일 write.

    O_APPEND 원자 추가 자체는 공용 `file_lock` seam이 소유한다 — pm_log는 락 경로
    규약과 부모 디렉토리 생성만 정한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with log_write_lock(path):
        _load_file_lock().append_atomic(path, text)


def _replace_atomic(path: Path, text: str) -> None:
    """같은 디렉터리의 임시 파일을 쓴 뒤 `os.replace`로 원자 교체한다."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(tmp), str(path))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


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

    # 존재 확인부터 최신 내용 read, archive index 발행, current 원자 교체까지 한 lock
    # 구간이다. append writer도 같은 lock을 쓰므로 archive가 읽은 뒤 들어온 entry가
    # stale `new_current`에 덮여 유실되는 interleave가 없다.
    with log_write_lock(CURRENT_FILE):
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
        slice_path.write_text(slice_body, encoding="utf-8")
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


def build_checkpoint_entry(
    task: str,
    trigger: str = "manual",
    date: str | None = None,
) -> str:
    """task의 compaction/manual 경계 보충 박제 골격을 만든다."""
    if date is None:
        date = datetime.date.today().isoformat()
    return (
        f"## [{date}] checkpoint | ({_TASK_TAG_PREFIX}{task}) — {trigger}\n\n"
        "- 구간: <직전 박제 경계 이후>\n"
        "- 서사: <PM 손>\n"
    )


def _registered_repos() -> set[str] | None:
    """예약 task 판정용 등록 repo를 fail-soft 해소한다(skew만 fail-loud)."""
    board_path = Path(__file__).resolve().parent / "board.py"
    try:
        board = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
        return board.registered_repos()
    except Exception as exc:  # noqa: BLE001 — 부재/areas 파싱 실패는 예약패턴 검증만 완화.
        if _is_engine_rev_skew(exc):
            raise
        return None


# ── PM-홈 worktree 오실행 가드 (checkpoint 쓰기 경로 전용) ──────────────

def _pm_home_misanchor() -> Path | None:
    """REPO가 PM 홈의 등록 worktree면 그 PM 홈을 반환한다(board detector 재사용)."""
    board_path = Path(__file__).resolve().parent / "board.py"
    try:
        board = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — detector 부재/로드 실패는 현행 동작 보존.
        if _is_engine_rev_skew(exc):
            raise
        return None
    detector = getattr(board, "_pm_home_worktree_misanchor", None)
    if detector is None:
        return None
    try:
        return detector(REPO)
    except Exception:  # noqa: BLE001 — detector 해소 실패는 오탐 없이 fail-soft.
        return None


def _guard_worktree_misanchor() -> bool:
    """checkpoint가 코드 전용 worktree log에 쓰기 전에 fail-loud 한다."""
    pm_home = _pm_home_misanchor()
    if pm_home is None:
        return False
    print(
        "[중단] `pm_log checkpoint` 를 worktree(코드 전용) 트리에서 실행했습니다 — "
        "checkpoint log는 PM 홈이 소유합니다. 이대로면 이 worktree에 stray log를 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {REPO})",
        file=sys.stderr,
    )
    return True


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """checkpoint 골격을 current.md에 append한다 (호출별 신규 entry)."""
    identity_args = _load_identity_args()
    try:
        identity_args.validate_task_name(args.task, _registered_repos())
    except identity_args.InvalidTaskName as exc:
        print(
            f"[중단] {exc} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
            "(`<repo>_<N>`·⑥)은 쓸 수 없다.",
            file=sys.stderr,
        )
        return 1
    if not CURRENT_FILE.exists():
        print(f"(current.md 없음: {_rel(CURRENT_FILE)} — migrate 먼저)", file=sys.stderr)
        return 2
    entry = build_checkpoint_entry(
        args.task,
        args.trigger,
    )
    # 선행 LF는 기존 파일이 trailing newline 없이 끝나도 새 `##` entry 경계를 보장한다.
    # 이미 LF로 끝난 파일에는 빈 줄 하나가 추가될 뿐이며, 전체 payload는 단일 원자 write다.
    append_log(CURRENT_FILE, "\n" + entry)
    print(f"✓ checkpoint append: task={args.task} · trigger={args.trigger}")
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

    p = sub.add_parser("checkpoint", help="compaction/manual 경계 보충 박제 골격 append")
    p.add_argument("--task", required=True, metavar="이름", help="checkpoint 귀속 task 이름")
    p.add_argument(
        "--trigger",
        choices=("compaction", "manual"),
        default="manual",
        help="박제 계기 (기본값: manual)",
    )
    p.set_defaults(fn=cmd_checkpoint)

    return parser


def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    args = build_parser().parse_args(argv)
    # checkpoint만 쓰기 op다. tail 등 read-only 서브커맨드는 기존처럼 가드하지 않는다.
    if args.cmd == "checkpoint" and _guard_worktree_misanchor():
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
