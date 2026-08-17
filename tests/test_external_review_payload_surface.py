"""T-0637 — 추가 리뷰 payload 기계 mirror 제외·sandbox transport·조기 종료 고지."""
from __future__ import annotations

import importlib.util
import inspect
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
MACHINE_PATH = ".opencode/node_modules/pkg/index.js"
SHIPPED_TARGETS = ("claude_code", "codex", "opencode")
DERIVED_PREFIX = "게이트 자동 유도: --gate"


def _load_external():
    spec = importlib.util.spec_from_file_location(
        "external_review_t0637", TOOLS / "external_review.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load_external()


@pytest.fixture(autouse=True)
def _neutral_codex_egress(monkeypatch):
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)


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


def _wire_main(external, monkeypatch, tmp_path: Path, *, conf=None, diff=None, excluded=()):
    """main을 tmp 앵커와 무스폰 reviewer로 격리한다."""
    config = {
        "additional_reviewer_enabled": "true",
        "review_paths": "src/ .opencode/node_modules/",
    }
    if conf is not None:
        config.update(conf)
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(config))
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: tmp_path,
    )
    monkeypatch.setattr(external, "_resolve_diff_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(external, "parse_ticket_touches", lambda *args, **kwargs: ["src/"])
    payload = diff if diff is not None else _raw_diff("src/app.py")
    monkeypatch.setattr(
        external, "extract_diff", lambda *args, **kwargs: (payload, list(excluded)),
    )
    monkeypatch.setattr(external, "_diff_cap_refusal", lambda *args, **kwargs: None)

    mirror = tmp_path / "reviewer"
    tree = mirror / "tree"
    home = mirror / "home"
    tree.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        external, "create_reviewer_workspace",
        lambda *args, **kwargs: external.ReviewerWorkspace(
            root=mirror, tree=tree, home=home, files=1,
            skipped_unsafe=0, git_repo=True,
        ),
    )
    calls: list[dict] = []

    def _review(*args, **kwargs):
        calls.append(kwargs)
        return _pass_result()

    monkeypatch.setattr(external, "run_review", _review)
    return calls


def test_extract_diff_excludes_machine_mirror_without_changing_measurement_axis(external):
    """payload는 mirror를 빼지만 numstat 측정은 기존 단일 술어 규칙 그대로다."""
    human = "src/app.py"
    raw = _raw_diff(MACHINE_PATH, human)
    numstat = f"900\t100\t{MACHINE_PATH}\n2\t1\t{human}\n"

    def _git(argv, **kwargs):
        stdout = numstat if "--numstat" in argv else raw
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    payload, excluded = external.extract_diff("main", ["."], run_fn=_git)

    assert excluded == [MACHINE_PATH]
    assert MACHINE_PATH not in payload
    assert human in payload
    assert external.diff_line_total(Path("/unused"), "main", ["."], run_fn=_git) == 3


def test_git_c_unquote_reassembles_octal_bytes_before_utf8_decode(external):
    quoted = (
        r'"a/.opencode/node_modules/pkg/'
        r'\355\225\234\352\270\200 \355\214\214\354\235\274.js"'
    )

    assert external._git_c_unquote(quoted) == (
        "a/.opencode/node_modules/pkg/한글 파일.js"
    )
    assert external._git_c_unquote(r'"a/quote\"-back\\slash.js"') == (
        'a/quote"-back\\slash.js'
    )


