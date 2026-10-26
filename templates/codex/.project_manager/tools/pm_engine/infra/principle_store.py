"""Home별 판단 원칙 레지스트리와 recall marker 저장소."""
from __future__ import annotations
import json
from pm_engine.infra import fs, pm_paths
from pathlib import Path
from pm_engine.domain import principle_rules

_SHIPPED_REL = Path("pm_principles.md")
_LOCAL_REL = Path("pm_principles.local.md")
_MARKER_DIR_REL = Path("principle-recall")


def _read_text(path: Path) -> str:
    """레지스트리·marker 판독 한 지점(공용 읽기 seam 위임). 부재·판독 실패는 빈 본문."""
    try:
        return fs.read_text_shared(path, encoding="utf-8")
    except OSError:
        return ""


def _load_all(home) -> tuple[tuple[principle_rules.Rule, ...], int]:
    """출하층 + 로컬층 합성(같은 key 면 로컬층이 이긴다) + 합산 파손 건수.

    파일 부재는 파손이 아니다(채택자 형상 — 로컬층 없음이 정상). 읽기 실패·존재하지 않는 경로는
    빈 본문으로 접혀 규칙 0건을 낼 뿐 예외를 던지지 않는다(비차단 계약)."""
    shipped_rules, shipped_broken = principle_rules._parse_text(
        _read_text(pm_paths.wiki_dir(home) / _SHIPPED_REL), layer="shipped"
    )
    local_path = pm_paths.wiki_dir(home) / _LOCAL_REL
    if local_path.is_file():
        local_rules, local_broken = principle_rules._parse_text(
            _read_text(local_path), layer="local"
        )
    else:
        local_rules, local_broken = ((), 0)
    merged: dict[object, principle_rules.Rule] = {}
    order: list[object] = []
    for rule in (*shipped_rules, *local_rules):
        key = principle_rules._dedup_key(rule)
        if key not in merged:
            order.append(key)
        merged[key] = rule
    return (tuple((merged[key] for key in order)), shipped_broken + local_broken)


def _marker_path(home, session_id) -> Path:
    return (
        home.local_dir
        / _MARKER_DIR_REL
        / f"{principle_rules._safe_session_id(session_id)}.json"
    )


def load_seen_marker(home, session_id) -> set[str]:
    """이번 세션에 이미 주입한 규칙 key 집합(marker 부재·파손은 빈 집합 — 비차단)."""
    path = _marker_path(home, session_id)
    try:
        data = json.loads(_read_text(path))
    except ValueError:
        return set()
    if isinstance(data, list):
        return {str(k) for k in data}
    return set()


def record_seen_marker(home, session_id, keys) -> None:
    """새로 주입한 규칙 key 를 marker 에 합친다(best-effort — 쓰기 실패는 삼킨다)."""
    keys = [str(k) for k in keys or ()]
    if not keys:
        return
    path = _marker_path(home, session_id)
    existing = load_seen_marker(home, session_id)
    existing.update(keys)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(existing)), encoding="utf-8", newline="\n")
    except OSError:
        pass


def rearm_seen_marker(home, session_id) -> None:
    """PostCompact 경계에서 marker 를 지워 다음 사이클에 규칙을 다시 주입 가능하게 한다."""
    try:
        _marker_path(home, session_id).unlink(missing_ok=True)
    except OSError:
        pass
