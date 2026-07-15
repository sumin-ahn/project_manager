#!/usr/bin/env python3
"""공용 정체성 인자 모듈 — `--repo`/`--slot` 파싱 + 리스(활성슬롯) 읽기·해소 (ADR-0057).

슬롯 정체성 인자를 받는 전 CLI(board·pm_config·pm_bootstrap·pm_handoff·ticket_finish·pm_relay·
worktree_pool)가 이 한 모듈로 수렴한다(단일 진실·DRY). 지금까지 도구마다 복붙됐던 리스 원장
읽기(`board._leased_sessions`·`pm_config._leased_sessions`·`pm_bootstrap._repo_slot_numbers`)와
슬롯 해소(`pm_bootstrap.SlotResolutionError` 규칙)를 여기 하나로 흡수한다(B-1·이 티켓 T-0322는
모듈 신설만 — 기존 도구의 로컬 리더 제거·교체는 채택 티켓 T-0314/0315/0317/0316/0318 몫).

두 층으로 응집한다(같은 파일·별 함수):
  - **순수 인자 층**: `add_identity_args`·`parse_identity` — 파일 IO 0·부작용 0.
  - **리스 IO 층**: `leased_sessions`·`repo_slot_numbers`·`resolve_actor_slot` — 리스 장부
    (`worktree-leases.json`) 를 stdlib json 으로 직접 point-read 한다. `worktree_pool` 을
    import 하지 않는다(ADR-0013 격리 관성 — 데이터 결합만, 모듈 결합 아님). **콜백 없음**
    (PM 67 리뷰 C 수정) — 이 모듈이 리스 읽기를 직접 소유한다.

해소 규칙(ADR-0057 결정 3·spike §3.1):
  ```
  --repo X --slot N  → kind="slot"  · session="X_N"
  --repo X (슬롯 무) → kind="repo"  · session=None(repo-scope — view 전체 vs actor 활성슬롯
                        해소는 caller 몫. actor 는 resolve_actor_slot 을 별도 호출한다)
  --slot N (repo 무) → fail-loud(ValueError): "--slot 은 --repo 필수 — --repo <name> --slot <N>"
  (인자 전무)        → kind="none" (no-flag 기본 해소는 ADR-0040·caller 몫·이 wave 범위 밖·불변)
  ```
`parse_identity` 는 **discriminated** `Identity(kind, repo, slot, session)` 를 반환한다(PM 67
리뷰 A 수정) — 모호한 단일 문자열을 반환하지 않는다. caller 는 `kind` 로 명시 분기한다.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


class Identity:
    """`parse_identity` 의 discriminated 결과 — caller 는 `kind` 로 분기한다(PM 67 리뷰 A).

    - `kind="slot"`: `--repo X --slot N` 둘 다 지정 — repo=X·slot=N·session="X_N"(정체성 완전 해소).
    - `kind="repo"`: `--repo X` 만 지정(슬롯 무) — repo=X·slot=None·session=None(repo-scope).
      caller 가 view(그 repo 의 내 슬롯 전체) 인지 actor(활성슬롯 1개 해소) 인지 판단 —
      actor 라면 `resolve_actor_slot(repo, leases_file)` 를 별도 호출한다.
    - `kind="none"`: 정체성 인자 전무 — repo/slot/session 전부 None. no-flag 기본 해소(ADR-0040
      의 env/lease/local.conf 유도 체인)는 이 모듈 범위 밖 — caller 가 그대로 이어간다.

    (평범한 클래스 — `@dataclass` 미사용: 엔진 도구는 `spec_from_file_location` 으로 로드되는데
    `from __future__ import annotations`(문자열 지연평가) 와 결합 시 모듈이 `sys.modules` 에
    등록 안 돼 있으면 dataclass 처리가 `AttributeError` 로 깨진다 — `worktree_pool.Lease`·
    `pm_relay` 의 동일 회피 관용구를 따른다.)
    """

    def __init__(self, kind: str, repo: str | None, slot: int | None, session: str | None):
        self.kind = kind
        self.repo = repo
        self.slot = slot
        self.session = session

    def __repr__(self) -> str:
        return (f"Identity(kind={self.kind!r}, repo={self.repo!r}, "
                f"slot={self.slot!r}, session={self.session!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Identity):
            return NotImplemented
        return (self.kind, self.repo, self.slot, self.session) == (
            other.kind, other.repo, other.slot, other.session)


def add_identity_args(parser: argparse.ArgumentParser) -> None:
    """`--repo`·`--slot` 을 parser 에 추가 — 순수(파일 IO 0·부작용 0·ADR-0057 canonical 인자).

    `--slot` 단독(``--repo`` 없이) 금지 규칙은 여기(add 시점)가 아니라 parse 후 `parse_identity`
    가 검사한다 — argparse 만으로는 "A 있으면 B 도 필수"를 표현할 수단이 마땅치 않고(양방향
    `required` 조합 불가), 에러 메시지도 전 도구가 동일해야 하므로(카드↔CLI 정합·ADR-0045) 검사를
    한 곳(`parse_identity`)에 모은다.
    """
    parser.add_argument(
        "--repo", metavar="이름", default=None,
        help="repo 이름 — 슬롯 정체성 지정 (ADR-0057 canonical). 단독이면 repo-scope, "
             "--slot 과 함께면 슬롯 정체성 <repo>_<N> 로 해소.",
    )
    parser.add_argument(
        "--slot", metavar="N", type=int, default=None,
        help="슬롯 번호 — --repo 필수(단독 사용 불가). 함께 주면 세션 <repo>_<N> 로 해소.",
    )


def parse_identity(args: argparse.Namespace) -> Identity:
    """parsed args(`.repo`·`.slot`)에서 discriminated `Identity` 로 해소한다 (ADR-0057 해소 규칙).

    fail-loud(`ValueError`) 두 경우 — (1) `--slot` 있는데 `--repo` 없음, (2) `--slot < 1`(슬롯
    번호는 1부터·`work/<repo>_<N>` 정합). caller 가 `parser.error(str(exc))` 로 그대로 보인다
    (`pm_bootstrap.resolve_repo_arg` 동형). 그 외 세 경우(slot/repo/none)는 성공한다.

    slot≥1 검증은 `pm_bootstrap` 원 계약(pm_bootstrap.py:2935·`test_bootstrap_slot_below_one_rejected`
    게이트) 보존 — canonical 모듈이 여기서 빠뜨리면 채택(T-0315) 시 회귀(codex 게이트 T-0322).
    """
    repo = getattr(args, "repo", None)
    slot = getattr(args, "slot", None)
    if slot is not None and repo is None:
        raise ValueError("--slot 은 --repo 필수 — --repo <name> --slot <N>")
    if slot is not None and slot < 1:
        raise ValueError("--slot 은 1 이상의 슬롯 번호여야 한다 (work/<repo>_<N>).")
    if repo is not None and slot is not None:
        return Identity(kind="slot", repo=repo, slot=slot, session=f"{repo}_{slot}")
    if repo is not None:
        return Identity(kind="repo", repo=repo, slot=None, session=None)
    return Identity(kind="none", repo=None, slot=None, session=None)


# ── 리스 IO 층 (worktree-leases.json 원장 point-read·ADR-0013 격리 관성) ──────────


def _load_lease_rows(leases_file: Path) -> list[dict] | None:
    """`leases_file` 의 `leases` 배열을 dict 리스트로 읽는다 — `leased_sessions`·`repo_slot_numbers`
    가 공유하는 내부 IO 프리미티브(모듈 내 DRY).

    부재/JSON 깨짐/스키마 불일치(최상위 dict 아님·`leases` 가 list 아님) → `None`(fail-soft 신호 —
    "읽을 수 없음"). 두 공개 함수가 이 `None` 을 각자의 관례로 번역한다.
    """
    try:
        data = json.loads(leases_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("leases", [])
    if not isinstance(rows, list):
        return None
    return rows


def leased_sessions(leases_file: Path) -> list[str]:
    """lease 장부에서 `state=="leased"` 행들의 session 목록 (count-based 유도용·ADR-0040 D1).

    `board._leased_sessions`·`pm_config._leased_sessions` 흡수(동형·byte-for-byte 동작 보존) —
    `state` 가 **정확히 "leased"** 인 행만 센다(back-compat 기본값 없음 — 원 두 사본과 동형).
    장부 부재/파싱실패/손상 → 빈 리스트(fail-soft — 세션 해소가 장부 손상으로 죽지 않게). session
    이 빈/None 인 행은 제외.

    주의(의도된 비대칭): `repo_slot_numbers` 는 `state` 키 **부재**를 `"leased"` 로 back-compat
    처리한다(원 `pm_bootstrap._repo_slot_numbers` 동형) — 이 함수는 그러지 않는다(원 두 `_leased_
    sessions` 사본 동형). 각자의 원 구현 동작을 정확히 보존해 흡수했다(조화 아님·T-0322 스코프).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return []
    sessions: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == "leased":
            sess = row.get("session")
            if sess:
                sessions.append(sess)
    return sessions


