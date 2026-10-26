"""board lint 출력과 advisory·차단 rc 번역 (층: cli)."""
from __future__ import annotations

import argparse
import sys
from pm_engine.app import lint_hooks
from pm_engine.app import ticket_lint
from pm_engine.infra import pre_push_hook


def cmd_lint(home, args: argparse.Namespace) -> int:
    """전체 lint 보고 (무인자) 또는 push 게이트 (`--gate`).

    무인자: 모든 issue 를 보고하고 issue 가 하나라도 있으면 종료코드 1 (현행 동작).
    `--gate`: 종료코드를 *차단 카테고리*에만 1 로 둔다 — status drift 같은 자문성
    (lint_status 의 "never blocks" 보장) 은 보고는 하되 종료코드에 반영하지 않는다.
    즉 `--gate` 는 pre-push 게이트용 엄격 부분집합이다.

    `--gate` 진입은 **훅 세대 판정**도 함께 탄다(`_stale_pre_push_hook_refusal`). 회귀 게이트는
    `tests/` 있는 트리에서만 도는데, 세대 판정의 호출자가 거기뿐이면 스위트 없는 트리(분리 형상
    PM 홈)는 다음 세대 교체 처방을 받을 자리가 없다 — 반면 lint 줄은 **두 형상 모두** 훅의 마지막
    줄로 항상 돈다. 새 게이트가 아니라 기존 판정의 부착 지점을 하나 더 두는 것이고, 판정은 lint
    수집보다 **앞**이다(옛 훅 아래서 통과한 lint 가 push 허가로 읽히지 않게).
    """
    gate = getattr(args, "gate", False)
    if gate:
        stale_hook = pre_push_hook._stale_pre_push_hook_refusal(home)
        if stale_hook is not None:
            print(stale_hook, file=sys.stderr)
            return 1
    issues = ticket_lint.lint_tickets(home)
    hook_issues = lint_hooks._run_lint_hooks(home)
    total = len(issues) + len(hook_issues)
    if total == 0:
        print("✓ no lint issues")
        return 0
    # 차단 카테고리 = 자문성(status drift) 제외 전부 + 모든 instance 훅 issue.
    blocking = [i for i in issues if i[1] not in _ADVISORY_LINT_KINDS]
    block_count = len(blocking) + len(hook_issues)
    label = "blocking " if gate else ""
    print(f"⚠️  {total} lint issue(s) ({block_count} {label}차단):"
          if gate else f"⚠️  {total} lint issue(s):")
    for ticket_id, kind, detail in issues:
        mark = " " if (kind in _ADVISORY_LINT_KINDS) else "✗"
        prefix = f"  {mark} " if gate else "  "
        print(f"{prefix}[{kind}] {ticket_id}: {detail}")
    for hook_name, detail in hook_issues:
        prefix = "  ✗ " if gate else "  "
        print(f"{prefix}[{hook_name}] {detail}")
    if gate:
        return 1 if block_count > 0 else 0
    return 1


