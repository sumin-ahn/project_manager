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
    python3 .project_manager/tools/pm_log.py snapshot [--cwd PATH] [--json]

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
import subprocess
import sys
import time
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
                        # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                        # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                        # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                        # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                        # ModuleNotFoundError · T-0746). 블록은 stdlib-only 라 지역 import 로 둔다.
                        import importlib as _bootstrap_importlib
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


# ── 엔진 사본 rev 스탬프 (pm_bootstrap deep-import target) ────────────────
ENGINE_REV = "v1.7.6"


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


def _write_machine_line(text: str) -> None:
    """기계 판독 한 줄(JSON 페이로드)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    사람 출력(``print``)은 PowerShell 캡처에서 콘솔 codepage(cp949 등)로 강등될 수 있고 그
    치환은 되돌릴 수 없다 — 다른 프로세스가 파싱하는 출력은 공용 seam 으로 UTF-8 bytes 를
    직접 쓴다(콘솔 코덱 전환과 독립).
    """
    console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
        cache=True,
    )
    console_encoding.write_machine_line(text)


# ── 공유 읽기 (등재 예외 · 형제 없이도 떠야 하는 판독) ──────────────────────
# 원자 교체 대상을 읽는 지점은 공용 seam 을 지난다([[T-0729]]) — 일반 `open` 리더가 하나라도
# 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막는다. 다만 이 모듈의 판독은 위 로더 주석의
# 계약대로 **형제 없이도 떠야 한다**(pm_bootstrap 이 `split_entries` 를 fail-soft 로 재사용한다).
# 그래서 여기는 **등재된 예외**다 — seam 이 있으면 쓰고, 없거나 로드가 실패하면 사유를 남기고
# 종전 읽기로 진행한다. 잃는 것은 "Windows 에서 이 판독이 열려 있는 동안의 교체 한 번" 이고,
# 얻는 것은 "깨진 트리에서도 로그 판독이 산다" 다.

# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "shared_read_seam": (
        "판독은 형제 없이도 떠야 한다 — pm_bootstrap 이 이 모듈의 로그 판독을 fail-soft 로 "
        "재사용하므로, seam 부재로 판독이 죽으면 부트스트랩 요약 전체가 함께 죽는다. "
        "부재/손상/혼합 사본을 흡수하되 조용하지 않게 사유를 stderr 로 남기고 종전 읽기로 "
        "진행한다(잃는 것은 Windows 에서 이 판독 중의 원자 교체 한 번)"
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



# 강등 사유는 프로세스당 한 번만 알린다 — 판독마다 찍으면 진단이 자기 소음에 묻힌다.
_shared_read_degraded = False


def _warn_shared_read_degraded(cause: str) -> None:
    """강등 사유를 **프로세스당 한 번** 알린다 (판독마다 찍으면 진단이 자기 소음에 묻힌다)."""
    global _shared_read_degraded
    if _shared_read_degraded:
        return
    _shared_read_degraded = True
    print(
        f"경고: 공유 읽기 seam 을 쓸 수 없어 일반 읽기로 진행합니다 ({cause}) — Windows "
        "에서는 이 판독이 열려 있는 동안 원자 교체가 실패할 수 있습니다. `pm-update` 로 "
        ".project_manager/tools/ 전체를 재동기하십시오.",
        file=sys.stderr,
    )


def _shared_read_api(name: str):
    """공유 읽기 seam 의 함수 하나 — 없거나 못 쓰면 `None` (등재 예외의 강등 분기·loud).

    **부재/손상 로드**와 **구세대 사본**(로드는 되는데 그 함수가 없는 형상)을 함께 본다 — 쓰기
    축의 등재 예외가 `getattr(..., "atomic_replace", None)` 로 두 형상을 같이 받는 것과 같다.
    한쪽만 보면 부분 업그레이드 트리에서 AttributeError 로 죽는다.
    """
    global _shared_read_degraded
    try:
        seam = _load_file_lock()
    except Exception as exc:  # noqa: BLE001 — 부재/손상/혼합은 이 판독의 정상 입력이다.
        skew = _absorb_engine_rev_skew_for_recovery(exc, "shared_read_seam")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
        _warn_shared_read_degraded(cause)
        return None
    api = getattr(seam, name, None)
    if api is None:
        _warn_shared_read_degraded(f"구세대 file_lock 사본에 {name} 이(가) 없음")
    return api


def _read_text_shared(path, *, encoding=None, errors=None, newline=None) -> str:
    """`file_lock.read_text_shared` — seam 을 못 쓰면 같은 의미의 종전 읽기로 강등한다."""
    api = _shared_read_api("read_text_shared")
    if api is not None:
        return api(path, encoding=encoding, errors=errors, newline=newline)
    with open(path, "r", encoding=encoding, errors=errors, newline=newline) as handle:
        return handle.read()


def _read_bytes_shared(path) -> bytes:
    """`file_lock.read_bytes_shared` — seam 을 못 쓰면 같은 의미의 종전 읽기로 강등한다."""
    api = _shared_read_api("read_bytes_shared")
    if api is not None:
        return api(path)
    with open(path, "rb") as handle:
        return handle.read()


