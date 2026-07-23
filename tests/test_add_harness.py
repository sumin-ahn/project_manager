"""add-harness 소스 해소 테스트 (T-0282 · ADR-0048 gap).

`pm_config add-harness <harness>` 가 **imported 인스턴스**(scoped-core 사본·`templates/` 부재·
② PM 홈 형상)에서 어댑터 소스를 못 찾던 갭을 닫는다. 소스 해소 우선순위(pm_import 단일 진실):

    explicit `--from`  >  dest local.conf upstream(path·templates 보유)  >  dest 자신(templates
    보유·framework-checkout 자기전환)  >  친화 FileNotFoundError

URL upstream 은 이번 스코프 밖(엔진은 로컬 파일만 복사·git clone/fetch 안 함) — path 만 자동
해소하고 URL 은 skip 해 `--from` 명시를 요구한다.

커버:
  - 소스 해소 precedence(_resolve_add_harness_source) — explicit·upstream(절대/상대)·URL skip·
    dest 자기전환·친화 에러·local.conf 부재.
  - add_harness end-to-end(source_root=None) — imported 인스턴스 자동 upstream 해소로 `.opencode/**`
    +`AGENTS.md` plan 산출 · explicit --from override.
  - pm_config `--from` 노출 + source_root forward(cmd_add_harness·dispatch·help surface).
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


@pytest.fixture(scope="module")
def pm_config():
    return _load("pm_config")


@pytest.fixture(autouse=True)
def _hermetic_opencode_models(pm_import, monkeypatch):
    """T-0033 동형 — apply 경로(resolve_opencode_model)가 라이브 `opencode models` CLI 를 치지
    않도록 (False, []) 고정(미설치 동치). 이 파일의 add_harness 는 전부 dry_run 이라 실제로 도달
    하지 않지만 hermetic 보험."""
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _write_local_conf(dest_root: Path, **keys: str) -> None:
    """dest_root/.project_manager/local.conf 를 주어진 키로 쓴다."""
    conf_dir = dest_root / ".project_manager"
    conf_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in keys.items()]
    (conf_dir / "local.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_fake_framework(root: Path, harness: str) -> Path:
    """root 에 templates/<dir>/ 만 갖춘 최소 프레임워크 checkout 을 만든다(helper 해소 프로브용)."""
    for name in {"claude": "claude_code", "opencode": "opencode"}[harness].split():
        (root / "templates" / name).mkdir(parents=True, exist_ok=True)
        (root / "templates" / name / ".keep").write_text("x", encoding="utf-8")
    return root


def _build_live_instance(pm_import, dest: Path, harness: str) -> Path:
    """`--new` 로 라이브 인스턴스 트리를 만든다(scoped-core·templates 부재)."""
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", "Live Inst"])
    assert rc == 0, f"라이브 인스턴스 셋업 실패(rc={rc}·harness={harness})."
    return dest


def _set_conf_upstream(dest_root: Path, value: str) -> None:
    """dest 의 local.conf upstream= 값을 결정적으로 덮는다(테스트 격리)."""
    conf = dest_root / ".project_manager" / "local.conf"
    text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    lines = [l for l in text.splitlines() if not l.strip().startswith("upstream=")]
    lines.append(f"upstream={value}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plan_relpaths(plan, dest: Path) -> list[str]:
    return sorted(a.dst.resolve().relative_to(dest.resolve()).as_posix() for a in plan)


# 어댑터 네임스페이스 참조 규칙(구현과 독립). ADR-0070 D5 ①: adapter dir 는 튜플(codex 는 이중
# `.codex`+`.agents`·나머지는 단일). render_excl = @render 엔진 리소스(claude 만 — codex/opencode 는 없음).
_ADD_HARNESS_NS = {
    "opencode": ((".opencode",), "AGENTS.md", ()),
    "claude": ((".claude",), "CLAUDE.md", (".claude/agents/", ".claude/skills/")),
    "codex": ((".codex", ".agents"), "AGENTS.md", ()),
}


def _rel_in_namespace(rel: str, added_harness: str) -> bool:
    adapter_dirs, root_doc, render_excl = _ADD_HARNESS_NS[added_harness]
    in_ns = rel == root_doc or any(rel.startswith(d + "/") for d in adapter_dirs)
    return in_ns and not any(rel.startswith(x) for x in render_excl)


# ── 소스 해소 precedence (_resolve_add_harness_source) ────────────────────────

def test_resolve_source_prefers_explicit(pm_import, tmp_path):
    """explicit `--from` 은 upstream 보다 우선하고 그대로 resolve 된다(기존 계약)."""
    dest = tmp_path / "inst"
    _make_fake_framework(dest, "opencode")            # dest 도 templates 보유(자기전환 후보)
    _write_local_conf(dest, upstream=str(_make_fake_framework(tmp_path / "up", "opencode")))
    explicit = _make_fake_framework(tmp_path / "explicit_fw", "opencode")
    got = pm_import._resolve_add_harness_source(dest, "opencode", explicit)
    assert got == explicit.resolve()


def test_resolve_source_from_upstream_absolute_path(pm_import, tmp_path):
    """dest 에 templates 부재(imported) → local.conf upstream(절대 path·templates 보유)에서 해소."""
    fw = _make_fake_framework(tmp_path / "fw", "opencode")
    dest = tmp_path / "inst"
    _write_local_conf(dest, upstream=str(fw))         # dest 자신엔 templates 없음
    got = pm_import._resolve_add_harness_source(dest, "opencode", None)
    assert got == fw.resolve()


def test_resolve_source_from_upstream_relative_path(pm_import, tmp_path):
    """상대 upstream 은 인스턴스 루트(dest) 기준으로 해소된다(cwd 무관)."""
    dest = tmp_path / "inst"
    _make_fake_framework(dest / "fw", "opencode")     # dest/fw 에 templates
    _write_local_conf(dest, upstream="fw")            # 상대 경로
    got = pm_import._resolve_add_harness_source(dest, "opencode", None)
    assert got == (dest / "fw").resolve()


def test_resolve_source_skips_url_upstream(pm_import, tmp_path):
    """URL upstream 은 자동 해소 대상 아님(엔진은 clone/fetch 안 함) — dest-self 도 없으면 친화 에러."""
    dest = tmp_path / "inst"
    _write_local_conf(dest, upstream="https://example.com/framework.git")
    with pytest.raises(FileNotFoundError) as exc:
        pm_import._resolve_add_harness_source(dest, "opencode", None)
    assert "--from" in str(exc.value)


def test_resolve_source_dest_self_when_has_templates(pm_import, tmp_path):
    """framework-checkout 자기전환: upstream 미해소여도 dest 에 templates 있으면 dest(회귀 보존)."""
    dest = _make_fake_framework(tmp_path / "fwdest", "opencode")
    _write_local_conf(dest, upstream="")              # 쓸 만한 upstream 없음
    got = pm_import._resolve_add_harness_source(dest, "opencode", None)
    assert got == dest.resolve()


def test_resolve_source_friendly_error_when_unresolvable(pm_import, tmp_path):
    """dest templates 부재 + upstream 부재 → actionable FileNotFoundError."""
    dest = tmp_path / "inst"
    _write_local_conf(dest, project_name="X")         # upstream 키 없음·templates 없음
    with pytest.raises(FileNotFoundError) as exc:
        pm_import._resolve_add_harness_source(dest, "opencode", None)
    msg = str(exc.value)
    assert "--from" in msg and "upstream" in msg


def test_resolve_source_no_local_conf_falls_through(pm_import, tmp_path):
    """local.conf 부재 + dest templates 부재 → 친화 에러(침묵 폴백 없음)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    with pytest.raises(FileNotFoundError):
        pm_import._resolve_add_harness_source(dest, "opencode", None)


