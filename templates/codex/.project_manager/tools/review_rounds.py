#!/usr/bin/env python3
"""리뷰 라운드 장부 공용 seam.

추가 리뷰어(`additional_reviewer.py`)와 내부 code-reviewer(`pm_delegate.py`)는 저장 파일은 서로
분리하지만, 장부의 gate entry/예약/산출/수렴 판정 스키마는 같이 쓴다. 이 모듈은 두 축에서
갈리면 안 되는 다음 규칙만 소유한다.

* JSON 장부 read + PID/UUID 임시 파일을 이용한 원자 write (교체 자체는 공용 `file_lock` seam —
  플랫폼별 rename 의미 차이를 이 모듈이 복제하지 않는다)
* gate entry 정규화와 단조 sequence 예약/스폰 전 환불
* 예약 순서 기준 must-fix 추이, 진행 중 예약을 포함한 상한, 발산 조기 차단

파일락의 경로와 임계 구역은 호출자가 소유한다. 두 도구의 저장 파일과 부가 기록(external의
livegate/wave, internal의 raw attempt 결속)가 다르기 때문이다. 호출자는 반드시 같은 장부 옆의
파일락 안에서 read-modify-write를 수행한다.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
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


# baked 엔진 rev — engine_rev.py --bump가 기계 일괄 재작성한다.
ENGINE_REV = "v1.7.12"

CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"

# 폐지된 라운드 연장 승인이 장부에 남긴 필드. 스키마에서 뺐고 **값을 승계하지도 않는다** —
# 승인 수위만큼 집계 창을 잘라 판정 수를 줄이던 값이라, 남아 있으면 그 게이트의 집계가 전체
# 레코드 기준과 달라진다. 버리는 사실은 게이트·값과 함께 loud 로 알린다(정규화가 키를 떨구므로
# 장부가 다시 기록되면 알릴 대상이 없어 자연히 1회다 — 별도 상태 파일 없음).
RETIRED_ACK_FIELD = "acked_through"

# 기계 확인(pm-review-confirmation-v1) 증거로 여는 내부 게이트 처분. 상한·쿼터 대신
# 증거를 요구한다(`pm_delegate.pm_verified_evidence_problem` 이
# 선언·완료 재검증 공용으로 판정한다). 이 모듈은 표기 문구만 소유한다.
RESOLUTION_PM_VERIFIED = "pm-verified"



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


def _load_file_lock():
    """원자 교체 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다 (지연·프로세스 1회).

    **쓰기 경로 전용 지연 로드**다 — 읽기(`read_ledger`)와 판정은 형제 없이도 떠야 한다(부분
    동기 트리에서도 라운드 판정은 살아 있어야 하고, 이 모듈을 소비하는 도구들이 그 판정으로
    복구를 안내한다). 부재도 rev 불일치도 조용히 흡수하지 않고 marked skew 로 올린다 — 다른
    엔진 형제 로더와 같은 규칙이다.
    """
    lock_path = Path(__file__).resolve().with_name("file_lock.py")
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


# ── 공유 읽기 (등재 예외 · 형제 없이도 떠야 하는 판독) ──────────────────────
# 원자 교체 대상을 읽는 지점은 공용 seam 을 지난다([[T-0729]]) — 일반 `open` 리더가 하나라도
# 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막는다. 다만 이 모듈의 판독은 위 로더 주석의
# 계약대로 **형제 없이도 떠야 한다**(부분 동기 트리에서 라운드 판정이 살아 있어야 그 판정을 쓰는
# 도구들이 복구를 안내한다). 그래서 여기는 **등재된 예외**다 — seam 이 있으면 쓰고, 없거나 로드가
# 실패하면 사유를 남기고 종전 읽기로 진행한다. 잃는 것은 "Windows 에서 이 판독이 열려 있는 동안의
# 교체 한 번" 이고, 얻는 것은 "깨진 트리에서도 라운드 판정이 산다" 다.

# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "shared_read_seam": (
        "판독은 형제 없이도 떠야 한다 — 부분 동기 트리에서 라운드 판정이 죽으면 그 판정으로 "
        "복구를 안내하는 도구들이 함께 죽는다. 부재/손상/혼합 사본을 흡수하되 조용하지 않게 "
        "사유를 stderr 로 남기고 종전 읽기로 진행한다(잃는 것은 Windows 에서 이 판독 중의 "
        "원자 교체 한 번)"
    ),
}


def _is_engine_rev_skew(exc) -> bool:
    """fail-soft 소비 지점에서 rev skew 만 구분하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


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


def _open_shared(path, *, binary, encoding=None, errors=None, newline=None):
    """`file_lock.open_shared` — seam 을 못 쓰면 같은 의미의 종전 열기로 강등한다."""
    api = _shared_read_api("open_shared")
    if api is not None:
        return api(path, binary=binary, encoding=encoding, errors=errors, newline=newline)
    if binary:
        return open(path, "rb")
    return open(path, "r", encoding=encoding, errors=errors, newline=newline)


