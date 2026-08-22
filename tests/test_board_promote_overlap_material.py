"""발행 시점 touches 겹침 + 가용 슬롯 재료 — `new`/`promote` 판단 지점 (T-0778).

PM 이 판단하는 자리(티켓 분할·병렬 판정)에 그 판단에 필요한 실측이 없었다는 실증(PM 42차)을
엔진으로 닫는다. `new`/`promote` 가 다른 활성·draft 티켓과의 touches 겹침 + 가용(idle) 슬롯 수를
**stderr 재료**로 낸다 — 절대 차단하지 않는다(판정은 PM). 이 파일이 고정하는 축:

  1. 겹침 1건 이상이면 **내 touch 경로별 집계**(겹침 수 오름차순·경로 상한 8·경로당 ID 상한 6)를
     낸다. 겹침 0이면 슬롯 줄까지 포함해 **전부 침묵**.
  2. 슬롯 수는 lease 장부 실측(`identity_args.repo_slot_state_counts`)에서 오고 하드코딩이 아니다.
  3. 좌표 정규화는 `repo_coordinates` 단일 소유자(workspace 없이 lease 매핑) — `work/<repo>_<N>/`
     접두 선언과 무접두 선언이 같은 파일로 만난다.
  4. 자기 자신·done·손상 frontmatter·형식 불명/빈 touches 후보가 있어도 크래시 0·rc 불변.
  5. 이 축은 **차단하지 않는다** — 겹치는 draft/open 티켓의 `promote`는 rc==0 이다.
  6. board 비-git(legacy) 형상에서도 재료가 나온다.

hermetic: board.py 의 경로 전역은 import 시점에 실 repo 로 굳으므로 tmp 홈으로 재앵커한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 git 통합 케이스 skip.")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_board():
    spec = importlib.util.spec_from_file_location(
        "board_overlap_material", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(argv, cwd):
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _write_ledger(mod, *leases):
    """리스 장부 파일에 엔트리를 직접 쓴다(worktree_pool atomic write 동형 스키마) —
    `tests/test_board_per_repo.py` 의 `_write_ledger`/`_lease_row` 패턴과 동형."""
    mod.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod.LEASES_FILE.write_text(json.dumps({"leases": list(leases)}), encoding="utf-8")


def _lease_row(*, slot, repo, state="leased", session="s"):
    return {"slot": slot, "repo": repo, "branch": None, "session": session,
            "pid": 1, "started": "t", "state": state, "test_cmd": None}


def _register_slot(root: Path, *, slot: str = "work/demo_1", repo: str = "demo") -> Path:
    """리스 장부에 지속 slot↔repo 매핑을 심고 그 worktree 디렉터리를 만든다(T-0776 패턴 재사용)."""
    ledger = root / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": slot, "repo": repo, "state": "leased"}]}),
        encoding="utf-8")
    path = root / slot
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed(mod, status: str, tid: str, touches, **extra) -> Path:
    """활성/draft 후보 티켓 하나를 `tickets_dir()/<status>` (또는 `.drafts`)에 직접 심는다."""
    directory = mod.drafts_dir() if status == "draft" else (mod.tickets_dir() / status)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tid}-x.md"
    fm = {"id": tid, "title": "x", "status": status, "touches": touches,
          "depends_on": [], "blocks": [], "tags": []}
    fm.update(extra)
    mod.dump_ticket(path, fm, "# x\n\n## 목표\nx\n")
    return path


@pytest.fixture
def anchored(tmp_path, monkeypatch):
    """board-git 없이 헬퍼 함수만 태우는 tmp 홈."""
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    (tmp_path / ".project_manager" / "board" / "tickets").mkdir(parents=True)
    monkeypatch.setattr(
        mod, "LEASES_FILE", tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    return mod


# ════════════════════════════════════════════════════════════════════════
# 1. 겹침 집계 — 경로별 오름차순·상한 8/6·자기 제외·done 제외·fail-soft
# ════════════════════════════════════════════════════════════════════════

def test_no_candidates_is_silent_even_with_resolved_ledger(anchored):
    """겹침 0 — 슬롯 줄까지 전부 침묵(다른 티켓이 아예 없어도 헤더가 안 나온다)."""
    _write_ledger(anchored, _lease_row(slot="work/demo_1", repo="demo", state="idle"))
    anchored.REPO = anchored.REPO  # no-op(가독)
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines == []


def test_overlapping_ticket_is_reported_with_id_and_count(anchored):
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines[0] == anchored._PUBLISH_OVERLAP_HEADER
    assert any("tools/x.py" in ln and "T-0002" in ln and "1건" in ln for ln in lines)


def test_self_id_is_excluded(anchored):
    """방금 만든 티켓 자신은 후보에서 빠진다(자기 자신과는 항상 겹치므로 제외하지 않으면 오탐)."""
    _seed(anchored, "open", "T-0001", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines == []


def test_done_status_is_not_a_candidate(anchored):
    _seed(anchored, "done", "T-0002", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines == []


def test_draft_status_is_a_candidate(anchored):
    """draft 는 board.md·list·board-git 어디에도 안 보이지만 좌표를 이미 점유한다."""
    _seed(anchored, "draft", "T-0002", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("T-0002" in ln for ln in lines)


def test_corrupted_candidate_frontmatter_is_skipped_fail_soft(anchored, capsys):
    """손상 frontmatter 후보 1건이 있어도 나머지 판정은 계속되고 크래시하지 않는다."""
    bad = anchored.tickets_dir() / "open" / "T-bad-x.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not: [valid yaml\n---\nbody", encoding="utf-8")
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("T-0002" in ln for ln in lines)


def test_candidate_with_unclear_touches_format_is_dropped_not_crashed(anchored):
    """후보 touches 가 리스트가 아닌 스칼라(형식 불명)면 그 후보만 조용히 빠진다."""
    path = anchored.tickets_dir() / "open" / "T-0002-x.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    anchored.dump_ticket(path, {
        "id": "T-0002", "title": "x", "status": "open", "touches": "tools/x.py",
        "depends_on": [], "blocks": [], "tags": [],
    }, "# x\n")
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines == []


def test_candidate_with_empty_touches_is_dropped_not_crashed(anchored):
    _seed(anchored, "open", "T-0002", [])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert lines == []


def test_own_touches_unclear_format_yields_silence(anchored):
    """대상 자신의 touches 가 형식 불명(스칼라)이면 판정불능으로 침묵(크래시 없음)."""
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", "tools/x.py", repo="demo")  # str, not list
    assert lines == []


def test_own_touches_empty_yields_silence(anchored):
    lines = anchored._publish_overlap_material("T-0001", [], repo="demo")
    assert lines == []


def test_paths_sorted_ascending_by_overlap_count_and_capped(anchored):
    """오름차순 정렬 + 총 줄 수(헤더+경로+잔여요약) 상한 9(리뷰 F-003 — 슬롯 줄 없을 때 경로
    표시 몫은 8이 아니라 잔여-요약 1줄을 뺀 7이다)."""
    own_touches = [f"tools/f{i}.py" for i in range(10)]
    # f0 은 1건, f1 은 2건 ... f9 는 10건 겹치게 후보를 심는다(오름차순 검증용 계단형).
    counter = 0
    for i, own_path in enumerate(own_touches):
        overlap_n = i + 1
        for j in range(overlap_n):
            counter += 1
            _seed(anchored, "open", f"T-{1000 + counter}", [own_path])
    lines = anchored._publish_overlap_material("T-0001", own_touches, repo="demo")
    assert lines[0] == anchored._PUBLISH_OVERLAP_HEADER
    assert len(lines) <= anchored._PUBLISH_OVERLAP_TOTAL_LINE_BUDGET
    body = lines[1:]
    # 슬롯 줄이 없으므로 예산은 헤더 1을 뺀 8 — 경로 10개가 그 예산을 넘으므로 잔여-요약 1줄이
    # 그 예산을 먹어 실제 경로 표시는 7줄이다(8 - 1).
    path_lines = [ln for ln in body if "겹침" in ln]
    assert len(path_lines) == anchored._PUBLISH_OVERLAP_PATH_LIMIT - 1
    assert any("외 3개 경로" in ln for ln in body)
    # 오름차순 — 가장 적게 겹치는(f0=1건) 경로가 먼저 온다.
    assert "f0.py: 1건" in path_lines[0]
    assert "f6.py: 7건" in path_lines[-1]


def test_id_list_capped_at_six_and_remainder_folded_by_count(anchored):
    for i in range(9):
        _seed(anchored, "open", f"T-{2000 + i}", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    target = next(ln for ln in lines if "tools/x.py" in ln)
    assert "9건 겹침" in target
    assert target.count("T-2") == 6
    assert "외 3건" in target


def test_directory_and_file_declarations_meet_on_the_same_path_axis(anchored):
    """내 touches 가 디렉터리(`tests`)와 파일(`tools/x.py`)을 함께 선언하면 각 축이 후보와
    독립적으로 만난다 — 디렉터리 선언 후보(`tests/sub/y.py`)는 내 `tests` 버킷에 잡히고,
    파일 선언 후보(`tools/x.py`)는 내 `tools/x.py` 버킷에 잡힌다."""
    _seed(anchored, "open", "T-0002", ["tests/sub/y.py"])
    _seed(anchored, "open", "T-0003", ["tools/x.py"])
    lines = anchored._publish_overlap_material("T-0001", ["tests", "tools/x.py"], repo="demo")
    tests_line = next(ln for ln in lines if ln.strip().startswith("tests:"))
    file_line = next(ln for ln in lines if "tools/x.py" in ln)
    assert "T-0002" in tests_line
    assert "T-0003" in file_line


# ════════════════════════════════════════════════════════════════════════
# 2. 좌표 정규화 — work/<repo>_<N>/ 접두 혼재
# ════════════════════════════════════════════════════════════════════════

def test_prefixed_touch_normalizes_and_overlaps_bare_declaration(anchored, tmp_path):
    """`work/<repo>_1/tests` 접두 선언과 무접두 `tests` 선언이 같은 파일로 만난다."""
    _register_slot(tmp_path, slot="work/demo_1", repo="demo")
    _seed(anchored, "open", "T-0002", ["work/demo_1/tests"])
    lines = anchored._publish_overlap_material("T-0001", ["tests"], repo="demo")
    assert any("T-0002" in ln for ln in lines)


def test_prefixed_touch_without_ledger_row_drops_without_crash(anchored):
    """등록 안 된 슬롯 접두는 항목 단위로 드롭 — 크래시 0·판정 계속."""
    _seed(anchored, "open", "T-0002", ["work/unregistered_9/tests"])
    _seed(anchored, "open", "T-0003", ["tests"])
    lines = anchored._publish_overlap_material("T-0001", ["tests"], repo="demo")
    # 미등록 접두(T-0002)는 드롭되어 안 보이고, 정상 무접두(T-0003)는 그대로 겹침으로 잡힌다.
    joined = "\n".join(lines)
    assert "T-0003" in joined
    assert "T-0002" not in joined


# ════════════════════════════════════════════════════════════════════════
# 3. 가용(idle) 슬롯 수 — lease 장부 실측(하드코딩 아님)
# ════════════════════════════════════════════════════════════════════════

def test_idle_slot_count_follows_ledger_not_hardcoded(anchored):
    """`repo` 인자로 전달된 canonical 값을 그대로 장부 조회에 쓴다 — `anchored.REPO.name`(물리
    tmp 디렉터리명)과는 무관하게 idle 수가 장부를 그대로 추종한다(리뷰 F-002 — 재료가 REPO.name
    을 다시 유도하지 않는다는 것을 이 테스트에서도 고정한다)."""
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    assert anchored.REPO.name != "demo", "REPO.name 이 우연히 demo 와 같으면 이 테스트가 무의미해진다."

    _write_ledger(anchored, _lease_row(slot="work/A_1", repo="demo", state="idle"),
                  _lease_row(slot="work/A_2", repo="demo", state="idle"),
                  _lease_row(slot="work/A_3", repo="demo", state="idle"),
                  _lease_row(slot="work/A_4", repo="demo", state="leased"))
    lines_idle3 = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("idle) 슬롯 3개" in ln for ln in lines_idle3)

    _write_ledger(anchored, _lease_row(slot="work/A_1", repo="demo", state="leased"))
    lines_idle0 = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("idle) 슬롯 0개" in ln for ln in lines_idle0)


def test_creating_state_is_not_counted_as_idle(anchored):
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    _write_ledger(anchored, _lease_row(slot="work/A_1", repo="demo", state="creating"))
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("idle) 슬롯 0개" in ln for ln in lines)


def test_missing_ledger_omits_slot_line_but_keeps_overlap(anchored):
    """장부 부재 — 슬롯 줄은 생략되지만 겹침 재료 자체는 여전히 나온다(판정불능≠전체 침묵)."""
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    assert not anchored.LEASES_FILE.exists()
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("T-0002" in ln for ln in lines)
    assert not any("idle" in ln for ln in lines)


def test_corrupt_ledger_omits_slot_line_without_crash(anchored):
    _seed(anchored, "open", "T-0002", ["tools/x.py"])
    anchored.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    anchored.LEASES_FILE.write_text("{ not json", encoding="utf-8")
    lines = anchored._publish_overlap_material("T-0001", ["tools/x.py"], repo="demo")
    assert any("T-0002" in ln for ln in lines)
    assert not any("idle" in ln for ln in lines)


# ════════════════════════════════════════════════════════════════════════
# 4. e2e — 실 CLI(`cmd_new`/`cmd_promote`) stderr 재료 + 비차단 + 기계 채널 불변
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def board_git(tmp_path, monkeypatch):
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod, "LEASES_FILE", tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    board = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(
        (REPO / ".project_manager" / "wiki" / "tickets" / "_template.md")
        .read_text(encoding="utf-8"), encoding="utf-8")
    bare = tmp_path / "bare"
    steps = (
        (["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path),
        (["init", "-q", "-b", "main"], board),
        (["remote", "add", "origin", str(bare)], board),
        (["add", "-A"], board),
        (["commit", "-qm", "board init"], board),
        (["push", "-q", "-u", "origin", "main"], board),
    )
    for argv, cwd in steps:
        r = _git(argv, cwd)
        assert r.returncode == 0, f"board-git setup 실패: git {argv} → {r.stderr}"
    mod._board_dir = board
    return mod


def _new_args(**overrides) -> argparse.Namespace:
    args = dict(title="겹침 재료", touches=None, depends=None, tag=None,
                estimate="small", prefix=None, user=None, session=None, design=None)
    args.update(overrides)
    return argparse.Namespace(**args)


_FILLED_BODY = (
    "# T-NNNN — 제목\n\n"
    "## 목표\n실 목표.\n\n"
    "## 인터페이스\n실 인터페이스.\n\n"
    "## 결정\n실 결정.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 산출물\n\n"
    "## 참고\n- 참고\n\n## 메모\n"
)


@requires_git
def test_cmd_new_emits_overlap_material_on_stderr(board_git, capsys):
    board_dir = board_git._board_dir
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": [".project_manager/tools/board.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))

    rc = board_git.cmd_new(_new_args(touches=".project_manager/tools/board.py"))
    assert rc == 0
    err = capsys.readouterr().err
    assert board_git._PUBLISH_OVERLAP_HEADER in err
    assert "T-0002" in err
    assert ".project_manager/tools/board.py" in err


@requires_git
def test_cmd_promote_emits_material_and_never_blocks(board_git, capsys):
    """겹침이 있어도 승격은 성공한다(rc==0) — 재료는 판정 위가 아니라 옆이다."""
    board_dir = board_git._board_dir
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": [".project_manager/tools/board.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))

    assert board_git.cmd_new(_new_args(
        touches=".project_manager/tools/board.py")) == 0
    draft = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    fm, _ = board_git.load_ticket(draft)
    tid = fm["id"]
    board_git.dump_ticket(draft, fm, _FILLED_BODY.replace("T-NNNN", tid))
    capsys.readouterr()

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 0, "touches 겹침이 승격을 막았다 — never-block 계약 위반."
    err = capsys.readouterr().err
    assert board_git._PUBLISH_OVERLAP_HEADER in err
    assert "T-0002" in err


@requires_git
def test_no_overlap_promote_has_no_material_header(board_git, capsys):
    """겹침이 없으면 promote 성공 stderr 에 재료 헤더 자체가 없다(기존 draft/sync 경고와는 별개 축)."""
    board_dir = board_git._board_dir
    assert board_git.cmd_new(_new_args(touches="unique/path/only_here.py")) == 0
    draft = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    fm, _ = board_git.load_ticket(draft)
    tid = fm["id"]
    board_git.dump_ticket(draft, fm, _FILLED_BODY.replace("T-NNNN", tid))
    capsys.readouterr()

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 0
    err = capsys.readouterr().err
    assert board_git._PUBLISH_OVERLAP_HEADER not in err


@requires_git
def test_stdout_machine_channel_is_unchanged(board_git, capsys):
    """재료는 stderr 전용 — stdout 의 `created <ID>` 형식은 무변경(기계 채널 불변)."""
    board_dir = board_git._board_dir
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": [".project_manager/tools/board.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))

    rc = board_git.cmd_new(_new_args(touches=".project_manager/tools/board.py"))
    assert rc == 0
    out = capsys.readouterr().out
    assert re.match(r"created (T-\S+)", out) is not None
    assert board_git._PUBLISH_OVERLAP_HEADER not in out


# ════════════════════════════════════════════════════════════════════════
# 4b. 리뷰 fix — F-002(canonical repo 정체성) · F-003(총 9줄 상한 실 진입점 확인)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_cmd_new_reports_registered_repo_idle_count_not_physical_folder_name(
        board_git, capsys):
    """F-002 재현 — `--repo`가 물리 tmp 디렉터리명(REPO.name)과 다른 등록명이면, 그 등록명으로
    장부를 조회해 idle 수를 낸다. 고치기 전에는 REPO.name 으로 조회해 실제 idle 2 를 0 으로 냈다."""
    assert board_git.REPO.name != "canonical", "REPO.name 이 우연히 canonical 과 같으면 이 테스트가 무의미해진다."
    board_dir = board_git._board_dir
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": ["tools/x.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))
    _write_ledger(
        board_git,
        _lease_row(slot="work/canonical_1", repo="canonical", session="canonical_1", state="leased"),
        _lease_row(slot="work/canonical_2", repo="canonical", session="canonical_2", state="idle"),
        _lease_row(slot="work/canonical_3", repo="canonical", session="canonical_3", state="idle"))

    rc = board_git.cmd_new(_new_args(repo="canonical", touches="tools/x.py"))
    assert rc == 0
    err = capsys.readouterr().err
    assert "idle) 슬롯 2개" in err, f"REPO.name 을 다시 유도했다면 0개가 나온다: {err!r}"


@requires_git
def test_cmd_promote_reports_registered_repo_idle_count_from_ticket_provenance(
        board_git, capsys):
    """F-002 재현(promote 축) — `promote`는 `--repo` 인자가 없다. 티켓 `created_by` provenance 의
    세션(`canonical_1`)을 장부로 재해소해 canonical repo(`canonical`)를 얻어야 한다(REPO.name 아님)."""
    assert board_git.REPO.name != "canonical"
    board_dir = board_git._board_dir
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": ["tools/x.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))

    assert board_git.cmd_new(_new_args(
        touches="tools/x.py", repo="canonical", slot=1)) == 0
    draft = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    fm, _ = board_git.load_ticket(draft)
    tid = fm["id"]
    assert fm["created_by"].rsplit("/", 1)[-1] == "canonical_1", fm["created_by"]
    board_git.dump_ticket(draft, fm, _FILLED_BODY.replace("T-NNNN", tid))
    capsys.readouterr()

    _write_ledger(
        board_git,
        _lease_row(slot="work/canonical_1", repo="canonical", session="canonical_1", state="leased"),
        _lease_row(slot="work/canonical_2", repo="canonical", session="canonical_2", state="idle"),
        _lease_row(slot="work/canonical_3", repo="canonical", session="canonical_3", state="idle"))

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 0
    err = capsys.readouterr().err
    assert "idle) 슬롯 2개" in err, f"REPO.name 을 다시 유도했다면 0개가 나온다: {err!r}"


@requires_git
def test_cmd_new_material_total_lines_stay_within_budget_with_slot_and_many_candidates(
        board_git, capsys):
    """F-003 재현 — idle 장부가 해소된 상태에서 겹침 경로 12개(각 20건 이상 겹침·후보 수백)를
    실 `cmd_new` 로 돌려 총 재료 줄 수가 9를 넘지 않는지, 오름차순·ID 6개 상한이 지켜지는지
    실 진입점 출력으로 확인한다(고치기 전엔 11줄이 나갔다)."""
    board_dir = board_git._board_dir
    own_touches = [f"tools/f{i}.py" for i in range(12)]
    total_candidates = 0
    for i, path in enumerate(own_touches):
        overlap_n = 20 + i   # 오름차순 검증용 계단형 + 총 후보 수백(20..31 합계=306).
        for _ in range(overlap_n):
            total_candidates += 1
            other_id = f"T-{9000 + total_candidates}"
            other_path = board_dir / "tickets" / "open" / f"{other_id}-x.md"
            board_git.dump_ticket(other_path, {
                "id": other_id, "title": "x", "status": "open",
                "touches": [path],
                "depends_on": [], "blocks": [], "tags": [],
            }, _FILLED_BODY.replace("T-NNNN", other_id))
    assert total_candidates >= 200, "이 회귀의 전제(후보 수백)가 성립하지 않는다."
    _write_ledger(board_git, _lease_row(
        slot="work/A_1", repo=board_git.REPO.name, state="idle"))

    rc = board_git.cmd_new(_new_args(touches=",".join(own_touches)))
    assert rc == 0
    err = capsys.readouterr().err
    assert board_git._PUBLISH_OVERLAP_HEADER in err

    # 재료 블록만 잘라 잰다 — draft 격리 경고 등 무관 stderr 줄은 이 상한의 대상이 아니다.
    all_lines = [ln for ln in err.splitlines() if ln.strip()]
    header_at = all_lines.index(board_git._PUBLISH_OVERLAP_HEADER)
    lines = all_lines[header_at:]
    assert len(lines) <= board_git._PUBLISH_OVERLAP_TOTAL_LINE_BUDGET, f"{len(lines)}줄: {lines!r}"
    assert "idle) 슬롯 1개" in err

    path_lines = [ln for ln in lines if "건 겹침" in ln]
    counts = [int(re.search(r": (\d+)건 겹침", ln).group(1)) for ln in path_lines]
    assert counts == sorted(counts), f"오름차순 정렬 위반: {counts!r}"
    for ln in path_lines:
        shown_ids = re.search(r"\((.*)\)$", ln).group(1)
        shown_ids = re.sub(r" 외 \d+건$", "", shown_ids)
        id_count = len([tok for tok in shown_ids.split(", ") if tok])
        assert id_count <= board_git._PUBLISH_OVERLAP_ID_LIMIT, ln


# ════════════════════════════════════════════════════════════════════════
# 5. board 비-git(legacy) 형상에서도 재료가 나온다
# ════════════════════════════════════════════════════════════════════════

def test_legacy_no_board_git_still_emits_material(tmp_path, monkeypatch, capsys):
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod, "LEASES_FILE", tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    wiki = tmp_path / ".project_manager" / "wiki"
    for status in ("open", "claimed", "blocked", "done"):
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (wiki / "tickets" / "_template.md").write_text(
        (REPO / ".project_manager" / "wiki" / "tickets" / "_template.md")
        .read_text(encoding="utf-8"), encoding="utf-8")
    existing = wiki / "tickets" / "open" / "T-0002-x.md"
    mod.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": [".project_manager/tools/board.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _FILLED_BODY.replace("T-NNNN", "T-0002"))

    rc = mod.cmd_new(_new_args(touches=".project_manager/tools/board.py"))
    assert rc == 0
    err = capsys.readouterr().err
    assert mod._PUBLISH_OVERLAP_HEADER in err
    assert "T-0002" in err
