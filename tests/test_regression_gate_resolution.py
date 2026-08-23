"""회귀 게이트 해소 체인 — 4층 우선순위·게이트 종류 판정·두 도구 파리티 (T-0730).

`ticket_finish`·`pm_handoff` 의 회귀 게이트는 다음 순서로 해소한다. 앞선 층이 비어 있지 않은
값을 주면 거기서 멈춘다:

  1. areas.md 활성 **prefix** 행의 test_cmd   (multi-repo 네임스페이스 형상)
  2. areas.md 활성 **repo** 행의 test_cmd     (prefix 칼럼이 빈 무prefix 형상)
  3. local.conf 의 test_cmd                   (per-clone 명시 설정)
  4. None → 호출부가 솔로 `pytest tests/ -q` venv argv (도그푸딩 불변)

2·3층이 없으면 무prefix 채택자(현행 `pm-config repo add` 의 기본 등록 형상 — prefix 칼럼이
빈 값)에서 게이트가 항상 하드코딩 pytest 로 폴백해, `tests/` 가 없는 repo 의 회귀가 **항상
red** 로 오판된다. 판정도 게이트 종류로 가른다 — pytest 게이트는 기존 요약행(불변), 비-pytest
게이트는 exit code 0 만 green 이고 rc != 0 은 그대로 중단이다(fail-soft 아님).

체인의 단일 사본은 `pm_handoff._resolve_gate_cmd` 이고 `ticket_finish` 는 자기 board 로드
seam 만 얹어 위임한다. 그래도 두 도구는 **자기 이름의 표면**을 각각 노출하므로, 층별 케이스를
양쪽에 걸고(파라미터화) 결과 동일성까지 단언해 미러가 갈리면 red 가 되게 한다.

board 모듈은 stub 으로 주입한다 — 두 도구가 각자 board 로드 seam(`_load_board_module` /
`_load_board`)을 갖는 기존 관례를 그대로 타므로, 그 seam 하나만 갈아끼우면 실 areas.md·
local.conf 없이 층별 판정을 고정할 수 있다. 마지막 절(무prefix 채택자 형상)은 stub 없이 **실
board 모듈**을 tmp 채택자 홈에 묶어 등록→해소→판정을 끝까지 태운다.

도구는 패키지가 아니므로 importlib 동적 로드 (test_ticket_finish·test_pm_handoff 관용구).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 제보 환경(heru)에서 실측된 비-pytest 게이트 — `tests/` 없는 채택자의 실 회귀 명령.
NON_PYTEST_CMD = "env PYTHONPATH=src .venv/bin/python -m viewer bind"
PREFIX_ROW_CMD = "pytest tests/ -q --prefix-layer"
LOCAL_CONF_CMD = "go test ./..."


def _load_tool(name: str):
    """도구 모듈을 경로 로드한다 (도구는 패키지가 아니므로 importlib)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tf():
    return _load_tool("ticket_finish")


@pytest.fixture(scope="module")
def hf():
    return _load_tool("pm_handoff")


# 두 도구는 같은 해소·판정 표면을 노출한다 — seam 이름만 다르다.
_BOARD_SEAM = {"ticket_finish": "_load_board_module", "pm_handoff": "_load_board"}


@pytest.fixture(params=sorted(_BOARD_SEAM))
def tool(request, tf, hf):
    """(도구 모듈, board 로드 seam 이름) — 같은 케이스를 두 도구에 건다."""
    mod = tf if request.param == "ticket_finish" else hf
    return mod, _BOARD_SEAM[request.param]


