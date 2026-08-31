"""tests/ 전역 conftest — codex ambient env 중화(autouse) + codex 라이브 하네스 공용 헬퍼 (ADR-0070 T-0407)
+ 병렬 실행(pytest-xdist) 경고 되살리기 (T-0757) + pytest 자기 프로세스 UTF-8 reader 선언 (T-0762).

네 관심사를 담는다:

1. **codex ambient env 중화 (autouse·T-0405 리뷰 forward-note)** — 회귀를 codex 하네스로 돌리면
   (에이전트=codex 세션이 pytest 를 기동) 그 세션의 ambient `CODEX_THREAD_ID`/`CODEX_CI` 가 pytest
   프로세스 env 로 새어들어와 `pm_bootstrap._is_codex_harness()` 를 참으로 만든다 → 부트스트랩 카드
   codex 절이 주입돼 **카드-정확일치 단언 테스트**(자체 monkeypatch 미보유 파일)가 깨진다. 세션 전역
   autouse 로 기계 테스트에 codex-미감지 baseline 을 준다 — 어느 하네스로 회귀를 돌려도 동일 결과
   (hermetic). 마커를 *원하는* 테스트(codex-card 케이스 등)는 이 fixture 뒤에 자기 monkeypatch.setenv
   로 되살린다(setenv 가 delenv 를 override — 같은 MonkeyPatch 인스턴스, 나중이 이김).

2. **codex 라이브 공용 헬퍼** — `test_pm_adr_live.py`·`test_pm_ticket_live.py`·`test_pm_relay_codex.py`
   가 공유하는 격리 CODEX_HOME + auth 준비/정리 + `codex exec` 실행 조립. adopter import 헬퍼가 있는
   `test_fresh_adopter_runtime_smoke.py`(T-0408 병렬 소유)를 건드리지 않으려 codex-전용 라이브 인프라는
   여기 한곳에 둔다(중복 금지). 라이브 규율(세션 실측·ADR-0070 D7): 격리 CODEX_HOME(실 ~/.codex/auth.json
   복사 후 테스트 종료 시 삭제) + `codex exec … stdin=DEVNULL`(미닫힘 시 무기한 대기 실측·spike §D3).

3. **병렬 실행 경고 되살리기 (T-0757)** — 회귀를 pytest-xdist 로 돌리면 워커에서 난 경고를
   컨트롤러가 클래스 이름으로 다시 import 해 되살린다. 파일 경로로 연 모듈의 이름은 컨트롤러에
   없어 import 가 실패하고, xdist 가 그 실패를 잡지 않아 경고 한 건이 실행 전체를 INTERNALERROR 로
   죽인다. 아래 ③ 이 그 되살리기를 실패-연성으로 감싼다.

4. **pytest 자기 프로세스 UTF-8 reader 선언 (T-0762)** — Windows 단독 실행(xdist 없음)에서 부모 셸이
   PowerShell 이면, 엔진 CLI(`board`·`pm_update` 등)의 `main()` 을 in-process 로 호출하는 테스트가
   `configure_console_utf8()` 을 통해 조상 체인을 PowerShell 캡처로 오판하고 비-tty 인 pytest 캡처
   스트림을 cp949·`pm_translit` 로 reconfigure 한다 — capsys 경로는 strict UTF-8 디코드에서 즉시
   깨지고, capsys 없는 테스트는 세션 1회 생성되는 전역 fd capture 가 영구 오염돼 이후 모든
   setup·teardown 이 error 가 된다(xdist 는 워커 부모가 `python.exe` 라 재현되지 않는다). 엔진은 이미
   이 캡처 하네스가 UTF-8 reader 임을 선언받는 탈출구를 갖고 있다
   (`console_encoding._utf8_reader_requested()` — `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`). 그
   탈출구는 자식 env 가 아니라 **호출하는 프로세스 자신의** `os.environ` 을 읽으므로, pytest
   자기 프로세스에 직접 심어야 발화한다. 아래 ④ 가 모듈 로드 시점(=pytest 프로세스 기동 시점)에
   1회 선언한다 — Windows 캡처 전환 자체를 검증하는 `tests/test_console_encoding.py` 는
   `_install_windows()` 헬퍼가 테스트별로 이 값을 `monkeypatch.delenv` 해 지우므로 충돌하지 않는다.
   **불변식(F-002)**: 이 선언은 `os.environ` 대입이라 이 프로세스가 스폰하는 자식에게도
   상속된다 — 자식 인코딩(cp949 등)을 검증하는 가드는 이 ambient 를 전제하지 말고 자기
   env 를 명시해야 한다(현존 3곳은 이미 그렇다 — `test_console_encoding.py`의
   `_install_windows()`·`test_machine_output_encoding.py:825`·`test_subprocess_encoding.py:17`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# ── ④ pytest 자기 프로세스 UTF-8 reader 선언 (T-0762) ───────────────────────────
#
# 모듈 docstring ④절 참고. fixture 가 아니라 모듈 로드 시점 top-level 문이다 — import 시점이
# 곧 이 pytest 프로세스의 기동 시점이라 어떤 테스트보다도 먼저 선언되고, xdist 워커도 이 파일을
# 각자 독립 프로세스에서 import 하므로 워커마다 동일하게 선언된다. `_utf8_reader_requested()`
# 는 `os.environ` 을 직접 읽으므로(모듈 재로드·`cache=False` 두 번 로드와 무관), 여기서 한 번
# 심으면 그 뒤 어떤 경로로 `configure_console_utf8()` 이 호출돼도 값으로 발화한다.
os.environ["PYTHONUTF8"] = "1"

# 테스트가 제품의 output_dir 폴백을 밟으면 tempdir에 감사 raw가 영구 누적된다.
# 세션 기본 tempdir를 pytest 소유 디렉터리로 격리해 다른 checkout/PM 프로세스 출력과 섞지 않는다.
_PROJECT_TEMPDIR_GLOBS = (
    "pm_delegate_*",
    "additional_reviewer_*",
    "pm_board_seed_*",
    "safewrite-*",
    "safewrite-fac-*",
    "swroot-*",
    "swout-*",
    # tempdir 폴백 장부(PM 홈 미해소 형상) + 그 잠금 파일. 기본 목적지가 repo 로 옮겨졌어도
    # 폴백 경로는 살아 있으므로 이 축도 세션 누출 대상이다.
    "pm_raw_outputs.json",
    "pm_raw_outputs.json.lock",
)
_TEMPDIR_ENV_KEYS = ("TMPDIR", "TEMP", "TMP")

# 위임·추가리뷰 raw 의 **기본 목적지**는 tempdir 가 아니라 해소된 repo 의
# `.project_manager/.local/` 하위다. 그래서 tempdir 격리만으로는 부족하다 — 기본 경로를 밟는
# 테스트는 tempdir 가 아니라 **실 작업 트리 안**에 raw 를 쓰고, 위 세션 스냅샷은 그것을 보지
# 못한다. 감시 축을 하나 더 세워 두 목적지를 함께 닫는다(tempdir 폴백 + repo 기본 경로).
# 이 경로는 *이 checkout* 소유라 다른 PM 프로세스(자기 PM 홈에 쓴다)의 동시 출력과 섞이지 않는다 —
# 전역 tempdir 감시가 flaky 했던 원인이 여기엔 없다.
_REPO_ROOT_FOR_RAW_GUARD = Path(__file__).resolve().parent.parent
_REPO_RAW_OUTPUT_DIRS = (
    Path(".project_manager") / ".local" / "delegate",
    Path(".project_manager") / ".local" / "review",
)
_REPO_RAW_OUTPUT_FILES = (
    Path(".project_manager") / ".local" / "raw_outputs.json",
    # 장부 잠금 파일도 세션이 남기면 누출이다 — 정상 종료면 잠금은 해제되지만 파일 자체가
    # 새로 생겼다는 사실이 "이 세션이 기본 경로를 밟았다"는 증거다.
    Path(".project_manager") / ".local" / "raw_outputs.json.lock",
)

_REPO_LIVE_BOARD_OUTPUT_ROOTS = (
    Path(".project_manager") / "wiki",
    Path(".project_manager") / "board",
)


def _snapshot_repo_raw_outputs(
        repo_root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """repo 기본 raw 목적지(디렉토리 직계 파일 + 장부 파일)의 파일 정체를 스냅샷한다.

    디렉토리/파일 부재는 정상(빈 스냅샷) — 신규 생성 자체가 델타로 잡힌다.

    `delegate/` 디렉토리는 raw 외에 **쓰고-지우는 중간 산출**(opencode 프롬프트 파일)도 만들 수
    있다. 정상 경로는 unlink 로 지우므로 델타에 남지 않고, 남았다면 그것 자체가 누출 신호다 —
    특정 접두어로 좁히지 않고 디렉토리의 직계 파일 전부를 본다.

    직계 열거(`iterdir`)로 충분하다 — raw 박제는 목적지 디렉토리에 **평평하게** 파일을 만든다
    (`pm_delegate_<harness>_<pid>_<uuid>.txt`·`additional_reviewer_<reviewer>_<ts>.txt`). 재귀 walk 는
    공용 repo-owned 열거 seam 을 우회하는 형태가 되고, 감시 대상은 git-ignored 산출물이라 그 seam 의
    tracked-only 의미와도 맞지 않는다. 하위 디렉토리가 생기는 설계 변경이 오면 그때 seam 을 태운다.
    """
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    candidates: list[Path] = [repo_root / rel for rel in _REPO_RAW_OUTPUT_FILES]
    for rel in _REPO_RAW_OUTPUT_DIRS:
        base = repo_root / rel
        if base.is_dir():
            candidates.extend(p for p in base.iterdir() if p.is_file())
    for path in candidates:
        try:
            stat_result = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        snapshot[path] = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
    return snapshot


def _snapshot_repo_board_outputs(
        repo_root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """라이브 board가 소유하는 두 루트 아래 모든 파일 정체를 스냅샷한다.

    산출 파일명을 나열하지 않고 소유 루트를 단일 진실로 둔다. 따라서 ``wiki/board.md`` 같은
    파생 출력과 이후 추가되는 board 산출도 자동으로 감시 대상이 된다. 테스트 픽스처는 pytest
    tmp 아래에 있어야 하며, 세션 전후 신규·변경·삭제를 모두 잡는다.
    """
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for rel in _REPO_LIVE_BOARD_OUTPUT_ROOTS:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat_result = path.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            snapshot[path] = (
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_mtime_ns,
                stat_result.st_size,
            )
    return snapshot


def _assert_repo_board_outputs_unchanged(
    before: dict[Path, tuple[int, int, int, int]],
    after: dict[Path, tuple[int, int, int, int]],
) -> None:
    """동일 live board의 전후 스냅샷에 신규·변경·삭제가 없음을 단언한다."""
    changed = sorted(
        path for path in (before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )
    assert not changed, (
        "테스트 세션이 실 작업 트리의 live board 산출을 만들거나 변경했습니다 — "
        "REPO/BOARD_FILE/tickets_dir()/board_root()를 모두 pytest tmp_path로 재앵커하세요:\n"
        + "\n".join(f"  - {path}" for path in changed)
    )


def _snapshot_project_temp_outputs(
        root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """프로젝트가 지정 tempdir에 만드는 이름 있는 산출물의 경로와 파일 정체를 스냅샷한다.

    additional_reviewer 파일명은 초 단위라 기존 파일을 덮어쓸 수 있다. 경로 집합만 비교하지 않고
    inode/mtime/size도 함께 기록해 같은 이름 덮어쓰기도 신규 tempdir 쓰기로 판정한다.
    """
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for pattern in _PROJECT_TEMPDIR_GLOBS:
        for path in root.glob(pattern):
            try:
                stat_result = path.stat()
            except FileNotFoundError:
                continue
            snapshot[path] = (
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_mtime_ns,
                stat_result.st_size,
            )
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def _forbid_real_tempdir_project_outputs(tmp_path_factory):
    """세션 기본 tempdir를 격리하고 이 세션의 프로젝트 산출물만 금지한다.

    Python 현재 프로세스의 캐시와 자식 프로세스가 상속할 표준 환경변수를 함께 고정한다.
    감시 경로는 지역 변수로 보존하므로 테스트가 이후 env/gettempdir를 monkeypatch해도 바뀌지
    않는다. 다른 pytest/PM 프로세스는 이 디렉터리를 모르므로 전역 tempdir 동시 출력은 무관하다.
    """
    session_tempdir = tmp_path_factory.mktemp("project-temp-output-guard")
    before = _snapshot_project_temp_outputs(session_tempdir)
    repo_before = _snapshot_repo_raw_outputs(_REPO_ROOT_FOR_RAW_GUARD)
    board_before = _snapshot_repo_board_outputs(_REPO_ROOT_FOR_RAW_GUARD)
    previous_tempdir = tempfile.tempdir
    previous_env = {key: os.environ.get(key) for key in _TEMPDIR_ENV_KEYS}
    for key in _TEMPDIR_ENV_KEYS:
        os.environ[key] = str(session_tempdir)
    tempfile.tempdir = str(session_tempdir)
    try:
        yield
        after = _snapshot_project_temp_outputs(session_tempdir)
        repo_after = _snapshot_repo_raw_outputs(_REPO_ROOT_FOR_RAW_GUARD)
        board_after = _snapshot_repo_board_outputs(_REPO_ROOT_FOR_RAW_GUARD)
    finally:
        tempfile.tempdir = previous_tempdir
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    written = sorted(path for path, identity in after.items() if before.get(path) != identity)
    assert not written, (
        f"테스트 세션이 격리 tempdir({session_tempdir})에 프로젝트 산출물을 썼습니다. "
        "흐름 테스트에 pytest tmp_path 기반 --output-dir/output_dir를 명시하세요:\n"
        + "\n".join(f"  - {path}" for path in written)
    )
    repo_written = sorted(
        path for path, identity in repo_after.items() if repo_before.get(path) != identity)
    assert not repo_written, (
        "테스트 세션이 실 작업 트리의 기본 raw 목적지"
        f"({_REPO_ROOT_FOR_RAW_GUARD}/.project_manager/.local/)에 산출물을 썼습니다 — "
        "기본 목적지가 여기이므로 흐름 테스트는 tmp_path 기반 "
        "--output-dir/output_dir 를 명시해야 합니다:\n"
        + "\n".join(f"  - {path}" for path in repo_written)
    )
    _assert_repo_board_outputs_unchanged(board_before, board_after)


_LIVE_RENDER_OUTPUTS = (
    Path(".project_manager/wiki/board.md"),
    Path(".project_manager/wiki/log/dashboard.md"),
)


def _live_render_identity() -> dict[Path, tuple[int, int, int] | None]:
    identity: dict[Path, tuple[int, int, int] | None] = {}
    for rel in _LIVE_RENDER_OUTPUTS:
        path = _REPO_ROOT_FOR_RAW_GUARD / rel
        try:
            st = path.stat()
        except OSError:
            identity[path] = None
        else:
            identity[path] = (st.st_mtime_ns, st.st_size, st.st_ino)
    return identity


@pytest.fixture(autouse=True)
def _name_the_live_render_polluter():
    """실 트리 렌더 산출을 건드린 **그 테스트**를 이름으로 짚는다.

    세션 스코프 오염 가드(`_forbid_real_tempdir_project_outputs`)는 누가 썼는지는 못 짚어
    범인 추적이 이분탐색이 된다. 감시 대상을 렌더 산출 두 개로 좁히면 테스트당 stat 두 번이라
    상시 켜 둘 수 있다. 재앵커 누락은 `conftest.anchor_board_module` 로 닫는다."""
    before = _live_render_identity()
    yield
    after = _live_render_identity()
    changed = sorted(str(path) for path, ident in after.items() if before.get(path) != ident)
    assert not changed, (
        "이 테스트가 실 작업 트리의 렌더 산출을 갱신했습니다 — board 모듈을 "
        "`conftest.anchor_board_module(mod, tmp_path, monkeypatch)` 로 재앵커하세요:\n  "
        + "\n  ".join(changed))


def anchor_board_module(mod, tmp_path, monkeypatch) -> None:
    """board 모듈의 **import 시점 REPO 파생 상수**를 tmp 로 한꺼번에 재앵커한다.

    `REPO` 만 바꾸면 상수로 굳은 `BOARD_FILE`·`BOARD_LOCK` 이 실 작업 트리를 계속 가리켜
    렌더가 live `wiki/board.md` 를 갱신한다(이 파일의 live board 오염 가드가 세션 끝에서 잡되,
    세션 스코프라 어느 테스트가 썼는지는 못 짚는다). 재앵커 목록을 테스트마다 재선언하지 않도록
    여기 한 곳에 둔다 — 새 파생 상수가 생기면 이 함수만 고친다.
    """
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(
        mod, "BOARD_FILE", tmp_path / ".project_manager" / "wiki" / "board.md")
    if hasattr(mod, "BOARD_LOCK"):
        monkeypatch.setattr(
            mod, "BOARD_LOCK",
            tmp_path / ".project_manager" / ".local" / "board.lock")
    # 디렉터리는 미리 만들지 않는다 — 호출 테스트가 자기 형상대로 만든다(선-생성은 그쪽
    # `mkdir(parents=True)` 와 충돌한다). 대신 렌더 시점에 부모가 없으면 만들도록
    # `refresh_board` 를 감싼다(엔진은 wiki/ 존재를 전제한다).
    refresh = getattr(mod, "refresh_board", None)
    if callable(refresh):
        def _refresh_with_parent(*args, _orig=refresh, _mod=mod, **kwargs):
            _mod.BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
            return _orig(*args, **kwargs)
        monkeypatch.setattr(mod, "refresh_board", _refresh_with_parent)


# ── 묶음 장부 픽스처 (완료 기록·리뷰 폭 판정의 입력) ──────────────────────────
#
# 엔진은 판정 기준(통합 브랜치)을 묶음 장부 `base_branch` 에서만 읽고, 선언이 없으면 다른
# 기준으로 접지 않고 멈춘다. 실 board 에서는 티켓 발행이 크기 1 장부를 만들며 그 값을 박는다 —
# 픽스처 board 도 같은 파일을 가져야 완료 기록·리뷰 폭 판정이 성립한다. 파일 하나를 여러 테스트
# 파일이 각자 재타이핑하면 스키마가 갈리므로 여기 한 곳에 둔다.
#
# 장부 `budget` 4키는 라운드 역할 **수열**이기도 하다 — 준비는 그 수열이 말하는 순서로만
# 다음 라운드를 연다. 그래서 픽스처는 자기가 실제로 예약할 단계를 선언해야 하고, 선언하지
# 않은 단계(값 0)는 건너뛴다.
_CLUSTER_BUDGET_STAGES: tuple[tuple[str, str], ...] = (
    ("architect", "architect"),
    ("developer", "developer_per_ticket"),
    ("code-reviewer", "code-reviewer"),
    ("developer", "fix"),
)
_CLUSTER_BUDGET_DEFAULT: dict[str, int] = {
    key: 1 for _role, key in _CLUSTER_BUDGET_STAGES
}


def cluster_budget_for(roles) -> dict[str, int]:
    """예약할 라운드 역할 순서를 장부 `budget` 4키로 옮긴다.

    수열이 곧 허용 순서라, 계획을 단계에 그리디로 배분한다 — 같은 역할이 이어지면 그 단계의
    수가 늘고, 역할이 바뀌면 그 역할이 나오는 다음 단계로 넘어간다. 수열로 표현할 수 없는
    순서는 여기서 바로 실패한다: 장부가 선언할 수 없는 계획을 테스트가 조용히 기대하지 않게
    한다.
    """
    budget = {key: 0 for _role, key in _CLUSTER_BUDGET_STAGES}
    stage = 0
    for role in roles:
        while (stage < len(_CLUSTER_BUDGET_STAGES)
               and _CLUSTER_BUDGET_STAGES[stage][0] != role):
            stage += 1
        if stage == len(_CLUSTER_BUDGET_STAGES):
            stage_roles = tuple(item for item, _key in _CLUSTER_BUDGET_STAGES)
            raise AssertionError(
                f"장부 예산 수열로 선언할 수 없는 라운드 순서다: {tuple(roles)} — "
                f"{role!r} 를 그 자리에 둘 단계가 없다(단계 순서 {stage_roles})")
        budget[_CLUSTER_BUDGET_STAGES[stage][1]] += 1
    return budget


def write_cluster_ledger(
    board_dir: Path, tickets, *, base_branch: str, cluster: str | None = None,
    branch: str | None = None, rounds=None,
) -> Path:
    """`<board_dir>/tickets/clusters/<묶음>.md` 를 쓴다 — 멤버·기준 브랜치·예산 선언.

    `cluster` 를 생략하면 크기 1 묶음(`C-<티켓>`)이다 — 티켓 frontmatter 에 `cluster` 필드가
    없는 픽스처가 읽히는 그 이름이라, 명세를 건드리지 않고 장부만 얹으면 된다.
    `rounds` 는 이 묶음이 예약할 라운드 역할 순서다 — 생략하면 단계마다 1건씩(기본 수열).
    """
    members = [tickets] if isinstance(tickets, str) else list(tickets)
    name = cluster or f"C-{members[0]}"
    budget = _CLUSTER_BUDGET_DEFAULT if rounds is None else cluster_budget_for(rounds)
    directory = board_dir / "tickets" / "clusters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        "---\n"
        f"id: {name}\n"
        "tickets:\n" + "".join(f"- {member}\n" for member in members)
        + f"base_branch: {base_branch}\n"
        + f"branch: {branch or 'null'}\n"
        "spike: null\n"
        "budget:\n"
        + "".join(f"  {key}: {value}\n" for key, value in budget.items())
        + "replans: []\n"
        "status: open\n"
        "---\n",
        encoding="utf-8", newline="\n")
    return path


def current_branch(root: Path) -> str:
    """그 git 트리의 현재 브랜치 — 픽스처 장부의 `base_branch` 실값."""
    result = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


# ── ① codex ambient env 중화 (autouse) ────────────────────────────────────────

# 카드 감지 predicate 의 두 마커를 중화한다 — Codex 세션에서 회귀를 돌리면 이 값들이 pytest env
# 로 새어, 하네스 미감지를 전제로 하는 케이스가 통째로 codex 판정을 타게 된다.
_CODEX_AMBIENT_MARKERS = (
    "CODEX_THREAD_ID",
    "CODEX_CI",
)


@pytest.fixture(autouse=True)
def _neutralize_codex_ambient_markers(request, monkeypatch):
    """모든 기계 테스트에 codex-미감지·egress-neutral env baseline 을 준다.

    라이브 게이트(`@pytest.mark.release`) 테스트는 실 codex 를 격리 CODEX_HOME env 로 명시 스폰하므로
    부모 프로세스의 ambient 마커에 의존/간섭하지 않는다 — 중화가 그 의도를 흐리지 않게 제외한다."""
    if request.node.get_closest_marker("release") is not None:
        return
    for key in _CODEX_AMBIENT_MARKERS:
        monkeypatch.delenv(key, raising=False)


# ── ② codex 라이브 공용 헬퍼 (adr/ticket live · relay smoke 공유) ──────────────────

_CODEX_AUTH = Path.home() / ".codex" / "auth.json"

# codex exec 공통 argv 접두 (fill 러너·relay driver 와 동일 핀·ADR-0070): --json JSONL · workspace-write
# (파일 편집/board.py 실행 필요) · --skip-git-repo-check(tmp 홈은 git repo 아님).
_CODEX_EXEC_PREFIX = ("codex", "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check")

# 라이브 codex subprocess 로 통과시킬 env 화이트리스트 — 부모 env 통째 상속 대신 필수만
# (test_fresh_adopter_runtime_smoke._live_env 취지 동일). HOME 은 통과하되 CODEX_HOME 을 격리 dir 로
# 덮어 실 ~/.codex 오염(history/세션/trust)을 막는다.
_CODEX_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")


def codex_auth_available() -> bool:
    """실 codex auth 토큰(`~/.codex/auth.json`)이 있나 — 라이브 skipif 게이트에 합류(없으면 graceful skip)."""
    return _CODEX_AUTH.is_file()


def make_codex_home(base: Path) -> Path:
    """격리 CODEX_HOME 을 만들고 실 auth.json 을 복사한다 (실 ~/.codex 미오염·trust 결정성).

    trust 는 여기서 강제하지 않는다 — adr/ticket/relay 라이브는 `-s workspace-write` CLI 핀으로
    쓰기가 열려 project trust 없이 동작(세션 실측). project config/hooks/skills 로드가 필요한
    측정은 호출부가 `config.toml [projects]` trust 를 이 홈에 추가한다(`-c` override 는 무효·실측 §D3)."""
    home = base / ".codex-home"
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_CODEX_AUTH, home / "auth.json")
    return home


def drop_codex_auth(home: Path) -> None:
    """테스트 종료 시 복사해 둔 auth 토큰을 삭제한다 (scratch 에 자격증명 잔류 방지·라이브 규율)."""
    (home / "auth.json").unlink(missing_ok=True)


def codex_live_env(home: Path) -> dict[str, str]:
    """격리 codex 라이브 subprocess env — 화이트리스트 + CODEX_HOME 격리."""
    env = {k: os.environ[k] for k in _CODEX_ENV_PASSTHROUGH if k in os.environ}
    env["CODEX_HOME"] = str(home)
    env["PM_NONINTERACTIVE"] = "1"
    # 모델은 CLI `-m` 로 넘긴다(아래 run_codex_exec) — 기본은 codex 로컬 config 상속(gpt-5.5·D5).
    return env


def run_codex_exec(prompt: str, cwd: Path, home: Path, *, model: str | None = None,
                   timeout: int = 600, sandbox: str = "workspace-write",
                   ) -> subprocess.CompletedProcess:
    """`codex exec` 한 turn 을 격리 홈으로 실행한다 (stdin=DEVNULL 필수·미닫힘 시 무기한 대기·실측).

    기본 모델 = codex 로컬 config 상속(`-m` 생략). `model` 명시(예 env `PM_ORCH_LIVE_CODEX_MODEL`)면
    `-m <model>` 로 override. cwd 는 `-C` 로 핀(child cwd 격리·PM repo root). resume 커맨드형이
    필요하면 driver(`pm_orch_codex.py`)를 쓴다 — 이 헬퍼는 단일-turn exec 전용."""
    if sandbox not in {"workspace-write", "danger-full-access"}:
        raise ValueError(f"unsupported codex sandbox: {sandbox}")
    cmd = list(_CODEX_EXEC_PREFIX)
    cmd[cmd.index("-s") + 1] = sandbox
    if model:
        cmd += ["-m", model]
    cmd += ["-C", str(cwd)]
    cmd.append(prompt)
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=codex_live_env(home), stdin=subprocess.DEVNULL, timeout=timeout,
    )


# ── ③ 병렬 실행(pytest-xdist) 경고 되살리기 (T-0757) ───────────────────────────

# 워커가 낸 경고를 컨트롤러는 `importlib.import_module(<경고 클래스의 모듈 이름>)` 으로 되살린다
# (`xdist/workermanage.py::unserialize_warning_message`). 파일 경로로 연 모듈의 이름은 컨트롤러에 없어
# import 가 실패하고, xdist 가 그 실패를 잡지 않아 경고 한 건이 실행 전체를 INTERNALERROR 로 죽는다
# (실측: 엔진 합성 이름 `_project_manager_repo_owned_files:<path>` · 테스트 사본 이름
# `file_lock_under_test`).
#
# 이름을 되살리는 대신 되살리기 실패를 낮춰 받는 **한 겹**으로 닫는다. 이름 해소는 표면을 못 덮는다 —
# 테스트의 `spec_from_file_location` 호출부 192 파일이 제각각 이름을 붙여 규칙이 없다. 그리고 잃는
# 정보도 없다 — 경고 필터·`pytest.warns` 판정은 이미 워커에서 그 클래스로 끝났고, 컨트롤러가 하는
# 일은 요약 표시뿐이라 원 모듈명·클래스명·메시지를 본문 문자열로 보존하면 표시가 그대로 남는다.
_DEGRADED_WARNING_MODULE = "builtins"
_DEGRADED_WARNING_CLASS = "Warning"
_XDIST_UNSERIALIZER_ATTR = "unserialize_warning_message"


def degraded_warning_message_data(data: dict) -> dict:
    """되살릴 수 없는 경고 데이터를 `builtins.Warning` 한 겹으로 낮춘다(원 정체는 본문에 보존)."""
    degraded = dict(data)
    degraded["message_module"] = _DEGRADED_WARNING_MODULE
    degraded["message_class_name"] = _DEGRADED_WARNING_CLASS
    degraded["message_args"] = (
        f"{data['message_module']}.{data['message_class_name']}: {data['message_str']}",)
    degraded["category_module"] = _DEGRADED_WARNING_MODULE
    degraded["category_class_name"] = _DEGRADED_WARNING_CLASS
    return degraded


def tolerant_warning_unserializer(original):
    """되살리기 실패를 낮춰 받는 감싸개를 만든다 (설치와 분리 — 가드 테스트가 직접 부른다)."""

    def tolerant_unserialize_warning_message(data: dict):
        try:
            return original(data)
        except Exception:
            return original(degraded_warning_message_data(data))

    tolerant_unserialize_warning_message._pm_tolerant = True
    # 감싸개가 원본을 들고 있어야 가드 테스트가 어느 프로세스에서든 "감싸기 이전" 동작을 부른다.
    tolerant_unserialize_warning_message._pm_original = original
    return tolerant_unserialize_warning_message


def install_tolerant_warning_unserializer() -> None:
    """xdist 컨트롤러의 경고 되살리기를 감싸개로 바꾼다(중복 설치 없음).

    감쌀 지점은 xdist 내부 심볼이다 — 상류가 이름을 바꾸면 조용히 넘어가지 않고 즉시 실패한다.
    조용히 넘어가면 병렬 회귀가 경고 한 건에 INTERNALERROR 로 죽는 상태로 되돌아간다.
    """
    import xdist
    from xdist import workermanage

    original = getattr(workermanage, _XDIST_UNSERIALIZER_ATTR, None)
    if original is None:
        raise RuntimeError(
            f"pytest-xdist {xdist.__version__} 의 workermanage 에 "
            f"`{_XDIST_UNSERIALIZER_ATTR}` 가 없다 — 병렬 회귀의 경고 되살리기를 감쌀 지점이 "
            "사라졌다(그대로 두면 워커 경고 한 건이 실행 전체를 INTERNALERROR 로 죽인다). "
            "tests/conftest.py ③절을 새 xdist API 에 맞춰 갱신하라.")
    if getattr(original, "_pm_tolerant", False):
        return
    setattr(workermanage, _XDIST_UNSERIALIZER_ATTR, tolerant_warning_unserializer(original))


def pytest_configure(config):
    """되살리기를 실제로 하는 프로세스(xdist 컨트롤러)에만 감싸기를 설치한다."""
    if hasattr(config, "workerinput"):
        return
    if not config.pluginmanager.hasplugin("xdist"):
        return
    install_tolerant_warning_unserializer()
