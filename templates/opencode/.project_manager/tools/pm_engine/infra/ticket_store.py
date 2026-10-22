"""티켓 파일 조회·읽기 (층: infra).

canonical ID 정확 일치 조회(`find_ticket_exact`)와 frontmatter 읽기(`load_ticket`·`load_ticket_soft`).
문법은 `pm_engine.domain.ticket_rules` 가 소유한다.
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pm_engine.domain import ticket_rules
from pm_engine.infra import fs, pm_paths

import contextlib
import os
import re


def _ticket_search_dirs(home) -> list[tuple[str, Path]]:
    """티켓 조회 범위 — (status 라벨, 디렉토리) 순서쌍. STATUS_DIRS → draft 격리 디렉토리."""
    return [*((status, pm_paths.tickets_dir(home) / status) for status in ticket_rules.STATUS_DIRS),
            ("draft", pm_paths.drafts_dir(home))]


def find_ticket_exact(
    home, tid: str, *, search_dirs: Sequence[tuple[str, Path]] | None = None,
) -> tuple[str, Path] | None:
    """canonical ID 가 **정확히** `tid` 인 티켓의 (status, path) — 없으면 None (공용 seam).

    `{tid}-*.md` **prefix glob 의 첫 매칭**을 티켓-ID 조회로 쓰면 legacy ID(`T-NNNN`)가 같은
    번호로 시작하는 prefixed ID(`T-NNNN-001`)에 오인 매칭된다 — 어느 쪽이 잡힐지는 디렉토리
    순서가 정하고, 그 결과 회귀 강등 스코프·완료 게이트가 **무관한 티켓의
    frontmatter** 를 보게 된다(전부 조용한 오판). 그래서 후보 전체를 훑어 frontmatter canonical
    ID(`_canonical_ticket_id`)가 정확히 일치하는 것만 인정한다.

    이 판정을 쓰는 표면이 board 밖에도 있어(추가 리뷰어 티켓 조회·`ticket_finish` 티켓 로드)
    공용 seam 으로 둔다 — 사본을 만들면 그 사본이 다시 첫-매칭으로 흘러 half-fix 가 재발한다.
    `search_dirs` 를 주면 그 (status 라벨, 디렉토리) 순서로만 본다 — 다른 PM 홈의 보드를 읽는
    호출부(`additional_reviewer --pm-home`)가 자기 범위를 명시하는 자리다. 미지정이면 이 board 의
    STATUS_DIRS → drafts.
    """
    for status, directory in (search_dirs if search_dirs is not None
                              else _ticket_search_dirs(home)):
        for path in sorted(Path(directory).glob(f"{tid}-*.md")):
            if _canonical_ticket_id(path) == tid:
                return status, path
    return None


def load_ticket(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string)."""
    return ticket_rules._parse_ticket_text(fs.read_text_shared(path, encoding="utf-8"), path)


# 손상 frontmatter fail-soft — **디렉토리 순회** 전용 로더.
#
# 한 티켓의 YAML 이 깨지면(실측: 콜론을 인용 없이 쓴 스칼라 값) 그 파일을
# 읽는 순간 예외가 나고, 순회 소비자(`list`·`lint`·`refresh`)가 통째로 traceback 으로 죽는다 —
# 보드 전체가 한 파일 때문에 조회 불능이 되는 클래스다. 순회에서는 그 티켓만 건너뛰되 **조용히
# 넘기지 않는다**(경고 1줄 + 경로 + 사유). 지정 대상 mutation(claim/complete/block/…)은 그대로
# `load_ticket` 을 써 fail-loud 다 — 고치라고 지목받은 파일이 조용히 무시되면 안 된다.
# PyYAML 은 예외가 났을 때 읽는다(`ticket_rules._parse_ticket_text` 와 같은 이유).
def _load_ticket_soft_errors() -> tuple[type[Exception], ...]:
    import yaml

    return (OSError, UnicodeError, ValueError, yaml.YAMLError)


