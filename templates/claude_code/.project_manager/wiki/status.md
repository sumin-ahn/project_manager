---
title: Current Status
created: {{DATE}}
updated: {{DATE}}
type: status
---

# 현재 진행 상태

> **현재 상태 단일 진실 (활성 작업)** — *진행 중* 모듈의 **상태·비고**는 여기를, 완성·안정 모듈 상세는 [`status_done.md`](status_done.md) 를 본다.
> status.md = **judgment-only** (ADR-0023): 모듈 상태·비고(architect 가 코드 대조로 유지·PM 점검) + 외부 의존성. **derivable 숫자(테스트 수·합계)는 저장하지 않는다** — 그건 기계가 안다(아래).

**테스트 수 단일 진실 = `{{PY}} .project_manager/tools/board.py regression`(또는 `{{TEST_CMD}}`) 실측** · history 는 `log/current.md` entry. (status.md 에 숫자를 *저장*하면 현재형 거짓 주장이 돼 drift — ADR-0023.)

**개발 작업 보드:** ticket 발행 현황의 단일 진실은 `board.py list` (tickets/ 직접 읽음). `board.md` 는 파생 대시보드(git-untracked). 워크플로는 [`tickets/README.md`](tickets/README.md).

## 범례
- ✅ 완성 + 테스트 통과
- 🟡 부분 구현 (스텁 / TODO 있음)
- ⬜ 미구현
- 🔒 외부 의존 대기 (키 발급 등)

---

## 활성 모듈 매트릭스 (🟡 부분 / ⬜ 미구현 / 🔒 외부 대기)

> **멀티-PM:** `area` 열 = 그 모듈을 소유한 영역 prefix(예 `PAY`). 각 영역 PM 은 *자기 행만* 편집 →
> 서로 다른 hunk 라 git 이 대개 auto-merge. status.md 가 **공유 프로젝트 브리프**다. 단일 PM 이면 area 열은 비워도 됨.

<!-- 진행 중·미구현·외부 대기 모듈만 여기. ✅ 완성+안정 모듈은 status_done.md 로 옮긴다(wave/handoff 시점).
     상태·비고 = architect 가 코드 대조로 유지·PM 점검(ADR-0022/0023). 비고는 1줄 — 전체 이력은 log + done ticket.
     테스트 *수*는 적지 않는다(derivable·pytest 실측). ⚠️ ✅ 행 누적 시 board.py lint 가 status_done.md archive 권고. -->

| area | 모듈 | 파일 | 상태 | 비고 |
|:-:|---|---|:-:|---|
| (예시) PAY | 핵심 모듈 | `core.py` | ⬜ | 첫 ticket 으로 구현 예정 |

---

## 외부 의존성

<!-- TODO: 외부 API 키, 발급 대기 자원, 환경 의존성을 여기 기록. -->

| 항목 | 상태 | 비고 |
|---|:-:|---|
| (예시) 외부 API 키 | 🔒 | 발급 대기 |
