#!/usr/bin/env python3
"""engine.manifest 기반 배포 sync — upstream 엔진 경로만 덮어쓴다.

엔진/상태 분리의 managed-manifest 배포. 인스턴스 상태(tickets·status·log·
decisions/*.md·areas.md…)와 per-clone 로컬(board.md·pm_state·local.conf·.local)은
manifest 밖이라 절대 건드리지 않으므로, upstream 갱신이 인스턴스와 *구조적으로*
충돌하지 않는다 (수동 MERGE 백포트의 기계화).

사용:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> [--dry-run] [--version V]

동작:
  engine.manifest 의 각 경로를 <upstream>/<path> → <this-repo>/<path> 로 복사(overwrite).
  디렉토리는 재귀. manifest 에 없는 경로는 무시. --dry-run = 변경 예정만 출력(미적용).

결정:
  - merge 아니라 overwrite (엔진은 upstream 단일 진실). 커스터마이즈 가능 문서는 manifest 에서
    제외 — overlay 메커니즘은 후속.
  - 어떤 경로를 엔진으로 볼지는 *이 인스턴스의* engine.manifest 가 정한다(없으면 source 의 것).
  - stdlib 만. plan/apply 분리로 테스트 결정론.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / ".project_manager" / "engine.manifest"
VERSION_FILE = REPO / ".project_manager" / "engine.version"


def read_manifest(path: Path) -> list[str]:
    """manifest 파일 → 경로 리스트 ('#' 주석·빈 줄 제외)."""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _iter_files(root: Path, rel: str):
    """manifest 엔트리(파일/디렉토리) → (repo 기준 relpath, src 절대경로) 들."""
    src = root / rel
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(root)), p
    elif src.is_file():
        yield rel, src
    # missing → 아무것도 yield 안 함 (호출부가 missing 으로 보고)


def plan(source_root: Path, manifest: list[str]) -> tuple[list[tuple], list[str]]:
    """(changes, missing) 반환. changes = [(rel, src, dst, kind)] (kind: new|update)."""
    changes: list[tuple] = []
    missing: list[str] = []
    for rel in manifest:
        if not (source_root / rel).exists():
            missing.append(rel)
            continue
        for r, sp in _iter_files(source_root, rel):
            dst = REPO / r
            if not dst.exists():
                changes.append((r, sp, dst, "new"))
            elif not filecmp.cmp(sp, dst, shallow=False):
                changes.append((r, sp, dst, "update"))
    return changes, missing


def apply(changes: list[tuple]) -> None:
    for _r, sp, dst, _kind in changes:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dst)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pm_update.py", description=__doc__)
    ap.add_argument("--from", dest="source", required=True,
                    help="upstream 프레임워크 checkout 경로")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--version", help="동기화 후 기록할 엔진 버전 (engine.version)")
    args = ap.parse_args(argv)

    source_root = Path(args.source).resolve()
    manifest_path = MANIFEST if MANIFEST.exists() else source_root / "engine.manifest"
    if not manifest_path.exists():
        print("engine.manifest 없음 (this repo·source 둘 다).", file=sys.stderr)
        return 1

    manifest = read_manifest(manifest_path)
    changes, missing = plan(source_root, manifest)

    for r, _sp, _dst, kind in changes:
        print(f"  [{kind}] {r}")
    for r in missing:
        print(f"  [source 에 없음] {r}", file=sys.stderr)

    if not changes:
        print("최신 — 변경 없음.")
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(changes)} 파일 변경 예정 (적용 안 함).")
        return 0

    apply(changes)
    msg = f"✓ {len(changes)} 파일 동기화"
    if args.version:
        VERSION_FILE.write_text(args.version + "\n", encoding="utf-8")
        msg += f" · engine.version={args.version}"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
