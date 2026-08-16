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
import subprocess
from pathlib import Path

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths
from _textio import write_lf

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


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


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
    """OPERATIONAL_KEYS 에 있으나 operational dict 에 부재인 키(intentional-TODO 아님)는 빈
    문자열로 silently 치환하지 않고 토큰을 남겨 RenderLeakError 로 잡는다 (codex·침묵 비움 금지).

    회귀: `_fill_operational` 가 `.get(key, "")` 였을 때 미보유 키가 `` 로 *조용히 비워져*
    탐지 신호가 사라졌다. 미보유 키는 잔존→leak 으로 표면화해야 한다. (OPENCODE_PRO_MODEL 은
    intentional-TODO 예외라 별도 — test_opencode_pro_model_missing_graceful_todo_not_leak 참조.)
    """
    tpl = "root: {{PROJECT_ROOT}}\n"
    with pytest.raises(pm_render.RenderLeakError):
        # operational 에 다른 키만 보유·PROJECT_ROOT 부재 → 빈 치환 아닌 leak.
        pm_render.render_adapter(tpl, operational={"PROJECT_NAME": "acme"})


def test_operational_empty_value_leaks_not_silently_emptied(pm_render):
    """operational dict 에 *있되 빈 문자열* 인 키는 빈 치환하지 않고 토큰을 남겨 RenderLeakError.

    T-0218 발단(PM 49차 라이브): local.conf `project_name=` 빈값 → 렌더가 `{{PROJECT_NAME}}` 를
    `` 로 silent 치환 → description 이 " 프로젝트"(이름 빈칸)로 커밋·전파. `_assert_no_leak` 는
    *잔여 토큰* 만 보므로 통과했다. 빈값 항목은 치환하지 않아 토큰이 잔존→leak 으로 표면화해야
    한다(silent-empty = leak 클래스·렌더러 이중화·호출자 무관). 에러엔 빈값 힌트가 실린다.
    """
    tpl = "{{PROJECT_NAME}} 프로젝트\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={"PROJECT_NAME": ""})
    msg = str(exc.value)
    assert "{{PROJECT_NAME}}" in msg
    # 빈값 원인 힌트: local.conf `<key>=` 가 빈값 — 값을 채우라.
    assert "`project_name=`" in msg
    assert "빈값" in msg


def test_operational_empty_value_hint_only_for_leaked_token(pm_render):
    """빈값 키라도 그 토큰이 텍스트에 없으면 힌트에 등장하지 않는다(스퓨리어스 힌트 0).

    PROJECT_ROOT 가 빈값이지만 템플릿엔 `{{PROJECT_ROOT}}` 가 없다 → leak 아님. 반면 UNKNOWN 은
    다른 원인의 leak. 힌트는 *빈값이라 leak 된* 토큰에만 붙어야 한다(_assert_no_leak 정밀도).
    """
    tpl = "value: {{UNKNOWN}}\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(
            tpl, operational={"PROJECT_NAME": "acme", "PROJECT_ROOT": ""})
    msg = str(exc.value)
    assert "{{UNKNOWN}}" in msg
    # PROJECT_ROOT 는 빈값이나 텍스트에 없어 leak 아님 → 빈값 힌트 부재.
    assert "project_root" not in msg
    assert "빈값" not in msg


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


# ── 1. render_adapter — intentional-TODO graceful (T-0310·import↔self-update 대칭) ──

