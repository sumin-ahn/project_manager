# opencode 어댑터 타깃

Claude Project Framework 의 **opencode 어댑터** 타깃. 루트 엔진(`.project_manager/`)을 공유하고
어댑터층(`.opencode/`·`AGENTS.md`)만 이 타깃에서 다르다. (ADR-0005·ADR-0006)

> 프레임워크 **전체 가이드**(네 기둥·도입 절차·placeholder 표·워크플로·이식성 등급·계보)는
> 하니스 무관 공통 문서 — **루트 [`README.md`](../../README.md)**. 이 문서는 *opencode 어댑터
> 고유분*만 담는다 (claude_code 타깃 [`../claude_code/README.md`](../claude_code/README.md) 과 대칭).

## 어댑터층 (완성)

opencode LLM(로컬 gemma / 회사 Pro)이 `AGENTS.md` 를 진입으로 PM 을 self-driven 으로 구동한다.
claude_code 의 `CLAUDE.md`+`.claude/` 에 대응하는 opencode 등가물 — 엔진은 루트와 공유하고
여기 어댑터만 타깃 고유다. (PM 구동 = `AGENTS.md`(harness-neutral 공통 코어) + `.opencode/pm-instructions.md`
(opencode 실행 모델·위임 규약·`instructions` 배열로 함께 자동 로드); 이 README 는 채택 경로 안내.)

- **`AGENTS.md`** (full 진입·harness-neutral 공통 코어) — PM 부트스트랩·엔진 호출(인코딩)·완료 기록·
  결정 권한·안전 가드. opencode build 세션이 곧 PM 이다. (실행 모델·위임 규약은 아래 pm-instructions.)
- **`.opencode/pm-instructions.md`** — opencode-고유 실행 모델·위임 규약. `opencode.jsonc` `instructions`
  배열로 공통 코어와 함께 자동 로드된다 (@source 전파 — 방법론 갱신이 채택자에 도달·ADR-0069).
- **`AGENTS.lite.md`** (경량 진입) — 한 파일 + 공유 엔진 + `.claude/skills/` + `.opencode/command/`로 PM
  happy-path(부트스트랩 → 발행 → 위임 → finish)를 자족 운영하도록 압축한 판. 회사 200K 배포 1급.
  도입 시 `--weight lite` 로 선택 (아래 §채택).
- **`.opencode/agents/`** — pm primary 정의(`mode: primary` — orchestrator relay spawn 타깃) +
  researcher · developer · code-reviewer · architect subagent 정의(`mode: subagent`).
  위임 1차 = 네이티브 `task` tool 이 이 정의를 별도 자식 세션에서 구동한다.
- **`.claude/skills/`** — PM workflow canonical 스킬 15개. opencode 모델이 `skill` tool로 소비하는
  채널이며 canonical은 이 경로 하나다. 스킬 스캔 비활성화 금지
  (`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` 미설정).
- **`.opencode/command/`** — `/pm-bootstrap` 같은 사람 슬래시 팔레트 진입 15개. opencode 1.18.16
  실측에서 팔레트는 `{command,commands}/**/*.md`, 스킬은 별도 `skill` tool 표면이므로 둘 다
  출하한다. 각 `<name>.md`는 root canonical `.claude/skills/<name>/SKILL.md`에서 `pm_update`가
  기계 생성하며 command 파일을 손으로 편집하지 않는다(T-0674·ADR-0065 개정 대기).

### 위임 규약 단일 진실 = `.opencode/pm-instructions.md §2`

위임 1차는 **opencode 네이티브 `task` tool** 이다 — PM(build primary)이 `task` tool 을
`subagent_type=developer|code-reviewer|architect|researcher` 로 호출하면 opencode 가 `.opencode/agents/*.md`
를 별도 자식 세션(fresh 200K 격리·subagent `model:` 대로)에서 구동한다. `opencode run` 외부
프로세스는 headless·CI·task tool 미노출 빌드용 **폴백**으로 강등됐다(pm-instructions.md §2.7). 자세한 규약·프롬프트는
`.opencode/pm-instructions.md §2`(공통 코어 `AGENTS.md` 와 함께 자동 로드)가 단일 진실. 결정 근거는 ADR-0006(§3·D2·D3·D5).

## 채택 (pm_import — 정규 경로)

채택은 **manager 루트의 `pm-import.sh`(`/.cmd`) 파사드**(= `pm_import.py` 호출)로 한다 — 어댑터
복사·placeholder 치환·board init·git init(`--new`)·**모델 결정적 해소**까지 한 번에 처리한다.
opencode 는 모델 placeholder 해소가 필수라 **수동 `cp -r` 은 불완전 — 쓰지 않는다**
(claude_code 에 있는 수동 longhand(루트 [`docs/manual-import.md`](../../docs/manual-import.md))에 해당하는 것이 opencode 엔 없다).

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

## Context safety: native compaction + checkpoint

