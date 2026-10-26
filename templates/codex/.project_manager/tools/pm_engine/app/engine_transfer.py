"""자기 갱신의 엔진 코드 전달과 완료 프로세스 인계·출력 중계 (층: app)."""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pm_engine.app import update_adapters
from pm_engine.app import update_plan
from pm_engine.app import upstream_changes
from pm_engine.domain import manifest_rules
from pm_engine.infra import engine_transfer as package_transfer
from pm_engine.infra import retired_paths


# ── 갱신 절차: 전달 프로세스 → 완료 프로세스 ─────────────────────────────────────
# 디스크에 있던 갱신기(전달 프로세스)는 엔진 코드(`.project_manager/tools/`)만 옮기고(엔진 패키지는
# 디렉터리째 먼저 교체하고 나머지 평면 엔진 파일을 그 뒤에 복사한다), 옮겨 받은
# 갱신기를 같은 인자로 새 Python 프로세스(완료 프로세스)에서 띄운다. 렌더·이관·훅·어댑터 설정·기준
# rev 기록 같은 완료 단계는 완료 프로세스만 실행한다 — 전달하는 프로세스는 자기가 전달한 코드를
# 실행하지 않으므로 한 프로세스 안에 엔진 사본 세대가 섞이지 않는다. 기동 형태(인터프리터·대상
# 경로 `.project_manager/tools/pm_update.py`·같은 인자)와 인계 값(아래 환경 변수의 필드 셋)은 세대 간
# 계약이라 이후 세대가 바꾸지 않는다.
COMPLETION_ENV = "PM_UPDATE_COMPLETION"


_COMPLETION_FIELDS = frozenset({"dest", "source", "transferred"})


class CompletionHandoffError(ValueError):
    """완료 단계 인계 값(`PM_UPDATE_COMPLETION`)의 형식이 틀렸다 — 쓰기 전에 멈춘다."""


