"""하네스 사용 한도·압축 비율 반영 (T-0923 AT-001~AT-011·AT-023 · 리뷰 F-001~F-003).

local.conf 의 하네스별 사용 한도 W(`harness.<이름>.ctx_window_tokens`)와 세 비율(쓴 %)로 하네스 설정
파일 세 종류에 압축 지점을 쓰는 엔진 함수(`pm_import.apply_harness_ctx` 와 그 부품)를 잰다.

입력은 **실제 출하 템플릿**(`templates/*/` 의 settings.json·config.toml·opencode.jsonc)과 그 변형이다 —
지어낸 설정 문서만으로 바이트 보존·고정점을 단언하지 않는다. opencode 전역 설정 폴더는 늘 `tmp_path`
아래 폴더를 `opencode_config_dir` 로 넘기고 실제 `~/.config/opencode` 를 읽지 않는다.

축:
  1. 검사 — 위반을 모두 모아 한 번에 거부한다(비율 순서·범위·하네스별 규칙).
  2. 목표값 — 압축 지점 P = floor(W × compact_at / 100), opencode reserved = W − P.
  3. 파일 형식별 반영 — 바뀌는 바이트는 관리 키 자리(없어서 넣는 멤버·쉼표 포함)뿐이고 멱등이다.
  4. opencode 대상 모델 — 전역 세 파일(읽기만)과 프로젝트 파일의 합집합, 출력 상한 우선순위.
  5. 이전 계획 — 남은 % → 쓴 %, 공통 한도 → 하네스별 한도.
  6. 보고 채널·원장 — 관리 키는 기능 표에서 빠지고, 무편집 증언은 반영 뒤에도 유지된다.
"""
from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import shutil
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES = REPO / "templates"
TEMPLATE_FILES = {
    "claude": (TEMPLATES / "claude_code" / ".claude" / "settings.json", ".claude/settings.json"),
    "codex": (TEMPLATES / "codex" / ".codex" / "config.toml", ".codex/config.toml"),
    "opencode": (TEMPLATES / "opencode" / ".opencode" / "opencode.jsonc",
                 ".opencode/opencode.jsonc"),
}
GLM = ("provider", "ollama", "models", "glm-5.2:cloud", "limit")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"harness_ctx_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


@pytest.fixture(scope="module")
def local_conf():
    return _load("local_conf")


def _project(tmp_path: Path, *, conf: str = "", harnesses=("claude", "codex", "opencode"),
             texts: dict[str, str] | None = None) -> Path:
    """출하 템플릿(또는 그 변형 텍스트)을 깐 프로젝트 트리 하나."""
    root = tmp_path / "project"
    (root / ".project_manager").mkdir(parents=True)
    (root / ".project_manager" / "local.conf").write_text(conf, encoding="utf-8")
    for harness in harnesses:
        template, relpath = TEMPLATE_FILES[harness]
        destination = root / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        if texts and relpath in texts:
            destination.write_bytes(texts[relpath].encode("utf-8"))
        else:
            shutil.copyfile(template, destination)
    return root


def _global_dir(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """opencode 전역 설정 폴더(테스트 소유) — 파일 이름 → 텍스트."""
    folder = tmp_path / "opencode-global"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {}).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def _apply(pm_import, root: Path, config_dir: Path, *, write: bool = True):
    return pm_import.apply_harness_ctx(root, write=write, opencode_config_dir=config_dir)


def _jsonc(pm_import, path: Path) -> dict:
    """엔진 JSONC 로더로 다시 읽는다(보고 채널이 쓰는 그 로더)."""
    return pm_import._load_adapter_config_document(path)


def _only_insertions(old: str, new: str) -> bool:
    """글자 단위로 old → new 가 **넣기만** 했는가(지우거나 바꾼 글자 0)."""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return all(tag in ("equal", "insert") for tag, *_ in matcher.get_opcodes())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── AT-001 · AT-002 검사 ─────────────────────────────────────────────────────


def test_validate_collects_order_and_range_violations(pm_import):
    """위반을 하나만 알리지 않는다 — 순서 위반과 100 미만 위반이 한 예외에 함께 실린다."""
    with pytest.raises(pm_import.HarnessCtxError) as caught:
        pm_import.validate_harness_ctx(
            {"ctx.checkpoint_at_pct": "80", "ctx.precompact_at_pct": "80",
             "ctx.compact_at_pct": "100"}, frozenset())
    violations = caught.value.violations
    assert len(violations) == 2, violations
    assert any("ctx.compact_at_pct=100" in line and "100 보다 작아야" in line
               for line in violations), violations
    assert any("ctx.checkpoint_at_pct=80" in line and "ctx.precompact_at_pct=80" in line
               and "작아야" in line for line in violations), violations
    assert all(line in str(caught.value) for line in violations)

    # 역방향 — 순서를 지킨 99 는 정상 값이다(Codex 가 없으면).
    pm_import.validate_harness_ctx(
        {"ctx.checkpoint_at_pct": "70", "ctx.precompact_at_pct": "80",
         "ctx.compact_at_pct": "99"}, frozenset({"claude", "opencode"}))
    pm_import.validate_harness_ctx({}, frozenset({"claude", "codex", "opencode"}))

    for key in ("ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct"):
        for bad in ("0", "100", "abc", "1.5"):
            with pytest.raises(pm_import.HarnessCtxError) as caught:
                pm_import.validate_harness_ctx({key: bad}, frozenset())
            assert any(f"`{key}=" in line for line in caught.value.violations), (key, bad)


