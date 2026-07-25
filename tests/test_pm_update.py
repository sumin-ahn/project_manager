"""pm_update.py upstream 해소 단위 테스트 (T-0053).

--from 생략 시 dest local.conf 의 `upstream=` 을 기본으로 쓰는 해소 순서(①명시 --from
②local.conf upstream= ③에러)·stale 가드·`_read_local_conf` 파싱을 검증한다. 실 복사 없이
plan/dry-run 레벨로 — fake_repo(REPO monkeypatch) + tmp source 만으로 외부 의존 0.

self-location(--target 생략) 모드는 effective_dest=REPO 이므로 pm_update.REPO 를 tmp 로
monkeypatch 해 실 REPO 를 건드리지 않고 local.conf upstream 해소를 검증한다.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
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


def _write_dest_manifest(dest_root: Path, entries: list[str]) -> Path:
    """dest(로컬) engine.manifest — resolve_manifest_for_dest 가 dest 우선으로 집게 만든다."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return manifest


def _make_upstream_manifest(root: Path, entries: list[str]) -> None:
    """source(upstream) 트리 — 등재 경로별 sentinel 파일 + 그를 담은 engine.manifest.

    _make_upstream 의 다-경로 버전 — manifest 에 여러 등재분을 넣어 skew(로컬 manifest 가
    이 중 일부를 누락)를 시뮬레이션한다.
    """
    for rel in entries:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# upstream sentinel {rel}\n", encoding="utf-8")
    manifest = root / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")


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


# ── T-0413: 경로 upstream 은 seen(관찰값)도 baseline 과 동시 기록 (거짓 drift 근절) ──
# 경로 형상은 fetch 채널이 따로 없어 *동기 시점 checkout rev 가 곧 관찰값*이다. baseline 만
# 갱신하면 두 키가 영구히 어긋나 정상 흡수 직후에도 adapter-drift 가 상시 뜬다(② 실측).
# URL 형상은 스킬층이 fetch 후 seen 을 쓰므로 엔진이 건드리지 않는다(한 키 2역 금지·ADR-0032 D2).

def test_record_upstream_rev_baseline_records_seen_for_path_upstream(
        pm_update, tmp_path, monkeypatch):
    """경로 upstream — baseline(upstream_rev)과 관찰값(upstream_seen_rev)이 같은 rev 로 동시 기록."""
    dest = tmp_path / "dest"
    _write_local_conf(dest, "session=pm\nupstream=/some/checkout\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "pathrev1234")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream_rev"] == "pathrev1234"
    assert conf["upstream_seen_rev"] == "pathrev1234", \
        f"경로 upstream 인데 seen 미기록(두 키 어긋남 잔존): {conf!r}"
    assert conf["upstream"] == "/some/checkout" and conf["session"] == "pm"  # 타 키 보존


def test_record_upstream_rev_baseline_replaces_stale_seen_in_place(
        pm_update, tmp_path, monkeypatch):
    """이미 어긋난 채 남은 conf(baseline 신·seen 구)는 다음 sync 1회로 수렴한다(별도 backfill 불요).

    ② adopter#0 실측 형상(upstream_rev=ddf6f484…·upstream_seen_rev=0ccc0251…=그 조상)의 재현 —
    set-or-replace 라 그 줄만 제자리 교체되고 주석·타 키는 보존된다.
    """
    dest = tmp_path / "dest"
    _write_local_conf(
        dest,
        "# per-clone\n"
        "upstream=/w/project_manager_1\n"
        "upstream_rev=ddf6f4842653\n"
        "upstream_seen_rev=0ccc02513a7f\n"
        "py=python3\n",
    )
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "nextsyncrev9")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    text = (dest / ".project_manager" / "local.conf").read_text(encoding="utf-8")
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream_rev"] == conf["upstream_seen_rev"] == "nextsyncrev9", \
        f"다음 sync 1회로 수렴 안 됨: {conf!r}"
    assert "# per-clone" in text and conf["py"] == "python3"  # 주석·타 키 보존
    assert text.count("upstream_seen_rev=") == 1, f"seen 키 중복 append: {text!r}"


def test_record_upstream_rev_baseline_leaves_seen_for_url_upstream(
        pm_update, tmp_path, monkeypatch):
    """URL upstream(스킬이 cache clone 후 --from) — baseline 만 갱신, seen 은 스킬층 값 그대로.

    URL 은 fetch 관찰과 sync 가 분리된 채널이라 엔진이 seen 을 쓰면 race/자기비교가 된다
    (ADR-0032 D2·codex round-3 NEW-2). 엔진은 건드리지 않는다.
    """
    dest = tmp_path / "dest"
    _write_local_conf(
        dest,
        "upstream=https://github.com/example/project_manager.git\n"
        "upstream_seen_rev=skillfetchrev\n",
    )
    source = tmp_path / "cache"  # 스킬이 clone 한 로컬 cache checkout.
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "cacheheadrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream_rev"] == "cacheheadrev"
    assert conf["upstream_seen_rev"] == "skillfetchrev", \
        f"URL 형상인데 엔진이 seen 을 덮음(한 키 2역·스킬층 관찰 파괴): {conf!r}"


def test_record_upstream_revs_reports_recorded_keys_per_shape(
        pm_update, tmp_path, monkeypatch):
    """반환 `(changed, recorded)` 의 recorded = *엔진이 실제로 기록한* 키 — 형상별로 다르다.

    호출부(main)가 안내 문구를 결과 상태로 역추론하지 않게 하는 입력이다(상태 추론 금지):
    URL 정상 흐름은 스킬이 쓴 seen 이 이미 baseline 과 같아 파일만 봐선 구분이 안 된다.
    """
    source = tmp_path / "src"
    source.mkdir()
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shaperev777")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    path_dest = tmp_path / "path_dest"
    _write_local_conf(path_dest, "upstream=/w/project_manager_1\n")
    changed, recorded = pm_update.record_upstream_revs(path_dest, source)
    assert changed is True
    assert recorded == {"upstream_rev": "shaperev777", "upstream_seen_rev": "shaperev777"}

    url_dest = tmp_path / "url_dest"
    # URL 형상 + 스킬이 이미 기록한 seen == 이번 cache HEAD (파일 상태로는 구분 불가한 조건).
    _write_local_conf(
        url_dest,
        "upstream=https://github.com/example/project_manager.git\n"
        "upstream_seen_rev=shaperev777\n",
    )
    changed, recorded = pm_update.record_upstream_revs(url_dest, source)
    assert changed is True
    assert recorded == {"upstream_rev": "shaperev777"}, \
        f"URL 형상인데 seen 을 기록했다고 보고: {recorded!r}"


def test_record_upstream_revs_writes_both_keys_in_single_pass(
        pm_update, tmp_path, monkeypatch):
    """두 키는 **한 번의 set-or-replace + 한 번의 write** — baseline 만 앞선 반쪽 상태 불가.

    중간 중단 시 두 키가 어긋난 채 남는 것이 바로 이 티켓이 없앤 거짓 drift 의 원인이므로,
    분리 write 로 되돌아가면 실패한다(회귀 가드).
    """
    dest = tmp_path / "dest"
    _write_local_conf(dest, "upstream=/w/project_manager_1\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "onepassrev5")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    calls: list[dict] = []
    real_set = pm_import._set_conf_keys
    monkeypatch.setattr(
        pm_import, "_set_conf_keys",
        lambda text, updates: (calls.append(dict(updates)), real_set(text, updates))[1])

    assert pm_update.record_upstream_revs(dest, source)[0] is True
    assert calls == [{"upstream_rev": "onepassrev5", "upstream_seen_rev": "onepassrev5"}], \
        f"두 키가 단일 write 로 묶이지 않음(반쪽 상태 위험): {calls!r}"


def test_main_records_both_keys_on_path_sync(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(경로 upstream) 후 두 키가 같아진다 — 흡수 직후 drift advisory 0 의 조건."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "bothkeysrev1")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main([]) == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == conf.get("upstream_seen_rev") == "bothkeysrev1", \
        f"경로 sync 후 두 키 불일치(거짓 drift 재발): {conf!r}"
    assert "upstream_seen_rev 동시 기록" in capsys.readouterr().out


def test_main_no_changes_converges_stale_path_seen_rev(pm_update, tmp_path, monkeypatch, capsys):
    """RUN2(변경 0)도 경로 upstream의 stale seen 값을 baseline과 함께 수렴시킨다.

    업그레이드 RUN1은 구 엔진으로 실행돼 새 기록 로직을 못 탈 수 있다. RUN2는 새 엔진이나
    manifest 변경이 없으므로, 이 `not changes` 경로가 수렴 지점이어야 한다(T-0422).
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "stored_upstream"
    _make_upstream(source)
    sentinel = source / SENTINEL_REL
    dest_sentinel = fake_repo / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_text(sentinel.read_text(encoding="utf-8"), encoding="utf-8")
    _write_local_conf(
        fake_repo,
        f"upstream={source}\n"
        "upstream_rev=currentrev\n"
        "upstream_seen_rev=staleobservedrev\n",
    )
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "currentrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main([]) == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == conf.get("upstream_seen_rev") == "currentrev"
    assert "baseline 갱신" in capsys.readouterr().out


def test_main_no_changes_with_matching_path_revs_is_quiet(pm_update, tmp_path, monkeypatch, capsys):
    """정합된 RUN2는 local.conf write·revision 안내를 만들지 않는다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "stored_upstream"
    _make_upstream(source)
    sentinel = source / SENTINEL_REL
    dest_sentinel = fake_repo / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_text(sentinel.read_text(encoding="utf-8"), encoding="utf-8")
    local_conf = _write_local_conf(
        fake_repo,
        f"upstream={source}\nupstream_rev=currentrev\nupstream_seen_rev=currentrev\n",
    )
    before = local_conf.read_text(encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "currentrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    real_write_text = Path.write_text
    local_conf_writes: list[Path] = []

    def spy_write_text(path, *args, **kwargs):
        if path == local_conf:
            local_conf_writes.append(path)
        return real_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)

    assert pm_update.main([]) == 0
    assert local_conf.read_text(encoding="utf-8") == before
    assert local_conf_writes == [], "정합된 revision인데 local.conf write가 발생했다"
    assert "local.conf upstream_rev baseline 갱신" not in capsys.readouterr().out


def test_main_no_changes_dry_run_does_not_converge_path_revs(
        pm_update, tmp_path, monkeypatch, capsys):
    """RUN2 dry-run은 stale 경로 키를 발견해도 local.conf를 쓰지 않는다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "stored_upstream"
    _make_upstream(source)
    sentinel = source / SENTINEL_REL
    dest_sentinel = fake_repo / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_text(sentinel.read_text(encoding="utf-8"), encoding="utf-8")
    local_conf = _write_local_conf(
        fake_repo,
        f"upstream={source}\nupstream_rev=currentrev\nupstream_seen_rev=staleobservedrev\n",
    )
    before = local_conf.read_text(encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "currentrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main(["--dry-run"]) == 0
    assert local_conf.read_text(encoding="utf-8") == before
    assert "local.conf upstream_rev baseline 갱신" not in capsys.readouterr().out


def test_main_no_changes_skew_suppresses_both_path_revs(
        pm_update, tmp_path, monkeypatch, capsys):
    """변경 0이라도 manifest skew면 baseline·seen 수렴을 함께 억제한다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "stored_upstream"
    _make_upstream(source)
    sentinel = source / SENTINEL_REL
    dest_sentinel = fake_repo / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_text(sentinel.read_text(encoding="utf-8"), encoding="utf-8")
    local_conf = _write_local_conf(
        fake_repo,
        f"upstream={source}\nupstream_rev=currentrev\nupstream_seen_rev=staleobservedrev\n",
    )
    before = local_conf.read_text(encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    monkeypatch.setattr(pm_update, "detect_manifest_skew", lambda *a, **k: ("skew", ["new.py"]))

    assert pm_update.main([]) == 0
    assert local_conf.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "manifest skew" in out and "억제" in out


def test_main_url_sync_does_not_claim_seen_was_recorded(pm_update, tmp_path, monkeypatch, capsys):
    """URL 형상 실 sync — 엔진이 seen 을 안 썼으므로 "(+동시 기록)" 문구가 뜨면 안 된다.

    URL 정상 흐름(스킬이 fetch 후 seen 기록 → `--from <cache>` sync)에서는 seen==cache HEAD 라
    *결과 상태*로는 "엔진이 썼다"와 구분되지 않는다 — 문구는 실제 기록분으로만 정해야 한다.
    """
    fake_repo = tmp_path / "fake_repo"
    cache = tmp_path / "cache"  # 스킬이 clone/fetch 한 로컬 cache checkout.
    _make_upstream(cache)
    _write_local_conf(
        fake_repo,
        "upstream=https://github.com/example/project_manager.git\n"
        "upstream_seen_rev=cachehead77\n",  # 스킬이 fetch 후 기록한 관찰값.
    )
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "cachehead77")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main(["--from", str(cache)]) == 0
    out = capsys.readouterr().out
    assert "baseline 갱신" in out, f"URL sync 인데 baseline 갱신 안내 부재: {out!r}"
    assert "동시 기록" not in out, f"엔진이 안 쓴 seen 을 썼다고 주장(상태 역추론): {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_seen_rev") == "cachehead77"  # 스킬층 관찰값 그대로.


def test_main_skew_suppression_suppresses_seen_too(pm_update, tmp_path, monkeypatch, capsys):
    """manifest skew 억제 경로 — baseline 뿐 아니라 seen 도 미갱신(동시성 유지).

    baseline 만 멈추고 seen 이 앞서면 반대 방향 거짓 경보가 된다. 두 기록이 한 함수 안이라
    억제도 함께 걸린다 — skew 판정을 결정적으로 주입해 그 배선을 검증한다.
    """
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    skew_stub = ".project_manager/tools/__pm_update_skew_stub__.py"  # 합성 신규 등재분.
    monkeypatch.setattr(
        pm_update, "detect_manifest_skew", lambda *a, **k: ("skew", [skew_stub]))

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main([]) == 0
    out = capsys.readouterr().out
    assert "manifest skew" in out and "억제" in out, f"skew 억제 안내 미출력: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf, f"skew 인데 baseline 기록됨: {conf!r}"
    assert "upstream_seen_rev" not in conf, \
        f"skew 인데 seen 만 앞섬(반대 방향 거짓 경보): {conf!r}"


# ── T-0395: manifest skew → baseline 갱신 억제 (false-최신 차단·drift 은폐 방지) ──

NEW_ENGINE_REL = ".project_manager/tools/__pm_update_new_engine__.py"


def test_detect_manifest_skew_flags_new_upstream_entries(pm_update, tmp_path):
    """upstream manifest 에만 있는(로컬 manifest 부재) 등재 경로 → ('skew', [신규…]) 정렬."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])
    local_manifest = pm_update.read_manifest(_write_dest_manifest(tmp_path / "dest", [SENTINEL_REL]))

    status, new_entries = pm_update.detect_manifest_skew(local_manifest, source)
    assert status == "skew"
    assert new_entries == [NEW_ENGINE_REL], f"신규 등재분 미탐지/오탐: {new_entries!r}"


def test_detect_manifest_skew_in_sync_when_manifests_match(pm_update, tmp_path):
    """로컬 manifest 가 upstream 등재분을 모두 포함 → ('in_sync', []) (신규 등재분 0)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    local_manifest = pm_update.read_manifest(_write_dest_manifest(tmp_path / "dest", [SENTINEL_REL]))

    status, new_entries = pm_update.detect_manifest_skew(local_manifest, source)
    assert status == "in_sync" and new_entries == []


def test_detect_manifest_skew_markers_stripped_before_compare(pm_update, tmp_path):
    """마커(@render 등)는 read_manifest 가 떼어내므로 순수 경로 기준 비교 — 마커 차이는 skew 아님."""
    source = tmp_path / "src"
    (source / ".project_manager").mkdir(parents=True, exist_ok=True)
    (source / ".project_manager" / "engine.manifest").write_text(
        SENTINEL_REL + "  @render\n", encoding="utf-8")
    local_manifest = pm_update.read_manifest(_write_dest_manifest(tmp_path / "dest", [SENTINEL_REL]))

    status, new_entries = pm_update.detect_manifest_skew(local_manifest, source)
    assert status == "in_sync" and new_entries == [], \
        "마커만 다른 동일 경로를 신규 등재분으로 오탐(순수 경로 비교 위반)"


def test_detect_manifest_skew_upstream_missing_fail_soft(pm_update, tmp_path):
    """upstream engine.manifest 부재/읽기 실패 → ('upstream_missing', []) (fail-soft·현행 유지)."""
    source = tmp_path / "src"  # engine.manifest 미생성
    source.mkdir(parents=True, exist_ok=True)
    local_manifest = pm_update.read_manifest(_write_dest_manifest(tmp_path / "dest", [SENTINEL_REL]))

    status, new_entries = pm_update.detect_manifest_skew(local_manifest, source)
    assert status == "upstream_missing" and new_entries == []


def test_main_selfheal_supersedes_skew_suppression(pm_update, tmp_path, monkeypatch, capsys):
    """T-0395 amend(T-0396): 구형 로컬 manifest + 읽기 가능한 upstream 이면 baseline 억제가 아니라
    **자기치유** — upstream 승격으로 skew 가 정의상 0 이 되어, skew/억제 메시지 없이 baseline 이
    갱신된다(치유 후 정합). T-0395 억제는 upstream manifest 읽기 실패 잔여 케이스 안전망으로만 남고,
    읽기 가능한 구형 로컬은 이 경로가 대체한다(회사 실측 근절)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])  # 구형 — NEW_ENGINE_REL 누락.
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "amendrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])  # 실 sync.
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out and "억제" not in out, \
        f"읽기 가능한 구형 로컬인데 T-0395 억제가 자기치유로 대체 안 됨: {out!r}"
    assert "자기치유" in out and NEW_ENGINE_REL in out, f"자기치유 loud 미출력: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "amendrev", \
        f"치유 후 정합인데 baseline 미갱신(억제 잔존): {conf.get('upstream_rev')!r}"


def test_main_records_baseline_when_manifest_in_sync(pm_update, tmp_path, monkeypatch, capsys):
    """로컬 manifest 가 upstream 과 정합이면 현행대로 upstream_rev baseline 갱신 + skew 경고 없음."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL])  # 정합 로컬 manifest.
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "insyncrev7")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out, f"정합인데 skew 오탐: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "insyncrev7", \
        f"정합 sync 인데 baseline 미갱신: {conf.get('upstream_rev')!r}"


