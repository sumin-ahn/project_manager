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


_ADD_HARNESS_NS = {
    "opencode": (".opencode", "AGENTS.md", ()),
    "claude": (".claude", "CLAUDE.md", (".claude/agents/", ".claude/skills/")),
}


def _rel_in_namespace(rel: str, added_harness: str) -> bool:
    adapter_dir, root_doc, render_excl = _ADD_HARNESS_NS[added_harness]
    in_ns = rel == root_doc or rel.startswith(adapter_dir + "/")
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
