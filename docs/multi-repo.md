# multi-repo (N×M) 운용 — `pm-config` 파사드 (ADR-0011·0014·0016)

> 루트 [README](../README.md) 의 *여럿이 같이 쓰면* 절의 상세 운용. PM 홈을 만든 뒤(5분 여정),
> 여기서 프로젝트 repo 를 attach 한다 — *홈 생성 → 프로젝트 attach* 단일 채택 서사(ADR-0026 비임베드).
> 홈은 M=1 이어도 worktree 로 프로젝트를 잡는다.

모드 = **multi-PM(N 세션 × M repo·ADR-0016)** 한 개념. N=1·M=1 = 옛 solo(슬롯 오버헤드 0). 한
*사용자*가 여러 repo 를 묶어 운용할 때(M>1 = **single-user multi-repo** 로 재정의·ADR-0016
가 ADR-0011 amend), 셋업·조회·진단은 루트의 `pm-config.sh`(`/.cmd`) 한 파사드로 한다:

```bash
<manager>/pm-config.sh repo add <name> [--git <url>] [--test "<cmd>"]  # repo 등록 + .repos clone (신규=--git 필수 / 기등록 repo 는 --git 없이 areas URL 로 mirror hydrate)
<manager>/pm-config.sh worktree add <repo>                         # 새 worktree 슬롯 + submodule init
<manager>/pm-config.sh status | whoami                             # 풀/리스 + 이 세션 repo/슬롯/branch
<manager>/pm-config.sh release <slot> [--force]                    # 작업완료 반납 / 수동 강제(백스톱)
<manager>/pm-config.sh update [--from <upstream>]                  # 엔진 갱신 (pm-update 흡수)
```

셋업·조회·진단 전용이다 — 런타임 worktree alloc/release 자동화는 `pm-bootstrap`/handoff 가 하고,
`pm-config release` 는 수동 반납/강제(백스톱)만. 브랜치 할당은 `pm-bootstrap <repo> --branch <B>`
소관. 솔로(M=1)는 이 파사드를 안 써도 된다 — board/tools 현행 그대로(additive).
