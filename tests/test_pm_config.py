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
import hashlib
import importlib.util
import json
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


# ── identity_args 공용 모듈 채택 (T-0317 · ADR-0057 §Consequences B-1) ──────────
#
# pm_config 로컬 `_leased_sessions`(구 pm_config.py:138) 를 제거하고 공용 `identity_args.
# leased_sessions` 로 위임한다. sibling 모듈이 스크립트 직접 실행 + 테스트(spec_from_file_
# location) 양쪽에서 동일하게 로드됨을 확인한다.


def test_pm_config_local_leased_sessions_removed(pc):
    """pm_config 로컬 `_leased_sessions` 사본이 제거됐다 — 공용 모듈로 완전 흡수(ADR-0057 B-1)."""
    assert not hasattr(pc, "_leased_sessions")


def test_pm_config_loads_identity_args_sibling_module(pc):
    """sibling 모듈 `identity_args` 를 `_load_module` 로 로드 가능 — board.py/worktree_pool.py
    와 동형 패턴(spec_from_file_location)이라 스크립트 직접 실행·테스트 양쪽에서 동작한다
    (정적 top-level import 는 ADR-0013 격리 관성상 쓰지 않음)."""
    mod = pc._load_module("identity_args", "identity_args.py")
    assert mod is not None
    assert hasattr(mod, "leased_sessions")
    assert hasattr(mod, "parse_identity")
    assert hasattr(mod, "add_identity_args")


def test_default_session_delegates_to_identity_args_leased_sessions(pc, tmp_path, monkeypatch):
    """`_default_session` 이 리스 장부 읽기를 주입된 `identity_args` 대역의 `leased_sessions`
    로 위임한다 — 배선(호출 인자·반환값 사용) 단언. 장부 경로는 `REPO`(monkeypatch) 기준으로
    호출 시점 구성됨도 함께 확인한다.
    """
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    calls = []

    class FakeIdentity:
        @staticmethod
        def leased_sessions(leases_file):
            calls.append(leases_file)
            return ["svc_1"]

    assert pc._default_session(identity=FakeIdentity()) == "svc_1"
    assert calls == [tmp_path / ".project_manager" / ".local" / "worktree-leases.json"]


def test_default_session_identity_leased_zero_falls_back_to_local_conf(pc, tmp_path, monkeypatch):
    """주입 대역이 leased 0개를 돌려주면(장부 부재/solo) local.conf `session=` 폴백 체인은
    여전히 동작한다 — identity_args 로 위임한 뒤에도 `_default_session` 나머지 우선순위(ADR-0040
    D1)는 불변임을 확인한다.
    """
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    pm_dir = tmp_path / ".project_manager"
    pm_dir.mkdir(parents=True, exist_ok=True)
    (pm_dir / "local.conf").write_text("session=from-conf\n", encoding="utf-8")

    class FakeIdentityEmpty:
        @staticmethod
        def leased_sessions(leases_file):
            return []

    assert pc._default_session(identity=FakeIdentityEmpty()) == "from-conf"


def test_default_session_no_identity_injected_uses_real_module(pc, tmp_path, monkeypatch):
    """`identity=` 미주입 시 실 `identity_args` 모듈을 로드해 동작한다(순수 모듈·파일 IO 0 —
    스크립트 직접 실행과 동일 경로)."""
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # 장부·local.conf 둘 다 부재 → 미해소(None) — 크래시 없이 실 모듈 경로가 동작함을 확인.
    assert pc._default_session() is None


# ── sync-adapter-config --check 수렴 게이트 (T-0591) ─────────────────────────

