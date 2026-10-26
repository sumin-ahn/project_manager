"""갱신의 어댑터 설정·훅 세대·보호 훅·완료 게이트 (층: app)."""
from __future__ import annotations

from pathlib import Path
import os
import sys
from pm_engine.app import adapter_accept
from pm_engine.app import adapter_report
from pm_engine.app import harness_ctx
from pm_engine.app import hook_sets
from pm_engine.app import repo_protection
from pm_engine.domain import conf_schema
from pm_engine.domain import install_rules
from pm_engine.domain import manifest_rules
from pm_engine.infra import areas_store
from pm_engine.infra import branch_store
from pm_engine.infra import conf_store
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.infra import protected_hooks


# ── instance-owned 어댑터 config 채널 (3-way 원장 + drift 보고) ────────────────
# 어댑터 config(`.codex/hooks.json`·`config.toml`·`.claude/settings.json`·`.opencode/opencode.jsonc`)
# 는 어느 manifest 에도 없어 이 sync 가 안 덮고, add-harness 재실행조차 기존 값이 다르면 byte
# 보존한다 — 상류의 *동작* fix 가 기존 채택자에 도달할 채널이 0 이었다(훅 차단→비차단 fix 를 든
# 릴리스가 나간 뒤에도 채택자가 차단판을 그대로 운영한 실측이 이 클래스다).
#
# 채널은 두 갈래이고 판정·분류의 단일 진실은 `app.adapter_report` 다(소유 선언 → 채널 분류 → 원장 판정):
#   managed  무편집(dest 해시 == 원장 해시)이면 백업 후 현행 template 으로 갱신 + 후속 행동 안내.
#   report   갱신하지 않고 template 대비 drift 만 파일당 한 줄로 표기.
# 어느 갈래든 **편집분·원장 부재는 무조건 보존**한다(하한선) — 수용은 채택자가 명시 커맨드로 한다.
# 채택자가 실제로 칠 커맨드 — Windows 파사드를 병기한다(그 플랫폼엔 `./pm-config.sh` 가 없다·
#   같은 파일 add-harness 안내의 선례와 동형).
_ADAPTER_CONFIG_ACCEPT_HINT = "./pm-config.sh sync-adapter-config --accept"


_ADAPTER_CONFIG_LIST_HINT = (
    "./pm-config.sh sync-adapter-config --list (Windows 는 `.\\pm-config.cmd`)")


_ADAPTER_CONFIG_WINDOWS_HINT = "Windows 는 `.\\pm-config.cmd`"


_ADAPTER_BACKUP_HINT = ".pm_import_backups/<DATE>/"


# 채널 판정 전에 적용 여부를 가르는 최소 좌표.
# "존재하는 managed 대상"만 완료 게이트 대상이라는 계약 경계다. 파일이 하나도 없는 순수
# 엔진 복구 트리는 adapter 채널 unavailable이 아니라 적용 대상 없음(vacuous green)이다.
_ADAPTER_CONFIG_DEST_CANDIDATES = (
    ".codex/hooks.json", ".codex/config.toml", ".claude/settings.json",
    ".opencode/opencode.jsonc",
)


