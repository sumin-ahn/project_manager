"""pm_handoff dirty-tree 게이트 단위테스트 ([0/7]·T-0609).

핸드오프 시작 시점의 불변식 — **실행 앵커 트리는 clean(gitignored 제외)** — 을 기계 차단으로
고정한다. 계기: 세션 산출 11파일이 미커밋인 채 핸드오프가 완결·커밋·push 됐고 다음 세션
부트스트랩이 뒤늦게 발견했다([6/7] git status dump 는 보여주기만 하고 차단하지 않는다).

고정하는 성질:
  - clean → 통과(현행 흐름 100% 보존·log skeleton 이 실제로 append 된다).
  - dirty → rc 1 차단 + 파일 목록 열거. **차단 시점이 load-bearing** 이라 log/dashboard·task
    장부 어떤 mutation 도 일어나지 않았음을 함께 단언한다(재실행 중복 entry 0). 회귀보다도 앞.
  - 판정 범위 = PM 홈 + **활성 worktree 전수**(task 보유 슬롯 / slot·솔로 활성 슬롯). PM 홈
    `.gitignore` 가 `work/` 를 ignore 하므로 슬롯을 따로 안 보면 영영 안 잡힌다.
  - `--ack-dirty "<사유>"` → 통과 + 사유를 handoff entry 에 박제(단일행 평탄화·빈 사유 거부).
  - `--auto-trigger`(비대화 자동 실행 전용 신호) → 차단 대신 loud 경고 + 사유 자동 박제.
  - 같은-세션 재실행 → 기존 entry 에 ack 줄 **멱등 upsert**(콘솔로 흘리지 않는다).
  - `--dry-run` → 판정 결과 미리보기만(차단 없음).
  - unborn HEAD(커밋 0) → 판정 불가가 아니라 **전량 미커밋**으로 판정. 비-git 트리만 비차단 경고.

기계 테스트는 실 회귀 미접촉 — git seam 은 `-C <tree>` 를 읽는 결정론 stub DI, log/playbook/
pm_state 는 tmp 주입, `REPO` 는 monkeypatch. 마지막 절만 **실 git 바이너리**로 `--exclude-standard`
의미(gitignored 제외·staged 신규·tracked 수정)와 unborn HEAD 를 실측한다.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
_GIT = shutil.which("git")
requires_git_binary = pytest.mark.skipif(
    _GIT is None, reason="git 바이너리 부재 — 실 repo dirty 판정 실측 불가."
)



def _run_handoff(inst, **kw):
    """핸드오프 실행 — 승인 게이트에 정식 승인값을 실어 통과시킨다.

    이 모듈의 축은 dirty-tree 게이트이지 사용자-명시 승인 게이트가 아니다(그 축은
    ``tests/test_pm_handoff_user_ack.py``가 소유한다). 승인 대상값은 task > 슬롯 이름 >
    legacy solo sentinel 순으로 정해진다.
    """
    if "user_ack" not in kw:
        slot = kw.get("worktree_slot")
        kw["user_ack"] = kw.get("task") or (slot.rsplit("/", 1)[-1] if slot else "solo")
    return inst.run(**kw)

def _load(name: str = "pm_handoff"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hf():
    return _load()


# ── git seam stub (트리별 dirty 응답) ─────────────────────────────────────────
#
# 게이트가 트리마다 부르는 명령(실코드 `_dirty_paths_in_tree`·`_unborn_head_dirty_paths` 기준):
#   git -C <tree> diff --name-only --ignore-submodules=none HEAD
#                                                       → tracked 미커밋 + gitlink drift
#   git -C <tree> ls-files --others --exclude-standard → untracked(gitignored 제외)
#   git -C <tree> rev-parse --verify --quiet HEAD      → 커밋 유무(unborn 정련·diff 실패 시에만)
#   git -C <tree> diff --cached --name-only            → index 축(**커밋 0 트리 한정** — 전량
#                                                        `git add` 된 트리를 clean 으로 오판하지
#                                                        않게 untracked 와 union 한다)
#   git -C <tree> submodule status --recursive         → 등록된 submodule working tree 전수
# 나머지 호출([1b] 출하 surface·[6/7] status -s·ahead)은 무해한 기본 응답을 준다.


def _lines(paths: list[str]) -> str:
    return "\n".join(paths) + ("\n" if paths else "")


def _git_stub(dirty: dict[str, tuple[list[str], list[str]]] | None = None, *,
              staged: dict[str, list[str]] | None = None,
              submodules: dict[str, tuple[str, ...]] | None = None,
              gitlink_drift: dict[str, list[str]] | None = None,
              submodule_status_fail_trees: tuple[str, ...] = (),
              non_git_trees: tuple[str, ...] = (),
              unborn_trees: tuple[str, ...] = (),
              calls: list[list[str]] | None = None):
    """트리 → (tracked 미커밋, untracked) 응답 stub.

    `dirty`          = 트리 → (`diff --name-only --ignore-submodules=none HEAD` 목록,
                       `ls-files --others` 목록).
    `staged`         = 트리 → `diff --cached --name-only` 목록. **커밋 0 트리에서만 소비된다** —
                       엔진이 그 조회를 unborn 정련 경로에서만 부르기 때문이다(그래서 이 값은
                       `unborn_trees` 와 짝으로 준다).
    `submodules`      = 앵커 트리 → `submodule status --recursive` 가 열거할 상대경로 전수.
    `gitlink_drift`   = 트리 → 상위 pin 과 다른 submodule 경로. stub 은 tracked diff 에
                        `--ignore-submodules=none` 이 있을 때만 이를 돌려줘 `ignore = all` override 를
                        모델한다.
    `submodule_status_fail_trees` = submodule 열거만 rc 128(판정 불가 경고 경로).
    `non_git_trees`  = 모든 조회 rc 128(비-git 트리·판정 불가).
    `unborn_trees`   = 커밋 0 — `diff HEAD` 와 `rev-parse HEAD` 는 실패하고 `diff --cached`·
                       `ls-files` 만 답한다(실 git 의 unborn 동작·아래 실측 테스트가 이 stub
                       형상을 검증한다).
    """
    table = dirty or {}
    staged_table = staged or {}
    submodule_table = submodules or {}
    drift_table = gitlink_drift or {}

    def _runner(args: list[str]) -> tuple[int, str]:
        if calls is not None:
            calls.append(list(args))
        tree = args[args.index("-C") + 1] if "-C" in args else None
        tracked, untracked = table.get(tree, ([], []))
        if tree is not None and tree in non_git_trees:
            return 128, "fatal: not a git repository\n"
        if "submodule" in args and "status" in args:
            if tree in submodule_status_fail_trees:
                return 128, "fatal: submodule status failed\n"
            paths = submodule_table.get(tree, ())
            return 0, "".join(f" {'a' * 40} {path}\n" for path in paths)
        if "ls-files" in args:
            return 0, _lines(untracked)
        if "rev-parse" in args:
            if tree in unborn_trees:
                return 128, ""
            return 0, "abc123\n"
        if "diff" in args:
            if "--cached" in args:      # unborn 정련 축 — index 에 올라간 것.
                return 0, _lines(staged_table.get(tree, []))
            if any(".." in arg for arg in args):
                return 0, ""            # 커밋된-미push 없음([1b] 출하 surface).
            if tree in unborn_trees:
                return 128, "fatal: ambiguous argument 'HEAD'\n"
            drift = drift_table.get(tree, []) if "--ignore-submodules=none" in args else []
            return 0, _lines([*tracked, *drift])
        return 0, ""                    # status -s·rev-list 등.

    return _runner


class _TaskPool:
    """task membership + 보유 슬롯만 답하고 장부 mutation 호출을 기록하는 최소 seam."""

    def __init__(self, slots: tuple[str, ...] = ()):
        self._slots = slots
        self.released: list[str] = []

    def find_task(self, name):
        return type("_T", (), {"name": name, "pid": 4242})()

    def slots_for_task(self, name):
        return [type("_L", (), {"slot": slot})() for slot in self._slots]

    def release_task_pid(self, name):
        self.released.append(name)
        return type("_T", (), {"name": name, "pid": 0})()


_TASK_PM_STATE = """# task state

