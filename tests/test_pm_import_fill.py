"""pm_import.py fill 단계 단위 테스트 — 자유서술 placeholder 하니스 구동(opt-in·T-0009).

T-0007 의 기계 단계 위에 얹은 fill 단계를 검증한다. 핵심 안전 계약:
  - 실 하니스(claude/opencode) 바이너리는 절대 호출하지 않는다 — 전부 stub runner(토큰 0).
  - opt-in 게이트: PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만 실 runner 경로.
    둘 중 하나라도 없으면 실호출 차단(stub/manual 강제) — 토큰·모델 비용 0.
  - 생성물은 *제안* — 자유서술 placeholder 만 채우고 자동 확정 안 함(비가역 회피).

run_fill 의 runner seam(주입 콜러블)으로 명령 조립을 토큰 0 으로 검증한다. main 통합은
PM_IMPORT_LIVE_HARNESS 환경변수 격리(monkeypatch.delenv/setenv)로 게이트만 검증한다 —
main 의 auto 경로는 게이트 통과 시 *실* runner 를 부르므로 테스트에서는 게이트를 막아둔다.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from _win_skip import _can_symlink, posix_mode_supported

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

FREE_FORM_TOKENS = ("{{PROJECT_CONSTRAINTS}}", "{{PROTECTED_PATHS}}", "{{USER_GATE_ITEMS}}")
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    return _load_pm_import()


@pytest.fixture(autouse=True)
def _hermetic_opencode_models(pm_import, monkeypatch):
    """T-0033: main(--harness opencode) 가 실제 `opencode models` CLI 를 호출하지 않도록
    `_real_models_runner` 를 (False, []) 로 고정 — opencode 설치 환경서도 fill 테스트가 hermetic
    (미설치 동치 = 모델 토큰 TODO 폴백, fill 단계는 모델 토큰과 무관).
    """
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))


# ── stub runner (실 바이너리 미호출 — 토큰 0) ───────────────────────────────

class _StubRunner:
    """하니스 호출 seam stub — argv 를 기록하고 고정 (성공, 출력) 을 반환한다(토큰 0).

    실 claude/opencode 바이너리를 절대 부르지 않는다 — run_fill 이 조립한 argv·프롬프트만
    포착해 명령 조립을 검증한다. ok=False·json 출력 등 시나리오를 생성자로 지정.
    """

    def __init__(self, ok: bool = True, output: str = "제안된 제약 텍스트"):
        self.ok = ok
        self.output = output
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: list[str], prompt: str) -> tuple[bool, str]:
        self.calls.append((list(argv), prompt))
        return self.ok, self.output


def _forbidden_popen(*args, **kwargs):
    raise AssertionError("테스트가 실 하니스 바이너리를 spawn하려 함")


def _make_imported_tree(pm_import, tmp_path, harness="claude", name="Fillee"):
    """fill 대상이 될 실제 import 트리를 만든다(자유서술 placeholder 보존 상태).

    main 의 manual(기본) 경로로 import 하되, fill TODO 표시 전 상태를 보려면 직접 run_fill 을
    부르는 테스트는 별도 트리를 쓴다. 여기서는 자유서술 토큰이 트리에 남아있는지 먼저 확인한다.
    """
    dest = tmp_path / name.lower()
    # manual fill(기본)로 import — 이후 테스트가 트리 상태를 검사한다.
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", name])
    assert rc == 0
    return dest


# ── 심볼/계약 노출 ──────────────────────────────────────────────────────────

def test_exposes_fill_symbols(pm_import):
    assert callable(pm_import.run_fill)
    assert pm_import.FILL_CHOICES == ("auto", "manual")
    assert pm_import.FILL_HARNESS_CHOICES == pm_import.REGISTERED_HARNESSES
    # codex fill runner 매핑 (ADR-0070 D5·silent claude 폴백 소멸).
    assert pm_import.CODEX_FILL_CMD == ("codex", "exec")
    assert pm_import.FREE_FORM_TOKENS == FREE_FORM_TOKENS
    assert pm_import.OPENCODE_MODEL_TOKEN == OPENCODE_MODEL_TOKEN
    assert pm_import.LIVE_HARNESS_ENV == "PM_IMPORT_LIVE_HARNESS"
    assert set(pm_import.FILL_CAPABLE_HARNESSES) <= set(pm_import.REGISTERED_HARNESSES)
    # FillResult 형태.
    fr = pm_import.FillResult(mode="auto")
    assert fr.mode == "auto"
    assert fr.values == {} and fr.drafts == {} and fr.todos == []


def test_fill_capability_and_runner_mapping_are_registry_derived(pm_import, tmp_path):
    """선언된 모든 fill 가능 하네스는 argv runner 매핑을 가져야 한다."""
    for harness in pm_import.FILL_CAPABLE_HARNESSES:
        argv = pm_import._build_runner_argv(harness, "PROMPT", tmp_path)
        assert argv


def test_fill_failure_formatter_preserves_head_for_non_utf8_output(pm_import):
    """kill 경로의 비-UTF8 bytes도 원 진단을 가리지 않고 replacement로 표시한다."""
    exc = subprocess.TimeoutExpired(["runner"], 1, output=b"ok\xff", stderr=b"err\xfe")
    rendered = pm_import._fill_failure_with_partial("[원 진단]", exc)
    assert rendered.startswith("[원 진단]")
    assert "ok�" in rendered and "err�" in rendered


@pytest.mark.parametrize("output, expected", [
    ("", "[원 진단]"),
    ("short", "short"),
    ("x" * 200, "x" * 200),
    ("x" * 10000, "x" * 10000),
])
def test_shared_partial_formatter_keeps_all_text_without_truncation(pm_import, output, expected):
    """없음·짧음·기존 preview 경계·대용량 모두 공용 seam에서 전량 보존한다."""
    exc = subprocess.TimeoutExpired(["runner"], 1, output=output)
    rendered = pm_import._fill_failure_with_partial("[원 진단]", exc)
    assert expected in rendered
    assert rendered.startswith("[원 진단]")


def test_shared_partial_formatter_keeps_head_when_output_stringification_fails(pm_import):
    """포맷할 산출물이 고장나도 kill 원인 진단은 남는다."""
    class BrokenOutput:
        def __bool__(self):
            return True

        def __str__(self):
            raise UnicodeError("broken")

    exc = subprocess.TimeoutExpired(["runner"], 1)
    exc.output = BrokenOutput()
    assert pm_import._fill_failure_with_partial("[원 진단]", exc).startswith("[원 진단]")


def test_fill_failure_routes_through_shared_partial_formatter(pm_import, monkeypatch):
    """fill 소비처를 옛 로컬 결합기로 되돌리면 이 seam 감지가 실패한다."""
    class Relay:
        @staticmethod
        def format_partial_output(head, exc):
            return f"shared:{head}:{exc.output}"

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: Relay())
    exc = subprocess.TimeoutExpired(["runner"], 1, output="partial")
    assert pm_import._fill_failure_with_partial("head", exc) == "shared:head:partial"


@pytest.mark.parametrize("failure_site", ("loader", "formatter"))
def test_fill_failure_partial_formatter_keeps_head_for_general_failure(
        pm_import, monkeypatch, failure_site):
    """일반 로더·포맷터 실패는 원 진단을 가리지 않고 fill fail-soft를 유지한다."""
    failure = RuntimeError("general")
    if failure_site == "loader":
        monkeypatch.setattr(
            pm_import, "_load_watchdog",
            lambda: (_ for _ in ()).throw(failure),
        )
    else:
        class BrokenRelay:
            @staticmethod
            def format_partial_output(head, exc):
                raise failure

        monkeypatch.setattr(pm_import, "_load_watchdog", lambda: BrokenRelay())

    assert pm_import._fill_failure_with_partial("head", RuntimeError("original")) == "head"


@pytest.mark.parametrize("failure_site", ("loader", "formatter"))
def test_fill_failure_partial_formatter_reraises_engine_rev_skew(
        pm_import, monkeypatch, failure_site):
    """rev skew는 일반 포맷 실패와 달리 fill 경로에서도 숨기지 않는다."""
    skew = RuntimeError("skew")
    skew._engine_rev_skew = True
    if failure_site == "loader":
        monkeypatch.setattr(
            pm_import, "_load_watchdog",
            lambda: (_ for _ in ()).throw(skew),
        )
    else:
        class BrokenRelay:
            @staticmethod
            def format_partial_output(head, exc):
                raise skew

        monkeypatch.setattr(pm_import, "_load_watchdog", lambda: BrokenRelay())

    with pytest.raises(RuntimeError, match="skew"):
        pm_import._fill_failure_with_partial("head", RuntimeError("original"))


def test_fill_harness_help_lists_and_import_default_are_registry_derived(pm_import, capsys):
    """실제 --help의 registry 목록과 import 기본값/epilog가 ``all`` 계약에 일치한다."""
    with pytest.raises(SystemExit) as exc_info:
        pm_import.main(["--help"])
    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    registered_csv = ",".join(pm_import.HARNESS_TEMPLATE_DIRS)
    assert f"--fill-harness {{{registered_csv}}}" in help_text
    for harness in pm_import.HARNESS_TEMPLATE_DIRS:
        assert f"`{harness}`" in help_text
    assert "--fill-harness {claude,opencode}" not in help_text
    assert "전체: all; default: all" in help_text
    assert "harness=기본 all(등록 어댑터 전체:" in help_text
    assert "default: claude" not in help_text


def test_fill_harness_cap_advisory_uses_shared_relay_judgment(pm_import, monkeypatch, tmp_path):
    """fill은 호출층 상한 부족을 never-block 경고로 표면화한다."""
    monkeypatch.setenv("CLAUDECODE", "session")
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "1")
    warning = pm_import.fill_harness_cap_advisory("claude", tmp_path)
    assert warning is not None
    assert warning.startswith("[fill auto] 경고:")
    assert "부분 산출물 보존 전에" in warning


def test_fill_harness_cap_advisory_counts_startup_budget_without_progress_signal(
        pm_import, monkeypatch, tmp_path):
    """advisory는 실행 경로와 같이 startup 선언을 진행 신호로 다시 줄이지 않는다."""
    monkeypatch.setitem(
        pm_import.FILL_DRIVER_BY_CMD,
        pm_import.OPENCODE_FILL_CMD,
        ("opencode", False, None),
    )
    monkeypatch.setenv("OPENCODE", "session")
    monkeypatch.setenv("OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS", "14619000")

    warning = pm_import.fill_harness_cap_advisory("opencode", tmp_path)

    assert warning is not None
    assert "14620s" in warning


# ── DoD ①: --fill auto + stub runner → 자유서술 3종 해소·제안 생성 (토큰 0) ────

def test_fill_auto_stub_resolves_free_form(pm_import, tmp_path):
    """claude 트리: run_fill(auto·stub) → 자유서술 3종 값 + 초안 제안. 실 바이너리 미호출."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="ClaudeFill")
    stub = _StubRunner(ok=True, output="## 프로젝트 고유 제약\n- 핵심 결정은 순수 코드.")

    result = pm_import.run_fill(dest, "claude", live=False, runner=stub)

    assert result.mode == "auto"
    assert result.harness == "claude"
    # 자유서술 3종이 해소(값 채워짐)됐다.
    for token in FREE_FORM_TOKENS:
        assert token in result.values, f"{token} 가 제안 값에 없음."
        assert result.values[token].strip(), f"{token} 제안 값이 빔."
    # 초안 제안 생성.
    assert result.drafts, "초안 제안이 비어있음."
    # 토큰 0 증거: stub 가 정확히 1회 호출(실 바이너리 아님).
    assert len(stub.calls) == 1


