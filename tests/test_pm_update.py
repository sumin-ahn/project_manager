"""pm_update.py upstream 해소 단위 테스트 (T-0053).

--from 생략 시 dest local.conf 의 `upstream.path=` 을 기본으로 쓰는 해소 순서(①명시 --from
②local.conf upstream= ③에러)·stale 가드·`_read_local_conf` 파싱을 검증한다. 실 복사 없이
plan/dry-run 레벨로 — fake_repo(REPO monkeypatch) + tmp source 만으로 외부 의존 0.

self-location(--target 생략) 모드는 effective_dest=REPO 이므로 pm_update.REPO 를 tmp 로
monkeypatch 해 실 REPO 를 건드리지 않고 local.conf upstream 해소를 검증한다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from _win_skip import posix_mode_supported

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
    _track_source_tree(root)


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


def _track_source_tree(root: Path) -> None:
    """directory-manifest fixture를 실제 tracked checkout으로 만들어 fallback 경고를 막는다."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(root), "init", "-q"],
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", "-A"],
        capture_output=True,
        text=True,
        check=True,
    )


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
    _track_source_tree(root)


# ── _read_local_conf 파싱 단위 (board.local_config 규칙 미러) ────────────────

def test_read_local_conf_parses_key_value(pm_update, tmp_path):
    conf = tmp_path / "local.conf"
    conf.write_text(
        "# 헤더 주석\n"
        "\n"
        "session=pm\n"
        "upstream.path=/home/u/checkout\n"
        "   # 들여쓴 주석\n"
        "bad line without equals\n"
        "  runtime.py = python3  \n",
        encoding="utf-8",
    )
    result = pm_update._read_local_conf(conf)
    assert result["session"] == "pm"
    assert result["upstream.path"] == "/home/u/checkout"
    assert result["runtime.py"] == "python3"  # 양쪽 공백 strip
    assert "bad line without equals" not in result
    # 주석/빈 줄은 키가 되지 않는다.
    assert "# 헤더 주석" not in result
    assert "" not in result


def test_read_local_conf_missing_returns_empty(pm_update, tmp_path):
    assert pm_update._read_local_conf(tmp_path / "nope.conf") == {}


def test_read_local_conf_last_value_wins(pm_update, tmp_path):
    conf = tmp_path / "local.conf"
    conf.write_text("upstream.path=/first\nupstream.path=/second\n", encoding="utf-8")
    assert pm_update._read_local_conf(conf)["upstream.path"] == "/second"


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
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--from", str(explicit), "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    # explicit 의 manifest/sentinel 이 plan 에 떠야 하고, stored 의 것은 *뜨지 않아야* 한다.
    assert explicit_rel in out, "명시 --from(explicit) source 가 plan 에 안 쓰였다"
    assert stored_rel not in out, "local.conf 의 stored upstream 이 명시 --from 을 덮었다(우선순위 역전)"


def test_cli_classified_git_failure_is_one_line_without_raw_traceback(
        pm_update, tmp_path, monkeypatch, capsys):
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "templates" / "tgt").mkdir(parents=True)
    source = tmp_path / "source"
    _make_upstream(source)
    repo_files = pm_update._load_repo_owned_files()
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    monkeypatch.setattr(
        repo_files,
        "_real_git_runner",
        lambda _root: lambda _argv: (129, "error: unknown option `stage'"),
    )

    rc = pm_update.main(["--from", str(source), "--target", "tgt"])

    assert rc == 1
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert "source 출하 파일의 git 추적정보" in err
    assert str(source) in err
    assert "rc=129" in err
    assert "git index 상태를 확인·복구" in err
    assert "Traceback" not in err


def test_cli_classification_does_not_reload_repo_files_during_exception(
        pm_update, monkeypatch, capsys):
    repo_files = pm_update._load_repo_owned_files()
    calls = 0

    def load_once():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("예외 처리 중 repo-owned 로더 재호출")
        return repo_files

    def fail_main(_argv):
        raise repo_files.RepoFilesGitError("원 git 열거 오류")

    monkeypatch.setattr(pm_update, "_load_repo_owned_files", load_once)
    monkeypatch.setattr(pm_update, "_main", fail_main)

    assert pm_update.main([]) == 1
    assert calls == 1
    err = capsys.readouterr().err
    assert "원 git 열거 오류" in err
    assert "예외 처리 중" not in err


@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_tracked_manifest_inventory_is_nonzero_in_apply_and_dry_run(
        pm_update, tmp_path, monkeypatch, capsys, dry_run):
    """실제 git 저장소의 빈 index subtree는 실행·dry-run 모두 전파 전에 비0이다."""
    fake_repo = tmp_path / f"fake-repo-{dry_run}"
    (fake_repo / "templates" / "tgt").mkdir(parents=True)
    source = tmp_path / f"source-{dry_run}"
    ship = source / "ship"
    ship.mkdir(parents=True)
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("ship\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "add", ".project_manager/engine.manifest"],
        check=True,
    )
    assert subprocess.run(
        ["git", "-C", str(source), "ls-files", "--", "ship"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    argv = ["--from", str(source), "--target", "tgt"]
    if dry_run:
        argv.append("--dry-run")

    rc = pm_update.main(argv)

    captured = capsys.readouterr()
    assert rc == 1
    assert "pm-update 출하 인벤토리가 0건임" in captured.err
    assert str(source) in captured.err
    assert "subtree='ship'" in captured.err
    assert "git index" in captured.err
    assert list((fake_repo / "templates" / "tgt").iterdir()) == []


def test_empty_non_git_manifest_inventory_uses_filesystem_diagnostic(
        pm_update, tmp_path, monkeypatch, capsys):
    """비-git filesystem 강등은 git index 대신 빈 소스 디렉토리·checkout 루트를 안내한다."""
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "templates" / "tgt").mkdir(parents=True)
    source = tmp_path / "unpacked-source"
    (source / "ship").mkdir(parents=True)
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("ship\n", encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    with pytest.warns(
            pm_update._load_repo_owned_files().RepoFilesFallbackWarning,
            match="filesystem 전수 순회"):
        rc = pm_update.main(["--from", str(source), "--target", "tgt"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "filesystem 강등 상태" in err
    assert "소스 디렉토리가 비었는지" in err
    assert "checkout 루트" in err
    assert "git index" not in err
    assert list((fake_repo / "templates" / "tgt").iterdir()) == []


@pytest.mark.parametrize("subtree_state", ["empty", "absent"])
def test_shipping_inventory_guard_covers_both_silent_seam_shapes(
        pm_update, tmp_path, subtree_state):
    """디스크도 빈 subtree와 subtree 부재 모두 warning 없이 소비점의 명시 판정이 막는다."""
    source = tmp_path / f"source-{subtree_state}"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    rel = "ship"
    if subtree_state == "empty":
        (source / rel).mkdir()
    repo_files = pm_update._load_repo_owned_files()
    assert subprocess.run(
        ["git", "-C", str(source), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(
                pm_update.EmptyShippingInventoryError,
                match=r"pm-update 출하 인벤토리가 0건임.*subtree='ship'"):
            pm_update._shipping_inventory(repo_files, source, rel)

    assert caught == []


def test_missing_manifest_entry_keeps_existing_missing_report_path(
        pm_update, tmp_path):
    """경로 자체 부재는 빈 인벤토리 예외가 아니라 기존 plan missing 결과다."""
    source = tmp_path / "source-missing"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)

    changes, missing = pm_update.plan(
        source,
        [".project_manager/tools/absent.py"],
        dest_root=tmp_path / "dest",
    )

    assert changes == []
    assert missing == [".project_manager/tools/absent.py"]


# ── ② --from 생략 → local.conf upstream 사용 (plan 도달) ─────────────────────

def test_omitted_from_uses_local_conf_upstream(pm_update, tmp_path, monkeypatch, capsys):
    """--from 생략 시 dest local.conf 의 upstream= 을 기본 source 로 써서 plan 에 도달한다."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"# conf\nupstream.path={stored}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={stale}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={a_file}\n")

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
    _write_local_conf(dest, "session=pm\nupstream.path=/some/checkout\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "headcommit99")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    changed = pm_update.record_upstream_rev_baseline(dest, source)
    assert changed is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream.rev"] == "headcommit99"
    assert conf["upstream.path"] == "/some/checkout"  # 별개 키 보존(한 키 2역 금지)
    assert conf["session"] == "pm"


def test_record_upstream_rev_baseline_skips_when_source_not_git(pm_update, tmp_path, monkeypatch):
    """source 가 git checkout 이 아니면(read_upstream_rev=None·URL upstream 포함) graceful 생략."""
    dest = tmp_path / "dest"
    _write_local_conf(dest, "upstream.path=https://h/x.git\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    changed = pm_update.record_upstream_rev_baseline(dest, source)
    assert changed is False
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert "upstream.rev" not in conf


def test_main_records_upstream_rev_on_successful_sync(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(apply) 후 upstream_rev baseline 이 기록된다(매 sync·dry-run 은 기록 안 함)."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    # baseline rev 읽기를 결정적으로 stub(라이브 git 0).
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "syncedrev42")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])  # 실 sync(dry-run 아님) — sentinel 1개 복사.
    assert rc == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == "syncedrev42", \
        f"매 sync 후 upstream_rev baseline 미갱신: {conf.get('upstream.rev')!r}"


def test_main_dry_run_does_not_record_upstream_rev(pm_update, tmp_path, monkeypatch):
    """--dry-run 은 실 sync 가 아니므로 upstream_rev baseline 을 기록하지 않는다(파일 미변경)."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main(["--dry-run"])
    assert rc == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert "upstream.rev" not in conf, "dry-run 인데 upstream_rev 가 기록됨(부작용 누출)"


# ── T-0413: 경로 upstream 은 seen(관찰값)도 baseline 과 동시 기록 (거짓 drift 근절) ──
# 경로 형상은 fetch 채널이 따로 없어 *동기 시점 checkout rev 가 곧 관찰값*이다. baseline 만
# 갱신하면 두 키가 영구히 어긋나 정상 흡수 직후에도 adapter-drift 가 상시 뜬다(② 실측).
# URL 형상은 스킬층이 fetch 후 seen 을 쓰므로 엔진이 건드리지 않는다(한 키 2역 금지·ADR-0032 D2).

def test_record_upstream_rev_baseline_records_seen_for_path_upstream(
        pm_update, tmp_path, monkeypatch):
    """경로 upstream — baseline(upstream_rev)과 관찰값(upstream_seen_rev)이 같은 rev 로 동시 기록."""
    dest = tmp_path / "dest"
    _write_local_conf(dest, "session=pm\nupstream.path=/some/checkout\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "pathrev1234")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream.rev"] == "pathrev1234"
    assert conf["upstream.seen_rev"] == "pathrev1234", \
        f"경로 upstream 인데 seen 미기록(두 키 어긋남 잔존): {conf!r}"
    assert conf["upstream.path"] == "/some/checkout" and conf["session"] == "pm"  # 타 키 보존


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
        "upstream.path=/w/project_manager_1\n"
        "upstream.rev=ddf6f4842653\n"
        "upstream.seen_rev=0ccc02513a7f\n"
        "runtime.py=python3\n",
    )
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "nextsyncrev9")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    text = (dest / ".project_manager" / "local.conf").read_text(encoding="utf-8")
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream.rev"] == conf["upstream.seen_rev"] == "nextsyncrev9", \
        f"다음 sync 1회로 수렴 안 됨: {conf!r}"
    assert "# per-clone" in text and conf["runtime.py"] == "python3"  # 주석·타 키 보존
    assert text.count("upstream.seen_rev=") == 1, f"seen 키 중복 append: {text!r}"


def test_record_upstream_rev_baseline_leaves_seen_for_url_upstream(
        pm_update, tmp_path, monkeypatch):
    """URL upstream(스킬이 cache clone 후 --from) — baseline 만 갱신, seen 은 스킬층 값 그대로.

    URL 은 fetch 관찰과 sync 가 분리된 채널이라 엔진이 seen 을 쓰면 race/자기비교가 된다
    (ADR-0032 D2·codex round-3 NEW-2). 엔진은 건드리지 않는다.
    """
    dest = tmp_path / "dest"
    _write_local_conf(
        dest,
        "upstream.path=https://github.com/example/project_manager.git\n"
        "upstream.seen_rev=skillfetchrev\n",
    )
    source = tmp_path / "cache"  # 스킬이 clone 한 로컬 cache checkout.
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "cacheheadrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.record_upstream_rev_baseline(dest, source) is True
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf["upstream.rev"] == "cacheheadrev"
    assert conf["upstream.seen_rev"] == "skillfetchrev", \
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
    _write_local_conf(path_dest, "upstream.path=/w/project_manager_1\n")
    changed, recorded = pm_update.record_upstream_revs(path_dest, source)
    assert changed is True
    assert recorded == {"upstream.rev": "shaperev777", "upstream.seen_rev": "shaperev777"}

    url_dest = tmp_path / "url_dest"
    # URL 형상 + 스킬이 이미 기록한 seen == 이번 cache HEAD (파일 상태로는 구분 불가한 조건).
    _write_local_conf(
        url_dest,
        "upstream.path=https://github.com/example/project_manager.git\n"
        "upstream.seen_rev=shaperev777\n",
    )
    changed, recorded = pm_update.record_upstream_revs(url_dest, source)
    assert changed is True
    assert recorded == {"upstream.rev": "shaperev777"}, \
        f"URL 형상인데 seen 을 기록했다고 보고: {recorded!r}"


def test_record_upstream_revs_writes_both_keys_in_single_pass(
        pm_update, tmp_path, monkeypatch):
    """두 키는 **공용 writer 한 번**에 묶인다 — baseline 만 앞선 반쪽 상태 불가.

    중간 중단 시 두 키가 어긋난 채 남는 것이 바로 이 티켓이 없앤 거짓 drift 의 원인이므로,
    분리 write 로 되돌아가면 실패한다(회귀 가드). 감시 지점은 공용 writer 의 임계 구간 본문
    (`_write_conf_keys_locked`)이다 — 형상 판정·계획·write 가 한 conf 락 안이라 호출부가 그 본문을
    직접 부른다(락 재진입 금지).
    """
    dest = tmp_path / "dest"
    _write_local_conf(dest, "upstream.path=/w/project_manager_1\n")
    source = tmp_path / "src"
    source.mkdir()

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "onepassrev5")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    calls: list[dict] = []
    real_write = pm_import._write_conf_keys_locked
    monkeypatch.setattr(
        pm_import, "_write_conf_keys_locked",
        lambda path, updates: (calls.append(dict(updates)), real_write(path, updates))[1])

    assert pm_update.record_upstream_revs(dest, source)[0] is True
    assert calls == [{"upstream.rev": "onepassrev5", "upstream.seen_rev": "onepassrev5"}], \
        f"두 키가 단일 write 로 묶이지 않음(반쪽 상태 위험): {calls!r}"


def test_main_records_both_keys_on_path_sync(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(경로 upstream) 후 두 키가 같아진다 — 흡수 직후 drift advisory 0 의 조건."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "bothkeysrev1")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main([]) == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == conf.get("upstream.seen_rev") == "bothkeysrev1", \
        f"경로 sync 후 두 키 불일치(거짓 drift 재발): {conf!r}"
    assert "upstream.seen_rev 동시 기록" in capsys.readouterr().out


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
        f"upstream.path={source}\n"
        "upstream.rev=currentrev\n"
        "upstream.seen_rev=staleobservedrev\n",
    )
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "currentrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main([]) == 0
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == conf.get("upstream.seen_rev") == "currentrev"
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
        f"upstream.path={source}\nupstream.rev=currentrev\nupstream.seen_rev=currentrev\n",
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
        f"upstream.path={source}\nupstream.rev=currentrev\nupstream.seen_rev=staleobservedrev\n",
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
        f"upstream.path={source}\nupstream.rev=currentrev\nupstream.seen_rev=staleobservedrev\n",
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
        "upstream.path=https://github.com/example/project_manager.git\n"
        "upstream.seen_rev=cachehead77\n",  # 스킬이 fetch 후 기록한 관찰값.
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
    assert conf.get("upstream.seen_rev") == "cachehead77"  # 스킬층 관찰값 그대로.


def test_main_skew_suppression_suppresses_seen_too(pm_update, tmp_path, monkeypatch, capsys):
    """manifest skew 억제 경로 — baseline 뿐 아니라 seen 도 미갱신(동시성 유지).

    baseline 만 멈추고 seen 이 앞서면 반대 방향 거짓 경보가 된다. 두 기록이 한 함수 안이라
    억제도 함께 걸린다 — skew 판정을 결정적으로 주입해 그 배선을 검증한다.
    """
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "stored_upstream"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")
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
    assert "upstream.rev" not in conf, f"skew 인데 baseline 기록됨: {conf!r}"
    assert "upstream.seen_rev" not in conf, \
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


def test_detect_manifest_skew_uses_all_selected_upstream_manifests(
        pm_update, tmp_path):
    """diverged 로컬에서도 후순위 flavor 신규 경로를 skew로 잡아 in_sync 오판을 막는다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first_self = (
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest"
    )
    second_old = ".second/old    @source=templates/second/.second/old"
    second_new = ".second/new    @source=templates/second/.second/new"
    first.write_text(f"{SENTINEL_REL}\n{first_self}\n", encoding="utf-8")
    second.write_text(
        f"{second_old}\n{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    local_text = pm_update.merge_manifest_sources([first, second])["text"]
    dest = tmp_path / "dest"
    dest_manifest = _write_dest_manifest(dest, [])
    dest_manifest.write_text(
        local_text.rstrip("\n") + "\n.custom/local-only\n", encoding="utf-8")

    # 후순위 flavor가 진화했지만 로컬-only 경로 때문에 selfheal 전체 교체는 금지된다.
    second.write_text(
        f"{second_old}\n{second_new}\n{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    selfheal = pm_update.resolve_manifest_selfheal(dest, source)
    assert selfheal["status"] == "diverged"
    assert [p.parents[1].name for p in selfheal["upstream_manifests"]] == [
        "first", "second",
    ]

    local_entries = pm_update.read_manifest(dest_manifest)
    status, new_entries = pm_update.detect_manifest_skew(
        local_entries,
        source,
        upstream_manifests=selfheal["upstream_manifests"],
    )
    assert status == "skew", "후순위 flavor 신규 경로를 첫 manifest만 보고 in_sync로 오판"
    assert new_entries == [".second/new"]


def test_main_selfheal_supersedes_skew_suppression(pm_update, tmp_path, monkeypatch, capsys):
    """T-0395 amend(T-0396): 구형 로컬 manifest + 읽기 가능한 upstream 이면 baseline 억제가 아니라
    **자기치유** — upstream 승격으로 skew 가 정의상 0 이 되어, skew/억제 메시지 없이 baseline 이
    갱신된다(치유 후 정합). T-0395 억제는 upstream manifest 읽기 실패 잔여 케이스 안전망으로만 남고,
    읽기 가능한 구형 로컬은 이 경로가 대체한다(회사 실측 근절)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])  # 구형 — NEW_ENGINE_REL 누락.
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    assert conf.get("upstream.rev") == "amendrev", \
        f"치유 후 정합인데 baseline 미갱신(억제 잔존): {conf.get('upstream.rev')!r}"


def test_main_records_baseline_when_manifest_in_sync(pm_update, tmp_path, monkeypatch, capsys):
    """로컬 manifest 가 upstream 과 정합이면 현행대로 upstream_rev baseline 갱신 + skew 경고 없음."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL])  # 정합 로컬 manifest.
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "insyncrev7")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest skew" not in out, f"정합인데 skew 오탐: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == "insyncrev7", \
        f"정합 sync 인데 baseline 미갱신: {conf.get('upstream.rev')!r}"


def test_main_records_baseline_when_upstream_manifest_absent(pm_update, tmp_path, monkeypatch, capsys):
    """upstream manifest 부재(구 upstream)면 대조 생략·현행대로 baseline 갱신 + fail-soft note."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    # source 에 sentinel 파일은 있으나 engine.manifest 는 없음(구 upstream). dest manifest 로 sync.
    sentinel = source / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# upstream sentinel\n", encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(fake_repo, [SENTINEL_REL])
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "failsoftrev")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    rc = pm_update.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fail-soft" in out, f"upstream manifest 부재 fail-soft note 미출력: {out!r}"
    conf = pm_update._read_local_conf(fake_repo / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == "failsoftrev", \
        f"fail-soft 인데 baseline 미갱신: {conf.get('upstream.rev')!r}"


def test_main_dry_run_shows_selfheal_not_skew_without_recording(pm_update, tmp_path, monkeypatch, capsys):
    """T-0395 amend(T-0396): --dry-run 은 읽기 가능한 구형 로컬을 skew 억제로 표시하지 않고 **자기치유
    예정**으로 표시하며, baseline 은 기록하지 않는다(read-only)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    assert "upstream.rev" not in conf, "dry-run 인데 baseline 기록됨(부작용 누출)"


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
    _write_local_conf(target_root, f"upstream.path={source}\n")
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
    assert conf.get("upstream.rev") == "targetrev1", \
        f"--target 인데 baseline 미갱신(현행 거동 위반): {conf.get('upstream.rev')!r}"


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
    _track_source_tree(source)
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
    _track_source_tree(source)
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


# ── guest 마커 세대 하위호환 (T-0571) ────────────────────────────────────────
# 아래 세 리터럴은 **엔진 상수를 참조하지 않고 테스트 파일에 직접 박은** 바이트다. 상수를 참조하면
# 리터럴이 통째로 바뀌어도 테스트가 green 이라(그래서 이 결함이 30여 릴리즈 생존했다) 채택자
# 디스크에 이미 기록된 형태를 고정하지 못한다. 세대가 늘면 여기에 그 세대 리터럴을 추가한다.
GUEST_BEGIN_LITERAL_CURRENT = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 = (
    "# >>> pm add-harness guest @render (local·pm_update-preserved·T-0456) >>>")
GUEST_END_LITERAL = "# <<< pm add-harness guest @render (local) <<<"


def test_guest_marker_literals_match_current_constants(pm_update):
    """세대 고정 가드: 현행 상수 값 == 테스트에 박은 문자열 리터럴.

    리터럴이 또 바뀌면 이 테스트가 red 로 알린다 — 그때 옛 값을 `_GUEST_MANIFEST_BEGIN_LEGACY`
    에 추가하고(읽기 관용) 여기 CURRENT 를 갱신하는 것이 정상 절차다."""
    assert pm_update._GUEST_MANIFEST_BEGIN == GUEST_BEGIN_LITERAL_CURRENT
    assert pm_update._GUEST_MANIFEST_END == GUEST_END_LITERAL
    assert GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 in pm_update._GUEST_MANIFEST_BEGIN_LEGACY, \
        "옛 세대 시작 마커가 legacy 튜플에서 빠졌다(옛 채택자 guest 절이 소실된다)"
    assert GUEST_BEGIN_LITERAL_CURRENT not in pm_update._GUEST_MANIFEST_BEGIN_LEGACY, \
        "현행 리터럴이 legacy 튜플에 있다(쓰기 단일 세대 위반)"


def test_extract_guest_block_recognizes_legacy_marker(pm_update):
    """옛 리터럴로 기록된 manifest 도 guest 절로 인식된다 — 단일 비교였을 때 절이 조용히 사라졌다."""
    text = ("# m\n.project_manager/tools/board.py\n\n"
            + GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 + "\n"
            + ".codex/agents    @render @target-owned\n"
            + GUEST_END_LITERAL + "\n")
    block = pm_update._extract_guest_manifest_block(text)
    assert block is not None, "옛 세대 마커를 인식 못 함(guest 절 소실 경로)"
    assert ".codex/agents" in block
    # strip 도 같은 세대 집합 — core 경로 집합에 guest 라인이 섞이지 않는다(소유권 판정 정합).
    assert pm_update._core_manifest_paths(text) == {".project_manager/tools/board.py"}


def test_migrate_legacy_guest_markers_replaces_and_is_idempotent(pm_update):
    """옛 시작 마커 → 현행 리터럴 치환(changed=True) · 현행 입력은 무변경(changed=False)."""
    legacy = (GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 + "\n.codex/agents    @render @target-owned\n"
              + GUEST_END_LITERAL + "\n")
    migrated, changed = pm_update._migrate_legacy_guest_markers(legacy)
    assert changed is True
    assert GUEST_BEGIN_LITERAL_CURRENT in migrated
    assert GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 not in migrated
    assert migrated.endswith("\n"), "트레일링 개행이 사라졌다(멱등 비교가 어긋난다)"
    # 멱등 — 이미 현행이면 재치환 0.
    again, changed_again = pm_update._migrate_legacy_guest_markers(migrated)
    assert changed_again is False and again == migrated
    # 마커 자체가 없는 텍스트도 무변경.
    plain = "# m\n.project_manager/tools/board.py\n"
    assert pm_update._migrate_legacy_guest_markers(plain) == (plain, False)


def test_copy_manifest_preserving_guest_migrates_legacy_marker(pm_update, tmp_path, capsys):
    """옛 리터럴 dest + 신 upstream 왕복 → guest 절 보존 + 결과 마커가 **현행 리터럴** + 1줄 표기."""
    sp, dst = tmp_path / "up.manifest", tmp_path / "dest.manifest"
    sp.write_text("# m\n.project_manager/tools/board.py\n", encoding="utf-8")
    dst.write_text(
        "# m\n.project_manager/tools/board.py\n\n"
        + GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 + "\n"
        + ".codex/agents    @render @target-owned\n"
        + GUEST_END_LITERAL + "\n", encoding="utf-8")

    pm_update._copy_manifest_preserving_guest(sp, dst)

    after = dst.read_text(encoding="utf-8")
    assert GUEST_BEGIN_LITERAL_CURRENT in after, "재부착 마커가 현행 리터럴이 아님"
    assert GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 not in after, "옛 리터럴이 잔존(세대 공존)"
    assert ".codex/agents" in after, "guest 절이 사라졌다(이 결함의 본체)"
    assert "guest 절 마커 세대 마이그레이션" in capsys.readouterr().out, \
        "조용한 변환(표기 없음)"
    # 재실행 멱등 — 이미 현행이라 마이그레이션 표기가 다시 뜨지 않는다.
    pm_update._copy_manifest_preserving_guest(sp, dst)
    assert "guest 절 마커 세대 마이그레이션" not in capsys.readouterr().out


def test_plan_manifest_self_prop_updates_on_legacy_guest_marker(pm_update, tmp_path):
    """옛 세대 마커는 core 가 같아도 engine.manifest update 로 계획된다 — apply 가 도는 이 change 가
    마커 마이그레이션의 유일한 도달 경로다(계획에서 빠지면 옛 세대가 영구 잔존)."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    core = "# m\n" + MANIFEST_SELF_REL + "\n"
    (source / ".project_manager").mkdir(parents=True)
    (source / ".project_manager" / "engine.manifest").write_text(core, encoding="utf-8")
    _track_source_tree(source)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        core.rstrip("\n") + "\n\n" + GUEST_BEGIN_LITERAL_LEGACY_V1_4_3
        + "\n.codex/agents    @render @target-owned\n" + GUEST_END_LITERAL + "\n",
        encoding="utf-8")
    manifest = [pm_update.ManifestEntry(MANIFEST_SELF_REL)]

    changes, _missing = pm_update.plan(source, manifest, dest_root=dest)

    assert any(str(r).replace("\\", "/") == MANIFEST_SELF_REL for r, _s, _d, _k in changes), \
        "옛 세대 마커 dest 가 계획에 안 실려 마이그레이션이 영영 도달하지 않는다"


def _write_selfprop_dest_manifest(dest: Path, core: str, begin_literal: str) -> Path:
    """dest engine.manifest = core + 지정 세대 마커의 guest 절 (self-prop 분기 픽스처 공용)."""
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        core.rstrip("\n") + "\n\n" + begin_literal
        + "\n.codex/agents    @render @target-owned\n" + GUEST_END_LITERAL + "\n",
        encoding="utf-8")
    return manifest


