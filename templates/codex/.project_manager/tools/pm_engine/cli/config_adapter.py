"""어댑터 설정 상태 표시·명시 수용·rc 번역 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
import sys
from pm_engine.app import adapter_accept
from pm_engine.app import adapter_report
from pm_engine.app import harness_ctx
from pm_engine.app import hook_sets
from pm_engine.app import repo_protection
from pm_engine.domain import install_rules
from pm_engine.infra import upstream_git


# instance-owned 어댑터 config 판정 → 사람이 읽을 라벨. 판정 이름 자체는 app.adapter_report 소유이고
# 여기선 표시만 한다(판정 사본 0). ⚠ **라벨은 mode 와 함께 판정한다** — `unedited` 는 managed 에서만
# 자동 갱신을 뜻하고 report 대상에선 "무편집이지만 갱신 안 함" 이다(status 만 보면 거짓 안내).
_ADAPTER_CONFIG_STATUS_LABEL = {
    "in-sync": "최신 (template 과 동일)",
    "unedited": "무편집 — 다음 동기가 자동 갱신",
    "edited": "채택자 편집 — 보존(수용은 --accept)",
    "unrecorded": "원장 부재 — 보존(수용은 --accept)",
}
# 보고-전용 대상의 override 라벨(그 밖의 status 는 위 공통 라벨을 그대로 쓴다).
_ADAPTER_CONFIG_REPORT_LABEL = {
    "unedited": "무편집 — 보고 전용(자동 갱신 안 함·수용은 --accept)",
    "edited": "채택자 편집 — 보고 전용(수용은 --accept)",
    "unrecorded": "원장 부재 — 보고 전용(수용은 --accept)",
}
# 수용 결과 → rc·안내. `accepted` 만 성공이고 나머지는 사유를 그대로 노출한다(거짓 성공 금지).
_ADAPTER_ACCEPT_FAILURE_HINT = {
    "raced": "다시 실행해 새 판정을 받아라(그 사이 파일이 바뀌었다).",
    "ledger-blocked": "엔진을 갱신한 뒤 다시 실행하라(원장이 더 새로운 형식이다).",
    "write-failed": "경로 권한/디스크 상태를 확인한 뒤 다시 실행하라.",
    "ledger-failed": "원장 경로 권한을 고친 뒤 다시 실행하라(파일 내용은 이미 상류 값이다).",
}


def _adapter_accept_decision(home, dest: Path, source_root: Path, relpath: str):
    """수용 전 세대 판정 — 막을 사유 + 판정에 쓴 template 해시·선언 snapshot digest.

    **mutation 게이트라 상류 세대 선언으로 판정한다**: 설치본 선언은 상류가 이번에 들여오는 새
    플래그를 모르므로, 그 위에서 config 를 앞세우면 게이트가 있으나 마나다. 상류를 못 읽으면
    엔진이 fail-closed 로 blocker 를 낸다(모르면 멈춘다)."""
    return hook_sets.hook_set_accept_decision(
        dest, source_root, relpath,
        declarations=hook_sets.hook_set_declarations(
            home, source_root, required=True))


def _adapter_accept_order_blocked(decision, relpath: str) -> bool:
    """엔진 파일 선행 순서를 어기는 수용인가 — 어기면 처방을 내고 True(파일 미변경).

    수용은 dest config 를 상류 세대로 **앞세우는** 행위다. 그 세대가 요구하는 래퍼/드라이버가
    dest 에 아직 없으면 훅이 미지원 플래그를 rc2 로 거부하고, 그 rc 는 도구 차단으로 번역된다
    (v1.7.0 흡수 실측 락아웃). 위험 방향은 이쪽 하나뿐이라 "엔진 파일 선행 · config 후행"
    순서를 여기서 기계가 강제한다 — 단건 `--accept` 와 세트 수용이 같은 게이트를 탄다."""
    if not decision.blockers:
        return False
    print(f"[중단] 어댑터 config 수용 거부 ({relpath}) — 이 세대가 요구하는 훅 파일이 아직 "
          "dest 에 설치돼 있지 않다(파일을 바꾸지 않았다).", file=sys.stderr)
    for finding in decision.blockers:
        print(f"  - {finding.detail}", file=sys.stderr)
    print("  → pm-update 로 엔진 파일을 먼저 받은 뒤 이 수용을 다시 실행하라 "
          "(엔진 파일 선행 · config 후행).", file=sys.stderr)
    return True


def _accept_adapter_config_one(home, dest: Path, source_root: Path,
                               relpath: str, judgments) -> int:
    """config 한 개 수용 — 선행조건 게이트 → 원자 교체 → rc·안내 (단건·세트 공용 경로).

    단건과 세트가 이 함수 하나만 호출하므로 게이트·백업·경쟁 판정·안내가 갈릴 수 없다."""
    decision = _adapter_accept_decision(home, dest, source_root, relpath)
    if _adapter_accept_order_blocked(decision, relpath):
        return 1
    # 수용은 **판정을 먼저 받고** 그 시점 해시를 넘긴다 — 판정과 쓰기 사이에 파일이 바뀌면
    #   엔진이 raced 로 중단한다(검증 없는 덮어쓰기 0). 축은 둘이다: dest(채택자 동시 편집)와
    #   **template**(게이트가 검사한 bytes 와 실제 복사할 bytes 의 결속) — 후자가 없으면 상류가
    #   그 사이 바뀌어도 "검사한 세대" 와 다른 내용이 설치된다.
    judged = next((item for item in judgments if item.relpath == relpath), None)
    try:
        # 스냅샷 결속은 **둘이 한 벌**이다(상류 config bytes + 선언 package snapshot digest).
        outcome = adapter_accept.accept_adapter_config(
            dest, source_root, relpath,
            expected_sha256=judged.dest_sha256 if judged is not None else None,
            expected_template_sha256=decision.template_sha256,
            expected_generation_sha256=decision.generation_sha256)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"오류: 어댑터 config 를 교체하지 못했습니다 ({relpath}: {exc}) — "
              "원본은 그대로입니다.", file=sys.stderr)
        return 1
    if outcome.status != "accepted":
        print(f"오류: 어댑터 config 수용 실패 ({relpath} · {outcome.status}): "
              f"{outcome.detail}", file=sys.stderr)
        hint = _ADAPTER_ACCEPT_FAILURE_HINT.get(outcome.status)
        if hint:
            print(f"  → {hint}", file=sys.stderr)
        if outcome.backup is not None:
            print(f"  백업: {_dest_relative_label(outcome.backup, dest)}", file=sys.stderr)
        return 1
    print(f"✓ 어댑터 config 수용: {relpath} "
          f"(백업 {_dest_relative_label(outcome.backup, dest)})")
    print("  원장 기록까지 확인했다 — 다음 동기부터 무편집 판정(자동 갱신 궤도)이다.")
    note = install_rules.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(relpath)
    if note:
        print(f"  ⚠️ {note}")
    if relpath in harness_ctx.HARNESS_CTX_FILES.values():
        # 템플릿 값으로 돌아간 관리 키에 local.conf 의 사용 한도를 다시 쓴다(원장이 방금 증언한
        #   파일이라 반영 뒤에도 무편집 판정이 유지된다). 설치·add-harness 와 같은 한 번이다 —
        #   옛 키가 남은 conf 면 반영을 건너뛰고 알리며, 검사 거부만 rc 1(설정 파일 무변경)이다.
        try:
            harness_ctx._print_harness_ctx_apply(dest)
        except harness_ctx.HarnessCtxError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
    return 0


# 세트 수용에서 빼는 판정 → 제외 사유(출력 문구). 여기 없는 판정만 세트가 받는다 —
# 실질적으로 `unedited`(원장으로 무편집이 확인된 파일) 하나다.
_ADAPTER_SET_ACCEPT_EXCLUDED = {
    "edited": "채택자 편집분",
    "unrecorded": "원장 부재로 편집 여부 판정 불가",
}


def _warn_hook_set_query_fallback(generation) -> None:
    """조회 축 강등 사유 표면화 — 상류 선언을 못 읽어 설치본 세대로 판정했다는 사실을 남긴다.

    조회 축은 관대 계약이라 차단하지 않는다(판정을 통째로 잃는 것보다 한 세대 뒤 선언으로라도 보는
    편이 낫다). 다만 사유까지 버리면 `--check` 가 **상류 전용 플래그를 못 본 채** 무경고 green 이라,
    채택자는 강등된 판정을 정상 판정으로 읽는다 — 차단이 아니라 침묵만 제거한다(mutation 축의
    fail-closed 와 대비).

    **문구는 `app.hook_sets` 단일 진실**(`hook_set_query_fallback_lines` — `hook_set_remedy_lines` 와
    같은 규약)이다."""
    for line in hook_sets.hook_set_query_fallback_lines(generation):
        print(line, file=sys.stderr)


def _report_hook_set_findings(home, dest: Path, source_root: Path) -> bool:
    """훅 세트 세대 불일치를 stderr 로 내고 하나라도 있으면 True (`--check` 소비·write 0).

    판정·처방은 `app.hook_sets` 단일 진실을 그대로 쓴다(pm-update 와 같은 문구). 선언은 **상류
    세대**를 우선한다(같은 해소 지점) — 조회 성격이라 상류를 못 읽으면 설치본 선언으로 내려가되
    **그 사유는 알린다**(무경고 green 금지)."""
    generation = hook_sets.hook_set_declarations(home, source_root)
    _warn_hook_set_query_fallback(generation)
    findings = hook_sets.judge_adapter_hook_sets(
        dest, source_root, declarations=generation.declarations)
    for finding in findings:
        print(f"[중단] 어댑터 훅 세트 세대 불일치({finding.harness}) — {finding.detail}.",
              file=sys.stderr)
        for line in hook_sets.hook_set_remedy_lines(finding):
            print(f"    → {line}", file=sys.stderr)
    return bool(findings)


def _accept_adapter_config_set(home, dest: Path, source_root: Path,
                               judgments) -> int:
    """세트 수용 — 대상 선정·선-표시·파일당 수용 (`--accept-all`).

    선정 규칙 둘:
      - 이미 상류와 같은 파일은 후보에서 빠진다(바꿀 게 없다·byte churn 0).
      - **채택자 값이 걸린 판정은 기본 제외**한다. report 모드 대상(settings.json·
         opencode.jsonc·config.toml)에는 권한 allowlist·모델·threshold 같은 실 노브가 들어 있고,
         세트 커맨드 한 번이 그것을 무지목 일괄 교체하면 "채택자 커스텀은 안 덮는다" 하한선이
         깨진다. 제외 대상은 둘이다 — `edited`(편집이 확인됨)와 `unrecorded`(원장이 없어 편집
         여부를 **판정할 수 없음**). 후자를 받으면 원장 도입 전 구세대 채택자의 커스텀이 조용히
         사라진다(모르면 덮지 않는다). 둘 다 단건 `--accept <경로>` 로만 받는다(제외 사유를 함께
         낸다). 결국 세트가 받는 건 `unedited` — 설치가 내려놓은 그대로임이 원장으로 확인된
         파일뿐이다.

    무엇을 건드릴지 **먼저 전부 보이고** 나서 파일당 수용한다. 파일 1개 단위는 기존 원자 교체를
    그대로 재사용하고 다중 파일 롤백은 만들지 않는다 — 순서 게이트 + per-file 원자로 관측
    클래스가 닫히므로 상태 기계는 과설계다."""
    candidates = [item for item in judgments if item.status != "in-sync"]
    excluded = [item for item in candidates
                if item.status in _ADAPTER_SET_ACCEPT_EXCLUDED]
    targets = [item for item in candidates
               if item.status not in _ADAPTER_SET_ACCEPT_EXCLUDED]
    for item in excluded:
        print(f"→ 세트 수용 제외({_ADAPTER_SET_ACCEPT_EXCLUDED[item.status]}): {item.relpath} · "
              f"{item.harness} · {item.mode} "
              f"— 받으려면 `{repo_protection._FACADE_PROG} sync-adapter-config --accept {item.relpath}`")
    if not targets:
        print("어댑터 config 세트 수용 대상 없음 "
              + ("(편집분 제외 후 남은 대상 없음)." if excluded else "(전부 상류와 동일)."))
        return 1 if excluded else 0
    print(f"# 세트 수용 대상 {len(targets)}건 (소스: {source_root})")
    for item in targets:
        print(f"  - {item.relpath} · {item.harness} · {item.mode} · "
              f"{_adapter_config_label(item)}")
    failed = 0
    for item in targets:
        if _accept_adapter_config_one(
                home, dest, source_root, item.relpath, judgments) != 0:
            failed += 1
    print(f"# 세트 수용 결과: {len(targets) - failed}/{len(targets)} 수용"
          + (f" · {failed} 실패/거부" if failed else "")
          + (f" · {len(excluded)} 편집분 제외" if excluded else ""))
    return 1 if (failed or excluded) else 0


def _adapter_config_label(judgment) -> str:
    """판정 → 표시 라벨 (mode 와 status 를 함께 본다·mode 상수는 엔진 소유)."""
    common = _ADAPTER_CONFIG_STATUS_LABEL.get(judgment.status, judgment.status)
    if judgment.mode == install_rules.ADAPTER_CONFIG_REPORT:
        return _ADAPTER_CONFIG_REPORT_LABEL.get(judgment.status, common)
    return common


def cmd_sync_adapter_config(
    home,
    args: argparse.Namespace,
    *,
    dest_root: Path | None = None,
) -> int:
    """`sync-adapter-config [--list | --check | --accept <relpath> | --accept-all]` — config 채널.

    이 파일들(`.codex/hooks.json`·`config.toml`·`.claude/settings.json`·`.opencode/opencode.jsonc`)
    은 manifest 밖이라 pm-update 가 절대 덮지 않는다. 동기는 무편집(원장 일치)인 managed 대상만
    자동 갱신하고 나머지는 보존+보고한다 — `--accept`(단건)·`--accept-all`(세트)가 그 보존분을
    채택자가 **명시적으로** 받는 유일한 채널이다(이게 없으면 원장 도입 전 채택자가 영구 보고
    모드에 갇힌다). 두 경로 모두 수용 전에 훅 세트 선행조건을 같은 게이트로 검사한다
    (`_adapter_accept_order_blocked` — 엔진 파일 선행 · config 후행).

    판정·수용 로직은 `app.adapter_report`·`app.adapter_accept` 단일 진실(`judge_adapter_configs`·
    `accept_adapter_config`)이고
    여기선 표시와 rc 번역만 한다. dest 는 명시 `dest_root`, 없으면 `home.root`(cmd_add_harness 동형),
    소스 해소는 add-harness 와 같은 규칙(`--from` > local.conf upstream.path(경로) > dest
    자기전환 > local.conf upstream.path(URL→로컬 캐시 clone/fetch))이다.

    ``--check``의 인스턴스-소유 세대 요약은 경로별 기준 원장을 따른다. 동기가
    ``upstream.rev``를 전진시키는 경로는 직후 check에서 사라질 수 있지만, report-drift·
    edited managed 파일은 보존된 파일별 ``template_rev``를 쓰므로 명시적 ``--accept``
    전까지 매 check에 반복된다. 백업/git을 대신하는 durable backstop은 아니다.
    """
    dest = dest_root if dest_root is not None else home.root
    try:
        source_root = adapter_accept.resolve_adapter_config_source(
            dest, getattr(args, "source", None))
    except FileNotFoundError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))
    except upstream_git.UpstreamCacheError as exc:
        # URL upstream 캐시 clone/fetch 실패 — 옛 캐시로 진행하지 않고 판정 자체를 멈춘다.
        print(f"[중단] {exc}", file=sys.stderr)
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))

    # 완료 게이트가 보는 같은 source checkout에서 instance-owned 전량(진입문서 ``none`` 포함)의
    # 세대 델타도 읽기 전용으로 노출한다. 판정 실패는 config 수렴 rc와 섞지 않는 advisory지만,
    # 변경 없음으로 접지 않고 전량 확인을 명시한다.
    if getattr(args, "check", False):
        try:
            delta_lines = adapter_report.instance_owned_template_delta_lines(
                dest, source_root)
        except Exception as exc:  # noqa: BLE001 — 관측 실패는 advisory 로 내고 check 본류는 계속한다.
            if getattr(exc, "_engine_rev_skew", False):
                raise
            delta_lines = [
                "⚠️  인스턴스 소유 템플릿 세대 판정 unavailable — "
                f"{exc} · 판정 불가(변경 없음 아님)·전량 확인 권장."
            ]
        for line in delta_lines:
            print(line)

    try:
        judgments = adapter_report.judge_adapter_configs(dest, source_root)
    except Exception as exc:  # noqa: BLE001 — 판정 채널 unavailable은 완료 게이트 red.
        if getattr(exc, "_engine_rev_skew", False):
            raise
        print(f"[중단] 어댑터 config 판정 채널 unavailable: {exc}", file=sys.stderr)
        print(
            "  → `--from <framework checkout>`이 해당 managed template을 포함한 올바른 "
            "source인지 확인하고 pm-update 후 다시 검사하라.",
            file=sys.stderr,
        )
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))
    accept = getattr(args, "accept", None)
    if accept:
        return _finish_sync_adapter_config(
            _accept_adapter_config_one(
                home, dest, source_root, accept, judgments),
            check=getattr(args, "check", False),
        )
    if getattr(args, "accept_all", False):
        return _finish_sync_adapter_config(
            _accept_adapter_config_set(
                home, dest, source_root, judgments),
            check=getattr(args, "check", False),
        )

    if not judgments:
        print("어댑터 config 채널 대상 없음 (설치 하네스의 config 가 없거나 소스에 template 부재).")
        # config 채널 대상이 0 이어도 훅 세트는 따로 볼 수 있다(설치된 훅 + 채택자 settings 는
        #   실재). 여기서 조용히 green 을 내면 게이트 표면이 통째로 우회된다.
        if getattr(args, "check", False):
            return _finish_sync_adapter_config(
                1 if _report_hook_set_findings(home, dest, source_root) else 0,
                check=True,
            )
        return _finish_sync_adapter_config(0, check=False)
    print(f"# 어댑터 config 판정 (소스: {source_root})")
    for judgment in judgments:
        label = _adapter_config_label(judgment)
        print(f"  - {judgment.relpath} · {judgment.harness} · {judgment.mode} · {label}")
    if getattr(args, "check", False):
        # `--check`는 판정만 읽는다(write 0). 완료 판정은 adapter_report helper가 소유하므로
        # pm-update와 이 CLI가 상태를 서로 다르게 해석할 수 없다.
        # 훅 세트 세대도 **같은 자리에서** 본다 — 스킬 카드가 이 커맨드를 완료 게이트로 지목하는데
        #   pm-update 가 rc1 로 막는 클래스를 여기서 green 으로 통과시키면 게이트가 갈린다.
        hook_set_failed = _report_hook_set_findings(home, dest, source_root)
        unconverged = adapter_report.unconverged_managed_adapter_configs(judgments)
        if not unconverged:
            if hook_set_failed:
                return _finish_sync_adapter_config(1, check=True)
            print("✓ managed 어댑터 config 수렴 확인 (template 내용 + 원장 dest hash).")
            return _finish_sync_adapter_config(0, check=True)
        print("[중단] managed 어댑터 config 미수렴:", file=sys.stderr)
        for judgment, status in unconverged:
            print(f"  - {judgment.relpath}: {status}", file=sys.stderr)
            if status == "unedited":
                print(
                    "    → `pm-update`를 재실행해 백업·자동 갱신·원장화를 완료하라.",
                    file=sys.stderr,
                )
            elif status == "unrecorded" and judgment.status == "in-sync":
                print(
                    "    → 실 `pm-update`를 재실행하면 파일 byte 변경 없이 원장을 backfill한다.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"    → `{repo_protection._FACADE_PROG} sync-adapter-config --accept "
                    f"{judgment.relpath}` (백업 후 상류 값 수용·원장화).",
                    file=sys.stderr,
                )
        return _finish_sync_adapter_config(1, check=True)
    # 세트 수용도 여기서 병기한다 — 단건만 노출하면 무편집분이 여러 개인 채택자가 세트 커맨드의
    #   존재를 모른 채 한 파일씩 수용한다(발견성). 제외 규칙은 세트 커맨드가 스스로 알린다.
    print(f"  수용(백업 후 상류 값 채택): {repo_protection._FACADE_PROG} sync-adapter-config --accept <경로> "
          "· 무편집분 일괄은 --accept-all (Windows 는 `.\\pm-config.cmd`)")
    return _finish_sync_adapter_config(0, check=False)


def _finish_sync_adapter_config(rc: int, *, check: bool) -> int:
    """``--check`` 말미에 경로별 세대 요약 수명을 명시한다."""
    if check:
        print("세대 요약은 기준 원장별 수명 — upstream.rev 전진분은 이후 사라질 수 있고, "
              "보존·미수용 managed 파일은 --accept 전까지 매 검사 반복; "
              "지난 세대는 백업/git 로 확인")
    return rc


def _dest_relative_label(path, dest_root) -> str:
    """dest 기준 상대 표기 — 밖이면 절대 경로 그대로(표시가 죽지 않게)."""
    try:
        return Path(path).relative_to(Path(dest_root)).as_posix()
    except ValueError:
        return str(path)
