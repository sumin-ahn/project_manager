---
name: pm-handoff
description: "PM 세션 종료 핸드오프 7단계 자동화 — log entry skeleton append + pm_role.md sliding window 정리 + 인계 프롬프트 stdout + 회귀 측정 + git status. backbone CLI .project_manager/tools/pm_handoff.py thin wrapper. Triggers: '핸드오프', '인계', 'PM 세션 종료', 'pm-handoff'."
---

# /pm-handoff — PM 세션 종료 핸드오프 자동화

> {{PROJECT_NAME}} PM 세션의 핸드오프 7단계 (pm_role.md §"핸드오프 절차") 를
> 한 trigger 로 처리한다. PM 손은 *log.md 본문 서술 + 인계 프롬프트 §핵심
> 인계 사항 채움 + git commit* 만 남는다. backbone =
> `.project_manager/tools/pm_handoff.py`.

## 사용 시점

다음 중 하나면 호출:
- 사용자 명시 종료 신호 (*"세션 종료"·"인계해"*)
- PM 컨텍스트 < 10% 신호 (자기 보고)
- wave 마지막 commit 후 자연 종료 시점

## 실행

```bash
{{PY}} .project_manager/tools/pm_handoff.py \
  --session-num <N차> \
  --wave-summary "<wave 1~3 한 줄 요약>"
```

옵션:
- `--dry-run` — log.md / pm_role.md 변경 미적용·stdout 미리보기만.
- `--no-pytest` — 회귀 측정 skip (직전 wave 종결 commit 의 숫자 신뢰 시·**비권장**).

## CLI 자동 처리 단계

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단·핸드오프 불가 (baseline fix 후 재시도).
2. **log.md handoff entry skeleton append** — `## [YYYY-MM-DD] handoff | PM N차 → 다음 PM 세션` 형식. 본문 = `<PM 손 채움>`.
3. **pm_role.md sliding window 정리** — §세션 식별 표에 N차 entry 추가 + 가장 오래된 entry 제거. 자세히 → pm_role.md §핸드오프 절차 #4.
4. **pm_role.md 길이 검증** — `wc -l` 700 라인 초과 시 warning (과거 누적 정리 누락 신호).
5. **인계 프롬프트 stdout 출력** — pm_role.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)" 의 고정부 채움. `<핵심 인계 사항>` 절은 PM 손.
6. **git status dump** — `git status -s` 출력 + 변경 파일 카운트.
7. **잔여 PM 수동 작업 checklist 출력**.

## 잔여 PM 손작업 (CLI 후)

1. **log.md handoff entry 본문 서술** — skeleton 의 `<...>` placeholder 를 실제 내용으로 채움 (직전 PM 세션 핵심 산출물·메타 학습 누적·다음 PM 우선순위).
2. **인계 프롬프트 §핵심 인계 사항 절 채움** — board 상태·진행 중 작업·다음 권장 작업·incident·외부 대기 항목 5~10 불릿.
3. **git commit** — 핸드오프 commit message 형식: `PM 세션(N차) 핸드오프 — pm_role.md sliding window + log.md handoff entry + PM (N+1)차 인계`. trailer `Co-Authored-By: Claude`.
4. **마지막 응답에 인계 프롬프트 코드블록 출력** — 사용자가 복사해 새 PM 세션에 붙여넣음.

## 결정

- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. 핸드오프 후 신규 PM 이 broken state 로 시작 회피.
- **sliding window 정리는 자동** — 표 편집 race 회피 위해 CLI 가 직렬 처리.

## 참고

- `.project_manager/tools/pm_handoff.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 핸드오프 절차 7단계 단일 진실
