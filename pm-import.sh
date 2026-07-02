#!/usr/bin/env bash
# pm_import 루트 파사드 (POSIX) — thin forwarder.
#
# 외부 위치에서 프레임워크를 import 할 때 deep 경로와 인터프리터를 매번 안 치도록,
# 자기 스크립트 위치를 해석해 그 경로의 pm_import.py 를 호출하고 모든 인자를 그대로
# forward 한다. 자체 인자 파싱/검증은 0 — pm_import 이 CLI 계약의 단일 진실이다.
# (flag 가 추가돼도 이 파사드는 변경 불필요.)
#
# 사용:  <manager>/pm-import.sh --new <dest> --harness opencode
#        (--from 은 pm_import 이 manager 루트로 auto-default 하므로 생략 가능.)
set -eu

# 자기 디렉토리 해석 (호출 cwd 무관).
DIR="$(cd "$(dirname "$0")" && pwd)"

# 인터프리터 선택 — 후보를 순회하며 *실행검증*(--version rc)으로 채택(엔진 _detect_py·T-0022 시맨틱과
# 동형·POSIX 선호순 python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim
# (command -v 통과·실행 시 Permission denied rc126)을 못 거른다. 전부 실패 시 python 폴백 —
# exec 가 command-not-found/Permission denied 로 명시 에러(기존 파사드 계약 유지).
PY=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        PY="$_cand"
        break
    fi
done
[ -n "$PY" ] || PY=python

# 인자 verbatim forward + exec 로 rc 전파.
exec "$PY" "$DIR/.project_manager/tools/pm_import.py" "$@"
