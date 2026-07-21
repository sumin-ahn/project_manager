"""T-0397 — 엔진 사본 rev 스탬프 정합 fail-loud (사본 skew → 명시 에러).

엔진 도구가 형제 모듈(identity_args·worktree_pool …)을 `spec_from_file_location` 으로
동적 로드할 때, 사본 skew(신 도구 + 구/불일치 형제)를 로드 시점에 명시 에러로 전환하는지
검증한다. 회사 실측(신 pm_handoff + 구 identity_args → `identity.task` AttributeError)을
재현해, 이제 cryptic AttributeError 대신 "어느 파일이 구형인지" 지목하는 fail-loud 가 나는지
확인한다.

모두 hermetic — 실 tools/ 파일을 tmp 디렉토리로 복사하고 형제를 mutate 해 skew 를 만든 뒤
로드한다. 실 트리·실 LLM·subprocess 미진입.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / ".project_manager" / "tools"

# 구형(T-0397 이전) identity_args — ENGINE_REV 상수가 아예 없다. 회사 실측 시나리오의 형제.
_OLD_IDENTITY_ARGS = "# 구형 identity_args (T-0397 스탬프 이전) — ENGINE_REV 부재.\nTASK = 1\n"
# 구버전 릴리즈에서 온 stale identity_args — 스탬프는 있으나 rev 값이 다르다.
_STALE_IDENTITY_ARGS = '# stale identity_args (구 릴리즈).\nENGINE_REV = "v1.3.4"\n'

# 현재 엔진 rev — 리터럴 하드코딩 금지(v1.4.0 bump 실측: 첫 bump 이 v1.3.5 리터럴 단언 6건 노출).
# bump 가 전 스탬프를 일괄 재작성하므로 단언은 로더의 실제 baked 값을 동적 참조한다(bump-내구).
_CUR_REV = None


def _cur_rev() -> str:
    global _CUR_REV
    if _CUR_REV is None:
        _CUR_REV = _load(TOOLS, "engine_rev").ENGINE_REV
    return _CUR_REV



def _build_tools(tmp_path: Path, files: dict[str, str | None]) -> Path:
    """tmp 아래 `.project_manager/tools/` 를 만들고 파일을 채운다.

    값이 None 이면 실 tools/ 에서 복사(canonical), str 이면 그 내용으로 기록(mutate)."""
    tools_dir = tmp_path / ".project_manager" / "tools"
    tools_dir.mkdir(parents=True)
    for name, content in files.items():
        if content is None:
            shutil.copy(TOOLS / name, tools_dir / name)
        else:
            (tools_dir / name).write_text(content, encoding="utf-8")
    return tools_dir


def _load(tools_dir: Path, name: str):
    """tmp tools_dir 에서 모듈을 spec 로드한다(도구의 실 로드 경로와 동형)."""
    spec = importlib.util.spec_from_file_location(name, tools_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── engine_rev.check() 단위 (baked rev 비교기·기전의 축) ─────────────────────


def _load_engine_rev_module():
    return _load(TOOLS, "engine_rev")


def test_check_passes_when_rev_matches():
    """형제 baked rev 가 로더 baked rev 와 같으면 통과(정상 동기·거동 변경 0)."""
    er = _load_engine_rev_module()
    er.check(er.ENGINE_REV, er.ENGINE_REV,
             sibling_filename="identity_args.py", loader_filename="pm_handoff.py")  # no raise


def test_check_fails_when_rev_absent():
    """형제 baked rev 부재(=구형 형제·None) → EngineRevSkew(fail-loud)."""
    er = _load_engine_rev_module()
    with pytest.raises(er.EngineRevSkew) as exc:
        er.check(None, er.ENGINE_REV,
                 sibling_filename="identity_args.py", loader_filename="pm_handoff.py")
    msg = str(exc.value)
    assert "identity_args.py" in msg          # 어느 파일이 구형인지 지목
    assert "pm_handoff.py" in msg             # 어느 로더인지 지목
    assert "None" in msg                      # 부재 rev 표시
    assert "pm-update" in msg                 # 해소 안내


def test_check_fails_when_rev_mismatches():
    """형제 baked rev 가 다른 값(stale 릴리즈) → EngineRevSkew(불일치 값 표시)."""
    er = _load_engine_rev_module()
    with pytest.raises(er.EngineRevSkew) as exc:
        er.check("v0.0.0-stale", er.ENGINE_REV,
                 sibling_filename="worktree_pool.py", loader_filename="pm_bootstrap.py")
    msg = str(exc.value)
    assert "worktree_pool.py" in msg
    assert "v0.0.0-stale" in msg
    assert er.ENGINE_REV in msg               # 로더(기대) rev 도 표시


# ── 회사 실측 시나리오: 신 pm_handoff + 구 identity_args ──────────────────────


def test_new_pm_handoff_old_identity_args_fails_loud(tmp_path):
    """신 pm_handoff 가 구 identity_args(ENGINE_REV 부재)를 로드 → 명시 에러(AttributeError 아님).

    회사 실측(`identity.task` AttributeError) 재현 — 이제 로드 시점에 어느 파일이 구형인지
    지목하는 fail-loud 로 전환된다."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_handoff.py": None,
        "identity_args.py": _OLD_IDENTITY_ARGS,
    })
    with pytest.raises(RuntimeError) as exc:  # EngineRevSkew ⊂ RuntimeError
        _load(tools, "pm_handoff")
    msg = str(exc.value)
    assert "identity_args.py" in msg
    assert "불일치" in msg
    assert "pm-update" in msg