def load_ticket_soft(home, path: Path) -> tuple[dict[str, Any], str] | None:
    """순회용 `load_ticket` — 읽기/파싱 실패면 경고 1줄 뒤 None (그 티켓만 skip).

    frontmatter 가 dict 가 아닌 형상(스칼라·리스트)도 같은 축으로 접는다 — 뒤따르는 `fm.get(...)`
    이 AttributeError 로 터지면 fail-soft 가 반쪽이다.
    """
    try:
        fm, body = load_ticket(path)
    except _load_ticket_soft_errors() as exc:
        print(f"경고: 티켓을 읽지 못해 건너뜁니다 — {pm_paths._rel_to_repo(home, path)} "
              f"({type(exc).__name__}: {exc})\n"
              "  · frontmatter YAML 문법을 확인하세요 (콜론 포함 값은 인용 필요 — "
              'title: "a: b").', file=sys.stderr)
        return None
    if not isinstance(fm, dict):
        print(f"경고: 티켓 frontmatter 가 매핑이 아니라 건너뜁니다 — {pm_paths._rel_to_repo(home, path)} "
              f"({type(fm).__name__})", file=sys.stderr)
        return None
    return fm, body


def _canonical_ticket_id(path: Path) -> str | None:
    """티켓의 canonical ID — frontmatter `id:` 1차 진실·파일명 파싱은 폴백 (숫자-slug 모호성 해소).

    파일명 파서(`_ticket_id_from_filename`)는 legacy `T-NNNN` + **숫자로 끝나는 slug**
    (`T-0036-fix-123.md`)를 prefixed ID `T-0036-fix-123` 으로 오인한다(하이픈-마디 grammar 모호성·
    `dump_ticket`/`_next_id` 이 발행 시 확정해 쓰는 frontmatter `id:` 가
    권위 진실이므로 이를 우선하고, 부재/읽기 실패(비-UTF-8·frontmatter 깨짐) 시에만 파일명 파싱으로
    폴백한다(fail-soft). `_scan_prefix_tickets` 의 `fm.get("id") or …` 와 동형 semantics — 스캔측과
    rename-planning 측이 같은 canonical 해소를 쓰게 해 파일명↔content 불일치를 원천 차단한다.

    **frontmatter 가 매핑이 아닌 형상**(스칼라·리스트)도 같은 축으로 접는다(`load_ticket_soft`
    규약 — 손상 = 없는 것으로 보고 파일명 폴백). YAML 이 비어 있지 않은 스칼라면 파싱은 성공하고
    `fm.get` 이 AttributeError 로 터지는데, 이 함수는 정확-일치 조회(`find_ticket_exact`)의
    후보 순회 안에서 불리므로 그 예외가 **활성 리뷰 판정·강등 판정보다 먼저** 실행을 죽인다
    (손상 티켓은 skip 하고 FULL 로 남긴다는 규약이 깨진다).
    """
    try:
        fm, _ = load_ticket(path)
    except _load_ticket_soft_errors():  # 읽기/파싱 실패는 파일명 폴백(fail-soft·비-UTF-8 등).
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    fid = fm.get("id")
    if isinstance(fid, str) and fid:
        return fid
    return ticket_rules._ticket_id_from_filename(path.name)


def _all_tickets(home) -> list[tuple[str, dict]]:
    """[(status, frontmatter), ...] for every ticket regardless of dir.

    손상 티켓은 경고 1줄과 함께 빠진다(`load_ticket_soft`) — 한 파일이 lint 전체를 죽이지 않는다.
    """
    out: list[tuple[str, dict]] = []
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = load_ticket_soft(home, p)
            if loaded is None:
                continue
            out.append((status, loaded[0]))
    return out


def find_item(base_dir: Path, statuses: tuple[str, ...], item_id: str,
              kind: str = "item") -> tuple[str, Path]:
    """Return (status_dir, path) for an `{item_id}-*.md` file under base_dir.

    Generic over tickets and ideas — the lookup is identical, only the
    directory layout and ID shape differ. Raises FileNotFoundError if missing.

    **prefix glob 의 첫 매칭이다.** 티켓 조회는 이 함수를 직접 부르지 말고 `find_ticket`
    (정확 일치 우선) 또는 `find_ticket_exact`(정확 일치만)를 쓴다 — 티켓 ID 공간은 legacy
    `T-NNNN` 과 prefixed `T-NNNN-001` 이 공존해 첫 매칭이 남의 티켓을 답한다. idea ID 는
    `NNNN` 하나뿐이라(`_next_idea_id`) 그 충돌 공간이 없다.
    """
    for status in statuses:
        for p in (base_dir / status).glob(f"{item_id}-*.md"):
            return status, p
    raise FileNotFoundError(f"{kind} not found: {item_id}")


