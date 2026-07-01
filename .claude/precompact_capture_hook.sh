#!/usr/bin/env sh
# PreCompact hook (도그푸딩 root 전용 breadcrumb) — 네이티브 auto-compact 가 수동 /pm-handoff
# 보다 먼저 터질 때 남기는 최소 신호. root(.claude)는 ctx hard-stop 훅이 없어 압축이 수동
# 핸드오프를 선점할 수 있는 유일한 net-less tree라 1줄 breadcrumb 만 남긴다(ADR-0038 D3).
# 폐기된 pm_handoff.py --trigger(T-0186)에 비의존 — inline append·항상 exit 0(압축/세션 무차단).
set -u

# 스크립트 위치(.claude/)에서 repo root 자기해소 — placeholder/cwd 무관.
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
repo_root=$(CDPATH= cd -- "$hook_dir/.." && pwd) || exit 0

log="$repo_root/.project_manager/wiki/log/current.md"
[ -f "$log" ] || exit 0   # log 부재(어댑터 미배선 등) → graceful skip

# 네이티브 압축 발생 breadcrumb 1줄 (수동 핸드오프 미완 가능 신호·blockquote 라 새 entry 아님).
# 실패해도 압축/세션 무차단.
printf '\n> ⚠ 네이티브 auto-compact 발생 — /pm-handoff 미완일 수 있음 (수동 확인 요망).\n' >> "$log" 2>/dev/null || true
exit 0
