# opencode 어댑터 타깃

Claude Project Framework 의 **opencode 어댑터** 타깃. 루트 엔진(`.project_manager/`)을 공유하고
어댑터층(`.opencode/`·`AGENTS.md`)만 이 타깃에서 다르다. (ADR-0005·ADR-0006)

> 프레임워크 **전체 가이드**(네 기둥·도입 절차·placeholder 표·워크플로·이식성 등급·계보)는
> 하니스 무관 공통 문서 — **루트 [`README.md`](../../README.md)**. 이 문서는 *opencode 어댑터
> 고유분*만 담는다 (claude_code 타깃 [`../claude_code/README.md`](../claude_code/README.md) 과 대칭).

## 어댑터층 (완성)

opencode LLM(로컬 gemma / 회사 Pro)이 `AGENTS.md` 를 진입으로 PM 을 self-driven 으로 구동한다.
claude_code 의 `CLAUDE.md`+`.claude/` 에 대응하는 opencode 등가물 — 엔진은 루트와 공유하고
여기 어댑터만 타깃 고유다. (PM 구동·위임 규약의 단일 진실 = `AGENTS.md §3`; 이 README 는 채택 경로 안내.)

- **`AGENTS.md`** (full 진입) — PM 부트스트랩·위임·인코딩 규약. opencode build 세션이 곧 PM 이다.
- **`AGENTS.lite.md`** (경량 진입) — 한 파일 + 공유 엔진 + `.opencode/command/` 만으로 PM
  happy-path(부트스트랩 → 발행 → 위임 → finish)를 자족 운영하도록 압축한 판. 회사 200K 배포 1급.
  도입 시 `--weight lite` 로 선택 (아래 §채택).
- **`.opencode/agents/`** — developer · code-reviewer · architect subagent 정의(`mode: subagent`).
  위임 1차 = 네이티브 `task` tool 이 이 정의를 별도 자식 세션에서 구동한다.
- **`.opencode/command/`** — PM workflow slash command (pm-bootstrap·pm-wave-claim·pm-wave-finish·
  pm-handoff·spike-new · claude `.claude/skills/` 등가).

### 위임 규약 단일 진실 = `AGENTS.md §3`

위임 1차는 **opencode 네이티브 `task` tool** 이다 — PM(build primary)이 `task` tool 을
`subagent_type=developer|code-reviewer|architect` 로 호출하면 opencode 가 `.opencode/agents/*.md`
를 별도 자식 세션(fresh 200K 격리·subagent `model:` 대로)에서 구동한다. `opencode run` 외부
프로세스는 headless·CI·task tool 미노출 빌드용 **폴백**으로 강등됐다(§3.7). 자세한 규약·프롬프트는
`AGENTS.md §3` 가 단일 진실. 결정 근거는 ADR-0006(§3·D2·D3·D5).

## 채택 (pm_import — 정규 경로)

채택은 **manager 루트의 `pm-import.sh`(`/.cmd`) 파사드**(= `pm_import.py` 호출)로 한다 — 어댑터
복사·placeholder 치환·board init·git init(`--new`)·**모델 결정적 해소**까지 한 번에 처리한다.
opencode 는 모델 placeholder 해소가 필수라 **수동 `cp -r` 은 불완전 — 쓰지 않는다**
(claude_code 에 있는 수동 longhand(루트 [`README.md`](../../README.md) §3.2)에 해당하는 것이 opencode 엔 없다).

```bash
# 신규 프로젝트 (디렉토리 생성 + git init)
<manager>/pm-import.sh --new <PATH> --harness opencode

# 기존 프로젝트에 도입 (비파괴·충돌 파일 백업)
<manager>/pm-import.sh --into <PATH> --harness opencode

# 적용 전 계획만 미리보기 (파일시스템·하니스 미호출) — 권장
<manager>/pm-import.sh --new <PATH> --harness opencode --dry-run
```

> Windows 는 `pm-import.cmd`. 파사드 없이 직접 호출하려면
> `python3 .project_manager/tools/pm_import.py …` (Windows 런처: `py -3.12 …pm_import.py …`).

### 모델 선택 (`{{OPENCODE_PRO_MODEL}}` 해소 · T-0033)

opencode 어댑터의 subagent `model:` 필드는 placeholder `{{OPENCODE_PRO_MODEL}}` 로 출하된다.
pm_import 가 이를 **추측 없이 `opencode models` 결정적 조회**로 해소한다 (해소 순서):

1. **`--opencode-model PROVIDER/MODEL`** (비대화/CI) — 먼저 치환, 가용목록 대조는 best-effort 경고.
   ```bash
   python3 .project_manager/tools/pm_import.py --new <PATH> --harness opencode \
     --opencode-model ollama/qwen3.6:27b
   ```
2. **tty 대화형** — `--opencode-model` 미지정·터미널이면 `opencode models` 목록에서 번호 선택.
3. **비-tty·조회 실패·미선택 등**(비-tty/CI·opencode 바이너리 부재·`opencode models` 조회 실패·
   tty에서 가용목록 없음 또는 선택 건너뜀) — frontmatter 의 `model:` **줄 전체를 YAML 주석(`#`)으로
   비활성화**하고 (조회 성공 시) 가용목록을 인라인한 TODO 안내 + 경고를 남긴다. 이렇게 하면
   frontmatter 에 `model` 키가 *부재*하므로 **opencode 가 기본 모델로 agent 를 그대로 띄운다**
   (graceful — 깨진 미해소 placeholder 로 agent 가 거부되지 않음). 원하는 모델을 쓰려면 그 줄의
   주석(`#`)을 해제하고 `provider/model` 로 치환하거나 `--opencode-model` 로 재import 한다.

## 엔진 동기화 (메인테이너 · 루트 → 이 타깃)

루트에서 이 타깃으로 엔진을 동기화하는 방법은 두 가지다 (엔진 경로만 덮어씀 — 어댑터 보존).

**루트에서 직접 (`--target` 플래그):**
```bash
# 루트 repo 에서
python3 .project_manager/tools/pm_update.py --from . --target opencode --dry-run
python3 .project_manager/tools/pm_update.py --from . --target opencode
```

**타깃 내부에서 실행 (self-location):**
```bash
cd templates/opencode
python3 .project_manager/tools/pm_update.py --from ../../ --dry-run
python3 .project_manager/tools/pm_update.py --from ../../
```

## 참고

- `AGENTS.md` — PM 부트스트랩·위임(§3)·인코딩 규약 단일 진실 (= claude_code 의 `CLAUDE.md`).
- ADR-0006 — opencode 어댑터 타깃: 위임·인코딩·모델·self-driven import 결정.
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).
