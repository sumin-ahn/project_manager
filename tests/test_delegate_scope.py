"""위임 범위 밖 변경 감지기 테스트 (T-0462)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from _win_skip import posix_mode_supported


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scope():
    return _load("delegate_scope", TOOLS / "delegate_scope.py")


@pytest.fixture
def delegated_repo(tmp_path: Path) -> tuple[Path, Path]:
    """repo_coordinates가 검증할 실제 PM-home ``work/<repo>_<N>`` 형상."""
    workspace = tmp_path / "work" / "demo_1"
    workspace.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return tmp_path, workspace


def _audit(scope, pm_root: Path, workspace: Path, before, touches, role="developer"):
    after = scope.capture_worktree_state(workspace)
    return scope.out_of_scope_changes(
        before,
        after,
        touches=touches,
        role=role,
        pm_root=pm_root,
        workspace=workspace,
    )


def test_touches_inside_only_has_no_warning(scope, delegated_repo):
    """PM-home 좌표 touch를 T-0473 normalizer로 strip하고 내부 산출물은 허용한다."""
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    target = workspace / "src" / "inside.py"
    target.parent.mkdir()
    target.write_text("inside\n", encoding="utf-8")

    paths = _audit(
        scope,
        pm_root,
        workspace,
        before,
        ["work/demo_1/src"],
    )

    assert paths == ()
    assert scope.format_warning(paths) == ""


def test_new_file_outside_touches_emits_loud_nonblocking_warning(scope, delegated_repo):
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    (workspace / "render-output.html").write_text("stray\n", encoding="utf-8")

    paths = _audit(scope, pm_root, workspace, before, ["src/allowed.py"])
    warning = scope.format_warning(paths)

    assert paths == ("render-output.html",)
    assert "위임 범위 밖 변경" in warning
    assert "render-output.html" in warning
    assert "차단하지 않으며" in warning


def test_untracked_files_are_expanded_and_detected(scope, delegated_repo):
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    target = workspace / "stray-dir" / "untracked.txt"
    target.parent.mkdir()
    target.write_text("untracked\n", encoding="utf-8")

    after = scope.capture_worktree_state(workspace)
    paths = scope.out_of_scope_changes(
        before,
        after,
        touches=["src"],
        role="developer",
        pm_root=pm_root,
        workspace=workspace,
    )

    assert scope.StatusEntry("??", "stray-dir/untracked.txt") in after.entries
    assert paths == ("stray-dir/untracked.txt",)


def test_read_only_role_allows_zero_changes_even_when_touches_exist(scope, delegated_repo):
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    target = workspace / "src" / "review-note.md"
    target.parent.mkdir()
    target.write_text("must not be written\n", encoding="utf-8")

    paths = _audit(
        scope,
        pm_root,
        workspace,
        before,
        ["src"],
        role="code-reviewer",
    )

    assert paths == ("src/review-note.md",)
    assert "위임 범위 밖 변경" in scope.format_warning(paths)


def test_empty_touches_allows_zero_changes(scope, delegated_repo):
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    (workspace / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")

    paths = _audit(scope, pm_root, workspace, before, [])

    assert paths == ("unexpected.txt",)
    assert "unexpected.txt" in scope.format_warning(paths)


# ── 재수정(M→M)·rename·toplevel·항목별 정규화 (리뷰 fix) ────────────────────────

def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True)


def _commit(workspace: Path, relative: str, text: str) -> Path:
    """추적 파일 1개를 만들고 커밋한다(재수정/rename 시나리오의 기준선)."""
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(workspace, "add", relative)
    _git(workspace, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", f"add {relative}")
    return target


def test_already_dirty_file_modified_again_is_detected(scope, delegated_repo):
    """이미 dirty(M) 한 파일을 위임이 **또** 고치면 상태코드는 M→M 그대로 — 내용 해시가 잡는다.

    병렬 공유 트리 clobber(다른 dev 의 WIP 덮어쓰기)가 정확히 이 형상이다."""
    pm_root, workspace = delegated_repo
    target = _commit(workspace, "outside/wip.py", "original\n")
    target.write_text("사람이 만든 WIP\n", encoding="utf-8")     # 위임 전부터 dirty

    before = scope.capture_worktree_state(workspace)
    assert scope.StatusEntry(" M", "outside/wip.py") in before.entries
    target.write_text("위임이 덮어씀\n", encoding="utf-8")        # 위임 중 재수정(코드 불변)

    after = scope.capture_worktree_state(workspace)
    assert {entry.code for entry in after.entries} == {" M"}      # 상태코드는 그대로
    assert dict(before.digests)["outside/wip.py"] != dict(after.digests)["outside/wip.py"]
    assert _audit(scope, pm_root, workspace, before, ["src"]) == ("outside/wip.py",)


def test_dirty_file_untouched_by_delegation_is_not_reported(scope, delegated_repo):
    """해시 비교가 오탐을 만들지 않는다 — 손대지 않은 dirty 파일은 여전히 무경고."""
    pm_root, workspace = delegated_repo
    target = _commit(workspace, "outside/wip.py", "original\n")
    target.write_text("사람이 만든 WIP\n", encoding="utf-8")

    before = scope.capture_worktree_state(workspace)
    assert _audit(scope, pm_root, workspace, before, ["src"]) == ()


def test_rename_reports_both_paths(scope, delegated_repo):
    """rename 은 결과 경로와 **원본 경로**를 함께 싣는다 — 범위 밖 원본이 사라진 사실을 숨기지 않는다."""
    pm_root, workspace = delegated_repo
    _commit(workspace, "outside/origin.py", "payload\n")
    (workspace / "src").mkdir()
    before = scope.capture_worktree_state(workspace)
    _git(workspace, "mv", "outside/origin.py", "src/moved.py")

    after = scope.capture_worktree_state(workspace)
    codes = {entry.path: entry.code for entry in after.entries}
    assert "src/moved.py" in codes and "outside/origin.py" in codes
    assert codes["src/moved.py"].startswith("R")           # 두-토큰 rename 브랜치를 실제로 탄다
    assert _audit(scope, pm_root, workspace, before, ["src"]) == ("outside/origin.py",)


def test_rename_two_token_consumption_has_no_phantom_entry(scope):
    """두-토큰 소비를 지우면 old 경로가 **상태 라인처럼** 재파싱돼 유령 엔트리가 생긴다(회귀 가드).

    구분력을 위해 old 경로의 3번째 문자가 공백인 형상을 쓴다 — 그때 `<XY> <path>` 파싱 가드를
    통과해 ('ol', 'd/old.py') 같은 유령이 실제로 만들어진다(일반 경로면 가드에 걸려 조용히 넘어가
    변이가 안 잡힌다)."""
    entries = scope.parse_porcelain_z("R  src/new.py\0ol d/old.py\0?? stray.txt\0")
    assert entries == (
        scope.StatusEntry("R ", "src/new.py"),
        scope.StatusEntry("R ", "ol d/old.py"),
        scope.StatusEntry("??", "stray.txt"),
    )


def test_covered_does_not_match_sibling_prefix(scope, delegated_repo):
    """`src` 허용이 `src_stray.py`/`srcbak/` 까지 덮으면 안 된다(경계는 세그먼트 단위)."""
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    (workspace / "src").mkdir()
    (workspace / "src" / "ok.py").write_text("in\n", encoding="utf-8")
    (workspace / "src_stray.py").write_text("out\n", encoding="utf-8")
    (workspace / "srcbak").mkdir()
    (workspace / "srcbak" / "copy.py").write_text("out\n", encoding="utf-8")

    assert _audit(scope, pm_root, workspace, before, ["src"]) == (
        "src_stray.py", "srcbak/copy.py",
    )


def test_resolve_workspace_root_from_subdirectory(scope, delegated_repo):
    """--cwd 가 repo 하위 디렉토리여도 판정 기준은 git toplevel."""
    _pm_root, workspace = delegated_repo
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    assert scope.resolve_workspace_root(nested) == workspace.resolve()


def test_resolve_workspace_root_outside_repo_is_loud(scope, tmp_path):
    """repo 밖이면 조용히 통과시키지 않고 DelegateScopeError(호출부가 loud degrade).

    "이 자리는 어느 checkout 도 아니다"가 입력이다 — 픽스처 위치가 그 답을 정하지 않도록
    이 함수가 이미 가진 `run_git` 주입 seam 으로 비-repo(rc 128)를 명시한다.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    with pytest.raises(scope.DelegateScopeError):
        scope.resolve_workspace_root(
            outside, run_git=lambda _cwd, _args: (128, ""))


