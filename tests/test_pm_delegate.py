"""pm_delegate.py 엔진 코어 단위 테스트 — cross-harness 역할 위임 채널 (ADR-0075·sealed spike).

전부 mock(run_fn DI) — 실 하네스 바이너리는 절대 호출하지 않는다(라이브는 T-0449). 검증 축(ticket DoD):
  ① 3 드라이버 argv 정확성(3하네스×4역할·codex 전역옵션 위치·cwd 핀·reasoning 드라이버 매핑·§3.3).
  ② config 해소 원자성(§3.2) — CLI 완전지정 미참조·부분 override 거부·hard fail-loud·미매핑 fail-loud·
     티어 세트 통째·비-개발 tier usage error.
  ③ reasoning 드라이버별 허용집합(codex xhigh 수용·밖 fail-loud·claude/opencode 미확정 fail-loud·§6).
  ④ opt-in 게이트 disabled = rc=3(§5.4).
  ⑤ prompt-file containment 거부(§3.1·§4.6).
  ⑥ env allowlist 정제(PM 세션 타 크리덴셜 미상속·§4.7).
  ⑦ 권한 역할축 매핑(read 역할 write 도구 부재·mock 범위·§3.5).
  ⑧ 쓰기-타깃 axis 재앵커(엔진 코드 write + PM 홈 cwd fail-loud·PM-doc 통과·§4.6).
  ⑨ dry-run 출력(§3.1).
  ⑩ 결과 수집 — reply 추출·빈 reply/rc≠0/timeout fail-loud·raw O_EXCL/0600 박제(§3.4).
  ⑪ 시크릿 denylist 스캔(§4.7).
"""
from __future__ import annotations

import importlib.util
import json as _json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate", TOOLS / "pm_delegate.py")


# ── canned 하네스 출력 (파서 재사용 확인·pm_relay 실측 형식) ──────────────────────

def _codex_stdout(reply: str = "DONE") -> str:
    return "\n".join([
        _json.dumps({"type": "thread.started", "thread_id": "th1"}),
        _json.dumps({"type": "item.completed",
                     "item": {"type": "agent_message", "text": reply}}),
        _json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])


def _claude_stdout(reply: str = "DONE") -> str:
    return _json.dumps({"type": "result", "result": reply, "session_id": "s1"})


def _opencode_stdout(reply: str = "DONE") -> str:
    return _json.dumps({"type": "text", "sessionID": "ses_1",
                        "part": {"type": "text", "text": reply}})


class _FakeRun:
    """run_fn seam stub — argv/env/cwd/stdin 을 기록하고 canned RunResult 반환(subprocess 폭발 0)."""

    def __init__(self, stdout: str = "", returncode: int = 0,
                 stderr: str = "", timed_out: bool = False):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append({"argv": argv, "stdin_text": stdin_text, "cwd": cwd,
                           "env": env, "timeout": timeout, "harness": harness})
        return {"returncode": self.returncode, "stdout": self.stdout,
                "stderr": self.stderr, "timed_out": self.timed_out}


def _write_prompt(tmp_path: Path, text: str = "티켓 본문: 구현하라.") -> Path:
    p = tmp_path / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


# ══ ① 드라이버 argv (3하네스×4역할·§3.3) ═════════════════════════════════════

def test_codex_argv_global_options_before_exec(pd):
    """codex `-a never -s <mode>` 는 exec **앞** 전역 옵션(exec 뒤는 rc=2·0.145.0 실측)."""
    argv = pd.build_codex_argv("gpt-x", None, "developer", "/w/t")
    assert argv[:6] == ["codex", "-a", "never", "-s", "workspace-write", "exec"]
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert argv[argv.index("-C") + 1] == "/w/t"
    assert argv[argv.index("-m") + 1] == "gpt-x"


def test_codex_argv_read_role_sandbox(pd):
    """read 역할(researcher/code-reviewer)은 codex `-s read-only`(기계적 차단·§3.5)."""
    for role in ("researcher", "code-reviewer"):
        argv = pd.build_codex_argv("m", None, role, "/w")
        assert argv[argv.index("-s") + 1] == "read-only"


def test_codex_argv_reasoning_flag(pd):
    """codex reasoning = `-c model_reasoning_effort=<r>`(§6)."""
    argv = pd.build_codex_argv("m", "xhigh", "developer", "/w")
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=xhigh"


