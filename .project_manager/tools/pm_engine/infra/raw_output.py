"""하네스 원출력 보존 기록과 엔진 임시 루트 (층: infra).

위임·리뷰 실행의 원출력 파일 자리(`temp_root`·`raw_storage_paths`), 원출력 기록 행(시작·종료·정리), 고아
원출력 파일 판정, 기록 주인 프로세스 생존 판정을 소유한다.
"""
from __future__ import annotations


import contextlib
import datetime
import errno
import json
import os
import uuid
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

from pm_engine.infra import fs, lease_store, pm_paths


# 위임/추가 리뷰 raw 위치 기록. 두 실행 명령이 같은 파일을 갱신하므로 파일락 아래
# read-modify-write 하고 unique tmp를 atomic replace한다. 미마감은 완료 폭주와 별도 보존해
# 최근 비정상 종료를 찾을 수 있게 하되, 기간과 개수에 모두 상한을 둬 조회가 무한 누적으로
# 무력화되지 않게 한다. raw 파일은 감사 산출물이므로 이 기록 정리에서 삭제하지 않는다.
#
# 완료 기준(COMPLETED_DAYS·MAX_COMPLETED)은 귀속(ticket·role·model·rc·elapsed)의 유일 사본이라
# 원문 txt(무한 보존)보다 짧게 잡으면 계측 소스가 원문보다 먼저 사라진다. 레코드 크기
# 실측 846B/건·34건/일을 기준으로 90일 보존 시 약 3,060건 2.6MB — 매
# 위임 시작·마감마다 그 전량을 재기록해도 무리 없는 수준이라 상한을 3,060 여유분을 두고 4096으로
# 잡는다(무한 보존은 아니다 — 상한 없는 보존은 조회 무력화로 이어진다).
RAW_RECORD_VERSION = 1
RAW_RECORD_UNFINISHED_DAYS = 30
RAW_RECORD_COMPLETED_DAYS = 90
RAW_RECORD_MAX_UNFINISHED = 128
RAW_RECORD_MAX_COMPLETED = 4096

# 고아(기록 미참조) 원문 스캔 대상 파일명 접두 — 엔진이 만든 raw 만 가리킨다. `.local/delegate`에는
# 이 밖에 PM 수작업 산출(`T-XXXX-*.txt`·`*.md`)이 섞여 있고(실측 74+185건) 스캔
# 대상에 넣으면 고아 목록을 보고 사용자가 잘못 지운다.
ENGINE_RAW_FILENAME_PREFIXES = (
    "pm_delegate_", "additional_reviewer_", "external_review_",
)


