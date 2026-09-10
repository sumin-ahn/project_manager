"""PM 홈 자신을 작업 폴더로 등재하던 개념이 소스에서 사라졌는지 (T-0919).

두 축으로 잰다.

  (1) **이름** — 그 개념이 쓰던 식별자가 `.py` 소스 전역에 0회 등장한다. 되살리면 red 다.
  (2) **값** — 갓 만든 홈의 작업 폴더 목록(`pm-config status --json` 의 `slots`)에 홈 자신이
      없다. 개념이 다른 이름으로 부활해도 값이 잡는다(이름이 아니라 값을 재는 축).

`.` 행이 이미 박힌 옛 홈을 흡수가 지우는 축은 `tests/test_upgrade_adopter_e2e.py` 가 소유한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 삭제된 개념의 식별자 — 상수·등록 함수·안내 렌더러·소비자 쪽 배제 판별.
_REMOVED_IDENTIFIERS = (
    "HOME_SLOT",
    "register_home_slot",
    "home_slot_registration_note",
    "_phase0_is_home_slot",
    "_is_home_slot",
)

# 스캔 대상 = 엔진 · 테스트 · 관리자 화면. `templates/` 는 엔진 동기 산출이라 제외한다(원본이
# 0 이면 사본도 0 이고, 사본을 따로 세면 같은 사실을 두 번 센다).
_SCAN_SUBTREES = (".project_manager/tools", "tests", "manager")


def _python_sources() -> list[Path]:
    """repo-owned 열거 seam 으로 모은 `.py` — 자체 tree-walk 를 만들지 않는다."""
    found: list[Path] = []
    for subtree in _SCAN_SUBTREES:
        found += [path for path in repo_owned_paths(REPO, subtree, mode=OWNED)
                  if path.suffix == ".py" and path.is_file()]
    return sorted(found)


def test_removed_identifiers_have_zero_references():
    """삭제한 식별자가 `.py` 소스 어디에도 없다."""
    hits: list[str] = []
    for path in _python_sources():
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for name in _REMOVED_IDENTIFIERS:
            if name in text:
                hits.append(f"{path.relative_to(REPO)}: {name}")
    assert hits == [], "삭제한 개념의 식별자가 남아 있다:\n" + "\n".join(hits)


def test_retired_test_files_are_gone():
    """개념 전용 테스트 파일과 옛 시드 파일이 남아 있지 않다."""
    assert not (REPO / "tests" / "test_solo_shape_abolished.py").exists()
    assert not (REPO / "tests" / "_home_slot.py").exists()
    assert (REPO / "tests" / "_slot_seed.py").is_file()


def test_a_fresh_home_lists_no_work_folder(tmp_path):
    """갓 만든 홈의 작업 폴더 목록이 비어 있다 — 홈 자신을 행으로 지어내지 않는다(실 서브프로세스)."""
    spec_path = TOOLS / "pm_import.py"
    dest = tmp_path / "fresh"
    imported = subprocess.run(
        [sys.executable, str(spec_path), "--new", str(dest), "--harness", "claude",
         "--name", "Adopter", "--fill", "manual", "--from", str(REPO)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"}, timeout=300,
    )
    assert imported.returncode == 0, imported.stderr

    status = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_config.py"),
         "status", "--json"],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env={**os.environ, "PM_NONINTERACTIVE": "1"}, timeout=300,
    )
    assert status.returncode == 0, status.stderr
    slots = json.loads(status.stdout)["slots"]
    assert slots == [], f"import 이 작업 폴더 행을 지어냈다: {slots}"
