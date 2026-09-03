"""루트 conftest — pytest 임시 루트를 프로젝트 안으로 선언한다.

`.project_manager/.local/tmp` 는 per-clone 스크래치(`.project_manager/.gitignore` 의
`.local/`)라 임시물이 저장소를 더럽히지 않는다. 프로젝트 밖(`/tmp` 등)을 쓰지 않으므로
위치가 판정에 영향을 주지 않는다는 이 저장소의 계약이 회귀에서도 성립한다.

선언은 **모듈 로드 시점** 이다 — 컨트롤러와 xdist 워커가 각자 이 파일을 import 하므로 env
상속에 기대지 않고, `Path(__file__).parent` 라 조상 훑기도 없다.

`pytest_configure` 에서 `config.option.basetemp` 를 재설정하지 않는다. 그 훅은 워커
프로세스에서도 돌고 `TempPathFactory.getbasetemp` 는 주어진 basetemp 를 `rm_rf` 한 뒤 다시
만들기 때문에, 워커들이 xdist 가 준 개별 경로 대신 공유 루트를 서로 지운다(실측
`FileNotFoundError` 1517건). `PYTEST_DEBUG_TEMPROOT` 는 basetemp 가 아니라 temproot 만
정해 그 경로를 타지 않는다.
"""
from __future__ import annotations

import os
from pathlib import Path

PYTEST_TEMP_ROOT = Path(__file__).resolve().parent / ".project_manager" / ".local" / "tmp"

# pytest 는 temproot 아래 `pytest-of-<user>` 를 `parents=False` 로 만든다 — temproot 자체는
# 선언한 쪽이 세운다.
PYTEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(PYTEST_TEMP_ROOT)
# 임시 루트가 저장소 **안**이라(T-0888) git 이 픽스처 디렉터리에서 위로 올라가 실제 저장소를 찾는다 —
# 비-git 픽스처에서 `git rev-parse --git-path hooks` 가 실제 보호훅 dir 을 돌려줘 `board init` 이 그것을
# 덮었다(2026-09-03 실측 · 테스트 18건 · 22538→486). 걸어올라감의 천장을 임시 루트로 박아 옛 `/tmp`
# 형상과 같게 한다(픽스처가 자기 저장소를 git init 했으면 그대로 자기 훅으로 해소된다 · 서브프로세스도 상속).
os.environ["GIT_CEILING_DIRECTORIES"] = str(PYTEST_TEMP_ROOT)
