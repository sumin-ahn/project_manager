#!/usr/bin/env python3
"""Ticket board CLI — multi-session development coordination.

Atomic claim via POSIX rename(2). Tickets live as markdown files in
`project_wiki/tickets/{open,claimed,blocked,done}/`. Each command updates
`project_wiki/board.md` automatically.

`board.py idea …` manages pre-ADR ideas under
`project_wiki/ideas/{open,promoted,killed}/` with the same atomic-rename +
frontmatter-sync mechanics (see the `idea` subcommand group).

See `project_wiki/tickets/README.md` and `project_wiki/ideas/README.md` for
the workflows.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
TICKETS_DIR = REPO / "project_wiki" / "tickets"
IDEAS_DIR = REPO / "project_wiki" / "ideas"
BOARD_FILE = REPO / "project_wiki" / "board.md"
LOG_FILE = REPO / "project_wiki" / "log.md"
STATUS_FILE = REPO / "project_wiki" / "status.md"
TEMPLATE_FILE = TICKETS_DIR / "_template.md"
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")
# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")


# ── utilities ──────────────────────────────────────────────────────────

def session_name(override: str | None = None) -> str:
    if override:
        return override
    env = os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    return f"{socket.gethostname()}-{os.getpid()}"


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def find_item(base_dir: Path, statuses: tuple[str, ...], item_id: str,
              kind: str = "item") -> tuple[str, Path]:
    """Return (status_dir, path) for an `{item_id}-*.md` file under base_dir.

    Generic over tickets and ideas — the lookup is identical, only the
    directory layout and ID shape differ. Raises FileNotFoundError if missing.
    """
    for status in statuses:
        for p in (base_dir / status).glob(f"{item_id}-*.md"):
            return status, p
    raise FileNotFoundError(f"{kind} not found: {item_id}")


def find_ticket(tid: str) -> tuple[str, Path]:
    """Return (status_dir, path). Raises FileNotFoundError if missing."""
    return find_item(TICKETS_DIR, STATUS_DIRS, tid, "ticket")


def find_idea(iid: str) -> tuple[str, Path]:
    """Return (status_dir, path) for idea `iid`. Raises FileNotFoundError."""
    return find_item(IDEAS_DIR, IDEA_STATUS_DIRS, iid, "idea")


def load_ticket(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string)."""
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {path}")
    # Split on the FIRST closing '---' after the opener
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise ValueError(f"unterminated frontmatter: {path}")
    fm_text = after_open[:end]
    body = after_open[end + 5:]
    fm = yaml.safe_load(fm_text) or {}
    return fm, body


def dump_ticket(path: Path, fm: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm_text}\n---\n{body}")


def move_item(base_dir: Path, src: Path, dst_status: str) -> Path:
    """Atomic mv of an item file into a sibling status directory.

    The POSIX rename(2) is the lock — a lost race surfaces as FileNotFoundError.
    Generic over tickets and ideas.
    """
    dst = base_dir / dst_status / src.name
    os.rename(src, dst)
    return dst


def move_ticket(src: Path, dst_status: str) -> Path:
    """Atomic mv into a ticket status directory."""
    return move_item(TICKETS_DIR, src, dst_status)


def move_idea(src: Path, dst_status: str) -> Path:
    """Atomic mv into an idea status directory."""
    return move_item(IDEAS_DIR, src, dst_status)


def next_numeric_id(base_dir: Path, statuses: tuple[str, ...],
                    glob_pat: str, id_re: str) -> int:
    """Return the next free integer ID across every status directory.

    `glob_pat` selects candidate files; `id_re` extracts the integer from a
    filename (its first group). Generic over tickets (`T-NNNN`) and ideas
    (`NNNN`).
    """
    max_id = 0
    pattern = re.compile(id_re)
    for d in statuses:
        for p in (base_dir / d).glob(glob_pat):
            m = pattern.match(p.name)
            if m:
                max_id = max(max_id, int(m.group(1)))
    return max_id + 1


def _next_id() -> str:
    n = next_numeric_id(TICKETS_DIR, STATUS_DIRS, "T-*.md", r"T-(\d+)-")
    return f"T-{n:04d}"


def _next_idea_id() -> str:
    n = next_numeric_id(IDEAS_DIR, IDEA_STATUS_DIRS, "[0-9]*.md", r"(\d+)-")
    return f"{n:04d}"


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9가-힣-]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "ticket"


# ── commands ───────────────────────────────────────────────────────────

