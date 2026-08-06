"""T-0541 ①⑥ — 출하 wiki relpath 의 manifest 소유권과 표기 폴백 정합.

## 닫는 결함
`engine.manifest` 어느 엔트리도 소유하지 않는 출하 wiki 문서는 표기 context 를 못 받아
**조용히 skip** 됐다. 그래서 codex+opencode·all 설치에서 `.project_manager/wiki/raw/README.md`
가 canonical `/spike-new` 그대로 출하됐다(단일 하네스 설치는 표기가 같아 안 드러남).

## 소유권 판정 (① 택일 근거)
manifest 미소유 출하 wiki = **인스턴스 상태 seed**(`status.md`·`log/current.md`·`architecture.md`·
`raw/README.md` 등)다. manifest 는 upstream 이 관리(=매 sync 덮어씀)하는 경로만 담으므로
([[ADR-0001]] 엔진/상태 분리) 이 부류를 등재하면 `pm_update` 가 채택자 상태를 clobber 한다.
따라서 (b) fail-loud(등재 강제)가 아니라 **(a) 설치 하네스 전체 집합 폴백**이 정답이고,
`pm_import._is_notation_fallback_scope` 가 그 범위를 소유한다.

## 이 파일이 보는 것
1. 출하 wiki relpath 집합 ↔ manifest 등재 집합의 **path-set parity** — 미등재 출하 파일 원장.
   신규 미등재 출하 파일이 생기면 red(판정 강제: 인스턴스 seed 면 원장에, framework-managed 면
   manifest 에).
2. 원장 전체가 폴백 범위 안 — ①의 택일과 정합(등재 없이도 표기가 해소된다).
3. 폴백 렌더 동작·loud 표기·범위 경계(엔진 backbone·루트 진입문서 불가침).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from _harness_matrix import HARNESSES, REPO, TEMPLATES, _PM_IMPORT
from _repo_owned_inventory import TRACKED_ONLY, repo_owned_paths


def _load_pm_update():
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    spec = importlib.util.spec_from_file_location("wiki_ownership_pm_update", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PM_UPDATE = _load_pm_update()

WIKI_PREFIX = ".project_manager/wiki/"

# manifest 미소유 출하 wiki relpath **원장**(세 template 합집합·tracked-only 출하 판정).
# 전부 인스턴스 상태 seed 라 upstream 관리 대상이 아니며(등재하면 pm_update 가 채택자 상태를
# 덮는다) 표기는 설치 하네스 전체 집합 폴백으로 해소된다. 새 출하 파일을 여기 넣기 전에
# 판정하라 — framework 가 관리할 문서면 원장이 아니라 `engine.manifest` 에 등재해야 한다.
# 유지보수자 checkout 의 미추적 파일은 채택자에게 가지 않으므로 원장 대상이 아니다(tracked-only).
UNOWNED_SHIPPED_WIKI_LEDGER = frozenset({
    ".project_manager/wiki/architecture.md",
    ".project_manager/wiki/decisions/README.md",
    ".project_manager/wiki/domain/README.md",
    ".project_manager/wiki/ideas/README.md",
    ".project_manager/wiki/ideas/killed/.gitkeep",
    ".project_manager/wiki/ideas/open/.gitkeep",
    ".project_manager/wiki/ideas/promoted/.gitkeep",
    ".project_manager/wiki/log/archive/.gitkeep",
    ".project_manager/wiki/log/current.md",
    ".project_manager/wiki/pm_role.local.md",
    ".project_manager/wiki/raw/README.md",
    ".project_manager/wiki/specs/README.md",
    ".project_manager/wiki/status.md",
    ".project_manager/wiki/status_done.md",
    ".project_manager/wiki/tickets/README.md",
    ".project_manager/wiki/tickets/blocked/.gitkeep",
    ".project_manager/wiki/tickets/claimed/.gitkeep",
    ".project_manager/wiki/tickets/done/.gitkeep",
    ".project_manager/wiki/tickets/open/.gitkeep",
})


def _template_dir(harness: str) -> str:
    (dirname,) = _PM_IMPORT.HARNESS_TEMPLATE_DIRS[harness]
    return dirname


def _shipped_wiki_relpaths(template_dir: str) -> set[str]:
    """template 트리가 실제로 출하하는 wiki relpath.

    출하 판정은 production 과 같은 tracked-only 의미다(`_iter_source_files` 의 TRACKED_ONLY) —
    유지보수자 checkout 의 미추적 파일은 채택자에게 가지 않으므로 원장 대상도 아니다."""
    root = TEMPLATES / template_dir
    return {
        path.relative_to(root).as_posix()
        for path in repo_owned_paths(
            REPO, root.relative_to(REPO), mode=TRACKED_ONLY
        )
        if path.relative_to(root).as_posix().startswith(WIKI_PREFIX)
    }


def _manifest_entries(template_dir: str) -> set[str]:
    manifest = TEMPLATES / template_dir / ".project_manager" / "engine.manifest"
    return {
        str(entry).replace("\\", "/")
        for entry in _PM_UPDATE.read_manifest(manifest)
    }


def _is_owned(rel: str, entries: set[str]) -> bool:
    """manifest 엔트리(파일 정확일치 OR 디렉토리 prefix)가 이 relpath 를 소유하는가."""
    return any(
        rel == entry.rstrip("/") or rel.startswith(entry.rstrip("/") + "/")
        for entry in entries
    )


def _unowned_shipped_wiki(template_dir: str, extra: tuple[str, ...] = ()) -> set[str]:
    entries = _manifest_entries(template_dir)
    shipped = _shipped_wiki_relpaths(template_dir) | set(extra)
    return {rel for rel in shipped if not _is_owned(rel, entries)}


# ── ⑥ path-set parity ─────────────────────────────────────────────────────────

def test_unowned_shipped_wiki_relpaths_match_declared_ledger():
    """출하 wiki 중 manifest 미등재인 것의 합집합 == 원장. 신규 미등재 출하 파일이면 red."""
    union: set[str] = set()
    for harness in HARNESSES:
        union |= _unowned_shipped_wiki(_template_dir(harness))
    assert union == set(UNOWNED_SHIPPED_WIKI_LEDGER), (
        "manifest 미등재 출하 wiki 집합이 원장과 다르다 — "
        f"신규: {sorted(union - set(UNOWNED_SHIPPED_WIKI_LEDGER))} · "
        f"사라짐: {sorted(set(UNOWNED_SHIPPED_WIKI_LEDGER) - union)}. "
        "인스턴스 상태 seed 면 원장에 추가하고, framework 관리 문서면 engine.manifest 에 등재하라."
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_each_template_unowned_set_is_within_ledger(harness):
    """template 별 미등재 집합도 원장 안에 있다(합집합만 보면 template 별 유입을 놓친다)."""
    unowned = _unowned_shipped_wiki(_template_dir(harness))
    assert unowned <= set(UNOWNED_SHIPPED_WIKI_LEDGER), sorted(
        unowned - set(UNOWNED_SHIPPED_WIKI_LEDGER)
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_guard_is_red_when_a_new_unregistered_wiki_file_ships(harness):
    """sensitivity — 신규 미등재 출하 wiki 파일을 주입하면 원장 단언이 red."""
    injected = ".project_manager/wiki/newly_shipped_note.md"
    unowned = _unowned_shipped_wiki(_template_dir(harness), extra=(injected,))
    assert injected in unowned
    assert not unowned <= set(UNOWNED_SHIPPED_WIKI_LEDGER), (
        "신규 미등재 출하 wiki 파일이 원장 가드를 통과함(⑥ 무효)"
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_manifest_wiki_entries_are_actually_shipped(harness):
    """반대 방향 — manifest 가 선언한 wiki 경로는 그 template 이 실제로 출하한다."""
    template_dir = _template_dir(harness)
    shipped = _shipped_wiki_relpaths(template_dir)
    declared = {
        rel for rel in _manifest_entries(template_dir) if rel.startswith(WIKI_PREFIX)
    }
    assert declared, f"{template_dir}: manifest 의 wiki 선언이 0건(수집 공허)"
    missing = sorted(
        rel for rel in declared
        if not any(
            shipped_rel == rel or shipped_rel.startswith(rel.rstrip("/") + "/")
            for shipped_rel in shipped
        )
    )
    assert not missing, f"{template_dir}: manifest 등재됐으나 미출하 wiki 경로 {missing}"


# ── ① 폴백 정합 (원장 ↔ 폴백 범위) ────────────────────────────────────────────

def test_every_ledger_entry_is_inside_the_notation_fallback_scope():
    """원장 전부가 폴백 범위 안 — 등재 없이도 표기가 해소된다(①의 (a) 택일과 정합)."""
    outside = sorted(
        rel for rel in UNOWNED_SHIPPED_WIKI_LEDGER
        if not _PM_IMPORT._is_notation_fallback_scope(rel)
    )
    assert not outside, f"폴백 범위 밖 미등재 출하 wiki: {outside}"


def test_production_derives_the_same_unowned_set_as_the_ledger():
    """add-harness/`--into` 가 재렌더 대상으로 쓰는 **production 파생**이 원장과 일치한다.

    가드가 원장을 지키는데 production 이 다른 집합을 쓰면 가드가 공허해진다 — 두 축을 맞물린다."""
    contexts = {}
    for harness in HARNESSES:
        contexts.update(dict.fromkeys(_manifest_entries(_template_dir(harness)), ()))
    derived = set(_PM_IMPORT._unowned_shipped_wiki_relpaths(REPO, HARNESSES, contexts))
    assert derived == set(UNOWNED_SHIPPED_WIKI_LEDGER), (
        f"production 파생 ≠ 원장 — 초과: {sorted(derived - set(UNOWNED_SHIPPED_WIKI_LEDGER))} · "
        f"누락: {sorted(set(UNOWNED_SHIPPED_WIKI_LEDGER) - derived)}"
    )


def test_fallback_scope_excludes_engine_backbone_and_root_entry_docs():
    """폴백 범위는 인스턴스 wiki 뿐 — 엔진 backbone·루트 진입문서는 독자가 달라 제외한다.

    `tools/**` 는 하네스별 표면이 아니고(런타임 표기는 `_runtime_skill_entry` 소관),
    `AGENTS.md` 는 그 문서를 읽는 하네스 부분집합이 독자라 호출부가 멤버십을 명시 전달한다."""
    assert not _PM_IMPORT._is_notation_fallback_scope(
        ".project_manager/tools/pm_bootstrap.py")
    assert not _PM_IMPORT._is_notation_fallback_scope("AGENTS.md")
    assert not _PM_IMPORT._is_notation_fallback_scope("CLAUDE.md")
    assert not _PM_IMPORT._is_notation_fallback_scope(".claude/skills/pm-adr/SKILL.md")


# ── ① 폴백 렌더 동작 ─────────────────────────────────────────────────────────

def _seed_instance(tmp_path: Path, wiki_body: str) -> tuple[Path, set[Path], dict]:
    """manifest 소유자가 없는 wiki 문서 하나만 있는 최소 dest 트리."""
    dest = tmp_path / "inst"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    # 소유자 후보를 일부러 다른 경로로 둔다 — 대상 wiki 문서는 어느 엔트리에도 안 걸린다.
    manifest.write_text(
        ".project_manager/wiki/pm_role.md\n.claude/skills    @render\n", encoding="utf-8")
    target = dest / ".project_manager" / "wiki" / "raw" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_text(wiki_body, encoding="utf-8")
    return dest, {Path(".project_manager/wiki/raw/README.md")}, {}


def test_unowned_wiki_gets_installed_harness_context_and_is_loud(tmp_path, capsys):
    """manifest 미소유 wiki 가 설치 하네스 전체 표기로 렌더되고 그 사실이 loud 하게 남는다."""
    dest, copied, subs = _seed_instance(tmp_path, "박제는 `/spike-new` 스킬이 한다\n")
    changed = _PM_IMPORT.render_managed_files(
        dest, subs, copied, installed_notation_context=("codex", "opencode"))
    assert changed == 1
    body = (dest / ".project_manager/wiki/raw/README.md").read_text(encoding="utf-8")
    assert body == "박제는 `$spike-new`(codex) / `/spike-new`(opencode) 스킬이 한다\n"
    err = capsys.readouterr().err
    assert "폴백" in err and ".project_manager/wiki/raw/README.md" in err, err


def test_single_harness_fallback_uses_that_harness_notation(tmp_path):
    dest, copied, subs = _seed_instance(tmp_path, "박제는 `/spike-new` 스킬이 한다\n")
    _PM_IMPORT.render_managed_files(
        dest, subs, copied, installed_notation_context=("codex",))
    assert (dest / ".project_manager/wiki/raw/README.md").read_text(
        encoding="utf-8") == "박제는 `$spike-new` 스킬이 한다\n"


def test_without_fallback_context_the_defect_reproduces(tmp_path):
    """sensitivity — 폴백을 되돌리면(context 미전달) 옛 조용한 skip 이 그대로 재현된다."""
    source = "박제는 `/spike-new` 스킬이 한다\n"
    dest, copied, subs = _seed_instance(tmp_path, source)
    changed = _PM_IMPORT.render_managed_files(dest, subs, copied)
    assert changed == 0
    assert (dest / ".project_manager/wiki/raw/README.md").read_text(
        encoding="utf-8") == source


def test_fallback_does_not_touch_engine_backbone_copies(tmp_path):
    """엔진 backbone 사본은 폴백 대상이 아니다 — 런타임 안내 문자열이 하네스 표기로 변조되면 안 된다."""
    dest = tmp_path / "inst"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(".project_manager/wiki/pm_role.md\n", encoding="utf-8")
    tool = dest / ".project_manager" / "tools" / "pm_bootstrap.py"
    tool.parent.mkdir(parents=True)
    body = 'print("먼저 `/pm-bootstrap` 을 실행하라")\n'
    tool.write_text(body, encoding="utf-8")
    changed = _PM_IMPORT.render_managed_files(
        dest, {}, {Path(".project_manager/tools/pm_bootstrap.py")},
        installed_notation_context=("codex", "opencode"))
    assert changed == 0
    assert tool.read_text(encoding="utf-8") == body
