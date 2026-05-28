# raw/ — Immutable Time Snapshots

이 디렉토리는 **시간 위에 고정된 산출물**을 보존한다. 한번 들어오면 *절대
수정하지 않는다* — 후속 사실은 *새 파일* 로 추가한다.

## 무엇이 들어오는가

전형적인 하위 디렉토리 (프로젝트에 해당하는 것만 만들면 된다):

| 하위 디렉토리 | 의미 | 예시 파일명 |
|---|---|---|
| `plans/` | plan_v1 → plan_v2 → … 누적되는 전체 계획 문서 | `plan_v1.md`, `plan_v2.md`, `plan_v5_review_A.md` |
| `evaluations/` | 모델 평가·외부 코드 리뷰·사용자 피드백 정리 | `model_eval_2026-05.md`, `external_review_2026-06.md` |
| `benchmarks/` | 실측 결과 (latency·throughput·정확도 등) | `latency_2026-05-19.md`, `bench_GPU_2026-06.md` |

프로젝트가 plan 을 안 쓰면 `plans/` 를 안 만든다. 평가가 없으면 `evaluations/`
를 안 만든다. 빈 디렉토리를 미리 만들 필요는 없다.

## 왜 immutable 인가

- **수정하면 시간이 사라진다** — "plan_v2 가 당시 어떻게 생겼는가" 의 답이 늘
  필요하다 (왜 그때 그렇게 결정했나·평가가 어떻게 바뀌었나 등).
- **수정하면 인용이 깨진다** — ADR·log entry·ticket 본문이 raw 의 특정 절을
  인용한다. 후속 사실은 *새 파일* 에 적어 인용 사슬을 보존한다.

## 그럼 갱신은 어떻게

- 같은 plan 의 새 버전이면: `plan_v2.md`. v1 은 그대로 둔다.
- plan v5 에 대한 외부 리뷰면: `plans/reviews/plan_v5_review_A.md`.
- 같은 벤치마크의 재측정이면: `benchmarks/latency_2026-06-15.md`. 5월 측정은 그대로.

## 어디서 무엇을 인용하는가

- ADR / spec / pm_role 등 *current* 문서는 raw 를 **참고로 인용**할 수 있다 —
  "plan_v3 §4 의 trade-off 가 본 결정의 출발점" 같은 식.
- *current 정의는 current 문서가 단일 진실* — raw 는 출처·증거이지 정의가 아니다.

## 규칙 요약

- ✅ 새 파일 추가
- ✅ 다른 문서에서 link 인용
- ❌ 기존 파일 수정 (오타 수정도 — 사람 손이 닿으면 같은 시간 스냅샷이 아니게 된다)
- ❌ 기존 파일 삭제

심각한 잘못 (오해 소지·민감 정보 누출 등) 이 있으면 별도 ADR 로 처리하고 *왜
삭제했는가* 를 명시한다.
