"""보드 동시성 하드닝 테스트 (T-0057 · ADR-0012).

단일 루트 동시 세션이 공유 `.project_manager` 파일을 안전하게 쓰는지 검증한다:

  1. 동시 `new` → 유일 ID (board_lock 이 `_next_id`+파일생성 구간을 직렬화).
     - 락 무력화(no-op) 시 ID 충돌이 *재현*되는 sensitivity 테스트도 둔다 — 락이 실제로
       무엇을 막는지 박제(테스트가 락 없이도 통과하면 무의미).
  2. 동시 같은 ticket claim → 하나만 성공·나머지 `claim race lost`
     (기존 `move_ticket` atomic rename 의 원자성 회귀 박제 — 신규 락 아님).
  3. 크래시 시 락 자동해제 — 락 보유 프로세스가 죽으면 다음 획득자가 막히지 않는다
     (OS flock 특성·stale-lock 없음).

**hermetic 필수**: board.py 모듈 전역(`TICKETS_DIR`·`BOARD_LOCK`·`LOCAL_DIR` 등)은 import
시점에 실 repo 절대경로로 굳는다 — tmp 프로젝트로 재지정해 실 루트를 절대 건드리지 않는다
(test_board_multipm.py 의 monkeypatch hermetic 패턴 동류). 동시성 검증은 *별도 프로세스*를
띄워야 의미가 있으므로, 자식 워커는 tmp 프로젝트 경로를 인자로 받아 board.py 를 새로 로드 후
경로 전역을 그 tmp 로 재바인딩한다(부모 monkeypatch 는 자식에 상속되지 않으므로 명시 재배선).
"""
from __future__ import annotations

import importlib.util
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
REAL_LOCAL_DIR = REPO / ".project_manager" / ".local"

# spawn 워커는 워커마다 board 모듈을 재임포트하므로 부팅비용이 있다. full-suite 반복 실행 시
# 잔여 스위트의 CPU 포화와 겹치면 6워커 부트+drain 이 옛 30s drain 상한을 드물게 넘겨 flaky
# 했다(단독 실행은 항상 0.1s pass). 세 축으로 결정적 green 을 만든다:
#   - WORKERS: 동시 race 노출엔 ≥2 면 충분(같은 ID/같은 ticket 에 다수가 일제 진입하면 race 가
#     성립) — 4 로 줄여 부팅비용·CPU 압력을 낮춘다. (sensitivity 는 그대로: 락/rename 무력화 시
#     4 워커로도 충돌이 재현된다.)
#   - SYNC_TIMEOUT: ready/go/join/drain 의 실패 상한. 단발 pass 는 여전히 0.1~0.2s 로 빠르고,
#     이 값은 *부하 하 최악*에 걸리는 안전망일 뿐이라 넉넉히 잡아 마진을 확보한다.
#   - _drain: drain 실패 시 살아있는 워커를 terminate — 좀비 spawn 워커가 후속 테스트 CPU 를
#     갉아 flake 가 전파되는 2차 피해를 끊는다(핵심). 워커는 예외도 out_q 로 보고해 부모가
#     `_queue.Empty` 위장 대신 진짜 원인을 단언한다.
WORKERS = 4
SYNC_TIMEOUT = 120


# ── board 모듈 로드 + tmp 재배선 (부모·자식 공용) ─────────────────────────────

def _load_board_bound(proj: Path):
    """board.py 를 새로 로드하고 모든 경로 전역을 `proj` tmp 프로젝트로 재바인딩한다.

    부모(monkeypatch)와 자식(프로세스 경계로 monkeypatch 미상속) 양쪽에서 같은 배선을
    쓰도록 함수로 추출. import 시점에 굳은 실 REPO 경로를 tmp 로 전부 덮어쓴다 —
    `.local/board.lock`·`BOARD_FILE`·`TICKETS_DIR` 등 동시성에 관여하는 전역 포함.
    """
    spec = importlib.util.spec_from_file_location("board_conc", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    local = pm / ".local"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "BOARD_LOCK": local / "board.lock",
    }
    for name, val in overrides.items():
        setattr(mod, name, val)
    return mod


def _make_project(root: Path) -> None:
    """tmp 프로젝트 골격 — tickets/{open,claimed,blocked,done}/ + _template.md + .local/."""
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    (tickets / "_template.md").write_text(
        "---\n"
        "id: T-NNNN\n"
        "title: <제목>\n"
        "status: open\n"
        "created: YYYY-MM-DD\n"
        "claimed_by:\n"
        "claimed_at:\n"
        "completed_at:\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
        "# T-NNNN — <제목>\n\n## 목표\n채워라.\n",
        encoding="utf-8",
    )


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 루트 (골격 생성)."""
    p = tmp_path / "proj"
    _make_project(p)
    return p


@pytest.fixture
def board(proj):
    """부모-프로세스용 tmp-바인딩 board 모듈 (단위/단일프로세스 검증)."""
    return _load_board_bound(proj)


class _Args:
    """argparse.Namespace 대용 — cmd_new/cmd_claim 인자 컨테이너."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _new_args(title="t", prefix=None):
    return _Args(title=title, prefix=prefix, touches=None, depends=None,
                 tag=None, estimate="small")


