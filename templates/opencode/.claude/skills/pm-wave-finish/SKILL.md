---
name: pm-wave-finish
description: "wave 안 ticket 완료 부기 — ticket_finish.py wrapper + 회귀 측정 + log/current.md skeleton + board complete + git stage. 모듈 판정·비고·log/current.md 서술·git commit 은 PM 손. Triggers: 'T-NNNN 완료', 'ticket 정리', 'finish', 'pm-wave-finish'."
audience: pm-internal
---

# /pm-wave-finish T-NNNN — wave ticket 완료 부기

dev/reviewer cycle 통과(must-fix 0) 또는 PM 직접 구현 ticket 완료 시 `.project_manager/tools/ticket_finish.py`를 실행하고 잔여 판단·서술·commit은 PM이 한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 실행

```bash
python3 .project_manager/tools/ticket_finish.py T-NNNN
```

- `--repo <repo> --slot <N>` (multi-PM): 회귀를 돌릴 worktree 슬롯. 분리된 PM 홈에는 `tests/`가 없어 활성 worktree에서 회귀해야 한다. **솔로/단일슬롯/default-1은 생략 가능**(자동해소). 미지정 상태에서 진짜 모호(repo≥2·slot-1 부재)하면 **fail-loud**하며 `--slot`을 요구한다. pm_handoff `--repo/--slot`과 동형.
- **`--no-pytest`**: 회귀를 별도(`/pm-qa` 등)로 이미 측정했을 때 회귀를 skip한다(board complete는 `--tests-pass` 유지). 회귀 cwd가 불필요하므로 모호 게이트도 우회한다.
- `--section`: **deprecated no-op**(status.md 합계표 제거, 후방호환 수용만).

## CLI 자동 처리

1. **회귀 측정** — `pytest tests/ -q`. red면 즉시 중단하고 dev 재작업. ticket complete도 차단한다.
2. **log/current.md skeleton append** — `## [YYYY-MM-DD] complete | T-NNNN — <title>`; 본문은 `<PM: 무엇을·왜>` placeholder.
3. **board.py complete T-NNNN** — `--tests-pass` 가드 + **DoD 부기 게이트** 후 open→done.
4. **git stage** — ticket frontmatter `touches` ∪ 이 실행이 실제 쓴 산출물만 `git add`:
   - `.project_manager/wiki/log/current.md`
   - legacy 형상(board 미분리·**출하 기본**)에서 옮긴 ticket의 옛/새 경로

ADR(`decisions/`)·domain 페이지·`architecture.md`·`status.md`는 다른 실행의 산출물이므로 자동 stage하지 않는다. 이번 작업에서 고쳤다면 아래 3단계에서 PM이 경로를 직접 나열한다. commit도 PM이 한다.

> **DoD는 실행 전에 마감한다.** `board.py complete`는 ticket 본문 `## 완료 조건` 절의 체크박스를 전부 본다 — 통과 형태는 두 가지뿐이다.
>
> - `- [x] <원문>` — 실제로 했다.
> - `- [>] <원문> (이월: <사유·귀속>)` — 안 했고, 사유와 귀속(다음 ticket·wave 종료 측정 등)을 같은 줄에 남기고 이월했다.
>
> 미체크(`- [ ]`)나 사유 없는 `- [>]`가 하나라도 남으면 3단계에서 rc=1로 차단되고, 2단계 log skeleton은 이미 append된 상태다. 본문을 고친 뒤 재실행하면 중복 entry가 생기므로 **실행 전에** 본문 DoD를 마감한다.

> **[4/5] 잔여 보고는 둘 다 확인한다.**
>
> - `⚠ 미스테이지 잔여 N건` = **under-stage**. 내 작업 누락이면 ticket `touches`를 보강해 다시 stage하고, 남의 WIP면 둔다.
> - `⚠ 스코프 밖 staged N건` = 남이 index에 미리 올린 변경으로 bare commit에 포함된다. 빼려면 `git restore --staged <경로>`; 아래 pathspec commit이면 포함되지 않는다.

`status.md`는 자동으로 건드리지 않는다. 테스트 수는 박제하지 않고 pytest 실측/history는 log에 둔다.

## CLI 후 PM 손작업

1. **status.md 모듈 판정/비고** — 모듈 상태가 바뀌었으면 architect가 코드 대조로 갱신하고 PM이 점검한다. 테스트 수는 박제하지 않는다. CLI 자동화 금지.

2. **log/current.md complete entry 서술** — `<PM: 무엇을·왜>`를 다음 실제 내용으로 교체:
   - 변경 파일 목록
   - 단위 테스트 수·증가량
   - dev/reviewer cycle 요약(must-fix·should-fix 처리 분기)
   - PM 직접 처리 should-fix(1줄·dev 안 도는 영역)
   - 메타 학습(wave 다음 단계·후속 ticket 후보)
   - spec/ADR 정합 갱신(있으면)

3. **git commit — pathspec 필수** — bare `git commit`은 남이 stage한 것도 싣는다. **[4/5]의 `✓ git add — 선언 경로 N개만 stage` 아래 출력 경로 목록**을 그대로 `--` 뒤에 쓴다.

   ```bash
   git commit -m "T-NNNN — <title 요약>" -- \
     <ticket frontmatter touches 의 실경로들> \
     .project_manager/wiki/log/current.md \
     .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
     .project_manager/wiki/tickets/done/T-NNNN-<slug>.md \
     .project_manager/wiki/status.md      # 모듈 행 판정/비고를 손봤을 때만
   ```

   - **ticket 파일 claimed·done 두 경로를 빠뜨리지 않는다.** legacy 형상에서는 옛/새 경로가 모두 있어야 rename이 완성된다. 누락하면 HEAD에 claimed로 남고 rename이 index에 남아 다음 commit에 딸려간다. board 분리 형상은 board-git이 이동을 자체 commit하므로 두 경로와 [4/5] 목록이 없다.
   - **status.md·ADR·domain 페이지는 자동 stage 대상이 아니다.** 이번에 고쳤으면 경로를 직접 나열한다. 새 파일은 먼저 `git add <경로>`한다. 미추적 경로를 pathspec에 주면 `error: pathspec '…' did not match any file(s) known to git`으로 **커밋 전체가 rc=1로 죽는다**.
   - wave 단위 단일 commit(복수 ticket)이면 각 ticket 목록의 **합집합**을 나열한다. pathspec 생략·`-A` 대체 금지.

4. **wave 진행 중** — `/pm-wave-claim`으로 다음 ticket.

5. **wave 종결** — pm_playbook.md §"Wave 메타 학습 누적" 표준에 따라 wave 메타 entry append.

## 제약

- 회귀 red 시 즉시 중단한다(fail-soft 아님). `board.py complete`의 `--tests-pass`가 ticket complete를 막는다.
- 모듈 판정·commit은 자동화하지 않는다.
- wave 종결 commit message: `PM 세션(N차) wave M — <ticket 목록> + <핵심 메타 학습 요약>`. wave 단위 단일 commit.
- backbone CLI는 `.project_manager/tools/ticket_finish.py`, wave 패턴 단일 진실은 `.project_manager/wiki/pm_role.md`.