def test_resolve_source_upstream_without_templates_falls_to_error(pm_import, tmp_path):
    """upstream 이 path 이나 templates/<harness> 부재면 skip → 말단 에러(오해소 방지)."""
    bare = tmp_path / "bare"                            # templates 없는 디렉토리
    bare.mkdir()
    dest = tmp_path / "inst"
    _write_local_conf(dest, upstream=str(bare))
    with pytest.raises(FileNotFoundError):
        pm_import._resolve_add_harness_source(dest, "opencode", None)


def test_resolve_source_harness_specific_templates(pm_import, tmp_path):
    """해소 프로브는 harness 별 어댑터 디렉토리를 본다 — opencode 만 있는 upstream 은 claude 미해소."""
    fw = _make_fake_framework(tmp_path / "fw", "opencode")   # templates/opencode 만
    dest = tmp_path / "inst"
    _write_local_conf(dest, upstream=str(fw))
    # opencode 는 해소.
    assert pm_import._resolve_add_harness_source(dest, "opencode", None) == fw.resolve()
    # claude(templates/claude_code 부재)는 upstream 스킵 → dest-self 도 없어 에러.
    with pytest.raises(FileNotFoundError):
        pm_import._resolve_add_harness_source(dest, "claude", None)


# ── add_harness end-to-end (source_root=None → upstream 자동 해소) ────────────

