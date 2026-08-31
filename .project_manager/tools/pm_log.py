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
import shutil
import subprocess
import sys
import tempfile
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


# ── 엔진 사본 rev 스탬프 (pm_bootstrap deep-import target) ────────────────
ENGINE_REV = "v1.7.12"


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
    "inflight_ledger_query": (
        "compaction snapshot 은 hook 경계다 — 진행 중 작업 절의 장부 조회(pm_delegate·pm_relay "
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
    """task/slot 축의 compaction/manual 경계 보충 박제 골격을 만든다."""
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
        # Consumer grammar: pm_handoff.collect_session_entries()의 task/slot
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
    config_key = (
        f"harness.{normalized_harness}.ctx_window_tokens"
        if normalized_harness is not None else "ctx.window_tokens"
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


class PmHomeResolutionError(RuntimeError):
    """anchor 자신의 선언에서 소유 PM 홈을 확정하지 못한 오류."""


# 두-git 형상의 공유 bare 저장소는 PM 홈 안 `.repos/<repo>.git` 에 놓인다 — 슬롯을 만든
# 도구(`worktree_pool.bare_repo_path` = `pm_config.REPOS_DIR / f"{repo}.git"`)가 써 넣은
# 자리라 조상 추측이 아니라 선언이다.
_GIT_DIR_NAME = ".git"
_BARE_REPOS_DIR_NAME = ".repos"


def _pm_home_from_common_dir(common_dir: Path) -> Path | None:
    """공용 Git 저장소 경로에서 그 저장소를 만든 PM 홈을 되돌린다(선언 형상 2종)."""
    if common_dir.name == _GIT_DIR_NAME:
        # `<X>/.git` — 단일-git PM 홈이 자기 checkout 에서 판 worktree.
        return common_dir.parent
    if (
        common_dir.suffix == _GIT_DIR_NAME
        and common_dir.parent.name == _BARE_REPOS_DIR_NAME
    ):
        # `<X>/.repos/<repo>.git` — worktree 풀이 만든 두-git 슬롯과 그 슬롯에서 판 스냅샷.
        return common_dir.parent.parent
    return None


def _require_lease_ledger(pm_home: Path, anchor: Path) -> None:
    """PM 홈 lease 장부를 strict point-read 한다 — 부재·손상·빈 장부는 전부 실패."""
    ledger = Path(pm_home) / ".project_manager" / ".local" / "worktree-leases.json"
    if not ledger.is_file():
        raise PmHomeResolutionError(
            f"{anchor}: 소유 PM 홈 후보 {pm_home} 의 worktree lease 장부 없음 ({ledger})"
        )
    try:
        data = json.loads(_read_text_shared(ledger, encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise PmHomeResolutionError(
            f"{anchor}: 소유 PM 홈 후보 {pm_home} 의 worktree lease 장부를 읽을 수 "
            f"없습니다 ({ledger}: {type(exc).__name__}: {exc})"
        ) from exc
    rows = data.get("leases") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PmHomeResolutionError(
            f"{anchor}: 소유 PM 홈 후보 {pm_home} 의 worktree lease 장부 형식이 "
            f"올바르지 않습니다 ({ledger})"
        )
    if not rows:
        raise PmHomeResolutionError(
            f"{anchor}: 소유 PM 홈 후보 {pm_home} 의 worktree lease 장부에 등록 행이 "
            f"없습니다 ({ledger})"
        )


def owning_pm_home(anchor: Path) -> Path:
    """anchor 자신이 들고 있는 선언만으로 소유 PM 홈을 유도한다 — 답은 하나이거나 예외다.

    입력은 anchor 의 `.git` 포인터(슬롯을 만든 도구가 쓴 값)와 그 포인터가 지목한 PM 홈의
    lease 장부뿐이다. 조상 훑기·cwd·환경·subprocess 를 쓰지 않으므로 같은 모양의 트리는
    파일시스템 어디에 있어도 같은 답을 낸다.

      - `.git` 없음/디렉터리        → anchor 자신 (아무의 linked worktree 도 아니다)
      - `.git` 파일 → `<X>/.repos/<repo>.git` 또는 `<X>/.git` → `X`
      - 그 밖의 commondir           → 실패

    유도된 `X` 는 `<X>/.project_manager` 실재와 lease 장부 strict point-read 를 요구한다.
    부재·손상·빈 장부는 전부 실패다 — 못 받으면 anchor 자신으로 강등하지 않는다.
    """
    anchor = Path(anchor).resolve(strict=False)
    if not (anchor / _GIT_DIR_NAME).is_file():
        # 없음(합성 트리·비-git 채택 폴더) · 디렉터리(PM 홈 main checkout·일반 clone).
        return anchor
    common_dir = _git_common_dir_from_files(anchor)
    if common_dir is None:
        raise PmHomeResolutionError(
            f"{anchor}: `.git` 포인터에서 공용 Git 저장소를 읽지 못했습니다."
        )
    pm_home = _pm_home_from_common_dir(common_dir)
    if pm_home is None:
        raise PmHomeResolutionError(
            f"{anchor}: 공용 Git 저장소 {common_dir} 형상에서 소유 PM 홈을 찾지 못했습니다."
        )
    pm_home = pm_home.resolve(strict=False)
    if not (pm_home / ".project_manager").is_dir():
        raise PmHomeResolutionError(
            f"{anchor}: 공용 Git 저장소 {common_dir} 의 소유 PM 홈을 찾지 못했습니다 — "
            f"{pm_home} 에 .project_manager 가 없습니다."
        )
    _require_lease_ledger(pm_home, anchor)
    return pm_home


def _lease_rows(pm_home: Path) -> list[dict]:
    data = _read_json_object(
        Path(pm_home) / ".project_manager" / ".local" / "worktree-leases.json"
    )
    rows = data.get("leases") if data else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _ledger_task_names(pm_home: Path) -> set[str]:
    """장부 top-level ``tasks`` 컬렉션의 이름 집합 — **task 축의 단일 진실**(등록 membership).

    "이 정체성이 task 인가"를 이름 모양(``foo_1`` 형상 추측)이나 slot 경로 대조가 아니라 등록
    여부로 묻는다(``worktree_pool.reclaim_stale``·``_ensure_slot_pm_state_locked`` 가 이미 쓰는
    근거와 같은 축). 부재/손상 → 빈 집합(fail-soft).
    """
    data = _read_json_object(
        Path(pm_home) / ".project_manager" / ".local" / "worktree-leases.json"
    )
    rows = data.get("tasks") if data else None
    if not isinstance(rows, list):
        return set()
    return {row["name"] for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str) and row.get("name")}


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
# 슬롯 정체성 키 형식(`<repo>_<N>`) — `identity_args._SLOT_KEY_RE` 와 같은 값 술어다(이 모듈은
# leaf point-reader 라 형제를 로드하지 않으므로 패턴만 동형 보유·경로 형태 강제 아님).
_SLOT_KEY_PATTERN = r"[^()\s/\\]+_[1-9]\d*"

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
    """cwd lease → 단일 활성 task 순으로 snapshot 정체성을 해소한다.

    두 층이 전부다. 슬롯을 하나만 쓰는 홈도 그 홈 자신이 lease 행이라 cwd 가 그 행 안에 있고,
    첫 층이 답을 준다 — "장부가 아무 말도 없으면 이 홈은 단일 정체성"이라는 층은 없다(그
    추론은 실제로 슬롯을 여럿 쓰는 홈에서 오귀속이었다).

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


def _checkpoint_identity_axes(
    pm_home: Path,
    cwd: Path,
    identity: str,
    source: str,
) -> tuple[str | None, str | None]:
    """snapshot 해소값을 checkpoint의 task/session 2축으로 분리한다.

    축 판정의 단일 진실은 **장부**다: cwd 를 소유한 lease 행의 정체성이 (1) 등록 task 이름이
    아니고 (2) 슬롯 키 형식(``<repo>_<N>``)이면 slot 축이다. 예전 규칙(``slot.name == identity``)
    은 정체성을 **슬롯 경로 이름**과 대조해, 경로에 이름이 없는 슬롯(PM 홈 자신을 가리키는 행)과
    경로·session 이 어긋난 행을 전부 task 축으로 오분류했다(task 이름 검증까지 흘러가 bare
    handoff 가 argparse 에러로 죽었다). task 오분류 방지는 경로 대조가 아니라 **등록 membership**
    (``_ledger_task_names``)이 맡는다 — ``foo_1`` 형상 task 는 슬롯 예약 패턴이라 애초에 등록될
    수 없고, 등록됐다면 그 값이 진실이다.
    """
    if source != IDENTITY_SOURCE_CWD_LEASE:
        return identity, None

    task_names = _ledger_task_names(pm_home)
    matches: list[tuple[int, bool]] = []
    for row in _lease_rows(pm_home):
        if row.get("state") != "leased" or row.get("session") != identity:
            continue
        slot = _lease_slot_path(pm_home, row)
        if slot is None or not _is_within(cwd, slot):
            continue
        is_slot_axis = (
            identity not in task_names
            and re.fullmatch(_SLOT_KEY_PATTERN, identity) is not None
        )
        matches.append((len(slot.resolve(strict=False).parts), is_slot_axis))
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
    """해소된 task 의 pm_state 경로를 반환한다."""
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


def _status_dirs() -> tuple[str, ...]:
    """티켓 상태 디렉토리 집합 — board 의 `STATUS_DIRS` 단일 진실을 지연 로드로 승계한다(fail-soft).

    census 버킷을 손으로 적으면 board 가 상태를 추가할 때(`discarded` — 처분 종결) 그 상태의
    티켓이 장부 집계에서 **조용히** 빠진다(crash 0 이라 아무도 못 본다). 로드 실패는
    `_registered_repos` 와 같은 fail-soft — 빈 튜플이면 소비측이 "미해소"로 명시 표기한다
    (손으로 적은 목록으로 되돌아가지 않는다). 사본 skew 만 fail-loud.
    """
    board_path = Path(__file__).resolve().parent / "board.py"
    try:
        board = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 부재/로드 실패는 census 표기만 완화.
        if _is_engine_rev_skew(exc):
            raise
        return ()
    return tuple(getattr(board, "STATUS_DIRS", ()))


def _ticket_counts(pm_home: Path) -> tuple[Path, dict[str, int]]:
    """`pm_home` board 의 상태별 티켓 수 — 버킷은 `_status_dirs()`(board 단일 진실) 파생이다.

    상태 디렉토리 스캔 자체는 board.py 호출 없이 디렉토리 존재만 본다(집합만 board 에서 온다).
    """
    manager = Path(pm_home) / ".project_manager"
    # board 분리 형상 우선, legacy는 wiki/tickets.
    board_root = manager / "board"
    tickets = (board_root if board_root.is_dir() else manager / "wiki") / "tickets"
    counts: dict[str, int] = {}
    for status in _status_dirs():
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
    # 버킷 순서·집합은 `_status_dirs()`(board `STATUS_DIRS`) 그대로다 — 새 상태는 손대지 않고
    # 뒤에 붙고, 기존 status 표기는 바뀌지 않는다.
    count_text = " / ".join(f"{key} {value}" for key, value in counts.items()) or "(상태 목록 미해소)"
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


def _load_pm_delegate():
    """pm_delegate 모듈을 in-process 로드 — 미회수 라운드 준비 조회(``ticket_copy_records``) 전용.

    부재/로드 실패/사본 skew 는 모두 예외로 전파한다. 호출부(``_inflight_section``)가 절 단위
    fail-soft 로 접고, skew 만 등록 경계(``inflight_ledger_query``)에서 흡수해 표출한다
    (``pm_handoff._load_pm_delegate`` 동형 seam — CLI subprocess 왕복 대신 같은 프로세스 호출).
    """
    path = Path(__file__).resolve().parent / "pm_delegate.py"
    return _load_module_from_path(path, "pm_delegate.py", verifier=_verify_engine_rev)


def _load_pm_relay():
    """pm_relay 모듈을 in-process 로드 — 미마감 raw 무락 조회(``raw_records(lock=False)``) 전용."""
    path = Path(__file__).resolve().parent / "pm_relay.py"
    return _load_module_from_path(path, "pm_relay.py", verifier=_verify_engine_rev)


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
    """``pm_delegate.ticket_copy_records(unharvested=True)`` 실값 → 미회수 라운드 준비 불릿."""
    prefix = "- 미회수 라운드 준비 "
    suffix = " → `python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested`"
    if not rows:
        return _inflight_capped_line(prefix=prefix, content="0건", suffix=suffix)
    labels = [f"{row['ticket']} {row['role']}#{row['ordinal']}" for row in rows]
    content = f"{len(rows)}건: {_fold_join(labels, _INFLIGHT_UNHARVESTED_SHOWN, '건')}"
    return _inflight_capped_line(prefix=prefix, content=content, suffix=suffix)


def _unfinished_raw_line(rows: list[dict], ledger_path: Path) -> str:
    """``pm_relay.unfinished_raw_records(lock=False)`` 실값 → 미마감 위임 raw 불릿(kill 증거)."""
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


def _inflight_query_failure_line(exc: Exception, *, skew: bool) -> str:
    """진행 중 작업 절의 skew 흡수 경계 표출 — 흡수해도 stderr 1줄 + payload 1줄로 표출한다.

    복구 마커 호출(사유 등록 검증)은 흡수 지점인 호출부 except 핸들러가 직접 수행하고
    (`skew` 로 그 판정을 받는다 — 정적 가드의 시야가 핸들러 본문이다), 여기서는 표출만 한다.
    stderr 진단은 원문 그대로(다중행이어도 무방) 남기되, payload 에 실리는 절 본문은
    ``_inflight_safe_text``로 정규화하고 ``_inflight_capped_line``으로 줄 상한을 지킨다
    (예외 메시지에 개행이 섞이면 가짜 heading 을 주입할 수 있다).
    """
    cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
    print(f"경고: 진행 중 작업 절 조회 실패 ({cause}) — 절을 1줄로 접음", file=sys.stderr)
    line = _inflight_capped_line(
        prefix="- 조회 실패: ",
        content=_inflight_safe_text(cause),
        suffix=" → `python3 .project_manager/tools/pm_bootstrap.py`로 직접 확인",
    )
    return "## 진행 중 작업 (장부 실측)\n" + line + "\n"


def _inflight_section(
    pm_home: Path, task: str, *, deadline: float, monotonic=time.monotonic,
) -> str:
    """미회수 라운드 준비·미마감 raw·claimed 티켓·슬롯 WIP 을 in-process 조회로 편입한다.

    장부 조회는 subprocess 0(``pm_delegate.ticket_copy_records``·``pm_relay.raw_records``
    in-process 호출) — WIP git 프로브(``_wip_slot_line``)만 유일한 예외(자체 상한·전 예외 흡수).
    네 조회 중 하나라도 예외를 올리면 절 전체를 1줄로 접는다(다른 절은 온전 — 절 단위 fail-soft).
    각 불릿은 ``_inflight_safe_text``/``_inflight_capped_line`` 으로 이미 단일 줄이지만, 물리
    행 수도 조립 단계에서 실제로 강제한다(``_INFLIGHT_MAX_LINES`` — 선언만
    되고 조립에서 미강제였던 결함).
    """
    try:
        delegate = _load_pm_delegate()
        unharvested_rows = delegate.ticket_copy_records(pm_home, unharvested=True)
        relay = _load_pm_relay()
        _raw_dir, raw_ledger = relay.raw_storage_paths(
            pm_home, "delegate", None, temp_dir=Path(tempfile.gettempdir()),
        )
        unfinished_raw_rows = relay.unfinished_raw_records(raw_ledger, lock=False)
        tickets_dir, _counts = _ticket_counts(pm_home)
        claimed_ids = [
            path.stem for path in (tickets_dir / "claimed").glob("T-*.md")
            if path.is_file()
        ]
    except Exception as exc:  # noqa: BLE001 — 절 단위 fail-soft(등록 경계에서만 skew 흡수).
        skew = _absorb_engine_rev_skew_for_recovery(exc, "inflight_ledger_query")
        return _inflight_query_failure_line(exc, skew=skew)

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


def _truncate_snapshot_text_keep_tail(text: str) -> str:
    """``_truncate_snapshot_text`` 의 꼬리-보존 변형 — 문자/UTF-8 경계를 지키며 뒤쪽을 남긴다.

    always-keep 접두 자체가 이중 상한을 넘는 극단 형상에서, 나중에
    추가된 절(전언 경고)이 먼저 온 절(ctx 진단)보다 우선 살아남게 한다.
    """
    text = text[-SNAPSHOT_MAX_CHARS:] if len(text) > SNAPSHOT_MAX_CHARS else text
    if len(text.encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[-middle:] if middle else ""
        if len(candidate.encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[-low:] if low else ""


def _truncate_identity_keeping_suffix(identity: str, suffix: str) -> str:
    """identity 를 이중 상한 안으로 절단하되 always-keep 접두(``suffix``)는 뒤에 온전히 붙인다.

    접두 자체가 상한을 넘는 비정상 입력이면 identity 를 버리고 접두를 꼬리-보존으로 안전
    절단한다(나중에 추가된 절 — 전언 경고 — 이 먼저 온 절보다 우선 산다). 정상 운용에서 접두는
    몇백 자 수준이라 이 분기는 사실상 도달하지 않는다.
    """
    if not suffix:
        return _truncate_snapshot_text(identity.rstrip() + "\n")
    trimmed = identity.rstrip()
    combined = f"{trimmed}\n{suffix}" if trimmed else suffix
    if _snapshot_within_limits(combined):
        return combined
    if not _snapshot_within_limits(suffix):
        return _truncate_snapshot_text_keep_tail(suffix)
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

    ``required`` 는 ``sections`` 앞 N 개(예: ctx 진단+전언 경고)를 tail-drop 대상에서 제외한다
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
        # 아직 log가 생기지 않은 fresh 홈은 판독 실패가 아니라 활성 진단 없음이다.
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
    """주입 최종 텍스트를 조립한다. 반환은 ``(text, warning)``.

    장부 조회는 subprocess·lock 0 (진행 중 작업 절의 WIP git 프로브만 상한 3회의 유일한 예외 —
    `_wip_slot_line` 참고). 절 순서: [ctx 진단] → 전언 경고(always-keep) → 장부 포인터 →
    진행 중 작업 → pm_state → 복구 포인터.
    """
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

    sections.append(_pm_state_section(pm_home, identity_name, source, line_limit))
    if monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
        return cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None
    sections.append(_recovery_section(identity_name, source))
    return cap_snapshot_sections(identity, sections, required=always_keep), None


def cmd_snapshot(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve(strict=False) if args.cwd else Path.cwd().resolve(strict=False)
    pm_home = owning_pm_home(REPO)
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
                "(`<repo>_<N>`)은 쓸 수 없다.",
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
        help="진단 처방의 per-harness harness.<name>.ctx_window_tokens 키",
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
        pm_home = owning_pm_home(REPO)
        if pm_home != REPO.resolve(strict=False):
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
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
