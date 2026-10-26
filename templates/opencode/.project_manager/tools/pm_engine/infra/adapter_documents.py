"""어댑터 JSONC·TOML config 와 훅 커맨드 문서 판독 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import adapter_specs
from pm_engine.domain import jsonc
from pm_engine.infra import fs
import json
import re
import shlex
import tomllib


def _load_adapter_config_document(path: Path) -> dict:
    """설정 파일 → 문서 dict. 형식은 **확장자**가 정한다(하네스 이름을 묻지 않는다).

    읽기 실패·파손은 빈 문서다 — 채택자 소유 파일이라 임의 상태가 정상 범위이고
    (`_read_hook_set_config` 와 같은 관용), 빈 문서로 보면 선언 키가 전부 `missing` 으로 실려
    표에서 loud 하다(조용한 `same` 으로 접히지 않는다).

    JSONC 주석·후행 쉼표는 domain.jsonc의 공통 문법 처리로 판독한다.
    """
    path = Path(path)
    try:
        text = fs.read_text_shared(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    suffix = path.suffix.lower()
    if suffix == ".toml":
        try:
            document = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}
    if suffix == ".jsonc":
        text = jsonc._strip_jsonc_trailing_commas(jsonc._mask_jsonc_comments(text))
    try:
        document = json.loads(text)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


# 훅 항목이 커맨드를 싣는 키 — 플랫폼별로 갈린다. codex `hooks.json` 은 POSIX 용 `command` 와
# Windows 용 `commandWindows` 를 **함께** 싣고, 그 플랫폼에서 실제로 실행되는 건 후자다. 한쪽만
# 스캔하면 Windows 채택자의 세대 불일치가 통째로 판정 밖이 된다(같은 락아웃, 다른 플랫폼).
_HOOK_COMMAND_KEYS = ("command", "commandWindows")


def _hook_event_commands(document) -> dict[str, tuple[tuple[str | None, str], ...]]:
    """config document 의 훅 커맨드를 **이벤트별 (matcher, 커맨드)** 로 — `hooks.<event>[]`.

    matcher 는 그 커맨드가 든 그룹의 선언값이고, 키가 없으면 `None` 이다. 커맨드만 실으면 같은
    스크립트를 다른 matcher 로 부르는 그룹들이 한 값으로 접혀 기능 표가 그 차이를 못 본다.

    claude `settings.json` 과 codex `hooks.json` 이 같은 형상이라 추출 규칙이 하나다. 형상이
    어긋난 항목은 조용히 건너뛴다(채택자 소유 파일이라 임의 구조가 정상 범위). 세대 판정은
    이벤트를 묻지 않고(`_hook_commands`), 기능 표는 이벤트가 곧 행이라 같은 순회를 나눠 쓴다."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return {}
    out: dict[str, tuple[tuple[str | None, str], ...]] = {}
    for event, groups in hooks.items():
        commands: list[tuple[str | None, str]] = []
        if isinstance(groups, list):
            for group in groups:
                entries = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(entries, list):
                    continue
                matcher = group.get("matcher")
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for key in _HOOK_COMMAND_KEYS:
                        command = entry.get(key)
                        if isinstance(command, str) and command.strip():
                            commands.append((matcher, command))
        out[event] = tuple(commands)
    return out


def _hook_commands(document) -> list[str]:
    """config document 의 훅 커맨드 전수(이벤트·matcher 무관·선언 순서 보존)."""
    return [command
            for commands in _hook_event_commands(document).values()
            for _matcher, command in commands]