def _take_completion_handoff() -> dict | None:
    """인계 값을 환경에서 꺼낸다(꺼낸 뒤 환경에 남기지 않는다) — 없으면 None.

    JSON 객체이고 키가 정확히 `dest`·`source`(절대경로 문자열)·`transferred`(0 이상 정수)여야 한다.
    아니면 `CompletionHandoffError`."""
    raw = os.environ.pop(COMPLETION_ENV, None)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise CompletionHandoffError(f"JSON 이 아니다 ({exc})") from exc
    if not isinstance(value, dict) or set(value) != _COMPLETION_FIELDS:
        found = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise CompletionHandoffError(
            f"키는 정확히 dest·source·transferred 여야 한다 (받은 값: {found})")
    for key in ("dest", "source"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise CompletionHandoffError(f"{key} 는 절대경로 문자열이어야 한다 ({value[key]!r})")
    transferred = value["transferred"]
    if isinstance(transferred, bool) or not isinstance(transferred, int) or transferred < 0:
        raise CompletionHandoffError(f"transferred 는 0 이상 정수여야 한다 ({transferred!r})")
    return value


def _refuse_scope_with_pending_engine_code(changes, *, target: bool = False,
                                           scope_paths=(), source_root: Path) -> int:
    """`--paths` 는 엔진 코드 변경이 남아 있으면 쓰기 전에 거부한다 — 있으면 rc 1, 없으면 0.

    엔진 코드는 세대 단위로만 옮긴다. 일부만 옮기면 다음 프로세스가 세대가 섞인 엔진 코드를 적재하고,
    하나도 옮기지 않으면 옛 엔진 코드가 새 템플릿을 그린다. 자기 갱신(`target=False`)은 어떤 엔진 코드
    변경이든 거부한다. 내보내기(`target=True`)는 엔진 패키지 교체 행만 예외로, 요청 경로가 패키지 전체
    (패키지 경로 자신 또는 그 조상, dest·source 두 좌표)를 덮으면 그 행을 남은 변경에서 뺀다. 패키지
    안 일부만 고른 요청은 자동으로 넓히지 않고 패키지 경로를 고르라고 거부한다. 다른 평면 엔진 변경의
    거부는 그대로다."""
    pending_changes = [change for change in changes if manifest_rules._is_engine_code(change[0])]
    if target:
        for change in [change for change in pending_changes if change[3] == "replace-dir"]:
            candidates = upstream_changes._scope_change_candidates(change[0], change[1], source_root)
            if any(upstream_changes._in_scope_paths(candidate, list(scope_paths))
                   for candidate in candidates):
                pending_changes.remove(change)
                continue
            partial = [path for path in scope_paths
                       if any(path.startswith(candidate + "/") for candidate in candidates)]
            if partial:
                print(
                    f"오류: 엔진 패키지는 디렉터리 단위로만 옮긴다 — 패키지 안 일부 경로({', '.join(partial)})"
                    f"만 전파하지 않는다. --paths {change[0]} 로 패키지 전체를 고르라.",
                    file=sys.stderr,
                )
                return 1
    pending = sorted(str(change[0]).replace("\\", "/") for change in pending_changes)
    if not pending:
        return 0
    print(
        f"오류: 엔진 코드 {len(pending)}건이 상류와 달라 부분 전파(--paths)를 하지 않는다. 먼저 "
        f"--paths 없이 갱신해 엔진 코드를 받은 뒤 다시 실행하라: {', '.join(pending)}",
        file=sys.stderr,
    )
    return 1


def _transfer_engine_code(home, args, argv: list[str], scope_paths: list[str]) -> int:
    """전달 프로세스 — 엔진 코드만 옮기고 받은 갱신기를 완료 프로세스로 띄운다(rc 는 그 프로세스의 것).

    대상은 실행 Home(`home`)의 트리다. 상류 해소·manifest 자기치유 판정·엔진 코드 계획은 전부
    복사 **전**에 끝난다. 엔진 패키지에 변경이 있으면 같은 부모의 락 안에서 패키지를 디렉터리째 먼저
    교체하고(`package_transfer.engine_package_delivery`), 나머지 평면 엔진 파일을 복사한 뒤 이전 패키지
    backup 을 정리한다. 정리까지 성공해야 완료 프로세스를 띄운다 — 교체·복사·정리 실패는
    `PackageReplaceError` 로 올라가 완료 프로세스가 뜨지 않는다. 복사를 시작한 뒤에는 엔진 모듈을 새로
    적재하지 않는다. 인계 파일 수는 평면 행 수 + 패키지의 추가·변경·삭제 파일 수다. `--dry-run` 은 엔진
    코드 변경이 있으면 그 변경만 보여 주고 끝난다(받은 갱신기가 디스크에 없어 렌더·훅·어댑터 미리보기를
    그릴 수 없다). `--paths` 는 엔진 코드 변경이 남아 있으면 어떤 쓰기보다 먼저 거부한다."""
    rc, _dest_root, source_root = upstream_changes._resolve_dest_source(home, args)
    if rc:
        return rc
    effective_dest = home.root
    try:
        engine = update_plan._engine_code_plan(home, effective_dest, source_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except retired_paths.RetiredPathError as exc:
        print(f"[중단] retired-path manifest 자기치유 실패 — {exc}", file=sys.stderr)
        return 1
    changes = engine["changes"]
    package_plan = engine["package_plan"]
    # `--paths` 의 source 부재 판정은 스코프 안 항목만 본다 — 완료 프로세스의 스코프 판정이 한다
    #   (스코프 밖 부재가 정상 부분 전파를 막지 않는다).
    rc = (_refuse_scope_with_pending_engine_code(changes, source_root=source_root) if scope_paths
          else update_adapters._refuse_missing_sources(engine["missing"], engine["manifest"]))
    if rc:
        return rc
    transferred = package_transfer.change_file_count(changes, package_plan)
    _print_engine_changes(changes, package_plan)
    if args.dry_run and changes:
        print(f"[dry-run] 엔진 코드 {transferred} 파일 변경 예정 (적용 안 함).")
        print("  렌더·진입 문서·훅·어댑터 설정 미리보기는 엔진 코드를 받은 뒤 다시 --dry-run 을 "
              "실행하면 나온다.")
        return 0
    if not args.dry_run:
        # 엔진 패키지 먼저, 나머지 평면 엔진 파일 다음이다. `apply` 는 훅 세트 판정자와 렌더 입력을
        #   해소하므로 부르지 않는다(엔진 코드 행은 훅 세트·렌더 대상이 아니다).
        flat_changes = [change for change in changes if change[3] != "replace-dir"]
        with (package_transfer.engine_package_delivery(package_plan)
              if len(flat_changes) < len(changes) else contextlib.nullcontext()):
            for _rel, src, dst, _kind in flat_changes:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    return _launch_completion(
        effective_dest, source_root, 0 if args.dry_run else transferred, argv)


def _print_engine_changes(changes, package_plan) -> None:
    """변경 행을 `[kind] 경로` 로 보인다 — 패키지 교체 행에는 추가·변경·삭제 파일 수를 붙인다."""
    for rel, _src, _dst, kind in changes:
        if kind == "replace-dir":
            print(f"  [replace-dir] {rel} (추가 {len(package_plan.added)}·"
                  f"변경 {len(package_plan.updated)}·삭제 {len(package_plan.deleted)})")
        else:
            print(f"  [{kind}] {rel}")


def _launch_completion(dest_root: Path, source_root: Path, transferred: int,
                       argv: list[str]) -> int:
    """받은 갱신기를 같은 인자로 새 프로세스에서 띄우고 그 출력과 종료 코드를 그대로 돌려준다.

    출력은 이 프로세스가 받아 같은 스트림에 다시 쓴다 — 완료 프로세스의 부모는 python.exe 라
    PowerShell 캡처를 알아보지 못하므로, 직접 쓰게 하면 그 파이프에서 한글이 깨진다. 이 프로세스의
    콘솔 판정(`console`)이 그대로 적용되게 줄 단위로 중계한다. `PYTHONIOENCODING=utf-8` 은
    완료 프로세스가 콘솔 설정에 닿기 전의 출력도 UTF-8 로, `PYTHONUNBUFFERED=1` 은 두 스트림의 줄이
    도착 순서대로 오게 한다. 기동 실패(인터프리터·파일 부재)는 그대로 올라간다. 테스트 교체 지점이다
    (`tests/_manager_home.complete_in_process`)."""
    sys.stdout.flush()
    sys.stderr.flush()
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        COMPLETION_ENV: json.dumps({"dest": str(dest_root), "source": str(source_root),
                                    "transferred": transferred}),
    }
    updater = Path(dest_root) / ".project_manager" / "tools" / "pm_update.py"
    proc = subprocess.Popen([sys.executable, str(updater), *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    readers = [threading.Thread(target=_relay_lines, args=(proc.stdout, sys.stdout)),
               threading.Thread(target=_relay_lines, args=(proc.stderr, sys.stderr))]
    for reader in readers:
        reader.start()
    rc = proc.wait()
    for reader in readers:
        reader.join()
    return rc


def _relay_lines(pipe, stream) -> None:
    """파이프의 UTF-8 줄을 읽어 이 프로세스의 스트림에 그대로 쓴다(줄마다 flush)."""
    for line in io.TextIOWrapper(pipe, encoding="utf-8", errors="replace"):
        stream.write(line)
        stream.flush()
