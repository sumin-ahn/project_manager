"""외부 리뷰어 가시 범위 격리 + echo 오염 검출 (T-0563).

codex 게이트 실행이 저장소 밖 PM 로컬 산출물을 탐색해 판정 본문에 echo 하는 오염이 실측됐다
(T-0544 raw: 같은 세션 내부 reviewer 보고 verbatim + 옛 리뷰 raw 재인용). 리뷰어 CLI 로는 읽기
범위를 못 좁히므로(`codex exec` 에 read 스코프 옵션 없음 — 엔진 주석에 실측 기각 근거 박제) 두 축을
기계로 세운다:

1. **가시 범위 격리** — 리뷰어 프로세스가 PM 세션 cwd/env 대신 저장소 밖 tracked 파일 거울과
   세션 포인터가 빠진 env 를 받는다. 불변식은 하나다: *격리 루트의 조상에 `.project_manager` 가
   없다*(PM 홈 분리 형상 + standalone 채택자 형상을 한 검사로 닫는다).
2. **echo 검출 백스톱** — 판정 본문에 남은 raw 파일명/전사 경로/엇갈린 판정 라인을 loud 진단으로
   올리고, 엇갈린 판정은 '판정 불명확'(보수적 exit 1)으로 강등해 오염된 통과를 막는다.

hermetic: 실 리뷰어 프로세스를 스폰하지 않는다(외부 전송 0). 거울은 tmp git 저장소에서 만들고
run_fn/`run_review` 주입으로 seam 만 단언한다.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".project_manager" / "tools"


def _load(name: str = "external_review"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load("external_review")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def _git_out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _jail(tmp_path: Path) -> Path:
    """격리 루트를 만들 저장소 밖 base 디렉터리(조상에 PM 인스턴스 없음)."""
    base = tmp_path / "jail"
    base.mkdir(exist_ok=True)
    return base


def _standalone_adopter(tmp_path: Path) -> Path:
    """`pm_home == diff_root == repo` 인 standalone 채택자 형상 — 옛 raw 가 저장소 안에 쌓인다."""
    repo = tmp_path / "adopter"
    (repo / ".project_manager" / "tools").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / ".project_manager" / ".gitignore").write_text(".local/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("adopter\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")

    # 옛 리뷰 raw(git-ignored) + PM 세션 전사 흉내 — 격리 대상 두 축.
    raw_dir = repo / ".project_manager" / ".local" / "review"
    raw_dir.mkdir(parents=True)
    (raw_dir / "external_review_codex_20260806_040406_11_ab.txt").write_text(
        "판정: 반려\n\n**must-fix**:\n- 옛 라운드 지적\n", encoding="utf-8",
    )
    (repo / "src" / "scratch_untracked.py").write_text("secret_note = 1\n", encoding="utf-8")
    return repo


# ── 거울: 무엇이 실리고 무엇이 안 실리나 ────────────────────────────────────


def test_workspace_mirrors_worktree_content_without_local_artifacts(external, tmp_path):
    """tracked 파일은 **작업 트리 내용**으로 실리고, git-ignored 옛 raw·untracked 잔재는 안 실린다."""
    repo = _standalone_adopter(tmp_path)
    (repo / "src" / "app.py").write_text("value = 2  # unstaged\n", encoding="utf-8")

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        mirrored = workspace.tree / "src" / "app.py"
        # 언스테이징 수정도 프롬프트 diff 에 실리므로 거울이 그것과 어긋나면 안 된다.
        assert mirrored.read_text(encoding="utf-8") == "value = 2  # unstaged\n"
        assert (workspace.tree / "README.md").exists()
        assert not (workspace.tree / ".project_manager" / ".local").exists()
        assert not (workspace.tree / "src" / "scratch_untracked.py").exists()
        assert workspace.files >= 3
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_workspace_hides_old_review_raw_from_reviewer_visible_tree(external, tmp_path):
    """DoD 축: 거울 어디에도 옛 리뷰 raw 본문/파일명이 없다(standalone 채택자 형상 포함)."""
    repo = _standalone_adopter(tmp_path)
    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        visible = [
            path for path in workspace.tree.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(workspace.tree).parts
        ]
        assert visible, "거울이 비어 있으면 격리가 아니라 검토 불능이다"
        assert not any("external_review_" in path.name for path in visible)
        assert not any("옛 라운드 지적" in path.read_text(encoding="utf-8", errors="replace")
                       for path in visible)
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_workspace_ancestors_have_no_project_manager_instance(external, tmp_path):
    """격리 불변식: 작업 루트의 어느 조상에도 PM 인스턴스가 없다(상대 walk 로 raw 도달 불가)."""
    repo = _standalone_adopter(tmp_path)
    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert external._project_manager_ancestor(workspace.tree.resolve()) is None
        assert not workspace.root.resolve().is_relative_to(repo.resolve())
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_workspace_refuses_base_dir_inside_pm_instance(external, tmp_path):
    """TMPDIR 이 PM 인스턴스 안이면 격리가 성립하지 않는다 — fail-loud + 잔재 없음."""
    repo = _standalone_adopter(tmp_path)
    base = repo / "tmpjail"
    base.mkdir()

    with pytest.raises(external.ReviewerWorkspaceError) as excinfo:
        external.create_reviewer_workspace(repo, base_dir=base)
    assert "PM 인스턴스" in str(excinfo.value)
    assert list(base.iterdir()) == []


def test_workspace_is_self_contained_git_repo_without_origin_pointer(external, tmp_path):
    """거울은 자족 git 저장소다 — 원본을 가리키는 gitdir 포인터가 없어야 격리가 유지된다.

    `gate_snapshot` 의 linked worktree 를 그대로 쓰지 않은 이유의 회귀다(`.git` 파일 안 절대
    gitdir 로 원본 트리·상위 PM 홈에 되돌아갈 수 있다).
    """
    repo = _standalone_adopter(tmp_path)
    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert workspace.git_repo is True
        assert (workspace.tree / ".git").is_dir()
        assert Path(_git_out(workspace.tree, "rev-parse", "--show-toplevel")).resolve() \
            == workspace.tree.resolve()
        assert str(repo.resolve()) not in (workspace.tree / ".git" / "config").read_text(
            encoding="utf-8")
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="symlink 생성 권한이 플랫폼-bound")
def test_workspace_skips_symlinks_that_bridge_out_of_the_mirror(external, tmp_path):
    """저장소 밖/절대 symlink 는 격리 우회 다리 — 복제하지 않고 개수만 남긴다."""
    repo = _standalone_adopter(tmp_path)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("바깥 파일\n", encoding="utf-8")
    os.symlink(outside, repo / "bridge_out.txt")
    os.symlink("src/app.py", repo / "inside_link.py")
    _git(repo, "add", "-A")

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert workspace.skipped_unsafe == 1
        assert not (workspace.tree / "bridge_out.txt").exists()
        assert (workspace.tree / "inside_link.py").is_symlink()
        assert os.readlink(workspace.tree / "inside_link.py") == "src/app.py"
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="symlink 생성 권한이 플랫폼-bound")
def test_workspace_refuses_files_reached_through_out_of_repo_parent_symlink(
        external, tmp_path):
    """최종 경로만 보면 뚫린다 — tracked `dir/file` 의 **부모**가 저장소 밖 symlink 인 경우.

    lstat 은 정상 파일을 보고하므로 경로 구성요소 전체를 해소한 realpath 로만 막힌다.
    """
    repo = _standalone_adopter(tmp_path)
    (repo / "vendor").mkdir()
    (repo / "vendor" / "note.txt").write_text("in-repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "vendor")

    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (outside / "note.txt").write_text("바깥 비밀\n", encoding="utf-8")
    shutil_rmtree = external.shutil.rmtree
    shutil_rmtree(repo / "vendor")
    os.symlink(outside, repo / "vendor")  # 부모 디렉터리만 저장소 밖으로 바꿔치기

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert workspace.skipped_unsafe == 1
        assert not (workspace.tree / "vendor" / "note.txt").exists()
        mirrored = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in workspace.tree.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(workspace.tree).parts
        ]
        assert not any("바깥 비밀" in text for text in mirrored)
    finally:
        shutil_rmtree(workspace.root, ignore_errors=True)


def test_mirror_excludes_secret_denylist_files(external, tmp_path):
    """프롬프트에서 빼는 파일을 거울에 두면 리뷰어가 그냥 열어 읽는다 — 같은 폭으로 제외한다."""
    repo = _standalone_adopter(tmp_path)
    (repo / ".env").write_text("API_TOKEN=live-secret\n", encoding="utf-8")
    (repo / "deploy.key").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (repo / "conf").mkdir()
    (repo / "conf" / "vendor_credentials.json").write_text('{"pw": 1}\n', encoding="utf-8")
    _git(repo, "add", "-f", ".env", "deploy.key", "conf/vendor_credentials.json")
    _git(repo, "commit", "-qm", "secrets")

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert workspace.skipped_secret == 3
        for excluded in (".env", "deploy.key", "conf/vendor_credentials.json"):
            assert not (workspace.tree / excluded).exists()
        assert (workspace.tree / "src" / "app.py").exists()   # 검토 대상은 그대로.
        leaked = [
            path for path in workspace.tree.rglob("*")
            if path.is_file() and "live-secret" in path.read_text(
                encoding="utf-8", errors="replace")
        ]
        assert not leaked
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_mirror_denylist_follows_the_resolved_width(external, tmp_path):
    """배포가 승계한 denylist(local.conf 확장)도 거울에 그대로 적용된다."""
    repo = _standalone_adopter(tmp_path)
    (repo / "vendor_dump.sql").write_text("고객 데이터\n", encoding="utf-8")
    _git(repo, "add", "-f", "vendor_dump.sql")
    _git(repo, "commit", "-qm", "dump")

    workspace = external.create_reviewer_workspace(
        repo, base_dir=_jail(tmp_path),
        denylist=external._SECRET_DENYLIST_PATTERNS + ("*dump*",),
    )
    try:
        assert workspace.skipped_secret == 1
        assert not (workspace.tree / "vendor_dump.sql").exists()
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_mirror_refuses_tracked_local_review_artifacts(external, tmp_path):
    """`.project_manager/.local/**` 은 추적돼 있어도 거울에 싣지 않는다(ignore 규칙 밖 두 번째 자물쇠)."""
    repo = _standalone_adopter(tmp_path)
    raw = repo / ".project_manager" / ".local" / "review" / "old_raw.txt"
    raw.write_text("판정: 반려\n- 옛 지적\n", encoding="utf-8")
    _git(repo, "add", "-f", str(raw.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked local raw")

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert not (workspace.tree / ".project_manager" / ".local").exists()
        assert workspace.skipped_unsafe == 1
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO 생성이 플랫폼-bound")
def test_mirror_skips_non_regular_files(external, tmp_path):
    """FIFO/디바이스 노드를 복사하면 열기에서 블록된다 — 정규 파일과 링크만 복제한다."""
    repo = _standalone_adopter(tmp_path)
    placeholder = repo / "pipe.sock"
    placeholder.write_text("placeholder\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "placeholder")
    placeholder.unlink()
    os.mkfifo(placeholder)

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert not (workspace.tree / "pipe.sock").exists()
        assert workspace.skipped_unsafe == 1
        assert (workspace.tree / "src" / "app.py").exists()
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_workspace_errors_are_normalized_for_the_recovery_channel(external, tmp_path, monkeypatch):
    """복사/git 의 OSError 가 그대로 새면 `--allow-unisolated-reviewer` 복구 경로가 안 탄다."""
    repo = _standalone_adopter(tmp_path)

    def _boom(*args, **kwargs):
        raise PermissionError(13, "권한 없음")

    monkeypatch.setattr(external, "_mirror_tracked_files", _boom)
    with pytest.raises(external.ReviewerWorkspaceError) as excinfo:
        external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    assert "권한 없음" in str(excinfo.value)
    assert list(_jail(tmp_path).iterdir()) == []   # 실패 잔재도 남기지 않는다.


# ── 임시 홈: 세션·이력 없는 홈 + 선언된 인증/설정만 ────────────────────────


def _user_home_with_history(tmp_path: Path) -> Path:
    """실 사용자 홈 형상 — 인증/설정과 **세션 전사·이력**이 같은 트리에 있다(실측 배치)."""
    home = tmp_path / "user-home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".claude" / "projects" / "-repo").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token": "t"}\n', encoding="utf-8")
    # 실물 폭: 경로를 담는 기능 테이블 전량(hooks·mcp_servers·plugins·projects) + 보존 대상.
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-x"\n'
        'model_reasoning_effort = "max"\n'
        f'[projects."{home}/workspace/secret-repo"]\n'
        'trust_level = "trusted"\n'
        '[hooks]\n'
        f'pre_tool_use = "{home}/.codex/hooks/audit.sh"\n'
        '[mcp_servers.local]\n'
        f'command = "{home}/.local/bin/mcp-server"\n'
        '[plugins.vendor]\n'
        f'path = "{home}/.codex/plugins/vendor"\n'
        '[tui]\n'
        'status_line = ["model"]\n',
        encoding="utf-8")
    (home / ".codex" / "history.jsonl").write_text('{"turn": "옛 대화"}\n', encoding="utf-8")
    (home / ".codex" / "sessions" / "s1.jsonl").write_text('{"old": "세션"}\n', encoding="utf-8")
    (home / ".claude" / "projects" / "-repo" / "s2.jsonl").write_text(
        '{"transcript": "내부 reviewer 보고 전문"}\n', encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text('{"cred": 1}\n', encoding="utf-8")
    # 온보딩/신뢰 상태와 **세션 흔적**이 한 파일에 섞여 있는 실측 형상.
    (home / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True,
        "oauthAccount": {"emailAddress": "user@example.invalid"},
        "projects": {
            f"{home}/workspace/secret-repo": {
                "hasTrustDialogAccepted": True,
                "lastSessionId": "9f2e-transcript-id",
                "lastSessionFirstPrompt": "내부 reviewer 보고를 검토하라",
            },
        },
        "githubRepoPaths": {f"{home}/workspace/secret-repo": "org/secret-repo"},
    }, ensure_ascii=False), encoding="utf-8")
    (home / ".config" / "opencode" / "opencode.jsonc").write_text(
        '{"model": "ollama/glm"}\n', encoding="utf-8")
    (home / ".local" / "share" / "opencode" / "auth.json").write_text(
        '{"opencode": "auth"}\n', encoding="utf-8")
    # 자격증명과 세션 이력이 함께 든 DB — 선언표에 없으므로 복제되면 안 된다.
    (home / ".local" / "share" / "opencode" / "opencode.db").write_text(
        "SQLite format 3\x00 옛 세션", encoding="utf-8")
    return home


def test_reviewer_home_carries_auth_but_no_sessions(external, tmp_path):
    """리뷰어 홈은 전사/이력의 부모가 되면 안 된다 — 선언된 인증/설정 파일만 복제한다."""
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), _user_home_with_history(tmp_path),
    ).copied
    assert set(copied) == {
        ".codex/auth.json", ".codex/config.toml",
        ".claude/.credentials.json", ".claude.json",
        ".local/share/opencode/auth.json", ".config/opencode/opencode.jsonc",
    }
    # 자격증명이 세션 이력과 한 파일에 든 저장소(opencode.db)는 선언에 없어 복제되지 않는다.
    assert not (home / ".local" / "share" / "opencode" / "opencode.db").exists()
    assert (home / ".codex" / "auth.json").read_text(encoding="utf-8") == '{"token": "t"}\n'
    assert not (home / ".codex" / "sessions").exists()
    assert not (home / ".codex" / "history.jsonl").exists()
    assert not (home / ".claude" / "projects").exists()
    survivors = {
        path.relative_to(home).as_posix()
        for path in home.rglob("*") if path.is_file()
    }
    assert survivors == set(copied)


def test_reviewer_home_scrubs_session_traces_from_onboarding_state(external, tmp_path):
    """온보딩/신뢰 상태 파일에 세션 흔적이 섞여 있으면 그 키만 떼고 복제한다.

    `~/.claude.json` 실측: 최상위는 온보딩 상태지만 `projects` 하위에 원본 저장소 경로와
    전사 id·사용자 프롬프트 원문이 들어 있다 — 통째로 복제하면 이 티켓이 닫은 채널이 다시 열린다.
    """
    home = tmp_path / "reviewer-home"
    external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), _user_home_with_history(tmp_path),
    )
    copied = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert copied["hasCompletedOnboarding"] is True          # 온보딩 상태는 남는다.
    assert copied["oauthAccount"]["emailAddress"]            # 계정 연속성도 남는다.
    assert "projects" not in copied                          # 세션 흔적은 사라진다.
    body = (home / ".claude.json").read_text(encoding="utf-8")
    assert "secret-repo" not in body and "9f2e-transcript-id" not in body
    assert "내부 reviewer 보고를 검토하라" not in body


def test_reviewer_home_scrubs_repo_paths_from_codex_config(external, tmp_path):
    """`[projects."<절대경로>"]` 를 그대로 복제하면 원본 위치를 리뷰어에게 다시 알려준다."""
    source = _user_home_with_history(tmp_path)
    (source / ".codex" / "config.toml").write_text(
        'model = "gpt-x"\n'
        'model_reasoning_effort = "max"\n'
        '[projects."/home/user/workspace/secret-repo"]\n'
        'trust_level = "trusted"\n'
        '[tui]\n'
        'status_line = ["model"]\n',
        encoding="utf-8",
    )
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), source, env={},
    ).copied
    assert ".codex/config.toml" in copied
    body = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "secret-repo" not in body and "[projects" not in body
    assert 'model = "gpt-x"' in body and "[tui]" in body   # 모델/설정은 보존.


def test_reviewer_home_skips_unscrubbable_codex_config(external, tmp_path):
    """줄 절단으로 못 빼는 인라인 선언은 복제 자체를 취소한다(검증 실패 = 미복제)."""
    source = _user_home_with_history(tmp_path)
    (source / ".codex" / "config.toml").write_text(
        'projects = { "/home/user/secret-repo" = { trust_level = "trusted" } }\n',
        encoding="utf-8",
    )
    home = tmp_path / "reviewer-home"
    build = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), source, env={},
    )
    copied = build.copied
    assert ".codex/config.toml" not in copied
    assert ".codex/config.toml" in build.scrub_failed   # 부재가 아니라 '정화 실패'다.
    assert not (home / ".codex" / "config.toml").exists()


def test_reviewer_home_source_follows_existing_env_anchors(external, tmp_path):
    """사용자가 `CODEX_HOME` 등으로 다른 경로를 쓰면 홈만 봐서는 인증을 못 찾는다."""
    custom = tmp_path / "custom-codex"
    custom.mkdir()
    (custom / "auth.json").write_text('{"token": "custom"}\n', encoding="utf-8")
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, (".codex/auth.json",), _user_home_with_history(tmp_path),
        env={"CODEX_HOME": str(custom)},
    ).copied
    assert copied == (".codex/auth.json",)
    assert (home / ".codex" / "auth.json").read_text(encoding="utf-8") == '{"token": "custom"}\n'


def test_reviewer_home_falls_back_to_home_without_env_anchor(external, tmp_path):
    """앵커가 없으면 종전대로 홈 상대경로를 쓴다(회귀 없음)."""
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, (".codex/auth.json",), _user_home_with_history(tmp_path), env={},
    ).copied
    assert copied == (".codex/auth.json",)
    assert (home / ".codex" / "auth.json").read_text(encoding="utf-8") == '{"token": "t"}\n'


def test_no_isolated_absolute_path_survives_in_any_home_artifact(external, tmp_path):
    """**성질 회귀**: 복제된 전 아티팩트에 실 홈 루트·검토 저장소 절대경로 출현 0.

    키 열거로만 막으면 도구가 판올림하며 새로 생기는 경로 키에서 조용히 다시 열린다. 이 단언은
    "무엇을 지웠나"가 아니라 "무엇이 남았나"를 보므로 새 키에도 자동으로 red 다.
    """
    repo = _standalone_adopter(tmp_path)
    source_home = _user_home_with_history(tmp_path)
    workspace = external.create_reviewer_workspace(
        repo, base_dir=_jail(tmp_path), source_home=source_home,
    )
    try:
        artifacts = [path for path in workspace.home.rglob("*") if path.is_file()]
        assert artifacts, "복제분이 없으면 이 성질은 공허하다"
        for path in artifacts:
            body = path.read_text(encoding="utf-8", errors="replace")
            assert str(source_home) not in body, f"실 홈 경로 잔존: {path.name}"
            assert str(repo.resolve()) not in body, f"검토 저장소 경로 잔존: {path.name}"
        config = (workspace.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'model = "gpt-x"' in config and "[tui]" in config      # 보존 축
        for dropped in ("[projects", "[hooks]", "[mcp_servers", "[plugins"):
            assert dropped not in config                              # 중화 축
        # 성질만 보면 "그냥 다 안 복제"도 통과한다 — 선언표가 실제로 지워서 통과했음을 못박는다.
        assert workspace.home_scrub_failed == ()
        onboarding = json.loads(
            (workspace.home / ".claude.json").read_text(encoding="utf-8"))
        assert onboarding["hasCompletedOnboarding"] is True
        assert "githubRepoPaths" not in onboarding and "projects" not in onboarding
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_unknown_path_bearing_key_is_treated_as_scrub_failure(external, tmp_path):
    """선언표가 모르는 새 경로 키가 생기면 조용히 복제되지 않고 '정화 실패'로 선다."""
    source = _user_home_with_history(tmp_path)
    (source / ".codex" / "config.toml").write_text(
        'model = "gpt-x"\n'
        '[future_feature]\n'
        f'workspace = "{source}/workspace/secret-repo"\n',
        encoding="utf-8")
    home = tmp_path / "reviewer-home"
    build = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), source, env={},
        forbidden_paths=(str(source),),
    )
    assert ".codex/config.toml" in build.scrub_failed
    assert not (home / ".codex" / "config.toml").exists()


def test_workspace_wires_forbidden_paths_into_the_home_build(external, tmp_path):
    """성질 자물쇠는 **배선**이 있어야 산다 — 미선언 경로 키가 실제 워크스페이스 생성에서 걸리나."""
    repo = _standalone_adopter(tmp_path)
    source_home = _user_home_with_history(tmp_path)
    (source_home / ".codex" / "config.toml").write_text(
        'model = "gpt-x"\n'
        '[future_feature]\n'
        f'workspace = "{repo.resolve()}"\n',
        encoding="utf-8")
    workspace = external.create_reviewer_workspace(
        repo, base_dir=_jail(tmp_path), source_home=source_home,
    )
    try:
        assert ".codex/config.toml" in workspace.home_scrub_failed
        assert not (workspace.home / ".codex" / "config.toml").exists()
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_scrub_failure_is_distinguished_from_absence(external, monkeypatch, tmp_path, capsys):
    """'부재'와 '정화 실패(다른 모델로 동작 가능)'는 화면에서 구분돼야 한다."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config",
                        lambda repo=None: {"additional_reviewer_enabled": "true"})
    monkeypatch.setattr(external, "_build_reviewer_home",
                        lambda *a, **k: external.ReviewerHomeBuild(
                            (".codex/auth.json",), (".codex/config.toml",)))
    monkeypatch.setattr(external, "run_review", lambda **kwargs: {
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "contamination": (), "unisolated": False, "file": None, "failed": False,
        "started": True, "any_must_fix": False, "all_pass": True})

    assert external.main(["--paths", str(repo / "src"), "--no-gate",
                          "--output-dir", str(tmp_path / "raw")]) == 0
    err = capsys.readouterr().err
    assert "홈 정화 실패 1개 미복제(.codex/config.toml)" in err
    assert "다른 모델" in err