# ════════════════════════════════════════════════════════════════════════
# 자식-프로세스 워커 (top-level — spawn/fork 모두 picklable 해야 함)
# ════════════════════════════════════════════════════════════════════════

def _worker_new(proj_str: str, ready, go, out_q) -> None:
    """동시 `new` 워커 — 배리어(go)에서 일제히 cmd_new 를 호출하고 rc 를 보고한다.

    board_lock 이 ID 발행+파일생성을 직렬화하므로 동시 new 가 ID 를 충돌시키지 않는다.
    """
    proj = Path(proj_str)
    board = _load_board_bound(proj)
    ready.put(os.getpid())
    go.wait()  # 모든 워커가 동시에 진입하도록 배리어
    rc = board.cmd_new(_new_args(title="concurrent"))
    ids = sorted(p.name for p in (board.TICKETS_DIR / "open").glob("T-*.md"))
    out_q.put((rc, ids))


def _worker_claim(proj_str: str, tid: str, ready, go, out_q) -> None:
    """동시 claim 워커 — 같은 ticket 을 일제히 claim 시도하고 *claim 성공 여부 + stderr* 를 보고.

    board 의 atomic rename(`os.rename`)이 락이고, **진 쪽은 FileNotFoundError 로 표면화**된다
    (board.py move_item docstring·`a lost race surfaces as FileNotFoundError`). 다만
    `cmd_claim` 은 `find_ticket` 으로 open path 를 본 뒤 `load_ticket`→`move_ticket` 순으로
    진행하므로, 패배의 FileNotFoundError 가 `load_ticket` *또는* `move_ticket` 어느 쪽 race
    window 에서든 날 수 있다. **ADR-0012 계약(T-0057)**: 두 window 모두 cmd_claim 안에서
    `claim race lost`(stderr) + rc=1 로 *깨끗이* 흡수되어야 한다 — 패배자에게 미처리 traceback 이
    새어나오면 계약 위반이다.

    그래서 워커는 (결과태그, stderr) 튜플을 보고한다:
      - rc==0 → ("WIN", stderr)
      - rc!=0 → ("LOSE", stderr)   # 깨끗한 패배 — 부모가 stderr 에 "claim race lost" 단언
      - cmd_claim 이 던진 모든 예외(FileNotFoundError 포함) → ("EXC", traceback)
        (fix 전: load_ticket race 가 미처리 FileNotFoundError 로 새어 EXC 로 잡힘 = 계약 위반 노출.
         fix 후: 절대 안 나옴 — 두 window 모두 board 안에서 rc=1 로 흡수.)
    워커가 조용히 죽으면 부모 drain 이 행이 되므로 예외도 무조건 보고해 진짜 원인을 드러낸다.
    """
    import contextlib
    import io
    import traceback as _tb
    proj = Path(proj_str)
    board = _load_board_bound(proj)
    ready.put(os.getpid())
    go.wait()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc = board.cmd_claim(_Args(id=tid, session=f"sess-{os.getpid()}"))
        out_q.put(("WIN" if rc == 0 else "LOSE", err.getvalue()))
    except BaseException as e:  # noqa: BLE001 — 예측 못 한 死 = 부모 행, 무조건 보고
        # FileNotFoundError 도 여기로 — fix 후엔 cmd_claim 이 안 던지므로 *절대* 안 와야 한다.
        out_q.put(("EXC", f"{e!r}\n{_tb.format_exc()}\n--stderr--\n{err.getvalue()}"))


def _worker_hold_lock(proj_str: str, acquired) -> None:
    """락을 잡고 *영원히* 멈춘다(부모가 kill). 크래시-시-자동해제 검증용.

    board_lock 컨텍스트 *안*에서 멈춰 락을 보유한 채 죽도록, contextmanager 를 수동 구동한다.
    공유 Event 를 `wait` 하지 않고 단순 sleep 으로 멈춘다 — 공유 동기화 프리미티브를 wait 중인
    프로세스를 kill 하면 그 내부 락이 dead 프로세스에 잡힌 채 남아 부모의 `set()` 이 데드락날
    수 있기 때문(time.sleep 은 공유 락을 안 쥔다).
    """
    proj = Path(proj_str)
    board = _load_board_bound(proj)
    cm = board.board_lock()
    cm.__enter__()        # 락 획득 (컨텍스트 종료 없이 보유)
    acquired.set()
    time.sleep(3600)      # 부모가 terminate 할 때까지 락 보유 채로 대기 (해제 안 함)


def _has_flock_primitive() -> bool:
    """OS 배타락 프리미티브(fcntl/msvcrt)가 있는지 — 없으면 board_lock 은 무락 폴백.

    상호배제를 *전제*하는 결정적 테스트(scan/write mutual-exclusion)는 폴백 환경에선
    배타성이 없어 비적용 → skip 판정에 쓴다. (크래시-해제 테스트의 `_probe_lock_free` 는
    '지금 락이 잡혀있나'를 보는 다른 용도라 혼용 불가 — 무경합 시점엔 항상 free 다.)
    """
    try:
        import fcntl  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401
        return True
    except ImportError:
        return False