def test_main_records_baseline_when_upstream_manifest_absent(pm_update, tmp_path, monkeypatch, capsys):
    """upstream manifest 부재(구 upstream)면 대조 생략·현행대로 baseline 갱신 + fail-soft note."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    # source 에 sentinel 파일은 있으나 engine.manifest 는 없음(구 upstream). dest manifest 로 sync.
    sentinel = source / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    _write_dest_manifest(fake_repo, [SENTINEL_REL])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "failsoftrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fail-soft" in out, f"upstream manifest 부재 fail-soft note 미출력: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "failsoftrev", \
        f"fail-soft 인데 baseline 미갱신: {conf.get('upstream_rev')!r}"


def test_main_dry_run_shows_selfheal_not_skew_without_recording(pm_update, tmp_path, monkeypatch, capsys):
    """T-0395 amend(T-0396): --dry-run 은 읽기 가능한 구형 로컬을 skew 억제로 표시하지 않고 **자기치유
    예정**으로 표시하며, baseline 은 기록하지 않는다(read-only)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out and "억제" not in out, \
        f"dry-run 이 읽기 가능한 구형 로컬을 skew 억제로 오표시: {out!r}"
    assert "자기치유 예정" in out and NEW_ENGINE_REL in out, \
        f"dry-run 이 자기치유 예정을 표시하지 않음: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf, "dry-run 인데 baseline 기록됨(부작용 누출)"


def test_main_target_mode_skips_skew_detection(pm_update, tmp_path, monkeypatch, capsys):
    """--target(엔진 export) 경로는 타깃별 manifest 차이로 skew 를 오탐하지 않는다 — 검출/억제 비발화.

    타깃 manifest(templates/<name>/engine.manifest)가 루트(source)보다 등재분이 적어도(의도적
    어댑터 비대칭) skew 경고/baseline 억제가 발화하면 안 된다(codex must-fix). 현행 거동(무조건
    baseline 갱신)을 유지한다 — skew 검출은 self-update(채택자 흡수) 경로 한정.
    """
    fake_repo = tmp_path / "fake_repo"
    target_root = fake_repo / "templates" / "tgt"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])  # 루트 manifest = 2 등재.
    _write_dest_manifest(target_root, [SENTINEL_REL])  # 타깃 manifest = 1 등재(의도적 차이).
    _write_local_conf(target_root, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "targetrev1")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--from", str(source), "--target", "tgt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out and "억제" not in out, \
        f"--target 에서 skew 검출/억제가 발화함(오탐): {out!r}"
    conf = pm_update._read_local_conf(target_root / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "targetrev1", \
        f"--target 인데 baseline 미갱신(현행 거동 위반): {conf.get('upstream_rev')!r}"


# ── manifest 자기치유 (T-0396·self-update 2-pass 단일 실행) ─────────────────────
# 구형 로컬 manifest 를 upstream manifest 로 승격해 신규 등재분을 한 번의 실행으로 도달시킨다.
# manifest 자기전파 엔트리(T-0305) — upstream 이 항상 등재. 회사 실측 형상 재현에 함께 넣어
# 로컬 manifest 파일까지 자기치유되는지(신규 등재분 반영) 검증한다.
MANIFEST_SELF_REL = ".project_manager/engine.manifest"


def test_resolve_manifest_selfheal_promotes_upstream_on_new_entry(pm_update, tmp_path):
    """upstream 이 신규 등재 → ('heal', added=[신규], manifest=upstream_entries)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])
    _write_dest_manifest(tmp_path / "dest", [SENTINEL_REL])  # 구형 로컬 manifest.

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "heal"
    assert result["added"] == [NEW_ENGINE_REL] and result["removed"] == []
    assert {str(e) for e in result["manifest"]} == {SENTINEL_REL, NEW_ENGINE_REL}, \
        "승격 manifest 가 upstream 등재분 전체가 아님"


def test_resolve_manifest_selfheal_diverged_when_local_only_paths(pm_update, tmp_path):
    """로컬-전용 경로 존재(커스텀 추가) → ('diverged', manifest=None) — 승격 안 함.

    로컬이 upstream 의 부분집합이 아니면 전체 교체가 커스텀을 클로버하므로 승격하지 않고 현행 로컬
    manifest 를 유지한다([[T-0395]] skew 대조가 upstream 신규분 안전망). "항목 제외"(로컬⊂upstream)와 구별."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    _write_dest_manifest(tmp_path / "dest", [SENTINEL_REL, NEW_ENGINE_REL])  # 로컬-전용 경로.

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "diverged" and result["manifest"] is None
    assert result["removed"] == [NEW_ENGINE_REL]


def test_resolve_manifest_selfheal_ignores_guest_block_and_heals(pm_update, tmp_path):
    """add-harness guest 절(로컬-전용 `@target-owned`)이 있어도 **core 비교에서 제외**돼 upstream 신규
    항목이 정상 자기치유(승격)된다 (T-0456 R14 MF-1). 옛엔 guest 경로가 removed 에 섞여 **영구 diverged**
    → add-harness 인스턴스가 엔진 신규 등재를 영영 못 받았다(red-첫). guest 는 apply 가 재부착(대칭)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])   # upstream 신규 등재.
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    # dest = 구형 core(SENTINEL) + guest 절(.codex/agents·로컬-전용).
    _write_dest_manifest(tmp_path / "dest", [
        SENTINEL_REL, begin, ".codex/agents    @render @target-owned", end])
    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "heal", f"guest 절이 core 비교를 오염(diverged): {result['status']}"
    assert result["added"] == [NEW_ENGINE_REL]
    assert ".codex/agents" not in result["removed"], result["removed"]


def test_plan_engine_manifest_self_prop_no_churn_with_guest_block(pm_update, tmp_path):
    """engine.manifest self-prop: dest 가 guest 절을 갖고 upstream 은 안 가져도 **core 가 같으면 plan
    changes 에 안 뜬다** (guest 절 차감 semantic 비교·T-0456 R14 MF-2). raw filecmp 였으면 apply 가
    절을 재부착하는 한 **매 sync 영구 update(churn)** 로 떴다(red-첫)."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    core = "# manifest\n" + MANIFEST_SELF_REL + "\n"
    (source / ".project_manager").mkdir(parents=True)
    (source / ".project_manager" / "engine.manifest").write_text(core, encoding="utf-8")
    # dest = core + guest 절(apply 재부착 형상).
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        core.rstrip("\n") + "\n\n" + begin
        + "\n.codex/agents    @render @target-owned\n" + end + "\n", encoding="utf-8")
    manifest = [pm_update.ManifestEntry(MANIFEST_SELF_REL)]  # bare self-prop.
    changes, _missing = pm_update.plan(source, manifest, dest_root=dest)
    assert not any(str(r).replace("\\", "/") == MANIFEST_SELF_REL for r, _s, _d, _k in changes), \
        f"guest 절 때문에 engine.manifest 가 영구 churn(update): {changes}"


def test_plan_manifest_self_prop_no_churn_with_trailing_blank_upstream(pm_update, tmp_path):
    """**trailing blank line 보유 upstream manifest** 에서도 engine.manifest self-prop 이 반복 update 를
    안 만든다 (T-0456 R22 suggestion). `_strip_guest_manifest_block` 이 절 앞 빈 줄을 회수하며 upstream
    트레일링 블랭크까지 지워 core 비교가 어긋나 churn 나던 것을 `rstrip("\\n")` 정규화로 닫는다."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    core = "# m\n" + MANIFEST_SELF_REL + "\n\n"  # ← 트레일링 빈 줄.
    (source / ".project_manager").mkdir(parents=True)
    (source / ".project_manager" / "engine.manifest").write_text(core, encoding="utf-8")
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        core + begin + "\n.codex/agents    @render @target-owned\n" + end + "\n",
        encoding="utf-8")
    manifest = [pm_update.ManifestEntry(MANIFEST_SELF_REL)]
    changes, _missing = pm_update.plan(source, manifest, dest_root=dest)
    assert not any(str(r).replace("\\", "/") == MANIFEST_SELF_REL for r, _s, _d, _k in changes), \
        f"트레일링 블랭크 upstream 에서 engine.manifest churn(반복 update): {changes}"


def test_copy_manifest_preserving_guest_prunes_promoted_paths(pm_update, tmp_path):
    """소유권 전환: guest 경로가 upstream core 로 승격되면 재부착이 그 경로를 guest 절에서 **차감**해
    **단일 등재**(upstream 소유)로 만든다 — 이중 등재 → 뒤쪽 @target-owned guest 가 owner 로 이겨
    upstream 소스가 영구 skip 되던 것을 닫는다 (T-0456 R15 red-첫). 승격 안 된 guest 는 잔존."""
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    guest = (begin + "\n.codex/agents    @render @target-owned\n"
             ".agents/skills    @render @target-owned\n" + end)
    sp, dst = tmp_path / "up.manifest", tmp_path / "dest.manifest"
    # upstream core 가 .codex/agents 를 소유하게 됨(@source·승격). .agents/skills 는 여전히 guest.
    sp.write_text("# m\n.codex/agents    @render @source=templates/codex/.codex/agents\n",
                  encoding="utf-8")
    dst.write_text("# m\n\n" + guest + "\n", encoding="utf-8")  # old dest: core(no codex) + guest 절.
    pm_update._copy_manifest_preserving_guest(sp, dst)
    ents = pm_update.read_manifest(dst)
    codex = [e for e in ents if str(e) == ".codex/agents"]
    assert len(codex) == 1, f".codex/agents 이중 등재(전환 후 {len(codex)}개)"
    assert codex[0].source_rel is not None and not codex[0].target_owned, \
        ".codex/agents owner 가 upstream core(@source) 아님 — guest @target-owned 가 이겨 영구 skip"
    block = pm_update._extract_guest_manifest_block(dst.read_text(encoding="utf-8"))
    block_paths = {ln.split()[0] for ln in block.splitlines()
                   if ln.strip() and not ln.strip().startswith("#")}
    assert block_paths == {".agents/skills"}, f"guest 절 잔여가 예상과 다름: {block_paths}"


def test_prune_guest_block_ancestor_and_full_promotion(pm_update):
    """차감 판정 유닛: **상위 경로** 소유도 차감(core `.opencode` → guest `.opencode/agents`) + 전량
    승격 시 절 제거(None) + 무관 core 는 절 그대로 (T-0456 R15 경로-포함 판정)."""
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    guest = begin + "\n.opencode/agents    @render @target-owned\n" + end
    # core 가 상위 `.opencode` 소유 → guest `.opencode/agents` 차감 → 남는 guest 0 → 절 제거(None).
    assert pm_update._prune_guest_block_owned_by_core(
        guest, ".opencode    @render @source=x\n") is None
    # core 무관 → 절 그대로(무차감).
    assert pm_update._prune_guest_block_owned_by_core(
        guest, ".claude/agents    @render\n") == guest
    # guest_block None → None(무동작).
    assert pm_update._prune_guest_block_owned_by_core(None, ".opencode\n") is None


def test_resolve_manifest_selfheal_flavor_source_not_clobbered_by_root(pm_update, tmp_path):
    """codex MF 회귀: flavor 채택자(@source self-prop)는 root(bare)가 아니라 **flavor upstream** 과
    비교/승격한다 — root 승격으로 flavor manifest 의 `@source` self-prop 을 bare 로 클로버하지 않는다.

    naive 경로-집합 비교였다면 로컬(@source)과 root(bare)를 대조해 flavor manifest 를 root 로 덮었다.
    self-prop 의 @source 를 따라 flavor↔flavor 로 비교하면 마커가 정합해 신규분만 승격되고 @source 가 보존된다."""
    source = tmp_path / "src"
    # root(bare) manifest — 신규 등재 포함(flavor 채택자가 이걸 승격 대상으로 삼으면 클로버).
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])
    # flavor upstream manifest(templates/claude_code) — flavor self-prop(@source)·신규 등재 포함.
    flavor_rel = "templates/claude_code/.project_manager/engine.manifest"
    flavor_self = f"{MANIFEST_SELF_REL}    @source={flavor_rel}"
    (source / "templates" / "claude_code" / ".project_manager").mkdir(parents=True, exist_ok=True)
    (source / flavor_rel).write_text(
        "\n".join([SENTINEL_REL, NEW_ENGINE_REL, flavor_self]) + "\n", encoding="utf-8")
    # 채택자(dest) manifest — flavor self-prop(@source)·구형(NEW_ENGINE_REL 미등재).
    _write_dest_manifest(tmp_path / "dest", [SENTINEL_REL, flavor_self])

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "heal", \
        f"flavor 채택자가 flavor upstream 과 자기치유 안 됨(root 대조로 diverged?): {result['status']}"
    assert result["added"] == [NEW_ENGINE_REL]
    selfprop = [e for e in result["manifest"] if str(e) == MANIFEST_SELF_REL]
    assert selfprop and selfprop[0].source_rel == flavor_rel, \
        "승격 manifest 의 self-prop 이 flavor @source 를 보존 안 함(root bare 로 클로버)"


def test_resolve_manifest_selfheal_diverged_on_marker_edit(pm_update, tmp_path):
    """공통 경로의 마커가 로컬에서 편집(예: @render 추가)되면 → ('diverged') — 마커 클로버 방지.

    경로 집합만 보면 subset(removed 0)이라 heal 로 오판하지만, 공통 경로의 마커/@source divergence 를
    감지해 승격을 막는다(codex MF 강화)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])  # root: bare SENTINEL + NEW.
    _write_dest_manifest(tmp_path / "dest", [f"{SENTINEL_REL}    @render"])  # SENTINEL 에 @render 로컬 편집.

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "diverged" and result["manifest"] is None, \
        f"공통 경로 마커 divergence 인데 승격됨(클로버): {result['status']}"