def test_opencode_pro_model_missing_graceful_todo_not_leak(pm_render):
    """OPENCODE_PRO_MODEL 미해소(local.conf 미보유)는 leak(자족 위반)이 아니라 intentional-TODO
    로 graceful 중화된다 (T-0310·불변식 c 예외·import 대칭).

    회귀 반전: @source 전파(ADR-0054) 후 opencode 채택자 self-update 가 미해소 토큰을 leak 으로
    rc-fail(update 전멸)해 릴리즈 라이브 게이트가 포착했다. 이제 import(--fill manual)와 대칭 —
    model: 줄을 주석화·토큰 제거해 자족 유지하고 채택자-fill 지점만 남긴다(rc0).
    """
    tpl = 'model: "{{OPENCODE_PRO_MODEL}}"\nbody\n'
    out = pm_render.render_adapter(tpl, operational={"PROJECT_NAME": "acme"})
    # 리터럴 토큰 제거(자족 유지) — render leak 안 남.
    assert "{{OPENCODE_PRO_MODEL}}" not in out
    # model: 줄 주석화(YAML frontmatter 에 model 키 부재 → opencode 기본 모델·T-0077).
    assert out.splitlines()[0].startswith("# model:")
    # 채택자 발견경로: 형식 힌트 + TODO 안내 + 본문 무손상.
    assert "<provider/model>" in out
    assert "TODO" in out
    assert "body" in out


def test_leak_assert_integrity_other_token_still_leaks_with_model_todo(pm_render):
    """불변식 c: OPENCODE_PRO_MODEL 이 graceful 중화돼도 산출물의 *다른* 미해소 토큰은 여전히
    leak fail-loud — intentional-TODO placeholder 만 예외(false-green 근절 유지·T-0310).
    """
    tpl = 'model: "{{OPENCODE_PRO_MODEL}}"\nvalue: {{GENUINELY_UNWIRED}}\n'
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={})
    msg = str(exc.value)
    # 진짜 미배선 토큰은 leak 목록에 올라 fail-loud.
    assert "{{GENUINELY_UNWIRED}}" in msg
    # OPENCODE_PRO_MODEL 은 중화됐으므로 leak 목록에 없다(예외만 허용).
    assert "{{OPENCODE_PRO_MODEL}}" not in msg


def test_opencode_pro_model_off_model_line_still_leaks(pm_render):
    """intentional-TODO 예외는 `model:` 필드 줄 한정 — 산문/헤더의 미해소 OPENCODE_PRO_MODEL 은
    중화 대상 아님 → 여전히 leak(진짜 미배선·불변식 c·false-green 근절)."""
    tpl = "설명: {{OPENCODE_PRO_MODEL}} 사용 예시\n"
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={})
    assert "{{OPENCODE_PRO_MODEL}}" in str(exc.value)


def test_opencode_pro_model_empty_in_conf_still_leaks(pm_render):
    """`opencode_pro_model=` 빈값(present-but-empty)은 intentional-TODO 로 중화하지 않고 leak 한다
    (T-0218 빈값 가드 보존·codex T-0310 must-fix). 부재(absent)만 graceful TODO — 빈값은 오설정 신호.

    회귀: T-0310 의 neutralize 가 `_assert_no_leak` *전에* 토큰을 제거하면 빈값 케이스가 조용히
    TODO 로 swallow 돼 T-0218 빈값-leak(값을 채우라)을 우회한다(false-green). 부재 vs 빈값을 구분해
    빈값은 leak 시켜야 한다. pm_import 는 해소 시에만 키를 쓰므로 빈값=손-편집/손상 신호.
    """
    tpl = 'model: "{{OPENCODE_PRO_MODEL}}"\n'
    # 경로 ①: operational 에 빈 문자열로 존재 → _fill_operational 가 detected_empty 로 기록.
    with pytest.raises(pm_render.RenderLeakError) as exc:
        pm_render.render_adapter(tpl, operational={"OPENCODE_PRO_MODEL": ""})
    assert "{{OPENCODE_PRO_MODEL}}" in str(exc.value)
    assert "빈값" in str(exc.value), "빈값 원인 힌트(값을 채우라)가 소실."
    # 경로 ②: 호출자(pm_update)가 local.conf `opencode_pro_model=` 를 excluded → empty_keys 로 전달.
    with pytest.raises(pm_render.RenderLeakError) as exc2:
        pm_render.render_adapter(
            tpl, operational={"PROJECT_NAME": "acme"}, empty_keys=["OPENCODE_PRO_MODEL"])
    assert "{{OPENCODE_PRO_MODEL}}" in str(exc2.value)


