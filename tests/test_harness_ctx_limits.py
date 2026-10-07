"""하네스 사용 한도·압축 비율 반영 (T-0923 AT-001~AT-011·AT-023 · 리뷰 F-001~F-003 · T-0943 AT-001~AT-005).

local.conf 의 하네스별 사용 한도 W(`harness.<이름>.ctx_window_tokens`)와 세 비율(쓴 %)로 하네스 설정
파일 세 종류에 압축 지점을 쓰는 엔진 함수(`pm_import.apply_harness_ctx` 와 그 부품)를 잰다. 모델 한도 C 는
적용 시점에 한 번 조회해 압축 한도 기록(`local_conf.CTX_BUDGET_RELPATH`)에 고정한다.

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
  7. 적용 시점 고정(T-0943) — 기록 형식·읽기 계약·도구별 C 조회. 조회는 교체 지점
     (`_read_harness_ctx_metadata`·`_ctx_claude_threshold`·`_ctx_native_protocol`)만 바꾸고 실제 CLI 를
     부르지 않는다. 축 1~6 은 고정 C(`_manager_home.known_ctx_seams`)로 돈다.
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

from _manager_home import _known_ctx_installation, known_ctx_seams

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


@pytest.fixture(autouse=True)
def _known_capacity(pm_import, monkeypatch):
    """축 1~6 의 모델 한도 — C 200,000 · 모델 창 232,000 · Claude 압축 예약 0(autoCompactWindow = P)."""
    for name, seam in known_ctx_seams(capacity=200000, context=232000).items():
        monkeypatch.setattr(pm_import, name, seam)


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


def test_targets_floor_and_reserved(pm_import):
    """설정값은 도구 기록 항목(E·P·모델 행)에서 나온다 — Claude 창 = P + d, opencode reserved = E − P."""
    def entries(effective):
        point = effective * 90 // 100
        return {
            "claude": {"E": effective, "P": point, "models": [{"id": "m", "provider": "firstParty", "C": 400000,
                       "basis": {"model_window": 433000, "auto_compact_threshold": 400000}}]},
            "codex": {"E": effective, "P": point, "models": [{"id": "m", "provider": "openai", "C": 400000,
                      "basis": {"context_window": 1000000, "max_context_window": None,
                                "model_context_window": None, "percent": 40}}]},
            "opencode": {"E": effective, "P": point, "models": [{"id": "glm-5.2:cloud", "provider": "ollama",
                         "C": 400000, "basis": {"context": 1000000, "output": 32000, "source": "fixture"}}]}}
    targets = pm_import.harness_ctx_targets({("ollama", "glm-5.2:cloud"): 32768}, entries(200000))

    assert targets[".claude/settings.json"] == {("autoCompactWindow",): 213000}
    assert targets[".codex/config.toml"] == {
        ("model_auto_compact_token_limit",): 180000}
    assert targets[".opencode/opencode.jsonc"] == {
        (*GLM, "context"): 1000000, (*GLM, "input"): 200000, (*GLM, "output"): 32768,
        ("compaction", "reserved"): 20000}

    odd = pm_import.harness_ctx_targets({("ollama", "glm-5.2:cloud"): 32768}, entries(333333))
    assert odd[".claude/settings.json"][("autoCompactWindow",)] == 299999 + 33000   # 내림(반올림 300000 아님)
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


# ── 다른 테스트 파일이 쓰는 기록 설치 도우미 ─────────────────────────────────


def install_ctx_record(root, values=None, harness="claude", capacity=2000000, model="fixture-model") -> str:
    """격리 런타임 트리에 고정 모델 한도로 실제 적용한 압축 한도 기록을 설치한다 — 그 기록의 세션 모델 이름.

    `values` 가 있으면 local.conf 에 그 값을 쓴 뒤 적용한다(기록 입력과 local.conf 가 같다). 훅·구동기가
    그 트리의 엔진 사본을 싣도록 필요한 도구 파일을 복사한다."""
    root = Path(root)
    module = _load("pm_import")
    conf = root / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    if not conf.exists():
        conf.write_text("", encoding="utf-8")
    target = root / TEMPLATE_FILES[harness][1]
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE_FILES[harness][0], target)
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name in ("local_conf.py", "repo_owned_files.py", "file_lock.py", "console_encoding.py",
                 "pm_relay.py", "python_floor.py"):
        if not (tools / name).exists():
            shutil.copyfile(TOOLS / name, tools / name)
    for name, seam in known_ctx_seams(capacity=capacity, context=3000000, model=model).items():
        setattr(module, name, seam)
    module._apply_harness_ctx(root, write=True, opencode_config_dir=root / "global", updates=values)
    return "ollama/glm-5.2:cloud" if harness == "opencode" else model


# ── T-0943 적용 시점 고정 ─────────────────────────────────────────────────────
# 확정 값(설계 1절 · PM 홈 실측): Claude W 600,000 · C 967,000 · 압축 예약 d 33,000 → E 600,000 ·
# P 564,000 · autoCompactWindow 597,000. Codex 실효 창 600,000 × 95% → C 570,000 → E 570,000 ·
# P 535,800. OpenCode 대상 모델 최소 C 230,144 → E 200,000 · P 188,000 · reserved 12,000.

T0943_CONF = ("harness.claude.ctx_window_tokens=600000\nharness.codex.ctx_window_tokens=600000\n"
              "harness.opencode.ctx_window_tokens=200000\nctx.checkpoint_at_pct=80\n"
              "ctx.precompact_at_pct=90\nctx.compact_at_pct=94\n")
T0943_CLAUDE_RESERVE = 33000
T0943_CLAUDE_ROW = {"id": "claude-opus-5-5", "provider": "firstParty", "C": 967000,
                    "basis": {"model_window": 1000000, "auto_compact_threshold": 967000}}
T0943_CODEX_ROW = {"id": "gpt-6.1-sol", "provider": "openai", "C": 570000,
                   "basis": {"context_window": 272000, "max_context_window": 872000,
                             "model_context_window": 600000, "percent": 95}}
# 기록에 두지 않는 옛 근거 필드 — 만료·관측 시각·해시·환경변수·설치 파일·인증 정체·검증 분리.
T0943_RETIRED_FIELDS = ("verification", "physical", "fingerprint", "valid_until", "observed_at", "sha256",
                        "digest", "environment", "native", "profile", "identity", "receipt")


def _t0943_seams(pm_import, monkeypatch, *, threshold_offset=T0943_CLAUDE_RESERVE):
    """모델 한도 조회 교체 — 확정 값 행과 Claude 확인 조회(창 − d)."""
    def reader(root, harness, documents, models):
        if harness == "claude":
            return [dict(T0943_CLAUDE_ROW)]
        if harness == "codex":
            return [dict(T0943_CODEX_ROW)]
        return [{"id": model, "provider": provider, "C": 230144,
                 "basis": {"context": 262144, "output": 32000, "source": "http://localhost:11434/v1"}}
                for (provider, model) in models]
    monkeypatch.setattr(pm_import, "_read_harness_ctx_metadata", reader)
    monkeypatch.setattr(pm_import, "_ctx_claude_threshold", lambda root, window: window - threshold_offset)


def _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch):
    _t0943_seams(pm_import, monkeypatch)
    root = _project(tmp_path, conf=T0943_CONF)
    _apply(pm_import, root, _global_dir(tmp_path))
    return root, local_conf.load(root / ".project_manager" / "local.conf").values


def _t0943_watched(root: Path) -> dict:
    paths = [root / relpath for _template, relpath in TEMPLATE_FILES.values()]
    paths += [root / ".project_manager" / "local.conf", root / ".project_manager" / ".local" / "ctx-budget.json"]
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def test_t0943_record_three_tools_fields_and_values(pm_import, local_conf, tmp_path, monkeypatch):
    """AT-001 — 세 도구 적용 뒤 기록은 schema 5 의 정해진 필드뿐이고 E·P·설정값이 확정 값이다."""
    root, _values = _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch)
    text = (root / local_conf.CTX_BUDGET_RELPATH).read_text(encoding="utf-8")
    record = json.loads(text)
    assert set(record) == {"schema", "applied_at", "inputs", "harnesses"} and record["schema"] == 5
    assert record["inputs"] == {
        "harness.claude.ctx_window_tokens": "600000", "harness.codex.ctx_window_tokens": "600000",
        "harness.opencode.ctx_window_tokens": "200000", "ctx.checkpoint_at_pct": "80",
        "ctx.precompact_at_pct": "90", "ctx.compact_at_pct": "94"}
    for entry in record["harnesses"].values():
        assert set(entry) == {"models", "W", "C", "E", "P", "checkpoint", "precompact", "settings"}
        assert all(set(row) == {"id", "provider", "C", "basis"} for row in entry["models"])
    numbers = {harness: tuple(entry[key] for key in ("W", "C", "E", "P", "checkpoint", "precompact"))
               for harness, entry in record["harnesses"].items()}
    assert numbers == {"claude": (600000, 967000, 600000, 564000, 480000, 540000),
                       "codex": (600000, 570000, 570000, 535800, 456000, 513000),
                       "opencode": (200000, 230144, 200000, 188000, 160000, 180000)}
    assert record["harnesses"]["claude"]["settings"] == {"autoCompactWindow": 597000}
    assert record["harnesses"]["codex"]["settings"] == {"model_auto_compact_token_limit": 535800}
    assert record["harnesses"]["opencode"]["settings"] == {
        "compaction.reserved": 12000, 'provider.ollama.models."glm-5.2:cloud".limit.context': 262144,
        'provider.ollama.models."glm-5.2:cloud".limit.input': 200000,
        'provider.ollama.models."glm-5.2:cloud".limit.output': 32768}
    assert not [field for field in T0943_RETIRED_FIELDS if field in text]

    assert json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))["autoCompactWindow"] == 597000
    assert tomllib.loads((root / ".codex/config.toml").read_text(encoding="utf-8"))[
        "model_auto_compact_token_limit"] == 535800
    opencode = _jsonc(pm_import, root / ".opencode/opencode.jsonc")
    assert opencode["compaction"]["reserved"] == 12000
    assert opencode["provider"]["ollama"]["models"]["glm-5.2:cloud"]["limit"] == {
        "context": 262144, "input": 200000, "output": 32768}
    assert _apply(pm_import, root, _global_dir(tmp_path)) == ()
    # 같은 값의 재적용은 기록도 다시 쓰지 않는다(적용 시각만 바뀌는 재기록 없음).
    assert (root / local_conf.CTX_BUDGET_RELPATH).read_text(encoding="utf-8") == text


def test_t0943_reader_value_survives_time_cache_environment_and_path(pm_import, local_conf, tmp_path,
                                                                      monkeypatch):
    """AT-002 — 기록 값은 시각·Codex 캐시·환경변수·실행 파일 경로가 바뀌어도 그대로다(None 반환 없음)."""
    root, values = _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch)
    first = {harness: local_conf.read_ctx_budget(root, values, harness) for harness in local_conf.CTX_HARNESSES}
    assert first["claude"] == {"E": 600000, "P": 564000, "checkpoint": 480000, "precompact": 540000,
                               "checkpoint_at_pct": 80, "precompact_at_pct": 90, "models": ["claude-opus-5-5"],
                               "applied_at": first["claude"]["applied_at"], "warning": None}
    assert [first[harness]["E"] for harness in local_conf.CTX_HARNESSES] == [600000, 570000, 200000]

    path = root / local_conf.CTX_BUDGET_RELPATH
    record = json.loads(path.read_text(encoding="utf-8"))
    record["applied_at"] = "2026-10-06T00:00:00+00:00"        # 하루 전에 적용한 기록
    path.write_text(json.dumps(record), encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text('{"fetched_at": "2020-01-01T00:00:00Z", "models": []}',
                                                  encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX", "1000")
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "50")
    moved = tmp_path / "moved-bin"
    moved.mkdir()
    (moved / "claude").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(moved) + os.pathsep + os.environ.get("PATH", ""))

    later = {harness: local_conf.read_ctx_budget(root, values, harness) for harness in local_conf.CTX_HARNESSES}
    for harness in local_conf.CTX_HARNESSES:
        assert {**later[harness], "applied_at": None} == {**first[harness], "applied_at": None}, harness
    assert all(row["status"] == "ok" for row in local_conf.ctx_budget_status(root, values).values())


@pytest.mark.parametrize("damage", ["missing", "broken", "schema4", "tool", "window", "ratio", "basis", "basis-type"])
def test_t0943_reader_errors_name_ctx_apply(pm_import, local_conf, tmp_path, monkeypatch, damage):
    """AT-002 — 부재·손상·이전 형식·도구 항목 없음·입력 변경은 `CtxBudgetError`("ctx-apply" 안내)다.
    모델 출처(basis)가 세 형식의 키를 갖추지 않았거나 값 형식이 다르면 읽기 단계의 기록 손상이다(F-004)."""
    root, values = _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch)
    path = root / local_conf.CTX_BUDGET_RELPATH
    record = json.loads(path.read_text(encoding="utf-8"))
    if damage == "missing":
        path.unlink()
    elif damage == "broken":
        path.write_text("{", encoding="utf-8")
    elif damage == "schema4":
        path.write_text(json.dumps({**record, "schema": 4}), encoding="utf-8")
    elif damage == "tool":
        del record["harnesses"]["claude"]
        path.write_text(json.dumps(record), encoding="utf-8")
    elif damage in ("basis", "basis-type"):
        basis = record["harnesses"]["claude"]["models"][0]["basis"]
        record["harnesses"]["claude"]["models"][0]["basis"] = {} if damage == "basis" else {
            **basis, "model_window": str(basis["model_window"])}
        path.write_text(json.dumps(record), encoding="utf-8")
    elif damage == "window":
        values = {**values, "harness.claude.ctx_window_tokens": "500000"}
    else:
        values = {**values, "ctx.compact_at_pct": "93"}
    with pytest.raises(local_conf.CtxBudgetError, match="ctx-apply"):
        local_conf.read_ctx_budget(root, values, "claude", "claude-opus-5-5")
    status = local_conf.ctx_budget_status(root, values)["claude"]
    assert status == {"status": "error", "reason": status["reason"]} and "ctx-apply" in status["reason"]


def test_t0943_reader_session_model_mismatch_warns_and_keeps_budget(pm_import, local_conf, tmp_path,
                                                                    monkeypatch):
    """AT-002 — 세션 모델이 기록 모델과 다르면 값은 그대로이고 `warning` 만 붙는다."""
    root, values = _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch)
    read = local_conf.read_ctx_budget
    assert read(root, values, "claude", "claude-opus-5-5")["warning"] is None
    assert read(root, values, "codex", "gpt-6.1-sol")["warning"] is None
    assert read(root, values, "opencode", "ollama/glm-5.2:cloud")["warning"] is None
    assert read(root, values, "claude", "firstParty/claude-opus-5-5")["warning"] is None
    other = read(root, values, "claude", "claude-sonnet-5")
    assert other["E"] == 600000 and other["P"] == 564000
    assert "기록 claude-opus-5-5 · 현재 claude-sonnet-5" in other["warning"] and "firstParty" not in other["warning"]
    assert "ctx-apply" in other["warning"]
    assert read(root, values, "opencode", "openrouter/glm-5.2:cloud")["warning"]


@pytest.mark.parametrize("basis", [
    {"context_window": 272000, "max_context_window": None, "model_context_window": None, "percent": 95},
    {"context": 400000, "input": 272000, "output": 16000, "source": "models.dev 목록"}])
def test_t0943_reader_accepts_every_basis_form(pm_import, local_conf, tmp_path, monkeypatch, basis):
    """F-004 역방향 — 출처 형식 검사는 적용이 쓰는 형식을 거부하지 않는다. 세 도구 기본 형식은 AT-002 의
    `ctx_budget_status` 전부 ok 가 덮고, 여기는 Codex 설정 창 null·opencode input 있음을 본다(도구 이름 무관)."""
    root, values = _t0943_applied(pm_import, local_conf, tmp_path, monkeypatch)
    path = root / local_conf.CTX_BUDGET_RELPATH
    record = json.loads(path.read_text(encoding="utf-8"))
    record["harnesses"]["claude"]["models"][0]["basis"] = basis
    path.write_text(json.dumps(record), encoding="utf-8")
    assert local_conf.read_ctx_budget(root, values, "claude")["E"] == 600000
    status = local_conf.ctx_budget_status(root, values)["claude"]
    assert status["status"] == "ok" and status["basis_text"]


def test_t0943_record_write_failure_returns_rc1(pm_import, local_conf, tmp_path, monkeypatch, capsys):
    """F-005 — 쓰기 단계 실패는 바꾼 파일이 없어도 `ctx-apply` rc 1 이다. 같은 입력의 재적용에서 Claude 출처
    표시값만 달라 기록만 다시 써야 할 때 기록 쓰기가 실패하면 local.conf·설정 파일·기록은 그대로다.
    조회·검사 단계의 거부는 그대로 rc 3 이다(역방향)."""
    _t0943_seams(pm_import, monkeypatch)
    pm_config = _load("pm_config")
    parser = pm_config.build_parser()
    root = _project(tmp_path, harnesses=("claude",), conf=T0943_CONF)
    assert pm_config.cmd_ctx_apply(parser.parse_args(["ctx-apply"]), pm_import=pm_import, dest_root=root) == 0
    before = _t0943_watched(root)
    row = {**T0943_CLAUDE_ROW, "basis": {"model_window": 1000100, "auto_compact_threshold": 967100}}  # d 33,000 그대로
    monkeypatch.setattr(pm_import, "_read_harness_ctx_metadata", lambda *args: [dict(row)])
    replace = pm_import._atomic_write_dest_bytes
    monkeypatch.setattr(pm_import, "_atomic_write_dest_bytes", lambda dest_root, rel, payload, **kwargs: (
        False if Path(rel) == local_conf.CTX_BUDGET_RELPATH else replace(dest_root, rel, payload, **kwargs)))
    capsys.readouterr()

    assert pm_config.cmd_ctx_apply(parser.parse_args(["ctx-apply"]), pm_import=pm_import, dest_root=root) == 1
    assert "압축 한도 기록(.project_manager/.local/ctx-budget.json)을 쓰지 못했습니다" in capsys.readouterr().err
    assert _t0943_watched(root) == before

    refused = pm_config.cmd_ctx_apply(parser.parse_args(["ctx-apply", "--set", "ctx.precompact_at_pct=95"]),
                                      pm_import=pm_import, dest_root=root)
    assert refused == 3 and "ctx.precompact_at_pct=95" in capsys.readouterr().err
    assert _t0943_watched(root) == before


def _t0943_claude_protocol(calls, *, offset=T0943_CLAUDE_RESERVE):
    """Claude Code 무턴 조회 응답 — 기본 창 1,000,000/967,000, 창 X 지정이면 X/X − offset."""
    def protocol(command, root, requests):
        calls.append(list(command))
        assert command[0] == "claude" and [name for name, _request, _note in requests] == ["initialize", "context"]
        window = command[command.index("--autocompact") + 1]
        raw = 1000000 if window == "auto" else int(window)
        threshold = 967000 if window == "auto" else raw - offset
        return {"initialize": {"account": {"apiProvider": "firstParty"}},
                "context": {"model": "claude-opus-5-5", "rawMaxTokens": raw, "autoCompactThreshold": threshold}}
    return protocol


def _t0943_claude_protocol(calls, *, offset=T0943_CLAUDE_RESERVE):
    """Claude Code 무턴 조회 응답 — 기본 창 1,000,000/967,000, 창 X 를 주면 X/X − offset."""
    def protocol(command, root, requests):
        calls.append(command[command.index("--autocompact") + 1])
        assert command[0] == "claude" and "--version" not in command
        assert [name for name, _request, _notification in requests] == ["initialize", "context"]
        window = calls[-1]
        raw = 1000000 if window == "auto" else int(window)
        threshold = 967000 if window == "auto" else raw - offset
        return {"initialize": {"account": {"apiProvider": "firstParty"}},
                "context": {"model": "claude-opus-5-5", "rawMaxTokens": raw, "autoCompactThreshold": threshold}}
    return protocol


def test_t0943_claude_default_window_and_confirm_query(local_conf, tmp_path, monkeypatch):
    """AT-003 — 기본 창 조회로 C 967,000·d 33,000, 창 597,000 확인 조회의 압축 기준 = P 564,000."""
    engine = _load("pm_import")
    calls = []
    monkeypatch.setattr(engine, "_ctx_native_protocol", _t0943_claude_protocol(calls))
    root = _project(tmp_path, harnesses=("claude",), conf=T0943_CONF)
    assert _apply(engine, root, _global_dir(tmp_path)) == (
        ".claude/settings.json autoCompactWindow 180000 → 597000",)
    assert calls == ["auto", "597000"]
    entry = json.loads((root / local_conf.CTX_BUDGET_RELPATH).read_text(encoding="utf-8"))["harnesses"]["claude"]
    assert entry["models"] == [T0943_CLAUDE_ROW]
    assert (entry["C"], entry["E"], entry["P"], entry["settings"]) == (
        967000, 600000, 564000, {"autoCompactWindow": 597000})


@pytest.mark.parametrize("case", ["threshold", "below-range"])
def test_t0943_claude_refuses_without_writing(local_conf, tmp_path, monkeypatch, case):
    """AT-003 — 확인 조회 압축 기준 ≠ P, 창이 100,000 미만이면 거부하고 어떤 파일도 바뀌지 않는다."""
    engine = _load("pm_import")
    calls = []
    offset = 30000 if case == "threshold" else T0943_CLAUDE_RESERVE
    monkeypatch.setattr(engine, "_ctx_native_protocol", _t0943_claude_protocol(calls, offset=offset))
    root = _project(tmp_path, harnesses=("claude",), conf=T0943_CONF)
    before = _t0943_watched(root)
    window = "600000" if case == "threshold" else "70000"          # 70,000 → P 65,800 → 창 98,800
    with pytest.raises(engine.HarnessCtxError) as caught:
        engine._apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path),
                                  updates={"harness.claude.ctx_window_tokens": window})
    assert _t0943_watched(root) == before
    if case == "threshold":
        assert calls == ["auto", "597000"] and "567000" in str(caught.value) and "564000" in str(caught.value)
    else:
        assert calls == ["auto"] and "98800" in str(caught.value) and "100000~1000000" in str(caught.value)


def _t0943_codex_cache(tmp_path: Path, monkeypatch, models) -> None:
    """Codex 모델 캐시 — 버전·받은 시각·인증 정체는 C 와 무관한 값이다."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({
        "client_version": "9.9.9", "etag": "fixture", "fetched_at": "2020-01-01T00:00:00Z",
        "identity": "another-account", "models": models}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))


