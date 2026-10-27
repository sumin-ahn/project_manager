"""대상 Home의 repo 보호 정책·설정·설치 결과 보고 (층: app)."""
from __future__ import annotations

import sys
from pathlib import Path

from pm_engine.domain import areas_rules, upstream_rules, worktree_records
from pm_engine.infra import (areas_store, branch_store, conf_store, git, pm_paths,
                             protected_hooks, python_env)


def _local_conf_test_cmd(home) -> str | None:
    """Home.local_conf의 test.cmd를 공용 설정 로더로 읽는다."""
    return _local_conf_value(home, "test.cmd")


_PROTECTED_GATE_RELEASE = "release"
_PROTECTED_GATE_SELF_TEST = "self-test"


def _local_conf_value(home, key: str) -> str | None:
    """`local.conf` 한 키의 마지막 값 (부재/OSError → None).

    `conf_store.local_config()`와 같은 **last-wins**다. 중복 키의 마지막 값이
    비어 있으면 앞의 값을 해제한 것으로 보아 `""`를 반환한다. 동일 파일을
    `upstream show`와 보호 push gate가 다르게 해석하지 않게 하는 계약이다.
    """
    conf_file = home.local_conf
    return conf_store.load_checked(conf_file).get(key)


def _repo_registry_git(home, repo: str) -> str | None:
    """동일 repo 행에서 처음 나타나는 비어 있지 않은 git 값을 읽는다."""
    _header, rows = areas_store._parse_areas(home)
    for row in rows:
        if row.get("repo") == repo and row.get("git"):
            return row["git"]
    return None


def _protected_push_gate_config(home,
    repo: str, *, report_downgrade: bool = True,
) -> tuple[str, str]:
    """repo 보호 push의 증거 계약 `(mode, self_test_cmd)`를 provenance로 해소한다.

    PM 홈 `local.conf upstream.path=`이 이 홈의 canonical `work/<repo>_<N>` 슬롯 자체거나,
    URL identity가 areas의 repo remote와 같으면 프레임워크 자기 repo라 기존 release
    livegate를 유지한다. 다른 repo면 areas `test_cmd`(없으면 local.conf/default)로 자기 검증한다.
    upstream/registry provenance를 판별할 수 없으면 사용자 통제 밖 조건으로 영구 차단하지
    않고 adopter `self-test`로 강등한다. 증거 명령 자체는 그대로 fail-closed다.
    """
    test_cmd = _resolve_repo_test_cmd(home, repo)
    upstream = _local_conf_value(home, "upstream.path")
    if not upstream:
        if report_downgrade:
            print(
                f"⚠ 보호 push gate 강등({repo}): local.conf upstream.path 축 미해소 — "
                "release 대신 self-test를 사용한다; upstream을 설정하라.",
                file=sys.stderr,
            )
        return _PROTECTED_GATE_SELF_TEST, test_cmd

    # URL/scp-like upstream 판별은 upstream_rules.classify_upstream 단일 진실을 쓴다. 여기서 정규식을
    # 따로 들면 `host:path` 같은 지원 형식이 경로로 갈라져 release gate가 빠진다.
    kind = upstream_rules.classify_upstream(upstream)
    if kind == "url":
        repo_git = _repo_registry_git(home, repo)
        if not repo_git:
            if report_downgrade:
                print(
                    f"⚠ 보호 push gate 강등({repo}): areas.md git 축 미해소 — "
                    "release 대신 self-test를 사용한다; 해당 repo의 git URL을 등록하라.",
                    file=sys.stderr,
                )
            return _PROTECTED_GATE_SELF_TEST, test_cmd
        mode = (_PROTECTED_GATE_RELEASE
                if git._git_remote_identity(upstream) == git._git_remote_identity(repo_git)
                else _PROTECTED_GATE_SELF_TEST)
        return mode, test_cmd

    # 경로 해소 실패는 **어떤 값이** 안 풀렸는지와 함께 올린다 — 플랫폼마다 문구가 달라(POSIX
    # "embedded null byte" / Windows "stat: embedded null character in path") 입력을 안 실으면
    # 전파된 traceback 에 진단이 남지 않는다.
    try:
        candidate = pm_paths._expanded_user_path(upstream)
        if not candidate.is_absolute():
            candidate = home.root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"upstream 경로 해소 실패({upstream}) — {type(exc).__name__}: {exc}") from exc
    work_root = (home.root / "work").resolve()
    name, sep, slot_no = candidate.name.rpartition("_")
    if candidate.parent == work_root and sep and name == repo and slot_no.isdigit():
        return _PROTECTED_GATE_RELEASE, test_cmd
    return _PROTECTED_GATE_SELF_TEST, test_cmd


