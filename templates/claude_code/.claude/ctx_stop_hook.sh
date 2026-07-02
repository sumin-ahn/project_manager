#!/usr/bin/env bash
# PreToolUse/UserPromptSubmit hook 래퍼: ctx_stop_hook.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code hook JSON (그대로 python 에 전달). stdout: 훅 JSON (deny/block/없음). rc 보존.
#
# 멀티-유저/멀티-프로젝트 안전(T-0202·A안 portable-by-construction): settings.json 에 인터프리터
# 토큰({{PY}})·절대경로를 박지 않는다 — 스크립트 위치에서 자기 디렉토리를 self-resolve 하고
# 인터프리터는 python3→python 런타임 폴백(run_tests_hook.sh 와 동일 패턴). 이 파일은 치환 토큰이
# 없어 모든 머신/프로젝트에서 byte-identical 하다.
#
# 인터프리터 부재 시 rc0 조용히 통과 — 훅은 정상 작업을 막지 않는다(엔진 자체가 python 필수라
# 부재 머신에선 어차피 프레임워크 전체가 비동작·hard-stop 만 따로 살릴 수 없음).
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0

if command -v python3 >/dev/null 2>&1; then py=python3
elif command -v python >/dev/null 2>&1; then py=python
else exit 0
fi

exec "$py" "$hook_dir/ctx_stop_hook.py" "$@"
