"""출하 OpenCode 모델의 시험용 단일 출처.

출하 `templates/opencode/.opencode/opencode.jsonc` 의 모델 선언에서 읽는다. 시험 코드에는 출하
모델 이름 리터럴을 두지 않는다(`test_opencode_shipped_model.py` 가 지킨다).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pm_engine.domain.jsonc import _mask_jsonc_comments, _strip_jsonc_trailing_commas

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "templates" / "opencode" / ".opencode" / "opencode.jsonc"


def _read_shipped_model() -> tuple[str, str]:
    document = json.loads(_strip_jsonc_trailing_commas(_mask_jsonc_comments(SHIPPED_CONFIG.read_text(encoding="utf-8"))))
    declared = [(provider, model_id)
                for provider, body in document["provider"].items() for model_id in body["models"]]
    if len(declared) != 1:
        raise RuntimeError(f"출하 opencode.jsonc 는 모델을 정확히 1개 선언해야 한다: {declared}")
    return declared[0]


SHIPPED_PROVIDER, SHIPPED_MODEL_ID = _read_shipped_model()
SHIPPED_MODEL = f"{SHIPPED_PROVIDER}/{SHIPPED_MODEL_ID}"
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", SHIPPED_MODEL)
