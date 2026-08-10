#!/usr/bin/env sh
# Claude PreCompact: sidechain/PM-home 판정 뒤 durable breadcrumb + checkpoint 골격 생성.
# settings.json은 instance-owned지만 이 스크립트는 manifest @source로 세대 정합 경로에 포함된다.
# 모든 실패/출력은 흡수해 compaction과 hook stdout 프로토콜을 막거나 오염시키지 않는다.
set -u

hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0

py=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" --version >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
[ -n "$py" ] || exit 0

# stdin의 sidechain 판정과 worktree→PM-home 해소가 끝난 뒤 Python 엔진이 breadcrumb/checkpoint를
# 같은 원자 append로 기록한다. 셸에서 현재 worktree log를 먼저 만지지 않는다.
"$py" "$hook_dir/ctx_stop_hook.py" --precompact-capture >/dev/null 2>&1 || true
exit 0