def _t0943_codex_protocol(calls):
    def protocol(command, root, requests):
        calls.append(list(command))
        assert list(command[:2]) == ["codex", "app-server"]
        assert [name for name, _request, _notification in requests] == ["initialize", "config", "models"]
        return {"initialize": {}, "config": {"config": {"model": "gpt-6.1-sol", "model_context_window": 600000}},
                "models": {"data": []}}
    return protocol


_T0943_CODEX_CACHE_ROW = {"slug": "gpt-6.1-sol", "context_window": 272000, "max_context_window": 872000,
                          "effective_context_window_percent": 95}


def test_t0943_codex_app_server_and_cache_row(local_conf, tmp_path, monkeypatch):
    """AT-004 — 실효 창 600,000(설정) ≤ 최대 872,000 × 95% = C 570,000 → E 570,000 · P 535,800."""
    engine = _load("pm_import")
    calls = []
    monkeypatch.setattr(engine, "_ctx_native_protocol", _t0943_codex_protocol(calls))
    _t0943_codex_cache(tmp_path, monkeypatch, [{"slug": "other", "context_window": 1}, _T0943_CODEX_CACHE_ROW])
    root = _project(tmp_path, harnesses=("codex",), conf=T0943_CONF)
    assert _apply(engine, root, _global_dir(tmp_path)) == (
        ".codex/config.toml model_auto_compact_token_limit 180000 → 535800",)
    assert len(calls) == 1
    entry = json.loads((root / local_conf.CTX_BUDGET_RELPATH).read_text(encoding="utf-8"))["harnesses"]["codex"]
    assert entry["models"] == [T0943_CODEX_ROW]
    assert (entry["C"], entry["E"], entry["P"]) == (570000, 570000, 535800)


