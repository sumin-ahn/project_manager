"""릴리즈 라이브 게이트 판정 (층: app).

미해소 must-fix 잔여 표식·거부, 라운드 장부 위치, 저장 증거와 현재 선언의 재검증을 소유한다.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from collections.abc import Sequence

from pm_engine.infra import clock, fs, git, regression_store, review_rounds
from pm_engine.app import regression


                           # tests/test_release_wave.py `_EXPECTED_RELEASE_TESTS` 와 값 공유.
LIVEGATE_TEST_CMD = "pytest -m release -q"   # 라이브 릴리즈 wave selection.

# ── 릴리즈 must-fix 잔여 차단 (릴리즈 절차의 첫 기계 관문) ────────────────
# "must-fix 잔여가 있으면 릴리즈하지 않는다"를 PM 규율이 아니라 기계 게이트로 박는다. 라운드
# 상한으로 종결된 게이트의 잔여 must-fix가 현재 티켓 fix에서 닫히지 않은 채 릴리즈 절차가
# 진행되던 것이 근절 대상이다.
#
# 판정 입력은 **장부의 기록 사실**뿐이다 — 최종 라운드의 must_fix 수(`gate_residual_must_fix`),
# 처분 선언(`gate_resolution`)과 기계 확인 증거다. PM 자의 판정("사소함·엣지")이 들어갈
# 자리를 만들지 않는다. 우회 플래그도 없다.
#
# 검사 지점이 `livegate record` 인 이유는 그것이 릴리즈 절차의 첫 기계 관문이고 push 보호훅이 그
# green 을 소비하기 때문이다 — 여기서 막으면 하류 전체가 막힌다. 그래서 차단은 **fail 기록까지**
# 남긴다(같은 HEAD 의 옛 green 이 살아남아 훅을 통과하는 창을 닫는다).
_LIVEGATE_MUST_FIX_REASON = "unresolved-must-fix"   # fail 기록의 사유 표식(훅 메시지와 별개 감사면).
_RELEASE_MUST_FIX_MARKER_NAME = "release-must-fix"
_RELEASE_MUST_FIX_CLEAR = "clear"
_RELEASE_MUST_FIX_BLOCKED = "blocked"
_MUST_FIX_BLOCK_GUIDANCE = (
    "livegate: fail — 미해소 must-fix 잔여 {count}건 (릴리즈 차단 · 우회 플래그 없음).\n"
    "{items}\n"
    "  처방 — 현재 티켓 fix의 기계 확인 증거로 게이트마다 처분을 선언하세요:\n"
    "    python3 .project_manager/tools/additional_reviewer.py "
    "--resolve-gate <게이트> --pm-verified\n"
    "  라운드 장부: {ledger}"
)

# ── 릴리즈 엔진 사본 drift 차단 (릴리즈 절차의 두 번째 기계 관문 · must-fix 거부 직후) ──
# 실행 엔진 사본(PM 홈 `.project_manager/tools/`)이 upstream 과 drift 한 상태에서 라이브 wave 를
# 돌리면 게이트 판정이 false 가 된다(빈 diff false-green · stale-pin false-block). must-fix 와
# 같은 이유로 값비싼 라이브 wave **전에** 막고, fail 을 기록까지 한다(같은 HEAD 의 옛 green 잔존 0).
_LIVEGATE_ENGINE_DRIFT_REASON = "engine-drift"   # fail 기록의 사유 표식(must-fix 축과 구분).
_ENGINE_DRIFT_BLOCK_GUIDANCE = (
    "livegate: fail — 실행 엔진 사본이 upstream 과 drift 상태 {count}건 (릴리즈 차단 · 우회 플래그 없음).\n"
    "{items}\n"
    "  처방: python3 .project_manager/tools/pm_update.py 로 엔진 사본을 동기한 뒤 재실행하세요."
)


def _release_rounds_ledger(flag: Path) -> Path:
    """릴리즈 검사가 읽을 라운드 장부 — livegate.json 과 **같은 `.local`**.

    기록 위치 정렬(`_resolve_livegate_flag`)을 그대로 물려받는다 — 어느 board.py 사본/cwd 로
    record 하든 additional_reviewer 가 실제로 쓴 그 장부(소유 PM 홈 `.local/review_rounds.json`)를
    본다. 사본별로 다른 장부를 읽으면 잔여가 있는데 "무대상"으로 통과하는 false-green 이 난다.

    정렬이 성립하는 범위는 **engine-root 모드**(보호훅 활성)다 — 훅 부재 폴백(`_LG_NO_HOOK`)에서는 호출된
    사본의 `.local` 이 곧 장부 위치라 같은 식이 그대로 맞는다(단일-repo 형상)."""
    return flag.parent / review_rounds.REVIEW_ROUNDS_LEDGER_NAME


def _release_must_fix_marker(flag: Path) -> Path:
    """보호훅이 shell 단독으로 읽는 현행 잔여 표식 — livegate/장부와 같은 `.local`."""
    return flag.parent / _RELEASE_MUST_FIX_MARKER_NAME


def _release_rounds_lock(ledger: Path) -> Path:
    """additional_reviewer writer 와 livegate record/check 가 공유하는 장부 락 경로."""
    return ledger.with_name("review_rounds.lock")


def _write_release_must_fix_marker(flag: Path, problems: Sequence[str]) -> Path:
    """현행 잔여를 `clear|blocked` 정확 한 줄로 원자 교체한다.

    훅은 JSON/Python 없이 이 파일만 읽는다. 미해소 상태는 **장부보다 먼저**, 해소 상태는 **장부보다
    나중**에 쓰는 순서를 writer 가 지키면 두 파일을 한 rename 으로 묶을 수 없는 crash 창도 항상
    차단 쪽으로만 기운다. 이 함수는 한 파일의 atomic replace 를 소유하고, 호출 순서는 writer 가
    소유한다. 호출자는 review_rounds.lock 을 보유해야 한다."""
    marker = _release_must_fix_marker(flag)
    marker.parent.mkdir(parents=True, exist_ok=True)
    state = _RELEASE_MUST_FIX_BLOCKED if problems else _RELEASE_MUST_FIX_CLEAR
    tmp = marker.with_name(
        f"{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_text(state + "\n", encoding="ascii", newline="\n")
        fs.atomic_replace(tmp, marker)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
    return marker


def _refuse_release_for_must_fix(flag: Path, cwd: str, problems: list[str]) -> int:
    """미해소 must-fix 잔여로 릴리즈를 거부한다 — fail 기록 + 목록·처방 + rc1.

    fail 을 **기록까지** 하는 이유는 훅이 `livegate check` 로 소비하는 그 파일이 판정의 하류이기
    때문이다 — 거부만 하고 파일을 그대로 두면 같은 HEAD 에 남은 옛 green 이 push 를 통과시킨다
    (이 게이트가 막으려는 바로 그 형상). 실행 전 거부라 수집 N 은 0 이고 rc 는 미실행(None)이다.
    """
    flag.parent.mkdir(parents=True, exist_ok=True)   # 기록 경로 보장 (pass 기록과 같은 규칙)
    regression_store._write_json_atomic(flag, {
        "head": git._git_head_at(cwd), "status": "fail", "n": 0, "rc": None,
        "ts": clock.now_utc(), "reason": _LIVEGATE_MUST_FIX_REASON,
    })
    print(_MUST_FIX_BLOCK_GUIDANCE.format(
        count=len(problems), items="\n".join(problems),
        ledger=_release_rounds_ledger(flag)), file=sys.stderr)
    return 1


_ENGINE_DRIFT_LISTED_MAX = 20   # stderr 목록 상한(넘으면 "외 N건"으로 접는다) — must-fix 목록과 별개 상수.


def _release_platform_commands(home, cwd: str) -> tuple[tuple[str, str], ...]:
    """실제 선언 파일이 없는 legacy만 loader 없이 처리한다. 존재/손상은 기존 판독자에 맡긴다."""
    try:
        (Path(cwd) / ".project_manager" / "local.conf").lstat()
    except FileNotFoundError:
        return ()
    return regression._platform_test_commands(home, cwd)


def _release_core_context(home, flag: Path, cwd: str, session: str | None = None):
    """같은 F의 core 앵커에서 선언을 해소한다. readonly L은 선언 앵커를 대체하지 않는다."""
    core_flag = regression_store._regression_flag_for(home, session, local_dir=flag.parent)
    core = None
    if core_flag.exists():
        try:
            core = json.loads(fs.read_text_shared(core_flag, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None, cwd, (), "core record unreadable"
        if not isinstance(core, dict):
            return None, cwd, (), "core record is not an object"
    copied_flag = regression_store._regression_flag_for(home, session, local_dir=home.local_dir)
    if core is None and copied_flag != core_flag and copied_flag.exists():
        try:
            copied = json.loads(fs.read_text_shared(copied_flag, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            copied = None
        if isinstance(copied, dict) and ("command" in copied or "platforms" in copied):
            return None, cwd, (), "core 증거가 호출 사본에만 있어 engine-root 좌표와 불일치"
    anchor = core.get("conf_anchor", cwd) if core is not None else cwd
    if core is not None and ("command" in core or "platforms" in core) and (
            not isinstance(anchor, str) or not os.path.isabs(anchor) or not Path(anchor).is_dir()):
        return core, cwd, (), "core conf_anchor invalid"
    try:
        commands = _release_platform_commands(home, anchor)
        if core is None and not commands:
            anchor = str(flag.parent.parent.parent)
            commands = _release_platform_commands(home, anchor)
        if core is not None and "platforms" in core:
            return core, anchor, commands, "old aggregate requires core rerun"
    except (regression.PlatformConfigError, OSError) as exc:
        return core, anchor, (), f"platform 설정 오류: {exc}"
    return core, anchor, commands, None


def _release_evidence_problem(home, data: dict, flag: Path, rev: str) -> str | None:
    """저장 snapshot과 현재 A/L/F·core·선언·명령을 재검증한다. 자식은 실행하지 않는다."""
    anchor, cwd = data.get("conf_anchor"), data.get("cwd")
    for path in (anchor, cwd):
        if not isinstance(path, str) or not os.path.isabs(path) or not Path(path).is_dir():
            return "release coordinates invalid"
        if git._git_head_at(path) != rev:
            return "release head drift"
    resolved, mode = regression_store._resolve_livegate_flag(home, cwd)
    core_resolved, core_mode = regression_store._resolve_regression_flag(home, anchor)
    if (mode == regression_store._LG_BROKEN or core_mode == regression_store._LG_BROKEN
            or resolved != flag or core_resolved.parent != flag.parent):
        return "release evidence coordinates mismatch"
    core, current_anchor, commands, problem = _release_core_context(home, flag, cwd, data.get("session"))
    if problem is not None:
        return problem
    if current_anchor != anchor or not commands or core is None:
        return "release declaration/core missing"
    problem = regression._core_record_problem(home, core, anchor, rev, session=data.get("session"),
                                   local_dir=flag.parent, release=True)
    if problem is not None:
        return problem
    for key in ("scope", "collected", "previous_collected", "command", "conf_anchor", "session"):
        if data.get(key) != core.get(key):
            return f"core snapshot {key} mismatch"
    normalized = {k: v for k, v in data.items() if k not in {"command", "cwd"}}
    return regression._platform_record_problem(normalized, anchor, rev, commands, livegate=True)
