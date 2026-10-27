"""브랜치 부모·보존 기록과 보호목록 판독."""
from __future__ import annotations

import sys
from pathlib import Path

from pm_engine.domain import worktree_records
from pm_engine.infra import clock, pm_paths, pool_store, worktree_git
from pm_engine.infra import areas_store


class BranchParentError(RuntimeError):
    """브랜치 부모 기록 읽기/쓰기(와 그 기록을 쓰는 조회) 실패 — git 출력을 싣는다."""


def _branch_config_key(branch: str, key: str) -> str:
    return f"{worktree_records._BRANCH_CONFIG_PREFIX}{branch}.{key}"


def _write_branch_config(branch: str, values: "tuple[tuple[str, str], ...]", *, what: str,
                         git_runner: worktree_records.GitRunner) -> None:
    """`branch.<branch>.<키>=<값>` 들을 git 설정에 쓴다. rc≠0 → `BranchParentError`(`what` 기록 쓰기 실패)."""
    for key, value in values:
        config_key = _branch_config_key(branch, key)
        rc, out = git_runner(["config", config_key, value])
        if rc != 0:
            raise BranchParentError(
                f"브랜치 {branch!r} {what} 쓰기 실패 — `git config {config_key} {value}` "
                f"(rc={rc}): {str(out).strip()[:200]}"
            )


def record_branch_parent(branch: str, parent: str, fork: str, *, git_runner: worktree_records.GitRunner) -> None:
    """`branch` 의 부모 이름과 갈라진 커밋을 git 설정에 쓴다. rc≠0 → `BranchParentError`."""
    _write_branch_config(branch, ((worktree_records.BRANCH_PARENT_KEY, parent), (worktree_records.BRANCH_FORK_KEY, fork)),
                         what="부모 기록", git_runner=git_runner)


def _branch_parent_records(git_runner: worktree_records.GitRunner) -> "dict[str, dict[str, str]]":
    """부모 기록 전체 `{브랜치: {"pm-parent": 부모 이름, "pm-fork": 갈라진 커밋}}` — 설정 조회 1회.

    저장소 공용 설정이라 같은 저장소의 어느 작업 폴더에서 읽어도 같다. 한 키에 값이 여럿이면
    마지막 값을 쓴다(`git config --get` 과 같은 규칙). 기록이 하나도 없으면 {}."""
    rc, out = git_runner(["config", "--get-regexp", worktree_records._PARENT_RECORDS_PATTERN])
    if rc == worktree_records._GIT_CONFIG_MISSING_RC:
        return {}
    if rc != 0:
        raise BranchParentError(
            f"부모 기록 읽기 실패 — `git config --get-regexp` (rc={rc}): {str(out).strip()[:200]}"
        )
    records: dict[str, dict[str, str]] = {}
    for line in str(out).splitlines():
        key, _sep, value = line.partition(" ")
        # 브랜치 이름에 `.`·`/` 가 들어갈 수 있어 접두·접미를 잘라 이름을 얻는다.
        for name in (worktree_records.BRANCH_PARENT_KEY, worktree_records.BRANCH_FORK_KEY):
            suffix = f".{name}"
            if key.startswith(worktree_records._BRANCH_CONFIG_PREFIX) and key.endswith(suffix):
                records.setdefault(key[len(worktree_records._BRANCH_CONFIG_PREFIX):-len(suffix)], {})[name] = value
    return records


def _parent_record(branch: str, records: "dict[str, dict[str, str]]") -> "dict | None":
    """`records` 에서 `branch` 의 부모 기록 `{"name", "fork"}` — 둘 다 없으면 None, 하나만 있으면 기록 손상 raise."""
    values = records.get(branch, {})
    name = values.get(worktree_records.BRANCH_PARENT_KEY)
    fork = values.get(worktree_records.BRANCH_FORK_KEY)
    if name is None and fork is None:
        return None
    if name is None or fork is None:
        raise BranchParentError(
            f"브랜치 {branch!r} 부모 기록 손상 — `{worktree_records.BRANCH_PARENT_KEY}`={name!r}·"
            f"`{worktree_records.BRANCH_FORK_KEY}`={fork!r} 중 하나만 있다"
        )
    return {"name": name, "fork": fork}


def read_branch_parent(branch: str, *, git_runner: worktree_records.GitRunner) -> "dict | None":
    """`branch` 의 부모 기록 `{"name", "fork"}` — 둘 다 없으면 None, 하나만 있으면 기록 손상 raise."""
    return _parent_record(branch, _branch_parent_records(git_runner))


def _children_of(branch: str, records: "dict[str, dict[str, str]]") -> "list[str]":
    """`records` 에서 부모 기록이 `branch` 를 가리키는 브랜치 이름 목록(정렬)."""
    return sorted(child for child, values in records.items()
                  if values.get(worktree_records.BRANCH_PARENT_KEY) == branch)


def branch_children(branch: str, *, git_runner: worktree_records.GitRunner) -> "list[str]":
    """부모 기록이 `branch` 를 가리키는 브랜치 이름 목록(정렬). 기록이 하나도 없으면 []."""
    return _children_of(branch, _branch_parent_records(git_runner))


def _ref_exists(ref: str, *, git_runner: worktree_records.GitRunner) -> bool:
    rc, _out = git_runner(["show-ref", "--verify", "--quiet", ref])
    return rc == 0


