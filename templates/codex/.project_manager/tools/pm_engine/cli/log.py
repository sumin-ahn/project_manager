"""로그 명령과 복구 경계 CLI."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import areas_store
import argparse
from pm_engine.infra import compaction_store
from pm_engine.infra import console
import contextlib
from pm_engine.domain import ctx_recovery
import datetime
from pm_engine.infra import fs
from pm_engine.domain import identity_args
import json
from pm_engine.domain import log_entries
from pm_engine.infra import log_store
from pm_engine.infra import pm_paths
from pm_engine.app import session_identity
from pm_engine.app import session_snapshot
import subprocess
import sys


def cmd_tail(home, args: argparse.Namespace) -> int:
    if not (pm_paths.wiki_dir(home) / 'log/current.md').exists():
        print(f"(current.md 없음: {_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} — migrate 먼저)", file=sys.stderr)
        return 2
    _preamble, entries = log_entries.split_entries(log_store._read_text_exact((pm_paths.wiki_dir(home) / 'log/current.md')))
    if not entries:
        print("(entry 없음)")
        return 0
    print(entries[-1][1].rstrip())
    return 0


def cmd_archive(home, args: argparse.Namespace) -> int:
    before = getattr(args, "before", None)
    keep_last = getattr(args, "keep_last", None)

    # --before 와 --keep-last 는 상호배타 — 정확히 하나만 지정한다 (둘 다/둘 다 없음 거부).
    # argparse mutex 그룹이 CLI 에서 "둘 다"를 먼저 걸러내지만, 함수 직접 호출(테스트) 경로에서도
    # "정확히 하나" 를 못박는다.
    if (before is None) == (keep_last is None):
        print("archive: --before DATE 와 --keep-last N 중 정확히 하나를 지정하세요 "
              "(둘 다/둘 다 없음 불가).", file=sys.stderr)
        return 1

    cutoff = None
    if before is not None:
        try:
            cutoff = datetime.date.fromisoformat(before)
        except ValueError:
            print(f"--before 날짜 형식 오류: {before!r} (YYYY-MM-DD)", file=sys.stderr)
            return 1

    # 존재 확인부터 최신 내용 read, archive index 발행, current 원자 교체까지 한 lock
    # 구간이다. append writer도 같은 lock을 쓰므로 archive가 읽은 뒤 들어온 entry가
    # stale `new_current`에 덮여 유실되는 interleave가 없다.
    with log_store.log_write_lock((pm_paths.wiki_dir(home) / 'log/current.md')):
        if not (pm_paths.wiki_dir(home) / 'log/current.md').exists():
            print(f"(current.md 없음: {_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} — migrate 먼저)", file=sys.stderr)
            return 2

        text = log_store._read_text_exact((pm_paths.wiki_dir(home) / 'log/current.md'))
        preamble, entries = log_entries.split_entries(text)

        if cutoff is not None:
            # 날짜 기반: DATE 미만(strict <)만 봉인, DATE 이상은 유지.
            old = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) < cutoff]
            keep = [(d, e) for d, e in entries if datetime.date.fromisoformat(d) >= cutoff]
            mode_line = f"--before {before}"
            noop_msg = f"옮길 entry 없음 (--before {before} 미만 entry 0개) — no-op."
        else:
            # 개수 기반: 최근 N entry(tail)만 유지, 나머지 오래된 쪽을 봉인. entry 단위.
            n = keep_last
            old = entries[:-n] if n < len(entries) else []
            keep = entries[-n:] if n < len(entries) else entries
            mode_line = f"--keep-last {n}"
            noop_msg = f"옮길 entry 없음 (entry {len(entries)}개 ≤ --keep-last {n}) — no-op."

        if not old:
            print(noop_msg)
            return 0

        idx = log_store.next_archive_index((pm_paths.wiki_dir(home) / 'log/archive'))
        first, last = old[0][0], old[-1][0]
        slice_name = f"{idx:04d}-{first}_to_{last}.md"
        slice_path = (pm_paths.wiki_dir(home) / 'log/archive') / slice_name
        slice_body = (
            f"# Log archive {idx:04d} ({first} ~ {last})\n\n"
            f"> `pm_log.py archive {mode_line}` 로 current.md 에서 봉인. 수정 금지.\n\n"
            + "".join(e for _d, e in old)
        )
        new_current = preamble + "".join(e for _d, e in keep)

        if args.dry_run:
            print(f"[dry-run] {_rel(home, slice_path)} 로 {len(old)} entry 봉인, "
                  f"current.md 는 {len(keep)} entry 유지.")
            return 0

        (pm_paths.wiki_dir(home) / 'log/archive').mkdir(parents=True, exist_ok=True)
        slice_path.write_text(slice_body, encoding="utf-8", newline="\n")
        log_store._replace_atomic((pm_paths.wiki_dir(home) / 'log/current.md'), new_current)
        print(f"✓ {len(old)} entry → {_rel(home, slice_path)} 봉인. "
              f"current.md {len(keep)} entry 유지.")
        return 0


def cmd_migrate(home, args: argparse.Namespace) -> int:
    """기존 단일 log.md → archive/0000-legacy.md 봉인 + current.md 생성. 멱등."""
    if (pm_paths.wiki_dir(home) / 'log/current.md').exists():
        print(f"이미 마이그레이션됨 ({_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} 존재) — no-op.")
        return 0

    legacy_dst = (pm_paths.wiki_dir(home) / 'log/archive') / "0000-legacy.md"
    if args.dry_run:
        if (pm_paths.wiki_dir(home) / 'log.md').exists():
            print(f"[dry-run] {_rel(home, pm_paths.wiki_dir(home) / 'log.md')} → {_rel(home, legacy_dst)} 봉인 + "
                  f"{_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} 생성.")
        else:
            print(f"[dry-run] 기존 log.md 없음 — 빈 {_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} 만 생성.")
        return 0

    (pm_paths.wiki_dir(home) / 'log/archive').mkdir(parents=True, exist_ok=True)
    if (pm_paths.wiki_dir(home) / 'log.md').exists():
        legacy_text = log_store._read_text_exact((pm_paths.wiki_dir(home) / 'log.md'))
        sealed = (
            "# Log archive 0000 (legacy — 마이그레이션 이전 단일 log.md)\n\n"
            "> 구조 전환 전의 기존 `log.md` 를 그대로 봉인. 수정 금지. "
            "이후 새 entry 는 `log/current.md`.\n\n"
            + legacy_text
        )
        legacy_dst.write_text(sealed, encoding="utf-8", newline="\n")
        (pm_paths.wiki_dir(home) / 'log.md').unlink()
        print(f"✓ {_rel(home, pm_paths.wiki_dir(home) / 'log.md')} → {_rel(home, legacy_dst)} 봉인.")
    else:
        print("기존 log.md 없음 — 빈 current.md 만 생성.")

    (pm_paths.wiki_dir(home) / 'log/current.md').write_text(ctx_recovery.CURRENT_HEADER, encoding="utf-8", newline="\n")
    ((pm_paths.wiki_dir(home) / 'log/archive') / ".gitkeep").touch()
    print(f"✓ {_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} 생성.")
    return 0


def cmd_ctx_guidance(args: argparse.Namespace) -> int:
    """ctx 가드 공유 안내를 raw text 또는 Codex hook JSON envelope로 출력한다."""
    text = ctx_recovery.build_ctx_guard_guidance(
        args.band,
        used_pct=args.used_pct,
        precompact_at_pct=args.precompact_at_pct,
    )
    if args.json:
        console.write_machine_line(json.dumps(
            {"systemMessage": text, "suppressOutput": False},
            ensure_ascii=False,
            separators=(",", ":"),
        ))
    else:
        print(text)
    return 0


def cmd_snapshot(home, args: argparse.Namespace) -> int:
    text, warning = session_snapshot.build_snapshot(home, args.session_id)
    if warning:
        print(warning, file=sys.stderr)
    if not text:
        # 주입할 것을 만들지 못한 실행(세션 task 미해소)은 실패다. 실패에 반쪽 답(무응답 엔벨로프)을
        # 내지 않는다: 소비자가 그 한 줄을 "답" 으로 세면 조용한 생략이 되고, 셸 폴백 배선
        # (`snapshot --json || printf …`)에서는 엔벨로프가 두 줄이 된다. stdout 0줄 + rc 1 이면 세 하네스가 같은 실패를 같은 자리에서 본다.
        return 1
    if args.json:
        payload = {"systemMessage": text, "suppressOutput": False}
        console.write_machine_line(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )
    else:
        sys.stdout.write(text)
    return 0


def _registered_repos(home) -> set[str] | None:
    """예약 task 판정용 등록 repo를 fail-soft 해소한다."""
    try:
        return areas_store.registered_repos(home)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화.
        return None


def cmd_checkpoint(home, args: argparse.Namespace) -> int:
    """checkpoint 골격 append. compaction은 boundary/phase marker로 경계당 1건만 허용한다."""
    task = getattr(args, "task", None)
    identity_name = task
    if not task:
        identity_name, reason = session_identity.resolve_session_task(home, getattr(args, "session_id", None))
        if identity_name is None:
            print(
                f"[중단] checkpoint 세션 task 미해소: {reason}. --task 이름 을 주거나 "
                "이 세션에서 pm_bootstrap.py --task 이름 으로 task 를 연다.",
                file=sys.stderr,
            )
            return 1
        task = identity_name
    if task is not None:
        try:
            identity_args.validate_task_name(task, _registered_repos(home))
        except identity_args.InvalidTaskName as exc:
            print(
                f"[중단] {exc} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
                "(`<repo>_<N>`)은 쓸 수 없다.",
                file=sys.stderr,
            )
            return 1
    if not (pm_paths.wiki_dir(home) / 'log/current.md').exists():
        print(f"(current.md 없음: {_rel(home, pm_paths.wiki_dir(home) / 'log/current.md')} — migrate 먼저)", file=sys.stderr)
        return 2
    entry = ctx_recovery.build_checkpoint_entry(task, args.trigger)
    breadcrumb = ctx_recovery._PRECOMPACT_BREADCRUMB if getattr(args, "breadcrumb", False) else ""
    payload = breadcrumb + "\n" + entry
    # 선행 LF는 기존 파일이 trailing newline 없이 끝나도 새 `##` entry 경계를 보장한다.
    # 이미 LF로 끝난 파일에는 빈 줄 하나가 추가될 뿐이며, 전체 payload는 단일 원자 write다.
    if args.trigger != "compaction":
        log_store.append_log((pm_paths.wiki_dir(home) / 'log/current.md'), payload)
        print(f"✓ checkpoint append: task={identity_name} · trigger={args.trigger}")
        return 0

    marker = None
    claimed = None
    # boundary 유도→marker 선점→append를 log lock 하나에 묶어 동시 호출도 같은 log 상태를 본다.
    with log_store.log_write_lock((pm_paths.wiki_dir(home) / 'log/current.md')):
        current_text = fs.read_text_shared((pm_paths.wiki_dir(home) / 'log/current.md'), encoding="utf-8")
        boundary_id, phase, implicit_state = compaction_store._resolve_compaction_boundary(home, args, task=identity_name, current_text=current_text)
        if boundary_id is not None and phase is not None:
            marker = compaction_store._compaction_marker_path(home, identity_name, getattr(args, 'session_id', None), boundary_id)
            claimed = compaction_store.claim_compaction_checkpoint(marker, phase=phase)
            if claimed is False:
                if phase == "post":
                    compaction_store._clear_implicit_boundary_state(implicit_state, boundary_id)
                print(
                    f"✓ checkpoint dedup skip: task={identity_name} · trigger=compaction"
                )
                return 0
            if claimed is None:
                print(
                    "[pm-checkpoint] dedup 장부 I/O 실패 — durable checkpoint 기록은 계속합니다.",
                    file=sys.stderr,
                )
        else:
            print(
                "[pm-checkpoint] boundary/phase 식별자 없음 — dedup 없이 durable checkpoint를 기록합니다.",
                file=sys.stderr,
            )
        try:
            log_store.append_log_locked((pm_paths.wiki_dir(home) / 'log/current.md'), payload)
        except Exception:
            # 같은 log lock 안에서만 rollback하므로 후속 선점과 unlink 경쟁이 생기지 않는다.
            if marker is not None and claimed is True:
                with contextlib.suppress(OSError):
                    marker.unlink()
            raise
        if phase == "post":
            compaction_store._clear_implicit_boundary_state(implicit_state, boundary_id)
    print(f"✓ checkpoint append: task={identity_name} · trigger={args.trigger}")
    return 0


def _rel(home, path: Path) -> str:
    try:
        return str(path.relative_to(home.root))
    except ValueError:
        return str(path)


def _positive_int(value: str) -> int:
    """argparse type — 양의 정수(≥1)만 허용. 0·음수·비정수는 거부."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아님: {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"양의 정수여야 함 (≥1): {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pm_log.py', description='log 의미단위 읽기 + 아카이브 도구.')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('tail', help='current.md 의 마지막 entry 만 출력')
    p.set_defaults(fn=cmd_tail)
    p = sub.add_parser('archive', help='entry 를 archive/ 로 봉인 (--before DATE | --keep-last N)')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--before', metavar='YYYY-MM-DD', help='이 날짜 미만의 entry 를 아카이브')
    mode.add_argument('--keep-last', type=_positive_int, metavar='N', help='최근 N entry 만 남기고 나머지(오래된 쪽)를 아카이브')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(fn=cmd_archive)
    p = sub.add_parser('migrate', help='기존 log.md → archive/0000-legacy.md + current.md')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser('ctx-guidance', help='세 하네스 공용 ctx 연속성 안내 출력 (쓰기 0)')
    p.add_argument('--band', choices=('nudge', 'nudge2', 'final', 'precompact'), required=True, help='안내 강도/경계 종류')
    p.add_argument('--used-pct', type=int, metavar='N', help='관측 컨텍스트 사용률(사용 한도 대비 쓴 %%)')
    p.add_argument('--precompact-at-pct', type=int, metavar='N', help='final 밴드의 압축 임박 경고 비율(ctx.precompact_at_pct)')
    p.add_argument('--json', action='store_true', help='Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력')
    p.set_defaults(fn=cmd_ctx_guidance)
    p = sub.add_parser('checkpoint', help='compaction/manual 경계 보충 박제 골격 append')
    p.add_argument('--task', metavar='이름', help='checkpoint 귀속 task 이름 (생략 시 --session-id 로 그 세션이 연 task)')
    p.add_argument('--trigger', choices=('compaction', 'manual'), default='manual', help='박제 계기 (기본값: manual)')
    p.add_argument('--session-id', help='하네스 세션 식별자 (task 판정 입력·compaction dedup 1순위 키)')
    p.add_argument('--boundary-id', help='하네스가 관측한 compaction 경계 식별자')
    p.add_argument('--phase', choices=('pre', 'post'), help='compaction hook phase')
    p.add_argument('--breadcrumb', action='store_true', help='Claude PreCompact breadcrumb를 checkpoint와 같은 PM 홈 append에 포함')
    p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser('snapshot', help='compaction 뒤 재주입할 PM 정체성·장부 포인터 출력')
    p.add_argument('--session-id', help='하네스 세션 식별자 (그 세션이 연 task 를 찾는다)')
    p.add_argument('--json', action='store_true', help='Codex hook envelope(systemMessage/suppressOutput) JSON 하나로 출력')
    p.set_defaults(fn=cmd_snapshot)
    return parser


def main(home, argv: list[str] | None=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    if args.cmd in {'snapshot', 'checkpoint'}:
        pm_home = pm_paths.owning_pm_home(home.root)
        if pm_home != home.root.resolve(strict=False):
            canonical = pm_home / '.project_manager' / 'tools' / 'pm_log.py'
            if not canonical.is_file():
                print(f'[pm-{args.cmd}] PM 홈 엔진 부재 — 경계 처리를 생략: {canonical}', file=sys.stderr)
                return 2
            timeout = ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS if args.cmd == 'snapshot' else 5.0
            try:
                result = subprocess.run([sys.executable, str(canonical), *raw_argv], cwd=str(pm_home), timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f'[pm-{args.cmd}] PM 홈 엔진 실행 실패: {exc}', file=sys.stderr)
                return 1
            return result.returncode
    return args.fn(args) if args.cmd == 'ctx-guidance' else args.fn(home, args)
