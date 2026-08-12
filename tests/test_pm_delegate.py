"""pm_delegate.py 엔진 코어 단위 테스트 — cross-harness 역할 위임 채널 (ADR-0075·sealed spike).

대부분 mock(run_fn DI)이며, 권한 계약만 실제 codex sandbox opt-in과 무과금 opencode `agent list`
통합으로 고정한다. 검증 축(ticket DoD):
  ① 3 드라이버 argv 정확성(3하네스×4역할·codex 전역옵션 위치·cwd 핀·reasoning 드라이버 매핑·§3.3).
  ② config 해소 원자성(§3.2) — CLI 완전지정 미참조·부분 override 거부·hard fail-loud·미매핑 fail-loud·
     티어 세트 통째·비-개발 tier usage error.
  ③ reasoning 드라이버별 허용집합(codex xhigh·claude effort·opencode variant T-0449 실측값 수용·밖 fail-loud·§6).
  ④ opt-in 게이트 disabled = rc=3(§5.4).
  ⑤ prompt-file containment 거부(§3.1·§4.6).
  ⑥ env allowlist 정제(PM 세션 타 크리덴셜 미상속·§4.7).
  ⑦ 권한 역할축 매핑(read 역할 write 도구 부재·mock 범위·§3.5).
  ⑧ 쓰기-타깃 axis 재앵커(엔진 코드 write + PM 홈 cwd fail-loud·PM-doc 통과·§4.6).
  ⑨ dry-run 출력(§3.1).
  ⑩ 결과 수집 — reply 추출·빈 reply/rc≠0/timeout fail-loud·raw O_EXCL/0600 박제(§3.4).
  ⑪ 시크릿 denylist 스캔(§4.7).
  ⑫ Codex egress 승격 브리지 — network-off 마커 환경의 증명 게이트·dry-run 표시·감사 라벨(T-0592).
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import importlib.util
import json as _json
import os
import re
import shutil
import stat
import subprocess
import sys
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
    # 출력 형식 = stream-json(+CLI 강제 `--verbose`) — 진행 신호 축 승격(T-0489 ④).
    assert dev[:7] == ["claude", "-p", "--output-format", "stream-json", "--verbose",
                       "--model", "opus"]
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


def test_reasoning_claude_opencode_measured_sets(pd):
    """claude/opencode reasoning 허용집합 = T-0449 라이브 실측값 — 안은 수용·밖은 fail-loud(§6).

    claude `--effort` = {low,medium,high,xhigh,max}(CLI 경고 authoritative)·opencode `--variant` =
    {minimal,low,medium,high,max}(문서 ladder·CLI passthrough typo-guard). 실측 전 '미확정 빈 집합'을
    대체 — 허용값은 통과하고 집합 밖(오타)만 fail-loud 함을 못박는다."""
    for r in ("low", "medium", "high", "xhigh", "max"):
        assert pd._validate_reasoning("claude", r) == r
    for r in ("minimal", "low", "medium", "high", "max"):
        assert pd._validate_reasoning("opencode", r) == r
    # 집합 밖 = fail-loud(조용한 무시/강등 금지).
    with pytest.raises(pd.DelegateError, match="허용집합"):
        pd._validate_reasoning("claude", "ultra")
    with pytest.raises(pd.DelegateError, match="허용집합"):
        pd._validate_reasoning("opencode", "bogus")
    # 드라이버별 집합이 실제로 다르다 — codex/claude 는 `xhigh`·`max` 를 함께 수용하지만
    # (T-0590 에서 codex `max` 편입: 추가 리뷰어 기본 프로필 ladder 상단), opencode 는 `xhigh` 가
    # 없다. 집합이 서로의 사본이 아님을 이 대비로 못박는다.
    assert pd._validate_reasoning("codex", "max") == "max"
    with pytest.raises(pd.DelegateError, match="허용집합"):
        pd._validate_reasoning("opencode", "xhigh")


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
    isolated_argv = list(argv)
    if "--output-dir" not in isolated_argv and "--prompt-file" in isolated_argv:
        prompt = Path(isolated_argv[isolated_argv.index("--prompt-file") + 1])
        raw_dir = prompt.parent / "raw"
        if "--cwd" in isolated_argv:
            cwd = Path(isolated_argv[isolated_argv.index("--cwd") + 1])
            raw_dir = cwd.parent / f".{cwd.name}-raw"
        isolated_argv += ["--output-dir", str(raw_dir)]
    return pd.main(isolated_argv, run_fn=run_fn)


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


def test_t0658_code_reviewer_dry_run_prompt_contains_parser_derived_contract(
    pd, monkeypatch, tmp_path, capsys,
):
    """code-reviewer 실 합성 prompt가 판정 선언·목록 0건·허용 토큰 계약을 모두 싣는다."""
    prompt = _write_prompt(tmp_path, "T-0658 리뷰 요청")
    conf = {
        "delegate_enabled": "false",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-review",
    }
    fake = _FakeRun(stdout=_codex_stdout())

    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "code-reviewer", "--prompt-file", str(prompt),
            "--cwd", str(tmp_path), "--ticket", "T-0658", "--dry-run",
        ],
        conf,
        fake,
    )
    out = capsys.readouterr().out
    external = pd._load_external_review()

    assert rc == 0 and fake.calls == []
    assert "행 선두" in out and "인용문·코드펜스" in out
    assert "`판정: 통과` 또는 `판정: 반려`" in out
    assert "must-fix 절은 markdown 제목 `## must-fix`" in out
    assert "목록으로 쓴다" in out and "`- 없음` 한 항목" in out
    assert "산문 `must-fix 없습니다`는 0건으로 읽히지 않는다" in out
    assert "판정 낱말은 파서 허용 토큰 중 하나만" in out
    for token in external._PASS_VERDICT_TOKENS | external._REJECT_VERDICT_TOKENS:
        assert f"`판정: {token}`" in out
    for token in pd._INTERNAL_NONE_ITEM_TOKENS:
        assert f"`- {token}`" in out


def test_t0658_review_contract_tracks_parser_token_sources_without_literal_copy(
    pd, monkeypatch,
):
    """허용 토큰 원천을 바꾸면 preamble도 즉시 따라가며 0건 regex와 안내도 한 tuple을 읽는다."""
    class _ContractProbe:
        _PASS_VERDICT_TOKENS = frozenset({"PROBE_PASS"})
        _REJECT_VERDICT_TOKENS = frozenset({"PROBE_REJECT"})

    monkeypatch.setattr(pd, "_load_external_review", lambda: _ContractProbe)
    preamble = pd._internal_review_format_preamble()

    assert "`판정: PROBE_PASS`" in preamble
    assert "`판정: PROBE_REJECT`" in preamble
    for token in pd._INTERNAL_NONE_ITEM_TOKENS:
        assert pd._INTERNAL_NONE_ITEM_RE.fullmatch(token)
        assert f"`- {token}`" in preamble


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
        seen["prompt_file"] = Path(file_arg)
        seen["prepared_prompt"] = Path(file_arg).resolve()
        seen["content"] = Path(file_arg).read_text(encoding="utf-8")
        return {"returncode": 0, "stdout": _opencode_stdout("oc답"), "stderr": "", "timed_out": False}

    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--output-dir", str(tmp_path / "raw")], conf, _capture)
    assert rc == 0
    assert seen["stdin_text"] is None  # opencode 는 stdin 미사용
    assert seen["content"].startswith("너는 이 프로젝트의 developer")  # 합성 프롬프트 --file
    assert seen["prepared_prompt"].is_relative_to(tmp_path.resolve())


def test_opencode_transport_copy_non_repo_is_o_excl_0600(pd, monkeypatch, tmp_path):
    """비-repo ``--cwd``에도 sandbox 내부 wire 디렉터리를 만들고 O_EXCL·0600을 지킨다."""
    cwd = tmp_path / "plain-directory"
    cwd.mkdir()
    fixed_uuid = type("FixedUuid", (), {"hex": "fixed-transport-id"})()
    monkeypatch.setattr(pd.uuid, "uuid4", lambda: fixed_uuid)

    prompt_path = pd._save_opencode_transport_prompt(cwd, "합성 프롬프트")

    assert prompt_path.parent.parent == cwd / ".project_manager" / ".local" / "delegate"
    assert prompt_path.name == "prompt.md"
    assert (prompt_path.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert prompt_path.read_text(encoding="utf-8") == "합성 프롬프트"
    assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        pd._save_opencode_transport_prompt(cwd, "덮어쓰기 금지")
    assert prompt_path.read_text(encoding="utf-8") == "합성 프롬프트"
    pd._cleanup_attempt_transport(prompt_path)
    assert not (cwd / ".project_manager").exists()


def test_opencode_unsupported_platform_fails_closed_before_create(
        pd, monkeypatch, tmp_path, capsys):
    """dir_fd/O_NOFOLLOW 미지원 시 경로 폴백 없이 cross 위임 rc=1과 처방을 낸다."""
    prompt = _write_prompt(tmp_path)
    conf = _enabled_conf(**{
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "prov/m",
    })
    fake = _FakeRun(stdout=_opencode_stdout("호출되면 안 됨"))
    monkeypatch.setattr(pd, "_OPENCODE_TRANSPORT_FD_SUPPORTED", False)

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        conf, fake,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "fail-closed" in err
    assert "Linux/POSIX" in err and "codex/claude" in err
    assert fake.calls == []
    assert not (tmp_path / ".project_manager").exists()


def test_opencode_transport_save_is_wired_to_containment_guard(
        pd, monkeypatch, tmp_path):
    """공용 containment 가드를 지우면 깨지도록 실제 save 진입점의 배선을 고정한다."""
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    calls = []

    class RejectingRelay:
        class HarnessContractError(Exception):
            pass

        @staticmethod
        def assert_opencode_prompt_in_cwd(cwd_arg, prompt_file):
            calls.append((Path(cwd_arg), Path(prompt_file)))
            raise RejectingRelay.HarnessContractError("배선 가드 거부")

    monkeypatch.setattr(pd, "_load_relay", lambda: RejectingRelay)

    with pytest.raises(pd.DelegateError, match="배선 가드 거부"):
        pd._save_opencode_transport_prompt(cwd, "합성 프롬프트")

    assert len(calls) == 1
    assert calls[0][0] == cwd
    assert not (cwd / ".project_manager").exists()


def test_opencode_execute_checks_containment_again_immediately_before_send(
        pd, monkeypatch, tmp_path):
    """실행 진입점은 생성 전 검사에 더해 argv 조립 직전 2차 containment를 수행한다."""
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    original_assert = pd._assert_opencode_transport_path
    calls = []

    def _counting_assert(cwd_arg, prompt_file):
        calls.append((Path(cwd_arg), Path(prompt_file)))
        original_assert(cwd_arg, prompt_file)

    monkeypatch.setattr(pd, "_assert_opencode_transport_path", _counting_assert)

    pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=cwd,
        prompt="합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=lambda *a, **k: {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        },
        attempt="primary",
    )

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert not (cwd / ".project_manager").exists()


def test_opencode_overlapping_transport_cleanup_removes_shared_skeleton_quietly(
        pd, tmp_path, capsys):
    """겹친 두 transport의 마지막 정리가 공유 골격을 회수하고 ENOTEMPTY를 경고하지 않는다."""
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    first = pd._save_opencode_transport_prompt(cwd, "첫 번째 합성 프롬프트")
    second = pd._save_opencode_transport_prompt(cwd, "두 번째 합성 프롬프트")

    pd._cleanup_attempt_transport(first)
    assert (cwd / ".project_manager" / ".local" / "delegate").is_dir()
    pd._cleanup_attempt_transport(second)

    assert not (cwd / ".project_manager").exists()
    assert capsys.readouterr().err == ""


def _track_transport_directory_fds(pd, monkeypatch):
    """transport가 os.open으로 연 디렉터리 fd 중 아직 close되지 않은 집합을 돌려준다."""
    live_fds = set()
    original_open = pd.os.open
    original_close = pd.os.close

    def _tracked_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if flags & pd.os.O_DIRECTORY:
            live_fds.add(fd)
        return fd

    def _tracked_close(fd):
        result = original_close(fd)
        live_fds.discard(fd)
        return result

    monkeypatch.setattr(pd.os, "open", _tracked_open)
    monkeypatch.setattr(pd.os, "close", _tracked_close)
    return live_fds


def test_opencode_chain_open_retries_after_parent_skeleton_disappears(
        pd, monkeypatch, tmp_path):
    """열어 둔 상위 골격이 정리되면 fd를 되감고 새 chain으로 재시도해 성공한다."""
    if not pd._OPENCODE_TRANSPORT_FD_SUPPORTED:
        pytest.skip("dir_fd/O_NOFOLLOW 보장 플랫폼 전용")
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    live_fds = _track_transport_directory_fds(pd, monkeypatch)
    original_mkdir = pd.os.mkdir
    original_rmdir = pd.os.rmdir
    injected = 0

    def _remove_parent_once(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == ".local" and injected == 0:
            # B가 .project_manager fd를 연 직후 A가 빈 골격을 이름 공간에서 제거한 형상.
            original_rmdir(cwd / ".project_manager")
            injected += 1
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pd.os, "mkdir", _remove_parent_once)

    transport = pd._save_opencode_transport_prompt(cwd, "재시도 뒤 저장")
    assert injected == 1
    assert transport.read_text(encoding="utf-8") == "재시도 뒤 저장"

    pd._cleanup_attempt_transport(transport)
    assert live_fds == set()
    assert list(cwd.iterdir()) == []


def test_opencode_chain_open_retry_exhaustion_is_delegate_error_without_residue(
        pd, monkeypatch, tmp_path):
    """매 chain-open의 상위 골격이 사라지면 유한 소진 뒤 번역하고 fd·디스크를 비운다."""
    if not pd._OPENCODE_TRANSPORT_FD_SUPPORTED:
        pytest.skip("dir_fd/O_NOFOLLOW 보장 플랫폼 전용")
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    live_fds = _track_transport_directory_fds(pd, monkeypatch)
    original_mkdir = pd.os.mkdir
    original_rmdir = pd.os.rmdir
    injected = 0

    def _remove_parent_every_time(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == ".local":
            original_rmdir(cwd / ".project_manager")
            injected += 1
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pd.os, "mkdir", _remove_parent_every_time)

    with pytest.raises(pd.DelegateError) as caught:
        pd._save_opencode_transport_prompt(cwd, "저장되면 안 됨")

    max_attempts = pd._OPENCODE_TRANSPORT_CHAIN_OPEN_MAX_ATTEMPTS
    assert injected == max_attempts
    assert "chain-open 경쟁(ENOENT)" in str(caught.value)
    assert f"{max_attempts}회 시도" in str(caught.value)
    assert live_fds == set()
    assert list(cwd.iterdir()) == []


def test_opencode_raw_reservation_failure_rolls_back_transport(
        pd, monkeypatch, tmp_path):
    """wire 준비 뒤 raw 예약 실패도 합성 프롬프트와 cwd 골격을 모두 되감는다."""
    cwd = tmp_path / "sandbox"
    cwd.mkdir()

    def _fail_raw_reservation(*args, **kwargs):
        raise OSError("의도한 raw 예약 실패")

    monkeypatch.setattr(pd, "_reserve_raw_output", _fail_raw_reservation)

    with pytest.raises(OSError, match="의도한 raw 예약 실패"):
        pd._execute_attempt(
            harness="opencode",
            model="prov/m",
            reasoning=None,
            role="developer",
            cwd=cwd,
            prompt="남으면 안 되는 합성 프롬프트",
            timeout=30,
            output_dir=tmp_path / "raw",
            run_fn=lambda *a, **k: pytest.fail("raw 예약 실패 뒤 실행되면 안 됨"),
            attempt="primary",
        )

    assert list(cwd.iterdir()) == []


def test_opencode_symlink_cwd_uses_same_lexical_dir_for_process_and_file(
        pd, tmp_path):
    """symlink cwd의 감사 argv는 lexical 값을 보존하고 spawn argv/cwd는 같은 고정 inode를 쓴다."""
    real_cwd = tmp_path / "real-workspace"
    linked_cwd = tmp_path / "workspace-link"
    real_cwd.mkdir()
    linked_cwd.symlink_to(real_cwd, target_is_directory=True)
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        dir_arg = argv[argv.index("--dir") + 1]
        file_arg = argv[argv.index("--file") + 1]
        seen.update(
            dir_arg=dir_arg,
            file_arg=file_arg,
            process_cwd=cwd,
            resolved_dir=Path(dir_arg).resolve(),
            resolved_file=Path(file_arg).resolve(),
        )
        assert cwd == dir_arg
        assert Path(file_arg).read_text(encoding="utf-8") == "합성 프롬프트"
        assert seen["resolved_file"].is_relative_to(seen["resolved_dir"])
        return {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        }

    result = pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=linked_cwd,
        prompt="합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=_capture,
        attempt="primary",
    )

    assert seen["resolved_dir"] == real_cwd.resolve()
    assert seen["resolved_file"].is_relative_to(real_cwd.resolve())
    assert result.argv[result.argv.index("--dir") + 1] == seen["dir_arg"]
    assert result.argv[result.argv.index("--file") + 1] == seen["file_arg"]
    assert not (real_cwd / ".project_manager").exists()


@pytest.mark.parametrize(
    ("swap_point", "binding_label"),
    [
        pytest.param("sandbox", "sandbox", id="sandbox"),
        pytest.param("prompt-parent", "prompt 부모", id="prompt-parent"),
        pytest.param("prompt-file", "prompt 파일", id="prompt-file"),
    ],
)
def test_opencode_rename_swap_after_prepare_is_rc1_and_cleanup_preserves_residue(
        pd, monkeypatch, tmp_path, capsys, swap_point, binding_label):
    """3개 결속 rename-swap은 spawn을 거부하고 cleanup도 replacement를 지우지 않는다."""
    real_cwd = tmp_path / "real-workspace"
    real_cwd.mkdir()
    prompt = _write_prompt(real_cwd)
    seen = {}
    original_reserve = pd._reserve_raw_output

    def _reserve_then_swap(*args, **kwargs):
        raw_path = original_reserve(*args, **kwargs)
        prepared_prompt = next(
            real_cwd.glob(".project_manager/.local/delegate/*/prompt.md")
        )
        prepared_parent = prepared_prompt.parent
        original_ignore = prepared_parent / ".gitignore"
        if swap_point == "sandbox":
            relative_prompt = prepared_prompt.relative_to(real_cwd)
            parked_cwd = tmp_path / "prepared-workspace"
            real_cwd.rename(parked_cwd)
            real_cwd.mkdir()
            attacker_prompt = real_cwd / relative_prompt
            attacker_prompt.parent.mkdir(parents=True)
            original_prompt = parked_cwd / relative_prompt
            original_ignore = original_prompt.parent / ".gitignore"
        elif swap_point == "prompt-parent":
            parked_parent = prepared_parent.with_name(
                f"{prepared_parent.name}-prepared"
            )
            prepared_parent.rename(parked_parent)
            prepared_parent.mkdir()
            attacker_prompt = None
            replacement_path = prepared_parent
            original_prompt = parked_parent / prepared_prompt.name
            original_ignore = parked_parent / ".gitignore"
        else:
            original_prompt = prepared_prompt.with_name("prepared-prompt.md")
            prepared_prompt.rename(original_prompt)
            attacker_prompt = prepared_prompt
        if attacker_prompt is not None:
            attacker_prompt.write_text("공격자 프롬프트", encoding="utf-8")
            replacement_path = attacker_prompt
        seen.update(
            attacker_prompt=attacker_prompt,
            replacement_path=replacement_path,
            original_prompt=original_prompt,
            original_ignore=original_ignore,
            raw_path=raw_path,
        )
        return raw_path

    monkeypatch.setattr(pd, "_reserve_raw_output", _reserve_then_swap)
    fake = _FakeRun(stdout=_opencode_stdout("호출되면 안 됨"))
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(real_cwd)],
        _enabled_conf(**{
            "delegate.developer.harness": "opencode",
            "delegate.developer.model": "prov/m",
        }),
        fake,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert f"spawn 직전 {binding_label}가 준비 뒤 교체됨" in err
    assert "cleanup identity 재대조 실패" in err
    assert "잔존 가능 경로" in err
    assert "Traceback" not in err
    assert fake.calls == []
    assert seen["replacement_path"].exists()
    if seen["attacker_prompt"] is not None:
        assert seen["attacker_prompt"].read_text(encoding="utf-8") == "공격자 프롬프트"
    assert seen["original_prompt"].is_file()
    assert seen["original_ignore"].read_text(encoding="utf-8") == "*\n"


def test_opencode_runner_boundary_swap_child_reads_prepared_prompt(
        pd, tmp_path):
    """최종 검사 뒤 rename+재배치돼도 실제 자식은 준비 때 고정한 prompt inode를 읽는다."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    seen = {}

    def _swap_then_spawn(argv, *, stdin_text, cwd, env, timeout, harness):
        prepared_prompt = next(
            sandbox.glob(".project_manager/.local/delegate/*/prompt.md")
        )
        relative_prompt = prepared_prompt.relative_to(sandbox)
        parked = tmp_path / "prepared-sandbox"
        attacker = tmp_path / "attacker-sandbox"
        sandbox.rename(parked)
        sandbox.mkdir()
        attacker_prompt = sandbox / relative_prompt
        attacker_prompt.parent.mkdir(parents=True)
        attacker_prompt.write_text("공격자 프롬프트", encoding="utf-8")
        try:
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; "
                    "print(Path(sys.argv[1]).read_text(encoding='utf-8'), end='')",
                    argv[argv.index("--file") + 1],
                ],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            seen.update(
                child_rc=child.returncode,
                child_stdout=child.stdout,
                child_stderr=child.stderr,
                child_cwd=cwd,
                child_file=argv[argv.index("--file") + 1],
                child_dir=argv[argv.index("--dir") + 1],
            )
        finally:
            sandbox.rename(attacker)
            parked.rename(sandbox)
        return {
            "returncode": child.returncode,
            "stdout": _opencode_stdout("완료") if child.returncode == 0 else "",
            "stderr": child.stderr,
            "timed_out": False,
        }

    pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=sandbox,
        prompt="준비된 합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=_swap_then_spawn,
        attempt="primary",
    )

    assert seen["child_rc"] == 0, seen["child_stderr"]
    assert seen["child_stdout"] == "준비된 합성 프롬프트"
    assert seen["child_cwd"] == seen["child_dir"]
    assert seen["child_file"].startswith(f"/proc/{os.getpid()}/fd/")
    assert seen["child_dir"].startswith(f"/proc/{os.getpid()}/fd/")


@pytest.mark.parametrize(
    "role", ["developer", "researcher", "architect", "code-reviewer"],
)
def test_opencode_proc_fd_absent_owner_guarded_normal_path_all_roles(
        pd, monkeypatch, tmp_path, role):
    """procfs 없는 POSIX도 신뢰 owner/권한 체인이면 네 역할 모두 lexical 위임을 실행한다."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    sandbox.chmod(0o700)
    binding_checks = 0
    seen = {}
    original_assert = pd._assert_opencode_transport_binding

    def _counting_assert(transport):
        nonlocal binding_checks
        binding_checks += 1
        return original_assert(transport)

    monkeypatch.setattr(pd, "_opencode_proc_fd_root", lambda: None)
    monkeypatch.setattr(pd, "_assert_opencode_transport_binding", _counting_assert)

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        file_arg = argv[argv.index("--file") + 1]
        seen.update(
            cwd=cwd,
            dir_arg=argv[argv.index("--dir") + 1],
            file_arg=file_arg,
            content=Path(file_arg).read_text(encoding="utf-8"),
        )
        return {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        }

    result = pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role=role,
        cwd=sandbox,
        prompt="정상 합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=_capture,
        attempt="primary",
    )

    raw_text = result.raw_path.read_text(encoding="utf-8")
    assert result.result["returncode"] == 0
    assert binding_checks == 1
    assert seen["cwd"] == str(sandbox)
    assert seen["dir_arg"] == str(sandbox)
    assert seen["file_arg"].startswith(str(sandbox) + os.sep)
    assert seen["content"].endswith("정상 합성 프롬프트")
    assert "# transport_binding_mode: owner-guarded-lexical" in raw_text


def test_opencode_proc_fd_absent_swap_is_rejected_before_runner(
        pd, monkeypatch, tmp_path):
    """procfs fd 경로가 없어도 전송 직전 inode 재대조가 교체 prompt를 fail-loud 한다."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    sandbox.chmod(0o700)
    transport = pd._save_opencode_transport_prompt(sandbox, "준비된 합성 프롬프트")
    argv = pd._build_target_argv(
        "opencode", "prov/m", None, "developer", transport.sandbox, transport,
    )
    prepared = transport.path.with_name("prepared-prompt.md")
    transport.path.rename(prepared)
    transport.path.write_text("공격자 프롬프트", encoding="utf-8")
    monkeypatch.setattr(pd, "_opencode_proc_fd_root", lambda: None)
    try:
        with pytest.raises(pd.DelegateError, match="prompt 파일.*준비 뒤 교체됨"):
            pd._opencode_transport_launch_target(transport, argv)
    finally:
        transport.path.unlink()
        prepared.rename(transport.path)
        pd._cleanup_attempt_transport(transport)