def _left_right_counts(left: str, right: str, *, git_runner: worktree_records.GitRunner) -> "tuple[int, int]":
    """`rev-list --left-right --count <left>...<right>` → (left 에만 있는 수, right 에만 있는 수)."""
    rc, out = git_runner(["rev-list", "--left-right", "--count", f"{left}...{right}"])
    parts = str(out).split()
    if rc != 0 or len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise BranchParentError(
            f"`git rev-list --left-right --count {left}...{right}` 실패 (rc={rc}): "
            f"{str(out).strip()[:200]}"
        )
    return int(parts[0]), int(parts[1])


def _resolve_parent_ref(parent: str, *, git_runner: worktree_records.GitRunner) -> "tuple[str | None, tuple[int, int] | None]":
    """`parent_ref` 본체 — (비교 ref, 원격·로컬이 둘 다 있을 때의 (origin 전용 수, 로컬 전용 수))."""
    has_origin = _ref_exists(f"refs/remotes/origin/{parent}", git_runner=git_runner)
    has_local = _ref_exists(f"refs/heads/{parent}", git_runner=git_runner)
    if not has_origin and not has_local:
        return None, None
    if not has_local:
        return f"origin/{parent}", None
    if not has_origin:
        return parent, None
    counts = _left_right_counts(f"origin/{parent}", parent, git_runner=git_runner)
    return (parent if counts[1] > 0 else f"origin/{parent}"), counts


def parent_ref(parent: str, *, git_runner: worktree_records.GitRunner) -> "str | None":
    """부모와 비교할 ref — origin 만 있으면 `origin/<p>`·로컬만 있으면 `<p>`·둘 다 있으면 로컬이
    origin 에 없는 커밋을 가질 때만 `<p>`(아니면 `origin/<p>`)·둘 다 없으면 None(부모 브랜치 지워짐).

    풀 bare 의 로컬 heads 는 `fetch` 로 움직이지 않으므로(갱신 대상은 remote-tracking ref 뿐) origin 에
    정기 push 되는 브랜치는 origin 이 최신이고, origin 에 안 올리는 통합 브랜치에서만 로컬이 최신이다."""
    return _resolve_parent_ref(parent, git_runner=git_runner)[0]


class BranchDeletion:
    """`delete_branch_by_rule` 결과 — 행동·사유와 사람이 다음 판단에 쓸 근거.

    - `branch` · `action`(BRANCH_DELETED / BRANCH_PRESERVED) · `reason`(BRANCH_REASON_*).
    - `parent` — 부모 기록의 부모 이름(판정이 그 앞에서 멈췄으면 None).
    - `parent_ref` — 합쳐짐을 잰 비교 ref(`parent_ref` 결과·재지 않았으면 None).
    - `children` — 자식 브랜치 목록(children 사유일 때).
    - `unmerged_commits` — `<parent_ref>..<끝 커밋>` 의 `<짧은 sha> <제목>` 줄 목록(unmerged 보존·
      acked 삭제·그 뒤 단계에서 멈춘 보존).
    - `origin` — ORIGIN_* (unmerged 보존일 때만·그 밖 None).
    - `detail` — git 출력 첫 줄(delete-refused) 또는 끝 커밋 변화(tip-moved).

    (dataclass 미사용 — RemoveResult 와 같은 이유.)
    """

    def __init__(self, branch: str, *, action: str, reason: str, parent: "str | None" = None,
                 parent_ref: "str | None" = None, children: "list[str] | None" = None,
                 unmerged_commits: "list[str] | None" = None, origin: "str | None" = None,
                 detail: "str | None" = None):
        self.branch = branch
        self.action = action
        self.reason = reason
        self.parent = parent
        self.parent_ref = parent_ref
        self.children = list(children or [])
        self.unmerged_commits = list(unmerged_commits or [])
        self.origin = origin
        self.detail = detail

    def summary(self) -> str:
        """행동·사유 한 줄 — 슬롯 제거 CLI 가 내는 문구다."""
        head = f"브랜치 {self.branch}"
        count = len(self.unmerged_commits)
        if self.action == worktree_records.BRANCH_DELETED:
            if self.reason == worktree_records.BRANCH_REASON_ACKED:
                return f"{head} 삭제 (합쳐지지 않은 커밋 {count}개 · 사용자 승인값)"
            return f"{head} 삭제 (부모 {self.parent_ref} 에 다 합쳐짐)"
        origin_text = {
            worktree_records.ORIGIN_NO_BRANCH: "origin 에 브랜치 없음",
            worktree_records.ORIGIN_CONTAINS_TIP: "origin 에 끝 커밋까지 있음",
            worktree_records.ORIGIN_MISSING_TIP: "origin 브랜치에 끝 커밋 없음",
        }
        reason_text = {
            worktree_records.BRANCH_REASON_PROTECTED: "보호 브랜치",
            worktree_records.BRANCH_REASON_DEFAULT: "공용 저장소 기본 브랜치",
            worktree_records.BRANCH_REASON_CHILDREN: f"자식 브랜치 {', '.join(self.children)}",
            worktree_records.BRANCH_REASON_NO_PARENT: "부모 기록 없음 · 합쳐짐 판정 불가",
            worktree_records.BRANCH_REASON_PARENT_GONE: f"부모 브랜치 {self.parent} 없음 · 합쳐짐 판정 불가",
            worktree_records.BRANCH_REASON_UNMERGED: (f"부모 {self.parent_ref} 에 합쳐지지 않은 커밋 {count}개 · "
                                     f"{origin_text.get(self.origin, self.origin)}"),
            worktree_records.BRANCH_REASON_TIP_MOVED: f"판정 뒤 끝 커밋이 바뀜 · {self.detail}",
            worktree_records.BRANCH_REASON_DELETE_REFUSED: f"git branch -D 거부 · {self.detail}",
        }
        return f"{head} 보존 ({reason_text[self.reason]})"

    def __repr__(self) -> str:
        return (f"BranchDeletion(branch={self.branch!r}, action={self.action!r}, "
                f"reason={self.reason!r}, parent={self.parent!r}, parent_ref={self.parent_ref!r}, "
                f"children={self.children!r}, unmerged_commits={self.unmerged_commits!r}, "
                f"origin={self.origin!r}, detail={self.detail!r})")


