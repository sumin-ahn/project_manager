"""위임 명령의 fail-loud 오류 형식 (층: domain).

설정 해소·검증·경로 봉쇄·리뷰 양식 판정이 함께 쓰는 기반 예외를 소유한다. 진입점이 종료 코드 1로 바꾼다.
"""
from __future__ import annotations


class DelegateError(Exception):
    """config 해소·검증·containment·재앵커 등의 fail-loud 오류 (main 이 rc=1 로 변환)."""