## 세션 식별 (현재까지 사용된 이름)

최근 N 차 (sliding window, 기본 3 차):
  - **1차** (2026-08-09 · 첫 세션): 첫 세션.

## 진행 중인 의사결정

표 내용.
"""


def _make_handoff(hf, tmp_path: Path, monkeypatch, *, git_runner, pool=None,
                  task_mode: bool = False, task: str = "alpha"):
    """REPO 를 tmp 로 핀한 hermetic PmHandoff — log/playbook/dashboard 는 tmp 주입.

    `REPO` 핀이 load-bearing 이다: 게이트의 판정 앵커(PM 홈 트리)가 곧 `REPO` 라, 실 repo 를
    보면 개발 중 작업트리 상태에 따라 결과가 흔들린다(결정성 상실).

    `task_mode=True` 면 **pm_state 를 명시 주입하지 않는다** — 명시 주입은
    `_pm_state_file_explicit` 를 세워 `task_mode` 를 False 로 만들고, 그러면 task pid=0 기록
    같은 task 장부 경로가 아예 실행되지 않아 "게이트가 그 앞에서 멈췄다"를 검증할 수 없다.
    대신 task 서술 공간(`.local/tasks/<task>/pm_state.md`)에 실제 state 를 깔아 준다.
    """
    monkeypatch.setattr(hf, "REPO", tmp_path)
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    log_file.write_text("# log\n", encoding="utf-8")
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")
    kwargs = {}
    if task_mode:
        task_state = tmp_path / ".project_manager" / ".local" / "tasks" / task / "pm_state.md"
        task_state.parent.mkdir(parents=True, exist_ok=True)
        task_state.write_text(_TASK_PM_STATE, encoding="utf-8")
    else:
        # 부재 경로 — 3·4 단계는 fail-soft skip (게이트 판정과 무관).
        kwargs["pm_state_file"] = tmp_path / "nope" / "pm_state.md"
    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "120 passed in 1.0s\n"),
        run_git_fn=git_runner,
        log_file=log_file,
        pm_playbook_file=playbook_file,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
        **kwargs,
    )
    return inst, log_file


_DIRTY_TWO = ([".claude/agents/developer.md"], ["docs/new-note.md"])


# ── clean 통과 ────────────────────────────────────────────────────────────────

def test_clean_tree_passes_and_appends_entry(hf, tmp_path, monkeypatch, capsys):
    """clean → rc 0·게이트 통과 표시·[2/7] log skeleton 이 실제로 append 된다."""
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=_git_stub())

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[0/7] dirty-tree 게이트" in out
    assert "✓ clean" in out
    assert "PM 5차" in log_file.read_text(encoding="utf-8")


def test_clean_tree_entry_has_no_ack_line(hf, tmp_path, monkeypatch):
    """clean 핸드오프 entry 는 ack 줄이 없다 (현행 lean 스키마 byte-호환)."""
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=_git_stub())

    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 0
    assert hf.DIRTY_ACK_ENTRY_LABEL not in log_file.read_text(encoding="utf-8")


# ── dirty 차단 (rc 1 · 파일 목록 · 회귀보다 앞 · 첫 mutation 전) ──────────────

def test_dirty_tree_blocks_with_rc1_and_file_list(hf, tmp_path, monkeypatch, capsys):
    """dirty → rc 1 + 잔여 파일 목록 열거 + 커밋/`--ack-dirty` 안내."""
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    captured = capsys.readouterr()
    assert "미커밋 잔여 2건" in captured.out
    assert ".claude/agents/developer.md" in captured.out
    assert "docs/new-note.md" in captured.out
    assert "[중단]" in captured.err
    assert "--ack-dirty" in captured.err


def test_dirty_gate_runs_before_regression(hf, tmp_path, monkeypatch, capsys):
    """게이트는 회귀([1/7]) **앞**이다 — 차단 시 pytest 는 호출조차 되지 않는다.

    판정은 git 조회 몇 번인데 회귀는 수 분이다. 뒤에 두면 "커밋만 하면 될" PM 이 회귀 한 판을
    통째로 기다린 뒤에야 차단 사유를 본다.
    """
    calls: list[str] = []
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    inst._run_pytest_fn = lambda: (calls.append("pytest"), (0, "1 passed\n"))[1]

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)

    assert rc == 1
    assert calls == []                      # 회귀 미실행.
    assert "[1/7] 회귀 측정" not in capsys.readouterr().out


def test_dirty_block_touches_no_file(hf, tmp_path, monkeypatch):
    """차단은 **첫 mutation 전** — log 는 byte 불변·dashboard 는 생성조차 안 된다.

    [6/7] 시점 차단이었다면 log entry 가 이미 append 된 뒤라 재실행이 중복 entry 를 만든다.
    이 단언이 그 순서 계약을 못 박는다.
    """
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    before = log_file.read_bytes()

    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 1

    assert log_file.read_bytes() == before
    assert not (tmp_path / "dashboard.md").exists()


def test_dirty_rerun_after_block_is_still_first_entry(hf, tmp_path, monkeypatch):
    """차단 후 커밋(=clean) 재실행이 entry 를 **1개만** 남긴다 (중복 부기 0)."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 1

    inst._run_git_fn = _git_stub()      # 잔여를 커밋했다 — 이제 clean.
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 0

    assert log_file.read_text(encoding="utf-8").count("handoff | PM 5차") == 1