def _shared_default_branch(*, git_runner: worktree_records.GitRunner) -> str:
    """공용 git 저장소의 기본 브랜치(HEAD) 이름 — worktree 에 묶인 runner 여도 worktree HEAD 가 아니다.

    `rev-parse --git-common-dir` 로 공용 git 디렉터리를 얻고 그 HEAD 를 읽는다(bare 는 `.`).
    full ref(`refs/heads/<name>`)를 받아 그 접두만 정확히 제거한다 — `symbolic-ref --short` 는
    같은 이름의 태그가 있으면 `heads/<name>` 을 돌려준다(실측 · git 2.43).

    조회 실패와 브랜치가 아닌 출력(detached HEAD)은 판정 실패라 raise 한다. 빈 값으로 접으면
    기본 브랜치 보존 축이 조용히 사라진다.
    """
    rc, out = git_runner(["rev-parse", "--git-common-dir"])
    common_dir = str(out).strip()
    if rc != 0 or not common_dir:
        raise BranchParentError(
            f"`git rev-parse --git-common-dir` 실패 (rc={rc}): {common_dir[:200]} — "
            "공용 저장소 기본 브랜치를 읽지 못해 브랜치를 지우지 않았다."
        )
    rc, out = git_runner(["--git-dir", common_dir, "rev-parse", "--symbolic-full-name", "HEAD"])
    ref = str(out).strip()
    if rc != 0 or not ref.startswith(worktree_git._SYMREF_BRANCH_PREFIX):
        raise BranchParentError(
            f"`git --git-dir {common_dir} rev-parse --symbolic-full-name HEAD` 실패 (rc={rc}): "
            f"{ref[:200]} — 공용 저장소 기본 브랜치를 읽지 못해 브랜치를 지우지 않았다."
        )
    return ref[len(worktree_git._SYMREF_BRANCH_PREFIX):]


def _branch_tip(branch: str, *, git_runner: worktree_records.GitRunner) -> "str | None":
    """`refs/heads/<branch>` 의 끝 커밋 sha — 그 브랜치가 없으면 None, 조회 실패는 raise."""
    rc, out = git_runner(["rev-parse", "--verify", "--quiet",
                          f"{worktree_git._SYMREF_BRANCH_PREFIX}{branch}^{{commit}}"])
    value = str(out).strip()
    if rc == 0 and value:
        return value
    if rc == worktree_records._REV_PARSE_MISSING_RC and not value:
        return None
    raise BranchParentError(
        f"브랜치 {branch!r} 끝 커밋 조회 실패 — `git rev-parse --verify` (rc={rc}): {value[:200]}"
    )


def _is_ancestor_or_raise(ancestor: str, descendant: str, *, git_runner: worktree_records.GitRunner) -> bool:
    """`merge-base --is-ancestor` — rc0 참·rc1 거짓·그 밖 raise(판정 실패).

    `_is_ancestor`(head 관계 판정용)는 판정 불가를 False 로 접는다. 삭제 판정에서 그렇게 접으면
    조회 실패가 "합쳐지지 않음" 보존으로 둔갑하므로 이 판정은 따로 raise 한다."""
    rc, out = git_runner(["merge-base", "--is-ancestor", ancestor, descendant])
    if rc == 0:
        return True
    if rc == worktree_records._IS_ANCESTOR_FALSE_RC:
        return False
    raise BranchParentError(
        f"`git merge-base --is-ancestor {ancestor} {descendant}` 실패 (rc={rc}): "
        f"{str(out).strip()[:200]}"
    )


def _commits_between(base: str, tip: str, *, git_runner: worktree_records.GitRunner) -> "list[str]":
    """`<base>..<tip>` 커밋의 `<짧은 sha> <제목>` 줄 목록(최신 먼저). 조회 실패는 raise."""
    rc, out = git_runner(["log", "--format=%h %s", f"{base}..{tip}"])
    if rc != 0:
        raise BranchParentError(
            f"`git log {base}..{tip}` 실패 (rc={rc}): {str(out).strip()[:200]}"
        )
    return [line for line in str(out).splitlines() if line.strip()]