def _probe_lock_free(lock_path: Path) -> bool:
    """`lock_path` 에 *비차단* 배타락이 잡히는지 본다 (잡으면 즉시 해제). True=락 free.

    크래시-자동해제 검증의 비차단 프로브 — 블로킹 thread 없이 락 상태를 결정적으로 관찰한다.
    POSIX=fcntl LOCK_NB·Windows=msvcrt LK_NBLCK. 프리미티브 없으면(폴백 무락) 항상 free.
    """
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return True
            except (BlockingIOError, OSError):
                return False
        except ImportError:
            pass
        try:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return True
            except OSError:
                return False
        except ImportError:
            pass
        return True  # 폴백 무락 환경 — 항상 free 로 본다
    finally:
        os.close(fd)


# ── 배리어 헬퍼 ───────────────────────────────────────────────────────────

def _spawn_and_sync(n: int, target, args_factory) -> tuple[list, mp.Queue]:
    """n 개 프로세스를 띄우고 모두 ready 가 되면 go 배리어를 풀어 *동시* 진입시킨다.

    Returns (procs, out_q). 호출부가 join + out_q 수거.
    """
    ctx = mp.get_context("spawn")  # 부모 monkeypatch 비상속 — 자식이 명시 재배선(cross-platform)
    ready = ctx.Queue()
    go = ctx.Event()
    out_q = ctx.Queue()
    procs = [ctx.Process(target=target, args=args_factory(ready, go, out_q))
             for _ in range(n)]
    for p in procs:
        p.start()
    # 모든 워커가 배리어에 도달할 때까지 대기 후 일제 release.
    for _ in range(n):
        ready.get(timeout=SYNC_TIMEOUT)
    go.set()
    return procs, out_q


def _drain(out_q: mp.Queue, n: int, procs: list) -> list:
    """워커 결과 n 개를 수거한다. 실패해도 *남은 워커를 정리*하고 join 한다.

    Queue 에 put 한 자식은 부모가 drain 해야 join 이 막히지 않는다(파이프 버퍼). drain 이
    timeout 으로 깨지면 살아있는 워커를 terminate 한다 — 안 그러면 좀비 spawn 워커가 *후속*
    테스트의 CPU 를 갉아 flake 가 전파되기 때문(첫 flake 의 2차 피해 차단). 그 경우 진짜
    원인(몇 개 받았나·exitcode)을 메시지에 담아 `_queue.Empty` 보다 진단 가능하게 만든다.
    """
    rcs: list = []
    try:
        for _ in range(n):
            rcs.append(out_q.get(timeout=SYNC_TIMEOUT))
    finally:
        # 성공/실패 무관하게 워커를 정리(좀비·CPU 누수 차단). 성공 경로에선 이미 종료 직전.
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)
    assert len(rcs) == n, (
        f"워커 {n} 개 중 {len(rcs)} 개만 결과 보고 — 나머지는 行/死 "
        f"(exitcodes={[p.exitcode for p in procs]})")
    return rcs


# ════════════════════════════════════════════════════════════════════════
# 1. 동시 new → 유일 ID
# ════════════════════════════════════════════════════════════════════════

def test_concurrent_new_yields_unique_ids(proj):
    """N 개 세션이 동시에 `new` → 발행된 티켓 ID 가 모두 distinct(중복 발행 0).

    board_lock 이 `_next_id`(max+1)+파일생성을 직렬화하므로 같은 ID 가 둘 이상 안 나온다.
    """
    n = WORKERS
    procs, out_q = _spawn_and_sync(
        n, _worker_new,
        lambda ready, go, out_q: (str(proj), ready, go, out_q))
    results = _drain(out_q, n, procs)
    assert all(rc == 0 for rc, _ in results), f"new 실패한 워커: {results}"

    # 최종 상태: open/ 의 모든 티켓 ID 가 distinct 하고 정확히 n 개여야 한다.
    board = _load_board_bound(proj)
    files = sorted((board.TICKETS_DIR / "open").glob("T-*.md"))
    # 파일명 T-NNNN-slug.md → ID = 두 번째 '-' 전까지(T-NNNN).
    ids = [f.name[: f.name.index("-", 2)] for f in files]
    assert len(files) == n, f"기대 {n}개, 실제 {len(files)}: {[f.name for f in files]}"
    assert len(set(ids)) == n, f"ID 중복 발행: {ids}"


def test_unlocked_id_issuance_collides_deterministically(board, monkeypatch):
    """sensitivity — 락 없이 `_next_id` 를 *읽기만* 하고 파일을 안 만들면 같은 ID 가 나온다.

    `cmd_new` 가 board_lock 으로 막는 정확한 race 를 결정적으로 박제한다: ID 발행(`_next_id`
    = max+1)과 파일 생성 사이의 틈에 다른 세션이 끼면 같은 ID 를 본다. 락이 이 read→create 를
    원자로 묶어 충돌을 없앤다. (확률적 spawn race 대신 seam 을 직접 노출 — 빠르고 결정적.)
    """
    # 빈 보드 — 두 '세션'이 파일 생성 전에 _next_id 를 각자 읽으면 둘 다 T-0001.
    assert board._next_id(None) == "T-0001"
    assert board._next_id(None) == "T-0001"   # 무락 read-only → 동일 ID (충돌 가능성의 근원)

    # board_lock 안에서 ID 발행+파일생성을 원자로 하면 두 번째는 T-0002 를 본다(직렬화 효과).
    a = _Args(title="first", prefix=None, touches=None, depends=None,
              tag=None, estimate="small")
    b = _Args(title="second", prefix=None, touches=None, depends=None,
              tag=None, estimate="small")
    assert board.cmd_new(a) == 0
    assert board.cmd_new(b) == 0
    ids = sorted(f.name[: f.name.index("-", 2)]
                 for f in (board.TICKETS_DIR / "open").glob("T-*.md"))
    assert ids == ["T-0001", "T-0002"], f"직렬 발행이 distinct ID 를 못 줌: {ids}"


