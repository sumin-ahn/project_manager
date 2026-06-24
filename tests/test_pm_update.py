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


# ── T-0145: upstream_rev baseline 기록 (매 sync·drift-lint 입력·ADR-0032 D2) ──

def test_record_upstream_rev_baseline_records_head(pm_update, tmp_path, monkeypatch):
    """source 가 git checkout 이면 매 sync 후 upstream_rev=<HEAD> 를 dest local.conf 에 기록.

    pm_update 는 git 을 직접 안 부르고 pm_import.read_upstream_rev(URL 안전 git 호출)를 재사용한다
    — 그 read 를 monkeypatch 해 라이브 git 없이 baseline 기록 *배선* 을 검증한다(매 sync).
    """
    dest = tmp_path / "dest"
    _write_local_conf(dest, "session=pm\nupstream=/some/checkout\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "headcommit99")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    changed = pm_update.record_upstream_rev_baseline(dest, source)
    assert changed is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream_rev"] == "headcommit99"
    assert conf["upstream"] == "/some/checkout"  # 별개 키 보존(한 키 2역 금지)
    assert conf["session"] == "pm"


def test_record_upstream_rev_baseline_skips_when_source_not_git(pm_update, tmp_path, monkeypatch):
    """source 가 git checkout 이 아니면(read_upstream_rev=None·URL upstream 포함) graceful 생략."""
    dest = tmp_path / "dest"
    _write_local_conf(dest, "upstream=https://h/x.git\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    changed = pm_update.record_upstream_rev_baseline(dest, source)
    assert changed is False
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf


def test_main_records_upstream_rev_on_successful_sync(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(apply) 후 upstream_rev baseline 이 기록된다(매 sync·dry-run 은 기록 안 함)."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    # baseline rev 읽기를 결정적으로 stub(라이브 git 0).
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "syncedrev42")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])  # 실 sync(dry-run 아님) — sentinel 1개 복사.
    assert rc == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "syncedrev42", \
        f"매 sync 후 upstream_rev baseline 미갱신: {conf.get('upstream_rev')!r}"