def _origin_state(branch: str, tip: str, *, git_runner: worktree_records.GitRunner) -> str:
    """origin 에 이 브랜치의 끝 커밋이 올라가 있는가 — ORIGIN_* 하나."""
    origin_ref = f"{worktree_records._ORIGIN_BRANCH_PREFIX}{branch}"
    if not _ref_exists(origin_ref, git_runner=git_runner):
        return worktree_records.ORIGIN_NO_BRANCH
    if _is_ancestor_or_raise(tip, origin_ref, git_runner=git_runner):
        return worktree_records.ORIGIN_CONTAINS_TIP
    return worktree_records.ORIGIN_MISSING_TIP


def delete_branch_by_rule(home, 
    branch: str,
    *,
    repo: str,
    acked_branches: "frozenset[str]" = frozenset(),
    git_runner: worktree_records.GitRunner,
) -> BranchDeletion:
    """브랜치 삭제 판정과 삭제 — 슬롯 제거가 쓰는 단일 규칙.

    차례(앞에서 걸리면 그 자리에서 보존한다):
      1. repo 보호 브랜치(`areas_store._repo_protected`) 또는 공용 저장소 기본 브랜치 → 보존.
      2. 자식 브랜치(`branch_children`) 1개 이상 → 보존 + 자식 목록.
      3. 부모 기록 없음 → 보존. 부모 브랜치가 지워져 비교 ref(`parent_ref`)가 없으면 합쳐짐을 잴 수
         없어 보존.
      4. 끝 커밋이 비교 ref 의 조상이 아니고(`merge-base --is-ancestor`) 이 브랜치가 `acked_branches`
         에 없음 → 보존 + 합쳐지지 않은 커밋 목록 + origin 반영 여부.
      5. 그 밖(합쳐짐, 또는 승인값 있음) → 판정 때 읽은 끝 커밋과 지금 `refs/heads/<브랜치>` 가 같을
         때만 `git branch -D`(부모 기록 설정도 git 이 함께 지운다). 다르면 보존, `-D` rc≠0(다른
         worktree 가 쥠 등)이면 보존 + git 출력 첫 줄.

    `git_runner` 는 그 브랜치가 사는 저장소에 묶인 runner 다(bare 든 worktree 든 — 기본 브랜치는
    공용 git 디렉터리에서 읽는다). 판정 의미가 아닌 git 실패는 `BranchParentError` 로 올린다 —
    보존으로 접으면 조회가 깨진 채 "보존" 이 정상 결과처럼 보인다.
    """
    def preserved(reason: str, **fields) -> BranchDeletion:
        return BranchDeletion(branch, action=worktree_records.BRANCH_PRESERVED, reason=reason, **fields)

    if branch in areas_store._repo_protected(home, repo):
        return preserved(worktree_records.BRANCH_REASON_PROTECTED)
    if branch == _shared_default_branch(git_runner=git_runner):
        return preserved(worktree_records.BRANCH_REASON_DEFAULT)
    children = branch_children(branch, git_runner=git_runner)
    if children:
        return preserved(worktree_records.BRANCH_REASON_CHILDREN, children=children)
    record = read_branch_parent(branch, git_runner=git_runner)
    if record is None:
        return preserved(worktree_records.BRANCH_REASON_NO_PARENT)
    parent = record["name"]
    compare_ref = parent_ref(parent, git_runner=git_runner)
    if compare_ref is None:
        return preserved(worktree_records.BRANCH_REASON_PARENT_GONE, parent=parent)
    tip = _branch_tip(branch, git_runner=git_runner)
    if tip is None:
        raise BranchParentError(
            f"브랜치 {branch!r} 가 없다(`refs/heads/{branch}`) — 끝 커밋을 읽지 못해 판정하지 않았다."
        )
    judged = {"parent": parent, "parent_ref": compare_ref}
    unmerged: list[str] = []
    if not _is_ancestor_or_raise(tip, compare_ref, git_runner=git_runner):
        unmerged = _commits_between(compare_ref, tip, git_runner=git_runner)
        if branch not in acked_branches:
            return preserved(worktree_records.BRANCH_REASON_UNMERGED, unmerged_commits=unmerged,
                             origin=_origin_state(branch, tip, git_runner=git_runner), **judged)
    current = _branch_tip(branch, git_runner=git_runner)
    if current != tip:
        return preserved(worktree_records.BRANCH_REASON_TIP_MOVED, unmerged_commits=unmerged,
                         detail=f"판정 때 {tip} → 지금 {current or '브랜치 없음'}", **judged)
    rc, out = git_runner(["branch", "-D", branch])
    if rc != 0:
        first_line = next((line.strip() for line in str(out).splitlines() if line.strip()), "")
        return preserved(worktree_records.BRANCH_REASON_DELETE_REFUSED, unmerged_commits=unmerged,
                         detail=f"rc={rc}: {first_line}", **judged)
    return BranchDeletion(branch, action=worktree_records.BRANCH_DELETED,
                          reason=worktree_records.BRANCH_REASON_ACKED if unmerged else worktree_records.BRANCH_REASON_MERGED,
                          unmerged_commits=unmerged, **judged)


def record_preserved_branch(branch: str, *, slot: str, reason: str, git_runner: worktree_records.GitRunner) -> None:
    """슬롯 제거가 보존한 `branch` 에 쓰던 슬롯·보존 시각·사유를 기록한다. rc≠0 → `BranchParentError`."""
    _write_branch_config(
        branch,
        ((worktree_records.BRANCH_SLOT_KEY, slot), (worktree_records.BRANCH_PRESERVED_AT_KEY, clock.now_utc()),
         (worktree_records.BRANCH_PRESERVED_REASON_KEY, reason)),
        what="보존 기록", git_runner=git_runner)