def test_plan_multi_harness_manifest_updates_on_legacy_guest_marker(pm_update, tmp_path):
    """**다중-harness 합집합 분기**(`manifest_source_text`)도 옛 세대 마커를 update 사유로 본다.

    합집합 텍스트는 upstream 실파일이 아니라 인메모리라 별도 분기를 탄다 — 거기에 마커 축이 없으면
    core 가 같은 다중-harness 채택자는 마이그레이션이 담긴 change 를 영영 못 받아 세대가 잔존한다
    (단일 flavor 채택자만 수렴하는 형상 의존 결함)."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    core = "# m\n" + MANIFEST_SELF_REL + "\n"
    (source / ".project_manager").mkdir(parents=True)
    _track_source_tree(source)
    manifest_file = _write_selfprop_dest_manifest(
        dest, core, GUEST_BEGIN_LITERAL_LEGACY_V1_4_3)
    manifest = [pm_update.ManifestEntry(MANIFEST_SELF_REL)]

    changes, _missing = pm_update.plan(
        source, manifest, dest_root=dest, manifest_source_text=core)

    assert any(str(r).replace("\\", "/") == MANIFEST_SELF_REL for r, _s, _d, _k in changes), \
        "합집합 분기에서 옛 세대 마커 dest 가 계획에 안 실림(마이그레이션 미도달)"

    pm_update.apply(changes)

    after = manifest_file.read_text(encoding="utf-8")
    assert GUEST_BEGIN_LITERAL_CURRENT in after, "적용 후 마커가 현행 리터럴이 아님"
    assert GUEST_BEGIN_LITERAL_LEGACY_V1_4_3 not in after, "옛 리터럴 잔존(세대 공존)"
    assert ".codex/agents" in after, "guest 절이 사라졌다"


def test_plan_multi_harness_manifest_no_churn_with_current_marker(pm_update, tmp_path):
    """같은 합집합 분기에서 **현행 마커 + core 동일**은 계획에 안 실린다(과교정·영구 churn 방지)."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    core = "# m\n" + MANIFEST_SELF_REL + "\n"
    (source / ".project_manager").mkdir(parents=True)
    _track_source_tree(source)
    _write_selfprop_dest_manifest(dest, core, GUEST_BEGIN_LITERAL_CURRENT)
    manifest = [pm_update.ManifestEntry(MANIFEST_SELF_REL)]

    changes, _missing = pm_update.plan(
        source, manifest, dest_root=dest, manifest_source_text=core)

    assert not any(str(r).replace("\\", "/") == MANIFEST_SELF_REL for r, _s, _d, _k in changes), \
        f"현행 마커인데 합집합 분기가 영구 update(churn): {changes}"


def test_resolve_manifest_selfheal_ignores_legacy_guest_block_and_heals(pm_update, tmp_path):
    """옛 세대 마커 guest 절도 core 비교에서 제외 — 승격(heal)이 막히지 않는다(영구 diverged 방지)."""
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])
    _write_dest_manifest(tmp_path / "dest", [
        SENTINEL_REL, GUEST_BEGIN_LITERAL_LEGACY_V1_4_3,
        ".codex/agents    @render @target-owned", GUEST_END_LITERAL])

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)

    assert result["status"] == "heal", \
        f"옛 세대 guest 절이 core 비교를 오염(diverged): {result['status']}"
    assert ".codex/agents" not in result["removed"], result["removed"]


# ── guest 절 엔진 행 동기 채널 (T-0574) ──────────────────────────────────────
# guest 절 한 줄의 `@render` 유무가 소유 채널을 가른다: 렌더물=add-harness refresh(plan 제외),
# 엔진 파일=update 채널(plan 포함·byte-copy). 아래 픽스처는 **실 flavor 디렉토리명**을 쓴다 —
# 이름이 실물이어야 pm_import 하네스 registry 로 어댑터 namespace 가 해소된다(가짜 이름은 파생 제외).

def _current_engine_rev() -> str:
    spec = importlib.util.spec_from_file_location(
        "_fixture_engine_rev", TOOLS / "engine_rev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ENGINE_REV


# 합성 엔진 사본도 **스탬프를 단다** — 활성 stamped 모듈이 리터럴 없이 놓인 트리는 "구형 활성
# 모듈"(verifier 가 곧 skew 로 판정할 상태)이라 동기 종료 시 미수렴으로 잡힌다. 이 파일의 관심사는
# guest 절 채널이지 rev 수렴이 아니므로, 픽스처가 그 상태를 우연히 만들지 않게 상류·채택자 양쪽을
# 현행 rev 로 맞춘다(byte 전파 판정에는 영향 없음 — 양쪽이 같은 텍스트다).
_ENGINE_SENTINEL = f'# engine v2\nENGINE_REV = "{_current_engine_rev()}"\n'

_GUEST_FRAMEWORK_FILES = {
    "templates/claude_code/.claude/ctx_guard.py": "# ctx guard v2\n",
    "templates/opencode/.opencode/lib/relay.js": "// relay v2\n",
    "templates/opencode/.opencode/pm_orch_opencode.py": "# driver v2\n",
    "templates/opencode/.opencode/agents/pm.md": "upstream agent\n",
    ".project_manager/tools/board.py": _ENGINE_SENTINEL,
}

_CLAUDE_FLAVOR_MANIFEST = (
    ".project_manager/tools/board.py\n"
    ".claude/ctx_guard.py    @source=templates/claude_code/.claude/ctx_guard.py\n"
    f"{MANIFEST_SELF_REL}    "
    "@source=templates/claude_code/.project_manager/engine.manifest\n"
)
_OPENCODE_FLAVOR_MANIFEST = (
    ".project_manager/tools/board.py\n"
    ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n"
    ".opencode/lib    @source=templates/opencode/.opencode/lib\n"
    ".opencode/pm_orch_opencode.py    "
    "@source=templates/opencode/.opencode/pm_orch_opencode.py\n"
    f"{MANIFEST_SELF_REL}    "
    "@source=templates/opencode/.project_manager/engine.manifest\n"
)


def _make_guest_framework(root: Path) -> Path:
    """합성 프레임워크 — `templates/{claude_code,opencode}` 최소 manifest + 실 파일.

    실 프레임워크와 같이 `tools/pm_import.py` 사본을 둔다 — 훅 세트 세대 선언이 거기 살고,
    경로 스코프 가드가 상류 세대를 못 읽으면 어댑터 훅 영역 부분 전파를 fail-closed 로 거부한다
    (T-0610). 상류가 온전한 checkout 이라는 전제를 픽스처가 재현해야 그 가드의 정상 경로를 탄다."""
    for rel, text in _GUEST_FRAMEWORK_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    upstream_pm_import = root / ".project_manager" / "tools" / "pm_import.py"
    upstream_pm_import.parent.mkdir(parents=True, exist_ok=True)
    upstream_pm_import.write_text(
        (TOOLS / "pm_import.py").read_text(encoding="utf-8"), encoding="utf-8")
    (root / ".project_manager" / "engine.manifest").write_text(
        ".project_manager/tools/board.py\n", encoding="utf-8")
    for flavor, text in (("claude_code", _CLAUDE_FLAVOR_MANIFEST),
                         ("opencode", _OPENCODE_FLAVOR_MANIFEST)):
        manifest = root / "templates" / flavor / ".project_manager" / "engine.manifest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(text, encoding="utf-8")
    _track_source_tree(root)
    return root


def _make_legacy_guest_adopter(pm_update, dest: Path, *, core: str | None = None) -> Path:
    """claude host + **legacy guest 절**(렌더물 행만·`@source` provenance 없음) 채택자 dest.

    엔진 파일 사본은 설치 시점 값으로 얼려 둔다 — 이번 동기가 그것을 갱신해야 한다."""
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        (core if core is not None else _CLAUDE_FLAVOR_MANIFEST)
        + "\n" + pm_update._GUEST_MANIFEST_BEGIN
        + "\n.opencode/agents    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END + "\n",
        encoding="utf-8")
    for rel, text in (
        (".opencode/lib/relay.js", "// relay v1 (frozen)\n"),
        (".opencode/pm_orch_opencode.py", "# driver v1 (frozen)\n"),
        (".opencode/agents/pm.md", "adopter-owned agent\n"),
        (".claude/ctx_guard.py", "# ctx guard v2\n"),
        (".project_manager/tools/board.py", _ENGINE_SENTINEL),
    ):
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return manifest


def _guest_block_entries(pm_update, manifest: Path) -> dict:
    """dest guest 절 → {경로: ManifestEntry} (테스트 판독 편의)."""
    return {
        str(entry).replace("\\", "/"): entry
        for entry in pm_update._dest_guest_manifest_entries(manifest.parent.parent)
    }


def test_dest_guest_manifest_entries_keep_channel_markers(pm_update, tmp_path):
    """guest 절 파싱: 행마다 `@render`/`@target-owned`/`@source` 마커를 그대로 실어 온다.

    두 행 다 update 채널이 전파하되 **방식**이 갈린다(렌더물=재렌더·엔진 행=byte-copy) — 계획이
    그 방식을 고르려면 경로만이 아니라 마커까지 필요하다."""
    dest = tmp_path / "dest"
    _make_legacy_guest_adopter(pm_update, dest)
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            pm_update._GUEST_MANIFEST_END,
            ".opencode/lib    @target-owned    "
            "@source=templates/opencode/.opencode/lib\n"
            + pm_update._GUEST_MANIFEST_END),
        encoding="utf-8")

    entries = _guest_block_entries(pm_update, manifest)
    assert set(entries) == {".opencode/agents", ".opencode/lib"}
    assert entries[".opencode/agents"].render and entries[".opencode/agents"].target_owned
    assert not entries[".opencode/lib"].render and entries[".opencode/lib"].target_owned
    assert entries[".opencode/lib"].source_rel == "templates/opencode/.opencode/lib"


def test_guest_rows_join_plan_engine_copy_and_render_rerender(
        pm_update, tmp_path, monkeypatch, capsys):
    """guest 절의 두 행이 **같은 update 채널**로 전파된다 — 엔진 행은 byte-copy, 렌더물은 재렌더.

    엔진 행이 빠지면 `pm_relay` 코어와 짝인 드라이버가 설치 시점 사본으로 영구 동결되고, 렌더물이
    빠지면 conf 를 바꿔도 그 어댑터가 설치 시점 값으로 남는다(채택자 실측: 카드 model 이 conf 와
    영구 불일치). dry-run 은 그 재렌더를 `[render]` 로 예고한다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    manifest = _make_legacy_guest_adopter(pm_update, dest)
    # 실 add-harness 가 적는 provenance 를 붙인다 — source 가 해소돼야 재렌더가 실제로 돈다.
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            ".opencode/agents    @render @target-owned",
            ".opencode/agents    @render    @target-owned    "
            "@source=templates/opencode/.opencode/agents"),
        encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework), "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "[render] .opencode/agents/pm.md" in preview, \
        f"dry-run 이 guest 렌더물 재렌더를 예고하지 않았다: {preview!r}"
    assert (dest / ".opencode" / "agents" / "pm.md").read_text(
        encoding="utf-8") == "adopter-owned agent\n", "dry-run 이 파일을 바꿨다"

    assert pm_update.main(["--from", str(framework)]) == 0
    out = capsys.readouterr().out

    assert (dest / ".opencode" / "lib" / "relay.js").read_text(
        encoding="utf-8") == "// relay v2\n", "guest 엔진 파일이 동기되지 않았다(영구 동결)"
    assert (dest / ".opencode" / "pm_orch_opencode.py").read_text(
        encoding="utf-8") == "# driver v2\n", "engine-mirror 드라이버가 동기되지 않았다"
    assert (dest / ".opencode" / "agents" / "pm.md").read_text(
        encoding="utf-8") == "upstream agent\n", \
        "guest 렌더물이 재렌더를 안 받았다(설치 시점 사본 동결·손편집이 conf 를 이긴다)"
    assert "[render] .opencode/agents/pm.md" in out, f"렌더물이 계획에 없다: {out!r}"

    # 멱등 — 수렴 후 재실행은 변경 0(렌더 산출이 안정).
    assert pm_update.main(["--from", str(framework)]) == 0
    assert "최신 — 변경 없음" in capsys.readouterr().out


def test_guest_engine_backfill_persists_rows_once_then_idempotent(
        pm_update, tmp_path, monkeypatch, capsys):
    """legacy 절(provenance 0) → 파생 백필 1회 지속화 → 2차 실행 변경 0 (⑩).

    파생은 매 실행 돌지만 기록은 첫 실행에서 수렴한다 — 기록 후에는 provenance 로 해소되므로
    추론이 다시 발화하지 않고, 절/파일 어느 쪽도 churn 하지 않는다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    manifest = _make_legacy_guest_adopter(pm_update, dest)
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework)]) == 0
    first = capsys.readouterr().out
    assert "guest 엔진 행 2건 파생" in first, f"파생 보고 누락: {first!r}"

    entries = _guest_block_entries(pm_update, manifest)
    assert set(entries) == {".opencode/agents", ".opencode/lib",
                            ".opencode/pm_orch_opencode.py"}, entries
    assert entries[".opencode/lib"].source_rel == "templates/opencode/.opencode/lib", \
        "파생 행이 flavor provenance 없이 기록됐다(다음 실행이 또 추론한다)"
    assert entries[".opencode/agents"].render, "렌더물 행이 파생 병합으로 훼손됐다"

    after_first = manifest.read_bytes()
    assert pm_update.main(["--from", str(framework)]) == 0
    second = capsys.readouterr().out
    assert "최신 — 변경 없음" in second, f"2차 실행이 멱등하지 않다: {second!r}"
    assert "파생" not in second, f"기록 뒤에도 파생 보고 반복: {second!r}"
    assert manifest.read_bytes() == after_first, "절이 매 실행 churn"


def test_legacy_guest_first_run_reports_backfill_without_frozen_warning(
        pm_update, tmp_path, monkeypatch, capsys):
    """legacy 절 **첫 실행**: `미등재 flavor 파일 관측` 경고 0 + 파생 보고만 (자기모순 출력 제거).

    같은 run 이 "이 파일들은 어떤 동기 채널도 없다"고 경고한 직후 그 파일들을 등재·동기하면 채택자는
    무엇을 믿어야 할지 알 수 없다(제보 코호트 실형상). 파생 경로를 경고 판정 집합에 합쳐 첫 실행부터
    비발화시킨다 — 파생이 실제 채널을 만드므로 비발화가 참이다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    _make_legacy_guest_adopter(pm_update, dest)
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework)]) == 0
    captured = capsys.readouterr()

    assert "guest 엔진 행 2건 파생" in captured.out, f"파생 보고 누락: {captured.out!r}"
    assert "미등재 flavor 파일 관측" not in captured.err, \
        f"등재·동기하는 파일을 같은 run 이 '채널 없음' 으로 경고(자기모순): {captured.err!r}"
    assert "add-harness" not in captured.err, \
        f"불필요한 add-harness 재실행 안내(이미 update 채널이 처리한다): {captured.err!r}"
    # 비발화가 "탐지기를 껐다" 가 아니라 "채널이 생겼다" 임을 파일 동기로 확증.
    assert (dest / ".opencode" / "pm_orch_opencode.py").read_text(
        encoding="utf-8") == "# driver v2\n"


def test_guest_engine_backfill_dry_run_writes_nothing(
        pm_update, tmp_path, monkeypatch, capsys):
    """dry-run 은 파생만 하고 아무것도 쓰지 않는다 — 무변경 계약 유지 (⑩ 경계)."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    manifest = _make_legacy_guest_adopter(pm_update, dest)
    before_manifest = manifest.read_bytes()
    frozen = dest / ".opencode" / "pm_orch_opencode.py"
    before_driver = frozen.read_bytes()
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ".opencode/pm_orch_opencode.py" in out, \
        f"dry-run 계획에 guest 엔진 행이 없다(파생 미발화): {out!r}"
    assert manifest.read_bytes() == before_manifest, "dry-run 이 guest 절을 기록했다"
    assert frozen.read_bytes() == before_driver, "dry-run 이 파일을 덮었다"


def test_infer_guest_flavors_ignores_cross_namespace_shared_paths(pm_update):
    """cross-ns 공유 경로는 flavor 증거가 아니다 — 없던 인스턴스에 파일을 만들지 않는다 (⑪).

    codex host + opencode guest 의 절은 `.claude/skills`(opencode 네이티브 소비·claude flavor 도
    선언)를 담는다. 단순 namespace 매칭이면 claude 로 오인해 claude 어댑터 엔진 파일 전량을
    생성한다 — 배타 경로 증거만 쓰면 그 클래스가 구조적으로 닫힌다. 출하 manifest 실물 픽스처."""
    codex_guest = {".codex/agents", ".agents/skills"}
    opencode_guest = {".opencode/agents", ".opencode/pm-instructions.md", ".claude/skills"}

    assert pm_update._infer_guest_flavors(REPO, opencode_guest) == ["opencode"], \
        "cross-ns `.claude/skills` 가 claude_code 증거로 오인됐다"
    assert pm_update._infer_guest_flavors(REPO, codex_guest) == ["codex"]
    assert pm_update._infer_guest_flavors(REPO, {".claude/agents"}) == ["claude_code"]
    assert pm_update._infer_guest_flavors(REPO, set()) == []


def test_guest_engine_backfill_resolves_mixed_generation_cohort(pm_update, tmp_path):
    """새 세대(provenance 有)와 구 세대(렌더물 행만) guest 가 **공존**해도 둘 다 해소된다.

    하네스를 순차로 얹고 하나만 refresh 한 인스턴스가 이 형상이다. "선언이 하나라도 있으면 추론
    비발화" 였다면 구 세대 쪽 엔진 행이 영구 미등재로 남아 정확히 이 채널이 닫으려던 동결이
    존속한다. 해소 = 선언 ∪ 미선언분 추론이며, 추론 판정 자체(배타 경로 증거)는 그대로라
    cross-ns 오탐 가드도 함께 성립해야 한다. 출하 manifest 실물 픽스처."""
    claude_core = (
        REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    ).read_text(encoding="utf-8")
    dest = tmp_path / "mixed-cohort"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        claude_core + "\n" + pm_update._GUEST_MANIFEST_BEGIN
        # codex — 새 세대(엔진 행 + provenance).
        + "\n.codex/agents    @render    @target-owned    "
        "@source=templates/codex/.codex/agents"
        + "\n.codex/pm_orch_codex.py    @target-owned    "
        "@source=templates/codex/.codex/pm_orch_codex.py"
        # opencode — 구 세대(렌더물 행만·provenance 0).
        + "\n.opencode/agents    @render @target-owned"
        + "\n.claude/skills    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END + "\n",
        encoding="utf-8")

    guest_entries = pm_update._dest_guest_manifest_entries(dest)
    backfill, flavors = pm_update._guest_engine_backfill_entries(
        dest, REPO, guest_entries)
    paths = {str(entry).replace("\\", "/") for entry in backfill}

    assert flavors == ["codex", "opencode"], \
        f"혼재 코호트에서 구 세대 flavor 가 해소되지 않음(영구 동결): {flavors}"
    assert {".opencode/lib", ".opencode/plugins", ".opencode/pm_orch_opencode.py",
            ".opencode/.gitignore"} <= paths, \
        f"구 세대 guest 의 엔진 행이 파생되지 않음: {sorted(paths)}"
    assert ".codex/pm_orch_codex.py" in paths, \
        f"선언된 flavor 의 엔진 행이 함께 파생되지 않음: {sorted(paths)}"
    # cross-ns 오탐 가드는 혼재 코호트에서도 유지 — `.claude/skills` 는 claude 증거가 아니다.
    assert "claude_code" not in flavors, f"cross-ns 행으로 flavor 오인: {flavors}"
    assert not [p for p in paths if p.startswith(".claude/")], \
        f"없던 하네스의 파일을 만들 파생 행: {sorted(paths)}"


def test_guest_engine_backfill_cross_namespace_creates_no_foreign_files(
        pm_update, tmp_path, monkeypatch, capsys):
    """codex host + opencode legacy guest → claude 어댑터 파일이 생기지 않는다 (⑪ 롤아웃)."""
    codex_core = (
        REPO / "templates" / "codex" / ".project_manager" / "engine.manifest"
    ).read_text(encoding="utf-8")
    dest = tmp_path / "codex-host"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        codex_core + "\n" + pm_update._GUEST_MANIFEST_BEGIN
        + "\n.opencode/agents    @render @target-owned"
        + "\n.claude/skills    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END + "\n",
        encoding="utf-8")

    guest_entries = pm_update._dest_guest_manifest_entries(dest)
    backfill, flavors = pm_update._guest_engine_backfill_entries(
        dest, REPO, guest_entries)

    assert flavors == ["opencode"], f"cross-ns 행으로 flavor 오인: {flavors}"
    foreign = sorted(str(e) for e in backfill if str(e).startswith(".claude/"))
    assert not foreign, f"없던 하네스의 파일을 만들 파생 행: {foreign}"
    assert {str(e) for e in backfill} == {
        ".opencode/lib", ".opencode/plugins", ".opencode/pm_orch_opencode.py",
        ".opencode/.gitignore"}, sorted(str(e) for e in backfill)


def test_guest_engine_backfill_not_persisted_when_legacy_preserved(
        pm_update, tmp_path, monkeypatch, capsys):
    """`legacy_preserved`(로컬 manifest 불가침)면 전파는 하되 절 기록만 생략 + 안내 (⑫)."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    # 후보 어느 것과도 exact-match 하지 않는 legacy core(=@source 선언 0·로컬 고유 경로 포함).
    manifest = _make_legacy_guest_adopter(
        pm_update, dest,
        core=".project_manager/tools/board.py\n.local/custom.md\n")
    (dest / ".local").mkdir(parents=True, exist_ok=True)
    (dest / ".local" / "custom.md").write_text("local\n", encoding="utf-8")
    (framework / ".local").mkdir(parents=True, exist_ok=True)
    (framework / ".local" / "custom.md").write_text("upstream\n", encoding="utf-8")
    _track_source_tree(framework)
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework)]) == 0
    captured = capsys.readouterr()
    assert "정확히 하나와 완전 일치하지 않는다" in captured.err, \
        f"픽스처 전제 붕괴 — legacy_preserved 가 아니다: {captured.err!r}"
    assert "guest 절에는 기록하지 않는다" in captured.err, \
        f"legacy_preserved 안내 누락: {captured.err!r}"

    # 파일 동기는 수행 — 동결 사본이 갱신됐다.
    assert (dest / ".opencode" / "lib" / "relay.js").read_text(
        encoding="utf-8") == "// relay v2\n", "legacy_preserved 에서 파일 동기까지 멈췄다"
    # 절은 원형 유지(선언 존중).
    assert set(_guest_block_entries(pm_update, manifest)) == {".opencode/agents"}, \
        "legacy_preserved 인데 guest 절이 다시 쓰였다"


def test_guest_source_declarations_do_not_promote_flavor_union(pm_update, tmp_path):
    """guest 절의 `@source` 는 flavor 선택 선언으로 새지 않는다 (⑥·불변식).

    새면 설치하지 않은 flavor 의 upstream manifest 가 core 합집합으로 승격돼 채택자 TOML override
    까지 클로버한다 — 승격 축은 **core 선언만** 본다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    manifest = _make_legacy_guest_adopter(pm_update, dest)
    # 절에 opencode provenance 를 심는다(백필 지속화 후 형상).
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            pm_update._GUEST_MANIFEST_END,
            ".opencode/lib    @target-owned    "
            "@source=templates/opencode/.opencode/lib\n"
            + pm_update._GUEST_MANIFEST_END),
        encoding="utf-8")

    selfheal = pm_update.resolve_manifest_selfheal(dest, framework)
    selected = [p.as_posix() for p in selfheal["upstream_manifests"]]
    assert selected == [
        (framework / "templates" / "claude_code" / ".project_manager"
         / "engine.manifest").as_posix()
    ], f"guest `@source` 가 flavor 합집합 승격을 발화시켰다: {selected}"


def test_copy_manifest_preserving_guest_backfills_and_prunes_promoted(pm_update, tmp_path):
    """절 재부착이 파생 엔진 행을 기록하고, core 로 승격된 경로는 차감한다 (⑦).

    마커 세대 마이그레이션 → 백필 순서로 조합된다(구 세대 절에도 파생이 도달)."""
    upstream = tmp_path / "up.manifest"
    dest_manifest = tmp_path / "dest.manifest"
    upstream.write_text(
        ".project_manager/tools/board.py\n"
        ".opencode/plugins    @source=templates/opencode/.opencode/plugins\n",
        encoding="utf-8")
    dest_manifest.write_text(
        "# core\n\n" + GUEST_BEGIN_LITERAL_LEGACY_V1_4_3
        + "\n.opencode/agents    @render @target-owned"
        + "\n.opencode/plugins    @target-owned\n"
        + GUEST_END_LITERAL + "\n",
        encoding="utf-8")

    pm_update._copy_manifest_preserving_guest(
        upstream, dest_manifest,
        [".opencode/lib    @target-owned    @source=templates/opencode/.opencode/lib"])

    text = dest_manifest.read_text(encoding="utf-8")
    block = pm_update._extract_guest_manifest_block(text)
    assert block is not None and block.startswith(GUEST_BEGIN_LITERAL_CURRENT), \
        "마커 세대 마이그레이션이 백필 조합에서 유실됐다"
    paths = {line.split()[0] for line in block.splitlines()
             if line.strip() and not line.strip().startswith("#")}
    assert ".opencode/lib" in paths, "파생 엔진 행이 기록되지 않았다"
    assert ".opencode/agents" in paths, "렌더물 행이 백필 병합으로 사라졌다"
    assert ".opencode/plugins" not in paths, \
        "upstream core 로 승격된 경로가 guest 절에 남았다(이중 등재·영구 skip)"
    assert "@source=templates/opencode/.opencode/lib" in block


def test_merge_guest_backfill_lines_is_noop_without_block(pm_update):
    """절이 없으면 백필이 절을 새로 만들지 않는다 — 파생은 절의 존재를 전제한다 (⑦ 경계)."""
    assert pm_update._merge_guest_backfill_lines(None, [".x    @target-owned"]) is None
    block = (pm_update._GUEST_MANIFEST_BEGIN + "\n.a    @render @target-owned\n"
             + pm_update._GUEST_MANIFEST_END)
    assert pm_update._merge_guest_backfill_lines(block, []) == block


def test_guest_engine_row_missing_source_skips_with_rc0(
        pm_update, tmp_path, monkeypatch, capsys):
    """upstream flavor 가 그 엔진 파일을 안 들고 있으면 loud `[skip]` + rc0 (⑧).

    `@target-owned` 가 없었다면 rc2 로 전체 동기가 막혀, 상류가 파일 하나를 은퇴시킬 때마다
    guest 를 얹은 채택자가 업데이트 불능이 된다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    manifest = _make_legacy_guest_adopter(pm_update, dest)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            pm_update._GUEST_MANIFEST_END,
            ".opencode/retired.py    @target-owned    "
            "@source=templates/opencode/.opencode/retired.py\n"
            + pm_update._GUEST_MANIFEST_END),
        encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(framework)]) == 0, \
        "상류 부재 guest 엔진 행이 rc2 로 전체를 막았다(@target-owned 누락)"
    out = capsys.readouterr().out
    assert "[skip] .opencode/retired.py" in out, f"조용한 skip(loud 아님): {out!r}"


def test_scope_paths_accepts_guest_engine_row_before_persistence(
        pm_update, tmp_path, monkeypatch, capsys):
    """`--paths` 가 파생 엔진 행 경로를 오거부하지 않는다 (⑨).

    선검증은 **파일만** 읽으므로, 영속화 전 첫 실행에서는 파생분이 어느 manifest 에도 없다 —
    합집합에 넣지 않으면 이번 실행이 실제로 전파할 경로를 미등재로 거부한다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    _make_legacy_guest_adopter(pm_update, dest)
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    rc = pm_update.main(
        ["--from", str(framework), "--paths", ".opencode/pm_orch_opencode.py"])
    captured = capsys.readouterr()
    assert rc == 0, f"파생 엔진 행 경로가 오거부됐다: {captured.err!r}"
    assert (dest / ".opencode" / "pm_orch_opencode.py").read_text(
        encoding="utf-8") == "# driver v2\n"
    # 렌더물 행도 같은 update 채널이라 스코프로 지정할 수 있다(옛 "guest 절 소속" 오거부 소멸).
    rc_render = pm_update.main(
        ["--from", str(framework), "--paths", ".opencode/agents"])
    assert rc_render == 0, "guest 렌더물 경로가 스코프에서 오거부됐다"
    assert "[미등재]" not in capsys.readouterr().err


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
    _track_source_tree(source)
    # 채택자(dest) manifest — flavor self-prop(@source)·구형(NEW_ENGINE_REL 미등재).
    _write_dest_manifest(tmp_path / "dest", [SENTINEL_REL, flavor_self])

    result = pm_update.resolve_manifest_selfheal(tmp_path / "dest", source)
    assert result["status"] == "heal", \
        f"flavor 채택자가 flavor upstream 과 자기치유 안 됨(root 대조로 diverged?): {result['status']}"
    assert result["added"] == [NEW_ENGINE_REL]
    selfprop = [e for e in result["manifest"] if str(e) == MANIFEST_SELF_REL]
    assert selfprop and selfprop[0].source_rel == flavor_rel, \
        "승격 manifest 의 self-prop 이 flavor @source 를 보존 안 함(root bare 로 클로버)"


def test_selected_manifest_order_is_primary_then_declared_flavors_without_selfprop_noise(
        pm_update, tmp_path, capsys):
    """opencode-primary 합집합 순서는 고정하되 구조상 정상인 self-prop 충돌은 경고하지 않는다."""
    opencode = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    claude = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    merged = pm_update.merge_manifest_sources([opencode, claude])
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(merged["text"], encoding="utf-8")

    result = pm_update.resolve_manifest_selfheal(dest, REPO)

    assert [path.parents[1].name for path in result["upstream_manifests"]] == [
        "opencode", "claude_code",
    ]
    pm_update._print_manifest_merge_conflicts(result)
    err = capsys.readouterr().err
    assert ".project_manager/engine.manifest" not in err
    assert "선언 순서상 첫 flavor" not in err


