"""Home별 판단 원칙 레지스트리와 recall marker 저장소."""
from __future__ import annotations
import json
from pm_engine.infra import fs, pm_paths
from pathlib import Path
from pm_engine.domain import ctx_recovery, principle_rules

_SHIPPED_REL = Path("pm_principles.md")
_LOCAL_REL = Path("pm_principles.local.md")
_MARKER_DIR_REL = Path("principle-recall")


def _read_text(path: Path) -> str:
    """레지스트리·marker 판독 한 지점(공용 읽기 seam 위임). 파일 부재만 빈 본문이고 다른 판독 오류는 올린다."""
    try:
        return fs.read_text_shared(path, encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_all(home) -> tuple[tuple[principle_rules.Rule, ...], int]:
    """출하층 + 로컬층 합성(같은 key 면 로컬층이 이긴다) + 합산 파손 건수.

    파일 부재는 파손이 아니다(채택자 형상 — 로컬층 없음이 정상). 디렉터리·권한 등 실제 판독 불능은
    규칙 0건으로 접지 않고 그대로 올린다. 형식이 틀린 항목은 파손 건수로 센다."""
    shipped_rules, shipped_broken = principle_rules._parse_text(
        _read_text(pm_paths.wiki_dir(home) / _SHIPPED_REL), layer="shipped"
    )
    local_rules, local_broken = principle_rules._parse_text(
        _read_text(pm_paths.wiki_dir(home) / _LOCAL_REL), layer="local"
    )
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
        / f"{ctx_recovery.safe_session_id(session_id)}.json"
    )


def load_seen_marker(home, session_id) -> set[str]:
    """이번 세션에 이미 주입한 규칙 key 집합(marker 부재·JSON 형식 파손은 빈 집합, 판독 불능은 예외)."""
    path = _marker_path(home, session_id)
    try:
        data = json.loads(_read_text(path))
    except ValueError:
        return set()
    if isinstance(data, list):
        return {str(k) for k in data}
    return set()


def record_seen_marker(home, session_id, keys) -> None:
    """새로 주입한 규칙 key 를 marker 에 합친다. 쓰기 오류는 그대로 올린다."""
    keys = [str(k) for k in keys or ()]
    if not keys:
        return
    path = _marker_path(home, session_id)
    existing = load_seen_marker(home, session_id)
    existing.update(keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(existing)), encoding="utf-8", newline="\n")


def rearm_seen_marker(home, session_id) -> None:
    """압축 경계에서 marker 를 지워 다음 사이클에 규칙을 다시 주입 가능하게 한다. 삭제 오류는 올린다."""
    _marker_path(home, session_id).unlink(missing_ok=True)