def utc_now_iso() -> str:
    """UTC ISO-8601 시각 문자열."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def as_int(value: object) -> int:
    """장부 정수 필드 정규화(손상/누락은 0)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def read_ledger(
    path: Path | str,
    *,
    warning_sink: Callable[[str], None] | None = None,
) -> dict:
    """장부를 읽는다. 부재·손상·비매핑 최상위는 빈 장부로 fail-soft 한다.

    파일 부재는 첫 실행의 정상 형상이라 조용하다. 반면 읽기 권한/IO 실패·깨진 UTF-8·빈 파일을
    포함한 JSON 파싱 실패는 호출자가 ``warning_sink``를 주면 loud 진단을 남긴다. 어느 형상이든
    손상된 바이트가 라운드 호출을 예외로 막지 않도록 빈 장부로 복구한다.
    """
    target = Path(path)
    try:
        data = json.loads(_read_text_shared(target, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if warning_sink is not None:
            warning_sink(
                f"{target}: {type(exc).__name__}: {exc}"
            )
        return {}
    return data if isinstance(data, dict) else {}


def write_ledger(path: Path | str, ledger: dict) -> None:
    """장부 JSON을 같은 디렉터리의 고유 임시 파일을 거쳐 원자 교체한다.

    read-modify-write 직렬화는 호출자의 파일락이 보장한다. 여기서는 독자가 부분 JSON을 보지
    않는 원자 교체와 실패한 임시 파일 정리만 소유한다.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n"
        )
        _load_file_lock().atomic_replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def warn_retired_ack_field(gate: str, value: int) -> None:
    """폐지 필드를 버린다는 사실을 그 게이트·값과 함께 알린다(승계 없음)."""
    print(
        f"경고: 라운드 장부의 폐지 필드 `{RETIRED_ACK_FIELD}` 를 버립니다 — 게이트 {gate} · "
        f"{RETIRED_ACK_FIELD}={value}. 폐지된 라운드 연장 승인이 남긴 값이라 승계하지 "
        "않으므로, 이 게이트의 집계는 이제 전체 레코드 기준입니다. 장부가 다시 기록되면 이 "
        "안내는 사라집니다.",
        file=sys.stderr,
    )


def normalize_gate_entry(
    ledger: dict,
    gate: str,
    *,
    reserved_keys: Sequence[str] = (),
) -> tuple[dict, int]:
    """gate entry를 external/internal 공용 스키마로 정규화해 장부에 심고, 폐기 필드
    (`RETIRED_ACK_FIELD`) 감지 여부를 호출부에 돌려준다 — `(entry, retired_ack)`.

    폐지 필드는 여기서 떨어진다(승계 없음). `retired_ack` 는 버린 원값(0=미검출)이다 —
    **알림·영속은 이 함수가 하지 않는다**: 순수 조회(report/preview)는 정규화 결과를 저장할
    수 없어 '1회' 고지를 지킬 수 없고, mutation/차단 경로는 저장 시점을 스스로 정한다(거부
    직전에라도 저장해야 알림이 실제로 1회가 된다). 호출부가 `retired_ack` 를 보고 자기
    계약에 맞는 저장·고지 정책을 고른다(`warn_retired_ack_field` 재사용). 접는 자리를
    정규화로 둬야 조회·기록·legacy 승계가 한 규칙이다(승계 항목은 장부 스캔이 아니라 이
    경로로 들어온다)."""
    if gate in reserved_keys:
        raise ValueError(
            f"라운드 장부 예약 키를 게이트로 쓸 수 없습니다: {gate!r} "
            f"(예약 키: {', '.join(sorted(reserved_keys))})"
        )
    entry = ledger.get(gate)
    if not isinstance(entry, dict):
        entry = {}
    records = entry.get("records")
    records = [row for row in records if isinstance(row, dict)] \
        if isinstance(records, list) else []
    rounds = entry.get("rounds")
    rounds = [row for row in rounds if isinstance(row, dict)] \
        if isinstance(rounds, list) else []
    resolution = entry.get("resolution")
    if not isinstance(resolution, dict):
        resolution = None
    retired_ack = max(0, as_int(entry.get(RETIRED_ACK_FIELD)))
    normalized = {
        "count": as_int(entry.get("count")),
        "sequence": max(
            as_int(entry.get("sequence")),
            *(as_int(row.get("sequence", row.get("number"))) for row in records),
            0,
        ),
        "resolution": resolution,
        "records": records,
        "rounds": rounds,
    }
    ledger[gate] = normalized
    return normalized, retired_ack


def reservation_deadline(wall_timeout_sec: int | None) -> str | None:
    """예약이 실행 중일 수 있는 마지막 시각(백스톱 부재 시 None)."""
    if not wall_timeout_sec or wall_timeout_sec <= 0:
        return None
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=wall_timeout_sec)
    ).isoformat()


def reserve_round(
    entry: dict,
    record_id: str,
    *,
    wall_timeout_sec: int | None = None,
    target_rev: str | None = None,
) -> dict:
    """단조 sequence로 한 라운드를 예약하고 예약 레코드를 반환한다."""
    entry["sequence"] = max(0, as_int(entry.get("sequence"))) + 1
    entry["count"] = max(0, as_int(entry.get("count"))) + 1
    record = {
        "id": record_id,
        "number": entry["sequence"],
        "sequence": entry["sequence"],
        "started_at": utc_now_iso(),
        "target_rev": target_rev,
        "deadline": reservation_deadline(wall_timeout_sec),
    }
    entry.setdefault("records", []).append(record)
    return record


def refund_round(entry: dict, record_id: str) -> bool:
    """스폰 전 실패한 예약을 제거한다. sequence identity는 재사용하지 않는다."""
    before = len(entry.get("records", []))
    entry["records"] = [
        row for row in entry.get("records", [])
        if isinstance(row, dict) and row.get("id") != record_id
    ]
    refunded = len(entry["records"]) != before
    if refunded and entry.get("count", 0) > 0:
        entry["count"] -= 1
    return refunded


def append_round_outcome(entry: dict, outcome: dict) -> dict:
    """완성된 라운드 산출을 보존 이력에 추가한다."""
    entry.setdefault("rounds", []).append(outcome)
    return outcome


def round_outcome_order_key(outcome: dict) -> tuple[bool, int]:
    """sequence 없는 구기록을 앞에 두는 안정 정렬 키."""
    sequence = outcome.get("sequence")
    valid = isinstance(sequence, int) and not isinstance(sequence, bool)
    return (valid, sequence if valid else 0)


def ordered_round_outcomes(
    rounds: Sequence[dict],
    *,
    order_key: Callable[[dict], object] | None = None,
) -> list[dict]:
    """append 완료 순서가 아니라 예약 sequence 순으로 산출을 정렬한다."""
    return sorted(rounds, key=order_key or round_outcome_order_key)


def recorded_must_fix_series(
    entry: dict,
    *,
    order_key: Callable[[dict], object] | None = None,
) -> tuple[int | None, ...]:
    """예약 순서의 must-fix 추이. 셀 근거가 없으면 None을 보존한다."""
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    series: list[int | None] = []
    for outcome in ordered_round_outcomes(rounds, order_key=order_key):
        value = outcome.get("must_fix")
        series.append(
            value if isinstance(value, int) and not isinstance(value, bool) else None
        )
    return tuple(series)


def inflight_reservations(
    entry: dict,
    *,
    wall_timeout_sec: int | None = None,
) -> int:
    """아직 실행 중일 수 있는 미마감 예약 수."""
    now = utc_now_iso()
    legacy_cutoff = None
    if wall_timeout_sec and wall_timeout_sec > 0:
        legacy_cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=wall_timeout_sec)
        ).isoformat()
    total = 0
    for row in entry.get("records") or []:
        if not isinstance(row, dict) or row.get("finished_at"):
            continue
        deadline = row.get("deadline")
        if isinstance(deadline, str) and deadline:
            if deadline < now:
                continue
            total += 1
            continue
        started = row.get("started_at")
        if (
            legacy_cutoff is not None
            and isinstance(started, str)
            and started
            and started < legacy_cutoff
        ):
            continue
        total += 1
    return total


def convergence_round_usage(
    entry: dict,
    *,
    wall_timeout_sec: int | None = None,
    order_key: Callable[[dict], object] | None = None,
) -> tuple[int, int]:
    """(완료 산출 수, 실행 중 예약 수)."""
    return (
        len(recorded_must_fix_series(entry, order_key=order_key)),
        inflight_reservations(entry, wall_timeout_sec=wall_timeout_sec),
    )


def convergence_refusal(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
    order_key: Callable[[dict], object] | None = None,
) -> str | None:
    """must-fix 증가 또는 완료+진행 중 라운드 상한의 거부 사유."""
    series = recorded_must_fix_series(entry, order_key=order_key)
    if (
        len(series) >= 2
        and series[-1] is not None
        and series[-2] is not None
        and series[-1] > series[-2]
    ):
        return CONVERGENCE_DIVERGING
    completed, inflight = convergence_round_usage(
        entry, wall_timeout_sec=wall_timeout_sec, order_key=order_key
    )
    if completed + inflight >= limit:
        last = series[-1] if series else None
        return CONVERGENCE_CAP_REACHED if last == 0 else CONVERGENCE_CAP_UNRESOLVED
    return None


def latest_round_outcome(
    entry: dict,
    *,
    order_key: Callable[[dict], object] | None = None,
) -> dict | None:
    """예약 순서상 최신 완료 산출."""
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    ordered = ordered_round_outcomes(rounds, order_key=order_key)
    return ordered[-1] if ordered else None


def describe_pm_verified_resolution(declared: dict) -> str:
    """보고/완료 출력용 표기 — 기계 확인 해소임을 명시(리뷰 재투입 없음).

    `declared` 는 보고 호출 관례를 유지하려고 받는다. 이 처분은 별도 근거 blob 을 싣지 않아
    (증거는 매번 라이브로 재검증) 문구는 kind 고정이다.
    """
    del declared
    return "pm-verified(PM 기계 확인 해소·reviewer 재투입 없음)"
