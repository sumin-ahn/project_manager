"""worktree git runner와 생성 전제·목록·브랜치 조회."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from pm_engine.domain import worktree_records
from pm_engine.infra import pm_paths, repo_files, worktree_paths


# git subprocess 타임아웃 (초) — captured 러너(_real_git_runner)의 subprocess timeout + worktree
# add console-visible 러너(_real_git_runner_interactive)의 상한. **타임아웃의 실패모드 = 정상 op 도
# 죽임(false-kill)** — 대형 repo 의 worktree add(로컬 bare→full checkout·느린 디스크/VPN/
# Windows)가 진행 중인데도 짧은 고정값(옛 120)에 걸려 false-kill 되던 실측 블로커. submodule 선례
# (SUBMODULE_TIMEOUT)와 동형으로 env override + 관대한 기본으로 옮긴다.
#
# env override (`_resolve_submodule_timeout` 미러): 코드 직수정은 worktree_pool.py=manifest
#   엔진이라 다음 pm_update 가 원복 → **env `PM_GIT_TIMEOUT`(pm_update overwrite 를 넘어 persist)** 로
#   튜닝한다. `0`/`none`/`unlimited`/빈값 → None(무제한 — 진행이 콘솔에 보이는 worktree add 에 안전·
#   hang 은 콘솔 가시·Ctrl-C), 양의 정수 → 그 초, 미설정/비정상 → 기본 1800(30분·유한 관대). 무제한을
#   기본으로 안 두는 건 captured 계열 fast op(status/checkout 등)의 silent-hang 방지(submodule 3600
#   유한 기본과 동일 철학·무제한은 env opt-in).
#
# ⚠️ **무제한은 console-visible 러너에만** (codex 게이트·설계 모순 fix): `None`(무제한)은 진행이 콘솔에
#   보이는 worktree-add 인터랙티브 러너에만 적용한다. **captured 러너(`_real_git_runner`)는 절대 무제한이
#   되지 않는다** — silent(진행 안 보임)라 무제한이면 base 파생 `fetch origin` 네트워크 stall 시 silent hang
#   한다. 그래서 captured 러너는 GIT_TIMEOUT_SECONDS 가 None 일 때 `_GIT_TIMEOUT_DEFAULT`(유한)로 폴백-캡한다
#   (아래 `_real_git_runner`). 즉 captured=항상 유한·visible add=none 가능.
_GIT_TIMEOUT_DEFAULT = 1800   # 기본 유한 상한 (초·30분) — 미설정/비정상 기본 + captured 러너 무제한 폴백 cap.


def _resolve_git_timeout() -> "int | None":
    raw = os.environ.get("PM_GIT_TIMEOUT")
    if raw is None:
        return _GIT_TIMEOUT_DEFAULT
    raw = raw.strip().lower()
    if raw in ("0", "none", "unlimited", ""):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return _GIT_TIMEOUT_DEFAULT


GIT_TIMEOUT_SECONDS = _resolve_git_timeout()


# submodule init 인터랙티브 러너의 timeout. 짧은 captured git(status·checkout·dirty·
# stash)은 GIT_TIMEOUT_SECONDS(기본 1800) 로 충분하지만, submodule clone 은 대형 family
# repo + VPN 에서 600s 도 초과(실 Windows multi-PM 파일럿 "10분 아슬" 실증) → TimeoutExpired
# 로 죽었다. 인터랙티브 러너는 stdio 를 콘솔에 상속(진행상황·credential 프롬프트 작동)하고
# 이 대폭 확대된 timeout(또는 None=무제한)으로 큰 clone 을 끝까지 돌린다. 수동 콘솔 실행과
# 동일 거동. None 으로 두면 timeout 자체를 끈다(완전 무제한·hang 위험은 콘솔에 가시).
#
# env override (reviewer): 극단적 대형 repo·느린 VPN 에서 1h 도 모자라면 코드 수정 없이
#   `PM_SUBMODULE_TIMEOUT` 로 재조정한다 — `0`/`none`/`unlimited` → None(무제한), 양의 정수 → 그 초.
#   미설정/비정상 → 기본 3600.
def _resolve_submodule_timeout() -> "int | None":
    raw = os.environ.get("PM_SUBMODULE_TIMEOUT")
    if raw is None:
        return 3600
    raw = raw.strip().lower()
    if raw in ("0", "none", "unlimited", ""):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return 3600


SUBMODULE_TIMEOUT = _resolve_submodule_timeout()


def _real_git_runner(cwd: Path) -> worktree_records.GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner 를 만든다 (pm_import._real_git_runner 선례).

    반환 callable: argv(list) → (returncode, stdout+stderr). git 바이너리 부재(shutil.which)
    면 (1, msg)·예외는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부에 위임). `git -C
    <cwd> <argv...>` 형태로 항상 그 work tree/repo 에 묶는다. 인코딩은 엔진 관례대로 UTF-8
    (한글 경로·메시지 안전).

    **stdout+stderr 결합 반환 (pm_config._real_clone_runner 정합)**: 옛 코드는
    `result.stdout` 만 돌려 stderr 를 버려 — TimeoutExpired/실패 시 out='' 가 돼 진단이
    불가능했다(`git submodule init failed: ''`). stderr 를 합쳐 에러를 가시화한다.
    ⚠️ `_is_dirty` 는 결합된 출력을 `_porcelain_status_lines`(porcelain 형식 라인만 추림·아래)로
    필터하므로 stderr 경고가 섞여도 dirty 오탐이 없다 — 이 결합은 진단용이고 dirty 판정은
    porcelain 라인만 보는 경로로 분리돼 있다.

    ⚠️ **captured 러너는 절대 무제한이 되지 않는다 (codex 게이트·설계 모순 fix)**: subprocess
    timeout 은 `GIT_TIMEOUT_SECONDS or _GIT_TIMEOUT_DEFAULT` — GIT_TIMEOUT_SECONDS 가
    None(PM_GIT_TIMEOUT=none)이어도 유한 fallback(1800s)으로 캡한다. captured 는 진행이 콘솔에
    안 보여(silent) 무제한이면 network stall(base 파생 `fetch origin`) 시 silent hang 하기 때문.
    무제한(None)은 진행이 콘솔에 보이는 worktree-add 인터랙티브 러너에만 허용한다.
    """
    return repo_files.real_git_runner(
        cwd,
        missing_binary_rc=1,
        # captured=항상 유한: None(무제한)이면 silent hang → 기본값으로 캡.
        timeout=GIT_TIMEOUT_SECONDS or _GIT_TIMEOUT_DEFAULT,
        output_mode="stdout_stderr",
        which=shutil.which,
        run=subprocess.run,
    )