# ════════════════════════════════════════════════════════════════════════
# 2. 동시 같은 ticket claim → 하나만 성공 (기존 atomic rename 원자성 박제)
# ════════════════════════════════════════════════════════════════════════

def test_concurrent_claim_only_one_wins(proj):
    """N 개 세션이 같은 open ticket 을 동시에 claim → 정확히 하나만 성공·**패배자는 깨끗이 실패**.

    `move_ticket` 의 POSIX atomic rename(`os.rename`)이 락 — 진 쪽은 FileNotFoundError 로
    표면화된다(board.py move_item docstring). 이 *기존* 원자성을 회귀로 박제한다(신규 락 없음).

    **검증 의미 (T-0057·ADR-0012 계약)** = 두 가지를 동시에 박제한다:
      1. *안전성* — 정확히 한 세션만 claim 성공·ticket 은 한 번만 claimed 로 이동(분열·중복 0).
      2. *깨끗한 패배* — 패배자는 **미처리 traceback 없이 rc=1 로 깨끗이 실패**해야 한다(EXC 0).
         패배는 타이밍에 따라 세 가지 *깨끗한* 형태 중 하나로 난다(모두 rc=1·traceback 0):
           (a) 늦게 들어와 `find_ticket` 이 이미 claimed/ 상태를 봄 → "cannot claim ... claimed/"
           (b) `find_ticket`→`load_ticket` window race → "claim race lost"
           (c) `find_ticket`→`move_ticket` window race → "claim race lost"
         fix 전엔 (b) 가 미처리 FileNotFoundError 로 새어 EXC(=계약 위반)로 잡혔다. fix 후엔
         (b)도 (c)처럼 board 안에서 rc=1 로 흡수된다. 어느 *깨끗한* 형태로 지든 무해하므로 이
         테스트는 EXC(미처리 traceback)만 fail 로 본다. (b) window 의 결정적 박제 + "claim race
         lost" 메시지 자체는 `test_claim_load_window_race_lost_cleanly`(아래)가 담당한다 —
         확률적 spawn 타이밍에 메시지 문구를 의존하지 않기 위함(이 spawn 테스트의 flake 회피).
    """
    # seed: open/ 에 claim 대상 ticket 하나.
    board = _load_board_bound(proj)
    tid = "T-0001"
    path = board.TICKETS_DIR / "open" / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": "seed", "status": "open",
                             "depends_on": []}, "# seed\n")

    n = WORKERS
    procs, out_q = _spawn_and_sync(
        n, _worker_claim,
        lambda ready, go, out_q: (str(proj), tid, ready, go, out_q))
    results = _drain(out_q, n, procs)  # 각 원소 = (tag, stderr_or_traceback)

    # 패배자가 미처리 예외로 죽었으면(EXC) = 계약 위반. 진짜 traceback 을 단언에 드러낸다
    # (`_queue.Empty` 위장 방지 + fix 회귀 시 load_ticket race 의 FileNotFoundError 가 여기 걸림).
    excs = [detail for tag, detail in results if tag == "EXC"]
    assert not excs, (
        "claim 패배자가 깨끗이 실패하지 않고 미처리 예외로 죽음 (ADR-0012 계약 위반):\n"
        + "\n".join(excs))

    # 정확히 한 워커만 WIN, 나머지는 모두 LOSE — atomic-rename 원자성의 핵심 계약(안전성).
    wins = [r for r in results if r[0] == "WIN"]
    losers = [r for r in results if r[0] == "LOSE"]
    assert len(wins) == 1, f"정확히 1명만 claim 성공해야 함, results={results}"
    assert len(losers) == n - 1, f"패배자 수 불일치: results={results}"

    # 패배자는 모두 *깨끗한* rc=1 패배여야 한다(EXC 위에서 이미 배제). stderr 은 알려진 깨끗한
    # 패배 메시지(claimed/ 선점 또는 race-lost) 중 하나 — 어느 쪽이든 traceback 0·rc=1.
    for tag, stderr in losers:
        assert ("claim race lost" in stderr
                or f"cannot claim {tid}: currently in claimed/" in stderr), (
            f"패배자 stderr 이 알려진 깨끗한 패배 형태가 아님: {stderr!r}")
    # winner 는 어떤 패배 메시지도 내지 않는다(자기 stderr 깨끗).
    win_stderr = wins[0][1]
    assert win_stderr == "", f"승자 stderr 이 비어있지 않음: {win_stderr!r}"

    # 최종: ticket 은 claimed/ 에 정확히 1개·open 에 잔존 0 (분열·중복 없음).
    claimed = list((board.TICKETS_DIR / "claimed").glob(f"{tid}-*.md"))
    assert len(claimed) == 1
    assert not list((board.TICKETS_DIR / "open").glob(f"{tid}-*.md"))