def pid_is_alive(
    pid: object,
    *,
    _platform_name: str | None = None,
    _windows_api=None,
) -> bool:
    """프로세스를 종료하지 않고 PID 생존 여부만 조회하는 공용 cross-platform 함수.

    Windows는 ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` 핸들 획득 여부만
    확인하고, POSIX는 signal 0의 ESRCH/EPERM 규약을 따른다. 기록에서 읽은 값의
    bool/비정수/0 이하 입력은 실제 PID가 아니므로 부재로 정규화한다.

    ``_platform_name``과 ``_windows_api``는 Windows 분기를 비-Windows 회귀 환경에서도
    실제 프로세스에 손대지 않고 검증하기 위한 주입 교체 지점이다.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False

    is_windows = (
        os.name == "nt" if _platform_name is None else _platform_name == "nt"
    )
    if is_windows:
        if _windows_api is None:  # pragma: no cover — Windows 실환경 경로
            import ctypes
            _windows_api = ctypes.windll.kernel32
        try:
            handle = _windows_api.OpenProcess(0x1000, False, pid)
        except OSError as exc:
            # 명백한 PID 부재(WinError 87)만 사망 — 접근 거부 등 그 외 실패는 보수적으로
            # 생존으로 본다(살아있는 프로세스의 레코드를 마감하는 쪽이 더 나쁘다).
            code = getattr(exc, "winerror", None)
            if code is None:
                code = exc.errno
            return code != 87 if code is not None else True
        if not handle:
            # NULL 은 PID 부재만이 아니다 — 접근 거부(ERROR_ACCESS_DENIED=5)도 NULL 을
            # 준다(프로세스는 살아 있다). GetLastError 로 명백한 부재
            # (ERROR_INVALID_PARAMETER=87)만 False, 접근 거부·불명 오류는 True(생존 보수).
            last_error = None
            get_last_error = getattr(_windows_api, "GetLastError", None)
            if callable(get_last_error):
                try:
                    last_error = int(get_last_error())
                except (OSError, TypeError, ValueError):
                    last_error = None
            return last_error not in (87,) if last_error is not None else True
        try:
            _windows_api.CloseHandle(handle)
        except OSError:
            # 생존 판정은 이미 끝났다. 핸들 정리 오류가 PID를 죽음으로 뒤집지는 않는다.
            pass
        return True

    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # EPERM은 존재하지만 조회 권한이 없는 프로세스다. 그 밖의 조회 오류도 슬롯을
        # 조기에 정리하지 않도록 보수적으로 생존으로 간주한다.
        return True


TEMP_ROOT_DIRNAME = "tmp"


def temp_root(repo: Path) -> Path:
    """이 clone 이 소유한 작업용 임시 루트 — 없으면 만든다.

    `raw_storage_paths` 와 같은 기준 위치(`repo/.project_manager/.local`) 아래다. `.local/` 접두에는
    gitignore·남은 판정 제외(`finish_rules._is_local_runtime_path`)·pytest 수집 제외가 이미
    걸려 있어 새 규칙이 필요 없다 — 형제로 두면 같은 뜻의 두 번째 접두를 세 곳에 더해야 한다.

    엔진이 자기 작업용으로 만드는 임시 디렉터리의 자리는 이 함수 하나가 정한다. 호출부는
    `mkdtemp(prefix=..., dir=temp_root(repo))` 로만 쓰고 자리 규약을 자기 파일에 다시 적지
    않는다. 자식 이름의 유일성은 `mkdtemp` 가 그대로 소유한다 — 바뀌는 것은 부모뿐이다.
    """
    root = Path(repo) / ".project_manager" / ".local" / TEMP_ROOT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def raw_storage_paths(
    repo: Path,
    surface: str,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    """raw 디렉터리와 공유 기록 경로를 결정한다.

    명시 output_dir은 raw와 기록을 함께 그 디렉터리에 격리한다. 그 밖에는 해소된 repo의
    `.project_manager/.local/{delegate,review}`와 공유 `raw_outputs.json`이다. 위임 raw 출력과
    라운드 기록은 무엇을 보냈는지·몇 라운드였는지를 남기는 **기록**이라, 대상 저장소를 해소하지 못하면
    휘발성 위치로 바꾸지 않고 여기서 멈춘다.
    """
    if output_dir is not None:
        base = Path(output_dir)
        return base, base / "raw_outputs.json"
    target_repo = Path(repo)
    if not (target_repo / ".project_manager").is_dir():
        raise ValueError(
            f"raw 저장 위치를 해소하지 못했습니다 — 대상 저장소에 .project_manager 가 없습니다: {target_repo}. "
            "위임 raw 출력과 라운드 기록은 보존 대상이라 임시 위치로 대체하지 않습니다. "
            "PM 홈이나 그 lease 에 등록된 슬롯에서 실행하고, 직전에 'PM 홈 해소 실패' 경고가 "
            "있었다면 슬롯 대여 기록을 먼저 복구하세요. PM 홈이 아닌 저장소를 대상으로 삼는 "
            "실행이면 --output-dir <DIR> 로 저장 위치를 명시하세요."
        )
    local = target_repo / ".project_manager" / ".local"
    return local / surface, local / "raw_outputs.json"


def _raw_lock_path(record_path: Path) -> Path:
    return record_path.with_suffix(record_path.suffix + ".lock")


@contextlib.contextmanager
def _raw_record_lock(record_path: Path) -> Iterator[None]:
    """기록 read-modify-write를 배타 파일락으로 직렬화한다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 대체 동작)는 공용 `fs` 함수가 소유하고
    락 경로 규약(기록 옆 `.lock`)과 권한(0o600)만 이 도구가 정한다.
    """
    with fs.exclusive_file_lock(
        _raw_lock_path(record_path), mode=0o600,
    ):
        yield


def _read_raw_records(record_path: Path) -> dict:
    """기록 부재만 빈 기록으로 보고 손상은 fail-loud한다."""
    if not record_path.exists():
        return {"version": RAW_RECORD_VERSION, "records": []}
    data = json.loads(fs.read_text_shared(record_path, encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"raw 출력 기록 형식 오류: {record_path}")
    return data


def _write_raw_records(record_path: Path, raw_data: dict) -> None:
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = record_path.with_name(
        f"{record_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(raw_data, ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        fs.atomic_replace(tmp, record_path)
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
            RAW_RECORD_COMPLETED_DAYS if is_completed else RAW_RECORD_UNFINISHED_DAYS
        )
        if current - started > datetime.timedelta(days=age_limit):
            continue
        (completed if is_completed else unfinished).append(row)
    key = lambda row: str(row.get("started_at", ""))
    unfinished = sorted(unfinished, key=key, reverse=True)[:RAW_RECORD_MAX_UNFINISHED]
    completed = sorted(completed, key=key, reverse=True)[:RAW_RECORD_MAX_COMPLETED]
    return sorted((*unfinished, *completed), key=key)


# 기록 공통 스키마 키 — 실행 종류별 `extra` 가 절대 덮어쓰지 못하는 이름이다. 조회면이 보는 필드의
# 뜻이 실행 종류마다 달라지면 기록 하나로 실행을 재구성할 수 없다. **시작·마감이 같은 이 한 표를**
# 예약 규칙으로 쓴다: 시작을 "지금 이 행에 있는 키"로만 막으면 마감 전용 키(finished_at·rc 등)가
# 시작 시점엔 아직 없어 통과하고, 그러면 시작 extra 가 `finished_at` 을 심어 미마감 sweep 이
# 죽은 실행을 못 보거나 마감이 그 값을 덮어써 두 실행 종류의 extra 가 서로를 지운다.
RAW_RECORD_RESERVED_KEYS: frozenset[str] = frozenset({
    "id", "surface", "harness", "model", "role", "attempt", "pid",
    "started_at", "raw_path", "finished_at", "rc", "elapsed_sec", "silence_sec",
    "finish_note",
})


class RawRecordAlreadyFinished(ValueError):
    """마감 충돌 — 레코드에 이미 `finished_at` 이 있다(수동 `raw close` 선행 등).

    첫 마감을 보존한다는 신호다. 원 실행의 후속 마감 호출부는 이 예외를 잡아 실행 결과를
    실패로 바꾸지 않고 경고로 낮춘다(회신 자체는 raw 파일에 이미 적혀 있다)."""


def start_raw_record(
    record_path: Path,
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
    """자식 프로세스 실행 전에 미마감 레코드를 원자 등록하고 record id를 반환한다.

    `extra` = 실행 종류별 해소 provenance 의 **추가** 필드(예: 추가 리뷰어의 source/reasoning/command).
    공통 스키마 키(`RAW_RECORD_RESERVED_KEYS`)는 덮어쓰지 않는다 — 조회면이 보는 필드 이름의 뜻이
    실행 종류마다 달라지지 않게 한다(마감의 같은 규칙과 **한 표**를 공유한다).
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
        if key not in RAW_RECORD_RESERVED_KEYS:
            row[key] = value
    with _raw_record_lock(record_path):
        raw_data = _read_raw_records(record_path)
        records = [item for item in raw_data["records"] if isinstance(item, dict)]
        records.append(row)
        raw_data["version"] = RAW_RECORD_VERSION
        raw_data["records"] = _prune_raw_records(records, now=current)
        _write_raw_records(record_path, raw_data)
    return record_id


