"""현재-진실 문서의 소유 repo full OID 검증과 재핀 진입 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
import shutil
import subprocess
import sys
from pm_engine.app import freshness_lint
from pm_engine.app import freshness_pin
from pm_engine.infra import git


# ── verified_at 핀 쓰기 (repin) ────────────
# 현재-진실 문서(architecture.md·status.md·covers 보유 domain 페이지)의 `verified_at`·`repo` 를 쓰는
# 채널은 `verified-at-repin` 하나다. 핀이 없는 문서도 `--page` 로 골라 같은 명령으로 처음 쓴다.

def _repo_head_sha(home, repo: Path | None = None) -> str | None:
    """`repo`(부재=`home.root`)의 현재 HEAD sha (조회 불가 → None·fail-soft)."""
    if shutil.which("git") is None:
        return None
    repo = Path(repo) if repo is not None else home.root
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=git._BOARD_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):  # 타임아웃/바이너리 이상은 None(fail-soft).
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _canonical_commit_oid(home, sha: str, *, repo: Path | None = None) -> str | None:
    """`sha` 가 유효한 backward anchor 면 그 **canonical full OID** 를, 아니면 None (repin 쓰기).

    repin 이 **검증에서 얻은 full OID 를 그대로 기록**하게 하는 helper — 입력 축약/
    ref 문자열을 기록하면 (a) 하류 소비자가 다시 해석(canonical 위반) (b) `0123456` 같은 전부-숫자
    축약은 YAML unquoted 정수 파싱돼 재-lint 서 깨진다. `_sha_anchor_status` 재사용(read/write 대칭·
    사본 0): 고정 hex + 고정 SHA 해소(hex-이름 ref 아님) + HEAD 선조면 `(OK, full_oid)` →
    full_oid 반환·아니면 None. 검증 불가(git 부재·rc≥2 → UNKNOWN)도 None — 기록은 확실할 때만 한다."""
    verdict, full_oid = freshness_lint._sha_anchor_status(home, sha, repo=repo)
    return full_oid if verdict == freshness_lint._ANCHOR_OK else None


def cmd_verified_at_repin(home, args: argparse.Namespace) -> int:
    """`verified-at-repin` — 현재-진실 문서를 validate-all-first로 소유 repo full OID에 재핀한다."""
    owner = args.repo
    owner_repo, owner_error = freshness_lint._freshness_owner_repo(home, owner)
    if owner_repo is None:
        print(f"verified-at-repin: 소유 repo 미해소 — {owner_error}", file=sys.stderr)
        return 1
    requested = (args.sha or "").strip()
    if requested:
        sha = _canonical_commit_oid(home, requested, repo=owner_repo)
        if sha is None:
            print(f"verified-at-repin: --sha {requested} 가 {owner} repo의 HEAD 선조 commit으로 "
                  "검증되지 않는다 — 기록 중단.", file=sys.stderr)
            return 1
    else:
        sha = _repo_head_sha(home, owner_repo)
        if not sha:
            print(f"verified-at-repin: {owner} repo HEAD 조회 실패 — 기준 sha 미정.",
                  file=sys.stderr)
            return 1
    results = freshness_pin.repin_verified_at(
        home,
        sha, owner, dry_run=args.dry_run, pages=getattr(args, "pages", None))
    if not results:
        print("verified-at-repin: 대상 문서 없음 (architecture.md·status.md·covers domain 부재).")
        return 0
    prefix = "[dry-run] " if args.dry_run else ""
    updated = 0
    shown_by_path: dict[Path, str] = {}
    for path, state in results:
        try:
            shown = path.relative_to(home.root).as_posix()
        except ValueError:
            shown = str(path)
        shown_by_path[path] = shown
        if state == "updated":
            updated += 1
        stream = sys.stderr if state.startswith("error:") else sys.stdout
        print(f"{prefix}{state}: {shown} → repo={owner}, verified_at={sha}", file=stream)
    validation_errors = [(path, state) for path, state in results
                         if state.startswith(("error:read:", "error:no-frontmatter",
                                              "error:yaml:", "error:enumeration:"))]
    if validation_errors:
        details = ", ".join(f"{shown_by_path[path]}({state})" for path, state in validation_errors)
        print("verified-at-repin: 전 대상 검증 실패 — 무변경·재핀 중단: " + details,
              file=sys.stderr)
        return 1
    write_errors = [(path, state) for path, state in results if state.startswith("error:write:")]
    if write_errors:
        changed = [shown_by_path[path] for path, state in results if state == "updated"]
        changed_text = ", ".join(changed) if changed else "(없음)"
        failed_text = ", ".join(shown_by_path[path] for path, _state in write_errors)
        print(f"verified-at-repin: 쓰기 실패({failed_text}) — 이미 변경된 파일: {changed_text}",
              file=sys.stderr)
        return 1
    print(f"{prefix}{updated}개 문서 재핀 (소유 repo={owner}, anchor={sha}).")
    return 0
