"""git 저장소 조회 (층: infra).

`git rev-parse`·설정값·alias·명령 목록·미커밋 여부 조회. 판정 규칙은 `pm_engine.domain.git_policy` 가 소유한다.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from pm_engine.domain import git_policy
import re
from pm_engine.domain import worktree_records
from pm_engine.infra import pm_paths


def _git_repo_root(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    root = _git_rev_parse(anchor, "--show-toplevel", runner=runner)
    return Path(root).resolve() if root else None


def _shared_repository_root(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    """`anchor` 가 PM 홈 공용 저장소(bare `<PM 홈>/.repos/<repo>.git`) 안이면 그 저장소 경로.

    bare 저장소는 작업 폴더가 없어 ``--show-toplevel`` 이 실패하므로 git-dir 로 따로 해소한다.
    형상 규칙은 슬롯 소유 유도(`pm_paths.owning_pm_home`)와 같다 — `.repos` 아래 `.git` 접미
    디렉터리이고 그 위가 `.project_manager` 를 가진 PM 홈이어야 한다.
    """
    answer = _git_rev_parse(anchor, "--is-bare-repository", "--absolute-git-dir", runner=runner)
    lines = answer.splitlines() if answer else []
    if len(lines) != 2 or lines[0] != "true":
        return None
    git_dir = Path(lines[1]).resolve()
    if git_dir.suffix != ".git" or git_dir.parent.name != ".repos":
        return None
    return git_dir if (git_dir.parent.parent / ".project_manager").is_dir() else None


def _git_commit_exists(anchor: Path, name: str, *, runner: Any) -> bool:
    return _git_rev_parse(
        anchor, "--verify", "--quiet", f"{name}^{{commit}}", runner=runner,
    ) is not None


def _git_local_branch_exists(anchor: Path, name: str, *, runner: Any) -> bool:
    return _git_rev_parse(
        anchor, "--verify", "--quiet", f"refs/heads/{name}", runner=runner,
    ) is not None


# `git config --get` 의 "키 없음" 종료 코드. 그 밖의 실패(설정 파일 손상 등)는 판정 실패로 올린다.
_GIT_CONFIG_KEY_MISSING_RC = 1


def _git_alias_value(invocation: git_policy._GitInvocation, *, runner: Any) -> str | None:
    config = [part for value in invocation.config for part in ("-c", value)]
    result = runner(
        ["git", "-C", str(invocation.cwd), *config, "config", "--get",
         f"alias.{invocation.subcommand}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode == _GIT_CONFIG_KEY_MISSING_RC:
        return None
    if result.returncode != 0:
        raise RuntimeError(
            f"git alias 조회 실패(rc={result.returncode}): {(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip() or None


def _git_command_names(anchor: Path, *, runner: Any) -> frozenset[str]:
    """git 명령 이름(내장 + PATH 의 ``git-*``). alias 가 이 이름을 가리면 git 은 alias 를 무시한다."""
    result = runner(
        ["git", "-C", str(anchor), "--list-cmds=main,others"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return frozenset((result.stdout or "").split())


def _git_path_uncommitted(
    anchor: Path, relative: Path, *, runner: Any = subprocess.run
) -> bool | None:
    """`anchor` 저장소에서 `relative` 파일이 **미커밋** 상태인지 (git 조회 1회).

    `git status --porcelain --ignored -- <path>` 의 출력이 있으면 그 사본은 커밋으로 확정되지
    않았다(작업트리 변경·스테이지·미추적·무시됨). git 아님/오류면 None — **판정 불가**이며
    호출부는 이를 단정으로 메우지 않는다.
    """
    try:
        result = runner(
            ["git", "-C", str(anchor), "status", "--porcelain", "--ignored",
             "--", relative.as_posix()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return bool((result.stdout or "").strip())


def _git_rev_parse(anchor: Path, *args: str, runner: Any = subprocess.run) -> str | None:
    """`git -C <anchor> rev-parse <args>` 결과(strip)를 반환. git 아님/오류/빈 값이면 None
    (fail-soft — standalone·비-git 트리 무영향). `runner` 는 hermetic 테스트 주입 seam."""
    try:
        r = runner(["git", "-C", str(anchor), "rev-parse", *args],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _is_linked_worktree(anchor: Path, *, runner: Any = subprocess.run) -> bool:
    """`anchor` 가 linked git worktree(main checkout 아님)인가.

    linked worktree 는 `git rev-parse --git-dir` 가 `<common>/worktrees/<name>` 을 가리켜
    `--git-common-dir`(=`<common>`)와 **다르다**. 일반 checkout·PM 홈은 둘 다 `.git` 로 동일
    (→ False). git 아님/오류면 False(fail-soft — standalone 무영향).
    """
    git_dir = _git_rev_parse(anchor, "--git-dir", runner=runner)
    common_dir = _git_rev_parse(anchor, "--git-common-dir", runner=runner)
    if git_dir is None or common_dir is None:
        return False

    def _abs(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (anchor / pp).resolve()

    return _abs(git_dir) != _abs(common_dir)


def _git_head_at(cwd: str) -> str:
    """주어진 작업 디렉토리의 git HEAD sha 를 반환한다 (실패/비-repo 면 '').

    `_git_head` 는 Home 루트(board 프로세스의 `REPO`) 기준이지만, livegate 기록은 테스트가 실제로
    돈 **활성 slot worktree**(=`_livegate_cwd`)의 HEAD 를 키로 삼아야 한다 — 보호훅이
    push 하는 sha(=worktree HEAD)와 대조되기 때문. 이 함수가 그 cwd-매개 HEAD 를 낸다.
    """
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


# board git 호출 timeout — pull/push 는 네트워크 왕복이라 user-email 폴백(5s)보다 길게
# 둔다. 환경 이상(hang·offline DNS)에서 무한 대기를 막는 상한(엔진 subprocess 관례).
_BOARD_GIT_TIMEOUT_SECONDS = 30


def _cluster_current_branch(tree: str) -> str | None:
    """코드 트리의 현재 브랜치 — detached·비-git·git 부재는 None."""
    result = _git_run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo=Path(tree))
    if result is None or result[0] != 0:
        return None
    return (result[1] or "").strip() or None


def _git_run(argv: list[str], *, runner=None, repo: Path) -> tuple[int, str] | None:
    """git 명령의 `(rc, stdout)` 반환·git 부재/예외 → None. runner seam(hermetic 테스트)·미주입
    시 실 subprocess(`git -C repo`). rev-parse 해소 OID(prefix 검증용)가 필요한 곳이 쓴다."""
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo)
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *argv],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):  # 타임아웃/바이너리 이상은 None(환경적).
            return None
        return r.returncode, r.stdout or ""
    try:
        rc, out = runner(argv)
    except Exception:  # noqa: BLE001 — 주입 runner raise 도 None.
        return None
    return rc, out


def _git_head(home) -> str:
    return _git_head_at(str(home.root))


def _git_config_get(cwd: str, key: str) -> str | None:
    """`git -C <cwd> config --get <key>` 값 (미설정/비-repo/빈값이면 None).

    인코딩 명시(git path 출력이 cp949 로 디코딩되지 않도록 utf-8·`_hooks_dir` 동형).
    """
    r = subprocess.run(["git", "-C", cwd, "config", "--get", key],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    val = r.stdout.strip() if r.returncode == 0 else ""
    return val or None


GIT_TIMEOUT_SECONDS = 600   # clone 은 네트워크 — 부트스트랩/worktree git 보다 넉넉히.


def _real_clone_runner() -> worktree_records.GitRunner:
    """`git clone ...` 을 실행하는 GitRunner (fail-soft). argv 는 clone 인자 그대로.

    clone 은 `-C <dir>` 가 아니라 `git clone <url> <dest>` 형태라 별도 runner 로 둔다.
    git 바이너리 부재(shutil.which)·예외는 (1, stderr-or-"") 로 감싼다 — 호출부가 rc 로
    판정. 인코딩은 엔진 관례대로 UTF-8(한글 경로·메시지 안전).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부에 위임.
            return 1, str(exc)

    return runner


