#!/usr/bin/env bash
# statusLine 래퍼: ctx_statusline.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code statusLine JSON (그대로 python 에 전달). stdout: statusline 한 줄.
#
# 멀티-유저/멀티-프로젝트 안전(T-0202·A안 portable-by-construction): settings.json 에 인터프리터
# 토큰({{PY}})·절대경로를 박지 않는다 — 스크립트 위치 self-resolve + python3→python 런타임 폴백
# (run_tests_hook.sh·ctx_stop_hook.sh 와 동일 패턴). 치환 토큰 0·모든 머신 byte-identical.
# statusLine 은 ${CLAUDE_PROJECT_DIR} 미지원이라 settings.json 이 이 파일을 상대경로로 가리킨다
# (cwd=프로젝트루트면 동작·아니면 무표시 graceful — T-0191 노트 승계).
#
# 인터프리터 부재 시 rc0 무출력 — statusline 은 가시화일 뿐 흐름을 막지 않는다.
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0

if command -v python3 >/dev/null 2>&1; then py=python3
elif command -v python >/dev/null 2>&1; then py=python
else exit 0
fi

exec "$py" "$hook_dir/ctx_statusline.py" "$@"
