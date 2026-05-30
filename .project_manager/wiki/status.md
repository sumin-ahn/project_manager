---
title: Current Status
created: {{DATE}}
updated: {{DATE}}
type: status
---

# 현재 진행 상태

> **단일 진실 소스 (활성 작업)** — *진행 중* 모듈 상태는 여기를, 완성·안정 모듈 상세는 [`status_done.md`](status_done.md) 를 본다. 새 모듈/테스트 추가 시 이 파일을 먼저 갱신.

<!-- ⚠️ 아래 "전체 테스트" 라인은 스칼라 앵커다 (ticket_finish.py 가 편집). incident/wave 서술을
     이 라인에 붙이지 않는다 — narrative 는 log/current.md 의 entry 로. board.py lint 가 길이 초과 시 경고. -->
**전체 테스트: 0 / 0 통과** (통합 0개는 외부 의존으로 기본 skip)

**완성 모듈:** 0개 — 상세는 [`status_done.md`](status_done.md) (✅ 안정 모듈은 거기로 이동, 이 파일은 활성만 유지).

**개발 작업 보드:** ticket 발행 현황의 단일 진실은 `board.py list` (tickets/ 직접 읽음). `board.md` 는 파생 대시보드(git-untracked). 멀티 세션 병렬 작업 가능. 워크플로는 [`tickets/README.md`](tickets/README.md).

## 범례
- ✅ 완성 + 테스트 통과
- 🟡 부분 구현 (스텁 / TODO 있음)
- ⬜ 미구현
- 🔒 외부 의존 대기 (키 발급 등)

---

## 활성 모듈 매트릭스 (🟡 부분 / ⬜ 미구현 / 🔒 외부 대기)

> **멀티-PM:** `area` 열 = 그 모듈을 소유한 영역 prefix(예 `PAY`). 각 영역 PM 은 *자기 행만*
> 편집 → 서로 다른 hunk 라 git 이 대개 auto-merge. status.md 가 **공유 프로젝트 브리프**다
> (pm_state 가 비운 프로젝트-전역 상태는 여기·board·ADR 로). 단일 PM 이면 area 열은 비워도 됨.

<!-- 진행 중·미구현·외부 대기 모듈만 여기. ✅ 완성+안정 모듈은 status_done.md 로 옮긴다
     (wave-finish/handoff 시점). Layer/그룹별로 섹션을 나누고 섹션 끝마다 소계 행을 두면
     ticket_finish.py 의 --section 으로 자동 갱신된다. 비고는 1줄 — 전체 이력은 log + done ticket.
     ⚠️ ✅ 행이 누적되면 board.py lint 가 status_done.md archive 를 권고한다. -->

| area | 모듈 | 파일 | 테스트 | 상태 | 비고 |
|:-:|---|---|---:|:-:|---|
| (예시) PAY | 핵심 모듈 | `core.py` | 0 | ⬜ | 첫 ticket 으로 구현 예정 |

---

## 테스트 합계표

> `.project_manager/tools/ticket_finish.py` 가 ticket 완료 시 아래 스칼라를 자동 갱신한다 —
> 이 표의 라인 형식(`| 섹션명 | N |`, `| **합계** | **N** |`)을 바꾸면
> `ticket_finish.py` 의 `_RE_*` 정규식도 같이 바꿔야 한다.

| 섹션 | 테스트 수 |
|---|---:|
| 핵심 모듈 | 0 |
| **합계** | **0** |

회귀 실측 `pytest tests/ -q` = **0 / 0** 와 일치.
<!-- 위 라인은 ticket_finish.py 의 회귀 앵커. pytest 외 러너를 쓰면
     이 라인과 ticket_finish.py 의 _RE_REGRESSION 을 함께 교체. -->

---

## 외부 의존성

<!-- TODO: 외부 API 키, 발급 대기 자원, 환경 의존성을 여기 기록. -->

| 항목 | 상태 | 비고 |
|---|:-:|---|
| (예시) 외부 API 키 | 🔒 | 발급 대기 |
