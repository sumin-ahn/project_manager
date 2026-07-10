# raw/ — Immutable Time Snapshots

이 디렉토리는 **시간 위에 고정된 산출물**을 보존한다. 한번 들어오면 *절대
수정하지 않는다* — 후속 사실은 *새 파일* 로 추가한다.

## 무엇이 들어오는가

전형적인 하위 디렉토리 (프로젝트에 해당하는 것만 만들면 된다):

| 하위 디렉토리 | 의미 | 예시 파일명 |
|---|---|---|
| `spikes/` | 대화형 설계 spike 산출 (옵션 비교 + ADR/ticket DRAFT). `/spike-new` 스킬이 박제 | `web-auth-redesign-2026-06-03.md`, `_template.md` |
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

## 예외 — spike 는 *seal* 시점에 immutable 이 바인딩된다 (ADR-0010)

위 근거는 **태어날 때 봉인된**(born-sealed) 산출 — `plans/`·`evaluations/`·
`benchmarks/` — 에 정확히 들어맞는다. 이들은 *측정·작성이 끝난 한 시점*의 스냅샷이다.
**그러나 `spikes/` 는 여러 턴에 걸쳐 저작되는 live 과정**(born-draft)이라, immutability
가 *생성* 시점이 아니라 ***seal* 시점**에 바인딩된다:

- `status: draft` — 편집 가능·**세션 경계 무관**. 핸드오프해도 다음 세션이 *같은 파일*을
  이어 쓴다 (새 날짜 파일이 아니다). 설계가 여러 턴에 걸쳐 누적되는 동안의 정상 상태.
- `status: sealed (<date>)` — immutable·인용 가능. 설계 절 전부 합의 + §4·§5 완비 +
  **사용자 사인오프** 시에만 봉인한다(혼자 봉인 금지). 봉인된 순간부터 위의 두 근거(시간
  스냅샷·인용 사슬)가 그대로 적용된다.
- **안전 기본값**: frontmatter `status:` 가 `draft` 로 시작하지 *않으면*(또는 없으면)
  **immutable.** 기본이 immutable 이라 누락/오타가 데이터를 안 깨뜨리고, 기존 sealed
  spike 마이그레이션은 0이다. 기존 `accepted (<date>)` 는 sealed 별칭(후방호환·=immutable).
- **발행 입력은 sealed spike 만** — ADR/ticket 은 sealed 된 spike 에서만 발행한다. draft 는
  권위 인용 대상이 아니므로(PM 수렴 입력이 아니므로) draft 편집이 인용 사슬을 안 깨뜨린다.
- **sealed 후 vN 개정·born-sealed(plan/eval/bench) 는 현행 그대로** — 이 예외는 *spike 에만*
  적용된다. sealed spike 의 개정은 여전히 새 날짜 파일(vN), born-sealed 는 태어날 때부터 immutable.

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
- ✅ **`spikes/` 예외**: `status: draft` 동안은 *같은 파일* 편집·세션무관 resume 가능 — `sealed (<date>)` 부터 immutable (ADR-0010)
- ❌ 기존 파일 수정 (오타 수정도 — 사람 손이 닿으면 같은 시간 스냅샷이 아니게 된다). *non-draft 는 일체 immutable — 안전 기본값.*
- ❌ 기존 파일 삭제

심각한 잘못 (오해 소지·민감 정보 누출 등) 이 있으면 별도 ADR 로 처리하고 *왜
삭제했는가* 를 명시한다.
