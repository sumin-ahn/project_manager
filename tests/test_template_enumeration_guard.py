"""출하 템플릿 열거와 전체 전파 안내의 재발 방지 가드 (T-0469)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TARGET_PATTERN = re.compile(r"`templates/([a-z0-9_]+)/`")
# 타깃 열거를 담는 루트 진입문서 전부 — CLAUDE.md 만 가드하면 README 열거가 재발 사각
# (T-0469 내부 reviewer must-fix). 새 진입문서에 열거를 추가하면 여기에도 등재한다.
ROOT_ENTRY_DOCS = (REPO / "CLAUDE.md", REPO / "README.md")
ADOPT_DOC = REPO / "ADOPT.md"
# 템플릿 디렉토리명 → 채택 가이드의 하니스 표기 (그 외는 동명).
_DIR_TO_HARNESS = {"claude_code": "claude"}
TEMPLATE_READMES = tuple(sorted((REPO / "templates").glob("*/README.md")))


def _documented_targets(entry_doc: Path) -> set[str]:
    return set(TARGET_PATTERN.findall(entry_doc.read_text(encoding="utf-8")))


def _actual_targets(templates_dir: Path) -> set[str]:
    # 런타임 discover_target_names 와 동일 규칙 — 숨김 디렉토리는 타깃이 아니다(일관성·codex R3).
    return {path.name for path in templates_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")}


@pytest.mark.parametrize("entry_doc", ROOT_ENTRY_DOCS, ids=lambda p: p.name)
def test_root_entry_doc_enumerates_exactly_existing_template_targets(entry_doc: Path):
    """루트 진입문서의 출하 타깃 목록은 `templates/`와 정확히 일치해야 한다."""
    assert _documented_targets(entry_doc) == _actual_targets(REPO / "templates")


def test_adopt_doc_mentions_every_target_harness():
    """채택 가이드(ADOPT.md)는 존재하는 모든 타깃의 하니스를 언급해야 한다.

    v1.4.0 codex 편입 때 ADOPT.md 가 2-하니스 열거로 남았던 재발 클래스 — 타깃
    디렉토리 집합에서 하니스 표기를 파생해 새 타깃도 자동 가드된다."""
    text = ADOPT_DOC.read_text(encoding="utf-8")
    for target in _actual_targets(REPO / "templates"):
        harness = _DIR_TO_HARNESS.get(target, target)
        assert harness in text, f"ADOPT.md 에 하니스 '{harness}'({target}) 언급이 없다"


def test_template_entry_docs_direct_maintainers_to_all_targets():
    """개별 타깃 README도 전체 엔진 변경에 단일 --target 안내를 하지 않는다."""
    assert TEMPLATE_READMES, "templates/*/README.md 진입문서가 없다"
    for entry_doc in TEMPLATE_READMES:
        text = entry_doc.read_text(encoding="utf-8")
        assert "--all-targets" in text, f"{entry_doc.relative_to(REPO)}에 전체 전파 안내가 없다"


def test_guard_is_sensitive_to_a_new_undocumented_fourth_target(tmp_path: Path):
    """가짜 네 번째 디렉토리를 추가하면 문서 집합 불일치가 실제로 드러난다."""
    templates_dir = tmp_path / "templates"
    for name in ("claude_code", "codex", "opencode", "fourth_harness"):
        (templates_dir / name).mkdir(parents=True)
    entry_doc = tmp_path / "CLAUDE.md"
    entry_doc.write_text(
        "`templates/claude_code/`·`templates/codex/`·`templates/opencode/`\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError):
        assert _documented_targets(entry_doc) == _actual_targets(templates_dir)


def _load_pm_update():
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    spec = importlib.util.spec_from_file_location("pm_update_template_enumeration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_all_targets_discovers_a_new_directory_at_runtime(tmp_path: Path, monkeypatch):
    """`--all-targets`는 고정 목록 없이 새 디렉터리까지 **전부** 자식 호출로 넘긴다.

    파일 부재 확인만으로는 타깃 누락도 통과라(codex 게이트 suggestion) 자식 argv 를 직접
    기록해 두 타깃 모두 처리됐음을 검증한다."""
    pm_update = _load_pm_update()
    fake_repo = tmp_path / "repo"
    source = tmp_path / "source"
    rel = ".project_manager/tools/sentinel.py"
    (source / rel).parent.mkdir(parents=True)
    (source / rel).write_text("# sentinel\n", encoding="utf-8")
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.write_text(f"{rel}\n", encoding="utf-8")
    for name in ("alpha", "fourth_harness"):
        (fake_repo / "templates" / name).mkdir(parents=True)

    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    real_main = pm_update.main
    child_calls: list[list[str]] = []
    monkeypatch.setattr(
        pm_update, "main", lambda argv: child_calls.append(list(argv)) or 0
    )

    assert real_main(["--from", str(source), "--all-targets", "--dry-run"]) == 0
    assert child_calls == [
        ["--from", str(source), "--target", "alpha", "--dry-run"],
        ["--from", str(source), "--target", "fourth_harness", "--dry-run"],
    ]


@pytest.mark.parametrize(
    "extra", [["--count-only"], ["--log"], ["--count-only", "--log"]]
)
def test_all_targets_rejects_changes_only_options(
    tmp_path: Path, monkeypatch, capsys, extra: list[str]
):
    """`--all-targets` + `--changes 전용 옵션` 조합은 실 동기화 대신 명확 에러다.

    오사용 검증이 --all-targets 분기보다 뒤면 두 옵션이 자식 argv 에 안 실린 채 조용히
    무시되고 실 동기화가 돈다(T-0469 codex 게이트 must-fix — 검증 선행이 계약)."""
    pm_update = _load_pm_update()
    fake_repo = tmp_path / "repo"
    (fake_repo / "templates" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    child_calls: list[list[str]] = []
    real_main = pm_update.main
    monkeypatch.setattr(
        pm_update, "main", lambda argv: child_calls.append(list(argv)) or 0
    )

    assert real_main(["--all-targets", *extra]) == 1
    assert child_calls == []  # 자식 동기화 호출 0 — 오사용이 실 sync 로 새지 않는다.
    assert "--changes 전용" in capsys.readouterr().err


def test_all_targets_rejects_changes_mode(tmp_path: Path, monkeypatch, capsys):
    """`--all-targets --changes` 는 모순 조합 — 명확 에러."""
    pm_update = _load_pm_update()
    fake_repo = tmp_path / "repo"
    (fake_repo / "templates" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    assert pm_update.main(["--all-targets", "--changes"]) == 1
    assert "--changes 와 함께 쓸 수 없다" in capsys.readouterr().err