OpenCode의 native compaction을 유지하고 `.opencode/plugins/ctx-guard.js`가 메인 세션의 최신
`AssistantMessage.tokens`를 관측한다. nudge·강화·final 밴드는 다음 model turn의 system context에
checkpoint 안내를 사이클당 한 번씩 추가할 뿐 prompt, 도구 실행, compaction을
차단하지 않는다. `session.compacted` 뒤에는 `pm_log.py snapshot` 최종 텍스트를 다음 model turn의
system context에 verbatim 1회 주입하고 dedup checkpoint 골격을 만든 뒤 다음 사이클을 재무장한다.
native task가 만든 자식 세션은 checkpoint 안내 대상에서 제외되고 자체 compaction으로 정리된다.

안내를 받은 PM은 현재 ticket 경계를 닫고 수동 checkpoint의 진행·결정·검증 상태를 박제한다.
압축 경계 골격은 기계 생성되며 PM은 구간·서사 불릿을 채운다. host로 채택한 인스턴스의 plugin은
`pm_update` 갱신 대상이고, guest로 추가한 plugin은 위 §갱신 채널대로 `add-harness opencode`를
다시 실행해 갱신한다.

### 모델 선택 (`{{OPENCODE_PRO_MODEL}}` 해소 · T-0033)

opencode 어댑터의 subagent `model:` 필드는 placeholder `{{OPENCODE_PRO_MODEL}}` 로 출하된다.
pm_import 가 이를 **추측 없이 `opencode models` 결정적 조회**로 해소한다 (해소 순서):

1. **`--opencode-model PROVIDER/MODEL`** (비대화/CI) — 먼저 치환, 가용목록 대조는 best-effort 경고.
   ```bash
   python3 .project_manager/tools/pm_import.py --new <PATH> --harness opencode \
     --opencode-model ollama/glm-5.2:cloud
   ```
2. **tty 대화형** — `--opencode-model` 미지정·터미널이면 `opencode models` 목록에서 번호 선택.
3. **비-tty·조회 실패·미선택 등**(비-tty/CI·opencode 바이너리 부재·`opencode models` 조회 실패·
   tty에서 가용목록 없음 또는 선택 건너뜀) — frontmatter 의 `model:` **줄 전체를 YAML 주석(`#`)으로
   비활성화**하고 (조회 성공 시) 가용목록을 인라인한 TODO 안내 + 경고를 남긴다. 이렇게 하면
   frontmatter 에 `model` 키가 *부재*하므로 **opencode 가 기본 모델로 agent 를 그대로 띄운다**
   (graceful — 깨진 미해소 placeholder 로 agent 가 거부되지 않음). 원하는 모델을 쓰려면 그 줄의
   주석(`#`)을 해제하고 `provider/model` 로 치환하거나 `--opencode-model` 로 재import 한다.

## 갱신 채널 (채택자 관점 · ADR-0058)

도입 후 upstream 프레임워크 변경을 흡수하는 경로는 이 어댑터가 **host 냐 guest 냐**로 갈린다.

- **opencode 로 채택** (이 템플릿을 import — opencode 가 host) — 엔진과 `.opencode/*` 어댑터가
  `pm_update`(`./pm-update.sh`)로 전파된다 (flavor manifest·ADR-0054). `AGENTS.md` 는 인스턴스
  소유라 유지된다 (claude `CLAUDE.md` 대칭).
- **claude 인스턴스에 dual-harness 로 얹은 opencode** (`add-harness opencode` — opencode 가 guest) —
  이 `.opencode/*` 는 host manifest 의 guest 절에 등재돼 `pm_update` 가 함께 전파한다: 렌더물
  (`@render`·agent 카드 등)은 채택자 `local.conf` 값으로 다시 렌더하고, 엔진 파일은 byte-copy 로
  갱신한다. `add-harness opencode` 재실행은 어댑터 파일이 새로 추가/폐기됐을 때 등재를 갱신하는
  용도다 (기존 인스턴스 위 live-safe·ADR-0048).

## 엔진 동기화 (메인테이너 · 루트 → 이 타깃)

루트에서 이 타깃으로 엔진을 동기화하는 방법은 두 가지다 (엔진 경로만 덮어씀 — 어댑터 보존). 전체
엔진 변경은 이 타깃만 손으로 골라 실행하지 말고, 루트에서 `--all-targets`로 `templates/` 아래의
**존재하는 모든 타깃**에 전파한다. 아래 `--target opencode`는 이 타깃만 의도적으로 재동기화할 때 쓴다.

**루트에서 전체 전파, 또는 이 타깃만 의도적으로 갱신 (`--target`):**
```bash
# 루트 repo 에서
python3 .project_manager/tools/pm_update.py --from . --all-targets --dry-run
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

- `AGENTS.md` — PM 부트스트랩·엔진 호출(인코딩)·완료 기록·결정·안전 가드 공통 코어 (= claude_code 의 `CLAUDE.md`).
- `.opencode/pm-instructions.md` — opencode 실행 모델·위임 규약 (`opencode.jsonc` `instructions` 배열 자동 로드·@source 전파).
- ADR-0006 · ADR-0069 — opencode 어댑터 타깃 + 진입 doc 공통 코어/하네스별 전달 채널 결정.
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).
