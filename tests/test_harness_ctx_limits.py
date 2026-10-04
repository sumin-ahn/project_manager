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
    (root / ".project_manager/independent-runtime-fixture.json").write_text('{"generation":"fixture-generation","usable":200000}',encoding="utf-8")
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


def _snapshot_fixture_bytes(root, *, excluded_names=()):
    """격리 fixture 바이트를 기존 열거 seam으로 전수 확인한다."""
    repo_files = _load('repo_owned_files')
    entries = repo_files.list_repo_owned_entries(
        root, '.', mode=repo_files.OWNED,
        git_runner=lambda _args: (127, 'isolated fixture, filesystem oracle'),
    )
    return {entry.path.as_posix(): (root / entry.path).read_bytes()
            for entry in entries if (root / entry.path).is_file()
            and entry.path.name not in excluded_names}


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
    # 입력 검사는 W만 검증한다. native P 범위는 C를 적용한 계획에서 검사한다.
    for window in ("199998", "2000002", "200000", "2000000"):
        pm_import.validate_harness_ctx({"harness.claude.ctx_window_tokens": window}, frozenset({"claude"}))
    pm_import.validate_harness_ctx({"ctx.compact_at_pct": "95"}, frozenset({"codex"}))

    for harness in ("claude", "codex", "opencode"):
        key = f"harness.{harness}.ctx_window_tokens"
        for bad in ("0", "-1", "abc", "1.5"):
            with pytest.raises(pm_import.HarnessCtxError) as caught:
                pm_import.validate_harness_ctx({key: bad}, frozenset({harness}))
            assert any(f"`{key}={bad}`" in line for line in caught.value.violations)
    # 역방향 — 설정 파일이 없는 하네스의 한도는 검사하지 않는다(그 값은 어디에도 쓰이지 않는다).
    pm_import.validate_harness_ctx({"harness.codex.ctx_window_tokens": "abc"}, frozenset({"claude"}))


# ── AT-003 목표값 ────────────────────────────────────────────────────────────


def test_targets_floor_and_reserved(pm_import,local_conf):
    def budgets(window):
        rows = {}
        for h in ('claude','codex','opencode'):
            identity={'model':'glm-5.2:cloud','provider':'ollama','profile':''}
            metadata={**_t0932_metadata(identity,400000),'context':1000000,'output':128000,'effective_output':32000,'native_formula':'input-reserved'}
            row=local_conf.resolve_ctx_capacity(window,(70,80,90),identity,metadata)
            rows[h]={**row,'models':[row]}
        return rows
    targets = pm_import.harness_ctx_targets({}, {("ollama", "glm-5.2:cloud"): 32768}, budgets(200000))

    assert targets[".claude/settings.json"] == {("autoCompactWindow",): 180000}
    assert targets[".codex/config.toml"] == {
        ("model_auto_compact_token_limit",): 180000}
    assert targets[".opencode/opencode.jsonc"] == {
        (*GLM, "context"): 1000000, (*GLM, "input"): 200000, (*GLM, "output"): 32768,
        ("compaction", "reserved"): 20000}

    odd = pm_import.harness_ctx_targets(
        {f"harness.{name}.ctx_window_tokens": "333333" for name in ("claude", "codex", "opencode")},
        {},budgets(333333))
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
    managed = "model_auto_compact_token_limit = 180000\n"
    assert shipped.count(managed) == 1
    # 앞 세대 채택자 파일: 한도 키 하나(900000)만 있고 뒤에 표가 하나 더 있다.
    previous = shipped.replace(managed, "model_auto_compact_token_limit = 900000\n", 1) + (
        "\n[tui]\n# 알림 표시\nnotifications = true\n")
    root = _project(tmp_path, harnesses=("codex",), texts={".codex/config.toml": previous})
    config_dir = _global_dir(tmp_path)
    path = root / ".codex" / "config.toml"

    lines = _apply(pm_import, root, config_dir)
    assert lines == (".codex/config.toml model_auto_compact_token_limit 900000 → 180000",)
    after = path.read_text(encoding="utf-8")
    document = tomllib.loads(after)
    assert "model_context_window" not in document
    assert document["model_auto_compact_token_limit"] == 180000
    assert "model_context_window" not in document["features"]
    assert "model_context_window" not in document["tui"]
    assert after == previous.replace(
        "model_auto_compact_token_limit = 900000\n",
        "model_auto_compact_token_limit = 180000\n", 1), (
        "값 자리·넣은 한 줄 밖의 바이트(주석 포함)가 바뀌었다")
    comments = [line for line in previous.splitlines() if line.lstrip().startswith("#")]
    assert [line for line in after.splitlines() if line.lstrip().startswith("#")] == comments
    assert _apply(pm_import, root, config_dir) == ()


def test_toml_bracket_lines_inside_multiline_values_are_not_the_first_table(pm_import):
    """여러 줄 문자열·배열 안의 `[이름]` 꼴 줄은 표 머리가 아니다 — 관리 키를 그 값 안에 넣지 않는다.

    재현(2026-09-30 T-0923 수정 라운드 발견): 최상위 `notes = \"\"\"…\"\"\"` 안에 `[memo]` 줄이 있으면
    그 줄을 첫 표 머리로 보아 키 줄을 문자열 안에 넣었고, 반영할 때마다 다시 넣었다."""
    text = 'notes = """\n[memo]\n"""\nids = [\n  [3],\n  [4]\n]\n[features]\nhooks = true\n'
    new, old = pm_import._toml_set_top_level_integer(text, "model_context_window", 200000)
    assert old is None
    doc = tomllib.loads(new)
    assert doc["notes"] == "[memo]\n"
    assert doc["ids"] == [[3], [4]]
    assert doc["model_context_window"] == 200000
    assert "model_context_window" not in doc["features"]
    again, again_old = pm_import._toml_set_top_level_integer(new, "model_context_window", 200000)
    assert (again, again_old) == (new, "200000")


def test_apply_codex_config_crlf_and_quoted_table_headers(pm_import, tmp_path):
    """표 머리 판정은 TOML 키 문법과 CRLF 줄 끝을 따른다 (T-0923 리뷰 F-001).

    CRLF 로 저장한 앞 세대 파일(한도 키 하나 · 900000)에서도 두 키가 최상위에 들어가고, 넣은 줄도
    CRLF 다. 인용 키에 `/`·`@`·`:`·한글이 든 표 머리는 표의 시작이다 — 그 표 안의 같은 이름 키는
    관리 키가 아니므로 바뀌지 않는다."""
    shipped = TEMPLATE_FILES["codex"][0].read_text(encoding="utf-8")
    managed = "model_auto_compact_token_limit = 180000\n"
    previous = shipped.replace(managed, "model_auto_compact_token_limit = 900000\n", 1)
    config_dir = _global_dir(tmp_path)

    crlf = previous.replace("\n", "\r\n")
    root = _project(tmp_path / "crlf", harnesses=("codex",), texts={".codex/config.toml": crlf})
    path = root / ".codex" / "config.toml"
    assert _apply(pm_import, root, config_dir) == (
        ".codex/config.toml model_auto_compact_token_limit 900000 → 180000",)
    after = path.read_bytes().decode("utf-8")
    assert after == crlf.replace(
        "model_auto_compact_token_limit = 900000\r\n",
        "model_auto_compact_token_limit = 180000\r\n", 1), (
        "값 자리·넣은 한 줄 밖의 바이트가 바뀌었거나 넣은 줄이 CRLF 가 아니다")
    document = tomllib.loads(after)
    assert "model_context_window" not in document
    assert document["model_auto_compact_token_limit"] == 180000
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
            "model_auto_compact_token_limit = 180000\n", 1), header
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
    restored = after.replace('"context": 232000', '"context": 200000', 1)
    assert _only_insertions(before, restored), "관리 context 외의 주석·다른 멤버 바이트를 지우거나 바꿨다"
    document = _jsonc(pm_import, path)
    ollama = document["provider"]["ollama"]["models"]
    assert ollama["glm-5.2:cloud"]["limit"] == {"context": 232000, "input": 200000, "output": 32768}
    assert ollama["qwen3.8:27b-unc"]["limit"] == {"context": 232000, "input": 200000, "output": 32000}
    openrouter = document["provider"]["openrouter"]["models"]
    assert openrouter["anthropic/claude-x"]["limit"] == {
        "context": 232000, "input": 200000, "output": 16000}
    assert set(openrouter) == {"anthropic/claude-x"}, "모델 id 를 슬래시에서 갈랐다"
    assert document["compaction"]["reserved"] == 20000
    for provider in document["provider"].values():
        for model in provider["models"].values():
            if "limit" in model:
                assert {"context", "output"} <= set(model["limit"]), model
    assert '.opencode/opencode.jsonc provider.ollama.models."qwen3.8:27b-unc".limit.context (없음) → 232000' in lines
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


def test_shipped_defaults_and_resolved_native_fixed_point(pm_import, tmp_path):
    root = _project(tmp_path)
    config_dir = _global_dir(tmp_path)
    before = {relpath: (root / relpath).read_bytes() for _template, relpath in TEMPLATE_FILES.values()}

    expected=('.opencode/opencode.jsonc provider.ollama.models."glm-5.2:cloud".limit.context 200000 → 232000',)
    assert _apply(pm_import, root, config_dir, write=False) == expected
    assert {relpath: (root / relpath).read_bytes() for relpath in before} == before
    assert _apply(pm_import, root, config_dir) == expected
    assert _apply(pm_import, root, config_dir) == ()

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

# T-0932 독립 metadata 및 동결 계획 계약
@pytest.mark.parametrize('window,capacity,effective', [(150000,200000,150000),(200000,200000,200000),(600000,200000,200000),(333333,400000,333333)])
def test_t0932_effective_budget(local_conf, window, capacity, effective):
    identity = {'model':'fixture-model','provider':'fixture-provider','profile':''}
    metadata = _t0932_metadata(identity, capacity)
    result = local_conf.resolve_ctx_capacity(window, (80,90,94), identity, metadata)
    assert result['status'] == 'known'
    assert result['W'] == window and result['C'] == capacity and result['E'] == effective
    assert (result['checkpoint'],result['precompact'],result['P']) == tuple(effective*p//100 for p in (80,90,94))


def _t0932_metadata(identity, capacity=200000):
    return {**identity, 'source':'independent-runtime','generation':'fixture-generation',
            'observed_at':'2026-10-02T00:00:00+00:00','valid_until':'2099-01-01T00:00:00+00:00',
            'independent':True,'semantics':'usable','usable':capacity,'context':1000000,
            'output':32000,'effective_percent':95}


@pytest.mark.parametrize('change', [ {'model':'other'}, {'provider':'other'}, {'profile':'other'},
    {'independent':False}, {'usable':None}, {'generation':''}, {'valid_until':'2000-01-01T00:00:00+00:00'},
    {'semantics':'max_context_window'}, {'usable':True} ])
def test_t0932_metadata_identity(local_conf, change):
    identity={'model':'fixture-model','provider':'fixture-provider','profile':''}
    metadata={**_t0932_metadata(identity),**change}
    result=local_conf.resolve_ctx_capacity(600000,(80,90,94),identity,metadata)
    assert result['status']=='unknown' and result['E'] is None


def test_t0932_metadata_identity_percent_once(local_conf):
    identity={'model':'fixture-model','provider':'fixture-provider','profile':''}
    metadata={**_t0932_metadata(identity),'semantics':'context-percent','context':272000,'effective_percent':95}
    result=local_conf.resolve_ctx_capacity(600000,(80,90,94),identity,metadata)
    assert result['C']==258400
    metadata.update(semantics='usable',usable=258400)
    assert local_conf.resolve_ctx_capacity(600000,(80,90,94),identity,metadata)['C']==258400


def test_t0932_plan_no_write_unknown(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path)
    before=_snapshot_fixture_bytes(root)
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a,**kw: {'models':[], 'evidence':{}})
    with pytest.raises(pm_import.HarnessCtxError,match='unknown'):
        _apply(pm_import,root,_global_dir(tmp_path))
    assert before==_snapshot_fixture_bytes(root)


def _t0932_reader(pm_import,root,harness,documents,models,capacity=200000):
    source=root/'.project_manager/independent-runtime-fixture.json'
    if not source.exists():
        source.write_text('{"generation":"fixture-generation","usable":200000}',encoding='utf-8')
    identities=[{'model':model,'provider':provider,'profile':''} for provider,model in models] if harness=='opencode' else [{'model':'fixture-model','provider':'fixture-provider','profile':''}]
    rows=[]
    for identity in identities:
        metadata=_t0932_metadata(identity,capacity)
        metadata.update(native_formula='input-reserved',output=128000,effective_output=32000)
        rows.append({'identity':identity,'metadata':metadata})
    native=Path(shutil.which(harness) or '')
    native_files={str(native.resolve()):pm_import._ctx_native_stat(native)} if native.is_file() else {}
    native_commands={harness:str(native.resolve())} if native.is_file() else {}
    evidence={str(source.resolve()):_sha(source)}
    if harness=='claude':
        settings=[str(source.resolve()),str(root/'.claude/settings.local.json'),str(root/'.project_manager/mock-managed.json')]
        roles={'settings':settings,'managed_bundle':'/etc/claude-code/managed-settings.d'}
        for path in settings:evidence[path]=pm_import._ctx_file_digest(Path(path))
    elif harness=='codex':
        auth=str(root/'.project_manager/mock-auth.json');evidence[auth]=None
        roles={'manifest':str(source.resolve()),'selection':str(source.resolve()),'catalog':str(source.resolve()),'auth':auth}
    else:roles={'manifest':str(source.resolve())}
    return {'models':rows,'evidence':evidence,'native_files':native_files,'native_commands':native_commands,'roles':roles}