def test_main_passes_resolved_denylist_and_conf_to_isolation(external, monkeypatch, tmp_path):
    """거울/프롬프트 비대칭과 keep_extra 탈출구 사망을 막는 두 배선을 캡처로 고정한다."""
    repo = _standalone_adopter(tmp_path)
    conf = {
        "additional_reviewer_enabled": "true",
        "review_denylist_extra": "*vendor_dump*",
        "reviewer_env_keep_extra": "VENDOR_REVIEW_KEY",
    }
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(conf))
    seen: dict[str, object] = {}
    real_scope = external.reviewer_visibility_scope

    def _capture(diff_root, **kwargs):
        seen.update(kwargs)
        return real_scope(diff_root, **kwargs)

    monkeypatch.setattr(external, "reviewer_visibility_scope", _capture)
    monkeypatch.setattr(external, "run_review", lambda **kwargs: {
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "contamination": (), "unisolated": False, "file": None, "failed": False,
        "started": True, "any_must_fix": False, "all_pass": True})

    assert external.main(["--paths", str(repo / "src"), "--no-gate",
                          "--output-dir", str(tmp_path / "raw")]) == 0
    # denylist: 해소된 폭(엔진 기본 + local.conf 승계)이 그대로 거울로 간다.
    assert "*vendor_dump*" in seen["denylist"]
    assert set(external._SECRET_DENYLIST_PATTERNS) <= set(seen["denylist"])
    # conf: 임시 홈 아티팩트·keep_extra 해소의 입력이라 빠지면 탈출구가 죽는다.
    assert seen["conf"]["reviewer_env_keep_extra"] == "VENDOR_REVIEW_KEY"


