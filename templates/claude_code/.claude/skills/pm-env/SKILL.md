---
name: pm-env
description: "PM 환경 관리 단일 스킬 — pm-config.sh facade wrap. repo add · worktree add(→pm-bootstrap 바인딩 안내) · slot status/release · upstream show/switch(path↔URL). multi-PM 셋업·upstream 전환의 단일 진입. Triggers: 'pm-env', 'repo 추가', 'worktree 추가', 'slot 상태', 'upstream 전환', '환경 관리'."
---

# /pm-env — PM 환경 관리 (pm-config facade)

> PM 환경 셋업·조회를 한 스킬로 — `./pm-config.sh` facade backbone 위에 repo/worktree/slot/upstream 분기.
> multi-PM 토폴로지(여러 repo·worktree 슬롯)와 upstream 값 전환의 단일 진입. thin — `pm-config` 가 CLI 계약
> 단일 진실(서브커맨드 추가돼도 이 스킬 변경 불필요).

> **Windows 진입**: `./pm-config.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-config.cmd`**(동일 인자·
> pm_import 가 루트로 복사). PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신
> **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## 인접 스킬과 구분
- **pm-env (이 스킬)** = 환경 *셋업/조회*(repo·worktree·slot·upstream 값).
- [[pm-update]] = 환경이 가리키는 upstream 으로 엔진 *갱신*. upstream 값을 여기서 전환 → pm-update 가 적용.
- [[pm-bootstrap]] = 세션 *시작* 시 슬롯 바인딩·상태점검.

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
`set` 은 검증 후 `local.conf upstream=` atomic 재기록(타 키 보존·fail-closed): URL→`git ls-remote` 도달성 · 경로→존재+checkout. 값 self-describing(https/ssh/file→URL · 그 외→경로)이라 **전환 후 [[pm-update]] 가 자동 적응**(URL→cache clone · 경로→pull).

### worktree add timeout 노브 — 하네스 false-kill 방지 (T-0293·3-layer)

대형 repo `worktree add`(로컬 bare→full checkout·느린 디스크/VPN/Windows)가 *진행 중인데도* 짧은
고정 타임아웃에 죽는(false-kill) 걸 막는 3-layer 노브. 기본 **30분**·튜닝 가능:

- **엔진** `PM_GIT_TIMEOUT`(초·`none`/`0`/`unlimited`=무제한·기본 1800) — worktree add console-visible
  러너(T-0292). `export PM_GIT_TIMEOUT=none` 으로 초대형 repo 무제한(진행 콘솔 가시·hang 은 Ctrl-C).
- **claude 하네스** `BASH_DEFAULT_TIMEOUT_MS`·`BASH_MAX_TIMEOUT_MS`(ms·기본 1800000=30분) —
  `.claude/settings.json` `env` 블록에 출하 기본. 값 변경 시 세션 재시작(env 는 시작 시 read).
- **opencode 하네스** `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`(ms) — opencode 는 config 파일로
  못 실어(`.env` 자동로드 없음·실측) **shell export** 로: `export OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=1800000`
  (shell 프로파일/direnv `.envrc`). `EXPERIMENTAL` = 버전 의존이니 회사 버전서 라이브 확인.

넘으면 엔진 트립 메시지가 "터미널 직접 실행(`PM_GIT_TIMEOUT=none`)"을 안내한다. 값을 바꾸려면 위 3
표면(엔진 env·claude settings.json·opencode shell export)을 각각 조정.

## 결정
- **단일 스킬**(trigger/인자 분기·사용자 확정) — `pm-config` 대화형 콘솔과 동형 진입.
- thin — 비즈니스 로직 0. upstream 전환 백엔드(검증·atomic·디커플)는 엔진(`pm_config upstream`·T-0145).

## 참고
- 설계: ADR-0032(D3 스킬화·D4 upstream 하이브리드) · backbone facade `pm-config.sh`→`pm_config.py`.
- [[pm-update]](전환한 upstream 으로 갱신) · [[pm-bootstrap]](worktree add 후 슬롯 바인딩).