def _default_test_cmd(home) -> str:
    """worktree add 빌드명령 프롬프트의 최종 폴백값 — local.conf 또는 `python_env` 기본.

    regression._test_cmd 의 최종 폴백 레이어(`local_config().get("test.cmd") or "pytest -q"`)와
    동형. `_resolve_repo_test_cmd` 의 마지막 레이어(areas 미등록·빈 값일 때)다 —
    프롬프트 표시값 resolve 의 폴백.
    """
    configured = _local_conf_test_cmd(home)
    if configured:
        return configured
    return python_env.default_pytest_cmd()


def _resolve_repo_test_cmd(home, repo: str) -> str:
    """프롬프트 `[기본: <X>]` 의 X — 그 repo 가 Enter 시 실제로 폴백할 test_cmd.

    regression._test_cmd 의 해소 체인을 *그 repo 한정으로* 재현한다 (areas 파서
    `areas_store._parse_areas`/`_areas_row_for_prefix` 재사용):
      1. **활성 repo 의 areas.md test_cmd** — 그 repo(=prefix)의 레지스트리 행에 비어
         있지 않은 `test_cmd` 가 있으면 그것(per-repo 스택·`go test ./...` 등).
      2. **최종 폴백** — areas 미등록·빈 값이면 `local.conf test.cmd` 또는 `pytest -q`.
    (활성 슬롯 레이어는 새 슬롯 생성 *전* 시점이라 표시에 무의미 — 생략.) 빈입력(Enter) 시 슬롯에 안 박고(None) 이 체인으로 폴백함을 투명하게
    보여주는 게 목적이다(must-fix 1 — 슬롯이 areas 보다 우선이라 잘못 덮으면 안 됨).
    """
    _header, rows = areas_store._parse_areas(home)
    for row in rows:
        if row.get("repo") == repo and row.get("test_cmd"):
            return row["test_cmd"]
    # 구 3칼럼 registry에는 repo 칼럼이 없으므로 prefix lookup을 하위호환으로 유지한다.
    row = areas_store._areas_row_for_prefix(home, repo)
    if row and row.get("test_cmd"):
        return row["test_cmd"]
    return _default_test_cmd(home)


# `_install_protected_hook` 는 repo add/worktree add를 깨지 않기 위해 bool fail-soft를
# 유지한다. 단, 그 과정에서 raw 예외 원인을 잃으면 bootstrap/reporting이 모든
# 실패를 "bare 부재" 로 오도한다. CLI는 단일 process에서 순차 설치하므로 repo별
# 최신 실패 원인을 함께 기록하고, 공개 helper로 동일 깔때기의 호출자가 소비한다.
_PROTECTED_HOOK_FAILURE_REASONS: dict[tuple, str] = {}


def protected_hook_install_failure_reason(home, repo: str) -> str | None:
    return _PROTECTED_HOOK_FAILURE_REASONS.get((home, repo))


def _install_protected_hook(home, repo: str) -> bool:
    """보호 훅을 설치하고 Home·repo별 최근 실패 사유를 보관한다."""
    _PROTECTED_HOOK_FAILURE_REASONS.pop((home, repo), None)
    protected = areas_store._repo_protected(home, repo)
    mode, test_cmd = _protected_push_gate_config(home, repo)
    installed = bool(protected_hooks.install_protected_hook(
        home, repo, protected, gate_mode=mode, test_cmd=test_cmd))
    if not installed:
        _PROTECTED_HOOK_FAILURE_REASONS[(home, repo)] = (
            "install_protected_hook이 False를 반환(bare 미러 부재 또는 "
            "bare core.hooksPath 설정 실패)")
    return installed


def _areas_protected_cell(home, repo: str) -> str | None:
    """areas.md 그 repo 행의 **raw** `protected` 셀 — 행 부재 → None (출처 판정용).

    `areas_store._repo_protected`(폴백 적용된 실효값)와 달리 "명시했나 / 비었나 / 행이 없나"를
    구별해야 조회 출력의 **출처** 줄을 정직하게 낼 수 있다. 행 부재 → None, 파싱 예외는 전파.
    """
    _header, rows = areas_store._parse_areas(home)
    for row in rows:
        if row.get("repo") == repo:
            return row.get("protected") or ""
    return None


def _warn_missing_protected_branches(home,
    repo: str,
    branches: list[str],
    *,
    clone_runner: worktree_records.GitRunner | None = None,
    repos_dir: Path | None = None,
) -> list[str]:
    """bare 에 없는 보호 브랜치를 경고 1줄로 낸다 — **양 경로 공용 깔때기**.

    `repo add --protected` (신규 등록)와 `repo protected <repo> <목록>` (사후 설정)은 **같은**
    보호목록을 같은 문법으로 받는다 — 한쪽에만 오타 가시화가 있으면 `--protected mian` 이 기본
    보호목록(main/master/develop)을 덮으면서 조용히 통과해 **보호 가드가 실질적으로 꺼진다**.
    그래서 두 경로가 이 함수 하나를 탄다(검증 비대칭 클래스 폐쇄).

    거부가 아니라 경고다. 반환 = 없는 브랜치 목록.
    """
    missing = git._bare_missing_branches(home,
        repo, branches, clone_runner=clone_runner, repos_dir=repos_dir)
    if missing:
        print(
            f"[경고] `.repos/{repo}.git` 에 없는 브랜치를 보호목록에 넣었다: "
            f"{', '.join(missing)} — 아직 없는 브랜치를 미리 보호하는 것은 정상이지만 "
            "오타가 아닌지 확인하라(검증하지 않음·"
            f"수정은 `{_FACADE_PROG} repo protected {repo} <목록>`).",
            file=sys.stderr,
        )
    return missing


