"""상류(upstream) 값의 URL·경로 분류 (층: domain).

값 모양만 본다(네트워크·파일 IO 없음). 갱신기·설치기·설정 도구·관리자가 같은 분류를 쓴다.
"""
from __future__ import annotations

import re


# ── upstream URL 안전 계약 + self-describing 분류 ──────────
# upstream 값은 git URL *또는* 로컬 경로다 — self-describing(모양으로 분기). git 을
# 호출하는 모든 경로(ls-remote·remote get-url·rev-parse)가 이 계약을 지킨다:
#   - argv-list(no shell·_real_git_runner 가 항상 list 전달) · leading-dash 거부(옵션 오인)
#   - protocol allowlist(https/ssh/file 명시) · credential-in-URL 거부(SSRF·자격증명 누출)
#   - scp-style(user@host:path)/Windows(C:\)/상대/모호 colon 분기로 URL↔경로 정확 판별
# 비대화 auth(GIT_TERMINAL_PROMPT=0)·timeout 은 git 호출 runner(_real_upstream_git_runner)가
# 강제한다. 이 검증 자체는 *순수 함수*(네트워크 0) — 도달성은 ls-remote 호출부가 따로 본다.

# URL scheme allowlist — https/ssh/file *만*. http(평문)·git://(비인증
# 평문·MITM 취약)·ftp·ext::<cmd>(임의명령)·임의 transport 는 전부 거부(SSRF·중간자·원격 코드
# 실행 회피). git:// 는 ssh 위 전송이 아니라 *비인증 평문*이라 allowlist 에서 뺀다(codex MF2).
_UPSTREAM_URL_SCHEMES = ("https://", "ssh://", "file://")

# scp-style URL(`user@host:path`·`host:path`) 판별 — colon 앞에 슬래시가 없고(경로 아님)
# Windows 드라이브(`C:`)가 아니어야 한다. git 의 scp 문법은 이 모양을 SSH 로 해석한다.
_SCP_LIKE_RE = re.compile(r"^[^/\\:]+@[^/\\:]+:.+$|^[A-Za-z][A-Za-z0-9_.-]*:.+$")

# Windows 드라이브 경로(`C:\...`·`C:/...`) — 단일 알파벳 + colon + 슬래시. scp-style 과
# 구분해 *경로*로 취급(콜론 모호성 해소). 단일문자 호스트의 scp 와 충돌하지 않게 슬래시 요구.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def classify_upstream(value: str) -> str:
    """upstream 값을 self-describing 으로 분류한다 — 'url' | 'path'.

    엔진의 소스 해소(URL→로컬 캐시 clone/fetch · 경로→그 checkout)와 스킬의 pull 판단이 이
    한 규칙을 공유한다(값 모양만 본다·네트워크 0). 판정 순서:
      1. 허용 scheme(`https://`·`ssh://`·`file://`) prefix → 'url'.
      2. Windows 드라이브(`C:\\`·`C:/`) → 'path'(콜론 모호성 우선 해소).
      3. scp-style(`user@host:path`·`host:path`) → 'url'(git SSH 문법).
      4. 그 외 → 'path'(상대/절대 로컬 경로).
    빈 문자열은 'path'(호출부 검증이 별도로 거른다 — 여기선 모양 분류만). scheme allowlist
    밖(git://·http:// 등)은 prefix 매칭이 안 돼 scp-style 분기로 빠질 수 있으나, 안전 거부는
    `validate_upstream_value`(`://` allowlist-밖 명시 reject)가 전담한다(분류 ≠ 허가).
    """
    if any(value.startswith(s) for s in _UPSTREAM_URL_SCHEMES):
        return "url"
    if _WINDOWS_DRIVE_RE.match(value):
        return "path"
    if _SCP_LIKE_RE.match(value):
        return "url"
    return "path"