def test_reviewer_home_skips_unparsable_scrub_targets(external, tmp_path):
    """정화 못 한 파일은 복제하지 않는다 — 미인증 증상이 세션 흔적 유입보다 낫다."""
    source = _user_home_with_history(tmp_path)
    (source / ".claude.json").write_text("{깨진 json", encoding="utf-8")
    home = tmp_path / "reviewer-home"
    build = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), source,
    )
    copied = build.copied
    assert ".claude.json" not in copied and ".claude.json" in build.scrub_failed
    assert not (home / ".claude.json").exists()
    assert ".codex/auth.json" in copied      # 나머지 복제는 계속된다.


def test_reviewer_home_artifacts_land_where_the_env_points(external, tmp_path):
    """opencode 형상 정합 — 인증은 XDG_DATA_HOME, 설정은 XDG_CONFIG_HOME 아래로 간다."""
    home = tmp_path / "reviewer-home"
    external._build_reviewer_home(
        home, external.reviewer_home_artifacts(), _user_home_with_history(tmp_path),
    )
    env = external.reviewer_env(tmp_path / "tree", home, env={"PATH": "/usr/bin"})
    assert (Path(env["XDG_DATA_HOME"]) / "opencode" / "auth.json").is_file()
    assert (Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.jsonc").is_file()
    assert (Path(env["CODEX_HOME"]) / "auth.json").is_file()
    assert (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").is_file()


def test_reviewer_home_artifacts_extra_is_declarable(external, tmp_path):
    """배포별 인증 파일 이름은 코드 수정 없이 선언으로 추가한다(자기잠김 금지)."""
    source = _user_home_with_history(tmp_path)
    (source / ".config" / "vendor").mkdir(parents=True)
    (source / ".config" / "vendor" / "key.json").write_text("{}\n", encoding="utf-8")
    conf = {"reviewer_home_artifacts_extra": ".config/vendor/key.json"}
    assert ".config/vendor/key.json" in external.reviewer_home_artifacts(conf)
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, external.reviewer_home_artifacts(conf), source,
    ).copied
    assert ".config/vendor/key.json" in copied


def test_reviewer_home_ignores_escaping_artifact_paths(external, tmp_path):
    """선언이 홈 밖을 가리키면 무시한다 — 선언 표가 임의 파일 복제 통로가 되지 않게."""
    source = _user_home_with_history(tmp_path)
    home = tmp_path / "reviewer-home"
    copied = external._build_reviewer_home(
        home, ("../outside.txt", "/etc/passwd"), source,
    ).copied
    assert copied == ()


def test_reviewer_env_repoints_home_family_at_the_temp_home(external, tmp_path):
    """실 홈 값을 남기면 이름을 걸러도 전사 디렉터리의 부모를 그대로 넘기는 셈이다."""
    tree = tmp_path / "tree"
    home = tmp_path / "home"
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "CODEX_HOME": "/home/user/.codex",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
        "XDG_CONFIG_HOME": "/home/user/.config",
        "XDG_DATA_HOME": "/home/user/.local/share",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "OPENCODE_CONFIG_DIR": "/home/user/.config/opencode",
    }
    resolved = external.reviewer_env(tree, home, env=env)
    assert resolved["HOME"] == str(home)
    assert resolved["CODEX_HOME"] == str(home / ".codex")
    assert resolved["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert resolved["XDG_CONFIG_HOME"] == str(home / ".config")
    assert resolved["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert "XDG_RUNTIME_DIR" not in resolved and "OPENCODE_CONFIG_DIR" not in resolved
    assert not any(str(value).startswith("/home/user") for value in resolved.values())


def test_workspace_builds_tree_and_home_side_by_side(external, tmp_path):
    """홈은 거울 **밖**이다 — 거울 안이면 인증 파일이 검토 대상처럼 보이고 git add 대상이 된다."""
    repo = _standalone_adopter(tmp_path)
    workspace = external.create_reviewer_workspace(
        repo, base_dir=_jail(tmp_path), source_home=_user_home_with_history(tmp_path),
    )
    try:
        assert workspace.tree.parent == workspace.root
        assert workspace.home.parent == workspace.root
        assert not workspace.home.is_relative_to(workspace.tree)
        assert (workspace.home / ".codex" / "auth.json").exists()
        assert not list(workspace.tree.rglob("auth.json"))
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_visibility_scope_removes_workspace_after_use(external, tmp_path):
    repo = _standalone_adopter(tmp_path)
    with external.reviewer_visibility_scope(repo, base_dir=_jail(tmp_path)) as workspace:
        assert workspace is not None and workspace.root.is_dir()
        captured = workspace.root
    assert not captured.exists()


def test_cleanup_failure_is_loud(external, tmp_path, monkeypatch, capsys):
    """정리 실패를 삼키면 저장소 사본과 인증 파일 사본이 조용히 남는다."""
    def _boom(path):
        raise PermissionError(13, "삭제 불가")

    monkeypatch.setattr(external.shutil, "rmtree", _boom)
    workspace = external.ReviewerWorkspace(
        root=tmp_path / "container", tree=tmp_path / "container" / "tree",
        home=tmp_path / "container" / "home", files=1, skipped_unsafe=0, git_repo=True,
    )
    external._remove_reviewer_workspace(workspace)
    err = capsys.readouterr().err
    assert "정리 실패" in err and str(workspace.root) in err


def _blocked_gate_repo(external, monkeypatch, tmp_path) -> Path:
    """라운드 상한이 이미 닫힌 실 저장소 형상 (거부가 확정된 실행의 입력)."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config", lambda repo=None: {
        "additional_reviewer_enabled": "true", "additional_reviewer_round_limit": "0"})
    return repo


def test_round_limit_rejection_never_enters_the_isolation_seam(
        external, monkeypatch, tmp_path):
    """상한에 닿은 실행은 거울을 **만들지도 않는다** — 만들었다 지우는 건 부작용 0 이 아니다.

    "남은 게 없다"와 "격리 seam 에 들어간 적 없다"는 다른 진술이다: 거울 생성은 tracked 파일과 홈
    인증/설정을 실제로 복제하는 작업이고, 정리 실패는 loud 경고로 남을 뿐 되돌려지지 않는다
    (`test_cleanup_failure_is_loud`). 전송도 못 할 실행이 그 왕복을 하지 않게 예산 게이트가 격리
    **앞**에 선다."""
    repo = _blocked_gate_repo(external, monkeypatch, tmp_path)
    created: list[Path] = []
    real_create = external.create_reviewer_workspace

    def _record(*args, **kwargs):
        workspace = real_create(*args, **kwargs)
        created.append(workspace.root)
        return workspace

    monkeypatch.setattr(external, "create_reviewer_workspace", _record)
    calls = {"n": 0}

    def _never(**kwargs):
        calls["n"] += 1
        raise AssertionError("라운드 상한 초과 실행은 리뷰어를 호출하면 안 된다")

    monkeypatch.setattr(external, "run_review", _never)
    rc = external.main(["--gate", "T-9999", "--paths", str(repo / "src"),
                        "--output-dir", str(tmp_path / "raw")])
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED and calls["n"] == 0
    assert created == []                                   # 격리 seam 진입 0


def test_workspace_is_removed_when_the_review_raises(external, monkeypatch, tmp_path):
    """생성 이후 **모든 경로**가 정리를 지난다 — 예외로 빠져나가는 경로 포함."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config", lambda repo=None: {
        "additional_reviewer_enabled": "true"})
    created: dict[str, Path] = {}
    real_create = external.create_reviewer_workspace

    def _record(*args, **kwargs):
        workspace = real_create(*args, **kwargs)
        created["root"] = workspace.root
        return workspace

    monkeypatch.setattr(external, "create_reviewer_workspace", _record)

    def _boom(**kwargs):
        raise RuntimeError("리뷰 도중 예외")

    monkeypatch.setattr(external, "run_review", _boom)
    with pytest.raises(RuntimeError):
        external.main(["--gate", "T-9999", "--paths", str(repo / "src"),
                       "--output-dir", str(tmp_path / "raw")])
    assert created and not created["root"].exists()


def test_visibility_scope_fails_closed_by_default(external, tmp_path):
    """격리 실패의 기본은 차단 — echo 검출은 *인용한* 참조만 잡으므로 미격리는 관측 불가 구간이다."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with pytest.raises(external.ReviewerWorkspaceError):
        with external.reviewer_visibility_scope(empty, base_dir=_jail(tmp_path)):
            raise AssertionError("격리 실패 시 본문이 실행되면 안 된다")


def test_visibility_scope_opt_out_is_explicit_and_loud(external, tmp_path, capsys):
    """자기잠김 금지 탈출구 — 명시 요청일 때만 미격리로 계속하고, 조용히 넘어가지 않는다."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with external.reviewer_visibility_scope(
            empty, base_dir=_jail(tmp_path), allow_unisolated=True) as workspace:
        assert workspace is None
    err = capsys.readouterr().err
    assert external.UNISOLATED_REVIEWER_FLAG in err
    assert "세션 전사" in err  # 미격리로 무엇이 노출되는지까지 진단에 남는다.


