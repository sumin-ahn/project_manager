"""external_review 게이트 티켓 본문 입력 회귀 (T-0695)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / ".project_manager" / "tools" / "external_review.py"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def _load():
    spec = importlib.util.spec_from_file_location("external_review_ticket_body", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load()


def _ticket(path: Path, *, body: str) -> Path:
    path.write_text(
        "---\n"
        "id: T-9001\n"
        "secret_frontmatter: do-not-send\n"
        "touches:\n"
        "- x.py\n"
        "---\n"
        + body,
        encoding="utf-8",
    )
    return path


def _wire(external, monkeypatch, tmp_path, ticket: Path | None = None, *, conf=None):
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(conf or {}))
    monkeypatch.setattr(external, "extract_diff", lambda *args, **kwargs: (DIFF, []))
    if ticket is not None:
        monkeypatch.setattr(
            external, "_find_ticket_file", lambda ticket_id, pm_home=None: ticket,
        )


def _stub_real_send(external, monkeypatch, tmp_path, prompts: list[str]):
    def _workspace(*args, **kwargs):
        root = tmp_path / "reviewer"
        tree = root / "tree"
        home = root / "home"
        tree.mkdir(parents=True)
        home.mkdir()
        return external.ReviewerWorkspace(
            root=root, tree=tree, home=home,
            files=1, skipped_unsafe=0, git_repo=True,
        )

    def _run_review(prompt, *args, **kwargs):
        prompts.append(prompt)
        return {
            "reviewer": "fixture", "ok": True, "output": "판정: 통과",
            "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
            "failed": False, "started": True,
            "any_must_fix": False, "all_pass": True,
        }

    monkeypatch.setattr(external, "create_reviewer_workspace", _workspace)
    monkeypatch.setattr(external, "run_review", _run_review)
    # 이 파일은 프롬프트 입력 축(T-0695)을 소유한다. 산출 회수(T-0696)는 실 board 왕복이 필요한
    # 별도 축이라 tests/test_external_review_ticket_harvest.py가 소유하고 여기서는 격리한다.
    monkeypatch.setattr(
        external, "_harvest_external_review_section", lambda *_a, **_k: None,
    )


def test_ticket_dry_run_prompt_includes_full_body_without_frontmatter(
    external, monkeypatch, tmp_path, capsys,
):
    body = (
        "# T-9001\n\n## 결정\n승인된 방어는 제거한다.\n\n"
        "<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충\n그대로 보존\n"
        "<!-- pm-ticket-section:end role=developer -->\n"
    )
    ticket = _ticket(tmp_path / "T-9001-body.md", body=body)
    _wire(external, monkeypatch, tmp_path, ticket)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "### 게이트 티켓 본문 (T-9001)" in captured.out
    assert "## 결정\n승인된 방어는 제거한다." in captured.out
    assert "<!-- pm-ticket-section:start role=developer -->" in captured.out
    assert "secret_frontmatter: do-not-send" not in captured.out
    assert f"ticket_body_bytes: {len(body.encode('utf-8'))} / 65536" in captured.out


def test_real_send_prompt_includes_ticket_body_without_frontmatter(
    external, monkeypatch, tmp_path,
):
    """비-dry-run 전송 직전 조립이 run_review 입력에도 전체 본문을 싣는다(F-005)."""
    body = "## 결정\n실 전송 경로도 확정 결정을 받는다.\n"
    ticket = _ticket(tmp_path / "T-9001-real-send.md", body=body)
    _wire(
        external, monkeypatch, tmp_path, ticket,
        conf={"additional_reviewer_enabled": "true"},
    )
    prompts: list[str] = []
    _stub_real_send(external, monkeypatch, tmp_path, prompts)

    assert external.main([
        "--ticket", "T-9001", "--output-dir", str(tmp_path / "raw"),
    ]) == 0
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "### 게이트 티켓 본문 (T-9001)" in prompt
    assert "## 결정\n실 전송 경로도 확정 결정을 받는다." in prompt
    assert "secret_frontmatter: do-not-send" not in prompt


def test_paths_only_prompt_does_not_include_ticket_body(
    external, monkeypatch, tmp_path, capsys,
):
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--no-gate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "### 게이트 티켓 본문" not in out
    assert "ticket_body_bytes:" not in out


@pytest.mark.parametrize(
    "accounting_args",
    (["--gate", "T-7777"], ["--no-gate"]),
    ids=("explicit-gate-override", "no-gate"),
)
def test_ticket_and_paths_header_uses_body_source_ticket_id(
    external, monkeypatch, tmp_path, capsys, accounting_args,
):
    body = "## 결정\n본문 출처는 T-9001이다.\n"
    ticket = _ticket(tmp_path / "T-9001-paths.md", body=body)
    _wire(external, monkeypatch, tmp_path, ticket)

    assert external.main([
        "--ticket", "T-9001", "--paths", "x.py", *accounting_args, "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "### 게이트 티켓 본문 (T-9001)" in out
    assert "### 게이트 티켓 본문 (T-7777)" not in out
    assert "본문 출처는 T-9001이다." in out


def test_ticket_and_paths_missing_body_warns_and_keeps_recovery_channel(
    external, monkeypatch, tmp_path, capsys,
):
    _wire(external, monkeypatch, tmp_path)

    assert external.main([
        "--ticket", "T-missing", "--paths", "x.py", "--dry-run",
    ]) == 0
    captured = capsys.readouterr()
    assert "본문 없이 계속합니다" in captured.err
    assert "### 게이트 티켓 본문" not in captured.out


def test_plain_list_ticket_scope_is_explicitly_a_test_fixture_seam(
    external, monkeypatch, tmp_path, capsys,
):
    _wire(external, monkeypatch, tmp_path)
    monkeypatch.setattr(
        external, "parse_ticket_touches", lambda ticket_id, pm_home=None: ["x.py"],
    )

    assert external.main(["--ticket", "T-fixture", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "테스트 fixture가 ticket touches seam만 주입" in captured.err
    assert "주입된 ticket scope" not in captured.err
    assert "### 게이트 티켓 본문" not in captured.out


def test_ticket_body_over_limit_fails_loud_without_truncation(
    external, monkeypatch, tmp_path, capsys,
):
    body = "## 결정\n" + "절대-자르지-않음" * 12 + "\nEND-OF-TICKET\n"
    ticket = _ticket(tmp_path / "T-9001-large.md", body=body)
    _wire(
        external, monkeypatch, tmp_path, ticket,
        conf={"review_ticket_body_max_bytes": "16"},
    )

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "상한을 초과" in captured.err
    assert "본문을 자르지 않았고 외부로 전송하지 않습니다" in captured.err
    assert "--ticket-body-max <bytes>" in captured.err
    assert "프롬프트 미리보기" not in captured.out
    assert "END-OF-TICKET" not in captured.out

    assert external.main([
        "--ticket", "T-9001", "--ticket-body-max", "4096", "--dry-run",
    ]) == 0
    assert "END-OF-TICKET" in capsys.readouterr().out


def test_ticket_body_exactly_at_limit_is_included(
    external, monkeypatch, tmp_path, capsys,
):
    body = "## 결정\n정확 경계 통과\n"
    ticket = _ticket(tmp_path / "T-9001-exact-limit.md", body=body)
    body_bytes = len(body.encode("utf-8"))
    _wire(
        external, monkeypatch, tmp_path, ticket,
        conf={"review_ticket_body_max_bytes": str(body_bytes)},
    )

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"ticket_body_bytes: {body_bytes} / {body_bytes}" in out
    assert "정확 경계 통과" in out


def test_confirm_fix_prompt_keeps_the_same_ticket_body(
    external, monkeypatch, tmp_path, capsys,
):
    body = "## 결정\n확인 라운드에서도 이 결정을 권위로 삼는다.\n"
    ticket = _ticket(tmp_path / "T-9001-confirm.md", body=body)
    _wire(external, monkeypatch, tmp_path, ticket)

    assert external.main([
        "--ticket", "T-9001", "--confirm-fix", "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "### 이 라운드의 임무 (확인 전용 · 필수)" in out
    assert "### 게이트 티켓 본문 (T-9001)" in out
    assert "확인 라운드에서도 이 결정을 권위로 삼는다." in out


def test_review_context_always_declares_ticket_decisions_authoritative(
    external, monkeypatch, tmp_path,
):
    phrase = "티켓 §결정·§설계·PM 판정 블록은 **권위 있는 확정 사항**"
    assert phrase in external._DEFAULT_CONTEXT_HEADER

    overlay = tmp_path / "review_context.local.md"
    overlay.write_text("# 프로젝트 전용 맥락\n", encoding="utf-8")
    monkeypatch.setattr(external, "REVIEW_CONTEXT_FILE", overlay)
    loaded = external._load_review_context()
    assert "# 프로젝트 전용 맥락" in loaded
    assert phrase in loaded
    assert "design-proposal" in loaded
    assert "must-fix 로 내지 마라" in loaded


def test_ticket_body_real_board_lookup_is_exact_across_status_dirs(
    external, tmp_path,
):
    tickets = tmp_path / ".project_manager" / "wiki" / "tickets"
    for status in external.STATUS_DIRS:
        (tickets / status).mkdir(parents=True, exist_ok=True)
    exact = tickets / "claimed" / "T-9001-exact.md"
    _ticket(exact, body="## 결정\n정확 티켓 본문\n")
    (tickets / "open" / "T-9001-001-prefix.md").write_text(
        "---\nid: T-9001-001\ntouches:\n- wrong.py\n---\n## 결정\n충돌 티켓 본문\n",
        encoding="utf-8",
    )

    body = external._load_ticket_body("T-9001", pm_home=tmp_path)
    assert "정확 티켓 본문" in body
    assert "충돌 티켓 본문" not in body


def test_ticket_and_cross_repo_paths_load_body_from_selected_pm_home(
    external, monkeypatch, tmp_path, capsys,
):
    engine_home = tmp_path / "engine"
    selected_home = tmp_path / "selected"
    for home, body in (
        (engine_home, "## 결정\n잘못된 엔진 홈 본문\n"),
        (selected_home, "## 결정\n선택된 PM 홈 본문\n"),
    ):
        tickets = home / ".project_manager" / "wiki" / "tickets"
        for status in external.STATUS_DIRS:
            (tickets / status).mkdir(parents=True, exist_ok=True)
        _ticket(tickets / "claimed" / "T-9001-cross.md", body=body)

    monkeypatch.setattr(external, "REPO", engine_home)
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo",
        lambda anchor, **kwargs: (
            engine_home if Path(anchor).resolve() == engine_home.resolve() else selected_home
        ),
    )
    monkeypatch.setattr(external, "_resolve_diff_root", lambda *args, **kwargs: selected_home)
    monkeypatch.setattr(
        external, "_normalize_review_paths", lambda *args, **kwargs: ("x.py",),
    )
    monkeypatch.setattr(external, "extract_diff", lambda *args, **kwargs: (DIFF, []))

    assert external.main([
        "--ticket", "T-9001", "--paths", str(selected_home / "x.py"), "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "선택된 PM 홈 본문" in out
    assert "잘못된 엔진 홈 본문" not in out


def test_unterminated_frontmatter_fails_loud_for_body_and_touches(
    external, monkeypatch, tmp_path,
):
    broken = tmp_path / "T-9001-broken.md"
    broken.write_text("---\nid: T-9001\ntouches:\n- x.py\n", encoding="utf-8")
    monkeypatch.setattr(
        external, "_find_ticket_file", lambda ticket_id, pm_home=None: broken,
    )

    with pytest.raises(external.AnchorResolutionError, match="frontmatter가 닫히지"):
        external._load_ticket_body("T-9001")
    with pytest.raises(external.AnchorResolutionError, match="frontmatter가 닫히지"):
        external._parse_touches_from_file(broken)