def test_t0932_native_constraints_clamped_actual_entry(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,conf='harness.claude.ctx_window_tokens=600000\nharness.codex.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n')
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a))
    _apply(pm_import,root,_global_dir(tmp_path))
    assert json.loads((root/'.claude/settings.json').read_text(encoding='utf-8'))['autoCompactWindow']==188000
    codex=tomllib.loads((root/'.codex/config.toml').read_text(encoding='utf-8'))
    assert 'model_context_window' not in codex and codex['model_auto_compact_token_limit']==188000
    oc=_jsonc(pm_import,root/'.opencode/opencode.jsonc')
    assert oc['provider']['ollama']['models']['glm-5.2:cloud']['limit']=={'context':1000000,'input':200000,'output':32768}
    assert oc['compaction']['reserved']==12000
    assert 200000-oc['compaction']['reserved']==188000
    assert 'harness.claude.ctx_window_tokens=600000' in (root/'.project_manager/local.conf').read_text(encoding='utf-8')


@pytest.mark.parametrize('change',['conf','native','ledger','source','generation','model','provider','profile'])
def test_t0932_plan_no_write_stale(pm_import,tmp_path,monkeypatch,change):
    root=_project(tmp_path,harnesses=('claude',))
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a))
    with pm_import._local_conf_write_lock(root/'.project_manager/local.conf'):
        pass
    plan=pm_import._prepare_harness_ctx(root,{'ctx.compact_at_pct':'94','delegate.enabled':'true'},opencode_config_dir=_global_dir(tmp_path),updates={'ctx.compact_at_pct':'94','delegate.enabled':'true'})
    paths={'conf':root/'.project_manager/local.conf','native':root/'.claude/settings.json',
           'ledger':root/pm_import.ADAPTER_BASELINE_RELPATH,'source':root/'.project_manager/independent-runtime-fixture.json'}
    target=paths.get(change,paths['source'])
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes((target.read_bytes() if target.exists() else b'')+change.encode())
    before=_snapshot_fixture_bytes(root)
    with pytest.raises(pm_import.HarnessCtxError,match='stale'):
        pm_import._commit_harness_ctx(plan)
    assert before==_snapshot_fixture_bytes(root)


def test_t0932_plan_no_write_single_resolution_ctx_apply(pm_import,tmp_path,monkeypatch,capsys):
    root=_project(tmp_path,conf='harness.claude.ctx_window_tokens=600000\n',harnesses=('claude',))
    config=_load('pm_config');calls=[]
    def reader(*a):
        calls.append(a[1]);return _t0932_reader(pm_import,*a)
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',reader)
    from types import SimpleNamespace
    rc=config.cmd_ctx_apply(SimpleNamespace(set=[('ctx.compact_at_pct','94')],json=True),pm_import=pm_import,dest_root=root)
    assert rc==0 and calls==['claude']
    result=json.loads(capsys.readouterr().out)
    assert result['ctx_budget']['claude']['E']==200000
    assert json.loads((root/'.claude/settings.json').read_text(encoding='utf-8'))['autoCompactWindow']==188000


def test_t0932_native_constraints_opencode_min_and_unknown(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('opencode',),texts={'.opencode/opencode.jsonc':'{"provider":{"p":{"models":{"a":{},"b":{}}}}}'})
    def reader(*a):
        result=_t0932_reader(pm_import,*a,capacity=300000)
        result['models'][1]['metadata']['usable']=150000
        return result
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',reader)
    global_dir=_global_dir(tmp_path)
    _apply(pm_import,root,global_dir)
    doc=_jsonc(pm_import,root/'.opencode/opencode.jsonc')
    assert {m['limit']['input'] for m in doc['provider']['p']['models'].values()}=={150000}
    assert doc['compaction']['reserved']==15000
    before=(root/'.opencode/opencode.jsonc').read_bytes()
    def missing(*a):
        result=reader(*a);result['models'].pop();return result
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',missing)
    with pytest.raises(pm_import.HarnessCtxError,match='집합'):_apply(pm_import,root,global_dir)
    assert before==(root/'.opencode/opencode.jsonc').read_bytes()

@pytest.fixture(autouse=True)
def _legacy_native_fixture(request,tmp_path,monkeypatch):
    """기존 독립 metadata fixture의 설치/명령 정체만 격리한다. C는 공급하지 않는다."""
    if 't0937' in request.node.name or 't0938' in request.node.name:return
    folder=tmp_path/'legacy-native-bin';folder.mkdir()
    for harness in ('claude','codex','opencode'):
        command=folder/harness;command.write_text('#!/bin/sh\nexit 91\n',encoding='utf-8');command.chmod(0o755)
    monkeypatch.setenv('PATH',str(folder)+os.pathsep+os.environ.get('PATH',''))


@pytest.fixture(autouse=True)
def _legacy_independent_metadata(request,pm_import,monkeypatch):
    if 't0932' in request.node.name or 't0937' in request.node.name or 't0938' in request.node.name:
        return
    def reader(*a):
        result=_t0932_reader(pm_import,*a)
        for row in result['models']:
            row['metadata'].update(context=232000,output=128000)
        return result
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',reader)


def _t0932_known_leaf(tmp_path):
    """기존 밴드 fixture의 독립 고정 C. 실제 계획/기록 reader를 그대로 통과한다."""
    from types import SimpleNamespace
    module=_load('pm_import');leaf=_load('local_conf')
    identity={'model':'fixture-model','provider':'fixture-provider','profile':''}
    def read(_root,values,harness,_identity=None):
        normalized=leaf.ctx_input_values(values)
        # 입력/default 검사와 known native 적용 검사는 구분한다.
        try:
            window=int(normalized[f'harness.{harness}.ctx_window_tokens'])
            ratios=tuple(int(normalized[k]) for k in leaf.CTX_INPUT_KEYS[-3:])
        except ValueError:
            return {'status':'unknown','E':None,'reason':'fixture 입력 오류'}
        if window<=0:return {'status':'unknown','E':None,'reason':'fixture 입력 오류'}
        if not 0<ratios[0]<ratios[1]<ratios[2]<100:
            normalized.update(zip(leaf.CTX_INPUT_KEYS[-3:],('70','80','90')))
        digest=hashlib.sha256(json.dumps([normalized,harness],sort_keys=True).encode()).hexdigest()[:16]
        root=tmp_path/digest
        if not root.exists():
            root=_project(root,conf=''.join(f'{k}={v}\n' for k,v in normalized.items()),harnesses=(harness,))
        else:root=root/'project'
        def reader(*args):
            snapshot=_t0932_reader(module,*args,capacity=2000000)
            for row in snapshot['models']:row['metadata'].update(context=3000000)
            return snapshot
        module._read_harness_ctx_metadata=reader
        if not (root/leaf.CTX_BUDGET_RELPATH).exists():
            module.apply_harness_ctx(root,write=True,opencode_config_dir=root/'global')
        row=leaf.read_ctx_budget(root,normalized,harness,identity if harness!='opencode' else {'model':'glm-5.2:cloud','provider':'ollama','profile':''})
        return row
    return SimpleNamespace(read_ctx_budget=read),identity


def _t0932_install_budget_fixture(root,values=None,harness='claude',capacity=2000000):
    """격리 런타임 fixture에 고정 독립 C를 실제 적용한 기록을 설치한다."""
    root=Path(root);leaf=_load('local_conf');module=_load('pm_import')
    values=values if values is not None else leaf.load(root/'.project_manager/local.conf').values
    conf=root/'.project_manager/local.conf'
    if not conf.exists():
        conf.parent.mkdir(parents=True,exist_ok=True)
        conf.write_text(''.join(f'{k}={v}\n' for k,v in values.items()),encoding='utf-8')
    native=root/TEMPLATE_FILES[harness][1]
    if not native.exists():
        native.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(TEMPLATE_FILES[harness][0],native)
    source=root/'.project_manager/independent-runtime-fixture.json'
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_text(json.dumps({'generation':'fixture-generation','usable':capacity}),encoding='utf-8')
    def reader(*a):
        result=_t0932_reader(module,*a,capacity=capacity)
        for row in result['models']:row['metadata'].update(context=3000000)
        return result
    module._read_harness_ctx_metadata=reader
    tools=root/'.project_manager/tools'
    tools.mkdir(parents=True,exist_ok=True)
    for name in ('local_conf.py','repo_owned_files.py','file_lock.py','console_encoding.py','pm_relay.py','python_floor.py'):
        if not (tools/name).exists():shutil.copyfile(TOOLS/name,tools/name)
    module._apply_harness_ctx_values(root,values,write=True,opencode_config_dir=root/'global')
    return {'model':'fixture-model','provider':'fixture-provider','profile':''} if harness!='opencode' else {'model':'glm-5.2:cloud','provider':'ollama','profile':''}


@pytest.mark.parametrize('semantics,raw',[
    ('input-reserved',{'input':232000,'reserved':32000,'context':262144,'output':128000}),
    ('context-output',{'context':232000,'effective_output':32000,'output':128000}),
    ('usable',{'usable':200000,'context':262144,'output':128000,'reserved':32000}),
])
def test_t0932_metadata_identity_reservation_semantics(local_conf,semantics,raw):
    identity={'model':'fixture-model','provider':'fixture-provider','profile':''}
    result=local_conf.resolve_ctx_capacity(600000,(80,90,94),identity,{**_t0932_metadata(identity),**raw,'semantics':semantics})
    assert (result['C'],result['E'],result['P'])==(200000,200000,188000)


def test_t0932_native_constraints_claude_range_after_clamp(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\n')
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a,capacity=100000))
    before=_snapshot_fixture_bytes(root)
    with pytest.raises(pm_import.HarnessCtxError,match='100000~1000000'):_apply(pm_import,root,_global_dir(tmp_path))
    assert before==_snapshot_fixture_bytes(root)


def test_t0932_plan_no_write_native_provider_environment(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('claude',))
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a))
    plan=pm_import._prepare_harness_ctx(root,{},opencode_config_dir=_global_dir(tmp_path))
    before=_snapshot_fixture_bytes(root, excluded_names=('local-conf.lock',))
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK','1')
    with pytest.raises(pm_import.HarnessCtxError,match='환경 변경'):pm_import._commit_harness_ctx(plan)
    assert before==_snapshot_fixture_bytes(root, excluded_names=('local-conf.lock',))


def test_t0932_native_constraints_output_reservation_insufficient(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('opencode',))
    def reader(*a):
        result=_t0932_reader(pm_import,*a)
        for row in result['models']:row['metadata']['context']=200000
        return result
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',reader)
    before=_snapshot_fixture_bytes(root)
    with pytest.raises(pm_import.HarnessCtxError,match='native encoding|출력 예약'):_apply(pm_import,root,_global_dir(tmp_path))
    assert before==_snapshot_fixture_bytes(root)


def test_t0932_metadata_identity_damaged_record(pm_import,local_conf,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\n')
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a))
    _apply(pm_import,root,_global_dir(tmp_path))
    path=root/local_conf.CTX_BUDGET_RELPATH
    record=json.loads(path.read_text(encoding='utf-8'));row=record['budgets']['claude'];identity=row['identity']
    row['E']=600000
    path.write_text(json.dumps(record), encoding='utf-8')
    assert local_conf.read_ctx_budget(root,{'harness.claude.ctx_window_tokens':'600000'},'claude',identity)['status']=='unknown'
    record['projection_sha256']=local_conf.ctx_record_digest(record)
    path.write_text(json.dumps(record), encoding='utf-8')
    assert local_conf.read_ctx_budget(root,{'harness.claude.ctx_window_tokens':'600000'},'claude',identity)['status']=='unknown'


def test_t0932_native_constraints_global_selection_is_a_target(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('opencode',))
    global_dir=_global_dir(tmp_path,{'opencode.json':'{"model":"another/selected"}'})
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(pm_import,*a))
    _apply(pm_import,root,global_dir)
    document=_jsonc(pm_import,root/'.opencode/opencode.jsonc')
    assert document['provider']['another']['models']['selected']['limit']['input']==200000
    before=(root/'.opencode/opencode.jsonc').read_bytes()
    def missing(*a):
        snapshot=_t0932_reader(pm_import,*a)
        snapshot['models']=[m for m in snapshot['models'] if m['identity']['provider']!='another']
        return snapshot
    monkeypatch.setattr(pm_import,'_read_harness_ctx_metadata',missing)
    with pytest.raises(pm_import.HarnessCtxError,match='집합'):_apply(pm_import,root,global_dir)
    assert (root/'.opencode/opencode.jsonc').read_bytes()==before


def _t0932_native_install(tmp_path,monkeypatch,harness,version="0.159.3"):
    package=tmp_path/(harness+'-installation');binary=package/'bin'/('codex' if harness=='codex' else 'opencode.exe')
    binary.parent.mkdir(parents=True)
    if harness=='codex':
        binary.write_bytes(b'models-cache-v1')
        (package/'codex-package.json').write_text(json.dumps({'layoutVersion':1,'version':version,'entrypoint':'bin/codex'}), encoding='utf-8')
    else:
        # 실제 1.18.34 byte-read의 overflow 및 getter 식, native 실행은 하지 않는다.
        binary.write_bytes(b'S(exports,{maxOutputTokens:()=>yy,OUTPUT_TOKEN_MAX:()=>V7});var V7=32000;function yy($,Z=V7){return Math.min($.limit.output,Z)||Z}function vn(e){let o=e.model.limit.context;let l=e.cfg.compaction?.reserved??Math.min(20000,yy(e.model,e.outputTokenMax));return e.model.limit.input?Math.max(0,e.model.limit.input-l):Math.max(0,o-yy(e.model,e.outputTokenMax))}')
        (package/'package.json').write_text(json.dumps({'name':'opencode-ai','version':'1.18.34','bin':{'opencode':'bin/opencode.exe'}}), encoding='utf-8')
    original=shutil.which
    monkeypatch.setattr(shutil,'which',lambda name,*a,**kw:str(binary) if name==harness else original(name,*a,**kw))
    return binary


