"""설치 공통 import_args (cli)."""
from __future__ import annotations

from pm_engine.domain import install_rules
import argparse

def _parse_harness_arg(value: str, *, engine_rev: str) -> tuple[str, ...]:
    """argparse 경계: 집합 파서 오류를 표준 CLI usage 오류로 바꾼다."""
    try:
        install_rules._enforce_legacy_harness_alias_deadline(engine_rev)
        return install_rules.parse_harness_selection(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _cli_description(module_doc: str) -> str:
    """모듈 도움말의 fill 목록도 현재 registry에서 렌더한다."""
    registered = tuple(install_rules.HARNESS_TEMPLATE_DIRS)
    return module_doc.replace(
        "@REGISTERED_FILL_HARNESSES@", ",".join(registered)
    ).replace(
        "@REGISTERED_FILL_BINARIES@",
        "·".join(f"`{harness}`" for harness in registered),
    )
