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

from _harness_matrix import HARNESSES as _HARNESS_IDS, _PM_IMPORT

REPO = Path(__file__).resolve().parents[1]
SH = REPO / "pm-import.sh"
CMD = REPO / "pm-import.cmd"


def _facade_axis(harness_ids, harness_template_dirs) -> tuple[str, ...]:
    """update 파사드 byte-drift 축 = 하네스 정체성 → 템플릿-dir 명(`templates/<dir>`).

    파생원 = `_harness_matrix.HARNESSES`(하네스 정체성·T-0429) + 엔진 `HARNESS_TEMPLATE_DIRS`
    (하네스 → 어댑터 트리 dir 명). 이 축의 원소는 **템플릿-dir 명**(claude_code)이지 하네스 정체성
    (claude)이 아니다 — 의미 축을 안 섞는다(T-0434 결정·T-0429 대조표 '별개' 판정 유지). 새 단일-
    어댑터 하네스가 추가되면 이 축(및 아래 UPDATE_SH/CMD·드리프트 가드)에 자동 편입된다.
    """
    return tuple(harness_template_dirs[h][0] for h in harness_ids)


# update 파사드 — templates 각 하네스 루트(채택자 루트로 배포·T-0054). 축은 파생(손-열거 아님·T-0434).
HARNESSES = _facade_axis(_HARNESS_IDS, _PM_IMPORT.HARNESS_TEMPLATE_DIRS)
UPDATE_SH = {h: REPO / "templates" / h / "pm-update.sh" for h in HARNESSES}
UPDATE_CMD = {h: REPO / "templates" / h / "pm-update.cmd" for h in HARNESSES}

# subprocess 는 CreateProcess 검색 순서상 System32\bash.exe(WSL 런처)를 PATH 의 Git Bash 보다
# 먼저 집는다 — WSL bash 는 `/mnt/c/…` 마운트라 Windows-form 경로(`C:/…`·`/c/…`)를 못 연다(rc127).
# `shutil.which` 는 PATH 순서(=Git Bash)를 반환하므로 그 절대경로로 실행해 일관된 POSIX 셸을 쓴다
# (Linux 는 `/bin/bash`·무영향). requires_bash 와 동일 소스라 skip 조건과 정합.
BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None,
    reason="bash 부재(POSIX e2e 불가) — .sh 실행 환경 아님",
)


def _bash_arg(path) -> str:
    """bash 에 넘길 경로 인자를 forward-slash(as_posix) 로 변환.

    Windows Git Bash 는 argv 의 `\\` 를 escape 로 처리해 소실한다(`C:\\Users` → `C:Users`) —
    Windows-form 절대경로를 그대로 넘기면 스크립트를 못 찾거나(rc127) 포워딩 인자가 깨진다.
    forward-slash 형은 bash 가 그대로 해소하고, native python 호출 시 MSYS 가 Windows 경로로
    되돌린다. POSIX 는 `as_posix` 가 무변경이라 동작 불변(교차플랫폼 안전).
    """
    return Path(path).as_posix()


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
        [BASH, _bash_arg(SH), "--dry-run", "--new", _bash_arg(dest),
         "--harness", "opencode"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
        [BASH, _bash_arg(SH), "--dry-run", "--new", _bash_arg(dest),
         "--harness", "opencode"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    # source 가 이 manager 루트의 templates/opencode 로 해소돼야 한다.
    # Windows 는 소스 출력이 mixed-separator(`…project_manager_1/templates/opencode` — pm_import
    # f-string 이 `/templates/` 를 하드코딩)라 separator 정규화 후 비교. POSIX 는 무변경.
    expected_src = str(REPO / "templates" / "opencode")
    assert expected_src.replace("\\", "/") in combined.replace("\\", "/"), combined


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
        [BASH, _bash_arg(SH), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
        [BASH, _bash_arg(UPDATE_SH[harness]), "--help"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    """모든 템플릿 pm-update.sh 는 byte 동일(harness-무관 thin forwarder·파생 축 전체·T-0434)."""
    bodies = {h: UPDATE_SH[h].read_text(encoding="utf-8") for h in HARNESSES}
    ref = HARNESSES[0]
    drift = sorted(h for h in HARNESSES if bodies[h] != bodies[ref])
    assert not drift, f"pm-update.sh 드리프트({ref} 기준 불일치): {drift}"


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
    """모든 템플릿 pm-update.cmd 는 byte 동일(파생 축 전체·T-0434)."""
    bodies = {h: UPDATE_CMD[h].read_text(encoding="utf-8") for h in HARNESSES}
    ref = HARNESSES[0]
    drift = sorted(h for h in HARNESSES if bodies[h] != bodies[ref])
    assert not drift, f"pm-update.cmd 드리프트({ref} 기준 불일치): {drift}"


def test_facade_axis_auto_includes_new_harness() -> None:
    """파사드 byte-drift 축이 파생 하네스 정체성 → 템플릿-dir 매핑임을 못박는다(손-열거 아님·T-0434).

    실축(HARNESSES)은 파생 하네스 전부의 템플릿-dir 명, 가짜 4번째 하네스(상수 + templates dir
    등록)는 **자동 편입**된다(T-0429 `derive_harnesses` 가짜-하네스 패턴 재사용). 네 번째 하네스가
    파사드 게이트(UPDATE_SH/CMD·드리프트·forward 토큰)에 손 편집 0 으로 흐르는 게 티켓 핵심.
    """
    # 실축 = 파생 하네스 정체성을 템플릿-dir 로 매핑한 것과 동일.
    assert HARNESSES == _facade_axis(_HARNESS_IDS, _PM_IMPORT.HARNESS_TEMPLATE_DIRS)
    # 가짜 4번째 하네스(상수 + templates dir) → 축에 자동 편입(dir 명 매핑 경유).
    fake_ids = (*_HARNESS_IDS, "fourth")
    fake_map = {**_PM_IMPORT.HARNESS_TEMPLATE_DIRS, "fourth": ("fourth_tmpl",)}
    got = _facade_axis(fake_ids, fake_map)
    assert got[-1] == "fourth_tmpl", got
    assert set(HARNESSES) < set(got), got


# --- import 복사: 채택자 루트로 pm-update.sh 배포 (hermetic) ---

@pytest.mark.parametrize("harness", _HARNESS_IDS)
def test_update_facade_deployed_to_adopter_root(harness: str, tmp_path: Path,
                                                monkeypatch) -> None:
    """pm_import --new <dest> --harness <h> 후 채택자 루트에 pm-update.* 가 복사된다.

    축 = 파생 하네스 정체성(`_HARNESS_IDS`·claude/codex/opencode·T-0453) — 파사드 dir 명 축이
    아니라 `--harness` **정체성** 축이다(의미 축 안 섞음). 파사드 배포는 하네스-무관(전 하네스가
    templates/<h>/ 루트 파일 → 채택자 루트로 복사)이라 codex 도 정당 편입 — 손-열거
    `["claude","opencode"]` 는 codex(ADR-0070) 를 못 따라온 decay 였다. plan_copy 가 AGENTS.md
    처럼 채택자 루트로 복사한다. opencode 경로의 라이브 `opencode models` 호출은
    _real_models_runner 고정으로 차단(codex/claude 는 그 seam 미호출·무영향).
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
