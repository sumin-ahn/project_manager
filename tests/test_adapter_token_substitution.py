"""어댑터 토큰 치환의 **채택자 산출물** 가드 (T-0424).

`{{PROJECT_NAME}}` 같은 operational placeholder 는 두 채널로 채택자 값이 된다:
  ① sed 채널 — `pm_import.substitute_placeholders`(+`_should_substitute` 판정)
  ② render 채널 — `pm_import.render_managed_files` / `pm_update` 의 `@render` 재전파

둘 다 **확장자 열거**(`SUBSTITUTE_SUFFIXES = (".md",".json",".sh",".py")` · `.md` 하드 필터)에
묶여 있어서, codex(세 번째 하니스)가 들여온 `.codex/agents/*.toml` 이 어느 채널도 못 타고
채택자 트리에 `{{PROJECT_NAME}}` 리터럴로 출하됐다. 4600 green 뒤에 숨어 있던 이유는 기존
가드(`tests/test_codex_adapter_delegation.py`)가 **템플릿에 토큰이 *있다*** 만 봤기 때문이다 —
"채택자 트리에서 *치환됐다*" 를 아무도 안 봤다.

이 파일은 그 공백을 **채택자 산출물 기준**으로 닫는다:
  (a) 하니스 전수(claude·opencode·codex) fresh `--new` import → 어댑터 네임스페이스에 `{{` 잔존 0.
  (b) codex `.codex/agents/*.toml` 4개에 실제 프로젝트명이 박힌다.
  (c) add-harness 경로(기존 인스턴스에 codex 추가)도 잔존 0 — 이 경로는 dest manifest 에
      `.codex/agents @render` 항목이 없어 render 가 no-op 이므로 **sed 채널이 반드시 잡아야** 한다.
      두 경로가 서로 다른 채널로 커버됨을 각각 확인한다.
  (d) pm_update `@render` 재전파도 `.toml` 을 렌더한다(세 번째 채널).
  (e) 판정이 **제외 사유 기반**으로 역전됐다 — 새 하니스의 새 형식(`.yaml`·`.jsonc`)은 자동 편입,
      엔진 소스(`tools/**`)·엔진 메타데이터(`engine.manifest`)·방법론 문서는 여전히 제외.

기계 테스트 — 라이브 하니스/네트워크 0(`opencode models` 조회는 stub). codex CLI 미실행.
"""
from __future__ import annotations

import importlib.util
import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from _git_fixture import init_git_repo

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")
PROJECT_NAME = "AcmeProj"

# 하니스별 어댑터 네임스페이스(채택자가 소비하는 산출물 트리) — 여기엔 미해소 토큰이 0 이어야 한다.
# 엔진(`.project_manager/**`)은 verbatim 사본이라 대상이 아니다(토큰-문서 보유가 정상).
HARNESS_ADAPTER_DIRS = {
    "claude": (".claude",),
    "opencode": (".opencode",),
    "codex": (".codex", ".agents"),
}

CODEX_AGENTS = ("architect", "code-reviewer", "developer", "researcher")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    mod = _load("pm_import")
    # hermetic — 라이브 `opencode models` CLI 미호출(미설치 동치).
    mod._real_models_runner = lambda: (False, [])
    return mod


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