def _t0932_codex_native_catalog(tmp_path,monkeypatch,version="0.159.3"):
    from datetime import datetime,timezone
    _t0932_native_install(tmp_path,monkeypatch,'codex',version)
    home=tmp_path/'codex-native-home';home.mkdir();monkeypatch.setenv('CODEX_HOME',str(home))
    provider={'name':'Fixture','base_url':'https://fixture.example/v1','model_catalog_url':'https://fixture.example/models','requires_openai_auth':False}
    # Native Rust identity의 독립 oracle, 제품 identity 함수를 호출하지 않는다.
    fields=[b'models-cache-v1',b'Fixture',b'https://fixture.example/v1',b'model_catalog_url',b'https://fixture.example/models',(0).to_bytes(8,'little'),b'\x00',b'\x00',b'None',(0).to_bytes(8,'little')]
    digest=hashlib.sha256()
    for field in fields:digest.update(len(field).to_bytes(8,'little'));digest.update(field)
    catalog={'identity':digest.hexdigest(),'client_version':version,'fetched_at':datetime.now(timezone.utc).isoformat(),'etag':'native-generation',
        'models':[{'slug':'exact-model','context_window':272000,'effective_context_window_percent':95,'max_context_window':872000,'supports_experimental_context':False}]}
    (home/'config.toml').write_text('model="exact-model"\nprofile="team"\n[profiles.team]\nmodel_provider="fixture"\n[profiles.inactive]\nmodel_context_window=128000\n[model_providers.fixture]\nname="Fixture"\nbase_url="https://fixture.example/v1"\nmodel_catalog_url="https://fixture.example/models"\nrequires_openai_auth=false\n', encoding='utf-8')
    (home/'models_cache.json').write_text(json.dumps(catalog), encoding='utf-8')
    return home,catalog


@pytest.mark.parametrize('bad',[None,'identity','version','expired','model','override'])
def test_t0932_metadata_identity_actual_codex_format(pm_import,tmp_path,monkeypatch,bad):
    from datetime import datetime,timedelta,timezone
    root=_project(tmp_path,harnesses=('codex',),conf='harness.codex.ctx_window_tokens=600000\n')
    home,catalog=_t0932_codex_native_catalog(tmp_path,monkeypatch)
    global_before=(home/'config.toml').read_bytes()
    if bad=='identity':catalog['identity']=None
    if bad=='version':catalog['client_version']='old-version'
    if bad=='expired':catalog['fetched_at']=(datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    if bad=='model':catalog['models'][0]['slug']='similar-model'
    if bad=='override':
        catalog['models'][0].pop('max_context_window')
        (home/'config.toml').write_text('model_context_window=600000\n'+(home/'config.toml').read_text(encoding='utf-8'), encoding='utf-8')
    (home/'models_cache.json').write_text(json.dumps(catalog), encoding='utf-8')
    before=_snapshot_fixture_bytes(root)
    if bad:
        with pytest.raises(pm_import.HarnessCtxError):_apply(pm_import,root,_global_dir(tmp_path))
        assert before==_snapshot_fixture_bytes(root)
    else:
        _apply(pm_import,root,_global_dir(tmp_path))
        record=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))
        assert record['budgets']['codex']['C']==258400
        assert (home/'config.toml').read_bytes()==global_before


@pytest.mark.parametrize('input_limit,capacity',[(192000,192000),(None,230144)])
def test_t0932_native_constraints_actual_opencode_format(pm_import,tmp_path,monkeypatch,input_limit,capacity):
    _t0932_native_install(tmp_path,monkeypatch,'opencode')
    cache=tmp_path/'cache';(cache/'opencode').mkdir(parents=True);monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    limit={'context':262144,'output':128000}
    if input_limit is not None:limit['input']=input_limit
    registry={'p':{'id':'p','api':'https://fixture.example','models':{'m':{'id':'m','limit':limit}}}}
    source=cache/'opencode/models.json';source.write_text(json.dumps(registry), encoding='utf-8');source_before=source.read_bytes()
    root=_project(tmp_path,harnesses=('opencode',),conf='harness.opencode.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n',texts={'.opencode/opencode.jsonc':'{"model":"p/m","provider":{"p":{"models":{"m":{"limit":{"output":32768}}}}}}'})
    _apply(pm_import,root,_global_dir(tmp_path))
    record=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'));row=record['budgets']['opencode']
    assert (row['C'],row['E'],row['P'])==(capacity,capacity,capacity*94//100)
    document=_jsonc(pm_import,root/'.opencode/opencode.jsonc')
    encoded=document['provider']['p']['models']['m']['limit']
    assert encoded['context']==262144 and encoded['input']-document['compaction']['reserved']==row['P']
    assert encoded['output']==32768 and source.read_bytes()==source_before
    before=(root/'.opencode/opencode.jsonc').read_bytes();registry['p']['models'].clear();source.write_text(json.dumps(registry), encoding='utf-8')
    with pytest.raises(pm_import.HarnessCtxError,match='정확 모델 없음'):_apply(pm_import,root,_global_dir(tmp_path/'retry'))
    assert (root/'.opencode/opencode.jsonc').read_bytes()==before


def test_t0932_metadata_identity_actual_claude_missing_producer(pm_import,tmp_path,monkeypatch):
    root=_project(tmp_path,harnesses=('claude',))
    _t0932_control_claude_fixture(tmp_path,monkeypatch)
    def missing(*a,**kw):
        raise ValueError('기존 실행정보 mock 원천 없음')
    monkeypatch.setattr(pm_import,'_ctx_native_protocol',missing)
    before=(root/'.claude/settings.json').read_bytes()
    with pytest.raises(pm_import.HarnessCtxError,match='기존 실행정보'):_apply(pm_import,root,_global_dir(tmp_path))
    assert (root/'.claude/settings.json').read_bytes()==before


@pytest.mark.parametrize('field', ['provider', 'model'])
def test_t0932_metadata_identity_registry_id_mismatch(pm_import, tmp_path, monkeypatch, field):
    _t0932_native_install(tmp_path, monkeypatch, 'opencode')
    cache = tmp_path / 'cache/opencode'
    cache.mkdir(parents=True)
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache.parent))
    provider_id = 'other-provider' if field == 'provider' else 'p'
    model_id = 'other-model' if field == 'model' else 'm'
    registry = {'p': {'id': provider_id, 'api': 'https://fixture.example',
                     'models': {'m': {'id': model_id, 'limit': {'context': 262144, 'output': 128000}}}}}
    (cache / 'models.json').write_text(json.dumps(registry), encoding='utf-8')
    root = _project(tmp_path, harnesses=('opencode',), texts={'.opencode/opencode.jsonc': '{"model":"p/m","provider":{"p":{"models":{"m":{}}}}}'})
    before = _snapshot_fixture_bytes(root)
    with pytest.raises(pm_import.HarnessCtxError, match='정체'):
        _apply(pm_import, root, _global_dir(tmp_path))
    assert _snapshot_fixture_bytes(root) == before


@pytest.mark.parametrize('version', ['0.159.3', '0.160.0'])
def test_t0932_review_codex_project_selection(tmp_path, monkeypatch, version):
    importer = _load('pm_import')
    root = _project(tmp_path, harnesses=('codex',),
        conf='harness.codex.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n',
        texts={'.codex/config.toml': 'model="small-model"\n'})
    home, catalog = _t0932_codex_native_catalog(tmp_path, monkeypatch, version)
    with (home / 'config.toml').open('a', encoding='utf-8') as stream:
        stream.write(f'\n[projects.{json.dumps(str(root.resolve()))}]\ntrust_level="trusted"\n')
    global_before = (home / 'config.toml').read_bytes()
    catalog['models'].append({'slug': 'small-model', 'context_window': 100000,
        'effective_context_window_percent': 95})
    (home / 'models_cache.json').write_text(json.dumps(catalog), encoding='utf-8')
    importer.apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path))
    native = tomllib.loads((root / '.codex/config.toml').read_text(encoding='utf-8'))
    row = json.loads((root / '.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['codex']
    print('선택 모델:', native['model'], '기록 모델:', row['identity']['model'], 'C:', row['C'], '압축:', native['model_auto_compact_token_limit'])
    assert (home / 'config.toml').read_bytes() == global_before
    assert row['identity']['model'] == 'small-model'
    assert row['C'] == 95000
    assert native['model_auto_compact_token_limit'] == 89300



def test_t0932_review_plan_target_presence(tmp_path, monkeypatch):
    importer = _load('pm_import')
    root = _project(tmp_path, harnesses=('claude',))
    monkeypatch.setattr(importer, '_read_harness_ctx_metadata', lambda *args: _t0932_reader(importer, *args))
    plan = importer._prepare_harness_ctx(root, {'ctx.compact_at_pct': '94'},
        updates={'ctx.compact_at_pct': '94'}, opencode_config_dir=_global_dir(tmp_path))
    added = root / '.codex/config.toml'
    added.parent.mkdir()
    added.write_text('model="unverified-model"\n', encoding='utf-8')
    conf_before = (root / '.project_manager/local.conf').read_bytes()
    native_before = (root / '.claude/settings.json').read_bytes()
    rejected = False
    try:
        result = importer._commit_harness_ctx(plan)
        print('기록 하네스:', sorted(result[2]), 'conf 변경:', result[0])
    except importer.HarnessCtxError:
        rejected = True
    print('거부:', rejected, 'conf 보존:', (root / '.project_manager/local.conf').read_bytes() == conf_before,
        'native 보존:', (root / '.claude/settings.json').read_bytes() == native_before)
    assert rejected
    assert (root / '.project_manager/local.conf').read_bytes() == conf_before
    assert (root / '.claude/settings.json').read_bytes() == native_before



def test_t0932_review_native_policy_formula(tmp_path, monkeypatch):
    importer = _load('pm_import')
    binary = _t0932_native_install(tmp_path, monkeypatch, 'opencode')
    original = binary.read_bytes()
    assert b'e.model.limit.input-l' in original
    binary.write_bytes(original.replace(b'e.model.limit.input-l', b'e.model.limit.input+l'))
    cache = tmp_path / 'cache/opencode'
    cache.mkdir(parents=True)
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache.parent))
    (cache / 'models.json').write_text(json.dumps({'p': {'id': 'p', 'api': 'https://fixture.example',
        'models': {'m': {'id': 'm', 'limit': {'context': 262144, 'output': 128000}}}}}), encoding='utf-8')
    root = _project(tmp_path, harnesses=('opencode',),
        conf='harness.opencode.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n',
        texts={'.opencode/opencode.jsonc': '{"model":"p/m","provider":{"p":{"models":{"m":{"limit":{"output":32768}}}}}}'})
    before = (root / '.opencode/opencode.jsonc').read_bytes()
    rejected = False
    try:
        importer.apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path))
        row = json.loads((root / '.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['opencode']
        native = json.loads((root / '.opencode/opencode.jsonc').read_text(encoding='utf-8'))
        recorded_point = row['P']
        actual_formula_point = native['provider']['p']['models']['m']['limit']['input'] + native['compaction']['reserved']
        print('원천 수식 거부:', rejected, '기록 P:', recorded_point, '변형 native P:', actual_formula_point)
    except importer.HarnessCtxError:
        rejected = True
    assert rejected
    assert (root / '.opencode/opencode.jsonc').read_bytes() == before


@pytest.mark.parametrize('change', ['input-plus', 'context-plus', 'input-operand', 'context-operand', 'getter-output', 'getter-fallback', 'getter-default', 'getter-argument'])
def test_t0932_review_native_policy_formula_value_flow(tmp_path, monkeypatch, change):
    importer = _load('pm_import')
    command = _t0932_native_install(tmp_path, monkeypatch, 'opencode')
    original = command.read_bytes()
    substitutions = {
        'input-plus': (b'e.model.limit.input-l', b'e.model.limit.input+l'),
        'context-plus': (b'o-yy(e.model,e.outputTokenMax)', b'o+yy(e.model,e.outputTokenMax)'),
        'input-operand': (b'e.model.limit.input-l', b'e.model.limit.context-l'),
        'context-operand': (b'o-yy(e.model,e.outputTokenMax)', b'l-yy(e.model,e.outputTokenMax)'),
        'getter-output': (b'$.limit.output,Z', b'$.limit.input,Z'),
        'getter-fallback': (b'||Z}', b'||V7}'),
        'getter-default': (b'Z=V7', b'Z=32000'),
        'getter-argument': (b'e.model,e.outputTokenMax', b'e.model,e.otherOutput'),
    }
    old, new = substitutions[change]
    assert old in original
    assert importer._ctx_opencode_policy(original) == 32000
    with pytest.raises(ValueError):
        importer._ctx_opencode_policy(original.replace(old, new))


@pytest.mark.parametrize('harness', ['claude', 'codex', 'opencode'])
@pytest.mark.parametrize('shape', ['file', 'directory', 'symlink'])
def test_t0932_review_plan_target_presence_all_harnesses(tmp_path, monkeypatch, harness, shape):
    importer = _load('pm_import')
    kept = 'codex' if harness == 'claude' else 'claude'
    root = _project(tmp_path, harnesses=(kept,))
    monkeypatch.setattr(importer, '_read_harness_ctx_metadata', lambda *a: _t0932_reader(importer, *a))
    with importer._local_conf_write_lock(root / '.project_manager/local.conf'):
        pass
    plan = importer._prepare_harness_ctx(root, {'ctx.compact_at_pct': '94'},
        updates={'ctx.compact_at_pct': '94'}, opencode_config_dir=_global_dir(tmp_path))
    target = root / TEMPLATE_FILES[harness][1]
    target.parent.mkdir(parents=True, exist_ok=True)
    if shape == 'file':
        shutil.copyfile(TEMPLATE_FILES[harness][0], target)
    elif shape == 'directory':
        target.mkdir()
    else:
        target.symlink_to(TEMPLATE_FILES[harness][0])
    before = _snapshot_fixture_bytes(root)
    with pytest.raises(importer.HarnessCtxError):
        importer._commit_harness_ctx(plan)
    assert _snapshot_fixture_bytes(root) == before


@pytest.mark.parametrize('version', ['0.159.3', '0.160.0', '0.161.0'])
@pytest.mark.parametrize('trust', [None, 'untrusted', 'trusted'])
@pytest.mark.parametrize('model', ['small-model', 'missing-model'])
def test_t0932_review_codex_project_selection_trust_and_exclusions(tmp_path, monkeypatch, trust, model, version):
    importer = _load('pm_import')
    root = _project(tmp_path, harnesses=('codex',), conf='harness.codex.ctx_window_tokens=600000\n',
        texts={'.codex/config.toml': f'model="{model}"\nmodel_provider="excluded-provider"\nprofile="excluded-profile"\n'})
    home, catalog = _t0932_codex_native_catalog(tmp_path, monkeypatch, version)
    if trust:
        with (home / 'config.toml').open('a', encoding='utf-8') as stream:
            stream.write(f'\n[projects.{json.dumps(str(root.resolve()))}]\ntrust_level="{trust}"\n')
    catalog['models'].append({'slug': 'small-model', 'context_window': 100000, 'effective_context_window_percent': 95})
    (home / 'models_cache.json').write_text(json.dumps(catalog), encoding='utf-8')
    before = _snapshot_fixture_bytes(root)
    global_before = (home / 'config.toml').read_bytes()
    if trust == 'trusted' and model == 'small-model' and version != '0.161.0':
        importer.apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path))
        row = json.loads((root / '.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['codex']
        assert row['identity'] == {'model': 'small-model', 'provider': 'fixture', 'profile': 'team'}
        assert (row['C'], row['E'], row['P']) == (95000, 95000, 85500)
    else:
        with pytest.raises(importer.HarnessCtxError):
            importer.apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path))
        assert _snapshot_fixture_bytes(root) == before
    assert (home / 'config.toml').read_bytes() == global_before