# ── env: 호출 세션 포인터 제거 · 인증 앵커 보존 ────────────────────────────


def test_reviewer_env_is_an_allowlist_full_snapshot(external):
    """잔존 env **전수** 스냅샷 — allowlist 밖 이름은 모양과 무관하게 전부 빠진다.

    제거-list 였을 때의 실제 반례를 입력에 넣는다: 인증 예외어(PATH/CONFIG)를 품은 세션·원본
    포인터(`CLAUDE_TRANSCRIPT_PATH`·`CODEX_ROLLOUT_PATH`·`CLAUDE_PROJECT_DIR`)와 하네스 접두어가
    아예 없는 git 포인터(`GIT_DIR`).
    """
    env = {
        # 남아야 하는 것
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LANG": "ko_KR.UTF-8",
        "HTTPS_PROXY": "http://proxy:3128",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "CODEX_HOME": "/home/user/.codex",
        "CODEX_API_KEY": "k",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
        "OPENCODE_CONFIG_DIR": "/home/user/.config/opencode",
        "ANTHROPIC_API_KEY": "a",
        # 빠져야 하는 것
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "abc-123",
        "CLAUDE_TRANSCRIPT_PATH": "/home/user/.claude/projects/x/abc.jsonl",
        "CLAUDE_PROJECT_DIR": "/home/user/workspace/repo",
        "CODEX_THREAD_ID": "thread-9",
        "CODEX_ROLLOUT_PATH": "/home/user/.codex/sessions/9f2.jsonl",
        "OPENCODE_PID": "77",
        "GIT_DIR": "/home/user/workspace/repo/.git",
        "GIT_WORK_TREE": "/home/user/workspace/repo",
        "OLDPWD": "/home/user/workspace/repo",
        "PWD": "/home/user/workspace/repo",
        "SOME_UNKNOWN_TOOL_STATE": "x",
    }
    resolved = external.reviewer_env(Path("/tmp/ws"), env=env)
    assert resolved == {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LANG": "ko_KR.UTF-8",
        "HTTPS_PROXY": "http://proxy:3128",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "CODEX_HOME": "/home/user/.codex",
        "CODEX_API_KEY": "k",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
        "OPENCODE_CONFIG_DIR": "/home/user/.config/opencode",
        "ANTHROPIC_API_KEY": "a",
        "PWD": "/tmp/ws",  # cwd 를 무시하고 PWD 로 해석하는 하네스까지 닫는다.
    }


