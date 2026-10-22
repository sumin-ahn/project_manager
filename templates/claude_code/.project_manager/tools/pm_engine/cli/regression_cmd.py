"""`board.py regression` 명령과 라이브 wave 실행 위치 (층: cli).

측정·기록(run)·확인(check), 슬롯 여럿의 순회를 실행한다. 해소 실패는 `[중단]` 문구로 종료한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pm_engine.domain import identity_args, pytest_report
from pm_engine.infra import clock, fs, git, lease_store, pre_push_hook, regression_store
from pm_engine.app import regression
from pm_engine.cli import task_args


# ── M>1 회귀 슬롯 해소 ────────────────────────────────────
# 훅은 `--task` 를 못 넘긴다(pre-push 훅 스크립트 무변경·check||run 체인). 명시/env/단일-lease
# 는 그 슬롯(현행 결과 동일)이지만, 활성 슬롯이 여럿(leased ≥2·무명시)이면 **어느 세션이 push 하든**
# 전 leased 슬롯이 green 이어야 한다 — check-first(저비용·기록 baseline)로 이미 green 인 슬롯의
# pytest 재실행을 억제하고 stale/red 만 run, 하나라도 red 면 push 차단
# nothing 철학과 동형). 슬롯별 회귀 플래그를 분리해(같은 `.local/` 공유) 결과가 서로 덮이지 않게 한다.

def _regression_run_slot(home, args: argparse.Namespace, session: str, cwd: str, *, workspace: identity_args.Workspace | None = None) -> int:
    """한 슬롯의 회귀를 pytest 로 측정·기록(슬롯별 플래그) — rc 반환.

    단일-슬롯 run 본체와 동형(같은 env·shell·인코딩·rc0 만 pass·rc5 vacuous 근절·수집 증감
    표시)이되, cwd/test_cmd/플래그/HEAD 를 슬롯별로 해소한다. 스코프(touches)는 훅 M>1 경로엔
    없으므로 full 만(플래그는 push 게이트 대상). 플래그 키는 그 슬롯 worktree HEAD 다
    (`_git_head_at`).

    conf 앵커 이어받기(`_inherit_flag_anchor`)는 여기 없다 — 슬롯 실행 위치는 리스 장부에서
    구조적으로 나오고(플래그도 그 세션 키), 장부가 슬롯을 옮겼다면 새 위치가 맞다.
    """
    label = f"{session}@{workspace.slot}" if workspace is not None else session
    cwd = os.path.abspath(cwd)
    base, mode = regression_store._resolve_regression_flag(home, cwd)
    if mode == regression_store._LG_BROKEN:
        print(f"regression[{label}]: 보호훅 sidecar 무효", file=sys.stderr)
        return 1
    flag = regression_store._regression_flag_for(home, session, local_dir=base.parent, workspace=workspace.slot if workspace is not None else None)
    cmd = " ".join(p for p in (regression._test_cmd(home, args.cmd, session=session, workspace=workspace), regression._quarantine_args(home)) if p)
    expected_head = git._git_head_at(cwd)
    if not expected_head:
        regression_store._write_regression_flag(flag, {
            "head": "", "status": "fail", "rc": "head-drift", "scope": "full",
            "collected": None, "conf_anchor": cwd, "command": cmd,
            "session": session, "ts": clock.now_utc(),
        })
        return 1
    print(f"regression[{label}]: $ {cmd}  (cwd={cwd})")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    rc, out, _err = regression._run_regression_cmd(cmd, cwd, env)
    collected = pytest_report._collected_count(out)
    status = "pass" if rc == 0 else "fail"
    recorded_rc = rc
    if git._git_head_at(cwd) != expected_head:
        status, recorded_rc = "fail", "head-drift"
    record = {
        "head": expected_head, "status": status, "rc": recorded_rc, "scope": "full",
        "collected": collected, "conf_anchor": cwd, "command": cmd,
        "session": session, "ts": clock.now_utc(),
    }
    regression_store._write_regression_flag(flag, record)
    note = " · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else ""
    print(f"regression[{label}]: {status} (rc={recorded_rc}{note})"
          f"{pytest_report.collection_delta_text(collected, record['previous_collected'])} "
          f"@ {expected_head[:8]}")
    return 0 if status == "pass" else (rc or 1)


def _regression_multi_check(home, workspaces: tuple[identity_args.Workspace, ...]) -> int:
    """M>1 check — 전 leased 슬롯이 green(HEAD 일치·pass)이어야 rc0 (all-or-nothing).

    미검증(stale/red/missing) 슬롯이 하나라도 있으면 rc1 로 push 를 차단하고 *어느 슬롯이
    어떤 상태(+rc)*인지 명시한다(디버깅 동선). pytest 는 안 돌린다(check = 저비용 baseline 검증).
    """
    not_green: list[str] = []
    for ws in workspaces:
        session, cwd = ws.session, str(home.root / ws.slot)
        label = f"{session}@{ws.slot}"
        slot_state = regression._regression_slot_state(home, session, cwd, workspace=ws)
        if slot_state.state != "green":
            not_green.append(
                regression._slot_state_label(label, slot_state.state, slot_state.rc))
    if not_green:
        print(f"regression(M={len(workspaces)}): 미검증 슬롯 [{', '.join(not_green)}] "
              "— `regression run` 필요 (push 차단).", file=sys.stderr)
        return 1
    print(f"regression(M={len(workspaces)}): 전 슬롯 green ✓ [{', '.join(f'{ws.session}@{ws.slot}' for ws in workspaces)}]")
    return 0


def _regression_multi_run(home, args: argparse.Namespace, workspaces: tuple[identity_args.Workspace, ...]) -> int:
    """M>1 run — 슬롯별 check-first(green skip)·stale/red 만 pytest·하나라도 red 면 rc1.

    all-or-nothing: 어느 세션이 push 하든 전
    leased 슬롯이 green 이어야 통과. check-first 로 이미 green(기록 baseline·HEAD 일치)인 슬롯의
    pytest 재실행을 억제(비용 억제)하고, 실패 슬롯(red)을 종합 메시지에 명시한다(디버깅 동선).
    """
    skipped: list[str] = []
    ran: list[str] = []
    red: list[str] = []
    for ws in workspaces:
        if args.cmd:
            ws = identity_args.Workspace(ws.slot, ws.repo, ws.session, args.cmd)
        session, cwd = ws.session, str(home.root / ws.slot)
        label = f"{session}@{ws.slot}"
        if regression._regression_slot_state(home, session, cwd, workspace=ws).state == "green":
            skipped.append(label)
            continue
        # stale/red/missing → 이 슬롯 pytest 실행·슬롯별 플래그 갱신.
        ran.append(label)
        if _regression_run_slot(home, args, session, cwd, workspace=ws) != 0:
            red.append(label)
    summary = (f"regression(M={len(workspaces)}): "
               f"skip(green) {len(skipped)} · run {len(ran)}")
    if red:
        print(f"{summary} · RED [{', '.join(red)}] — push 차단.", file=sys.stderr)
        return 1
    print(f"{summary} · 전 슬롯 green ✓")
    return 0


def cmd_regression(home, args: argparse.Namespace) -> int:
    """run = 측정+기록(HEAD 키), check = HEAD 가 green 인지 (pre-push 훅이 호출).

    M>1(leased ≥2) 홈은 전 leased 슬롯 all-or-nothing 으로 해소한다: 훅은
    --task 를 못 넘기므로 활성 슬롯이 여럿이면 슬롯별 check-first(저비용) 후 stale/red 만
    run 하며 하나라도 red 면 push 를 차단한다.

    **보호 게이트는 ambient env(PM_SESSION_NAME/CLAUDE_SESSION_NAME)로 좁혀지면 안 된다**
    (codex): 훅 프로세스가 env 세션을 상속하면 "어느 세션이 push 하든 전 leased 슬롯 green"이
    조용히 자기 슬롯 단일 경로로 우회된다. 그래서 M>1 디스패치 판정은 **CLI `--task` 명시**
    (문서화된 의도적 조작, 폴더는 위치 인자로 고름)만 단일-슬롯으로 좁히고 env 는 이 판정에서
    제외한다 — 단일-lease/명시는 현행 결과 동일. (env 는 단일-슬롯 threading 등 다른 해소엔 그대로
    유효.) `--task` 없는 `--repo`/`--slot` 은 run·check 모두 실행 명령 판정 함수가 거부한다.
    """
    # 구형 서명 훅 = 차단 — run/check 두 진입 모두에서 대조한다. 옛 세대 본문은 트리를 가리지
    # 않고 **항상** 회귀를 불렀으므로, 여기서 막으면 채택자가 릴리즈 노트를 읽지 않아도 옛 훅으로는
    # push 가 성립하지 않는다 — `tests/` 없는 PM 홈도 마찬가지다(처방은 `init` 재실행 1회).
    # 판정은 **어떤 부작용보다 앞**이다 — 회귀를 돌리고 기록까지 남긴 뒤 막으면, 그 기록이 다음
    # 실행의 green 재사용 입력이 된다.
    stale_hook = pre_push_hook._stale_pre_push_hook_refusal(home)
    if stale_hook is not None:
        print(stale_hook, file=sys.stderr)
        return 1
    # task-mode(`--task`) 실행 위치 — 특정 슬롯 worktree 절대경로를 cwd
    # 로 고정하고 슬롯 test_cmd 를 실어 잘못된 형제-슬롯 유도를 피한다. 절대경로를
    # surface 해 dev/git 짐작 여지를 없앤다(cwd 비참여). run 만 실행 위치가 필요하다.
    if args.action == "run" and getattr(args, "task", None):
        task_cwd, task_test_cmd = task_args._resolve_task_workspace_cwd(home, args, require_test_cmd=True)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        if getattr(args, "cmd", None) is None:
            # 슬롯을 **확정**했으므로 그 슬롯 test_cmd 를 직접 싣는다 — session 유도
            # (`_active_slot_test_cmd(<task>)`)는 같은 task 의 **형제 슬롯 첫-매칭**을 반환해 오매칭
            # 여지가 있다. 해소 함수가 해당 행과 선택 repo/local 기본값만 판독한다.
            args.cmd = task_test_cmd
        print(f"regression: 작업공간(task {args.task}) → {task_cwd}")
    # 디스패치 판정은 CLI --task(명시) 만 본다 — env 세션은 M>1 게이트를 조용히 좁히므로
    # 여기선 제외(위 docstring·codex). explicit 없고 leased ≥2 면 env 유무 무관 전-슬롯 순회.
    # 명시 `--cwd`(run)면 실행 위치가 그 경로로 핀돼(단일 위치) M>1 순회를 건너뛴다.
    _run_cwd_pinned = args.action == "run" and getattr(args, "cwd", None)
    explicit_override = task_args._execution_actor_session_override(home, args)
    if not _run_cwd_pinned and explicit_override is None:
        try:
            workspaces = regression._leased_regression_workspaces(home)
        except identity_args.WorkspaceResolutionError as exc:
            sys.exit(f"[중단] {exc}")
        if len(workspaces) >= 2:
            return (_regression_multi_run(home, args, workspaces) if args.action == "run"
                    else _regression_multi_check(home, workspaces))
    # 단일-슬롯 (명시 --task·단일-lease·명시 --cwd 핀) — 현행 경로. sess 는 슬롯
    # test_cmd/cwd threading 용(env 유효·위에서 M>1 만 걸러냄).
    sess = lease_store.session_name(home, explicit_override)
    if args.action == "run":
        # `--ticket` 스코프도 확정 실패(None·형식 불명·손상)는 빈 목록 = FULL 이다 — 확정 못 한
        # 선언으로 좁히면 그 실행의 rc0 이 아무 것도 검증하지 않은 '가짜 green' 이 된다.
        touches = ((regression._ticket_touches(home, args.ticket) or []) if getattr(args, "ticket", None)
                   else (args.touches.split(",") if getattr(args, "touches", None) else []))
        explicit_cwd = getattr(args, "cwd", None)
        cwd = os.path.abspath(regression._regression_cwd(home, explicit_cwd))
        implicit_full = not touches and not getattr(args, "final", False)
        if implicit_full:
            gates, cycle_touches = regression._review_cycle_downgrade(home)
            if cycle_touches:
                touches = cycle_touches
                print(f"regression: 활성 리뷰 사이클 [{', '.join(gates)}] — FULL 요청을 "
                      "touches targeted 로 강등합니다 (수렴 후 FULL 은 `regression run --final`).")
        scoped = bool(touches)
        if not scoped:
            regression_flag, mode = regression_store._resolve_regression_flag(home, cwd)
            if mode == regression_store._LG_BROKEN:
                print("regression: 보호훅 engine-root sidecar 무효", file=sys.stderr)
                return 1
            if not explicit_cwd:
                cwd = regression_store._inherit_flag_anchor(home, cwd, regression_flag)
                regression_flag, mode = regression_store._resolve_regression_flag(home, cwd)
                if mode == regression_store._LG_BROKEN:
                    print("regression: 보호훅 engine-root sidecar 무효", file=sys.stderr)
                    return 1
        parts = [regression._test_cmd(home, args.cmd, session=sess)]
        if scoped:
            parts.append(regression._scope_args(touches))
        parts.append(regression._quarantine_args(home))
        cmd = " ".join(p for p in parts if p)
        # shell=True 로 띄운 pytest 자식은 별도 프로세스 — 부모 콘솔 reconfigure 보호를
        # 못 받는다. 자식의 인코딩을 도구가 코드로 명시(env 워크어라운드 아님): 한국어
        # Windows(cp949 콘솔)에서도 자식 stdout/stderr·파일 IO 를 UTF-8 로 강제.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # cwd seam — 회귀는 이 트리(REPO)에서 돈다(슬롯 자동해소 없음·회귀 단위 = push 되는 트리).
        # `--cwd`/`--task` 주입 시 그 경로, 미주입이면 REPO. **절대경로로 정규화**해
        # 실행·기록·이후 conf 해소가 프로세스 cwd 에 흔들리지 않게 한다(상대 `--cwd` 방어).
        # 스위트 없는 트리 = 실행 전 거부. 판정은 **최종 cwd** 기준이고(앵커 승계 뒤), pytest 를
        # 띄우지도 플래그를 쓰지도 않는다 — 헛돈 결과가 게이트 기록으로 남으면 안 된다.
        suiteless = regression._suiteless_tree_refusal(cmd, cwd, explicit_cwd)
        if suiteless is not None:
            print(suiteless, file=sys.stderr)
            return 1
        # 실행 안내는 **거부 판정 뒤**다 — 돌지 않은 명령을 `$ …` 로 남기면 로그·핸드오프
        # 인용에서 실행한 것처럼 읽힌다(거부 문구가 스스로 '측정·기록 안 함'을 말하는 것과 같은 축).
        expected_head = git._git_head_at(cwd) if not scoped else ""
        if not scoped and not expected_head:
            regression_store._write_regression_flag(regression_flag, {
                "head": "", "status": "fail", "rc": "head-drift", "scope": "full",
                "collected": None, "conf_anchor": cwd, "command": cmd, "ts": clock.now_utc(),
            })
            print("regression: FULL 시작 HEAD 해소 실패", file=sys.stderr)
            return 1
        print(f"regression: $ {cmd}")
        rc, out, _err = regression._run_regression_cmd(cmd, cwd, env)
        # pass = rc0 한정. pytest rc5(수집 0·"no tests ran")는 fail — 수집 0 은 green 이
        # 아니라 테스트 루트/cwd 결함이다.
        # 미해소 시 상시 vacuous green 이었다.
        status = "pass" if rc == 0 else "fail"
        collected = pytest_report._collected_count(out)     # 파싱은 stdout 단독(stderr 로그 오염 차단).
        rc5_note = regression._regression_rc5_note(rc, cwd, getattr(args, "cwd", None))
        if scoped:
            # 스코프 실행 = dev 빠른 피드백 (advisory). full 만 push 게이트 → 게이트 플래그 안 씀.
            # 수집 증감도 FULL 게이트 기록의 축이다 — 스코프는 touches 매칭분만 도는 게
            # 정상이라 FULL 기록과 견주면 상시 가짜 증감이 된다(기록을 쓰지도 읽지도 않는다).
            print(f"regression(scoped, {len(touches)} touches): {status} (rc={rc}{rc5_note}) "
                  "— dev 피드백 · push 게이트 아님")
            return 0 if status == "pass" else 1
        recorded_rc = rc
        if git._git_head_at(cwd) != expected_head:
            status, recorded_rc = "fail", "head-drift"
        record = {"head": expected_head, "status": status, "rc": recorded_rc, "scope": "full",
                  "collected": collected, "conf_anchor": cwd, "command": cmd, "ts": clock.now_utc()}
        regression_store._write_regression_flag(regression_flag, record)
        print(f"regression: {status} (rc={recorded_rc}{rc5_note})"
              f"{pytest_report.collection_delta_text(collected, record['previous_collected'])} "
              f"@ {expected_head[:8]}")
        return 0 if status == "pass" else 1
    # action == "check" — pre-push 게이트
    check_cwd = getattr(args, "cwd", None) or str(home.root)
    regression_flag, regression_flag_mode = regression_store._resolve_regression_flag(home, check_cwd)
    if regression_flag_mode == regression_store._LG_BROKEN:
        print("regression: platform 증거 좌표 해소 실패 — 보호훅 engine-root sidecar가 "
              "무효다 (push 차단).", file=sys.stderr)
        return 1
    if not regression_flag.exists():
        print("regression: 기록 없음 — `board.py regression run` 필요 (push 차단).",
              file=sys.stderr)
        return 1
    try:
        data = json.loads(fs.read_text_shared(regression_flag, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("regression: stale (회귀 기록 판독 실패) — core 재실행 필요.", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("regression: stale (회귀 기록 object 손상) — core 재실행 필요.", file=sys.stderr)
        return 1
    if "conf_anchor" not in data:
        # key 자체가 없던 legacy 기록만 board REPO 좌표로 폴백한다.
        anchor = str(home.root)
        head = git._git_head(home)
        if not head:
            print(f"regression: stale (legacy REPO {anchor}의 Git HEAD 해소 실패) "
                  "— 재실행 필요.", file=sys.stderr)
            return 1
    else:
        anchor = data.get("conf_anchor")
        if (
            not isinstance(anchor, str) or not anchor
            or not os.path.isabs(anchor) or not Path(anchor).is_dir()
        ):
            print("regression: stale (기록 conf_anchor가 유효한 절대 디렉토리가 아님) "
                  "— 재실행 필요.", file=sys.stderr)
            return 1
        head = git._git_head_at(anchor)
        if not head:
            print(f"regression: stale (기록 conf_anchor {anchor}의 Git HEAD 해소 실패) "
                  "— 재실행 필요.", file=sys.stderr)
            return 1
    if data.get("head") != head:
        print(f"regression: stale (기록 {str(data.get('head'))[:8]} ≠ HEAD {head[:8]}) "
              "— 재실행 필요.", file=sys.stderr)
        return 1
    if data.get("status") != "pass":
        rc = data.get("rc")
        extra = " · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else ""
        print(f"regression: RED @ {head[:8]} (rc={rc}){extra} — push 차단.", file=sys.stderr)
        return 1
    # 신규 core 기록은 명시 선택과 같은 작업공간에서 측정돼야 한다.
    # 인자 없는 훅과 명령 증거가 없던 legacy 기록의 기존 앵커 소비는 유지한다.
    if "command" in data:
        selected_cwd = getattr(args, "cwd", None)
        selected_slot = None
        if getattr(args, "task", None):
            selected_slot, _cmd = task_args._resolve_task_workspace_cwd(home, args, require_test_cmd=False)
        if (selected_slot is not None and os.path.abspath(home.root / selected_slot) != anchor
                or selected_cwd is not None and os.path.abspath(selected_cwd) != anchor):
            print("regression: stale (선택 작업공간과 core 기록 좌표 불일치) "
                  "— core 재실행 필요.", file=sys.stderr)
            return 1
    problem = regression._core_record_problem(home, data, anchor, head, local_dir=regression_flag.parent)
    if problem is not None:
        print(f"regression: stale ({problem}) — core 재실행 필요.", file=sys.stderr)
        return 1
    # green 줄에도 그 기록이 담은 직전 대비 증감을 붙인다 — 막지 않는 표시다(기록을 읽는
    # 자리이므로 같은 축의 수집수끼리 견준다).
    delta = pytest_report.collection_delta_text(regression_store._flag_collected(data), data.get("previous_collected"))
    print(f"regression: green @ {head[:8]}{delta} ✓")
    return 0


def _livegate_cwd(home, override: str | None = None, session: str | None = None) -> str:
    """라이브 wave(`pytest -m release`)를 돌릴 작업 디렉토리를 해소한다.

    회귀 cwd(`_regression_cwd`)와 갈리는 이유는 실행 주체가 다르기 때문이다 — 회귀는 push 되는
    트리 자신이 자기 스위트를 돌지만, 릴리즈 라이브 게이트는 **PM 홈에서 실행돼 코드가 사는 슬롯
    트리를 겨냥**한다. 그래서 슬롯 해소는 여기 남는다.

    해소 순서:
      - `override`(CLI `--cwd` — 릴리즈 readonly 슬롯 핀)가 있으면 그것,
      - 없으면 **활성 슬롯 경로**(`_active_slot_path(session)` — lease 장부에서 이 세션의 leased
        슬롯 worktree 경로·worktree_pool 미import·`session` 명시는 M>1 슬롯 지목용),
      - 활성 슬롯이 미해소면 **fail-loud**(`sys.exit`) — `--task <task>`/`--cwd <path>`
        명시를 안내한다.

    슬롯 하나만 쓰는 홈도 그 홈 자신이 슬롯 행이라 첫 층에서 해소된다(행의 `slot` 이 홈을
    가리킨다). 그래서 "행이 없으니 이 트리겠지"라는 `REPO` 침묵 폴백을 두지 않는다 — 그
    폴백은 코드가 없는 PM 홈을 라이브 wave 대상으로 만들어 broken slot 수집·false fail 을
    내던 자리다(livegate `--cwd` 우회의 근원). 모호(leased ≥2)와 미등록(행 0)은 같은 이유로
    같은 중단을 탄다.
    """
    if override:
        return override
    slot = regression._active_slot_path(home, session)
    if slot:
        return slot
    sys.exit(
        "[중단] livegate cwd 미해소 — 이 세션의 활성 슬롯을 특정할 수 없다(활성 슬롯이 "
        "여럿이거나 이 홈이 아직 슬롯 행으로 등록되지 않았다). PM 홈으로 침묵 폴백하면 "
        "tests 없는 트리에서 broken slot 을 수집해 false fail 이 되므로 거부한다. "
        "`--task <task>`(폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가)로 실행하거나 "
        f"`--cwd <worktree 절대경로>` 로 직접 지정하라. {identity_args.NO_WORK_FOLDER_REMEDY}"
    )