def finish_raw_record(
    record_path: Path,
    record_id: str,
    *,
    rc: int,
    elapsed_sec: float,
    silence_sec: float | None,
    now: datetime.datetime | None = None,
    finish_note: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """동일 레코드에 종료 관측치를 기록한다. 시작 레코드 부재는 유실이므로 fail-loud한다.

    `finish_note` 는 마감 provenance 의 명시 완료 필드다 — 예약 키라 시작/마감 extra 로는
    주입될 수 없고 이 파라미터로만 기록된다(수동 `raw close` 사유 등).

    `extra` = 실행 **후에야 알 수 있는** 실행 종류별 구조화 관측(예: 위임의 세션 id·usage 분해·
    직전 라운드 must_fix 항목). 공통 스키마 키는 무시하고, 나머지만 같은 레코드 행에 싣는다 —
    실행 하나의 사실이 두 기록으로 갈리지 않게 한다(조인 키 문제 원천 제거).
    """
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    with _raw_record_lock(record_path):
        raw_data = _read_raw_records(record_path)
        found = False
        for row in raw_data["records"]:
            if isinstance(row, dict) and row.get("id") == record_id:
                if row.get("finished_at") is not None:
                    raise RawRecordAlreadyFinished(
                        f"raw 출력 기록 항목은 이미 마감됨: {record_id}"
                    )
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
                if finish_note is not None:
                    row["finish_note"] = finish_note
                for key, value in (extra or {}).items():
                    if key not in RAW_RECORD_RESERVED_KEYS:
                        row[key] = value
                found = True
                break
        if not found:
            raise ValueError(f"raw 출력 기록에 시작 항목 없음: {record_id}")
        raw_data["records"] = _prune_raw_records(raw_data["records"], now=current)
        _write_raw_records(record_path, raw_data)


def raw_records(
    record_path: Path, *, unfinished_only: bool = False, lock: bool = True,
) -> list[dict]:
    """기록 레코드를 최신순으로 반환한다. 빈 기록과 손상 기록을 구분한다.

    `lock=False` 는 배타 파일락(`_raw_record_lock`)을 잡지 않고 공유 읽기만 한다 — 쓰기가
    `_write_raw_records` 의 atomic replace 라 항상 완결된 옛/새 파일 중 하나를 본다. compaction
    snapshot 처럼 "빌더는 subprocess·lock 0" 계약을 지키는 조회 전용 호출자용이다.
    """
    if lock:
        with _raw_record_lock(record_path):
            raw_data = _read_raw_records(record_path)
    else:
        raw_data = _read_raw_records(record_path)
    rows = [
        row for row in raw_data["records"]
        if (
            isinstance(row, dict)
            and (not unfinished_only or row.get("finished_at") is None)
        )
    ]
    return sorted(rows, key=lambda row: str(row.get("started_at", "")), reverse=True)


def unfinished_raw_records(record_path: Path, *, lock: bool = True) -> list[dict]:
    """미마감 레코드만 최신순으로 반환하는 조회 facade. `lock` 은 `raw_records` 로 전달."""
    return raw_records(record_path, unfinished_only=True, lock=lock)


class OrphanRawSummary(NamedTuple):
    """기록 미참조 엔진 명명 원문 스캔 결과 — 읽기 전용. 삭제는 이 티켓의 비목표다."""

    count: int
    total_bytes: int
    paths: tuple[Path, ...]


def scan_orphan_raw_files(
    base_dirs: Iterable[Path], record_path: Path,
) -> OrphanRawSummary:
    """`base_dirs` 아래 엔진 명명 원문 중 기록(`record_path`)이 가리키지 않는 것을 나열한다.

    완료·미마감을 가리지 않고 기록의 모든 `raw_path` 를 참조로 본다 — 아직 마감 전인 실행의
    raw 를 고아로 오판하지 않기 위해서다. 파일은 지우지 않는다(조회만 · 삭제는 사용자 결정).
    """
    referenced = {
        str(Path(row["raw_path"]).resolve())
        for row in raw_records(record_path)
        if isinstance(row.get("raw_path"), str)
    }
    seen_dirs: list[Path] = []
    for base_dir in base_dirs:
        if base_dir not in seen_dirs:
            seen_dirs.append(base_dir)
    orphans: list[Path] = []
    for base_dir in seen_dirs:
        if not base_dir.is_dir():
            continue
        for path in sorted(base_dir.iterdir()):
            if not path.is_file():
                continue
            if not path.name.startswith(ENGINE_RAW_FILENAME_PREFIXES):
                continue
            if str(path.resolve()) in referenced:
                continue
            orphans.append(path)
    total_bytes = sum(path.stat().st_size for path in orphans)
    return OrphanRawSummary(
        count=len(orphans), total_bytes=total_bytes, paths=tuple(orphans),
    )


def _peer_engine_records(home, config_repo_override: Path | None) -> tuple[Path, ...]:
    """현재 엔진과 같은 PM 홈 계열의 다른 사본이 소유한 기본 기록을 찾는다.

    PM 홈의 실 board + ``work/*`` 관례는 기존 대상 저장소 다시 잡기 판정과 같은 물리 증거다. 후보는 실제
    ``pm_delegate.py`` 사본과 기본 기록 파일을 모두 가져야 한다. 기록 내용은 읽지 않으며,
    조회 대상을 다른 사본으로 바꾸지도 않는다.
    """
    try:
        current = home.root.resolve()
        pm_home = (config_repo_override or
                   pm_paths.resolve_pm_home_for_repo(current)).resolve()
        candidates = [pm_home, *lease_store._registered_worktrees(pm_home)]
        record_files: set[Path] = set()
        for repo in candidates:
            resolved_repo = repo.resolve()
            if resolved_repo == current:
                continue
            engine = (
                resolved_repo
                / ".project_manager"
                / "tools"
                / "pm_delegate.py"
            )
            record_file = (
                resolved_repo
                / ".project_manager"
                / ".local"
                / "raw_outputs.json"
            )
            if engine.is_file() and record_file.is_file():
                record_files.add(record_file.resolve())
        return tuple(sorted(record_files))
    except (OSError, RuntimeError, AttributeError, ImportError):
        return ()



# ── 결과 기록 (O_EXCL·0600·PID/UUID) ─────────────────────────────────────

def _gettempdir() -> str:
    """OS 임시 디렉터리 — 소비자는 `_with_resolved_child_env` **하나**다(자식 env 의 임시 디렉터리).

    엔진이 만드는 임시물의 자리는 `temp_root`(프로젝트 안) 하나다. 이 값은 엔진이
    만드는 자리가 아니라 **스폰하는 자식에게 넘기는 env 값**이라 남는다. 근거 둘(실측):
    (1) 받는 쪽이 하네스 CLI 와 그 자식 전부(node·npm·git·pytest)인데 엔진에 그 임시물의 목록도
    정리 훅도 없다 — 프로젝트 안을 주면 주인 없는 산출이 저장소에 쌓인다(격리 트리 실측
    205M·243M·197M). (2) codex 정책의 `exclude_tmpdir_env_var` 때문에 `$TMPDIR` 은 workspace-write
    의 쓰기 루트에 자동으로 들어간다 — 프로젝트 안을 주면 남의 도구의 쓰기 권한이 우리 트리로
    넓어진다. 새 소비자를 여기 붙이지 마라 — 엔진 자기 임시물이면 `temp_root` 다
    (회귀가 이 소비자 하나만 통과시킨다: `tests/test_pytest_temp_root.py`).
    """
    import tempfile
    return tempfile.gettempdir()


def _raw_storage(home, output_dir: Path | None = None) -> tuple[Path, Path]:
    """raw/기록 위치를 해소된 PM 홈 소유자에서 정한다(REPO 미해소는 fail-loud)."""
    return raw_storage_paths(
        home.local_dir.parent.parent, "delegate", output_dir,
    )


def _reserve_raw_output(home, harness: str, output_dir: Path | None = None) -> Path:
    """실행 전 기록이 가리킬 0600 raw 파일을 O_EXCL로 먼저 차지한다."""
    base_dir, _record_path = _raw_storage(home, output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    dest = (
        base_dir
        / f"pm_delegate_{harness}_{os.getpid()}_{uuid.uuid4().hex}.txt"
    )
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return dest


def _write_reserved_raw(dest: Path, content: str) -> None:
    """먼저 차지한 raw 파일 내용을 교체한다(파일 정체성과 0600 권한 유지)."""
    with dest.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