_CHECK_HOOKS_REL = ".codex/hooks.json"
_CHECK_REPORT_REL = ".codex/config.toml"
_CHECK_UPSTREAM_HOOKS = '{"hooks": {"PreCompact": ["new"]}}\n'
_CHECK_OLD_HOOKS = '{"hooks": {"PreCompact": ["old"]}}\n'
_CHECK_EDITED_HOOKS = '{"hooks": {"PreCompact": ["edited"]}}\n'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_check_case(tmp_path: Path, *, hooks: str, ledger_sha: str | None):
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex"
    template.mkdir(parents=True)
    (template / "hooks.json").write_text(_CHECK_UPSTREAM_HOOKS, encoding="utf-8")
    (template / "config.toml").write_text("upstream = true\n", encoding="utf-8")

    dest = tmp_path / "dest"
    (dest / ".codex").mkdir(parents=True)
    (dest / ".agents").mkdir()
    (dest / ".project_manager").mkdir()
    (dest / "AGENTS.md").write_text("# adopter\n", encoding="utf-8")
    (dest / _CHECK_HOOKS_REL).write_text(hooks, encoding="utf-8")
    # report-only 차이는 모든 case에 둔다. 이 차이는 출력되되 rc를 올리면 안 된다.
    (dest / _CHECK_REPORT_REL).write_text("adopter_knob = true\n", encoding="utf-8")
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["codex"]}\n', encoding="utf-8")
    if ledger_sha is not None:
        document = {
            "schema": 1,
            "files": {
                _CHECK_HOOKS_REL: {
                    "sha256": ledger_sha,
                    "recorded_at": "2026-01-01T00:00:00+09:00",
                    "template_rev": "old",
                }
            },
        }
        (dest / ".project_manager" / "adapter_baseline.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return dest, source


@pytest.mark.parametrize(
    ("name", "hooks", "ledger_sha", "expected_rc", "remedy"),
    [
        ("converged", _CHECK_UPSTREAM_HOOKS, _sha(_CHECK_UPSTREAM_HOOKS), 0, None),
        ("unrecorded", _CHECK_OLD_HOOKS, None, 1, "--accept .codex/hooks.json"),
        ("unedited", _CHECK_OLD_HOOKS, _sha(_CHECK_OLD_HOOKS), 1, "pm-update"),
        ("edited", _CHECK_EDITED_HOOKS, _sha(_CHECK_OLD_HOOKS), 1,
         "--accept .codex/hooks.json"),
        # byte가 같아도 durable 원장 증거가 없으면 false-green이다.
        ("in-sync-unrecorded", _CHECK_UPSTREAM_HOOKS, None, 1,
         "backfill"),
    ],
)
def test_sync_adapter_config_check_exit_and_actionable_remedy(
        pc, pim, tmp_path, capsys, name, hooks, ledger_sha, expected_rc, remedy):
    dest, source = _make_check_case(tmp_path / name, hooks=hooks, ledger_sha=ledger_sha)
    ledger = dest / ".project_manager" / "adapter_baseline.json"
    before = ledger.read_bytes() if ledger.is_file() else None
    args = argparse.Namespace(
        list=False, check=True, accept=None, source=str(source))

    rc = pc.cmd_sync_adapter_config(args, pm_import=pim, dest_root=dest)

    captured = capsys.readouterr()
    assert rc == expected_rc
    assert _CHECK_REPORT_REL in captured.out, "report-only 차이가 판정 출력에서 사라짐"
    if remedy is None:
        assert "수렴 확인" in captured.out
    else:
        assert remedy in captured.err, (name, captured.err)
        assert "미수렴" in captured.err
    after = ledger.read_bytes() if ledger.is_file() else None
    assert after == before, "--check가 원장을 썼다(write 0 계약 위반)"
    assert (dest / _CHECK_HOOKS_REL).read_text(encoding="utf-8") == hooks


def test_sync_adapter_config_check_in_sync_unrecorded_prescribes_real_pm_update(
        pc, pim, tmp_path, capsys):
    """byte 동일+원장 부재는 accept overwrite가 아니라 실 pm-update의 무변경 backfill 대상이다."""
    dest, source = _make_check_case(
        tmp_path, hooks=_CHECK_UPSTREAM_HOOKS, ledger_sha=None)
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(args, pm_import=pim, dest_root=dest) == 1

    err = capsys.readouterr().err
    assert "pm-update" in err and "backfill" in err
    assert "--accept .codex/hooks.json" not in err


def test_sync_adapter_config_check_report_only_drift_is_nonblocking(
        pc, pim, tmp_path, capsys):
    dest, source = _make_check_case(
        tmp_path, hooks=_CHECK_UPSTREAM_HOOKS, ledger_sha=_sha(_CHECK_UPSTREAM_HOOKS))
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(
        args, pm_import=pim, dest_root=dest) == 0

    out = capsys.readouterr().out
    assert _CHECK_REPORT_REL in out and "report" in out


def test_sync_adapter_config_check_unavailable_is_rc1(pc, tmp_path, capsys):
    class Unavailable:
        ADAPTER_CONFIG_REPORT = "report"

        @staticmethod
        def resolve_adapter_config_source(_dest, explicit):
            return Path(explicit)

        @staticmethod
        def judge_adapter_configs(_dest, _source):
            raise OSError("ledger read failed")

    args = argparse.Namespace(
        list=False, check=True, accept=None, source=str(tmp_path / "source"))

    assert pc.cmd_sync_adapter_config(
        args, pm_import=Unavailable(), dest_root=tmp_path / "dest") == 1
    err = capsys.readouterr().err
    assert "unavailable" in err and "ledger read failed" in err


def test_sync_adapter_config_check_managed_dest_missing_source_template_is_rc1(
        pc, pim, tmp_path, capsys):
    """managed dest가 있는데 source template이 없으면 빈 판정 rc0로 접지 않는다."""
    dest, source = _make_check_case(
        tmp_path, hooks=_CHECK_UPSTREAM_HOOKS, ledger_sha=_sha(_CHECK_UPSTREAM_HOOKS))
    (source / "templates" / "codex" / _CHECK_HOOKS_REL).unlink()
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(
        args, pm_import=pim, dest_root=dest) == 1

    err = capsys.readouterr().err
    assert "unavailable" in err and _CHECK_HOOKS_REL in err
    assert "--from <framework checkout>" in err, "source 복구 처방이 actionable하지 않음"


def test_sync_adapter_config_check_report_only_only_missing_managed_template_is_green(
        pc, pim, tmp_path, capsys):
    """managed dest 후보가 아예 없고 report-only만 있으면 missing managed template은 비차단."""
    dest, source = _make_check_case(
        tmp_path, hooks=_CHECK_UPSTREAM_HOOKS, ledger_sha=_sha(_CHECK_UPSTREAM_HOOKS))
    (dest / _CHECK_HOOKS_REL).unlink()
    (source / "templates" / "codex" / _CHECK_HOOKS_REL).unlink()
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(
        args, pm_import=pim, dest_root=dest) == 0
    out = capsys.readouterr().out
    assert _CHECK_REPORT_REL in out and "수렴 확인" in out


def test_sync_adapter_config_check_partial_managed_dest_missing_template_is_rc1(
        pc, pim, tmp_path, capsys):
    """영수증/완전 shape 없는 managed dest도 빈 판정 rc0로 접지 않는다."""
    source = tmp_path / "source"
    source.mkdir()
    dest = tmp_path / "partial-dest"
    hooks = dest / _CHECK_HOOKS_REL
    hooks.parent.mkdir(parents=True)
    hooks.write_text(_CHECK_OLD_HOOKS, encoding="utf-8")
    assert pim.installed_harnesses(dest, source) == []
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(args, pm_import=pim, dest_root=dest) == 1

    err = capsys.readouterr().err
    assert "unavailable" in err and _CHECK_HOOKS_REL in err


def test_sync_adapter_config_check_partial_managed_dest_unrecorded_is_rc1(
        pc, pim, tmp_path, capsys):
    """partial managed 파일은 source가 정상이어도 durable baseline 없이는 green이 아니다."""
    source = tmp_path / "source"
    template = source / "templates" / "codex" / _CHECK_HOOKS_REL
    template.parent.mkdir(parents=True)
    template.write_text(_CHECK_UPSTREAM_HOOKS, encoding="utf-8")
    dest = tmp_path / "partial-dest"
    hooks = dest / _CHECK_HOOKS_REL
    hooks.parent.mkdir(parents=True)
    hooks.write_text(_CHECK_OLD_HOOKS, encoding="utf-8")
    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))

    assert pc.cmd_sync_adapter_config(args, pm_import=pim, dest_root=dest) == 1

    err = capsys.readouterr().err
    assert "unrecorded" in err and "--accept .codex/hooks.json" in err


def test_sync_adapter_config_parser_check_is_exclusive(pc):
    parser = pc.build_parser()
    checked = parser.parse_args(
        ["sync-adapter-config", "--check", "--from", "/tmp/framework"])
    assert checked.func is pc.cmd_sync_adapter_config
    assert checked.check is True and checked.source == "/tmp/framework"
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--check", "--list"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["sync-adapter-config", "--check", "--accept", ".codex/hooks.json"])