# ── task 장부 무접촉 (task_mode 참 구성 · sensitivity 대조군 포함) ────────────

def test_dirty_block_does_not_release_task_pid(hf, tmp_path, monkeypatch, capsys):
    """task 모드 차단은 pid=0 기록(첫 장부 mutation) **전**에 멈춘다."""
    pool = _TaskPool()
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
        pool=pool, task_mode=True,
    )
    before = log_file.read_bytes()

    rc = _run_handoff(inst, session_num=None, wave_summary="x", dry_run=False,
                  skip_pytest=True, task="alpha")

    assert rc == 1
    # rc 1 의 유래가 게이트임을 메시지로 고정(다른 중단과 혼동 금지).
    assert "핸드오프 시작 시점에 미커밋 잔여" in capsys.readouterr().err
    assert pool.released == []          # pid=0 기록 = 첫 장부 mutation — 도달하지 않았다.
    assert log_file.read_bytes() == before


def test_clean_task_run_reaches_pid_release(hf, tmp_path, monkeypatch):
    """sensitivity 대조군 — 같은 구성에서 clean 이면 pid=0 기록까지 **실제로 도달**한다.

    이게 없으면 위 테스트의 `released == []` 가 "게이트가 막았다"가 아니라 "원래 못 가는
    경로였다"로도 green 이다(게이트를 무력화해도 통과하는 가짜 단언).
    """
    pool = _TaskPool()
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch, git_runner=_git_stub(),
        pool=pool, task_mode=True,
    )

    rc = _run_handoff(inst, session_num=None, wave_summary="x", dry_run=False,
                  skip_pytest=True, task="alpha")

    assert rc == 0
    assert pool.released == ["alpha"]


# ── 판정 범위 (PM 홈 + 활성 worktree 전수) ───────────────────────────────────

def test_task_mode_gate_covers_held_slots(hf, tmp_path, monkeypatch, capsys):
    """PM 홈이 clean 이어도 **보유 슬롯**이 dirty 면 차단한다 (task 모드 판정 범위)."""
    slot_dir = tmp_path / "work" / "app_1"
    slot_dir.mkdir(parents=True)
    pool = _TaskPool(slots=("work/app_1",))
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(slot_dir): (["src/leftover.py"], [])}),
        pool=pool, task_mode=True,
    )

    rc = _run_handoff(inst, session_num=None, wave_summary="x", dry_run=False,
                  skip_pytest=True, task="alpha")

    assert rc == 1
    captured = capsys.readouterr()
    assert "핸드오프 시작 시점에 미커밋 잔여" in captured.err   # rc 1 유래 = 게이트.
    assert str(slot_dir) in captured.out
    assert "src/leftover.py" in captured.out
    assert pool.released == []


def test_slot_mode_gate_covers_active_worktree(hf, tmp_path, monkeypatch, capsys):
    """slot 모드 — PM 홈 clean 이어도 활성 worktree 가 dirty 면 차단한다.

    PM 홈 `.gitignore` 는 `work/` 를 ignore 하므로 슬롯 트리의 미커밋은 PM 홈 판정에 **절대**
    안 잡힌다. 슬롯을 판정 대상에 넣지 않으면 slot 모드 세션은 게이트를 통째로 우회한다.
    """
    slot_dir = tmp_path / "work" / "app_1"
    slot_dir.mkdir(parents=True)
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(slot_dir): ([], ["untracked-slot-output.md"])}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  worktree_slot="work/app_1")

    assert rc == 1
    captured = capsys.readouterr()
    assert "untracked-slot-output.md" in captured.out
    assert "핸드오프 시작 시점에 미커밋 잔여" in captured.err


def test_gate_trees_include_solo_auto_resolved_worktree(hf, tmp_path, monkeypatch):
    """솔로(슬롯 미지정)도 같은 규칙 — `_regression_cwd(None)` 해소 트리가 판정에 들어간다.

    solo 는 multi-PM 의 부분집합이라 모드별 특례를 두지 않는다.
    """
    monkeypatch.setattr(hf, "REPO", tmp_path)
    monkeypatch.setattr(hf, "_regression_cwd", lambda slot=None, *a, **k: str(tmp_path / "auto"))
    inst = hf.PmHandoff(run_git_fn=_git_stub())
    inst._worktree_slot = None

    assert inst._dirty_gate_trees(None) == [str(tmp_path), str(tmp_path / "auto")]