def test_opencode_proc_fd_absent_same_owner_after_final_check_residual_is_explicit(
        pd, monkeypatch, tmp_path):
    """허용된 같은-euid 잔여는 값과 raw binding mode로 숨김없이 표출한다."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    sandbox.chmod(0o700)
    seen = {"runner_calls": 0}
    original_assert = pd._assert_opencode_transport_binding

    def _assert_then_swap(transport):
        original_assert(transport)
        prepared = transport.path.with_name("prepared-after-final-check.md")
        transport.path.rename(prepared)
        transport.path.write_text("ATTACKER_AFTER_FINAL_CHECK", encoding="utf-8")
        seen.update(
            prepared=prepared,
            lexical=transport.path,
            prepared_content=prepared.read_text(encoding="utf-8"),
            attacker_content=transport.path.read_text(encoding="utf-8"),
        )

    def _runner(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["runner_calls"] += 1
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "print(Path(sys.argv[1]).read_text(encoding='utf-8'), end='')",
                argv[argv.index("--file") + 1],
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        seen.update(child_rc=child.returncode, child_stdout=child.stdout)
        return {
            "returncode": child.returncode,
            "stdout": _opencode_stdout("완료"),
            "stderr": child.stderr,
            "timed_out": False,
        }

    monkeypatch.setattr(pd, "_opencode_proc_fd_root", lambda: None)
    monkeypatch.setattr(pd, "_assert_opencode_transport_binding", _assert_then_swap)

    result = pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=sandbox,
        prompt="ORIGINAL_AFTER_FINAL_CHECK",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=_runner,
        attempt="primary",
    )

    assert seen["prepared_content"] == "ORIGINAL_AFTER_FINAL_CHECK"
    assert seen["attacker_content"] == "ATTACKER_AFTER_FINAL_CHECK"
    assert seen["runner_calls"] == 1
    assert seen["child_rc"] == 0
    assert seen["child_stdout"] == "ATTACKER_AFTER_FINAL_CHECK"
    assert (
        "# transport_binding_mode: owner-guarded-lexical"
        in result.raw_path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "role", ["developer", "researcher", "architect", "code-reviewer"],
)
def test_opencode_proc_fd_absent_unsafe_parent_rejection_closes_raw_ledger(
        pd, monkeypatch, tmp_path, capsys, role):
    """다른 UID 쓰기 가능 부모의 네 역할 거부는 raw 사유+rc=1 마감이며 unfinished 0이다."""
    shared = tmp_path / "shared"
    sandbox = shared / "sandbox"
    shared.mkdir()
    shared.chmod(0o777)
    sandbox.mkdir()
    output_dir = tmp_path / "raw"
    monkeypatch.setattr(pd, "_opencode_proc_fd_root", lambda: None)

    with pytest.raises(pd.DelegateError, match="다른 UID가 교체할 수 있어"):
        pd._execute_attempt(
            harness="opencode",
            model="prov/m",
            reasoning=None,
            role=role,
            cwd=sandbox,
            prompt="전송되면 안 됨",
            timeout=30,
            output_dir=output_dir,
            run_fn=lambda *args, **kwargs: pytest.fail("runner must not start"),
            attempt="primary",
        )

    rows = pd._load_relay().raw_records(output_dir / "raw_outputs.json")
    assert len(rows) == 1
    row = rows[0]
    raw_path = Path(row["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    assert raw_path.stat().st_size > 0
    assert row["finished_at"] is not None
    assert row["rc"] == 1
    assert row["pre_spawn_rejected"] is True
    assert row["finish_note"] == "pre-spawn rejection"
    assert "pre-spawn rejection" in raw_text
    assert "# transport_binding_mode: fail-closed" in raw_text

    capsys.readouterr()
    assert pd._cmd_raw([
        "--unfinished", "--output-dir", str(output_dir),
    ]) == 0
    assert "미마감 raw 없음" in capsys.readouterr().out


def _init_transport_test_repo(repo: Path, tracked_path: str = "seed.txt") -> None:
    repo.mkdir()
    target = repo / tracked_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", tracked_path], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=repo, check=True, capture_output=True,
    )


def test_opencode_non_root_cwd_transport_is_hidden_while_running(pd, tmp_path):
    """repo 하위 cwd의 wire 사본은 실행 중에도 git untracked 표면에 나타나지 않는다."""
    repo = tmp_path / "repo"
    _init_transport_test_repo(repo, "pkg/seed.txt")
    cwd = repo / "pkg"

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout
        assert status == ""
        assert Path(argv[argv.index("--file") + 1]).is_file()
        return {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        }

    pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=cwd,
        prompt="합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=_capture,
        attempt="primary",
    )

    assert not (cwd / ".project_manager").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout == ""


def test_opencode_pm_uninstalled_repo_leaves_no_marker_skeleton(pd, tmp_path):
    """PM 미설치 repo에서 성공 정리 뒤 .project_manager 마커 골격을 남기지 않는다."""
    repo = tmp_path / "plain-repo"
    _init_transport_test_repo(repo)
    assert not (repo / ".project_manager").exists()

    pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=repo,
        prompt="합성 프롬프트",
        timeout=30,
        output_dir=tmp_path / "raw",
        run_fn=lambda *a, **k: {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        },
        attempt="primary",
    )

    assert not (repo / ".project_manager").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout == ""


def test_opencode_external_symlink_prepare_is_main_rc1_without_traceback(
        pd, monkeypatch, tmp_path, capsys):
    """외부 symlink containment 거부는 DelegateError traceback 대신 main rc=1 진단으로 끝난다."""
    cwd = tmp_path / "sandbox"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    (cwd / ".project_manager").symlink_to(outside, target_is_directory=True)
    prompt = _write_prompt(cwd)
    conf = _enabled_conf(**{
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "prov/m",
    })
    fake = _FakeRun(stdout=_opencode_stdout("호출되면 안 됨"))

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd)],
        conf, fake,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "sandbox 밖" in err
    assert "Traceback" not in err
    assert fake.calls == []


def test_cwd_symlink_loop_is_main_rc1_without_traceback(
        pd, monkeypatch, tmp_path, capsys):
    """Python 3.11/3.12 resolve RuntimeError도 DelegateError 진단 rc로 마감한다."""
    prompt = _write_prompt(tmp_path)
    cwd_loop = tmp_path / "cwd-loop"
    cwd_loop.symlink_to(cwd_loop, target_is_directory=True)
    fake = _FakeRun(stdout=_opencode_stdout("호출되면 안 됨"))

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd_loop)],
        _enabled_conf(**{
            "delegate.developer.harness": "opencode",
            "delegate.developer.model": "prov/m",
        }),
        fake,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "--cwd 실경로 해소 실패" in err
    assert "Traceback" not in err
    assert fake.calls == []


def test_opencode_external_symlink_fallback_prepare_is_main_rc1_without_traceback(
        pd, monkeypatch, tmp_path, capsys):
    """폴백 준비의 DelegateError도 primary raw 진단을 보존한 main rc=1로 번역한다."""
    cwd = tmp_path / "sandbox"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    (cwd / ".project_manager").symlink_to(outside, target_is_directory=True)
    prompt = _write_prompt(cwd)
    conf = _enabled_conf(**{
        "delegate.developer.fallback.harness": "opencode",
        "delegate.developer.fallback.model": "prov/m",
    })
    calls = []

    def _primary_launch_failure(argv, *, stdin_text, cwd, env, timeout, harness):
        calls.append(harness)
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "codex executable missing",
            "timed_out": False,
            pd.RUN_RESULT_LAUNCH_FAILED: True,
        }

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd)],
        conf, _primary_launch_failure,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "폴백 실행 준비/raw 박제 실패" in err
    assert "sandbox 밖" in err
    assert "primary raw:" in err
    assert "Traceback" not in err
    assert calls == ["codex"]


def test_opencode_parent_swap_after_containment_cannot_create_outside(
        pd, monkeypatch, tmp_path, capsys):
    """containment 검사 직후 부모를 외부 symlink로 바꿔도 fd 순회가 거부하고 외부 생성은 0이다."""
    if not pd._OPENCODE_TRANSPORT_FD_SUPPORTED:
        pytest.skip("dir_fd/O_NOFOLLOW 보장 플랫폼 전용")
    cwd = tmp_path / "sandbox"
    local_dir = cwd / ".project_manager" / ".local"
    outside = tmp_path / "outside"
    local_dir.mkdir(parents=True)
    outside.mkdir()
    prompt = _write_prompt(cwd)
    parked = cwd / ".project_manager" / ".local-before-swap"
    original_assert = pd._assert_opencode_transport_path
    calls = 0

    def _assert_then_swap(cwd_arg, prompt_file):
        nonlocal calls
        original_assert(cwd_arg, prompt_file)
        calls += 1
        if calls == 1:
            local_dir.rename(parked)
            local_dir.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(pd, "_assert_opencode_transport_path", _assert_then_swap)
    conf = _enabled_conf(**{
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "prov/m",
    })
    fake = _FakeRun(stdout=_opencode_stdout("호출되면 안 됨"))

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd)],
        conf, fake,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "안전한 디렉터리가 아님" in err
    assert "Traceback" not in err
    assert fake.calls == []
    assert list(outside.iterdir()) == []


def test_opencode_cleanup_failure_warns_and_preserves_main_result(
        pd, monkeypatch, tmp_path, capsys):
    """민감 사본 삭제 실패는 경로·오류를 경고하되 성공 reply/rc를 갈아치우지 않는다."""
    if not pd._OPENCODE_TRANSPORT_FD_SUPPORTED:
        pytest.skip("dir_fd unlink 경로 전용")
    repo = tmp_path / "repo"
    _init_transport_test_repo(repo, "prompt.md")
    prompt = repo / "prompt.md"
    conf = _enabled_conf(**{
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "prov/m",
    })
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["prompt_path"] = Path(argv[argv.index("--file") + 1])
        seen["prepared_path"] = seen["prompt_path"].resolve()
        return {
            "returncode": 0,
            "stdout": _opencode_stdout("주 결과 보존"),
            "stderr": "",
            "timed_out": False,
        }

    original_unlink = pd.os.unlink

    def _deny_transport_unlink(path, *args, **kwargs):
        if Path(path).name == "prompt.md":
            raise PermissionError("의도한 삭제 거부")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(pd.os, "unlink", _deny_transport_unlink)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(repo)],
        conf, _capture,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "주 결과 보존" in captured.out
    assert "합성 프롬프트 삭제 실패" in captured.err
    assert str(seen["prepared_path"]) in captured.err
    assert "의도한 삭제 거부" in captured.err
    assert seen["prepared_path"].is_file()
    assert (seen["prepared_path"].parent / ".gitignore").is_file()
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout == ""


def test_opencode_transport_guard_rejects_external_and_symlink_paths(pd, tmp_path):
    """전송 전 가드는 lexical 외부 경로와 sandbox 안 symlink의 실제 외부 대상을 함께 거부한다."""
    relay = pd._load_relay()
    cwd = tmp_path / "sandbox"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    prompt = outside / "prompt.md"
    prompt.write_text("지시", encoding="utf-8")

    with pytest.raises(relay.HarnessContractError, match="sandbox 밖"):
        relay.assert_opencode_prompt_in_cwd(cwd, prompt)

    link = cwd / "linked-outside"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(relay.HarnessContractError, match="sandbox 밖"):
        relay.assert_opencode_prompt_in_cwd(cwd, link / "prompt.md")


def test_opencode_pm_home_raw_and_cwd_transport_are_separate_and_cross_recorded(
        pd, monkeypatch, tmp_path):
    """PM 홈≠cwd 회귀: 감사 raw는 PM 홈, wire는 sandbox에 두고 장부 한 행에 두 좌표를 남긴다."""
    pm_home = tmp_path / "pm-home"
    cwd = tmp_path / "work" / "slot-1"
    (pm_home / ".project_manager").mkdir(parents=True)
    cwd.mkdir(parents=True)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", pm_home)
    seen = {}

    def _capture(argv, *, stdin_text, cwd: str, env, timeout, harness):
        prompt_path = Path(argv[argv.index("--file") + 1])
        prepared_path = prompt_path.resolve()
        prepared_cwd = Path(cwd).resolve()
        ledger_path = pm_home / ".project_manager" / ".local" / "raw_outputs.json"
        row = _json.loads(ledger_path.read_text(encoding="utf-8"))["records"][0]
        seen.update(
            prompt_path=prompt_path,
            prepared_path=prepared_path,
            prepared_cwd=prepared_cwd,
            row_during_run=row,
        )
        assert stdin_text is None
        assert prepared_path.is_relative_to(prepared_cwd)
        assert prepared_path.parent.parent == (
            prepared_cwd / ".project_manager" / ".local" / "delegate"
        )
        assert prompt_path.read_text(encoding="utf-8") == "role preamble + task"
        assert row[pd.OPENCODE_TRANSPORT_PROMPT_FIELD] == str(prepared_path)
        assert Path(row["raw_path"]).parent == (
            pm_home / ".project_manager" / ".local" / "delegate"
        )
        return {
            "returncode": 0,
            "stdout": _opencode_stdout("완료"),
            "stderr": "",
            "timed_out": False,
        }

    result = pd._execute_attempt(
        harness="opencode",
        model="prov/m",
        reasoning=None,
        role="developer",
        cwd=cwd,
        prompt="role preamble + task",
        timeout=30,
        output_dir=None,
        run_fn=_capture,
        attempt="primary",
    )

    ledger_path = pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    row = _json.loads(ledger_path.read_text(encoding="utf-8"))["records"][0]
    assert result.raw_path.resolve() == Path(row["raw_path"])
    assert row[pd.OPENCODE_TRANSPORT_PROMPT_FIELD] == str(seen["prepared_path"])
    raw_text = result.raw_path.read_text(encoding="utf-8")
    assert str(seen["prompt_path"]) in raw_text
    assert (
        f"# transport_sandbox_path_lexical: {cwd}" in raw_text
    )
    assert (
        f"# transport_prompt_path_lexical: {seen['prepared_path']}" in raw_text
    )
    assert "# transport_binding_mode: procfd" in raw_text
    assert row[pd.OPENCODE_TRANSPORT_PROMPT_FIELD] == str(seen["prepared_path"])
    assert result.raw_path.is_file()
    assert not seen["prompt_path"].exists()


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


def test_codex_read_attempt_reanchors_execution_root_env_preamble_and_cleanup(
        pd, monkeypatch, tmp_path):
    """Codex read attempt는 profile의 암묵 write root까지 tmp로 옮기고 잔여를 0으로 만든다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        read_tmp = Path(env["TMPDIR"])
        seen.update(
            argv=argv, prompt=stdin_text, env=env, read_tmp=read_tmp,
            process_cwd=Path(cwd),
        )
        assert read_tmp.is_dir()
        assert stat.S_IMODE(read_tmp.stat().st_mode) == 0o700
        (read_tmp / "pytest-artifact").mkdir()
        (read_tmp / "pytest-artifact" / "result.txt").write_text("green")
        return {
            "returncode": 0, "stdout": _codex_stdout("검토 완료"),
            "stderr": "", "timed_out": False,
        }

    pd._execute_attempt(
        harness="codex", model="gpt-x", reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="role preamble + task",
        timeout=30, output_dir=tmp_path / "raw", run_fn=_capture,
        attempt="primary",
    )

    argv = seen["argv"]
    assert "-s" not in argv and "workspace-write" not in argv
    overrides = [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "-c"]
    assert f'default_permissions="{pd._CODEX_READ_TMP_PROFILE}"' in overrides
    profile = next(value for value in overrides if value.startswith("permissions."))
    assert '":root"="read"' in profile
    assert f'"{seen["read_tmp"]}"="write"' in profile
    assert str(cwd) not in profile  # worktree write grant 0
    assert argv[argv.index("-C") + 1] == str(seen["read_tmp"])
    assert seen["process_cwd"] == seen["read_tmp"]
    assert all(seen["env"][key] == str(seen["read_tmp"])
               for key in pd._READ_TMP_ENV_KEYS)
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert str(seen["read_tmp"]) in seen["prompt"]
    assert "-p no:cacheprovider" in seen["prompt"]
    assert f"cd {cwd}" in seen["prompt"]
    assert "worktree" in seen["prompt"]
    assert not seen["read_tmp"].exists()
    assert pd.WRITE_ROLES == frozenset({"developer", "architect"})


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("PM_DELEGATE_LIVE_SANDBOX") != "1" or not shutil.which("codex"),
    reason=(
        "Codex 실제 sandbox 권한 probe — PM_DELEGATE_LIVE_SANDBOX=1 + codex binary가 "
        "필요(모델/API 호출 없음)"
    ),
)
def test_codex_live_sandbox_tmp_write_and_worktree_open_denied(
        pd, monkeypatch, tmp_path):
    """실 Codex sandbox에서 0-byte write까지 tmp=성공/worktree=open 거부를 단언한다."""
    system_temp = tmp_path / "system-temp"
    worktree = tmp_path / "worktree"
    system_temp.mkdir()
    worktree.mkdir()
    target = worktree / "tracked.py"
    target.write_text("sentinel = 1\n", encoding="utf-8")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(system_temp))
    read_tmp = pd._create_read_role_temp("codex", worktree)
    assert read_tmp is not None
    temp_probe = read_tmp.path / "probe"
    before = (
        target.stat().st_size,
        target.stat().st_mtime_ns,
        hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    probe_script = "\n".join([
        "import errno, json, os",
        "results = {}",
        f"paths = [",
        f"    ('worktree', {_json.dumps(str(target))}, os.O_WRONLY),",
        f"    ('tmp', {_json.dumps(str(temp_probe))}, os.O_WRONLY | os.O_CREAT),",
        "]",
        "for label, path, flags in paths:",
        "    try:",
        "        fd = os.open(path, flags, 0o600)",
        "        written = os.write(fd, b'')",
        "        os.close(fd)",
        "        results[label] = {'open': True, 'write': written}",
        "    except OSError as exc:",
        "        results[label] = {'open': False, 'errno': exc.errno, "
        "name': errno.errorcode.get(exc.errno)}",
        "print(json.dumps(results, sort_keys=True))",
    ])
    profile_override = (
        f"permissions.{pd._CODEX_READ_TMP_PROFILE}="
        f"{pd._codex_read_tmp_profile_value(read_tmp.path)}"
    )
    env = pd._apply_read_tmp_env(dict(os.environ), "codex", read_tmp)
    try:
        completed = subprocess.run(
            [
                "codex", "sandbox", "-c", profile_override,
                "-P", pd._CODEX_READ_TMP_PROFILE,
                "-C", str(read_tmp.path), "--", sys.executable, "-c", probe_script,
            ],
            cwd=read_tmp.path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        observed = _json.loads(completed.stdout.strip().splitlines()[-1])
        assert observed["tmp"] == {"open": True, "write": 0}
        assert observed["worktree"]["open"] is False
        assert observed["worktree"]["errno"] in {
            getattr(os, "EROFS", 30), getattr(os, "EACCES", 13), getattr(os, "EPERM", 1),
        }
        after = (
            target.stat().st_size,
            target.stat().st_mtime_ns,
            hashlib.sha256(target.read_bytes()).hexdigest(),
        )
        assert after == before
    finally:
        pd._cleanup_read_role_temp(read_tmp)
    assert not read_tmp.path.exists()


def test_claude_read_attempt_add_dir_and_cleanup(pd, monkeypatch, tmp_path):
    """Claude 2.1.227 등가 수단은 같은 attempt tmp를 --add-dir와 env에 연결한다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        read_tmp = Path(env["TMPDIR"])
        seen.update(argv=argv, prompt=stdin_text, read_tmp=read_tmp)
        (read_tmp / "pytest.tmp").write_text("ok")
        return {
            "returncode": 0, "stdout": _claude_stdout("검토 완료"),
            "stderr": "", "timed_out": False,
        }

    pd._execute_attempt(
        harness="claude", model="opus", reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="role preamble + task",
        timeout=30, output_dir=tmp_path / "raw", run_fn=_capture,
        attempt="primary",
    )

    assert seen["argv"][seen["argv"].index("--add-dir") + 1] == str(seen["read_tmp"])
    assert str(seen["read_tmp"]) in seen["prompt"]
    assert not seen["read_tmp"].exists()


def test_opencode_read_attempt_uses_measured_allowed_tmp_and_shared_cleanup(
        pd, monkeypatch, tmp_path):
    """OpenCode plan의 실측 `${TMPDIR}/opencode/*`와 wire 사본을 한 cleanup seam이 회수한다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        read_tmp = Path(env["TMPDIR"])
        writable_tmp = Path(env["TMP"])
        prompt_path = Path(argv[argv.index("--file") + 1])
        seen.update(
            read_tmp=read_tmp, writable_tmp=writable_tmp,
            prompt_path=prompt_path,
        )
        assert read_tmp.parent == temp_root / pd._OPENCODE_READ_TMP_PARENT
        assert writable_tmp == read_tmp / pd._OPENCODE_ALLOWED_TMP_COMPONENT
        assert env["TEMP"] == str(writable_tmp)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        assert str(writable_tmp) in prompt_text
        assert '"$TMPDIR/opencode/pytest"' in prompt_text
        (writable_tmp / "pytest-artifact").write_text("ok")
        return {
            "returncode": 0, "stdout": _opencode_stdout("검토 완료"),
            "stderr": "", "timed_out": False,
        }

    pd._execute_attempt(
        harness="opencode", model="prov/m", reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="role preamble + task",
        timeout=30, output_dir=tmp_path / "raw", run_fn=_capture,
        attempt="primary",
    )

    assert not seen["read_tmp"].exists()
    assert not seen["prompt_path"].exists()
    assert not (temp_root / pd._OPENCODE_READ_TMP_PARENT).exists()
    assert not (cwd / ".project_manager").exists()


@pytest.mark.integration
@pytest.mark.skipif(
    not shutil.which("opencode"),
    reason="opencode 실제 `${TMPDIR}/opencode/*` 권한 선언 계약 — binary 필요",
)
def test_opencode_binary_permission_tracks_child_tmpdir(tmp_path):
    """OpenCode 1.18.12 실제 agent 선언은 고정 /tmp가 아니라 child TMPDIR 값을 보간한다."""
    attempt = tmp_path / "attempt"
    allowed = attempt / "opencode"
    sandbox = tmp_path / "sandbox"
    attempt.mkdir()
    allowed.mkdir()
    sandbox.mkdir()
    env = dict(os.environ)
    env.update({"TMPDIR": str(attempt), "TMP": str(allowed), "TEMP": str(allowed)})
    for key, name in (
        ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_STATE_HOME", "xdg-state"),
    ):
        path = tmp_path / name
        path.mkdir()
        env[key] = str(path)

    completed = subprocess.run(
        ["opencode", "agent", "list"],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert f'"pattern": "{attempt}/opencode/*"' in completed.stdout
    assert '"pattern": "/tmp/opencode/*"' not in completed.stdout


def test_read_tmp_unsupported_platform_preamble_is_explicit(
        pd, monkeypatch, tmp_path):
    """안전한 fd cleanup 불가 플랫폼은 회귀 숫자 인용·미실행 명시 문구를 wire에 강제한다."""
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    monkeypatch.setattr(pd, "_READ_TMP_FD_SUPPORTED", False)
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["prompt"] = stdin_text
        return {
            "returncode": 0, "stdout": _codex_stdout("검토 완료"),
            "stderr": "", "timed_out": False,
        }

    pd._execute_attempt(
        harness="codex", model="gpt-x", reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="role preamble + task",
        timeout=30, output_dir=tmp_path / "raw", run_fn=_capture,
        attempt="primary",
    )

    assert pd.READ_REGRESSION_UNAVAILABLE_NOTE in seen["prompt"]
    assert "회귀 숫자는 developer 보고값을 인용" in seen["prompt"]
    assert "직접 실행하지 못했다는 사실" in seen["prompt"]


def test_read_tmp_identity_swap_refuses_wrong_tree_cleanup(
        pd, monkeypatch, tmp_path, capsys):
    """생성 뒤 root 교체는 다른 트리를 지우지 않고 loud residue 진단을 남긴다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    read_tmp = pd._create_read_role_temp("codex", cwd)
    assert read_tmp is not None
    # inode 재사용에 기대지 않고 정리 시점 identity 불일치를 결정적으로 주입한다.
    read_tmp.identity = (-1, -1)

    pd._cleanup_attempt_transport(None, read_tmp)

    assert read_tmp.path.is_dir()
    assert "정리 실패" in capsys.readouterr().err
    read_tmp.path.rmdir()


def test_opencode_created_parent_inode_swap_preserves_replacement(
        pd, monkeypatch, tmp_path, capsys):
    """공유 temp 부모 이름이 교체되면 생성 inode와 다른 빈 디렉터리를 rmdir하지 않는다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    read_tmp = pd._create_read_role_temp("opencode", cwd)
    assert read_tmp is not None and read_tmp.created_parent is not None
    owned_parent = temp_root / pd._OPENCODE_READ_TMP_PARENT
    moved_parent = temp_root / "owned-parent-moved"
    owned_parent.rename(moved_parent)
    owned_parent.mkdir()

    pd._cleanup_read_role_temp(read_tmp)

    assert owned_parent.is_dir()  # 교체자가 만든 빈 디렉터리를 보존한다.
    assert list(owned_parent.iterdir()) == []
    assert list(moved_parent.iterdir()) == []  # fd로 고정한 원래 attempt만 회수했다.
    assert "temp 부모 생성 identity" in capsys.readouterr().err
    owned_parent.rmdir()
    moved_parent.rmdir()


def test_read_tmp_cleanup_on_runner_exception(pd, monkeypatch, tmp_path):
    """runner가 예상 밖 예외를 올려도 finally가 read tmp 하위 산출물까지 잔여 0으로 만든다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))

    def _explode(argv, *, stdin_text, cwd, env, timeout, harness):
        read_tmp = Path(env["TMPDIR"])
        (read_tmp / "runner-artifact").write_text("partial", encoding="utf-8")
        raise RuntimeError("의도한 runner 예외")

    with pytest.raises(RuntimeError, match="의도한 runner 예외"):
        pd._execute_attempt(
            harness="claude", model="opus", reasoning=None,
            role="code-reviewer", cwd=cwd, prompt="review",
            timeout=30, output_dir=tmp_path / "raw", run_fn=_explode,
            attempt="primary",
        )

    assert list(temp_root.iterdir()) == []


def test_read_tmp_cleanup_on_timeout_child_kill_result(pd, monkeypatch, tmp_path):
    """watchdog timeout/child-kill 결과가 돌아온 뒤에도 read tmp 잔여는 0이다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))

    def _timed_out(argv, *, stdin_text, cwd, env, timeout, harness):
        read_tmp = Path(env["TMPDIR"])
        (read_tmp / "child-partial").write_text("killed", encoding="utf-8")
        return {
            "returncode": 124, "stdout": "", "stderr": "watchdog killed child",
            "timed_out": True,
        }

    result = pd._execute_attempt(
        harness="claude", model="opus", reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="review",
        timeout=1, output_dir=tmp_path / "raw", run_fn=_timed_out,
        attempt="primary",
    )

    assert result.result["timed_out"] is True
    assert list(temp_root.iterdir()) == []


def test_read_tmp_cleanup_on_raw_reservation_failure(
        pd, monkeypatch, tmp_path):
    """transport 준비 뒤 raw 예약 실패도 runner 전에 read tmp를 잔여 0으로 되감는다."""
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    temp_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(
        pd, "_reserve_raw_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("raw reserve failed")),
    )

    with pytest.raises(OSError, match="raw reserve failed"):
        pd._execute_attempt(
            harness="claude", model="opus", reasoning=None,
            role="code-reviewer", cwd=cwd, prompt="review",
            timeout=30, output_dir=tmp_path / "raw",
            run_fn=lambda *args, **kwargs: pytest.fail("runner must not start"),
            attempt="primary",
        )

    assert list(temp_root.iterdir()) == []


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


def _run_pm_home_prompt(pd, monkeypatch, tmp_path: Path, text: str):
    """PM 홈 재앵커 게이트를 mock 하네스까지 통과시켜 rc와 호출 기록을 반환한다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(text, encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt),
         "--cwd", str(home), "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    return rc, fake, wt


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
    # 경로/이름은 발췌로 그대로 노출(T-0472 — 원인 특정에 추측이 필요했던 관측 결함 해소).
    # 크리덴셜 *값* 은 여전히 마스킹된다(test_secret_scan_value_excerpt_is_masked).
    assert "credentials.env" in err
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
    assert pd._prompt_file_contained(prompt, cwd, pm_home=repo) is False


def test_containment_repo_pm_home_allowed(pd, tmp_path, monkeypatch):
    """이 repo PM 홈(REPO/.project_manager) 하위는 cwd 밖이어도 허용(b 루트)."""
    repo = tmp_path / "repo"
    (repo / ".project_manager").mkdir(parents=True)
    monkeypatch.setattr(pd, "REPO", repo)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    prompt = repo / ".project_manager" / "task.md"
    prompt.write_text("x", encoding="utf-8")
    assert pd._prompt_file_contained(prompt, cwd, pm_home=repo) is True


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
    assert pd._prompt_file_contained(link, cwd, pm_home=repo) is False


def test_cwd_root_usage_error(pd, monkeypatch, tmp_path):
    """`--cwd /`(파일시스템 루트)는 usage error — 전 파일시스템을 containment 로 여는 우회 차단."""
    prompt = _write_prompt(tmp_path)
    monkeypatch.setattr(pd, "local_config", lambda: _enabled_conf())
    with pytest.raises(SystemExit) as ei:
        pd.main(["--role", "developer", "--prompt-file", str(prompt), "--cwd", "/"])
    assert ei.value.code == 2


# ══ must-fix 2: launch 오류 정규화 (_default_run_fn·traceback 금지) ═══════════

_REAL_RELAY = None


def _relay_module():
    """실 pm_relay 모듈(1회 로드 캐시) — 대역이 선언 테이블/해소기를 **실제 엔진 것**으로 쓴다."""
    global _REAL_RELAY
    if _REAL_RELAY is None:
        _REAL_RELAY = _load("pm_relay", TOOLS / "pm_relay.py")
    return _REAL_RELAY


class _RelayStub:
    """실 pm_relay 위임 대역 — 필요한 지점만 덮어쓴다.

    프로필 테이블·시간 예산 해소는 실제 엔진 것을 쓴다: 대역이 값 규칙을 자체 구현하면 "하네스별
    값이 실제로 갈리는가"를 검증하는 테스트가 거짓이 된다."""

    def __getattr__(self, name):
        return getattr(_relay_module(), name)


class _FakeRelayWatchdog(_RelayStub):
    """run_with_first_event_watchdog 이 주입 예외를 raise 하는 대역(3드라이버 공통 경로).

    codex/claude 도 워치독 경유가 됐으므로 이 대역이 3드라이버 전부를 커버한다. 호출 인자(kwargs)를
    기록해 드라이버 선언(첫-이벤트 창·재시도·무진행 상한)이 실제로 전달되는지 단언할 수 있게 한다."""

    StallWatchdogError = type("StallWatchdogError", (RuntimeError,), {})

    def __init__(self, exc=None, completed=None):
        self._exc = exc
        self._completed = completed
        self.calls: list[dict] = []

    def first_event_timeout_default(self):
        return 1.0

    def stall_retries_default(self):
        return 0

    def run_with_first_event_watchdog(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        if self._exc is not None:
            raise self._exc
        return self._completed


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_default_run_fn_launch_error_all_drivers(pd, monkeypatch, harness):
    """3드라이버 전부 바이너리 미설치(워치독 스폰 FileNotFoundError) → rc≠0·진단(traceback 아님)."""
    monkeypatch.setattr(pd, "_load_relay",
                        lambda: _FakeRelayWatchdog(FileNotFoundError(2, "nope", "bin")))
    res = pd._default_run_fn(["bin"], stdin_text="x", cwd="/tmp", env={}, timeout=1, harness=harness)
    assert res["returncode"] != 0 and res["timed_out"] is False
    assert "실행 불가" in res["stderr"]


# ══ must-fix 3: timeout 정규화 (3드라이버 공통 워치독 경로·§5.3 · T-0489 ③) ══════

class _SpyRelayKill:
    def __init__(self):
        self.kill_calls = []

    def _kill_process_group(self, proc):
        self.kill_calls.append(proc)


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_default_run_fn_timeout_all_drivers(pd, monkeypatch, harness):
    """3드라이버 timeout(워치독이 그룹째 kill 후 TimeoutExpired 전파) → timed_out=True·§5.3.

    프로세스그룹 kill 자체는 이제 워치독 소관이다(pm_relay 실-Popen 테스트가 잔존 0을 단언) —
    여기서는 위임층의 정규화(분류 가능한 timed_out 신호)만 본다."""
    monkeypatch.setattr(pd, "_load_relay",
                        lambda: _FakeRelayWatchdog(subprocess.TimeoutExpired(cmd="x", timeout=1)))
    res = pd._default_run_fn(["bin"], stdin_text="x", cwd="/tmp", env={}, timeout=1, harness=harness)
    assert res["timed_out"] is True and res["returncode"] != 0
    assert pd.classify_infrastructure_failure(res) == pd.FAILURE_CLASS_TIMEOUT


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
    """conf delegate_timeout 비수치/≤0 은 traceback 대신 **하네스 프로필 선언값**으로 fail-soft."""
    ns = type("NS", (), {"timeout": None})()
    declared = int(_relay_module().HARNESS_PROFILES["codex"].wall_timeout)
    assert pd._resolve_timeout(ns, {"delegate_timeout": "abc"}, "codex") == declared
    assert pd._resolve_timeout(ns, {"delegate_timeout": "-5"}, "codex") == declared
    assert pd._resolve_timeout(ns, {"delegate_timeout": "600"}, "codex") == 600
    # 하네스별 키가 표면-flat legacy 키를 이긴다(더 구체적인 선언).
    assert pd._resolve_timeout(
        ns, {"delegate_timeout": "600", "harness.codex.wall_timeout": "900"}, "codex") == 900
    ns2 = type("NS", (), {"timeout": 42})()
    assert pd._resolve_timeout(ns2, {}, "codex") == 42   # CLI 가 가장 강함


# ══ R2 must-fix 1: opencode positional message 필수 ══════════════════════════

def test_opencode_argv_has_message_positional(pd):
    """opencode run 은 --file 이 있어도 비어있지 않은 positional message 를 요구(부재 시 rc=1·실측)."""
    argv = pd.build_opencode_argv("m", None, "developer", "/w", "/p")
    # 고정 message 선언은 공용 드라이버 계약(pm_relay)이 소유한다 — 위임 wrapper 는 그 값을 쓴다.
    assert argv[:3] == ["opencode", "run", pd._load_relay().OPENCODE_ATTACHED_MSG]
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


@pytest.mark.parametrize(
    "call",
    [
        "show T-0466",
        "show T-pay-001",
        "list --all",
        "list --task X",
        "list --status open --tag phase-1 --repo app --slot 2",
        "lint",
        "lint --gate",
    ],
)
def test_targets_engine_code_ignores_read_only_board_cli(pd, call):
    """A: show/list/lint의 read-only 인자 변형은 엔진 코드 수정 타깃이 아니다."""
    prompt = (
        "wiki/roadmap.md 를 갱신하라\n"
        f"python3 .project_manager/tools/board.py {call}"
    )
    assert pd._prompt_targets_engine_code(prompt) is False


@pytest.mark.parametrize(
    "template, skill_rel",
    [
        ("codex", ".agents/skills/pm-dev-delegate/SKILL.md"),
        ("claude_code", ".claude/skills/pm-dev-delegate/SKILL.md"),
        ("opencode", ".claude/skills/pm-dev-delegate/SKILL.md"),
    ],
)
def test_shipped_skill_show_idiom_with_natural_language_tail_rc0(
        pd, monkeypatch, tmp_path, capsys, template, skill_rel):
    """세 출하 템플릿의 표준 show 관용구는 false-block 없이 rc=0이다."""
    skill = REPO / "templates" / template / skill_rel
    # 행 번호 핀 금지 — 인접 편집(T-0471 sweep)으로 줄이 밀리면 무관 실패가 난다.
    # 관용구 내용으로 실 출하 줄을 찾는다(부재 자체가 곧 회귀 신호).
    marker = "board.py show T-NNNN 로 확인"
    matches = [ln.strip() for ln in skill.read_text(encoding="utf-8").splitlines()
               if marker in ln]
    assert matches, f"{skill} 에 표준 show 관용구가 없다"
    shipped_line = matches[0]
    rc, fake, _wt = _run_pm_home_prompt(
        pd, monkeypatch, tmp_path, shipped_line,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_show_natural_language_tail_with_write_instruction_rc1(
        pd, monkeypatch, tmp_path, capsys):
    """같은 줄 tail이 파일 write를 지시하면 read-only 명령 면제를 취소해 rc=1."""
    rc, fake, wt = _run_pm_home_prompt(
        pd,
        monkeypatch,
        tmp_path,
        "ticket 본문은 python3 .project_manager/tools/board.py "
        "show T-NNNN 로 확인 후 이 파일을 수정",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


@pytest.mark.parametrize(
    "prose",
    [
        "이 명령의 prefix 규칙을 문서화하라",
        "이 명령의 touches 필드를 문서화하라",
        "미수정 상태로 보고하라",
        "미수정 보고",
    ],
)
def test_read_only_quote_write_verb_substrings_do_not_false_block_rc0(
        pd, monkeypatch, tmp_path, capsys, prose):
    """ASCII 부분 문자열과 한국어 부정접두 상태어는 write 동사로 오인하지 않아 rc=0."""
    rc, fake, _wt = _run_pm_home_prompt(
        pd,
        monkeypatch,
        tmp_path,
        "python3 .project_manager/tools/board.py list --all\n" + prose,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_read_only_quote_real_file_write_still_rc1(
        pd, monkeypatch, tmp_path, capsys):
    """경계 보강 뒤에도 실제 파일 수정 지시는 계속 재앵커되어 rc=1."""
    rc, fake, wt = _run_pm_home_prompt(
        pd,
        monkeypatch,
        tmp_path,
        "python3 .project_manager/tools/board.py list --all\n"
        "이 파일을 수정하라",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


@pytest.mark.parametrize(
    "call",
    [
        "python .project_manager/tools/board.py show T-0466",
        "py -3.12 .project_manager/tools/board.py show T-0466",
        "python3 .project_manager/tools/board.py show <T-NNNN>",
    ],
)
def test_read_only_launcher_and_placeholder_forms_rc0(
        pd, monkeypatch, tmp_path, capsys, call):
    """python/Windows py 버전 런처와 문서용 show placeholder를 read-only 호출로 인정."""
    rc, fake, _wt = _run_pm_home_prompt(
        pd, monkeypatch, tmp_path, call,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize(
    "call",
    [
        "list --user alice",
        "idea list",
        "prefix list",
        "regression check",
        "livegate check",
    ],
)
def test_additional_read_only_board_operations_rc0(
        pd, monkeypatch, tmp_path, capsys, call):
    """실제 비파괴 board 조회/sidecar check 다섯 형태를 화이트리스트한다."""
    rc, fake, _wt = _run_pm_home_prompt(
        pd,
        monkeypatch,
        tmp_path,
        f"python3 .project_manager/tools/board.py {call}",
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize(
    "prompt",
    [
        "`.project_manager/tools` 는 건드리지 마라",
        "수정 금지: .project_manager/tools/pm_delegate.py",
        ".project_manager/tools/board.py 에는 손대지 마라",
    ],
)
def test_targets_engine_code_ignores_negative_context(pd, prompt):
    """B: 경로와 같은 문장/절의 명시적 금지 문맥은 write 타깃이 아니다."""
    assert pd._prompt_targets_engine_code(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        ".project_manager/tools/board.py 를 수정하라",
        ".project_manager/tools/board.py 를 고쳐라",
        ".project_manager/tools 를 고쳐라",
        ".project_manager/./tools/pm_delegate.py 구현을 변경하라",
    ],
)
def test_targets_engine_code_still_blocks_real_writes(pd, prompt):
    """음성 통제: 실제 엔진 write 지시 3종은 계속 재앵커 대상이다."""
    assert pd._prompt_targets_engine_code(prompt) is True


@pytest.mark.parametrize(
    "instruction",
    [
        "show T-0466 출력 형식을 바꿔라",
        "lint 를 고쳐라",
        "이 스크립트를 고쳐라",
        "이 파일을 건드려라",
        "이 파일을 지워라",
        "이 파일을 손봐라",
    ],
)
def test_read_only_board_reference_with_korean_conjugated_write_rc1(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """활용형 write 지시는 board.py 조회 문맥에서도 재앵커되어 rc=1이다."""
    rc, fake, wt = _run_pm_home_prompt(
        pd, monkeypatch, tmp_path,
        "python3 .project_manager/tools/board.py " + instruction,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


@pytest.mark.parametrize(
    "call",
    [
        "show T-0466",
        "list --all",
        "list --task X",
        "lint --gate",
    ],
)
def test_read_only_board_cli_pm_doc_delegation_passes_main(
        pd, monkeypatch, tmp_path, capsys, call):
    """재현: architect + PM 홈 + board.py show/list/lint 인용 PM-doc 위임은 rc=0이다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "wiki/roadmap.md 를 갱신하라\n"
        f"python3 .project_manager/tools/board.py {call}",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "architect", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(**{
            "delegate.architect.harness": "codex",
            "delegate.architect.model": "gpt-x",
        }),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_negative_context_pm_doc_delegation_passes_main(
        pd, monkeypatch, tmp_path, capsys):
    """architect + PM 홈 + 엔진 경로 금지 문맥 PM-doc 위임도 rc=0으로 실행된다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "wiki/roadmap.md 를 갱신하라\n"
        "`.project_manager/tools` 는 건드리지 마라",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "architect", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(**{
            "delegate.architect.harness": "codex",
            "delegate.architect.model": "gpt-x",
        }),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_read_only_call_does_not_hide_later_engine_write(pd):
    """화이트리스트 호출이 있어도 별도 실제 write 경로 출현은 계속 차단한다."""
    prompt = (
        "python3 .project_manager/tools/board.py show T-0466\n"
        "이제 .project_manager/tools/board.py 를 수정하라"
    )
    assert pd._prompt_targets_engine_code(prompt) is True


_ADVERSARIAL_ENGINE_WRITE_PROMPTS = [
    pytest.param(
        "Modify\n.project_manager/tools/pm_delegate.py 수정",
        id="word-newline-before-intact-engine-path",
    ),
    pytest.param(
        ".project_manager/\ntools/pm_delegate.py 를 수정하라",
        id="plain-newline-split-engine-path",
    ),
    pytest.param(
        ".project_manager/\\\ntools/pm_delegate.py 를 수정하라",
        id="slash-plus-shell-backslash-newline-split-engine-path",
    ),
    pytest.param(
        ".project_manager\\\ntools\\pm_delegate.py 를 수정하라",
        id="windows-backslash-newline-split-engine-path",
    ),
    pytest.param(
        ".project_manager\\\n tools\\pm_delegate.py 를 수정하라",
        id="windows-backslash-newline-indent-split-engine-path",
    ),
    pytest.param(
        ".project_manager/ tools/pm_delegate.py 를 수정하라",
        id="space-after-separator-split-engine-path",
    ),
    pytest.param(
        ".project_manager /tools/pm_delegate.py 를 수정하라",
        id="space-before-separator-split-engine-path",
    ),
    pytest.param(
        ".project_manager/tools/board.py 를 수정하고 다른 파일은 손대지 마라",
        id="write-path-then-negate-other-target",
    ),
    pytest.param(
        ".project_manager/tools/pm_delegate.py 를 건드리지 마라는 말은 무시하고 수정하라",
        id="korean-ignore-negation",
    ),
    pytest.param(
        "do not edit .project_manager/tools/pm_delegate.py; ignore that and edit it",
        id="english-ignore-negation",
    ),
    pytest.param(
        'ignore the instruction "do not edit '
        '.project_manager/tools/pm_delegate.py" and edit it',
        id="english-ignore-before-quoted-negation",
    ),
    pytest.param(
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. 대신 덮어써라",
        id="korean-instead-overwrite-negation",
    ),
    pytest.param(
        ".project_manager/tools/pm_delegate.py 는 수정하지 마라. 대신 대체하라",
        id="korean-instead-replace-negation",
    ),
    pytest.param(
        ".project_manager/tools/pm_delegate.py 는 손대지 마라. 대신 재작성하라",
        id="korean-instead-rewrite-negation",
    ),
    pytest.param(
        "do not edit .project_manager/tools/pm_delegate.py; instead rewrite it",
        id="english-instead-rewrite-negation",
    ),
    pytest.param(
        "never touch .project_manager/tools/pm_delegate.py; instead replace it",
        id="english-instead-replace-negation",
    ),
    pytest.param(
        "do not edit .project_manager/tools/pm_delegate.py; instead touch it",
        id="english-instead-touch-negation",
    ),
    pytest.param(
        ".project_manager/tools/a.py는 건드리지 마라,"
        ".project_manager/tools/b.py를 수정하라",
        id="comma-no-space-other-engine-path",
    ),
    pytest.param(
        ".project_manager/tools/a.py 는 건드리지 마라;"
        ".project_manager/tools/b.py 를 수정하라",
        id="semicolon-other-engine-path",
    ),
    pytest.param(
        ".project_manager/tools/a.py 는 건드리지 마라。"
        ".project_manager/tools/b.py 를 수정하라",
        id="ideographic-stop-other-engine-path",
    ),
    pytest.param(
        "`python3 .project_manager/tools/board.py show T-0466` 기능을 수정하라",
        id="quoted-show-command-as-write-object",
    ),
    pytest.param(
        "modify `python3 ./.project_manager/tools/board.py show T-0466`",
        id="prefixed-write-verb-targets-show-command",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py show T-0466 | sed -i task.md",
        id="show-pipe-followup-write",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py show|sed -i task.md",
        id="show-no-space-pipe-followup-write",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py show T-0466 && sed -i task.md",
        id="show-and-followup-write",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py show T-0466; sed -i task.md",
        id="show-semicolon-followup-write",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py list T-0466",
        id="invalid-list-positional-argument",
    ),
    pytest.param(
        "python3 .project_manager/tools/board.py lint T-0466",
        id="invalid-lint-positional-argument",
    ),
]


@pytest.mark.parametrize("prompt", _ADVERSARIAL_ENGINE_WRITE_PROMPTS)
def test_adversarial_contexts_are_not_exempted(pd, prompt):
    """금지 절 전파·show span 주변 write·shell 후속 명령 우회는 모두 보수적으로 차단한다."""
    assert pd._prompt_targets_engine_code(prompt) is True


def test_normal_multiple_path_listing_does_not_form_engine_path(pd):
    """독립된 정상 경로 나열의 공백/개행은 합치지 않아 `.project_manager/tools`로 오탐하지 않는다."""
    prompt = (
        "검토 경로:\n"
        "- .project_manager/wiki/roadmap.md\n"
        "- tools/pm_delegate.py\n"
        "- tests/test_pm_delegate.py"
    )
    assert pd._prompt_targets_engine_code(prompt) is False


@pytest.mark.parametrize("separator", ["⁄", "∕", "⧸", "／", "╱", "⟋"])
def test_unicode_confusable_separator_is_noncanonical_and_harmless(pd, separator):
    """유니코드 slash 동형문자 6종은 실제 경로 구분자가 아니므로 비매칭 동작을 박제한다."""
    assert pd._prompt_targets_engine_code(
        f".project_manager{separator}tools/pm_delegate.py 를 수정하라"
    ) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "wiki/roadmap.md 를 갱신하라\n"
        "`python3 ./.project_manager/tools/board.py show T-0466`",
        "wiki/roadmap.md 를 갱신하라\n"
        '"python3 .project_manager/tools/board.py show T-0466"',
        "wiki/roadmap.md 를 갱신하라\n"
        "```sh\npython3 .project_manager/tools/board.py show T-0466\n```",
        "wiki/roadmap.md 를 갱신하라\n"
        "```sh\npython3 .project_manager/\ntools/board.py show T-0466\n```",
        "wiki/roadmap.md 를 갱신하라\n"
        "`python3 .project_manager/ tools/board.py show T-0466`",
        "wiki/roadmap.md 를 수정하되 "
        "`.project_manager/tools/board.py`는 건드리지 마라",
        "wiki/roadmap.md 를 수정하라; "
        "do not edit ./.project_manager/tools/pm_delegate.py",
        "wiki/roadmap.md 를 수정하라; "
        "should not edit ./.project_manager/tools/pm_delegate.py",
        "wiki/roadmap.md 를 수정하라。"
        "수정 금지: `./.project_manager/tools/pm_delegate.py`",
    ],
)
def test_only_two_narrow_exemption_forms_remain_valid(pd, prompt):
    """인용/백틱/./·fence 변형도 순수 read-only 호출 또는 직접 결합 금지만 통과한다."""
    assert pd._prompt_targets_engine_code(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. "
        "대신 wiki/roadmap.md 를 수정하라",
        "do not edit .project_manager/tools/pm_delegate.py; "
        "instead edit wiki/roadmap.md",
    ],
)
def test_negative_engine_path_redirected_to_non_engine_path_is_exempt(pd, prompt):
    """대신/instead의 명시 대상이 비-엔진 경로면 엔진 금지는 유지되어 false-block하지 않는다."""
    assert pd._prompt_targets_engine_code(prompt) is False


@pytest.mark.parametrize(
    "instruction",
    [
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. "
        "lint 경고는 무시하고 wiki/roadmap.md 를 수정하라",
        "Do not edit .project_manager/tools/pm_delegate.py. "
        "Ignore the lint warnings and update wiki/roadmap.md",
        ".project_manager/tools/pm_delegate.py 는 수정하지 마라. "
        "설정 override 규칙을 wiki 에 수정 반영하라",
        ".project_manager/tools/pm_delegate.py 는 수정하지 마라. "
        "대신 그 대안으로 wiki 문서를 수정하라",
    ],
)
def test_ignore_override_redirected_to_non_engine_path_rc0(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """ignore/override도 명시 비-엔진 write 대상과 결합하면 엔진 금지를 유지해 rc=0."""
    rc, fake, _wt = _run_pm_home_prompt(
        pd, monkeypatch, tmp_path, instruction,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize(
    "prompt",
    [
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. "
        "대신 .project_manager/tools/pm_delegate.py 를 수정하라",
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. 대신 그것을 수정하라",
        "do not edit .project_manager/tools/pm_delegate.py; instead edit it",
    ],
)
def test_negative_engine_path_same_or_pronoun_redirect_still_blocks(pd, prompt):
    """같은 엔진 경로 또는 무경로 대명사 redirect는 금지 취소이므로 계속 write로 판정한다."""
    assert pd._prompt_targets_engine_code(prompt) is True


@pytest.mark.parametrize(
    "instruction",
    [
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. "
        "대신 wiki/roadmap.md 를 수정하라",
        "do not edit .project_manager/tools/pm_delegate.py; "
        "instead edit wiki/roadmap.md",
    ],
)
def test_non_engine_redirect_passes_main(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """통합: 명시 비-엔진 redirect는 PM 홈에서도 재앵커하지 않고 하네스를 실행해 rc=0이다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(instruction, encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_pronoun_redirect_does_not_capture_path_from_next_sentence(
        pd, monkeypatch, tmp_path, capsys):
    """instead 절의 대명사 redirect가 다음 문장 비-엔진 경로에 오귀속되지 않아 rc=1이다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "do not edit .project_manager/tools/pm_delegate.py; instead edit it. "
        "See wiki/roadmap.md.",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


@pytest.mark.parametrize(
    "instruction",
    [
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. "
        "대신 .project_manager/tools/pm_delegate.py 를 수정하라",
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라. 대신 그것을 수정하라",
        "do not edit .project_manager/tools/pm_delegate.py; instead edit it",
    ],
)
def test_same_or_pronoun_redirect_fails_main(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """통합: 동일 경로·무경로 대명사 redirect는 재앵커 fail-loud(rc=1)한다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(instruction, encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


def test_show_quote_followed_by_general_task_instructions_passes_main(
        pd, monkeypatch, tmp_path, capsys):
    """show 본문 참조 뒤 일반 지시는 정당 관용구로 rc=0 — 대명사 의미 추론 과잉 조임을 막는다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "python3 .project_manager/tools/board.py show T-0466\n"
        "위 티켓 본문이 단일 진실이다.\n"
        "본문대로 구현하고 테스트하라.",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize(
    "instruction",
    [
        "```sh\npython3 .project_manager/tools/board.py show T-0466\n```\n"
        "위 명령의 기능을 수정하라",
        "아래 명령의 기능을 수정하라:\n```sh\n"
        "python3 .project_manager/tools/board.py show T-0466\n```",
        "`python3 .project_manager/tools/board.py show T-0466`\n"
        "이 명령의 기능을 설명하고 wiki/roadmap.md 를 수정하라.",
        "- `python3 .project_manager/tools/board.py show T-0466`\n"
        "- 위 명령을 참고해 wiki/roadmap.md 를 수정하라.",
        "python3 .project_manager/tools/board.py show T-0466\n"
        "이를 근거로 wiki/roadmap.md 를 수정하라.",
    ],
)
def test_general_command_reference_with_non_engine_write_rc0(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """일반 명령/기능/대명사 참조는 파일 지시어가 아니므로 read-only 면제를 취소하지 않는다."""
    rc, fake, _wt = _run_pm_home_prompt(
        pd, monkeypatch, tmp_path, instruction,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_prior_pm_doc_sentence_does_not_target_later_show_quote(
        pd, monkeypatch, tmp_path, capsys):
    """완결된 PM-doc write 문장은 뒤 show 인용의 파일 참조 지시로 전파되지 않아 rc=0이다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "wiki/roadmap.md 를 수정하라.\n"
        "다음 명령으로 티켓을 확인하라.\n"
        "python3 .project_manager/tools/board.py show T-0466",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize(
    "instruction",
    [
        "위 스크립트를 수정하라.",
        "이 파일을 편집하라.",
        "Modify the script above.",
        "Edit that file.",
    ],
)
def test_show_quote_followed_by_file_reference_write_fails_main(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """show 인용을 지시어+파일명사로 write 대상으로 삼으면 한/영 모두 rc=1이다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "python3 .project_manager/tools/board.py show T-0466\n"
        + instruction,
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


def test_patch_show_quote_file_reference_fails_main(
        pd, monkeypatch, tmp_path, capsys):
    """추가된 patch 동사도 show 인용 스크립트 write 지시를 rc=1로 차단한다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "python3 .project_manager/tools/board.py show T-0466\n"
        "Patch the script above.",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


def test_ignore_in_prior_sentence_does_not_override_engine_path_prohibition(
        pd, monkeypatch, tmp_path, capsys):
    """이전 지시의 ignore는 뒤 엔진 경로 금지를 폐기하지 않으므로 정상 위임 rc=0이다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "이전 wiki 지시는 무시하라. "
        ".project_manager/tools/pm_delegate.py 는 건드리지 마라",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


def test_write_target_docstring_lists_residual_threat_shapes(pd):
    """판정 함수가 의도적으로 남긴 위협모델 경계 3형태를 docstring에 명시한다."""
    doc = pd._prompt_targets_engine_code.__doc__ or ""
    for phrase in ("자기모순 shape", "separator 없이", "유니코드 동형"):
        assert phrase in doc


def test_wrapped_show_quote_passes_main(
        pd, monkeypatch, tmp_path, capsys):
    """경로 내부 줄바꿈을 접어도 정당한 board show 인용은 원문 span으로 판정해 rc=0이다."""
    home, _wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "wiki/roadmap.md 를 갱신하라\n"
        "python3 .project_manager/\ntools/board.py show T-0466",
        encoding="utf-8",
    )
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--output-dir", str(tmp_path / "raw")],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert capsys.readouterr().out.strip() == "DONE"


@pytest.mark.parametrize("instruction", _ADVERSARIAL_ENGINE_WRITE_PROMPTS)
def test_adversarial_engine_writes_fail_loud_in_main(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """양 게이트 실증: 모든 적대 프롬프트가 codex 실행 전에 rc=1로 재앵커된다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(instruction, encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


@pytest.mark.parametrize(
    "instruction",
    [
        ".project_manager/tools/board.py 를 수정하라",
        ".project_manager/tools 를 고쳐라",
        ".project_manager/./tools/pm_delegate.py 구현을 변경하라",
    ],
)
def test_real_engine_writes_still_fail_loud_in_main(
        pd, monkeypatch, tmp_path, capsys, instruction):
    """음성 통제 통합: 실제 엔진 write 지시 3종은 main 에서 모두 rc=1이다."""
    home, wt = _fake_pm_home(tmp_path)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(instruction, encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home)],
        _enabled_conf(),
        fake,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "재앵커" in err and str(wt) in err
    assert fake.calls == []


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


# ══ ⑫ config lint — 동일-모델 dev/reviewer 경고 (§3.7·never-block·T-0446) ══════

def _reviewer_conf(dev_model: str, reviewer_model: str, **extra) -> dict:
    conf = {
        "delegate.developer.harness": "codex", "delegate.developer.model": dev_model,
        "delegate.code-reviewer.harness": "claude", "delegate.code-reviewer.model": reviewer_model,
    }
    conf.update(extra)
    return conf


def test_lint_same_model_warns(pd):
    """dev 와 code-reviewer 모델 문자열 동일 → 경고 1건(하네스 무관 비교)."""
    findings = pd.lint_same_model(_reviewer_conf("gpt-x", "gpt-x"))
    assert len(findings) == 1
    label, detail = findings[0]
    assert "developer/code-reviewer" in label
    assert "generate≠evaluate" in detail


def test_lint_different_model_no_warn(pd):
    """dev 와 reviewer 모델이 다르면 무경고."""
    assert pd.lint_same_model(_reviewer_conf("gpt-x", "opus")) == []


def test_lint_harness_agnostic_same_model(pd):
    """같은 .model 문자열이면 하네스가 달라도 경고(harness:model 완전일치 폐기·§3.7)."""
    conf = _reviewer_conf("shared-m", "shared-m")
    conf["delegate.code-reviewer.harness"] = "opencode"  # dev=codex·reviewer=opencode·모델 동일
    assert len(pd.lint_same_model(conf)) == 1


def test_lint_alias_maps_same(pd):
    """서로 다른 표기가 같은 alias 에 속하면 경고(alias 경유·정규화 테이블)."""
    conf = _reviewer_conf("gpt-5.6-terra", "ollama/glm")
    conf["delegate.model_alias.base"] = "gpt-5.6-terra, ollama/glm"
    findings = pd.lint_same_model(conf)
    assert len(findings) == 1
    assert "alias 경유" in findings[0][1]


def test_lint_alias_distinct_no_warn(pd):
    """다른 alias(또는 미등록)면 경고 없음 — family 자동추론 없음(문자열+명시 매핑만)."""
    conf = _reviewer_conf("gpt-5.6-terra", "opus")
    conf["delegate.model_alias.a"] = "gpt-5.6-terra"
    conf["delegate.model_alias.b"] = "opus"
    assert pd.lint_same_model(conf) == []


def test_lint_alias_multi_membership_warns(pd):
    """한 모델이 여러 alias 에 속해도 공유 alias 를 놓치지 않는다(동치류 집합 교차·suggestion 3R).

    dev 모델 M 이 alias a·b 둘에 속하고 reviewer 모델 N 은 b 에만 속한다 — 단일 대표 alias 덮어쓰기
    방식이면 M→b 로 굳어 우연히 잡히거나(순서 의존) 놓칠 수 있으나, 집합 교차는 공유 b 로 확정 경고."""
    conf = _reviewer_conf("model-M", "model-N")
    conf["delegate.model_alias.a"] = "model-M, other-X"
    conf["delegate.model_alias.b"] = "model-M, model-N"        # M·N 공유 alias b
    findings = pd.lint_same_model(conf)
    assert len(findings) == 1
    assert "alias 경유" in findings[0][1]


def test_lint_alias_multi_membership_disjoint_no_warn(pd):
    """여러 alias 소속이라도 공유 alias 가 없으면 무경고(교차 없음)."""
    conf = _reviewer_conf("model-M", "model-N")
    conf["delegate.model_alias.a"] = "model-M, other-X"
    conf["delegate.model_alias.b"] = "model-M, other-Y"        # M 은 a·b, N 은 어디에도 없음
    conf["delegate.model_alias.c"] = "model-N"                 # N 은 c 단독 — M 과 공유 0
    assert pd.lint_same_model(conf) == []


def test_lint_unset_reviewer_skips(pd):
    """code-reviewer 매핑 미설정 → skip(경고 대상 아님·조용히 넘어감·lint 는 강제 아님)."""
    assert pd.lint_same_model({"delegate.developer.model": "gpt-x"}) == []


def test_lint_unset_developer_skips(pd):
    """developer 매핑 미설정 → skip(reviewer 만 있어도 비교 대상 없음)."""
    assert pd.lint_same_model({"delegate.code-reviewer.model": "opus"}) == []


def test_lint_hard_tier_same_as_reviewer_warns(pd):
    """developer.hard 모델이 reviewer 와 같으면 hard 티어에 대한 경고(normal 과 별개 축)."""
    conf = _reviewer_conf("normal-m", "hard-m")
    conf["delegate.developer.hard.harness"] = "codex"
    conf["delegate.developer.hard.model"] = "hard-m"
    findings = pd.lint_same_model(conf)
    assert len(findings) == 1
    assert "developer.hard/code-reviewer" in findings[0][0]


def test_lint_empty_conf_no_warn(pd):
    """delegate 미설정(빈 conf) → 경고 없음·크래시 없음."""
    assert pd.lint_same_model({}) == []


def test_cmd_lint_subcommand_never_blocks(pd, monkeypatch, capsys):
    """`pm_delegate.py lint` 는 동일-모델 경고가 있어도 rc=0(never-block·§3.7)."""
    monkeypatch.setattr(pd, "local_config", lambda: _reviewer_conf("gpt-x", "gpt-x"))
    rc = pd.main(["lint"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "generate≠evaluate" in err


def test_cmd_lint_subcommand_clean(pd, monkeypatch, capsys):
    """정합(모델 상이) 시 lint 는 stdout 안내 + rc=0."""
    monkeypatch.setattr(pd, "local_config", lambda: _reviewer_conf("gpt-x", "opus"))
    rc = pd.main(["lint"])
    assert rc == 0
    assert "경고 없음" in capsys.readouterr().out


# ── role preamble drift 가드(§4.3·T-0447): 엔진-shipped 최소본 4개 존재 + 금지 문구 포함 ──
def test_role_preambles_cover_all_four_roles(pd):
    """ROLE_PREAMBLES = 정확히 4역할(developer/researcher/architect/code-reviewer)·비-빈 텍스트."""
    assert set(pd.ROLE_PREAMBLES) == {"developer", "researcher", "architect", "code-reviewer"}
    for role, text in pd.ROLE_PREAMBLES.items():
        assert text.strip(), f"{role} preamble 이 비어 있음"
        assert role.split("-")[0] in text or "서브에이전트" in text  # 역할 정체성 서술


def test_role_preambles_include_prohibition_phrases(pd):
    """4 preamble 모두 금지 문구(commit/push·board 조작·어댑터 수정 금지)를 포함(drift 가드)."""
    adapter_directories = tuple(pd.ADAPTER_DIRECTORIES)
    assert adapter_directories, "엔진 등록부에서 파생된 어댑터 디렉토리가 0개"
    for role, text in pd.ROLE_PREAMBLES.items():
        assert "commit" in text and "push" in text, f"{role}: git 비가역 금지 문구 누락"
        assert "board" in text, f"{role}: board 조작 금지 문구 누락"
        assert "엔진 등록 통합 루트 전체" in text, f"{role}: 어댑터 범위 정의 누락"
        for adapter_dir in adapter_directories:
            assert adapter_dir in text, f"{role}: 등록 어댑터 {adapter_dir} 수정 금지 문구 누락"
        assert text.count(pd._prohibition()) == 1, f"{role}: 공통 금지 문구를 정확히 한 번 써야 함"


def _isolated_delegate(tmp_path: Path, registry_source: str):
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for filename in ("pm_delegate.py", "repo_owned_files.py", "console_encoding.py"):
        shutil.copy2(TOOLS / filename, tools / filename)
    (tools / "pm_import.py").write_text(
        f'ENGINE_REV = "{_load("pm_delegate_rev", TOOLS / "pm_delegate.py").ENGINE_REV}"\n'
        f"{registry_source}\n",
        encoding="utf-8",
    )
    return _load(f"pm_delegate_isolated_{id(tmp_path)}", tools / "pm_delegate.py"), tools


def test_adapter_registry_engine_rev_skew_fails_loud_when_lazy_boundary_is_consumed(
    tmp_path,
):
    """A sensitivity: import는 lazy라 살지만 stamped source를 실제 읽으면 skew가 명시 red다."""
    delegate, tools = _isolated_delegate(
        tmp_path,
        'ADD_HARNESS_ADAPTER = {"claude": ((".claude",), "CLAUDE.md")}',
    )
    source = tools / "pm_import.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            f'ENGINE_REV = "{delegate.ENGINE_REV}"',
            'ENGINE_REV = "v0.0.0-stale"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="엔진 사본 버전 불일치") as exc:
        tuple(delegate.ADAPTER_DIRECTORIES)
    assert getattr(exc.value, "_engine_rev_skew", False) is True
    assert "pm_import.py" in str(exc.value)


def test_adapter_registry_old_value_shape_fails_explicitly_without_character_spread(
    tmp_path,
):
    """A schema sensitivity: 구형 `(dir, doc)` 값은 `.claude` 문자 펼침 전에 명시 실패한다."""
    delegate, _tools = _isolated_delegate(
        tmp_path,
        'ADD_HARNESS_ADAPTER = {"claude": (".claude", "CLAUDE.md")}',
    )

    with pytest.raises(delegate._AdapterRegistrySchemaError, match="adapter_dirs"):
        tuple(delegate.ADAPTER_DIRECTORIES)


@pytest.mark.parametrize(
    ("registry_source", "expected"),
    [
        (
            'ADD_HARNESS_ADAPTER: dict = '
            '{"claude": ((".claude",), "CLAUDE.md")}',
            (".claude",),
        ),
        (
            'REGISTRY = {"claude": ((".claude",), "CLAUDE.md")}\n'
            "ADD_HARNESS_ADAPTER = REGISTRY",
            (),
        ),
        (
            'ADD_HARNESS_ADAPTER = '
            '{"claude": (frozenset({".claude"}), "CLAUDE.md")}',
            (),
        ),
    ],
    ids=("annotated-assignment", "variable-reference", "frozenset-expression"),
)
def test_adapter_registry_notation_changes_do_not_brick_cli(
    tmp_path, registry_source, expected,
):
    """B sensitivity: AnnAssign는 파생하고 비-literal 표기는 일반 문구 degrade·CLI help rc=0."""
    delegate, tools = _isolated_delegate(tmp_path, registry_source)
    assert tuple(delegate.ADAPTER_DIRECTORIES) == expected
    prohibition = delegate.ROLE_PREAMBLES["developer"]
    assert "엔진 등록 통합 루트 전체" in prohibition
    for adapter_dir in expected:
        assert adapter_dir in prohibition

    completed = subprocess.run(
        [sys.executable, str(tools / "pm_delegate.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _template_adapter_surface():
    """출하 템플릿 최상위의 실제 어댑터 디렉토리 합집합(등록 목록과 독립 관측)."""
    pm_import = _load("pm_import_adapter_surface", TOOLS / "pm_import.py")
    template_dirs = {
        dirname
        for names in pm_import.HARNESS_TEMPLATE_DIRS.values()
        if len(names) == 1
        for dirname in names
    }
    infrastructure = {".project_manager", ".github"}
    observed = {
        child.name
        for dirname in template_dirs
        for child in (REPO / "templates" / dirname).iterdir()
        if child.is_dir() and child.name.startswith(".") and child.name not in infrastructure
    }
    assert observed, "실제 템플릿에서 관측한 어댑터 디렉토리가 0개"
    return observed


def _assert_adapter_registry_matches_surface(registered, observed):
    assert set(registered) == set(observed), (
        f"엔진 등록 어댑터와 실제 템플릿 표면 불일치: "
        f"등록={sorted(registered)} 실제={sorted(observed)}"
    )


def test_adapter_registry_matches_actual_template_surface(pd):
    """단일 출처가 템플릿 3타깃의 실제 어댑터 루트를 빠짐없이 포괄한다."""
    _assert_adapter_registry_matches_surface(
        pd.ADAPTER_DIRECTORIES, _template_adapter_surface())


def test_adapter_surface_guard_is_sensitive_to_each_omission(pd):
    """등록 어댑터를 어느 하나라도 빼면 표면 대조 가드가 red임을 전 항목으로 입증한다."""
    observed = _template_adapter_surface()
    _assert_adapter_registry_matches_surface(pd.ADAPTER_DIRECTORIES, observed)
    for omitted in pd.ADAPTER_DIRECTORIES:
        reduced = tuple(d for d in pd.ADAPTER_DIRECTORIES if d != omitted)
        with pytest.raises(AssertionError, match="실제 템플릿 표면 불일치"):
            _assert_adapter_registry_matches_surface(reduced, observed)


# ══ ⑬ 시크릿 판정 양성매칭 2축 (경로축/값축·오탐 폐쇄·T-0472) ═══════════════════
# PM 12차 실측: 정상 conf 키명 `ctx_window_tokens_opencode` 가 `*token*` substring 에 걸려 위임
# 발사가 차단됐다(우회=키명을 풀어 쓰기). 오탐은 없애되 **미탐 방향 금지** — 실 시크릿(파일 경로·
# 크리덴셜 값)은 계속 차단됨을 음성 통제로 박는다.

# 테스트 전용 합성 크리덴셜(형식만 실제와 동일·실 자격증명 아님).
_FAKE_GITHUB_PAT = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_FAKE_ANTHROPIC_KEY = "sk-ant-api03-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5"
# GitHub push protection 이 실형식 리터럴을 차단하므로 런타임 조립(스캔 대상 문자열은 동일).
_FAKE_SLACK_TOKEN = "xoxb-" + "1234567890-A1b2C3d4E5f6G7h8"
_FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX2mpLEKEY"
_FAKE_PEM_BLOCK = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1b2C3d4\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    "prose",
    [
        "local.conf 의 ctx_window_tokens_opencode 임계를 낮춰라",          # PM 12차 실 차단 케이스
        "ctx_window_tokens_opencode=180000 으로 조정하라",                # 정상 키 할당(값=숫자)
        "ctx_window_tokens_claude 와 토큰 수(tokens) 계산을 점검",
        "access_token_refresh 함수를 리팩터하라",                          # 식별자 substring
        "GITHUB_TOKEN 환경변수 *이름*만 언급하는 문서를 갱신",              # 값 없는 키명
        "secret_scan_pattern 상수를 추가하고 credential_helper 설정을 조사",
        "verified_at: e6d6b8602a05ab903f1a110a7209a78687e21140 을 재핀",   # 커밋 SHA(비-시크릿 키명)
        "docs 링크 https://github.com/org/repo2 를 본문에 추가",
        # 실 PM 문서 코퍼스(167 문서) 실측 오탐 — 소스/문서 확장자·산문 조각·디렉토리 줄기
        "tests/test_adapter_token_substitution.py 의 케이스를 늘려라",
        "docs/secret-scan.md 문서를 갱신하라",
        "ADR-0041 의 ctx_window_tokens_claude/_opencode 분기를 확인",
        "opencode json 의 part.tokens.input 필드를 파싱하라",
        "`key/token(다른 계정)` 표기를 정리",
        "log 의 token/input/output/cost 집계를 확인",
        # fix 라운드 추가 — 미탐 폐쇄(basename 앵커·조사 트리밍) 후에도 통과해야 하는 오탐 통제
        "auth_url=https://idp.example.com/oauth/token 을 conf 에 넣어라",   # URL 엔드포인트(외부 MF1)
        "엔드포인트 https://api.example.com/v1/token/refresh 를 문서화",
        "password_policy: enforce_minimum_length 를 문서에 적어라",          # 무숫자 값 완화의 통제
        "`부재[insteadOf/credential` 표기를 정리",                          # 코퍼스 실측 산문 조각
        "**secret** 강조 표기를 본문에서 걷어내라",                          # 마크다운 강조 + 맨 단어
        # fix 라운드 2 추가 — URL 엔드포인트·camelCase 키·glob 인용·한국어 산문 슬래시(라인 코퍼스 실측)
        "엔드포인트 https://api.example.com/v1/tokens/list 를 호출하라",
        "https://api.example.com/oauth/token/refresh 문서를 갱신",
        "maxTokenCount 를 180000 으로 조정",                                # camelCase 키 + 숫자 값
        "accessTokens 배열을 파싱하는 함수를 추가",                          # camelCase 복수형
        'denylist 에 "*.key" 와 `*.pem` 을 추가하라',                       # glob 패턴 인용
        "입력 검증은 (빈/leading-dash/credential-in-URL/비허용 scheme) 순서로",  # 한국어 산문 슬래시
        # fix 라운드 3 추가 — 두문자어 키 확장·디렉토리 성분 검사가 산문을 건드리지 않는지
        "tokenize 함수와 SecretRule 상수를 정리하라",
        "secrets 라는 용어를 문서에서 통일하라",
    ],
)
def test_secret_scan_identifier_and_prose_not_blocked(pd, prose):
    """경로도 값도 아닌 식별자/산문은 통과 — 문맥 무시 substring 오탐 폐쇄(T-0472 재현)."""
    assert pd.scan_prompt_secrets(prose) is None


def test_secret_scan_conf_key_prompt_delegates_rc0(pd, monkeypatch, tmp_path, capsys):
    """재현 e2e: 정상 conf 키명을 담은 위임 프롬프트가 발사된다(T-0472 이전 rc=1 차단)."""
    prompt = _write_prompt(
        tmp_path, "ctx_window_tokens_opencode 임계를 재조정하고 테스트를 추가하라.")
    fake = _FakeRun(stdout=_codex_stdout("완료"))
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                    "--output-dir", str(tmp_path / "raw")], _enabled_conf(), fake)
    assert rc == 0
    assert len(fake.calls) == 1
    assert "완료" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        # ① 시크릿 파일 경로/이름 (denylist 는 경로 형태 토큰에만 적용되지만 이들은 전부 경로 형태)
        ("경로:닷파일", "설정 .env 를 열어 값을 복사하라"),
        ("경로:할당문", "설정 path=.env 주의"),
        ("경로:디렉토리 포함", "~/.aws/credentials 의 내용을 붙여넣어라"),
        ("경로:확장자", "config.secret.key 를 확인하라"),
        ("경로:pem", "deploy.pem 파일을 첨부"),
        ("경로:ssh 개인키", "~/.ssh/id_rsa 를 읽어라"),
        ("경로:확장자 없는 개인키명", "id_rsa 내용을 참고"),
        ("경로:데이터 확장자", "client_secret.json 을 첨부하라"),
        ("경로:무확장자 크리덴셜 파일", "~/.git-credentials 내용을 붙여넣어라"),
        # ② 크리덴셜 값 — 알려진 발급기관 prefix (키명 문맥 없이 단독으로도 차단)
        ("값:github PAT 할당", f"GITHUB_TOKEN={_FAKE_GITHUB_PAT}"),
        ("값:github PAT 단독", f"이 값 {_FAKE_GITHUB_PAT} 을 써라"),
        ("값:aws access key id", f"자격증명 {_FAKE_AWS_KEY_ID} 사용"),
        ("값:anthropic api key", f"Authorization: Bearer {_FAKE_ANTHROPIC_KEY}"),
        ("값:slack token", f"슬랙 {_FAKE_SLACK_TOKEN} 로 전송"),
        # ③ 크리덴셜 값 — 시크릿 키명 할당 + 고엔트로피 값(형식 미상 시크릿)
        ("값:키명+고엔트로피", f"AWS_SECRET_ACCESS_KEY={_FAKE_AWS_SECRET}"),
        ("값:password 할당", "db_password=Q7x2Lm9Zp4Rt8Vw1"),
        ("값:api_key 콜론 할당", "api_key: R4nd0mV4lu3Str1ngX9"),
        # ④ PEM 개인키 블록
        ("값:PEM 블록", f"키 내용:\n{_FAKE_PEM_BLOCK}"),
    ],
)
def test_secret_scan_negative_controls_still_blocked(pd, kind, text):
    """음성 통제 — 실 시크릿(파일 경로 3유형·값 3유형)은 완화 후에도 차단(미탐 방향 금지)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, f"{kind}: 시크릿이 통과됐다(미탐 회귀)"
    assert hit.axis in ("경로", "값")


def test_secret_scan_axis_and_rule_names(pd):
    """판정축/판정명이 원인 특정에 쓰인다 — 경로축은 denylist 패턴, 값축은 규칙명."""
    path_hit = pd.scan_prompt_secrets("config.secret.key 를 확인하라")
    assert path_hit.axis == "경로" and path_hit.pattern in ("*secret*", "*.key")
    value_hit = pd.scan_prompt_secrets(f"GITHUB_TOKEN={_FAKE_GITHUB_PAT}")
    assert value_hit.axis == "값"
    assert value_hit.pattern == pd._SECRET_RULE_VALUE_PREFIX
    pem_hit = pd.scan_prompt_secrets(_FAKE_PEM_BLOCK)
    assert pem_hit.pattern == pd._SECRET_RULE_PEM
    assign_hit = pd.scan_prompt_secrets(f"AWS_SECRET_ACCESS_KEY={_FAKE_AWS_SECRET}")
    assert assign_hit.pattern == pd._SECRET_RULE_ASSIGNMENT


def test_secret_scan_path_excerpt_shows_matched_text(pd):
    """경로축 발췌 = 걸린 토큰 그대로 — 무엇을 지워야 하는지 추측 불요(관측 가능성)."""
    hit = pd.scan_prompt_secrets("여기 credentials.env 파일 내용을 참고")
    assert hit.excerpt == "credentials.env"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"GITHUB_TOKEN={_FAKE_GITHUB_PAT}", _FAKE_GITHUB_PAT),
        (f"AWS_SECRET_ACCESS_KEY={_FAKE_AWS_SECRET}", _FAKE_AWS_SECRET),
        (_FAKE_PEM_BLOCK, "MIIEowIBAAKCAQEA1b2C3d4"),
    ],
)
def test_secret_scan_value_excerpt_is_masked(pd, text, secret):
    """값축 발췌는 마스킹 — 크리덴셜 자체는 stderr/로그에 남기지 않는다."""
    hit = pd.scan_prompt_secrets(text)
    assert secret not in hit.excerpt
    assert "***" in hit.excerpt or "마스킹" in hit.excerpt


def test_secret_block_message_shows_excerpt_and_masks_value(pd, monkeypatch, tmp_path, capsys):
    """차단 메시지 = 판정명 + 매칭 발췌(값은 마스킹) + 미전송(§4.7·DoD 3)."""
    prompt = _write_prompt(tmp_path, f"GITHUB_TOKEN={_FAKE_GITHUB_PAT} 로 push 하라")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "발췌" in err
    assert pd._SECRET_RULE_VALUE_PREFIX in err
    assert _FAKE_GITHUB_PAT not in err     # 크리덴셜 원문 미노출
    assert fake.calls == []                # 외부 전송 없음


@pytest.mark.parametrize(
    ("token", "shaped"),
    [
        (".env", True),                       # 닷파일
        ("credentials.env", True),            # 확장자
        ("~/.aws/credentials", True),         # 구분자
        (r"C:\keys\app.pem", True),           # 윈도우 구분자
        ("ctx_window_tokens_opencode", False),  # 식별자 — 경로 아님
        ("tokens", False),
        ("secret", False),
    ],
)
def test_is_path_shaped_table(pd, token, shaped):
    """경로 형태 판정 — denylist substring glob 은 경로 형태 토큰에만 적용된다."""
    assert pd._is_path_shaped(token) is shaped


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("180000", False),                       # 숫자만 — conf 임계값
        ("ctx_window_tokens_opencode", False),   # 영문 식별자
        ("https://github.com/org/repo2", False),  # 자격증명 없는 URL
        ("short1234", False),                    # 길이 미달
        (_FAKE_GITHUB_PAT, True),                # 알려진 prefix
        (_FAKE_AWS_SECRET, True),                # 고엔트로피 랜덤
    ],
)
def test_looks_like_secret_value_table(pd, value, expected):
    """값-형태 판정 — 길이·영숫자 혼합·엔트로피(또는 알려진 prefix)로 크리덴셜만 남긴다."""
    assert pd._looks_like_secret_value(value) is expected


def test_secret_scan_loads_external_review_once(pd, monkeypatch):
    """형제 모듈 로드는 스캔당 1회 — 토큰마다 재-import 하면 긴 프롬프트에서 비용이 폭증한다."""
    real = pd._load_external_review
    calls = []

    def _counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(pd, "_load_external_review", _counting)
    pd.scan_prompt_secrets(" ".join(f"토큰{i} ctx_window_tokens_opencode" for i in range(200)))
    assert len(calls) == 1


def test_prompt_file_path_ancestor_directory_name_not_blocked(pd, tmp_path):
    """조상 디렉토리 이름의 substring 은 prompt-file 을 차단하지 않는다(경로 오탐 폐쇄·T-0472).

    파일 *이름* 은 여전히 판정 대상이다(`.env`·`credentials.env`·`id_rsa`)."""
    nested = tmp_path / "T-0472-token-secret-guard"
    nested.mkdir()
    clean = nested / "prompt.md"
    clean.write_text("task", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(clean) is None
    for name in (".env", "credentials.env", "id_rsa"):
        secret_file = nested / name
        secret_file.write_text("x", encoding="utf-8")
        assert pd._prompt_file_denylist_pattern(secret_file) is not None, name


# ══ ⑭ 시크릿 판정 fix 라운드 (미탐 폐쇄 역방향 프로브 · 리뷰 2건 반영·T-0472) ═══
# 1차분(양성매칭 2축)이 오탐을 없애며 **옛 판정이 잡던 실 시크릿 경로/값까지** 통과시킨 회귀를 닫는다.
# 각 케이스는 리뷰 실측표의 한 항목을 찌른다 — 조사 밀착·비ASCII 경로 성분·env 변수 경로·마크다운
# 강조·따옴표 공백 값·무숫자 고엔트로피 값·rc 파일·URL 내장 자격증명.

_FAKE_RANDOM_PASSWORD = "XkwPqrLmZvTbNhGf"          # 무숫자·랜덤 대소문자 혼합
_FAKE_SPACED_PASSWORD = "A1pha Bravo C3arlie Delta"  # 따옴표 안 공백 포함
_FAKE_URL_CREDENTIAL_PASSWORD = "s3cretPassw0rdXyz"


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        # ① 경로축 — basename 앵커 전환 전에는 경로 성분 하나의 비ASCII/`@`/`$` 로 전 축이 skip 됐다
        ("조사 밀착", "~/.aws/credentials를 확인하라"),
        ("비ASCII 경로 성분", "/home/사용자/.env 를 읽어라"),
        ("윈도우 비ASCII 경로", r"C:\Users\사용자\.aws\credentials 를 열어라"),
        ("env 변수 경로", "$HOME/.env 내용을 붙여넣어라"),
        ("중괄호 env 경로", "${HOME}/.aws/credentials 를 참고"),
        ("scope 디렉토리", "node_modules/@scope/pkg/.env 를 열어라"),
        ("비ASCII+시크릿 파일명", "/opt/앱/id_rsa 를 참고"),
        ("마크다운 강조", "**~/.aws/credentials** 를 확인"),
        ("백틱+조사", "설정 `.env`를 열어라"),
        # ② 데드 상수였던 rc 파일명(외부 MF4)
        ("npmrc", "~/.npmrc 를 첨부하라"),
        ("netrc", "~/.netrc 를 붙여넣어라"),
        # ③ 값축 — 할당값 캡처 한계(외부 MF3)
        ("따옴표 공백 값", f'db_password="{_FAKE_SPACED_PASSWORD}"'),
        ("무숫자 고엔트로피 값", f'db_password="{_FAKE_RANDOM_PASSWORD}"'),
        # ④ URL 내장 자격증명(외부 MF1 의 뒷면 — URL 면제는 자격증명 없는 URL 에만)
        ("자격증명 URL",
         f"https://user:{_FAKE_URL_CREDENTIAL_PASSWORD}@git.example.com/org/repo 를 clone"),
    ],
)
def test_secret_scan_review_miss_probes_blocked(pd, kind, text):
    """리뷰 실측표 미탐 케이스가 전부 재차단된다(fix 라운드 역방향 프로브)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, f"{kind}: 실 시크릿이 통과됐다(미탐)"
    assert hit.axis in ("경로", "값")


def test_secret_path_candidates_trims_particle_and_wrappers(pd):
    """끝 비ASCII 런(조사)·대칭 강조·wrapper 를 벗겨 경로 후보를 낸다(토큰화 층).

    ``key/token(다른`` 의 조각 `key/token` 은 후보로는 나오되(fix3 조각 재판정) **판정에서** 걸러진다 —
    무확장자 상대경로는 경로가 아니라는 요건(`_is_named_path_shape`)이 그 경계다."""
    assert "~/.aws/credentials" in pd._secret_path_candidates("~/.aws/credentials를")
    assert ".env" in pd._secret_path_candidates("`.env`를")
    assert "~/.aws/credentials" in pd._secret_path_candidates("**~/.aws/credentials**")
    assert pd.scan_prompt_secrets("log 의 key/token(다른 계정) 집계") is None


@pytest.mark.parametrize(
    ("token", "matched"),
    [
        ("/home/사용자/.env", True),            # 비ASCII 디렉토리 — basename 은 깨끗
        ("${HOME}/.aws/credentials", True),    # env 변수 전개 형태
        ("node_modules/@scope/pkg/.env", True),
        ("/opt/앱/id_rsa", True),
        ("부재[insteadOf/credential", False),   # 산문 마커 — 실 코퍼스 오탐
        ("`json`→token/input", False),
        ("ctx_window_tokens_opencode", False),  # 경로 형태 아님
    ],
)
def test_matching_secret_path_pattern_basename_anchor(pd, token, matched):
    """경로 판정은 basename 앵커 + 산문 마커 배제 — 토큰 전문 strict 는 실 경로를 통째로 skip 했다."""
    er = pd._load_external_review()
    pattern = pd._matching_secret_path_pattern(
        token, er._SECRET_DENYLIST_PATTERNS, er._matching_denylist_pattern)
    assert (pattern is not None) is matched


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('db_password="A1pha Bravo C3arlie Delta"', "A1pha Bravo C3arlie Delta"),
        ("db_password='A1pha Bravo C3arlie Delta'", "A1pha Bravo C3arlie Delta"),
        ("db_password=`A1pha Bravo C3arlie Delta`", "A1pha Bravo C3arlie Delta"),
        ("db_password=Q7x2Lm9Zp4Rt8Vw1", "Q7x2Lm9Zp4Rt8Vw1"),   # 따옴표 없는 값은 공백까지
    ],
)
def test_assignment_value_captures_quoted_span(pd, text, expected):
    """따옴표로 감싼 값은 닫는 따옴표까지 캡처 — 공백에서 끊겨 길이 미달로 미탐이던 갭(외부 MF3)."""
    match = pd._ASSIGNMENT_RE.search(text)
    assert pd._assignment_value(match) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_FAKE_RANDOM_PASSWORD, True),        # 무숫자 랜덤 대소문자 혼합 — 완화로 열린 케이스
        (_FAKE_SPACED_PASSWORD, True),        # 공백 포함(숫자 있음)
        ("enforce_minimum_length", False),    # 무숫자 식별자 — 단어 구분자
        ("MINIMUMLENGTHPOLICY", False),       # 단일 케이스(대문자만)
        ("Thisisaverylongnote", False),       # 산문형 값 — 교대 밀도 미달
        ("<YOUR_API_KEY_HERE>", False),       # 자리표시자
    ],
)
def test_digitless_value_relaxation_table(pd, value, expected):
    """무숫자 값 완화는 '단어 구분자 부재 + 대소문자 혼합 + 교대 밀도'로 조인다(엔트로피 단독 아님)."""
    assert pd._looks_like_secret_value(value) is expected


def test_digitless_relaxation_documented_overlap(pd):
    """완화의 잔여 겹침 — 다중 hump CamelCase 값은 랜덤과 못 갈라 차단 쪽에 남는다(실측 표기).

    발화 조건이 '좌변이 시크릿 키명' 이라 실 코퍼스에는 0건이다(§4.7 한계 표기와 같은 사실)."""
    assert pd._looks_like_secret_value("ValueFormatKnownPrefix") is True
    assert pd.scan_prompt_secrets("secret_rule: ValueFormatKnownPrefix") is not None
    # 시크릿 키명 문맥이 아니면 값축은 발화하지 않는다(완화가 산문 전체로 번지지 않음)
    assert pd.scan_prompt_secrets("rule_name: ValueFormatKnownPrefix") is None


def test_url_endpoint_not_path_axis_but_credentials_url_blocked(pd):
    """자격증명 없는 URL 은 경로축 비대상(외부 MF1) · userinfo 를 담은 URL 은 값축 차단."""
    assert pd.scan_prompt_secrets("auth_url=https://idp.example.com/oauth/token") is None
    hit = pd.scan_prompt_secrets(
        f"https://user:{_FAKE_URL_CREDENTIAL_PASSWORD}@git.example.com/org/repo")
    assert hit is not None
    assert hit.pattern == pd._SECRET_RULE_URL_CREDENTIALS
    assert hit.axis == "값"
    assert _FAKE_URL_CREDENTIAL_PASSWORD not in hit.excerpt   # password 는 마스킹
    assert "git.example.com" in hit.excerpt                   # 위치(호스트)는 남는다


def test_path_axis_excerpt_masks_value_shaped_token(pd):
    """경로축이라도 발췌 토큰이 값-형태면 마스킹한다(값축 원칙과 일관·내부 SF2)."""
    secret_named_key = "Xk9Qm4Rt8Lp2Vb7Zw1Nc5.key"
    hit = pd.scan_prompt_secrets(f"키 파일 {secret_named_key} 를 첨부")
    assert hit is not None and hit.axis == "경로"
    assert secret_named_key not in hit.excerpt
    assert "마스킹" in hit.excerpt
    # 값-형태가 아닌 경로/파일명은 그대로 보여야 고칠 수 있다(관측 가능성)
    assert pd.scan_prompt_secrets("여기 credentials.env 참고").excerpt == "credentials.env"


@pytest.mark.parametrize(
    "name", ["secrets.py", "token.sh", "app_credentials.log", "id_rsa", ".npmrc"],
)
def test_prompt_file_gate_ignores_data_extension_condition(pd, tmp_path, name):
    """④ prompt-file 게이트는 확장자 조건 없이 이름 앵커만 적용한다(내부 SF1).

    파일 *내용이 통째로 전송*되는 지점이라 "소스/문서 확장자는 시크릿 아님"(③ 합성 프롬프트 스캔의
    완화 전제)이 성립하지 않는다 — 같은 이름이라도 ③ 에서는 통과한다."""
    secret_file = tmp_path / name
    secret_file.write_text("x", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(secret_file) is not None


def test_prompt_scan_still_allows_source_file_mentions(pd):
    """③ 합성 프롬프트 스캔의 완화는 유지 — 소스/문서 파일 *언급* 은 시크릿이 아니다."""
    for mention in ("tests/test_secrets.py 를 고쳐라", "scripts/token.sh 를 읽어라"):
        assert pd.scan_prompt_secrets(mention) is None


def test_name_anchored_patterns_cached(pd):
    """이름 앵커 패턴 tuple 은 캐시 재사용 — 토큰마다 재생성하면 긴 프롬프트에서 비용이 쌓인다."""
    pd._name_anchored_patterns.cache_clear()
    prompt = " ".join(f"토큰{i} ctx_window_tokens_opencode docs/note{i}.md" for i in range(50))
    pd.scan_prompt_secrets(prompt)
    info = pd._name_anchored_patterns.cache_info()
    assert info.hits >= 1 and info.currsize == 1


# ══ ⑮ 시크릿 판정 fix 라운드 2 (URL 안 크리덴셜·확장자·키 표기·문서 프롬프트명·T-0472) ══
# 재검(내부 + codex R2)이 잡은 회귀/갭: ① URL 면제가 값축까지 껐고(URL 안 PAT·query token 통과)
# ② 확장자 8자 상한이 `.properties` 를 못 봤고 ③ JSON 따옴표 키·camelCase 키를 못 읽었고
# ④ 프롬프트 문서명(`T-0472-token-guard.md`)을 이 티켓이 없애려던 오탐 클래스 그대로 막았다.

_FAKE_URL_PAT_CLONE = f"https://{_FAKE_GITHUB_PAT}@github.com/org/repo.git"


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("URL userinfo 단독 PAT", f"git clone {_FAKE_URL_PAT_CLONE}"),
        ("URL query token", f"curl https://api.example.com/v1/me?access_token={_FAKE_GITHUB_PAT}"),
        ("URL query AWS key", f"curl https://api.example.com/v1/x?key={_FAKE_AWS_KEY_ID}"),
        ("URL 경로 slack token",
         f"webhook https://hooks.slack.com/services/{_FAKE_SLACK_TOKEN} 로 보내라"),
        ("URL user:pass",
         f"https://user:{_FAKE_URL_CREDENTIAL_PASSWORD}@git.example.com/org/repo 를 clone"),
    ],
)
def test_secret_scan_credentials_inside_url_blocked(pd, kind, text):
    """URL 경로축 면제가 **값축까지 끄면 안 된다** — URL 안에 실린 크리덴셜은 계속 차단(R2 회귀 폐쇄)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, f"{kind}: URL 안 크리덴셜이 통과됐다(미탐 회귀)"
    assert hit.axis == "값"


def test_url_userinfo_pat_fully_masked_in_excerpt(pd):
    """username-only userinfo(PAT clone URL)도 차단 + 발췌에서 **크리덴셜 전문 미노출**."""
    hit = pd.scan_prompt_secrets(f"git clone {_FAKE_URL_PAT_CLONE}")
    assert hit is not None
    assert _FAKE_GITHUB_PAT not in hit.excerpt
    assert "마스킹" in hit.excerpt


def test_url_userinfo_plain_username_not_blocked(pd):
    """평범한 사용자명 userinfo(`https://username@host/…`)는 자격증명이 아니라 통과(오탐 방지)."""
    assert pd.scan_prompt_secrets(
        "https://username@bitbucket.org/team/repo.git 를 clone 하라") is None


@pytest.mark.parametrize(
    ("kind", "text", "blocked"),
    [
        ("원격 시크릿 파일", "https://raw.example.com/o/r/main/deploy/.env 를 받아라", True),
        ("원격 pem", "https://files.example.com/keys/deploy.pem 를 내려받아라", True),
        ("엔드포인트 token", "https://idp.example.com/oauth/token 을 호출", False),
        ("엔드포인트 tokens/list", "https://api.example.com/v1/tokens/list 를 호출", False),
        ("substring 경로", "https://api.example.com/api/secret-store 를 조회", False),
    ],
)
def test_url_path_uses_exact_patterns_only(pd, kind, text, blocked):
    """원격 URL 경로는 **정확-이름/확장자 패턴만** 본다 — 엔드포인트 오탐과 접점을 두지 않는다."""
    assert (pd.scan_prompt_secrets(text) is not None) is blocked, kind


@pytest.mark.parametrize(
    "text",
    [
        "client_secret.properties 를 첨부하라",
        "access_token.properties 를 열어라",
        "config/credentials.properties 내용을 붙여넣어라",
    ],
)
def test_long_extension_secret_files_blocked(pd, text):
    """확장자 상한이 `.properties`(10자)를 못 봐 통째로 통과하던 회귀 폐쇄(외부 리뷰 R2)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None and hit.axis == "경로"


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("JSON 따옴표 키", '{"token": "Q7x2Lm9Zp4Rt8Vw1"}'),
        ("JSON 따옴표 키(single)", "{'password': 'Q7x2Lm9Zp4Rt8Vw1'}"),
        ("camelCase 키", "accessToken=Q7x2Lm9Zp4Rt8Vw1"),
        ("camelCase 키 콜론", "clientSecret: Q7x2Lm9Zp4Rt8Vw1"),
        ("camelCase 키 dbPassword", "dbPassword=Q7x2Lm9Zp4Rt8Vw1"),
    ],
)
def test_quoted_and_camel_case_keys_blocked(pd, kind, text):
    """JSON 따옴표 키·camelCase 키의 고엔트로피 값도 차단(외부 리뷰 R2 — 좌변을 못 읽어 미탐이었다)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, kind
    assert hit.pattern == pd._SECRET_RULE_ASSIGNMENT


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("accessToken", True), ("clientSecret", True), ("dbPassword", True),
        ("GITHUB_TOKEN", True), ("api_key", True), ("apiKey", True),
        ("accessTokens", False),      # 복수형 — `tokens` 배제 규칙과 동형
        ("ctx_window_tokens_opencode", False),
        ("maxTokenCount", True),      # hump 뒤가 대문자 — 키명으로는 잡되 값 판정이 최종 필터
        ("tokenizerName", False),     # 소문자 이어짐 — 크리덴셜 키 아님
    ],
)
def test_secret_key_name_table(pd, key, expected):
    """좌변 키명 판정 — 성분 경계 + camelCase hump. 소문자가 이어지는 합성어는 제외."""
    assert pd._is_secret_key_name(key) is expected


def test_markdown_link_path_blocked(pd):
    """마크다운 링크 `[label](path)` 의 경로도 판정 대상 — `](` 경계에서만 좁게 분리(R2)."""
    hit = pd.scan_prompt_secrets("[설정](~/.aws/credentials) 을 참고")
    assert hit is not None and hit.axis == "경로"
    # 경계 한계: 링크가 아닌 산문 대괄호는 여전히 분리하지 않는다(오탐 재발 방지)
    assert pd.scan_prompt_secrets("`부재[insteadOf/credential` 표기") is None


@pytest.mark.parametrize(
    ("name", "blocked"),
    [
        ("T-0472-token-guard.md", False),      # 티켓 주제어를 담은 정상 프롬프트명(PM 12차 실사용)
        ("secret-scan-review.markdown", False),
        ("credential-flow.rst", False),
        ("secrets.py", True), ("token.sh", True), ("app_credentials.log", True),
        ("id_rsa", True), (".npmrc", True), (".env", True), ("credentials.env", True),
    ],
)
def test_prompt_file_doc_extension_exemption(pd, tmp_path, name, blocked):
    """④ 게이트: 문서 확장자는 이름-substring 패턴 면제(내용은 ⑧ 값축이 재스캔) · 그 외는 차단 유지."""
    prompt_file = tmp_path / name
    prompt_file.write_text("작업 지시", encoding="utf-8")
    assert (pd._prompt_file_denylist_pattern(prompt_file) is not None) is blocked, name


def test_prompt_file_doc_exemption_still_scans_content(pd, monkeypatch, tmp_path, capsys):
    """문서 확장자 면제는 *이름* 뿐 — 내용에 실 크리덴셜이 있으면 합성 프롬프트 스캔이 잡는다."""
    prompt = tmp_path / "T-0472-token-guard.md"
    prompt.write_text(f"이 값을 써라: {_FAKE_GITHUB_PAT}", encoding="utf-8")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
                   _enabled_conf(), fake)
    assert rc == 1
    assert _FAKE_GITHUB_PAT not in capsys.readouterr().err
    assert fake.calls == []


def test_glob_pattern_quotes_not_treated_as_path(pd):
    """denylist glob 인용(`"*.key"`·`*.pem`)은 경로가 아니다 — 대칭 강조만 벗긴다(라인 코퍼스 실측)."""
    assert pd.scan_prompt_secrets('denylist 에 "*.key" 를 추가하라') is None
    assert pd.scan_prompt_secrets("`*.pem` 패턴을 문서화") is None
    assert pd.scan_prompt_secrets("`.env.*` 패턴 설명을 보강") is None
    # 대칭 강조는 계속 벗겨진다(미탐 폐쇄 유지)
    assert pd.scan_prompt_secrets("**~/.aws/credentials** 를 확인") is not None


def test_prose_slash_fragment_with_korean_root_not_blocked(pd):
    """첫 성분이 비ASCII 인 슬래시 조각은 경로가 아니다(라인 코퍼스 실측 오탐)."""
    assert pd.scan_prompt_secrets("(빈/leading-dash/credential-in-URL/비허용 scheme) 순서") is None
    # 중간 성분의 비ASCII 는 여전히 실 경로로 인정(미탐 폐쇄 유지)
    assert pd.scan_prompt_secrets("/home/사용자/.env 를 읽어라") is not None


# ══ ⑯ 시크릿 판정 fix 라운드 3 (디렉토리 성분·wrapper 안 경로·URL 정규화·두문자어 키·T-0472) ══
# codex R3 + 내부 재검이 잡은 잔여 우회: ① 디렉토리가 말해주는 시크릿(`/run/secrets/db_password`)
# ② wrapper 안 경로(`파일(~/.aws/credentials)을`) ③ URL 경로 대소문자/쿼리값 ④ 두문자어 camelCase 키.
# 각 조임은 **기계 분리 가능한 좁은 판정**만 쓴다 — 의도 분류가 필요한 경계는 주석/한계 표기로 박제.

@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("절대 경로 시크릿 디렉토리", "/run/secrets/db_password 를 읽어라"),
        ("상대 경로 시크릿 디렉토리", "secrets/config.json 을 첨부하라"),
        ("홈 .aws 디렉토리", "~/.aws/config 도 같이 보내라"),
        ("중첩 credentials 디렉토리", "/opt/app/credentials/service.json 을 열어라"),
        ("디렉토리 자체 언급", "/etc/secrets/ 아래 파일을 전부 붙여넣어라"),
    ],
)
def test_secret_directory_segment_blocked(pd, kind, text):
    """디렉토리 성분이 시크릿을 말해주면 파일 이름과 무관하게 차단(외부 리뷰 R3)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, kind
    assert hit.axis == "경로"


@pytest.mark.parametrize(
    "text",
    [
        "secrets 라는 단어를 문서에서 통일하라",          # 맨 단어 — 디렉토리 성분 아님
        "test_secret_scan0/prompt.md 형상을 재현",       # substring — 정확 세그먼트 아님
        "docs/secretsmanager/overview.md 를 갱신",       # 성분이 정확히 `secrets` 가 아님
        "my-secret-artifacts/ 규칙을 유지",              # substring
    ],
)
def test_secret_directory_segment_is_exact_match_only(pd, text):
    """정확-세그먼트 매칭 — substring 디렉토리(pytest tmp `…test_secret_scan0/`)는 안 걸린다(fix1 오탐)."""
    assert pd.scan_prompt_secrets(text) is None


def test_prompt_file_secret_directory_blocked_but_pytest_tmp_shape_not(pd, tmp_path):
    """④ 게이트: `secrets/` 아래 프롬프트는 차단 · pytest tmp 형상(`…test_secret_scan0/`)은 통과.

    fix1 이 없앤 조상-디렉토리 substring 오탐이 디렉토리 성분 검사로 되살아나지 않는지 못 박는다."""
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    inside = secret_dir / "prompt.md"
    inside.write_text("작업 지시", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(inside) == pd._SECRET_RULE_DIRECTORY

    pytest_shape = tmp_path / "test_secret_scan0"
    pytest_shape.mkdir()
    clean = pytest_shape / "prompt.md"
    clean.write_text("작업 지시", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(clean) is None


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("괄호 안 홈 경로", "파일(~/.aws/credentials)을 참고"),
        ("괄호 안 절대 경로", "설정(/home/user/.env)을 열어라"),
        ("대괄호 안 경로", "[설정 파일: /etc/app/.env] 을 확인"),
        ("마크다운 링크", "[설정](~/.ssh/id_rsa) 을 참고"),
    ],
)
def test_wrapper_wrapped_paths_blocked(pd, kind, text):
    """wrapper(괄호·대괄호) 안 경로도 조각 재판정으로 잡는다 — 마커에서 토큰을 버리던 우회 폐쇄(R3)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None, kind
    assert hit.axis == "경로"


@pytest.mark.parametrize(
    "prose",
    [
        "`부재[insteadOf/credential` 표기(다른 계정)를 정리",
        "log 의 key/token(다른 계정) 집계를 확인",
        "opencode json 의 part.tokens.input(파싱) 확인",
    ],
)
def test_fragment_rejudgement_keeps_prose_controls(pd, prose):
    """조각 재판정이 옛 산문 오탐을 되살리지 않는다 — 무확장자 상대경로 요건이 경계."""
    assert pd.scan_prompt_secrets(prose) is None


@pytest.mark.parametrize(
    ("kind", "text", "blocked"),
    [
        ("URL 경로 대문자", "https://files.example.com/DEPLOY.PEM 를 받아라", True),
        ("URL 쿼리 파일값", "https://host.example.com/download?file=.env 를 받아라", True),
        ("URL 쿼리 pem", "https://host.example.com/get?path=deploy.PEM 를 받아라", True),
        ("URL 쿼리 엔드포인트 파라미터", "https://api.example.com/v1/list?page=token&limit=20", False),
        ("URL 경로 엔드포인트", "https://idp.example.com/oauth/token 을 호출", False),
    ],
)
def test_url_path_normalization(pd, kind, text, blocked):
    """URL 경로/쿼리값도 일반 경로와 같은 정규화(소문자) — 정확-이름/확장자 원칙은 불변(R3)."""
    assert (pd.scan_prompt_secrets(text) is not None) is blocked, kind


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("XSRFToken", True), ("APIToken", True), ("AWSSecret", True), ("JWTSecret", True),
        ("accessTokens", False), ("tokenizerName", False), ("tokenize", False),
        ("SecretRule", False),                 # 앞 경계 없음(문장 첫 단어형)
        ("ctx_window_tokens_opencode", False),
    ],
)
def test_secret_key_name_acronym_boundary(pd, key, expected):
    """두문자어 접두 camelCase(`XSRFToken`)까지 좌변으로 인정 — 복수/소문자 이어짐은 그대로 제외."""
    assert pd._is_secret_key_name(key) is expected


