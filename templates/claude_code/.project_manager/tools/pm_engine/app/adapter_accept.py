"""판정에 결속된 어댑터 config 수용과 백업·원장 확인 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import adapter_report
from pm_engine.domain import install_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import fs
from pm_engine.infra import install_files
from pm_engine.infra import install_receipt
from pm_engine.infra import upstream_declarations
from pm_engine.infra import upstream_git
from typing import NamedTuple
import datetime
import hashlib


def record_adapter_baseline(dest_root: Path, source_root: Path, harnesses=None, *,
                            root_identity: tuple | None = None) -> list[str]:
    """template 과 **일치가 확인된** config 의 해시를 원장에 기록 — 기록한 relpath 목록 반환.

    기록 조건이 곧 판정의 전제다: dest 가 template 과 **내용 일치**(개행 정규화 후)일 때만 "이
    내용이 상류가 준 그대로" 라고 말할 수 있다. 편집분·보존분은 기록하지 않는다(원장 부재 =
    보고 모드 = 안전 기본값). 설치·add-harness 의 레이다운 시점과, 동기 시점의 in-sync backfill 이
    같은 규칙을 쓴다 — backfill 이 없으면 원장 도입 전 채택자는 손댄 적이 없어도 영구히 보고
    모드에 갇힌다.

    구 원장(raw bytes 다이제스트)이 정규화 다이제스트와 다른 항목도 이 backfill 이 재기록한다.
    **파일은 건드리지 않는다** — 바뀌는 건 같은 사실의 표현(원장 값)뿐이다.

    그 재기록은 dest 가 template 과 다른 항목에도 닿는다(무변경 마이그레이션). in-sync 조건만
    걸면 상류가 바뀐 뒤의 구 축 항목은 backfill 이 영영 닿지 않아, 설치 이후 아무것도 안 한
    채택자가 `edited` 오판에 갇힌 채 `--accept` 를 강요받는다. 마이그레이션 항목은 `sha256` 만
    현재 축으로 바꾸고 `recorded_at`·`template_rev` 는 **보존**한다 — 이번 실행이 관측한 건
    "그때 기록한 그 bytes 그대로" 라는 사실 하나뿐이고, 세대 스탬프를 현재로 밀면
    `instance_owned_template_delta_lines` 가 실제 세대 델타를 침묵시킨다."""
    dest_root = Path(dest_root)
    document = adapter_baseline.read_adapter_baseline(dest_root)
    files = dict(document.get("files") or {})
    template_rev = upstream_git.read_upstream_rev(Path(source_root))
    recorded_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    changed: list[str] = []
    for relpath, _harness, _mode, template in adapter_report.adapter_config_targets(
            dest_root, source_root, harnesses):
        dest_path = dest_root / relpath
        if not dest_path.is_file():
            continue
        digest = adapter_baseline.content_sha256(dest_path)
        if digest is None:
            continue
        existing = files.get(relpath)
        recorded = existing.get("sha256") if isinstance(existing, dict) else None
        if adapter_baseline.content_sha256(template) != digest:
            # template 과 다른 파일은 in-sync backfill 대상이 아니다. 단 **구 축(raw bytes)으로
            #   기록된 같은 파일**이면 표현만 현재 축으로 옮긴다 — 파일 bytes 도, 레이다운 시점
            #   메타(`recorded_at`·`template_rev`)도 그대로다.
            if (recorded is not None and recorded != digest
                    and adapter_baseline._baseline_attests_dest(recorded, dest_path, digest)):
                files[relpath] = {**existing, "sha256": digest}
                changed.append(relpath)
            continue
        if recorded == digest:
            continue  # 같은 해시 재기록 — 타임스탬프만 흔들지 않는다(byte 안정).
        files[relpath] = {
            "sha256": digest,
            "recorded_at": recorded_at,
            "template_rev": template_rev,
        }
        changed.append(relpath)
    if not changed:
        return []
    document = {"schema": adapter_baseline.ADAPTER_BASELINE_SCHEMA, "files": files}
    if not adapter_baseline._write_adapter_baseline(dest_root, document, root_identity=root_identity):
        return []
    return sorted(changed)


def resolve_adapter_config_source(dest_root: Path, explicit=None) -> Path:
    """instance-owned config 채널의 어댑터 소스 checkout — add-harness 와 **같은 해소 규칙**.

    설치 하네스를 돌며 `_resolve_add_harness_source` 를 태운다(해소 규칙 사본 0). 어느 하네스로도
    소스를 못 찾으면 add-harness 와 같은 친화 FileNotFoundError 를 그대로 올린다.

    URL 캐시 실패(`UpstreamCacheError`)는 순회가 잡지 않는다 — 하네스가 달라도 URL 은 하나라
    다음 하네스에서 같은 clone/fetch 를 한 번 더 시도하게 될 뿐이다(재시도 0)."""
    if explicit is not None:
        return Path(explicit).resolve()
    dest_root = Path(dest_root).resolve()
    last: FileNotFoundError | None = None
    for harness in install_receipt.installed_harnesses(dest_root):
        try:
            return upstream_git._resolve_add_harness_source(dest_root, harness, None)
        except FileNotFoundError as exc:
            last = exc
    if last is not None:
        raise last
    raise FileNotFoundError(
        f"어댑터 config 소스 미해소: {dest_root} 에 설치된 PM 어댑터가 없다 "
        f"(`--from <프레임워크 checkout>` 를 명시하라).")


class AdapterConfigAcceptResult(NamedTuple):
    """수용 시도의 결과 — 성공은 `accepted` 하나뿐이고 나머지는 전부 비정상이다.

    status:
      `accepted`       백업 + 원자 교체 + 원장 기록까지 검증 완료(이 파일은 자동 갱신 궤도)
      `raced`          판정 시점 해시와 다름(그 사이 편집) → 아무것도 덮지 않고 중단
      `ledger-blocked` 원장을 쓸 수 없는 상태(상위 schema) → **파일도 건드리지 않는다**
      `write-failed`   교체 실패(원본 보존·백업은 남는다)
      `ledger-failed`  파일은 갱신됐으나 원장 기록이 확인되지 않음(자동 갱신 궤도 미진입)
    """
    status: str
    relpath: str
    backup: Path | None
    sha256: str | None
    detail: str = ""


def accept_adapter_config(dest_root: Path, source_root: Path, relpath: str, *,
                          expected_sha256: str | None = None,
                          expected_template_sha256: str | None = None,
                          expected_generation_sha256: str | None = None,
                          root_identity: tuple | None = None) -> AdapterConfigAcceptResult:
    """config 한 개를 **백업 후** 현행 template 으로 원자 교체하고 원장 기록까지 확인한다.

    managed 자동 갱신과 `--accept` 수용이 같은 write 경로를 탄다(백업 규칙·경쟁 판정·원장 기록이
    갈리지 않는다). 순서가 곧 안전 계약이다:
      1) 원장 쓰기 가능 여부 선확인 — 기록할 수 없으면 파일을 아예 안 건드린다(갱신해 놓고 판정
         기준을 못 남기면 다음 실행이 영구 보고 모드로 본다).
      2) `expected_sha256` 재검증 — 판정과 쓰기 사이의 동시 편집을 검증 없이 덮지 않는다.
      2') `expected_template_sha256`·`expected_generation_sha256` 재검증 — **게이트가 검사한 상류
         스냅샷과 지금 설치할 것을 결속**한다. 세대 게이트는 두 가지를 읽고 판정한다: 상류 config
         template 내용과 **선언 소스**(상류 엔진 package snapshot). 둘 중 하나라도 판정 뒤에 바뀌면 "검사한
         세대" 가 아닌 상태가 설치되므로 중단한다(config 만 묶으면 선언 갱신이 그대로 통과한다).
      3) 중앙 백업(`.pm_import_backups/<날짜>/`) 후 **백업 내용까지** 같은 해시인지 확인 — 덮는
         내용이 백업에 담겼음이 확인된 뒤에만 교체한다.
      4) 같은 디렉토리 임시 파일에 write→flush→fsync→원자 교체 — 디스크 오류가
         빈/부분 파일을 남기고 호출부가 "원본 보존" 으로 오보고하는 창을 없앤다.
      5) 교체 결과 해시 + 원장 항목을 다시 읽어 확인 — 확인 못 하면 비정상 상태로 반환한다.
    대상이 채널 밖이거나 dest 파일이 없으면 ValueError(호출 오류·조용한 무동작 금지)."""
    dest_root = Path(dest_root)
    targets = {rel: template for rel, _h, _m, template in adapter_report.adapter_config_targets(
        dest_root, source_root)}
    template = targets.get(relpath)
    if template is None:
        raise ValueError(
            f"어댑터 config 채널 대상이 아니다: {relpath} "
            f"(대상: {', '.join(sorted(targets)) or '없음'})")
    dest_path = dest_root / relpath
    if not dest_path.is_file():
        raise ValueError(f"채택자 트리에 그 파일이 없다: {dest_path}")

    if adapter_baseline._load_adapter_baseline_document(dest_root).status == "newer-schema":
        return AdapterConfigAcceptResult(
            "ledger-blocked", relpath, None, None,
            f"원장 schema 가 이 엔진(지원 상한 {adapter_baseline.ADAPTER_BASELINE_SCHEMA})보다 새로워 기록할 수 "
            "없다 — 파일을 바꾸지 않았다(엔진을 갱신한 뒤 다시 실행하라)")

    current = adapter_baseline.content_sha256(dest_path)
    if expected_sha256 is not None and current != expected_sha256:
        return AdapterConfigAcceptResult(
            "raced", relpath, None, None,
            "판정 뒤 파일이 바뀌었다(동시 편집) — 검증 없이 덮지 않는다. 다시 실행해 새 판정을 받아라")
    install_files.ensure_backup_dir_ignored(dest_root, root_identity=root_identity)
    backup = install_files._copy_dest_file_nofollow(
        dest_root, Path(relpath),
        Path(install_rules.BACKUP_DIR_NAME) / datetime.date.today().isoformat() / relpath,
        root_identity=root_identity)
    if adapter_baseline.content_sha256(backup) != current:
        return AdapterConfigAcceptResult(
            "raced", relpath, backup, None,
            "백업 시점 내용이 판정 내용과 달랐다(동시 편집) — 덮지 않았다")

    if expected_generation_sha256 is not None:
        # 선언 package 는 조회와 **같은 snapshot helper** 로 다시 읽어 대조한다(파일 집합·해시
        #   알고리즘의 소유자는 하나다). 다시 읽지 못하면 결속을 확인할 수 없으므로 쓰지 않는다.
        try:
            _payloads, current_generation = upstream_declarations._read_declaration_snapshot(source_root)
        except upstream_declarations.SNAPSHOT_READ_ERRORS as exc:
            return AdapterConfigAcceptResult(
                "raced", relpath, backup, None,
                f"상류 세대 선언 package 를 다시 읽지 못했다({type(exc).__name__}: {exc}) — 판정한 "
                "세대를 확인할 수 없어 설치하지 않는다. 다시 실행해 새 판정을 받아라")
        if current_generation != expected_generation_sha256:
            return AdapterConfigAcceptResult(
                "raced", relpath, backup, None,
                "판정 뒤 상류 세대 선언 package 가 바뀌었다 — 구 선언으로 낸 판정 위에 새 세대를 "
                "설치하지 않는다. 다시 실행해 새 판정을 받아라")
    payload = fs.read_bytes_shared(template)
    # 두 다이제스트의 축이 다르다. `digest` 는 **설치할 raw bytes** — 게이트가 검사한 template 과의
    #   결속·교체 후 write 검증용이다. `ledger_digest` 는 **내용** — 원장이 기록하는 값이고
    #   판정(`judge_adapter_configs`)이 대조하는 값이다. 섞으면 CRLF template 을 깐 트리에서
    #   원장 확인이 영구 실패한다.
    digest = hashlib.sha256(payload).hexdigest()
    ledger_digest = adapter_baseline.content_sha256(payload)
    if expected_template_sha256 is not None and digest != expected_template_sha256:
        return AdapterConfigAcceptResult(
            "raced", relpath, backup, None,
            "판정 뒤 상류 template 이 바뀌었다 — 게이트가 검사한 bytes 와 다른 내용을 설치하지 "
            "않는다. 다시 실행해 새 판정을 받아라")
    if not adapter_baseline._atomic_write_dest_bytes(dest_root, Path(relpath), payload,
                                    root_identity=root_identity):
        return AdapterConfigAcceptResult(
            "write-failed", relpath, backup, None,
            "원자 교체에 실패했다 — 기존 내용은 그대로다(백업도 남아 있다)")
    # write 검증은 **raw** 다 — 심은 bytes 가 그대로 착지했는지를 보는 자리라 정규화하면 부분
    #   write·개행 변조를 통과시킨다(판정이 아니라 쓰기 결과 확인).
    if adapter_baseline.file_sha256(dest_path) != digest:
        return AdapterConfigAcceptResult(
            "write-failed", relpath, backup, None,
            "교체 후 내용이 template 과 다르다 — 백업에서 복원할 수 있다")

    record_adapter_baseline(dest_root, source_root, root_identity=root_identity)
    entry = adapter_baseline.read_adapter_baseline(dest_root).get("files", {}).get(relpath)
    if not isinstance(entry, dict) or entry.get("sha256") != ledger_digest:
        return AdapterConfigAcceptResult(
            "ledger-failed", relpath, backup, ledger_digest,
            "파일은 갱신됐으나 원장 기록을 확인하지 못했다 — 다음 동기는 이 파일을 보고 모드로 "
            "본다(원장 경로의 쓰기 권한을 확인하라)")
    return AdapterConfigAcceptResult("accepted", relpath, backup, ledger_digest)


def adapter_config_drift_summary(judgment: adapter_baseline.AdapterConfigJudgment,
                                 dest_root: Path) -> str:
    """template 대비 차이 한 줄 요약 — 크기·줄 수·첫 차이 줄 번호.

    무엇이 다른지 사람이 판단할 최소 좌표만 준다(전체 diff 는 채택자가 직접 볼 일이고, 동기
    출력이 파일당 한 줄을 넘기면 아무도 안 읽는다)."""
    dest_path = Path(dest_root) / judgment.relpath
    try:
        dest_lines = fs.read_text_shared(dest_path, encoding="utf-8", errors="replace").splitlines()
        template_lines = fs.read_text_shared(judgment.template, encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "차이 요약 불가(읽기 실패)"
    first_diff = next(
        (index + 1 for index, (left, right) in enumerate(
            zip(dest_lines, template_lines)) if left != right),
        min(len(dest_lines), len(template_lines)) + 1,
    )
    return (f"채택자 {len(dest_lines)}줄 ↔ template {len(template_lines)}줄 · "
            f"첫 차이 {first_diff}줄")