def test_main_dry_run_does_not_record_upstream_rev(pm_update, tmp_path, monkeypatch):
    """--dry-run 은 실 sync 가 아니므로 upstream_rev baseline 을 기록하지 않는다(파일 미변경)."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf, "dry-run 인데 upstream_rev 가 기록됨(부작용 누출)"


# ── MF1(codex): URL upstream + --from 생략 → 명확·actionable 에러 (D5 경계·침묵 실패 금지) ──

def test_url_upstream_omitted_from_errors_clearly(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf upstream= 이 URL 이고 --from 생략이면 디렉토리 resolve 안 하고 명확 에러로 멈춘다.

    엔진(pm_update)은 로컬 파일만 복사한다(git clone/fetch 안 함·ADR-0032 D5). URL upstream 을
    `Path(url).resolve()` 했다간 "디렉터리 없음" 류로 침묵 실패하므로, classify_upstream 으로
    URL 을 판별해 actionable 에러(pm-update 스킬·--from 명시 안내)로 멈춘다(MF1).
    """
    fake_repo = tmp_path / "fake_repo"
    _write_local_conf(fake_repo, "upstream=https://github.com/acme/proj.git\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main([])  # --from 생략 → local.conf URL upstream 해소 시도.
    assert rc == 1, "URL upstream 인데 rc 0/2 — 명확 에러로 안 멈춤"
    err = capsys.readouterr().err
    assert "URL" in err and ("pm-update" in err or "--from" in err), \
        f"actionable 에러 아님(스킬·--from 안내 없음): {err!r}"


def test_url_upstream_explicit_from_local_still_works(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf 가 URL upstream 이어도 --from <로컬 checkout> 명시면 정상 sync(URL 게이트 우회).

    MF1 게이트는 *--from 생략 + local.conf URL* 경로 한정 — 명시 --from(로컬)은 그대로 동작
    (URL 게이트는 stored upstream 해소 분기에만 있고 명시 --from 은 그 분기를 안 탄다).
    """
    fake_repo = tmp_path / "fake_repo"
    _write_local_conf(fake_repo, "upstream=https://github.com/acme/proj.git\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    local_src = tmp_path / "local_checkout"
    _make_upstream(local_src)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "localrev1")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--from", str(local_src)])
    assert rc == 0, f"명시 --from(로컬)인데 URL 게이트가 막음: {capsys.readouterr().err!r}"


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


# ── ⑥ --target 모드: source 부재 항목 graceful skip (T-0137·D17 · @target-owned) ──
# target-owned 어댑터(루트 엔진 upstream 엔 없고 타깃 자신만 보유·예: opencode `.opencode/*`)
# 가 manifest 에 있을 때, --target 동기가 rc2 로 전체를 막지 않고 skip + 안내 로그하는지.
# 판별자는 명시 마커 `@target-owned` 한정 — non-@target-owned(엔진경로·@render-only 엔진
# 리소스 포함) source-부재는 --target 모드여도 rc2 + 에러(silent skip 금지·엔진 누락 은폐 방지).

def test_target_mode_skips_target_owned_source_absent_with_log(pm_update, tmp_path, monkeypatch, capsys):
    """--target + manifest 의 @target-owned 항목이 root source 부재 → rc2 대신 skip + 안내 로그.

    copy 가능 항목(sentinel)은 정상 plan 되고, target-owned 부재 항목은 [skip] 로그로 surface
    된다(부분 skip 이 전체를 막지 않음). dry-run 레벨로 검증(실 복사 없음).
    """
    fake_repo = tmp_path / "fake_repo"
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    stored = tmp_path / "up_target"
    # source(upstream)에는 sentinel 1개만 두되, manifest 엔 target-owned 부재 경로도 등재.
    sentinel = stored / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    absent_rel = ".opencode/command/pm-only.md"  # root upstream 엔 없는 target-owned 어댑터
    manifest = stored / ".project_manager" / "engine.manifest"
    # @target-owned 태그 = 타깃 고유 어댑터 신호 → source-부재 시 graceful skip 대상.
    # 실 어댑터는 @render @target-owned 함께지만 skip 판별은 @target-owned 단독으로도 성립.
    manifest.write_text(
        SENTINEL_REL + "\n" + absent_rel + "  @render @target-owned\n", encoding="utf-8")
    _write_local_conf(target_dir, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0, "target-owned source 부재가 rc2 로 전체를 막았다(graceful skip 실패)."
    # 정상 항목은 plan 에 진행(부분 skip 이 전체를 막지 않음).
    assert SENTINEL_REL in captured.out, "copy 가능 항목이 plan 되지 않음."
    # 부재 항목은 침묵 skip 이 아니라 [skip] 안내 로그로 surface 되어야 한다.
    assert "[skip]" in captured.out and absent_rel in captured.out, \
        "target-owned 부재 경로가 안내 로그로 surface 되지 않음(침묵 skip 금지)."
    assert "target-owned" in captured.out, "skip 사유(target-owned: root source 부재) 미표기."
    # --target 모드의 부재 skip 은 에러가 아니므로 rc2 missing 에러 메시지가 없어야 한다.
    assert "source 에 없음" not in captured.err


def test_target_mode_non_target_owned_source_absent_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--target + manifest 의 **non-@target-owned** 항목이 root source 부재 → rc2 + 에러(skip 아님).

    엔진경로(`.project_manager/tools/*` 등)는 @target-owned 가 아니므로, source 부재면 진짜 누락
    (오타·잘못된 --from·전파돼야 하는데 빠진 도구)이다 — --target 모드여도 silent skip 금지·rc2.
    이게 핵심 회귀(엔진 빠짐을 못 보는 클래스 방지).
    """
    fake_repo = tmp_path / "fake_repo"
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    stored = tmp_path / "up_target"
    sentinel = stored / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    # non-@target-owned 엔진경로가 source 에 부재 — 전파돼야 하는데 빠진 도구(진짜 누락).
    engine_absent = ".project_manager/tools/foo.py"
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(SENTINEL_REL + "\n" + engine_absent + "\n", encoding="utf-8")
    _write_local_conf(target_dir, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "non-@target-owned 엔진경로 부재가 skip 됐다(silent skip — rc2 이어야 함)."
    assert engine_absent in captured.err, "엔진경로 누락이 에러로 surface 되지 않음."
    assert "[skip]" not in captured.out, "non-@target-owned 부재를 skip 으로 처리함(판별자 위반)."


def test_target_mode_render_only_source_absent_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--target + **@render-only**(target_owned 아님) 항목이 root source 부재 → rc2 + 에러.

    핵심 회귀(codex 발·over-broad-skip 가드): `.claude/agents @render` 처럼 루트 upstream 에
    *존재해야 하는* 엔진 리소스도 @render 다. 옛 구현(@render 판별)은 잘못된 --from 에서 이게
    빠져도 skip 으로 숨겼다. @target-owned 가 없으면 @render 라도 엔진 누락으로 보고 rc2 여야 한다.
    """
    fake_repo = tmp_path / "fake_repo"
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    stored = tmp_path / "up_target"
    sentinel = stored / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    # @render 이지만 @target-owned 가 아닌 엔진 리소스 — source 부재면 진짜 누락(은폐 금지).
    render_only_absent = ".claude/agents/some-engine-agent.md"
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(
        SENTINEL_REL + "\n" + render_only_absent + "  @render\n", encoding="utf-8")
    _write_local_conf(target_dir, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "@render-only(target_owned 아님) 엔진 리소스 부재가 skip 됐다(은폐 — rc2 이어야 함)."
    assert render_only_absent in captured.err, "@render-only 엔진 리소스 누락이 에러로 surface 되지 않음."
    assert "[skip]" not in captured.out, "@render-only 부재를 skip 으로 처리함(over-broad-skip 회귀)."


def test_target_mode_mixed_absent_engine_missing_wins(pm_update, tmp_path, monkeypatch, capsys):
    """--target + @target-owned 부재 + non-@target-owned 부재 동시 → non-@target-owned 때문에 rc2.

    부분 skip 이 엔진 누락을 가리면 안 된다 — @target-owned 어댑터는 skip 안내해도, non-
    @target-owned 엔진경로 부재가 하나라도 있으면 전체가 rc2 로 멈춘다(엔진 누락이 전체를 막아야 함).
    """
    fake_repo = tmp_path / "fake_repo"
    target_dir = fake_repo / "templates" / "oc"
    target_dir.mkdir(parents=True)
    stored = tmp_path / "up_target"
    (stored / ".project_manager").mkdir(parents=True)
    owned_absent = ".opencode/command/pm-only.md"       # target-owned 어댑터(부재)
    engine_absent = ".project_manager/tools/foo.py"     # 엔진경로 non-@target-owned(부재)
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(
        owned_absent + "  @target-owned\n" + engine_absent + "\n", encoding="utf-8")
    _write_local_conf(target_dir, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "oc", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "혼합 부재에서 non-@target-owned 엔진 누락이 rc2 로 막지 못함."
    # @target-owned 어댑터는 여전히 skip 안내(surface), 엔진경로는 에러로 surface.
    assert "[skip]" in captured.out and owned_absent in captured.out, \
        "@target-owned 어댑터 부재가 skip 안내로 surface 되지 않음."
    assert engine_absent in captured.err, "엔진경로 누락이 에러로 surface 되지 않음."


def test_self_location_source_absent_still_errors(pm_update, tmp_path, monkeypatch, capsys):
    """self-update 경로의 **non-@target-owned** source 부재는 rc2 에러 유지(양 모드 공통 안전판).

    self-update 에서도 @target-owned 부재는 graceful skip 하나(양 모드 공통), non-@target-owned
    엔진경로 부재는 진짜 잘못된 upstream 신호이므로 skip 대상이 아니다 — rc2 + 안내 에러 동작
    불변을 회귀로 박는다(엔진 누락이 self-update 에서 침묵 skip 되는 클래스 방지).
    """
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "up_self"
    # manifest 에 등재됐으나 source 에 없는 경로 1개 — self-loc 에선 진짜 에러.
    absent_rel = ".project_manager/tools/__absent__.py"
    (stored / ".project_manager").mkdir(parents=True)
    (stored / ".project_manager" / "engine.manifest").write_text(
        absent_rel + "\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "self-update 경로의 source 부재가 rc2 로 멈추지 않음(기존 동작 깨짐)."
    assert "source 에 없음" in captured.err
    assert "[skip]" not in captured.out, \
        "self-update non-@target-owned 부재를 skip 으로 처리함(판별자 위반)."


# ── ⑥b self-update 모드: @target-owned source 부재 graceful skip (T-0137·양 모드 공통) ──
# opencode 채택자(`pm_import --harness opencode`)의 manifest 엔 `.opencode/* @target-owned` 가
# 있으나 upstream=프레임워크 루트(.opencode/ 부재·root=claude)라 self-update 시 source-부재 →
# 과거 rc2(전체 update 실패). @target-owned 는 어느 모드든 판별자이므로 self-update 에서도
# skip(rc0)해야 한다(ship-blocker 수정). non-@target-owned 부재는 양 모드 공통 rc2 유지.

def test_self_location_skips_target_owned_source_absent_with_log(
        pm_update, tmp_path, monkeypatch, capsys):
    """self-update(--target 없음) + @target-owned 항목 source 부재 → rc2 대신 skip + 로그·rc0.

    opencode 채택자 self-update 의 실측 시나리오: manifest 의 `.opencode/* @target-owned` 가
    root upstream(claude)에 없어 과거 rc2 였던 것을 graceful skip 으로 surface 한다. 정상 항목
    (sentinel)은 plan 에 진행(부분 skip 이 전체를 막지 않음).
    """
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    stored = tmp_path / "up_self"
    # source(upstream)에는 sentinel 1개만 두되, manifest 엔 target-owned 부재 경로 등재.
    _make_upstream(stored)
    absent_rel = ".opencode/command/pm-only.md"  # 채택자 어댑터·root upstream(claude) 부재
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(
        SENTINEL_REL + "\n" + absent_rel + "  @render @target-owned\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0, \
        "self-update 의 @target-owned source 부재가 rc2 로 전체를 막았다(graceful skip 실패)."
    # 정상 항목은 plan 에 진행(부분 skip 이 전체를 막지 않음).
    assert SENTINEL_REL in captured.out, "copy 가능 항목이 plan 되지 않음."
    # 부재 항목은 침묵 skip 이 아니라 [skip] 안내 로그로 surface 되어야 한다.
    assert "[skip]" in captured.out and absent_rel in captured.out, \
        "self-update 의 @target-owned 부재 경로가 안내 로그로 surface 되지 않음(침묵 skip 금지)."
    assert "target-owned" in captured.out, "skip 사유(target-owned: upstream source 부재) 미표기."
    assert "source 에 없음" not in captured.err, "@target-owned skip 인데 missing 에러가 찍힘."


def test_self_location_render_only_source_absent_errors(
        pm_update, tmp_path, monkeypatch, capsys):
    """self-update + **@render-only**(target_owned 아님) source 부재 → rc2 + 에러.

    양 모드 공통 회귀(codex 발): self-update 에서도 @render 만 붙은 엔진 리소스(`.claude/* @render`)
    부재는 엔진 누락이지 target-owned skip 대상이 아니다 — rc2 로 멈춰 은폐를 막는다.
    """
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    stored = tmp_path / "up_self"
    _make_upstream(stored)
    render_only_absent = ".claude/agents/some-engine-agent.md"  # @render 엔진 리소스(부재)
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(
        SENTINEL_REL + "\n" + render_only_absent + "  @render\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "self-update 의 @render-only 엔진 리소스 부재가 skip 됐다(은폐 — rc2 이어야 함)."
    assert render_only_absent in captured.err, "@render-only 엔진 리소스 누락이 에러로 surface 되지 않음."
    assert "[skip]" not in captured.out, "@render-only 부재를 skip 으로 처리함(over-broad-skip 회귀)."


def test_self_location_mixed_absent_engine_missing_wins(
        pm_update, tmp_path, monkeypatch, capsys):
    """self-update + @target-owned 부재 + non-@target-owned 부재 동시 → non-@target-owned 때문에 rc2.

    self-update 에서도 @target-owned 어댑터는 skip 안내하되, non-@target-owned 엔진경로 부재가
    하나라도 있으면 전체가 rc2 로 멈춘다(엔진 누락이 부분 skip 에 가려지면 안 됨·양 모드 공통).
    """
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    stored = tmp_path / "up_self"
    (stored / ".project_manager").mkdir(parents=True)
    owned_absent = ".opencode/command/pm-only.md"       # target-owned 어댑터(부재)
    engine_absent = ".project_manager/tools/foo.py"     # 엔진경로 non-@target-owned(부재)
    manifest = stored / ".project_manager" / "engine.manifest"
    manifest.write_text(
        owned_absent + "  @target-owned\n" + engine_absent + "\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "self-update 혼합 부재에서 non-@target-owned 엔진 누락이 rc2 로 막지 못함."
    # @target-owned 어댑터는 여전히 skip 안내(surface), 엔진경로는 에러로 surface.
    assert "[skip]" in captured.out and owned_absent in captured.out, \
        "@target-owned 어댑터 부재가 skip 안내로 surface 되지 않음."
    assert engine_absent in captured.err, "엔진경로 누락이 에러로 surface 되지 않음."


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


# ── pm_import.py manifest 편입 (T-0140·ADR-0032 — PM 31 ⓒ stale 근본 해소) ──────────
# pm_import.py 가 manifest 미등재(root-only)라 pm_update 가 채택자/템플릿으로 전파 못 해
# 소리없이 stale 되던 것(PM 31 ⓒ)을 회귀로 박는다. 편입 후 pm_update(채택자 흡수)·
# pm_update --target(템플릿 refresh)가 전파한다. manifest 진화(새 항목이 *기존* 채택자에
# 도달)는 pm-update 스킬 reconcile(T-0142)·self-list 아님(codex round-2).

_PM_IMPORT_REL = ".project_manager/tools/pm_import.py"


@pytest.mark.parametrize("manifest_path", _MANIFESTS, ids=lambda p: p.parent.parent.name or "root")
def test_pm_import_in_manifest(pm_update, manifest_path):
    """pm_import.py 가 3 manifest(root + claude_code + opencode) 모두 등재 — 전파 채널 확보(de-list 가드)."""
    entries = pm_update.read_manifest(manifest_path)
    assert _PM_IMPORT_REL in entries, (
        f"{_PM_IMPORT_REL} manifest 미등재 ({manifest_path}) — pm_update 전파 누락(PM 31 ⓒ stale 재발)"
    )


def test_pm_import_byte_identical_root_templates():
    """pm_import.py 가 root↔양 템플릿 byte-identical (전파 무드리프트·`both` import 첫-트리 mismatch 회피).

    pm_import 의 `--harness both` 는 공유 엔진파일을 양 템플릿 트리에서 가져오므로, 두 트리의
    pm_import.py 가 다르면 import 가 mismatch 한다. root 단일 진실 → pm_update --target 전파로
    byte-identical 유지([[verify-engine-template-propagation]]·test_agents_root_templates_byte_identical 동형).
    """
    root_bytes = (REPO / _PM_IMPORT_REL).read_bytes()
    for harness in ("claude_code", "opencode"):
        tmpl = REPO / "templates" / harness / _PM_IMPORT_REL
        assert tmpl.exists(), (
            f"{harness} 템플릿에 pm_import.py 부재 — pm_update --target 전파 필요(T-0140)"
        )
        assert tmpl.read_bytes() == root_bytes, (
            f"{harness} pm_import.py root↔template 드리프트 — 엔진 변경 후 pm_update --target 전파 필요"
        )


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


# ── read_manifest 마커 파싱 (T-0137 — @render·@target-owned·복수·순서무관) ────────
# path 행 끝의 마커들을 복수·순서 무관으로 인식·전부 떼어내고 render/target_owned 플래그로
# 운반하는지 단위로 박는다. 미주석=둘 다 False(후방호환). board.py 의 @render 의존(render
# 파싱 불변·render-leak lint) 회귀도 같이 가드한다.

def _write_manifest(tmp_path: Path, lines: list[str]) -> Path:
    manifest = tmp_path / "engine.manifest"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def test_read_manifest_no_markers_both_false(pm_update, tmp_path):
    """미주석 path → render=False, target_owned=False (후방호환·전파 대상)."""
    manifest = _write_manifest(tmp_path, [".project_manager/tools/board.py"])
    entries = pm_update.read_manifest(manifest)
    assert len(entries) == 1
    e = entries[0]
    assert str(e) == ".project_manager/tools/board.py"
    assert e.render is False
    assert e.target_owned is False


def test_read_manifest_render_only(pm_update, tmp_path):
    """`path @render` → render=True, target_owned=False (엔진 리소스 렌더·skip 비대상)."""
    manifest = _write_manifest(tmp_path, [".claude/agents  @render"])
    e = pm_update.read_manifest(manifest)[0]
    assert str(e) == ".claude/agents"
    assert e.render is True
    assert e.target_owned is False


def test_read_manifest_target_owned_only(pm_update, tmp_path):
    """`path @target-owned` → render=False, target_owned=True (source-부재 skip 판별)."""
    manifest = _write_manifest(tmp_path, [".opencode/command/pm-only.md  @target-owned"])
    e = pm_update.read_manifest(manifest)[0]
    assert str(e) == ".opencode/command/pm-only.md"
    assert e.render is False
    assert e.target_owned is True


def test_read_manifest_both_markers(pm_update, tmp_path):
    """`path @render @target-owned` → 둘 다 True, 순수 경로만 값으로 남는다."""
    manifest = _write_manifest(tmp_path, [".opencode/agents  @render @target-owned"])
    e = pm_update.read_manifest(manifest)[0]
    assert str(e) == ".opencode/agents"
    assert e.render is True
    assert e.target_owned is True


def test_read_manifest_both_markers_order_independent(pm_update, tmp_path):
    """마커 순서 무관 — `@target-owned @render` 도 둘 다 True 로 파싱."""
    manifest = _write_manifest(tmp_path, [".opencode/agents  @target-owned @render"])
    e = pm_update.read_manifest(manifest)[0]
    assert str(e) == ".opencode/agents"
    assert e.render is True
    assert e.target_owned is True


def test_read_manifest_render_preserved_with_target_owned(pm_update, tmp_path):
    """board.py compat 회귀: @target-owned 가 붙은 행도 render 를 올바로 파싱(render-leak lint).

    board.py 가 read_manifest 의 `.render` 로 render-leak 검사 대상을 모은다 — @target-owned
    공존이 render 파싱을 깨면 안 된다(이름·의미 불변·target_owned 는 *추가* 속성).
    """
    manifest = _write_manifest(tmp_path, [
        ".claude/agents  @render",                         # render-only 엔진 리소스
        ".opencode/agents  @render @target-owned",         # 둘 다
        ".project_manager/tools/board.py",                 # 무마커
    ])
    entries = pm_update.read_manifest(manifest)
    render_paths = {str(e) for e in entries if e.render}
    assert render_paths == {".claude/agents", ".opencode/agents"}, \
        "@target-owned 공존이 render 파싱을 깼다(board.py render-leak lint 회귀)."
