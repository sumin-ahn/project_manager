"""최신 릴리즈 태그 조회 — 실 git 과 실 로컬 bare 로 판정한다 (T-0910 AT-003 · 네트워크 0).

문자열 비교는 `v1.7.9` 를 `v1.7.13` 보다 크게 보므로 숫자 튜플로 비교하고, `wip/*` 같은 태그와
annotated 태그의 `^{}` peeled 행은 후보가 아니다. 같은 upstream 을 가진 프로젝트가 N 개여도
조회는 1회다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags

from manager import CallError, upstream

pytestmark = git_required


@pytest.fixture(scope="module")
def bare(tmp_path_factory) -> Path:
    return make_bare_with_tags(tmp_path_factory.mktemp("upstream"))


def test_the_newest_release_tag_wins_by_numbers(bare):
    listing = subprocess.run(["git", "ls-remote", "--tags", str(bare)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    assert "refs/tags/v2.0.0^{}" in listing.stdout, "annotated peeled 행이 없으면 이 판정이 헐거워진다"
    assert "refs/tags/wip/x" in listing.stdout

    assert upstream.latest_tag(str(bare)) == "v2.0.0"


def test_a_repository_without_release_tags_has_no_latest(tmp_path):
    empty = make_bare_with_tags(tmp_path / "wip-only", tags=("wip/x",))
    subprocess.run(["git", "-C", str(empty), "tag", "-d", "v2.0.0"], capture_output=True,
                   text=True, encoding="utf-8", check=True)

    assert upstream.latest_tag(str(empty)) is None


def test_the_same_upstream_is_queried_once(bare, monkeypatch):
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counted(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(upstream.subprocess, "run", counted)

    results = upstream.latest_tags([str(bare), str(bare), str(bare)])

    assert results == {str(bare): {"tag": "v2.0.0"}}
    assert len(calls) == 1, f"같은 upstream 에 ls-remote 를 {len(calls)}회 불렀다"


def test_an_unreadable_upstream_is_reported_with_its_stderr(tmp_path):
    with pytest.raises(CallError) as caught:
        upstream.latest_tag(str(tmp_path / "not-a-repository"))

    assert caught.value.kind == "invalid-path"
    assert caught.value.rc != 0
    assert caught.value.stderr.strip(), "git 이 말한 사유가 버려졌다"


def test_one_broken_upstream_does_not_erase_the_others(bare, tmp_path):
    broken = str(tmp_path / "not-a-repository")

    results = upstream.latest_tags([str(bare), broken])

    assert results[str(bare)] == {"tag": "v2.0.0"}
    assert results[broken]["error"]["kind"] == "invalid-path"


def test_a_missing_git_binary_is_a_spawn_failure(bare, monkeypatch):
    monkeypatch.setenv("PATH", "")

    with pytest.raises(CallError) as caught:
        upstream.latest_tag(str(bare))

    assert caught.value.kind == "spawn"