def test_gate_trees_dedup_pm_home_and_stale_slot(hf, tmp_path, monkeypatch):
    """stale 슬롯(디렉토리 부재)은 `_regression_cwd` 가 REPO 로 폴백 — 중복 판정하지 않는다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    inst = hf.PmHandoff(run_git_fn=_git_stub())
    inst._task_slots_snapshot = ("alpha", ("work/missing_9",))

    trees = inst._dirty_gate_trees("alpha")

    assert trees == [str(tmp_path)]


# ── submodule 경계 확장 (T-0620) ─────────────────────────────────────────────

def test_submodule_discovery_reuses_shared_parser_requests_recursive_and_skips_uninit(
        hf, monkeypatch):
    """공용 flag 파서를 호출하고 `--recursive` 전수 열거 중 `-` working tree 만 제외한다."""
    parser_calls: list[str] = []
    git_calls: list[list[str]] = []

    class SharedParser:
        @staticmethod
        def _parse_submodule_entries(output: str):
            parser_calls.append(output)
            return [
                ("-", "vendor/uninitialized"),
                (" ", "vendor/my lib"),
                ("+", "vendor/my lib"),
            ]

    def _runner(args: list[str]):
        git_calls.append(list(args))
        return 0, "shared-parser-input"

    monkeypatch.setattr(hf, "_load_worktree_pool", lambda: SharedParser)

    paths = hf._submodule_paths_in_tree("/super", _runner)

    assert paths == ["vendor/my lib"]
    assert parser_calls == ["shared-parser-input"]
    assert git_calls == [["-C", "/super", "submodule", "status", "--recursive"]]


def test_submodule_dirty_blocks_and_lists_parent_relative_paths(
        hf, tmp_path, monkeypatch, capsys):
    """재귀 열거된 모든 submodule 의 내부 잔여를 앵커 상대경로로 합쳐 차단한다."""
    board = tmp_path / ".project_manager" / "board"
    nested = tmp_path / "vendor" / "outer" / "inner"
    runner = _git_stub(
        {
            str(board): (["tickets/_template.md"], []),
            str(nested): ([], ["notes/leftover.md"]),
        },
        submodules={
            str(tmp_path): (".project_manager/board", "vendor/outer/inner"),
        },
    )
    inst, _log = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    captured = capsys.readouterr()
    assert ".project_manager/board/tickets/_template.md" in captured.out
    assert "vendor/outer/inner/notes/leftover.md" in captured.out
    assert "미커밋 잔여 2건" in captured.out


def test_gitlink_pin_drift_blocks_via_ignore_submodules_none(
        hf, tmp_path, monkeypatch, capsys):
    """상위 pin ≠ submodule HEAD 는 `ignore = all` 모델에서도 tracked 축이 차단한다."""
    calls: list[list[str]] = []
    runner = _git_stub(
        submodules={str(tmp_path): (".project_manager/board",)},
        gitlink_drift={str(tmp_path): [".project_manager/board"]},
        calls=calls,
    )
    inst, _log = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    assert ".project_manager/board" in capsys.readouterr().out
    tracked_calls = [args for args in calls if "diff" in args and "HEAD" in args]
    assert tracked_calls
    assert all("--ignore-submodules=none" in args for args in tracked_calls)


def test_parent_and_registered_submodule_clean_pass(
        hf, tmp_path, monkeypatch, capsys):
    """앵커와 등록 submodule 모두 clean 이면 기존 핸드오프 흐름을 그대로 통과한다."""
    runner = _git_stub(
        submodules={str(tmp_path): (".project_manager/board",)},
    )
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    assert "판정 트리 2개" in capsys.readouterr().out
    assert "PM 5차" in log_file.read_text(encoding="utf-8")


def test_judged_tree_count_excludes_unjudgeable_submodule(
        hf, tmp_path, monkeypatch, capsys):
    """판정 실패한 submodule 은 clean 요약의 판정 트리 수에 포함하지 않는다."""
    sub_tree = tmp_path / "vendor" / "missing"
    runner = _git_stub(
        submodules={str(tmp_path): ("vendor/missing",)},
        non_git_trees=(str(sub_tree),),
    )
    inst, _log = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    captured = capsys.readouterr()
    assert "판정 트리 1개" in captured.out
    assert str(sub_tree) in captured.err


def test_submodule_status_failure_warns_without_blocking(
        hf, tmp_path, monkeypatch, capsys):
    """submodule 열거 실패는 판정 불가 stderr 경고만 내고 정상 핸드오프를 유지한다."""
    runner = _git_stub(submodule_status_fail_trees=(str(tmp_path),))
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    captured = capsys.readouterr()
    assert "submodule status --recursive 실패" in captured.err
    assert "[중단]" not in captured.err
    assert "PM 5차" in log_file.read_text(encoding="utf-8")


def test_ignore_all_override_closes_internal_dirty_and_gitlink_axes(
        hf, tmp_path, monkeypatch, capsys):
    """`ignore = all` 모델에서도 내부 파일·gitlink drift 두 축을 함께 놓치지 않는다."""
    board = tmp_path / ".project_manager" / "board"
    calls: list[list[str]] = []
    runner = _git_stub(
        {str(board): (["tickets/open.md"], [])},
        submodules={str(tmp_path): (".project_manager/board",)},
        gitlink_drift={str(tmp_path): [".project_manager/board"]},
        calls=calls,
    )
    inst, _log = _make_handoff(hf, tmp_path, monkeypatch, git_runner=runner)

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    out = capsys.readouterr().out
    assert ".project_manager/board" in out
    assert ".project_manager/board/tickets/open.md" in out
    assert any(
        "diff" in args and "HEAD" in args and "--ignore-submodules=none" in args
        for args in calls
    )


# ── --ack-dirty override (사유 필수·단일행·entry 박제) ───────────────────────

def test_ack_dirty_passes_and_stamps_reason(hf, tmp_path, monkeypatch, capsys):
    """`--ack-dirty "<사유>"` → rc 0 + handoff entry 에 사유·잔여 건수 박제."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty="어댑터 사본은 다음 세션 릴리즈 wave 에서 커밋")

    assert rc == 0
    entry = log_file.read_text(encoding="utf-8")
    assert hf.DIRTY_ACK_ENTRY_LABEL in entry
    assert "어댑터 사본은 다음 세션 릴리즈 wave 에서 커밋" in entry
    assert "미커밋 2건" in entry
    assert "--ack-dirty override" in capsys.readouterr().out


