#!/usr/bin/env python3
"""PM 부기 자동화 헬퍼 — ticket 완료 시 기계적 부기를 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — pytest tests/ -q. red 면 즉시 중단.
  2. log/current.md 스켈레톤 append — 표준 형식 entry 골격.
  3. board.py complete 호출 — 회귀를 이미 통과했으므로 --tests-pass.
  4. git add -A — 스테이징. commit 은 PM 이 한다.
  5. 잔여 PM 수동 작업 출력.

결정 (T-0064 / T-0103):
  - subprocess DI: pytest/git/board.py subprocess 는 주입 가능한 함수로 감싼다.
  - red 면 중단: log/current.md / board / git 어떤 것도 건드리지 않는다.
  - status.md 는 더 이상 건드리지 않는다 (ADR-0023 a안 — status.md = judgment-only).
    테스트 수·합계·소계·회귀 실측은 derivable(pytest/board.py regression 실측·log history)이라
    status.md 에 손으로 박제하지 않는다. 이 도구는 status.md 미접촉.
  - 모듈 행·서술·commit 은 자동화하지 않는다 (v1 축소판 — §배경).
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확한 메시지).
  - LLM 미호출 — stdlib + board.py import 만.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored)
AREAS_FILE = REPO / ".project_manager" / "areas.md"  # shared per-repo registry (ADR-0014)


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable


def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


# ── 회귀 명령 해소 (per-repo·ADR-0014) ──────────────────────────────────
#
# multi-PM(multi-PM) 모델에선 활성 repo 가 비-Python(Go 등)일 수 있어 `pytest tests/ -q`
# 가 틀린다 — 회귀는 **활성 repo 의 per-repo test_cmd**(areas.md 레지스트리)를 써야
# 한다(ADR-0014). board.py 의 `_test_cmd` 가 그 우선순위(override > areas.md 활성 prefix
# 행 > local.conf > 기본)의 단일 진실이므로 import 해 재사용한다.
#
# **솔로/프레임워크 자기 회귀(=현행 `pytest tests/ -q` venv 실행)는 반드시 보존**한다:
# areas.md 없음 / 활성 prefix 없음 / 그 행의 test_cmd 빈 값이면 *multi-PM 오버라이드가
# 아니므로* None 을 돌려, 호출부가 현행 하드코딩 argv 를 그대로 쓰게 한다(board 의
# 솔로 폴백 `pytest -q` 와 달리 venv 인터프리터·`tests/` 경로를 보존 — 도그푸딩 불변).

def _load_board_module():
    """board.py 를 경로 import 해 모듈로 반환한다 (실패 시 None).

    별도 함수로 둔 건 테스트가 areas.md/local.conf 해소를 hermetic 하게 가로채는 seam —
    board 의 areas/local 경로 전역을 tmp 로 재바인딩한 모듈을 주입할 수 있게 한다.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_test_cmd", BOARD_PY)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _resolve_per_repo_test_cmd() -> str | None:
    """multi-PM 모드 활성 repo 의 per-repo test_cmd(문자열)를 해소한다. 솔로면 None.

    board.py 를 import 해 areas.md 레지스트리 해소(`id_prefix`·`_areas_row_for_prefix`)를
    재사용한다 — areas.md 가 있고 활성 prefix 의 행에 비어 있지 않은 `test_cmd` 가 있을
    때만 그 문자열을 반환한다. 그 외(솔로·미등록·빈 값·import 실패)는 None(현행 보존).
    """
    if not AREAS_FILE.exists():
        return None
    mod = _load_board_module()
    if mod is None:
        return None
    try:
        prefix = mod.id_prefix()
        if not prefix:
            return None
        row = mod._areas_row_for_prefix(prefix)
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    except Exception:
        return None
    return None


# ── pytest 출력 파서 ────────────────────────────────────────────────────