def test_claude_argv_tools_by_role(pd):
    """claude `--tools` 역할별 도구셋(§3.5): write=편집포함·researcher=Bash 제외·reviewer=Bash 포함."""
    dev = pd.build_claude_argv("opus", None, "developer")
    assert dev[:6] == ["claude", "-p", "--output-format", "json", "--model", "opus"]
    dev_tools = dev[dev.index("--tools") + 1]
    assert "Write" in dev_tools and "Edit" in dev_tools and "Bash" in dev_tools
    assert "--permission-mode" in dev and dev[dev.index("--permission-mode") + 1] == "acceptEdits"

    res_tools = pd.build_claude_argv("opus", None, "researcher")
    rt = res_tools[res_tools.index("--tools") + 1]
    assert "Bash" not in rt and "Write" not in rt and "Edit" not in rt  # 순수읽기·기계적

    rev = pd.build_claude_argv("opus", None, "code-reviewer")
    rvt = rev[rev.index("--tools") + 1]
    assert "Bash" in rvt and "Write" not in rvt and "Edit" not in rvt  # 읽기+테스트(규율)
    assert "--permission-mode" not in rev  # read 역할은 write perm-mode 없음


def test_claude_argv_effort(pd):
    argv = pd.build_claude_argv("opus", "high", "developer")
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "high"


def test_opencode_argv_agent_and_dir(pd):
    """opencode `--file`·`--dir <cwd>`(cwd 무시 대응)·`--agent build|plan`(권한 강제·§3.3·D2)."""
    dev = pd.build_opencode_argv("prov/m", None, "developer", "/w/t", "/tmp/p.md")
    assert dev[:2] == ["opencode", "run"]
    assert dev[dev.index("--file") + 1] == "/tmp/p.md"
    assert dev[dev.index("--dir") + 1] == "/w/t"
    assert dev[dev.index("--agent") + 1] == "build"
    assert dev[dev.index("-m") + 1] == "prov/m"

    res = pd.build_opencode_argv("prov/m", None, "researcher", "/w", "/tmp/p.md")
    assert res[res.index("--agent") + 1] == "plan"


def test_opencode_argv_variant(pd):
    argv = pd.build_opencode_argv("prov/m", "medium", "architect", "/w", "/tmp/p.md")
    assert "--variant" in argv and argv[argv.index("--variant") + 1] == "medium"


# ══ ② config 해소 원자성 (§3.2) ══════════════════════════════════════════════

def _conf(**kw) -> dict:
    return dict(kw)


def test_resolve_config_maps_role(pd):
    conf = _conf(**{"delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
                    "delegate.developer.reasoning": "high"})
    assert pd.resolve_delegate(conf, "developer", "normal", None, None, None) == ("codex", "gpt-x", "high")


def test_resolve_cli_full_override_ignores_config(pd):
    """CLI 완전지정(--harness AND --model) = 설정 미참조(원자 override)."""
    conf = _conf(**{"delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"})
    assert pd.resolve_delegate(conf, "developer", "normal", "claude", "opus", None) == ("claude", "opus", None)


def test_resolve_hard_tier_read_wholesale(pd):
    """hard 티어는 delegate.developer.hard.* 세트를 통째로 읽는다(normal 혼합 상속 금지)."""
    conf = _conf(**{
        "delegate.developer.harness": "codex", "delegate.developer.model": "normal-m",
        "delegate.developer.hard.harness": "codex", "delegate.developer.hard.model": "hard-m",
        "delegate.developer.hard.reasoning": "high",
    })
    assert pd.resolve_delegate(conf, "developer", "hard", None, None, None) == ("codex", "hard-m", "high")


def test_resolve_hard_missing_fail_loud(pd):
    """hard 세트 미설정 → fail-loud(normal 강등 금지·§3.2)."""
    conf = _conf(**{"delegate.developer.harness": "codex", "delegate.developer.model": "normal-m"})
    with pytest.raises(pd.DelegateError, match="hard"):
        pd.resolve_delegate(conf, "developer", "hard", None, None, None)


def test_resolve_hard_incomplete_fail_loud(pd):
    """hard 세트 불완전(model 없음) → fail-loud(부분 채움 금지)."""
    conf = _conf(**{"delegate.developer.hard.harness": "codex"})
    with pytest.raises(pd.DelegateError):
        pd.resolve_delegate(conf, "developer", "hard", None, None, None)


def test_resolve_unmapped_fail_loud(pd):
    """역할 매핑 미설정 → fail-loud(조용한 claude 폴백 금지·ADR-0070 D5)."""
    with pytest.raises(pd.DelegateError, match="매핑 미설정"):
        pd.resolve_delegate(_conf(), "researcher", "normal", None, None, None)


def test_resolve_cli_partial_override_rejected(pd):
    """CLI 부분 override(--harness 만) → DelegateError(방어적·usage error 는 _validate_args)."""
    with pytest.raises(pd.DelegateError, match="동반 필수"):
        pd.resolve_delegate(_conf(), "developer", "normal", "codex", None, None)