def _has_adapter_config_candidate(dest_root: Path) -> bool:
    """instance-owned config 경로 엔트리가 하나라도 있는가(읽기/쓰기 0).

    regular file만 세면 directory·broken symlink·권한 오류가 외곽 gate에서 채널 실행 자체를
    건너뛴다. 중앙 판정이 unavailable로 올릴 기회를 보존하도록 "부재가 확인된 경우"만 제외한다.
    """
    for relpath in _ADAPTER_CONFIG_DEST_CANDIDATES:
        try:
            (Path(dest_root) / relpath).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _instance_owned_template_delta_lines(
        dest_root: Path, source_root: Path) -> list[str]:
    """세대 델타 관측 채널 — 실패도 변경 없음으로 접지 않는 advisory 한 줄."""
    try:
        return adapter_report.instance_owned_template_delta_lines(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — sync 결과는 보존하고 관측 불가만 loud.
        return [
            "⚠️  인스턴스 소유 템플릿 세대 판정 unavailable — "
            f"{exc} · 판정 불가(변경 없음 아님)·전량 확인 권장."
        ]


def _print_instance_owned_template_delta(lines: list[str]) -> None:
    """파일별 한 줄 출력. 정합이면 ``lines=[]``라 완전 무출력."""
    for line in lines:
        print(line)


def sync_adapter_configs(dest_root: Path, source_root: Path, *, write: bool) -> dict:
    """instance-owned 어댑터 config 채널을 1회 돌린다 — 판정 결과 dict(출력은 호출부).

    `write=False`(dry-run)는 판정만 한다(파일·원장 미변경). 판정 실패는
    `status="unavailable"` 로 반환한다. 엔진 파일 적용은 되돌리지 않지만 상위 완료 게이트는
    이를 non-green 으로 소비한다."""
    result = {"status": "ok", "updated": [], "preserved": [], "drift": [],
              "backfilled": [], "degraded": [], "blocking": [],
              "managed_converged": False}
    try:
        judgments = adapter_report.judge_adapter_configs(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 sync 를 막지 않는다.
        # 판정 불가는 `unavailable` 로 내려가 완료 게이트가 rc1 로 재실행을 요구한다(이미 착지한
        #   엔진 파일은 그대로 선다).
        return {**result, "status": "unavailable", "reason": str(exc)}

    backfill_candidates = {
        judgment.relpath for judgment in judgments
        if judgment.status == "in-sync"
        and judgment.baseline_sha256 != judgment.dest_sha256
    }
    for judgment in judgments:
        if judgment.status == "in-sync":
            # 이미 상류와 같다 — write 경로가 아래에서 원장을 뒤늦게 채운다. 성공 여부는
            # 재판정으로 확인한 뒤에만 ``backfilled`` 로 보고한다(원장 write 실패 false-green 금지).
            continue
        summary = adapter_accept.adapter_config_drift_summary(judgment, dest_root)
        managed = judgment.mode == install_rules.ADAPTER_CONFIG_MANAGED
        if managed and judgment.status == "unedited":
            backup_rel = None
            if write:
                try:
                    # 판정 시점 해시를 넘겨 **판정↔쓰기 사이 동시 편집**을 엔진이 재검증하게 한다
                    #   (raced 면 아무것도 안 덮고 돌아온다). 백업·원자 교체·원장 확인도 그 안이다.
                    accepted = adapter_accept.accept_adapter_config(
                        dest_root, source_root, judgment.relpath,
                        expected_sha256=judgment.dest_sha256)
                except (OSError, ValueError) as exc:
                    # 한 파일의 IO 실패가 이미 끝난 엔진 sync 를 traceback 으로 덮지 않게 보존 쪽으로
                    #   내린다(다음 실행이 같은 판정으로 재시도). 경로 안전 거부·루트 교체는 의도적
                    #   hard abort 라 그대로 올라간다.
                    result["preserved"].append({
                        "relpath": judgment.relpath, "status": "update-failed",
                        "summary": str(exc)})
                    continue
                if accepted.status != "accepted":
                    # `ledger-failed` 만 성질이 다르다 — 파일은 이미 바뀌었으므로 "보존" 으로
                    #   묶으면 거짓 보고다. 별도 버킷으로 낸다(둘 다 loud).
                    bucket = ("degraded" if accepted.status == "ledger-failed"
                              else "preserved")
                    result[bucket].append({
                        "relpath": judgment.relpath, "status": accepted.status,
                        "summary": accepted.detail})
                    continue
                backup_rel = Path(accepted.backup).relative_to(Path(dest_root)).as_posix()
            result["updated"].append({
                "relpath": judgment.relpath,
                "backup_rel": backup_rel,
                "note": install_rules.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(judgment.relpath),
            })
            continue
        bucket = "preserved" if managed else "drift"
        result[bucket].append({
            "relpath": judgment.relpath, "status": judgment.status, "summary": summary})

    if write and (result["updated"] or backfill_candidates):
        # 갱신분·in-sync backfill 을 한 번에 기록한다(record 는 template 일치분만 담는다).
        try:
            adapter_accept.record_adapter_baseline(dest_root, source_root)
        except Exception as exc:  # noqa: BLE001 — 파일 적용은 보존, 재판정이 rc1로 승격.
            result["degraded"].append({
                "relpath": ", ".join(sorted(backfill_candidates)) or "adapter_baseline.json",
                "status": "ledger-failed",
                "summary": str(exc),
            })

    # 완료 판정은 **쓰기 후 재판정**이 진실이다. accept가 파일을 바꿨으나 원장을 못 남긴 경우,
    # in-sync backfill write가 실패한 경우를 모두 여기서 같은 red로 잡는다. dry-run은 현재 상태를
    # 그대로 판정하므로 write 0 계약을 유지한다.
    try:
        final_judgments = adapter_report.judge_adapter_configs(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — 엔진 적용 결과는 보존하되 게이트는 red.
        return {**result, "status": "unavailable", "reason": str(exc)}
    unconverged = adapter_report.unconverged_managed_adapter_configs(final_judgments)
    result["blocking"] = [
        {"relpath": judgment.relpath, "status": status,
         "judgment_status": judgment.status}
        for judgment, status in unconverged
    ]
    result["managed_converged"] = not result["blocking"]
    final_by_rel = {judgment.relpath: judgment for judgment in final_judgments}
    result["backfilled"] = sorted(
        relpath for relpath in backfill_candidates
        if relpath in final_by_rel
        and final_by_rel[relpath].baseline_sha256 == final_by_rel[relpath].dest_sha256
    )
    return result


def apply_harness_ctx_limits(effective_dest: Path, *, write: bool) -> bool:
    """local.conf 의 하네스별 사용 한도·압축 비율을 하네스 설정 파일에 반영한다 — 멈추면 False.

    무엇을 어디에 쓰는지·검사·원장 처리는 `app.harness_ctx.apply_harness_ctx` 한 곳이 정하고 여기서는
    출력과 멈춤만 맡는다. 어댑터 config 채널(`sync_adapter_configs`) **뒤**에 돈다 — 채널이 템플릿
    갱신·원장 기록을 끝낸 파일 위에 관리 키를 쓴다. `write=False`(dry-run)는 바뀔 줄만 낸다.

    local.conf 에 값 공급을 잃는 옛 키가 남아 있으면 반영을 계산하지 않고 그 사실을 한 줄로 낸다 —
    이 채널은 구표기 conf 를 가진 채택자에게도 엔진 파일을 배달해야 하고(`_read_local_conf`),
    반영은 옛 이름을 읽지 않는다. dry-run 은 이름 이동 전의 conf 를 보므로 이 줄이 나오고, 실제
    실행은 이동(`migrate_renamed_conf_keys`)을 먼저 끝낸 뒤 반영한다. 검사를 통과하지 못한 값
    (`HarnessCtxError`)은 설정 파일을 쓰지 않고 멈춘다 — 호출부가 rc 1 로 끝내 upstream.rev 기록에
    닿지 않는다. 그 밖의 실패는 그대로 올라간다."""
    prefix = "[dry-run] 하네스 설정 반영 예정" if not write else "  ✓ 하네스 설정 반영"
    remaining = conf_schema.blocking_legacy(conf_store.load(
        Path(effective_dest) / ".project_manager" / "local.conf").legacy)
    if remaining:
        print(f"{'[dry-run] ' if not write else ''}하네스 설정 반영을 건너뛴다 — local.conf 에 "
              f"옛 키({', '.join(remaining)})가 남아 있다. 이름 이동과 폐지 키 정리가 끝난 뒤의 "
              "pm-update 가 반영한다.")
        return True
    try:
        lines = harness_ctx.apply_harness_ctx(
            effective_dest, write=write,
            opencode_config_dir=harness_ctx.opencode_global_config_dir(os.environ, Path.home()))
    except harness_ctx.HarnessCtxError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return False
    for line in lines:
        print(f"{prefix}: {line}")
    return True


def _adapter_config_gate_failed(result: dict) -> bool:
    """동기 결과의 managed 완료 게이트 — unavailable/미수렴이면 True."""
    return (result.get("status") != "ok"
            or not result.get("managed_converged", False))


def _print_adapter_config_finding(result: dict, *, dry_run: bool = False) -> None:
    """어댑터 config 채널 결과 출력 — 변경/보존은 loud, 보고는 파일당 한 줄.

    조용한 갱신을 금지하는 게 이 출력의 목적이다: managed 갱신은 채택자가 훅을 다시 승인해야
    발화하므로 갱신 사실과 후속 행동이 같은 자리에서 보여야 한다. 소견이 없으면(정상·전부 최신)
    무출력이지만, **`unavailable` 은 조용히 넘기지 않는다**(형제 `_print_protected_hook_reinstall_
    finding` 동형) — 채널이 안 돌았다는 건 상류 동작 fix 가 이 채택자에 안 갔다는 뜻이라, 침묵하면
    그 사실을 알 방법이 아예 없다."""
    status = result.get("status")
    if status == "unavailable":
        print(
            "[경고] 어댑터 config 채널을 건너뛰었다 — "
            f"{result.get('reason')}. instance-owned config(hooks.json 등)의 상류 동작 fix 가 "
            "이번 동기에 반영되지 않았다.\n"
            "  → 먼저 pm-update로 엔진 전체를 동기화한 뒤 재실행하세요.\n"
            f"  → 현재 판정 조회: {_ADAPTER_CONFIG_LIST_HINT}",
            file=sys.stderr,
        )
        return
    if status != "ok":
        return
    for item in result.get("updated", []):
        verb = "갱신 예정" if dry_run else "갱신"
        print(f"→ 어댑터 config {verb} — {item['relpath']} (무편집 확인·상류 동작 fix 반영)")
        if dry_run:
            print(f"    (원본은 {_ADAPTER_BACKUP_HINT} 에 백업 예정·적용 안 함)")
        elif item.get("backup_rel"):
            print(f"    백업: {item['backup_rel']}")
        if item.get("note"):
            print(f"    ⚠️ {item['note']}")
    for item in result.get("preserved", []):
        reason = {
            "edited": "채택자 편집",
            "unrecorded": "원장 부재(구세대 설치)",
            "update-failed": "갱신 실패(다음 실행이 재시도)",
            "raced": "판정 뒤 동시 편집 감지",
            "ledger-blocked": "원장 기록 불가(상위 schema)",
            "write-failed": "원자 교체 실패",
            "ledger-failed": "원장 기록 미확인",
        }.get(item["status"], item["status"])
        print(f"⚠️  어댑터 config {item['relpath']} — {reason}이라 보존한다 "
              f"({item['summary']}).", file=sys.stderr)
        print(f"    상류 값을 받으려면: {_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']} "
              f"(백업 후 교체 · {_ADAPTER_CONFIG_WINDOWS_HINT})", file=sys.stderr)
    for item in result.get("degraded", []):
        # 파일은 갱신됐는데 판정 기준(원장)이 안 남은 상태 — 조용히 두면 다음 동기가 이 파일을
        #   영구 보고 모드로 본다. 보존과 섞지 않고 실제 상태 그대로 알린다.
        print(f"⚠️  어댑터 config {item['relpath']} — 갱신은 됐으나 원장 기록을 확인하지 못했다 "
              f"({item['summary']}).", file=sys.stderr)
        print(f"    원장 경로 권한을 고친 뒤 `{_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']}` 로 "
              f"기록을 복구하라(내용은 이미 상류 값이다).", file=sys.stderr)
    # in-sync byte지만 원장 backfill 실패 같은 상태는 preserved/degraded 버킷에 아직 없을 수 있다.
    already_reported = {
        item["relpath"] for bucket in ("preserved", "degraded")
        for item in result.get(bucket, [])
    }
    for item in result.get("blocking", []):
        if (item["relpath"] in already_reported
                or item.get("judgment_status") != "in-sync"):
            continue
        print(
            f"⚠️  어댑터 config {item['relpath']} — 내용은 상류와 같지만 원장 기록이 없어 "
            "수렴 완료로 볼 수 없다.",
            file=sys.stderr,
        )
        print(
            ("    다음 실 pm-update가 파일 byte를 바꾸지 않고 원장을 backfill한다. "
             "dry-run 없이 pm-update를 재실행하라."
             if dry_run else
             f"    원장화하려면: {_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']} "
             f"(백업 후 확인 · {_ADAPTER_CONFIG_WINDOWS_HINT})"),
            file=sys.stderr,
        )
    drift = result.get("drift", [])
    for item in drift:
        print(f"→ 어댑터 config drift(보고 전용) {item['relpath']} — {item['summary']}")
    if drift:
        print(f"    위 파일은 채택자 소유라 갱신하지 않는다 — 상류 값을 받으려면 "
              f"`{_ADAPTER_CONFIG_ACCEPT_HINT} <경로>` ({_ADAPTER_CONFIG_WINDOWS_HINT}).")


# ── 훅 세트 세대 정합 검사 ───────────────────────────────────
# 어댑터 config 채널의 형제다. 채널이 "config 내용이 상류와 같은가" 를 보는 반면, 이 검사는
# "config 가 요구하는 훅 세대를 **설치된 래퍼/드라이버가 실제로 감당하는가**" 를 본다. 두 축의
# 갱신 주체가 달라(config = 채택자 소유·엔진 불가침 / 래퍼·드라이버 = manifest 등재) 세대 혼합
# 창이 구조적으로 열리고, 그중 config 가 앞선 조합은 PreToolUse rc2 = 도구 전면 차단이다
# (v1.7.0 흡수 실측). 검사는 읽기 전용이고, 판정·처방은 `app.hook_sets` 단일 진실을 그대로 쓴다.


def check_adapter_hook_sets(home, dest_root: Path, source_root: Path) -> dict:
    """훅 세트 세대 정합을 1회 판정한다 — 결과 dict(출력은 호출부·write 0).

    판정 실패는 `status="unavailable"` 로 내린다(형제 `sync_adapter_configs` 와 같은 fail-soft
    — 이 검사가 성공한 엔진 동기를 traceback 으로 덮지 않는다)."""
    result: dict = {"status": "ok", "findings": [], "entrypoints": [], "reason": None}
    try:
        # 판정 선언은 **상류 세대**를 우선한다 — 상류가 이번에 들여오는 새 플래그를 설치본 선언은
        #   모르므로, 그 세대로 보면 요구를 "선언 밖 플래그" 로 접어 green 이 된다. 조회 성격이라
        #   상류를 못 읽으면 로컬 선언으로 내려가되(판정을 통째로 잃지 않는다) **사유는 알린다**
        #   (문구는 `hook_sets.hook_set_query_fallback_lines` 단일 진실 — pm-config `--check` 와 같다).
        generation = hook_sets.hook_set_declarations(home, source_root)
        for line in hook_sets.hook_set_query_fallback_lines(generation):
            print(line, file=sys.stderr)
        findings = hook_sets.judge_adapter_hook_sets(
            dest_root, source_root, declarations=generation.declarations)
        remedy_lines = hook_sets.hook_set_remedy_lines
        # 역방향 진입점 축은 **별도 목록**이다 — 완료 게이트가 소비하는 `findings` 에 섞으면
        #   advisory 가 차단으로 승격돼 훅을 의도적으로 끈 채택자의 흡수가 영구히 막힌다.
        entrypoint_lines = hook_sets.hook_entrypoint_advisory_lines
        entrypoint_findings = hook_sets.judge_adapter_hook_entrypoints(
            dest_root, source_root, declarations=generation.declarations)
    except Exception as exc:  # noqa: BLE001 — 판정 실패가 동기를 무효화하지 않는다.
        return {**result, "status": "unavailable", "reason": str(exc)}
    result["findings"] = [
        {"harness": finding.harness, "config_relpath": finding.config_relpath,
         "kind": finding.kind, "subject": finding.subject,
         "unmet_paths": list(finding.unmet_paths), "remedy": finding.remedy,
         "detail": finding.detail, "remedy_lines": remedy_lines(finding)}
        for finding in findings
    ]
    result["entrypoints"] = [
        {"harness": finding.harness, "config_relpath": finding.config_relpath,
         "kind": finding.kind, "event": finding.event, "matcher": finding.matcher,
         "dispatcher": finding.dispatcher, "detail": finding.detail,
         "remedy_lines": entrypoint_lines(finding)}
        for finding in entrypoint_findings
    ]
    return result


def _unverified_hook_scope_paths(updated) -> tuple[str, ...]:
    """판정이 실패했을 때 **거부해야 할** 스코프 경로 — 훅 네임스페이스 하위 전량.

    "결합 묶음을 검증할 수 없다" 는 인식 상태는 하나이므로 처분도 하나여야 한다(선언 미해소 폴백과
    같은 fail-closed)."""
    namespaces = tuple(f"{name}/" for name in hook_sets.hook_set_namespaces(None))
    return tuple(path for path in updated if path.startswith(namespaces))


def _change_dest_paths(changes) -> list[str]:
    """change 목록 → dest 기준 relpath 목록 (판정 입력 정규화)."""
    return [str(change[0]).replace("\\", "/").strip("/") for change in changes]


def refuse_partial_hook_set_scope(home, scoped_changes, planned_changes,
                                  source_root=None) -> int:
    """경로 스코프가 결합 묶음을 반쪽만 갱신하면 **쓰기 전에** 거부한다 (rc 1·정상이면 0).

    `--paths` 는 어댑터 채널을 끄므로(요청 밖 write 0) 세대 검사가 전무하다 — 래퍼만 옮기고
    드라이버를 두면 "신 래퍼 + 구 드라이버" 락아웃을 손수 만들고도 rc0 다. 엔진 도구 부분 전파
    경고와는 **다른 축**이다: 저쪽은 rev 혼재를 알리기만 하고, 이쪽은 하네스가 잠기므로 차단한다.

    입력은 **해소된 계획**이다(원문 `--paths` 표기가 아니라 dest 좌표의 change 목록):
      `scoped_changes`  이번 실행이 실제로 갱신할 change
      `planned_changes` 스코프가 없었다면 갱신됐을 change(= 상류와 다른 것 전수)
    원문 표기를 비교하면 `@source` 상류 좌표(`templates/claude_code/.claude/…`)로 지목한 요청이
    dest 좌표(`.claude/…`) 선언과 교집합 0 이 되어 검사가 통째로 무발화한다. 계획을 입력으로 삼으면
    좌표 표기 전반이 한 번에 닫히고, **이미 최신인 형제**는 `planned` 에 없어 거짓 거부도 없다.

    결합 묶음 선언은 **상류 세대**를 쓴다 — 상류가 이번에 들여오는 새 묶음을 설치본 선언은 모르고,
    그 첫 전파가 정확히 반쪽 갱신이 나는 자리다. 상류를 못 읽으면 설치본 선언으로 한 번 더 보되,
    **어댑터 훅 네임스페이스 하위를 건드리는 스코프는 통째로 거부**한다 — 그 세대의 결합을 검증할
    방법이 없고, 로컬 묶음 membership 으로 좁히면 "상류만 아는 묶음" 이 정확히 그 틈으로 빠진다
    (탈출구는 스코프 없는 전량 pm-update). 훅과 무관한 부분 전파는 종전대로 통과한다.

    판정이 실패하면 훅 네임스페이스 하위 스코프를 거부하고 조용히 넘어가지 않는다(무진단 침묵 금지)."""
    updated = _change_dest_paths(scoped_changes)
    unverified: tuple[str, ...] = ()
    try:
        generation = hook_sets.hook_set_declarations(home, source_root, required=True)
        if generation.declarations is None:
            # 조회 폴백(설치본 선언)으로 판정은 계속한다 — 그 세대가 아는 반쪽 갱신은 여전히
            #   잡는다. 다만 fail-closed 대상은 **로컬 결합 묶음으로 좁히지 않는다**: 상류에만
            #   있는 묶음은 정의상 로컬이 모르므로, membership 으로 좁히면 지금 닫으려는
            #   케이스가 그대로 빠져나간다. 훅이 사는 **네임스페이스** 하위를 건드리면 전부
            #   거부하고, 훅과 무관한 경로는 종전대로 통과시킨다.
            generation = hook_sets.hook_set_declarations(home, source_root)
            namespaces = tuple(
                f"{name}/" for name in
                hook_sets.hook_set_namespaces(generation.declarations))
            unverified = tuple(
                path for path in updated if path.startswith(namespaces))
        partial = hook_sets.hook_set_partial_update(
            updated, _change_dest_paths(planned_changes),
            declarations=generation.declarations)
    except Exception as exc:  # noqa: BLE001 — 가드 판정 실패가 복구 전파를 자기잠금하면 안 된다.
        partial = None
        # **인식 상태는 위 폴백과 같다** — 결합 묶음을 검증할 방법이 없다. 그러니 처분도 같아야
        #   한다: 훅 네임스페이스 하위만 거부하고(무관 경로는 종전대로 통과) 탈출구는 스코프 없는
        #   pm-update 다 — `--paths` 한정이라 복구 채널은 잠기지 않는다.
        unverified = _unverified_hook_scope_paths(updated)
        print(f"[경고] 훅 세트 부분 전파 가드를 건너뛰었다 — {exc}.", file=sys.stderr)
    if unverified:
        print(
            "[중단] 상류 훅 세트 세대 선언을 확인할 수 없어 어댑터 훅 영역의 부분 전파를 거부한다 — "
            f"이 스코프가 결합 묶음을 반쪽만 갱신하는지 검증할 방법이 없다: {', '.join(unverified)}\n"
            "  → 스코프 없이 pm-update 를 돌리거나(전량 전파는 이 가드 대상이 아니다) `--from` 이 "
            "온전한 프레임워크 checkout 인지 확인하라.",
            file=sys.stderr,
        )
        return 1
    if not partial:
        return 0
    for harness, left_behind, required in partial:
        print(
            f"[중단] 경로 스코프가 {harness} 훅 세트를 반쪽만 갱신한다 — 갱신이 필요한데 스코프 "
            f"밖인 {len(left_behind)}건: {', '.join(left_behind)}",
            file=sys.stderr,
        )
        print(
            "  훅 세트는 한 세대 단위로만 정합이다(래퍼·드라이버·공유 코어). 반쪽 갱신은 "
            "미지원 플래그 rc2 = 도구 차단을 만든다.\n"
            f"  → 이 묶음을 함께 지목하거나(--paths {' '.join(required)}) 스코프 없이 pm-update 를 "
            "돌려라.",
            file=sys.stderr,
        )
    return 1


def _adapter_hook_set_gate_failed(result: dict) -> bool:
    """훅 세트 검사의 완료 게이트 — 불일치가 하나라도 남으면 True.

    `unavailable` 은 게이트를 막지 않는다(형제 config 채널과 다른 판단): 이 검사는 채널이 아니라
    가드라, 판정 불가를 red 로 올리면 구형/부분 트리에서 복구 실행 자체가 자기잠김한다. 대신
    침묵하지 않고 경고를 낸다."""
    return bool(result.get("findings"))


def _print_adapter_hook_set_finding(result: dict, *, dry_run: bool = False) -> None:
    """훅 세트 검사 결과 — 정합이면 무출력, 불일치는 loud 처방(사유 + 실행 커맨드).

    dry_run 은 문구만 바꾼다(판정 자체가 읽기 전용이라 write 경로와 결과가 같다)."""
    status = result.get("status")
    if status == "unavailable":
        print(
            "[경고] 어댑터 훅 세트 세대 검사를 건너뛰었다 — "
            f"{result.get('reason')}. 설치된 훅 래퍼/드라이버가 채택자 config 가 요구하는 "
            "세대인지 확인되지 않았다.",
            file=sys.stderr,
        )
        return
    for finding in result.get("findings", []):
        print(f"⚠️  어댑터 훅 세트 세대 불일치({finding['harness']}) — {finding['detail']}.",
              file=sys.stderr)
        for line in finding["remedy_lines"]:
            print(f"    → {line}", file=sys.stderr)
    # 역방향 진입점 축은 advisory 다 — 같은 자리에서 loud 하게 내되 게이트는 안 건드린다.
    for finding in result.get("entrypoints", []):
        print(f"⚠️  어댑터 훅 진입점 누락({finding['harness']}) — {finding['detail']}.",
              file=sys.stderr)
        for line in finding["remedy_lines"]:
            print(f"    → {line}", file=sys.stderr)
    if result.get("findings") and dry_run:
        print("    (dry-run — 판정만 했다. 위 처방을 적용한 뒤 실 pm-update 를 돌려라.)",
              file=sys.stderr)


# ── 보호 훅 전수 재설치 트리거 ────────────────────────────────
# 보호 훅(`.local/repo-hooks/<repo>/pre-push`·`pre-commit`)은 엔진 코드(worktree_pool 의 훅
# 본문 상수)에서 *생성*되는 런타임 산출물이라, 엔진 파일이 갱신돼도 **재설치가 돌아야** 새 훅이
# 디스크에 놓인다. 그런데 기존 설치 트리거는 `repo add`·`worktree add` 둘뿐이었다 — 즉 엔진
# 업그레이드만 한 채택자는 새 훅(예: pre-commit 가드)을 **영영 못 받는다**(값-연결이
# 끊긴 채 green·[[robustness-value-connections-before-ship]]). 그래서 매 sync **실행마다** 등록
# repo 전수 정합 확인 + drift 재설치를 신설한다.
#
# ⚠ **`changes` 유무로 게이트하지 않는다**(내부/추가 리뷰어 게이트 must-fix·격리 실측): 업그레이드
# 경계에서 sync 를 *실행하는 주체는 dest 의 구 엔진*이다 — 이 기능을 배달하는 그 sync 자체는
# 재설치 코드를 갖고 있지도 않다(RUN 1 미발화). 바로 다음 실행은 dest 가 신 엔진이지만
# `changes == 0` 이라, "changes>0 에서만" 으로 좁히면 **다음에 우연히 엔진이 또 바뀔 때까지**
# 훅이 안 깔린다(RUN 2 미발화). 그래서 옆의 `migrate_entry_doc` 와 **동형으로** changes 0 경로
# 에서도 돈다. 노이즈는 트리거를 끄는 대신 **정합이면 조용**(아래 `_protected_hook_in_sync`
# drift 판정)으로 낮춘다 — sidecar reconcile 의 "비교 우선·정합이면 subprocess 0" 과
# 같은 패턴이라 새 개념이 늘지 않는다. 이 판정은 훅 디렉토리가 통째로 지워진 clone 도 덮는다
# (bootstrap reconcile 은 sidecar 파일이 없으면 즉시 return 이라 그 상태를 영구 침묵한다).
#
# 완료 프로세스에서 실행 트리의 보호 패키지를 import하고 대상 Home의 데이터를 읽는다.
# --target export는 이 완료 경로를 실행하지 않는다.
def _read_hook_artifact(path: Path) -> str | None:
    """설치된 훅 산출물(훅 본문·sidecar) 1개를 읽는다 — 부재/읽기 실패는 **None**.

    `_protected_hook_in_sync` 의 유일한 읽기 창구다. 부재와 **읽기 실패**(non-UTF-8 로 깨진
    본문·권한·IO 오류)를 *같은* None 으로 수렴시키는 게 요점 — 둘 다 "이 파일은 현 엔진 산출물이
    아니다" 라는 같은 결론이고, 따라서 같은 해소(재설치)로 가야 한다. 예외를 밖으로 내면 호출부의
    fail-soft 가 그걸 `unavailable`(=재설치 안 함)로 처리해 **깨진 훅이 영영 복구되지 않는다**."""
    try:
        return fs.read_text_shared(path, encoding="utf-8")
    except OSError:
        return None            # 부재·권한·IO — 재설치 대상.
    except UnicodeDecodeError:
        return None            # 깨진 본문(non-UTF-8) — 우리 산출물이 아니다 → 재설치 대상.


# 실행 비트 축을 볼 수 있는 플랫폼인가 — POSIX 만. Windows 는 실행 비트 개념이 다르다(NTFS 에
# mode 가 없고 `Path.chmod` 는 read-only 플래그만 만진다·`st_mode` 는 항상 0o666/0o444 계열).
# 거기서 이 축을 보면 **매 sync 거짓 drift → 무한 재설치**가 된다. git-for-windows 는 훅을 sh 로
# 돌리며 실행 비트를 요구하지도 않으므로 축 자체가 무의미하다. 테스트는 이 상수를 뒤집어
# Windows 거동을 hermetic 하게 친다(플랫폼 분기 실행 불요).
_EXEC_BIT_MEANINGFUL = os.name != "nt"


def _hook_artifact_executable(path: Path) -> bool:
    """산출물이 실행 가능한가 — 실행권한 축 (Windows 는 축 비활성이라 항상 True).

    **없으면 git 이 훅을 조용히 건너뛴다** — 본문만 비교하면 `chmod 0644` 된 훅이 `in_sync` 로
    오판돼 보호가 침묵 비활성화된다. stat 실패(부재·권한)는 `False`(drift·재설치)."""
    if not _EXEC_BIT_MEANINGFUL:
        return True
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _protected_hook_in_sync(home, repo: str) -> bool:
    try:
        expected = branch_store._resolve_protected(home, repo)
        mode, test_cmd = repo_protection._protected_push_gate_config(home, repo, report_downgrade=False)
        for artifact in protected_hooks.protected_hook_artifacts(
                home, repo, expected, gate_mode=mode, test_cmd=test_cmd):
            if _read_hook_artifact(artifact.path) != artifact.content:
                return False
            if artifact.executable and not _hook_artifact_executable(artifact.path):
                return False
        return protected_hooks.protected_hook_wired(home, repo) is True
    except Exception:
        return False


def reinstall_protected_hooks(dest_root: Path, *, write: bool) -> dict:
    result = {"status": "unavailable", "targets": [], "in_sync": [],
              "drifted": [], "failed": [], "no_bare": [], "reason": None}
    home = pm_paths.home_at(Path(dest_root).resolve())
    try:
        repos = sorted(areas_store.registered_repos(home))
        if not repos:
            result["status"] = "no_repos"
            return result
        for repo in repos:
            if not pm_paths.bare_repo_path(home, repo).exists():
                result["no_bare"].append(repo)
                continue
            result["targets"].append(repo)
            if _protected_hook_in_sync(home, repo):
                result["in_sync"].append(repo)
                continue
            result["drifted"].append(repo)
            if write and not repo_protection._install_protected_hook_reporting(
                    home, repo, action="(재)설치"):
                result["failed"].append(repo)
        result["status"] = "done"
        return result
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result


def _print_protected_hook_reinstall_finding(result: dict, *, dry_run: bool = False) -> None:
    """reinstall_protected_hooks 결과 요약 (per-repo 성공/실패 줄은 repo_protection 깔때기 소관).

    **정합이면 완전히 조용**하다(`in_sync` 만 있는 매 sync 의 정상 경로) — 트리거를 끄지 않고
    출력만 낮춘 게 이 함수다. 등록 repo 0(=`no_repos`)도 조용(걸 대상 없음). `unavailable` 은
    훅이 갱신되지 않았다는 뜻이므로 stderr 경고 + 재설치 커맨드를 낸다(침묵 무력화 금지)."""
    status = result.get("status")
    if status == "no_repos":
        return
    if status == "unavailable":
        print(
            "[경고] 보호 브랜치 훅 정합 확인/재설치를 건너뛰었다 — "
            f"{result.get('reason')}. 이 clone 의 훅은 **옛 엔진 본문**으로 남을 수 있다.\n"
            "  → 먼저 pm-update로 엔진 전체를 동기화한 뒤 재실행하세요.\n"
            "  → 그 다음 재설치(멱등): pm-config repo add <repo>",
            file=sys.stderr,
        )
        return
    drifted = result.get("drifted") or []
    if drifted and dry_run:
        print(f"→ 보호 브랜치 훅 (재)설치 예정: {', '.join(drifted)} "
              "(설치된 훅이 현 엔진과 불일치·적용 안 함)")
    no_bare = result.get("no_bare") or []
    if no_bare:
        print(f"→ 보호 훅 대상 아님(bare `.repos/<repo>.git` 부재): {', '.join(no_bare)} "
              "— 미러를 만들면(`pm-config repo add <repo>`) 훅이 걸린다.")


def _refuse_missing_sources(missing: list[str], manifest: list) -> int:
    """source 부재 항목 처리 — `@target-owned` 는 skip 안내, 그 밖의 부재가 있으면 rc 2(없으면 0).

    manifest 의 일부는 *target-owned 어댑터* 일 수 있다 — 엔진 upstream(루트)엔 source 가 없고 타깃
    자신만 보유하는 경로(예: opencode `.opencode/*`). 그런 항목은 upstream→dest 전파 대상이
    *아니므로* rc2 로 전체를 막는 대신 graceful skip + 안내 로그로 surface 한다(침묵 skip 금지).

    skip 은 **`@target-owned` 항목 한정**이다(명시 마커). `@render` 를 판별자로 쓰면 틀린다:
    `.claude/agents @render`·`.claude/skills @render` 처럼 *루트 upstream 에 존재해야 하는 엔진
    리소스*도 @render 라, 잘못된 --from/upstream 에서 빠지면 rc2 대신 skip 으로 숨겨 엔진 누락을
    은폐한다. non-`@target-owned` 항목이 source-부재면 진짜 누락(오타·잘못된 --from·전파돼야 하는데
    빠진 도구·@render 엔진 리소스 포함)이므로 rc2 + 에러다(silent skip 금지). 혼합이면
    non-@target-owned 가 전체를 막는다.

    이 판별은 **모든 모드(전달 프로세스·완료 프로세스·`--target`) 공통**이다. opencode 채택자의
    self-update 는 manifest 에 `.opencode/* @target-owned` 가 있으나 upstream=프레임워크 루트
    (.opencode/ 부재·root=claude)라 source-부재가 정상이다."""
    if not missing:
        return 0
    # missing 은 path 문자열만 운반하므로 manifest 에서 각 path 의 @target-owned 플래그를
    # 복원한다(plan 의 render_enabled=False 는 copy/render 동작만 끄고 entry 플래그는 보존).
    target_owned_flag = {str(e): manifest_rules._entry_target_owned_flag(e) for e in manifest}
    owned = [r for r in missing if target_owned_flag.get(r, False)]
    engine_missing = [r for r in missing if not target_owned_flag.get(r, False)]
    for r in owned:
        print(
            f"  [skip] {r} — target-owned: upstream source 부재 "
            "(타깃 고유 @target-owned 어댑터·엔진 upstream 에 없음·전파 대상 아님)"
        )
    if not engine_missing:
        return 0
    for r in engine_missing:
        print(f"  [source 에 없음] {r}", file=sys.stderr)
    print(
        f"오류: 엔진 경로 {len(engine_missing)}개가 source 에 없음(non-@target-owned) — "
        "--from 경로가 올바른 엔진 upstream 인지 확인하라 "
        "(@target-owned 어댑터만 target-owned skip 대상).",
        file=sys.stderr,
    )
    return 2
