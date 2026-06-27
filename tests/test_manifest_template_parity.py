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


# ── 가드 2b: content 정합 (공유 엔진 byte-identical) ─────────────────────────


def _expand_manifest_files(base: Path, relpath: str) -> dict[str, Path]:
    """manifest 경로 1개를 `{rel파일경로: 절대경로}` 로 전개 (파일=자기 자신·디렉토리=재귀 파일).

    base 아래 해당 경로가 없으면 빈 dict (경로 비대칭 — path-set 가드 소관, content 가드 밖)."""
    p = base / relpath
    if p.is_file():
        return {relpath: p}
    if p.is_dir():
        return {
            str(f.relative_to(base)): f
            for f in sorted(p.rglob("*"))
            if f.is_file()
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
