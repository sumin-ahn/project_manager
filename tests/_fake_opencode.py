"""가짜 `opencode` 실행 파일 — 모델 목록 조회(`opencode models --verbose`)의 실제 출력 형식을 재현한다.

형식(1.18.5·1.18.34 실행 파일 판독, `models` 명령 처리부): 모델마다 머리 줄 `${provider}/${id}` 를 쓰고,
`--verbose` 면 그 뒤에 `JSON.stringify(모델, null, 2)` 를 쓴다. 모델 객체는 만들 때 `variants: {}` 로
시작해 항상 `variants` 키를 갖는다. 여기서는 판독에 쓰이는 필드(`id`·`providerID`·`variants`)만 싣는다.

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


def listing_for(models: dict[str, tuple[str, ...]]) -> str:
    """`{provider/id: variants}` → `opencode models --verbose` 출력 텍스트(머리 줄 + JSON 객체 반복)."""
    blocks: list[str] = []
    for name, variants in models.items():
        provider, model_id = name.split("/", 1)
        model = {"id": model_id, "providerID": provider,
                 "variants": {variant: {} for variant in variants}}
        blocks.append(f"{name}\n{json.dumps(model, indent=2)}\n")
    return "".join(blocks)


# 2026-10-08 실측의 두 모양 — variants 없는 모델과 low·medium·high 를 받는 모델.
OLLAMA_LISTING = listing_for({
    "ollama/glm-5.2:cloud": (),
    "ollama/ornith1.5:35b-a3b": ("low", "medium", "high"),
})


def install_fake_opencode(folder: Path, listing: str, *, rc: int = 0) -> Path:
    """`folder` 에 가짜 `opencode`(0o755)와 `listing.txt` 를 둔다. 반환은 `folder`.

    인자가 `models --verbose` 면 `listing` 을 그대로 내고 rc 0, 그 밖의 인자는 rc 2 다. `rc` 가 0 이
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
        f"if [ \"$*\" = \"models --verbose\" ]; then cat {listing_path}; exit 0; fi\n"
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
