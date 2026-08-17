"""T-0710 — 어댑터 config 내용 동일성 판정의 개행 축.

Windows 채택자 형상(`core.autocrlf=true` 체크아웃)을 Linux 에서 **bytes 로** 재현한다: 엔진이 깐
LF 내용이 워킹트리에서 CRLF 로 바뀌어 있고 원장에는 깔 때의 LF 내용 해시가 남아 있다. 내용은
같으므로 판정은 `converged`(`--check` rc 0)여야 하고, 그 판정 때문에 **파일 bytes 가 바뀌어서도**
안 된다(판정은 정규화, 쓰기는 verbatim — 두 축이 갈린다).

픽스처가 개행을 `write_bytes` 로 직접 심는 게 이 절의 핵심이다. `write_text` 로 쓰면 Linux 에선
CRLF 가 생기지 않아 이 클래스가 플랫폼 밖에서 관측되지 않는다([[guard-must-cover-its-own-surface]]).
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

_HOOKS_REL = ".codex/hooks.json"
_REPORT_REL = ".codex/config.toml"
_UPSTREAM_HOOKS = '{"hooks": {"PreCompact": ["상류 비차단 안내"]}}\n'
_INSTALLED_HOOKS = '{"hooks": {"PreCompact": ["설치 시점 차단판"]}}\n'
_EDITED_HOOKS = '{"hooks": {"PreCompact": ["채택자 손편집"]}}\n'
# UTF-8 로 디코딩되지 않는 payload — 여기서 `\r\n` 은 개행이 아니라 데이터다(정규화 금지 대상).
_BINARY_HOOKS_LF = b"\xff\xfe\x00binary\n\x00payload\n"
_BINARY_HOOKS_CRLF = b"\xff\xfe\x00binary\r\n\x00payload\r\n"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pm_import():
    return _load("pm_import")


@pytest.fixture()
def pm_config():
    return _load("pm_config")


@pytest.fixture()
def pm_update():
    return _load("pm_update")


def _crlf(text: str) -> bytes:
    """체크아웃 변환이 만든 워킹트리 bytes (엔진이 깐 LF 내용과 **같은 내용**)."""
    return text.replace("\n", "\r\n").encode("utf-8")


def _sha(payload) -> str:
    return hashlib.sha256(
        payload if isinstance(payload, bytes) else payload.encode("utf-8")).hexdigest()


def _ledger_entry(sha256: str) -> dict:
    """원장 항목 — "그 내용으로 레이다운했다" 는 기록(해시만 판정에 쓰인다)."""
    return {
        "sha256": sha256,
        "recorded_at": "2026-01-01T00:00:00+09:00",
        "template_rev": "deadbeef",
    }


def _make_case(tmp_path: Path, *, dest_hooks: bytes,
               template_hooks: bytes = _UPSTREAM_HOOKS.encode("utf-8"),
               ledger_sha: str | None = None,
               plant_upstream_engine: bool = False) -> tuple[Path, Path]:
    """codex 채택자 + 합성 프레임워크 — (dest, source_root).

    hooks 는 managed 채널, config.toml 은 report-only 채널이다. 후자에 채택자 노브를 남겨
    report 차이가 rc 를 올리지 않는 경로도 함께 태운다.

    `plant_upstream_engine` 은 상류 트리에 pm_import 사본을 놓는다 — 훅 세트 게이트가 **상류
    세대 선언**을 읽는 전제라, `--accept` 계열을 태우는 절만 필요하다(부재면 fail-closed)."""
    source = tmp_path / "framework"
    template = source / "templates" / "codex" / ".codex"
    template.mkdir(parents=True)
    (template / "hooks.json").write_bytes(template_hooks)
    (template / "config.toml").write_text("upstream = true\n", encoding="utf-8")
    if plant_upstream_engine:
        upstream_engine = source / ".project_manager" / "tools" / "pm_import.py"
        upstream_engine.parent.mkdir(parents=True)
        upstream_engine.write_bytes((TOOLS / "pm_import.py").read_bytes())

    dest = tmp_path / "adopter"
    (dest / ".codex").mkdir(parents=True)
    (dest / ".agents").mkdir()
    (dest / ".project_manager").mkdir()
    (dest / "AGENTS.md").write_text("# adopter 진입 문서\n", encoding="utf-8")
    (dest / _HOOKS_REL).write_bytes(dest_hooks)
    (dest / _REPORT_REL).write_text("adopter_knob = true\n", encoding="utf-8")
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["codex"]}\n', encoding="utf-8")
    if ledger_sha is not None:
        document = {"schema": 1, "files": {_HOOKS_REL: _ledger_entry(ledger_sha)}}
        (dest / ".project_manager" / "adapter_baseline.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return dest, source


def _read_ledger(dest: Path) -> dict:
    path = dest / ".project_manager" / "adapter_baseline.json"
    return json.loads(path.read_text(encoding="utf-8"))["files"] if path.is_file() else {}


def _judgment(pm_import, dest: Path, source: Path, relpath: str = _HOOKS_REL):
    return next(item for item in pm_import.judge_adapter_configs(dest, source)
                if item.relpath == relpath)


# ── content_sha256 자체 (판정 다이제스트) ─────────────────────────────────────

def test_content_sha256_folds_newline_notation(pm_import, tmp_path):
    """개행 표기만 다른 두 파일은 같은 다이제스트 — 체크아웃 변환은 내용 변경이 아니다."""
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(_UPSTREAM_HOOKS.encode("utf-8"))
    crlf.write_bytes(_crlf(_UPSTREAM_HOOKS))

    assert lf.read_bytes() != crlf.read_bytes(), "픽스처 전제(bytes 는 달라야 한다)"
    assert pm_import.content_sha256(lf) == pm_import.content_sha256(crlf)


def test_content_sha256_matches_raw_digest_for_lf_content(pm_import, tmp_path):
    """LF 파일에서는 값이 `file_sha256` 과 같다 — 구 원장 항목이 그대로 유효한 근거."""
    path = tmp_path / "lf.json"
    path.write_bytes(_UPSTREAM_HOOKS.encode("utf-8"))

    assert pm_import.content_sha256(path) == pm_import.file_sha256(path)
    assert pm_import.content_sha256(path) == _sha(_UPSTREAM_HOOKS)


def test_content_sha256_accepts_bytes_payload(pm_import, tmp_path):
    """설치할 payload 를 그대로 넘길 수 있다(파일을 다시 읽지 않고 원장 값을 만든다)."""
    path = tmp_path / "crlf.json"
    path.write_bytes(_crlf(_UPSTREAM_HOOKS))

    assert pm_import.content_sha256(_crlf(_UPSTREAM_HOOKS)) == _sha(_UPSTREAM_HOOKS)
    assert pm_import.content_sha256(path) == pm_import.content_sha256(
        _UPSTREAM_HOOKS.encode("utf-8"))


def test_content_sha256_keeps_raw_bytes_for_undecodable_payload(pm_import, tmp_path):
    """UTF-8 로 못 읽는 config 는 raw bytes 판정 — `\\r\\n` 이 개행이라는 보장이 없다."""
    lf = tmp_path / "lf.bin"
    crlf = tmp_path / "crlf.bin"
    lf.write_bytes(_BINARY_HOOKS_LF)
    crlf.write_bytes(_BINARY_HOOKS_CRLF)

    assert pm_import.content_sha256(lf) == pm_import.file_sha256(lf) == _sha(
        _BINARY_HOOKS_LF)
    assert pm_import.content_sha256(lf) != pm_import.content_sha256(crlf), \
        "바이너리 payload 가 개행 정규화로 접혔다"


def test_content_sha256_unreadable_path_is_none(pm_import, tmp_path):
    """읽을 수 없으면 None — `file_sha256` 과 같은 fail-soft(판정 불가를 0 으로 접지 않는다)."""
    assert pm_import.content_sha256(tmp_path / "없는파일.json") is None
    assert pm_import.content_sha256(tmp_path) is None  # 디렉토리


# ── CRLF 체크아웃 채택자의 판정 (T-0710 회귀 본류) ────────────────────────────

def test_crlf_checkout_config_is_converged(pm_import, pm_config, tmp_path, capsys):
    """CRLF 로 깔린 managed config 는 `in-sync`·`converged` 이고 `--check` rc 0.

    수정 전에는 raw bytes 판정이라 `edited`(원장 해시 불일치) → rc 1 이었다. Windows 채택자가
    자동 갱신 궤도에서 이탈하던 지점이다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_UPSTREAM_HOOKS), ledger_sha=_sha(_UPSTREAM_HOOKS))
    before = (dest / _HOOKS_REL).read_bytes()

    judgment = _judgment(pm_import, dest, source)

    assert judgment.status == "in-sync", judgment
    assert pm_import.adapter_config_convergence_status(judgment) == "converged"

    args = argparse.Namespace(list=False, check=True, accept=None, source=str(source))
    rc = pm_config.cmd_sync_adapter_config(args, pm_import=pm_import, dest_root=dest)

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "수렴 확인" in captured.out
    assert (dest / _HOOKS_REL).read_bytes() == before, "--check 가 개행을 뒤집었다"


