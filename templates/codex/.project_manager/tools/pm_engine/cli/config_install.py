"""설정 facade 의 엔진 갱신·하네스 추가·설정 반영 진입 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import sys
from pm_engine.app import add_harness
from pm_engine.app import conf_apply
from pm_engine.app import harness_ctx
from pm_engine.app import repo_protection
from pm_engine.cli import config_models
from pm_engine.cli import config_status
from pm_engine.cli import update_cmd


def cmd_update(home, forward_args: list[str]) -> int:
    """`update [--from ...]` — 엔진 갱신 (pm-update 흡수).

    `cli.update_cmd.main(home, forward_args, prog="pm-config update")` 로 verbatim forward 한다 —
    갱신 CLI 규격의 단일 진실은 update_cmd 이고, 이 서브커맨드는 위임만 한다(중복 구현 0).
    forward_args 는 `update` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--from`·`--dry-run` 등 option-like 플래그를 디스패처가 가로채지 않게 `pm_config.main`
    이 (raw[0]=="update" special-case 로) argparse 를 우회해 이 핸들러로 raw 토큰을 넘긴다.
    usage 줄의 prog 는 값으로 넘긴다(에이전트가 칠 실 커맨드 `pm-config update` 와 정합).
    """
    return update_cmd.main(home, forward_args, prog=f"{repo_protection._FACADE_PROG} update")


def cmd_add_harness(
    home,
    args: argparse.Namespace,
    *,
    dest_root: Path | None = None,
) -> int:
    """`add-harness <harness> [--dry-run]` — 라이브 인스턴스에 두 번째 harness 어댑터 추가.

    `app.add_harness.add_harness_cli(home, dest, harness, dry_run=)` 로 verbatim 위임한다 — 복사
    스코프(어댑터 네임스페이스만)·비파괴 백업·토큰 치환은 add_harness 가, 인터페이스 예외의 친화
    번역(rc 1)은 그 main-style 래퍼 add_harness_cli 가 단일 진실로 소유한다(CLI 규격·
    로직/에러경계 중복 0). 실행 Home 은 호출자가 넘긴 명시 Home 이다. harness 는 choices 로 재검증하지
    않고 verbatim 으로 넘긴다 — 미지원 harness('both'/오타)는 add_harness_cli 가 ValueError→rc 1 로
    거른다(config 자체 검증 0).

    dest 는 명시 `dest_root` 이고, 없으면 *호출 시점*의 `home.root`(pm_config.py 가 사는 인스턴스 루트 —
    harness 를 추가할 라이브 인스턴스)다. 명시 dest 를 Home 으로 덮어쓰지 않는다.

    dest_root 주입으로 hermetic 테스트(실 복사 부작용 없이 위임 배선 검증).
    """
    dest = dest_root if dest_root is not None else home.root
    # --from(source_root) 은 optional — 생략 시 add_harness 가 dest local.conf upstream.path 에서 어댑터
    # 소스를 자동 해소한다(imported 인스턴스 갭). 기존 Namespace(테스트/구 호출)에 source 가
    # 없어도 getattr 로 안전하게 None 폴백(하위호환·verbatim forward).
    source_root = getattr(args, "source", None)
    return add_harness.add_harness_cli(
        home, dest, args.harness, dry_run=args.dry_run, source_root=source_root)


def cmd_apply(home, args, *, dest_root: Path | None = None) -> int:
    """`apply [--json] [--set 키=값 ...]` — 값 검사 → local.conf 기록 → 파생 파일 반영.

    설정 저장의 유일한 입구다(관리자 저장·[기본값으로] 포함). `--set` 이 있으면 바뀐 키가 걸리는 반영
    단계만, 없으면 모든 단계를 다시 돈다(손으로 고친 local.conf 를 반영할 때). 단계는 사용 한도(하네스
    설정 파일·압축 한도 기록)와 렌더 입력(`@render` 산출물을 설치 세대 원본으로 다시 그림) 둘이고,
    어느 묶음에도 없는 키는 local.conf 기록만 한다(`app.conf_apply.apply_conf`).

    rc 0 반영(바뀐 줄 0 포함) · 3 반영 전 거부(값 검사·모델 한도 조회·렌더 검사·렌더 원본 세대·옛 키 —
    local.conf·파생 파일 무변경 · 사유는 stderr) · 1 그 밖의 실패(local.conf 부재, 쓰기 단계 실패,
    처리하지 못한 예외 — 쓰기 단계에서 멈추면 stderr 가 교체한 파일을 적는다) · 2 인자 오류(argparse).
    `--json` 은 한 줄 `{"changed": local.conf 가 바뀌었나, "applied": 반영 줄}` 이다.
    dest 는 명시 `dest_root`, 없으면 호출 시점의 `home.root` 다."""
    dest = dest_root if dest_root is not None else home.root
    conf_file = dest / ".project_manager" / "local.conf"
    updates = dict(getattr(args, "set", None) or ())
    if updates and not conf_file.is_file():
        print(f"[중단] local.conf 없음 ({conf_file}) — 먼저 `pm-config init` 으로 셋업하라.",
              file=sys.stderr)
        return 1
    try:
        result = conf_apply.apply_conf(
            home, dest, updates={key: value or None for key, value in updates.items()},
            opencode_config_dir=harness_ctx.opencode_global_config_dir(os.environ, Path.home()))
    except conf_apply.ConfApplyError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1 if exc.write_stage else config_models._APPLY_REFUSED
    except RuntimeError as exc:
        if getattr(exc, "_engine_rev_skew", False):
            raise
        # 옛 키가 남은 conf(또는 `--set` 이 옛 키) — 값을 읽는 지점의 fail-loud 를 반영 전 거부로 낸다.
        if not getattr(exc, "_legacy_conf_key", False):
            raise
        print(f"[중단] {exc}", file=sys.stderr)
        return config_models._APPLY_REFUSED
    if getattr(args, "json", False):
        config_status._write_machine_line(json.dumps(
            {"changed": result.changed, "applied": list(result.applied)}, ensure_ascii=False))
        return 0
    if result.changed:
        print(f"✓ local.conf 기록: {', '.join(updates)}")
    for line in result.applied:
        print(f"✓ 반영: {line}")
    if not result.applied:
        print("파생 파일: 바뀐 줄 없음 (이미 local.conf 값과 같다).")
    return 0