def test_manifest_merge_conflict_warning_surfaces_real_marker_conflict(
        pm_update, tmp_path, capsys):
    """실 중복 경로의 마커 충돌은 첫 flavor 우선 정책과 함께 stderr에 반드시 발화한다."""
    first = tmp_path / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = tmp_path / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(".shared/x    @render\n", encoding="utf-8")
    second.write_text(".shared/x\n", encoding="utf-8")

    merged = pm_update.merge_manifest_sources([first, second])
    assert merged["conflicts"] == [".shared/x"]

    pm_update._print_manifest_merge_conflicts({"merge_conflicts": merged["conflicts"]})
    err = capsys.readouterr().err
    assert "선언 순서상 첫 flavor" in err
    assert ".shared/x" in err


def test_manifest_merge_selfprop_ignores_only_source_difference(pm_update, tmp_path):
    """self-prop은 @source 차이만 정상 소음이고 render/target-owned 차이는 실제 충돌이다."""
    first = tmp_path / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = tmp_path / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        f"{MANIFEST_SELF_REL}    @render "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )

    merged = pm_update.merge_manifest_sources([first, second])
    assert merged["conflicts"] == [MANIFEST_SELF_REL]


def test_declared_second_flavor_missing_manifest_is_preserved_and_warned(
        pm_update, tmp_path, capsys):
    """선언된 후순위 flavor manifest 부재를 조용히 drop하지 않고 선택 목록+로컬 union으로 보존한다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/a    @source=templates/second/.second/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(
        pm_update.merge_manifest_sources([first, second])["text"], encoding="utf-8"
    )
    second.unlink()

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "upstream_missing"
    assert result["upstream_manifests"] == [first, second], \
        "부재한 후순위 선언이 candidate 필터에서 조용히 drop됨"
    assert ".second/a" in manifest.read_text(encoding="utf-8"), \
        "upstream 해소 실패가 로컬 union을 변경함"
    err = capsys.readouterr().err
    assert "후순위 flavor의 upstream manifest가 없다" in err
    assert "선언을 버리지 않고 로컬 union을 유지" in err


def test_declared_manifest_warns_on_undeclared_flavor_tree_without_promoting(
        pm_update, tmp_path, capsys):
    """선언 manifest도 타 flavor 관리-고유 경로를 보면 frozen 의심을 알리되 자동 선택하지 않는다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/a    @source=templates/second/.second/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        first.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (dest / ".second").mkdir()
    (dest / ".second" / "a").write_text("frozen\n", encoding="utf-8")

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "in_sync"
    assert result["upstream_manifests"] == [first]
    assert second not in result["upstream_manifests"]
    err = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" in err
    assert "@source 선택 선언이 있는 manifest" in err
    assert "선언되지 않은 타 flavor(second)" in err
    assert "(1/1: .second/a)" in err
    assert "`add-harness second`는 그 하네스 어댑터만 등재한다" in err
    assert "<manager>/pm-import.sh --into <project> --harness all --dry-run" in err
    assert "cd <project> && ./pm-update.sh" in err


def test_declared_manifest_excludes_guest_paths_but_warns_on_non_guest_file(
        pm_update, tmp_path, capsys):
    """guest 소유 경로만 evidence에서 빠지고, guest 밖 flavor 고유 파일이 관측되면 경고가 난다.

    옛 동작은 guest 소유 경로 하나로 그 flavor 전체를 evidence에서 버려, add-harness 채택자
    (항상 guest 행이 있다)에게 frozen 경고가 구조적으로 발화하지 못했다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/render    @render @source=templates/second/.second/render\n"
        ".second/hook    @source=templates/second/.second/hook\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(
        first.read_text(encoding="utf-8")
        + "\n"
        + pm_update._GUEST_MANIFEST_BEGIN
        + "\n.second/render    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END
        + "\n",
        encoding="utf-8",
    )
    (dest / ".second").mkdir()
    (dest / ".second" / "render").write_text("guest\n", encoding="utf-8")
    (dest / ".second" / "hook").write_text("guest hook\n", encoding="utf-8")

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["upstream_manifests"] == [first], \
        "경고는 자동 승격이 아니다 — 선택 목록은 선언된 flavor 그대로여야 한다"
    err = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" in err
    assert "선언되지 않은 타 flavor(second)" in err
    assert "(1/1: .second/hook)" in err, \
        f"guest 밖 고유 파일만 evidence여야 한다(guest .second/render 제외): {err!r}"
    assert ".second/render" not in err, \
        "guest 소유 경로가 frozen evidence에 섞임(add-harness refresh 채널 관할)"


def test_add_harness_adopter_shape_backfill_silences_frozen_warning(
        pm_update, tmp_path, capsys):
    """채택자 실형상(claude host + 구 세대 add-harness codex 절) — 경고 대신 **채널**이 생긴다.

    옛 형상에선 flavor 의 `@render` 선언만 guest 로 등재돼 `.codex/pm_orch_codex.py`(비-@render)가
    동결됐고, 그 동결을 frozen 경고가 표면화했다. 파생 백필이 같은 실행에서 그 파일을 등재·동기하는
    지금은 경고가 **자기모순**이다(제보 채택자 코호트가 정확히 이 형상) — 판정 집합에 파생 경로를
    합쳐 첫 실행부터 비발화시킨다.

    민감도: 파생이 실제로 그 경로를 담는지 함께 단언해, "경고가 없다" 가 "채널이 생겼다" 로만
    성립하게 한다(판정 집합만 넓히고 파생을 잃은 회귀는 red)."""
    claude = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(
        claude.read_text(encoding="utf-8")
        + "\n"
        + pm_update._GUEST_MANIFEST_BEGIN
        + "\n.codex/agents    @render @target-owned"
        + "\n.agents/skills    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END
        + "\n",
        encoding="utf-8",
    )
    (dest / ".codex").mkdir()
    (dest / ".codex" / "pm_orch_codex.py").write_text("# frozen\n", encoding="utf-8")

    backfill, flavors = pm_update._guest_engine_backfill_entries(
        dest, REPO, pm_update._dest_guest_manifest_entries(dest))
    assert flavors == ["codex"], f"구 세대 절에서 flavor 미해소: {flavors}"
    assert ".codex/pm_orch_codex.py" in {str(e).replace("\\", "/") for e in backfill}, \
        "동결됐던 파일이 파생에 없다(채널 부재인데 경고만 사라지면 회귀다)"

    pm_update.resolve_manifest_selfheal(dest, REPO)

    err = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" not in err, \
        f"이번 실행이 등재·동기할 파일을 '채널 없음' 으로 경고(자기모순): {err!r}"


def test_frozen_warning_silent_when_all_flavor_paths_are_guest_owned(
        pm_update, tmp_path, capsys):
    """flavor 고유 경로가 전부 guest 소유면 경고 0 — 순정 add-harness 채택자 오탐 방지."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/render    @render @source=templates/second/.second/render\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(
        first.read_text(encoding="utf-8")
        + "\n"
        + pm_update._GUEST_MANIFEST_BEGIN
        + "\n.second/render    @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END
        + "\n",
        encoding="utf-8",
    )
    (dest / ".second").mkdir()
    (dest / ".second" / "render").write_text("guest\n", encoding="utf-8")

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["upstream_manifests"] == [first]
    assert "미등재 flavor 파일 관측" not in capsys.readouterr().err, \
        "guest가 전부 커버하는 flavor에 오탐 경고(add-harness refresh 채널이 전담)"


def test_frozen_evidence_drops_only_guest_owned_paths(pm_update):
    """evidence 계산 유닛: guest 소유는 개별 제외, 전부 guest면 None(그때만 skip)."""
    entries = [
        pm_update.ManifestEntry(".second/render"),
        pm_update.ManifestEntry(".second/hook"),
    ]

    assert pm_update._frozen_flavor_evidence(
        entries, set(), set(), {".second/render"}) == [".second/hook"]
    assert pm_update._frozen_flavor_evidence(
        entries, set(), set(), {".second"}) is None, \
        "고유 경로 전부가 guest 소유(상위 포함)면 None이어야 한다"


def test_frozen_evidence_drops_paths_covered_by_host_core_directory(pm_update):
    """evidence 계산 유닛: host core **디렉터리 등재**가 덮는 파일은 제외, 진짜 stray는 남는다.

    manifest 등재는 파일과 디렉터리 양쪽이라 exact 문자열 대조는 `.claude/skills` 디렉터리 등재가
    이미 덮고 실제로 render 되는 `.claude/skills/pm-dev-delegate/SKILL.md`를 '어느 동기 채널도
    선언하지 않은 출하 파일'로 분류했다(claude 단독 채택자 오탐)."""
    entries = [
        pm_update.ManifestEntry(".claude/skills/pm-dev-delegate/SKILL.md"),
        pm_update.ManifestEntry(".second/stray"),
    ]

    assert pm_update._frozen_flavor_evidence(
        entries, set(), {".claude/skills"}, set()) == [".second/stray"], \
        "host core 디렉터리 등재가 덮는 파일이 evidence에 남았다(오탐) — stray만 남아야 한다"
    assert pm_update._frozen_flavor_evidence(
        entries, set(),
        {".claude/skills/pm-dev-delegate/SKILL.md"}, set()) == [".second/stray"], \
        "exact 등재 제외는 그대로 유지돼야 한다"
    assert pm_update._frozen_flavor_evidence(
        entries, set(), set(), set()) == [
            ".claude/skills/pm-dev-delegate/SKILL.md", ".second/stray"], \
        "어느 core 등재로도 안 덮이면 둘 다 evidence다(판정이 무뎌지면 red)"


def test_frozen_warning_silent_for_core_directory_covered_flavor_file(
        pm_update, tmp_path, capsys):
    """host core 디렉터리 등재가 덮는 타 flavor 파일은 경고 0 · 안 덮이는 stray는 여전히 경고.

    채택자 실형상(claude host + `.agents/skills` 디렉터리 등재)에서 codex manifest의 파일 단위
    override(`.agents/skills/pm-dev-delegate/SKILL.md`)가 그 디렉터리 등재로 이미 동기되는데도
    '어느 동기 채널도 선언하지 않은 출하 파일'로 경고됐다. 같은 실행에서 core가 안 덮는
    `.codex/pm_orch_codex.py`는 경고 대상으로 남아, 오탐 제거가 탐지 무력화가 아님을 고정한다."""
    claude = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(
        claude.read_text(encoding="utf-8")
        + "\n.agents/skills    @render @source=.claude/skills\n",
        encoding="utf-8",
    )
    shared_skill = dest / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    shared_skill.parent.mkdir(parents=True)
    shared_skill.write_text("# rendered by .agents/skills\n", encoding="utf-8")

    pm_update.resolve_manifest_selfheal(dest, REPO)

    err = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" not in err, \
        f"core 디렉터리 등재가 덮는 파일을 '채널 없음'으로 경고(오탐): {err!r}"

    stray = dest / ".codex" / "pm_orch_codex.py"
    stray.parent.mkdir(parents=True)
    stray.write_text("# stray\n", encoding="utf-8")

    pm_update.resolve_manifest_selfheal(dest, REPO)

    err = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" in err, \
        "어느 등재로도 안 덮이는 타 flavor 파일은 여전히 경고여야 한다"
    assert ".codex/pm_orch_codex.py" in err
    assert ".agents/skills/pm-dev-delegate/SKILL.md" not in err, \
        "디렉터리 등재가 덮는 파일이 관측 목록에 섞였다"


def test_claude_only_adopter_shape_stays_quiet_across_repeat_runs(
        pm_update, tmp_path, capsys):
    """claude 단독 채택자(순정 host manifest) — 초회·수렴 두 실행 모두 frozen 경고 0.

    제보 형상: opencode manifest가 `.claude/skills/pm-dev-delegate/SKILL.md`에 파일 단위 override를
    두는데 그 경로는 host `.claude/skills @render`가 덮는다. opencode를 설치한 적 없는 채택자가 매
    실행 경고를 받았고, 처방이 `add-harness opencode`/전체 재-import라 무해한 형상에 파괴적이었다."""
    claude = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [])
    manifest.write_text(claude.read_text(encoding="utf-8"), encoding="utf-8")
    skill = dest / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# rendered by .claude/skills\n", encoding="utf-8")

    for run in ("초회", "수렴"):
        result = pm_update.resolve_manifest_selfheal(dest, REPO)
        assert result["upstream_manifests"] == [claude]
        err = capsys.readouterr().err
        assert "미등재 flavor 파일 관측" not in err, \
            f"{run} 실행에서 claude 단독 채택자 오탐: {err!r}"


def test_declared_single_flavor_without_other_tree_has_no_frozen_warning(
        pm_update, tmp_path, capsys):
    """순정 단일-flavor 선언 채택자는 자기 flavor만 선택하며 frozen 경고가 없다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/a    @source=templates/second/.second/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        first.read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["upstream_manifests"] == [first]
    assert "미등재 flavor 파일 관측" not in capsys.readouterr().err


def test_unresolvable_source_declaration_disables_legacy_presence_fallback(
        pm_update, tmp_path, capsys):
    """해소 불가 @source가 하나라도 있으면 타 flavor tree 실재에도 [primary]만 유지하고 경고한다."""
    source = tmp_path / "source"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/a    @source=templates/second/.second/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".second").mkdir()
    (dest / ".second" / "a").write_text("stray\n", encoding="utf-8")
    (dest / ".project_manager" / "engine.manifest").write_text(
        first.read_text(encoding="utf-8")
        + ".mystery/a    @source=templates/no_such/.mystery/a\n"
        + ".mystery/b    @source=templates/no_such/.mystery/a\n",
        encoding="utf-8",
    )

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["upstream_manifests"] == [first]
    assert second not in result["upstream_manifests"]
    err = capsys.readouterr().err
    assert "해소할 수 없는 @source 선언" in err
    assert "legacy 존재-휴리스틱을 사용하지 않는다" in err
    assert "primary manifest만 유지" in err
    assert err.count("templates/no_such/.mystery/a") == 1, \
        f"같은 unresolved 선언이 경고에 중복 출력됨: {err!r}"


def test_legacy_candidate_manifest_non_utf8_is_fail_soft(
        pm_update, tmp_path, capsys):
    """legacy 후보 하나가 비-UTF8이어도 traceback 없이 그 후보만 제외하고 primary를 해소한다."""
    source = tmp_path / "source"
    primary = source / "templates" / "primary" / ".project_manager" / "engine.manifest"
    other = source / "templates" / "other" / ".project_manager" / "engine.manifest"
    broken = source / "templates" / "broken" / ".project_manager" / "engine.manifest"
    for path in (primary, other, broken):
        path.parent.mkdir(parents=True)
    primary.write_text(
        ".primary/a    @source=templates/primary/.primary/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/primary/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    other.write_text(
        ".other/a    @source=templates/other/.other/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/other/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    broken.write_bytes(b"\xff\xfe\xfa")
    dest = tmp_path / "dest"
    (dest / ".primary").mkdir(parents=True)
    (dest / ".primary" / "a").write_text("installed\n", encoding="utf-8")
    _write_dest_manifest(dest, [".primary/a", MANIFEST_SELF_REL])

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "heal"
    assert result["upstream_manifests"] == [primary]
    err = capsys.readouterr().err
    assert "후보 manifest를 읽을 수 없어 제외한다(fail-soft)" in err
    assert str(broken) in err


def test_legacy_exact_path_set_must_match_exactly_one_candidate(
        pm_update, tmp_path, capsys):
    """동일 경로 집합 후보가 둘이면 flavor 정보가 0이므로 tiebreak 없이 로컬 manifest를 보존한다."""
    source = tmp_path / "source"
    root = source / ".project_manager" / "engine.manifest"
    root.parent.mkdir(parents=True)
    root.write_text(f"{MANIFEST_SELF_REL}\n", encoding="utf-8")
    for flavor in ("first", "second"):
        candidate = (
            source / "templates" / flavor / ".project_manager" / "engine.manifest")
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            ".shared/a\n"
            f"{MANIFEST_SELF_REL}    "
            f"@source=templates/{flavor}/.project_manager/engine.manifest\n",
            encoding="utf-8",
        )
    dest = tmp_path / "dest"
    manifest = _write_dest_manifest(dest, [".shared/a", MANIFEST_SELF_REL])
    before = manifest.read_bytes()

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "legacy_preserved"
    assert result["manifest"] is None
    assert manifest.read_bytes() == before
    err = capsys.readouterr().err
    assert "정확히 하나와 완전 일치하지 않는다" in err
    assert "자동 flavor 승격·행 제거·치유 0" in err
    assert "배타적 flavor 경로 관측 0" in err
    assert "<manager>/pm-import.sh --into <project> --harness all --dry-run" in err, \
        "관측 flavor가 0개여도 빈 --harness가 아닌 안전한 migration 안내가 필요함"


_RETIRE_OLD = ".project_manager/tools/external_review.py"
_RETIRE_NEW = ".project_manager/tools/additional_reviewer.py"


def _write_retirement_flavor(source, flavor, *, selfprop=False):
    manifest = source / "templates" / flavor / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    lines = [".shared/a", _RETIRE_NEW]
    if selfprop:
        lines.append(
            f"{MANIFEST_SELF_REL}    "
            f"@source=templates/{flavor}/.project_manager/engine.manifest"
        )
    lines.append(f"# pm-retired-path: {_RETIRE_OLD} -> {_RETIRE_NEW}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    replacement = source / _RETIRE_NEW
    replacement.parent.mkdir(parents=True, exist_ok=True)
    replacement.write_text("new reviewer\n", encoding="utf-8")
    return manifest


def test_retirement_inverse_exact_match_selects_unique_legacy_flavor_and_heals(
        pm_update, tmp_path):
    source = tmp_path / "source"
    root = source / ".project_manager" / "engine.manifest"
    root.parent.mkdir(parents=True)
    root.write_text(f"{MANIFEST_SELF_REL}\n", encoding="utf-8")
    selected = _write_retirement_flavor(source, "selected")
    other = source / "templates" / "other" / ".project_manager" / "engine.manifest"
    other.parent.mkdir(parents=True)
    other.write_text(".other/a\n", encoding="utf-8")
    dest = tmp_path / "dest"
    _write_dest_manifest(dest, [".shared/a", _RETIRE_OLD])

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "heal"
    assert result["upstream_manifests"] == [selected]
    assert result["added"] == [_RETIRE_NEW]
    assert result["removed"] == []
    assert result["retired_removed"] == [_RETIRE_OLD]


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_retirement_inverse_requires_exactly_one_candidate(
        pm_update, tmp_path, candidate_count):
    source = tmp_path / "source"
    root = source / ".project_manager" / "engine.manifest"
    root.parent.mkdir(parents=True)
    root.write_text(f"{MANIFEST_SELF_REL}\n", encoding="utf-8")
    for index in range(max(candidate_count, 1)):
        _write_retirement_flavor(source, f"flavor-{index}")
    dest = tmp_path / "dest"
    local = [".shared/a", _RETIRE_OLD]
    if candidate_count == 0:
        local.append(".local/unapproved")
    _write_dest_manifest(dest, local)

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "legacy_preserved"
    assert result["manifest"] is None
    assert result["retired_removed"] == []


def test_retirement_filter_keeps_undeclared_local_only_path_diverged(
        pm_update, tmp_path):
    source = tmp_path / "source"
    selected = _write_retirement_flavor(source, "selected", selfprop=True)
    dest = tmp_path / "dest"
    selfprop = (
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/selected/.project_manager/engine.manifest"
    )
    _write_dest_manifest(
        dest, [".shared/a", _RETIRE_OLD, ".local/unapproved", selfprop]
    )

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "diverged"
    assert result["upstream_manifests"] == [selected]
    assert result["removed"] == [".local/unapproved"]
    assert result["retired_removed"] == [_RETIRE_OLD]
    assert result["manifest"] is None


def test_declared_flavor_validates_only_selected_retirement_manifests(
        pm_update, tmp_path):
    source = tmp_path / "source"
    selected = _write_retirement_flavor(source, "selected", selfprop=True)
    unrelated = source / "templates/unrelated/.project_manager/engine.manifest"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        ".other/a\n# pm-retired-path: malformed\n", encoding="utf-8"
    )
    dest = tmp_path / "dest"
    _write_dest_manifest(dest, [
        ".shared/a",
        _RETIRE_OLD,
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/selected/.project_manager/engine.manifest",
    ])

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "heal"
    assert result["upstream_manifests"] == [selected]
    assert result["retired_removed"] == [_RETIRE_OLD]


@pytest.mark.parametrize("shape,expected", [
    ("unowned", "manifest-owned"),
    ("render", "bare byte-copy"),
    ("missing", "regular file"),
])
def test_validated_retirement_rejects_unowned_marked_or_missing_replacement(
        pm_update, tmp_path, shape, expected):
    source = tmp_path / shape
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    row = "" if shape == "unowned" else _RETIRE_NEW
    if shape == "render":
        row += "    @render"
    manifest.write_text(
        (row + "\n" if row else "")
        + f"# pm-retired-path: {_RETIRE_OLD} -> {_RETIRE_NEW}\n",
        encoding="utf-8",
    )
    if shape == "render":
        replacement = source / _RETIRE_NEW
        replacement.parent.mkdir(parents=True)
        replacement.write_text("new\n", encoding="utf-8")

    with pytest.raises(pm_update.RetiredPathError, match=expected):
        pm_update.validated_retired_path_directives(source, [manifest])


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink 권한과 무관한 POSIX 경계")
def test_validated_retirement_rejects_symlink_replacement(pm_update, tmp_path):
    source = tmp_path / "source"
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"{_RETIRE_NEW}\n"
        f"# pm-retired-path: {_RETIRE_OLD} -> {_RETIRE_NEW}\n",
        encoding="utf-8",
    )
    target = source / "payload.py"
    target.write_text("payload\n", encoding="utf-8")
    replacement = source / _RETIRE_NEW
    replacement.parent.mkdir(parents=True, exist_ok=True)
    replacement.symlink_to(target)

    with pytest.raises(pm_update.RetiredPathError, match="symlink/reparse|regular file"):
        pm_update.validated_retired_path_directives(source, [manifest])


def test_legacy_opencode_proper_subset_does_not_promote_or_rewrite_manifest(
        pm_update, tmp_path):
    """@source 없는 opencode legacy의 한 줄 누락은 exact-match가 아니므로 불가침이다."""
    source = REPO
    upstream = source / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    local_lines = [
        (line.split(" @source=", 1)[0] if " @source=" in line else line)
        for line in upstream.read_text(encoding="utf-8").splitlines()
        if not line.startswith(".opencode/.gitignore")
    ]
    dest = tmp_path / "legacy-opencode"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("\n".join(local_lines) + "\n", encoding="utf-8")
    before = manifest.read_bytes()

    result = pm_update.resolve_manifest_selfheal(dest, source)

    assert result["status"] == "legacy_preserved"
    assert result["manifest"] is None
    assert result["added"] == []
    assert result["removed"] == []
    assert result["upstream_manifests"] == [
        source / ".project_manager" / "engine.manifest"
    ]
    assert manifest.read_bytes() == before


def test_frozen_evidence_requires_candidate_exclusive_path(pm_update):
    """둘 이상 후보가 공유하는 `.claude/skills`류 경로는 어느 flavor의 evidence도 아니다."""
    entries = [
        pm_update.ManifestEntry(".claude/skills"),
        pm_update.ManifestEntry(".first/unique"),
    ]

    evidence = pm_update._frozen_flavor_evidence(
        entries,
        {".claude/skills", ".second/unique"},
        set(),
        set(),
    )

    assert evidence == [".first/unique"]


def test_legacy_nonmatch_keeps_manifest_and_updates_declared_local_rows(
        pm_update, tmp_path, monkeypatch, capsys):
    """커스텀/다중-tree legacy는 rc=1 중단·승격·행 제거 없이 로컬 선언대로 계속 갱신한다."""
    source = tmp_path / "source"
    root_manifest = source / ".project_manager" / "engine.manifest"
    first = source / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "second" / ".project_manager" / "engine.manifest"
    root_manifest.parent.mkdir(parents=True)
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    root_manifest.write_text(".project_manager/tools/root.py\n", encoding="utf-8")
    first.write_text(
        ".first/a    @source=templates/first/.first/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/first/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    second.write_text(
        ".second/a    @source=templates/second/.second/a\n"
        f"{MANIFEST_SELF_REL}    "
        "@source=templates/second/.project_manager/engine.manifest\n",
        encoding="utf-8",
    )
    (source / ".project_manager" / "tools").mkdir()
    (source / ".project_manager" / "tools" / "root.py").write_text(
        "# root\n", encoding="utf-8")
    for rel in (".custom/x", ".first/a", ".second/a"):
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"upstream {rel}\n", encoding="utf-8")
    _track_source_tree(source)

    dest = tmp_path / "dest"
    _write_dest_manifest(
        dest, [".custom/x", ".first/a", ".second/a", MANIFEST_SELF_REL])
    for rel in (".custom/x", ".first/a", ".second/a"):
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local\n", encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    manifest_path = dest / ".project_manager" / "engine.manifest"
    before_manifest = manifest_path.read_bytes()
    rc = pm_update.main(["--from", str(source)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "정확히 하나와 완전 일치하지 않는다" in captured.err
    assert "자동 flavor 승격·행 제거·치유 0" in captured.err
    assert "완전 마이그레이션" in captured.err
    assert "엔진 경로" not in captured.err
    assert "source 에 없음" not in captured.err
    assert manifest_path.read_bytes() == before_manifest
    assert ".custom/x" in manifest_path.read_text(encoding="utf-8")
    for rel in (".custom/x", ".first/a", ".second/a"):
        assert (dest / rel).read_text(encoding="utf-8") == f"upstream {rel}\n"


def test_empty_legacy_manifest_does_not_promote_arbitrary_flavor(
        pm_update, tmp_path, monkeypatch, capsys):
    """빈 manifest는 exact-match가 아니므로 임의 승격 없이 정상 갱신 경로와 loud 진단을 유지한다."""
    dest = tmp_path / "empty-manifest-opencode-adopter"
    _write_dest_manifest(dest, [])
    (dest / ".opencode" / "agents").mkdir(parents=True)
    (dest / ".opencode" / "agents" / "developer.md").write_text(
        "installed opencode adapter\n", encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(REPO), "--dry-run"]) == 0
    captured = capsys.readouterr()
    err = captured.err
    assert "정확히 하나와 완전 일치하지 않는다" in err
    assert "로컬 core 0행" in err
    assert "자동 flavor 승격·행 제거·치유 0" in err
    assert "엔진 경로" not in err
    assert "[new] .claude/agents" not in captured.out, \
        "빈 manifest가 claude_code로 승격돼 .claude 설치 계획을 만들었다"


def test_template_manifests_only_declare_their_own_flavor(pm_update):
    """templates/<f> manifest의 template @source flavor는 언제나 자기 자신뿐이다."""
    manifests = sorted(REPO.glob("templates/*/.project_manager/engine.manifest"))
    assert manifests
    for manifest in manifests:
        flavor = manifest.parents[1].name
        declared = {
            Path(entry.source_rel.replace("\\", "/")).parts[1]
            for entry in pm_update.read_manifest(manifest)
            if entry.source_rel
            and len(Path(entry.source_rel.replace("\\", "/")).parts) >= 3
            and Path(entry.source_rel.replace("\\", "/")).parts[0] == "templates"
        }
        assert declared == {flavor}, \
            f"{manifest}: 자기 flavor 외 @source 선언 발견: {sorted(declared)}"


def test_pm_home_root_manifest_ignores_three_full_stray_adapter_trees(
        pm_update, tmp_path):
    """PM 홈 root 선언 + opencode/codex/skills 실재에도 3-flavor 합집합 승격은 0이다."""
    dest = tmp_path / "pm-home"
    (dest / ".project_manager").mkdir(parents=True)
    shutil.copy2(
        REPO / ".project_manager" / "engine.manifest",
        dest / ".project_manager" / "engine.manifest",
    )
    shutil.copytree(REPO / "templates" / "opencode" / ".opencode", dest / ".opencode")
    shutil.copytree(REPO / "templates" / "codex" / ".codex", dest / ".codex")
    shutil.copytree(REPO / "templates" / "codex" / ".agents", dest / ".agents")

    result = pm_update.resolve_manifest_selfheal(dest, REPO)

    assert result["status"] == "in_sync"
    assert result["upstream_manifests"] == [
        REPO / ".project_manager" / "engine.manifest",
    ], "root PM 홈의 stray adapter tree가 선택 flavor로 무단 승격됨"
    assert result.get("multi_flavor_recovery") is not True


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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    assert conf.get("upstream.rev") == "healedrev", \
        f"치유 후 정합인데 baseline 미갱신: {conf.get('upstream.rev')!r}"


def test_main_dry_run_shows_selfheal_without_side_effects(pm_update, tmp_path, monkeypatch, capsys):
    """--dry-run 은 자기치유 예정을 표시하되 파일/manifest/baseline 을 건드리지 않는다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "src"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL, MANIFEST_SELF_REL])
    _write_dest_manifest(fake_repo, [SENTINEL_REL, MANIFEST_SELF_REL])
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    assert "upstream.rev" not in conf, "dry-run 인데 baseline 기록됨(부작용)"


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
    _write_local_conf(target_root, f"upstream.path={source}\n")
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
    _track_source_tree(source)
    # 채택자 manifest = flavor(@source self-prop)·구형(NEW_ENGINE_REL 미등재).
    _write_dest_manifest(fake_repo, [SENTINEL_REL, flavor_self])
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    assert conf.get("upstream.rev") == "flavorrev", \
        f"flavor 치유 후 정합인데 baseline 미갱신(root skew 오탐 억제?): {conf.get('upstream.rev')!r}"