def test_new_pm_handoff_stale_identity_args_fails_loud(tmp_path):
    """신 pm_handoff + stale identity_args(구 릴리즈 rev) → 불일치 fail-loud(값 지목)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_handoff.py": None,
        "identity_args.py": _STALE_IDENTITY_ARGS,
    })
    with pytest.raises(RuntimeError) as exc:
        _load(tools, "pm_handoff")
    msg = str(exc.value)
    assert "identity_args.py" in msg
    assert "v1.3.4" in msg                    # stale 형제 rev
    assert _cur_rev() in msg                    # 엔진 rev


def test_normal_sync_no_effect(tmp_path):
    """정상 동기(모든 형제 canonical) → skew 없음·정상 로드(거동 변경 0)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_handoff.py": None,
        "identity_args.py": None,
    })
    mod = _load(tools, "pm_handoff")           # no raise
    assert mod.ENGINE_REV == _cur_rev()
    assert mod.identity_args is not None       # 형제 로드 성공
    assert mod.identity_args.ENGINE_REV == _cur_rev()


def test_partial_copy_current_engine_rev_but_old_baked_sibling_detected(tmp_path):
    """**구조적 핵심**(codex R2): engine_rev.py 가 present+current(v1.3.5)여도 구 baked 형제는 검출된다.

    차기 릴리즈 부분복사 = 신 engine_rev.py + 구 stamped sibling(옛 baked 리터럴). 런타임 공유-읽기
    설계였다면 형제가 같은 신 engine_rev.py 를 읽어 자기-일치→**미검출**이었을 결함이다. baked 리터럴
    은 형제 소스에 고정돼 따라가므로(v1.3.4), 로더의 baked(v1.3.5)와 mismatch → fail-loud 로 검출된다."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,                    # 신·current(v1.3.5) — present 여도 detection 유효
        "pm_handoff.py": None,                    # 신 로더(baked v1.3.5)
        "identity_args.py": _STALE_IDENTITY_ARGS,  # 구 릴리즈 방식 형제(baked v1.3.4)
    })
    with pytest.raises(RuntimeError) as exc:
        _load(tools, "pm_handoff")
    msg = str(exc.value)
    assert "identity_args.py" in msg
    assert "v1.3.4" in msg                        # 구 형제 baked rev
    assert _cur_rev() in msg                        # 신 로더 baked rev


def test_missing_engine_rev_is_benign_at_runtime(tmp_path):
    """engine_rev.py 부재는 런타임 로드에 무해 — 검증은 baked 리터럴로만(런타임 의존 0).

    (engine_rev.py 는 bump CLI·평시 가드 테스트의 참조일 뿐, 형제 verify 는 이 파일을 읽지 않는다.)"""
    tools = _build_tools(tmp_path, {
        "pm_handoff.py": None,
        "identity_args.py": None,                 # engine_rev.py 는 일부러 뺌 — 그래도 정상 로드
    })
    mod = _load(tools, "pm_handoff")              # no raise (baked 리터럴 일치)
    assert mod.ENGINE_REV == _cur_rev()
    assert mod.identity_args is not None


# ── 다른 로더도 같은 패턴(board·worktree_pool 사본 skew) ──────────────────────


def test_board_old_identity_args_fails_loud(tmp_path):
    """board 도 구 identity_args 를 로드하면 fail-loud(동일 패턴·전 도구 대칭)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "board.py": None,
        "identity_args.py": _OLD_IDENTITY_ARGS,
    })
    with pytest.raises(RuntimeError) as exc:
        _load(tools, "board")
    assert "identity_args.py" in str(exc.value)


