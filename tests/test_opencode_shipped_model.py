"""시험 코드가 출하 OpenCode 모델 이름을 다시 적지 않는지 지키는 시험."""
from __future__ import annotations

from _opencode_model import SHIPPED_MODEL_ID
from _repo_owned_inventory import OWNED, REPO, repo_owned_paths


def test_test_code_does_not_repeat_the_shipped_opencode_model():
    repeated = [
        f"{path.relative_to(REPO).as_posix()}:{number}"
        for path in repo_owned_paths(REPO, "tests", mode=OWNED) if path.suffix == ".py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if SHIPPED_MODEL_ID in line
    ]
    assert not repeated, "출하 모델 id 는 tests/_opencode_model.py 에서 가져온다: " + ", ".join(repeated)
