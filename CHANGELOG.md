# Changelog

이 프로젝트의 주요 변경 사항을 이 파일에 기록한다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 를 따르고,
버저닝은 [Semantic Versioning](https://semver.org/spec/v2.0.0.html) 을 따른다.

## [Unreleased]

### Fixed
- **Codex cross-harness egress 승인 브리지** — `workspace-write` 샌드박스의
  `network_access=false`를 유지한 채 `pm_delegate → claude/opencode/codex CLI` 실위임만
  Codex `exec_command` 건별 승격으로 실행한다. dry-run이 승격 필요를 미리
  표시하고, 실행은 `sandbox_permissions=require_escalated` +
  `--codex-egress-escalated` attestation을 동반한다. 최초 승인은 `pm_delegate.py`
  전용 reusable prefix로 기억하고, `delegate_enabled=true`인 후속 호출은 과금을
  재질문하지 않는다. 일반 sandbox 오호출은 원격 CLI
  재시도·raw 예약·과금 전 fail-loud하고, 거절/실패를 native GPT로 무음
  대체하지 않는다.
- **PM 홈 상시-red 예시 CI 제거** — claude_code 채택자 템플릿에서
  `.github/workflows/regression.yml`을 제거하고 세 하네스 manifest 모두 GitHub workflow를
  비출하한다. 표준 PM 홈에는 제품 테스트가 없어 기존 예시가 push마다 pytest exit 5와 실패 메일을
  만들었다. 기존 채택자는 자동 삭제 대상이 아니므로 `git rm .github/workflows/regression.yml`로
  제거한다(프로젝트가 직접 만든 workflow라면 삭제하지 말고 프로젝트 CI로 소유권을 전환).

### Docs
- pm-handoff 스킬 §사용 시점 구 계약 잔재 교정 — 컨텍스트 임계·wave 자연 종료를 핸드오프
  트리거로 나열하던 서술 제거. 핸드오프는 사용자 명시 종료 신호가 유일한 트리거이며, 컨텍스트
  임계 규약은 checkpoint 박제 후 컴팩션 통과다(compaction-native·PM 실오판 재현 사례로 확인).

## [1.6.2] - 2026-08-07

**채택자 제보 흡수 릴리스.** 실채택자(v1.4.5→v1.6.0→v1.6.1 2단계 채택)가 제보한 엔진 결함
10건과 잔여 로컬 편차를 전량 흡수한다. 관통 성질: add-harness guest 형상과 기존-채택자
업그레이드 경로가 처음으로 상설 게이트에 들어온다 — 지금까지의 fresh-adopter 게이트는 신규
설치만 검증해 이 클래스(디스크의 옛 데이터·guest 이력·치환 상태에서만 발현)를 구조적으로
못 봤다.

### 업그레이드 노트

- **`pm-update` 를 2회 실행하라.** 엔진 자신이 `pm-update` 로 배달되므로 1회차는 구 엔진
  코드로 돈다. v1.5.0~v1.6.1 엔진 + v1.5.0 이전 add-harness 이력(옛 세대 guest 마커) 조합은
  1회차에서 guest 절이 소실될 수 있다 — 신 엔진이 착지한 2회차가 마커 세대를 수렴시키고,
  절이 이미 소실된 채택자는 `미등재 flavor 파일 관측` 경고의 안내대로
  `./pm-config.sh add-harness <harness>` 재실행으로 복구한 뒤 다시 동기하면 된다(이후에는
  guest 절 파생 백필이 자동 유지한다).
- guest 하네스의 **엔진 파일**(codex `pm_orch_codex.py` 등)이 이번 릴리스부터 `pm-update` 로
  갱신된다. 종전에 동결됐던 파일은 첫 동기에서 상류와 수렴한다.

### Added
- **guest 하네스 엔진 파일 동기 채널** — guest 절 행이 2채널로 갈린다: `@render` 행(어댑터
  렌더물)은 종전대로 add-harness refresh 소유, 비-`@render` `@source` 행(guest 엔진 파일)은
  `pm-update` 가 byte-copy 로 갱신한다. add-harness 가 엔진 행을 함께 등재하고(복사 술어 공유로
  "등재 ⊆ 복사" 구조 보장), 구세대 절 채택자는 flavor 배타 경로 증거로 엔진 행을 파생·등재해
  (receipt 미사용·cross-ns 오탐 가드·지속화) 재실행 없이 동결이 풀린다. instance-owned
  config(`config.toml`·`hooks.json`·`settings.json`·`opencode.jsonc`)는 종전대로 불가침.
- **상류 은퇴 파일 보고 채널** — 상류에서 삭제·rename 된 등재 파일을 apply·dry-run·`--changes`
  가 같은 문구로 보고한다(0건 침묵·상한 20 + "외 N건"). 삭제 전파는 하지 않는다 — 채택자
  로컬 자산(등재 디렉토리 안 공존 스킬 등)과 은퇴 파일을 기계로 구분할 수 없어, 보고가 정확한
  하한이다. `--changes` 는 D/R 을 `removed_upstream` 버킷으로 분리해 "이번 동기가 받는 것"
  오보를 제거한다.
- **회귀 FULL 게이트 수집 하한** — `local.conf` 의 `regression_min_collected`(기본 0=off)가
  rc0 부분수집 false-green 을 `partial-collection` 으로, 하한 활성 + 요약 검증 불가를
  `unverified-collection` 으로 강등한다. 플래그에 수집수·하한·conf 앵커를 기록해 green 재사용을
  산술로 무효화하고, pre-push 재실행이 같은 앵커를 승계한다. 러너는 Popen tee(실시간 출력 +
  캡처)·요약 파싱은 stdout 단독·요약행 문법 완전 일치(앞뒤 로그 오염 양방향 차단).
- **외부리뷰 라운드 산출 장부 + wave 총예산** — 게이트별 라운드 이력(`rounds`: 판정·must-fix
  수·예약 순번)과 세션 총예산(`external_review_wave_budget` 기본 24·`--ack-wave` 승인 재개·
  세대 토큰으로 환불 우회 차단)을 장부에 신설, `--rounds-report` read-only 조회면을 제공한다.
  게이트 상한과 wave 예산은 독립 축이며 승인은 서로를 열지 않는다.
- **업그레이드-채택자 e2e 게이트** — 구세대 디스크 상태(옛 guest 마커·add-harness 이력·치환
  상태)를 주입한 채택자 픽스처로 guest 절 생존·동결 가시성·토큰 안정성·reconcile 절차 안전을
  상설 검증한다(v1.6.1 엔진이면 red 인 민감도 실증 포함).
- **render 토큰 소유권 가드** — bare 등재 파일의 운영 토큰은 소비 시점 치환 선언
  (`CONSUMPTION_TIME_TOKENS`)이 없는 한 출하를 차단한다(파일×토큰 매트릭스·소비처 실재 검증).
- **사설참조 strip 데이터-리터럴 표식** — `# pm:data-literal`(라인·begin/end 블록) 주석이 붙은
  wire 리터럴은 스트립·재유입 가드가 함께 제외한다(tokenize.COMMENT 한정·완전 포함 면제·경계
  걸침 탐지 유지·near-miss 오탈자 loud·lone CR 거부). v1.5.0 스트립 사고(guest 마커 절단)의
  클래스 폐쇄.
- **pytest 요약 파서 공용 seam** — 요약행 파서 5벌(첫-매칭)을 board.py 의 끝-탐색·문법 완전
  일치 파서로 승격하고 livegate·bootstrap·ticket_finish·handoff 가 공동 소비한다(사본 재유입
  가드·seam 부재 fail-closed + 재동기 안내).

### Fixed
- **guest 절 마커 세대 단절** — v1.5.0 스트립이 마커 리터럴을 바꿔 기존 채택자 guest 절이
  다음 동기에서 경고 없이 소실되던 것(제보 1항목). 읽기는 세대 집합 인식·쓰기는 현행 단일,
  dest 재부착 직전 1회 치환한다(다중-harness manifest 분기 포함).
- **frozen 경고 도달 불가** — guest 소유 경로 하나로 flavor evidence 전체를 버리던 판정을
  개별 제외로 교체(제보 2항목의 탐지 축). add-harness 채택자에서 경고가 실제로 발화하고, 문구를
  `미등재 flavor 파일 관측` + facade 복구 안내로 재조준했다.
- **`--changes` 미리보기 과소·과대보고** — `@source` 매핑 무시(제보 5항목·출하 manifest 7/7
  오분류)와 self-heal 미반영(제보 6항목)을 좌표·owner-우선순위 판정과 계획 manifest 헬퍼 공동
  소비로 폐쇄(미리보기 == 계획·legacy_preserved·guest 채널 상속 포함).
- **render 토큰 진동** — 치환된 `wiki/README.md` 를 pm-update 가 토큰으로 되돌리던 반쪽(제보
  7항목)을 토큰 소유권 정리로 폐쇄: README 는 토큰 제거(하네스 중립 문구), `pm_state.template`·
  `domain/_template` 의 `{{DATE}}` 는 소비 시점(생성기) 치환 소유로 이관.
- **테스트 훅 러너 하드코딩** — `run_tests_hook.sh` 가 `local.conf` 의 `test_cmd` 를 해소해
  실행한다(제보 9항목·`sh -c` 신선 셸로 엔진과 실행 의미 통일·rc 보존·읽기실패와 미지정 구분·
  파서 동형성 26종 배터리 잠금).
- **opencode.jsonc 빈 배열 삽입 파손** — 빈(또는 비-문자열 원소) `instructions` 배열에 후행
  쉼표를 만들던 삽입을 비-공백 바디 판정으로 교체(제보 10항목).

### Docs
- pm-update 스킬 §3: 선-cp 는 기본 생략(self-prop 채택자는 엔진이 manifest 도달을 처리·실측
  게이트 박제), cp 사용 시 guest 절 소실과 복구 절차를 명시. README 의 guest 채널·frozen 경고
  서술 현행화. `docs/manual-import.md`·`docs/placeholders.md` 의 치환 판정을 엔진과 등가로 교정.

## [1.6.1] - 2026-08-06

**게이트·렌더 견고화 릴리스.** 관통 성질: 조용한 degrade 의 잔여 클래스를 기계 판정으로 폐쇄한다 —
표기 렌더의 미소유 파일 skip, 외부리뷰 앵커의 자기잠김/무필터 송신, 락 사본 중복, 현재-진실
문서의 히스토리 누적이 각각 가드·fail-loud·공용 seam 으로 닫힌다.

### Added
- **공용 파일락 seam** — `file_lock.py` 신설. board·pm_log·pm_relay·pm_handoff·worktree_pool·
  external_review 의 배타 파일락과 O_APPEND 원자 append 를 단일 구현(POSIX flock·Windows msvcrt·
  프리미티브 부재 시에만 무락 폴백)으로 수렴한다. 락 경로 규약·권한은 각 도구가 유지하며,
  플랫폼 락 분기의 재복제를 AST 가드가 차단한다(수렴 잔여 0 을 가드가 박제).
- **codex 외부 리뷰어 가시 범위 격리** — 게이트 리뷰어를 저장소 밖 tracked 거울(시크릿 denylist
  동일 적용) + 세션·이력 없는 임시 홈(인증만 선언 복제·projects/기능 테이블 scrub·경로 노출 성질
  자물쇠) + 최소 allowlist env 로 실행한다. 세션 전사·옛 raw 의 echo 오염은 회신 채널 한정 검출로
  판정을 전면 불명확 처리하고, 격리 실패는 기본 차단(`--allow-unisolated-reviewer` 탈출구)이다.
- **영속 설치 기록(install receipt)** — `pm_import`/`add-harness` 가 실제 성립한 하네스를
  `.project_manager/install.json`(git 추적)에 기록하고 표기 독자 판정이 기록을 1순위로 소비한다
  (부재 시 증거 추론 폴백·손상은 `.corrupt` 백업 후 재기록·미래 schema 읽기/쓰기 거부).
- **pm_update `--paths`** — 명시 경로만 전파하는 opt-in 스코프(등재 검증 선행·디렉토리 하위 오타
  rc=1·board 분리 리매핑·부분 전파는 baseline/마이그레이션 비발화).
- **라운드 장부 소유 PM 홈 앵커** — 외부리뷰 라운드 상한 장부가 diff 슬롯이 아닌 소유 PM 홈에
  쌓인다(스냅샷/새 worktree 로 상한이 리셋되던 창 폐쇄·기존 슬롯 장부는 1회 승계·차단 상태 유지).
- **worktree refresh `--onto` 명시 해소** — 명시 ref 를 그대로 해소한다(자동 origin 대체 제거·
  무인자 기본 경로는 origin 우선 유지·성공 메시지가 실측 ref/sha 표기).
- **codex 템플릿 wiki seed 대칭** — claude/opencode 와 동일한 13종 seed(architecture·status·
  decisions/ideas/specs 스캐폴드 등)를 codex 템플릿도 출하한다(README dangling 링크 0).
- **domain lint `history` 축** — 현재-진실 domain 페이지에 세션별 delta/변경 이력이 쌓이면
  advisory 로 검출한다(시점 스탬프 lead-in·라벨-only 헤딩·bold delta·never-block). 판정 기준은
  `wiki/domain/README.md` 가 문서화한다.
- **manifest 실재 일반화 가드** — 각 flavor manifest 의 비-`@source` 경로가 템플릿 트리에
  실재하는지 회귀로 단언한다(등록됐는데 안 실리는 클래스 기계 차단).
- **미등재 출하 wiki 원장 가드** — 출하되는 manifest 미소유 wiki relpath 집합을 원장으로 박제,
  신규 미등재 파일 유입 시 red.

### Changed
- **표기 렌더 폴백** — manifest 미소유 출하 wiki(인스턴스 seed)는 설치 하네스 전체 집합 표기로
  폴백 렌더한다(loud·조용한 skip 제거). add-harness/`--into` 도 기존 공유 문서·seed 를 복사 전
  계획 표시·중앙 백업·경로 안전 검증·개행 보존으로 재렌더하며(멱등), placeholder 치환·manual
  fill 은 이번 실행이 복사한 파일로 한정한다. 표기 독자 = dest 실설치 하네스 ∪ 이번 선택
  (`installed_harnesses` 단일 판정·`pm_update` 동일 소비).
- **외부리뷰 앵커 보안** — conf 소유자 강등 시 소유 PM 홈의 유효 범위·denylist 를 승계하고,
  승계 불가면 전송 전 차단한다(`--paths` 탈출구 유지). 소유자 conf 읽기 실패는 `--paths` 로도
  차단(fail-closed). 명시 앵커 실행은 선택-전 config 를 읽지 않는다(자기잠김 제거). diff 폭
  서술은 단일 표로 수렴하고 슬롯 소유 근거는 명시 base 로 한정한다.
- **리뷰 raw 장부 앵커** — external_review 의 raw 기록이 diff 슬롯이 아니라 해소된 소유 PM 홈
  장부에 박제된다(`pm_delegate raw` 통합 조회 정합·슬롯 축적/오염 원천 제거).
- **gate_snapshot 정밀화** — 심링크 prefix-only 해소(false-red 제거), 출력 경로 거부를 git 공용
  디렉터리·타 등록 worktree 로 확장(prunable 등록 제외·`worktree prune` 처방), 성공 출력 검증
  파일 전량 열거, `worktree list -z` 파싱.
- **terminology 가드 변형 클래스 매칭** — 옛 표기 재유입 검사를 낱말 표기표+생성 정규식으로
  재편(구분자·대소문자·한영 혼용·합성어 경계). 항목별 변형 축 샘플·음역 선언을 메타 테스트로 강제.
- **failsoft 가드 self.<attr> 축** — 인스턴스 속성 바인딩 콜러블의 skew 전파를 스캐너가 추적,
  기존 fail-soft 가 삼키던 실 결함 4건을 재전파로 교정. 콘솔 worktree add 는 marked skew 시
  루프를 종료한다.
- **pm_import 쓰기 채널 TOCTOU 전면 폐쇄** — 복사·중앙 백업·재렌더·placeholder 치환·모델 해소·
  fill 전 채널의 dest IO 를 fd(`O_NOFOLLOW` 컴포넌트 순회·루트 (st_dev,st_ino) 신원 고정·기존
  파일은 단일 `O_RDWR` fd 로 백업+쓰기 결속·계획-상태 보존)로 전환한다. 루트 교체는 전체 중단
  (오염 차단), 파일 단위 경쟁은 loud 제외·rc 0 이다.
- **private-context 대장 라인 키 탈결합** — 검토 대장 키를 `(path, kind, match, surface, hash)`
  +count 로 재설계해 라인 이동만으로 재생성이 강제되던 결합을 제거한다(재생성 CLI 동반).
- **domain 현재-진실 규칙 출하** — 판정 요지를 `_template.md` 스캐폴드와 capture-draft 산출에
  싣고, 출하 wiki 전 파일의 링크 해소를 가드가 검증한다.

### Breaking (채택자 체감 동작 변경)
- 소유 PM 홈을 해소할 수 없는 위치의 무인자 `external_review` 실행은 경고 후 진행하지 않고
  rc=1 로 차단한다(문서화된 게이트 경로 `--ticket`/`--paths` 는 무영향). 커밋만 된 변경으로
  슬롯을 고르려면 `--base` 앵커 명시가 필요하다.
- wiki seed 를 symlink 로 운용하는 인스턴스는 add-harness/`--into` 가 rc=1 로 중단한다
  (이전: 해당 파일만 조용히 무시).

## [1.6.0] - 2026-08-06

**Compaction-native 컨텍스트 가드 릴리스.** 관통 성질: 컨텍스트 한계 대응을 차단(hard-stop)에서
"압축은 허용하고 서사를 미리 박제"로 전환한다. 가드 산출은 비차단 checkpoint 안내·log 박제·relay
회전으로 한정되며, 세 하네스(claude·opencode·codex)가 같은 규약을 공유한다. 전 경로 3하네스 실
LLM 라이브 검증 완료.

### Added
- **checkpoint 점진 박제** — `pm_log.py checkpoint` subcommand 신설. ticket 경계의 complete
  entry 에 task 태그가 붙고, compaction 경계에서 checkpoint entry 골격을 만들어 서사를 압축 전에
  보존한다. 연속성의 단일 진실은 파일(log·pm_state·board)이다.
- **relay 회전 가시화** — 세션 회전 시 loud 알림을 출력하고, 첫 spawn 의 bootstrap 응답도
  출력한다. ctx 예산의 유효 회전 임계가 bootstrap 실측 상한 이하이면 구동 전에 fail-loud 로
  거부한다(spawn-loop·토큰 무한 소모 차단).
- **import trust 상태 loud 화** — 신규 import 프로젝트가 trust 미승인이라 출하
  `permissions.allow` 가 무력한 상태(silent degrade)를 import 시점 안내와 콘솔 경고로 표면화한다.

### Changed
- **3하네스 ctx 가드 비차단 전환** — claude 훅은 stop 차단(deny/block)·핸드오프 allow-list·
  PreToolUse 등록을 삭제하고 stop 을 최종 넛지로 전환하며, compaction 후 밴드 marker 를
  재무장한다(멱등을 세션당 1회에서 사이클당 1회로 재정의). opencode plugin 은 stop 차단을
  삭제하고 임박 안내에 `session.compacted` 병행 신호를 추가한다(압축 후 checkpoint 안내 적재·
  plugin 재기동 시 압축 횟수 복원). codex PreCompact 훅은 compaction 차단을 제거하고 통과 +
  checkpoint 안내로 전환한다(README Context safety 전면 개정·headless 도달성 실측 명기).
- **relay 회전 판정을 driver usage 판정으로 일원화** — 세 driver 가 매 turn 후 wire usage 로
  post-turn marker 를 박제한다(claude stream-json usage / opencode step_finish tokens / codex
  누계 차분). pre-turn 의미론은 폐기. codex 는 thread 누계 usage 를 점유로 오독해 약 2.8배
  조기 회전하던 것을 turn 간 차분 판정으로 교정했다.
- **handoff 얇은 마감** — handoff entry 를 박제 entries 자동 목록 + 메타 학습 + pending intent +
  회귀 1줄로 축소했다(thread-tail 슬롯 폐지 — 서사는 ticket/checkpoint 경계 박제가 담당).

### Fixed
- **relay 파이프 입력이 조용히 사라지던 문제** — claude/opencode driver 가 child stdin 을
  격리하지 않아 supervisor 의 파이프 입력을 child 가 통째로 삼키던 것(첫 spawn 후 무출력
  정상종료)을 stdin 격리로 통일했다. 무출력 rc0 급 이상은 stderr 진단을 남긴다.
- **공유 log writer 의 동시 기록 lost-update** — log append 계열 writer 전부를 flock 직렬화
  seam 으로 수렴해 병렬 기록 유실 클래스를 닫았다.
- 옛 차단형(hard-stop) 표기 잔재 전수 정리 — README 3종 개정·전 타깃 전파·fresh-adopter 검증.

## [1.5.2] - 2026-08-03

**게이트 신뢰성 릴리스.** 관통 성질 — 게이트가 무엇을 검토했는지 기계가 보장한다: stale 스냅샷
검토(false-green), 프로세스 cwd 오해소, 엔진 사본 불일치의 조용한 흡수를 전부 fail-loud 로 닫았다.

### Added
- **게이트 격리 스냅샷 도구**(`gate_snapshot.py`) — 검토 대상 경로의 staged 내용이 working tree 와
  일치함을 검증하고 격리 스냅샷을 만든다. 미-stage 산출이면 생성을 거부한다(자동 stage 없음 — 병렬
  작업 오염 금지). HEAD·index 엔트리·실 파일 집합을 생성 전후 이중 대조하고, submodule 원본은
  건드리지 않으며, eol 속성 파일은 git 정규화 기준으로 비교한다. 병렬 작업에서는 `--paths` 를 파일
  단위로 지정한다. code-reviewer 위임 카드가 수동 절차 대신 이 도구 호출을 안내한다.

### Fixed
- **엔진 사본 불일치가 조용히 흡수되던 문제** — 넓은 예외 처리 경계 141곳을 전수 분류해 marked
  불일치는 재전파 또는 명시 종단 보고만 허용한다. AST 가드가 파라미터 전달·클로저·조건식 콜러블 등
  6개 축으로 새 흡수 유입을 차단하고, CLI 도구들은 불일치 시 traceback 대신 한국어 복구 안내
  (`pm-update 로 엔진 전체를 동기화한 뒤 다시 실행`)와 rc=1 로 끝난다.
- **위임·외부리뷰 도구가 프로세스 cwd 에 따라 조용히 오해소되던 문제** — 앵커를 명시 인자
  (`--cwd`·`--paths`·ticket touches)에서 파생한다. 외부리뷰 라운드 상한은 판정 라운드와 중단(kill)
  라운드를 구분해 센다(판정 4 + 미완 2).
- **보호훅이 `dirname` 부재 환경에서 무승인 push 를 통과시키던 문제** — 훅 경로 해소를 dirname
  비의존으로 바꾸고 해소 실패는 차단이다(fail-closed).
- **codex 채택자가 출하 문서 안내대로 스킬을 못 부르던 문제** — 스킬 진입 표기를 하네스별로 렌더한다
  (codex `$이름`·그 외 `/이름`). 실 설치본을 스캔하는 가드가 잔존 오표기를 차단한다.
- **다중 하네스 설치가 권장 경로에서 항상 경고 7건으로 시작하던 문제** — 템플릿 공유 파일을 byte
  동일로 정규화했다. 경고 채널은 실제 충돌에만 남는다.
- **부분 산출물 표시·실행 상한 경고가 도구마다 갈리던 문제** — 포맷터를 공용 seam 으로 단일화하고
  `--fill` 도 위임과 같은 상한 경고를 공유한다. 등록 하네스에 fill 실행기가 없으면 설치 전에 거부한다.
- 게이트 격리 스냅샷 안에서 board 앵커 의존 테스트가 항상 red 라 회귀가 완주하지 못하던 문제.
- 중앙 로더 전환 잔여(legacy 판정 경로·이중 traceback) 정리.

## [1.5.1] - 2026-08-01

**채널 신뢰성 릴리스.** 관통 성질 — 채널이 조용히 실패하지 않는다: 정상 작업을 죽이던 시간 판정,
산출물을 잃던 백그라운드 위임, 채택자 환경에서만 갈리던 설정·설치 형상을 라이브 실측 근거로 닫았다.

### Fixed
- **장시간 위임·외부 리뷰가 벽시계 타임아웃에 잘리던 문제** — 시간 판정을 "시작 후 경과"에서
  "마지막 진행 이후 침묵"으로 바꿨다. 진행 신호는 stdout·stderr 양쪽을 보므로 진행 로그가 전부
  stderr 로 나오는 평문 리뷰어도 살아남는다. 벽시계는 백스톱으로 남고 임계는 설정에서 조정한다.
- **백그라운드 위임이 끊기면 산출물을 못 찾던 문제** — raw 파일 위치를 실행 *전에* 공유 장부에
  등재한다. 호출이 죽어 표준출력(그 안의 경로)을 잃어도 `pm_delegate.py raw --unfinished` 로
  조회하며, 미마감 레코드 자체가 중단 증거다.
- **폴백이 claude·opencode 실패에는 발동하지 않던 문제** — 인프라 실패 분류가 codex 표기만 담고
  있었다. 세 하네스 전부 라이브 실측·공식 문서 근거로 한도·인증 패턴을 편입했고, 연결 실패처럼
  진단이 아예 나오지 않는 침묵은 기존 무응답 판정이 담당한다. 설정 오류는 여전히 미분류로 남아
  폴백 없이 실패를 알린다.
- **위임 대상이 작업 워크트리에서 티켓 본문을 못 읽던 문제** — 조회 명령이 장부로 단일 소유 PM
  홈을 확정해 입력을 재앵커한다(쓰기는 여전히 PM 홈 전용).
- **두 하네스를 함께 쓰는 채택자의 opencode 어댑터가 갱신되지 않던 문제** — 설치 manifest 를
  선택한 트리들의 합집합으로 잡는다.
- **머신-로컬 파일이 채택자에게 출하되던 문제** — 출하 열거를 git 추적분으로 좁혔고, 열거 결과
  0건은 성공이 아니라 실패로 세운다.
- **엔진 사본마다 설정 해소가 갈리던 문제** — 위임·외부 리뷰가 어느 설정 파일로 나갔는지 실행마다
  표시하고, 트리별 설정이 실제로 다르면 경고한다.
- **opencode 템플릿의 무시 규칙 파일이 자기 자신을 숨겨 출하되지 않던 문제**.
- **시크릿 스캔 승인이 폴백 수신자까지 승계되던 문제** — 승인은 해소된 수신자에 결속되며, 승인이
  붙은 실행은 폴백을 끄고 그 사유를 남긴다.
- **하네스 감지가 설정 경로 환경변수를 세션 표시로 오용하던 문제** — 실측한 세션 마커만 근거로
  삼는다.

### Added
- **세션 시작 로딩 축소** — 부트스트랩 사전 읽기 계약을 출하 문서 전 축에서 통일해, 새 세션이 대형
  문서를 통독하지 않고 진입 문서·현재 상태·부트스트랩 출력만 읽는다. 재유입은 테스트가 막는다.
- **어댑터 편집 금지의 기계 경고** — 위임이 어댑터 디렉토리(`.claude`·`.codex`·`.agents`·
  `.opencode`)를 건드리면 티켓 범위 판정과 무관한 축으로 경고한다.
- **출하 카드 구조 가드** — 코드펜스 파괴·하네스 전용 토큰 유입을 테스트가 잡는다.
- **문서 단위 재검증 채널** — `board.py verified-at-repin --page <경로>` 로 문서 하나만 재핀한다.
- **출하 엔진 커버리지 기계 집계** — `domain.py coverage` 가 담당 문서의 정성 주장과 실제를 대조한다.

### Changed
- 파일 열거·트리 순회 구현이 여러 도구에 흩어져 있던 것을 공용 진입점 하나로 모았다.
- 테스트가 실제 임시 디렉토리에 산출물을 쌓던 것을 정리했다(누적 파일이 실 산출물을 덮던 문제).

## [1.5.0] - 2026-07-28

**채택자 컨텍스트 감소 릴리스.** 관통 성질 — 출하물은 채택자가 쓰는 데 필요한 것만 담는다:
조회할 수 없는 개발 이력 포인터와 유지보수용 서사를 걷어내고, 재유입은 테스트가 기계로 차단한다.

### Changed
- **에이전트가 로드하는 문서 24% 경량화** — 스킬 카드 15장·에이전트 정의 4장·운영
  매뉴얼(pm_role/pm_playbook)·진입 문서(CLAUDE/AGENTS)를 지시 중심으로 압축 재작성
  (합계 293KB → 225KB). 커맨드·경고·Windows 노트·치환 토큰·절차 순서는 그대로다 —
  같은 행동을 더 적은 컨텍스트로 유도한다.
- **출하 소스·문서·런타임 메시지에서 내부 이력 참조 제거** — 채택자 환경에서 해소되지 않는
  내부 작업 번호·설계문서 절 참조·변경 경위 서사를 엔진 주석·docstring·CLI 출력·출하 md 에서
  정리했다. 이유 설명은 산문으로 자족하고, 이력은 git 히스토리가 담당한다.

### Added
- **사설 참조 재유입 가드** — 출하 표면(엔진 소스 산문·출하 md·templates)에 내부 작업 번호나
  설계문서 절 참조가 다시 들어오면 테스트가 실패한다. 마커류는 기준선 래칫으로 신규 발생만
  차단하고, 기준선이 실제보다 크면(정리 진행 미반영) 그것도 잡는다.
- **사설 참조 스트립 도구**(`scripts/strip_private_refs.py`) — 산문 토큰만 결정적으로 정리하는
  유지보수 도구. 코드 문자열·ID 예시 데이터는 건드리지 않으며, 삭제-only·들여쓰기 보존을
  스스로 검증한다.

## [1.4.5] - 2026-07-27

**Windows 실사용 마찰과 세션 격리 누수를 닫은 유지보수 릴리스.** 관통 성질 — 환경이 어긋나도
조용히 잘못된 상태로 가지 않는다: 콘솔 인코딩·인터프리터 버전·컨텍스트 보호·티켓 가시성이 전부
기계로 확인되거나 시끄럽게 실패한다.

### Added
- **Python 지원 하한(3.11) 선언과 검증** — 인터프리터 탐지·진입 파사드·보호 훅이 3.11 미만을
  후보에서 탈락시키고, 전부 미달이면 발견한 버전을 나열해 알린다. 도구 실행 방식과 동일한
  방식으로 버전을 확인하므로 런처가 다른 버전으로 우회 실행하는 창이 없다. 도입 진입점도
  불명확한 import 오류 대신 필요 버전을 명시해 중단한다.
- **위임 시크릿 차단의 건별 명시 승인** — 전송 전 스캔이 프롬프트를 막으면 탐지된 항목을 전부
  열거하고 승인 토큰을 함께 낸다. 사람이 발췌를 읽고 판단한 뒤 그 토큰으로 한 번만 통과시킬 수
  있으며, 승인은 그 프롬프트 전문과 해소된 수신자에 묶인다(한 글자만 바뀌거나 다른 모델로 보내면
  재승인). 상시 우회 설정은 만들 수 없다.

### Changed
- **커맨드 카드가 인터프리터 실값을 채운다** — 세션 시작 시 출력되는 명령 목록이 설정에 기록된
  검증된 인터프리터로 렌더링된다(해소 실패 시 기존 표기 유지). Windows 세션이 문서 표기를 그대로
  복사해 실행 실패·재시도로 도는 낭비가 줄어든다.
- **opencode 자동 컴팩션을 기본값으로 되돌림** — 전역으로 꺼두면 위임 서브에이전트가 컴팩션도
  컨텍스트 정지도 없이 죽는다(정지 기제는 메인 세션 전용). 메인 세션은 정지가 컴팩션보다 먼저
  발화해 그대로 보호되며, 그 선행 조건과 모델 설정 함정을 설정 주석에 명시했다.
- **세션 기본 화면이 사용자까지 구분한다** — 작업 이름이 겹치거나 작업공간을 넘겨받았을 때 다른
  사용자의 티켓이 섞여 보이던 문제를 닫았다. 숨겨진 항목이 있으면 이유를 알린다(조용히 빈 목록이
  되지 않는다).
- **작업공간 기준점을 슬롯 기록에서 읽는다** — 기준점을 지정한 작업공간은 그 기준으로 뒤처짐을
  판정한다(미지정이면 저장소 기본값). 판정에 쓴 기준의 출처도 함께 표시한다.

### Fixed
- **한국어 Windows 콘솔에서 명령이 죽던 문제** — 일부 명령이 출력 기호에서 인코딩 오류로 중단됐다.
  콘솔 인코딩 설정을 공용 진입 절차로 모으고 모든 명령줄 도구에 배선했으며, 새 도구가 이를
  빠뜨리면 테스트가 잡는다.

## [1.4.4] - 2026-07-27

**cross-harness 위임 채널을 실전 등급으로 끌어올리고(인프라 폴백·범위-밖 변경 가드·시크릿 스캔
정밀화), 좌표계·문서 신선도·출하 정합의 구조적 미탐/오탐을 닫은 릴리스.** 관통 성질 — 프롬프트
규율 대신 기계 판정: 위임이 만든 범위 밖 변경, 전파 안 된 어댑터, 좌표 불일치가 전부 기계로
표면화되거나 차단된다.

### Added
- **위임 인프라-실패 폴백** — 외부 하네스의 한도 소진·타임아웃·스폰 실패·응답 지연 시, 설정된
  폴백 하네스로 1단 loud 폴백한다(발동 사유·실행 provenance 를 결과에 명시). 발동은 인프라 실패의
  양성 분류만 — 모델 답변이 오류 문구를 *인용*해도 오발동하지 않고, 정상 완료(반려 포함)·전송-전
  차단은 폴백 대상이 아니다. **미설정이면 기존 fail-loud 그대로**(엔진 하드코딩 기본값 없음).
- **위임 범위-밖 변경 가드** — 위임 회수 시 작업 트리 전/후를 기계 비교해, 티켓 선언 범위 밖의
  신규/수정/삭제/이름변경, 이미 dirty 하던 파일의 재수정, 파일 모드 변경, 심지어 **위임 중 발생한
  커밋**까지 경고 블록으로 표면화한다(차단 아님 — 판정은 사람 몫). 읽기 전용 역할은 어떤 변경이든
  경고. 위임 시 `--ticket` 으로 허용 범위를 지정한다.
- **좌표 normalizer** — 멀티-worktree 형상에서 티켓 작업 범위 선언(홈 좌표)과 코드 저장소 상대
  좌표의 구조적 미매칭을 공용 seam 하나로 해소: domain 영향 페이지 소환이 실제로 동작하고(항상
  "없음" 이던 결함 폐쇄·디렉토리 선언도 매칭), 완료 부기의 stage 가 선언 경로를 실제로 싣는다.
  검증된 작업공간(장부+디렉토리 실재+git 저장소 정체성)일 때만 변환하고 불일치는 시끄럽게 거부.
- **읽기 조회의 앵커 표시** — board 조회 출력 첫 줄에 실제 측정한 저장소 경로와 역할 라벨을 명시해,
  다른 저장소 상태를 자기 것으로 오독하는 클래스를 닫는다.

### Changed
- **시크릿 스캔 양성매칭 전환** — 위임 프롬프트의 시크릿 차단을 문맥-무시 substring 에서 경로축
  (파일명·시크릿 디렉토리 정확-세그먼트)+값축(발급기관 prefix·개인키 블록·고엔트로피 할당·URL
  자격증명) 양성매칭으로 교체. 정상 설정 키명이 차단되던 오탐을 없애면서 이전엔 통과하던 실
  시크릿 형태(값·URL 내장 자격증명·`id_rsa` 류)를 신규 차단한다. 차단 메시지에 매칭 발췌를
  표시하되 값은 마스킹한다.
- **출하 정합이 blocking 으로 승격** — 프레임워크 루트의 토큰-form 어댑터 원본이 render-leak 으로
  오탐되던 12건을 출하-템플릿 미러 판정(전 타깃 byte 대조)으로 해소하고, 반대로 **어댑터 수정 후
  전 타깃 미전파**는 이제 lint 가 차단한다(어느 타깃이 뒤처졌는지 지목).
- **문서 신선도 시계 = 담당 코드 소유 저장소** — 페이지가 담당하는 코드가 다른 저장소(upstream)에
  있으면 그쪽 git 시계로 신선도를 판정한다(사본 시계의 조용한 오답 창 폐쇄). 검증 앵커 일괄 재핀
  CLI 동반.
- **외부 리뷰 타임아웃 실측 기반 900s** 기본 + 설정 채널(정상 라운드가 죽던 300s 대체)·실패 사유 병기.
- **엔진 일괄 전파 `--all-targets`** — 존재하는 모든 출하 타깃에 한 번에 동기하고, 진입문서의 타깃
  열거와 실디렉토리 집합 일치를 기계 검증한다.
- **task 진입 좌표 단일화** — task 세션 진입은 `--task` 단독으로 고정하고 슬롯 좌표 혼합 지정은
  일괄 거부한다(어느 축이 이겼는지 모호하던 상태 제거). subprocess 텍스트 디코딩 UTF-8 명시 동반.

## [1.4.3] - 2026-07-25

**문서-신선도 판정의 거짓 green 을 근절하고, dual-harness 채택 채널과 장기 세션 안정성을
강화한 패치.** 관통 성질 — **판정할 수 없는 것은 조용히 통과시키지 않고 정직하게 알리며,
안전장치·리뷰 게이트는 자의 판단이 아니라 기계 한도로 멈춘다.**

### Added
- **문서 신선도 관찰불가 표면화** — 현재-진실 문서(architecture/status/domain)의 `verified_at`
  판정이 "판정 불가"를 구분해 advisory 로 알린다: 저장소에 없는 covers 경로, 다른 git 의 SHA,
  움직이는 ref(브랜치/태그·16진 이름 포함), 모호한 축약 SHA, 비-선조 커밋 — 전부 이전엔 조용한
  green 이던 클래스. anchor 는 canonical full OID 로 유일 해소해 기록·소비한다.
- **guest 어댑터 인스턴스 manifest 등재** — `add-harness` 가 설치한 guest 어댑터(dual-harness)를
  인스턴스 `engine.manifest` 마커 구획에 등재해, 채택자 형상에서도 렌더·잔존-토큰 검사가 guest 를
  커버한다. 업데이트는 guest 를 덮지 않고(구획 보존·plan 제외), 재실행(refresh)이 동기 채널.
  cross-harness 의존물(예: codex 호스트의 `.claude/skills`)도 정확히 따라온다.
- **외부 리뷰 라운드 상한** — 게이트별 라운드 장부·기본 4회 한도. 초과분은 실행 전 차단되고
  사용자 승인(`--ack-rounds`) 후에만 재개된다(호출 전 예약이라 타임아웃으로 우회 불가).
- **cross-harness 역할 위임 채널 (`pm_delegate`)** — PM 세션이 세션을 떠나지 않고 역할 노동
  (developer·researcher·architect·code-reviewer)을 **다른 하네스 CLI** 로 위임한다. 호출측 하네스
  조건 0(N×N 대칭) — claude·codex·opencode 세 드라이버를 지원하고, 역할→(하네스·모델·reasoning)
  매핑을 설정에서 **티어 세트 통째로** 해소한다(평시/난제 2티어·부분 상속 없음·미설정은 조용한
  폴백 대신 fail-loud). 역할축으로 권한을 강제하고(쓰기=developer·architect / 읽기=researcher·
  code-reviewer), 엔진 코드 쓰기 위임이 잘못된 저장소를 향하면 차단하며, 프롬프트 시크릿 스캔과
  하위 프로세스 환경변수 정제를 거친다. 결과는 최종 답변만 회수하고 원문은 별도 파일로 박제.
  **기본 OFF** — 외부 송신·과금 수용 opt-in 을 설정에서 켜야 동작한다.

### Changed
- **컨텍스트 가드 세션-스코프 분리** — hard-stop 은 메인 세션만(정제 handoff 강제), 서브에이전트는
  compaction 을 허용해 장기 위임 작업이 컨텍스트 한도로 죽지 않는다(auto-compact 재활성).
- **covers 글롭 판정을 원본 글롭으로** — 신선도·stale 판정이 손실 접두사 대신 원본 글롭
  (`:(glob)`·지원 문법 검증)을 git 에 직접 전달한다. 미지원 형태는 오번역 대신 advisory.
- **잔존-토큰(overlay)·테스트 게이트의 하네스 축 파생 전환** — 손-열거를 manifest/레지스트리
  파생으로 교체해 새 하네스가 자동 편입된다(닫힌 은퇴 채널은 명시 보존).

### Fixed
- 엔진 자기서술 stale 3건(pm_import docstring·`--task` help·주석 경로) 정정.
- add-harness/manifest 경로 안전(containment·symlink 거부)·TOML 사용자 override 보존 강화.

## [1.4.2] - 2026-07-24

**Codex 어댑터 첫 실전 운영에서 드러난 결함을 닫는 패치.** Codex 를 PM/개발 하네스로 실제
운영(도그푸딩)하며 발견된 권한·compaction·설정 보존 결함들과, 공유 board·엔진 갱신의 정직성
결함을 수정한다. 관통 성질 — **어댑터가 채택자 소유 설정을 덮지 않고, 안전장치는 차단 후
복구 경로까지 제공하며, 도구는 실패를 성공으로 위장하지 않는다.**

### Added
- **Codex 일상 작업 권한 정렬** — Claude `settings.json` 동급의 allow/deny 를 Codex
  execpolicy 로 제공. 일상 명령·통상 `git push` 는 무질의 실행, 파괴적 push
  (`--force`/`--delete`/`--mirror` 등)만 확인 질의로 분리.
- **Codex TUI auto-compaction 차단** — 프레임워크는 compaction 이 아닌 handoff 지향.
  PreCompact hook 이 auto-compaction 을 차단하고(내구 상태 보존), 차단이 반복되는 임계
  상황에는 **one-shot 복구 절차**(chat ID 확인 → hook 비활성 resume → handoff → 새 세션)를
  화면에 안내한다 — 영구 설정 약화 없이 빠져나오는 break-glass. footer 에 잔여 컨텍스트 표시.
- **Codex ticket scaffold·board 상태 디렉토리 자가복구** — 부분 손상 형상에서 조용히 실패하던
  경로를 자가복구로.

### Fixed
- **어댑터 refresh 가 채택자 소유 Codex 설정을 덮던 문제** — `add-harness`/재-import 가
  `config.toml`·`hooks.json`·`AGENTS.md`·agent 별 model override(명시 `model`/
  `model_reasoning_effort` 있는 TOML)를 보존한다(byte 보존·loud skip). native delegation
  override 도 보존.
- **board 가 local commit 실패를 "보존"으로 위장 보고하던 문제** — 실패를 정직하게 보고.
- **ticket 완료 stage 가 잘못된 저장소에 고정되던 문제** — task 모드에서 실제 작업
  worktree 기준으로 저장소별 stage.
- **무변경 엔진 갱신에서 revision 키가 수렴하지 않던 문제** — `pm-update` 반복 실행 멱등.
- **render-leak 백스톱의 확장자 열거 누락·어댑터 토큰 치환 오판** — 제외-판정 방식으로 역전해
  신규 확장자에도 안전.
- 네트워크 격리 환경에서 소켓 E2E 테스트가 EPERM 으로 깨지던 것을 capability 감지 skip 으로.

### Changed
- **PM 지시 문서의 git commit 을 경로 명시형으로** — bare `git commit` 이 공유 워킹트리에서
  타 작업의 stage 를 함께 싣던 클래스를 절차·가드로 폐쇄(디자인 stage/commit 스코프화 포함).
- **게이트 하네스 축을 파생으로 전환** — 하네스 목록을 단일 원천에서 파생해 신규 하네스 추가
  시 게이트 누락을 방지.
- README 를 발표 본편과 운영 레퍼런스로 분리·흐름 개편.

## [1.4.1] - 2026-07-21

**프레임워크가 자기 자신을 운영하다 드러난 결함 9건을 닫는 패치.** 세션 진입(부트스트랩 0단계)이
실행 불가능한 해소 커맨드를 주던 것에서 시작해, 보호 브랜치 규율의 기계 강제·보호목록 설정 채널
신설·공유 board 의 claim 차단 해소까지 이어졌다. **관통하는 성질 하나 — "값을 바꿨는데 그 값이
실제 동작에 도달하지 않는" 끊김과, "기계가 정합하다고 보고하는데 실은 아닌" 거짓 green 을 없앤다.**
전 변경 이중게이트(내부 reviewer + codex 외부·실결함 21건 다라운드 수렴)·회귀 4600.

### Added
- **보호 브랜치 목록 설정 채널** (ADR-0072·T-0417) — 지금까지 `areas.md` 표를 손으로 고치는 것이
  유일한 경로였고, 고쳐도 훅이 읽는 sidecar 는 stale 이라 사용자가 바꿨다고 믿는 값과 훅이 강제하는
  값이 갈렸다(silent). 기계 설정·조회 + 값-연결 폐쇄:
  - `pm-config repo add <name> --protected "main,develop"` — 등록 시점에 보호목록 지정
    (종전 `protected=""` 하드코딩 제거).
  - `pm-config repo protected <repo> [<목록>|default]` — 값 없으면 **조회**, 주면 **설정**
    (`upstream show|set` family 동형). `default` = 칼럼 비움 = main/master/develop 폴백
    ("보호 없음" 은 표현 불가 — 빈 문자열은 거부하고 `default` 로 안내).
  - 조회는 **실효값 + 출처(명시/기본값 폴백/미등록) + 훅 sidecar 정합/drift** 3줄 — "빈 값이라
    기본값으로 도는 중"·"이 clone 의 훅만 옛 목록" 을 각각 구별해 보여준다.
  - 설정은 **areas.md → sidecar 순서 고정**(역순이면 비준되지 않은 목록을 훅이 강제) + board-git
    best-effort 동기. 다른 clone 은 `/pm-bootstrap` 0단계가 **drift 일 때만** sidecar 를 재설치한다
    (정합이면 subprocess 0·fail-soft).
  - `pm-config repo list` — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner). 빈
    `protected` 는 "기본값" 을 명시해 "보호 없음" 오독을 막는다.
