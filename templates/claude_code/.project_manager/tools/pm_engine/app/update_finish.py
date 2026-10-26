"""옛 자체 슬롯·세션 장부와 areas 자기 행의 갱신 후처리 (층: app)."""
from __future__ import annotations

from pathlib import Path
import json
import re
from pm_engine.app import areas_registry
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store


def fill_self_repo_row(effective_dest: Path, *, write: bool) -> None:
    """대상 Home의 빈 자기 행을 채우고 기존 write/dry-run 결과를 출력한다."""
    filled = areas_registry.fill_self_repo_row(pm_paths.home_at(Path(effective_dest)), write=write)
    if filled is None:
        return
    if write:
        print(f"  ✓ areas.md 자기 행 git·base 기록: {filled}")
    else:
        print(f"[dry-run] areas.md 자기 행 git·base 기록 예정: {filled}")


def drop_self_lease_rows(effective_dest: Path, *, write: bool) -> int:
    """흡수 말미 1회 — 리스 장부에서 **PM 홈 자신을 가리키던 행**(`slot="."`)을 지운다.

    옛 엔진은 홈 자신을 첫 슬롯 행으로 등재했다. 그 행은 작업 폴더가 아니라 홈이라,
    관리자 화면·슬롯 목록에 작업 폴더 `.` 로 보이고 소비자마다 예외 분기를 갖게 했다. 그
    경로를 폐기하면서 이미 박힌 행도 함께 지운다 — 옛 값을 남기지 않는다.

    지울 행이 없으면 장부를 **읽기만** 한다(멱등 — 재실행이 byte 를 바꾸지 않는다).
    `write=False`(dry-run)면 판정만 한다. 반환값은 지운(지울) 행 수다.

    읽기·판정·쓰기 전부를 dest 의 `worktree-leases.lock` 배타락 안에서 한다 —
    엔진(`pool_store._lease_lock`)이 선언한 "장부의 모든 read-modify-write 는 이 한
    구간" 계약과 같은 직렬화 구간이다. 쓰기도 엔진과 같이 tmp → 원자 교체를 지난다.
    """
    ledger = (Path(effective_dest) / ".project_manager" / ".local"
              / "worktree-leases.json")
    if not ledger.is_file():
        return 0
    with pool_store._lease_lock(pm_paths.home_at(Path(effective_dest))):
        data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
        leases = data.get("leases") or []
        kept = [row for row in leases if row.get("slot") != "."]
        dropped = len(leases) - len(kept)
        if dropped == 0:
            return 0
        prefix = "[dry-run] " if not write else "  ✓ "
        print(f"{prefix}작업 폴더 목록에서 PM 홈 자신 행 {dropped}개 제거"
              f"{' 예정' if not write else ''} — 홈은 작업 폴더가 아니다.")
        if write:
            data["leases"] = kept
            tmp = ledger.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            fs.atomic_replace(tmp, ledger)
    return dropped


# 옛 슬롯 모드가 리스 행 `session` 에 적던 표기(`<repo>_<N>`). 값 술어의 주인은
# `identity_args._SLOT_KEY_RE` 이고, 이 갱신기는 dest 형제 사본을 로드하지 않는 self-contained
# 장부 수술이라(`drop_self_lease_rows` 와 같은 관례) 패턴만 동형으로 들고 있다. task 이름은 이
# 패턴을 예약어로 거부당하므로(`identity_args.validate_task_name`) 두 집합은 겹치지 않는다.
_LEGACY_SLOT_SESSION_RE = re.compile(r"^[A-Za-z0-9][^\s()/\\]*_[1-9]\d*$")


def idle_legacy_slot_session_rows(effective_dest: Path, *, write: bool) -> int:
    """흡수 말미 1회 — `session` 이 옛 슬롯 표기(`<repo>_<N>`)인 대여 행을 idle 로 옮긴다.

    세션 정체성이 task 하나로 좁혀지면서 슬롯을 점유 주체로 적던 경로가 사라졌다. 그 표기로
    남은 대여 행은 어느 task 의 것도 아닌데, 부트스트랩·핸드오프의 정체성 해소(cwd→lease 행)가
    그 행을 집으면 예약 패턴이라 task 이름 검증에서 거부당해 세션이 그 폴더에서 진입하지
    못한다. 표기를 바꾼 것은 갱신이지 채택자가 아니므로 **바꾼 쪽이 옮긴다**.

    옮기는 것은 점유 표시뿐이다 — 작업 폴더도 그 안의 작업도 건드리지 않는다. `state="idle"`
    + `session=""` 는 엔진 반납 기록(`release`)과 같은 값이라, 다음 `alloc` 이 그 폴더를 그대로
    다시 빌려준다.

    옮길 행이 없으면 장부를 **읽기만** 한다(멱등 — 재실행이 byte 를 바꾸지 않는다).
    `write=False`(dry-run)면 판정만 한다. 반환값은 옮긴(옮길) 행 수다. 락·원자 교체 계약은
    `drop_self_lease_rows` 와 같다.
    """
    ledger = (Path(effective_dest) / ".project_manager" / ".local"
              / "worktree-leases.json")
    if not ledger.is_file():
        return 0
    with pool_store._lease_lock(pm_paths.home_at(Path(effective_dest))):
        data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
        leases = data.get("leases") or []
        moved: list[str] = []
        for row in leases:
            if not isinstance(row, dict) or row.get("state") != "leased":
                continue
            session = row.get("session")
            if not isinstance(session, str):
                continue
            if _LEGACY_SLOT_SESSION_RE.fullmatch(session) is None:
                continue
            moved.append(str(row.get("slot") or session))
            if write:
                row["state"] = "idle"
                row["session"] = ""
        if not moved:
            return 0
        prefix = "[dry-run] " if not write else "  ✓ "
        print(f"{prefix}옛 슬롯 표기로 잡혀 있던 작업 폴더 {len(moved)}개를 반납 상태로 이동"
              f"{' 예정' if not write else ''} — 세션 정체성은 이제 task 하나다"
              "(폴더와 그 안의 작업은 그대로).")
        for slot in moved:
            print(f"  {slot}")
        if write:
            tmp = ledger.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            fs.atomic_replace(tmp, ledger)
    return len(moved)