def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, deselected) 를 파싱한다.

    반환: (passed, deselected) — 파싱 실패 시 None.

    pytest -q 요약 라인 형식 예:
      "1472 passed, 24 deselected in 12.34s"
      "1472 passed in 12.34s"
      "5 failed, 1467 passed, 24 deselected in 10.00s"

    red (failed > 0) 여부 판단은 호출 측이 한다 (failed 수 포함 파싱은 하지 않음).
    반환값 (passed, deselected) 만 추출한다.
    """
    passed_match = re.search(r"(\d+) passed", output)
    deselected_match = re.search(r"(\d+) deselected", output)

    if passed_match is None:
        return None

    passed = int(passed_match.group(1))
    deselected = int(deselected_match.group(1)) if deselected_match else 0
    return passed, deselected


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    returncode 도 함께 검사한다 — returncode != 0 이면(인터럽트·부분 출력 등)
    명확한 'N passed' 가 있어도 green 으로 오판하지 않는다.
    """
    if returncode != 0:
        return False
    if re.search(r"\d+ failed", output):
        return False
    if re.search(r"\d+ passed", output):
        return True
    return False


# ── board.py 연동 ───────────────────────────────────────────────────────

def count_board_done(board_py: Path) -> int:
    """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

    board.py 를 직접 import 해 find_ticket / STATUS_DIRS 를 활용한다.
    실패 시 -1 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper", board_py)
    if spec is None:
        return -1
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        done_dir = mod.TICKETS_DIR / "done"
        return len(list(done_dir.glob("T-*.md")))
    except Exception:
        return -1


def get_ticket_title(board_py: Path, ticket_id: str) -> str:
    """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

    실패 시 빈 문자열 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper2", board_py)
    if spec is None:
        return ""
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _status, path = mod.find_ticket(ticket_id)
        fm, _body = mod.load_ticket(path)
        return fm.get("title") or ""
    except Exception:
        return ""