def protected_retry_command(home, repo: str) -> str:
    """그 repo 의 보호목록 **재실행 안내 커맨드** — 현재 상태(명시/폴백/미등록) 단일 분기.

    안내는 *실행하면 지금 상태를 그대로 복원*해야 한다. 그런데 상태에 따라 정확한 커맨드가 다르다:

      - **기본값 폴백**(areas `protected` 칼럼이 빈 값) → `repo protected <repo> default`.
        여기서 `main,master,develop` 을 **명시 커맨드로 안내하면 안 된다** — 사용자가 그대로
        실행하면 동작은 같아도 raw cell 의 출처가 "기본값 폴백" → "명시 설정" 으로 바뀐다
        (안내가 상태를 조용히 바꾼다). 이후 DEFAULT_PROTECTED 가 바뀌어도 안 따라간다.
      - **명시 설정** → `repo protected <repo> <현재 목록>`(그 값 그대로 재적용·멱등).
      - **미등록**(areas 에 행 없음) → 셀을 고칠 대상이 없으니 `repo add <repo>` 가 먼저다.

    이 분기를 **여기 한 곳**에만 둔다 — 조회의 drift/미배선 줄·설정 실패 경고·bootstrap reconcile
    실패 경고가 전부 이 함수를 소비한다.
    pm_bootstrap은 이 모듈의 함수를 직접 호출한다.
    """
    cell = _areas_protected_cell(home, repo)
    if cell is None:
        return f"{_FACADE_PROG} repo add {repo}"
    if not cell.strip():
        return f"{_FACADE_PROG} repo protected {repo} {areas_rules._PROTECTED_DEFAULT_LITERAL}"
    listed = ",".join(t.strip() for t in cell.split(",") if t.strip())
    return f"{_FACADE_PROG} repo protected {repo} {listed}"


def _install_protected_hook_reporting(home,
    repo: str,
    *,
    action: str,
    retry: str | None = None,
) -> bool:
    """훅 sidecar (재)설치 + **결과 보고 단일 깔때기** (성공도 실패도 조용하지 않다).

    `_install_protected_hook` 은 실패를 **예외가 아니라 False** 로 돌려준다(bare 부재·
    `core.hooksPath` 설정 실패). 호출부마다 `if ...: print(성공)` 만 쓰면 실패가 전부
    침묵한다 — 훅이 안 걸렸는데 사용자는 걸린 줄 안다(보호 가드 침묵 무력화). 성공은 `✓` 1줄,
    실패는 **stderr 경고 + 재실행 커맨드**로 낸다.

    `action` = 사람이 읽는 맥락("설치"·"(재)설치"). `retry` = 재실행 안내 커맨드 — 보호목록을
    *바꾸는* 경로는 `protected_retry_command`(상태 인지 단일 분기)를 넘기고, 등록/슬롯 생성 경로는
    생략해 방금 친 멱등 커맨드(`repo add <repo>`)를 안내한다. 반환 = 설치 성공 여부(호출부는 rc 를
    바꾸지 않는다 — 보호 훅은 추가 가드).
    """
    if _install_protected_hook(home, repo):
        print(f"✓ 보호 브랜치 pre-push + pre-commit 훅 {action}: {repo}.")
        return True
    retry = retry or f"{_FACADE_PROG} repo add {repo}"
    reason = protected_hook_install_failure_reason(home, repo) or "원인 미해소"
    print(
        f"[경고] 보호 브랜치 훅 sidecar 를 {action}하지 못했다: {repo} — {reason}. 이 clone 의 "
        "pre-push·pre-commit 훅은 보호목록을 강제하지 않거나 옛 목록으로 동작할 수 있다.\n"
        f"  → 재실행(멱등): {retry}",
        file=sys.stderr,
    )
    return False


def _refresh_protected_gate_contracts(home) -> tuple[list[str], list[str]]:
    repos = sorted(areas_store.registered_repos(home))
    attempted, failed = [], []
    for repo in repos:
        attempted.append(repo)
        if not _install_protected_hook_reporting(home, repo, action="gate 계약으로 (재)설치"):
            failed.append(repo)
    return attempted, failed


# 사용자 안내와 CLI prog에 쓰는 파사드 이름.
_FACADE_PROG = "pm-config"
