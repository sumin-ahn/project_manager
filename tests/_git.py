"""실 git 픽스처를 사용자 전역 설정과 분리하는 공용 seam."""
from __future__ import annotations

import os
from collections.abc import Mapping


def commit_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """commit 작성자/커미터 identity를 결정적으로 고정한 환경을 반환한다."""
    env = dict(os.environ if base is None else base)
    env.update({
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x.invalid",
    })
    return env
