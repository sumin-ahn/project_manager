"""pm-config `add-harness` 위임 서브커맨드 테스트 (T-0270 · ADR-0048).

`pm_config add-harness <harness> [--dry-run]` 가 pm_import 의 main-style CLI 진입
`add_harness_cli(dest, harness, dry_run=)` 로 **verbatim 위임**함을 검증한다 — 복사 스코프
(어댑터 네임스페이스)·비파괴 백업·**계약 예외의 친화 번역(rc 1)** 은 전부 pm_import 가 단일
진실로 소유하고(T-0269 add_harness + T-0270 add_harness_cli 래퍼), pm_config 는 얇은
디스패처(로직 0)다. 실 복사 부작용은 mock 주입으로 격리한다(test_pm_config_facade.py 의
DI seam 동류).

커버:
  - forward 배선 — cmd_add_harness → pm_import.add_harness_cli(dest, harness, dry_run=) verbatim.
  - dest 해소 — 미주입 시 pm_config 규약(REPO=인스턴스 루트)으로 폴백.
  - dispatch 라우팅 — `main(["add-harness", ...])` 가 이 핸들러로 간다.
  - usage prog 정합 — `_forwarded_prog` 위임-경계 가드 + 서브파서 usage=`pm-config add-harness`(파일명 leak 0).
  - 엔진 부재 격리 — pm_import 부재면 명시 에러 rc 1.
  - **계약 예외 경계(codex)** — add_harness_cli 가 ValueError/FileNotFoundError/FileVsDirConflict/
    AncestorConflict 를 친화 메시지 + rc 1 로 번역(traceback 0·pm_import.main 동형).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


@pytest.fixture(scope="module")
def pim():
    return _load_pm_import()


# ── 주입형 pm_import fake (DI seam — hermetic) ────────────────────────────────


class FakePmImport:
    """pm_import 모듈 대역 — add_harness_cli(dest, harness, dry_run=) 호출 인자를 기록한다.

    실 복사/plan_copy 없이 *pm_config 가 어떤 인자로 위임하는지*(배선)만 결정적으로 친다.
    on_call 훅으로 위임 *중* 동작(예: _forwarded_prog 활성 여부 관찰)을 주입할 수 있다.
    """

    def __init__(self, *, rc=0, on_call=None):
        self.calls: list[dict] = []
        self._rc = rc
        self._on_call = on_call

    def add_harness_cli(self, dest_root, harness, *, dry_run, source_root=None):
        self.calls.append(
            {"dest_root": dest_root, "harness": harness,
             "dry_run": dry_run, "source_root": source_root}
        )
        if self._on_call is not None:
            self._on_call()
        return self._rc


# ── forward 배선 — verbatim (cmd_add_harness → pm_import.add_harness_cli) ─────


def test_add_harness_forwards_to_pm_import(pc):
    """`add-harness opencode` → pm_import.add_harness_cli(dest, "opencode", dry_run=False) 그대로."""
    fake = FakePmImport(rc=0)
    args = argparse.Namespace(harness="opencode", dry_run=False)
    rc = pc.cmd_add_harness(args, pm_import=fake, dest_root=Path("/live/inst"))
    assert rc == 0
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["dest_root"] == Path("/live/inst")
    assert call["harness"] == "opencode"
    assert call["dry_run"] is False


def test_add_harness_dry_run_flag_forwarded(pc):
    """`--dry-run` 이 dry_run=True 로 위임된다 (플래그 verbatim 전달)."""
    fake = FakePmImport(rc=0)
    args = argparse.Namespace(harness="claude", dry_run=True)
    rc = pc.cmd_add_harness(args, pm_import=fake, dest_root=Path("/x"))
    assert rc == 0
    assert fake.calls[0]["harness"] == "claude"
    assert fake.calls[0]["dry_run"] is True


def test_add_harness_propagates_returncode(pc):
    """add_harness_cli 의 rc 가 그대로 전파된다 (위임·중복 로직 0)."""
    fake = FakePmImport(rc=1)
    args = argparse.Namespace(harness="opencode", dry_run=False)
    assert pc.cmd_add_harness(args, pm_import=fake, dest_root=Path("/x")) == 1


def test_add_harness_dest_defaults_to_repo(pc):
    """dest_root 미주입 시 pm_config 규약(REPO=인스턴스 루트)으로 해소해 위임한다."""
    fake = FakePmImport(rc=0)
    args = argparse.Namespace(harness="opencode", dry_run=False)
    rc = pc.cmd_add_harness(args, pm_import=fake)  # dest_root 미주입
    assert rc == 0
    assert fake.calls[0]["dest_root"] == pc.REPO


def test_add_harness_engine_missing_errors_isolated(pc, monkeypatch, capsys):
    """_load_module 가 None(pm_import 부재)이면 명시 에러 rc 1 (침묵 무력화 금지·ADR-0013)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    args = argparse.Namespace(harness="opencode", dry_run=False)
    rc = pc.cmd_add_harness(args)
    assert rc == 1
    assert "pm_import.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── dispatch 라우팅 — main(["add-harness", ...]) → cmd_add_harness ────────────


