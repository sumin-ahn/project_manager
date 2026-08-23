"""`changelog material --since` — 완료 티켓 본문에서 뽑은 릴리즈 노트 재료.

여기서 지키는 성질은 다섯이다.
  (1) 구간 판정의 기준은 **코드 체크아웃에서 해소한 rev 시각**이고, 경계(같은 시각)는 밖이다.
  (2) 블록은 선언된 필드를 **값으로** 싣는다 — 티켓 ID·제목·완료 시각·분류 후보·채택자 영향
      인용·근거 절(목표·결정·완료 조건).
  (3) board 는 **읽기 전용**이다 — 실행 뒤 티켓 파일의 내용도 mtime 도 바뀌지 않는다.
  (4) 재료 0건은 오류가 아니다 — 빈 stdout + rc 0. 구간 밖 완료만 그 빈 손을 만든다.
  (5) 코드 체크아웃·rev 미해소와 **손상된 done 티켓**(완료 시각·id·제목·근거 절 결손·판독 불가)은
      조용한 누락이 아니라 그 경로를 지목한 rc≠0 이다.

hermetic 패턴은 `test_cluster_review_round.py`(자기-정박 PM 홈 + 실 git)를 따른다.
"""
from __future__ import annotations

import datetime
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "changelog-material",
    "GIT_AUTHOR_EMAIL": "changelog-material@test.invalid",
    "GIT_COMMITTER_NAME": "changelog-material",
    "GIT_COMMITTER_EMAIL": "changelog-material@test.invalid",
}

_TAG = "v-fixture"
# 픽스처 시각은 실행 시점에서 만든다 — 소스에 날짜 리터럴을 박지 않는다(출하 위생).
_FIXTURE_DAY = datetime.date.today().isoformat()


def _load_tool(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_tool("pm_delegate", "pm_delegate_changelog_material")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _done_ticket_text(
    ticket: str, *, title: str, completed_at: str,
    goal: str, decision: str, dod: str,
) -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        f"title: {title}\n"
        "status: done\n"
        f"created: '{_FIXTURE_DAY}'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        f"completed_at: '{completed_at}'\n"
        "depends_on: []\n"
        "blocks: []\n"
        f"touches:\n- {ticket.lower()}.py\n"
        "estimate: medium\n"
        "design: done\n"
        "tags: []\n"
        "---\n"
        f"# {ticket} — {title}\n\n"
        f"## 목표\n{goal}\n\n"
        f"## 인터페이스\n인터페이스 한 줄.\n\n"
        f"## 결정\n{decision}\n\n"
        f"## 완료 조건 (Definition of Done)\n{dod}\n\n"
        "## 참고\n참고 한 줄.\n"
    )


def _fixture_board(pd, home: Path):
    board = pd._load_module_from_path(
        home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = home
    board.LOCAL_DIR = home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    return board


def _stamp(instant: datetime.datetime) -> str:
    return instant.isoformat()


@pytest.fixture
def material_env(tmp_path, pd, monkeypatch):
    """PM 홈(board 데이터) + 코드 체크아웃(태그 보유) — 두 좌표가 분리된 실 형상."""
    home = tmp_path / "home"
    pm_tools = home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    done = home / ".project_manager" / "wiki" / "tickets" / "done"
    done.mkdir(parents=True)
    (home / ".project_manager" / ".local").mkdir(parents=True)

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert _git(checkout, "init", "-q", "-b", "main").returncode == 0
    (checkout / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(checkout, "add", "seed.txt").returncode == 0
    assert _git(checkout, "commit", "-qm", "release seed").returncode == 0
    assert _git(checkout, "tag", "-a", _TAG, "-m", "release seed").returncode == 0
    since = datetime.datetime.fromisoformat(
        _git(checkout, "log", "-1", "--format=%cI", _TAG).stdout.strip())

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, home))
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {
        "upstream.path": str(checkout),
    })
    return home, done, checkout, since


def _seed_three_done(done: Path, since: datetime.datetime) -> None:
    """구간 밖 1 · 구간 안 2 — 경계(=태그 시각)는 밖이다."""
    (done / "T-8101-before.md").write_text(_done_ticket_text(
        "T-8101", title="구간 밖 완료",
        completed_at=_stamp(since - datetime.timedelta(hours=1)),
        goal="구간 밖 목표.", decision="구간 밖 결정.",
        dod="- [x] 구간 밖 완료 조건",
    ), encoding="utf-8", newline="\n")
    (done / "T-8102-added.md").write_text(_done_ticket_text(
        "T-8102", title="묶음 장부 신설",
        completed_at=_stamp(since + datetime.timedelta(hours=1)),
        goal="운영 단위 장부를 신설한다.",
        decision="채택자 board 스키마는 자동 부여로 마이그레이션한다.",
        dod="- [x] 장부 생성·귀속·lint",
    ), encoding="utf-8", newline="\n")
    (done / "T-8103-removed.md").write_text(_done_ticket_text(
        "T-8103", title="면제 경로 폐지",
        completed_at=_stamp(since + datetime.timedelta(hours=2)),
        goal="면제 값을 폐지하고 상태만 남긴다.",
        decision="옛 값은 거부한다.",
        dod="- [x] 폐지 값 거부",
    ), encoding="utf-8", newline="\n")