def test_add_harness_auto_resolves_upstream_end_to_end(pm_import, tmp_path):
    """imported 인스턴스(templates 부재)에 add-harness → upstream(REPO)에서 opencode 어댑터 plan.

    ② 케이스의 하네스-독립 재현: dest 는 claude 로 만든 scoped-core(templates 없음)·local.conf
    upstream=REPO(templates 보유). source_root 미지정으로 add_harness 를 걸면 더 이상 templates
    부재 에러가 아니라 upstream 에서 `.opencode/**`+`AGENTS.md` plan 을 산출한다.
    """
    dest = _build_live_instance(pm_import, tmp_path / "imported", "claude")
    assert not (dest / "templates").exists(), "imported 인스턴스는 scoped-core(templates 부재)여야."
    _set_conf_upstream(dest, str(REPO))
    plan = pm_import.add_harness(dest, "opencode", dry_run=True)   # source_root=None
    rels = _plan_relpaths(plan, dest)
    assert rels, "plan 이 비어 있다 — upstream 에서 opencode 어댑터가 해소돼야 한다."
    assert "AGENTS.md" in rels
    assert ".opencode/agents/pm.md" in rels
    assert all(_rel_in_namespace(r, "opencode") for r in rels), rels
    # dry-run = 파일시스템 미변경.
    assert not (dest / ".opencode").exists()


def test_add_harness_explicit_from_overrides_upstream(pm_import, tmp_path):
    """explicit source_root(--from)은 upstream 자동 해소를 override 한다(기존 계약 보존)."""
    dest = _build_live_instance(pm_import, tmp_path / "imported2", "claude")
    _set_conf_upstream(dest, "https://example.com/bogus.git")     # URL — 자동 해소면 실패할 값
    # explicit REPO 를 주면 upstream 무관하게 그대로 소스로 쓴다.
    plan = pm_import.add_harness(dest, "opencode", dry_run=True, source_root=REPO)
    rels = _plan_relpaths(plan, dest)
    assert "AGENTS.md" in rels and ".opencode/agents/pm.md" in rels


def test_add_harness_imported_instance_errors_without_resolvable_source(pm_import, tmp_path):
    """imported 인스턴스인데 upstream 이 URL·--from 미지정이면 친화 FileNotFoundError(→rc 1)."""
    dest = _build_live_instance(pm_import, tmp_path / "imported3", "claude")
    _set_conf_upstream(dest, "https://example.com/bogus.git")
    with pytest.raises(FileNotFoundError) as exc:
        pm_import.add_harness(dest, "opencode", dry_run=True)      # source_root=None
    assert "--from" in str(exc.value)


# ── codex add-harness (세 번째 하네스·ADR-0070 D5·T-0403) ─────────────────────
# codex 어댑터 네임스페이스 = `.codex/**`(agents·config·hooks·relay) + `.agents/**`(skills remap) +
# `AGENTS.md`(공통 코어). 라이브 호스트(claude 또는 opencode)에 비파괴 추가. AGENTS.md 는 opencode 와
# byte-identical(공통 코어) → opencode 호스트에선 git-safe skip 으로 무충돌(D3 C-v2 부수 이득).