def find_ticket_for_mutation(home, tid: str) -> tuple[str, Path]:
    """상태-변경 명령 전용 조회 — canonical ID **정확 일치만** (없으면 FileNotFoundError).

    claim/complete/block/unclaim/unblock/promote 와 backfill 쓰기는 조회 결과를 **이동·기록**
    한다. 그 자리에서 옛 glob 첫-매칭 폴백(`find_ticket`)을 쓰면 없는 번호를 입력했을 때 같은
    번호로 시작하는 **다른 티켓**(legacy `T-NNNN` 부재 · prefixed `T-NNNN-001` 실재)이 이동한다 —
    경고 한 줄이 나가지만 파일은 이미 옮겨진 뒤다. 되돌리려면 사람이 손으로 상태를 복원해야
    하므로, 이 축은 경고가 아니라 **차단**이다(불일치 = 미존재).

    조회 범위는 `find_ticket_exact` 와 같다 — STATUS_DIRS(전 상태 디렉토리) →
    draft 격리 디렉토리(`drafts_dir()`·pseudo-status `"draft"`). draft 를 찾은 mutation 은
    각자의 status 검사가 즉시 거부한다(예: "cannot claim T-x: currently in draft/").

    안내는 "왜 못 찾았나"를 말한다 — 같은 번호로 시작하는 후보가 실재하면 그 canonical ID 를
    함께 보여, 오타 입력이 조용한 오이동 대신 정확한 재입력으로 이어지게 한다.
    """
    exact = find_ticket_exact(home, tid)
    if exact is not None:
        return exact
    near = _near_miss_ticket_ids(home, tid)
    hint = (f" — 같은 번호로 시작하는 티켓은 있으나 canonical ID 가 다르다: "
            f"{', '.join(near)} (정확한 ID 로 다시 호출하라)" if near else "")
    raise FileNotFoundError(f"ticket not found: {tid}{hint}")


def _near_miss_ticket_ids(home, tid: str) -> list[str]:
    """`{tid}-*.md` prefix 로 잡히지만 canonical ID 가 다른 티켓들의 ID (없으면 빈 목록).

    차단 안내 전용이다 — 옛 폴백이 *이동시켰을* 티켓을 이름으로 보여줘 "왜 못 찾았나"가
    미스터리로 남지 않게 한다. 조회 자체에는 관여하지 않는다(fail-soft: 읽기 실패는 무시).
    """
    near: list[str] = []
    with contextlib.suppress(OSError):
        for _status, directory in _ticket_search_dirs(home):
            for path in sorted(Path(directory).glob(f"{tid}-*.md")):
                canonical = _canonical_ticket_id(path)
                if canonical and canonical != tid and canonical not in near:
                    near.append(canonical)
    return near


