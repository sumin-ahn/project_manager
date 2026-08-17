"""훅 커맨드 문자열의 **인용 표면** 추출 헬퍼 (T-0720).

PowerShell 5.x 는 native 명령 인자의 큰따옴표를 보존하지 않는다 (Windows 11 실측:
``py -3.12 -c 'print("quoted")'`` → ``NameError: name 'quoted' is not defined``). 따라서
"native 명령에 넘기는 인자"와 "셸 안에서만 쓰는 문자열"을 갈라 봐야 한다 — 후자
(``$fallback`` 리터럴처럼 ``Write-Output`` 으로만 나가는 값)는 이 결함의 대상이 아니다.

여기 두 추출기는 그 경계를 기계적으로 긋는다. 판정은 호출 측 테스트가 한다.
"""
from __future__ import annotations


_POWERSHELL_INVOCATION_START = "& $"
_POWERSHELL_STATEMENT_END = ";}"


def powershell_native_arguments(command: str) -> list[str]:
    """PowerShell 커맨드에서 native 호출(``& $py …``) 한 건씩의 인자 텍스트를 뽑는다.

    작은따옴표 안의 ``;``/``}`` 는 문장 끝이 아니다(PowerShell 리터럴). 인용 상태를
    추적해 실제 문장 경계에서만 끊는다.
    """
    segments: list[str] = []
    index = 0
    length = len(command)
    while index < length:
        if command[index] == "'":
            index = _skip_single_quoted(command, index)
            continue
        if command.startswith(_POWERSHELL_INVOCATION_START, index):
            start = index
            index += len(_POWERSHELL_INVOCATION_START)
            while index < length:
                if command[index] == "'":
                    index = _skip_single_quoted(command, index)
                    continue
                if command[index] in _POWERSHELL_STATEMENT_END:
                    break
                index += 1
            segments.append(command[start:index])
            continue
        index += 1
    return segments


def inline_script_payloads(command: str) -> list[str]:
    """``-c '<script>'`` 로 넘기는 인라인 스크립트 본문을 뽑는다 (POSIX·PowerShell 공통 표기)."""
    payloads: list[str] = []
    marker = "-c '"
    index = command.find(marker)
    while index != -1:
        start = index + len(marker)
        end = _skip_single_quoted(command, start - 1)
        payloads.append(command[start:end - 1])
        index = command.find(marker, end)
    return payloads


def _skip_single_quoted(text: str, opening: int) -> int:
    """여는 따옴표 위치에서 시작해 닫는 따옴표 **다음** 인덱스를 돌려준다 (``''`` 는 이스케이프)."""
    index = opening + 1
    length = len(text)
    while index < length:
        if text[index] == "'":
            if index + 1 < length and text[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return length