import shutil as _shutil_for_git       # noqa: E402 — 실 git 가용 게이트(explicit commit 케이스).
import subprocess as _sp_for_git       # noqa: E402

requires_git = pytest.mark.skipif(
    _shutil_for_git.which("git") is None,
    reason="git 바이너리 부재 — 실 git commit 통합 케이스 skip(dry-run 케이스는 항상 실행).",
)


def _git_commit_all(dest: Path) -> None:
    """dest git repo 의 현재 트리를 전부 커밋 — 이후 파일이 '추적&미변경'(git-safe)이 되게 한다."""
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    _sp_for_git.run(["git", "-C", str(dest), "add", "-A"], check=True, capture_output=True)
    _sp_for_git.run(["git", "-C", str(dest), *ident, "commit", "-m", "seed"],
                    check=True, capture_output=True)


def test_add_harness_codex_onto_claude_host_lays_down_adapter(pm_import, tmp_path):
    """claude 호스트에 codex add(dry-run): codex 어댑터(.codex/**·.agents/skills·AGENTS.md)만 산출·
    엔진/wiki/claude 어댑터 불가침·전부 codex 네임스페이스 안(dual-namespace 스코프 정확성)."""
    dest = _build_live_instance(pm_import, tmp_path / "cl_host", "claude")
    _set_conf_upstream(dest, str(REPO))
    plan = pm_import.add_harness(dest, "codex", dry_run=True)      # source_root=None
    rels = _plan_relpaths(plan, dest)
    assert rels, "plan 이 비어 있다 — codex 어댑터가 해소돼야 한다."
    assert "AGENTS.md" in rels                                     # claude 호스트엔 없던 공통 코어 신규
    assert any(r.startswith(".codex/agents/") for r in rels), rels
    assert any(r.startswith(".agents/skills/") for r in rels), rels
    # 전부 codex 네임스페이스 안(불변식).
    assert all(_rel_in_namespace(r, "codex") for r in rels), rels
    # 네임스페이스 밖(엔진·wiki·claude 어댑터·파사드)은 0개.
    assert not [r for r in rels if r.startswith(".project_manager/")], rels
    assert not [r for r in rels if r.startswith(".claude/")], rels
    assert "CLAUDE.md" not in rels
    # dry-run = 파일시스템 미변경.
    assert not (dest / ".codex").exists()
    assert not (dest / ".agents").exists()


def test_add_harness_codex_namespace_invariant_zero_outside(pm_import, tmp_path):
    """불변식(ADR-0048 Decision 5·codex): codex add plan 이 codex 네임스페이스 밖 relpath 0개."""
    dest = _build_live_instance(pm_import, tmp_path / "cl_host_inv", "claude")
    _set_conf_upstream(dest, str(REPO))
    plan = pm_import.add_harness(dest, "codex", dry_run=True)
    rels = _plan_relpaths(plan, dest)
    outside = [r for r in rels if not _rel_in_namespace(r, "codex")]
    assert outside == [], f"codex add plan 에 네임스페이스 밖 relpath 포함(불변식 위반): {outside}"


@requires_git
def test_add_harness_codex_onto_opencode_host_agents_md_git_safe_skip(pm_import, tmp_path):
    """opencode 호스트에 codex add: AGENTS.md 는 공통 코어라 git 추적&미변경이면 git-safe skip
    (백업 없이 덮기) — 파괴적 백업 churn 없이 무충돌(D3 C-v2 부수 이득). codex 어댑터는 신규 안착."""
    dest = _build_live_instance(pm_import, tmp_path / "oc_host", "opencode")
    agents_before = (dest / "AGENTS.md").read_bytes()
    _git_commit_all(dest)                                         # AGENTS.md 를 추적&미변경으로
    _set_conf_upstream(dest, str(REPO))                          # (local.conf 만 dirty·AGENTS.md 는 clean)
    plan = pm_import.add_harness(dest, "codex", dry_run=True)     # source_root=None
    agents = next((a for a in plan if a.dst == dest / "AGENTS.md"), None)
    assert agents is None, "추적된 instance-owned AGENTS.md가 plan에 들어가면 안 된다."
    assert (dest / "AGENTS.md").read_bytes() == agents_before
    # codex 고유 어댑터는 opencode 호스트엔 없어 신규 안착.
    rels = _plan_relpaths(plan, dest)
    assert any(r.startswith(".codex/agents/") for r in rels), rels
    assert any(r.startswith(".agents/skills/") for r in rels), rels


