#!/usr/bin/env bash
# pm_config 루트 파사드 (POSIX) — thin forwarder.
#
# multi-PM 루트 셋업/관리(repo add·worktree add·status|whoami·release·update)를 deep 경로와
# 인터프리터를 매번 안 치도록, 자기 스크립트 위치를 해석해 그 경로의 pm_config.py 를
# 호출하고 모든 인자를 그대로 forward 한다. 자체 인자 파싱/검증은 0 — pm_config 이 CLI
# 계약의 단일 진실이다(서브커맨드가 추가돼도 이 파사드는 변경 불필요).
#
# 사용:  <manager>/pm-config.sh repo add <name> --git <url> --test "<cmd>"
#        <manager>/pm-config.sh worktree add <repo>
#        <manager>/pm-config.sh status | whoami
#        <manager>/pm-config.sh release <slot> [--force]
#        <manager>/pm-config.sh update [--from <upstream>]
set -eu

# 자기 디렉토리 해석 (호출 cwd 무관).
DIR="$(cd "$(dirname "$0")" && pwd)"

# 인터프리터 선택 — 후보를 순회하며 *실행+버전검증*으로 채택(엔진 _detect_py 시맨틱과
# 동형·POSIX 선호순 python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim
# (command -v 통과·실행 시 Permission denied rc126)을 못 거른다. 전부 실패 시 진단 후 python 폴백
# (기존 파사드 fail-soft 계약 유지).
# POSIX 후보에는 Windows py 런처의 shebang 간접 디스패치가 없으므로 script probe 대신 -c를 쓴다.
# (3, 11)은 engine_rev.MIN_PYTHON 미러이며 테스트가 skew 를 차단한다.
PY=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 &&
            "$_cand" --version >/dev/null 2>&1 &&
            "$_cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" \
                >/dev/null 2>&1; then
        PY="$_cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.11+ 필요 — 지원 인터프리터를 찾지 못해 python으로 폴백합니다." >&2
    PY=python
fi

# 인자 verbatim forward + exec 로 rc 전파.
exec "$PY" "$DIR/.project_manager/tools/pm_config.py" "$@"