def test_fill_auto_stub_opencode_excludes_model_token(pm_import, tmp_path, monkeypatch):
    """opencode 트리: {{OPENCODE_PRO_MODEL}} 은 LLM fill 후보가 *아니다*(T-0033 결정적 분리).

    모델 토큰은 resolve_opencode_model(결정적 `opencode models` 조회)이 전담하므로 fill 의
    제안 값에 들어가면 안 된다(중복·환각 제거). 자유서술 3종만 fill 대상이다.

    main 의 정상 파이프라인은 substitute_placeholders 직후 resolve_opencode_model 이 돌아
    토큰을 항상 해소/중화한다(치환 또는 `<provider/model>` 폴백) — 그래서 실 import 완료 트리엔
    리터럴 토큰이 남지 않는다. 이 테스트가 검증하려는 "fill 은 model 토큰을 안 건드린다" 계약은
    **model 해소 전** 시점(substitute 직후)의 실 어댑터 파일(`.opencode/agents/*.md` 의
    `model:` 필드)을 대상으로 해야 하므로, resolve_opencode_model 을 no-op stub 으로 바꿔
    그 시점을 재현한다(T-0192 #6 전 README 문서화 산문 예시를 실 출하 파일로 repoint).
    """
    monkeypatch.setattr(
        pm_import, "resolve_opencode_model",
        lambda dest_root, copied_relpaths, **kwargs: pm_import.ModelResolveResult(
            active=False, path="inactive", note="테스트 stub — 모델 해소 단계 건너뜀.",
        ),
    )
    # render_managed_files(@render)가 미해소 리터럴 토큰을 leak 로 hard-fail 하므로(T-0133
    # RenderLeakError) 함께 no-op — 이 테스트는 fill 스캔(run_fill)만 격리 검증한다(render 계약은
    # test_pm_render.py 소관).
    monkeypatch.setattr(
        pm_import,
        "render_managed_files",
        lambda dest_root, subs, copied, **kwargs: 0,
    )
    dest = _make_imported_tree(pm_import, tmp_path, harness="opencode", name="OpenFill")
    # 어댑터 트리이므로 모델 토큰은 잔존하나(전제 확인) — 그건 resolve_opencode_model 소관.
    assert pm_import._token_present(dest, OPENCODE_MODEL_TOKEN), \
        "opencode 트리인데 {{OPENCODE_PRO_MODEL}} 토큰이 안 보임 — 전제 깨짐."

    stub = _StubRunner(ok=True, output='{"result": "ollama/gemma4:26b"}')
    result = pm_import.run_fill(dest, "opencode", live=False, runner=stub)

    assert OPENCODE_MODEL_TOKEN not in result.values, \
        "모델 토큰이 LLM fill 제안 대상에 끼어듦(T-0033 분리 위반)."
    # 트리에 *실제로 존재하는* 자유서술 토큰만 fill 대상(없는 토큰은 채울 필요 없음).
    # {{USER_GATE_ITEMS}} 는 pm_role.local.md(양 트리)에 있어 opencode 트리에도 존재(→present 분기).
    for token in FREE_FORM_TOKENS:
        if pm_import._token_present(dest, token):
            assert token in result.values, f"{token} 가 트리에 있는데 제안 대상에서 빠짐."
        else:
            assert token not in result.values, f"{token} 가 트리에 없는데 제안 대상에 끼어듦."


