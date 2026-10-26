"""티켓 보드 lint (층: app).

의존 순환·부재, 귀속 정체성, 라운드 파일, 묶음 장부, 본문에 남은 구 역할 절 주석을 판정해 lint 행을 낸다.
"""
from __future__ import annotations

from collections.abc import Sequence

from pm_engine.domain import ticket_rules
from pm_engine.infra import cluster_store, pm_paths, round_store, ticket_store
from pm_engine.app import round_seed
from pm_engine.app import areas_lint
from pm_engine.app import body_lint
from pm_engine.app import conf_lint
from pm_engine.app import doc_lint
from pm_engine.app import freshness_lint
from pm_engine.app import render_lint
from pm_engine.app import wiki_lint
from pm_engine.app import wikilink_lint
from pm_engine.infra import codex_observation


# ── 구 역할 절(명세 본문 marker) 탐지 ─────────────────────────────────────
#
# 라운드 사이드카 이전의 board 는 역할 산출을 명세 본문 안에 marker 로 감싸 누적했다
# (`<!-- pm-ticket-section:start role=… -->` … `:end` + 봉인 줄). 변환 명령은 v1.8.1 까지 있었고
# 이 엔진에는 없다. 남은 marker 는 `legacy-growth-section` lint 가 차단으로 보인다.
_LEGACY_SECTION_MARKER = "pm-ticket-section"
_LEGACY_SEAL_MARKER = "pm-ticket-seal"


def _fenced_code_line_mask(lines: Sequence[str]) -> list[bool]:
    """줄마다 ``` 코드 펜스 안인지 (토글 줄 자체도 안쪽으로 센다).

    문법을 문서화한 티켓은 예시 marker 를 펜스로 감싼다 — 그 예시를 데이터로 읽으면 lint 가
    예시 하나로 영영 red 가 된다. 짝이 맞지 않는 마지막 펜스(파일이 열린 채 끝남)는 안쪽으로
    접는다 — 더 넓게 안전한 쪽이다. `_has_legacy_growth_markers`(및 그걸 위임하는 lint)의
    시야다.
    """
    mask: list[bool] = []
    inside = False
    for line in lines:
        if line.lstrip().startswith("```"):
            inside = not inside
            mask.append(True)
            continue
        mask.append(inside)
    return mask


def _has_legacy_growth_markers(body: str) -> bool:
    """본문에 구 컨테이너 주석 **줄**이 있는가 (lint 판정의 시야).

    후보는 **column 0** 의 주석 줄뿐이다 — 산문이 backtick 이나 인용부호 안에서 같은 표기를
    설명하는 티켓이 실재한다(구 봉인 문법을 설명한 티켓). 그것을 데이터로 읽으면 lint 가
    정리 뒤에도 영영 red 로 남는다. 좁힌 시야는 구 엔진의 봉인 후보 판정과 같은 규칙이고,
    ``` 펜스로 감싼 문법 예시도 산문과 같이 데이터에서 뺀다(`_fenced_code_line_mask`).
    """
    lines = body.splitlines()
    fenced = _fenced_code_line_mask(lines)
    return any(
        not fenced[index] and line.startswith(marker)
        for index, line in enumerate(lines)
        for marker in _LEGACY_GROWTH_MARKERS
    )


# lint 판정 코드 — advisory 등재(`_ADVISORY_LINT_KINDS`)와 같은 문자열을 여기서 소유한다.
_CLUSTER_MEMBER_LINT_KIND = "cluster-member-missing"
_CLUSTER_DUPLICATE_LINT_KIND = "cluster-duplicate"


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return circular paths in a directed graph.

    Each cycle is a node list closed on itself, e.g. ['A', 'B', 'A'].
    Cycles sharing the same node set are reported once.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == GRAY:          # back edge → cycle
                cyc = stack[stack.index(nxt):] + [nxt]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
            elif color.get(nxt, WHITE) == WHITE:
                dfs(nxt)
        stack.pop()
        color[node] = BLACK

    for n in graph:
        if color[n] == WHITE:
            dfs(n)
    return cycles


