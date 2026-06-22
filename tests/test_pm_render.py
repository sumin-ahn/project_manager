"""render 엔진 단위 테스트 (T-0131·ADR-0028·§3.2~3.4).

세 표면을 합성 입력(실 템플릿/manifest 무의존)으로 검증한다:
  1. pm_render.render_adapter — slot-fill(단/멀티라인)·drop-line·drop-section·marker strip·
     operational plain·leak raise·overlay 부재→전 omit.
  2. pm_update.read_manifest(@render 파싱·후방호환) + plan/apply render 분기(합성 manifest).
  3. board.lint_render_leak — @render 산출물 한정·활성화 전 무발화·blocking(advisory 밖).

실 트리/네트워크 무의존 — tmp_path 합성 + 모듈 직접 로드(다른 엔진 테스트 패턴 동형).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_render():
    return _load("pm_render")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


@pytest.fixture(scope="module")
def board():
    return _load("board")


# ── 1. render_adapter — slot-fill ──────────────────────────────────────────

def test_slot_fill_single_line(pm_render):
    """단일라인 free-form 값 → 토큰이 값으로 치환(host 행 유지)."""
    tpl = "- **보호 영역 수정** — {{PROTECTED_PATHS}}\n"
    out = pm_render.render_adapter(tpl, {"PROTECTED_PATHS": "src/core/**"})
    assert out == "- **보호 영역 수정** — src/core/**\n"


def test_slot_fill_multiline_indents_at_token_column(pm_render):
    """멀티라인 값 → 2번째 줄 이후가 토큰 컬럼(들여쓰기)에 정렬된다."""
    tpl = "    {{PROJECT_CONSTRAINTS}}\n"
    val = "- 핵심 결정 = 순수 코드\n- 분석 계층 = 보조"
    out = pm_render.render_adapter(tpl, {"PROJECT_CONSTRAINTS": val})
    assert out == (
        "    - 핵심 결정 = 순수 코드\n"
        "    - 분석 계층 = 보조\n"
    )


# ── 1. render_adapter — conditional-omit (drop-line) ────────────────────────

def test_drop_line_when_value_empty(pm_render):
    """빈 값(overlay 에 빈 문자열) → host 행 drop."""
    tpl = "keep above\n- {{PROTECTED_PATHS}}\nkeep below\n"
    out = pm_render.render_adapter(tpl, {"PROTECTED_PATHS": ""})
    assert out == "keep above\nkeep below\n"


def test_drop_line_when_key_absent(pm_render):
    """overlay 에 key 부재 → host 행 drop(빈 값과 동일)."""
    tpl = "keep above\n- {{USER_GATE_ITEMS}}\nkeep below\n"
    out = pm_render.render_adapter(tpl, {})
    assert out == "keep above\nkeep below\n"


def test_overlay_absent_omits_all_freeform_hosts(pm_render):
    """overlay 부재(빈 dict) → 모든 free-form host 가 omit(깨끗한 출하-기본)."""
    tpl = (
        "intro\n"
        "- {{PROJECT_CONSTRAINTS}}\n"
        "- {{PROTECTED_PATHS}}\n"
        "- {{USER_GATE_ITEMS}}\n"
        "outro\n"
    )
    out = pm_render.render_adapter(tpl, {})
    assert out == "intro\noutro\n"


# ── 1. render_adapter — drop-section + marker strip ─────────────────────────

def test_drop_section_when_empty(pm_render):
    """짝 마커 span — 빈 key 면 span 통째 drop(마커 줄도 사라짐)."""
    tpl = (
        "before\n"
        "<!-- pm:omit-if-empty PROTECTED_PATHS -->\n"
        "### 보호 영역\n"
        "{{PROTECTED_PATHS}}\n"
        "<!-- /pm:omit-if-empty -->\n"
        "after\n"
    )
    out = pm_render.render_adapter(tpl, {})
    assert out == "before\nafter\n"


def test_drop_section_keeps_inner_strips_markers_when_filled(pm_render):
    """값 있으면 안쪽 유지 + 마커 줄만 strip(렌더-제어 전용은 출하물에서 항상 제거)."""
    tpl = (
        "before\n"
        "<!-- pm:omit-if-empty PROTECTED_PATHS -->\n"
        "### 보호 영역\n"
        "{{PROTECTED_PATHS}}\n"
        "<!-- /pm:omit-if-empty -->\n"
        "after\n"
    )
    out = pm_render.render_adapter(tpl, {"PROTECTED_PATHS": "ops/limits.py"})
    assert out == (
        "before\n"
        "### 보호 영역\n"
        "ops/limits.py\n"
        "after\n"
    )


# ── 1. render_adapter — operational plain replace ───────────────────────────

def test_operational_plain_replace_no_omit(pm_render):
    """operational 토큰은 plain replace(omit 없음·빈 값이어도 host 행 유지)."""
    tpl = "session for {{PROJECT_NAME}} runs {{TEST_CMD}}\n"
    out = pm_render.render_adapter(
        tpl, overlay={}, operational={"PROJECT_NAME": "acme", "TEST_CMD": "pytest -q"})
    assert out == "session for acme runs pytest -q\n"


def test_operational_token_alone_not_dropped(pm_render):
    """operational 토큰 단독 행은 free-form 이 아니므로 drop 대상 아님(plain replace)."""
    tpl = "{{PROJECT_ROOT}}\n"
    out = pm_render.render_adapter(tpl, overlay={}, operational={"PROJECT_ROOT": "/repo"})
    assert out == "/repo\n"


def test_operational_missing_key_leaks_not_silently_emptied(pm_render):
    """OPERATIONAL_KEYS 에 있으나 operational dict 에 부재인 키는 빈 문자열로 silently 치환하지
    않고 토큰을 남겨 RenderLeakError 로 잡는다 (codex·침묵 비움 금지).

    회귀: `_fill_operational` 가 `.get(key, "")` 였을 때, 기존 opencode 채택자의 local.conf 가
    `opencode_pro_model` 미보유면 `model: {{OPENCODE_PRO_MODEL}}` 이 `model: ` 로 *조용히 비워져*
    첫 @render pm_update 가 기존 모델 설정을 덮었다. 미보유 키는 잔존→leak 으로 표면화해야 한다.
    """
    tpl = "model: {{OPENCODE_PRO_MODEL}}\n"
    with pytest.raises(pm_render.RenderLeakError):
        # operational 에 다른 키만 보유·OPENCODE_PRO_MODEL 부재 → 빈 치환 아닌 leak.
        pm_render.render_adapter(tpl, overlay={}, operational={"PROJECT_NAME": "acme"})


def test_freeform_and_operational_one_pass(pm_render):
    """free-form(slot/omit) + operational(plain) 한 pass 공존."""
    tpl = (
        "root: {{PROJECT_ROOT}}\n"
        "- {{PROTECTED_PATHS}}\n"
        "- {{USER_GATE_ITEMS}}\n"
    )
    out = pm_render.render_adapter(
        tpl,
        overlay={"PROTECTED_PATHS": "core/**"},
        operational={"PROJECT_ROOT": "/r"},
    )
    assert out == "root: /r\n- core/**\n"


def test_freeform_and_operational_same_line_both_resolved(pm_render):
    """operational+free-form 이 *같은 행* 에 공존해도 둘 다 해소(whole-text 최종 패스·내부 should-fix).

    이전엔 free-form slot 행에 operational plain-replace 가 안 닿아 operational 이 리터럴로
    남았다(`{{PROJECT_NAME}}: core/**`). render_adapter 가 루프 후 결과 전체에 operational
    최종 1회 패스를 적용하므로 이제 균일 해소된다(잔여 토큰 0).
    """
    tpl = "{{PROJECT_NAME}}: {{PROTECTED_PATHS}}\n"
    out = pm_render.render_adapter(
        tpl,
        overlay={"PROTECTED_PATHS": "core/**"},
        operational={"PROJECT_NAME": "acme"},
    )
    assert out == "acme: core/**\n"
    assert "{{" not in out


def test_operational_in_kept_drop_section_resolved(pm_render):
    """drop-section 안쪽(값 있어 keep)의 operational 토큰도 whole-text 최종 패스로 해소된다."""
    tpl = (
        "<!-- pm:omit-if-empty PROTECTED_PATHS -->\n"
        "root {{PROJECT_ROOT}} guards {{PROTECTED_PATHS}}\n"
        "<!-- /pm:omit-if-empty -->\n"
    )
    out = pm_render.render_adapter(
        tpl,
        overlay={"PROTECTED_PATHS": "core/**"},
        operational={"PROJECT_ROOT": "/repo"},
    )
    assert out == "root /repo guards core/**\n"
    assert "{{" not in out


def test_opencode_pro_model_in_operational_keys(pm_render):
    """OPENCODE_PRO_MODEL ∈ OPERATIONAL_KEYS — opencode 어댑터 토큰이 operational 채널에 배선됨(T-0133)."""
    assert "OPENCODE_PRO_MODEL" in pm_render.OPERATIONAL_KEYS


def test_opencode_pro_model_operational_resolved(pm_render):
    """operational 에 OPENCODE_PRO_MODEL 공급 → `{{OPENCODE_PRO_MODEL}}` plain replace 해소(leak 0)."""
    tpl = "pro model: {{OPENCODE_PRO_MODEL}}\n"
    out = pm_render.render_adapter(
        tpl, overlay={}, operational={"OPENCODE_PRO_MODEL": "anthropic/claude-opus-4"})
    assert out == "pro model: anthropic/claude-opus-4\n"
    assert "{{" not in out


# ── 1. render_adapter — leak assertion ──────────────────────────────────────

def test_leak_raises_on_unknown_token(pm_render):
    """allow-list 밖 토큰(`{{FOO}}`)이 산출물에 잔존하면 RenderLeakError."""
    tpl = "value: {{FOO}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, {})
    assert "{{FOO}}" in str(exc.value)


def test_render_file_leak_reports_source(pm_render, tmp_path):
    """render_file 의 leak 에러에 source 파일 경로가 실린다(진단)."""
    p = tmp_path / "developer.md"
    p.write_text("v: {{UNKNOWN}}\n", encoding="utf-8")
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_file(p)
    assert str(p) in str(exc.value)
    assert "{{UNKNOWN}}" in str(exc.value)


def test_stray_close_marker_raises(pm_render):
    """짝 없는 close 마커가 산출물에 잔존하면 RenderLeakError(중첩/미짝 무음 출하 방지)."""
    tpl = "body\n<!-- /pm:omit-if-empty -->\ntail\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, {})
    assert "omit-marker" in str(exc.value)


def test_nested_open_marker_leftover_raises(pm_render):
    """중첩 미지원(§3.2): 안쪽 open 마커가 첫 close 로 닫혀 산출물에 잔존 → RenderLeakError.

    바깥 open..안쪽 open..close..close 에서 엔진은 *첫* close 를 짝으로 본다(중첩 미인식).
    값이 있어 span 을 keep 하면 안쪽 open 마커가 산출물에 남아 stray 로 잡힌다.
    """
    tpl = (
        "<!-- pm:omit-if-empty PROTECTED_PATHS -->\n"
        "keep {{PROTECTED_PATHS}}\n"
        "<!-- pm:omit-if-empty USER_GATE_ITEMS -->\n"
        "<!-- /pm:omit-if-empty -->\n"
        "<!-- /pm:omit-if-empty -->\n"
    )
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, {"PROTECTED_PATHS": "core/**"})
    assert "omit-marker" in str(exc.value)


def test_drop_section_inner_token_renders_via_recursion(pm_render):
    """drop-section 안쪽의 free-form 토큰도 재귀 렌더(slot-fill/omit)된다."""
    tpl = (
        "<!-- pm:omit-if-empty PROTECTED_PATHS -->\n"
        "head {{PROTECTED_PATHS}}\n"
        "- {{USER_GATE_ITEMS}}\n"
        "<!-- /pm:omit-if-empty -->\n"
    )
    # PROTECTED_PATHS 채워짐(span keep) · 안쪽 USER_GATE_ITEMS 부재 → 그 host 행 omit.
    out = pm_render.render_adapter(tpl, {"PROTECTED_PATHS": "core/**"})
    assert out == "head core/**\n"


def test_unfilled_operational_token_is_leak_strict(pm_render):
    """operational 토큰을 안 채우면(값 미공급) 잔여 리터럴 → RenderLeakError(엄격·자족 산출물).

    이전 allow-list 는 미해소 operational 을 통과시켰으나, ADR-0028 자족 산출물 = 토큰 0 이므로
    이제 미해소 토큰은 *침묵 출하 대신* 큰소리로 표면화한다(D17-2 forward-flag 의 fail-loud 근거).
    """
    tpl = "host: {{PROJECT_NAME}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, overlay={}, operational={})
    assert "{{PROJECT_NAME}}" in str(exc.value)


def test_filled_operational_token_no_leak(pm_render):
    """operational 토큰을 값으로 채우면 잔여 0 → leak 아님(plain replace 정상 경로)."""
    tpl = "host: {{PROJECT_NAME}}\n"
    out = pm_render.render_adapter(
        tpl, overlay={}, operational={"PROJECT_NAME": "acme"})
    assert out == "host: acme\n"
    assert "{{" not in out


def test_multiple_freeform_tokens_one_line_left_literal_is_leak(pm_render):
    """한 행에 free-form 토큰 2개면 host 모호 → slot/omit 안 함 → 리터럴 잔존 → RenderLeakError.

    재저작 규율(T-0130)상 정상 입력이 아니다 — 엔진이 추론하지 않고(§3.2), 잔존 토큰은 엄격
    가드가 자족 위반으로 잡는다(allow-list 폐지 후 더는 무음 통과 안 됨).
    """
    tpl = "{{PROTECTED_PATHS}} and {{USER_GATE_ITEMS}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, {"PROTECTED_PATHS": "x", "USER_GATE_ITEMS": "y"})
    # 단일 토큰이 아니므로 slot-fill 안 함 → 둘 다 리터럴로 남아 leak.
    assert "{{PROTECTED_PATHS}}" in str(exc.value)
    assert "{{USER_GATE_ITEMS}}" in str(exc.value)


# ── 1. load_overlay ─────────────────────────────────────────────────────────

def test_load_overlay_missing_returns_empty(pm_render, tmp_path):
    assert pm_render.load_overlay(tmp_path) == {}


def test_load_overlay_reads_yaml(pm_render, tmp_path):
    overlay = tmp_path / ".project_manager" / "overlay.local.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        "PROTECTED_PATHS: 'core/**'\n"
        "PROJECT_CONSTRAINTS: |\n  - a\n  - b\n",
        encoding="utf-8",
    )
    data = pm_render.load_overlay(tmp_path)
    assert data["PROTECTED_PATHS"] == "core/**"
    assert data["PROJECT_CONSTRAINTS"].strip() == "- a\n- b"


def test_load_overlay_non_dict_returns_empty(pm_render, tmp_path):
    """yaml 이 list/scalar 면 {}(방어·free-form host omit)."""
    overlay = tmp_path / ".project_manager" / "overlay.local.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert pm_render.load_overlay(tmp_path) == {}


# ── 2. read_manifest @render 파싱 (후방호환) ────────────────────────────────

def _write_manifest(root: Path, lines: list[str]) -> Path:
    m = root / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m


def test_read_manifest_render_flag_parsed(pm_update, tmp_path):
    """`  @render` 태그 → 그 항목 .render=True, 순수 경로만 값으로 남는다."""
    m = _write_manifest(tmp_path, [
        "# comment",
        ".project_manager/tools/board.py",
        ".claude/agents    @render",
    ])
    entries = pm_update.read_manifest(m)
    assert ".project_manager/tools/board.py" in entries  # str 동작 유지(후방호환)
    assert ".claude/agents" in entries  # @render 태그 떼고 순수 경로
    by_path = {str(e): e.render for e in entries}
    assert by_path[".project_manager/tools/board.py"] is False
    assert by_path[".claude/agents"] is True


def test_read_manifest_backcompat_str_ops(pm_update, tmp_path):
    """ManifestEntry 가 str 처럼 동작 — startswith/in/== (기존 테스트 계약 미파괴)."""
    m = _write_manifest(tmp_path, [".claude/agents @render", ".github/workflows/x.yml"])
    entries = pm_update.read_manifest(m)
    claude = [e for e in entries if e.startswith(".claude/")]
    assert claude == [".claude/agents"]
    assert all(isinstance(e, str) for e in entries)


# ── 2. plan/apply render 분기 (합성) ────────────────────────────────────────

def _seed_render_dest(dest_root: Path, overlay_yaml: str | None = None,
                      local_conf: str | None = None) -> None:
    pm = dest_root / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    if overlay_yaml is not None:
        (pm / "overlay.local.yaml").write_text(overlay_yaml, encoding="utf-8")
    if local_conf is not None:
        (pm / "local.conf").write_text(local_conf, encoding="utf-8")


def test_plan_non_render_uses_copy_semantics(pm_update, tmp_path):
    """@render 없는 항목(평문 str manifest)은 filecmp 기반 — 후방호환(byte-copy)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / ".project_manager/tools").mkdir(parents=True)
    (src / ".project_manager/tools/board.py").write_text("# new\n", encoding="utf-8")
    (dst / ".project_manager/tools").mkdir(parents=True)
    (dst / ".project_manager/tools/board.py").write_text("# old\n", encoding="utf-8")
    # 평문 str manifest (레거시 호출) → render=False.
    changes, missing = pm_update.plan(src, [".project_manager/tools/board.py"], dest_root=dst)
    assert missing == []
    assert len(changes) == 1
    assert changes[0][3] == "update"
    assert getattr(changes[0][2], "render", False) is False


