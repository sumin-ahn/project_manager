"""engine.manifest 기반 배포 sync — upstream 엔진 경로만 덮어쓴다.

엔진/상태 분리의 managed-manifest 배포. 인스턴스 상태(tickets·status·log·
decisions/*.md·areas.md…)와 per-clone 로컬(board.md·pm_state·local.conf·.local)은
manifest 밖이라 절대 건드리지 않으므로, upstream 갱신이 인스턴스와 *구조적으로*
충돌하지 않는다 (수동 MERGE 백포트의 기계화).

사용:
    # 인스턴스/타깃 내부에서 실행 (self-location):
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> [--dry-run]
    # --from 생략 시 dest local.conf 의 upstream.path= 을 기본으로 쓴다(pm_import 가 자동 기록):
    python3 .project_manager/tools/pm_update.py [--dry-run]

    # 루트(upstream)에서 특정 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --target <name> [--dry-run]
    # 예: --target opencode  →  templates/opencode/ 에 동기화

    # 루트(upstream)에서 존재하는 모든 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --all-targets [--dry-run]

    # 명시 경로만 전파 (opt-in 부분 전파 · 단독/--target/--all-targets 와 조합):
    python3 .project_manager/tools/pm_update.py --all-targets --paths <path> [<path>...]

    # 받은 baseline ↔ upstream HEAD 변경점만 read-only 확인 (실 sync 안 함):
    python3 .project_manager/tools/pm_update.py --changes [--from <checkout>] [--count-only] [--log]

동작:
  자기 갱신은 두 프로세스다. 디스크에 있던 갱신기는 엔진 코드(.project_manager/tools/)만 옮기고,
  옮겨 받은 갱신기를 같은 인자로 새 프로세스에서 띄워 나머지(렌더·진입 doc 이관·훅·어댑터 설정·
  upstream.rev 기록)를 끝낸다. 종료 코드와 출력은 그 프로세스의 것이다. 엔진 코드가 바뀌는 갱신의
  --dry-run 은 받을 엔진 코드만 보여 주고, --paths 는 엔진 코드 변경이 남아 있으면 거부한다.
  engine.manifest 의 각 경로를 <upstream>/<path> → <dest-root>/<path> 로 복사(overwrite).
  디렉토리는 재귀. manifest 에 없는 경로는 무시. --dry-run = 변경 예정만 출력(미적용).
  --paths 지정 시 그 경로(파일 또는 디렉토리) 아래만 전파한다 — manifest 등재분에 한정하고
  미등재 경로는 rc1 로 거부한다. 부분 전파이므로 upstream.rev baseline 기록·진입 doc
  마이그레이션·보호 훅 재설치·동기 후 프롬프트는 발화하지 않는다(전량 흡수로 오인 방지).
  --target 지정 시 dest-root = REPO/templates/<target>/ (타깃 자신의 manifest 우선).
  sync 적용 후에는 등록 repo 전수 **보호 훅 재설치**— 훅은 엔진 코드에서 생성되는
  런타임 산출물이라 파일 복사만으론 새 훅이 배포되지 않는다(--target 은 비발화).

결정:
  - merge 아니라 overwrite (엔진은 upstream 단일 진실). 커스터마이즈 가능 문서는 manifest 에서
    제외 — 채택자 customization 은 local.conf(operational)·canonical home(free-form FILL)이 보존.
  - 어떤 경로를 엔진으로 볼지는 *dest-root 의* engine.manifest 가 정한다(없으면 source 의 것).
  - stdlib 만. plan/apply 분리로 테스트 결정론.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import sys
from pm_engine.app import conf_apply
from pm_engine.app import engine_transfer
from pm_engine.app import entry_doc_migration
from pm_engine.app import hook_sets
from pm_engine.app import update_adapters
from pm_engine.app import update_finish
from pm_engine.app import update_manifest
from pm_engine.app import update_plan
from pm_engine.app import update_render
from pm_engine.app import upstream_changes
from pm_engine.app import upstream_revisions
from pm_engine.cli import update_changes
from pm_engine.domain import manifest_rules
from pm_engine.infra import console
from pm_engine.infra import install_conf
from pm_engine.infra import repo_files
from pm_engine.infra import retired_paths
from pm_engine.infra import shipping_targets


def _main(home, argv: list[str] | None = None, *, prog: str = "pm_update.py") -> int:
    # 원본 인자 목록 — 완료 프로세스가 같은 인자로 뜬다(`pm_config.py update` 경유면 그 forward 인자).
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        prog=prog,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--from 생략 시 <dest>/.project_manager/local.conf 의 `upstream.path=` 값을 기본으로 쓴다 "
            "(pm_import 가 한 번 import 하면 자동 기록·--from 명시로 override 가능). "
            "upstream= 이 **URL**(릴리스 추적 기본)이면 <dest>/.project_manager/.local/"
            "upstream-cache/ 에 clone(최초)/fetch(이후)한 뒤 그 캐시를 소스로 쓴다 — "
            "clone/fetch 가 실패하면 옛 캐시로 진행하지 않고 명확한 에러로 멈춘다. "
            "upstream 미등록이거나 그 경로가 부재/디렉토리 아님이어도 명확한 에러로 멈춘다(침묵 폴백 없음)."
        ),
    )
    ap.add_argument("--from", dest="source", required=False, default=None,
                    help="upstream 프레임워크 checkout 경로 "
                         "(생략 시 local.conf 의 upstream.path= 사용)")
    ap.add_argument("--dry-run", action="store_true")
    target_group = ap.add_mutually_exclusive_group()
    target_group.add_argument(
        "--target",
        metavar="NAME",
        help=(
            "루트에서 templates/<NAME>/ 타깃으로 동기화. "
            "REPO/templates/<NAME>/ 디렉토리가 존재하면 유효. "
            "생략 시 self-location(스크립트 위치 기준 dest) 사용."
        ),
    )
    target_group.add_argument(
        "--all-targets",
        action="store_true",
        help=(
            "루트에서 templates/ 직계 하위의 존재하는 모든 타깃으로 동기화. "
            "새 타깃도 디렉토리만 있으면 자동 포함한다. --target 및 --changes 와 함께 쓸 수 없다."
        ),
    )
    ap.add_argument(
        "--paths",
        metavar="PATH",
        action="extend",
        nargs="+",
        default=None,
        help=(
            "명시한 경로만 전파한다(opt-in 부분 전파·반복 지정 가능). manifest 등재 경로만 "
            "허용하며 미등재 경로는 거부한다(rc1·조용한 무전파 없음). 엔진 코드"
            "(.project_manager/tools/) 변경이 남아 있으면 쓰기 전에 거부한다(rc1). 경로는 repo 루트 상대 "
            "(파일 또는 디렉토리)다. 전량 흡수가 아니므로 upstream.rev baseline 기록·진입 doc "
            "마이그레이션·보호 훅 재설치·동기 후 프롬프트는 건너뛴다(부분 전파를 '최신' 으로 "
            "박으면 drift-lint 가 거짓 침묵한다). --target·--all-targets 와 조합 가능하고 "
            "--changes 와는 함께 쓸 수 없다."
        ),
    )
    # ── read-only 변경점 확인 (실 sync 안 함) ──────────────
    ap.add_argument(
        "--changes",
        action="store_true",
        help=(
            "받은 upstream baseline(local.conf upstream.rev) ↔ 그 이후 upstream HEAD 변경점을 "
            "read-only 로 요약(실 sync 안 함). 엔진 영향(manifest 경로)/그 외 분리. "
            "URL upstream 은 같은 로컬 캐시(clone/fetch)를 소스로 쓴다."
        ),
    )
    ap.add_argument(
        "--count-only",
        action="store_true",
        help="--changes 와 함께: baseline..HEAD commit 개수 1줄만 출력(advisory/스크립트).",
    )
    ap.add_argument(
        "--log",
        action="store_true",
        help="--changes 와 함께: `git log --oneline baseline..HEAD` 커밋 목록을 꼬리에 출력.",
    )
    args = ap.parse_args(raw_argv)

    # ── --count-only/--log 는 --changes 전용 (codex suggestion 2·CLI 오사용 차단) ──
    #    --changes 없이 주면 일반 sync 가 돌면서 두 옵션이 조용히 무시된다 → 명확 에러로 멈춘다.
    #    --all-targets 분기보다 **앞**이어야 한다 — 뒤면 자식 argv 에 안 실리는 두 옵션이 조용히
    #    무시된 채 실 동기화가 돈다(오사용 검증이 모든 모드에 선행).
    if (args.count_only or args.log) and not args.changes:
        misused = []
        if args.count_only:
            misused.append("--count-only")
        if args.log:
            misused.append("--log")
        print(
            f"오류: {', '.join(misused)} 는 --changes 전용 옵션이다 — --changes 와 함께 쓰라 "
            "(read-only 변경점 확인 모드). 일반 sync 에는 무효.",
            file=sys.stderr,
        )
        return 1

    # ── `--paths` 경로 스코프 입구 검증 (부작용 전·--all-targets 분기보다 앞) ──
    #    나쁜 값(절대경로·`..`)과 모드 충돌은 어떤 복사/기록보다 먼저 거른다. --all-targets 는 이
    #    검증을 통과한 값을 자식 argv 로 그대로 넘긴다(자식이 다시 같은 검증을 탄다).
    scope_paths: list[str] = []
    if args.paths is not None:
        if args.changes:
            print("오류: --paths 는 실 동기화 옵션이며 --changes(read-only 확인)와 함께 쓸 수 없다.",
                  file=sys.stderr)
            return 1
        scope_paths, rejected = upstream_changes._normalize_scope_paths(args.paths)
        if rejected:
            for reason in rejected:
                print(f"  [거부] {reason}", file=sys.stderr)
            print("오류: --paths 값이 repo 루트 상대 경로가 아니다 — 위 항목을 고쳐 다시 실행하라.",
                  file=sys.stderr)
            return 1
        if not scope_paths:
            print("오류: --paths 에 유효한 경로가 없다(빈 스코프는 전파 0 이라 무의미).",
                  file=sys.stderr)
            return 1

    # 전체 export 는 타깃 집합을 디렉토리에서 매번 발견한다. 단일 타깃 실행을 재사용해
    # manifest/안전 가드/출력의 의미를 갈라놓지 않는다. 한 타깃의 실패는 즉시 반환한다.
    if args.all_targets:
        if args.changes:
            print("오류: --all-targets 는 실 동기화 옵션이며 --changes 와 함께 쓸 수 없다.", file=sys.stderr)
            return 1
        target_names = shipping_targets.discover_target_names(home)
        if not target_names:
            print("오류: templates/ 아래에 동기화할 타깃 디렉토리가 없다.", file=sys.stderr)
            return 1
        for target_name in target_names:
            child_argv = ["--target", target_name]
            if args.source:
                child_argv = ["--from", args.source, *child_argv]
            if args.dry_run:
                child_argv.append("--dry-run")
            if scope_paths:
                # 경로 스코프는 타깃마다 같은 값이다 — 정규화된 값을 넘겨 자식이 다시 같은 검증을
                #   통과하게 한다(타깃별로 스코프가 달라지는 경로 없음).
                child_argv.extend(["--paths", *scope_paths])
            rc = main(home, child_argv, prog=prog)
            if rc:
                return rc
        return 0

    # ── read-only 변경점 확인 — main 초입 early-return(실 sync 안 함).
    #    dest/source 해소는 _run_changes 안에서 sync 와 동일 경로(_resolve_dest_source)로 탄다.
    if args.changes:
        return update_changes._run_changes(home, args)

    # ── 자기 갱신은 두 프로세스다: 인계 값이 없으면 이 프로세스는 전달 프로세스(엔진 코드만 옮기고
    #    받은 갱신기를 띄운다), 있으면 완료 프로세스다. 내보내기(`--target`)는 실행 중인 코드를 바꾸지
    #    않으므로 한 프로세스로 끝난다.
    try:
        handoff = engine_transfer._take_completion_handoff()
    except engine_transfer.CompletionHandoffError as exc:
        print(f"[중단] 완료 단계 인계 값({engine_transfer.COMPLETION_ENV})이 올바르지 않다: {exc}", file=sys.stderr)
        return 1
    if handoff is None and not args.target:
        return engine_transfer._transfer_engine_code(home, args, raw_argv, scope_paths)
    if handoff is not None:
        # 완료 프로세스 — 상류를 다시 해소하지 않고(URL 재조회 없음·교체 안내는 전달 프로세스가 냈다)
        #   인계 값의 source 를 쓴다. 인계 대상이 이 갱신기의 위치와 다르면 쓰기 전에 멈춘다.
        rc, dest_root = upstream_changes._resolve_dest(home, args)
        if rc != 0:
            return rc
        effective_dest = dest_root if dest_root is not None else home.root
        if Path(handoff["dest"]).resolve() != effective_dest.resolve():
            print(f"[중단] 완료 단계 인계 값의 대상({handoff['dest']})이 이 갱신기의 위치"
                  f"({effective_dest})와 다르다.", file=sys.stderr)
            return 1
        source_root = Path(handoff["source"])
        if not upstream_changes._source_dir_exists(source_root, f"완료 단계 인계 값 {engine_transfer.COMPLETION_ENV}"):
            return 1
        transferred = handoff["transferred"]
    else:
        # 내보내기 — dest/source 해소(--target·--from·URL 캐시·stale 가드)는 --changes 와 공유한다.
        rc, dest_root, source_root = upstream_changes._resolve_dest_source(home, args)
        if rc != 0:
            return rc
        effective_dest = dest_root if dest_root is not None else home.root
        transferred = 0

    # ── guest 절 해소(읽기 전용) — 스코프 선검증·계획·기록이 **같은 산출**을 쓴다. 엔진 행은 upstream
    #    flavor 에서 매 실행 재파생하므로, 절에 아직 기록되지 않은 파생분도 이 시점에 확정된다.
    guest_entries = update_manifest._dest_guest_manifest_entries(effective_dest)
    guest_backfill, guest_backfill_flavors = update_manifest._guest_engine_backfill_entries(
        effective_dest, source_root, guest_entries)

    # ── `--paths` 소유권 선검증: **어떤 dest 쓰기보다 먼저**. 미등재 요청은
    #    rc1 로 끝나는데 그 전에 write 가 일어나면 "거부인데 파일이 바뀐다" 가 된다.
    #    판정은 읽기만 하고 관대하다(dest∪source manifest 합집합) — 좁은 판정은 계획 확정 뒤 한 번
    #    더 돈다. manifest 를 못 읽으면 선검증을 생략한다(정규 경로가 같은 오류를 더 정확히 낸다).
    if scope_paths:
        early_manifest = upstream_changes._scope_validation_manifest(effective_dest, source_root)
        if early_manifest is not None:
            # 파생 엔진 행은 **영속화 전**(첫 실행)엔 어느 파일에도 없다 — 합집합에 넣지 않으면
            #   이번 실행이 전파할 경로를 "미등재" 로 오거부한다(선검증만 파일을 읽는 비대칭).
            early_paths = {str(entry).replace("\\", "/") for entry in early_manifest}
            early_manifest = early_manifest + [
                entry for entry in guest_backfill
                if str(entry).replace("\\", "/") not in early_paths
            ]
            rc = upstream_changes._refuse_unregistered_scope_paths(
                early_manifest, scope_paths, effective_dest)
            if rc:
                return rc

    # ── manifest 자기치유 (self-update 2-pass) — upstream engine.manifest 를 이번 sync 의
    #    계획 기준으로 승격해, 로컬 manifest 가 구형이어도 신규 등재분이 한 번의 실행으로 plan→apply
    #    에 실린다(회사 실측: bare CLI 흡수가 신규 등재분 미도달). manifest 자신도 self-prop 엔트리
    #라 같은 plan 안에서 로컬 파일이 upstream 판으로 갱신된다(별도 write 불요). upstream
    #    manifest 부재/읽기 실패는 fail-soft(로컬 유지) — baseline 억제가 그 잔여 경로
    #    안전망. --target(엔진 export)은 타깃 manifest 가 루트와 의도적으로 달라 승격하지 않는다
    #    (현행·아래 skew 검출과 동일 경계). 승격 후 skew 는 정의상 0(manifest==upstream).
    # 계획이 훑은 출하 좌표는 두 판정이 함께 쓴다(그래서 스코프 유무와 무관하게 모은다):
    #   - 경로 스코프의 등재 디렉토리 **안의 오타**(`adapterdir/typo.md`) — 등재 검증은 통과하나
    #     대응 파일이 없다. 변경 0 은 "이미 최신" 과 구분되지 않으므로 인벤토리 대응으로 닫는다.
    #   - 은퇴 후보 판정 — 상류가 실제로 공급하는 파일을 재판정 없이 걸러낸다.
    plan_inventory: set[str] = set()
    # 은퇴 판정은 **dest 좌표만** 대조한다 — `--paths` 오타 검출용 합집합에는 상류 좌표가 섞여 있어,
    #   그대로 넘기면 우연한 문자열 일치로 잔존물이 "상류가 공급함" 으로 접힌다(false negative).
    plan_dest_inventory: set[str] = set()
    # ── 추론 값 정리(conf 이관) — **계획·렌더보다 먼저**. 그 모델이 받지 않는 opencode 추론 값이 남아
    #    있으면 렌더가 그 카드에서 멈추므로 여기서 지우고 알린다. 경계는 아래 키 이름 이관과 같다
    #    (`--target` 내보내기·`--paths` 부분 전파는 conf 를 건드리지 않는다). 돌려받은 conf 로 계획한다.
    conf = install_conf._read_local_conf(effective_dest / ".project_manager" / "local.conf")
    if not args.target and not scope_paths:
        try:
            conf = conf_apply.prune_unavailable_reasoning(effective_dest, conf, write=not args.dry_run)
        except conf_apply.ReasoningPruneError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
    # manifest 자기치유 → notation 템플릿 → guest backfill 기록 판정 → `plan()` 을 한 함수로 묶어
    #   호출한다 — self-update/`--target` export 경로와 drift 게이트(`drift_changes`)가 **같은
    #   함수**를 공유한다(복제 금지). `report=True` 는 기존과 같은 지점·같은 문구로 안내를 낸다.
    try:
        sync_plan = update_plan._resolve_engine_sync_plan(
            home, effective_dest, source_root,
            target=args.target,
            guest_entries=guest_entries,
            guest_backfill=guest_backfill,
            guest_backfill_flavors=guest_backfill_flavors,
            inventory_out=plan_inventory,
            dest_inventory_out=plan_dest_inventory,
            report=True,
            conf=conf,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except retired_paths.RetiredPathError as exc:
        print(f"[중단] retired-path manifest 자기치유 실패 — {exc}", file=sys.stderr)
        return 1
    except update_plan._EngineSyncNotationContextError as exc:
        # 엔진 사본 skew 는 이 타입으로 감싸이지 않는다(`_resolve_engine_sync_plan` 이 원본 그대로
        #   재-raise 해 이 except 를 지나쳐 아래 `main()` 의 최외곽 skew 재-raise 로 올라간다) —
        #   그걸 "context 실패 rc2" 로 덮으면 로드 경계 진단이 사라진다. 여기 도달한 것만 진짜
        #   notation context 해소 실패다(무진단 침묵 금지).
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    selfheal = sync_plan["selfheal"]
    manifest = sync_plan["manifest"]
    changes, missing = sync_plan["changes"], sync_plan["missing"]

    # 완료 프로세스는 엔진 코드 변경 0건일 때만 진행한다 — 전달 프로세스가 옮긴 뒤 상류가 바뀌었으면
    #   여기서 옮기면 이 프로세스가 복사한 엔진 모듈을 적재하게 된다. 쓰기 전에 멈추고 재실행을 안내한다.
    pending_engine = sorted(str(change[0]).replace("\\", "/") for change in changes
                            if manifest_rules._is_engine_code(change[0]))
    if handoff is not None and pending_engine:
        print(f"[중단] 완료 단계에서 엔진 코드 변경 {len(pending_engine)}건을 찾았다. 전달 뒤 상류가 "
              f"바뀐 것이다. 같은 갱신을 다시 실행하라: {', '.join(pending_engine)}",
              file=sys.stderr)
        return 1
    # 내보내기의 `--paths` 도 같은 규칙이다 — 엔진 코드는 세대 단위로만 옮긴다.
    if scope_paths:
        rc = engine_transfer._refuse_scope_with_pending_engine_code(changes)
        if rc:
            return rc

    skew_manifest = None
    if selfheal["status"] == "legacy_preserved":
        # self-prop 계획 제외는 `_resolve_planning_manifest` 가 한다(미리보기와 공유). 여기선 skew
        # 대조 기준만 갈라 둔다 — 대조는 **원문 전체**를 봐야 upstream 신규 등재분을 정상 검출한다.
        skew_manifest = update_manifest._resolve_local_manifest(effective_dest, source_root)

    retired_files = update_manifest._retired_manifest_files(
        source_root, manifest, effective_dest, plan_dest_inventory)

    # ── 경로 스코프 적용: **계획을 좁히는 게 아니라 계획 결과를 거른다.** manifest 항목은 디렉토리
    #    일 수 있어(예 `.claude/skills`) 항목 단위로 자르면 "그 디렉토리 안 파일 하나" 를 못 고른다 —
    #    plan 이 산출한 파일 단위 결과에서 고르면 경로 리매핑·render 판정을 그대로 물려받는다.
    #    등재 검증은 manifest 기준(요청이 어떤 항목과도 안 겹치면 rc1) — 오타가 조용한 무전파로
    #    끝나지 않게 한다. 계획 자체는 여전히 manifest 전량을 훑으므로 스코프 밖 항목의 구조 오류
    #    (출하 인벤토리 0 등)는 그대로 표면화된다 — 스코프는 *적용 범위*를 좁히지 실패 판정을
    #    끄지 않는다.
    if scope_paths:
        # 좁은 판정(계획 확정 manifest 기준) — 선검증은 합집합이라 관대했다.
        rc = upstream_changes._refuse_unregistered_scope_paths(
            manifest, scope_paths, effective_dest)
        if rc:
            return rc
        # 등재 디렉토리 **안의 오타**는 위 판정을 통과한다(`adapterdir/typo.md` 는 `adapterdir`
        #   등재와 겹친다) — 실제 계획 인벤토리(변경 0 포함)와 source 부재 보고 어디에도 대응이
        #   없으면 그 요청은 아무 파일도 가리키지 않는다. 조용한 rc0 무전파 대신 거부한다.
        # 부재 보고의 좌표는 dest·source 양쪽이다(`@source` 항목은 둘이 다르다) — 한쪽만 보면
        #   upstream 경로로 지정한 "source 부재" 요청이 오타로 오분류된다(rc2 여야 할 것이 rc1).
        source_rel_by_dest = {
            str(entry).replace("\\", "/").strip("/"):
                manifest_rules._source_root_rel(entry).replace("\\", "/").strip("/")
            for entry in manifest
        }
        missing_coordinates: set[str] = set()
        for rel in missing:
            dest_norm = str(rel).replace("\\", "/").strip("/")
            missing_coordinates.add(dest_norm)
            missing_coordinates.add(source_rel_by_dest.get(dest_norm, dest_norm))
        phantom = [
            path for path in scope_paths
            if not any(manifest_rules._paths_overlap(path, item) for item in plan_inventory)
            and not any(manifest_rules._paths_overlap(path, item) for item in missing_coordinates)
        ]
        if phantom:
            for path in phantom:
                print(f"  [대응 없음] {path}", file=sys.stderr)
            print(
                f"오류: --paths 경로 {len(phantom)}개가 이번 계획의 출하 인벤토리에 대응하지 "
                "않는다(등재 디렉토리 안의 오타·이미 폐기된 경로). 아무것도 전파하지 않고 멈춘다 — "
                "경로 철자와 upstream 에 그 파일이 있는지 확인하라.",
                file=sys.stderr,
            )
            return 1
        # 스코프 적용 **전** 계획을 보존한다 — 훅 세트 반쪽 갱신 판정이 "이번에 옮기는 것" 과
        #   "옮겨야 하는데 스코프 밖인 것" 을 dest 좌표로 대조하는 데 쓴다(아래).
        planned_changes = list(changes)
        changes = [
            change for change in changes
            if any(upstream_changes._in_scope_paths(candidate, scope_paths)
                   for candidate in upstream_changes._scope_change_candidates(
                       change[0], change[1], source_root))
        ]
        # source 부재 보고는 **항목 단위**(디렉토리 등재 하나)이고 `@source` 항목은 dest·upstream
        #   경로가 다르다 — dest 경로만 대조하면 upstream 경로로 지정한 요청의 부재 보고가 접혀
        #   "변경 없음 rc0" 라는 거짓 성공이 된다. 위에서 만든 양쪽 좌표를 그대로 쓴다.
        missing = [
            rel for rel in missing
            if any(
                manifest_rules._paths_overlap(coordinate, path)
                for coordinate in {
                    str(rel).replace("\\", "/").strip("/"),
                    source_rel_by_dest.get(
                        str(rel).replace("\\", "/").strip("/"),
                        str(rel).replace("\\", "/").strip("/")),
                }
                for path in scope_paths
            )
        ]
        print(f"  경로 스코프: {' '.join(scope_paths)} — 이 경로만 전파한다"
              "(baseline 기록·진입 doc 마이그레이션·훅 재설치·동기 후 프롬프트 생략).")
        # 훅 세트 반쪽 갱신은 알리는 데서 그치지 않는다 — 하네스가 잠기므로 **쓰기 전에** 거부.
        #   입력은 **해소된 계획**이다(원문 스코프 표기 아님) — `--paths` 는 dest 좌표와 `@source`
        #   상류 좌표를 모두 받으므로, 원문을 그대로 비교하면 상류 좌표 요청이 교집합 0 으로 빠져
        #   검사가 통째로 무발화한다(래퍼만 갱신하고 rc0).
        rc = update_adapters.refuse_partial_hook_set_scope(home, changes, planned_changes, source_root)
        if rc:
            return rc

    for r, _sp, _dst, kind in changes:
        # render path 는 byte-copy 가 아니라 재렌더 산출물 — PM 이 구분하게 [render] 로 표기
        # ([update] = byte-copy· dry-run 표기). new 든 update 든 render 면 [render].
        label = (
            "render"
            if (
                getattr(_dst, "render", False)
                or getattr(_dst, "entry_notation_template", None) is not None
            )
            else kind
        )
        print(f"  [{label}] {r}")

    rc = update_adapters._refuse_missing_sources(missing, manifest)
    if rc:
        return rc

    # ── manifest skew 탐지 — upstream engine.manifest 와 로컬(sync 에 쓰인) manifest
    #    를 대조해 "로컬에 없는 upstream 신규 등재 경로"(신규 엔진 파일)를 찾는다. 로컬 manifest
    #    가 구형이면 신규 경로가 이번 sync 로 도달하지 않으므로, 아래 baseline 갱신을 억제해
    #    drift-lint 가 계속 skew 를 울리게 한다(false-최신 차단). --dry-run 도 동일 대조 결과 표시.
    #
    #    **self-update(채택자 흡수) 경로 한정** — `--target`(루트→templates/<name> 엔진 export)은
    #    타깃별 manifest(templates/*/engine.manifest)가 루트와 *의도적으로* 다르므로(어댑터
    #    비대칭·@target-owned 등) 대조하면 대량 오탐 + baseline 억제가 된다. --target 은 검출/억제
    #    를 비발화하고 현행 거동(무조건 baseline 갱신)을 유지한다(codex must-fix).
    #
    #    **flavor-correct 대조 기준 통일**: skew 대조 upstream manifest 는 selfheal 이
    #    해소한 *동일* flavor-correct 경로(`selfheal["upstream_manifest"]`)를 넘긴다 — 안 그러면
    #    flavor 채택자(@source self-prop)가 치유 후에도 root-only 경로(`.claude/agents` 등)를 skew
    #    오탐해 baseline 이 억제된다(승격 기준 == 탐지 기준). 승격되면 manifest==flavor
    #    upstream 이라 skew 는 정의상 0.
    skew_status, skew_new = (
        ("skipped", [])
        if args.target
        else update_manifest.detect_manifest_skew(
            skew_manifest if skew_manifest is not None else manifest,
            source_root,
            upstream_manifest=selfheal["upstream_manifest"],
            upstream_manifests=selfheal.get("upstream_manifests"),
        )
    )

    # ── 진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정 ──
    #    --target(엔진 export)은 비발화(skew/selfheal 동일 경계). 구형 미수정 opencode AGENTS.md
    #    를 신형 공통 코어로 자동 전환(+백업·jsonc idempotent), 수정 흔적 있으면 무손·loud 안내.
    #    AGENTS.md·opencode.jsonc 는 instance-owned(manifest 밖)이라 changes 유무와 독립.
    #
    #    ⚠ 시퀀싱 (비파괴 보장): 실제 전환 write 는 **apply(changes) 성공 이후**에만 한다.
    #    apply 가 render/IO 로 중단되면 신규 등재분(예 `.opencode/pm-instructions.md`)이 lay down
    #    되지 않는데, 그 전에 AGENTS.md 를 신형(위임 공백 공통 코어)으로 갈고 jsonc 가 미-laydown
    #    파일을 참조하면 채택자가 반쪽 상태(위임 방법론 공백)에 갇힌다 — 구형은 인라인 자족이라
    #    전환 전이 더 안전한 역설. 따라서 apply 실패 시 채택자가 *완전한 구형*에 남도록, has-changes
    #    경로는 apply 뒤에서만 전환한다. changes 없음(=엔진 최신·신규 등재분도 이미 laydown)·dry-run
    #    (무write)은 apply 가 없으므로 각 경로에서 직접 처리한다. 각 경로 migrate 1회(write flag 만 상이).
    #    `--paths`(부분 전파)도 비발화다 — 요청 밖 인스턴스 파일을 고치지 않는다는 게 이 모드의
    #    전부이고, 진입 doc 전환은 요청하지 않은 write 다.
    do_migrate = not args.target and not scope_paths
    # manifest가 명시한 engine-owned 구 경로 퇴역도 전량 self-sync에서만 돈다.
    # 구 updater가 신 파일·신 updater를 배달한 뒤 다음 changes=0 실행이
    # 실제 퇴역을 닫는다. `--paths`/`--target`은 요청 밖 write라 비발화다.
    do_retire = not args.target and not scope_paths

    # ── 보호 훅 정합 확인 + drift 재설치 — migrate 와 **같은 경계·같은
    #    시퀀싱**(--target 비발화 · changes 0 경로에서도 write · dry-run 은 판정만). changes 로
    #    게이트하면 이 기능을 배달하는 sync(구 엔진이 실행)도, 그 다음 실행(changes 0)도 발화
    #    하지 않아 채택자가 가드를 못 받는다(격리 실측 RUN1/RUN2·모듈 주석). 반복 출력은
    #    `_protected_hook_in_sync` 정합 판정이 흡수한다(정합이면 무출력).
    #    `--paths` 는 migrate 와 같은 이유로 비발화(요청 밖 write 0).
    do_reinstall = not args.target and not scope_paths

    # ── instance-owned 어댑터 config 채널 — migrate/reinstall 과 **같은 경계·같은 시퀀싱**.
    #    `--target`(엔진 export)은 dest 가 출하 템플릿 트리라 채택자 config 개념이 없고,
    #    `--paths`(부분 전파)는 요청 밖 write 를 하지 않는다. changes 유무와 독립인 것도 같다 —
    #    이 채널이 나르는 건 manifest 밖 파일이라 엔진 변경 0 인 실행에서도 할 일이 있다.
    do_adapter_config = (
        not args.target and not scope_paths
        and update_adapters._has_adapter_config_candidate(effective_dest)
    )
    # instance-owned 세대 요약은 각 종료 분기에서 config 채널의 **최종 판정 뒤** 계산한다.
    # 무편집 managed는 같은 실행에서 자동 갱신·원장화되므로 사전 계산한 --accept 처방을 뒤늦게
    # 출력하면 실제 최종 상태와 모순된다. report-drift·edited managed는 원장이 전진하지 않아
    # 최종 계산에도 그대로 남는다. --target·--paths는 채택 인스턴스 전체 흡수가 아니므로 비발화.
    def print_instance_owned_delta() -> None:
        if not args.target and not scope_paths:
            update_adapters._print_instance_owned_template_delta(
                update_adapters._instance_owned_template_delta_lines(effective_dest, source_root))

    if not changes:
        # 전달 프로세스가 엔진 코드를 옮겼으면 이 실행은 변경이 있었던 실행이다 — 적용 사실을 완료
        #   단계의 게이트보다 먼저 남긴다(변경 있음 경로의 `✓` 와 같은 자리).
        if transferred:
            print(f"✓ {transferred} 파일 동기화")
        # 잔존 은퇴 파일은 changes 와 독립이다 — "변경 없음" 이 곧 "dest 가 상류와 같다" 는 아니다.
        update_manifest._print_retired_manifest_files(retired_files)
        update_manifest._print_manifest_selfheal_finding(selfheal, dry_run=args.dry_run)
        update_manifest._print_manifest_skew_finding(skew_status, skew_new, dry_run=args.dry_run)
        if do_retire and not retired_paths._run_retired_path_migration(
            effective_dest, source_root, manifest, selfheal,
            write=not args.dry_run,
        ):
            return 1
        if do_migrate:
            # 엔진 변경 0 = 이미 최신(신규 등재분도 laydown 완료) → 전환 write 안전(apply 무관).
            result = entry_doc_migration.migrate_entry_doc(
                effective_dest, source_root, write=not args.dry_run)
            entry_doc_migration._print_entry_doc_migration_finding(result, dry_run=args.dry_run)
            update_finish.drop_self_lease_rows(effective_dest, write=not args.dry_run)
            update_finish.idle_legacy_slot_session_rows(effective_dest, write=not args.dry_run)
            entry_doc_migration.migrate_renamed_conf_keys(effective_dest, write=not args.dry_run)
            update_finish.fill_self_repo_row(effective_dest, write=not args.dry_run)
        if do_reinstall:
            # **업그레이드 배달 다음 실행이 여기로 온다**(dest 는 신 엔진·changes 0) — 훅이
            # 실제로 깔리는 지점이므로 migrate 와 동형으로 write 한다(정합이면 무출력).
            hooks = update_adapters.reinstall_protected_hooks(
                effective_dest, write=not args.dry_run)
            update_adapters._print_protected_hook_reinstall_finding(hooks, dry_run=args.dry_run)
        if do_adapter_config:
            # 엔진 변경 0 인 실행에서도 돈다 — 이 채널의 대상은 manifest 밖이라 `changes` 와
            #   무관하고, 훅 재설치와 같은 이유로 여기가 실제 배달 지점이 되는 실행이 있다.
            configs = update_adapters.sync_adapter_configs(
                effective_dest, source_root, write=not args.dry_run)
            update_adapters._print_adapter_config_finding(configs, dry_run=args.dry_run)
            # 하네스 사용 한도·압축 비율 반영 — 채널이 템플릿 갱신·원장 기록을 끝낸 파일 위에 쓴다.
            if not update_adapters.apply_harness_ctx_limits(effective_dest, write=not args.dry_run):
                return 1
            # 세대 정합은 config 채널과 **같은 경계·같은 자리**에서 본다(둘 다 manifest 밖
            #   config 를 축으로 하는 판정이라 한쪽만 도는 실행이 있으면 안 된다). 게이트 순서는
            #   심각도순 — 세대 불일치는 하네스가 잠기는 상태고 원장 미수렴은 다음 실행이
            #   수렴시킨다(두 소견 자체는 이미 위에서 각자 출력됐다).
            hook_set_check = update_adapters.check_adapter_hook_sets(home, effective_dest, source_root)
            update_adapters._print_adapter_hook_set_finding(hook_set_check, dry_run=args.dry_run)
            print_instance_owned_delta()
            if not args.dry_run and update_adapters._adapter_hook_set_gate_failed(hook_set_check):
                print(
                    "[중단] 설치된 훅 세트가 채택자 config 가 요구하는 세대가 아니다 — 위 처방 후 "
                    "pm-update 를 재실행하라(방치하면 훅이 rc2 로 도구 호출을 막는다).",
                    file=sys.stderr,
                )
                return 1
            if not args.dry_run and update_adapters._adapter_config_gate_failed(configs):
                # 엔진 파일에는 적용할 변경이 없었다는 사실만 말한다. adapter config 가 red인데
                # "최신/흡수 완료"라고 접으면 release가 false-complete 되므로 baseline 기록도 막는다.
                print(
                    "[중단] 엔진 manifest 변경은 0건이지만 managed 어댑터 config가 "
                    "미수렴이다 — 위 처방 후 pm-update를 재실행하고 "
                    "`pm-config sync-adapter-config --check`를 통과시켜라.",
                    file=sys.stderr,
                )
                return 1
        else:
            print_instance_owned_delta()
        if not transferred:
            print("최신 — 변경 없음.")
        # RUN2 수렴 지점: 엔진을 배달한 RUN1은 구 pm_update로 실행될 수 있으므로, 새 엔진의
        # 변경 0 재실행에서도 경로 upstream의 baseline/seen 쌍을 확인한다. dry-run은 기존
        # 계약대로 local.conf를 절대 쓰지 않는다. manifest skew면 has-changes 경로와 동형으로
        # 두 키를 함께 억제해 반쪽 상태/거짓 drift를 만들지 않는다.
        #    `--paths` 는 baseline 을 건드리지 않는다 — 부분 전파를 "여기까지 흡수함" 으로 박으면
        #    나머지 미전파분이 drift-lint 에서 사라진다(거짓 최신).
        if not args.dry_run and not scope_paths:
            # 변경 0 경로에서도 baseline 수렴은 has-changes 경로와 **같은 순서·같은 게이트**다.
            upstream_revisions.converge_upstream_revs(
                effective_dest, source_root, skew_status, skew_new)
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(changes)} 파일 변경 예정 (적용 안 함).")
        update_manifest._print_retired_manifest_files(retired_files)  # apply 경로와 같은 보고(무write).
        update_manifest._print_manifest_selfheal_finding(selfheal, dry_run=True)
        update_manifest._print_manifest_skew_finding(skew_status, skew_new, dry_run=True)
        if do_retire and not retired_paths._run_retired_path_migration(
            effective_dest, source_root, manifest, selfheal, write=False,
            changes=changes,
        ):
            return 1
        if do_migrate:  # 판정만(write=False·무부작용).
            result = entry_doc_migration.migrate_entry_doc(effective_dest, source_root, write=False)
            entry_doc_migration._print_entry_doc_migration_finding(result, dry_run=True)
            update_finish.drop_self_lease_rows(effective_dest, write=False)
            update_finish.idle_legacy_slot_session_rows(effective_dest, write=False)
            entry_doc_migration.migrate_renamed_conf_keys(effective_dest, write=False)
            update_finish.fill_self_repo_row(effective_dest, write=False)
        if do_reinstall:  # 대상 해소만(write=False·무부작용).
            hooks = update_adapters.reinstall_protected_hooks(effective_dest, write=False)
            update_adapters._print_protected_hook_reinstall_finding(hooks, dry_run=True)
        if do_adapter_config:  # 판정만(write=False·파일·원장 미변경).
            configs = update_adapters.sync_adapter_configs(effective_dest, source_root, write=False)
            update_adapters._print_adapter_config_finding(configs, dry_run=True)
            if not update_adapters.apply_harness_ctx_limits(effective_dest, write=False):
                return 1
            update_adapters._print_adapter_hook_set_finding(
                update_adapters.check_adapter_hook_sets(home, effective_dest, source_root), dry_run=True)
        print_instance_owned_delta()
        return 0

    # 훅 세트 판정자는 **상류 세대**로 미리 해소해 넘긴다 — 이번 실행이 엔진 package 를
    #   갱신하면 dest 사본의 선언은 구세대라, 이번 세대가 추가한 훅 경로가 바로 이 실행에서
    #   비원자 copy2 로 떨어진다(원자 write 가 영영 한 세대 늦게 도착).
    update_render.apply(home, changes,  # ← 실패 시 예외 전파 → 아래 전환 미도달(채택자 완전한 구형 유지).
                        is_hook_set_path=hook_sets.resolve_hook_set_predicate(home, source_root))
    print(f"✓ {transferred + len(changes)} 파일 동기화")

    # apply 완료를 관측한 뒤에만 구 경로를 옮긴다. 이 게이트가 실패하면
    # 아래 instance migration·upstream.rev baseline 모두에 도달하지 않는다.
    if do_retire and not retired_paths._run_retired_path_migration(
        effective_dest, source_root, manifest, selfheal, write=True,
    ):
        return 1

    update_manifest._print_retired_manifest_files(retired_files)
    update_manifest._print_manifest_selfheal_finding(selfheal, dry_run=False)
    update_manifest._print_manifest_skew_finding(skew_status, skew_new, dry_run=False)
    if do_migrate:
        # 전환 write 는 apply(changes) 성공 이후 — 반쪽 상태 방지.
        result = entry_doc_migration.migrate_entry_doc(effective_dest, source_root, write=True)
        entry_doc_migration._print_entry_doc_migration_finding(result, dry_run=False)
        update_finish.drop_self_lease_rows(effective_dest, write=True)
        update_finish.idle_legacy_slot_session_rows(effective_dest, write=True)
        entry_doc_migration.migrate_renamed_conf_keys(effective_dest, write=True)
        update_finish.fill_self_repo_row(effective_dest, write=True)

    if do_reinstall:
        # apply 이후 — 방금 착지한 *새* 엔진 사본에서 훅 본문을 읽어 배포한다. 단
        # 이 경로만으로는 부족하다(배달 sync 는 구 엔진이 실행) — 위 changes 0 경로가 짝이다.
        hooks = update_adapters.reinstall_protected_hooks(effective_dest, write=True)
        update_adapters._print_protected_hook_reinstall_finding(hooks, dry_run=False)

    if do_adapter_config:
        # apply 이후 — 백업/교체 write 를 엔진 적용 성공 뒤로 미룬다(엔진이 반쯤 적용된 트리에
        #   어댑터 config 만 새 세대로 앞서가지 않게·migrate 와 같은 시퀀싱 논거).
        configs = update_adapters.sync_adapter_configs(effective_dest, source_root, write=True)
        update_adapters._print_adapter_config_finding(configs, dry_run=False)
        if not update_adapters.apply_harness_ctx_limits(effective_dest, write=True):
            return 1
        # apply 로 방금 착지한 **새 훅 세트**를 기준으로 판정한다 — 이 순서라야 "엔진 파일이
        #   뒤처져서 난 불일치" 가 이번 실행에서 저절로 해소되고, 그래도 남는 것만 red 다.
        hook_set_check = update_adapters.check_adapter_hook_sets(home, effective_dest, source_root)
        update_adapters._print_adapter_hook_set_finding(hook_set_check, dry_run=False)
        print_instance_owned_delta()
        if update_adapters._adapter_hook_set_gate_failed(hook_set_check):
            print(
                f"[중단] 엔진 파일 {len(changes)}건은 적용됐지만 설치된 훅 세트가 채택자 config "
                "가 요구하는 세대가 아니다 — 위 처방 후 pm-update 를 재실행하라"
                "(방치하면 훅이 rc2 로 도구 호출을 막는다).",
                file=sys.stderr,
            )
            return 1
        if update_adapters._adapter_config_gate_failed(configs):
            # apply는 이미 완료됐다. rollback/성공 은폐 없이 엔진 적용 사실(위 ✓ 출력)을 남기고,
            # 전체 흡수 baseline·후속 성공 프롬프트만 차단한다.
            print(
                f"[중단] 엔진 파일 {len(changes)}건은 적용됐지만 managed 어댑터 config가 "
                "미수렴이다 — 위 처방 후 pm-update를 재실행하고 "
                "`pm-config sync-adapter-config --check`를 통과시켜라.",
                file=sys.stderr,
            )
            return 1
    else:
        print_instance_owned_delta()

    # upstream.rev baseline 갱신 — 매 sync 마다 source(upstream) HEAD 를
    # local.conf 에 박아 drift-lint의 "마지막 동기 이후" 기준점을 최신화한다.
    # `upstream.seen_rev`(현재 관찰값)도 같은 rev 로 함께 기록한다(동기 시점 소스 checkout rev 가
    # 곧 관찰값·두 키가 어긋난 채 남으면 상시 거짓 drift). 단
    # **manifest skew**(로컬 manifest 구형·신규 등재분 미도달)면 갱신을 억제한다 —
    # baseline 을 최신으로 박으면 drift-lint 가 "최신"으로 침묵해 신규 엔진 파일 누락을 은폐한다
    # (회사 채택자 실측). skew 아님(정합·또는 upstream manifest 부재 fail-soft)이면 현행대로 갱신.
    # source 는 로컬 checkout 이다(URL upstream 은 이 실행이 만든 캐시 checkout).
    # best-effort — 기록 실패가 동기화 자체를 무효화하지 않는다(파일은 이미 적용됨). --target
    # 모드는 effective_dest(templates/<name>)의 conf 에 기록(루트 오염 방지).
    # `--paths`(부분 전파)는 baseline 을 건너뛴다 — 요청 경로만 옮긴 실행을 "전량 흡수"
    # 로 박으면 나머지 미전파분이 drift-lint 에서 사라진다(거짓 최신).
    if scope_paths:
        print("  (경로 스코프 — upstream.rev baseline 을 갱신하지 않는다: 나머지 경로는 "
              "여전히 미전파다.)")
        return 0
    upstream_revisions.converge_upstream_revs(effective_dest, source_root, skew_status, skew_new)
    return 0


def main(home, argv: list[str] | None = None, *, prog: str = "pm_update.py") -> int:
    """CLI 경계: repo 출하 seam의 분류 오류만 짧은 실행 오류로 바꾼다.

    apply/render/IO 등 다른 예외는 프로그래밍·시퀀싱 오류이므로 기존처럼 호출자에게 전파한다.
    """
    console.configure_console_utf8()
    try:
        return _main(home, argv, prog=prog)
    except manifest_rules.EmptyShippingInventoryError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # 엔진 사본 skew 는 분류 이전에 그대로 올린다 — 아래 분기도 결과적으로 re-raise 하지만,
        #   skew 가 이 경계를 지난다는 사실을 명시해 둔다.
        if getattr(exc, "_engine_rev_skew", False):
            raise
        if not isinstance(exc, repo_files.RepoFilesGitError):
            raise
        print(
            "오류: source 출하 파일의 git 추적정보를 열거하지 못함 — "
            f"{exc}; 해당 checkout 경로와 git index 상태를 확인·복구한 뒤 다시 실행하라.",
            file=sys.stderr,
        )
        return 1