# ── MF1(codex): URL upstream + --from 생략 → 명확·actionable 에러 (D5 경계·침묵 실패 금지) ──

def test_url_upstream_omitted_from_errors_clearly(pm_update, tmp_path, monkeypatch, capsys):
    """local.conf upstream= 이 URL 이고 --from 생략이면 디렉토리 resolve 안 하고 명확 에러로 멈춘다.

    엔진(pm_update)은 로컬 파일만 복사한다(git clone/fetch 안 함·ADR-0032 D5). URL upstream 을
    `Path(url).resolve()` 했다간 "디렉터리 없음" 류로 침묵 실패하므로, classify_upstream 으로
    URL 을 판별해 actionable 에러(pm-update 스킬·--from 명시 안내)로 멈춘다(MF1).
    """
    fake_repo = tmp_path / "fake_repo"
    _write_local_conf(fake_repo, "upstream.path=https://github.com/acme/proj.git\n")
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
    _write_local_conf(fake_repo, "upstream.path=https://github.com/acme/proj.git\n")
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
    _write_local_conf(target_dir, f"upstream.path={stored}\n")

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
    _track_source_tree(stored)
    _write_local_conf(target_dir, f"upstream.path={stored}\n")

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
    _track_source_tree(stored)
    _write_local_conf(target_dir, f"upstream.path={stored}\n")

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
    _track_source_tree(stored)
    _write_local_conf(target_dir, f"upstream.path={stored}\n")

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
    _write_local_conf(target_dir, f"upstream.path={stored}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")

    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    rc = pm_update.main(["--dry-run"])

    captured = capsys.readouterr()
    assert rc == 2, "self-update 혼합 부재에서 non-@target-owned 엔진 누락이 rc2 로 막지 못함."
    # @target-owned 어댑터는 여전히 skip 안내(surface), 엔진경로는 에러로 surface.
    assert "[skip]" in captured.out and owned_absent in captured.out, \
        "@target-owned 어댑터 부재가 skip 안내로 surface 되지 않음."
    assert engine_absent in captured.err, "엔진경로 누락이 에러로 surface 되지 않음."


# ── --target = operational 렌더 off + codex 표기 최소 렌더 가드 ───────────────
# main() 의 `render_enabled = not args.target` 매핑을 회귀로 박는다. --target 동기는
# 템플릿(local.conf 없는 토큰-form 소스)을 렌더하면 operational leak/_assert_no_leak crash
# 나므로 operational 토큰은 copy2 여야 한다. canonical slash와 값이 다른 codex만 호출 표기 최소
# 렌더를 하며 claude/opencode는 byte-copy다.

def _spy_render_enabled(pm_update, monkeypatch, captured):
    """pm_update.plan 을 감싸 main() 이 전달한 render_enabled 키워드를 포착한다(실 plan 위임)."""
    real_plan = pm_update.plan

    def spy(*args, **kwargs):
        captured["render_enabled"] = kwargs.get("render_enabled")
        captured["entry_notation_template"] = kwargs.get("entry_notation_template")
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(pm_update, "plan", spy)


def test_main_target_passes_render_disabled(pm_update, tmp_path, monkeypatch):
    """main() --target → operational render off, 표기 context는 target 이름으로 전달."""
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "templates" / "oc").mkdir(parents=True)
    stored = tmp_path / "up_target"
    _make_upstream(stored)
    _write_local_conf(fake_repo / "templates" / "oc", f"upstream.path={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    captured: dict = {}
    _spy_render_enabled(pm_update, monkeypatch, captured)

    assert pm_update.main(["--target", "oc", "--dry-run"]) == 0
    assert captured["render_enabled"] is False, "--target 인데 render 가 켜졌다(템플릿 토큰 렌더 위험)."
    assert captured["entry_notation_template"] == "oc"


def test_main_self_location_passes_render_enabled(pm_update, tmp_path, monkeypatch):
    """main() --target 없음(채택자 self-update) → plan(render_enabled=True) — render 유지·불변."""
    fake_repo = tmp_path / "fake_repo"
    stored = tmp_path / "up_self"
    _make_upstream(stored)
    _write_local_conf(fake_repo, f"upstream.path={stored}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    captured: dict = {}
    _spy_render_enabled(pm_update, monkeypatch, captured)

    assert pm_update.main(["--dry-run"]) == 0
    assert captured["render_enabled"] is True, "채택자 self-update 인데 render 가 꺼졌다(토큰 출하 위험)."
    assert captured["entry_notation_template"] is None


@pytest.mark.parametrize(
    ("template_dir", "expected", "notation_template"),
    (("codex", "$pm-bootstrap", "codex"), ("opencode", "/pm-bootstrap", None)),
)
def test_target_export_conditionally_renders_only_skill_entry_notation(
        pm_update, tmp_path, template_dir, expected, notation_template):
    source = tmp_path / f"source-{template_dir}"
    skill = source / ".claude" / "skills" / "pm-bootstrap" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "# /pm-bootstrap — entry\n"
        "run `/pm-bootstrap --task main`\n"
        "path .claude/skills/pm-bootstrap/SKILL.md\n"
        "project {{PROJECT_NAME}}\n",
        encoding="utf-8",
    )
    _track_source_tree(source)
    dest = tmp_path / f"dest-{template_dir}"
    manifest = [pm_update.ManifestEntry(".claude/skills", render=True)]

    changes, missing = pm_update.plan(
        source,
        manifest,
        dest_root=dest,
        render_enabled=False,
        entry_notation_template=template_dir,
    )
    assert not missing and len(changes) == 1
    assert changes[0][2].render is False
    assert changes[0][2].entry_notation_template == notation_template
    pm_update.apply(changes)

    rendered = (dest / ".claude/skills/pm-bootstrap/SKILL.md").read_text(encoding="utf-8")
    assert f"# {expected} — entry" in rendered
    assert f"`{expected} --task main`" in rendered
    assert ".claude/skills/pm-bootstrap/SKILL.md" in rendered
    assert "{{PROJECT_NAME}}" in rendered, "--target 최소 렌더가 operational 토큰까지 건드렸다"


def test_target_export_wiring_sensitivity_identity_renderer_leaves_wrong_prefix(
        pm_update, tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = source / ".claude" / "skills" / "pm-bootstrap" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# /pm-bootstrap — entry\n", encoding="utf-8")
    _track_source_tree(source)
    dest = tmp_path / "dest"
    real_render = pm_update._load_pm_render()
    fake_render = SimpleNamespace(
        render_skill_entry_notation=lambda text, _template, **_kwargs: text,
        render_adapter=real_render.render_adapter,
    )
    monkeypatch.setattr(pm_update, "_load_pm_render", lambda: fake_render)

    changes, missing = pm_update.plan(
        source,
        [pm_update.ManifestEntry(".claude/skills", render=True)],
        dest_root=dest,
        render_enabled=False,
        entry_notation_template="codex",
    )
    assert not missing
    pm_update.apply(changes)
    rendered = (dest / ".claude/skills/pm-bootstrap/SKILL.md").read_text(encoding="utf-8")
    assert "/pm-bootstrap" in rendered and "$pm-bootstrap" not in rendered


def test_render_comparison_ignores_newline_notation_and_write_preserves_it(
        pm_update, tmp_path):
    """개행 표기만 다른 dest 는 '변경 없음'이고, 실제 변경은 dest 표기 그대로 기록된다 (T-0709).

    Windows 채택자 체크아웃(`core.autocrlf=true`)의 전파 트리는 CRLF 다. 표기 차이를 '다름'으로
    읽으면 self-update 가 같은 소스인데 트리를 통째로 되쓰고(pm_import↔pm_update 렌더 drift),
    LF 로 되쓰면 채택자가 손대지 않은 줄까지 전면 diff 가 된다. 판정은 정규화 후, 쓰기는 표기
    보존이다. 내용 변경 탐지력은 아래 후반부와
    `test_render_comparison_treats_non_utf8_destination_as_update` 가 지킨다."""
    source = tmp_path / "source"
    src = source / "card.md"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"/pm-bootstrap\n")
    _track_source_tree(source)
    dest = tmp_path / "dest"
    dst = dest / "card.md"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"/pm-bootstrap\r\n")

    changes, missing = pm_update.plan(
        source,
        [pm_update.ManifestEntry("card.md", render=True)],
        dest_root=dest,
        entry_notation_template="claude_code",
    )
    assert not missing and changes == []
    assert dst.read_bytes() == b"/pm-bootstrap\r\n", "표기만 다른 dest 를 되썼다(churn)"

    # 내용이 실제로 바뀌면 update 로 잡히고, 기록은 dest 의 CRLF 표기를 유지한다.
    src.write_bytes(b"/pm-bootstrap\nsecond line\n")
    changes, missing = pm_update.plan(
        source,
        [pm_update.ManifestEntry("card.md", render=True)],
        dest_root=dest,
        entry_notation_template="claude_code",
    )
    assert not missing and [(rel, kind) for rel, _, _, kind in changes] == [
        ("card.md", "update")
    ]
    pm_update.apply(changes)
    assert dst.read_bytes() == b"/pm-bootstrap\r\nsecond line\r\n"


def test_render_write_uses_source_notation_for_new_destination(pm_update, tmp_path):
    """dest 부재(첫 배달)면 소스 표기를 따른다 — pm_import 의 byte-copy+표기보존 렌더와 동형."""
    source = tmp_path / "source"
    src = source / "card.md"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"/pm-bootstrap\r\ncard\r\n")
    _track_source_tree(source)
    dest = tmp_path / "dest"

    changes, missing = pm_update.plan(
        source,
        [pm_update.ManifestEntry("card.md", render=True)],
        dest_root=dest,
        entry_notation_template="claude_code",
    )
    assert not missing and [(rel, kind) for rel, _, _, kind in changes] == [
        ("card.md", "new")
    ]
    pm_update.apply(changes)
    assert (dest / "card.md").read_bytes() == b"/pm-bootstrap\r\ncard\r\n"


def test_render_comparison_treats_non_utf8_destination_as_update(pm_update, tmp_path):
    source = tmp_path / "source"
    src = source / "card.md"
    src.parent.mkdir(parents=True)
    src.write_text("/pm-bootstrap\n", encoding="utf-8")
    _track_source_tree(source)
    dest = tmp_path / "dest"
    dst = dest / "card.md"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"\xff\xfe\x00")

    changes, missing = pm_update.plan(
        source,
        [pm_update.ManifestEntry("card.md", render=True)],
        dest_root=dest,
        entry_notation_template="codex",
    )
    assert not missing and len(changes) == 1 and changes[0][3] == "update"
    pm_update.apply(changes)
    assert dst.read_bytes() == b"$pm-bootstrap\n"


def test_multi_harness_render_preserves_per_entry_flavor(pm_update, tmp_path):
    source = tmp_path / "source"
    first = source / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "codex" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(".claude/skills    @render\n", encoding="utf-8")
    second.write_text(
        ".agents/skills    @render @source=.claude/skills\n",
        encoding="utf-8",
    )
    card = source / ".claude" / "skills" / "pm-bootstrap" / "SKILL.md"
    card.parent.mkdir(parents=True)
    card.write_text("# /pm-bootstrap — entry\n", encoding="utf-8")
    _track_source_tree(source)
    contexts = pm_update._entry_notation_templates_from_manifests(
        [first, second], source
    )
    assert contexts == {
        ".claude/skills": ("opencode",),
        ".agents/skills": ("codex",),
    }

    entries = pm_update.merge_manifest_sources([first, second])["entries"]
    broken_dest = tmp_path / "broken-dest"
    broken_changes, _ = pm_update.plan(
        source,
        entries,
        dest_root=broken_dest,
        entry_notation_template="opencode",  # 옛 first-manifest 전역 적용
    )
    pm_update.apply(broken_changes)
    assert (broken_dest / ".agents/skills/pm-bootstrap/SKILL.md").read_text(
        encoding="utf-8"
    ).startswith("# /pm-bootstrap"), "전역 flavor 파손 재현이 공허함"

    dest = tmp_path / "dest"
    changes, missing = pm_update.plan(
        source,
        entries,
        dest_root=dest,
        entry_notation_templates=contexts,
    )
    assert not missing
    pm_update.apply(changes)
    assert (dest / ".claude/skills/pm-bootstrap/SKILL.md").read_text(
        encoding="utf-8"
    ).startswith("# /pm-bootstrap")
    assert (dest / ".agents/skills/pm-bootstrap/SKILL.md").read_text(
        encoding="utf-8"
    ).startswith("# $pm-bootstrap")


def test_multi_harness_shared_path_combines_selected_flavors(pm_update, tmp_path):
    source = tmp_path / "source"
    first = source / "templates" / "codex" / ".project_manager" / "engine.manifest"
    second = source / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_text(".project_manager/wiki/pm_role.md\n", encoding="utf-8")
    role = source / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("start `/pm-bootstrap --task main`\n", encoding="utf-8")
    _track_source_tree(source)

    contexts = pm_update._entry_notation_templates_from_manifests(
        [first, second], source
    )
    assert contexts == {
        ".project_manager/wiki/pm_role.md": ("codex", "opencode")
    }
    changes, missing = pm_update.plan(
        source,
        pm_update.merge_manifest_sources([first, second])["entries"],
        dest_root=tmp_path / "dest",
        entry_notation_templates=contexts,
    )
    assert not missing
    pm_update.apply(changes)
    assert (tmp_path / "dest" / ".project_manager" / "wiki" / "pm_role.md").read_text(
        encoding="utf-8"
    ) == (
        "start `$pm-bootstrap --task main`(codex) / "
        "`/pm-bootstrap --task main`(opencode)\n"
    )


def test_selected_flavor_manifest_read_failure_is_loud(pm_update, tmp_path):
    source = tmp_path / "source"
    unreadable_shape = (
        source / "templates" / "codex" / ".project_manager" / "engine.manifest"
    )
    unreadable_shape.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="context.*중단"):
        pm_update._entry_notation_templates_from_manifests(
            [unreadable_shape], source
        )


# ── v2 엔진 manifest 정합 (T-0088 — 신규 엔진 등재/개명 누락 가드) ────────────────
# domain.py 가 manifest 미등재라 templates 에 전파 안 되던 실 버그를 회귀로 박는다.
# 출하 manifest 전수(root + 3 flavor)가 v2 엔진을 일관되게 담는지 검증. **codex 를 빼면 등재
# 축이 출하 축보다 좁아** 그 flavor 만의 등재 누락/오등재가 어느 가드에도 안 걸린다(원장 미등재
# 가드가 그 비대칭을 드러냈다) — 목록은 출하 flavor 를 따라간다.

_MANIFESTS = [
    REPO / ".project_manager" / "engine.manifest",
    REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest",
    REPO / "templates" / "codex" / ".project_manager" / "engine.manifest",
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
    """pm_import.py 가 root↔양 템플릿 byte-identical (전파 무드리프트·다중 import 첫-tree mismatch 회피).

    pm_import 의 다중 선택은 공유 엔진파일을 여러 템플릿 트리에서 가져오므로, 두 트리의
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
    _track_source_tree(src)

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
    _track_source_tree(source)

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
    _track_source_tree(source)

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
    _track_source_tree(source)

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\nadditional_reviewer.enabled=false\n")
    _track_source_tree(stored)

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\nadditional_reviewer.enabled=false\n")

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
    _write_local_conf(oc_dir, f"upstream.path={fake_repo}\nadditional_reviewer.enabled=false\n")
    _track_source_tree(fake_repo)

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
    _write_local_conf(fake_repo, f"upstream.path={stored}\nadditional_reviewer.enabled=false\n")
    _track_source_tree(stored)

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
        f"upstream.path={stored}\nproject.name=AcmePay\nadditional_reviewer.enabled=false\n")
    _track_source_tree(stored)

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


def test_summarize_classifies_source_mapped_entry_as_engine(pm_update, tmp_path):
    """`@source=` 매핑 엔트리는 **상류 읽기 경로**(source_rel)로 변경이 오므로 그 좌표도 engine 이다.

    dest 경로만 비교하던 옛 판정은 `@source` 엔트리 전부를 '그 외(동기 안 받음)' 로 오분류해,
    미리보기가 어댑터 훅 교체를 통째로 놓쳤다(파일 매핑 + 디렉토리 매핑 둘 다 red-첫)."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [
        # 파일 매핑(출하 claude 어댑터 훅 형상).
        ".claude/ctx_guard.py    @source=templates/claude_code/.claude/ctx_guard.py",
        # 디렉토리 매핑(하위 파일이 prefix 로 잡혀야 한다).
        ".codex/agents    @render @source=templates/codex/.codex/agents",
    ])
    runner = _make_fake_git_runner(
        head="h",
        log_lines=["abc1234 adapter hook 교체"],
        diff_lines=[
            "M\ttemplates/claude_code/.claude/ctx_guard.py",   # source_rel 파일 동일
            "A\ttemplates/codex/.codex/agents/reviewer.md",    # source_rel 디렉토리 prefix
            "M\ttemplates/opencode/.opencode/agents/x.md",     # 미등재 매핑 → other
        ],
    )
    s = pm_update.summarize_upstream_changes(source, "base", manifest, git_runner=runner)
    engine_paths = {p for _c, p in s["engine"]}
    assert engine_paths == {
        "templates/claude_code/.claude/ctx_guard.py",
        "templates/codex/.codex/agents/reviewer.md",
    }, f"@source 매핑 상류 변경이 engine 으로 분류되지 않음: {s['engine']}"
    assert {p for _c, p in s["other"]} == {"templates/opencode/.opencode/agents/x.md"}


def test_path_under_manifest_covers_shipped_source_entries(pm_update):
    """출하 claude manifest **실물**의 모든 `@source` 엔트리가 source_rel 좌표로 engine 판정된다.

    실형상 픽스처 — 합성 픽스처가 전부 bare 엔트리라 이 축이 테스트에 아예 없었고(갭), 출하
    manifest 의 @source 엔트리 전량이 오분류되는 것을 아무도 못 봤다."""
    shipped = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    manifest = pm_update.read_manifest(shipped)
    mapped = [e for e in manifest if getattr(e, "source_rel", None)]
    assert mapped, f"출하 manifest 에 @source 엔트리가 없다(픽스처 전제 붕괴): {shipped}"
    misclassified = [
        e.source_rel for e in mapped
        if not pm_update._path_under_manifest(e.source_rel, manifest)
    ]
    assert not misclassified, \
        f"출하 @source 엔트리의 상류 경로가 manifest 밖으로 오분류: {misclassified}"


def test_path_under_manifest_bare_entry_unchanged(pm_update):
    """source_rel 없는 bare 엔트리는 동작 무변경 — dest 경로 축만으로 판정(회귀 없음)."""
    manifest = _manifest_entries(pm_update, [
        ".project_manager/tools/board.py",   # 파일 항목
        ".claude/agents",                    # 디렉토리 항목
    ])
    assert pm_update._path_under_manifest(".project_manager/tools/board.py", manifest)
    assert pm_update._path_under_manifest(".claude/agents/x.md", manifest)
    assert not pm_update._path_under_manifest("README.md", manifest)
    # bare 엔트리는 source 좌표가 dest 와 같으므로 templates/ 경로가 새로 딸려오지 않는다.
    assert not pm_update._path_under_manifest(
        "templates/claude_code/.claude/agents/x.md", manifest)


def test_path_under_manifest_respects_specific_source_override(pm_update):
    """더 구체적인 파일 override 가 있으면 **상위 항목의 source 변경은 그 파일 축에서 비분류**다.

    출하 codex 형상: `.agents/skills @source=.claude/skills` 위에
    `.agents/skills/pm-dev-delegate/SKILL.md @source=templates/codex/...` 가 얹혀 그 파일만 다른
    source 를 공급한다. plan 은 소유권 우선순위(`_manifest_owner_index`)로 상위 항목의 열거에서 그
    경로를 빼므로, 상위 source 변경을 엔진 영향이라 하면 **읽지도 않을 파일 때문에** 오보한다.
    좌표만 보던 판정의 sensitivity — override 자신의 source 변경은 정상 분류돼야 한다."""
    parent_source = ".claude/skills"
    override_dest = ".agents/skills/pm-dev-delegate/SKILL.md"
    override_source = "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md"
    manifest = _manifest_entries(pm_update, [
        f".agents/skills    @render @source={parent_source}",
        f"{override_dest}    @render @source={override_source}",
    ])
    # ① override 가 소유하는 파일의 **상위 source** 변경 — 그 파일은 override 에서 오므로 비분류.
    assert not pm_update._path_under_manifest(
        f"{parent_source}/pm-dev-delegate/SKILL.md", manifest), \
        "override 된 경로인데 상위 항목 source 변경을 엔진 영향으로 과분류"
    # ② override 자신의 source 변경 — 그 파일의 실제 공급원이므로 분류.
    assert pm_update._path_under_manifest(override_source, manifest), \
        "override 자신의 source 변경이 엔진 영향에서 누락"
    # ③ override 가 없는 형제 경로는 상위 항목이 그대로 공급 — 분류(과교정 방지).
    assert pm_update._path_under_manifest(f"{parent_source}/pm-adr/SKILL.md", manifest)


def test_path_under_manifest_shipped_codex_override_not_overclassified(pm_update):
    """출하 codex manifest **실물**에서도 override 축이 지켜진다(실형상 픽스처).

    `.agents/skills` 상위 항목과 그 아래 파일 override 가 동시에 존재하는 유일한 출하 형상이라,
    합성 픽스처만으로는 실 manifest 순서/마커 조합이 바뀌었을 때를 못 잡는다."""
    shipped = REPO / "templates" / "codex" / ".project_manager" / "engine.manifest"
    manifest = pm_update.read_manifest(shipped)
    overrides = [
        e for e in manifest
        if getattr(e, "source_rel", None) and str(e).count("/") >= 2
        and any(str(other) != str(e) and str(e).startswith(str(other) + "/")
                for other in manifest)
    ]
    assert overrides, f"출하 codex manifest 에 파일 override 가 없다(픽스처 전제 붕괴): {shipped}"
    for override in overrides:
        parent = next(
            other for other in manifest
            if str(other) != str(override) and str(override).startswith(str(other) + "/"))
        via_parent = str(override).replace(str(parent), _source_root_rel_of(parent), 1)
        assert not pm_update._path_under_manifest(via_parent, manifest), \
            f"상위 항목 source 좌표({via_parent})가 override 된 파일 때문에 과분류"
        assert pm_update._path_under_manifest(override.source_rel, manifest), \
            f"override 자신의 source({override.source_rel})가 엔진 영향에서 누락"


def test_path_under_manifest_mapped_entry_ignores_dest_coordinate(pm_update):
    """`@source` 매핑 엔트리는 **읽기 좌표만** 엔진 영향이다 (T-0575 축·codex 지적).

    `.codex/agents @source=templates/codex/.codex/agents` 에서 상류 변경이 dest 좌표
    (`.codex/agents/x.md`)로 오면 이번 계획은 그 경로를 읽지 않는다 — 그런데도 분류하면 미리보기가
    "이번 동기가 받는 것" 에 도달하지 않을 파일을 싣는다. bare 엔트리는 두 좌표가 같아 무변경."""
    manifest = _manifest_entries(pm_update, [
        ".codex/agents    @render @source=templates/codex/.codex/agents",
        ".project_manager/tools/board.py",
    ])
    assert pm_update._path_under_manifest(
        "templates/codex/.codex/agents/reviewer.md", manifest), \
        "매핑 엔트리의 상류 좌표가 엔진 영향에서 누락"
    assert not pm_update._path_under_manifest(".codex/agents/reviewer.md", manifest), \
        "매핑 엔트리의 dest 좌표를 엔진 영향으로 과분류(계획이 읽지 않는 경로)"
    # bare 엔트리는 dest 좌표가 곧 읽기 좌표 — 회귀 없음.
    assert pm_update._path_under_manifest(".project_manager/tools/board.py", manifest)


def _source_root_rel_of(entry) -> str:
    """테스트 편의: manifest 항목의 상류 읽기 경로(@source 있으면 그것·없으면 dest 경로)."""
    return getattr(entry, "source_rel", None) or str(entry)


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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=samerev00000\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=gonerev00000\n")
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
        fake_repo, "upstream.path=https://github.com/acme/proj.git\nupstream.rev=base\n")
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
        fake_repo, "upstream.path=https://github.com/acme/proj.git\nupstream.rev=base\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base\n")
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


# ── 미리보기 분류 기준 == 적용 계획 기준 (self-heal 승격 manifest 공유·T-0576) ──

