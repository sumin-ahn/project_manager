"""엔진 구현 패키지 (층: domain·infra·app·cli)."""


# 엔진 런타임 Python 하한의 단일 진실. pm_import 가 stdlib `tomllib` 를 직접 사용하므로
# Python 3.11 이 지배 제약이다(PEP 585 표기 자체의 3.9 하한보다 높음). board 탐지와
# Python 외부 파사드의 미러 리터럴은 이 값을 테스트로 대조해 skew 를 차단한다.
MIN_PYTHON = (3, 11)