@pytest.mark.parametrize(
    "text",
    [
        "XSRFToken=Q7x2Lm9Zp4Rt8Vw1",
        "APIToken: Q7x2Lm9Zp4Rt8Vw1",
        "AWSSecret=Q7x2Lm9Zp4Rt8Vw1",
        '{"JWTSecret": "Q7x2Lm9Zp4Rt8Vw1"}',
    ],
)
def test_acronym_camel_case_assignment_blocked(pd, text):
    """두문자어 키 + 고엔트로피 값 할당은 차단(내부 리뷰 R3 실측 4형)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None and hit.pattern == pd._SECRET_RULE_ASSIGNMENT


def test_url_userinfo_username_only_high_entropy_blocked(pd):
    """username-only userinfo 분기의 load-bearing 형태 — 발급기관 prefix 가 **없는** 고엔트로피 값.

    prefix 형(`ghp_…`)은 값축이 먼저 잡으므로 이 분기의 고유 커버리지는 이 케이스뿐이다(내부 리뷰 R3)."""
    opaque = "XkwPqrLmZvTbNhGf1"
    hit = pd.scan_prompt_secrets(f"https://{opaque}@git.internal/team/repo.git 를 clone")
    assert hit is not None
    assert hit.pattern == pd._SECRET_RULE_URL_CREDENTIALS
    assert opaque not in hit.excerpt and "마스킹" in hit.excerpt
    # 음성쌍 — 평범한 사용자명은 자격증명이 아니다
    assert pd.scan_prompt_secrets("https://username@bitbucket.org/team/repo.git") is None


def test_url_excerpt_masks_query_and_fragment_values(pd):
    """URL 발췌는 userinfo 뿐 아니라 **쿼리/fragment 의 자격증명성 값**도 마스킹한다(외부 리뷰 R4).

    파라미터 *이름* 과 경로형 값(`?file=.env`)은 남겨 위치 특정은 유지한다."""
    query_secret, fragment_secret = "Q7x2Lm9Zp4Rt8Vw1", "XkwPqrLmZvTbNhGf1"
    hit = pd.scan_prompt_secrets(
        f"https://user:pass@host.example.com/?access_token={query_secret}")
    assert query_secret not in hit.excerpt and "access_token=" in hit.excerpt
    hit = pd.scan_prompt_secrets(
        f"https://user:pass@host.example.com/page#{fragment_secret}")
    assert fragment_secret not in hit.excerpt
    hit = pd.scan_prompt_secrets("https://user:pass@host.example.com/download?file=.env")
    assert "file=.env" in hit.excerpt   # 경로형 값은 그대로 — 무엇을 지울지 보여야 한다


def test_compound_url_masks_credentials_in_value_and_path_audit_excerpts(
        pd, monkeypatch, tmp_path, capsys):
    """userinfo 자격증명 + 시크릿 파일 경로가 한 URL에 있어도 두 축 모두 같은 마스킹 표시층을 탄다."""
    query_secret = "Q7x2Lm9Zp4Rt8Vw1"
    compound_url = (
        f"https://user:{_FAKE_URL_CREDENTIAL_PASSWORD}@host.example.com/.env"
        f"?access_token={query_secret}"
    )
    hits = pd.scan_prompt_secret_hits(compound_url)
    value_hits = [hit for hit in hits if hit.axis == "값"]
    path_hits = [hit for hit in hits if hit.axis == "경로"]

    assert any(hit.pattern == pd._SECRET_RULE_URL_CREDENTIALS for hit in value_hits)
    assert path_hits
    for hit in (*value_hits, *path_hits):
        assert _FAKE_URL_CREDENTIAL_PASSWORD not in hit.excerpt
        assert query_secret not in hit.excerpt
    assert all("마스킹" in hit.excerpt for hit in (*value_hits, *path_hits))

    prompt = _write_prompt(tmp_path, f"다음 주소를 점검하라: {compound_url}")
    outdir = tmp_path / "raw"
    argv = [
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(tmp_path), "--output-dir", str(outdir),
    ]
    fake = _FakeRun(stdout=_codex_stdout())

    assert _run_main(pd, monkeypatch, argv, _enabled_conf(), fake) == 1
    blocked = capsys.readouterr().err
    digest_match = re.search(r"승인 토큰: ([0-9a-f]{24})", blocked)
    assert digest_match is not None
    assert "값축 판정" in blocked and "경로축 판정" in blocked
    assert _FAKE_URL_CREDENTIAL_PASSWORD not in blocked
    assert query_secret not in blocked

    assert _run_main(
        pd, monkeypatch,
        [*argv, "--secret-scan-ack", digest_match.group(1)],
        _enabled_conf(), fake,
    ) == 0
    approved = capsys.readouterr()
    raw = next(outdir.glob("pm_delegate_codex_*.txt")).read_text(encoding="utf-8")
    assert _FAKE_URL_CREDENTIAL_PASSWORD not in approved.err
    assert query_secret not in approved.err
    assert _FAKE_URL_CREDENTIAL_PASSWORD not in raw
    assert query_secret not in raw
    assert "# secret_scan_ack_hit:" in raw
    assert "값축 판정" in raw and "경로축 판정" in raw


@pytest.mark.parametrize(
    ("kind", "text", "blocked"),
    [
        ("경로 없는 URL 쿼리", "https://files.example?file=.env 를 받아라", True),
        ("경로 없는 URL fragment", "https://files.example#deploy.pem 를 받아라", True),
        ("경로 없는 URL 엔드포인트", "https://api.example?page=token&limit=20 을 호출", False),
    ],
)
def test_url_without_path_still_checks_query(pd, kind, text, blocked):
    """authority 뒤 경로가 없어도 쿼리/fragment 를 검사한다 — 조기 반환 우회 폐쇄(외부 리뷰 R4)."""
    assert (pd.scan_prompt_secrets(text) is not None) is blocked, kind


@pytest.mark.parametrize(
    "name", [".ENV", "DEPLOY.PEM", "Credentials.env", "ID_RSA", ".NPMRC"],
)
def test_prompt_file_name_case_insensitive(pd, tmp_path, name):
    """④ 게이트도 대소문자 무관 — `.ENV`·`DEPLOY.PEM` 이 내용 읽기 전에 통과하던 우회 폐쇄(R4)."""
    prompt_file = tmp_path / name
    prompt_file.write_text("x", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(prompt_file) is not None, name


def test_prompt_file_uppercase_doc_extension_still_exempt(pd, tmp_path):
    """소문자 정규화가 문서 확장자 면제를 깨지 않는다(`T-0472-TOKEN-GUARD.MD` 통과)."""
    prompt_file = tmp_path / "T-0472-TOKEN-GUARD.MD"
    prompt_file.write_text("작업 지시", encoding="utf-8")
    assert pd._prompt_file_denylist_pattern(prompt_file) is None


@pytest.mark.parametrize(
    "text",
    [
        f"토큰({_FAKE_GITHUB_PAT})을 쓴다",
        f"키[{_FAKE_GITHUB_PAT}]를 넣어",
    ],
)
def test_value_axis_splits_on_brackets(pd, text):
    """값축 토큰화도 괄호/대괄호를 경계로 본다 — 한국어 관용 표기의 값축 비대칭 폐쇄(내부 리뷰 R4)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None and hit.pattern == pd._SECRET_RULE_VALUE_PREFIX
    assert _FAKE_GITHUB_PAT not in hit.excerpt