def test_fill_auto_claude_excludes_opencode_token(pm_import, tmp_path):
    """claude-only 트리에는 {{OPENCODE_PRO_MODEL}} 가 없으므로 fill 대상에서 빠진다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="NoOpen")
    assert not pm_import._token_present(dest, OPENCODE_MODEL_TOKEN), \
        "claude-only 트리인데 opencode 모델 토큰이 잔존."
    stub = _StubRunner()
    result = pm_import.run_fill(dest, "claude", live=False, runner=stub)
    assert OPENCODE_MODEL_TOKEN not in result.values


# ── DoD ②: --fill manual(기본) → 하니스 미호출·TODO 표시 ─────────────────────

def test_fill_manual_is_default_and_marks_todo(pm_import, tmp_path):
    """main 기본(--fill 미지정 = manual): 하니스 미구동, 자유서술 placeholder 에 TODO 표시."""
    dest = tmp_path / "manualdefault"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Manual"])
    assert rc == 0
    # 자유서술 placeholder 가 있는 파일(root doc CLAUDE.md §프로젝트 고유 제약)에 TODO 마커가 표시됐다.
    # (ADR-0030: 어댑터는 free-form-free → 마커는 root doc·pm_role.local.md 에 — developer.md 는 더 이상 토큰 없음.)
    marked = dest / "CLAUDE.md"
    text = marked.read_text(encoding="utf-8")
    # placeholder 는 보존되되, 그 줄(또는 인접)에 TODO 마커가 있어야 한다.
    assert "TODO" in text, "manual 모드인데 TODO 표시가 없음."


def test_run_manual_fill_marks_only_missing_todo(pm_import, tmp_path):
    """_run_manual_fill: TODO 가 이미 있는 줄엔 마커를 추가하지 않는다(비파괴·중복 방지)."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="ManualMark")
    result = pm_import._run_manual_fill(dest)
    assert result.mode == "manual"
    # 표시 결과 멱등 — 재실행 시 추가 마킹 0.
    result2 = pm_import._run_manual_fill(dest)
    assert result2.todos == [], "manual fill 재실행이 멱등하지 않음(중복 TODO 마킹)."


def test_fill_manual_does_not_call_runner(pm_import, tmp_path):
    """manual 경로는 runner seam 을 절대 건드리지 않는다 — run_fill auto 와 대비."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="NoRunner")
    # _run_manual_fill 은 runner 인자 자체가 없다(하니스 미구동 보장). 호출만으로 검증.
    result = pm_import._run_manual_fill(dest)
    assert result.runner_calls == [], "manual fill 이 하니스를 호출함."


# ── DoD ③: opt-in 게이트 — 환경변수 미설정 시 실호출 차단 ─────────────────────

def test_gate_blocks_without_env(pm_import, monkeypatch):
    """PM_IMPORT_LIVE_HARNESS 미설정 → --fill auto 라도 _live_harness_allowed False."""
    monkeypatch.delenv("PM_IMPORT_LIVE_HARNESS", raising=False)
    assert pm_import._live_harness_allowed("auto") is False
    assert pm_import._live_harness_allowed("manual") is False


def test_gate_requires_both_env_and_auto(pm_import, monkeypatch):
    """게이트는 env=1 AND mode=auto 동시 충족 시만 통과."""
    monkeypatch.setenv("PM_IMPORT_LIVE_HARNESS", "1")
    assert pm_import._live_harness_allowed("auto") is True
    # env 만 있고 manual 이면 차단.
    assert pm_import._live_harness_allowed("manual") is False
    # env 가 거짓 값이면 차단.
    monkeypatch.setenv("PM_IMPORT_LIVE_HARNESS", "0")
    assert pm_import._live_harness_allowed("auto") is False


def test_main_auto_without_env_forces_manual(pm_import, tmp_path, monkeypatch, capsys):
    """main --fill auto + 게이트 미통과(env 없음) → 실호출 차단, manual 폴백(TODO 표시).

    실 runner(_real_harness_runner)가 절대 호출되면 안 된다 — 호출 시 pytest.fail.
    """
    monkeypatch.delenv("PM_IMPORT_LIVE_HARNESS", raising=False)
    monkeypatch.setattr(
        pm_import, "_real_harness_runner",
        lambda argv, prompt: pytest.fail("게이트 미통과인데 실 하니스가 호출됨 — opt-in 위반."),
    )
    dest = tmp_path / "autonoenv"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "AutoNoEnv",
                         "--fill", "auto"])
    assert rc == 0
    out = capsys.readouterr().out
    # 게이트 미통과 안내 + manual 폴백(TODO 표시).
    assert "stub/미구동" in out or "게이트 미통과" in out
    text = (dest / "CLAUDE.md").read_text(encoding="utf-8")  # ADR-0030: free-form 은 root doc 에(어댑터 아님)
    assert "TODO" in text, "게이트 미통과 manual 폴백인데 TODO 표시가 없음."


def test_run_fill_live_false_no_runner_does_not_call_real(pm_import, tmp_path, monkeypatch):
    """run_fill(live=False, runner=None) → 실 runner 미호출(stub 없음 → 미구동 경로)."""
    monkeypatch.setattr(
        pm_import, "_real_harness_runner",
        lambda argv, prompt: pytest.fail("live=False·runner 없음인데 실 하니스 호출됨."),
    )
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="LiveFalse")
    result = pm_import.run_fill(dest, "claude", live=False, runner=None)
    # 미구동 — 값 없음(제안 없음). note 로 manual 폴백 권장.
    assert result.values == {}
    assert result.runner_calls == []


# ── DoD ④: claude/opencode runner 분기 — 명령 조립 검증 ──────────────────────

def test_runner_argv_claude_branch(pm_import, tmp_path):
    """claude 분기: stub 에 `claude -p "<프롬프트>"` 형태로 조립돼 전달된다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="ArgvClaude")
    stub = _StubRunner()
    pm_import.run_fill(dest, "claude", live=False, runner=stub)
    assert len(stub.calls) == 1
    argv, prompt = stub.calls[0]
    assert argv[0] == "claude" and "-p" in argv
    # 마지막 인자가 프롬프트(repo 분석 지시).
    assert argv[-1] == prompt
    assert "--format" not in argv, "claude 분기에 opencode json 플래그가 섞임."


