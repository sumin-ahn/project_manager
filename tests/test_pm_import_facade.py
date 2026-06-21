"""pm-import.sh / pm-import.cmd · pm-update.sh / pm-update.cmd 파사드 테스트 (T-0052·T-0054).

파사드는 thin forwarder — 자기 위치를 해석해 deep 경로(`.project_manager/tools/pm_*.py`)를
호출하고 모든 인자를 그대로 forward 한다. 자체 로직 0.

- import 파사드(manager 루트·T-0052) — POSIX e2e: 임의 cwd 에서 `bash pm-import.sh --dry-run ...`
  가 pm_import 에 *도달*하는지를 rc 0 + dry-run 출력 마커로 단언(실 하니스 무호출·미변경).
- update 파사드(templates 양쪽 루트·채택자 루트로 배포·T-0054) — POSIX e2e: `bash
  pm-update.sh --help` 가 pm_update 에 *도달*해 epilog 의 upstream 등록 안내를 surface 하는지
  (`--help` 는 부작용 0·실 sync 안 함).
- `--help` surface 검증: import/update 파사드 양쪽 epilog(T-0053)의 upstream 안내 문구.
- `.sh` 실행권한 + forward 토큰 정적 단언 + 양 템플릿 드리프트 가드(동일성).
- `.cmd` 는 Linux 러너서 실행 불가 → 내용 토큰 정적 단언.
- import 복사: pm_import --new 후 채택자 루트에 pm-update.sh 가 배포되는지(hermetic).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess

import pytest

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SH = REPO / "pm-import.sh"
CMD = REPO / "pm-import.cmd"

# update 파사드 — templates 양쪽 루트(채택자 루트로 배포·T-0054).
HARNESSES = ("claude_code", "opencode")
UPDATE_SH = {h: REPO / "templates" / h / "pm-update.sh" for h in HARNESSES}
UPDATE_CMD = {h: REPO / "templates" / h / "pm-update.cmd" for h in HARNESSES}

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash 부재(POSIX e2e 불가) — .sh 실행 환경 아님",
)


def _load_pm_import():
    """import 복사 검증용 — 엔진 pm_import 모듈을 동적 로드(외부 의존 0)."""
    tools = REPO / ".project_manager" / "tools"
    spec = importlib.util.spec_from_file_location("pm_import", tools / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- POSIX e2e: 임의 cwd 에서 파사드가 pm_import dry-run 에 도달 ---

@requires_bash
def test_sh_facade_reaches_pm_import_dry_run(tmp_path: Path) -> None:
    """다른 디렉토리에서 호출해도 자기 위치 기준으로 pm_import 에 도달·dry-run 출력."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    dest = tmp_path / "facade_dest"

    proc = subprocess.run(
        ["bash", str(SH), "--dry-run", "--new", str(dest),
         "--harness", "opencode"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"rc={proc.returncode}\n{combined}"
    # pm_import dry-run 도달 마커 — "소스:" 헤더 + dry-run 미변경 안내.
    assert "소스:" in combined, combined
    assert "dry-run" in combined, combined
    # dry-run 은 파일시스템 미변경 — dest 가 생성되면 안 된다.
    assert not dest.exists(), "dry-run 인데 dest 가 생성됨"


@requires_bash
def test_sh_facade_forwards_from_default_to_manager_root(tmp_path: Path) -> None:
    """--from 미지정 시 pm_import 이 manager 루트로 auto-default — opencode 소스 트리 도달."""
    dest = tmp_path / "facade_dest2"
    proc = subprocess.run(
        ["bash", str(SH), "--dry-run", "--new", str(dest),
         "--harness", "opencode"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    # source 가 이 manager 루트의 templates/opencode 로 해소돼야 한다.
    expected_src = str(REPO / "templates" / "opencode")
    assert expected_src in combined, combined


# --- .sh 정적 단언 ---

def test_sh_is_executable() -> None:
    assert SH.is_file(), "pm-import.sh 부재"
    assert os.access(SH, os.X_OK), "pm-import.sh 실행권한 비트 없음"


def test_sh_forwards_verbatim_with_exec() -> None:
    body = SH.read_text(encoding="utf-8")
    # 인자 verbatim forward + exec 로 rc 전파.
    assert '"$@"' in body, "인자 forward($@) 없음"
    assert "exec " in body, "exec 로 rc 전파 안 함"
    assert "pm_import.py" in body, "pm_import.py 경로 호출 없음"
    # cwd 무관 자기위치 해석.
    assert 'dirname "$0"' in body, "자기위치(dirname $0) 해석 없음"
    # POSIX 인터프리터 선호순 python3 → python.
    assert "python3" in body and "python" in body, "인터프리터 후보 없음"


# --- .cmd 정적 단언 (Linux 러너 실행 불가) ---

def test_cmd_exists_and_has_forward_tokens() -> None:
    assert CMD.is_file(), "pm-import.cmd 부재"
    body = CMD.read_text(encoding="utf-8")
    # 자기위치 기준 deep 경로 호출.
    assert "%~dp0" in body, "%~dp0 (배치 위치) 없음"
    assert "pm_import.py" in body, "pm_import.py 경로 호출 없음"
    # 인자 forward + rc 전파.
    assert "%*" in body, "%* 인자 forward 없음"
    assert "exit /b" in body, "exit /b rc 전파 없음"
    # 인터프리터 후보 3종 (python / py / python3).
    assert "python" in body, "python 후보 없음"
    assert "py" in body, "py 후보 없음"
    assert "python3" in body, "python3 후보 없음"


# ── import 파사드 --help surface 검증 (T-0053 epilog) ────────────────────────

@requires_bash
def test_import_sh_help_surfaces_upstream_record_note() -> None:
    """pm-import.sh --help 가 pm_import epilog 의 upstream 기록 안내를 surface."""
    proc = subprocess.run(
        ["bash", str(SH), "--help"],
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"rc={proc.returncode}\n{combined}"
    # T-0053 pm_import epilog — import source 가 local.conf upstream= 으로 기록된다는 안내.
    assert "upstream" in combined, combined
    assert "pm_update" in combined, combined


# ── update 파사드 (templates 양쪽 루트·채택자 루트로 배포·T-0054) ────────────

# --- POSIX e2e: pm-update.sh --help 가 pm_update epilog 의 upstream 등록 안내 surface ---

@requires_bash
@pytest.mark.parametrize("harness", HARNESSES)
def test_update_sh_help_surfaces_upstream_note(harness: str, tmp_path: Path) -> None:
    """임의 cwd 에서 pm-update.sh --help 가 pm_update 에 도달·epilog 의 upstream 등록 안내 출력.

    `--help` 는 부작용 0 — 실 sync 가 일어나지 않음을 cwd 변화 없음으로 함께 확인.
    """
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    proc = subprocess.run(
        ["bash", str(UPDATE_SH[harness]), "--help"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"rc={proc.returncode}\n{combined}"
    # T-0053 pm_update epilog — --from 생략 시 local.conf upstream= 사용 안내.
    assert "upstream" in combined, combined
    assert "--from" in combined, combined
    # --help 는 부작용 0 — 호출 cwd 에 아무것도 생성되지 않아야 한다.
    assert list(cwd.iterdir()) == [], "--help 인데 cwd 에 산출물 생성됨"


# --- .sh 정적 단언 + 실행권한 + 드리프트 가드 ---

@pytest.mark.parametrize("harness", HARNESSES)
def test_update_sh_is_executable(harness: str) -> None:
    sh = UPDATE_SH[harness]
    assert sh.is_file(), f"{harness}/pm-update.sh 부재"
    assert os.access(sh, os.X_OK), f"{harness}/pm-update.sh 실행권한 비트 없음"


@pytest.mark.parametrize("harness", HARNESSES)
def test_update_sh_forwards_verbatim_with_exec(harness: str) -> None:
    body = UPDATE_SH[harness].read_text(encoding="utf-8")
    # 인자 verbatim forward + exec 로 rc 전파.
    assert '"$@"' in body, "인자 forward($@) 없음"
    assert "exec " in body, "exec 로 rc 전파 안 함"
    assert "pm_update.py" in body, "pm_update.py 경로 호출 없음"
    # cwd 무관 자기위치 해석.
    assert 'dirname "$0"' in body, "자기위치(dirname $0) 해석 없음"
    # POSIX 인터프리터 선호순 python3 → python.
    assert "python3" in body and "python" in body, "인터프리터 후보 없음"


def test_update_sh_drift_guard_identical() -> None:
    """양 템플릿 pm-update.sh 는 byte 동일(harness-무관 thin forwarder)."""
    bodies = {h: UPDATE_SH[h].read_text(encoding="utf-8") for h in HARNESSES}
    assert bodies["claude_code"] == bodies["opencode"], \
        "claude_code / opencode pm-update.sh 드리프트"


# --- .cmd 정적 단언 + 드리프트 가드 (Linux 러너 실행 불가) ---

@pytest.mark.parametrize("harness", HARNESSES)
def test_update_cmd_exists_and_has_forward_tokens(harness: str) -> None:
    cmd = UPDATE_CMD[harness]
    assert cmd.is_file(), f"{harness}/pm-update.cmd 부재"
    body = cmd.read_text(encoding="utf-8")
    # 자기위치 기준 deep 경로 호출.
    assert "%~dp0" in body, "%~dp0 (배치 위치) 없음"
    assert "pm_update.py" in body, "pm_update.py 경로 호출 없음"
    # 인자 forward + rc 전파.
    assert "%*" in body, "%* 인자 forward 없음"
    assert "exit /b" in body, "exit /b rc 전파 없음"
    # 인터프리터 후보 3종 (python / py / python3).
    assert "python" in body, "python 후보 없음"
    assert "py" in body, "py 후보 없음"
    assert "python3" in body, "python3 후보 없음"


def test_update_cmd_drift_guard_identical() -> None:
    """양 템플릿 pm-update.cmd 는 byte 동일."""
    bodies = {h: UPDATE_CMD[h].read_text(encoding="utf-8") for h in HARNESSES}
    assert bodies["claude_code"] == bodies["opencode"], \
        "claude_code / opencode pm-update.cmd 드리프트"


# --- import 복사: 채택자 루트로 pm-update.sh 배포 (hermetic) ---

@pytest.mark.parametrize("harness", ["claude", "opencode"])
def test_update_facade_deployed_to_adopter_root(harness: str, tmp_path: Path,
                                                monkeypatch) -> None:
    """pm_import --new <dest> --harness <h> 후 채택자 루트에 pm-update.* 가 복사된다.

    파사드는 templates/<harness>/ 루트 파일 → plan_copy 가 AGENTS.md 처럼 채택자 루트로
    복사한다. opencode 경로의 라이브 `opencode models` 호출은 _real_models_runner 고정으로 차단.
    """
    pm_import = _load_pm_import()
    # opencode models CLI 라이브 호출 차단(설치 환경서도 hermetic).
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))

    dest = tmp_path / "adopter"
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", "P"])
    assert rc == 0, f"pm_import rc={rc}"

    # 채택자 루트에 update 파사드 배포 + deep 엔진 진입점 존재.
    assert (dest / "pm-update.sh").is_file(), "채택자 루트에 pm-update.sh 미배포"
    assert (dest / "pm-update.cmd").is_file(), "채택자 루트에 pm-update.cmd 미배포"
    assert (dest / ".project_manager" / "tools" / "pm_update.py").is_file(), \
        "deep 엔진 진입점 pm_update.py 부재"