@pytest.mark.parametrize("case", ["missing-row", "above-limit"])
def test_t0943_codex_refuses_without_writing(local_conf, tmp_path, monkeypatch, case):
    """AT-004 — 캐시에 모델 행이 없거나 P 가 실효 창의 90% 를 넘으면 거부, 어떤 파일도 바뀌지 않는다."""
    engine = _load("pm_import")
    monkeypatch.setattr(engine, "_ctx_native_protocol", _t0943_codex_protocol([]))
    rows = [{**_T0943_CODEX_CACHE_ROW, "slug": "gpt-6.1"}] if case == "missing-row" else [_T0943_CODEX_CACHE_ROW]
    _t0943_codex_cache(tmp_path, monkeypatch, rows)
    root = _project(tmp_path, harnesses=("codex",), conf=T0943_CONF)
    before = _t0943_watched(root)
    with pytest.raises(engine.HarnessCtxError) as caught:
        # 95% → P 541,500 > 600,000 × 90% = 540,000
        engine._apply_harness_ctx(root, write=True, opencode_config_dir=_global_dir(tmp_path),
                                  updates={"ctx.compact_at_pct": "94" if case == "missing-row" else "95"})
    assert _t0943_watched(root) == before
    expected = "gpt-6.1-sol 행이 없거나" if case == "missing-row" else "541500"
    assert expected in str(caught.value), caught.value


