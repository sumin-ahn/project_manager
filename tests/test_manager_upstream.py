"""최신 릴리즈 태그 조회 — 실 git 과 실 로컬 bare 로 판정한다 (T-0910 AT-003 · 네트워크 0).

문자열 비교는 `v1.7.9` 를 `v1.7.13` 보다 크게 보므로 숫자 튜플로 비교하고, `wip/*` 같은 태그와
annotated 태그의 `^{}` peeled 행은 후보가 아니다. 같은 upstream 을 가진 프로젝트가 N 개여도
조회는 1회다.

값과 실행이 지나는 자리는 **엔진의 upstream seam** 하나다(관리자 쪽 사본 0) — 값은
`pm_import.validate_upstream_value` 가 먼저 거르고, git 은 `_real_upstream_git_runner` 가
비대화 auth·config 격리·`--` 종결로 부른다. 그 두 축을 실 호출의 argv·env 로 실측한다.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags

from manager import CallError, load_engine_module, upstream

pytestmark = git_required

# 엔진이 형태 안전으로 거부하는 값 — 분류(`classify_upstream`)만으로는 셋 다 git 에 넘어간다.
REFUSED_VALUES = {
    "transport-helper": "ext::sh -c 'touch pm-manager-pwned'",
    "plain-git": "git://example.invalid/framework.git",
    "leading-dash": "--upload-pack=touch pm-manager-pwned",
}


@pytest.fixture(scope="module")
def bare(tmp_path_factory) -> Path:
    return make_bare_with_tags(tmp_path_factory.mktemp("upstream"))


@pytest.fixture
def observed(monkeypatch) -> list[dict]:
    """실행된 git 호출의 argv·env — 실행은 그대로 하고 인자만 관찰한다(결과를 지어내지 않는다)."""
    seen: list[dict] = []
    real_run = subprocess.run

    def observer(argv, **kwargs):
        seen.append({"argv": list(argv), "env": dict(kwargs.get("env") or os.environ)})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", observer)
    return seen


@pytest.fixture
def no_spawn(monkeypatch):
    """스폰 자리의 관찰자 — 불리면 그 자리에서 실패한다(결과를 지어내지 않는다)."""
    load_engine_module("pm_import.py")      # 엔진 모듈 로드는 관찰 대상이 아니다(캐시에 올린다)

    def observer(*args, **kwargs):
        raise AssertionError(f"git 이 떴다: {args!r}")

    monkeypatch.setattr(subprocess, "run", observer)


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


def test_the_same_upstream_is_queried_once(bare, observed):
    results = upstream.latest_tags([str(bare), str(bare), str(bare)])

    assert results == {str(bare): {"tag": "v2.0.0"}}
    assert len(observed) == 1, f"같은 upstream 에 ls-remote 를 {len(observed)}회 불렀다"


# ── 엔진 seam — 값 검증 · 실행 규약 ──────────────────────────────────────────


@pytest.mark.parametrize("value", list(REFUSED_VALUES.values()), ids=list(REFUSED_VALUES))
def test_a_value_the_engine_refuses_never_reaches_git(value, no_spawn):
    """분류 ≠ 허가 — 엔진이 거부하는 값은 서브프로세스를 띄우기 전에 오류 dict 가 된다."""
    with pytest.raises(CallError) as caught:
        upstream.latest_tag(value)

    assert caught.value.kind == "invalid-path"
    assert caught.value.rc is None                       # git 을 부른 적이 없다
    assert caught.value.message.strip(), "엔진이 말한 거부 사유가 버려졌다"


def test_a_refused_value_does_not_erase_the_other_upstreams(bare, tmp_path):
    """값 단위 격리는 그대로다 — 거부된 값 하나가 정상 값의 태그를 지우지 않는다."""
    results = upstream.latest_tags([str(bare), REFUSED_VALUES["plain-git"]])

    assert results[str(bare)] == {"tag": "v2.0.0"}
    assert results[REFUSED_VALUES["plain-git"]]["error"]["kind"] == "invalid-path"


def test_the_git_call_goes_through_the_engine_upstream_runner(bare, observed):
    """정상 값은 그대로 조회되고, 그 실행이 엔진 runner 의 비대화 env·`--` 종결을 지난다."""
    assert upstream.latest_tag(str(bare)) == "v2.0.0"

    argv, environment = observed[0]["argv"], observed[0]["env"]
    assert Path(argv[0]).name.startswith("git"), argv       # 절대경로 git 바이너리
    assert argv[1:] == ["ls-remote", "--tags", "--", str(bare)]
    assert environment["GIT_TERMINAL_PROMPT"] == "0"        # 자격증명 프롬프트로 멈추지 않는다
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert "GIT_ALLOW_PROTOCOL" not in environment
    injected = {environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(environment["GIT_CONFIG_COUNT"]))}
    assert injected["credential.helper"] == ""
    assert injected["protocol.allow"] == "never"


def test_a_url_upstream_is_read_the_same_way(bare, observed):
    """URL 값도 막히지 않는다 — allowlist 안 scheme(`file://`)의 실 조회로 잰다(네트워크 0)."""
    assert upstream.latest_tag(bare.as_uri()) == "v2.0.0"

    assert observed[0]["argv"][1:] == ["ls-remote", "--tags", "--", bare.as_uri()]


# ── 실패 ─────────────────────────────────────────────────────────────────────


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


def test_a_missing_git_binary_is_reported_in_the_engines_own_words(bare, monkeypatch):
    """runner 는 fail-soft 다 — 바이너리 부재도 rc≠0 하나이고 사유는 그 원문이 나른다."""
    monkeypatch.setenv("PATH", "")

    with pytest.raises(CallError) as caught:
        upstream.latest_tag(str(bare))

    assert caught.value.kind == "invalid-path"
    assert caught.value.rc == 1
    assert "git 바이너리" in caught.value.stderr
