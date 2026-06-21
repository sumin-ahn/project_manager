---
name: spike-new
description: "사용자와 대화형으로 한 설계 주제를 진행하고 그 산출을 raw/spikes/ 에 박제하는 설계 스킬. 혼자 다 쓰지 않는다 — 실측 현황은 먼저 파악해 보고하고, 옵션·결정은 사용자와 한 절씩 합의하며 채운다. ADR/ticket 은 spike 파일 안 DRAFT 초안으로만(decisions/·board 안 건드림), 실제 발행은 PM 이 raw 초안 참고해 진행. spike 는 status: draft 동안 편집·세션무관 resume, 합의+사인오프 시 sealed 후 IMMUTABLE (ADR-0010). Triggers: '설계 spike', 'spike 만들어', 'raw 설계안', 'design spike', 'spike-new'."
---

# /spike-new <주제> — 대화형 설계 spike

> 사용자와 Claude 가 한 주제를 **대화형으로** 설계하고, 그 산출을 `raw/spikes/` 에 박제한다.
> ⚠️ **혼자 §0~§6 을 다 쓰고 끝내지 않는다.** 실측은 먼저 파악해 보고하고, 옵션·결정은 사용자와 한 절씩 합의하며 채운다.
> spike 는 **sealed 후 IMMUTABLE** — 생성 시 `status: draft`(편집·세션무관 resume), 합의+사용자 사인오프 시 `sealed (<date>)`. sealed 후 개정은 새 날짜 파일. (컨벤션 단일 진실: `raw/README.md` · ADR-0010.)

## 언제 쓰나

- 한 설계 주제를 **옵션 비교 + ADR/ticket 초안** 수준까지 사용자와 같이 익힐 때.
- 구분: 빠른 한 줄 후보·backlog 는 `ideas/` (`{{PY}} .project_manager/tools/board.py idea`). 한 주제의 설계 산출 박제는 여기 `raw/spikes/`.
- 주제가 명확하면 ideas 를 건너뛰고 여기서 바로 시작해도 된다.

## 흐름 — 대화형 (이 스킬의 핵심)

본질은 **한 번에 다 쓰지 않는 것** + **주제를 먼저 못박는 것**. 순서:

0. **주제 · scope 합의 (먼저!)** — *무엇을 설계하나* + *무엇은 범위 밖인가* 를 사용자와 한두 문장으로 확정한다.
   - **이게 합의되기 전엔 파일도 안 만들고 실측도 시작하지 않는다.** 주제가 흐릿하면 실측 중 흥미로운 곁가지(버그·개별 결함)로 주제가 새고, 그 증상이 마치 주제인 것처럼 굳는다 — 이 스킬이 막아야 할 1순위 실패.
   - 사용자가 준 한 줄이 추상적이면("X 를 유기적으로") scope 를 좁혀 되묻는다: 무엇이 핵심 산출(설계 대상)이고 무엇이 증상/곁가지(범위 밖 or §6)인지.
1. **파일 생성 + frontmatter** — 합의된 주제를 `title` 에. 아래 "파일 만들기" 절차.
2. **실측 파악 → 보고** — 코드/데이터에서 §1 현황을 직접 확인하고 사용자에게 보고한다. 여기까진 Claude 가 진행해도 된다 (사실 확인이라 합의 불필요).
   - **주제 고정 가드:** 실측 중 나온 곁가지(개별 버그·죽은 코드 등)는 §6 열린질문이나 별도 메모로 빼고 **주제로 돌아온다.** 곁가지를 §2 옵션 갈림길로 승격시키지 않는다.
3. **사용자와 설계** — §2 옵션부터는 한 절씩:
   - 옵션을 제시 → 사용자 의견 → **합의된 방향만** 파일에 기록.
   - 설계 의도가 걸리는 갈림길은 **추정으로 채우지 말고 사용자에게 묻는다.**
   - 사용자가 다음으로 넘어가자 하기 전엔 다음 절을 쓰지 않는다.
4. **마무리 + seal** — 모든 절이 합의되면 파일 저장 + 이번 spike 산출만 git add. 끝낼 때 **PM 수렴 권장안 블록**을 출력한다(아래) — PM(orchestrator)이 raw 초안을 받아 발행하도록.
   - **seal 단계 (ADR-0010):** 설계 절 전부 합의 + §4·§5 DRAFT 완비 + **사용자 사인오프** 확인 시에만 frontmatter `status:` 를 `draft → sealed (<date>)` 로 전환한다. **그 전엔 draft 로 둔다 — 혼자 봉인 금지(사인오프가 seal 트리거).** sealed 부터 immutable·인용 가능 / draft 는 편집 가능. ADR/ticket 발행 입력은 **sealed spike 만**(draft 는 PM 수렴 입력이 아님).
   - **핸드오프-중-spike:** 설계가 한 세션에 안 끝나면 `status: draft` 로 두고 핸드오프한다. 다음 세션은 *새 날짜 파일이 아니라 같은 파일*을 이어 쓴다. handoff entry "읽기 범위"에 `draft spike <파일> — 같은 파일 이어쓰기`로 명시한다(thread-tail 연속성과 동형).

## 파일 만들기