def test_runner_argv_opencode_branch(pm_import, tmp_path):
    """opencode 분기: `opencode run "<프롬프트>" --format json` 형태로 조립된다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="opencode", name="ArgvOpen")
    stub = _StubRunner(ok=True, output='{"result": "텍스트"}')
    pm_import.run_fill(dest, "opencode", live=False, runner=stub)
    assert len(stub.calls) == 1
    argv, _ = stub.calls[0]
    assert argv[0] == "opencode" and argv[1] == "run"
    assert "--format" in argv and "json" in argv, "opencode 분기에 --format json 누락."


def test_build_runner_argv_units(pm_import):
    """_build_runner_argv 단위: 분기별 명령 조립 직접 검증."""
    claude_argv = pm_import._build_runner_argv("claude", "PROMPT")
    assert claude_argv == ["claude", "-p", "PROMPT"]
    open_argv = pm_import._build_runner_argv("opencode", "PROMPT")
    assert open_argv == ["opencode", "run", "PROMPT", "--format", "json"]


def test_opencode_json_text_extracted(pm_import, tmp_path):
    """opencode 출력(--format json) 에서 결과 텍스트가 추출돼 제안 값에 들어간다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="opencode", name="JsonParse")
    stub = _StubRunner(ok=True, output='{"result": "추출된 제안 텍스트"}')
    result = pm_import.run_fill(dest, "opencode", live=False, runner=stub)
    # json 의 result 필드 텍스트가 제안 값으로 추출됐다(원문 json 그대로가 아님).
    assert any("추출된 제안 텍스트" == v for v in result.values.values()), \
        "opencode json result 텍스트가 추출되지 않음."


def test_fill_harness_resolution(pm_import, monkeypatch):
    """명시값 우선, 집합이면 registry 순서의 첫 가용 하네스를 선택한다."""
    # claude가 가용한 조합 판정을 결정론화한다.
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: h == "claude")
    assert pm_import._resolve_fill_harness(None, "claude") == "claude"
    assert pm_import._resolve_fill_harness(None, "opencode") == "opencode"
    assert pm_import._resolve_fill_harness(None, "claude,opencode") == "claude"
    assert pm_import._resolve_fill_harness("opencode", "claude") == "opencode"  # override.
    # codex 는 standalone — --harness codex → fill harness codex(claude 폴백 아님·ADR-0070 D5).
    assert pm_import._resolve_fill_harness(None, "codex") == "codex"


# ── codex fill runner (ADR-0070 D5·silent claude 폴백 소멸·T-0403 재작업) ──────
# 과거 버그: `--harness codex --fill auto` 가 codex fill runner 미등록으로 조용히 `claude -p` 로
# 폴백하고 출력만 harness=codex 로 오표기(PM_IMPORT_LIVE_HARNESS=1 에서 잘못된 바이너리 호출).
# 수정: codex runner 명시 등록(codex exec --json·stdin DEVNULL·codex JSONL 파서) + 미지원 harness fail-loud.

def test_build_runner_argv_codex(pm_import, tmp_path):
    """codex → `codex exec --json -s workspace-write --skip-git-repo-check -C <dest> <prompt>`
    (과거 silent `claude -p` 폴백 소멸). dest_root 생략 시 -C 없이 조립(cwd 바인딩이 workdir 담당)."""
    argv = pm_import._build_runner_argv("codex", "PROMPT", tmp_path)
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[-1] == "PROMPT"                          # 프롬프트 = 마지막 positional
    assert argv[0] != "claude"                           # 오폴백 회귀 방지(회귀 가드)
    # dest_root 생략 → -C 없이 조립.
    argv2 = pm_import._build_runner_argv("codex", "P")
    assert argv2[:2] == ["codex", "exec"] and "-C" not in argv2


def test_build_runner_argv_unknown_harness_fail_loud(pm_import):
    """미지원 harness 는 silent claude 폴백이 아니라 fail-loud (잘못된 바이너리 호출·오표기 방지·
    제4 하네스 재발 방지·ADR-0070 D5)."""
    with pytest.raises(ValueError) as exc:
        pm_import._build_runner_argv("bogus", "P")
    assert "bogus" in str(exc.value)


def test_parse_codex_json_extracts_agent_message(pm_import):
    """codex `exec --json` JSONL → 최종 agent_message .text 추출·미발견/비-JSON 은 원문 fail-soft."""
    jsonl = ('{"type":"thread.started","thread_id":"t1"}\n'
             '{"type":"item.completed","item":{"type":"agent_message","text":"첫 제안"}}\n'
             '{"type":"item.completed","item":{"type":"agent_message","text":"최종 제안"}}\n'
             '{"type":"turn.completed","usage":{"input":10,"output":5}}')
    assert pm_import._parse_codex_json(jsonl) == "최종 제안"       # 마지막 agent_message
    assert pm_import._parse_codex_json("not json at all") == "not json at all"  # fail-soft
    # agent_message 없음(예: 도구만) → 원문 반환(fail-soft).
    no_msg = '{"type":"turn.completed","usage":{}}'
    assert pm_import._parse_codex_json(no_msg) == no_msg


def test_run_fill_codex_uses_codex_parser_and_labels_codex(pm_import, tmp_path):
    """codex fill: 응답을 codex JSONL 파서(agent_message)로 추출·FillResult.harness=codex(오표기 소멸)·
    argv=codex 커맨드(과거 silent claude 오호출 회귀 방지)."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="codex", name="CodexFill")
    jsonl = ('{"type":"thread.started","thread_id":"t1"}\n'
             '{"type":"item.completed","item":{"type":"agent_message","text":"코덱스 제안 텍스트"}}\n'
             '{"type":"turn.completed","usage":{"input":10,"output":5}}')
    stub = _StubRunner(ok=True, output=jsonl)
    result = pm_import.run_fill(dest, "codex", live=False, runner=stub)
    assert result.mode == "auto"
    assert result.harness == "codex"                     # 오표기 소멸(claude 아님)
    # codex JSONL agent_message 텍스트가 추출됐다(원문 JSONL 그대로가 아님).
    assert any("코덱스 제안 텍스트" == v for v in result.values.values()), result.values
    # argv = codex 커맨드(claude 오호출 회귀 방지) + dest workdir 핀.
    assert stub.calls, "stub 미호출"
    argv0 = stub.calls[0][0]
    assert argv0[:2] == ["codex", "exec"] and argv0[0] != "claude", argv0
    assert "-C" in argv0


def test_real_harness_runner_codex_stdin_eof(pm_import, monkeypatch):
    """공용 워치독에서도 codex stdin 은 즉시 EOF, claude 는 기존 상속을 유지한다."""
    captured: dict = {}
    relay = pm_import._load_watchdog()

    class _FakeRelay:
        def __getattr__(self, name):
            return getattr(relay, name)

        def run_with_first_event_watchdog(self, argv, **kw):
            captured["input_text"] = kw.get("input_text")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: _FakeRelay())
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)
    # codex → 빈 stdin PIPE를 닫아 EOF.
    pm_import._real_harness_runner(pm_import._build_runner_argv("codex", "P"), "P")
    assert captured["input_text"] == ""
    # claude → None(상속·현행 무변경 — codex-특수 stdin 이 다른 하니스에 새지 않음).
    captured.clear()
    pm_import._real_harness_runner(pm_import._build_runner_argv("claude", "P"), "P")
    assert captured["input_text"] is None


def test_fill_driver_codex_plain_keeps_stdin_eof_without_incremental_signal(pm_import):
    """codex 평문 출력도 stdin EOF는 유지하되 JSONL 증분 신호로 오인하지 않는다."""
    harness, emits_progress, input_text = pm_import._fill_driver(
        ["codex", "exec", "prompt"]
    )

    assert harness == ""
    assert emits_progress is False
    assert input_text == ""


# ── codex live fill (gated·실 실행은 T-0407·과금) ─────────────────────────────

_codex_live_gate = pytest.mark.skipif(
    os.environ.get("PM_IMPORT_LIVE_HARNESS", "").strip() not in ("1", "true", "yes", "on")
    or shutil.which("codex") is None,
    reason="codex live fill — PM_IMPORT_LIVE_HARNESS=1 + codex 바이너리 필요(실 실행은 T-0407·과금).",
)


@_codex_live_gate
def test_codex_live_fill_real_binary(pm_import, tmp_path):
    """[gated·실 실행 T-0407·과금] 실 codex 바이너리로 codex fill — argv=codex exec·harness=codex 라벨.

    게이트(PM_IMPORT_LIVE_HARNESS + codex 설치) 미충족이면 collection 단계에서 skip(일반 서브셋
    무영향). 충족 시 실 gpt-5.5 구동 — argv 가 codex 커맨드이고 라벨이 정합(claude 오호출 아님)."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="codex", name="CodexLive")
    result = pm_import.run_fill(dest, "codex", live=True, copied_relpaths=None)
    assert result.harness == "codex"
    assert result.runner_calls and result.runner_calls[0][:2] == ["codex", "exec"]


