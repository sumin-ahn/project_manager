"""회귀를 돌릴 작업 폴더 해소 (층: app).

등록 repo 목록, 단일 self-host 슬롯 판정, 명시 `--repo --slot` 의 장부 조인과 회귀 실행 위치를 소유한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from pm_engine.domain import identity_args
from pm_engine.infra import fs, lease_store, pm_paths
from pm_engine.domain import worktree_records
from pm_engine.infra import pool_store
from pm_engine.infra import worktree_paths


def _regression_cwd(
    home, worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (분리된 PM 홈+worktree 모델).

    분리된 PM 홈엔 `tests/` 가 없으므로 회귀는 활성 repo 의 worktree cwd 에서
    돌아야 한다. 이 함수가 그 경로를 해소한다.

    해소 순서:
      - `worktree_slot`(호출부가 고른 슬롯) 이 있으면 `repo_root / worktree_slot`
        (단 그 디렉토리가 실제로 없으면 stale 슬롯 → `repo_root` 로 폴백·경고 1줄),
      - 없으면 같은 모듈 `_auto_slot` 으로 단일 self-host 슬롯을 자동해소(**행의 `slot` 경로 값** ·
        **같은 존재 검사**를 탄다 — 디렉토리가 없으면 명시 분기와 동형으로 `repo_root` 폴백·경고),
      - 그것도 없으면(판정 불능 — 모호) **현 `repo_root`**. 등록된 홈은 홈 자신이
        슬롯 행이라 위 층에서 해소되므로 이 층은 "행이 없으니 이 트리겠지"라는 추론이 아니라
        판정 불능의 마지막 트리다(회귀 실행 중 crash 회피).

    판정 로직은 `_auto_slot` 재사용이다(DRY). areas/leases/repo_root 는 명시 인자로 노출해 hermetic
    테스트 가능. `areas_file` 미지정이면 `pm_paths.areas_file`(board 루트 추종)로 해소한다 —
    legacy 위치를 보면 _auto_slot 이
    등록 repo 를 0개로 세 self-host 슬롯을 미해소한다. `repo_root` 미지정이면 `home.root`.

    슬롯이 리스 장부 조인은 통과했더라도(명시든 자동해소든)
    실제 worktree 디렉토리가 물리적으로 없을 수 있다(장부-파일시스템 out-of-sync·저빈도 엣지) —
    그대로 `subprocess.run(cwd=...)` 에 넘기면 `FileNotFoundError` 로 크래시한다. 여기서 존재를
    확인해 없으면 **`home.root` 로 폴백**(비차단·경고 1줄)한다 — (장부 자체가 모순 — 하드 fail-loud)와
    boundary 를 정합시킨다: 장부-불일치는 loud reject, 장부는 맞는데 디스크만 stale 이면 soft 폴백.
    **두 분기가 같은 검사를 탄다** — 같은 상태(장부에는 있고 디스크에는 없음)를 해소 경로에 따라
    다르게 처리하면 자동해소 실행만 크래시한다.
    """
    if repo_root is None:
        repo_root = home.root
    if areas_file is None:
        areas_file = pm_paths.areas_file(home)
    if leases_file is None:
        leases_file = pm_paths.leases_file(home)
    if worktree_slot:
        candidate = repo_root / worktree_slot
        if candidate.is_dir():
            return str(candidate)
        print(
            f"  ⚠ 명시 슬롯 '{worktree_slot}' 의 worktree 디렉토리가 없다 ({candidate}) — "
            "REPO 로 폴백한다 (stale 슬롯).",
            file=sys.stderr,
        )
        return str(repo_root)
    auto = _auto_slot(home, areas_file, leases_file)
    if auto:
        # 자동해소도 **명시 슬롯과 같은 존재 검사**를 탄다. 판정은 장부(areas·leases)만 보므로
        #   worktree 디렉토리가 물리적으로 없을 수 있고(장부-파일시스템 out-of-sync), 그대로
        #   `subprocess.run(cwd=...)` 에 넘기면 FileNotFoundError 로 크래시한다 — 같은 상태를
        #   분기마다 다르게 처리할 이유가 없다(폴백도 경고도 동형).
        # 경로는 **장부 행의 slot 값**이다(번호로 `work/<repo>_<N>` 재조립 금지).
        candidate = repo_root / auto.slot
        if candidate.is_dir():
            return str(candidate)
        print(
            f"  ⚠ 자동해소 슬롯 '{auto.key}' 의 worktree 디렉토리가 없다 ({candidate}) — "
            "REPO 로 폴백한다 (stale 슬롯).",
            file=sys.stderr,
        )
    return str(repo_root)