class _StubBoard:
    """board 모듈 대역 — 해소 체인이 실제로 읽는 표면만 흉내낸다."""

    def __init__(self, *, areas_exists=True, rows=(), prefix=None, repos=(),
                 session=None, conf=None):
        self._areas_exists = areas_exists
        self._rows = list(rows)
        self._prefix = prefix
        self._repos = set(repos)
        self._session = session
        self._conf = dict(conf or {})

    def areas_file(self):
        exists = self._areas_exists

        class _Path:
            def exists(self):
                return exists

        return _Path()

    def id_prefix(self):
        return self._prefix

    def _areas_row_for_prefix(self, prefix):
        for row in self._rows:
            if row.get("prefix") == prefix:
                return row
        return None

    def _parse_areas(self):
        return ([], list(self._rows))

    def session_name(self):
        return self._session

    def _repo_from_session(self, session):
        """board `_repo_from_session` 과 동형 — `<repo>_<N>` 에서 repo 를 뗀다."""
        head, sep, tail = session.rpartition("_")
        if not sep or not head or not tail.isdigit():
            return None
        return head

    def registered_repos(self):
        return set(self._repos)

    def local_config(self):
        return dict(self._conf)


def _resolve(mod, seam, stub, monkeypatch):
    monkeypatch.setattr(mod, seam, lambda: stub)
    return mod._resolve_per_repo_test_cmd()


# ── 층별 우선순위 ────────────────────────────────────────────────────────