# ── DoD ⑤: --dry-run + --fill auto → 제안 출력·파일 미변경 ────────────────────

def test_dry_run_auto_does_not_touch_fs(pm_import, tmp_path, capsys):
    """--dry-run + --fill auto: 디렉토리 미생성·파일 미변경, fill 의도만 출력."""
    dest = tmp_path / "dryauto"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryAuto",
                         "--fill", "auto", "--dry-run"])
    assert rc == 0
    assert not dest.exists(), "--dry-run 인데 대상 디렉토리가 생성됨."
    out = capsys.readouterr().out
    assert "fill=auto" in out
    assert "dry-run" in out.lower()


def test_dry_run_auto_does_not_call_real_harness(pm_import, tmp_path, monkeypatch):
    """--dry-run + --fill auto + 게이트 통과(env 설정)여도 실 하니스 미호출(파일 미변경)."""
    monkeypatch.setenv("PM_IMPORT_LIVE_HARNESS", "1")
    monkeypatch.setattr(
        pm_import, "_real_harness_runner",
        lambda argv, prompt: pytest.fail("dry-run 인데 실 하니스가 호출됨."),
    )
    dest = tmp_path / "dryautoenv"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryAutoEnv",
                         "--fill", "auto", "--dry-run"])
    assert rc == 0
    assert not dest.exists()


# ── fail-soft: 하니스 실패 시 import 안 깨짐 ──────────────────────────────────

def test_fill_runner_failure_is_soft(pm_import, tmp_path):
    """stub 가 (ok=False) 를 반환해도 run_fill 은 예외 없이 note 로 보고(fail-soft)."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="SoftFail")
    stub = _StubRunner(ok=False, output="[하니스 오류]")
    result = pm_import.run_fill(dest, "claude", live=False, runner=stub)
    assert result.mode == "auto"
    assert result.values == {}, "실패인데 제안 값이 채워짐."
    assert "실패" in result.note
    # argv 는 기록(시도 흔적).
    assert result.runner_calls and result.runner_calls[0][0] == "claude"


def test_fill_runner_failure_saves_complete_private_raw(pm_import, tmp_path):
    """긴 stdout 뒤 stderr까지 private raw에 전량 박제하고 note에는 경로만 남긴다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="RawFail")
    output = "[stdout]\n" + ("O" * 6000) + "\n[stderr]\n" + ("E" * 3000)

    result = pm_import.run_fill(
        dest, "claude", live=False, runner=_StubRunner(ok=False, output=output))

    assert len(result.note) < 300
    assert "부분/오류 출력 원문 보존:" in result.note
    relative = result.note.split("부분/오류 출력 원문 보존: ", 1)[1]
    raw_path = dest / relative
    assert raw_path.read_text(encoding="utf-8") == output
    if posix_mode_supported():
        assert raw_path.stat().st_mode & 0o777 == 0o600
    assert "E" * 100 not in result.note, "raw가 note에 중복 노출됨"


def test_fill_failure_raw_without_dir_fd_or_o_nofollow_saves_path(
        pm_import, monkeypatch, tmp_path):
    """dir_fd/O_NOFOLLOW가 없는 형상에서도 raw 원문과 반환 경로를 보존한다."""
    dest = _make_imported_tree(
        pm_import, tmp_path, harness="claude", name="PortableRaw",
    )
    monkeypatch.setattr(pm_import.os, "supports_dir_fd", frozenset())
    monkeypatch.delattr(pm_import.os, "O_NOFOLLOW", raising=False)

    raw_path = pm_import._save_fill_failure_output(
        dest, "claude", "portable raw output",
    )

    assert raw_path.is_relative_to(dest)
    assert raw_path.read_text(encoding="utf-8") == "portable raw output"


@pytest.mark.skipif(not _can_symlink(), reason="symlink 생성 능력 필요")
def test_fill_failure_raw_rechecks_containment_after_file_creation(
        pm_import, monkeypatch, tmp_path):
    """파일 open 직전 중간 경로가 바뀌어도 repo 밖 raw를 삭제하고 chmod하지 않는다."""
    dest = _make_imported_tree(
        pm_import, tmp_path, harness="claude", name="RawPostCreateSwap",
    )
    local = dest / ".project_manager" / ".local"
    fill = local / "fill"
    fill.mkdir(parents=True)
    parked = local.with_name(".local-parked")
    outside = tmp_path / "outside-post-create"
    outside_fill = outside / "fill"
    outside_fill.mkdir(parents=True, mode=0o755)
    outside_fill.chmod(0o755)
    original_open = pm_import.os.open
    swapped = False

    def _open_then_swap(path, flags, mode=0o777, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).parent == fill:
            local.rename(parked)
            local.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pm_import.os, "open", _open_then_swap)
    try:
        with pytest.raises(OSError, match="대상 repo 밖"):
            pm_import._save_fill_failure_output(dest, "claude", "must be removed")
    finally:
        if local.is_symlink():
            local.unlink()
            parked.rename(local)

    assert swapped
    assert list(outside_fill.iterdir()) == []
    if posix_mode_supported():
        assert stat.S_IMODE(outside_fill.stat().st_mode) == 0o755