def cmd_claim(args: argparse.Namespace) -> int:
    sess = session_name(args.session)
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot claim {args.id}: currently in {status}/", file=sys.stderr)
        return 1

    fm, body = load_ticket(path)
    # Check dependencies
    for dep in fm.get("depends_on") or []:
        try:
            dep_status, _ = find_ticket(dep)
        except FileNotFoundError:
            print(f"dependency {dep} not found", file=sys.stderr)
            return 1
        if dep_status != "done":
            print(f"dependency {dep} is {dep_status}/, not done", file=sys.stderr)
            return 1

    # Atomic rename is the lock
    try:
        new_path = move_ticket(path, "claimed")
    except FileNotFoundError:
        print(f"claim race lost on {args.id}", file=sys.stderr)
        return 1

    fm["status"] = "claimed"
    fm["claimed_by"] = sess
    fm["claimed_at"] = now_utc()
    dump_ticket(new_path, fm, body)
    print(f"claimed {args.id} as {sess}")
    refresh_board()
    return 0


def _complete_gate(tid: str, args: argparse.Namespace) -> list[str]:
    """Verify completion housekeeping before a ticket may move to done/.

    Returns a list of *blocking* problems (empty = gate passes). Non-blocking
    concerns are printed to stderr as warnings from here.

    The regression check trusts the caller's `--tests-pass` assertion rather
    than re-running the (slow) suite — see T-0020.
    """
    problems: list[str] = []
    id_re = re.compile(rf"\b{re.escape(tid)}\b")

    # 1. log.md must carry an entry for this ticket.
    if not args.allow_missing_log:
        log_text = LOG_FILE.read_text() if LOG_FILE.exists() else ""
        if not id_re.search(log_text):
            problems.append(
                f"no log.md entry mentions {tid} — append one to "
                f"{_rel_to_repo(LOG_FILE)} (or pass --allow-missing-log)")

    # 2. regression must be confirmed by the implementing session.
    if not (args.tests_pass or args.allow_untested):
        problems.append(
            "regression not confirmed — run `pytest tests/ -q`, then pass "
            "--tests-pass (or --allow-untested for a regression-irrelevant "
            "ticket)")

    # 3. status.md staleness — warning only, never blocks.
    status_text = STATUS_FILE.read_text() if STATUS_FILE.exists() else ""
    if not id_re.search(status_text):
        print(f"  ⚠️  {_rel_to_repo(STATUS_FILE)} does not mention {tid} — "
              f"confirm the affected module row is updated", file=sys.stderr)

    return problems


def cmd_complete(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "claimed":
        print(f"cannot complete {args.id}: in {status}/, must be claimed",
              file=sys.stderr)
        return 1

    # Sync gate — refuse to mark done until housekeeping is verified.
    problems = _complete_gate(args.id, args)
    if problems:
        print(f"cannot complete {args.id}: sync gate failed —", file=sys.stderr)
        for msg in problems:
            print(f"  ✗ {msg}", file=sys.stderr)
        return 1

    fm, body = load_ticket(path)
    new_path = move_ticket(path, "done")
    fm["status"] = "done"
    fm["completed_at"] = now_utc()
    dump_ticket(new_path, fm, body)
    print(f"completed {args.id}")
    refresh_board()
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status not in ("open", "claimed"):
        print(f"cannot block from {status}/", file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "blocked")
    fm["status"] = "blocked"
    note = f"\n## Blocked\n{args.reason} — {datetime.date.today().isoformat()}\n"
    dump_ticket(new_path, fm, body + note)
    print(f"blocked {args.id}: {args.reason}")
    refresh_board()
    return 0


def cmd_unclaim(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "claimed":
        print(f"cannot unclaim {args.id}: in {status}/", file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "open")
    fm["status"] = "open"
    fm["claimed_by"] = None
    fm["claimed_at"] = None
    dump_ticket(new_path, fm, body)
    print(f"unclaimed {args.id}")
    refresh_board()
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "blocked":
        print(f"cannot unblock {args.id}: in {status}/, must be blocked",
              file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "open")
    fm["status"] = "open"
    dump_ticket(new_path, fm, body)
    print(f"unblocked {args.id}")
    refresh_board()
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    tid = _next_id()
    slug = _slugify(args.title)
    filename = f"{tid}-{slug}.md"

    template = TEMPLATE_FILE.read_text()
    tmpl_fm, tmpl_body = load_ticket(TEMPLATE_FILE)
    # Replace placeholder tokens in body
    body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)

    fm: dict[str, Any] = dict(tmpl_fm)
    fm["id"] = tid
    fm["title"] = args.title
    fm["status"] = "open"
    fm["created"] = datetime.date.today().isoformat()
    fm["claimed_by"] = None
    fm["claimed_at"] = None
    fm["completed_at"] = None
    fm["touches"] = (args.touches.split(",") if args.touches else [])
    fm["depends_on"] = (args.depends.split(",") if args.depends else [])
    fm["blocks"] = []
    fm["tags"] = (args.tag.split(",") if args.tag else [])
    fm["estimate"] = args.estimate

    path = TICKETS_DIR / "open" / filename
    dump_ticket(path, fm, body)
    print(f"created {tid} ({_rel_to_repo(path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint` "
          "(placeholders left in the body fail lint)")
    refresh_board()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows: list[tuple[str, dict]] = []
    for status in STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in (fm.get("tags") or []):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no tickets)")
        return 0
    for status, fm in rows:
        tags = ",".join(fm.get("tags") or [])
        claimed = fm.get("claimed_by") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:7s}] {fm['id']}  {title:60s}  {claimed:18s}  {tags}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"-- {args.id} ({status}/) --\n")
    print(path.read_text())
    return 0