def test_crlf_checkout_sync_leaves_dest_bytes_verbatim(pm_update, tmp_path):
    """수렴 판정이 난 CRLF 파일은 실 동기(write=True)도 건드리지 않는다 — byte churn 0.

    정규화가 판정 축에만 걸렸는지 확인하는 자리다. 여기서 파일이 LF 로 뒤집히면 Windows 채택자
    트리 전체가 무관한 diff 로 오염된다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_UPSTREAM_HOOKS), ledger_sha=_sha(_UPSTREAM_HOOKS))
    before = (dest / _HOOKS_REL).read_bytes()

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert result["status"] == "ok" and result["managed_converged"], result
    assert result["updated"] == [] and result["blocking"] == [], result
    assert [item["relpath"] for item in result["preserved"]] == [], result
    assert (dest / _HOOKS_REL).read_bytes() == before
    assert not (dest / ".pm_import_backups").exists(), \
        "바꿀 게 없는데 백업이 생겼다(무동작 경로가 아님)"


def test_legacy_raw_ledger_entry_is_backfilled_without_touching_dest(
        pm_import, pm_update, tmp_path):
    """구 원장(raw bytes 다이제스트)은 무변경 backfill 로 재기록된다 — 채택자 파일 불변.

    CRLF template 을 가진 상류 체크아웃에서 설치한 채택자의 원장이 이 형상이다(기록된 값이
    CRLF bytes 해시). 정규화 판정으로 바뀐 뒤에도 파일을 덮지 않고 원장 값만 새 축으로 옮긴다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_UPSTREAM_HOOKS),
        ledger_sha=_sha(_crlf(_UPSTREAM_HOOKS)))
    before = (dest / _HOOKS_REL).read_bytes()

    judgment = _judgment(pm_import, dest, source)
    assert judgment.status == "in-sync", judgment
    assert pm_import.adapter_config_convergence_status(judgment) == "unrecorded", \
        "구 축 원장은 backfill 대상이어야 한다"

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert result["backfilled"] == [_HOOKS_REL], result
    assert result["managed_converged"], result
    assert result["updated"] == [], "무변경 backfill 인데 갱신으로 보고됐다"
    assert _read_ledger(dest)[_HOOKS_REL]["sha256"] == _sha(_UPSTREAM_HOOKS)
    assert (dest / _HOOKS_REL).read_bytes() == before, "backfill 이 파일을 건드렸다"
    assert not (dest / ".pm_import_backups").exists()