# ── 명시 `--repo --slot` → 복구 문 제품 트리 (ticket_finish 공용 seam) ──────────
# 핸드오프는 쓰지 않는다 — 핸드오프 정체성은 task 하나다. `cli.ticket_finish.main`
# (복구 문 `--reconcile-integrated`)이 자기 `--repo`/`--slot` 을 파싱한 뒤 리스-조인 검증을 위해
# 부른다(판정 복붙 0).
def _resolve_explicit_identity_slot(
    home, repo: str,
    slot: int,
    leases_file: Path | None = None,
) -> tuple[str | None, str | None]:
    """명시 `--repo --slot` 을 worktree 슬롯 키로 해소한다 — `(worktree_slot, error_msg)`.

    `work/<repo>_<slot>` 을 고른다. 리스 장부가 읽히면(`lease_store.repo_slot_numbers` 가 `None`
    아님) 그 슬롯이 실제 **활성(leased)** 리스에 있는지 검증한다 — 없으면 조인 불일치로
    `(None, error_msg)`(fail-loud). 장부 미해독(파일 부재/깨짐 → `None`)은 *검증불가*라
    fail-soft(그대로 신뢰) — "판정불가"와 "모순"은 다르게 다룬다(과잉 차단 방지).

    `leases_file` 미지정이면 `pm_paths.leases_file(home)` 으로 해소한다(hermetic — 다른 리스 해소
    함수들과 동형).
    """
    if leases_file is None:
        leases_file = pm_paths.leases_file(home)
    known = lease_store.repo_slot_numbers(repo, leases_file)
    if known is not None and slot not in known:
        listing = ", ".join(str(n) for n in known) if known else "없음"
        return None, (
            f"[M3] 대여 기록 조인 불일치 — repo '{repo}' 의 활성 슬롯({listing}) 중 "
            f"{slot} 이 없다. `--slot` 을 정확히 지정하라."
        )
    # 실행 슬롯 경로는 **장부 행의 slot 값**이 권위다 — 번호로 `work/<repo>_<N>` 을 재조립하면
    # 장부가 다른 경로를 들고 있는 슬롯에서 존재하지 않는 cwd 가 나온다. 장부 미해독(부재/
    # 손상/행 부재)은 *검증 불가*라 종전 canonical 조립으로 폴백하지만, **모순**(같은 번호를
    # 서로 다른 경로가 주장)은 폴백 대상이 아니다 — 두 답 중 하나를 조용히 고르면 pm_state·
    # 로그를 엉뚱한 슬롯에 쓴다(리뷰 F-004·"판정불가"와 "모순"은 다르게 다룬다).
    resolution = lease_store.slot_path_resolution(repo, slot, leases_file)
    if resolution.status == identity_args.SLOT_PATH_CONFLICT:
        return None, (
            f"[M3] 장부 모순 — {resolution.detail}. 장부를 정리한 뒤 다시 실행하라."
        )
    return (resolution.path or f"work/{repo}_{slot}"), None