def test_add_harness_dispatch_routes_to_handler(pc, monkeypatch):
    """`main(["add-harness", "opencode", "--dry-run"])` 가 func-dispatch 로 핸들러에 도달한다."""
    fake = FakePmImport(rc=0)
    monkeypatch.setattr(
        pc, "_load_module",
        lambda name, filename: fake if name == "pm_import" else None,
    )
    rc = pc.main(["add-harness", "opencode", "--dry-run"])
    assert rc == 0
    assert len(fake.calls) == 1
    assert fake.calls[0]["harness"] == "opencode"
    assert fake.calls[0]["dry_run"] is True
    # dest 미주입(dispatch 경로) → REPO 폴백.
    assert fake.calls[0]["dest_root"] == pc.REPO


# ── usage prog 정합 (T-0249·ADR-0043) — 위임-경계 가드 + 서브파서 usage ──────


def test_add_harness_forward_remaps_foreign_prog(pc):
    """위임 *중* `_forwarded_prog` 활성 — 경계 안에서 만든 `pm_import.py` prog 를
    `pm-config add-harness` 로 치환하고, 위임 종료 후 argparse 전역을 원복한다 (T-0249·경계 leak 0)."""
    observed: dict = {}

    def _surface_usage():
        # add_harness_cli 가 위임 중 어떤 argparse usage 를 surface 하는 상황 모사.
        observed["prog"] = argparse.ArgumentParser(prog="pm_import.py").prog

    fake = FakePmImport(rc=0, on_call=_surface_usage)
    args = argparse.Namespace(harness="opencode", dry_run=False)
    rc = pc.cmd_add_harness(args, pm_import=fake, dest_root=Path("/x"))
    assert rc == 0
    assert observed["prog"] == "pm-config add-harness"   # 위임 중 파일명 leak 0
    # 위임 종료 후 전역 argparse 원복(누수 0).
    assert argparse.ArgumentParser(prog="pm_import.py").prog == "pm_import.py"


def _usage_line(text: str) -> str:
    return next(l for l in text.splitlines() if l.startswith("usage:"))


def test_add_harness_help_usage_shows_facade_prog(pc, capsys):
    """`add-harness --help` usage 줄은 `pm-config add-harness` — 파일명 leak 0 (T-0249·ADR-0043)."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["add-harness", "--help"])
    assert exc.value.code == 0
    usage = _usage_line(capsys.readouterr().out)
    assert usage.startswith("usage: pm-config add-harness")   # facade+서브 표기(카드↔실행 정합)
    assert "pm_import.py" not in usage
    assert "pm_config.py" not in usage


def test_add_harness_error_usage_shows_facade_prog(pc, capsys):
    """`add-harness`(harness 누락) 인자 에러 usage 는 `pm-config add-harness`·파일명 leak 0."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["add-harness"])   # required positional 누락 → argparse 에러
    assert exc.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pm-config add-harness" in combined
    assert "pm_import.py" not in combined
    assert "pm_config.py" not in combined


