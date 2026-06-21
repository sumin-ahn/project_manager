"""pm_bootstrap 시간대 폴백 + subprocess 인코딩 단위 테스트 (T-0018).

두 가지를 deterministic 하게 본다.
 (C4) zoneinfo 조회 실패(tzdata 부재 Windows / zoneinfo 부재 3.8-) 를 강제 시뮬레이션
       하고, 모듈을 재로드해 KST 가 고정 UTC+9 오프셋으로 폴백하는지 — now(tz=KST) 의
       타임스탬프가 +09:00 인지 검증한다. (이 머신은 tzdata 가 설치돼 있으므로 실제
       uninstall 대신 zoneinfo.ZoneInfo 를 ZoneInfoNotFoundError 로 throw 하게 monkeypatch.)
 (C3) board.py 를 캡처하는 subprocess.run 이 encoding="utf-8", errors="replace" 를 넘기는지
       — 모듈의 subprocess 를 fake 로 갈아 호출 kwargs 를 검증하고, 한글/em-dash 출력을
       round-trip 시켜 cp949 디코딩 크래시가 나지 않음을 확인한다.

도구는 패키지가 아니므로 importlib 로 경로 로드(test_portability 와 동일).
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOOTSTRAP_PY = TOOLS / "pm_bootstrap.py"


def _load_module(name: str = "pm_bootstrap"):
    """pm_bootstrap 를 fresh import 한다 (모듈-레벨 try/except 폴백을 재평가하기 위해)."""
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── C4: KST 시간대 폴백 ────────────────────────────────────────────────────

def test_kst_falls_back_to_fixed_offset_when_zoneinfo_lookup_fails(monkeypatch):
    """zoneinfo 조회가 ZoneInfoNotFoundError 로 깨지면 KST 가 고정 UTC+9 폴백이어야 한다.

    tzdata 부재 Windows / zoneinfo 부재 3.8- 의 실패 분기를 시뮬레이션한다.
    monkeypatch 로 zoneinfo.ZoneInfo 를 호출 시 ZoneInfoNotFoundError 를 던지게 한 뒤
    모듈을 fresh exec → 모듈-레벨 try/except 가 폴백 가지를 타도록 강제한다.
    """
    import zoneinfo

    def _boom(_key):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key Asia/Seoul")

    # 모듈이 import 하는 zoneinfo.ZoneInfo 자체를 ZoneInfoNotFoundError 로 throw 하게.
    monkeypatch.setattr(zoneinfo, "ZoneInfo", _boom)

    mod = _load_module()

    # 폴백은 datetime.timezone 의 고정 오프셋이어야 한다 (ZoneInfo 가 아님).
    assert isinstance(mod.KST, datetime.timezone)
    assert mod.KST.utcoffset(None) == datetime.timedelta(hours=9)

    # 실제 타임스탬프가 +09:00 으로 표기되는지 round-trip 검증.
    now = datetime.datetime.now(tz=mod.KST)
    assert now.utcoffset() == datetime.timedelta(hours=9)
    assert now.strftime("%z") == "+0900"
    assert now.isoformat().endswith("+09:00")


def test_kst_uses_zoneinfo_when_available():
    """정상 경로(시스템 tz DB/tzdata 존재)에서는 KST 가 **ZoneInfo** 여야 한다 (리눅스/맥 보존).

    UTC+9 만 단언하면 "try/except 가 항상 폴백을 타는" 회귀를 못 잡는다(폴백도 UTC+9 라
    통과). 그래서 zoneinfo 가 실제로 Asia/Seoul 을 만들 수 있는 환경에서는 KST 가
    datetime.timezone 폴백이 아니라 zoneinfo.ZoneInfo 인스턴스임을 단언해 정상 경로를 지킨다.
    한국은 1988 이후 서머타임 없음 → 어느 경로든 오프셋은 동일 UTC+9. (codex NIT 반영)
    """
    import zoneinfo

    try:
        zoneinfo.ZoneInfo("Asia/Seoul")
        zoneinfo_available = True
    except Exception:
        zoneinfo_available = False  # tzdata 부재 Windows 등 — 폴백이 정상

    mod = _load_module()
    now = datetime.datetime.now(tz=mod.KST)
    assert now.utcoffset() == datetime.timedelta(hours=9)
    if zoneinfo_available:
        # 정상 경로 보존 — 폴백으로 조용히 떨어지지 않았는지 가드.
        assert isinstance(mod.KST, zoneinfo.ZoneInfo)


# ── C3: subprocess 인코딩 ──────────────────────────────────────────────────

def test_run_board_passes_utf8_encoding(monkeypatch):
    """board.py 캡처 subprocess.run 이 encoding="utf-8", errors="replace" 를 넘겨야 한다.

    encoding 미지정이면 Windows 부모가 cp949 로 디코딩 → board.py 한글/이모지 출력에
    크래시. 폭로 테스트: kwargs 를 캡처해 명시 인코딩을 단언한다 (pre-fix 코드는 실패).
    """
    mod = _load_module()
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    bootstrap = mod.PmBootstrap()
    bootstrap._default_run_board(["list"])

    assert captured["kwargs"].get("encoding") == "utf-8"
    assert captured["kwargs"].get("errors") == "replace"


def test_run_board_round_trips_korean_and_emdash(monkeypatch):
    """한글 + em-dash(U+2014) + 이모지 출력이 cp949 디코딩 크래시 없이 round-trip 되어야 한다.

    fake subprocess.run 이 결정한 encoding/errors 로 자식 stdout(bytes)을 디코딩하도록
    재현 — encoding="utf-8" 이 박혀 있으면 비-ASCII 가 그대로 살아온다. (ambient
    PYTHONUTF8 에 의존하지 않고, fake 가 받은 encoding 으로 실제 디코딩해 폭로.)
    """
    mod = _load_module()
    payload = "보드 목록 — done 3 / open 2 ✅"
    raw = payload.encode("utf-8")

    def _fake_run(cmd, **kwargs):
        encoding = kwargs.get("encoding")
        errors = kwargs.get("errors", "strict")
        # encoding 미지정이면 (pre-fix) cp949 로 디코딩 → '—'·이모지에서 깨짐/오역.
        decode_enc = encoding or "cp949"
        decoded = raw.decode(decode_enc, errors=errors)
        return SimpleNamespace(returncode=0, stdout=decoded, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    bootstrap = mod.PmBootstrap()
    rc, output = bootstrap._default_run_board(["list"])

    assert rc == 0
    # utf-8 로 디코딩됐으면 원문이 그대로 — em-dash·한글·이모지 보존.
    assert output == payload
    assert "—" in output and "✅" in output
