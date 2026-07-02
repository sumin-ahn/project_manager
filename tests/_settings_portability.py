"""settings.json portability 판정 공용 헬퍼 (T-0202).

engine.manifest **밖**(render 미탑승) settings.json + 훅 래퍼가 어느 머신/프로젝트로 verbatim
복사돼도 동작하려면 '치환 토큰 0 · 머신-특정 절대경로 0' 이어야 한다(portable-by-construction).
이 모듈은 그 판정 로직을 한곳에 모아 어댑터-parity 가드(source 트리)와 fresh-adopter e2e(import
결과)가 재사용한다(중복 command 파싱 통합).

절대경로 검사는 raw 텍스트 마커(`/home/`·드라이브)로는 `/tmp`·`/opt`·`/workspace` 등 일반 POSIX
절대경로를 놓친다(T-0202 codex must-fix). 그래서 JSON 을 **구조적으로 순회** — 운영 문자열값
(hooks/statusLine command·env 값·permissions.allow/deny 항목)만 걷어 머신-특정 절대경로를 잡는다.
산문(`_comment`)은 런타임 무영향이라 제외(false-fire 방지).
"""
from __future__ import annotations

import json
import re
import shlex

# manifest-out 이라 렌더 안 됨 → `{{...}}` 잔존은 곧 깨진 경로.
SUBST_TOKEN = re.compile(r"\{\{[A-Z_]+\}\}")

# ${CLAUDE_PROJECT_DIR} 는 런타임 해소 var(치환 불요·portable). 절대경로 판정 전에 word 토큰으로
# 중화한다 — 제거하면 '${...}/.claude' 가 '/.claude' 로 변해 절대경로처럼 보이는 false-fire 발생.
_PROJECT_DIR_VAR = "${CLAUDE_PROJECT_DIR}"
_PROJECT_DIR_SENTINEL = "CLAUDE_PROJECT_DIR"

# 절대경로 판정 공통: '/' 나 드라이브 문자가 **토큰 경계**(문자열 시작·공백·구분자 ( = , " ' 및
# 셸 연산자 > < | ; & 뒤)에 시작할 때만 절대경로로 본다 — command 는 셸 문자열이라 리다이렉션
# (`cmd >/tmp/log`·`2>/opt/x`)·파이프 직후의 절대경로도 잡아야 한다(codex T-0202 r3).
# '\x22'=" · '\x27'=' (regex 엔진이 해석 — Python 소스 인용부호 충돌 회피).
_TOKEN_START = r"(?:^|[\s(=,><|;&\x22\x27])"
# POSIX 절대경로: 토큰 시작 위치의 '/'. ':'는 구분자에서 **제외** — URL '://'·scp 'host:/path'
# false-fire 방지. '~/…' 홈·상대경로·중화된 ${CLAUDE_PROJECT_DIR}/… 는 '/'가 word char 뒤라 미매치.
_POSIX_ABS = re.compile(_TOKEN_START + r"/")
# 드라이브 문자 절대경로(json.loads 후 parsed 값이라 백슬래시 언이스케이프 — C:\ 또는 C:/). 드라이브
# 문자는 토큰 시작의 **단일** 문자여야 한다 — 'https://' 의 's:' 처럼 letters 뒤에 오는 건 URL 이라 제외.
_DRIVE_ABS = re.compile(_TOKEN_START + r"[A-Za-z]:[\\/]")


def _hook_and_statusline_commands(data: dict) -> list[str]:
    """settings.json 의 모든 hooks.*[].hooks[].command + statusLine.command (빈 문자열 제외)."""
    cmds = [
        h.get("command", "")
        for event_hooks in data.get("hooks", {}).values()
        for block in event_hooks
        for h in block.get("hooks", [])
    ]
    status_cmd = data.get("statusLine", {}).get("command")
    if status_cmd:
        cmds.append(status_cmd)
    return [c for c in cmds if c]


def referenced_hook_paths(settings_text: str) -> list[str]:
    """hooks/statusLine command 에서 참조 *실행 파일* 상대경로 추출(${CLAUDE_PROJECT_DIR}/ 접두
    제거·상대경로 그대로). 하드코딩 목록이 아니라 파싱 — 미래 훅 추가에도 자동 확장.

    command 문자열 전체가 경로라는 전제 대신 `shlex.split` 첫 토큰(=실행 파일)만 본다(codex) —
    나중에 인자·env prefix 가 붙어도 파일-실재 검사가 엉뚱한 문자열을 보지 않는다.
    """
    data = json.loads(settings_text)
    paths = []
    for cmd in _hook_and_statusline_commands(data):
        tokens = shlex.split(cmd.replace(_PROJECT_DIR_VAR + "/", ""))
        # env prefix(KEY=val) 는 건너뛰고 첫 실행-파일 토큰을 잡는다.
        executable = next((t for t in tokens if "=" not in t.split("/", 1)[0]), "")
        if executable:
            paths.append(executable)
    return paths


def operational_strings(data: dict) -> list[str]:
    """머신-특정 절대경로가 새면 안 되는 *운영* 문자열값 — hooks/statusLine command·env 값·
    permissions.allow/deny 항목. 산문(_comment)은 런타임 무영향이라 제외."""
    strings = list(_hook_and_statusline_commands(data))
    strings.extend(str(v) for v in data.get("env", {}).values())
    perms = data.get("permissions", {})
    strings.extend(perms.get("allow", []))
    strings.extend(perms.get("deny", []))
    return strings


def absolute_path_hits(value: str) -> list[str]:
    """문자열에 박힌 머신-특정 절대경로(POSIX '/…'·드라이브 'C:\\…')를 찾는다.
    ${CLAUDE_PROJECT_DIR}(런타임 var)·상대경로·'~' 홈 경로는 통과."""
    neutralized = value.replace(_PROJECT_DIR_VAR, _PROJECT_DIR_SENTINEL)
    hits = []
    if _POSIX_ABS.search(neutralized):
        hits.append(f"POSIX-abs {value!r}")
    if _DRIVE_ABS.search(neutralized):
        hits.append(f"drive-abs {value!r}")
    return hits


def portability_failures(settings_text: str) -> list[str]:
    """settings.json 텍스트의 portable-by-construction 위반 목록(빈 리스트=통과).

    (a) 치환 토큰 잔존, (b) 운영 문자열값의 머신-특정 절대경로. 유효 JSON 전제 — 호출부가
    json.loads 로 invalid-escape(Windows 백슬래시)를 먼저 가드한다.
    """
    failures = []
    tokens = SUBST_TOKEN.findall(settings_text)
    if tokens:
        failures.append(f"subst-token {sorted(set(tokens))}")
    data = json.loads(settings_text)
    for s in operational_strings(data):
        failures.extend(absolute_path_hits(s))
    return failures