def _git_remote_identity(value: str) -> tuple[str, str, int | None, str]:
    """Git remote를 transport class + endpoint/path identity로 정규화한다.

    판정 축(코드·테스트 공통 표):

    ======================  ================================================
    입력 축                 identity 규칙
    ======================  ================================================
    ssh/https/scp           같은 ``network`` class로 정규화
    file://                 ``file`` class를 보존(network과 절대 동일 아님)
    user, host case         user 제거, host 소문자화
    host / port             독립 tuple 필드(IPv6 콜론과 port 경계 보존)
    default port            ssh:22/https:443은 None; non-default는 int로 보존
    path                    선행/후행 slash·마지막 ``.git`` 정규화
    ======================  ================================================

    따라서 ``ssh://host:22/a/b``·``git@host:a/b.git``·``https://host/a/b``는
    같지만, ``file://host/a/b``는 endpoint 문자열이 같아도 다른 identity다.
    """
    raw = value.strip().rstrip("/")
    # SCP-like IPv6는 `user@[::1]:path`처럼 host의 콜론을 대괄호로 감싼다. 일반
    # `host:path`와 별도 패턴으로 먼저 분리해야 `::1` 일부가 path로 새지 않는다.
    scp = None
    if "://" not in raw:
        scp = re.match(r"^(?:[^@/]+@)?\[([^\]]+)\]:(.+)$", raw)
        if scp is None:
            scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", raw)
    if scp is not None:
        host, path = scp.groups()
        normalized_path = path.removesuffix(".git").strip("/")
        return ("network", host.lower(), None, normalized_path)
    if "://" in raw:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        normalized_path = parsed.path.removesuffix(".git").strip("/")
        try:
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            # 잘못된 authority를 유사 remote로 잘못 합치지 않는 안전 방향 폴백.
            authority = parsed.netloc.rsplit("@", 1)[-1].lower()
            return (f"invalid-{scheme}", authority, None, normalized_path)
        default_port = {"ssh": 22, "https": 443}.get(scheme)
        normalized_port = None if port is None or port == default_port else port
        transport = "network" if scheme in {"ssh", "https"} else scheme
        # host와 port를 tuple의 독립 필드로 둔다. 문자열 `host:port` 결합은 IPv6
        # host 자체의 콜론과 경계가 사라져 `[::1]:2222`와 `[::1:2222]`를 합친다.
        return (transport, host, normalized_port, normalized_path)
    return ("path", "", None, raw.removesuffix(".git"))