def test_ack_dirty_on_clean_tree_stamps_nothing(hf, tmp_path, monkeypatch, capsys):
    """clean 인데 ack 를 줘도 박제하지 않는다 (없는 incident 를 log 에 만들지 않음)."""
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=_git_stub())

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty="사유")

    assert rc == 0
    assert hf.DIRTY_ACK_ENTRY_LABEL not in log_file.read_text(encoding="utf-8")
    assert "--ack-dirty 무시" in capsys.readouterr().out


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_ack_reason_aborts_engine_path(hf, tmp_path, monkeypatch, blank, capsys):
    """빈 사유는 엔진 run() 직접 호출도 거부 — 부작용 0 중단(사유 없는 override 금지)."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    before = log_file.read_bytes()

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty=blank)

    assert rc == 1
    assert "--ack-dirty 는 사유가 필수" in capsys.readouterr().err
    assert log_file.read_bytes() == before


def test_cli_rejects_blank_ack_reason(hf, capsys):
    """CLI `--ack-dirty ""` → usage error(rc 2) — 사유 없는 override 를 파서에서 닫는다."""
    with pytest.raises(SystemExit) as excinfo:
        hf.main(
            ["--session-seq", "5", "--wave-summary", "x", "--ack-dirty", ""],
            identity_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("인자 오류보다 identity 해소가 먼저 실행됨")
            ),
        )
    assert excinfo.value.code == 2
    assert "--ack-dirty 는 사유가 필수" in capsys.readouterr().err


def test_cli_rejects_bare_ack_flag(hf, capsys):
    """CLI `--ack-dirty` 를 인자 없이 주면 argparse 가 usage error — bare 플래그 불가."""
    with pytest.raises(SystemExit) as excinfo:
        hf.main(["--session-seq", "5", "--wave-summary", "x", "--ack-dirty"])
    assert excinfo.value.code == 2
    assert "--ack-dirty" in capsys.readouterr().err


# ── 사유 개행 무해화 (가짜 entry 위조 차단) ──────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("한 줄 사유", "한 줄 사유"),
    ("앞\n## [2026-08-09] handoff | PM 9차 → 다음 PM 세션", "앞 ## [2026-08-09] handoff | PM 9차 → 다음 PM 세션"),
    ("CR\r\n뒤", "CR 뒤"),
    ("탭\t과   연속   공백", "탭 과 연속 공백"),
])
def test_flatten_ack_reason_is_single_line(hf, raw, expected):
    """개행/탭/연속 공백을 단일 공백으로 평탄화한다 — 사유는 언제나 한 줄."""
    flattened = hf.flatten_ack_reason(raw)
    assert flattened == expected
    assert "\n" not in flattened and "\r" not in flattened


def test_multiline_ack_reason_cannot_forge_entry(hf, tmp_path, monkeypatch):
    """개행 사유를 줘도 entry 줄 수가 늘지 않는다 — 위조 헤더가 log 에 생기지 않는다."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, 
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        ack_dirty="사유\n## [2026-08-09] handoff | PM 99차 → 다음 PM 세션\n- 위조: 있음",
    )

    assert rc == 0
    text = log_file.read_text(encoding="utf-8")
    # entry 헤더는 **줄 시작**에서만 파싱된다 — 위조 문자열이 줄을 시작하지 못하는 것이 불변식.
    header_lines = [line for line in text.splitlines() if line.startswith("## [")]
    assert len(header_lines) == 1
    assert "PM 99차" not in header_lines[0]
    ack_lines = [l for l in text.splitlines() if l.startswith(hf.DIRTY_ACK_ENTRY_LABEL)]
    assert len(ack_lines) == 1
    # 사유 전체가 그 한 줄 안에 평탄화돼 들어갔다(줄 분리 아님).
    assert "위조: 있음" in ack_lines[0] and "PM 99차" in ack_lines[0]


def test_cli_flattens_multiline_ack_reason(hf):
    """CLI 층도 같은 무해화를 태운다 — 파서 통과 값이 이미 단일행."""
    parser = hf.build_parser()
    args = parser.parse_args(
        ["--session-seq", "5", "--wave-summary", "x", "--ack-dirty", "앞\n뒤"]
    )
    assert "\n" in args.ack_dirty            # 전제: argparse 는 그대로 받는다.
    assert hf.flatten_ack_reason(args.ack_dirty) == "앞 뒤"


# ── 비대화 자동 트리거 (전용 신호 · 차단 아님 · 사유 자동 박제) ──────────────

def test_auto_trigger_warns_and_stamps_instead_of_blocking(
        hf, tmp_path, monkeypatch, capsys):
    """`--auto-trigger` → rc 0 + loud 경고 + 사유 자동 박제.

    자동 실행 시점 차단은 세션 상태 전체를 잃으므로 강등한다 — 대신 다음 세션이 log 만 읽고
    잔여를 알도록 사유와 건수를 박제한다.
    """
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  auto_trigger=True)

    assert rc == 0
    assert "사용자 명시 핸드오프 호환 신호(--auto-trigger)" in capsys.readouterr().err
    assert "사용자 명시 핸드오프 호환 신호 — dirty 2건 잔존" in log_file.read_text(encoding="utf-8")


