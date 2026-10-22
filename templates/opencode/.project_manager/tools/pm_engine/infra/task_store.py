"""task 상태·todo·세션 claim 저장과 조회."""
from __future__ import annotations

import contextlib
import datetime
import json
import re
from pathlib import Path
from collections.abc import Iterator

from pm_engine.domain import task_rules
from pm_engine.infra import fs, pm_paths, pool_store


@contextlib.contextmanager
def _task_todo_lock(home, name: str) -> Iterator[None]:
    """한 task 의 할 일 파일 read-modify-write 를 직렬화한다.

    장부 락(`_lease_lock`)과 **다른 락**이다. 재진입 금지는 장부 락과 같다. 이 구간 안에서
    장부 락을 잡지 않는다 — 두 락을 겹쳐 잡는 자리를 만들면 순서를 지킬 책임이 생긴다.
    """
    path = pm_paths._task_todo_lock_file(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with fs.exclusive_file_lock(path):
        yield


def _write_todo_doc(home, name: str, doc: dict) -> None:
    """할 일 파일을 atomic replace 로 쓴다. **`_task_todo_lock` 보유 전제**."""
    path = pm_paths.task_todo_file(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    fs.atomic_replace(tmp, path)


def _read_todo_doc(home, name: str) -> dict:
    """할 일 파일을 읽는다 — 파일이 없으면 빈 문서.

    **고쳐 쓰는 경로는 `_task_todo_lock` 보유 전제**(조회는 락 없이 부른다 — 쓰기가 원자 교체라
    읽는 쪽이 반쪽 내용을 보지 않는다). 손상 JSON 과 어긋난 스키마는 올린다 — 빈 목록으로
    흘리면 열린 일이 없는 것처럼 보인다.
    """
    path = pm_paths.task_todo_file(home, name)
    if not path.is_file():
        return {"next_id": 1, "todos": []}
    doc = json.loads(fs.read_text_shared(path, encoding="utf-8"))
    task_rules._validate_todo_doc(path, doc)
    return doc


def read_task_todos(home, name: str) -> list[dict]:
    """이 task 의 열린 할 일 — 표시·판정이 쓰는 단일 조회.

    **조회는 아무것도 만들지 않는다** — 파일이 없으면 빈 목록이고 task 폴더도 락 파일도 만들지
    않는다. 쓰기 판정이 파일 수정마다 부르는 자리라 조회가 부작용을 남기면 안 된다. task 레코드
    유무도 보지 않는다(없는 task 는 빈 목록). 쓰기 판정·부트스트랩 dump·압축 재주입·관리자
    화면이 전부 이 함수 하나를 부른다(형식 사본 0).
    """
    task_rules._validate_task_name(name)
    return list(_read_todo_doc(home, name)["todos"])


def ensure_task_pm_state(home, name: str) -> Path:
    """task pm_state를 즉시 보장한다 — 신규 생성·구 task 재개 모두 같은 불변식.

    task는 슬롯 0개여도 독립 정체성이므로 state 생성은 slot alloc/bind나 첫 handoff까지 미루지
    않는다. 이미 있으면 byte 불변 no-op. 없으면 tracked template을 task 첫 세션 형태로 렌더해
    원자 교체한다. template 부재/손상은 task 장부만 생기는 반쪽 상태를 허용하지 않고 fail-loud.
    """
    task_rules._validate_task_name(name)
    target = pm_paths.task_pm_state_file(home, name)
    if target.exists():
        return target
    template = pm_paths._task_pm_state_template_file(home)
    if not template.exists():
        raise FileNotFoundError(
            f"task pm_state template 부재: {template} — task state를 만들 수 없다"
        )
    rendered = task_rules._render_initial_task_pm_state(
        fs.read_text_shared(template, encoding="utf-8"),
        datetime.date.today().isoformat(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8", newline="\n")
    fs.atomic_replace(tmp, target)
    return target


class NoTaskSessionClaim(Exception):
    """부트스트랩할 task 의 세션 기록이 없다 — 하네스 밖 실행이거나 가드 훅이 없다."""

    def __init__(self, home, name: str):
        self.name = name
        super().__init__(
            f"task {name!r} 의 하네스 세션 기록이 없다 — 부트스트랩은 하네스(claude·codex·opencode) "
            "안에서 셸 도구로 실행해야 가드 훅이 세션 ID 를 남긴다. 훅 설치를 확인하라"
            f" ({pm_paths._task_session_claim_path(home, name)})."
        )


def record_task_session_claim(home, name: str, session: str) -> Path:
    """가드 훅 전용 — `name` task 를 `session` 이 부트스트랩하려 한다는 기록을 남긴다."""
    task_rules._validate_task_name(name)
    if not isinstance(session, str) or not session:
        raise ValueError(f"task {name!r} 세션 기록에 쓸 세션 ID 가 비어 있다")
    path = pm_paths._task_session_claim_path(home, name)
    with pool_store._lease_lock(home):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps({"task": name, "session": session}, ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")
        fs.atomic_replace(tmp, path)
    return path


def take_task_session_claim(home, name: str) -> str:
    """부트스트랩 전용 — 가드 훅이 남긴 세션 기록을 가져가(지우고) 세션 ID 를 돌려준다."""
    task_rules._validate_task_name(name)
    path = pm_paths._task_session_claim_path(home, name)
    with pool_store._lease_lock(home):
        if not path.exists():
            raise NoTaskSessionClaim(home, name)
        data = json.loads(fs.read_text_shared(path, encoding="utf-8"))
        path.unlink()
    session = data.get("session") if isinstance(data, dict) and data.get("task") == name else None
    if not isinstance(session, str) or not session:
        raise NoTaskSessionClaim(home, name)
    return session


def todo_age_days(created: str, *, now=None) -> "int | None":
    """등록일(UTC ISO)에서 며칠 묵었는지 — 값이 없거나 ISO 가 아니면 `None`.

    나이는 사람이 보는 표시값이라 못 읽는 시각 하나가 목록 전체를 막지 않는다 — 그 줄의 나이만
    비운다. 판독측(`_validate_todo_doc`)이 문자열만 통과시키므로 여기 오는 값은 문자열이거나 빈 값이다.
    """
    if not created:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(created)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    current = now or datetime.datetime.now(datetime.timezone.utc)
    return max((current - stamp).days, 0)


def format_todo_line(todo: dict, *, now=None) -> str:
    """열린 할 일 한 줄 — `#<ID> <내용> (등록 <날짜> · N일째)`.

    표시 형식의 단일 진실이다 — CLI(`task todo list`)·부트스트랩 dump·압축 재주입이 같은 함수를
    부른다(문구 사본 0). 나이를 못 구하면 등록일만 적는다. 앞에 붙는 ID 는 `close_task_todo` 가
    받는 값 그대로다 — 목록이 줄어도 번호가 밀리지 않는다.

    받는 행은 `read_task_todos` 가 낸 것이다(`_validate_todo_doc` 통과) — 세 키의 타입이 이미
    보장돼 있어 여기서 다시 확인하지 않는다.
    """
    head = f"#{todo['id']}"
    text = todo["text"]
    created = todo["created"]
    if not created:
        return f"{head} {text} (등록일 없음)"
    age = todo_age_days(created, now=now)
    tail = f"{created[:10]} · {age}일째" if age is not None else created[:10]
    return f"{head} {text} (등록 {tail})"


# 종료된 task 서술 폴더의 아카이브 루트 — `.local/tasks/_ended/`. 선행 `_` 라 `_validate_task_name`
# 이 실 task 명으로는 거부하므로(path 컴포넌트 규칙) 아카이브 하위와 실 task 가 절대 충돌하지 않는다.
_ENDED_DIR_NAME = "_ended"


def task_archive_candidates(home, name: str) -> "tuple[Path, ...]":
    """동명 task의 종료 archive 실디렉터리를 basename 순으로 반환한다.

    후보는 정확히 ``_ended/<name>-YYYYMMDD[-N]`` 모양인 실디렉터리뿐이다.
    파일과 symlink(디렉터리 symlink 포함)는 복원 후보가 아니다.
    """
    task_rules._validate_task_name(name)
    ended_root = pm_paths.tasks_dir(home) / _ENDED_DIR_NAME
    if not ended_root.is_dir() or ended_root.is_symlink():
        return ()
    pattern = re.compile(
        rf"^{re.escape(name)}-(?P<date>\d{{8}})(?:-(?P<ordinal>\d+))?$"
    )
    candidates: list[Path] = []
    for path in ended_root.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None or not path.is_dir() or path.is_symlink():
            continue
        try:
            archive_date = datetime.datetime.strptime(match.group("date"), "%Y%m%d")
        except ValueError:
            continue
        if archive_date.strftime("%Y%m%d") != match.group("date"):
            continue
        ordinal = match.group("ordinal")
        if ordinal is not None and len(ordinal) == 8:
            try:
                nested_date = datetime.datetime.strptime(ordinal, "%Y%m%d")
            except ValueError:
                pass
            else:
                if nested_date.strftime("%Y%m%d") == ordinal:
                    # ``name-date-date``는 짧은 name의 재종료 ordinal과
                    # 긴 task ``name-date``의 archive 양쪽으로 성립한다. 소유권을
                    # 증명할 metadata가 없으므로 짧은 이름에서는 fail-closed한다.
                    continue
        candidates.append(path)
    return tuple(sorted(candidates, key=lambda path: path.name))


def _archive_dest(home, name: str) -> Path:
    """종료 task 서술 폴더의 이동 목적지 `.local/tasks/_ended/<name>-<UTC날짜>/`.

    같은 날 같은 이름 재종료 시 충돌하면 `-2`·`-3`… 로 유일화한다(덮어써 기록 유실 방지). 날짜는
    UTC `YYYYMMDD`(`_now_utc` 와 같은 시계·표시만 날짜 단위). 이동이라 삭제-위임 원칙 무저촉이고,
    이름 재사용 시 옛 pm_state 를 resume 처럼 오인하는 조용한 오염을 막는다.
    """
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    ended_root = pm_paths.tasks_dir(home) / _ENDED_DIR_NAME
    dest = ended_root / f"{name}-{date}"
    n = 2
    while dest.exists():
        dest = ended_root / f"{name}-{date}-{n}"
        n += 1
    return dest