# ── idea commands ──────────────────────────────────────────────────────
#
# Ideas are pre-ADR candidates living under ideas/{open,promoted,killed}/.
# They reuse the ticket frontmatter/body helpers (load_ticket / dump_ticket)
# and the generic find_item / move_item / next_numeric_id helpers — only the
# lifecycle differs (no claim/complete; just open → promoted|killed).

# Body skeleton for `idea new`. Mirrors the 권장 섹션 list in ideas/README.md.
_IDEA_BODY_TEMPLATE = """# Idea-{iid} — {title}

## 한 줄 요약

<무엇을 / 왜 끌리는가 1~2 문장>

## 동기

- <왜 이 idea 에 끌리는가>

## 가능한 구현 형태 (high-level)

- <high-level 구현 방향 — 어느 모듈/계층에, 어떤 형태로>

## 위험 / 고민거리

- <검토할 위험>

## 열린 질문

- [ ] <답이 필요한 질문>

## 다음 행동

- promote 기준 / kill 기준 / 어떻게 익힐지

## 관련 링크

- [[xxxxx]]
"""


def cmd_idea_list(args: argparse.Namespace) -> int:
    rows: list[tuple[str, dict]] = []
    for status in IDEA_STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((IDEAS_DIR / status).glob("[0-9]*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in (fm.get("tags") or []):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no ideas)")
        return 0
    for status, fm in rows:
        tags = ",".join(fm.get("tags") or [])
        iid = fm.get("id") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:8s}] {iid:6s} {title:60s}  {tags}")
    return 0


def cmd_idea_new(args: argparse.Namespace) -> int:
    iid = _next_idea_id()
    slug = _slugify(args.title)
    filename = f"{iid}-{slug}.md"

    today = datetime.date.today().isoformat()
    fm: dict[str, Any] = {
        "id": iid,
        "title": args.title,
        "created": today,
        "updated": today,
        "type": "idea",
        "status": "open",
        "tags": (args.tag.split(",") if args.tag else []),
    }
    body = "\n" + _IDEA_BODY_TEMPLATE.format(iid=iid, title=args.title)

    path = IDEAS_DIR / "open" / filename
    dump_ticket(path, fm, body)
    print(f"created idea {iid} ({_rel_to_repo(path)})")
    print("  → fill in 한 줄 요약 / 동기 / 위험 / 다음 행동")
    return 0


# Maps an idea's destination status to the imperative verb used in messages.
_IDEA_TRANSITION_VERB = {"promoted": "promote", "killed": "kill"}


def _transition_idea(iid: str, dst_status: str) -> int:
    """Atomic mv open/ → dst_status/ + frontmatter status sync.

    Shared by `idea promote` and `idea kill` — the only transitions ideas
    support. Both move out of `open/` only.
    """
    verb = _IDEA_TRANSITION_VERB[dst_status]
    try:
        status, path = find_idea(iid)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot {verb} idea {iid}: currently in {status}/",
              file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_idea(path, dst_status)
    fm["status"] = dst_status
    fm["updated"] = datetime.date.today().isoformat()
    dump_ticket(new_path, fm, body)
    print(f"{dst_status} idea {iid} ({_rel_to_repo(new_path)})")
    return 0


def cmd_idea_promote(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "promoted")


def cmd_idea_kill(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "killed")


def cmd_refresh(_args: argparse.Namespace) -> int:
    refresh_board()
    print(f"board refreshed: {_rel_to_repo(BOARD_FILE)}")
    issues = lint_tickets()
    if issues:
        print(f"⚠️  {len(issues)} lint issue(s) — run `board.py lint` for detail",
              file=sys.stderr)
    return 0