def find_ticket(home, tid: str) -> tuple[str, Path]:
    """Return (status_dir, path). Raises FileNotFoundError if missing. **읽기 소비자 전용.**

    **canonical ID 정확 일치가 우선이다**(`find_ticket_exact`) — 그 뒤에야 옛 glob 첫 매칭으로
    떨어진다. 순서가 이렇게 정해진 이유는 `T-NNNN` 과 `T-NNNN-001` 이 공존할 때 첫 매칭이
    디렉토리 순서에 좌우되기 때문이다(호출부는 어느 티켓을 받았는지 모른 채 그 frontmatter 를
    쓴다). 폴백을 남겨 두는 건 frontmatter `id:` 가 없고 파일명 파서가 모호한 legacy 티켓
    (`T-NNNN-<slug>-123.md`)까지 못 찾게 되면 조회 자체가 회귀하기 때문이다 — 정확 일치 후보가
    있으면 언제나 그쪽이고, 없을 때만 종전 동작이다.

    **상태를 바꾸는 명령은 이 함수를 쓰지 않는다** — 폴백이 답한 남의 티켓을 그대로 옮기기
    때문이다(경고를 봤을 땐 이미 이동한 뒤다). 그쪽은 `find_ticket_for_mutation`(정확 일치만)
    이 소유하고, 여기 남는 소비자는 부작용 없는 조회(`show`)뿐이다.

    STATUS_DIRS(전 상태 디렉토리)에서 못 찾으면 draft 격리 디렉토리
    (`drafts_dir()`)를 폴백으로 스캔해 pseudo-status `"draft"` 로 반환한다 — `show` 가 draft 를
    조회할 수 있게 한다.
    """
    exact = find_ticket_exact(home, tid)
    if exact is not None:
        return exact

    def _loud_if_mismatch(result: tuple[str, Path]) -> tuple[str, Path]:
        # 폴백이 남의 티켓을 무음으로 답하는 마지막 표면 가시화 — 차단이 아니라 경고
        # (legacy `id:` 부재 티켓 조회는 유지·정확 후보가 있으면 여기 도달하지 않는다).
        canonical = _canonical_ticket_id(result[1])
        if canonical and canonical != tid:
            print(
                f"⚠ 정확 일치 티켓 없음 — {result[1].name} (canonical {canonical}) 로 폴백",
                file=sys.stderr,
            )
        return result

    try:
        return _loud_if_mismatch(find_item(pm_paths.tickets_dir(home), ticket_rules.STATUS_DIRS, tid, "ticket"))
    except FileNotFoundError:
        for p in pm_paths.drafts_dir(home).glob(f"{tid}-*.md"):
            return _loud_if_mismatch(("draft", p))
        raise


def find_idea(home, iid: str) -> tuple[str, Path]:
    """Return (status_dir, path) for idea `iid`. Raises FileNotFoundError."""
    return find_item(pm_paths.ideas_dir(home), ticket_rules.IDEA_STATUS_DIRS, iid, "idea")


# 티켓 파일을 **읽어서 다시 쓰는** 경로(lifecycle 이동·frontmatter 갱신)는 본문을
# LF 로 다루지만, 되쓸 때 표기를 뒤집으면 안 된다. CRLF 체크아웃(Windows `core.autocrlf=true`)
# 채택자에선 손대지 않은 줄까지 전면 diff 가 되고, 위임 사본 harvest 가 byte 로 재는 "준비 뒤
# 외부 편집" 판정(pm_delegate)이 내용은 같은데 stale 로 오판해 회수를 거부한다.
# 진실은 OS 가 아니라 **그 파일의 현재 표기**다 — 플랫폼 분기를 쓰지 않는다.
DEFAULT_TEXT_NEWLINE = "\n"