@pytest.mark.parametrize('harness', ['claude', 'codex', 'opencode'])
@pytest.mark.parametrize('change', ['delete', 'directory'])
def test_t0932_review_plan_target_presence_existing(tmp_path, monkeypatch, harness, change):
    importer = _load('pm_import')
    root = _project(tmp_path, harnesses=(harness,))
    monkeypatch.setattr(importer, '_read_harness_ctx_metadata', lambda *a: _t0932_reader(importer, *a))
    with importer._local_conf_write_lock(root / '.project_manager/local.conf'):
        pass
    plan = importer._prepare_harness_ctx(root, {'ctx.compact_at_pct': '94'},
        updates={'ctx.compact_at_pct': '94'}, opencode_config_dir=_global_dir(tmp_path))
    target = root / TEMPLATE_FILES[harness][1]
    target.unlink()
    if change == 'directory':
        target.mkdir()
    before = _snapshot_fixture_bytes(root)
    with pytest.raises(importer.HarnessCtxError):
        importer._commit_harness_ctx(plan)
    assert before == _snapshot_fixture_bytes(root)


def _t0932_control_claude_fixture(tmp_path, monkeypatch, model='claude-opus-5-5', window=1000000):
    from datetime import datetime, timezone
    package = tmp_path/'claude-installation'; command = package/'bin/claude'
    command.parent.mkdir(parents=True)
    command.write_bytes(b'// Version: 2.1.287\nfunction Bq(e,n){let r=Math.min(e.maxOutputTokens,z_t),s=Sm()?n:void 0,{window:g}=bv(e,s);return g-r}var z_t=20000;function Tq(e,n){let r=e-13000,s=n.testPctOverride;if(s!==void 0&&!isNaN(s)&&s>0&&s<=100)return Math.min(Math.floor(e*(s/100)),r);return r}' + b'function TCe(e,n){return Tq(Bq(e,n),yne(e,n))}function oTt(e,n){return Tq(Bq(e,n),{...yne(e,n),testPctOverride:void 0})}')
    (package/'package.json').write_text(json.dumps({'version':'2.1.287'}), encoding='utf-8')
    home = tmp_path/'claude-home'; home.mkdir(); monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home))
    (home/'settings.json').write_text('{}', encoding='utf-8')
    original = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name,*a,**kw: str(command) if name=='claude' else original(name,*a,**kw))
    return {'responses': {'initialize': {'models':[{'value':'default','resolvedModel':model}], 'account': {'apiProvider':'firstParty','subscriptionType':'Claude Max'}},
        'context': {'model':model,'rawMaxTokens':window,'maxTokens':window,'autoCompactThreshold':window-33000,
        'autocompactSource':'model-default','isAutoCompactEnabled':True,'apiUsage':None,
        'categories':[{'kind':'buffer','tokens':33000}]}}, 'frames':[], 'sent_user_turns':0,
        'observed_at':datetime.now(timezone.utc).isoformat()}


@pytest.mark.parametrize('model,window,capacity,encoded',[
    ('claude-opus-5-5',1000000,967000,597000),
    ('claude-sonnet-4-6',200000,167000,189980),
])
def test_t0932_native_producer_claude_zero_turn_cutoff(tmp_path, monkeypatch, model, window, capacity, encoded):
    importer = _load('pm_import')
    root = _project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n')
    response = _t0932_control_claude_fixture(tmp_path,monkeypatch,model,window)
    monkeypatch.setattr(importer,'_ctx_native_protocol',lambda *a,**kw:response,raising=False)
    importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
    native = json.loads((root/'.claude/settings.json').read_text(encoding='utf-8'))
    row = json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['claude']
    assert row['C']==capacity
    assert native['autoCompactWindow']==encoded
    assert native['autoCompactWindow']-33000==row['P']
    assert row['precompact']<row['P']


def test_t0932_native_producer_claude_existing_encoding_red(tmp_path, monkeypatch):
    importer = _load('pm_import'); root = _project(tmp_path,harnesses=('claude',))
    monkeypatch.setattr(importer,'_read_harness_ctx_metadata',lambda *a:_t0932_reader(importer,*a,capacity=967000))
    leaf = _load('local_conf'); values={'harness.claude.ctx_window_tokens':'600000','ctx.checkpoint_at_pct':'80','ctx.precompact_at_pct':'90','ctx.compact_at_pct':'94'}
    result=leaf.resolve_ctx_capacity(600000,(80,90,94),{'model':'m','provider':'firstParty','profile':''},
        {**_t0932_metadata({'model':'m','provider':'firstParty','profile':''}), 'semantics':'claude-cutoff',
         'context':1000000,'native_context':1000000,'reserved':20000,'summary_buffer':13000,'native_formula':'claude-cutoff','native_window_max':1000000})
    assert result['status']=='known'
    actual = importer.harness_ctx_targets(values,{}, {'claude':result})['.claude/settings.json'][('autoCompactWindow',)]
    assert actual-33000==564000


@pytest.mark.parametrize('bad',['source-auto','source-settings','suffix','provider','model-turn','clock','origin','output-change','window-change'])
def test_t0932_native_producer_claude_unknown_no_write(tmp_path,monkeypatch,bad):
    importer=_load('pm_import');root=_project(tmp_path,harnesses=('claude',))
    response=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    context=response['responses']['context']
    if bad=='source-auto':context['autocompactSource']='auto'
    if bad=='source-settings':context['autocompactSource']='settings'
    if bad=='suffix':context['model']='glm-5.3:cloud[1m]'
    if bad=='provider':response['responses']['initialize']['account']['apiProvider']='bedrock'
    if bad=='model-turn':response['sent_user_turns']=1
    if bad=='clock':response['observed_at']='2100-01-01T00:00:00Z'
    if bad=='origin':monkeypatch.setenv('ANTHROPIC_BASE_URL','http://localhost:11434')
    def protocol(*a,**kw):
        if bad in ('output-change','window-change'):
            monkeypatch.setenv('CLAUDE_CODE_MAX_OUTPUT_TOKENS' if bad=='output-change' else 'CLAUDE_CODE_AUTO_COMPACT_WINDOW','16000')
        return response
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    with importer._local_conf_write_lock(root/'.project_manager/local.conf'):
        pass
    before=_snapshot_fixture_bytes(root)
    with pytest.raises(importer.HarnessCtxError):importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
    assert _snapshot_fixture_bytes(root)==before


@pytest.mark.parametrize('change',['model','origin','output','percent','disable','window'])
def test_t0932_native_producer_claude_record_env_binding(tmp_path,monkeypatch,change):
    importer=_load('pm_import');root=_project(tmp_path,harnesses=('claude',))
    response=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(importer,'_ctx_native_protocol',lambda *a,**kw:response)
    importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
    names={'model':'ANTHROPIC_MODEL','origin':'ANTHROPIC_BASE_URL','output':'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
           'percent':'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE','disable':'DISABLE_AUTO_COMPACT','window':'CLAUDE_CODE_AUTO_COMPACT_WINDOW'}
    monkeypatch.setenv(names[change],'changed')
    leaf=_load('local_conf');before=_snapshot_fixture_bytes(root)
    assert leaf.read_ctx_budget(root,{},'claude',{'model':'claude-opus-5-5','provider':'firstParty','profile':''})['status']=='unknown'
    assert before==_snapshot_fixture_bytes(root)


def test_t0932_native_producer_protocol_late_fallback(tmp_path,monkeypatch):
    import io,time
    importer=_load('pm_import')
    class Late(io.StringIO):
        def __iter__(self):
            time.sleep(.05)
            return super().__iter__()
    class Process:
        stdin=io.StringIO();stdout=io.StringIO('{"id":1,"result":{}}\n')
        stderr=Late('failed to refresh available models\n')
        def poll(self):return 0
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(importer.subprocess,'Popen',lambda *a,**kw:Process())
    result=importer._ctx_native_protocol(['mock'],tmp_path,[('init',{'id':1,'method':'initialize','params':{}},None)])
    assert result['catalog_fallback'] is True


@pytest.mark.parametrize('bad',[None,'retired','wrong-tag','wrong-origin','architecture'])
def test_t0932_native_producer_opencode_exact_show(tmp_path,monkeypatch,bad):
    importer=_load('pm_import');_t0932_native_install(tmp_path,monkeypatch,'opencode')
    cache=tmp_path/'cache';(cache/'opencode').mkdir(parents=True);monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    config={'provider':{'ollama':{'npm':'@ai-sdk/openai-compatible','options':{'baseURL':'http://localhost:11434/v1'},
        'models':{'exact:tag':{'limit':{'output':32768}}}}}}
    root=_project(tmp_path,harnesses=('opencode',),conf='harness.opencode.ctx_window_tokens=600000\n',texts={'.opencode/opencode.jsonc':json.dumps(config)})
    from datetime import datetime,timezone
    def show(origin,model):
        if bad=='retired':raise ValueError('Ollama 정확 target retired')
        return {'origin':origin if bad!='wrong-origin' else 'http://other:11434/v1',
            'model':model if bad!='wrong-tag' else 'alias:tag','generation':'show-generation',
            'observed_at':datetime.now(timezone.utc).isoformat(),
            'body':{'model_info':{'general.architecture':'a','a.context_length':262144} if bad!='architecture' else {'general.architecture':'b','a.context_length':262144}}}
    monkeypatch.setattr(importer,'_ctx_ollama_show',show)
    before=_snapshot_fixture_bytes(root)
    if bad:
        with pytest.raises(importer.HarnessCtxError):importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        assert before==_snapshot_fixture_bytes(root)
    else:
        importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        record=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))
        row=record['budgets']['opencode'];assert (row['C'],row['E'],row['P'])==(230144,230144,207129)
        native=json.loads((root/'.opencode/opencode.jsonc').read_text(encoding='utf-8'))
        assert native['provider']['ollama']['models']['exact:tag']['limit']['context']==262144
        assert native['provider']['ollama']['models']['exact:tag']['limit']['input']-native['compaction']['reserved']==row['P']


@pytest.mark.parametrize('formula',['codex','claude'])
def test_t0932_native_producer_encoder_failure_unknown_fields(tmp_path,formula):
    leaf=_load('local_conf');identity={'model':'m','provider':'p','profile':''}
    metadata={**_t0932_metadata(identity), 'usable':100000, 'native_compact_max':90000}
    if formula=='claude':metadata.update(semantics='claude-cutoff',context=100000,native_context=100000,
        native_window_max=100000,reserved=0,summary_buffer=0,native_formula='claude-cutoff')
    result=leaf.resolve_ctx_capacity(100000,(80,90,94),identity,metadata)
    assert result['status']=='unknown'
    assert result['reason']
    assert all(result[k] is None for k in ('C','E','P','checkpoint','precompact'))


