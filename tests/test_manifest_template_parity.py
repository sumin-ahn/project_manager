"""T-0171 — manifest 경로 집합 정합 + facade 정합 가드.

T-0170 이 manifest/facade 전파 채널 갭을 노출했다. 실측 결론:
  - 갭2(templates manifest "stale") = 실체는 경로 집합 byte-identical, drift 는 주석뿐.
    → 경로 집합 정합을 못박아 *경로* drift(전파 채널 누락/잉여)는 즉시 fail 시킨다.
  - 갭3(pm-update.sh facade 부재) = 진짜 갭. pm_import 가 채택자 루트로 복사하는 facade
    (pm-config·pm-update)가 각 템플릿 트리에 전부 실재해야 채택자에 도달한다.

선례: SHIPPING_GLOBS↔manifest 동형(T-0154)·template_scaffold_parity. 모두 hermetic —
실 파일 존재/내용만 본다(LLM·subprocess 미진입). manifest 파싱은 pm_update.read_manifest
재사용(주석·`@마커` 제거를 한 곳에서 — 자체 파서 drift 회피).

historical 주의: manifest *경로* 만 비교한다(주석 drift 는 무시 — 갭2 실체가 주석뿐).
폐기 용어 잔존은 별개 가드(test_terminology·T-0171 범위 확장)가 본다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

ROOT_MANIFEST = REPO / ".project_manager" / "engine.manifest"
CC_MANIFEST = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
OC_MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"

# opencode 트리의 정당한 manifest 차이(harness-correct·화이트리스트). 임의 경로가 새로
# 추가/누락되면 fail — 의도적 어댑터 비대칭만 통과시킨다(전파 채널 우발 drift 차단).
#   opencode 가 추가: .opencode 어댑터 트리(claude 의 .claude 대응).
#   opencode 가 제외: .claude/* 어댑터 + regression.yml(claude-scoped CI 워크플로).
OPENCODE_ONLY_PATHS = {".opencode/agents", ".opencode/command"}
CLAUDE_ONLY_PATHS = {".claude/agents", ".claude/skills", ".github/workflows/regression.yml"}

# pm_import 가 *채택자 루트로 복사*하는 facade 파일명 집합 (engine.manifest L33-34 주석·
# pm_import.plan_copy 동작). 템플릿 트리 전체가 채택자 루트로 복사되므로, 이 파일들이 각
# 템플릿 트리에 실재해야 채택자에 도달한다. pm-import 는 *manager 루트*(① worktree)에만 있고
# 채택자엔 안 간다(채택자는 자기를 import 할 일 없음) → 템플릿 트리 facade = config + update.
ADOPTER_FACADE_STEMS = ("pm-config", "pm-update")
FACADE_EXTS = (".sh", ".cmd")

TEMPLATE_ROOTS = {
    "claude_code": REPO / "templates" / "claude_code",
    "opencode": REPO / "templates" / "opencode",
}


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_path_set(manifest: Path) -> set[str]:
    """manifest 의 *경로* 집합 (주석·빈 줄·`@마커` 제거 — read_manifest 재사용)."""
    pm_update = _load_pm_update()
    return {str(entry) for entry in pm_update.read_manifest(manifest)}


# ── 가드 2: manifest 경로 집합 정합 ──────────────────────────────────────────


def test_root_and_claude_manifest_path_sets_identical():
    """루트 engine.manifest 와 claude_code 템플릿 manifest 의 경로 집합이 동일(둘 다 claude-scoped).

    갭2 재발 차단 — 경로(전파 채널)는 byte-identical 이어야 한다(drift 는 주석에만 허용)."""
    root = _manifest_path_set(ROOT_MANIFEST)
    cc = _manifest_path_set(CC_MANIFEST)
    assert root == cc, (
        "루트↔claude_code manifest 경로 drift — "
        f"루트에만: {sorted(root - cc)} / claude_code에만: {sorted(cc - root)}"
    )


def test_opencode_manifest_diff_is_whitelisted_only():
    """opencode 템플릿 manifest 는 harness-correct 하게 다르되, 차이가 화이트리스트에만 있어야 한다.

    claude_code 대비 opencode 의 추가/누락 경로가 의도적 어댑터 비대칭(.opencode/* 추가·
    .claude/* + regression.yml 제외)에만 있음을 단언. 임의 경로가 새로 들고/빠지면 fail."""
    cc = _manifest_path_set(CC_MANIFEST)
    oc = _manifest_path_set(OC_MANIFEST)
    added = oc - cc        # opencode 가 추가한 경로
    dropped = cc - oc      # opencode 가 제외한 경로
    assert added == OPENCODE_ONLY_PATHS, (
        f"opencode manifest 추가 경로가 화이트리스트와 불일치 — "
        f"예상 {sorted(OPENCODE_ONLY_PATHS)}, 실제 {sorted(added)}"
    )
    assert dropped == CLAUDE_ONLY_PATHS, (
        f"opencode manifest 제외 경로가 화이트리스트와 불일치 — "
        f"예상 {sorted(CLAUDE_ONLY_PATHS)}, 실제 {sorted(dropped)}"
    )


# ── 가드 3: facade 정합 (갭3 재발 차단) ──────────────────────────────────────


def _missing_facades(template_root: Path) -> list[str]:
    """template_root 에서 빠진 채택자 facade 파일명 리스트 (정합용 helper)."""
    missing = []
    for stem in ADOPTER_FACADE_STEMS:
        for ext in FACADE_EXTS:
            if not (template_root / f"{stem}{ext}").is_file():
                missing.append(f"{stem}{ext}")
    return missing


def test_each_template_tree_has_all_adopter_facades():
    """pm_import 가 채택자 루트로 복사하는 facade(pm-config·pm-update의 .sh/.cmd)가 각 템플릿 트리에 전부 존재.

    갭3 재발 차단 — ② 가 pm-update.sh 를 못 받은 클래스(facade 누락이 채택자에 전파)를 박제.
    템플릿 트리 전체가 채택자 루트로 복사되므로(pm_import.plan_copy), facade 가 트리에 없으면
    채택자도 못 받는다."""
    for name, root in TEMPLATE_ROOTS.items():
        missing = _missing_facades(root)
        assert not missing, (
            f"'{name}' 템플릿 트리에 채택자 facade 누락: {missing} — "
            "pm_import 가 채택자 루트로 복사하지 못해 채택자가 못 받는다(갭3)."
        )


def test_facade_guard_is_sensitive_to_missing_facade():
    """sensitivity — facade 한 개를 (가상으로) 빠뜨리면 가드가 fail 함을 입증(non-vacuous).

    실 파일은 안 건드린다 — 존재하지 않는 가상 트리 경로에 helper 를 돌려 'missing 검출' 만 확인."""
    nonexistent_root = REPO / "templates" / "__nonexistent_for_sensitivity__"
    missing = _missing_facades(nonexistent_root)
    # 가상 트리엔 아무 facade 도 없으므로 전부 missing 으로 잡혀야 한다.
    expected = [f"{s}{e}" for s in ADOPTER_FACADE_STEMS for e in FACADE_EXTS]
    assert missing == expected, (
        f"facade 가드가 누락을 못 잡음(vacuous 위험) — 검출 {missing}, 예상 {expected}"
    )
