"""관리자 앱 패키지 — 등록된 PM 홈을 **부르는 쪽**(엔진은 각 프로젝트 사본이 소유한다).

경계 두 줄:
  - 관리자가 프로젝트 폴더에 직접 쓰는 파일은 0이다. 쓰기는 전부 그 사본의 명령이 한다.
  - 관리자가 프로젝트 폴더에서 직접 읽는 파일은 `local.conf` 하나이고 키는
    `runtime.py`(사본을 부를 인터프리터)·`upstream.path`(최신 태그 조회 대상) 둘뿐이다.
    현황·버전·보드는 전부 그 사본의 `pm-config status --json` 이다.

이 모듈은 그 경계에 필요한 공용 두 가지만 갖는다 — 번들 엔진 버전 표기와 실패 하나의 표현.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
# 관리자와 함께 배포되는 엔진 사본(제품 repo 의 canonical 트리). 프로젝트 사본의 엔진이 아니다 —
# 옛 세대 사본의 설정 로더(`pm_engine.infra.conf_store`)로 값을 읽으면 그 세대의 파싱 규칙에 끌려간다.
BUNDLED_TOOLS = REPO_ROOT / ".project_manager" / "tools"
# 번들 엔진 패키지(`pm_engine`)는 일반 import 로 읽는다.
if str(BUNDLED_TOOLS) not in sys.path:
    sys.path.insert(0, str(BUNDLED_TOOLS))
import pm_engine  # noqa: E402
from pm_engine.infra import console  # noqa: E402,F401  (console 은 진입점이 쓴다)

# 창은 화면의 호출을 스레드마다 따로 처리한다. 모듈 로드는 `sys.modules` 에 **먼저 넣고 실행**
# 하므로, 직렬화하지 않으면 다른 스레드가 반쯤 채워진 모듈을 집어 간다 — 실측(프로젝트 7개
# 동시 조회): `local_conf` 에 `load` 가 없다는 AttributeError 3건, 그리고 엔진이 지연 로드하는
# 형제도 반쯤 보였다. 그래서 **모듈 로드뿐 아니라 엔진 호출 자체**를 이 잠금으로 묶는다(호출은
# conf 파싱 한 번이라 값싸고, 느린 서브프로세스는 잠금 밖에서 돈다).
ENGINE_LOCK = threading.RLock()


class CallError(RuntimeError):
    """관리자가 바깥(프로젝트 사본·git·등록부 파일)을 부르다 실패한 지점 하나.

    `kind` 는 화면 계약(브리지 오류 dict)의 다섯 값이다. 실패를 `None`·빈 값으로 바꾸지 않고
    이 예외로 올린 뒤 브리지가 `as_error_dict()` 로 화면에 원문(stderr·rc)까지 넘긴다.
    """

    KINDS = ("old-generation", "spawn", "timeout", "not-json", "invalid-path")

    def __init__(self, kind: str, message: str, *, stderr: str = "", rc: int | None = None) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"브리지 오류 종류가 계약 밖이다: {kind!r} (허용: {self.KINDS})")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.stderr = stderr
        self.rc = rc

    def as_error_dict(self) -> dict:
        """화면이 `"error" in resp` 로 가르는 묶음 공통 형태."""
        return {"error": {"kind": self.kind, "message": self.message,
                          "stderr": self.stderr, "rc": self.rc}}


def engine_rev() -> str:
    """번들 엔진 패키지의 세대 값 — 관리자 버전 표기의 단일 진실(자기 버전 상수를 따로 두지 않는다)."""
    return pm_engine.ENGINE_REV
