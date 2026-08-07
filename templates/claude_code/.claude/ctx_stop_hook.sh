#!/usr/bin/env bash
# PreToolUse/UserPromptSubmit hook 래퍼: ctx_stop_hook.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code hook JSON (그대로 python 에 전달). stdout: 훅 JSON (additionalContext/없음). rc 보존.
#
# 멀티-유저/멀티-프로젝트 안전(T-0202·A안 portable-by-construction): settings.json 에 인터프리터
# 치환 토큰·절대경로를 박지 않는다 — 스크립트 위치에서 자기 디렉토리를 self-resolve 하고
# 인터프리터는 python3→python 런타임 폴백(run_tests_hook.sh 와 동일 패턴). 이 파일은 치환 토큰이
# 없어 모든 머신/프로젝트에서 byte-identical 하다.
#
# 인터프리터 부재 시 rc0 조용히 통과 — 훅은 정상 작업을 막지 않는다(엔진 자체가 python 필수라
# 부재 머신에선 어차피 프레임워크 전체가 비동작·넛지 훅만 따로 살릴 수 없음).
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0

# 인터프리터 선택 — 후보를 순회하며 *실행검증*(--version rc)으로 채택(엔진 _detect_py·T-0022 시맨틱과
# 동형·python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim(command -v 통과·
# 실행 시 Permission denied rc126)을 못 거른다. 전부 실패 시 rc0 조용 통과(훅은 정상 작업을 막지 않음).
py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || exit 0

target="$hook_dir/ctx_stop_hook.py"
if [ "${1-}" = "--git-anchor-hook" ]; then
    target="$hook_dir/pm_orch_claude.py"
fi

exec "$py" "$target" "$@"
