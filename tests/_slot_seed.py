"""작업 폴더(슬롯) 행 시드 — hermetic 테스트가 "슬롯이 있는 홈" 형상을 실 장부로 재현한다.

정체성 해소·핸드오프 승인 대상값·회귀 cwd 가 전부 리스 장부의 슬롯 행에서 오므로, 행이 없는
tmp 홈은 "아직 작업 폴더가 없는 홈"이고 귀속 조작이 fail-loud 한다. 이 헬퍼가 그 행 하나를 실
파일로 깔아 준다(조립 dict 아님).

세션 키는 tmp 디렉토리 이름과 무관한 고정값(`SESSION`)이라 단언이 결정적이다 — 슬롯 정체성은
경로가 아니라 행의 `session` 값에서 온다.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

SLOT_REPO = "proj"
SESSION = f"{SLOT_REPO}_1"
SLOT = f"work/{SLOT_REPO}_1"


def lease_row(*, slot: str = SLOT, repo: str = SLOT_REPO,
              session: str = SESSION) -> dict:
    """슬롯 lease 행 1개 (엔진이 등록 시 쓰는 canonical 키 집합)."""
    return {
        "slot": slot,
        "repo": repo,
        "session": session,
        "pid": 0,
        "started": "2026-08-22T00:00:00+00:00",
        "state": "leased",
        "test_cmd": None,
        "bound": True,
    }


def seed_slot(repo_root: Path, *, slot: str = SLOT, repo: str = SLOT_REPO,
              session: str = SESSION, tasks: list | None = None) -> Path:
    """`repo_root` 를 작업 폴더 1개짜리 홈으로 만든다 — lease 장부 파일 경로를 반환한다."""
    ledger = Path(repo_root) / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {"leases": [lease_row(slot=slot, repo=repo, session=session)],
             "tasks": tasks or []},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return ledger


def seed_areas(repo_root: Path, *, repo: str = SLOT_REPO) -> Path:
    """`repo_root` 의 areas.md 에 repo 행 1개를 깐다 (등록 repo 정확히 1개 형상)."""
    areas = Path(repo_root) / ".project_manager" / "areas.md"
    areas.parent.mkdir(parents=True, exist_ok=True)
    areas.write_text(
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| {repo} |  |  |  |  |  |  |  |\n",
        encoding="utf-8",
    )
    return areas


# 대역(worktree_pool mock) 풀이 내는 슬롯 경로의 뿌리 — 0단계 실재 검사가 `.exists()` 로
# 판정하므로 **실재하는** 디렉터리여야 한다(장부 행이 폴더 부재를 대신 통과시키던 예외는
# 삭제됐다). 임시물은 프로젝트 안 `.project_manager/.local/tmp` 아래에 만든다.
_FAKE_SLOT_ROOT: "Path | None" = None


def fake_slot_path(slot: str) -> Path:
    """대역 풀용 슬롯 경로 — 호출 시 만들어 두고 그 경로를 낸다."""
    global _FAKE_SLOT_ROOT
    if _FAKE_SLOT_ROOT is None:
        base = Path(__file__).resolve().parents[1] / ".project_manager" / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        _FAKE_SLOT_ROOT = Path(tempfile.mkdtemp(prefix="fake-slots-", dir=base))
    path = _FAKE_SLOT_ROOT / slot
    path.mkdir(parents=True, exist_ok=True)
    return path
