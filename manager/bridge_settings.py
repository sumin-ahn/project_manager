"""설정 탭의 브리지 메서드 — local.conf 편집과 어댑터 파일 절.

경계 두 줄(패키지 docstring)이 이 파일에서 구체화된다.

  - **쓰는 파일은 `local.conf` 하나**이고, 그 쓰기는 번들 엔진의 `pm_import._write_conf_keys`
    한 함수를 통한다(원자 교체 + 대상 conf 에서 유도한 배타락 + 실효값 검증). 두 번째 conf
    writer 를 만들지 않는다. 어댑터 수용·upstream 전환은 **그 사본의 명령**이 한다.
  - 항목 목록의 정본은 **그 프로젝트 사본의** 키 레지스트리다
    (`<interpreter> <pm_home>/.project_manager/tools/local_conf.py --keys --json`).
    번들 레지스트리로 대신 그리지 않는다 — 세대가 다르면 키 집합이 다르다.

저장 직후에는 같은 사본을 `status --json` 으로 다시 읽어 그 판정을 돌려준다(낙관적 갱신 없음).
저장이 어댑터 카드에 박히는 키를 건드려도 여기서 렌더를 돌리지 않는다 — 재렌더 경로는
`pm-config update` 하나이고, 그 전량 동기는 갱신 탭의 일이다.

어댑터 절과 매트릭스는 **버튼으로만** 돈다(`adapters`·`adapter_matrix`). URL upstream 프로젝트는
소스 해소가 전체 clone 이라 탭을 여는 것만으로 네트워크가 나가면 안 된다.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import CallError, ENGINE_LOCK, load_engine_module
from . import registry, runner


# 이 키만 값 자체가 도달성 계약이라 conf writer 로 곧장 쓰지 않는다 — 전환은 그 사본의
# `pm-config upstream set` 이고(URL→ls-remote·경로→git checkout 확인·fail-closed), 그 명령이
# 같은 writer 로 커밋한다.
UPSTREAM_PATH_KEY = "upstream.path"


def _conf_path(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "local.conf"


def _pm_home(path: Path | str) -> Path:
    """등록된 PM 홈으로 해소한다 — 사본을 부르기 전에 절대화·표식 확인(runner 와 같은 규칙)."""
    home = Path(path).resolve()
    if not (home / ".project_manager").is_dir():
        raise CallError("invalid-path", f"PM 홈이 아니다 (.project_manager/ 없음): {home}")
    return home


def _run(home: Path, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """그 사본의 스크립트 하나를 부른다 — rc 는 판정하지 않고 그대로 돌려준다.

    rc≠0 이 곧 실패인 자리(`--keys --json`)와 rc 자체가 산출인 자리(수용 거부·upstream 거부)를
    한 함수로 덮으려면 판정은 호출부가 해야 한다. 스폰 실패·응답 없음만 여기서 `CallError` 다.
    """
    try:
        return subprocess.run(
            argv, cwd=str(home), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=runner.CREATION_FLAGS)
    except subprocess.TimeoutExpired as exc:
        raise CallError("timeout", f"{timeout}초 안에 응답이 없다: {' '.join(argv)}") from exc
    except OSError as exc:
        raise CallError("spawn", f"인터프리터를 실행하지 못했다: {' '.join(argv)} ({exc})") from exc


def _tool(home: Path, filename: str) -> str:
    return str(home / ".project_manager" / "tools" / filename)


def _completed(completed: subprocess.CompletedProcess) -> dict:
    """rc·stdout·stderr 를 그대로 화면에 넘기는 자리 — 거부 문구는 엔진 원문이다."""
    return {"rc": completed.returncode, "stdout": completed.stdout,
            "stderr": completed.stderr}


class SettingsApi:
    """설정 탭(local.conf 편집·어댑터 절) 동작."""

    # ── local.conf ────────────────────────────────────────────────────────
    def conf_keys(self, path: str) -> dict:
        """그 사본의 키 레지스트리 — `local_conf.py --keys --json` 산출 그대로."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "local_conf.py"),
                    "--keys", "--json"]
            completed = _run(home, argv, timeout=runner.LIGHT_TIMEOUT_SECONDS)
            if completed.returncode != 0:
                raise CallError(
                    "old-generation",
                    "이 사본의 엔진은 `local_conf.py --keys --json` 을 모른다 — 먼저 갱신하라.",
                    stderr=completed.stderr, rc=completed.returncode)
            try:
                return json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise CallError(
                    "not-json", f"키 목록 출력이 JSON 이 아니다 ({exc})",
                    stderr=completed.stderr or completed.stdout,
                    rc=completed.returncode) from exc
        except CallError as exc:
            return exc.as_error_dict()

    def conf_values(self, path: str) -> dict:
        """현재 값 + 설정 완전성 다섯 목록.

        값은 번들 엔진의 `local_conf.load(...).values`(runner 와 같은 자리)이고, 판정은 그 사본의
        `status --json` 의 `conf` 절이다 — 판정을 관리자가 다시 계산하지 않는다.
        """
        try:
            home = _pm_home(path)
            return {"values": runner.conf_values(home),
                    "conf": runner.status(home)["conf"]}
        except CallError as exc:
            return exc.as_error_dict()

    def conf_save(self, path: str, updates: dict) -> dict:
        """바뀐 키만 쓰고 **직후 재조회**한 판정을 돌려준다.

        쓰기는 번들 엔진의 유일한 conf writer 한 함수다. 요청값과 실효값이 다르면 그 엔진이
        `RuntimeError` 로 멈추고, 여기서 잡지 않는다(성공으로 흡수할 수 없는 실패다).
        """
        if UPSTREAM_PATH_KEY in updates:
            raise ValueError(
                f"{UPSTREAM_PATH_KEY} 는 이 경로로 쓰지 않는다 — 도달성 검증을 포함한 "
                "`upstream_set(path, value)` 을 부른다")
        try:
            home = _pm_home(path)
            with ENGINE_LOCK:
                changed = load_engine_module("pm_import.py")._write_conf_keys(
                    _conf_path(home), dict(updates))
            return {"changed": bool(changed), "conf": runner.status(home)["conf"]}
        except CallError as exc:
            return exc.as_error_dict()

    def conf_reset(self, path: str, key: str) -> dict:
        """[기본값으로] — 레지스트리 기본값을 쓴다. 기본값이 없는 키는 되돌릴 자리가 없다."""
        with ENGINE_LOCK:
            default = load_engine_module("local_conf.py").default_for(key)
        if default is None:
            raise ValueError(
                f"기본값이 선언되지 않은 키는 되돌릴 수 없다: {key} "
                "(화면은 이 키에 [기본값으로] 버튼을 두지 않는다)")
        return self.conf_save(path, {key: default})

    # ── 어댑터 파일 ───────────────────────────────────────────────────────
    def adapters(self, path: str) -> dict:
        """`status --json --with-adapters` 의 `adapters` 절 — **버튼으로만** 부른다.

        소스는 그 사본이 자기 `upstream.path` 로 해소한다(URL 이면 엔진 캐시 clone/fetch).
        """
        try:
            return runner.status(_pm_home(path), with_adapters=True)["adapters"]
        except CallError as exc:
            return exc.as_error_dict()

    def adapter_accept(self, path: str, relpath: str) -> dict:
        """상류 값 수용 — 그 사본의 `sync-adapter-config --accept <relpath>` 한 번.

        관리자가 파일을 복사하거나 백업하지 않는다(백업·원장 기록은 엔진이 한다).
        """
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "sync-adapter-config", "--accept", relpath]
            return _completed(_run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()

    def adapter_matrix(self) -> dict:
        """등록 프로젝트 전부의 어댑터 절 — 프로젝트별로 독립 실패한다(버튼으로만)."""
        try:
            homes = registry.load(self._registry_file)
        except CallError as exc:
            return exc.as_error_dict()
        return {str(home): self.adapters(str(home)) for home in homes}

    # ── upstream ──────────────────────────────────────────────────────────
    def upstream_show(self, path: str) -> dict:
        """`pm-config upstream show` — 미등록 안내도 그 사본의 문구 그대로다."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "upstream", "show"]
            return _completed(_run(home, argv, timeout=runner.LIGHT_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()

    def upstream_set(self, path: str, value: str) -> dict:
        """`pm-config upstream set <값>` — 도달성 검증은 그 사본이 한다(거부 문구 그대로)."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "upstream", "set", value]
            return _completed(_run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()