- **`board.areas_set_cell(repo, column, value)`** (ADR-0072) — areas.md 를 "등록(행 추가)은
  append-only, **기존 셀 변경은 `board_lock()` 하 비파괴 in-place 재기록**" 으로 재규정한 범용
  백엔드. 줄 종결자(CRLF)·주석·타 행 보존, 구 헤더는 canonical 8칼럼으로 업그레이드, wider-row 는
  canonical 인덱스로 매핑 — 인덱스 해소는 `_migrate_areas_text` 와 **공용 헬퍼**로 공유한다
  (두 벌로 갈라지면 T-0168 칼럼 오매핑 재발). 대상 repo 행이 2개 이상이면 fail-loud(부작용 0).
- **`board lint` `areas-duplicate-repo` 권고** (ADR-0072·advisory·never-block) — 중복 repo 행이
  first-match 로 조용히 굳는 것을 상시 표면화한다(자동 병합 없음·사람 판정).

### Changed
- **보호 브랜치에서의 `git commit` 이 차단된다** (ADR-0071·T-0415) — **동작 변경**: 지금까지
  통과하던 커밋이 막힌다. 풀 슬롯(worktree)에서 보호 브랜치(`main`/`master`/`develop`·per-repo
  override)를 체크아웃한 채 커밋하면 pre-commit 훅이 거부한다. 종전엔 "보호 브랜치에 자율
  commit 하지 않는다" 가 문서 규율일 뿐이었고 강제 수단은 *push* 단계뿐이었다.
  - escape 는 `PM_ALLOW_PROTECTED_COMMIT=1`(예: `PM_ALLOW_PROTECTED_COMMIT=1 git commit ...`) —
    `PM_ALLOW_PROTECTED_PUSH` 와 동형 시맨틱. detached HEAD 는 통과한다.
  - **비커버**(우발 방지 가드이지 적대적 통제가 아니다): `git commit --no-verify` · merge 커밋
    (`pre-merge-commit` 소관) · rebase/cherry-pick/revert. 하드 백스톱은 pre-push 훅(라이브 게이트 포함).
  - 릴리즈 flow 는 그대로다 — 릴리즈 커밋은 release 브랜치에서 하고 `main` 은 merge 로 받으면
    escape 없이 통과한다.
  - 훅은 우리 bare 미러(`.repos/<repo>.git`)의 `core.hooksPath` client-side 가드다 — **회사
    repo 서버 ref·사용자 클론 무영향**(pre-push 훅과 같은 배선·sidecar 공용). 그 미러를 쓰지 않는
    clone(예: PM 홈 자신)은 영향 없다.
