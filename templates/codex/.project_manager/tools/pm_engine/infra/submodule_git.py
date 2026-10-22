"""submodule 상태 파싱과 선택적 재동기."""
from __future__ import annotations

import sys
from pathlib import Path

from pm_engine.domain import worktree_records
from pm_engine.infra import sparse_checkout, worktree_git


def _checkout(slot_path_: Path, branch: str, *,
              git_runner: worktree_records.GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 에서 **기존** 브랜치 체크아웃 (브랜치 변경 = 같은 슬롯 재체크아웃).

    `git checkout --no-recurse-submodules <branch>` 하나만 부르고 rc·출력을 그대로 돌려준다(로컬
    브랜치 미존재·타 worktree 점유 등은 rc≠0). 브랜치를 만들지도(`-b`) 리셋하지도(`-B`) 않는다 —
    옛 `-B` 폴백은 타 worktree 가 점유한 기존 브랜치 ref 를 이 슬롯 HEAD 로 리셋했고, `-b` 폴백은
    부모 기록 없는 브랜치를 만들었다. 새 브랜치는 부모를 기록하는 `create_task_branch` 만 만든다.

    **`--no-recurse-submodules`**: 사용자 환경(전역
    `~/.gitconfig` 또는 repo config)에 `submodule.recurse=true` 가 설정돼 있으면 plain
    `git checkout` 이 *selective resync 전에* submodule 을 재귀 갱신해 on-branch(dev) submodule
    을 detached pin 으로 낚아챈다 — dev 파괴.
    checkout 호출에 `--no-recurse-submodules`(git 2.13+·2.43 확인)를 박아 ambient config 를
    override → checkout 은 submodule 을 절대 안 건드리고 `_resync_submodules_selective` 가
    submodule 상태의 **유일 권위**가 된다.
    """
    runner = git_runner or worktree_git._real_git_runner(slot_path_)
    return runner(["checkout", "--no-recurse-submodules", branch])


def _parse_submodule_entries(status_out: str) -> list[tuple[str, str]]:
    """`git submodule status` 출력에서 `(flag, path)` 를 뽑는다 (git 2.43).

    각 라인 형식 = `<flag><40-hex-sha> <path>[ (<describe>)]` — **선두 1글자가 status 플래그**
    (`' '`=index pin 과 일치·`'+'`=working≠pin·`'-'`=미초기화·`'U'`=충돌)다. 플래그는 항상
    라인 첫 글자(`line[0]`)이고(공백 플래그도 포함), sha 뒤 첫 whitespace 이후부터 선택적인
    마지막 ` (<describe>)` 직전까지가 경로다. 경로 자체에는 공백이 올 수 있으므로 전체
    whitespace 토큰 분리는 쓰지 않는다. describe 는 git 2.43 구현상 `' '`/`'+'` 상태에만
    출력되므로 그 두 flag 에서만 걷는다 — `-`(미초기화)·`U`(충돌) 행의 괄호로 끝나는
    경로는 그대로 보존한다. 빈 출력(submodule 없음)·sha/경로 경계가 없는 행은
    건너뛴다.

    `flag` 는 pin↔working 판정에 필요하다(`+`=drift·`' '`=pinned). 경로만
    필요한 호출부(`_resync_submodules_selective`)는 `_parse_submodule_paths` 를 쓴다(경로만 뽑음).
    """
    entries: list[tuple[str, str]] = []
    for line in status_out.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) >= 2:
            flag = line[0]
            path = parts[1]
            if flag in (" ", "+") and path.endswith(")"):
                candidate, separator, describe = path.rpartition(" (")
                if separator and describe[:-1] and not any(
                    char.isspace() for char in describe[:-1]
                ):
                    path = candidate
            entries.append((flag, path))
    return entries


def _parse_submodule_paths(status_out: str) -> list[str]:
    """`git submodule status` 출력에서 submodule 경로(superproject-상대)만 뽑는다 (git 2.43).

    `_parse_submodule_entries` 의 경로만 반환하는 얇은 래퍼 — 플래그가 불필요한 호출부
    (`_resync_submodules_selective`)의 기존 인터페이스를 그대로 보존한다(행동 불변).
    """
    return [path for _flag, path in _parse_submodule_entries(status_out)]


def _submodule_dirty(sub: str, runner: worktree_records.GitRunner) -> bool:
    """submodule 워킹트리에 미커밋 변경(untracked 포함)이 있는지 — `_is_dirty` 동형(sub 컨텍스트).

    superproject-바인딩 runner 로 `git -C <sub> status --porcelain` 을 돌려 그 submodule
    워킹트리를 본다(다중 `-C` = superproject cwd 기준 sub 로 재진입). porcelain 엔트리 라인이
    하나라도 있으면 dirty. rc≠0(상태 조회 실패)은 **보수적으로 dirty** 로 본다 — `_is_dirty`
    와 같은 규율(모르면 안 날린다·미커밋 작업 유실 방지). stderr 경고 오탐은
    `_porcelain_status_lines` 로 걸러진다(`_is_dirty` 와 동일 가드).
    """
    rc, out = runner(["-C", sub, "status", "--porcelain"])
    if rc != 0:
        return True
    return len(worktree_git._porcelain_status_lines(out)) > 0


def _upstream_status(runner: worktree_records.GitRunner) -> tuple[str | None, bool]:
    """슬롯 브랜치의 `@{upstream}` 추적 브랜치명 + 해소 여부.

    `git rev-parse --abbrev-ref @{upstream}` — 해소되면 rc0 + 추적 브랜치명(예 `origin/a5`),
    미설정이면 rc≠0(`fatal: no upstream configured …`)이다. **rc 를 먼저 본다** — `_real_git_runner`
    가 stdout+stderr 를 합쳐 돌려주므로미해소 시 out 이 fatal 메시지로 *비어있지 않다*.
    rc≠0 또는 빈 이름이면 `(None, False)`(미해소·부트스트랩이 경고), 해소면 `(name, True)`.
    """
    rc, out = runner(["rev-parse", "--abbrev-ref", "@{upstream}"])
    name = out.strip()
    if rc != 0 or not name:
        return None, False
    return name, True


def _classify_unset_submodules(runner: worktree_records.GitRunner, entries: list[tuple[str, str]], *,
                               target: str) -> dict[str, str]:
    """비활성 항목을 폴더 밖, 채워짐, 초기화 안 됨으로 분류한다.

    빈 폴더에서 git은 상위 저장소를 읽으므로 show-prefix가 빈 출력인 경로만 채워짐으로 본다.
    정상 활성 항목은 추가 git 호출 없이 기존 판정을 따른다.
    """
    unset = [path for flag, path in entries if flag == "-"]
    if not unset:
        return {}
    tags = sparse_checkout._index_gitlink_tags(runner, target=target, paths=unset)
    result: dict[str, str] = {}
    for sub in unset:
        if tags.get(sub) == sparse_checkout._SKIP_WORKTREE_TAG:
            result[sub] = "outside"
            continue
        rc, out = runner(["-C", sub, "rev-parse", "--show-prefix"])
        result[sub] = "populated" if rc == 0 and not str(out).strip() else "uninitialized"
    return result


def _submodule_statuses(runner: worktree_records.GitRunner, *, target: str) -> list[worktree_records.SubmoduleStatus]:
    """각 submodule 을 역할별로 판정한 `SubmoduleStatus` 리스트.

    `_resync_submodules_selective` 와 *같은* primitive 로 역할을 정한다(중복 판별 구현 금지):
    `git submodule status`(`_parse_submodule_entries` — flag+path) + submodule 당
    `git -C <sub> symbolic-ref -q HEAD`(rc0=on-branch/dev·rc≠0=detached) + `_submodule_dirty`.

      - on-branch(dev) → `"dev-ahead"`(정보). 사용자가 그 submodule 에서 브랜치를 파 작업 중.
      - detached & flag `-`(미초기화) → `"uninitialized"`(경고·슬롯 init 비정상).
      - detached & flag 공백(pin==working) → `"pinned"`(정상).
      - detached & 그 외 flag(`+`/`U`·pin≠working) → `"drift"`(경고).

    fail-soft: `git submodule status` rc≠0(조회 불가/submodule 없음)이면 **빈 리스트**
    (부트스트랩이 submodule 줄 생략). dirty 는 *왜* drift 가 안 풀렸는지 surface 용
    dirty detached 를 재동기 skip → drift 잔존).
    """
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return []  # 조회 불가(대개 submodule 없음/손상) → 빈 목록(부트스트랩 줄 생략).
    statuses: list[worktree_records.SubmoduleStatus] = []
    entries = _parse_submodule_entries(out)
    classified = _classify_unset_submodules(runner, entries, target=target)
    for flag, sub in entries:
        kind = classified.get(sub)
        if kind == "outside":
            continue
        if kind == "uninitialized":
            statuses.append(worktree_records.SubmoduleStatus(sub, kind, warning=True, dirty=False))
            continue
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            kind = "dev-ahead"       # on-branch = dev 역할(정보·경고 아님).
        elif flag == "-":
            rc_pin, pin = runner(["rev-parse", f"HEAD:{sub}"])
            rc_work, work = runner(["-C", sub, "rev-parse", "HEAD"])
            kind = "pinned" if rc_pin == rc_work == 0 and pin.strip() == work.strip() else "drift"
        elif flag == " ":
            kind = "pinned"          # detached & pin == working(정상).
        else:
            kind = "drift"           # detached & pin ≠ working('+'/'U' → 경고).
        # dirty 는 미초기화 submodule 엔 무의미하다(워킹트리 부재 → `status --porcelain` rc≠0 을
        # `_submodule_dirty` 가 보수적 True 로 → `⚠ uninitialized ·dirty` 잉여 렌더). uninitialized
        # 는 skip(False)·나머지만 `_submodule_dirty` 재사용(불필요한 `-C status` 호출도 절약·reviewer).
        dirty = False if kind == "uninitialized" else _submodule_dirty(sub, runner)
        statuses.append(
            worktree_records.SubmoduleStatus(sub, kind, warning=kind in ("drift", "uninitialized"), dirty=dirty)
        )
    return statuses


def _resync_submodules_selective(slot_path_: Path, *, git_runner: worktree_records.GitRunner | None = None) -> None:
    """브랜치 전환(`_checkout`) 성공 후 submodule 을 **선택적으로** superproject pin 에 재동기.

    worktree 풀 슬롯의 브랜치를 바꾸면 superproject 는 새 브랜치의 submodule pin 을 가리키지만
    submodule 워킹트리는 이전 pin 그대로라 drift 가 생긴다. 브랜치 전환
    직후 각 submodule 을 **역할별로** 재동기한다 — 역할은 별도 장부 없이 submodule 의 live git
    HEAD 로 판별한다(무스키마·기본 A):

      - **on-branch submodule (= dev 역할·`symbolic-ref -q HEAD` rc0)**: **skip**. 사용자가 그
        submodule 에서 브랜치를 파 작업 중이므로 detached pin 으로 낚아채지 않는다(전역
        `submodule.recurse=true` 가 dev 브랜치를 파괴하던 크럭스 A → *selective* 인 이유).
      - **detached submodule (= consume 역할·`symbolic-ref -q HEAD` rc≠0)**: superproject pin
        으로 `git submodule update --init --recursive --force -- <sub>` 재동기. 단 워킹트리가
        **dirty(미커밋 변경)면 skip + 경고** — 재동기가 미커밋 작업을 날리지 않게 보호.
      - **submodule 없는 repo**: `git submodule status` 가 rc0·빈 출력 → no-op.

    fail-soft(브랜치 전환은 이미 성공했고 submodule 재동기 실패가 checkout 을 되돌리지 않는다·
    raise 금지): `submodule status` rc≠0(조회 불가)면 no-op 반환. update 실패는 경고만 하고
    drift 잔존을 surface 한다(침묵 무력화 금지). `git_runner` 미주입 시 슬롯 worktree 바인딩
    `_real_git_runner(slot_path_)` 로 해소(기존 DI seam 패턴·`_is_dirty` 동형) — 모든 submodule
    git 호출을 이 superproject-바인딩 runner + `-C <sub>` 로 돌려 테스트 mock 가능(hermetic).
    """
    runner = git_runner or worktree_git._real_git_runner(slot_path_)
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return  # 조회 불가(대개 submodule 없음/손상) → no-op fail-soft(checkout 은 이미 성공).
    entries = _parse_submodule_entries(out)
    try:
        classified = _classify_unset_submodules(runner, entries, target=str(slot_path_))
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[경고] submodule 분류 실패: {exc}", file=sys.stderr)
        return
    for _flag, sub in entries:
        kind = classified.get(sub)
        # 채워진 비활성 submodule은 초기화하지 않는다(공용 active 설정과 작업 보존).
        if kind in ("outside", "populated"):
            continue
        # 역할 = live git HEAD(장부 없음). on-branch(dev) → 보호(skip).
        rc_head, _out = ((1, "") if kind == "uninitialized"
                         else runner(["-C", sub, "symbolic-ref", "-q", "HEAD"]))
        if rc_head == 0:
            continue
        # detached(consume) → pin 재동기. 단 dirty(미커밋)면 작업 유실 방지 위해 skip + 경고.
        if kind != "uninitialized" and _submodule_dirty(sub, runner):
            print(
                f"[경고] submodule {sub!r} 이 detached 이나 미커밋 변경으로 dirty — pin "
                f"재동기 skip (작업 보호). 정리 후 재-alloc 하면 재동기된다.",
                file=sys.stderr,
            )
            continue
        rc_up, out_up = runner(
            ["submodule", "update", "--init", "--recursive", "--force", "--", sub]
        )
        if rc_up != 0:
            print(
                f"[경고] submodule {sub!r} pin 재동기 실패 (rc={rc_up}): "
                f"{str(out_up).strip()[:200]} — drift 잔존 가능(fail-soft·checkout 은 성공).",
                file=sys.stderr,
            )