def _import_new(pm_import, dest: Path, harness: str) -> None:
    """`--new` 로 채택자 인스턴스를 만든다(출력은 삼킴 — 테스트 로그 오염 방지)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pm_import.main(
            ["--new", str(dest), "--harness", harness,
             "--name", PROJECT_NAME, "--fill", "manual"]
        )
    assert rc == 0, f"{harness} import 실패(rc={rc}):\n{buf.getvalue()[-2000:]}"


def _token_leaks(dest: Path, dirs: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """어댑터 네임스페이스에 남은 리터럴 `{{...}}` 토큰 — (relpath, 토큰들) 목록."""
    leaks: list[tuple[str, list[str]]] = []
    for d in dirs:
        for path in sorted((dest / d).rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 바이너리·읽기불가 = 치환 대상 아님(제외 사유 ④)
            if "{{" in text:
                leaks.append((path.relative_to(dest).as_posix(),
                              sorted(set(TOKEN_RE.findall(text))) or ["{{...}}"]))
    return leaks


@pytest.fixture(scope="module")
def imported(pm_import, tmp_path_factory):
    """하니스 전수 fresh import 트리 (모듈 1회 — import 는 비싸다)."""
    root = tmp_path_factory.mktemp("adopters")
    trees: dict[str, Path] = {}
    for harness in HARNESS_ADAPTER_DIRS:
        dest = root / f"adopter-{harness}"
        _import_new(pm_import, dest, harness)
        trees[harness] = dest
    return trees


# ── (a) 하니스 전수 — 채택자 어댑터 트리에 토큰 잔존 0 ────────────────────────

@pytest.mark.parametrize("harness", sorted(HARNESS_ADAPTER_DIRS))
def test_imported_adapter_namespace_has_zero_tokens(imported, harness):
    """fresh import 한 **채택자 트리**의 어댑터 네임스페이스에 미해소 `{{` 가 0이다.

    기존 가드는 *템플릿에 토큰이 있다* 만 봤다 — 그래서 codex `.toml` 4개가 치환 없이 출하되는
    걸 아무도 못 봤다(T-0424). 여기서 보는 건 채택자가 실제로 받는 산출물이다."""
    leaks = _token_leaks(imported[harness], HARNESS_ADAPTER_DIRS[harness])
    assert not leaks, (
        f"{harness} 채택자 어댑터 트리에 미해소 토큰 잔존 — 치환 채널이 이 파일 형식을 "
        f"못 따라왔다(확장자 열거 재발 의심): {leaks}"
    )


# ── (b) codex `.toml` 에 실제 프로젝트명이 박힌다 ─────────────────────────────

@pytest.mark.parametrize("agent", CODEX_AGENTS)
def test_codex_agent_toml_carries_project_name(imported, agent):
    """`.codex/agents/<agent>.toml` 의 `{{PROJECT_NAME}}` 자리에 실제 프로젝트명이 박혀 있다."""
    path = imported["codex"] / ".codex" / "agents" / f"{agent}.toml"
    assert path.is_file(), f"codex agent TOML 미출하: {path}"
    text = path.read_text(encoding="utf-8")
    assert "{{PROJECT_NAME}}" not in text, (
        f"{agent}.toml 에 리터럴 토큰 잔존 — 치환 채널 미도달(T-0424 결함 재발).")
    assert PROJECT_NAME in text, (
        f"{agent}.toml 에 프로젝트명이 안 박힘 — 토큰이 조용히 비워졌는지 확인(T-0218).")


# ── (c) add-harness 경로 — render no-op 이라 sed 채널이 잡아야 한다 ───────────

def test_add_harness_codex_substitutes_toml_via_sed_channel(pm_import, tmp_path):
    """기존 claude 인스턴스에 `add_harness(dest, "codex")` → `.toml` 잔존 0 (sed 채널 단독 커버).

    이 경로의 dest manifest 는 claude flavor 라 `.codex/agents @render` 항목이 **없다** →
    `render_managed_files` 는 구조적으로 no-op 이고, sed 채널(`substitute_placeholders`)이
    유일한 커버다. import 경로(@render 실재)와 *다른 채널*로 같은 보장을 얻는지 못박는다."""
    dest = tmp_path / "live-instance"
    _import_new(pm_import, dest, "claude")

    manifest = (dest / ".project_manager" / "engine.manifest").read_text(encoding="utf-8")
    assert ".codex/agents" not in manifest, (
        "전제 붕괴 — claude flavor manifest 에 .codex/agents 항목이 생겼다면 이 테스트가 "
        "노리는 'render no-op' 경로가 아니다(테스트를 갱신하라).")

    buf = io.StringIO()
    with redirect_stdout(buf):
        pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    leaks = _token_leaks(dest, HARNESS_ADAPTER_DIRS["codex"])
    assert not leaks, f"add-harness codex 어댑터에 미해소 토큰 잔존(sed 채널 미커버): {leaks}"
    developer = (dest / ".codex" / "agents" / "developer.toml").read_text(encoding="utf-8")
    assert PROJECT_NAME in developer, (
        "add-harness 가 프로젝트명을 안 박음 — 라이브 인스턴스 local.conf 의 project_name 을 "
        "존중해 치환해야 한다.")


def test_import_codex_declares_toml_render_entry(imported):
    """import 경로는 반대로 `.codex/agents @render` 선언이 실재한다(두 경로의 채널이 다름).

    (c) 의 add-harness 경로가 sed 단독인 것과 대비 — codex flavor manifest 는 @render 를 선언하고,
    render 채널도 `.toml` 을 다뤄야 한다(아래 render_managed_files 단위 가드)."""
    manifest = (imported["codex"] / ".project_manager" / "engine.manifest").read_text(
        encoding="utf-8")
    assert re.search(r"^\.codex/agents\s+@render", manifest, re.M), (
        "codex flavor manifest 에 `.codex/agents @render` 선언이 없다 — 결함의 전제가 바뀌었다.")


def test_render_managed_files_covers_non_md_declared_paths(pm_import, tmp_path):
    """`render_managed_files` 는 확장자가 아니라 **@render 선언**으로만 대상을 고른다(T-0424).

    옛 `.md` 하드 필터는 manifest 선언을 덮는 중복·모순 판정이었다. `.toml` 산출물이 렌더되는지
    직접 확인한다(sed 채널을 거치지 않은 토큰-form 파일을 심어 render 채널만 태운다)."""
    dest = tmp_path / "inst"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(".codex/agents    @render\n", encoding="utf-8")
    agent = dest / ".codex" / "agents" / "developer.toml"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text('description = "{{PROJECT_NAME}} 구현 subagent"\n', encoding="utf-8")

    rel = Path(".codex/agents/developer.toml")
    changed = pm_import.render_managed_files(
        dest, {"{{PROJECT_NAME}}": PROJECT_NAME}, {rel})

    assert changed == 1, "render 채널이 @render 선언된 `.toml` 을 건너뛰었다(확장자 필터 재발)."
    assert agent.read_text(encoding="utf-8") == f'description = "{PROJECT_NAME} 구현 subagent"\n'


# ── (d) pm_update `@render` 재전파도 `.toml` 을 렌더한다 ──────────────────────

def test_pm_update_renders_toml_adapter(pm_update, tmp_path, monkeypatch, capsys):
    """채택자 self-update 의 `@render @source` 재전파가 `.toml` 을 byte-copy 로 새게 두지 않는다.

    sed 채널만 고치면 pm_update 가 다음 흡수에서 토큰-form 소스를 그대로 덮어써 결함이 되살아난다
    (재전파 경로·T-0424 DoD). `test_render_with_source_marker_renders_operational_tokens`
    (`.md`)의 codex TOML 대응."""
    adopter = tmp_path / "adopter"
    stored = tmp_path / "framework_root"
    src = stored / "templates" / "codex" / ".codex" / "agents" / "developer.toml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text('description = "{{PROJECT_NAME}} 구현 subagent"\n', encoding="utf-8")
    # 출하 인벤토리는 `git ls-files` 가 낸다 — source 트리가 자기 checkout 이라고 선언한다.
    init_git_repo(stored, commit="seed")

    dest_manifest = adopter / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    dest_manifest.write_text(
        ".codex/agents    @render @source=templates/codex/.codex/agents\n", encoding="utf-8")
    conf = adopter / ".project_manager" / "local.conf"
    conf.write_text(
        f"upstream.path={stored}\nproject.name={PROJECT_NAME}\n",
        encoding="utf-8")

    monkeypatch.setattr(pm_update, "REPO", adopter)
    rc = pm_update.main([])

    captured = capsys.readouterr()
    assert rc == 0, f"codex TOML @render 재전파 실패: {captured.err!r}"
    landed = adopter / ".codex" / "agents" / "developer.toml"
    assert landed.is_file(), "@render @source TOML 산출물이 dest 에 착지하지 않음."
    assert landed.read_text(encoding="utf-8") == (
        f'description = "{PROJECT_NAME} 구현 subagent"\n'), (
        "pm_update 가 TOML 을 렌더하지 않고 byte-copy 했다 — 재전파가 토큰을 되살린다.")
    assert "[render]" in captured.out, "TOML @render 항목이 byte-copy 로 오분기됐다."


# ── (e) 판정 역전 — 새 형식 자동 편입 / 엔진 자산은 계속 제외 ─────────────────

def test_substitute_suffix_allowlist_is_gone(pm_import):
    """확장자 allowlist 상수가 되살아나지 않는다 — 열린 집합 열거는 이 결함의 근본이다(T-0424)."""
    assert not hasattr(pm_import, "SUBSTITUTE_SUFFIXES"), (
        "SUBSTITUTE_SUFFIXES 재도입 — 치환 대상을 확장자로 열거하면 네 번째 하니스의 새 형식이 "
        "또 조용히 미커버로 남는다. 판정은 제외 사유 기반으로 유지하라(_should_substitute).")


@pytest.mark.parametrize("rel", [
    ".codex/agents/developer.toml",   # 세 번째 하니스가 들여온 형식(이 결함의 본체)
    ".claude/agents/developer.md",
    ".claude/settings.json",
    ".opencode/plugins/ctx-guard.js",
    "pm-config.sh",
    # 아직 없는 형식 — 네 번째 하니스가 들여와도 자동 편입돼야 한다(파생 판정의 teeth).
    ".future/agents/developer.yaml",
    ".future/config.jsonc",
])
def test_should_substitute_includes_any_adapter_format(pm_import, rel):
    """치환 대상 판정은 확장자를 묻지 않는다 — 제외 사유가 없으면 대상이다."""
    assert pm_import._should_substitute(Path(rel), frozenset()), (
        f"{rel} 이 치환 대상에서 빠졌다 — 확장자 열거가 되살아났는지 확인하라.")


@pytest.mark.parametrize("rel,why", [
    (".project_manager/tools/board.py", "엔진 소스는 verbatim(주석의 토큰은 *설명*)"),
    (".project_manager/tools/pm_import.py", "엔진 소스는 verbatim"),
    (".project_manager/engine.manifest", "엔진 메타데이터 주석이 토큰 메커니즘을 설명"),
])
def test_should_substitute_excludes_engine_assets(pm_import, rel, why):
    """엔진 소유 자산은 판정 역전 후에도 계속 제외된다(동작 무변경)."""
    assert not pm_import._should_substitute(Path(rel), frozenset()), (
        f"{rel} 이 치환 대상이 됐다 — {why}.")


@pytest.mark.parametrize("rel", [
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
])
def test_should_substitute_excludes_methodology_docs(pm_import, rel):
    """manifest 파생 치환-제외 집합(방법론 문서)은 그대로 존중된다(T-0329 무변경)."""
    exclude = frozenset({".project_manager/wiki/pm_role.md",
                         ".project_manager/wiki/pm_playbook.md"})
    assert not pm_import._should_substitute(Path(rel), exclude)


# ── (e-2) 채택자 산출물에서의 동일 무변경 확인 ────────────────────────────────

@pytest.mark.parametrize("harness", sorted(HARNESS_ADAPTER_DIRS))
def test_engine_python_source_stays_verbatim(imported, harness):
    """엔진 `.py` 사본은 판정 역전 후에도 토큰-문서를 그대로 갖는다(verbatim·T-0133)."""
    engine = imported[harness] / ".project_manager" / "tools" / "pm_import.py"
    assert "{{PROJECT_NAME}}" in engine.read_text(encoding="utf-8"), (
        "엔진 소스가 치환됐다 — 주석·docstring 의 토큰은 placeholder 가 아니라 문서다.")


def test_engine_manifest_token_comment_preserved(imported):
    """codex `engine.manifest` 주석의 토큰은 치환되지 않는다(메커니즘 설명 보존).

    판정 역전으로 `.manifest` 가 처음으로 치환 후보에 들어왔다 — 엔진 메타데이터 제외 사유가
    없으면 "developer_instructions 에 {{PROJECT_NAME}} 토큰 보유" 설명이 concrete 값으로 변질된다."""
    manifest = (imported["codex"] / ".project_manager" / "engine.manifest").read_text(
        encoding="utf-8")
    assert "{{PROJECT_NAME}}" in manifest, (
        "engine.manifest 주석이 치환됐다 — 토큰 메커니즘 *설명*이 concrete 값으로 변질된다.")


@pytest.mark.parametrize("harness", sorted(HARNESS_ADAPTER_DIRS))
def test_methodology_docs_keep_tokens_in_adopter_tree(imported, harness):
    """채택자 트리의 pm_role.md 도 토큰을 그대로 보존한다(T-0329 제외집합 무변경)."""
    pm_role = imported[harness] / ".project_manager" / "wiki" / "pm_role.md"
    assert pm_role.is_file(), f"{harness} 채택자에 pm_role.md 부재"
    assert "{{PROJECT_NAME}}" in pm_role.read_text(encoding="utf-8"), (
        "pm_role.md 가 오치환됐다 — 방법론 문서는 토큰을 *설명*으로 담는다(T-0329).")


def test_empty_sub_value_still_leaves_token(pm_import, tmp_path):
    """빈값 subs 는 판정 역전 후에도 치환하지 않는다(T-0218 무변경·silent-empty 근절)."""
    dest = tmp_path / "inst"
    target = dest / ".codex" / "agents" / "developer.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('description = "{{PROJECT_NAME}} subagent"\n', encoding="utf-8")

    changed = pm_import.substitute_placeholders(
        dest, {"{{PROJECT_NAME}}": ""}, {Path(".codex/agents/developer.toml")})

    assert changed == 0
    assert "{{PROJECT_NAME}}" in target.read_text(encoding="utf-8"), (
        "빈값이 토큰을 침묵 비움 — 미해소 탐지 신호가 사라진다(T-0218).")


def test_substitution_scope_stays_copied_relpaths(pm_import, tmp_path):
    """판정을 넓혀도 **범위**(이번 run 이 복사한 파일)는 절대 안 넓힌다(MF1 비파괴).

    채택자가 원래 갖고 있던 파일(복사 목록 밖)은 우연히 토큰을 담아도 불가침이다."""
    dest = tmp_path / "inst"
    untouched = dest / "user" / "notes.toml"
    untouched.parent.mkdir(parents=True, exist_ok=True)
    original = 'note = "{{PROJECT_NAME}} 은 내 파일의 문자열"\n'
    untouched.write_text(original, encoding="utf-8")

    copied = dest / ".codex" / "agents" / "developer.toml"
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_text('description = "{{PROJECT_NAME}}"\n', encoding="utf-8")

    changed = pm_import.substitute_placeholders(
        dest, {"{{PROJECT_NAME}}": PROJECT_NAME}, {Path(".codex/agents/developer.toml")})

    assert changed == 1
    assert untouched.read_text(encoding="utf-8") == original, (
        "복사 목록 밖 사용자 파일이 치환됐다 — copied_relpaths 비파괴 불변식 위반(MF1).")
