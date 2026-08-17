"""T-0476 — §4.7 합성 프롬프트 시크릿 스캔의 사람 승인 CLI 경로."""
from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"
_TOKEN_RE = re.compile(r"승인 토큰: ([0-9a-f]{24})")


@pytest.fixture()
def pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_t0476", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRun:
    def __init__(self, reply: str = "ACK-DONE"):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append(
            {
                "argv": argv,
                "stdin_text": stdin_text,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
                "harness": harness,
            }
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "th-t0476"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": self.reply},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            ]
        )
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
        }


class _SequenceRun:
    def __init__(self, *results: dict):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append(
            {
                "argv": argv,
                "stdin_text": stdin_text,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
                "harness": harness,
            }
        )
        return self.results.pop(0)


def _conf(**extra) -> dict[str, str]:
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
    }
    conf.update(extra)
    return conf


def _run(pd, monkeypatch, argv: list[str], fake: _FakeRun, conf=None) -> int:
    monkeypatch.setattr(pd, "local_config", lambda: conf or _conf())
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *args, **kwargs: True)
    return pd.main(argv, run_fn=fake)


def _base_argv(prompt: Path, cwd: Path, output_dir: Path | None = None) -> list[str]:
    if output_dir is None:
        output_dir = cwd / "raw"
    argv = [
        "--role",
        "developer",
        "--prompt-file",
        str(prompt),
        "--cwd",
        str(cwd),
        "--output-dir",
        str(output_dir),
    ]
    return argv


def _blocked_digest(stderr: str) -> str:
    match = _TOKEN_RE.search(stderr)
    assert match is not None, stderr
    return match.group(1)


def test_secret_path_example_blocks_then_matching_ack_sends_with_loud_provenance(
    pd, monkeypatch, tmp_path, capsys
):
    """T-0472 저장 프롬프트형: 차단 → 출력 digest 승인 → mock 송신·3채널 감사."""
    prompt = tmp_path / "T-0472-fix-prompt.md"
    prompt.write_text(
        "시크릿 스캐너가 예시 경로 ~/.aws/credentials 를 탐지하도록 수정하라.",
        encoding="utf-8",
    )
    outdir = tmp_path / "raw"
    argv = _base_argv(prompt, tmp_path, outdir)
    fake = _FakeRun()

    blocked_rc = _run(pd, monkeypatch, argv, fake)
    blocked = capsys.readouterr()
    digest = _blocked_digest(blocked.err)

    assert blocked_rc == 1
    assert fake.calls == []
    # T-0472의 기존 fail-loud 재료는 유지하고, 승인 토큰/재실행 1줄을 덧붙인다.
    assert "합성 프롬프트가 시크릿 denylist 판정" in blocked.err
    assert "경로축 판정" in blocked.err
    assert "발췌: ~/.aws/credentials" in blocked.err
    assert f"--secret-scan-ack {digest}" in blocked.err
    assert len([line for line in blocked.err.splitlines() if "· 재실행:" in line]) == 1

    approved_rc = _run(
        pd,
        monkeypatch,
        [*argv, "--secret-scan-ack", digest],
        fake,
    )
    approved = capsys.readouterr()

    assert approved_rc == 0
    assert len(fake.calls) == 1
    assert "~/.aws/credentials" in fake.calls[0]["stdin_text"]
    assert (
        "시크릿 스캔 차단을 명시 승인으로 통과 — "
        f"발췌 <~/.aws/credentials> · digest <{digest}>"
    ) in approved.err
    assert (
        f"[pm-delegate] 실행 provenance: 시크릿 스캔 명시 승인 통과 · digest={digest}"
        in approved.out
    )
    assert "ACK-DONE" in approved.out
    raws = list(outdir.glob("pm_delegate_codex_*.txt"))
    assert len(raws) == 1
    raw_text = raws[0].read_text(encoding="utf-8")
    assert f"# secret_scan_ack: explicit override · digest={digest}" in raw_text
    assert "경로축 판정" in raw_text
    assert "발췌 <~/.aws/credentials>" in raw_text


