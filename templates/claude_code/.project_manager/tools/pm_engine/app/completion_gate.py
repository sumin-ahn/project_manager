"""완료·리뷰·livegate 판정 (층: app)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from pm_engine.app import release
from pm_engine.app import round_seed
from pm_engine.domain import markdown_text
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.domain import ticket_rules
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.infra import regression_store
from pm_engine.infra import review_rounds
from pm_engine.infra import ticket_store
import argparse
import datetime
import json
import os
import re
import sys


GATE_CHANNEL_ADDITIONAL = "additional"    # review_rounds.json (추가 리뷰어)


_UTC_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)


_TARGET_REV_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _parse_utc_iso(value: object) -> datetime.datetime | None:
    """writer 형식의 ISO-8601 UTC 시각만 datetime 으로 파싱한다 (아니면 None).

    `datetime.fromisoformat` 단독은 공백 separator·offset 생략 등도 받아들인다. 장부 선후는 보안
    경계라 writer 가 내는 `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`(`Z`도 동치)만 받고, 실제 달력값과
    UTC offset 을 다시 확인한다. 문자열 사전순 비교는 손상값 `zzz`가 미래로 통하는 구멍이라 쓰지
    않는다."""
    if not isinstance(value, str) or _UTC_ISO_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == datetime.timedelta(0) else None


def _unresolved_must_fix_data(home, data: object, ledger: Path) -> list[str]:
    """파싱된 라운드 장부의 미해소 must-fix 차단 사유 (writer/reader 공용)."""
    if not isinstance(data, dict):
        return ["  · 라운드 장부 형식 오류(최상위가 매핑이 아님) — 잔여 must-fix 를 확인할 수 "
                "없어 차단합니다"]
    problems: list[str] = []
    for gate, entry in sorted(data.items()):
        if gate == review_rounds._REVIEW_LEDGER_WAVE_KEY:
            continue                     # 예약 키(wave 예산 절)는 게이트가 아니다.
        # 추가 리뷰어(release) 축은 pm-verified 처분을 허용한다 — PM rejected/기계 확인 증거로
        # 채널 폐지 뒤에도 재호출 없이 잔여를 종결할 수 있어야 한다(콜백은 그 게이트의
        # 잔여 must-fix 건수로 스코프한 채널 격리 재검증).
        problem = review_rounds._gate_disposition_problem(
            gate, entry,
            allow_pm_verified=True,
            pm_verified_problem=_gate_pm_verified_problem(home,
                gate, entry, GATE_CHANNEL_ADDITIONAL,
            ),
        )
        if problem is not None:
            problems.append(problem)
    return problems


def _unresolved_must_fix_gates(home, ledger: Path) -> list[str]:
    """미해소 must-fix 잔여의 차단 사유 목록 — 없으면 빈 목록(=릴리즈 진행).

    장부가 **아예 없으면 무대상**이다(빈 목록) — 추가 리뷰어를 쓰지 않는 채택자에겐 검사할 사실
    자체가 없고, 그 형상에 없는 파일을 요구하면 게이트가 아니라 장애물이다. 반면 장부가 있는데
    **읽지 못하면 차단**한다: "잔여가 없다"를 확인하지 못한 것과 "잔여가 없다"는 다르다. 그 원칙은
    파일 단위에서 끝나지 않고 **항목 단위**까지 간다(`gate_entry_corruption`).
    """
    if not ledger.exists():
        return []
    try:
        data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return [f"  · 라운드 장부를 읽지 못했습니다 ({type(exc).__name__}: {exc}) — 잔여 "
                "must-fix 를 확인할 수 없어 차단합니다 (장부 수리 후 재실행)"]
    return _unresolved_must_fix_data(home, data, ledger)


def _sync_release_must_fix_marker(home, flag: Path) -> list[str]:
    """장부 스냅샷 판정과 잔여 표식 교체를 writer 락 하나로 직렬화한다.

    livegate record/check 경로다. additional_reviewer 는 이미 같은 락을 보유한 `_save_round_ledger` 안에서
    `_unresolved_must_fix_data` + `_write_release_must_fix_marker` 를 직접 호출해 재진입을 피한다."""
    ledger = release._release_rounds_ledger(flag)
    with fs.exclusive_file_lock(release._release_rounds_lock(ledger)):
        problems = _unresolved_must_fix_gates(home, ledger)
        release._write_release_must_fix_marker(flag, problems)
    return problems


def _livegate_check(home, args: argparse.Namespace) -> int:
    """기록된 라이브 게이트가 `--rev <sha>` 에 대해 green 인지 검증한다 (보호훅 소비 채널).

    기록 존재 ∧ status==pass ∧ head==rev → rc0. 아니면 rc1 + 사유 3분화(기록 부재 / red /
    rev 불일치 — 각각 다른 메시지로 훅이 그대로 surface). fail-soft 아님(명확 rc).

    읽을 livegate.json 은 record 와 **동일 해소**(`_resolve_livegate_flag` — engine-root sidecar)
    를 거친다(단일 소스). 기본 위치(`pm_paths.livegate_flag`) 직독이 아니라, 어느 board.py 사본/cwd 로
    check 하든 push 보호훅이 기록한 **바로 그** 파일을 읽어 stale/wrong-copy 오독(false-green/
    false-red)을 원천 차단한다 — record(`_livegate_record`)의 기록-위치 정렬과 대칭이다.
    `cwd` = `--cwd`(record 와 대칭·미지정 시 이 board.py 사본의 `REPO` — 그 repo 의 `core.hooksPath`
    로 engine-root 해소). engine-root 무효(`_LG_BROKEN`)는 record 와 **동형** fail-loud(조용한 통과 금지).
    """
    rev = getattr(args, "rev", None)
    if not rev:
        print("livegate check: --rev <sha> 필요 (push 대상 커밋).", file=sys.stderr)
        return 1
    # 읽을 위치를 push 보호훅 read 위치와 정렬(record 와 대칭·단일 소스). 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 사본으로 check 해도
    # 훅이 기록한 한 파일을 읽는다. hooksPath 미설정/훅 부재면 현행 `pm_paths.livegate_flag`(REPO/.local) 폴백 무변경.
    cwd = getattr(args, "cwd", None) or str((home.root))
    flag, mode = regression_store._resolve_livegate_flag(home, cwd)
    if mode == regression_store._LG_BROKEN:
        # 보호훅 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소) →
        # 기록 위치와 훅 read 위치가 갈릴 수 있어 pass/fail 판정을 신뢰 못 한다. 조용한 통과 대신
        # record 와 동형 fail-loud 거부(false-green/false-red 백스톱). engine-root
        # sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단). engine-root sidecar 수리 또는 "
              "`pm-config repo add` 재실행. 릴리즈 차단.", file=sys.stderr)
        return 1
    # 미해소 must-fix 잔여 — record 와 **같은 검사**를 소비 시점에도 한다(대칭). 기록은 HEAD 로
    # 키되지만 라운드 장부는 그 뒤로도 자란다: record 가 green 을 찍은 뒤 같은 HEAD 에서 새 반려
    # 라운드가 기록되면 옛 green 이 push 를 통과시킨다(check 만 스냅샷을 믿는 창). 판정을 소비
    # 지점에서 다시 물어 그 창을 닫는다 — 읽기 전용이라 기록은 손대지 않는다.
    try:
        # record 와 같은 장부 스냅샷으로 표식까지 현행화한다. check 가 라운드 writer 와 경합해
        # 옛 clear 를 새 blocked 위에 덮지 않도록 둘 다 review_rounds.lock 을 공유한다.
        problems = _sync_release_must_fix_marker(home, flag)
    except OSError as exc:
        print("livegate: fail — must-fix 잔여 표식을 원자 갱신할 수 없어 차단 "
              f"({type(exc).__name__}: {exc}). 장부/`.local` 권한을 복구한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return 1
    if problems:
        print(release._MUST_FIX_BLOCK_GUIDANCE.format(
            count=len(problems), items="\n".join(problems),
            ledger=release._release_rounds_ledger(flag)), file=sys.stderr)
        return 1
    if not flag.exists():
        print("livegate: 기록 없음 — `board.py livegate record` 필요 (릴리즈 차단).",
              file=sys.stderr)
        return 1
    data = json.loads(fs.read_text_shared(flag, encoding="utf-8"))
    if data.get("status") != "pass":
        n, rc = data.get("n"), data.get("rc")
        print(f"livegate: RED (status={data.get('status')}, 수집 {n}, rc={rc}) "
              "— 릴리즈 차단.", file=sys.stderr)
        return 1
    head = data.get("head")
    if head != rev:
        print(f"livegate: rev 불일치 (기록 {str(head)[:8]} ≠ push {str(rev)[:8]}) "
              "— `livegate record` 재실행 필요 (릴리즈 차단).", file=sys.stderr)
        return 1
    if "platforms" in data:
        problem = release._release_evidence_problem(home, data, flag, rev)
        if problem is not None:
            print(f"livegate: platform snapshot 불일치({problem}) — 릴리즈 차단.", file=sys.stderr)
            return 1
    else:
        _core, _anchor, commands, problem = release._release_core_context(home, flag, os.path.abspath(cwd))
        if commands or problem is not None:
            print(f"livegate: platform snapshot 누락({problem or '현재 선언 플랫폼 존재'}) — "
                  "livegate record 재실행 필요. 릴리즈 차단.", file=sys.stderr)
            return 1
    print(f"livegate: green @ {str(rev)[:8]} ✓")
    return 0


def _pm_verified_channel_role(channel: str) -> str | None:
    """게이트 채널 → review_form 의 reviewer role 문자열 (모르는 채널이면 None → fail-closed).

    role 문자열은 review_form 이 단일 진실이라 board 에 사본을 두지 않는다."""
    return {
        GATE_CHANNEL_ADDITIONAL: review_form.ADDITIONAL_REVIEWER_ROLE,
    }.get(channel)


def _pm_verified_delegate_problem(home,
    tid: str, *, channel: str, entry: dict, checker_name: str,
) -> str | None:
    """티켓/채널 입력을 해소해 review_state 의 지정 pm-verified checker를 호출한다.

    preflight와 post-resolution evidence가 티켓/채널/표면 하한 해소를 복제하지 않게 하는 공용
    배선이다. 실제 판정은 checker_name이 가리키는 review_state 함수만 소유한다.

    추가 리뷰어 release 게이트는 `GATE_CHANNEL_ADDITIONAL` 로 호출하고, 판정 표면 하한은 **그
    채널 장부 entry 의 잔여 must-fix 건수**다. 채널 스코프 없이 티켓 전역을 보면 다른 채널의
    기계 확인이 이 게이트의 잔여를 여는 구멍이 생긴다."""
    found = ticket_store.find_ticket_exact(home, tid)
    if found is None:
        return f"티켓을 찾지 못했습니다: {tid}"
    _status, path = found
    try:
        spec_text = fs.read_text_shared(path, encoding="utf-8", newline="")
    except (OSError, UnicodeError) as exc:
        return f"티켓 명세를 읽을 수 없습니다: {exc}"
    reviewer_role = _pm_verified_channel_role(channel)
    if reviewer_role is None:
        return f"pm-verified 재검증 채널을 확정할 수 없습니다: {channel!r}"
    rounds = round_seed.load_rounds(pm_paths.tickets_dir(home), tid, ticket_text=spec_text)
    checker = getattr(review_state, checker_name)
    return checker(
        spec_text, rounds,
        reviewer_role=reviewer_role,
        surface_floor=review_rounds.gate_residual_must_fix(entry),
    )


def _pm_verified_evidence_problem(home, tid: str, *, channel: str, entry: dict) -> str | None:
    """`pm-verified` 발동·완료의 post-resolution 증거를 엄격 재검증한다."""
    return _pm_verified_delegate_problem(home,
        tid, channel=channel, entry=entry,
        checker_name="pm_verified_evidence_problem",
    )


def _gate_pm_verified_problem(home,
    gate: str, entry: dict, channel: str,
) -> Callable[[], str | None]:
    """게이트별 pm-verified 콜백 factory — 루프 클로저의 뒤늦은 바인딩을 피한다.

    `_gate_disposition_problem` 은 이 콜백을 같은 순회 안에서 즉시 호출하지만, 인자를 함수
    시그니처로 고정해 두면 나중에 호출 지점이 지연·재배치되더라도 클로저가 마지막 `gate`/`entry`
    를 참조하는 사고가 구조적으로 막힌다."""
    return lambda: _pm_verified_evidence_problem(home, gate, channel=channel, entry=entry)


_DOD_SECTION = "## 완료 조건"


_DOD_CHECKED_MARKS = frozenset({"x", "X"})


_DOD_DEFERRED_MARK = ">"


_DOD_CHECKBOX_RE = re.compile(
    r"^\s*[-*+]\s+\[(?P<mark>[^\[\]]*)\](?![\(\[])\s?(?P<text>.*)$")


_DOD_DEFERRED_REASON_RE = re.compile(r"\(이월:\s*\S[^)]*\)")


_DOD_VALUE_FORMS = "`- [x] <원문>` 또는 `- [>] <원문> (이월: <사유·귀속>)`"


def _dod_section_texts(body: str) -> list[str]:
    """`## 완료 조건` 절 본문 **전부** — 공용 슬라이서(`_section_texts`)의 DoD 이름(정확 일치).

    존재 판정은 설계 절과 같은 규칙(`exact`)이다: 헤딩 줄이 그 이름 자체이거나 괄호 부제만
    붙어야 한다(`## 완료 조건 (Definition of Done)`). 자유 접두 일치는 `## 완료 조건 검토 이력`
    같은 **다른 절**을 DoD 로 잡아 실제 DoD 를 통째로 가린다 — 그 절엔 체크박스가 없으니 미체크
    항목이 있어도 완료가 통과했다(fail-open).

    **중복 절은 합산한다**(거부하지 않는다). 기존 판정 구조가 "있는 체크박스의 미결만 막는다"
    (절 부재·체크박스 0개는 무영향)이므로, 새 형식 거부를 만드는 것보다 판정 대상을 전 절로
    넓히는 쪽이 그 구조와 같은 축이다 — 우회(앞 빈 절로 뒤 절 가리기)는 그대로 닫히고 레거시
    본문에는 새 차단 사유가 생기지 않는다."""
    return markdown_text._section_texts(body, _DOD_SECTION, exact=True)


def _dod_heading_mismatch(body: str) -> str | None:
    """`## 완료 조건` 으로 시작하지만 절 이름 규칙에 안 맞는 헤딩 줄 (없으면 None).

    정확 일치 규칙은 오인 매칭을 닫는 대신 **새 사각**을 만든다: `## 완료 조건 (DoD) — 부제`
    처럼 괄호 밖 부제가 붙은 헤딩은 어느 절로도 안 잡혀 DoD 가 통째로 안 보인다(미체크 항목이
    있어도 통과 — 닫으려던 그 형상 그대로). 그래서 "정확 일치 실패 + 접두는 일치"를 조용한 통과가
    아니라 차단 사유로 표면화한다. 펜스 안 인용 헤딩은 슬라이서와 같은 규칙으로 먼저 걷는다."""
    if markdown_text._section_text(body, _DOD_SECTION, exact=True) is not None:
        return None
    lines = markdown_text._FENCED_CODE_RE.sub("", body).splitlines()
    return next((line.strip() for line in lines if line.startswith(_DOD_SECTION)), None)


def _dod_open_items(body: str) -> list[str]:
    """done 을 막는 DoD 사유 목록 — 전부 통과하면 빈 리스트.

    `_complete_gate` 가 쓰는 단일 깔때기다(ticket_finish 의 선-검사도 이 함수를 부른다).
    막는 형상 넷:

      1. **절 부재** — `## 완료 조건` 절이 없다. 판정할 완료 조건이 없으면 완료를 증명할 수
         없다. 레거시 면제는 두지 않는다 — 같은 판정이 `lint`(thin·`_REQUIRED_SECTIONS`)로
         이미 open/claimed 를 차단하고 있어 이 게이트로 새로 잠기는 활성 티켓이 없다.
      2. **헤딩 미인식** — 절 이름 규칙 밖 헤딩(기존 차단·`_dod_heading_mismatch`).
      3. **체크박스 0개**(산문 DoD) — 기계로 마감을 판정할 항목이 없다.
      4. **전량 이월** — 항목은 있는데 **전부** 사유 있는 이월이라 실제로 한 게 하나도 없다.
         그건 구현 완료가 아니라 처분이므로 `discard` 로 종결한다. 부분 이월(`[x]` ≥1)은
         통과다. 미체크·미지 마커가 하나라도 섞이면 처분이 아니라 **미완료**이므로 이 진단을
         내지 않는다 — 그 항목들은 위 형상들이 각각 지목한다(틀린 방향 안내 금지).

    `## 완료 조건` 절이 여럿이면 **전 절을 합산**한다 — 첫 절만 보면 앞에 빈 절 하나를 두고 뒤
    절에 미체크 항목을 남기는 우회가 성립한다.
    """
    sections = _dod_section_texts(body)
    if not sections:
        heading = _dod_heading_mismatch(body)
        if heading is None:
            return [f"DoD 절 부재: `{_DOD_SECTION}` 절이 없어 완료를 판정할 항목이 하나도 "
                    f"없다 — 절을 채우고 각 항목을 {_DOD_VALUE_FORMS} 로 마감하라"]
        return [f"DoD 절 헤딩 형식: {heading!r} — 절을 식별할 수 없어 미체크 항목을 판정하지 "
                f"못한다. `{_DOD_SECTION}` 또는 `{_DOD_SECTION} (부제)` 형식으로 쓰라"]
    problems: list[str] = []
    checkbox_count = 0
    checked_count = 0
    deferred_count = 0        # 사유가 붙은 유효 이월만 센다 — 전량 이월 판정의 분자.
    for line in "\n".join(sections).splitlines():
        match = _DOD_CHECKBOX_RE.match(line)
        if match is None:
            continue
        checkbox_count += 1
        # 마커는 좌우 공백을 걷고 본다 — `- [ x ]` 는 사람이 체크한 표기다. 걷고도 남는 다문자
        # (`xx`)는 통과 형식이 아니므로 미지 마커로 차단한다(비인식=통과 폐쇄).
        mark = match.group("mark").strip()
        text = match.group("text").strip()
        if mark in _DOD_CHECKED_MARKS:
            checked_count += 1
            continue
        if mark == _DOD_DEFERRED_MARK:
            if _DOD_DEFERRED_REASON_RE.search(text):
                deferred_count += 1
                continue
            problems.append(
                f"DoD 이월 사유 누락: {text!r} — 이월은 같은 줄에 `(이월: <사유·귀속>)` 필수")
            continue
        unknown = f"마커 `[{mark}]` 는 통과 형식이 아니다 · " if mark else ""
        problems.append(f"DoD 미체크: {text!r} — {unknown}{_DOD_VALUE_FORMS} 로 마감하라")
    if checkbox_count == 0:
        problems.append(
            f"DoD 체크박스 0개: `{_DOD_SECTION}` 절에 기계로 판정할 항목이 없다(산문 DoD) — "
            f"각 항목을 {_DOD_VALUE_FORMS} 형식으로 적어라")
    elif deferred_count == checkbox_count:
        problems.append(
            f"DoD 전량 이월(사유 있는 이월 {deferred_count} / 체크박스 {checkbox_count}개): "
            "구현된 항목이 하나도 없다 "
            "— done 이 아니라 **처분**이다. 병합·폐기라면 "
            f"`board.py discard <ID> {'|'.join(ticket_rules.DISPOSITION_KINDS)} --reason <사유>` 로 "
            "종결하라(부분 이월은 `[x]` 가 하나 이상일 때 통과한다)")
    return problems
