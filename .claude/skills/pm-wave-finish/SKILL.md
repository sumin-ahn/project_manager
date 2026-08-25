---
name: pm-wave-finish
description: "묶음 종결(close) — ticket_finish.py 고정 8단계 wrapper: final-fix 확인 입력 preflight → 확인 생성·게이트 처분 → 티켓별 완료 기록 → 슬롯 커밋 → 재배치 → 머지 → 슬롯 반납 → board·포인터 커밋. 재실행이 곧 재개. 모듈 판정·비고·log/current.md 서술은 PM 손. Triggers: 'T-NNNN 완료', '묶음 종결', 'ticket 정리', 'finish', 'pm-wave-finish'."
audience: pm-internal
---

# /pm-wave-finish — 묶음 종결(close)

리뷰·fix 를 통과한 묶음의 종결을 `.project_manager/tools/ticket_finish.py` 한 커맨드가 고정 순서로
실행한다. 순서를 PM 이 규칙으로 지키던 자리를 커맨드가 가져갔으므로 **손 git 은 0**이고, PM 에게
남는 잔여는 서술(log/current.md)과 모듈 판정(status.md)뿐이다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

```bash
# 묶음 종결 — 고정 8단계
python3 .project_manager/tools/ticket_finish.py --cluster <C-이름>
# 티켓 표기도 같은 경로다 — 그 티켓이 속한 묶음 전체를 종결한다(크기 1이면 티켓 하나)
python3 .project_manager/tools/ticket_finish.py T-NNNN
```

- **완료 기록 단위는 묶음 하나다.** 티켓 ID 를 준 호출도 그 티켓의 묶음으로 해소해 같은 파이프라인을
  탄다(티켓당 별도 코드 경로 0). 묶음을 선언하지 않은 티켓은 board 가 크기 1로 접어 준다.
  멤버 중 final-fix 확인 입력이나 완료 조건이 덜 채워진 티켓이 있으면 **1단계(preflight) 또는 3단계(완료 기록)에서 멈춘다**
  — 그 지점까지의 부작용만 남고 나머지 단계는 실행되지 않는다.
- **재실행이 곧 재개다.** 각 단계는 자기 부작용이 이미 있는지 관측해서 건너뛴다(티켓이 done 인가 ·
  커밋할 변경이 남았나 · 통합 브랜치의 조상인가 · 슬롯이 아직 대여 중인가). 실패 지점을 고치고 같은
  커맨드를 다시 부르면 끝난 단계는 반복하지 않는다.
- `--repo <repo> --slot <N>` (multi-PM): 회귀·측정·stage 를 돌릴 worktree 슬롯. 분리된 PM 홈에는 `tests/`가 없어 활성 worktree 에서 회귀해야 한다. **단일 슬롯·default-1은 생략 가능**(자동해소). 미지정 상태에서 진짜 모호(repo≥2·slot-1 부재)하면 **fail-loud**하며 `--slot`을 요구한다. pm_handoff `--repo/--slot`과 동형.
- **`--task <이름>`** (task-mode · 일반 사용자 경로): 작업공간을 task 리스에서 해소해 회귀·diff 서킷브레이커 측정·stage 가 **그 worktree** 를 본다. `--repo/--slot` 과 혼합은 거부. task 세션(`/pm-bootstrap --task <이름>`)에서는 **이 형태가 정상 경로**다.
- **`--no-pytest`**: 회귀를 별도(`/pm-qa` 등)로 이미 측정했을 때 회귀 실행만 skip 한다(board complete는 `--tests-pass` 유지). 코드 트리 해소·diff 서킷브레이커 측정·stage 는 그대로 그 트리에서 한다. **wave 중 종결은 이 형태가 표준**이며 전량 검증은 릴리즈 절차 1회다.
- **`--dry-run`**: 편집·board·git 없이 어느 단계가 무엇을 할지만 출력한다. 처음 도는 묶음은 이걸 먼저 본다.
- `--section`: **deprecated no-op**(status.md 합계표 제거, 후방호환 수용만).
