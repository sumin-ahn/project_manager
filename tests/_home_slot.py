"""홈 N=1 슬롯 행 시드 — hermetic 테스트가 "등록된 홈" 형상을 실 장부로 재현한다.

슬롯을 하나만 쓰는 홈도 자기 자신이 장부의 첫 슬롯 행(`slot="."`)이다. 정체성 해소·핸드오프
승인 대상값·회귀 cwd 가 전부 그 행에서 오므로, 행이 없는 tmp 홈은 "아직 등록되지 않은 홈"이고
귀속 조작이 fail-loud 한다. 이 헬퍼가 그 행 하나를 실 파일로 깔아 준다(조립 dict 아님).

세션 키는 tmp 디렉토리 이름과 무관한 고정값(`HOME_SESSION`)이라 단언이 결정적이다 — 슬롯
정체성은 경로가 아니라 행의 `session` 값에서 온다.
"""
from __future__ import annotations

import json
from pathlib import Path

HOME_SLOT = "."
HOME_REPO = "proj"
HOME_SESSION = f"{HOME_REPO}_1"


def home_lease_row(*, slot: str = HOME_SLOT, repo: str = HOME_REPO,
                   session: str = HOME_SESSION) -> dict:
    """홈 슬롯 lease 행 1개 (엔진이 등록 시 쓰는 canonical 키 집합)."""
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


def seed_home_slot(repo_root: Path, *, slot: str = HOME_SLOT, repo: str = HOME_REPO,
                   session: str = HOME_SESSION, tasks: list | None = None) -> Path:
    """`repo_root` 를 등록된 N=1 홈으로 만든다 — lease 장부 파일 경로를 반환한다."""
    ledger = Path(repo_root) / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {"leases": [home_lease_row(slot=slot, repo=repo, session=session)],
             "tasks": tasks or []},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return ledger


def seed_areas(repo_root: Path, *, repo: str = HOME_REPO) -> Path:
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
