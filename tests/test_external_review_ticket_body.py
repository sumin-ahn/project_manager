"""external_review 게이트 티켓 본문 입력 회귀 (T-0695 · 라운드 사이드카 [[ADR-0090]]).

리뷰 입력은 **명세 파일 전문 + 역할별 마지막 산출 라운드 파일**이다. 입력 바이트 상한은 없다
(파일 선별이 대체 · 명세 과대는 lint 문제) — 이 파일은 상한 축을 소유하지 않는다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TOOL = TOOLS / "external_review.py"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.enabled": "true",
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load():
    spec = importlib.util.spec_from_file_location("external_review_ticket_body", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load()


@pytest.fixture
def rounds_seam(external):
    return external._load_ticket_rounds()


def _ticket(path: Path, *, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "id: T-9001\n"
        "title: 본문 입력 픽스처\n"
        "secret_frontmatter: do-not-send\n"
        "touches:\n"
        "- x.py\n"
        "estimate: medium\n"
        # T-0815 설계 근거 게이트(developer 시드 seam) 관심사 밖 — `done` + 설계 절(본문이
        # 소유)로 미리 해소한다. 본문 bytes 는 호출부가 준 그대로 둔다(선택 축이 그 값을 잰다).
        "design: done\n"
        "---\n"
        + body,
        encoding="utf-8",
    )
    return path


def _board_ticket(pm_home: Path, *, body: str, status: str = "claimed") -> Path:
    """실 board 레이아웃(`board/tickets/<status>/`)의 티켓 — 라운드 사이드카가 파생한다."""
    return _ticket(
        pm_home / ".project_manager" / "board" / "tickets" / status / "T-9001-body.md",
        body=body,
    )


def _round(pm_home: Path, ordinal: int, role: str, text: str) -> Path:
    path = (
        pm_home / ".project_manager" / "board" / "tickets" / "rounds" / "T-9001"
        / f"{ordinal:02d}-{role}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _round_text(role: str, marker: str) -> str:
    return f"## 라운드 ({role} · 2026-08-18)\n\n{marker}\n"


def _wire(external, monkeypatch, tmp_path, ticket: Path | None = None, *, conf=None):
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(external, "local_config",
                        lambda repo=None: {**_REVIEWER_TARGET, **(conf or {})})
    monkeypatch.setattr(external, "extract_diff", lambda *args, **kwargs: (DIFF, []))
    if ticket is not None:
        monkeypatch.setattr(
            external, "_find_ticket_file", lambda ticket_id, pm_home=None: ticket,
        )


def _stub_real_send(external, monkeypatch, tmp_path, prompts: list[str]):
    # 실 송신 경로는 서킷브레이커 진입 검사를 지난다 — 그 폭의 기준점(묶음 장부 통합 브랜치 ·
    # 그 merge-base)은 이 tmp REPO 에 없으므로 해소된 값을 그 자리에 넣는다. 기준점 해소
    # 자체의 거부는 전용 파일(`test_external_review_diff_cap.py`)이 실 git 으로 값 단언한다.
    monkeypatch.setattr(
        external, "cluster_integration_tip", lambda *a, **k: ("task/main", None))
    monkeypatch.setattr(
        external, "integration_anchor", lambda *a, **k: ("a" * 40, None))

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
    body = "# T-9001\n\n## 결정\n승인된 방어는 제거한다.\n"
    ticket = _ticket(tmp_path / "T-9001-body.md", body=body)
    _wire(external, monkeypatch, tmp_path, ticket)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "### 게이트 티켓 본문 (T-9001)" in captured.out
    assert "## 결정\n승인된 방어는 제거한다." in captured.out
    assert "secret_frontmatter: do-not-send" not in captured.out
    # 상한 표기는 없다 — 상한 자체가 사라졌다([[ADR-0090]]).
    assert f"ticket_body_bytes: {len(body.encode('utf-8'))} ·" in captured.out
    assert "(included / max)" not in captured.out


def test_real_send_prompt_includes_ticket_body_without_frontmatter(
    external, monkeypatch, tmp_path,
):
    """비-dry-run 전송 직전 조립이 run_review 입력에도 전체 본문을 싣는다(F-005)."""
    body = "## 결정\n실 전송 경로도 확정 결정을 받는다.\n"
    ticket = _ticket(tmp_path / "T-9001-real-send.md", body=body)
    _wire(
        external, monkeypatch, tmp_path, ticket,
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


def test_paths_and_gate_without_ticket_states_body_is_unassembled(
    external, monkeypatch, tmp_path, capsys,
):
    """`--paths --gate T-NNNN`(본문 미조립 형상) — [[T-0819]] 값 단언.

    `--ticket` 이 없으면 `prepare_ticket_body` 가 조립을 건너뛰어 `ticket_body_bytes` 줄
    자체가 안 찍힌다(형상 E). 미리보기가 침묵 대신 이 사실을 한 줄로 말해야 한다.
    """
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--gate", "T-9999", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "### 게이트 티켓 본문" not in out
    assert "ticket_body_bytes:" not in out, "본문 미조립 — 이 줄 자체가 없다"
    assert "ticket_body: 미조립" in out, "침묵 대신 미조립 사실을 값으로 말해야 한다"


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


def test_removed_confirm_fix_dry_run_is_rejected_before_loading_ticket_body(
    external, monkeypatch, tmp_path, capsys,
):
    body = "## 결정\n이 결정은 권위 입력이다.\n"
    ticket = _ticket(tmp_path / "T-9001-confirm.md", body=body)
    _wire(external, monkeypatch, tmp_path, ticket)

    with pytest.raises(SystemExit):
        external.main(["--ticket", "T-9001", "--confirm-fix", "--dry-run"])
    captured = capsys.readouterr()
    assert "unrecognized arguments: --confirm-fix" in captured.err
    assert "### 게이트 티켓 본문" not in captured.out


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
    for status in external._status_dirs():
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
        for status in external._status_dirs():
            (tickets / status).mkdir(parents=True, exist_ok=True)
        _ticket(tickets / "claimed" / "T-9001-cross.md", body=body)

    monkeypatch.setattr(external, "REPO", engine_home)
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(_REVIEWER_TARGET))
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


# ── [[ADR-0090]] — 입력 선별 (명세 전량 · 역할별 마지막 라운드 파일) ────────────────


SPEC_BODY = (
    "## 목표\n목표 내용\n\n"
    "## 인터페이스\n인터페이스 내용\n\n"
    "## 결정\n결정 내용\n\n"
    "## 설계\n"
    "- **경계 실측**: 기계 테스트 픽스처\n"
    "- **불변식**: 이 파일의 축 밖\n"
    "- **표면 상한**: 픽스처 1건\n"
    "- **테스트 전략**: 정상·실패 경로\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 항목\n\n"
    "## 참고\n참고 내용\n\n"
    "## 메모\n메모 내용\n\n"
    "## PM finding 판정\n```pm-review-disposition-v1\n판정 본문\n```\n"
)


def _multi_round_board(pm_home: Path, rounds: int) -> Path:
    """developer/code-reviewer 각 `rounds` 라운드를 가진 실 board 형상."""
    ticket = _board_ticket(pm_home, body=SPEC_BODY)
    ordinal = 0
    for index in range(rounds):
        for role in ("developer", "code-reviewer"):
            ordinal += 1
            _round(
                pm_home, ordinal, role,
                _round_text(role, f"라운드{index} {role} 본문"),
            )
    return ticket


def test_ticket_body_selection_includes_the_whole_spec(
    external, monkeypatch, tmp_path, capsys,
):
    """(a) 명세(권위 절 + PM 판정 블록)는 라운드 수와 무관하게 전량 실린다."""
    _multi_round_board(tmp_path, rounds=2)
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for heading, content in (
        ("## 목표", "목표 내용"),
        ("## 인터페이스", "인터페이스 내용"),
        ("## 결정", "결정 내용"),
        ("## 설계", "**경계 실측**: 기계 테스트 픽스처"),
        ("## 완료 조건 (Definition of Done)", "- [ ] 항목"),
        ("## 참고", "참고 내용"),
        ("## 메모", "메모 내용"),
        ("## PM finding 판정", "판정 본문"),
    ):
        assert heading in out
        assert content in out

    # 선별 헤더 한 줄의 실제 값(값 존재가 아니라 값 자체를 단언).
    assert (
        "id=T-9001 · title=본문 입력 픽스처 · touches=[x.py] · estimate=medium"
    ) in out


def test_ticket_body_selection_keeps_only_the_last_round_per_role(
    external, monkeypatch, tmp_path, capsys,
):
    """(b) 역할별 마지막 라운드 파일만 실리고 앞 라운드는 생략 한 줄로 대체된다."""
    _multi_round_board(tmp_path, rounds=3)
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    out = capsys.readouterr().out

    assert "라운드2 developer 본문" in out
    assert "라운드2 code-reviewer 본문" in out
    assert "--- 05-developer ---" in out
    assert "--- 06-code-reviewer ---" in out
    for index in (0, 1):
        assert f"라운드{index} developer 본문" not in out
        assert f"라운드{index} code-reviewer 본문" not in out
    assert "(생략한 라운드 4개" in out
    assert "생략 라운드: 4개" in out


def test_ticket_body_selection_skips_rounds_without_output(
    external, monkeypatch, rounds_seam, tmp_path, capsys,
):
    """(c) 시드 그대로인(산출 없음) 라운드는 리뷰 입력에 싣지 않는다."""
    ticket = _board_ticket(tmp_path, body=SPEC_BODY)
    _round(tmp_path, 1, "developer", _round_text("developer", "1라운드 실산출"))
    _round(
        tmp_path, 2, "developer",
        rounds_seam.render_round_seed(
            "developer", ticket.read_text(encoding="utf-8"), today="2026-08-18",
        ),
    )
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1라운드 실산출" in out
    assert "--- 02-developer ---" not in out
    # 생략은 접힌 **산출**의 수다 — 산출 없는 라운드는 접힐 것이 없어 세지 않는다.
    assert "생략 라운드: 0개" in out
    assert "(생략한 라운드" not in out


def test_ticket_body_selection_without_rounds_is_byte_identical(
    external, monkeypatch, tmp_path, capsys,
):
    """(d) 라운드 0개 티켓의 입력은 선별 도입 전후로 동일하다 — 원문 그대로."""
    _board_ticket(tmp_path, body=SPEC_BODY)
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"ticket_body_bytes: {len(SPEC_BODY.encode('utf-8'))} ·" in out
    assert "생략 라운드: 0개" in out
    assert "id=T-9001 · title=" not in out       # 선별이 없으면 요약 헤더도 없다.
    assert "(생략한 라운드" not in out


def test_select_ticket_body_for_review_is_pure_and_deterministic(
    external, rounds_seam,
):
    """selection 함수 단위 — 순수 함수로 같은 입력에 같은 결과(부수효과 없음)."""
    rounds = [
        rounds_seam.Round(
            ordinal=ordinal, role="code-reviewer",
            path=Path(rounds_seam.round_filename(ordinal, "code-reviewer")),
            text=_round_text("code-reviewer", f"라운드{ordinal} 본문"), pending=False,
        )
        for ordinal in (1, 2)
    ]
    first = external._select_ticket_body_for_review(SPEC_BODY, rounds)
    second = external._select_ticket_body_for_review(SPEC_BODY, rounds)
    assert first == second
    assert first.omitted_rounds == 1
    assert "라운드1 본문" not in first.text
    assert "라운드2 본문" in first.text
    assert first.text.startswith(SPEC_BODY)


def test_malformed_rounds_directory_fails_loud_without_sending(
    external, monkeypatch, tmp_path, capsys,
):
    """(e) 라운드 디렉터리 규약 위반은 조용히 지나가지 않는다 — 전송 없이 loud 하게 멈춘다."""
    _board_ticket(tmp_path, body=SPEC_BODY)
    stray = (
        tmp_path / ".project_manager" / "board" / "tickets" / "rounds" / "T-9001"
        / "developer.md"
    )
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("이름 문법 위반\n", encoding="utf-8")
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", "T-9001", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "라운드 사이드카가 손상" in captured.err
    assert "프롬프트 미리보기" not in captured.out


def test_byte_cap_surface_is_gone(external):
    """상한 상수·CLI·conf 키는 남기지 않는다(파일 선별이 대체 · 사용자 2026-08-18)."""
    assert not hasattr(external, "REVIEW_TICKET_BODY_MAX_BYTES_KEY")
    assert not hasattr(external, "DEFAULT_REVIEW_TICKET_BODY_MAX_BYTES")
    assert not hasattr(external, "_ticket_body_max_bytes")
    assert not hasattr(external, "_positive_bytes_arg")
    source = TOOL.read_text(encoding="utf-8")
    assert "--ticket-body-max" not in source
    assert "review_ticket_body_max_bytes" not in source