def cmd_lint(_args: argparse.Namespace) -> int:
    issues = lint_tickets()
    if not issues:
        print("✓ no lint issues")
        return 0
    print(f"⚠️  {len(issues)} lint issue(s):")
    for ticket_id, kind, detail in issues:
        print(f"  [{kind}] {ticket_id}: {detail}")
    return 1


def _rel_to_repo(path: Path) -> str:
    """Best-effort pretty path. Falls back to absolute when path is outside REPO
    (e.g. in unit tests using tmp_path)."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


# ── lint ───────────────────────────────────────────────────────────────

def _all_tickets() -> list[tuple[str, dict]]:
    """[(status, frontmatter), ...] for every ticket regardless of dir."""
    out: list[tuple[str, dict]] = []
    for status in STATUS_DIRS:
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            out.append((status, fm))
    return out


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


def lint_dependencies() -> list[tuple[str, str, str]]:
    """Return list of (ticket_id, issue_kind, detail).

    Checks:
      - unknown:        depends_on / blocks references a non-existent ticket
      - self-reference: ticket lists its own ID in depends_on or blocks
      - asymmetric:     A.blocks contains B but B.depends_on does not contain A
      - cycle:          depends_on graph contains a circular path
    """
    tickets = {fm["id"]: (status, fm) for status, fm in _all_tickets()}
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


# Unfilled `_template.md` text — its presence means the ticket is still a stub.
# The `## 메모` placeholder is intentionally NOT listed: that section is a work
# journal filled at completion time, so an empty 메모 is normal for a complete,
# claimable ticket and must not count as "thin".
_PLACEHOLDERS: tuple[str, ...] = (
    "무엇을 만들 / 바꿀 / 검증할지",
    "핵심 산출물 (파일, 동작)",
    "[[xxxxx]]",
    "<제목>",
)
_REQUIRED_SECTIONS: tuple[str, ...] = ("## 목표", "## 완료 조건", "## 참고")

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _strip_code(text: str) -> str:
    """Drop fenced blocks and inline code spans.

    A ticket *about* the template (like T-0020) legitimately quotes placeholder
    strings inside backticks; only unquoted prose counts as an unfilled stub.
    """
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))


def lint_bodies() -> list[tuple[str, str, str]]:
    """Lint open/claimed ticket bodies for self-containment.

    Checks:
      - placeholder: unfilled `_template.md` text still present as prose
      - thin:        a standard section (목표 / 완료 조건 / 참고) is missing

    done/blocked tickets are skipped — only live, claimable work is gated.
    """
    issues: list[tuple[str, str, str]] = []
    for status in ("open", "claimed"):
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, body = load_ticket(p)
            tid = fm.get("id") or p.name
            prose = _strip_code(body)
            for placeholder in _PLACEHOLDERS:
                if placeholder in prose:
                    issues.append((tid, "placeholder",
                                   f"unfilled template text: {placeholder!r}"))
            for section in _REQUIRED_SECTIONS:
                if section not in body:
                    issues.append((tid, "thin",
                                   f"missing standard section: {section}"))
    return issues


def lint_ideas() -> list[tuple[str, str, str]]:
    """Lint ideas for frontmatter `status` ↔ directory agreement.

    The directory is the source of truth; a mismatched frontmatter `status`
    means a manual `mv` bypassed board.py (drift — see ideas/README.md).
    """
    issues: list[tuple[str, str, str]] = []
    for status in IDEA_STATUS_DIRS:
        for p in sorted((IDEAS_DIR / status).glob("[0-9]*.md")):
            fm, _ = load_ticket(p)
            iid = fm.get("id") or p.name
            fm_status = fm.get("status")
            if fm_status != status:
                issues.append((iid, "idea-status",
                               f"in {status}/ but frontmatter status={fm_status!r}"))
    return issues


def lint_tickets() -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement."""
    return lint_dependencies() + lint_bodies() + lint_ideas()


# ── board.md regeneration ──────────────────────────────────────────────