@pytest.mark.parametrize("text", ["deploy/id_rsa 를 참고", "keys/id_ed25519 를 첨부"])
def test_exact_secret_filename_exempt_from_anchor_requirement(pd, text):
    """정확 시크릿 파일명은 앵커 없는 상대경로여도 차단 — 이름 자체가 비모호(내부 리뷰 R4)."""
    hit = pd.scan_prompt_secrets(text)
    assert hit is not None and hit.axis == "경로"


def test_extensionless_substring_relative_path_is_documented_gap(pd):
    """잔여 창 박제(§4.7 한계 ④): 앵커·확장자 없는 substring 이름 상대경로는 미차단.

    `etc/credentials` 는 산문 조각(`key/token`)과 형태가 같아 기계적으로 못 가른다 — 앵커가 붙으면
    (`/etc/credentials`) 차단된다."""
    assert pd.scan_prompt_secrets("etc/credentials 를 참고") is None
    assert pd.scan_prompt_secrets("/etc/credentials 를 참고") is not None


def test_relative_path_with_non_ascii_root_is_documented_boundary(pd):
    """경계 박제: 상대경로 **첫 성분**이 비ASCII 면 경로축 비대상(§4.7 한계 ④)."""
    assert pd.scan_prompt_secrets("문서/설정/.env 를 열어라") is None
    # 앵커가 붙으면(절대·홈) 비ASCII 성분이 있어도 판정 대상
    assert pd.scan_prompt_secrets("/문서/설정/.env 를 열어라") is not None