def test_resolve_colon_model_id_whole(pd):
    """콜론 포함 모델 ID(.model 통째)는 모호 0 — 값을 그대로 통과."""
    conf = _conf(**{"delegate.developer.harness": "opencode",
                    "delegate.developer.model": "ollama/glm-5.2:cloud"})
    _h, model, _r = pd.resolve_delegate(conf, "developer", "normal", None, None, None)
    assert model == "ollama/glm-5.2:cloud"


def test_resolve_bad_harness_fail_loud(pd):
    conf = _conf(**{"delegate.developer.harness": "gemini", "delegate.developer.model": "m"})
    with pytest.raises(pd.DelegateError, match="미지원 harness"):
        pd.resolve_delegate(conf, "developer", "normal", None, None, None)


# ══ ③ reasoning 드라이버별 허용집합 (§6) ══════════════════════════════════════

def test_reasoning_codex_accepts_xhigh(pd):
    assert pd._validate_reasoning("codex", "xhigh") == "xhigh"


def test_reasoning_codex_out_of_set_fail_loud(pd):
    with pytest.raises(pd.DelegateError, match="허용집합"):
        pd._validate_reasoning("codex", "ultra")


def test_reasoning_claude_undetermined_fail_loud(pd):
    """claude/opencode reasoning 허용값 T-0449 실측 전 미확정 → 지정 시 fail-loud(조용한 무시 금지)."""
    with pytest.raises(pd.DelegateError, match="미확정"):
        pd._validate_reasoning("claude", "high")
    with pytest.raises(pd.DelegateError, match="미확정"):
        pd._validate_reasoning("opencode", "medium")


def test_reasoning_none_omits_flag(pd):
    assert pd._validate_reasoning("claude", None) is None
    assert pd._validate_reasoning("opencode", "") is None


# ══ ④~⑨ main 통합 (mock run_fn·usage/fail-loud/dry-run) ═════════════════════