def refresh_board() -> None:
    """Regenerate project_wiki/board.md."""
    by_status: dict[str, list[dict]] = {s: [] for s in STATUS_DIRS}
    for status in STATUS_DIRS:
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            by_status[status].append(fm)

    lines: list[str] = [
        "---",
        "title: Ticket Board",
        "type: dashboard",
        f"updated: {now_utc()}",
        "---",
        "",
        "# Ticket Board",
        "",
        "> 자동 생성 — `tools/board.py` 의 모든 변경 명령 끝에 갱신. 수동 편집 금지.",
        "> 작업 흐름: [`tickets/README.md`](tickets/README.md).",
        "",
    ]
    totals = " · ".join(f"{s}={len(by_status[s])}" for s in STATUS_DIRS)
    lines.append(f"**현황:** {totals}")
    lines.append("")

    emoji = {"open": "🟢", "claimed": "🟡", "blocked": "🔴", "done": "✅"}

    for status in STATUS_DIRS:
        items = by_status[status]
        # Skip the done section header when empty so the board stays focused on live work
        if status == "done" and not items:
            continue
        lines.append(f"## {emoji[status]} {status.upper()} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("*없음*")
            lines.append("")
            continue
        if status == "open":
            lines.append("| ID | Title | depends_on | touches | tags |")
            lines.append("|---|---|---|---|---|")
            for fm in items:
                dep = ", ".join(fm.get("depends_on") or []) or "—"
                tch = ", ".join((fm.get("touches") or [])[:3]) or "—"
                tag = ", ".join(fm.get("tags") or [])
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | {dep} | {tch} | {tag} |"
                )
        elif status == "claimed":
            lines.append("| ID | Title | Claimed by | Since (UTC) |")
            lines.append("|---|---|---|---|")
            for fm in items:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"`{fm.get('claimed_by','')}` | {(fm.get('claimed_at') or '')[:19]} |"
                )
        elif status == "blocked":
            lines.append("| ID | Title | (reason at the bottom of the file) |")
            lines.append("|---|---|---|")
            for fm in items:
                lines.append(f"| {fm['id']} | {fm.get('title','')} | — |")
        elif status == "done":
            # Show most-recent 10
            lines.append("| ID | Title | Completed (UTC) |")
            lines.append("|---|---|---|")
            recent = sorted(items, key=lambda f: f.get("completed_at") or "",
                            reverse=True)[:10]
            for fm in recent:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"{(fm.get('completed_at') or '')[:19]} |"
                )
        lines.append("")

    BOARD_FILE.write_text("\n".join(lines))


# ── argparse ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="board.py",
                                     description="Multi-session ticket board.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list tickets")
    p.add_argument("--status", choices=STATUS_DIRS)
    p.add_argument("--tag")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id")
    p.add_argument("--session", help="session name (default $CLAUDE_SESSION_NAME or hostname-pid)")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("complete", help="mv claimed → done (sync gate enforced)")
    p.add_argument("id")
    p.add_argument("--tests-pass", action="store_true",
                   help="assert the regression suite passes "
                        "(required unless --allow-untested)")
    p.add_argument("--allow-missing-log", action="store_true",
                   help="bypass the log.md entry check")
    p.add_argument("--allow-untested", action="store_true",
                   help="bypass the regression check "
                        "(regression-irrelevant ticket)")
    p.set_defaults(fn=cmd_complete)

    p = sub.add_parser("block", help="mv open|claimed → blocked")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("unclaim", help="mv claimed → open")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unclaim)

    p = sub.add_parser("unblock", help="mv blocked → open")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("new", help="create a new ticket")
    p.add_argument("title")
    p.add_argument("--touches", help="comma-separated file paths")
    p.add_argument("--depends", help="comma-separated ticket IDs")
    p.add_argument("--tag", help="comma-separated tags")
    p.add_argument("--estimate", choices=["small", "medium", "large"],
                   default="small")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("refresh", help="regenerate board.md")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("lint", help="check depends_on / blocks consistency")
    p.set_defaults(fn=cmd_lint)

    # idea subcommand group — pre-ADR candidates under ideas/{open,promoted,killed}/
    idea = sub.add_parser("idea", help="manage pre-ADR ideas")
    idea_sub = idea.add_subparsers(dest="idea_cmd", required=True)

    ip = idea_sub.add_parser("list", help="list ideas")
    ip.add_argument("--status", choices=IDEA_STATUS_DIRS)
    ip.add_argument("--tag")
    ip.set_defaults(fn=cmd_idea_list)

    ip = idea_sub.add_parser("new", help="create a new idea in open/")
    ip.add_argument("title")
    ip.add_argument("--tag", help="comma-separated tags")
    ip.set_defaults(fn=cmd_idea_new)

    ip = idea_sub.add_parser("promote", help="mv idea open → promoted")
    ip.add_argument("id")
    ip.set_defaults(fn=cmd_idea_promote)

    ip = idea_sub.add_parser("kill", help="mv idea open → killed")
    ip.add_argument("id")
    ip.set_defaults(fn=cmd_idea_kill)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
