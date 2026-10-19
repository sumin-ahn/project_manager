"""회귀 게이트 해소와 판정 (층: app).

테스트 명령·실행 위치 해소, 회귀 명령 실행과 출력 중계, 리뷰 사이클 강등 범위, 스위트 없는 트리 거부, 선언 플랫폼
실행과 기록 정합 판정(수집수 저장 계약 포함), 슬롯별 회귀 상태 판정을 소유한다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from collections.abc import Sequence
from typing import NamedTuple

from pm_engine.domain import conf_schema, shell_policy, ticket_rules
from pm_engine.infra import areas_store, clock, conf_store, fs, git, lease_store, pm_paths, python_env, regression_store, review_rounds, ticket_store


def _active_slot_test_cmd(home, session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)에 바인딩된 test_cmd (없으면 None).

    같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 1/2/3·full vs a-only 등)을
    지속적으로 가질 수 있게 — `_test_cmd` 가 이를 repo areas *위* 레이어로 끼운다.

    **이 모듈은 worktree_pool 을 import 하지 않는다**.
    대신 리스 장부 *파일*(`pm_paths.leases_file(home)` = `.local/worktree-leases.json`·worktree_pool 과
    같은 위치)을 stdlib json 으로 직접 read 한다 — areas.md 를 읽듯 데이터-결합만(모듈 결합
    아님). 리스는 worktree_pool 이 원자 교체(`fs.atomic_replace`)로 쓰므로 **락 없는
    point-read 가 일관 스냅샷**을 본다(쓰기 경합과 분리 — 부분쓰기 장부를 못 본다).

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회가 슬롯별 test_cmd
    를 뽑을 때 쓴다(무명시는 현행대로 `session_name()` 해소). 그 행의 test_cmd 가 비어 있지 않으면
    반환. 장부 부재/파싱실패/매칭없음/빈 test_cmd → None
    (침묵 폴백 — 슬롯 레이어는 *추가 우선*이지 강제 아님·호출부가 다음 레이어로 폴백).
    파싱 실패를 에러로 죽이지 않는다(fail-soft — 장부 손상이 회귀해소를 깨면 안 된다).
    """
    if not pm_paths.leases_file(home).exists():
        return None
    try:
        data = json.loads(fs.read_text_shared(pm_paths.leases_file(home), encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 장부 손상 = fail-soft (위 docstring): 유효 JSON 이라도 dict/list 가 아니면
    # `.get`/순회가 크래시 — 회귀해소를 깨지 않게 None 폴백(다음 레이어).
    if not isinstance(data, dict):
        return None
    leases = data.get("leases", [])
    if not isinstance(leases, list):
        return None
    sess = session if session is not None else lease_store.session_name(home)
    for row in leases:
        if not isinstance(row, dict):
            continue
        # `and sess` 방어가드: sess 가 None(모호 M>1·미바인딩)일 때 손상 행(`session: null`·
        # `state: "leased"`)에 None==None 으로 false-match 하지 않게 한다.
        if sess and row.get("session") == sess and row.get("state") == "leased":
            cmd = row.get("test_cmd")
            return cmd or None   # 빈/None → None (이 활성 슬롯엔 바인딩 없음·다음 레이어로)
    return None


def _active_slot_path(home, session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)의 절대 경로 (없으면 None).

    분리된 PM 홈(코드 없음)+worktree 모델에서 **PM 홈이 실행하는 코드 트리 작업**(leased ≥2 회귀
    순회·릴리즈 라이브 게이트)은 활성 repo 의 worktree cwd 에서 돌아야 한다 — 이 함수가 그 경로를
    lease 장부에서 해소한다. (push 게이트 회귀는 push 되는 트리 자신이 돈다 — `_regression_cwd`.)

    `_active_slot_test_cmd` 와 *동형* 데이터-결합: **worktree_pool 을 import 하지 않고**
    리스 장부 파일(`pm_paths.leases_file(home)`)을 stdlib json 으로 직접 read 한다.
    slot 식별자는 `work/` 접두를 이미 포함(`work/<repo>_<N>`)하므로 worktree_pool 의
    `slot_path()`(= `REPO / slot`)와 동일하게 import 없이 `home.root / lease["slot"]` 로 직접 구성한다.
    리스는 worktree_pool 이 atomic-replace 로 쓰므로 락 없는 point-read 가 일관 스냅샷을 본다.

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회가 슬롯별 cwd 를 뽑을 때 쓴다. 그 행의
    `slot` 을 `home.root / slot` 절대경로로 반환. 장부 부재/파싱실패/매칭없음/빈 slot → None
    (fail-soft — 호출부가 다음 레이어 `home.root` 로 폴백).
    """
    if not pm_paths.leases_file(home).exists():
        return None
    try:
        data = json.loads(fs.read_text_shared(pm_paths.leases_file(home), encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 장부 손상 = fail-soft (_active_slot_test_cmd 와 동일 가드): 유효 JSON 이라도
    # dict/list 가 아니면 None 폴백(회귀해소를 깨지 않게).
    if not isinstance(data, dict):
        return None
    leases = data.get("leases", [])
    if not isinstance(leases, list):
        return None
    sess = session if session is not None else lease_store.session_name(home)
    for row in leases:
        if not isinstance(row, dict):
            continue
        # `and sess` 방어가드 — sess None 시 손상 행 false-match 방지.
        if sess and row.get("session") == sess and row.get("state") == "leased":
            slot = row.get("slot")
            if not slot:
                return None  # 빈/None slot → None (다음 레이어 `home.root` 로)
            return str(home.root / slot)
    return None


def _test_cmd(home, override: str | None, session: str | None = None) -> str:
    """회귀에 쓸 테스트 명령을 해소한다 (per-repo + per-slot).

    우선순위:
      1. `override` (CLI `--cmd`).
      2. **활성 슬롯(lease)의 test_cmd** — worktree 리스 장부에서 이 세션의 leased 슬롯에
         바인딩된 명령(`_active_slot_test_cmd(session)`). 같은 repo 슬롯별 빌드변형을 수용한다
         `session` 명시는 M>1 슬롯 순회가 슬롯별로
         호출할 때 쓴다(무명시는 `session_name()` 해소). 장부 부재/매칭없음/빈 값이면 다음
         레이어로 폴백.
      3. **등록 repo 1개의 areas.md test_cmd** — 멀티-PM 모드(areas.md 존재)에서
         `id_prefix(None)`(등록 prefix 가 정확히 1개일 때만)의 레지스트리 행에 비어 있지 않은
         `test_cmd` 가 있으면 그것. 세션 이름에서 repo 를 떼어 행을 고르지 않는다 — 슬롯별
         명령은 2층(대여 행 test_cmd)이 준다.
      4. **최종 폴백** — 위 전부 미스면 현 단일 `local.conf test.cmd`.
         없으면 실행 가능한 Python으로 `pytest tests/ -q`를 만들고 pytest-xdist를 import할 수
         있는 환경에서는 `-n auto`를 붙인다.
    """
    if override:
        return override
    slot_cmd = _active_slot_test_cmd(home, session)
    if slot_cmd:
        return slot_cmd
    prefix = areas_store.id_prefix(home, None)
    if prefix:
        row = areas_store._areas_row_for_prefix(home, prefix)
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    return conf_store.local_config(home).get("test.cmd") or python_env.default_pytest_cmd()


def _regression_cwd(home, override: str | None = None) -> str:
    """회귀를 실행할 작업 디렉토리 — 명시 `--cwd` 가 있으면 그것, 없으면 **이 트리**(`REPO`).

    회귀 단위는 push 되는 코드 트리 자신이다. 종전엔 여기서 활성 슬롯 worktree 로 우회했는데,
    그것은 "PM 홈엔 `tests/` 가 없다"는 증상을 cwd 로 땜질한 것이라 코드가 한 줄도 안 바뀐 PM 홈
    push 가 슬롯 스위트 전량을 요구했다(채택자 제보). 그 판정은 이제 훅 본문이 push 시점에
    직접 한다(`pre_push_hook_body` 의 `[ -d tests ]`) — 스위트가 없는 트리는 회귀를 아예 요구하지
    않으므로 우회할 대상이 없다.

    슬롯 트리를 겨냥해야 하는 형상은 각자 자기 자리에서 해소한다: leased ≥2 순회는
    `_regression_multi_run`/`_regression_multi_check` 가 `_active_slot_path` 로, 릴리즈 라이브
    게이트는 `_livegate_cwd` 로, 다른 트리를 지목하는 실행은 `--cwd` 로 명시한다.
    """
    return override or str(home.root)


def _ticket_touches(home, tid: str) -> list[str] | None:
    """회귀 스코프용 티켓 touches — **canonical ID 정확 일치**만 · 확정 실패면 None.

    조회 규칙은 강등 판정(`_gate_ticket`)과 같은 한 지점이다 — 판정은 정확 일치로 하고 스코프는
    glob 첫 매칭으로 읽으면 두 표면이 서로 다른 티켓을 볼 수 있다(무관 touches 로 좁혀진 '가짜
    green'). 티켓을 못 찾으면 빈 목록이고, 호출부는 그때 FULL 로 남긴다(좁히는 방향으로는 안
    틀린다).

    반환 None 은 **스코프를 확정하지 못했다**는 뜻이다 — 손상/비-매핑 frontmatter
    (`load_ticket_soft` 규약으로 skip)와 형식 불명 touches(`_normalized_touches`)가 같은 축이고,
    호출부는 둘 다 FULL 로 남긴다."""
    found = _gate_ticket(home, tid)
    if found is None:
        return []
    loaded = ticket_store.load_ticket_soft(home, found[1])
    if loaded is None:
        return None
    return ticket_rules._normalized_touches(loaded[0].get("touches"))


def _scope_args(touches: list[str]) -> str:
    """touches → pytest -k 선택식 (파일 stem 기반). 비면 '' (스코프 없음 = full)."""
    stems = sorted({Path(t).stem for t in touches
                    if t.strip() and Path(t).stem not in ("", "__init__")})
    return f'-k "{" or ".join(stems)}"' if stems else ""


# ── 회귀 스테이징 (활성 리뷰 사이클 중 full → targeted 강등) ──────────────────
# 리뷰 라운드마다 FULL 회귀를 도는 것이 라운드 비용의 세 번째 축이었다(실측: 라운드당 full).
# 리뷰가 아직 닫히지 않은 티켓의 FULL 요청은 그 티켓 touches 로 강등한다 — 라운드 중에 필요한
# 신호는 "내가 건드린 것이 깨졌나"고, 스위트 전체 green 은 **수렴 후 한 번**이면 된다.
#
# 판정 입력은 additional_reviewer 의 라운드 장부(`.local/review_rounds.json`)뿐이다. 장부를 읽는
# 규칙은 `review_rounds` 가 소유하고 여기서는 **읽기만** 한다.
def _gate_ticket(home, gate: str) -> tuple[str, Path] | None:
    """게이트 이름이 가리키는 티켓의 (status, path) — **canonical ID 정확 일치**만 (아니면 None).

    게이트 이름은 자유 문자열이 실사용이다(장부 실측 `wave4-b1`). 그 문자열을 그대로 조회에
    넘기면 두 가지가 샌다:

      (a) `{이름}-*.md` **prefix glob 의 첫 매칭**은 legacy ID(`T-NNNN`)를 같은 번호로 시작하는
         prefixed 티켓(`T-NNNN-001-<slug>.md`)에 오인 매칭시킨다 — 무관한 티켓의 touches 로 FULL
         회귀가 targeted 강등되면 그 실행은 '가짜 green'이다. 그래서 조회는 공용 정확-일치
         seam(`find_ticket_exact`)만 쓴다(폴백 없음 — 여긴 못 찾으면 FULL 이 안전한 축이다).
      (b) 애초에 티켓 ID 문법(`_is_valid_ticket_id`)이 아닌 라벨은 조회 자체를 생략한다 — glob
         메타(`*`·`?`·`[`)가 든 이름이 보드를 훑는 것도, 없는 티켓을 매번 찾는 것도 뜻이 없다.
    """
    if not ticket_rules._is_valid_ticket_id(gate):
        return None
    try:
        return ticket_store.find_ticket_exact(home, gate)
    except OSError:
        return None


def _gate_ticket_is_claimed(home, gate: str) -> bool:
    """그 게이트 이름이 **현재 claimed 인 티켓**인가 (아니면·못 찾으면 False).

    라운드 장부는 append-only 라 자체 마감 이벤트가 없다 — 반려로 끝난 라운드는 티켓이 done 이
    된 뒤에도 장부에 그대로 남는다(실측: 마감된 티켓 10건이 '활성'). 마감 신호를 장부에 새로
    쓰는 대신 **이미 있는 데이터**(보드 티켓 status)를 대조한다: 리뷰 사이클은 그 티켓이 작업 중
    (claimed)일 때만 살아 있고, done/open/blocked/draft 는 정의상 라운드를 더 돌지 않는다.
    """
    found = _gate_ticket(home, gate)
    return found is not None and found[0] == "claimed"


def _active_review_gates(home) -> list[str]:
    """리뷰 사이클이 아직 안 닫힌 게이트 이름 (장부 부재/손상이면 빈 목록·fail-soft).

    **미마감 = (그 티켓이 현재 claimed) ∧ (마지막 라운드가 통과 rc 0 이 아님).** 두 조건을 함께
    쓰는 이유는 각자 못 잡는 게 있어서다: 장부만 보면 done 티켓의 옛 반려가 영원히 강등을 유발하고
    (마감 이벤트 부재), status 만 보면 이미 수렴한 claimed 티켓까지 좁힌다. 장부를 못 읽거나 티켓을
    못 찾으면 강등하지 않는다 — 이 축의 실패가 회귀 게이트를 좁히면 안 된다(fail-open 이 안전한
    방향이다)."""
    try:
        data = json.loads(fs.read_text_shared(review_rounds._review_rounds_ledger(home), encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    if not isinstance(data, dict):
        return []
    active: list[str] = []
    for gate, entry in sorted(data.items()):
        if gate == review_rounds._REVIEW_LEDGER_WAVE_KEY or not isinstance(entry, dict):
            continue
        verdict = review_rounds._last_round_verdict(entry)
        if verdict is not None and verdict != 0 and _gate_ticket_is_claimed(home, gate):
            active.append(gate)
    return active


def _downgrade_cancelled(gate: str, reason: str) -> tuple[list[str], list[str]]:
    """강등 전체 취소 — 미해소 게이트를 식별하는 사유 1줄 + 빈 결과(호출부가 FULL 로 남긴다)."""
    print(f"regression: 게이트 {gate} 의 {reason} — 활성 리뷰 사이클 강등을 취소하고 FULL 로 "
          "실행합니다 (부분 강등은 미해소 게이트 범위를 한 번도 돌지 않는 '가짜 green').",
          file=sys.stderr)
    return [], []


def _review_cycle_downgrade(home) -> tuple[list[str], list[str]]:
    """활성 리뷰 사이클의 (게이트 목록, 그 티켓들의 touches 합집합).

    touches 를 못 얻으면(자유 문자열 게이트·ticket 부재) 강등하지 않는다 — 스코프를 모르는 채
    좁히면 아무 테스트도 안 도는 '가짜 green'이 된다.

    **확정은 전-게이트 단위다** — 활성 게이트 중 하나라도 스코프를 확정하지 못하면 나머지
    touches 로 부분 강등하지 않고 강등 자체를 취소한다. 부분 강등은 미해소 게이트가 건드린
    범위를 한 번도 돌지 않은 실행에 rc0 을 주는데, 그 green 은 활성 게이트 **전부**에 대한
    통과로 읽힌다(false-green). 확정 실패는 세 가지고 전부 같은 취급이다: 읽기/파싱 실패(손상
    frontmatter)·빈 touches(선언 없음)·티켓 미해소(`_ticket_touches` 가 빈 목록).
    손상 티켓 한 건이 `regression run` 을 크래시시키지는 않는다 — 사유 1줄을 내고 FULL 로
    진행한다(넓히는 방향이라 안전하다)."""
    gates: list[str] = []
    touches: list[str] = []
    for gate in _active_review_gates(home):
        try:
            gate_touches = _ticket_touches(home, gate)
        except Exception as exc:  # noqa: BLE001 — 손상 티켓 1건이 회귀 실행을 막지 않는다.
            return _downgrade_cancelled(
                gate, f"touches 를 읽지 못했습니다 ({type(exc).__name__}: {exc})")
        if gate_touches is None:
            return _downgrade_cancelled(
                gate, "touches 형식을 확정하지 못했습니다 (리스트[str] 아님·손상 frontmatter)")
        if not gate_touches:
            return _downgrade_cancelled(
                gate, "touches 를 확정하지 못했습니다 (빈 선언·티켓 미해소)")
        gates.append(gate)
        for touch in gate_touches:
            if touch not in touches:
                touches.append(touch)
    return (gates, touches) if touches else ([], [])


def _quarantine_args(home) -> str:
    """quarantine.txt(있으면)의 test node id 를 --deselect 로. flaky 격리 (full 게이트 보호)."""
    q = home.root / ".project_manager" / "quarantine.txt"
    if not q.exists():
        return ""
    ids = [ln.strip() for ln in fs.read_text_shared(q, encoding="utf-8").splitlines()
           if ln.strip() and not ln.startswith("#")]
    return " ".join(f"--deselect {i}" for i in ids)


PM_QA_RESULT_PREFIX = "PM_QA_RESULT_V1="
_PLATFORM_CELL_KEYS = {
    "name", "command", "head", "status", "rc", "collected", "ts",
}
_PLATFORM_REGRESSION_KEYS = {
    "head", "status", "rc", "scope", "collected", "previous_collected",
    "conf_anchor", "ts", "platforms",
}
_PLATFORM_LIVEGATE_KEYS = {
    "head", "status", "n", "rc", "scope", "collected", "previous_collected",
    "conf_anchor", "ts", "platforms",
}


class PlatformConfigError(RuntimeError):
    """platform local.conf를 판독·검증할 수 없음 — CLI 경계에서 한 줄 fail-closed로 번역."""


def _platform_test_commands(home, cwd: str) -> tuple[tuple[str, str], ...]:
    """그 실행 트리의 선언 platform command를 공용 local.conf seam으로 해소한다."""
    try:
        values = conf_store.local_config(home, Path(cwd))
        return conf_schema.platform_test_commands(values)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PlatformConfigError(str(exc)) from exc
    except RuntimeError as exc:
        # 구표기 키는 공용 로더의 RuntimeError 서브클래스다. 다른 RuntimeError까지 사용자 설정
        # 오류로 축약하면 진단을 잃으므로 정확한 클래스만 번역한다.
        if type(exc).__name__ != "LegacyConfKeyError":
            raise
        raise PlatformConfigError(str(exc)) from exc


def _platform_marker(stdout: str) -> tuple[dict | None, str | None]:
    """stdout의 exact versioned marker 하나를 파싱한다. 없거나 복수/손상이면 사유를 돌린다."""
    markers = [line for line in (stdout or "").splitlines()
               if line.startswith(PM_QA_RESULT_PREFIX)]
    if len(markers) != 1:
        return None, f"marker count={len(markers)} (exactly 1 required)"
    def reject_duplicate_members(pairs):
        payload = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate member: {key}")
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            markers[0][len(PM_QA_RESULT_PREFIX):],
            object_pairs_hook=reject_duplicate_members,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        return None, f"marker JSON invalid: {message}"
    if not isinstance(payload, dict):
        return None, "marker payload is not an object"
    expected = {"platform", "head", "status", "collected"}
    if set(payload) != expected:
        return None, f"marker key set mismatch: {sorted(payload)}"
    return payload, None


def _tee_stream(stream, echo: Callable[[str], None]) -> str:
    """자식 스트림을 줄 단위로 **실시간 echo 하면서 동시에** 전체를 모아 돌려준다.

    수집 증감 표시는 pytest 요약행을 읽어야 해서 출력을 버퍼에 담아야 하는데, 캡처만 하면
    `git push` 회귀(pre-push 훅)가 수 분 동안 **완전 무출력**으로 멈춰 있는 것처럼 보인다
    (실측 367s). tee 로 진행을 그대로 흘리면서 파싱용 버퍼를 유지해 둘 다 만족한다.
    스트림 미개방(None)은 빈 문자열.
    """
    if stream is None:
        return ""
    chunks: list[str] = []
    for line in stream:
        chunks.append(line)
        echo(line)
    return "".join(chunks)


def _run_regression_cmd(cmd: str, cwd: str,
                        env: dict[str, str]) -> tuple[int, str, str]:
    """회귀 test_cmd 를 실행하고 `(rc, stdout, stderr)` 를 돌려준다 — run 두 경로 공용 단일 지점.

    출력을 버퍼에 담는 이유는 수집 증감 표시가 pytest 요약행을 읽어야 하기 때문이다(별도
    `--co -q` 재실행 대신 *이미 돈* 실행의 요약을 쓴다·자식 1회). 동시에 `_tee_stream` 으로
    실시간 echo 해 캡처 이전의 가시성을 그대로 유지한다(무출력 대기 근절).

    **두 스트림을 분리해 돌려준다** — 요약행 파싱은 **stdout 단독**이어야 한다. pytest 요약은
    stdout 에만 나오므로, stderr 에 섞인 로그(`3 passed …`)를 합치면 그게 "마지막 요약행"으로
    뽑혀 수집수가 오염된다(false-green/false-RED 양방향). 재출력·진단은 두 스트림 모두 쓴다.
    스트림 읽기는 스레드 2개로 동시에 — 한쪽만 읽으면 파이프가 차서 자식이 멈춘다(교착).
    """
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    captured: dict[str, str] = {}

    def _pump(name: str, stream, echo: Callable[[str], None]) -> None:
        captured[name] = _tee_stream(stream, echo)

    pumps = [
        threading.Thread(target=_pump, daemon=True, args=(
            "stdout", proc.stdout, lambda line: print(line, end="", flush=True))),
        threading.Thread(target=_pump, daemon=True, args=(
            "stderr", proc.stderr, lambda line: print(line, end="", flush=True,
                                                      file=sys.stderr))),
    ]
    for pump in pumps:
        pump.start()
    rc = proc.wait()
    for pump in pumps:
        pump.join()
    return rc, captured.get("stdout", ""), captured.get("stderr", "")


def _run_platform_cells(commands: tuple[tuple[str, str], ...], cwd: str,
                        expected_head: str,
                        env: dict[str, str], *, core_passed: bool,
                        head_after_core: str) -> tuple[list[dict], bool, int | str]:
    """선언 platform을 순서대로 실행하고 ordered record cell과 aggregate 판정을 돌린다."""
    cells: list[dict] = []
    aggregate_pass = core_passed and bool(expected_head) and head_after_core == expected_head
    aggregate_rc: int | str = 0
    if not aggregate_pass:
        reason = "core-red" if not core_passed else "head-drift"
        for name, command in commands:
            cells.append({
                "name": name, "command": command, "head": expected_head,
                "status": "not-run", "rc": reason, "collected": None,
                "ts": clock.now_utc(),
            })
            print(f"regression platform[{name}]: not-run (rc={reason})")
        return cells, False, reason

    for index, (name, command) in enumerate(commands):
        print(f"regression platform[{name}]: $ {command}  (cwd={cwd})")
        cell_env = {
            **env,
            "PM_QA_PLATFORM": name,
            "PM_QA_EXPECTED_HEAD": expected_head,
        }
        rc, stdout, _stderr = _run_regression_cmd(command, cwd, cell_env)
        payload, marker_problem = _platform_marker(stdout)
        host_head = git._git_head_at(cwd)
        collected = payload.get("collected") if payload is not None else None
        valid_collected = (
            isinstance(collected, int) and not isinstance(collected, bool)
            and collected > 0
        )
        passed = (
            aggregate_pass and rc == 0 and marker_problem is None
            and payload.get("platform") == name
            and payload.get("head") == expected_head
            and payload.get("status") == "pass"
            and valid_collected
            and host_head == expected_head
        )
        if passed:
            recorded_rc: int | str = 0
            status = "pass"
            note = ""
        else:
            status = "fail"
            if rc != 0:
                recorded_rc = rc
                note = f"command rc={rc}"
            elif host_head != expected_head:
                recorded_rc = "head-drift"
                note = f"host HEAD {host_head[:8] or '?'} != {expected_head[:8]}"
            elif marker_problem is not None:
                recorded_rc = "invalid-result"
                note = marker_problem
            elif payload.get("platform") != name:
                recorded_rc = "platform-mismatch"
                note = f"marker platform={payload.get('platform')!r}"
            elif payload.get("head") != expected_head:
                recorded_rc = "head-mismatch"
                note = f"marker head={payload.get('head')!r}"
            elif payload.get("status") != "pass":
                recorded_rc = "status-not-pass"
                note = f"marker status={payload.get('status')!r}"
            else:
                recorded_rc = "collection-invalid"
                note = f"marker collected={collected!r}"
            aggregate_pass = False
            aggregate_rc = recorded_rc
        cells.append({
            "name": name, "command": command,
            "head": payload.get("head") if payload is not None else "",
            "status": status, "rc": recorded_rc, "collected": collected,
            "ts": clock.now_utc(),
        })
        if passed:
            print(f"regression platform[{name}]: pass · collected {collected} "
                  f"· HEAD {expected_head[:8]} (rc=0)")
        else:
            print(f"regression platform[{name}]: {status} (rc={recorded_rc})"
                  + (f" — {note}" if note else ""))
        if not passed:
            for pending_name, pending_command in commands[index + 1:]:
                cells.append({
                    "name": pending_name, "command": pending_command,
                    "head": expected_head, "status": "not-run",
                    "rc": "previous-platform-red", "collected": None,
                    "ts": clock.now_utc(),
                })
                print(f"regression platform[{pending_name}]: not-run "
                      "(rc=previous-platform-red)")
            break
    return cells, aggregate_pass, aggregate_rc


def _platform_record_problem(data: dict, cwd: str, expected_head: str,
                             commands: tuple[tuple[str, str], ...], *,
                             session: str | None = None,
                             livegate: bool = False,
                             allow_unknown_collection: bool = False) -> str | None:
    """현재 선언과 저장된 FULL core/platform aggregate의 exact 정합을 검증한다."""
    expected_keys = set(_PLATFORM_LIVEGATE_KEYS if livegate else _PLATFORM_REGRESSION_KEYS)
    if session is not None or (livegate and "session" in data):
        expected_keys.add("session")
    if set(data) != expected_keys:
        return "aggregate schema mismatch"
    if data.get("status") != "pass":
        return "aggregate status mismatch"
    if data.get("head") != expected_head:
        return "aggregate head mismatch"
    if (not isinstance(data.get("rc"), int) or isinstance(data.get("rc"), bool)
            or data.get("rc") != 0):
        return "aggregate rc mismatch"
    if data.get("scope") != "full":
        return "aggregate scope mismatch"
    collected = data.get("collected")
    if not (allow_unknown_collection and collected is None) and (
            not isinstance(collected, int) or isinstance(collected, bool) or collected < 0):
        return "aggregate collected mismatch"
    previous = data.get("previous_collected")
    if previous is not None and (not isinstance(previous, int)
                                 or isinstance(previous, bool)):
        return "aggregate previous_collected mismatch"
    if not isinstance(data.get("ts"), str) or not data.get("ts"):
        return "aggregate timestamp missing"
    if session is not None and data.get("session") != session:
        return "aggregate session mismatch"
    if livegate and "session" in data and (
            not isinstance(data.get("session"), str) or not data.get("session")):
        return "aggregate session mismatch"
    if livegate and (
            not isinstance(data.get("n"), int) or isinstance(data.get("n"), bool)
            or data.get("n") != LIVEGATE_RELEASE_PIN):
        return "livegate collection mismatch"
    if data.get("conf_anchor") != os.path.abspath(cwd):
        return "conf_anchor mismatch"
    cells = data.get("platforms")
    if not isinstance(cells, list) or len(cells) != len(commands):
        return "platform list missing/count mismatch"
    for cell, (name, command) in zip(cells, commands):
        if not isinstance(cell, dict) or set(cell) != _PLATFORM_CELL_KEYS:
            return f"platform[{name}] schema mismatch"
        collected = cell.get("collected")
        if (
            cell.get("name") != name or cell.get("command") != command
            or cell.get("head") != expected_head or cell.get("status") != "pass"
            or not isinstance(cell.get("rc"), int) or isinstance(cell.get("rc"), bool)
            or cell.get("rc") != 0
            or not isinstance(collected, int) or isinstance(collected, bool)
            or collected <= 0
            or not isinstance(cell.get("ts"), str) or not cell.get("ts")
        ):
            return f"platform[{name}] evidence mismatch/red"
    return None


def _core_command_at(home, anchor: str, session: str | None = None, *,
                     local_dir: Path | None = None) -> str:
    """기록 좌표의 장부에서 실제 앵커 행을 선택하고 기존 명령 우선순위를 적용한다."""
    local_dir = local_dir or home.local_dir
    ledger_home = local_dir.parent.parent
    leases = local_dir / "worktree-leases.json"
    matches = []
    if leases.exists():
        try:
            data = json.loads(fs.read_text_shared(leases, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ""
        rows = data.get("leases", []) if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if (isinstance(row, dict) and row.get("state") == "leased"
                    and isinstance(row.get("slot"), str)
                    and os.path.abspath(ledger_home / row["slot"]) == anchor):
                matches.append(row)
    if len(matches) > 1:
        return ""
    if matches:
        row = matches[0]
        if session is not None and row.get("session") != session:
            return ""
        command = row.get("test_cmd")
        if not command:
            _header, rows = areas_store._parse_areas(home, ledger_home / ".project_manager" / "areas.md")
            matching = [r for r in rows if r.get("repo") == row.get("repo")]
            if len(matching) > 1:
                return ""
            # 실제 task 등록 행만 선택 repo 층을 사용한다. 세션 인자 부재는 task가 아니다.
            command = (matching[0].get("test_cmd") if matching and (
                row.get("session") in lease_store.task_names(leases)
                or matching[0].get("prefix")) else None)
        command = command or conf_store.local_config(home, ledger_home).get("test.cmd")
        return " ".join(p for p in (command or python_env.default_pytest_cmd(),
                                    _quarantine_args(home)) if p)
    command = (conf_store.local_config(home, Path(anchor)).get("test.cmd") if anchor != str(home.root)
               else None)
    return " ".join(p for p in (command or _test_cmd(home, None, session=session),
                                _quarantine_args(home)) if p)


# 선언 platform aggregate 는 "수집수는 정수" 라는 저장 계약을 갖는다 — 그 검증 불가는 여전히
# 전용 라벨의 fail 이다(하한과 무관한 축이라 증감 전환 뒤에도 남는다).
REGRESSION_RC_UNVERIFIED_COLLECTION = "unverified-collection"  # 수집수를 못 읽어 검증 불가


def _require_collected_count(rc: int, status: str, collected: int | None, *,
                             require_collected: bool = False,
                             ) -> tuple[str, int | str, str]:
    """선언 platform aggregate 의 "수집수는 정수" 저장 계약 — `(status, 기록 rc, 진단 note)`.

    선언 platform 은 cell 마다 수집수를 대조하므로 core 수집수를 못 읽으면 그 대조가 통째로
    성립하지 않는다. 그래서 rc0 이라도 파싱 실패면 `fail`(`unverified-collection`)로 기록한다 —
    경고만 내고 pass 로 기록하면 검증하지 못한 결과가 push 를 통과한다. 비-pytest `test_cmd`
    (요약행 규약 밖)로 platform 을 쓰려면 요약행 호환 러너가 필요하다(remedy 를 메시지에 싣는다).

    platform 미선언이면 no-op 이다 — 수집수는 증감 표시에만 쓰이고 아무것도 막지 않는다.
    """
    if rc != 0 or not require_collected or collected is not None:
        return status, rc, ""
    return ("fail", REGRESSION_RC_UNVERIFIED_COLLECTION,
            " · 수집수 파싱 실패 — 선언 platform aggregate 검증 불가"
            "(요약행 호환 test_cmd 필요)")


def _core_record_problem(home, data: dict, anchor: str, head: str, *,
                         session: str | None = None, local_dir: Path | None = None,
                         release: bool = False) -> str | None:
    """신규 flat core를 기존 exact schema 판정에 연결한다. 옛 aggregate는 재측정한다."""
    if "platforms" in data:
        return "old aggregate requires core rerun"
    if "command" not in data:
        if release:
            return "core command evidence missing"
        return None if data.get("status") == "pass" and data.get("head") == head else "core red/stale"
    command = data.get("command")
    if not isinstance(command, str) or not command:
        return "core command missing"
    if session is None and "session" in data:
        session = data["session"]
        if not isinstance(session, str) or not session:
            return "core session mismatch"
    if release:
        status, rc, _note = _require_collected_count(
            data.get("rc"), data.get("status"), data.get("collected"), require_collected=True)
        if status == "fail" and rc == REGRESSION_RC_UNVERIFIED_COLLECTION:
            return REGRESSION_RC_UNVERIFIED_COLLECTION
    normalized = {k: v for k, v in data.items() if k != "command"}
    normalized["platforms"] = []
    problem = _platform_record_problem(
        normalized, anchor, head, (), session=session,
        allow_unknown_collection=not release,
    )
    if problem is not None:
        return problem
    if release and (not isinstance(data["collected"], int)
                    or isinstance(data["collected"], bool) or data["collected"] <= 0):
        return "core collection invalid"
    if git._git_head_at(anchor) != head:
        return "core anchor head mismatch"
    if command != _core_command_at(home, anchor, session, local_dir=local_dir):
        return "core command changed"
    return None


def _regression_rc5_note(rc: int, cwd: str, override: str | None) -> str:
    """rc5(pytest 수집 0 · "no tests ran") 진단 힌트를 만든다 (rc≠5 면 '').

    "no tests ran"(exit 5)은 pass 로 기록하지 않는다 — 수집 0 은 테스트 루트/cwd 가 어긋났다는
    신호지 green 이 아니다. 나아가 그 트리에 `tests/` 자체가 없으면 **트리 사실**을 실어 처방까지
    준다: 회귀는 이 트리에서 돌고(회귀 단위 = push 되는 트리) 다른 트리는 명시로만 지목한다.
    (종전엔 여기서 '활성 slot lease 미매칭 — 세션 설정 확인'이라 말했다. 회귀 cwd 가 lease/세션을
    해소하던 시절의 문장이라, 우회가 삭제된 지금은 정상 바인딩된 세션에게도 거짓 처방이 된다.)
    `override`(명시 `--cwd`/`--task`)면 채택자가 트리를 확정한 것이라 힌트를 붙이지 않는다.
    """
    if rc != 5:
        return ""
    note = " · 수집 0 — 테스트 루트/cwd 확인"
    if not override and not (Path(cwd) / "tests").is_dir():
        note += (f" · 이 트리에 `tests/` 가 없다({cwd} · 분리 형상 PM 홈) — 코드 트리에서 "
                 "실행하거나 `--cwd <코드 트리 절대경로>`/`--task <task>` 으로 지목하라")
    return note


# ── 스위트 없는 트리의 회귀 요청 = 실행 전 거부 ────────────────────────────
# 판정 축은 훅 본문과 같다(그 트리에 `tests/` 가 있는가). 훅이 push 를 가리는 것과 대칭으로,
# 사람이 직접 부른 `regression run` 도 스위트가 없는 트리에서는 돌지 않는다 — 돌려봐야 홈 루트
# 재귀 수집(슬롯 스위트 오수집)이나 rc4/rc5 이고, 그 결과가 소비자 없는 홈 플래그에 red 로 남는다.
_SUITELESS_TREE_REFUSAL = (
    "regression: 이 트리엔 회귀 스위트가 없다 — {cwd} 에 `tests/` 가 없는데 test_cmd "
    "`{cmd}` 는 이 트리를 대상으로 삼는다({why}). 코드 트리에서 실행하거나 "
    "`--cwd <코드 트리 절대경로>` / `--task <task>` 으로 지목하라 (측정·기록 안 함)."
)
_SUITELESS_WHY_TARGETS_TESTS = "`tests/` 지목"
_SUITELESS_WHY_NO_PATH = "수집 경로 미지정 → cwd 재귀 수집"

# pytest 인자 중 **경로로 볼 수 있는 토큰**의 모양 — 옵션 값(`-n 8`·`-k expr`)을 경로로
# 오인하지 않으려고 세 신호만 본다: 경로 구분자 · `.py` · nodeid(`::`). 이 셋 다 아니면 실재
# 여부로 한 번 더 본다(`pytest pkg` 같은 디렉토리 인자).
_PYTEST_PATHLIKE_RE = re.compile(r"[/\\]|\.py$|::")


def _pytest_collect_paths(args: Sequence[str], cwd: str) -> bool:
    """pytest argv 가 **수집 경로를 하나라도 지정**하는지 (아니면 cwd 재귀 수집이다).

    옵션(`-` 로 시작)은 건너뛰고, 남은 토큰 중 경로 모양(`_PYTEST_PATHLIKE_RE`)이거나 그 트리에
    실재하는 것이 하나라도 있으면 True. 옵션 **값**(`-n 8` 의 `8`·`-k expr` 의 `expr`)은 셋 다
    아니라 걸러진다 — 값을 경로로 세면 "경로 미지정" 판정이 조용히 무력화된다.
    """
    for raw in args:
        if not raw or raw.startswith("-"):
            continue
        if _PYTEST_PATHLIKE_RE.search(raw) or (Path(cwd) / raw).exists():
            return True
    return False


def _suiteless_tree_refusal(cmd: str, cwd: str, override: str | None) -> str | None:
    """스위트가 없는 트리의 회귀 요청이면 거부 안내 1줄, 아니면 None (판정만·부작용 0).

    거부는 **세 조건이 모두** 참일 때다:
      1. 명시 `--cwd`/`--task` 가 **없다** — 명시면 채택자가 트리를 확정한 것이라 존중한다.
      2. test_cmd 가 pytest 호출이고(`_engine_invocation_details` — git-anchor 가드와 같은
         정규화) **이 트리를 대상으로 삼는다**. 둘 중 하나면 그렇다:
           · 상대 `tests/` 지목(`_pytest_targets_tests` — 같은 가드가 쓰는 판정), 또는
           · 수집 경로 미지정(`_pytest_collect_paths` False) — pytest 가 cwd 를 재귀 수집한다.
             엔진 기본 폴백 test_cmd(`pytest -q`)가 이 갈래다. 스위트 없는 홈에서 이 형상은
             슬롯 worktree 의 `work/<repo>_<N>/tests/**` 를 잘못된 rootdir 로 긁어 **FULL green
             기록**을 남겼다(리뷰 실측).
      3. 그 트리에 `tests/` 가 없다.
    스위트가 `tests/` 밖이면서 경로를 명시하는 커스텀 test_cmd(`pytest src -q` 등)와 비-pytest
    test_cmd 는 판정 대상이 아니다(무영향). 판정 불능(따옴표 불균형·pytest 아님)도 거부하지
    않는다 — 이 게이트는 fail-open 이다(실행을 막는 쪽이 아니라 헛도는 실행을 줄이는 쪽).
    """
    if override:
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    details = shell_policy._engine_invocation_details(cwd, argv)
    if details is None or details.kind != "pytest":
        return None
    if shell_policy._pytest_targets_tests(details.args):
        why = _SUITELESS_WHY_TARGETS_TESTS
    elif not _pytest_collect_paths(details.args, cwd):
        why = _SUITELESS_WHY_NO_PATH
    else:
        return None
    if (Path(cwd) / "tests").is_dir():
        return None
    return _SUITELESS_TREE_REFUSAL.format(cmd=cmd, cwd=cwd, why=why)


class _SlotRegressionState(NamedTuple):
    """슬롯 회귀 상태 판정 결과 — 상태 + 진단에 쓰는 기록값(rc·수집수)."""
    state: str                    # green | stale | red | missing
    rc: int | str | None
    collected: int | None


def _regression_slot_state(home, session: str, cwd: str) -> _SlotRegressionState:
    """슬롯별 회귀 플래그를 읽어 상태를 판정 — (`'green'|'stale'|'red'|'missing'`, rc, 수집수).

    all-or-nothing check-first 의 저비용 판정 (pytest 미실행): per-slot 플래그
    (`_regression_flag_for(session)`)를 읽고 그 슬롯 worktree HEAD(`_git_head_at(cwd)`·각 슬롯은
    독립 worktree·독립 commit)와 대조한다. green(HEAD 일치·pass)이면 재실행 skip, 그 외
    (stale=HEAD 불일치·red=fail·missing=기록없음/손상)는 run 대상. 손상 플래그는 missing 강등
    (fail-soft — 장부 손상이 회귀해소를 깨면 안 된다·`_regression_slot_state` 는 재실행을 유도).

    rc 는 기록값 그대로다 — 강등이면 전용 라벨(`unverified-collection`·정수 아님)이라 기록된
    수집수와 함께 라벨에 사유를 실을 수 있다.
    """
    base, mode = regression_store._resolve_regression_flag(home, cwd)
    if mode == regression_store._LG_BROKEN:
        return _SlotRegressionState("red", "broken-sidecar", None)
    flag = regression_store._regression_flag_for(home, session, local_dir=base.parent)
    if not flag.exists():
        return _SlotRegressionState("missing", None, None)
    try:
        data = json.loads(fs.read_text_shared(flag, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _SlotRegressionState("missing", None, None)
    if not isinstance(data, dict):
        return _SlotRegressionState("missing", None, None)
    collected = regression_store._flag_collected(data)
    current_head = git._git_head_at(cwd)
    if not current_head or data.get("head") != current_head:
        return _SlotRegressionState("stale", data.get("rc"), collected)
    if data.get("status") != "pass":
        return _SlotRegressionState("red", data.get("rc"), collected)
    if _core_record_problem(home, data, os.path.abspath(cwd), current_head,
                            session=session, local_dir=base.parent) is not None:
        return _SlotRegressionState("stale", data.get("rc"), collected)
    return _SlotRegressionState("green", data.get("rc"), collected)


def _slot_state_label(session: str, state: str, rc: int | str | None) -> str:
    """미검증 슬롯 라벨 — `<session>=<state>(rc=N·수집0)` (rc None 이면 상태만·codex sug).

    check 실패 메시지에 슬롯별 rc 를 실어 단일-슬롯 진단과 균질화한다 — 특히 rc5(수집 0)는
    테스트 루트/cwd 결함 신호이므로 힌트를 붙인다(`_regression_slot_state` 가 이미 rc 반환·저비용).
    선언 platform 의 수집수 검증 불가(`unverified-collection`)도 같은 자리에 사유를 붙인다.
    """
    if rc is None:
        return f"{session}={state}"
    if rc == REGRESSION_RC_UNVERIFIED_COLLECTION:
        hint = "·수집수 미확인"
    else:
        hint = "·수집0" if rc == 5 else ""
    return f"{session}={state}(rc={rc}{hint})"


# ── 릴리즈 라이브 게이트 ────────────────────────────────────────
# 라이브 LLM 검증(실 하네스 smoke)을 릴리즈(main 머지) 단일 지점으로 모은 게이트.
# `livegate record` 가 `pytest -m release` 를 슬롯 cwd 해소(`_livegate_cwd`)로 실행·측정하고
# (실행=기록·손기록 없음), 보호훅이 `livegate check --rev <sha>` 로 push HEAD 가 green 인지
# 소비한다. false-green 방어를 위해 rc0 만으로는 부족하고 수집 N==pin 을 함께 요구한다
# (수집 pin·rc5 vacuous-pass 근절의 원칙을 라이브 채널로 확장).
LIVEGATE_RELEASE_PIN = 22  # `pytest -m release` 로 돌아야 하는 라이브/사이클 케이스 수.


# ── 회귀 게이트 해소 (per-repo test_cmd) — 두 도구 공용 단일 사본 ─────────
#
# 회귀 게이트가 `pytest tests/ -q` 라는 보장은 없다. 채택자 repo 는 `go test ./...`·커스텀 CLI
# 일 수 있고 `tests/` 자체가 없을 수 있다. 해소를 areas.md **prefix** 칼럼으로만 하면 현행
# `pm-config repo add`(prefix 를 **빈 값**으로 등록 — prefix=작업 카테고리이지 repo 명이 아님)
# 로 등록한 채택자에서 `id_prefix()` 가 None 이라, 올바른 `test_cmd` 를 담은 행이 areas.md 에
# 실재하는데도 도달하지 못하고 하드코딩 pytest 로 폴백한다. 그 형상(=`repo add` 의 기본 등록
# 형상)에서 회귀는 **항상 red** 로 오판되고 핸드오프가 통째로 막힌다(→ `--no-pytest` 상시 우회
# → 진짜 red 를 놓친다).
#
# 이 절이 해소 체인의 **단일 사본**이다 — `pm_handoff`·`ticket_finish` 의 `_resolve_per_repo_test_cmd`
# 는 자기 트리의 `Home` 을 넘겨 `_resolve_gate_cmd(home)` 에 위임한다. 해소 함수가 한 도구에만
# 있던 미러 이탈이 이 결함의 절반이었다.

def _active_repo_name(home) -> str | None:
    """활성 repo 명 — 등록 repo 가 정확히 1개일 때만 그것.

    `areas_store.id_prefix` 해소 체인(count-based)과 **같은 규칙**을 repo 축에서 되풀이한다. 세션
    이름에서 repo 를 떼지 않는다. 2개 이상이면 어느 쪽인지 알 수 없으므로 None 이라 호출부가
    다음 층으로 내려간다(추측 금지).
    """
    repos = areas_store.registered_repos(home)
    if len(repos) == 1:
        return next(iter(repos))
    return None


def _areas_test_cmd_by_repo(home) -> str | None:
    """areas.md 활성 **repo** 행의 `test_cmd`. 미해소/미등록/빈 값 → None.

    prefix 칼럼이 **빈** 무prefix 형상을 위한 층이다. 행 매칭은 `_repo_base`·`_areas_git_url`
    과 동형이다 — `repo` 칼럼 일치 first-match.
    """
    repo = _active_repo_name(home)
    if not repo:
        return None
    _header, rows = areas_store._parse_areas(home)
    for row in rows:
        if row.get("repo") == repo and row.get("test_cmd"):
            return row["test_cmd"]
    return None


def _resolve_gate_cmd(home) -> str | None:
    """활성 회귀 게이트 명령(문자열)을 해소한다. 해소 실패면 None(기본 폴백).

    해소 체인 — 앞선 층이 비어 있지 않은 값을 주면 거기서 멈춘다:

      1. areas.md 활성 **prefix** 행의 `test_cmd`   (multi-repo 네임스페이스 형상)
      2. areas.md 활성 **repo** 행의 `test_cmd`     (prefix 칼럼이 빈 무prefix 형상)
      3. `local.conf` 의 `test.cmd`                 (per-clone 명시 설정)
      4. `python_env.default_pytest_cmd()` — xdist 가용 Python이면 `-n auto` 병렬 기본

    areas.md 존재 가드는 `pm_paths.areas_file`(board 루트 추종)에 위임한다 — board/ 분리 시
    areas.md 가 board/ 안(submodule)으로 옮겨가므로 legacy 위치를 보면 stale 이다. **3층은 그
    가드 밖**이다 — 채택자 진입문서가 게이트의 단일 진실로 선언하는 레이어라 areas.md 가 아예
    없는 형상에서도 읽혀야 한다.

    해소 중 예외는 삼켜 기본 폴백(None)으로 흘린다 — 게이트 해소 실패가 도구를 죽이지 않는다
    (현행 보존).
    """
    try:
        if pm_paths.areas_file(home).exists():
            # 1층 — 활성 prefix 행 (multi-repo 네임스페이스).
            prefix = areas_store.id_prefix(home)
            if prefix:
                row = areas_store._areas_row_for_prefix(home, prefix)
                if row and row.get("test_cmd"):
                    return row["test_cmd"]
            # 2층 — 활성 repo 행 (prefix 칼럼이 빈 무prefix 형상).
            by_repo = _areas_test_cmd_by_repo(home)
            if by_repo:
                return by_repo
        # 3층 — local.conf 명시 설정 (areas.md 부재 형상에서도 유효·가드 밖).
        conf_cmd = conf_store.local_config(home).get("test.cmd")
        if conf_cmd:
            return conf_cmd
    except Exception as exc:  # noqa: BLE001 — 해소 실패는 기본 폴백(현행 보존).
        # 구표기 conf 잔존은 삼키지 않는다 — 삼키면 채택자가 conf 에 적어 둔 게이트가 사라진 채
        # 기본 pytest 로 조용히 강등된다(중앙 로더를 fail-loud 로 만든 이유가 여기서 무효가 된다).
        if getattr(exc, "_legacy_conf_key", False):
            raise
        return None
    return python_env.default_pytest_cmd()