def _enabled_conf(**extra) -> dict:
    base = {"delegate_enabled": "true",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    base.update(extra)
    return base


def _run_main(pd, monkeypatch, argv, conf, run_fn=None):
    monkeypatch.setattr(pd, "local_config", lambda: conf)
    # cwd git-repo 검증은 통과로 고정(테스트 tmp_path 는 git repo 가 아님) — 검증 자체는 전용 테스트에서.
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    return pd.main(argv, run_fn=run_fn)


def test_disabled_returns_rc3(pd, monkeypatch, tmp_path, capsys):
    """delegate_enabled=false → rc=3 + stderr(false-green 차단·rc=0 no-op 금지·§5.4)."""
    prompt = _write_prompt(tmp_path)
    conf = {"delegate_enabled": "false",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   conf, fake)
    assert rc == 3
    assert "비활성" in capsys.readouterr().err
    assert fake.calls == []  # 스폰 없음


def test_prompt_file_containment_reject(pd, monkeypatch, tmp_path, capsys):
    """prompt-file 이 cwd/.project_manager 밖 → fail-loud(§3.1·§4.6)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    prompt = _write_prompt(outside)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "repo 경계 밖" in capsys.readouterr().err
    assert fake.calls == []


def test_dry_run_outputs_argv_and_prompt(pd, monkeypatch, tmp_path, capsys):
    """dry-run = 합성 프롬프트 + argv 출력·미실행(비활성이어도 허용·rc=0)."""
    prompt = _write_prompt(tmp_path, "고유task마커")
    conf = {"delegate_enabled": "false",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--dry-run"], conf, fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out and "codex" in out and "고유task마커" in out
    assert "developer 서브에이전트" in out  # role preamble 합성 확인
    assert fake.calls == []  # 미실행


def test_successful_delegation_stdout_reply(pd, monkeypatch, tmp_path, capsys):
    """성공 위임 → 최종 reply 만 stdout·raw 박제·codex stdin 주입·§3.4."""
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout("최종답변"))
    outdir = tmp_path / "raw"
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--output-dir", str(outdir)], _enabled_conf(), fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().splitlines()[0] == "최종답변"
    call = fake.calls[0]
    assert call["harness"] == "codex"
    assert call["stdin_text"] is not None and "티켓 본문" in call["stdin_text"]  # 프롬프트 stdin
    assert list(outdir.glob("pm_delegate_codex_*.txt"))  # raw 박제


def test_empty_reply_fail_loud(pd, monkeypatch, tmp_path, capsys):
    """reply 미추출(빈 출력) → rc=1 + raw 경로(§3.4·false-green 차단)."""
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout="")
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "reply 미추출" in capsys.readouterr().err


def test_nonzero_rc_fail_loud(pd, monkeypatch, tmp_path, capsys):
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout="", returncode=2)
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "하네스 실패" in capsys.readouterr().err


def test_timeout_fail_loud(pd, monkeypatch, tmp_path, capsys):
    """timeout(프로세스그룹 종료) → rc=1 + raw(§5.3)."""
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout="", timed_out=True)
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "타임아웃" in capsys.readouterr().err


def test_opencode_prompt_file_channel(pd, monkeypatch, tmp_path):
    """opencode 는 합성 프롬프트를 임시 파일로 --file 전달(stdin 아님)·§3.3. 파일은 run 중에만 존재
    (이후 삭제)이라 내용은 run_fn 안에서 확인한다."""
    prompt = _write_prompt(tmp_path)
    conf = _enabled_conf(**{"delegate.developer.harness": "opencode",
                            "delegate.developer.model": "prov/m"})
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["stdin_text"] = stdin_text
        file_arg = argv[argv.index("--file") + 1]
        seen["content"] = Path(file_arg).read_text(encoding="utf-8")
        return {"returncode": 0, "stdout": _opencode_stdout("oc답"), "stderr": "", "timed_out": False}

    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--output-dir", str(tmp_path / "raw")], conf, _capture)
    assert rc == 0
    assert seen["stdin_text"] is None  # opencode 는 stdin 미사용
    assert seen["content"].startswith("너는 이 프로젝트의 developer")  # 합성 프롬프트 --file


# ══ ⑤ 비-개발 tier / 부분 override usage error (rc=2·argparse) ════════════════

def test_non_dev_tier_usage_error(pd, monkeypatch, tmp_path):
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "researcher", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                 "--tier", "hard"])
    assert ei.value.code == 2


def test_partial_cli_override_usage_error(pd, monkeypatch, tmp_path):
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                 "--harness", "codex"])  # --model 없음
    assert ei.value.code == 2


def test_reasoning_without_override_usage_error(pd, monkeypatch, tmp_path):
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                 "--reasoning", "high"])
    assert ei.value.code == 2


def test_relative_cwd_usage_error(pd, monkeypatch, tmp_path):
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", "relative/path"])
    assert ei.value.code == 2


# ══ ⑥ env allowlist (§4.7) ═══════════════════════════════════════════════════

def test_build_env_allowlist_excludes_foreign_creds(pd, monkeypatch):
    """PM 세션 타 크리덴셜(FOO_API_KEY) 미상속·PATH/HOME/LC_*·하네스 인증 키만 전달(§4.7)."""
    monkeypatch.setenv("FOO_API_KEY", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("CODEX_HOME", "/iso/codex")
    env = pd.build_env("codex")
    assert "FOO_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin"
    assert env.get("HOME") == "/home/u"
    assert env.get("LC_ALL") == "C.UTF-8"
    assert env.get("CODEX_HOME") == "/iso/codex"


def test_env_allowlist_applied_in_main(pd, monkeypatch, tmp_path):
    """main 이 정제된 env 를 run_fn 에 넘긴다 — 실행 경로에서도 leak 차단."""
    monkeypatch.setenv("SECRET_TOKEN", "xyz")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    _run_main(pd, monkeypatch,
              ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
               "--output-dir", str(tmp_path / "raw")], _enabled_conf(), fake)
    assert "SECRET_TOKEN" not in fake.calls[0]["env"]


# ══ ⑦ 권한 역할축 매핑 (read 역할 write 차단·mock 범위·§3.5) ═════════════════

def test_read_role_permission_axis(pd):
    """read 역할(researcher/code-reviewer) argv 가 write 권한을 부여하지 않음(mock 범위 검증)."""
    # codex: read-only sandbox
    assert pd.build_codex_argv("m", None, "researcher", "/w")[4] == "read-only"
    # claude researcher: Write/Edit/Bash 부재(기계적)
    rt = pd.build_claude_argv("m", None, "researcher")
    tools = rt[rt.index("--tools") + 1]
    assert not any(t in tools for t in ("Write", "Edit", "Bash"))
    # opencode reviewer: plan agent(권한 강제)
    assert pd.build_opencode_argv("m", None, "code-reviewer", "/w", "/p")[
        pd.build_opencode_argv("m", None, "code-reviewer", "/w", "/p").index("--agent") + 1] == "plan"


# ══ ⑧ 쓰기-타깃 axis 재앵커 (§4.6) ════════════════════════════════════════════

def _fake_pm_home(tmp_path: Path) -> tuple[Path, Path]:
    """실 board 소유(board/tickets/open/T-*.md) + canonical worktree(work/wt/.project_manager/tools/
    external_review.py)를 가진 PM 홈 형상 구성 — external_review._pm_home_reanchor 판정 대상."""
    home = tmp_path / "pmhome"
    open_dir = home / ".project_manager" / "board" / "tickets" / "open"
    open_dir.mkdir(parents=True)
    (open_dir / "T-0001-x.md").write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    wt_tools = home / "work" / "wt1" / ".project_manager" / "tools"
    wt_tools.mkdir(parents=True)
    (wt_tools / "external_review.py").write_text("# stub", encoding="utf-8")
    return home, home / "work" / "wt1"


def test_write_target_reanchor_engine_code(pd, tmp_path):
    """write 역할 + 엔진 코드(.project_manager/tools/) 타깃 + PM 홈 cwd → 재앵커 대상 반환(§4.6)."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = "다음 파일을 수정하라: .project_manager/tools/board.py 의 함수"
    assert pd.check_write_target_reanchor("developer", home, prompt) == wt


def test_write_target_pm_doc_passes(pd, tmp_path):
    """PM-doc(wiki) write 는 PM 홈 cwd 정당 → 재앵커 안 함(§4.6)."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = "wiki/roadmap.md 를 갱신하라"
    assert pd.check_write_target_reanchor("architect", home, prompt) is None


def test_read_role_no_reanchor(pd, tmp_path):
    """read 역할은 엔진 코드 언급이 있어도 재앵커 안 함(write 타깃 아님·§4.6)."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = ".project_manager/tools/board.py 를 조사하라"
    assert pd.check_write_target_reanchor("researcher", home, prompt) is None


def test_reanchor_fail_loud_in_main(pd, monkeypatch, tmp_path, capsys):
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text("수정: .project_manager/tools/pm_delegate.py", encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
                   _enabled_conf(), fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


# ══ ⑪ 시크릿 denylist 스캔 + raw 파일 보안 (§4.7·§3.4) ═══════════════════════

def test_secret_scan_blocks_denylist_token(pd):
    hit = pd.scan_prompt_secrets("배포 전에 config.secret.key 를 확인하라")
    assert hit is not None


def test_secret_scan_clean_prompt(pd):
    assert pd.scan_prompt_secrets("board.py 의 함수를 리팩터하라") is None


def test_secret_scan_fail_loud_in_main(pd, monkeypatch, tmp_path, capsys):
    prompt = _write_prompt(tmp_path, "여기 credentials.env 파일 내용을 참고")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "denylist" in err
    assert "credentials.env" not in err  # 원문 토큰 미노출·패턴명만(suggestion)
    assert fake.calls == []


def test_save_raw_output_o_excl_0600(pd, tmp_path):
    """raw 박제 = O_EXCL 원자 생성 + mode 0600(감사·권한 유출 회귀·§3.4)."""
    dest = pd.save_raw_output("codex", "raw content", tmp_path)
    assert dest.exists()
    assert dest.name.startswith("pm_delegate_codex_")
    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode == 0o600


def test_extract_reply_parsers(pd):
    """pm_relay 3파서 재사용 — 하네스별 최종 reply 추출(§3.4)."""
    assert pd.extract_reply("codex", _codex_stdout("cx")) == "cx"
    assert pd.extract_reply("claude", _claude_stdout("cl")) == "cl"
    assert pd.extract_reply("opencode", _opencode_stdout("oc")) == "oc"


# ══ must-fix 1: containment 우회 폐쇄 (경로 성분 매칭 폐기·§4.6) ═══════════════

def test_containment_external_project_manager_rejected(pd, tmp_path, monkeypatch):
    """외부 `.project_manager/` 경로(cwd 무관)는 거부 — 옛 경로 성분 매칭 우회 폐쇄."""
    repo = tmp_path / "repo"
    (repo / ".project_manager").mkdir(parents=True)
    monkeypatch.setattr(pd, "REPO", repo)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    outside = tmp_path / "outside" / ".project_manager"
    outside.mkdir(parents=True)
    prompt = outside / "secret.txt"
    prompt.write_text("x", encoding="utf-8")
    assert pd._prompt_file_contained(prompt, cwd) is False


def test_containment_repo_pm_home_allowed(pd, tmp_path, monkeypatch):
    """이 repo PM 홈(REPO/.project_manager) 하위는 cwd 밖이어도 허용(b 루트)."""
    repo = tmp_path / "repo"
    (repo / ".project_manager").mkdir(parents=True)
    monkeypatch.setattr(pd, "REPO", repo)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    prompt = repo / ".project_manager" / "task.md"
    prompt.write_text("x", encoding="utf-8")
    assert pd._prompt_file_contained(prompt, cwd) is True


def test_containment_symlink_escape_rejected(pd, tmp_path, monkeypatch):
    """cwd 안 symlink 가 밖 파일을 가리키면 realpath 이탈로 거부(symlink 우회 차단)."""
    repo = tmp_path / "repo"
    (repo / ".project_manager").mkdir(parents=True)
    monkeypatch.setattr(pd, "REPO", repo)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    link = cwd / "link.md"
    link.symlink_to(secret)
    assert pd._prompt_file_contained(link, cwd) is False


def test_cwd_root_usage_error(pd, monkeypatch, tmp_path):
    """`--cwd /`(파일시스템 루트)는 usage error — 전 파일시스템을 containment 로 여는 우회 차단."""
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", "/"])
    assert ei.value.code == 2


# ══ must-fix 2: launch 오류 정규화 (_default_run_fn·traceback 금지) ═══════════

class _FakeRelayWatchdog:
    """opencode 경로용 fake relay — run_with_first_event_watchdog 이 주입 예외를 raise."""

    StallWatchdogError = type("StallWatchdogError", (RuntimeError,), {})

    def __init__(self, exc):
        self._exc = exc

    def first_event_timeout_default(self):
        return 1.0

    def stall_retries_default(self):
        return 0

    def run_with_first_event_watchdog(self, *a, **k):
        raise self._exc


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_default_run_fn_launch_error_codex_claude(pd, monkeypatch, harness):
    """codex/claude 바이너리 미설치(FileNotFoundError) → rc≠0·진단(traceback 아님)."""
    def _boom(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    monkeypatch.setattr(pd.subprocess, "Popen", _boom)
    monkeypatch.setattr(pd, "_load_relay", lambda: object())
    res = pd._default_run_fn(["bin"], stdin_text="x", cwd="/tmp", env={}, timeout=1, harness=harness)
    assert res["returncode"] != 0 and res["timed_out"] is False
    assert "실행 불가" in res["stderr"]


def test_default_run_fn_launch_error_opencode(pd, monkeypatch):
    """opencode 바이너리 미설치(워치독 Popen FileNotFoundError) → rc≠0·진단."""
    monkeypatch.setattr(pd, "_load_relay",
                        lambda: _FakeRelayWatchdog(FileNotFoundError(2, "nope", "opencode")))
    res = pd._default_run_fn(["opencode", "run"], stdin_text=None, cwd="/tmp", env={},
                             timeout=1, harness="opencode")
    assert res["returncode"] != 0 and "실행 불가" in res["stderr"]


# ══ must-fix 3: timeout 프로세스그룹 kill 검증 (3드라이버·§5.3) ════════════════

class _FakeTimeoutProc:
    """communicate 가 첫 호출 TimeoutExpired·kill 후(2번째)엔 빈 출력 반환(수확)."""

    def __init__(self):
        self.pid = 4321
        self._calls = 0
        self.returncode = -9

    def communicate(self, input=None, timeout=None):
        self._calls += 1
        if self._calls == 1:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return ("", "")


class _SpyRelayKill:
    def __init__(self):
        self.kill_calls = []

    def _kill_process_group(self, proc):
        self.kill_calls.append(proc)


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_default_run_fn_timeout_killpg_codex_claude(pd, monkeypatch, harness):
    """codex/claude timeout → _kill_process_group 호출(그룹째 종료)·start_new_session 분리·§5.3."""
    made = {}

    def _fake_popen(argv, **kw):
        made["proc"] = _FakeTimeoutProc()
        made["kwargs"] = kw
        return made["proc"]

    spy = _SpyRelayKill()
    monkeypatch.setattr(pd.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pd, "_load_relay", lambda: spy)
    res = pd._default_run_fn(["bin"], stdin_text="x", cwd="/tmp", env={}, timeout=1, harness=harness)
    assert res["timed_out"] is True and res["returncode"] != 0
    assert spy.kill_calls and spy.kill_calls[0] is made["proc"]
    if os.name == "posix":
        assert made["kwargs"].get("start_new_session") is True  # 프로세스그룹 분리(kill 대상)


def test_default_run_fn_timeout_opencode(pd, monkeypatch):
    """opencode timeout(워치독이 그룹째 kill 후 TimeoutExpired 전파) → timed_out=True·§5.3."""
    monkeypatch.setattr(pd, "_load_relay",
                        lambda: _FakeRelayWatchdog(subprocess.TimeoutExpired(cmd="oc", timeout=1)))
    res = pd._default_run_fn(["opencode", "run"], stdin_text=None, cwd="/tmp", env={},
                             timeout=1, harness="opencode")
    assert res["timed_out"] is True and res["returncode"] != 0


# ══ must-fix 3(보완): 3하네스×4역할 argv 매트릭스 전수(12·권한축) ════════════

@pytest.mark.parametrize("role", ["developer", "researcher", "architect", "code-reviewer"])
@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_argv_matrix_permission_axis(pd, harness, role):
    """3하네스×4역할 12조합 전수 — 권한축(write=developer/architect·read=나머지)이 argv 에 정확 반영."""
    write = role in ("developer", "architect")
    if harness == "codex":
        argv = pd.build_codex_argv("m", None, role, "/w")
        assert argv[argv.index("-s") + 1] == ("workspace-write" if write else "read-only")
    elif harness == "claude":
        argv = pd.build_claude_argv("m", None, role)
        tools = argv[argv.index("--tools") + 1]
        if write:
            assert "Write" in tools and "Edit" in tools
            assert "--permission-mode" in argv
        else:
            assert "Write" not in tools and "Edit" not in tools
            assert "--permission-mode" not in argv
            assert ("Bash" in tools) == (role == "code-reviewer")  # reviewer 만 Bash(pytest)
    else:  # opencode
        argv = pd.build_opencode_argv("m", None, role, "/w", "/p")
        assert argv[argv.index("--agent") + 1] == ("build" if write else "plan")


# ══ suggestion: --timeout / delegate_timeout 양의 정수 검증 ═══════════════════

def test_timeout_nonpositive_usage_error(pd, monkeypatch, tmp_path):
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                 "--timeout", "0"])
    assert ei.value.code == 2


def test_conf_delegate_timeout_failsoft(pd):
    """conf delegate_timeout 비정수/≤0 은 traceback 대신 기본으로 fail-soft·유효값은 채택."""
    ns = type("NS", (), {"timeout": None})()
    assert pd._resolve_timeout(ns, {"delegate_timeout": "abc"}) == pd.DELEGATE_TIMEOUT_SECONDS
    assert pd._resolve_timeout(ns, {"delegate_timeout": "-5"}) == pd.DELEGATE_TIMEOUT_SECONDS
    assert pd._resolve_timeout(ns, {"delegate_timeout": "600"}) == 600
    ns2 = type("NS", (), {"timeout": 42})()
    assert pd._resolve_timeout(ns2, {}) == 42


# ══ R2 must-fix 1: opencode positional message 필수 ══════════════════════════

def test_opencode_argv_has_message_positional(pd):
    """opencode run 은 --file 이 있어도 비어있지 않은 positional message 를 요구(부재 시 rc=1·실측)."""
    argv = pd.build_opencode_argv("m", None, "developer", "/w", "/p")
    assert argv[:3] == ["opencode", "run", pd._OPENCODE_ATTACHED_MSG]
    assert argv[2].strip()  # 비어있지 않음


# ══ R2 must-fix 2: opt-in 게이트가 매핑 해소보다 앞 (빈 config → rc=3) ═════════

def test_empty_config_disabled_rc3(pd, monkeypatch, tmp_path, capsys):
    """빈 config(기본 OFF·매핑 없음) 새 설치 → rc=3(disabled), NOT rc=1(매핑 미설정)·§5.4."""
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: {})
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                 run_fn=fake)
    assert rc == 3
    assert "비활성" in capsys.readouterr().err
    assert fake.calls == []


