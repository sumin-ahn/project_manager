#!/usr/bin/env python3
"""Ticket board CLI — multi-session development coordination.

Atomic claim via POSIX rename(2). Tickets live as markdown files in
`.project_manager/wiki/tickets/{open,claimed,blocked,done}/`. Each command
updates `.project_manager/wiki/board.md` automatically.

`board.py idea …` manages pre-ADR ideas under
`.project_manager/wiki/ideas/{open,promoted,killed}/` with the same
atomic-rename + frontmatter-sync mechanics (see the `idea` subcommand group).

See `.project_manager/wiki/tickets/README.md` and
`.project_manager/wiki/ideas/README.md` for the workflows.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"
IDEAS_DIR = REPO / ".project_manager" / "wiki" / "ideas"
BOARD_FILE = REPO / ".project_manager" / "wiki" / "board.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
STATUS_FILE = REPO / ".project_manager" / "wiki" / "status.md"
TEMPLATE_FILE = TICKETS_DIR / "_template.md"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): prefix, session
AREAS_FILE = REPO / ".project_manager" / "areas.md"    # shared registry (committed, merge=union)
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"          # per-clone (git-ignored)
PM_STATE_TEMPLATE = REPO / ".project_manager" / "wiki" / "pm_state.template.md"  # tracked skeleton
LOCAL_DIR = REPO / ".project_manager" / ".local"            # per-clone scratch (git-ignored)
REGRESSION_FLAG = LOCAL_DIR / "regression.json"             # last regression result, keyed by HEAD
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")
# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")


# ── utilities ──────────────────────────────────────────────────────────

def local_config() -> dict[str, str]:
    """Per-clone local config (`.project_manager/local.conf`, git-ignored).

    Plain `KEY=value` lines; `#` comments and blank lines ignored. Missing → {}.
    Holds per-clone settings that must NOT be shared via git (prefix, session) —
    see the multi-PM model. Written by `pm-init`.
    """
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def session_name(override: str | None = None) -> str:
    if override:
        return override
    env = os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    sess = local_config().get("session")
    if sess:
        return sess
    return f"{socket.gethostname()}-{os.getpid()}"


def id_prefix(override: str | None = None) -> str | None:
    """Resolve ticket-ID namespace prefix (multi-PM areas).

    Order: override > local.conf `prefix=` > None. None → legacy `T-NNNN`
    (graceful / backward compatible). Non-None → `T-<PREFIX>-NNN` namespace.
    """
    if override:
        return override
    return local_config().get("prefix") or None


_AREAS_ROW_RE = re.compile(r"^\|\s*([A-Za-z][\w-]*)\s*\|")


def registered_prefixes() -> set[str]:
    """Prefixes registered in areas.md (shared registry). Empty set if no registry.

    The registry's *existence* is the multi-PM mode signal — when present,
    `board.py new` requires a registered prefix (see cmd_new guard).
    """
    if not AREAS_FILE.exists():
        return set()
    out: set[str] = set()
    for line in AREAS_FILE.read_text(encoding="utf-8").splitlines():
        m = _AREAS_ROW_RE.match(line.strip())
        if m and m.group(1).lower() != "prefix":
            out.add(m.group(1))
    return out


def areas_append(prefix: str, area: str, owner: str) -> None:
    """Register a prefix in areas.md (append-only; create with header if missing).

    Append-only + `merge=union` (.gitattributes) → concurrent registrations from
    different clones never conflict.
    """
    if not AREAS_FILE.exists():
        AREAS_FILE.write_text(
            "# Area Registry\n\n"
            "> prefix → area → owner. 멀티-PM ID 네임스페이스의 단일 진실. "
            "append-only (`merge=union`).\n"
            "> `board.py init` 이 등록. prefix 유일성 = race-free ID 의 전제.\n\n"
            "| prefix | area | owner |\n|---|---|---|\n",
            encoding="utf-8")
    with AREAS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"| {prefix} | {area} | {owner} |\n")


# ── 회귀 게이트 (R8) ──────────────────────────────────────────────────────
# 회귀 단위 ≡ push 단위 · green 인 것만 push. `regression run` 이 측정·기록(per-clone
# 로컬 플래그), pre-push 훅이 `regression check` 로 HEAD green 을 검증. 비차단 pre-warm 은
# PM 이 `run_in_background` 로 `regression run` 을 돌리는 워크플로(하니스 background).

def _git_head() -> str:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _hooks_dir() -> Path | None:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-path", "hooks"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    d = Path(r.stdout.strip())
    return d if d.is_absolute() else REPO / d


def install_pre_push_hook() -> bool:
    """Install the R8 pre-push regression gate. Idempotent. False if not a git repo."""
    hooks = _hooks_dir()
    if hooks is None:
        return False
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        "# pm pre-push gate (R8) — green 회귀만 push. board.py init 이 설치.\n"
        "python3 .project_manager/tools/board.py regression check || \\\n"
        "  python3 .project_manager/tools/board.py regression run\n")
    hook.chmod(0o755)
    return True


def _test_cmd(override: str | None) -> str:
    return override or local_config().get("test_cmd") or "pytest -q"


def cmd_regression(args: argparse.Namespace) -> int:
    """run = 측정+기록(HEAD 키), check = HEAD 가 green 인지 (pre-push 훅이 호출)."""
    if args.action == "run":
        cmd = _test_cmd(args.cmd)
        print(f"regression: $ {cmd}")
        rc = subprocess.run(cmd, shell=True, cwd=str(REPO)).returncode
        status = "pass" if rc in (0, 5) else "fail"  # pytest rc5 = no tests collected
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REGRESSION_FLAG.write_text(json.dumps(
            {"head": _git_head(), "status": status, "rc": rc, "ts": now_utc()}),
            encoding="utf-8")
        print(f"regression: {status} (rc={rc}) @ {_git_head()[:8] or '?'}")
        return 0 if status == "pass" else 1
    # action == "check" — pre-push 게이트
    if not REGRESSION_FLAG.exists():
        print("regression: 기록 없음 — `board.py regression run` 필요 (push 차단).",
              file=sys.stderr)
        return 1
    data = json.loads(REGRESSION_FLAG.read_text(encoding="utf-8"))
    head = _git_head()
    if data.get("head") != head:
        print(f"regression: stale (기록 {str(data.get('head'))[:8]} ≠ HEAD {head[:8]}) "
              "— 재실행 필요.", file=sys.stderr)
        return 1
    if data.get("status") != "pass":
        print(f"regression: RED @ {head[:8]} — push 차단.", file=sys.stderr)
        return 1
    print(f"regression: green @ {head[:8]} ✓")
    return 0


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


def _next_id(prefix: str | None = None) -> str:
    """Next ticket ID. Namespaced per prefix so concurrent areas never collide.

    prefix=None → legacy `T-NNNN` (4-digit). prefix="PAY" → `T-PAY-NNN` (3-digit),
    counted independently (scans only `T-PAY-*`). The legacy regex `T-(\\d+)-`
    never matches a prefixed file, so the two namespaces stay disjoint.
    """
    if prefix:
        n = next_numeric_id(TICKETS_DIR, STATUS_DIRS,
                            f"T-{prefix}-*.md", rf"T-{re.escape(prefix)}-(\d+)-")
        return f"T-{prefix}-{n:03d}"
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

    # 1. log/current.md must carry an entry for this ticket.
    if not args.allow_missing_log:
        log_text = LOG_FILE.read_text() if LOG_FILE.exists() else ""
        if not id_re.search(log_text):
            problems.append(
                f"no log/current.md entry mentions {tid} — append one to "
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


INIT_GUIDE = """\
─ pm-init 완료 — 이 clone 의 멀티-PM 등록 끝 ─
  3계층: 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf · git-ignored)
  규칙: 내구 진실은 공유 채널에만 · pm_state 는 버려도 되는 로컬 · 공유 파일 직접 난편집 금지
  ID:   네 ticket 은 `board.py new` 로 T-{prefix}-NNN 발행 (네임스페이스라 영역 간 ID 충돌 없음)