def _bare_missing_branches(home,
    repo: str,
    branches: list[str],
    *,
    clone_runner: worktree_records.GitRunner | None = None,
    repos_dir: Path | None = None,
) -> list[str]:
    """`branches` 중 `.repos/<repo>.git` 에 없는 것들 (경고 1줄용·검증 아님).

    보호목록은 **브랜치 실재를 요구하지 않는다**(아직 없는 `release` 를 미리 보호하는 게 정상).
    그래서 거부하지 않고 *경고*만 낸다 — 오타(`mian`)를 조용히 흘리지 않기 위한 가시화다.
    bare 부재(clone 전)·git 실패는 빈 리스트(fail-soft·경고 생략).

    **bare 조회 자체가 실패하면 경고를 통째로 생략한다** — git 바이너리 부재/권한 등으로 모든
    `show-ref` 가 rc≠0 이 되면 멀쩡한 브랜치까지 "없다"고 오탐한다. 먼저 `rev-parse
    --is-bare-repository` 로 bare 를 프로브해 rc≠0 이면 빈 리스트(경고의 목적은 오타 포착이지
    차단이 아니다).
    """
    bare = (repos_dir if repos_dir is not None else pm_paths.repos_dir(home)) / f"{repo}.git"
    if not branches or not bare.exists():
        return []
    runner = clone_runner or _real_clone_runner()
    try:
        probe_rc, _probe = runner(
            ["-C", str(bare), "rev-parse", "--is-bare-repository"])
        if probe_rc != 0:
            return []   # bare 조회 불가(git 부재/깨진 bare) — 오탐 방지로 경고 생략.
        missing: list[str] = []
        for branch in branches:
            rc, _out = runner(
                ["-C", str(bare), "show-ref", "--verify", "--quiet",
                 f"refs/heads/{branch}"])
            if rc != 0:
                missing.append(branch)
    except Exception:  # noqa: BLE001 — fail-soft: 경고는 부가 정보(판정 실패=생략).
        return []
    return missing