def test_fill_runner_failure_exposes_full_output_if_raw_save_fails(
        pm_import, tmp_path, monkeypatch):
    """raw 박제 실패도 fail-soft이며 preview 절단 없이 stderr 끝까지 화면 note에 싣는다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="RawFallback")
    output = "O" * 600 + "\n[stderr]\nTAIL-ERR"
    monkeypatch.setattr(
        pm_import, "_save_fill_failure_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only")),
    )

    result = pm_import.run_fill(
        dest, "claude", live=False, runner=_StubRunner(ok=False, output=output))

    assert "raw 저장 실패" in result.note
    assert output in result.note


@pytest.mark.skipif(not _can_symlink(), reason="symlink 생성 능력 필요")
@pytest.mark.parametrize("linked_component", [".local", "fill"])
def test_fill_failure_raw_rejects_symlink_component_without_touching_target(
        pm_import, tmp_path, linked_component):
    """raw 경로 또는 조상이 symlink면 repo 밖 chmod/create 없이 전문 표시 폴백한다."""
    dest = _make_imported_tree(
        pm_import, tmp_path, harness="claude", name=f"RawLink{linked_component}")
    local = dest / ".project_manager" / ".local"
    outside = tmp_path / f"outside-{linked_component}"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)

    if linked_component == ".local":
        if local.exists():
            shutil.rmtree(local)
        local.symlink_to(outside, target_is_directory=True)
    else:
        local.mkdir(parents=True, exist_ok=True)
        (local / "fill").symlink_to(outside, target_is_directory=True)

    output = "FULL-STDOUT\n[stderr]\nFULL-TAIL"
    result = pm_import.run_fill(
        dest, "claude", live=False, runner=_StubRunner(ok=False, output=output))

    assert "raw 저장 실패" in result.note
    assert output in result.note, "symlink 거부 폴백이 원문을 절단함"
    assert list(outside.iterdir()) == [], "repo 밖 symlink 대상에 raw 파일을 생성함"
    if posix_mode_supported():
        assert outside.stat().st_mode & 0o777 == 0o755, "repo 밖 디렉터리 권한을 변경함"


# ── 비파괴 (MF·T-0009 반려 수정): fill 은 이번 import 가 복사한 파일만 건드린다 ──────
# --into dest 에 이번 import 가 복사하지 *않는* 기존 사용자 파일(우연히 sentinel 포함)을
# 두고, fill 단계가 그 파일을 절대 스캔/수정하지 않는지 단언한다(T-0007 비파괴 계약 충돌 해소).

def _make_into_dest_with_user_file(tmp_path, name="into_target"):
    """--into 대상이 될 git repo 디렉토리 + 자유서술 sentinel 을 품은 사용자 파일을 만든다.

    NOTES.md 는 템플릿에 없으므로 이번 import 가 복사하지 않는다 — 따라서 fill 단계가
    이 파일을 건드리면 비파괴 위반이다(이 파일 안 {{PROJECT_CONSTRAINTS}} 는 import 와 무관).
    --into 는 기존 git repo·디렉토리를 전제하므로 git init 까지 해 둔다.
    """
    dest = tmp_path / name
    dest.mkdir()
    subprocess.run(["git", "init", str(dest)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    user_file = dest / "NOTES.md"
    user_content = (
        "# 내 노트\n\n"
        "여기 우연히 sentinel 이 들어있다: {{PROJECT_CONSTRAINTS}}\n"
        "그리고 {{PROTECTED_PATHS}} 도 본문에 등장한다.\n"
    )
    user_file.write_text(user_content, encoding="utf-8")
    return dest, user_file, user_content


def test_into_manual_fill_does_not_touch_user_file(pm_import, tmp_path):
    """--into + manual: 복사 안 한 사용자 파일(sentinel 포함)은 TODO 마킹 없이 불변."""
    dest, user_file, original = _make_into_dest_with_user_file(tmp_path, "into_manual")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "IntoManual"])
    assert rc == 0
    # 사용자 파일이 글자 하나 안 바뀌었다 — TODO 마커도 sentinel 치환도 없음.
    assert user_file.read_text(encoding="utf-8") == original, \
        "manual fill 이 복사 안 한 사용자 파일을 수정함(비파괴 위반)."
    assert "TODO" not in user_file.read_text(encoding="utf-8")
    # 반면 이번 import 가 복사한 파일에는 TODO 표시가 됐다(범위 한정이 맞다는 양성 증거).
    # ADR-0030: 어댑터 free-form-free → 마커는 복사된 root doc CLAUDE.md(§프로젝트 고유 제약).
    copied_marked = dest / "CLAUDE.md"
    assert "TODO" in copied_marked.read_text(encoding="utf-8")


def test_into_auto_stub_fill_does_not_touch_user_file(pm_import, tmp_path, monkeypatch):
    """--into + auto(게이트 미통과 → manual 폴백): 사용자 파일 불변(실 하니스도 미호출)."""
    monkeypatch.delenv("PM_IMPORT_LIVE_HARNESS", raising=False)
    monkeypatch.setattr(
        pm_import, "_real_harness_runner",
        lambda argv, prompt: pytest.fail("게이트 미통과인데 실 하니스가 호출됨."),
    )
    dest, user_file, original = _make_into_dest_with_user_file(tmp_path, "into_auto")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "IntoAuto",
                         "--fill", "auto"])
    assert rc == 0
    assert user_file.read_text(encoding="utf-8") == original, \
        "auto(manual 폴백) fill 이 복사 안 한 사용자 파일을 수정함(비파괴 위반)."


def test_token_present_scoped_to_copied_relpaths(pm_import, tmp_path):
    """_token_present 가 copied_relpaths 범위 밖 사용자 파일의 sentinel 을 보지 않는다."""
    dest, _user_file, _ = _make_into_dest_with_user_file(tmp_path, "into_scope")
    # 빈 copied_relpaths → NOTES.md 의 sentinel 이 있어도 범위 밖이라 미검출.
    assert pm_import._token_present(dest, "{{PROJECT_CONSTRAINTS}}", set()) is False, \
        "copied_relpaths 가 비었는데 사용자 파일 sentinel 을 검출함(범위 한정 실패)."
    # 폴백(None)에서는 전체 스캔하므로 검출된다(범위 한정 인자가 실제로 작동함을 대비 증명).
    assert pm_import._token_present(dest, "{{PROJECT_CONSTRAINTS}}", None) is True


def test_mark_todos_scoped_to_copied_relpaths(pm_import, tmp_path):
    """_mark_todos 가 copied_relpaths 밖 사용자 파일에는 마커를 주입하지 않는다."""
    dest, user_file, original = _make_into_dest_with_user_file(tmp_path, "into_mark_scope")
    marked = pm_import._mark_todos(
        dest, ["{{PROJECT_CONSTRAINTS}}", "{{PROTECTED_PATHS}}"], set())
    assert marked == [], "빈 copied_relpaths 인데 사용자 파일에 마커가 추가됨."
    assert user_file.read_text(encoding="utf-8") == original, \
        "_mark_todos 가 범위 밖 사용자 파일을 수정함(비파괴 위반)."


# ── MF1: 조합 + claude 부재 시 opencode 폴백 (회사 배포 1급 경로) ────────────

def test_combination_falls_back_to_opencode_when_claude_absent(pm_import, monkeypatch):
    """조합: claude 바이너리 부재 → opencode 폴백(회사 배포 claude code 없음)."""
    # claude 는 PATH 에 없고 opencode 만 있는 상황 stub.
    monkeypatch.setattr(pm_import, "_harness_binary_available",
                        lambda h: h == "opencode")
    assert pm_import._resolve_fill_harness(None, "claude,opencode") == "opencode", \
        "조합 + claude 부재인데 opencode 로 폴백하지 않음(회사 배포 1급 경로 깨짐)."


def test_combination_prefers_claude_when_present(pm_import, monkeypatch):
    """조합: claude 바이너리 존재 → registry상 claude 우선."""
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: True)
    assert pm_import._resolve_fill_harness(None, "claude,opencode") == "claude"


def test_combination_all_absent_returns_first_for_gate(pm_import, monkeypatch):
    """조합의 바이너리가 모두 부재하면 첫 하네스를 반환해 상위 게이트에 위임한다."""
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: False)
    assert pm_import._resolve_fill_harness(None, "claude,opencode") == "claude"


def test_explicit_fill_harness_overrides_binary_detection(pm_import, monkeypatch):
    """--fill-harness 명시값은 바이너리 유무와 무관하게 그대로 존중(사용자 의도 우선)."""
    # 둘 다 부재여도 명시값 opencode 는 그대로.
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: False)
    assert pm_import._resolve_fill_harness("opencode", "claude,codex") == "opencode"
    assert pm_import._resolve_fill_harness("claude", "opencode,codex") == "claude"


def test_harness_binary_available_uses_shutil_which(pm_import, monkeypatch):
    """_harness_binary_available 은 shutil.which 로 탐지(테스트가 patch 가능한 seam)."""
    monkeypatch.setattr(pm_import.shutil, "which",
                        lambda binary: "/usr/bin/claude" if binary == "claude" else None)
    assert pm_import._harness_binary_available("claude") is True
    assert pm_import._harness_binary_available("opencode") is False
    # 알 수 없는 harness를 False로 삼으면 조용히 첫 하네스로 떨어지므로 구성 오류를 loud 처리한다.
    with pytest.raises(ValueError, match="미등록 fill harness"):
        pm_import._harness_binary_available("nope")


@pytest.mark.skipif(
    not posix_mode_supported(),
    reason="확장 바이너리 fixture의 shebang/exec-bit 탐지는 POSIX 전용",
)
def test_real_fourth_registry_tree_drives_help_and_binary_resolution(
        pm_import, tmp_path, capsys):
    """소스 registry에 실제 4번째 트리를 등록하면 help와 가용 바이너리 선택이 자동 추종한다.

    제품 모듈 monkeypatch가 아니라 임시 checkout의 registry 소스 자체를 한 줄 확장하고
    ``templates/fourth_tmpl`` 및 PATH 실행 파일을 실제 생성하는 sensitivity다.
    """
    checkout = tmp_path / "checkout"
    tools_dir = checkout / ".project_manager" / "tools"
    tools_dir.mkdir(parents=True)
    source = (TOOLS / "pm_import.py").read_text(encoding="utf-8")
    registry_anchor = '    "codex": ("codex",),\n}'
    assert registry_anchor in source
    source = source.replace(
        registry_anchor,
        '    "codex": ("codex",),\n    "fourth": ("fourth_tmpl",),\n}',
        1,
    )
    (tools_dir / "pm_import.py").write_text(source, encoding="utf-8")
    shutil.copy2(TOOLS / "console_encoding.py", tools_dir / "console_encoding.py")
    for dirname in ("claude_code", "opencode", "codex", "fourth_tmpl"):
        (checkout / "templates" / dirname).mkdir(parents=True)

    spec = importlib.util.spec_from_file_location(
        "pm_import_with_real_fourth", tools_dir / "pm_import.py"
    )
    fourth_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fourth_module)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fourth_binary = bin_dir / "fourth"
    fourth_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fourth_binary.chmod(0o755)
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = str(bin_dir)
    try:
        roots = fourth_module.resolve_template_roots(checkout, "all")
        assert [root.name for root in roots][-1] == "fourth_tmpl"
        assert fourth_module._resolve_fill_harness(None, "all") == "fourth"
        with pytest.raises(SystemExit) as exc_info:
            fourth_module.main(["--help"])
        assert exc_info.value.code == 0
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--fill-harness {claude,opencode,codex,fourth}" in help_text
    assert "`fourth`" in help_text


def test_combination_runner_argv_uses_opencode_when_claude_absent(
        pm_import, tmp_path, monkeypatch):
    """end-to-end(stub): 조합 + claude 부재 → run_fill 이 opencode argv 로 조립한다."""
    # opencode 어댑터 트리를 import 해 토큰이 잔존하게 한다(조합 폴백 시 opencode가 채울 대상).
    dest = _make_imported_tree(pm_import, tmp_path, harness="opencode", name="BothFallback")
    monkeypatch.setattr(pm_import, "_harness_binary_available",
                        lambda h: h == "opencode")
    resolved = pm_import._resolve_fill_harness(None, "claude,opencode")
    assert resolved == "opencode"
    stub = _StubRunner(ok=True, output='{"result": "ollama/gemma4:26b"}')
    pm_import.run_fill(dest, resolved, live=False, runner=stub)
    assert len(stub.calls) == 1
    argv, _ = stub.calls[0]
    assert argv[0] == "opencode" and argv[1] == "run", \
        "조합 폴백인데 opencode runner argv 로 조립되지 않음."


# ── MF2: --dry-run + --fill auto → fill 계획 출력 (실호출·파일변경 없음) ────────

def test_dry_run_auto_prints_fill_plan(pm_import, tmp_path, capsys):
    """--dry-run + --fill auto: 채울 대상 토큰·결정된 harness·게이트 상태를 계획으로 출력."""
    dest = tmp_path / "dryplan"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryPlan",
                         "--fill", "auto", "--dry-run"])
    assert rc == 0
    assert not dest.exists(), "--dry-run 인데 대상 디렉토리가 생성됨."
    out = capsys.readouterr().out
    # 계획에 대상 토큰·fill harness·게이트 상태가 모두 등장.
    assert "채울 대상 토큰" in out, "dry-run 계획에 대상 토큰 목록이 없음."
    assert "{{PROJECT_CONSTRAINTS}}" in out, "dry-run 계획에 실제 자유서술 토큰이 안 나옴."
    assert "fill harness: claude" in out, "dry-run 계획에 결정된 fill harness 가 없음."
    assert "게이트" in out, "dry-run 계획에 opt-in 게이트 상태가 없음."


def test_dry_run_auto_plan_gate_not_passed_states_manual_fallback(pm_import, tmp_path,
                                                                  monkeypatch, capsys):
    """--dry-run + auto + 게이트 미통과(env 없음): 계획이 'manual 폴백' 상태를 명시한다."""
    monkeypatch.delenv("PM_IMPORT_LIVE_HARNESS", raising=False)
    dest = tmp_path / "drygate"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryGate",
                         "--fill", "auto", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "미통과" in out and "manual 폴백" in out, \
        "게이트 미통과 dry-run 계획이 manual 폴백 상태를 명시하지 않음."


def test_dry_run_auto_plan_gate_passed_states_live(pm_import, tmp_path, monkeypatch, capsys):
    """--dry-run + auto + 게이트 통과(env 설정): 계획이 '적용 시 실 하니스 구동' 을 명시."""
    monkeypatch.setenv("PM_IMPORT_LIVE_HARNESS", "1")
    # dry-run 이므로 게이트 통과여도 실 하니스는 절대 호출되면 안 된다.
    monkeypatch.setattr(
        pm_import, "_real_harness_runner",
        lambda *a, **k: pytest.fail("dry-run 인데 실 하니스가 호출됨."),
    )
    dest = tmp_path / "drygatepass"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryGatePass",
                         "--fill", "auto", "--dry-run"])
    assert rc == 0
    assert not dest.exists(), "--dry-run 인데 파일시스템이 변경됨."
    out = capsys.readouterr().out
    assert "통과" in out and "실 하니스 구동" in out, \
        "게이트 통과 dry-run 계획이 실구동 예정을 명시하지 않음."


def test_plan_fill_targets_reads_source_files(pm_import, tmp_path):
    """_plan_fill_targets: 복사 *예정* src 파일에서 잔존 토큰을 스캔(dest 미복사 상태)."""
    # 자유서술 토큰을 품은 src 와 안 품은 src 를 만들어 plan 스캔을 검증.
    src_with = tmp_path / "a.md"
    src_with.write_text("제약: {{PROJECT_CONSTRAINTS}} 그리고 {{PROTECTED_PATHS}}\n",
                        encoding="utf-8")
    src_without = tmp_path / "b.md"
    src_without.write_text("토큰 없음\n", encoding="utf-8")
    dest_root = tmp_path / "dest"
    actions = [
        pm_import.CopyAction(src_with, dest_root / "a.md", None),
        pm_import.CopyAction(src_without, dest_root / "b.md", None),
    ]
    targets = pm_import._plan_fill_targets(actions)
    assert "{{PROJECT_CONSTRAINTS}}" in targets
    assert "{{PROTECTED_PATHS}}" in targets
    assert "{{USER_GATE_ITEMS}}" not in targets, "src 에 없는 토큰이 계획에 끼어듦."
    # dest 는 만들어지지 않았다(plan 단계 — 파일 미변경).
    assert not dest_root.exists()


# ── SF: 실 하니스 구동 cwd = dest_root (대상 repo 에서 실행) ────────────────────

def test_real_harness_runner_runs_in_dest_cwd(pm_import, tmp_path, monkeypatch):
    """_real_harness_runner(cwd=dest_root): 공용 워치독에 대상 repo cwd가 전달된다."""
    captured = {}
    relay = pm_import._load_watchdog()

    class _FakeRelay:
        def __getattr__(self, name):
            return getattr(relay, name)

        def run_with_first_event_watchdog(self, argv, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: _FakeRelay())
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)
    dest = tmp_path / "targetrepo"
    dest.mkdir()
    ok, _out = pm_import._real_harness_runner(["claude", "-p", "P"], "P", cwd=dest)
    assert ok is True
    assert captured["cwd"] == str(dest), "실 하니스가 대상 repo(dest_root) cwd 에서 안 돈다."


def test_run_fill_live_binds_dest_cwd_to_real_runner(pm_import, tmp_path, monkeypatch):
    """run_fill(live=True): 실 runner 에 dest_root 가 cwd 로 바인딩돼 호출된다."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="LiveCwd")
    captured = {}

    def _fake_real(argv, prompt, cwd=None):
        captured["cwd"] = cwd
        return True, "제안 텍스트"

    monkeypatch.setattr(pm_import, "_real_harness_runner", _fake_real)
    pm_import.run_fill(dest, "claude", live=True, runner=None)
    assert captured["cwd"] == dest, "live 실행이 dest_root 를 cwd 로 바인딩하지 않음."


