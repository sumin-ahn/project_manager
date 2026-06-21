#!/usr/bin/env sh
# PreCompact hook — 네이티브 압축이 수동 handoff 보다 먼저 터질 때 durable flush.
# capture/continuity 방어 폴백 (ADR-0020 pre-compact·ADR-0018 capture 와 동일 메커니즘).
# 항상 exit 0 — 압축/세션을 절대 막지 않는다 (fail-soft). 새 flush 엔진 신설 0:
# 기존 비대화 트리거 handoff 경로(pm_handoff.py --trigger)를 재사용해 log/current.md 에
# PreCompact marker 를 남긴다 (수동 handoff 가 미완일 수 있다는 durable 신호).
set -u

# 스크립트 위치(.claude/)에서 repo root 자기해소 — placeholder/cwd 무관·견고.
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
repo_root=$(CDPATH= cd -- "$hook_dir/.." && pwd) || exit 0

handoff="$repo_root/.project_manager/tools/pm_handoff.py"
[ -f "$handoff" ] || exit 0   # 엔진 부재(어댑터 미배선 등) → graceful skip

# python3 → python 폴백 (둘 다 없으면 graceful skip).
if command -v python3 >/dev/null 2>&1; then py=python3
elif command -v python >/dev/null 2>&1; then py=python
else exit 0
fi

# durable flush — 비대화 트리거 모드. 실패해도 압축/세션 무차단 (|| true · exit 0).
( cd "$repo_root" && "$py" "$handoff" --trigger --reason precompact ) >/dev/null 2>&1 || true
exit 0