def test_claim_load_window_race_lost_cleanly(board, capsys):
    """sensitivity(결정적) — `find_ticket`→`load_ticket` 사이 window 의 패배를 깨끗이 흡수.

    spawn 워커는 부하·타이밍에 따라 `load_ticket` window 가 안 열릴 수 있어(승자가 너무 빨리
    옮기지 *않으면* 패배가 `move_ticket` window 로만 떨어짐) 그 window 의 계약을 *결정적으로*
    노출하지 못한다. 그래서 그 seam 을 직접 연다: `find_ticket` 이 open path 를 돌려준 *직후*
    승자가 옮긴 효과로 그 파일을 사라지게 하고 `cmd_claim` 을 호출한다.

    ADR-0012(T-0057) 계약: 그 window 에서 `load_ticket(path)` 가 던지는 FileNotFoundError 는
    미처리 traceback 으로 새지 말고 `claim race lost`(stderr) + rc=1 로 흡수되어야 한다.
    (계약 fix 를 되돌리면 load_ticket 이 try 밖이라 이 호출이 미처리 FileNotFoundError 로 죽어
     이 테스트가 fail 을 재현한다 = fix 가 load-bearing — `test_unlocked_id_...` 와 동류의
     결정적 sensitivity, 확률적 spawn race 비의존.)
    """
    tid = "T-0001"
    path = board.TICKETS_DIR / "open" / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": "seed", "status": "open",
                             "depends_on": []}, "# seed\n")

    # find_ticket 을 래핑해 *반환 직후* open path 를 제거 — 승자가 옮긴(rename out) 효과를
    # 결정적으로 재현한다. 이어지는 load_ticket(path) 가 그 사라진 path 를 읽으려다 진다.
    orig_find = board.find_ticket

    def racing_find(t):
        status, p = orig_find(t)
        if t == tid:
            p.unlink()  # 승자가 open/ 밖으로 옮긴 효과
        return status, p

    board.find_ticket = racing_find
    try:
        rc = board.cmd_claim(_Args(id=tid, session="loser"))  # 미처리 예외면 여기서 죽음(fail)
    finally:
        board.find_ticket = orig_find

    err = capsys.readouterr().err
    assert rc == 1, f"load 단계 race 패배 rc 가 1 이 아님: {rc}"
    assert "claim race lost" in err, f"깨끗한 race-lost 메시지 없음: {err!r}"
    # ticket 은 어디로도 안 옮겨졌다(이 세션은 졌고 승자는 시뮬레이션상 별도) — claimed/ 비어야.
    assert not list((board.TICKETS_DIR / "claimed").glob(f"{tid}-*.md"))


def test_claim_normal_rejections_unaffected(board, capsys):
    """동작 보존 — race 통합이 *정상* 거부 경로(status!=open·미충족 depends_on)를 안 건드림.

    race-FileNotFoundError 만 `claim race lost` 로 합치고, 정상 거부는 각자 메시지·rc=1 그대로.
    """
    # (1) status != open → "cannot claim ... currently in claimed/" (race-lost 아님).
    cp = board.TICKETS_DIR / "claimed" / "T-0009-x.md"
    board.dump_ticket(cp, {"id": "T-0009", "title": "x", "status": "claimed",
                           "depends_on": []}, "# x\n")
    assert board.cmd_claim(_Args(id="T-0009", session="s")) == 1
    err = capsys.readouterr().err
    assert "currently in claimed/" in err and "claim race lost" not in err, err

    # (2) 미충족 depends_on → "dependency ... not done" (race-lost 아님).
    dep = board.TICKETS_DIR / "open" / "T-0010-dep.md"
    board.dump_ticket(dep, {"id": "T-0010", "title": "dep", "status": "open",
                            "depends_on": []}, "# dep\n")
    tgt = board.TICKETS_DIR / "open" / "T-0011-tgt.md"
    board.dump_ticket(tgt, {"id": "T-0011", "title": "tgt", "status": "open",
                            "depends_on": ["T-0010"]}, "# tgt\n")
    assert board.cmd_claim(_Args(id="T-0011", session="s")) == 1
    err = capsys.readouterr().err
    assert "is open/, not done" in err and "claim race lost" not in err, err

    # (3) 미존재 dependency → "dependency ... not found" (race-lost 아님·바깥 try 에 안 새어나감).
    tgt2 = board.TICKETS_DIR / "open" / "T-0012-tgt.md"
    board.dump_ticket(tgt2, {"id": "T-0012", "title": "tgt2", "status": "open",
                             "depends_on": ["T-9999"]}, "# tgt2\n")
    assert board.cmd_claim(_Args(id="T-0012", session="s")) == 1
    err = capsys.readouterr().err
    assert "dependency T-9999 not found" in err and "claim race lost" not in err, err

    # (4) 비경합 정상 claim → rc=0·claimed/ 이동.
    ok = board.TICKETS_DIR / "open" / "T-0013-ok.md"
    board.dump_ticket(ok, {"id": "T-0013", "title": "ok", "status": "open",
                           "depends_on": []}, "# ok\n")
    assert board.cmd_claim(_Args(id="T-0013", session="s")) == 0
    assert list((board.TICKETS_DIR / "claimed").glob("T-0013-*.md"))


# ════════════════════════════════════════════════════════════════════════
# 3. 크래시 시 락 자동해제 (OS flock — stale-lock 없음)
# ════════════════════════════════════════════════════════════════════════