def lint_dependencies(home) -> list[tuple[str, str, str]]:
    """Return list of (ticket_id, issue_kind, detail).

    Checks:
      - unknown:        depends_on / blocks references a non-existent ticket
      - self-reference: ticket lists its own ID in depends_on or blocks
      - asymmetric:     A.blocks contains B but B.depends_on does not contain A
      - cycle:          depends_on graph contains a circular path
    """
    tickets = {fm["id"]: (status, fm) for status, fm in ticket_store._all_tickets(home)}
    issues: list[tuple[str, str, str]] = []

    for tid, (_status, fm) in tickets.items():
        deps = list(fm.get("depends_on") or [])
        blocks = list(fm.get("blocks") or [])

        # self-reference
        if tid in deps:
            issues.append((tid, "self-reference",
                           "depends_on contains itself"))
        if tid in blocks:
            issues.append((tid, "self-reference",
                           "blocks contains itself"))

        # unknown reference
        for ref in deps:
            if ref != tid and ref not in tickets:
                issues.append((tid, "unknown",
                               f"depends_on references missing {ref}"))
        for ref in blocks:
            if ref != tid and ref not in tickets:
                issues.append((tid, "unknown",
                               f"blocks references missing {ref}"))

        # asymmetric blocks ↔ depends_on
        for ref in blocks:
            if ref == tid or ref not in tickets:
                continue
            other_fm = tickets[ref][1]
            other_deps = list(other_fm.get("depends_on") or [])
            if tid not in other_deps:
                issues.append((tid, "asymmetric",
                               f"blocks {ref} but {ref}.depends_on lacks {tid}"))

    # circular depends_on — self-references are handled above and excluded here
    graph = {
        tid: [d for d in (fm.get("depends_on") or [])
              if d in tickets and d != tid]
        for tid, (_status, fm) in tickets.items()
    }
    for cycle in _find_cycles(graph):
        issues.append((cycle[0], "cycle",
                       f"circular depends_on: {' → '.join(cycle)}"))

    return issues


def lint_claim_identity(home) -> list[tuple[str, str, str]]:
    """open + claimed_by/claimed_at/claimed_rev 잔존 모순 형상 advisory (never-block).

    불변식(`status == "open"` 인 티켓은 claimed_by/claimed_at/claimed_rev 전부 null)이
    깨진 형상을 가시화한다 — **세 필드 중 하나라도** non-null 이면 잡는다(단독 잔존도 포함·
    F-002). 옛 `cmd_unblock`(정합 수정 이전)이 claimed_by 를 무접촉으로 둔 채 무조건 open 으로
    옮기던 결함의 잔재로 생길 수 있다 — 조회·필터(`--mine`)·후속 claim 판단을 오염시킨다.
    adopter#0 보드 실측에서 대상 0건이라 자동 backfill 은 만들지 않고(대상 없는
    코드), 정정은 사용자가 직접 한다: 실제로 아무도 진행하지 않으면 frontmatter 에서 잔존
    필드를 지우고, 실제로 진행 중이면 `status: claimed` 로 옮겨 상태와 소유를 정합시킨다.

    kind=`open-claimed-contradiction`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·
    push 미차단). `blocked` + claimed_by 는 모순이 **아니다**(claimed-origin blocked 의 정상
    형상) — 여기서 잡지 않는다.
    """
    issues: list[tuple[str, str, str]] = []
    for status, fm in ticket_store._all_tickets(home):
        if status != "open":
            continue
        # 그 불변식이 지목하는 세 필드 전부를 개별 검사한다 — 하나만 잔존해도 모순(F-002 확장).
        residue = {field: fm.get(field)
                   for field in ("claimed_by", "claimed_at", "claimed_rev") if fm.get(field)}
        if not residue:
            continue
        residue_desc = ", ".join(f"{field}={value!r}" for field, value in residue.items())
        issues.append((
            fm.get("id", "?"), "open-claimed-contradiction",
            f"status=open 인데 {residue_desc} 잔존 — 소유 표식과 상태가 모순이다. 진행 중이 "
            "아니면 잔존 필드를 지우고, 진행 중이면 claimed/ 로 옮겨 정합시켜라."))
    return issues


# 라운드가 아닌 잔여를 가리키는 lint 종류. 판정 코드 자체는 사이드카 seam 이 소유하고
# (`round-name`·`round-gap`·`round-dup`·`round-pending`) board 는 그것을 그대로 lint kind 로
# 쓴다 — 아래 이름은 그 밖의 board 고유 관측 하나뿐이다.
_ROUND_TEMPORARY_LINT_KIND = "round-temporary"