def test_plan_render_path_compares_rendered_output(pm_update, tmp_path):
    """render path: dst 가 *렌더 산출물* 과 같으면 변경 없음(템플릿≠산출물 오보 회피·§3.3)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("- {{PROTECTED_PATHS}}\nbody\n", encoding="utf-8")
    _seed_render_dest(dst, overlay_yaml="PROTECTED_PATHS: 'core/**'\n")
    # dst 에 *렌더 산출물* 을 미리 둔다 — 같으면 change 없어야.
    (dst / ".claude/agents").mkdir(parents=True)
    (dst / rel).write_text("- core/**\nbody\n", encoding="utf-8")

    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    # 템플릿("- {{PROTECTED_PATHS}}\nbody\n") != dst, 그러나 *렌더 산출물* == dst → 변경 없음.
    rendered_paths = [c for c in changes if c[0] == rel]
    assert rendered_paths == [], f"render path 가 오보로 update 처리됨: {changes}"


def test_plan_render_path_update_when_output_differs(pm_update, tmp_path):
    """render 산출물이 dst 와 다르면 update 로 잡힌다(정직 판정)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("- {{PROTECTED_PATHS}}\nbody\n", encoding="utf-8")
    _seed_render_dest(dst, overlay_yaml="PROTECTED_PATHS: 'core/**'\n")
    (dst / ".claude/agents").mkdir(parents=True)
    (dst / rel).write_text("- STALE\nbody\n", encoding="utf-8")

    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    target = [c for c in changes if c[0] == rel]
    assert len(target) == 1
    assert target[0][3] == "update"
    assert getattr(target[0][2], "render", False) is True