# ── ADR-0007 / T-0028: pm_playbook.local.md 스텁 생성 + 재-import 비파괴 ─────────
# 방법론(pm_playbook.md·엔진 synced)과 분리된 *인스턴스 소유* 누적 학습 칸을 import 가
# 자동 생성하고, 재-import 가 기존 내용을 덮지 않는지 검증한다(루트 T-0027 seam 정합).

PM_PLAYBOOK_LOCAL_RELPATH = Path(".project_manager") / "wiki" / "pm_playbook.local.md"


def test_import_creates_pm_playbook_local_stub(pm_import, tmp_path):
    """import 후 pm_playbook.local.md 스텁이 기대 마커와 함께 존재한다(ADR-0007)."""
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="PbStub")
    stub = dest / PM_PLAYBOOK_LOCAL_RELPATH
    assert stub.is_file(), "import 후 pm_playbook.local.md 스텁이 생성되지 않음."
    text = stub.read_text(encoding="utf-8")
    # 루트 T-0027 스텁 형식 정합: 프런트매터 type + 역참조 + TODO 절.
    assert "type: playbook-local" in text, "스텁 프런트매터 type 누락."
    assert "[[pm_playbook]]" in text, "스텁에 [[pm_playbook]] 역참조 누락."
    assert "TODO" in text, "스텁에 TODO 안내 절 누락."
    assert "인스턴스 소유" in text, "스텁에 manifest 밖·인스턴스 소유 안내 누락."