def test_add_harness_codex_apply_prints_trust_guidance(pm_import, tmp_path, capsys):
    """codex add-harness 실적용 완료 출력에 loud 2단계 trust 안내(D5) + 실제 laydown sanity."""
    dest = _build_live_instance(pm_import, tmp_path / "cl_host_apply", "claude")
    _set_conf_upstream(dest, str(REPO))
    pm_import.add_harness(dest, "codex", dry_run=False)           # 실적용
    out = capsys.readouterr().out
    assert "2단계 trust 승인" in out              # loud 헤더(copy 로그 `.../hooks/` 경로와 충돌 회피)
    assert "hook trust" in out
    assert "trust_level" in out
    # 실제로 laydown 됐는지 sanity.
    assert (dest / "AGENTS.md").is_file()
    assert (dest / ".codex" / "agents").is_dir()
    assert (dest / ".agents" / "skills").is_dir()


# ── add-harness 백업 → .gitignore 위생 (T-0411 · main import :3289 대칭) ────────
# add_harness 가 중앙 백업(`.pm_import_backups/`)을 만들면 main import 와 **대칭**으로
# ensure_backup_dir_gitignored 를 태워 채택자 git status 오염을 막는다. 백업은
# instance-owned AGENTS.md가 아니라 model override 없는 engine-managed agent 충돌로 유도한다.
# `.gitignore`를 커밋(git-safe)해 두면 helper가 패턴을 비파괴 append 한다.


def _git_commit_paths(dest: Path, *relpaths: str) -> None:
    """dest git repo 에서 *지정 경로만* 커밋 — 그 파일만 '추적&미변경'(git-safe)이 되고 나머지는
    미커밋 fresh(untracked)로 남는다(.gitignore=git-safe·researcher agent=fresh → 백업 발생 유도)."""
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    _sp_for_git.run(["git", "-C", str(dest), "add", *relpaths], check=True, capture_output=True)
    _sp_for_git.run(["git", "-C", str(dest), *ident, "commit", "-m", "seed subset"],
                    check=True, capture_output=True)


def _git_ignores(dest: Path, relpath: str) -> bool:
    """`git check-ignore <relpath>` rc==0 이면 무시됨(True)."""
    r = _sp_for_git.run(["git", "-C", str(dest), "check-ignore", relpath],
                        capture_output=True, text=True)
    return r.returncode == 0