"""


def cmd_init(args: argparse.Namespace) -> int:
    """멀티-PM clone 등록 (clone 당 1회): prefix 레지스트리 + local.conf + pm_state."""
    prefix = args.prefix
    if prefix in registered_prefixes():
        print(f"prefix {prefix!r} 이미 등록됨 (areas.md) — local.conf 만 갱신.")
    else:
        if not args.area:
            print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
            return 1
        owner = args.owner or session_name()
        areas_append(prefix, args.area, owner)
        print(f"✓ areas.md 등록: {prefix} | {args.area} | {owner}")
    sess = args.session or f"{prefix.lower()}-pm"
    LOCAL_CONF.write_text(
        "# per-clone 설정 (git-ignored). pm-init 생성. clone 마다 다름.\n"
        f"prefix={prefix}\nsession={sess}\n", encoding="utf-8")
    print(f"✓ local.conf: prefix={prefix} · session={sess}")
    if not PM_STATE_FILE.exists() and PM_STATE_TEMPLATE.exists():
        PM_STATE_FILE.write_text(PM_STATE_TEMPLATE.read_text(encoding="utf-8"),
                                 encoding="utf-8")
        print(f"✓ pm_state.md 생성 ({_rel_to_repo(PM_STATE_TEMPLATE)} 에서)")
    if install_pre_push_hook():
        print("✓ pre-push 회귀 게이트 훅 설치 (green 회귀만 push)")
    print(INIT_GUIDE.format(prefix=prefix))
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    prefix = id_prefix(getattr(args, "prefix", None))
    if AREAS_FILE.exists():  # multi-PM mode (registry exists) → registered prefix 필수
        if not prefix:
            print("멀티-PM 모드(areas.md 존재) — prefix 필요. 먼저 "
                  "`board.py init --prefix <PFX> --area <name>`.", file=sys.stderr)
            return 1
        if prefix not in registered_prefixes():
            print(f"prefix {prefix!r} 미등록 (areas.md). `board.py init` 로 등록하거나 "
                  "등록된 prefix 사용.", file=sys.stderr)
            return 1
    tid = _next_id(prefix)
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


# status.md 부트스트랩 컨텍스트 드리프트 가드 임계값 (warn-only — 차단 아님).
# 헤더 = 스칼라 앵커, 활성 매트릭스 = 진행 중만. 초과 시 정리 신호.
STATUS_HEADER_MAX_CHARS = 280
STATUS_DONE_ROW_WARN = 30

# status.md 의 "전체 테스트:" 헤더 라인 (ticket_finish 가 편집하는 스칼라 앵커).
_STATUS_HEADER_RE = re.compile(r"^\*\*전체 테스트:.*$", re.MULTILINE)
# 모듈 매트릭스 행 중 상태 셀이 ✅ 인 행 (범례 "- ✅ ..." 는 `|` 시작 아니라 제외).
_STATUS_DONE_ROW_RE = re.compile(r"^\|.*\| ✅ \|", re.MULTILINE)


def lint_status() -> list[tuple[str, str, str]]:
    """status.md 의 부트스트랩 컨텍스트 비대화를 경고한다 (warn-only).

    Checks:
      - status-header-bloat: '전체 테스트' 헤더 라인이 너무 김 — incident/wave narrative 가
        스칼라 앵커 라인에 섞인 신호. 서술은 log/current.md entry 로.
      - status-done-accum:   활성 매트릭스에 ✅ 완성 행이 누적 — status_done.md 로 archive 권고.

    status.md 없으면 빈 리스트. (board.py refresh/lint 끝에서 호출.)
    """
    issues: list[tuple[str, str, str]] = []
    if not STATUS_FILE.exists():
        return issues
    text = STATUS_FILE.read_text()

    header = _STATUS_HEADER_RE.search(text)
    if header and len(header.group(0)) > STATUS_HEADER_MAX_CHARS:
        issues.append((
            "status.md", "status-header-bloat",
            f"'전체 테스트' 헤더 {len(header.group(0))}자 > {STATUS_HEADER_MAX_CHARS} — "
            f"incident/wave narrative 는 log/current.md 로 (헤더는 스칼라 앵커)"))

    done_rows = len(_STATUS_DONE_ROW_RE.findall(text))
    if done_rows > STATUS_DONE_ROW_WARN:
        issues.append((
            "status.md", "status-done-accum",
            f"활성 매트릭스 ✅ 완성 행 {done_rows}개 > {STATUS_DONE_ROW_WARN} — "
            f"status_done.md 로 archive 권고"))

    return issues


def lint_tickets() -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement + status.md context drift guard."""
    return lint_dependencies() + lint_bodies() + lint_ideas() + lint_status()