def test_subdirectory_capture_uses_repo_relative_paths(scope, delegated_repo):
    """toplevel 기준 캡처라 하위 디렉토리 위임에서도 경로가 repo-relative 로 맞는다."""
    pm_root, workspace = delegated_repo
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    root = scope.resolve_workspace_root(nested)
    before = scope.capture_worktree_state(root)
    (workspace / "stray.txt").write_text("out\n", encoding="utf-8")

    assert _audit(scope, pm_root, root, before, ["work/demo_1/src"]) == ("stray.txt",)


def test_unresolvable_touch_item_is_dropped_not_fatal(scope, delegated_repo):
    """타 슬롯 항목 하나가 리스트 전체를 죽이지 않는다 — 항목별 정규화 + 드롭 통보."""
    pm_root, workspace = delegated_repo
    dropped: list[tuple[str, str]] = []
    allowed = scope.allowed_paths(
        ["work/other_2/src", "work/demo_1/src"],
        role="developer",
        pm_root=pm_root,
        workspace=workspace,
        on_drop=lambda item, reason: dropped.append((item, reason)),
    )

    assert allowed == ("src",)                      # 이 workspace 항목은 살아남는다
    assert [item for item, _ in dropped] == ["work/other_2/src"]


def test_all_touch_items_dropped_means_zero_allowed(scope, delegated_repo):
    """전부 드롭되면 허용 0(보수 방향) — 조용한 전체 허용으로 뒤집히지 않는다."""
    pm_root, workspace = delegated_repo
    before = scope.capture_worktree_state(workspace)
    (workspace / "anything.txt").write_text("x\n", encoding="utf-8")

    dropped: list[str] = []
    after = scope.capture_worktree_state(workspace)
    paths = scope.out_of_scope_changes(
        before, after,
        touches=["work/other_2/src"],
        role="developer",
        pm_root=pm_root,
        workspace=workspace,
        on_drop=lambda item, reason: dropped.append(item),
    )

    assert paths == ("anything.txt",) and dropped == ["work/other_2/src"]


