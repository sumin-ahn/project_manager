# hooks/ — 인스턴스 lint 훅 (ADR-0003)

> 이 프로젝트 **고유**의 lint 검사를 엔진 `board.py` 를 안 건드리고 더하는 자리.
> (인스턴스 소유 — `engine.manifest` 밖이라 `pm_update` 가 덮어쓰지 않는다.)

## 왜 여기인가

프레임워크 **공통** 검사(의존성 그래프·thin ticket·dangling wikilink 등)는 엔진 `board.py` 에
내장돼 있다. 프로젝트마다 다른 검사(도메인 규약·고유 네이밍·프로젝트 화이트리스트 등)를 더하려고
엔진 파일을 직접 고치면, 엔진이 업데이트될 때마다 수동 재적용해야 한다(= fork 부담).

대신 여기에 훅 파일을 떨구면 `board.py lint` 가 자동으로 발견·실행한다 — 엔진은 그대로 synced 유지.
(문서 overlay 의 코드판. ADR-0003.)

## 계약

- 파일명: `lint_*.py` (예: `lint_naming.py`, `lint_spec_refs.py`).
- 모듈은 `check() -> list[str]` 를 노출한다 — 각 문자열이 이슈 한 건의 설명.
- 빈 리스트 = 통과. 반환된 각 항목은 `board.py lint` 출력에 `[lint_<name>] <설명>` 으로 보고되고,
  하나라도 있으면 lint exit code 가 1.
- **fail-soft:** 한 훅의 로드/실행 실패나 `check()` 미정의는 stderr 경고로 보고하고 **나머지는 계속**한다
  (부분 실패가 lint 전체를 막지 않음).

## 예시

```python
# .project_manager/hooks/lint_naming.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def check() -> list[str]:
    issues: list[str] = []
    # ... 이 프로젝트 고유 규약 검사 ...
    return issues
```

## 비고

- `import` 자유 — 단, 실행이 빠르고 read-only 여야 한다(lint 는 자주 돈다).
- 무거운/네트워크 검사는 여기 말고 별도 CI 단계로.
