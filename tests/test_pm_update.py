"""pm_update.py upstream 해소 단위 테스트 (T-0053).

--from 생략 시 dest local.conf 의 `upstream=` 을 기본으로 쓰는 해소 순서(①명시 --from
②local.conf upstream= ③에러)·stale 가드·`_read_local_conf` 파싱을 검증한다. 실 복사 없이
plan/dry-run 레벨로 — fake_repo(REPO monkeypatch) + tmp source 만으로 외부 의존 0.

self-location(--target 생략) 모드는 effective_dest=REPO 이므로 pm_update.REPO 를 tmp 로
monkeypatch 해 실 REPO 를 건드리지 않고 local.conf upstream 해소를 검증한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

SENTINEL_REL = ".project_manager/tools/__pm_update_upstream_sentinel__.py"


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_update():
    return _load_pm_update()


def _make_upstream(root: Path, rel: str = SENTINEL_REL) -> None:
    """source(upstream) 트리 — sentinel 1개 + 그를 가리키는 engine.manifest.

    `rel` 로 sentinel 상대경로를 달리하면 두 source 를 plan 출력에서 식별할 수 있다
    (어느 source 의 manifest 가 실제로 쓰였는지 = 해소 우선순위 증명).
    """
    sentinel = root / rel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    manifest = root / ".project_manager" / "engine.manifest"
    manifest.write_text(rel + "\n", encoding="utf-8")


def _write_local_conf(dest_root: Path, text: str) -> Path:
    local_conf = dest_root / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True, exist_ok=True)
    local_conf.write_text(text, encoding="utf-8")
    return local_conf


# ── _read_local_conf 파싱 단위 (board.local_config 규칙 미러) ────────────────

def test_read_local_conf_parses_key_value(pm_update, tmp_path):
    conf = tmp_path / "local.conf"
    conf.write_text(
        "# 헤더 주석\n"
        "\n"
        "session=pm\n"
        "upstream=/home/u/checkout\n"
        "   # 들여쓴 주석\n"
        "bad line without equals\n"
        "  py = python3  \n",
        encoding="utf-8",
    )
    result = pm_update._read_local_conf(conf)
    assert result["session"] == "pm"
    assert result["upstream"] == "/home/u/checkout"
    assert result["py"] == "python3"  # 양쪽 공백 strip
    assert "bad line without equals" not in result
    # 주석/빈 줄은 키가 되지 않는다.
    assert "# 헤더 주석" not in result
    assert "" not in result


def test_read_local_conf_missing_returns_empty(pm_update, tmp_path):
    assert pm_update._read_local_conf(tmp_path / "nope.conf") == {}


def test_read_local_conf_last_value_wins(pm_update, tmp_path):
    conf = tmp_path / "local.conf"
    conf.write_text("upstream=/first\nupstream=/second\n", encoding="utf-8")
    assert pm_update._read_local_conf(conf)["upstream"] == "/second"


# ── ① 명시 --from 우선 (local.conf upstream 무시) ───────────────────────────

def test_explicit_from_takes_priority_over_local_conf(pm_update, tmp_path, monkeypatch, capsys):
    """--from 명시 시 local.conf 의 upstream= 보다 우선한다 — *명시 source 로* plan.

    두 source 의 sentinel 상대경로를 다르게 둬, plan 출력에 어느 쪽이 떴는지로 우선순위를 식별한다
    (둘 다 유효·동일 경로면 어느 게 쓰였는지 구분 못 함 — reviewer should-fix 강화).
    """
    explicit_rel = ".project_manager/tools/__pm_update_explicit_sentinel__.py"
    stored_rel = ".project_manager/tools/__pm_update_stored_sentinel__.py"
    fake_repo = tmp_path / "fake_repo"
    explicit = tmp_path / "explicit_upstream"
    stored = tmp_path / "stored_upstream"
    _make_upstream(explicit, rel=explicit_rel)
    _make_upstream(stored, rel=stored_rel)
    # local.conf 에는 stored 를 등록 — 명시 --from(explicit)이 이를 덮어야 한다.
    _write_local_conf(fake_repo, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--from", str(explicit), "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    # explicit 의 manifest/sentinel 이 plan 에 떠야 하고, stored 의 것은 *뜨지 않아야* 한다.
    assert explicit_rel in out, "명시 --from(explicit) source 가 plan 에 안 쓰였다"
    assert stored_rel not in out, "local.conf 의 stored upstream 이 명시 --from 을 덮었다(우선순위 역전)"


# ── ② --from 생략 → local.conf upstream 사용 (plan 도달) ─────────────────────

def test_omitted_from_uses_local_conf_upstream(pm_update, tmp_path, monkeypatch, capsys):
    """--from 생략 시 dest local.conf 의 upstream= 을 기본 source 로 써서 plan 에 도달한다."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"# conf\nupstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert SENTINEL_REL in out, "stored upstream 의 sentinel 이 plan 되지 않음 — 기본값 미사용."


# ── ③ 둘 다 없음 → rc!=0 + 명확 에러 (침묵 폴백 금지) ────────────────────────

