#!/usr/bin/env bash
# statusLine 래퍼: ctx_statusline.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code statusLine JSON (그대로 python 에 전달). stdout: statusline 한 줄.
#
# 멀티-유저/멀티-프로젝트 안전(T-0202·A안 portable-by-construction): settings.json 에 인터프리터
# 치환 토큰·절대경로를 박지 않는다 — 스크립트 위치 self-resolve + python3→python 런타임 폴백
# (ctx_stop_hook.sh 와 동일 패턴). 치환 토큰 0·모든 머신 byte-identical.
# statusLine 은 ${CLAUDE_PROJECT_DIR} 미지원이라 settings.json 이 이 파일을 상대경로로 가리킨다
# (cwd=프로젝트루트면 동작·아니면 무표시 graceful — T-0191 노트 승계).
#
# 인터프리터 부재 시 rc0 무출력 — statusline 은 가시화일 뿐 흐름을 막지 않는다.
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

exec "$py" "$hook_dir/ctx_statusline.py" "$@"
