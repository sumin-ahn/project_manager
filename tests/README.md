# tests/ — 엔진 도구 단위테스트

> 모노레포 루트의 **canonical 엔진**(`.project_manager/tools/*.py`)을 직접 테스트한다.
> 그동안 엔진 도구가 upstream 무테스트로 출하되던 공백을 닫는 자리.

## 실행

```bash
python3 -m pytest tests/ -q
```

## 범위

- `test_engine_smoke.py` — 엔진 도구 import + 핵심 함수 존재 스모크 (지금).
- (후속) 다운스트림 인스턴스 `test_external_review.py` 등 일반화 포팅 → board.py·ticket_finish·external_review 단위테스트.

## 비고

- 테스트 대상은 **루트 `.project_manager/tools/`**(canonical). 템플릿(`templates/*/`)의 엔진은
  루트에서 `pm_update` 로 동기화된 사본이므로 루트만 테스트하면 충분하다.
- 의존: `pytest`. (도구 자체는 stdlib + pyyaml.)