@pytest.mark.parametrize(
    "path,is_mirror",
    [
        (".opencode/node_modules/pkg/file name.js", True),
        (".opencode/node_modules/pkg/한글 파일.js", True),
        ('.opencode/node_modules/pkg/quote".js', True),
        ("src/file name.js", False),
    ],
)
def test_extract_diff_filters_paths_from_real_git_output(
        external, monkeypatch, tmp_path, path, is_mirror):
    """공백/C-quote 경로는 실제 Git 메타데이터로 복원하고 사람 경로는 보존한다."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    source = repo / path
    try:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("old\n", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"이 플랫폼에서 테스트 파일명을 만들 수 없음: {exc}")
    _git(repo, "add", "--", path)
    _git(
        repo, "-c", "user.name=T-0637 test", "-c", "user.email=t0637@example.invalid",
        "commit", "-qm", "base",
    )
    marker = "changed-content-must-not-leak" if is_mirror else "human-content-must-remain"
    source.write_text(marker + "\n", encoding="utf-8")
    raw = _git(repo, "diff", "HEAD", "--", path).stdout
    assert marker in raw

    seen_diff_outputs: list[str] = []

    def _recording_git(argv, **kwargs):
        result = subprocess.run(argv, **kwargs)
        if "diff" in argv:
            seen_diff_outputs.append(result.stdout)
        return result

    monkeypatch.setattr(external, "REPO", repo)
    payload, excluded = external.extract_diff("HEAD", [path], run_fn=_recording_git)

    assert raw in seen_diff_outputs
    if is_mirror:
        assert excluded == [path]
        assert marker not in payload
    else:
        assert excluded == []
        assert marker in payload


def test_ambiguous_diff_header_without_path_metadata_is_fail_closed(external):
    raw = (
        "diff --git a/src/file name.js b/src/file name.js\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    payload, excluded = external._filter_diff_hunks(raw, lambda _path: False)

    assert payload == ""
    assert len(excluded) == 1
    assert "diff 경로 확정 실패" in excluded[0]
    assert "fail-closed" in excluded[0]


@pytest.mark.parametrize(
    "old_marker,new_marker",
    [
        ("--- /dev/null", "+++ b/.opencode/node_modules/pkg/file name.js\t"),
        ("--- a/.opencode/node_modules/pkg/file name.js\t", "+++ /dev/null"),
    ],
)
def test_dev_null_side_uses_the_opposite_git_metadata_path(
        external, old_marker, new_marker):
    path = ".opencode/node_modules/pkg/file name.js"
    raw = (
        f"diff --git a/{path} b/{path}\n"
        f"{old_marker}\n{new_marker}\n"
        "@@ -0,0 +1 @@\n+new\n"
    )

    payload, excluded = external._filter_diff_hunks(
        raw, external._is_review_machine_mirror_path,
    )

    assert payload == ""
    assert excluded == [path]


def test_rename_metadata_resolves_an_ambiguous_space_header(external):
    destination = ".opencode/node_modules/pkg/file name.js"
    raw = (
        f"diff --git a/src/file name.js b/{destination}\n"
        "similarity index 100%\n"
        "rename from src/file name.js\n"
        f"rename to {destination}\n"
    )

    payload, excluded = external._filter_diff_hunks(
        raw, external._is_review_machine_mirror_path,
    )

    assert payload == ""
    assert excluded == [destination]


def test_unresolved_path_exclusion_reason_is_reported(
        external, monkeypatch, tmp_path, capsys):
    unresolved = (
        f"{external._DIFF_PATH_UNRESOLVED_PREFIX}"
        "diff --git a/src/file name.js b/src/file name.js"
    )
    calls = _wire_main(external, monkeypatch, tmp_path, excluded=[unresolved])

    rc = external.main(["--no-gate"])

    assert rc == 0
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "경로를 유일하게 확정하지 못해" in captured.err
    assert "fail-closed" in captured.err
    assert unresolved in captured.out


@pytest.mark.parametrize("target", SHIPPED_TARGETS)
def test_hand_edited_template_manifest_remains_in_payload(external, target):
    """실재하는 손편집 manifest는 측정 subtree 안이어도 payload에 남는다."""
    hand_edited_manifest = f"templates/{target}/.project_manager/engine.manifest"
    assert (REPO / hand_edited_manifest).is_file(), f"테스트 대상 실파일 없음: {hand_edited_manifest}"
    machine_copy = f"templates/{target}/.project_manager/tools/external_review.py"
    raw = _raw_diff(machine_copy, hand_edited_manifest)

    def _git(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr="")

    payload, excluded = external.extract_diff("main", ["templates/"], run_fn=_git)

    assert excluded == [machine_copy]
    assert machine_copy not in payload
    assert hand_edited_manifest in payload
    assert external.is_machine_mirror_path(hand_edited_manifest) is True


def test_explicit_machine_mirror_path_is_blocked_with_predicate_reason(
        external, monkeypatch, tmp_path, capsys):
    calls = _wire_main(external, monkeypatch, tmp_path, excluded=[MACHINE_PATH])

    rc = external.main(["--paths", MACHINE_PATH, "--no-gate"])

    assert rc == 1
    assert calls == []
    err = capsys.readouterr().err
    assert MACHINE_PATH in err
    assert "is_machine_mirror_path=True" in err
    assert "false-confidence" in err


@pytest.mark.parametrize("target", SHIPPED_TARGETS)
def test_explicit_hand_edited_manifest_is_not_blocked(
        external, monkeypatch, tmp_path, target):
    """측정 술어가 True여도 exact manifest는 payload carve-out을 따라 리뷰할 수 있다."""
    hand_edited_manifest = f"templates/{target}/.project_manager/engine.manifest"
    assert (REPO / hand_edited_manifest).is_file(), f"테스트 대상 실파일 없음: {hand_edited_manifest}"
    calls = _wire_main(external, monkeypatch, tmp_path)

    rc = external.main(["--paths", hand_edited_manifest, "--no-gate"])

    assert rc == 0
    assert len(calls) == 1
    assert external.is_machine_mirror_path(hand_edited_manifest) is True


def test_explicit_machine_mirror_root_is_blocked_even_when_diff_is_empty(
        external, monkeypatch, tmp_path, capsys):
    """subtree 루트 직접 지목은 빈 diff 안내로 오도되기 전에 predicate 사유로 차단한다."""
    calls = _wire_main(external, monkeypatch, tmp_path, diff="")
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *args, **kwargs: pytest.fail("직접 지목 차단 뒤 diff를 추출하면 안 된다"),
    )

    rc = external.main(["--paths", ".opencode/node_modules", "--no-gate"])

    assert rc == 1
    assert calls == []
    err = capsys.readouterr().err
    assert ".opencode/node_modules" in err
    assert "is_machine_mirror_path=True" in err
    assert "리뷰할 diff 가 없습니다" not in err


def test_implicit_machine_mirror_exclusion_warns_and_annotates_verdict(
        external, monkeypatch, tmp_path, capsys):
    calls = _wire_main(external, monkeypatch, tmp_path, excluded=[MACHINE_PATH])

    rc = external.main(["--no-gate"])

    assert rc == 0
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert f"기계 mirror 경로 '{MACHINE_PATH}'" in captured.err
    assert "is_machine_mirror_path=True" in captured.err
    assert f"종합 판정: 통과 (검토 제외 1건 — {MACHINE_PATH})" in captured.out


@pytest.mark.parametrize("target", SHIPPED_TARGETS)
def test_reviewer_mirror_preserves_hand_edited_manifest(
        external, monkeypatch, tmp_path, target):
    hand_edited_manifest = f"templates/{target}/.project_manager/engine.manifest"
    assert (REPO / hand_edited_manifest).is_file(), f"테스트 대상 실파일 없음: {hand_edited_manifest}"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for relative, content in (
        (MACHINE_PATH, "generated\n"),
        (hand_edited_manifest, "hand edited\n"),
        ("src/app.py", "human\n"),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    destination.mkdir()
    tracked = [MACHINE_PATH, hand_edited_manifest, "src/app.py"]
    monkeypatch.setattr(external, "_tracked_relative_paths", lambda *args, **kwargs: tracked)

    copied, skipped_unsafe, skipped_secret = external._mirror_tracked_files(source, destination)

    assert (copied, skipped_unsafe, skipped_secret) == (2, 1, 0)
    assert not (destination / MACHINE_PATH).exists()
    assert (destination / hand_edited_manifest).read_text(encoding="utf-8") == "hand edited\n"
    assert external._is_denied_mirror_path(MACHINE_PATH) is True
    assert external._is_denied_mirror_path(hand_edited_manifest) is False


def _owner_only(external, path: Path) -> bool:
    """`path` 가 소유자 전용 접근인가 — 엔진 공용 seam(플랫폼 수단 소유)으로 실측한다."""
    return external._load_file_lock().owner_only_access(path)


def _opencode_target(external):
    return external.resolve_reviewer_target({
        "additional_reviewer.harness": "opencode",
        "additional_reviewer.model": "zai/glm-4.6",
    })


def test_opencode_transport_is_inside_review_sandbox_and_self_hidden(external, tmp_path):
    target = _opencode_target(external)
    sandbox = tmp_path / "mirror-tree"
    sandbox.mkdir()

    with external._structured_transport(target, "검토 diff", sandbox) as (argv, stdin_text):
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
    real_restrict = external._restrict_to_owner
    monkeypatch.setattr(
        external, "_restrict_to_owner",
        lambda path: (restricted.append(Path(path)), real_restrict(path))[1])

    with external._structured_transport(target, "검토 diff", sandbox) as (argv, _stdin):
        prompt_file = Path(argv[argv.index("--file") + 1])

    assert set(restricted) == {
        prompt_file, prompt_file.parent, prompt_file.parent / ".gitignore",
    }, f"전달 산출물 중 접근 제한을 안 건 자리가 있다: {restricted}"


def test_opencode_post_create_containment_failure_cleans_transport(
        external, monkeypatch, tmp_path):
    target = _opencode_target(external)
    delegate = external._load_delegate_transport()
    real_assert = delegate._assert_opencode_transport_path
    seen: list[Path] = []

    def _assert(cwd, prompt_file):
        seen.append(Path(prompt_file))
        real_assert(cwd, prompt_file)
        if len(seen) == 2:
            raise delegate.DelegateError("containment 재검사 거부")

    monkeypatch.setattr(delegate, "_assert_opencode_transport_path", _assert)

    with pytest.raises(delegate.DelegateError, match="containment 재검사 거부"):
        with external._structured_transport(target, "검토 diff", tmp_path):
            pytest.fail("containment 실패 뒤 yield하면 안 된다")

    assert len(seen) == 2
    assert not seen[-1].exists()
    assert not (tmp_path / ".project_manager").exists()


# 각 tuple이 한 셀의 기대값이다. 특히 no-gate/명시 gate 행은 조건형 고지까지 부재해야 한다.
@pytest.mark.parametrize(
    "early_exit,gate_mode,expected_rc,derived_notice,unaccounted_notice,confirmed_notice",
    [
        ("empty", "derived", 1, True, False, False),
        ("empty", "no-gate", 1, False, False, False),
        ("empty", "explicit", 1, False, False, False),
        ("disabled", "derived", 0, True, False, False),
        ("disabled", "no-gate", 0, False, False, False),
        ("disabled", "explicit", 0, False, False, False),
        ("egress", "derived", 1, True, False, False),
        ("egress", "no-gate", 1, False, False, False),
        ("egress", "explicit", 1, False, False, False),
        ("diff-cap", "derived", 1, True, False, False),
        ("diff-cap", "no-gate", 1, False, False, False),
        ("diff-cap", "explicit", 1, False, False, False),
    ],
)
def test_early_exit_notice_matrix_has_cell_specific_expectations(
        external, monkeypatch, tmp_path, capsys, early_exit, gate_mode, expected_rc,
        derived_notice, unaccounted_notice, confirmed_notice):
    conf = {"additional_reviewer_enabled": "false" if early_exit == "disabled" else "true"}
    diff = "" if early_exit == "empty" else _raw_diff("src/app.py")
    calls = _wire_main(external, monkeypatch, tmp_path, conf=conf, diff=diff)
    if early_exit == "egress":
        monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    if early_exit == "diff-cap":
        monkeypatch.setattr(external, "_diff_cap_refusal", lambda *args, **kwargs: "diff-cap 차단")

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
    assert (external._UNACCOUNTED_OPT_OUT_NOTICE in captured.err) is unaccounted_notice
    assert (external._SUMMARY_UNACCOUNTED_GATE in captured.out) is confirmed_notice
    if derived_notice:
        assert "이 실행이 전송되면" in captured.err
    else:
        assert "이 실행이 전송되면" not in captured.err
    assert "이번 전송은" not in captured.err
    assert not (tmp_path / ".project_manager" / ".local" / "review_rounds.json").exists()


def test_gate_and_anchor_order_remains_unambiguous(external):
    """게이트 해소 → 조기 종료 → 선택 거부 → 앵커 차단 → 예약 순서를 고정한다."""
    source = inspect.getsource(external._main)
    positions = [
        source.index("gate_derivation = _derive_gate_from_ticket(args)"),
        source.index("if not diff.strip():"),
        source.index("if not _is_enabled(conf) and not args.force:"),
        source.index("if codex_egress_required and not args.codex_egress_escalated:"),
        source.index("cap_block = _diff_cap_refusal("),
        source.index("if not args.gate and not args.no_gate:"),
        source.index("anchor_refusal = _self_anchored_round_refusal("),
        source.index("budget = _reserve_round_budget("),
    ]
    assert positions == sorted(positions)


def test_help_and_shipped_skill_send_examples_choose_gate_accounting(external):
    """복사 가능한 help/SKILL 실송 예시는 ticket/gate/no-gate 중 하나를 반드시 고른다."""
    script = "python3 .project_manager/tools/external_review.py"
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