- **`pm-update` 가 매 실행마다 보호 훅 정합을 확인하고 어긋나면 다시 깐다** (T-0415) — 훅은 엔진
  코드에서 생성되는 런타임 산출물이라 **파일 복사만으론 새 훅이 배포되지 않는다**(설치 트리거가
  `repo add`·`worktree add` 뿐이라 엔진만 올린 인스턴스는 새 가드를 못 받았다). 이제 설치된 훅
  본문·보호목록 sidecar·`core.hooksPath` 배선을 현 엔진과 대조해 **정합이면 조용히 넘어가고**
  어긋나면 재설치한다(훅 디렉토리가 통째로 지워진 clone 도 자가치유). 실패는 loud 경고 + 재설치
  커맨드(update 종료코드는 불변).
  - **업그레이드 노트**: 이 버전을 흡수하는 그 `pm-update` 실행은 *갱신 전* 엔진이 수행하므로
    훅이 아직 안 깔린다. **흡수 직후 `pm-update` 를 한 번 더 실행**하면(변경이 없어도 정합
    확인이 돌아) pre-commit 가드가 배포된다. `pm-config repo add <repo>`(멱등)로도 즉시 깔린다.
- **board 의 미커밋 변경이 더 이상 무관한 티켓의 claim 을 막지 않는다** (ADR-0073·T-0419) —
  공유 board(별도 git) 형상에서 파일 하나가 uncommitted 라는 이유로 **모든** claim 이 전면
  차단됐고, 안내는 `add -A`(= 남의 미완성 편집까지 대신 커밋)였다. 조율 권위는 로컬 워킹트리의
  clean 여부가 아니라 **원격 ref 의 fast-forward push** 라는 원칙으로 재정렬했다:
  - **선점 감지가 읽기 전용**이 됐다 — `fetch` + 원격 트리 직접 조회(`ls-tree`)로 그 티켓이
    이미 claimed/done/blocked 인지 본다(로컬 변경 0·통합 성공에 의존하지 않음). claim 의
    `pull --rebase` 는 **원격이 앞섰을 때만** 시도한다. 단일 clone 다중 슬롯은 같은 로컬
    브랜치를 공유해 behind=0 이므로 잔여 차단이 구조적으로 발생하지 않는다.
  - **커밋 스코프 = 그 mutation 이 만진 경로만** — claim 과 best-effort 6곳
    (new/promote/complete/block/unclaim/unblock) 전부. 공유 워킹트리의 무관한 미커밋 작업이
    board 커밋에 실려 push 되던 누출이 닫힌다(`.gitattributes` 는 **엔진이 이번에 보강한
    경우에만** 함께 실린다 — 사용자가 편집 중인 그 파일을 대신 커밋하지 않는다).
  - **롤백이 비파괴** — `reset --hard` 폐기. `reset --soft` + 그 티켓 파일 역이동·원본 복원 +
    **index 스냅샷 복원**만 한다(무관한 미커밋 작업 불변). 스냅샷은 claim 직전 그 두 경로의
    index 항목을 그대로 뜬 것이라, 대상 티켓이 unstaged/untracked/staged 어느 상태였든 롤백 후
    `git status` 가 claim 직전과 **같다**(대상 파일만 staged 로 바뀌던 사각 폐쇄). push 가
    timeout 으로 끝나면 원격
    브랜치가 그 claim 커밋을 **포함하는지** 재확인해(fetch + 조상 판정) 이미 반영됐으면
    롤백하지 않는다(원격 claimed·로컬 open 인 고아 claim 폐쇄). 롤백 뒤엔 winner 를 로컬에
    반영하고, 미커밋 변경 때문에 못 당기면 "로컬 board 뷰 stale" 을 loud 하게 알린다.
  - **잔여 차단은 사유가 정확하다** — `원격 앞섬 ∧ 통합 불가` 일 때만 막고, 사유를 갈라
    (미커밋 파일 / rebase 충돌 / offline / upstream 미설정 / 원인 미상) `behind N` + **막고 있는
    파일 목록**(최대 5건 + 총계) + 그 경로만 커밋하는 안내를 낸다. mid-rebase 는 `rebase
    --abort/--continue` 를, detached 는 `checkout` 을 안내한다(옛 2단 오진 폐쇄). 분류는 git
    메시지 문자열이 아니라 **관측 상태**로 한다(로케일·git 버전 무관) — 특히 원격에서 들어오는
    파일과 같은 경로의 **미추적 파일**이 pull 을 막는 경우를 더 이상 offline 으로 오진하지
    않고, 그 파일을 커밋하거나 옮기라고 안내한다.
  - **board-git mutation 직렬화 락 신설** — 같은 clone 의 두 슬롯이 commit→push→rollback 을
    인터리브하던 창을 닫는다. board-git 미분리(legacy·솔로) 채택자는 **100% 무변경**.