def test_auto_trigger_ack_reason_wins_over_default(hf, tmp_path, monkeypatch):
    """자동 경로여도 명시 `--ack-dirty` 사유가 있으면 사람이 준 사유를 박제한다."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty="사람이 준 사유", auto_trigger=True)

    assert rc == 0
    entry = log_file.read_text(encoding="utf-8")
    assert "사람이 준 사유" in entry
    assert "ctx 자동 핸드오프" not in entry


@pytest.mark.parametrize("env_value", ["1", "true", "yes", "on"])
def test_env_noninteractive_does_not_degrade_gate(
        hf, tmp_path, monkeypatch, env_value, capsys):
    """범용 비대화 env(`PM_NONINTERACTIVE`)는 강등 신호가 **아니다** — 여전히 차단한다.

    범용 신호로 강등하면 CI·headless·import 자동화 전반이 게이트를 통째로 우회한다. 강등은
    문서화된 자동 핸드오프 경로만 주는 전용 플래그(`--auto-trigger`)로 좁힌다.
    """
    monkeypatch.setenv("PM_NONINTERACTIVE", env_value)
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    before = log_file.read_bytes()

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    assert "핸드오프 시작 시점에 미커밋 잔여" in capsys.readouterr().err
    assert log_file.read_bytes() == before


def test_cli_auto_trigger_flag_defaults_off(hf):
    """`--auto-trigger` 는 명시할 때만 참 — 기본은 차단 계약."""
    parser = hf.build_parser()
    assert parser.parse_args(["--session-seq", "5", "--wave-summary", "x"]).auto_trigger is False
    assert parser.parse_args(
        ["--session-seq", "5", "--wave-summary", "x", "--auto-trigger"]
    ).auto_trigger is True


# ── 같은-세션 재실행: ack 줄 멱등 upsert ─────────────────────────────────────

def test_same_session_rerun_upserts_ack_line(hf, tmp_path, monkeypatch, capsys):
    """같은 차수 재실행이 기존 entry 에 ack 줄을 **삽입**한다 (콘솔로 흘리지 않는다)."""
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=_git_stub())
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 0
    capsys.readouterr()

    inst._run_git_fn = _git_stub({str(tmp_path): _DIRTY_TWO})
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty="두고 나가는 사유")

    assert rc == 0
    text = log_file.read_text(encoding="utf-8")
    assert "두고 나가는 사유" in text
    assert text.count("handoff | PM 5차") == 1          # 같은 entry 안에 박혔다.
    assert "멱등 upsert" in capsys.readouterr().out


def test_same_session_rerun_ack_is_idempotent(hf, tmp_path, monkeypatch):
    """같은 사유로 두 번 더 재실행해도 ack 줄은 1개·두 번째부터 byte 동일(멱등)."""
    inst, log_file = _make_handoff(hf, tmp_path, monkeypatch, git_runner=_git_stub())
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 0

    inst._run_git_fn = _git_stub({str(tmp_path): _DIRTY_TWO})
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                    ack_dirty="같은 사유") == 0
    first = log_file.read_bytes()
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                    ack_dirty="같은 사유") == 0

    assert log_file.read_bytes() == first
    text = log_file.read_text(encoding="utf-8")
    assert len([l for l in text.splitlines() if l.startswith(hf.DIRTY_ACK_ENTRY_LABEL)]) == 1


def test_upsert_dirty_ack_line_replaces_existing(hf):
    """이미 ack 줄이 있으면 재삽입이 아니라 **교체** — 줄이 쌓이지 않는다."""
    entry = (
        "## [2026-08-09] handoff | PM 5차 → 다음 PM 세션\n\n"
        "- 이 세션 박제 entries: (없음)\n"
        f"{hf.DIRTY_ACK_ENTRY_LABEL}: 옛 사유\n"
        "- 회귀/incident: <PM 손>\n"
    )

    updated = hf.upsert_dirty_ack_line(entry, "새 사유")

    assert "옛 사유" not in updated
    assert f"{hf.DIRTY_ACK_ENTRY_LABEL}: 새 사유" in updated
    assert updated.count(hf.DIRTY_ACK_ENTRY_LABEL) == 1


def test_upsert_dirty_ack_line_inserts_before_incident(hf):
    """ack 줄이 없으면 `- 회귀/incident:` 바로 앞에 삽입한다 (신규 skeleton 과 같은 자리)."""
    entry = (
        "## [2026-08-09] handoff | PM 5차 → 다음 PM 세션\n\n"
        "- 이 세션 박제 entries: (없음)\n"
        "- 회귀/incident: <PM 손>\n"
    )

    lines = hf.upsert_dirty_ack_line(entry, "사유").splitlines()

    assert lines[lines.index("- 회귀/incident: <PM 손>") - 1] == (
        f"{hf.DIRTY_ACK_ENTRY_LABEL}: 사유"
    )


def test_upsert_dirty_ack_line_appends_when_no_anchor(hf):
    """incident 앵커가 없어도(사람이 지운 entry) 끝에 붙여 박제를 잃지 않는다."""
    entry = "## [2026-08-09] handoff | PM 5차 → 다음 PM 세션\n\n- 메타 학습: 없음\n"

    updated = hf.upsert_dirty_ack_line(entry, "사유")

    assert updated.endswith(f"{hf.DIRTY_ACK_ENTRY_LABEL}: 사유\n")


_CRLF_ENTRY = (
    "## [2026-08-09] handoff | PM 5차 → 다음 PM 세션\r\n"
    "\r\n"
    "- 이 세션 박제 entries: (이 세션 박제 entry 없음)\r\n"
    "- 메타 학습: 없음\r\n"
    "- 회귀/incident: <PM 손>\r\n"
)


def test_upsert_dirty_ack_line_preserves_crlf_terminator(hf):
    """CRLF entry — 삽입도 교체도 줄 종결자가 CRLF 로 유지된다(bare LF 혼입 0)."""
    inserted = hf.upsert_dirty_ack_line(_CRLF_ENTRY, "사유", "\r\n")
    ack_line = [l for l in inserted.split("\r\n") if l.startswith(hf.DIRTY_ACK_ENTRY_LABEL)]
    assert ack_line == [f"{hf.DIRTY_ACK_ENTRY_LABEL}: 사유"]
    assert "\n" not in inserted.replace("\r\n", "")      # LF 는 전부 CRLF 의 일부.

    replaced = hf.upsert_dirty_ack_line(inserted, "새 사유", "\r\n")
    assert "\n" not in replaced.replace("\r\n", "")
    assert f"{hf.DIRTY_ACK_ENTRY_LABEL}: 새 사유\r\n" in replaced


def test_upsert_dirty_ack_line_crlf_replacement_is_byte_idempotent(hf):
    """CRLF entry 에 같은 사유를 다시 upsert 하면 byte 동일 (재실행 멱등)."""
    once = hf.upsert_dirty_ack_line(_CRLF_ENTRY, "같은 사유", "\r\n")
    twice = hf.upsert_dirty_ack_line(once, "같은 사유", "\r\n")

    assert once == twice


def test_same_session_rerun_ack_is_idempotent_on_crlf_log(hf, tmp_path, monkeypatch):
    """CRLF log 실 흐름 — 같은 사유 재실행 2회가 byte 동일하고 종결자가 유지된다."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    log_file.write_bytes(("# log\r\n\r\n" + _CRLF_ENTRY).encode("utf-8"))

    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                    ack_dirty="같은 사유") == 0
    first = log_file.read_bytes()
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                    ack_dirty="같은 사유") == 0

    assert log_file.read_bytes() == first
    # `read_text` 는 universal-newline 변환을 하므로 CRLF 판정은 **bytes 로** 한다.
    assert b"\r\n" in first and first.replace(b"\r\n", b"").count(b"\n") == 0
    text = first.decode("utf-8")
    assert text.count("handoff | PM 5차") == 1
    ack_lines = [l for l in text.split("\r\n") if l.startswith(hf.DIRTY_ACK_ENTRY_LABEL)]
    assert len(ack_lines) == 1
    assert "같은 사유" in ack_lines[0]


def test_upsert_dirty_ack_line_raises_when_unstampable(hf):
    """박제할 자리가 전혀 없으면(빈 entry) 조용히 통과시키지 않고 fail-loud."""
    with pytest.raises(hf.DirtyAckStampError):
        hf.upsert_dirty_ack_line("   \n", "사유")