def _tree_digest(root: Path) -> dict[str, str]:
    """트리의 relpath → 내용 해시 — read-only 불변식 단언용(파일 추가/삭제/수정 전부 포착)."""
    return {
        str(path.relative_to(root)).replace("\\", "/"):
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _changes_engine_paths(out: str) -> set[str]:
    """`--changes` 출력의 '엔진 영향' 블록 경로 집합 — 출력 형식은 `  <코드> <경로>`."""
    engine_block = out.split("엔진 영향")[1].split("그 외 변경")[0]
    return {
        stripped.split(" ", 1)[1].strip()
        for stripped in (line.strip() for line in engine_block.splitlines())
        if stripped.startswith(("M ", "A ", "D ", "R ", "C ", "T "))
    }


def _dry_run_plan_targets(pm_update, monkeypatch) -> set[str]:
    """같은 픽스처에서 dry-run 이 실제로 계획한 대상 집합 — plan 을 감싸 산출을 포획한다."""
    recorded: dict[str, set[str]] = {}
    real_plan = pm_update.plan

    def spy_plan(*args, **kwargs):
        changes, missing = real_plan(*args, **kwargs)
        recorded["targets"] = {
            str(rel).replace("\\", "/") for rel, _sp, _dst, _kind in changes}
        return changes, missing

    monkeypatch.setattr(pm_update, "plan", spy_plan)
    assert pm_update.main(["--dry-run"]) == 0
    monkeypatch.setattr(pm_update, "plan", real_plan)
    return recorded["targets"]


def _selfheal_preview_fixture(pm_update, tmp_path, monkeypatch):
    """상류가 신규 엔진 파일을 등재했고 로컬 manifest 는 구형인 dest — self-heal 승격 대상 형상.

    dest 는 기존 등재분(SENTINEL)을 upstream 과 byte-동일하게 갖는다 — 이번 계획의 대상이 신규
    등재 파일 하나로 좁혀져야 `--changes` 분류 집합과 계획 대상 집합을 직접 비교할 수 있다.
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_upstream_manifest(source, [SENTINEL_REL, NEW_ENGINE_REL])  # 상류: 신규 도구 + manifest 행.
    _write_dest_manifest(fake_repo, [SENTINEL_REL])                  # 로컬: 구형 manifest.
    dest_sentinel = fake_repo / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_bytes((source / SENTINEL_REL).read_bytes())  # 기존 등재분은 이미 최신.
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    return fake_repo, source


def test_changes_uses_selfheal_promoted_manifest(pm_update, tmp_path, monkeypatch, capsys):
    """`--changes` 도 self-heal 승격 manifest 로 분류한다 — 신규 등재 엔진 파일이 engine 이다.

    실 sync 는 승격 manifest 로 계획하는데 미리보기만 낡은 로컬 manifest 로 해소하면, 정확히
    self-heal 이 이번 sync 로 전달하는 파일이 '그 외(동기 안 받음)' 으로 미리보기된다(red-첫)."""
    fake_repo, _source = _selfheal_preview_fixture(pm_update, tmp_path, monkeypatch)
    runner = _make_fake_git_runner(
        head="head0000", log_lines=["abc1234 신규 도구 등재"],
        diff_lines=[f"A\t{NEW_ENGINE_REL}", "M\tREADME.md"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    rc = pm_update.main(["--changes"])
    assert rc == 0
    out = capsys.readouterr().out
    engine_block = out.split("엔진 영향")[1].split("그 외 변경")[0]
    assert NEW_ENGINE_REL in engine_block, \
        f"신규 등재 엔진 파일이 미리보기에서 '동기 안 받음' 으로 분류됨: {out!r}"


def test_changes_engine_set_equals_dry_run_plan_targets(
        pm_update, tmp_path, monkeypatch, capsys):
    """등가성 가드: 같은 픽스처에서 `--changes` engine 집합 == dry-run 계획 대상 집합.

    헬퍼를 공유해도 소비 방식이 갈라지면 잡아야 하므로 함수 동일성이 아니라 **산출 동일성**을
    비교한다(둘 다 실행해 교차 검증). 집합 비교는 **bare manifest 좌표 한정**이다 — `@source`
    채택자는 분류(상류 읽기 경로)와 계획 대상(dest 경로)의 좌표계가 애초에 달라 직접 비교 대상이
    아니다(그 축은 `test_path_under_manifest_*` 가 소유권까지 따로 검증한다)."""
    fake_repo, _source = _selfheal_preview_fixture(pm_update, tmp_path, monkeypatch)
    runner = _make_fake_git_runner(
        head="head0000", log_lines=["abc1234 신규 도구 등재"],
        diff_lines=[f"A\t{NEW_ENGINE_REL}", "M\tREADME.md"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0
    changes_engine = _changes_engine_paths(capsys.readouterr().out)

    assert changes_engine == _dry_run_plan_targets(pm_update, monkeypatch), (
        "미리보기 분류 기준과 적용 계획 기준이 어긋난다 "
        f"(--changes engine={sorted(changes_engine)})")


def _legacy_preserved_preview_fixture(pm_update, tmp_path, monkeypatch, shared_rel):
    """self-heal 이 'legacy_preserved' 인 dest — 후보 flavor 둘의 경로 집합이 같아 tiebreak 불가.

    그 코호트에선 로컬 manifest 가 불가침이라 계획이 self-prop 을 뺀다. 미리보기가 이 축을 공유하지
    않으면 engine.manifest 를 '받는다' 로 오보한다. dest 의 공유 파일은 upstream 과 달라(갱신 대상)
    계획 대상이 정확히 하나가 되게 한다.
    """
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    root = source / ".project_manager" / "engine.manifest"
    root.parent.mkdir(parents=True)
    root.write_text(f"{shared_rel}\n{MANIFEST_SELF_REL}\n", encoding="utf-8")
    (source / shared_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / shared_rel).write_text("# upstream shared\n", encoding="utf-8")
    for flavor in ("first", "second"):  # 동일 경로 집합 후보 둘 → flavor 정보 0(legacy_preserved).
        candidate = source / "templates" / flavor / ".project_manager" / "engine.manifest"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            f"{shared_rel}\n{MANIFEST_SELF_REL}    "
            f"@source=templates/{flavor}/.project_manager/engine.manifest\n",
            encoding="utf-8")
    _track_source_tree(source)

    _write_dest_manifest(fake_repo, [shared_rel, MANIFEST_SELF_REL])
    (fake_repo / shared_rel).parent.mkdir(parents=True, exist_ok=True)
    (fake_repo / shared_rel).write_text("# stale local\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "shouldnotappear")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    return fake_repo, source


def test_changes_matches_plan_in_legacy_preserved_cohort(
        pm_update, tmp_path, monkeypatch, capsys):
    """`legacy_preserved` 코호트에서도 미리보기 == 계획 — engine.manifest 를 '받는다' 로 오보하지 않는다.

    이 코호트는 로컬 manifest 불가침이라 `_main` 이 self-prop 을 계획에서 뺀다. 그 축이 헬퍼 밖에
    있으면(=`_main` 전용) 미리보기만 manifest 를 엔진 영향으로 세어 두 기준이 갈린다 — T-0576 이
    닫으려는 클래스의 잔여."""
    shared_rel = ".project_manager/tools/__pm_update_shared__.py"
    fake_repo, source = _legacy_preserved_preview_fixture(
        pm_update, tmp_path, monkeypatch, shared_rel)
    assert pm_update.resolve_manifest_selfheal(fake_repo, source)["status"] \
        == "legacy_preserved", "픽스처가 legacy_preserved 를 만들지 못했다(전제 붕괴)"
    capsys.readouterr()  # 픽스처 검증이 낸 경고는 비운다.

    runner = _make_fake_git_runner(
        head="head0000", log_lines=["abc1234 c1"],
        diff_lines=[f"M\t{shared_rel}", f"M\t{MANIFEST_SELF_REL}"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0
    changes_engine = _changes_engine_paths(capsys.readouterr().out)

    assert MANIFEST_SELF_REL not in changes_engine, \
        "legacy_preserved 인데 미리보기가 engine.manifest 를 '받는다' 로 표시(계획엔 없다)"
    assert changes_engine == _dry_run_plan_targets(pm_update, monkeypatch), (
        "legacy_preserved 코호트에서 미리보기와 계획이 어긋난다 "
        f"(--changes engine={sorted(changes_engine)})")


def test_changes_with_selfheal_leaves_dest_tree_unchanged(
        pm_update, tmp_path, monkeypatch, capsys):
    """self-heal 해소를 태워도 `--changes` 는 read-only — dest 트리 해시가 그대로다.

    승격 manifest 를 디스크에 쓰면(부수효과) 미리보기가 실 sync 가 된다 — 판정만 쓰고 쓰지 않는다."""
    fake_repo, _source = _selfheal_preview_fixture(pm_update, tmp_path, monkeypatch)
    runner = _make_fake_git_runner(
        head="head0000", log_lines=["abc1234 신규 도구 등재"],
        diff_lines=[f"A\t{NEW_ENGINE_REL}"])
    _patch_upstream_runner(pm_update, monkeypatch, runner)
    before = _tree_digest(fake_repo)

    assert pm_update.main(["--changes"]) == 0

    assert _tree_digest(fake_repo) == before, "--changes 가 dest 트리를 변경했다(read-only 위반)"
    assert not (fake_repo / NEW_ENGINE_REL).exists(), "--changes 가 신규 파일을 전파했다"
    healed = (fake_repo / ".project_manager" / "engine.manifest").read_text(encoding="utf-8")
    assert NEW_ENGINE_REL not in healed, "--changes 가 승격 manifest 를 디스크에 썼다(부수효과)"


# ── codex suggestion 1: 집계 실패 surface (빈 결과 → "변경 0" 오판 금지) ──────

def test_changes_main_summary_failed_surfaces(pm_update, tmp_path, monkeypatch, capsys):
    """log/diff git 호출 rc≠0(요약 불가) → stderr 안내·exit 0(변경 0 오판 금지·suggestion 1)."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".project_manager/tools/board.py"])
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(["--log"])
    assert rc == 1, "--log 가 --changes 없이도 통과(조용한 무시)."
    err = capsys.readouterr().err
    assert "--log" in err and "--changes" in err


# ════════════════════════════════════════════════════════════════════════
# 상류 삭제·rename 보고 채널 — 미전파 오보 제거
# ════════════════════════════════════════════════════════════════════════
# manifest 디렉토리 엔트리 동기는 source 만 열거해 추가·갱신만 한다 — 상류에서 은퇴한 파일은
# dest 에 영구 잔존하는데, 옛 출력은 `--changes` 가 D 를 "이번 동기가 받는 것" 에 싣고 동기는
# "변경 없음" 만 말해 서로 반대로 오보했다. 삭제 전파는 여전히 하지 않고(로컬 자산과 구분 불가)
# 세 출력이 같은 사실("동기는 안 지운다")을 말하는지 검증한다.

RETIRED_DIR_REL = "adapter"


def _make_dir_entry_upstream(root: Path, files: dict) -> None:
    """디렉토리 엔트리(`adapter`) 하나를 등재한 tracked upstream — files = {relpath: 내용}."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    manifest = root / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(RETIRED_DIR_REL + "\n", encoding="utf-8")
    _track_source_tree(root)


@pytest.mark.parametrize("dry_run", [False, True])
def test_retired_upstream_file_survives_sync_and_is_reported(
        pm_update, tmp_path, monkeypatch, capsys, dry_run):
    """상류가 은퇴시킨 디렉토리 엔트리 하위 파일은 동기 후에도 dest 에 남고 보고에 뜬다.

    옛 출력은 이 파일을 아예 언급하지 않아 채택자가 잔존 사실을 알 길이 없었다(동기는 "변경
    없음" 이라 말하고 baseline 만 전진)."""
    fake_repo = tmp_path / f"dest-{dry_run}"
    source = tmp_path / f"source-{dry_run}"
    _make_dir_entry_upstream(source, {f"{RETIRED_DIR_REL}/keep.md": "# keep\n"})
    # dest: 상류에 있는 파일 + 상류가 은퇴시킨 파일(잔존).
    keep = fake_repo / RETIRED_DIR_REL / "keep.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("# keep\n", encoding="utf-8")
    retired = fake_repo / RETIRED_DIR_REL / "retired.md"
    retired.write_text("# 상류에서 은퇴한 파일\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    argv = ["--dry-run"] if dry_run else []
    assert pm_update.main(argv) == 0

    out = capsys.readouterr().out
    assert retired.is_file(), "동기가 은퇴 파일을 지웠다(삭제 전파 금지 계약 위반)"
    assert "상류 부재 파일 1건" in out, f"은퇴 후보 보고가 없다: {out!r}"
    assert f"{RETIRED_DIR_REL}/retired.md" in out
    assert "동기는 지우지 않는다" in out
    assert f"{RETIRED_DIR_REL}/keep.md" not in out.split("상류 부재 파일")[1], \
        "상류가 공급하는 파일이 은퇴 후보로 오보됨"


def test_adopter_local_asset_is_reported_as_indistinguishable(
        pm_update, tmp_path, monkeypatch, capsys):
    """manifest 디렉토리 안의 채택자 로컬 자산도 같은 보고에 뜨고, 문구가 구분 불가를 명시한다.

    로컬 자산과 은퇴 파일은 '상류에 source 가 없다' 는 **같은 신호**다 — 기계로 가를 수 없으니
    지우지 않고 판단을 채택자에게 넘긴다."""
    fake_repo = tmp_path / "dest-local-asset"
    source = tmp_path / "source-local-asset"
    _make_dir_entry_upstream(source, {f"{RETIRED_DIR_REL}/keep.md": "# keep\n"})
    local_asset = fake_repo / RETIRED_DIR_REL / "project-local" / "SKILL.md"
    local_asset.parent.mkdir(parents=True)
    local_asset.write_text("# 채택자 로컬 자산\n", encoding="utf-8")
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    assert pm_update.main([]) == 0

    out = capsys.readouterr().out
    assert local_asset.is_file(), "채택자 로컬 자산이 삭제됨(자산 파괴)"
    assert f"{RETIRED_DIR_REL}/project-local/SKILL.md" in out
    assert "채택자 로컬 자산과 구분 불가" in out
    assert "수동 정리 판단" in out


def test_retired_report_is_silent_when_dest_matches_upstream(
        pm_update, tmp_path, monkeypatch, capsys):
    """상류 부재 파일 0건이면 보고는 침묵한다(정상 채택자 노이즈 0)."""
    fake_repo = tmp_path / "dest-clean"
    source = tmp_path / "source-clean"
    _make_dir_entry_upstream(source, {f"{RETIRED_DIR_REL}/keep.md": "# keep\n"})
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    assert pm_update.main([]) == 0

    assert "상류 부재 파일" not in capsys.readouterr().out


def test_retired_manifest_files_reverses_source_remap(pm_update, tmp_path):
    """`@source` 엔트리는 dest→source **역방향** 매핑으로 판정한다(dest 좌표로 보면 전량 오보)."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    upstream_dir = source / "templates" / "codex" / ".codex" / "agents"
    upstream_dir.mkdir(parents=True)
    (upstream_dir / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    dest_dir = dest / ".codex" / "agents"
    dest_dir.mkdir(parents=True)
    (dest_dir / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    (dest_dir / "retired.md").write_text("# 상류 은퇴\n", encoding="utf-8")
    manifest = _manifest_entries(pm_update, [
        ".codex/agents    @source=templates/codex/.codex/agents",
    ])

    retired = pm_update._retired_manifest_files(source, manifest, dest, set())

    assert retired == [".codex/agents/retired.md"], \
        f"@source 역방향 매핑 실패(상류가 공급하는 파일까지 은퇴로 오보?): {retired}"


def test_retired_manifest_files_skips_target_owned_and_supplied_paths(pm_update, tmp_path):
    """`@target-owned`(upstream source 부재가 정상)와 이번 계획이 공급하는 좌표는 후보가 아니다.

    `@target-owned` 픽스처는 **상류 source 가 실재**해야 가드를 단독 검증한다 — source 를 안 두면
    "상류 부재라 걸러졌는지 `@target-owned` 라 걸러졌는지" 구분할 수 없어 가드가 사라져도 green 이다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    (source / ".project_manager").mkdir(parents=True)
    owned_dir = dest / ".opencode" / "lib"
    owned_dir.mkdir(parents=True)
    (owned_dir / "safe-write.cjs").write_text("// 타깃 고유\n", encoding="utf-8")
    # 상류에 같은 등재 디렉토리를 두되 그 파일은 없다 — `@target-owned` 가드가 아니면 은퇴 후보다.
    (source / ".opencode" / "lib").mkdir(parents=True)
    shipped_dir = dest / RETIRED_DIR_REL
    shipped_dir.mkdir(parents=True)
    (shipped_dir / "keep.md").write_text("# keep\n", encoding="utf-8")
    (source / RETIRED_DIR_REL).mkdir(parents=True)
    manifest = _manifest_entries(pm_update, [
        ".opencode/lib    @target-owned",
        RETIRED_DIR_REL,
    ])

    # keep.md 는 상류에 없지만 계획 인벤토리(dest_map)에 있으면 상류가 공급하는 파일이다.
    assert pm_update._retired_manifest_files(
        source, manifest, dest, {f"{RETIRED_DIR_REL}/keep.md"}) == []
    # dest_map 이 비면 그때만 후보 — target-owned 항목은 어느 경우에도 후보가 아니다.
    assert pm_update._retired_manifest_files(source, manifest, dest, set()) == [
        f"{RETIRED_DIR_REL}/keep.md"
    ]


def test_retired_manifest_files_reports_whole_directory_removed_upstream(
        pm_update, tmp_path):
    """상류 디렉토리 엔트리가 통째로 사라지면 dest 잔존 **전부**를 보고한다.

    옛 판정은 source 디렉토리 부재를 만나면 즉시 continue 해, 가장 크게 잔존하는 형상(디렉토리
    통째 은퇴)이 가장 조용했다. `@target-owned` 제외 규칙은 그대로 우선한다 — 그쪽은 상류 부재가
    정상이라 애초에 후보가 아니다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    (source / ".project_manager").mkdir(parents=True)
    dest_dir = dest / RETIRED_DIR_REL
    (dest_dir / "nested").mkdir(parents=True)
    (dest_dir / "a.md").write_text("# a\n", encoding="utf-8")
    (dest_dir / "nested" / "b.md").write_text("# b\n", encoding="utf-8")
    # `@target-owned` 대조군 — 상류 부재가 정상이라 통째 삭제 형상에서도 후보가 아니다.
    owned_dir = dest / ".opencode" / "lib"
    owned_dir.mkdir(parents=True)
    (owned_dir / "relay.cjs").write_text("// 타깃 고유\n", encoding="utf-8")
    manifest = _manifest_entries(pm_update, [
        RETIRED_DIR_REL,
        ".opencode/lib    @target-owned",
    ])

    retired = pm_update._retired_manifest_files(source, manifest, dest, set())

    assert retired == [
        f"{RETIRED_DIR_REL}/a.md", f"{RETIRED_DIR_REL}/nested/b.md",
    ], f"상류 디렉토리 통째 소멸 시 dest 잔존이 누락됨: {retired}"
    # 계획이 공급하는 좌표는 여전히 접힌다(제외 규칙 우선순위 유지).
    assert pm_update._retired_manifest_files(
        source, manifest, dest, {f"{RETIRED_DIR_REL}/a.md"}) == [
        f"{RETIRED_DIR_REL}/nested/b.md"
    ]


def test_retired_manifest_files_skips_deleted_but_indexed_paths(pm_update, tmp_path):
    """working tree 에서 지웠지만 index 에 남은 경로는 은퇴 후보가 아니다 (T-0577 축·codex 지적).

    dest 열거는 `repo_owned_files` OWNED(=git index)라, 삭제-미commit 파일이 계속 후보로 뜬다.
    이 보고의 명제는 "dest 에 잔존한다" 이므로 잔존물이 없으면 보고할 것도 없다 — 없는 파일을
    "수동 정리 판단" 대상으로 매 sync 마다 들이미는 노이즈를 닫는다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    (source / RETIRED_DIR_REL).mkdir(parents=True)
    shipped_dir = dest / RETIRED_DIR_REL
    shipped_dir.mkdir(parents=True)
    (shipped_dir / "kept.md").write_text("# 잔존\n", encoding="utf-8")
    deleted = shipped_dir / "deleted.md"
    deleted.write_text("# 곧 삭제\n", encoding="utf-8")
    _track_source_tree(dest)  # 두 파일 모두 index 에 등록.
    manifest = _manifest_entries(pm_update, [RETIRED_DIR_REL])

    assert sorted(pm_update._retired_manifest_files(source, manifest, dest, set())) == [
        f"{RETIRED_DIR_REL}/deleted.md", f"{RETIRED_DIR_REL}/kept.md",
    ], "픽스처 전제 붕괴 — 두 파일 모두 후보여야 한다"

    deleted.unlink()  # working tree 에서만 삭제(index 엔 잔존).

    assert pm_update._retired_manifest_files(source, manifest, dest, set()) == [
        f"{RETIRED_DIR_REL}/kept.md"
    ], "index 에만 남은 삭제분이 은퇴 후보로 보고됐다(디스크 잔존물 없음)"


def test_changes_labels_upstream_delete_as_not_removed_by_sync(
        pm_update, tmp_path, monkeypatch, capsys):
    """`--changes` 는 상류 삭제를 '받는 것' 이 아니라 '동기가 지우지 않음' 버킷에 싣는다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".claude/skills"])
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    runner = _make_fake_git_runner(
        head="head0000",
        log_lines=["abc1234 스킬 은퇴"],
        diff_lines=[
            "M\t.claude/skills/pm-update/SKILL.md",
            "D\t.claude/skills/spike-new/SKILL.md",
        ],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0

    out = capsys.readouterr().out
    assert "엔진 영향 (manifest 경로·이번 동기가 받는 것): 1 files" in out
    engine_block = out.split("엔진 영향")[1].split("그 외 변경")[0]
    assert ".claude/skills/spike-new/SKILL.md" not in engine_block, \
        "상류 삭제가 '이번 동기가 받는 것' 으로 오보됨"
    assert "상류 삭제·rename (동기가 지우지 않음 — 아래 보고 참조): 1 files" in out
    removed_block = out.split("상류 삭제·rename")[1]
    assert "D .claude/skills/spike-new/SKILL.md" in removed_block


def test_changes_inherits_guest_channel_split(
        pm_update, tmp_path, monkeypatch, capsys):
    """`--changes` 가 guest 절 채널 분리를 계획과 **같은 기준**으로 상속한다 (SF-8).

    분리가 `_main` 에만 있던 동안 미리보기는 guest 렌더물을 "이번 동기가 받는 것" 으로, guest 엔진
    행을 "동기 안 받음" 으로 표시해 정확히 반대로 말했다 — `_resolve_planning_manifest` 안으로
    옮겨 "미리보기 == 계획" 주장이 guest 축에서도 참이 되게 한다."""
    framework = _make_guest_framework(tmp_path / "fw")
    dest = tmp_path / "adopter"
    _make_legacy_guest_adopter(pm_update, dest)
    _write_local_conf(dest, f"upstream.path={framework}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    runner = _make_fake_git_runner(
        head="head0000",
        log_lines=["abc1234 guest 드라이버 교체"],
        diff_lines=[
            "M\ttemplates/opencode/.opencode/pm_orch_opencode.py",   # 엔진 행(update 채널)
            "M\ttemplates/opencode/.opencode/agents/pm.md",          # 렌더물(refresh 채널)
        ],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0
    out = capsys.readouterr().out
    engine_block = out.split("엔진 영향")[1].split("그 외 변경")[0]
    assert "엔진 영향 (manifest 경로·이번 동기가 받는 것): 1 files" in out, out
    assert "templates/opencode/.opencode/pm_orch_opencode.py" in engine_block, \
        f"guest 엔진 행 변경이 미리보기에서 '안 받음' 으로 오분류: {out!r}"
    assert "templates/opencode/.opencode/agents/pm.md" not in engine_block, \
        f"guest 렌더물(refresh 채널)이 미리보기에서 '받는 것' 으로 오분류: {out!r}"
    assert "그 외 변경 (manifest 밖·동기 안 받음): 1 files" in out, out


def test_changes_prints_retired_report_for_dangling_pointer(
        pm_update, tmp_path, monkeypatch, capsys):
    """`--changes` 의 "아래 보고 참조" 가 실제 은퇴 보고를 가리킨다 (SF-5).

    보고 호출이 없으면 그 헤더는 아무 데도 가리키지 않는 빈 포인터였다. **파일 엔트리 삭제**도
    같은 채널이 본다 — 상류에서 통째로 사라진 등재가 dest 에 파일로 잔존하는 형상이다."""
    dest = tmp_path / "dest"
    source = tmp_path / "checkout"
    _make_dir_entry_upstream(source, {f"{RETIRED_DIR_REL}/keep.md": "# keep\n"})
    _make_source_with_manifest(
        source, [RETIRED_DIR_REL, ".claude/settings-legacy.json"])
    dest_dir = dest / RETIRED_DIR_REL
    dest_dir.mkdir(parents=True)
    (dest_dir / "keep.md").write_text("# keep\n", encoding="utf-8")
    (dest_dir / "retired.md").write_text("# 상류 은퇴\n", encoding="utf-8")
    # 파일 엔트리 자체가 상류에서 사라진 형상 — dest 엔 잔존한다.
    (dest / ".claude").mkdir(parents=True)
    (dest / ".claude" / "settings-legacy.json").write_text("{}\n", encoding="utf-8")
    _write_local_conf(dest, f"upstream.path={source}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    runner = _make_fake_git_runner(
        head="head0000",
        log_lines=["abc1234 은퇴"],
        diff_lines=[f"D\t{RETIRED_DIR_REL}/retired.md"],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0
    out = capsys.readouterr().out
    assert "상류 부재 파일" in out, f"'아래 보고 참조' 가 빈 포인터: {out!r}"
    assert f"{RETIRED_DIR_REL}/retired.md" in out
    assert ".claude/settings-legacy.json" in out, \
        "파일 엔트리 삭제가 은퇴 보고 채널에 없다(디렉토리 하위만 스캔)"


def test_retired_report_caps_listing_with_remainder_count(pm_update, capsys):
    """은퇴 후보 나열은 상한을 두되 건수는 정확히 알린다 (suggestion 채택)."""
    limit = pm_update._RETIRED_REPORT_LIST_LIMIT
    retired = [f"adapter/f{index:03d}.md" for index in range(limit + 7)]

    pm_update._print_retired_manifest_files(retired)

    out = capsys.readouterr().out
    assert f"상류 부재 파일 {limit + 7}건" in out, "총 건수가 잘렸다(정보 손실)"
    assert out.count("adapter/f") == limit, "나열 상한이 적용되지 않았다"
    assert "외 7건" in out, f"잔여 건수 표기 누락: {out!r}"


def test_retired_scan_does_not_leak_repo_files_fallback_warning(
        pm_update, tmp_path, recwarn):
    """비-git dest 은퇴 스캔이 엔진 내부 폴백 경고를 채택자에게 노출하지 않는다 (SF-7).

    열거는 폴백으로 정상 동작한다 — 진단 보고 하나 때문에 seam 경고 원문을 띄울 이유가 없다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"  # git repo 아님 → filesystem 폴백 경로.
    _make_dir_entry_upstream(source, {f"{RETIRED_DIR_REL}/keep.md": "# keep\n"})
    dest_dir = dest / RETIRED_DIR_REL
    dest_dir.mkdir(parents=True)
    (dest_dir / "keep.md").write_text("# keep\n", encoding="utf-8")
    (dest_dir / "retired.md").write_text("# 상류 은퇴\n", encoding="utf-8")
    manifest = _manifest_entries(pm_update, [RETIRED_DIR_REL])

    assert pm_update._retired_manifest_files(source, manifest, dest, set()) == [
        f"{RETIRED_DIR_REL}/retired.md"
    ], "폴백 억제가 열거 자체를 죽였다(보고 0)"
    leaked = [w for w in recwarn.list
              if type(w.message).__name__ == "RepoFilesFallbackWarning"]
    assert not leaked, f"엔진 내부 폴백 경고가 raw 노출됨: {[str(w.message) for w in leaked]}"


def test_retired_planned_filter_uses_dest_coordinates_only(pm_update, tmp_path):
    """`planned` 대조는 dest 좌표만 본다 — 상류 좌표 혼입이 잔존물을 삼키지 않는다 (suggestion)."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    upstream_dir = source / "templates" / "codex" / ".codex" / "agents"
    upstream_dir.mkdir(parents=True)
    (upstream_dir / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    dest_dir = dest / ".codex" / "agents"
    dest_dir.mkdir(parents=True)
    (dest_dir / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    (dest_dir / "retired.md").write_text("# 상류 은퇴\n", encoding="utf-8")
    manifest = _manifest_entries(pm_update, [
        ".codex/agents    @source=templates/codex/.codex/agents",
    ])

    # dest 좌표만 담긴 인벤토리 — 잔존물은 그대로 후보다.
    dest_only = {".codex/agents/reviewer.md"}
    assert pm_update._retired_manifest_files(source, manifest, dest, dest_only) == [
        ".codex/agents/retired.md"
    ]
    # dest 좌표에 잔존물이 있으면(=상류가 공급) 후보에서 빠진다 — 필터 자체는 살아 있다.
    assert pm_update._retired_manifest_files(
        source, manifest, dest, dest_only | {".codex/agents/retired.md"}) == []


# ── T-0876: manifest 명시 퇴역 경로(backup + atomic move) ─────────────────

_RETIRED_REVIEWER_OLD = ".project_manager/tools/external_review.py"
_RETIRED_REVIEWER_NEW = ".project_manager/tools/additional_reviewer.py"
_RETIRED_REVIEWER_DIRECTIVE = (
    f"# pm-retired-path: {_RETIRED_REVIEWER_OLD} -> {_RETIRED_REVIEWER_NEW}"
)


def _retired_path_fixture(pm_update, tmp_path, *, dest_new=True):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source_new = source / _RETIRED_REVIEWER_NEW
    source_new.parent.mkdir(parents=True)
    source_new.write_bytes(b"new reviewer\n")
    source_manifest = source / ".project_manager" / "engine.manifest"
    source_manifest.write_text(
        f"{_RETIRED_REVIEWER_NEW}\n{_RETIRED_REVIEWER_DIRECTIVE}\n",
        encoding="utf-8",
    )
    old = dest / _RETIRED_REVIEWER_OLD
    old.parent.mkdir(parents=True)
    old.write_bytes(b"locally modified old reviewer\n")
    if dest_new:
        new = dest / _RETIRED_REVIEWER_NEW
        new.write_bytes(source_new.read_bytes())
    manifest = [pm_update.ManifestEntry(_RETIRED_REVIEWER_NEW)]
    return source, dest, source_manifest, manifest, old


def test_retired_path_parser_rejects_duplicate_conflict_cycle_and_escape(
        pm_update, tmp_path):
    manifest = tmp_path / "engine.manifest"
    invalid = [
        (
            "# pm-retired-path: a.py -> b.py\n"
            "# pm-retired-path: a.py -> b.py\n",
            "OLD 중복",
        ),
        (
            "# pm-retired-path: a.py -> b.py\n"
            "# pm-retired-path: b.py -> a.py\n",
            "순환",
        ),
        ("# pm-retired-path: ../a.py -> b.py\n", "repo-relative POSIX"),
        ("# pm-retired-path: C:\\\\a.py -> b.py\n", "repo-relative POSIX"),
    ]
    for text, expected in invalid:
        manifest.write_text(text, encoding="utf-8")
        with pytest.raises(pm_update.RetiredPathError, match=expected):
            pm_update.parse_retired_path_directives(manifest)


def test_retired_path_dry_run_accepts_planned_new_but_changes_zero_requires_hash(
        pm_update, tmp_path):
    source, dest, source_manifest, manifest, old = _retired_path_fixture(
        pm_update, tmp_path, dest_new=False,
    )
    source_new = source / _RETIRED_REVIEWER_NEW

    planned = pm_update.retire_manifest_paths(
        dest, source, manifest, [source_manifest], write=False,
        prospective_replacements={_RETIRED_REVIEWER_NEW: source_new},
    )

    assert planned and planned[0][0] == _RETIRED_REVIEWER_OLD
    assert old.read_bytes() == b"locally modified old reviewer\n"
    assert not (dest / ".pm_import_backups").exists(), "dry-run이 backup 디렉터리를 쓸"
    with pytest.raises(pm_update.RetiredPathError, match="destination replacement"):
        pm_update.retire_manifest_paths(
            dest, source, manifest, [source_manifest], write=False,
        )


def test_retired_path_moves_modified_old_bytes_and_mode_then_is_idempotent(
        pm_update, tmp_path):
    source, dest, source_manifest, manifest, old = _retired_path_fixture(
        pm_update, tmp_path,
    )
    original = old.read_bytes()
    if posix_mode_supported():
        old.chmod(0o751)

    moved = pm_update.retire_manifest_paths(
        dest, source, manifest, [source_manifest], write=True,
    )

    assert not old.exists()
    assert len(moved) == 1
    backup = moved[0][1]
    assert backup.read_bytes() == original
    if posix_mode_supported():
        assert stat.S_IMODE(backup.stat().st_mode) == 0o751
    assert pm_update.retire_manifest_paths(
        dest, source, manifest, [source_manifest], write=True,
    ) == []


def test_retired_path_hash_or_symlink_failure_keeps_old_and_writes_no_backup(
        pm_update, tmp_path):
    source, dest, source_manifest, manifest, old = _retired_path_fixture(
        pm_update, tmp_path,
    )
    (dest / _RETIRED_REVIEWER_NEW).write_bytes(b"wrong generation\n")
    with pytest.raises(pm_update.RetiredPathError, match="hash 불일치"):
        pm_update.retire_manifest_paths(
            dest, source, manifest, [source_manifest], write=True,
        )
    assert old.is_file()
    assert not (dest / ".pm_import_backups").exists()

    (dest / _RETIRED_REVIEWER_NEW).write_bytes(
        (source / _RETIRED_REVIEWER_NEW).read_bytes()
    )
    old.unlink()
    old.symlink_to(source / _RETIRED_REVIEWER_NEW)
    with pytest.raises(pm_update.RetiredPathError, match="symlink/reparse"):
        pm_update.retire_manifest_paths(
            dest, source, manifest, [source_manifest], write=True,
        )
    assert old.is_symlink()


def test_retired_path_os_replace_failure_keeps_old_and_surfaces_error(
        pm_update, tmp_path, monkeypatch):
    source, dest, source_manifest, manifest, old = _retired_path_fixture(
        pm_update, tmp_path,
    )

    def blocked_replace(_old, _backup):
        raise PermissionError("open handle blocks rename")

    monkeypatch.setattr(pm_update.os, "replace", blocked_replace)
    with pytest.raises(PermissionError, match="open handle"):
        pm_update.retire_manifest_paths(
            dest, source, manifest, [source_manifest], write=True,
        )
    assert old.is_file()


def test_retired_path_blocked_backup_keeps_old(pm_update, tmp_path):
    source, dest, source_manifest, manifest, old = _retired_path_fixture(
        pm_update, tmp_path,
    )
    date_root = dest / ".pm_import_backups" / pm_update.datetime.date.today().isoformat()
    date_root.parent.mkdir(parents=True)
    date_root.write_text("backup path blocker\n", encoding="utf-8")

    with pytest.raises(OSError):
        pm_update.retire_manifest_paths(
            dest, source, manifest, [source_manifest], write=True,
        )
    assert old.is_file()


def test_retired_path_main_failure_suppresses_baseline(pm_update, tmp_path, monkeypatch):
    source, dest, _source_manifest, _manifest, old = _retired_path_fixture(
        pm_update, tmp_path,
    )
    _write_dest_manifest(dest, [_RETIRED_REVIEWER_NEW, _RETIRED_REVIEWER_DIRECTIVE])
    _track_source_tree(source)
    monkeypatch.setattr(pm_update, "REPO", dest)
    baseline_calls = []
    monkeypatch.setattr(
        pm_update, "converge_upstream_revs",
        lambda *a, **k: baseline_calls.append((a, k)) or True,
    )
    monkeypatch.setattr(
        pm_update.os, "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("open handle")),
    )

    assert pm_update.main(["--from", str(source)]) == 1
    assert old.is_file()
    assert baseline_calls == [], "retired move 실패 뒤 baseline을 전진시킴"


def test_retired_path_main_dry_run_predicts_install_then_changes_zero_retires(
        pm_update, tmp_path, monkeypatch, capsys):
    source, dest, _source_manifest, _manifest, old = _retired_path_fixture(
        pm_update, tmp_path, dest_new=False,
    )
    _write_dest_manifest(dest, [_RETIRED_REVIEWER_NEW, _RETIRED_REVIEWER_DIRECTIVE])
    _track_source_tree(source)
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_update.main(["--from", str(source), "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "퇴역 예정" in preview and _RETIRED_REVIEWER_OLD in preview
    assert old.is_file() and not (dest / _RETIRED_REVIEWER_NEW).exists()

    # 구 updater가 RUN1에서 신 파일을 설치한 상태를 재현한다. 새 updater의
    # changes=0 RUN2가 바로 backup+퇴역을 닫아야 하며 baseline보다 먼저다.
    (dest / _RETIRED_REVIEWER_NEW).write_bytes(
        (source / _RETIRED_REVIEWER_NEW).read_bytes()
    )
    monkeypatch.setattr(pm_update, "converge_upstream_revs", lambda *a, **k: False)
    monkeypatch.setattr(pm_update, "_verify_engine_rev_convergence", lambda *a, **k: True)
    assert pm_update.main(["--from", str(source)]) == 0
    assert not old.exists()
    backups = list((dest / ".pm_import_backups").glob(
        "*/.project_manager/tools/external_review.py"
    ))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"locally modified old reviewer\n"


def test_changes_reports_rename_old_path_leaving_manifest(
        pm_update, tmp_path, monkeypatch, capsys):
    """manifest 밖으로 나가는 rename 은 낡은 dest 파일이 잔존한다 — old 경로가 보고에 뜬다."""
    fake_repo = tmp_path / "fake_repo"
    source = tmp_path / "checkout"
    _make_source_with_manifest(source, [".claude/skills"])
    _write_local_conf(fake_repo, f"upstream.path={source}\nupstream.rev=base12345678\n")
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    runner = _make_fake_git_runner(
        head="head0000",
        log_lines=["abc1234 스킬을 manifest 밖으로 이동"],
        diff_lines=["R100\t.claude/skills/moved/SKILL.md\tdocs/moved/SKILL.md"],
    )
    _patch_upstream_runner(pm_update, monkeypatch, runner)

    assert pm_update.main(["--changes"]) == 0

    out = capsys.readouterr().out
    removed_block = out.split("상류 삭제·rename")[1]
    assert "R .claude/skills/moved/SKILL.md" in removed_block, \
        f"rename old 경로가 잔존 보고에 없다: {out!r}"
    assert "그 외 변경 (manifest 밖·동기 안 받음): 1 files" in out, \
        "rename 새 경로(manifest 밖)는 종전대로 '그 외' 분류여야 한다"


def test_summarize_rename_inside_manifest_keeps_new_path_as_engine(pm_update, tmp_path):
    """manifest 안 rename: 새 경로는 engine(상류가 공급)·낡은 경로는 removed_upstream(잔존)."""
    source = tmp_path / "src"
    source.mkdir()
    manifest = _manifest_entries(pm_update, [".project_manager/tools"])
    runner = _make_fake_git_runner(
        head="h",
        log_lines=["aaa 도구 rename"],
        diff_lines=["R100\t.project_manager/tools/old.py\t.project_manager/tools/new.py"],
    )

    s = pm_update.summarize_upstream_changes(source, "base", manifest, git_runner=runner)

    assert ("R", ".project_manager/tools/new.py") in s["engine"]
    assert ("R", ".project_manager/tools/old.py") in s["removed_upstream"]


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
    _track_source_tree(root)


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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
    _write_local_conf(fake_repo, f"upstream.path={source}\n")
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
# engine safety-훅(ctx-stop)·엔진 도구가 manifest **밖**이면 채택자는 import 시점 frozen 사본을
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
    _track_source_tree(upstream)

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


def test_self_update_bare_engine_file_propagates(pm_update, tmp_path):
    """bare(root-sourced) 파일 행: 루트 실재분도 self-update 로 전파.

    ctx 훅은 @source(ship 템플릿) remap 이지만 엔진 도구 `.project_manager/tools/*.py` 28행은
    루트에 실재라 bare 등록(agents/skills 동형). bare 엔트리는 source_root/<rel> 을 그대로 읽어
    dest 로 복사한다."""
    upstream = tmp_path / "framework"
    adopter = tmp_path / "adopter"
    rel = ".project_manager/tools/board.py"

    src = upstream / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# NEW engine tool\n", encoding="utf-8")
    _track_source_tree(upstream)
    frozen = adopter / rel
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text("# OLD frozen tool\n", encoding="utf-8")

    entries = _manifest_entries(pm_update, [rel])  # bare (source_rel None → 루트 상대 = rel)
    changes, missing = pm_update.plan(upstream, entries, dest_root=adopter, render_enabled=True)
    assert not missing
    assert [c[0] for c in changes] == [rel]
    pm_update.apply(changes)
    assert "NEW engine tool" in frozen.read_text(encoding="utf-8")


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
    _track_source_tree(root)


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
    _write_local_conf(fake_repo, f"upstream.path={fake_repo}\n")

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
def test_reinstall_protected_hooks_deploys_adopter_self_test_contract(pm_update, tmp_path):
    """pm_update 재설치는 adopter mode와 repo test_cmd sidecar를 새 훅 본문과 함께 배포한다."""
    dest = _make_pm_home(tmp_path / "adopter-home")
    _write_local_conf(dest, f"upstream.path={tmp_path / 'framework-upstream'}\n")

    result = pm_update.reinstall_protected_hooks(dest, write=True)

    assert result["failed"] == [] and result["drifted"] == ["svc"]
    hooks = _hook_dir(dest, "svc")
    assert (hooks / "gate-contract").read_text(encoding="utf-8") == (
        "self-test\npytest -q\n")
    body = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "self-test)" in body and "sh -c \"$self_test_cmd\"" in body


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
    assert "[경고]" in cap.err and "pm-update" in cap.err and "repo add" in cap.err


def test_reinstall_protected_hooks_absorbs_marked_skew_at_recovery_boundary(
        pm_update, tmp_path, monkeypatch):
    """목적지 엔진 skew가 부가 훅 수렴을 막아도 update 복구 채널은 계속 열린다."""
    skew = RuntimeError("injected engine rev skew")
    skew._engine_rev_skew = True
    monkeypatch.setattr(pm_update, "_load_dest_pm_config", lambda _dest: (_ for _ in ()).throw(skew))

    result = pm_update.reinstall_protected_hooks(tmp_path, write=True)

    assert result["status"] == "unavailable"
    assert "사본 불일치" in result["reason"] and "복구 sync는 유지" in result["reason"]


@_git_required
def test_main_sync_stays_available_when_post_sync_dest_engine_is_skewed(
        pm_update, tmp_path, monkeypatch, capsys):
    """실제 main sync는 marked skew를 loud하게 보고하고도 파일 복구와 rc=0을 완료한다."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    _stub_no_baseline_git(pm_update, monkeypatch)
    skew = RuntimeError("injected post-sync engine skew")
    skew._engine_rev_skew = True
    monkeypatch.setattr(
        pm_update,
        "_load_dest_pm_config",
        lambda _dest: (_ for _ in ()).throw(skew),
    )

    rc = pm_update.main([])

    assert rc == 0
    assert (dest / SENTINEL_REL).read_text(encoding="utf-8") == (
        source / SENTINEL_REL
    ).read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "[경고]" in captured.err and "사본 불일치" in captured.err


@_git_required
def test_main_reinstalls_protected_hooks_after_apply(pm_update, tmp_path, monkeypatch, capsys):
    """실 sync(main) — apply 후 등록 repo 전수 훅 재설치가 실제로 돈다 (T-0415 트리거 배선).

    엔진 업그레이드 = `pm_update` sync 다. 그 경로에 재설치가 안 걸리면 새 훅은 우리 clone 에서만
    돌고 채택자에겐 영영 안 간다(ADR-0071 재설치 트리거 신설 근거)."""
    dest = _make_pm_home(tmp_path / "home")
    source = tmp_path / "upstream"
    _make_upstream(source)                       # sentinel 1개 = changes>0
    _write_local_conf(dest, f"upstream.path={source}\n")
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
    _write_local_conf(dest, f"upstream.path={source}\n")
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
    _write_local_conf(dest, f"upstream.path={source}\n")
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
    _write_local_conf(dest, f"upstream.path={source}\n")
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
@pytest.mark.parametrize(
    "artifact", ["pre-commit", "pre-push", "protected", "engine-root", "gate-contract"])
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
    gate_mode, test_cmd = _pc._protected_push_gate_config("svc", board=_board)
    spec = worktree_pool.protected_hook_artifacts(
        "svc", ["main", "release"], gate_mode=gate_mode, test_cmd=test_cmd)
    assert spec, "명세가 비었다(테스트 전제 깨짐)"

    for artifact in spec:
        name = artifact.path.name
        # ① 내용 훼손 → drift → 재설치로 복구.
        artifact.path.write_text("tampered\n", encoding="utf-8")
        assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"], \
            f"{name} 내용 훼손이 drift 로 안 잡힘(미검사 축)"
        assert artifact.path.read_text(encoding="utf-8") == artifact.content
        # ② 실행권한 상실 → drift → 0755 복구 (실행권한을 요구하는 산출물만).
        if artifact.executable and posix_mode_supported():
            artifact.path.chmod(0o644)
            assert pm_update.reinstall_protected_hooks(dest, write=True)["drifted"] == ["svc"], \
                f"{name} 실행권한 상실이 drift 로 안 잡힘 — git 이 훅을 조용히 건너뛴다"
            assert artifact.path.stat().st_mode & 0o111, f"{name} 0755 복구 실패"
        # ③ 전부 정합이면 조용(무한 재설치 회귀 방지).
        assert pm_update.reinstall_protected_hooks(dest, write=True)["in_sync"] == ["svc"]


@_git_required
@pytest.mark.skipif(
    not posix_mode_supported(), reason="chmod 실행 비트 왕복을 지원하지 않는 filesystem"
)
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
@pytest.mark.parametrize(
    "artifact", ["pre-commit", "pre-push", "protected", "engine-root", "gate-contract"])
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


def test_protected_hook_in_sync_rethrows_marked_engine_rev_skew(pm_update):
    """A1 — read-only drift 판정도 marked skew를 일반 drift(False)로 흡수하지 않는다."""
    skew = RuntimeError("injected engine rev skew")
    skew._engine_rev_skew = True
    pm_config = SimpleNamespace(
        _resolve_repo_protected=lambda *_args, **_kwargs: (_ for _ in ()).throw(skew))
    worktree_pool = SimpleNamespace(protected_hook_artifacts=lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="engine rev skew"):
        pm_update._protected_hook_in_sync(
            "svc", pm_config=pm_config, worktree_pool=worktree_pool, board=object())


# ── `--paths` 경로 스코프 (opt-in 부분 전파) ────────────────────────────────
# 기본 sync 는 manifest 전량이라 all-or-nothing 이다 — 한 파일만 내보내야 할 때 엔진 밖 수동 복사로
# 우회하면 안전 판정·render 를 통째로 건너뛴다. 이 축이 그 필요를 엔진 안에서 처리한다:
# 명시 경로만 전파 · manifest 등재분에 한정(미등재 rc1) · 부분 전파라 "전량 흡수" 후속 단계 비발화.


def _make_upstream_tree(root: Path, files: dict, manifest_entries: list) -> None:
    """source(upstream) 트리 — 임의 파일 집합 + 직접 지정한 manifest 등재 목록.

    `_make_upstream_manifest` 는 등재 1줄=파일 1개라 **디렉토리 등재**(재귀 동기)를 못 만든다.
    경로 스코프의 핵심 성질(디렉토리 등재 안에서 파일 하나만 고르기)이 그 형상에서만 재현된다.
    """
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    manifest = root / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(manifest_entries) + "\n", encoding="utf-8")
    _track_source_tree(root)


_SCOPE_FILES = {
    ".project_manager/tools/__scope_alpha__.py": "# alpha\n",
    ".project_manager/tools/__scope_beta__.py": "# beta\n",
    "adapterdir/one.md": "# one\n",
    "adapterdir/two.md": "# two\n",
}
_SCOPE_MANIFEST = [
    ".project_manager/tools/__scope_alpha__.py",
    ".project_manager/tools/__scope_beta__.py",
    "adapterdir",
]


def _scope_fixture(pm_update, tmp_path, monkeypatch, name: str) -> tuple[Path, Path]:
    """(dest=self-location REPO, source) — 파일 2개 + 디렉토리 등재 1개를 가진 upstream."""
    dest = tmp_path / f"{name}_dest"
    source = tmp_path / f"{name}_source"
    _make_upstream_tree(source, _SCOPE_FILES, _SCOPE_MANIFEST)
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    return dest, source


def _landed(dest: Path) -> set:
    return {
        p.relative_to(dest).as_posix()
        for p in dest.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(dest).parts
        and p.relative_to(dest).as_posix() != ".project_manager/local.conf"
    }


def test_paths_scope_propagates_only_the_named_file(pm_update, tmp_path, monkeypatch):
    """명시한 파일 하나만 착지한다 — 같은 manifest 의 나머지 등재분은 손대지 않는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "single")

    rc = pm_update.main(["--paths", ".project_manager/tools/__scope_alpha__.py"])

    assert rc == 0
    assert _landed(dest) == {".project_manager/tools/__scope_alpha__.py"}


def test_paths_scope_picks_one_file_inside_a_directory_entry(pm_update, tmp_path, monkeypatch):
    """디렉토리 등재 **안의 파일 하나**도 고를 수 있다 — 스코프가 항목 단위가 아니라 파일 단위다.

    manifest 항목으로 자르면 `adapterdir` 등재 전체가 딸려 온다(요청하지 않은 파일 전파). plan 이
    산출한 파일 목록에서 고르므로 경로 리매핑·render 판정은 그대로 물려받는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "indir")

    rc = pm_update.main(["--paths", "adapterdir/one.md"])

    assert rc == 0
    assert _landed(dest) == {"adapterdir/one.md"}


def test_paths_scope_directory_request_covers_its_files(pm_update, tmp_path, monkeypatch):
    """디렉토리를 지정하면 그 아래 전부 — 파일/디렉토리 지정이 같은 규칙을 탄다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "dir")

    rc = pm_update.main(["--paths", "adapterdir"])

    assert rc == 0
    assert _landed(dest) == {"adapterdir/one.md", "adapterdir/two.md"}


def test_paths_scope_accepts_repeated_and_multi_value_flags(pm_update, tmp_path, monkeypatch):
    """반복 지정과 다중 값이 함께 쌓인다(누적) — 마지막 `--paths` 가 앞을 덮지 않는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "multi")

    rc = pm_update.main([
        "--paths", ".project_manager/tools/__scope_alpha__.py", "adapterdir/one.md",
        "--paths", ".project_manager/tools/__scope_beta__.py",
    ])

    assert rc == 0
    assert _landed(dest) == {
        ".project_manager/tools/__scope_alpha__.py",
        ".project_manager/tools/__scope_beta__.py",
        "adapterdir/one.md",
    }