def _open_shared(path, *, binary, encoding=None, errors=None, newline=None):
    """`file_lock.open_shared` — seam 을 못 쓰면 같은 의미의 종전 열기로 강등한다."""
    api = _shared_read_api("open_shared")
    if api is not None:
        return api(path, binary=binary, encoding=encoding, errors=errors, newline=newline)
    if binary:
        return open(path, "rb")
    return open(path, "r", encoding=encoding, errors=errors, newline=newline)


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
SNAPSHOT_PM_STATE_LINES = 24
_SNAPSHOT_IDENTITY_HEADING = "## PM 정체성 (compaction 복구)"
_CTX_WINDOW_MISMATCH_READ_FAILED = object()
_CTX_WINDOW_MISMATCH_READ_WARNING = (
    "[pm-snapshot] ctx-window-mismatch 원장 판독 실패 — 기존 armed payload를 보존하고 재시도"
)
_CTX_DIAGNOSTIC_APPEND_FAILED_SIGNAL = "[pm-checkpoint] ctx-diagnostic-append-failed"
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
        append_log_locked(path, text)


def append_log_locked(path: Path, text: str) -> None:
    """이미 ``log_write_lock(path)``를 보유한 호출자의 O_APPEND primitive.

    read→판정→append 전체를 한 임계구역에 묶어야 하는 소비자용이다. 이 함수 자체는 락을
    잡지 않는다. 공개 기본 경로는 계속 :func:`append_log`이며, 소비자는 반드시 같은
    ``log_write_lock`` 문맥 안에서만 이 함수를 호출한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _load_file_lock().append_atomic(path, text)


def _replace_atomic(path: Path, text: str) -> None:
    """같은 디렉터리의 임시 파일을 쓴 뒤 `file_lock.atomic_replace`로 원자 교체한다."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        # newline=""은 입력 문자열의 LF/CRLF/mixed newline bytes를 그대로 인코딩한다.
        # read-modify-write 소비자가 PM 작성 본문을 byte-preserve할 때 플랫폼 개행 변환으로
        # 범위 밖 bytes가 흔들리지 않게 한다.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _load_file_lock().atomic_replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_text_exact(path: Path) -> str:
    """UTF-8 파일을 universal-newline 변환 없이 읽어 CRLF/mixed bytes를 보존한다."""
    return _read_bytes_shared(Path(path)).decode("utf-8")


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
    *,
    session: str | None = None,
    ctx_band_checked: bool = False,
    ctx_band_missed: bool = False,
    ctx_window_tokens: int | None = None,
    ctx_observed_tokens: int | None = None,
    harness: str | None = None,
) -> str:
    """task/slot/solo의 compaction/manual 경계 보충 박제 골격을 만든다."""
    if date is None:
        date = datetime.date.today().isoformat()
    identity_tag = (
        f" | ({_TASK_TAG_PREFIX}{task})" if task
        else f" | ({session})" if session
        else ""
    )
    ctx_advisory = (
        build_ctx_window_mismatch_advisory(
            ctx_window_tokens=ctx_window_tokens,
            ctx_observed_tokens=ctx_observed_tokens,
            harness=harness,
        )
        if ctx_band_missed else ""
    )
    ctx_check = (
        f"<!-- ctx-band-check: {'missed' if ctx_band_missed else 'fired'} -->\n"
        if ctx_band_checked else ""
    )
    return (
        # Consumer grammar: pm_handoff.collect_session_entries()의 task/slot/solo
        # 분기와 동기화한다.
        f"## [{date}] checkpoint{identity_tag} — {trigger}\n\n"
        f"{ctx_check}"
        f"{ctx_advisory}"
        "- 구간: <직전 박제 경계 이후>\n"
        "- 서사: <PM 손>\n"
    )


def build_ctx_window_mismatch_advisory(
    *,
    ctx_window_tokens: int | None,
    ctx_observed_tokens: int | None,
    harness: str | None = None,
) -> str:
    """밴드 미발화 압축의 loud 진단과 고정 처방을 렌더한다.

    어댑터는 marker 판정과 숫자 전달만 맡고, checkpoint/snapshot에 들어갈 최종 텍스트는
    엔진이 단독 소유한다. 관측 토큰이 없으면 오탐성 숫자를 만들지 않고 정성 처방만 남긴다.
    """
    window = ctx_window_tokens if isinstance(ctx_window_tokens, int) and ctx_window_tokens > 0 \
        else None
    observed = (
        ctx_observed_tokens
        if isinstance(ctx_observed_tokens, int) and ctx_observed_tokens > 0
        else None
    )
    evidence = f"설정 {window:,} tokens" if window is not None else "해소된 설정 창"
    if observed is not None:
        evidence += f" · PreCompact 관측 {observed:,} tokens"
    remedy_limit = (
        f"관측 사용량 {observed:,} tokens 이하"
        if observed is not None else "실 auto-compact 지점 이하"
    )
    normalized_harness = (
        harness.strip().lower()
        if isinstance(harness, str)
        and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", harness.strip().lower())
        else None
    )
    config_key = "ctx_window_tokens" + (
        f"_{normalized_harness}" if normalized_harness is not None else ""
    )
    return (
        "> ⚠ [ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼 — 이번 압축 사이클에서 "
        f"nudge/nudge2/final 밴드가 한 번도 발화하지 않음 ({evidence}).\n"
        f"> 처방: `.project_manager/local.conf`의 `{config_key}`를 "
        f"{remedy_limit}로 낮추고 다음 사이클 밴드 발화를 확인.\n\n"
    )


# ── compaction 경계 읽기 모델 (snapshot·checkpoint 공통) ───────────────

def _read_json_object(path: Path) -> dict | None:
    """JSON object point-read. 부재·손상·스키마 불일치는 fail-soft ``None``."""
    try:
        value = json.loads(_read_text_shared(Path(path), encoding="utf-8"))
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