def test_layer1_prefix_row_wins(tool, monkeypatch):
    """1층 — 활성 prefix 행에 test_cmd 가 있으면 뒤 층을 보지 않는다."""
    mod, seam = tool
    stub = _StubBoard(
        rows=[{"repo": "heru", "prefix": "HERU", "test_cmd": PREFIX_ROW_CMD}],
        prefix="HERU",
        repos={"heru"},
        conf={"test.cmd": LOCAL_CONF_CMD},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == PREFIX_ROW_CMD


def test_layer2_repo_row_when_prefix_blank(tool, monkeypatch):
    """2층 — **이 티켓의 회귀 케이스**: prefix 칼럼이 빈 무prefix 형상(현행 repo add 기본).

    `id_prefix()` 가 None 이라 1층이 행에 도달하지 못한다. repo 칼럼으로 찾아야 한다 —
    이 케이스가 깨지면 `tests/` 없는 채택자의 회귀가 항상 red 로 오판된다.
    """
    mod, seam = tool
    stub = _StubBoard(
        rows=[{"repo": "heru", "prefix": "", "test_cmd": NON_PYTEST_CMD}],
        prefix=None,
        repos={"heru"},
        conf={"test.cmd": LOCAL_CONF_CMD},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == NON_PYTEST_CMD


def test_layer2_repo_resolved_from_session(tool, monkeypatch):
    """2층 — 활성 repo 는 세션명 `<repo>_<N>` 유도가 1순위다(등록 repo 2개여도 확정)."""
    mod, seam = tool
    stub = _StubBoard(
        rows=[
            {"repo": "other", "prefix": "", "test_cmd": "npm test"},
            {"repo": "heru", "prefix": "", "test_cmd": NON_PYTEST_CMD},
        ],
        prefix=None,
        repos={"heru", "other"},
        session="heru_1",
    )
    assert _resolve(mod, seam, stub, monkeypatch) == NON_PYTEST_CMD


def test_layer2_ambiguous_repo_falls_through(tool, monkeypatch):
    """2층 — 세션 유도 불가 + 등록 repo 2개 = 모호 → 그 층을 건너뛴다(추측 금지)."""
    mod, seam = tool
    stub = _StubBoard(
        rows=[
            {"repo": "a", "prefix": "", "test_cmd": "npm test"},
            {"repo": "b", "prefix": "", "test_cmd": NON_PYTEST_CMD},
        ],
        prefix=None,
        repos={"a", "b"},
        session=None,
        conf={"test.cmd": LOCAL_CONF_CMD},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == LOCAL_CONF_CMD


def test_layer2_unknown_session_repo_falls_through(tool, monkeypatch):
    """2층 — 세션이 가리키는 repo 행이 없으면(미등록) 다음 층으로."""
    mod, seam = tool
    stub = _StubBoard(
        rows=[{"repo": "other", "prefix": "", "test_cmd": "npm test"}],
        prefix=None,
        repos={"other"},
        session="heru_1",
        conf={"test.cmd": LOCAL_CONF_CMD},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == LOCAL_CONF_CMD


def test_layer3_local_conf_when_areas_absent(tool, monkeypatch):
    """3층 — areas.md 가 **아예 없는** 형상에서도 local.conf test_cmd 를 읽는다.

    3층이 areas 존재 가드 *안*에 있으면 이 케이스가 솔로 pytest 로 잘못 폴백한다.
    """
    mod, seam = tool
    stub = _StubBoard(areas_exists=False, conf={"test.cmd": LOCAL_CONF_CMD})
    assert _resolve(mod, seam, stub, monkeypatch) == LOCAL_CONF_CMD


def test_layer3_local_conf_when_rows_have_no_test_cmd(tool, monkeypatch):
    """3층 — prefix 행·repo 행이 있어도 test_cmd 가 빈 값이면 local.conf 로 내려간다."""
    mod, seam = tool
    stub = _StubBoard(
        rows=[{"repo": "heru", "prefix": "HERU", "test_cmd": ""}],
        prefix="HERU",
        repos={"heru"},
        conf={"test.cmd": LOCAL_CONF_CMD},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == LOCAL_CONF_CMD


def test_layer4_none_when_nothing_configured(tool, monkeypatch):
    """4층 — 아무것도 없으면 None → 호출부가 솔로 pytest argv(도그푸딩 불변)."""
    mod, seam = tool
    stub = _StubBoard(areas_exists=False, conf={})
    assert _resolve(mod, seam, stub, monkeypatch) is None


def test_board_load_failure_is_solo_fallback(tool, monkeypatch):
    """board 로드 실패(None)는 예외가 아니라 솔로 폴백이다."""
    mod, seam = tool
    monkeypatch.setattr(mod, seam, lambda: None)
    assert mod._resolve_per_repo_test_cmd() is None


def test_resolution_exception_is_solo_fallback(tool, monkeypatch):
    """해소 중 예외도 삼켜 솔로 폴백 — 게이트 해소가 도구를 죽이지 않는다."""
    mod, seam = tool

    class _Boom(_StubBoard):
        def id_prefix(self):
            raise RuntimeError("areas 파싱 실패")

    assert _resolve(mod, seam, _Boom(repos={"heru"}), monkeypatch) is None


def test_session_resolution_failure_falls_back_to_count(tool, monkeypatch):
    """세션 해소가 터져도 등록 repo 1개면 count-based 로 2층이 성립한다(fail-soft)."""
    mod, seam = tool

    class _SessionBoom(_StubBoard):
        def session_name(self):
            raise RuntimeError("lease 장부 손상")

    stub = _SessionBoom(
        rows=[{"repo": "heru", "prefix": "", "test_cmd": NON_PYTEST_CMD}],
        repos={"heru"},
    )
    assert _resolve(mod, seam, stub, monkeypatch) == NON_PYTEST_CMD


def test_ticket_finish_resolution_is_delegated_single_copy(tf, monkeypatch):
    """해소 체인의 사본은 하나다 — ticket_finish 는 pm_handoff seam 에 위임한다.

    pm_handoff 를 못 얻으면(부재/로드 실패) 해소 가능한 board 를 줘도 None(솔로 폴백)이다.
    체인을 ticket_finish 안에 다시 복제하면 이 단언이 깨진다 — 미러 이탈 재발 차단.
    """
    stub = _StubBoard(
        rows=[{"repo": "heru", "prefix": "", "test_cmd": NON_PYTEST_CMD}],
        repos={"heru"},
    )
    monkeypatch.setattr(tf, "_load_board_module", lambda: stub)
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: None)
    assert tf._resolve_per_repo_test_cmd() is None


# ── 게이트 종류 판정 ─────────────────────────────────────────────────────

def test_gate_is_pytest_classification(tool):
    """None(해소 실패)=솔로 pytest argv → True. pytest 토큰 포함 → True. 그 외 → False."""
    mod, _seam = tool
    assert mod._gate_is_pytest(None) is True
    assert mod._gate_is_pytest("pytest tests/ -q") is True
    assert mod._gate_is_pytest("python3 -m pytest tests/ -q") is True
    assert mod._gate_is_pytest(NON_PYTEST_CMD) is False
    assert mod._gate_is_pytest(LOCAL_CONF_CMD) is False


def test_non_pytest_gate_green_is_exit_code_only(tool):
    """비-pytest 게이트는 exit 0 만 green — 요약행이 없어도 green 이다."""
    mod, _seam = tool
    assert mod._regression_is_green("✅ 매니페스트 검증 통과", 0, NON_PYTEST_CMD) is True
    assert mod._regression_is_green("", 0, NON_PYTEST_CMD) is True


def test_non_pytest_gate_red_is_not_swallowed(tool):
    """fail-soft 가 아니다 — 비-pytest 게이트도 rc != 0 이면 red."""
    mod, _seam = tool
    assert mod._regression_is_green("아무 출력", 1, NON_PYTEST_CMD) is False
    assert mod._regression_is_green("✅ 통과처럼 보이는 출력", 2, NON_PYTEST_CMD) is False


def test_pytest_gate_verdict_unchanged(tool):
    """pytest 게이트 판정은 기존 요약행 경로 그대로(도그푸딩 무회귀)."""
    mod, _seam = tool
    green = "1472 passed, 24 deselected in 12.34s"
    assert mod._regression_is_green(green, 0, None) is True
    assert mod._regression_is_green(green, 0, "pytest tests/ -q") is True
    # 요약행 없음 → pytest 게이트는 red (exit 0 이어도 파싱 실패는 green 아님).
    assert mod._regression_is_green("요약행 없는 출력", 0, None) is False
    # 요약행에 failed 가 있으면 red.
    assert mod._regression_is_green("5 failed, 1467 passed in 10.00s", 1, None) is False


def test_gate_label_shows_solo_argv_for_none(tool):
    """안내 문구 — 해소 실패(None)는 실제 실행 argv 라벨을 보여준다."""
    mod, _seam = tool
    assert mod._gate_label(None) == "pytest tests/ -q"
    assert mod._gate_label(NON_PYTEST_CMD) == NON_PYTEST_CMD


# ── 두 도구 파리티 (해소·판정이 갈리면 red) ──────────────────────────────
#
# 위 케이스는 도구별로 같은 기대값을 단언한다. 아래는 **같은 입력에 두 도구가 같은 답을
# 낸다**는 관계 자체를 직접 못박는다 — 한쪽만 고친 반쪽 수정이 여기서 걸린다.

_PARITY_BOARDS = {
    "prefix_row": _StubBoard(
        rows=[{"repo": "heru", "prefix": "HERU", "test_cmd": PREFIX_ROW_CMD}],
        prefix="HERU", repos={"heru"}, conf={"test.cmd": LOCAL_CONF_CMD}),
    "repo_row_blank_prefix": _StubBoard(
        rows=[{"repo": "heru", "prefix": "", "test_cmd": NON_PYTEST_CMD}],
        prefix=None, repos={"heru"}, conf={"test.cmd": LOCAL_CONF_CMD}),
    "ambiguous_repos": _StubBoard(
        rows=[{"repo": "a", "prefix": "", "test_cmd": "npm test"},
              {"repo": "b", "prefix": "", "test_cmd": NON_PYTEST_CMD}],
        prefix=None, repos={"a", "b"}, conf={"test.cmd": LOCAL_CONF_CMD}),
    "areas_absent": _StubBoard(areas_exists=False, conf={"test.cmd": LOCAL_CONF_CMD}),
    "nothing": _StubBoard(areas_exists=False, conf={}),
}

_PARITY_EXPECTED = {
    "prefix_row": PREFIX_ROW_CMD,
    "repo_row_blank_prefix": NON_PYTEST_CMD,
    "ambiguous_repos": LOCAL_CONF_CMD,
    "areas_absent": LOCAL_CONF_CMD,
    "nothing": None,
}


@pytest.mark.parametrize("case", sorted(_PARITY_BOARDS))
def test_resolution_parity_across_tools(tf, hf, monkeypatch, case):
    """같은 board 에서 두 도구의 해소 결과가 같다 — 그리고 기대값과도 같다(비-vacuous)."""
    stub = _PARITY_BOARDS[case]
    monkeypatch.setattr(tf, "_load_board_module", lambda: stub)
    monkeypatch.setattr(hf, "_load_board", lambda: stub)
    finish_cmd = tf._resolve_per_repo_test_cmd()
    handoff_cmd = hf._resolve_per_repo_test_cmd()
    assert finish_cmd == handoff_cmd, f"{case}: 두 도구의 게이트 해소가 갈렸다"
    assert finish_cmd == _PARITY_EXPECTED[case]


@pytest.mark.parametrize(
    "output,returncode,gate_cmd,expected",
    [
        ("1472 passed in 1.00s", 0, None, True),
        ("1472 passed in 1.00s", 1, None, False),
        ("요약행 없음", 0, None, False),
        ("1472 passed in 1.00s", 0, "pytest tests/ -q", True),
        ("✅ 매니페스트 검증 통과", 0, NON_PYTEST_CMD, True),
        ("✅ 매니페스트 검증 통과", 1, NON_PYTEST_CMD, False),
        ("", 0, LOCAL_CONF_CMD, True),
    ],
)
def test_verdict_parity_across_tools(tf, hf, output, returncode, gate_cmd, expected):
    """green 판정도 두 도구가 같다 — 게이트 종류 분기가 한쪽에만 들어가면 red."""
    finish_verdict = tf._regression_is_green(output, returncode, gate_cmd)
    handoff_verdict = hf._regression_is_green(output, returncode, gate_cmd)
    assert finish_verdict == handoff_verdict, "두 도구의 회귀 판정이 갈렸다"
    assert finish_verdict is expected


def test_gate_surface_parity_across_tools(tf, hf):
    """두 도구가 같은 이름의 게이트 표면과 같은 라벨 상수를 노출한다."""
    for name in ("_resolve_per_repo_test_cmd", "_gate_is_pytest", "_gate_label",
                 "_regression_is_green"):
        assert callable(getattr(tf, name)), f"ticket_finish 에 {name} 부재"
        assert callable(getattr(hf, name)), f"pm_handoff 에 {name} 부재"
    assert tf._DEFAULT_GATE_LABEL == hf._DEFAULT_GATE_LABEL
    assert tf._PYTEST_GATE_TOKEN == hf._PYTEST_GATE_TOKEN
    for gate_cmd in (None, "pytest tests/ -q", NON_PYTEST_CMD, LOCAL_CONF_CMD):
        assert tf._gate_is_pytest(gate_cmd) == hf._gate_is_pytest(gate_cmd)
        assert tf._gate_label(gate_cmd) == hf._gate_label(gate_cmd)


# ── 무prefix 채택자 형상 e2e (repo add + tests/ 없음 + 비-pytest 게이트) ──
#
# 도그푸딩 형상은 prefix 유도가 서므로 이 결함이 보이지 않았다([[dogfooding-blind-spot-
# adopter-shape]]). 여기서는 stub 없이 **실 board 모듈**을 tmp 채택자 홈에 묶고, 실제 등록
# 표면이 쓰는 writer 로 행을 남긴 뒤, 두 도구가 그 행을 읽어 green 판정까지 가는지 본다.


class _FakeBoardForRepoAdd:
    """`pm-config repo add` 의 areas 기록만 관찰하는 board 대역 (부작용 0)."""

    def __init__(self):
        self.append_calls: list[dict] = []

    def registered_repos(self):
        return set()

    def registered_prefixes(self):
        return set()

    def areas_append(self, prefix, area, owner, *, repo=None, git=None,
                     test_cmd=None, base=None, protected=None, area_owner=None):
        self.append_calls.append({"prefix": prefix, "repo": repo,
                                  "test_cmd": test_cmd, "base": base})

    def _repo_protected(self, repo):
        return ["main", "master", "develop"]

    def _areas_git_url(self, repo):
        return None


class _GitFakeForRepoAdd:
    """repo add 의 git 호출 대역 — clone/refspec/HEAD 해소만 흉내낸다(네트워크 0)."""

    def __call__(self, argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return 0, "true\n"
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return 0, "0123abc\n"
        if "symbolic-ref" in argv:
            return 0, "refs/heads/main\n"
        return 0, ""


def test_repo_add_registers_blank_prefix_with_test_cmd(tmp_path):
    """`repo add` 는 prefix 를 **빈 값**으로 등록한다 — 이 결함의 전제 형상 못박기.

    prefix=작업 카테고리이지 repo 명이 아니라서 자동시드가 폐지됐다. 그 결과 prefix 칼럼만
    보는 해소는 채택자 기본 등록 형상에서 행에 도달하지 못한다.
    """
    pm_config = _load_tool("pm_config")
    board = _FakeBoardForRepoAdd()
    args = argparse.Namespace(name="heru", git="git@h:me/heru.git",
                              test=NON_PYTEST_CMD, owner="me", base=None)
    rc = pm_config.cmd_repo_add(args, board=board, clone_runner=_GitFakeForRepoAdd(),
                                repos_dir=tmp_path / ".repos")
    assert rc == 0
    assert board.append_calls == [
        {"prefix": "", "repo": "heru", "test_cmd": NON_PYTEST_CMD, "base": "main"},
    ]


@pytest.fixture
def adopter_board(tmp_path, monkeypatch):
    """무prefix 채택자 홈에 묶인 **실** board 모듈 — areas.md 는 실 writer 가 쓴다.

    board.py 의 경로 전역은 import 시점에 실 REPO 로 굳으므로 tmp 로 재지정한다
    (test_board_per_repo 의 hermetic 관례). `tests/` 는 만들지 않는다 — 채택자 형상.
    """
    pm_dir = tmp_path / ".project_manager"
    (pm_dir / ".local").mkdir(parents=True, exist_ok=True)
    board = _load_tool("board")
    for name, value in {
        "REPO": tmp_path,
        "AREAS_FILE": pm_dir / "areas.md",
        "LOCAL_CONF": pm_dir / "local.conf",
        "LOCAL_DIR": pm_dir / ".local",
        "BOARD_LOCK": pm_dir / ".local" / "board.lock",
        "LEASES_FILE": pm_dir / ".local" / "worktree-leases.json",
    }.items():
        monkeypatch.setattr(board, name, value)
    # 실 PM 세션 env 가 hermetic 해소로 새지 않게 제거(장부/local.conf 만이 세션 소스).
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # `pm-config repo add` 가 부르는 그 writer 로 등록 — prefix 는 빈 값이다.
    board.areas_append("", "", "me", repo="heru", git="git@h:me/heru.git",
                       test_cmd=NON_PYTEST_CMD, base="main", protected="",
                       area_owner="me@example.com")
    assert not (tmp_path / "tests").exists()   # 채택자 형상 — pytest 스위트가 없다.
    return board


def test_no_prefix_adopter_shape_resolves_real_areas_row(adopter_board, tf, hf,
                                                         monkeypatch):
    """실 areas.md(prefix 빈 값)에서 두 도구가 채택자 게이트를 해소한다 — 하드코딩 폴백 아님."""
    assert adopter_board.id_prefix() is None      # 제보 환경 실측과 같은 상태.
    assert adopter_board.registered_prefixes() == set()
    assert adopter_board.registered_repos() == {"heru"}
    monkeypatch.setattr(tf, "_load_board_module", lambda: adopter_board)
    monkeypatch.setattr(hf, "_load_board", lambda: adopter_board)
    assert tf._resolve_per_repo_test_cmd() == NON_PYTEST_CMD
    assert hf._resolve_per_repo_test_cmd() == NON_PYTEST_CMD


def test_no_prefix_adopter_ticket_finish_completes_green(adopter_board, tf, tmp_path,
                                                         monkeypatch, capsys):
    """채택자 형상 완료 기록 — 비-pytest 게이트가 exit 0 이면 green 으로 완주한다(rc 0).

    수정 전엔 하드코딩 `pytest tests/ -q` 로 폴백해 `tests/` 부재("no tests ran")로 항상
    red 였다.
    """
    monkeypatch.setattr(tf, "_load_board_module", lambda: adopter_board)
    log_file = tmp_path / "log.md"
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "✅ 매니페스트 검증 통과 — 전략 13"),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "채택자 티켓",
        affected_domain_fn=lambda tid: [],
        log_file=log_file,
    )
    rc = finisher.run("T-0035", section=None, dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"green: `{NON_PYTEST_CMD}` (exit 0)" in out
    assert "테스트 수 미측정" in out
    assert "[완료] T-0035 기록 완료." in out
    # 테스트 수는 측정하지 않는다 — log 스켈레톤은 `--no-pytest` 경로와 같은 "?" 를 싣는다.
    assert "?" in log_file.read_text(encoding="utf-8")


def test_no_prefix_adopter_ticket_finish_red_on_nonzero_rc(adopter_board, tf, tmp_path,
                                                           monkeypatch, capsys):
    """fail-soft 가 아니다 — 비-pytest 게이트가 rc != 0 이면 기록 전에 중단(rc 1·부작용 0)."""
    monkeypatch.setattr(tf, "_load_board_module", lambda: adopter_board)
    log_file = tmp_path / "log.md"
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (1, "✗ 매니페스트 검증 실패"),
        run_board_fn=lambda args: pytest.fail("red 인데 board complete 가 불렸다"),
        run_git_fn=lambda args: pytest.fail("red 인데 git 이 불렸다"),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "채택자 티켓",
        affected_domain_fn=lambda tid: [],
        # 잔여 preflight 는 이 테스트의 초점(비-pytest 게이트 rc 판정)과 무관하다 — 실
        # board_py/실 git 을 타지 않게 off 로 그 초점만 남긴다.
        residual_block_fn=lambda tid: None,
        log_file=log_file,
    )
    rc = finisher.run("T-0035", section=None, dry_run=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert "회귀 red" in err
    assert f"`{NON_PYTEST_CMD}` (exit 1)" in err
    assert not log_file.exists()      # log 스켈레톤 append 도 없다.


def test_no_prefix_adopter_handoff_verdict_and_command(adopter_board, hf, monkeypatch):
    """핸드오프도 같은 게이트로 판정하고, 실행은 그 명령을 shell 로 돌린다."""
    monkeypatch.setattr(hf, "_load_board", lambda: adopter_board)
    gate_cmd = hf._resolve_per_repo_test_cmd()
    assert gate_cmd == NON_PYTEST_CMD
    assert hf._regression_is_green("✅ 매니페스트 검증 통과", 0, gate_cmd) is True
    assert hf._regression_is_green("✗ 실패", 1, gate_cmd) is False

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell", False)

        class _Result:
            returncode = 0
            stdout = "✅ 매니페스트 검증 통과"
            stderr = ""

        return _Result()

    monkeypatch.setattr(hf.subprocess, "run", fake_run)
    handoff = hf.PmHandoff(venv_python="/venv/bin/python",
                           run_git_fn=lambda args: (0, ""))
    returncode, output = handoff._default_run_pytest()
    assert returncode == 0
    assert captured["cmd"] == NON_PYTEST_CMD      # 하드코딩 pytest argv 아님.
    assert captured["shell"] is True
    assert "매니페스트" in output


def test_solo_shape_keeps_venv_pytest_argv(hf, monkeypatch):
    """도그푸딩 불변 — 해소 실패(솔로)면 핸드오프는 현행 venv pytest argv 를 그대로 쓴다."""
    monkeypatch.setattr(hf, "_resolve_per_repo_test_cmd", lambda: None)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell", False)

        class _Result:
            returncode = 0
            stdout = "1 passed in 0.01s"
            stderr = ""

        return _Result()

    monkeypatch.setattr(hf.subprocess, "run", fake_run)
    handoff = hf.PmHandoff(venv_python="/venv/bin/python",
                           run_git_fn=lambda args: (0, ""))
    handoff._default_run_pytest()
    assert captured["cmd"] == ["/venv/bin/python", "-m", "pytest", "tests/", "-q"]
    assert captured["shell"] is False