def test_paths_scope_refuses_unregistered_path_loudly(pm_update, tmp_path, monkeypatch, capsys):
    """manifest 미등재 경로는 조용한 무전파가 아니라 rc1 — 오타·인스턴스 소유 파일 지정을 잡는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "unreg")

    rc = pm_update.main(["--paths", ".project_manager/wiki/status.md"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "[미등재] .project_manager/wiki/status.md" in err, err
    assert _landed(dest) == set(), "거부인데 파일이 착지함"


@pytest.mark.parametrize(
    "case, bad",
    [("abs", "/etc/passwd"), ("dotdot", "../outside.py"),
     ("inner_dotdot", ".project_manager/../../x"), ("blank", "   ")])
def test_paths_scope_refuses_unsafe_values_before_any_work(
        pm_update, tmp_path, monkeypatch, capsys, case, bad):
    """절대경로·`..`·빈 값은 입구에서 거부한다(스코프가 저장소 밖을 가리킬 수 없다)."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, f"bad_{case}")

    rc = pm_update.main(["--paths", bad])

    err = capsys.readouterr().err
    assert rc == 1
    assert "[거부]" in err and "--paths" in err, err
    assert _landed(dest) == set()


def test_paths_scope_is_refused_with_changes_mode(pm_update, tmp_path, monkeypatch, capsys):
    """`--changes`(read-only 확인)와는 함께 쓸 수 없다 — 조용히 무시되는 옵션 0."""
    _dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "chg")

    rc = pm_update.main(["--changes", "--paths", "adapterdir"])

    assert rc == 1
    assert "--paths" in capsys.readouterr().err


def test_paths_scope_dry_run_writes_nothing(pm_update, tmp_path, monkeypatch, capsys):
    """dry-run 은 스코프 안 계획만 보여주고 파일을 만들지 않는다(무변경 계약 유지)."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "dry")

    rc = pm_update.main(["--dry-run", "--paths", "adapterdir/one.md"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "adapterdir/one.md" in out and "adapterdir/two.md" not in out, out
    assert _landed(dest) == set()


def test_paths_scope_does_not_record_upstream_baseline(pm_update, tmp_path, monkeypatch):
    """부분 전파는 baseline 을 갱신하지 않는다 — 갱신하면 미전파분이 drift-lint 에서 사라진다.

    같은 형상에서 스코프 없이 돌리면 baseline 이 기록된다(대조군) — 억제가 이 모드 고유임을 못박는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "baseline")
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "scopedrev7")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    assert pm_update.main(["--paths", "adapterdir/one.md"]) == 0
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert "upstream.rev" not in conf, "부분 전파가 baseline 을 최신으로 박음(거짓 최신)"

    assert pm_update.main([]) == 0  # 대조군 — 전량 sync 는 기존대로 기록한다.
    conf = pm_update._read_local_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream.rev") == "scopedrev7"


def test_paths_scope_skips_whole_instance_steps(pm_update, tmp_path, monkeypatch):
    """진입 doc 마이그레이션·보호 훅 재설치는 발화하지 않는다 — 요청 밖 write 0."""
    _dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "steps")
    called: list = []
    monkeypatch.setattr(
        pm_update, "migrate_entry_doc",
        lambda *a, **k: called.append("migrate") or {"status": "skipped"})
    monkeypatch.setattr(
        pm_update, "reinstall_protected_hooks",
        lambda *a, **k: called.append("hooks") or {"status": "skipped"})
    monkeypatch.setattr(
        pm_update, "_run_retired_path_migration",
        lambda *a, **k: called.append("retire") or True)

    assert pm_update.main(["--paths", "adapterdir/one.md"]) == 0

    assert called == [], f"부분 전파가 전량 흡수 후속 단계를 태웠다: {called}"


def test_target_mode_never_runs_retired_path_migration(
        pm_update, tmp_path, monkeypatch):
    fake_repo = tmp_path / "manager"
    (fake_repo / "templates" / "tgt").mkdir(parents=True)
    source = tmp_path / "source"
    _make_upstream(source)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)
    monkeypatch.setattr(
        pm_update, "_run_retired_path_migration",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("target retire fired")),
    )

    assert pm_update.main([
        "--from", str(source), "--target", "tgt", "--dry-run",
    ]) == 0


def test_paths_scope_forwards_to_every_target(pm_update, tmp_path, monkeypatch, capsys):
    """`--all-targets` 와 조합 — 각 타깃이 같은 스코프만 받는다(전파 채널 하나·자식 재검증)."""
    fake_repo = tmp_path / "targets_repo"
    for target in ("alpha", "beta"):
        (fake_repo / "templates" / target).mkdir(parents=True)
    source = tmp_path / "targets_source"
    _make_upstream_tree(source, _SCOPE_FILES, _SCOPE_MANIFEST)
    monkeypatch.setattr(pm_update, "REPO", fake_repo)

    rc = pm_update.main(
        ["--from", str(source), "--all-targets", "--paths", "adapterdir/one.md"])

    assert rc == 0
    for target in ("alpha", "beta"):
        assert _landed(fake_repo / "templates" / target) == {"adapterdir/one.md"}


def test_scope_helpers_normalize_and_match(pm_update):
    """정규화·등재 판정·후보 파생의 단위 규칙(호출부가 의존하는 성질)."""
    normalized, rejected = pm_update._normalize_scope_paths(
        [" a/b/ ", "a\\b", "a/b", "", "/abs", "x/../y"])
    assert normalized == ["a/b"]  # 공백·백슬래시·꼬리 슬래시·중복 접기
    assert len(rejected) == 3

    manifest = ["pkg", "tools/one.py"]
    # 양방향 겹침 — 디렉토리 등재 아래 파일, 여러 등재를 담는 상위 디렉토리 모두 등재로 본다.
    assert pm_update._unregistered_scope_paths(manifest, ["pkg/deep/f.md"]) == []
    assert pm_update._unregistered_scope_paths(manifest, ["tools"]) == []
    assert pm_update._unregistered_scope_paths(manifest, ["other/f.md"]) == ["other/f.md"]

    # `@source` 리매핑 항목은 dest·upstream 경로가 다르다 — 둘 다 후보여야 어느 쪽으로 지정해도 걸린다.
    source_root = Path("/src")
    assert pm_update._scope_change_candidates(
        ".opencode/agents/pm.md", source_root / "templates/opencode/.opencode/agents/pm.md",
        source_root) == [".opencode/agents/pm.md",
                         "templates/opencode/.opencode/agents/pm.md"]
    # 인메모리 source(manifest 합집합)는 파일 경로가 없어 dest 후보만 남는다(TypeError 로 안 샌다).
    assert pm_update._scope_change_candidates(
        ".project_manager/engine.manifest", pm_update._ManifestTextSource("x"),
        source_root) == [".project_manager/engine.manifest"]


def test_paths_scope_reports_a_missing_source_entry_that_holds_the_request(
        pm_update, tmp_path, monkeypatch, capsys):
    """요청 파일을 담은 등재가 source 에 통째로 없으면 rc2 — '변경 없음' 거짓 성공 0.

    부재 보고는 항목 단위(디렉토리)라 스코프 판정이 단방향이면 그 보고가 접혀 사라진다."""
    dest = tmp_path / "missing_dest"
    source = tmp_path / "missing_source"
    _make_upstream_tree(
        source,
        {".project_manager/tools/__scope_alpha__.py": "# alpha\n"},
        [".project_manager/tools/__scope_alpha__.py", "adapterdir"],
    )
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--paths", "adapterdir/one.md"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "adapterdir" in err, err


def test_paths_scope_matches_equivalent_path_spellings(pm_update, tmp_path, monkeypatch):
    """`./a/b`·`a//b` 는 `a/b` 와 같은 요청이다 — 정규화 결과를 저장·매칭한다.

    원본 표기를 그대로 들고 있으면 등재 검증은 통과하고 변경 매칭만 빗나가 **조용한 rc0 무전파**가
    된다(요청했는데 아무것도 안 갔는데 성공으로 끝난다)."""
    for index, spelling in enumerate(("./adapterdir/one.md", "adapterdir//one.md")):
        dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, f"spell{index}")

        rc = pm_update.main(["--paths", spelling])

        assert rc == 0
        assert _landed(dest) == {"adapterdir/one.md"}, f"{spelling!r} 가 매칭되지 않음"


def test_scope_normalization_folds_equivalent_spellings(pm_update):
    """정규화 단위 규칙 — 동치 표기는 한 항목으로 접히고 `.`/`..` 는 사유별로 거부된다."""
    normalized, rejected = pm_update._normalize_scope_paths(
        ["a/b", "./a/b", "a//b", ".", "../x"])
    assert normalized == ["a/b"]
    assert len(rejected) == 2
    assert any("자기 참조" in reason for reason in rejected)
    assert any(".." in reason for reason in rejected)


def test_paths_scope_refusal_writes_nothing_to_dest(
        pm_update, tmp_path, monkeypatch, capsys):
    """미등재 거부는 **어떤 dest 쓰기보다 먼저**다 — 중앙 로더 선복구도 돌지 않는다.

    거부인데 seam 복구 write 가 선행하면 "rc1 인데 파일이 바뀐다" 가 된다(무변경 계약 위반)."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "nowrite")
    predeployed: list = []
    monkeypatch.setattr(
        pm_update, "_predeploy_central_loader",
        lambda *a, **k: predeployed.append("called"))

    rc = pm_update.main(["--paths", ".project_manager/wiki/status.md"])

    assert rc == 1
    assert predeployed == [], "거부 전에 중앙 로더 선복구(dest 쓰기)가 돌았다"
    assert _landed(dest) == set()
    assert "[미등재]" in capsys.readouterr().err


def test_paths_scope_accepts_guest_clause_path(pm_update, tmp_path, monkeypatch, capsys):
    """guest 절 경로는 정상 스코프 대상이다 — 절의 렌더물도 update 채널이 전파하기 때문이다.

    옛 판정("guest 절 소속이라 지정 불가")은 렌더물을 계획에서 빼던 시절의 진단이라 소멸했다."""
    dest = tmp_path / "guest_dest"
    source = tmp_path / "guest_source"
    _make_upstream_tree(source, _SCOPE_FILES, _SCOPE_MANIFEST)
    _write_local_conf(dest, f"upstream.path={source}\n")
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "\n".join(_SCOPE_MANIFEST) + "\n"
        + pm_update._GUEST_MANIFEST_BEGIN + "\n"
        + ".opencode/agents @render @target-owned\n"
        + pm_update._GUEST_MANIFEST_END + "\n",
        encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--paths", ".opencode/agents"])

    err = capsys.readouterr().err
    assert rc == 0, f"guest 절 경로가 미등재로 거부됐다: {err!r}"
    assert "[미등재]" not in err, err


def test_paths_scope_warns_on_engine_rev_mixing(pm_update, tmp_path, monkeypatch, capsys):
    """엔진 도구가 스코프에 들면 rev 혼재 가능성을 알린다 — 차단이 아니라 경고(최종 방어는 전량 전파)."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "revmix")

    rc = pm_update.main(["--paths", ".project_manager/tools/__scope_alpha__.py"])

    err = capsys.readouterr().err
    assert rc == 0
    assert "ENGINE_REV" in err and "all-targets" in err, err
    # 어댑터만 옮기는 스코프는 조용하다(경고가 무차별이 아님).
    dest2, _s2 = _scope_fixture(pm_update, tmp_path, monkeypatch, "revquiet")
    capsys.readouterr()
    assert pm_update.main(["--paths", "adapterdir/one.md"]) == 0
    assert "ENGINE_REV" not in capsys.readouterr().err


