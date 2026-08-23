#!/usr/bin/env bash
# PreToolUse(Agent) hook 래퍼: delegate_channel_guard.py 를 인터프리터 self-resolve 로 실행한다.
# stdin: Claude Code hook JSON (그대로 python 에 전달). stdout: deny 훅 JSON 또는 없음(통과). rc 보존.
#
# 위임 채널 기계 가드: native Agent 위임(developer·code-reviewer·researcher·architect)이
# local.conf `delegate.<role>.harness` 매핑과 어긋나면 deny + pm_delegate 처방을 반환한다.
# 멀티-유저/멀티-프로젝트 안전(portable-by-construction): 치환 토큰·절대경로 없음 —
# 자기 디렉토리 self-resolve + 인터프리터 python3→python 런타임 폴백(ctx_stop_hook.sh 와 동일 패턴).
#
# 인터프리터 부재 시 rc0 조용히 통과(fail-open) — 가드 고장이 정상 위임을 막지 않는다.
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0

py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || exit 0

# 엔진 파일 부재(세대 불일치·구 엔진 사본 등)면 rc0 조용 통과(fail-open) — rc≠0 은 Agent 호출
# 전면 차단으로 번역되므로, 가드 설치 결손이 정상 위임을 볼모로 잡지 않게 한다.
target="$hook_dir/../.project_manager/tools/delegate_channel_guard.py"
[ -f "$target" ] || exit 0

exec "$py" "$target"