def clear_preserved_branch(branch: str, *, git_runner: worktree_records.GitRunner) -> None:
    """`branch` 의 보존 기록 세 키를 지운다 — 없는 키는 그대로 두고, 그 밖의 rc≠0 은 `BranchParentError`."""
    for key in worktree_records._PRESERVED_RECORD_KEYS:
        config_key = _branch_config_key(branch, key)
        rc, out = git_runner(["config", "--unset", config_key])
        if rc not in (0, worktree_records._GIT_CONFIG_UNSET_MISSING_RC):
            raise BranchParentError(
                f"브랜치 {branch!r} 보존 기록 지우기 실패 — `git config --unset {config_key}` "
                f"(rc={rc}): {str(out).strip()[:200]}"
            )


def _read_preserved_records(repo: str, *, git_runner: worktree_records.GitRunner) -> "dict[str, dict[str, str]]":
    """공용 git 설정 조회 1회로 `{브랜치: {키: 값}}` — 기록이 하나도 없으면 {}."""
    rc, out = git_runner(["config", "--get-regexp", worktree_records._PRESERVED_RECORD_PATTERN])
    if rc == worktree_records._GIT_CONFIG_MISSING_RC:
        return {}
    if rc != 0:
        raise BranchParentError(
            f"repo {repo!r} 보존 브랜치 조회 실패 — `git config --get-regexp` (rc={rc}): "
            f"{str(out).strip()[:200]}"
        )
    records: dict[str, dict[str, str]] = {}
    for line in str(out).splitlines():
        key, _sep, value = line.partition(" ")
        # 키 = `branch.<브랜치>.<변수>` — 변수 이름에는 `.` 이 없어 마지막 `.` 에서 자른다.
        section, _dot, variable = key.rpartition(".")
        records.setdefault(section[len(worktree_records._BRANCH_CONFIG_PREFIX):], {})[variable] = value
    return records


def _unmerged_count(branch: str, parent: "str | None", *, git_runner: worktree_records.GitRunner) -> "int | None":
    """부모에 합쳐지지 않은 커밋 수 — 부모 기록이 없거나 부모 브랜치가 지워졌으면 None."""
    if parent is None:
        return None
    compare_ref = parent_ref(parent, git_runner=git_runner)
    if compare_ref is None:
        return None
    return _left_right_counts(compare_ref, f"{worktree_git._SYMREF_BRANCH_PREFIX}{branch}", git_runner=git_runner)[1]


def preserved_branches(home) -> "list[dict]":
    """슬롯 없는 보존 브랜치 목록 — `.repos/*.git` 마다 보존 기록을 읽어 repo·이름 순으로 싣는다.

    행 = `{repo, name, slot, preserved_at, reason, parent, unmerged}`. `parent` 는 부모 기록의 부모
    이름(없으면 None), `unmerged` 는 부모에 합쳐지지 않은 커밋 수(`_unmerged_count`).
    세 키 중 일부만 있거나 기록된 브랜치가 없으면 기록 손상이라 `BranchParentError` 다(빈 행으로
    접지 않는다). `git_runner` 주입 시 모든 repo 에 그 runner, 아니면 repo 마다 bare 에 묶인 실 runner."""
    rows: list[dict] = []
    for repo, bare in worktree_git._discover_bares(home):
        runner = worktree_git._real_git_runner(bare)
        for branch, values in sorted(_read_preserved_records(repo, git_runner=runner).items()):
            missing = [key for key in worktree_records._PRESERVED_RECORD_KEYS if key not in values]
            if missing:
                raise BranchParentError(
                    f"repo {repo!r} 브랜치 {branch!r} 보존 기록 손상 — {', '.join(missing)} 가 없다"
                )
            if _branch_tip(branch, git_runner=runner) is None:
                raise BranchParentError(
                    f"repo {repo!r} 보존 기록 손상 — 기록된 브랜치 {branch!r} 가 없다"
                    f"(`refs/heads/{branch}`)"
                )
            record = read_branch_parent(branch, git_runner=runner)
            parent = record["name"] if record is not None else None
            rows.append({
                "repo": repo,
                "name": branch,
                "slot": values[worktree_records.BRANCH_SLOT_KEY],
                "preserved_at": values[worktree_records.BRANCH_PRESERVED_AT_KEY],
                "reason": values[worktree_records.BRANCH_PRESERVED_REASON_KEY],
                "parent": parent,
                "unmerged": _unmerged_count(branch, parent, git_runner=runner),
            })
    return rows