def test_paths_scope_reports_missing_source_for_source_remapped_entry(
        pm_update, tmp_path, monkeypatch, capsys):
    """`@source` 항목을 **upstream 경로**로 지정했는데 그 source 가 없으면 rc2 — 접히지 않는다.

    부재 보고는 dest 경로(manifest 경로)로 오므로, 스코프 대조가 dest 좌표만 보면 upstream 경로로
    지정한 요청이 스코프 밖으로 접혀 "변경 없음 rc0" 라는 거짓 성공이 된다."""
    dest = tmp_path / "remap_dest"
    source = tmp_path / "remap_source"
    _make_upstream_tree(
        source,
        {".project_manager/tools/__scope_alpha__.py": "# alpha\n"},
        [".project_manager/tools/__scope_alpha__.py",
         ".opencode/agents @render @source=templates/opencode/.opencode/agents"],
    )
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--paths", "templates/opencode/.opencode/agents"])

    err = capsys.readouterr().err
    assert rc == 2, f"source 부재가 접혀 성공으로 끝남(err={err!r})"
    assert ".opencode/agents" in err, err


def test_paths_scope_refuses_a_typo_under_a_registered_directory(
        pm_update, tmp_path, monkeypatch, capsys):
    """등재 **디렉토리 안의 오타**는 등재 검증을 통과하지만 대응 파일이 없다 — rc1 로 멈춘다.

    `adapterdir/typo.md` 는 `adapterdir` 등재와 겹쳐 소유권 판정을 통과하고, 변경 0 은 "이미 최신"
    과 구분되지 않는다 → 옛 형상은 아무것도 전파하지 않고 rc0 으로 끝났다(조용한 무전파)."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "typo")

    rc = pm_update.main(["--paths", "adapterdir/typo.md"])

    err = capsys.readouterr().err
    assert rc == 1, "오타가 조용한 rc0 무전파로 끝남"
    assert "[대응 없음] adapterdir/typo.md" in err, err
    assert _landed(dest) == set()


def test_paths_scope_accepts_an_already_in_sync_path(pm_update, tmp_path, monkeypatch):
    """이미 최신인 경로는 정상이다 — 대응 검증이 "변경 0" 을 오타로 오분류하지 않는다."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "insync")
    assert pm_update.main(["--paths", "adapterdir/one.md"]) == 0
    assert _landed(dest) == {"adapterdir/one.md"}

    rc = pm_update.main(["--paths", "adapterdir/one.md"])  # 두 번째 실행 — 변경 0.

    assert rc == 0
    assert _landed(dest) == {"adapterdir/one.md"}


def test_paths_scope_accepts_the_manifest_union_self_prop(pm_update, tmp_path, monkeypatch):
    """`engine.manifest` 를 스코프로 지정할 수 있다 — 합집합 분기도 계획 인벤토리에 실린다.

    그 분기는 upstream 에 단일 실파일이 없어 인메모리 source 로 계획된다. 인벤토리에서 빠지면
    유효 변경인데도 "대응 없음" 으로 거부돼(rc1) 스스로 만든 게이트에 막힌다."""
    dest = tmp_path / "union_dest"
    source = tmp_path / "union_source"
    _make_upstream_tree(source, _SCOPE_FILES, _SCOPE_MANIFEST + [".project_manager/engine.manifest"])
    # flavor 합집합 경로 재현 — selfheal 이 여러 upstream manifest 를 계획 기준으로 올린다.
    for flavor in ("claude_code", "codex"):
        flavor_manifest = source / "templates" / flavor / ".project_manager" / "engine.manifest"
        flavor_manifest.parent.mkdir(parents=True, exist_ok=True)
        flavor_manifest.write_text(
            "\n".join(_SCOPE_MANIFEST + [".project_manager/engine.manifest"]) + "\n",
            encoding="utf-8")
    _track_source_tree(source)
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--paths", ".project_manager/engine.manifest"])

    assert rc == 0, "합집합 self-prop 좌표가 인벤토리에서 빠져 유효 요청이 거부됨"
    assert (dest / ".project_manager" / "engine.manifest").is_file()
    assert _landed(dest) == {".project_manager/engine.manifest"}


def _loader_rel() -> str:
    return ".project_manager/tools/repo_owned_files.py"


def test_paths_scope_does_not_touch_the_central_loader(
        pm_update, tmp_path, monkeypatch, capsys):
    """스코프 밖 중앙 로더는 갱신하지 않는다 — "명시 경로만 전파" 계약(내용·mtime 불변).

    복구가 필요한 상태면 **알린다**(조용한 방치 금지) — 전량 sync 로 안내."""
    dest, _source = _scope_fixture(pm_update, tmp_path, monkeypatch, "loader")
    stale = dest / _loader_rel()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# 구형 seam(복구 대상)\n", encoding="utf-8")
    before_bytes = stale.read_bytes()
    before_mtime = stale.stat().st_mtime_ns
    assert pm_update.central_loader_needs_recovery(dest), "픽스처 전제(복구 대상 상태)"

    rc = pm_update.main(["--paths", "adapterdir/one.md"])

    err = capsys.readouterr().err
    assert rc == 0
    assert stale.read_bytes() == before_bytes, "스코프 밖 파일이 갱신됨(계약 위반)"
    assert stale.stat().st_mtime_ns == before_mtime
    assert "경로 스코프 밖이라 건드리지 않는다" in err, err


def test_loader_in_scope_is_still_recovered(pm_update, tmp_path, monkeypatch):
    """그 경로를 스코프에 넣으면 정상 복구된다(기능 제거가 아니라 스코프 준수)."""
    dest = tmp_path / "loader_in_scope"
    source = tmp_path / "loader_source"
    _make_upstream_tree(
        source,
        {**_SCOPE_FILES, _loader_rel(): (REPO / _loader_rel()).read_text(encoding="utf-8")},
        _SCOPE_MANIFEST + [_loader_rel()],
    )
    _write_local_conf(dest, f"upstream.path={source}\n")
    stale = dest / _loader_rel()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# 구형 seam\n", encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--paths", _loader_rel()])

    assert rc == 0
    assert not pm_update.central_loader_needs_recovery(dest), "스코프 안인데 복구되지 않음"


def test_paths_scope_accepts_board_separated_remap(pm_update, tmp_path, monkeypatch):
    """board 분리 인스턴스의 리매핑 좌표(`board/tickets/_template.md`)를 등재로 인정한다.

    계획은 그 좌표로 착지시키는데 게이트만 manifest 원문(`wiki/tickets/_template.md`)을 보면,
    실제 관리 파일을 요청해도 rc1 로 거부된다(스스로 만든 게이트에 막힘)."""
    dest = tmp_path / "board_dest"
    source = tmp_path / "board_source"
    template_rel = ".project_manager/wiki/tickets/_template.md"
    board_rel = ".project_manager/board/tickets/_template.md"
    _make_upstream_tree(source, {template_rel: "# ticket 템플릿\n"}, [template_rel])
    (dest / ".project_manager" / "board" / "tickets").mkdir(parents=True)  # board 분리 형상.
    _write_local_conf(dest, f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update._is_board_separated(dest), "픽스처 전제(board 분리 판정)"

    rc = pm_update.main(["--paths", board_rel])

    assert rc == 0, "board 분리 좌표를 미등재로 거부함"
    assert (dest / board_rel).read_text(encoding="utf-8") == "# ticket 템플릿\n"


# ── instance-owned 어댑터 config 채널 (T-0585) ────────────────────────────────
# 채널의 하한선을 못박는 절이다: **채택자 커스텀은 절대 안 덮는다**. 자동 갱신은 "설치가 내려놓은
# 그대로"(dest 해시 == 원장 해시)인 managed 대상 하나뿐이고, 그 밖은 전부 보존 + 보고다.
# 픽스처는 합성 프레임워크다 — 실 template 내용에 의존하면 상류 문구가 바뀔 때마다 이 절이
# 무관하게 깨진다(채널 동작이 판정 대상이지 특정 파일 본문이 아니다).

_ADAPTER_HOOKS_REL = ".codex/hooks.json"
_ADAPTER_CONFIG_REL = ".codex/config.toml"
_UPSTREAM_HOOKS = '{"hooks": {"PreCompact": ["upstream 비차단 안내"]}}\n'
_UPSTREAM_CONFIG = 'sandbox_mode = "workspace-write"\n'
_INSTALLED_HOOKS = '{"hooks": {"PreCompact": ["설치 시점 차단판"]}}\n'
_EDITED_HOOKS = '{"hooks": {"PreCompact": ["채택자 손편집"]}}\n'


def _make_adapter_config_case(
    tmp_path: Path, *, hooks: str, config: str = _UPSTREAM_CONFIG,
    ledger: dict | None = None,
) -> tuple[Path, Path]:
    """codex 채택자 + 합성 프레임워크 — (dest, source_root).

    `installed_harnesses` 는 설치 기록을 진실로 쓰므로 install.json 을 심는다(구조 증거도 함께
    둬 유령 형상 경고가 판정 출력에 섞이지 않게 한다)."""
    source = tmp_path / "framework"
    template = source / "templates" / "codex"
    (template / ".codex").mkdir(parents=True)
    (template / ".codex" / "hooks.json").write_text(_UPSTREAM_HOOKS, encoding="utf-8")
    (template / ".codex" / "config.toml").write_text(_UPSTREAM_CONFIG, encoding="utf-8")

    dest = tmp_path / "adopter"
    (dest / ".codex").mkdir(parents=True)
    (dest / ".agents").mkdir(parents=True)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / "AGENTS.md").write_text("# adopter 진입 문서\n", encoding="utf-8")
    (dest / _ADAPTER_HOOKS_REL).write_text(hooks, encoding="utf-8")
    (dest / _ADAPTER_CONFIG_REL).write_text(config, encoding="utf-8")
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["codex"]}\n', encoding="utf-8")
    if ledger is not None:
        (dest / ".project_manager" / "adapter_baseline.json").write_text(
            json.dumps({"schema": 1, "files": ledger}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return dest, source


def _ledger_entry(text: str) -> dict:
    """원장 항목 — 그 내용으로 레이다운했다는 기록(해시만 판정에 쓰인다)."""
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "recorded_at": "2026-01-01T00:00:00+09:00",
        "template_rev": "deadbeef",
    }


def _read_ledger(dest: Path) -> dict:
    path = dest / ".project_manager" / "adapter_baseline.json"
    return json.loads(path.read_text(encoding="utf-8"))["files"] if path.is_file() else {}


def test_adapter_config_unedited_is_backed_up_updated_and_rerecorded(
        pm_update, tmp_path, capsys):
    """무편집(dest 해시 == 원장 해시) managed → 백업 + 현행 template 갱신 + 원장 갱신 + 재승인 안내.

    이게 이 티켓이 여는 채널 전부다 — 상류의 동작 fix(훅 차단→비차단)가 기존 채택자에 도달하는
    유일한 자동 경로. 재승인 안내가 빠지면 훅이 조용히 비활성 상태로 남는다(조용한 degrade)."""
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_INSTALLED_HOOKS,
        ledger={_ADAPTER_HOOKS_REL: _ledger_entry(_INSTALLED_HOOKS)})

    result = pm_update.sync_adapter_configs(dest, source, write=True)
    pm_update._print_adapter_config_finding(result, dry_run=False)

    assert [item["relpath"] for item in result["updated"]] == [_ADAPTER_HOOKS_REL]
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _UPSTREAM_HOOKS
    backup = dest / result["updated"][0]["backup_rel"]
    assert backup.read_text(encoding="utf-8") == _INSTALLED_HOOKS, \
        "덮기 전 값이 백업되지 않음(비파괴 위반)"
    assert _read_ledger(dest)[_ADAPTER_HOOKS_REL]["sha256"] == hashlib.sha256(
        _UPSTREAM_HOOKS.encode("utf-8")).hexdigest(), "원장이 새 값으로 갱신되지 않음"
    out = capsys.readouterr().out
    assert "/hooks" in out, f"재승인 안내 부재(조용한 훅 비활성화): {out!r}"


def test_adapter_config_edited_is_preserved_with_accept_guidance(
        pm_update, tmp_path, capsys):
    """채택자 편집(dest 해시 != 원장 해시) → 보존 + loud 보고 + 수용 커맨드 안내(갱신 0)."""
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_EDITED_HOOKS,
        ledger={_ADAPTER_HOOKS_REL: _ledger_entry(_INSTALLED_HOOKS)})

    result = pm_update.sync_adapter_configs(dest, source, write=True)
    pm_update._print_adapter_config_finding(result, dry_run=False)

    assert result["updated"] == [], "편집분을 덮었다(하한선 위반)"
    assert [item["status"] for item in result["preserved"]] == ["edited"]
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _EDITED_HOOKS
    err = capsys.readouterr().err
    assert "sync-adapter-config --accept .codex/hooks.json" in err, \
        f"수용 커맨드 안내 부재(보고 모드 탈출 경로 미제시): {err!r}"


def test_adapter_config_unrecorded_is_preserved_as_safe_default(
        pm_update, tmp_path, capsys):
    """원장 부재(구세대 채택자) → 판정 불가이므로 보존 + 보고 — 안전 기본값.

    원장이 없다는 건 "손댔는지 모른다" 이지 "안 댔다" 가 아니다. 여기서 덮으면 원장 도입 전
    채택자 전원의 커스텀이 업그레이드 한 번에 사라진다."""
    dest, source = _make_adapter_config_case(tmp_path, hooks=_INSTALLED_HOOKS)

    result = pm_update.sync_adapter_configs(dest, source, write=True)
    pm_update._print_adapter_config_finding(result, dry_run=False)

    assert result["updated"] == []
    assert [item["status"] for item in result["preserved"]] == ["unrecorded"]
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _INSTALLED_HOOKS
    assert "원장 부재" in capsys.readouterr().err


@pytest.mark.parametrize("ledger_state", ["absent", "matching", "stale"])
def test_adapter_config_report_only_target_is_never_updated(
        pm_update, tmp_path, capsys, ledger_state):
    """보고-전용 대상은 **어떤 원장 상태에서도** 갱신되지 않고 파일당 한 줄로만 표기된다.

    managed 와 갈리는 지점이 여기다 — 원장이 무편집을 증명해도 갱신하지 않는다(그 파일엔 채택자
    노브가 실재해 자동 갱신이 값을 지울 수 있다). 매 sync 반복 표기라 한 줄 상한도 함께 본다."""
    adopter_config = _UPSTREAM_CONFIG + "# 채택자 노브\n"
    ledger = {
        "absent": None,
        "matching": {_ADAPTER_CONFIG_REL: _ledger_entry(adopter_config)},
        "stale": {_ADAPTER_CONFIG_REL: _ledger_entry("다른 세대\n")},
    }[ledger_state]
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_UPSTREAM_HOOKS, config=adopter_config, ledger=ledger)

    result = pm_update.sync_adapter_configs(dest, source, write=True)
    pm_update._print_adapter_config_finding(result, dry_run=False)

    assert result["updated"] == [], f"{ledger_state}: 보고-전용 대상을 갱신했다"
    assert [item["relpath"] for item in result["drift"]] == [_ADAPTER_CONFIG_REL]
    assert (dest / _ADAPTER_CONFIG_REL).read_text(encoding="utf-8") == adopter_config
    per_file = [line for line in capsys.readouterr().out.splitlines()
                if _ADAPTER_CONFIG_REL in line]
    assert len(per_file) == 1, f"{ledger_state}: 파일당 1줄 상한 위반: {per_file}"


def test_adapter_config_dry_run_changes_nothing(pm_update, tmp_path, capsys):
    """dry-run 은 판정만 한다 — 파일도 원장도 안 바뀐다(무변경 계약)."""
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_INSTALLED_HOOKS,
        ledger={_ADAPTER_HOOKS_REL: _ledger_entry(_INSTALLED_HOOKS)})
    before_ledger = _read_ledger(dest)

    result = pm_update.sync_adapter_configs(dest, source, write=False)
    pm_update._print_adapter_config_finding(result, dry_run=True)

    assert [item["relpath"] for item in result["updated"]] == [_ADAPTER_HOOKS_REL], \
        "판정 자체가 사라짐(dry-run 이 무동작이 아니라 무write 여야 한다)"
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _INSTALLED_HOOKS
    assert _read_ledger(dest) == before_ledger
    assert not (dest / ".pm_import_backups").exists(), "dry-run 이 백업을 만들었다"
    assert "갱신 예정" in capsys.readouterr().out


def test_adapter_config_in_sync_backfills_ledger_without_touching_file(
        pm_update, tmp_path):
    """이미 상류와 같은 파일은 원장만 뒤늦게 채운다 — 구세대 채택자의 보고 모드 탈출 자동 경로.

    backfill 이 없으면 손댄 적 없는 채택자도 원장이 비어 영구 `unrecorded`(보존+보고)에 갇힌다."""
    dest, source = _make_adapter_config_case(tmp_path, hooks=_UPSTREAM_HOOKS)
    assert _read_ledger(dest) == {}, "픽스처 전제(원장 부재)"

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert sorted(result["backfilled"]) == sorted(
        {_ADAPTER_HOOKS_REL, _ADAPTER_CONFIG_REL})
    assert set(_read_ledger(dest)) == {_ADAPTER_HOOKS_REL, _ADAPTER_CONFIG_REL}
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _UPSTREAM_HOOKS
    assert not (dest / ".pm_import_backups").exists(), "무변경 파일에 백업을 만들었다"


def test_adapter_config_accept_enters_auto_update_track(pm_update, tmp_path):
    """수용 → 백업 + template 채택 + 원장 기록 → **다음 상류 변경이 자동 갱신**된다.

    구세대 채택자가 보고 모드를 빠져나가는 경로 전체를 한 번에 태운다(수용 없이는 영구 보고)."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_EDITED_HOOKS)
    assert pm_update.sync_adapter_configs(dest, source, write=True)["preserved"], \
        "픽스처 전제(수용 전에는 보존+보고)"

    outcome = pm_import.accept_adapter_config(
        dest, source, _ADAPTER_HOOKS_REL,
        expected_sha256=hashlib.sha256(_EDITED_HOOKS.encode("utf-8")).hexdigest())

    assert outcome.status == "accepted", outcome.detail
    assert Path(outcome.backup).read_text(encoding="utf-8") == _EDITED_HOOKS
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _UPSTREAM_HOOKS
    assert _read_ledger(dest)[_ADAPTER_HOOKS_REL]["sha256"] == hashlib.sha256(
        _UPSTREAM_HOOKS.encode("utf-8")).hexdigest()
    # 상류가 다시 움직인다 — 이제 무편집 판정이라 자동 갱신 궤도다.
    next_upstream = '{"hooks": {"PreCompact": ["다음 세대"]}}\n'
    (source / "templates" / "codex" / ".codex" / "hooks.json").write_text(
        next_upstream, encoding="utf-8")

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert [item["relpath"] for item in result["updated"]] == [_ADAPTER_HOOKS_REL], \
        "수용 후에도 자동 갱신 궤도에 못 들었다(원장 기록 누락)"
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == next_upstream


def test_adapter_baseline_ledger_is_not_registered_and_not_shipped(pm_update):
    """원장은 어느 manifest 에도 없고 어느 템플릿 트리에도 출하되지 않는다 (제3 분류).

    등재하면 byte-copy 가 채택자 원장을 상류 값으로 덮어 "무편집" 판정이 통째로 거짓이 된다.
    동시에 출하물이 아니므로(인스턴스가 런타임에 만드는 상태 파일) **출하-등재 역방향 가드
    (T-0584)의 스캔 대상 밖**이다 — 그 가드에 예외 목록을 둘 이유가 없다는 판정의 근거다."""
    pm_import = pm_update._load_pm_import()
    ledger_rel = pm_import.ADAPTER_BASELINE_RELPATH.as_posix()
    for manifest_path in _MANIFESTS:
        registered = {str(entry).replace("\\", "/")
                      for entry in pm_update.read_manifest(manifest_path)}
        assert ledger_rel not in registered, (
            f"{manifest_path} 가 원장을 등재 — byte-copy 가 채택자 판정 기준을 덮는다")
    shipped = [flavor for flavor in ("claude_code", "codex", "opencode")
               if (REPO / "templates" / flavor / ledger_rel).exists()]
    assert shipped == [], f"원장이 템플릿 트리에 출하됨(인스턴스 상태 파일이어야 한다): {shipped}"


def test_adapter_config_accept_refuses_when_file_changed_after_judgment(
        pm_update, tmp_path):
    """판정 뒤 동시 편집이 있으면 수용이 **검증 없이 덮지 않는다**(raced·파일 불변).

    판정과 쓰기 사이는 실재하는 창이다 — 그 사이 채택자/다른 프로세스가 파일을 바꿨는데 판정
    시점 결론으로 덮으면, 백업엔 남더라도 "무편집이라 안전" 이라는 전제 자체가 거짓이 된다."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_INSTALLED_HOOKS)
    judged_hash = hashlib.sha256(_INSTALLED_HOOKS.encode("utf-8")).hexdigest()
    concurrent = '{"hooks": {"PreCompact": ["판정 뒤 편집"]}}\n'
    (dest / _ADAPTER_HOOKS_REL).write_text(concurrent, encoding="utf-8")

    outcome = pm_import.accept_adapter_config(
        dest, source, _ADAPTER_HOOKS_REL, expected_sha256=judged_hash)

    assert outcome.status == "raced", outcome
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == concurrent, \
        "동시 편집분을 덮었다(판정-쓰기 레이스 미차단)"
    assert _read_ledger(dest) == {}, "덮지도 않았는데 원장을 기록했다"


def test_adapter_config_accept_reports_ledger_failure_instead_of_success(
        pm_update, tmp_path):
    """원장을 쓸 수 없으면 성공으로 반환하지 않는다 — "자동 갱신 궤도" 거짓 안내 차단.

    파일 교체는 성공했는데 판정 기준이 안 남으면 다음 동기가 이 파일을 영구 보고 모드로 본다.
    호출부(CLI·동기 채널)가 그 사실을 알아야 사람에게 복구 경로를 낼 수 있다."""
    pm_import = pm_import_module = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_EDITED_HOOKS)
    # 원장 쓰기를 실패시킨다 — 기록 경로를 **디렉토리**로 점유(권한 chmod 는 root 에서 무력).
    (dest / ".project_manager" / "adapter_baseline.json").mkdir()

    outcome = pm_import_module.accept_adapter_config(
        dest, source, _ADAPTER_HOOKS_REL,
        expected_sha256=hashlib.sha256(_EDITED_HOOKS.encode("utf-8")).hexdigest())

    assert outcome.status == "ledger-failed", outcome
    assert outcome.detail, "실패 사유가 비어 있다(호출부가 안내할 내용 없음)"
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _UPSTREAM_HOOKS, \
        "픽스처 전제(파일 교체 자체는 성공)"
    assert pm_import.read_adapter_baseline(dest).get("files") == {}


def test_adapter_config_future_schema_ledger_is_not_overwritten(pm_update, tmp_path):
    """상위 schema 원장은 **읽기도 쓰기도 거부**한다 — 미래 형식 데이터 무변조 왕복.

    해석 못 하는 문서를 빈 원장으로 접은 뒤 이 엔진 형식으로 다시 쓰면 신 엔진의 기록이 통째로
    파괴된다(읽기 거부와 쓰기 거부는 짝이어야 한다·설치 기록과 동형)."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_INSTALLED_HOOKS)
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    future = json.dumps(
        {"schema": 2, "files": {_ADAPTER_HOOKS_REL: {"sha256": "0" * 64,
                                                     "future_field": "미래 엔진 값"}}},
        ensure_ascii=False, indent=2) + "\n"
    ledger_path.write_text(future, encoding="utf-8")

    # 판정: 해석 불가 → 빈 원장 취급(=보존 쪽). 기록: 거부.
    assert pm_import.read_adapter_baseline(dest)["files"] == {}
    assert pm_import.record_adapter_baseline(dest, source) == []
    assert ledger_path.read_text(encoding="utf-8") == future, "미래 schema 원장을 덮었다"

    # 동기 채널·수용도 같은 판정을 받는다 — 파일은 손대지 않고 loud 하게 보존한다.
    result = pm_update.sync_adapter_configs(dest, source, write=True)
    assert result["updated"] == []
    assert ledger_path.read_text(encoding="utf-8") == future
    outcome = pm_import.accept_adapter_config(dest, source, _ADAPTER_HOOKS_REL)
    assert outcome.status == "ledger-blocked", outcome
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _INSTALLED_HOOKS, \
        "원장을 못 쓰는데 파일을 갈아치웠다(다음 동기가 영구 보고 모드로 본다)"


def test_adapter_config_write_is_atomic_replace_not_truncate_in_place(
        pm_update, tmp_path, monkeypatch):
    """교체는 임시 파일 → `os.replace` 다 — 쓰기 중 실패가 빈/부분 파일을 남기지 않는다.

    in-place `O_TRUNC` 재열기는 디스크 오류 시 원본을 날린 채 호출부엔 "원본 보존" 으로 보고되는
    창을 만든다. 임시 파일 write 단계에서 실패시켜 **원본 byte 불변 + 임시 파일 잔재 0** 을 본다."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_EDITED_HOOKS)

    def _fail_fsync(_fd):
        raise OSError("디스크 오류(주입)")

    monkeypatch.setattr(os, "fsync", _fail_fsync)  # 엔진이 쓰는 그 os 모듈 객체.

    outcome = pm_import.accept_adapter_config(
        dest, source, _ADAPTER_HOOKS_REL,
        expected_sha256=hashlib.sha256(_EDITED_HOOKS.encode("utf-8")).hexdigest())

    assert outcome.status == "write-failed", outcome
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _EDITED_HOOKS, \
        "쓰기 실패인데 원본이 훼손됐다(in-place 절단)"
    leftovers = [p.name for p in (dest / ".codex").iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"임시 파일 잔재: {leftovers}"


def test_adapter_config_unavailable_channel_is_loud_not_silent(
        pm_update, tmp_path, monkeypatch, capsys):
    """채널이 못 돌면(**pm_import 로드 실패 등**) 조용히 넘기지 않는다 (형제 훅 재설치 동형).

    조용한 skip 은 "상류 동작 fix 가 이 채택자에 안 갔다" 는 사실을 관측 불가로 만든다 —
    hooks.json 이 옛 세대로 남았는데 출력 0줄이면 사람이 알 방법이 없다."""
    dest, source = _make_adapter_config_case(tmp_path, hooks=_INSTALLED_HOOKS)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: None)

    result = pm_update.sync_adapter_configs(dest, source, write=True)
    pm_update._print_adapter_config_finding(result, dry_run=False)

    assert result["status"] == "unavailable" and result["reason"]
    err = capsys.readouterr().err
    assert "어댑터 config 채널을 건너뛰었다" in err, f"무음 skip(관측 불가): {err!r}"
    assert result["reason"] in err, "사유가 안 나와 사람이 원인을 못 짚는다"
    assert "sync-adapter-config --list" in err, "판정 조회 안내 부재"