def _dominant_newline(text: str, default: str = DEFAULT_TEXT_NEWLINE) -> str:
    """번역 없이 읽은 본문의 지배 개행 — 다수결, 동수면 첫 등장, 개행 0 이면 `default`.

    `pm_import.dominant_newline` 과 같은 규칙이다(두 구현의 일치는 테스트가 못박는다). board 는
    엔진 모듈을 import 하지 않으므로 이 작은 원시 함수만 각자 둔다."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return default
    if crlf != lf:
        return "\r\n" if crlf > lf else "\n"
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def ticket_newline(path: Path) -> str:
    """그 티켓 파일의 현재 지배 개행 — 부재·읽기 실패·비-UTF8 은 `DEFAULT_TEXT_NEWLINE`(신규 발행 = LF)."""
    try:
        with fs.open_shared(Path(path), binary=False, encoding="utf-8", newline="") as handle:
            return _dominant_newline(handle.read(), DEFAULT_TEXT_NEWLINE)
    except (OSError, UnicodeError):
        return DEFAULT_TEXT_NEWLINE


def _with_newline(text: str, newline: str) -> str:
    """LF 본문을 주어진 표기로 되돌린다 (삽입 블록도 같은 표기 — 혼재 금지)."""
    return text if newline == "\n" else text.replace("\n", newline)


def _write_text_keeping_newlines(path: Path, text: str) -> None:
    """줄끝을 **번역하지 않고** 쓴다 — 본문이 담은 표기가 그대로 bytes 가 된다."""
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _issued_ticket_id(home, path: Path, fm: dict[str, Any] | None = None) -> str | None:
    """그 파일이 **발행한 티켓 ID** — frontmatter `id` 가 진실, 미기재/읽기 실패만 파일명 폴백.

    **파일명은 ID 의 진실이 아니다.** 대소문자 무시 파일시스템(Windows·macOS)에서 `T-AAA-001`
    과 `T-aaa-001` 은 같은 이름이라, 파일명을 진실로 쓰면 발행 ID 의 case 가 그 파일이 어느
    이름으로 먼저 만들어졌는지(또는 체크아웃 때 어떻게 접혔는지)에 따라 뒤집힌다. frontmatter
    는 파일 내용이라 어느 파일시스템에서도 접히지 않는다.

    폴백을 남기는 이유는 clobber 안전이다 — 티켓의 ID 를 통째로 버리면 유일성 판정과 번호
    이어붙이기가 그 ID 를 모른 채 전진해 그 위에 다른 티켓을 덮어쓴다. 그래서 폴백 조건은 둘이다:
    읽기 실패/`id` 부재 · **ID 문법이 아닌 `id` 값**(파일명은 문법을 만족하는데 frontmatter 만
    깨진 형상에서 번호가 통째로 사라지지 않게). 값은 문자열로 정규화한다(YAML 이 `id: 123` 을
    int 로 파싱하는 형상 포함).

    `fm` 을 이미 읽었으면 넘겨서 재파싱을 피한다(`_scan_prefix_tickets` 순회).
    """
    if fm is None:
        loaded = load_ticket_soft(home, path)
        fm = loaded[0] if loaded is not None else None
    raw = (fm or {}).get("id")
    tid = str(raw).strip() if raw is not None else ""
    if tid and ticket_rules._ticket_id_number(tid) is None:
        tid = ""                       # ID 문법 아님 — 진실로 쓸 수 없다(파일명 폴백).
    return tid or ticket_rules._ticket_id_from_filename(path.name)


def _ticket_document(fm: dict[str, Any], body: str, newline: str) -> str:
    """티켓 파일 전체 본문 — frontmatter 직렬화 + body 를 그 파일의 표기로 되돌린 결과."""
    import yaml

    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    return _with_newline(f"---\n{fm_text}\n---\n{body}", newline)


def dump_ticket(path: Path, fm: dict[str, Any], body: str) -> None:
    # A partial fresh-adopter scaffold must not turn a valid lifecycle write
    # into FileNotFoundError.  The destination directory is part of the write
    # contract, including `new`'s initial open/ ticket.
    path.parent.mkdir(parents=True, exist_ok=True)
    # 표기는 **대상 파일의 현재 것**을 유지한다(부재 = 신규 발행이라 LF). 상태 이동 call site 도
    # `move_ticket`(rename·bytes 무변경) 뒤에 부르므로 대상은 이미 원본 파일이다.
    _write_text_keeping_newlines(
        path, _ticket_document(fm, body, ticket_newline(path)))


def dump_ticket_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    """`dump_ticket` 과 같은 바이트를 쓰되 temp 파일 + `fs.atomic_replace` 로 원자 교체한다.

    부분쓰기로 티켓 frontmatter 가 깨지는 것을 막는다(worktree_pool `_write_ledger`
    동형 — tmp 에 전체를 쓰고 같은 디렉토리 안에서 atomic rename). backfill 처럼
    *기존* 티켓을 제자리 갱신할 때 쓴다 — 같은 status 디렉토리 안 rename 이라 원자적이다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = _ticket_document(fm, body, ticket_newline(path))
    tmp = path.with_suffix(path.suffix + ".tmp")
    _write_text_keeping_newlines(tmp, document)
    fs.atomic_replace(tmp, path)


def move_item(base_dir: Path, src: Path, dst_status: str) -> Path:
    """Atomic mv of an item file into a sibling status directory.

    On POSIX rename(2) is atomic and a lost race surfaces as FileNotFoundError
    (the source is already gone). 이 primitive 자체는 generic ticket/idea 이동이라 lock-free다.
    티켓 호출자는 lifecycle rename과 in-place write(tier) 사이 lookup→path 고정→replace TOCTOU를
    막기 위해 전체 read/transform/move/write를 `board_lock`으로 감싼다. idea는 in-place writer
    대상이 아니므로 별도 lifecycle의 현행 lock-free 호출을 유지한다.
    """
    dst_dir = base_dir / dst_status
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    os.rename(src, dst)
    return dst


