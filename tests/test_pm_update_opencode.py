"""pm_update.py opencode 타깃 동기화 단위 테스트.

plan/apply 분리 설계 덕에 파일시스템 임시 디렉토리만 있으면 외부 의존 없이 테스트 가능.
"""
from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_update():
    return _load_pm_update()


# ── 심볼 존재 확인 ─────────────────────────────────────────────────────────

def test_pm_update_exposes_new_symbols(pm_update):
    assert callable(pm_update.resolve_target_root)
    assert callable(pm_update.resolve_manifest_for_dest)
    # KNOWN_TARGETS 는 제거됨 — 타깃 발견은 templates/*/ 디렉토리 존재로 (ADR-0006)
    assert not hasattr(pm_update, "KNOWN_TARGETS"), (
        "KNOWN_TARGETS 가 남아 있음 — 디렉토리 기반 발견으로 교체됐어야 한다."
    )


# ── resolve_target_root ────────────────────────────────────────────────────

def test_resolve_target_root_opencode_ok(pm_update):
    """실제 REPO/templates/opencode/ 가 있으면 REPO 기준 경로를 반환한다."""
    result = pm_update.resolve_target_root("opencode")
    assert result == REPO / "templates" / "opencode"


def test_resolve_target_root_claude_code_ok(pm_update):
    """실제 REPO/templates/claude_code/ 가 있으면 REPO 기준 경로를 반환한다."""
    result = pm_update.resolve_target_root("claude_code")
    assert result == REPO / "templates" / "claude_code"


def test_resolve_target_root_unknown_raises(pm_update):
    """templates/ 하위에 디렉토리가 없는 타깃 이름은 FileNotFoundError 를 발생시킨다."""
    with pytest.raises(FileNotFoundError, match="알 수 없는 타깃 또는 디렉토리 없음"):
        pm_update.resolve_target_root("nonexistent_target_xyz_should_not_exist")


def test_resolve_target_root_result_is_under_repo(pm_update):
    """반환 경로가 항상 REPO 하위임을 보장한다 (source 와 독립적)."""
    result = pm_update.resolve_target_root("opencode")
    assert result.is_relative_to(pm_update.REPO), (
        f"dest({result})가 REPO({pm_update.REPO}) 하위가 아님 — source 가 오염됐을 가능성."
    )


# ── resolve_manifest_for_dest ──────────────────────────────────────────────

def test_resolve_manifest_prefers_dest(pm_update, tmp_path):
    """dest_root 에 engine.manifest 가 있으면 그것을 우선한다."""
    dest = tmp_path / "dest"
    source = tmp_path / "source"
    dest_manifest = dest / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True)
    dest_manifest.write_text("dest_entry\n", encoding="utf-8")

    source_manifest = source / ".project_manager" / "engine.manifest"
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text("source_entry\n", encoding="utf-8")

    result = pm_update.resolve_manifest_for_dest(dest, source)
    assert result == dest_manifest


def test_resolve_manifest_falls_back_to_source(pm_update, tmp_path):
    """dest_root 에 engine.manifest 가 없으면 source_root 의 것을 쓴다."""
    dest = tmp_path / "dest"
    source = tmp_path / "source"
    dest.mkdir()

    source_manifest = source / ".project_manager" / "engine.manifest"
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text("source_entry\n", encoding="utf-8")

    result = pm_update.resolve_manifest_for_dest(dest, source)
    assert result == source_manifest


def test_resolve_manifest_raises_when_both_missing(pm_update, tmp_path):
    """dest·source 둘 다 engine.manifest 가 없으면 FileNotFoundError."""
    dest = tmp_path / "dest"
    source = tmp_path / "source"
    dest.mkdir()
    source.mkdir()
    with pytest.raises(FileNotFoundError, match="engine.manifest 없음"):
        pm_update.resolve_manifest_for_dest(dest, source)


# ── plan() with dest_root ──────────────────────────────────────────────────

def _make_source(root: Path, files: dict[str, str]) -> None:
    """source_root 에 파일들을 생성한다."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_plan_with_dest_root_new_files(pm_update, tmp_path):
    """dest_root 가 지정되면 해당 경로 기준으로 new 변경이 계획된다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_source(source, {".project_manager/tools/board.py": "# board"})

    manifest = [".project_manager/tools/board.py"]
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert len(changes) == 1
    rel, src_path, dst_path, kind = changes[0]
    assert rel == ".project_manager/tools/board.py"
    assert kind == "new"
    assert dst_path == dest / ".project_manager/tools/board.py"


