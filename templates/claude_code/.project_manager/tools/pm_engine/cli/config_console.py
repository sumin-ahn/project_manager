"""대화형 콘솔 상태 표시와 작업 공간 입력."""
from __future__ import annotations

import argparse
from typing import Callable
from pm_engine.infra import areas_store, lease_store
from pm_engine.infra import git
from pm_engine.infra import pool_store
from pm_engine.cli import config_repo
from pm_engine.cli import config_slots


# ── 대화형 콘솔 ──────────────────────────────────────────────────────
# 무인자(tty) `pm-config` 의 휴먼 프론트엔드. 상태를 렌더하고 메뉴로 액션을 받고
# 입력마다 바뀐 상태를 재렌더한다. 액션은 모두 *기존 핸들러*(cmd_repo_add·
# cmd_worktree_add·cmd_set_test_cmd)로 위임한다 — 콘솔은 얇은 셸(동작 분기 0·중복 0).
# 커맨드형 CLI와 같은 소유 함수를 호출한다. 경로는 Home, 입력은 input_fn으로 전달한다.


def _render_repos(home) -> None:
    """areas.md per-repo 레지스트리 행을 surface 한다 (`areas_store._parse_areas` 재사용).

    areas.md 부재(미배선)면 빈 안내. 중복 파싱 구현 0 — 헤더-인식 파서를 그대로 쓴다.
    """
    print("## repos (areas.md per-repo 레지스트리):")
    _header, rows = areas_store._parse_areas(home)
    if not rows:
        print("  (등록된 repo 없음 — [r] 로 추가하라)")
        return
    for row in rows:
        name = row.get("repo") or row.get("prefix") or "(?)"
        print(
            f"  - {name} · prefix={row.get('prefix') or '-'} · "
            f"git={row.get('git') or '-'} · test_cmd={row.get('test_cmd') or '(미지정)'} · "
            f"owner={row.get('owner') or '-'} · base={row.get('base') or '(bare HEAD)'}"
        )


def _render_slots(home) -> None:
    """worktree 풀 리스 장부를 slot·repo·build(test_cmd)·state·session 으로 surface.

    pool_store.list_leases(home) 재사용. 빈 장부면 안내를 표시한다. cmd_status의 풀
    surface 와 같은 데이터 — 콘솔은 build(test_cmd) 칼럼을 강조한다(이 콘솔의 1급 관심사).
    """
    print("## slots (worktree 풀 리스):")
    leases = pool_store.list_leases(home)
    if not leases:
        print("  (슬롯 없음 — [w] 로 추가하라)")
        return
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · build={l.test_cmd or '(미지정)'} · "
            f"state={l.state} · session={l.session or '-'}"
        )


def _render_state(home) -> None:
    """콘솔 상태 1회 렌더 — repos(areas) + slots(리스). 액션마다 재호출(재렌더)."""
    print()
    print(f"# pm-config 콘솔 — 세션: {lease_store.session_name(home) or '(비바인딩)'}")
    _render_repos(home)
    _render_slots(home)


# 콘솔 프롬프트 중단(EOF/Ctrl-C) sentinel — `_console_input` 이 예외 대신 이걸 돌려준다.
# 호출부(메뉴 루프·각 액션)는 `is _CONSOLE_ABORT` 로 판정해 일관되게 취소/종료한다.
# `None`/`""` 과 구별돼야(그건 정상 빈입력 의미) — 고유 sentinel 객체로 둔다.
_CONSOLE_ABORT = object()


def _console_input(input_fn: Callable[[str], str], prompt: str):
    """콘솔 공유 입력 헬퍼 — `EOFError`/`KeyboardInterrupt` 를 `_CONSOLE_ABORT` 로 흡수.

    **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]` 이름/git/test·`[w]` repo·`[b]` slot/
    빌드명령)가 이 헬퍼를 거친다 — 어느 프롬프트서 Ctrl-C/EOF 가 나도 예외가 전파돼 크래시
    하는 것을 막는다(must-fix 2·codex — "우아 종료/크래시 0" 보장). 반환:
      - 정상 입력 → `str`(strip 안 함 — 호출부가 의미에 맞게 strip; 빈입력 보존).
      - EOF/Ctrl-C → `_CONSOLE_ABORT`(호출부가 액션 취소/메뉴 복귀 또는 루프 종료).
    input_fn 주입으로 hermetic(라이브 input 블록 0).
    """
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return _CONSOLE_ABORT