def test_no_from_no_upstream_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--from 도 없고 local.conf upstream= 도 없으면 rc!=0 + 미등록 안내 에러."""
    fake_repo = tmp_path / "fake_repo"
    # upstream 없는 local.conf (다른 키만).
    _write_local_conf(fake_repo, "session=pm\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "upstream 미등록" in err
    assert "--from" in err  # --from 안내 포함


def test_no_local_conf_file_errors(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf 자체가 없으면(미import 클론) 미등록 에러 — 침묵 진행 금지."""
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / ".project_manager").mkdir(parents=True)

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    assert rc != 0
    assert "upstream 미등록" in capsys.readouterr().err


# ── ④ stale (upstream 경로 부재/비-디렉토리) → 명확 에러 (rc 2 와 구분) ──────

def test_stale_upstream_path_errors(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf upstream 이 부재 경로면 자동 진행 안 하고 명확한 stale 에러로 멈춘다."""
    fake_repo = tmp_path / "fake_repo"
    stale = tmp_path / "moved_away_checkout"  # 생성하지 않음 → 부재
    _write_local_conf(fake_repo, f"upstream={stale}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "디렉토리가 아니거나 존재하지 않음" in err
    # 기존 missing-manifest(rc 2) 메시지와 구분되는 upstream-stale 메시지여야 한다.
    assert "manifest" not in err.split("\n")[0]


def test_stale_explicit_from_errors(pm_update, tmp_path, monkeypatch, capsys):
    """명시 --from 이 부재 경로여도 동일 stale 에러(출처 표기는 --from)."""
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / ".project_manager").mkdir(parents=True)
    missing = tmp_path / "does_not_exist"

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--from", str(missing), "--dry-run"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "디렉토리가 아니거나 존재하지 않음" in err
    assert "--from" in err  # 출처 표기


def test_upstream_file_not_dir_errors(pm_update, tmp_path, monkeypatch, capsys):
    """upstream 이 *파일*(디렉토리 아님)이어도 stale 가드가 잡는다."""
    fake_repo = tmp_path / "fake_repo"
    a_file = tmp_path / "a_file"
    a_file.write_text("not a dir\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={a_file}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    assert rc != 0
    assert "디렉토리가 아니거나 존재하지 않음" in capsys.readouterr().err


# ── ⑤ --target 모드: --from 생략 시 *타깃* local.conf 의 upstream 사용 (self-loc 과 일관) ──

def test_target_mode_omitted_from_uses_target_local_conf(pm_update, tmp_path, monkeypatch, capsys):
    """--target <name> + --from 생략 → effective_dest=templates/<name>/ 의 local.conf upstream 사용.

    self-location(REPO)과 동일한 해소 코드 경로가 --target 의 effective_dest 에도 일관 적용됨을 강제
    (codex suggestion·ticket 검증 주의 경계). 타깃 local.conf 의 upstream 으로 plan 도달.
    """
    fake_repo = tmp_path / "fake_repo"
    # resolve_target_root 가 통과하도록 templates/<name>/ 를 디렉토리로 만든다.
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    stored = tmp_path / "target_stored_upstream"
    _make_upstream(stored)
    # 타깃 자신의 local.conf 에 upstream 등록 (self-loc 의 REPO local.conf 자리와 동형).
    _write_local_conf(target_dir, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert SENTINEL_REL in out, "--target 의 local.conf upstream 으로 plan 안 됨(해소 불일치)."


def test_target_mode_no_upstream_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--target + --from 생략 + 타깃 upstream 미등록 → self-loc 과 동일한 미등록 에러."""
    fake_repo = tmp_path / "fake_repo"
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    _write_local_conf(target_dir, "session=pm\n")  # upstream 없음

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    assert rc != 0
    assert "upstream 미등록" in capsys.readouterr().err


# ── --target = copy2 (render_enabled=False) 가드 (T-0133·should-fix) ──────────
# main() 의 `render_enabled = not args.target` 매핑을 회귀로 박는다. --target 동기는
# 템플릿(local.conf 없는 토큰-form 소스)을 렌더하면 operational leak/_assert_no_leak crash
# 나므로 copy2 여야 한다. plan-level 가드(plan(render_enabled=...))는 별 테스트가 박았으나,
# main() 의 매핑 자체는 회귀 그물 밖이었다(reviewer should-fix). @render 활성화 후 load-bearing.

def _spy_render_enabled(pm_update, monkeypatch, captured):
    """pm_update.plan 을 감싸 main() 이 전달한 render_enabled 키워드를 포착한다(실 plan 위임)."""
    real_plan = pm_update.plan

    def spy(*args, **kwargs):
        captured["render_enabled"] = kwargs.get("render_enabled")
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(pm_update, "plan", spy)


def test_main_target_passes_render_disabled(pm_update, tmp_path, monkeypatch):
    """main() --target → plan(render_enabled=False) — 템플릿 동기는 copy2(토큰-form 보존)."""
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "templates" / "oc").mkdir(parents=True)
    stored = tmp_path / "up_target"
    _make_upstream(stored)
    _write_local_conf(fake_repo / "templates" / "oc", f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    captured: dict = {}
    _spy_render_enabled(pm_update, monkeypatch, captured)

    assert pm_update.main(["--target", "oc", "--dry-run"]) == 0
    assert captured["render_enabled"] is False, "--target 인데 render 가 켜졌다(템플릿 토큰 렌더 위험)."


def test_main_self_location_passes_render_enabled(pm_update, tmp_path, monkeypatch):
    """main() --target 없음(채택자 self-update) → plan(render_enabled=True) — render 유지·불변."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "up_self"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    captured: dict = {}
    _spy_render_enabled(pm_update, monkeypatch, captured)

    assert pm_update.main(["--dry-run"]) == 0
    assert captured["render_enabled"] is True, "채택자 self-update 인데 render 가 꺼졌다(토큰 출하 위험)."


# ── v2 엔진 manifest 정합 (T-0088 — 신규 엔진 등재/개명 누락 가드) ────────────────
# domain.py 가 manifest 미등재라 templates 에 전파 안 되던 실 버그를 회귀로 박는다.
# 3 manifest(root + claude_code + opencode)가 v2 엔진을 일관되게 담는지 검증.

_MANIFESTS = [
    REPO / ".project_manager" / "engine.manifest",
    REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest",
    REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest",
]


@pytest.mark.parametrize("manifest_path", _MANIFESTS, ids=lambda p: p.parent.parent.name or "root")
def test_v2_engine_in_manifest(pm_update, manifest_path):
    """domain.py·pm_relay.py 가 등재되고, 개명 전 pm_orchestrator.py 잔재는 없다."""
    entries = pm_update.read_manifest(manifest_path)
    assert ".project_manager/tools/domain.py" in entries, "domain.py manifest 미등재 (전파 누락)"
    assert ".project_manager/tools/pm_relay.py" in entries, "pm_relay.py manifest 미등재"
    assert ".project_manager/tools/pm_orchestrator.py" not in entries, "옛 pm_orchestrator.py 잔재 (relay 개명 누락)"


# ── domain/_template.md 엔진 동기 채널 (T-0095 — 스캐폴드 파리티 가드) ──────────────
# domain/_template.md 가 다른 _template(tickets·spikes·state)과 동급 엔진 소유 스캐폴드인데
# manifest 누락이라 pm_update 동기에서 빠지던 것(T-0090 수기 전파 근본)을 회귀로 박는다.
# domain/ *페이지*는 인스턴스 소유(manifest 밖) — _template.md 만 엔진 소유임에 유의.

DOMAIN_TEMPLATE_REL = ".project_manager/wiki/domain/_template.md"


@pytest.mark.parametrize("manifest_path", _MANIFESTS, ids=lambda p: p.parent.parent.name or "root")
def test_domain_template_in_manifest(pm_update, manifest_path):
    """3 manifest(root + claude_code + opencode) 모두 domain/_template.md 를 엔진으로 등재."""
    entries = pm_update.read_manifest(manifest_path)
    assert DOMAIN_TEMPLATE_REL in entries, (
        f"{DOMAIN_TEMPLATE_REL} manifest 미등재 ({manifest_path}) — domain 스캐폴드 동기 누락"
    )
    # domain/ 페이지(인스턴스 소유)는 manifest 밖이어야 한다 — _template.md 만 엔진.
    assert ".project_manager/wiki/domain" not in entries, (
        "domain/ 디렉토리 통째 등재 — domain/ 페이지는 인스턴스 소유(manifest 밖)여야 한다"
    )


def test_domain_template_planned_as_managed(pm_update, tmp_path):
    """domain/_template.md 가 plan 의 동기 대상으로 잡힌다 — source 변형 시 update 로 떠야 한다.

    내용이 dest 와 동일하면 plan 은 changes 에 넣지 않으므로(no-op), source 측 내용을 일부러
    변형해 'manifest 가 이 경로를 실제로 동기 대상으로 본다'를 update change 발생으로 입증한다.
    실 트리를 건드리지 않도록 fake source/dest 를 tmp 에 구성한다.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_tpl = src / DOMAIN_TEMPLATE_REL
    dst_tpl = dst / DOMAIN_TEMPLATE_REL
    src_tpl.parent.mkdir(parents=True)
    dst_tpl.parent.mkdir(parents=True)
    src_tpl.write_text("# domain template — upstream 개선판\n", encoding="utf-8")
    dst_tpl.write_text("# domain template — 구버전\n", encoding="utf-8")

    # manifest 에 domain/_template.md 만 둔 최소 plan — 동기 대상 인식만 검증.
    changes, missing = pm_update.plan(src, [DOMAIN_TEMPLATE_REL], dest_root=dst)

    assert missing == [], "domain/_template.md 가 source 에서 missing 으로 잡힘"
    planned = {rel: kind for rel, _sp, _dst, kind in changes}
    assert planned.get(DOMAIN_TEMPLATE_REL) == "update", (
        "domain/_template.md 가 plan 의 동기 대상(update)으로 안 잡힘 — manifest 동기 채널 누락"
    )