# ── 1. neutralize_model_todo — 공유 중화(단일 진실) 단위 ─────────────────────

def test_neutralize_model_todo_comments_and_neutralizes(pm_render):
    """model: 줄을 주석화하고 토큰을 <provider/model> 로 중화 + TODO 꼬리(가용목록 없음 형식)."""
    text = 'model: "{{OPENCODE_PRO_MODEL}}"\n'
    out, marked = pm_render.neutralize_model_todo(text)
    assert marked is True
    assert out == (
        '# model: "<provider/model>"'
        "  # TODO: opencode 모델 ID 를 넣으려면 이 줄 주석 해제 후 "
        "provider/model(예: ollama/glm-5.2:cloud) 로 치환\n"
    )


def test_neutralize_model_todo_available_inlined(pm_render):
    """조회된 가용 모델 목록은 TODO 꼬리에 인라인된다(사람이 바로 고르게)."""
    text = 'model: "{{OPENCODE_PRO_MODEL}}"\n'
    out, marked = pm_render.neutralize_model_todo(
        text, available=["ollama/glm-5.2:cloud", "anthropic/opus"])
    assert marked is True
    assert "가용: ollama/glm-5.2:cloud, anthropic/opus" in out
    assert "{{OPENCODE_PRO_MODEL}}" not in out


def test_neutralize_model_todo_noop_without_token(pm_render):
    """토큰 부재(이미 해소된 model 값)면 무동작·marked False(회귀 0)."""
    text = 'model: "anthropic/opus"\nbody\n'
    out, marked = pm_render.neutralize_model_todo(text)
    assert marked is False
    assert out == text


def test_neutralize_model_todo_idempotent(pm_render):
    """이미 주석/TODO 붙은 줄은 재처리 안 함 — 멱등(self-update 재렌더 안정)."""
    text = 'model: "{{OPENCODE_PRO_MODEL}}"\n'
    once, marked1 = pm_render.neutralize_model_todo(text)
    twice, marked2 = pm_render.neutralize_model_todo(once)
    assert marked1 is True
    assert marked2 is False
    assert twice == once


def test_neutralize_model_todo_only_model_field_line(pm_render):
    """`model:` 시작 줄만 대상 — 산문 줄의 토큰은 안 건드린다(markdown 무손상)."""
    text = "설명: {{OPENCODE_PRO_MODEL}} 예시\n"
    out, marked = pm_render.neutralize_model_todo(text)
    assert marked is False
    assert out == text


def test_render_neutralize_matches_import_fallback_output(pm_render, pm_import, tmp_path):
    """pm_import 폴백(_mark_model_todos)과 render_adapter 의 중화가 byte-동일 (단일 진실·T-0310).

    import 은 pm_render.neutralize_model_todo 에 위임하므로, 같은 model: 템플릿 줄에서 양쪽이
    같은 결과를 내야 self-update 재렌더가 spurious diff 를 안 만든다(import↔update 대칭 lock-in).
    """
    model_tpl = 'model: "{{OPENCODE_PRO_MODEL}}"\n'
    # import 폴백을 파일에 적용(비-tty·가용목록 없음 경로 = available=[]).
    dest = tmp_path / "adopter"
    rel = Path(".opencode/agents/x.md")
    f = dest / rel
    f.parent.mkdir(parents=True)
    f.write_text(model_tpl, encoding="utf-8")
    pm_import._mark_model_todos(dest, {rel}, [])
    import_out = f.read_text(encoding="utf-8")
    # render_adapter 중화(operational 미해소) — self-update 경로.
    render_out = pm_render.render_adapter(model_tpl, operational={})
    assert import_out == render_out
    assert "{{OPENCODE_PRO_MODEL}}" not in import_out


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


def _track_source_tree(root: Path) -> None:
    """directory manifest 합성 source를 실제 tracked checkout으로 만든다."""
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True,
        text=True,
        check=True,
    )


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