def _console_repo_add(home, input_fn):
    """`[r]` — repo 이름/git/test/base 를 프롬프트로 받아 cmd_repo_add 위임 (기존 핸들러 재사용).

    각 프롬프트가 `_console_input` 을 거쳐 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 를 받는다 —
    그 경우 이 액션 자체가 `_CONSOLE_ABORT` 를 *반환*해 run_console 이 루프를 우아 종료한다
    (must-fix 2 — 액션 내부 중단도 traceback 0·rc 0). 빈입력은 취소(None 반환·메뉴 복귀).
    """
    name = _console_input(input_fn, "repo 이름 (= prefix): ")
    if name is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    name = name.strip()
    if not name:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    git = _console_input(input_fn, "git URL: ")
    if git is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    git = git.strip()
    if not git:
        print("  (git URL 비어 있음 — 취소)")
        return None
    test = _console_input(
        input_fn, "test 명령 (빈 입력 = 미지정·나중에 worktree/[b] 에서 설정): "
    )
    if test is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    test = test.strip()
    base = _console_input(input_fn, "base 브랜치 (빈 입력 = 기본 브랜치 사용): ")
    if base is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    base = base.strip()
    # 빈입력 → base=None(= CLI `--base` 생략 = bare HEAD 기본·기존 동작). 비어있지 않으면
    # 그 브랜치명을 cmd_repo_add 로 — 형식/존재 검증은 콘솔이 따로 안 하고 `_resolve_base`
    # 의 `show-ref --verify`단일 sink 가 거른다(중복 검사 0).
    args = argparse.Namespace(
        name=name, git=git, test=(test or None), owner=None, base=(base or None),
        user=None,  # area_owner 는 cmd_repo_add 가 local.conf identity.user= / git email 로 해소.
    )
    config_repo.cmd_repo_add(home, args)
    return None


def _console_worktree_add(home, input_fn):
    """`[w]` — repo 를 받아 cmd_worktree_add 위임. 빌드명령은 그 핸들러가 프롬프트(tty 경로).

    repo 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). 빈입력은 취소.
    물리 슬롯 생성 전에 repo 이름을 한 번 더 입력하는 사용자-present 확인 프롬프트를
    띄우고, 정확히 일치한 값만 CLI ``--user-ack <repo>``와 동치인 토큰으로 넘긴다.
    빌드명령 프롬프트의 표시 기본값은 명시 Home의 areas_store에서 읽는다. 프롬프트 내부 중단은
    cmd_worktree_add → _prompt_test_cmd 가 None 으로 흡수(크래시 0·기존 폴백).

    이 액션은 핸들러 rc 를 읽지 않는다(취소/중단 sentinel 만 반환) — 그래서 엔진 사본 불일치
    (marked engine skew)는 rc 가 아니라 예외로 올라와야 루프가 끝난다. cmd_worktree_add 가
    그 경계를 재전파하고 `main` 이 CLI 와 같은 문구·rc 로 번역한다.
    """
    repo = _console_input(input_fn, "슬롯을 만들 repo 이름: ")
    if repo is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    repo = repo.strip()
    if not repo:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    confirmation = _console_input(
        input_fn,
        f"사용자 승인 확인 — 생성할 repo 이름 {repo!r}을 다시 입력: ",
    )
    if confirmation is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    if confirmation.strip() != repo:
        print("  [중단] repo 재입력이 일치하지 않아 슬롯 생성을 취소했습니다.")
        return None
    # 콘솔은 항상 대화형(tty 전제) → cmd_worktree_add 가 빌드명령 프롬프트를 띄우게
    # is_tty=lambda: True 로 강제(콘솔 진입 자체가 tty 보장·main 분기). --test 는 미지정.
    args = argparse.Namespace(repo=repo, test=None, user_ack=repo)
    config_slots.cmd_worktree_add(home,
        args, input_fn=input_fn, is_tty=lambda: True
    )
    return None


def _console_set_test_cmd(home, input_fn):
    """`[b]` — slot·새 빌드명령을 받아 cmd_set_test_cmd 위임 (worktree_pool.set_test_cmd).

    slot·빌드명령 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). slot 빈입력은
    취소. 빌드명령 빈입력은 None(바인딩 해제·현행).
    """
    slot = _console_input(input_fn, "빌드명령을 바꿀 슬롯 (work/<repo>_<N>): ")
    if slot is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    slot = slot.strip()
    if not slot:
        print("  (슬롯 비어 있음 — 취소)")
        return None
    cmd = _console_input(input_fn, "새 빌드/테스트 명령 (빈 입력 = 바인딩 해제): ")
    if cmd is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    cmd = cmd.strip()
    config_slots.cmd_set_test_cmd(home, slot, cmd or None)
    return None