def test_write_roles_injection_overrides_default_set(scope, delegated_repo):
    """쓰기 역할집합은 호출부 주입이 단일 출처 — 주입 밖 역할은 허용 0(안전 방향)."""
    pm_root, workspace = delegated_repo
    common = dict(pm_root=pm_root, workspace=workspace, touches=["work/demo_1/src"])
    assert scope.allowed_paths(role="developer", **common) == ("src",)
    # 주입 집합에서 빠지면 touches 가 있어도 허용 0
    assert scope.allowed_paths(role="developer", write_roles={"architect"}, **common) == ()
    # 미지 역할은 기본 집합에서도 읽기 전용 취급(신규 역할 = 안전 방향)
    assert scope.allowed_paths(role="future-role", **common) == ()


def test_warning_notes_gitignored_and_concurrent_edits(scope):
    """경고 블록이 판정 사각(gitignore 제외·동시 편집 혼입·repo 경계)을 함께 알린다."""
    warning = scope.format_warning(["stray.txt"])
    assert "gitignored" in warning and "판정 대상이 아닙니다" in warning
    assert "다른 터미널" in warning
    assert "중첩 repo" in warning


# ── 커밋·mode/submodule·ticket 조회 (codex R2 fix) ────────────────────────────

def test_committed_out_of_scope_change_is_detected(scope, delegated_repo):
    """범위 밖 파일을 고치고 **커밋**하면 전후 worktree 가 둘 다 clean — HEAD 비교가 유일한 신호."""
    pm_root, workspace = delegated_repo
    _commit(workspace, "outside/committed.py", "original\n")
    before = scope.capture_worktree_state(workspace)
    assert before.head and not before.entries               # 위임 전 clean

    (workspace / "outside" / "committed.py").write_text("위임이 고침\n", encoding="utf-8")
    _git(workspace, "add", "outside/committed.py")
    _git(workspace, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "delegate commit")

    after = scope.capture_worktree_state(workspace)
    assert not after.entries                               # 상태 비교로는 흔적 0
    assert scope.head_moved(before, after)
    assert scope.committed_paths(before, after, workspace=workspace) == ("outside/committed.py",)
    assert _audit(scope, pm_root, workspace, before, ["src"]) == ("outside/committed.py",)