def _git_common_dir_from_files(repo: Path) -> Path | None:
    """``repo/.git`` 포인터만으로 공용 Git 디렉토리를 해소한다.

    linked worktree의 ``.git`` 파일은 실제 git-dir을, 그 아래 ``commondir``은 공용
    저장소를 가리킨다. snapshot 빌더의 subprocess 0 계약을 넓히지 않으면서 외부 absolute
    lease slot도 그 저장소를 소유한 PM 홈으로 역추적할 수 있게 하는 순수 파일 seam이다.
    """
    repo = Path(repo).resolve(strict=False)
    try:
        dot_git = repo / ".git"
        if dot_git.is_dir():
            git_dir = dot_git
        elif dot_git.is_file():
            pointer = _read_text_shared(dot_git, encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            target = Path(pointer[len("gitdir:"):].strip())
            git_dir = target if target.is_absolute() else repo / target
        else:
            return None

        common_pointer = git_dir / "commondir"
        if not common_pointer.is_file():
            return git_dir.resolve(strict=False)
        raw_common = _read_text_shared(common_pointer, encoding="utf-8").strip()
        if not raw_common:
            return None
        common = Path(raw_common)
        return (common if common.is_absolute() else git_dir / common).resolve(
            strict=False
        )
    except (OSError, UnicodeError, ValueError):
        return None


def _pm_home_candidates(repo: Path, cwd: Path) -> list[Path]:
    """엔진 루트와 공용 Git 저장소의 조상에서 PM 홈 후보를 가까운 순서로 낸다."""
    candidates: list[Path] = []
    seen: set[str] = set()
    # 엔진 사본의 repo가 신뢰 앵커다. 임의 cwd 조상을 별도 탐색하면 다른 프로젝트에서 호출된
    # subprocess가 그 프로젝트의 PM 원장을 주워 교차 귀속될 수 있다. cwd는 후보 원장의 slot
    # 역매칭에만 참여한다.
    del cwd
    repo = Path(repo).resolve(strict=False)
    common_dir = _git_common_dir_from_files(repo)
    seeds = [repo, *repo.parents]
    # absolute slot이 PM 홈의 조상/하위가 아니어도 linked-worktree 공용 저장소는 PM 홈 소유
    # 경로 안에 있다. common-dir 자신부터 조상을 훑어 .repos/<name>.git 형상도 함께 지원한다.
    if common_dir is not None:
        seeds.extend((common_dir, *common_dir.parents))
    for candidate in seeds:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def resolve_pm_home(repo: Path, cwd: Path) -> Path:
    """등록 worktree면 lease 역참조로 PM 홈을, 아니면 엔진 루트를 반환한다.

    git/board subprocess·lock에 진입하지 않는다. 엔진 루트 조상과 ``.git`` 파일이 가리키는
    common-dir 조상의 ``worktree-leases.json``만 point-read하고, cwd 또는 현재 엔진 루트가
    leased 슬롯 안에 있을 때 그 원장 소유 루트를 채택한다.
    """
    repo = Path(repo).resolve(strict=False)
    cwd = Path(cwd).resolve(strict=False)
    for candidate in _pm_home_candidates(repo, cwd):
        ledger = candidate / ".project_manager" / ".local" / "worktree-leases.json"
        data = _read_json_object(ledger)
        if data is None:
            continue
        rows = data.get("leases")
        if not isinstance(rows, list):
            continue
        # 원장이 현재 엔진 루트 자체 소유면 solo/multi PM 홈이다.
        if candidate.resolve(strict=False) == repo:
            return candidate
        for row in rows:
            if not isinstance(row, dict):
                continue
            slot = _lease_slot_path(candidate, row)
            if slot is not None and (_is_within(repo, slot) or _is_within(cwd, slot)):
                return candidate
    return repo


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


# 정체성 해소 층은 사람이 읽는 진단 문구이면서 pm_handoff의 실행/수집 축 분류 계약이다.
# 소비자가 문자열 리터럴을 복제하지 않도록 생산자가 집합까지 함께 노출한다.
IDENTITY_SOURCE_CWD_LEASE = "cwd→lease"
IDENTITY_SOURCE_SINGLE_ACTIVE_TASK = "단일 활성 task"
IDENTITY_SOURCE_SOLO_LOCAL_CONF = "solo local.conf"
IDENTITY_SOURCE_LEGACY_PM_STATE = "legacy pm_state"
IDENTITY_SOURCE_UNRESOLVED = "정체성 미해소"
SNAPSHOT_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
    IDENTITY_SOURCE_SOLO_LOCAL_CONF,
    IDENTITY_SOURCE_LEGACY_PM_STATE,
    IDENTITY_SOURCE_UNRESOLVED,
})
HANDOFF_TASK_MODE_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
})
HANDOFF_COLLECTION_ONLY_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_SOLO_LOCAL_CONF,
    IDENTITY_SOURCE_LEGACY_PM_STATE,
})


