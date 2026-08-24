# /pm-wave-finish 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 종결 8단계 (고정 순서 · 관측 기반 멱등)

| # | 단계 | 하는 일 | 건너뛰기 관측 |
|---|---|---|---|
| 1 | 기계 확인 (확인 존재·accepted 잔여 0) | 처분 대기 게이트마다 확인 증거와 잔여를 판정 | 잔여 게이트 0 |
| 2 | 리뷰 게이트 처분 | `rounds resolve --cluster --pm-verified`(확인 커맨드는 엔진이 실행) | 처분할 게이트 없음 |
| 3 | 티켓별 완료 기록 | 회귀·log 스켈레톤·`board complete`·선언 경로 stage | 이미 done |
| 4 | 슬롯 커밋 | 티켓마다 그 티켓이 stage 한 경로를 **티켓 제목 문안**으로 커밋 | 커밋할 변경 없음 |
| 5 | 통합 브랜치로 재배치 | 묶음 브랜치를 통합 브랜치 위로 rebase(충돌은 abort 후 정지) | 통합 브랜치 미선언 |
| 6 | 통합 브랜치 머지 | 통합 브랜치에서 묶음 브랜치를 `--no-ff` 로 받음 | 이미 조상 · 브랜치 미선언 |
| 7 | 슬롯 반납 | 이 종결이 쓴 슬롯 lease 해제 | 슬롯 미해소 · 리스 없음 |
| 8 | board·포인터 커밋 | board 커밋 + PM 홈 서브모듈 포인터 커밋 | 포인터 미대상 |

- **관측이 기록보다 앞선다.** 장부의 단계 기록은 사람이 읽는 값이고, 건너뛰기 판정은 실제 상태
  관측이다. 기록과 실제가 어긋나면(중단·외부 원복) 기록을 믿는 재개는 없는 커밋 위에서 진행한다.
- **관측 실패는 정지다.** "관측했는데 대상이 없다"(커밋할 변경 없음·리스 없음)와 "관측 자체가
  실패했다"는 다르다. 뒤쪽을 무대상으로 접으면 근거 없는 종결 기록만 남는다.
- 발행이 만든 크기 1 장부는 통합·묶음 브랜치를 선언하지 않으므로 5·6단계가
  `통합 브랜치 미선언 — 무대상` 으로 건너뛴다(비차단). 브랜치까지 쓰려면
  `python3 .project_manager/tools/board.py cluster new <이름> --tickets <T-NNNN>` 으로 선언한다.

## 3단계(완료 기록)가 티켓마다 하는 일

1. **회귀 측정** — red면 즉시 중단하고 dev 재작업. board complete 도 차단한다.
2. **log/current.md skeleton append** — `## [YYYY-MM-DD] complete | T-NNNN — <title>`; 본문은 `<PM: 무엇을·왜>` placeholder.
3. **board complete** — `--tests-pass` 가드 + **DoD 기록 게이트** 후 claimed→done.
4. **git stage** — ticket frontmatter `touches` ∪ 이 실행이 실제 쓴 산출물만 `git add`:
   - `.project_manager/wiki/log/current.md`
   - legacy 형상(board 미분리·**출하 기본**)에서 옮긴 ticket의 옛/새 경로

ADR(`decisions/`)·domain 페이지·`architecture.md`·`status.md`는 다른 실행의 산출물이므로 자동 stage하지 않는다. 이번 작업에서 고쳤다면 아래 PM 손 잔여에서 경로를 직접 `git add` 한다 — 4단계 커밋은 stage 된 선언 경로만 싣는다.

> **DoD는 실행 전에 마감한다.** `board.py complete`는 ticket 본문 `## 완료 조건` 절의 체크박스를 전부 본다 — 통과 형태는 두 가지뿐이다.
>
> - `- [x] <원문>` — 실제로 했다.
> - `- [>] <원문> (이월: <사유·귀속>)` — 사용자가 명시적으로 범위에서 제외한 항목만 그 결정과 귀속을 같은 줄에 남겼다.
>
> 미체크(`- [ ]`)나 사유 없는 `- [>]`가 하나라도 남으면 rc=1로 차단되고, log skeleton은 이미 append된 상태다. 본문을 고친 뒤 재실행하면 중복 entry가 생기므로 **실행 전에** 본문 DoD를 마감한다.

> **stage 잔여 보고는 둘 다 확인한다.**
>
> - `⚠ 미스테이지 잔여 N건` = **under-stage**. 내 작업 누락이면 ticket `touches`를 보강해 다시 실행하고, 남의 WIP면 둔다. 선언 밖 경로는 4단계 커밋에도 실리지 않는다.
> - `⚠ 스코프 밖 staged N건` = 남이 index에 미리 올린 변경. 4단계 커밋은 선언 경로 pathspec 으로만 커밋하므로 실리지 않는다. 빼려면 `git restore --staged <경로>`.

`status.md`는 자동으로 건드리지 않는다. 테스트 수는 박제하지 않고 pytest 실측/history는 log에 둔다.

## 종결 뒤 PM 손 잔여

1. **status.md 모듈 판정/비고** — 모듈 상태가 바뀌었으면 architect가 코드 대조로 갱신하고 PM이 점검한다. 테스트 수는 박제하지 않는다. CLI 자동화 금지.

2. **log/current.md complete entry 서술** — `<PM: 무엇을·왜>`를 다음 실제 내용으로 교체:
   - 변경 파일 목록
   - 단위 테스트 수·증가량
   - 리뷰·fix 라운드 요약(finding 판정 분기)
   - 메타 학습(이번 묶음에서 확인한 수렴 신호)
   - spec/ADR 정합 갱신(있으면)

3. **묶음 산출 밖 파일** — ADR·domain 페이지·`status.md`처럼 티켓 `touches` 밖 산출은 4단계 커밋에
   실리지 않는다. 이번에 고쳤으면 그 경로만 따로 `git add` 하고 별도 커밋으로 싣는다. 종결이 만든
   커밋을 손으로 고쳐 끼워 넣지 않는다.

4. **wave 종결 entry** — `pm_playbook.md` §"Wave 메타 학습 누적" 표준에 따라 묶음 종결 entry 를 append 한다.

## 제약

- 회귀 red 시 즉시 중단한다(fail-soft 아님). `board.py complete`의 `--tests-pass`가 ticket complete를 막는다.
- 모듈 판정은 자동화하지 않는다.
- 커밋 문안은 엔진이 낸다 — 티켓 커밋은 티켓 제목, 머지는 `<단위> merge — <제목>` 이다. 손으로 다시 쓰지 않는다.
- backbone CLI는 `.project_manager/tools/ticket_finish.py`, 묶음 단계 표의 단일 진실은 `/pm-dev-delegate` §클러스터 단계 표다.