def test_committed_in_scope_change_is_not_reported(scope, delegated_repo):
    """커밋 합산도 touches 필터를 그대로 탄다 — 범위 안 커밋은 목록에 안 오른다(경고는 별도)."""
    pm_root, workspace = delegated_repo
    _commit(workspace, "src/impl.py", "original\n")
    before = scope.capture_worktree_state(workspace)
    (workspace / "src" / "impl.py").write_text("범위 안 수정\n", encoding="utf-8")
    _git(workspace, "add", "src/impl.py")
    _git(workspace, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "in scope")

    after = scope.capture_worktree_state(workspace)
    assert scope.head_moved(before, after)                  # 커밋 사실 자체는 신호로 남는다
    assert _audit(scope, pm_root, workspace, before, ["work/demo_1/src"]) == ()


def test_head_unmoved_skips_commit_diff(scope, delegated_repo):
    """HEAD 가 그대로면 커밋 판정은 아예 돌지 않는다(불필요한 git 호출 0)."""
    _pm_root, workspace = delegated_repo
    _commit(workspace, "src/impl.py", "x\n")
    before = scope.capture_worktree_state(workspace)
    after = scope.capture_worktree_state(workspace)

    calls: list[list[str]] = []

    def _spy(cwd, args):
        calls.append(args)
        return 0, ""

    assert scope.head_moved(before, after) is False
    assert scope.committed_paths(before, after, workspace=workspace, run_git=_spy) == ()
    assert calls == []


@pytest.mark.skipif(
    not posix_mode_supported(), reason="chmod 실행 비트 왕복을 지원하지 않는 filesystem"
)
def test_mode_change_on_already_dirty_file_is_detected(scope, delegated_repo):
    """chmod(+x)는 내용이 그대로라 상태코드도 해시도 안 움직인다 — mode 지문이 유일한 신호."""
    pm_root, workspace = delegated_repo
    target = _commit(workspace, "outside/tool.sh", "#!/bin/sh\n")
    target.write_text("#!/bin/sh\necho wip\n", encoding="utf-8")   # 위임 전부터 dirty

    before = scope.capture_worktree_state(workspace)
    target.chmod(0o755)                                            # 위임이 실행권한만 바꿈

    after = scope.capture_worktree_state(workspace)
    assert {entry.code for entry in after.entries} == {" M"}       # 상태코드 동일
    assert dict(before.digests)["outside/tool.sh"] == dict(after.digests)["outside/tool.sh"]
    assert dict(before.modes)["outside/tool.sh"] != dict(after.modes)["outside/tool.sh"]
    assert _audit(scope, pm_root, workspace, before, ["src"]) == ("outside/tool.sh",)


