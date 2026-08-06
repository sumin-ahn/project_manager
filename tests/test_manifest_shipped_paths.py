"""T-0544 ④ — flavor manifest 등록 경로의 템플릿 트리 실재 가드 (일반화).

닫는 결함 클래스 = "등록은 됐는데 파일이 안 실림". manifest 에 경로를 넣고 실 파일을
템플릿 트리에 안 실으면 채택자는 그 경로를 영영 못 받는다. codex manifest 의
`.project_manager/wiki/README.md` 미실재가 이 클래스의 실례였고, 그때까지의 가드는
`gate_snapshot.py` 같은 *개별 파일* 단언이라 새 파일이 늘 때마다 단언을 손으로 추가해야
잡혔다. 여기서는 각 flavor manifest 의 **모든** 등록 경로를 한 번에 본다 — 비-`@source` 항목
전부 + 소스가 그 템플릿 트리 안인 `@source` 항목(아래 스코프 경계).

스코프 경계 — `@source=<relpath>` 는 **소스가 어느 트리에 있냐**로 갈린다. 소스가 이 템플릿
트리 안(`templates/<이 flavor>/…`)이면 부재가 곧 절반 출하라 판정 대상이고, 프레임워크 루트나
다른 flavor 를 가리키면(예 codex 의 `@source=.claude/skills`) 이 트리는 전파 결과물만 갖는
쪽이라 대상이 아니다(등록 경로 ↔ 소스 remap 정합은 test_manifest_template_parity 의 `@source`
가드 계열이 본다).

hermetic — 실 파일 존재만 본다(LLM·subprocess 미진입). manifest 파싱은 pm_update.read_manifest
재사용(주석·`@마커` 제거를 한 곳에서 — 자체 파서 drift 회피). pm_update 로드는 모듈 캐시라
호출마다 re-exec 하지 않는다(같은 파일을 flavor·테스트 수만큼 다시 실행할 이유가 없다).
"""
from __future__ import annotations

import functools
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


@functools.lru_cache(maxsize=1)
def _load_pm_update():
    """pm_update 모듈 (프로세스당 1회 exec·캐시).

    sys.modules 에 등록하지 않는 로드라 캐시 없이는 호출마다 파일 전체가 다시 실행된다."""
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _in_tree_source_rel(entry, template_root: Path) -> str | None:
    """`@source` 소스가 이 템플릿 트리 안이면 트리 상대 경로, 아니면 None (스코프 경계)."""
    source_rel = getattr(entry, "source_rel", None)
    if source_rel is None:
        return None
    prefix = f"templates/{template_root.name}/"
    normalized = source_rel.replace("\\", "/")
    return normalized[len(prefix):] if normalized.startswith(prefix) else None


def _unshipped_manifest_paths(manifest: Path, template_root: Path) -> list[str]:
    """manifest 등록 경로 중 template_root 아래 실재하지 않는 것 (정합용 helper).

    두 부류를 본다 — 비-`@source` 항목은 등록 경로 자체가 출하 경로이고, `@source` 항목 중
    소스가 이 트리 안인 것은 그 소스 경로가 출하 경로다(둘 다 부재 = 채택자 미도달). 디렉토리
    항목도 그대로 본다(디렉토리 자체의 실재 = 재귀 동기화 대상 존재)."""
    missing = []
    for entry in _load_pm_update().read_manifest(manifest):
        if getattr(entry, "source_rel", None) is None:
            rel, label = str(entry), str(entry)
        else:
            rel = _in_tree_source_rel(entry, template_root)
            if rel is None:
                continue  # 루트/타 flavor 소스 — 이 트리 판정 대상 아님
            label = f"{rel} (등록 경로 {entry})"
        if not (template_root / rel).exists():
            missing.append(label)
    return sorted(missing)


def _in_tree_source_entries(manifest: Path, template_root: Path) -> list[str]:
    """이 트리 안을 소스로 삼는 `@source` 항목의 소스 경로 목록 (스코프 실측용)."""
    return sorted(
        rel
        for entry in _load_pm_update().read_manifest(manifest)
        if (rel := _in_tree_source_rel(entry, template_root)) is not None
    )


