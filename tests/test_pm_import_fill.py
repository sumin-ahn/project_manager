"""pm_import.py fill 단계 단위 테스트 — 자유서술 placeholder 하니스 구동(opt-in·T-0009).

T-0007 의 기계 단계 위에 얹은 fill 단계를 검증한다. 핵심 안전 계약:
  - 실 하니스(claude/opencode) 바이너리는 절대 호출하지 않는다 — 전부 stub runner(토큰 0).
  - opt-in 게이트: PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만 실 runner 경로.
    둘 중 하나라도 없으면 실호출 차단(stub/manual 강제) — 토큰·외부모델 비용 0.
  - 생성물은 *제안* — 자유서술 placeholder 만 채우고 자동 확정 안 함(비가역 회피).

run_fill 의 runner seam(주입 콜러블)으로 명령 조립을 토큰 0 으로 검증한다. main 통합은
PM_IMPORT_LIVE_HARNESS 환경변수 격리(monkeypatch.delenv/setenv)로 게이트만 검증한다 —
main 의 auto 경로는 게이트 통과 시 *실* runner 를 부르므로 테스트에서는 게이트를 막아둔다.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

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
    assert pm_import.FILL_HARNESS_CHOICES == ("claude", "opencode")
    assert pm_import.FREE_FORM_TOKENS == FREE_FORM_TOKENS
    assert pm_import.OPENCODE_MODEL_TOKEN == OPENCODE_MODEL_TOKEN
    assert pm_import.LIVE_HARNESS_ENV == "PM_IMPORT_LIVE_HARNESS"
    # FillResult 형태.
    fr = pm_import.FillResult(mode="auto")
    assert fr.mode == "auto"
    assert fr.values == {} and fr.drafts == {} and fr.todos == []


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
    monkeypatch.setattr(pm_import, "render_managed_files", lambda dest_root, subs, copied: 0)
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
    """_resolve_fill_harness: --fill-harness 우선, 없으면 --harness, both→claude(존재 시)."""
    # both→claude 폴백 판정을 결정론화: claude 바이너리 존재 stub.
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: h == "claude")
    assert pm_import._resolve_fill_harness(None, "claude") == "claude"
    assert pm_import._resolve_fill_harness(None, "opencode") == "opencode"
    assert pm_import._resolve_fill_harness(None, "both") == "claude"  # both→claude 우선(존재).
    assert pm_import._resolve_fill_harness("opencode", "claude") == "opencode"  # override.


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


# ── MF1: both + claude 부재 시 opencode 폴백 (회사 배포 1급 경로) ──────────────

def test_both_falls_back_to_opencode_when_claude_absent(pm_import, monkeypatch):
    """both: claude 바이너리 부재(which stub) → opencode 폴백(회사 배포 claude code 없음)."""
    # claude 는 PATH 에 없고 opencode 만 있는 상황 stub.
    monkeypatch.setattr(pm_import, "_harness_binary_available",
                        lambda h: h == "opencode")
    assert pm_import._resolve_fill_harness(None, "both") == "opencode", \
        "both + claude 부재인데 opencode 로 폴백하지 않음(회사 배포 1급 경로 깨짐)."


def test_both_prefers_claude_when_present(pm_import, monkeypatch):
    """both: claude 바이너리 존재 → claude 우선(opencode 도 있어도 claude)."""
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: True)
    assert pm_import._resolve_fill_harness(None, "both") == "claude"


def test_both_both_absent_returns_claude_for_gate(pm_import, monkeypatch):
    """both: claude·opencode 둘 다 부재 → claude 반환(상위 게이트/manual 폴백에 위임)."""
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: False)
    assert pm_import._resolve_fill_harness(None, "both") == "claude"


def test_explicit_fill_harness_overrides_binary_detection(pm_import, monkeypatch):
    """--fill-harness 명시값은 바이너리 유무와 무관하게 그대로 존중(사용자 의도 우선)."""
    # 둘 다 부재여도 명시값 opencode 는 그대로.
    monkeypatch.setattr(pm_import, "_harness_binary_available", lambda h: False)
    assert pm_import._resolve_fill_harness("opencode", "both") == "opencode"
    assert pm_import._resolve_fill_harness("claude", "both") == "claude"


def test_harness_binary_available_uses_shutil_which(pm_import, monkeypatch):
    """_harness_binary_available 은 shutil.which 로 탐지(테스트가 patch 가능한 seam)."""
    monkeypatch.setattr(pm_import.shutil, "which",
                        lambda binary: "/usr/bin/claude" if binary == "claude" else None)
    assert pm_import._harness_binary_available("claude") is True
    assert pm_import._harness_binary_available("opencode") is False
    # 알 수 없는 harness 는 보수적으로 False.
    assert pm_import._harness_binary_available("nope") is False


def test_both_runner_argv_uses_opencode_when_claude_absent(pm_import, tmp_path, monkeypatch):
    """end-to-end(stub): both + claude 부재 → run_fill 이 opencode argv 로 조립한다."""
    # opencode 어댑터 트리를 import 해 토큰이 잔존하게 한다(both 폴백 시 opencode 가 채울 대상).
    dest = _make_imported_tree(pm_import, tmp_path, harness="opencode", name="BothFallback")
    monkeypatch.setattr(pm_import, "_harness_binary_available",
                        lambda h: h == "opencode")
    resolved = pm_import._resolve_fill_harness(None, "both")
    assert resolved == "opencode"
    stub = _StubRunner(ok=True, output='{"result": "ollama/gemma4:26b"}')
    pm_import.run_fill(dest, resolved, live=False, runner=stub)
    assert len(stub.calls) == 1
    argv, _ = stub.calls[0]
    assert argv[0] == "opencode" and argv[1] == "run", \
        "both 폴백인데 opencode runner argv 로 조립되지 않음."


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
    """_real_harness_runner(cwd=dest_root): subprocess.run 에 cwd 가 대상 repo 로 전달된다."""
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeCompleted()

    monkeypatch.setattr(pm_import.subprocess, "run", _fake_run)
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