def get_ticket_touches(board_py: Path, ticket_id: str) -> list[str]:
    """ticket_id 의 frontmatter `touches`(파일/디렉토리 경로 목록)를 board.py 로 읽는다.

    문자열 원소만 취한다(비-문자열 오기는 버림). board 미로드·ticket 부재/깨짐 →
    [](graceful·crash 0 — soft 알림은 막지 않는다). domain soft 알림 step 이 쓴다.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper3", board_py)
    if spec is None:
        return []
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _status, path = mod.find_ticket(ticket_id)
        fm, _body = mod.load_ticket(path)
    except Exception:
        return []
    touches = fm.get("touches")
    if isinstance(touches, str):
        return [touches.strip()] if touches.strip() else []
    if isinstance(touches, list):
        # --touches CLI 와 동형: 각 원소 strip·빈 값/비-문자열 drop (silent-miss 방어).
        return [t.strip() for t in touches if isinstance(t, str) and t.strip()]
    return []


# ── domain 연동 (soft 알림·ADR-0018 #2) ──────────────────────────────────
#
# 순환 없음: domain→board / ticket_finish→board,domain / board 는 둘 다 import 안 함.
# domain.py 부재(솔로/신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).

DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"


def _load_domain_module():
    """domain.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    board.py·areas 해소와 동일한 deep-import seam — 테스트가 hermetic 하게 대역을
    주입하거나 None(부재)을 흉내낼 수 있다.
    """
    import importlib.util
    if not DOMAIN_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_domain_soft", DOMAIN_PY)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def affected_domain_titles(ticket_id: str, board_py: Path) -> list[tuple[str, bool | None]] | None:
    """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록을 돌려준다.

    각 원소 = `(title, stale)` — stale 은 `domain.page_stale`(True=낡음·False=fresh·
    None=판정불가/unknown). soft step 이 stale True 줄 앞에 ⚠ 를 단다(visibility·ADR-0018 #3).
    domain.py 부재·로드 실패 → None (호출부가 조용히 skip — 솔로/신규 clone 무영향).
    touches 부재·영향 0 → [](빈 알림). domain.pages_for_touches 재사용(중복 매칭 0).

    **git_runner 1회 생성해 공유** — 영향 페이지마다 새로 만들지 않고 한 runner 를
    page_stale 에 넘긴다(reviewer suggestion·subprocess 셋업 중복 회피). page_stale 은
    그 자체로 fail-soft(예외/git 부재→None)지만, stale 산출 단계 전체를 한 번 더 try 로
    감싸 어떤 예외도 무표시(None)로 흡수한다 — 비차단·graceful 계약 불변.
    """
    domain = _load_domain_module()
    if domain is None:
        return None
    touches = get_ticket_touches(board_py, ticket_id)
    # touches 가 비면 매칭 0 확정 — load_pages 스캔(깨진 페이지 warning 포함) 자체를 건너뛴다.
    if not touches:
        return []
    try:
        pages = domain.pages_for_touches(touches, domain.load_pages())
    except Exception:
        return None
    # git_runner 를 한 번만 만든다(REPO 컨텍스트) — 페이지마다 subprocess 셋업 반복 방지.
    # 생성 자체가 실패하면 stale 은 전부 unknown(None) 으로 두고 계속(비차단).
    try:
        git_runner = domain._real_git_runner(REPO)
    except Exception:  # noqa: BLE001 — runner 생성 실패는 stale unknown 으로 흡수.
        git_runner = None
    out: list[tuple[str, bool | None]] = []
    for page in pages:
        try:
            stale = domain.page_stale(page, git_runner=git_runner)
        except Exception:  # noqa: BLE001 — stale 못 구하면 무표시(None)·비차단.
            stale = None
        out.append((page["title"], stale))
    return out


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

# 회귀 baseline 은 *실측* new_total 1줄만 남긴다 (ADR-0008 lean baseline·ADR-0023 — 직전
# 합계는 status.md 에 박제하지 않으므로 delta 는 PM 이 서술로 채운다·history 단일 진실=log).
LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | {ticket_id} — {title}

- <!-- PM: 무엇을·왜 서술 -->
- 테스트: 회귀 {new_total} / {new_total} (실측 · 직전 대비 delta 는 PM 서술).
- board: done {board_before}→{board_after}.
"""


def build_log_skeleton(
    ticket_id: str,
    title: str,
    new_total: int,
    board_before: int,
    board_after: int,
    entry_type: str = "<!-- feat/fix/verify/… -->",
    date: str | None = None,
) -> str:
    if date is None:
        date = datetime.date.today().isoformat()
    return LOG_SKELETON_TEMPLATE.format(
        date=date,
        entry_type=entry_type,
        ticket_id=ticket_id,
        title=title,
        new_total=new_total,
        board_before=board_before,
        board_after=board_after,
    )


# ── 핵심 흐름 ──────────────────────────────────────────────────────────

class TicketFinisher:
    """PM 부기 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    broker/dispatch.py 의 clock_fn/sleep_fn DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        board_count_fn: Callable[[], int] | None = None,
        ticket_title_fn: Callable[[str], str] | None = None,
        affected_domain_fn: Callable[[str], list[tuple[str, bool | None]] | None] | None = None,
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        venv_python: str | Path = _default_python(),
        regression_cwd: str | Path | None = None,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        self._venv_python = venv_python
        # 회귀 cwd seam (ADR-0014) — multi-PM 모델은 활성 repo 의 worktree 에서 회귀를 돌려야
        # 한다(multi-PM 루트엔 코드/테스트 없음·spike §8-4 c). 주입 시 그 경로, 미주입(솔로/multi-PM-
        # 미배선)은 REPO. 실제 worktree 경로 결정/주입은 T-0060(bootstrap 리스)의 일이다.
        self._regression_cwd = str(regression_cwd) if regression_cwd else str(REPO)

        # subprocess DI — 기본값은 실제 subprocess 호출
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_git_fn = run_git_fn or self._default_run_git

        # board 조회 DI — 기본값은 실 board.py import 구현
        self._board_count_fn = board_count_fn or self._default_board_count
        self._ticket_title_fn = ticket_title_fn or self._default_ticket_title

        # domain soft 알림 DI (ADR-0018 #2) — 기본값은 실 domain.py import 구현.
        # None 반환 = domain 부재/로드 실패(조용히 skip). 막지 않음(soft).
        self._affected_domain_fn = affected_domain_fn or self._default_affected_domain

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소(ADR-0014 per-repo):
          - **multi-PM 모드** — 활성 repo 의 per-repo test_cmd(areas.md)가 있으면 그 문자열을
            shell 로 실행(board.py 회귀와 동형·비-Python repo 수용).
          - **솔로/프레임워크 자기 회귀** — per-repo cmd 가 없으면 현행 그대로
            `[venv_python, -m, pytest, tests/, -q]` venv argv(도그푸딩 불변·하위호환).

        cwd 는 회귀 seam(`self._regression_cwd`·ADR-0014) — 솔로/multi-PM-미배선은 REPO,
        주입(T-0060 bootstrap) 시 활성 repo 의 worktree. 명령·cwd 모두 활성 repo 정합.
        """
        per_repo_cmd = _resolve_per_repo_test_cmd()
        if per_repo_cmd:
            # multi-PM — per-repo test_cmd 문자열을 shell 로(board.py regression run 과 동형).
            result = subprocess.run(
                per_repo_cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._regression_cwd,
            )
        else:
            # 솔로/프레임워크 자기 회귀 — 현행 venv pytest argv 보존(불변).
            result = subprocess.run(
                [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._regression_cwd,
            )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        """board.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_board_count(self) -> int:
        """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

        실패 시 -1 반환.
        """
        return count_board_done(self._board_py)

    def _default_ticket_title(self, ticket_id: str) -> str:
        """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

        실패 시 빈 문자열 반환.
        """
        return get_ticket_title(self._board_py, ticket_id)

    def _default_affected_domain(self, ticket_id: str) -> list[tuple[str, bool | None]] | None:
        """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록 (soft 알림).

        domain.py 부재/로드 실패 → None (조용히 skip). domain.pages_for_touches 재사용.
        """
        return affected_domain_titles(ticket_id, self._board_py)

    # ── 메인 흐름 ────────────────────────────────────────────────────

    def run(
        self,
        ticket_id: str,
        section: str | None,
        dry_run: bool,
    ) -> int:
        """ticket_id 완료 부기 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).

        `section` 은 후방호환용으로 받기만 하고 무시한다 — status.md 합계표 섹션 행은
        ADR-0023(a안) 으로 제거됐다(judgment-only·테스트 수는 박제 안 함).
        """
        del section  # ADR-0023 — status 합계표 제거로 더 이상 쓰지 않음(후방호환 수용만).
        print(f"[ticket_finish] {ticket_id} 완료 부기 시작 (dry_run={dry_run})")

        # ── 1. 회귀 실행 ──────────────────────────────────────────────
        # dry-run 도 pytest 를 실제 실행한다 — "부작용 없음"이지 "빠름"이 아니다.
        # 파일·board·git 편집만 생략하므로 pytest 실행은 항상 수행.
        print("\n[1/5] 회귀 실행 중...")
        if dry_run:
            print("  [dry-run] pytest tests/ -q 실행 중 (파일·board·git 편집만 생략)...")
        returncode, output = self._run_pytest_fn()
        print(output.rstrip())

        if not is_pytest_green(output, returncode):
            print(
                "\n[중단] 회귀 red — log/current.md·board·git 어떤 것도 건드리지 않는다.",
                file=sys.stderr,
            )
            print(
                "원인: pytest 가 실패를 보고했거나 출력 파싱 실패.",
                file=sys.stderr,
            )
            return 1

        parsed = parse_pytest_output(output)
        if parsed is None:
            print(
                "\n[중단] pytest 출력 파싱 실패 — passed 수를 읽지 못했다.",
                file=sys.stderr,
            )
            return 1

        new_total, deselected = parsed
        print(f"\n  ✓ green: passed={new_total}, deselected={deselected}")

        # status.md 는 더 이상 갱신하지 않는다 (ADR-0023 a안 — judgment-only).
        # 테스트 수는 위 pytest 실측이 단일 진실·history 는 아래 log skeleton 으로 남는다.

        # ── 2. log/current.md 스켈레톤 append ────────────────────────────────
        print("\n[2/5] log/current.md 스켈레톤 append...")
        board_before = self._board_count_fn()
        board_after = board_before + 1  # board complete 후 +1

        title = self._ticket_title_fn(ticket_id)
        if not title:
            title = f"<{ticket_id} 제목을 읽지 못했습니다>"

        skeleton = build_log_skeleton(
            ticket_id=ticket_id,
            title=title,
            new_total=new_total,
            board_before=board_before,
            board_after=board_after,
        )

        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + skeleton, encoding="utf-8")
            print(f"  ✓ log/current.md 스켈레톤 append ({ticket_id})")

        # ── 3. board.py complete ──────────────────────────────────────
        print("\n[3/5] board.py complete...")
        if dry_run:
            print(f"  [dry-run] board.py complete {ticket_id} --tests-pass")
        else:
            board_rc, board_out = self._run_board_fn(
                ["complete", ticket_id, "--tests-pass"]
            )
            print(f"  {board_out.rstrip()}")
            if board_rc != 0:
                print(
                    f"\n[중단] board.py complete 실패 (rc={board_rc}). "
                    "log/current.md 는 이미 편집됐다.",
                    file=sys.stderr,
                )
                return 1
            print(f"  ✓ board: {ticket_id} → done")

        # ── 4. git add -A ─────────────────────────────────────────────
        print("\n[4/5] git stage (git add -A)...")
        if dry_run:
            print("  [dry-run] git add -A (실제 실행 생략)")
        else:
            git_rc, git_out = self._run_git_fn(["add", "-A"])
            if git_rc != 0:
                print(
                    f"\n[중단] git add -A 실패 (rc={git_rc}): {git_out.rstrip()}",
                    file=sys.stderr,
                )
                return 1
            print("  ✓ git add -A 완료 (commit 은 아직 안 했다)")

        # ── 5. 잔여 PM 작업 출력 ─────────────────────────────────────
        print("\n[5/5] PM 이 손으로 할 잔여 작업:")
        print("  ① log/current.md 서술 불릿 채우기 — <!-- PM: 무엇을·왜 서술 --> 를 실제 내용으로 교체")
        print("  ② status.md 모듈 행(상태 + 비고) — 변경된 모듈 행 판정을 architect/PM 이 직접 갱신 (테스트 수는 박제 안 함·ADR-0023)")
        print("  ③ git commit — 메시지는 PM 이 작성 (Co-Authored-By: Claude 트레일러 포함)")

        # ── soft 알림: 영향받는 domain 페이지 (ADR-0018 #2·U2·비차단) ──────
        # 정보일 뿐 게이트가 아니다 — 완료 흐름·rc 를 막지 않는다(예외도 삼킨다).
        # domain.py 부재(솔로/신규 clone) → None → 조용히 skip(무영향).
        self._notify_affected_domain(ticket_id)

        if dry_run:
            print("\n[dry-run] 완료 — 실제 편집·board·git 는 실행하지 않았다.")
        else:
            print(f"\n[완료] {ticket_id} 부기 완료.")

        return 0

    def _notify_affected_domain(self, ticket_id: str) -> None:
        """이 ticket 이 건드린 영역의 domain 페이지를 soft 알림으로 출력한다 (비차단).

        영향 페이지가 stale(covers 코드가 page updated 후 커밋·ADR-0018 #3)이면 그 줄 앞에
        `⚠` 를 단다 — fresh(False)/unknown(None)은 무표시. 도그푸딩/multi-PM 어디서든 완료를
        절대 막지 않는다 — domain 부재·예외는 조용히 삼키고(crash 0), 영향 0 이면 한 줄
        안내만 낸다. dry-run/실행 동일(정보 출력만). stale 못 구해도(예외/unknown) 비차단.
        """
        print("\n[domain] 영향받는 domain 페이지 (soft·비차단):")
        try:
            affected = self._affected_domain_fn(ticket_id)
        except Exception:  # noqa: BLE001 — soft 알림은 완료를 막지 않는다.
            affected = None
        if affected is None:
            print("  (domain 레이어 없음 — skip)")
        elif not affected:
            print("  (영향 domain 페이지 없음)")
        else:
            # 각 영향 페이지: stale(True) 줄 앞에 ⚠ — fresh/unknown 은 무표시.
            labels = [f"⚠ {title}" if stale is True else title for title, stale in affected]
            joined = ", ".join(labels)
            print(f"  📝 이 ticket 이 건드린 영역 domain 페이지: [{joined}] — 갱신 확인(soft)")

# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_finish.py",
        description="ticket 완료 시 PM 부기 자동화 헬퍼 (v1 축소판).",
    )
    parser.add_argument("ticket_id", metavar="T-NNNN", help="완료할 ticket ID")
    parser.add_argument(
        "--section",
        metavar="섹션명",
        default=None,
        help=(
            "(deprecated·no-op) status.md 합계표 섹션 행은 ADR-0023 으로 제거됐다 — "
            "받기만 하고 무시한다(후방호환). status.md = judgment-only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="편집·board·git 없이 무엇을 바꿀지만 출력한다.",
    )
    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    finisher = TicketFinisher()
    return finisher.run(
        ticket_id=args.ticket_id,
        section=args.section,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
