"""엔진 구현 패키지 (층: domain·infra·app·cli)."""


# 엔진 런타임 Python 하한의 단일 진실. pm_import 가 stdlib `tomllib` 를 직접 사용하므로
# Python 3.11 이 지배 제약이다(PEP 585 표기 자체의 3.9 하한보다 높음). board 탐지와
# Python 외부 파사드의 미러 리터럴은 이 값을 테스트로 대조해 skew 를 차단한다.
MIN_PYTHON = (3, 11)

# 엔진 세대 표기의 단일 진실. 관리자 버전·status JSON·설치 하네스 별칭 기한·릴리즈 일치 검사가
# 이 값을 읽는다. 릴리즈 때 이 한 줄을 고친다.
ENGINE_REV = "v1.8.2"