def test_unstampable_ack_blocks_handoff(hf, tmp_path, monkeypatch, capsys):
    """박제 불가는 차단이 기본 — rc 1 이고 log 는 무접촉이다."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    monkeypatch.setattr(
        hf, "upsert_dirty_ack_line",
        lambda *a, **k: (_ for _ in ()).throw(hf.DirtyAckStampError("자리 없음")),
    )
    # 같은-세션 재실행 경로로 들어가도록 기존 entry 를 미리 깔아 둔다.
    inst._run_git_fn = _git_stub()
    assert _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True) == 0
    before = log_file.read_bytes()
    inst._run_git_fn = _git_stub({str(tmp_path): _DIRTY_TWO})

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
                  ack_dirty="사유")

    assert rc == 1
    assert "박제할 수 없다" in capsys.readouterr().err
    assert log_file.read_bytes() == before


# ── --dry-run (미리보기·비차단) ───────────────────────────────────────────────

def test_dry_run_previews_dirty_without_blocking(hf, tmp_path, monkeypatch, capsys):
    """`--dry-run` → dirty 여도 rc 0·미리보기 표기·log byte 불변(기존 dry-run 의미 유지)."""
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )
    before = log_file.read_bytes()

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=True, skip_pytest=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "미커밋 잔여 2건" in out
    assert "[dry-run] 판정 결과 미리보기" in out
    assert log_file.read_bytes() == before


def test_dry_run_preview_shows_ack_line_in_skeleton(hf, tmp_path, monkeypatch, capsys):
    """dry-run + ack → append 예정 skeleton 미리보기에 ack 줄이 보인다(적용 전 확인)."""
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): _DIRTY_TWO}),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=True, skip_pytest=True,
                  ack_dirty="릴리즈 wave 에서 커밋")

    assert rc == 0
    assert hf.DIRTY_ACK_ENTRY_LABEL in capsys.readouterr().out


# ── 판정 축: unborn HEAD / 비-git / 예외 ─────────────────────────────────────

def test_unborn_head_tree_is_judged_by_untracked(hf, tmp_path, monkeypatch, capsys):
    """커밋 0(unborn HEAD) → '판정 불가'가 아니라 **전량 미커밋**으로 차단한다.

    `diff HEAD` 는 비교 ref 가 없어 실패하지만 그 트리야말로 게이트가 가장 잡아야 할 상태다.
    """
    inst, _log = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub({str(tmp_path): ([], ["first.md", "second.md"])},
                             unborn_trees=(str(tmp_path),)),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    captured = capsys.readouterr()
    assert "미커밋 잔여 2건" in captured.out
    assert "판정 불가" not in captured.err


def test_unborn_head_all_staged_is_dirty(hf, tmp_path, monkeypatch, capsys):
    """커밋 0인데 **전량 `git add`** 된 트리 → untracked 는 비지만 여전히 dirty 로 차단한다.

    unborn 폴백이 untracked 축만 보면 이 트리가 clean 으로 오판된다 — 아무것도 커밋되지 않은
    바로 그 상태다. index 축(`diff --cached --name-only`)을 함께 봐야 닫힌다.
    """
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub(
            {str(tmp_path): ([], [])},                       # untracked 0건.
            staged={str(tmp_path): ["a.md", "sub/b.md"]},    # 전량 staged.
            unborn_trees=(str(tmp_path),),
        ),
    )
    before = log_file.read_bytes()

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1
    captured = capsys.readouterr()
    assert "미커밋 잔여 2건" in captured.out
    assert "sub/b.md" in captured.out
    assert "판정 불가" not in captured.err
    assert log_file.read_bytes() == before


def test_unborn_head_unions_staged_and_untracked(hf):
    """unborn 판정 축 = staged ∪ untracked (중복 제거)."""
    runner = _git_stub(
        {"/wt": ([], ["u.md", "dup.md"])},
        staged={"/wt": ["s.md", "dup.md"]},
        unborn_trees=("/wt",),
    )

    assert sorted(hf._dirty_paths_in_tree("/wt", runner)) == ["dup.md", "s.md", "u.md"]


def test_unborn_head_staged_query_failure_is_unjudgeable(hf):
    """unborn 인데 index 조회가 실패하면 dirty 단정 대신 판정 불가(None)."""
    def _runner(args):
        if "rev-parse" in args:
            return 128, ""            # 커밋 0.
        if "diff" in args and "--cached" in args:
            return 128, "boom"        # index 조회 실패.
        if "ls-files" in args:
            return 0, ""
        return 128, ""

    assert hf._dirty_paths_in_tree("/wt", _runner) is None


def test_non_git_tree_warns_on_stderr_without_blocking(hf, tmp_path, monkeypatch, capsys):
    """비-git 트리(모든 조회 실패) → 차단 대신 stderr 경고 1줄 + 정상 진행.

    판정 못 한 트리를 dirty 로 단정하면 비-git 채택자의 정상 핸드오프가 영구 차단된다.
    경고 채널은 게이트의 다른 판정 메시지와 같은 stderr 다.
    """
    inst, log_file = _make_handoff(
        hf, tmp_path, monkeypatch,
        git_runner=_git_stub(non_git_trees=(str(tmp_path),)),
    )

    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    captured = capsys.readouterr()
    assert "dirty 판정 불가" in captured.err
    assert captured.err.count("dirty 판정 불가") == 1
    assert "submodule status --recursive 실패" not in captured.err
    assert "dirty 판정 불가" not in captured.out
    assert "PM 5차" in log_file.read_text(encoding="utf-8")


def test_dirty_paths_in_tree_failsoft_on_exception(hf):
    """git 예외(미설치 등) → None(판정 불가) — 크래시하지 않는다."""
    def _boom(args):
        raise RuntimeError("git boom")

    assert hf._dirty_paths_in_tree("/wt", _boom) is None


def test_dirty_paths_in_tree_unions_tracked_and_untracked(hf):
    """판정 축 = tracked 미커밋 ∪ untracked-unignored (출하 surface 와 같은 seam)."""
    runner = _git_stub({"/wt": (["a.py"], ["b.md"])})
    assert sorted(hf._dirty_paths_in_tree("/wt", runner)) == ["a.py", "b.md"]


def test_dirty_paths_in_tree_none_when_commits_exist_but_diff_fails(hf):
    """커밋은 있는데 diff 만 실패 → 판정 불가(None) — unborn 정련이 과잉발동하지 않는다."""
    def _runner(args):
        if "ls-files" in args:
            return 0, ""
        if "rev-parse" in args:
            return 0, "abc123\n"      # 커밋 있음.
        return 128, "boom"            # diff 실패.

    assert hf._dirty_paths_in_tree("/wt", _runner) is None


# ── 목록 접기 (메시지 상한) ───────────────────────────────────────────────────

def test_format_dirty_paths_folds_over_limit(hf):
    """limit 초과분은 '… 외 N건' 한 줄로 접는다 (수백 건 dump 방지)."""
    paths = [f"f{index:03d}.md" for index in range(hf.DIRTY_GATE_LIST_LIMIT + 5)]

    lines = hf._format_dirty_paths(paths)

    assert len(lines) == hf.DIRTY_GATE_LIST_LIMIT + 1
    assert lines[-1].strip() == "… 외 5건"


def test_format_dirty_paths_keeps_all_within_limit(hf):
    """limit 이하면 전부 열거한다 (접기 표기 없음)."""
    lines = hf._format_dirty_paths(["b.md", "a.md"])

    assert [line.strip() for line in lines] == ["a.md", "b.md"]


# ── 실 git 실측 (stub 형상이 진짜 git 과 같은지) ─────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _git_output(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [_GIT, *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout


def _init_real_repo(repo: Path) -> None:
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)


@requires_git_binary
def test_real_git_ignore_all_is_overridden_for_gitlink_drift(hf, tmp_path):
    """실 git — `.gitmodules ignore=all` 이어도 명시 none override 는 gitlink drift 를 보고한다."""
    source = tmp_path / "source"
    superproject = tmp_path / "super"
    _init_real_repo(source)
    _init_real_repo(superproject)
    _git(
        "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(source), "vendor/lib", cwd=superproject,
    )
    _git("config", "-f", ".gitmodules", "submodule.vendor/lib.ignore", "all",
         cwd=superproject)
    _git("commit", "-q", "-am", "add ignored submodule", cwd=superproject)

    _git("commit", "-q", "--allow-empty", "-m", "drift", cwd=source)
    drift_sha = _git_output("rev-parse", "HEAD", cwd=source).strip()
    checkout = superproject / "vendor" / "lib"
    _git("fetch", "-q", cwd=checkout)
    _git("checkout", "-q", drift_sha, cwd=checkout)

    rc_plain, out_plain = hf._module_run_git(
        ["-C", str(superproject), "diff", "--name-only", "HEAD"]
    )
    assert rc_plain == 0 and out_plain.strip() == ""  # ignore=all 전제 실측.
    assert hf._dirty_paths_in_tree(
        str(superproject), hf._module_run_git,
    ) == ["vendor/lib"]


@requires_git_binary
def test_real_git_recursive_submodule_paths_are_superproject_relative(hf, tmp_path):
    """실 git — 재귀 status 의 중첩·공백 경로는 최상위 superproject 상대경로다."""
    leaf = tmp_path / "leaf"
    middle = tmp_path / "middle"
    superproject = tmp_path / "super"
    _init_real_repo(leaf)
    _init_real_repo(middle)
    _git(
        "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(leaf), "deps/inner lib", cwd=middle,
    )
    _git("commit", "-q", "-am", "add leaf", cwd=middle)
    _init_real_repo(superproject)
    _git(
        "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(middle), "vendor/outer lib", cwd=superproject,
    )
    _git("commit", "-q", "-am", "add middle", cwd=superproject)
    _git(
        "-c", "protocol.file.allow=always", "submodule", "update", "--init",
        "--recursive", cwd=superproject,
    )

    raw = _git_output("submodule", "status", "--recursive", cwd=superproject)
    nested = "vendor/outer lib/deps/inner lib"
    assert nested in raw  # Git 자체 출력 좌표계 실측.
    assert hf._submodule_paths_in_tree(
        str(superproject), hf._module_run_git,
    ) == ["vendor/outer lib", nested]


@requires_git_binary
def test_real_git_dirty_paths_respect_exclude_standard(hf, tmp_path):
    """실 git — tracked 수정 ∪ staged 신규 ∪ untracked 를 잡고 **gitignored 는 제외**한다.

    stub 이 아니라 실 바이너리로 `--exclude-standard` 의미를 못 박는다. gitignored 산출물
    (`.local/` 류 런타임 상태)이 판정에 섞이면 정상 핸드오프가 영구 차단된다.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
    (repo / "tracked.md").write_text("v1\n", encoding="utf-8")
    _git("add", ".gitignore", "tracked.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    (repo / "tracked.md").write_text("v2\n", encoding="utf-8")          # tracked 수정.
    (repo / "staged_new.md").write_text("new\n", encoding="utf-8")      # staged 신규.
    _git("add", "staged_new.md", cwd=repo)
    (repo / "untracked.md").write_text("u\n", encoding="utf-8")         # untracked.
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "runtime.json").write_text("{}", encoding="utf-8")

    paths = hf._dirty_paths_in_tree(str(repo), hf._module_run_git)

    assert sorted(paths) == ["staged_new.md", "tracked.md", "untracked.md"]
    assert not any("ignored_dir" in path for path in paths)