def test_iter_files_yields_posix_rel_keys(pm_update, tmp_path):
    """_iter_files 디렉토리 재귀 relpath 는 posix(슬래시) key — Windows 역슬래시 금지(T-0212).

    plan change 튜플 key 가 소비자/테스트(슬래시 규약)와 일치하려면 디렉토리 하위 파일 relpath
    가 OS-네이티브 `str(Path)`(Windows `\\`)가 아니라 `as_posix()` 여야 한다. 이 직접 단언이
    `.claude/agents` 같은 디렉토리 manifest 항목의 역슬래시 회귀를 못박는다(POSIX 는 항상 슬래시).
    """
    root = tmp_path / "root"
    nested = root / ".claude" / "agents"
    nested.mkdir(parents=True)
    (nested / "developer.md").write_text("x\n", encoding="utf-8")
    (nested / "reviewer.md").write_text("y\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True, text=True, check=True)
    rels = [r for r, _sp in pm_update._iter_files(root, ".claude/agents")]
    assert rels == [".claude/agents/developer.md", ".claude/agents/reviewer.md"]
    assert all("\\" not in r for r in rels)


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
    write_lf(src / rel, "- {{PROJECT_NAME}}\nbody\n")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    # dst 에 *렌더 산출물* 을 미리 둔다 — 같으면 change 없어야.
    (dst / ".claude/agents").mkdir(parents=True)
    write_lf(dst / rel, "- acme\nbody\n")

    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    _track_source_tree(src)
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
    write_lf(src / rel, "- {{PROJECT_NAME}}\nbody\n")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    (dst / ".claude/agents").mkdir(parents=True)
    write_lf(dst / rel, "- STALE\nbody\n")

    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    _track_source_tree(src)
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
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    # operational 토큰 둘 다 해소·잔여 토큰 0.
    assert written == "- acme\n- /r\nbody\n"
    assert "{{" not in written


def test_render_dir_text_files_rendered_binary_copied(pm_update, tmp_path):
    """@render 디렉토리 하위는 **텍스트면 확장자 무관 render** · 바이너리는 byte-copy (T-0424).

    옛 규칙은 `.md` 한정이었다 — 확장자 열거라, codex 가 들여온 `.codex/agents/*.toml`(@render
    선언 O)이 byte-copy 로 새어 채택자에게 `{{PROJECT_NAME}}` 리터럴을 전파했다. 이제 render
    대상 판정은 manifest `@render` 선언 + "UTF-8 텍스트로 읽히는가"(`_is_text_source`)뿐이라
    네 번째 하니스의 새 형식도 자동 편입된다. 텍스트로 못 읽는 리소스는 계속 byte-copy.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    md_rel = ".claude/agents/developer.md"
    toml_rel = ".claude/agents/developer.toml"
    bin_rel = ".claude/agents/icon.bin"
    (src / ".claude/agents").mkdir(parents=True)
    (src / md_rel).write_text("- {{PROJECT_NAME}}\nbody\n", encoding="utf-8")
    # 새 하니스 형식(TOML): @render 선언 하위라면 확장자와 무관하게 렌더된다.
    (src / toml_rel).write_text('description = "{{PROJECT_NAME}}"\n', encoding="utf-8")
    # 바이너리(UTF-8 디코드 불가) — render 대상 아님(byte-copy·자족 산출물 아님).
    (src / bin_rel).write_bytes(b"\xff\xfe\x00binary\x00")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    by_rel = {c[0]: c for c in changes}
    # 텍스트(.md·.toml) → render=True, 바이너리 → render=False(copy2).
    assert getattr(by_rel[md_rel][2], "render", False) is True
    assert getattr(by_rel[toml_rel][2], "render", False) is True
    assert getattr(by_rel[bin_rel][2], "render", False) is False
    pm_update.apply(changes)
    assert (dst / md_rel).read_text(encoding="utf-8") == "- acme\nbody\n"
    assert (dst / toml_rel).read_text(encoding="utf-8") == 'description = "acme"\n'
    assert (dst / bin_rel).read_bytes() == b"\xff\xfe\x00binary\x00"


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
    _track_source_tree(src)

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
    _track_source_tree(src)

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
    operational, empty_keys = pm_update._operational_from_local_conf(dst)
    assert operational["OPENCODE_PRO_MODEL"] == "anthropic/claude-opus-4"
    assert empty_keys == []


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
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    assert written == "model: anthropic/claude-opus-4\nbody\n"
    assert "{{" not in written


def test_opencode_pro_model_unresolved_self_update_graceful(pm_update, tmp_path):
    """self-update apply 가 opencode_pro_model *미해소*(local.conf 부재) model: 템플릿을 렌더할
    때 leak crash 하지 않고 graceful TODO 중화한다 (T-0310·@source 자기-update 근본fix).

    발단: opencode 채택자가 opencode 없이 import → local.conf 에 opencode_pro_model 부재 →
    @source 재렌더(.opencode/agents)가 `{{OPENCODE_PRO_MODEL}}` 을 leak → apply RenderLeakError
    → self-update 전체 실패(엔진 update 까지 막힘). 이제 중화·rc0 동치(라이브 게이트 포착 회귀).
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".opencode/agents/architect.md"
    (src / ".opencode/agents").mkdir(parents=True)
    (src / rel).write_text('model: "{{OPENCODE_PRO_MODEL}}"\nbody\n', encoding="utf-8")
    # 채택자 dest — local.conf 에 opencode_pro_model *부재*(해소 못 한 채택자).
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".opencode/agents @render"]))
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    # apply 가 crash 하지 않고 중화 산출을 기록(leak 0).
    pm_update.apply(changes)
    written = (dst / rel).read_text(encoding="utf-8")
    assert "{{OPENCODE_PRO_MODEL}}" not in written          # 리터럴 토큰 제거(자족)
    assert written.splitlines()[0].startswith("# model:")   # model: 줄 주석화
    assert "body" in written