def test_plan_with_dest_root_update_when_different(pm_update, tmp_path):
    """source 와 dest 파일 내용이 다르면 update 로 계획된다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _make_source(source, {".project_manager/tools/board.py": "# new content"})
    _make_source(dest, {".project_manager/tools/board.py": "# old content"})

    manifest = [".project_manager/tools/board.py"]
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert len(changes) == 1
    assert changes[0][3] == "update"


def test_plan_with_dest_root_no_change_when_identical(pm_update, tmp_path):
    """source 와 dest 파일이 동일하면 변경 없음."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    content = "# same content"
    _make_source(source, {".project_manager/tools/board.py": content})
    _make_source(dest, {".project_manager/tools/board.py": content})

    manifest = [".project_manager/tools/board.py"]
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert len(changes) == 0
    assert len(missing) == 0


def test_plan_without_dest_root_defaults_to_repo(pm_update, tmp_path):
    """dest_root=None 이면 REPO 기준으로 dst 가 계산된다 (dst 경로를 직접 assert)."""
    source = tmp_path / "source"
    source.mkdir()

    # source 에 sentinel 파일을 만들어 changes 에 포함시킨다
    rel = ".project_manager/tools/__nonexistent_test_sentinel__.py"
    sentinel = source / rel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# sentinel", encoding="utf-8")

    manifest = [rel]
    changes, missing = pm_update.plan(source, manifest, dest_root=None)

    # REPO 하위에 파일이 없으므로 "new" 로 계획돼야 한다
    assert len(changes) == 1
    _rel, _src, dst_path, kind = changes[0]
    assert kind == "new"
    # dst 가 REPO 기준인지 직접 검증
    assert dst_path == pm_update.REPO / rel, (
        f"dest_root=None 일 때 dst={dst_path!r}이 REPO/{rel} 이어야 함"
    )


# ── source != REPO 케이스 — dest 가 항상 REPO/templates/ 임을 검증 ─────────

def test_resolve_target_root_dest_is_repo_not_external_source(pm_update, tmp_path):
    """--from 이 REPO 외부 upstream 이어도 dest 는 항상 REPO/templates/ 를 가리킨다.

    이것이 버그 수정의 핵심 불변식이다:
      - source_root(엔진 읽기)과 dest_root(동기화 쓰기)는 독립적.
      - --from <external> --target opencode 시 dest 가 external/templates/ 를 덮어쓰면 안 된다.
    """
    # resolve_target_root 는 이제 source 인자가 없다 — REPO 만 사용
    dest = pm_update.resolve_target_root("opencode")

    assert dest == pm_update.REPO / "templates" / "opencode"
    # 외부 경로(tmp_path)와 무관해야 한다
    assert not dest.is_relative_to(tmp_path), (
        "dest 가 외부 source(tmp_path) 하위를 가리키고 있다 — source/dest 분리 버그."
    )


def test_plan_dest_is_repo_templates_when_source_is_external(pm_update, tmp_path):
    """source 가 외부 upstream 일 때 plan() 의 dst 경로가 REPO/templates/ 하위임을 검증.

    --from <external> --target opencode 시나리오:
      - source_root = tmp_path 하위 (외부 upstream 시뮬레이션)
      - dest_root   = REPO/templates/opencode (이 repo — source 와 무관)
    """
    source = tmp_path / "external_upstream"
    _make_source(source, {".project_manager/tools/board.py": "# external board"})

    # dest 는 실제 REPO/templates/opencode (source 와 독립)
    dest = REPO / "templates" / "opencode"

    manifest = [".project_manager/tools/board.py"]
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    # changes 가 있든 없든 dst 가 항상 dest(REPO/templates/opencode) 하위여야 한다
    for _rel, _src, dst_path, _kind in changes:
        assert dst_path.is_relative_to(dest), (
            f"dst={dst_path!r}이 dest({dest}) 하위가 아님"
        )
        assert not dst_path.is_relative_to(tmp_path), (
            f"dst={dst_path!r}이 외부 source({tmp_path}) 하위를 가리킴 — 버그."
        )


# ── main() --target 통합 (임시 파일시스템) ────────────────────────────────