def move_ticket(home, src: Path, dst_status: str) -> Path:
    """Atomic mv into a ticket status directory."""
    return move_item(pm_paths.tickets_dir(home), src, dst_status)


def move_idea(home, src: Path, dst_status: str) -> Path:
    """Atomic mv into an idea status directory."""
    return move_item(pm_paths.ideas_dir(home), src, dst_status)


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


def _next_prefixed_id(home, prefix: str) -> str:
    """Next `T-<canonical>-NNN` for `prefix`, matched **case-insensitively**.

    prefix 동일성은 case-insensitive fold 이되 발행 ID 는 *기존 시리즈* case(canonical)를 이어쓴다
    — `--prefix aaa` 가 case-분할 `T-aaa-*` 를 새로 파지 않고 기존 `T-AAA-*` 를 잇게 한다. fold
    일치하는 모든 case 변종의 최대 번호+1 을 세고(case-insensitive 카운트), canonical case 는
    최저 번호 티켓의 실제 case 다. fold-일치 티켓이 없으면 입력 case 그대로(최초 사용이 canonical
    case 확립)·번호 1.

    **시리즈 판정의 입력은 파일명이 아니라 발행 ID(frontmatter `id`)** 다(`_issued_ticket_id`).
    파일명으로 세면 대소문자 무시 파일시스템(Windows·macOS)에서 판정이 무너진다 — `T-aaa-001`
    로 쓴 티켓이 기존 `T-AAA-001-*.md` 파일에 그대로 얹혀 파일명은 `AAA`, 내용은 `aaa` 로
    갈리고, 그러면 다음 발행 case 가 *파일이 어느 이름으로 먼저 만들어졌는가*로 결정된다.
    같은 이유로 최저 번호가 동률이면 ID 문자열 자체로 tie-break 해(`(num, tid)` 최소) 파일
    순회 순서·파일시스템에 결과가 흔들리지 않게 한다.

    prefix 경계는 `_ticket_prefix`(ID 문법 단일 진실)로 뗀 마디를 fold 비교한다 — 하이픈 포함
    prefix(`service-a`)도 번호 경계 모호성 0 이고, `AB` 와 `ABC` 는 서로 다른 네임스페이스다.
    """
    fold = prefix.lower()
    max_num = 0
    anchor: tuple[int, str] | None = None   # (최저 번호, 그 ID) — canonical case 의 결정적 출처.
    for d in ticket_rules._ID_SCAN_STATUSES:
        for p in (pm_paths.tickets_dir(home) / d).glob("T-*.md"):
            tid = _issued_ticket_id(home, p)
            if not tid:
                continue
            tid_prefix = ticket_rules._ticket_prefix(tid)
            if tid_prefix is None or tid_prefix.lower() != fold:
                continue
            num = ticket_rules._ticket_id_number(tid)
            if num is None:
                continue
            max_num = max(max_num, num)
            if anchor is None or (num, tid) < anchor:
                anchor = (num, tid)
    canonical = ticket_rules._ticket_prefix(anchor[1]) if anchor else prefix   # 최초 사용: 입력 case.
    return f"T-{canonical}-{max_num + 1:03d}"


def _next_id(home, prefix: str | None = None) -> str:
    """Next ticket ID. Namespaced per prefix so concurrent areas never collide.

    prefix=None → legacy `T-NNNN` (4-digit). prefix="PAY" → `T-PAY-NNN` (3-digit),
    counted independently. The legacy regex `T-(\\d+)-` never matches a prefixed
    file, so the two namespaces stay disjoint. prefix 동일성은 case-insensitive fold —
    `--prefix aaa` 는 기존 `T-AAA-*` 시리즈를 이어간다(`_next_prefixed_id`).
    legacy 경로(prefix=None)는 무변경(`T-NNNN` 회귀 0).
    """
    if prefix:
        return _next_prefixed_id(home, prefix)
    n = next_numeric_id(pm_paths.tickets_dir(home), ticket_rules._ID_SCAN_STATUSES, "T-*.md", r"T-(\d+)-")
    return f"T-{n:04d}"


def _next_idea_id(home) -> str:
    n = next_numeric_id(pm_paths.ideas_dir(home), ticket_rules.IDEA_STATUS_DIRS, "[0-9]*.md", r"(\d+)-")
    return f"{n:04d}"