@requires_git_binary
def test_real_git_unborn_head_reports_untracked(hf, tmp_path):
    """실 git — 커밋 0 repo 도 판정된다(untracked 전량). `diff HEAD` 실패가 곧 '불명'이 아니다."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "first.md").write_text("first\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("x\n", encoding="utf-8")

    paths = hf._dirty_paths_in_tree(str(repo), hf._module_run_git)

    assert sorted(paths) == [".gitignore", "first.md"]


@requires_git_binary
def test_real_git_unborn_all_staged_is_dirty(hf, tmp_path):
    """실 git — 커밋 0 + 전량 `git add` 된 트리도 dirty(staged 축)로 잡힌다.

    `ls-files --others` 는 빈 목록을 돌려주므로 그 축만 보면 clean 오판이다.
    """
    repo = tmp_path / "staged-unborn"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "a.md").write_text("a\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.md").write_text("b\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("x\n", encoding="utf-8")
    _git("add", ".gitignore", "a.md", "sub/b.md", cwd=repo)

    # 전제 실측: untracked 축은 비어 있다(전량 staged) — 그래도 dirty 여야 한다.
    rc_others, out_others = hf._module_run_git(
        ["-C", str(repo), "ls-files", "--others", "--exclude-standard"]
    )
    assert rc_others == 0 and out_others.strip() == ""

    paths = hf._dirty_paths_in_tree(str(repo), hf._module_run_git)

    assert sorted(paths) == [".gitignore", "a.md", "sub/b.md"]


@requires_git_binary
def test_real_git_clean_tree_reports_empty(hf, tmp_path):
    """실 git — 전부 커밋된 트리는 빈 목록(=clean). 게이트가 통과시킨다."""
    repo = tmp_path / "clean"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "a.md").write_text("a\n", encoding="utf-8")
    _git("add", "a.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    assert hf._dirty_paths_in_tree(str(repo), hf._module_run_git) == []


@requires_git_binary
def test_real_git_non_repo_is_unjudgeable(hf, tmp_path, monkeypatch):
    """실 git — git repo 가 아닌 디렉토리는 None(판정 불가·비차단 경고 대상).

    `GIT_CEILING_DIRECTORIES` 로 상위 탐색을 tmp 경계에서 끊는다. TMPDIR 가 어쩌다 git repo 안에
    있는 환경(개발자 로컬·일부 CI)에서는 git 이 상위로 올라가 그 repo 를 찾아내 rc 0 을 내므로,
    고정하지 않으면 이 단언이 환경에 따라 흔들린다."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert hf._dirty_paths_in_tree(str(plain), hf._module_run_git) is None