# ── board.md regeneration ──────────────────────────────────────────────

def refresh_board() -> None:
    """Regenerate .project_manager/wiki/board.md."""
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
        "> 자동 생성 파생물 (git-untracked) — `board.py` 변경 명령마다 로컬 갱신 · `board.py refresh` 로 재생성. 단일 진실은 `tickets/`, 라이브 상태는 `board.py list`. 수동 편집 금지.",
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
                   help="bypass the log/current.md entry check")
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
    p.add_argument("--prefix", help="ID namespace prefix (default: local.conf "
                   "prefix / none → legacy T-NNNN)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("init", help="멀티-PM clone 등록 (prefix·local.conf·pm_state, clone 당 1회)")
    p.add_argument("--prefix", required=True, help="이 clone 의 ID 네임스페이스 (예: PAY)")
    p.add_argument("--area", help="영역 설명 (새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="소유자 (기본: session 이름)")
    p.add_argument("--session", help="세션 이름 (기본: <prefix>-pm)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("regression",
                       help="회귀 게이트 (run=측정·기록 / check=HEAD green 검증·pre-push 훅용)")
    p.add_argument("action", choices=["run", "check"])
    p.add_argument("--cmd", help="테스트 명령 (기본: local.conf test_cmd / pytest -q)")
    p.set_defaults(fn=cmd_regression)

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