def _registered_repos(home, areas_file: Path | None = None) -> list[str]:
    """areas.md 레지스트리에서 등록된 repo 이름 목록 (identity surface '등록영역' 표면용).

    areas.md 의 `repo`/`prefix`
    칼럼을 stdlib 로 가볍게 읽는다. 파일 부재/스키마 불일치 → 빈 목록(fail-soft·무해).
    `areas_file` 미지정이면 `pm_paths.areas_file`(board 루트 추종)로 *호출 시점* 해소.
    """
    if areas_file is None:
        areas_file = pm_paths.areas_file(home)
    if not areas_file.exists():
        return []
    rows: list[str] = []
    header: list[str] | None = None
    sep_pattern = re.compile(r"^\|[\s:|-]+\|?$")
    for line in fs.read_text_shared(areas_file, encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or sep_pattern.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if not any(cells):
            continue
        row = dict(zip(header, cells))
        name = row.get("repo") or row.get("prefix")
        if name:
            rows.append(name)
    return rows


def _auto_slot(
    home, areas_file: Path | None = None,
    leases_file: Path | None = None,
) -> tuple[str, int] | None:
    """단일 self-host 슬롯 판정 — 정확히 1 repo + 그 repo **활성(leased)** 슬롯 정확히 1개면 그 슬롯.

    핸드오프의 회귀 cwd 해소(`_regression_cwd`)가 쓰는 순수 판정이다. 등록 repo 가
    정확히 1개이고 그 repo 의 leased 슬롯이 정확히 1개면 그 슬롯, 아니면 None(미해소) —
    repo 0개/≥2개 또는 활성 슬롯 0개/≥2개.

    반환은 `identity_args.SlotIdentity` — 정체성 키(`.key`)와 **장부 행의 경로 값**(`.slot`)을
    함께 나른다. 호출부는 번호로 `work/<repo>_<N>` 을 재조립하지 않고 `.slot`/`.key` 를 그대로
    쓴다 — 장부가 다른 경로를 들고 있는 슬롯에서 존재하지 않는 cwd 가 나오던 자리다.

    판정:
      1. `_registered_repos` 재사용 — areas.md 등록 repo 가 정확히 1개인가(아니면 None).
      2. **공용 `lease_store.resolve_actor_workspace` 위임** —
         그 repo 의 **leased** 슬롯이 정확히 1개면 그 세션을, 0개면 None 을 돌려준다. ≥2개(모호)
         는 그 프리미티브가 `identity_args.SlotResolutionError` 로 raise 하지만, 이 함수는
         **순수 resolver 계약**(정확히 1 아니면 항상 None·fail-soft)이라 그 예외를 여기서 흡수해
         호출부가 미해소로 처리하게 한다.

    **idle 필터 영향(codex must-fix)**: 공용 프리미티브가 leased 만 세므로, `{1:idle, 2:leased}`
    는 이전 None(2개→폴백) 대신 leased={2}→exactly-1→슬롯2 로 해소된다 — incidental(`_regression_cwd`·
    display)이 *활성* 슬롯을 찾는 것이라 오히려 정합·개선(의도된 변화). `{1:leased}`→1 은 불변.

    파일 부재/스키마 불일치/JSON 깨짐 → None(미해소). `areas_file`
    미지정(None)이면 `_registered_repos` 가 `pm_paths.areas_file`(board 루트 추종)로 해소한다.
    """
    if leases_file is None:
        leases_file = pm_paths.leases_file(home)
    repos = _registered_repos(home, areas_file)
    if len(repos) != 1:
        return None
    repo = repos[0]
    try:
        workspace = lease_store.resolve_actor_workspace(repo, leases_file)
    except identity_args.SlotResolutionError:
        return None  # ≥2 활성 슬롯(모호) — 순수 resolver 계약상 fail-soft None(raise 아님).
    if workspace is None:
        return None
    # 경로는 행의 `slot` 값, 정체성은 행이 준 키 — 세션에서 번호를 뜯어 경로를 재조립하지 않는다.
    return lease_store.resolve_slot_identity(workspace.slot, leases_file)


def _resolve_current_slot(home, slot_arg: str | None) -> str:
    """dev/sync 의 대상 슬롯을 해소한다 — 명시 슬롯 문자열 > cwd > 세션 leased.

    `slot_arg` 는 CLI `--repo`/`--slot` 로 정체성이 완전 해소된 경우 `main`(`identity.kind ==
    "slot"`)이 조립해 넘기는 `<repo>_<N>` 문자열이다 — 이 함수 자체는 어느
    source 문자열이든 정규화·검증만 한다(`--slot` CLI 인자 자체를 직접 받지 않는다).

    우선순위:
      1. `slot_arg` 명시(빈 문자열 포함) → `_normalize_slot`(main 의 명시 --repo/--slot 조립·
         권장 경로).
      2. cwd 가 슬롯 worktree 안이면 그 슬롯(`_slot_from_cwd`).
      3. 세션(`_default_session`·env/단일-lease 유입)이 보유한 leased 슬롯 — 정확히 1개면 그것.

    3에서 매칭 leased 슬롯이 0개(무바인딩)이거나 ≥2(모호)면 `SlotResolutionError` raise — CLI
    main 이 rc 1 + `--repo/--slot` 안내로 surface 한다(침묵 오타깃 금지). 이 해소는 dev/sync
    mutation의 대상 선택이므로 장부를 strict로 읽고, `_default_session`(lease_store.session_name 동형)을
    재사용한다(기존 관례). (인자 전무 시
    이 no-flag 체인은 불변.)

    **값-경계 검증**: `slot_arg` 명시는 `_normalize_slot` 이, 그 외 유입(cwd·장부)도 반환 전
    같은 `_normalize_slot` 을 재적용해 최종 슬롯의 해소 경로가 **PM 홈 안**임을 강제한다 — 어느
    source(명시/cwd/session)든 부적격/traversal 값이 `slot_path` 결합으로 슬롯 경계를 벗어나지
    못하게 하는 단일 불변식(이름 모양 강제가 아니다). `slot_arg` 는 **빈 문자열도 명시로
    취급**(`is not None`)해 검증에 태운다(빈값 거부).
    """
    if slot_arg is not None:
        return worktree_paths._normalize_slot(home, slot_arg)  # 명시(빈값 포함) → 정규화 + 형식 검증.
    cwd_slot = worktree_paths._slot_from_cwd(home)
    if cwd_slot:
        return worktree_paths._normalize_slot(home, cwd_slot)  # cwd 유입도 최종 형식 검증(단일 불변식).
    sess = lease_store._default_session(home)
    with pool_store._lease_lock(home):
        mine = [
            lease
            for lease in pool_store._read_ledger_strict(home)
            if lease.state == "leased" and lease.session == sess
        ]
    if len(mine) == 1:
        return worktree_paths._normalize_slot(home, mine[0].slot)  # 장부 유입도 최종 형식 검증(단일 불변식).
    if not mine:
        raise worktree_records.SlotResolutionError(
            f"세션 {sess!r} 의 leased 슬롯이 없다 — `--repo <name> --slot <N>` 으로 대상 슬롯을 명시하라."
        )
    raise worktree_records.SlotResolutionError(
        f"세션 {sess!r} 의 leased 슬롯이 {len(mine)}개"
        f"({', '.join(l.slot for l in mine)}) — `--repo <name> --slot <N>` 으로 어느 슬롯인지 명시하라."
    )
