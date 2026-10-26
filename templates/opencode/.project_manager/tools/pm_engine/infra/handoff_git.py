"""핸드오프 git 상태 관측."""
from __future__ import annotations

from typing import Callable
from pm_engine.domain import handoff_text
from pm_engine.infra import submodule_git
import subprocess


def _ahead_commit_count(
    runner: Callable[[list[str]], tuple[int, str]],
) -> tuple[int, str] | None:
    """PM 홈의 미push commit 수 — (count, baseline ref). 해소 불가면 None (fail-soft).

    `git rev-list --count <baseline>..HEAD` 를 baseline 후보(`_PENDING_PUSH_BASELINE_REFS` —
    출하 변경 분류와 같은 순서: @{upstream} 우선·origin/main 폴백) 순으로 시도해 첫 성공을 쓴다.
    detached/upstream 미설정/원격 부재/비수치 출력은 전부 None — 판정을 지어내지 않는다.
    """
    for ref in handoff_text._PENDING_PUSH_BASELINE_REFS:
        try:
            rc, out = runner(["rev-list", "--count", f"{ref}..HEAD"])
        except Exception:  # noqa: BLE001 — fail-soft: git 예외는 rc 실패와 같이 다음 후보로.
            continue       # 첫 후보의 예외로 폴백 후보를 건너뛰면 upstream 미설정이 곧 미해소가 된다.
        if rc != 0:
            continue
        text = out.strip().splitlines()[0].strip() if out.strip() else ""
        if text.isdigit():
            return int(text), ref
    return None


def _resolve_pending_baseline(
    worktree: str,
    git_runner: Callable[[list[str]], tuple[int, str]],
) -> str | None:
    """미push diff 기준 baseline ref 를 해소한다. 해소 불가 시 None (ambiguous).

    `git -C <worktree> rev-parse --verify <ref>` 로 후보 ref(@{upstream}·origin/main)를
    순서대로 시도해 첫 성공 ref 문자열을 반환한다. detached/upstream 미설정/원격부재면
    모두 비-0 → None → 호출부가 has_unknown(ambiguous→surface) 처리. fail-soft.
    """
    for ref in handoff_text._PENDING_PUSH_BASELINE_REFS:
        rc, _ = git_runner(["-C", worktree, "rev-parse", "--verify", "--quiet", ref])
        if rc == 0:
            return ref
    return None