@pytest.mark.parametrize('percent,encoded',[(None,597000),(80,725000),(94,620000),(100,597000)])
def test_t0932_native_producer_claude_inverse_branches(tmp_path,percent,encoded):
    leaf=_load('local_conf');identity={'model':'m','provider':'firstParty','profile':''}
    metadata={**_t0932_metadata(identity), 'semantics':'claude-cutoff','context':1000000,'native_context':1000000,
        'native_window_max':1000000,'reserved':20000,'summary_buffer':13000,'native_formula':'claude-cutoff','native_percent':percent}
    result=leaf.resolve_ctx_capacity(600000,(80,90,94),identity,metadata)
    assert result['status']=='known'
    assert result['P']==564000
    assert result['native_window']==encoded
    base=encoded-20000
    assert (min(base-13000,int(base*percent/100)) if percent else base-13000)==result['P']


@pytest.mark.parametrize('retired',[False,True])
def test_t0932_native_producer_opencode_mixed_sources(tmp_path,monkeypatch,retired):
    importer=_load('pm_import');_t0932_native_install(tmp_path,monkeypatch,'opencode')
    cache=tmp_path/'cache';(cache/'opencode').mkdir(parents=True);monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    registry={'p':{'id':'p','models':{'m':{'id':'m','limit':{'context':262144,'output':128000}}}}}
    (cache/'opencode/models.json').write_text(json.dumps(registry),encoding='utf-8')
    config={'provider':{'p':{'models':{'m':{}}},'ollama':{'npm':'@ai-sdk/openai-compatible',
        'options':{'baseURL':'http://localhost:11434/v1'},'models':{'qwen3.5:397b-cloud':{}}}}}
    root=_project(tmp_path,harnesses=('opencode',),conf='harness.opencode.ctx_window_tokens=600000\n',
        texts={'.opencode/opencode.jsonc':json.dumps(config)})
    from datetime import datetime,timezone
    called=[]
    def show(origin,model):
        called.append(model)
        if retired:raise ValueError('Ollama 정확 target retired')
        return {'origin':origin,'model':model,'generation':'independent-show', 'observed_at':datetime.now(timezone.utc).isoformat(),
            'body':{'model_info':{'general.architecture':'glm','glm.context_length':1048576}}}
    monkeypatch.setattr(importer,'_ctx_ollama_show',show);before=_snapshot_fixture_bytes(root)
    if retired:
        with pytest.raises(importer.HarnessCtxError):importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        assert before==_snapshot_fixture_bytes(root)
    else:
        importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        row=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['opencode']
        assert row['C']==230144
        assert len(row['models'])==2
        assert {m['E'] for m in row['models']}=={230144}
    assert called==['qwen3.5:397b-cloud']


@pytest.mark.parametrize('bad',[None,'missing-max','native-cap'])
def test_t0932_native_producer_codex_conditional_override(tmp_path,monkeypatch,bad):
    importer=_load('pm_import');home,catalog=_t0932_codex_native_catalog(tmp_path,monkeypatch,version='0.160.0')
    (home/'config.toml').write_text('model_context_window=600000\n'+(home/'config.toml').read_text(encoding='utf-8'),encoding='utf-8')
    if bad=='missing-max':catalog['models'][0].pop('max_context_window')
    (home/'models_cache.json').write_text(json.dumps(catalog),encoding='utf-8')
    compact=99 if bad=='native-cap' else 94
    root=_project(tmp_path,harnesses=('codex',),conf=f'harness.codex.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct={compact}\n')
    before=_snapshot_fixture_bytes(root);global_before=(home/'config.toml').read_bytes()
    if bad:
        with pytest.raises(importer.HarnessCtxError):importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        assert before==_snapshot_fixture_bytes(root)
    else:
        importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        row=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['budgets']['codex']
        assert (row['C'],row['E'],row['P'])==(570000,570000,535800)
        assert row['P']<=540000
    assert (home/'config.toml').read_bytes()==global_before


@pytest.mark.parametrize('opaque',['other-account','other-provider','same-nonempty'])
def test_t0932_native_producer_codex_unapproved_cache_unknown(tmp_path,monkeypatch,opaque):
    importer=_load('pm_import');home,catalog=_t0932_codex_native_catalog(tmp_path,monkeypatch,version='0.160.0')
    (home/'auth.json').write_text('{"fixture":true}',encoding='utf-8')
    catalog['identity']=opaque;(home/'models_cache.json').write_text(json.dumps(catalog),encoding='utf-8')
    root=_project(tmp_path,harnesses=('codex',))
    config={'config':{'model':'exact-model','model_provider':'fixture','profile':'team','model_context_window':600000},
        'layers':[{'name':{'type':'user','file':str(home/'config.toml'),'profile':'team'},'version':'sha256:fixture','disabledReason':None,
            'config':{'model':'exact-model','model_context_window':600000}}]}
    response={'sent_user_turns':0,'catalog_fallback':False,'responses':{'config':config,'config_after':config,
        'gateway':{'providerId':'fixture','required':False,'error':None},'models':{'data':[{'id':'exact-model','model':'exact-model'}]}}}
    monkeypatch.setattr(importer,'_ctx_native_protocol',lambda *a,**kw:response)
    before=_snapshot_fixture_bytes(root)
    with pytest.raises(importer.HarnessCtxError,match='승인 catalog 세대'):importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
    assert before==_snapshot_fixture_bytes(root)


def _t0932_file_auth(plan='pro', refresh=True):
    import base64
    claims={'email':'fixture@example.invalid','https://api.openai.com/auth':{
        'chatgpt_user_id':'fixture-user','chatgpt_plan_type':plan,'chatgpt_account_is_fedramp':False}}
    encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'auth_mode':'chatgpt','tokens':{'account_id':'fixture-account','id_token':'header.'+encoded+'.signature',
        'access_token':'DO-NOT-OUTPUT-ACCESS','refresh_token':'DO-NOT-OUTPUT-REFRESH'},
        'last_refresh':'2026-10-03T00:00:00Z' if refresh else None}


def _t0932_file_identity_oracle(plan='Pro', raw='pro'):
    fields=[b'models-cache-v1',b'OpenAI',b'https://chatgpt.com/backend-api/codex',(0).to_bytes(8,'little'),
        b'\x01',b'\x00',b'Some(Chatgpt)',b'Some("fixture-account")',b'Some("fixture-user")',
        b'Some("fixture@example.invalid")',f'Some({plan})'.encode(),
        b'None' if raw is None else f'Some("{raw}")'.encode(),b'\x00',(1).to_bytes(8,'little'),b'version',b'0.160.0']
    digest=hashlib.sha256()
    for field in fields:digest.update(len(field).to_bytes(8,'little'));digest.update(field)
    return digest.hexdigest()


@pytest.mark.parametrize('plan,refresh,typed,raw',[('pro',True,'Pro','pro'),('hc',True,'Enterprise','enterprise'),
    ('education',True,'Edu','edu'),('PRO',True,'Unknown','PRO'),('pro',False,'Pro',None),
    ('promax',True,'ProMax','promax'),('edu_pro',True,'EduPro','edu_pro')])
def test_t0932_native_file_auth_identity_golden(plan,refresh,typed,raw):
    importer=_load('pm_import')
    provider={'name':'OpenAI','base_url':'https://chatgpt.com/backend-api/codex','requires_openai_auth':True,
        'http_headers':{'version':'0.160.0'}}
    assert importer._ctx_codex_cache_identity(provider,_t0932_file_auth(plan,refresh))==_t0932_file_identity_oracle(typed,raw)


@pytest.mark.parametrize('bad',[None,'identity','expired','version','fallback','auto','profile','env','provider',
    'no-owner','malformed','api-mode','implicit-api','auth-race','cache-race','config-race','file-race',
    'refresh-schema','claim-schema','unicode-owner','keyring','dynamic-header','base-override','base-origin'])
