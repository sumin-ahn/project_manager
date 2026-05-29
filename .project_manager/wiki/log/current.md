# Project Log

> 프로젝트 운영 작업의 시간순 기록. Append-only. 활성 로그는 이 파일(`log/current.md`).
> 오래된 entry 는 `pm_log.py archive` 로 `log/archive/` 에 봉인된다.
> 형식: `## [YYYY-MM-DD] action | subject`
> Actions: create, update, decide (ADR), ticket, spec, split, handoff, lint

## [{{DATE}}] create | Project wiki initialized

- Claude Project Framework 템플릿에서 부트스트랩.
- 구조 생성: README, status, board, log/, architecture, pm_role, pm_state + tickets/ decisions/ specs/ ideas/
- 다음: CLAUDE.md·status.md·architecture.md 의 placeholder 를 채우고 첫 ticket 발행.
