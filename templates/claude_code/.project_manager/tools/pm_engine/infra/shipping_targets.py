"""실행 Home 의 출하 template 타깃 열거·해소 (층: infra)."""
from __future__ import annotations

from pathlib import Path
import os


def resolve_target_root(home, target_name: str) -> Path:
    """타깃 이름 → 동기화 대상 루트 경로 (항상 `home.root/templates/<target_name>/`).

    source(--from)와 dest는 독립적이다:
    - source_root(--from): 엔진 파일을 읽어오는 곳
    - dest(이 함수 반환값): 실행 Home 의 templates/<target>/

    따라서 --from 이 다른 checkout 이어도 dest 는 항상 실행 Home 을 가리킨다.

    타깃 유효성은 `home.root/templates/<name>/` 디렉토리 존재로 판단한다.
    새 타깃 추가가 이 파일 수정을 강제하지 않는다.

    보안: target_name 은 단일 path segment 이어야 한다.
    '/', os.sep, '..', 빈 문자열을 포함하면 path traversal 로 간주해 거부한다.
    이후 resolve() 결과의 parent 가 `home.root/templates/` 임을 이중 검증한다.
    """
    # ── 1차: 단일 segment 검증 (빠른 거부) ──────────────────────────────────
    if (
        not target_name
        or "/" in target_name
        or os.sep in target_name
        or target_name == ".."
        or target_name.startswith("../")
        or ".." in target_name.split("/")
    ):
        raise ValueError(
            f"잘못된 타깃 이름: {target_name!r}. "
            "타깃은 단일 path segment 이어야 한다 ('/', '..', 빈 문자열 불허)."
        )

    # ── 2차: resolve() 후 parent 검증 (symlink·우회 방어) ───────────────────
    templates_dir = home.root / "templates"
    templates_resolved = templates_dir.resolve()
    candidate = (templates_dir / target_name).resolve()
    if candidate.parent != templates_resolved:
        raise ValueError(
            f"타깃 경로 탈출 시도: {target_name!r} → {candidate}. "
            f"허용 범위: {templates_resolved}/<name>."
        )

    target_root = candidate
    if not target_root.is_dir():
        if templates_dir.is_dir():
            known = sorted(p.name for p in templates_dir.iterdir() if p.is_dir())
        else:
            known = []
        known_hint = ", ".join(known) if known else "(없음)"
        raise FileNotFoundError(
            f"알 수 없는 타깃 또는 디렉토리 없음: {target_name!r}. "
            f"REPO/templates/<name>/ 디렉토리를 먼저 만들어라. "
            f"현재 발견된 타깃: {known_hint}"
        )
    return target_root


def discover_target_names(home) -> list[str]:
    """실행 Home 의 `templates/` 직계 디렉토리의 타깃 이름을 정렬해 반환한다.

    타깃 추가 시 CLI의 고정 목록을 갱신하지 않도록 `resolve_target_root`와 같은
    디렉토리-발견 규칙을 사용한다. `--all-targets`의 대상은 이 함수의 반환값이다.
    """
    templates_dir = home.root / "templates"
    if not templates_dir.is_dir():
        return []
    # 숨김 디렉토리(.git 류)는 타깃이 아니다 — 비-숨김 직계 디렉토리는 전부 타깃으로 간주한다
    # (templates/ 는 관례상 타깃 전용·문서 열거 ↔ 디렉토리 집합 일치는 enumeration 가드가 강제).
    return sorted(path.name for path in templates_dir.iterdir()
                  if path.is_dir() and not path.name.startswith("."))
