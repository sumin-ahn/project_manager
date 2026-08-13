---
name: pm-wave-finish
description: "wave 안 ticket 완료 부기 — ticket_finish.py wrapper + 회귀 측정 + log/current.md skeleton + board complete + git stage. 모듈 판정·비고·log/current.md 서술·git commit 은 PM 손. Triggers: 'T-NNNN 완료', 'ticket 정리', 'finish', 'pm-wave-finish'."
audience: pm-internal
---

# /pm-wave-finish T-NNNN — wave ticket 완료 부기

dev/reviewer cycle 통과(must-fix 0) 또는 PM 직접 구현 ticket 완료 시 `.project_manager/tools/ticket_finish.py`를 실행하고 잔여 판단·서술·commit은 PM이 한다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](../../.claude/skills/pm-wave-finish/references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

```bash
python3 .project_manager/tools/ticket_finish.py T-NNNN
```

- `--repo <repo> --slot <N>` (multi-PM): 회귀를 돌릴 worktree 슬롯. 분리된 PM 홈에는 `tests/`가 없어 활성 worktree에서 회귀해야 한다. **솔로/단일슬롯/default-1은 생략 가능**(자동해소). 미지정 상태에서 진짜 모호(repo≥2·slot-1 부재)하면 **fail-loud**하며 `--slot`을 요구한다. pm_handoff `--repo/--slot`과 동형.
- **`--no-pytest`**: 회귀를 별도(`/pm-qa` 등)로 이미 측정했을 때 회귀를 skip한다(board complete는 `--tests-pass` 유지). 회귀 cwd가 불필요하므로 모호 게이트도 우회한다. **wave 중 완료 부기는 이 형태가 표준**이다 — 지정 회귀 실측(dev/reviewer 라운드 보고 숫자)을 근거로 부기하고, 전체 회귀는 릴리즈 절차 1단계 1회(조용한 트리·green 확인 → livegate record)로 미룬다(병렬 wave 의 전체 회귀는 타 dev WIP 로 오염된 신호다 — `pm_playbook.md` §"라운드 프로토콜").
- `--section`: **deprecated no-op**(status.md 합계표 제거, 후방호환 수용만).