def test_lock_auto_released_on_holder_crash(proj):
    """락 보유 프로세스가 죽으면 다음 획득자가 막히지 않는다 (OS flock 자동해제).

    자식이 board_lock 을 잡은 채 멈춘 동안 부모의 *비차단* 프로브는 실패해야 하고(락이
    실제로 잡힘), 자식을 kill 하면 OS 가 락을 해제해 프로브가 성공한다. 블로킹 thread 없이
    비차단 프로브로 결정적·빠르게 stale-lock 부재를 박제한다.
    """
    board = _load_board_bound(proj)
    lock_path = board.BOARD_LOCK

    ctx = mp.get_context("spawn")
    acquired = ctx.Event()
    child = ctx.Process(target=_worker_hold_lock, args=(str(proj), acquired))
    child.start()
    try:
        assert acquired.wait(timeout=SYNC_TIMEOUT), "자식이 락을 획득하지 못함"

        # 폴백 무락 환경(fcntl/msvcrt 둘 다 없음)에선 배타성이 없으니 이 단언을 건너뛴다.
        if _probe_lock_free(lock_path):
            pytest.skip("락 프리미티브 없음(폴백 무락) — 크래시-해제 단언 비적용")

        # 자식 보유 중 — 비차단 프로브 실패(락 잡힘).
        assert not _probe_lock_free(lock_path), "자식 보유 중인데 락이 free (배타성 위반)"

        # 자식 강제 종료 → OS 가 flock 자동 해제.
        child.terminate()
        child.join(timeout=SYNC_TIMEOUT)

        # 크래시 후 락이 풀려 프로브가 성공해야 한다(stale-lock 없음).
        deadline = time.time() + SYNC_TIMEOUT
        while time.time() < deadline:
            if _probe_lock_free(lock_path):
                break
            time.sleep(0.05)
        assert _probe_lock_free(lock_path), "보유자 크래시 후에도 락이 안 풀림 (stale-lock)"
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=10)


# ════════════════════════════════════════════════════════════════════════
# 4. refresh_board — scan+render+write 가 한 락 구간 (ADR-0012 lost-update 방지)
# ════════════════════════════════════════════════════════════════════════

def _seed_open_ticket(board, tid: str) -> None:
    """open/ 에 ticket 하나 seed (refresh_board 가 board.md 에 렌더하도록)."""
    p = board.TICKETS_DIR / "open" / f"{tid}-seed.md"
    board.dump_ticket(p, {"id": tid, "title": f"title-{tid}", "status": "open",
                          "depends_on": [], "touches": [], "tags": []}, "# seed\n")


def _worker_change_then_refresh(proj_str: str, tid: str, ready, go, out_q) -> None:
    """워커가 고유 ticket 을 new 로 만들고(각자) refresh_board 로 board.md 를 재생성한다.

    cmd_new 는 끝에서 refresh_board 를 부른다. N 워커가 일제히 진입하면 N 번의
    scan+render+write 가 경합한다. ADR-0012(T-0057): scan+write 가 한 락 구간이라
    *마지막* writer 가 모든 선행 new 이후 상태를 scan → board.md 가 전 ticket 을 반영.
    scan 을 락 밖으로 되돌리면 stale writer 가 일부 ticket 누락된 board 를 덮어쓸 수 있다.
    """
    proj = Path(proj_str)
    board = _load_board_bound(proj)
    ready.put(os.getpid())
    go.wait()
    rc = board.cmd_new(_new_args(title=f"concurrent-{os.getpid()}"))
    out_q.put((rc, None))


def test_concurrent_refresh_board_reflects_latest_state(proj):
    """N 세션이 각자 new(+refresh_board) → board.md 가 *모든* ticket 을 반영(stale 0).

    cmd_new 끝의 refresh_board 가 경합한다. scan+render+write 가 한 board_lock 구간이라
    마지막 writer 가 전 ticket 을 본다 → board.md 가 최종 상태(open=N)와 일치한다.
    write 만 감쌌다면(scan 락 밖) stale snapshot writer 가 일부 누락 board 를 덮어쓸 수 있다.
    """
    n = WORKERS
    procs, out_q = _spawn_and_sync(
        n, _worker_change_then_refresh,
        lambda ready, go, out_q: (str(proj), None, ready, go, out_q))
    results = _drain(out_q, n, procs)
    assert all(rc == 0 for rc, _ in results), f"new 실패한 워커: {results}"

    board = _load_board_bound(proj)
    open_files = sorted((board.TICKETS_DIR / "open").glob("T-*.md"))
    assert len(open_files) == n, f"기대 {n}개 open ticket, 실제 {len(open_files)}"

    # board.md 가 *최신* 상태를 반영해야 한다 — 모든 ticket ID + 정확한 OPEN 카운트.
    text = board.BOARD_FILE.read_text(encoding="utf-8")
    assert f"OPEN ({n})" in text, (
        f"board.md OPEN 카운트가 최신이 아님(stale write 의심):\n{text}")
    for f in open_files:
        tid = f.name[: f.name.index("-", 2)]
        assert tid in text, f"board.md 에 {tid} 누락(stale write 로 유실):\n{text}"