class _T0943OllamaShow:
    """Ollama `/api/show` 대역 — 루프백 주소에서 모델별 context 길이만 답한다(외부 망 없음)."""

    def __init__(self, contexts: dict) -> None:
        import http.server
        import threading
        contexts_by_model = contexts

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (표준 라이브러리 계약)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.dumps({"model_info": {
                    "general.architecture": "fixture",
                    "fixture.context_length": contexts_by_model[body["model"]]}}).encode("utf-8")
                self.send_response(200 if self.path == "/api/show" else 404)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _t0943_opencode_tree(tmp_path: Path, monkeypatch, origin: str) -> tuple[Path, Path]:
    """대상 모델 5개 — Ollama 2개(전역 경로 설정) · models.dev 목록 3개."""
    cache = tmp_path / "cache"
    (cache / "opencode").mkdir(parents=True)
    (cache / "opencode" / "models.json").write_text(json.dumps({
        "openrouter": {"api": "https://openrouter.ai/api/v1", "models": {
            "anthropic/claude-x": {"limit": {"context": 400000, "input": 272000, "output": 128000}},
            "z-ai/glm-x": {"limit": {"context": 300000, "output": 64000}}}},
        "opencode": {"models": {"big-pickle": {"limit": {"context": 1000000, "output": 128000}}}}}),
        encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    global_dir = _global_dir(tmp_path, {"opencode.json": json.dumps({"provider": {
        "ollama": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": origin},
                   "models": {"qwen3.8:27b-unc": {}}},
        "openrouter": {"models": {"anthropic/claude-x": {"limit": {"context": 400000, "output": 16000}},
                                  "z-ai/glm-x": {}}},
        "opencode": {"models": {"big-pickle": {}}}}})})
    root = _project(tmp_path, harnesses=("opencode",), conf=T0943_CONF)
    return root, global_dir