# 라운드 디렉터리 자리(`tickets/rounds/` 직계)에 있는 파일 — 티켓 하나의 라운드 디렉터리만
# 사는 자리라 파일은 잘못 놓인 잔여다(seam 의 티켓 단위 판정은 그 자리를 보지 않는다).
_ROUND_STRAY_LINT_KIND = "round-stray"

# 한 티켓의 라운드 판정이 실패한 자리 — 나머지 티켓 판정은 그대로 내고 이 티켓만 사유로 남긴다.
_ROUND_UNREADABLE_LINT_KIND = "round-unreadable"

# 구 단일-파일 컨테이너의 흔적. 본문에 이 주석이 남아 있으면 그 티켓의 역할 산출은 아직
# 라운드 파일로 옮겨지지 않은 것이다(사람이 정리할 대상 · `legacy-growth-section` 차단).
_LEGACY_GROWTH_MARKERS: tuple[str, ...] = (
    f"<!-- {_LEGACY_SECTION_MARKER}:", f"<!-- {_LEGACY_SEAL_MARKER}")
_LEGACY_GROWTH_LINT_KIND = "legacy-growth-section"
_LEGACY_GROWTH_MIGRATION_HINT = (
    "변환 명령은 v1.8.1 까지 있었다(`board.py rounds migrate`). 절 내용을 라운드 파일로 "
    "옮기거나 marker·봉인 주석 줄을 지워 본문 텍스트로 남겨라"
)


def lint_rounds(home) -> list[tuple[str, str, str]]:
    """라운드 디렉터리의 판정(`verify_rounds`)을 board lint 표면으로 올린다 (advisory).

    차단 소비자는 완료 게이트다 — 여기서는 같은 판정을 **보이게만** 한다(never-block).

    fail-soft 경계는 **티켓 하나**다 — 한 티켓의 읽기 실패가 그때까지 모은 판정을 통째로
    버리지 않고 그 티켓만 사유로 남긴다. 판정 자체를 못 한 자리를 조용히 비우지 않기 위해서다.

    라운드 디렉터리 자리의 파일 항목과 점(`.`) 으로 시작하는 항목은 라운드가 아니라 잘못 놓인
    잔여·엔진의 원자 교체 중간 파일이다 — 둘 다 seam 판정 밖이라 여기서 표면화한다.
    """
    try:
        root = round_store.rounds_dir(pm_paths.tickets_dir(home))
        entries = sorted(root.iterdir()) if root.is_dir() else []
    # fail-soft 사유: 라운드 판정은 전역 board lint 의 visibility-only/never-block 축이다.
    # 디렉터리 판독 실패가 무관한 board 조회를 죽이지 않게 축을 생략한다. 차단 소비자(완료
    # 게이트)는 반대로 fail-closed 다 — 이 비대칭은 의도된 것이다.
    except Exception:  # noqa: BLE001 — 위 등록 사유의 advisory fail-soft 경계.
        return []

    findings: list[tuple[str, str, str]] = []
    for entry in entries:
        if not entry.is_dir():
            findings.append((
                entry.name, _ROUND_STRAY_LINT_KIND,
                f"라운드 디렉터리 자리에 파일이 있다: {pm_paths._rel_to_repo(home, entry)} "
                "(이 자리엔 티켓별 라운드 디렉터리만 산다)",
            ))
            continue
        ticket = entry.name
        try:
            for problem in round_seed.verify_rounds(pm_paths.tickets_dir(home), ticket):
                findings.append((ticket, problem.code, problem.detail))
            leftovers = sorted(
                item.name for item in entry.iterdir()
                if item.name.startswith(round_store.ROUND_TEMPORARY_PREFIX)
            )
        except Exception as exc:  # noqa: BLE001 — 티켓 단위 advisory fail-soft 경계.
            findings.append((
                ticket, _ROUND_UNREADABLE_LINT_KIND,
                f"라운드 판정 실패(이 티켓만 건너뛴다): {exc}",
            ))
            continue
        if leftovers:
            findings.append((
                ticket, _ROUND_TEMPORARY_LINT_KIND,
                f"엔진 임시 파일 잔여 {len(leftovers)}개(교체 중 크래시 의심): "
                + ", ".join(leftovers),
            ))
    return findings