def test_refresh_board_scan_and_write_are_mutually_exclusive(board):
    """sensitivity(결정적) — refresh_board 의 scan+write 는 다른 refresh 의 write 와 상호배제.

    lost-update 의 핵심 race 를 결정적 seam 으로 박제한다. A 가 scan 을 시작한 직후 멈춘
    동안(scan-pause), B 가 *더 새로운* 상태(ticket 추가)를 통째 refresh(scan+write) 한다.
    이어 A 가 재개해 자기 write 를 한다.

      - fix(scan 락 안): A 의 scan-pause 는 board_lock 보유 중이므로 B 의 refresh_board 가
        락에서 막힌다 → B 는 A 가 완전히 끝난 뒤에야 scan+write → board.md 가 *최신*(2 ticket).
      - broken(scan 락 밖): A 는 락 없이 scan(1 ticket) 후 pause, B 가 끼어들어 2 ticket 을
        write, A 가 write-lock 잡고 stale(1 ticket) 로 덮어씀 → board.md=1 ticket (FAIL).

    스레드로 구동한다(board_lock 은 매 호출 새 fd 를 열어 flock 이 같은 프로세스의 두 fd 도
    상호배제 — 스레드 간 직렬화가 OS 락으로 결정적). flock 미지원 폴백 환경은 skip.
    """
    if not _has_flock_primitive():
        pytest.skip("락 프리미티브 없음(폴백 무락) — scan/write 상호배제 단언 비적용")

    _seed_open_ticket(board, "T-0001")  # 초기 상태 S1 = 1 ticket

    a_scanning = threading.Event()    # A 가 scan 에 진입(첫 ticket 을 load)했음을 알림
    a_may_finish = threading.Event()  # 테스트가 A 의 scan 재개를 허용

    orig_load = board.load_ticket
    state = {"paused": False}

    def hooked_load(p):
        # A 의 첫 scan load 에서 한 번만 멈춘다(B 가 끼어들 틈을 결정적으로 연다).
        # state.paused=True 이후엔(=B 의 scan 및 A 의 잔여 scan) 그냥 통과.
        fm = orig_load(p)
        if not state["paused"]:
            state["paused"] = True
            a_scanning.set()
            assert a_may_finish.wait(timeout=SYNC_TIMEOUT), "A 재개 신호 timeout"
        return fm

    err: dict[str, BaseException] = {}

    def run_a():
        try:
            board.refresh_board()  # A: scan(첫 load 에서 pause) → render → write
        except BaseException as e:  # noqa: BLE001
            err["a"] = e

    board.load_ticket = hooked_load
    a = threading.Thread(target=run_a)
    a.start()
    try:
        assert a_scanning.wait(timeout=SYNC_TIMEOUT), "A 가 scan 에 진입하지 못함"

        # A 가 scan-pause 중 — 더 새로운 상태 S2 = 2 ticket 으로 만들고 B 가 통째 refresh.
        _seed_open_ticket(board, "T-0002")

        b_done = threading.Event()

        def run_b():
            try:
                board.refresh_board()  # B: 2 ticket scan+write (hook 은 통과만)
            except BaseException as e:  # noqa: BLE001
                err["b"] = e
            finally:
                b_done.set()

        b = threading.Thread(target=run_b)
        b.start()

        # fix 라면 B 는 A 의 락 때문에 *여기서 막혀* b_done 이 안 떨어진다(A 가 아직 보유).
        # broken 이라면 B 가 자유로이 진행해 곧 b_done. 어느 쪽이든 최종 단언이 진실을 가른다.
        time.sleep(0.3)

        # A 재개 → A 가 scan(락 안이면 S1=1 ticket 만 봤음) 마치고 write, 락 해제.
        a_may_finish.set()

        a.join(timeout=SYNC_TIMEOUT)
        assert b_done.wait(timeout=SYNC_TIMEOUT), "B refresh 가 끝나지 않음"
        b.join(timeout=SYNC_TIMEOUT)
    finally:
        a_may_finish.set()  # 누수 방지
        board.load_ticket = orig_load

    assert not err, f"refresh 스레드 예외: {err}"

    # 최종 board.md 는 *최신* 상태(2 ticket)를 반영해야 한다.
    #   - fix: B 가 A 뒤에 락을 잡아 2 ticket 을 scan+write → 최신.
    #   - broken: A 가 마지막에 stale 1-ticket 을 덮어써 OPEN(1) → 이 단언 FAIL(재현).
    text = board.BOARD_FILE.read_text(encoding="utf-8")
    assert "OPEN (2)" in text, (
        "board.md 가 최신(2 ticket)을 반영하지 않음 — scan 이 락 밖이면 stale write 재현:\n"
        + text)
    assert "T-0001" in text and "T-0002" in text, f"ticket 누락:\n{text}"


# ════════════════════════════════════════════════════════════════════════
# _append_atomic (O_APPEND 원자 추가)
# ════════════════════════════════════════════════════════════════════════

def test_append_atomic_creates_and_appends(board, tmp_path):
    """_append_atomic — 파일 없으면 생성하고, 있으면 끝에 원자 추가(덮어쓰기 아님)."""
    f = tmp_path / "log.md"
    board._append_atomic(f, "line1\n")
    board._append_atomic(f, "line2\n")
    assert f.read_text(encoding="utf-8") == "line1\nline2\n"