def test_reimport_does_not_clobber_pm_playbook_local(pm_import, tmp_path):
    """재-import(--into): 기존 pm_playbook.local.md 의 sentinel 이 살아남는다(비파괴)."""
    # 1) 최초 import — 스텁 생성.
    dest = _make_imported_tree(pm_import, tmp_path, harness="claude", name="PbReimport")
    stub = dest / PM_PLAYBOOK_LOCAL_RELPATH
    assert stub.is_file()
    # 2) 인스턴스가 누적 학습을 채운 상태를 흉내 — sentinel 주입.
    sentinel = "SENTINEL-누적학습-T0028 — 이 줄은 재-import 에서 보존돼야 한다.\n"
    stub.write_text(stub.read_text(encoding="utf-8") + sentinel, encoding="utf-8")
    # 3) 같은 dest 로 재-import(--into).
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "PbReimport"])
    assert rc == 0
    after = stub.read_text(encoding="utf-8")
    assert sentinel in after, "재-import 가 기존 pm_playbook.local.md 의 누적 학습을 덮어씀(비파괴 위반)."


def test_ensure_pm_playbook_local_stub_created_then_preserved(pm_import, tmp_path):
    """ensure_pm_playbook_local_stub: 신규=created, 기존=preserved(덮지 않음)."""
    dest = tmp_path / "ensure_target"
    (dest / ".project_manager" / "wiki").mkdir(parents=True)
    # 신규 생성.
    status1 = pm_import.ensure_pm_playbook_local_stub(dest, ".backup.2026-06-15")
    assert status1 == "created"
    stub = dest / PM_PLAYBOOK_LOCAL_RELPATH
    assert "type: playbook-local" in stub.read_text(encoding="utf-8")
    # sentinel 주입 후 재호출 — 비파괴 보존(미생성·내용 불변).
    sentinel = "SENTINEL-preserve\n"
    stub.write_text(stub.read_text(encoding="utf-8") + sentinel, encoding="utf-8")
    status2 = pm_import.ensure_pm_playbook_local_stub(dest, ".backup.2026-06-15")
    assert status2 == "preserved"
    assert sentinel in stub.read_text(encoding="utf-8"), \
        "preserved 경로가 기존 .local 내용을 보존하지 않음."