def lint_clusters(home) -> list[tuple[str, str, str]]:
    """클러스터 장부 판정 — 멤버 부재·중복 귀속 (advisory·never-block).

    장부는 운영 재료지 push 결함이 아니다 — 보이게만 한다. 판정 입력은 **장부 파일 안에만**
    있다: 이 표면은 인자를 받지 않고 장부에도 코드 트리·repo 를 적는 자리가 없어
    (`CLUSTER_LEDGER_KEYS`) 통합 브랜치 실재를 잴 트리를 지목할 방법이 없다. 트리를 추측해서
    재면 PM 홈(코드 없는 트리)에서 "브랜치 부재"가 상시 참이 된다 — 그래서 브랜치 실재는
    **트리를 든 표면**(생성 `_cluster_ensure_branch` · 확인 실행 `cli.delegate_cmd._cluster_confirmation_tree` ·
    공용 트리 해소 `app.cluster_code_tree.resolve_code_tree`)만 판정한다.
    """
    clusters = cluster_store.all_clusters(home)
    if not clusters:
        return []
    findings: list[tuple[str, str, str]] = []
    owners: dict[str, list[str]] = {}
    for fm in clusters:
        cluster = str(fm.get("id") or "").strip()
        for tid in cluster_store.cluster_tickets(fm):
            owners.setdefault(tid, []).append(cluster)
            if ticket_store.find_ticket_exact(home, tid) is None:
                findings.append((
                    cluster, _CLUSTER_MEMBER_LINT_KIND,
                    f"멤버 티켓 부재: {tid} — 장부가 board 에 없는 티켓을 가리킨다"
                    "(draft 는 승격 전까지 공유 board 밖이다)",
                ))
    for tid, declared in sorted(owners.items()):
        if len(declared) > 1:
            findings.append((
                tid, _CLUSTER_DUPLICATE_LINT_KIND,
                f"중복 귀속: {', '.join(sorted(declared))} — 티켓은 한 묶음에만 속한다",
            ))
    return findings


def lint_legacy_growth_sections(home) -> list[tuple[str, str, str]]:
    """명세 본문에 남은 구 역할 절(marker/봉인 주석)을 blocking 으로 잡는다.

    라운드 사이드카 전환 뒤에도 절이 본문에 남아 있으면 그 산출은 어떤 라운드 판정에도 잡히지
    않는다(두 형식이 공존하는 상태). 이 엔진은 변환하지 않으므로 사람이 정리할 때까지 red 다.
    done 을 포함한 전 상태와 draft 를 본다 — 남은 절이 완료 티켓에 몰려 있다.

    시야는 `_has_legacy_growth_markers`(column 0 주석 줄 · ``` 펜스 밖)다.
    """
    findings: list[tuple[str, str, str]] = []
    for status in (*ticket_rules.STATUS_DIRS, ".drafts"):
        for path in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, path)
            if loaded is None:
                continue
            fm, body = loaded
            if not _has_legacy_growth_markers(body):
                continue
            findings.append((
                str(fm.get("id") or path.name),
                _LEGACY_GROWTH_LINT_KIND,
                f"본문에 구 역할 절 주석이 남아 있다 — {_LEGACY_GROWTH_MIGRATION_HINT}",
            ))
    return findings


def lint_codex_delegate_observations(home) -> list[tuple[str, str, str]]:
    """Surface unmatched Codex SubagentStart observations once (never-block).

    The guard owns JSONL rotation and the idempotent set comparison.  Board lint
    only aggregates all retained misses into one PM-facing advisory line.
    """
    try:
        misses = codex_observation.scan_codex_observation_misses(
            home, state_dir=home.local_dir / "delegate-channel",
        )
    except Exception as exc:  # noqa: BLE001 — observation visibility never blocks lint.
        return []
    if not misses:
        return []

    identities: list[str] = []
    for entry in misses:
        identity = "/".join(
            str(entry.get(field) or "?")
            for field in ("session_id", "turn_id", "agent_id")
        )
        if identity not in identities:
            identities.append(identity)
    sample = ", ".join(identities[:3])
    remainder = len(identities) - min(len(identities), 3)
    suffix = f" 외 {remainder}개 identity" if remainder else ""
    return [(
        "Codex SubagentStart",
        "codex-delegate-matcher-miss",
        f"일치하는 PreToolUse 처리 기록이 없는 SubagentStart {len(misses)}건 — "
        f"spawn matcher drift 또는 가드 미발화 가능({sample}{suffix}). "
        "append-only 관측은 소비하지 않았으며 native spawn은 사후 관측만 한다.",
    )]


