"""슬롯 값 검증과 worktree 경로 대응."""
from __future__ import annotations

import os
from pathlib import Path

from pm_engine.domain import home, identity_args, worktree_records
from pm_engine.infra import lease_store, pm_paths


def _slot_from_worktree_path(home, path_str: str) -> "str | None":
    """worktree 절대경로 → 슬롯 식별자 `work/<repo>_<N>` (WORK_DIR 하위 단일 컴포넌트만).

    슬롯은 WORK_DIR(=REPO/work) 바로 아래의 단일 디렉토리(`work/<repo>_<N>`)다. bare 원·multi-PM
    루트·중첩 경로는 슬롯이 아니므로 None(슬롯번호/reconcile 에서 제외). 심링크(/tmp 등)는 양쪽을
    `resolve()` 로 정규화해 장부 `slot_path`(REPO/slot) 파생과 일관 매칭한다.
    """
    try:
        p = Path(path_str).resolve()
        rel = p.relative_to(pm_paths.work_dir(home).resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) != 1:
        return None
    return f"work/{rel.parts[0]}"


def _is_pool_worktree_slot(home, slot: str) -> bool:
    """`slot` 값이 **pool worktree 자리**(`WORK_DIR` 직계 하위 한 마디)인가 — 값 판정.

    `_slot_from_worktree_path`(git 목록 → 슬롯 값)의 역방향 술어다. 그 함수가 낼 수 있는 값 域이
    곧 "git worktree 목록과 대조 가능한 자리"라, reconcile 이 그 域 밖의 행을 stale 로 판정하지
    않게 가른다. 이름 모양이 아니라 위치(경로 값)로만 판정한다 — `work/legacy-dir` 도 자리다.
    """
    try:
        relative = (home.root / slot).resolve(strict=False).relative_to(pm_paths.work_dir(home).resolve(strict=False))
    except (ValueError, OSError):
        return False
    return len(relative.parts) == 1


# 슬롯 경로 값의 안전 경계 — **이름 모양이 아니라 값**으로 판정한다. 예전 `_SLOT_ID_RE`
# (`^work/<repo>_<N>$`)는 traversal 방어와 pool 명명 규약을 한 정규식에 묶어, 장부가 정당하게
# 들고 있는 다른 경로(PM 홈 자신을 가리키는 행 `slot="."` 등)를 "형식 오류"로 거부했다. 경계는
# 세 축으로 나눈다 — (1) 구문(빈값·백슬래시·절대경로 + 마디마다 `identity_args.
# path_component_rejection`: 드라이브 표기·Windows 금지 문자·`..`·예약 이름), (2) 위치(PM 홈
# 자신 또는 `work/` 직계 하위 **한 마디**), (3) 해소(실제 경로가 PM 홈 안 — symlink 탈출까지
# 거부·정규식이 못 보던 축). 슬롯 **이름**에 대한 강제는 사라진다(`work/legacy-dir` 통과) —
# 이름 규약(`work/<repo>_<N>`)은 pool **생성**(`_slot_for`)이 소유한다.
#
# (1)의 마디 판정은 **장부 session 키 축과 같은 술어**다(`identity_args.is_slot_key` 가 같은
# 함수를 소비) — 두 축이 갈리면 한쪽이 막는 값이 다른 축으로 들어와 같은 파일 경로 결합에
# 도달한다(리뷰 F-003: `C:_1` 이 session 키로 통과해 `.local/slots/C:_1` 이 Windows 에서 상태
# 루트 밖을 가리켰다).
_SLOT_VALUE_ALT_SEPARATOR = "\\"    # 경로구분자 표기 혼용(백슬래시) — OS 별 해석이 갈려 거부.
_SLOT_HOME_VALUE = "."              # PM 홈 자신을 가리키는 슬롯 값(경로 마디가 없는 슬롯).


def _normalize_slot(home, slot_arg: str) -> str:
    """`--slot` 값을 슬롯 경로 값으로 정규화 + **값-경계 검증**(슬롯 경계 보호).

    스킬/사용자가 `work/A_1`(정규형) 또는 `A_1`(접두 생략) 어느 쪽으로 줘도 받는다 —
    `slot_path`/장부의 슬롯 경로 관례(`_slot_for`)와 정합. 경로 마디가 없는 bare 이름에만
    `work/` 를 붙이고, 이미 경로인 값(`work/A_1`·`.`)은 그대로 정규화한다.

    거부 축(위 주석의 3축):
      - 구문 — 빈값 · 백슬래시 구분자 · 절대경로(`/etc`) · **마디 안전**(드라이브 표기 `C:` ·
        Windows 금지 문자 · 제어문자 · `..` 마디(`../x`·`work/../x`·`work/x/../y`) · Windows
        예약 장치 이름) — 마디 판정은 장부 session 키 축과 **같은 술어**
        (`identity_args.path_component_rejection`)다.
      - 위치 — PM 홈 자신(`.`) 또는 `work/` 직계 하위 한 마디만. `work/`(슬롯들의 부모)·
        `work/A_1/sub`(슬롯 하위) 거부.
      - 해소 — `REPO / slot` 의 실제 해소 경로가 PM 홈 밖이면 거부(symlink 탈출).
    통과 조건은 "PM 홈 안의 슬롯 자리"이며 **이름 모양이 아니다** — 장부가 `slot="."` 을 들고
    있어도 side-effect 는 여전히 홈 경계 안에 갇힌다. 반환은 PM 홈 상대 POSIX 표기 정규형.
    """
    s = slot_arg.strip()
    if not s:
        raise worktree_records.SlotResolutionError(
            "슬롯 값 형식 오류 — 빈값. PM 홈 안의 슬롯 경로(`work/<repo>_<N>` 또는 접두 생략 "
            "`<repo>_<N>`)를 지정하라(슬롯 경계 보호)."
        )
    if _SLOT_VALUE_ALT_SEPARATOR in s or s.startswith("/"):
        raise worktree_records.SlotResolutionError(
            f"슬롯 값 {slot_arg!r} 형식 오류 — 절대경로·백슬래시 구분자는 PM 홈 "
            "상대 슬롯 경로가 아니다(경로구분자·절대경로 거부·슬롯 경계 보호)."
        )
    slot = s if ("/" in s or s == _SLOT_HOME_VALUE) else f"work/{s}"
    parts = [part for part in slot.split("/") if part and part != _SLOT_HOME_VALUE]
    for part in parts:
        reason = identity_args.path_component_rejection(part)
        if reason is not None:
            raise worktree_records.SlotResolutionError(
                f"슬롯 값 {slot_arg!r} 형식 오류 — 마디 {reason}"
                "(POSIX·Windows 공통 안전 마디만·슬롯 경계 보호)."
            )
    normalized = "/".join(parts) if parts else _SLOT_HOME_VALUE
    if normalized != _SLOT_HOME_VALUE and (len(parts) != 2 or parts[0] != "work"):
        raise worktree_records.SlotResolutionError(
            f"슬롯 값 {slot_arg!r} 형식 오류 — 슬롯 자리는 PM 홈 자신(`.`) 또는 `work/` 직계 "
            "하위 한 마디다(`work/` 단독·슬롯 하위 경로 거부·슬롯 경계 보호)."
        )
    resolved_home = home.root.resolve(strict=False)
    resolved = (home.root / normalized).resolve(strict=False)
    if resolved != resolved_home and resolved_home not in resolved.parents:
        raise worktree_records.SlotResolutionError(
            f"슬롯 값 {slot_arg!r} 형식 오류 — 해소 경로 {resolved} 가 PM 홈({resolved_home}) 밖이다"
            "(symlink 탈출 거부·슬롯 경계 보호)."
        )
    return normalized


