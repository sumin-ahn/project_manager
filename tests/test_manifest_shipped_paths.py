"""flavor manifest ↔ 템플릿 트리 **양방향** 커버리지 가드.

두 방향을 한 파일에서 본다 — 앞쪽(T-0544 ④)은 "등록됐는데 안 실림", 뒤쪽(T-0584)은 "실렸는데
어느 채널에도 안 등록됨". 같은 두 집합(manifest 등록 경로 ↔ 템플릿 트리 출하 파일)의 대칭 결함이라
파서·스코프 경계 helper 를 공유한다.

T-0544 ④ — flavor manifest 등록 경로의 템플릿 트리 실재 가드 (일반화).

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

from _harness_matrix import HARNESSES, HARNESS_ADAPTER_DIRS, HARNESS_ROOT_DOC
from _repo_owned_inventory import TRACKED_ONLY, repo_owned_paths


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FLAVORS = ("claude_code", "codex", "opencode")
TEMPLATE_ROOTS = {flavor: REPO / "templates" / flavor for flavor in FLAVORS}
FLAVOR_MANIFESTS = {
    flavor: root / ".project_manager" / "engine.manifest"
    for flavor, root in TEMPLATE_ROOTS.items()
}


@functools.lru_cache(maxsize=1)
def _load_pm_import():
    """pm_import 모듈 (프로세스당 1회 exec·캐시).

    역방향 가드의 (b) 축이 소비하는 **선언 상수**(instance-owned 파일·add-harness create-if-absent)
    출처. 손 목록 사본을 두면 선언과 가드가 각자 진화해 결국 다른 진실을 말한다."""
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# ── 역방향: 출하됐는데 어느 채널에도 안 실린 어댑터 파일 (T-0584) ────────────────────
#
# 닫는 결함 클래스 = "출하는 되는데 갱신 채널이 0". manifest 에 없으면 pm_update 가 안 덮고
# add-harness guest 절(T-0574)도 flavor manifest 파생이라 못 실어서, 채택자는 import 시점 사본을
# 영구히 들고 산다(frozen). 실례가 codex `.codex/rules/default.rules` 였다 — 주하네스 채택자에게도,
# guest 채택자에게도 도달 채널이 없었고 manifest 기반 frozen 경고로는 관측조차 안 됐다.
#
# 판정: 어댑터 네임스페이스의 git-추적 출하 파일은 다음 **정확히 하나**를 만족해야 한다.
#   (a) 그 flavor manifest 에 등재 — 파일 직접 또는 디렉토리 엔트리 하위. `@source` 항목은 등재
#       dest 경로와 in-tree 소스 경로 **양쪽**을 커버로 친다(codex `.agents/skills` 는 소스가
#       루트 `.claude/skills` 라 dest 로만, `.codex/pm_orch_codex.py` 는 둘이 같다).
#   (b) 엔진의 **instance-owned 선언**에 명시 — 채택자 소유라 상류가 안 덮는 파일
#       (`INSTANCE_OWNED_ADAPTER_FILES`·pm_import). 선언 상수를 import 해 소비한다.
#
# 스코프 = 어댑터 네임스페이스(`ADD_HARNESS_ADAPTER` 파생 adapter dirs + 루트 doc). 루트 doc 의
# lite 변형(`CLAUDE.lite.md`·`AGENTS.lite.md`)은 **채택자 경로가 아니라** 같은 dest(`CLAUDE.md`)의
# 무게축 변형 소스라 별도 대상이 아니다 — 루트 doc 분류가 그 dest 를 이미 덮는다. 네임스페이스 밖
# (엔진 트리·루트 파사드·`.github/`)은 이 가드의 관심이 아니다(그쪽은 앞 절의 정방향 가드와
# manifest-parity 계열이 본다).


def _flavor_template_dir(harness: str) -> str:
    """하네스의 `templates/` 어댑터 트리 디렉토리명 (엔진 HARNESS_TEMPLATE_DIRS 파생)."""
    (dirname,) = _load_pm_import().HARNESS_TEMPLATE_DIRS[harness]
    return dirname


def _manifest_covered_relpaths(manifest: Path, template_root: Path) -> set[str]:
    """manifest 가 커버하는 **템플릿 트리 상대** 경로 집합 (파일/디렉토리 prefix 혼재).

    등재 dest 경로 + in-tree `@source` 소스 경로 양쪽을 넣는다 — 트리의 실 파일이 어느 쪽 이름으로
    있든(remap 여부에 따라 다르다) 커버로 잡히게 하려는 것이고, 둘 다 넣어도 과탐이 아니다(어느
    쪽이든 그 경로는 실제로 이 flavor 의 동기 채널을 탄다)."""
    covered: set[str] = set()
    for entry in _load_pm_update().read_manifest(manifest):
        covered.add(str(entry).replace("\\", "/"))
        in_tree = _in_tree_source_rel(entry, template_root)
        if in_tree is not None:
            covered.add(in_tree)
    return covered


def _unclassified_shipped_files(
        relpaths, covered: set[str], instance_owned) -> list[str]:
    """(a) manifest 등재 · (b) instance-owned 선언 **어느 축에도** 없는 경로 (순수 판정).

    실 트리 없이 호출 가능한 순수 함수라 sensitivity 케이스가 같은 판정을 직접 태운다."""
    return sorted(
        rel for rel in relpaths
        if rel not in instance_owned
        and not any(rel == c or rel.startswith(c + "/") for c in covered)
    )


def _adapter_namespace_relpaths(harness: str) -> list[str]:
    """flavor 템플릿 트리의 어댑터 네임스페이스 git-추적 출하 파일 전수 (트리 상대·POSIX).

    열거는 tracked-only seam — 추적 안 되는 로컬 산출물(빌드 잔재·백업)은 출하물이 아니라
    분류 대상이 아니다."""
    flavor = _flavor_template_dir(harness)
    template_root = REPO / "templates" / flavor
    subtrees = [f"templates/{flavor}/{d}" for d in HARNESS_ADAPTER_DIRS[harness]]
    subtrees.append(f"templates/{flavor}/{HARNESS_ROOT_DOC[harness]}")
    rels: set[str] = set()
    for subtree in subtrees:
        for path in repo_owned_paths(REPO, subtree, mode=TRACKED_ONLY):
            rels.add(path.relative_to(template_root).as_posix())
    return sorted(rels)


@pytest.mark.parametrize("harness", HARNESSES)
def test_shipped_adapter_files_are_registered_or_declared_instance_owned(harness):
    """어댑터 네임스페이스 출하 파일이 전부 (a) manifest 등재 또는 (b) instance-owned 선언이다.

    둘 다 아니면 그 파일은 **출하되지만 갱신 채널이 0** — 상류 fix 가 기존 채택자에 영영 도달하지
    않고, manifest 기반 관측에도 안 잡힌다."""
    flavor = _flavor_template_dir(harness)
    template_root = REPO / "templates" / flavor
    shipped = _adapter_namespace_relpaths(harness)
    assert shipped, (
        f"'{harness}' 어댑터 네임스페이스에서 추적 출하 파일이 0건 — 열거 스코프가 빗나갔다"
        "(가드가 vacuous)")
    covered = _manifest_covered_relpaths(
        template_root / ".project_manager" / "engine.manifest", template_root)
    instance_owned = _load_pm_import().INSTANCE_OWNED_ADAPTER_FILES[harness]

    unclassified = _unclassified_shipped_files(shipped, covered, instance_owned)

    assert unclassified == [], (
        f"'{harness}' 어댑터 출하 파일이 어느 채널에도 없다(영구 동결): {unclassified}. "
        f"처방 둘 중 하나 — (a) 프레임워크 소유면 templates/{flavor}/.project_manager/"
        "engine.manifest 에 등재(비-@render `@source` 행), (b) 채택자 소유면 pm_import "
        "INSTANCE_OWNED_ADAPTER_FILES 에 선언."
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_instance_owned_declaration_ships_and_stays_out_of_manifest(harness):
    """instance-owned 선언이 실 출하 파일을 가리키고, 동시에 manifest 등재가 아니다.

    두 방향 모두 봐야 선언이 살아 있다 — 실재 안 하면 오타/폐기 잔재(가드를 조용히 무력화)이고,
    manifest 에도 있으면 '채택자 소유' 와 'pm_update 가 덮음' 이 동시에 참인 모순 상태다."""
    flavor = _flavor_template_dir(harness)
    template_root = REPO / "templates" / flavor
    covered = _manifest_covered_relpaths(
        template_root / ".project_manager" / "engine.manifest", template_root)
    declared = sorted(_load_pm_import().INSTANCE_OWNED_ADAPTER_FILES[harness])
    assert declared, f"'{harness}' instance-owned 선언이 비었다 — 루트 doc 은 늘 채택자 소유다"

    missing = [rel for rel in declared if not (template_root / rel).is_file()]
    assert missing == [], (
        f"'{harness}' instance-owned 선언이 출하되지 않는 경로를 가리킴(오타/폐기 잔재): {missing}")
    registered = [
        rel for rel in declared
        if any(rel == c or rel.startswith(c + "/") for c in covered)
    ]
    assert registered == [], (
        f"'{harness}' instance-owned 선언 경로가 manifest 에도 등재됨(소유 모순): {registered}")


def test_create_if_absent_policy_is_subset_of_instance_owned_declaration():
    """add-harness create-if-absent 정책 ⊆ instance-owned 선언 (두 상수 drift 차단).

    create-if-absent 는 '소유' 가 아니라 그 소유에 딸린 **add-harness 복사 정책**이다. 선언에 없는
    경로를 정책에만 넣으면 소유 진실이 둘로 갈린다(가드는 그 경로를 미분류로 red)."""
    pm_import = _load_pm_import()
    for harness, policy in pm_import.ADD_HARNESS_CREATE_IF_ABSENT.items():
        declared = pm_import.INSTANCE_OWNED_ADAPTER_FILES[harness]
        assert set(policy) <= set(declared), (
            f"'{harness}' create-if-absent 정책에 instance-owned 미선언 경로: "
            f"{sorted(set(policy) - set(declared))}")


def test_reverse_guard_is_sensitive_to_an_unclassified_shipped_file():
    """sensitivity — 미분류 출하 파일을 주입하면 red 임을 입증(non-vacuous·실 트리 미변경).

    네 갈래를 한 번에 태운다: 디렉토리 엔트리 하위(커버) · 파일 엔트리 정확일치(커버) ·
    instance-owned 선언(커버) · 어느 쪽도 아님(미분류 1건). prefix 매칭이 경로 경계를 안 지키면
    `.fake/agentsX/…` 가 `.fake/agents` 로 잘못 커버돼 여기서 잡힌다."""
    covered = {".fake/agents", ".fake/pm_orch_fake.py"}
    instance_owned = frozenset({"FAKE.md", ".fake/config.toml"})
    shipped = [
        ".fake/agents/architect.toml",   # (a) 디렉토리 엔트리 하위
        ".fake/agentsX/stray.toml",      # 미분류 — prefix 경계 미준수면 조용히 커버된다
        ".fake/pm_orch_fake.py",         # (a) 파일 엔트리 정확일치
        ".fake/config.toml",             # (b) instance-owned 선언
        "FAKE.md",                       # (b) 루트 doc
    ]

    assert _unclassified_shipped_files(shipped, covered, instance_owned) == [
        ".fake/agentsX/stray.toml"]
    # 음성 통제: 그 파일을 등재하면 0건 (false-positive 아님 확인).
    assert _unclassified_shipped_files(
        shipped, covered | {".fake/agentsX"}, instance_owned) == []