def repo_slot_numbers(repo: str, leases_file: Path) -> list[int] | None:
    """`leases_file` 장부에서 `repo` 의 **활성(leased)** worktree 슬롯 번호(`work/<repo>_<N>`→N).

    `pm_bootstrap._repo_slot_numbers` 흡수(동형·byte-for-byte 동작 보존). `state` 가 `"leased"`
    인 엔트리만 센다 — **`state` 키 부재는 `"leased"` 로 back-compat**(`worktree_pool.from_dict`
    default 동형·`leased_sessions` 와의 의도된 비대칭은 위 docstring 참고). 같은 슬롯 N 의 중복
    장부 엔트리는 dedup(정렬된 unique 목록).

    파일 부재/JSON 깨짐/스키마 불일치 → `None`(fail-soft·"읽을 수 없음"); 정상 read 인데 그 repo
    의 leased 슬롯이 0개면 빈 리스트 `[]`("읽었으나 활성 슬롯 없음"). 호출부는 두 경우를 구분한다
    (`resolve_actor_slot` 이 이 구분을 그대로 위임한다).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return None
    slot_re = re.compile(rf"^work/{re.escape(repo)}_(\d+)$")
    slot_nums: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("repo") != repo:
            continue
        # 활성(leased) 슬롯만 — idle(반납)은 죽은 세션이라 라우팅 대상 아님(codex must-fix 원 흡수).
        # state 키 부재는 leased 로 본다(worktree_pool from_dict default·back-compat).
        if row.get("state", "leased") != "leased":
            continue
        m = slot_re.match(str(row.get("slot") or ""))
        if m:
            slot_nums.add(int(m.group(1)))
    return sorted(slot_nums)


class SlotResolutionError(Exception):
    """`resolve_actor_slot` 이 활성 슬롯을 자동 해소할 수 없을 때(≥2 개·모호) — fail-loud.

    `--repo`-단독 actor 해소(claim/finish/handoff/regression/livegate) 전용 규칙(ADR-0057 결정
    3) — 원 `pm_bootstrap.SlotResolutionError`(session-entry 용·default-1 규칙 포함)와 이름은
    같으나(의미 보존·단일 진실로 수렴 예정) 이 모듈의 판정은 더 단순하다: **1개면 해소·0개/≥2개는
    각각 None/raise** — default-1 규칙(slot 1 우선)은 여기 적용하지 않는다(그건 session-entry
    전용 관심사).
    """


def resolve_actor_slot(repo: str, leases_file: Path) -> str | None:
    """actor(`--repo`-단독) 슬롯 해소 — `repo` 의 활성(leased) 슬롯이 정확히 1개면 그 session.

    ADR-0057 결정 3: `--repo` 단독 인자에서 actor 연산(claim/finish/handoff/regression/livegate)
    은 활성 슬롯이 1개면 자동 해소하고, ≥2 개면 모호해 `SlotResolutionError`(fail-loud) — 기존
    SlotResolutionError 의미를 보존한다. 활성 슬롯이 0개(장부 부재/파싱실패/그 repo 활성 슬롯 없음)
    는 `None`(fail-soft) — actor 정체성이 미해소라는 신호일 뿐이며, `--session` 필요 여부(required)
    판단은 caller 몫이다(`board.session_name` 의 `required` 패턴과 동형).
    """
    slot_nums = repo_slot_numbers(repo, leases_file)
    if not slot_nums:
        return None
    if len(slot_nums) == 1:
        return f"{repo}_{slot_nums[0]}"
    raise SlotResolutionError(
        f"repo '{repo}' 활성 슬롯 {len(slot_nums)}개"
        f"({', '.join(f'work/{repo}_{n}' for n in slot_nums)}) 중 하나로 특정할 수 없다 — "
        f"`--slot <N>` 으로 명시하라."
    )
