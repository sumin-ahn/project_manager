"""하네스 ctx 관측의 예산·밴드·안내·marker 순서 조정 (층: app).

어댑터가 native 기록에서 얻은 점유 토큰·모델을 받아 압축 한도 기록의 적용 한도 E 와 비율로
쓴 %·밴드를 정하고, 밴드 안내와 압축 경계 처리 marker 를 선점한다. 사용률·밴드 규칙과 안내 문구는
`domain.ctx_recovery`, marker 파일은 `infra.compaction_store`, 원칙 recall marker 는
`infra.principle_store` 가 소유한다. 하네스 JSON 은 읽지 않는다.
"""
from __future__ import annotations

from pm_engine.domain import conf_schema, ctx_recovery
from pm_engine.infra import compaction_store, conf_store, principle_store


# 응답 형식 세대 — 어댑터·JS 가 이 값으로 형식을 확인한다.
CTX_RESPONSE_VERSION = 1

# `stop` 밴드는 회전이 아니라 최종 넛지로 소비된다 — 안내 렌더·marker 종류 이름은 `final` 이다.
_BAND_GUIDANCE = {"nudge": "nudge", "nudge2": "nudge2", "stop": "final"}


def _observe(home, harness: str, tokens: int, model: str | None) -> tuple[dict, dict | None]:
    """status 응답 6키와 읽은 예산(판정 불능이면 None). 예산 기록 문제만 unavailable 로 옮긴다."""
    try:
        budget = conf_store.read_ctx_budget(
            home.local_dir.parent.parent, conf_store.local_config(home), harness, model,
        )
    except conf_schema.CtxBudgetError as exc:
        return _status_view("unavailable", tokens, None, None, str(exc)), None
    used = ctx_recovery.context_used_pct(tokens, budget["E"])
    view = _status_view("ok", tokens, used, ctx_recovery.classify(used, budget), budget["warning"])
    return view, budget


def _status_view(status: str, tokens: int, used_pct: int | None, level: str | None,
                 notice: str | None) -> dict:
    return {
        "version": CTX_RESPONSE_VERSION, "status": status, "measured": tokens > 0,
        "used_pct": used_pct, "level": level, "notice": notice,
    }


def context_status(home, *, harness: str, tokens: int, model: str | None) -> dict:
    """읽기 전용 ctx 상태(version·status·measured·used_pct·level·notice). marker 를 쓰지 않는다."""
    return _observe(home, harness, tokens, model)[0]


def _claim_compaction(home, session: str, boundary: str | None) -> tuple[bool, str | None, str | None]:
    """새 압축 경계면 (True, precompact 안내, 기록용 경계 ID), 처리 없음·이미 처리면 (False, None, None).

    안내를 먼저 렌더한 뒤 경계 marker 를 선점하고, 최초 선점이면 밴드 marker 셋과 원칙 recall marker 를
    재무장한다. 재무장이 실패하면 이번에 선점한 경계 marker 만 지워 다음 호출이 다시 처리하게 하고 원인을
    올린다. 경계는 하네스가 관측한 고유값이라 marker 이름과 기록용 경계 ID 에 그대로 쓴다."""
    if boundary is None:
        return False, None, None
    guidance = ctx_recovery.build_ctx_guard_guidance("precompact")
    kind = compaction_store.CTX_COMPACTION_MARKER_PREFIX + boundary
    if not compaction_store.claim_ctx_marker(home, session, kind):
        return False, None, None
    try:
        compaction_store.rearm_ctx_cycle(home, session)
        principle_store.rearm_seen_marker(home, session)
    except BaseException:
        compaction_store.ctx_marker_path(home, session, kind).unlink()
        raise
    return True, guidance, boundary


def _band_guidance(home, session: str, tokens: int, view: dict, budget: dict) -> str | None:
    """밴드 안내를 사이클당 한 번 낸다 — 양수 ok 실측은 재무장, 미측정 0 은 marker 보존."""
    level = view["level"]
    if level == "ok":
        if tokens > 0:
            compaction_store.rearm_ctx_cycle(home, session)
        return None
    band = _BAND_GUIDANCE[level]
    guidance = ctx_recovery.build_ctx_guard_guidance(
        band, used_pct=view["used_pct"], precompact_at_pct=budget["precompact_at_pct"],
    )
    return guidance if compaction_store.claim_ctx_marker(home, session, band) else None


def judge_context(home, *, harness: str, session: str, tokens: int, model: str | None,
                  main_thread: bool, boundary: str | None = None) -> dict:
    """ctx 판정 11키 — status 6키 + guidance·emit·compaction_new·compaction_guidance·compaction_boundary_id.

    서브에이전트(main_thread=False)는 설정·marker 를 읽지도 쓰지도 않는다. 압축 경계 처리는 예산 판정과
    독립이라 예산 기록 문제가 있어도 수행한다."""
    if not main_thread:
        view = _status_view("ok", 0, None, None, None)
        return {**view, "guidance": None, "emit": False, "compaction_new": False,
                "compaction_guidance": None, "compaction_boundary_id": None}
    compaction_new, compaction_guidance, boundary_id = _claim_compaction(home, session, boundary)
    view, budget = _observe(home, harness, tokens, model)
    guidance = _band_guidance(home, session, tokens, view, budget) if budget is not None else None
    return {
        **view, "guidance": guidance, "emit": guidance is not None,
        "compaction_new": compaction_new, "compaction_guidance": compaction_guidance,
        "compaction_boundary_id": boundary_id,
    }