def test_ack_from_other_prompt_and_one_character_change_are_rejected(
    pd, monkeypatch, tmp_path, capsys
):
    """프롬프트 A의 digest를 프롬프트 B main 실행에 교차 재사용하면 fail-loud."""
    prompt_a = tmp_path / "prompt-a.md"
    prompt_b = tmp_path / "prompt-b.md"
    prompt_a.write_text("예시 ~/.aws/credentials 를 스캐너가 잡는지 확인하라.", encoding="utf-8")
    prompt_b.write_text("예시 ~/.aws/credentials 를 스캐너가 잡는지 확인하라!", encoding="utf-8")
    argv_a = _base_argv(prompt_a, tmp_path)
    argv_b = _base_argv(prompt_b, tmp_path)
    fake = _FakeRun()

    assert _run(pd, monkeypatch, argv_a, fake) == 1
    old_digest = _blocked_digest(capsys.readouterr().err)

    assert _run(
        pd, monkeypatch, [*argv_b, "--secret-scan-ack", old_digest], fake
    ) == 1
    changed = capsys.readouterr().err
    new_digest = _blocked_digest(changed)

    assert new_digest != old_digest
    assert (
        "승인 digest 불일치 — 프롬프트 또는 해소된 수신자 "
        "(harness:model)가 바뀜 · 발췌 재확인"
    ) in changed
    assert fake.calls == []


def test_digest_binds_role_preamble_and_entire_task_text(pd):
    task = "예시 ~/.aws/credentials 를 확인"
    developer = pd.ROLE_PREAMBLES["developer"] + "\n\n" + task
    researcher = pd.ROLE_PREAMBLES["researcher"] + "\n\n" + task

    digest = pd.secret_scan_prompt_digest(developer, "codex", "gpt-x")
    assert re.fullmatch(r"[0-9a-f]{24}", digest)
    assert digest != pd.secret_scan_prompt_digest(developer + "!", "codex", "gpt-x")
    assert digest != pd.secret_scan_prompt_digest(researcher, "codex", "gpt-x")
    assert digest != pd.secret_scan_prompt_digest(developer, "claude", "gpt-x")
    assert digest != pd.secret_scan_prompt_digest(developer, "codex", "gpt-y")


def test_conf_cannot_enable_ack_override(pd, monkeypatch, tmp_path, capsys):
    """동명의 conf 값이 정확 digest여도 CLI 인자가 없으면 기본 게이트는 계속 차단."""
    prompt = tmp_path / "prompt.md"
    task = "예시 ~/.aws/credentials 를 확인"
    prompt.write_text(task, encoding="utf-8")
    digest = pd.secret_scan_prompt_digest(
        pd.ROLE_PREAMBLES["developer"] + "\n\n" + task, "codex", "gpt-x"
    )
    fake = _FakeRun()

    rc = _run(
        pd,
        monkeypatch,
        _base_argv(prompt, tmp_path),
        fake,
        conf=_conf(**{"secret_scan_ack": digest, "delegate.secret_scan_ack": digest}),
    )

    assert rc == 1
    assert _blocked_digest(capsys.readouterr().err) == digest
    assert fake.calls == []


def test_prompt_file_path_gate_ignores_even_matching_content_ack(
    pd, monkeypatch, tmp_path, capsys
):
    """④ 경로/이름 차단은 내용 읽기 전이며, 합성 전문과 맞는 ack도 적용하지 않는다."""
    prompt = tmp_path / "credentials.env"
    task = "일반 작업"
    prompt.write_text(task, encoding="utf-8")
    digest = pd.secret_scan_prompt_digest(
        pd.ROLE_PREAMBLES["developer"] + "\n\n" + task, "codex", "gpt-x"
    )
    fake = _FakeRun()

    rc = _run(
        pd,
        monkeypatch,
        [*_base_argv(prompt, tmp_path), "--secret-scan-ack", digest],
        fake,
    )
    err = capsys.readouterr().err

    assert rc == 1
    assert "--prompt-file 경로/이름" in err
    assert "내용 읽기 전 차단" in err
    assert "명시 승인으로 통과" not in err
    assert fake.calls == []


def test_retry_command_replaces_stale_ack_instead_of_accumulating(pd):
    command = pd._secret_scan_retry_command(
        ["--role", "developer", "--secret-scan-ack=stale", "--cwd", "/repo"],
        "a" * 24,
    )
    assert "stale" not in command
    assert command.count("--secret-scan-ack") == 1
    assert "a" * 24 in command