주제를 kebab-case 로, 오늘 날짜 suffix. 단일은 flat, 개정·첨부가 따르면 주제 서브디렉토리:
```bash
T="<주제-kebab>"; D=$(date +%F)
# (a) 단일 — flat:
F=".project_manager/wiki/raw/spikes/${T}-${D}.md"
# (b) 개정/첨부 동반 — 주제 서브디렉토리:
#     F=".project_manager/wiki/raw/spikes/${T}/<각도>-${D}.md"
mkdir -p "$(dirname "$F")"
cp .project_manager/wiki/raw/spikes/_template.md "$F" && echo "생성: $F"
```
생성 후: 파일 Read → frontmatter(`title`·`created`·`session`·`related`) 채우고 상단 사용법 주석 블록 삭제. **`status:` 는 `draft` 로 생성한다**(편집 가능·세션무관) — 합의+사용자 사인오프 시 §4 마무리에서 `sealed (<date>)` 로 전환(ADR-0010).

## 섹션 작성 가이드

- **0 요약 + 권고** — 합의된 결론. (대화가 끝난 뒤 채운다 — 처음부터 박지 않는다.)
- **1 실측** — 코드/데이터에서 직접 확인한 사실. 추정 vs 실측 구분 · `file:line` 근거. 도메인 경계·계약(모듈/레이어 경계 등)이 걸리면 명시. *Claude 가 먼저 채워 보고하는 절.*
- **2 옵션 비교 + 권고** — 핵심. 옵션 N개 → 장점/단점/비용 → 권고. *사용자와 합의하며 채운다.*
- **3 DRAFT 설계** — 스키마/DDL·인터페이스·의사코드, 전부 "DRAFT" 표시. 계약 변경 시 마이그레이션도.
- **4 ADR 후보 (DRAFT)** — 굳히려는 결정의 ADR 본문 초안을 **이 spike 파일 안에** 적는다. scope·기각 대안 포함.
  ⚠️ `decisions/` 에 ADR 파일을 만들지 않는다 · 번호 부여·실제 발행은 스킬 밖.
- **5 ticket 분할안 (DRAFT)** — 후속 구현을 ticket 단위로 **이 spike 파일 안에** 적는다. 각 항목 `touches`·`depends_on`·DoD 가설.
  ⚠️ `tickets/` 에 파일을 만들지 않는다 · `board.py` 도 건드리지 않는다 · 발행은 스킬 밖.
- **6 위험 / 열린 질문** — 스스로 못 정해 사용자 결정이 필요한 것. 비용·외부송신·키발급·안전경계가 걸리면 여기.
- **7** — 발행·후속 메모. 비워 둬도 된다.

## 발행은 PM (이 스킬은 raw 초안까지)

설계가 끝나도 이 스킬은 **raw/spikes 파일 박제까지만**. ADR/ticket 의 실제 발행(번호 부여, `decisions/`·`tickets/` 파일 생성, `board.py`)은 **PM(orchestrator)이 raw 초안을 참고해 진행**한다 — spike 는 PM 이 보는 위치(decisions/·board)를 안 건드리고, raw 안 §4·§5 DRAFT 가 PM 발행의 입력이다. 사용자 게이트가 걸리는 결정(비가역·비용·외부송신·안전)은 PM 이 사용자 비준을 거쳐 발행.

마무리 시 사이드이펙트 격리:
- 파일 저장 + (사용자 사인오프 시) 이 spike *자신의* frontmatter `status:` 를 `sealed (<date>)` 로 전환만. **`status.md`·`log/`·`board.py` 는 건드리지 않는다**(PM 담당 — spike 자체 frontmatter `status:` 는 별개).
- git: 이번 spike 의 `raw/spikes/` 하위 산출만 `add` (다른 변경과 섞지 말 것).

**PM 수렴 권장안 블록** — 마지막 응답에 출력 (PM 이 §0·§4·§5·§6 받아 수렴·발행):
```
spike 종료 — raw/spikes/<파일>
§0 권고: <한 줄>
PM 수렴 권장안:
- ADR    : <발행 권장 N건 | 불필요> · §4 초안 · scope: PM-자율(internal·reversible) | 사용자-게이트(비가역·비용·외부송신·안전)
- ticket : <N개> · §5 분할 · touches·depends_on
- 사용자 게이트 : <없음 | 있음: 비용·외부송신·키발급·안전경계·비가역>
- 열린 질문 : <§6 핵심 N개 — 사용자/PM 결정>
권장 시퀀스: <예: 사용자 scope 결정 → ADR 발행 → ticket 병렬 dev 위임>
```
PM 은 이 블록 + spike 본문(§0·§4·§5·§6)만으로 수렴을 시작할 수 있어야 한다.

## 결정

- **주제 먼저 고정** — scope(범위 안/밖)를 사용자와 합의한 뒤에야 파일·실측 시작. 실측 중 곁가지(증상)로 주제를 바꾸지 않는다.
- **대화형** — 이 스킬은 혼자 결정·완성하지 않는다. 실측 보고 후 옵션·결정은 사용자와 한 절씩.
- **raw/spikes = board 밖** — 자유 파일명. spike 는 `status: draft` 동안 편집 가능·세션무관 resume, **sealed (<date>) 부터 IMMUTABLE** — sealed 후 개정은 새 날짜 파일(vN 누적). immutability 는 생성이 아니라 seal 시점 바인딩(ADR-0010).
- **ADR/ticket 은 raw 안 DRAFT** — spike 파일 안에 초안으로(decisions/·board 안 건드림). 실제 발행은 **PM** 이 raw 초안 참고해 진행.
- **backbone 없음** — `cp` + 손 frontmatter 로 충분.

## 참고

- `.project_manager/wiki/raw/spikes/_template.md` — 복제 원본(섹션 골격)
- `.project_manager/wiki/raw/README.md` — `raw/` immutable 컨벤션 + spike draft→sealed 예외(ADR-0010)
- `.claude/agents/architect.md` — 무거운 설계 노동을 서브에이전트에 위임할 때(설계 노동 ≠ 결정)