# ══ R2 must-fix 3a: cwd = git 저장소 루트/하위 검증 ═══════════════════════════

def test_cwd_in_git_repo_unit(pd):
    ok = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "/repo\n"})()
    bad = lambda *a, **k: type("R", (), {"returncode": 128, "stdout": ""})()
    assert pd._cwd_in_git_repo(Path("/x"), ok) is True
    assert pd._cwd_in_git_repo(Path("/x"), bad) is False

    def _boom(*a, **k):
        raise FileNotFoundError("git")
    assert pd._cwd_in_git_repo(Path("/x"), _boom) is False


def test_cwd_not_git_repo_fail_loud(pd, monkeypatch, tmp_path, capsys):
    """비-repo(광범위 홈 등) cwd → fail-loud rc=1(신뢰 작업공간 아님)."""
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    fake = _FakeRun(stdout=_codex_stdout())
    bad_git = lambda *a, **k: type("R", (), {"returncode": 128, "stdout": ""})()
    rc = pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                 run_fn=fake, git_run_fn=bad_git)
    assert rc == 1
    assert "git 저장소" in capsys.readouterr().err
    assert fake.calls == []


# ══ R2 must-fix 3b: prompt-file 경로 자체 denylist (내용 읽기 전·패턴명만) ══════

def test_prompt_file_path_denylist_reject(pd, monkeypatch, tmp_path, capsys):
    """prompt-file 이름이 denylist(credential/.env 류)면 내용 읽기 전 차단·패턴명만 노출·§4.7b."""
    secret_prompt = tmp_path / "credentials.env"
    secret_prompt.write_text("task", encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(secret_prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "denylist" in err
    assert "credentials.env" not in err  # 원문 파일명 미노출·패턴명만
    assert fake.calls == []


# ══ R2 must-fix 4: engine.manifest 등록 + templates 3종 동기 ══════════════════

def test_pm_delegate_registered_in_root_manifest():
    manifest = (REPO / ".project_manager" / "engine.manifest").read_text(encoding="utf-8")
    assert ".project_manager/tools/pm_delegate.py" in manifest


@pytest.mark.parametrize("target", ["claude_code", "opencode", "codex"])
def test_pm_delegate_registered_and_synced_in_template(target):
    """3종 템플릿 manifest 에 등록 + 파일이 canonical 과 byte-identical(전파 채널 정합·pm_update --target)."""
    base = REPO / "templates" / target / ".project_manager"
    manifest = (base / "engine.manifest").read_text(encoding="utf-8")
    assert ".project_manager/tools/pm_delegate.py" in manifest
    tmpl_file = base / "tools" / "pm_delegate.py"
    assert tmpl_file.is_file()
    canonical = (REPO / ".project_manager" / "tools" / "pm_delegate.py").read_bytes()
    assert tmpl_file.read_bytes() == canonical  # byte-identical(공유 엔진 파일 정합 가드)


# ══ R3 must-fix 1: symlink 해소 순서 — 원본+해소 경로 양쪽 denylist ═══════════

def test_prompt_file_denylist_symlink_resolved(pd, tmp_path):
    """prompt.md → .env symlink 은 resolve() 해소 경로(.env)가 denylist 에 걸린다(원본만 보던 우회 폐쇄)."""
    secret = tmp_path / ".env"
    secret.write_text("x", encoding="utf-8")
    link = tmp_path / "prompt.md"
    link.symlink_to(secret)
    assert pd._prompt_file_denylist_pattern(link) is not None


def test_prompt_file_symlink_to_secret_rejected(pd, monkeypatch, tmp_path, capsys):
    """prompt.md → <cwd>/.env symlink 위임 → 내용 읽기 전 fail-loud(시크릿 읽기 차단)."""
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")
    link = tmp_path / "prompt.md"
    link.symlink_to(secret)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(link), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "denylist" in capsys.readouterr().err
    assert fake.calls == []  # 시크릿 전송 없음


# ══ R3 must-fix 2: denylist 토큰 정규화 (구두점·할당문·소문자) ═════════════════

def test_secret_scan_token_normalization(pd):
    """공백 스캔이 놓치던 표기 3종을 강화 토큰화로 잡는다."""
    assert pd.scan_prompt_secrets("파일 .env. 를 확인") is not None       # trailing 마침표
    assert pd.scan_prompt_secrets("설정 path=.env 주의") is not None       # = 할당문 분리
    assert pd.scan_prompt_secrets("키 foo.pem. 참고") is not None          # trailing 마침표 + .pem
    assert pd.scan_prompt_secrets("키 KEY:secret.pem 참고") is not None     # : 할당문 분리
    assert pd.scan_prompt_secrets("board.py 를 리팩터하라") is None         # clean(오탐 0)


# ══ R3 must-fix 3: 재앵커 판정 경로 정규화 ═══════════════════════════════════

def test_targets_engine_code_path_normalization(pd):
    """정확 문자열 매칭이 아니라 성분 시퀀스 판정 — trailing slash 없음·`./` 우회 표기도 잡는다."""
    assert pd._prompt_targets_engine_code("수정 .project_manager/tools/board.py 함수") is True
    assert pd._prompt_targets_engine_code("대상 .project_manager/tools") is True          # trailing slash 없음
    assert pd._prompt_targets_engine_code("수정 .project_manager/./tools/x.py") is True    # ./ 우회
    assert pd._prompt_targets_engine_code("wiki/roadmap.md 를 갱신하라") is False          # PM-doc(통과)
    assert pd._prompt_targets_engine_code(".project_manager/wiki/x.md 초안") is False      # tools 아님


def test_reanchor_normalized_variants_in_main(pd, monkeypatch, tmp_path, capsys):
    """trailing-slash 없는 엔진 코드 타깃 + PM 홈 cwd → 재앵커 fail-loud(우회 표기도 게이트)."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text("수정 대상: .project_manager/tools", encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert "재앵커" in capsys.readouterr().err
    assert fake.calls == []


# ══ R3 suggestion: opencode 합성 프롬프트 임시 파일 삭제(raw 결과만 보존) ═══════

def test_opencode_prompt_tempfile_cleaned_up(pd, monkeypatch, tmp_path):
    """opencode 합성 프롬프트 임시 파일은 run 후 삭제(raw 결과만 보존)."""
    prompt = _write_prompt(tmp_path)
    conf = _enabled_conf(**{"delegate.developer.harness": "opencode",
                            "delegate.developer.model": "prov/m"})
    outdir = tmp_path / "raw"
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        file_arg = argv[argv.index("--file") + 1]
        seen["prompt_file"] = Path(file_arg)
        seen["existed_during_run"] = Path(file_arg).is_file()
        return {"returncode": 0, "stdout": _opencode_stdout("oc"), "stderr": "", "timed_out": False}

    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--output-dir", str(outdir)], conf, _capture)
    assert rc == 0
    assert seen["existed_during_run"] is True       # run 중엔 존재(--file 로 전달)
    assert not seen["prompt_file"].exists()          # run 후 삭제됨
    assert list(outdir.glob("pm_delegate_opencode_*.txt"))  # raw 결과는 보존
