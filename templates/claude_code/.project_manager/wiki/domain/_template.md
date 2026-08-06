---
title: <한 가지 — 모듈 / 개념 / 작업방법>
type: concept            # concept | guide | research
covers:                  # 담당 코드 (글롭). 코드-무관 개념이면 이 줄 비움.
  - path/to/**
repo: self               # verified_at/covers 소유 시계: self | upstream(local.conf 의 로컬 경로)
                         # ⚠ upstream 은 freshness 판정 + domain 소환(affected/capture 의 worktree
                         # touches 매칭)이 모두 local.conf `upstream=` 해소에 걸린다 —
                         # 미설정/URL/경로 이동이면 covers 가 맞아도 소환 제외(경고 동반).
derived: false           # 사람 author. (코드서 자동생성이면 true·손대지 마)
updated: {{DATE}}
---

<!-- 현재-진실 vs 히스토리 (무엇이 이 페이지에 남나) — 이 안내는 채우면서 지운다.
  판정: 같은 사실을 "지금 X다"로 쓸 수 있으면 페이지, "언제 X로 바뀌었다"로만 쓸 수 있으면 log.
  남는 것 = 지금의 구조·형상 · 지금 유효한 보장 동작·기본값·경계 · 지금도 밟는 gotcha ·
    왜 이 모양인지의 링크. 나가는 것(log·ADR) = 언제 그렇게 됐나 · 옛 동작과 그걸 바꾼
    티켓·사유 · 지금은 없는 함정 · 결정 서사 본문.
  - 갱신은 덧붙이기가 아니라 교체다. 새로 안 사실이 기존 서술을 뒤집으면 delta 를 덧붙이지 말고
    그 서술을 고쳐 쓴다(덧붙이면 위에서 아래로 읽었을 때 무엇이 현재인지 모호해진다).
  - 값은 스냅샷 하나만 둔다. `A → B → C 로 올라왔다`는 계보 서술은 log 몫이다.
  - 시점이 붙는 사실도 서술을 앞세우고 시점은 괄호로 쓴다(`기준값은 X(YYYY-MM-DD 통보)`).
    줄머리에 날짜를 두면 이력 항목과 구분이 안 되고, 값이 바뀔 때 줄을 덧붙이게 된다.
  - 페이지에서 뺄 때도 삭제가 아니라 log 로 이동한다(실측으로 얻은 지식은 비싸다).
  - type: research 도 같은 규칙이다. 누적 대상은 조사 대상의 현재 사실이지 세션별 변경 기록이 아니다.
  기계 판정: `domain.py lint` 의 history 축(advisory·never-block)이 시점 스탬프(`YYYY-MM-DD`·
  `PM <N>차`·`vX.Y.Z`·`T-NNNN`)로 시작하는 헤딩·인용 블록과 이력 절 제목(`변경 이력`·`changelog`)을
  잡는다. 본문 안쪽에서 날짜·티켓을 근거로 인용하는 건 대상이 아니다. -->

# <제목>

## 한 줄
<이게 뭔지 / 뭘 하는지 1줄.>

## 본문
<!-- concept: 무엇 · 어떻게 동작 · 왜 이 모양 (간단·"왜 결정"은 ADR 로 링크)
     guide:   목적(언제 쓰나) · 단계(절차·tour) · 검증
     research: 조사 결과 · 근거 · 미해결 -->

## gotcha · 디버깅
<함정 · 실전 주의 · 디버깅 절차.>

## 관련
<!-- "왜 이 모양인가"(결정·근거)는 ADR 로, 옆 개념은 domain 페이지로 링크 -->
- 왜 → `[[ADR-NNNN]]`
- `[[관련-domain-페이지]]`