def test_validate_harness_specific_limits(pm_import):
    """claude 압축 지점 범위·codex 95 규칙은 그 파일이 있을 때만, 한도 검사는 있는 하네스만."""
    ratios = {"ctx.checkpoint_at_pct": "30", "ctx.precompact_at_pct": "40",
              "ctx.compact_at_pct": "50"}
    claude = frozenset({"claude"})
    for window in ("199998", "2000002"):                  # P 99999 · 1000001
        with pytest.raises(pm_import.HarnessCtxError) as caught:
            pm_import.validate_harness_ctx(
                {**ratios, "harness.claude.ctx_window_tokens": window}, claude)
        assert any("Claude Code 압축 지점" in line for line in caught.value.violations)
    for window in ("200000", "2000000"):                  # P 100000 · 1000000 — 경계 포함
        pm_import.validate_harness_ctx(
            {**ratios, "harness.claude.ctx_window_tokens": window}, claude)

    codex = frozenset({"codex"})
    with pytest.raises(pm_import.HarnessCtxError) as caught:
        pm_import.validate_harness_ctx({"ctx.compact_at_pct": "95"}, codex)
    assert any("Codex" in line for line in caught.value.violations)
    pm_import.validate_harness_ctx({"ctx.compact_at_pct": "94"}, codex)
    # 역방향 — codex 파일이 없으면 95 는 거부 사유가 아니다.
    pm_import.validate_harness_ctx({"ctx.compact_at_pct": "95"}, frozenset({"claude", "opencode"}))

    for harness in ("claude", "codex", "opencode"):
        key = f"harness.{harness}.ctx_window_tokens"
        for bad in ("0", "-1", "abc", "1.5"):
            with pytest.raises(pm_import.HarnessCtxError) as caught:
                pm_import.validate_harness_ctx({key: bad}, frozenset({harness}))
            assert any(f"`{key}={bad}`" in line for line in caught.value.violations)
    # 역방향 — 설정 파일이 없는 하네스의 한도는 검사하지 않는다(그 값은 어디에도 쓰이지 않는다).
    pm_import.validate_harness_ctx({"harness.codex.ctx_window_tokens": "abc"}, claude)


# ── AT-003 목표값 ────────────────────────────────────────────────────────────


def test_targets_floor_and_reserved(pm_import):
    targets = pm_import.harness_ctx_targets({}, {("ollama", "glm-5.2:cloud"): 32768})

    assert targets[".claude/settings.json"] == {("autoCompactWindow",): 180000}
    assert targets[".codex/config.toml"] == {
        ("model_context_window",): 200000, ("model_auto_compact_token_limit",): 180000}
    assert targets[".opencode/opencode.jsonc"] == {
        (*GLM, "context"): 200000, (*GLM, "input"): 200000, (*GLM, "output"): 32768,
        ("compaction", "reserved"): 20000}

    odd = pm_import.harness_ctx_targets(
        {f"harness.{name}.ctx_window_tokens": "333333" for name in ("claude", "codex", "opencode")},
        {})
    assert odd[".claude/settings.json"][("autoCompactWindow",)] == 299999      # 내림(반올림 300000 아님)
    assert odd[".codex/config.toml"][("model_auto_compact_token_limit",)] == 299999
    assert odd[".opencode/opencode.jsonc"][("compaction", "reserved")] == 33334


# ── AT-004 · AT-005 · AT-006 파일 형식별 반영 ─────────────────────────────────