def test_t0932_native_file_auth_bound_catalog(tmp_path,monkeypatch,capsys,bad):
    from datetime import datetime,timedelta,timezone
    importer=_load('pm_import');home,catalog=_t0932_codex_native_catalog(tmp_path,monkeypatch,version='0.160.0')
    # 실제 최적화 binary처럼 hash marker literal이 없어도 정확 native digest는 검사한다.
    binary=Path(shutil.which('codex'));binary.write_bytes(b'optimized native fixture without identity marker')
    (home/'config.toml').write_text('model="exact-model"\nmodel_context_window=600000\n',encoding='utf-8')
    auth=_t0932_file_auth()
    if bad=='no-owner':auth['tokens'].pop('account_id')
    if bad=='malformed':auth['tokens']['id_token']='DO-NOT-OUTPUT-BROKEN'
    if bad=='api-mode':auth['auth_mode']='api_key'
    if bad=='implicit-api':auth.pop('auth_mode');auth['OPENAI_API_KEY']='DO-NOT-OUTPUT-API'
    if bad=='refresh-schema':auth['last_refresh']='invalid'
    if bad=='claim-schema':auth['tokens']['account_id']=42
    if bad=='unicode-owner':auth['tokens']['account_id']='소유자'
    (home/'auth.json').write_text(json.dumps(auth),encoding='utf-8')
    catalog['identity']=_t0932_file_identity_oracle()
    if bad=='identity':catalog['identity']='other-account'
    if bad=='expired':catalog['fetched_at']=(datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    if bad=='version':catalog['client_version']='0.159.3'
    (home/'models_cache.json').write_text(json.dumps(catalog),encoding='utf-8')
    root=_project(tmp_path,harnesses=('codex',),conf='harness.codex.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n')
    config={'config':{'model':'exact-model','model_provider':None,'profile':None,'model_context_window':600000,
        'chatgpt_base_url':'https://chatgpt.com/backend-api/','cli_auth_credentials_store':'file'},
        'layers':[{'name':{'type':'user','file':str(home/'config.toml')},'version':'sha256:fixture','disabledReason':None,
            'config':{'model':'exact-model','model_context_window':600000}}]}
    if bad=='auto':config['config']['cli_auth_credentials_store']='auto'
    if bad=='keyring':config['config']['cli_auth_credentials_store']='keyring'
    if bad=='profile':config['config']['profile']='other'
    if bad=='base-override':config['layers'][0]['config']['chatgpt_base_url']='https://chatgpt.com/backend-api/'
    if bad=='base-origin':config['config']['chatgpt_base_url']='https://other.example/backend-api/'
    if bad=='env':monkeypatch.setenv('CODEX_ACCESS_TOKEN','DO-NOT-OUTPUT-ENV')
    if bad=='provider':config['layers'][0]['config']['model_providers']={'openai':{'name':'Other'}}
    if bad=='dynamic-header':config['config']['model_providers']={'openai':{'name':'OpenAI','requires_openai_auth':True,
        'http_headers':{'version':'0.160.0','authorization':'DO-NOT-OUTPUT-HEADER'}}}
    response={'sent_user_turns':0,'catalog_fallback':bad=='fallback','responses':{'config':config,'config_after':config,
        'gateway':{'providerId':'openai','required':False,'error':None},'models':{'data':[{'id':'exact-model'}]}}}
    if bad=='config-race':response['responses']['config_after']=json.loads(json.dumps(config));response['responses']['config_after']['config']['model']='other'
    def protocol(*a,**kw):
        if bad=='file-race':(home/'config.toml').write_text('model="other"\n',encoding='utf-8')
        return response
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    digest=importer._ctx_file_digest
    def racing(path):
        if (bad=='auth-race' and Path(path).name=='auth.json') or (bad=='cache-race' and Path(path).name=='models_cache.json'):return 'racing-generation'
        return digest(path)
    monkeypatch.setattr(importer,'_ctx_file_digest',racing)
    before=_snapshot_fixture_bytes(root);user_before={p.name:p.read_bytes() for p in home.iterdir()}
    if bad:
        with pytest.raises(importer.HarnessCtxError) as error:
            importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        projection=str(error.value)
        assert before==_snapshot_fixture_bytes(root)
    else:
        importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        projection=(root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8')
        budget=json.loads(projection)['budgets']['codex']
        assert (budget['C'],budget['E'],budget['P'])==(570000,570000,535800)
        leaf=importer._load_local_conf();values=leaf.load(root/'.project_manager/local.conf').values
        assert leaf.read_ctx_budget(root,values,'codex',budget['identity'])['status']=='known'
        (home/'auth.json').write_text(json.dumps(_t0932_file_auth('plus')),encoding='utf-8')
        assert leaf.read_ctx_budget(root,values,'codex',budget['identity'])['status']=='unknown'
        (home/'auth.json').write_bytes(user_before['auth.json'])
    assert {p.name:p.read_bytes() for p in home.iterdir() if bad!='file-race' or p.name!='config.toml'}=={
        k:v for k,v in user_before.items() if bad!='file-race' or k!='config.toml'}
    projection+=str(capsys.readouterr())
    assert all(value not in projection for value in ('DO-NOT-OUTPUT','fixture@example.invalid','fixture-user','fixture-account','header.'))


def _t0937_actual_fixture(tmp_path,monkeypatch,*,mixed=False,apply=True):
    """제품 producer는 그대로 실행하며 native protocol/show만 격리한다."""
    from datetime import datetime,timezone
    from types import SimpleNamespace
    importer=_load('pm_import');leaf=importer._load_local_conf()
    isolated=tmp_path/'isolated-home';isolated.mkdir();monkeypatch.setenv('HOME',str(isolated))
    receipt=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    codex,catalog=_t0932_codex_native_catalog(tmp_path,monkeypatch,version='0.160.0')
    path=codex/'config.toml';path.write_text('model_context_window=600000\n'+path.read_text(encoding='utf-8'),encoding='utf-8')
    op_native=_t0932_native_install(tmp_path,monkeypatch,'opencode','1.18.34')
    binaries=[Path(shutil.which(h)) for h in ('claude','codex','opencode')]
    for binary in binaries:binary.chmod(0o755)
    # 별도 Node/Python 소비도 같은 격리 설치를 선택한다. command는 실행하지 않는다.
    (op_native.parent/'opencode').symlink_to(op_native.name)
    monkeypatch.setenv('PATH',os.pathsep.join(str(b.parent) for b in binaries)+os.pathsep+os.environ.get('PATH',''))
    cache=tmp_path/'cache';(cache/'opencode').mkdir(parents=True);monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    registry=cache/'opencode/models.json'
    registry.write_text(json.dumps({'registry':{'id':'registry','api':'https://registry.invalid/v1','models':{
        'exact-registry':{'id':'exact-registry','limit':{'context':262144,'input':250000,'output':128000}}}}}),encoding='utf-8')
    provider={'ollama':{'npm':'@ai-sdk/openai-compatible','options':{'baseURL':'http://fixture.invalid:11434/v1'},
        'models':{'glm-5.2:cloud':{'limit':{'output':32768}}}}}
    if mixed:provider['registry']={'models':{'exact-registry':{'limit':{'output':128000}}}}
    root=_project(tmp_path,conf='harness.claude.ctx_window_tokens=600000\nharness.codex.ctx_window_tokens=600000\nharness.opencode.ctx_window_tokens=200000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n',
        texts={'.opencode/opencode.jsonc':json.dumps({'provider':provider})})
    global_dir=_global_dir(tmp_path);calls=[]
    def protocol(*a,**kw):calls.append('protocol');return receipt
    def show(origin,model):
        calls.append('show')
        return {'model':model,'origin':origin,'body':{'model_info':{'general.architecture':'glm','glm.context_length':1048576}},
            'generation':'independent-show-generation','observed_at':datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    monkeypatch.setattr(importer,'_ctx_ollama_show',show)
    if apply:importer.apply_harness_ctx(root,write=True,opencode_config_dir=global_dir)
    values=leaf.load(root/'.project_manager/local.conf').values
    record=json.loads((root/leaf.CTX_BUDGET_RELPATH).read_text(encoding='utf-8')) if apply else {}
    return SimpleNamespace(importer=importer,leaf=leaf,root=root,values=values,record=record,codex=codex,
        registry=registry,global_dir=global_dir,calls=calls,op_native=op_native)


@pytest.mark.parametrize('source',['codex-cache','codex-config','opencode-config'])
def test_t0937_actual_producer_scope_matrix(tmp_path,monkeypatch,source):
    f=_t0937_actual_fixture(tmp_path,monkeypatch)
    assert all(f.leaf.read_ctx_budget(f.root,f.values,h,b['identity'])['status']=='known' for h,b in f.record['budgets'].items())
    assert (f.record['budgets']['claude']['E'],f.record['budgets']['codex']['E'],f.record['budgets']['opencode']['E'])==(600000,570000,200000)
    if source=='codex-cache':(f.codex/'models_cache.json').write_text('{"client_version":"0.150.0","models":[]}',encoding='utf-8')
    elif source=='codex-config':(f.codex/'config.toml').write_text('model="other"\n',encoding='utf-8')
    else:(f.global_dir/'opencode.json').write_text('{}',encoding='utf-8')
    own='opencode' if source.startswith('opencode') else 'codex';calls=list(f.calls);before=_snapshot_fixture_bytes(f.root)
    for h,b in f.record['budgets'].items():
        result=f.leaf.read_ctx_budget(f.root,f.values,h,b['identity'])
        assert result['status']==('unknown' if h==own else 'known'),(h,result)
        assert result['E']==(None if h==own else b['E'])
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls


@pytest.mark.parametrize('mixed',[False,True])
def test_t0937_opencode_used_source_matrix(tmp_path,monkeypatch,mixed):
    f=_t0937_actual_fixture(tmp_path,monkeypatch,mixed=mixed)
    f.registry.write_text('{}',encoding='utf-8');calls=list(f.calls);before=_snapshot_fixture_bytes(f.root)
    row=f.record['budgets']['opencode'];result=f.leaf.read_ctx_budget(f.root,f.values,'opencode',row['identity'])
    assert result['status']==('unknown' if mixed else 'known')
    assert result['E']==(None if mixed else 200000)
    assert f.leaf.read_ctx_budget(f.root,f.values,'claude',f.record['budgets']['claude']['identity'])['status']=='known'
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls


@pytest.mark.parametrize('change',['claude-env','codex-env','opencode-env','unused-cache-env','claude-window','codex-window',
    'opencode-window','ratio','claude-native','codex-native','opencode-native','ttl-before','ttl-expired'])
def test_t0937_scoped_native_environment_inputs(tmp_path,monkeypatch,change):
    from datetime import timedelta
    f=_t0937_actual_fixture(tmp_path,monkeypatch);affected=set()
    if change.endswith('-env'):
        key={'claude-env':'ANTHROPIC_MODEL','codex-env':'CODEX_HOME','opencode-env':'OPENCODE_CONFIG_CONTENT',
             'unused-cache-env':'XDG_CACHE_HOME'}[change]
        monkeypatch.setenv(key,'changed scope');affected=set() if change=='unused-cache-env' else {change.split('-')[0]}
    elif change.endswith('-window'):
        h=change.split('-')[0];f.values[f'harness.{h}.ctx_window_tokens']='300000';affected={h}
    elif change=='ratio':f.values['ctx.checkpoint_at_pct']='79';affected=set(f.record['budgets'])
    elif change.endswith('-native'):
        h=change.split('-')[0];path=Path(f.record['native_commands'][h]);path.write_bytes(path.read_bytes()+b'changed install');affected={h}
    else:
        now=f.leaf._ctx_time(f.record['budgets']['codex']['metadata']['observed_at'])+timedelta(seconds=299 if change=='ttl-before' else 300)
        original=f.leaf.resolve_ctx_capacity
        monkeypatch.setattr(f.leaf,'resolve_ctx_capacity',lambda window,ratios,identity,metadata:original(window,ratios,identity,metadata,now=now))
        affected=set() if change=='ttl-before' else {'codex'}
    before=_snapshot_fixture_bytes(f.root);calls=list(f.calls)
    for h,b in f.record['budgets'].items():
        row=f.leaf.read_ctx_budget(f.root,f.values,h,b['identity'])
        assert row['status']==('unknown' if h in affected else 'known'),(h,row)
        assert row['E']==(None if h in affected else b['E'])
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls


@pytest.mark.parametrize('damage',['none','bindings-missing','binding-null','unknown-owner','swapped-owner','native-empty','install-empty',
    'required-file','settings-role','catalog-file','registry-string','registry-hidden','models-duplicate','capacity','point','native-window','metadata',
    'json-duplicate'])
def test_t0937_record_scope_and_semantic_integrity(tmp_path,monkeypatch,damage):
    f=_t0937_actual_fixture(tmp_path,monkeypatch,mixed=damage=='registry-hidden');r=f.record;b=r['bindings']['claude'];row=r['budgets']['claude']
    if damage=='bindings-missing':r.pop('bindings')
    elif damage=='binding-null':r['bindings']['claude']=None
    elif damage=='unknown-owner':r['bindings']['other']=b
    elif damage=='swapped-owner':r['bindings']['claude']=r['bindings']['codex']
    elif damage=='native-empty':b['native_commands']={}
    elif damage=='install-empty':b['native_files']={}
    elif damage=='required-file':b['fingerprints'].pop(b['roles']['settings'][0])
    elif damage=='settings-role':b['roles'].pop('settings')
    elif damage=='catalog-file':
        c=r['bindings']['codex'];p=c['roles']['catalog'];c['fingerprints'].pop(p);c['required_files'].remove(p)
    elif damage=='registry-string':r['bindings']['opencode']['registry']='false'
    elif damage=='registry-hidden':r['bindings']['opencode']['registry']=False;r['bindings']['opencode']['roles'].pop('catalog')
    elif damage=='models-duplicate':row['models'].append(row['models'][0])
    elif damage=='capacity':row['C']+=1
    elif damage=='point':row['P']+=1
    elif damage=='native-window':row['native_window']+=1
    elif damage=='metadata':row['models'][0]['metadata']['reserved']+=1
    path=f.root/f.leaf.CTX_BUDGET_RELPATH
    if damage!='bindings-missing':r['projection_sha256']=f.leaf.ctx_record_digest(r)
    raw=json.dumps(r)
    if damage=='json-duplicate':raw=raw.replace('"bindings": {','"bindings": {}, "bindings": {',1)
    path.write_text(raw,encoding='utf-8');before=_snapshot_fixture_bytes(f.root);calls=list(f.calls)
    result=f.leaf.read_ctx_budget(f.root,f.values,'claude',row['identity'])
    assert result['status']==('known' if damage=='none' else 'unknown'),(damage,result)
    if damage!='none':assert all(result[k] is None for k in ('C','E','P'))
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls


@pytest.mark.parametrize('changed',['claude-source','codex-source','opencode-source','unused-registry','conf','baseline','record',
    'target-add','target-shape','native','environment','ttl','capacity','point','binding'])
def test_t0937_commit_preflight_all_sources_no_write(tmp_path,monkeypatch,changed):
    from datetime import datetime,timezone
    f=_t0937_actual_fixture(tmp_path,monkeypatch);plan=f.importer._prepare_harness_ctx(f.root,f.values,opencode_config_dir=f.global_dir)
    doc=json.loads(plan)
    if changed=='claude-source':Path(doc['bindings']['claude']['roles']['settings'][0]).write_text('{}\n',encoding='utf-8')
    elif changed=='codex-source':(f.codex/'models_cache.json').write_text('{}',encoding='utf-8')
    elif changed=='opencode-source':(f.global_dir/'config.json').write_text('{}',encoding='utf-8')
    elif changed=='unused-registry':f.registry.write_text('{}',encoding='utf-8')
    elif changed in ('conf','baseline','record'):
        name={'conf':'.project_manager/local.conf','baseline':f.importer.ADAPTER_BASELINE_RELPATH,'record':f.leaf.CTX_BUDGET_RELPATH}[changed]
        (f.root/name).write_bytes(b'changed bytes')
    elif changed=='target-add':
        # 부재 대상의 등장도 검사한다. 새 단일 하네스 fixture에서 다른 설정을 추가한다.
        path=f.root/'.codex/config.toml';path.unlink();plan=f.importer._prepare_harness_ctx(f.root,f.values,opencode_config_dir=f.global_dir)
        path.write_text('model="new"',encoding='utf-8')
    elif changed=='target-shape':path=f.root/'.claude/settings.json';path.unlink();path.mkdir()
    elif changed=='native':f.op_native.write_bytes(f.op_native.read_bytes()+b'changed')
    elif changed=='environment':monkeypatch.setenv('CODEX_HOME','changed')
    elif changed=='ttl':
        original=f.importer._load_local_conf;leaf=original();resolve=leaf.resolve_ctx_capacity
        deadline=leaf._ctx_time(doc['budgets']['codex']['metadata']['valid_until'])
        leaf.resolve_ctx_capacity=lambda w,r,i,m:resolve(w,r,i,m,now=deadline)
        monkeypatch.setattr(f.importer,'_load_local_conf',lambda:leaf)
    else:
        if changed=='capacity':doc['budgets']['claude']['C']+=1
        elif changed=='point':doc['budgets']['claude']['P']+=1
        else:doc['bindings']['claude']['native_commands']={}
        plan=json.dumps(doc)
    calls=[]
    for name in ('_write_conf_keys_locked','_harness_ctx_write','_atomic_write_dest_bytes'):
        original=getattr(f.importer,name)
        def write(*a,_name=name,_original=original,**kw):calls.append(_name);return _original(*a,**kw)
        monkeypatch.setattr(f.importer,name,write)
    before=_snapshot_fixture_bytes(f.root)
    with pytest.raises(f.importer.HarnessCtxError):f.importer._commit_harness_ctx(plan)
    assert before==_snapshot_fixture_bytes(f.root) and not calls


def test_t0937_legacy_unknown_existing_apply_refresh(tmp_path,monkeypatch):
    f=_t0937_actual_fixture(tmp_path,monkeypatch);path=f.root/f.leaf.CTX_BUDGET_RELPATH
    legacy=dict(f.record);legacy['schema']=1;legacy.pop('bindings');legacy['projection_sha256']=f.leaf.ctx_record_digest(legacy)
    path.write_text(json.dumps(legacy),encoding='utf-8');before=_snapshot_fixture_bytes(f.root);calls=list(f.calls)
    row=f.leaf.read_ctx_budget(f.root,f.values,'claude',legacy['budgets']['claude']['identity'])
    assert row['status']=='unknown' and '재검증' in row['reason'] and row['E'] is None
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls
    f.importer.apply_harness_ctx(f.root,write=True,opencode_config_dir=f.global_dir)
    refreshed=json.loads(path.read_text(encoding='utf-8'));assert refreshed['schema']==2
    assert f.leaf.read_ctx_budget(f.root,f.values,'claude',refreshed['budgets']['claude']['identity'])['status']=='known'


def test_t0937_setup_fixture_without_installed_cli(tmp_path,monkeypatch):
    from _manager_home import _KNOWN_CTX_READER_PY, known_ctx_module
    monkeypatch.setenv('PATH',str(tmp_path/'empty-path'))
    monkeypatch.delenv('_KNOWN_CTX_FIXTURE_BIN',raising=False)
    original=shutil.which
    assert shutil.which('claude') is None
    namespace={};exec(_KNOWN_CTX_READER_PY,namespace)
    with namespace['fixture_native_installation'](tmp_path/'native'):
        importer=_load('pm_import');known_ctx_module(importer)
        for index in range(2):
            root=_project(tmp_path/str(index),harnesses=('claude',))
            importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path/str(index)))
            leaf=importer._load_local_conf();record=json.loads((root/leaf.CTX_BUDGET_RELPATH).read_text(encoding='utf-8'))
            assert leaf.read_ctx_budget(root,leaf.load(root/'.project_manager/local.conf').values,'claude',record['budgets']['claude']['identity'])['status']=='known'
            assert not (root/'.project_manager/mock-native-bin').exists()
            assert shutil.which is original
    assert shutil.which is original and shutil.which('claude') is None
    assert '_KNOWN_CTX_FIXTURE_BIN' not in os.environ


def test_t0937_managed_source_absence_is_bound(tmp_path,monkeypatch):
    import stat
    from types import SimpleNamespace
    f=_t0937_actual_fixture(tmp_path,monkeypatch);plan=f.importer._prepare_harness_ctx(f.root,f.values,opencode_config_dir=f.global_dir)
    bundle=f.record['bindings']['claude']['roles']['managed_bundle'];original=Path.lstat
    monkeypatch.setattr(Path,'lstat',lambda path,*a,**kw:SimpleNamespace(st_mode=stat.S_IFDIR) if str(path)==bundle else original(path,*a,**kw))
    assert f.leaf.read_ctx_budget(f.root,f.values,'claude',f.record['budgets']['claude']['identity'])['status']=='unknown'
    assert f.leaf.read_ctx_budget(f.root,f.values,'codex',f.record['budgets']['codex']['identity'])['status']=='known'
    before=_snapshot_fixture_bytes(f.root)
    with pytest.raises(f.importer.HarnessCtxError):f.importer._commit_harness_ctx(plan)
    assert before==_snapshot_fixture_bytes(f.root)


@pytest.mark.parametrize('harness', ['claude', 'codex'])
@pytest.mark.parametrize('stage', ['record', 'commit'])
@pytest.mark.parametrize('replacement', ['target', 'other-source', 'manifest-or-local'])
def test_t0937_role_source_correlation(tmp_path, monkeypatch, harness, stage, replacement):
    f=_t0937_actual_fixture(tmp_path,monkeypatch)
    if stage=='commit':
        updates={'ctx.compact_at_pct':'93'}
        document=json.loads(f.importer._prepare_harness_ctx(f.root,{**f.values,**updates},updates=updates,opencode_config_dir=f.global_dir))
    else:document=f.record
    binding=document['bindings'][harness]
    if harness=='claude':
        index=1 if replacement=='manifest-or-local' else 0
        original=binding['roles']['settings'][index]
        binding['roles']['settings'][index]=str(f.root/'.codex/config.toml') if replacement=='other-source' else binding['target']
        path=binding['roles']['settings'][index]
        binding['fingerprints'][path]=document['fingerprints'][path]
        if path not in binding['required_files']:binding['required_files'].append(path)
        Path(original).write_text(json.dumps({'env':{'ANTHROPIC_MODEL':'changed-own-model'}}),encoding='utf-8')
    else:
        role='manifest' if replacement=='manifest-or-local' else 'auth'
        original=binding['roles'][role]
        binding['roles'][role]=binding['roles']['catalog'] if replacement=='other-source' else binding['target']
        Path(original).write_text('{"synthetic":"changed-own-auth"}',encoding='utf-8')
    binding['fingerprints'].pop(original)
    binding['required_files'].remove(original)
    document['fingerprints'].pop(original)
    calls=list(f.calls)
    writes=[]
    for name in ('_write_conf_keys_locked','_harness_ctx_write','_atomic_write_dest_bytes'):
        original_write=getattr(f.importer,name)
        def write(*a,_name=name,_original=original_write,**kw):
            writes.append(_name);return _original(*a,**kw)
        monkeypatch.setattr(f.importer,name,write)
    if stage=='record':
        document['projection_sha256']=f.leaf.ctx_record_digest(document)
        (f.root/f.leaf.CTX_BUDGET_RELPATH).write_text(json.dumps(document),encoding='utf-8')
        before=_snapshot_fixture_bytes(f.root)
        row=f.leaf.read_ctx_budget(f.root,f.values,harness,document['budgets'][harness]['identity'])
        assert row['status']=='unknown' and all(row[k] is None for k in ('C','E','P')),row
    else:
        before=_snapshot_fixture_bytes(f.root)
        with pytest.raises(f.importer.HarnessCtxError):f.importer._commit_harness_ctx(json.dumps(document))
    assert before==_snapshot_fixture_bytes(f.root) and not writes and calls==f.calls


@pytest.mark.parametrize('stage', ['record', 'commit', 'record-no-command', 'commit-no-command', 'record-empty', 'commit-empty', 'commit-shape', 'claude-only', 'codex-only', 'opencode-only', 'two-tools', 'empty'])
def test_t0937_registered_owner_completeness(tmp_path, monkeypatch, stage):
    damaged=stage.startswith(('record','commit'))
    f=_t0937_actual_fixture(tmp_path,monkeypatch,apply=damaged)
    identity=f.record['budgets']['claude']['identity'] if damaged else None
    if stage.startswith('record'):
        document=f.record
    else:
        keep={'claude-only':{'claude'},'codex-only':{'codex'},'opencode-only':{'opencode'},
              'two-tools':{'claude','opencode'},'empty':set()}.get(stage,{'claude','codex','opencode'})
        for harness,rel in f.importer.HARNESS_CTX_FILES.items():
            if harness not in keep:(f.root/rel).unlink()
        updates={'ctx.compact_at_pct':'93'}
        document=json.loads(f.importer._prepare_harness_ctx(f.root,{**f.values,**updates},updates=updates,opencode_config_dir=f.global_dir))
    if stage=='commit-shape':document['target_shapes'].pop('.codex/config.toml')
    elif damaged:
        document['bindings'].pop('codex');document['budgets'].pop('codex')
        if stage.endswith('no-command'):document['native_commands'].pop('codex')
        elif stage.endswith('empty'):document['bindings'].clear();document['budgets'].clear()
    calls=list(f.calls)
    if stage.startswith('record'):
        document['projection_sha256']=f.leaf.ctx_record_digest(document)
        (f.root/f.leaf.CTX_BUDGET_RELPATH).write_text(json.dumps(document),encoding='utf-8')
        before=_snapshot_fixture_bytes(f.root)
        row=f.leaf.read_ctx_budget(f.root,f.values,'claude',identity)
        assert row['status']=='unknown' and all(row[k] is None for k in ('C','E','P')),row
        assert before==_snapshot_fixture_bytes(f.root)
    elif stage.startswith('commit'):
        before=_snapshot_fixture_bytes(f.root)
        writes=[]
        for name in ('_write_conf_keys_locked','_harness_ctx_write','_atomic_write_dest_bytes'):
            original=getattr(f.importer,name)
            def write(*a,_name=name,_original=original,**kw):writes.append(_name);return _original(*a,**kw)
            monkeypatch.setattr(f.importer,name,write)
        with pytest.raises(f.importer.HarnessCtxError):f.importer._commit_harness_ctx(json.dumps(document))
        assert before==_snapshot_fixture_bytes(f.root) and not writes
    else:
        result=f.importer._commit_harness_ctx(json.dumps(document));assert set(result[2])==keep
        values=f.leaf.load(f.root/'.project_manager/local.conf').values
        for harness,budget in result[2].items():
            assert f.leaf.read_ctx_budget(f.root,values,harness,budget['identity'])['status']=='known'
    assert calls==f.calls


@pytest.mark.parametrize('harness',['claude','codex'])
def test_t0937_role_source_shared_paths(tmp_path,monkeypatch,harness):
    f=_t0937_actual_fixture(tmp_path,monkeypatch,apply=False)
    if harness=='claude':monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(f.root/'.claude'))
    else:
        home=f.root/'.codex'
        text=(f.codex/'config.toml').read_text(encoding='utf-8').replace('model_context_window=600000\n','')
        text+='\n[projects.'+json.dumps(str(f.root))+']\ntrust_level="trusted"\n'
        (home/'config.toml').write_text(text,encoding='utf-8')
        catalog=json.loads((f.codex/'models_cache.json').read_text(encoding='utf-8'))
        catalog['models'][0]['context_window']=600000
        (home/'models_cache.json').write_text(json.dumps(catalog),encoding='utf-8')
        monkeypatch.setenv('CODEX_HOME',str(home))
    f.importer.apply_harness_ctx(f.root,write=True,opencode_config_dir=f.global_dir)
    record=json.loads((f.root/f.leaf.CTX_BUDGET_RELPATH).read_text(encoding='utf-8'))
    roles=record['bindings'][harness]['roles']
    assert (roles['settings'][0] if harness=='claude' else roles['selection'])==record['bindings'][harness]['target']
    calls=list(f.calls);before=_snapshot_fixture_bytes(f.root)
    values=f.leaf.load(f.root/'.project_manager/local.conf').values
    for owner,budget in record['budgets'].items():
        assert f.leaf.read_ctx_budget(f.root,values,owner,budget['identity'])['status']=='known'
    assert before==_snapshot_fixture_bytes(f.root) and calls==f.calls


