"""설치 CLI(`pm_import.py`)의 argv 해석과 설치 단계 조정 (층: cli).

도움말 원문은 `cli.import_args` 모듈 문서가 소유한다."""
from __future__ import annotations

from pathlib import Path
import argparse
import datetime
import sys
from pm_engine.app import adapter_accept
from pm_engine.app import add_harness
from pm_engine.app import harness_ctx
from pm_engine.app import import_fill
from pm_engine.app import import_render
from pm_engine.app import install_board
from pm_engine.app import install_plan
from pm_engine.cli import import_args
from pm_engine.domain import install_rules
from pm_engine.domain import upstream_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import console
from pm_engine.infra import install_conf
from pm_engine.infra import install_files
from pm_engine.infra import install_git
from pm_engine.infra import install_receipt
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from pm_engine.infra import skill_inventory
from pm_engine.infra import upstream_git


@install_board._translate_dest_safety_errors
def main(home, argv: list[str] | None = None, *, engine_rev: str) -> int:
    console.configure_console_utf8()
    ap = argparse.ArgumentParser(
        prog="pm_import.py",
        description=import_args._cli_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "온보딩(fresh 채택자): manager(project_manager) 경로/URL 만 있으면 자율 import — "
            "harness=기본 all(등록 어댑터 전체: " + "|".join(install_rules.HARNESS_TEMPLATE_DIRS)
            + "; 단일/콤마 조합 명시 가능), --new(빈 PM 홈)/--into(기존 프로젝트 임베드) "
            "맥락 판단. 상세 가이드 = manager 루트 ADOPT.md. import 후 다음 단계: 기준 커밋 → "
            "`pm-config worktree add <repo> --user-ack <repo>`(작업 폴더, 사용자 승인) → "
            + skill_inventory._runtime_skill_entry("pm-bootstrap") + " --task <task> → 필요하면 "
            + skill_inventory._runtime_skill_entry("pm-env") + ".\n\n"
            "upstream 기록: --from 은 *파일 소스*, --upstream 은 *future update 기록*으로 "
            "디커플된다. local.conf 에 `upstream.path=`(pm_update 가 --from 생략 시 사용) + "
            "`upstream.rev=<commit>`(drift baseline·--from 이 로컬 git checkout 일 때)이 기록된다. "
            "--upstream 생략 시 --from 으로 폴백하되, --from 이 로컬 clone 이면 origin URL 을 자동도출한다 "
            "(릴리스 추적 기본). 재-import 시 현재 값으로 갱신."
        ),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--into", metavar="PATH", help="기존 프로젝트에 임베드 import(비파괴·백업·특정 케이스)")
    mode.add_argument("--new", metavar="PATH", help="PM 홈 생성 + git init (코드 없는 홈·표준 채택)")
    ap.add_argument(
        "--harness",
        type=import_args._parse_harness_arg,
        default="all",
        metavar="HARNESS[,HARNESS...]|all",
        help=(
            "어댑터 집합 (등록: " + ", ".join(install_rules.REGISTERED_HARNESSES)
            + "; 전체: all; default: all)"
        ),
    )
    ap.add_argument("--weight", choices=install_rules.WEIGHT_CHOICES, default="full",
                    help="무게축 (default: full)")
    ap.add_argument("--from", dest="source", default=str(home.root),
                    help="이번 import 의 *파일 소스* checkout 경로 (default: 이 repo 루트). "
                         "엔진/어댑터 파일을 여기서 복사한다.")
    ap.add_argument("--upstream", dest="upstream", default=None,
                    help="future update 기록값(URL 선호) — pm_update 가 --from 생략 시 쓸 upstream. "
                         "URL|경로 self-describing. 생략 시 --from 으로 폴백하되, --from 이 로컬 "
                         "git clone 이면 `git remote get-url origin` 으로 URL 자동도출(릴리스 추적). "
                         "값은 안전 검증(scheme allowlist·credential 거부·leading-dash)을 통과해야 한다.")
    ap.add_argument("--name", help="{{PROJECT_NAME}} 값 (default: 대상 디렉토리명)")
    ap.add_argument("--fill", choices=install_rules.FILL_CHOICES, default="manual",
                    help="자유서술 placeholder 채움 — auto: 하니스 구동 제안(opt-in), "
                         "manual: TODO 표시(default)")
    ap.add_argument(
        "--fill-harness",
        choices=install_rules.FILL_HARNESS_CHOICES,
        default=None,
        help="fill 구동 하네스 (default: 선택 집합 중 등록 순서상 첫 가용 바이너리)",
    )
    ap.add_argument("--opencode-model", dest="opencode_model", metavar="PROVIDER/MODEL",
                    default=None,
                    help="{{OPENCODE_PRO_MODEL}} 결정적 치환값 (비대화/CI). 예 'ollama/glm-5.2:cloud'. "
                         "opencode 어댑터 미포함이면 무시(claude-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="적용 없이 fill 계획만 출력 (실 하니스 미호출·파일시스템 미변경)")
    ap.add_argument("--board-submodule", action="store_true",
                    help="board(tickets+areas)를 별도 git submodule(.project_manager/board)로 셋업 "
                         "(공유 board). --new + --board-remote <url> 필수. "
                         "미지정(기본)=board 를 superproject inline(무변경).")
    ap.add_argument("--board-remote", dest="board_remote", metavar="URL", default=None,
                    help="공유 board 의 remote URL (--board-submodule 필수). 빈 remote 면 tickets "
                         "구조+areas.md 로 초기화 후 push, 내용 있으면 재사용(합류). URL 안전검증 "
                         "(credential-in-url·leading-dash·비허용 scheme 거부).")
    args = ap.parse_args(argv)
    # 제거 기한에 도달한 legacy harness alias 가 남아 있으면 설치 인자 해석을 막는다(실제 각인 값).
    install_rules._enforce_legacy_harness_alias_deadline(engine_rev)

    # --upstream 명시값은 *부작용 전* 입구에서 fail-closed 검증. 나쁜 값
    # (빈/leading-dash/credential-in-URL/비허용 scheme)을 silently 기록하지 않게 입구에서 거른다.
    if args.upstream is not None:
        ok, reason = upstream_rules.validate_upstream_value(args.upstream)
        if not ok:
            print(f"오류: --upstream 값 거부 — {reason}", file=sys.stderr)
            return 1

    is_new = args.new is not None
    dest_root = Path(args.into or args.new).resolve()
    source_root = Path(args.source).resolve()
    project_name = args.name or dest_root.name
    today = datetime.date.today().isoformat()

    # --board-submodule: board 를 별도 git submodule 로 셋업. --new + --board-remote
    #   <url> 필수(공유 board 의 본질=remote·미제공 fail-loud). --new 없이 단독은 거부(범위=신규 홈
    #   셋업). *부작용 전* 입구에서 fail-closed 검증(--upstream·MF2 git_init 규율 정합). inline 기본
    #   (플래그 없음)은 이 게이트를 통과해 완전 무변경.
    if args.board_submodule:
        if not is_new:
            print("오류: --board-submodule 은 --new 와 함께만 씁니다 (신규 PM 홈 셋업 범위). "
                  "기존 프로젝트엔 수동 `git submodule add` 를 쓰세요.", file=sys.stderr)
            return 1
        if not args.board_remote:
            print("오류: --board-submodule 은 --board-remote <url> 이 필수입니다 "
                  "(공유 board 의 본질은 remote). 예: --board-remote git@github.com:you/board.git",
                  file=sys.stderr)
            return 1
        ok, reason = upstream_rules.validate_upstream_value(args.board_remote)
        if not ok:
            print(f"오류: --board-remote 값 거부 — {reason}", file=sys.stderr)
            return 1
    elif args.board_remote:
        print("오류: --board-remote 는 --board-submodule 과 함께 씁니다 "
              "(board submodule 셋업 없이는 무의미).", file=sys.stderr)
        return 1

    try:
        template_roots = install_source.resolve_template_roots(source_root, args.harness)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # --new 대상이 기존 *파일* 이면 아래 iterdir() 가 NotADirectoryError 로
    #      터진다 — 디렉토리 여부를 먼저 검사해 친화적 비0 오류로 거부한다.
    if is_new and dest_root.exists() and not dest_root.is_dir():
        print(
            f"오류: --new 대상이 디렉토리가 아닌 기존 파일입니다: {dest_root}. "
            f"다른 경로를 지정하거나 해당 파일을 직접 옮긴 뒤 다시 시도하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --new 는 백업 없이 복사하므로(아래 backup_root=None), 대상이 비어있지 않으면
    #      기존 파일을 무백업 덮을 위험 → 명세(비0=대상 비어있지 않은데 백업 불가)대로 거부.
    #      dry-run 에서도 동일 판정(계획 전 게이트).
    if is_new and dest_root.is_dir() and any(dest_root.iterdir()):
        print(
            f"오류: --new 대상이 비어있지 않습니다: {dest_root}. "
            f"기존 파일이 있는 디렉토리에는 비파괴 백업이 되는 --into 를 사용하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --into 는 *기존 프로젝트* 가정이다. 미존재/비-디렉토리 경로면, 복사가 디렉토리를
    #      새로 만들고 git init 없이 board.py init 이 성공해 pre-push 훅 없는 불완전 import 가
    #      "완료"된다 → 거부. 새 프로젝트는 git init·디렉토리 생성을 하는 --new 로 안내.
    #      dry-run 에서도 동일 판정(계획 전 게이트 — --new 가드와 대칭).
    if not is_new and not dest_root.is_dir():
        print(
            f"오류: --into 대상이 존재하는 디렉토리가 아닙니다: {dest_root}. "
            f"--into 는 기존 프로젝트 전용입니다 — 새 프로젝트는 --new 를 사용하세요 "
            f"(디렉토리 생성 + git init).",
            file=sys.stderr,
        )
        return 1

    # 다중 tree import의 manifest 합집합은 **어떤 복사보다 먼저** 읽고 병합 가능성을 검증한다.
    # 후순위 manifest 부재/병합 실패를 복사 뒤 발견하면 --new/--into 모두 부분 설치가 남는다.
    try:
        prepared_manifest_union = install_plan._prepare_selected_manifest_union(template_roots)
        install_plan._warn_selected_manifest_union_conflicts(prepared_manifest_union)
        entry_doc_source_overrides = install_plan._selected_entry_doc_source_overrides(
            source_root, args.harness, args.weight)
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            "오류: 선택된 어댑터 manifest 합집합/진입문서 병합을 만들 수 없어 복사 전에 "
            "중단합니다 — "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    # --into 백업을 중앙 디렉토리 `<dest>/.pm_import_backups/<DATE>/` 로 모은다.
    #   --new 는 빈 디렉토리 보장이라 백업 없음(backup_root=None). git_safe = '추적&미변경'
    #   relpath 집합(또는 None=비-git·판정불가). git 호출 실패는 None→전부 백업(보수적 폴백).
    backup_root = None if is_new else dest_root / install_rules.BACKUP_DIR_NAME / today
    git_safe = None if is_new else install_git.git_safe_relpaths(dest_root)
    # dest 루트 신원을 **계획(plan_copy) 앞에서** 고정한다 — 계획 자체가 dest 를 읽으므로, 고정이
    #   계획 뒤면 "계획은 원래 트리에서 세우고 적용은 교체된 트리에서" 라는 어긋남이 남는다.
    #   `--new` 는 아직 트리가 없어 생성 직후에 잡는다. 획득 실패는 fail-loud(복사 전 rc1) —
    #   None 폴백은 루트 검사를 통째로 끄는 우회가 된다.
    root_identity = None
    if not is_new:
        try:
            root_identity = install_files.dest_root_identity(dest_root)
        except install_files.UnsafeDestPathError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
    try:
        actions = install_plan.plan_copy(
            template_roots, dest_root, backup_root, args.weight,
            git_safe=git_safe, source_overrides=entry_doc_source_overrides, backup_dir_name=install_rules.BACKUP_DIR_NAME,
        )
        # local.conf 백업 target 의 조상도 plan 단계에서 검증한다. 이 백업은
        #   plan_copy actions 밖(apply 후 backup_existing_local_conf)에서 일어나므로, 그 조상이
        #   일반 파일/symlink 면 *복사가 일부 끝난 뒤* mkdir 실패로 부분 적용이 남는다 → 사전 차단.
        if backup_root is not None and not is_new:
            _local_conf = dest_root / ".project_manager" / "local.conf"
            if _local_conf.is_file() or _local_conf.is_symlink():
                install_plan._check_ancestor_safe(
                    dest_root, backup_root / ".project_manager" / "local.conf", set())
    except (
        install_files.FileVsDirConflict,
        install_files.AncestorConflict,
        install_source.EmptyTemplateShippingInventoryError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    try:
        # 표기 독자 = **dest 실설치 하네스 ∪ 이번 선택**. `--into` 로 codex 인스턴스에 claude 를
        #   얹으면 공유 wiki 는 두 하네스가 함께 읽는다 — 이번 run 의 집합만 쓰면 새로 깔린
        #   문서가 claude 단독 표기로 나가 기존 codex 독자에게 틀린 표기가 된다(실측).
        #   `--new` 는 빈 dest 라 실설치 집합이 비어 선택과 같다(현행 회귀 불변).
        #   기존 설치분은 따로 붙들어 둔다 — 설치 기록 자격 판정이 "이번에 선 어댑터" 와 "원래
        #   있던 것" 을 갈라야 한다(원래 것은 이번 실행이 임의로 철회하지 않는다).
        preexisting_harnesses = install_receipt.installed_harnesses(dest_root, source_root)
        notation_harnesses = tuple(
            name for name in install_rules.HARNESS_TEMPLATE_DIRS
            if name in {*preexisting_harnesses, *args.harness}
        )
        # context 파생 manifest 도 그 독자 집합에서 뽑는다(선택 트리만 보면 기존 하네스가 빠져
        #   공유 문서가 단독 표기로 재렌더된다). 소스에서 한 하네스의 manifest 라도 못 얻으면
        #   **조용히 빼지 않고 복사 전에 중단한다** — 조용한 제외는 그 독자가 없는 것처럼
        #   재렌더해(기존 codex 인스턴스에 claude 단독 표기) 이 티켓이 닫은 클래스를 되살린다.
        notation_manifests = []
        missing_notation_manifests = []
        for name in notation_harnesses:
            for dirname in install_rules.HARNESS_TEMPLATE_DIRS[name]:
                manifest = (source_root / "templates" / dirname
                            / ".project_manager" / "engine.manifest")
                if manifest.is_file():
                    notation_manifests.append(manifest)
                else:
                    missing_notation_manifests.append(f"{name}({manifest})")
        if missing_notation_manifests:
            raise RuntimeError(
                "설치 하네스의 표기 manifest 를 소스에서 찾을 수 없습니다: "
                f"{', '.join(missing_notation_manifests)}. 그 하네스를 독자에서 조용히 빼면 "
                "공유 문서가 잘못된 단독 표기로 재렌더되므로 중단합니다 — `--from` 이 그 flavor "
                "template 을 가진 checkout 인지 확인하세요."
            )
        entry_notation_templates = manifest_store._entry_notation_templates_from_manifests(
            notation_manifests
            or [root / ".project_manager" / "engine.manifest" for root in template_roots],
            source_root,
        )
        shared_agents_members = install_rules.NEUTRAL_SHARED_ENTRY_DOCS[Path("AGENTS.md")][0]
        selected_shared_agents = tuple(
            install_rules.HARNESS_TEMPLATE_DIRS[harness][0]
            for harness in notation_harnesses
            if harness in shared_agents_members
        )
        # AGENTS.md 는 manifest 미등재(instance-owned 루트 doc)라 소유자 매칭으로는 context 가
        #   안 나온다. 독자가 설치 하네스 전체가 아니라 이 진입문서를 읽는 부분집합이므로
        #   wiki 폴백에 맡기지 않고 그 멤버십을 명시 전달한다 — 단일 하네스도 마찬가지다
        #   (조용한 skip 0·단일이면 그 하네스 표기라 출하 형상에선 no-op).
        if selected_shared_agents:
            entry_notation_templates["AGENTS.md"] = selected_shared_agents
    except (OSError, ValueError, RuntimeError) as exc:
        # 엔진 사본 skew 는 삼키지 않는다 — 이 블록은 설치 하네스 판별(`installed_harnesses` →
        #   출하 인벤토리 열거)까지 품어 skew 가 도달할 수 있고, 그걸 "context 실패 rc1" 로
        #   덮으면 로드 경계 진단이 사라진다(엔진 관례: fail-soft 하되 skew 만 재-raise).
        if getattr(exc, "_engine_rev_skew", False):
            raise
        print(
            "오류: 선택된 어댑터의 스킬 표기 context를 만들 수 없어 복사 전에 중단합니다 — "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    template_dir_names = [
        template_dir
        for harness_name in args.harness
        for template_dir in install_rules.HARNESS_TEMPLATE_DIRS[harness_name]
    ]
    # 폴백 표기 context = 이 인스턴스를 읽는 하네스 전체(기존 설치 + 이번 선택).
    notation_context_dirs = tuple(
        template_dir
        for harness_name in notation_harnesses
        for template_dir in install_rules.HARNESS_TEMPLATE_DIRS[harness_name]
    )
    # 미등록 표기 하네스는 **복사 전** 중단한다 — 렌더 단계에서 터지면 부분 설치가 남는다.
    #   판정 대상은 실제 렌더 context 전체다(기존 설치 하네스가 미등록이어도 복사 뒤에 터진다).
    unregistered_notation = import_render.unregistered_skill_notation_template_dirs(home,
        notation_context_dirs)
    if unregistered_notation:
        print(
            f"오류: {import_render._unregistered_skill_notation_message(unregistered_notation)}",
            file=sys.stderr,
        )
        return 1

    # 복사 대상이 아닌 **기존 dest 파일**의 재렌더 계획(`--into` 축·`--new` 는 빈 dest 라 0건).
    #   복사가 닿지 않는 두 부류가 여기서 걸린다: (a) 이번 선택 하네스가 출하하지 않는 wiki seed
    #   (기존 하네스가 깔아 둔 것) (b) 템플릿에서 *생성된* 인스턴스 파일(`pm_state.md`). 계획·백업·
    #   경로 안전을 add-harness 와 같은 경로로 태운다(비파괴 보장 동형).
    copied_dst_relpaths = {a.dst.relative_to(dest_root) for a in actions}
    try:
        existing_rerender_relpaths, existing_backup_blocked = (
            add_harness._shared_notation_rerender_plan(
                home, dest_root,
                add_harness._notation_rerender_contexts(
                    entry_notation_templates,
                    add_harness._unowned_shipped_wiki_relpaths(
                        source_root, notation_harnesses, entry_notation_templates),
                    notation_context_dirs,
                ),
                backup_root,
                git_safe,
                root_identity=root_identity,
            )
        )
    except install_files.UnsafeDestPathError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    # 이번 run 이 복사하는 파일은 이미 render 범위 안이라 중복 처리하지 않는다.
    existing_rerender_relpaths = [
        rel for rel in existing_rerender_relpaths if rel not in copied_dst_relpaths]

    # ── 계획 출력 ──
    mode_label = f"--new {dest_root}" if is_new else f"--into {dest_root}"
    harness_label = ",".join(args.harness)
    print(f"[pm_import] {mode_label}  harness={harness_label}  weight={args.weight}")
    print(f"  소스: {source_root}/templates/{'+'.join(template_dir_names)}")
    n_copy = len(actions)
    n_backup = sum(1 for a in actions if a.backup is not None)
    n_git_safe = sum(1 for a in actions if a._git_safe_skip)
    for a in actions:
        print(a.describe())
    # 백업이 중앙 디렉토리이고, git-safe(추적&미변경)는 백업 생략임을 한 줄로 요약한다.
    if not is_new:
        git_note = (
            f"git work tree (추적&미변경 {n_git_safe} 백업 생략)"
            if git_safe is not None
            else "비-git/판정불가 (충돌 전부 백업)"
        )
        print(f"  백업 위치: {install_rules.BACKUP_DIR_NAME}/{today}/  · {git_note}")
    print(f"  → {n_copy} 파일 복사 ({n_backup} 백업), placeholder 치환, board.py init")
    # 복사 대상은 아니지만 표기 병기로 *내용이 바뀌는* 기존 파일 — 계획에 명시한다(dry-run 공통).
    if existing_rerender_relpaths:
        print(f"  기존 문서 재렌더 ({len(existing_rerender_relpaths)}건 · 병기 표기 · "
              "백업 후 변경):")
        for rel in existing_rerender_relpaths:
            print(f"  [rerender] {rel.as_posix()}")
    for rel_posix in existing_backup_blocked:
        print(f"  ⚠️ 기존 문서 {rel_posix}: 백업 자리({install_rules.BACKUP_DIR_NAME}/)가 막혀 있어 재렌더를 "
              "생략합니다 — 백업 못 하는 파일은 고치지 않습니다(비파괴).", file=sys.stderr)
    if len(template_roots) > 1:
        print(
            f"  engine.manifest: 선택된 {len(template_roots)}개 트리 선언의 합집합 "
            "(중복 경로는 registry 정규 순서상 첫 트리 우선)"
        )
    if args.board_submodule:
        print(f"  board submodule: {args.board_remote} → {install_board._BOARD_SUBMODULE_PATH} "
              f"(빈 remote 면 구조 init+push·ignore=all)")

    # fill 단계 계획/게이트 미리보기 (dry-run·실행 공통). 실 하니스 호출 여부는 opt-in 게이트
    # (PM_IMPORT_LIVE_HARNESS=1 AND --fill auto)로 결정한다 — 여기서는 의도만 출력한다.
    fill_harness = import_fill._resolve_fill_harness(args.fill_harness, args.harness)
    if args.fill == "auto" and fill_harness not in install_rules.FILL_CAPABLE_HARNESSES:
        print(
            f"오류: fill harness {fill_harness!r}는 runner 매핑이 없습니다 — "
            f"지원: {', '.join(install_rules.FILL_CAPABLE_HARNESSES)}. 파일 설치 전 중단합니다.",
            file=sys.stderr,
        )
        return 1
    live_allowed = import_fill._live_harness_allowed(args.fill)
    if args.fill == "auto":
        try:
            cap_warning = import_fill.fill_harness_cap_advisory(fill_harness, dest_root)
        except Exception as exc:  # noqa: BLE001 — advisory는 import를 막지 않는다.
            if getattr(exc, "_engine_rev_skew", False):
                raise
            print(f"[fill auto] 경고: 하네스 상한 판정 실패({exc})", file=sys.stderr)
        else:
            if cap_warning is not None:
                print(cap_warning, file=sys.stderr)
    if args.fill == "auto":
        gate = "실구동(게이트 통과)" if live_allowed else "stub/미구동(게이트 미통과 — 안전)"
        print(f"  fill=auto  harness={fill_harness}  → {gate}")
    else:
        print("  fill=manual  → 자유서술 placeholder 를 TODO 로 표시(하니스 미구동).")

    if args.dry_run:
        # opencode 모델 결정적 해소 계획(LLM 아님·fill 이전 단계). 복사 *예정* src 에
        #         {{OPENCODE_PRO_MODEL}} 가 잔존하면(opencode 트리) 어느 경로로 갈지·플래그값·
        #         tty 여부만 출력한다 — 프롬프트·파일변경·`opencode models` 실호출 0.
        if import_fill._plan_opencode_model_targets(actions):
            stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
            print("  [dry-run] opencode 모델 해소 계획 (결정적·LLM 아님·파일 미변경):")
            print(f"    stdin tty: {stdin_tty}")
            if args.opencode_model:
                print(f"    경로: flag → --opencode-model '{args.opencode_model}' 로 치환.")
            elif stdin_tty:
                print("    경로: interactive → `opencode models` 번호목록·대화형 선택 후 치환.")
            else:
                print("    경로: todo → 비-tty(또는 opencode 부재) — 치환 안 함·TODO 마커 폴백.")
        # MF2: --dry-run = fill 계획 미리보기. 실 하니스 미호출·파일 미변경. auto 면 무엇을
        #      채울지(대상 토큰)·결정된 fill harness·게이트 상태(실구동/manual 폴백)를 출력한다.
        #      복사를 안 하므로 dest 가 아닌 복사 *예정* src(actions)에서 잔존 토큰을 스캔한다.
        if args.fill == "auto":
            plan_tokens = import_fill._plan_fill_targets(actions)
            print("  [dry-run] fill=auto 계획 (실 하니스 미호출·파일 미변경):")
            if plan_tokens:
                print(f"    채울 대상 토큰: {'·'.join(plan_tokens)}")
            else:
                print("    채울 대상 토큰: (트리에 자유서술 placeholder 없음 — 처리 불필요)")
            print(f"    fill harness: {fill_harness}")
            if live_allowed:
                print("    게이트: 통과 → 적용 시 실 하니스 구동(제안 — 사람 검토 전제).")
            else:
                print("    게이트: 미통과(PM_IMPORT_LIVE_HARNESS 미설정) → 적용 시 manual 폴백"
                      "(TODO 표시·하니스 미구동).")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return 0

    # ── 적용 ──
    if is_new:
        dest_root.mkdir(parents=True, exist_ok=True)
        # MF2: git init 실패 시 git repo 없는 불완전 import — board.py init 의 pre-push 훅이
        #      git repo 에 의존하므로 비0 전파(복사 전에 중단).
        git_rc = install_board.git_init(dest_root)
        if git_rc != 0:
            print(
                f"오류: git init 비0 종료({git_rc}) — import 미완(git repo 없이는 "
                f"board.py init 의 pre-push 훅이 동작하지 않습니다).",
                file=sys.stderr,
            )
            return git_rc
        # 트리를 이제 만들었으므로 여기서 신원을 고정한다(이후 렌더까지 재사용). 여기도 fail-loud
        #   — 아직 파일을 하나도 복사하지 않은 지점이라 중단이 곧 "부분 적용 0" 이다.
        try:
            root_identity = install_files.dest_root_identity(dest_root)
        except install_files.UnsafeDestPathError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1

    # 복사 **직전** 루트 재확인(위 add-harness 와 동형) — 뒤에 두면 첫 쓰기가 교체 트리에 간다.
    install_files.assert_dest_root_unchanged(dest_root, root_identity)
    # 파일 단위 위반은 그 파일만 빼고 loud(적용 단계 rc 정책) — 루트 교체만 전체 중단.
    copy_outcome = install_files.apply_copy_plan(actions, dest_root, root_identity=root_identity)
    install_files.report_copy_apply_anomalies(copy_outcome)

    merged_manifest_entries = import_render._install_selected_manifest_union(
        prepared_manifest_union, dest_root)
    if merged_manifest_entries:
        print(
            f"✓ engine.manifest 선택 트리 합집합 설치 "
            f"({len(template_roots)}개 flavor · {merged_manifest_entries}개 관리 경로)"
        )

    # MF1: 치환 범위 = 이번 run 이 **실제로 복사한** 파일만(복사 안 한 사용자 파일 불가침·
    #      적용 단계에서 제외된 파일도 범위 밖).
    copied_relpaths = set(copy_outcome.copied)
    subs = import_render._substitution_map(project_name, dest_root, today)
    n_subst = import_render.substitute_placeholders(dest_root, subs, copied_relpaths,
                                      root_identity=root_identity)
    print(f"✓ {len(copied_relpaths)} 파일 복사 · {n_subst} 파일 placeholder 치환")

    # ── opencode 모델 결정적 해소: substitute *직후*·render *이전*. @render 활성화
    #    로 .opencode/agents 가 render 대상이 됐으므로, render_managed_files 가 model: 줄의
    #    {{OPENCODE_PRO_MODEL}} 을 만나기 *전* 에 해소해야 한다 — flag/interactive=토큰 치환,
    #    todo(미해소)=줄 주석화 + 토큰 중화(<provider/model>) → 어느 경로든 render 시점엔 토큰 0.
    #    (활성화가 그 순서
    #    가정을 깸·옛 "todo 토큰은 YAML 주석으로 남아 leak 없음" 전제 무효.) local.conf 기록
    #    (record_opencode_model)은 board init 이 local.conf 를 만든 *뒤* 로 분리(아래).
    #    LLM 추측(fill)이 아니라 `opencode models` 결정적 조회로 해소(환각·미가용 모델 제거).
    #    범위 = substitute_placeholders 와 동일한 copied_relpaths(비파괴). claude-only=inactive.
    model_result = import_fill.resolve_opencode_model(home,
        dest_root, copied_relpaths, model_arg=args.opencode_model,
        root_identity=root_identity)
    if model_result.active:
        if model_result.path == "flag":
            print(f"✓ {install_rules.OPENCODE_MODEL_TOKEN} 치환(--opencode-model "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "interactive":
            print(f"✓ {install_rules.OPENCODE_MODEL_TOKEN} 치환(대화형 선택 "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "todo":
            print(f"  {install_rules.OPENCODE_MODEL_TOKEN} TODO 표시 — {model_result.note}")

    # render 단계: @render manifest path 의 복사본을 render_adapter
    # 산출물로 다시 쓴다 — operational 토큰(subs·이미 sed) 치환. free-form value-fill 은
    # 로 제거(FILL 채널이 canonical home 전담). substitute·모델해소 *직후*. 범위 = copied_relpaths(비파괴).
    # 복사 밖 기존 문서(위 계획)는 **변경 직전 백업 후** 같은 render 채널에 실어 병기 표기를
    #   받는다. 치환·fill 범위(copied_relpaths·MF1)는 넓히지 않는다 — 표기 렌더만 태운다.
    existing_backup = install_files._backup_before_inplace_edit(
        dest_root, existing_rerender_relpaths, backup_root, git_safe,
        root_identity=root_identity)
    if existing_backup.backed_up:
        print(f"✓ 기존 문서 {len(existing_backup.backed_up)}건 백업: "
              f"{', '.join(existing_backup.backed_up)}")
    # 백업 못 한 파일은 고치지 않는다(비파괴) — 교체·삭제 둘 다 재렌더 대상에서 뺀다.
    if existing_backup.refused:
        print(f"  ⚠️ 기존 문서 {len(existing_backup.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 "
              f"없어(symlink 교체·비-디렉토리 컴포넌트) 백업할 수 없습니다 — 재렌더에서 "
              f"제외합니다(원본·저장소 밖 파일 불변): {', '.join(existing_backup.refused)}",
              file=sys.stderr)
    if existing_backup.vanished:
        print(f"  ⚠️ 기존 문서 {len(existing_backup.vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 "
              f"삭제) 백업할 수 없습니다 — 재렌더에서 제외합니다(새로 만들지 않습니다): "
              f"{', '.join(existing_backup.vanished)}", file=sys.stderr)
    _existing_dropped = set(existing_backup.refused) | set(existing_backup.vanished)
    if _existing_dropped:
        existing_rerender_relpaths = [
            rel for rel in existing_rerender_relpaths
            if rel.as_posix() not in _existing_dropped]
    n_render = import_render.render_managed_files(home,
        dest_root,
        subs,
        copied_relpaths | set(existing_rerender_relpaths),
        entry_notation_templates=entry_notation_templates,
        installed_notation_context=notation_context_dirs,
        root_identity=root_identity,
    )
    if n_render:
        print(f"✓ {n_render} 파일 render (operational 토큰 치환)")

    # ── board submodule 셋업: --new --board-submodule 일 때만. board.py init
    #    (아래 run_board_init) *이전* 에 둬야 (a) board 루트가 board/ 로 해소되고 (b) board.py
    #    init 의 _configure_board_submodule() 이 .git/config ignore=all 을 설정한다. substitute·
    #    render *이후* — 복사·치환이 끝난 wiki/tickets 를 스캐폴드로 board 에 seed 하기 때문. inline
    #    기본(플래그 없음)은 이 블록을 건너뛰어 완전 무변경(현행 --new 회귀). 실패 시 fail-loud.
    if is_new and args.board_submodule:
        board_rc = install_board.setup_board_submodule(dest_root, args.board_remote)
        if board_rc != 0:
            print("오류: board submodule 셋업 실패 — import 미완(board 미배선·부분 홈).",
                  file=sys.stderr)
            return board_rc

    # MF1: board.py init 은 local.conf 를 무조건 덮으므로(local.conf 는 복사/백업 대상 트리
    #      밖), --into 재-import 면 기존 per-clone 설정(추가 리뷰어 대상 튜플
    #      additional_reviewer.harness/.model/.reasoning 등)이 무백업 손실된다.
    #      init *호출 전*에 백업하고 원본 텍스트를 받아둔다(--new 는
    #      빈 디렉토리 보장이라 None — 보존할 것 없음).
    # 구표기 conf 는 백업 전에 멈춘다 — 바로 뒤 `board.py init` 이 그 conf 를 읽어 fail-loud 하므로
    # 원인이 traceback 으로만 보이면 채택자가 무엇을 바꿔야 하는지 알 수 없다. 자동 이관은 하지
    # 않는다(엔진은 채택자 소유 파일을 대신 고쳐 쓰지 않는다) — 키 단위 처방만 낸다.
    if not is_new and install_conf._local_conf_has_blocking_legacy(dest_root):
        print("오류: local.conf 에 구표기 키가 남아 있어 board.py init 을 실행하지 않습니다 "
              "— 엔진 파일은 이미 갱신됐으니 아래대로 키를 바꾸고 같은 명령을 다시 실행하세요.",
              file=sys.stderr)
        install_conf.print_conf_migration_notice(dest_root)
        return 1

    preserved_conf_text = install_conf.backup_existing_local_conf(dest_root, backup_root) if not is_new else None

    # SF2: board.py init 비0 이면 local.conf·pm_state 미생성 = import 미완 → 비0 전파.
    # board init 은 subprocess 라 우리 fd 를 물려줄 수 없다 — 직전에 루트를 재확인해 창을 좁힌다
    #   (검사-사용 gap 은 남는다·구조적 폐쇄 아님). 불일치는 전체 중단 클래스다.
    install_files.assert_dest_root_unchanged(dest_root, root_identity)
    rc = install_board.run_board_init(dest_root)
    if rc != 0:
        print(f"오류: board.py init 비0 종료({rc}) — import 미완(local.conf·pm_state 확인 필요).",
              file=sys.stderr)
        return rc

    # init 성공 직후 local.conf 의 operational 해소값(project.name·test.cmd·runtime.py)을 sed
    # 치환값과 정렬해 엔진 문서(local.conf 해소)와 CLAUDE.md(치환)가 같은 값을 보게 한다.
    if install_conf.sync_local_conf(dest_root, project_name):
        print("✓ local.conf operational 값 동기화 (project.name·test.cmd·runtime.py)")

    # MF1: init 이 덮은 local.conf 위에, 백업해 둔 기존 사용자 키 중 init 이 *안 쓴* 것
    #      (추가 리뷰어 대상 튜플 additional_reviewer.harness/.model/.reasoning 등)을
    #      재병합. init/operational sync 값은 우선(사라진 이름은 되얹지 않는다).
    if preserved_conf_text is not None:
        install_conf.reapply_preserved_conf_keys(dest_root, preserved_conf_text)

    # upstream 값을 local.conf 에 upstream= 으로 기록한다. board init·conf
    #   sync·preserve 단계 *이후* 에 둬야 한다 — 그래야 재-import 에서도 preserve 가 stale 값을
    #   붙들지 않고 *현재 값* 으로 확정된다(_set_conf_keys 제자리 갱신). 이후 pm_update 가
    #   --from 생략 시 이 값을 기본 upstream 으로 쓴다(--new·--into 공통).
    #
    #   --from(파일 소스)↔--upstream(update 기록) 디커플:
    #     --upstream 명시      → 그 값(URL|경로·이미 입구에서 검증됨).
    #     생략 + --from 이 로컬 git clone → origin URL 자동도출(릴리스 추적 기본).
    #     생략 + 도출 실패(git repo 아님·origin 부재) → --from 경로(기존 동작 회귀 보존).
    upstream_value = args.upstream
    if upstream_value is None:
        derived = upstream_git.derive_origin_url(source_root)
        upstream_value = derived if derived is not None else str(source_root)
    if install_conf.record_upstream(dest_root, upstream_value):
        print(f"✓ local.conf upstream.path 기록 (pm_update --from 기본값): {upstream_value}")

    # upstream.rev baseline 기록 — --from 이 로컬 git checkout
    # 이면 그 HEAD commit 을 baseline 으로 박는다("마지막 동기 이후 변경" 의 기준점). git repo
    # 아님·HEAD 해소 실패면 graceful 생략(다음 pm-update 가 두 rev 키를 함께 기록한다).
    baseline_rev = upstream_git.read_upstream_rev(source_root)
    if baseline_rev and install_conf.record_upstream_rev(dest_root, baseline_rev):
        print(f"✓ local.conf upstream.rev baseline 기록 (drift-lint 기준점): {baseline_rev}")

    # ── opencode 모델 local.conf 기록: board init·conf sync 가 local.conf 를 만든 *뒤*.
    #    실제 모델을 해소한 경로(flag·interactive)만 기록 — 이후 pm_update @render 가
    #    {{OPENCODE_PRO_MODEL}} 을 local.conf 에서 재유도할 때 키 부재로 leak assertion crash 하는
    #    걸 막는다. todo(미해소)는 위 resolve 가 토큰을 주석화+중화(<provider/model>)했으니 기록
    #    안 함(키 없어도 어댑터에 토큰 0 → leak 없음). claude import 는 active=False 라 자연 skip.
    #    (resolve_opencode_model 자체는 render 이전으로 이동·위 substitute 직후 블록 참조.)
    if model_result.active and model_result.path in ("flag", "interactive") \
            and model_result.model:
        if install_conf.record_opencode_model(dest_root, model_result.model):
            print(f"✓ local.conf harness.opencode.pro_model 기록 ({model_result.model})")

    # ── 설치 기록(install receipt): 이번 선택 ∪ dest 에 이미 설치돼 있던 하네스(=
    #    `notation_harnesses`·복사 전에 산출한 그 독자 집합)를 인스턴스 메타에 박제한다. 이후
    #    `installed_harnesses`(pm_import 판정·pm_update 표기 독자)는 증거 추론 대신 이 기록을 읽으므로
    #    판별자 파일이 지워져도 독자가 유실되지 않는다. 기록이 없던 구 인스턴스는 `--into` 재-import
    #    가 이 지점에서 backfill 한다(추론 산출을 한 번 원천으로 옮긴다). 복사·render 이후에 두는
    #    이유는 `.project_manager/` 가 그때 확실히 존재하기 때문이다(--new 포함).
    #    **실제로 어댑터가 선 하네스만** 올린다 — 적용 단계에서 그 하네스 파일이 전부 제외되면
    #    기록이 유령을 진실로 만든다(기록이 관측을 덮어 스스로 교정되지도 않는다).
    recordable, unestablished = install_receipt.established_harnesses(
        dest_root, notation_harnesses, preexisting_harnesses)
    if unestablished:
        print(f"  ⚠️ {', '.join(unestablished)} 어댑터가 이번 실행에서 서지 않아(복사 제외) 설치 "
              f"기록에 올리지 않습니다 — 위 제외 사유를 해소한 뒤 다시 실행하세요.", file=sys.stderr)
    template_coordinates = install_receipt._copied_instance_owned_template_coordinates(
        actions, dest_root, source_root, copied_relpaths, args.weight)
    if install_receipt.record_install_receipt(
            dest_root, recordable, root_identity=root_identity,
            template_coordinates=template_coordinates):
        print(f"✓ 설치 기록 갱신 ({install_receipt.INSTALL_RECEIPT_RELPATH.as_posix()}): "
              f"{', '.join(recordable)}")

    # instance-owned config 원장 — add-harness 와 같은 기록 시점·같은 규칙(레이다운/일치 확인분만).
    #   이게 있어야 다음 pm_update 가 "채택자가 손댔는가" 를 판정해 상류 동작 fix 를 안전히 나른다.
    baselined = adapter_accept.record_adapter_baseline(
        dest_root, source_root, recordable, root_identity=root_identity)
    if baselined:
        print(f"✓ 어댑터 config 원장 기록 ({adapter_baseline.ADAPTER_BASELINE_RELPATH.as_posix()}): "
              f"{', '.join(baselined)}")
    # 하네스 사용 한도 반영 — add-harness 와 같은 자리(원장 기록 뒤)·같은 규칙. `--into` 재적용이면
    #   보존된 conf 값(예 `ctx.compact_at_pct`)이 여기서 설정 파일에 들어간다.
    try:
        harness_ctx._print_harness_ctx_apply(dest_root)
    except harness_ctx.HarnessCtxError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # ── fill 단계: board init·conf sync 직후 hook. 자유서술 placeholder 처리.
    #    auto + opt-in 게이트 통과 → 하니스 구동 *제안*(파일 미변경, 사람 검토 전제).
    #    그 외(manual 또는 게이트 미통과) → TODO 표시(채택자 손작업 지점 명시).
    #    MF(비파괴): fill 스캔 범위 = substitute_placeholders 와 동일한 copied_relpaths —
    #    이번 import 가 복사한 파일만. --into 에서 복사 안 한 사용자 파일은 절대 스캔/수정 안 함.
    if args.fill == "auto" and live_allowed:
        # 실 하니스는 subprocess 라 우리 fd 를 못 물려준다 — 직전에 루트를 재확인해 창을 좁힌다.
        install_files.assert_dest_root_unchanged(dest_root, root_identity)
        fill_result = import_fill.run_fill(dest_root, fill_harness, live=True,
                               copied_relpaths=copied_relpaths,
                               root_identity=root_identity)
        if not fill_result.values:
            # 하니스 미구동/실패 → manual 폴백(자유서술이 빈 채로 남지 않게 TODO 표시).
            print(f"  fill=auto 제안 없음({fill_result.note}) — manual 폴백.")
            fill_result = import_fill._run_manual_fill(dest_root, copied_relpaths,
                                           root_identity=root_identity)
    else:
        # --fill auto 라도 게이트 미통과면 실호출 차단 → manual 강제(안전·토큰 0).
        fill_result = import_fill._run_manual_fill(dest_root, copied_relpaths,
                                       root_identity=root_identity)
    import_fill._print_fill_result(fill_result, dry_run=False)

    # pm_playbook.local 스텁 생성: fill 과 같은 자리 — 인스턴스 소유
    # 누적 학습 칸. 루트 .local 은 manifest 밖이라 복사로 안 오니 여기서 생성한다. 재-import
    # 에서 기존 .local 은 비파괴 보존(누적 학습 손실 방지·local.conf 백업 철학과 같은 결).
    playbook_status = import_fill.ensure_pm_playbook_local_stub(
        dest_root, backup_root, root_identity=root_identity)
    if playbook_status == "created":
        print(f"✓ pm_playbook.local.md 스텁 생성 ({import_fill.PM_PLAYBOOK_LOCAL_RELPATH})")
    elif playbook_status == "preserved":
        print("  pm_playbook.local.md 기존 파일 비파괴 보존 (인스턴스 소유 — 덮지 않음).")

    # dest 가 git repo(git_safe is not None)이고 이번에 중앙 백업 디렉토리가
    #   실제로 만들어졌으면, .gitignore 가 `.pm_import_backups/` 를 무시하지 않을 때 1줄 append
    #   — 백업이 git status 를 오염시키지 않게 한다. 비-git/미생성/이미 무시 중이면 skip(멱등).
    if not is_new and git_safe is not None and backup_root is not None and backup_root.exists():
        gi_status = install_board.ensure_backup_dir_gitignored(dest_root, git_safe, copied_relpaths)
        if gi_status in ("added", "created"):
            print(f"✓ .gitignore 에 {install_rules.BACKUP_DIR_NAME}/ 추가 (백업이 git status 오염 방지)")
        elif gi_status == "unsafe-skip":
            print(f"  ⚠️ .gitignore 가 미추적/변경 상태 — 비파괴 위해 자동 추가 생략. "
                  f"수동으로 `{install_rules.BACKUP_DIR_NAME}/` 한 줄을 추가하세요.")

    install_conf.print_conf_migration_notice(dest_root)
    print(f"✓ import 완료: {dest_root}")
    print("  다음: (--fill auto 했으면) 자유서술 placeholder 제안 검토·반영 → 기준 커밋 → "
          "`pm-config worktree add <repo> --user-ack <repo>` → "
          f"`{skill_inventory._runtime_skill_entry('pm-bootstrap')} --task <task>`.")
    # claude 는 프로젝트 trust 수락 전 settings.json permissions.allow 를 조용히 무시한다.
    if "claude" in args.harness:
        add_harness._print_claude_trust_guidance()
    # codex 어댑터는 laydown 후 trusted project + hook trust 2단계 승인이 있어야 발화.
    if "codex" in args.harness:
        add_harness._print_codex_trust_guidance()
    return 0
