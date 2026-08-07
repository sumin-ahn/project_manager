"""T-0171 — manifest 경로 집합 정합 + facade 정합 가드.

T-0170 이 manifest/facade 전파 채널 갭을 노출했다. 실측 결론:
  - 갭2(templates manifest "stale") = 실체는 경로 집합 byte-identical, drift 는 주석뿐.
    → 경로 집합 정합을 못박아 *경로* drift(전파 채널 누락/잉여)는 즉시 fail 시킨다.
  - 갭3(pm-update.sh facade 부재) = 진짜 갭. pm_import 가 채택자 루트로 복사하는 facade
    (pm-config·pm-update)가 각 템플릿 트리에 전부 실재해야 채택자에 도달한다.

선례: SHIPPING_GLOBS↔manifest 동형(T-0154)·template_scaffold_parity. 모두 hermetic —
실 파일 존재/내용만 본다(LLM·subprocess 미진입). manifest 파싱은 pm_update.read_manifest
재사용(주석·`@마커` 제거를 한 곳에서 — 자체 파서 drift 회피).

historical 주의: 경로-집합 가드(가드 2)는 manifest *경로* 만 비교한다(주석 drift 는 무시 — 갭2
실체가 주석뿐). 폐기 용어 잔존은 별개 가드(test_terminology·T-0171 범위 확장)가 본다.

T-0176 보강(가드 2b·content 정합): 경로-집합·facade 가드는 공유 엔진 파일의 *내용* drift 를 못
잡는다(전파 누락·구버전 잔존이 회귀를 통과). 공유 엔진(manifest non-render·양 트리 실재)을
canonical ↔ 각 템플릿 byte-identical 로 강제해 그 갭을 메운다. 어댑터-비대칭(.claude/* vs
.opencode/*·@render 렌더 항목)은 스코프 밖 — render/target_owned 마커 + 경로 비대칭으로 자동 제외
(별도 content 화이트리스트 불요·path-set 화이트리스트와 동거).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

ROOT_MANIFEST = REPO / ".project_manager" / "engine.manifest"
CC_MANIFEST = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
OC_MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
CODEX_MANIFEST = REPO / "templates" / "codex" / ".project_manager" / "engine.manifest"

# opencode 트리의 정당한 manifest 차이(harness-correct·화이트리스트). 임의 경로가 새로
# 추가/누락되면 fail — 의도적 어댑터 비대칭만 통과시킨다(전파 채널 우발 drift 차단).
#   opencode 가 추가: .opencode 어댑터 트리(claude 의 .claude 대응) — agents·lib·plugins·
#     pm_orch_opencode.py(hook/driver). (`.opencode/command` 은 T-0364/ADR-0065 로 은퇴 — PM-workflow
#     스킬은 `.claude/skills` 단일 소비로 전환돼 이제 opencode 도 등록·claude 와 공유 경로.)
#   opencode 가 제외: .claude/agents(opencode 는 .opencode/agents)·ctx 훅·회귀 훅·relay 드라이버 +
#     regression.yml(claude-scoped CI 워크플로). `.claude/skills` 는 **더 이상 제외 아님** — opencode
#     (≥1.17.x)가 네이티브 스캔하는 canonical 스킬을 claude 와 동일 bare @render 로 공유한다(ADR-0065).
# NOTE(T-0305 supersedes T-0283): .opencode/lib·.opencode/plugins(ctx-guard core+shim)·pm_orch_opencode.py
#   는 T-0283 당시 `@target-owned` 등재=self-update skip(전파 0)이라 미등재였으나, T-0303 `@source`
#   채널(ADR-0054)이 그 비대칭(canonical=templates/opencode·루트 `.opencode/` 부재)을 이어 이제 engine
#   update 로 *전파*된다(engine-mirror·frozen 근절). 대칭으로 claude 는 ctx 훅·relay 드라이버를 등재.
#   settings.json·opencode.jsonc·루트 doc(CLAUDE/AGENTS)·local.conf 는 여전히 instance-owned(미등재).
OPENCODE_ONLY_PATHS = {
    ".opencode/agents",
    # pm-instructions.md (ADR-0069·T-0401): AGENTS.md 공통 코어에서 이관한 opencode-고유 운영 지침
    #   (실행 모델·위임 규약). @render @source 전파 등록이라 claude_code 엔 없는 opencode-only 경로.
    ".opencode/pm-instructions.md",
    ".opencode/lib", ".opencode/plugins", ".opencode/pm_orch_opencode.py",
    # .gitignore (T-0492): `.opencode/` 로컬 산출물(node_modules·package.json·lock) 무시 규칙.
    #   opencode 가 생성하는 원본은 자기 자신까지 무시해 영영 미추적으로 남는다 → 프레임워크가
    #   자기-은닉 줄을 뺀 판을 소유·@source 전파한다. claude 엔 없는 opencode-only 경로.
    ".opencode/.gitignore",
}
CLAUDE_ONLY_PATHS = {
    ".claude/agents", ".github/workflows/regression.yml",
    ".claude/ctx_guard.py", ".claude/ctx_stop_hook.py", ".claude/ctx_stop_hook.sh",
    ".claude/ctx_statusline.py", ".claude/ctx_statusline.sh",
    ".claude/pm_orch_claude.py", ".claude/run_tests_hook.sh",
}
# codex 트리(ADR-0070)의 정당한 manifest 차이(3-way·화이트리스트). claude_code 대비:
#   codex 가 추가: .codex/agents(TOML 4축 custom agent·claude .claude/agents 대응) · .agents/skills
#     (codex 네이티브 스킬 네임스페이스 — root `.claude/skills` 를 @source 로 remap·D2) ·
#     pm-dev-delegate file override(Codex native spawn schema를 shared Claude source와 분리·T-0435) ·
#     .codex/pm_orch_codex.py(relay 드라이버·engine-mirror·@source 전파·claude .claude/pm_orch_claude.py·
#     opencode .opencode/pm_orch_opencode.py 대응·T-0404) ·
#     .codex/rules/default.rules(execpolicy command policy·엔진 소유 위임 행동 규칙·claude 는 같은
#     경계를 settings.json deny 로 표현하는데 그건 instance-owned 라 대응 등재가 없다·T-0584.
#     codex 는 `.codex/rules/` 디렉토리의 `*.rules` 전부를 로드하므로 채택자 커스텀은 형제 파일로
#     분리 가능 → 이 파일 자체는 프레임워크 소유).
#   codex 가 제외: CLAUDE_ONLY_PATHS 전부 **+ .claude/skills**. opencode 는 .claude/skills 를 claude 와
#     공유(bare @render·같은 파일명 스캔)했지만 codex 는 스킬 네임스페이스가 `.agents/skills` 라
#     .claude/skills 자체는 codex manifest 에서 빠진다(→ .agents/skills remap 으로 대체). 이 한 줄이
#     opencode 화이트리스트와 codex 를 가르는 핵심 비대칭.
CODEX_ONLY_PATHS = {
    ".codex/agents",
    ".agents/skills",
    ".agents/skills/pm-dev-delegate/SKILL.md",
    ".codex/pm_orch_codex.py",
    ".codex/rules/default.rules",
}
CODEX_DROPPED_PATHS = CLAUDE_ONLY_PATHS | {".claude/skills"}
# engine-mirror hook/driver 등록 경로 (T-0305) — self-prop assert 및 등록 회귀 가드가 참조.
CLAUDE_HOOK_PATHS = frozenset({
    ".claude/ctx_guard.py", ".claude/ctx_stop_hook.py", ".claude/ctx_stop_hook.sh",
    ".claude/ctx_statusline.py", ".claude/ctx_statusline.sh",
    ".claude/pm_orch_claude.py", ".claude/run_tests_hook.sh",
})
OPENCODE_HOOK_PATHS = frozenset({
    ".opencode/lib", ".opencode/plugins", ".opencode/pm_orch_opencode.py",
})
# codex engine-mirror 드라이버 등록 경로 (ADR-0070·T-0404) — relay 드라이버만(ctx 가드는 driver-side·
#   config.toml/hooks.json 은 instance-owned). canonical=templates/codex 라 @source remap 필수.
CODEX_HOOK_PATHS = frozenset({
    ".codex/pm_orch_codex.py",
})
# manifest 자기전파 엔트리 (B-selfprop·T-0305·OQ-B1) — 3 매니페스트 모두 자신을 전파 대상에 넣어야
#   신 엔트리(위 hook/driver)가 기존 채택자에 도달한다.
SELF_PROP_PATH = ".project_manager/engine.manifest"

# pm_import 가 *채택자 루트로 복사*하는 facade 파일명 집합 (engine.manifest L33-34 주석·
# pm_import.plan_copy 동작). 템플릿 트리 전체가 채택자 루트로 복사되므로, 이 파일들이 각
# 템플릿 트리에 실재해야 채택자에 도달한다. pm-import 는 *manager 루트*(① worktree)에만 있고
# 채택자엔 안 간다(채택자는 자기를 import 할 일 없음) → 템플릿 트리 facade = config + update.
ADOPTER_FACADE_STEMS = ("pm-config", "pm-update")
FACADE_EXTS = (".sh", ".cmd")

TEMPLATE_ROOTS = {
    "claude_code": REPO / "templates" / "claude_code",
    "opencode": REPO / "templates" / "opencode",
    "codex": REPO / "templates" / "codex",
}

# 여러 harness가 같은 대상 relpath를 설치하면 ``plan_copy``는 byte가 다를 때 경고하고
# registry 첫 트리를 택한다. engine.manifest는 선택 선언의 합집합으로 다시 쓰는 예외라 이
# 사전 정합 대상에서 뺀다. 한 트리에만 있는 경로는 어댑터 소유일 수 있으므로 여기서 판단하지
# 않는다.
_IMPORT_MERGED_EXCEPTIONS = {
    ".project_manager/engine.manifest",
    # 단일 설치에서는 각 실제 진입 표기를 갖고, 다중 설치에서는 pm_import의 선언된 중립-source
    # override + 선택 flavor 병기가 충돌을 해소한다. 다른 공유 relpath는 계속 byte-identity 대상이다.
    "AGENTS.md",
}


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shared_import_diffs(template_roots: dict[str, Path]) -> list[str]:
    """다중-harness 설치에서 실제로 병합되는 공유 relpath의 byte drift를 반환한다.

    두 트리 이상에 있는 파일만 비교한다. 한 트리 부재는 어댑터-고유 경로일 수 있어 여기서는
    판정하지 않는다. manifest path-set 가드는 등록된 경로만 다루므로, 미등록 출하 relpath의
    부재 정합은 이 가드 계열의 범위 밖이며 별도 후속 범위다. 같은 경로가 둘 이상에서 출하될
    때의 거짓 충돌만 이 가드의 책임이다.
    """
    pm_import = _load_pm_import()
    sources: dict[str, list[Path]] = {}
    for root in template_roots.values():
        for rel, source in pm_import._iter_source_files(root, "full"):
            rel_s = rel.as_posix()
            if rel_s not in _IMPORT_MERGED_EXCEPTIONS:
                sources.setdefault(rel_s, []).append(source)
    return sorted(
        rel for rel, paths in sources.items()
        if len(paths) > 1 and any(path.read_bytes() != paths[0].read_bytes() for path in paths[1:])
    )


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


def test_codex_manifest_diff_is_whitelisted_only():
    """codex 템플릿 manifest 는 harness-correct 하게 다르되, 차이가 화이트리스트에만 있어야 한다 (ADR-0070·T-0402).

    claude_code 대비 codex 의 추가(.codex/agents·.agents/skills)/제외(CLAUDE_ONLY_PATHS + .claude/skills)가
    의도적 어댑터 비대칭에만 있음을 단언 — 임의 경로가 새로 들고/빠지면 fail(3-way 전파 채널 우발 drift
    차단). opencode 와의 핵심 차이 = codex 는 .claude/skills 도 제외(→ .agents/skills 로 remap)한다."""
    cc = _manifest_path_set(CC_MANIFEST)
    cx = _manifest_path_set(CODEX_MANIFEST)
    added = cx - cc        # codex 가 추가한 경로
    dropped = cc - cx      # codex 가 제외한 경로
    assert added == CODEX_ONLY_PATHS, (
        f"codex manifest 추가 경로가 화이트리스트와 불일치 — "
        f"예상 {sorted(CODEX_ONLY_PATHS)}, 실제 {sorted(added)}"
    )
    assert dropped == CODEX_DROPPED_PATHS, (
        f"codex manifest 제외 경로가 화이트리스트와 불일치 — "
        f"예상 {sorted(CODEX_DROPPED_PATHS)}, 실제 {sorted(dropped)}"
    )


def test_codex_and_opencode_agents_md_differ_only_by_native_entry_notation():
    """단일-harness AGENTS 코어는 실제 호출 표기 외 같은 내용을 유지한다."""
    oc_agents = REPO / "templates" / "opencode" / "AGENTS.md"
    cx_agents = REPO / "templates" / "codex" / "AGENTS.md"
    assert oc_agents.is_file(), f"opencode AGENTS.md 없음: {oc_agents}"
    assert cx_agents.is_file(), f"codex AGENTS.md 없음: {cx_agents}"
    normalize = lambda text: re.sub(
        r"(?<![A-Za-z0-9_.>/\-])[/\$](pm-[a-z][a-z0-9-]*)",
        r"<ENTRY>\1",
        text,
    )
    assert normalize(oc_agents.read_text(encoding="utf-8")) == normalize(
        cx_agents.read_text(encoding="utf-8")
    ), "codex/opencode AGENTS 코어가 실제 호출 표기 외 내용까지 drift"


# ── 가드 2c: engine-mirror hook/driver 등록 + manifest 자기전파 (T-0305·ADR-0032 Q3) ──────────


def _entry_source_rel(pm_update, manifest: Path, relpath: str) -> str | None:
    """manifest 에서 relpath 엔트리의 `@source=<rel>` 값(없으면 None). 등록 여부·소스 remap 검증용."""
    for entry in pm_update.read_manifest(manifest):
        if str(entry) == relpath:
            return getattr(entry, "source_rel", None)
    return None


def test_claude_manifests_register_engine_mirror_hooks_and_driver():
    """루트·claude_code manifest 가 ctx 훅·회귀 훅·relay 드라이버를 engine-mirror 로 등록 (T-0305).

    frozen 근절 — 이 파일들이 manifest 안이라야 pm-update self-update 로 전파된다(엔진 safety-훅 fix
    가 채택자에 닿음). ctx 훅/드라이버는 ship 템플릿(templates/claude_code/.claude/*)이 canonical 이라
    `@source=` remap 을 달고, run_tests_hook.sh 는 루트 `.claude/` 실재라 root-sourced(bare)."""
    pm_update = _load_pm_update()
    for name, manifest in (("root", ROOT_MANIFEST), ("claude_code", CC_MANIFEST)):
        paths = _manifest_path_set(manifest)
        missing = CLAUDE_HOOK_PATHS - paths
        assert not missing, f"{name} manifest 에 engine-mirror hook/driver 미등록: {sorted(missing)}"
        # ctx 훅/드라이버(루트 .claude/ 부재)는 @source=templates/claude_code/... remap 필수 —
        # 안 달면 self-update 가 루트에서 소스를 못 찾아 rc2(전파 실패).
        for rel in CLAUDE_HOOK_PATHS - {".claude/run_tests_hook.sh"}:
            src = _entry_source_rel(pm_update, manifest, rel)
            assert src == f"templates/claude_code/{rel}", (
                f"{name} manifest {rel} 의 @source remap 이 templates/claude_code/ 를 가리키지 않음: {src!r}")


def test_opencode_manifest_registers_engine_mirror_hooks_and_driver():
    """opencode manifest 가 ctx-guard core/plugin·relay 드라이버를 @source 로 등록 (T-0305·T-0303 채널).

    T-0283 미등재(전파 0)를 T-0303 @source 채널로 해소 — canonical=templates/opencode 라 remap 으로
    루트-부재 비대칭을 잇고 이제 engine update 로 전파된다(frozen 근절)."""
    pm_update = _load_pm_update()
    paths = _manifest_path_set(OC_MANIFEST)
    missing = OPENCODE_HOOK_PATHS - paths
    assert not missing, f"opencode manifest 에 engine-mirror hook/driver 미등록: {sorted(missing)}"
    for rel in OPENCODE_HOOK_PATHS:
        src = _entry_source_rel(pm_update, OC_MANIFEST, rel)
        assert src == f"templates/opencode/{rel}", (
            f"opencode manifest {rel} 의 @source remap 이 templates/opencode/ 를 가리키지 않음: {src!r}")


def test_codex_manifest_registers_relay_driver():
    """codex manifest 가 relay 드라이버(pm_orch_codex.py)를 @source 로 등록 (ADR-0070·T-0404).

    canonical=templates/codex(루트 `.codex/` 부재)라 @source remap 필수 — 안 달면 self-update 가
    루트에서 소스를 못 찾아 rc2(전파 실패). opencode pm_orch_opencode.py 등록 가드 대칭.
    (ctx 가드는 driver-side usage 판정이라 별도 hook 파일 없음·config.toml/hooks.json 은 instance-owned.)"""
    pm_update = _load_pm_update()
    paths = _manifest_path_set(CODEX_MANIFEST)
    missing = CODEX_HOOK_PATHS - paths
    assert not missing, f"codex manifest 에 engine-mirror relay 드라이버 미등록: {sorted(missing)}"
    for rel in CODEX_HOOK_PATHS:
        src = _entry_source_rel(pm_update, CODEX_MANIFEST, rel)
        assert src == f"templates/codex/{rel}", (
            f"codex manifest {rel} 의 @source remap 이 templates/codex/ 를 가리키지 않음: {src!r}")


def test_all_manifests_self_propagate():
    """4 매니페스트 모두 자기 자신(`.project_manager/engine.manifest`)을 전파 대상에 등록 (B-selfprop·OQ-B1·ADR-0070).

    없으면 이 파일에 새로 추가한 hook/driver 엔트리(=manifest 진화)가 기존 채택자에 영영 도달하지
    못한다(import 시점 frozen manifest 영속) → "frozen 근절" 이 성립 불가. 루트는 self-flavor(bare),
    claude_code/opencode/codex 는 각자 flavor 매니페스트를 @source 로 읽는다(harness-flavor remap)."""
    pm_update = _load_pm_update()
    for name, manifest in (("root", ROOT_MANIFEST), ("claude_code", CC_MANIFEST),
                           ("opencode", OC_MANIFEST), ("codex", CODEX_MANIFEST)):
        assert SELF_PROP_PATH in _manifest_path_set(manifest), (
            f"{name} manifest 가 자기전파 엔트리({SELF_PROP_PATH}) 미등록 — 신 엔트리가 채택자에 미도달")
    # claude_code/opencode/codex 는 flavor 매니페스트를 @source 로 읽어야 자기 flavor 를 받는다(교차 오염 방지).
    assert _entry_source_rel(pm_update, CC_MANIFEST, SELF_PROP_PATH) == \
        "templates/claude_code/.project_manager/engine.manifest"
    assert _entry_source_rel(pm_update, OC_MANIFEST, SELF_PROP_PATH) == \
        "templates/opencode/.project_manager/engine.manifest"
    assert _entry_source_rel(pm_update, CODEX_MANIFEST, SELF_PROP_PATH) == \
        "templates/codex/.project_manager/engine.manifest"


def test_instance_owned_config_not_registered():
    """adopter config(settings.json·opencode.jsonc·루트 doc·local.conf·precompact)는 manifest 밖·미전파 (T-0305 DoD).

    hooks/driver 는 전파하되 adopter-소유 config 는 전파하지 않는다(customization clobber 방지·ADR-0032 Q3).
    precompact_capture_hook.sh 는 ship 템플릿 부재·루트 settings 전용이라 engine-mirror 아님(미등록)."""
    forbidden = {
        "root": (ROOT_MANIFEST, {
            ".claude/settings.json", ".claude/precompact_capture_hook.sh",
            "CLAUDE.md", ".project_manager/local.conf"}),
        "claude_code": (CC_MANIFEST, {".claude/settings.json", "CLAUDE.md"}),
        "opencode": (OC_MANIFEST, {".opencode/opencode.jsonc", "AGENTS.md", "AGENTS.lite.md"}),
        # codex(ADR-0070·§3.6): AGENTS.md(공통 코어 root doc)·.codex/config.toml·.codex/hooks.json 는
        #   adopter config/root doc 라 manifest 밖(미전파·trust 재승인 churn 회피). config/hooks 는
        #   T-0406 이 물리 배치하되 여기선 "manifest 에 미등록"만 단언(경로 부재 무관·경로집합 검사).
        "codex": (CODEX_MANIFEST, {"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
    }
    for name, (manifest, forbidden_paths) in forbidden.items():
        leaked = forbidden_paths & _manifest_path_set(manifest)
        assert not leaked, f"{name} manifest 가 instance-owned config 를 등록(전파 위반): {sorted(leaked)}"


# ── 가드 2b: content 정합 (공유 엔진 byte-identical) ─────────────────────────


def test_shared_imported_relpaths_are_byte_identical():
    """실제 다중-harness 병합 공유 파일은 byte-identical이라 첫 트리 경고를 만들지 않는다."""
    diffs = _shared_import_diffs(TEMPLATE_ROOTS)
    assert not diffs, (
        "다중-harness 설치 공유 relpath content drift — 첫 트리 우선 경고의 거짓 양성: "
        f"{diffs}"
    )


def test_shared_import_guard_classifies_equal_missing_and_byte_drift(tmp_path):
    """동일·어댑터 부재는 통과하고 0/1바이트를 포함한 공유 drift는 검출한다."""
    roots = {name: tmp_path / name for name in TEMPLATE_ROOTS}
    rel = Path("tickets") / ".gitkeep"
    for root in roots.values():
        target = root / rel
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")
    assert _shared_import_diffs(roots) == []

    # 0-byte vs 1-byte는 사람이 보기엔 사소해도 설치 충돌의 원인이므로 red다.
    (roots["codex"] / rel).write_bytes(b"\n")
    assert _shared_import_diffs(roots) == [rel.as_posix()]

    # 한 harness 부재는 adapter-owned 후보라 이 content 가드에서는 판정하지 않는다.
    (roots["codex"] / rel).unlink()
    (roots["codex"] / "adapter-only.txt").write_bytes(b"adapter\n")
    assert _shared_import_diffs(roots) == []


def test_shared_import_guard_detects_byte_drift_in_exactly_two_trees(tmp_path):
    """세 harness 중 정확히 둘이 출하한 relpath도 byte drift면 검출한다."""
    roots = {name: tmp_path / name for name in TEMPLATE_ROOTS}
    for root in roots.values():
        root.mkdir()
        (root / "common.txt").write_bytes(b"same\n")
    rel = Path(".claude") / "skills" / "example" / "SKILL.md"
    for name, content in (("claude_code", b"claude\n"), ("opencode", b"opencode\n")):
        target = roots[name] / rel
        target.parent.mkdir(parents=True)
        target.write_bytes(content)

    assert _shared_import_diffs(roots) == [rel.as_posix()]


def _expand_manifest_files(base: Path, relpath: str) -> dict[str, Path]:
    """manifest 경로 1개를 `{rel파일경로: 절대경로}` 로 전개 (파일=자기 자신·디렉토리=재귀 파일).

    base 아래 해당 경로가 없으면 빈 dict (경로 비대칭 — path-set 가드 소관, content 가드 밖)."""
    p = base / relpath
    if p.is_file():
        return {relpath: p}
    if p.is_dir():
        return {
            str(f.relative_to(base)): f
            for f in repo_owned_paths(base, relpath, mode=OWNED)
        }
    return {}


def _engine_content_diffs(template_root: Path, manifest_entries=None) -> list[str]:
    """canonical(REPO 루트) ↔ template_root 의 *공유 엔진* 파일 byte 차이 리스트 (정합용 helper).

    스코프 = manifest 항목 중 ``@render``/``@target-owned`` 가 아니고(=byte-copy 계약·렌더/타깃소유는
    내용이 갈릴 수 있어 제외) **양 트리에 실재**하는(경로 비대칭은 path-set 가드 소관) 파일. 디렉토리
    항목은 재귀 파일 단위로 본다. read_manifest(pm_update) 재사용 — 자체 파서 금지(주석·`@마커` 제거 동형).

    sensitivity 용으로 ``manifest_entries`` 를 주입할 수 있다(미지정 시 ROOT_MANIFEST 파싱). 반환은
    drift 파일의 rel 경로 + 비대칭은 ``MISSING:`` 접두(양 트리 모두 present 인 디렉토리 내부 누락)."""
    pm_update = _load_pm_update()
    if manifest_entries is None:
        manifest_entries = pm_update.read_manifest(ROOT_MANIFEST)
    diffs: list[str] = []
    for entry in manifest_entries:
        if getattr(entry, "render", False) or getattr(entry, "target_owned", False):
            continue  # 렌더/타깃소유 = byte-copy 계약 밖
        rel = str(entry)
        if rel == SELF_PROP_PATH:
            # 매니페스트 자기전파(B-selfprop·T-0305): 매니페스트는 harness-flavor(루트↔claude_code↔
            # opencode 주석/@source 상이)라 공유-엔진 byte-parity 대상이 아니다. path-set·self-prop·
            # 등록 가드가 구조를 지키고, content 는 flavor-specific 이라 제외한다.
            continue
        canon_files = _expand_manifest_files(REPO, rel)
        if not canon_files:
            continue  # canonical 부재 — 별개 사안(여기선 비교 불가)
        tmpl_files = _expand_manifest_files(template_root, rel)
        if not tmpl_files:
            continue  # 경로 비대칭(어댑터-고유) — path-set 화이트리스트 가드 소관
        for rel_file in sorted(canon_files):
            tmpl_path = tmpl_files.get(rel_file)
            if tmpl_path is None:
                diffs.append(f"MISSING:{rel_file}")  # 디렉토리 내부 파일 누락
            elif canon_files[rel_file].read_bytes() != tmpl_path.read_bytes():
                diffs.append(rel_file)
    return diffs


def test_shared_engine_files_are_byte_identical_across_templates():
    """공유 엔진 파일(manifest non-render·양 트리 실재)이 canonical ↔ 각 템플릿 byte-identical.

    pm_update overwrite 계약 — `.project_manager/tools/**`·`wiki/_template`·`.gitignore`·`.gitattributes`
    등 공유 엔진은 양 템플릿 트리에 canonical 과 1바이트도 다르지 않아야 한다. content drift(전파
    누락·구버전 잔존)를 즉시 fail 시킨다 — path-set 가드(경로만)·facade 가드(존재만)가 못 보던 갭.
    어댑터-비대칭(.claude/* vs .opencode/*·@render 렌더 항목)은 스코프 밖(helper 가 제외)."""
    for name, root in TEMPLATE_ROOTS.items():
        diffs = _engine_content_diffs(root)
        assert not diffs, (
            f"'{name}' 템플릿의 공유 엔진 파일이 canonical 과 content drift — {sorted(diffs)}. "
            "pm_update 전파 누락/구버전 잔존 — 엔진을 다시 전파(pm_update --target)해야 한다."
        )


def test_content_guard_is_sensitive_to_drift():
    """sensitivity — 고의로 1바이트 다른 가상 template 트리에 helper 가 drift 를 검출함을 입증(non-vacuous).

    실 트리는 안 건드린다 — canonical(REPO) board.py 내용에 1바이트를 더한 사본을 임시 디렉토리
    (가상 template_root)에 만들어 helper 에 주입한다. canonical 은 실 파일이라 불변. helper 가 그 1바이트
    차이를 잡아내면(diff == [board.py]) 가드가 vacuous 하지 않음이 입증된다. 끝에 동일-트리 음성 통제로
    false-positive 가 아님도 확인한다."""
    import tempfile

    pm_update = _load_pm_update()
    # board.py 엔트리 하나만 골라 격리 (단일 파일·non-render).
    entry = next(
        e for e in pm_update.read_manifest(ROOT_MANIFEST)
        if str(e) == ".project_manager/tools/board.py"
    )
    rel = str(entry)
    canon_file = REPO / rel

    with tempfile.TemporaryDirectory() as td:
        fake_root = Path(td)
        target = fake_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # canonical 내용 + 1바이트 → 고의 drift (실 파일 미변경).
        target.write_bytes(canon_file.read_bytes() + b"\n# sensitivity drift\n")

        diffs = _engine_content_diffs(fake_root, manifest_entries=[entry])
        assert diffs == [rel], (
            f"content 가드가 1바이트 drift 를 못 잡음(vacuous 위험) — 검출 {diffs}, 예상 [{rel!r}]"
        )

    # 음성 통제: 동일 입력(canonical=REPO 자신)엔 0 diff (false-positive 아님 확인).
    no_diff = _engine_content_diffs(REPO, manifest_entries=[entry])
    assert no_diff == [], f"동일 트리에 false-positive drift 검출 — {no_diff}"


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