def test_apply_claude_settings_keeps_other_bytes(pm_import, tmp_path):
    """재직렬화하지 않는다 — 4칸 들여쓰기 파일에 키를 넣어도 나머지 바이트는 그대로다."""
    shipped = json.loads(TEMPLATE_FILES["claude"][0].read_text(encoding="utf-8"))
    shipped.pop("autoCompactWindow")
    four_space = json.dumps(shipped, ensure_ascii=False, indent=4) + "\n"
    root = _project(tmp_path, harnesses=("claude",),
                    texts={".claude/settings.json": four_space})
    config_dir = _global_dir(tmp_path)
    path = root / ".claude" / "settings.json"

    assert _apply(pm_import, root, config_dir) == (
        ".claude/settings.json autoCompactWindow (없음) → 180000",)
    after = path.read_text(encoding="utf-8")
    inserted = ',\n    "autoCompactWindow": 180000'
    assert after.count(inserted) == 1
    assert after.replace(inserted, "", 1) == four_space, "관리 키 밖 바이트가 바뀌었다"
    assert json.loads(after)["autoCompactWindow"] == 180000
    assert _apply(pm_import, root, config_dir) == (), "두 번째 반영이 또 바꿨다(멱등 아님)"

    # 값만 다른 기존 키 — 값 자리만 바뀐다(출하 템플릿 그대로의 2칸 파일).
    shipped_text = TEMPLATE_FILES["claude"][0].read_text(encoding="utf-8")
    root2 = _project(tmp_path / "second", harnesses=("claude",), conf="ctx.compact_at_pct=85\n")
    assert _apply(pm_import, root2, config_dir) == (
        ".claude/settings.json autoCompactWindow 180000 → 170000",)
    assert (root2 / ".claude" / "settings.json").read_text(encoding="utf-8") == shipped_text.replace(
        '"autoCompactWindow": 180000', '"autoCompactWindow": 170000', 1)
    assert _apply(pm_import, root2, config_dir) == ()


def test_apply_codex_config_top_level_keys(pm_import, tmp_path):
    """두 키는 최상위에 들어간다 — 첫 표 뒤에 넣으면 `[features]` 아래 키가 된다."""
    shipped = TEMPLATE_FILES["codex"][0].read_text(encoding="utf-8")
    managed = "model_context_window = 200000\nmodel_auto_compact_token_limit = 180000\n"
    assert shipped.count(managed) == 1
    # 앞 세대 채택자 파일: 한도 키 하나(900000)만 있고 뒤에 표가 하나 더 있다.
    previous = shipped.replace(managed, "model_auto_compact_token_limit = 900000\n", 1) + (
        "\n[tui]\n# 알림 표시\nnotifications = true\n")
    root = _project(tmp_path, harnesses=("codex",), texts={".codex/config.toml": previous})
    config_dir = _global_dir(tmp_path)
    path = root / ".codex" / "config.toml"

    lines = _apply(pm_import, root, config_dir)
    assert lines == (".codex/config.toml model_context_window (없음) → 200000",
                     ".codex/config.toml model_auto_compact_token_limit 900000 → 180000")
    after = path.read_text(encoding="utf-8")
    document = tomllib.loads(after)
    assert document["model_context_window"] == 200000
    assert document["model_auto_compact_token_limit"] == 180000
    assert "model_context_window" not in document["features"]
    assert "model_context_window" not in document["tui"]
    assert after == previous.replace(
        "model_auto_compact_token_limit = 900000\n",
        "model_auto_compact_token_limit = 180000\nmodel_context_window = 200000\n", 1), (
        "값 자리·넣은 한 줄 밖의 바이트(주석 포함)가 바뀌었다")
    comments = [line for line in previous.splitlines() if line.lstrip().startswith("#")]
    assert [line for line in after.splitlines() if line.lstrip().startswith("#")] == comments
    assert _apply(pm_import, root, config_dir) == ()