def test_add_harness_surfaced_in_help(pc, capsys):
    """`add-harness` 가 최상위 `--help` 서브커맨드 목록에 노출된다(발견성)."""
    rc = pc.main([])
    assert rc == 1
    assert "add-harness" in capsys.readouterr().out


# ── 계약 예외 경계 (codex) — add_harness_cli 가 친화 메시지 + rc 1 로 번역 ─────
# add_harness 는 내부 plan_copy 가 FileVsDirConflict/AncestorConflict 를, 입구 검증이
# ValueError(미지원 harness)/FileNotFoundError(dest 부재)를 던진다. CLI 노출 시 이를 잡지
# 않으면 traceback 이 사용자에게 샌다 — main-style 래퍼 add_harness_cli 가 pm_import.main 과
# *동일하게* 잡아 `오류: …`(stderr) + rc 1 로 번역함을 검증한다(에러 경계=CLI contract owner=pm_import).


@pytest.mark.parametrize("make_exc, needle", [
    (lambda pim: ValueError("미지원 harness"), "미지원 harness"),
    (lambda pim: FileNotFoundError("dest 없음"), "dest 없음"),
    (lambda pim: pim.FileVsDirConflict("dst 에 디렉토리"), "dst 에 디렉토리"),
    (lambda pim: pim.AncestorConflict("조상 symlink"), "조상 symlink"),
])
def test_add_harness_cli_translates_contract_exc_to_rc1(
    pim, monkeypatch, capsys, make_exc, needle,
):
    """add_harness 가 던지는 계약 예외 4종 → add_harness_cli 가 rc 1 + 친화 메시지(traceback 0)."""
    exc = make_exc(pim)

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(pim, "add_harness", _raise)
    rc = pim.add_harness_cli(Path("/live/inst"), "opencode", dry_run=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert needle in err            # 예외 메시지가 친화적으로 surface
    assert "오류:" in err           # main 동형 접두
    assert "Traceback" not in err   # traceback 0


def test_add_harness_cli_success_returns_zero(pim, monkeypatch):
    """성공 시 add_harness 로 인자를 투명 전달하고 rc 0 (자체 출력 중복 0)."""
    called: dict = {}

    def _ok(dest_root, harness, *, dry_run, source_root=None):
        called["args"] = (dest_root, harness, dry_run, source_root)
        return ["fake-plan"]

    monkeypatch.setattr(pim, "add_harness", _ok)
    rc = pim.add_harness_cli(Path("/x"), "claude", dry_run=True, source_root=Path("/src"))
    assert rc == 0
    assert called["args"] == (Path("/x"), "claude", True, Path("/src"))


def test_add_harness_bad_harness_end_to_end_rc1(pc, pim, tmp_path, capsys):
    """전체 스택(cmd_add_harness → 실 add_harness_cli → add_harness): 미지원 harness 는
    복사 전 입구 검증에서 ValueError → rc 1·부작용 0(어댑터 파일 미생성·traceback 0)."""
    args = argparse.Namespace(harness="both", dry_run=True)   # 'both' 는 최초 import 소관·add-harness 미지원
    rc = pc.cmd_add_harness(args, pm_import=pim, dest_root=tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert list(tmp_path.iterdir()) == []   # 부작용 0


def test_add_harness_missing_dest_end_to_end_rc1(pc, pim, tmp_path, capsys):
    """존재하지 않는 dest → 실 add_harness_cli 가 FileNotFoundError 를 rc 1 로 번역(traceback 0)."""
    missing = tmp_path / "no-such-instance"
    args = argparse.Namespace(harness="opencode", dry_run=True)
    rc = pc.cmd_add_harness(args, pm_import=pim, dest_root=missing)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "디렉토리가 아니다" in err
