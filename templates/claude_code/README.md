# Claude Code 어댑터 타깃

Claude Project Framework 의 **Claude Code 어댑터** 타깃. 루트 엔진(`.project_manager/`)을 공유하고
어댑터층(`.claude/`·`CLAUDE.md`)만 이 타깃에서 다르다. (ADR-0005)

> 프레임워크 **전체 가이드**(네 기둥·도입 절차·placeholder 표·워크플로·이식성 등급·계보)는
> 하니스 무관 공통 문서 — **루트 [`README.md`](../../README.md)**. 이 문서는 *Claude Code 어댑터
> 고유분*만 담는다 (opencode 타깃 [`../opencode/README.md`](../opencode/README.md) 과 대칭).

## 어댑터층

claude_code LLM 세션이 `CLAUDE.md` 를 진입으로 PM 을 구동한다 — opencode 의 `AGENTS.md`+`.opencode/`
에 대응하는 Claude Code 등가물. 엔진은 루트와 공유하고 여기 어댑터만 타깃 고유다.

- **`CLAUDE.md`** (진입) — 세션 부트스트랩·작업 원칙·자주 쓰는 명령. Claude Code 가 자동 로드.
- **`.claude/agents/`** — researcher · architect · developer · code-reviewer 서브에이전트 정의.
- **`.claude/skills/`** — PM workflow slash command (pm-bootstrap · pm-wave-claim · pm-dev-delegate ·
  pm-wave-finish · pm-handoff). 목록·역할 단일 진실 = `pm_role.md` §"skill 카탈로그".
- **`.claude/settings*.json`** · **`run_tests_hook.sh`** — PM 세션 권한 + 파일 편집 시 회귀 hook.

### 위임 기제 = `Agent` 툴 `subagent_type`

PM(메인 세션)이 `Agent` 툴을 `subagent_type=developer|code-reviewer|architect|researcher` 로
호출하면 `.claude/agents/*.md` 정의가 별도 자식 세션에서 구동된다. 표준 위임 프롬프트는
`/pm-dev-delegate` skill. (위임 *개념*·generate≠evaluate 는 루트 README §5.)

## 채택 (pm_import — 정규 경로)

manager 루트의 `pm-import.sh`(`/.cmd`) 파사드로 한다 (default harness = claude):

```bash
<manager>/pm-import.sh --new <dest>             # 신규 프로젝트 (디렉토리 + git init)
<manager>/pm-import.sh --into <dest>            # 기존 프로젝트에 도입 (비파괴 · 충돌 백업)
<manager>/pm-import.sh --new <dest> --dry-run   # 적용 전 계획만 — 파일 미변경 (권장)
```

(Windows 는 `pm-import.cmd`. `--from` 은 manager 루트 auto-default.) 파사드 없이 푸는 수동
longhand·placeholder 표·도입 절차는 루트 [`README.md`](../../README.md) §3·§4.

## 엔진 동기화 (메인테이너 · 루트 → 이 타깃)

엔진 경로만 덮어쓴다 — 어댑터·CLAUDE.md·README 는 보존(manifest 밖).

```bash
# 루트에서 직접 (--target 플래그)
python3 .project_manager/tools/pm_update.py --from . --target claude_code --dry-run

# 타깃 내부에서 (self-location)
cd templates/claude_code && python3 .project_manager/tools/pm_update.py --from ../../ --dry-run
```

## 참고

- `CLAUDE.md` — 채택자 세션 진입(부트스트랩·작업 원칙·명령) 단일 진실 (= opencode 의 `AGENTS.md`).
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).
- ADR-0005 — 모노레포 multi-target 구조.
