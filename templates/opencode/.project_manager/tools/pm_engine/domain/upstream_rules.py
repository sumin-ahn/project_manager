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


def validate_upstream_value(value: str) -> tuple[bool, str]:
    """upstream 값의 *순수* 안전 검증 (네트워크 0·도달성은 별도). (ok, reason).

    git 을 호출하기 *전* 입구 가드 — fail-closed(나쁜 값은 거부, silently 기록 금지
    동형). 검사:
      - 빈/공백 거부.
      - leading-dash 거부 — `--upload-pack=...` 류 옵션 오인(argv 첫 위치라도 안전).
      - URL 이면 scheme allowlist(https/ssh/file·git://·http:// 거부) 강제 + credential-in-URL
        (`user:pass@`) 거부(자격증명 누출·SSRF) + **authority(host[:port]) leading-dash 거부**
        — `ssh://-oProxyCommand=...`·`ssh://git@-oProxyCommand=...` 류 ssh 옵션 주입 차단
        (codex MF3·defense-in-depth·git 자체 방어에만 의존하지 않음). scp-style(`user@host:
        path`)의 user 부는 허용(비밀 아님)이나 `user:pass@host` 의 password·transport helper
        (`ext::cmd`)는 거부.
    도달성(ls-remote)·경로 존재는 호출부(검증 후속)가 본다 — 여기선 *형태 안전*만.
    """
    if not value or not value.strip():
        return False, "upstream 값이 비어 있다."
    if value.startswith("-"):
        return False, f"upstream 값이 '-' 로 시작한다(옵션 오인·argv 안전): {value!r}"
    # scheme-form(`X://...`)인데 allowlist 밖이면 명시 거부 — http(평문)·git://(비인증 평문·MITM)·
    # ftp·ext::cmd(임의명령)·임의 transport 차단(SSRF·중간자·원격 코드 실행). scp-style(콜론만·
    # `://` 없음)은 아래에서 분류.
    if "://" in value and not value.startswith(_UPSTREAM_URL_SCHEMES):
        scheme = value.split("://", 1)[0]
        return False, (
            f"upstream URL scheme {scheme!r} 비허용 — 허용: https/ssh/file "
            "(http·git:// 평문·ftp·ext::cmd 등 거부·SSRF/중간자/MITM 방지)."
        )
    kind = classify_upstream(value)
    if kind == "url":
        if value.startswith(_UPSTREAM_URL_SCHEMES) and not value.startswith("file://"):
            # scheme-form URL — authority(userinfo@host[:port]) 부를 분리해 검사.
            after_scheme = value.split("://", 1)[1] if "://" in value else value
            authority = after_scheme.split("/", 1)[0]
            userinfo = authority.split("@", 1)[0] if "@" in authority else ""
            host = authority.split("@", 1)[1] if "@" in authority else authority
            # credential-in-URL(`user:pass@`) 거부(자격증명 누출).
            if userinfo and ":" in userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·credential helper/SSH 키를 쓰라)."
                )
            # MF3: host(또는 userinfo)가 `-` 로 시작하면 ssh 옵션 주입(`-oProxyCommand=...`)으로
            # 해석될 수 있다 — defense-in-depth 로 거부(git 자체 방어에만 의존하지 않음).
            if host.startswith("-") or userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입·-oProxyCommand 류 차단): {value!r}"
                )
        elif not value.startswith("file://"):
            # scp-style(`user@host:path`) — git transport helper(`ext::cmd`·`fd::N` 등 double-
            # colon)는 임의명령 실행이라 거부(`://` 없이 `::` 가 있는 형태). 정상 scp 는 single
            # colon(`host:path`)뿐이다.
            if "::" in value:
                return False, (
                    f"upstream 값에 git transport helper(`::`)가 있다 — 거부 "
                    f"(ext::cmd 등 임의명령 실행 회피): {value!r}"
                )
            # scp authority 분리 (codex round-2 정정) — git scp 문법은 `[user@]host:path`.
            # **먼저 첫 `:` 로 lhs(`[user@]host`) ↔ path 를 나눈다.** path(첫 `:` 뒤)는 자유 —
            # `@`·`:` 포함 정상(`host:path@v1.git`·`host:sub/dir@ref`)이라 거기서 credential/
            # leading-dash 를 보면 false-reject 한다. authority 해석은 **lhs 안에서만** 한다.
            #
            scp_lhs = value.split(":", 1)[0]
            if "@" in scp_lhs:
                scp_userinfo, scp_host = scp_lhs.split("@", 1)
            else:
                scp_userinfo, scp_host = "", scp_lhs
            # `user:pass@host` 형태의 password 박힘 거부(자격증명 누출) — **lhs 의 userinfo 한정**.
            # scp 문법은 password 를 지원하지 않으므로(그건 scheme URL `https://user:pass@` 형식)
            # path 의 `:`·`@`(첫 `:` 뒤)는 자유다 — `host:path@with:colon`·`user:pass@host:path`
            # (git 은 host=`user`·path=`pass@host:path` 로 본다)는 정상 scp 로 통과한다(codex 알고리즘:
            # authority 해석은 lhs[첫 `:` 앞] 안에서만). 진짜 credential 박힘은 scheme-form 에서 거부.
            if ":" in scp_userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·SSH 키를 쓰라)."
                )
            # MF3: scp host/userinfo leading-dash 거부(ssh 옵션 주입 차단·defense-in-depth).
            if scp_host.startswith("-") or scp_userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입 차단): {value!r}"
                )
        return True, ""
    # 경로 — 형태 안전(leading-dash 는 위에서 이미 거름). 존재 검증은 호출부.
    return True, ""
