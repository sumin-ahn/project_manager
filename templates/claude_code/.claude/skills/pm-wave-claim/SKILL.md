---
name: pm-wave-claim
description: "wave 안 ticket claim — board show + DoD self-containment PM 검증 + claim. ticket 본문에 placeholder / depends_on 미충족 / wikilink dangling 있으면 차단. Triggers: 'T-NNNN claim', 'ticket 잡기', 'wave 시작', 'pm-wave-claim'."
audience: pm-internal
---

# /pm-wave-claim T-NNNN — wave 시작 ticket claim

묶음을 선언하고 그 멤버를 claim한다. claim 전에 ticket self-containment를 PM이 검증하며, 검증은
멤버마다 한다(운영 단위가 묶음이어도 claim 은 티켓 단위 소유 표시다).

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

```bash
# 1. 묶음 선언 (이번 wave 의 운영 단위 · 통합 브랜치·설계 문서 결속 · 코드 트리는 명시 필수)
python3 .project_manager/tools/board.py cluster new <이름> --tickets <T-NNNN,T-NNNN> --spike <설계 문서 경로> --repo <이름> --slot <N>
python3 .project_manager/tools/board.py cluster show <이름>   # 선언값 + 멤버 현재 status

# 2. ticket 본문 dump (멤버마다)
python3 .project_manager/tools/board.py show T-NNNN

# 3. lint (의존성 일관성)
python3 .project_manager/tools/board.py lint

# 4. PM 검증 (아래 체크리스트 · 멤버마다)

# 5. 통과 시 claim (멤버마다)
python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 활성 lease 1개면 생략 가능
```

- 발행이 이미 크기 1 장부를 만들어 뒀으면 `cluster new` 가 그것을 흡수한다. 이미 여러 티켓의 묶음에
  속한 티켓은 거부되므로 옛 묶음을 먼저 정리한다.
- `cluster new` 는 코드 트리를 **명시로만** 받는다(`--repo <이름> --slot <N>` 또는 `--task <이름>`) —
  없으면 첫 쓰기 앞에서 rc=1 이다. 그 트리의 **현재 브랜치를 통합 브랜치로 기록**하고 묶음 브랜치를
  만드므로, 통합 브랜치를 체크아웃한 트리를 그 인자로 지목한다(cwd 는 판정에 쓰이지 않는다).
- 묶음 단계 표(설계 1 · 개발 N · 리뷰 1 · fix 1)와 예산은 `/pm-dev-delegate` §클러스터 단계 표가
  단일 진실이다.