def test_adapter_config_backfill_is_green_only_after_ledger_verification(
        pm_update, tmp_path, monkeypatch):
    """template byte가 같아도 원장 write 실패면 backfilled 성공/green으로 접지 않는다."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_UPSTREAM_HOOKS)
    monkeypatch.setattr(pm_import, "record_adapter_baseline", lambda *_a, **_kw: [])
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert result["managed_converged"] is False
    assert result["backfilled"] == [], "원장 기록 실패를 backfill 성공으로 보고함"
    assert result["blocking"] == [{
        "relpath": _ADAPTER_HOOKS_REL,
        "status": "unrecorded",
        "judgment_status": "in-sync",
    }]
    assert not (dest / ".project_manager" / "adapter_baseline.json").exists()


def test_adapter_config_ledger_exception_is_rc_contract_not_traceback(
        pm_update, tmp_path, monkeypatch):
    """원장 writer 예외도 파일 적용을 되돌리지 않고 managed red 결과로 번역한다."""
    pm_import = pm_update._load_pm_import()
    dest, source = _make_adapter_config_case(tmp_path, hooks=_UPSTREAM_HOOKS)

    def _raise(*_args, **_kwargs):
        raise OSError("ledger disk failure")

    monkeypatch.setattr(pm_import, "record_adapter_baseline", _raise)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert result["managed_converged"] is False
    assert result["blocking"][0]["status"] == "unrecorded"
    assert result["degraded"][0]["status"] == "ledger-failed"
    assert "ledger disk failure" in result["degraded"][0]["summary"]


@pytest.mark.parametrize("has_engine_change", (False, True))
def test_pm_update_adapter_nonconvergence_is_rc1_without_hiding_engine_result(
        pm_update, tmp_path, monkeypatch, capsys, has_engine_change):
    """changes 0/양수 모두 adapter 판정이 돌고, managed red는 엔진 적용을 보존한 채 rc1이다."""
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_EDITED_HOOKS,
        ledger={_ADAPTER_HOOKS_REL: _ledger_entry(_INSTALLED_HOOKS)})
    engine_rel = ".project_manager/tools/__adapter_gate_sentinel__.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# new engine payload\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    if not has_engine_change:
        local = dest / engine_rel
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(sentinel.read_bytes())
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    rc = pm_update.main(["--from", str(source)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "managed 어댑터 config가 미수렴" in captured.err
    assert "sync-adapter-config --accept" in captured.err
    assert "최신 — 변경 없음" not in captured.out, "adapter red를 최신으로 오보고함"
    assert (dest / _ADAPTER_HOOKS_REL).read_text(encoding="utf-8") == _EDITED_HOOKS
    if has_engine_change:
        assert (dest / engine_rel).read_bytes() == sentinel.read_bytes(), \
            "rc1 처리에서 이미 적용한 엔진 파일을 rollback함"
        assert "파일 동기화" in captured.out, "엔진 적용 사실을 숨김"


def test_pm_update_adapter_channel_unavailable_is_rc1(
        pm_update, tmp_path, monkeypatch, capsys):
    """판정 채널 unavailable은 loud 보고뿐 아니라 명령 전체를 non-green으로 만든다."""
    dest, source = _make_adapter_config_case(tmp_path, hooks=_UPSTREAM_HOOKS)
    engine_rel = ".project_manager/tools/__adapter_gate_sentinel__.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# same\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    local = dest / engine_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(sentinel.read_bytes())
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: None)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(source)]) == 1
    captured = capsys.readouterr()
    assert "어댑터 config 채널을 건너뛰었다" in captured.err
    assert "최신 — 변경 없음" not in captured.out


def test_pm_update_managed_dest_missing_source_template_is_rc1_without_rev_convergence(
        pm_update, tmp_path, monkeypatch, capsys):
    """managed 비교 template 부재는 changes=0에서도 rc1이고 최신/revision 완료를 기록하지 않는다."""
    dest, source = _make_adapter_config_case(
        tmp_path, hooks=_UPSTREAM_HOOKS,
        ledger={_ADAPTER_HOOKS_REL: _ledger_entry(_UPSTREAM_HOOKS)})
    (source / "templates" / "codex" / _ADAPTER_HOOKS_REL).unlink()
    engine_rel = ".project_manager/tools/__adapter_missing_template_sentinel__.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# same\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    local = dest / engine_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(sentinel.read_bytes())
    conf = _write_local_conf(
        dest, f"upstream.path={source}\nupstream.rev=old\nupstream.seen_rev=old\n")
    before_conf = conf.read_bytes()
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    def _must_not_converge(*_args, **_kwargs):
        raise AssertionError("adapter unavailable인데 revision baseline을 수렴시켰다")

    monkeypatch.setattr(pm_update, "converge_upstream_revs", _must_not_converge)

    assert pm_update.main(["--from", str(source)]) == 1
    captured = capsys.readouterr()
    assert "비교 기준 unavailable" in captured.err
    assert "최신 — 변경 없음" not in captured.out
    assert conf.read_bytes() == before_conf


def test_pm_update_partial_engine_without_adapter_candidates_is_vacuous_green(
        pm_update, tmp_path, monkeypatch, capsys):
    """source/dest config 후보가 모두 없는 partial recovery는 adapter channel을 요구하지 않는다."""
    dest = tmp_path / "partial-dest"
    source = tmp_path / "partial-source"
    engine_rel = ".project_manager/tools/recovery.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("# same\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    local = dest / engine_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(sentinel.read_bytes())
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("adapter 후보 0인데 판정 채널을 실행했다")

    monkeypatch.setattr(pm_update, "sync_adapter_configs", _must_not_run)

    assert pm_update.main(["--from", str(source)]) == 0
    captured = capsys.readouterr()
    assert "최신 — 변경 없음" in captured.out
    assert "어댑터 config 채널" not in captured.err


@pytest.mark.parametrize("kind", ("file", "directory", "broken-symlink"))
def test_adapter_config_outer_gate_counts_any_existing_path_entry(
        pm_update, tmp_path, kind):
    """외곽 gate가 중앙 판정 전부터 비정상 managed 경로를 조용히 제외하지 않는다."""
    candidate = tmp_path / _ADAPTER_HOOKS_REL
    candidate.parent.mkdir(parents=True)
    if kind == "file":
        candidate.write_text("{}\n", encoding="utf-8")
    elif kind == "directory":
        candidate.mkdir()
    else:
        candidate.symlink_to(tmp_path / "missing-hooks-target.json")

    assert pm_update._has_adapter_config_candidate(tmp_path) is True


def test_adapter_config_outer_gate_candidate_zero_is_false(pm_update, tmp_path):
    """순수 partial-engine 트리의 vacuous-green 전제: config 경로 엔트리 0."""
    assert pm_update._has_adapter_config_candidate(tmp_path) is False


def test_adapter_config_outer_candidate_paths_match_channel_declaration(pm_update):
    """복구용 외곽 후보 상수는 중앙 channel의 non-none 경로 전수와 정확히 일치한다."""
    pm_import = pm_update._load_pm_import()
    declared = {
        relpath
        for channel in pm_import.ADAPTER_CONFIG_CHANNEL.values()
        for relpath, mode in channel.items()
        if mode != pm_import.ADAPTER_CONFIG_NO_CHANNEL
    }
    assert set(pm_update._ADAPTER_CONFIG_DEST_CANDIDATES) == declared


def test_adapter_config_dry_run_in_sync_unrecorded_prescribes_real_backfill(
        pm_update, tmp_path, capsys):
    """dry-run은 accept 교체가 아니라 다음 실 pm-update의 byte-불변 원장 backfill을 안내한다."""
    dest, source = _make_adapter_config_case(tmp_path, hooks=_UPSTREAM_HOOKS)

    result = pm_update.sync_adapter_configs(dest, source, write=False)
    pm_update._print_adapter_config_finding(result, dry_run=True)

    err = capsys.readouterr().err
    assert "실 pm-update" in err and "backfill" in err
    assert "sync-adapter-config --accept" not in err


def test_pm_update_explicit_claude_receipt_ignores_foreign_codex_managed_file(
        pm_update, tmp_path, monkeypatch, capsys):
    """claude-only receipt가 foreign hooks 하나 때문에 codex red/overwrite로 뒤집히지 않는다."""
    dest = tmp_path / "claude-only"
    source = tmp_path / "source"
    engine_rel = ".project_manager/tools/recovery.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("# same\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    codex_template = source / "templates" / "codex" / _ADAPTER_HOOKS_REL
    codex_template.parent.mkdir(parents=True)
    codex_template.write_text(_UPSTREAM_HOOKS, encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    local = dest / engine_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(sentinel.read_bytes())
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["claude"]}\n', encoding="utf-8")
    foreign = dest / _ADAPTER_HOOKS_REL
    foreign.parent.mkdir(parents=True)
    foreign_body = _EDITED_HOOKS
    foreign.write_text(foreign_body, encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(source)]) == 0

    captured = capsys.readouterr()
    assert "최신 — 변경 없음" in captured.out
    assert "managed 어댑터 config가 미수렴" not in captured.err
    assert foreign.read_text(encoding="utf-8") == foreign_body


def test_pm_update_partial_managed_dest_blocks_zero_change_revision_convergence(
        pm_update, tmp_path, monkeypatch, capsys):
    """영수증/완전 shape가 없는 managed dest도 zero-change RUN의 완료 baseline을 막는다."""
    dest = tmp_path / "partial-dest"
    source = tmp_path / "partial-source"
    engine_rel = ".project_manager/tools/recovery.py"
    sentinel = source / engine_rel
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("# same\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        engine_rel + "\n", encoding="utf-8")
    template = source / "templates" / "codex" / _ADAPTER_HOOKS_REL
    template.parent.mkdir(parents=True)
    template.write_text(_UPSTREAM_HOOKS, encoding="utf-8")
    _track_source_tree(source)
    _write_dest_manifest(dest, [engine_rel])
    local = dest / engine_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(sentinel.read_bytes())
    hooks = dest / _ADAPTER_HOOKS_REL
    hooks.parent.mkdir(parents=True)
    hooks.write_text(_INSTALLED_HOOKS, encoding="utf-8")
    conf = _write_local_conf(
        dest, f"upstream.path={source}\nupstream.rev=old\nupstream.seen_rev=old\n")
    before_conf = conf.read_bytes()
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    def _must_not_converge(*_args, **_kwargs):
        raise AssertionError("partial managed 판정 실패인데 revision baseline을 수렴시켰다")

    monkeypatch.setattr(pm_update, "converge_upstream_revs", _must_not_converge)

    assert pm_update.main(["--from", str(source)]) == 1
    captured = capsys.readouterr()
    assert "managed 어댑터 config가 미수렴" in captured.err
    assert "sync-adapter-config --accept" in captured.err
    assert "최신 — 변경 없음" not in captured.out
    assert conf.read_bytes() == before_conf


def test_flat_opencode_command_rewrites_only_operational_detail_target(pm_update):
    source = (
        "before [references/operational-details.md]"
        "(references/operational-details.md) after\n"
    )
    assert pm_update._flat_command_skill_name(
        ".opencode/command/pm-review.md"
    ) == "pm-review"
    assert pm_update._flat_command_skill_name(
        ".claude/skills/pm-review/SKILL.md"
    ) is None
    assert pm_update._render_flat_command_reference(source, "pm-review") == (
        "before [references/operational-details.md]"
        "(../../.claude/skills/pm-review/references/operational-details.md) after\n"
    )


@pytest.mark.parametrize("source", ["no link\n", """
[references/operational-details.md](references/operational-details.md)
[references/operational-details.md](references/operational-details.md)
"""])
def test_flat_opencode_command_rewrite_fails_loud_on_link_count(pm_update, source):
    with pytest.raises(ValueError, match="정확히 1개"):
        pm_update._render_flat_command_reference(source, "pm-x")


# ── claude native agent 카드 model = delegate conf 해소값 (T-0731) ────────────
# native 스폰의 실효 모델은 카드 frontmatter 다. 카드가 리터럴이면 `delegate.<role>` 선언이
# 실행면에 닿지 못하고(채택자 실측: conf sonnet ↔ 카드 opus 상시 경고), `.claude/agents @render`
# 라 채택자가 고쳐도 다음 pm-update 가 되돌린다. 카드를 역할별 operational 토큰으로 렌더해
# local.conf 단일 진실이 실행면에 반영되게 한다.

CARD_MODEL_TOKENS = {
    "developer.md": "DELEGATE_MODEL_DEVELOPER",
    "developer-hard.md": "DELEGATE_MODEL_DEVELOPER_HARD",
    "researcher.md": "DELEGATE_MODEL_RESEARCHER",
    "architect.md": "DELEGATE_MODEL_ARCHITECT",
    "code-reviewer.md": "DELEGATE_MODEL_CODE_REVIEWER",
}
# 카드는 **두 트리 모두** 소스다: fresh import 는 templates 사본을 복사하고, 채택자 self-update 는
# manifest 의 bare `.claude/agents @render`(=<source-root>/.claude/agents)를 읽어 루트 사본을
# 렌더한다. 한쪽만 토큰-form 이면 그 채널만 정합해진다(byte-identical 가드가 둘을 묶는다).
CARD_TREES = (
    REPO / ".claude" / "agents",
    REPO / "templates" / "claude_code" / ".claude" / "agents",
)

CARD_ROLE_CONF_KEYS = {
    "DELEGATE_MODEL_DEVELOPER": "delegate.developer.model",
    "DELEGATE_MODEL_RESEARCHER": "delegate.researcher.model",
    "DELEGATE_MODEL_ARCHITECT": "delegate.architect.model",
    "DELEGATE_MODEL_CODE_REVIEWER": "delegate.code-reviewer.model",
}

# 위임 토큰 표 전체 — 4 역할 축 + developer hard 티어의 모델·추론 각 5키(T-0766: codex 역할
# 카드가 추론 필드를 갖게 되면서 추론 토큰도 5개가 됐다). 표가 조용히 줄면 그 카드가 영영
# TODO 로 중화되므로 기대 집합을 못박는다(가드 시야 == 표면).
DELEGATE_CONF_KEYS = {
    **CARD_ROLE_CONF_KEYS,
    "DELEGATE_REASONING_DEVELOPER": "delegate.developer.reasoning",
    "DELEGATE_REASONING_RESEARCHER": "delegate.researcher.reasoning",
    "DELEGATE_REASONING_ARCHITECT": "delegate.architect.reasoning",
    "DELEGATE_REASONING_CODE_REVIEWER": "delegate.code-reviewer.reasoning",
    "DELEGATE_MODEL_DEVELOPER_HARD": "delegate.developer.hard.model",
    "DELEGATE_REASONING_DEVELOPER_HARD": "delegate.developer.hard.reasoning",
}


def _card_model_line(text: str) -> str:
    """카드 frontmatter 의 model 줄(해소·중화 양쪽) — 없으면 빈 문자열."""
    for line in text.splitlines():
        if line.lstrip("# ").startswith("model:"):
            return line
    return ""


def test_delegate_model_tokens_wired_in_both_channels(pm_update):
    """위임 토큰 전부가 render 채널(OPERATIONAL_KEYS)과 local.conf 채널 양쪽에 배선.

    한쪽만 있으면 (a) 토큰이 영영 미해소 leak 이거나 (b) conf 값이 아무 산출도 안 바꾼다.
    배선은 표 하나(`DELEGATE_MODEL_CONF_KEYS`)의 파생이라, 표를 실제로 순회해 검사한다."""
    pm_render = pm_update._load_pm_render()
    assert pm_render.DELEGATE_MODEL_CONF_KEYS == DELEGATE_CONF_KEYS, \
        "위임 토큰 표가 기대 집합과 갈렸다(역할/티어가 조용히 늘거나 줄었다)"
    conf_channel = pm_update._local_conf_operational_map()
    for token_key, conf_key in pm_render.DELEGATE_MODEL_CONF_KEYS.items():
        assert token_key in pm_render.OPERATIONAL_KEYS, \
            f"{token_key} 가 render operational 채널에 미등재 — 재렌더가 토큰을 leak 시킨다"
        assert conf_channel.get(conf_key) == token_key, \
            f"local.conf `{conf_key}` → {token_key} 매핑 부재 — 선언이 실행면에 닿지 않는다"
    assert pm_render.DELEGATE_HARNESS_CONF_KEYS == {
        token_key: conf_key.rsplit(".", 1)[0] + ".harness"
        for token_key, conf_key in DELEGATE_CONF_KEYS.items()
    }, "미사용 프로필 판정이 읽는 harness 키가 모델 키와 같은 프로필을 가리키지 않는다"


def test_delegate_harness_from_local_conf_reads_profile_harness(pm_update, tmp_path):
    """`_delegate_harness_from_local_conf` 가 역할/티어별 harness 를 token-key 로 돌려준다.

    미설정/빈값은 담지 않는다 — 판정 불가는 "미사용 프로필" 이 아니라 현행 거동(해소값 렌더)이다."""
    dest = tmp_path / "dest"
    _write_local_conf(dest, "\n".join([
        "delegate.developer.harness=claude",
        "delegate.developer.hard.harness=codex",
        "delegate.architect.harness=",
        "",
    ]))

    assert pm_update._delegate_harness_from_local_conf(dest) == {
        "DELEGATE_MODEL_DEVELOPER": "claude",
        "DELEGATE_REASONING_DEVELOPER": "claude",
        "DELEGATE_MODEL_DEVELOPER_HARD": "codex",
        "DELEGATE_REASONING_DEVELOPER_HARD": "codex",
    }


def test_agent_cards_use_role_model_tokens_and_no_literal_model():
    """카드 5장 × 두 소스 트리 — `model:` 이 역할·티어별 토큰이고 리터럴 `opus` 0."""
    for tree in CARD_TREES:
        for name, token_key in CARD_MODEL_TOKENS.items():
            text = (tree / name).read_text(encoding="utf-8")
            assert _card_model_line(text) == 'model: "{{' + token_key + '}}"', (
                f"{tree.name}/{name} 의 model 이 역할 토큰이 아니다 — 카드가 리터럴이면 "
                f"delegate.* 선언이 native 스폰에 반영되지 않는다 (tree={tree})"
            )
            assert "opus" not in _card_model_line(text), (
                f"{tree}/{name} 에 리터럴 model 잔존 — 두 트리 모두 소스다"
                "(import=templates · self-update=루트 bare @render)"
            )


def test_agent_card_renders_conf_model_through_local_conf(pm_update, tmp_path):
    """dest local.conf 의 `delegate.<role>.model` 이 카드 렌더 결과가 된다(역할별 개별)."""
    pm_render = pm_update._load_pm_render()
    dest = tmp_path / "dest"
    _write_local_conf(dest, "\n".join([
        "delegate.developer.model=sonnet",
        "delegate.researcher.model=haiku",
        "delegate.architect.model=opus",
        "delegate.code-reviewer.model=gpt-5.6-sol",
        "project.name=Acme",
        "",
    ]))

    operational, empty_keys = pm_update._operational_from_local_conf(dest)

    assert empty_keys == []
    expected = {
        "developer.md": 'model: "sonnet"',
        "researcher.md": 'model: "haiku"',
        "architect.md": 'model: "opus"',
        "code-reviewer.md": 'model: "gpt-5.6-sol"',
    }
    template_tree = REPO / "templates" / "claude_code" / ".claude" / "agents"
    for name, model_line in expected.items():
        rendered = pm_render.render_adapter(
            (template_tree / name).read_text(encoding="utf-8"),
            operational,
            source=name,
        )
        assert _card_model_line(rendered) == model_line, \
            f"{name} 이 conf 해소값으로 렌더되지 않았다"


def test_agent_card_unset_delegate_model_is_graceful_todo(pm_update, tmp_path):
    """위임 매핑 미설정 채택자 — rc-fail 0·TODO 표기·다른 토큰은 정상 렌더(부분-graceful).

    미설정은 오설정이 아니라 정상 형상이다(local.conf 시드는 delegate.* 를 전부 주석으로 낸다).
    한 토큰 미해소가 어댑터 update 전체를 막으면 채택자가 엔진 fix 를 받을 수 없다
    (`{{OPENCODE_PRO_MODEL}}` 선례와 동형)."""
    pm_render = pm_update._load_pm_render()
    dest = tmp_path / "dest"
    _write_local_conf(dest, "project.name=Acme\ndelegate.researcher.model=haiku\n")

    operational, empty_keys = pm_update._operational_from_local_conf(dest)
    template_tree = REPO / "templates" / "claude_code" / ".claude" / "agents"
    rendered = pm_render.render_adapter(
        (template_tree / "developer.md").read_text(encoding="utf-8"),
        operational, source="developer.md", empty_keys=empty_keys,
    )

    line = _card_model_line(rendered)
    assert line.startswith('# model: "<model>"'), \
        f"미해소 토큰이 중화되지 않았다(leak 또는 활성 리터럴 출하): {line!r}"
    assert "delegate.developer.model=" in line, "TODO 가 채울 conf 키를 지목하지 않는다"
    assert "{{" not in rendered, "중화 산출물에 리터럴 토큰 잔존(자족 위반)"
    # 해소된 역할은 같은 실행에서 정상 렌더 — 미해소 하나가 전체를 끌어내리지 않는다.
    resolved = pm_render.render_adapter(
        (template_tree / "researcher.md").read_text(encoding="utf-8"),
        operational, source="researcher.md", empty_keys=empty_keys,
    )
    assert _card_model_line(resolved) == 'model: "haiku"'
    # 멱등 — 중화된 산출물을 다시 렌더해도 같은 bytes(재렌더 왕복 0).
    assert pm_render.render_adapter(
        rendered, operational, source="developer.md", empty_keys=empty_keys) == rendered


def test_agent_card_empty_delegate_model_is_loud_leak(pm_update, tmp_path):
    """`delegate.<role>.model=` 빈값(오설정)은 중화하지 않고 leak 으로 표면화한다."""
    pm_render = pm_update._load_pm_render()
    dest = tmp_path / "dest"
    _write_local_conf(dest, "project.name=Acme\ndelegate.developer.model=\n")

    operational, empty_keys = pm_update._operational_from_local_conf(dest)

    assert empty_keys == ["DELEGATE_MODEL_DEVELOPER"]
    template_tree = REPO / "templates" / "claude_code" / ".claude" / "agents"
    with pytest.raises(pm_render.RenderLeakError, match="delegate.developer.model"):
        pm_render.render_adapter(
            (template_tree / "developer.md").read_text(encoding="utf-8"),
            operational, source="developer.md", empty_keys=empty_keys,
        )


def _import_adopter(pm_import, dest: Path, harness: str = "claude") -> None:
    """fresh 채택자 설치 (출력 삼킴·라이브 하네스 미호출)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pm_import.main([
            "--new", str(dest), "--harness", harness,
            "--name", "Acme", "--fill", "manual",
        ])
    assert rc == 0, f"fresh import 실패(rc={rc}):\n{buf.getvalue()[-2000:]}"


def _pin_card_model(card: Path, model: str) -> None:
    """카드의 model 줄을 리터럴로 손편집한다(중화 주석줄도 활성 리터럴로 되돌린다)."""
    lines = []
    for line in card.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.lstrip("# ").startswith("model:"):
            eol = "\n" if line.endswith("\n") else ""
            lines.append(f'model: "{model}"' + eol)
        else:
            lines.append(line)
    card.write_text("".join(lines), encoding="utf-8", newline="")


def test_fresh_adopter_card_model_follows_delegate_conf_across_updates(
        pm_update, tmp_path, monkeypatch):
    """fresh-adopter e2e — conf 모델 ≠ opus 케이스로 도그푸딩 사각을 닫는다.

    adopter#0 은 conf 가 opus 라 카드 리터럴과 우연히 일치해 이 결함을 못 봤다
    ([[dogfooding-blind-spot-adopter-shape]]). 체인: fresh import(위임 미설정 → graceful TODO)
    → local.conf 에 sonnet/haiku 기록 → pm-update 초회·수렴 두 실행 모두 그 값으로 렌더 →
    delegate_channel_guard 경고 0.
    """
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))
    dest = tmp_path / "adopter"
    _import_adopter(pm_import, dest, "claude")
    card = dest / ".claude" / "agents" / "developer.md"

    # ① 위임 미설정 fresh import — 활성 리터럴 대신 TODO 중화, 어댑터 토큰 leak 0.
    assert _card_model_line(card.read_text(encoding="utf-8")).startswith(
        '# model: "<model>"'), "위임 미설정 import 가 model 줄을 중화하지 않았다"
    leaked = [
        p for p in (dest / ".claude").rglob("*")
        if p.is_file() and "{{" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not leaked, f"import 산출물에 미해소 토큰 잔존: {leaked}"

    # ② 채택자가 위임 매핑을 기록한다(선언 = local.conf 단일 진실).
    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "delegate.enabled=true\n"
        + "delegate.developer.harness=claude\n"
        + "delegate.developer.model=sonnet\n"
        + "delegate.researcher.model=haiku\n",
        encoding="utf-8", newline="\n",
    )

    # ③ self-update 소스는 manifest 의 bare `.claude/agents @render` = <source-root>/.claude/agents.
    assert _card_model_line(
        (REPO / ".claude" / "agents" / "developer.md").read_text(encoding="utf-8")
    ) == 'model: "{{DELEGATE_MODEL_DEVELOPER}}"', (
        "self-update 소스인 루트 카드가 토큰-form 이 아니다 — templates 사본만 고치면 "
        "채택자 재동기가 리터럴을 되돌린다"
    )
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    for run in ("초회", "수렴"):
        assert pm_update.main(["--from", str(REPO)]) == 0, f"{run} pm-update rc≠0"
        assert _card_model_line(card.read_text(encoding="utf-8")) == 'model: "sonnet"', \
            f"{run} 실행 후 카드가 conf 해소값이 아니다"
        assert _card_model_line(
            (dest / ".claude" / "agents" / "researcher.md").read_text(encoding="utf-8")
        ) == 'model: "haiku"', f"{run} 실행에서 역할별 개별 해소가 깨졌다"
        assert _card_model_line(
            (dest / ".claude" / "agents" / "architect.md").read_text(encoding="utf-8")
        ).startswith('# model: "<model>"'), \
            f"{run}: 미설정 역할이 다른 역할의 값을 물려받았다"

    # ④ 렌더가 맞으면 native 카드 불일치 경고가 자연히 사라진다(가드 변경 없이).
    guard = dest / ".project_manager" / "tools" / "delegate_channel_guard.py"
    decided = subprocess.run(
        [sys.executable, str(guard), "decide", "--role", "developer", "--harness", "claude"],
        cwd=str(dest), capture_output=True, text=True,
        # 자식은 기계 판정을 UTF-8 로 낸다 — 코덱을 명시하지 않으면 부모가 콘솔 코덱(Windows
        # cp949)으로 디코드하다 리더 스레드가 죽고 stdout 이 None 이 된다(엔진 호출부 관례).
        encoding="utf-8", errors="replace",
    )
    assert decided.returncode == 0, f"guard rc={decided.returncode}\n{decided.stderr}"
    assert "불일치" not in decided.stdout + decided.stderr, \
        f"카드가 conf 와 같은데 불일치 경고:\n{decided.stdout}\n{decided.stderr}"
    assert json.loads(decided.stdout)["model"] == "sonnet"


def test_guest_agent_card_model_follows_delegate_conf_after_absorb(
        pm_update, tmp_path, monkeypatch, capsys):
    """codex host + claude guest(add-harness) — **흡수 한 번**이면 guest 카드가 conf 값이 된다.

    guest 절 렌더물을 계획에서 빼던 동안 이 형상의 카드는 add-harness 를 다시 돌리기 전엔 설치
    시점 값으로 남았다(채택자 실측: 카드 opus ↔ conf sonnet 상시 경고). host 형상만 보는 e2e 로는
    안 잡히는 사각이라 guest 형상으로 직접 태운다: dry-run `[render]` 예고 → apply 값 일치 →
    재실행 변경 0(멱등).
    """
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    dest = tmp_path / "guest-adopter"
    _import_adopter(pm_import, dest, "codex")
    pm_import.add_harness(dest, "claude", dry_run=False, source_root=REPO)
    capsys.readouterr()
    card = dest / ".claude" / "agents" / "developer.md"
    assert card.is_file(), "claude-as-guest 카드 미복사(픽스처 전제 붕괴)"

    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "delegate.enabled=true\n"
        + "delegate.developer.harness=claude\n"
        + "delegate.developer.model=sonnet\n",
        encoding="utf-8", newline="\n",
    )
    _pin_card_model(card, "opus")  # 옛 값(손편집) — 다음 update 가 되돌려야 한다.

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(REPO), "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "[render] .claude/agents/developer.md" in preview, \
        f"dry-run 이 guest 카드 재렌더를 예고하지 않았다: {preview!r}"
    assert _card_model_line(card.read_text(encoding="utf-8")) == 'model: "opus"', \
        "dry-run 이 파일을 바꿨다(무변경 계약 위반)"

    assert pm_update.main(["--from", str(REPO)]) == 0
    assert _card_model_line(card.read_text(encoding="utf-8")) == 'model: "sonnet"', \
        "흡수 후에도 guest 카드가 conf 값이 아니다(add-harness 재실행을 요구하는 옛 형상)"

    settled = card.read_bytes()
    assert pm_update.main(["--from", str(REPO)]) == 0
    assert card.read_bytes() == settled, "재렌더가 멱등하지 않다(매 흡수 churn)"


def test_unused_profile_card_is_neutralized_not_filled(pm_update, tmp_path, monkeypatch, capsys):
    """conf 가 다른 하네스를 가리키는 카드는 **값 대신 사유**로 중화된다(미사용 프로필).

    codex host 인데 developer 를 claude 로 위임하는 형상: `.codex/agents/developer-hard.toml` 은
    이번 형상에서 스폰되지 않으므로 claude 용 모델을 박으면 카드가 conf 와 어긋난 사실을 감춘다.
    렌더는 실패하지 않는다 — 미해소가 update rc 를 바꾸지 않는다."""
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    dest = tmp_path / "unused-profile"
    _import_adopter(pm_import, dest, "codex")
    capsys.readouterr()

    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "delegate.developer.hard.harness=claude\n"
        + "delegate.developer.hard.model=opus\n"
        + "delegate.developer.hard.reasoning=high\n",
        encoding="utf-8", newline="\n",
    )
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")

    assert pm_update.main(["--from", str(REPO)]) == 0, "미사용 프로필이 update rc 를 바꿨다"

    toml = (dest / ".codex" / "agents" / "developer-hard.toml").read_text(encoding="utf-8")
    model_line = next(ln for ln in toml.splitlines() if ln.lstrip("# ").startswith("model ="))
    assert model_line.startswith('# model = "<model>"'), \
        f"미사용 프로필인데 다른 하네스 모델이 박혔다: {model_line!r}"
    assert "claude" in model_line, "중화 사유(conf 의 그 역할 하네스)가 안 실렸다"
    assert "opus" not in toml, "conf 의 claude 용 모델이 codex 카드에 새어 들어갔다"
    assert "{{" not in toml, "중화 산출물에 리터럴 토큰 잔존(자족 위반)"
