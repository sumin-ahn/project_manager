---
name: pm-wave-finish
description: "wave 안 ticket 완료 부기 — ticket_finish.py wrapper + 회귀 측정 + log/current.md skeleton + board complete + git stage. 모듈 판정·비고·log/current.md 서술·git commit 은 PM 손. Triggers: 'T-NNNN 완료', 'ticket 정리', 'finish', 'pm-wave-finish'."
audience: pm-internal
---

# /pm-wave-finish T-NNNN — wave ticket 완료 부기

> {{PROJECT_NAME}} PM wave 안 ticket 완료 시 부기 자동화. backbone =
> `.project_manager/tools/ticket_finish.py`. 본 skill 은 호출 chain
> + PM 손 잔여 작업 안내.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사용 시점

dev/reviewer cycle 통과 (must-fix 0) 또는 PM 직접 구현 ticket 완료 시.

## 실행

```bash
python3 .project_manager/tools/ticket_finish.py T-NNNN
```

> `--repo <repo> --slot <N>` (multi-PM) — 회귀를 돌릴 worktree 슬롯을 명시한다.
> 분리된 PM 홈엔 `tests/` 가 없어 회귀가 활성 worktree(`tests/` 보유)에서 돌아야 하는데, 슬롯이
> 여럿이면 자동해소가 모호해질 수 있다. **솔로/단일슬롯/default-1 은 생략 가능**(자동해소)·미지정+진짜
> 모호(repo≥2·slot-1 부재)면 **fail-loud**(어느 슬롯인지 `--slot` 요구). pm_handoff `--repo/--slot` 과 동형.
> **`--no-pytest`** — 회귀를 별도(/pm-qa 등)로 이미 측정했을 때 회귀 단계를 skip(board complete 는
> `--tests-pass` 유지). 모호 게이트도 우회한다(회귀 cwd 불필요).
> `--section` 인자는 **deprecated no-op**(status.md 합계표 제거로 더 이상 쓰지 않음·후방호환 수용만).

## CLI 자동 처리

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단 (반려 → dev 재작업 필요).
2. **log/current.md complete entry skeleton append** — `## [YYYY-MM-DD] complete | T-NNNN — <title>` 형식. 본문 = `<PM: 무엇을·왜>` placeholder.
3. **board.py complete T-NNNN** — `--tests-pass` 가드 통과 후 status open→done.
4. **git stage** — 이 ticket 이 **선언한 경로만** 자동 `git add`. 선언원 = ticket frontmatter `touches` ∪ **이 실행이 실제로 쓴 산출물**, 즉 `.project_manager/wiki/log/current.md`(2단계가 append) + legacy 형상(board 미분리·**출하 기본**)에서 3단계가 옮긴 티켓 파일의 **옛/새 경로** — 그 둘뿐이다. ADR(`decisions/`)·domain 페이지·`architecture.md`·`status.md` 는 **다른 실행**의 산출이라 스코프 밖이다(디렉토리로 넓히면 남의 미완성 draft 까지 실려 좁힌 척만 하게 된다). 자동으로 안 실리니 **PM 이 아래 3 에서 손으로 나열**한다. commit 은 별도 (PM 손).

> **[4/5] 잔여 보고는 두 방향이다** — 어느 쪽도 침묵하지 않는다.
> - `⚠ 미스테이지 잔여 N건` = **under-stage**. 내 작업 누락이면 ticket `touches` 를 보강해 다시 stage 하고, 남의 WIP 면 그대로 둔다.
> - `⚠ 스코프 밖 staged N건` = 남이 index 에 미리 올려둔 변경이라 **bare commit 이면 내 커밋에 실린다**. 빼려면 `git restore --staged <경로>` — 아래 3 의 pathspec commit 을 쓰면 애초에 안 실린다.

> status.md 는 건드리지 않는다 — judgment-only·테스트 수는 박제 안 함(pytest 실측·history 는 log).

## 잔여 PM 손작업 (CLI 후)

1. **status.md 모듈 *판정/비고*** — architect content-truth·PM 점검. 모듈 상태가 바뀌었으면(라이브 결선/완성) architect 가 *코드 대조*로 갱신·PM 점검. **테스트 수는 박제하지 않는다**(pytest 실측). CLI 자동화 안 함.
2. **log/current.md complete entry 본문 서술** — skeleton 의 `<PM: 무엇을·왜>` 를 실제 내용으로:
   - 변경 파일 목록
   - 단위 테스트 수·증가량
   - dev/reviewer cycle 요약 (must-fix·should-fix 처리 분기)
   - PM 직접 처리 should-fix (1줄·dev 안 도는 영역)
   - 메타 학습 (wave 다음 단계·후속 ticket 후보)
   - spec/ADR 정합 갱신 (있으면)
3. **git commit — pathspec 필수** — bare `git commit` 은 *남이 stage 해 둔 것*까지 함께 싣는다. **[4/5] 가 `✓ git add — 선언 경로 N개만 stage` 아래 출력한 경로 목록이 곧 이 커밋의 pathspec** 이다 — 그대로 `--` 뒤에 붙이면 어긋날 일이 없다:

   ```bash
   git commit -m "T-NNNN — <title 요약>" -- \
     <ticket frontmatter touches 의 실경로들> \
     .project_manager/wiki/log/current.md \
     .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
     .project_manager/wiki/tickets/done/T-NNNN-<slug>.md \
     .project_manager/wiki/status.md      # 모듈 행 판정/비고를 손봤을 때만
   ```

   - **티켓 파일 두 줄(claimed·done)을 빠뜨리지 마라.** legacy 형상에선 [4/5] 가 `claimed/→done/` 이동을 stage 하는데, **옛 경로와 새 경로를 함께** 줘야 rename 이 커밋으로 완성된다. 빠뜨리면 티켓이 HEAD 에선 영영 `claimed` 로 남고 그 rename 이 index 에 남아 **다음 사람 커밋에 딸려간다**. board 분리 형상에선 그 이동을 board-git 이 자기 커밋으로 기록하므로 이 두 줄이 없다 — [4/5] 목록에도 안 뜬다.
   - **status.md·ADR·domain 페이지는 자동 stage 대상이 아니다** — 이번에 고쳤으면 경로를 직접 나열하라. 새로 만든 파일이면 `git add <경로>` 를 **먼저** 해야 한다: 미추적 경로를 pathspec 에 주면 `error: pathspec '…' did not match any file(s) known to git` 으로 **커밋 전체가 rc=1 로 죽는다**(실측).

   wave 단위 단일 commit(복수 ticket)이면 각 ticket 의 위 목록을 **합집합으로 나열**한다 — pathspec 생략·`-A` 로 갈음하지 않는다.
4. **wave 진행 중이면 다음 ticket** — `/pm-wave-claim` 으로 다음.
5. **wave 종결이면 wave 메타 entry append** — pm_playbook.md §"Wave 메타 학습 누적" 표준.

## 결정

- **모듈 판정·commit 자동화 안 함 (의도적)** — 현재-진실 doc(status 판정) 직접 편집·자동 commit 의 부수 영향 회피. *자동화는 잡일까지·판정/서술/commit 은 architect/PM 손* 패턴 정합.
- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. ticket complete 차단 (board.py complete 의 `--tests-pass` 가드).
- **wave 종결 commit message 형식** — `PM 세션(N차) wave M — <ticket 목록> + <핵심 메타 학습 요약>`. wave 단위 단일 commit (history bisect/cherry-pick 어려움 trade-off).

## 참고

- `.project_manager/tools/ticket_finish.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — wave 패턴 단일 진실