def test_apply_codex_config_crlf_and_quoted_table_headers(pm_import, tmp_path):
    """표 머리 판정은 TOML 키 문법과 CRLF 줄 끝을 따른다 (T-0923 리뷰 F-001).

    CRLF 로 저장한 앞 세대 파일(한도 키 하나 · 900000)에서도 두 키가 최상위에 들어가고, 넣은 줄도
    CRLF 다. 인용 키에 `/`·`@`·`:`·한글이 든 표 머리는 표의 시작이다 — 그 표 안의 같은 이름 키는
    관리 키가 아니므로 바뀌지 않는다."""
    shipped = TEMPLATE_FILES["codex"][0].read_text(encoding="utf-8")
    managed = "model_context_window = 200000\nmodel_auto_compact_token_limit = 180000\n"
    previous = shipped.replace(managed, "model_auto_compact_token_limit = 900000\n", 1)
    config_dir = _global_dir(tmp_path)

    crlf = previous.replace("\n", "\r\n")
    root = _project(tmp_path / "crlf", harnesses=("codex",), texts={".codex/config.toml": crlf})
    path = root / ".codex" / "config.toml"
    assert _apply(pm_import, root, config_dir) == (
        ".codex/config.toml model_context_window (없음) → 200000",
        ".codex/config.toml model_auto_compact_token_limit 900000 → 180000")
    after = path.read_bytes().decode("utf-8")
    assert after == crlf.replace(
        "model_auto_compact_token_limit = 900000\r\n",
        "model_auto_compact_token_limit = 180000\r\nmodel_context_window = 200000\r\n", 1), (
        "값 자리·넣은 한 줄 밖의 바이트가 바뀌었거나 넣은 줄이 CRLF 가 아니다")
    document = tomllib.loads(after)
    assert (document["model_context_window"], document["model_auto_compact_token_limit"]) == (
        200000, 180000)
    assert all("model_context_window" not in table
               for table in document.values() if isinstance(table, dict))
    assert _apply(pm_import, root, config_dir) == ()

    for index, header in enumerate(('[profiles."team/a"]', "[profiles.'ops@host:1']",
                                    '[ profiles . "한글 팀" ]')):
        table = f"{header}\nmodel_context_window = 128000\n\n[features]\n"
        # 최상위 키가 이미 목표값이면 파일은 그대로다 — 표 안의 128000 을 최상위로 읽지 않는다.
        current = shipped.replace("[features]\n", table, 1)
        root = _project(tmp_path / f"quoted-{index}", harnesses=("codex",),
                        texts={".codex/config.toml": current})
        assert _apply(pm_import, root, config_dir) == (), header
        assert (root / ".codex" / "config.toml").read_text(encoding="utf-8") == current, header
        # 최상위 키가 없으면 그 표 머리 앞에 넣는다.
        old_generation = previous.replace("[features]\n", table, 1)
        root = _project(tmp_path / f"quoted-previous-{index}", harnesses=("codex",),
                        texts={".codex/config.toml": old_generation})
        _apply(pm_import, root, config_dir)
        after = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert after == old_generation.replace(
            "model_auto_compact_token_limit = 900000\n",
            "model_auto_compact_token_limit = 180000\nmodel_context_window = 200000\n", 1), header
        profile = next(iter(tomllib.loads(after)["profiles"].values()))
        assert profile == {"model_context_window": 128000}, header


def test_apply_opencode_writes_limits_for_every_config_model(pm_import, tmp_path):
    """설정 파일에 적힌 모델 전부 — 전역에만 있는 모델도 프로젝트 파일에 `limit` 이 생긴다."""
    config_dir = _global_dir(tmp_path, {
        "opencode.jsonc": (
            '{\n  // 로컬 모델\n  "provider": {\n    "ollama": {\n      "models": {\n'
            '        "qwen3.8:27b-unc": { "name": "qwen", "options": { "num_ctx": 262144 } }\n'
            '      }\n    }\n  }\n}\n'),
        "opencode.json": json.dumps({"provider": {"openrouter": {"models": {
            "anthropic/claude-x": {"limit": {"context": 128000, "output": 16000}}}}}}, indent=2),
    })
    global_before = {name: _sha(config_dir / name) for name in ("opencode.jsonc", "opencode.json")}
    root = _project(tmp_path, harnesses=("opencode",))
    path = root / ".opencode" / "opencode.jsonc"
    before = path.read_text(encoding="utf-8")

    lines = _apply(pm_import, root, config_dir)

    after = path.read_text(encoding="utf-8")
    assert _only_insertions(before, after), "주석·다른 멤버 바이트를 지우거나 바꿨다"
    document = _jsonc(pm_import, path)
    ollama = document["provider"]["ollama"]["models"]
    assert ollama["glm-5.2:cloud"]["limit"] == {"context": 200000, "input": 200000, "output": 32768}
    assert ollama["qwen3.8:27b-unc"]["limit"] == {"context": 200000, "input": 200000, "output": 32000}
    openrouter = document["provider"]["openrouter"]["models"]
    assert openrouter["anthropic/claude-x"]["limit"] == {
        "context": 200000, "input": 200000, "output": 16000}
    assert set(openrouter) == {"anthropic/claude-x"}, "모델 id 를 슬래시에서 갈랐다"
    assert document["compaction"]["reserved"] == 20000
    for provider in document["provider"].values():
        for model in provider["models"].values():
            if "limit" in model:
                assert {"context", "output"} <= set(model["limit"]), model
    assert '.opencode/opencode.jsonc provider.ollama.models."qwen3.8:27b-unc".limit.context (없음) → 200000' in lines
    assert ".opencode/opencode.jsonc provider.openrouter.models.anthropic/claude-x.limit.output (없음) → 16000" in lines
    assert {name: _sha(config_dir / name) for name in global_before} == global_before, (
        "opencode 전역 설정을 썼다")
    assert _apply(pm_import, root, config_dir) == ()


# ── AT-007 전역 폴더 해소·주입·격리 ─────────────────────────────────────────