def test_apply_render_writes_rendered_output(pm_update, tmp_path):
    """apply 가 render path 를 render_adapter 산출물로 기록(byte-copy 아님)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("- {{PROTECTED_PATHS}}\n- {{USER_GATE_ITEMS}}\nbody\n",
                           encoding="utf-8")
    _seed_render_dest(dst, overlay_yaml="PROTECTED_PATHS: 'core/**'\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    # PROTECTED_PATHS 채워짐, USER_GATE_ITEMS(overlay 부재) host omit, 잔여 토큰 0.
    assert written == "- core/**\nbody\n"
    assert "{{" not in written


def test_render_dir_only_md_rendered_others_copied(pm_update, tmp_path):
    """@render 디렉토리 하위는 `.md` 만 render — 비-.md(json 등)는 byte-copy(codex suggestion).

    토큰을 담은 비-.md 파일이 render 대상이 되면 (a) 비-md 를 자족 .md 로 오인하고 (b) 엄격
    가드가 그 토큰을 leak 으로 터뜨릴 수 있다. .md 한정으로 비-.md 는 그대로 복사된다.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    md_rel = ".claude/agents/developer.md"
    json_rel = ".claude/agents/config.json"
    (src / ".claude/agents").mkdir(parents=True)
    (src / md_rel).write_text("- {{PROTECTED_PATHS}}\nbody\n", encoding="utf-8")
    # 비-.md 파일은 토큰처럼 보이는 텍스트를 담아도 render 안 됨(byte-copy·자족 .md 아님).
    (src / json_rel).write_text('{"k": "{{PROTECTED_PATHS}}"}\n', encoding="utf-8")
    _seed_render_dest(dst, overlay_yaml="PROTECTED_PATHS: 'core/**'\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    by_rel = {c[0]: c for c in changes}
    # .md → render=True, .json → render=False(copy2).
    assert getattr(by_rel[md_rel][2], "render", False) is True
    assert getattr(by_rel[json_rel][2], "render", False) is False
    pm_update.apply(changes)
    # .md 는 렌더 산출물, .json 은 byte-copy(토큰 그대로·자족 변환 안 함).
    assert (dst / md_rel).read_text(encoding="utf-8") == "- core/**\nbody\n"
    assert (dst / json_rel).read_text(encoding="utf-8") == '{"k": "{{PROTECTED_PATHS}}"}\n'


def test_apply_non_render_byte_copies(pm_update, tmp_path):
    """평문 Path dst(레거시 apply 직접 호출)는 copy2(후방호환·현 동작 불변)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_f = src / ".project_manager/tools/board.py"
    dst_f = dst / ".project_manager/tools/board.py"
    src_f.parent.mkdir(parents=True)
    src_f.write_text("# engine {{NOT_A_RENDER_TOKEN}}\n", encoding="utf-8")
    dst_f.parent.mkdir(parents=True)
    dst_f.write_text("# old\n", encoding="utf-8")
    # 평문 Path dst 로 직접 change 구성 → render 속성 없음 → copy2 그대로.
    changes = [(".project_manager/tools/board.py", src_f, dst_f, "update")]
    pm_update.apply(changes)
    # 토큰이 있어도 byte-copy(렌더 안 함) — 순수 엔진 후방호환.
    assert dst_f.read_text(encoding="utf-8") == "# engine {{NOT_A_RENDER_TOKEN}}\n"


# ── 3. board.lint_render_leak — @render 한정·활성화 전 무발화·blocking ───────

def test_render_leak_not_advisory_is_blocking(board):
    """render-leak 은 `_ADVISORY_LINT_KINDS` 밖 → `--gate` 차단(blocking)."""
    assert "render-leak" not in board._ADVISORY_LINT_KINDS


def test_render_leak_silent_when_no_render_path(board, monkeypatch, tmp_path):
    """현 트리 동형: @render manifest path 0 → 검사 대상 0 → 무발화."""
    # manifest 에 @render 항목이 없는 합성 repo 로 REPO 를 가리킨다.
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".project_manager/tools/board.py\n.claude/agents\n", encoding="utf-8")
    # 토큰을 가진 어댑터가 있어도 @render 가 아니므로 검사 안 함.
    adapter = fake_repo / ".claude/agents/developer.md"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("- {{PROTECTED_PATHS}}\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", fake_repo)
    assert board.lint_render_leak() == []


def test_render_leak_flags_token_in_render_managed_path(board, monkeypatch, tmp_path):
    """@render 활성화된 path 산출물에 리터럴 `{{...}}` 잔존 → render-leak finding(blocking)."""
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".claude/agents @render\n", encoding="utf-8")
    adapter = fake_repo / ".claude/agents/developer.md"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("- {{PROTECTED_PATHS}}\nbody\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", fake_repo)
    findings = board.lint_render_leak()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert kind == "render-leak"
    assert "{{PROTECTED_PATHS}}" in detail
    assert label.endswith("developer.md")


def test_render_leak_clean_when_render_path_fully_rendered(board, monkeypatch, tmp_path):
    """@render path 라도 잔여 토큰이 없으면(완전 렌더) finding 0."""
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".claude/agents @render\n", encoding="utf-8")
    adapter = fake_repo / ".claude/agents/developer.md"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("- core/**\nbody\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", fake_repo)
    assert board.lint_render_leak() == []


# ── 2. plan render_enabled=False (--target copy2·토큰-form 보존·T-0133) ───────
# --target(루트→templates/<name>) 은 *템플릿* manifest 를 읽는데 거기에 @render 가 있으면
# plan/apply 가 루트 어댑터를 렌더하려 든다 — 템플릿엔 local.conf 가 없어 operational 토큰이
# 미해소 leak → _assert_no_leak crash. 템플릿은 토큰-form 소스라 절대 렌더 대상이 아니므로
# --target 일 때 render_enabled=False 로 @render 를 무시하고 전부 copy2(토큰 보존)한다.

def test_plan_render_disabled_forces_copy_for_render_manifest(pm_update, tmp_path):
    """(a) --target(render_enabled=False) + @render manifest → copy2(render=False·예외 없음)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    # 토큰-form 소스 (templates/ 의 어댑터처럼 free-form+operational 토큰 보유).
    src_text = "- {{PROTECTED_PATHS}}\n- {{PROJECT_NAME}}\nbody\n"
    (src / rel).write_text(src_text, encoding="utf-8")
    # dst(=템플릿 타깃)엔 local.conf/overlay 없음 — 렌더 시 leak 날 환경.
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))

    # render_enabled=False → @render 무시·copy2. leak/crash 없이 new 변경 1건.
    changes, missing = pm_update.plan(
        src, manifest, dest_root=dst, render_enabled=False)
    assert missing == []
    target = [c for c in changes if c[0] == rel]
    assert len(target) == 1
    assert target[0][3] == "new"
    assert getattr(target[0][2], "render", False) is False
    # apply 도 copy2 — 토큰-form 이 byte 그대로 보존(렌더 안 됨).
    pm_update.apply(changes)
    assert (dst / rel).read_text(encoding="utf-8") == src_text