def test_main_target_opencode_dry_run(pm_update, tmp_path, monkeypatch):
    """--target opencode --dry-run 이 파일을 변경하지 않고 rc=0 을 반환한다.

    source 와 dest 를 모두 tmp_path 하위로 monkeypatch 해 실 REPO 에 영향 없이 검증한다.
    manifest 항목이 source 에 모두 존재하면 missing=0 → changes>0 → dry-run 이므로 rc=0.
    dry-run 이므로 dest 에 실제 파일이 생성되지 않아야 한다.
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    sentinel_rel = ".project_manager/tools/__dry_run_sentinel__.py"

    # source 에 sentinel 파일과 manifest 준비
    _make_source(source, {sentinel_rel: "# dry run sentinel\n"})
    manifest_file = source / ".project_manager" / "engine.manifest"
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(sentinel_rel + "\n", encoding="utf-8")

    # fake_repo 에 templates/opencode/ 타깃 디렉토리 생성 (dest manifest 없음 → source fallback)
    dest = fake_repo / "templates" / "opencode"
    dest.mkdir(parents=True)

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main([
        "--from", str(source),
        "--target", "opencode",
        "--dry-run",
    ])

    assert rc == 0
    # dry-run 이므로 실제 파일은 생성되지 않아야 한다
    assert not (dest / sentinel_rel).exists()


def test_main_target_unknown_returns_error(pm_update, tmp_path):
    """REPO/templates/<name>/ 디렉토리가 없는 타깃 이름은 rc=1 을 반환한다."""
    source = tmp_path / "upstream"
    source.mkdir()

    # templates/ 하위에 절대 존재하지 않을 이름 사용
    rc = pm_update.main([
        "--from", str(source),
        "--target", "no_such_target_xyz_should_not_exist",
        "--dry-run",
    ])

    assert rc == 1


# ── opencode 타깃 engine.manifest 내용 검증 ──────────────────────────────

def test_opencode_manifest_excludes_claude_adapter(pm_update):
    """실제 opencode engine.manifest 가 .claude/ 항목을 포함하지 않는다."""
    opencode_manifest = (
        REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    )
    assert opencode_manifest.exists(), "templates/opencode/.project_manager/engine.manifest 없음"

    entries = pm_update.read_manifest(opencode_manifest)
    claude_entries = [e for e in entries if e.startswith(".claude/")]
    assert claude_entries == [], (
        f".claude/ 항목이 opencode manifest 에 있으면 안 됨: {claude_entries}"
    )


def test_opencode_manifest_excludes_github_workflows(pm_update):
    """opencode manifest 가 .github/workflows/ 항목을 포함하지 않는다.

    opencode 채택자(Windows 환경 등)는 GitHub Actions 가 없을 수 있으므로
    regression.yml 같은 CI 설정은 opencode 타깃 manifest 에서 제외한다.
    """
    opencode_manifest = (
        REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    )
    entries = pm_update.read_manifest(opencode_manifest)
    gh_entries = [e for e in entries if e.startswith(".github/")]
    assert gh_entries == [], (
        f".github/ 항목이 opencode manifest 에 있으면 안 됨: {gh_entries}"
    )


def test_opencode_manifest_includes_engine_tools(pm_update):
    """실제 opencode engine.manifest 가 핵심 엔진 도구를 포함한다."""
    opencode_manifest = (
        REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    )
    entries = pm_update.read_manifest(opencode_manifest)
    required = [
        ".project_manager/tools/board.py",
        ".project_manager/tools/pm_update.py",
        ".project_manager/wiki/pm_role.md",
    ]
    for entry in required:
        assert entry in entries, f"필수 엔진 항목 누락: {entry}"


# ── maybe_prompt_external_review dest_root 기반 동작 ─────────────────────

def test_maybe_prompt_external_review_uses_dest_local_conf(pm_update, tmp_path):
    """maybe_prompt_external_review 가 dest_root 기준 local.conf 를 읽는다.

    --target 모드에서 루트 local.conf 를 오염시키지 않음을 검증한다.
    dest_root 에 local.conf 가 없으면 (init 전) 아무 일도 일어나지 않아야 한다.
    """
    dest = tmp_path / "dest_instance"
    dest.mkdir()
    # local.conf 없음 → 조기 리턴, 루트 LOCAL_CONF 에 아무것도 쓰지 않아야 한다
    root_local_conf = pm_update.REPO / ".project_manager" / "local.conf"
    existed_before = root_local_conf.exists()

    pm_update.maybe_prompt_external_review(dest)

    # 루트 local.conf 존재 여부가 변해서는 안 된다
    assert root_local_conf.exists() == existed_before, (
        "--target 모드에서 루트 local.conf 를 건드려선 안 된다."
    )
    # dest 기준 local.conf 도 생성돼선 안 된다 (init 전)
    dest_local_conf = dest / ".project_manager" / "local.conf"
    assert not dest_local_conf.exists()


def test_maybe_prompt_external_review_skips_when_already_set(pm_update, tmp_path):
    """dest_root 의 local.conf 에 external_review_enabled 가 있으면 아무것도 하지 않는다."""
    dest = tmp_path / "dest_instance"
    local_conf = dest / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("external_review_enabled=false\n", encoding="utf-8")

    mtime_before = local_conf.stat().st_mtime
    pm_update.maybe_prompt_external_review(dest)
    mtime_after = local_conf.stat().st_mtime

    assert mtime_before == mtime_after, "이미 설정된 경우 local.conf 를 수정해선 안 된다."


def test_maybe_prompt_external_review_skips_non_tty(pm_update, tmp_path, monkeypatch):
    """비대화형(stdin isatty=False)이면 prompt 없이 넘어간다 — local.conf 변경 없음."""
    dest = tmp_path / "dest_instance"
    local_conf = dest / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    # external_review_enabled 미포함 → prompt 조건 성립, 그러나 비대화형이므로 skip
    local_conf.write_text("# 초기\n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin", type("FakeStdin", (), {"isatty": lambda self: False})())

    mtime_before = local_conf.stat().st_mtime
    pm_update.maybe_prompt_external_review(dest)
    mtime_after = local_conf.stat().st_mtime

    assert mtime_before == mtime_after, "비대화형에서 local.conf 를 수정해선 안 된다."


# ── T-0071: PM_NONINTERACTIVE 명시 신호 우선 (isatty 신뢰불가 함정 회피) ──

@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_maybe_prompt_skips_when_pm_noninteractive_truthy(
    pm_update, tmp_path, monkeypatch, val
):
    """PM_NONINTERACTIVE truthy → isatty=True(거짓 DEVNULL 보고)여도 묻지 않고 skip.

    input() 을 절대 안 부르고 local.conf 를 수정하지 않는다(Windows DEVNULL isatty 함정 회피).
    """
    dest = tmp_path / "dest_instance"
    local_conf = dest / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("# 초기\n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin", type("FakeStdin", (), {"isatty": lambda self: True})())
    monkeypatch.setenv("PM_NONINTERACTIVE", val)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("PM_NONINTERACTIVE 인데 input() 호출됨 — skip 위반."),
    )

    mtime_before = local_conf.stat().st_mtime
    pm_update.maybe_prompt_external_review(dest)
    mtime_after = local_conf.stat().st_mtime

    assert mtime_before == mtime_after, "PM_NONINTERACTIVE 면 local.conf 미수정."


def test_maybe_prompt_falsy_env_preserves_isatty_path(pm_update, tmp_path, monkeypatch):
    """PM_NONINTERACTIVE 빈/falsy + isatty=True + 'y' → 기록까지 진행(isatty 폴백 보존)."""
    dest = tmp_path / "dest_instance"
    local_conf = dest / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("# 초기\n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin", type("FakeStdin", (), {"isatty": lambda self: True})())
    monkeypatch.setenv("PM_NONINTERACTIVE", "0")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    pm_update.maybe_prompt_external_review(dest)

    assert "external_review_enabled=true" in local_conf.read_text(encoding="utf-8")


# ── 타깃 발견: templates/*/ 디렉토리 기반 ────────────────────────────────

def test_resolve_target_root_uses_directory_discovery(pm_update, tmp_path, monkeypatch):
    """resolve_target_root 가 KNOWN_TARGETS 없이 templates/*/ 존재로 타깃을 발견한다.

    REPO 를 monkeypatch 해 임시 디렉토리를 REPO 로 쓰고, 그 안에 templates/my_new_target/
    을 만들면 resolve_target_root("my_new_target")이 성공해야 한다.
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "templates" / "my_new_target").mkdir(parents=True)

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    result = pm_update.resolve_target_root("my_new_target")
    assert result == fake_repo / "templates" / "my_new_target"


def test_resolve_target_root_error_lists_available_targets(pm_update, tmp_path, monkeypatch):
    """존재하지 않는 타깃 요청 시 에러 메시지에 발견된 타깃 목록이 포함된다."""
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "templates" / "alpha").mkdir(parents=True)
    (fake_repo / "templates" / "beta").mkdir(parents=True)

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    with pytest.raises(FileNotFoundError, match="alpha"):
        pm_update.resolve_target_root("nonexistent")


# ── 보안: path traversal 방어 ────────────────────────────────────────────────

def test_resolve_target_root_rejects_dotdot(pm_update):
    """--target .. 은 path traversal 로 거부된다 (ValueError)."""
    with pytest.raises(ValueError, match="단일 path segment"):
        pm_update.resolve_target_root("..")


def test_resolve_target_root_rejects_dotdot_slash(pm_update):
    """--target ../x 는 path traversal 로 거부된다."""
    with pytest.raises(ValueError, match="단일 path segment"):
        pm_update.resolve_target_root("../x")


def test_resolve_target_root_rejects_slash_in_name(pm_update):
    """--target a/b 는 단일 segment 가 아니므로 거부된다."""
    with pytest.raises(ValueError, match="단일 path segment"):
        pm_update.resolve_target_root("a/b")


def test_resolve_target_root_rejects_empty(pm_update):
    """--target '' (빈 문자열)는 거부된다."""
    with pytest.raises(ValueError, match="단일 path segment"):
        pm_update.resolve_target_root("")


def test_resolve_target_root_rejects_absolute_path(pm_update):
    """--target /etc/passwd 처럼 절대 경로 형태도 거부된다."""
    with pytest.raises(ValueError, match="단일 path segment"):
        pm_update.resolve_target_root("/etc/passwd")


def test_main_target_dotdot_returns_error(pm_update, tmp_path):
    """main() 에서 --target .. 은 rc=1 을 반환하고 REPO 루트를 dest 로 삼지 않는다."""
    source = tmp_path / "upstream"
    source.mkdir()
    rc = pm_update.main([
        "--from", str(source),
        "--target", "..",
        "--dry-run",
    ])
    assert rc == 1


def test_main_target_dotdot_slash_x_returns_error(pm_update, tmp_path):
    """main() 에서 --target ../x 는 rc=1 을 반환한다."""
    source = tmp_path / "upstream"
    source.mkdir()
    rc = pm_update.main([
        "--from", str(source),
        "--target", "../x",
        "--dry-run",
    ])
    assert rc == 1


def test_main_target_nested_returns_error(pm_update, tmp_path):
    """main() 에서 --target a/b 는 rc=1 을 반환한다."""
    source = tmp_path / "upstream"
    source.mkdir()
    rc = pm_update.main([
        "--from", str(source),
        "--target", "a/b",
        "--dry-run",
    ])
    assert rc == 1


# ── missing 처리: source 에 없는 manifest 항목 ───────────────────────────────

def test_main_missing_manifest_entries_returns_rc2(pm_update, tmp_path, monkeypatch):
    """manifest 항목이 source 에 없으면 rc=2 를 반환한다 (잘못된 --from 감지).

    missing 이 있어도 changes=0 이면 '최신'으로 0 종료하던 기존 동작을 수정한다.
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    source.mkdir()

    # dest: fake_repo/templates/mytarget/
    dest = fake_repo / "templates" / "mytarget"
    dest.mkdir(parents=True)

    # source 에는 아무 파일도 없지만 manifest 엔 항목이 있다 → all missing
    manifest_in_source = source / ".project_manager" / "engine.manifest"
    manifest_in_source.parent.mkdir(parents=True)
    manifest_in_source.write_text(
        ".project_manager/tools/board.py\n"
        ".project_manager/tools/pm_update.py\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main([
        "--from", str(source),
        "--target", "mytarget",
        "--dry-run",
    ])

    assert rc == 2, f"missing 항목이 있는데 rc={rc} — rc=2 이어야 한다."


def test_main_no_missing_all_present_returns_rc0(pm_update, tmp_path, monkeypatch):
    """manifest 항목이 source 에 모두 있고 changes=0 이면 rc=0 (최신)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    sentinel_rel = ".project_manager/tools/__sentinel_present__.py"
    sentinel_content = "# sentinel\n"

    # source 와 dest 에 동일한 파일 준비 → no change, no missing
    _make_source(source, {sentinel_rel: sentinel_content})

    dest = fake_repo / "templates" / "mytarget"
    _make_source(dest, {sentinel_rel: sentinel_content})

    manifest_in_source = source / ".project_manager" / "engine.manifest"
    manifest_in_source.parent.mkdir(parents=True, exist_ok=True)
    manifest_in_source.write_text(sentinel_rel + "\n", encoding="utf-8")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main([
        "--from", str(source),
        "--target", "mytarget",
        "--dry-run",
    ])

    assert rc == 0, f"missing=0, changes=0 → rc=0 이어야 한다. 실제: rc={rc}"
