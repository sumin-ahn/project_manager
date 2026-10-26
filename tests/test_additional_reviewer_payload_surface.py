"""T-0637 — 추가 리뷰 payload 의 sandbox transport·조기 종료 고지."""
from __future__ import annotations

from pm_engine.infra import fs

from pm_engine.infra import review_diff
from pm_engine.domain import review_diff_rules
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_budget
from pm_engine.infra import reviewer_raw
from pm_engine.app import reviewer_run
from pm_engine.app import reviewer_scope
from pm_engine.infra import round_store

from pm_engine.infra import conf_store
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra import reviewer_target

import importlib.util
import inspect
import subprocess
from pathlib import Path

import pytest
from pm_engine.domain import delegate_errors
from pm_engine.infra import pm_paths


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
MACHINE_PATH = ".opencode/node_modules/pkg/index.js"
SHIPPED_TARGETS = ("claude_code", "codex", "opencode")
DERIVED_PREFIX = "게이트 자동 유도: --gate"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load_external():
    spec = importlib.util.spec_from_file_location(
        "additional_reviewer_t0637", TOOLS / "additional_reviewer.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load_external()


def _raw_diff(*paths: str) -> str:
    return "".join(
        f"diff --git a/{path} b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
        for path in paths
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="strict",
    )


def _pass_result() -> dict:
    answer = "판정: 통과\n\n**must-fix**:\n- 없음\n"
    return {
        "reviewer": "fixture", "ok": True, "output": answer, "answer": answer,
        "verdict": {"has_must_fix": False, "has_pass": True},
        "file": None, "failed": False, "started": True,
        "any_must_fix": False, "all_pass": True,
    }


def _wire_main(external, monkeypatch, tmp_path: Path, *, conf=None, diff=None):
    """main을 tmp 앵커와 무스폰 reviewer로 격리한다."""
    config = {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "src/ .opencode/node_modules/",
    }
    if conf is not None:
        config.update(conf)
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(conf_store, 'local_config', lambda _home, repo=None: dict(config))
    monkeypatch.setattr(
        pm_paths, "resolve_pm_home_for_repo", lambda anchor, **kwargs: tmp_path,
    )
    monkeypatch.setattr(reviewer_scope, "_resolve_diff_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(round_store, "parse_ticket_touches", lambda *args, **kwargs: ["src/"])
    payload = diff if diff is not None else _raw_diff("src/app.py")
    monkeypatch.setattr(
        review_diff, "extract_diff",
        lambda *args, **kwargs: review_diff.ReviewDiff(
            payload,
            'HEAD',
            'INDEX/WORKTREE (untracked 포함)',
            ('x.py',),
            (('git', '-C', str(external.REPO), 'diff', '--', 'x.py'),),
        ),
    )

    calls: list[dict] = []

    def _review(*args, **kwargs):
        calls.append(kwargs)
        return _pass_result()

    monkeypatch.setattr(reviewer_run, "run_review", _review)
    return calls


def test_git_c_unquote_reassembles_octal_bytes_before_utf8_decode(external):
    quoted = (
        r'"a/.opencode/node_modules/pkg/'
        r'\355\225\234\352\270\200 \355\214\214\354\235\274.js"'
    )

    assert review_diff_rules._git_c_unquote(quoted) == (
        "a/.opencode/node_modules/pkg/한글 파일.js"
    )
    assert review_diff_rules._git_c_unquote(r'"a/quote\"-back\\slash.js"') == (
        'a/quote"-back\\slash.js'
    )


def _owner_only(external, path: Path) -> bool:
    """`path` 가 소유자 전용 접근인가 — 엔진 공용 seam(플랫폼 수단 소유)으로 실측한다."""
    return fs.owner_only_access(path)


def _opencode_target(external):
    return reviewer_target.resolve_reviewer_target({
        "additional_reviewer.harness": "opencode",
        "additional_reviewer.model": "zai/glm-4.6",
    })


def test_opencode_transport_is_inside_review_sandbox_and_self_hidden(external, tmp_path):
    target = _opencode_target(external)
    sandbox = tmp_path / "mirror-tree"
    sandbox.mkdir()

    with reviewer_raw._structured_transport(target, "검토 diff", sandbox) as (argv, stdin_text):
        prompt_file = Path(argv[argv.index("--file") + 1])
        wire_dir = Path(argv[argv.index("--dir") + 1])
        ignore = prompt_file.parent / ".gitignore"
        assert prompt_file.resolve().is_relative_to(sandbox.resolve())
        assert wire_dir == sandbox.resolve()
        assert prompt_file.read_text(encoding="utf-8") == "검토 diff"
        # 접근 제한 판정은 퍼미션 비트가 아니라 **OS 되묻기**다 — Windows 의 `chmod` 는 아무
        # 제한도 걸지 않아(`S_IMODE`=0o666 실측) 비트 단언이 그 플랫폼에서 항상 거짓이 된다.
        # 보장(다른 사용자가 못 읽는다)은 같고 수단만 다르므로 수단을 아는 seam 에 묻는다.
        assert _owner_only(external, prompt_file) is True
        assert ignore.read_text(encoding="utf-8") == "*\n"
        assert _owner_only(external, ignore) is True
        assert stdin_text == ""

    assert not prompt_file.exists()
    assert not ignore.exists()
    assert not (sandbox / ".project_manager").exists()


def test_opencode_transport_restriction_goes_through_the_platform_seam(
        external, monkeypatch, tmp_path):
    """전달 파일의 접근 제한은 **공용 seam** 을 지난다 — `chmod` 직접 호출은 Windows 에서 무효다.

    이 배선이 없으면 그 플랫폼에서는 diff 원문이 든 프롬프트가 아무 제한 없이 남는다(생성 seam 의
    `0600` 은 read-only 속성만 만진다). 판정 대상 파일 자신이 POSIX 에서는 이미 0600 이라
    결과만 봐서는 배선 유무가 구분되지 않으므로 **호출**을 본다.
    """
    target = _opencode_target(external)
    sandbox = tmp_path / "mirror-tree"
    sandbox.mkdir()
    restricted: list[Path] = []
    real_restrict = review_rounds._restrict_to_owner
    monkeypatch.setattr(
        review_rounds, "_restrict_to_owner",
        lambda path: (restricted.append(Path(path)), real_restrict(path))[1])

    with reviewer_raw._structured_transport(target, "검토 diff", sandbox) as (argv, _stdin):
        prompt_file = Path(argv[argv.index("--file") + 1])

    assert set(restricted) == {
        prompt_file, prompt_file.parent, prompt_file.parent / ".gitignore",
    }, f"전달 산출물 중 접근 제한을 안 건 자리가 있다: {restricted}"


def test_opencode_post_create_containment_failure_cleans_transport(
        external, monkeypatch, tmp_path):
    target = _opencode_target(external)
    real_assert = delegate_temp._assert_opencode_transport_path
    seen: list[Path] = []

    def _assert(cwd, prompt_file):
        seen.append(Path(prompt_file))
        real_assert(cwd, prompt_file)
        if len(seen) == 2:
            raise delegate_errors.DelegateError("containment 재검사 거부")

    monkeypatch.setattr(delegate_temp, '_assert_opencode_transport_path', _assert)

    with pytest.raises(delegate_errors.DelegateError, match="containment 재검사 거부"):
        with reviewer_raw._structured_transport(target, "검토 diff", tmp_path):
            pytest.fail("containment 실패 뒤 yield하면 안 된다")

    assert len(seen) == 2
    assert not seen[-1].exists()
    assert not (tmp_path / ".project_manager").exists()


# 각 tuple이 한 셀의 기대값이다. 특히 no-gate/명시 gate 행은 조건형 고지까지 부재해야 한다.
@pytest.mark.parametrize(
    "gate_mode,expected_rc,derived_notice,unaccounted_notice,confirmed_notice",
    [
        ("derived", 1, True, False, False),
        ("no-gate", 1, False, False, False),
        ("explicit", 1, False, False, False),
    ],
)
def test_early_exit_notice_matrix_has_cell_specific_expectations(
        external, monkeypatch, tmp_path, capsys, gate_mode, expected_rc,
        derived_notice, unaccounted_notice, confirmed_notice):
    """빈 diff 조기 종료 칸마다 고지가 다르다(호출 0 · 장부 0)."""
    calls = _wire_main(external, monkeypatch, tmp_path, diff="")

    if gate_mode == "derived":
        argv = ["--ticket", "T-0637"]
    elif gate_mode == "no-gate":
        argv = ["--paths", "src/app.py", "--no-gate"]
    else:
        argv = ["--paths", "src/app.py", "--gate", "T-0637"]

    assert external.main(argv) == expected_rc
    assert calls == []
    captured = capsys.readouterr()
    assert (DERIVED_PREFIX in captured.err) is derived_notice
    assert (reviewer_budget._UNACCOUNTED_OPT_OUT_NOTICE in captured.err) is unaccounted_notice
    assert (reviewer_budget._SUMMARY_UNACCOUNTED_GATE in captured.out) is confirmed_notice
    if derived_notice:
        assert "이 실행이 리뷰어를 부르면 라운드 회계가" in captured.err
    else:
        assert "이 실행이 리뷰어를 부르면 라운드 회계가" not in captured.err
    assert not (tmp_path / ".project_manager" / ".local" / "review_rounds.json").exists()


def test_gate_and_anchor_order_remains_unambiguous(external):
    """게이트 해소 → 조기 종료 → 선택 거부 → 예약 순서를 고정한다."""
    source = inspect.getsource(external._main)
    positions = [
        source.index("gate_derivation = reviewer_reservation._derive_gate_from_ticket(args)"),
        source.index("if not diff.strip():"),
        source.index("if not args.gate and not args.no_gate:"),
        source.index("budget = reviewer_reservation._reserve_round_budget("),
    ]
    assert positions == sorted(positions)


def test_help_and_shipped_skill_send_examples_choose_gate_accounting(external):
    """복사 가능한 help/SKILL 실송 예시는 ticket/gate/no-gate 중 하나를 반드시 고른다."""
    script = "python3 .project_manager/tools/additional_reviewer.py"
    help_commands = [
        line.strip()
        for line in external.build_arg_parser().format_help().splitlines()
        if line.strip().startswith(script)
    ]
    skill_paths = sorted({
        *(REPO / ".claude" / "skills").glob("*/SKILL.md"),
        *REPO.glob("templates/*/.claude/skills/*/SKILL.md"),
        *REPO.glob("templates/*/.agents/skills/*/SKILL.md"),
    })
    skill_commands: list[str] = []
    for path in skill_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if script in line:
                skill_commands.append(line[line.index(script):])

    examples = help_commands + skill_commands
    real_send = [
        command for command in examples
        if not {"--dry-run", "--rounds-report", "--resolve-gate"}.intersection(command.split())
    ]
    assert help_commands and skill_commands and real_send
    for command in real_send:
        assert any(flag in command.split() for flag in ("--ticket", "--gate", "--no-gate")), command


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("failure", ["empty", "whitespace", "base", "names", "mismatch", "untracked-list"])
def test_invalid_review_input_has_no_side_effects(
        external, monkeypatch, tmp_path, capsys, dry_run, failure):
    real_extract = review_diff.extract_diff
    calls = _wire_main(external, monkeypatch, tmp_path)
    def git(argv, **kwargs):
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 128 if failure == "untracked-list" else 0,
                                               "", "untracked-list failed")
        if "--name-only" in argv:
            return subprocess.CompletedProcess(argv, 128 if failure == "names" else 0,
                                               "" if failure == "mismatch" else "x.py\0", "names failed")
        if failure == "base":
            return subprocess.CompletedProcess(argv, 128, "", "base failed")
        text = "" if failure == "empty" else " \n\t" if failure == "whitespace" else "AUTO_PATCH_SENTINEL"
        return subprocess.CompletedProcess(argv, 0, text, "")
    monkeypatch.setattr(review_diff, "extract_diff", lambda _home, base, paths: real_extract(_home, base, paths, git))
    output = tmp_path / "must-not-exist"
    argv = ["--paths", "x.py", "--gate", "T-9001", "--output-dir", str(output)]
    if failure != "untracked-list":
        argv += ["--base", "missing-ref" if failure == "base" else "base-ref"]
    if dry_run:
        argv += ["--dry-run"]
    assert external.main(argv) == 1
    assert not calls and not output.exists()
    assert not (tmp_path / ".project_manager" / ".local").exists()
    assert "오류" in capsys.readouterr().err


def test_raw_preserves_actual_reviewer_git_output(external, monkeypatch, tmp_path):
    target = reviewer_target.resolve_reviewer_target(_REVIEWER_TARGET)
    actual_output = "diff --git a/x.py b/x.py\n+ACTUAL_TOOL_OUTPUT\n"
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, actual_output, "git read succeeded")
    result = reviewer_run.run_review(
        pm_paths.home_at(external.REPO), "### 리뷰 입력 좌표\n", target, 5, run_fn=run,
        cwd=tmp_path, output_dir=tmp_path / "raw",
    )
    raw = Path(result["file"]).read_text(encoding="utf-8")
    assert "ACTUAL_TOOL_OUTPUT" in raw and "git read succeeded" in raw
    assert "AUTO_PATCH_SENTINEL" not in raw
