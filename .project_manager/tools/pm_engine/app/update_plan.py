"""갱신 계획·drift·엔진 코드 전달 계획의 단일 계산 (층: app)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import filecmp
import sys
from pm_engine.app import update_manifest
from pm_engine.app import update_render
from pm_engine.domain import manifest_rules
from pm_engine.domain import upstream_rules
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import manifest_store
from pm_engine.infra import update_paths


def plan(
    home,
    source_root: Path,
    manifest: list,
    dest_root: Path | None = None,
    *,
    render_enabled: bool = True,
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
    entry_notation_templates: dict[str, tuple[str, ...]] | None = None,
    manifest_source_text: str | None = None,
    inventory_out: set[str] | None = None,
    dest_inventory_out: set[str] | None = None,
    guest_backfill_lines: list[str] | None = None,
    conf: Mapping[str, str],

) -> tuple[list[tuple], list[str]]:
    """(changes, missing) 반환. changes = [(rel, src, dst, kind)] (kind: new|update).

    home: 실행 Home — 카드 렌더의 코드 inventory 와 조회 위치.
    dest_root: 동기화 대상 루트. None 이면 실행 Home 루트(self-location) 사용.

    conf: render path 판정에 쓸 local.conf 값. 갱신은 dest local.conf 를, 저장(`apply_conf`)은 쓰기 전의
    제안 값을 넘긴다 — 계획이 디스크를 따로 읽지 않는다.

    계획과 적용은 같은 실행 Home 을 쓰며, 모델 목록 조회 기억은 contract가 소유한다.

    manifest 항목이 `ManifestEntry`(render 플래그 운반·read_manifest 산출)면 그 path 의 render
    여부를 dst(`_RenderDst` 래퍼)에 실어 apply 가 byte-copy vs render 를 분기하게 한다. 평문
    str 항목(레거시 호출)은 render=False(후방호환·순수 copy2). render path 의 변경검출은
    filecmp 대신 rendered-output 비교(`_render_eq_dst`) — 템플릿≠산출물 오보 회피.

    render_enabled=False 면 operational 전체 렌더는 끈다. 다만 entry notation context가 있으면
    ``@render`` 어댑터와 공유 canonical wiki의 `/pm-*` 호출 토큰만 타깃 표기로 바꾼다. 프로젝트명
    등 나머지 토큰-form은 그대로 보존한다. 채택자 self-update에서도 같은 항목별 하네스 context를
    넘겨 canonical 스킬 표기가 되돌아오지 않게 한다.

    ``inventory_out``을 주면 이번 계획이 훑은 **출하 파일 좌표**(dest relpath + source relpath)를
    거기에 모은다. 변경 0인 파일도 담기므로 "요청 경로가 실제로 존재하는가"를 changes 유무와 독립
    으로 판정할 수 있다(경로 스코프의 오타 검출 — 별도 재열거 없이 같은 열거를 재사용).

    ``dest_inventory_out``은 같은 열거의 **dest 좌표만** 모은다. 은퇴 판정처럼 dest 잔존물과 대조하는
    소비자는 상류 좌표가 섞이면 우연한 문자열 일치로 후보를 놓치므로(false negative) 좌표축을 나눈다.

    ``guest_backfill_lines``는 apply 가 guest 절에 기록할 파생 엔진 행이다. self-prop 판정에만 쓴다 —
    기록 지점이 apply 한 곳뿐이라 그 change 가 계획에 없으면 파생분이 영원히 기록되지 않는다.
    """
    effective_dest = dest_root if dest_root is not None else home.root
    changes: list[tuple] = []
    missing: list[str] = []
    for entry_index, entry in enumerate(manifest):
        rel = str(entry)
        if rel.replace("\\", "/") == manifest_rules._MANIFEST_SELF_REL and manifest_source_text is not None:
            # 선택 flavor 합집합은 upstream에 단일 실파일로 존재하지 않는다. 인메모리 source를
            # change tuple에 실어 self-prop가 합집합 전체를 설치/갱신하게 한다.
            if inventory_out is not None:
                # 이 분기도 **계획이 다루는 좌표**다 — 빼면 `--paths .project_manager/engine.manifest`
                #   가 유효 변경인데도 "대응 없음" 으로 거부된다(합집합 경로에만 생기는 갭).
                inventory_out.add(rel.replace("\\", "/").strip("/"))
            if dest_inventory_out is not None:
                dest_inventory_out.add(rel.replace("\\", "/").strip("/"))
            dst = update_paths._RenderDst(
                effective_dest / rel, False,
                guest_backfill_lines=guest_backfill_lines)
            source = update_paths._ManifestTextSource(manifest_source_text)
            if not dst.exists():
                changes.append((rel, source, dst, "new"))
            else:
                try:
                    # 판정은 단일 flavor 분기와 **같은 헬퍼**다 — guest 절 차감 core 비교 + 옛 세대
                    #   마커. 여기만 마커 축이 빠지면 다중-harness 형상에서 마커 세대가 영구 잔존한다.
                    if update_manifest._manifest_self_prop_needs_update(
                            fs.read_text_shared(Path(dst), encoding="utf-8"), manifest_source_text,
                            guest_backfill_lines):
                        changes.append((rel, source, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    changes.append((rel, source, dst, "update"))
            continue
        declared_render = manifest_rules._entry_render_flag(entry)
        item_notation_template = (
            (entry_notation_templates or {}).get(rel.replace("\\", "/"))
            or entry_notation_template
        )
        # 전체 render는 adopter self-update에서만. --target은 아래 호출 표기 최소 렌더만 허용한다.
        render = declared_render if render_enabled else False
        inventory, source_missing, _target_owned = manifest_store.manifest_entry_shipping_inventory(
            source_root,
            manifest,
            entry_index,
            effective_dest,
        )
        if source_missing:
            # 부재 보고는 manifest(dest) 경로(rel) — missing-핸들러가 @target-owned 플래그를
            # str(entry) key 로 조회한다. @source 항목은 non-@target-owned → source 부재면 rc2
            # (템플릿 누락 은폐 금지·안전판).
            missing.append(rel)
            continue
        for r, sp in inventory:
            if inventory_out is not None:
                # 이 계획이 **실제로 다루는** 좌표(dest·source 양쪽) — 변경 유무와 무관하게 모은다.
                inventory_out.add(str(r).replace("\\", "/").strip("/"))
                try:
                    inventory_out.add(Path(sp).relative_to(source_root).as_posix())
                except ValueError:
                    pass
            if dest_inventory_out is not None:
                dest_inventory_out.add(str(r).replace("\\", "/").strip("/"))
            # render 대상 판정 = @render manifest 선언 + **텍스트로 읽히는가**.
            # 옛 `.md` 확장자 하드 필터는 제거했다: 확장자 열거는 manifest 선언을 덮는 중복
            # 판정이라, codex 가 들여온 `.codex/agents/*.toml`(@render 선언 O)이 byte-copy 로
            # 새어 채택자 트리에 `{{PROJECT_NAME}}` 리터럴을 재전파했다(pm_import 와 동형 결함·
            # 두 채널을 함께 닫는다). 텍스트 아님(바이너리 리소스)은 여전히 byte-copy 로 남는다.
            text_source = update_render._is_text_source(sp)
            file_render = render and text_source
            # flat command rewrite는 canonical SKILL.md를 개별 command 파일로 remap한 행만 대상.
            # `.opencode/command` 디렉터리 자체를 소스로 가진 기존 채택자/합성 어댑터 행은 이미
            # command-native 내용이므로 재해석하지 않는다.
            flat_command_skill = (
                update_render._flat_command_skill_name(r)
                if str(rel).replace("\\", "/") == str(r).replace("\\", "/")
                else None
            )
            codex_operational_skill = update_render._codex_operational_skill_name(r)
            notation_template = None
            rendered_entry: str | None = None
            notation_managed = declared_render or (
                render_enabled
                and str(r).replace("\\", "/").startswith(".project_manager/wiki/")
            )
            if (
                not file_render
                and notation_managed
                and text_source
                and (
                    item_notation_template is not None
                    or flat_command_skill is not None
                    or codex_operational_skill is not None
                )
            ):
                # 변환 지점이 실제로 있는 파일만 생성 산출물로 표시한다. 토큰 부재 파일은 기존
                # copy2 경로를 유지해 metadata/출력 churn을 만들지 않는다. 미등록 template 값은
                # helper가 여기서 fail-loud한다(dry-run도 원문 복사 false-green 금지).
                source_text = fs.read_text_shared(Path(sp), encoding="utf-8")
                rendered_entry = source_text
                if item_notation_template is not None:
                    rendered_entry = update_render._render_skill_entry_text(home, sp, item_notation_template)
                if flat_command_skill is not None:
                    rendered_entry = update_render._render_flat_command_reference(
                        rendered_entry, flat_command_skill
                    )
                if codex_operational_skill is not None:
                    rendered_entry = update_render._render_codex_operational_detail(
                        rendered_entry, codex_operational_skill
                    )
                if (
                    item_notation_template is not None
                    and rendered_entry != source_text
                ):
                    notation_template = item_notation_template
            dst = update_paths._RenderDst(
                effective_dest / r,
                file_render,
                entry_notation_template=(
                    item_notation_template if file_render else notation_template
                ),
                flat_command_skill=flat_command_skill,
                codex_operational_skill=codex_operational_skill,
                # guest 절 기록은 self-prop 분기에서만 일어난다 — 전 change 에 실으면 낭비다.
                guest_backfill_lines=(
                    guest_backfill_lines
                    if str(r).replace("\\", "/") == manifest_rules._MANIFEST_SELF_REL
                    else None
                ),
            )
            if not dst.exists():
                changes.append((r, sp, dst, "new"))
            elif file_render:
                # render path: 템플릿이 산출물과 byte-equal 일 수 없으므로 filecmp 는 항상 오보.
                # 렌더한 결과가 dst 와 다를 때만 update(정직 판정).

                if not update_render._render_eq_dst(
                    home, sp, dst, effective_dest, conf, item_notation_template,
                    flat_command_skill,
                    codex_operational_skill,
                ):
                    changes.append((r, sp, dst, "update"))
            elif (
                (
                    notation_template is not None
                    or flat_command_skill is not None
                    or codex_operational_skill is not None
                )
                and rendered_entry is not None
            ):
                # 대조 축은 전면 렌더 분기와 **같은 헬퍼**다 — 여기만 raw bytes 로 보면 CRLF
                #   체크아웃에서 내용 동일 파일이 매 실행 update 로 계획된다(쓰기는 표기 보존이라
                #   수렴 불가).
                if not update_render._rendered_text_matches_dest(rendered_entry, dst):
                    changes.append((r, sp, dst, "update"))
            elif str(r).replace("\\", "/") == manifest_rules._MANIFEST_SELF_REL:
                # engine.manifest self-prop — 판정(guest 절 차감 core 비교 + 옛 세대 마커)은
                #   다중-harness 합집합 분기와 **같은 헬퍼**를 쓴다(사유·근거는 그 docstring).
                try:
                    if update_manifest._manifest_self_prop_needs_update(
                            fs.read_text_shared(Path(dst), encoding="utf-8"),
                            fs.read_text_shared(Path(sp), encoding="utf-8"),
                            guest_backfill_lines):
                        changes.append((r, sp, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    if not filecmp.cmp(sp, dst, shallow=False):
                        changes.append((r, sp, dst, "update"))
            elif not filecmp.cmp(sp, dst, shallow=False):
                changes.append((r, sp, dst, "update"))
    return changes, missing


class _EngineSyncNotationContextError(RuntimeError):
    """선택된 flavor manifest 에서 notation 템플릿 context 를 해소하지 못함 — 원본 사유를 감싼다.

    `plan()` 자신이 낼 수 있는 다른 `RuntimeError`(예 `EmptyShippingInventoryError`)와 구분되는
    전용 타입이다 — 호출부(`_main`·`drift_changes`)가 "notation context 실패"만 좁게 받아 처리
    (에러+rc2 대 None 판정 불능)하고, `plan()` 이 낸 무관한 `RuntimeError` 는 그대로 전파시킨다
    (엔진 사본 skew 는 이 타입으로 감싸지 않는다 — 마커를 잃지 않도록 원본 그대로 재-raise 한다).
    """


def _resolve_engine_sync_plan(
    home,
    effective_dest: Path,
    source_root: Path,
    *,
    target: str | None = None,
    guest_entries: list | None = None,
    guest_backfill: list | None = None,
    guest_backfill_flavors: list[str] | None = None,
    inventory_out: set[str] | None = None,
    dest_inventory_out: set[str] | None = None,
    report: bool = False,
    conf: Mapping[str, str],

) -> dict:
    """manifest 자기치유 → notation 템플릿 해소 → guest backfill 기록 판정 → `plan()` 을 한
    호출로 묶어 반환하는 **구조화 계획**. `_main`(self-update·`--target` export 공용)과 드리프트
    게이트(`drift_changes`)가 이 함수를 실제로 공동 호출한다 — 별도 diff/계획 구현을 두지 않는다.
    `conf` 는 `plan()` 의 render path 판정 값이다(저장 `apply_conf` 는 쓰기 전의 제안 값을 넘긴다).

    `guest_entries`/`guest_backfill`/`guest_backfill_flavors` 는 호출부가 이미 해소했으면 넘기고
    (중복 IO 회피), 없으면(`guest_backfill is None`) 여기서 해소한다.

    `report=True` 면 `_main` 이 기존에 내던 것과 **같은 지점·같은 문구**로 manifest merge conflict·
    guest backfill 파생 안내를 stdout/stderr 에 낸다. `report=False`(기본값)는 아무것도 출력하지
    않는다 — `drift_changes` 는 이 값으로 호출해 출력·부작용 0 을 지킨다.

    manifest 해소 실패(`FileNotFoundError`)·notation context 해소 실패(`_EngineSyncNotationContextError`
    — `RuntimeError` 서브클래스)는 삼키지 않고 그대로 전파한다 — 호출부마다 처리(에러 메시지+rc2
    대 None 판정 불능)가 다르기 때문이다. `plan()` 이 낼 수 있는 다른 `RuntimeError`(예
    `EmptyShippingInventoryError`)는 감싸지 않고 그대로 전파한다(호출부의 좁은 catch 를 우회하지
    않는다).

    반환 dict:
      - "changes": `plan()` 의 (rel, src, dst, kind) 튜플 리스트
      - "missing": `plan()` 의 non-`@target-owned` source 부재 relpath 리스트 (버리지 않는다 —
        buried 하면 확정 drift 0 과 판정 불능이 뒤섞인다)
      - "manifest": 계획에 쓰인 manifest
      - "selfheal": `resolve_manifest_selfheal` 결과(`target` 모드는 skip 상수)
      - "persisted_guest_backfill": apply 가 절에 기록할 manifest-line 리스트(`legacy_preserved`
        면 None)
      - "entry_notation_templates": 계획에 쓰인 notation 템플릿 dict
    """
    if guest_entries is None:
        guest_entries = update_manifest._dest_guest_manifest_entries(effective_dest)
    if guest_backfill is None:
        guest_backfill, guest_backfill_flavors = update_manifest._guest_engine_backfill_entries(
            effective_dest, source_root, guest_entries)
    elif guest_backfill_flavors is None:
        guest_backfill_flavors = []

    selfheal: dict = {
        "status": "skipped", "added": [], "removed": [],
        "manifest": None, "upstream_manifest": None,
        "upstream_manifests": [], "manifest_text": None,
        "merge_conflicts": [],
    }
    if not target:
        # 파생 경로를 함께 넘긴다 — 이번 실행이 등재할 파일을 "채널 없음" 으로 경고하면 같은 run 이
        #   자기모순을 낸다(legacy 코호트 첫 실행). 이미 해소한 산출이라 재파생도 없다.
        selfheal = update_manifest.resolve_manifest_selfheal(
            effective_dest, source_root,
            guest_backfill_paths={
                str(entry).replace("\\", "/") for entry in guest_backfill})
        if report:
            update_manifest._print_manifest_merge_conflicts(selfheal)

    # 계획 기준 manifest — 승격분 우선·없으면 dest 우선 로컬 해소. `--changes` 미리보기가 **같은
    #   헬퍼**를 소비한다(판정 사본 0: 미리보기 분류 기준 == 적용 계획 기준). FileNotFoundError 는
    #   호출부가 처리한다(진입별로 rc 가 다르다 — sync=rc1 에러, drift 게이트=None 판정 불능).
    manifest = update_manifest._resolve_planning_manifest(
        effective_dest, source_root, selfheal,
        guest_entries=guest_entries, guest_backfill=guest_backfill)

    # 파생 엔진 행의 **기록** 여부 — 전파는 아래 `plan()` 이 이미 계획에 싣고, 여기선 절에 남길지만
    #   정한다. `legacy_preserved`(로컬 manifest 불가침 선언)는 기록만 생략한다(선언 존중·파일
    #   동기는 유지). 그 밖에는 apply 의 절 재부착에 실어, 다음 실행부터 추론 없이 provenance 로
    #   해소되게 한다.
    persisted_guest_backfill: list[str] | None = None
    if guest_backfill:
        recorded = {str(entry).replace("\\", "/") for entry in guest_entries}
        fresh = sorted(
            str(entry).replace("\\", "/") for entry in guest_backfill
            if str(entry).replace("\\", "/") not in recorded
        )
        if selfheal["status"] == "legacy_preserved":
            if report:
                print("  ⚠️ guest 엔진 행은 동기하되 guest 절에는 기록하지 않는다 — 로컬 manifest "
                      "불가침(legacy_preserved) 선언을 존중한다. 절 기록은 이 인스턴스가 legacy "
                      "상태를 벗어난 뒤 다음 sync 에서 이뤄진다.", file=sys.stderr)
        else:
            persisted_guest_backfill = [
                manifest_rules._manifest_entry_line(entry) for entry in guest_backfill]
        if fresh and report:
            print(f"  guest 엔진 행 {len(fresh)}건 파생 "
                  f"(flavor: {', '.join(guest_backfill_flavors)}): {', '.join(fresh)}")

    # --target은 operational 전체 render를 끄되 @render 경로의 호출 표기 토큰만 target 이름으로
    # 조건부 렌더한다. adopter self-update는 선택된 flavor manifest 경로에서 같은 context를 파생한다.
    # root canonical manifest는 flavor template가 아니므로 None(기존 `/` token-form 유지).
    render_enabled = not target
    entry_notation_template = target if target else None
    try:
        notation_manifests = (
            []
            if target
            else update_manifest._installed_entry_notation_manifests(
                effective_dest, source_root, selfheal.get("upstream_manifests") or [])
        )
        entry_notation_templates = (
            {}
            if target
            else manifest_store._entry_notation_templates_from_manifests(notation_manifests, source_root)
        )
    except RuntimeError as exc:
        # 엔진 사본 skew 는 감싸지 않고 원본 그대로 재-raise 한다(마커 보존) — `plan()` 자신이
        #   낼 수 있는 다른 `RuntimeError`(예 `EmptyShippingInventoryError`)와 이 지점의 notation
        #   context 실패를 구분해야 호출부가 좁게 처리한다(F-001 추출 전 `_main` 의 try/except
        #   경계와 동형 — 넓게 잡으면 `plan()` 의 무관한 예외까지 이 지점 처리로 오분류된다).
        if getattr(exc, "_engine_rev_skew", False):
            raise
        raise _EngineSyncNotationContextError(str(exc)) from exc

    changes, missing = plan(
        home, source_root, manifest, dest_root=effective_dest,
        render_enabled=render_enabled,
        entry_notation_template=entry_notation_template,
        entry_notation_templates=entry_notation_templates,
        manifest_source_text=(
            selfheal.get("manifest_text")
            if len(selfheal.get("upstream_manifests", [])) > 1 else None
        ),
        inventory_out=inventory_out,
        dest_inventory_out=dest_inventory_out,
        guest_backfill_lines=persisted_guest_backfill,
        conf=conf,

    )
    return {
        "changes": changes,
        "missing": missing,
        "manifest": manifest,
        "selfheal": selfheal,
        "persisted_guest_backfill": persisted_guest_backfill,
        "entry_notation_templates": entry_notation_templates,
    }


def drift_changes(home, engine_root: Path) -> list[str] | None:
    """`engine_root` 의 실행 엔진 사본이 upstream 과 drift 한 relpath 목록.

    엔진 코드 행(`_engine_code_plan`, 전달 프로세스와 같은 계획)을 먼저 보고, 변경이 있으면 그 목록을
    돌려준다. 없을 때만 `_resolve_engine_sync_plan`(완료 단계 dry-run 이 실제로 호출하는 것과 **같은
    함수**)으로 간다 — 별도 diff 알고리즘을 두지 않는다(render 설정·notation·guest-backfill 인자도
    dry-run 과 동일하게 태운다).

    판정 불능(`None`) — 아래는 전부 같은 "이 형상엔 게이트가 적용되지 않는다"로 수렴한다(거부가
    아니라 무판정):
      - `local.conf` 자체가 없음(빈 dict 로드 → upstream 키도 없음)
      - upstream 키가 비어 있음(미등록)
      - upstream 이 URL(릴리스 추적 기본값) — 이 게이트는 lint 경로라 **네트워크 0** 을 유지한다
        (`pm-config update` 의 URL 캐시를 여기로 넓히지 않는다 — lint 가 네트워크에 의존하게 된다).
        URL 형상의 실제 흡수는 `pm-update` 가 캐시로 수행한다.
      - upstream 경로가 디렉토리로 존재하지 않음(이동/삭제)
      - manifest 부재(engine_root·source_root 양쪽 다 `engine.manifest` 없음)
      - 선택된 flavor manifest 를 읽을 수 없음(notation context 해소 실패)
      - manifest 등재 source 가 non-`@target-owned` 인데 upstream 에 없음(missing>0) — 확정
        drift 0 과 판정 불능을 구분한다(버리면 그 형상을 drift 0 으로 오판하는 false-green).

    출력·부작용 0(stdout·파일 쓰기) — `_resolve_engine_sync_plan(report=False)` 로 호출한다.
    """
    engine_root = Path(engine_root)
    local_conf = engine_root / ".project_manager" / "local.conf"
    conf = install_conf._read_local_conf(local_conf)
    stored = conf.get("upstream.path", "").strip()
    if not stored:
        return None
    if upstream_rules.classify_upstream(stored) == "url":
        return None
    source_root = Path(stored).resolve()
    if not source_root.is_dir():
        return None
    try:
        # 엔진 코드 행을 먼저 본다 — 엔진 코드가 뒤처진 트리에서 설치 렌더러로 다음 세대 템플릿을
        #   판정하면 그 렌더가 실패한다. 엔진 코드 변경이 있으면 그 목록이 곧 drift 다.
        engine = _engine_code_plan(home, engine_root, source_root)
        if engine["missing"]:
            return None
        if engine["changes"]:
            return [str(rel) for rel, _src, _dst, _kind in engine["changes"]]
        sync_plan = _resolve_engine_sync_plan(home, engine_root, source_root, report=False, conf=conf)
    except FileNotFoundError:
        return None
    except _EngineSyncNotationContextError:
        return None
    except RuntimeError as exc:
        # 엔진 사본 skew(마커 보존을 위해 `_resolve_engine_sync_plan` 이 원본 그대로 재-raise 함)도
        #   여기서 판정 불능으로 접지 않는다 — 이 게이트는 "실행 엔진 사본이 upstream 과 갈렸는가"
        #   를 묻는데, 사본끼리 rev 가 갈린 트리는 그 질문의 **가장 강한 양성**이다. `None` 은
        #   호출부에서 "이 형상엔 게이트가 적용되지 않음(무차단)" 으로 읽히므로, 접으면 게이트가
        #   잡으라고 만들어진 그 형상에서만 조용히 통과한다(false-green). 흡수 없이 올려 사본
        #   재동기 안내를 그대로 보이고, `plan()` 의 다른 RuntimeError(예
        #   `EmptyShippingInventoryError`)도 종전대로 전파한다.
        raise
    missing = sync_plan["missing"]
    if missing:
        # `@target-owned` 항목의 source 부재는 정상(타깃 고유 어댑터·엔진 upstream 에 없음) — `_main`
        #   의 skip 판별과 같은 술어(`_entry_target_owned_flag`)를 쓴다(별도 판정 사본 없음). 그 밖의
        #   (non-`@target-owned`) 부재만 판정 불능이다 — 여기까지 `@target-owned` 를 섞으면 정상
        #   self-update 형상(cross-flavor 어댑터 보유)도 과도하게 "판정 불능" 으로 접힌다.
        owned = {str(entry): manifest_rules._entry_target_owned_flag(entry) for entry in sync_plan["manifest"]}
        if any(not owned.get(rel, False) for rel in missing):
            return None
    return [str(rel) for rel, _src, _dst, _kind in sync_plan["changes"]]


def _engine_code_plan(home, effective_dest: Path, source_root: Path) -> dict:
    """엔진 코드 행의 계획 — `{"changes", "missing", "manifest", "selfheal"}`.

    계획 manifest 해소(guest 절·자기치유·은퇴 지시 검증 포함)는 완료 단계와 같은 함수를 쓰고, 계획은
    엔진 코드 행만 렌더 없이 한다(엔진 코드 행에는 표식이 없어 렌더러를 읽지 않는다). 전달 프로세스가
    옮길 대상이고, board 드리프트 검사(`drift_changes`)가 먼저 보는 축이다. `manifest` 는 해소된 계획
    manifest 전체다(부재 판정이 `@target-owned` 표식을 찾는다)."""
    guest_entries = update_manifest._dest_guest_manifest_entries(effective_dest)
    guest_backfill, _flavors = update_manifest._guest_engine_backfill_entries(
        effective_dest, source_root, guest_entries)
    selfheal = update_manifest.resolve_manifest_selfheal(
        effective_dest, source_root,
        guest_backfill_paths={str(entry).replace("\\", "/") for entry in guest_backfill})
    manifest = update_manifest._resolve_planning_manifest(
        effective_dest, source_root, selfheal,
        guest_entries=guest_entries, guest_backfill=guest_backfill)
    changes, missing = plan(
        home, source_root, [entry for entry in manifest if manifest_rules._is_engine_code(entry)],
        dest_root=effective_dest, render_enabled=False, conf={})
    return {"changes": changes, "missing": missing, "manifest": manifest, "selfheal": selfheal}