# ══ T-0474: 명시 설정 기반 loud 인프라 폴백 ═══════════════════════════════════

def _fallback_conf(**extra) -> dict:
    conf = _enabled_conf(**{
        "delegate.developer.fallback.harness": "claude",
        "delegate.developer.fallback.model": "opus",
        "delegate.developer.fallback.reasoning": "high",
    })
    conf.update(extra)
    return conf


class _SequenceRun:
    """primary/fallback 결과를 순서대로 내고 두 드라이버 호출을 모두 기록한다."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append({
            "argv": argv, "stdin_text": stdin_text, "cwd": cwd,
            "env": env, "timeout": timeout, "harness": harness,
        })
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_fallback_uses_its_own_harness_budget(pd, monkeypatch, tmp_path):
    """폴백이 **자기 축의 시간 예산**으로 실행된다 — 축이 다르면 값도 달라야 한다.

    시간 예산이 하네스별로 갈린 뒤(클라우드 vs 로컬 GPU) primary 값을 폴백에 그대로 쓰면,
    클라우드→로컬 GPU 폴백에서 3시간짜리 로컬 작업이 클라우드 예산에 잘린다."""
    relay = _relay_module()
    conf = _enabled_conf(**{
        "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
        "delegate.developer.fallback.harness": "opencode",
        "delegate.developer.fallback.model": "prov/m",
    })
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "", "timed_out": True},   # primary 인프라 실패
        {"returncode": 0, "stdout": _opencode_stdout("fallback ok"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(tmp_path)],
        conf, run_fn=fake)
    assert rc == 0
    assert [c["harness"] for c in fake.calls] == ["codex", "opencode"]
    assert fake.calls[0]["timeout"] == int(relay.HARNESS_PROFILES["codex"].wall_timeout)
    assert fake.calls[1]["timeout"] == int(relay.HARNESS_PROFILES["opencode"].wall_timeout)
    assert fake.calls[1]["timeout"] > fake.calls[0]["timeout"]


def test_resolve_fallback_role_tier_atomic_and_no_default(pd):
    """주 매핑 동형 per-role/tier 원자 tuple: 미설정=None, hard는 normal 폴백을 상속하지 않는다."""
    normal = _fallback_conf()
    assert pd.resolve_fallback(normal, "developer", "normal") == ("claude", "opus", "high")
    assert pd.resolve_fallback(normal, "developer", "hard") is None

    hard = {
        "delegate.developer.hard.fallback.harness": "opencode",
        "delegate.developer.hard.fallback.model": "prov/hard",
        "delegate.developer.hard.fallback.reasoning": "max",
    }
    assert pd.resolve_fallback(hard, "developer", "hard") == ("opencode", "prov/hard", "max")
    with pytest.raises(pd.DelegateError, match="불완전"):
        pd.resolve_fallback(
            {"delegate.developer.fallback.harness": "claude"},
            "developer",
            "normal",
        )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"returncode": 1, "stdout": "", "stderr": "", "timed_out": True}, "타임아웃"),
        ({"returncode": 127, "stdout": "", "stderr": "실행 불가", "timed_out": False,
          "launch_failed": True}, "스폰 실패/바이너리 부재"),
        ({"returncode": 1, "stdout": "PARTIAL", "stderr": "정리 실패", "timed_out": False,
          "cleanup_failed": True}, None),
        ({"returncode": 1, "stdout": "", "stderr": "rate_limit_exceeded", "timed_out": False},
         "한도/레이트리밋"),
        ({"returncode": 1, "stdout": "", "stderr": "You've hit your usage limit.",
          "timed_out": False}, "한도/레이트리밋"),
        ({"returncode": 1, "stdout": "", "stderr": "unexpected status 401 Unauthorized",
          "timed_out": False}, "인증 실패"),
        ({"returncode": 1, "stdout": "", "stderr": "tests rejected: must-fix",
          "timed_out": False}, None),
    ],
)
def test_infrastructure_failure_classifier_positive_only(pd, result, expected):
    """문서/실측 양성 패턴만 분류하고 정상 완료·일반 반려는 보수적으로 폴백하지 않는다."""
    assert pd.classify_infrastructure_failure(result) == expected


@pytest.mark.parametrize(
    "stderr",
    [
        # 성공 turn 인데 하네스가 잔여 한도 배너를 stderr 에 찍은 형(공백 분리 한글 문장 — `\b` 가
        # 살아 패턴이 실제로 매칭된다).
        "경고: rate_limit_exceeded 상태에 근접했습니다. 남은 quota 를 확인하세요",
        # 영문 reply/배너 형 — 문장 안에서도 매칭된다.
        "warning: rate limit reached for this window; the turn completed anyway",
    ],
)
def test_rc_zero_success_is_never_classified(pd, stderr):
    """rc=0 성공은 출력이 한도 문구를 **실제로 매칭**해도 분류 안 함 — `rc == 0` 가드가 load-bearing.

    변이 실증: classify_infrastructure_failure 의 `if rc == 0: return None` 을 지우면 이 케이스들이
    '한도/레이트리밋'으로 분류돼 red 가 된다(가드 없이도 green 이던 이전 케이스는 조사 밀착으로
    `\\b` 가 깨져 애초에 비매칭이었다)."""
    matched = pd._INFRA_QUOTA_PATTERNS[0].search(stderr) or pd._INFRA_QUOTA_PATTERNS[1].search(stderr)
    assert matched is not None, "케이스 문자열이 패턴에 매칭돼야 가드가 load-bearing 하다"
    result = {"returncode": 0, "stdout": _codex_stdout("완료"), "stderr": stderr,
              "timed_out": False}
    assert pd.classify_infrastructure_failure(result) is None


@pytest.mark.parametrize(
    "signal",
    [{"timed_out": True},
     {"launch_failed": True},
     {"stalled": True}],
)
def test_rc_zero_beats_explicit_failure_signal(pd, signal):
    """rc=0 검사가 명시 신호보다 **앞** — 신호 세팅에 버그가 나도 정상 완료는 폴백 안 한다.

    실 엔진은 성공 turn 에 신호를 붙이지 않는다(timeout=rc1·launch=rc127·stall=rc1). 이 조합은
    문서 계약("rc=0 은 절대 폴백 안 함")의 방어적 보장이다 — 검사 순서를 되돌리면 red."""
    result = {"returncode": 0, "stdout": _claude_stdout("완료"), "stderr": "", "timed_out": False}
    result.update(signal)
    assert pd.classify_infrastructure_failure(result) is None


def _codex_error_event(message: str) -> str:
    return _json.dumps({"type": "error", "message": message})


def _codex_user_echo(text: str) -> str:
    return _json.dumps({"type": "item.completed",
                        "item": {"type": "user_message", "text": text}})


def test_failure_scan_excludes_reply_and_prompt_echo(pd):
    """rc≠0 스캔은 stderr + stdout **error 이벤트**만 — reply/프롬프트 에코 인용은 폴백 미발동.

    실측 재현(2건): 폴백 규칙 자체를 리뷰 위임하면 에이전트 reply(agent_message)와 프롬프트 에코가
    `rate_limit_exceeded` 를 인용해 stdout 전문 스캔이 부당 폴백을 냈다(자기참조 재현)."""
    quote = "패턴 rate_limit_exceeded 와 401 Unauthorized 를 문서화했다"
    echoed = "\n".join([_codex_stdout(quote), _codex_user_echo(quote)])
    assert pd.classify_infrastructure_failure(
        {"returncode": 1, "stdout": echoed, "stderr": "", "timed_out": False}) is None
    # 같은 문구라도 stderr(하네스 진단 채널)면 양성 — 경계가 채널이지 문구가 아님을 고정.
    assert pd.classify_infrastructure_failure(
        {"returncode": 1, "stdout": echoed, "stderr": quote, "timed_out": False}
    ) == "한도/레이트리밋"
    # stdout 채널도 error 이벤트면 살아있다(죽은 채널 아님).
    assert pd.classify_infrastructure_failure({
        "returncode": 1,
        "stdout": "\n".join([_codex_stdout(quote),
                             _codex_error_event("stream error: rate_limit_exceeded")]),
        "stderr": "", "timed_out": False,
    }) == "한도/레이트리밋"


def test_error_event_reply_field_is_not_scanned(pd):
    """error 표식이 붙은 이벤트라도 reply 본문 필드(claude `result`)는 스캔 대상이 아니다."""
    event = _json.dumps({"type": "result", "is_error": True, "session_id": "s1",
                         "result": "리뷰 결과: rate_limit_exceeded 처리를 보강하라"})
    assert pd.classify_infrastructure_failure(
        {"returncode": 1, "stdout": event, "stderr": "", "timed_out": False}) is None


def _failed_stdout(stdout: str) -> dict:
    return {"returncode": 1, "stdout": stdout, "stderr": "", "timed_out": False}


def _claude_api_retry_event(error: str) -> str:
    """claude 2.1.220 무효-key 실측 stdout JSONL 형상(error 값만 교체 가능)."""
    return _json.dumps({
        "type": "system",
        "subtype": "api_retry",
        "attempt": 10,
        "max_retries": 10,
        "retry_delay_ms": 32126,
        "error_status": 401,
        "error": error,
        "session_id": "5b1f6a05-measured-shape",
        "uuid": "4dbf088f-measured-shape",
    })


def _claude_assistant_error_event(error: str) -> str:
    """claude 2.1.220 fresh-config 미로그인 실측 stdout JSONL 형상."""
    return _json.dumps({
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "Not logged in · Please run /login"}],
        },
        "error": error,
        "is_api_error_message": True,
        "session_id": "0d512ef0-measured-shape",
        "uuid": "97241cee-measured-shape",
    })


def _opencode_api_error_event(response_body: str) -> str:
    """opencode 1.18.4 provider APIError 실측 stdout JSONL 형상(responseBody passthrough)."""
    return _json.dumps({
        "type": "error",
        "timestamp": 1785456000000,
        "sessionID": "ses_measured_shape",
        "error": {
            "name": "APIError",
            "data": {
                "message": "API key is invalid.",
                "statusCode": 401,
                "isRetryable": False,
                "responseBody": response_body,
                "metadata": {"url": "https://api.anthropic.com/v1/messages"},
            },
        },
    })


def test_claude_api_retry_authentication_failed_is_auth(pd):
    """무효 key: api_retry 비-reply error enum이 AUTH 양성 근거다."""
    event = _claude_api_retry_event("authentication_failed")
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) == pd.FAILURE_CLASS_AUTH


def test_claude_assistant_authentication_failed_is_auth(pd):
    """미로그인: 사람용 content는 제외돼도 top-level error enum으로 AUTH를 분류한다."""
    event = _claude_assistant_error_event("authentication_failed")
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) == pd.FAILURE_CLASS_AUTH


@pytest.mark.parametrize(
    ("event_factory", "error"),
    [
        (_claude_api_retry_event, "rate_limit"),
        (_claude_api_retry_event, "billing_error"),
        (_claude_assistant_error_event, "rate_limit"),
        (_claude_assistant_error_event, "billing_error"),
    ],
)
def test_claude_error_enums_are_quota(pd, event_factory, error):
    """실측한 두 claude 이벤트 형상에서 quota error enum만 바꿔도 QUOTA로 분류한다."""
    assert pd.classify_infrastructure_failure(
        _failed_stdout(event_factory(error))
    ) == pd.FAILURE_CLASS_QUOTA


def test_opencode_provider_authentication_error_passthrough_is_auth(pd):
    """APIError name/status int가 아니라 responseBody의 상류 401 enum이 AUTH 근거다."""
    response_body = _json.dumps({
        "type": "error",
        "error": {"type": "authentication_error", "message": "API key is invalid."},
    })
    event = _opencode_api_error_event(response_body)
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) == pd.FAILURE_CLASS_AUTH


@pytest.mark.parametrize(
    "response_body",
    [
        _json.dumps({"type": "error", "error": {"type": "rate_limit_error"}}),
        _json.dumps({"type": "error", "error": {"message": "Credit balance is too low"}}),
    ],
)
def test_opencode_provider_quota_passthrough_is_quota(pd, response_body):
    """opencode가 responseBody에 전달한 Anthropic enum/메시지를 QUOTA로 분류한다."""
    event = _opencode_api_error_event(response_body)
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) == pd.FAILURE_CLASS_QUOTA


@pytest.mark.parametrize(
    ("reply_key", "quote"),
    [
        ("result", "authentication_failed"),
        ("text", "authentication_error"),
        ("result", "rate_limit"),
        ("text", "rate_limit_error"),
        ("result", "billing_error"),
        ("text", "credit balance is too low"),
        ("result", "credit balance too low"),
        ("text", "Not logged in"),
    ],
)
def test_new_infra_terms_in_reply_fields_are_not_classified(pd, reply_key, quote):
    """각 신규 패턴(및 실측 사람용 문구)은 rc≠0 error 이벤트의 reply 인용만으로 발동하지 않는다."""
    event = _json.dumps({
        "type": "error",
        "is_error": True,
        "session_id": "echo-only",
        reply_key: f"문서에 {quote} 패턴을 인용했다",
    })
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) is None


def test_bare_rate_limit_pattern_has_token_boundary(pd):
    """bare enum은 단독 줄만 잡는다 — 합성어·산문 문장-중간 표기는 배제한다."""
    bare = next(pattern for pattern in pd._INFRA_QUOTA_PATTERNS
                if pattern.pattern == r"^\s*rate_limit\s*$")
    dedicated = next(pattern for pattern in pd._INFRA_QUOTA_PATTERNS
                     if pattern.pattern == r"\brate_limit_error\b")
    assert bare.search("rate_limit_options_menu") is None
    assert bare.search("rate_limit_error") is None
    assert bare.search("configuration key rate_limit is invalid") is None
    assert bare.search("diag\nrate_limit\ndiag") is not None  # 진단 join 의 독립 줄 = 실측 enum 형상
    assert bare.search("diag\nrate_limit\r\ndiag") is not None  # CRLF stderr 보험
    assert dedicated.search("rate_limit_error") is not None


@pytest.mark.parametrize(
    "unlisted",
    ["overloaded", "server_error", "invalid_request", "oauth_org_not_allowed",
     "API key is invalid."],
)
def test_complement_enums_stay_unclassified(pd, unlisted):
    """여집합 결정 핀 — 주석이 '추가하지 않는다'고 못박은 표기는 단독 enum 줄로 와도 None 이다.
    이 테스트가 red 면 누군가 패턴을 과광범화한 것이다(보수 방향 위반)."""
    event = _claude_api_retry_event(unlisted)
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) is None


@pytest.mark.parametrize("channel", ["stderr", "stdout_diag"])
def test_prose_rate_limit_mention_is_not_quota(pd, channel):
    """산문 진단("configuration key rate_limit is invalid")은 stderr/이벤트 진단 어느 채널에서도
    quota 로 오분류되지 않는다(단독-줄 앵커 음성 회귀)."""
    prose = "configuration key rate_limit is invalid"
    if channel == "stderr":
        res = {"returncode": 1, "stdout": "", "stderr": prose, "timed_out": False}
    else:
        event = _json.dumps({"type": "error", "is_error": True, "message": prose})
        res = {"returncode": 1, "stdout": event, "stderr": "", "timed_out": False}
    assert pd.classify_infrastructure_failure(res) is None


def test_opencode_model_not_found_remains_unclassified(pd):
    """카탈로그/config 오류는 한도·인증이 아니므로 fail-loud(None) 여집합에 남긴다."""
    event = _json.dumps({
        "type": "error",
        "timestamp": 1785456000000,
        "sessionID": "ses_catalog_error",
        "error": {
            "name": "UnknownError",
            "data": {"message": "Model not found: unconfigured-provider/missing-model"},
        },
    })
    assert pd.classify_infrastructure_failure(_failed_stdout(event)) is None


def test_launch_failure_needs_explicit_signal_not_rc127(pd):
    """rc=127 추론 폐기 — 정상 실행된 하네스의 자체 exit 127 을 스폰 실패로 오분류하지 않는다."""
    harness_own_127 = {"returncode": 127, "stdout": _codex_stdout("bash: 명령 없음"),
                       "stderr": "script exited 127", "timed_out": False}
    assert pd.classify_infrastructure_failure(harness_own_127) is None
    signalled = pd._launch_failure_result("codex", FileNotFoundError(2, "No such file", "codex"))
    assert signalled[pd.RUN_RESULT_LAUNCH_FAILED] is True
    assert pd.classify_infrastructure_failure(signalled) == pd.FAILURE_CLASS_LAUNCH


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_default_run_fn_launch_error_carries_explicit_signal(pd, monkeypatch, harness):
    """실 드라이버의 launch 정규화도 rc 가 아니라 명시 신호를 싣는다(분류 근거 단일화)."""
    monkeypatch.setattr(pd, "_load_relay",
                        lambda: _FakeRelayWatchdog(FileNotFoundError(2, "No such file", "bin")))
    res = pd._default_run_fn(["bin"], stdin_text="x", cwd="/tmp", env={}, timeout=1,
                             harness=harness)
    assert res[pd.RUN_RESULT_LAUNCH_FAILED] is True
    assert pd.classify_infrastructure_failure(res) == pd.FAILURE_CLASS_LAUNCH


def test_opencode_first_event_stall_is_infrastructure_class(pd, monkeypatch):
    """opencode 첫-이벤트 stall(재시도 소진)은 rc=1/timed_out=False 로 정규화돼도 인프라 실패다."""
    stalled = _FakeRelayWatchdog.StallWatchdogError("3회 소진")
    stalled.timeout_axis = "first-event"
    stalled.threshold_seconds = 90.0
    stalled.silence_seconds = 90.0
    stalled.output = "startup partial stdout\n"
    stalled.stderr = "startup partial stderr\n"
    relay = _FakeRelayWatchdog(stalled)
    monkeypatch.setattr(pd, "_load_relay", lambda: relay)
    res = pd._default_run_fn(["opencode", "run"], stdin_text=None, cwd="/tmp", env={},
                             timeout=1, harness="opencode")
    assert res["returncode"] == 1 and res["timed_out"] is False
    assert res[pd.RUN_RESULT_STALLED] is True
    assert res["stdout"] == "startup partial stdout\n"
    assert "startup partial stderr" in res["stderr"]
    assert pd.classify_infrastructure_failure(res) == pd.FAILURE_CLASS_STALL
    # 엔진 마커만 남은 결과(신호 키 없음)도 백스톱으로 분류된다 — 마커는 엔진이 직접 찍는다.
    marker_only = {"returncode": 1, "stdout": "",
                   "stderr": f"{pd.OPENCODE_STALL_MARKER} 3회 소진]", "timed_out": False}
    assert pd.classify_infrastructure_failure(marker_only) == pd.FAILURE_CLASS_STALL


def test_startup_watchdog_wall_axis_is_timeout_not_first_event_stall(pd, monkeypatch):
    """--timeout이 첫-event 창보다 짧아 wall이 먼저 울면 timeout 분류·안내를 유지한다."""
    expired = _FakeRelayWatchdog.StallWatchdogError("overall watchdog")
    expired.timeout_axis = "wall"
    expired.threshold_seconds = 30.0
    expired.silence_seconds = 30.0
    expired.output = "partial before wall\n"
    expired.stderr = "wall diagnostic\n"
    monkeypatch.setattr(pd, "_load_relay", lambda: _FakeRelayWatchdog(expired))

    res = pd._default_run_fn(
        ["opencode", "run"], stdin_text=None, cwd="/tmp", env={},
        timeout=30, harness="opencode",
    )
    assert res["returncode"] == 1 and res["timed_out"] is True
    assert res[pd.RUN_RESULT_TIMEOUT_AXIS] == "wall"
    assert res[pd.RUN_RESULT_TIMEOUT_THRESHOLD_SEC] == 30.0
    assert pd.RUN_RESULT_STALLED not in res
    assert pd.OPENCODE_STALL_MARKER not in res["stderr"]
    assert "벽시계 백스톱 30s" in res["stderr"]
    assert pd.classify_infrastructure_failure(res) == pd.FAILURE_CLASS_TIMEOUT


def test_stall_triggers_configured_fallback(pd, monkeypatch, tmp_path, capsys):
    """opencode primary 가 stall 로 죽으면 설정된 폴백이 1회 실행된다(분류 누락 회귀 가드)."""
    conf = _fallback_conf(**{"delegate.developer.harness": "opencode",
                             "delegate.developer.model": "prov/m"})
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": f"{pd.OPENCODE_STALL_MARKER} 3회 소진]",
         "timed_out": False, pd.RUN_RESULT_STALLED: True},
        {"returncode": 0, "stdout": _claude_stdout("stall fallback"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(tmp_path / "raw")],
        conf, fake,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert [call["harness"] for call in fake.calls] == ["opencode", "claude"]
    assert f"사유: {pd.FAILURE_CLASS_STALL}" in captured.err


def test_cleanup_failure_forbids_configured_fallback(pd, monkeypatch, tmp_path, capsys):
    """정리 실패는 raw 박제 뒤 잔존 프로세스 위험을 loud로 말하고 설정 폴백을 금지한다."""
    conf = _fallback_conf()
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "PARTIAL", "stderr": "cleanup failed",
         "timed_out": False, pd.RUN_RESULT_CLEANUP_FAILED: True},
        {"returncode": 0, "stdout": _claude_stdout("cleanup fallback"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(tmp_path / "raw")],
        conf, fake,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert [call["harness"] for call in fake.calls] == ["codex"]
    assert "자동 폴백을 실행하지 않습니다" in captured.err
    assert "아직 살아 있을 수 있어" in captured.err
    assert "폴백:" not in captured.err
    primary_raw = sorted((tmp_path / "raw").glob("pm_delegate_codex_*"))[0]
    assert "PARTIAL" in primary_raw.read_text(encoding="utf-8")


def test_dry_run_displays_configured_fallback_mapping(pd, monkeypatch, tmp_path, capsys):
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--dry-run"],
        _fallback_conf(),
        fake,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "폴백: harness=claude model=opus reasoning=high" in out
    assert fake.calls == []


def test_help_documents_opt_in_claude_opus_fallback_example(pd):
    """운용 확정 조합은 채택자 예시로만 노출하고 엔진 런타임 기본값으로 만들지 않는다."""
    help_text = pd.build_arg_parser().format_help()
    assert "delegate.developer.fallback.harness=claude" in help_text
    assert "delegate.developer.fallback.model=opus" in help_text
    assert pd.resolve_fallback({}, "developer", "normal") is None


def test_quota_failure_loud_fallback_and_reply_provenance(
        pd, monkeypatch, tmp_path, capsys):
    """Codex 한도 실패 → claude/opus 1회, loud stderr + 회수 stdout provenance + raw 2개."""
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "error code: rate_limit_exceeded",
         "timed_out": False},
        {"returncode": 0, "stdout": _claude_stdout("폴백 완료"), "stderr": "",
         "timed_out": False},
    )
    outdir = tmp_path / "raw"
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd,
        monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(outdir)],
        _fallback_conf(),
        fake,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert [call["harness"] for call in fake.calls] == ["codex", "claude"]
    assert "폴백: codex→claude(opus) — 사유: 한도/레이트리밋" in captured.err
    assert "실행 provenance: fallback=claude(model=opus)" in captured.out
    assert captured.out.rstrip().endswith("폴백 완료")
    raw = list(outdir.glob("pm_delegate_*.txt"))
    assert len(raw) == 2
    assert any("# attempt: primary" in path.read_text(encoding="utf-8") for path in raw)
    assert any("# attempt: fallback-from-codex:한도/레이트리밋" in
               path.read_text(encoding="utf-8") for path in raw)


def test_spawn_exception_uses_configured_fallback(pd, monkeypatch, tmp_path, capsys):
    """DI/실 subprocess spawn OSError도 rc=127로 정규화되어 폴백한다."""
    fake = _SequenceRun(
        FileNotFoundError(2, "No such file", "codex"),
        {"returncode": 0, "stdout": _claude_stdout("spawn fallback"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    captured = capsys.readouterr()
    assert rc == 0 and len(fake.calls) == 2
    assert "사유: 스폰 실패/바이너리 부재" in captured.err


def test_timeout_uses_configured_fallback(pd, monkeypatch, tmp_path, capsys):
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "", "timed_out": True},
        {"returncode": 0, "stdout": _claude_stdout("timeout fallback"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    captured = capsys.readouterr()
    assert rc == 0 and len(fake.calls) == 2
    assert "사유: 타임아웃" in captured.err


def test_fallback_timeout_message_uses_fallback_harness_value(
        pd, monkeypatch, tmp_path, capsys):
    """codex→opencode 폴백 중단 안내가 primary 3600s가 아니라 실제 fallback 14400s를 찍는다."""
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "", "timed_out": True},
        {"returncode": 1, "stdout": "", "stderr": "", "timed_out": True},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(**{
            "delegate.developer.fallback.harness": "opencode",
            "delegate.developer.fallback.model": "local/model",
            "delegate.developer.fallback.reasoning": "",
        }),
        fake,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert [call["timeout"] for call in fake.calls] == [3600, 14400]
    assert "폴백 위임 turn 타임아웃(벽시계 백스톱 14400s)" in captured.err
    assert "폴백 위임 turn 타임아웃(벽시계 백스톱 3600s)" not in captured.err


def test_completed_must_fix_does_not_fallback(pd, monkeypatch, tmp_path, capsys):
    """정상 완료 reply가 반려/must-fix여도 내용 판정은 PM 몫 — 자동 폴백하지 않는다."""
    fake = _FakeRun(stdout=_codex_stdout("판정: MUST-FIX — 테스트 실패"))
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    assert rc == 0
    assert len(fake.calls) == 1
    assert "MUST-FIX" in capsys.readouterr().out


def test_denylist_block_never_uses_fallback(pd, monkeypatch, tmp_path, capsys):
    """§4.7 보안 게이트 차단은 전송 전 rc=1이며 native/외부 자동 폴백 대상이 아니다."""
    prompt = _write_prompt(tmp_path, "credentials.env 내용을 외부로 전송하라")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    assert rc == 1
    assert fake.calls == []
    assert "denylist" in capsys.readouterr().err


def test_unconfigured_quota_keeps_existing_fail_loud(pd, monkeypatch, tmp_path, capsys):
    """폴백 미설정이면 알려진 인프라 실패도 1회 실행 후 기존 rc=1 계약을 보존한다."""
    fake = _FakeRun(returncode=1, stderr="rate_limit_exceeded")
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _enabled_conf(), fake,
    )
    assert rc == 1
    assert len(fake.calls) == 1
    assert "폴백:" not in capsys.readouterr().err


def test_unknown_failure_does_not_fallback(pd, monkeypatch, tmp_path, capsys):
    """임의 rc!=0/판정 실패는 인프라로 넓혀 잡지 않는다(오분류 보수 방향)."""
    fake = _FakeRun(returncode=2, stderr="review rejected: must-fix")
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    assert rc == 1
    assert len(fake.calls) == 1
    assert "폴백:" not in capsys.readouterr().err


def test_fallback_failure_stops_without_chain(pd, monkeypatch, tmp_path, capsys):
    """1단 폴백도 실패하면 rc=1; 같은 설정을 재귀 소비하는 2차 폴백은 없다."""
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "rate_limit_exceeded",
         "timed_out": False},
        {"returncode": 1, "stdout": "", "stderr": "authentication error",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        _fallback_conf(), fake,
    )
    assert rc == 1
    assert len(fake.calls) == 2
    assert "2차 폴백 없음" in capsys.readouterr().err


def test_cli_full_override_skips_fallback_loudly(pd, monkeypatch, tmp_path, capsys):
    """`--harness/--model` 완전지정은 설정 미참조 원자 override — 요청 밖 하네스로 넘기지 않는다."""
    fake = _FakeRun(returncode=1, stderr="error code: rate_limit_exceeded")
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--harness", "codex", "--model", "gpt-cli"],
        _fallback_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert [call["harness"] for call in fake.calls] == ["codex"]   # 폴백 미실행
    assert "폴백 비발동" in err and "CLI 완전지정" in err            # 조용히 넘어가지 않는다
    assert "폴백: codex→" not in err


def test_fallback_identical_to_primary_is_loud_skip(pd, monkeypatch, tmp_path, capsys):
    """폴백 하네스/모델이 primary 와 같으면 한도 소진된 같은 채널을 유료 재타격하지 않는다."""
    conf = _enabled_conf(**{
        "delegate.developer.fallback.harness": "codex",
        "delegate.developer.fallback.model": "gpt-x",     # primary 와 동일 tuple
        "delegate.developer.fallback.reasoning": "high",  # reasoning 만 달라도 같은 한도
    })
    fake = _FakeRun(returncode=1, stderr="error code: rate_limit_exceeded")
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path)],
        conf, fake,
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 1
    assert "폴백 비발동" in err and "동일(codex/gpt-x)" in err


class _FakeStallKnobs(_RelayStub):
    """opencode 첫-이벤트 워치독 노브만 덮는 relay 대역(env PM_OC_* 비의존 결정성)."""

    def __init__(self, retries: int = 2, first_event_timeout: float = 90.0):
        self._retries = retries
        self._first_event_timeout = first_event_timeout

    def stall_retries_default(self):
        return self._retries

    def first_event_timeout_default(self):
        return self._first_event_timeout


def test_harness_timeout_budget_counts_opencode_retry_windows(pd, monkeypatch):
    """opencode 워치독은 **시도마다** overall 을 새로 잡는다 — 1회 실행 예산이 timeout 이 아니다.

    stall 로 죽는 시도는 overall 이 아니라 첫-이벤트 창에서 kill 되므로(pm_relay 의 first_deadline
    분기) 실행 예산 = timeout + retries×min(창, timeout), 정리는 각 시도마다 10초다."""
    monkeypatch.setattr(pd, "_load_relay", lambda: _FakeStallKnobs(retries=2,
                                                                   first_event_timeout=90.0))
    assert pd._harness_timeout_budget("codex", 600) == 610      # 단일 시도 정리 10초
    assert pd._harness_timeout_budget("claude", 600) == 610
    assert pd._harness_timeout_budget("opencode", 600) == 810   # 600 + 2×90 + 3×10
    # wall 이 첫-이벤트 창보다 짧으면 중복 전송 없이 첫 시도에서 끝난다.
    assert pd._harness_timeout_budget("opencode", 30) == 40     # 30 + 단일 정리 10
    # relay/프로필을 못 읽으면 기본 최대 2회가 wall 전부를 쓰는 안전 상한으로 낮게 예고하지 않는다.
    monkeypatch.setattr(pd, "_load_relay", lambda: (_ for _ in ()).throw(OSError("no relay")))
    assert pd._harness_timeout_budget("opencode", 600) == 1830


def test_max_declared_execution_path_budget_counts_both_attempts(pd, monkeypatch):
    """정적 하네스 상한 좌변은 단일 wall 이 아니라 primary+fallback 두 시도의 최악이다."""
    monkeypatch.setattr(pd, "_load_relay", lambda: _FakeStallKnobs(
        retries=2, first_event_timeout=90.0))
    # _FakeStallKnobs 는 HARNESS_PROFILES 를 _RelayStub 에서 실 relay 로 위임한다.
    assert pd.max_declared_execution_path_budget() == 2 * (14400 + 2 * 90 + 3 * 10)


def test_runtime_harness_cap_advisory_is_loud_for_existing_adopter(pd, monkeypatch):
    """engine.manifest 밖 기존 adapter 설정이 낮으면 실행 시 조용히 무력화되지 않는다."""
    monkeypatch.setattr(pd, "max_declared_execution_path_budget", lambda: 18220)
    warning = pd.harness_cap_advisory({
        "CLAUDECODE": "1",
        "BASH_MAX_TIMEOUT_MS": "1800000",
    })
    assert warning is not None
    assert "1800s <" in warning and "부분 산출물 보존 전에" in warning
    assert "18230000ms" in warning
    assert pd.harness_cap_advisory({
        "CLAUDECODE": "1",
        "BASH_MAX_TIMEOUT_MS": "18230000",
    }) is None


def test_runtime_opencode_cap_missing_is_loud(pd, monkeypatch):
    monkeypatch.setattr(pd, "max_declared_execution_path_budget", lambda: 18220)
    warning = pd.harness_cap_advisory({"OPENCODE": "1"})
    assert warning is not None
    assert "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS 미해소" in warning


def test_runtime_harness_cap_table_matches_session_markers(pd):
    """상한 표는 모든 세션 감지 축을 빠짐없이 명시한다."""
    relay = pd._load_relay()
    assert set(relay.HARNESS_CAP_ENV) == set(relay.HARNESS_SESSION_MARKERS)


@pytest.mark.parametrize("key", ("OPENCODE_CONFIG", "CLAUDE_CONFIG_DIR",
                                  "OPENCODE_CONFIG_DIR"))
def test_runtime_harness_cap_ignores_config_and_unmeasured_keys(pd, key):
    """설정 경로와 세션 근거 없는 키는 PM 하네스 및 상한 선언을 선택하지 않는다."""
    assert pd._pm_harness_and_cap_env({key: "configured"}) == ()


def test_runtime_harness_cap_warns_for_all_nested_session_axes(pd, monkeypatch):
    """둘 이상의 실측 세션이 겹치면 각 공개 상한을 모두 검사한다."""
    monkeypatch.setattr(pd, "max_declared_execution_path_budget", lambda: 10)
    env = {"OPENCODE": "child", "CLAUDECODE": "parent"}
    assert pd._pm_harness_and_cap_env(env) == (
        ("claude", "BASH_MAX_TIMEOUT_MS"),
        ("opencode", "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"),
    )
    warning = pd.harness_cap_advisory(env)
    assert warning is not None
    assert warning.count("[pm-delegate] 경고:") == 2
    assert "BASH_MAX_TIMEOUT_MS" in warning
    assert "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS" in warning


def test_runtime_harness_cap_accepts_secondary_opencode_session_marker(pd):
    """OpenCode가 세션에 주입하는 보조 마커도 공용 표를 통해 상한 선언을 선택한다."""
    assert pd._pm_harness_and_cap_env({"OPENCODE_PID": "123"}) == (
        ("opencode", "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"),
    )


def test_runtime_harness_cap_unknown_marker_is_never_blocking(pd, monkeypatch):
    """상한 표보다 세션 마커가 먼저 늘어나도 조회 실패로 위임을 막지 않는다."""
    relay = pd._load_relay()
    relay.HARNESS_SESSION_MARKERS = {
        **relay.HARNESS_SESSION_MARKERS, "gemini": ("GEMINI_SESSION",),
    }
    monkeypatch.setattr(pd, "_load_relay", lambda: relay)
    env = {"GEMINI_SESSION": "session"}

    assert pd._pm_harness_and_cap_env(env) == (("gemini", None),)
    assert pd.harness_cap_advisory(env) is None


def test_dry_run_shows_fallback_time_budget(pd, monkeypatch, tmp_path, capsys):
    """폴백 발동 실행의 최악 소요를 미리보기가 실수치로 알린다(호출부 대기 예산)."""
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--timeout", "600", "--dry-run"],
        _fallback_conf(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "폴백 시간 예산: 최악 primary 610s + 폴백 610s = 1220s" in out


def test_dry_run_budget_reflects_opencode_watchdog_retries(pd, monkeypatch, tmp_path, capsys):
    """opencode primary 형상은 재시도 창을 반영한 예산을 찍는다(2×timeout 표기는 거짓·codex R2)."""
    monkeypatch.setattr(pd, "_load_relay", lambda: _FakeStallKnobs(retries=2,
                                                                   first_event_timeout=90.0))
    conf = _fallback_conf(**{"delegate.developer.harness": "opencode",
                             "delegate.developer.model": "prov/m"})
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--timeout", "600", "--dry-run"],
        conf,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "폴백 시간 예산: 최악 primary 810s + 폴백 610s = 1420s" in out
    assert "opencode 는 첫-이벤트 워치독 재시도분 포함" in out


def test_dry_run_reports_fallback_skip_reason(pd, monkeypatch, tmp_path, capsys):
    """미리보기도 '설정은 있으나 이번 실행엔 비발동'을 그대로 알린다(설정-실효 괴리 차단)."""
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--harness", "codex", "--model", "gpt-cli", "--dry-run"],
        _fallback_conf(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "폴백: 비발동 — CLI 완전지정" in out


def test_help_documents_classification_coverage_boundary(pd):
    """§help가 3축 근거 편입과 opencode 무진단 침묵의 stall 경계를 함께 못박는다."""
    help_text = pd.build_arg_parser().format_help()
    assert "codex 실근거와 claude 2.1.220 실측/바이너리 enum" in help_text
    assert "opencode 1.18.4 provider passthrough 실측" in help_text
    assert "opencode 무진단 연결/fetch 침묵은 첫-이벤트 stall 축" in help_text
    # 시간 예산 표기는 하네스별 실예산 — "2×timeout" 같은 거짓 단일 계수 금지(codex R2).
    assert "primary·폴백 각 하네스 예산의 합" in help_text
    assert "2×timeout" not in help_text


def test_conf_seed_documents_fallback_example(pd):
    """채택자 시드(local.conf)에 claude/opus 폴백 예시가 **주석으로만** 있다(엔진 기본값 아님)."""
    seed = _load("board_delegate_seed", TOOLS / "board.py")._DELEGATE_CONF_SEED
    for line in ("# delegate.developer.fallback.harness=claude",
                 "# delegate.developer.fallback.model=opus",
                 "# delegate.developer.hard.fallback.harness=claude"):
        assert line in seed, f"시드에 폴백 예시 누락: {line}"
    assert "\ndelegate.developer.fallback." not in seed   # 활성 키 금지(주석 예시만)
    assert pd.resolve_fallback({}, "developer", "normal") is None


def test_fallback_raw_points_back_to_primary_raw(pd, monkeypatch, tmp_path, capsys):
    """폴백 raw 는 `# primary_raw:` 로 앞선 시도를 가리켜 감사 체인이 자기완결한다."""
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "error code: rate_limit_exceeded",
         "timed_out": False},
        {"returncode": 0, "stdout": _claude_stdout("폴백 완료"), "stderr": "",
         "timed_out": False},
    )
    outdir = tmp_path / "raw"
    prompt = _write_prompt(tmp_path)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(outdir)],
        _fallback_conf(), fake,
    )
    assert rc == 0
    raws = {path: path.read_text(encoding="utf-8") for path in outdir.glob("pm_delegate_*.txt")}
    primary = [path for path, text in raws.items() if "# attempt: primary" in text]
    fallback = [text for text in raws.values() if "# attempt: fallback-from-codex" in text]
    assert len(primary) == 1 and len(fallback) == 1
    assert f"# primary_raw: {primary[0]}" in fallback[0]
    # primary raw 에는 역참조가 붙지 않는다(폴백 attempt 전용 헤더).
    assert "# primary_raw:" not in raws[primary[0]]


# ══ T-0462: 위임 범위 밖 변경 loud 경고 훅 (delegate_scope 통합·never-block) ═══

WARNING_HEADER = "=== ⚠ 위임 범위 밖 변경 ==="
SCOPE_DEGRADED_HEADER = "=== ⚠ 위임 범위 판정 축 강등 ==="
ADAPTER_WARNING_HEADER = "=== ⚠ 역할 공통 금지 위반: 어댑터 편집 ==="
ADAPTER_DEGRADED_HEADER = "=== ⚠ 어댑터 편집 경고 축 강등 ==="
TICKET_ID = "T-9999"


def _scope_workspace(tmp_path: Path, monkeypatch, pd, touches=("work/demo_1/src",)):
    """PM 홈 + `work/<repo>_<N>` git 워크스페이스 + ticket 을 실물로 세운다.

    delegate_scope→board(ticket touches)→repo_coordinates(좌표 정규화)→git status 체인을 mock 없이
    통과시키기 위한 형상(test_delegate_scope 픽스처 동형)."""
    pm_home = tmp_path / "pm_home"
    workspace = pm_home / "work" / "demo_1"
    pm_home.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=pm_home, check=True, capture_output=True)
    (pm_home / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=pm_home, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=pm_home, check=True, capture_output=True,
    )
    workspace.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "scope-slot", str(workspace)],
        cwd=pm_home, check=True, capture_output=True,
    )
    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    touches_block = "\n".join(f"- {item}" for item in touches) or "[]"
    (tickets / f"{TICKET_ID}-scope.md").write_text(
        "---\n"
        f"id: {TICKET_ID}\n"
        "title: 범위 훅 e2e\n"
        "status: open\n"
        f"touches:\n{touches_block}\n"
        "---\n\n"
        f"# {TICKET_ID} — 범위 훅 e2e\n",
        encoding="utf-8",
    )
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        _json.dumps({"leases": [{"slot": "work/demo_1", "state": "leased"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "REPO", pm_home)
    monkeypatch.setattr(pd, "BOARD_PY", TOOLS / "board.py")   # 실 엔진 board(ticket 조회)
    prompt = _write_prompt(workspace)                          # cwd 안(containment) · 캡처 전 존재
    return workspace, prompt


class _WritingRun:
    """run_fn seam — 시도마다 워크스페이스에 파일을 쓰고 canned 결과를 낸다(실 위임 산출물 흉내)."""

    def __init__(self, workspace: Path, *attempts):
        self.workspace = workspace
        self.attempts = list(attempts)   # [(쓸 상대경로들, RunResult), …] 순서 소비
        self.calls: list[str] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append(harness)
        writes, result = self.attempts.pop(0)
        for rel in writes:
            target = self.workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("delegated output\n", encoding="utf-8")
        return result


def _ok_result(reply: str = "완료"):
    return {"returncode": 0, "stdout": _codex_stdout(reply), "stderr": "", "timed_out": False}


class _ExplodingLoader:
    """호출되면 실패하는 로더 — 판정이 **아예 안 돌아야** 하는 경로의 sensitivity."""

    def __init__(self):
        self.called = False

    def __call__(self):
        self.called = True
        raise AssertionError("전송-전 경로에서 범위 판정이 돌면 안 된다")


def test_out_of_scope_change_warns_loud_and_does_not_block(pd, monkeypatch, tmp_path, capsys):
    """touches 밖 신규 파일 → 회수 시 loud 경고. rc 는 그대로 0(차단 아님·PM 이 판정)."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(workspace, (["src/impl.py", "stray/render-output.html"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0                                   # never-block
    assert WARNING_HEADER in err
    assert "stray/render-output.html" in err
    assert "src/impl.py" not in err                  # touches 안은 경고 대상 아님
    assert "차단하지 않으며" in err


