"""하네스 축 파생 로직 자체를 공격하는 가드 (T-0429).

`_harness_matrix.derive_harnesses` 는 채택자-내용 게이트 전부의 하네스 축을 공급하는 단일 출처다.
그 파생이 *구현이 성공하는 입력*만 삼키면(실트리 3-하네스) 네 번째 하네스에서 손-열거와 똑같이
샌다. 그래서 파생원(엔진 상수)과 디렉토리 실존이 **어긋나는** 형상들을 tmp fixture 로 직접 먹인다:
  - stray 디렉토리(빈 dir·파일만·`.hidden`)가 하네스로 오인되지 않는가
  - 상수엔 있는데 디렉토리 없음 / 디렉토리 있는데 상수 없음(양방향 불일치)
  - combo 키('both')는 단일 하네스가 아니라 제외되는가
  - 축소 형상(1-하네스 트리)·**0-하네스(vacuous green 방지 loud fail)**
  - 새 `templates/<name>/` + 상수 등록이 **자동 편입**되는가(손 편집 0)

실트리 정합(엔진 상수 ↔ ADD_HARNESS_ADAPTER ↔ templates/ 디렉토리)도 함께 못박는다.
"""
from __future__ import annotations

import pytest

import _harness_matrix as hm


# ── 실트리 파생 결과 + 엔진 상수 정합 ─────────────────────────────────────────

def test_real_tree_anchors_known_harnesses():
    """실트리 파생이 **알려진 하네스 3종을 최소 포함**(anchor-subset). 기존 하네스 탈락 = red,
    신규(4번째) 하네스 정상 등록 = **무수정 green**. exact 3-tuple 동등 단언이었으면 4번째가 정상
    추가돼도 이 테스트만 수동 수정이 필요해져 '자동 편입' 목표와 자기모순이었다(S·codex 채택).
    파생 결과가 전부 유효 하네스 형상(dir/root doc 실존)인지는 아래 validity 테스트가 별도 검증."""
    # belt-and-suspenders 로 유지 — round3 MF 의 derive '등록 ⇒ 실존' loud fail 은
    #   *registered-without-dir* 만 잡는다. 이 anchor 테스트는 그와 **다른** 실패,
    #   즉 알려진 하네스가 HARNESS_TEMPLATE_DIRS 에서 **통째로 미등록(제거)** 되는 회귀를 잡는다
    #   (미등록은 iterate 대상에서 빠져 loud 안 뜬다). 두 가드는 상보적이라 둘 다 둔다.
    anchors = {"claude", "codex", "opencode"}
    assert anchors <= set(hm.HARNESSES), (
        f"알려진 하네스 탈락(파생 축에서 사라짐): {anchors - set(hm.HARNESSES)} · 현 파생={hm.HARNESSES}")


def test_derived_axis_matches_add_harness_adapter_keys():
    """파생 축(HARNESS_TEMPLATE_DIRS 기반)이 ADD_HARNESS_ADAPTER 키집합과 일치 — 두 엔진 상수가
    같은 하네스 목록을 말해야 게이트의 축·네임스페이스가 어긋나지 않는다(손-복제였으면 벌어졌다)."""
    assert set(hm.HARNESSES) == set(hm.HARNESS_ADAPTER_DIRS) == set(hm.HARNESS_ROOT_DOC)


def test_real_tree_every_harness_has_template_dir_and_namespace():
    """실트리 무-불일치: 파생된 각 하네스가 templates/ 어댑터 트리 디렉토리 + 어댑터 네임스페이스를
    실제로 보유(상수엔 있는데 디렉토리 없는 drift 를 실트리에서 loud 로 잡는다)."""
    for h in hm.HARNESSES:
        (dirname,) = hm._PM_IMPORT.HARNESS_TEMPLATE_DIRS[h]
        assert (hm.TEMPLATES / dirname).is_dir(), f"{h}: templates/{dirname} 디렉토리 부재"
        for ns in hm.HARNESS_ADAPTER_DIRS[h]:
            # 네임스페이스는 채택자 dest 상대 경로(예 .codex)라 소스 트리 dir 명과 다를 수 있어
            # shape(비어있지 않은 str)만 확인 — 실 배치는 import 게이트가 검증한다.
            assert isinstance(ns, str) and ns
        # 루트 doc 은 소스 트리에 **반드시 실재**를 strict 단언한다 — 옛 `or ...endswith(".md")` 는
        #   루트 doc 명이 항상 `.md` 라 무조건 참(vacuous)이라 실존 검사가 무효였다(MF1).
        root = hm.TEMPLATES / dirname / hm.HARNESS_ROOT_DOC[h]
        assert root.is_file(), f"{h}: 루트 doc {hm.HARNESS_ROOT_DOC[h]} 소스 트리 부재: {root}"


# ── 파생 로직 공격 (tmp fixture · 실트리 미오염) ──────────────────────────────