def test_self_update_partial_graceful_engine_still_updates(pm_update, tmp_path):
    """불변식 b: opencode_pro_model 한 토큰 미해소가 엔진/타 파일 update 전체를 막지 않는다
    (부분-graceful·T-0310). @render 미해소 파일은 중화되고 일반 엔진 파일은 정상 update·crash 0."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    # (1) @render opencode agent — 미해소 model 토큰.
    (src / ".opencode/agents").mkdir(parents=True)
    (src / ".opencode/agents/architect.md").write_text(
        'model: "{{OPENCODE_PRO_MODEL}}"\n', encoding="utf-8")
    # (2) 일반 엔진 파일(비-@render·byte-copy).
    (src / ".project_manager/tools").mkdir(parents=True)
    (src / ".project_manager/tools/board.py").write_text("v2\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=acme\n")
    (dst / ".project_manager/tools").mkdir(parents=True, exist_ok=True)
    (dst / ".project_manager/tools/board.py").write_text("v1\n", encoding="utf-8")
    manifest = pm_update.read_manifest(_write_manifest(src, [
        ".opencode/agents @render",
        ".project_manager/tools/board.py",
    ]))
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    pm_update.apply(changes)  # 한 토큰 미해소로 전체가 막히지 않음(crash 0).
    # 엔진 파일 정상 update.
    assert (dst / ".project_manager/tools/board.py").read_text(encoding="utf-8") == "v2\n"
    # @render agent 는 graceful 중화(리터럴 토큰 0).
    agent = (dst / ".opencode/agents/architect.md").read_text(encoding="utf-8")
    assert "{{OPENCODE_PRO_MODEL}}" not in agent


# ── 4. 빈값 operational silent-empty 가드 (T-0218·pm_update·pm_import 경로) ────

def test_operational_from_local_conf_excludes_empty_value(pm_update, tmp_path):
    """local.conf `project_name=`(빈값) → operational dict 에서 제외·empty_keys 로 표면화(T-0218).

    빈값을 그대로 넘기면 렌더가 토큰을 빈 문자열로 silent 치환한다(PM 49차 " 프로젝트" 오염).
    부재와 동일 취급으로 제외 → 토큰 잔존 → render 가 leak 으로 잡는다. 정상값은 무영향(회귀).
    """
    dst = tmp_path / "dst"
    # project_name 은 빈값, py 는 정상값 — 빈값만 제외되고 정상값은 보존돼야 한다.
    _seed_render_dest(dst, local_conf="project_name=\npy=python3\n")
    operational, empty_keys = pm_update._operational_from_local_conf(dst)
    assert "PROJECT_NAME" not in operational  # 빈값 → dict 에서 제외(부재 동일 취급)
    assert empty_keys == ["PROJECT_NAME"]  # 빈값 token-key 표면화
    assert operational["PY"] == "python3"  # 정상값은 보존(회귀 무변경)


def test_operational_from_local_conf_all_normal_no_empty_keys(pm_update, tmp_path):
    """정상값만 있으면 empty_keys 는 빈 리스트(빈값 가드가 정상 경로를 오탐 안 함·회귀)."""
    dst = tmp_path / "dst"
    _seed_render_dest(dst, local_conf="project_name=acme\ntest_cmd=pytest -q\n")
    operational, empty_keys = pm_update._operational_from_local_conf(dst)
    assert operational["PROJECT_NAME"] == "acme"
    assert operational["TEST_CMD"] == "pytest -q"
    assert empty_keys == []


def test_render_text_empty_local_conf_raises_with_hint(pm_update, tmp_path):
    """빈값 local.conf 로 재렌더하면 RenderLeakError + 빈값 힌트(pm_update end-to-end·T-0218).

    발단 재현: 채택자 local.conf `project_name=` 빈값·어댑터에 `{{PROJECT_NAME}}` → 재렌더가
    silent 로 " 프로젝트" 를 쓰던 것을 이제 큰소리로 실패시킨다(값을 채우라 힌트).
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_f = src / ".claude/agents/developer.md"
    src_f.parent.mkdir(parents=True)
    src_f.write_text("description: {{PROJECT_NAME}} 프로젝트\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=\n")
    # RenderLeakError 는 RuntimeError 서브클래스 — pm_update 가 pm_render 를 격리 로드하므로
    # 클래스 동일성 대신 base + 이름으로 잡는다(모듈 인스턴스 간 클래스 객체 상이).
    with pytest.raises(RuntimeError) as exc:
        pm_update._render_text(src_f, dst)
    assert type(exc.value).__name__ == "RenderLeakError"
    msg = str(exc.value)
    assert "{{PROJECT_NAME}}" in msg
    assert "`project_name=`" in msg
    assert "빈값" in msg


