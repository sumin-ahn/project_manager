#!/usr/bin/env bash
# PreToolUse(Agent) hook 래퍼: 드라이버 pm_orch_claude.py --delegate-channel-hook 을 인터프리터 self-resolve 로
# 실행한다. 드라이버가 판정 엔진(.project_manager/tools/delegate_channel_guard.py)을 부른다.
# stdin: Claude Code hook JSON (그대로 python 에 전달). stdout: 판정 훅 JSON 또는 없음(통과). rc 보존.
#
# 위임 채널 기계 가드: native Agent 위임(developer·code-reviewer·researcher·architect)이
# local.conf `delegate.<role>.harness` 매핑과 어긋나면 deny + pm_delegate 해결 방법을 반환한다.
# 멀티-유저/멀티-프로젝트 안전(portable-by-construction): 치환 토큰·절대경로 없음 —
# 자기 디렉토리 self-resolve + 인터프리터 python3→python 런타임 대체(ctx_stop_hook.sh 와 동일 패턴).
#
# 자기 위치나 인터프리터를 찾지 못하면 원인 한 줄과 rc 1 로 끝나고, settings.json 의
# `onFailure: "block"` 이 그 위임 호출을 막는다. 판정을 얻지 못한 그 밖의 실패는 드라이버가 막는 문구와 rc 2 로 낸다.
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1

py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || { echo "Python 실행기 없음(python3·python 을 PATH 에서 찾지 못함). 훅을 실행하지 못했다." >&2; exit 1; }

exec "$py" "$hook_dir/pm_orch_claude.py" --delegate-channel-hook
