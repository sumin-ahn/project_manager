"""가짜 `opencode` 실행 파일 — 모델 목록 조회(`opencode api GET /api/model --standalone`)의 실제 출력 형식을 재현한다.

형식(2.3.0 실측): `{"location": {"directory": <작업 폴더>}, "data": [모델, ...]}`. 모델 객체는 `providerID`·`id`·
`variants`(`{"id": 이름, ...}` 배열)·`limit`(`context`·`output`, 있으면 `input`)을 늘 갖는다. 여기서는 판독에
쓰이는 필드만 싣는다. `limit` 을 주지 않은 모델은 opencode 가 목록에 없는 설정 모델에 주는 값(context 200000 ·
output 32000)을 쓴다.

`install_fake_opencode` 는 POSIX sh 스크립트를 둔다 — 쓰는 테스트는 `posix_bash_supported()` 가 거짓이면
건너뛴다(선례 `tests/test_codex_delegate_channel_guard.py`). PATH 는 `가짜 폴더 + os.pathsep + 기존 PATH`
로 앞에 둔다(스크립트가 `cat` 을 쓴다). 스크립트는 호출마다 작업 폴더(`pwd -P`)와 인자를 탭으로 묶어
`opencode-calls.txt` 에 한 줄씩 덧붙인다 — 조회 횟수와 실행 cwd 를 테스트가 이 파일로 잰다.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

CALLS_FILE = "opencode-calls.txt"
LISTING_FILE = "listing.txt"
# 가짜가 목록을 내는 인자 — 엔진의 모델 목록 조회 명령(`contract.MODEL_SOURCE["opencode"].command`)의 인자.
MODEL_QUERY_ARGS = "api GET /api/model --standalone"
# opencode 2.3.0 이 목록에 없는 설정 모델에 주는 limit(`Model.Info.default`).
DEFAULT_LIMIT = {"context": 200000, "output": 32000}


def listing_for(models: dict[str, tuple[str, ...]], limits: dict[str, dict] | None = None) -> str:
    """`{provider/id: variants}`(와 `{provider/id: limit}`) → 모델 목록 조회 출력 JSON 텍스트."""
    data = []
    for name, variants in models.items():
        provider, model_id = name.split("/", 1)
        data.append({"id": model_id, "providerID": provider,
                     "variants": [{"id": variant} for variant in variants],
                     "limit": dict((limits or {}).get(name, DEFAULT_LIMIT))})
    return json.dumps({"location": {"directory": "."}, "data": data}, indent=2) + "\n"


# 2026-10-08 실측의 두 모양 — variants 없는 모델과 low·medium·high 를 받는 모델.
OLLAMA_LISTING = listing_for({
    "ollama/glm-5.2:cloud": (),
    "ollama/ornith1.5:35b-a3b": ("low", "medium", "high"),
})


def install_fake_opencode(folder: Path, listing: str, *, rc: int = 0) -> Path:
    """`folder` 에 가짜 `opencode`(0o755)와 `listing.txt` 를 둔다. 반환은 `folder`.

    인자가 `MODEL_QUERY_ARGS` 면 `listing` 을 그대로 내고 rc 0, 그 밖의 인자는 rc 2 다. `rc` 가 0 이
    아니면 stderr 에 `boom` 을 쓰고 그 rc 로 끝난다.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LISTING_FILE).write_text(listing, encoding="utf-8", newline="\n")
    calls = shlex.quote(str(folder / CALLS_FILE))
    listing_path = shlex.quote(str(folder / LISTING_FILE))
    script = folder / "opencode"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\t%s\\n' \"$(pwd -P)\" \"$*\" >> {calls}\n"
        f"if [ {int(rc)} -ne 0 ]; then echo boom >&2; exit {int(rc)}; fi\n"
        f"if [ \"$*\" = \"{MODEL_QUERY_ARGS}\" ]; then cat {listing_path}; exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8", newline="\n",
    )
    script.chmod(0o755)
    return folder


def path_with(folder: Path, base: str | None = None) -> str:
    """가짜 폴더를 앞에 둔 PATH 값(기존 PATH 는 뒤에 그대로)."""
    rest = os.environ.get("PATH", "") if base is None else base
    return str(folder) + os.pathsep + rest


def calls(folder: Path) -> list[tuple[str, str]]:
    """가짜 실행 파일이 받은 호출 — (작업 폴더, 인자) 목록. 호출이 없으면 빈 목록."""
    log = Path(folder) / CALLS_FILE
    if not log.exists():
        return []
    return [tuple(line.split("\t", 1)) for line in log.read_text(encoding="utf-8").splitlines()]
