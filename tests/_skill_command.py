"""canonical skill → opencode flat command 사본 정합의 공용 판정 seam (T-0674·T-0708).

**판정 층**: 양쪽을 `read_bytes()` 로 읽고 개행 표기만 LF로 정규화한 **내용 동일성**이다. 체크아웃
표기(`core.autocrlf`)는 채택자 설정 소관이라 같은 내용이 표기만 달라 red가 되면 안 되고, 내용이 한
글자라도 다르면 red다. 기대값을 `read_text()`(universal newline)로, 실측값을 `read_bytes()`(원본
표기 보존)로 만들어 비교하던 층 혼합이 CRLF 체크아웃에서 상시 drift를 만들었다(T-0708).

파일을 읽는 지점을 여기 한 곳으로 모아 소비자가 층을 다시 섞을 수 없게 한다.
"""
from __future__ import annotations

from pathlib import Path

from _textio import normalize_newline_bytes

# 스킬 원문의 상세 문서 링크(스킬 디렉터리 기준 상대) — command 사본은 평탄 좌표로만 rewrite된다.
DETAIL_LINK = "(references/operational-details.md)"


def flat_command_link(name: str) -> str:
    """평탄 command 파일 위치에서 본 같은 상세 문서 링크."""
    return f"(../../.claude/skills/{name}/references/operational-details.md)"


def normalized_bytes(path: Path) -> bytes:
    """파일을 판정 층(LF 정규화 bytes)으로 읽는다 — 기대·실측 양쪽의 유일한 읽기 지점."""
    return normalize_newline_bytes(path.read_bytes())


def expected_command_bytes(skill: Path, name: str) -> bytes:
    """스킬 원문을 command 사본의 기대 내용으로 렌더한다 (LF 정규화 bytes)."""
    return normalized_bytes(skill).replace(
        DETAIL_LINK.encode("utf-8"), flat_command_link(name).encode("utf-8")
    )


def command_matches_skill(skill: Path, name: str, command: Path) -> bool:
    """사본이 canonical 스킬의 기계 렌더와 내용 동일한가 (개행 표기 무관)."""
    return expected_command_bytes(skill, name) == normalized_bytes(command)