def _tree_fingerprint(root: Path) -> dict[str, tuple[bytes, int]]:
    # 픽스처 board 는 고정 깊이(최대 4)라 재귀 열거 없이 깊이별 glob 으로 전수 센다.
    found = [
        path
        for depth in ("*", "*/*", "*/*/*", "*/*/*/*")
        for path in root.glob(depth)
        if path.is_file()
    ]
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(found)
    }


# ════════════════════════════════════════════════════════════════════════
# 구간 판정 — 코드 체크아웃 rev 시각
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_material_selects_only_completions_after_the_rev_instant(
        pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    out = capsys.readouterr().out
    assert out.count("## T-") == 2
    assert "T-8102" in out and "T-8103" in out
    assert "T-8101" not in out


@requires_git
def test_material_excludes_a_completion_at_the_rev_instant_itself(
        pd, material_env, capsys):
    """경계는 밖이다 — 태그 커밋과 같은 시각의 완료는 그 릴리즈 재료가 아니다."""
    _home, done, _checkout, since = material_env
    (done / "T-8104-boundary.md").write_text(_done_ticket_text(
        "T-8104", title="경계 완료", completed_at=_stamp(since),
        goal="경계 목표.", decision="경계 결정.", dod="- [x] 경계",
    ), encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    assert capsys.readouterr().out == ""


@requires_git
def test_material_orders_blocks_by_completion_instant(pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    out = capsys.readouterr().out
    assert out.index("## T-8102") < out.index("## T-8103")


@requires_git
def test_material_refuses_a_done_ticket_without_a_readable_completion(
        pd, material_env, capsys):
    """완료 시각이 없는 done 티켓은 건너뛰지 않는다 — 그 경로를 찍고 rc 1 이다."""
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)
    nulled = done / "T-8105-nulled.md"
    nulled.write_text(_done_ticket_text(
        "T-8105", title="완료 시각 없음", completed_at=_stamp(since),
        goal="목표.", decision="결정.", dod="- [x] 항목",
    ).replace(f"completed_at: '{_stamp(since)}'", "completed_at: null"),
        encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "시각을 읽지 못했습니다" in captured.err
    assert str(nulled) in captured.err


@requires_git
def test_material_refuses_a_done_ticket_that_cannot_be_parsed(
        pd, material_env, capsys):
    """읽지 못한 티켓을 건너뛰면 그 티켓의 재료가 조용히 사라진다 — 경로를 찍고 rc 1."""
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)
    broken = done / "T-8108-broken.md"
    broken.write_text("---\nid: T-8108\ntitle: 열린 frontmatter\n",
                      encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(broken) in captured.err


@requires_git
def test_material_refuses_a_done_ticket_missing_a_source_section(
        pd, material_env, capsys):
    """근거 절이 사라진 완료 티켓은 빈 값으로 싣지 않는다 — 절 이름과 경로를 찍고 rc 1."""
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)
    sectionless = done / "T-8109-sectionless.md"
    full = _done_ticket_text(
        "T-8109", title="근거 절 결손",
        completed_at=_stamp(since + datetime.timedelta(hours=3)),
        goal="목표.", decision="결정 본문.", dod="- [x] 항목",
    )
    sectionless.write_text(full.replace("## 결정\n결정 본문.\n\n", ""),
                           encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "`## 결정` 절이 없거나 비어 있습니다" in captured.err
    assert str(sectionless) in captured.err


@requires_git
def test_material_refuses_a_done_ticket_without_a_title(pd, material_env, capsys):
    """제목이 비면 파일명으로 채우지 않는다 — 그 자리를 찍고 rc 1."""
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)
    untitled = done / "T-8110-untitled.md"
    untitled.write_text(_done_ticket_text(
        "T-8110", title="제목 결손",
        completed_at=_stamp(since + datetime.timedelta(hours=4)),
        goal="목표.", decision="결정.", dod="- [x] 항목",
    ).replace("title: 제목 결손\n", "title: ''\n"),
        encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "`title` 가 비어 있습니다" in captured.err
    assert str(untitled) in captured.err


# ════════════════════════════════════════════════════════════════════════
# 블록 필드 — 값 단언
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_material_block_carries_id_title_instant_and_source_sections(
        pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    out = capsys.readouterr().out
    assert "## T-8102 — 묶음 장부 신설" in out
    assert f"- 완료: {_stamp(since + datetime.timedelta(hours=1))}" in out
    assert "- 근거 · 목표:\n  운영 단위 장부를 신설한다." in out
    assert "- 근거 · 결정:\n  채택자 board 스키마는 자동 부여로 마이그레이션한다." in out
    assert "- 근거 · 완료 조건:\n  - [x] 장부 생성·귀속·lint" in out


@requires_git
def test_material_block_lists_category_candidates_without_deciding(
        pd, material_env, capsys):
    """분류는 **후보**다 — 신호가 겹치면 겹친 대로 전부 싣는다."""
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    blocks = capsys.readouterr().out.split("## T-")
    added = next(block for block in blocks if block.startswith("8102"))
    removed = next(block for block in blocks if block.startswith("8103"))
    assert "- 분류 후보: Added" in added
    assert "- 분류 후보: Removed" in removed


@requires_git
def test_material_block_quotes_adopter_impact_lines(pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    out = capsys.readouterr().out
    assert ("- 채택자 영향 인용:\n"
            "  - 채택자 board 스키마는 자동 부여로 마이그레이션한다.") in out


@requires_git
def test_material_block_marks_absent_fields_instead_of_omitting_them(
        pd, material_env, capsys):
    """신호 0 인 티켓도 필드를 비우지 않는다 — 없음이 값이다."""
    _home, done, _checkout, since = material_env
    (done / "T-8106-quiet.md").write_text(_done_ticket_text(
        "T-8106", title="조용한 티켓",
        completed_at=_stamp(since + datetime.timedelta(hours=1)),
        goal="문장 하나.", decision="문장 둘.", dod="- [x] 문장 셋",
    ), encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    out = capsys.readouterr().out
    assert "- 분류 후보: (없음)" in out
    assert "- 채택자 영향 인용: (없음)" in out


# ════════════════════════════════════════════════════════════════════════
# board 읽기 전용 · 빈 손 · fail-loud
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_material_leaves_the_board_untouched(pd, material_env, capsys):
    home, done, _checkout, since = material_env
    _seed_three_done(done, since)
    board_tree = home / ".project_manager" / "wiki"
    before = _tree_fingerprint(board_tree)

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0
    capsys.readouterr()

    assert _tree_fingerprint(board_tree) == before


@requires_git
def test_material_is_empty_and_successful_when_no_ticket_completed_in_the_span(
        pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    (done / "T-8107-before.md").write_text(_done_ticket_text(
        "T-8107", title="구간 밖 완료",
        completed_at=_stamp(since - datetime.timedelta(days=1)),
        goal="목표.", decision="결정.", dod="- [x] 항목",
    ), encoding="utf-8", newline="\n")

    assert pd.main(["changelog", "material", "--since", _TAG]) == 0

    captured = capsys.readouterr()
    assert captured.out == "" and "오류" not in captured.err


@requires_git
def test_material_refuses_an_unresolvable_rev(pd, material_env, capsys):
    _home, done, _checkout, since = material_env
    _seed_three_done(done, since)

    assert pd.main(["changelog", "material", "--since", "v-없는태그"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rev 를 해소하지 못했습니다" in captured.err


def test_material_refuses_a_missing_code_checkout_coordinate(
        pd, material_env, monkeypatch, capsys):
    """조용한 빈 목록 금지 — 좌표가 없으면 rc≠0 이다."""
    _home, _done, _checkout, _since = material_env
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {})

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "코드 체크아웃을 해소하지 못했습니다" in captured.err


def test_material_refuses_a_code_checkout_that_is_not_a_local_directory(
        pd, material_env, monkeypatch, capsys):
    _home, _done, _checkout, _since = material_env
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {
        "upstream.path": "https://example.invalid/framework.git",
    })

    assert pd.main(["changelog", "material", "--since", _TAG]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "코드 체크아웃이 로컬 디렉터리가 아닙니다" in captured.err


# ════════════════════════════════════════════════════════════════════════
# CLI 표면
# ════════════════════════════════════════════════════════════════════════

def test_changelog_material_surface_is_registered_in_the_public_parser(pd):
    """스킬 md ↔ CLI 존재 가드가 읽는 그 parser 에 서브커맨드·옵션이 실재한다."""
    parser = pd.build_subcommand_parser("changelog")

    assert parser is not None
    args = parser.parse_args(["material", "--since", _TAG])
    assert (args.changelog_command, args.since) == ("material", _TAG)


def test_changelog_material_requires_the_span_start(pd):
    parser = pd.build_subcommand_parser("changelog")

    with pytest.raises(SystemExit):
        parser.parse_args(["material"])