def _mk_templates(tmp_path, *dirnames: str):
    """tmp 에 templates/<dirname>/ 들을 만든다(각 dir 에 파일 하나 — 디렉토리 실존 프로브용)."""
    templates = tmp_path / "templates"
    for name in dirnames:
        d = templates / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".keep").write_text("x", encoding="utf-8")
    return templates


def test_derive_auto_includes_new_registered_harness(tmp_path):
    """새 하네스가 상수 등록(단일 templates dir) + 디렉토리 실존이면 **자동 편입**(게이트 손 편집 0).

    이게 티켓의 핵심 — 네 번째 하네스는 상수 한 줄 + templates/ 트리면 모든 게이트에 자동으로 흐른다.
    """
    templates = _mk_templates(tmp_path, "claude_code", "opencode", "codex", "fourth_tmpl")
    const = {**hm._PM_IMPORT.HARNESS_TEMPLATE_DIRS, "fourth": ("fourth_tmpl",)}
    got = hm.derive_harnesses(templates, const)
    assert got == ("claude", "codex", "fourth", "opencode"), got


def test_derive_ignores_stray_empty_dir(tmp_path):
    """상수에 없는 빈 디렉토리는 하네스로 오인되지 않는다(파일시스템-only 파생이었으면 오수집)."""
    templates = _mk_templates(tmp_path, "claude_code")
    (templates / "empty_stray").mkdir()
    const = {"claude": ("claude_code",)}
    assert hm.derive_harnesses(templates, const) == ("claude",)


def test_derive_ignores_files_only_and_hidden_dirs(tmp_path):
    """파일만 있는 디렉토리·`.hidden` 디렉토리도 상수에 없으면 무시된다."""
    templates = _mk_templates(tmp_path, "opencode")
    (templates / "files_only").mkdir()
    (templates / "files_only" / "note.txt").write_text("x", encoding="utf-8")
    (templates / ".hidden").mkdir()
    (templates / ".hidden" / "x").write_text("x", encoding="utf-8")
    const = {"opencode": ("opencode",)}
    assert hm.derive_harnesses(templates, const) == ("opencode",)


def test_derive_registered_without_dir_raises_loud(tmp_path):
    """상수엔 있는데 templates/ 디렉토리가 없으면 **loud RuntimeError**(round3 MF·'등록 ⇒ 실존').
    옛 ∩ 는 조용히 드롭했는데, 그러면 templates/<dir> 소실/개명 시 그 하네스의 전 parametrize
    게이트가 **무음 우회**된다(수집 축소만·red 0). 파생 자체가 loud fail 로 그 소실을 표면화한다."""
    templates = _mk_templates(tmp_path, "claude_code")  # opencode 트리는 안 만든다
    const = {"claude": ("claude_code",), "opencode": ("opencode",)}
    with pytest.raises(RuntimeError, match="디렉토리가 없다"):
        hm.derive_harnesses(templates, const)


def test_derive_excludes_combo_key(tmp_path):
    """combo 키('both' = 어댑터 트리 2개)는 단일 하네스가 아니라 제외(디렉토리 다 있어도)."""
    templates = _mk_templates(tmp_path, "claude_code", "opencode")
    const = {
        "claude": ("claude_code",),
        "opencode": ("opencode",),
        "both": ("claude_code", "opencode"),
    }
    assert hm.derive_harnesses(templates, const) == ("claude", "opencode")


def test_derive_reduced_shape_single_harness(tmp_path):
    """1-하네스 축소 형상(상수·디렉토리 **함께** 1종)도 정상 파생 — '등록 ⇒ 실존' 불변식과 정합.
    (상수가 opencode 도 등록하는데 dir 이 없으면 축소가 아니라 소실 = RuntimeError·위 테스트.)"""
    templates = _mk_templates(tmp_path, "claude_code")
    const = {"claude": ("claude_code",)}   # 상수도 claude 만
    assert hm.derive_harnesses(templates, const) == ("claude",)


def test_derive_only_combo_keys_raises_loud(tmp_path):
    """단일-하네스 등록 항목이 하나도 없으면(combo 키만) 파생 0 → loud RuntimeError(vacuous green 방지)."""
    templates = _mk_templates(tmp_path, "claude_code", "opencode")
    const = {"both": ("claude_code", "opencode")}   # combo 키만 — 단일 하네스 0
    with pytest.raises(RuntimeError, match="파생된 하네스 0개"):
        hm.derive_harnesses(templates, const)


def test_derive_missing_templates_dir_raises(tmp_path):
    """templates_dir 자체가 없으면 첫 등록 하네스의 dir 부재로 '등록 ⇒ 실존' loud fail."""
    with pytest.raises(RuntimeError, match="디렉토리가 없다"):
        hm.derive_harnesses(tmp_path / "no_such_templates", hm._PM_IMPORT.HARNESS_TEMPLATE_DIRS)