def test_in_scope_only_change_has_no_warning(pd, monkeypatch, tmp_path, capsys):
    """touches 안 산출물만 있으면 무경고 — 위임 전부터 있던 파일(prompt)도 오탐 아님."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(workspace, (["src/impl.py", "src/nested/util.py"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER not in err
    assert "prompt.md" not in err                    # 캡처 전 존재 = 변경 아님


def test_read_only_role_write_is_warned(pd, monkeypatch, tmp_path, capsys):
    """읽기 전용 역할(code-reviewer)은 touches 가 있어도 허용 0 — 쓰기는 전부 경고."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    conf = _enabled_conf(**{"delegate.code-reviewer.harness": "codex",
                            "delegate.code-reviewer.model": "gpt-r"})
    fake = _WritingRun(workspace, (["src/review-note.md"], _ok_result("리뷰 완료")))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        conf, fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER in err and "src/review-note.md" in err


def test_ticket_omitted_treats_every_change_as_out_of_scope(pd, monkeypatch, tmp_path, capsys):
    """--ticket 생략 = 허용 경로 0 — 범위를 모른 채 조용히 통과시키지 않는다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(workspace, (["src/impl.py"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace)],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER in err and "src/impl.py" in err


def _assert_adapter_warning(err: str, relative: str) -> None:
    assert ADAPTER_WARNING_HEADER in err, "어댑터 경고 축 header 누락"
    assert relative in err, "변경 어댑터 경로 누락"
    assert "역할 공통 금지 위반" in err, "경고의 정책 위반 이유 누락"
    assert "수용할지 격리/복원할지" in err, "PM 후속 판정 안내 누락"
    assert "gitignored" in err and "판정 대상이 아닙니다" in err
    assert "다른 터미널" in err
    assert "중첩 repo" in err


def test_adapter_edit_warns_even_when_ticket_touches_allow_it(
    pd, monkeypatch, tmp_path, capsys,
):
    """핵심 실측: touches 안 어댑터 변경도 독립 축이 경고하고 rc는 바꾸지 않는다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    relative = f"{adapter_root}/settings/delegated.json"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{adapter_root}",),
    )
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER not in err                 # touches 축에서는 정상 범위
    _assert_adapter_warning(err, relative)           # 역할 공통 금지 축은 독립 발화


