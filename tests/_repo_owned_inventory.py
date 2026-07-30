"""실 repo 트리를 검사하는 테스트용 공용 repo-owned 열거 어댑터."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
_HELPER = REPO / ".project_manager" / "tools" / "repo_owned_files.py"
_MODULE_NAME = f"_tests_repo_owned_files:{_HELPER.resolve()}"


def _load_repo_owned_files():
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — canonical 도구 사본을 확인하라"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


REPO_FILES = _load_repo_owned_files()
TRACKED_ONLY = REPO_FILES.TRACKED_ONLY
OWNED = REPO_FILES.OWNED
RepoFilesFallbackWarning = REPO_FILES.RepoFilesFallbackWarning


def repo_owned_paths(
    checkout: Path,
    subtree: Path | str,
    *,
    mode: str,
) -> list[Path]:
    """seam 엔트리를 working-tree 재검사 없이 checkout 기준 절대 경로로 반환한다.

    git 성공 결과는 index/``ls-files``가 진실이다. 여기서 ``is_file()``로 다시 거르면
    삭제된 tracked 파일뿐 아니라 symlink(mode 120000)·gitlink(mode 160000)까지 조용히
    사라져 출하 가드가 false-green이 될 수 있다. 따라서 canonical seam과 같이 엔트리를
    보존하고, 실제 byte 읽기/파일형 제한이 필요한 소비처가 loud하게 판정하게 한다.
    """
    root = checkout.resolve()
    return [
        root / entry.path
        for entry in REPO_FILES.list_repo_owned_entries(root, subtree, mode=mode)
    ]
