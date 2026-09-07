"""upstream 최신 릴리즈 태그 조회 — 엔진의 upstream git seam 으로 `ls-remote --tags` 한 번.

엔진에는 이 축이 없다(도달성 검사뿐)이라 관리자가 새로 만드는 셋 중 하나다. 조회 실패는
upstream 값 단위로 격리한다 — 한 프로젝트의 실패가 다른 행의 마지막 값을 지우지 않는다.

값과 실행은 **엔진의 같은 계약**을 지난다(관리자 쪽 사본 0):

  - 값은 `pm_import.validate_upstream_value` 가 먼저 거른다(scheme allowlist·credential-in-URL·
    transport helper·leading-dash). `classify_upstream` 은 분류일 뿐 허가가 아니라서
    (그 함수 docstring), 분류만으로 git 에 넘기면 엔진이 거부하는 값이 그대로 나간다.
  - git 실행은 `pm_import._real_upstream_git_runner()` 다 — 비대화 auth(`GIT_TERMINAL_PROMPT=0`)·
    global/system config 격리·protocol allowlist·`--` 위치인자 종결이 그 안에 있다.

그 runner 는 fail-soft 다(rc≠0 로 호출부에 위임). 그래서 git 이 답하지 못한 자리는 종류가
하나다 — 브리지 오류 종류 `invalid-path`(화면에서 고칠 값이 `upstream.path` 라는 뜻)이고,
진짜 사유는 git·runner 가 낸 원문이 그대로 나른다(바이너리 부재·상한 초과·저장소 아님).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from . import CallError, ENGINE_LOCK, load_engine_module

# `refs/tags/v<숫자>.<숫자>.<숫자>` 만 후보다 — `wip/*` 같은 태그와 annotated 태그의
# `^{}` peeled 행은 이 정규식에서 탈락한다(peeled 는 이름이 같아 중복 제거이기도 하다).
RELEASE_TAG_REFERENCE = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")
LS_REMOTE_TIMEOUT_SECONDS = 30


def latest_tag(upstream: str) -> str | None:
    """그 upstream 의 최신 릴리즈 태그. 릴리즈 태그가 하나도 없으면 None."""
    # 모듈 로드와 순수 함수 호출만 잠금 안이다 — 네트워크가 나가는 git 호출은 잠금 밖에서
    # 돈다(잠금을 쥔 채 기다리면 등록 프로젝트 전체의 주기 조회가 함께 멈춘다).
    with ENGINE_LOCK:
        engine = load_engine_module("pm_import.py")
        accepted, reason = engine.validate_upstream_value(upstream)
        git = engine._real_upstream_git_runner(timeout=LS_REMOTE_TIMEOUT_SECONDS)
    if not accepted:
        raise CallError("invalid-path", f"upstream 값을 엔진이 거부했다: {reason}")
    returncode, output = git(["ls-remote", "--tags", "--", upstream])
    if returncode != 0:
        raise CallError("invalid-path", f"upstream 을 읽지 못했다: {upstream}",
                        stderr=output, rc=returncode)
    candidates: dict[tuple[int, int, int], str] = {}
    for line in output.splitlines():
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