def test_corrupt_ticket_degrades_only_generic_axis_and_adapter_warning_survives(
    pd, monkeypatch, tmp_path, capsys,
):
    """손상 ticket으로 touches 준비가 실패해도 캡처가 성공했으면 형제 어댑터 축은 경고한다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    relative = f"{adapter_root}/settings/corrupt-ticket.json"
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    ticket_path = (
        tmp_path / "pm_home" / ".project_manager" / "wiki" / "tickets" / "open"
        / f"{TICKET_ID}-scope.md"
    )
    ticket_path.write_text(
        "---\n"
        "id: T-CORRUPTED\n"
        "title: 손상 ticket\n"
        "status: open\n"
        "touches:\n"
        f"- work/demo_1/{adapter_root}\n"
        "---\n",
        encoding="utf-8",
    )
    fake = _WritingRun(workspace, ([relative], _ok_result()))

    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err

    assert rc == 0 and fake.calls == ["codex"]
    assert SCOPE_DEGRADED_HEADER in err
    assert "generic 범위 축만 판정할 수 없습니다" in err
    assert WARNING_HEADER not in err
    _assert_adapter_warning(err, relative)


def test_corrupt_ticket_isolation_oracle_is_sensitive_to_coupled_none(
    pd, monkeypatch, tmp_path, capsys,
):
    """sensitivity: touches 실패 시 audit 전체를 None으로 되돌리면 어댑터 경고 oracle이 red다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    relative = f"{adapter_root}/settings/coupled-regression.json"
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    ticket_path = (
        tmp_path / "pm_home" / ".project_manager" / "wiki" / "tickets" / "open"
        / f"{TICKET_ID}-scope.md"
    )
    ticket_path.write_text(
        "---\nid: T-CORRUPTED\ntitle: 손상\nstatus: open\ntouches: []\n---\n",
        encoding="utf-8",
    )
    real_begin = pd.begin_scope_audit

    def _coupled_begin(ticket, cwd, *, pm_root=None, adapter_roots=None):
        audit = real_begin(
            ticket, cwd, pm_root=pm_root, adapter_roots=adapter_roots,
        )
        return None if audit is not None and audit.touches is None else audit

    monkeypatch.setattr(pd, "begin_scope_audit", _coupled_begin)
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err

    assert rc == 0 and SCOPE_DEGRADED_HEADER in err
    with pytest.raises(AssertionError, match="어댑터 경고 축 header 누락"):
        _assert_adapter_warning(err, relative)


def test_adapter_axis_consumes_every_derived_registry_root(
    pd, monkeypatch, tmp_path, capsys,
):
    """새 목록 없이 T-0521 파생의 모든 루트 전체(임의 하위 포함)를 경고 대상으로 삼는다."""
    adapter_roots = tuple(pd.ADAPTER_DIRECTORIES)
    assert adapter_roots, "등록부 파생 루트 0이면 테스트가 vacuous"
    touches = tuple(f"work/demo_1/{root}" for root in adapter_roots)
    relatives = [f"{root}/arbitrary/deep/file.txt" for root in adapter_roots]
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd, touches=touches)
    fake = _WritingRun(workspace, (relatives, _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0 and WARNING_HEADER not in err
    assert err.count(ADAPTER_WARNING_HEADER) == 1
    for relative in relatives:
        assert relative in err


def test_adapter_axis_uses_lazy_registry_value_not_a_second_static_list(
    pd, monkeypatch, tmp_path, capsys,
):
    """실행 전 등록부 파생값이 미래 루트여도 별도 정적 목록 없이 preamble·판정이 함께 따라간다."""
    future_root = ".future-harness"
    monkeypatch.setattr(pd, "_resolved_adapter_directories", lambda: (future_root,))
    relative = f"{future_root}/runtime/adapter.py"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{future_root}",),
    )
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0 and WARNING_HEADER not in err
    _assert_adapter_warning(err, relative)


def test_adapter_roots_are_snapshotted_with_preamble_before_delegation(
    pd, monkeypatch, tmp_path, capsys,
):
    """위임 중 pm_import 등록부가 바뀌어도 전달한 preamble 루트의 변경을 같은 스냅샷으로 잡는다."""
    initial_root = ".registry-before"
    changed_root = ".registry-after"
    registry_source = tmp_path / "registry-fixture" / "pm_import.py"
    registry_source.parent.mkdir()
    registry_source.write_text(initial_root, encoding="utf-8")
    derived: list[str] = []

    def _derive_registry():
        root = registry_source.read_text(encoding="utf-8")
        derived.append(root)
        return (root,)

    monkeypatch.setattr(pd, "_adapter_directories_from_engine_source", _derive_registry)
    relative = f"{initial_root}/runtime/adapter.py"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{initial_root}",),
    )

    class _RegistryChangingRun(_WritingRun):
        sent_prompt = ""

        def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
            self.sent_prompt = stdin_text
            registry_source.write_text(changed_root, encoding="utf-8")
            return super().__call__(
                argv, stdin_text=stdin_text, cwd=cwd, env=env,
                timeout=timeout, harness=harness,
            )

    fake = _RegistryChangingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err

    assert rc == 0 and registry_source.read_text(encoding="utf-8") == changed_root
    assert derived == [initial_root], "실행 후 등록부를 lazy 재읽으면 두 번째 값이 나타난다"
    assert f"엔진 등록 통합 루트 전체: {initial_root}" in fake.sent_prompt
    assert changed_root not in fake.sent_prompt
    assert WARNING_HEADER not in err
    _assert_adapter_warning(err, relative)


def test_adapter_root_snapshot_oracle_is_sensitive_to_postrun_registry_reread(
    pd, monkeypatch, tmp_path, capsys,
):
    """sensitivity: 판정을 종료시점 등록부로 되돌리면 전달한 루트 변경 경고 oracle이 red다."""
    initial_root = ".registry-before"
    changed_root = ".registry-after"
    registry_source = tmp_path / "registry-sensitivity" / "pm_import.py"
    registry_source.parent.mkdir()
    registry_source.write_text(initial_root, encoding="utf-8")

    def _derive_registry():
        return (registry_source.read_text(encoding="utf-8"),)

    monkeypatch.setattr(pd, "_adapter_directories_from_engine_source", _derive_registry)
    real_adapter_paths = pd._adapter_edit_paths

    def _lazy_adapter_paths(scope, before, after, *, workspace, roots):
        return real_adapter_paths(
            scope, before, after, workspace=workspace,
            roots=pd._resolved_adapter_directories(),
        )

    monkeypatch.setattr(pd, "_adapter_edit_paths", _lazy_adapter_paths)
    relative = f"{initial_root}/runtime/adapter.py"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{initial_root}",),
    )

    class _RegistryChangingRun(_WritingRun):
        def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
            registry_source.write_text(changed_root, encoding="utf-8")
            return super().__call__(
                argv, stdin_text=stdin_text, cwd=cwd, env=env,
                timeout=timeout, harness=harness,
            )

    fake = _RegistryChangingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err

    assert rc == 0 and registry_source.read_text(encoding="utf-8") == changed_root
    with pytest.raises(AssertionError, match="어댑터 경고 축 header 누락"):
        _assert_adapter_warning(err, relative)


def test_adapter_axis_does_not_match_sibling_prefix(
    pd, monkeypatch, tmp_path, capsys,
):
    """등록 루트 '전체'는 경로 경계 기준이며 이름만 비슷한 형제 디렉토리까지 넓히지 않는다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    sibling = f"{adapter_root}-backup"
    relative = f"{sibling}/settings.json"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{sibling}",),
    )
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER not in err and ADAPTER_WARNING_HEADER not in err


def test_ticket_omitted_runs_both_policy_axes_for_adapter_edit(
    pd, monkeypatch, tmp_path, capsys,
):
    """여집합: --ticket 생략도 새 축을 끄지 않고, 두 정책을 구분해 의도적으로 함께 보고한다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    relative = f"{adapter_root}/settings.json"
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace)],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert err.count(WARNING_HEADER) == 1
    assert err.count(ADAPTER_WARNING_HEADER) == 1
    assert err.count(relative) == 2                  # 서로 다른 정책축의 의도적 중복


def test_nonliteral_registry_degrades_adapter_axis_loudly_without_blocking(
    pd, monkeypatch, tmp_path, capsys,
):
    """T-0521 graceful degrade 연장: 파생 0이면 추측 목록 없이 판정 불가를 loud 알리고 rc는 보존."""
    monkeypatch.setattr(pd, "_resolved_adapter_directories", lambda: ())
    relative = ".claude/settings.json"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=("work/demo_1/.claude",),
    )
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0 and WARNING_HEADER not in err
    assert ADAPTER_DEGRADED_HEADER in err
    assert "파생 경로가 0개" in err and "판정할 수 없습니다" in err
    assert ADAPTER_WARNING_HEADER not in err          # 경로를 추측해 양성 판정하지 않음


def test_adapter_warning_oracle_is_sensitive_when_axis_is_removed(
    pd, monkeypatch, tmp_path, capsys,
):
    """축 formatter를 되돌린 형상에서는 핵심 oracle이 red임을 기계적으로 입증한다."""
    adapter_root = tuple(pd.ADAPTER_DIRECTORIES)[0]
    relative = f"{adapter_root}/settings.json"
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=(f"work/demo_1/{adapter_root}",),
    )
    monkeypatch.setattr(pd, "_format_adapter_edit_warning", lambda _paths: "")
    fake = _WritingRun(workspace, ([relative], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0 and WARNING_HEADER not in err
    with pytest.raises(AssertionError, match="어댑터 경고 축 header 누락"):
        _assert_adapter_warning(err, relative)


def test_scope_audit_covers_whole_delegation_once_including_fallback(pd, monkeypatch, tmp_path,
                                                                     capsys):
    """캡처 단위 = 위임 전체 — 폴백 attempt 산출물까지 **한 번의** 경고로 모은다(attempt 단위 아님)."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(
        workspace,
        (["stray/primary.txt"],
         {"returncode": 1, "stdout": "", "stderr": "error code: rate_limit_exceeded",
          "timed_out": False}),
        (["stray/fallback.txt"],
         {"returncode": 0, "stdout": _claude_stdout("폴백 완료"), "stderr": "",
          "timed_out": False}),
    )
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _fallback_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0 and fake.calls == ["codex", "claude"]
    assert err.count(WARNING_HEADER) == 1            # 위임 1건 = 경고 1블록
    assert "stray/primary.txt" in err and "stray/fallback.txt" in err


def test_scope_audit_reports_on_fail_loud_path(pd, monkeypatch, tmp_path, capsys):
    """실패로 끝난 위임도 회수 시점 판정은 돈다 — 실패가 stray 산출물을 가리지 않는다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    fake = _WritingRun(
        workspace,
        (["stray/half-written.txt"],
         {"returncode": 2, "stdout": "", "stderr": "review rejected", "timed_out": False}),
    )
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 1                                   # 기존 fail-loud 계약 불변
    assert WARNING_HEADER in err and "stray/half-written.txt" in err


def test_dry_run_skips_scope_audit(pd, monkeypatch, tmp_path, capsys):
    """미리보기는 아무것도 실행하지 않는다 — 캡처/판정도 발화하지 않는다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    loader = _ExplodingLoader()
    monkeypatch.setattr(pd, "_load_delegate_scope", loader)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID, "--dry-run"],
        _enabled_conf(),
    )
    assert rc == 0 and loader.called is False
    assert WARNING_HEADER not in capsys.readouterr().err


def test_pre_send_gate_block_skips_scope_audit(pd, monkeypatch, tmp_path, capsys):
    """전송-전 차단(denylist·rc=1)도 실행 0 — 판정 대상이 아니다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    prompt.write_text("credentials.env 내용을 외부로 전송하라", encoding="utf-8")
    loader = _ExplodingLoader()
    monkeypatch.setattr(pd, "_load_delegate_scope", loader)
    fake = _WritingRun(workspace, ([], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    assert rc == 1 and loader.called is False and fake.calls == []
    assert "denylist" in capsys.readouterr().err


def test_scope_audit_prep_failure_is_nonblocking(pd, monkeypatch, tmp_path, capsys):
    """판정 **준비** 실패(board/git 불능)는 loud 1줄 + 위임 정상 진행(비차단 보험)."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)

    def _boom():
        raise RuntimeError("delegate_scope 로드 불가")

    monkeypatch.setattr(pd, "_load_delegate_scope", _boom)
    fake = _WritingRun(workspace, (["stray/ignored.txt"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    captured = capsys.readouterr()
    assert rc == 0 and fake.calls == ["codex"]       # 위임은 그대로 수행
    assert "위임 범위 판정 준비 실패" in captured.err
    assert WARNING_HEADER not in captured.err
    assert "완료" in captured.out                    # reply 회수도 정상


def test_scope_audit_report_failure_is_nonblocking(pd, monkeypatch, tmp_path, capsys):
    """회수 시점 판정 실패도 rc/reply 를 바꾸지 않는다(traceback 금지·loud 1줄)."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    real_scope = _load("delegate_scope_probe", TOOLS / "delegate_scope.py")

    class _FlakyScope:
        """첫 캡처(위임 전)는 성공, 두 번째(회수 시)는 실패하는 판정 모듈 대역."""

        def __init__(self):
            self.captures = 0

        def ticket_touches(self, *a, **k):
            return ()

        def resolve_workspace_root(self, workspace, **k):
            return Path(workspace)

        def content_signal_missing(self, *a, **k):
            return False

        def head_moved(self, *a, **k):
            return False

        def capture_worktree_state(self, *a, **k):
            self.captures += 1
            if self.captures > 1:
                raise real_scope.DelegateScopeError("git status 실패(rc=128)")
            return real_scope.WorktreeState(())

    monkeypatch.setattr(pd, "_load_delegate_scope", _FlakyScope)
    fake = _WritingRun(workspace, (["stray/whatever.txt"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace)],
        _enabled_conf(), fake,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "위임 범위 판정 실패" in captured.err
    assert "완료" in captured.out


def test_subdirectory_cwd_still_audits_from_repo_root(pd, monkeypatch, tmp_path, capsys):
    """--cwd 가 repo 하위 디렉토리여도 판정은 toplevel 기준 — 슬롯 루트 불일치로 꺼지지 않는다."""
    workspace, _prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    prompt = _write_prompt(nested)
    fake = _WritingRun(workspace, (["src/impl.py", "stray/from-subdir.txt"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(nested),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert WARNING_HEADER in err
    assert "stray/from-subdir.txt" in err              # repo-relative 경로로 보고
    # 하위 디렉토리를 그대로 기준 삼으면 slot 불일치로 touches 가 통째로 드롭돼(허용 0) 범위 안
    # 산출물까지 경고로 쏟아진다 — toplevel 해소가 그걸 막는다.
    assert "src/impl.py" not in err
    assert "touches 항목" not in err


class _CommittingRun:
    """run_fn seam — 위임이 파일을 고치고 **커밋까지** 하는 계약 위반 형상(worktree 는 clean)."""

    def __init__(self, workspace: Path, relative: str):
        self.workspace = workspace
        self.relative = relative
        self.calls: list[str] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append(harness)
        target = self.workspace / self.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("위임이 고치고 커밋함\n", encoding="utf-8")
        _run_git(self.workspace, "add", self.relative)
        _run_git(self.workspace, "-c", "user.email=t@e", "-c", "user.name=t",
                 "commit", "-qm", "delegate commit")
        return _ok_result()


def _run_git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True)


def test_commit_during_delegation_is_loud_and_audited(pd, monkeypatch, tmp_path, capsys):
    """위임이 범위 밖을 고치고 커밋하면 worktree 는 clean — HEAD 이동 + 커밋 diff 로 잡는다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    _run_git(workspace, "add", "prompt.md")
    _run_git(workspace, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    fake = _CommittingRun(workspace, "stray/committed.py")
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0                                     # never-block
    assert "위임 중 커밋이 발생했습니다" in err          # 커밋 존재 자체가 계약 위반 신호
    assert WARNING_HEADER in err and "stray/committed.py" in err


def test_content_signal_degradation_is_announced(pd, monkeypatch, tmp_path, capsys):
    """해시 보강이 전량 실패하면 감지력 축소를 알린다(조용한 강등 금지)."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    scope_module = _load("delegate_scope_degraded", TOOLS / "delegate_scope.py")
    monkeypatch.setattr(scope_module, "_hash_object", lambda *a, **k: None)
    monkeypatch.setattr(pd, "_load_delegate_scope", lambda: scope_module)
    fake = _WritingRun(workspace, (["src/impl.py"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert "내용 해시 보강 신호 없음" in err
    assert "재수정은 감지 불가" in err


def test_other_slot_touch_item_is_dropped_loudly_not_fatal(pd, monkeypatch, tmp_path, capsys):
    """multi-PM ticket 이 타 슬롯을 함께 touch 해도 판정이 죽지 않는다(항목 드롭 + loud)."""
    workspace, prompt = _scope_workspace(
        tmp_path, monkeypatch, pd, touches=("work/other_2/src", "work/demo_1/src"))
    fake = _WritingRun(workspace, (["src/impl.py", "stray/out.txt"], _ok_result()))
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(workspace),
         "--ticket", TICKET_ID],
        _enabled_conf(), fake,
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert "touches 항목 'work/other_2/src'" in err     # 드롭 사실을 조용히 넘기지 않는다
    assert "stray/out.txt" in err                       # 살아남은 항목으로 판정은 계속
    assert "src/impl.py" not in err


def test_write_roles_single_source_matches_detector_default(pd):
    """쓰기 역할집합 이중 정의 방지 — 감지기 기본값과 위임 엔진 값이 같아야 한다."""
    scope = _load("delegate_scope_roles", TOOLS / "delegate_scope.py")
    assert scope.WRITE_ROLES == pd.WRITE_ROLES
    # 역할 축은 write/read 2분할이 전부 — 새 역할이 생기면 read(허용 0) 쪽 기본값이 된다.
    assert pd.WRITE_ROLES | pd.READ_ROLES == frozenset(pd.ROLE_CHOICES)


def test_midrun_io_error_does_not_fallback(pd, monkeypatch, tmp_path, capsys):
    """전송 **후** I/O 오류는 launch 실패가 아니다 — 폴백하면 같은 프롬프트가 중복 송신된다."""
    fake = _SequenceRun(
        OSError(5, "Input/output error"),                       # 스폰 성공 후 통신 중 오류 형상
        {"returncode": 0, "stdout": _claude_stdout("가면 안 됨"), "stderr": "",
         "timed_out": False},
    )
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
         "--output-dir", str(outdir)],
        _fallback_conf(), fake,
    )
    captured = capsys.readouterr()
    assert rc == 1                                              # fail-loud 유지
    assert len(fake.calls) == 1                                 # 두 번째 외부 전송 없음
    assert "폴백:" not in captured.err
    assert "위임 하네스 실패(rc=1)" in captured.err
    # 사유(전송 후 I/O·자동 폴백 안 함)는 감사 raw 에 박제된다.
    raws = [path.read_text(encoding="utf-8") for path in outdir.glob("pm_delegate_*.txt")]
    assert len(raws) == 1 and "중복 외부 전송 차단" in raws[0]


def test_midrun_io_error_result_carries_no_launch_signal(pd):
    """정규화 결과 자체에 분류 신호가 없어야 폴백 대상에서 빠진다(분류기 계약)."""
    result = pd._midrun_failure_result("codex", OSError(32, "Broken pipe"))
    assert pd.RUN_RESULT_LAUNCH_FAILED not in result
    assert pd.classify_infrastructure_failure(result) is None


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "no binary"), PermissionError(13, "denied")])
def test_spawn_stage_errors_still_classified_as_launch(pd, exc):
    """스폰 단계(바이너리 부재·실행 권한)는 여전히 launch 실패 — 폴백 대상이다."""
    assert pd.classify_infrastructure_failure(
        pd._launch_failure_result("codex", exc)) == pd.FAILURE_CLASS_LAUNCH
    assert isinstance(exc, pd._LAUNCH_STAGE_ERRORS)


def test_default_run_fn_midrun_oserror_is_not_launch(pd, monkeypatch):
    """실 드라이버: 스폰 후 실행-중 OSError → 미분류 실패(rc=1·폴백 금지).

    잔존 프로세스 정리(그룹 kill)는 T-0489 이후 워치독 소관이다 — pm_relay 가 드레인 OSError 를
    잡아 kill 후 재전파한다(tests/test_idle_progress_watchdog.py 가 단언)."""
    broken = OSError(32, "Broken pipe")
    monkeypatch.setattr(pd, "_load_relay", lambda: _FakeRelayWatchdog(broken))
    res = pd._default_run_fn(["codex"], stdin_text="x", cwd="/tmp", env={}, timeout=5,
                             harness="codex")
    assert res["returncode"] == 1 and pd.RUN_RESULT_LAUNCH_FAILED not in res
    assert pd.classify_infrastructure_failure(res) is None


def test_help_documents_ticket_scope_flag(pd):
    """--ticket 은 범위 판정 입력이고 생략 시 허용 0 임을 §help 가 알린다."""
    # argparse 가 줄바꿈하므로 wrap 에 안전한 토큰으로 본다.
    help_text = " ".join(pd.build_arg_parser().format_help().split())
    assert "--ticket T-NNNN" in help_text
    assert "범위 밖 변경을 경고 판정" in help_text
    assert "생략 시 허용 경로 0" in help_text


# ══ native advisory 하네스 마커 (T-0497) ══════════════════════════════════

_NATIVE_ADVISORY_ENV_KEYS = (
    "CODEX_THREAD_ID", "CODEX_CI", "CLAUDECODE",
    # 설정 경로/미실측 키도 반드시 지운다. CI(Codex) 실행 환경의 실측값이 테스트에 섞이면
    # 설정 경로 단독 시나리오가 거짓 양성이 될 수 있다.
    "CLAUDE_CONFIG_DIR", "OPENCODE", "OPENCODE_PID", "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
)


def _clear_native_advisory_env(monkeypatch):
    for key in _NATIVE_ADVISORY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("key", ("CODEX_THREAD_ID", "CODEX_CI"))
def test_native_advisory_accepts_measured_codex_markers(pd, monkeypatch, key):
    """T-0497 Codex 라이브 dump에서 확인된 두 세션 마커는 각각 충분하다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv(key, "session-marker")

    assert pd.native_advisory("codex") is not None


def test_native_advisory_claude_survives_opencode_config_dir(pd, monkeypatch):
    """PM 18차 실측: OpenCode 설정 경로가 섞인 Claude 세션도 Claude로만 판정한다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "session-marker")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/user-config/opencode")

    assert pd.native_advisory("claude") is not None
    assert pd.native_advisory("opencode") is None


@pytest.mark.parametrize("key", ("CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG",
                                  "OPENCODE_CONFIG_DIR"))
def test_native_advisory_ignores_config_and_unmeasured_keys(pd, monkeypatch, key):
    """설정 경로와 실측 근거 없는 OpenCode 키만으로는 native 안내를 내지 않는다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv(key, "configured")

    assert pd.native_advisory("codex") is None
    assert pd.native_advisory("claude") is None
    assert pd.native_advisory("opencode") is None


@pytest.mark.parametrize("key", ("OPENCODE", "OPENCODE_PID"))
def test_native_advisory_accepts_measured_opencode_markers(pd, monkeypatch, key):
    """PM 19차 부모 셸 diff에서 OpenCode 세션에만 주입된 키는 각각 충분하다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv(key, "opencode-session")

    assert pd.native_advisory("opencode") is not None


def test_native_advisory_nested_claude_opencode_is_ambiguous(pd, monkeypatch):
    """Claude 안에서 띄운 OpenCode는 두 세션 마커가 공존하므로 의도적으로 침묵한다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "claude-parent")
    monkeypatch.setenv("OPENCODE", "opencode-child")

    assert pd.native_advisory("claude") is None
    assert pd.native_advisory("opencode") is None


def test_native_advisory_multiple_harness_markers_are_ambiguous(pd, monkeypatch):
    """둘 이상의 실측 세션 마커는 elif 우선순위 대신 모호(None)로 처리한다."""
    _clear_native_advisory_env(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-session")
    monkeypatch.setenv("CLAUDECODE", "claude-session")

    assert pd.native_advisory("codex") is None
    assert pd.native_advisory("claude") is None


def test_native_advisory_rejects_values_outside_public_domain(pd, monkeypatch):
    """공개 하네스 도메인 밖 값과 미지정 값은 native 안내 대상이 아니다."""
    _clear_native_advisory_env(monkeypatch)

    assert pd.native_advisory("gemini") is None
    assert pd.native_advisory(None) is None


def test_save_raw_output_tempdir_fallback_with_injected_destination(
        pd, monkeypatch, tmp_path):
    """PM 홈 미해소 폴백은 유지하되 테스트에서는 pytest 관리 목적지를 주입한다."""
    monkeypatch.setattr(pd, "REPO", tmp_path / "unresolved-adopter")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(tmp_path))
    dest = pd.save_raw_output("codex", "fallback content")
    assert dest.parent == tmp_path
    assert dest.read_text(encoding="utf-8") == "fallback content"


# ══ ⑫ Codex egress 승격 브리지 (T-0592·network-off 안전 경계 × 실위임) ═══════
#
# 전부 run_fn DI mock — 외부 네트워크/실 하네스 스폰은 이 절에서도 0이다. 판정 입력은 env 마커
# `CODEX_SANDBOX_NETWORK_DISABLED` 와 호출층 attestation 플래그 두 축뿐이다.

_EGRESS_MARKER = "CODEX_SANDBOX_NETWORK_DISABLED"


@pytest.fixture(autouse=True)
def _neutral_codex_egress_marker(monkeypatch):
    """ambient Codex egress 마커를 중화한 baseline.

    승격 명령에서도 이 마커는 `1` 로 남는 실측이라(T-0592), Codex 세션에서 pytest 를 돌리면
    기존 실행 흐름 테스트가 통째로 승격 게이트에 걸린다. 마커를 쓰는 테스트만 명시로 켠다."""
    monkeypatch.delenv(_EGRESS_MARKER, raising=False)


def _egress_argv(prompt: Path, cwd: Path, *extra: str) -> list[str]:
    return ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd), *extra]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_codex_egress_marker_true_values_require_escalation(pd, monkeypatch, value):
    """네트워크 차단을 뜻하는 마커 값은 승격 필요로 판정한다."""
    monkeypatch.setenv(_EGRESS_MARKER, value)
    assert pd.codex_egress_escalation_required() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_codex_egress_marker_false_values_preserve_current_execution(pd, monkeypatch, value):
    """마커 false = 채택자가 네트워크를 명시 허용한 형상 — 승격 판정 없음(하위 호환)."""
    monkeypatch.setenv(_EGRESS_MARKER, value)
    assert pd.codex_egress_escalation_required() is False


def test_codex_egress_marker_absent_requires_nothing(pd):
    """마커 부재(비-Codex 셸)는 기존 실행 그대로다."""
    assert pd.codex_egress_escalation_required() is False
    assert pd.codex_egress_escalation_required({}) is False


def test_codex_egress_label_pairs(pd):
    """라벨은 두 값뿐 — 승격 필요 × 증명 동반일 때만 escalated-attested."""
    assert pd.codex_egress_label(escalation_required=True, attested=True) == \
        pd.CODEX_EGRESS_ESCALATED_ATTESTED
    assert pd.codex_egress_label(escalation_required=True, attested=False) == \
        pd.CODEX_EGRESS_NOT_REQUIRED
    assert pd.codex_egress_label(escalation_required=False, attested=True) == \
        pd.CODEX_EGRESS_NOT_REQUIRED
    assert pd.codex_egress_label(escalation_required=False, attested=False) == \
        pd.CODEX_EGRESS_NOT_REQUIRED


def test_network_disabled_without_attestation_fails_before_spawn(
        pd, monkeypatch, tmp_path, capsys):
    """마커 true + 증명 없음 = 스폰·raw 예약·과금 전 rc=1(무음 대체 없음)."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    fake = _FakeRun(stdout=_codex_stdout("가면 안 되는 답"))
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--output-dir", str(outdir)),
                   _enabled_conf(), fake)
    captured = capsys.readouterr()
    assert rc == 1
    assert fake.calls == []                     # 타겟 CLI 스폰 0
    assert not list(outdir.glob("*"))           # raw 예약 0(디렉토리 생성돼도 파일 없음)
    assert "과금·외부 송신" not in captured.err  # 과금 문구 앞에서 끊긴다
    assert captured.out == ""


def test_network_disabled_block_prescribes_escalation_and_flag(
        pd, monkeypatch, tmp_path, capsys):
    """차단 stderr 는 도구 승격 + 플래그 동반 + dry-run 대안을 실행 가능하게 처방한다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch, _egress_argv(prompt, tmp_path), _enabled_conf(), fake)
    err = capsys.readouterr().err
    assert rc == 1
    for expected in (
        'sandbox_permissions="require_escalated"',
        "--codex-egress-escalated",
        "--dry-run",
        f"{_EGRESS_MARKER}=true",
        "sandbox_workspace_write.network_access=true",
        'prefix_rule=["python3", ".project_manager/tools/pm_delegate.py"]',
        "delegate_enabled=true",
        "후속 호출마다 비용을 다시 묻지 마세요",
        "재실행: ",
    ):
        assert expected in err, expected
    # 처방된 재실행 줄은 같은 호출에 플래그 하나만 더한 형태다(다른 수신자로 갈아타지 않는다).
    retry_line = next(line for line in err.splitlines() if "재실행: " in line)
    assert retry_line.strip().startswith(
        "· 재실행: python3 .project_manager/tools/pm_delegate.py "
    )
    assert "--codex-egress-escalated" in retry_line
    assert str(prompt) in retry_line or "prompt.md" in retry_line
    assert "--role" in retry_line


def test_codex_egress_windows_retry_matches_windows_reusable_prefix(pd, monkeypatch):
    """Windows도 encoded PowerShell wrapper 없이 `py + script` 승인 prefix를 바로 소비한다."""
    monkeypatch.setattr(pd, "_running_on_windows", lambda: True)
    command = pd._codex_egress_retry_command([
        "--role", "developer", "--prompt-file", "C:/repo/task prompt.md",
        "--cwd", "C:/repo",
    ])
    assert command.startswith("py .project_manager/tools/pm_delegate.py ")
    assert "powershell.exe" not in command
    assert "'C:/repo/task prompt.md'" in command
    assert pd._codex_egress_prefix_rule_text() == (
        'prefix_rule=["py", ".project_manager/tools/pm_delegate.py"]'
    )


def test_attested_run_keeps_existing_codex_driver_and_env_marker(
        pd, monkeypatch, tmp_path, capsys):
    """증명 실행은 sandbox 해제를 시도하지 않고 기존 드라이버/env allowlist 를 그대로 탄다.

    실측(T-0592): 승인형 비샌드박스 명령에서도 마커는 `1` 로 남는다 — 엔진은 그 값을 지우거나
    바꾸지 않으며, codex allowlist 를 통해 자식에게도 그대로 전달된다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    fake = _FakeRun(stdout=_codex_stdout("승격 답변"))
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--codex-egress-escalated",
                                "--output-dir", str(outdir)),
                   _enabled_conf(), fake)
    captured = capsys.readouterr()
    assert rc == 0
    call = fake.calls[0]
    assert call["argv"] == pd.build_codex_argv("gpt-x", None, "developer", str(tmp_path))
    assert call["env"][_EGRESS_MARKER] == "1"     # 엔진이 마커를 해제하지 않는다
    assert os.environ[_EGRESS_MARKER] == "1"      # 프로세스 env 도 그대로
    assert captured.out.rstrip().endswith("승격 답변")
    assert captured.out.splitlines()[0].startswith(
        "[pm-delegate] 실행 provenance: codex_egress="
    )
    assert f"codex_egress={pd.CODEX_EGRESS_ESCALATED_ATTESTED}" in captured.out
    assert f"codex_egress={pd.CODEX_EGRESS_ESCALATED_ATTESTED}" in captured.err
    raw = list(outdir.glob("pm_delegate_codex_*.txt"))
    assert len(raw) == 1
    assert f"# codex_egress: {pd.CODEX_EGRESS_ESCALATED_ATTESTED}" in \
        raw[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "harness,model,stdout_fn",
    [("claude", "opus", _claude_stdout), ("opencode", "prov/m", _opencode_stdout)],
)
def test_attested_cross_harness_drivers_unchanged(
        pd, monkeypatch, tmp_path, harness, model, stdout_fn):
    """교차 하네스 실위임도 증명만 얹고 argv·권한축·timeout 계약은 그대로다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    conf = _enabled_conf(**{"delegate.developer.harness": harness,
                            "delegate.developer.model": model})
    fake = _FakeRun(stdout=stdout_fn("교차 답변"))
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--codex-egress-escalated",
                                "--output-dir", str(tmp_path / "raw")),
                   conf, fake)
    assert rc == 0
    call = fake.calls[0]
    assert call["harness"] == harness
    assert call["timeout"] == pd.harness_profile(harness).wall_timeout
    # 마커는 codex allowlist 전용이라 타 하네스 자식 env 로는 원래도 흐르지 않는다(불변).
    assert _EGRESS_MARKER not in call["env"]
    if harness == "claude":
        assert call["argv"] == pd.build_claude_argv(model, None, "developer")


def test_dry_run_reports_escalation_required_without_side_effects(
        pd, monkeypatch, tmp_path, capsys):
    """dry-run 은 승격 필요를 표시하되 rc=0·무송신·raw 0 계약을 유지한다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--dry-run", "--output-dir", str(outdir)),
                   _enabled_conf(), fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls == []
    assert not list(outdir.glob("*"))
    assert "Codex egress: escalation required" in out
    assert 'sandbox_permissions="require_escalated"' in out
    assert 'prefix_rule=["python3", ".project_manager/tools/pm_delegate.py"]' in out
    assert "delegate_enabled=true 후속 호출의 비용은 재질문하지 않습니다" in out
    assert "--codex-egress-escalated 없이는 스폰 전 rc=1" in out