def _origin_or_local_base_ref(base_branch: str, *, slot: str, runner: worktree_records.GitRunner) -> str:
    """`origin/<base_branch>` 대 로컬 `<base_branch>` 신선도 해소 — 무인자(`onto` 생략) 공용 helper.

    `rebase`(base 미지정)와 `refresh`(onto 미지정) 두 하위명령이 같은 인자에 반대 규칙을 쓰던 결함을
    닫는다 — 둘 다 이 helper 하나로 대상 ref 를 해소해, 같은 도구·같은 인자에 규칙이 갈리지 않는다.

    판정은 `parent_ref` 와 같다(좌우 수 계산은 `_resolve_parent_ref` 한 곳). 차이는 둘:
      - `origin/<base_branch>` 가 없으면(로컬도 없는 경우 포함) `base_branch` 를 그대로 돌려준다
        (호출부 refresh/rebase 가 그 이름으로 git 을 불러 실패를 드러낸다).
      - 원격·로컬이 둘 다 있고 로컬이 origin 에 없는 커밋을 가져 로컬을 고르면 stderr 1줄에 좌(origin
        전용)·우(로컬 전용) 두 수를 함께 고지한다(diverged 여도 조용한 선택이 아니다)."""
    ref, counts = _resolve_parent_ref(base_branch, git_runner=runner)
    if ref is None:
        return base_branch
    if counts is not None and ref == base_branch:
        origin_ahead, local_ahead = counts
        print(
            f"[정보] 슬롯 {slot} 로컬 브랜치 `{base_branch}` 이 `origin/{base_branch}` 보다 앞서 "
            f"있다(origin 전용 {origin_ahead}커밋 · 로컬 전용 {local_ahead}커밋) — origin 대체를 "
            "건너뛰고 로컬 tip 을 쓴다.",
            file=sys.stderr,
        )
    return ref