- **보호 브랜치 훅 설치 실패가 이제 loud 하다** (T-0417) — `pm-config repo add`·`worktree add` 는
  훅 설치 결과(False)를 조용히 삼켜서, 훅이 안 걸렸는데 사용자는 걸린 줄 알았다(보호 가드 침묵
  무력화). 이제 실패 시 stderr 경고 + 재실행 커맨드가 나간다(성공 출력·종료코드는 불변 — 보호 훅은
  추가 가드라 등록/슬롯 생성을 깨지 않는다). 같은 이유로 이미 등록된 repo 에 `--protected` 를 주면
  "반영되지 않았다" 를 loud 하게 알리고 `repo protected` 로 안내한다.

## [1.4.0] - 2026-07-21

**codex CLI(OpenAI Codex 0.144.x)가 세 번째 지원 하네스로 추가**(ADR-0070) — claude_code·opencode
와 동급 풀 파리티(위임 4축·스킬·ctx 가드·relay·라이브/릴리즈 게이트). 동반해 **진입 doc 을
"얇은 harness-neutral 공통 코어 + 하네스별 네이티브 채널"로 재편**(ADR-0069). 전 변경 이중게이트
(내부 reviewer + codex 외부·실결함 10건 다라운드 수렴) + 라이브 실측(실 codex·gpt-5.5) 통과·회귀 4302.

### codex 하네스 어댑터 (ADR-0070)
- **`templates/codex/` 신설** — `pm-import.sh --new <dest> --harness codex` 로 채택, 기존 인스턴스엔
  `pm-config add-harness codex` 로 비파괴 추가(공존 조합은 add-harness 채널로 통일). 어댑터 구성:
  `.codex/agents/` 위임 4축 TOML(developer/architect/code-reviewer/researcher·`model` 생략=사용자
  config 상속)·`.agents/skills/` canonical 스킬 15종 remap 사본·`.codex/config.toml`+`hooks.json`
  (auto-compact 상향·PreCompact 핸드오프 tripwire·instance-owned)·relay driver `pm_orch_codex.py`.
- **PM = 메인 세션** — codex 전용 정적 진입 doc 없음. 공통 코어 `AGENTS.md`(자동 로드) + 부트스트랩
  커맨드 카드의 codex 절(env `CODEX_THREAD_ID`/`CODEX_CI` 기계 감지) + 스킬이 운영 지침을 전달.
- **trust 2단계 필요(codex 플랫폼 제약)** — 첫 진입 시 ① 대화형 `codex` 로 프로젝트 trust 수락
  ② `/hooks` 로 hook trust 승인. import/add-harness 가 loud 안내(`-c` CLI trust override 는 무효·실측).
- `--fill auto` codex 지원(fill runner) + 미지원 하네스 silent 폴백을 fail-loud 로 전환.

### 진입 doc 공통 코어 + 조건부 자동 마이그레이션 (ADR-0069)
- **opencode `AGENTS.md` 재편** — 자족 매뉴얼(22.6KiB)→harness-neutral 공통 코어(codex 와
  byte-parity). opencode-고유 실행모델·위임규약은 `.opencode/pm-instructions.md`(자동 로드·전파
  채널 등재 — 방법론 갱신이 채택자에 도달)로 이관.
- **기존 opencode 채택자는 pm_update 흡수 시 조건부 자동 전환** — 진입 doc/`opencode.jsonc` 가
  출하 원본과 byte-일치(미수정)면 자동 전환+백업(`.pm_import_backups/`), 수정 흔적 있으면 무손 +
  loud 안내·1회 마이그레이션 절차 제시. 재실행 멱등.
- add-harness 경로 백업 디렉토리 gitignore 위생 배선(main import 와 대칭).

### 라이브/릴리즈 게이트 codex 축
- `PM_ORCH_LIVE=1` 에 codex 축(adr/ticket 발행 flow 실 LLM 검증) + relay live smoke. **릴리즈
  livegate 수집 pin 16→17** — codex 라이브 green 없이는 release push 가 차단된다.
- relay 신뢰성: codex usage wire 정규화·ctx stop **marker 계약 이원화**(pre-turn=재전송/post-turn=
  무재전송 — 이미 실행된 turn 의 이중 실행 방지)·sandbox `workspace-write` 명시 핀·stdin 무기한
  대기 방어.

### 운영 규율
- 병렬 wave 의 내부 리뷰 게이트용 **격리 스냅샷 절차**(staged-only worktree) 표준화 — 리뷰↔dev
  편집 경합 클래스 폐쇄(pm-dev-delegate 스킬).

## [1.3.5] - 2026-07-20

task 세션 표면의 **슬롯-집합 1급화**(ADR-0068) + 엔진 업데이트-채널 견고화. adopter#0 도그푸딩
(PM 78 task 모드 실전 + 회사 채택자 흡수 실패)이 실측으로 드러낸 두 결함군을 한 릴리즈로 닫는다.
전 변경 이중게이트(내부 reviewer + codex, 여러 건 codex 다라운드 수렴) 통과·회귀 4152·사이클 e2e
릴리즈 게이트 신설.

### task 세션 슬롯-집합 1급화 (ADR-0068)
task 는 슬롯의 묶음인데 세션 lifecycle 표면들이 "세션=슬롯 1개" 문법을 상속해 묶음 처리가 끊기던
것을 불변식 4개(집합 변경=재열거·진입 전수 열거·alloc 항상-신규·지목은 실행 순간만)로 재설계.

- **부트스트랩 진입 = 보유 집합 전수 열거+검증** (T-0399) — task 모드가 보유 슬롯 전체를 행렬로
  열거하고 슬롯별 0단계 검증(기록↔live·점유·보호브랜치·stale/creating). fault 1+ = 진입 차단(전
  fault 일괄 표시+해소 커맨드). 0슬롯=no-op.
- **task alloc = 항상 신규 대여** (T-0398·BREAKING(task 축)) — `alloc --task` 의 멱등(기존 슬롯을
  신규처럼 반환하던 silent aliasing) 폐기 → 항상 idle 신규 대여(같은 repo 복수 보유 지원). 집합
  변경 연산(alloc·release·task end·add)은 결과 집합 재열거. `worktree add <repo> --task <이름>` =
  생성 직후 그 슬롯 task 명의 대여. task-명의 lease 도 reclaim/재부착 보호(tasks 장부 조인).
- **핸드오프 퇴장 = 집합 전체 두고-가기** (T-0393) — 재스냅은 보유 전 슬롯, 회귀는 변경 흔적 있는
  슬롯만(stale 슬롯 fail-loud). task 회귀 cwd 미배선(REPO 폴백 red)도 해소.
- **인계 트리거 = `--task` 앵커** (T-0394) — task 모드 핸드오프 트리거가 `/pm-bootstrap --task
  <이름>`(슬롯 자동 수령). 엔진층 task명 validator choke.
- **task 사이클 e2e = 릴리즈 게이트** (T-0400) — 생성→편입→작업→핸드오프→재개 완주를 실 엔진/실
  git 로 검증하는 기계 e2e 를 livegate 수집 pin 에 편입. "단위게이트 green·사이클 단절" 클래스를
  릴리즈마다 차단.

### 엔진 업데이트-채널 견고화
회사 채택자가 pm-update 후에도 신규 엔진 파일이 안 와 AttributeError 로 깨진 사건의 근본 해소.

- **pm_update manifest 자기치유** (T-0396·amends ADR-0032/T-0142) — self-update 가 upstream
  manifest 를 계획 기준으로 승격(2-pass 단일 실행)해 구형 로컬 manifest 라도 한 번에 신규 등재분이
  도달. self-prop `@source` 따라 flavor↔flavor 대조(클로버 차단).
- **manifest skew 탐지 + baseline 억제** (T-0395) — 로컬 manifest 가 upstream 신규 등재분을 놓친
  상태에서 upstream_rev baseline 을 최신으로 찍어 drift-lint 가 침묵하던 false-최신 차단(loud
  경고+갱신 억제).
- **엔진 버전 스탬프 정합 fail-loud** (T-0397) — 도구 사본 skew(신 도구+구 sibling)를 로드 시점에
  "엔진 사본 불일치·pm-update" 명시 에러로(baked `ENGINE_REV` 대조·부분복사도 검출).
  `engine_rev.py --bump vX.Y.Z` 로 버전 일괄 갱신·릴리즈 태그와 정합 가드.

### BREAKING
- **`alloc --task <이름>` 이 항상 신규 idle 슬롯을 대여**한다(T-0398) — 기존 멱등(같은 repo 보유 시
  기존 슬롯 반환)에 의존하던 스크립트는 거동이 바뀐다. task 축은 v1.3.x 신설이라 실사용 채택자
  영향은 없다고 본다.

## [1.3.4] - 2026-07-20

세션/task 라이프사이클 장부 정합 4결함 폐쇄 + 표면(메시지) 개선 — 전부 additive·BREAKING 없음.
adopter#0 도그푸딩(PM 78 task 모드 첫 실전)이 실측으로 발견한 것들. 전 변경 이중게이트(내부
reviewer + codex·2건은 codex must-fix 재작업 수렴) 통과·회귀 4047.

### Fixed
- **pm_handoff 종료 git 재스냅 배선** (T-0388) — 핸드오프가 "두고 간 상태"를 lease 장부에 재기록하지
  않아(도착 스냅만 잔존), 세션 중 브랜치가 바뀌면(릴리즈 등) 차기 부트스트랩 0단계가 `diverged`
  외부-개입 **오경보**로 FAIL-LOUD 하던 갭 폐쇄. 부기 완료 후 `record_git_snapshot(slot)` 호출
  (base 보존·`--done` 경로 제외·fail-soft loud).
- **사람 bind lease 의 reclaim/재부착 보호 — `Lease.bound` 마커** (T-0389) — `bind_slot` 의 pid 는
  즉사하는 bootstrap subprocess pid 라, 타 명의 `alloc` 진입 `reclaim_stale` 이 stale 오판으로 남의
  세션/task 바인딩을 회수(정체성 탈취)할 수 있었다(T-0074 가 dormant 로 박제한 cross-path 를 F2 task
  alloc 출하가 live 화·PM 78 실측). `bound: true`(additive·구 장부=false·마이그레이션 0)를 bind 가
  기록, reclaim 과 alloc branch/resume 재부착 경로가 제외. 해제는 명시 lifecycle 전이에서만.