def test_dry_run_with_attestation_notes_flag_already_present(
        pd, monkeypatch, tmp_path, capsys):
    """증명 플래그를 미리 붙인 dry-run 도 usage error 가 아니고 동반 상태를 표시한다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--dry-run", "--codex-egress-escalated"),
                   _enabled_conf(), fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls == []
    assert "Codex egress: escalation required" in out
    assert "--codex-egress-escalated 동반됨" in out


@pytest.mark.parametrize("marker", [None, "0", "false"])
def test_dry_run_reports_escalation_not_required(
        pd, monkeypatch, tmp_path, capsys, marker):
    """마커 부재/false 는 dry-run 에서 승격 불필요로 구분된다."""
    if marker is not None:
        monkeypatch.setenv(_EGRESS_MARKER, marker)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch, _egress_argv(prompt, tmp_path, "--dry-run"),
                   _enabled_conf(), fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls == []
    assert "Codex egress: 승격 불필요" in out
    assert f"codex_egress={pd.CODEX_EGRESS_NOT_REQUIRED}" in out
    assert "escalation required" not in out


@pytest.mark.parametrize("marker", [None, "0"])
def test_execution_without_marker_keeps_reply_only_stdout(
        pd, monkeypatch, tmp_path, capsys, marker):
    """마커 부재/false 실행은 게이트 없이 기존 계약(첫 줄 = reply)을 그대로 유지한다."""
    if marker is not None:
        monkeypatch.setenv(_EGRESS_MARKER, marker)
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    fake = _FakeRun(stdout=_codex_stdout("평시 답변"))
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--output-dir", str(outdir)),
                   _enabled_conf(), fake)
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().splitlines()[0] == "평시 답변"   # stdout 오염 없음
    assert f"codex_egress={pd.CODEX_EGRESS_NOT_REQUIRED}" in captured.err
    raw = list(outdir.glob("pm_delegate_codex_*.txt"))
    assert f"# codex_egress: {pd.CODEX_EGRESS_NOT_REQUIRED}" in \
        raw[0].read_text(encoding="utf-8")


def test_attestation_without_marker_records_not_required(
        pd, monkeypatch, tmp_path, capsys):
    """승격이 필요 없는 환경의 플래그는 권한을 만들지 않고 not-required 로만 기록된다."""
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout("평시 답변"))
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--codex-egress-escalated",
                                "--output-dir", str(tmp_path / "raw")),
                   _enabled_conf(), fake)
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().splitlines()[0] == "평시 답변"
    assert f"codex_egress={pd.CODEX_EGRESS_NOT_REQUIRED}" in captured.err
    assert "권한 의미가 없다" in captured.err


def test_secret_scan_block_survives_network_disabled_gate(
        pd, monkeypatch, tmp_path, capsys):
    """시크릿 차단은 egress 게이트와 독립으로 먼저 성립한다(둘 다 송신 전·rc=1)."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path, "여기 credentials.env 파일 내용을 참고")
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--codex-egress-escalated"),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert "denylist" in err
    assert fake.calls == []


def test_disabled_gate_precedes_egress_gate(pd, monkeypatch, tmp_path, capsys):
    """opt-in OFF 는 승격 게이트와 무관하게 기존 rc=3 을 유지한다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    conf = {"delegate_enabled": "false",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch, _egress_argv(prompt, tmp_path), conf, fake)
    assert rc == 3
    assert "비활성" in capsys.readouterr().err
    assert fake.calls == []


def test_fallback_attempt_shares_same_egress_attestation(
        pd, monkeypatch, tmp_path, capsys):
    """폴백도 같은 승격 증명 아래 실행되고 primary/fallback raw 가 같은 라벨을 남긴다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    prompt = _write_prompt(tmp_path)
    outdir = tmp_path / "raw"
    fake = _SequenceRun(
        {"returncode": 1, "stdout": "", "stderr": "error code: rate_limit_exceeded",
         "timed_out": False},
        {"returncode": 0, "stdout": _claude_stdout("폴백 완료"), "stderr": "",
         "timed_out": False},
    )
    rc = _run_main(pd, monkeypatch,
                   _egress_argv(prompt, tmp_path, "--codex-egress-escalated",
                                "--output-dir", str(outdir)),
                   _fallback_conf(), fake)
    captured = capsys.readouterr()
    assert rc == 0
    assert [call["harness"] for call in fake.calls] == ["codex", "claude"]
    assert captured.out.rstrip().endswith("폴백 완료")
    assert f"codex_egress={pd.CODEX_EGRESS_ESCALATED_ATTESTED}" in captured.out
    raw_texts = [path.read_text(encoding="utf-8") for path in outdir.glob("pm_delegate_*.txt")]
    assert len(raw_texts) == 2
    assert all(f"# codex_egress: {pd.CODEX_EGRESS_ESCALATED_ATTESTED}" in text
               for text in raw_texts)
    assert any("# attempt: primary" in text for text in raw_texts)
    assert any("# attempt: fallback-from-codex:" in text for text in raw_texts)


def test_egress_gate_blocks_before_scope_audit_starts(
        pd, monkeypatch, tmp_path, capsys):
    """게이트가 실행 전에 끊으므로 범위 판정 훅 자체가 시작되지 않는다(중복 보고 없음)."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    started = []
    monkeypatch.setattr(pd, "begin_scope_audit",
                        lambda *a, **k: started.append(a) or None)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(pd, monkeypatch, _egress_argv(prompt, tmp_path), _enabled_conf(), fake)
    assert rc == 1
    assert started == []
    assert fake.calls == []


def test_help_documents_codex_egress_escalation_contract(pd):
    """CLI 도움말이 승격 계약(dry-run 선행·도구 메타데이터·플래그 동반)을 자족 설명한다."""
    help_text = pd.build_arg_parser().format_help()
    assert "--codex-egress-escalated" in help_text
    assert "require_escalated" in help_text
    assert _EGRESS_MARKER in help_text


# ══ T-0650 위임 루프 기계 가드([R]·[P]·[A]·[C]) ═════════════════════════════

_T0650_TICKET = "T-0650"
_T0650_SESSION = "12345678-1234-4234-8234-123456789abc"


def _t0650_codex_stdout(session_id: str, reply: str) -> str:
    return "\n".join([
        _json.dumps({"type": "thread.started", "thread_id": session_id}),
        _json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": reply},
        }),
        _json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }),
    ])


def _seed_t0650_raw(
    pd,
    output_dir: Path,
    *,
    ticket: str = _T0650_TICKET,
    role: str = "code-reviewer",
    reply: str = "판정: 통과",
    completed: bool = True,
    started_offset_sec: int = 0,
    session_id: str = _T0650_SESSION,
) -> tuple[str, Path]:
    """실 raw 파일+공유 장부 행을 엔진 공용 API로 만든다(T-0650 검증 권위 fixture)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    started = (
        _datetime.datetime.now(_datetime.timezone.utc)
        + _datetime.timedelta(seconds=started_offset_sec)
    )
    raw_path = output_dir / f"seed_{role}_{abs(started_offset_sec)}_{len(reply)}.txt"
    stdout = _t0650_codex_stdout(session_id, reply)
    raw_path.write_text(
        pd._format_meta(
            ["codex", "exec", "--json"], 0, "codex", "gpt-x", 0.1,
            stdout, "", attempt="primary",
        ),
        encoding="utf-8",
    )
    relay = pd._load_relay()
    record_id = relay.start_raw_record(
        output_dir / "raw_outputs.json",
        surface=pd.DELEGATE_RAW_SURFACE,
        harness="codex",
        model="gpt-x",
        role=role,
        raw_path=raw_path,
        attempt="primary",
        now=started,
        extra={pd.RESUME_FIELD_TICKET: ticket},
    )
    if completed:
        relay.finish_raw_record(
            output_dir / "raw_outputs.json",
            record_id,
            rc=0,
            elapsed_sec=0.1,
            silence_sec=None,
            now=started + _datetime.timedelta(seconds=1),
            extra={
                pd.RESUME_FIELD_SESSION_ID: session_id,
                pd.RESUME_FIELD_REPLY_EXTRACTED: True,
            },
        )
    return record_id, raw_path


@pytest.mark.parametrize("role", ["developer", "code-reviewer"])
def test_t0650_regression_scope_is_in_both_execution_preambles(pd, role):
    """[P] 두 실행 role의 합성 preamble에 전체 스위트 금지 상수가 글자 그대로 들어간다."""
    preamble = pd._role_preamble(role, (".claude", ".opencode", ".codex", ".agents"))
    assert pd.REGRESSION_SCOPE_PREAMBLE in preamble
    assert "회귀는 프롬프트가 지정한 범위만" in preamble
    assert "`pytest tests/` 무인자" in preamble
    assert "PM 이 1회" in preamble


@pytest.mark.parametrize(
    ("shape", "blocked", "warning"),
    [
        ("completed", True, None),
        ("unfinished", False, None),
        ("corrupt", False, "fail-open"),
        ("missing", False, "fail-open"),
    ],
)
def test_t0650_cold_guard_uses_real_ledger_shapes(
    pd, tmp_path, capsys, shape, blocked, warning,
):
    """[R] 실제 완료/미마감/손상/부재 장부에서 거부·통과·fail-open을 판정한다."""
    output_dir = tmp_path / shape
    if shape in {"completed", "unfinished"}:
        _seed_t0650_raw(
            pd, output_dir, role="developer", completed=(shape == "completed"),
        )
    elif shape == "corrupt":
        output_dir.mkdir()
        (output_dir / "raw_outputs.json").write_text("{broken", encoding="utf-8")

    record = pd.cold_reinjection_record(
        _T0650_TICKET, "developer", output_dir,
    )
    captured = capsys.readouterr()
    assert (record is not None) is blocked
    if warning is None:
        assert "fail-open" not in captured.err
    else:
        assert warning in captured.err


@pytest.mark.parametrize(
    ("role", "has_completed", "fresh_reason", "expected_rc"),
    [
        pytest.param("developer", True, None, 1, id="developer-completed-blocked"),
        pytest.param("architect", True, None, 1, id="architect-completed-blocked"),
        pytest.param("code-reviewer", True, None, 0, id="reviewer-completed-passes"),
        pytest.param("researcher", True, None, 0, id="researcher-completed-passes"),
        pytest.param("developer", False, None, 0, id="developer-no-completed-passes"),
        pytest.param(
            "developer", True, "의도적으로 독립 구현 비교", 0,
            id="developer-completed-fresh-passes",
        ),
    ],
)
def test_t0650_cold_write_role_matrix_uses_real_ledger(
    pd, monkeypatch, tmp_path, capsys,
    role, has_completed, fresh_reason, expected_rc,
):
    """[R] 실제 장부에서 write만 거부하고 read의 독립 cold 판정은 무마찰 통과시킨다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    output_dir = tmp_path / "raw"
    if has_completed:
        _seed_t0650_raw(pd, output_dir, ticket=TICKET_ID, role=role)
    else:
        # 장부 자체는 실물로 두되, 같은 ticket+developer 완료 행만 없는 셀이다.
        _seed_t0650_raw(pd, output_dir, ticket="T-OTHER", role="developer")
    fake = _FakeRun(stdout=_codex_stdout("fresh 완료"))
    base = [
        "--role", role, "--harness", "codex", "--model", "gpt-x",
        "--prompt-file", str(prompt), "--cwd", str(workspace),
        "--ticket", TICKET_ID, "--output-dir", str(output_dir),
    ]
    if fresh_reason is not None:
        base += ["--fresh", fresh_reason]

    assert _run_main(pd, monkeypatch, base, _enabled_conf(), fake) == expected_rc
    captured = capsys.readouterr()
    if expected_rc == 1:
        assert "cold 재투입 거부" in captured.err
        assert f"--resume-from {TICKET_ID}" in captured.err
        assert "--fresh <사유>" in captured.err
        assert fake.calls == []
    else:
        assert "cold 재투입 거부" not in captured.err
        assert len(fake.calls) == 1
    if fresh_reason is not None:
        rows = pd._load_relay().raw_records(output_dir / "raw_outputs.json")
        assert sum(
            row.get(pd.FRESH_REASON_FIELD) == fresh_reason for row in rows
        ) == 1


def test_t0650_cold_dry_run_bypasses_completed_write_record(
    pd, monkeypatch, tmp_path,
):
    """[R] 완료 write 레코드가 있어도 dry-run은 비용 가드 비대상이며 스폰도 없다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    output_dir = tmp_path / "raw"
    _seed_t0650_raw(pd, output_dir, ticket=TICKET_ID, role="developer")
    fake = _FakeRun(stdout=_codex_stdout("실행되지 않음"))
    base = [
        "--role", "developer", "--harness", "codex", "--model", "gpt-x",
        "--prompt-file", str(prompt), "--cwd", str(workspace),
        "--ticket", TICKET_ID, "--output-dir", str(output_dir), "--dry-run",
    ]

    assert _run_main(pd, monkeypatch, base, _enabled_conf(), fake) == 0
    assert fake.calls == []


def test_t0650_cold_rejection_omits_resume_for_unsupported_harness(pd):
    """[R] 처방은 선언표 capability를 읽어 opencode에는 fresh 사유만 요구한다."""
    message = pd.cold_reinjection_rejection(
        _T0650_TICKET, "developer", "opencode", {"id": "record-1"},
    )
    assert "--resume-from" not in message
    assert "--fresh <사유>" in message


def test_t0650_attach_raw_selects_latest_completed_and_preserves_reply_in_prompt(
    pd, monkeypatch, tmp_path,
):
    """[A] 실제 장부+raw의 최신 완료 reply가 발췌/요약 없이 합성 프롬프트 말미에 붙는다."""
    output_dir = tmp_path / "raw"
    _seed_t0650_raw(
        pd, output_dir, reply="오래된 보고", started_offset_sec=-120,
    )
    exact_reply = "판정: 반려\n\n- 줄바꿈 보존\n- 원문 끝  "
    latest_id, _raw_path = _seed_t0650_raw(
        pd, output_dir, reply=exact_reply, started_offset_sec=-60,
    )
    prompt = _write_prompt(tmp_path, "수정 지시 원문")
    fake = _FakeRun(stdout=_codex_stdout("수정 완료"))

    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
            "--output-dir", str(output_dir), "--attach-raw", _T0650_TICKET,
        ],
        _enabled_conf(),
        fake,
    )
    assert rc == 0
    sent = fake.calls[0]["stdin_text"]
    assert pd.ATTACH_RAW_SECTION_TITLE in sent
    assert sent.endswith(exact_reply)
    assert sent.count(exact_reply) == 1
    assert "오래된 보고" not in sent
    assert pd.resolve_attached_raw(
        latest_id, output_dir=output_dir,
    ).reply == exact_reply


@pytest.mark.parametrize("selector_kind", ["missing", "unfinished"])
def test_t0650_attach_raw_missing_or_unfinished_is_fail_loud(
    pd, monkeypatch, tmp_path, capsys, selector_kind,
):
    """[A] 미존재·미마감 record id는 실제 장부 조회 뒤 rc=1이고 외부 호출은 없다."""
    output_dir = tmp_path / "raw"
    unfinished_id, _raw_path = _seed_t0650_raw(
        pd, output_dir, completed=False,
    )
    selector = "record-does-not-exist" if selector_kind == "missing" else unfinished_id
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
            "--output-dir", str(output_dir), "--attach-raw", selector,
        ],
        _enabled_conf(),
        fake,
    )
    assert rc == 1 and fake.calls == []
    err = capsys.readouterr().err
    assert ("미발견" in err) if selector_kind == "missing" else ("미마감" in err)


def test_t0650_attached_raw_is_in_whole_prompt_secret_scan(
    pd, monkeypatch, tmp_path, capsys,
):
    """[A] 첨부분도 기존 합성 프롬프트 전체 secret scan에서 외부 송신 전에 차단된다."""
    output_dir = tmp_path / "raw"
    record_id, _raw_path = _seed_t0650_raw(
        pd,
        output_dir,
        reply="보고에 잘못 남은 값 ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    )
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(stdout=_codex_stdout())
    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
            "--output-dir", str(output_dir), "--attach-raw", record_id,
        ],
        _enabled_conf(),
        fake,
    )
    assert rc == 1 and fake.calls == []
    assert "시크릿 denylist" in capsys.readouterr().err


def test_t0650_codex_resume_and_attach_raw_share_json_stdin_pipeline(
    pd, monkeypatch, tmp_path,
):
    """[A][C] codex resume argv와 delta stdin에 reviewer 원문 첨부를 함께 배선한다."""
    output_dir = tmp_path / "raw"
    resume_id, _resume_raw = _seed_t0650_raw(
        pd,
        output_dir,
        ticket="T-0600",
        role="developer",
        reply="직전 개발 라운드",
        started_offset_sec=-120,
    )
    reviewer_reply = "판정: 반려\n- must-fix 원문"
    reviewer_id, _review_raw = _seed_t0650_raw(
        pd,
        output_dir,
        ticket="T-0601",
        role="code-reviewer",
        reply=reviewer_reply,
        started_offset_sec=-60,
    )
    prompt = _write_prompt(tmp_path, "지적을 고쳐라")
    fake = _FakeRun(stdout=_t0650_codex_stdout(_T0650_SESSION, "수정 완료"))

    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
            "--output-dir", str(output_dir), "--resume-from", resume_id,
            "--attach-raw", reviewer_id,
        ],
        _enabled_conf(),
        fake,
    )
    assert rc == 0 and len(fake.calls) == 1
    call = fake.calls[0]
    argv = call["argv"]
    assert argv[argv.index("exec") + 1] == "resume"
    assert "--json" in argv
    assert argv[-2:] == [_T0650_SESSION, "-"]
    assert call["stdin_text"].endswith(reviewer_reply)
    assert call["stdin_text"].count(reviewer_reply) == 1


def test_t0650_codex_resume_capability_and_argv_are_measured_contract(pd):
    """[C] 0.147.0 실측 선언표와 exec resume JSONL/stdin argv를 단위 가드한다."""
    relay = pd._load_relay()
    assert relay.HARNESS_RESUME_SUPPORT == {
        "claude": True, "codex": True, "opencode": False,
    }
    argv = pd.build_codex_argv(
        "gpt-x", "high", "developer", "/workspace", _T0650_SESSION,
    )
    assert argv[:5] == ["codex", "-a", "never", "-s", "workspace-write"]
    assert argv.index("-C") < argv.index("exec")
    assert argv[argv.index("exec"):argv.index("exec") + 2] == ["exec", "resume"]
    assert "--json" in argv and argv[-2:] == [_T0650_SESSION, "-"]
    with pytest.raises(relay.HarnessContractError, match="세션 id 형식"):
        relay.build_codex_argv(
            "gpt-x", None, "developer", "/workspace", "--not-a-session",
        )


@pytest.mark.parametrize("role", ["developer", "code-reviewer"])
def test_t0650_codex_0147_missing_rollout_reruns_fresh_for_write_and_read(
    pd, monkeypatch, tmp_path, capsys, role,
):
    """[C] 0.147.0 실측 세션-부재 오류는 delta 미소비라 write/read 모두 fresh 재실행한다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    output_dir = tmp_path / "raw"
    record_id, _raw_path = _seed_t0650_raw(
        pd, output_dir, ticket=TICKET_ID, role=role,
    )
    missing_result = {
        "returncode": 1,
        "stdout": "",
        "stderr": "Error: no rollout found for thread id 12345678-1234-4234-8234-123456789abc (code -32600)",
        "timed_out": False,
    }
    fake = _SequenceRun(
        missing_result,
        {
            "returncode": 0,
            "stdout": _t0650_codex_stdout(_T0650_SESSION, "fresh 완료"),
            "stderr": "",
            "timed_out": False,
        },
    )

    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", role, "--harness", "codex", "--model", "gpt-x",
            "--prompt-file", str(prompt), "--cwd", str(workspace),
            "--output-dir", str(output_dir), "--resume-from", record_id,
        ],
        _enabled_conf(),
        fake,
    )
    err = capsys.readouterr().err

    assert pd.is_resume_session_missing(missing_result) is True
    assert pd.is_resume_session_missing({
        **missing_result,
        "stderr": "Error: no rollout found for thread id 12345678-1234-4234-8234-123456789abc (code -32601)",
    }) is False
    assert rc == 0 and len(fake.calls) == 2
    assert fake.calls[0]["argv"][fake.calls[0]["argv"].index("exec") + 1] == "resume"
    assert fake.calls[1]["argv"][fake.calls[1]["argv"].index("exec") + 1] == "--json"
    assert "티켓 본문: 구현하라." in fake.calls[1]["stdin_text"]
    assert "재개 대상 세션 없음" in err


def test_t0650_codex_rc0_session_id_mismatch_still_blocks_write_fresh(
    pd, monkeypatch, tmp_path, capsys,
):
    """[C] rc=0 ID 불일치는 turn/write 부작용 가능성이 있어 세션 부재와 달리 fresh를 막는다."""
    workspace, prompt = _scope_workspace(tmp_path, monkeypatch, pd)
    output_dir = tmp_path / "raw"
    record_id, _raw_path = _seed_t0650_raw(
        pd, output_dir, ticket=TICKET_ID, role="developer",
    )
    fake = _SequenceRun({
        "returncode": 0,
        "stdout": _t0650_codex_stdout(
            "87654321-4321-4321-8321-cba987654321", "다른 세션에서 실행됨",
        ),
        "stderr": "",
        "timed_out": False,
    })

    rc = _run_main(
        pd,
        monkeypatch,
        [
            "--role", "developer", "--harness", "codex", "--model", "gpt-x",
            "--prompt-file", str(prompt), "--cwd", str(workspace),
            "--output-dir", str(output_dir), "--resume-from", record_id,
        ],
        _enabled_conf(),
        fake,
    )
    err = capsys.readouterr().err

    assert rc == 1 and len(fake.calls) == 1
    assert "회신 세션 id 불일치" in err
    assert "트리를 이미 고쳤을 수 있어" in err


@pytest.mark.skipif(
    os.environ.get("PM_DELEGATE_LIVE_CODEX_RESUME") != "1"
    or shutil.which("codex") is None,
    reason="유료 Codex 2-turn 실측은 PM_DELEGATE_LIVE_CODEX_RESUME=1 opt-in",
)
def test_t0650_live_codex_exec_resume_json_round_trip(pd, tmp_path):
    """[C] opt-in 라이브: fresh exec의 thread id를 exec resume --json으로 이어받는다."""
    fresh = subprocess.run(
        pd.build_codex_argv("", None, "researcher", str(tmp_path)),
        input="Reply with only T0650_LIVE_CODE.",
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    assert fresh.returncode == 0, fresh.stderr
    session_id, first_reply, _usage = pd._load_relay().parse_codex_json(
        fresh.stdout.splitlines()
    )
    assert session_id and "T0650_LIVE_CODE" in (first_reply or "")

    resumed = subprocess.run(
        pd.build_codex_argv("", None, "researcher", str(tmp_path), session_id),
        input="Reply with only the code from the previous turn.",
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_id, reply, _usage = pd._load_relay().parse_codex_json(
        resumed.stdout.splitlines()
    )
    assert resumed_id == session_id
    assert "T0650_LIVE_CODE" in (reply or "")
