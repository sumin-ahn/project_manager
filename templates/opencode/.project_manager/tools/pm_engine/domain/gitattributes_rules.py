"""`.gitattributes` 판독 규칙 (층: domain).

패턴 일치(부정·디렉터리 접미 포함)와 경로별 `merge` 속성 해소, areas.md `merge=union` 선언 판정을 소유한다.
"""
from __future__ import annotations

import re


# ── areas.md union merge 배포 ───────────────────
# areas.md 의 동시 등록 안전(merge 시 **양쪽 행을 모두 보존**)은 git 내장 `merge=union` 드라이버에
# 기댄다. 그 선언은 **areas.md 를 담은 git** 의 `.gitattributes` 에 있어야 유효하다:
#   - inline(legacy) : areas.md = `<REPO>/.project_manager/areas.md` → 루트 `.gitattributes` 의
#     `.project_manager/areas.md merge=union` 이 그대로 유효(engine.manifest 로 채택자에도 배포).
#   - board 분리     : areas.md 는 board git(submodule) *안* 이라 루트 선언이 **다른 git** 이라 닿지
#     않는다 — `git -C .project_manager/board check-attr merge -- areas.md` = unspecified.
#     그래서 board git 자체에 `.gitattributes` 를 둔다: 신규는 pm_import seed, 기존 board 는
#     `_ensure_board_gitattributes` backfill. 두 형상 다 union 이 성립한다(루트 선언은 유지).
# 판정은 전부 **파일 내용**으로 한다 — 런타임 `git check-attr` 호출을 늘리지 않는다(비용·이식성).
_AREAS_UNION_DRIVER = "union"

# 형상별 areas.md 경로 표기(그 git 의 `.gitattributes` 기준 상대). 선언 줄의 패턴을 이 표기들에
# 맞춰본다 — git slash anchoring + `*`·`**` 의미의 보수적 matcher를 공유한다.
_BOARD_AREAS_ATTR_TARGETS: tuple[str, ...] = ("areas.md", "/areas.md")
_INLINE_AREAS_ATTR_TARGETS: tuple[str, ...] = (
    ".project_manager/areas.md", "/.project_manager/areas.md")


def _gitattributes_positive_pattern_matches(
        pattern: str, targets: tuple[str, ...]) -> bool:
    """부정 없는 git-attributes 패턴을 확실히 해석할 수 있을 때만 match한다."""
    anchored = pattern.startswith("/")
    pat = pattern[1:] if anchored else pattern
    # 디렉토리 패턴·escape·문자 클래스는 이 보수적 부분집합 밖이다. 거짓 정상보다 중복
    # backfill이 안전하므로 판정 불능은 False다.
    if not pat or pat.endswith("/") or any(char in pat for char in "\\[]"):
        return False
    has_slash = anchored or "/" in pat
    regex: list[str] = []
    index = 0
    while index < len(pat):
        char = pat[index]
        if char == "*" and index + 1 < len(pat) and pat[index + 1] == "*":
            boundary_before = index == 0 or pat[index - 1] == "/"
            after = index + 2
            if not boundary_before or (after < len(pat) and pat[after] != "/"):
                return False
            if after < len(pat):
                regex.append("(?:.*/)?")
                index = after + 1
            else:
                regex.append(".*")
                index = after
        elif char == "*":
            regex.append("[^/]*")
            index += 1
        elif char == "?":
            regex.append("[^/]")
            index += 1
        else:
            regex.append(re.escape(char))
            index += 1
    compiled = re.compile("".join(regex) + r"\Z")
    for target in targets:
        candidate = target.lstrip("/")
        subject = candidate if has_slash else candidate.rsplit("/", 1)[-1]
        if compiled.fullmatch(subject):
            return True
    return False


def _gitattributes_pattern_matches(pattern: str, targets: tuple[str, ...]) -> bool:
    """`.gitattributes` 패턴이 target을 덮는가 (git slash/`*`/`**` 의미의 보수적 부분집합).

    slash 없는 패턴은 어느 깊이의 basename에도 적용된다. slash가 있으면 파일 기준으로 앵커되고
    단일 ``*``는 slash를 넘지 않으며 ``**``만 디렉토리 경계를 넘는다. 선행 ``!`` 부정과
    escape·문자 클래스처럼 이 함수가 확실히 판정하지 않는 표면은 False로 둔다.
    """
    if pattern.startswith("!"):
        return False
    return _gitattributes_positive_pattern_matches(pattern, targets)


def _gitattributes_merge_attr(text: str, targets: tuple[str, ...]) -> str | None:
    """`.gitattributes` 본문에서 areas.md 경로에 걸린 **마지막** merge 속성 값 (없으면 None).

    git 은 뒤 줄이 앞 줄을 덮으므로(last-match-wins) 전 줄을 훑어 마지막 선언을 취한다 —
    `areas.md merge=union` 뒤에 `areas.md -merge` 가 오면 union 은 취소된 것으로 읽는다.
    빈 줄과 `#` 로 *시작하는* 주석 줄은 건너뛴다. `#` 은 줄 끝 주석이 **아니므로**(git 은
    `areas.md merge=union # 설명` 을 잘못된 속성 이름으로 보고 줄 전체를 무시한다·실측)
    필드에 `#` 이 섞인 줄은 git 과 동일하게 **무효** 취급한다 — 그렇지 않으면 실제로는
    unspecified 인 파일을 "선언됨" 으로 읽어 union 상실이 조용히 굳는다(거짓 정상).

    패턴 매칭은 git의 slash anchoring과 `*`·`**` 의미를 처리한다
    (`_gitattributes_pattern_matches`) — 리터럴 경로, `*.md`·`tickets/**/*.md` 형태가 대상이다.
    그래서 union 선언 **뒤에 오는 glob unset**
    (`areas.md merge=union` 다음 줄 `*.md -merge` → git 은 unset·실측)도 잡는다.

    escape·문자 클래스처럼 보수적 부분집합 밖의 패턴은 match로 인정하지 않는다. 그 결과는
    advisory+중복 backfill인 거짓 경보 방향이며, LF/union 보호가 빠지는 거짓 정상은 만들지 않는다.
    반환: `"union"`(우리 선언) · 다른 드라이버명 · `""`(unset/`-merge`) · None(선언 없음).
    """
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if any("#" in field for field in fields):
            continue  # git 이 줄 전체를 무시하는 형태 — 우리도 무효로 본다(거짓 정상 방지).
        pattern = fields[0]
        if pattern.startswith("!"):
            found = None
            continue
        if not _gitattributes_pattern_matches(pattern, targets):
            continue
        for attr in fields[1:]:
            if attr.startswith("merge="):
                found = attr[len("merge="):]
            elif attr in ("merge", "-merge", "!merge"):
                found = ""  # 드라이버 미지정 set / unset / unspecified — union 아님.
    return found


def _areas_union_declared(text: str, targets: tuple[str, ...]) -> bool:
    """이 `.gitattributes` 본문이 areas.md 에 union merge 를 부여하는가 (내용 판정)."""
    return _gitattributes_merge_attr(text, targets) == _AREAS_UNION_DRIVER
