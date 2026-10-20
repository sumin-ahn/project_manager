"""wikilink 와 파일명 무관 참조 lint (층: app).

검사 대상 문서 수집, `[[...]]` 대상 해소, 파일명에 기대는 불안정 참조 판정을 소유한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from pm_engine.domain import markdown_text, ticket_rules
from pm_engine.infra import fs, pm_paths, ticket_store


# ── wikilink lint ───────────────────────────────────────────
# 엔진은 *구조적으로 해석 가능한* 참조만 검증한다: [[ADR-NNNN]]·[[T-NNNN]]/[[T-PFX-NNN]]·
# [[idea-NNNN]] 가 실제 파일로 resolve 되는지. 자유어휘([[some-memory-slug]] 등)는 프로젝트마다
# 화이트리스트가 달라 엔진이 판정할 수 없으므로 건드리지 않는다(오탐 0) — 프로젝트 고유 링크 검사는
# lint 훅(.project_manager/hooks/lint_*.py)으로 분리. placeholder([[T-NNNN]]·[[xxxxx]])는
# 숫자 패턴이 아니라 자연히 제외된다.

# [[name]] 또는 alias [[name|display]] — name 만 캡처. backtick 안도 포함.
_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_\s.\-]+?)(?:\|[^\]]+)?\]\]")

# 어댑터 scaffold 경로 — fresh adopter 에 출하되는 harness 어댑터(.claude/.opencode).
# 채택자(특히 framework ADR 0001~ 이 없는 다운스트림 앱)는 자기 repo 의 scaffold 에서
# framework ADR/idea 를 [[bracket]] 참조하면 *영구 dangling* 이 된다 — 이는 정상이며
# push 를 막아선 안 된다("차단은 최소·advisory 우선"). `_collect_wikilink_files`
# 의 scaffold rel 목록과 동일 — POSIX 경계로 비교(_rel_to_repo 는 `/` 정규화).
# `.opencode/command/` **legacy-compat 로 유지** — 은퇴 전 import 한
# 채택자 트리엔 command 파일이 잔존하고(pm_update 는 복사만·은퇴 경로 삭제 안 함), 여기서 빼면 그
# 파일들의 framework wikilink 가 scaffold-advisory 대신 blocking dangling 으로 오분류된다.
_SCAFFOLD_PATH_PREFIXES: tuple[str, ...] = (
    ".claude/agents/", ".claude/skills/", ".opencode/agents/", ".opencode/command/")


def _is_scaffold_src(src: str) -> bool:
    """src(`_rel_to_repo` 결과)가 어댑터 scaffold 경로 하위인지 — `\\`→`/` 정규화 후 prefix 매칭."""
    norm = src.replace("\\", "/")
    return norm.startswith(_SCAFFOLD_PATH_PREFIXES)


def _collect_wikilink_files(home) -> list[Path]:
    """wikilink 검사 대상 .md — wiki/ 전체 + 레포 루트 CLAUDE.md·README.md + 어댑터 scaffold.

    어댑터 scaffold(`.claude/{agents,skills}`·`.opencode/{agents,command}`)도 스캔한다 —
    fresh adopter 엔 framework ADR/ticket 이 없으므로, 출하 scaffold 의 `[[ADR-NNNN]]` 같은
    구조참조 wikilink 가 그대로 새 나가면 fresh-clone 에서 dangling 이 된다. 가드가 wiki/ 만
    보던 동안 이 scaffold dangling 은 *구조적으로* 안 잡혔다(scaffold ref 를 늘림).
    각 dir 은 harness 별로 존재 여부가 다르므로(claude 채택자엔 `.opencode` 부재·역도 마찬가지)
    `.is_dir()` 가드로 있을 때만 추가한다.
    """
    # PM 운영 wiki는 등록 worktree read 폴백에서 소유 PM 홈을 따른다. 루트 README와
    # adapter scaffold는 아래에서 계속 REPO(현재 제품 worktree)를 읽어 코드 lint 의미를 보존한다.
    wiki = pm_paths.wiki_dir(home)
    files: list[Path] = list(wiki.rglob("*.md")) if wiki.is_dir() else []
    # board/ 분리 시 ticket 본문이 wiki/ 밖(board/tickets)으로 빠진다 — 그러면
    # ticket 의 `[[ADR-NNNN]]` 구조참조가 wiki-only 스캔에선 안 보여 dangling 이 *미검출*된다.
    # tickets_dir() 를 union 해 두 루트를 모두 본다. legacy(board_root==wiki)면 이 경로는 이미
    # wiki.rglob 에 포함되므로 set dedup 으로 no-op(중복 0). board/areas.md 등 비-md 는 제외.
    tk = pm_paths.tickets_dir(home)
    if tk.is_dir():
        files.extend(tk.rglob("*.md"))
    files = list(dict.fromkeys(files))  # 순서보존 dedup (legacy 중복 제거 + board union 합집합)
    for name in ("CLAUDE.md", "README.md"):
        p = home.root / name
        if p.exists():
            files.append(p)
    # `.opencode/command` = 은퇴 경로·legacy-compat 스캔 유지(_SCAFFOLD_PATH_PREFIXES 주석).
    for rel in (".claude/agents", ".claude/skills", ".opencode/agents", ".opencode/command"):
        d = home.root / rel
        if d.is_dir():
            files.extend(d.rglob("*.md"))
    return files


def _leading_num(filename: str) -> str | None:
    """파일명 선두 숫자를 0-strip 정규화해 반환 ('0028-foo.md' → '28'). 없으면 None."""
    m = re.match(r"(\d+)", filename)
    return (m.group(1).lstrip("0") or "0") if m else None


def _resolve_wikilink_targets(home) -> tuple[set[str], set[str], set[str]]:
    """(ticket_ids, adr_nums, idea_nums) — 실재하는 구조 참조 대상 집합."""
    ticket_ids = {fm.get("id") for _s, fm in ticket_store._all_tickets(home) if fm.get("id")}
    adr_nums: set[str] = set()
    if pm_paths.decisions_dir(home).is_dir():
        for p in pm_paths.decisions_dir(home).glob("[0-9]*.md"):
            n = _leading_num(p.name)
            if n is not None:
                adr_nums.add(n)
    idea_nums: set[str] = set()
    for status in ticket_rules.IDEA_STATUS_DIRS:
        for p in (pm_paths.ideas_dir(home) / status).glob("[0-9]*.md"):
            n = _leading_num(p.name)
            if n is not None:
                idea_nums.add(n)
    return ticket_ids, adr_nums, idea_nums


def lint_wikilinks(home) -> list[tuple[str, str, str]]:
    """Return dangling [[name]] for *structural* refs (ADR/ticket/idea) only.

    name 으로 dedupe 하고 사용처를 detail 에 모은다. 자유어휘는 검사하지 않는다.
    코드 span/fence 안의 *예시* wikilink(관례 문서가 backtick 으로 보여주는
    `[[ADR-NNNN]]`)는 실 참조가 아니므로 `_strip_code` 로 제거 후 스캔한다 —
    `lint_unstable_refs` 와 동일한 처리(오탐 0).

    kind 분류:
      - `dangling-wikilink`          = wiki/·root-doc(CLAUDE.md·README) 의 framework ADR/idea
        dangling, 그리고 **모든 ticket(`[[T-...]]`) dangling** — `lint --gate` 차단(blocking).
      - `dangling-wikilink-scaffold` = framework ADR/idea dangling 이 *오직* 어댑터 scaffold
        경로(`.claude/{agents,skills}`·`.opencode/{agents,command}`)에서만 등장 — advisory
        (`_ADVISORY_LINT_KINDS` 등재·`--gate` 미차단). 채택자(framework ADR 부재 다운스트림)의
        scaffold bracket-ref 는 영구 dangling 이 정상이라 push 를 막으면 안 된다.
    같은 ref 가 scaffold + wiki/root-doc 양쪽에서 dangle 하면 blocking 유지(프레임워크 자기
    문서는 dangle 하면 안 됨). per-occurrence source 경로를 추적해 분기한다(name 별로 ADR/idea
    여부 + 사용처 전부가 scaffold 인지).
    """
    ticket_ids, adr_nums, idea_nums = _resolve_wikilink_targets(home)
    # name → (is_ticket, [source rel paths]). is_ticket=True 면 항상 blocking,
    # False(ADR/idea)면 사용처가 전부 scaffold 일 때만 advisory 강등.
    dangling: dict[str, tuple[bool, list[str]]] = {}

    for path in _collect_wikilink_files(home):
        try:
            text = fs.read_text_shared(path, encoding="utf-8")
        except OSError:
            continue
        src = pm_paths._rel_to_repo(home, path)
        text = markdown_text._strip_code(text)
        for raw in _WIKILINK_RE.findall(text):
            name = raw.strip()
            m_adr = re.fullmatch(r"ADR-(\d+)", name)
            m_idea = re.fullmatch(r"idea-(\d+)", name)
            is_ticket = False
            if m_adr:
                ok = (m_adr.group(1).lstrip("0") or "0") in adr_nums
            elif re.fullmatch(ticket_rules._TICKET_ID_BODY, name):
                # prefixed(`T-PAY-001`)·legacy(`T-NNNN`) wikilink 둘 다 ticket 참조로 본다
                # (multi-repo 보드). grammar 는 `_TICKET_ID_BODY` 공유(자체 regex 금지).
                ok = name in ticket_ids
                is_ticket = True
            elif m_idea:
                ok = (m_idea.group(1).lstrip("0") or "0") in idea_nums
            else:
                continue  # 자유어휘 — 엔진 판정 안 함
            if ok:
                continue
            _t, srcs = dangling.setdefault(name, (is_ticket, []))
            if src not in srcs:
                srcs.append(src)

    issues: list[tuple[str, str, str]] = []
    for name in sorted(dangling):
        is_ticket, srcs = dangling[name]
        shown = ", ".join(srcs[:3]) + (f" (외 {len(srcs) - 3}개)" if len(srcs) > 3 else "")
        # ticket dangling 은 항상 blocking. ADR/idea 는 사용처가 *전부* scaffold 일 때만
        # advisory 강등 — 하나라도 wiki/root-doc 이면 framework 자기 문서 dangle 이라 blocking.
        scaffold_only = (not is_ticket) and all(_is_scaffold_src(s) for s in srcs)
        if scaffold_only:
            issues.append((name, "dangling-wikilink-scaffold",
                           f"[[{name}]] 대상 파일 없음 (어댑터 scaffold 참조 · 채택자 "
                           f"decisions/ 에 framework ADR 부재 = 정상) · 사용처: {shown}"))
        else:
            issues.append((name, "dangling-wikilink",
                           f"[[{name}]] 대상 파일 없음 · 사용처: {shown}"))
    return issues


# ── 파일명-무관 참조 강제  ──────────
# 엔진은 [[ADR-NNNN]] 를 *번호*로 resolve 하므로 슬러그는 무관하다. 그러나 LLM 이
# 구조화 디렉토리(decisions/·tickets/·ideas/)를 **생파일명·슬러그**로 가리키면 — markdown 경로
# 링크 `](…/decisions/<slug>.md)` 나 숫자선두 자유어휘 wikilink `[[NNNN-slug]]` — 슬러그가 바뀌면
# 부정확 참조가 된다. 이 둘은 *번호로 resolve 가능*
# resolve 실패 = dangling(차단), 실재하지만 슬러그 의존 = 권고(canonical ID-wikilink 로 전환).
# 자유어휘 일반([[some-memory]])·산문 언급은 *건드리지 않는다*.

# 구조화 디렉토리를 가리키는 markdown 링크. 견고성을 위해 2단계로 본다
# suggestion — 정규식만으론 link-form edge 가 새므로):
#   (a) `_MD_LINK_TARGET_RE` 로 링크 target 을 추출 — 선택적 `<…>` 꺾쇠·트레일링 `"title"` 허용.
#   (b) target 에서 fragment(`#…`)·query(`?…`)를 떼고, 외부 URL(`scheme://`)이면 건너뛴 뒤
#       `_STRUCT_PATH_RE` 로 구조화 경로(decisions/·tickets/<state>/·ideas/<state>/<file>.md)를 매칭.
# 이렇게 `.md)`·`.md#sec)`·`.md "title")`·앞 경로 유무를 다 흡수하고 외부 URL 오탐(오차단)을 막는다.
# `(?:^|/)` 로 segment 경계를 요구해 `mydecisions/` 류 비-경계 매치를 배제. 매핑: decisions→ADR,
# tickets/<state>→ticket, ideas/<state>→idea (tickets/ideas 는 상태 디렉토리 필수 — README·_template 제외).
# title 은 CommonMark 3형 모두 흡수 — `"…"`·`'…'`·`(…)`.
_MD_LINK_TARGET_RE = re.compile(
    r"\]\(\s*<?([^)>\s]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")

# 상태 조각은 **상수에서 유도**한다 — 리터럴로 적으면 상태 디렉토리가 늘어날 때 그 상태의 링크가
# 조용히 lint 밖으로 빠진다(dangling·슬러그 권고 둘 다 미검사). `re.escape` 는 상태명에 정규식
# 메타문자가 섞여도 문자 그대로 매칭시킨다.
_TICKET_STATUS_PATTERN = "|".join(re.escape(status) for status in ticket_rules.STATUS_DIRS)
_IDEA_STATUS_PATTERN = "|".join(re.escape(status) for status in ticket_rules.IDEA_STATUS_DIRS)
_STRUCT_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    r"decisions/([^/]+\.md)"
    rf"|tickets/(?:{_TICKET_STATUS_PATTERN})/([^/]+\.md)"
    rf"|ideas/(?:{_IDEA_STATUS_PATTERN})/([^/]+\.md)"
    r")$")

# 숫자선두 자유어휘 wikilink — `[[NNNN-slug]]`·`[[NNNN]]`·alias `[[NNNN-slug|표시명]]`
# (ADR/idea 를 ID 아닌 형으로 적은 것). slug 부는 `[^\]|]+`(비-ASCII 포함) — `_slugify` 가 한글
# slug 를 허용하므로 `[[0001-한글아이디어]]` 도 포착.
# alias `|표시명` 은 `_WIKILINK_RE` 동작과 동일하게 흡수(codex suggestion).
_NUM_LEAD_WIKILINK_RE = re.compile(r"\[\[(\d+)(?:-[^\]|]+)?(?:\|[^\]]+)?\]\]")


def lint_unstable_refs(home) -> list[tuple[str, str, str]]:
    """파일명/슬러그-의존 구조 참조를 포착한다 (kind=`unstable-ref`/`unstable-ref-advice`).

    두 형태를 본다 (둘 다 *구조화 디렉토리*를 가리킬 때만):
      - markdown 경로 링크 `](…/decisions/<slug>.md)`·`](…/tickets/<state>/<slug>.md)`·
        `](…/ideas/<state>/<slug>.md)` → 대상 파일이 실재 안 하면 dangling(차단), 실재하면 권고.
        (명시적 구조 경로라 의도가 분명 → 차단 가능.)
      - 숫자선두 wikilink `[[NNNN-slug]]`·`[[NNNN]]` → 번호가 ADR/idea 로 **resolve 될 때만**
        canonical `[[ADR-NNNN]]`/`[[idea-NNNN]]` 권고. resolve 안 되면 자유어휘(`[[2026-roadmap]]`
        등)와 구분 불가 → **불검사**(차단 안 함 · 오탐 0).

    kind 분류:
      - `unstable-ref`        = markdown 경로 링크가 실재 안 함 (환각) — `lint --gate` 차단.
      - `unstable-ref-advice` = 실재 파일을 슬러그로 가리킴 / 숫자선두 슬러그형 — 작동은 함,
        ID-wikilink 권고만(자문성·차단 안 함).

    자유어휘 일반(`[[some-memory]]`)·산문 언급은 건드리지 않는다 (오탐 0). detail 메시지에
    권장 교체형(`→ [[ADR-NNNN]]`)을 싣는다. (name, dangling) 으로 dedupe 하고 사용처를 모은다.
    """
    _ticket_ids, adr_nums, idea_nums = _resolve_wikilink_targets(home)
    # (name, dangling) → (detail, [source rel paths]) — name+상태가 dedupe 키.
    found: dict[tuple[str, bool], tuple[str, list[str]]] = {}

    def _record(name: str, dangling: bool, detail: str, src: str) -> None:
        # raw/ 스냅샷(sealed 면 immutable)은 슬러그-경로 *권고*(never-blocks)를 면제한다:
        # 봉인된 스냅샷의 링크는 고칠 수 없고(immutable) 역사적 인용이라 ID-wikilink 권고가 비실행적이다.
        # dangling(환각·차단)은 유지 — 깨진 구조 링크는 raw 에서도 surface 한다.
        if not dangling and "/raw/" in ("/" + src.replace("\\", "/")):
            return
        detail0, bucket = found.setdefault((name, dangling), (detail, []))
        del detail0  # 첫 detail 보존 (같은 name 의 메시지는 동일).
        if src not in bucket:
            bucket.append(src)

    for path in _collect_wikilink_files(home):
        try:
            text = fs.read_text_shared(path, encoding="utf-8")
        except OSError:
            continue
        src = pm_paths._rel_to_repo(home, path)
        # 코드 span/fence 안의 예시 링크·wikilink 는 실제 참조 아님 → 제거 후 스캔(오탐 0).
        text = markdown_text._strip_code(text)

        # (1) markdown 링크 — target 추출 → fragment/query 제거·외부 URL 제외 → 구조화 경로 분류.
        for raw_target in _MD_LINK_TARGET_RE.findall(text):
            # 외부 URL 제외(오탐·오차단 방지) — scheme `http://` 형 + protocol-relative `//host/…`
            # Windows `C:/` 는 `://`·`//`
            # 둘 다 아니라 영향 없음(이식성 고려해 urlsplit scheme-오인 회피).
            if "://" in raw_target or raw_target.startswith("//"):
                continue
            clean = raw_target.split("#", 1)[0].split("?", 1)[0]
            m = _STRUCT_PATH_RE.search(clean)
            if not m:
                continue
            dec_f, tic_f, idea_f = m.group(1), m.group(2), m.group(3)
            if dec_f:
                num = _leading_num(dec_f)
                if num is None:
                    continue  # 숫자선두 ADR 파일만 (template 등 비번호 제외).
                if not (pm_paths.decisions_dir(home) / dec_f).exists():
                    _record(dec_f, True,
                            f"decisions/{dec_f} 실재 안 함 (슬러그 링크 dangling) "
                            f"→ [[ADR-{num.zfill(4)}]] 로 교체", src)
                else:
                    _record(dec_f, False,
                            f"decisions/{dec_f} 슬러그 경로 링크 (환각 취약) "
                            f"→ [[ADR-{num.zfill(4)}]] 권고", src)
            elif tic_f:
                target = _find_ticket_file(home, tic_f)
                tid = ticket_rules._ticket_id_from_filename(tic_f)
                if target is None:
                    _record(tic_f, True,
                            f"tickets/.../{tic_f} 실재 안 함 (슬러그 링크 dangling)"
                            + (f" → [[{tid}]] 로 교체" if tid else ""), src)
                else:
                    _record(tic_f, False,
                            f"tickets/.../{tic_f} 슬러그 경로 링크 (환각 취약)"
                            + (f" → [[{tid}]] 권고" if tid else ""), src)
            elif idea_f:
                num = _leading_num(idea_f)
                if num is None:
                    continue
                target = _find_idea_file(home, idea_f)
                if target is None:
                    _record(idea_f, True,
                            f"ideas/.../{idea_f} 실재 안 함 (슬러그 링크 dangling) "
                            f"→ [[idea-{num.zfill(4)}]] 로 교체", src)
                else:
                    _record(idea_f, False,
                            f"ideas/.../{idea_f} 슬러그 경로 링크 (환각 취약) "
                            f"→ [[idea-{num.zfill(4)}]] 권고", src)

        # (2) 숫자선두 wikilink — [[NNNN-slug]]/[[NNNN]]. ADR/idea 번호로 *resolve 될 때만* 권고
        #     (canonical ID-wikilink 로 전환·자문성). resolve 안 되면 자유어휘(`[[2026-roadmap]]`·
        #     `[[1234-experiment]]` 등 숫자선두 메모리 링크)와 구분 불가 → **건드리지 않는다**
        #     ("자유어휘 불검사·오탐 0"; 미resolve hard-block 은 자유어휘를
        #     거짓 차단하는 회귀였음). 차단(dangling)은 명시적 구조 경로를 가진 markdown 링크만(위 (1)).
        for m in _NUM_LEAD_WIKILINK_RE.finditer(text):
            # alias(`|표시명`) 제거 후를 dedupe 키·메시지로 — 같은 대상이 alias 만 달라도 1 issue.
            name = m.group(0)[2:-2].split("|", 1)[0]   # 대괄호·alias 벗긴 raw (NNNN-slug).
            num = m.group(1).lstrip("0") or "0"
            if num in adr_nums:
                _record(name, False,
                        f"[[{name}]] 숫자선두 슬러그형 → canonical [[ADR-{num.zfill(4)}]] 권고", src)
            elif num in idea_nums:
                _record(name, False,
                        f"[[{name}]] 숫자선두 슬러그형 → canonical [[idea-{num.zfill(4)}]] 권고", src)
            # else: ADR/idea 로 resolve 안 됨 → 자유어휘로 간주·불검사(오탐 0).

    # dangling = hard block (resolve 실패 = 환각), 실재 슬러그 = 권고(자문성).
    # kind 로 구분: `unstable-ref`(차단) vs `unstable-ref-advice`(never-blocks · 권장형).
    issues: list[tuple[str, str, str]] = []
    for (name, dangling) in sorted(found, key=lambda k: (k[0], k[1])):
        detail, srcs = found[(name, dangling)]
        if not srcs:
            continue
        shown = ", ".join(srcs[:3]) + (f" (외 {len(srcs) - 3}개)" if len(srcs) > 3 else "")
        kind = "unstable-ref" if dangling else "unstable-ref-advice"
        issues.append((name, kind, f"{detail} · 사용처: {shown}"))
    return issues


def _find_ticket_file(home, filename: str) -> Path | None:
    """tickets/<state>/<filename> 중 실재하는 첫 경로 (상태 무관 — mv 로 이동했을 수 있음)."""
    for status in ticket_rules.STATUS_DIRS:
        p = pm_paths.tickets_dir(home) / status / filename
        if p.exists():
            return p
    return None


def _find_idea_file(home, filename: str) -> Path | None:
    """ideas/<state>/<filename> 중 실재하는 첫 경로 (상태 무관)."""
    for status in ticket_rules.IDEA_STATUS_DIRS:
        p = pm_paths.ideas_dir(home) / status / filename
        if p.exists():
            return p
    return None