def test_resolve_manifest_selfheal_in_sync_when_identical(pm_update, tmp_path):
    """로컬 manifest 텍스트가 upstream 과 동일 → ('in_sync', manifest=None·로컬 유지)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    # upstream 과 동일 텍스트로 dest manifest 를 쓴다(개행/순서 동일).
    (tmp_path / "dest" / ".project_manager").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dest" / ".project_manager" / "engine.manifest").write_text(
        SENTINEL_REL + "\n", encoding="utf-8")

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "in_sync" and result["manifest"] is None


def test_resolve_manifest_selfheal_upstream_missing_fail_soft(pm_update, tmp_path):
    """upstream engine.manifest 부재 → ('upstream_missing', manifest=None) (fail-soft·T-0395 안전망)."""
    source = tmp_path / "src"  # engine.manifest 미생성.
    source.mkdir(parents=True, exist_ok=True)
    _write_dest_manifest(tmp_path / "dest", [SENTINEL_REL])

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "upstream_missing" and result["manifest"] is None


def test_resolve_manifest_selfheal_no_local_manifest(pm_update, tmp_path):
    """로컬 manifest 부재(fresh) → ('no_local', manifest=None) (resolve 가 이미 source 기준)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    (tmp_path / "dest").mkdir(parents=True, exist_ok=True)  # dest manifest 없음.

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "no_local" and result["manifest"] is None


def test_main_selfheal_reaches_new_entry_in_one_run(pm_update, tmp_path, monkeypatch, capsys):
    """회사 실측 재현: 구 manifest + 신 등재 upstream → **한 번의 pm_update** 로 신규 파일 도달.

    로컬 manifest 가 구형(NEW_ENGINE_REL 미등재)이어도 upstream manifest 승격으로 plan 이
    신규 파일을 실어 apply 가 dest 에 복사한다. manifest 자기전파(T-0305)로 로컬 manifest 파일도
    upstream 판으로 갱신되고, 치유 후 정합이므로 baseline 이 갱신된다(다음 sync 부터 최신 추적).
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])  # 구형 — 신규 등재 누락.
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "healedrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])  # 단일 실행.
    assert rc == 0
    # ① 신규 엔진 파일이 한 번의 실행으로 dest 에 도달(자기치유 핵심 DoD).
    assert (fake_repo / NEW_ENGINE_REL).exists(), \
        "신규 등재 엔진 파일이 한 번의 pm_update 로 도달 안 함(자기치유 실패)"
    # ② 로컬 manifest 파일이 upstream 판으로 갱신(신규 등재분 반영·self-prop).
    healed = (fake_repo / ".project_manager" / "engine.manifest").read_text(encoding="utf-8")
    assert NEW_ENGINE_REL in healed, "로컬 manifest 자기치유 안 됨(신규 등재분 미반영)"
    # ③ 자기치유 loud + 치유 후 정합이라 baseline 갱신(skew 미발화).
    out = capsys.readouterr().out
    assert "자기치유" in out and NEW_ENGINE_REL in out, f"자기치유 loud 미출력: {out!r}"
    assert "manifest skew" not in out, f"치유 후에도 skew 발화(정합 위반): {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "healedrev", \
        f"치유 후 정합인데 baseline 미갱신: {conf.get('upstream_rev')!r}"


def test_main_dry_run_shows_selfheal_without_side_effects(pm_update, tmp_path, monkeypatch, capsys):
    """--dry-run 은 자기치유 예정을 표시하되 파일/manifest/baseline 을 건드리지 않는다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "자기치유 예정" in out and NEW_ENGINE_REL in out, \
        f"dry-run 이 자기치유 예정을 표시하지 않음: {out!r}"
    assert not (fake_repo / NEW_ENGINE_REL).exists(), "dry-run 인데 신규 파일 복사됨(부작용)"
    healed = (fake_repo / ".project_manager" / "engine.manifest").read_text(encoding="utf-8")
    assert NEW_ENGINE_REL not in healed, "dry-run 인데 로컬 manifest 갱신됨(부작용)"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf, "dry-run 인데 baseline 기록됨(부작용)"


def test_main_target_mode_skips_selfheal(pm_update, tmp_path, monkeypatch, capsys):
    """--target(엔진 export)은 자기치유 비발화 — 타깃 manifest(루트보다 적은 등재)를 그대로 쓴다.

    타깃 manifest 는 루트와 의도적으로 다르므로(어댑터 비대칭) upstream 승격하면 대량 오탐/오전파.
    현행(타깃 manifest 기준)을 유지한다 — 자기치유는 self-update(채택자 흡수) 경로 한정.
    """
    fake_repo = tmp_path / "fake_repo"
    target_root = fake_repo / "templates" / "tgt"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])  # 루트 = 2 등재.
    _write_dest_manifest(target_root, [SENTINEL_REL])  # 타깃 = 1 등재(의도적 차이).
    _write_local_conf(target_root, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "targetrev2")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--from", str(source), "--target", "tgt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "자기치유" not in out, f"--target 에서 자기치유 발화(오탐): {out!r}"
    # 타깃 manifest 기준이라 NEW_ENGINE_REL 은 전파되지 않는다(의도적 어댑터 비대칭 유지).
    assert not (target_root / NEW_ENGINE_REL).exists(), \
        "--target 인데 루트-전용 등재분이 타깃으로 새어 전파됨(승격 오발화)"


def test_main_flavor_selfheal_skew_uses_flavor_manifest_no_false_suppress(
        pm_update, tmp_path, monkeypatch, capsys):
    """codex R3 회귀: flavor 채택자(@source self-prop) 는 치유 후 root-only 경로를 skew 오탐하지 않는다.

    skew 대조 upstream manifest 를 selfheal 이 해소한 flavor 경로로 통일(T-0395 탐지 == T-0396 승격
    기준). root manifest 에만 있는 경로(`root_only`)가 있어도, flavor 채택자는 flavor upstream 과
    대조하므로 skew/억제 없이 신규분이 도달하고 baseline 이 갱신된다. (fix 부재면 root 대조로
    root_only 가 skew 오탐돼 baseline 억제.)"""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    flavor_rel = "templates/claude_code/.project_manager/engine.manifest"
    flavor_self = f"{MANIFEST_SELF_REL}    @source={flavor_rel}"
    root_only_rel = ".project_manager/tools/__pm_update_root_only__.py"
    # 공유 source 파일(플랜 apply 가 읽는다).
    for rel in (SENTINEL_REL, NEW_ENGINE_REL, root_only_rel):
        f = source / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# src {rel}\n", encoding="utf-8")
    # root manifest(bare self-prop) — root-only 경로 + 신규 포함(flavor 채택자가 이걸로 skew 하면 오탐).
    (source / ".project_manager" / "engine.manifest").write_text(
        "\n".join([SENTINEL_REL, root_only_rel, NEW_ENGINE_REL, MANIFEST_SELF_REL]) + "\n",
        encoding="utf-8")
    # flavor upstream manifest(@source self-prop) — 공유 + 신규, root-only 없음.
    (source / flavor_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / flavor_rel).write_text(
        "\n".join([SENTINEL_REL, NEW_ENGINE_REL, flavor_self]) + "\n", encoding="utf-8")
    # 채택자 manifest = flavor(@source self-prop)·구형(NEW_ENGINE_REL 미등재).
    _write_dest_manifest(fake_repo, [SENTINEL_REL, flavor_self])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "flavorrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out and "억제" not in out, \
        f"flavor 채택자가 root-only 경로를 skew 오탐(대조 기준 불일치): {out!r}"
    assert "자기치유" in out and NEW_ENGINE_REL in out, f"자기치유 loud 미출력: {out!r}"
    assert (fake_repo / NEW_ENGINE_REL).exists(), "flavor 자기치유가 신규 파일 미도달"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "flavorrev", \
        f"flavor 치유 후 정합인데 baseline 미갱신(root skew 오탐 억제?): {conf.get('upstream_rev')!r}"


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


# ── ADR-0036: engine.version 유령 마커 폐기 가드 (제거 회귀 방지) ──────────────