def lint_tickets(home) -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement + status.md ✅ 완성 행 누적 권고(judgment-only) +
    dangling wikilink + unstable (slug/filename) refs +
    family wiki scope 인지+
    domain freshness advisory(stale/orphan/oversized/history·never-block) +
    architecture.md freshness advisory(architecture-stale·never-block) +
    adapter-layer drift advisory(adapter-drift·never-block·baseline rev 비교) +
    render-leak(리터럴 `{{...}}` 누출·blocking·@render 산출물 한정·활성화 전 무발화) +
    adr-author(ADR `author: <user>/<pm-slot>` provenance 권고·advisory·never-block) +
    현재-진실 문서 freshness(architecture-stale·status-stale·domain-stale·verified_at sha 판정·
    advisory·never-block) +
    areas-repo-unregistered(areas.md 등록 repo 행 0 → 구 clone 이관 안내·advisory·never-block) +
    areas-duplicate-repo(areas.md 중복 repo 행 → first-match 고착 가시화·advisory·
    never-block) +
    areas-merge-union(areas.md 를 담은 git 에 merge=union 미배포 → 동시 등록 안전 상실 가시화·
    advisory·never-block) +
    delegate-same-model(delegate dev/reviewer 동일-모델 해소 → 맹점 공유로 인한 검출력 저하 가시화·
    advisory·never-block) +
    codex-delegate-matcher-miss(unmatched SubagentStart → PM 표면 1줄·advisory·never-block) +
    round-*(라운드 순번 유일성·연속성·산출 없음·임시 파일 잔여 가시화·advisory·never-block·
    차단은 완료 게이트) + legacy-growth-section(명세 본문에 남은 구 역할 절 → 사람이
    라운드 파일로 옮기거나 marker 를 지워 해소·blocking) +
    design-pending(티켓 설계 단계 `design: required` 미완 — 설계 절 미충전/필드 미승격 가시화·
    advisory·never-block·차단은 claim 게이트) +
    open-claimed-contradiction(status=open 인데 claimed_by 잔존 — 상태-소유 모순 가시화·
    advisory·never-block) +
    local-conf-unknown-key(local.conf 의 레지스트리 밖 키 — 오타/폐기 키 조용한 무시 표면화·
    advisory·never-block) +
    local-conf-missing-required(local.conf 에 필수 키 미선언 — 핵심 명령이 못 도는 형상 표면화·
    advisory·never-block) +
    local-conf-unreadable(local.conf 가 있는데 판독 실패 — 값 0 개로 위장되던 상태 표면화·
    advisory·never-block) +
    cluster-*(클러스터 장부의 멤버 부재·통합 브랜치 부재·중복 귀속 가시화·advisory·
    never-block)."""
    return (lint_dependencies(home) + body_lint.lint_bodies(home) + body_lint.lint_ideas(home)
            + wiki_lint.lint_status(home)
            + wikilink_lint.lint_wikilinks(home) + wikilink_lint.lint_unstable_refs(home) + wiki_lint.lint_scopes(home)
            + doc_lint.lint_domain(home) + wiki_lint.lint_adr_lifecycle(home) + wiki_lint.lint_adr_author(home)
            + freshness_lint.lint_architecture_freshness(home) + freshness_lint.lint_status_freshness(home)
            + doc_lint.lint_domain_freshness(home) + conf_lint.lint_adapter_drift(home)
            + render_lint.lint_render_leak(home)
            + areas_lint.lint_areas_repo_unregistered(home)
            + areas_lint.lint_areas_duplicate_repo(home) + areas_lint.lint_areas_merge_union(home)
            + conf_lint.lint_delegate(home) + conf_lint.lint_local_conf(home) + conf_lint.lint_local_conf_keys(home)
            + conf_lint.lint_local_conf_required(home) + conf_lint.lint_local_conf_readable(home)
            + lint_rounds(home) + lint_clusters(home)
            + lint_legacy_growth_sections(home)
            + lint_codex_delegate_observations(home) + lint_claim_identity(home))