@requires_git
def test_add_harness_backup_gitignores_backup_dir(pm_import, tmp_path):
    """① 백업 발생(opencode 호스트 + codex add·researcher agent 충돌) → `.pm_import_backups/`
    가 .gitignore 로 무시된다(main import :3289 대칭·채택자 git status 오염 폐쇄)."""
    dest = _build_live_instance(pm_import, tmp_path / "oc_gi", "opencode")
    (dest / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
    (dest / ".codex" / "agents" / "researcher.toml").write_text("LOCAL ENGINE-MANAGED EDIT\n", encoding="utf-8")
    _git_commit_paths(dest, ".gitignore")            # .gitignore=git-safe·researcher=fresh
    _set_conf_upstream(dest, str(REPO))
    today = datetime.date.today().isoformat()
    pm_import.add_harness(dest, "codex", dry_run=False)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    # AGENTS.md는 instance-owned라 백업하지 않고, engine-managed researcher 충돌이 백업을 유도한다.
    assert (backup_root / ".codex" / "agents" / "researcher.toml").is_file()
    # 결과: 패턴 append + git 이 실제로 무시(파일 단언 + git check-ignore 이중).
    assert f"{pm_import.BACKUP_DIR_NAME}/" in (dest / ".gitignore").read_text(encoding="utf-8")
    assert _git_ignores(dest, f"{pm_import.BACKUP_DIR_NAME}/{today}/.codex/agents/researcher.toml"), \
        "백업 디렉토리가 git 에 무시되지 않음 — add-harness 위생 미배선."


@requires_git
def test_add_harness_no_backup_leaves_gitignore_untouched(pm_import, tmp_path):
    """② 백업 미발생(전부 커밋 → instance-owned AGENTS.md skip) → .gitignore 무변."""
    dest = _build_live_instance(pm_import, tmp_path / "oc_nobk", "opencode")
    _git_commit_all(dest)                            # AGENTS.md·.gitignore 전부 git-safe
    _set_conf_upstream(dest, str(REPO))              # (local.conf 만 dirty·AGENTS.md 는 clean)
    before = (dest / ".gitignore").read_text(encoding="utf-8")
    assert f"{pm_import.BACKUP_DIR_NAME}/" not in before   # sanity: 원래 패턴 없음
    today = datetime.date.today().isoformat()
    pm_import.add_harness(dest, "codex", dry_run=False)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    assert not backup_root.exists(), "백업이 없어야 하는데 중앙 백업 디렉토리가 생성됨(전제 붕괴)."
    assert (dest / ".gitignore").read_text(encoding="utf-8") == before, \
        "무백업 add 가 .gitignore 를 편집함(최소 변경 위반)."


@requires_git
def test_add_harness_backup_gitignore_preserves_custom_appends_once(pm_import, tmp_path):
    """③ 기존 .gitignore 커스텀 규칙 보존 + 패턴 1회 append(비파괴·append-only — helper 성질 상속)."""
    dest = _build_live_instance(pm_import, tmp_path / "oc_custom", "opencode")
    (dest / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
    (dest / ".codex" / "agents" / "researcher.toml").write_text("LOCAL ENGINE-MANAGED EDIT\n", encoding="utf-8")
    (dest / ".gitignore").write_text("my-secret-artifacts/\n", encoding="utf-8")   # 사용자 커스텀만
    _git_commit_paths(dest, ".gitignore")            # 커스텀 .gitignore=git-safe·AGENTS.md=fresh
    _set_conf_upstream(dest, str(REPO))
    pm_import.add_harness(dest, "codex", dry_run=False)
    text = (dest / ".gitignore").read_text(encoding="utf-8")
    assert "my-secret-artifacts/" in text, "사용자 커스텀 규칙이 손실됨(비파괴 위반)."
    assert text.count(f"{pm_import.BACKUP_DIR_NAME}/") == 1, \
        "패턴이 정확히 1회 append 되지 않음(0=미배선·>1=중복 — append-only 위반)."
    assert text.endswith(f"{pm_import.BACKUP_DIR_NAME}/\n"), "패턴이 말미에 append 되지 않음."


@requires_git
def test_add_harness_backup_gitignore_idempotent_when_present(pm_import, tmp_path):
    """③(멱등) 이미 `.pm_import_backups/` 를 무시 중이면 백업 발생해도 중복 append 없음(present skip)."""
    dest = _build_live_instance(pm_import, tmp_path / "oc_idem", "opencode")
    (dest / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
    (dest / ".codex" / "agents" / "researcher.toml").write_text(
        "LOCAL ENGINE-MANAGED EDIT\n", encoding="utf-8")
    (dest / ".gitignore").write_text(
        f"keep/\n{pm_import.BACKUP_DIR_NAME}/\n", encoding="utf-8")   # 이미 무시 중
    _git_commit_paths(dest, ".gitignore")
    _set_conf_upstream(dest, str(REPO))
    pm_import.add_harness(dest, "codex", dry_run=False)
    text = (dest / ".gitignore").read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    assert (dest / pm_import.BACKUP_DIR_NAME / today / ".codex" / "agents" / "researcher.toml").is_file()
    assert text.count(f"{pm_import.BACKUP_DIR_NAME}/") == 1, "이미 존재하는 패턴이 중복됨(멱등 위반)."
    assert "keep/" in text, "기존 규칙이 손실됨(비파괴 위반)."


def test_add_harness_backup_on_non_git_adopter_creates_no_gitignore(pm_import, tmp_path):
    """비-git 채택자(git_safe None) + 백업 발생 → `.gitignore` **무생성**(무의미 아티팩트 오염 방지).

    `git_safe is not None` short-circuit 이 load-bearing: 없으면 백업 발생 시 helper 가 호출되고,
    add_harness 스코프는 `.gitignore` 를 복사 안 함(import_owned 항상 False)이라 helper 의 else-분기가
    비-git 디렉토리에 `.gitignore` 를 *신규 생성*("created") → git 도 아닌 채택자에 잉여 파일 오염.
    @requires_git 불요 — git 바이너리 유무와 무관하게 git_safe 는 None(비-git tmp·fail-soft)이라
    이 비-git 코드경로가 항상 탄다(plain 디렉토리 시나리오)."""
    dest = tmp_path / "nongit_inst"
    dest.mkdir()
    _write_local_conf(dest, project_name="NonGit", upstream=str(REPO))  # 소스 해소용
    (dest / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
    (dest / ".codex" / "agents" / "researcher.toml").write_text("existing agent\n", encoding="utf-8")
    today = datetime.date.today().isoformat()
    pm_import.add_harness(dest, "codex", dry_run=False)             # source_root=None → upstream 해소
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    # 전제: 비-git 이라 engine-managed 충돌이 중앙 백업됨(git_safe None → 전부 백업).
    assert (backup_root / ".codex" / "agents" / "researcher.toml").is_file()
    # 결과: git 아닌 채택자엔 .gitignore 를 만들지 않는다(guard 미동작 시 helper 가 'created' 로 오염).
    assert not (dest / ".gitignore").exists(), \
        "비-git 채택자에 .gitignore 가 생성됨 — `git_safe is not None` 가드 미동작(오염)."


# ── pm_config `--from` 노출 + source_root forward ─────────────────────────────

class _FakePmImport:
    """pm_import 대역 — add_harness_cli 호출 인자(특히 source_root)를 기록한다."""

    def __init__(self, rc: int = 0):
        self.calls: list[dict] = []
        self._rc = rc

    def add_harness_cli(self, dest_root, harness, *, dry_run, source_root=None):
        self.calls.append(
            {"dest_root": dest_root, "harness": harness,
             "dry_run": dry_run, "source_root": source_root})
        return self._rc


def test_cmd_add_harness_forwards_source_root(pm_config):
    """`--from` 값(args.source)이 add_harness_cli 로 source_root 로 forward 된다."""
    fake = _FakePmImport()
    args = argparse.Namespace(harness="opencode", dry_run=True, source=str(REPO))
    rc = pm_config.cmd_add_harness(args, pm_import=fake, dest_root=Path("/live/inst"))
    assert rc == 0
    assert fake.calls[0]["source_root"] == str(REPO)


def test_cmd_add_harness_source_defaults_none(pm_config):
    """source 미보유 Namespace(구 호출/테스트)도 getattr 폴백으로 source_root=None(하위호환)."""
    fake = _FakePmImport()
    args = argparse.Namespace(harness="opencode", dry_run=False)   # source 없음
    rc = pm_config.cmd_add_harness(args, pm_import=fake, dest_root=Path("/x"))
    assert rc == 0
    assert fake.calls[0]["source_root"] is None


def test_add_harness_dispatch_forwards_from_flag(pm_config, monkeypatch):
    """`main(["add-harness","opencode","--from",<src>,"--dry-run"])` 가 source_root 로 forward."""
    fake = _FakePmImport()
    monkeypatch.setattr(
        pm_config, "_load_module",
        lambda name, filename: fake if name == "pm_import" else None)
    rc = pm_config.main(["add-harness", "opencode", "--from", "/src/fw", "--dry-run"])
    assert rc == 0
    assert fake.calls[0]["source_root"] == "/src/fw"
    assert fake.calls[0]["dry_run"] is True


def test_add_harness_from_surfaced_in_help(pm_config, capsys):
    """`add-harness --help` 가 `--from` 을 노출한다(발견성)."""
    with pytest.raises(SystemExit) as exc:
        pm_config.main(["add-harness", "--help"])
    assert exc.value.code == 0
    assert "--from" in capsys.readouterr().out