def test_all_secret_hits_are_listed_before_ack_and_preserved_in_raw_audit(
    pd, monkeypatch, tmp_path, capsys
):
    """앞 경로 예시 뒤의 실 PAT까지 전부 표시해야만 ack가 송신을 연다."""
    pat = "ghp_0123456789ABCDEFGHIJKLMNOP"
    task = (
        "먼저 무해한 스캐너 예시 ~/.aws/credentials 를 문서화하라.\n"
        f"뒤쪽 실 토큰 GITHUB_TOKEN={pat}"
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text(task, encoding="utf-8")
    outdir = tmp_path / "raw"
    argv = _base_argv(prompt, tmp_path, outdir)
    fake = _FakeRun()
    full_prompt = pd.ROLE_PREAMBLES["developer"] + "\n\n" + task
    hits = pd.scan_prompt_secret_hits(full_prompt)

    assert len(hits) >= 2
    assert hits[0].excerpt == "~/.aws/credentials"
    assert any(hit.excerpt.startswith("ghp_***") for hit in hits)

    assert _run(pd, monkeypatch, argv, fake) == 1
    blocked = capsys.readouterr().err
    digest = _blocked_digest(blocked)

    assert blocked.count("발췌:") == len(hits)
    assert "발췌: ~/.aws/credentials" in blocked
    assert "발췌: ghp_***" in blocked
    assert pat not in blocked
    assert fake.calls == []

    assert _run(pd, monkeypatch, [*argv, "--secret-scan-ack", digest], fake) == 0
    approved = capsys.readouterr()

    assert len(fake.calls) == 1
    assert approved.err.count("발췌:") == len(hits)
    raw = next(outdir.glob("pm_delegate_codex_*.txt")).read_text(encoding="utf-8")
    assert f"전 탐지={len(hits)}" in raw
    for hit in hits:
        assert hit.pattern in raw
        assert hit.excerpt in raw
    assert pat not in raw


@pytest.mark.parametrize("malformed", ["가" * 24, "A" * 24])
def test_malformed_ack_is_usage_error_before_scan(pd, tmp_path, capsys, malformed):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("예시 ~/.aws/credentials", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        pd.main([*_base_argv(prompt, tmp_path), "--secret-scan-ack", malformed])

    assert exc.value.code == 2
    assert "승인 토큰은 24자리 소문자 hex" in capsys.readouterr().err


def test_windows_retry_command_uses_encoded_powershell_for_shell_metacharacters(
    pd, monkeypatch
):
    monkeypatch.setattr(pd, "_running_on_windows", lambda: True)
    dangerous = "/repo/a&b%PATH%'quoted"
    command = pd._secret_scan_retry_command(
        ["--role", "developer", "--cwd", dangerous], "b" * 24
    )

    encoded_command, decoded_display = command.splitlines()
    assert encoded_command.startswith(
        "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
    )
    assert "&" not in encoded_command
    assert "%PATH%" not in encoded_command
    encoded = encoded_command.rsplit(" ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert script.startswith("& ")
    assert "'/repo/a&b%PATH%''quoted'" in script
    # 인용이 필요한 토큰만 PowerShell 리터럴로 감싼다 — 승인 토큰은 그대로 읽혀야 한다.
    assert "--secret-scan-ack " + "b" * 24 in script
    assert "PowerShell 디코드(검토용·복사는 위 인코딩본):" in decoded_display
    assert script in decoded_display


# ── T-0714: 처방 커맨드 셸 인용 seam (POSIX/Windows 렌더러 직접 주입) ──────────

_WINDOWS_ACK_ARGV = [
    r"C:\Users\ci\AppData\Local\Programs\Python\Python312\python.exe",
    r"C:\repo\.project_manager\tools\pm_delegate.py",
    "--role",
    "developer",
    "--secret-scan-ack",
    "c" * 24,
]


def test_render_shell_command_posix_quotes_only_tokens_that_need_it(pd):
    rendered = pd.render_shell_command(
        ["python3", "pm_delegate.py", "--role", "developer"], windows=False
    )

    assert rendered == "python3 pm_delegate.py --role developer"
    assert pd.render_shell_command(
        ["--cwd", "/work tree/repo"], windows=False
    ) == "--cwd '/work tree/repo'"


def test_render_shell_command_windows_uses_native_quoting_not_posix(pd):
    rendered = pd.render_shell_command(_WINDOWS_ACK_ARGV, windows=True)

    assert rendered == " ".join(_WINDOWS_ACK_ARGV)
    assert "'" not in rendered
    assert pd.render_shell_command(
        ["--cwd", r"C:\work tree\repo"], windows=True
    ) == '--cwd "C:\\work tree\\repo"'


def test_render_shell_token_follows_the_same_platform_rules(pd):
    assert pd.render_shell_token("/work tree/repo", windows=False) == "'/work tree/repo'"
    assert pd.render_shell_token(r"C:\ci\repo", windows=True) == r"C:\ci\repo"
    assert pd.render_shell_token(r"C:\work tree\repo", windows=True) == (
        '"C:\\work tree\\repo"'
    )
    # 셸 메타문자는 argv 규칙상 인용이 불필요해도 셸이 먼저 재해석한다.
    assert pd.render_shell_token("a&b", windows=True) == '"a&b"'


def test_render_shell_command_windows_escalates_unquotable_tokens(pd):
    """인용만으로 두 Windows 셸에서 리터럴화 못 하는 토큰은 EncodedCommand로 올린다."""
    rendered = pd.render_shell_command(
        ["python.exe", "run.py", "--cwd", r"C:\a%PATH%b"], windows=True
    )

    assert rendered.startswith(
        "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
    )
    assert "%PATH%" not in rendered.splitlines()[0]


def test_render_shell_command_windows_escalates_quoted_program_token(pd):
    """PowerShell은 인용된 첫 토큰을 명령이 아닌 문자열로 읽는다 — 붙여넣기 안전형으로 올린다."""
    rendered = pd.render_shell_command(
        [r"C:\Program Files\Python312\python.exe", "run.py", "--role", "developer"],
        windows=True,
    )
    encoded_command, decoded_display = rendered.splitlines()
    script = base64.b64decode(encoded_command.rsplit(" ", 1)[1]).decode("utf-16-le")

    assert script.startswith("& 'C:\\Program Files\\Python312\\python.exe' run.py")
    assert "--role developer" in script
    assert "PowerShell 디코드" in decoded_display


@pytest.mark.parametrize("windows", (False, True))
def test_rendered_prescriptions_never_chain_commands(pd, windows):
    rendered = pd.render_shell_command(_WINDOWS_ACK_ARGV, windows=windows)

    assert "&&" not in rendered
    assert ";" not in rendered


def test_secret_scan_retry_command_on_windows_is_pasteable(pd, monkeypatch):
    """Windows 재실행 처방은 POSIX 인용 없이 붙여넣어 실행되는 줄이어야 한다."""
    monkeypatch.setattr(pd, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        pd.sys, "executable", r"C:\ci\Python312\python.exe", raising=False
    )
    digest = "d" * 24

    command = pd._secret_scan_retry_command(
        ["--role", "developer", "--cwd", r"C:\ci\repo"], digest
    )

    assert f"--secret-scan-ack {digest}" in command
    assert f"'--secret-scan-ack' '{digest}'" not in command
    assert r"--cwd C:\ci\repo" in command
    assert "&&" not in command


def test_secret_scan_retry_command_on_posix_keeps_shlex_rules(pd, monkeypatch):
    monkeypatch.setattr(pd, "_running_on_windows", lambda: False)
    digest = "e" * 24

    command = pd._secret_scan_retry_command(
        ["--role", "developer", "--cwd", "/work tree/repo"], digest
    )

    assert f"--secret-scan-ack {digest}" in command
    assert "--cwd '/work tree/repo'" in command


def test_ack_digest_cannot_be_reused_for_different_resolved_recipient(
    pd, monkeypatch, tmp_path, capsys
):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("예시 ~/.aws/credentials 를 확인하라.", encoding="utf-8")
    argv = _base_argv(prompt, tmp_path)
    fake = _FakeRun()

    assert _run(pd, monkeypatch, argv, fake) == 1
    codex_digest = _blocked_digest(capsys.readouterr().err)

    assert _run(
        pd,
        monkeypatch,
        [
            *argv,
            "--harness",
            "claude",
            "--model",
            "opus",
            "--secret-scan-ack",
            codex_digest,
        ],
        fake,
    ) == 1
    changed = capsys.readouterr().err
    claude_digest = _blocked_digest(changed)

    assert claude_digest != codex_digest
    assert "승인 digest 불일치" in changed
    assert fake.calls == []


def test_ack_help_names_prompt_and_resolved_primary_recipient_binding(pd):
    help_text = " ".join(pd.build_arg_parser().format_help().split())

    assert "합성 프롬프트 전문 + 해소된 primary 수신자 harness:model에 결속" in help_text


def test_ack_help_warns_that_a_new_round_changes_the_digest(pd):
    """차단→승인 사이에 같은 티켓·역할 위임이 마감되면 digest 가 바뀐다는 안내 1줄 (T-0600).

    `--resume-from` 후보가 그 사이 새 레코드로 바뀌면 delta 가 달라져 합성 프롬프트 전문이
    달라진다 — 받아 둔 digest 는 불일치로 loud 차단되고 재승인이 필요하다. 안내가 없으면
    승인자가 그 실패를 "ack 이 안 먹는다"로 읽는다.
    """
    help_text = " ".join(pd.build_arg_parser().format_help().split())

    assert "--resume-from 후보가 바뀌어" in help_text
    assert "다시 승인한다" in help_text


def test_missing_digest_fails_closed_without_python_assert(
    pd, monkeypatch, tmp_path, capsys
):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("예시 ~/.aws/credentials 를 확인하라.", encoding="utf-8")
    fake = _FakeRun()
    monkeypatch.setattr(pd, "secret_scan_prompt_digest", lambda *_args: None)

    assert _run(pd, monkeypatch, _base_argv(prompt, tmp_path), fake) == 1
    err = capsys.readouterr().err

    assert "승인 digest를 생성하지 못했습니다 — 전송 전 차단" in err
    assert fake.calls == []


def test_ack_suppresses_infrastructure_fallback_loudly_in_stderr_and_raw(
    pd, monkeypatch, tmp_path, capsys
):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("예시 ~/.aws/credentials 를 확인하라.", encoding="utf-8")
    outdir = tmp_path / "raw"
    argv = _base_argv(prompt, tmp_path, outdir)
    conf = _conf(
        **{
            "delegate.developer.fallback.harness": "claude",
            "delegate.developer.fallback.model": "opus",
        }
    )

    assert _run(pd, monkeypatch, argv, _FakeRun(), conf=conf) == 1
    digest = _blocked_digest(capsys.readouterr().err)
    fake = _SequenceRun(
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "error code: rate_limit_exceeded",
            "timed_out": False,
        },
        {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "type": "result",
                    "result": "FALLBACK-DONE",
                    "session_id": "s-t0476",
                }
            ),
            "stderr": "",
            "timed_out": False,
        },
    )

    assert _run(
        pd, monkeypatch, [*argv, "--secret-scan-ack", digest], fake, conf=conf
    ) == 1
    captured = capsys.readouterr()

    assert [call["harness"] for call in fake.calls] == ["codex"]
    assert len(fake.results) == 1, "폴백 결과가 소비되면 ack 억제가 발화하지 않은 것"
    assert pd.ACK_FALLBACK_SUPPRESSION_REASON in captured.err
    raw_texts = [
        path.read_text(encoding="utf-8")
        for path in outdir.glob("pm_delegate_*.txt")
    ]
    assert len(raw_texts) == 1
    primary_raw = raw_texts[0]
    assert "# attempt: primary" in primary_raw
    assert (
        f"# fallback_suppressed: {pd.ACK_FALLBACK_SUPPRESSION_REASON}"
        in primary_raw
    )
    assert f"# secret_scan_ack: explicit override · digest={digest}" in primary_raw
    assert "# harness: codex" in primary_raw
    assert "# model: gpt-x" in primary_raw
    assert all("# attempt: fallback-from-" not in text for text in raw_texts)


