#!/usr/bin/env python3
"""PM relay — 상태 없는 thin supervisor 세션 자동-회전 (엔진 core).

세션당 200K 한계를 *이음매 없이* 회전해 **연속 PM 운영**을 주는 바깥 루프. supervisor 는
LLM 이 아니라 dumb pipe 인 코드 프로세스다 — user↔PM 메시지를 그냥 지나보내고 컨텍스트를
누적하지 않는다(stateless). 연속성은 **file**(board=작업상태)이
담당하고 supervisor 는 무기억으로 회전만 한다.

루프 (run_loop):
  spawn PM(fresh ctx + bootstrap 프롬프트로 file 재유도)
    → (user 입력 ↔ relay_turn) 반복
    → 매 turn 직후 stop_marker_present(sid) 1회 stat
        → marker 있으면: 그 turn 은 이미 처리된 완료 후 신호 →
           새 sid 로 respawn(같은 입력 반복 없음) → 계속
  EOF / `/quit` → 종료.

회전 관측 = ctx 가드가 박는 marker(`.project_manager/.local/ctx-stop/<sid>.done`).
marker 파일명 예측 = 결정적 `--session-id`(supervisor 가 uuid4 발급 → driver 가 child 에 전달).
hook·pm_handoff·pm_bootstrap 는 **무수정**(읽기만) — supervisor 는 그 marker 를 stat 만 한다.

이 모듈은 **하니스 무관**(claude/opencode 공통). driver 는 SessionDriver Protocol 뒤로 주입
(DI 경계) — 테스트는 FakeDriver, 실 구동은 claude driver(`pm_orch_claude.py`).
"""
from __future__ import annotations

import codecs
import contextlib
import datetime
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, NamedTuple, Protocol, TextIO

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


# baked 엔진 rev — engine_rev.py --bump가 기계 일괄 재작성한다.
ENGINE_REV = "v1.6.3"


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
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/에서 경로 로드한다.

    장부 락 경로(읽기·쓰기 모두 이 락을 지난다)에서만 지연 로드한다 — 회전 루프(supervisor)와
    driver import 경로는 이 seam에 의존하지 않게 두려는 것이다. 로드 실패는 흡수하지 않고(fail-loud) 캐시하되,
    중앙 loader가 소비 때마다 baked rev를 재검증하므로 사본 skew는 계속 표출된다.
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


# marker 디렉토리 — ctx_stop_hook.py 의 _MARKER_DIR 와 동일해야 한다(읽기 측·hook 이 쓰는 측).
MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"


def _runtime_skill_entry(skill: str) -> str:
    """현재 실행 하네스의 사용자 호출 표기(Codex env marker 외 slash)."""
    prefix = "$" if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI") else "/"
    return f"{prefix}{skill}"

# child 에 줄 bootstrap 프롬프트 — 새 PM 이 file(board+handoff)에서 맥락을 재유도하게 유도.
# 맥락 자체를 주입하지 않는다(stateless·file-as-memory) — 새 세션이 직접 읽게 한다.
def build_bootstrap_prompt(
    task: str | None = None,
    *,
    entry: str | None = None,
) -> str:
    """재진입 부트스트랩 프롬프트를 빌드한다 — task 명시 시 `/pm-bootstrap --task <name>` 로 주입.

    **relay task 전달 = () 명시 전달**(): supervisor 가 받은 task 정체성을
    재진입 프롬프트에 실값으로 박아, 컨텍스트 한계로 회전된 새 PM 세션이 같은 task 를 재바인딩
    (resume)하게 한다. cwd/env 추론()은 기각("cwd 는 해소에 참여하지 않는다"·"cwd/env
    추론 금지"와 모순) — 정체성은 per-call 명시 전달이다. task 슬롯 0개 엣지에서도 (b)만
    동작한다. task 없으면(슬롯/솔로) bare `/pm-bootstrap`(현행·byte-동일)."""
    cmd = entry or _runtime_skill_entry("pm-bootstrap")
    if task:
        cmd += f" --task {task}"
    return (
        "너는 이 프로젝트의 PM 세션이다. 이전 PM 세션이 컨텍스트 한계로 회전됐다. "
        f"먼저 `{cmd}` 을 수행하고 `log/current.md` 의 최신 handoff entry 를 읽어 "
        "직전 세션의 작업을 이어받아라. 준비되면 'READY' 라고만 답하라."
    )


# 기본(task 무·슬롯/솔로) 재진입 프롬프트 — 현행 bare `/pm-bootstrap` 프롬프트와 byte-동일.
BOOTSTRAP_PROMPT = build_bootstrap_prompt(entry="/" + "pm-bootstrap")

# 종료 명령 — supervisor 루프를 끝낸다(EOF 와 동치).
QUIT_COMMANDS = frozenset({"/quit", "/exit"})

# 연속 respawn 가드 기본값 — bootstrap turn 만으로 marker 가 박히면
# marker→respawn→또 marker 무한 회전(토큰 무한 소모). 연속 즉시-회전이 이 횟수를
# 넘으면 명시 중단한다. 보수적 기본(드문 병적 케이스 방어용).
MAX_CONSECUTIVE_RESPAWNS = 5

# run_loop 가드 발동 종료 코드 — 정상 종료(0·EOF/quit)와 구분되는 sentinel.
GUARD_TRIPPED_RC = 1

# 격리 스모크에서 관측한 relay bootstrap turn 실측 상한(31.8k~40.4k tok).
RELAY_BOOTSTRAP_COST_TOKENS = 40_400


def validate_relay_budget(budget: int | None, stop_pct: int) -> None:
    """주입된 relay ctx 예산의 유효 회전 임계가 bootstrap 상한보다 큰지 검증한다.

    ``None`` 은 예산 가드 미주입이므로 통과한다. 값이 주입됐다면 stop_pct 적용 후
    회전 임계가 실측 bootstrap 상한을 엄격히 초과해야 한다.
    """
    if budget is None:
        return
    effective_threshold_x100 = budget * (100 - stop_pct)
    if effective_threshold_x100 <= RELAY_BOOTSTRAP_COST_TOKENS * 100:
        effective_threshold = effective_threshold_x100 / 100
        raise ValueError(
            f"[relay] ctx 예산 {budget} tok·stop_pct {stop_pct}%의 유효 회전 임계 "
            f"{effective_threshold:g} tok은 bootstrap 실측 상한 "
            f"{RELAY_BOOTSTRAP_COST_TOKENS} tok 이하여서 즉시 세션 회전 위험. "
            "ctx_window_tokens를 높이거나 stop_pct를 낮춰라."
        )

# 위임/외부리뷰 raw 위치 장부. 두 실행 표면이 같은 파일을 갱신하므로 파일락 아래
# read-modify-write 하고 unique tmp를 atomic replace한다. 미마감은 완료 폭주와 별도 보존해
# 최근 비정상 종료를 찾을 수 있게 하되, 기간과 개수에 모두 상한을 둬 조회가 무한 누적으로
# 무력화되지 않게 한다. raw 파일은 감사 산출물이므로 이 장부 정리에서 삭제하지 않는다.
RAW_LEDGER_VERSION = 1
RAW_LEDGER_UNFINISHED_DAYS = 30
RAW_LEDGER_COMPLETED_DAYS = 7
RAW_LEDGER_MAX_UNFINISHED = 128
RAW_LEDGER_MAX_COMPLETED = 256


def raw_storage_paths(
    repo: Path,
    surface: str,
    output_dir: Path | None = None,
    *,
    temp_dir: Path,
) -> tuple[Path, Path]:
    """raw 디렉터리와 공유 장부 경로를 결정한다.

    명시 output_dir은 raw와 장부를 함께 그 디렉터리에 격리한다. 기본은 해소된 repo의
    `.project_manager/.local/{delegate,review}`와 공유 `raw_outputs.json`이다. 채택자 형상에서
    PM 홈 마커를 못 찾으면 OS tempdir의 결정적 장부로 폴백한다.
    """
    if output_dir is not None:
        base = Path(output_dir)
        return base, base / "raw_outputs.json"
    if (repo / ".project_manager").is_dir():
        local = repo / ".project_manager" / ".local"
        return local / surface, local / "raw_outputs.json"
    fallback = Path(temp_dir)
    return fallback, fallback / "pm_raw_outputs.json"


def _raw_lock_path(ledger_path: Path) -> Path:
    return ledger_path.with_suffix(ledger_path.suffix + ".lock")


@contextlib.contextmanager
def _raw_ledger_lock(ledger_path: Path) -> Iterator[None]:
    """장부 read-modify-write를 배타 파일락으로 직렬화한다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `file_lock` seam이 소유하고
    락 경로 규약(장부 옆 `.lock`)과 권한(0o600)만 이 도구가 정한다.
    """
    with _load_file_lock().exclusive_file_lock(
        _raw_lock_path(ledger_path), mode=0o600,
    ):
        yield


def _read_raw_ledger(ledger_path: Path) -> dict:
    """장부 부재만 빈 장부로 보고 손상은 fail-loud한다."""
    if not ledger_path.exists():
        return {"version": RAW_LEDGER_VERSION, "records": []}
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"raw 장부 형식 오류: {ledger_path}")
    return data