@pytest.mark.parametrize("output_max", [None, "8000"])
def test_t0943_opencode_target_minimum_and_output_cap(local_conf, tmp_path, monkeypatch, output_max):
    """AT-005 — 대상 모델 5개의 C 최솟값(qwen 262,144 − 32,000 = 230,144)으로 E 200,000·P 188,000,
    reserved 12,000, 모든 `limit.input` 200,000. 출력 상한 환경변수는 C 에 반영된다. CLI 는 부르지 않는다."""
    engine = _load("pm_import")
    monkeypatch.setattr(engine, "_ctx_native_protocol", lambda *args: pytest.fail("opencode 는 CLI 를 부르지 않는다"))
    if output_max:
        monkeypatch.setenv("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX", output_max)
    ollama = _T0943OllamaShow({"glm-5.2:cloud": 1048576, "qwen3.8:27b-unc": 262144})
    try:
        root, global_dir = _t0943_opencode_tree(tmp_path, monkeypatch, ollama.origin)
        _apply(engine, root, global_dir)
    finally:
        ollama.close()
    entry = json.loads((root / local_conf.CTX_BUDGET_RELPATH).read_text(encoding="utf-8"))["harnesses"]["opencode"]
    output = int(output_max or 32000)
    capacities = {f"{row['provider']}/{row['id']}": row["C"] for row in entry["models"]}
    assert capacities == {
        "ollama/glm-5.2:cloud": 1048576 - output, "ollama/qwen3.8:27b-unc": 262144 - output,
        "openrouter/anthropic/claude-x": 272000, "openrouter/z-ai/glm-x": 300000 - output,
        "opencode/big-pickle": 1000000 - output}
    assert entry["C"] == 262144 - output and (entry["E"], entry["P"]) == (200000, 188000)
    document = _jsonc(pm_import=engine, path=root / ".opencode/opencode.jsonc")
    assert document["compaction"]["reserved"] == 12000
    limits = [model["limit"] for provider in document["provider"].values() for model in provider["models"].values()]
    assert len(limits) == 5 and all(limit["input"] == 200000 for limit in limits)
    assert document["provider"]["ollama"]["models"]["qwen3.8:27b-unc"]["limit"] == {
        "context": 262144, "input": 200000, "output": 32000}
    text = local_conf.ctx_budget_status(root, local_conf.load(root / ".project_manager/local.conf").values)[
        "opencode"]["basis_text"]
    assert text == f"ollama/qwen3.8:27b-unc · 창 262,144 − 출력 {output:,} ({ollama.origin})"


def test_t0943_opencode_one_unread_model_refuses_all(local_conf, tmp_path, monkeypatch):
    """AT-005 — 대상 모델 하나라도 C 를 읽지 못하면 적용 전체를 거부한다(어떤 파일도 바뀌지 않는다)."""
    engine = _load("pm_import")
    ollama = _T0943OllamaShow({"glm-5.2:cloud": 1048576, "qwen3.8:27b-unc": 262144})
    try:
        root, global_dir = _t0943_opencode_tree(tmp_path, monkeypatch, ollama.origin)
        (global_dir / "opencode.jsonc").write_text('{"provider": {"lmstudio": {"models": {"local-x": {}}}}}',
                                                   encoding="utf-8")
        before = _t0943_watched(root)
        with pytest.raises(engine.HarnessCtxError) as caught:
            _apply(engine, root, global_dir)
    finally:
        ollama.close()
    assert "lmstudio/local-x" in str(caught.value) and _t0943_watched(root) == before
