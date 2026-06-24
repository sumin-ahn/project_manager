---
description: "PM 환경 관리 단일 command — pm-config.sh facade wrap. repo add · worktree add(→pm-bootstrap 바인딩 안내) · slot status/release · upstream show/switch(path↔URL). multi-PM 셋업·upstream 전환의 단일 진입. Triggers: 'pm-env', 'repo 추가', 'worktree 추가', 'slot 상태', 'upstream 전환', '환경 관리'."
---

<command-instruction>

# /pm-env — PM 환경 관리 (pm-config facade)

> {{PROJECT_NAME}} PM 환경 셋업·조회를 한 command 로 — `./pm-config.sh` facade backbone 위에
> repo/worktree/slot/upstream 분기. multi-PM 토폴로지(여러 repo·worktree 슬롯)와 upstream 값 전환의 단일
> 진입. thin — `pm-config` 가 CLI 계약 단일 진실(서브커맨드 추가돼도 이 command 변경 불필요).

## 인접 command 와 구분
- **pm-env (이 command)** = 환경 *셋업/조회*(repo·worktree·slot·upstream 값).
- `/pm-update` = 환경이 가리키는 upstream 으로 엔진 *갱신*. upstream 값을 여기서 전환 → pm-update 가 적용.
- `/pm-bootstrap` = 세션 *시작* 시 슬롯 바인딩·상태점검.

## 분기 (trigger·인자로)

### repo add — multi-PM repo 등록
```bash
./pm-config.sh repo add <name> --git <url> --test "<cmd>"
```
`areas.md` 공유 레지스트리 등록 + per-repo 셋업. 이후 worktree 슬롯을 붙인다.

### worktree add — 슬롯 생성 (→ bootstrap 바인딩 안내)
```bash
./pm-config.sh worktree add <repo>
```
추가 후 PM 에게 안내: **"이제 `/pm-bootstrap <repo> --slot N` 으로 이 슬롯에 바인딩하세요"**
— `pm_bootstrap` 의 multi-PM identity surface(T-0074)와 연결(정체성=세션 맥락).

### slot status / release
```bash
./pm-config.sh status | whoami        # 풀/리스 + 이 세션 repo/슬롯/branch
./pm-config.sh release <slot> [--force]
```

### upstream show / switch (path ↔ URL · T-0145)
```bash
./pm-config.sh upstream show
./pm-config.sh upstream set <url|path>
```
`set` 은 검증 후 `local.conf upstream=` atomic 재기록(타 키 보존·fail-closed): URL→`git ls-remote` 도달성 · 경로→존재+checkout. 값 self-describing(https/ssh/file→URL · 그 외→경로)이라 **전환 후 `/pm-update` 가 자동 적응**(URL→cache clone · 경로→pull).

## 결정
- **단일 command**(trigger/인자 분기·사용자 확정) — `pm-config` 대화형 콘솔과 동형 진입.
- thin — 비즈니스 로직 0. upstream 전환 백엔드(검증·atomic·디커플)는 엔진(`pm_config upstream`·T-0145).
- ⚠️ opencode 채택자: 이 command(`.opencode/command/`)는 `@target-owned` 라 `pm_update` 가 전파하지 않는다 — 새 command 는 **re-import 로 도달**. `/pm-import` 가이드의 re-import 경로 참조.

## 참고
- 설계: ADR-0032(D3 스킬화·D4 upstream 하이브리드) · backbone facade `pm-config.sh`→`pm_config.py`.
- `/pm-update`(전환한 upstream 으로 갱신) · `/pm-bootstrap`(worktree add 후 슬롯 바인딩).

</command-instruction>