def test_opencode_global_dir_resolution_and_isolation(pm_import, tmp_path, monkeypatch,
                                                      tmp_path_factory):
    resolve = pm_import.opencode_global_config_dir
    assert resolve({"XDG_CONFIG_HOME": "/cfg"}, Path("/h")) == Path("/cfg/opencode")
    assert resolve({"XDG_CONFIG_HOME": ""}, Path("/h")) == Path("/h/.config/opencode")
    assert resolve({}, Path("/h")) == Path("/h/.config/opencode")
    assert resolve({"OPENCODE_CONFIG_DIR": "/elsewhere"}, Path("/h")) == Path("/h/.config/opencode")

    # 주입한 폴더만 읽는다 — HOME 아래 전역 설정은 보지 않는다.
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.jsonc").write_text(
        '{"provider": {"ollama": {"models": {"home-model": {}}}}}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    empty = _global_dir(tmp_path)
    root = _project(tmp_path, harnesses=("opencode",))
    _apply(pm_import, root, empty)
    assert "home-model" not in (root / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8")

    # `harness.opencode.pro_model` 은 반영 입력이 아니다 — 있든 없든 결과가 같다.
    outcomes = []
    for index, conf in enumerate(("ctx.compact_at_pct=85\n",
                                  "ctx.compact_at_pct=85\nharness.opencode.pro_model=ollama/nothing\n")):
        project = _project(tmp_path / f"pro-{index}", harnesses=("opencode",), conf=conf)
        lines = _apply(pm_import, project, empty)
        outcomes.append((lines, (project / ".opencode" / "opencode.jsonc").read_bytes()))
    assert outcomes[0] == outcomes[1]
    assert not pm_import.is_harness_ctx_input_key("harness.opencode.pro_model")
    for key in ("ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct",
                "harness.claude.ctx_window_tokens", "harness.codex.ctx_window_tokens",
                "harness.opencode.ctx_window_tokens"):
        assert pm_import.is_harness_ctx_input_key(key), key

    # 스위트 격리 — 이 프로세스(와 자식)가 보는 전역 설정은 pytest 임시 루트 아래 빈 폴더다.
    base = tmp_path_factory.getbasetemp().resolve()
    isolated = Path(os.environ["XDG_CONFIG_HOME"]).resolve()
    assert base in isolated.parents, isolated
    global_dir = resolve(os.environ, Path.home())
    assert not any((global_dir / name).exists() for name in pm_import.OPENCODE_GLOBAL_CONFIG_NAMES)


# ── AT-008 템플릿 고정점 ─────────────────────────────────────────────────────


def test_shipped_templates_are_fixed_points_of_apply(pm_import, tmp_path):
    root = _project(tmp_path)
    config_dir = _global_dir(tmp_path)
    before = {relpath: (root / relpath).read_bytes() for _template, relpath in TEMPLATE_FILES.values()}

    assert _apply(pm_import, root, config_dir, write=False) == ()
    assert _apply(pm_import, root, config_dir) == ()
    assert {relpath: (root / relpath).read_bytes() for relpath in before} == before

    document = _jsonc(pm_import, TEMPLATE_FILES["opencode"][0])
    assert document["provider"]["ollama"]["models"]["glm-5.2:cloud"]["limit"] == {
        "context": 200000, "input": 200000, "output": 32768}
    assert document["compaction"] == {"reserved": 20000}
    assert tomllib.loads(TEMPLATE_FILES["codex"][0].read_text(encoding="utf-8"))[
        "model_auto_compact_token_limit"] == 180000
    assert json.loads(TEMPLATE_FILES["claude"][0].read_text(encoding="utf-8"))[
        "autoCompactWindow"] == 180000


# ── AT-009 · AT-010 이전 계획 ────────────────────────────────────────────────


def _plan(local_conf, tmp_path: Path, text: str) -> dict:
    path = tmp_path / "local.conf"
    path.write_text(text, encoding="utf-8")
    return local_conf.rename_updates(local_conf.load(path))


def test_rename_updates_turns_remaining_into_used(local_conf, tmp_path):
    assert _plan(local_conf, tmp_path, "ctx.checkpoint_pct=30\nctx.precompact_pct=20\n") == {
        "ctx.checkpoint_pct": None, "ctx.checkpoint_at_pct": "70",
        "ctx.precompact_pct": None, "ctx.precompact_at_pct": "80"}
    assert _plan(local_conf, tmp_path, "ctx_nudge_pct=25\nctx.stop_pct=15\n") == {
        "ctx_nudge_pct": None, "ctx.checkpoint_at_pct": "75",
        "ctx.stop_pct": None, "ctx.precompact_at_pct": "85"}
    # 새 키에 값이 있으면 옛 줄만 지운다(옛 값이 새 값을 덮지 않는다).
    assert _plan(local_conf, tmp_path, "ctx.checkpoint_pct=30\nctx.checkpoint_at_pct=60\n") == {
        "ctx.checkpoint_pct": None}
    # 기본값과 같은 값을 새로 쓰는 항목은 없다 — 옛 키가 없으면 새 줄도 없다.
    assert _plan(local_conf, tmp_path, "test.cmd=pytest\n") == {}
    with pytest.raises(ValueError, match="ctx.checkpoint_pct=abc"):
        _plan(local_conf, tmp_path, "ctx.checkpoint_pct=abc\n")


def test_rename_updates_splits_common_window_per_harness(local_conf, tmp_path):
    plan = _plan(local_conf, tmp_path,
                 "ctx.window_tokens=300000\nharness.claude.ctx_window_tokens=1000000\n")
    assert plan == {"ctx.window_tokens": None,
                    "harness.codex.ctx_window_tokens": "300000",
                    "harness.opencode.ctx_window_tokens": "300000"}, "claude 명시값을 덮었다"
    plan = _plan(local_conf, tmp_path, "ctx_window_tokens_codex=250000\nctx_window_tokens=300000\n")
    assert plan["harness.codex.ctx_window_tokens"] == "250000", "하네스 전용 구키가 져서는 안 된다"
    assert plan["harness.claude.ctx_window_tokens"] == "300000"
    assert plan["harness.opencode.ctx_window_tokens"] == "300000"

    # 하네스 목록을 못 읽는 사본 — 옛 줄만 지우고 값을 잃지 않는다(멈춘다).
    lonely = tmp_path / "lonely-tools"
    lonely.mkdir()
    shutil.copyfile(TOOLS / "local_conf.py", lonely / "local_conf.py")
    spec = importlib.util.spec_from_file_location("harness_ctx_lonely_conf", lonely / "local_conf.py")
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert isolated._harness_names() == ()
    with pytest.raises(ValueError, match="등록 하네스 목록"):
        _plan(isolated, tmp_path, "ctx.window_tokens=300000\n")


def test_rename_updates_keeps_explicit_new_key_over_non_integer_old(local_conf, tmp_path):
    """새 키에 값이 있으면 옛 줄만 지운다 — 버려질 옛 값은 정수인지 보지 않는다 (T-0923 리뷰 F-003)."""
    assert _plan(local_conf, tmp_path, "ctx.checkpoint_pct=abc\nctx.checkpoint_at_pct=60\n") == {
        "ctx.checkpoint_pct": None}
    assert _plan(local_conf, tmp_path, "ctx_stop_pct=15%\nctx.precompact_at_pct=85\n") == {
        "ctx_stop_pct": None}
    # 새 키가 없으면 옮겨야 하는 값이라 여전히 키 이름을 담아 멈춘다(지어낸 값으로 옮기지 않는다).
    with pytest.raises(ValueError, match="ctx.checkpoint_pct=abc"):
        _plan(local_conf, tmp_path, "ctx.checkpoint_pct=abc\n")


# ── AT-011 보고 채널·원장 ────────────────────────────────────────────────────


def _ledger(root: Path, relpath: str, digest: str) -> Path:
    path = root / ".project_manager" / "adapter_baseline.json"
    path.write_text(json.dumps({"schema": 1, "files": {relpath: {
        "sha256": digest, "recorded_at": "2026-09-01T00:00:00+09:00",
        "template_rev": "rev-at-install"}}}), encoding="utf-8")
    return path


def test_report_skips_managed_keys_and_ledger_follows_apply(pm_import, tmp_path):
    assert "model_auto_compact_token_limit" not in pm_import.ADAPTER_SETTINGS_KEYS[".codex/config.toml"]

    latest = _jsonc(pm_import, TEMPLATE_FILES["opencode"][0])
    project = json.loads(json.dumps(latest))
    project["provider"]["ollama"]["models"]["glm-5.2:cloud"]["limit"]["context"] = 50000
    project["provider"]["ollama"]["models"]["qwen"] = {"limit": {"context": 1, "output": 1}}
    project["provider"]["lmstudio"] = {"models": {"x": {"limit": {"context": 1, "output": 1}}}}
    rows = pm_import._adapter_feature_rows(".opencode/opencode.jsonc", project, latest)
    assert all(row.verdict == "same" for row in rows if row.name == "provider"), rows
    # 모델 항목에 limit 밖 값이 있는 변형 — 행이 서고 same 이다.
    options = {"baseURL": "http://localhost:11434/v1"}
    latest_with_options = json.loads(json.dumps(latest))
    latest_with_options["provider"]["ollama"]["options"] = dict(options)
    project_with_options = json.loads(json.dumps(project))
    project_with_options["provider"]["ollama"]["options"] = dict(options)
    provider_rows = [row for row in pm_import._adapter_feature_rows(
        ".opencode/opencode.jsonc", project_with_options, latest_with_options)
        if row.name == "provider"]
    assert [row.verdict for row in provider_rows] == ["same"]
    project_with_options["provider"]["ollama"]["options"]["baseURL"] = "http://other/v1"
    provider_rows = [row for row in pm_import._adapter_feature_rows(
        ".opencode/opencode.jsonc", project_with_options, latest_with_options)
        if row.name == "provider"]
    assert [row.verdict for row in provider_rows] == ["differs"]

    # 원장이 증언하던 파일 — 반영 뒤 sha256 만 새 내용 해시, template_rev·recorded_at 은 그대로.
    root = _project(tmp_path, harnesses=("claude",), conf="ctx.compact_at_pct=85\n")
    settings = root / ".claude" / "settings.json"
    ledger = _ledger(root, ".claude/settings.json", pm_import.content_sha256(settings))
    _apply(pm_import, root, _global_dir(tmp_path))
    entry = json.loads(ledger.read_text(encoding="utf-8"))["files"][".claude/settings.json"]
    assert entry == {"sha256": pm_import.content_sha256(settings),
                     "recorded_at": "2026-09-01T00:00:00+09:00", "template_rev": "rev-at-install"}
    judged = pm_import.judge_adapter_configs(root, REPO, harnesses=["claude"])
    assert [(item.relpath, item.status) for item in judged] == [(".claude/settings.json", "unedited")]

    # 편집된 파일 — 원장 항목을 건드리지 않는다.
    edited_root = _project(tmp_path / "edited", harnesses=("claude",), conf="ctx.compact_at_pct=85\n")
    edited_ledger = _ledger(edited_root, ".claude/settings.json", "0" * 64)
    ledger_before = edited_ledger.read_bytes()
    _apply(pm_import, edited_root, _global_dir(tmp_path))
    assert edited_ledger.read_bytes() == ledger_before


def test_partial_write_failure_keeps_ledger_for_written_files(pm_import, tmp_path, monkeypatch):
    """한 파일을 교체하지 못해도 이미 교체한 파일의 원장은 기록한다 (T-0923 리뷰 F-002).

    원장이 무편집을 증언하던 settings.json 은 교체 뒤에도 무편집으로 판정되고(편집됨이면 세트 수용에서
    빠진다), 예외 문구는 교체한 파일과 못 한 파일을 나눠 적는다. 못 쓴 config.toml 과 그 원장 항목은
    그대로이고, 쓰기가 되살아난 다음 반영이 남은 파일만 바꾼다."""
    root = _project(tmp_path, harnesses=("claude", "codex"), conf="ctx.compact_at_pct=85\n")
    settings, config = root / ".claude" / "settings.json", root / ".codex" / "config.toml"
    ledger = root / ".project_manager" / "adapter_baseline.json"
    recorded = {"recorded_at": "2026-09-01T00:00:00+09:00", "template_rev": "rev-at-install"}
    ledger.write_text(json.dumps({"schema": 1, "files": {
        relpath: {"sha256": pm_import.content_sha256(root / relpath), **recorded}
        for relpath in (".claude/settings.json", ".codex/config.toml")}}), encoding="utf-8")
    config_before = config.read_bytes()
    config_entry = json.loads(ledger.read_text(encoding="utf-8"))["files"][".codex/config.toml"]
    replace = pm_import._atomic_write_dest_bytes

    def codex_folder_is_read_only(dest_root, rel, payload, **kwargs):
        if Path(rel).as_posix() == ".codex/config.toml":
            return False                    # 엔진 교체 함수의 실패 답(원본은 손대지 않음)
        return replace(dest_root, rel, payload, **kwargs)

    monkeypatch.setattr(pm_import, "_atomic_write_dest_bytes", codex_folder_is_read_only)
    with pytest.raises(pm_import.HarnessCtxError) as caught:
        _apply(pm_import, root, _global_dir(tmp_path))
    message = str(caught.value)
    assert message.startswith("사용 한도·압축 비율을 .claude/settings.json 에 반영한 뒤 멈췄습니다"), message
    assert ".codex/config.toml 를 쓰지 못했습니다" in message and "반영하지 않았습니다" not in message

    assert json.loads(settings.read_text(encoding="utf-8"))["autoCompactWindow"] == 170000
    entries = json.loads(ledger.read_text(encoding="utf-8"))["files"]
    assert entries[".claude/settings.json"] == {"sha256": pm_import.content_sha256(settings), **recorded}
    assert config.read_bytes() == config_before and entries[".codex/config.toml"] == config_entry
    judged = {item.relpath: item.status
              for item in pm_import.judge_adapter_configs(root, REPO, harnesses=["claude", "codex"])}
    assert judged[".claude/settings.json"] == "unedited", judged

    monkeypatch.setattr(pm_import, "_atomic_write_dest_bytes", replace)
    assert _apply(pm_import, root, _global_dir(tmp_path)) == (
        ".codex/config.toml model_auto_compact_token_limit 180000 → 170000",)
    judged = {item.relpath: item.status
              for item in pm_import.judge_adapter_configs(root, REPO, harnesses=["claude", "codex"])}
    assert (judged[".claude/settings.json"], judged[".codex/config.toml"]) == ("unedited", "unedited")


# ── AT-023 opencode 출력 상한 우선순위·전역 읽기 실패 ─────────────────────────


def _model_limit(pm_import, root: Path) -> dict:
    return _jsonc(pm_import, root / ".opencode" / "opencode.jsonc")[
        "provider"]["ollama"]["models"]["m"]["limit"]


def test_opencode_output_precedence_and_global_read_errors(pm_import, tmp_path):
    config_dir = _global_dir(tmp_path, {
        "config.json": '{"provider": {"ollama": {"models": {"m": {"limit": '
                       '{"context": 9, "output": 1000}}}}}}',
        # 주석·후행 쉼표가 든 opencode.json — opencode 는 세 이름 모두 JSONC 로 읽는다.
        "opencode.json": '{\n  // 뒤 파일이 이긴다\n  "provider": {"ollama": {"models": {"m": '
                         '{"limit": {"context": 9, "output": 2000,},},},},},\n}\n',
        "opencode.jsonc": '{"provider": {"ollama": {"models": {"m": {"name": "m"}}}}}',
    })
    root = _project(tmp_path / "global-wins", harnesses=("opencode",))
    _apply(pm_import, root, config_dir)
    assert _model_limit(pm_import, root)["output"] == 2000

    shipped = TEMPLATE_FILES["opencode"][0].read_text(encoding="utf-8")
    with_project_output = shipped.replace(
        '"glm-5.2:cloud": {',
        '"m": {\n          "limit": {\n            "context": 1,\n            "output": 3000\n'
        '          }\n        },\n        "glm-5.2:cloud": {', 1)
    root = _project(tmp_path / "project-wins", harnesses=("opencode",),
                    texts={".opencode/opencode.jsonc": with_project_output})
    _apply(pm_import, root, config_dir)
    assert _model_limit(pm_import, root)["output"] == 3000

    root = _project(tmp_path / "nothing", harnesses=("opencode",))
    _apply(pm_import, root, _global_dir(tmp_path / "only-name", {
        "opencode.jsonc": '{"provider": {"ollama": {"models": {"m": {}}}}}'}))
    assert _model_limit(pm_import, root)["output"] == 32000

    # 전역 파일을 풀지 못하거나 가져다 쓸 값이 정수가 아니면 멈춘다 — 빈 전역으로 반영하지 않는다.
    for folder, files in (
            ("broken", {"opencode.json": '{"provider": {"ollama": '}),
            ("string", {"opencode.json": '{"provider": {"ollama": {"models": {"m": '
                                         '{"limit": {"context": 9, "output": "big"}}}}}}'})):
        broken_dir = _global_dir(tmp_path / folder, files)
        root = _project(tmp_path / f"stop-{folder}", conf="ctx.compact_at_pct=85\n")
        _ledger(root, ".claude/settings.json", pm_import.content_sha256(root / ".claude/settings.json"))
        watched = [root / relpath for _template, relpath in TEMPLATE_FILES.values()]
        watched.append(root / ".project_manager" / "adapter_baseline.json")
        before = {path: path.read_bytes() for path in watched}
        with pytest.raises(pm_import.HarnessCtxError) as caught:
            _apply(pm_import, root, broken_dir)
        assert str(broken_dir / "opencode.json") in str(caught.value), caught.value
        assert {path: path.read_bytes() for path in watched} == before, folder


def test_object_slots_are_checked_only_in_opencode_documents(pm_import, tmp_path):
    """객체 자리 검사(`provider`·`compaction` …)는 opencode 문서에만 건다.

    claude 설정에 같은 이름의 키가 있어도 반영을 막지 않고, opencode 전역 문서의 `provider` 가 객체가
    아니면 그 파일과 키를 지목해 멈춘다(어느 파일도 쓰지 않는다)."""
    shipped = TEMPLATE_FILES["claude"][0].read_text(encoding="utf-8")
    claude_text = shipped.replace("{\n", '{\n  "provider": "anthropic",\n', 1)
    root = _project(tmp_path / "claude", harnesses=("claude",), conf="ctx.compact_at_pct=85\n",
                    texts={".claude/settings.json": claude_text})
    _apply(pm_import, root, _global_dir(tmp_path))
    assert _jsonc(pm_import, root / ".claude" / "settings.json")["autoCompactWindow"] == 170000

    broken_dir = _global_dir(tmp_path / "non-object", {"opencode.json": '{"provider": "x"}'})
    root = _project(tmp_path / "opencode", harnesses=("opencode",))
    before = (root / ".opencode" / "opencode.jsonc").read_bytes()
    with pytest.raises(pm_import.HarnessCtxError) as caught:
        _apply(pm_import, root, broken_dir)
    assert f"{broken_dir / 'opencode.json'} 의 `provider` 가 객체가 아닙니다" in str(caught.value)
    assert (root / ".opencode" / "opencode.jsonc").read_bytes() == before