@pytest.mark.parametrize('version,damage', [('2.1.287',None),('2.1.288',None),('2.1.288','reserve'),('2.1.288','buffer'),('2.1.289',None)])
def test_t0932_verified_claude_patch_policy_compatibility(tmp_path,monkeypatch,version,damage):
    importer=_load('pm_import')
    root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\nctx.checkpoint_at_pct=80\nctx.precompact_at_pct=90\nctx.compact_at_pct=94\n')
    receipt=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    command=Path(shutil.which('claude')).resolve()
    data=command.read_bytes().replace(b'// Version: 2.1.287',b'// Version: '+version.encode())
    if version!='2.1.287':
        data=data.replace(b'Bq(',b'TK(').replace(b's=Sm()',b's=Df()').replace(b'=bv(',b'=Dw(')
    if damage=='reserve':data=data.replace(b'=20000;',b'=19000;')
    if damage=='buffer':data=data.replace(b'e-13000',b'e-12000')
    command.write_bytes(data)
    calls=[]
    def protocol(*args,**kwargs):
        calls.append(True)
        return receipt
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    before=_snapshot_fixture_bytes(root)
    if damage:
        with pytest.raises(importer.HarnessCtxError):
            importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        assert _snapshot_fixture_bytes(root)==before
        assert calls==[]
    else:
        importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
        record=json.loads((root/'.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))
        row=record['budgets']['claude']
        assert row['metadata']['source_version']==version
        assert (row['C'],row['E'],row['P'])==(967000,600000,564000)
        assert json.loads((root/'.claude/settings.json').read_text(encoding='utf-8'))['autoCompactWindow']==597000
        assert calls==[True]


# 정제 2.1.289 설치 fragment: 실제 reserve/cutoff와 두 caller의 결속.
_T0938_CLAUDE = b"function OK(e,n){let r=Math.min(e.maxOutputTokens,tTt),s=Lf()?n:void 0,{window:g}=Nw(e,s);return g-r}var tTt=20000;function dK(e,n){let r=e-13000,s=n.testPctOverride;if(s!==void 0&&!isNaN(s)&&s>0&&s<=100)return Math.min(Math.floor(e*(s/100)),r);return r}function TCe(e,n){return dK(OK(e,n),yne(e,n))}function oTt(e,n){return dK(OK(e,n),{...yne(e,n),testPctOverride:void 0})}function yne(e,n,r){let s=process.env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE,g=process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE;return{enabled:Lf(),precomputeBufferFraction:reo(e,n,r),testPctOverride:s?parseFloat(s):void 0,testBlockingOverride:g?Uc(g):void 0}}"


@pytest.mark.parametrize('case', ['287','288','289','renamed','duplicate','embedded',
    'reserve19000','buffer12000','backref','wrapper','different-duplicate','string','comment',
    'pct-lower','pct-upper','branch','provider','apiUsage','origin','model','selection','buffer','max','source','install-race'])
def test_t0938_at1_claude_policy_and_neutral_oracle(tmp_path, monkeypatch, case):
    import copy
    importer = _load('pm_import')
    root = _project(tmp_path, harnesses=('claude',), conf='harness.claude.ctx_window_tokens=600000\n')
    receipt = _t0932_control_claude_fixture(tmp_path, monkeypatch)
    command = Path(shutil.which('claude')); source = _T0938_CLAUDE
    good = case in {'287','288','289','renamed','duplicate','embedded'}
    if case in {'287','288'}:
        source=source.replace(b'OK(',b'Bq(' if case=='287' else b'TK(').replace(b'Lf()',b'Sm()' if case=='287' else b'Df()').replace(b'Nw(',b'bv(' if case=='287' else b'Dw(')
    if case=='renamed':
        import re
        mapping={b'OK':b'reserve',b'dK':b'cut',b'TCe':b'caller',b'oTt':b'defaultCaller',b'yne':b'options',b'tTt':b'cap',b'e':b'$a',b'n':b'_b',b'r':b'x',b's':b'y',b'g':b'z'}
        source=re.sub(rb'(?<![A-Za-z0-9_$])[A-Za-z_$][A-Za-z0-9_$]*(?![A-Za-z0-9_$])',lambda m:mapping.get(m[0],m[0]),source)
    if case=='duplicate':source=source+source
    if case=='embedded':source=b'function OK(x){return x+1}\0'+source
    mutations={'reserve19000':(b'=20000;',b'=19000;'),'buffer12000':(b'e-13000',b'e-12000'),
        'backref':(b'Nw(e,s)',b'Nw(n,s)'),'wrapper':(b'dK(OK(e,n),yne(e,n))',b'dK(OK(n,e),yne(e,n))'),
        'pct-lower':(b's>0',b's>=0'),'pct-upper':(b's<=100',b's<=101'),
        'branch':(b'return r}',b'if(n.extra)return 1;return r}')}
    if case in mutations:source=source.replace(*mutations[case])
    if case=='different-duplicate':source+=source.replace(b'e-13000',b'e-12000')
    if case=='string':source=b'var fake="'+source+b'";'
    if case=='comment':source=b'/*'+source+b'*/'
    version=case if case in {'287','288'} else '289'
    command.write_bytes(b'// Version: 2.1.'+version.encode()+b'\n'+source)
    if case=='provider':receipt['responses']['initialize']['account']['apiProvider']='custom'
    context=receipt['responses']['context']
    if case=='apiUsage':context['apiUsage']={'input_tokens':1}
    if case=='origin':monkeypatch.setenv('ANTHROPIC_BASE_URL','https://else.invalid')
    if case=='model':context['model']='claude-opus-5-5[1m]'
    if case=='selection':context['model']='other-model'
    if case=='buffer':context['categories'][0]['tokens']=32000
    if case=='max':context['maxTokens']=999999
    if case=='source':context['autocompactSource']='env'
    calls=[]
    def protocol(argv, path, requests):
        calls.append(requests)
        assert all(message['type']=='control_request' for _,message,_ in requests)
        assert [message['request']['subtype'] for _,message,_ in requests]==['initialize','get_context_usage']
        if case=='install-race':command.write_bytes(command.read_bytes()+b'changed')
        return copy.deepcopy(receipt)
    monkeypatch.setattr(importer, '_ctx_native_protocol', protocol)
    before = _snapshot_fixture_bytes(root)
    result = importer.preview_ctx_capacity(root, 'claude')
    assert result['status']==('known' if good else 'unknown')
    assert result['C']==(1000000-20000-13000 if good else None)
    assert _snapshot_fixture_bytes(root) == before
    if good:
        checked=importer.preview_ctx_capacity(root,'claude',receipt=result['receipt'])
        assert checked=={'mode':'check','schema':1,'harness':'claude','status':'known','reason':None}
        assert len(calls)==1


@pytest.mark.parametrize('case',['W-only','override','replace','expiry299','expiry300','TTL-extend',
    'future','missing','duplicate','auth','selection','install','input','race',
    'file-auth','file-auth-W-only','native-layer','RPC-race','catalog-version','catalog-identity'])
def test_t0938_at3_codex_snapshot_lifecycle(tmp_path, monkeypatch, case):
    from datetime import datetime,timedelta,timezone
    importer = _load('pm_import');leaf=importer._load_local_conf()
    root = _project(tmp_path, harnesses=('codex',), conf='harness.codex.ctx_window_tokens=600000\n')
    home, catalog = _t0932_codex_native_catalog(tmp_path, monkeypatch, version='0.160.0')
    if case in {'file-auth','file-auth-W-only','native-layer','RPC-race','catalog-version','catalog-identity'}:
        import copy
        global_override=case!='file-auth-W-only'
        (home/'config.toml').write_text('model="exact-model"\n'+('model_context_window=600000\n' if global_override else ''), encoding="utf-8")
        (home/'auth.json').write_text(json.dumps(_t0932_file_auth()), encoding="utf-8")
        catalog['identity']=_t0932_file_identity_oracle()
        config={'config':{'model':'exact-model','model_provider':None,'profile':None,'model_context_window':600000,
            'chatgpt_base_url':'https://chatgpt.com/backend-api/','cli_auth_credentials_store':'file'},
            'layers':[{'name':{'type':'project','dotCodexFolder':str(root/'.codex')},'version':'sha256:project',
                'config':{'model_context_window':600000}},
                {'name':{'type':'user','file':str(home/'config.toml')},'version':'sha256:user',
                 'config':{'model':'exact-model',**({'model_context_window':600000} if global_override else {})}}]}
        if case=='native-layer':
            layer=root.parent/'.codex/config.toml';layer.parent.mkdir();layer.write_text('model="exact-model"', encoding="utf-8")
            config['layers'].append({'name':{'type':'project','dotCodexFolder':str(layer.parent)},'version':'sha256:parent','config':{'model':'exact-model'}})
        after=copy.deepcopy(config)
        if case=='RPC-race':after['config']['model']='other'
        response={'sent_user_turns':0,'catalog_fallback':False,'responses':{'config':config,'config_after':after,
            'gateway':{'providerId':'openai','required':False,'error':None},'models':{'data':[{'id':'exact-model'}]}}}
        def protocol(argv,path,requests):
            assert [r[1]['method'] for r in requests]==['initialize','config/read','account/gatewayOAuth/read','model/list','config/read']
            return copy.deepcopy(response)
        monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    fetched=datetime.now(timezone.utc);catalog['fetched_at']=fetched.isoformat()
    cache=home/'models_cache.json'
    if case=='override':(home/'config.toml').write_text('model_context_window=600000\n'+(home/'config.toml').read_text(encoding="utf-8"), encoding="utf-8")
    if case=='future':catalog['fetched_at']=(fetched+timedelta(seconds=1)).isoformat()
    if case=='catalog-version':catalog['client_version']='0.150.0'
    if case=='catalog-identity':catalog['identity']='other'
    if case=='missing':catalog['models']=[]
    if case=='duplicate':catalog['models']*=2
    cache.write_text(json.dumps(catalog), encoding="utf-8")
    original=importer._ctx_file_bytes
    def read(path):
        raw=original(path)
        if case=='race' and Path(path)==cache:cache.write_text('{}', encoding="utf-8")
        return raw
    monkeypatch.setattr(importer,'_ctx_file_bytes',read)
    before=_snapshot_fixture_bytes(root)
    result = importer.preview_ctx_capacity(root, 'codex',now=fetched)
    assert _snapshot_fixture_bytes(root)==before
    if case in {'future','missing','duplicate','race','RPC-race','catalog-version','catalog-identity'}:
        assert result['status']=='unknown' and result['C'] is None;return
    assert result['C'] == (600000 if case in {'override','file-auth','native-layer'} else 272000)*95//100
    models=result['receipt']['models']
    assert models[0]['metadata']['generation']==hashlib.sha256(cache.read_bytes()).hexdigest()
    assert result['valid_until']==(fetched+timedelta(seconds=300)).isoformat()
    if case.startswith('file-auth') or case=='native-layer':
        assert 'DO-NOT-OUTPUT' not in json.dumps(result)
    # Apply uses independent producer and still binds the catalog, including replacement.
    importer.apply_harness_ctx(root,write=True,opencode_config_dir=_global_dir(tmp_path))
    result=importer.preview_ctx_capacity(root,'codex',now=fetched)
    values=leaf.load(root/'.project_manager/local.conf').values
    identity=models[0]['identity']
    assert leaf.read_ctx_budget(root,values,'codex',identity)['status']=='known'
    check_time=fetched
    if case in {'replace','expiry299','expiry300'}:
        cache.write_text('{"client_version":"0.150.0","models":[]}', encoding="utf-8")
        check_time=fetched+timedelta(seconds=300 if case=='expiry300' else 299)
        assert leaf.read_ctx_budget(root,values,'codex',identity)['status']=='unknown'
    if case=='TTL-extend':
        result['receipt']['models'][0]['metadata']['valid_until']=(fetched+timedelta(seconds=301)).isoformat()
        r=result['receipt'];r['digest']=hashlib.sha256(json.dumps({k:v for k,v in r.items() if k!='digest'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if case=='auth':(home/'auth.json').write_text('{"sentinel":"secret"}', encoding="utf-8")
    if case=='selection':(home/'config.toml').write_text('model="other"', encoding="utf-8")
    if case=='install':Path(shutil.which('codex')).write_bytes(b'changed')
    if case=='input':(root/'.project_manager/local.conf').write_text('harness.codex.ctx_window_tokens=123000\n', encoding="utf-8")
    before=_snapshot_fixture_bytes(root)
    checked=importer.preview_ctx_capacity(root,'codex',receipt=result['receipt'],now=check_time)
    assert set(checked)=={'mode','schema','harness','status','reason'}
    assert checked['status']==('unknown' if case in {'expiry300','TTL-extend','auth','selection','install','input'} else 'known')
    assert _snapshot_fixture_bytes(root)==before


@pytest.mark.parametrize('case',['positive','same-copy','renamed','embedded','long-string','long-comment'])
def test_t0938_review_claude_policy_lexical_context(tmp_path,monkeypatch,case):
    import copy,re
    assert hashlib.sha256(_T0938_CLAUDE).hexdigest()=='803006685b877fb3c1f700f7852be0432c3e65820712e004ec8d8371c94d68da'
    source=_T0938_CLAUDE
    if case=='same-copy':source+=source
    if case=='embedded':source=b'function OK(x){return x+1}\0'+source
    if case=='renamed':
        names={b'OK':b'reserve',b'dK':b'cutoff',b'TCe':b'caller',b'oTt':b'wrapper',b'yne':b'options'}
        source=re.sub(rb'[A-Za-z_$][A-Za-z0-9_$]*',lambda m:names.get(m[0],m[0]),source)
    if case=='long-string':source=b'const fake="'+b'x'*9000+source+b'y'*9000+b'";'
    if case=='long-comment':source=b'/*'+b'x'*9000+source+b'y'*9000+b'*/'
    importer=_load('pm_import');root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\n')
    native=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    Path(shutil.which('claude')).write_bytes(b'// Version: 2.1.289\n'+source)
    trace=[]
    def protocol(argv,path,requests):
        trace.append([r[1]['request']['subtype'] for r in requests]);return copy.deepcopy(native)
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    before=_snapshot_fixture_bytes(tmp_path)
    result=importer.preview_ctx_capacity(root,'claude')
    good=case in {'positive','same-copy','renamed','embedded'}
    assert result['status']==('known' if good else 'unknown')
    assert result['C']==(1000000-20000-13000 if good else None)
    assert trace==([['initialize','get_context_usage']] if good else [])
    assert before==_snapshot_fixture_bytes(tmp_path)


@pytest.mark.parametrize('case',['positive','same-copy','caller','wrapper','reserve','cutoff'])
def test_t0938_review_claude_connected_caller_ambiguity(tmp_path,monkeypatch,case):
    import copy
    additions={'caller':b'function TCe(e,n){return dK(OK(n,e),yne(e,n))}',
        'wrapper':b'function oTt(e,n){return dK(OK(n,e),{...yne(e,n),testPctOverride:void 0})}',
        'reserve':b'function OK(e,n){return 1}', 'cutoff':b'function dK(e,n){return e-12000}'}
    source=_T0938_CLAUDE+(_T0938_CLAUDE if case=='same-copy' else additions.get(case,b''))
    importer=_load('pm_import');root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\n')
    native=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    Path(shutil.which('claude')).write_bytes(b'// Version: 2.1.289\n'+source)
    trace=[]
    def protocol(*args):trace.append('metadata');return copy.deepcopy(native)
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    before=_snapshot_fixture_bytes(tmp_path)
    result=importer.preview_ctx_capacity(root,'claude')
    good=case in {'positive','same-copy'}
    assert result['status']==('known' if good else 'unknown')
    assert result['C']==(967000 if good else None)
    assert trace==(['metadata'] if good else [])
    assert before==_snapshot_fixture_bytes(tmp_path)


@pytest.mark.parametrize('key',['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN'])
@pytest.mark.parametrize('phase',['stable','check-change','query-race'])
def test_t0938_review_preview_auth_environment_binding(tmp_path,monkeypatch,key,phase):
    import copy
    importer=_load('pm_import');leaf=importer._load_local_conf()
    root=_project(tmp_path,harnesses=('claude',),conf='harness.claude.ctx_window_tokens=600000\n')
    native=_t0932_control_claude_fixture(tmp_path,monkeypatch)
    Path(shutil.which('claude')).write_bytes(b'// Version: 2.1.289\n'+_T0938_CLAUDE)
    monkeypatch.setenv(key,'ENV_AUTH_ONE');trace=[]
    original=leaf.ctx_environment_fingerprints('claude')
    others={h:leaf.ctx_environment_fingerprints(h) for h in ('codex','opencode')}
    def protocol(*args):
        trace.append('metadata')
        if phase=='query-race':monkeypatch.setenv(key,'ENV_AUTH_TWO')
        return copy.deepcopy(native)
    monkeypatch.setattr(importer,'_ctx_native_protocol',protocol)
    before=_snapshot_fixture_bytes(tmp_path)
    result=importer.preview_ctx_capacity(root,'claude')
    if phase=='query-race':
        assert result['status']=='unknown' and result['C'] is None
    else:
        assert result['C']==967000
        assert result['receipt']['binding']['environment'][key]==hashlib.sha256(b'ENV_AUTH_ONE').hexdigest()
        if phase=='check-change':monkeypatch.setenv(key,'ENV_AUTH_TWO')
        checked=importer.preview_ctx_capacity(root,'claude',receipt=result['receipt'])
        assert checked['status']==('known' if phase=='stable' else 'unknown')
        assert 'C' not in checked
    assert leaf.ctx_environment_fingerprints('claude')==original and key not in original
    assert others=={h:leaf.ctx_environment_fingerprints(h) for h in others}
    assert trace==['metadata']
    assert 'ENV_AUTH_' not in json.dumps(result)
    assert before==_snapshot_fixture_bytes(tmp_path)