def test_areas_append_uses_atomic_append(board):
    """areas_append 가 O_APPEND 경로를 타고도 기존 계약(생성·append·append-only) 유지."""
    board.areas_append("PAY", "결제", "alice")
    board.areas_append("ACC", "정산", "bob")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert text.count("| repo | prefix | git | test_cmd | owner |") == 1   # 헤더 한 번만 (ADR-0014 per-repo 스키마)
    assert "| PAY | PAY |  |  | alice |" in text   # repo 미지정 → prefix·git/test_cmd 빈칸
    assert "| ACC | ACC |  |  | bob |" in text
    assert board.registered_prefixes() == {"PAY", "ACC"}


def _worker_areas(proj_str: str, idx: int, ready, go) -> None:
    """워커별 고유 prefix(P{idx})를 areas.md 에 동시 등록(O_APPEND 원자성 검증)."""
    proj = Path(proj_str)
    board = _load_board_bound(proj)
    ready.put(idx)
    go.wait()
    board.areas_append(f"P{idx}", f"area-{idx}", f"owner-{idx}")


def test_concurrent_areas_append_no_lost_rows(proj):
    """동시 areas 등록(O_APPEND) → 모든 행 보존(lost update 0).

    각 워커가 고유 prefix(P0..P{n-1})를 *동시에* append 한다. O_APPEND 가 각 추가를 원자로
    처리하므로 한 워커가 다른 워커의 행을 덮어쓰지 않는다 → 전 prefix 보존.
    """
    # 헤더를 먼저 만들어 둔다(생성 race 와 무관하게 append 만 측정).
    board = _load_board_bound(proj)
    board.areas_append("SEED", "씨앗", "root")

    n = WORKERS
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    go = ctx.Event()
    procs = [ctx.Process(target=_worker_areas, args=(str(proj), i, ready, go))
             for i in range(n)]
    for p in procs:
        p.start()
    for _ in range(n):
        ready.get(timeout=SYNC_TIMEOUT)
    go.set()
    try:
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)
    finally:
        # 행 워커가 남으면 후속 테스트 CPU 를 갉아 flake 전파 — 정리한다.
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)
    assert all(p.exitcode == 0 for p in procs), (
        f"areas 워커 비정상 종료: exitcodes={[p.exitcode for p in procs]}")

    prefixes = board.registered_prefixes()
    # SEED + 워커별 고유 prefix n 개 = n+1 개 전부 보존.
    expected = {"SEED"} | {f"P{i}" for i in range(n)}
    assert prefixes == expected, f"lost rows: {expected - prefixes}"


def test_concurrent_areas_append_from_empty_header_once_all_rows(proj):
    """헤더 *없는* 상태에서 동시 areas_append N개 → 헤더 1회·모든 row 보존(클로버 0).

    must-fix 2(ADR-0012): areas_append 가 헤더 생성(if-absent)+row append 를 한 board_lock
    구간으로 원자화한다. 락이 없으면 동시 최초 등록 2개가 둘 다 "not exists" 를 보고 → 둘 다
    헤더를 write_text 해 한쪽이 다른쪽 append row 를 클로버한다(row 만 O_APPEND 라도 헤더
    race 가 남음). sensitivity: 헤더 생성을 락 밖 write_text 로 되돌리면 헤더 중복·row 유실 재현.

    (위 `..._no_lost_rows` 는 헤더를 SEED 로 *선생성*해 헤더 race 를 안 탄다 — 이 테스트만
     헤더-없음에서 시작해 최초-생성 race 를 결정적으로 노출한다.)
    """
    # 헤더 선생성 안 함 — AREAS_FILE 가 아예 없는 상태에서 N 워커가 *동시에* 최초 등록.
    board = _load_board_bound(proj)
    assert not board.AREAS_FILE.exists(), "테스트 전제: areas.md 부재"

    n = WORKERS
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    go = ctx.Event()
    procs = [ctx.Process(target=_worker_areas, args=(str(proj), i, ready, go))
             for i in range(n)]
    for p in procs:
        p.start()
    for _ in range(n):
        ready.get(timeout=SYNC_TIMEOUT)
    go.set()
    try:
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)
    assert all(p.exitcode == 0 for p in procs), (
        f"areas 워커 비정상 종료: exitcodes={[p.exitcode for p in procs]}")

    text = board.AREAS_FILE.read_text(encoding="utf-8")
    # 헤더는 정확히 한 번(동시 최초 등록에도 중복 생성 0).
    assert text.count("| repo | prefix | git | test_cmd | owner |") == 1, (
        f"헤더가 1회가 아님(최초 생성 race — 헤더 중복):\n{text}")
    assert text.count("# Area Registry") == 1, f"헤더 블록 중복:\n{text}"
    # 모든 워커 row 보존(클로버 0).
    prefixes = board.registered_prefixes()
    expected = {f"P{i}" for i in range(n)}
    assert prefixes == expected, f"lost rows: {expected - prefixes} (전체={prefixes})"


# 참고: hermetic 입증 — 실 루트 .local/board.lock 가 테스트로 안 생기는지.
def test_real_root_local_dir_untouched_by_tmp(board):
    """tmp-바인딩 board 가 실 루트 .local 을 안 건드리는지 가드(경로 재배선 확인)."""
    assert board.BOARD_LOCK != REAL_LOCAL_DIR / "board.lock", "BOARD_LOCK 가 tmp 로 재배선 안 됨"