def test_porcelain_v2_mode_parsing_shapes(scope):
    """v2 레코드 파싱 — ordinary/rename(원본 토큰 소비)/untracked 혼재."""
    output = (
        "1 .M N... 100644 100644 100755 aaa bbb outside/tool.sh\0"
        "2 R. N... 100644 100644 100644 ccc ddd R100 src/new.py\0outside/old.py\0"
        "? stray.txt\0"
    )
    parsed = dict(scope.parse_porcelain_v2_modes(output))
    assert parsed["outside/tool.sh"] == "N...:100755"
    assert parsed["src/new.py"] == "N...:100644"
    assert "stray.txt" not in parsed                     # untracked 는 mode 정보 없음
    assert "outside/old.py" not in parsed                # rename 원본 토큰은 소비만(유령 없음)


def test_ticket_id_with_glob_metacharacter_is_rejected(scope, tmp_path):
    """`T-*` 같은 입력이 다른 ticket 의 touches 를 집어오지 않게 조회 전에 거부한다."""
    board_py = TOOLS / "board.py"
    for bad in ("T-*", "T-04?4", "T-[01]474", "../T-0001", "T-0474/x"):
        with pytest.raises(scope.DelegateScopeError, match="형식 거부"):
            scope.ticket_touches(board_py, bad, pm_root=tmp_path)


def test_ticket_id_mismatch_in_frontmatter_is_loud(scope, tmp_path):
    """파일명으로 찾았어도 frontmatter id 가 다르면 정확 일치 실패로 거부한다."""
    tickets = tmp_path / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-0474-x.md").write_text(
        "---\nid: T-0001\ntitle: 다른 티켓\nstatus: open\ntouches:\n- src\n---\n\n# 본문\n",
        encoding="utf-8",
    )
    with pytest.raises(scope.DelegateScopeError, match="id 불일치"):
        scope.ticket_touches(TOOLS / "board.py", "T-0474", pm_root=tmp_path)


def test_ticket_touches_exact_match_reads_own_frontmatter(scope, tmp_path):
    """정상 경로 — 정확히 일치하는 ticket 의 touches 만 읽는다(음성 통제)."""
    tickets = tmp_path / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    for tid, touch in (("T-0474", "src/mine"), ("T-0475", "src/other")):
        (tickets / f"{tid}-t.md").write_text(
            f"---\nid: {tid}\ntitle: t\nstatus: open\ntouches:\n- {touch}\n---\n\n# 본문\n",
            encoding="utf-8",
        )
    assert scope.ticket_touches(TOOLS / "board.py", "T-0474", pm_root=tmp_path) == ("src/mine",)


def test_large_untracked_file_is_not_hashed(scope, delegated_repo, monkeypatch):
    """대용량 파일은 해시 보강을 건너뛴다(존재/상태코드로 이미 보임·최악 I/O 폐쇄)."""
    _pm_root, workspace = delegated_repo
    monkeypatch.setattr(scope, "HASH_MAX_FILE_BYTES", 16)
    (workspace / "small.txt").write_text("tiny\n", encoding="utf-8")
    (workspace / "big.bin").write_text("x" * 64, encoding="utf-8")

    state = scope.capture_worktree_state(workspace)
    digests = dict(state.digests)
    assert "small.txt" in digests and "big.bin" not in digests
    assert {entry.path for entry in state.entries} == {"small.txt", "big.bin"}   # 표면화는 유지


def test_content_signal_missing_flags_total_hash_loss(scope, delegated_repo, monkeypatch):
    """해시 대상이 있는데 지문이 0이면 강등 사실을 호출부가 알 수 있어야 한다."""
    _pm_root, workspace = delegated_repo
    (workspace / "dirty.txt").write_text("x\n", encoding="utf-8")

    healthy = scope.capture_worktree_state(workspace)
    assert healthy.digests and scope.content_signal_missing(healthy, workspace) is False

    monkeypatch.setattr(scope, "_hash_object", lambda *a, **k: None)   # 해시 전량 실패 형상
    degraded = scope.capture_worktree_state(workspace)
    assert degraded.digests == ()
    assert scope.content_signal_missing(degraded, workspace) is True
