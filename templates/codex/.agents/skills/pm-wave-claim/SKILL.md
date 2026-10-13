---
name: pm-wave-claim
description: "wave 안 ticket claim — board show + DoD self-containment PM 검증 + claim. ticket 본문에 placeholder / depends_on 미충족 / wikilink dangling 있으면 차단. Triggers: 'T-NNNN claim', 'ticket 잡기', 'wave 시작', 'pm-wave-claim'."
audience: pm-internal
---

# $pm-wave-claim T-NNNN — wave 시작 ticket claim

묶음을 선언하고 그 멤버를 claim한다. claim 전에 ticket self-containment를 PM이 검증하며, 검증은
멤버마다 한다(운영 단위가 묶음이어도 claim 은 티켓 단위 소유 표시다).

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

```bash
# 1. 묶음 선언 (이번 wave 의 운영 단위 · 통합 브랜치·설계 문서 결속 · 코드 트리는 명시 필수)
#    <ticket-group> 은 새로 짓는 묶음 이름(영숫자와 . _ -, 예: login-fix). 티켓 ID 모양(T-NNNN)은 발행이 만든 크기 1 장부(C-T-NNNN)의 자리라 거부된다.
python3 .project_manager/tools/board.py cluster new <ticket-group> --tickets <T-NNNN,T-NNNN> --spike <설계 문서 경로> --repo <repo> --slot <N>
python3 .project_manager/tools/board.py cluster show <ticket-group>   # 선언값 + 멤버 현재 status

# 2. ticket 본문 dump (멤버마다)
python3 .project_manager/tools/board.py show T-NNNN

# 3. lint (의존성 일관성)
python3 .project_manager/tools/board.py lint

# 4. PM 검증 (아래 체크리스트 · 멤버마다)

# 5. 통과 시 claim (멤버마다)
python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 슬롯 모드, 활성 lease 1개면 생략 가능
# task 모드의 명시 실행 대상, 귀속은 task로 유지
python3 .project_manager/tools/board.py claim T-NNNN --task <task> --repo <repo> --slot <N>

# 6. 작업 폴더를 묶음 브랜치로 전환 (통합 폴더가 아닌 쪽 · <slot> 은 work/<repo>_<N>)
python3 .project_manager/tools/worktree_pool.py switch <slot> task/<ticket-group>
```

- 발행이 만든 크기 1 장부(`C-T-NNNN`)는 새 이름으로 선언한 `cluster new` 가 흡수한다(그 장부 id 는 없어진다).
  이미 여러 티켓의 묶음에 속한 티켓은 거부되므로 옛 묶음을 먼저 정리한다.
- `cluster new`는 코드 트리를 명시로 받는다. 슬롯 모드는 `--repo <repo> --slot <N>`, task 단일 작업공간은 `--task <task>`, task의 다중 작업공간은 `--task <task> --repo <repo> --slot <N>`으로 지목한다. 귀속 task와 선택한 소유 작업공간의 HEAD·브랜치를 결속하며,
  없으면 첫 쓰기 앞에서 rc=1 이다. 그 트리의 **현재 브랜치를 통합 브랜치로 기록**하고 묶음 브랜치를
  만드므로, 통합 브랜치를 체크아웃한 트리를 그 인자로 지목한다(cwd 는 판정에 쓰이지 않는다).
- 작업은 다른 작업 폴더를 6단계 `switch` 로 묶음 브랜치에 옮겨서 한다. 종결의 머지는 base_branch 를
  든 폴더(통합 폴더)에서만 되므로 코드 변경이 있는 묶음은 작업 폴더 2개가 필요하다. 코드 변경이
  없으면 한 폴더를 그대로 전환해도 닫힌다. 2개를 빌린 task 는 `--task` 만으로 폴더를 고를 수 없으므로
  new·cluster new·claim 에 `--repo <repo> --slot <N>` 을 붙인다.
- 묶음 단계 표(설계 1 · 개발 N · 리뷰 1 · fix 1)와 예산은 `$pm-dev-delegate` §클러스터 단계 표가
  단일 진실이다.
