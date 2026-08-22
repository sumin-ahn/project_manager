## 리뷰 (code-reviewer · 2026-08-14)

### 판정: 반려

#### must-fix

1. **OpenCode 평면 command 15개의 operational-detail 참조가 전부 끊겨 있다.**
   `templates/opencode/.opencode/command/*.md` 15개는 모두
   `(references/operational-details.md)`를 링크하지만, 해소 대상인
   `templates/opencode/.opencode/command/references/operational-details.md`는 없다. 대표 지점은
   `templates/opencode/.opencode/command/pm-adr.md:20`, 누락 mapping은
   `templates/opencode/.project_manager/engine.manifest:119-137`이다. 현재 manifest는 command 15개와
   환경 ref 2개만 생성한다. `tests/test_doc_constants_guard.py:27-35`는 실제 파생
   consumer 경로 대신 root canonical detail을 읽어 이 출하 결함을 가린다. command별
   실재·containment·sensitivity 가드가 필요하다.

2. **Codex 전용 override 카드 2개가 T-0678/T-0679 전환을 흡수하지 않았다.**
   `templates/codex/.agents/skills/pm-dev-delegate/SKILL.md:13-16`과
   `templates/codex/.agents/skills/pm-review/SKILL.md:22-25`에 구 Windows 블록이 그대로 남아
   있고, 둘 다 `references/operational-details.md` 링크가 0개다. 반면 상위
   `.agents/skills @source=.claude/skills` mapping이 두 detail 파일을 생성해 **orphan 2개**가
   됐다(`templates/codex/.project_manager/engine.manifest:105-112`). 더구나 현재 Codex
   `pm-review` detail은 `Claude PM` 지시를 담고 있어
   (`templates/codex/.agents/skills/pm-review/references/operational-details.md:5`) 단순 link만 붙일
   수도 없다. Codex flavor canonical에서 두 override의 slim card/detail을 함께
   정합하고, 출하 surface 전수를 검사하도록 테스트를 확장해야 한다.

#### should-fix

- 없음. 위 두 항목은 DoD의 missing/orphan/reference containment을 직접 위반하므로
  둘 다 차단 사항이다.

#### suggestion

- 없음.

### 독립 검증 수치

- 타깃 집합: `claude_code,codex,opencode` 정확히 3. 변경 경로 162개
  (tracked 109 + untracked generated 53), touches 밖 0, instance-state diff 0.
- 현재 `pm_update --all-targets --dry-run`: rc0, 3타깃 모두 changes 0. 템플릿
  SHA-256 `7bacf5d3e4f322bfaecd2366432c8dd00764b524cd4f4893bb09ce4707f7c831` 및
  `local.conf=537a07c…`, `pm_state=0973707c…` 실행 전후 동일. developer 초기 적용
  증거는 43/38/55=총 136, 보완 적용은 17/17/19=총 53, RUN2 0/0/0.
- byte parity: 엔진 6×3=`18/18`, wiki 2×3=`6/6`, Claude agent `3/3`,
  Claude/OpenCode shared skill `30/30`, OpenCode command↔model skill `15/15`.
- 집합: model card/operational/env는 각 타깃 `15/15/2`; OpenCode 사람 command/env는
  `15/2`. 그러나 실제 해소는 OpenCode operational missing `15`, Codex operational orphan `2`.
- 독립 focused/parity/fresh/T-0675~T-0682 소비 표면: `671 passed, 6 skipped`.
  private-context ledger/doc/render-token 보강: `163 passed`. 합계 `834 passed, 6 skipped`.
  developer full 회귀 `10417 passed, 43 skipped`는 지시대로 재실행하지 않았다.
- `git diff --check` clean, `domain.py lint` clean. private-context hard allowlist `405→395`는
  pre-ship T-0679/T-0681 inflow 6건 제거+분리된 detail로 6건 경로 이동+OpenCode command
  stale 4건 제거와 일치하고 재생성 가드가 green이라 무근거 갱신은 아니다.