@pytest.mark.parametrize("flavor", FLAVORS)
def test_flavor_manifest_registers_only_shipped_paths(flavor):
    """각 flavor manifest 의 등록 경로(비-`@source` + in-tree `@source` 소스)가 전부 실재한다.

    미실재면 채택자가 그 경로를 못 받는다(등록만 되고 출하 안 된 절반 출하)."""
    missing = _unshipped_manifest_paths(
        FLAVOR_MANIFESTS[flavor], TEMPLATE_ROOTS[flavor]
    )
    assert not missing, (
        f"'{flavor}' manifest 가 템플릿 트리에 없는 경로를 등록 — {missing}. "
        "실 파일을 templates 트리에 싣거나(pm_update 전파) 등록을 빼야 한다."
    )


@pytest.mark.parametrize("flavor", FLAVORS)
def test_guard_scope_covers_in_tree_source_entries(flavor):
    """각 flavor manifest 에 이 트리를 소스로 삼는 `@source` 항목이 실제로 있다.

    편입한 부분집합이 비면 위 가드의 그 갈래는 실 트리에서 아무것도 안 보는 셈이다(조용한
    미탐 창). flavor 디렉토리명 규칙이 바뀌어 prefix 매칭이 통째로 빗나가도 여기서 잡힌다."""
    entries = _in_tree_source_entries(
        FLAVOR_MANIFESTS[flavor], TEMPLATE_ROOTS[flavor]
    )
    assert entries, (
        f"'{flavor}' manifest 에 `@source=templates/{flavor}/…` 항목이 하나도 없다 — "
        "가드의 in-tree 갈래가 실 트리에서 vacuous"
    )


def test_guard_is_sensitive_to_a_registered_but_unshipped_path(tmp_path):
    """sensitivity — 신규 미실재 경로를 주입하면 red 임을 입증(non-vacuous·실 트리 미변경).

    가상 템플릿 트리에 manifest 5항목(실재 bare · 미실재 bare · 실재 in-tree `@source` ·
    미실재 in-tree `@source` · 타 flavor `@source`)을 만들어 helper 판정을 확인한다. 마지막
    두 항목이 스코프 경계다 — in-tree `@source` 를 빼면 미탐 창이 남고, 타 flavor `@source`
    를 넣으면 remap 항목에 false-red 를 낸다."""
    template_root = tmp_path / "fake_flavor"
    shipped = template_root / ".project_manager" / "tools" / "shipped_tool.py"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("# shipped\n", encoding="utf-8")
    driver = template_root / ".fake" / "shipped_driver.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("# shipped driver\n", encoding="utf-8")
    manifest = template_root / ".project_manager" / "engine.manifest"
    manifest.write_text(
        "# 가상 flavor manifest (sensitivity 전용)\n"
        ".project_manager/tools/shipped_tool.py\n"
        ".project_manager/wiki/README.md\n"
        ".fake/driver.py            @source=templates/fake_flavor/.fake/shipped_driver.py\n"
        ".fake/pm_orch_fake.py      @source=templates/fake_flavor/.fake/missing_driver.py\n"
        ".codex/pm_orch_codex.py    @source=templates/codex/.codex/pm_orch_codex.py\n",
        encoding="utf-8",
    )

    missing = _unshipped_manifest_paths(manifest, template_root)

    assert missing == [
        ".fake/missing_driver.py (등록 경로 .fake/pm_orch_fake.py)",
        ".project_manager/wiki/README.md",
    ], f"미실재 등록 경로 판정이 예상과 다름 — 검출 {missing}"

    # 음성 통제: 미실재 항목을 실으면 0건 (false-positive 아님 확인).
    (template_root / ".project_manager" / "wiki").mkdir()
    (template_root / ".project_manager" / "wiki" / "README.md").write_text(
        "# shipped later\n", encoding="utf-8"
    )
    (template_root / ".fake" / "missing_driver.py").write_text(
        "# shipped later\n", encoding="utf-8"
    )
    assert _unshipped_manifest_paths(manifest, template_root) == []