- **부트스트랩 `--task` 동반 `--repo/--slot` = task 명의 원스텝 바인딩** (T-0390) — 종전엔 세션 명의
  bind + task 작업공간 0개라 직후 F6 이 loud 차단하고 `pm-config alloc` 별도 스텝을 요구. 이제 지정
  슬롯을 `bind_slot(session=<task명>)` 로 리스해 부트스트랩 한 줄로 작업 가능(멱등 재진입·타 명의
  점유는 거부·슬롯-only/task-only 경로 100% 불변). 카드 슬롯 번호는 슬롯 식별자에서 파생(task명
  session 오염 기계 차단).
- **task 정상-종료 기록 — 재개 crash 오탐 제거** (T-0392) — task 장부 pid 는 즉사하는 bootstrap
  subprocess pid 라, 핸드오프가 정상-종료를 기록하지 않으면 **모든 재개**가 "회수(이전 세션 crash·
  다른 창 작업중일 수 있음)" 경고로 표시됐다. 핸드오프 완료 단계가 `release_task_pid`(pid=0)를
  기록하고 재개가 clean resume 으로 분류된다 — 진짜 crash(pid>0 잔존)·산 pid 거부(2창)는 현행 유지.

### Added / Changed (표면·거동 변경 0)
- **메시지 3건** (T-0391) — ① 신규 task 첫 부트스트랩: `task 1차` + "🆕 신규 task — 복구할 인계
  없음" 사유 명시(종전 "(log/current.md 없음 또는 entry 파싱 실패)" 오독 제거) ② 0단계 diverged
  FAIL-LOUD: head 관계 판정 근거 + 정당 판단 시 재동기 커맨드 실값 제시 — `worktree_pool.py record
  <slot>` CLI 신설(`record_git_snapshot` thin 노출·자동 실행 없음·해소 주체=사용자 불변) ③ 핸드오프
  재스냅 출력: 실갱신 vs 무변경(스냅 불가·기존 유지) 구분. pm-worktree 스킬 열거에 `record` 반영.

## [1.3.3] - 2026-07-19

세션 뷰의 축을 "생성 세션" 하나로 통일하는 뷰 계층 변경 — 타 세션 티켓 정보는 기본 출력에서 완전히
사라진다(카운트 줄 포함). 전 변경 이중게이트(내부 reviewer + codex 3라운드 수렴) 통과·회귀 3996.

### BREAKING
- **`board list` 세션 기본 뷰 = 생성-세션 스트림·타 세션분 완전 비노출** (ADR-0067·ADR-0066 amend) —
  무인자 `board.py list`(및 부트스트랩 dump)와 명시 세션 뷰 `board list --repo X --slot N` 이 이제 **그
  세션이 생성한 open(`created_by` 세션 일치) + 그 세션 claim** 만 출력한다. ADR-0066 의 **"그 외 open
  N건" 접힘 카운트 줄은 제거**됐고(타 세션 정보의 기본 노출도 컨텍스트 누수로 간주), open 카운트 모수도
  내 스트림으로 좁혀졌다. 전체/타 세션은 명시 `board list --all`(경합 가시·backlog 확인·타 PM 열람) 몫.
  - **스트림 판정 = 생성 세션**(uniform·슬롯/task 공통) — ADR-0066 의 **task-prefix 스트림 판정을 폐기**
    (prefix 는 ID 라벨일 뿐). 같은 사용자의 타 슬롯 생성 open 도 명시 세션 뷰에서 비노출(PM 77 누출 fix).
  - **`board new --repo X --slot N`** 추가 — created_by 에 `<user>/<repo>_<N>` 생성-세션을 기록(claim 과
    동일 identity 해소 경로 재사용). 미명시 시 현행(user-only / 유도 세션) 유지.
  - **`--mine`(user-wide·전 세션)·`--all`·strict-exclude(타 사용자 차단) 의미론 불변.** 솔로/무바인딩
    (세션 미해소)은 user-단위(--mine) 폴백(solo=subset·N=1 이면 user 스트림=세션 스트림이라 등가).
  - **세션 뷰의 claim 판정도 session 라벨 축** — `claimed_by` 의 세션 토큰 일치로 판정(user 무관·open
    의 생성-세션 판정과 대칭). user 미해소(git email 부재) 상태에서도 자기 세션 claim 이 보인다. user
    축 조회는 `--mine` 몫.
  - **부트스트랩 dump 정합** — "그 외 open N건" 줄·open 전용 backlog 라벨(`_OPEN_SCOPE_LABEL`)·`--all`
    전량 재조회(접힘 모수)·"타 세션 진행(claimed)" 현황 줄 제거. "다른 활성 PM" 슬롯 레지스트리(환경
    정보·leases 유래)는 유지. 무인자 출력·부트스트랩 dump 를 파싱/의존하던 스크립트·문서는 `--all` 로
    갱신 필요.
  - 발단: fresh 슬롯/task 세션 첫 화면·기본 조회에 타 세션 티켓(그 세션이 안 만든 것)이 섞여 컨텍스트
    누수(사용자 재문제제기·PM 77). 유실 방지는 카운트 상시 노출이 아니라 명시 조회·핸드오프 인계로 이동.

## [1.3.2] - 2026-07-19

세션 기본 뷰를 "내 스트림"으로 좁히는 뷰 계층 변경(무관 backlog 는 카운트 접기) + 릴리즈 절차 문서
결함 수정. 전 변경 이중게이트(내부 reviewer + codex 3라운드 수렴) 통과·회귀 4004·실데이터 3표면
실출력 검증.

### BREAKING
- **`board list` 무인자 기본 뷰 = 내 스트림 스코프** (ADR-0066) — 무인자 `board.py list`(및 부트스트랩
  dump)가 이제 **내 세션 claimed + 내 task prefix 의 open** 만 상세로 보이고, 그 외 open backlog 는
  "그 외 open N건 — 전체는 `board.py list --all`" **카운트 1줄로 접는다**(유실 방지 유지·N>0 항상
  표시). 기존 무인자 전체 뷰는 신설 **`board list --all`** 로 이관 — 무인자 출력을 파싱/의존하던
  스크립트·문서는 `--all` 로 갱신 필요. `--mine`/`--task`/`--repo`+`--slot` 렌즈·strict-exclude·open
  데이터 모델(슬롯 무소속·claim 조정)은 불변. 접힘/스트림/카운트의 모수는 공유 풀 전량(소유 무관 —
  strict-exclude 는 `--mine` 렌즈 한정). solo 특례 없음(단일슬롯 솔로도 open 은 접힘).
  - 발단: fresh 슬롯/task 세션 첫 화면에 무관 backlog 가 상세로 섞여 오독·노이즈(실측). 티켓은
    사용자/스트림 소속이지 슬롯 소속이 아니므로, 기본 화면은 자기 스트림만 — 공유 backlog 의 존재는
    카운트 줄이 승계한다.

### Fixed
- **릴리즈 절차 태그 push 실패** — 릴리즈 안내(pm-release 스킬)의 태그 push 명령이 브랜치명=태그명
  컨벤션에서 refspec 모호("src refspec matches more than one")로 실패하던 것(v1.3.1 릴리즈 실측) →
  `git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z` 명시형으로 교체.

## [1.3.1] - 2026-07-19

릴리즈 브랜치명=태그명 컨벤션에서 세션 시작이 가짜 "외부 개입" 경보로 차단되던 결함 수정(브랜치명
해소 full-ref 전환·전 소비자) + `regression run --cwd` 핀의 모호 오발화 수정 + `board list` 에
board-git 최신성 1줄 표면화. 전 수정 이중게이트(내부 reviewer + codex) 통과·회귀 3982.

### Fixed
- **동명 태그+브랜치에서 세션 시작 가짜 차단** — 릴리즈가 브랜치명을 그대로 태그로 찍으면(예
  `v1.3.0` 브랜치+태그) git 브랜치명 조회(`symbolic-ref --short`)가 모호성 회피 `heads/<name>` 을
  돌려줘, 슬롯 git 기록-대-실측 비교가 "외부 개입" FAIL-LOUD 로 부트스트랩을 차단했다(매 릴리즈
  재발 구조). 브랜치명 해소를 full ref(`symbolic-ref HEAD` → `refs/heads/` 정확 제거)로 전환해
  모호성을 원천 제거 — 진짜 이름이 `heads/x` 인 브랜치도 오인하지 않는다. 같은 클래스의 잔존
  소비자(부트스트랩 freshness 표시·repo 등록 기본브랜치 해소 2곳)도 동일 패턴으로 마감.
- **`regression run --repo <r> --cwd <경로>` 모호 오발화** — readonly 슬롯 추가 등으로 repo 활성
  슬롯이 2개 이상이면 `--cwd` 로 실행 위치를 핀했는데도 actor 슬롯 특정이 모호 오류로 죽던 결함.
  v1.3.0 의 livegate `--cwd` 수정과 동형으로 폐쇄 — `--cwd` 명시 시 모호하면 조용히 repo/local
  test_cmd 폴백(명시 `--slot` 의 슬롯 test_cmd 유도는 유지). push 게이트(check 경로)는 불변.

### Added
- **`board list` board-git 최신성 표면화** — 모든 list 변형이 board-git freshness 1줄(`최신`/
  `판정불가 — 스냅샷일 수 있음`/behind ⚠)을 **stderr** 로 advisory 표기 — stale board 스냅샷을
  최신으로 오독하는 클래스의 잔여 표면 폐쇄(부트스트랩 표면화와 같은 판정·오프라인 시 "최신"
  오단정 없음). 매 호출 원격 조회 비용은 60s TTL 가드+5s 상한으로 완화 — 비-git(solo) 형상은
  표기·조회 모두 없음.

## [1.3.0] - 2026-07-19

task-단위 PM(named task 정체성·readonly 공유 슬롯·슬롯 git 엔진화) + PM 운영 명령어化(티켓 authoring·codex 게이트·ADR 발행·릴리즈) + 진실유지 기계화(verified_at sha lint·contradiction lint) + opencode 스킬 단일 소비 전환. 전 티켓 이중게이트(내부 reviewer + codex) 통과·회귀 3952·라이브 dual-harness 실측.

### BREAKING
- **부트스트랩 0단계 main-참조 슬롯 진입 거부** (T-0360) — 슬롯 HEAD 가 **보호 브랜치(main 등) 직접 checkout** 또는 **보호 브랜치 원격(origin/main 등)을 upstream 으로 추적** 상태면 `/pm-bootstrap` 0단계가 **부분 dump 도 없이 진입 거부**한다(기존 = 경고 후 통과). 정상 작업 슬롯의 feature upstream(예 `origin/<feature>`) 추적은 거부 대상이 아니다. main 직접 checkout·수동 tracking 은 `--no-track`(신규 파생 경로에만 적용)이 못 막는 무방비 구멍이었고, 방어가 pre-push 훅(T-0076) 하나뿐이라 커밋이 다 된 뒤 push 순간에야 막혔다 — 진입 시점으로 앞당겨 canonical/보호 브랜치 오염 커밋을 원천 차단한다. readonly 공유 슬롯(detached·role=readonly)은 예외(브랜치 자체가 없음).

  - **채택자 영향**: main-checkout(또는 origin-추적) 슬롯을 쓰던 채택자는 갱신 즉시 부트스트랩이 거부된다 — 부분 dump 도 안 나온다(세션 진실 오염 방지).
  - **해소 절차 (택1)** — 거부 메시지가 실값 커맨드로 안내한다:
    - (a) 코드 읽기 기준면이 필요하면 readonly 슬롯을 만든다: `pm-config worktree add <repo> --readonly`
    - (b) 이 슬롯을 작업 브랜치로 전환한다(main 직접 checkout 이탈): `git -C work/<repo>_<N> switch -c <repo>_<N>`
  - **릴리즈 절차 재배선**: 릴리즈 livegate `--cwd`·codex `--paths` 가 지목하던 "origin/main 이 도는 slot-1" 역할은 readonly 공유 슬롯(detached·origin/main 기준면)으로 이전됐다(pm-release 스킬/커맨드 §2). readonly 슬롯은 무리스라 `--slot` 로 안 잡히므로 릴리즈는 `livegate record --cwd <readonly 슬롯 절대경로>` 로 핀한다.
  - **다운스트림 lockstep** (finance/회사 등 채택자): BREAKING 이라 main-checkout 슬롯 운용은 갱신 후 거부된다. `pm_update`(엔진 흡수) 후 위 해소 절차대로 각 슬롯을 readonly 또는 작업 브랜치로 정리하고, 릴리즈 절차를 쓰는 채택자는 livegate 핀을 readonly 슬롯 `--cwd` 로 갱신한다.

- **opencode `command/*.md` PM-workflow 사본 채널 은퇴 — canonical 스킬 단일 소비** (T-0364·ADR-0065) — opencode ≥**1.17.19** 가 `.claude/skills/*/SKILL.md` 를 네이티브 스캔하고 슬래시(`run --command <스킬명>`)로 호출하므로(라이브 실측), 수기 command 사본 11종을 제거하고 opencode 템플릿도 canonical `SKILL.md` 미러(`.claude/skills`·bare @render)를 싣는다. 수기 사본 pair-pin 재-pin 수고·silent-drift 채널 소멸.
  - **채택자 영향**: opencode 1.17.19 미만은 스킬 스캔이 없어 지원하지 않는다(폴백 없음). `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` 미설정 전제. 기존 인스턴스의 `.opencode/command/*.md` 잔재는 무해하나 갱신 후 제거를 권장(엔진 lint 는 legacy-compat 로 계속 스캔).

### Added
- **task-단위 PM (named task 정체성 축)** (T-0350~T-0357) — 슬롯 축과 직교하는 사람-명명 작업 단위: `--task <이름>` 귀속(`claimed_by=<user>/<task>`·예약 패턴 `<repo>_<N>` 금지·단일 validator 깔때기)·task prefix 설정·부트스트랩 0단계 preflight(엔진 앵커/작업공간/불완전 생성/타 점유/기록-live 정합 기계 검증·실패 시 부분 dump 금지)·슬롯 기준점(base) 장부 기록(set-base)·`board list --task` 렌즈(T-0365·read 경로도 검증 깔때기).
- **readonly 공유 슬롯** (T-0358) — research/기준면 전용 detached 슬롯: `pm-config worktree add <repo> --readonly`·배타 대여 없음·mutation/lease-op 거부·`refresh`(fetch→detach 이동+submodule 재동기·dirty 거부).
- **슬롯 git 운영 엔진화** (T-0359·T-0361) — `pm-worktree status`(단일/일괄·submodule pin/drift·dirty)·`rebase`(선-검사 4종·충돌=그대로+loud·`--abort` 미호출·장부 원자 갱신)·`pm-config status` 2축 cockpit(task 축+slot 풀·branch@head·base 대비 behind).
- **PM 운영 명령어化 4종** (ADR-0049) — `/pm-ticket`(티켓 authoring: draft→5절 fill 검증→promote·placeholder/절-삭제 게이트)·`/pm-review`(codex 게이트 규율: worktree 앵커·stage 선행·경로 핀 + PM 홈 앵커 능동 차단)·`/pm-adr`(ADR 발행 원자화: 채번·lifecycle back-ref 발행시점 부기·README 색인 이동·log entry·YAML 안전 직렬화)·`/pm-release`(릴리즈 순서 고정·main push 는 승인 게이트 유지). 전 스킬 frontmatter `audience` 라벨(user-entrypoint/pm-internal) 완비(T-0370).
- **진실유지 기계화** — 현재-진실 문서 freshness 를 `verified_at: <sha>` 이후 매핑경로 커밋 유무의 이진 판정으로 교체(T-0363·date 근사 폐기·`verified-at-backfill` 1회 커맨드·sha 실존 검증) + **contradiction lint**(T-0369·ADR-0064): ADR 개정(amends/supersedes) 순간 옛 결정을 참조하는 문서의 잔여 모순 후보를 advisory 표면화(탐지 LLM DI·기본 dry·판정 사람).
- **커맨드 카드 파서-생성화** (T-0362) — 부트스트랩 카드가 공용 정의서에서 모드별(task/slot/readonly) 렌더·카드↔CLI (tool,render)급 정합 가드.

