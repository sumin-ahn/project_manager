"""T-0544 ④ — flavor manifest 등록 경로의 템플릿 트리 실재 가드 (일반화).

닫는 결함 클래스 = "등록은 됐는데 파일이 안 실림". manifest 에 경로를 넣고 실 파일을
템플릿 트리에 안 실으면 채택자는 그 경로를 영영 못 받는다. codex manifest 의
`.project_manager/wiki/README.md` 미실재가 이 클래스의 실례였고, 그때까지의 가드는
`gate_snapshot.py` 같은 *개별 파일* 단언이라 새 파일이 늘 때마다 단언을 손으로 추가해야
잡혔다. 여기서는 각 flavor manifest 의 **모든** 비-`@source` 경로를 한 번에 본다.

스코프 경계 — `@source=<relpath>` 항목은 제외한다. 그 항목의 canonical 소스는 프레임워크
루트 기준 relpath(예 `templates/codex/.codex/pm_orch_codex.py`)에 있고, 템플릿 트리 안의
같은 이름 경로는 소스가 아니라 전파 결과물이라 이 가드의 판정 대상이 아니다(등록 경로 ↔
소스 remap 정합은 test_manifest_template_parity 의 `@source` 가드 계열이 본다).

hermetic — 실 파일 존재만 본다(LLM·subprocess 미진입). manifest 파싱은 pm_update.read_manifest
재사용(주석·`@마커` 제거를 한 곳에서 — 자체 파서 drift 회피).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FLAVORS = ("claude_code", "codex", "opencode")
TEMPLATE_ROOTS = {flavor: REPO / "templates" / flavor for flavor in FLAVORS}
FLAVOR_MANIFESTS = {
    flavor: root / ".project_manager" / "engine.manifest"
    for flavor, root in TEMPLATE_ROOTS.items()
}


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unshipped_manifest_paths(manifest: Path, template_root: Path) -> list[str]:
    """manifest 의 비-`@source` 등록 경로 중 template_root 아래 실재하지 않는 것 (정합용 helper).

    디렉토리 항목도 그대로 본다(디렉토리 자체의 실재 = 재귀 동기화 대상 존재)."""
    pm_update = _load_pm_update()
    return sorted(
        str(entry)
        for entry in pm_update.read_manifest(manifest)
        if getattr(entry, "source_rel", None) is None
        and not (template_root / str(entry)).exists()
    )


@pytest.mark.parametrize("flavor", FLAVORS)
def test_flavor_manifest_registers_only_shipped_paths(flavor):
    """각 flavor manifest 의 비-`@source` 경로가 그 템플릿 트리에 전부 실재한다.

    미실재면 채택자가 그 경로를 못 받는다(등록만 되고 출하 안 된 절반 출하)."""
    missing = _unshipped_manifest_paths(
        FLAVOR_MANIFESTS[flavor], TEMPLATE_ROOTS[flavor]
    )
    assert not missing, (
        f"'{flavor}' manifest 가 템플릿 트리에 없는 경로를 등록 — {missing}. "
        "실 파일을 templates 트리에 싣거나(pm_update 전파) 등록을 빼야 한다."
    )


def test_guard_is_sensitive_to_a_registered_but_unshipped_path(tmp_path):
    """sensitivity — 신규 미실재 경로를 주입하면 red 임을 입증(non-vacuous·실 트리 미변경).

    가상 템플릿 트리에 manifest 3항목(실재 bare·미실재 bare·미실재 `@source`)을 만들어
    helper 판정을 확인한다. `@source` 항목이 조용히 빠지는지도 같이 못박는다 — 스코프
    경계가 무너지면 이 가드가 remap 항목에 false-red 를 낸다."""
    template_root = tmp_path / "fake_flavor"
    shipped = template_root / ".project_manager" / "tools" / "shipped_tool.py"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("# shipped\n", encoding="utf-8")
    manifest = template_root / ".project_manager" / "engine.manifest"
    manifest.write_text(
        "# 가상 flavor manifest (sensitivity 전용)\n"
        ".project_manager/tools/shipped_tool.py\n"
        ".project_manager/wiki/README.md\n"
        ".codex/pm_orch_codex.py    @source=templates/codex/.codex/pm_orch_codex.py\n",
        encoding="utf-8",
    )

    missing = _unshipped_manifest_paths(manifest, template_root)

    assert missing == [".project_manager/wiki/README.md"], (
        f"미실재 등록 경로 판정이 예상과 다름 — 검출 {missing}"
    )

    # 음성 통제: 미실재 항목을 실으면 0건 (false-positive 아님 확인).
    (template_root / ".project_manager" / "wiki").mkdir()
    (template_root / ".project_manager" / "wiki" / "README.md").write_text(
        "# shipped later\n", encoding="utf-8"
    )
    assert _unshipped_manifest_paths(manifest, template_root) == []