def test_help_has_no_version_option():
    """`--version` CLI 인자가 폐기됐다(ADR-0036) — --help 출력에 다시 새지 않게 가드.

    engine.version 은 vestigial(read 0·no-op) 유령 마커였고, freshness 는 git rev-baseline
    (upstream_rev↔upstream_seen_rev)이 담당한다. 실 CLI 표면을 subprocess --help 로 검증한다.
    """
    result = subprocess.run(
        [sys.executable, str(TOOLS / "pm_update.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"--help rc!=0: {result.stderr!r}"
    assert "--version" not in result.stdout, \
        f"--version 옵션이 --help 에 재등장(ADR-0036 제거 회귀): {result.stdout!r}"


def test_version_arg_rejected_by_argparse(pm_update):
    """`--version` 이 argparse 에서 거부된다(ADR-0036) — 인터페이스 부재 + write 경로 도달불가 직접 증명.

    main(["--version", "x"]) 는 unrecognized arg 로 argparse SystemExit(2). 이 가드는 인자
    재추가(따라서 engine.version write 블록 복원 시 도달 가능)를 *직접* fail 시킨다 — 산출물
    부재(파일 미생성) 단언은 트리거(--version) 없인 write 블록 복원해도 통과하는 tautology 라 폐기.
    """
    with pytest.raises(SystemExit) as exc:
        pm_update.main(["--version", "x"])
    assert exc.value.code == 2, \
        f"--version 이 argparse 에서 거부 안 됨(ADR-0036 인자 제거 회귀): exit={exc.value.code}"


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


# ── @source= 마커 파싱 + source-remap (T-0303·ADR-0054) ──────────────────────
# `@source=<relpath>` 는 값 운반 마커 — source_root 아래 canonical 소스에서 읽고 dest 엔 manifest
# 경로로 기록한다(_source_root_rel·_remap_to_dest). @render/@target-owned(boolean)와 공존·순서무관.

def test_read_manifest_source_marker_parsed(pm_update, tmp_path):
    """`path @source=<relpath>` → source_rel=<relpath>, 순수 경로만 값으로 남는다(T-0303·ADR-0054)."""
    manifest = _write_manifest(
        tmp_path, [".opencode/agents  @source=templates/opencode/.opencode/agents"])
    e = pm_update.read_manifest(manifest)[0]
    assert str(e) == ".opencode/agents"
    assert e.source_rel == "templates/opencode/.opencode/agents"
    assert e.render is False
    assert e.target_owned is False


def test_read_manifest_render_and_source_coexist_order_independent(pm_update, tmp_path):
    """`@render @source=<path>` 공존·순서무관 — render=True + source_rel 파싱, 순수 경로만 남는다."""
    manifest = _write_manifest(tmp_path, [
        ".opencode/agents   @render @source=templates/opencode/.opencode/agents",
        ".opencode/command  @source=templates/opencode/.opencode/command @render",
    ])
    agents, command = pm_update.read_manifest(manifest)
    assert str(agents) == ".opencode/agents"
    assert agents.render is True and agents.target_owned is False
    assert agents.source_rel == "templates/opencode/.opencode/agents"
    assert str(command) == ".opencode/command"
    assert command.render is True and command.target_owned is False
    assert command.source_rel == "templates/opencode/.opencode/command"


def test_read_manifest_no_source_marker_is_none(pm_update, tmp_path):
    """@source 없는 항목(무마커·@render·@target-owned)은 source_rel None (후방호환·읽기 경로 = manifest 경로)."""
    manifest = _write_manifest(tmp_path, [
        ".project_manager/tools/board.py",
        ".claude/agents  @render",
        ".opencode/x  @target-owned",
    ])
    entries = pm_update.read_manifest(manifest)
    assert all(e.source_rel is None for e in entries)


def test_read_manifest_empty_source_value_is_none(pm_update, tmp_path):
    """`@source=`(빈 값)은 무의미 → source_rel None (읽기 경로 = manifest 경로·후방호환)."""
    e = pm_update.read_manifest(_write_manifest(tmp_path, [".opencode/agents  @source="]))[0]
    assert str(e) == ".opencode/agents"
    assert e.source_rel is None


# ── 단위: _source_root_rel / _remap_to_dest (source-remap·_dest_relpath_for 대칭 쌍) ──

def test_source_root_rel_uses_marker_else_str(pm_update, tmp_path):
    """@source= 있으면 _source_root_rel = source_rel(canonical 소스), 없으면 str(entry)."""
    sourced, plain = pm_update.read_manifest(_write_manifest(tmp_path, [
        ".opencode/agents  @source=templates/opencode/.opencode/agents",
        ".project_manager/tools/board.py",
    ]))
    assert pm_update._source_root_rel(sourced) == "templates/opencode/.opencode/agents"
    assert pm_update._source_root_rel(plain) == ".project_manager/tools/board.py"


def test_source_root_rel_plain_str_fallback(pm_update):
    """평문 str 항목(레거시 호출·source_rel 속성 부재) → str 그대로(getattr 폴백·후방호환)."""
    assert pm_update._source_root_rel(".project_manager/tools/board.py") == \
        ".project_manager/tools/board.py"


def test_remap_to_dest_directory_prefix(pm_update):
    """디렉토리 @source: yield relpath 의 source_rel prefix → manifest 경로로 치환(하위 파일)."""
    assert pm_update._remap_to_dest(
        "templates/opencode/.opencode/agents/architect.md",
        "templates/opencode/.opencode/agents",
        ".opencode/agents",
    ) == ".opencode/agents/architect.md"


def test_remap_to_dest_file_whole(pm_update):
    """파일 @source: yield 가 source_rel 자체 → manifest 경로로 통째 치환."""
    assert pm_update._remap_to_dest(
        "templates/opencode/.opencode/agents/pm.md",
        "templates/opencode/.opencode/agents/pm.md",
        ".opencode/agents/pm.md",
    ) == ".opencode/agents/pm.md"


def test_remap_to_dest_no_marker_passthrough(pm_update):
    """source_rel == manifest_path(마커 부재·기본) → 무변경(후방호환)."""
    assert pm_update._remap_to_dest(
        ".project_manager/tools/board.py",
        ".project_manager/tools/board.py",
        ".project_manager/tools/board.py",
    ) == ".project_manager/tools/board.py"


def test_remap_to_dest_normalizes_windows_separators(pm_update):
    """Windows 역슬래시 relpath 도 posix-normalize 후 치환(_dest_relpath_for 동형)."""
    assert pm_update._remap_to_dest(
        "templates\\opencode\\.opencode\\agents\\architect.md",
        "templates/opencode/.opencode/agents",
        ".opencode/agents",
    ) == ".opencode/agents/architect.md"


def test_plan_specific_file_source_overrides_shared_directory_source(pm_update, tmp_path):
    """pm-dev override는 쓰되 sibling shared skill(pm-qa)은 parent source에서 정상 update한다(T-0435)."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    shared = source / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    shared_sibling = source / ".claude" / "skills" / "pm-qa" / "SKILL.md"
    override = source / "templates" / "codex" / ".agents" / "skills" / \
        "pm-dev-delegate" / "SKILL.md"
    landed = dest / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    landed_sibling = dest / ".agents" / "skills" / "pm-qa" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared_sibling.parent.mkdir(parents=True)
    override.parent.mkdir(parents=True)
    landed.parent.mkdir(parents=True)
    landed_sibling.parent.mkdir(parents=True)
    shared.write_text("claude fields\n", encoding="utf-8")
    shared_sibling.write_text("shared pm-qa\n", encoding="utf-8")
    override.write_text("codex spawn_agent\n", encoding="utf-8")
    landed.write_text("stale\n", encoding="utf-8")
    landed_sibling.write_text("stale sibling\n", encoding="utf-8")
    entries = pm_update.read_manifest(_write_manifest(tmp_path, [
        ".agents/skills @source=.claude/skills",
        ".agents/skills/pm-dev-delegate/SKILL.md "
        "@source=templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    ]))

    changes, missing = pm_update.plan(source, entries, dest_root=dest)

    assert missing == []
    assert [(rel, src, kind) for rel, src, _dst, kind in changes] == [
        (".agents/skills/pm-qa/SKILL.md", shared_sibling, "update"),
        (".agents/skills/pm-dev-delegate/SKILL.md", override, "update")
    ]


def test_plan_missing_specific_override_never_falls_back_to_shared_skill(pm_update, tmp_path):
    """override source 부재는 parent shared source가 있어도 missing으로 loud 처리한다(T-0435 sensitivity)."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    shared = source / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    landed = dest / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    landed.parent.mkdir(parents=True)
    shared.write_text("claude-only fallback must not land\n", encoding="utf-8")
    landed.write_text("stale codex native delegate\n", encoding="utf-8")
    override_dest = ".agents/skills/pm-dev-delegate/SKILL.md"
    entries = pm_update.read_manifest(_write_manifest(tmp_path, [
        ".agents/skills @source=.claude/skills",
        f"{override_dest} @source=templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    ]))

    changes, missing = pm_update.plan(source, entries, dest_root=dest)

    assert changes == [], "specific source 부재 때 Claude shared skill로 silent fallback 하면 안 됨"
    assert missing == [override_dest]


def test_plan_specific_override_is_idempotent_not_replaced_by_parent(pm_update, tmp_path):
    """override와 이미 같은 destination은 상위 shared source 차이를 update로 오보하지 않는다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    shared = source / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    override = source / "templates" / "codex" / ".agents" / "skills" / \
        "pm-dev-delegate" / "SKILL.md"
    landed = dest / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    override.parent.mkdir(parents=True)
    landed.parent.mkdir(parents=True)
    shared.write_text("claude fields\n", encoding="utf-8")
    override.write_text("codex spawn_agent\n", encoding="utf-8")
    landed.write_text("codex spawn_agent\n", encoding="utf-8")
    entries = pm_update.read_manifest(_write_manifest(tmp_path, [
        ".agents/skills @source=.claude/skills",
        ".agents/skills/pm-dev-delegate/SKILL.md "
        "@source=templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    ]))

    changes, missing = pm_update.plan(source, entries, dest_root=dest)

    assert missing == []
    assert changes == []


# ── T-0303 통합: @source 전파(self-update·--target no-op)·안전판·render 정합·claude 무영향 ──
# opencode 채택자의 self-update 가 `.opencode/*` 를 templates/opencode canonical 소스서 전파하는지
# (과거 @target-owned skip 으로 영영 stale 이던 치명 버그)·진짜 부재 시 rc2 안전판·--target self-copy
# no-op·claude `.claude/* @render` 무영향·@render+@source 렌더 정합을 실 sync(apply)로 박는다.

def test_self_update_propagates_opencode_adapters_from_templates_source(
        pm_update, tmp_path, monkeypatch, capsys):
    """(a) opencode 채택자 self-update 가 `.opencode/agents`·`command` 를 templates/opencode 소스서 전파.

    채택자 dest 엔 `.opencode/*` 로 살지만 프레임워크 루트(source)의 canonical 소스는
    `templates/opencode/.opencode/*` 에 있다(루트=claude·`.opencode/` 부재). @source 마커가 root-상대
    소스를 dest `.opencode/*` 로 remap 해 실 전파를 일으킨다. 실 apply 로 dest 파일 착지를 검증한다.
    """
    fake_repo = tmp_path / "adopter"        # 채택자 = dest(self-location REPO)
    stored = tmp_path / "framework_root"    # upstream = 프레임워크 루트(templates/opencode 보유)

    # source(프레임워크 루트): canonical .opencode 소스는 templates/opencode/.opencode/* 아래.
    agent_src = stored / "templates" / "opencode" / ".opencode" / "agents" / "pm.md"
    agent_src.parent.mkdir(parents=True, exist_ok=True)
    agent_src.write_text("# pm agent (upstream 개선판)\n", encoding="utf-8")
    cmd_src = stored / "templates" / "opencode" / ".opencode" / "command" / "pm-bootstrap.md"
    cmd_src.parent.mkdir(parents=True, exist_ok=True)
    cmd_src.write_text("# bootstrap (upstream 개선판)\n", encoding="utf-8")

    # 채택자 dest manifest: @source 마커로 root-상대 소스 → dest `.opencode/*` remap.
    dest_manifest = fake_repo / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    dest_manifest.write_text(
        ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n"
        ".opencode/command   @render @source=templates/opencode/.opencode/command\n",
        encoding="utf-8",
    )
    _write_local_conf(fake_repo, f"upstream={stored}\nexternal_review_enabled=false\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main([])  # 실 sync(apply)

    captured = capsys.readouterr()
    assert rc == 0, f"self-update 전파 실패(rc={rc}): {captured.err!r}"
    landed_agent = fake_repo / ".opencode" / "agents" / "pm.md"
    landed_cmd = fake_repo / ".opencode" / "command" / "pm-bootstrap.md"
    assert landed_agent.exists(), ".opencode/agents/pm.md 가 전파되지 않음(source-remap 실패·치명 버그 재발)."
    assert landed_cmd.exists(), ".opencode/command/pm-bootstrap.md 가 전파되지 않음."
    assert landed_agent.read_text(encoding="utf-8") == "# pm agent (upstream 개선판)\n"
    # 정상 전파 — rc2 안전판/skip 경로가 아니다.
    assert "source 에 없음" not in captured.err
    assert "[skip]" not in captured.out


def test_self_update_source_templates_absent_errors_rc2(
        pm_update, tmp_path, monkeypatch, capsys):
    """(b) @source 대상(templates/opencode) 진짜 부재면 rc2 — 안전판(non-@target-owned·은폐 금지).

    @source 는 source 가 templates/ 아래 *실재*함을 전제한다 — 잘못된 --from(templates/opencode
    없는 checkout·stripped)이면 진짜 누락이므로 graceful skip 이 아니라 rc2 로 멈춰야 한다
    (@target-owned 폐지의 핵심 안전 회복·ADR-0054 Decision 4).
    """
    fake_repo = tmp_path / "adopter"
    stored = tmp_path / "bad_checkout"   # templates/opencode 부재(잘못된/불완전 엔진 checkout)
    (stored / ".project_manager").mkdir(parents=True)

    dest_manifest = fake_repo / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    dest_manifest.write_text(
        ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n",
        encoding="utf-8",
    )
    _write_local_conf(fake_repo, f"upstream={stored}\nexternal_review_enabled=false\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "@source 소스(templates/opencode) 부재가 rc2 로 멈추지 않음(엔진/템플릿 누락 은폐)."
    # 부재는 manifest(dest) 경로로 surface + non-@target-owned 이므로 skip 아님.
    assert ".opencode/agents" in captured.err, "@source 소스 부재가 에러로 surface 되지 않음."
    assert "[skip]" not in captured.out, "@source 부재를 skip 으로 처리함(안전판 무력화·@target-owned 아님)."


def test_target_opencode_source_channel_self_copy_noop(pm_update, tmp_path, monkeypatch, capsys):
    """(c) `--target opencode` self-copy no-op — @source 가 templates/opencode/.opencode/* 로 remap 되면
    dest(templates/opencode/.opencode/*)와 동일 경로라 byte-identical → 변경 0.

    source=프레임워크 루트·dest=templates/opencode 일 때 src==dst(self-copy)이므로 no-op 여야 한다
    (--target 은 render 무시·copy2 비교).
    """
    fake_repo = tmp_path / "fake_repo"
    oc_dir = fake_repo / "templates" / "opencode"
    # canonical .opencode 소스(= dest 와 동일 위치·self-copy 대상).
    agent = oc_dir / ".opencode" / "agents" / "pm.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text("# pm agent\n", encoding="utf-8")
    oc_manifest = oc_dir / ".project_manager" / "engine.manifest"
    oc_manifest.parent.mkdir(parents=True, exist_ok=True)
    oc_manifest.write_text(
        ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n",
        encoding="utf-8",
    )
    _write_local_conf(oc_dir, f"upstream={fake_repo}\nexternal_review_enabled=false\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--target", "opencode", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0, f"--target opencode self-copy 가 rc0 아님: {captured.err!r}"
    assert "최신 — 변경 없음" in captured.out, \
        f"self-copy 가 no-op 이 아님(변경 감지·src==dst 이어야 함): {captured.out!r}"


def test_claude_render_only_entry_unaffected_by_source_channel(
        pm_update, tmp_path, monkeypatch, capsys):
    """(d) claude `.claude/agents @render`(source_rel None) 는 @source 채널 도입에 무영향(후방호환).

    @source 마커가 없는 항목은 _source_root_rel == str(entry)·_remap_to_dest no-op 이라 source 읽기
    경로 = manifest 경로 그대로다. `.claude/agents/developer.md` 가 source `.claude/agents/*` 에서
    remap 없이 그대로 전파됨을 실 apply 로 검증(native root-sourced 유지·ADR-0054 Decision 5).
    """
    fake_repo = tmp_path / "adopter"
    stored = tmp_path / "framework_root"
    claude_src = stored / ".claude" / "agents" / "developer.md"
    claude_src.parent.mkdir(parents=True, exist_ok=True)
    claude_src.write_text("# developer agent\n", encoding="utf-8")

    dest_manifest = fake_repo / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    dest_manifest.write_text(".claude/agents  @render\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={stored}\nexternal_review_enabled=false\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main([])

    captured = capsys.readouterr()
    assert rc == 0, f"claude @render-only 전파 실패(source 채널 도입이 native 경로 깸): {captured.err!r}"
    landed = fake_repo / ".claude" / "agents" / "developer.md"
    assert landed.exists(), ".claude/agents/developer.md 가 manifest 경로 그대로 전파되지 않음(remap 오작동)."
    assert landed.read_text(encoding="utf-8") == "# developer agent\n"


def test_render_with_source_marker_renders_operational_tokens(
        pm_update, tmp_path, monkeypatch, capsys):
    """(e) `@render`+`@source` 정합 — @source 로 remap 한 토큰-form 소스를 채택자 local.conf 로 렌더해
    dest `.opencode/*` 에 자족 산출물을 쓴다(operational 치환·source-remap 후에도 render 배선 정합).

    canonical 소스의 `{{PROJECT_NAME}}` 토큰이 채택자 local.conf(project_name=)로 치환돼 dest 에
    착지하는지 실 apply 로 검증(byte-copy 아닌 [render] 표기).
    """
    fake_repo = tmp_path / "adopter"
    stored = tmp_path / "framework_root"
    agent_src = stored / "templates" / "opencode" / ".opencode" / "agents" / "pm.md"
    agent_src.parent.mkdir(parents=True, exist_ok=True)
    agent_src.write_text("# pm for {{PROJECT_NAME}}\n", encoding="utf-8")

    dest_manifest = fake_repo / ".project_manager" / "engine.manifest"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    dest_manifest.write_text(
        ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n",
        encoding="utf-8",
    )
    _write_local_conf(
        fake_repo,
        f"upstream={stored}\nproject_name=AcmePay\nexternal_review_enabled=false\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main([])

    captured = capsys.readouterr()
    assert rc == 0, f"@render+@source 렌더 전파 실패: {captured.err!r}"
    landed = fake_repo / ".opencode" / "agents" / "pm.md"
    assert landed.exists(), "@render+@source 산출물이 dest 에 착지하지 않음."
    rendered = landed.read_text(encoding="utf-8")
    assert rendered == "# pm for AcmePay\n", f"operational 토큰 미치환(source-remap 후 render 정합 실패): {rendered!r}"
    assert "{{PROJECT_NAME}}" not in rendered, "자족 산출물 위반(토큰 잔존)."
    assert "[render]" in captured.out, "@source+@render 항목이 [render] 로 표기되지 않음(byte-copy 오분기)."


# ── T-0146: pm_update --changes — baseline↔HEAD 변경점 요약 (read-only·D5) ────
# 전부 git_runner 주입(DI seam)으로 hermetic — 라이브 git/네트워크 0. fake_git_runner 가
# argv 패턴(rev-parse·cat-file·log·diff)별로 결정적 출력을 돌려준다.

def _make_fake_git_runner(
    *,
    head: str = "headHHHHHHHH",
    baseline_reachable: bool = True,
    log_lines=None,
    diff_lines=None,
):
    """argv 패턴별 결정적 (rc, out) — summarize_upstream_changes 의 4 호출을 커버한다.

    - rev-parse HEAD            → (0, head)        / head="" → (1, "")
    - cat-file -e <rev>^{commit} → (0, "") if reachable else (1, "missing")
    - log --oneline base..HEAD  → (0, "\\n".join(log_lines))
    - diff --name-status …      → (0, "\\n".join(diff_lines))
    """
    log_lines = log_lines or []
    diff_lines = diff_lines or []

    def runner(argv):
        if "rev-parse" in argv:
            return (0, head + "\n") if head else (1, "")
        if "cat-file" in argv:
            return (0, "") if baseline_reachable else (1, "fatal: bad object")
        if "log" in argv:
            return 0, "\n".join(log_lines) + ("\n" if log_lines else "")
        if "diff" in argv:
            return 0, "\n".join(diff_lines) + ("\n" if diff_lines else "")
        return 1, f"unexpected argv: {argv!r}"

    return runner


def _make_source_with_manifest(root: Path, manifest_lines):
    """source(upstream) checkout 디렉토리 + engine.manifest.

    main e2e 에서 _run_changes 가 resolve_manifest_for_dest(dest 우선·없으면 source)로 manifest 를
    해소하므로, source 트리에 engine.manifest 를 둬 그 경로가 권위가 되게 한다(dest manifest 부재 시).
    """
    (root / ".project_manager").mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / "engine.manifest").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8")


def _manifest_entries(pm_update, lines):
    """manifest 줄 리스트 → ManifestEntry 리스트 (summarize_upstream_changes 에 직접 주입용).

    헬퍼 단위테스트는 manifest 를 *인자로* 넘긴다(codex MF — 분류 manifest 는 호출부가 sync 와
    동일 경로로 해소). read_manifest 의 파싱(마커·주석)을 거쳐 실제 ManifestEntry 를 만든다.
    """
    text = "\n".join(lines) + "\n"
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".manifest", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = Path(f.name)
    try:
        return pm_update.read_manifest(path)
    finally:
        path.unlink()


# ── 헬퍼 단위: summarize_upstream_changes (git_runner 주입·hermetic) ──────────

def test_summarize_normal_splits_engine_and_other(pm_update, tmp_path):
    """정상: N commits + manifest 항목(파일·디렉토리)에 따라 engine/other 분리."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [
        ".project_manager/tools/board.py",   # 파일 항목
        ".claude/agents",                    # 디렉토리 항목(prefix 매칭)
    ])
    runner = _make_fake_git_runner(
        head="abcdef1234567890",
        log_lines=["abc1234 fix board lint", "def5678 add agent"],
        diff_lines=[
            "M\t.project_manager/tools/board.py",   # engine(파일 동일)
            "A\t.claude/agents/new-agent.md",       # engine(디렉토리 prefix)
            "M\tREADME.md",                          # other(manifest 밖)
            "D\t.project_manager/wiki/status.md",   # other
        ],
    )
    s = pm_update.summarize_upstream_changes(source, "base000000", manifest, git_runner=runner)
    assert s["status"] == "ok"
    assert s["head"] == "abcdef1234567890"
    assert s["count"] == 2
    engine_paths = {p for _c, p in s["engine"]}
    other_paths = {p for _c, p in s["other"]}
    assert engine_paths == {
        ".project_manager/tools/board.py", ".claude/agents/new-agent.md"}
    assert other_paths == {"README.md", ".project_manager/wiki/status.md"}
    # 코드(M/A/D) 보존.
    assert ("M", ".project_manager/tools/board.py") in s["engine"]
    assert ("A", ".claude/agents/new-agent.md") in s["engine"]
    assert ("D", ".project_manager/wiki/status.md") in s["other"]


def test_summarize_head_equals_baseline_empty_log(pm_update, tmp_path):
    """HEAD==baseline(빈 log) → status='up_to_date'·count 0·변경 목록 빈."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools/board.py"])
    runner = _make_fake_git_runner(head="samehead0000", log_lines=[], diff_lines=[])
    s = pm_update.summarize_upstream_changes(source, "samehead0000", manifest, git_runner=runner)
    assert s["status"] == "up_to_date"
    assert s["count"] == 0
    assert s["engine"] == [] and s["other"] == []


def test_summarize_baseline_unreachable(pm_update, tmp_path):
    """baseline rev 도달불가(cat-file rc≠0·force-push/shallow) → status='baseline_unreachable'."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools/board.py"])
    runner = _make_fake_git_runner(baseline_reachable=False)
    s = pm_update.summarize_upstream_changes(source, "gonerev00000", manifest, git_runner=runner)
    assert s["status"] == "baseline_unreachable"
    # 도달불가면 log/diff 집계는 하지 않는다(early-return).
    assert s["count"] == 0
    assert s["engine"] == [] and s["other"] == []


def test_summarize_rename_code_first_letter_and_new_path(pm_update, tmp_path):
    """R(rename) name-status 는 `R100\\told\\tnew` 3필드 — 코드 첫글자 R·경로는 새 경로."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools/board.py"])
    runner = _make_fake_git_runner(
        head="h",
        log_lines=["aaa renamed tool"],
        diff_lines=["R100\t.project_manager/tools/old.py\t.project_manager/tools/board.py"],
    )
    s = pm_update.summarize_upstream_changes(source, "base", manifest, git_runner=runner)
    assert ("R", ".project_manager/tools/board.py") in s["engine"]


def test_summarize_empty_manifest_all_other(pm_update, tmp_path):
    """빈 manifest(둘 다 부재·fresh-adopter) → 변경 전부 'other'(graceful·엔진 영향 0 보수 표시)."""
    source = tmp_path / "src"
    source.mkdir()
    runner = _make_fake_git_runner(
        head="h", log_lines=["x commit"],
        diff_lines=["M\t.project_manager/tools/board.py"])
    s = pm_update.summarize_upstream_changes(source, "base", [], git_runner=runner)
    assert s["engine"] == []
    assert ("M", ".project_manager/tools/board.py") in s["other"]


def test_summarize_log_failure_surfaces(pm_update, tmp_path):
    """git log rc≠0(도달가능한데 호출 실패) → status='summary_failed'(빈 결과 오판 금지·suggestion 1)."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools/board.py"])

    def runner(argv):
        if "rev-parse" in argv:
            return 0, "headXXXXXXXX\n"
        if "cat-file" in argv:
            return 0, ""  # baseline 도달 가능
        if "log" in argv:
            return 128, "fatal: bad revision"  # log 호출 실패
        return 1, "unexpected"

    s = pm_update.summarize_upstream_changes(source, "base", manifest, git_runner=runner)
    assert s["status"] == "summary_failed"
    # 빈 결과를 "변경 0"으로 오판하지 않게 — count 0 이어도 status 로 실패를 구분한다.
    assert s["engine"] == [] and s["other"] == []


def test_summarize_diff_failure_surfaces(pm_update, tmp_path):
    """git log 는 성공했으나 diff --name-status rc≠0 → status='summary_failed'(엔진 영향 0 오판 금지)."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools/board.py"])

    def runner(argv):
        if "rev-parse" in argv:
            return 0, "headXXXXXXXX\n"
        if "cat-file" in argv:
            return 0, ""
        if "log" in argv:
            return 0, "abc1234 commit one\n"  # log 성공(commit 1)
        if "diff" in argv:
            return 128, "fatal: diff failed"  # diff 호출 실패
        return 1, "unexpected"

    s = pm_update.summarize_upstream_changes(source, "base", manifest, git_runner=runner)
    assert s["status"] == "summary_failed"


# ── main --changes end-to-end (fake _real_upstream_git_runner 주입·라이브 git 0) ──

def _patch_upstream_runner(pm_update, monkeypatch, runner):
    """_run_changes 가 git_runner 미주입으로 호출하므로 pm_import._real_upstream_git_runner 를 stub.

    summarize_upstream_changes(source, baseline, manifest) (git_runner 없음) → _load_pm_import().
    _real_upstream_git_runner() 를 부른다. 그 팩토리를 fake runner 반환으로 바꿔 라이브 git 0.
    """
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "_real_upstream_git_runner", lambda: runner)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)


def test_changes_main_normal_three_blocks(pm_update, tmp_path, monkeypatch, capsys):
    """--changes: baseline..HEAD 3블록(헤더·엔진 영향·그 외) 출력·실 sync 안 함."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py", ".claude/agents"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="head9876543210",
        log_lines=["abc1234 fix", "def5678 feat"],
        diff_lines=[
            "M\t.project_manager/tools/board.py",
            "A\t.claude/agents/x.md",
            "M\tCHANGELOG.md",
        ],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "baseline base12345678" in out
    assert "HEAD head98765432" in out  # 12자 절단
    assert "2 commits" in out
    # 엔진 영향 2 / 그 외 1 분리.
    assert "엔진 영향" in out and "2 files" in out
    assert "M .project_manager/tools/board.py" in out
    assert "A .claude/agents/x.md" in out
    assert "그 외 변경" in out and "1 files" in out
    # 실 sync 안 함 — source 파일을 fake_repo 로 복사하지 않았다(엔진 영향 로그는 board.py 만 언급).
    assert not (fake_repo / ".project_manager" / "tools" / "board.py").exists()


def test_changes_main_count_only(pm_update, tmp_path, monkeypatch, capsys):
    """--changes --count-only: commit 개수 1줄만."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="h", log_lines=["a x", "b y", "c z"], diff_lines=["M\tREADME.md"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes", "--count-only"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "3", f"count-only 가 개수 1줄이 아님: {out!r}"


def test_changes_main_log_tail(pm_update, tmp_path, monkeypatch, capsys):
    """--changes --log: git log --oneline 커밋 목록을 꼬리에 출력."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="h",
        log_lines=["abc1234 first commit", "def5678 second commit"],
        diff_lines=["M\t.project_manager/tools/board.py"],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes", "--log"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "abc1234 first commit" in out
    assert "def5678 second commit" in out


def test_changes_main_baseline_unrecorded_graceful(pm_update, tmp_path, monkeypatch, capsys):
    """baseline(upstream_rev) 미기록 → graceful 안내·exit 0(요약 생략·다음 sync 후 추적)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    # upstream 은 있으나 upstream_rev 없음.
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "baseline 미기록" in out
    assert "다음" in out and "sync" in out


def test_changes_main_head_equals_baseline(pm_update, tmp_path, monkeypatch, capsys):
    """HEAD==baseline(변경 0) → '변경 0·최신' 안내·exit 0."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=samerev00000\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(head="samerev00000", log_lines=[], diff_lines=[])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "변경 0" in out and "최신" in out


def test_changes_main_baseline_unreachable(pm_update, tmp_path, monkeypatch, capsys):
    """baseline rev 도달불가(cat-file rc≠0) → 재clone 권고·exit 0."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=gonerev00000\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(baseline_reachable=False)
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "도달 불가" in out
    assert "재clone" in out or "재 clone" in out


def test_changes_main_url_upstream_early_error(pm_update, tmp_path, monkeypatch, capsys):
    """--changes + URL upstream + --from 생략 → 명확 에러로 멈춤(rc≠0·D5: git clone/fetch 안 함).

    엔진은 로컬 checkout 만 read 한다 — URL freshness 는 스킬층(T-0147) 소관. _run_changes 가
    sync 와 같은 _resolve_dest_source 를 타므로 URL 게이트를 동일하게 거친다.
    """
    fake_repo = tmp_path / "fake_repo"
    _write_local_conf(
        fake_repo, "upstream=https://github.com/acme/proj.git\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--changes"])
    assert rc == 1, "URL upstream 인데 명확 에러로 안 멈춤(D5 경계 위반)."
    err = capsys.readouterr().err
    assert "URL" in err and ("pm-update" in err or "--from" in err)


def test_changes_main_url_upstream_explicit_local_from_works(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf 가 URL 이어도 --changes --from <로컬 checkout> 명시면 동작(URL 게이트 우회).

    URL 게이트는 stored upstream 해소 분기 한정 — 명시 --from(로컬)은 그 분기를 안 탄다(sync 동형).
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(
        fake_repo, "upstream=https://github.com/acme/proj.git\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="h", log_lines=["a x"], diff_lines=["M\t.project_manager/tools/board.py"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes", "--from", str(source)])
    captured = capsys.readouterr()
    assert rc == 0, f"명시 --from(로컬)인데 URL 게이트가 막음: {captured.err!r}"
    assert "엔진 영향" in captured.out


def test_changes_does_not_sync(pm_update, tmp_path, monkeypatch, capsys):
    """--changes 는 read-only — apply 를 절대 부르지 않는다(실 복사 0·부작용 0)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    sentinel = source / ".project_manager" / "tools" / "board.py"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# real engine file\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    called = {"apply": False}
    monkeypatch.setattr(pm_update, "apply", lambda *a, **k: called.__setitem__("apply", True))

    runner = _make_fake_git_runner(
        head="h", log_lines=["a x"], diff_lines=["M\t.project_manager/tools/board.py"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    assert called["apply"] is False, "--changes 가 apply 를 불렀다(read-only 위반)."
    # source 의 board.py 가 dest(fake_repo)로 복사되지 않았다.
    assert not (fake_repo / ".project_manager" / "tools" / "board.py").exists()


# ── codex MF: 엔진 영향 분류 manifest = sync 와 동일(dest 우선·없으면 source) ──

def test_changes_uses_dest_manifest_when_present(pm_update, tmp_path, monkeypatch, capsys):
    """dest manifest ≠ source manifest → **dest manifest 가 권위**(실 sync 와 일치·codex MF).

    실 sync 는 resolve_manifest_for_dest(dest 우선)로 "무엇이 엔진인가"를 정한다. --changes 의
    "엔진 영향(이번 동기가 받는 것)"도 같은 manifest 를 써야 어긋나지 않는다. source manifest 는
    board.py 만 엔진이라 하고, dest manifest 는 .claude/agents 만 엔진이라 하는 상충 상황에서,
    분류가 *dest* 를 따라야 함을 단언(source 로 분류하던 잘못을 잡는 sensitivity).
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    # source manifest: board.py 만 엔진.
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    # dest(fake_repo) manifest: .claude/agents 만 엔진(source 와 상충).
    (fake_repo / ".project_manager").mkdir(parents=True, exist_ok=True)
    (fake_repo / ".project_manager" / "engine.manifest").write_text(
        ".claude/agents\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="head0000",
        log_lines=["abc1234 c1"],
        diff_lines=[
            "M\t.project_manager/tools/board.py",   # source 면 engine·dest 면 other
            "A\t.claude/agents/x.md",               # dest 면 engine·source 면 other
        ],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    # dest manifest 권위 → .claude/agents 가 엔진 영향, board.py 는 그 외.
    assert "엔진 영향 (manifest 경로·이번 동기가 받는 것): 1 files" in out
    assert "A .claude/agents/x.md" in out
    assert "그 외 변경 (manifest 밖·동기 안 받음): 1 files" in out
    # board.py 가 엔진 영향 목록에 떠선 안 된다(source manifest 로 분류했다는 증거).
    engine_block = out.split("엔진 영향")[1].split("그 외 변경")[0]
    assert ".project_manager/tools/board.py" not in engine_block, \
        "source manifest 로 분류함(dest 권위 위반·codex MF 회귀)."


def test_changes_falls_back_to_source_manifest_when_no_dest(pm_update, tmp_path, monkeypatch, capsys):
    """dest manifest 부재(fresh-adopter 직전·dest 미생성) → source manifest 로 분류(graceful fallback).

    resolve_manifest_for_dest 의 dest 우선·없으면 source 폴백을 --changes 도 그대로 상속한다.
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    # dest 에는 engine.manifest 없음(local.conf 만) → source manifest 가 폴백 권위.
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    runner = _make_fake_git_runner(
        head="h", log_lines=["a x"],
        diff_lines=["M\t.project_manager/tools/board.py", "M\tREADME.md"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "엔진 영향 (manifest 경로·이번 동기가 받는 것): 1 files" in out
    assert "M .project_manager/tools/board.py" in out
    assert "그 외 변경 (manifest 밖·동기 안 받음): 1 files" in out


# ── codex suggestion 1: 집계 실패 surface (빈 결과 → "변경 0" 오판 금지) ──────

def test_changes_main_summary_failed_surfaces(pm_update, tmp_path, monkeypatch, capsys):
    """log/diff git 호출 rc≠0(요약 불가) → stderr 안내·exit 0(변경 0 오판 금지·suggestion 1)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\nupstream_rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    def runner(argv):
        if "rev-parse" in argv:
            return 0, "head0000\n"
        if "cat-file" in argv:
            return 0, ""  # baseline 도달 가능(unreachable 과 구분)
        if "log" in argv:
            return 128, "fatal: bad revision"  # 집계 실패
        return 1, "unexpected"

    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0  # read-only 안내 — 진행은 막지 않되 명확히 surface.
    captured = capsys.readouterr()
    assert "집계 실패" in captured.err or "요약 불가" in captured.err, \
        "집계 실패가 surface 되지 않음(변경 0 오판 위험·suggestion 1)."
    # "변경 0·최신" 같은 오판 메시지가 stdout 에 뜨면 안 된다.
    assert "변경 0" not in captured.out


# ── codex suggestion 2: --count-only/--log 는 --changes 전용 (CLI 오사용 차단) ──

def test_count_only_without_changes_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--count-only 를 --changes 없이 주면 명확 에러(rc≠0) — 조용한 무시·일반 sync 진행 금지."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--count-only"])
    assert rc == 1, "--count-only 가 --changes 없이도 통과(일반 sync 진행·조용한 무시)."
    err = capsys.readouterr().err
    assert "--count-only" in err and "--changes" in err


def test_log_without_changes_errors(pm_update, tmp_path, monkeypatch, capsys):
    """--log 를 --changes 없이 주면 명확 에러(rc≠0) — 조용한 무시 금지."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--log"])
    assert rc == 1, "--log 가 --changes 없이도 통과(조용한 무시)."
    err = capsys.readouterr().err
    assert "--log" in err and "--changes" in err


# ════════════════════════════════════════════════════════════════════════
# T-0169 — board-분리 인지 template dest 리매핑 (ADR-0033 ① · 실 발생 버그 reconcile)
# ════════════════════════════════════════════════════════════════════════
# manifest 는 `wiki/tickets/_template.md`(① canonical·legacy 실 위치)를 들고 있다. board 가
# `.project_manager/board/`(submodule)로 분리된 adopter 에선 _template.md 가
# `board/tickets/_template.md` 에 산다(board_root() 추종). pm_update 가 dest 를 board_root 로
# 리매핑하지 않으면 매 sync 가 `wiki/tickets/_template.md` 를 부활시킨다(dead cruft·drift).
# 아래는 양 형상 hermetic 검증 — board-분리 시 board/, legacy 시 wiki/ (회귀 무영향).

_TEMPLATE_REL = ".project_manager/wiki/tickets/_template.md"
_BOARD_TEMPLATE_REL = ".project_manager/board/tickets/_template.md"


def _make_board_separated(dest_root: Path) -> None:
    """dest 를 board-분리 형상으로 — `.project_manager/board/tickets/{open,...}` 생성.

    board_root()/`_is_board_separated` 가 *실측*(board/tickets 가 dir 인가)으로 가르므로,
    그 디렉토리를 만들어 분리 adopter 를 모사한다(test_board_root._make_board_dir 동형)."""
    board_dir = dest_root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)


def _make_template_upstream(root: Path) -> None:
    """upstream(source) — manifest 에 wiki/tickets/_template.md 를 엔진으로 등재 + 실 파일.

    ① canonical 형상: upstream 은 항상 `wiki/tickets/_template.md` 에 템플릿을 들고 있다
    (board-분리는 *dest* 형상이지 upstream 형상이 아니다). source 는 wiki/ 에서 그대로 읽힌다."""
    tmpl = root / _TEMPLATE_REL
    tmpl.parent.mkdir(parents=True, exist_ok=True)
    tmpl.write_text("# ticket 본문 템플릿 (upstream)\n", encoding="utf-8")
    (root / ".project_manager" / "engine.manifest").write_text(
        _TEMPLATE_REL + "\n", encoding="utf-8")


# ── 단위: _is_board_separated 실측 판별 (board.py board_root 동형) ────────────

def test_is_board_separated_false_when_no_board_dir(pm_update, tmp_path):
    """board/tickets 부재(legacy·솔로) → _is_board_separated == False."""
    assert pm_update._is_board_separated(tmp_path) is False


def test_is_board_separated_true_when_board_tickets_dir(pm_update, tmp_path):
    """`.project_manager/board/tickets` 가 dir → _is_board_separated == True (분리 형상)."""
    _make_board_separated(tmp_path)
    assert pm_update._is_board_separated(tmp_path) is True


def test_is_board_separated_ignores_bare_board_without_tickets(pm_update, tmp_path):
    """board/ 만 있고 tickets/ 가 없으면 legacy 로 본다 (board_root 의 graceful 실측 동형)."""
    (tmp_path / ".project_manager" / "board").mkdir(parents=True)
    assert pm_update._is_board_separated(tmp_path) is False


# ── 단위: _dest_relpath_for 리매핑 (template 한정·분리 시만) ──────────────────

def test_dest_relpath_remaps_template_when_separated(pm_update, tmp_path):
    """board-분리 dest → wiki/tickets/_template.md 가 board/tickets/_template.md 로 리매핑."""
    _make_board_separated(tmp_path)
    assert pm_update._dest_relpath_for(_TEMPLATE_REL, tmp_path) == _BOARD_TEMPLATE_REL


def test_dest_relpath_keeps_template_in_wiki_when_legacy(pm_update, tmp_path):
    """legacy dest(board/ 미분리) → 종전 wiki/tickets/_template.md 유지(무변경·회귀)."""
    assert pm_update._dest_relpath_for(_TEMPLATE_REL, tmp_path) == _TEMPLATE_REL


def test_dest_relpath_passthrough_other_paths_even_when_separated(pm_update, tmp_path):
    """리매핑은 _template.md 한정 — 다른 엔진 경로는 board-분리 dest 여도 입력 그대로(무변경)."""
    _make_board_separated(tmp_path)
    other = ".project_manager/tools/board.py"
    assert pm_update._dest_relpath_for(other, tmp_path) == other


def test_dest_relpath_normalizes_windows_separators(pm_update, tmp_path):
    """relpath 에 `\\`(Windows _iter_files str(Path))가 섞여도 posix-normalize 후 리매핑한다."""
    _make_board_separated(tmp_path)
    win_rel = _TEMPLATE_REL.replace("/", "\\")
    assert pm_update._dest_relpath_for(win_rel, tmp_path) == _BOARD_TEMPLATE_REL


# ── plan 레벨: 양 형상 dst 목적지 (실 복사 없이) ──────────────────────────────

def test_plan_writes_template_to_board_when_separated(pm_update, tmp_path):
    """board-분리 dest → plan 의 template change dst 가 board/tickets/_template.md (wiki/ 부활 0)."""
    source = tmp_path / "upstream"
    dest = tmp_path / "dest"
    _make_template_upstream(source)
    _make_board_separated(dest)

    manifest = pm_update.read_manifest(source / ".project_manager" / "engine.manifest")
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert missing == []
    rels = [c[0] for c in changes]
    assert _BOARD_TEMPLATE_REL in rels, f"board-분리 dest 인데 board 경로로 plan 안 됨: {rels}"
    assert _TEMPLATE_REL not in rels, f"board-분리 dest 인데 wiki/ 경로를 부활(drift): {rels}"
    # dst 절대경로도 board/ 안을 가리킨다(source 는 upstream wiki/ 에서 그대로 읽힘).
    tmpl_change = next(c for c in changes if c[0] == _BOARD_TEMPLATE_REL)
    _r, sp, dst, _kind = tmpl_change
    assert Path(dst) == dest / _BOARD_TEMPLATE_REL
    assert Path(sp) == source / _TEMPLATE_REL, "source 는 upstream wiki/ 에서 읽어야 한다(dest 만 옮김)."


def test_plan_writes_template_to_wiki_when_legacy(pm_update, tmp_path):
    """legacy dest(board/ 미분리) → plan 의 template change dst 가 wiki/tickets/_template.md (종전)."""
    source = tmp_path / "upstream"
    dest = tmp_path / "dest"
    _make_template_upstream(source)
    (dest / ".project_manager").mkdir(parents=True)  # board/ 없음 = legacy

    manifest = pm_update.read_manifest(source / ".project_manager" / "engine.manifest")
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert missing == []
    rels = [c[0] for c in changes]
    assert _TEMPLATE_REL in rels, f"legacy dest 인데 종전 wiki/ 경로로 plan 안 됨(회귀): {rels}"
    assert _BOARD_TEMPLATE_REL not in rels, f"legacy dest 인데 board/ 로 옮김(오리매핑): {rels}"
    tmpl_change = next(c for c in changes if c[0] == _TEMPLATE_REL)
    assert Path(tmpl_change[2]) == dest / _TEMPLATE_REL


def test_plan_template_already_at_board_no_change(pm_update, tmp_path):
    """board-분리 dest 에 board/tickets/_template.md 가 이미 동일 내용이면 update 없음(no-op).

    리매핑된 dst 와 비교하므로 board 위치의 동일 파일은 변경 0 — 매 sync 가 무변경(idempotent)."""
    source = tmp_path / "upstream"
    dest = tmp_path / "dest"
    _make_template_upstream(source)
    _make_board_separated(dest)
    # board 위치에 upstream 과 동일 내용 선배치.
    board_tmpl = dest / _BOARD_TEMPLATE_REL
    board_tmpl.write_text((source / _TEMPLATE_REL).read_text(encoding="utf-8"), encoding="utf-8")

    manifest = pm_update.read_manifest(source / ".project_manager" / "engine.manifest")
    changes, missing = pm_update.plan(source, manifest, dest_root=dest)

    assert missing == []
    assert changes == [], f"board 위치에 동일 템플릿이 있는데 변경이 떴다(idempotent 깨짐): {changes}"


# ── e2e main --dry-run: 실 발생 버그 (board-분리 adopter 가 wiki/ 부활) ────────

def test_dry_run_board_separated_does_not_revive_wiki_template(pm_update, tmp_path, monkeypatch, capsys):
    """board-분리 adopter 의 `pm_update --dry-run` 이 [new] wiki/tickets/_template.md 를 안 올린다.

    이게 실 발생 버그(drift-0 DoD): board-분리 인스턴스에서 매 sync 가 wiki/tickets/_template.md
    를 부활(`[new]`)시키던 것을 reconcile — 표기는 board/tickets/_template.md 로 정직히 나온다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    _make_template_upstream(source)
    _make_board_separated(fake_repo)
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _TEMPLATE_REL not in out, \
        f"board-분리 adopter 가 wiki/tickets/_template.md 를 부활시킴(drift·실 버그 재발): {out!r}"
    assert _BOARD_TEMPLATE_REL in out, \
        f"board-분리 dest 의 template 이 board/ 경로로 표기되지 않음: {out!r}"


def test_dry_run_legacy_ships_wiki_template(pm_update, tmp_path, monkeypatch, capsys):
    """legacy adopter 의 `pm_update --dry-run` 은 종전대로 wiki/tickets/_template.md 를 ship(회귀)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    _make_template_upstream(source)
    (fake_repo / ".project_manager").mkdir(parents=True)  # board/ 없음 = legacy
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _TEMPLATE_REL in out, \
        f"legacy adopter 가 종전 wiki/tickets/_template.md 를 ship 안 함(회귀): {out!r}"
    assert _BOARD_TEMPLATE_REL not in out, \
        f"legacy adopter 인데 board/ 경로로 표기(오리매핑): {out!r}"


def test_apply_board_separated_writes_template_into_board(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(apply) — board-분리 dest 에 board/tickets/_template.md 가 쓰이고 wiki/ 는 안 만들어진다.

    dry-run 표기뿐 아니라 실제 파일 IO 도 board/ 안으로 가는지(그리고 wiki/ 부활 0) 검증."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "upstream"
    _make_template_upstream(source)
    _make_board_separated(fake_repo)
    _write_local_conf(fake_repo, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    # baseline rev 기록은 본 테스트 무관 — 라이브 git 없이 graceful 생략되게 stub None.
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])  # 실 sync.
    assert rc == 0
    board_tmpl = fake_repo / _BOARD_TEMPLATE_REL
    wiki_tmpl = fake_repo / _TEMPLATE_REL
    assert board_tmpl.exists(), "board-분리 sync 후 board/tickets/_template.md 가 안 쓰임."
    assert board_tmpl.read_text(encoding="utf-8") == \
        (source / _TEMPLATE_REL).read_text(encoding="utf-8")
    assert not wiki_tmpl.exists(), "board-분리 sync 가 wiki/tickets/_template.md 를 부활(drift)."


# ── T-0305: engine-mirror hook/driver self-update 전파 e2e (frozen 근절 실증) ──────────────
# engine safety-훅(ctx-stop·회귀 게이트)이 manifest **밖**이면 채택자는 import 시점 frozen 사본을
# 영영 유지해 엔진 fix 가 영영 안 갔다(stale 신호도 없음). 이제 hook 이 manifest 안(@source remap·
# root-sourced)이라 pm_update self-update 가 엔진 변경을 채택자 dest 로 전파한다. plan/apply 로 실
# apply 착지를 검증한다 — "엔진 safety-훅 변경이 채택자에 도달"(frozen 근절 DoD).


def test_self_update_propagates_engine_safety_hook_via_source_remap(pm_update, tmp_path):
    """@source hook 엔트리: 엔진(ship 템플릿) safety-훅 변경이 채택자 dest 로 전파(frozen 근절·H1).

    upstream(프레임워크 루트)의 canonical 소스는 templates/claude_code/.claude/ctx_stop_hook.sh 에
    있고, manifest 엔트리 `.claude/ctx_stop_hook.sh @source=templates/claude_code/.claude/ctx_stop_hook.sh`
    가 그 소스를 채택자 dest `.claude/ctx_stop_hook.sh` 로 remap 한다. 엔진에서 훅을 고치면 pm_update
    self-update 가 채택자의 frozen(import 시점 동결) 사본을 엔진 NEW 로 덮어쓴다 — 이게 프레임워크 자산이
    채택자에 닿는 유일한 채널(과거엔 manifest-out 이라 채널 자체가 없었다)."""
    upstream = tmp_path / "framework"
    adopter = tmp_path / "adopter"
    src_rel = "templates/claude_code/.claude/ctx_stop_hook.sh"
    dst_rel = ".claude/ctx_stop_hook.sh"

    # upstream canonical 소스 = 엔진 NEW safety fix (예: hard-stop 임계 수정).
    src = upstream / src_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("#!/bin/sh\n# NEW hard-stop safety fix\nexit 0\n", encoding="utf-8")

    # 채택자의 frozen 사본 = OLD (엔진 fix 이전·import 시점 동결).
    frozen = adopter / dst_rel
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text("#!/bin/sh\n# OLD frozen hook\nexit 0\n", encoding="utf-8")

    # 실 manifest 엔트리(@source remap) — read_manifest 파싱을 거친 ManifestEntry.
    entries = _manifest_entries(pm_update, [f"{dst_rel}    @source={src_rel}"])

    # plan: dest=adopter·render_enabled=True(hook 은 .sh → @render 없어도 byte-copy). @source 라
    #   source_root/src_rel 읽고 dest 엔 dst_rel 로 기록(remap·root 어댑터엔 ctx 훅 부재 비대칭 해소).
    changes, missing = pm_update.plan(upstream, entries, dest_root=adopter, render_enabled=True)
    assert not missing, f"@source 소스가 upstream 에 실재하는데 missing(rc2 위험): {missing}"
    assert [c[0] for c in changes] == [dst_rel], f"dest remap 경로 불일치: {[c[0] for c in changes]}"

    # apply → 채택자 frozen 사본이 엔진 NEW 로 덮인다(전파 착지·frozen 근절 실증).
    pm_update.apply(changes)
    landed = frozen.read_text(encoding="utf-8")
    assert "NEW hard-stop safety fix" in landed, "엔진 safety-훅 변경이 채택자에 미전파(frozen 잔존)"
    assert "OLD frozen hook" not in landed, "채택자 frozen 사본이 엔진 NEW 로 덮이지 않음"


def test_self_update_root_sourced_hook_propagates(pm_update, tmp_path):
    """root-sourced(bare) hook(run_tests_hook.sh): 루트 `.claude/` 실재분도 self-update 로 전파.

    ctx 훅은 @source(ship 템플릿) 이나 run_tests_hook.sh 는 루트 `.claude/` 에 byte-identical 로
    실재라 bare 등록(agents/skills 동형). bare 엔트리는 source_root/<rel> 을 그대로 읽어 dest 로 복사한다."""
    upstream = tmp_path / "framework"
    adopter = tmp_path / "adopter"
    rel = ".claude/run_tests_hook.sh"

    src = upstream / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("#!/bin/sh\n# NEW regression gate\nexit 0\n", encoding="utf-8")
    frozen = adopter / rel
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text("#!/bin/sh\n# OLD gate\nexit 0\n", encoding="utf-8")

    entries = _manifest_entries(pm_update, [rel])  # bare (source_rel None → 루트 상대 = rel)
    changes, missing = pm_update.plan(upstream, entries, dest_root=adopter, render_enabled=True)
    assert not missing
    assert [c[0] for c in changes] == [rel]
    pm_update.apply(changes)
    assert "NEW regression gate" in frozen.read_text(encoding="utf-8")


def test_source_remapped_hook_missing_source_reports_rc2_class(pm_update, tmp_path):
    """@source hook 의 canonical 소스가 upstream 에 진짜 부재면 missing 으로 잡힌다(non-@target-owned).

    @source 는 source 가 templates/ 아래 *실재* 함을 전제하므로 부재 = 엔진/템플릿 누락(오타·불완전
    checkout)이지 target-owned skip 이 아니다 → main 이 rc2 로 막는다(누락 은폐 금지·안전판). 여기선
    plan 레벨에서 missing 에 실림을 확인(main rc2 경로의 입력)."""
    upstream = tmp_path / "framework"   # templates/ 없음 (불완전 checkout)
    adopter = tmp_path / "adopter"
    dst_rel = ".claude/ctx_guard.py"
    src_rel = "templates/claude_code/.claude/ctx_guard.py"
    entries = _manifest_entries(pm_update, [f"{dst_rel}    @source={src_rel}"])
    changes, missing = pm_update.plan(upstream, entries, dest_root=adopter, render_enabled=True)
    assert changes == []
    assert missing == [dst_rel], f"@source 소스 부재가 missing(rc2 입력)으로 안 잡힘: {missing}"
    # @source 엔트리는 target_owned=False → main 이 skip 아닌 rc2(엔진 누락).
    assert not any(pm_update._entry_target_owned_flag(e) for e in entries)


# ── T-0305: manifest 자기전파 flavor 보존 e2e (self-prop 이 flavor 매니페스트 clobber 안 함) ──────
# self-prop 엔트리(`.project_manager/engine.manifest`)가 3 flavor(root·claude_code·opencode)에 모두
# 있으므로, `--target <flavor>` sync 가 flavor 매니페스트를 *자기 자신*(claude→claude·op→op)에서
# 읽어야(=@source remap + resolve_manifest_for_dest dest-우선) 한 flavor 가 다른 flavor 를 덮지 않는다.
# 만약 claude_code self-prop 이 bare(@source 없음)면 `--target claude_code` 가 source 루트 매니페스트
# (root flavor)를 읽어 claude 매니페스트를 clobber → flavor 오염. 그 회귀를 e2e 로 박제한다.


def _make_flavor_manifest_repo(root: Path) -> None:
    """fake REPO — root·claude_code·opencode 각 flavor 매니페스트(자기전파 엔트리 + 고유 FLAVOR 마커).

    각 매니페스트는 self-prop 1줄만 담아 격리한다. root 는 bare(자기 flavor), 템플릿은
    `@source=templates/<flavor>/...` 로 자기 flavor 를 읽는다(clobber 방지의 핵심)."""
    specs = {
        root / ".project_manager" / "engine.manifest":
            "# FLAVOR: root\n.project_manager/engine.manifest\n",
        root / "templates" / "claude_code" / ".project_manager" / "engine.manifest":
            "# FLAVOR: claude\n.project_manager/engine.manifest    "
            "@source=templates/claude_code/.project_manager/engine.manifest\n",
        root / "templates" / "opencode" / ".project_manager" / "engine.manifest":
            "# FLAVOR: opencode\n.project_manager/engine.manifest    "
            "@source=templates/opencode/.project_manager/engine.manifest\n",
    }
    for path, text in specs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _stub_no_baseline_git(pm_update, monkeypatch) -> None:
    """baseline rev 기록을 라이브 git 없이 graceful 생략(read_upstream_rev→None)."""
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)


@pytest.mark.parametrize("target,flavor,foreign", [
    ("claude_code", "claude", "root"),
    ("opencode", "opencode", "root"),
])
def test_self_prop_target_sync_preserves_flavor_manifest(
        pm_update, tmp_path, monkeypatch, target, flavor, foreign):
    """`--target <flavor>` 가 flavor 매니페스트를 자기 flavor 로 유지(root/타 flavor 로 clobber 안 함).

    self-prop 이 `@source=templates/<flavor>/...` + resolve_manifest_for_dest(dest 우선)라 flavor
    매니페스트를 자기 자신에서 읽는다(self no-op) — bare 였다면 source 루트(root flavor)를 읽어 덮었다."""
    fake_repo = tmp_path / "framework"
    _make_flavor_manifest_repo(fake_repo)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    _stub_no_baseline_git(pm_update, monkeypatch)

    dest_manifest = fake_repo / "templates" / target / ".project_manager" / "engine.manifest"
    before = dest_manifest.read_text(encoding="utf-8")

    rc = pm_update.main(["--from", str(fake_repo), "--target", target])
    assert rc == 0, f"--target {target} sync 실패 (rc={rc})"

    after = dest_manifest.read_text(encoding="utf-8")
    assert f"FLAVOR: {flavor}" in after, (
        f"--target {target} 후 {flavor} 매니페스트의 flavor 마커 소실(clobber?): {after!r}")
    assert f"FLAVOR: {foreign}" not in after, (
        f"--target {target} 가 {foreign} flavor 매니페스트로 {flavor} 를 clobber(self-prop remap 실패): {after!r}")
    assert after == before, "flavor 매니페스트가 자기전파에서 변경됨(self no-op 이어야)"


def test_self_prop_self_update_preserves_root_flavor(pm_update, tmp_path, monkeypatch):
    """self-update(--target 없음)는 root(bare) self-prop 으로 root flavor 매니페스트를 유지(② adopter#0)."""
    fake_repo = tmp_path / "framework"
    _make_flavor_manifest_repo(fake_repo)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    _stub_no_baseline_git(pm_update, monkeypatch)
    _write_local_conf(fake_repo, f"upstream={fake_repo}\n")

    root_manifest = fake_repo / ".project_manager" / "engine.manifest"
    before = root_manifest.read_text(encoding="utf-8")
    rc = pm_update.main([])  # self-location: dest=REPO=fake_repo.
    assert rc == 0
    after = root_manifest.read_text(encoding="utf-8")
    assert "FLAVOR: root" in after and "FLAVOR: claude" not in after and "FLAVOR: opencode" not in after
    assert after == before, "root self-update 가 root 매니페스트를 변경(self no-op 이어야)"


# ════════════════════════════════════════════════════════════════════════
# 보호 훅 전수 재설치 트리거 (T-0415·ADR-0071)
# ════════════════════════════════════════════════════════════════════════
# 보호 훅(`.local/repo-hooks/<repo>/pre-push`·`pre-commit`)은 엔진 코드에서 *생성*되는 런타임
# 산출물이다 — 엔진 파일이 복사돼도 **재설치가 돌아야** 새 훅이 디스크에 놓인다. 설치 트리거가
# `repo add`/`worktree add` 뿐이면 엔진 업그레이드만 한 채택자는 새 훅을 영영 못 받는다(값-연결
# 단절). 아래는 그 연결을 assert 한다 — dest 엔진 실사본 + 실 bare 로 *진짜 파일*을 본다.

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")

_AREAS_ONE_REPO = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| svc | SVC | git@x:svc.git | pytest -q | me | main | main,release | me |\n"
)


def _make_pm_home(dest_root: Path, *, areas: str | None = _AREAS_ONE_REPO,
                  bare_repos: tuple[str, ...] = ("svc",)) -> Path:
    """dest 를 **PM 홈 형상**으로 만든다 — 실 엔진 tools 사본 + areas 레지스트리 + 실 bare 미러.

    재설치는 dest 엔진(`pm_config`→`board`/`worktree_pool`)을 로드해 돌므로 tools 는 실 사본이어야
    한다(형제 rev 스탬프도 함께 맞는다·T-0397). bare 는 `git init --bare` 실물 — `core.hooksPath`
    설정이 rc0 이어야 설치가 성공(True)으로 보고된다."""
    tools = dest_root / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TOOLS, tools)
    if areas is not None:
        (dest_root / ".project_manager" / "areas.md").write_text(areas, encoding="utf-8")
    for repo in bare_repos:
        bare = dest_root / ".repos" / f"{repo}.git"
        bare.mkdir(parents=True, exist_ok=True)
        subprocess.run([_GIT, "init", "--bare", "-q", str(bare)], check=True,
                       capture_output=True, text=True)
    return dest_root


def _hook_dir(dest_root: Path, repo: str) -> Path:
    return dest_root / ".project_manager" / ".local" / "repo-hooks" / repo


@_git_required
def test_reinstall_protected_hooks_installs_both_hooks(pm_update, tmp_path, capsys):
    """등록 repo 전수 재설치 — pre-push + **pre-commit** 훅과 sidecar 가 디스크에 놓인다 (T-0415).

    이게 이 티켓의 값-연결이다: 엔진 업그레이드(파일 복사)만으론 훅이 안 바뀐다 — 재설치가
    dest 의 *새* worktree_pool 훅 본문을 깔아야 채택자에게 도달한다."""
    dest = _make_pm_home(tmp_path / "home")
    result = pm_update.reinstall_protected_hooks(dest, write=True)

    assert result["status"] == "done"
    assert result["targets"] == ["svc"] and result["drifted"] == ["svc"]
    assert result["in_sync"] == [] and result["failed"] == []
    hooks = _hook_dir(dest, "svc")
    assert (hooks / "pre-push").exists(), "pre-push 훅 미설치"
    commit_hook = hooks / "pre-commit"
    assert commit_hook.exists(), "pre-commit 훅이 sync 후 재설치로 배포되지 않음(T-0415 값-연결 단절)"
    assert "PM_ALLOW_PROTECTED_COMMIT" in commit_hook.read_text(encoding="utf-8")
    # 보호목록은 areas.md 권위(`main,release`)를 그대로 sidecar 로 — pm_update 가 목록을 재해소하지 않는다.
    assert (hooks / "protected").read_text(encoding="utf-8").splitlines() == ["main", "release"]
    # 설치 보고는 pm_config 깔때기를 탄다(성공 ✓ 1줄·조용하지 않다).
    assert "✓ 보호 브랜치 pre-push + pre-commit 훅 (재)설치: svc" in capsys.readouterr().out


@_git_required
def test_reinstall_protected_hooks_dry_run_writes_nothing(pm_update, tmp_path):
    """`write=False`(dry-run) — 판정만 하고 훅 파일은 쓰지 않는다 (무부작용)."""
    dest = _make_pm_home(tmp_path / "home")
    result = pm_update.reinstall_protected_hooks(dest, write=False)
    assert result["status"] == "done" and result["drifted"] == ["svc"]
    assert not _hook_dir(dest, "svc").exists(), "dry-run 이 훅을 실제로 설치했다"


@_git_required
def test_reinstall_protected_hooks_bare_absent_is_not_failure(pm_update, tmp_path, capsys):
    """등록됐지만 bare 미러가 없는 repo 는 `no_bare` — 실패 경고가 아니라 요약 1줄 (T-0415).

    게이트할 미러가 없으면 훅도 무의미하다(install 이 no-op). 매 sync 마다 실패 경고를 울리면
    진짜 실패가 묻힌다 — 그렇다고 침묵하지도 않는다(요약 1줄로 surface)."""
    dest = _make_pm_home(tmp_path / "home", bare_repos=())
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["status"] == "done"
    assert result["targets"] == [] and result["drifted"] == []
    assert result["failed"] == [] and result["no_bare"] == ["svc"]
    pm_update._print_protected_hook_reinstall_finding(result, dry_run=False)
    cap = capsys.readouterr()
    assert "bare" in cap.out and "svc" in cap.out
    assert cap.err == "", "bare 부재는 실패가 아니다(경고로 울리면 진짜 실패가 묻힌다)"


@_git_required
def test_reinstall_protected_hooks_no_registered_repos_is_quiet(pm_update, tmp_path, capsys):
    """등록 repo 0(솔로·미등록) → `no_repos`·완전 무출력 (걸 대상 없음 = 정상)."""
    dest = _make_pm_home(tmp_path / "home", areas=None, bare_repos=())
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["status"] == "no_repos"
    pm_update._print_protected_hook_reinstall_finding(result, dry_run=False)
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


def test_reinstall_protected_hooks_engine_absent_is_fail_soft_loud(pm_update, tmp_path, capsys):
    """dest 에 엔진이 없으면 `unavailable` + **stderr 경고**(침묵 무력화 금지·rc 무영향).

    sync 는 이미 성공했으므로 예외를 던져 update 를 깨면 안 되지만, 훅이 옛 본문으로 남았다는
    사실은 반드시 보여야 한다(재설치 커맨드 포함)."""
    dest = tmp_path / "no-engine"
    dest.mkdir()
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["status"] == "unavailable" and "pm_config.py" in result["reason"]
    pm_update._print_protected_hook_reinstall_finding(result, dry_run=False)
    cap = capsys.readouterr()
    assert "[경고]" in cap.err and "repo add" in cap.err


@_git_required
def test_main_reinstalls_protected_hooks_after_apply(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(main) — apply 후 등록 repo 전수 훅 재설치가 실제로 돈다 (T-0415 트리거 배선).

    엔진 업그레이드 = `pm_update` sync 다. 그 경로에 재설치가 안 걸리면 새 훅은 우리 clone 에서만
    돌고 채택자에겐 영영 안 간다(ADR-0071 재설치 트리거 신설 근거)."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)                       # sentinel 1개 = changes>0
    _write_local_conf(dest, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    _stub_no_baseline_git(pm_update, monkeypatch)

    rc = pm_update.main([])
    assert rc == 0
    assert (_hook_dir(dest, "svc") / "pre-commit").exists(), \
        "sync 후 pre-commit 훅이 배포되지 않음 — 엔진 업그레이드가 훅에 도달 안 함"
    assert "✓ 보호 브랜치 pre-push + pre-commit 훅 (재)설치: svc" in capsys.readouterr().out


@_git_required
def test_main_dry_run_previews_reinstall_without_installing(pm_update, tmp_path, monkeypatch, capsys):
    """`--dry-run` — 재설치 *예정*만 알리고 훅은 쓰지 않는다 (dry-run 무write 계약)."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)
    _write_local_conf(dest, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    assert "보호 브랜치 훅 (재)설치 예정" in capsys.readouterr().out
    assert not _hook_dir(dest, "svc").exists(), "dry-run 이 훅을 설치했다"


@_git_required
def test_main_target_mode_does_not_reinstall_hooks(pm_update, tmp_path, monkeypatch, capsys):
    """`--target`(엔진 export)은 재설치 비발화 — templates/<name> 은 PM 홈이 아니다 (경계 일치).

    selfheal/skew/진입 doc 마이그레이션과 같은 경계다. 여기서 재설치가 돌면 export 가 *루트 PM 홈*
    의 훅을 건드리는 월권이 된다."""
    framework = _make_pm_home(tmp_path / "framework")
    _make_upstream(framework)                    # 루트 manifest + sentinel
    target_root = framework / "templates" / "opencode" / ".project_manager"
    target_root.mkdir(parents=True)
    monkeypatch.setattr(pm_update, "REPO", framework)
    _stub_no_baseline_git(pm_update, monkeypatch)

    rc = pm_update.main(["--from", str(framework), "--target", "opencode"])
    assert rc == 0
    assert not _hook_dir(framework, "svc").exists(), \
        "--target export 가 PM 홈 보호 훅을 (재)설치했다(경계 위반)"
    assert "훅" not in capsys.readouterr().out


# ── 트리거는 `changes` 로 게이트되지 않는다 (must-fix·RUN1/RUN2 실측) ──────────────
# 업그레이드 경계에서 sync 를 *실행하는 주체는 dest 의 구 엔진*이다 — 이 기능을 배달하는 sync
# 자체는 재설치 코드가 없다(RUN 1). 그 다음 실행은 dest 가 신 엔진이지만 엔진이 이미 최신이라
# `changes == 0` 이다(RUN 2). "changes>0 에서만" 으로 좁히면 두 실행 다 미발화 → 채택자는 다음에
# 우연히 엔진이 또 바뀔 때까지 가드를 못 받는다. 다른 채널도 없다(bootstrap reconcile 은 sidecar
# 내용/배선만 보고 훅 *본문* 은 pm_update 축 소유·pre-commit 파일 부재는 영구 침묵).


@_git_required
def test_main_reinstalls_protected_hooks_when_no_changes(pm_update, tmp_path, monkeypatch, capsys):
    """**RUN 2 재현** — 엔진이 이미 최신(`changes == 0`)이어도 훅이 깔린다 (T-0415 must-fix).

    이게 업그레이드 배달의 실제 착지 지점이다: 배달 sync 는 구 엔진이 실행해 훅을 못 깔고,
    바로 다음 실행이 여기로 온다. 옆의 진입 doc 마이그레이션(`do_migrate`)이 정확히 같은 이유로
    이 경로에서 write 하는 것과 동형이다."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)
    # dest 를 source 와 동일하게 만들어 changes 0 을 만든다(엔진 이미 최신).
    _write_dest_manifest(dest, [SENTINEL_REL])
    sentinel = dest / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text((source / SENTINEL_REL).read_text(encoding="utf-8"), encoding="utf-8")
    _write_local_conf(dest, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    _stub_no_baseline_git(pm_update, monkeypatch)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "최신 — 변경 없음." in out, f"changes 0 경로가 아님(테스트 전제 깨짐): {out!r}"
    assert (_hook_dir(dest, "svc") / "pre-commit").exists(), \
        "changes 0 이라 훅이 안 깔림 — 업그레이드 배달 다음 실행이 무발화(must-fix 재발)"
    assert "✓ 보호 브랜치 pre-push + pre-commit 훅 (재)설치: svc" in out


@_git_required
def test_main_second_run_is_quiet_when_hooks_in_sync(pm_update, tmp_path, monkeypatch, capsys):
    """정합이면 **완전히 조용** — 트리거는 계속 돌되 출력만 없다 (반복 노이즈 회피).

    노이즈 해법으로 트리거를 끄면 안 되므로(그게 must-fix 였다), 대신 drift 판정이 흡수한다."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)
    _write_local_conf(dest, f"upstream={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    _stub_no_baseline_git(pm_update, monkeypatch)

    assert pm_update.main([]) == 0          # 1회차 — 설치(changes>0)
    capsys.readouterr()
    assert pm_update.main([]) == 0          # 2회차 — changes 0 + 훅 정합
    cap = capsys.readouterr()
    assert "훅" not in cap.out and cap.err == "", \
        f"정합 상태인데 훅 관련 출력이 났다(매 sync 노이즈): {cap.out!r}"
    assert (_hook_dir(dest, "svc") / "pre-commit").exists()


@_git_required
def test_reinstall_protected_hooks_skips_when_in_sync(pm_update, tmp_path, capsys):
    """정합 판정 — 2회차는 `in_sync`(재설치·출력 0)."""
    dest = _make_pm_home(tmp_path / "home")
    assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"]
    capsys.readouterr()
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["in_sync"] == ["svc"] and result["drifted"] == []
    pm_update._print_protected_hook_reinstall_finding(result, dry_run=False)
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


@_git_required
def test_reinstall_protected_hooks_heals_wiped_hook_dir(pm_update, tmp_path):
    """훅 디렉토리가 통째로 지워진 clone 도 자가치유 (bootstrap reconcile 이 못 덮는 상태).

    bootstrap 의 sidecar reconcile 은 sidecar 파일이 없으면 즉시 return 이라 이 상태를 영구
    침묵한다 — pm_update 축이 유일한 복구 채널이다."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    shutil.rmtree(_hook_dir(dest, "svc"))
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["drifted"] == ["svc"] and result["failed"] == []
    assert (_hook_dir(dest, "svc") / "pre-commit").exists()


@_git_required
def test_reinstall_protected_hooks_detects_stale_hook_body(pm_update, tmp_path):
    """설치된 훅 **본문**이 현 엔진 상수와 다르면 drift — 신 훅 배포의 판정축 (T-0415).

    구 엔진이 깐 pre-push 만 있고 pre-commit 이 없거나 본문이 낡은 상태가 정확히 업그레이드
    경계의 모습이다."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    stale = _hook_dir(dest, "svc") / "pre-commit"
    stale.write_text("#!/bin/sh\n# 구버전 훅\nexit 0\n", encoding="utf-8")
    assert pm_update.reinstall_protected_hooks(dest, write=False)["drifted"] == ["svc"]
    pm_update.reinstall_protected_hooks(dest, write=True)
    assert "PM_ALLOW_PROTECTED_COMMIT" in stale.read_text(encoding="utf-8")


@_git_required
def test_reinstall_protected_hooks_detects_stale_sidecar(pm_update, tmp_path):
    """sidecar 보호목록이 areas 실효값과 어긋나도 drift — 두 축(본문·목록) 모두 본다."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    sidecar = _hook_dir(dest, "svc") / "protected"
    sidecar.write_text("mian\n", encoding="utf-8")       # 옛/오타 목록
    assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"]
    assert sidecar.read_text(encoding="utf-8").splitlines() == ["main", "release"]


# ── 읽기 실패도 drift 로 수렴한다 — 4축 전부 (codex must-fix) ────────────────────
# 판정 함수가 예외를 밖으로 내면 호출부의 fail-soft 가 그걸 `unavailable`(= 재설치 안 함)로
# 처리해 **깨진 훅이 영영 복구되지 않는다**. `unavailable` 은 "dest 엔진/레지스트리를 못 불렀다"
# (= 어느 repo 도 못 만진다)를 위한 상태지 "이 repo 의 파일이 깨졌다" 가 아니다. 한 축만 고치면
# 나머지 축에서 같은 클래스가 남으므로 **네 산출물 각각**을 UTF-8 디코딩 불가 바이트로 깨서
# 같은 방향(drifted → 재설치 → 복구)으로 수렴하는지 고정한다.

_UNDECODABLE = b"\xff\xfe\x00\x80 broken hook\n"


@_git_required
@pytest.mark.parametrize("artifact", ["pre-commit", "pre-push", "protected", "engine-root"])
def test_reinstall_protected_hooks_unreadable_artifact_is_drift_not_unavailable(
        pm_update, tmp_path, artifact):
    """훅 산출물이 **읽기 불가**(non-UTF-8)여도 `unavailable` 이 아니라 `drifted` → 재설치 복구."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    broken = _hook_dir(dest, "svc") / artifact
    broken.write_bytes(_UNDECODABLE)

    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["status"] == "done", \
        f"{artifact} 읽기 실패가 unavailable 로 새 나감(복구 채널 소실): {result['reason']!r}"
    assert result["drifted"] == ["svc"] and result["failed"] == []
    # 실제로 복구됐다 — 깨진 바이트가 현 엔진 산출물로 덮였다.
    assert broken.read_bytes() != _UNDECODABLE
    assert pm_update.reinstall_protected_hooks(dest, write=False)["in_sync"] == ["svc"]


# ── 실행 비트도 drift 축이다 + 축은 **명세에서 유도**한다 (codex must-fix 2R) ──────
# 본문만 비교하면 `chmod 0644` 된 훅이 `in_sync` 로 오판돼 **보호가 침묵 비활성화**된다(git 이
# 실행권한 없는 훅을 조용히 건너뛴다). 더 근본은 축 산정 방식 — 판정이 자체 목록을 들면 설치가
# 자랄 때마다 축이 샌다(읽기 실패 → 실행 비트, 두 라운드 연속 같은 클래스). 그래서 판정은
# `worktree_pool.protected_hook_artifacts`(설치와 **같은 명세**)를 읽고, 아래 테스트도 그 명세를
# 순회해 축을 **유도**한다 — 명세에 산출물이 추가되면 이 테스트가 자동으로 그것까지 검사한다.


def _dest_engine(pm_update, dest):
    """dest 의 pm_config/board/worktree_pool 3종 (판정 함수가 받는 것과 같은 모듈)."""
    pm_config = pm_update._load_dest_pm_config(dest)
    return (pm_config,
            pm_config._load_module("board", "board.py"),
            pm_config._load_module("worktree_pool", "worktree_pool.py"))


@_git_required
def test_drift_axes_are_derived_from_install_artifact_spec(pm_update, tmp_path):
    """**명세 순회** — 설치 산출물 *하나하나*에 대해 내용 훼손·실행권한 상실이 drift 로 잡힌다.

    축을 손으로 열거하지 않는다: `protected_hook_artifacts` 가 말하는 산출물 전수를 돌며 각
    축을 친다. install 이 새 산출물을 추가하면(명세에 추가해야 하므로) 이 테스트가 자동으로
    그 산출물까지 검사한다 — "다음에 또 축이 샌다" 를 닫는 장치."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    _pc, _board, worktree_pool = _dest_engine(pm_update, dest)
    spec = worktree_pool.protected_hook_artifacts("svc", ["main", "release"])
    assert spec, "명세가 비었다(테스트 전제 깨짐)"

    for artifact in spec:
        name = artifact.path.name
        # ① 내용 훼손 → drift → 재설치로 복구.
        artifact.path.write_text("tampered\n", encoding="utf-8")
        assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"], \
            f"{name} 내용 훼손이 drift 로 안 잡힘(미검사 축)"
        assert artifact.path.read_text(encoding="utf-8") == artifact.content
        # ② 실행권한 상실 → drift → 0755 복구 (실행권한을 요구하는 산출물만).
        if artifact.executable:
            artifact.path.chmod(0o644)
            assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"], \
                f"{name} 실행권한 상실이 drift 로 안 잡힘 — git 이 훅을 조용히 건너뛴다"
            assert artifact.path.stat().st_mode & 0o111, f"{name} 0755 복구 실패"
        # ③ 전부 정합이면 조용(무한 재설치 회귀 방지).
        assert pm_update.reinstall_protected_hooks(dest, write=True)["in_sync"] == ["svc"]


@_git_required
@pytest.mark.parametrize("hook_name", ["pre-commit", "pre-push"])
def test_reinstall_protected_hooks_missing_exec_bit_is_drift(pm_update, tmp_path, hook_name):
    """실행 비트만 빠진 훅(본문 동일·`chmod 0644`)도 drift → 재설치 후 0755 복구 (T-0415).

    본문 동일이라 내용 축으로는 절대 안 잡히는 상태다 — 이게 "보호 훅 침묵 비활성화" 의 모습."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    hook = _hook_dir(dest, "svc") / hook_name
    body_before = hook.read_text(encoding="utf-8")
    hook.chmod(0o644)

    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["status"] == "done" and result["drifted"] == ["svc"], \
        f"{hook_name} 실행권한 상실이 in_sync 로 오판됨(보호 침묵 비활성)"
    assert hook.stat().st_mode & 0o111, f"{hook_name} 실행권한이 재설치로 복구되지 않음"
    assert hook.read_text(encoding="utf-8") == body_before   # 본문은 그대로(내용 축 무관)


@_git_required
def test_exec_bit_axis_disabled_on_windows(pm_update, tmp_path, monkeypatch):
    """Windows 는 실행 비트 축 **비활성** — 거짓 drift 로 매 sync 재설치가 돌지 않는다.

    NTFS 엔 POSIX mode 가 없고 `chmod` 는 read-only 플래그만 만진다(`st_mode & 0o111` 이 늘
    거짓) — 그 축을 그대로 보면 Windows 채택자는 매 pm_update 마다 재설치+출력이 난다.
    git-for-windows 는 훅을 sh 로 돌리며 실행 비트를 요구하지도 않아 축 자체가 무의미하다.
    플랫폼 분기는 상수 하나(`_EXEC_BIT_MEANINGFUL`)라 hermetic 하게 뒤집어 친다."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    (_hook_dir(dest, "svc") / "pre-commit").chmod(0o644)

    monkeypatch.setattr(pm_update, "_EXEC_BIT_MEANINGFUL", False)   # = Windows
    result = pm_update.reinstall_protected_hooks(dest, write=True)
    assert result["in_sync"] == ["svc"] and result["drifted"] == [], \
        "Windows 에서 실행 비트 축이 거짓 drift 를 냈다(매 sync 재설치)"


@_git_required
@pytest.mark.parametrize("artifact", ["pre-commit", "pre-push", "protected", "engine-root"])
def test_protected_hook_in_sync_never_raises_on_unreadable(pm_update, tmp_path, artifact):
    """판정 함수 자체의 계약 — 읽기 실패에 예외 대신 `False`(drift) (4축 동형)."""
    dest = _make_pm_home(tmp_path / "home")
    pm_update.reinstall_protected_hooks(dest, write=True)
    (_hook_dir(dest, "svc") / artifact).write_bytes(_UNDECODABLE)

    pm_config = pm_update._load_dest_pm_config(dest)
    board = pm_config._load_module("board", "board.py")
    worktree_pool = pm_config._load_module("worktree_pool", "worktree_pool.py")
    assert pm_update._protected_hook_in_sync(
        "svc", pm_config=pm_config, worktree_pool=worktree_pool, board=board) is False