### Changed
- **보호브랜치 커밋 차단이 진입 시점으로** — 위 BREAKING(T-0360) 참조.
- **promote/발행 게이트 강화** (T-0366) — 본문 5절(목표/인터페이스/결정/DoD/참고) 존재+절별 placeholder 잔존을 authoring 게이트에서 차단(전역 lint 는 3절 불변·레거시 무영향).
- **external_review 안전화** (T-0367) — adopter#0 PM 홈 앵커에서 `--paths` 없이 실행 시 diff 추출 전 fail-loud+worktree 재지정 안내(빈-diff 사후 차단의 사전 승격).

## [1.2.4] - 2026-07-17

cwd 오실행 stray 클래스 폐쇄 patch — 단일 티켓(T-0345). 이중게이트(내부 reviewer + codex 반려 1건 재작업 수렴) 통과.

### Fixed
- **PM-홈 worktree cwd 오실행 fail-loud 가드** (T-0345) — board 쓰기-경로(mutation subcommand 전수)와 `ticket_finish` 를 PM 홈의 등록 worktree(코드 전용·board 미소유) cwd 에서 실행하면, worktree 트리에 stray 티켓/log 를 **조용히 만들던 것**을 실행 전 fail-loud(실제 PM 홈 경로 안내)로 차단. 감지 = 3중 conjunction(실-board 미소유 ∧ linked git worktree ∧ 조상 PM 홈 board 소유) — 솔로/standalone·비-git·PM 홈 cwd 는 무영향(오탐 0). 읽기 경로(list/show/lint)는 무-게이트 유지. 실측 재현 픽스처(stray 티켓·stray log·오경보) 포함.

## [1.2.3] - 2026-07-17

v1.2.2 잔여 소진 patch — "버전 N 작업 중 발견분은 버전 N 에 탑재" 규율 아래 신뢰성 감지·worktree 안전성·부트스트랩 freshness·문서 정합을 일괄 마감. 이중게이트(내부 reviewer 5 PASS + codex 반려 3건 재작업 수렴) 통과. 파일-전달 규약(v1.2.2 T-0337)의 edit-denied 에이전트 실행 가능성도 라이브 확증 완료(T-0338 — opencode `safe_write` 는 permission deny 와 무관하게 가용[tools 맵=override 실측]·claude 는 Bash 경로·갭 0).

### Added
- **opencode 32k cap-hit detector** (T-0339) — 출력 상한(32k tok) silent 절단("stop" 위장)을 호출층에서 감지: relay 출력 소비 지점에서 응답이 cap 근방(char 임계 34560 = 32000×1.2char/tok 하한×0.90·정확 토크나이저 미의존 보수 근사·오탐 0 마진)이면 loud advisory(파일-전달 규약 안내 포함)·**never-block**(emission try/except)·env `PM_OC_CAP_HIT_THRESHOLD`. claude 는 `stop_reason=max_tokens` 네이티브 노출이라 범위 밖.
- **opencode command 수기 사본 silent-drift 가드** (T-0344) — claude 스킬(`.claude/skills/*/SKILL.md`)과 opencode command 사본(`.opencode/command/*.md`·자동 전파 채널 없는 수기 적응)의 대응쌍을 pair-pin(정규화 content hash) 테스트로 잠금 — 어느 한쪽 변경·신규 스킬 사본 누락 시 red + 정합 지시(재-pin 은 테스트 파일 직접 실행으로 drop-in 출력). canonical 갱신이 사본에 조용히 누락되는 클래스 제거(full 자동 생성기 전까지의 forcing function).
- **부트스트랩 board freshness·슬롯 시대차 경고** (T-0341) — offline(fetch 실패) 시 "최신" 오단정 제거("판정불가 — 스냅샷일 수 있음" fail-soft) + 슬롯 worktree HEAD 가 base 브랜치 대비 behind N 커밋이면 identity surface 에 경고(lean/alloc/JSON 3표면·advisory·areas 등록 base 만 신뢰·기존 freshness fetch 재사용=신규 fetch 0). 다중 사용자/슬롯 공유 board 의 stale 스냅샷 오신뢰 방지.

### Fixed
- **worktree 슬롯 재생성 충돌 fail-loud** (T-0335) — remove 가 보존한 미머지 브랜치와 같은 번호 재생성이 cryptic `already exists`(rc255) + orphan-worktree 오귀인으로 죽던 것 → `SlotBranchExists` **선-검출**(base·else 경로 공통)·명확한 두 갈래 안내(정리 후 재시도 / 수동 checkout 재개).
- **create_slot `branch=` 데이터-유실 클래스 종결** (T-0343) — `-B`(create-or-reset)가 기존 브랜치를 리셋해 보존 커밋을 잃을 수 있던 것 → 기존 브랜치는 **리셋 없는 checkout** 분기(신규만 `-B`)·존재 판정은 color-safe helper(`git branch --list --format=%(refname:short)`·ambient `color.branch=always` ANSI 오염 방어·실 git 백스톱 테스트).
- **opencode 문서·카드 정합** (T-0342) — config(opencode.jsonc·plugin·agent frontmatter)는 **프로세스 시작 시 로드·캐싱**(변경=완전 재시작 필요) 을 AGENTS.md 에 명시 · read-only 에이전트 카드의 "write 16KB 거부" 문구를 실체(write/edit 전면 deny → `safe_write`·bash)로 정정(가드 read-only-aware 분리·write-capable 요구 무약화) · 옛 `.nudge` 잔재 전수 점검(cruft 0 확인).

## [1.2.2] - 2026-07-17

opencode 문제해결 + 누락-기능 수정 patch — 대용량 write/edit silent-truncation 어댑터 가드(upstream 미해결 실측·라이브 재현) + worktree 슬롯 제거 단일 원자 커맨드. 이중게이트(내부 reviewer + codex 다라운드) 통과·라이브 매트릭스 실측 포함. (계획됐던 opencode subagent 기본 background 화는 sentinel 라이브 실측에서 headless 결과+작업 유실이 확정돼 **drop** — 상세는 PM 홈 log verify entry.)

