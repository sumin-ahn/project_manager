---
name: pm-wave-claim
description: "wave 안 ticket claim — board show + DoD self-containment PM 검증 + claim. ticket 본문에 placeholder / depends_on 미충족 / wikilink dangling 있으면 차단. Triggers: 'T-NNNN claim', 'ticket 잡기', 'wave 시작', 'pm-wave-claim'."
audience: pm-internal
---

# /pm-wave-claim T-NNNN — wave 시작 ticket claim

wave 시작 시 ticket 하나를 claim하며, claim 전에 ticket self-containment를 PM이 검증한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

```bash
# 1. ticket 본문 dump
python3 .project_manager/tools/board.py show T-NNNN

# 2. lint (의존성 일관성)
python3 .project_manager/tools/board.py lint

# 3. PM 검증 (아래 체크리스트)

# 4. 통과 시 claim
python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 솔로(M=1)면 생략 가능
```