def test_same_infrastructure_failure_without_ack_still_falls_back(
    pd, monkeypatch, tmp_path, capsys
):
    """ack 유무만 경계를 가른다 — 탐지 없는 정상 실행의 기존 폴백은 그대로다."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("정상 프롬프트를 처리하라.", encoding="utf-8")
    outdir = tmp_path / "raw"
    argv = _base_argv(prompt, tmp_path, outdir)
    conf = _conf(
        **{
            "delegate.developer.fallback.harness": "claude",
            "delegate.developer.fallback.model": "opus",
        }
    )
    fake = _SequenceRun(
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "error code: rate_limit_exceeded",
            "timed_out": False,
        },
        {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "type": "result",
                    "result": "FALLBACK-DONE",
                    "session_id": "s-t0496",
                }
            ),
            "stderr": "",
            "timed_out": False,
        },
    )

    assert _run(pd, monkeypatch, argv, fake, conf=conf) == 0
    captured = capsys.readouterr()

    assert [call["harness"] for call in fake.calls] == ["codex", "claude"]
    assert "FALLBACK-DONE" in captured.out
    assert pd.ACK_FALLBACK_SUPPRESSION_REASON not in captured.err
    raw_texts = [
        path.read_text(encoding="utf-8")
        for path in outdir.glob("pm_delegate_*.txt")
    ]
    assert len(raw_texts) == 2
    assert any("# attempt: fallback-from-codex:" in text for text in raw_texts)
    assert all("# fallback_suppressed:" not in text for text in raw_texts)


def test_exhaustive_hits_deduplicate_cap_display_and_split_raw_audit_lines(
    pd, monkeypatch
):
    duplicate = pd.PromptSecretHit("same.env", "*.env", "경로")
    unique_hits = tuple(
        pd.PromptSecretHit(f"file-{index}.env", "*.env", "경로")
        for index in range(pd.SECRET_SCAN_HIT_DISPLAY_LIMIT + 2)
    )
    monkeypatch.setattr(
        pd,
        "_iter_prompt_secret_hits",
        lambda _prompt: iter((duplicate, duplicate, *unique_hits)),
    )

    hits = pd.scan_prompt_secret_hits("synthetic")
    assert hits.count(duplicate) == 1
    assert len(hits) == len(unique_hits) + 1

    displayed = pd._format_secret_scan_hits(hits)
    assert displayed.count("발췌:") == pd.SECRET_SCAN_HIT_DISPLAY_LIMIT
    assert "… 3건 더 · 전체는 raw 감사줄에서 확인" in displayed
    assert "file-21.env" not in displayed

    raw = pd._format_meta(
        ["codex"], 0, "codex", "gpt-x", 0.1, "", "",
        secret_scan_ack_digest="c" * 24,
        secret_scan_ack_hits=hits,
    )
    hit_lines = [
        line for line in raw.splitlines()
        if line.startswith("# secret_scan_ack_hit:")
    ]
    assert len(hit_lines) == len(hits)
    assert all(line.count("발췌 <") == 1 for line in hit_lines)


def test_dry_run_previews_exhaustive_scan_and_bound_digest(
    pd, monkeypatch, tmp_path, capsys
):
    prompt = tmp_path / "prompt.md"
    task = "예시 ~/.aws/credentials 를 확인하라."
    prompt.write_text(task, encoding="utf-8")
    fake = _FakeRun()
    conf = _conf(
        **{
            "delegate.developer.fallback.harness": "claude",
            "delegate.developer.fallback.model": "opus",
        }
    )

    assert _run(
        pd, monkeypatch, [*_base_argv(prompt, tmp_path), "--dry-run"], fake, conf=conf
    ) == 0
    out = capsys.readouterr().out
    expected = pd.secret_scan_prompt_digest(
        pd.ROLE_PREAMBLES["developer"] + "\n\n" + task, "codex", "gpt-x"
    )

    assert "시크릿 스캔: 탐지 1건" in out
    assert "탐지 1/1: 경로축 판정" in out
    assert f"승인 digest 미리보기: {expected}" in out
    assert "단, --secret-scan-ack 로 통과하면 비발동" in out
    assert "--secret-scan-ack 통과 시 폴백 비발동이라 이 최악값은 과대" in out
    assert fake.calls == []


def test_dry_run_without_secret_detection_keeps_fallback_preview_unchanged(
    pd, monkeypatch, tmp_path, capsys
):
    """ack 경계 표기는 탐지된 미리보기에만 붙고, 기존 비탐지 출력은 그대로다."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("정상 프롬프트를 처리하라.", encoding="utf-8")
    conf = _conf(
        **{
            "delegate.developer.fallback.harness": "claude",
            "delegate.developer.fallback.model": "opus",
        }
    )

    assert _run(
        pd, monkeypatch, [*_base_argv(prompt, tmp_path), "--dry-run"], _FakeRun(), conf=conf
    ) == 0
    out = capsys.readouterr().out

    assert "폴백: harness=claude model=opus reasoning=None (인프라 실패에만 1회)" in out
    assert "폴백 시간 예산: 최악 primary 3610s + 폴백 3610s = 7220s (2차 폴백 없음)" in out
    assert "--secret-scan-ack 로 통과하면 비발동" not in out