def _split_hook_command(command: str) -> list[str]:
    """훅 커맨드 → 토큰. 따옴표가 깨져 shlex 가 못 읽으면 공백 분할로 내려간다(판정 포기 0)."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _hook_script_relpath(token: str) -> str | None:
    """훅 커맨드 토큰 → dest 기준 POSIX relpath. 해소 불가(절대경로·미지 변수)면 None."""
    text = token.strip().replace("\\", "/")
    for prefix in adapter_specs._HOOK_COMMAND_ROOT_TOKENS:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not text or text.startswith("/") or "$" in text or text.startswith(".."):
        return None
    return text


# 인터프리터를 앞세우는 훅 커맨드(`bash <path> --flag`·`py -3 <path>`)의 첫 토큰. 목록은 유한하고
# basename 소문자로 대조한다(`/usr/bin/bash`·`bash.exe` 포함) — 표기가 달라도 **실행되는 스크립트**가
# 판정 단위라, 이 형태를 못 읽으면 같은 훅이 표기 하나로 판정에서 빠진다.
# 의도된 경계 2가지(보수적 miss — 거짓 red 없음·판정 누락만): 하나는 이 목록 밖 래퍼 선행(`env`·
# `command`·`winpty` 등)으로, 그 토큰이 live_files 와 안 맞아 판정 대상 밖으로 떨어진다. 다른 하나는 분해가
# POSIX shlex 라 인용 없는 백슬래시 경로(`...\x.sh`)는 이스케이프로 먹혀 miss 다 — 출하 config 는
# 전부 forward slash/인라인이라 실영향 0 이며, 확장은 실수요(백슬래시 훅 커맨드 출하) 때 한다.
_HOOK_COMMAND_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "python", "python3", "py", "node", "pwsh", "powershell",
})


def _hook_script_and_arguments(tokens: list[str]) -> tuple[str | None, list[str]]:
    """훅 커맨드 토큰 → (스크립트 relpath, 그 뒤 인자). 해소 불가면 (None, []).

    직접 실행(`<path> --flag`)과 인터프리터 선행(`bash <path> --flag`)이 **같은 판정**을 타야 한다 —
    실행되는 스크립트와 그 스크립트가 받는 플래그는 표기와 무관하게 같기 때문이다. 인터프리터 뒤의
    옵션(`py -3.12 <path>`)은 건너뛴다(스크립트는 첫 비-옵션 토큰이다)."""
    if not tokens:
        return None, []
    head = tokens[0].strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head.endswith(".exe"):
        head = head[:-len(".exe")]
    if head not in _HOOK_COMMAND_INTERPRETERS:
        return _hook_script_relpath(tokens[0]), tokens[1:]
    for index in range(1, len(tokens)):
        if tokens[index].startswith("-"):
            continue                    # 인터프리터 옵션 — 스크립트는 아직 뒤다.
        return _hook_script_relpath(tokens[index]), tokens[index + 1:]
    return None, []                     # 인터프리터만 있고 스크립트가 없다(판정 대상 아님).


def _entrypoint_groups(document, event: str) -> list:
    """config 의 그 이벤트 matcher-group 목록 (형상이 어긋나면 빈 목록)."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []


def _group_commands(group) -> list[str]:
    """그 matcher-group 이 실행하는 커맨드 전수 (POSIX·Windows 양쪽)."""
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return []
    return [entry[key] for entry in entries if isinstance(entry, dict)
            for key in _HOOK_COMMAND_KEYS
            if isinstance(entry.get(key), str) and entry[key].strip()]


def _command_invokes_entrypoint(command: str, entrypoint: adapter_specs.AdapterHookEntrypoint) -> bool:
    """**한 커맨드 문자열**이 디스패처를 그 이벤트로 실제 호출하는가.

    디스패처 경로만 보면 실행하지 않는 문자열(`printf .codex/pm_orch_codex.py`)이나 다른
    이벤트를 부르는 커맨드까지 진입점으로 인정된다 — 발화하지 않는 config 가 소견 0 으로
    통과하므로, 경로와 호출 값을 **같은 문자열 안에서 함께** 요구한다. 경로 판정이 포함
    여부인 것은 출하 커맨드가 인터프리터 해소를 담은 셸 한 줄(`if command -v python3 …`)이라
    argv 분해로는 스크립트가 안 나오고, 하네스가 해소하는 루트 토큰(`${CLAUDE_PROJECT_DIR}/`)
    같은 접두가 붙어도 같은 파일을 가리키기 때문이다."""
    normalized = command.replace("\\", "/")
    if entrypoint.dispatcher not in normalized:
        return False
    if not entrypoint.flag:
        return True
    # 이벤트 이름은 **경계까지** 대조한다 — 접미가 붙은 다른 이벤트(`PreToolUseLegacy`)를
    #   같은 값으로 세면 값 대조가 다시 포함 판정으로 되돌아간다.
    return re.search(
        rf"{re.escape(entrypoint.flag)}[\s=]+['\"]?"
        rf"{re.escape(entrypoint.event)}['\"]?(?![\w.-])",
        normalized) is not None


def _entrypoint_is_present(document, entrypoint: adapter_specs.AdapterHookEntrypoint) -> bool:
    """그 진입점이 config 에 **값으로** 있는가 — matcher 일치 + 그 이벤트로의 디스패처 호출."""
    for group in _entrypoint_groups(document, entrypoint.event):
        if group.get("matcher") != entrypoint.matcher:
            continue
        if any(_command_invokes_entrypoint(command, entrypoint)
               for command in _group_commands(group)):
            return True
    return False
