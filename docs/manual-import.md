# 수동 longhand — pm-import 이 자동화하는 절차

> 루트 [README](../README.md) 의 *5분 여정* 은 파사드(`pm-import.sh`) one-shot 이다. 이 문서는 그
> 파사드를 못 쓰는 환경이거나 각 단계를 직접 이해·제어하려는 경우의 **수동 경로**다 —
> pm_import 이 내부에서 하는 것과 동일하다 (예시는 claude_code 타깃):

```bash
# 1) 어댑터 트리를 새 프로젝트 루트로 복사 (dotfile 빠지지 않게 trailing dot /. 주의)
cp -r templates/claude_code/. /path/to/new-project/
cd /path/to/new-project/

# 1b) 의존성 설치 — fresh clone 은 이걸 먼저 깔아야 board.py·pytest 가 import 단계를 넘는다.
python3 -m pip install -r requirements-dev.txt   # PyYAML(런타임) + pytest(테스트)

# 2) placeholder 일괄 치환 (placeholders.md 표). 대상 = **제외 사유가 없는 모든 텍스트 파일**
#    — pm_import 의 `_should_substitute` 와 동일한 제외-판정이다(확장자 열거 아님·T-0424). 확장자를
#    나열하면(옛 `--include='*.md' '*.json' '*.sh' '*.py'`) 새 하니스가 들여온 형식(.toml·.yaml 등)이
#    조용히 치환에서 빠진다 — codex 의 `.codex/agents/*.toml` 이 `{{PROJECT_NAME}}` 을 리터럴로 출하한
#    실결함(T-0424). 제외 사유: ① 엔진 소스 `.project_manager/tools/**` ② 엔진 메타데이터
#    `.project_manager/engine.manifest` (①② 는 주석의 토큰이 *설명*이라 verbatim) ③ 방법론 문서
#    pm_role.md·pm_playbook.md (엔진 동기화 대상 — {{PROJECT_NAME}} 를 리터럴로 두고 local.conf 가
#    해소, 치환하면 다음 pm_update 때 되돌아간다) ④ 바이너리 (`grep -I` 가 텍스트 아닌 파일을 건너뜀 —
#    엔진 판정은 UTF-8 decode·`-I` 는 NUL 휴리스틱 근사) ⑤ **소비 시점이 소유한 토큰**
#    (pm_import 의 `CONSUMPTION_TIME_TOKENS`) — `wiki/pm_state.template.md`·`wiki/domain/_template.md`
#    의 `{{DATE}}`. 이 둘은 manifest 등재(pm_update 가 byte-copy)인데 설치가 날짜로 굳히면 다음
#    sync 가 토큰-form 으로 되돌려 **매 sync 진동**한다 — 날짜는 그 템플릿이 산출물을 만드는 시점
#    (`board.py init` 의 pm_state.md · worktree_pool 의 task pm_state · 사람이 스캐폴드를 복사할 때)이
#    채운다. 엔진 제외는 *토큰* 단위지만 이 두 파일엔 `{{DATE}}` 외 운영 토큰이 없어 아래처럼 파일
#    단위로 빼도 등가다. (⑤ 밖의 `{{DATE}}`— status.md·architecture.md·log/current.md 등 manifest
#    미등재 인스턴스 seed — 는 설치일로 채우는 게 맞다·아래 sed 가 그대로 처리.)
#    {{PY}}/{{TEST_CMD}} 는 엔진 문서·어댑터에서 폐기(T-0219 — 문서 표기는 python3 관례·test 명령은
#    local.conf test_cmd= 노브)·진입 문서 등엔 잔존.
grep -rlI '{{' . --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=node_modules | \
  grep -vE '^\./\.project_manager/tools/|^\./\.project_manager/engine\.manifest$|^\./\.project_manager/wiki/pm_(role|playbook)\.md$|^\./\.project_manager/wiki/pm_state\.template\.md$|^\./\.project_manager/wiki/domain/_template\.md$' | \
  xargs sed -i \
    -e 's|{{PROJECT_NAME}}|My Project|g' \
    -e 's|{{PROJECT_TAGLINE}}|한 줄 프로젝트 설명|g' \
    -e 's|{{PROJECT_ROOT}}|/path/to/new-project|g' \
    -e 's|{{PY}}|python3|g' \
    -e 's|{{TEST_CMD}}|python3 -m pytest tests/ -q|g' \
    -e "s|{{DATE}}|$(date +%F)|g"

# 3) 이 clone 등록 (clone 당 1회·areas 레지스트리 행 등록은 필수) — solo(N=1·M=1) 또는 multi-repo(N×M·ADR-0016·multi-repo.md)
python3 .project_manager/tools/board.py init                        # 무prefix 등록: T-NNNN 발행
#   multi-repo(M>1·prefix 네임스페이스): board.py init --prefix pay --area "결제" --user-ack pay   # → T-pay-NNN

# 4) board.py 동작 확인 — 첫 ticket 발행
python3 .project_manager/tools/board.py new "첫 ticket — 환경 셋업 검증" --tag infra
python3 .project_manager/tools/board.py list

# 5) free-form placeholder 직접 채우기 (sed 로 안 되는 서술 항목 — placeholders.md):
#    - {{PROJECT_CONSTRAINTS}} → 진입 문서(CLAUDE.md/AGENTS.md §프로젝트 고유 제약) — 단일 거처 (어댑터는 operational 전용)
#    - {{PROTECTED_PATHS}}·{{USER_GATE_ITEMS}} → .project_manager/wiki/pm_role.local.md (overlay)
#      — 파일 안 <!-- TODO --> 참고.

# 6) (Python 외 언어면) local.conf 의 test_cmd + ticket_finish.py / pm_*.py 의 pytest 가정 교체 (portability.md).

# 이후 프레임워크 개선 받기: ./pm-update.sh [--from <upstream-checkout>] [--dry-run]
```

새 prefix를 처음 만드는 `board.py new --prefix`, `board.py init --prefix`, `pm-config task
prefix`, `board.py prefix rename/merge`는 사용자가 승인한 대상값과 같은 `--user-ack <prefix>`가
필요하다. 이미 areas·기발행 티켓·task 장부(또는 등록 prefix 0개인 solo conf)에 있는 prefix는
대소문자 무관으로 재사용되며 별도 ack가 필요 없다.

치환 후 남은 `{{...}}` 확인 — 위 2)와 **문자 그대로 같은 파이프라인**(파일 단위 `-rlI` + 동일 제외
egrep)을 건다. 엔진 소스·engine.manifest·pm_role/pm_playbook 의 토큰은 **의도적으로 남는다**(local.conf
가 런타임 해소). 아래가 파일을 출력하면 미치환 — 상세 위치는 `grep -n '{{' <파일>` 로:

```bash
grep -rlI '{{' . --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=node_modules | \
  grep -vE '^\./\.project_manager/tools/|^\./\.project_manager/engine\.manifest$|^\./\.project_manager/wiki/pm_(role|playbook)\.md$'
```