# 차단되지 않는 자문성 lint kind — push 를 막지 않는 권고/드리프트 카테고리.
# `lint --gate` 는 이 카테고리를 종료코드에서 제외한다 (push 게이트용 엄격 부분집합):
#   - status-done-accum : status.md ✅ 완성 행 누적 archive 권고 (lint_status "never blocks" 보장).
#     (status-header-bloat·scalar-anchor-broken 은 status judgment-only 화로 제거.)
#   - unstable-ref-advice : 실재 파일을 슬러그로 가리키는 링크 — 작동은 함, ID-wikilink 권고만
#     resolve 실패는 kind=`unstable-ref` 로 차단됨.
#   - scope-advice : family_scope 형식/등록 권고 — scope 자체로 hard-fail 안 함
#     "차단은 최소·advisory 우선").
#   - stale·orphan·oversized·history : domain freshness finding (lint_domain·history=현재-진실
#     페이지에 쌓인 변경 이력 항목). enforcement 아닌 visibility — push 를 절대 막지 않는다.
#   - dangling-wikilink-scaffold : 어댑터 scaffold(.claude/.opencode) 에서만 등장하는 framework
#     ADR/idea dangling. 채택자(framework ADR 부재 다운스트림)의 scaffold bracket-ref 는
#     영구 dangling 이 정상 — visibility 만, push 미차단. ticket dangling·wiki/root-doc dangling 은
#     여전히 `dangling-wikilink`(blocking).
#   - adapter-drift : 채택자의 adapter-layer(facade·진입문서·settings) 가 baseline(마지막 동기) 이후
#     전파 채널 없는 manifest-제외 잔여라 *전파 대신*
#     PM 에게 경고만 — `pm-update` 안내(visibility>enforcement). B 전파는 채택자 customization clobber(비파괴
#     위배)라 의도적 비-전파. instance-state(status·architecture·tickets·log·decisions·README·lite)는 채택자
#     소유·diverge 정상이라 scope 제외. push 미차단(never-block).
#   - adr-author : ADR frontmatter `author: <user>/<pm-slot>` provenance 권고.
#     "누가 결정했나"(provenance·연속성 아님)를 박는 발행측 규칙 — board.py 는 ADR 을 발행하지 않으므로
#     부재/형식어긋남을 권고만 한다. 구 ADR(author 부재)은 정상이라 push 미차단(never-block).
#   - architecture-stale·status-stale·domain-stale : 현재-진실 문서 freshness — 문서
#     frontmatter `verified_at: <sha>` 이후 그 문서 매핑 경로에 커밋이 있으면 "재검증 필요"
#     권고(date 비교 대체). architect 재검증·PM 점검 대상이지 push 결함이 아니므로
#     visibility 만·never-block.
#   - areas-repo-unregistered : areas.md 에 등록된 repo 행이 0개. init 이 repo 행을 항상
#     등록하기 전에 setup 된 구 clone 이라는 뜻이라, 처방(init 재실행) 1줄만 상시 보인다 —
#     무prefix 발행 자체는 1급 형상이라 push 결함이 아니다(never-block).
#   - areas-duplicate-repo : areas.md 에 같은 repo 행이 2개 이상. first-match 리졸버
#     4종이 첫 행에서 return 하므로 조용히 굳는 걸 보이게만 한다 — 자동 병합은 사람 판정 영역이고
#     레지스트리 정리가 push 결함도 아니라 never-block(`areas_set_cell` 의 fail-loud 와 짝).
#   - areas-merge-union : areas.md 를 담은 git 에 `merge=union` 이 선언되지 않음. 동시 등록
#     안전(양쪽 행 보존)이 사라진 상태를 보이게만 한다 — 엔진이 backfill 하는 채널이 이미 있고
#     (`_ensure_board_gitattributes`) 채택자가 자기 `.gitattributes` 를 가질 수 있어 never-block.
#   - design-pending : 티켓 설계 단계(`design: required`) 미완 — 설계 절 미충전 또는 필드
#     미승격. 차단은 **그 티켓을 실제로 집는 claim 게이트**가 하므로, 전역 lint 는 남의
#     설계-중 티켓이 내 push 를 막지 않게 visibility 만 준다(never-block).
#   - design-estimate : `estimate=large` 인데 `design: n/a`(또는 필드 부재). 자동 required 는
#     발행 시점 1회뿐이라 estimate 를 사후 교정한 티켓엔 설계 게이트가 영영 안 붙는다. 올릴지
#     면제할지는 **사람 판정**이라 엔진은 재유도만 한다(never-block).
#   - codex-delegate-matcher-miss : SubagentStart 는 `(session_id, turn_id, agent_id)`로 식별하고,
#     같은 session 의 선행 allow-spawn을 FIFO 1:1 대조했을 때 대응 PreToolUse 처리 기록이 없는
#     start를 가시화한다. 이미 시작된 native spawn의 사후 advisory라 push는 막지 않는다.
#     (`SubagentStart.agent_type="default"`는 역할도 상관 키도 아니다 — 라이브 payload 실측)
_ADVISORY_LINT_KINDS: frozenset[str] = frozenset(
    {"status-done-accum", "unstable-ref-advice", "scope-advice", "coverage",
     "stale", "orphan", "oversized", "history", "adr-lifecycle", "architecture-stale",
     "status-stale", "domain-stale", "domain-unverifiable",
     "architecture-unverifiable", "status-unverifiable",
     "dangling-wikilink-scaffold", "adapter-drift",
     "adr-author", "areas-repo-unregistered", "areas-duplicate-repo", "areas-merge-union",
     "delegate-same-model", "design-pending", "design-estimate",
     "codex-delegate-matcher-miss", "open-claimed-contradiction", "local-conf",
     "local-conf-unknown-key", "local-conf-missing-required",
     "local-conf-unreadable",
     # 라운드 판정 코드(사이드카 seam 소유) + board 고유 잔여·판정불능 관측. 차단은 완료
     # 게이트가 한다.
     "round-name", "round-gap", "round-dup", "round-pending", "round-temporary",
     "round-stray", "round-unreadable",
     # 클러스터 장부 관측 — 운영 재료지 push 결함이 아니다(never-block · `lint_clusters`).
     ticket_lint._CLUSTER_MEMBER_LINT_KIND, ticket_lint._CLUSTER_DUPLICATE_LINT_KIND})