def _uncommitted_and_untracked_paths(
    worktree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """작업트리 미커밋(staged+unstaged tracked) + untracked 신규파일 경로를 union 반환.

    push 시 확실히 올라갈 변경(커밋만 하면 됨). 두 git 호출을 합친다:
      - `git -C <wt> diff --name-only --ignore-submodules=none HEAD`
                                                         → staged+unstaged tracked 변경 + gitlink drift
      - `git -C <wt> ls-files --others --exclude-standard` → untracked 신규파일(.gitignore 제외)
    tracked 축의 `--ignore-submodules=none` 은 `.gitmodules`/로컬 config 의 `ignore = all` 을
    **이 판정에 한해서만** override 한다. 인스턴스 소유 설정은 건드리지 않으면서 submodule HEAD 와
    상위 pin 이 다른 gitlink 를 놓치지 않는다.
    둘 중 하나라도 비-0 종료면 작업트리 상태 불명 → None (호출부가 ambiguous 처리).
    runner DI seam 경유(hermetic). 예외는 호출부에서 흡수.
    """
    rc_diff, out_diff = runner(
        ["-C", worktree, "diff", "--name-only", "--ignore-submodules=none", "HEAD"]
    )
    if rc_diff != 0:
        return None
    rc_others, out_others = runner(
        ["-C", worktree, "ls-files", "--others", "--exclude-standard"]
    )
    if rc_others != 0:
        return None
    paths: list[str] = []
    for out in (out_diff, out_others):
        paths.extend(line.strip() for line in out.splitlines() if line.strip())
    return paths


def _shipping_paths_in_pending_push(
    worktree: str,
    *,
    git_runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> tuple[list[str], bool]:
    """"지금 push 하면 올라갈 변경" ∩ SHIPPING_GLOBS 를 해소한다 (비차단 surface).

    pm_handoff [7/7] 체크리스트는 핸드오프 *후* `git commit` 을 안내한다 — 정상 핸드오프
    시점엔 출하 변경이 대개 **working tree(staged/unstaged·미커밋·untracked)** 에 있다.
    따라서 커밋된-미push 만 보면(diff <baseline>..HEAD) 정상 핸드오프 시 게이트가 발동하지
    않는다(must-fix). "지금 push 하면 올라갈 변경 전체"를 union 한다:
      - 커밋된 미push: `git -C <wt> diff --name-only <baseline>..HEAD` (baseline 해소된 경우만)
      - 작업트리 vs HEAD(staged+unstaged tracked·gitlink drift):
        `git -C <wt> diff --name-only --ignore-submodules=none HEAD`
      - untracked 신규파일: `git -C <wt> ls-files --others --exclude-standard`
    이 union ∩ SHIPPING_GLOBS 가 `shipping_hits`.

    ambiguous 정련: uncommitted/untracked 출하 hit 이 있으면 **baseline 해소 여부와 무관하게
    발동**(그 변경은 확실히 올라간다). baseline 해소불가(또는 커밋된-미push diff 실패)
    **그리고** 출하 hit 이 전혀 없을 때만 has_unknown=True(커밋된-미push 출하분을 못 봐서
    불명). diff/ls-files 명령 실패·예외는 fail-soft(has_unknown=True) — silent skip 금지
    (false-skip = 미검증 출하 위험 > false-fire 낭비).

    반환: (shipping_hits, has_unknown).
      - shipping_hits 비어있지 않음 → 발동.
      - shipping_hits 비어있고 has_unknown False → 명확한 비-출하(또는 push 없음) → skip.
      - shipping_hits 비어있고 has_unknown True → ambiguous (호출부 surface).

    fail-soft: git 미설치·worktree 부재 등 예외는 has_unknown=True 로 흡수(크래시 금지).
    git_runner DI seam — hermetic 테스트는 결정론 stub 주입. 모든 git 호출은 이 seam 경유.
    """
    runner = git_runner if git_runner is not None else _module_run_git

    # 1. 확실히 올라갈 변경 — uncommitted(작업트리 vs HEAD)·untracked 신규파일.
    #    baseline 해소 여부와 무관하게 진실(이 변경은 push 시 반드시 올라간다).
    try:
        uncommitted_paths = _uncommitted_and_untracked_paths(worktree, runner)
    except Exception:  # noqa: BLE001 — fail-soft: git 예외는 ambiguous 로 흡수.
        return [], True
    if uncommitted_paths is None:
        # diff HEAD/ls-files 명령 실패 — 작업트리 상태를 모른다 → ambiguous.
        return [], True

    # 2. 커밋된 미push 변경 — baseline 해소된 경우만(detached/upstream 미설정/원격부재면 못 봄).
    committed_paths: list[str] = []
    committed_unknown = False
    try:
        baseline = _resolve_pending_baseline(worktree, runner)
        if baseline is None:
            # 커밋된-미push 경계를 모른다 → 그 부분만 불명(uncommitted 는 이미 확보).
            committed_unknown = True
        else:
            rc, out = runner(
                ["-C", worktree, "diff", "--name-only", f"{baseline}..HEAD"]
            )
            if rc != 0:
                # diff 자체 실패 — 커밋된-미push 가 무엇인지 모른다 → 그 부분만 불명.
                committed_unknown = True
            else:
                committed_paths = [
                    line.strip() for line in out.splitlines() if line.strip()
                ]
    except Exception:  # noqa: BLE001 — fail-soft: git 예외는 커밋된-미push 불명으로 흡수.
        committed_unknown = True

    all_paths = set(uncommitted_paths) | set(committed_paths)
    shipping_hits = sorted(p for p in all_paths if handoff_text._path_is_shipping(p))

    if shipping_hits:
        # uncommitted/untracked·또는 커밋된-미push 에서 확실한 출하 hit → 발동.
        # 발동할 변경이 확정됐으므로 baseline 해소불가여도 ambiguous 아님.
        return shipping_hits, False
    # 출하 hit 이 전혀 없을 때만 — 커밋된-미push 출하분을 못 봤다면(committed_unknown)
    # ambiguous(surface), 아니면 명확한 비-출하(skip).
    return [], committed_unknown


def _module_run_git(args: list[str]) -> tuple[int, str]:
    """모듈-레벨 git 실행 (DI 미주입 시 기본). (returncode, stdout+stderr)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout + result.stderr


def _unborn_head_dirty_paths(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """커밋 0(unborn HEAD) 트리의 미커밋 경로 전량 — 조회 실패면 None.

    unborn 에서도 동작하는 두 축을 union 한다. **둘 다 필요하다**:
      - `git -C <tree> diff --cached --name-only` → index 에 올라간 것(staged). `git add` 를
        마친 트리는 이 축만 답한다.
      - `git -C <tree> ls-files --others --exclude-standard` → 아직 add 안 한 것(untracked).
    untracked 만 보면 **전량 staged 인 커밋 0 저장소가 clean 으로 오판**된다(그 트리야말로
    아무것도 커밋되지 않은 상태다). gitignored 는 `--exclude-standard` 가 뺀다 — `--cached` 는
    이미 index 에 있는 것만 보므로 ignore 규칙과 무관하다.
    """
    rc_staged, out_staged = runner(["-C", tree, "diff", "--cached", "--name-only"])
    if rc_staged != 0:
        return None
    rc_untracked, out_untracked = runner(
        ["-C", tree, "ls-files", "--others", "--exclude-standard"]
    )
    if rc_untracked != 0:
        return None
    paths: list[str] = []
    for out in (out_staged, out_untracked):
        paths.extend(line.strip() for line in out.splitlines() if line.strip())
    return list(dict.fromkeys(paths))


def _dirty_paths_in_tree(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """트리의 미커밋(tracked modified) ∪ untracked-unignored 경로 — 판정 불가면 None.

    정상 경로의 판정 축은 [1b/7] 출하 surface 와 **같은 seam**(`_uncommitted_and_untracked_paths`):
      - `git -C <tree> diff --name-only --ignore-submodules=none HEAD`
                                                            → tracked 변경 + gitlink drift
      - `git -C <tree> ls-files --others --exclude-standard` → untracked(gitignored 제외)
    `--exclude-standard` 가 "clean 제외 gitignored" 판정 기준을 그대로 만족한다.

    그 seam 이 접어버리는 실패 하나를 여기서 정련한다 — **unborn HEAD**(커밋 0). `diff HEAD` 는
    비교 ref 가 없어 실패하지만 그 트리는 "전량 미커밋"이라 게이트가 가장 잡아야 할 상태다.
    커밋 유무를 `rev-parse --verify HEAD` 로 확인해, 커밋이 없으면 staged ∪ untracked 로 판정한다
    (`_unborn_head_dirty_paths` — 전량 staged 인 트리를 clean 으로 오판하지 않는다).
    커밋이 있는데 diff 가 실패했거나(git 이상) unborn 조회까지 실패(비-git 트리·git 부재)면 None —
    호출부가 비차단 경고로 처리한다(판정 못 한 것을 dirty 로 단정해 정상 핸드오프를 막지 않는다).
    """
    try:
        paths = _uncommitted_and_untracked_paths(tree, runner)
        if paths is not None:
            return paths
        rc_head, _out = runner(["-C", tree, "rev-parse", "--verify", "--quiet", "HEAD"])
        if rc_head == 0:
            return None      # 커밋은 있는데 tracked diff 실패 — 판정 불가.
        return _unborn_head_dirty_paths(tree, runner)
    except Exception:  # noqa: BLE001 — 판정 실패는 크래시가 아니라 '불명'(호출부가 surface).
        return None


def _submodule_paths_in_tree(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """등록된 재귀 submodule 경로(superproject 상대)를 반환 — 열거 불가면 None.

    `git -C <tree> submodule status --recursive` 하나를 권위로 쓰고, 출력 해석은 동적 로드 seam
    submodule_git의 공용 parser로 flag와 공백 경로를 보존한다.
    flag `-` 는 미초기화라 working tree 가 없으므로 제외한다. 부재 경로에서 `git -C` 를 돌리면
    상위 repo 로 올라가 상위 dirty 목록을 중복 반환할 수 있기 때문이다. 빈 출력은 등록된
    submodule 없음(`[]`)이다. git/파서 로드 실패·해석 불가능한 비어있지 않은 출력은 None 으로
    흡수해 호출부가 **비차단 경고**로 surface 한다 — 판정 못 한 것을 dirty 로 단정하지 않는다.
    """
    try:
        rc, output = runner(["-C", tree, "submodule", "status", "--recursive"])
    except Exception:  # noqa: BLE001 — git 부재 등은 판정 불가로 강등.
        return None
    if rc != 0:
        return None

    try:
        entries = submodule_git._parse_submodule_entries(output)
    except Exception:  # noqa: BLE001 — 공용 파서/동적 모듈 이상도 판정 불가로 강등.
        return None
    if output.strip() and not entries:
        return None
    paths = [path for flag, path in entries if flag != "-"]
    return list(dict.fromkeys(paths))
