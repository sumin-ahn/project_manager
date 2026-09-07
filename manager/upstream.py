"""upstream 최신 릴리즈 태그 조회 — `git ls-remote --tags <url|경로>` 하나.

엔진에는 이 축이 없다(도달성 검사뿐)이라 관리자가 새로 만드는 셋 중 하나다. 조회 실패는
upstream 값 단위로 격리한다 — 한 프로젝트의 실패가 다른 행의 마지막 값을 지우지 않는다.

`git` 이 rc≠0 로 답한 경우(도달 못 함·저장소 아님)는 브리지 오류 종류 `invalid-path` 다:
화면에서 고칠 값이 `upstream.path` 라는 뜻이고, 진짜 사유는 stderr 원문이 그대로 나른다.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable

from . import CallError

# `refs/tags/v<숫자>.<숫자>.<숫자>` 만 후보다 — `wip/*` 같은 태그와 annotated 태그의
# `^{}` peeled 행은 이 정규식에서 탈락한다(peeled 는 이름이 같아 중복 제거이기도 하다).
RELEASE_TAG_REFERENCE = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")
LS_REMOTE_TIMEOUT_SECONDS = 30


def latest_tag(upstream: str) -> str | None:
    """그 upstream 의 최신 릴리즈 태그. 릴리즈 태그가 하나도 없으면 None."""
    argv = ["git", "ls-remote", "--tags", upstream]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=LS_REMOTE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CallError("timeout",
                        f"{LS_REMOTE_TIMEOUT_SECONDS}초 안에 upstream 응답이 없다: {upstream}") from exc
    except OSError as exc:
        raise CallError("spawn", f"git 을 실행하지 못했다: {exc}") from exc
    if completed.returncode != 0:
        raise CallError("invalid-path", f"upstream 을 읽지 못했다: {upstream}",
                        stderr=completed.stderr, rc=completed.returncode)
    candidates: dict[tuple[int, int, int], str] = {}
    for line in completed.stdout.splitlines():
        _, _, reference = line.partition("\t")
        matched = RELEASE_TAG_REFERENCE.match(reference.strip())
        if matched:
            # 비교는 숫자 튜플이다 — 문자열 비교는 v1.7.9 를 v1.7.13 보다 크게 본다.
            candidates[tuple(int(part) for part in matched.groups())] = reference.strip()[len("refs/tags/"):]
    if not candidates:
        return None
    return candidates[max(candidates)]


def latest_tags(upstreams: Iterable[str]) -> dict[str, dict]:
    """upstream 값별 조회 결과 — 같은 값을 가진 프로젝트 N 개는 조회 1회다."""
    results: dict[str, dict] = {}
    for value in dict.fromkeys(upstreams):
        try:
            results[value] = {"tag": latest_tag(value)}
        except CallError as exc:
            results[value] = exc.as_error_dict()
    return results