def _write_raw_ledger(ledger_path: Path, ledger: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_name(
        f"{ledger_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(str(tmp), str(ledger_path))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _parse_raw_time(value: object) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _prune_raw_records(
    records: list[dict], *, now: datetime.datetime | None = None,
) -> list[dict]:
    """기간과 개수 상한을 적용하되 미마감/마감을 서로 밀어내지 않게 분리한다."""
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    current = current.astimezone(datetime.timezone.utc)
    unfinished: list[dict] = []
    completed: list[dict] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        started = _parse_raw_time(row.get("started_at"))
        if started is None:
            continue
        is_completed = row.get("finished_at") is not None
        age_limit = (
            RAW_LEDGER_COMPLETED_DAYS if is_completed else RAW_LEDGER_UNFINISHED_DAYS
        )
        if current - started > datetime.timedelta(days=age_limit):
            continue
        (completed if is_completed else unfinished).append(row)
    key = lambda row: str(row.get("started_at", ""))
    unfinished = sorted(unfinished, key=key, reverse=True)[:RAW_LEDGER_MAX_UNFINISHED]
    completed = sorted(completed, key=key, reverse=True)[:RAW_LEDGER_MAX_COMPLETED]
    return sorted((*unfinished, *completed), key=key)


def start_raw_record(
    ledger_path: Path,
    *,
    surface: str,
    harness: str,
    model: str,
    role: str,
    raw_path: Path,
    attempt: str,
    now: datetime.datetime | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    """외부 프로세스 실행 전에 미마감 레코드를 원자 등록하고 record id를 반환한다.

    `extra` = 표면별 해소 provenance 의 **추가** 필드(예: 추가 리뷰어의 source/reasoning/command).
    공통 스키마 키는 덮어쓰지 않는다 — 조회면이 보는 필드 이름의 뜻이 표면마다 달라지지 않게 한다.
    """
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    record_id = uuid.uuid4().hex
    row = {
        "id": record_id,
        "surface": surface,
        "harness": harness,
        "model": model,
        "role": role,
        "attempt": attempt,
        "pid": os.getpid(),
        "started_at": current.astimezone(datetime.timezone.utc).isoformat(
            timespec="microseconds"
        ),
        "raw_path": str(raw_path.resolve()),
    }
    for key, value in (extra or {}).items():
        if key not in row:
            row[key] = value
    with _raw_ledger_lock(ledger_path):
        ledger = _read_raw_ledger(ledger_path)
        records = [item for item in ledger["records"] if isinstance(item, dict)]
        records.append(row)
        ledger["version"] = RAW_LEDGER_VERSION
        ledger["records"] = _prune_raw_records(records, now=current)
        _write_raw_ledger(ledger_path, ledger)
    return record_id


# 장부 공통 스키마 키 — 표면별 `extra` 가 절대 덮어쓰지 못하는 이름이다. 조회면이 보는 필드의
# 뜻이 표면마다 달라지면 장부 하나로 실행을 재구성할 수 없다(start 의 같은 정책과 짝).
RAW_LEDGER_RESERVED_KEYS: frozenset[str] = frozenset({
    "id", "surface", "harness", "model", "role", "attempt", "pid",
    "started_at", "raw_path", "finished_at", "rc", "elapsed_sec", "silence_sec",
})


def finish_raw_record(
    ledger_path: Path,
    record_id: str,
    *,
    rc: int,
    elapsed_sec: float,
    silence_sec: float | None,
    now: datetime.datetime | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """동일 레코드에 종료 관측치를 기록한다. 시작 레코드 부재는 유실이므로 fail-loud한다.

    `extra` = 실행 **후에야 알 수 있는** 표면별 구조화 관측(예: 위임의 세션 id·usage 분해·
    직전 라운드 must_fix 항목). 공통 스키마 키는 무시하고, 나머지만 같은 레코드 행에 싣는다 —
    실행 하나의 사실이 두 장부로 갈리지 않게 한다(조인 키 문제 원천 제거).
    """
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    with _raw_ledger_lock(ledger_path):
        ledger = _read_raw_ledger(ledger_path)
        found = False
        for row in ledger["records"]:
            if isinstance(row, dict) and row.get("id") == record_id:
                row.update({
                    "finished_at": current.astimezone(
                        datetime.timezone.utc
                    ).isoformat(timespec="microseconds"),
                    "rc": int(rc),
                    "elapsed_sec": round(float(elapsed_sec), 3),
                    "silence_sec": (
                        None if silence_sec is None else round(float(silence_sec), 3)
                    ),
                })
                for key, value in (extra or {}).items():
                    if key not in RAW_LEDGER_RESERVED_KEYS:
                        row[key] = value
                found = True
                break
        if not found:
            raise ValueError(f"raw 장부 시작 레코드 미발견: {record_id}")
        ledger["records"] = _prune_raw_records(ledger["records"], now=current)
        _write_raw_ledger(ledger_path, ledger)


def raw_records(ledger_path: Path, *, unfinished_only: bool = False) -> list[dict]:
    """장부 레코드를 최신순으로 반환한다. 빈 장부와 손상 장부를 구분한다."""
    with _raw_ledger_lock(ledger_path):
        ledger = _read_raw_ledger(ledger_path)
    rows = [
        row for row in ledger["records"]
        if (
            isinstance(row, dict)
            and (not unfinished_only or row.get("finished_at") is None)
        )
    ]
    return sorted(rows, key=lambda row: str(row.get("started_at", "")), reverse=True)


def unfinished_raw_records(ledger_path: Path) -> list[dict]:
    """미마감 레코드만 최신순으로 반환하는 조회 facade."""
    return raw_records(ledger_path, unfinished_only=True)


class SpawnResult(NamedTuple):
    """spawn/bootstrap turn 결과 — 권위 session id와 사용자에게 전달할 reply."""

    session_id: str
    reply: str | None


class SessionDriver(Protocol):
    """하니스별 세션 구동 경계 (DI). claude=ClaudeCliDriver, 테스트=FakeDriver.

    supervisor 는 이 Protocol 뒤만 알고 실 claude 호출은 driver 에 갇힌다 →
    단위테스트가 실 subprocess 없이 relay/respawn 로직만 검증할 수 있다.
    """

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str | SpawnResult:
        """새 세션을 띄운다 — child 에 결정적 session_id 를 부여하고 bootstrap 프롬프트를
        첫 turn 으로 보낸다. 실제 사용된 session id와 spawn turn reply 를
        ``SpawnResult`` 로 반환한다. 문자열 sid 반환은 driver 전환 기간의 하위호환."""
        ...

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션을 resume 해 한 turn 중계한다 — text 를 보내고 reply 를 반환."""
        ...

    def close(self, session_id: str) -> None:
        """세션 정리(필요 시). `-p` 1회성 turn 은 자동 exit 라 보통 no-op."""
        ...


def new_session_id() -> str:
    """결정적 marker-matching 용 세션 id 발급(uuid4). supervisor 가 발급 →
    child 에 `--session-id` 로 전달 → marker 파일명 `<uuid>.done` 을 예측한다."""
    return str(uuid.uuid4())


def stop_marker_present(root: Path, session_id: str) -> bool:
    """ctx 가드가 박은 회전 marker가 있는지 1회 stat. 폴 스레드 없음(thin)."""
    return _marker_path(root, session_id).exists()


def clear_marker(root: Path, session_id: str) -> bool:
    """떠난 세션의 marker 정리(best-effort). 지웠으면 True. 회전 후 누적 방지용."""
    path = _marker_path(root, session_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ── post-turn marker 계약 ────────────────────────────────────────
# marker 파일 존재 = "이 세션을 다음 입력 전에 회전하라" 는 단일 post-turn 신호.
# payload는 진단용일 뿐 소비자는 존재만 판정한다.
# marker 생산자는 turn 완료 후에 신호를 박제한다.
# Supervisor 는 payload 를 판독하지 않는다.
def write_post_turn_marker(root: Path, session_id: str) -> bool:
    """post-turn 회전 marker 박제(best-effort).

    파일명/경로는 호환 유지하며 Supervisor는 payload를 판독하지
    않고 존재만을 post-turn 신호로 소비한다. 실패는 fail-soft(relay 무중단)."""
    path = _marker_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "post-turn rotation requested\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


def mark_ctx_post_turn_if_over(
    root: Path,
    session_id: str,
    used_tokens: int,
    budget: int,
    stop_pct: int,
    mark=write_post_turn_marker,
) -> bool:
    """turn 사용량이 회전 임계에 닿았으면 post-turn marker 를 박제한다.

    임계는 ``budget * (100 - stop_pct) / 100`` 이며 경계값을 포함한다.
    ``used_tokens <= 0`` 또는 ``budget <= 0`` 은 신호 미주입으로 보아 no-op(False).
    stop_pct 범위 클램프는 호출부 책임이다.
    marker writer 가 성공한 경우에만 True 를 반환한다.
    """
    if used_tokens <= 0 or budget <= 0:
        return False
    if used_tokens * 100 < budget * (100 - stop_pct):
        return False
    return bool(mark(root, session_id))


# claude assistant usage wire → 원장 usage 4필드 이름. 한 스칼라로 접으면 캐시 **재적재**
# (cache_read)와 **새 적재**(cache_creation)가 구분되지 않아 세션 재사용의 비용 근거가 사라진다.
CLAUDE_USAGE_WIRE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("cache_read_input_tokens", "cache_read"),
    ("output_tokens", "output"),
)


def _claude_usage_fields(usage: object) -> dict[str, int] | None:
    """claude `message.usage` wire → 원장 4필드(있는 값만·음이 아닌 정수만).

    **없는 필드는 적지 않는다** — 0 은 "0 토큰"이라는 다른 사실이고, 수집 못 한 자리를 0 으로
    채우면 비용 표가 false-정밀해진다. 값 하나라도 형식이 깨졌으면 그 관측 전체를 미수집으로
    본다(부분 신뢰 값이 사실로 굳는 걸 막는다)."""
    if not isinstance(usage, dict):
        return None
    fields: dict[str, int] = {}
    for wire_key, field in CLAUDE_USAGE_WIRE_FIELDS:
        if wire_key not in usage:
            continue
        value = usage.get(wire_key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        fields[field] = value
    return fields or None


def parse_stream_json(lines) -> tuple[str | None, str | None, int | None]:
    """`claude -p --output-format stream-json` 출력에서 (session_id, result, used_tokens) 추출.

    회전 판정용 3-tuple facade — 같은 1회 파싱의 usage 분해까지 쓰려면
    `_parse_stream_json_events`(원장·세션 재사용 축)를 쓴다. 파서는 하나다.

    - session_id: `system/init` 이벤트의 `session_id`(이후 모든 이벤트에도 실리나 init 우선).
      init 가 없으면 `result` 이벤트의 session_id 로 폴백.
    - result: `result` 이벤트의 `result` 필드(= 최종 reply 텍스트).
    - used_tokens: 마지막 `assistant` 이벤트의 `message.usage` 입력 계열
      (`input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`) +
      `output_tokens` 합 — 회전(post-turn) 판정은 *다음* turn 이 지게 될 컨텍스트가 기준이라
      이번 응답 출력을 포함한다(훅이 도구 실행 전에 보는 "현재 점유"와 용도가 다름·codex 게이트).
      각 값은 음이 아닌 정수만 인정하며 신호가 하나도 없으면 None.
    - JSONDecodeError 라인은 skip(부분/비-JSON 라인에 robust).

    PoC(`scratch/poc/orchestrator_claude_relay_swap.py`)의 run_turn 파싱 골격을
    순수 함수로 추출 — driver 가 호출하고 테스트가 직접 검증한다.
    """
    session_id, result, used_tokens, _usage = _parse_stream_json_events(lines)
    return session_id, result, used_tokens


def _parse_stream_json_events(
    lines,
) -> tuple[str | None, str | None, int | None, dict[str, int] | None]:
    """claude stream-json 1회 파싱 — (session_id, result, used_tokens, usage 4필드).

    `parse_stream_json` 의 본체다. 회전 판정은 스칼라 합만 쓰고 원장은 분해를 쓰므로 두
    소비면이 같은 한 번의 순회를 나눠 갖는다(두 번째 파서를 만들지 않는다)."""
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피.

    session_id: str | None = None
    result: str | None = None
    used_tokens: int | None = None
    usage_fields: dict[str, int] | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id") or session_id
        if event.get("type") == "assistant":
            message = event.get("message")
            usage_fields = _claude_usage_fields(
                message.get("usage") if isinstance(message, dict) else None
            )
            used_tokens = sum(usage_fields.values()) if usage_fields else None
        if event.get("type") == "result":
            result = event.get("result")
            session_id = session_id or event.get("session_id")
    return session_id, result, used_tokens, usage_fields


def parse_opencode_json(lines) -> tuple[str | None, str | None, int | None]:
    """`opencode run --format json` 출력에서 (session_id, reply, used_tokens) 추출.

    claude `parse_stream_json` 과 **대칭** 위치의 opencode 어댑터용 순수 헬퍼 —
    하니스가 다른 한 줄=한 이벤트 JSON 스트림을 같은 3-tuple 규격으로 흡수한다.
    opencode driver(`pm_orch_opencode.py`)가 DI 로 주입받아 쓴다(엔진은 파싱만 보유).

    - session_id: 모든 이벤트 top-level `sessionID`(실측 — 매 이벤트에 실린다). 첫 등장값
      을 잡는다(opencode 가 sid 를 발급 — claude 와 달리 사전지정 불가, 출력 파싱으로 획득).
    - reply: `type:"text"` 이벤트의 `part.text` 를 등장 순서대로 누적(멀티-part 답변 대응).
      reply 가 없으면(text part 0) None.
    - used_tokens: 마지막 `step_finish` 이벤트의 `part.tokens` 에서 유효한 `total`(양의 정수)을
      우선하고, 없으면 `input + output + reasoning + cache.read + cache.write`로 폴백한다. 이때
      reasoning을 별도 가산해 보수적 상위집합으로 잡는다. tokens가 없거나 어느 값/cache 형상이
      비정상이면 None.
      설치 opencode 1.18.5 바이너리 실측도 total 우선 + cache 합산 폴백을 확인했다.
    - 비-JSON / 비-dict 라인은 skip(부분/노이즈 라인에 robust — claude 파서와 동일 정책).

    실측 이벤트 형식(opencode 1.17.6):
      {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"PONG",...}}
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피(claude 파서 대칭).

    session_id: str | None = None
    reply_parts: list[str] = []
    used_tokens: int | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        sid = event.get("sessionID")
        if session_id is None and isinstance(sid, str) and sid:
            session_id = sid
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    reply_parts.append(text)
        if event.get("type") == "step_finish":
            part = event.get("part")
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if isinstance(tokens, dict):
                total = tokens.get("total")
                if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                    used_tokens = total
                else:
                    cache = tokens.get("cache", {})
                    values = [tokens.get(key, 0) for key in ("input", "output", "reasoning")]
                    if isinstance(cache, dict):
                        values.extend(cache.get(key, 0) for key in ("read", "write"))
                    else:
                        values.append(None)
                    # Upstream v1.18.5 근거 파일: session.ts, overflow.ts.
                    used_tokens = (
                        sum(values)
                        if all(isinstance(value, int) and not isinstance(value, bool)
                               and value >= 0 for value in values)
                        else None
                    )
            else:
                used_tokens = None
    reply = "".join(reply_parts) if reply_parts else None
    return session_id, reply, used_tokens


def _codex_usage_contract(wire: dict) -> dict:
    """codex wire usage(`*_tokens` 키)를 엔진 usage contract로 정규화.

    실측(codex 0.144.6·PM 라이브 프로브 5회): `turn.completed.usage` 의 실 wire 키는 접미사 `_tokens`
    형(`input_tokens`·`cached_input_tokens`·`cache_write_input_tokens`·`output_tokens`·
    `reasoning_output_tokens` — 예 input_tokens=
    12481·cached_input_tokens=9600·output_tokens=105·reasoning_output_tokens=92). 파서가 이 **wire→contract
    경계를 소유** 해 driver 인터페이스(접미사 없는 contract 키)를 하니스-무관으로 유지한다 — driver 는
    codex wire 형태를 몰라도 된다(claude/opencode 파서가 각자 wire 를 흡수하는 것과 동형). 비-정수/누락
    키는 0. cached_input/cache_write_input은 contract에 실어 관측 가능하게 두되 driver 사용 합산에서는
    제외한다. cached_input의 input 포함 관계는 실측됐지만 cache_write_input은 관측만 했고 포함 관계를
    검증하지 않았으므로 비합산 정책 자체만 보존한다(codex driver `_maybe_mark_ctx`).
    """
    def _n(key: str) -> int:
        val = wire.get(key)
        return val if isinstance(val, int) and not isinstance(val, bool) else 0

    return {
        "input": _n("input_tokens"),
        "cached_input": _n("cached_input_tokens"),
        "cache_write_input": _n("cache_write_input_tokens"),
        "output": _n("output_tokens"),
        "reasoning_output": _n("reasoning_output_tokens"),
    }


def parse_codex_json(lines) -> tuple[str | None, str | None, dict | None]:
    """`codex exec --json` 출력(JSONL)에서 (thread_id, reply, usage) 추출.

    claude `parse_stream_json`·opencode `parse_opencode_json` 과 **대칭** 위치의 codex 어댑터용
    순수 헬퍼 — codex 는 thread_id 를 사전지정 못 하고 `thread.started` 이벤트로 발급하므로
    (opencode sid 동형) 출력 파싱으로 획득한다. usage 는 codex driver 의 driver-side 기계 ctx
    가드(relay 경로엔 opencode plugin 같은 marker 채널이 없어 driver 가 예산을
    직접 판정)의 원천이다. 세 파서 모두 3번째 값으로 usage 를 낸다(v1.6.0 일원화 — codex 는 dict·claude/opencode 는 정수).

    - thread_id: `thread.started` 이벤트의 `thread_id`(첫 등장값 = resume 권위 id). 없으면 None
      (driver 가 치명 처리 — resume 불가).
    - reply: `item.*` 이벤트의 agent_message(`item.type == "agent_message"`) `text` — 최종
      (마지막 비어있지-않은) 값. item.started→completed 스트리밍 시 completed 가 최종 전체
      텍스트로 덮어쓴다. reasoning 등 다른 item 은 제외. 없으면 None.
      라이브 smoke 가 확정 — item 의 텍스트 필드가 다르면 여기 한 지점만 조정.)
    - usage: `turn.completed` 이벤트의 `usage`(마지막 등장값)를 wire(`*_tokens`)→contract
      (`{input,cached_input,cache_write_input,output,reasoning_output}`)로
      `_codex_usage_contract` 가 정규화한 값.
      없으면 None. (**실측 wire 키는 접미사 `_tokens`** — 접미사 없는 키를 읽으면 used==0 으로
      ctx 가드가 영구 사멸한다·PM 라이브 프로브 5회 권위·codex/opencode 리뷰 수렴 결함.)
    - 비-JSON / 비-dict 라인은 skip(claude·opencode 파서와 동일 robust 정책).
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피(claude·opencode 파서 대칭).

    thread_id: str | None = None
    reply: str | None = None
    usage: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if thread_id is None and isinstance(tid, str) and tid:
                thread_id = tid
        elif isinstance(etype, str) and etype.startswith("item"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    reply = text  # 최종(마지막) agent_message 전체 텍스트로 갱신.
        elif etype == "turn.completed":
            u = event.get("usage")
            if isinstance(u, dict):
                usage = _codex_usage_contract(u)  # wire(_tokens) → contract 정규화.
    return thread_id, reply, usage


# ── 하네스 드라이버 계약 (검증·argv·회신 추출) — 위임/추가 리뷰어 공용 단일 소유 ────────
#
# 위임(pm_delegate)과 추가 리뷰어(external_review)는 **같은 세 하네스 CLI**를 스폰한다. 값 테이블과
# argv 조립이 두 표면에 각각 있으면 규칙이 둘로 갈리므로(진행신호 테이블·시간 프로필과 같은 이유)
# 이 leaf 모듈이 단일 소유한다. 이 모듈은 다른 엔진 도구를 import 하지 않아(cycle-free) 양쪽이
# 안전하게 deep-import 할 수 있다 — external_review 는 pm_delegate 를 import 하지 않는다(역방향
# deep-import 가 이미 있어 순환이 된다).

HARNESS_CHOICES: tuple[str, ...] = ("claude", "codex", "opencode")
# 권한축 — write(편집 가능) / read(읽기 전용). 역할이 아니라 **축**이 드라이버 플래그를 정한다.
WRITE_ROLES: frozenset[str] = frozenset({"developer", "architect"})
READ_ROLES: frozenset[str] = frozenset({"researcher", "code-reviewer"})

# reasoning 드라이버별 허용집합 — (codex-cli 0.145.0·claude 2.1.218·
# opencode 1.18.4). 허용집합 밖 지정은 fail-loud(조용한 무시/강등 금지):
#   · codex(`-c model_reasoning_effort`): low/medium/high/xhigh/max — 0.145.0 xhigh 실측 수용(exec
#     앞/뒤 위치 무관 rc=0). `max` 는 추가 리뷰어 기본 프로필(gpt-5.6-sol)이 쓰는 ladder 상단이며,
#     codex 는 미지원값을 provider 로 넘기지 않고 CLI 에서 세우므로 허용집합에 명시 등재한다.
#   · claude(`--effort`): low/medium/high/xhigh/max — claude CLI 가 미지원값에 "Valid values: low,
#     medium, high, xhigh, max" 경고를 뱉어 **CLI-authoritative 실측**.
#   · opencode(`--variant`): minimal/low/medium/high/max — opencode 는 `--variant` 를 **CLI 검증하지
#     않고 provider 로 passthrough**(valid/invalid 모두 rc=0·미지원값은 provider 가 silent-ignore).
#     그래서 이 집합은 opencode 문서화 ladder 를 **typo-guard** 로 인코딩한 것 — silent no-op 을
#     전송 전에 차단한다. provider 별 실지원은 다를 수 있다(passthrough 특성).
REASONING_ALLOWED: dict[str, frozenset[str]] = {
    "codex": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "opencode": frozenset({"minimal", "low", "medium", "high", "max"}),
}

# codex sandbox 모드 — write=workspace-write·read=read-only.
# **실측(0.145.0)**: read-only 는 worktree 밖 `/tmp` 쓰기까지 차단해 pytest 기동이 막힌다(테스트
# 실행이 필요한 리뷰는 workspace-write 상향 필요). `-a never` 하 `git push` 는 승인 deadlock 없이
# git 레벨에서 실패한다 — push 방어선은 sandbox 가 아니라 role prompt 다.
CODEX_SANDBOX: dict[str, str] = {"write": "workspace-write", "read": "read-only"}
# opencode 권한 agent — write=build·read=plan.
OPENCODE_AGENT: dict[str, str] = {"write": "build", "read": "plan"}
# opencode `run` 은 첨부 `--file` 이 있어도 **비어있지 않은 positional message 를 요구**한다(실측·
# message 부재 시 rc=1). 실 지시는 `--file` 프롬프트에 있으므로 고정 안내 message 를 준다.
OPENCODE_ATTACHED_MSG = "첨부된 프롬프트 파일(--file)의 지시를 그대로 수행하라."

# claude 가용 도구셋 — `--tools`(가용성 제한, `--allowedTools` 아님). 역할축:
#   write(developer/architect) = 편집 도구 포함 · researcher = 순수읽기(Bash 제외·기계적) ·
#   code-reviewer = 읽기+Bash(pytest·Write/Edit 제외). 콤마-구분 단일 인자로 전달.
CLAUDE_TOOLS_WRITE = "Read,Glob,Grep,Bash,Write,Edit"
CLAUDE_TOOLS_RESEARCHER = "Read,Glob,Grep"
CLAUDE_TOOLS_REVIEWER = "Read,Glob,Grep,Bash"


class HarnessContractError(RuntimeError):
    """하네스/reasoning 값이 드라이버 허용집합 밖 — 스폰 전 fail-loud.

    소비 표면은 자기 예외(DelegateError·ReviewerTargetError)로 감싸되 **문구는 이 모듈이 소유**한다
    (같은 오설정이 두 표면에서 다르게 설명되지 않게).
    """


def validate_harness(harness: str) -> str:
    """지원 하네스만 통과시킨다(조용한 폴백 금지·명시 등록 요구)."""
    if harness not in HARNESS_CHOICES:
        raise HarnessContractError(
            f"미지원 harness {harness!r} — 지원: {', '.join(HARNESS_CHOICES)}. "
            "조용한 폴백 금지(명시 등록 요구)."
        )
    return harness


def validate_reasoning(harness: str, reasoning: str | None) -> str | None:
    """reasoning 값을 드라이버별 허용집합으로 검증. 미지정=None(플래그 생략).

    허용집합 밖이거나 capability 미확정이면 fail-loud — 조용한 무시/자동 강등 금지."""
    if reasoning is None or not reasoning.strip():
        return None
    reasoning = reasoning.strip()
    allowed = REASONING_ALLOWED.get(harness, frozenset())
    if reasoning not in allowed:
        known = ", ".join(sorted(allowed)) if allowed else "(미확정 라이브 실측 전)"
        raise HarnessContractError(
            f"reasoning {reasoning!r} 은 {harness} 드라이버 허용집합 {known} 밖 — "
            "조용한 무시/강등 금지·명시 설정을 요구한다."
        )
    return reasoning


def perm_axis(role: str) -> str:
    """역할 → 권한축('write' | 'read'). 미등록 역할은 보수적으로 read."""
    return "write" if role in WRITE_ROLES else "read"


def claude_tools(role: str) -> str:
    """역할 → claude `--tools` 값(code-reviewer = 읽기 + Bash·Write/Edit 없음)."""
    if role in WRITE_ROLES:
        return CLAUDE_TOOLS_WRITE
    if role == "researcher":
        return CLAUDE_TOOLS_RESEARCHER
    return CLAUDE_TOOLS_REVIEWER


def build_codex_argv(model: str, reasoning: str | None, role: str,
                     cwd: str | None = None) -> list[str]:
    """codex argv. `-a never -s <mode>` 는 exec **앞** 전역 옵션(exec 뒤는 rc=2).

    프롬프트는 stdin 주입(argv positional 아님). `cwd` 를 주면 `-C` 로 핀하고, 생략하면 프로세스
    cwd 를 그대로 쓴다(추가 리뷰어는 격리 거울을 subprocess cwd 로 이미 고정한다)."""
    mode = CODEX_SANDBOX[perm_axis(role)]
    argv = ["codex", "-a", "never", "-s", mode, "exec", "--json", "--skip-git-repo-check"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    if model:
        argv += ["-m", model]
    if reasoning:
        # `-c model_reasoning_effort=<r>` 는 `-a`/`-s`(exec 뒤=rc=2)와 달리 exec **앞/뒤 모두
        # rc=0 수용**된다(실측). reasoning 미지정 시 무영향.
        argv += ["-c", f"model_reasoning_effort={reasoning}"]
    return argv


def build_claude_argv(model: str, reasoning: str | None, role: str,
                      resume_session_id: str | None = None) -> list[str]:
    """claude argv. 프롬프트 stdin 주입·cwd 존중(플래그 불요). `--tools` 로 역할별 가용
    도구 제한, write 역할은 `--permission-mode acceptEdits` 로 무프롬프트 완주.

    **출력 형식 = `stream-json`**: `json` 은 종료 시 단일 덩어리라 진행 신호가 0이다. `--verbose`
    는 CLI 강제 요구다(미동반 시 즉시 rc≠0·2.1.220 실측). 회신 추출은 parse_stream_json 이며 이
    파서는 두 형식을 모두 통과시킨다.

    `resume_session_id` 를 주면 그 세션을 이어받는 turn 이 된다(직전 컨텍스트가 프롬프트 캐시
    단가로 재적재되고 도구 재읽기가 사라진다 — "재적재 0" 이 아니다). 값은 형식 검증을 통과할
    때만 실린다: 재개 id 는 argv 에 실리는 유일한 **외부 유래 토큰**이라, 손상 장부의 `--` 시작
    문자열이 그대로 나가면 CLI 가 그걸 플래그로 소비한다."""
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--tools", claude_tools(role)]
    if reasoning:
        argv += ["--effort", reasoning]
    if role in WRITE_ROLES:
        argv += ["--permission-mode", "acceptEdits"]
    if resume_session_id is not None:
        if not is_resumable_session_id(resume_session_id):
            raise HarnessContractError(
                f"세션 id 형식 불일치로 재개 argv 를 만들지 않는다: {resume_session_id!r} — "
                "형식(uuid 표기)이 맞는 값만 실린다(플래그 오소비 차단)."
            )
        argv += ["--resume", resume_session_id]
    return argv


def build_opencode_argv(
    model: str, reasoning: str | None, role: str, cwd: str, prompt_file: str,
) -> list[str]:
    """opencode argv. 프롬프트는 `--file`(실존 인터페이스·길이/ps 노출 회피)·cwd 는 `--dir`
    로 핀(opencode 는 subprocess cwd 를 무시한다). `--agent build|plan` 으로 권한 강제."""
    agent = OPENCODE_AGENT[perm_axis(role)]
    argv = ["opencode", "run", OPENCODE_ATTACHED_MSG, "--file", str(prompt_file),
            "--agent", agent, "--format", "json", "--dir", str(cwd)]
    if model:
        argv += ["-m", model]
    if reasoning:
        argv += ["--variant", reasoning]
    return argv


# ── 세션 재사용(resume) 계약 ─────────────────────────────────────────────────
#
# 재개는 **직전 세션 컨텍스트의 프롬프트 캐시 재적재 + 도구 재읽기 0**이다(재적재 비용 0 아님).
# 지원 여부는 분기가 아니라 선언표다:
#   · claude — `-p` 재개 플래그로 같은 세션 id 를 이어받는다. 성공 판정은 **회신 세션 id 일치**
#     (rc 로 판정하지 않는다 — 만료/부분 손상이 rc=0 새 세션으로 나오는 축은 미측정이다).
#   · codex — 재개가 대화형 전용이라 `exec --json` argv 와 형식이 다르다. 미검증 argv 를
#     출하하지 않는다(항상 fresh).
#   · opencode — 위임 드라이버에 재개 배선 없음.
HARNESS_RESUME_SUPPORT: dict[str, bool] = {
    "claude": True, "codex": False, "opencode": False,
}

# 세션 id 형식(uuid 표기) 가드. 재개 id 는 장부에서 온 값이라 형식이 맞을 때만 argv 에 싣는다.
SESSION_ID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z"
)


def harness_supports_resume(harness: str) -> bool:
    """이 하네스로 세션 재사용을 시도해도 되는가(미등록/미지원은 False = fresh)."""
    return bool(HARNESS_RESUME_SUPPORT.get(harness, False))


def is_resumable_session_id(value: object) -> bool:
    """재개 argv 에 실을 수 있는 세션 id 형식인가(uuid 표기 완전일치)."""
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


class HarnessReply(NamedTuple):
    """하네스 wire 1회의 구조화 관측 — 최종 회신·세션 id·원장 usage.

    세 파서가 각자 wire 를 흡수한 결과를 한 형상으로 모은다. 종전 reply 전용 seam 은 이 관측의
    한 필드를 꺼내는 얇은 facade 다(세션 id·usage 를 버리던 자리)."""

    reply: str | None
    session_id: str | None
    usage: dict[str, int] | None


def _claude_harness_reply(lines) -> HarnessReply:
    session_id, reply, _used_tokens, usage = _parse_stream_json_events(lines)
    return HarnessReply(reply, session_id, usage)


def _codex_harness_reply(lines) -> HarnessReply:
    """thread id 가 세션 id 자리다. usage 는 원장 4필드로 옮기지 않는다 — `cached_input` 이
    `input` 에 포함되고 cache write 의 포함 관계는 미검증이라, 같은 필드 이름으로 접으면 합산
    의미가 하네스마다 달라진다(추정 매핑 대신 필드 부재)."""
    thread_id, reply, _usage = parse_codex_json(lines)
    return HarnessReply(reply, thread_id, None)


def _opencode_harness_reply(lines) -> HarnessReply:
    """총합 스칼라만 오는 축 — 분해 관측이 없으므로 원장 usage 는 필드 부재로 남긴다."""
    session_id, reply, _used_tokens = parse_opencode_json(lines)
    return HarnessReply(reply, session_id, None)


# 하네스 → wire 관측 어댑터(선언표 — 소비면에 하네스 분기를 만들지 않는다).
HARNESS_REPLY_ADAPTERS = {
    "claude": _claude_harness_reply,
    "codex": _codex_harness_reply,
    "opencode": _opencode_harness_reply,
}


def extract_harness_result(harness: str, stdout: str) -> HarnessReply:
    """하네스 stdout(구조화 wire)에서 회신·세션 id·usage 를 한 번에 관측한다.

    reply 의 **타입 계약은 이 seam 이 소유한다**(아래 `extract_harness_reply` 주석 참조) — 그
    판정을 여기서 한 번 세우고 두 소비면(회신 전용 facade·원장/세션 재사용)이 같은 결과를 쓴다.
    미지원 하네스는 fail-loud(조용한 폴백 금지)."""
    adapter = HARNESS_REPLY_ADAPTERS.get(harness)
    if adapter is None:
        raise HarnessContractError(f"미지원 harness {harness!r} — reply 추출 불가.")
    observed = adapter(stdout.splitlines())
    reply = observed.reply
    return observed._replace(
        reply=reply if isinstance(reply, str) and reply.strip() else None
    )


def extract_harness_reply(harness: str, stdout: str) -> str | None:
    """하네스 stdout(구조화 wire)에서 최종 reply 텍스트만 추출한다.

    claude=parse_stream_json → result · codex=parse_codex_json → reply ·
    opencode=parse_opencode_json → reply. 미추출(파싱 실패·빈 출력)은 None — 호출자가 fail-loud
    한다(wire 원문을 그대로 판정에 넣지 않는다). 세션 id·usage 까지 필요한 호출자는
    `extract_harness_result` 를 쓴다(같은 관측의 다른 필드다).

    **최종 회신의 타입 계약은 이 seam 이 소유한다** — 내용이 있는 `str` 하나이고, 그 밖의 값
    (dict/list/수·null·빈 문자열·공백만)은 전부 미추출(None)이다. wire 파서는 "wire 가 무엇을 말했는지"를
    그대로 옮기는 층이라 claude `result` 필드처럼 스키마상 임의 JSON 값이 올 수 있는 자리를
    통과시킨다(실측 형상: `{"type":"result","result":{...}}`). 그 값이 소비 표면까지 흘러가면
    판정 파싱·전사 단계에서 AttributeError 로 터지는데, 그 시점은 raw 박제·장부 마감 **전**이라
    터진 실행은 원문도 장부도 남기지 않는다. 그래서 타입은 두 표면(위임·추가 리뷰어)이 공유하는
    이 한 지점에서 세운다 — 형상 붕괴는 "회신 없음"으로 환원되고 호출자의 기존 fail-loud 경로가
    그대로 받는다.

    **공백만 있는 회신은 빈 회신과 같다.** 참/거짓만 보면 `" "`·`"\\n"` 이 통과해 리뷰어가 아무 말도
    하지 않은 실행이 '회신 있음'으로 기록되고, 그 뒤의 판정 파싱은 아무 토큰도 찾지 못한 채 판정
    불명확으로만 끝난다 — 폴백 신호(`reply_extraction_failed`)가 서지 않아 호출자가 내부 리뷰어로
    갈아탈 근거를 잃는다. 그래서 존재 판정은 `strip()` 으로 하고, **돌려주는 값은 원문 그대로**다
    (앞뒤 공백까지 회신 바이트의 일부라 판정·전사 표면이 wire 와 어긋나면 안 된다)."""
    return extract_harness_result(harness, stdout).reply


# ── Codex egress 승격 브리지 (network-off 안전 경계 × 외부 스폰) ─────────────────
#
# 실측(2026년 8월 7일·adopter#0): Codex PM(`workspace-write` + `network_access=false`)에서 띄운
# 자식 CLI는 부모 sandbox 의 egress 차단을 그대로 상속해 API 재시도 뒤 rc≠0 로 끝난다. 엔진은
# sandbox 를 스스로 벗어나지 않는다(재실행·config 자동 완화 없음) — 실제 이탈은 Codex 도구 호출의
# `sandbox_permissions="require_escalated"` 메타데이터만 수행하고, 엔진은 그 의도를
# `--codex-egress-escalated` 호출층 attestation 으로 감사한다.
#
# 같은 실측에서 **승인형 비샌드박스 명령에도 마커가 그대로 남았다**. 그래서 마커는 "승격이 필요한
# 형상인가"만 판정하고, "이번 호출이 승격됐는가"는 attestation 플래그가 소유한다(플래그는 권한을
# 만들지 않으므로 샌드박스 안에서 잘못 붙이면 권한 상승 없이 타겟 CLI 가 기존처럼 fail-loud 한다).
#
# 두 외부 스폰 표면(pm_delegate 실위임·external_review 추가 리뷰어)이 이 절을 공유한다 — 진입점
# 스크립트와 지속 동의 키만 표면별 인자다(승인 prefix 는 그 표면 자신을 가리켜야 재사용된다).

CODEX_EGRESS_MARKER_ENV = "CODEX_SANDBOX_NETWORK_DISABLED"
CODEX_EGRESS_NOT_REQUIRED = "not-required"
CODEX_EGRESS_ESCALATED_ATTESTED = "escalated-attested"
CODEX_EGRESS_FLAG = "--codex-egress-escalated"
_CODEX_EGRESS_MARKER_TRUE = frozenset({"1", "true", "yes", "on"})

# 표면별 진입점 — Codex 도구 재사용 승인 prefix 가 되는 2-token 이다.
DELEGATE_ENTRYPOINT = ".project_manager/tools/pm_delegate.py"
EXTERNAL_REVIEW_ENTRYPOINT = ".project_manager/tools/external_review.py"


def running_on_windows() -> bool:
    """진입점 인터프리터 표기 판정(Windows 는 런처 `py`)."""
    return os.name == "nt"


def codex_egress_escalation_required(env: dict[str, str] | None = None) -> bool:
    """현재 환경이 외부 스폰에 Codex egress 승격을 요구하나.

    마커 부재/false(채택자가 네트워크를 명시 허용한 형상)는 False — 기존 실행을 그대로 보존한다."""
    env = os.environ if env is None else env
    value = env.get(CODEX_EGRESS_MARKER_ENV)
    return value is not None and value.strip().lower() in _CODEX_EGRESS_MARKER_TRUE


def codex_egress_label(*, escalation_required: bool, attested: bool) -> str:
    """실행 provenance·raw 감사 헤더가 공유하는 egress 라벨(두 값만)."""
    return (
        CODEX_EGRESS_ESCALATED_ATTESTED
        if (escalation_required and attested)
        else CODEX_EGRESS_NOT_REQUIRED
    )


def codex_egress_provenance(*, escalation_required: bool, attested: bool,
                            env: dict[str, str] | None = None) -> str:
    """`codex_egress=<라벨>` 1줄 — 실행 후 안전 경계를 재구성하는 감사 입력."""
    env = os.environ if env is None else env
    label = codex_egress_label(
        escalation_required=escalation_required, attested=attested)
    if label == CODEX_EGRESS_ESCALATED_ATTESTED:
        marker = env.get(CODEX_EGRESS_MARKER_ENV)
        note = (f"호출층 attestation({CODEX_EGRESS_FLAG}) 동반 · "
                f"{CODEX_EGRESS_MARKER_ENV}={marker!r} 은 승격 명령에서도 남는다"
                "(실측·엔진은 마커를 해제하지 않음)")
    elif attested:
        note = (f"{CODEX_EGRESS_MARKER_ENV} 미설정/false — 승격이 필요 없는 환경이라 "
                f"{CODEX_EGRESS_FLAG} 는 권한 의미가 없다(감사 기록만)")
    else:
        note = f"{CODEX_EGRESS_MARKER_ENV} 미설정/false — Codex sandbox egress 차단 없음"
    return f"codex_egress={label} · {note}"


def codex_egress_entrypoint(script: str, *,
                            windows: bool | None = None) -> tuple[str, str]:
    """Codex 도구 재사용 승인과 복사 재실행이 공유하는 2-token prefix."""
    on_windows = running_on_windows() if windows is None else windows
    return ("py" if on_windows else "python3", script)


def codex_egress_prefix_rule_text(script: str, *,
                                  windows: bool | None = None) -> str:
    """도구 승인 UI 에 그대로 붙일 좁은 prefix 규칙 표기."""
    interpreter, entry = codex_egress_entrypoint(script, windows=windows)
    return f'prefix_rule=["{interpreter}", "{entry}"]'


def codex_egress_retry_command(argv: list[str], *, script: str,
                               windows: bool | None = None) -> str:
    """재사용 승인 prefix와 동일한 진입점 + attestation 재실행 표시."""
    cleaned = [token for token in argv if token != CODEX_EGRESS_FLAG]
    # 안내를 복사한 실행이 바로 위 `prefix_rule`을 소비해야 한다. sys.executable·
    # resolve 절대경로를 쓰면 기능은 같아도 승인 토큰이 달라져 다시 prompt 될 수 있다.
    on_windows = running_on_windows() if windows is None else windows
    interpreter, entry = codex_egress_entrypoint(script, windows=on_windows)
    command = [interpreter, entry, *cleaned, CODEX_EGRESS_FLAG]
    if not on_windows:
        return shlex.join(command)
    # Encoded PowerShell wrapper를 쓰면 도구가 보는 명령 prefix가 `powershell.exe`로
    # 바뀌어 `py + script` 재사용 승인을 소비하지 못한다. 첫 2 token은 그대로 두고
    # 나머지만 PowerShell literal로 quote한다.
    rest = " ".join("'" + token.replace("'", "''") + "'" for token in command[2:])
    return f"{interpreter} {entry}{' ' + rest if rest else ''}"


def codex_egress_block_message(
    argv: list[str], harness: str, model: str, *,
    script: str,
    consent_key: str,
    subject: str,
    windows: bool | None = None,
) -> str:
    """증명 없는 network-off 실행의 차단 사유 + 실행 가능한 승격 처방."""
    return (
        f"오류: Codex sandbox 네트워크 차단 환경에서 승격 증명 없는 {subject}을 "
        "중단합니다 — 외부 스폰·raw 예약·과금 전입니다.\n"
        f"  · 판정 근거: {CODEX_EGRESS_MARKER_ENV}=true · 수신자 {harness}(model={model})\n"
        "  · 자식 CLI는 부모 Codex sandbox 의 egress 차단을 상속합니다. 지금 실행하면 API 재시도 뒤 "
        "무진행으로 끝납니다(엔진은 sandbox 를 스스로 벗어나지 않습니다).\n"
        "  · 미리보기는 이 환경에서 그대로 가능합니다(외부 송신 없음): --dry-run\n"
        "  · 실 실행은 두 계층을 **함께** 씁니다 — Codex 도구 호출을 "
        'sandbox_permissions="require_escalated" + 기술적 network justification 으로 올리고, 같은 명령에 '
        f"{CODEX_EGRESS_FLAG} 를 동반하세요(플래그는 권한을 만들지 않는 호출층 attestation).\n"
        f"  · 최초 도구 승인은 {codex_egress_prefix_rule_text(script, windows=windows)} "
        "로 이 진입점만 재사용 승인하세요. Python 전체를 승인하지 마세요.\n"
        f"  · {consent_key}=true 는 설정된 profile의 외부 전송·통상 과금에 대한 "
        "지속 동의입니다. 후속 호출마다 비용을 다시 묻지 마세요.\n"
        f"  · 재실행: {codex_egress_retry_command(argv, script=script, windows=windows)}\n"
        "  · 전역 sandbox_workspace_write.network_access=true 로 우회하지 마세요 — 일상 명령의 "
        "egress 차단은 프로젝트 안전 경계로 유지합니다."
    )


def dry_run_codex_egress_line(*, escalation_required: bool, attested: bool,
                              script: str, consent_key: str,
                              windows: bool | None = None) -> str:
    """dry-run 이 표시하는 승격 필요/불필요 1~2줄(외부 송신·raw·관측 부작용 없음)."""
    if not escalation_required:
        return (
            f"Codex egress: 승격 불필요 ({CODEX_EGRESS_MARKER_ENV} 미설정/false · "
            f"codex_egress={CODEX_EGRESS_NOT_REQUIRED})"
        )
    attestation = (
        f"{CODEX_EGRESS_FLAG} 동반됨 — 실 실행은 이 플래그와 함께 도구 승격도 유지하세요."
        if attested else
        f"실 실행은 {CODEX_EGRESS_FLAG} 없이는 스폰 전 rc=1 로 중단됩니다."
    )
    return (
        f"Codex egress: escalation required ({CODEX_EGRESS_MARKER_ENV}=true)\n"
        '  실행 도구 호출을 sandbox_permissions="require_escalated" + 기술적 network justification 으로 '
        f"올리고, 같은 명령에 {CODEX_EGRESS_FLAG} 를 동반합니다.\n"
        f"  최초에만 {codex_egress_prefix_rule_text(script, windows=windows)} 로 재사용 "
        f"승인하고, {consent_key}=true 후속 호출의 비용은 재질문하지 않습니다.\n  {attestation}"
    )


# ── opencode 첫-이벤트 stall 워치독 (하니스-무관 순수 헬퍼·parse_opencode_json 동거) ──
# opencode `run` 이 스타트업 network fetch stall 에 빠지면 `--format json` stdout 이벤트가 0바이트로
# **영원히** 멈춘다(정상 run 은 첫 이벤트 ~0.2–2초·헝 run 은 240초+ 창 지나도 자체
# 회복 없음·upstream #13841 진단 일치). 호출층에서 이를 닫는다: 첫 stdout 이벤트가 N초 내 안 오면
# 프로세스 그룹째 kill → 재시도(M회) → 소진 시 fail-loud. provider/원인 무관(내부 어느 fetch 가
# 멈추든 동작). mid-turn(정상 긴 생성) 침묵은 이번 범위 밖 — overall_timeout(호출부의 기존 hard
# 가드·예: pm_orch_opencode.TURN_TIMEOUT_SEC=600)이 그대로 백스톱. provider 노브(headerTimeout 등)는
# stall 이 provider 스트림 fetch *밖*에서 발생해 무효 실측() — 워치독이 클래스를 커버한다.

# env 노브 (worktree_pool 의 PM_GIT_TIMEOUT 네이밍 결). 세 표면(opencode driver·pm_import fill·
# release 라이브 헬퍼)이 아래 두 해소기로 이 기본값을 공유한다. 값을 바꾸려면 export 후 재실행.
FIRST_EVENT_TIMEOUT_ENV = "PM_OC_FIRST_EVENT_TIMEOUT"   # 첫-이벤트 대기 상한(초).
STALL_RETRIES_ENV = "PM_OC_STALL_RETRIES"               # stall 시 재시도 횟수.
# 기본 90초 = 느린 cloud 시작 대비 보수적(정상 첫 이벤트 ~0.2–2초). 재시도 기본 2회.
DEFAULT_FIRST_EVENT_TIMEOUT_SEC = 90.0
DEFAULT_STALL_RETRIES = 2
# 워치독 폴 간격·kill 후 grace(자식 잔존 방지·짧게). 매직넘버 회피 상수.
_WATCHDOG_POLL_INTERVAL_SEC = 0.1
_KILL_GRACE_SEC = 5.0
# timeout/stall 정리는 부모 종료 대기와 파이프 EOF drain이 **연속**으로 각각 grace를 다 쓸 수 있다.
# 하네스 상한 계산이 한 단계만 세면 부분 산출물 수확 전에 호출층이 먼저 프로세스를 죽일 수 있다.
_PROCESS_CLEANUP_GRACE_PHASES = 2

# 정리 뒤 분류·raw 박제·범위 감사에 남기는 공용 여유. 두 소비 표면(pm_delegate/external_review)이
# 같은 `harness_cap_required_budget`을 써야 상한 계산 규칙이 갈리지 않는다. 실측 보조 단계 합 7초를
# 플랫폼 편차를 위해 다음 10초 경계로 올린 값이다.
HARNESS_CAP_MEASURED_AUX_BUDGET_SEC = 7
HARNESS_CAP_MARGIN_SEC = (
    (HARNESS_CAP_MEASURED_AUX_BUDGET_SEC + 9) // 10
) * 10


def process_cleanup_budget_per_attempt() -> float:
    """timeout/stall 1회 정리의 최악 예산 — 부모 wait + pipe drain의 연속 grace."""
    return _PROCESS_CLEANUP_GRACE_PHASES * _KILL_GRACE_SEC


def watchdog_execution_budget(
    overall_timeout: float,
    *,
    first_event_timeout: float | None,
    retries: int,
) -> float:
    """공용 워치독 한 번의 최악 실행+정리 예산.

    startup 창이 wall보다 먼저 발화할 때만 실패한 각 startup 시도를 재실행한다. wall이 먼저거나
    동시에 발화하면 첫 시도에서 끝나므로 재시도 예산을 더하지 않는다. startup 창이 꺼진 축도
    전달된 retries를 무시해 기존 단일 시도 계약을 보존한다.
    """
    retry_count = (
        max(0, int(retries))
        if first_event_timeout is not None
        and float(first_event_timeout) < float(overall_timeout)
        else 0
    )
    retry_runtime = (
        retry_count * min(float(first_event_timeout), float(overall_timeout))
        if first_event_timeout is not None else 0.0
    )
    cleanup = (retry_count + 1) * process_cleanup_budget_per_attempt()
    return float(overall_timeout) + retry_runtime + cleanup


def harness_cap_required_budget(execution_budget: float) -> float:
    """엔진 실행+정리 뒤 진단/박제까지 마치기 위한 호출층 최소 상한."""
    return float(execution_budget) + HARNESS_CAP_MARGIN_SEC


def format_partial_output(head: str, exc: BaseException) -> str:
    """실패 진단 뒤에 kill 시점 stdout/stderr를 안전하게 붙인다.

    이미 실패한 timeout/cleanup 경로에서 쓰이므로, 예외 객체의 비정상 속성이나 비-UTF8 bytes가
    원래 진단을 가리는 일은 허용하지 않는다. 문자열 입력의 형식은 기존 소비처 형식을 보존한다.

    | 입력 ``output=b\"ok\\xff\"`` | 이전 external-review | 현재 공용 포맷터 |
    | --- | --- | --- |
    | 표시 | ``b'ok\\xff'`` (repr) | ``ok�`` (UTF-8 replacement decode) |

    pm_import fill은 통합 전에도 replacement decode를 사용했으므로 이 bytes 처리에는 변화가 없다.
    """
    def as_text(value) -> str:
        try:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value) if value else ""
        except Exception:  # noqa: BLE001 - formatter failure must not erase the diagnosis.
            return "[부분 산출물 표시 불가]"

    try:
        stdout = as_text(getattr(exc, "output", ""))
        stderr = as_text(getattr(exc, "stderr", ""))
        parts = [head]
        if stdout:
            parts.append(f"\n[중단 시점까지의 stdout — {len(stdout)}자 보존]\n{stdout}")
        if stderr:
            parts.append(f"\n[중단 시점까지의 stderr — {len(stderr)}자 보존]\n{stderr}")
        return "".join(parts)
    except Exception:  # noqa: BLE001 - `head` itself may be a hostile str subclass.
        return head if isinstance(head, str) else "[실패 진단]"


def harness_cap_advisory(
    env: dict[str, str], *, execution_budget: float,
    session_markers: dict[str, tuple[str, ...]], cap_env: dict[str, str | None],
    render_missing, render_invalid, render_low,
) -> str | None:
    """세 실행 표면이 공유하는 호출층 상한 판정; 표면별 기존 문구는 renderer가 소유한다."""
    required = int(harness_cap_required_budget(execution_budget))
    warnings = []
    for harness, markers in session_markers.items():
        if not any(env.get(marker) for marker in markers):
            continue
        cap_key = cap_env.get(harness)
        if cap_key is None:
            continue
        raw = env.get(cap_key)
        if raw is None and render_missing is not None:
            warnings.append(render_missing(harness, cap_key, required))
            continue
        try:
            cap_seconds = int(raw) / 1000.0
        except (TypeError, ValueError, OverflowError):
            warnings.append(render_invalid(harness, cap_key, raw, required))
            continue
        if not math.isfinite(cap_seconds) or cap_seconds < required:
            warnings.append(render_low(harness, cap_key, cap_seconds, required))
    return "\n".join(warnings) or None


class StallWatchdogError(RuntimeError):
    """첫-이벤트 워치독이 모든 재시도를 소진(반복 startup stall) → fail-loud.

    헬퍼는 raise 만 하고 *정책은 호출부가* 정한다:
      - opencode driver `_turn`: catch → loud stderr + fail-soft turn(None,None·relay 루프 생존).
      - pm_import fill(`_real_harness_runner`): catch → (False, 에러텍스트)(기존 fail-soft 계약).
      - release 라이브 헬퍼: uncatch → 테스트 fail-loud(라이브 환경 문제 가시화).
    """

    def __init__(self, message: str, *, timeout_axis: str | None = None,
                 threshold_seconds: float | None = None,
                 silence_seconds: float | None = None,
                 output: str | None = None, stderr: str | None = None) -> None:
        super().__init__(message)
        self.timeout_axis = timeout_axis
        self.threshold_seconds = threshold_seconds
        self.silence_seconds = silence_seconds
        self.output = output or ""
        self.stderr = stderr or ""


class ProcessCleanupError(RuntimeError):
    """프로세스 그룹 종료/파이프 drain 실패 — 부분 산출물을 싣는 loud sentinel."""

    def __init__(self, message: str, *, output: str | None = None,
                 stderr: str | None = None) -> None:
        super().__init__(message)
        self.process_cleanup_failed = True
        self.output = output or ""
        self.stderr = stderr or ""


def _env_positive_float(name: str, default: float) -> float:
    """env 노브를 양수 float 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_nonneg_int(name: str, default: int) -> int:
    """env 노브를 음이 아닌 int 로 해소(빈/불량/음수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_positive_int(name: str, default: int) -> int:
    """env 노브를 양의 int 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def first_event_timeout_default() -> float:
    """PM_OC_FIRST_EVENT_TIMEOUT env(초) 또는 기본 90. 세 표면 공유 해소기."""
    return _env_positive_float(FIRST_EVENT_TIMEOUT_ENV, DEFAULT_FIRST_EVENT_TIMEOUT_SEC)


def stall_retries_default() -> int:
    """PM_OC_STALL_RETRIES env 또는 기본 2. 세 표면 공유 해소기."""
    return _env_nonneg_int(STALL_RETRIES_ENV, DEFAULT_STALL_RETRIES)


# ── 무진행(idle) 판정 — 벽시계 단독 판정의 false-kill 폐쇄 ──────────────────────
# 외부 프로세스를 "시작 후 경과 시간"으로 죽이면 **정상 진행 중인 작업**이 잘린다. 값 튜닝으로는
# 못 닫는다 — 동일 입력이 900초를 넘기도 하고 안 넘기도 한 직접 증거(외부 리뷰 채널 실측 2회
# 실행·입력 동일·한 번은 kill/한 번은 성공)가 임계값이 정상 작업의 **분산 대역 안**에 있음을 보였다.
# 그래서 판정 기준을 **마지막 진행 이벤트 이후 무진행 시간**으로 교체하고 벽시계는 백스톱으로
# 강등한다(worktree_pool.py:84-100 "진행이 보이면 관대하게, 안 보이면 유한하게"의 승계).
# ── 시간 예산 실측 근거 (추측 상수 금지) ────────────────────────────────────────────
# **클라우드 축**(codex·claude — 원격 추론). 재료 = /tmp 잔존 위임 raw 코퍼스(pm_delegate_codex_*
# 5,100건·pm_delegate_claude_* 749건) + external_review raw(codex 평문 축 207건).
#   ⓐ 도구 실행 침묵(직접 실측·N=860 표본/89 파일) — codex 는 command_execution 을 item.started →
#      (그 도구가 도는 동안 stdout 이벤트 0) → item.completed 로 내므로 **한 도구 실행의 소요 =
#      그 구간의 stdout 침묵**이다. 도구가 스스로 찍은 소요(pytest "in NNNs")를 표본으로:
#      p50 0.5s · p90 82.6s · p95 102.7s · p99 124.9s · **max 254.6s**.
#   ⓑ 이벤트 간 평균 간격(비둘기집 하한·rc=0 완주 153건) — 총 소요/이벤트 수 ≤ 최대 간격:
#      p50 9.0s · p99 16.4s · max 17.7s. (총 소요 자체는 p50 370.2s·p99 1036.6s·max 1429.1s.)
#   ⓒ 평문 축(external_review 의 `codex exec` — `--json` 없음) — 진행 로그가 **stderr** 로 촘촘히
#      흐른다(리뷰 1건 stderr 12,233줄·hook/exec/succeeded 라인이 도구마다). stdout 은 최종 회신
#      뿐이라(실측 498~759 바이트) **stdout 단독 관측은 이 축을 전량 false-kill 한다** — 그래서
#      진행 신호는 stdout·stderr **양쪽 chunk 도착**으로 본다.
CLOUD_IDLE_TIMEOUT_SEC = 900.0    # 관측 최대 침묵(254.6s)의 3.5배.
CLOUD_WALL_TIMEOUT_SEC = 3600.0   # 관측 최대 완주(1429.1s)의 2.5배 — 백스톱이라 여유를 크게.

# **로컬 GPU 축**(opencode — 로컬 모델 추론). 위 코퍼스에 opencode 표본은 **0건**이라 값을 클라우드
# 측정치로 잡으면 안 된다(초기 구현이 그렇게 했다가 전제가 깨졌다). 근거는 사용자 실측 증언이다:
# 회사 환경(로컬 GPU 부족)의 opencode 위임은 **3시간까지** 걸리고, 관찰된 진행 양상은
# **"길게 멈췄다 가끔 움직임"** 이다 — 즉 총 소요만 긴 게 아니라 **침묵 구간 자체가 길다**.
# 클라우드 값(idle 900s)으로는 그 정상 작업을 죽인다.
#   · wall 4시간 = 관측된 3시간 완주 + 33% 여유(요구: "3시간 위").
#   · idle 1.5시간 = 3시간 실행에서 한 침묵이 전체의 절반에 달해도 통과. 이 이상 조용하면 총 소요의
#     절반을 무출력으로 보낸 것이라 정지로 본다. 하드 표본이 없는 축이라 **설정 안 해도 안 죽는
#     쪽**으로 잡았다 — 미설정 채택자의 false-kill 이 이 판정 전환의 실패 조건이기 때문이다.
#     GPU 가 넉넉한 배포는 환경 조건이 다르므로 local.conf `harness.opencode.*` 로 조인다.
LOCAL_GPU_IDLE_TIMEOUT_SEC = 5400.0    # 1.5시간
LOCAL_GPU_WALL_TIMEOUT_SEC = 14400.0   # 4시간

# ── 드라이버 진행-신호 능력 선언 (분기 특례가 아니라 테이블) ────────────────────────
# 무진행 판정은 "드라이버가 살아있음을 스트림으로 알릴 수 있는가"에만 의존한다. 하니스별 if 분기를
# 만들지 않고 **능력을 선언**한다 — 신호를 못 내는 드라이버가 생기면 선언만 바꾸면 된다.
PROGRESS_SIGNAL_EVENT_STREAM = "event-stream"  # 줄 단위 이벤트(codex --json·opencode --format json·claude stream-json)
PROGRESS_SIGNAL_PLAINTEXT = "plaintext"        # 평문 증분(codex exec 기본 — 진행 로그가 stderr 로 흐름)
PROGRESS_SIGNAL_NONE = "none"                  # 종료 시 단일 덩어리 — 무진행 판정 불가 → 벽시계 유지

PROGRESS_SIGNAL_KINDS: frozenset[str] = frozenset(
    {PROGRESS_SIGNAL_EVENT_STREAM, PROGRESS_SIGNAL_PLAINTEXT, PROGRESS_SIGNAL_NONE}
)

# 호출층 Bash 상한을 관측할 수 있는 세션 축의 단일 선언. 공개 상한이 없는 codex는 None으로
# 남기며, 그 자체는 advisory 대상이 아니다.
HARNESS_SESSION_MARKERS: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_THREAD_ID", "CODEX_CI"),
    "claude": ("CLAUDECODE",),
    "opencode": ("OPENCODE", "OPENCODE_PID"),
}
HARNESS_CAP_ENV: dict[str, str | None] = {
    "codex": None,
    "claude": "BASH_MAX_TIMEOUT_MS",
    "opencode": "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
}


class HarnessProfile(NamedTuple):
    """하네스 축 선언 — 관측 능력 + 시간 예산. **값만 갈리고 판정 코드는 하나다.**

    "하네스별 특례 분기 금지"의 금지 대상은 *코드 분기*이고, 드라이버별 *선언된 값*은 이 테이블의
    존재 이유다. 코드가 갈리면(하네스 이름으로 판정 경로가 바뀌면) 그건 위반이다."""

    progress_signal: str      # PROGRESS_SIGNAL_* — 무진행 판정 가능 여부
    startup_watchdog: bool    # 첫-stdout-이벤트 창 + 유한 재시도 적용 여부
    idle_timeout: float       # 무진행 상한(주 판정·초)
    wall_timeout: float       # 벽시계 백스톱(초)


HARNESS_PROFILES: dict[str, HarnessProfile] = {
    # 클라우드 축 — 원격 추론이라 침묵/완주 모두 짧다(위 ⓐⓑ 실측).
    "codex": HarnessProfile(PROGRESS_SIGNAL_EVENT_STREAM, False,
                            CLOUD_IDLE_TIMEOUT_SEC, CLOUD_WALL_TIMEOUT_SEC),
    "claude": HarnessProfile(PROGRESS_SIGNAL_EVENT_STREAM, False,
                             CLOUD_IDLE_TIMEOUT_SEC, CLOUD_WALL_TIMEOUT_SEC),
    # 로컬 GPU 축 — 긴 침묵 + 장시간 완주(사용자 실측 증언). startup stall 워치독은 이 축에서만
    # 실측된 결함(upstream #13841)이라 여기만 True 다.
    "opencode": HarnessProfile(PROGRESS_SIGNAL_EVENT_STREAM, True,
                               LOCAL_GPU_IDLE_TIMEOUT_SEC, LOCAL_GPU_WALL_TIMEOUT_SEC),
}

# 프로필을 모르는 축의 기본 = **테이블에 선언된 값 중 가장 관대한 쪽**(false-kill 방향 금지).
# 특정 프로필 상수의 alias 로 두면 더 관대한 축을 추가할 때 조용히 stale 해지므로 반드시 테이블에서
# 파생한다. 이 두 이름은 기존 호출자의 공개 호환 표면이라 유지하되 값의 소유권은 HARNESS_PROFILES 다.
DEFAULT_IDLE_TIMEOUT_SEC = max(profile.idle_timeout for profile in HARNESS_PROFILES.values())
DEFAULT_WALL_TIMEOUT_SEC = max(profile.wall_timeout for profile in HARNESS_PROFILES.values())

# 미지 하네스(설정 오류·신규 추가 누락) — 진행 신호를 **모르므로** 무진행 판정 대상이 아니고
# (모르는 드라이버를 무진행으로 죽이지 않는다), 벽시계는 가장 관대한 선언값으로 유한하게 남는다.
UNKNOWN_HARNESS_PROFILE = HarnessProfile(PROGRESS_SIGNAL_NONE, False,
                                         DEFAULT_IDLE_TIMEOUT_SEC, DEFAULT_WALL_TIMEOUT_SEC)

# 미지 **리뷰어** 커맨드 — 출력 형식을 모르므로 진행 신호가 있다고 가정하지 않는다. 종료 시 한 번에
# 출력하는 CLI를 평문 증분 축으로 오인하면 정상 침묵 구간이 곧 idle kill 대상이 된다. 모를 때
# 신호 없음(벽시계만)이 false-kill에 안전하고, 값은 가장 관대한 쪽으로 유한하게 남긴다.
REVIEWER_FALLBACK_PROFILE = HarnessProfile(PROGRESS_SIGNAL_NONE, False,
                                           DEFAULT_IDLE_TIMEOUT_SEC, DEFAULT_WALL_TIMEOUT_SEC)

# local.conf 하네스별 override 키 — GPU 여유/네트워크 같은 **환경 조건**은 엔진 속성이 아니라
# 배포 속성이라 per-clone 으로 받는다. 두 표면(위임·외부리뷰)이 같은 키를 읽는다.
HARNESS_IDLE_TIMEOUT_KEY = "harness.{harness}.idle_timeout"
HARNESS_WALL_TIMEOUT_KEY = "harness.{harness}.wall_timeout"


def normalize_timeout_seconds(raw, *, minimum: int = 1) -> int | None:
    """외부 timeout 입력을 **유한한 정수 초**로 정규화한다.

    CLI·local.conf 등 문자열/수치 입력의 단일 경계다. bool·NaN·±inf·분수·최솟값 미만은 None 으로
    거부한다. 소비처에서 `int()` 로 다시 자르지 않으므로 0.5→0초, int(nan/inf) 예외,
    `nan` 비교 우회로 idle watchdog 비활성화가 모두 같은 원인에서 닫힌다.
    """
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not value.is_integer():
        return None
    integer = int(value)
    return integer if integer >= minimum else None


def _conf_positive_float(conf: dict, key: str | None) -> float | None:
    """local.conf timeout 을 정수 초로 해소(미설정/깨진 값은 None + 경고·실행을 막지 않는다)."""
    if not key:
        return None
    raw = (conf.get(key) or "").strip()
    if not raw:
        return None
    value = normalize_timeout_seconds(raw)
    if value is None:
        sys.stderr.write(
            f"경고: local.conf {key}={raw!r} 은 유한한 정수 초(최소 1)가 아님 "
            "— 선언 기본값 사용.\n"
        )
        return None
    return float(value)


def resolve_harness_profile(harness: str, conf: dict | None = None, *,
                            fallback: HarnessProfile | None = None,
                            legacy_idle_key: str | None = None,
                            legacy_wall_key: str | None = None) -> HarnessProfile:
    """하네스 → 시간 예산이 해소된 프로필. **두 표면이 이 한 함수를 쓴다.**

    해소 순서(뒤가 이긴다): 선언 기본 → 표면-flat legacy 키(`delegate_timeout`·
    `external_review_timeout` 등 기존 노브) → 하네스별 키(`harness.<name>.*`). CLI 일회성 override 는
    호출부가 이 결과 위에 얹는다(가장 강함). 미지 하네스는 `fallback`(없으면 UNKNOWN_HARNESS_PROFILE).
    """
    conf = conf or {}
    profile = HARNESS_PROFILES.get(harness) or fallback or UNKNOWN_HARNESS_PROFILE
    idle, wall = profile.idle_timeout, profile.wall_timeout
    for key in (legacy_idle_key, HARNESS_IDLE_TIMEOUT_KEY.format(harness=harness)):
        value = _conf_positive_float(conf, key)
        if value is not None:
            idle = value
    for key in (legacy_wall_key, HARNESS_WALL_TIMEOUT_KEY.format(harness=harness)):
        value = _conf_positive_float(conf, key)
        if value is not None:
            wall = value
    return profile._replace(idle_timeout=idle, wall_timeout=wall)


def max_declared_wall_timeout() -> float:
    """선언된 하네스 wall 백스톱 중 최댓값 — 하네스 Bash 상한 부등식의 좌변(단일 출처)."""
    return max(profile.wall_timeout for profile in HARNESS_PROFILES.values())


def idle_timeout_for_signal(progress_signal: str,
                            configured: float | None = None) -> float | None:
    """진행-신호 선언 → 적용할 무진행 상한(초). 신호 없는 축은 None(=벽시계 유지).

    configured=None 이면 테이블 파생 기본을 쓴다. 미지의 선언은 **보수적으로** 신호 없음으로
    취급한다(모르는 드라이버를 무진행으로 죽이지 않는다 — false-kill 방향 금지)."""
    if progress_signal not in PROGRESS_SIGNAL_KINDS or progress_signal == PROGRESS_SIGNAL_NONE:
        return None
    value = DEFAULT_IDLE_TIMEOUT_SEC if configured is None else configured
    normalized = normalize_timeout_seconds(value)
    if normalized is None:
        raise ValueError(f"idle_timeout={value!r}: 유한한 정수 초(최소 1) 필요")
    return float(normalized)


def _kill_process_group(proc, process_group_id: int | None = None) -> None:
    """저장된 process-group ID를 부모 종료 여부와 무관하게 kill한다.

    부모 `poll()`은 그룹 생존 판정이 아니다. 부모가 rc=0으로 먼저 끝나도 자식이 파이프를 쥔 채
    남을 수 있으므로 POSIX에서는 생성 직후 저장한 pgid로 항상 `killpg`한다. ESRCH는 그룹이 이미
    사라진 성공 상태지만 권한/플랫폼 오류와 kill 후 부모 wait 실패는 조용히 삼키지 않는다.
    """
    if os.name == "posix":
        pgid = process_group_id
        if pgid is None:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                return
            except OSError as exc:
                raise ProcessCleanupError(
                    f"프로세스 그룹 ID 해소 실패(pid={proc.pid}): {exc}"
                ) from exc
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProcessCleanupError(
                f"프로세스 그룹 kill 실패(pgid={pgid}): {exc}"
            ) from exc
    elif proc.poll() is None:  # pragma: no cover — POSIX 회귀 환경
        try:
            proc.kill()
        except OSError as exc:
            raise ProcessCleanupError(f"프로세스 kill 실패(pid={proc.pid}): {exc}") from exc

    if proc.poll() is None:
        try:
            proc.wait(timeout=_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired as exc:
            raise ProcessCleanupError(
                f"프로세스 그룹 kill 후 부모가 {_KILL_GRACE_SEC:g}s 안에 종료하지 않음"
            ) from exc


def _cleanup_failed_watched_spawn(proc, process_group_id: int | None, *, argv,
                                  threads: list[threading.Thread]) -> None:
    """`Popen` 성공 뒤 어댑터 초기화 실패를 그룹 kill + pipe drain으로 롤백한다."""
    _kill_process_group(proc, process_group_id)
    try:
        proc.communicate(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired as exc:
        raise ProcessCleanupError(
            f"프로세스 생성 후 초기화 실패 정리 중 파이프가 "
            f"{_KILL_GRACE_SEC:g}s 안에 닫히지 않음: {argv!r}",
            output=exc.output, stderr=exc.stderr,
        ) from exc
    except (OSError, ValueError) as exc:
        raise ProcessCleanupError(
            f"프로세스 생성 후 초기화 실패 정리 중 파이프 drain 실패: {argv!r}: {exc}"
        ) from exc

    # Thread.start()가 성공한 뒤 다음 초기화 단계가 실패한 경우 reader/writer까지 회수한다.
    # 시작 전 Thread.join()은 RuntimeError이므로 ident가 생긴 스레드만 기다린다.
    for thread in threads:
        if thread.ident is None:
            continue
        thread.join(timeout=_KILL_GRACE_SEC)
        if thread.is_alive():
            raise ProcessCleanupError(
                f"프로세스 생성 후 초기화 실패 정리 중 I/O 스레드가 "
                f"{_KILL_GRACE_SEC:g}s 안에 종료하지 않음: {argv!r}"
            )


SPAWN_FAILED_ATTR = "spawn_failed"


def _mark_spawn_failed(exc: BaseException, failed: bool) -> None:
    """예외에 "자식이 떴는가" 표식을 붙인다 — 이미 붙어 있으면 그대로 둔다(최초 관측이 진실).

    표식의 소유자는 **스폰 경계를 실제로 본 층**이다: `_WatchedPopen` 은 실 `Popen` 호출의 앞뒤를
    보고 preflight 거절(NUL 입력)과 확정 기동 실패에 True(자식 없음)를, 그 밖의 `Popen` 예외와
    `Popen` 반환 뒤의 실패에 False(자식 있었음·보수)를 붙이고, 워치독 루프는 표식 없는 주입
    어댑터의 exec 실패(OSError)에 True 를 붙인다.
    상위(추가 리뷰어 러너)는 이 표식이 있으면 그것을, 없으면 예외 종류만 보고 보수적으로 판정한다.
    속성을 못 받는 예외(슬롯 정의)는 조용히 넘긴다 — 표식 없음은 '판정 유보'라 안전쪽이다.
    """
    if getattr(exc, SPAWN_FAILED_ATTR, None) is not None:
        return
    try:
        setattr(exc, SPAWN_FAILED_ATTR, failed)
    except (AttributeError, TypeError):
        pass


# `Popen` 이 던졌을 때 "자식이 뜬 적 없음"을 **확정**할 수 있는 종류. exec 자체가 실패한 형상들이고
# (파일 부재·실행 권한·경로 형상), CPython 은 이때 자기가 만든 자식을 errpipe 로 확인한 뒤 회수한다.
# external_review 의 `_DEFINITE_LAUNCH_FAILURES` 와 같은 집합이다(양쪽 테스트가 동치를 못박는다).
_DEFINITE_LAUNCH_FAILURES = (
    FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError,
)

# 문자열/경로/환경값에 섞이면 fork/exec **전에** 거절이 확정되는 바이트.
_NUL = "\x00"


def _nul_in(value) -> bool:
    """문자열·바이트·PathLike 어디에 실려 오든 embedded NUL 을 본다(판정 불가면 False)."""
    if isinstance(value, (bytes, bytearray)):
        return b"\x00" in value
    if isinstance(value, str):
        return _NUL in value
    try:
        return _NUL in os.fspath(value)
    except (TypeError, ValueError):
        return False


def _preflight_spawn_inputs(argv, *, cwd, env) -> None:
    """fork/exec **전에** 확정 판정할 수 있는 입력만 명시적으로 거절한다 (embedded NUL).

    `Popen` 이 던졌다는 사실만으로는 자식 유무를 확정할 수 없다 — 그 생성자는 fork/exec 앞의 인자
    검증도, 자식이 뜬 **뒤**의 부모측 초기화도 같은 자리에서 올린다. 그래서 "전송 0" 판정의 근거를
    예외를 받는 자리가 아니라 **부르기 전**으로 옮긴다: argv·cwd·env 의 NUL 은 이 함수가 보고
    거절하므로 그 거절에 붙는 '자식 없음' 표식은 관측이지 추측이 아니다(환불 계약 보존).

    검사 범위는 *확정 가능한 것* 뿐이다 — 여기서 통과한 입력이 `Popen` 안에서 무엇으로 실패하든
    그 판정은 호출부의 보수 규칙이 맡는다(넓히면 다시 추측이 된다).
    """
    for index, item in enumerate(argv or ()):
        if _nul_in(item):
            _reject_pre_child(f"argv[{index}]")
    if cwd is not None and _nul_in(cwd):
        _reject_pre_child("cwd")
    for key, value in (env or {}).items():
        if _nul_in(key):
            _reject_pre_child(f"env key {key!r}")
        if _nul_in(value):
            _reject_pre_child(f"env[{key!r}]")


def _reject_pre_child(where: str):
    """NUL 입력 거절 — `Popen` 과 같은 종류(`ValueError`)에 '자식 없음' 표식을 붙여 올린다."""
    exc = ValueError(f"embedded null byte: {where}")
    _mark_spawn_failed(exc, True)
    raise exc


def _mark_child_existed(exc: BaseException) -> None:
    """이 **실행**에 자식이 한 번이라도 있었음을 못박는다 — 기존 표식도 덮는다.

    `_mark_spawn_failed` 는 최초 관측이 이긴다(같은 시도 안에서 층끼리 서로 덮지 않게). 이 선언은
    시도를 가로지르는 상위 사실이라 그 규칙 위에 선다: 앞 시도가 자식을 띄웠다면 뒤 시도가 exec 에
    실패해도 실행 전체는 '자식 없음'이 아니다(앞 자식이 프롬프트를 이미 보냈을 수 있다). 방향은
    True→False 한쪽뿐이라 단조롭고, 그 쪽이 보수적(전송됐을 수 있음)이다.
    """
    try:
        setattr(exc, SPAWN_FAILED_ATTR, False)
    except (AttributeError, TypeError):
        pass


class _WatchedPopen:
    """실 subprocess.Popen 을 감싸 첫-stdout-이벤트 관측 + 진행(chunk) 관측 + 프로세스그룹 kill 제공.

    reader 스레드가 stdout/stderr 를 **실제 도착 chunk 단위**로 읽어 누적한다. stdout 의 완성된
    비어있지-않은 라인은 별도 line buffer 로 파싱해 first_event 를 set 한다(startup stall = 첫
    이벤트 라인조차 영원히 안 옴). stall 시 메인 루프가 kill 하면 blocking read 가 EOF 로 풀린다.
    stderr 도 별도 스레드로 드레인(파이프 버퍼 데드락 방지).
    fake(테스트)로 대체 가능한 얇은 어댑터 — 워치독 로직은 run_with_first_event_watchdog 이 쥔다.

    **진행 관측**: stdout·stderr **어느 쪽이든** chunk 가 도착한 시각을 `last_event_at()`
    로 노출한다(무진행 판정의 입력). 양쪽을 보는 이유는 실측이다 — `codex exec` 평문 축은 진행
    로그가 전부 stderr 로 흐르고 stdout 은 최종 회신뿐이라, stdout 만 보면 정상 진행을 무진행으로
    오판한다. `first_event_ready()` 는 **stdout 전용 그대로** 둔다(opencode startup stall 의미론
    보존).

    **stdin 주입**: `input_text` 를 주면 stdin=PIPE 로 열고 별도 스레드가 전문을 쓴 뒤 **닫는다**
    (EOF 전달 — codex/claude 는 stdin EOF 까지 프롬프트를 읽는다). None 이면 DEVNULL 로 닫아
    supervisor stdin 을 상속하지 않는다(relay 사용자 입력을 child 가 삼키는 fail-silent 방지).
    """

    def __init__(self, argv, *, cwd=None, env=None, text=True,
                 input_text: str | None = None, clock=None):
        # fork/exec 전에 확정 판정 가능한 입력(NUL)은 여기서 거절한다 — 그 거절의 '자식 없음'
        # 표식은 `Popen` 예외 종류를 보고 추측한 것이 아니라 경계 **앞**의 관측이다.
        _preflight_spawn_inputs(argv, cwd=cwd, env=env)
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
        )
        popen_kwargs["stdin"] = (
            subprocess.PIPE if input_text is not None else subprocess.DEVNULL
        )
        if text:
            popen_kwargs.update(text=True, encoding="utf-8", errors="replace")
        # 프로세스 그룹 분리 — kill 시 자식(모델 fetch 서브프로세스 등)까지 그룹째 정리.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover — POSIX 회귀 환경
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._clock = clock if clock is not None else time.monotonic
        spawned = None
        io_threads: list[threading.Thread] = []
        try:
            # 실제 자식 생성도 try 안에 둔다. Popen이 핸들을 반환한 직후부터 아래 후속 초기화
            # (Event/Lock/Thread 생성·start)가 실패해도 같은 트랜잭션에서 자식을 회수한다.
            try:
                spawned = subprocess.Popen(argv, **popen_kwargs)
            except _DEFINITE_LAUNCH_FAILURES as launch_failure:
                # exec 자체가 실패한 확정 기동 실패다 — CPython 은 이때 자기가 만든 자식을
                # errpipe 로 확인하고 회수하며, 프롬프트를 밀어 넣는 stdin 펌프는 `Popen` 이
                # 돌아온 **뒤에야** 시작한다. 전송 0·과금 0 이므로 환불 계약을 유지한다.
                _mark_spawn_failed(launch_failure, True)
                raise
            except Exception as unknown_boundary:
                # `Popen` 이 던졌다는 사실만으로는 자식 유무를 확정할 수 없다. 이 생성자는
                # fork/exec 앞의 인자 검증(그건 위 preflight 가 이미 걷어냈다)과 자식이 뜬
                # **뒤**의 부모측 초기화(errpipe 읽기·fd 정리)를 같은 자리에서 올린다. 확정할 수
                # 없는 나머지는 보수쪽으로 못박는다 — '자식 있었음'(False)이면 최악의 경우
                # 라운드를 한 번 더 세지만, 반대로 틀리면 이미 나간 전송이 예산에서 사라진다.
                # `BaseException`(Ctrl-C 등)은 표식 없이 그대로 올린다: 판정 유보 = 보수쪽이다.
                _mark_spawn_failed(unknown_boundary, False)
                raise
            self._proc = spawned
            self._argv = argv
            # start_new_session=True이므로 POSIX 그룹장은 곧 pid다. 부모 종료 후에는 getpgid(pid)를
            # 해소할 수 없으므로 생성 시점 값을 저장하는 것이 잔존 자식 정리의 load-bearing 계약이다.
            self._process_group_id = self._proc.pid if os.name == "posix" else None
            self._first_event = threading.Event()
            self._stdout_chunks: list[str] = []
            self._stderr_chunks: list[str] = []
            # 리더 스레드(2)와 메인 루프가 공유하는 상태(chunk 누적·마지막 도착 시각)의 단일 락.
            # 실제 read chunk 당 1회 획득이라 비용은 무시 가능하고, 스냅샷이 append 와 경합하지 않는다.
            self._progress_lock = threading.Lock()
            self._last_event_at: float | None = None
            self._stdout_reader = threading.Thread(target=self._pump_stdout, daemon=True)
            self._stderr_reader = threading.Thread(target=self._pump_stderr, daemon=True)
            io_threads.extend((self._stdout_reader, self._stderr_reader))
            self._stdin_writer: threading.Thread | None = None
            if input_text is not None:
                self._stdin_writer = threading.Thread(
                    target=self._pump_stdin, args=(input_text,), daemon=True
                )
                io_threads.append(self._stdin_writer)
                self._stdin_writer.start()
            self._stdout_reader.start()
            self._stderr_reader.start()
        except BaseException as primary:
            if spawned is not None:
                # 자식이 **실제로 떴다** — 뒤이은 초기화 실패는 '스폰 전'이 아니다. 회수는 하되
                # 그 사실을 예외에 표식으로 남긴다(호출부가 exec 실패와 구분해 전송 가능성을
                # 보수적으로 판정한다). 표식을 못 붙이는 예외는 표식 없음 = 판정 유보다.
                _mark_spawn_failed(primary, False)
                process_group_id = spawned.pid if os.name == "posix" else None
                try:
                    _cleanup_failed_watched_spawn(
                        spawned, process_group_id, argv=argv, threads=io_threads
                    )
                except Exception as cleanup_error:
                    # 원래 KeyboardInterrupt/SystemExit/초기화 오류의 identity를 유지한다.
                    raise primary from cleanup_error
            raise

    def _pump_stdin(self, text: str) -> None:
        """프롬프트 전문을 stdin 에 쓰고 **닫는다**(EOF). 파이프 파손은 삼킨다(자식 조기 종료)."""
        stream = self._proc.stdin
        if stream is None:
            return
        try:
            stream.write(text)
            stream.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def _note_arrival(self, chunks: list[str], text: str) -> None:
        """실제 byte chunk 도착 시각 갱신 + 디코딩된 텍스트 누적(단일 락).

        UTF-8 다중바이트 문자의 중간 조각은 디코더가 아직 텍스트를 못 내도 **바이트는 도착한
        것**이므로 시각은 갱신한다. 진행 판정은 텍스트/라인 완성 여부와 무관하다."""
        with self._progress_lock:
            if text:
                chunks.append(text)
            self._last_event_at = self._clock()

    @staticmethod
    def _read_chunk(stream):
        """파이프에서 지금 도착한 chunk 를 읽는다 — 줄 경계를 기다리지 않는다."""
        raw_stream = getattr(stream, "buffer", stream)
        read1 = getattr(raw_stream, "read1", None)
        return read1(4096) if callable(read1) else raw_stream.read(4096)

    def _pump_stream(self, stream, chunks: list[str], *, stdout: bool) -> None:
        if stream is None:
            if stdout:
                self._first_event.set()
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending_line = ""
        try:
            while True:
                raw = self._read_chunk(stream)
                if raw in (b"", ""):
                    break
                text = decoder.decode(raw) if isinstance(raw, bytes) else raw
                self._note_arrival(chunks, text)
                if stdout:
                    pending_line += text
                    while "\n" in pending_line:
                        line, _newline, pending_line = pending_line.partition("\n")
                        if line.strip():
                            self._first_event.set()
            tail = decoder.decode(b"", final=True)
            if tail:
                with self._progress_lock:
                    chunks.append(tail)
                pending_line += tail
            if stdout and pending_line.strip():
                self._first_event.set()  # EOF 로 끝난 마지막 비개행 이벤트 라인.
        except (ValueError, OSError):
            pass
        finally:
            if stdout:
                self._first_event.set()  # EOF/읽기 종료(빈 출력 포함) — startup 대기 해제.

    def _pump_stdout(self) -> None:
        self._pump_stream(self._proc.stdout, self._stdout_chunks, stdout=True)

    def _pump_stderr(self) -> None:
        self._pump_stream(self._proc.stderr, self._stderr_chunks, stdout=False)

    def first_event_ready(self) -> bool:
        return self._first_event.is_set()

    def last_event_at(self) -> float | None:
        """마지막 chunk(stdout/stderr) 도착 시각(단조 시계) — 한 번도 없으면 None.

        무진행 판정의 유일한 입력. 리더 스레드와 같은 락으로 읽어 경합-안전하다."""
        with self._progress_lock:
            return self._last_event_at

    def partial_output(self) -> tuple[str, str]:
        """지금까지 받은 (stdout, stderr) 스냅샷 — kill 시점 부분 산출물 보존용(전량 폐기 폐쇄)."""
        with self._progress_lock:
            return "".join(self._stdout_chunks), "".join(self._stderr_chunks)

    def poll(self):
        return self._proc.poll()

    def drain_complete(self) -> bool:
        """부모 종료 + stdin 전달 + stdout/stderr EOF가 모두 끝났을 때만 실행 완료."""
        return (
            self._proc.poll() is not None
            and (self._stdin_writer is None or not self._stdin_writer.is_alive())
            and not self._stdout_reader.is_alive()
            and not self._stderr_reader.is_alive()
        )

    def kill(self) -> None:
        _kill_process_group(self._proc, self._process_group_id)

    def communicate(self, timeout=None):
        """전체 deadline 동안 부모 **및 파이프 EOF**를 기다린다."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        self._proc.wait(timeout=remaining())
        if self._stdin_writer is not None:
            self._stdin_writer.join(timeout=remaining())
        self._stdout_reader.join(timeout=remaining())
        self._stderr_reader.join(timeout=remaining())
        if (
            (self._stdin_writer is not None and self._stdin_writer.is_alive())
            or self._stdout_reader.is_alive()
            or self._stderr_reader.is_alive()
        ):
            stdout, stderr = self.partial_output()
            raise subprocess.TimeoutExpired(
                self._argv, timeout, output=stdout, stderr=stderr
            )
        return self.partial_output()

    @property
    def returncode(self):
        return self._proc.returncode


def _default_watchdog_log(message: str) -> None:
    """워치독 loud 1줄 sink(기본 stderr — stdout 은 PM 대화 채널 보존)."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


TIMEOUT_AXIS_IDLE = "idle"
TIMEOUT_AXIS_WALL = "wall"
TIMEOUT_AXIS_FIRST_EVENT = "first-event"
TIMEOUT_AXIS_ATTR = "timeout_axis"
TIMEOUT_THRESHOLD_ATTR = "threshold_seconds"


class WatchdogTimeoutExpired(subprocess.TimeoutExpired):
    """공용 워치독 중단의 구조화된 진단 계약.

    소비자는 호출 시 인자로 받았던 값(하네스별 해소 전/primary 값일 수 있음)을 다시 추정하지 않고,
    예외가 싣는 **실제 발화 축·임계·중단 시 실측 침묵**을 읽는다."""

    def __init__(self, cmd, timeout, *, timeout_axis: str,
                 silence_seconds: float | None = None,
                 output=None, stderr=None) -> None:
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.timeout_axis = timeout_axis
        self.threshold_seconds = float(timeout)
        self.silence_seconds = silence_seconds


class IdleTimeoutExpired(WatchdogTimeoutExpired):
    """무진행(마지막 출력 이후 침묵) 판정으로 중단 — 벽시계 초과와 구분되는 sentinel.

    `subprocess.TimeoutExpired` 를 **상속** 해 기존 호출부의 `except subprocess.TimeoutExpired`
    가 그대로 잡는다(분류/폴백 경로 회귀 0). 구분이 필요한 지점(감사 헤더·실패 메시지)은
    `idle_seconds`(실측 침묵 초) 속성 유무로 본다."""

    def __init__(self, cmd, timeout, *, idle_seconds: float,
                 output=None, stderr=None) -> None:
        super().__init__(
            cmd, timeout, timeout_axis=TIMEOUT_AXIS_IDLE, silence_seconds=idle_seconds,
            output=output, stderr=stderr,
        )
        self.idle_seconds = idle_seconds


class WallTimeoutExpired(WatchdogTimeoutExpired):
    """벽시계 백스톱 중단 — 실제 벽시계 임계와 kill 시점 침묵을 함께 보존."""

    def __init__(self, cmd, timeout, *, silence_seconds: float | None,
                 output=None, stderr=None) -> None:
        super().__init__(
            cmd, timeout, timeout_axis=TIMEOUT_AXIS_WALL, silence_seconds=silence_seconds,
            output=output, stderr=stderr,
        )


# CompletedProcess 에 실어 보내는 관측 침묵 초 속성명 — 감사 헤더(save_raw_output)가 읽는다.
SILENCE_SEC_ATTR = "silence_sec"


def _proc_partial_output(proc) -> tuple[str, str]:
    """proc 가 지금까지 받은 (stdout, stderr) 스냅샷. 어댑터가 미지원이면 ("","")."""
    getter = getattr(proc, "partial_output", None)
    if not callable(getter):
        return "", ""
    try:
        stdout, stderr = getter()
    except Exception:  # noqa: BLE001 — 부분 산출물 회수 실패가 kill 경로를 깨면 안 된다.
        return "", ""
    return stdout or "", stderr or ""


def _attach_partial_output(exc: subprocess.TimeoutExpired, proc) -> None:
    """TimeoutExpired 에 kill 시점까지의 출력을 실어 준다(부분 산출물 전량 폐기 폐쇄)."""
    stdout, stderr = _proc_partial_output(proc)
    if not exc.output:
        exc.output = stdout
    if not exc.stderr:
        exc.stderr = stderr


def _attach_cleanup_output(exc: ProcessCleanupError, proc) -> None:
    """정리 실패 sentinel에도 kill 시점까지의 부분 산출물을 보존한다."""
    stdout, stderr = _proc_partial_output(proc)
    if not exc.output:
        exc.output = stdout
    if not exc.stderr:
        exc.stderr = stderr


def _proc_drain_complete(proc) -> bool:
    """부모+입출력 채널 완료 판정. 구형/fake 어댑터는 기존 poll 계약으로 호환."""
    checker = getattr(proc, "drain_complete", None)
    return bool(checker()) if callable(checker) else proc.poll() is not None


def _terminate_and_drain(proc, *, argv) -> tuple[str, str]:
    """그룹 kill 후 파이프 EOF까지 수확한다. 정리 실패는 부분 성공으로 숨기지 않는다."""
    try:
        proc.kill()
    except ProcessCleanupError as exc:
        _attach_cleanup_output(exc, proc)
        raise
    try:
        return proc.communicate(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired as exc:
        _attach_partial_output(exc, proc)
        # DI용 legacy fake는 kill 뒤 상태/파이프 완료를 모델링하지 않고 같은 예외 객체를 영구
        # 재발시키기도 한다. 실제 어댑터만 정리 실패를 판정하고, 구형 fake는 기존 부분출력 계약을
        # 유지한다(프로덕션 loud 보장을 약화하지 않음).
        if not isinstance(proc, _WatchedPopen):
            return _proc_partial_output(proc)
        raise ProcessCleanupError(
            f"프로세스 그룹 kill 후 파이프가 {_KILL_GRACE_SEC:g}s 안에 닫히지 않음: {argv!r}",
            output=exc.output, stderr=exc.stderr,
        ) from exc


def _silence_seconds(proc, now: float, start: float) -> float | None:
    """마지막 진행(chunk 도착) 이후 침묵 초. **관측 불가 어댑터면 None**(판정 skip).

    출력이 한 번도 없었으면 시작 시각을 기준으로 잰다(시작부터 전부 침묵). `last_event_at` 를
    노출하지 않는 proc 어댑터는 진행을 볼 수 없으므로 무진행 판정 대상이 아니다 —
    "신호 없으면 벽시계" 원칙(모르는 드라이버를 무진행으로 죽이지 않는다)."""
    getter = getattr(proc, "last_event_at", None)
    if not callable(getter):
        return None
    last = getter()
    # reader가 `now` 취득 직후 더 최신 chunk 시각을 기록할 수 있다. 그 경합은 진행이 미래에서
    # 왔다는 뜻이 아니라 관측 순서 차이이므로 감사값/판정 입력을 0 아래로 내리지 않는다.
    return max(0.0, now - (start if last is None else last))


def _drain_with_idle_judgment(proc, *, argv, start: float, idle_timeout: float,
                              overall_timeout: float, overall_deadline: float,
                              clock, sleep, log, poll_interval: float):
    """드레인 구간을 폴하며 **무진행** 을 주 판정으로 본다(벽시계는 백스톱).

    - 무진행 초과 → 프로세스 그룹째 kill + loud 1줄 + IdleTimeoutExpired(부분 산출물 동반).
    - 벽시계 초과 → 같은 kill + TimeoutExpired(부분 산출물 동반). 감지기가 고장나도 유한하게
      닫는다(무제한 금지·worktree_pool 의 captured 러너 폴백-캡과 동형).
    """
    while not _proc_drain_complete(proc):
        now = clock()
        silence = _silence_seconds(proc, now, start)
        if silence is not None and silence >= idle_timeout:
            stdout, stderr = _terminate_and_drain(proc, argv=argv)
            log(f"[pm-orch] idle watchdog: {silence:.0f}s 무진행(임계 {idle_timeout:.0f}s) "
                "— 프로세스 그룹 kill. 부분 산출물은 보존.")
            raise IdleTimeoutExpired(argv, idle_timeout, idle_seconds=silence,
                                     output=stdout, stderr=stderr)
        if now >= overall_deadline:
            stdout, stderr = _terminate_and_drain(proc, argv=argv)
            log(f"[pm-orch] overall watchdog: 벽시계 백스톱 {overall_timeout:.0f}s 초과 "
                "— 프로세스 그룹 kill. 부분 산출물은 보존.")
            raise WallTimeoutExpired(
                argv, overall_timeout, silence_seconds=silence,
                output=stdout, stderr=stderr,
            )
        sleep(poll_interval)
    return proc.communicate(timeout=max(0.0, overall_deadline - clock()))


def run_with_first_event_watchdog(
    argv,
    *,
    first_event_timeout: float | None,
    overall_timeout: float,
    retries: int,
    idle_timeout: float | None = None,
    cwd=None,
    env=None,
    text: bool = True,
    input_text: str | None = None,
    on_spawn_attempt=None,
    popen=None,
    clock=None,
    sleep=None,
    log=None,
    poll_interval: float = _WATCHDOG_POLL_INTERVAL_SEC,
):
    """argv 를 관측 워치독으로 실행 — startup stall(유한 재시도) + 무진행을 유한하게 닫는다.

    각 시도: 프로세스 시작 → stdout 첫 이벤트를 first_event_timeout 초 내 관측하는지 감시.
      - 관측(또는 첫 이벤트 없이 빠른 종료) → 완료까지 드레인 후
        subprocess.CompletedProcess(returncode·stdout·stderr) 반환.
      - 미관측(stall) → 프로세스 그룹째 kill·loud 1줄·다음 시도.
    모든 시도(= retries+1) 소진 → StallWatchdogError(fail-loud·호출부가 정책 결정).

    `first_event_timeout=None` = **startup 창 미적용**(첫-이벤트 감시 없이 바로 드레인). 첫 stdout
    이 종료 직전에야 오는 축(codex exec 평문 리뷰어)이 이 창에 걸려 죽지 않게 하는 선언이다 —
    하니스별 if 분기가 아니라 호출부의 능력 선언(idle_timeout_for_signal 과 짝).

    `idle_timeout=None` = **무진행 판정 미적용**(현행 동작 불변) — 드레인은 종전처럼 단일 블로킹
    `communicate(overall 백스톱)` 이다. 값이 있으면 드레인이 폴 루프로 바뀌어 마지막 진행 이후
    침묵이 그 값을 넘을 때 kill 한다(벽시계는 백스톱으로 잔류).

    DI seam(hermetic 테스트·바이너리 불요):
      popen : argv -> proc(first_event_ready()/poll()/kill()/communicate(timeout)/returncode
              [+ last_event_at()/partial_output() — 무진행 판정·부분 보존]).
              기본 = _WatchedPopen(cwd/env/text/input_text/clock 바인딩).
      clock : () -> 초(단조). 기본 time.monotonic.
      sleep : (초) -> None. 기본 time.sleep(폴 간격 양보). fake 는 여기서 clock 을 전진시킨다.
      log   : (str) -> None. 기본 stderr 1줄.

    `on_spawn_attempt` = **첫 자식 생성 직전** 1회 호출되는 seam(기본 None = 종전 동작). 호출
    지점은 인자 검증·프로필 해소·워치독 준비가 모두 끝나 **다음 문장이 자식을 띄우는** 자리다 —
    그보다 앞의 실패(준비 구간)는 전송이 확실히 0 이라, 이 콜백으로 예산 소유권을 옮기는 호출부가
    그 구간을 환불 대상으로 유지할 수 있다. stall 재시도의 2회차 이후에는 다시 호출하지 않는다
    (소유권 이전은 실행당 1회이고, 그때는 이미 자식이 한 번 떴다).

    스폰 실패의 "자식 없음" 표식(`spawn_failed`)은 두 층이 나눠 붙인다. 실 어댑터
    (`_WatchedPopen`)는 `Popen` 경계를 직접 보므로 preflight 거절(argv/cwd/env 의 NUL)과 확정 기동
    실패(exec 이 실패한 `FileNotFoundError`·`PermissionError`·`NotADirectoryError`·
    `IsADirectoryError`)에 True 를, 그 밖의 `Popen` 예외와 `Popen` 성공 뒤의 초기화 실패에는 False
    를 붙인다(자식 유무를 확정할 수 없으면 보수쪽). 이 루프는 표식 없는 주입 어댑터의 `OSError`
    에만 True 를 덧붙이고, **자식이 한 번이라도 떴으면** 그 뒤의 실패에 표식을 False 로 못박는다:
    앞선 시도의 자식이 있는데 뒤 시도가 기동에 실패한 경우도, `popen` 이 돌아온 **뒤** 본문
    (시계·first_event·poll·communicate·정리)이 무엇으로 실패한 경우도 같다. 실행 단위 보수 규칙이
    시도 단위 관측을 이긴다(이미 나갔을 전송이 환불되지 않게).
    overall_timeout 은 호출부의 벽시계 백스톱이다 — 무진행 판정이 켜지면 주 판정 자리를 내주고
    "감지기가 고장난 경우"의 유한 상한으로만 남는다.
    """
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    log = log if log is not None else _default_watchdog_log
    if idle_timeout is not None:
        normalized_idle = normalize_timeout_seconds(idle_timeout)
        if normalized_idle is None:
            raise ValueError(
                f"idle_timeout={idle_timeout!r}: 유한한 정수 초(최소 1) 필요"
            )
        idle_timeout = float(normalized_idle)
    if popen is None:
        def popen(_argv):  # 기본 실 Popen 어댑터(cwd/env/text/stdin/clock 클로저).
            return _WatchedPopen(_argv, cwd=cwd, env=env, text=text,
                                 input_text=input_text, clock=clock)

    attempts = retries + 1  # retries=재시도 횟수 → 총 시도 = retries+1(최초 1 + 재시도 M).
    last_reason = ""
    last_axis: str | None = None
    last_threshold: float | None = None
    last_silence: float | None = None
    stalled_stdout: list[str] = []
    stalled_stderr: list[str] = []
    # 이 **실행**에서 자식이 한 번이라도 실제로 떴는가. 재시도 2회차 이후의 기동 실패가 1회차
    # 자식(이미 전송·과금됐을 수 있다)을 '자식 없음'으로 되돌리지 못하게 막는 상위 사실이다.
    child_created = False
    for attempt in range(1, attempts + 1):
        # ── 실 스폰 경계 ────────────────────────────────────────────────
        # 여기까지가 "확실히 전송 전"이다(인자 검증·프로필 해소·워치독 준비). 다음 문장이 자식을
        # 띄우므로 소유권 이전 콜백은 이 자리에 선다. 재시도 2회차부터는 이미 자식이 한 번 떴으니
        # 다시 넘기지 않는다.
        if on_spawn_attempt is not None and attempt == 1:
            on_spawn_attempt()
        try:
            proc = popen(argv)
        except BaseException as exc:
            if child_created:
                # 앞선 시도의 자식이 이미 떴다 — 그 자식이 프롬프트를 보냈을 수 있으므로 이번
                # 시도가 무엇으로 실패했든 **실행 전체**를 '자식 없음'으로 만들 수 없다. 여기서
                # 되돌리면 이미 나간 전송이 예산에서 사라진다(재시도 2회차의 기동 실패가 1회차
                # 전송을 환불하던 구멍).
                _mark_child_existed(exc)
                raise
            # 첫 자식을 만들다 실패했다 — 자식은 아직 없었다. `_WatchedPopen` 이 실 `Popen`
            # 경계에서 붙인 표식(pre-child 거절 True·`Popen` 성공 뒤 실패 False)이 1순위고,
            # 표식 없는 주입 어댑터는 종전 규칙대로 exec 실패 종류(OSError)만 '자식 없음'으로
            # 확정한다(그 밖의 종류는 표식 없이 올려 상위가 보수적으로 판정한다).
            if isinstance(exc, OSError):
                _mark_spawn_failed(exc, True)
            raise
        child_created = True
        cleaned = False
        try:
            start = clock()
            overall_deadline = start + overall_timeout
            stalled = False
            wall_expired = False
            if first_event_timeout is not None:
                first_deadline = start + first_event_timeout
                while True:
                    if proc.first_event_ready():
                        break  # 첫 이벤트 관측 → 드레인 단계로.
                    if proc.poll() is not None:
                        break  # 첫 이벤트 없이 종료(빠른 exit·에러) → 드레인이 결과 수습.
                    now = clock()
                    first_expired = now >= first_deadline
                    wall_expired = now >= overall_deadline
                    if first_expired and (
                        not wall_expired or first_deadline < overall_deadline
                    ):
                        # coarse poll로 둘 다 지났더라도 더 이른 first-event 축을 최종 선택한다.
                        wall_expired = False
                        stalled = True
                        last_axis = TIMEOUT_AXIS_FIRST_EVENT
                        last_threshold = float(first_event_timeout)
                        last_silence = _silence_seconds(proc, now, start)
                        last_reason = f"첫 이벤트 임계 {first_event_timeout:.0f}s 초과"
                        break
                    if wall_expired:
                        last_silence = _silence_seconds(proc, now, start)
                        break
                    sleep(poll_interval)
            if wall_expired:
                stdout, stderr = _terminate_and_drain(proc, argv=argv)
                cleaned = True
                log(f"[pm-orch] overall watchdog: 벽시계 백스톱 {overall_timeout:.0f}s 초과 "
                    "— 프로세스 그룹 kill. 부분 산출물은 보존. 자동 재시도 안 함.")
                raise WallTimeoutExpired(
                    argv, overall_timeout, silence_seconds=last_silence,
                    output=stdout, stderr=stderr,
                )
            if stalled:
                stdout, stderr = _terminate_and_drain(proc, argv=argv)
                cleaned = True
                if stdout:
                    stalled_stdout.append(stdout)
                if stderr:
                    stalled_stderr.append(stderr)
                log(
                    f"[pm-orch] stall watchdog: {last_reason} kill·재시도 {attempt}/{attempts}"
                )
                continue
            if idle_timeout is None:
                remaining = overall_deadline - clock()
                if remaining < 0:
                    remaining = 0.0
                try:
                    stdout, stderr = proc.communicate(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr = _terminate_and_drain(proc, argv=argv)
                    cleaned = True
                    if not stdout:
                        stdout = exc.output or ""
                    if not stderr:
                        stderr = exc.stderr or ""
                    raise WallTimeoutExpired(
                        argv, overall_timeout,
                        silence_seconds=_silence_seconds(proc, clock(), start),
                        output=stdout, stderr=stderr,
                    ) from exc
            else:
                stdout, stderr = _drain_with_idle_judgment(
                    proc, argv=argv, start=start, idle_timeout=idle_timeout,
                    overall_timeout=overall_timeout, overall_deadline=overall_deadline,
                    clock=clock, sleep=sleep, log=log, poll_interval=poll_interval,
                )
            completed = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
            # 감사용 관측치 — 완주 시점의 침묵 초(다음 kill 의 원인을 사후 확정하는 입력).
            setattr(completed, SILENCE_SEC_ATTR, _silence_seconds(proc, clock(), start))
            return completed
        except BaseException as primary:
            # 여기까지 왔다는 것은 `popen(argv)` 가 이미 핸들을 돌려줬다는 뜻이다 — 이 실행에는
            # 자식이 **있었다**. 그러니 본문(시계·first_event·poll·communicate·정리)에서 무엇이
            # 나오든 실행 전체를 '자식 없음'으로 읽히게 두면 안 된다. 종류만 보는 상위(추가 리뷰어
            # 러너)는 `PermissionError` 같은 확정-기동-실패 종류가 여기서 올라오면 전송 0 으로
            # 오분류해 이미 나갔을 프롬프트를 환불한다. 정리·재전파 **전에** 못박고(정리가 실패해
            # cause 로 매달려도 원 예외 객체는 그대로라 표식이 보존된다), 방향은 True→False 한쪽뿐이라
            # 단조롭다(`_mark_child_existed` 는 앞선 표식도 덮는 실행 단위 사실이다).
            _mark_child_existed(primary)
            if isinstance(primary, (WatchdogTimeoutExpired, ProcessCleanupError)):
                # 이 sentinel들은 해당 판정 경로가 이미 그룹 kill+drain을 끝냈거나 그 정리 자체가
                # 실패했음을 뜻한다. 기존 timeout 의미론을 그대로 재전파한다.
                raise
            # Ctrl-C/SystemExit 및 실행 중 예상 밖 오류 모두 새 세션의 자식을 남기지 않는다.
            # 정리 실패 시에도 원래 BaseException을 재전파하되 cause로 안전 실패를 노출한다.
            if not cleaned:
                try:
                    _terminate_and_drain(proc, argv=argv)
                except Exception as cleanup_error:
                    raise primary from cleanup_error
            raise

    silence_label = (
        f", 중단 시 침묵 {last_silence:.1f}s" if last_silence is not None else ""
    )
    headline = (
        f"startup stall 이 {attempts}회 연속 발생"
        if last_axis == TIMEOUT_AXIS_FIRST_EVENT
        else f"overall watchdog가 {attempts}회 연속 발화"
    )
    diagnosis = (
        " startup network fetch stall 의심(upstream #13841)."
        if last_axis == TIMEOUT_AXIS_FIRST_EVENT else ""
    )
    exhausted = StallWatchdogError(
        f"{headline}("
        f"실제 발화: {last_reason}{silence_label}) — 재시도 소진. "
        f"{diagnosis}".rstrip(),
        timeout_axis=last_axis,
        threshold_seconds=last_threshold,
        silence_seconds=last_silence,
        output="".join(stalled_stdout),
        stderr="".join(stalled_stderr),
    )
    # 여기 도달했다는 것은 모든 시도가 자식을 띄우고 stall 로 kill 됐다는 뜻이다 — 표식을 명시해
    # 상위가 종류 추론으로 이 실행을 '전송 0' 으로 읽을 여지를 남기지 않는다.
    _mark_child_existed(exhausted)
    raise exhausted


# ── opencode 출력 cap-hit(32k 절단) detector (하니스-무관 순수 헬퍼) ──────────────────
# opencode 는 outbound 응답이 출력 cap(32000 토큰·`opencode.jsonc` 실효 = min(limit.output,32000))을
# 넘으면 응답을 **조용히 절단** 하고 finish 를 "stop" 으로 위장한다 — 수신자(PM)는
# 절단을 감지하지 못한다. 파일-전달 규약이 우회책이나, *절단이 실제로 일어났는지* 알 장치가
# 없으면 우회 실패가 조용히 지나간다. 이 detector 가 출력 소비 지점(Supervisor.run_loop)에서 응답이
# cap 근방인지 보고 loud advisory 를 낸다. **advisory·never-block** — 경고+로그만·파이프라인 무중단
# (오탐이 relay 를 죽이면 안 됨). claude 는 범위 밖: claude 는 truncation 을 stop_reason=max_tokens 로
# *네이티브 노출* 하므로 silent 절단 클래스가 아니다 — run_loop 배선은
# 하니스-무관 크기 advisory 라 claude 응답도 지나가나 임계가 정상 응답보다 한참 위라 무영향.

CAP_TOKENS = 32000                   # opencode 실효 출력 cap(opencode.jsonc limit.output).
# 정확 토크나이저는 쓰지 않는다(무거운 의존·하니스-무관 유지·ticket §결정). char 수를 보수적 token
# 근사로 환산한다: char↔token 비는 내용마다 다르나(영어/코드 ≈3.5–4·한글 ≈1.5–2.5 char/token), 순수
# 한글은 실 토크나이저에서 그보다 dense(<1.5)일 수 있어 32000~43200자 절단을 놓칠 위험이 있다. 그
# 한글-dominant false-negative 창을 닫기 위해 char 길이 하한을 **극단 dense CJK(~1.2 char/token)**
# 기준으로 보수 하향한다 — genuine 32k-token 응답은 이 밀도에서도 ≈38400 char 다.
CAP_HIT_MIN_CHARS_PER_TOKEN = 1.2    # char/token 보수적 하한(극단 dense CJK 기준·상한 token 근사).
CAP_HIT_RATIO = 0.90                 # cap 근방 밴드 = char 하한의 90% 이상이면 절단 의심.
# char 임계 = 32000 × 1.2 × 0.90 = 34560 char. 이 값은 (a) 내용 혼합 전반의 genuine 절단 char 길이
# (영어 128k·코드 112k·혼합 80k·한글 48k·극단 dense CJK 38k) *아래* 라 절단을 놓치지 않고
# (false-negative 회피 — 순수 한글 dense 창 포함), (b) 정상 PM relay 응답(수백~수 KB·verbose handoff
# ~15KB)보다 여전히 >2x 위라 오탐 0(false-positive 회피). 두 경계 폭이 넓어 단일 char 임계로 양쪽을
# 만족한다(정확 token 수 불필요·advisory 목적엔 절단 *의심* 표면화면 충분).
CAP_HIT_CHAR_THRESHOLD = int(CAP_TOKENS * CAP_HIT_MIN_CHARS_PER_TOKEN * CAP_HIT_RATIO)  # 34560
CAP_HIT_THRESHOLD_ENV = "PM_OC_CAP_HIT_THRESHOLD"  # char 임계 override(필요 시만·양의 정수).


def cap_hit_char_threshold_default() -> int:
    """PM_OC_CAP_HIT_THRESHOLD env(양의 char 수) 또는 기본 34560. cap-hit 감지 char 임계 해소기."""
    return _env_positive_int(CAP_HIT_THRESHOLD_ENV, CAP_HIT_CHAR_THRESHOLD)


def detect_output_cap_hit(text, *, char_threshold: int | None = None,
                          cap_tokens: int = CAP_TOKENS) -> tuple[bool, str]:
    """outbound 응답이 opencode 출력 cap(≈32k 토큰) 근방인지 감지 — silent 절단 의심.

    반환 (hit, reason). hit=True 면 응답이 cap-hit 임계(char) 이상이라 절단 의심 → 호출부가 loud
    advisory 를 낸다(never-block). hit=False 면 reason="". 정확 token 수는 근사(보수적 상한) —
    char↔token 비가 내용마다 달라 exact 는 못 주나 절단 *의심* 표면화엔 충분(advisory 목적).

    char_threshold=None 이면 env 노브(PM_OC_CAP_HIT_THRESHOLD) 또는 기본을 쓴다. text 가 비거나
    None 이면 무발화(정상 크기 = 무조건 통과·오탐 0 보장의 하한 경로).
    """
    if char_threshold is None:
        char_threshold = cap_hit_char_threshold_default()
    length = len(text) if text else 0
    if length < char_threshold:
        return False, ""
    approx_tokens = int(length / CAP_HIT_MIN_CHARS_PER_TOKEN)  # 보수적 상한 근사(token).
    reason = (
        f"응답 {length} 자(≈{approx_tokens} tok 보수적 근사) ≥ 임계 {char_threshold} 자 — "
        f"opencode 출력 cap {cap_tokens} tok 근방"
    )
    return True, reason


def cap_hit_warning_message(reason: str) -> str:
    """cap-hit loud advisory 1줄.

    run_loop 배선은 하니스-무관이라 claude 대형 응답에도 발화할 수 있다 — opencode 한정으로 단정하지
    않고 **조건부**('opencode 하니스라면')로 문구를 중립화해 오해 소지를 없앤다(claude 는 truncation 을
    stop_reason 으로 네이티브 노출하므로 이 silent-절단 클래스가 아님·§메모). 규약 안내는 유지한다.
    stdout(=PM 대화 채널)은 오염하지 않는다 — 호출부가 이 문자열을 stderr/log 로 낸다."""
    return (
        f"[pm-orch] ⚠ 출력 상한(32k tok) 근방: {reason}. **opencode 하니스라면** 이 응답이 silent "
        "절단됐을 가능성이 있다 — opencode 는 32k 출력 토큰에서 응답을 조용히 자르고 finish 를 'stop' "
        "으로 위장한다(수신자 감지 불가). 잘렸다면 파일-전달 규약으로 재시도하라: 대형 "
        "산출물은 파일로 쓰고(opencode 는 safe_write 8KB 청크·write 는 16KB 초과 거부), 응답엔 절대경로 "
        "+ 핵심 요약 ≤10줄만 반환."
    )


def _sanitize_session_id(session_id: str) -> str:
    """marker 파일명 안전화 — ctx_stop_hook._session_id 와 동일 규칙(파일명 안전 문자만·64자).

    hook 이 child 의 session_id 를 이 규칙으로 sanitize 해 marker 를 쓰므로, marker 경로를
    *예측* 하려면 supervisor 도 같은 변환을 적용해야 한다(uuid4 는 본래 안전하나 방어적 일치)."""
    safe = "".join(c for c in session_id.strip() if c.isalnum() or c in "-_")[:64]
    return safe or "unknown"


def _marker_path(root: Path, session_id: str) -> Path:
    return root / MARKER_DIR / f"{_sanitize_session_id(session_id)}.done"


class Supervisor:
    """상태 없는 thin supervisor.

    **stateless 불변식**: 인스턴스 상태는 *주입된 협력자*(driver)와 *고정 config*(root·
    marker_dir)뿐 — 대화/작업 상태 필드는 0. user↔PM 메시지는 누적하지 않고 지나보낸다
    (입력 반복 버퍼 없음). 연속성은 file.
    """

    def __init__(self, driver: SessionDriver, *, root: Path,
                 bootstrap: str | None = None, task: str | None = None,
                 max_consecutive_respawns: int = MAX_CONSECUTIVE_RESPAWNS) -> None:
        # 협력자·고정 config 만 — 대화/작업 상태 필드 없음(stateless 단언의 근거).
        # max_consecutive_respawns 는 *config* 상수(불변 임계)지 작업/대화 상태가 아니다.
        self.driver = driver
        self.root = Path(root)
        # task 정체성(() 명시 전달)은 재진입 프롬프트에 baked-in 되어 `self.bootstrap` 에
        # 흡수된다 — 별도 인스턴스 필드로 retain 하지 않는다(stateless 불변식 유지·respawn 은 같은
        # bootstrap 을 재사용해 task 를 자동 forward). bootstrap 명시 override(테스트/커스텀)가 우선,
        # 없으면 task 로 빌드(task None 이면 현행 bare BOOTSTRAP_PROMPT 와 byte-동일).
        self.bootstrap = bootstrap if bootstrap is not None else build_bootstrap_prompt(task)
        self.max_consecutive_respawns = max_consecutive_respawns

    def stop_marker_present(self, session_id: str) -> bool:
        return stop_marker_present(self.root, session_id)

    def run_loop(self, cwd: str, in_stream: TextIO, out_stream: TextIO,
                 cap_hit_log=None) -> int:
        """바깥 루프 — spawn → relay → post-turn marker 감지 → respawn → repeat.

        - in_stream: 사용자 입력 라인 소스(stdin·테스트는 StringIO).
        - out_stream: PM reply 출력 sink(stdout·테스트는 StringIO).
        - cap_hit_log: cap-hit(32k 절단 의심) loud advisory sink(기본 stderr·테스트는 list.append).
          출력 소비 지점에서 응답이 cap 근방이면 경고만 낸다(never-block·stdout 무오염).
        - 반환 = exit code(0=정상 종료 EOF/quit · GUARD_TRIPPED_RC=연속 respawn 가드 발동).

        marker 존재는 payload 구분 없이 "다음 입력 전 회전" 단일 규칙으로 소비한다.
        이미 완료된 turn 의 입력은 다시 보내지 않는다.

        연속 respawn 가드: bootstrap turn 만으로 marker 가 박히는 fresh 세션이 연속되면
        spawn-loop 가 된다. 이 연속 즉시-회전 횟수를 지역 카운터로 세고 max 초과 시
        진단 1줄 후 종료한다. 사용자 turn 처리 가능한 세션에 도달하면 0으로 리셋한다.
        """
        cap_hit_log = cap_hit_log if cap_hit_log is not None else _default_watchdog_log
        session_id = self._spawn(cwd, out_stream)
        consecutive_respawns = 0  # bootstrap 직후 연속 즉시-회전 횟수(지역).

        while True:
            # 불변식(): **marker 를 지닌 세션엔 추가 입력을 relay 하지 않는다.**
            # spawn/respawn 의 bootstrap turn 이 예산을 넘겨 marker 를 남겼으면
            # 첫 입력 처리 *전* 여기서 회전한다 — 안 그러면 과예산 세션에 입력 1회가 추가 실행된다
            # (지연 회전). bootstrap 은 이미 실행됐으니 반복 대상이 아니다. 병적
            # spawn-loop(예산이 bootstrap turn 보다도 작음)는 연속 즉시-회전 카운터로 막는다.
            if self.stop_marker_present(session_id):
                consecutive_respawns += 1
                if consecutive_respawns > self.max_consecutive_respawns:
                    out_stream.write(
                        f"[relay] spawn 직후 연속 {consecutive_respawns}회 ctx 회전 — 무한 회전 "
                        f"차단(max={self.max_consecutive_respawns}). 종료. ctx 예산이 bootstrap "
                        "turn 보다 작은지 점검.\n"
                    )
                    out_stream.flush()
                    self.driver.close(session_id)
                    clear_marker(self.root, session_id)
                    return GUARD_TRIPPED_RC
                session_id = self._respawn(
                    cwd, session_id, out_stream, reason="bootstrap 초과",
                )
                continue  # 새 sid 로 재-loop — 입력은 회전된 세션이 받는다.

            # marker 없는 fresh 세션에 도달 = 연속 즉시-회전 chain 종료.
            consecutive_respawns = 0
            line = in_stream.readline()
            if line == "":  # EOF.
                break
            text = line.rstrip("\n")
            if text.strip() in QUIT_COMMANDS:
                break
            if text.strip() == "":
                continue

            reply = self.driver.relay_turn(session_id, text)
            if reply is not None:
                out_stream.write(reply + "\n")
                out_stream.flush()
                # 출력 소비 지점 — 응답이 출력 상한(32k tok) 근방이면 silent 절단 의심 loud advisory
                # (never-block·stdout 은 이미 위에서 그대로 전달·경고는 별도 sink). advisory 는
                # relay 를 절대 못 죽인다 — detect/message/sink write 전 경로를 try/except 로 감싼다
                # (병적 sink·근사 예외가 파이프라인을 중단시키면 안 됨·never-block 을 코드로 못박음).
                try:
                    cap_hit, cap_reason = detect_output_cap_hit(reply)
                    if cap_hit:
                        cap_hit_log(cap_hit_warning_message(cap_reason))
                except Exception:  # noqa: BLE001 — advisory 는 어떤 이유로도 relay 를 막지 않는다.
                    pass

            # 매 turn 직후 1회 stat — marker 있으면 다음 입력을 받기 전에 회전.
            # 이 turn 은 이미 처리·응답됐으므로 입력을 다시 보내지 않는다.
            if self.stop_marker_present(session_id):
                session_id = self._respawn(
                    cwd, session_id, out_stream, reason="turn 초과",
                )

        self.driver.close(session_id)
        return 0

    # ── 내부 회전 헬퍼 (상태 없음 — 인자만으로 동작) ───────────────────────────

    def _spawn(self, cwd: str, out_stream: TextIO) -> str:
        """결정적 session_id 발급 → driver.spawn(bootstrap) → reply 출력."""
        session_id = new_session_id()
        spawned = self.driver.spawn(cwd, session_id, self.bootstrap)
        if isinstance(spawned, tuple):
            session_id, reply = spawned
            if reply is not None:
                out_stream.write(reply + "\n")
                out_stream.flush()
            return session_id
        # 기존 driver 는 sid 만 반환한다. driver 계약 전환 동안 호환 유지.
        return spawned

    def _respawn(self, cwd: str, old_session_id: str, out_stream: TextIO,
                 *, reason: str) -> str:
        """떠나는 세션 정리 후 새 세션 spawn. 새 sid 반환."""
        out_stream.write(f"[relay] ctx 임계 도달 — 세션 회전 ({reason})\n")
        out_stream.flush()
        self.driver.close(old_session_id)
        clear_marker(self.root, old_session_id)
        return self._spawn(cwd, out_stream)