def test_apply_render_empty_local_conf_raises(pm_update, tmp_path):
    """apply(render) 도 빈값 local.conf 면 hard-fail — silent 로 빈 이름을 기록하지 않는다(T-0218)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    rel = ".claude/agents/developer.md"
    (src / ".claude/agents").mkdir(parents=True)
    (src / rel).write_text("description: {{PROJECT_NAME}} 프로젝트\n", encoding="utf-8")
    _seed_render_dest(dst, local_conf="project_name=\n")
    manifest = pm_update.read_manifest(
        _write_manifest(src, [".claude/agents @render"]))
    _track_source_tree(src)
    changes, missing = pm_update.plan(src, manifest, dest_root=dst)
    assert missing == []
    with pytest.raises(RuntimeError) as exc:  # RenderLeakError (격리 로드·base 로 잡음)
        pm_update.apply(changes)
    assert type(exc.value).__name__ == "RenderLeakError"
    # silent 로 빈 이름(" 프로젝트")이 기록되지 않았음을 확인(파일 미생성 or 토큰 보존).
    assert not (dst / rel).exists()


def _seed_render_managed_tree(dest_root: Path, rel: str, body: str) -> None:
    """@render manifest(.claude/agents) + 해당 산출물 파일 — render_managed_files 대상 트리."""
    pm = dest_root / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "engine.manifest").write_text(".claude/agents @render\n", encoding="utf-8")
    f = dest_root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def test_render_managed_files_empty_sub_leaks(pm_import, tmp_path):
    """pm_import 경로(render_managed_files)도 빈값 sub → RenderLeakError(동일 가드·T-0218 DoD ③).

    subs 의 값이 빈 문자열이면 렌더가 토큰을 silent 로 비우지 않고 leak 으로 표면화해야 한다
    (호출자 무관 이중화·_fill_operational 방어). render_managed_files 는 leak 을 삼키지 않고
    전파 → 빈 이름이 파일에 기록되지 않는다.
    """
    dest = tmp_path / "adopter"
    rel = ".claude/agents/developer.md"
    _seed_render_managed_tree(dest, rel, "description: {{PROJECT_NAME}} 프로젝트\n")
    # RenderLeakError 는 RuntimeError 서브클래스 — pm_import 가 pm_render 를 격리 로드하므로
    # base + 이름으로 잡는다(모듈 인스턴스 간 클래스 객체 상이).
    with pytest.raises(RuntimeError) as exc:
        pm_import.render_managed_files(
            dest, subs={"{{PROJECT_NAME}}": ""}, copied_relpaths={Path(rel)})
    assert type(exc.value).__name__ == "RenderLeakError"
    # silent 로 빈 이름이 기록되지 않고 원본(토큰) 보존 — 파일이 " 프로젝트" 로 안 덮임.
    assert (dest / rel).read_text(encoding="utf-8") == "description: {{PROJECT_NAME}} 프로젝트\n"


def test_render_managed_files_normal_sub_renders(pm_import, tmp_path):
    """정상값 sub 는 render_managed_files 가 정상 치환·기록(회귀 무변경·T-0218 DoD ②)."""
    dest = tmp_path / "adopter"
    rel = ".claude/agents/developer.md"
    _seed_render_managed_tree(dest, rel, "description: {{PROJECT_NAME}} 프로젝트\n")
    changed = pm_import.render_managed_files(
        dest, subs={"{{PROJECT_NAME}}": "acme"}, copied_relpaths={Path(rel)})
    assert changed == 1
    written = (dest / rel).read_text(encoding="utf-8")
    assert written == "description: acme 프로젝트\n"
    assert "{{" not in written


# ── T-0219 (c): 렌더/sed-fed 어댑터 표면의 머신-가변 토큰 재도입 방지 ─────────
# {{PY}}/{{TEST_CMD}} 는 폐기(문서=python3 관례+래퍼 self-resolve·test 명령=local.conf 노브 지칭).
# 이 토큰이 어댑터 소스에 다시 들어오면 per-clone 렌더 왕복(ping-pong)이 재발한다.

_ADAPTER_SURFACES = [
    REPO / ".claude" / "agents",
    REPO / ".claude" / "skills",
    REPO / "templates" / "claude_code" / ".claude" / "agents",
    REPO / "templates" / "claude_code" / ".claude" / "skills",
    REPO / "templates" / "opencode" / ".opencode" / "agents",
    REPO / "templates" / "opencode" / ".claude" / "skills",
    REPO / "templates" / "opencode" / ".opencode" / "command",  # T-0674 canonical 기계 사본
]


def test_adapter_surfaces_no_machine_variant_tokens():
    """어댑터 소스(렌더/sed 대상 7표면)에 {{PY}}·{{TEST_CMD}} 재도입 금지 (T-0219 (c) 불변식)."""
    offenders = []
    for surface in _ADAPTER_SURFACES:
        assert surface.is_dir(), f"어댑터 표면 부재: {surface}"
        for md in repo_owned_paths(
            REPO, surface.relative_to(REPO), mode=OWNED
        ):
            if md.suffix != ".md":
                continue
            text = md.read_text(encoding="utf-8")
            for token in ("{{PY}}", "{{TEST_CMD}}"):
                if token in text:
                    offenders.append(f"{md.relative_to(REPO)}: {token}")
    assert not offenders, "머신-가변 토큰 재도입 (T-0219 ping-pong 재발 위험):\n" + "\n".join(offenders)