def test_crlf_checkout_unedited_is_accepted_not_raced(pm_import, pm_update, tmp_path):
    """CRLF 로 깔린 무편집분은 자동 갱신된다 — 판정↔쓰기 해시 축이 같아 `raced` 가 아니다.

    수정 전에는 판정이 정규화 해시(원장 일치)를 내고 수용이 raw 해시로 재검증해 축이 어긋났다
    (`status='raced'`). 갱신 결과 bytes 는 template verbatim 이고 백업엔 덮기 전 CRLF 가 남는다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_INSTALLED_HOOKS), ledger_sha=_sha(_INSTALLED_HOOKS))
    before = (dest / _HOOKS_REL).read_bytes()

    judgment = _judgment(pm_import, dest, source)
    assert judgment.status == "unedited", judgment

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert [item["relpath"] for item in result["updated"]] == [_HOOKS_REL], result
    assert result["managed_converged"], result
    assert (dest / _HOOKS_REL).read_bytes() == _UPSTREAM_HOOKS.encode("utf-8"), \
        "쓰기 축이 verbatim 이 아니다(template bytes 그대로여야 한다)"
    backup = dest / result["updated"][0]["backup_rel"]
    assert backup.read_bytes() == before, "덮기 전 CRLF bytes 가 백업되지 않음"
    assert _read_ledger(dest)[_HOOKS_REL]["sha256"] == _sha(_UPSTREAM_HOOKS)


def test_crlf_set_accept_does_not_exclude_unedited_as_adopter_edit(
        pm_import, pm_config, tmp_path, capsys):
    """세트 수용이 CRLF 무편집분을 "채택자 편집분" 으로 제외하지 않는다.

    제외 규칙은 `edited`/`unrecorded` 판정에 걸려 있으므로, 개행 오판이 그대로 세트 수용 배제로
    번역됐다(구세대 config 를 받을 채널이 채택자에게서 사라진다). report-only 대상은 원장이
    없어 계속 제외되므로(rc 1) 여기서 보는 건 **managed 대상이 대상 목록에 들어왔는가** 다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_INSTALLED_HOOKS), ledger_sha=_sha(_INSTALLED_HOOKS),
        plant_upstream_engine=True)
    args = argparse.Namespace(
        list=False, check=False, accept=None, accept_all=True, source=str(source))

    pm_config.cmd_sync_adapter_config(args, pm_import=pm_import, dest_root=dest)

    out = capsys.readouterr().out
    excluded = [line for line in out.splitlines() if "세트 수용 제외" in line]
    assert not [line for line in excluded if _HOOKS_REL in line], excluded
    assert "1/1 수용" in out, out
    assert (dest / _HOOKS_REL).read_bytes() == _UPSTREAM_HOOKS.encode("utf-8")
    assert _read_ledger(dest)[_HOOKS_REL]["sha256"] == _sha(_UPSTREAM_HOOKS)


