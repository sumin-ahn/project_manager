"""render 엔진 단위 테스트 (T-0131·ADR-0028·ADR-0031·§3.2~3.4).

세 표면을 합성 입력(실 템플릿/manifest 무의존)으로 검증한다:
  1. pm_render.render_adapter — operational plain replace·leak raise·미해소 토큰 표면화.
     (free-form value-fill 기계 = overlay/slot-fill/conditional-omit 은 ADR-0031 로 제거됨 —
      free-form 은 pm_import FILL 채널이 canonical home 에서 전담.)
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


# ── 1. render_adapter — operational plain replace ───────────────────────────

def test_operational_plain_replace_no_omit(pm_render):
    """operational 토큰은 plain replace(omit 없음·값으로 치환·host 행 유지)."""
    tpl = "session for {{PROJECT_NAME}} runs {{TEST_CMD}}\n"
    out = pm_render.render_adapter(
        tpl, operational={"PROJECT_NAME": "acme", "TEST_CMD": "pytest -q"})
    assert out == "session for acme runs pytest -q\n"


def test_operational_token_alone_not_dropped(pm_render):
    """operational 토큰 단독 행은 drop 대상 아님(plain replace·host 행 유지)."""
    tpl = "{{PROJECT_ROOT}}\n"
    out = pm_render.render_adapter(tpl, operational={"PROJECT_ROOT": "/repo"})
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
        pm_render.render_adapter(tpl, operational={"PROJECT_NAME": "acme"})


def test_multiple_operational_tokens_one_line_both_resolved(pm_render):
    """한 행의 operational 토큰 2개 모두 plain replace 로 해소(whole-text 패스·잔여 토큰 0)."""
    tpl = "{{PROJECT_NAME}}: {{PROJECT_ROOT}}\n"
    out = pm_render.render_adapter(
        tpl, operational={"PROJECT_NAME": "acme", "PROJECT_ROOT": "/r"})
    assert out == "acme: /r\n"
    assert "{{" not in out


def test_opencode_pro_model_in_operational_keys(pm_render):
    """OPENCODE_PRO_MODEL ∈ OPERATIONAL_KEYS — opencode 어댑터 토큰이 operational 채널에 배선됨(T-0133)."""
    assert "OPENCODE_PRO_MODEL" in pm_render.OPERATIONAL_KEYS


def test_opencode_pro_model_operational_resolved(pm_render):
    """operational 에 OPENCODE_PRO_MODEL 공급 → `{{OPENCODE_PRO_MODEL}}` plain replace 해소(leak 0)."""
    tpl = "pro model: {{OPENCODE_PRO_MODEL}}\n"
    out = pm_render.render_adapter(
        tpl, operational={"OPENCODE_PRO_MODEL": "anthropic/claude-opus-4"})
    assert out == "pro model: anthropic/claude-opus-4\n"
    assert "{{" not in out


# ── 1. render_adapter — leak assertion ──────────────────────────────────────

def test_leak_raises_on_unknown_token(pm_render):
    """allow-list 밖 토큰(`{{FOO}}`)이 산출물에 잔존하면 RenderLeakError."""
    tpl = "value: {{FOO}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl)
    assert "{{FOO}}" in str(exc.value)


def test_render_file_leak_reports_source(pm_render, tmp_path):
    """render_file 의 leak 에러에 source 파일 경로가 실린다(진단)."""
    p = tmp_path / "developer.md"
    p.write_text("v: {{UNKNOWN}}\n", encoding="utf-8")
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_file(p)
    assert str(p) in str(exc.value)
    assert "{{UNKNOWN}}" in str(exc.value)


def test_stray_omit_marker_raises(pm_render):
    """옛 free-form drop-section 마커(ADR-0031 제거)가 잔존하면 RenderLeakError(미마이그 표면화).

    어댑터는 free-form-free(ADR-0030)라 `<!-- pm:omit-if-empty ... -->` 류 마커가 절대 없어야
    한다 — 잔존하면 미마이그레이션 신호로 무음 출하를 막는다(_assert_no_leak·stray 검출).
    """
    tpl = "body\n<!-- /pm:omit-if-empty -->\ntail\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl)
    assert "omit-marker" in str(exc.value)


def test_unfilled_operational_token_is_leak_strict(pm_render):
    """operational 토큰을 안 채우면(값 미공급) 잔여 리터럴 → RenderLeakError(엄격·자족 산출물).

    이전 allow-list 는 미해소 operational 을 통과시켰으나, ADR-0028 자족 산출물 = 토큰 0 이므로
    이제 미해소 토큰은 *침묵 출하 대신* 큰소리로 표면화한다(D17-2 forward-flag 의 fail-loud 근거).
    """
    tpl = "host: {{PROJECT_NAME}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={})
    assert "{{PROJECT_NAME}}" in str(exc.value)


def test_filled_operational_token_no_leak(pm_render):
    """operational 토큰을 값으로 채우면 잔여 0 → leak 아님(plain replace 정상 경로)."""
    tpl = "host: {{PROJECT_NAME}}\n"
    out = pm_render.render_adapter(tpl, operational={"PROJECT_NAME": "acme"})
    assert out == "host: acme\n"
    assert "{{" not in out


def test_freeform_token_left_literal_is_leak(pm_render):
    """free-form 토큰(`{{PROTECTED_PATHS}}` 등)은 이 엔진이 채우지 않으므로 잔존 → RenderLeakError.

    ADR-0031 로 free-form value-fill 기계가 제거됐다 — free-form 은 pm_import FILL 채널이
    canonical home 에서 전담하므로 어댑터엔 free-form 토큰이 없어야 한다(ADR-0030 free-form-free).
    잔존하면 엄격 가드가 자족 위반(미마이그레이션)으로 표면화한다.
    """
    tpl = "보호: {{PROTECTED_PATHS}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={"PROJECT_NAME": "acme"})
    assert "{{PROTECTED_PATHS}}" in str(exc.value)


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


# ── 2. plan/apply render 분기 (합성·operational 채널) ───────────────────────

def _seed_render_dest(dest_root: Path, local_conf: str | None = None) -> None:
    pm = dest_root / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
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
    (src / rel).write_text("- {{PROJECT_NAME}}\nbody\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    # dst 에 *렌더 산출물* 을 미리 둔다 — 같으면 change 없어야.
    (dst / ".claude/agents").mkdir(parents=True)
    (dst / rel).write_text("- acme\nbody\n", encoding="utf-8")

    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    # 템플릿("- {{PROJECT_NAME}}\nbody\n") != dst, 그러나 *렌더 산출물* == dst → 변경 없음.
    rendered_paths = [c for c in changes if c[0] == rel]
    assert rendered_paths == [], f"render path 가 오보로 update 처리됨: {changes}"


def test_plan_render_path_update_when_output_differs(pm_update, tmp_path):
    """render 산출물이 dst 와 다르면 update 로 잡힌다(정직 판정)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("- {{PROJECT_NAME}}\nbody\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
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
    (src / rel).write_text("- {{PROJECT_NAME}}\n- {{PROJECT_ROOT}}\nbody\n",
                           encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=acme\nproject_root=/r\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    # operational 토큰 둘 다 해소·잔여 토큰 0.
    assert written == "- acme\n- /r\nbody\n"
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
    (src / md_rel).write_text("- {{PROJECT_NAME}}\nbody\n", encoding="utf-8")
    # 비-.md 파일은 토큰처럼 보이는 텍스트를 담아도 render 안 됨(byte-copy·자족 .md 아님).
    (src / json_rel).write_text('{"k": "{{PROJECT_NAME}}"}\n', encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
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
    assert (dst / md_rel).read_text(encoding="utf-8") == "- acme\nbody\n"
    assert (dst / json_rel).read_text(encoding="utf-8") == '{"k": "{{PROJECT_NAME}}"}\n'


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
    """@render manifest path 0 → 검사 대상 0 → 무발화 (트리 게이트가 아닌 *본 로직* 검증).

    렌더 산출물 트리(local.conf 존재)로 두어 트리 게이트(T-0170)를 통과시킨 뒤, manifest 에
    @render 항목이 없음 자체로 무발화함을 본다 — local.conf 부재 면제와 *별개*인 경로.
    """
    # manifest 에 @render 항목이 없는 합성 repo 로 REPO 를 가리킨다.
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".project_manager/tools/board.py\n.claude/agents\n", encoding="utf-8")
    # local.conf 존재 → 트리 게이트 통과(이 무발화는 @render 부재 때문이지 트리 면제가 아님).
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    # 토큰을 가진 어댑터가 있어도 @render 가 아니므로 검사 안 함.
    adapter = fake_repo / ".claude/agents/developer.md"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("- {{PROTECTED_PATHS}}\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", fake_repo)
    assert board.lint_render_leak() == []


def test_render_leak_flags_token_in_render_managed_path(board, monkeypatch, tmp_path):
    """@render 활성화된 path 산출물에 리터럴 `{{...}}` 잔존 → render-leak finding(blocking).

    렌더 산출물 트리(채택 인스턴스)임을 local.conf 존재로 표시한다 — 트리 게이트(T-0170)는
    local.conf 부재 트리(토큰-form 소스·① canonical)만 면제하고, 산출물 트리의 leak 발화는 보존.
    """
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".claude/agents @render\n", encoding="utf-8")
    # local.conf 존재 → 채택 인스턴스(render 산출물 트리·트리 게이트 통과해 실 leak 검사).
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
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
    """@render path 라도 잔여 토큰이 없으면(완전 렌더) finding 0 (산출물 트리에서 검증).

    렌더 산출물 트리(local.conf 존재)로 트리 게이트(T-0170)를 통과시킨 뒤, 완전 렌더된 어댑터엔
    토큰이 없어 무발화함을 본다 — 트리 면제가 아닌 본 leak-스캔 경로.
    """
    fake_repo = tmp_path / "repo"
    m = fake_repo / ".project_manager" / "engine.manifest"
    m.parent.mkdir(parents=True)
    m.write_text(".claude/agents @render\n", encoding="utf-8")
    # local.conf 존재 → 산출물 트리(트리 게이트 통과·실 스캔이 토큰 0 으로 무발화).
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
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
    # 토큰-form 소스 (templates/ 의 어댑터처럼 operational 토큰 보유).
    src_text = "- {{PROJECT_NAME}}\nbody\n"
    (src / rel).write_text(src_text, encoding="utf-8")
    # dst(=템플릿 타깃)엔 local.conf 없음 — 렌더 시 leak 날 환경.
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
    (src / rel).write_text("- {{PROJECT_NAME}}\nbody\n", encoding="utf-8")
    # 채택자 dest — local.conf(operational) 보유.
    _seed_render_dest(dst, local_conf="project_name=acme\n")
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
    # PROJECT_NAME(local.conf) 해소·잔여 토큰 0.
    assert written == "- acme\nbody\n"
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
