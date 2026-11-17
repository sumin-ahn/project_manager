#!/usr/bin/env bash
# PreToolUse/UserPromptSubmit hook 래퍼: ctx_stop_hook.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code hook JSON (그대로 python 에 전달). stdout: 훅 JSON (additionalContext/없음). rc 보존.
#
# 멀티-유저/멀티-프로젝트 안전(A안 portable-by-construction): settings.json 에 인터프리터
# 치환 토큰·절대경로를 박지 않는다 — 스크립트 위치에서 자기 디렉토리를 self-resolve 하고
# 인터프리터는 python3→python 런타임 대체(ctx_statusline.sh 와 동일 패턴). 이 파일은 치환 토큰이
# 없어 모든 머신/프로젝트에서 byte-identical 하다.
#
# 자기 위치나 인터프리터를 찾지 못하면 원인 한 줄을 stderr 에 쓰고 rc 1 로 끝난다. 막을지는
# settings.json 이 정한다: 판정 훅 항목(git 명령 검사)은 `onFailure: "block"` 이라 그 도구 호출이
# 막히고, ctx 안내·사용자 프롬프트 항목은 막지 않는 오류로 보인다.
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1

# 인터프리터 선택 — 후보를 순회하며 *실행검증*(--version rc)으로 채택(엔진 _detect_py 시맨틱과
# 와 같은 구조·python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim(command -v 통과·
# 실행 시 Permission denied rc126)을 못 거른다. 전부 실패하면 위 규칙대로 rc 1.
py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || { echo "Python 실행기 없음(python3·python 을 PATH 에서 찾지 못함). 훅을 실행하지 못했다." >&2; exit 1; }

target="$hook_dir/ctx_stop_hook.py"
if [ "${1-}" = "--git-anchor-hook" ]; then
    target="$hook_dir/pm_orch_claude.py"
fi

exec "$py" "$target" "$@"
