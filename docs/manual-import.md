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

# 2) placeholder 일괄 치환 (placeholders.md 표). pm_role.md·pm_playbook.md 는 제외 — 엔진(pm_update
#    동기화 대상)이라 {{PROJECT_NAME}} 를 리터럴로 두고 local.conf 가 해소한다(치환하면
#    다음 pm_update 때 되돌아간다). {{PY}}/{{TEST_CMD}} 는 엔진 문서·어댑터에서 폐기(T-0219 —
#    문서 표기는 python3 관례·test 명령은 local.conf test_cmd= 노브)·진입 문서 등엔 잔존.
grep -rl '{{' . --include='*.md' --include='*.json' --include='*.sh' --include='*.py' | \
  grep -vE 'wiki/pm_role\.md|wiki/pm_playbook\.md' | \
  xargs sed -i \
    -e 's|{{PROJECT_NAME}}|My Project|g' \
    -e 's|{{PROJECT_TAGLINE}}|한 줄 프로젝트 설명|g' \
    -e 's|{{PROJECT_ROOT}}|/path/to/new-project|g' \
    -e 's|{{PY}}|python3|g' \
    -e 's|{{TEST_CMD}}|python3 -m pytest tests/ -q|g' \
    -e "s|{{DATE}}|$(date +%F)|g"

# 3) 이 clone 등록 (clone 당 1회) — solo(N=1·M=1) 또는 multi-repo(N×M·ADR-0016·multi-repo.md)
python3 .project_manager/tools/board.py init                        # solo: legacy T-NNNN
#   multi-repo(M>1·prefix 네임스페이스): board.py init --prefix PAY --area "결제"   # → T-PAY-NNN

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

치환 후 남은 `{{...}}` 확인 (단, `pm_role.md` 의 `{{PROJECT_NAME}}` 는 **의도적으로 남는다** — local.conf 가 해소):

```bash
grep -rn '{{' . --include='*.md' --include='*.json' --include='*.sh' --include='*.py'
```