def test_plan_render_enabled_still_renders_for_adopter(pm_update, tmp_path):
    """(b) 비-target(render_enabled=True 기본) + @render + local.conf → render(토큰 해소·회귀)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("- {{PROTECTED_PATHS}}\n- {{PROJECT_NAME}}\nbody\n",
                           encoding="utf-8")
    # 채택자 dest — overlay(free-form) + local.conf(operational) 보유.
    _seed_render_dest(
        dst, overlay_yaml="PROTECTED_PATHS: 'core/**'\n",
        local_conf="project_name=acme\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))

    # 기본 render_enabled=True → render path (dst 가 산출물과 다르므로 render 변경).
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    target = [c for c in changes if c[0] == rel]
    assert len(target) == 1
    assert getattr(target[0][2], "render", False) is True
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    # PROTECTED_PATHS(overlay)·PROJECT_NAME(local.conf) 해소·잔여 토큰 0.
    assert written == "- core/**\n- acme\nbody\n"
    assert "{{" not in written


def test_opencode_pro_model_local_conf_mapping(pm_update, tmp_path):
    """local.conf `opencode_pro_model=...` → operational dict 의 OPENCODE_PRO_MODEL 매핑(T-0133)."""
    assert pm_update._LOCAL_CONF_TO_OPERATIONAL["opencode_pro_model"] == "OPENCODE_PRO_MODEL"
    dst = tmp_path / "dst"
    _seed_render_dest(dst, local_conf="opencode_pro_model=anthropic/claude-opus-4\n")
    operational = pm_update._operational_from_local_conf(dst)
    assert operational["OPENCODE_PRO_MODEL"] == "anthropic/claude-opus-4"


def test_opencode_pro_model_render_resolved_from_local_conf(pm_update, tmp_path):
    """apply render 가 local.conf 의 opencode_pro_model 로 `{{OPENCODE_PRO_MODEL}}` 해소(end-to-end)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".opencode/agents/architect.md"
    (src / ".opencode/agents").mkdir(parents=True)
    (src / rel).write_text("model: {{OPENCODE_PRO_MODEL}}\nbody\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="opencode_pro_model=anthropic/claude-opus-4\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".opencode/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    assert written == "model: anthropic/claude-opus-4\nbody\n"
    assert "{{" not in written
