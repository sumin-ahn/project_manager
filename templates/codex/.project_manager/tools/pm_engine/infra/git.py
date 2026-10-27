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

from collections.abc import Callable
from collections.abc import Sequence


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
    """git 명령의 `(rc, stdout)` 반환·git 부재(PATH 에 없음) → None. runner seam(hermetic 테스트)·미주입
    시 실 subprocess(`git -C repo`). 실행 예외(시간 초과 등)는 전파한다. rev-parse 해소 OID(prefix
    검증용)가 필요한 곳이 쓴다."""
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo)
        r = subprocess.run(
            ["git", "-C", str(repo), *argv],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
        return r.returncode, r.stdout or ""
    rc, out = runner(argv)
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
    """`git clone ...` 을 실행하는 GitRunner. argv 는 clone 인자 그대로.

    clone 은 `-C <dir>` 가 아니라 `git clone <url> <dest>` 형태라 별도 runner 로 둔다.
    git 바이너리 부재(shutil.which)는 (1, 사유) 로 돌려 호출부가 rc 로 판정한다. 실행 예외
    (시간 초과 등)는 전파한다. 인코딩은 엔진 관례대로 UTF-8(한글 경로·메시지 안전).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        result = subprocess.run(
            [git_binary, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")

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
    bare 부재(clone 전)·git rc≠0 은 빈 리스트(경고 생략), runner 예외는 전파한다.

    **bare 조회 자체가 실패하면 경고를 통째로 생략한다** — git 바이너리 부재/권한 등으로 모든
    `show-ref` 가 rc≠0 이 되면 멀쩡한 브랜치까지 "없다"고 오탐한다. 먼저 `rev-parse
    --is-bare-repository` 로 bare 를 프로브해 rc≠0 이면 빈 리스트(경고의 목적은 오타 포착이지
    차단이 아니다).
    """
    bare = (repos_dir if repos_dir is not None else pm_paths.repos_dir(home)) / f"{repo}.git"
    if not branches or not bare.exists():
        return []
    runner = clone_runner or _real_clone_runner()
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
    return missing


def _board_git(home, args: list[str], *, check: bool = False,
               timeout: float | None = None) -> subprocess.CompletedProcess:
    """board git working dir(`Home.board_root`)에서 git 명령을 실행한다 (UTF-8·timeout 고정).

    엔진 subprocess 관례: UTF-8 디코딩(한글 ticket/경로 안전)·짧은 timeout·`errors=replace`.
    `-C home.board_root` 로 작업 디렉토리를 board git 으로 고정한다(cwd 의존 0). `check=False`
    가 기본 — 호출부가 returncode 로 분기하며, 예외(timeout·바이너리 이상)는 호출부가
    fail-soft 로 처리한다. `timeout` 미지정은 `_BOARD_GIT_TIMEOUT_SECONDS`(30s) — advisory
    freshness fetch 처럼 대화형 hang 을 짧게 끊어야 하는 호출부만 override 한다(기존
    경로 무변경).
    """
    return subprocess.run(
        ["git", "-C", str(home.board_root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_GIT_TIMEOUT_SECONDS if timeout is None else timeout, check=check)


GitRunner = Callable[[list[str]], tuple[int, str]]


def _git_scope_run(repo: Path, args: list[str]) -> tuple[int, str]:
    """`git -C <repo> …` 기본 러너 → (rc, stdout) — 엔진 subprocess 관례(UTF-8·timeout 고정)."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_GIT_TIMEOUT_SECONDS, check=False)
    return r.returncode, r.stdout


def git_scope_relpath(repo: Path, path: Path) -> str | None:
    """절대 경로 → `<repo>` 루트 기준 상대 pathspec (그 repo 밖이면 None)."""
    try:
        return Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def git_scope_pathspec(repo: Path, paths: Sequence[Path], *,
                       exclude_prefixes: Sequence[str] = ()) -> tuple[str, ...]:
    """이 mutation 이 만진 경로 → `<repo>` pathspec (**스코프 산출 단일 지점**).

    규칙 셋을 한 곳에서만 적용한다(경로별로 다시 판정하는 두 번째 구현을 만들지 않는다):
      1. `repo` **밖** 경로는 제외 — 그 git 이 모르는 파일(예: 두-git 형상에서 코드 worktree
         의 `touches`, board-git 기준의 파생물 `wiki/board.md`).
      2. `exclude_prefixes` 로 시작하는 경로는 제외 — 호출부가 그 repo 고유의 금지 구역을
         준다(board-git 의 `tickets/.drafts/` = 미충전 draft).
      3. 중복 제거(순서 보존).
    반환이 비면 스코프가 없다는 뜻(호출부가 no-op 으로 처리).
    """
    rels: list[str] = []
    for path in paths:
        rel = git_scope_relpath(repo, path)
        if rel is None or rel in rels:
            continue
        if any(rel.startswith(prefix) for prefix in exclude_prefixes):
            continue
        rels.append(rel)
    return tuple(rels)


def _git_scope_nested_git(repo: Path, rel: str) -> bool:
    """`rel` 이 **중첩 git(서브모듈) 안쪽** 경로인가 — 상위 repo 의 `add` 를 fatal 로 죽이는 갈래.

    `git add -A -- .project_manager/board/tickets/…` 는 그 경로가 서브모듈 *내부* 면
    `fatal: Pathspec '…' is in submodule` 로 rc=128 이 되고 **pathspec 전체가 stage 되지
    않는다**(board 분리 형상에서 `touches` 에 board 경로를 적으면 finish 가 통째로 중단됐다).
    조상 디렉토리 중 `.git` 을 가진 것이 있으면(=별도 git) 그 아래는 상위 repo 의 pathspec 이
    아니므로 미리 거른다. 경로 자신은 제외(repo 루트/대상 자체가 git 인 건 정상).
    """
    parent = Path(rel).parent
    while parent != Path("."):
        if (Path(repo) / parent / ".git").exists():
            return True
        parent = parent.parent
    return False


def _git_scope_is_directory(repo: Path, rel: str) -> bool:
    """`rel` 이 디렉토리(또는 디렉토리 표기)인가 — 최종 관문의 판정.

    worktree 실측(`is_dir()`)에 더해 **끝 슬래시 표기**(`newdir/`)도 디렉토리로 본다: git 의
    porcelain 이 접어서 낸 untracked 디렉토리가 그 모양이라, 실측만 하면 이미 지워진 경로가
    빠져나간다. 판정이 애매할 때는 *제외* 가 안전한 방향이다 — 빠지면 잔여 loud 보고에 뜨지만,
    통과하면 남의 파일이 조용히 실린다.
    """
    return rel.endswith("/") or (Path(repo) / rel).is_dir()


def _git_scope_ignored(repo: Path, pathspec: Sequence[str],
                       run: GitRunner) -> set[str]:
    """`.gitignore` 에 걸리는 경로들 (rc 128 판정 불가면 빈 집합 · 러너 예외는 그대로 올린다).

    **`git add` 는 명시 pathspec 이 ignored 면 rc=1 에러다.** 광역 `add -A` 가 ignored 를 조용히
    건너뛰는 것과 다르고, `--ignore-errors` 로도 안 없어진다(reviewer 실측). 그래서 `touches` 에
    gitignored 경로가 하나만 있어도 stage 가 통째로 죽고, 하필 그 순간 잔여 loud 보고까지 함께
    사라진다(기록은 이미 절반 진행된 상태). 실형상 도달 가능 — 이 보드의 done 티켓 여러 건이
    `touches: .project_manager/local.conf`(gitignored)를 선언한다.

    판정은 `git check-ignore` 로 한다:
      - 먼저 **전체를 한 번** 물어(`check-ignore -- <paths>`) rc=1(매치 0)이면 즉시 종료 —
        평시 비용은 subprocess 1회다.
      - 매치가 있을 때만 경로별 `-q` 로 어느 것인지 가른다(`-q` 는 경로 1개만 받고 `-z` 는
        `--stdin` 전용이라, 인용/이스케이프 없는 **rc 판정** 만 쓴다 — 출력 파싱 0).
    **추적 파일은 check-ignore 가 보고하지 않는다**(실측) — 이미 추적 중인 파일은 ignore 규칙과
    무관하게 stage 되어야 하므로 이 동작이 정확히 맞다. rc=128(비-git·판정 불가)은 제외 0 이다.
    """
    if not pathspec:
        return set()
    rc, _out = run(["check-ignore", "--", *pathspec])
    if rc != 0:            # 1 = 매치 0(정상) · 128 = 비-git/판정 불가 → 제외 0.
        return set()
    ignored: set[str] = set()
    for path in pathspec:
        rc_one, _ = run(["check-ignore", "-q", "--", path])
        if rc_one == 0:
            ignored.add(path)
    return ignored


def git_parse_porcelain_z(out: str) -> tuple[tuple[str, str], ...]:
    """`git status --porcelain -z` 출력 → `((XY, 경로), …)` — **NUL 파싱 단일 지점**.

    `-z` 가 **판정용으로 필수**다: `-z` 없이는 git 이 비-ASCII 경로를 8진 이스케이프 + 인용으로
    낸다(`"\\353\\251\\200…"`). 그 문자열을 스코프 비교에 넣으면 *자기가 방금 stage 한 파일* 을
    "스코프 밖" 으로 오판해 "빼라" 고 지시한다(reviewer 실측 — PM 홈에 실제로 한글 티켓/아이디어
    경로가 있다). `core.quotepath=false` 도 공백 경로 인용(`"a b.md"`)이 남아 불충분하다.
    `-z` 는 인용 자체를 하지 않으므로 원문 경로가 그대로 나온다.

    rename/copy(`R`/`C`) 항목은 `<코드> <신규>\\0<원본>\\0` **2토큰**이라 원본 토큰을 소비하고
    신규 경로를 취한다. 스코프 산출(디렉토리 전개)과 잔여 보고가 **이 함수 하나**를 공유한다.
    """
    tokens = out.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in ("R", "C"):
            index += 1          # 원본 경로 토큰 소비 (신규 경로만 취한다)
        if path:
            entries.append((code, path))
    return tuple(entries)


def _git_scope_expand_dir(repo: Path, rel: str, run: GitRunner) -> list[str]:
    """디렉토리 선언 → 그 아래 **변경된 파일** 경로들 (`status --porcelain -z -- <dir>`).

    디렉토리가 **이미 삭제된** 경우도 같은 조회로 덮인다 — git 은 index 를 함께 보므로 삭제된
    파일이 ` D <경로>` 로 나온다(실측). 그래서 "지워진 디렉토리 선언" 을 위한 두 번째 경로를
    만들지 않는다.

    디렉토리를 그대로 pathspec 에 넘기면 `git add -A -- <dir>` 가 그 아래 **남의 미완성
    편집까지 통째로** stage 한다 — 좁힌 척하고 안 좁힌 상태다. 그래서
    스코프에서 디렉토리는 **살아남지 못한다**: 여기서 변경 파일 목록으로 펼쳐, 호출부가
    무엇이 실리는지 **파일 단위로 출력·검증**할 수 있게 한다(변경이 없으면 빈 목록 → 소멸).

    `--untracked-files=all` 이 **load-bearing** 이다: 기본값(`normal`)은 새 untracked 디렉토리를
    `?? newdir/` **한 항목으로 접어서** 낸다 — 그러면 전개 결과가 파일이 아니라 *디렉토리* 라
    아래 필터를 그대로 통과해 `git add -A -- newdir/` 가 되고, 막으려던 뭉뚱그리기가 이 경로로
    되살아난다(PM 실 git 재현). `all` 이면 `?? newdir/c.md` 로 파일까지 펼친다.

    `-z` + NUL 파싱은 `git_parse_porcelain_z` 한 곳이 담당한다(비-ASCII/공백 경로 인용 문제·
    rename 2토큰 처리를 두 벌로 만들지 않는다 — 잔여 보고도 같은 함수를 쓴다).
    """
    rc, out = run(["status", "--porcelain", "-z", "--untracked-files=all", "--", rel])
    if rc != 0:
        return []
    return [path for _code, path in git_parse_porcelain_z(out)]


def git_scope_stageable(repo: Path, pathspec: Sequence[str], *,
                        run_git: GitRunner | None = None) -> tuple[str, ...]:
    """`add`/`commit` 이 매치할 수 있는 **파일** 경로만 남긴다 — 미매치/서브모듈 fatal 방지 +
    디렉토리 소멸.

    다섯 가지를 한 곳에서 처리한다(호출부가 다시 판정하지 않는다):
      1. **디렉토리는 파일로 펼친다** — 변경 파일 목록으로(`_git_scope_expand_dir`). 디렉토리
         stage 는 그 아래 남의 WIP 를 함께 싣는 누출 채널이라 구조적으로 막는다. **이미 삭제된
         디렉토리도 전개한다**(선언한 삭제가 빠지면 안 된다 — 아래 루프 주석).
      2. **중첩 git(서브모듈) 내부 경로 제거** — 상위 repo 의 `add` 가 rc=128 fatal 로 죽는다.
      3. **미존재·미추적 경로 제거** — `git add -A -- <경로>` 는 index·worktree 어디에도 없는
         경로를 주면 rc=128 fatal 로 죽고 **아무것도 stage 되지 않는다**(→ commit 실패 →
         claim 롤백). 로컬에서 만들어진 뒤 한 번도 커밋되지 않은 티켓이 이동하면 *옛* 경로가
         정확히 그 상태고(detached 구간의 보류 commit), 디자인 git 에선 `touches` 가 이 repo 에
         없는 경로(두-git 형상의 코드 worktree 경로·오타)를 가리키는 경우가 그렇다. 추적 중인
         *삭제* 경로는 남긴다(삭제도 커밋돼야 이동이 완성된다).
      4. **gitignored 경로 제거**(`_git_scope_ignored`) — 명시 pathspec 이 ignored 면 `add` 가
         **rc=1 에러**다(광역 `add -A` 와 다르다). 사람이 손으로 적는 `touches` 에 gitignored
         경로가 섞여도 stage 가 통째로 죽으면 안 된다(3과 같은 fail-soft 클래스).
      5. **최종 관문: 디렉토리는 하나도 반환하지 않는다** — 1의 전개가 *조용히 실패* 해도
         불변식이 유지되게 마지막에 한 번 더 거른다. 실제로 그런 실패가 있었다: `status
         --porcelain` 기본값(`untracked-files=normal`)이 새 untracked 디렉토리를 `?? newdir/`
         한 항목으로 접어, 전개 결과가 디렉토리인 채 3의 `exists()` 를 통과했다(PM 실 git
         재현). **불변식은 전개의 성공에 의존하면 안 된다.**
    """
    run = run_git or (lambda args: _git_scope_run(repo, args))
    expanded: list[str] = []
    for p in pathspec:
        # 전개는 **디렉토리이거나 worktree 에 없는** 경로에 시도한다. 후자가 load-bearing 이다:
        # 선언한 디렉토리가 *통째로 삭제* 되면 `is_dir()` 이 false 라 옛 게이트는 전개를 건너뛰고,
        # 이어지는 추적 판정도 `ls-files -- src` 가 `src/a.py` 를 내놔 `src` 와 매칭되지 않아
        # 경로가 통째로 탈락했다 — **선언한 삭제가 커밋에서 조용히 빠졌다**(reviewer 실측).
        # 존재하는 *파일* 은 전개 결과가 자기 자신이라 시도할 이유가 없다(subprocess 절약).
        candidate_path = Path(repo) / p
        expand = candidate_path.is_dir() or not candidate_path.exists()
        candidates = (_git_scope_expand_dir(repo, p, run) or [p]) if expand else [p]
        for candidate in candidates:
            if candidate and candidate not in expanded:
                expanded.append(candidate)
    expanded = [p for p in expanded if not _git_scope_nested_git(repo, p)]
    on_disk = {p for p in expanded if (Path(repo) / p).exists()}
    unknown = [p for p in expanded if p not in on_disk]
    tracked: set[str] = set()
    if unknown:
        rc, out = run(["ls-files", "-z", "--", *unknown])
        if rc == 0:
            tracked = {line for line in out.split("\0") if line}
    stageable = [p for p in expanded if p in on_disk or p in tracked]
    ignored = _git_scope_ignored(repo, stageable, run)
    return tuple(p for p in stageable
                 if p not in ignored and not _git_scope_is_directory(repo, p))


def git_scope_stage_pathspec(repo: Path, paths: Sequence[Path], *,
                             exclude_prefixes: Sequence[str] = (),
                             run_git: GitRunner | None = None) -> tuple[str, ...]:
    """선언 경로 → 그 repo 에서 실제 stage 가능한 pathspec (스코프+동형 필터 단일 진입).

    `git_scope_pathspec`(스코프) → `git_scope_stageable`(미존재/미추적 제거) 순서 고정 —
    두 단계를 따로 부르는 호출부가 순서를 뒤바꾸거나 한쪽을 빼먹지 않게 한 진입점으로 묶는다.
    """
    return git_scope_stageable(
        repo, git_scope_pathspec(repo, paths, exclude_prefixes=exclude_prefixes),
        run_git=run_git)


def git_scope_stage_and_commit(repo: Path, message: str, pathspec: Sequence[str], *,
                               run_git: GitRunner | None = None,
                               after_stage: Callable[[], None] | None = None) -> None:
    """선언된 pathspec **만** stage+commit 한다 (`add -A --` + `commit -m … --` 쌍).

    `add` 선행은 **필수**다 — `git commit --only -- <새 경로>` 단독은 그 경로가 untracked 라
    `pathspec did not match` 로 실패한다(architect 실측). 커밋이 실제로 생겼는지 판정(HEAD
    전진)은 호출부 몫이다 — repo 마다 HEAD 조회 seam 이 다르다.
    """
    run = run_git or (lambda args: _git_scope_run(repo, args))
    run(["add", "-A", "--", *pathspec])
    if after_stage is not None:
        after_stage()
    run(["commit", "-m", message, "--", *pathspec])