def _slot_from_cwd(home) -> str | None:
    """cwd 가 `<WORK_DIR>/<repo>_<N>[/...]` 안이면 그 슬롯 식별자(`work/<repo>_<N>`), 아니면 None.

    슬롯 worktree(또는 그 하위 submodule) cwd 에서 커맨드를 부르면 정체성 인자 없이도 그 슬롯을
    타깃하게 한다(pm_bootstrap `_worktree_cwd`·`slot_resolution._auto_slot` 의 cwd-유입 정신과 정합). WORK_DIR 밖
    (예: multi-PM 루트)이면 None(→ session 해소로 폴백). 경로 해석 실패는 None(fail-soft).
    """
    try:
        rel = Path(os.getcwd()).resolve().relative_to(pm_paths.work_dir(home).resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    return f"work/{parts[0]}" if parts else None


def _explicit_slot_path(home, repo: str, number: int) -> str:
    """명시 `--repo X --slot N` → **장부 행의 slot 경로 값**(모순은 fail-loud·부재는 canonical).

    dev/sync 의 대상 슬롯은 실 git side-effect 의 cwd 라 "그 번호를 장부가 어느 경로로 들고
    있는가"가 권위다 — `f"{repo}_{N}"` 재조립은 장부가 다른 경로를 들고 있는 슬롯(PM 홈 자신을
    가리키는 행 `slot="."`·`nested/X_1`)을 없는 자리로 보낸다(리뷰 F-001). 장부 부재/손상/행
    부재는 종전 canonical 관례(`<repo>_<N>` — `_resolve_current_slot` 이 `work/` 를 붙인다)로
    폴백하고, **모순만** fail-loud 다(추측 금지·리뷰 F-004).
    """
    resolution = lease_store.slot_path_resolution(repo, number, pm_paths.leases_file(home))
    if resolution.status == identity_args.SLOT_PATH_CONFLICT:
        raise worktree_records.SlotResolutionError(
            f"대상 슬롯을 특정할 수 없다 — {resolution.detail}. 장부를 정리한 뒤 다시 실행하라."
        )
    return resolution.path or f"{repo}_{number}"


def _resolve_actor_slot_for_repo(home, repo: str) -> str:
    """`--repo` 단독(슬롯 무) 폴더 해소 — 공용 `lease_store.resolve_actor_workspace` 위임.

    dev/sync 는 실 git side-effect 를 내는 폴더 연산이다(귀속을 쓰지 않는다): 그 repo 의 활성(leased) 슬롯이 정확히 1개면 자동 해소, 0개/≥2개는 fail-loud(`--slot <N>`
    명시 안내). 로컬 리스 읽기를 재구현하지 않는다 — `lease_store` 가 리스
    장부를 직접 point-read 한다. `identity_args.SlotResolutionError`(모호·다른 모듈의 클래스)는
    이 모듈의 `SlotResolutionError` 로 번역해 전파한다 — CLI `main` 의 단일 except 로 수렴시키기
    위함(호출부가 두 예외 타입을 각각 알 필요 없음).
    """
    try:
        workspace = lease_store.resolve_actor_workspace(repo, pm_paths.leases_file(home))
    except identity_args.SlotResolutionError as exc:
        raise worktree_records.SlotResolutionError(str(exc)) from exc
    if workspace is None:
        raise worktree_records.SlotResolutionError(
            f"repo {repo!r} 의 활성(leased) 슬롯이 없다 — `--slot <N>` 으로 대상 슬롯을 명시하라."
        )
    # 대상 슬롯 경로는 **행의 slot 값**이다 — 세션에서 `work/<session>` 을 재조립하면 세션이 task
    # 이름이거나 슬롯 경로가 다른 자리일 때 없는 경로를 만든다. 값-경계 검증만 재적용한다.
    return _normalize_slot(home, workspace.slot)
