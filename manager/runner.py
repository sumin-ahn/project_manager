"""프로젝트 실행기 — 등록된 PM 홈의 사본을 부르고, local.conf 두 키를 읽는다.

부르는 것은 `pm-config status --json` 하나다([[ADR-0098]]). 옛 세대 사본(`--json` 미지원·
`pm_config.py` 부재)은 호환 분기 없이 실패 그대로 올라가 화면에서 "먼저 갱신" 이 된다.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pm_engine.infra import conf_store

from . import CallError, ENGINE_LOCK, load_engine_module

# 조회 상한 — 가벼운 절의 git 호출은 슬롯마다 5회와 저장소마다 고정 몇 회다. Windows VM 실측
# 슬롯 6개 2.4~2.7초. `--with-board` 는 티켓 883건에서 4.8초다.
LIGHT_TIMEOUT_SECONDS = 60
HEAVY_TIMEOUT_SECONDS = 60
# Windows 에서 사본을 부를 때 콘솔 창이 깜빡이지 않게 한다(POSIX 에는 이 상수가 없어 0).
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _conf_path(pm_home: Path | str) -> Path:
    return Path(pm_home) / ".project_manager" / "local.conf"


def conf_values(pm_home: Path | str) -> dict[str, str]:
    """local.conf 값 — 번들 엔진의 `conf_store.load` 로 읽는다.

    `load_checked` 가 아니다: 구표기 키가 남은 사본에 예외를 던져, "먼저 갱신" 을 보여 줘야 할
    바로 그 사본을 아예 못 부르게 만든다. `load` 는 raise 하지 않는다(부재=빈 값).

    엔진 호출은 `ENGINE_LOCK` 안에서 한다 — 첫 호출이 형제 모듈을 지연 로드하므로 스레드가
    겹치면 반쯤 로드된 형제를 집어 간다(창은 화면의 호출을 스레드마다 따로 처리한다).
    """
    with ENGINE_LOCK:
        return conf_store.load(_conf_path(pm_home)).values


def interpreter(pm_home: Path | str, *, os_name: str = os.name) -> tuple[str, ...]:
    """사본을 부를 인터프리터 argv — 엔진 `pm_bootstrap._resolve_python_argv` 와 같은 규칙.

    비어 있지 않은 `runtime.py` 는 실행 토큰 **한 개** 그대로다(`python3` 같은 PATH 이름과
    절대경로 둘 다 실측된다 · `Path` 로 감싸면 이름이 상대경로가 되어 깨진다). 빈 값이면 OS
    기본이다 — 예외를 두지 않는다(실측상 이 머신 PM 홈 7개 중 4개가 빈 값이다).
    """
    configured = (conf_values(pm_home).get("runtime.py") or "").strip()
    if configured:
        return (configured,)
    if os_name == "nt":
        return ("py", "-3")
    return ("python3",)


def upstream(pm_home: Path | str) -> str | None:
    """`upstream.path` 를 조회 가능한 값으로 — 상대 경로는 PM 홈 기준(엔진 해소 규칙과 같다).

    URL 은 그대로 둔다. 미등록이면 None 이고 그 프로젝트는 최신 태그 축이 없다.
    """
    value = (conf_values(pm_home).get("upstream.path") or "").strip()
    if not value:
        return None
    with ENGINE_LOCK:
        kind = load_engine_module("pm_import.py").classify_upstream(value)
    if kind == "path":
        return str((Path(pm_home) / value).resolve())
    return value


def status(pm_home: Path | str, *, with_board: bool = False, with_adapters: bool = False,
           with_remote: bool = False, source: Path | str | None = None) -> dict:
    """그 사본의 `pm-config status --json` 산출. 실패는 `CallError` 로 올린다."""
    # cwd 를 그 홈으로 옮겨 부르므로 경로를 먼저 절대화한다 — 상대 경로를 그대로 쓰면 자식이
    # 새 cwd 기준으로 다시 풀어 엉뚱한 자리를 가리킨다(실측: 경로가 두 번 붙는다).
    home = Path(pm_home).resolve()
    if not (home / ".project_manager").is_dir():
        raise CallError("invalid-path", f"PM 홈이 아니다 (.project_manager/ 없음): {home}")
    argv = [*interpreter(home), str(home / ".project_manager" / "tools" / "pm_config.py"),
            "status", "--json"]
    if with_board:
        argv.append("--with-board")
    if with_adapters:
        argv.append("--with-adapters")
    if with_remote:
        argv.append("--with-remote")          # repo 마다 원격 HEAD 브랜치를 fetch 한다(네트워크)
    if source is not None:
        argv += ["--from", str(source)]
    timeout = (HEAVY_TIMEOUT_SECONDS if (with_board or with_adapters or with_remote)
               else LIGHT_TIMEOUT_SECONDS)
    try:
        completed = subprocess.run(
            argv, cwd=str(home), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=CREATION_FLAGS)
    except subprocess.TimeoutExpired as exc:
        raise CallError("timeout", f"{timeout}초 안에 응답이 없다: {' '.join(argv)}") from exc
    except OSError as exc:
        # 인터프리터 자체를 실행하지 못한 자리(`runtime.py` 가 가리키는 파일 부재·권한).
        raise CallError("spawn", f"인터프리터를 실행하지 못했다: {' '.join(argv)} ({exc})") from exc
    if completed.returncode != 0:
        raise CallError(
            "old-generation",
            "이 사본의 엔진은 `status --json` 을 모른다 — 먼저 갱신하라.",
            stderr=completed.stderr, rc=completed.returncode)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CallError(
            "not-json", f"현황 출력이 JSON 이 아니다 ({exc})",
            stderr=completed.stderr or completed.stdout, rc=completed.returncode) from exc