def _local_conf_session(pm_home: Path) -> str | None:
    """solo/legacy ``local.conf``의 ``session=``을 board 파서와 같은 의미로 읽는다."""
    conf = Path(pm_home) / ".project_manager" / "local.conf"
    session = None
    try:
        lines = _read_text_shared(conf, encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "session":
            session = value.strip() or None
    return session


def resolve_snapshot_identity(pm_home: Path, cwd: Path) -> tuple[str | None, str]:
    """cwd lease → 단일 활성 task → solo/legacy 순으로 snapshot 정체성을 해소한다."""
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
    # task 장부가 없는 신규 solo 채택자와 legacy solo만 마지막에 받는다. cwd와 무관한
    # leased 행이 하나라도 있으면 multi/worktree 오귀속 가능성이 있으므로 local.conf를 쓰지 않는다.
    if not active and not any(row.get("state") == "leased" for row in lease_rows):
        session = _local_conf_session(pm_home)
        if session:
            return session, IDENTITY_SOURCE_SOLO_LOCAL_CONF
        legacy_state = Path(pm_home) / ".project_manager" / "wiki" / "pm_state.md"
        if legacy_state.is_file():
            return "pm", IDENTITY_SOURCE_LEGACY_PM_STATE
    return None, IDENTITY_SOURCE_UNRESOLVED


def _checkpoint_identity_axes(
    pm_home: Path,
    cwd: Path,
    identity: str,
    source: str,
) -> tuple[str | None, str | None]:
    """snapshot 해소값을 checkpoint의 task/session 2축으로 분리한다.

    cwd lease의 session이 그 lease slot의 canonical basename과 같을 때만 slot으로
    인정한다. task가 우연히 ``foo_1`` 형상인 경우를 일반 정규식으로 slot으로
    오분류하지 않고, lease 귀속과 slot 형상을 한 번에 검증한다.
    """
    if source in HANDOFF_COLLECTION_ONLY_IDENTITY_SOURCES:
        return identity, None
    if source != IDENTITY_SOURCE_CWD_LEASE:
        return identity, None

    matches: list[tuple[int, bool]] = []
    for row in _lease_rows(pm_home):
        if row.get("state") != "leased" or row.get("session") != identity:
            continue
        slot = _lease_slot_path(pm_home, row)
        if slot is None or not _is_within(cwd, slot):
            continue
        is_canonical_slot = (
            slot.name == identity
            and re.fullmatch(r"[^()\s/\\]+_[1-9]\d*", identity) is not None
        )
        matches.append((len(slot.resolve(strict=False).parts), is_canonical_slot))
    if matches:
        deepest = max(depth for depth, _is_slot in matches)
        kinds = {is_slot for depth, is_slot in matches if depth == deepest}
        if kinds == {True}:
            return None, identity
    return identity, None


def resolved_lease_slot_path(pm_home: Path, session: str) -> Path | None:
    """활성 lease의 ``session``이 가리키는 실제 slot 경로를 반환한다.

    상대 경로는 ``pm_home`` 기준으로 해소하고 absolute slot은 그대로 보존한다. 같은 session의
    활성 행이 정확히 하나일 때만 권위값을 내어 중복 장부를 임의 선택하지 않는다.
    """
    matches = [
        slot
        for row in _lease_rows(pm_home)
        if row.get("state") == "leased" and row.get("session") == session
        for slot in [_lease_slot_path(pm_home, row)]
        if slot is not None
    ]
    unique = {str(slot.resolve(strict=False)): slot for slot in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _pm_state_path(pm_home: Path, task: str, source: str) -> Path:
    """해소 층에 맞는 task 또는 legacy solo pm_state 경로를 반환한다."""
    if source in HANDOFF_COLLECTION_ONLY_IDENTITY_SOURCES:
        return Path(pm_home) / ".project_manager" / "wiki" / "pm_state.md"
    return (
        Path(pm_home) / ".project_manager" / ".local" / "tasks" / task / "pm_state.md"
    )


def _identity_section(pm_home: Path, cwd: Path, task: str, source: str) -> str:
    state_path = _pm_state_path(pm_home, task, source)
    return (
        f"{_SNAPSHOT_IDENTITY_HEADING}\n"
        f"- task: {task}\n"
        f"- 해소: {source}\n"
        f"- PM 홈: {Path(pm_home).resolve(strict=False)}\n"
        f"- 현재 cwd: {Path(cwd).resolve(strict=False)}\n"
        f"- pm_state: {state_path}\n"
    )


def _ticket_counts(pm_home: Path) -> tuple[Path, dict[str, int]]:
    manager = Path(pm_home) / ".project_manager"
    # board 분리 형상 우선, legacy는 wiki/tickets. board.py 호출 없이 디렉토리 존재만 본다.
    board_root = manager / "board"
    tickets = (board_root if board_root.is_dir() else manager / "wiki") / "tickets"
    counts: dict[str, int] = {}
    for status in ("open", "claimed", "blocked", "done"):
        try:
            counts[status] = sum(1 for path in (tickets / status).glob("T-*.md") if path.is_file())
        except OSError:
            counts[status] = 0
    return tickets, counts


def _ledger_section(pm_home: Path, task: str) -> str:
    active = _active_tasks(pm_home)
    leases = _lease_rows(pm_home)
    states: dict[str, int] = {}
    task_slots: list[str] = []
    for row in leases:
        state = str(row.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        if state == "leased" and row.get("session") == task and row.get("slot"):
            task_slots.append(str(row["slot"]))
    tickets, counts = _ticket_counts(pm_home)
    state_text = ", ".join(f"{key} {states[key]}" for key in sorted(states)) or "장부 없음"
    count_text = " / ".join(f"{key} {counts[key]}" for key in ("open", "claimed", "blocked", "done"))
    return (
        "## 장부 포인터\n"
        f"- 활성 tasks ({len(active)}): {', '.join(active) if active else '(없음)'}\n"
        f"- worktree leases: {state_text}\n"
        f"- 현재 task 슬롯: {', '.join(sorted(task_slots)) if task_slots else '(없음)'}\n"
        f"- board tickets: {count_text}\n"
        f"- tickets 경로: {tickets}\n"
    )


def _pm_state_section(pm_home: Path, task: str, source: str, line_limit: int) -> str:
    state_path = _pm_state_path(pm_home, task, source)
    try:
        lines = _read_text_shared(state_path, encoding="utf-8").splitlines()[:line_limit]
    except (OSError, UnicodeError):
        lines = []
    body = "\n".join(lines).strip() or "(pm_state를 읽을 수 없음)"
    return f"## pm_state 머리 ({line_limit}줄 상한)\n{body}\n"


def _recovery_section(task: str, source: str) -> str:
    bootstrap = "python3 .project_manager/tools/pm_bootstrap.py"
    if source not in HANDOFF_COLLECTION_ONLY_IDENTITY_SOURCES:
        bootstrap += f" --task {task}"
    return (
        "## 복구 포인터\n"
        f"- `{bootstrap}`로 장부를 다시 펼친다.\n"
        "- 자동 생성된 compaction checkpoint 골격의 구간·서사 불릿은 PM 판단으로 채운다.\n"
        f"- {CTX_GUARD_CONTINUITY_GUIDANCE}\n"
    )


def build_ctx_guard_guidance(
    band: str,
    *,
    used_pct: int | None = None,
    remaining_pct: int | None = None,
    stop_pct: int | None = None,
) -> str:
    """세 하네스가 공유하는 ctx nudge/compaction 안내문을 렌더한다.

    동적 사용률은 하네스 관측값을 그대로 표시하되, 세션 연속성 정책은
    :data:`CTX_GUARD_CONTINUITY_GUIDANCE` 한 상수에서만 온다. 밴드는 표현 강도만
    다르고 작업 중단·세션 종료를 유도하지 않는다.
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
            "`python3 .project_manager/tools/pm_log.py checkpoint --task <이름> "
            "--trigger compaction`(Windows는 `py -3`)으로 기록한다. "
        )
    else:
        observed: list[str] = []
        if used_pct is not None:
            observed.append(f"컨텍스트 사용 {used_pct}%")
        if remaining_pct is not None:
            remaining = f"잔여 {remaining_pct}%"
            if band == "final" and stop_pct is not None:
                remaining += f" ≤ {stop_pct}%"
            observed.append(remaining)
        if len(observed) >= 2:
            status = f"{observed[0]} ({', '.join(observed[1:])}). "
        elif observed:
            status = f"{observed[0]}. "
        else:
            status = ""
        checkpoint = "`python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"
        if band == "final":
            checkpoint += " --trigger compaction"
        checkpoint += "`"
        command = (
            "checkpoint 기록 권고 구간이다. "
            f"{checkpoint}(Windows는 `py -3`)으로 현재 구간·서사를 기록한다. "
            "`<이름>`에는 현재 task 이름을 사용한다. "
        )
    return f"[{labels[band]}] {status}{command}{CTX_GUARD_CONTINUITY_GUIDANCE}"


def cmd_ctx_guidance(args: argparse.Namespace) -> int:
    """ctx 가드 공유 안내를 raw text 또는 Codex hook JSON envelope로 출력한다."""
    text = build_ctx_guard_guidance(
        args.band,
        used_pct=args.used_pct,
        remaining_pct=args.remaining_pct,
        stop_pct=args.stop_pct,
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
    """문자/UTF-8 경계를 깨지 않고 snapshot의 이중 상한 안으로 자른다."""
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


def cap_snapshot_sections(identity: str, sections: list[str]) -> str:
    """뒤 절부터 통째로 덜고, 초대형 정체성 절은 안전 절단해 이중 상한을 지킨다."""
    kept = list(sections)
    while kept:
        text = "\n".join([identity, *kept]).rstrip() + "\n"
        if _snapshot_within_limits(text):
            return text
        kept.pop()
    return _truncate_snapshot_text(identity.rstrip() + "\n")


def _latest_ctx_window_mismatch_section(
    pm_home: Path,
    task: str | None,
    session: str | None = None,
) -> str | None | object:
    """append-only log의 최신 PreCompact 밴드 평가에서 복구용 진단을 읽는다.

    진단 전용 marker나 consume 상태를 만들지 않는다. 같은 사이클 재호출은 같은 평가를
    다시 append할 수 있고, 최신 ``fired`` 평가는 앞선 경고를 자연스럽게 가린다.
    """
    current = Path(pm_home) / ".project_manager" / "wiki" / "log" / "current.md"
    try:
        _preamble, entries = split_entries(_read_text_exact(current))
    except FileNotFoundError:
        # 아직 log가 생기지 않은 fresh/solo 형상은 판독 실패가 아니라 활성 진단 없음이다.
        return None
    except (OSError, UnicodeError):
        # ``None``은 판독에 성공했고 활성 진단이 없다는 뜻이다. 판독 실패까지 None으로 합치면
        # PostCompact가 기존 진단 payload를 무진단 snapshot으로 덮어쓸 수 있다.
        return _CTX_WINDOW_MISMATCH_READ_FAILED
    for _date, entry in reversed(entries):
        first_line = entry.partition("\n")[0]
        if task is not None:
            owns_entry = (
                f"({_TASK_TAG_PREFIX}{task})" in first_line
                and " checkpoint | " in first_line
            )
        elif session is not None:
            owns_entry = (
                f"({session})" in first_line and " checkpoint | " in first_line
            )
        else:
            owns_entry = " checkpoint — " in first_line
        if not owns_entry:
            continue
        state = re.search(r"<!-- ctx-band-check: (fired|missed) -->", entry)
        if state is None:
            continue
        if state.group(1) == "fired":
            return None
        start = entry.find("> ⚠ [ctx-window-mismatch]")
        if start < 0:
            return None
        end = entry.find("\n\n", start)
        advisory = entry[start:] if end < 0 else entry[start:end]
        return "## ctx 설정 진단 (compaction 경계)\n" + advisory.rstrip() + "\n"
    return None


def build_snapshot(
    pm_home: Path,
    cwd: Path,
    *,
    line_limit: int = SNAPSHOT_PM_STATE_LINES,
    monotonic=time.monotonic,
    ctx_band_missed: bool = False,
    ctx_window_tokens: int | None = None,
    ctx_observed_tokens: int | None = None,
    harness: str | None = None,
) -> tuple[str | None, str | None]:
    """주입 최종 텍스트를 조립한다. 반환은 ``(text, warning)``이며 외부 호출·lock은 0이다."""
    started = monotonic()
    identity_name, source = resolve_snapshot_identity(pm_home, cwd)
    if identity_name is None:
        return None, "[pm-snapshot] 정체성 미해소 — cwd lease 불일치·활성 task 비단일; 재주입 생략"
    task, session = _checkpoint_identity_axes(pm_home, cwd, identity_name, source)
    identity = _identity_section(pm_home, cwd, identity_name, source)
    sections: list[str] = []
    mismatch = (
        "## ctx 설정 진단 (compaction 경계)\n"
        + build_ctx_window_mismatch_advisory(
            ctx_window_tokens=ctx_window_tokens,
            ctx_observed_tokens=ctx_observed_tokens,
            harness=harness,
        ).rstrip()
        + "\n"
        if ctx_band_missed else _latest_ctx_window_mismatch_section(pm_home, task, session)
    )
    if mismatch is _CTX_WINDOW_MISMATCH_READ_FAILED:
        # 무진단 snapshot을 만들지 않아 hook의 기존 armed payload가 그대로 남게 한다.
        return None, _CTX_WINDOW_MISMATCH_READ_WARNING
    # loud 진단은 compaction 직후 복구 계약의 핵심이므로 tail-drop 최후까지 남는 첫 절이다.
    if mismatch is not None:
        sections.append(mismatch)
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(identity, sections), None

    sections.append(_ledger_section(pm_home, identity_name))
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(identity, sections[:1] if mismatch is not None else []), None
    sections.append(_pm_state_section(pm_home, identity_name, source, line_limit))
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(identity, sections[:1] if mismatch is not None else []), None
    sections.append(_recovery_section(identity_name, source))
    return cap_snapshot_sections(identity, sections), None


def cmd_snapshot(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve(strict=False) if args.cwd else Path.cwd().resolve(strict=False)
    pm_home = resolve_pm_home(REPO, cwd)
    text, warning = build_snapshot(
        pm_home,
        cwd,
        line_limit=args.state_lines,
        ctx_band_missed=bool(getattr(args, "ctx_band_missed", False)),
        ctx_window_tokens=getattr(args, "ctx_window_tokens", None),
        ctx_observed_tokens=getattr(args, "ctx_observed_tokens", None),
        harness=getattr(args, "harness", None),
    )
    if warning:
        print(warning, file=sys.stderr)
    if args.json:
        payload = {"suppressOutput": True}
        if text:
            payload = {"systemMessage": text, "suppressOutput": False}
        _write_machine_line(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )
    elif text:
        sys.stdout.write(text)
    return 1 if warning == _CTX_WINDOW_MISMATCH_READ_WARNING else 0


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


def _pm_home_misanchor() -> Path | None:
    """lease로 못 찾은 등록 worktree를 board의 git detector로 재확인한다."""
    board_path = Path(__file__).resolve().parent / "board.py"
    try:
        board = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — detector 부재/로드 실패는 오탐 없이 fail-soft.
        if _is_engine_rev_skew(exc):
            raise
        return None
    detector = getattr(board, "_pm_home_worktree_misanchor", None)
    if detector is None:
        return None
    try:
        return detector(REPO)
    except Exception:  # noqa: BLE001 — git/board 판정 실패는 standalone 동작을 보존한다.
        return None


def _report_worktree_misanchor(command: str, pm_home: Path) -> int:
    """수동 경계 명령의 worktree 오실행을 PM 홈 안내와 함께 fail-loud 한다."""
    print(
        f"[중단] `pm_log {command}` 를 worktree(코드 전용) 트리에서 실행했습니다 — "
        "경계 상태는 PM 홈이 소유합니다. 이대로면 이 worktree에 stray log를 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {REPO})",
        file=sys.stderr,
    )
    return 1


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


def _implicit_boundary_state_path(
    task: str,
    session_id: str | None,
    boundary_id: str | None = None,
) -> Path:
    """구 어댑터의 scope prefix 또는 경계별 durable pending 파일 경로."""
    key = _compaction_scope_key(task, session_id)
    prefix = REPO / ".project_manager" / ".local/ctx-stop" / f"compact-boundary.{key}"
    if boundary_id is None:
        return prefix
    return prefix.with_name(f"{prefix.name}.{_safe_marker_key(boundary_id)}")


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
            boundary_id = _read_text_shared(path, encoding="utf-8").strip()
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
        if _read_text_shared(path, encoding="utf-8").strip() == boundary_id:
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
    session = None
    identity_name = task
    if not task:
        cwd = Path(getattr(args, "cwd", None) or Path.cwd()).resolve(strict=False)
        identity_name, source = resolve_snapshot_identity(REPO, cwd)
        if identity_name is None:
            if getattr(args, "trigger", None) == "compaction":
                print(
                    "[pm-checkpoint] checkpoint 정체성 미해소 — "
                    "cwd lease 불일치·활성 task 비단일; 기록 생략",
                    file=sys.stderr,
                )
                return 0
            print(
                "[중단] checkpoint 정체성 미해소 — cwd lease 불일치·활성 task 비단일; "
                "--task NAME을 명시하세요.",
                file=sys.stderr,
            )
            return 1
        task, session = _checkpoint_identity_axes(REPO, cwd, identity_name, source)
    if task is not None:
        identity_args = _load_identity_args()
        try:
            identity_args.validate_task_name(task, _registered_repos())
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
    ctx_band_missed = (
        args.trigger == "compaction" and bool(getattr(args, "ctx_band_missed", False))
    )
    ctx_band_checked = (
        args.trigger == "compaction"
        and getattr(args, "phase", None) == "pre"
        and (
            bool(getattr(args, "ctx_band_checked", False))
            or ctx_band_missed
        )
    )
    entry = build_checkpoint_entry(
        task,
        args.trigger,
        session=session,
        ctx_band_checked=ctx_band_checked,
        ctx_band_missed=ctx_band_missed,
        ctx_window_tokens=getattr(args, "ctx_window_tokens", None),
        ctx_observed_tokens=getattr(args, "ctx_observed_tokens", None),
        harness=getattr(args, "harness", None),
    )
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
        current_text = _read_text_shared(CURRENT_FILE, encoding="utf-8")
        boundary_id, phase, implicit_state = _resolve_compaction_boundary(
            args, task=identity_name, current_text=current_text,
        )
        reevaluated_precompact = phase == "pre" and ctx_band_checked
        if boundary_id is not None and phase is not None and not reevaluated_precompact:
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
        elif boundary_id is None or phase is None:
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
        if reevaluated_precompact and boundary_id is not None:
            # 진단은 먼저 durable append한다. 과거 호출이 claim 직후 죽여 둔 marker가 있어도
            # 재평가 결과를 억제하지 않으며, 이 후속 claim은 기존 checkpoint dedup만 보존한다.
            marker = _compaction_marker_path(
                identity_name, getattr(args, "session_id", None), boundary_id,
            )
            claimed = claim_compaction_checkpoint(marker, phase=phase)
            if claimed is None:
                print(
                    "[pm-checkpoint] dedup 장부 I/O 실패 — durable 진단 기록은 완료됐습니다.",
                    file=sys.stderr,
                )
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
    p.add_argument("--used-pct", type=int, metavar="N", help="관측 컨텍스트 사용률")
    p.add_argument("--remaining-pct", type=int, metavar="N", help="관측 잔여 컨텍스트 비율")
    p.add_argument("--stop-pct", type=int, metavar="N", help="final 밴드 stop 임계")
    p.add_argument(
        "--json", action="store_true",
        help="Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력",
    )
    p.set_defaults(fn=cmd_ctx_guidance)

    p = sub.add_parser("checkpoint", help="compaction/manual 경계 보충 박제 골격 append")
    p.add_argument("--task", metavar="이름", help="checkpoint 귀속 task 이름 (생략 시 cwd/task 장부 해소)")
    p.add_argument(
        "--trigger",
        choices=("compaction", "manual"),
        default="manual",
        help="박제 계기 (기본값: manual)",
    )
    p.add_argument("--cwd", metavar="PATH", help="훅이 발화한 cwd (정체성·PM 홈 앵커 해소)")
    p.add_argument("--session-id", help="하네스 세션 식별자 (compaction dedup 1순위 키)")
    p.add_argument("--boundary-id", help="하네스가 관측한 compaction 경계 식별자")
    p.add_argument("--phase", choices=("pre", "post"), help="compaction hook phase")
    p.add_argument(
        "--breadcrumb", action="store_true",
        help="Claude PreCompact breadcrumb를 checkpoint와 같은 PM 홈 append에 포함",
    )
    p.add_argument(
        "--ctx-band-checked", action="store_true",
        help="PreCompact에서 이번 사이클 밴드 발화 여부를 재평가했음을 durable 기록",
    )
    p.add_argument(
        "--ctx-band-missed", action="store_true",
        help="PreCompact의 사이클 밴드 미발화를 checkpoint/snapshot에 진단",
    )
    p.add_argument(
        "--ctx-window-tokens", type=_positive_int, metavar="N",
        help="밴드 미발화 당시 해소된 하네스 ctx 설정 창",
    )
    p.add_argument(
        "--ctx-observed-tokens", type=_positive_int, metavar="N",
        help="밴드 미발화 당시 transcript에서 관측한 사용 토큰",
    )
    p.add_argument(
        "--harness", metavar="NAME",
        help="진단 처방의 per-harness ctx_window_tokens_<name> 키",
    )
    p.set_defaults(fn=cmd_checkpoint)

    p = sub.add_parser("snapshot", help="compaction 뒤 재주입할 PM 정체성·장부 포인터 출력")
    p.add_argument("--cwd", metavar="PATH", help="훅이 발화한 cwd (기본: 프로세스 cwd)")
    p.add_argument(
        "--state-lines", type=_positive_int, default=SNAPSHOT_PM_STATE_LINES, metavar="N",
        help=f"pm_state 머리 줄 수 (기본: {SNAPSHOT_PM_STATE_LINES})",
    )
    p.add_argument(
        "--ctx-band-missed", action="store_true",
        help="checkpoint append 실패 시 진단을 snapshot payload로 직접 보존",
    )
    p.add_argument("--ctx-window-tokens", type=_positive_int, metavar="N")
    p.add_argument("--ctx-observed-tokens", type=_positive_int, metavar="N")
    p.add_argument("--harness", metavar="NAME")
    p.add_argument(
        "--json", action="store_true",
        help="Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력",
    )
    p.set_defaults(fn=cmd_snapshot)

    return parser


def _legacy_hook_argv(raw_argv: list[str], command: str) -> list[str]:
    """세대 skew 재시도용으로 신형 hook 인자만 제거한 argv를 만든다."""
    value_options = {
        "checkpoint": {
            "--ctx-window-tokens", "--ctx-observed-tokens", "--harness",
        },
        "snapshot": {
            "--ctx-window-tokens", "--ctx-observed-tokens", "--harness",
        },
    }.get(command, set())
    flag_options = {
        "checkpoint": {"--ctx-band-checked", "--ctx-band-missed"},
        "snapshot": {"--ctx-band-missed"},
    }.get(command, set())
    stripped: list[str] = []
    skip_value = False
    for token in raw_argv:
        if skip_value:
            skip_value = False
            continue
        if token in flag_options:
            continue
        if token in value_options:
            skip_value = True
            continue
        if any(token.startswith(option + "=") for option in value_options):
            continue
        stripped.append(token)
    return stripped


def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    # snapshot/checkpoint가 등록 worktree에서 발화하면 PM 홈의 동기화된 엔진으로 치환한다.
    # tail/archive 등 기존 명령은 자기 앵커 의미를 보존한다.
    if args.cmd in {"snapshot", "checkpoint"}:
        cwd = Path(getattr(args, "cwd", None) or Path.cwd()).resolve(strict=False)
        pm_home = resolve_pm_home(REPO, cwd)
        if pm_home.resolve(strict=False) != REPO.resolve(strict=False):
            hook_fail_soft = (
                args.cmd == "snapshot"
                or getattr(args, "trigger", None) == "compaction"
            )
            canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
            if not canonical.is_file():
                print(
                    f"[pm-{args.cmd}] PM 홈 엔진 부재 — 경계 처리를 생략: {canonical}",
                    file=sys.stderr,
                )
                return 0 if hook_fail_soft else 2
            forwarded = list(raw_argv)
            if "--cwd" not in forwarded:
                forwarded.extend(("--cwd", str(cwd)))
            timeout = SNAPSHOT_TIMEOUT_SECONDS if args.cmd == "snapshot" else 5.0
            try:
                result = subprocess.run(
                    [sys.executable, str(canonical), *forwarded],
                    cwd=str(pm_home),
                    timeout=timeout,
                    check=False,
                )
                diagnostic_requested = (
                    args.cmd == "checkpoint"
                    and bool(getattr(args, "ctx_band_missed", False))
                )
                diagnostic_append_failed = diagnostic_requested and result.returncode != 0
                if (
                    args.cmd == "snapshot"
                    and bool(getattr(args, "ctx_band_missed", False))
                    and result.returncode != 0
                ):
                    # 구형 PM-home 엔진이 forced-diagnostic 옵션을 모르면 옵션을 버린 snapshot으로
                    # 강등하지 않는다. 신형 worktree builder가 해소한 PM-home을 직접 읽어 payload를
                    # 렌더한다. checkpoint의 legacy append 재시도와 역할이 다르다.
                    return args.fn(args)
                legacy_forwarded = _legacy_hook_argv(forwarded, args.cmd)
                if result.returncode != 0 and legacy_forwarded != forwarded:
                    result = subprocess.run(
                        [sys.executable, str(canonical), *legacy_forwarded],
                        cwd=str(pm_home),
                        timeout=timeout,
                        check=False,
                    )
                if diagnostic_append_failed:
                    # hook_fail_soft rc0은 세션 지속 계약이다. 진단 append 성공까지 뜻하지 않으므로
                    # 호출 훅이 pending snapshot fallback을 무장할 수 있는 별도 신호를 보낸다.
                    print(_CTX_DIAGNOSTIC_APPEND_FAILED_SIGNAL, file=sys.stderr)
                # snapshot/compaction은 훅 계약상 fail-soft다. 사람이 실행한 manual checkpoint는
                # 이름 검증·로그 부재·엔진 오류의 하위 rc를 그대로 돌려줘 성공으로 오보고하지 않는다.
                return 0 if hook_fail_soft else result.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                if hook_fail_soft:
                    if (
                        args.cmd == "checkpoint"
                        and bool(getattr(args, "ctx_band_missed", False))
                    ):
                        print(_CTX_DIAGNOSTIC_APPEND_FAILED_SIGNAL, file=sys.stderr)
                    return 0
                print(f"[pm-{args.cmd}] PM 홈 엔진 실행 실패: {exc}", file=sys.stderr)
                return 1
        # lease 원장은 등록 worktree의 부분집합이다. 미등재 worktree는 board의 git detector로
        # 재확인해 로컬 stray log를 막는다. hook 경계는 fail-soft, 수동 checkpoint는 fail-loud.
        fallback_home = _pm_home_misanchor()
        if fallback_home is not None:
            if args.cmd == "snapshot" or getattr(args, "trigger", None) == "compaction":
                return 0
            return _report_worktree_misanchor(args.cmd, fallback_home)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
