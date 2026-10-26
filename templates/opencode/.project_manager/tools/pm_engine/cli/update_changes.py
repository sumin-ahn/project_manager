"""읽기 전용 상류 변경점 명령의 출력과 종료 코드 (층: cli)."""
from __future__ import annotations

import sys
from pm_engine.app import update_manifest
from pm_engine.app import upstream_changes
from pm_engine.infra import install_conf


def _run_changes(home, args) -> int:
    """`--changes` read-only 분기 — baseline..HEAD 변경점 요약 출력(실 sync 안 함).

    dest/source 해소는 sync 와 공유(_resolve_dest_source) — URL upstream 도 거기서 같은 로컬
    캐시로 해소된다(clone/fetch). baseline(`upstream.rev`)은 *dest* local.conf
    에서 읽는다(매 sync 시 pm_update 가 기록한 마지막 동기 기준점). 전부 fail-soft·exit 0(graceful
    안내) — baseline 미기록·HEAD==baseline·baseline 도달불가 각각 메시지로 surface 한다.
    """
    rc, dest_root, source_root = upstream_changes._resolve_dest_source(home, args)
    if rc != 0:
        return rc  # 미등록·stale·캐시 clone/fetch 실패는 sync 와 동일한 명확 에러(rc≠0).

    effective_dest = dest_root if dest_root is not None else home.root
    baseline = install_conf._read_local_conf(
        effective_dest / ".project_manager" / "local.conf").get("upstream.rev", "").strip()

    # baseline 미기록(아직 sync 한 적 없음·구 import) — graceful 안내(exit 0). 다음 sync 후 추적된다.
    if not baseline:
        print(
            "upstream 변경: baseline 미기록 — 아직 동기 baseline(upstream.rev)이 local.conf 에 "
            "없다. 다음 `pm-update`(실 sync) 후 baseline 이 기록되면 변경점이 추적된다."
        )
        return 0

    # 엔진 영향 판별 manifest 는 **sync 와 동일한** `_resolve_planning_manifest` 로 해소한다
    # (self-heal 승격 우선·없으면 dest 우선 로컬·둘 다 부재면 source). 실 sync 가 그 manifest 로
    # "무엇이 엔진인가"를 정하므로 --changes 의 "엔진 영향(이번 동기가 받는 것)"도 같은 기준이어야
    # 한다 — 둘이 반드시 일치해야 한다. self-heal 해소 조건(`--target` 비발화)도 `_main` 과 같다.
    # 둘 다 부재(fresh-adopter)면 빈 manifest → 전부 'other'(graceful·엔진 영향 0 보수 표시·
    # summarize_upstream_changes 가 빈 리스트 허용). **read-only 유지** — self-heal 은 판정만 하고
    # (읽기 전용) 승격 manifest 를 디스크에 쓰지 않는다(쓰기는 실 sync 의 apply 소관).
    try:
        selfheal = (
            None if args.target
            else update_manifest.resolve_manifest_selfheal(effective_dest, source_root)
        )
        if selfheal is not None:
            # 다중-flavor 합집합의 마커 충돌은 미리보기에서도 알린다 — 실 sync 만 알리면 "미리보기는
            #   조용했는데 sync 가 경고" 가 되어, 미리보기로 판단하는 채택자가 충돌을 못 본다.
            update_manifest._print_manifest_merge_conflicts(selfheal)
        manifest = update_manifest._resolve_planning_manifest(effective_dest, source_root, selfheal)
    except FileNotFoundError:
        manifest = []

    summary = upstream_changes.summarize_upstream_changes(
        home, source_root, baseline, manifest, dest_root=effective_dest)

    # baseline rev 가 checkout 에서 도달 불가(force-push·shallow clone) — 재clone 권고(exit 0).
    if summary["status"] == "baseline_unreachable":
        print(
            f"upstream 변경: baseline {baseline[:12]} 가 upstream checkout 에서 도달 불가 "
            "(force-push 됐거나 shallow clone). upstream 을 재clone 하거나 `--from <온전한 "
            "checkout>` 으로 다시 확인하라."
        )
        return 0

    # 변경점 집계 실패(log/diff git 호출 rc≠0) — 빈 결과를 "변경 0"으로 오판하지 않게 surface
    # (codex suggestion 1·advisory 오판 금지). exit 0 유지(read-only 안내)하되 명확히 알린다.
    if summary["status"] == "summary_failed":
        print(
            f"upstream 변경: baseline {baseline[:12]} 이후 변경점 집계 실패(요약 불가) — "
            "upstream checkout 의 git log/diff 호출이 실패했다. checkout 이 온전한 git work "
            "tree 인지 확인하거나 `--from <온전한 checkout>` 으로 다시 시도하라.",
            file=sys.stderr,
        )
        return 0

    head = summary["head"]
    count = summary["count"]

    # --count-only: commit 개수 1줄만(advisory/스크립트).
    if args.count_only:
        print(str(count))
        return 0

    # HEAD == baseline(변경 0·최신) — count 0.
    if count == 0:
        print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} (변경 0·최신)")
        return 0

    # ── 3블록 요약 (채택자-facing·기본 간결) ──────────────────────────────────
    print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} ({count} commits)")

    engine = summary["engine"]
    other = summary["other"]
    removed_upstream = summary.get("removed_upstream") or []
    print(f"엔진 영향 (manifest 경로·이번 동기가 받는 것): {len(engine)} files")
    for code, path in engine:
        print(f"  {code} {path}")
    print(f"그 외 변경 (manifest 밖·동기 안 받음): {len(other)} files")
    # 삭제·rename 은 "받는 것" 두 버킷과 성격이 다르다(동기가 손대지 않는 dest 잔존) — 맨 뒤에
    # 따로 낸다. sync 실행의 은퇴 후보 보고(`_print_retired_manifest_files`)와 같은 사실을 말한다.
    print(
        "상류 삭제·rename (동기가 지우지 않음 — 아래 보고 참조): "
        f"{len(removed_upstream)} files"
    )
    for code, path in removed_upstream:
        print(f"  {code} {path}")
    # 헤더가 가리키는 "아래 보고" — 실 sync 와 같은 은퇴 후보 산출(read-only·파생만·write 0).
    #   여기서 안 부르면 그 포인터가 가리킬 대상이 없다(빈 포인터).
    update_manifest._print_retired_manifest_files(
        update_manifest._retired_manifest_files(source_root, manifest, effective_dest, set()))

    # --log: git log --oneline baseline..HEAD 꼬리.
    if args.log:
        print("커밋 (baseline..HEAD):")
        for sha, subject in summary["log"]:
            print(f"  {sha} {subject}")

    return 0