# ── 정규화가 삼키면 안 되는 것 ────────────────────────────────────────────────

def test_adopter_edit_on_crlf_checkout_is_still_preserved(
        pm_import, pm_update, tmp_path):
    """개행 말고 **내용**이 다르면 CRLF 트리에서도 `edited` 로 보존된다(과잉 정규화 가드)."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_crlf(_EDITED_HOOKS), ledger_sha=_sha(_INSTALLED_HOOKS))
    before = (dest / _HOOKS_REL).read_bytes()

    judgment = _judgment(pm_import, dest, source)
    assert judgment.status == "edited", judgment

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert [item["relpath"] for item in result["preserved"]] == [_HOOKS_REL], result
    assert (dest / _HOOKS_REL).read_bytes() == before, "채택자 편집분을 덮었다"


def test_undecodable_config_keeps_raw_bytes_judgment(pm_import, pm_update, tmp_path):
    """바이너리 config 는 개행 정규화 대상이 아니다 — `\\r\\n` 차이가 그대로 내용 차이다."""
    dest, source = _make_case(
        tmp_path, dest_hooks=_BINARY_HOOKS_CRLF, template_hooks=_BINARY_HOOKS_LF,
        ledger_sha=_sha(_BINARY_HOOKS_CRLF))
    before = (dest / _HOOKS_REL).read_bytes()

    judgment = _judgment(pm_import, dest, source)

    assert judgment.status == "unedited", judgment  # 원장(raw)과는 같고 template 과는 다르다
    assert judgment.dest_sha256 == _sha(_BINARY_HOOKS_CRLF)

    result = pm_update.sync_adapter_configs(dest, source, write=True)

    assert [item["relpath"] for item in result["updated"]] == [_HOOKS_REL], result
    assert (dest / _HOOKS_REL).read_bytes() == _BINARY_HOOKS_LF
    assert _read_ledger(dest)[_HOOKS_REL]["sha256"] == _sha(_BINARY_HOOKS_LF)
    assert before != _BINARY_HOOKS_LF, "픽스처 전제(교체 전후 bytes 가 달라야 한다)"