def _real_git_runner_interactive(
    cwd: Path, *, timeout: "int | None" = SUBMODULE_TIMEOUT,
) -> worktree_records.GitRunner:
    """console-visible 인터랙티브 git runner — stdio 콘솔 상속·튜닝 가능한 timeout.

    `_real_git_runner` 와 달리 **capture 하지 않는다** — stdout/stderr/stdin 을 부모 콘솔에
    그대로 상속한다(`subprocess.run(..., capture_output 안 줌`). 그래서:
      - 대형 clone/checkout 의 진행상황이 화면에 실시간 표시된다(긴 침묵 대신).
      - git credential/auth 프롬프트가 작동한다(수동 콘솔 실행과 동일).
      - `timeout` 이 관대(또는 None=무제한)라 느린 op 이 짧은 고정값에 false-kill 되지 않는다.

    **두 호출부 (같은 패턴·다른 timeout)**:
      - submodule init — `timeout` 미지정 → 기본 `SUBMODULE_TIMEOUT`(3600s·env
        `PM_SUBMODULE_TIMEOUT`). 600s 초과 대형 clone 이 TimeoutExpired 로 죽던 블로커 해소.
      - worktree add — `timeout=GIT_TIMEOUT_SECONDS`(1800s·env `PM_GIT_TIMEOUT`).
        대형 repo 의 full checkout 이 옛 captured 120s 에 false-kill 되던 블로커 해소.

    반환 `(rc, "")` — 출력은 콘솔로 직접 갔으므로 캡처 문자열은 없다(rc 로만 성공/실패 판정).
    git 부재/예외는 `(1, str(exc))`(또는 부재 메시지) — `_real_git_runner` 와 같은 fail-soft.
    `create_slot` 의 submodule/worktree-add 단계가 `git_runner is None` 인 실경로에서만 이걸
    쓴다 — 주입된 git_runner(테스트 mock)가 있으면 그대로(DI seam 보존·인터랙티브 안 탐).

    ⚠️ 비-tty(CI/pytest)서도 안전: 테스트는 git_runner 를 주입하므로 이 실 인터랙티브
    경로를 타지 않는다. 이 함수 자체의 단위테스트는 짧은 비-네트워크 git 명령(stdin 블록
    없음)으로만 호출한다(submodule clone 은 실행하지 않음).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            # capture_output 미지정 = stdout/stderr/stdin 부모 콘솔 상속(인터랙티브).
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                timeout=timeout,
            )
            return result.returncode, ""
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외 메시지 surface.
            return 1, str(exc)

    return runner


def _porcelain_status_lines(out: str) -> list[str]:
    """`git status --porcelain` 출력에서 *실제 status 엔트리* 라인만 추린다.

    porcelain v1 엔트리 형식 = `XY <path>`(X·Y = 2글자 status code·세 번째가 공백). git
    경고(stderr·`warning: ...`)가 stdout 캡처에 섞여도 그 형식이 아니므로 걸러진다 —
    `_real_git_runner` 가 stdout+stderr 를 합치게 바뀐 뒤dirty 오탐을 막는 가드.
    빈 줄 무시. 형식이 맞는 라인만 dirty 신호로 본다(보수성은 호출부 rc 가드가 유지).
    """
    lines: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain v1 엔트리: 2글자 status code + 공백 구분자 (예 " M file", "?? new").
        if len(line) >= 3 and line[2] == " ":
            lines.append(line)
    return lines


def _is_dirty(slot_path: Path, *, git_runner: worktree_records.GitRunner | None = None) -> bool:
    """슬롯 worktree 에 미커밋 변경(untracked 포함)이 있는지. git 오류 → 보수적으로 dirty.

    `git status --porcelain` 의 porcelain 엔트리 라인이 하나라도 있으면 dirty. git 호출
    실패(rc!=0)는 상태를 모르므로 **보수적으로 dirty 로 본다** — clean 으로 오판해 커밋 안 한
    변경을 날리는 것보다 안전.

    ⚠️ stderr 오탐 방어: `_real_git_runner` 가 stdout+stderr 를 합쳐 반환하게
    바뀌어, status 출력에 stderr 경고(`warning: ...`)가 섞일 수 있다. `out.strip()!=""`
    로 보면 그 경고만 있어도 dirty 오탐이 난다 → porcelain 엔트리 형식 라인만 보아
    경고에 안 흔들리게 한다(`_porcelain_status_lines`).
    """
    runner = git_runner or _real_git_runner(slot_path)
    rc, out = runner(["status", "--porcelain"])
    if rc != 0:
        return True
    return len(_porcelain_status_lines(out)) > 0


def _is_valid_bare(bare_path: Path, *, runner: worktree_records.GitRunner) -> bool:
    """`.repos/<repo>.git` 가 *worktree add 의 base 로 실제로 쓸 수 있는 bare git repo* 인지 검증.

    `Path.exists()` 는 경로 존재만 본다 — 중단된 `git clone --bare`(하네스 타임아웃·Ctrl-C·
    #5→#1 cascade)가 남긴 **부분·빈·깨진** `.repos/<repo>.git` 도 exists()=True 다. 그런 무효
    bare 를 "존재 → 재사용"으로 통과시키면 repo add 는 rc0 success 인데 invariant(쓸 수 있는
    bare)는 깨진 채, 나중 `git worktree add` 의 base 가 없어 날 git 에러로 죽는다(원인 안
    드러남·audit #1→#4).

    **두 조건 AND** (codex 게이트·실측 확정):
      1. **bare 형식** — `git -C <bare> rev-parse --is-bare-repository` rc0 && stdout 에 `true`.
      2. **HEAD 해소** — `git -C <bare> rev-parse --verify HEAD` **rc0**. is-bare(=`core.bare=true`)
         만으론 부족하다: `git init --bare`(또는 objects fetch 전 죽은 clone)가 남긴 **빈/부분
         bare** 는 core.bare=true 지만 HEAD/objects 가 없다(실측: is-bare "true"·`rev-parse
         --verify HEAD` rc128·`rev-list --all --count` 0). `worktree add <path>` 는 HEAD 를
         체크아웃하므로 **HEAD 해소 = base 가용성의 정확한 precondition** — rc 기반 검사(문자열
         파싱 아님·견고)로 이 빈/부분 bare 를 broken 으로 잡는다.

    부분 bare 는 discovery 가 상위 워킹트리로 올라가 is-bare `false`(rc0·`.repos/` 는 워킹트리
    안)거나 상위도 repo 아니면 rc≠0(fatal) — 어느 쪽도 조건1 False. **fail-soft**: git 바이너리
    부재·예외는 runner 가 (1, msg) 로 감싸므로 자연히 False(크래시 0·rc≠0 로 호출부 위임).

    is-bare 파싱은 `"true" in out.split()`(reviewer sug) — `_real_git_runner` 가 stdout+stderr
    를 결합 반환하므로, 유효 bare 라도 git 이 stderr 경고 한 줄을 내면 `out.strip()==
    "true"` 는 false-negative(유효를 broken 오판)다. 공백 토큰에 `true` 존재로 그 경고에 안 흔들린다.

    `runner` 는 argv 앞에 `-C <bare>` 를 받는 clone-runner 계열 — pm_config 의 sibling 헬퍼
    (`_set_bare_fetch_refspec`·`_ensure_bare_branch_tracking`·`_resolve_base`)가 주입 runner 를
    `-C <bare>` 로 재사용하는 관례와 동일(별도 DI seam 안 만듦·주입 mock 은 이 argv 로 유효/무효
    bare 를 모델링). 절대경로 `-C` 는 중첩돼도(worktree_pool 실경로 `_real_git_runner(bare)` 와
    이중 `-C`) idempotent(뒤 절대경로가 앞을 리셋)라 안전.
    """
    rc, out = runner(["-C", str(bare_path), "rev-parse", "--is-bare-repository"])
    if rc != 0 or "true" not in out.split():
        return False
    # HEAD 해소 검증 — is-bare(형식)만으론 빈/부분 bare 를 통과시킨다(위 조건2). worktree add 의
    # 체크아웃 대상 HEAD 가 해소돼야 base 로 쓸 수 있다. rc 기반(견고·문자열 무관).
    head_rc, _ = runner(["-C", str(bare_path), "rev-parse", "--verify", "HEAD"])
    return head_rc == 0


def _slot_branch_exists(runner: worktree_records.GitRunner, branch: str) -> bool:
    """로컬 브랜치 `branch` 가 존재하는지 — **color-safe** machine-readable 출력으로 판정.

    create_slot 이 작업 슬롯 전용 브랜치 `<repo>_<N>` 을 파기 전에 이미 있는지 선-검출한다
    (→ `SlotBranchExists` fail-loud·다른 슬롯에서 `switch` 로 재개 안내).

    ⚠️ **평문 `git branch --list <b>` 는 ambient `color.branch=always` 서 ANSI 오염**(codex 실측:
    출력 `'  A_1\\x1b[m\\n'` → `.split()` 토큰 `'A_1\\x1b[m'` 이 브랜치명과 불일치 → 기존 브랜치를
    "없음"으로 오판 → 선-검출이 빠져 `worktree add -b` 가 안내 없이 늦게 실패). 그래서 **`--format=
    %(refname:short)`**(ref-filter 포맷·색 atom 없음)로 뽑는다 — `color.branch=always` 여도 평문
    (`'A_1\\n'`·실측)이라 오염이 없다. **splitlines 정확-일치**(`line.strip()==branch`)로 판정:
    exact 패턴(`--list <b>`·glob 메타 없음)이라 그 브랜치만 리스트하고, `_real_git_runner` 의
    stdout+stderr 결합에 stderr 경고가 섞여도 그 라인은 브랜치명과 정확-일치하지 않아
    안전(`.split()` 부분매치보다 견고).

    **rc 무시** — `git branch --list` 는 매치 없어도 rc0 이라 rc 기반(show-ref/rev-parse)은 못 쓴다
    (게다가 주입 runner 의 generic 폴백 rc0 이 "존재" 오탐돼 hermetic 테스트를 깬다).
    출력 라인 정확-일치만 신뢰한다. 주입 runner(테스트 mock)의 generic 폴백 `(0, "")` 은 빈
    splitlines → **"부재"**(슬롯 브랜치 생성 경로로 진행).
    """
    _rc, out = runner(["branch", "--list", "--format=%(refname:short)", branch])
    return any(line.strip() == branch for line in str(out).splitlines())


def _worktree_registered(home, path: Path, *, git_runner: worktree_records.GitRunner) -> bool:
    """git 이 이 경로를 worktree 로 **등록**하고 있는가 — 파괴 판단의 입력.

    `git worktree list --porcelain`(`_parse_worktree_porcelain` 재사용 — 새 파서 0)을 bare
    컨텍스트에서 1회 조회해 경로 값으로 대조한다(슬롯 식별자가 아니라 `resolve()` 한 경로 —
    `_branch_checked_out_elsewhere` 와 같은 규칙). bare 엔트리는 슬롯이 아니라 제외한다.

    조회 실패(rc≠0)는 **답이 아니라 판정 실패**라 raise 한다 — 등록 여부를 모르는 채로
    디렉터리를 지우면 git 이 아직 쥐고 있는 트리를 날린다.
    """
    rc, out = git_runner(["worktree", "list", "--porcelain"])
    if rc != 0:
        raise RuntimeError(
            f"git worktree list failed (rc={rc}, out={str(out).strip()[:200]!r}) — "
            "등록 여부를 관측하지 못해 아무것도 지우지 않았다."
        )
    target = path.resolve(strict=False)
    for entry in _parse_worktree_porcelain(home, out):
        if entry.bare:
            continue
        try:
            if Path(entry.path).resolve(strict=False) == target:
                return True
        except OSError:
            continue
    return False


def _parse_worktree_porcelain(home, out: str) -> list[worktree_records.GitWorktree]:
    """`git worktree list --porcelain` 출력을 GitWorktree 리스트로 파싱한다.

    porcelain 포맷 = 엔트리별 속성 라인(각 줄 `worktree <path>`·`HEAD <sha>`·`branch
    refs/heads/<name>`·`detached`·`bare`·`locked`/`prunable` 등) + 빈 줄 구분. `worktree` 라인이
    새 엔트리 시작이다. `branch` 는 `refs/heads/` 프리픽스를 벗겨 브랜치명만. 인식 못 하는 속성
    라인은 무시(견고). 빈 출력/`worktree` 라인 전 잡음은 무시.
    """
    entries: list[worktree_records.GitWorktree] = []
    cur: "dict | None" = None

    def _flush() -> None:
        if cur is None:
            return
        entries.append(worktree_records.GitWorktree(
            path=cur["path"],
            slot=worktree_paths._slot_from_worktree_path(home, cur["path"]),
            branch=cur["branch"],
            detached=cur["detached"],
            bare=cur["bare"],
        ))

    for raw in out.splitlines():
        line = raw.rstrip()
        if line.startswith("worktree "):
            _flush()
            cur = {"path": line[len("worktree "):].strip(),
                   "branch": None, "detached": False, "bare": False}
        elif cur is None:
            continue
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        # HEAD/locked/prunable/기타 속성은 무시(reconcile 판정에 불요).
    _flush()
    return entries


def _discover_bares(home) -> "list[tuple[str, Path]]":
    """`.repos/*.git` 에서 (repo, bare 경로) 목록 — reconcile 의 전-repo 열거원.

    REPOS_DIR 부재면 빈 리스트(fail-soft). repo 이름 = `<name>.git` 의 stem(`.git` 제거).
    """
    if not pm_paths.repos_dir(home).exists():
        return []
    out: "list[tuple[str, Path]]" = []
    for p in sorted(pm_paths.repos_dir(home).glob("*.git")):
        out.append((p.name[:-len(".git")], p))
    return out


def list_git_worktrees(home, 
    repo: "str | None" = None, *, git_runner: worktree_records.GitRunner | None = None,
) -> list[worktree_records.GitWorktree]:
    """`git worktree list --porcelain` 로 실 git worktree 를 열거한다 — 장부 대조 소스.

    `repo` 지정 → 그 repo 의 bare(`.repos/<repo>.git`) 컨텍스트 1개만. `repo=None` → `.repos/` 의
    모든 bare 를 순회(전-repo·reconcile 용). 각 bare 에서 `git -C <bare> worktree list --porcelain`
    을 **capture 러너**(`_real_git_runner`)로 실행한다 — 출력을 파싱해야 하므로 인터랙티브(출력이
    콘솔 직행·빈 out)를 쓰지 않는다. 짧은 로컬 read-only op 라 유한 timeout 으로 충분.

    **fail-soft**: git 부재·rc≠0·예외는 그 bare 를 건너뛴다(빈 기여·크래시 0·rc≠0 로 위임). **DI
    seam 보존**: 주입 runner(테스트 mock)는 모든 bare 에 재사용(hermetic — porcelain 을 모델링·실
    git 안 탐). 반환은 GitWorktree 리스트(bare 엔트리 포함·slot=None) — 호출부(reconcile/슬롯번호)가
    non-bare 슬롯만 필터한다.
    """
    if repo is not None:
        bares = [(repo, pm_paths.bare_repo_path(home, repo))]
    else:
        bares = _discover_bares(home)
    result: list[worktree_records.GitWorktree] = []
    for _r, bare in bares:
        runner = git_runner or _real_git_runner(bare)
        try:
            rc, out = runner(["worktree", "list", "--porcelain"])
        except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 흡수(그 bare 건너뜀).
            continue
        if rc != 0:
            continue
        result.extend(_parse_worktree_porcelain(home, out))
    return result


def _git_slot_numbers(home, repo: str, *, git_runner: worktree_records.GitRunner | None = None) -> set[int]:
    """실 git worktree 목록에서 이 repo 의 슬롯 번호 집합 (orphan 포함·슬롯번호 충돌 회피).

    `_existing_slot_numbers`(ledger 기준)와 **병합**해 create_slot 이 orphan(ledger 미등록·중단된
    create/수동 add 잔존) worktree 번호와 충돌하지 않게 한다 — `git worktree add` "already exists"
    암호 에러(audit #4) 근절.
    """
    nums: set[int] = set()
    prefix = f"work/{repo}_"
    for w in list_git_worktrees(home, repo, git_runner=git_runner):
        if w.bare or not w.slot or not w.slot.startswith(prefix):
            continue
        tail = w.slot[len(prefix):]
        if tail.isdigit():
            nums.add(int(tail))
    return nums


# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — `refs/heads/<name>`. 이 접두 **정확히**를
# 제거해야 순수 브랜치명이 된다(모호성 접두가 붙는 `--short` 대신 full ref 를 읽는 이유).
_SYMREF_BRANCH_PREFIX = "refs/heads/"


def current_branch(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> str | None:
    """슬롯 worktree 의 git HEAD 에서 현재 브랜치를 **live** 로 읽는다.

    `git symbolic-ref HEAD` → `refs/heads/<name>` 에서 `refs/heads/` 접두를 정확히 제거해 브랜치명.
    브랜치가 git 의 단일 진실 — 장부에 저장된 복사본이 아니라 슬롯 worktree 의 실제 HEAD 를 매번
    조회한다(사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영·드리프트 불가능).

    **`symbolic-ref HEAD`(full ref·`--short` 없이)를 쓰는 이유**:
    `rev-parse --abbrev-ref HEAD` 는 (a) detached 를 `"HEAD"` 문자열로, (b) **unborn 브랜치**(아직
    커밋 0 인 새 브랜치)를 rc≠0 에러로 줘서 — *이름이 있는* unborn 브랜치를 detached/조회불가로
    오판한다(→ "(미지정)"). `symbolic-ref HEAD` 는 unborn 브랜치도 `refs/heads/<name>` 을 rc=0 으로
    주고, detached 일 때만 "ref HEAD is not a symbolic ref" 로 rc≠0 이라 — "현재 브랜치명 or 브랜치
    아님"의 정석 primitive 다(git=진실). **`--short` 를 뺀 이유**:
    `--short` 는 브랜치명과 같은 이름의 태그가 있으면(릴리즈가 `v1.3.0` 브랜치를 그대로 `v1.3.0`
    태그로 찍은 경우) 모호성 회피로 `heads/v1.3.0` 을 돌려줘 순수 브랜치명이 아니었다 — 장부 기록
    (`v1.3.0`)과 불일치해 부트스트랩 0단계가 가짜 "외부 개입" FAIL-LOUD 로 차단됐다.
    full ref 는 태그 존재와 무관하게 항상 `refs/heads/<정확한 브랜치명>` 이라 모호성 자체가 없고,
    `refs/heads/` 접두만 정확히 벗기면 진짜 이름이 `heads/x` 인 브랜치(합법·`heads/x` → 그대로 보존)
    도 오인하지 않는다(`--short` 후 `heads/` strip 은 이 브랜치를 `x` 로 오인한다·codex must-fix).

    `None` 반환(전부 fail-soft·예외 raise 금지·표시층이 "(detached/조회불가)" 등으로 변환):
      - detached HEAD — `symbolic-ref` 가 rc≠0(symbolic ref 아님).
      - git 호출 실패 (rc≠0) — 손상/락/git 부재 등.
      - `refs/heads/` 로 시작 안 하는 이상 출력 — 보수적으로 None(브랜치 아님).
      - 슬롯 경로 부재 — worktree 폴더가 아직 없거나 지워짐.

    `git_runner` 미주입 시 실경로는 `_real_git_runner(slot_path(slot))` 로 해소한다
    (기존 DI seam 패턴 — 테스트는 mock runner 주입으로 hermetic·실 git 불요). **슬롯 경로
    부재 가드는 실경로(미주입)에서만 본다** — git_runner 주입 시엔 그 runner 가 존재/rc/HEAD
    를 전부 모델링하는 권위이므로 fs 가드를 건너뛴다(hermetic 테스트가 슬롯 폴더 없이 동작).
    주입 runner 가 예외를 던져도 None 으로 흡수한다(docstring 의 "raise 금지" 규칙을 DI seam
    까지 보장 — 실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸므로 실경로는 영향 없음).
    """
    runner = git_runner
    if runner is None:
        path = pm_paths.slot_path(home, slot)
        if not path.exists():
            return None
        runner = _real_git_runner(path)
    try:
        rc, out = runner(["symbolic-ref", "HEAD"])
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 None(규칙: raise 금지).
        return None
    if rc != 0:  # detached(symbolic ref 아님)·git 부재/실패 → 브랜치 없음.
        return None
    ref = out.strip()
    if not ref.startswith(_SYMREF_BRANCH_PREFIX):  # 이상 출력(브랜치 아님) → 보수적 None.
        return None
    return ref[len(_SYMREF_BRANCH_PREFIX):] or None


def _rollback_worktree(home, repo: str, slot_path_: Path, *, git_runner: worktree_records.GitRunner | None = None) -> None:
    """`git worktree add` 성공 후 단계가 실패했을 때 만든 worktree 를 롤백한다.

    bare 컨텍스트(`.repos/<repo>.git`)에서 `git worktree remove <slot_path> --force` 를
    부른다 — `add` 가 거기서 일어났으므로 `remove` 도 같은 컨텍스트라야 한다(공유 .git 원
    = bare). 실패하면 best-effort 로 `worktree prune` 폴백.

    **best-effort·2차 예외 삼킴 금지**: 이 함수는 절대 raise 하지 않는다(롤백 자체가 실패해도
    원래 에러로 raise 되도록·호출부가 finally/except 에서 부른다). 댕글링 worktree("슬롯
    없음"+재시도 "이미 존재")가 안 생기게 fs 를 정리하는 게 목적이고, 정리 실패는 원래
    에러를 가린다(2차 예외)는 더 나쁜 결과를 부르므로 조용히 best-effort 한다.
    """
    runner = git_runner or _real_git_runner(pm_paths.bare_repo_path(home, repo))
    try:
        rc, _out = runner(["worktree", "remove", str(slot_path_), "--force"])
        if rc != 0:
            runner(["worktree", "prune"])  # 폴백 — 등록 메타만이라도 정리.
    except Exception:  # noqa: BLE001 — best-effort: 롤백 실패가 원래 에러를 가리면 안 됨.
        pass