def test_reviewer_env_covers_relay_declared_session_markers(external):
    """세션 마커 선언이 늘어도 자동으로 빠지는지 — 두 표가 갈릴 여지 자체를 없앤다."""
    markers = {
        marker
        for group in external._load_relay().HARNESS_SESSION_MARKERS.values()
        for marker in group
    }
    env = {marker: "1" for marker in markers} | {"PATH": "/usr/bin"}
    resolved = external.reviewer_env(Path("/tmp/ws"), env=env)
    assert markers and not (markers & set(resolved))


def test_reviewer_env_keep_extra_opens_declared_names_only(external):
    """배포별 인증 이름이 allowlist 에 없어 게이트가 죽는 자기잠김을 막는 탈출구."""
    conf = {"reviewer_env_keep_extra": "OPENROUTER_API_KEY, my_vendor_token"}
    extra = external.reviewer_env_keep_extra(conf)
    env = {"OPENROUTER_API_KEY": "r", "MY_VENDOR_TOKEN": "v", "OTHER_STATE": "x"}
    resolved = external.reviewer_env(Path("/tmp/ws"), env=env, extra_keep=extra)
    assert resolved == {"OPENROUTER_API_KEY": "r", "MY_VENDOR_TOKEN": "v",
                        "PWD": "/tmp/ws"}
    assert external.reviewer_env(Path("/tmp/ws"), env=env) == {"PWD": "/tmp/ws"}


def test_reviewer_env_is_none_when_not_isolated(external):
    """미격리 실행은 종전처럼 env 를 상속한다(None = 상속)."""
    assert external.reviewer_env(None) is None


# ── 거울 git 격리: 바깥 설정/훅이 거울을 만지지 못한다 ─────────────────────