def _resolve_base_commit(home, slot: str, ref: str, *, git_runner: worktree_records.GitRunner | None = None) -> "str | None":
    """`ref`(브랜치 또는 커밋)의 커밋 sha 를 슬롯 worktree 에서 해소한다 (rebase `--onto` 검증·fork).

    `git rev-parse --verify <ref>^{commit}` — 브랜치면 tip, 커밋이면 그 커밋. 해소 불가(rc≠0)·슬롯
    부재 → None, runner 예외는 전파한다. 실경로(runner 미주입)는 슬롯 worktree 바인딩 runner, 슬롯 부재면 None
    (`_slot_head` 동형 규율)."""
    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            return None
        runner = worktree_git._real_git_runner(p)
    rc, out = runner(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if rc != 0:
        return None
    return (out or "").strip() or None


def _slot_parent_status(branch: "str | None", lease: "Lease | None", *,
                        git_runner: worktree_records.GitRunner, shared: dict) -> "dict | None":
    """슬롯 행 `parent` — `{name, fork, ref, ahead, behind}` 또는 None(기록 없음·브랜치 없음).

    작업 슬롯은 현재 브랜치의 git 설정 부모 기록, readonly 슬롯은 브랜치가 없어 장부 `lease.git.base`
    (따라갈 부모 브랜치·detach 한 커밋)를 쓴다. ahead=슬롯에만 있는 커밋 수·behind=부모에만 있는 수.
    부모 브랜치가 지워져 비교 ref 가 없으면 ahead/behind None. 부모 기록과 부모 이름별 비교 ref 는
    `shared`(`slot_git_status` 의 저장소 공용 값)에서 처음 한 번만 읽고 ahead/behind 는 슬롯마다 잰다."""
    if lease is not None and lease.role == pool_store._LEASE_ROLE_READONLY:
        base = lease.git.get("base") if isinstance(lease.git, dict) else None
        if not isinstance(base, dict) or not base.get("branch"):
            return None
        record = {"name": base["branch"], "fork": base.get("commit")}
    elif branch is None:
        return None
    else:
        record = _parent_record(branch, _shared_parent_records(shared, git_runner))
        if record is None:
            return None
    refs = shared["refs"]
    if record["name"] not in refs:
        refs[record["name"]] = parent_ref(record["name"], git_runner=git_runner)
    ref = refs[record["name"]]
    ahead = behind = None
    if ref is not None:
        ahead, behind = _left_right_counts("HEAD", ref, git_runner=git_runner)
    return {"name": record["name"], "fork": record["fork"], "ref": ref,
            "ahead": ahead, "behind": behind}


def _stash_subjects(git_runner: worktree_records.GitRunner) -> "list[str]":
    """stash 제목 목록(`stash list --format=%gs`) — `refs/stash` 는 저장소 공용이라 작업 폴더마다 같다."""
    rc, out = git_runner(["stash", "list", "--format=%gs"])
    if rc != 0:
        raise BranchParentError(
            f"`git stash list` 실패 (rc={rc}): {str(out).strip()[:200]}"
        )
    return str(out).splitlines()


def _branch_stash_count(branch: str, subjects: "list[str]") -> int:
    """이 브랜치 이름으로 쌓인 stash 수 — 제목이 `WIP on <b>:`·`On <b>:`."""
    prefixes = tuple(prefix.format(branch=branch) for prefix in worktree_records._STASH_SUBJECT_PREFIXES)
    return sum(1 for line in subjects if line.startswith(prefixes))


def _shared_parent_records(shared: dict, git_runner: worktree_records.GitRunner) -> "dict[str, dict[str, str]]":
    """저장소 공용 값 `shared` 의 부모 기록 전체 — 처음 한 번만 읽는다(실패한 읽기는 남지 않는다)."""
    if "parents" not in shared:
        shared["parents"] = _branch_parent_records(git_runner)
    return shared["parents"]


def _shared_stash_subjects(shared: dict, git_runner: worktree_records.GitRunner) -> "list[str]":
    """저장소 공용 값 `shared` 의 stash 제목 목록 — 처음 한 번만 읽는다(실패한 읽기는 남지 않는다)."""
    if "stash" not in shared:
        shared["stash"] = _stash_subjects(git_runner)
    return shared["stash"]


def _normalize_branch_name(branch: str, *, git_runner: worktree_records.GitRunner) -> "str | None":
    """`branch` 를 **git 이 실제로 해석할 브랜치명**으로 정규화한다 — `check-ref-format --branch` 출력.

    task/세션 명이 그대로 브랜치명으로 흐르므로(`fix^2`·`a..b`·공백 등) **git 자신의 판정**으로
    거른다(자체 정규식 재구현 금지·규칙 drift 0). 단 이 커맨드는 rc 만 쓰면 안 된다 — `--branch` 는
    유효성 판정 + **revspec 확장**을 함께 한다(`@{-1}` → 이전 브랜치의 실명·rc 0·실측):

        $ git check-ref-format --branch '@{-1}'   →  main   (rc 0)
        $ git switch '@{-1}'                      →  실제로 main 으로 전환

    rc 만 보고 **원문 문자열**로 보호목록을 비교하면 `@{-1}` ≠ `main` 이라 통과하고, 그 결과 이
    커맨드가 스스로 슬롯을 보호 브랜치에 앉힌다(codex 게이트 must-fix — 이 티켓이 막으려던 상태를
    remedy 가 만든다). 그래서 **출력(정규화된 이름)을 단일 기준**으로 삼고 이후 전 단계(보호목록
    비교·존재 판정·D/F 검사·`git switch` 인자·장부 스냅 기록)에 일관 적용한다. 거부 목록(`@{` 금지)
    방식은 다른 revspec 문법에서 채택하지 않는다(정규화가 클래스를 닫는다).

    반환 = 정규화된 브랜치명. 아래는 전부 None(**fail-closed** 거부 — 정규화 불가 = 안전검사 불가라
    통과시키지 않는다. 옛 `_branch_ref_name_ok` 의 "판정 불가는 통과" fail-soft 를 뒤집는다):
      - rc≠0(ref 규칙 위반). runner 예외는 전파한다.
      - 출력이 비었거나 여러 줄·공백 포함(예상 밖 형태 방어).
      - **고정점 아님** — 정규화 결과를 한 번 더 통과시켰을 때 자기 자신이 아니면 거부한다
        (`--branch` rc 0 이 곧 안전은 아니라는 가정).

    ⚠ **이 함수가 브랜치명 수용 규칙의 단일 진실**이다 — 0단계 remedy 의 *후보 생성* 쪽
    (`bootstrap_slots._remedy_branch_ref_ok` → `_remedy_branch_name`)도 판정을 따로 구현하지 않고 이
    함수를 호출한다(제안 쪽과 실행 쪽이 갈리면 안내가 곧바로 실행 불가·codex 게이트 must-fix)."""
    def resolve(name: str) -> "str | None":
        rc, out = git_runner(["check-ref-format", "--branch", name])
        if rc != 0:
            return None
        text = (out or "").strip()
        if not text or len(text.splitlines()) != 1 or any(ch.isspace() for ch in text):
            return None
        return text

    first = resolve(branch)
    if first is None:
        return None
    return first if resolve(first) == first else None   # 고정점 확인.


def _local_branch_exists(branch: str, *, git_runner: worktree_records.GitRunner) -> bool:
    """슬롯 worktree 에 로컬 브랜치 `refs/heads/<branch>` 가 있는가 — switch `not-found`·task branch `exists` 판정.

    `git show-ref --verify --quiet refs/heads/<branch>` rc 0 = 존재, rc≠0 은 미존재.
    runner 예외는 전파한다."""
    rc, _out = git_runner(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return rc == 0


def _branch_protected_upstream(branch: str, protected: "list[str]", *,
                               git_runner: worktree_records.GitRunner) -> "str | None":
    """기존 브랜치 `branch` 의 `@{upstream}` 이 **보호 브랜치 원격**이면 그 upstream 명, 아니면 None.

    0단계 main-참조 판정의 **축 2**(`bootstrap_slots._phase0_protected_upstream`·)와 같은 규칙이다:
    upstream `<remote>/<branch>` 의 branch 부분(첫 `/` 이후·`feature/x` 도 보존)이 보호목록이면
    main-참조. 자기 feature 브랜치 추적(`origin/a5`)은 정상 작업 슬롯이라 통과시킨다(오탐 0).

    **기존 브랜치 전환 경로 전용** — `create_task_branch` 가 새로 파는 브랜치는 upstream 이 없다(실측). 이
    검사가 없으면 `switch <slot> <origin/main 추적 브랜치>` 가 통과하고 **다음 0단계가 다시 main-참조로
    막는다** = 이 티켓이 닫으려는 remedy-유발 상태전이의 다른 축(내부 리뷰 should-fix).

    `git rev-parse --abbrev-ref <branch>@{upstream}`(`_upstream_status` 의 branch-지정 변형·rc 우선
    판정 동형 — `_real_git_runner` 는 stdout+stderr 를 합쳐 주므로 미해소 시 out 이 비어있지 않다).
    미해소(rc≠0·빈 이름)·`/` 없는 이름는 None(판정 불가는 통과·오탐 0). runner 예외는 전파한다."""
    rc, out = git_runner(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    name = (out or "").strip()
    if rc != 0 or not name or "/" not in name:
        return None
    tracked = name.split("/", 1)[1]     # origin/main → main · origin/feature/x → feature/x
    return name if tracked in protected else None


def _non_branch_ref_kind(name: str, *, git_runner: worktree_records.GitRunner) -> "str | None":
    """`name` 이 **로컬 브랜치가 아닌 ref**(remote-tracking·태그)로도 해소되는가 — 종류 or None.

    `create_task_branch`(로컬 브랜치 미존재)에서 인자가 `origin/main`·태그명이면 "새 작업 브랜치
    이름" 으로 부적절하고, 옛 plain-checkout 폴백에선 **detached HEAD 이동**으로 빠졌다(실측:
    `git checkout origin/main` → `## HEAD (브랜치 없음)`). 게다가 `origin/main` 은 보호목록 `main`
    과 **문자열이 달라 보호검사를 통과**한다 — 그 창을 인자 단계에서 닫는다(codex 게이트 must-fix).

    축 2(`_branch_protected_upstream`)와 **다른 축**이다: 축 2 는 *기존 로컬 브랜치의 upstream*
    을 보고, 이건 *인자 자체가 로컬 브랜치가 아닌 것*을 가리키는 경우다(둘 다 필요).

    `refs/remotes/<name>` → "remote-tracking" · `refs/tags/<name>` → "태그" · 둘 다 아니면 None.
    runner 예외는 전파한다(`-b` 생성이 최종 방어)."""
    for ref_prefix, kind in (("refs/remotes/", "remote-tracking ref"), ("refs/tags/", "태그")):
        rc, _out = git_runner(["show-ref", "--verify", "--quiet", f"{ref_prefix}{name}"])
        if rc == 0:
            return kind
    return None


def _branch_df_conflict(branch: str, *, git_runner: worktree_records.GitRunner) -> "str | None":
    """`refs/heads/<branch>` 생성이 **접두 부모 ref** 와 D/F 충돌하는가 — 충돌 ref 명 or None.

    브랜치 `task` 가 존재하면 `refs/heads/task/main` 은 `show-ref` 상 **미존재**(→ 생성 경로)
    지만 `git checkout -b` 는 `cannot lock ref … 'refs/heads/task' exists` 로 실패한다(ref 는 파일
    시스템 디렉토리/파일 구조라 같은 이름이 파일이면서 디렉토리일 수 없다). 그래서 선-검사에서
    접두 부모(`a/b/c` → `a`, `a/b`)의 실재를 함께 본다.

    ⚠️ 역방향(디렉토리가 이미 있는데 그 이름의 파일 ref 를 만드는 경우 — `task/main` 존재 시
    `task` 생성)은 여기서 안 본다: 그 경우는 `git checkout -b` 의 loud 실패(→ `git-error`)로 수렴하고
    사유가 git 메시지에 그대로 실린다(선-검사 범위는 remedy 안내가 실제로 부딪히는 클래스에 한정)."""
    parts = branch.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if _local_branch_exists(parent, git_runner=git_runner):
            return parent
    return None


def _branch_checked_out_elsewhere(home, branch: str, slot: str, *,
                                  git_runner: worktree_records.GitRunner) -> "str | None":
    """기존 브랜치 `branch` 를 **이 슬롯이 아닌 다른 worktree** 가 이미 checkout 중이면 그 경로,
    아니면 None — `switch` 의 `checked-out-elsewhere` 선-검사가 쓴다.

    `git worktree list --porcelain`(`_parse_worktree_porcelain` 재사용 — 새 파서 0)을 슬롯의
    git_runner(슬롯이 속한 repo 전체를 보는 `worktree list`라 어느 worktree cwd 로 불러도 같은
    목록이 나온다·DI seam 유지)로 조회해, 대상 브랜치를 보유한 bare-아닌 엔트리 중 **self 가
    아닌** 것을 찾는다. self 판정은 슬롯 id 가 아니라 **경로 값**(`slot_path(slot)` vs porcelain
    경로, 둘 다 `resolve()`)으로 한다 — 장부 슬롯 값이 `_slot_from_worktree_path` 로 되짚어지지
    않는 경우에도 값 비교로는 정확히 self 로 잡힌다.

    fail-open: `worktree list` rc≠0 은 None(판정 생략·runner 예외는 전파) — 이 선-검사가 fail-open
    해도 `_checkout` 프리미티브(plain checkout 만·리셋 없음)가 기존 브랜치 리셋을 막는 최종 방어선이다(불변식 4·
    두 층 중 하나가 죽어도 데이터 유실은 0). 이 함수는 리셋을 막지 않는다 — 진단·명시 사유만
    낸다(정책은 여기, 비파괴 실행은 `_checkout` — 프리미티브가 정책을 알지 않는다)."""
    rc, out = git_runner(["worktree", "list", "--porcelain"])
    if rc != 0:
        return None
    self_path = pm_paths.slot_path(home, slot).resolve()
    for w in worktree_git._parse_worktree_porcelain(home, out):
        if w.bare or w.branch != branch:
            continue
        try:
            holder_path = Path(w.path).resolve()
        except OSError:
            continue
        if holder_path != self_path:
            return w.path
    return None
