"""티켓 ID와 prefix 변경 명령 (층: cli)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from pm_engine.app import areas_registry
from pm_engine.app import board_dashboard
from pm_engine.app import ticket_access
from pm_engine.domain import areas_rules
from pm_engine.domain import ticket_rules
from pm_engine.infra import areas_store
from pm_engine.infra import board_git
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.infra import pm_paths
from pm_engine.infra import round_store
from pm_engine.infra import ticket_store
from typing import Any
import argparse
import os
import re
import shutil
import subprocess
import sys


def _rewrite_text_counted(text: str, id_map: dict[str, str]) -> tuple[str, dict[str, int]]:
    """`rewrite_text_token_aware` 의 per-ID 카운트 판 (rewrite_refs 의 N 집계용·내부).

    반환 `(new_text, {old_id: 치환건수})`. 빈 맵/무매치면 `(text, {})`. old ID 를 **길이
    내림차순**으로 alternation 에 넣어 짧은 ID 가 더 긴 ID 를 가리는 것을 막는다(같은 앵커에서
    최장매치 선호). 단일 pass 치환이라 new 값 안에 다른 old 키가 있어도 재치환되지 않는다.
    """
    counts: dict[str, int] = {}
    if not id_map:
        return text, counts
    olds = sorted(id_map, key=len, reverse=True)
    pattern = re.compile(
        ticket_rules._REWRITE_LEADING_BOUNDARY
        + "(?:" + "|".join(re.escape(o) for o in olds) + ")"
        + ticket_rules._REWRITE_TRAILING_BOUNDARY)

    def _sub(m: re.Match[str]) -> str:
        old = m.group(0)
        counts[old] = counts.get(old, 0) + 1
        return id_map[old]

    return pattern.sub(_sub, text), counts


def rewrite_text_token_aware(text: str, id_map: dict[str, str]) -> tuple[str, int]:
    """한 문자열 내 모든 old ID 를 new 로 토큰단위 정확치환·(new_text, 총 치환건수) 반환.

    토큰 앞 문자는 `[A-Za-z0-9_-]`, 뒤 문자는 `[0-9_-]`에 속하지 않아야 한다.
    식별자 앞뒤에 문자·숫자·구분자가 붙어 더 긴 토큰을 이루면 치환하지 않는다.
    frontmatter·`[[wikilink]]`·bare·산문 임베드 전 표기형(무prefix `T-NNNN`·prefix `T-<p>-NNN`)을
    한 번에 커버한다.
    """
    new_text, counts = _rewrite_text_counted(text, id_map)
    return new_text, sum(counts.values())


def collect_rewrite_targets(root: str | Path) -> list[Path]:
    """rewrite 대상 `.md` 파일 집합 — `board/tickets/`·`wiki/`·`log/`(root 하위·순서보존 dedup).

    `root` 는 `board/`·`wiki/`·`log/` 를 담는 디렉토리(관례상 `<REPO>/.project_manager`)를
    호출측이 주입한다 — 전역 상태 무의존·테스트가 tmp 트리를 그대로 넘긴다. 각 하위는 있을
    때만 스캔한다(legacy 형상은 tickets/log 가 `wiki/` 안이라 `wiki.rglob` 가 이미 포섭·
    board/ 분리 형상은 `board/tickets` 가 별도 → union). frontmatter·h1 포함 텍스트 전체가
    스캔 대상(depends_on 등 frontmatter 참조도 치환). 파일명 slug rename 은 범위 밖.
    """
    root = Path(root)
    files: list[Path] = []
    for sub in (root / "board" / "tickets", root / "wiki", root / "log"):
        if sub.is_dir():
            files.extend(sub.rglob("*.md"))
    return list(dict.fromkeys(files))  # 순서보존 dedup (겹치는 서브트리 방어적 중복 제거)


def rewrite_refs(root: str | Path, id_map: dict[str, str], *, dry_run: bool,
                 changed_paths: list[Path] | None = None) -> dict[str, int]:
    """대상 파일 전부에 토큰단위 rewrite 적용·규모 집계 반환 (rename/merge 공용 코어).

    반환 `{"ids": N, "refs": M, "files": K}` — N=id_map 중 *실제 참조된* old ID 수, M=총
    치환 refs, K=치환이 일어난 파일 수. `dry_run=True` 면 파일을 기록하지 않고 카운트만 낸다
    (`board.py prefix … --dry-run` 의 "N ID·M refs" preview 원천).

    `changed_paths` 를 주면 **치환이 일어난 파일 경로**를 순서대로 append 한다(반환 dict 는
    불변 — 기존 소비자 무영향). 호출부가 "이 mutation 이 실제로 만진 경로" 를 알아야 스코프
    커밋이 가능한데, 그 집합은 여기서만 알 수 있다. dry_run 에서도 *바뀔* 파일을
    담는다(preview 와 동일 집합).
    """
    referenced: set[str] = set()
    total_refs = 0
    files_changed = 0
    for path in collect_rewrite_targets(root):
        # `newline=""` (universal-newline OFF): 원본 개행을 그대로 읽어 재쓰기 때 보존한다 —
        # CRLF 채택자(Windows 회사 실측)의 파일을 rewrite 가 LF 로 무단 정규화하지 않게 한다.
        # 읽기 실패는 OSError(권한·소실)뿐 아니라 UnicodeDecodeError(비-UTF-8 바이너리·깨진
        # 인코딩)도 포함해 넓게 잡되, **silent 누락 금지** — skip 한 파일을 stderr 로 1줄 경고해
        # 참조가 조용히 남는 것을 표면화한다.
        try:
            with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  ⚠ rewrite skip {path.name} — 읽기 실패({exc.__class__.__name__}): 참조가 "
                  "남을 수 있으니 수동 확인.", file=sys.stderr)
            continue
        new_text, counts = _rewrite_text_counted(text, id_map)
        if not counts:
            continue
        referenced.update(counts)
        total_refs += sum(counts.values())
        files_changed += 1
        if changed_paths is not None:
            changed_paths.append(path)
        if not dry_run:
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
    return {"ids": len(referenced), "refs": total_refs, "files": files_changed}


_NO_PREFIX_LABEL = "none"


def cmd_prefix_list(home, args: argparse.Namespace) -> int:
    """prefix별 티켓 수·번호범위 현황을 출력한다 (read-only·비파괴).

    STATUS_DIRS(전 상태 디렉토리)의 전 티켓 ID 를 파싱해 `T-<p>-NNN` → 그 prefix,
    `T-NNNN` → `none`(무prefix)로 버킷팅한다. mess(카테고리 난립·번호 재시작)를 표면화하는
    도구 — board mutation·rewrite 는 하지 않는다.
    """
    # prefix(또는 None=무prefix) → [(순번, 실 ID), …]. 실 ID 를 들고 있어 min/max 를 재구성
    # 없이 그대로 범위로 쓴다(zero-pad 재구성 mismatch 회피).
    buckets: dict[str | None, list[tuple[int, str]]] = {}
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            tid = ticket_store._issued_ticket_id(home, p, fm)
            if not tid:
                continue
            num = ticket_rules._ticket_id_number(tid)
            if num is None:
                continue
            buckets.setdefault(ticket_rules._ticket_prefix(tid), []).append((num, tid))
    if not buckets:
        print("(no tickets)")
        return 0
    # none(무prefix) 먼저, 그다음 prefix 알파벳순 — 무prefix 기준선을 맨 위에 고정.
    print(f"  {'prefix':16s}{'count':>6s}  범위(min ~ max)")
    for pfx in sorted(buckets, key=lambda k: (k is not None, k or "")):
        entries = sorted(buckets[pfx])
        lo_id, hi_id = entries[0][1], entries[-1][1]
        label = pfx if pfx is not None else _NO_PREFIX_LABEL
        rng = lo_id if lo_id == hi_id else f"{lo_id} ~ {hi_id}"
        print(f"  {label:16s}{len(entries):>6d}  {rng}")
    return 0


def _rename_map(src: str | None, dst: str | None,
                tickets: list[dict[str, Any]]) -> dict[str, str]:
    """rename 무충돌 맵 — src 네임스페이스 티켓의 prefix 만 dst 로 교체(번호 유지).

    src 매칭은 **case-insensitive fold**(`_fold_key`) — `rename aaa bbb` 가 기존
    `T-AAA-*`(prefix `AAA`)를 잡아 relabel 한다(case 만 다르면 silent no-op 하던 갭 봉합). dst 는
    호출자가 ``require_prefix_user_ack`` 공유 게이트에서 3소스 canonical case로
    해소한 값이므로 그대로 발행 case다(`_validate_dst_prefix`는 형식 sanity만 담당).
    """
    src_key = ticket_rules._fold_key(src)
    id_map: dict[str, str] = {}
    for t in tickets:
        if ticket_rules._fold_key(t["prefix"]) != src_key:
            continue
        new_id = areas_registry._format_ticket_id(dst, t["num"])
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _merge_append_map(sources: list[str | None], into: str | None,
                      tickets: list[dict[str, Any]]) -> dict[str, str]:
    """merge 기본(append) 맵 — source 티켓을 created 순으로 대상 max 번호 *뒤에* 재부여.

    대상(into) 티켓 번호는 무변경(저위험). tiebreak = (source 목록 순서, 기존 번호) 로 같은
    created 안에서 기존 상대순서를 보존한다.
    created 부재(빈 문자열) 티켓은 정렬에서 맨 앞(최고령)으로 간다 — created 없는 티켓은 대개
    구세대(초기 도입 전) 산출물이라 최고령 배치가 자연스럽다(suggestion 채택·현행 유지).

    source membership 은 **case-insensitive fold**(`_fold_key`) — `merge aaa --into bbb`
    가 기존 `T-AAA-*` source 를 모은다. into(대상) max 계산은 exact(`== into`) — 호출자의
    ``require_prefix_user_ack``가 into를 canonical case로 해소하므로 case-혼동이 없다
    (source 측만 fold·`_validate_dst_prefix`는 형식 sanity만 담당).
    """
    src_index = {ticket_rules._fold_key(p): i for i, p in enumerate(sources)}
    src_tickets = [t for t in tickets if ticket_rules._fold_key(t["prefix"]) in src_index]
    start = max((t["num"] for t in tickets if t["prefix"] == into), default=0)
    ordered = sorted(
        src_tickets, key=lambda t: (t["created"], src_index[ticket_rules._fold_key(t["prefix"])], t["num"]))
    id_map: dict[str, str] = {}
    for i, t in enumerate(ordered, start=start + 1):
        new_id = areas_registry._format_ticket_id(into, i)
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _merge_reorder_map(sources: list[str | None], into: str | None,
                       tickets: list[dict[str, Any]]) -> dict[str, str]:
    """merge --reorder-chronological 맵 — 대상+source 전체를 created 순으로 1..N 재번호(opt-in).

    전체 interleave 라 대상 티켓 ID 도 바뀌어 전 참조 rewrite(17k refs 고위험)라 opt-in 이다.
    tiebreak: 대상 그룹 먼저(-1), 그다음 source 목록 순서, 그다음 기존 번호.

    source membership 은 case-insensitive fold(`_fold_key`)·into 그룹은 exact(`== into`·
    호출자의 ``require_prefix_user_ack``가 canonical case 해소) — `_merge_append_map`과 동일 규율.
    """
    src_index = {ticket_rules._fold_key(p): i for i, p in enumerate(sources)}
    involved = [t for t in tickets
                if t["prefix"] == into or ticket_rules._fold_key(t["prefix"]) in src_index]

    def _key(t: dict[str, Any]) -> tuple[str, int, int]:
        group = -1 if t["prefix"] == into else src_index[ticket_rules._fold_key(t["prefix"])]
        return (t["created"], group, t["num"])

    id_map: dict[str, str] = {}
    for i, t in enumerate(sorted(involved, key=_key), start=1):
        new_id = areas_registry._format_ticket_id(into, i)
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _collision_key(tid: str) -> tuple[str | None, int] | str:
    """collision 판정용 정규화 키 — `(prefix, 논리번호)`. 파싱 불가면 문자열 그대로(방어적).

    문자열 비교로는 zero-pad 폭이 다른 표기가 서로 다른 최종 ID로 보여 *같은
    논리 티켓번호*의 공존을 놓친다(내부 reviewer should-fix). `(prefix, int)` 로 정규화해 폭
    불일치도 같은 키로 묶어 abort 로 잡는다. prefix 는 **case-insensitive fold**(`_fold_key`·
    이미 오염된 `T-AAA-001`/`T-aaa-001` 공존 보드에서 rename/merge 가 그 case-split 를
    collision 으로 잡는다(무손실 abort). 순번 미파싱(malformed)은 리터럴 문자열로 폴백해 자기끼리만
    충돌 판정(오검출 없음).
    """
    num = ticket_rules._ticket_id_number(tid)
    if num is None:
        return tid
    return (ticket_rules._fold_key(ticket_rules._ticket_prefix(tid)), num)


def _detect_collisions(id_map: dict[str, str], all_ids: set[str]) -> list[str]:
    """relabel 후 new ID 유일성 검사 — 중복(같은 논리 ID 로 ≥2 티켓)을 정렬 목록으로 반환.

    최종 ID = `id_map.get(cur, cur)`(맵에 없으면 자기 ID 유지). 유일성은 문자열이 아니라
    `(prefix, 논리번호)`(`_collision_key`)로 판정한다. zero-pad 폭만
    다른 같은 논리번호도 충돌로 잡는다. 두 티켓이 같은 논리 ID 로 떨어지면 relabel 이 티켓을
    잃으므로 abort 해야 한다(무손실 원칙).
    """
    seen: dict[tuple[str | None, int] | str, list[str]] = {}
    for cur in all_ids:
        final = id_map.get(cur, cur)
        seen.setdefault(_collision_key(final), []).append(final)
    dup: list[str] = []
    for finals in seen.values():
        if len(finals) > 1:
            dup.extend(finals)
    return sorted(set(dup))


def _is_rewritable(path: Path) -> bool:
    """`rewrite_refs` 가 본문을 읽어 치환할 수 있는 파일인지 — 읽기 실패(비-UTF-8·권한)면 False.

    `rewrite_refs` 의 read 와 *같은 방식*(`encoding="utf-8"` strict·`newline=""`)으로 프로브해,
    거기서 skip 될 파일을 `_plan_file_renames` 도 정확히 같은 판정으로 걸러내게 한다(둘의 skip
    결정이 어긋나면 파일명↔content 불일치가 생긴다).
    """
    try:
        with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as fh:
            fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _plan_file_renames(home, id_map: dict[str, str]) -> list[tuple[Path, Path]]:
    """slug 파일명 rename 계획 — `T-old-slug.md` → `T-new-slug.md` (상태 디렉토리 보존).

    canonical ID 는 **frontmatter `id:` 1차 진실**(`_canonical_ticket_id`)로 잡고, 파일명은 그 ID 로
    시작하므로 prefix 구간만 new ID 로 바꾸고 나머지(`-slug.md`)는 보존한다(slug 안에 old ID 가 또
    있어도 오치환 없음). 파일명-only 파싱은 숫자로 끝나는 legacy slug(ID 뒤 숫자 slug가 붙은 파일명)를
    prefixed ID 로 오인해 rename 을 누락시키므로, 스캔측(`_scan_prefix_tickets`)과
    같은 fm-우선 해소로 통일한다. canonical 이 파일명 접두가 아니면(코너/corrupt) slug 슬라이스가
    어긋나므로 파일명 파싱값으로 폴백해 접두 일치를 보장한다(비손실·기존 동작 보존).

    **본문 rewrite 가 스킵될 티켓 파일(비-UTF-8·읽기 실패)은 rename 도 제외**한다(suggestion 채택·
    `rewrite_refs` 가 그 파일의 content id 를 못 바꿔 남겨두는데 파일명만 new ID
    로 바꾸면 파일명↔content id 가 어긋난다. 같은 read 프로브(`_is_rewritable`)로 걸러 파일명을
    유지하고 stderr 경고로 수동 확인을 유도한다(silent 누락 금지·`rewrite_refs` 의 skip 경고와 짝).
    """
    renames: list[tuple[Path, Path]] = []
    for status in (*ticket_rules.STATUS_DIRS, ".drafts"):   # .drafts 포함 — _scan_prefix_tickets 와 lockstep.
        for p in (pm_paths.tickets_dir(home) / status).glob("T-*.md"):
            tid = ticket_store._canonical_ticket_id(p)       # frontmatter id 정본 (숫자-slug 모호성 해소).
            if not (tid and p.name.startswith(tid)):
                tid = ticket_rules._ticket_id_from_filename(p.name)   # 접두 불일치(corrupt) → 파일명 폴백.
            if not (tid and tid in id_map):
                continue
            if not _is_rewritable(p):
                print(f"  ⚠ rename skip {p.name} — 본문 rewrite 불가(비-UTF-8/읽기 실패): "
                      "파일명↔content id 불일치 방지 위해 파일명 유지. 수동 확인.", file=sys.stderr)
                continue
            new_name = id_map[tid] + p.name[len(tid):]
            renames.append((p, p.with_name(new_name)))
    return renames


def _plan_rounds_dir_renames(home,
        renames: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """티켓 파일 rename 계획을 따라 그 티켓의 라운드 디렉터리도 함께 옮길 계획을 세운다.

    라운드는 티켓 ID 로 이름 붙은 디렉터리 하나에 모여 있으므로 옮길 것도 **디렉터리 하나**다
    (파일별 계획이 아니다). 두고 가면 새 ID 는 라운드가 없는 티켓이 되고 옛 ID 자리에는
    티켓 없는 라운드가 남는다 — 그 상태에서 순번 연속성 판정은 아무 것도 못 본다.
    """
    plans: list[tuple[Path, Path]] = []
    for src, dst in renames:
        old_id = ticket_rules._ticket_id_from_filename(src.name)
        new_id = ticket_rules._ticket_id_from_filename(dst.name)
        if not old_id or not new_id or old_id == new_id:
            continue
        src_dir = round_store.rounds_dir_for_ticket(old_id, pm_paths.tickets_dir(home))
        if src_dir.is_dir():
            plans.append(
                (src_dir, round_store.rounds_dir_for_ticket(new_id, pm_paths.tickets_dir(home)))
            )
    return plans


def _apply_file_renames(renames: list[tuple[Path, Path]]) -> None:
    """이름 rename 을 2단계(src→tmp→dst)로 적용 — 번호 swap(reorder) 시 clobber 방지.

    대상은 티켓 파일과 그 티켓의 라운드 디렉터리다(둘 다 같은 부모 안 rename 이라 규칙이 같다).

    reorder 는 두 티켓이 번호를 맞바꿀 수 있어(T-P-001↔T-P-002) 직접 rename 하면 중간에 대상을
    덮어쓴다. 먼저 전부 유니크 tmp(`.relabel-N.tmp`·dot-prefix 라 STATUS glob 밖)로 옮긴 뒤
    tmp→최종으로 옮겨 사이클/swap 에서도 무손실이다.
    """
    staged: list[tuple[Path, Path]] = []
    for i, (src, dst) in enumerate(renames):
        tmp = src.with_name(f".relabel-{i}.tmp")
        os.rename(src, tmp)
        staged.append((tmp, dst))
    for tmp, dst in staged:
        os.rename(tmp, dst)


def _home_git_status_porcelain(home, ) -> str | None:
    """홈(superproject) git working tree 의 uncommitted 변경 (`status --porcelain`).

    None = git 부재/repo 아님(보고 skip·안전) · `""` = clean · non-empty = dirty. prefix
    rewrite 는 wiki/log(홈 git)를 건드리므로 relabel diff 가 남의 WIP 와 섞여 보일 수 있다 —
    그래서 이 값을 **안내로** 낸다.
    남의 dirty 로 내 작업이 막히는 과차단이었고, mutation 자체가 만진 경로만 커밋하므로
    격리는 스코프가 보장한다. fail-soft: 예외는 None.
    """
    if shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str((home.root)), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=git._BOARD_GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):  # fail-soft: 판정 예외는 가드 skip(None).
        return None
    if r.returncode != 0:
        return None  # not a git repo → 가드 skip
    return r.stdout


def _prefix_relabel(home, build_map, *, verb: str, label: str,
                    dry_run: bool, noun: str = "prefix",
                    pre_write_check: Callable[[], bool] | None = None,
                    preview_notice: str | None = None) -> int:
    """검증된 old→new 맵을 적용하는 공통 파이프라인 (rename/merge/reid 공용).

    `noun` = 출력·commit 메시지의 op 접두어(카테고리 동사는 "prefix", 단건 재부여는 "reid" —
    `op = f"{noun} {verb}".strip()`·verb 빈 문자열이면 noun 만). 기본값은 prefix 동사 하위호환.

    dry-run: 규모 preview("N ID·M refs·K 파일")만·쓰기 0(read-only 라 락 불요). 적용: 홈 git
    dirty 안내(**차단 아님**) → **단일 `board_lock()` 구간에서** 본문 토큰
    rewrite(`rewrite_refs`) + slug 파일명 rename → board.md 재생성 → **만진 경로만** board-git
    백업 commit(분리 형상)·legacy skip 안내. 티켓 물리삭제 없음.

    **락 직렬화(codex must-fix)**: rewrite→rename→refresh→board-git 백업 전체를 `cmd_new` 의 ID
    발행·`cmd_claim` 이 쓰는 그 board_lock 으로 감싼다— 동시 new/claim 과 직렬화해
    relabel 이 그들 사이에 끼어 참조를 절반만 고치는 것을 막는다. **재진입 정리**: 이 구간 안에서
    부르는 board.md 재생성은 `refresh_board`(자체 board_lock·재진입 데드락)가 아니라 락-보유 전제
    변형 `_refresh_board_locked` 를 직접 부른다. board-git 백업(`_board_git_*`)은 board_lock 이
    아니라 별도 git repo(subprocess)를 만지므로 재획득이 없다(구간 안이어도 데드락 없음).
    ticket lifecycle/tier/migrate writer도 같은 board_lock을 쓰므로 relabel 적용 구간과 직렬화된다.

    **TOCTOU 봉합**: `build_map` 은 (id_map|None, rc) 를 반환하는 클로저다 —
    스캔→맵→collision 검사를 *락 안 fresh snapshot* 에서 수행해, 검사와 적용 사이에 `cmd_new` 가
    같은 번호를 발행해 stale 검사를 통과하는 창을 없앤다. 추가 belt: stage1 전에 계획된 dst 가
    (계획 밖 파일로) 이미 존재하면 **아무것도 쓰기 전에 abort** 한다(덮어쓰기 원천 차단).
    ``pre_write_check``가 있으면 락 안 fresh snapshot 판정을 맵 재생성보다 먼저 수행한다.
    dry-run 은 read-only preview 라 락 없이 빌드하고 이 write-only check를 호출하지 않는다
    (정확성보다 규모 감이 목적).
    """
    root = (home.root) / ".project_manager"
    op = f"{noun} {verb}".strip()   # "prefix rename" / "prefix merge" / "reid" (verb 빈 문자열이면 noun만)

    def _scale_line(n_ids: int, scale: dict[str, int], n_renames: int) -> str:
        return (f"{n_ids} ID 변경 · {scale['refs']} refs · "
                f"{scale['files']} 파일(본문 rewrite) · {n_renames} 파일명 rename")

    if dry_run:
        # read-only preview — 어떤 파일도 안 쓰므로 board_lock 을 잡지 않는다(스냅샷 오차 허용).
        id_map, rc = build_map()
        if not id_map:
            return rc
        scale = rewrite_refs(root, id_map, dry_run=True)
        print(f"[dry-run] {op} {label} — "
              f"{_scale_line(len(id_map), scale, len(_plan_file_renames(home, id_map)))}")
        print("[dry-run] 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
        if preview_notice:
            print(preview_notice)
        return 0

    # 홈 git dirty 는 **차단하지 않고 보고**한다. 전면 abort 는 "공유
    # 트리를 나 혼자 쓴다"는 가정에 서 있어, 멀티-PM 에서 *남의* WIP 하나로 내 relabel 이 막혔다
    # 아래 mutation 은 자기가 만진 경로만
    # 커밋하므로 남의 dirty 와 무관하고, 리뷰용 격리는 이 안내 + board-git 백업 rev 로 갈음한다.
    dirty = _home_git_status_porcelain(home)
    if dirty:
        n_dirty = len([ln for ln in dirty.splitlines() if ln.strip()])
        print(f"  ⓘ 홈 git 에 무관한 미커밋 변경 {n_dirty}건 — relabel 이 만진 파일과 뒤섞여 보이니 "
              "리뷰 시 유의하라(차단하지 않는다). 이 명령은 자기가 만진 경로만 커밋한다.")

    # board-git 백업 rev — 분리 형상에서 relabel *직전* HEAD(되돌아갈 지점). rewrite 뒤 새 commit
    # 이 relabel 을 board-git 에 기록하므로 이 rev 로 `reset --hard` 하면 원복된다.
    backup_rev = board_git._board_git_head(home) if board_git._board_git_enabled(home) else None

    # 전체 mutation 을 단일 board_lock 으로 직렬화 (동시 new/claim 과 상호배제). 파일명
    # rename 계획은 락 보유 하에 fresh scan 으로 세운다(rewrite 와 같은 스냅샷). refresh 는 락-보유
    # 변형(`_refresh_board_locked`)을 직접 불러 board_lock 재진입(데드락)을 피한다.
    # 백업 commit 이 이 구간 안에 있으므로 **board_git_lock 을 board_lock 보다 먼저** 잡는다
    # (락 순서 고정 — 역순 획득이 하나라도 있으면 claim 과 데드락). board-git 비활성이면
    # board_git_lock 은 no-op 이라 legacy 경로는 무변경.
    with board_git.board_git_lock(home), fs.board_lock(home):
        if pre_write_check is not None and not pre_write_check():
            return 1
        # 맵 생성+collision 검사도 락 안 fresh snapshot 으로 — 검사↔적용 사이 cmd_new 창 봉합.
        id_map, rc = build_map()
        if not id_map:
            return rc
        renames = _plan_file_renames(home, id_map)
        rounds_renames = _plan_rounds_dir_renames(home, renames)
        # dst 선검증(belt·아무것도 쓰기 전): 계획 밖 파일이 dst 를 점유하면 abort — 덮어쓰기 차단.
        planned_srcs = {src for src, _ in renames}
        planned_srcs |= {src for src, _dst in rounds_renames}
        occupied = [dst for _, dst in renames + rounds_renames
                    if dst.exists() and dst not in planned_srcs]
        if occupied:
            sample = ", ".join(x.name for x in occupied[:5])
            print(f"[중단] rename 대상 경로가 이미 존재({sample}) — 덮어쓰기 방지 위해 적용 전 "
                  "abort(쓰기 0). 보드 상태 재확인 후 재시도.", file=sys.stderr)
            return 1
        # 이 relabel 이 실제로 만진 경로만 모아 스코프 커밋한다 — rewrite 가 쓴
        # 파일 + rename 의 옛/새 경로. 옛 경로는 더 이상 존재하지 않지만 index 에는 있어
        # `git_scope_stageable` 의 추적 판정이 살려낸다(삭제가 커밋에 실려야 rename 이 완성).
        touched: list[Path] = []
        scale = rewrite_refs(root, id_map, dry_run=False, changed_paths=touched)
        all_renames = renames + rounds_renames
        _apply_file_renames(all_renames)
        for src, dst in all_renames:
            touched.extend((src, dst))
        board_dashboard._refresh_board_locked(home)
        if board_git._board_git_enabled(home):
            board_git._board_git_stage_and_commit(home, f"{op} {label}", touched)

    areas_registry.invalidate_known_prefixes_cache()

    if board_git._board_git_enabled(home):
        if backup_rev:
            print(f"  board-git 백업 rev(되돌리려면 "
                  f"`git -C .project_manager/board reset --hard {backup_rev[:12]}`)")
    else:
        print("  legacy(board-git 미분리) — board 변경은 홈 git 에 함께 있다. "
              "되돌리려면 홈 git 으로 revert.")
    print(f"{op} {label} — {_scale_line(len(id_map), scale, len(renames))} 적용 완료.")
    return 0


def _validate_dst_prefix(raw: str, parsed: str | None) -> str | None:
    """대상 카테고리(dst/into) 형식 sanity — 위반 메시지·정상이면 None.

    `none`(parsed=None)은 항상 허용(이름 지우기·무prefix). 실 prefix 는 새/재사용 카테고리
    이름이므로 `_validate_prefix`(예약어+`[A-Za-z0-9][A-Za-z0-9_]*`·대소문자 허용)로
    못박아 malformed ID 발행을 막는다. source prefix 는 *기존* 발행분이라 검증하지 않는다(하이픈
    legacy 존중). 기존 prefix와의 fold 동일성 및 canonical case 해소는 네 표면이 공유하는
    ``require_prefix_user_ack``가 3소스 합집합으로 맡는다.
    """
    if parsed is None:
        return None
    reason = ticket_rules._validate_prefix(raw)
    if reason:
        return reason
    return None


def cmd_prefix_rename(home, args: argparse.Namespace) -> int:
    """`prefix rename <A|none> <B|none>` — 무충돌=번호유지 교체·충돌=merge 안내."""
    src = areas_registry._parse_prefix_arg(args.src)
    requested_dst = areas_registry._parse_prefix_arg(args.dst)
    dst = requested_dst
    dry_run = bool(getattr(args, "dry_run", False))
    if src == dst:
        print(f"[중단] src 와 dst 가 같다({args.src} → {args.dst}) — 변경 없음.",
              file=sys.stderr)
        return 1
    reason = _validate_dst_prefix(args.dst, dst)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    dst_is_new = False
    if requested_dst is not None:
        candidate, dst_is_new = areas_registry._prefix_target_snapshot(
            home, requested_dst, surface="board prefix rename dst",
        )
        if candidate is None:
            return 1
        dst = candidate

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot 에서 스캔→맵→collision (TOCTOU 봉합·dry-run 은 락 밖 호출).
        tickets, scan_rc = areas_registry._prefix_scan(home, "prefix rename")
        if tickets is None:
            return None, scan_rc
        id_map = _rename_map(src, dst, tickets)
        if not id_map:
            print(f"prefix {args.src} 에 해당하는 티켓이 없다 — 변경 없음.")
            return None, 0
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5]) + (" …" if len(collisions) > 5 else "")
            print(f"[중단] rename 충돌 — {args.dst} 네임스페이스에 번호가 겹친다({sample}). 번호 유지 "
                  f"rename 불가. `board.py prefix merge {args.src} --into {args.dst}` 로 created 순 "
                  f"재번호 통합하라(append·무손실).", file=sys.stderr)
            return None, 1
        return id_map, 0

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if dst_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_dst!r} 신설에는 사용자 승인이 필요하다 — "
                f"승인한 사용자만 `--user-ack {requested_dst}`를 대상값 그대로 붙여 실행하라."
            )
    else:
        # 승인 게이트는 실제 변경 맵이 있을 때만 연다. source 0건 no-op은 여기서 바로 끝나
        # 쓰기·승인 모두 0이고, 실제 적용은 아래에서 같은 맵을 락 안 fresh scan으로 다시 만든다.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_dst is not None:
            canonical = areas_registry.require_prefix_user_ack(
                home, requested_dst,
                getattr(args, "user_ack", None),
                surface="board prefix rename dst",
            )
            if canonical is None:
                return 1
            dst = canonical
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                home, requested_dst,
                getattr(args, "user_ack", None),
                canonical,
                surface="board prefix rename dst",
            ) is not None

    dst_label = dst if dst is not None else "none"
    return _prefix_relabel(home, build_map, verb="rename", label=f"{args.src} → {dst_label}",
                           dry_run=dry_run, pre_write_check=pre_write_check,
                           preview_notice=preview_notice)


def cmd_prefix_strip(home, args: argparse.Namespace) -> int:
    """`prefix strip <A>` — `rename <A> none` 의 별칭(별도 로직 없음)."""
    return cmd_prefix_rename(home, argparse.Namespace(
        src=args.prefix, dst="none", dry_run=getattr(args, "dry_run", False)))


def cmd_prefix_merge(home, args: argparse.Namespace) -> int:
    """`prefix merge <A> [B...] --into <T|none>` — created 순 통합(기본 append)."""
    sources = [areas_registry._parse_prefix_arg(s) for s in args.sources]
    requested_into = areas_registry._parse_prefix_arg(args.into)
    into = requested_into
    dry_run = bool(getattr(args, "dry_run", False))
    reason = _validate_dst_prefix(args.into, into)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    into_is_new = False
    if requested_into is not None:
        candidate, into_is_new = areas_registry._prefix_target_snapshot(
            home, requested_into, surface="board prefix merge --into",
        )
        if candidate is None:
            return 1
        into = candidate
    if ticket_rules._fold_key(into) in {ticket_rules._fold_key(s) for s in sources}:
        # 자기-merge 가드도 case-insensitive — `merge aaa --into AAA` 는 fold-동일
        # 네임스페이스라 자기 자신에 merge 다(source fold-매칭이 노출한 클래스).
        print(f"[중단] --into 대상({args.into})이 source 목록에 있다 — 자기 자신에 merge 불가"
              "(대소문자 무관).", file=sys.stderr)
        return 1
    reorder = bool(getattr(args, "reorder_chronological", False))

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot — merge 의 append start=max(...) 도 stale 이면 clobber.
        tickets, scan_rc = areas_registry._prefix_scan(home, "prefix merge")
        if tickets is None:
            return None, scan_rc
        id_map = (_merge_reorder_map if reorder else _merge_append_map)(sources, into, tickets)
        if not id_map:
            print("통합할 source 티켓이 없다 — 변경 없음.")
            return None, 0
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5]) + (" …" if len(collisions) > 5 else "")
            print(f"[중단] merge 충돌(new ID 유일성 위배): {sample}. 적용하지 않음.",
                  file=sys.stderr)
            return None, 1
        return id_map, 0

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if into_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_into!r} 신설에는 사용자 승인이 필요하다 — "
                f"승인한 사용자만 `--user-ack {requested_into}`를 대상값 그대로 붙여 실행하라."
            )
    else:
        # rename과 같은 정책: source 0건이면 승인 없이 no-op, 실제 변경 맵이 있을 때만 gate.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_into is not None:
            canonical = areas_registry.require_prefix_user_ack(
                home, requested_into,
                getattr(args, "user_ack", None),
                surface="board prefix merge --into",
            )
            if canonical is None:
                return 1
            into = canonical
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                home, requested_into,
                getattr(args, "user_ack", None),
                canonical,
                surface="board prefix merge --into",
            ) is not None

    mode = "reorder" if reorder else "append"
    into_label = into if into is not None else "none"
    label = f"{'+'.join(args.sources)} → {into_label} [{mode}]"
    return _prefix_relabel(home, build_map, verb="merge", label=label,
                           dry_run=dry_run, pre_write_check=pre_write_check,
                           preview_notice=preview_notice)


def _areas_clear_prefix_cell(home, prefix: str) -> int:
    """areas.md 에서 prefix 셀이 `prefix` 인 행의 그 셀을 빈 값으로 in-place 편집한다(행 보존).

    repo 등록 행 자체는 남기고 prefix 칼럼만 비운다
    동형·무손실. areas write 는 진짜 공유 mutation 이라 board_lock 으로 동시 `areas_append` 와의
    lost-update 를 막는다(`_migrate_areas_apply` 동형). **재진입 금지**: 락 안에서
    board_lock 을 다시 잡는 헬퍼는 부르지 않는다(순수 텍스트 RMW 만). 반환 = 비운 셀(행) 수.

    prefix 칼럼 index 는 헤더에서 얻는다(canonical per-repo 레지스트리). 헤더에 prefix 칼럼이
    없거나 areas.md 부재면 0(무변경). 변경된 행만 canonical `| … |`(areas_append 와 동형)로 재조립
    하고 나머지 줄(헤더·구분선·무관 행·산문)은 원문 그대로 보존한다.
    """
    af = pm_paths.areas_file(home)
    with fs.board_lock(home):
        if not af.exists():
            return 0
        text = fs.read_text_shared(af, encoding="utf-8")
        ends_nl = text.endswith("\n")
        header_seen = False
        pidx: int | None = None
        cleared = 0
        out: list[str] = []
        for line in text.splitlines():
            cells = areas_rules._split_areas_row(line)
            if cells is None:                       # 구분선·빈 줄·비-표 산문
                out.append(line)
                continue
            if not header_seen:                     # 첫 table row = 헤더
                header_seen = True
                lower = [c.lower() for c in cells]
                pidx = lower.index("prefix") if "prefix" in lower else None
                out.append(line)
                continue
            if pidx is not None and pidx < len(cells) and cells[pidx] == prefix:
                cells[pidx] = ""
                cleared += 1
                out.append("| " + " | ".join(cells) + " |")
            else:
                out.append(line)
        if cleared:
            af.write_text(
                "\n".join(out) + ("\n" if ends_nl else ""),
                encoding="utf-8", newline="\n")
    if cleared:
        areas_registry.invalidate_known_prefixes_cache()
    return cleared


def cmd_prefix_delete(home, args: argparse.Namespace) -> int:
    """`prefix delete <A>` — 빈(0티켓) prefix 의 areas 등록을 지운다·티켓 있으면 fail-loud.

    prefix 는 티켓으로만 존재하는 작업 카테고리(자동시드 폐지)다. 0 티켓이면:
      - areas.md 에 A 가 등록돼 있으면 → 그 행의 **prefix 셀을 실제로 비운다**(행·repo 등록 보존·
        무손실·PM 홈 수동 조치와 동형). promise=do — 메시지가 실제 셀 편집과 일치한다.
      - 미등록이면 → 지울 등록이 없으므로 "확인만"(변경 0)으로 정직하게 보고한다.
    티켓이 있으면 물리삭제 없이 rename/merge 로 안내한다(fail-loud). `--dry-run` 은 쓰기 0·규모
    preview(다른 동사와 공통 표기).
    """
    target = areas_registry._parse_prefix_arg(args.prefix)
    if target is None:
        print("[중단] none(무prefix) 네임스페이스는 delete 불가 — 기준 네임스페이스다.",
              file=sys.stderr)
        return 1
    dry_run = bool(getattr(args, "dry_run", False))
    # 티켓 존재 카운트는 **case-insensitive fold**(`_fold_key`) — `delete AAA` 가 case-변종
    # `T-aaa-*` 도 세어, fold-비지 않은 네임스페이스를 "빈 것"으로 오판해 등록만 지우는 것을 막는다.
    target_key = ticket_rules._fold_key(target)
    # 손상 티켓이 있으면 "0 티켓" 판정 자체를 믿을 수 없다 — 그 티켓이 이 네임스페이스일 수 있다.
    tickets, scan_rc = areas_registry._prefix_scan(home, "prefix delete")
    if tickets is None:
        return scan_rc
    count = sum(1 for t in tickets if ticket_rules._fold_key(t["prefix"]) == target_key)
    if count > 0:
        print(f"[중단] prefix {args.prefix} 에 티켓 {count}개 — delete 는 빈(0티켓) prefix 전용"
              f"(무손실·물리삭제 없음). 개명은 `board.py prefix rename {args.prefix} <B|none>`, "
              f"통합은 `board.py prefix merge {args.prefix} --into <T|none>` 로. "
              "B/T가 새 라벨이면 1순위로 사용자에게 승인을 요청하고, 승인한 "
              "사용자만 대상값과 같은 `--user-ack <B|T>`를 붙인다(세션 자동 부착 금지).",
              file=sys.stderr)
        return 1
    registered = areas_store._areas_row_for_prefix(home, target) is not None
    if not registered:
        print(f"prefix {args.prefix} — 0 티켓·areas 등록 없음. 확인만(변경 없음).")
        return 0
    if dry_run:
        print(f"[dry-run] prefix delete {args.prefix} — 0 티켓·areas 등록 셀 비움 예정"
              f"(행·repo 등록 보존). 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
        return 0
    cleared = _areas_clear_prefix_cell(home, target)
    print(f"prefix {args.prefix} — 0 티켓·areas 등록 셀 {cleared}행 비움(행·repo 등록 보존·무손실).")
    return 0


def cmd_reid(home, args: argparse.Namespace) -> int:
    """`reid <OLD-ID> <NEW-ID> [--dry-run]` — 단일 티켓 ID 를 무손실 재부여한다.

    잘못 발행된 티켓 1장의 ID를 다른 번호나 prefix로 바꾸며 파일명·
    frontmatter·**전 참조**(board 내 depends_on/blocks·타 티켓 본문·wiki/log wikilink)까지 한 번에
    고친다. 카테고리 일괄이 아니라 단건이므로 top-level 서브커맨드다(`prefix` 네임스페이스는 카테고리
    전용 유지).

    가드(값싼 정적 → 상태 의존 순): NEW-ID 형식 sanity(발행 문법·legacy prefix 포함) →
    NEW prefix 3소스 canonical/신설 승인 판정 → src≠dst → (락 안 fresh snapshot) OLD 실재 →
    타 세션 claim abort(단일세션 op) → NEW collision(전 상태
    디렉토리+.drafts 에 이미 존재하면 abort). 번호 자동발급 카운터는 `_next_id` 가 max 기반이라 어느
    번호로 옮겨도 무충돌이다 — 다음 발급이 최대치를 자연히 이으므로 별도 조정 없이 확인만 한다(결정).
    """
    old_id, requested_new_id = args.old_id, args.new_id
    new_id = requested_new_id
    dry_run = bool(getattr(args, "dry_run", False))

    # 정적 sanity(락·재조회 불요) — 값싼 거부 먼저. OLD/NEW 형식·자기 자신.
    # OLD-ID 도 형식 선검증: find_ticket 은 glob 기반이라 메타문자(`*`·`?`·`[]`)
    # 든 OLD 가 임의 티켓에 매치된 뒤 rewrite 는 리터럴 키라 no-op 인데 성공처럼 끝날 수 있다.
    if not ticket_rules._is_valid_ticket_id(old_id):
        print(f"[중단] OLD-ID {old_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 `T-<prefix>-NNN`"
              "(발행 문법) 이어야 한다.", file=sys.stderr)
        return 1
    if not ticket_rules._is_valid_ticket_id(requested_new_id):
        print(f"[중단] NEW-ID {requested_new_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 "
              "`T-<prefix>-NNN`(발행 문법·legacy prefix 포함) 이어야 한다.",
              file=sys.stderr)
        return 1

    requested_new_prefix = ticket_rules._ticket_prefix(requested_new_id)
    new_prefix_is_new = False
    canonical_new_prefix: str | None = None
    if requested_new_prefix is not None:
        canonical_new_prefix, new_prefix_is_new = areas_registry._prefix_target_snapshot(
            home, requested_new_prefix, surface="board reid NEW-ID prefix",
        )
        if canonical_new_prefix is None:
            return 1
        if new_prefix_is_new:
            reason = ticket_rules._validate_prefix(canonical_new_prefix)
            if reason:
                print(
                    f"[중단] 신규 prefix {canonical_new_prefix!r} 형식 위반 — {reason}",
                    file=sys.stderr,
                )
                return 1
        # fold-매치 기존 prefix는 그 canonical case로 무마찰 재사용한다.
        # 사용자가 쓴 번호 마디의 zero-pad 폭은 reid 기존 계약대로 보존한다.
        new_id = f"T-{canonical_new_prefix}-{requested_new_id.rsplit('-', 1)[1]}"

    if old_id == new_id:
        print(f"[중단] OLD 와 NEW 가 같다({old_id}) — 변경 없음.", file=sys.stderr)
        return 1

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 상태 의존 검사는 (적용 경로에선) `_prefix_relabel` 이 잡은 board_lock 안 fresh snapshot 에서
        # 수행한다 — 검사↔적용 사이 cmd_new/claim 창을 봉합한다(prefix rename build_map 동형).
        # OLD 티켓을 canonical ID 로 정확 선택한다. `find_ticket` 은 `{old_id}-*.md` glob 의 *첫*
        # legacy ID 부재 시 같은 숫자로 시작하는 prefixed ID가
        # glob에 걸려 무동작하거나, 두 표기의 파일이 공존할 때 디렉토리
        # 순서에 따라 후자가 먼저 잡혀 canonical mismatch 로 실재하는 OLD 를 놓침(false-negative).
        # `_scan_prefix_tickets`(fm.get("id") or 파일명 폴백·전 상태 디렉토리+.drafts)로 전수 스캔해
        # canonical ID 가 정확히 old_id 인 레코드만 고른다. 스냅샷은 **한 번만** 뜬다 — OLD 선택과
        # NEW collision 판정이 같은 스냅샷을 봐야 그 사이의 변화로 판정이 갈리지 않는다.
        tickets, scan_rc = areas_registry._prefix_scan(home, "reid")
        if tickets is None:
            return None, scan_rc
        matches = [t for t in tickets if t["id"] == old_id]
        if not matches:
            print(f"[중단] OLD 티켓 {old_id} 을 찾을 수 없다 — 재부여할 대상이 없다.",
                  file=sys.stderr)
            return None, 2
        if len(matches) > 1:
            # 같은 canonical ID 가 둘 이상 = 이미 오염된 보드 — 임의 선택은 그 자체로 silent-wrong-target.
            dup = ", ".join(t["path"].name for t in matches[:5])
            print(f"[중단] OLD 티켓 {old_id} 가 여러 파일에 존재한다({dup}) — 먼저 중복을 해소하라.",
                  file=sys.stderr)
            return None, 1
        path = matches[0]["path"]
        fm, _ = ticket_store.load_ticket(path)
        # 타 세션 claim 가드 — 단일세션 op(migrate-identity·prefix rename 동류). claim 중인
        # 티켓은 그 소유 세션만 reid 할 수 있다(다른 세션이 작업 중인 ID 를 바꿔 참조를 흔들지 않게).
        # 적용 경로에서는 아래 `_prefix_relabel`이 fresh map/소유 검사를 board_lock 안에서 다시
        # 수행한다. 이 선판정은 승인 안내를 위한 preview이며 쓰기 권위가 아니다.
        # 소유 판정은 lifecycle mutation 과 **같은 함수**다(`_ticket_ownership`) — 판정을 두 벌로
        # 두면 한쪽만 user 축을 보는 식으로 갈린다. 문구만 이 surface(단일세션 op)의 것을 쓴다.
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            remedy = ticket_rules._owner_session_remedy(ownership.owner or "")
            print(f"[중단] {old_id} 은 `{ownership.owner}` 가 claim 중 — reid 는 단일세션 op 다. "
                  f"{remedy}. 또는 먼저 unclaim 하라.",
                  file=sys.stderr)
            return None, 1
        # NEW collision — `_detect_collisions` 재사용(zero-pad 폭 정규화 포함). {OLD:NEW} 를 전 티켓
        # ID 집합에 적용해 두 티켓이 같은 논리 ID(전 상태 디렉토리+.drafts)로 떨어지면 잡는다.
        id_map = {old_id: new_id}
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5])
            print(f"[중단] NEW-ID {new_id} collision — 이미 존재하는 티켓과 번호가 겹친다({sample}). "
                  "미사용 ID 로 재부여하라(`board.py prefix list` 로 현황 확인).", file=sys.stderr)
            return None, 1
        return id_map, 0

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if new_prefix_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_new_prefix!r} 신설에는 사용자 "
                f"승인이 필요하다 — 승인한 사용자만 `--user-ack {requested_new_prefix}`를 "
                "대상값 그대로 붙여 실행하라."
            )
    else:
        # rename/merge와 동형: OLD 미존재·NEW collision 등으로 실제 맵이 없으면
        # 승인을 요구하지 않고, 쓰기 계획이 있을 때만 값-결속 게이트를 연다.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_new_prefix is not None:
            canonical = areas_registry.require_prefix_user_ack(
                home, requested_new_prefix,
                getattr(args, "user_ack", None),
                surface="board reid NEW-ID prefix",
            )
            if canonical is None:
                return 1
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                home, requested_new_prefix,
                getattr(args, "user_ack", None),
                canonical,
                surface="board reid NEW-ID prefix",
            ) is not None

    return _prefix_relabel(home,
        build_map,
        verb="",
        label=f"{old_id} → {new_id}",
        dry_run=dry_run,
        noun="reid",
        pre_write_check=pre_write_check,
        preview_notice=preview_notice,
    )
