"""위임 명령의 fail-loud 오류 형식 (층: domain).

설정 해소·검증·경로 봉쇄·리뷰 양식 판정이 함께 쓰는 기반 예외를 소유한다. 진입점이 종료 코드 1로 바꾼다.
"""
from __future__ import annotations



class DelegateError(Exception):
    """config 해소·검증·containment·재앵커 등의 fail-loud 오류 (main 이 rc=1 로 변환)."""



class ClusterRoundBudgetExceeded(DelegateError):
    """고정 수열 밖 라운드 요청. fix 뒤에는 사람 라운드를 다시 열지 않는다."""


class ReviewerTargetError(RuntimeError):
    """추가 리뷰어 대상 설정이 원자 tuple 계약을 어김 — 호출 전에 중단해야 함."""