@pytest.mark.skipif(os.name == "nt", reason="셸 필터/훅 스크립트가 플랫폼-bound")
def test_workspace_git_ignores_outside_config_and_hooks(external, tmp_path, monkeypatch):
    """바깥 git config 가 거울 생성 단계에 개입하면 안 된다.

    `--no-verify` 는 pre-commit/commit-msg 만 막고, `-c` 로 끌 수 있는 노브는 열거 가능한 것뿐이다.
    `filter.*`(clean/smudge)는 `.gitattributes` 만 있으면 `git add` 중에 **임의 명령을 실행**하며
    이름을 미리 열거할 수도 없다 — 그래서 설정 발견 경로 자체를 끊는다.
    """
    repo = _standalone_adopter(tmp_path)  # 픽스처 커밋은 바깥 설정 주입 *전에* 끝낸다.
    (repo / ".gitattributes").write_text("* filter=inject\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "attributes")

    hooks = tmp_path / "outside-hooks"
    hooks.mkdir()
    hook_marker = tmp_path / "hook_ran.txt"
    hook = hooks / "post-commit"
    hook.write_text(f"#!/bin/sh\necho ran > {hook_marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    filter_marker = tmp_path / "filter_ran.txt"
    template = tmp_path / "outside-template"
    (template / "info").mkdir(parents=True)
    (template / "INJECTED.txt").write_text("주입\n", encoding="utf-8")
    # 주입 경로는 **HOME 발견 규칙**이다 — `GIT_CONFIG_GLOBAL` 로 넣으면 GIT_* 접두어 제거만으로도
    # 사라져 이 축을 검사하지 못한다(변이 민감도 실측). 실제 사용자 형상인 `~/.gitconfig` 로 넣는다.
    outside_home = tmp_path / "outside-home"
    outside_home.mkdir()
    (outside_home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks}\n"
        f"[init]\n\ttemplateDir = {template}\n"
        "[commit]\n\tgpgsign = true\n"
        # `tee` = 내용을 그대로 흘리면서 파일을 만드는 clean 필터 — 바깥 명령이 실제로 실행됐다는
        # 증거만 남기고 거울 내용은 바꾸지 않는다(git config 값에 `;`/`#` 를 넣으면 주석으로 잘린다).
        f'[filter "inject"]\n\tclean = tee {filter_marker}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("HOME", str(outside_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(outside_home))

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert workspace.git_repo is True   # gpgsign=true 를 그대로 탔으면 커밋이 실패한다.
        assert not filter_marker.exists()   # add 중 바깥 필터 명령 실행 없음.
        assert not hook_marker.exists()
        assert not (workspace.tree / ".git" / "INJECTED.txt").exists()
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_mirror_ignores_hijacked_git_env(external, tmp_path, monkeypatch):
    """훅 안에서 실행돼 `GIT_DIR` 이 살아 있어도 거울이 엉뚱한 저장소의 목록을 담지 않는다."""
    repo = _standalone_adopter(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "test")
    (other / "OTHER_ONLY.txt").write_text("다른 저장소\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    workspace = external.create_reviewer_workspace(repo, base_dir=_jail(tmp_path))
    try:
        assert (workspace.tree / "src" / "app.py").exists()
        assert not (workspace.tree / "OTHER_ONLY.txt").exists()
    finally:
        external.shutil.rmtree(workspace.root, ignore_errors=True)


def test_workspace_git_env_cuts_outside_git_pointers(external, tmp_path):
    """훅 안에서 실행돼 GIT_DIR 이 살아 있어도 거울 git 이 바깥 저장소를 건드리지 않는다."""
    env = external._workspace_git_env(tmp_path / "ws")
    assert not any(key.startswith("GIT_") and key not in {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    } for key in env)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["HOME"] == str(tmp_path / "ws" / ".git")


# ── 러너 seam: 격리 입력이 실제 스폰까지 도달하나 ──────────────────────────


def test_default_runner_declares_isolation_kwargs(external):
    """기본 러너가 cwd/env 를 시그니처로 선언해야 `**_ignored` 가 격리를 삼키지 않는다."""
    parameters = inspect.signature(external._watchdog_reviewer_run).parameters
    assert {"cwd", "env"} <= set(parameters)


def test_default_runner_forwards_isolation_to_relay_watchdog(external, monkeypatch):
    """선언만으로는 부족하다 — relay 워치독 호출까지 값이 내려가는지 실제로 태운다."""
    seen: dict[str, object] = {}

    class _FakeRelay:
        def run_with_first_event_watchdog(self, argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "판정: 통과\n", "")

    monkeypatch.setattr(external, "_reviewer_watchdog_settings",
                        lambda _cmd: (_FakeRelay(), None, 0))
    external._watchdog_reviewer_run(
        ["codex"], input="p", timeout=5, idle_timeout=None,
        cwd="/tmp/ws", env={"PWD": "/tmp/ws"},
    )
    assert seen["cwd"] == "/tmp/ws" and seen["env"] == {"PWD": "/tmp/ws"}


def test_isolation_kwargs_reach_injected_runner(external):
    seen: dict[str, object] = {}

    def _runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "판정: 통과\n", "")

    ok, _out, started = external._run_reviewer_ex(
        "p", "codex", 5, _runner, cwd=Path("/tmp/ws"), env={"PWD": "/tmp/ws"},
    )
    assert (ok, started) == (True, True)
    assert seen["cwd"] == "/tmp/ws" and seen["env"] == {"PWD": "/tmp/ws"}


def test_unisolated_call_keeps_legacy_runner_kwargs(external):
    """미격리(None) 실행의 kwargs 는 종전과 동일 — 기존 strict 러너 seam 무변경."""
    seen: dict[str, object] = {}

    def strict(argv, *, input, capture_output, text, encoding, errors, timeout):
        seen.update(locals())
        return subprocess.CompletedProcess(argv, 0, "판정: 통과\n", "")

    ok, _out, _started = external._run_reviewer_ex("p", "codex", 5, strict)
    assert ok is True and "cwd" not in seen and "env" not in seen


def test_isolation_with_incompatible_runner_is_loud_not_silent(external):
    """격리 입력을 못 받는 러너는 조용히 미격리로 떨어지지 않고 seam 오류로 선다."""
    def strict(argv, *, input, capture_output, text, encoding, errors, timeout):
        raise AssertionError("bind 전에 호출되면 안 됨")

    ok, out, started = external._run_reviewer_ex(
        "p", "codex", 5, strict, cwd="/tmp/ws", env={"PWD": "/tmp/ws"},
    )
    assert (ok, started) == (False, False)
    assert "runner seam 계약 오류" in out.answer and out.log == ""


# ── main 배선: 실 게이트가 격리를 태우나 ───────────────────────────────────


def test_main_runs_reviewer_inside_isolated_workspace(external, monkeypatch, tmp_path):
    """게이트 실행이 리뷰어에게 저장소 밖 거울 cwd 와 정화된 env 를 넘기고, 끝나면 지운다."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config",
                        lambda repo=None: {"additional_reviewer_enabled": "true"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    seen: dict[str, object] = {}

    def _fake_run_review(**kwargs):
        cwd = Path(kwargs["cwd"])
        seen["cwd"] = cwd
        seen["exists_during_call"] = cwd.is_dir()
        seen["mirrored"] = (cwd / "src" / "app.py").exists()
        seen["old_raw_visible"] = (cwd / ".project_manager" / ".local").exists()
        seen["session_pointer"] = "CLAUDE_CODE_SESSION_ID" in kwargs["env"]
        return {"reviewer": "codex", "ok": True, "output": "판정: 통과",
                "verdict": {"has_must_fix": False, "has_pass": True},
                "contamination": (), "file": None, "failed": False, "started": True,
                "any_must_fix": False, "all_pass": True}

    monkeypatch.setattr(external, "run_review", _fake_run_review)
    rc = external.main(["--paths", str(repo / "src"), "--no-gate",
                        "--output-dir", str(tmp_path / "raw")])

    assert rc == 0
    assert seen["exists_during_call"] is True and seen["mirrored"] is True
    assert seen["old_raw_visible"] is False
    assert seen["session_pointer"] is False
    assert not seen["cwd"].exists()  # 실행 뒤 거울은 남지 않는다.


def test_isolation_banner_names_applied_keep_extra(external, monkeypatch, tmp_path, capsys):
    """탈출구가 보이지 않으면 없는 것과 같다 — 통과시킨 이름을 격리 배너에 병기한다."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config", lambda repo=None: {
        "additional_reviewer_enabled": "true",
        "reviewer_env_keep_extra": "VENDOR_REVIEW_KEY",
    })
    monkeypatch.setenv("VENDOR_REVIEW_KEY", "k")
    monkeypatch.setattr(external, "run_review", lambda **kwargs: {
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "contamination": (), "unisolated": False, "file": None, "failed": False,
        "started": True, "any_must_fix": False, "all_pass": True})

    assert external.main(["--paths", str(repo / "src"), "--no-gate",
                          "--output-dir", str(tmp_path / "raw")]) == 0
    err = capsys.readouterr().err
    assert "reviewer_env_keep_extra 통과: VENDOR_REVIEW_KEY" in err


def test_reviewer_failure_hint_names_both_escape_keys(external, monkeypatch, tmp_path, capsys):
    """allowlist/임시 홈 때문에 죽는 경로는 격리 실패 안내가 안 뜬다 — 실패 진단이 키를 알려준다."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config",
                        lambda repo=None: {"additional_reviewer_enabled": "true"})
    monkeypatch.setattr(external, "run_review", lambda **kwargs: {
        "reviewer": "codex", "ok": False, "output": "[리뷰어 실행 오류: 401 unauthorized]",
        "verdict": {"has_must_fix": False, "has_pass": False},
        "contamination": (), "unisolated": False, "file": None, "failed": True,
        "started": True, "any_must_fix": False, "all_pass": False})

    assert external.main(["--paths", str(repo / "src"), "--no-gate",
                          "--output-dir", str(tmp_path / "raw")]) == 1
    err = capsys.readouterr().err
    # 이름만 스치는 게 아니라 **실행 가능한 처방**(키=값 형태)이어야 진단으로서 쓸모가 있다.
    assert "reviewer_home_artifacts_extra=<홈 상대경로" in err
    assert "reviewer_env_keep_extra=<이름" in err
    assert external.UNISOLATED_REVIEWER_FLAG in err


def _wire_unbuildable_isolation(external, monkeypatch, tmp_path):
    """격리 생성만 실패시키고 나머지 게이트 경로는 그대로 태우는 배선."""
    repo = _standalone_adopter(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(external, "extract_diff",
                        lambda *a, **k: ("diff --git a/x b/x\n+n\n", []))
    monkeypatch.setattr(external, "local_config",
                        lambda repo=None: {"additional_reviewer_enabled": "true"})

    def _boom(*a, **k):
        raise external.ReviewerWorkspaceError("거울 생성 실패(주입)")

    monkeypatch.setattr(external, "create_reviewer_workspace", _boom)
    calls = {"n": 0}

    def _fake_run_review(**kwargs):
        calls["n"] += 1
        calls["cwd"] = kwargs["cwd"]
        return {"reviewer": "codex", "ok": True, "output": "판정: 통과",
                "verdict": {"has_must_fix": False, "has_pass": True},
                "contamination": (), "file": None, "failed": False, "started": True,
                "any_must_fix": False, "all_pass": True}

    monkeypatch.setattr(external, "run_review", _fake_run_review)
    return repo, calls


def test_main_blocks_the_gate_when_isolation_fails(external, monkeypatch, tmp_path, capsys):
    """기본은 차단 — 격리 없이 외부로 나가지 않는다(전송 0)."""
    repo, calls = _wire_unbuildable_isolation(external, monkeypatch, tmp_path)
    rc = external.main(["--paths", str(repo / "src"), "--no-gate",
                        "--output-dir", str(tmp_path / "raw")])
    assert rc == 1 and calls["n"] == 0
    err = capsys.readouterr().err
    assert "리뷰어 가시 범위 격리 실패" in err
    assert external.UNISOLATED_REVIEWER_FLAG in err  # 복구 채널을 진단이 직접 알려준다.


def test_main_opt_out_flag_runs_unisolated_once(external, monkeypatch, tmp_path, capsys):
    """명시 opt-out 은 자기잠김 탈출구 — 미격리로 1회 실행하되 판정은 강등하지 않는다."""
    repo, calls = _wire_unbuildable_isolation(external, monkeypatch, tmp_path)
    rc = external.main([external.UNISOLATED_REVIEWER_FLAG, "--paths", str(repo / "src"),
                        "--no-gate",
                        "--output-dir", str(tmp_path / "raw")])
    assert rc == 0 and calls["n"] == 1 and calls["cwd"] is None
    assert calls["cwd"] is None        # 미격리 사실이 실인자에서 그대로 드러난다.
    assert external.UNISOLATED_REVIEWER_FLAG in capsys.readouterr().err


def test_unisolated_run_is_marked_in_result_and_verdict_block(external, tmp_path, capsys):
    """stderr 만으로는 부족하다 — PM 이 반드시 읽는 판정 블록에 미격리 사실이 남아야 한다."""
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,   # cwd 미지정 = 실제로 미격리
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, "판정: 통과\n", ""),
    )
    assert result["unisolated"] is True
    external.print_summary(result)
    out = capsys.readouterr().out
    assert "미격리 실행" in out and external.UNISOLATED_REVIEWER_FLAG in out


def test_isolated_run_keeps_the_verdict_block_unchanged(external, tmp_path, capsys):
    """격리 정상 실행의 판정 블록은 종전과 동일하다(진단 라인 없음)."""
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        cwd=tmp_path, env={"PWD": str(tmp_path)},
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, "판정: 통과\n", ""),
    )
    assert result["unisolated"] is False
    external.print_summary(result)
    assert "미격리 실행" not in capsys.readouterr().out


# ── echo 오염 검출 (백스톱) ────────────────────────────────────────────────


def test_detects_old_raw_artifact_citation(external):
    output = (
        "판정: 통과\n\n**must-fix**:\n- 없음\n\n"
        "참고: .project_manager/.local/review/"
        "external_review_codex_20260806_040406_11_ab.txt 의 지적과 동일하다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.raw_artifacts == (
        "external_review_codex_20260806_040406_11_ab.txt",
    )
    assert any("raw 파일명 인용" in marker for marker in contamination.markers)


def test_detects_session_transcript_citation(external):
    output = "판정: 통과\n\n/home/user/.claude/projects/-home-user-repo/9f2.jsonl 를 확인했다.\n"
    contamination = external.detect_output_contamination(output)
    assert contamination.transcripts and any(
        "세션 전사" in marker for marker in contamination.markers)


def test_detects_conflicting_verdict_blocks(external):
    """옛 판정 블록이 앞에 echo 되면 파서가 그걸 이번 판정으로 읽는다 — 다중/불일치를 세운다."""
    output = (
        "이전 라운드 원문:\n판정: 반려\n\n**must-fix**:\n- 옛 지적\n\n"
        "---\n이번 리뷰:\n판정: 통과\n\n**must-fix**:\n- 없음\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.verdicts == ("반려", "통과")
    assert contamination.verdict_conflict is True


def test_repeated_same_verdict_is_not_flagged(external):
    """같은 판정의 재진술은 위험이 없다 — false-red 를 만들지 않는다."""
    output = "판정: 통과\n\n**must-fix**:\n- 없음\n\n## 요약\n판정: 통과\n"
    contamination = external.detect_output_contamination(output)
    assert len(contamination.verdicts) == 2
    assert contamination.verdict_conflict is False
    assert contamination.markers == ()


def test_detector_sees_every_verdict_line_the_parser_can_pick(external):
    """검출기가 파서보다 좁으면 파서가 집어 든 echo 라인을 못 본다 — 같은 함수를 쓰는지 단언한다.

    파서는 선언 목록의 **첫 줄**을 판정으로 쓰고 검출기는 **전량**을 본다(= 검출기 ⊇ 파서).
    """
    output = "판정: 반려\n\n**must-fix**:\n- 옛 지적\n\n판정: 통과\n"
    words = external.verdict_words(output)
    assert words == ("반려", "통과")
    assert external.parse_verdict(output)["has_must_fix"] is True  # 파서는 첫 줄(반려)을 집는다.
    contamination = external.detect_output_contamination(output)
    assert contamination.verdicts == words
    assert contamination.verdict_conflict is True


def test_prose_verdict_inside_a_sentence_is_not_a_declaration(external):
    """행 선두 앵커의 **판별** 축 — 산문 안 인용 판정은 선언이 아니고 오탐도 만들지 않는다.

    앵커를 풀면(문서 어디의 `판정:` 이든 세면) 같은 입력이 '판정 라인 다중/불일치'가 된다.
    """
    output = (
        "참고: 이전 라운드에서는 판정: 반려 였다.\n\n"
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    )
    assert external.verdict_words(output) == ("통과",)
    assert external.detect_output_contamination(output).verdict_conflict is False
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}


@pytest.mark.parametrize("prose", (
    # 실측 형상: 판정선이 아예 없고 **본문에만** '통과' 가 있다 — 본문 전역 스캔이면 통과가 된다.
    "회귀 통과 확인. 문제 없음.\n\n**must-fix** (반드시 수정):\n- 없음\n",
    "이번 리뷰의 판정: 통과 이다.\n\n**must-fix** (반드시 수정):\n- 없음\n",
    "이번 리뷰의 판정: 반려 이다.\n",
))
def test_format_noncompliant_prose_verdict_is_never_a_pass(external, tmp_path, prose):
    """형식 미준수 출력은 통과로 접지 않는다 — 앵커 판정선 0개면 '판정 불명확'(exit 1)."""
    assert external.verdict_words(prose) == ()
    assert external.parse_verdict(prose)["has_pass"] is False
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, prose, ""),
    )
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_prose_fallback_stays_off_when_a_declaration_exists(external):
    """앵커 선언이 하나라도 있으면 폴백은 비활성 — 정상 형식의 통과가 오탐으로 막히지 않는다."""
    output = (
        "덧붙임: 이전 라운드의 판정: 반려 는 해소됐다.\n\n"
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    )
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}


@pytest.mark.parametrize("word", ("[통과", "비통과", "PASS/REJECT", "통과|반려", "미정"))
def test_only_exact_verdict_tokens_count_as_a_verdict(external, word):
    """부분 문자열이면 템플릿 echo·부정형·선택지 나열이 전부 '통과'로 읽힌다(false-green 실측)."""
    assert external.verdict_kind(word) == external.VERDICT_UNKNOWN


@pytest.mark.parametrize("word,kind", (
    ("통과", "pass"), ("**통과**", "pass"), ("통과.", "pass"), ("PASS", "pass"),
    ("반려", "reject"), ("*반려*", "reject"), ("REJECT", "reject"),
))
def test_exact_tokens_survive_emphasis_and_sentence_punctuation(external, word, kind):
    """강조/문장부호만 벗긴다 — 정상 형식이 정확일치 때문에 불명확이 되면 안 된다."""
    assert external.verdict_kind(word) == kind


def test_prompt_template_echo_is_not_a_pass(external, tmp_path):
    """프롬프트 출력 형식이 회신에 그대로 실려도 통과가 아니다 — `판정: [통과 | 반려]`."""
    echoed = (
        "판정: [통과 | 반려]\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
        "**suggestion** (권장):\n- 없음\n"
    )
    assert external.verdict_words(echoed) == ("[통과",)
    assert external.parse_verdict(echoed)["has_pass"] is False
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, echoed, ""),
    )
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_negated_verdict_is_not_a_pass(external, tmp_path):
    """`판정: 비통과` 는 부분 문자열로는 통과였다 — 정확일치에서는 불명확이다."""
    output = "판정: 비통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.parse_verdict(output)["has_pass"] is False
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
    )
    assert result["all_pass"] is False


def test_unknown_verdict_line_beside_a_real_one_is_ambiguous(external):
    """허용 토큰이 아닌 판정선이 진짜 판정선과 섞이면 어느 게 이번 판정인지 모른다."""
    output = "판정: [통과 | 반려]\n\n판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.detect_output_contamination(output).verdict_conflict is True


def test_raw_artifact_regex_accepts_underscored_reviewer_names(external):
    """reviewer 이름에 `_` 가 있는 배포(`external_review_my_reviewer_…`)의 인용도 잡는다."""
    output = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n**suggestion** (권장):\n"
        "- external_review_my_reviewer_20260806_040406_11_ab.txt 의 지적과 같다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.raw_artifacts == (
        "external_review_my_reviewer_20260806_040406_11_ab.txt",
    )


def test_transcript_regex_detects_windows_paths(external):
    """Windows 형상 전사 경로를 못 보면 그 플랫폼에서는 백스톱이 없는 것과 같다."""
    output = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n**suggestion** (권장):\n"
        "- C:\\Users\\u\\.claude\\projects\\-repo\\9f2.jsonl 에서 확인했다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.transcripts
    assert any("9f2.jsonl" in hit for hit in contamination.transcripts)
    assert any("세션 전사" in marker for marker in contamination.markers)


def test_clean_output_has_no_markers(external):
    contamination = external.detect_output_contamination(
        "판정: 통과\n\n**must-fix**:\n- 없음\n\n**suggestion**:\n- 없음\n")
    assert contamination.markers == ()


# 실측 오염 형상 그대로의 **판별 픽스처**: 리뷰어 자신의 블록(통과·must-fix 없음)이 먼저 오고 옛
# 라운드 블록(반려)이 뒤에 echo 된다. 파서는 앞 블록만 보고 통과를 내므로, 이 입력에서 all_pass 를
# 막는 것은 오직 오염 강등뿐이다(앞뒤가 바뀐 입력은 파서 단독으로 이미 비-통과라 판별력이 0이다).
_ECHOED_OLD_BLOCK_OUTPUT = (
    "판정: 통과\n\n"
    "**must-fix** (반드시 수정):\n- 없음\n\n"
    "**suggestion** (권장):\n- 없음\n\n"
    "--- 참고: 이전 라운드 원문 ---\n"
    "판정: 반려\n\n"
    "**must-fix** (반드시 수정):\n- 옛 지적\n"
)


def test_parser_alone_reads_the_echoed_pass_as_a_clean_pass(external):
    """판별력 확인 — 이 입력은 파서 단독으로는 '통과'다(강등 절이 유일한 방어선)."""
    verdict = external.parse_verdict(_ECHOED_OLD_BLOCK_OUTPUT)
    assert verdict == {"has_must_fix": False, "has_pass": True}


def _isolated_run_kwargs(root: Path) -> dict:
    """격리 실행 DI 헬퍼 — cwd 와 정화 env 를 **함께** 주입한다(둘 다 있어야 격리다)."""
    return {"cwd": root, "env": {"PWD": str(root)}}


def test_isolation_requires_both_cwd_and_clean_env(external, tmp_path):
    """cwd 만 옮기고 env 를 상속하면 세션 포인터가 그대로 넘어간다 — 격리로 기록하면 안 된다."""
    def _runner(*a, **k):
        return subprocess.CompletedProcess(["codex"], 0, "판정: 통과\n", "")

    both = external.run_review("p", reviewer_cmd="codex", output_dir=tmp_path,
                               run_fn=_runner, **_isolated_run_kwargs(tmp_path))
    cwd_only = external.run_review("p", reviewer_cmd="codex", output_dir=tmp_path,
                                   run_fn=_runner, cwd=tmp_path)
    env_only = external.run_review("p", reviewer_cmd="codex", output_dir=tmp_path,
                                   run_fn=_runner, env={"PWD": str(tmp_path)})
    assert both["unisolated"] is False
    assert cwd_only["unisolated"] is True and env_only["unisolated"] is True


def test_contaminated_reject_is_not_recorded_as_a_reject(external, tmp_path):
    """옛 반려 블록 echo 를 '이번 리뷰의 반려'로 기록하면 리뷰어가 안 한 지적으로 일이 돈다."""
    output = (
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- 옛 지적\n\n"
        "**suggestion** (권장):\n- 없음\n\n--- 이전 라운드 원문 ---\n판정: 통과\n"
    )
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
        **_isolated_run_kwargs(tmp_path),
    )
    assert result["contamination"]
    assert result["any_must_fix"] is False and result["all_pass"] is False
    assert external._round_has_verdict(result) is False   # 장부에서도 판정이 아니다.
    assert external.determine_exit_code(result) == 1      # 여전히 비-통과(보수적).


def test_clean_reject_is_still_recorded_as_a_verdict(external, tmp_path):
    """오염 없는 반려는 종전대로 반려다 — 무효화가 정상 판정까지 삼키면 안 된다."""
    output = "판정: 반려\n\n**must-fix** (반드시 수정):\n- 실제 지적\n"
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
        **_isolated_run_kwargs(tmp_path),
    )
    assert result["contamination"] == ()
    assert result["any_must_fix"] is True
    assert external._round_has_verdict(result) is True


def test_run_review_downgrades_conflicting_verdict_to_unclear(external, tmp_path):
    """오염된 출력에서 '통과'가 그대로 나가면 false-green — 보수적으로 판정 불명확이어야 한다."""
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(
            ["codex"], 0, _ECHOED_OLD_BLOCK_OUTPUT, ""),
    )
    assert result["all_pass"] is False
    assert result["contamination"] and any(
        "판정 라인" in marker for marker in result["contamination"])
    assert external.determine_exit_code(result) == 1


def test_parser_and_detector_read_the_same_text(external, tmp_path):
    """파서만 진행 로그를 보면 로그의 옛 판정 블록이 판정에 반영되고 검출은 못 잡는다."""
    answer = "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    log = "이전 라운드 원문:\n판정: 반려\n\n**must-fix** (반드시 수정):\n- 옛 지적\n"
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, answer, log),
    )
    # 회신 구간만 본 판정 = 통과 · 오염 신호 없음 → 진행 로그가 판정에도 검출에도 안 샌다.
    assert result["verdict"] == {"has_must_fix": False, "has_pass": True}
    assert result["contamination"] == () and result["all_pass"] is True


@pytest.mark.parametrize("wrapped", (
    "```\n판정: 반려\n```\n",
    "> 판정: 반려\n",
))
def test_quoted_and_fenced_verdicts_count_for_neither_parser_nor_detector(external, wrapped):
    """인용/코드펜스 안의 판정 문구는 리뷰어 선언이 아니다 — 두 표면이 같이 무시해야 한다.

    검토 대상 diff 에 든 판정 문안(이 저장소의 테스트 픽스처가 그렇다)이 이 축의 상시 오탐 원천이라,
    좁히는 규칙을 한쪽에만 걸면 오탐이 남거나 "검출기 ⊇ 파서" 성질이 깨진다.
    """
    output = wrapped + "\n판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.verdict_words(output) == ("통과",)
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}
    assert external.detect_output_contamination(output).verdict_conflict is False


@pytest.mark.parametrize("citation", (
    "- external_review_codex_20260806_040406_11_ab.txt 의 지적과 같다.\n",
    "- /home/user/.claude/projects/-home-user-repo/9f2.jsonl 에서 확인했다.\n",
))
def test_run_review_downgrades_any_contaminated_pass(external, tmp_path, citation):
    """판정이 하나여도 옛 raw·전사를 인용했으면 그 통과는 리뷰어 자신의 판정이라는 보장이 없다."""
    output = ("판정: 통과\n\n**must-fix**:\n- 없음\n\n**suggestion**:\n" + citation)
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
    )
    assert result["contamination"]
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_reviewer_output_keeps_channels_structurally_separate(external):
    """회신/로그 경계는 **필드**다 — 표시 문자열을 되파싱하지 않는다.

    구분자 파싱이던 시절의 반례를 그대로 넣는다: 회신 안에 표시 구분자와 옛 판정 블록이 섞여
    들어와도 이번 판정(반려)이 '깨끗한 통과'로 잘리면 안 된다.
    """
    poisoned_answer = (
        "인용: 옛 라운드 원문\n판정: 통과\n" + external._STDERR_SECTION_MARKER
        + "판정: 반려\n\n**must-fix** (반드시 수정):\n- 이번 지적\n"
    )
    output = external.ReviewerOutput(poisoned_answer, "workdir: /tmp/ws\n")
    assert output.answer == poisoned_answer          # 잘리지 않는다.
    assert output.log == "workdir: /tmp/ws\n"
    assert output.combined.endswith("workdir: /tmp/ws\n")
    contamination = external.detect_output_contamination(output.answer)
    assert contamination.verdict_conflict is True    # 두 판정선이 모두 보인다.


def test_reviewer_output_normalizes_plain_string_runners(external):
    """문자열만 돌려주는 주입 러너/스텁은 회신 채널로 정규화한다(로그 없음)."""
    normalized = external._as_reviewer_output("판정: 통과\n")
    assert normalized == external.ReviewerOutput("판정: 통과\n", "")
    assert external._as_reviewer_output(normalized) is normalized


def test_progress_log_prompt_echo_is_not_contamination(external, tmp_path):
    """라이브 실측 형상 회귀: codex 진행 로그는 프롬프트 템플릿과 diff 원문을 그대로 싣는다.

    거기까지 오염으로 세면 (a) 모든 실행이 '판정 라인 다중'이 되고 (b) 검토 대상 코드에 든
    판정 문안/raw 파일명이 오염으로 둔갑해 정상 통과가 false-red 가 된다.
    """
    # 라이브 회신 형상 그대로 — 프롬프트가 강제하는 두 섹션이 다 있다.
    answer = ("판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
              "**suggestion** (권장):\n- 없음\n")
    progress_log = (
        "Reading prompt from stdin...\nworkdir: /tmp/pm_review_workspace_x\n"
        "판정: [통과 | 반려]\n"                     # 프롬프트 출력 형식 템플릿 echo
        "+    output = \"판정: 반려\\n\"\n"          # 검토 대상 diff 안의 판정 문안
        "+    raw = \"external_review_codex_20260806_040406_11_ab.txt\"\n"
    )
    result = external.run_review(
        "p", reviewer_cmd="codex", output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(
            ["codex"], 0, answer, progress_log),
    )
    assert result["contamination"] == ()
    assert result["all_pass"] is True
    assert result["log"] == progress_log        # 로그는 버리지 않고 따로 보관한다.
    assert progress_log in result["output"]     # raw 박제 표시에는 그대로 남는다.


def test_print_summary_surfaces_contamination(external, capsys):
    external.print_summary({
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "contamination": ("옛 리뷰/위임 raw 파일명 인용: external_review_codex_1.txt",),
        "file": None, "failed": False, "started": True,
        "any_must_fix": False, "all_pass": True,
    })
    out = capsys.readouterr().out
    assert "오염 의심" in out and "external_review_codex_1.txt" in out


def test_print_summary_unchanged_without_contamination(external, capsys):
    """오염 0건이면 출력은 종전과 동일하다."""
    external.print_summary({
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "file": None, "failed": False, "started": True,
        "any_must_fix": False, "all_pass": True,
    })
    assert "오염 의심" not in capsys.readouterr().out