### Added
- **opencode safe-write 가드 3층** (T-0334) — ① `tool.execute.before` **deny-and-redirect**: write/edit args 16KB(기본·`PM_SAFE_WRITE_DENY_BYTES`) 초과 시 throw + 모델-facing 유도(기존 파일 재작성→`edit` 분할·신규 대형 파일→`safe_write`). ② **`safe_write` custom tool**: 8KB chunk(기본·`PM_SAFE_WRITE_CHUNK_BYTES`) 강제 create/append — root containment 커널 강제(create=`openSync "wx"`·append=`O_NOFOLLOW`·절대경로/lexical/realpath-symlink 3중 거부). ③ opencode.jsonc 모델 `limit { context, output }` 명시(1.17.19 config 검증이 둘 다 요구·미명시 시 output 32000 hardcoded fallback). 근거 실측: 무가드 237KB 단일 write = **silent 실패**(파일 미생성·tool call 미성립·finish "stop" 위장)·무툴 대형 응답 = 정확히 32000 토큰 절단. 한계 명기: tool-call JSON 자체 절단은 plugin 이 못 잡음(upstream #18108·#19604·#17471 추적).
- **대형 산출물 파일-전달 규약** (T-0337) — 모델 응답(서브에이전트 최종 보고 포함)이 출력 상한(32k 토큰)에서 finish "stop" 위장으로 **조용히 절단**되는 축(도구로 인터셉트 불가)을 규약으로 우회: 출하 서브에이전트 카드 8종(양 하네스 × researcher/developer/architect/code-reviewer)에 "보고가 ~200줄/8KB 초과 예상이면 본문은 파일로(opencode 는 `safe_write` 8KB 청크)·응답엔 절대경로+핵심 요약 ≤10줄" 절 명시. read-only 역할은 산출-아티팩트 예외로 양립. 기계 가드 40 케이스(section-slice·하네스 관용 검증).
- **opencode stall 워치독 — 무한 hang 종결** (T-0336) — `opencode run` 이 스타트업 network fetch stall(간헐·자체 회복 없음·라이브 규명: brownout 창에서 시작한 프로세스는 창이 끝나도 미회복)에 빠지면 **첫-이벤트 워치독**이 kill+재시도: 첫 json 이벤트 `PM_OC_FIRST_EVENT_TIMEOUT`(기본 90초) 무소식 → 프로세스 그룹 kill → 재시도 `PM_OC_STALL_RETRIES`(기본 2) → 소진 시 fail-loud. 적용 3표면 = relay driver(턴 fail-soft·600s 가드 보존)·`pm_import --fill auto`·release 라이브 테스트(fail-loud). provider `headerTimeout`/`chunkTimeout` 노브는 무응답-서버 결정 실험에서 무효 실측이라 미채택. 무응답 소켓 픽스처 e2e 로 실 바이너리 kill+재시도 실증.
- **`pm-config worktree remove <slot> [--force]`** (T-0333) — 슬롯 통째 제거 단일 원자 커맨드: 리스/장부 확인→dirty·활성 리스 거부(`--force`=stash 보존+**stash-후 dirty 재검사**로 submodule 내부 변경 유실 차단)→`git worktree remove`(+prune)→전용 브랜치 정리(`-d` 고정=머지 시에만 삭제·미머지 보존·공유 브랜치 스킵)→장부 삭제. **장부까지 지워 `add` 가 빈 번호 재사용** — 수동 제거→dangling 장부→번호 skip footgun 종결. 제거 3분법: 등록 슬롯=`remove` / dangling 장부=`prune-stale` / orphan worktree=사용자 `git worktree remove`. pm-env 스킬/커맨드 3표면 동기.

## [1.2.1] - 2026-07-16

v1.2.0 직후 backlog 소거 patch — 게이트 무결성·부트스트랩 오독 방지·nudge 능동화·문서 정합. 이중게이트(내부 reviewer + codex·codex 반려 2건 재작업 수렴) 통과·라이브 검증 포함.

### Fixed
- **external_review 빈-diff fail-loud** (T-0326) — 빈/공백 diff 를 외부 리뷰어 호출 전에 exit 1 로 차단(원인·조치 안내 포함). 분리 형상(adopter#0 등)에서 stale 사본 실행이 "변경 없음 통과"로 위장하던 false-green 원천 차단. dry-run 포함 무조건 fail.
- **pm_bootstrap `--branch`/`--resume` repo-가드 순서** (T-0327) — 가드를 auto-resolve 앞으로 이동, `--repo` 없는 호출이 자동바인딩 슬롯에 silent 부착되던 edge 차단(에러 문구 불변).
- **pm_bootstrap 보드 요약 open 라벨 오독 방지** (T-0331) — open 카운트 라벨을 `(공유 backlog·슬롯무관)` 으로 정정(보드 섹션+첫-turn 요약 양쪽·done/claimed 는 슬롯-스코프 유지) + **타 세션 진행(claimed) 현황 1줄** 병기(전용 무렌즈 조회·fail-soft) + pm-bootstrap 카드에 "board 숫자는 스냅샷 — 옵션 제시 전 `list --mine` 교차 확인" 지침. claimed 행 파서는 고정폭 컬럼 위치 기반(제목/tags 내용 불독·cmd_list 실행 통합 가드).
- **pm_import 치환-제외 목록 하드코딩 제거** (T-0329) — 방법론 문서 제외 집합을 치환 시점 dest 인스턴스 manifest 에서 파생(신규 방법론 문서 자동 편입·`--from` 흡수 경로 정합) + broken-manifest 폴백 floor. identity_args 로더 관용구 통일·pm_playbook 라벨 정렬 동반.

### Changed
- **graceful nudge 2단 강화** (T-0328·ADR-0037 확장) — hard-stop 직전 strong 밴드(`min(stop+3%p, nudge)` 파생·노브 추가 없음) 신설: "지금 즉시 `/pm-handoff`" 능동 유도·단계별 멱등·claude/opencode 파리티·statusline 빨강 "정지 임박" 표시.
- **nudge 주입-도달 라이브 durable 테스트** (T-0286) — on-demand(`PM_ORCH_LIVE=1`) probe 기반 시나리오 2건(claude 라이브 1회 실통과·opencode 는 upstream tool-loop hang 확증 후 skip 박제). CI 기본 skip·release pin 불변.
- **dual-harness guest 어댑터 갱신 채널 정식화** (T-0330·ADR-0058) — "엔진+host 어댑터=`pm_update` / dual-harness 로 얹은 guest 어댑터=`add-harness <harness>` 재실행(refresh·live-safe)" 를 출하 문서 5표면에 명시(guest-flavor 채택자는 기존대로 pm_update 전파·ADR-0054).

## [1.2.0] - 2026-07-16

슬롯 정체성 CLI 플래그를 **decomposed `--repo`/`--slot`** 단일 방식으로 통일한다([[ADR-0057]] supersede [[ADR-0043]]). 여러 세대에 걸쳐 누적된 `--session`(actor)·`--worktree-slot`·`--session-num` 별칭을 **BREAKING 제거**하고, 전 도구가 공용 `identity_args` 모듈로 수렴한다. T-0313 슬롯-모호 remedy 오안내가 근본 소멸. 이중게이트(내부 reviewer + codex) 통과.

### BREAKING
- **정체성 CLI 플래그 통일 → `--repo`/`--slot`** (ADR-0057) — 아래 구 별칭을 **제거**했다(back-compat 없음). 정체성 인자를 받는 전 도구(board·pm_bootstrap·pm_handoff·ticket_finish·pm_config·worktree_pool)가 공용 `identity_args` 로 수렴.

  | 구 (제거됨) | 신 (canonical) |
  |---|---|
  | `--session <repo>_<N>` (actor) | `--repo <repo> --slot <N>` |
  | `--worktree-slot work/<repo>_<N>` | `--slot <N>` (`--repo` 와 함께) |
  | `--session-num <N>` (pm_handoff 차수) | `--session-seq <N>` |
  | bare `--slot <N>` (`--repo` 없음) | `--repo <repo> --slot <N>` (단독 `--slot` 은 fail-loud) |

  - `--repo X --slot N` → 슬롯 정체성 `<repo>_<N>`. `--repo X` 단독(actor) → 그 repo 활성 슬롯이 정확히 1개면 자동 해소·≥2 또는 0 이면 fail-loud. `--slot` 단독(`--repo` 없음) → fail-loud. 인자 전무 → 기존 해소 체인(`$PM_SESSION_NAME` > 활성 슬롯 lease 1개 > `local.conf session=`)은 **불변**.
  - **free-form `--session <name>` CLI 제거** — 커스텀 세션명은 `$PM_SESSION_NAME` 환경변수(또는 `local.conf session=`)로 바인딩한다. `board.py claim` 은 이제 `--repo`/`--slot`(+ `--user`)만 받는다.
  - `--session-seq`(handoff 차수·뷰-무관)·하니스 `--session-id`(대화 연속성)는 정체성과 무관해 **유지**한다.
  - **채택자 마이그레이션**: `board.py claim --session myproj_1` → `board.py claim --repo myproj --slot 1` · `pm_handoff --session-num 19` → `--session-seq 19` · `--worktree-slot work/myproj_1` → `--repo myproj --slot 1`.
  - **다운스트림 lockstep** (finance/회사 등 채택자): BREAKING 이라 미갱신 어댑터/스크립트의 구 플래그 호출은 깨진다. `pm_update`(엔진 흡수) + `add-harness` refresh(어댑터 표면)로 흡수한 뒤 위 매핑대로 호출 표기를 갱신한다.

### Changed
- **공용 `identity_args` 모듈** (T-0322) — 정체성 인자 파싱(`add_identity_args`/discriminated `parse_identity`)과 리스 원장 읽기를 전 도구 단일 진실로 수렴(도구별 복붙 제거·DRY).
- **docs/skill/command-card + 어댑터·템플릿 전수 sweep** (T-0320) — 25개 shipped 표면(pm_role·pm_playbook·skills·CLAUDE.md·AGENTS.md·opencode command·tickets/README + templates)을 새 표기로 정합(drift-0·byte-identical). parity 가드(T-0319·28 테스트)가 도구-간 semantics 동형 + shipped old-flag 부재를 steady-state 로 잠근다(재발 시 red).

### Fixed
- **슬롯-모호 remedy 오안내 근본 소멸** (T-0313 → 통일 흡수) — handoff/ticket_finish·pm_config 의 fail-loud 가 실재하지 않는 플래그를 가리키던 오안내가, 통일된 `--repo`/`--slot` 로 근본 해소.

## [1.1.4] - 2026-07-15

채택자(v1.1.0) 버그 wave — prefix 대소문자 + 세션/슬롯 뷰 격리를 채택자 관점으로 정합. 다 실버그·v1.1.3 재현 확인. dual gate(내부 reviewer + codex) 통과.

### Fixed
- **prefix 대소문자 허용** (ADR-0055 amends ADR-0042) — 생성/rename 검증이 소문자-only 라, 보드에 대문자 prefix 티켓(`T-AAA-*`)이 존재·정상 list 되는데도 `board new --prefix AAA` 를 자기 도구가 거부하던 등록측(`_REPO_NAME_RE`)·파싱측(`_TICKET_PREFIX_RE`)과의 3중 문법 불일치를 정합. prefix 동일성 = **case-insensitive fold**(대문자 허용)·canonical(등록/최초-사용) case **보존**(저장 소문자 강제·ID 재번호 없음 → 기존 대문자 보드 무손실·마이그 0). 등록/rename/merge/delete/repo-add 는 case-only 근접중복 fail-loud. (T-0311)
- **세션/슬롯 뷰 user-first** (ADR-0056 refines ADR-0053) — 필터 뷰(`list --mine`/`--session`/`--slot`)의 querying identity 를 area_owner-derived 에서 **현재 사용자**(`user_name()`)로 고정하고, `--session`/`--slot` 을 **내 것 ∩ 그 슬롯**(claim: user AND slot)으로 좁힌다. **타 사용자는 어떤 필터 뷰에도 안 나온다**(전체는 무필터 `list` 전용). bootstrap `--slot N` 카운트가 슬롯 정체성으로 조회(라벨 "(slot N)")되고 커맨드 카드가 `--mine`(전 슬롯)/`--session`(∩ 이 슬롯) 을 구분한다. legacy 슬롯-only claim 은 진짜 solo(distinct ticket-user AND distinct area_owner 둘 다 ≤1)에서만 slot 매칭 포함(multi-user 는 strict-exclude·`migrate-identity` backfill). 채택자 실측 S1(bootstrap 카운트)·S2(claim 가시성)·S3(필터 축) 종결. (T-0312)

## [1.1.3] - 2026-07-14

multi-PM 다중사용자 격리 robustness 완결 — 값-연결(격리·전파·표기·livegate) 근본 재설계 + 라이브 게이트. 다중사용자 공유 board 에서 타 사용자 미claim 티켓이 세션 뷰에 유출되던 격리 깨짐을 근본 fix 하고, 어댑터 safety-훅이 조용히 낡던 전파 갭을 닫았다.

### Added
- **멀티유저 세션 뷰 격리** (ADR-0053) — 다중사용자 공유 board 에서 세션 뷰(`list --mine`/`--session`/`--slot`)가 타 user 미claim open 을 열람하지 않는다(소유 = area_owner ?? created_by · `_distinct_ticket_users`≥2 strict-exclude · solo 는 all-open degrade 보존). 단일 predicate `_ticket_is_mine`. (T-0302 core · T-0304 기계 격리 게이트[각 슬롯 실 생성→뷰 섞임 검증])
- **라이브 멀티유저 composite 게이트** — release-marked opencode 라이브 테스트가 2 user(alice/bob) 공유 board 에서 각자 티켓 생성 후 세션 뷰 섞임 격리를 실증(비-공허 가드). (T-0309)
- **어댑터 hook/driver 전파** (ADR-0032 Q3 · ADR-0054) — engine-mirror 훅/드라이버(ctx-guard·hard-stop·statusline·회귀 게이트·relay 드라이버)를 `@source` source-remap 채널로 framework-owned 전파 + manifest 자기전파(신 엔트리가 기존 채택자에 도달). 엔진 safety-훅 fix 가 채택자에 닿는 채널을 신설(frozen 근절). (T-0303 core · T-0305)
- **anti-degrade 진단 surface** — `board list` 가 다중사용자 strict-exclude/정체성 미해소 시 stderr loud-warn(remedy 포함·stdout 무오염) · `pm-config status` 가 정체성·isolation posture(registry 기준 · 실 격리는 `board list --mine` 이 authoritative). (T-0307)
- **fresh opencode 채택자 drift-0 e2e** — pm_import↔pm_update 렌더 drift-0(byte-identical) + hook/driver 채택자 도달을 machine e2e 로 박제. (T-0308)

### Fixed
- **opencode self-update @render 비대칭 근본fix** — opencode 없이 import 한 채택자(모델 미해소)의 self-update(`pm_update`)가 `@source` 재렌더에서 미해소 `{{OPENCODE_PRO_MODEL}}` 을 leak 으로 rc-fail 시켜 **엔진 update 까지 전멸**하던 회귀(위 hook/driver `@source` 전파가 유입)를 근본 fix. 줄-중화 로직을 단일 진실(`pm_render.neutralize_model_todo`)로 추출해 import↔self-update 대칭화 — 미해소 모델은 graceful TODO placeholder 로 넘겨(부분-graceful·엔진/타 어댑터 정상 update) self-update 가 성공한다. 렌더러 로드 실패는 fail-loud, `opencode_pro_model=` 빈값(오설정)은 leak 으로 표면화(false-green 근절). 릴리즈 라이브 게이트가 포착한 blocker. (T-0310)
- **livegate check↔record 단일소스** — `livegate check` 도 record 와 동일한 engine-root sidecar 해소를 공유해, 어느 board.py 사본/cwd 로 check 하든 push 보호훅이 기록한 파일을 읽는다. wrong-copy stale 오독(false-green/false-red)을 원천 차단. (T-0306)
- **settings.json auto-compact 토글 중복** — 정본 top-level `autoCompactEnabled` 로 단일화(env `DISABLE_AUTO_COMPACT` 중복 제거) + 출하 template critical env 존재를 검증하는 guard 테스트(권한-승인 재직렬화 드롭 fail-loud). (T-0300)
- **livegate cwd fail-loud + slot-key 표기 정합** — 다중슬롯에서 livegate cwd 해소 모호를 fail-loud, slot-key 표기 sweep. (T-0298 · T-0299)

## [1.1.2] - 2026-07-14

worktree add 타임아웃 false-kill 제거(3-layer) + worktree/lease 견고성(중단-안전·정합) + board submodule 자동 셋업.

### Added
- **`pm-import --new --board-submodule --board-remote <url>`** — `--new` 의 board(tickets+areas)를
  별도 git submodule(`.project_manager/board`)로 자동 셋업한다(두-git 분리·multi-PM 공유 board·ADR-0033).
  빈 remote 는 tickets 폴더구조+areas.md 를 seed(commit+push)하고, 기존 board remote 는 재사용(합류)하며,
  `submodule.<path>.ignore=all` 을 설정한다. 잘못된(비-board) remote 는 명확히 fail-loud. inline 기본은
  완전 무변경. (T-0297)
- **worktree add 타임아웃 튜닝 노브** — 엔진 `PM_GIT_TIMEOUT`(초·`none`=무제한)·claude 하네스
  `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS`·opencode 하네스
  `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` 를 노출한다(기본 30분·`/pm-env` 문서화). (T-0292·T-0293)

### Fixed
- **worktree add false-kill (타임아웃 3-layer)** — 대형 repo 의 `worktree add`(로컬 bare→full checkout·
  느린 디스크/VPN/Windows)가 *진행 중인데도* 짧은 고정 타임아웃(120초)에 죽던 것을 고쳤다. 엔진은
  console-visible 러너 + 관대한 튜닝 가능 타임아웃으로, 그걸 호출하는 하네스(claude·opencode) bash 툴
  타임아웃도 30분으로 상향해 3층 모두 정상-느린 op 을 false-kill 하지 않게 했다. (T-0292·T-0293)
- **부분·깨진 bare mirror 의 조용한 통과** — `repo add`/`worktree add` 가 bare mirror 를 *경로 존재*로만
  판정해, 중단된 clone(하네스 타임아웃·Ctrl-C)이 남긴 부분/빈 bare 를 조용히 재사용하고 나중 `worktree add`
  가 날 git 에러로 죽던 것을, 실 bare 검증(`rev-parse --is-bare-repository` + HEAD 해소)으로 fail-loud
  진단하게 고쳤다(파괴적 자동삭제 없음). (T-0294)
- **create_slot 중단 시 orphan + status 미탐지 + 번호 충돌** — worktree 생성이 외부 중단(Ctrl-C/kill)되면
  장부에 없는 orphan worktree 가 남고 `status` 가 이를 못 보며 다음 슬롯 생성이 번호 충돌하던 것을 —
  provisional lease(중단-안전) + git↔장부 reconcile(orphan/stale/incomplete surface·조회 전용) + 안전
  cleanup(`worktree prune-stale`)로 고쳤다. (T-0295)
- **worktree add → 슬롯 바인딩 안내 누락** — `worktree add` 성공 출력이 다음 필수 스텝
  (`/pm-bootstrap <repo> --slot <N>` 바인딩)으로 이어주지 않던 것을 안내 한 줄로 보강했다. (T-0296)

## [1.1.1] - 2026-07-13

라이브 도그푸딩·채택자 실사용에서 드러난 출하 결함 수정 (버그 wave).

### Fixed
- **opencode 서브에이전트 보고 절단** — opencode 전역 `tool_output` 기본 상한(2000줄/50KB)이
  서브에이전트(task) 반환도 절단해, researcher/reviewer 의 큰 보고가 오케스트레이터에 온전히
  도달하지 못하던 것을 상한 상향으로 해소했다. (T-0289)
- **출하 스킬/커맨드 Windows 표기 파리티** — PM 스킬(claude)·커맨드(opencode)에 Windows 런처
  `py`·`.cmd` 파사드 표기를 통일했다. Windows 세션이 literal `python3`/`.sh` 를 그대로 실행해
  가짜 shim 실패·재시도로 시간을 낭비하던 것을 막는다. (T-0288)
- **livegate record 기록 위치 seam** — 두-git(홈 + worktree) 토폴로지에서 `livegate record` 가
  push 보호훅의 read 위치와 다른 곳에 기록될 수 있어 거짓 성공을 찍고 push 순간에야 드러나던 것을,
  기록 위치를 훅 read 위치와 단일-소스로 정렬하고 불일치 시 fail-loud 로 거부하도록 고쳤다.
  단일-repo 채택자는 무변경. (T-0287)
- **릴리즈 절차 GitHub Release 단계 강제화** — 릴리즈 절차에서 GitHub Release 생성을 필수 단계로
  승격하고 완결 확인 단계를 추가했다. 태그만 push 되고 Release 객체가 누락되던 것을 막는다. (T-0290)
- **공유 채택 폴더의 다중 사용자 repo hydrate** — 하나의 채택 폴더를 여러 사람이 clone 해 쓸 때,
  레지스트리(`areas.md` · git-tracked · 공유)엔 repo 가 등록돼 있으나 bare mirror(`.repos/` · gitignore ·
  per-clone)가 없어 2번째 사용자가 repo 를 받지도 추가하지도 못하던 것을 고쳤다. `pm-config repo add
  <repo>` 가 `--git` 없이도 `areas.md` 에 기록된 URL 로 mirror 를 hydrate 하고(불일치 시 등록 URL 우선),
  `worktree add` 의 mirror-부재 에러가 그 해법을 안내한다. (T-0291)

## [1.1.0] - 2026-07-13

dual-harness(claude·opencode 병행) 채택 지원, worktree/submodule 풀 관리 도구,
스킬-우선 PM 운영 규율(ADR-0052), 그리고 라이브 도그푸딩에서 발견한 출하 버그 수정.

### Added
- **dual-harness 채택** (`pm_config add-harness <claude|opencode>`) — 이미 채택한 인스턴스에 두 번째
  하네스 어댑터를 나란히 추가한다. claude·opencode 를 한 인스턴스에서 병행 운영하고(엔진 공유·어댑터층만
  분기), imported 인스턴스는 소스를 upstream fallback + `--from` 으로 해소한다. (T-0269/0270/0271/0282)
- **worktree/submodule 풀 관리** — `pm-worktree` 스킬 + `worktree_pool.py`(`dev`/`sync` 서브커맨드):
  pool 의 submodule 을 selective 재동기하고, 작업 중 submodule 을 dev 브랜치로 지정해 재동기로부터
  보호하며, drift 난 detached submodule 을 pin 으로 수동 재동기한다. 부트스트랩이 슬롯 브랜치·upstream·
  submodule status 를 surface 한다. (ADR-0049/0050/0051·T-0275/0276/0277/0278)
- 릴리즈 라이브 게이트에 worktree/dual-harness 시나리오를 반영하고 케이스 수집 pin 을 cascade 했다. (T-0278)

### Changed
- **스킬-우선 PM 운영 규율** (ADR-0052) — PM 운영단계(claim/finish/qa/dev-delegate/handoff)는 스킬로
  invoke 하고 backbone CLI 직접 우회를 금지한다. `pm_role` 에 규율을 명문화하고, 부트스트랩 커맨드 카드가
  스킬-우선을 반영하며, durable 회귀 가드로 못박았다. (T-0279/0280/0281)
- 용어 정합 sweep — 잔여 표현을 표준으로 통일했다. (T-0268)

### Fixed
- **opencode ctx-guard 플러그인 로드** — 플러그인 export 를 함수로 교정(ESM shim + `lib/` core 분리)해
  실 opencode 세션에서 정상 로드되도록 했다(이전엔 유닛만 green·라이브 세션에선 로드된 적 없음). (T-0283)
- **부트스트랩 fresh-slot self-sufficiency** — 새 슬롯 부트스트랩 출력의 스크램블 placeholder 를 제거해
  첫 세션이 자족적으로 시작하도록 했다. (T-0284)
- **ticket_finish 두-git seam** — 다중슬롯에서 회귀 cwd 해소가 모호하던 것을 `--session`/`--no-pytest`
  로 해소했다(ADR-0027). (T-0285)
- **worktree/repo origin-freshness** 2건 — 슬롯·repo 의 upstream 신선도 판정 버그를 고쳤다. (T-0273/0274)

## [1.0.6] - 2026-07-10

세션 정체성 인자의 canonical 통일, 멀티-PM 차수·워크스페이스의 슬롯별 격리,
부트스트랩 커맨드 카드, 채택자 진입문서·어댑터 정합 수정.

### Added
- **`board.py reid <OLD-ID> <NEW-ID>`** — 오발행 티켓의 ID(번호·prefix 부여/변경/제거)를 무손실
  재부여한다. 파일명·frontmatter 는 물론 전 참조(`depends_on`/`blocks`·본문 wikilink·slug
  파일명·`wiki`/`log`)를 토큰 단위로 정확히 rewrite 한다. collision 시 중단, `--dry-run` 미리보기,
  board-git 백업, 홈 git clean 요구, 다른 세션이 claim 중이면 중단, 멱등.
- **부트스트랩 커맨드 카드** — 부트스트랩이 이 세션이 쓸 커맨드를 정체성 실값으로 채운 완성형
  카드로 dump 한다(남는 자리는 사용자가 넣을 `T-NNNN`·`<PFX>` 같은 값뿐). 숨은 전제(claim 은
  promote 선행 · prefix 조작은 홈 git clean · livegate record 는 케이스 수 pin · migrate-identity 는
  단일 세션) 경고와 '정체성이 필요 없는 커맨드' 목록, '상황→소스' 포인터를 담아 `--help` 없이
  바로 칠 수 있다.
- **`pm_handoff --normalize-session-anchors [--dry-run]`** — `pm_state` 의 차수 앵커 오형식(`N차차`)을
  `N차` 로 정규화하는 멱등·비파괴 유지보수 도구. 파서를 관대하게 만드는 대신 데이터를 원천에서
  정규화한다.
- **멀티-PM slot 대시보드** `wiki/log/dashboard.md` — 핸드오프가 자기 섹션(키=세션 정체성)만
  overwrite 하고(3~5줄 상한·다른 섹션은 byte 불변·append 아님), 부트스트랩이 '다른 활성 PM' 섹션을
  가볍게 dump 한다. 런타임 파생물이라 gitignore 되고 출하물에 포함되지 않는다. 솔로면 건너뛴다.
- 릴리즈 라이브 게이트(`PM_ORCH_LIVE_RELEASE=1`)에 **커맨드 카드 기반 사용성 시나리오** 2건 추가 —
  실 LLM 이 카드만 보고 첫 시도에 커맨드를 성공시키는지 두 하네스에서 확인한다(라이브 케이스 7→9).

### Changed
- **세션 정체성 인자를 canonical 하나로 통일** — 정체성을 받는 커맨드가 `--session <repo>_<N>`
  (정체성) · `--session-seq N`(차수) 표기로 일원화됐다. `pm_handoff` 에 `--session`·`--session-seq` 를
  신설했고, 솔로(미지정) 경로는 동작이 바뀌지 않는다. canonical 과 구형 alias 를 함께 주고 값이
  다르면 명확히 실패한다.
- **차수(PM N차)를 전역 카운터에서 슬롯별 시퀀스로 격리** — 핸드오프 로그 헤더에 정체성 태그
  `PM N차 (<repo>_<N>)` 가 붙고(솔로는 태그 생략 = 기존 헤더와 byte 호환), 부트스트랩이 자기 슬롯
  태그 entry 만 필터해 차수·인계 본문·`pm_state`·reattach 를 복원한다. 멀티-PM 두 슬롯이 같은
  N차 를 주장하던 문제가 사라진다. 식별 불가 시 기존 전역 동작을 보존한다.
- **멀티-PM 기본 규율을 '자기 공간 우선' 으로 확정** — 자기 티켓은 `board list --mine`, 상태는
  per-slot `pm_state`, 인계는 자기 슬롯 태그 handoff entry 로 운영하고, 다른 PM 과는 대시보드로만
  공유한다.
- **`pm_role.md` 축약** — 커맨드 표기를 부트스트랩 카드에 위임하고, 필독 문서를 `CLAUDE.md` +
  per-slot `pm_state` + `/pm-bootstrap` dump 셋으로 줄였다(`status`·`architecture` 는 '필요시 조회').
  '찾아가는 법' 절을 신설했다.
- opencode 라이브 모델 예시를 `ollama/glm-5.2:cloud` 로 교체 — `pm_import` 의 seed 주석과
  `--opencode-model` 도움말 예시 문자열.

### Deprecated
- `pm_handoff --worktree-slot` → **`--session`**. 구형 플래그는 무기한 alias 로 계속 수용된다(기존
  스크립트 무파손). canonical 과 값이 다르면 명확히 실패한다.
- `pm_handoff --session-num` → **`--session-seq`**. 무기한 alias 로 수용, 불일치 시 실패. 차수 인자를
  rename 한 것은 정체성 `--session` 과의 명명 충돌을 피하기 위함이다.

### Fixed
- `pm_bootstrap` 이 `/pm-bootstrap <repo> --slot N` 의 positional `<repo>` 를 수용한다 — 핸드오프가
  찍어주던 커맨드와 raw CLI 의 불일치를 수리했다(`--repo` 와 alias·둘 다 주면 값 일치 필수·
  무인자 자동바인딩은 그대로).
- 진입문서와 어댑터 카드·위임 스킬의 세션명 지시를 canonical `<repo>_<N>`(솔로는 `--session` 생략)로
  정합 — opencode 의 하드코딩 `--session pm`·산문형 `` `pm` 세션 `` 과 claude 진입문서의 자유형
  `--session session-B` 를 제거했다. 하드코딩 세션명은 repo 유도를 조용히 건너뛰게 만든다. 재유입은
  표기 형태(인자형·산문형·괄호형)에 무관하게 한 규칙으로 막는 테스트로 봉인했다.
- 진입문서와 티켓 안내의 세션 식별 우선순위 서술을 실제 코드 동작에 맞췄다 — 없어진 `<hostname>-<pid>`
  정체성 폴백을 빼고, 활성 슬롯이 하나면 그 세션으로 해소하는 단계와 슬롯이 여럿이면 저장값을 건너뛰어
  오귀속을 막는 규칙을 반영했다.
- `board.py list` 와 보드 렌더가 숫자 태그(`tags: [2026, cleanup]`)에 크래시하던 것을 고쳤다 — 태그를
  문자열로 안전하게 처리한다. `--tag` 필터도 숫자 태그를 매치한다.
- board CLI `--help` 위생 — ticket 인자 metavar 를 `T-NNNN` 으로 표기하고, `new --prefix` 도움말을
  작업 카테고리 재정의에 맞게 갱신했다. `list --session`(뷰 렌즈)이 쓰기 주체 `--session` 과 별개라는
  주의문도 다시 썼다. 핸들러 동작은 바뀌지 않았다.
- `pm-config init`/`update` 의 usage 줄이 내부 파일명(`board.py`·`pm_update.py`) 대신 실제 커맨드명
  (`pm-config`)으로 표기된다 — 에이전트가 칠 커맨드를 오인하지 않게.
- 채택자 진입문서·스킬 정합 — pm-regression 스킬이 존재하지 않는 커맨드(done→open 복구)를 안내하던
  것을 정직하게 표기하고, pm-wave-claim 의 필수 섹션을 6→3(목표·완료 조건·참고)으로 정정했다.
  ADOPT 하네스 기본값을 `claude` 로 명시하고, 하네스별 ctx 예산 키를 진입문서에 반영했다.
- opencode 어댑터 정합 — researcher 출하 파일 말미의 스트레이 태그 2줄 제거, README 의 서브에이전트
  개수 undercount 정정, 인스턴스 소유 루트 `.gitignore` 신설(claude 파리티). opencode 의 `--opencode-model`
  예시와 `spike-new` 커맨드의 설계 스파이크 생애주기(초안 편집 → 봉인 → 이후 불변) 서술을 claude 쪽과
  맞췄다 — opencode 채택자가 옛 모델 예시·옛 봉인 모델을 받던 것을 정정했다.

## [1.0.5] - 2026-07-07

하네스별 ctx 예산 분리, 티켓 prefix 의 작업-카테고리 재정의와 관리 도구, 채택자 제보 결함 수정.

### Added
- **`board.py prefix` 관리 도구** — `list`(현황: prefix 별 개수·번호 범위) ·
  `rename <A|none> <B|none>`(카테고리 개명·이름 씌우기/지우기) · `strip <A>`(=rename A none) ·
  `merge <A> [B...] --into <T|none> [--reorder-chronological]`(created 순 통합·기본 append) ·
  `delete <A>`(빈 prefix 등록 제거). 전 동사 `--dry-run` 규모 미리보기, 참조 rewrite 는
  전 표기형(frontmatter·wikilink·본문·파일명) 토큰 단위 정확 치환, collision 시 중단,
  board-git 백업 커밋, 티켓 물리 삭제 없음(무손실 relabel). 혼재 보드(legacy `T-NNNN` +
  prefixed)를 시간순으로 합칠 수 있다.
- ctx 예산 **하네스별 오버라이드 키** — `ctx_window_tokens_claude` / `ctx_window_tokens_opencode`.
  한 repo 를 claude·opencode 로 동시 운용할 때 하네스별로 다른 예산을 준다. 미설정 시
  generic `ctx_window_tokens` → 200000 순으로 해소된다.
- `pm_log.py archive --keep-last N` — 날짜 대신 개수 기준으로 최근 N entry 만 남기고 봉인.
- prefix 사용 가이드(`pm_role.md`) — 언제 prefix(배타 카테고리)/tag(겹침 속성)/none(기본)을
  쓰는지, 남발 방지 수칙, 어댑터 마이그레이션 절차.

### Changed
- **티켓 prefix 의 의미를 재정의** — repo 네임스페이스 전용에서 **작업 카테고리**(M 무관·
  자유 입력·티켓당 1개)로. `repo add` 의 repo 명 prefix 자동 시드를 폐지하고, 명시
  `--prefix` 의 "등록값 강제"를 제거했다(형식 sanity `^[a-z0-9][a-z0-9_]*$` 와 예약어
  `none` 거부만 유지).
- **ctx 정지/넛지의 분모를 해소된 예산 하나로 통일** — claude statusLine(물리 window %
  표시 폐기)·claude hook·opencode plugin(`modelLimit()` 물리한도 조회 폐기)이 전부 같은
  예산을 쓴다. 표시와 정지가 같은 숫자로 움직인다. 큰 window 는 예산 키를 명시한다.
- domain 스캔이 frontmatter 없는 `.md`(tmp·메모)를 개별 경고 없이 조용히 건너뛰고
  디렉토리별 개수 요약 1줄만 남긴다(malformed 는 개별 경고 유지).

### Fixed
- 채택자 제보 결함 — `external_review.py`/`ticket_finish.py` 의 repo 루트 하드코딩
  (`.project_manager` 마커 상향 탐색으로 교체·venv 부재 폴백 명문화) · `pm_handoff` step3
  앵커 정확-일치 실패 시 핸드오프 전체가 죽던 것(정규화 부분일치 + fail-soft 로 완주) ·
  prefixed 티켓 ID lint 정합(회귀-lock).
- non-UTF-8 파일이 domain 스캔·참조 rewrite 를 크래시시키던 경로(graceful skip + 경고).
- relabel(대량 ID 변경)의 동시성 — 스캔·충돌 검사·적용 전체를 board 락 안 fresh snapshot
  으로 직렬화하고, 적용 직전 대상 경로 점유를 재검증해 덮어쓰기를 원천 차단.

## [1.0.4] - 2026-07-03

세션 정체성 유도 전환과 사람-친화 문서 개편.

### Added
- CHANGELOG(이 파일)와 GitHub Releases — 릴리즈 절차에 노트 단계가 포함된다(v1.0.0~1.0.3 소급).
- MIT 라이선스.
- README 전면 개편 — 문제(compaction)와 해결을 앞세운 사람-친화 구성, 절별 프롬프트 예시,
  Mermaid 다이어그램. 기계 절차 reference 는 `docs/` 4파일로 분리.

### Changed
- **세션 정체성·티켓 prefix 를 저장값에서 유도값으로 전환** — `local.conf` 의
  `session=`/`prefix=` 는 solo 전용 legacy 로 강등. 활성 슬롯이 정확히 1개면 그 세션으로
  자동 유도되고, 여러 개면(멀티 홈) 명시 없는 귀속 조작이 명확히 실패한다(silent 오귀속
  차단). 멀티 홈은 두 키를 제거해도(남아도 무시) 동작이 같다.
- 멀티 홈의 push 회귀 게이트가 **전 활성 슬롯 all-or-nothing** 으로 동작한다 — 기록 확인
  우선(저비용)·미검증 슬롯만 실행·하나라도 red 면 차단. 게이트 좁히기는 CLI `--session`
  명시로만 가능하고 환경변수로는 좁혀지지 않는다.
- 티켓 prefix 는 areas.md 의 repo 등록이 단일 진실 — 등록 repo 가 1개면 자동 적용되고,
  여러 개면 세션에서 유도하며, 모호하면 발행이 명확히 실패한다.
- Windows PowerShell 안내 정합 — `.\pm-config.cmd`/`.\pm-update.cmd` 진입을 문서에 표기하고,
  PowerShell 5.x 의 `&&` 체이닝 미지원(ParseError) 주의를 진입 문서에 추가.

### Fixed
- 멀티 슬롯 홈에서 비바인딩 세션이 `local.conf` 의 세션명을 물려받아 남의 세션으로
  자기 식별하던 문제.
- 미바인딩 상태의 `repo add` 가 등록 owner 를 문자열 "None" 으로 기록할 수 있던 경로 —
  부작용 전에 명확히 중단한다.

## [1.0.3] - 2026-07-03

게이트 하드닝 + 릴리즈 단일 라이브 게이트(기계 강제).

### Added
- `board.py livegate record` / `check` — 릴리즈 라이브 테스트 wave 를 실측하면 특정 엔진
  rev 에 pin 된 기계 검증 green 마커가 기록되고, 보호 브랜치 pre-push 훅이 릴리즈 push 전에
  이를 소비한다.
- 부트스트랩이 git freshness 를 surface — fetch 해서 upstream 대비 얼마나 뒤처졌는지 보고하고
  안전한 fast-forward 동기를 안내한다. fresh-clone 연속성 포함.

### Changed
- 라이브 테스트를 릴리즈 단일 게이트로 통합 — 별도 "shipping" tier 는 폐지. 보호 브랜치 push 는
  push 대상 rev 에 기록된 라이브 green 을 요구한다(라이브-무관/핫픽스 변경만 문서화된 우회 허용).
- 핸드오프가 더 이상 차단형 shipping 테스트를 돌리지 않는다 — 비차단 1줄 안내로 대체되어
  핸드오프가 다시 빨라졌다.
- 렌더된 어댑터 파일이 머신-불변이 됐다(인터프리터·테스트 명령 placeholder 중립화) —
  clone 간 재렌더가 파일을 더 이상 뒤흔들지 않는다.

### Fixed
- `pm_render` 가 누락된 `local.conf` 키를 빈 문자열로 조용히 치환하지 않는다 — 명확히
  실패하거나 경고한다.
- 수집된 테스트가 0개일 때 회귀 실행이 false pass 를 기록하지 않는다.

## [1.0.2] - 2026-07-02

Windows 전체 지원.

### Added
- Windows 지원 — 프레임워크가 Windows(네이티브·Git Bash)에서 동작한다: 인터프리터 해소가
  `py` 런처를 우선하고, UTF-8/cp949 인코딩을 엔진 코드가 처리하며, 셸 facade 와 git 훅이
  정확히 실행된다.

### Changed
- ticket claim 이 Windows 에서도 직렬화된다 — 동시 세션 간 claim 배타성 보존.
- context-guard 임계를 상향해 hard stop 전 여유를 늘렸다.

### Fixed
- board git-sync 가 detached HEAD 를 오프라인으로 오진하지 않고, orphan 커밋을 조용히
  누적하지 않는다.
- hard-stop 이 핸드오프 자체를 잠그지 않는다.
- Windows 경로 처리 정규화(render·update 의 백슬래시 경로).

## [1.0.1] - 2026-07-01

### Added
- graceful handoff nudge — 컨텍스트 예산 nudge 임계에서 비차단 안내가 현재 단계 마무리와
  핸드오프를 유도한다(hard stop 전·Claude Code + opencode).

### Changed
- hard-stop 이 새 작업만 정지한다 — 핸드오프 도구는 예외 통과라 세션이 항상 깨끗하게
  인계할 수 있다.
- 엔진 freshness 는 git rev-baseline 단일 추적 — 중복 버전-파일 마커와
  `pm_update --version` 플래그 제거.

### Fixed
- `board init` / `pm-config init` 재실행이 `local.conf` 를 덮어쓰지 않는다 — 사용자·운영
  설정을 비파괴 병합한다.
- multi-PM 핸드오프 프롬프트에 대상 슬롯이 포함된다 — 다음 세션이 모호함 없이 worktree 를
  해소한다.

## [1.0.0] - 2026-06-28

첫 안정 릴리즈.

### Added
- PM 오케스트레이션 프레임워크: ticket 보드 + wiki 지식 베이스 + ADR(Architecture Decision
  Records) + 살아있는 domain 지식 레이어.
- 멀티-하니스 지원 — Claude Code·opencode 어댑터가 하나의 엔진을 공유.
- 멀티-PM 운용: 여러 저장소 × 여러 PM 세션, worktree 풀 + 슬롯 lease 기반.
- self-sufficient 부트스트랩·핸드오프 — 새 세션이 자기 컨텍스트(슬롯·차수·직전 인계)를
  자동 해소해 컨텍스트 한계를 넘어 연속성이 유지된다.
- 이중 게이트 코드리뷰 + 3-tier 테스트 워크플로(단위 회귀·smoke·라이브 릴리즈 wave).