def test_board_normal_sync_ok(tmp_path):
    """board 정상 동기 → 정상 로드(오탐 없음)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "board.py": None,
        "identity_args.py": None,
    })
    mod = _load(tools, "board")
    assert mod.ENGINE_REV == _cur_rev()
    assert mod.identity_args is not None


# ── fail-soft 로더 경유 중첩 skew 재-raise (must-fix ②·삼킴 방지) ────────────────
# 신 도구가 fail-soft 로 stamped 형제를 로드하고, 그 형제가 *또 다른* 구 형제를 중첩 로드해
# skew 를 낼 때, 바깥 로더의 `except Exception` 이 그 skew 를 None 으로 삼키면 안 된다(fail-loud
# 보존). 검출은 예외 마커(`_engine_rev_skew`)로 — 로드 실패/부재만 흡수한다.


def test_pm_bootstrap_load_board_reraises_nested_skew(tmp_path):
    """신 pm_bootstrap → 신 board → 구 identity_args 중첩 skew 는 _load_board 가 fail-loud(None 아님)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_bootstrap.py": None,
        "board.py": None,
        "identity_args.py": _OLD_IDENTITY_ARGS,   # board 가 중첩 로드 → skew
    })
    pmb = _load(tools, "pm_bootstrap")
    with pytest.raises(RuntimeError) as exc:
        pmb._load_board()                          # fail-soft 였다면 조용히 None
    assert "identity_args.py" in str(exc.value)    # 중첩 형제가 지목됨


def test_pm_config_load_module_reraises_nested_skew(tmp_path):
    """신 pm_config → 신 board → 구 identity_args 중첩 skew 는 _load_module 이 fail-loud(None 아님)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_config.py": None,
        "board.py": None,
        "identity_args.py": _OLD_IDENTITY_ARGS,
    })
    pmc = _load(tools, "pm_config")
    with pytest.raises(RuntimeError) as exc:
        pmc._load_module("board", "board.py")
    assert "identity_args.py" in str(exc.value)


def test_pm_config_load_module_unstamped_sibling_no_false_positive(tmp_path):
    """pm_config._load_module 은 미계측 형제(_STAMPED_SIBLINGS 밖)엔 verify 를 안 걸어 오탐 없음.

    정상 사본에서도 ENGINE_REV 가 없는 pm_update/pm_import 류를 로드해도 skew 오판이 안 난다."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_config.py": None,
        # ENGINE_REV 없는 미계측 형제 — allow-set 밖이라 verify 미발화(정상 로드).
        "some_tool.py": "VALUE = 1\n",
    })
    pmc = _load(tools, "pm_config")
    mod = pmc._load_module("some_tool", "some_tool.py")   # no raise
    assert mod is not None and mod.VALUE == 1


def test_pm_bootstrap_load_board_normal_returns_module(tmp_path):
    """정상 동기 pm_bootstrap._load_board → board 모듈 반환(fail-loud 오탐 없음)."""
    tools = _build_tools(tmp_path, {
        "engine_rev.py": None,
        "pm_bootstrap.py": None,
        "board.py": None,
        "identity_args.py": None,
    })
    pmb = _load(tools, "pm_bootstrap")
    board = pmb._load_board()
    assert board is not None and board.ENGINE_REV == _cur_rev()
