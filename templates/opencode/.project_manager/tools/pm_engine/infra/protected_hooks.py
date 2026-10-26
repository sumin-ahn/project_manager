"""보호 훅 payload·artifact 생성·설치·배선 조회 (층: infra)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from pm_engine.domain import worktree_records
from pm_engine.infra import fs, git, pm_paths, worktree_git


# ── 보호 브랜치 pre-push 훅 (하드·회사 repo 무영향 / 라이브 게이트 승격) ────
# 훅 = `.project_manager/.local/repo-hooks/<repo>/pre-push`(프레임워크 소유·gitignore).
# bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리로 set → 슬롯 push 가 이 훅에
# 게이트된다. **회사 repo 서버/사용자 클론 무변경** — client-side·우리 bare 미러 config 1줄만.
#
# 훅은 *generic* 이다 — 보호목록은 `protected`, repo 형상별 증거 계약은
# `gate-contract` sidecar에서 읽는다. 계약 파일의 1행은 mode(release/self-test),
# 2행은 self-test 명령이며 둘은 하나의 원자 교체로 갈린다. 따라서
# 재설치 중 push가 겹쳐도 서로 다른 세대의 mode/command가 결합하지 않는다.
# 설치(install_protected_hook)가 sidecar를 채우며, 훅 본문 자체는 repo 무관하게
# 동일하다. POSIX sh — Windows git 번들 sh 로도 동작.
#
# 로직: stdin 의 `<localref> <localsha> <remoteref> <remotesha>` 줄들(=이 push 의 모든 ref)을 순회한다.
# pre-push 는 push 전체에 한 번 발화하는 all-or-nothing 게이트라 **보호 ref 를 전부 검증**하고 하나라도
# 실패하면 push 전체를 거부한다(예: `git push main release` 서 main 만 green 이어도 release 가 미검증이면
# 편승 차단). remote ref (`refs/heads/<b>`)의 `<b>` 가 sidecar 보호목록에 있으면 (그 ref 의 localsha 로) —
#   - `PM_ALLOW_PROTECTED_PUSH=1` 아니면 하드 차단(echo 안내·exit 1).
#   - `release`: push SHA의 `board.py livegate check --rev <sha>` rc0을 추가 요구.
#     `PM_SKIP_LIVE_GATE=1`은 라이브-무관·긴급 변경 한정 우회다. 단 **미해소 must-fix 잔여**는
#     그 우회의 대상이 아니다. 훅은 장부 writer가 현행화한 `release-must-fix` 한 줄 표식만 읽고,
#     `clear`가 명확할 때만 우회한다. 표식 부재·손상·읽기 실패는 잔여 미상이라 fail-closed다.
#   - `self-test`: 현재 checkout HEAD=push SHA·clean을 고정한 뒤 계약의 repo 테스트
#     명령을 실행한다. `PM_SKIP_SELF_TEST=1` + 빈 값이 아닌
#     `PM_SELF_TEST_BYPASS_REASON` 조합만 감사 로그를 남기고 우회한다.
# 계약/mode 미해소·필요 자원 부재는 fail-closed다. 보호목록을 정상 해소한 뒤의
# feature(비보호) 브랜치와 목록이 필요 없는 tag push는 통과(exit 0·PM_ALLOW/증거 게이트 무관).
#
# 외부 명령 실패 방향: pre-push의 `cat`(보호목록 I/O 건전성), 비우회 self-test의 `git`·`sh`,
# 우회 감사의 `tr`·`date`, release의 Python launcher·board.py는 모두 실패 시 거부한다.
# 우회 중 `git status`만 검증 결과가 아닌 보조 dirty telemetry라 실패를 `unknown`으로 명시 기록한다.
# pre-commit의 `git` 판정 실패도 거부한다. 경로 계산은 셸 확장만 써 dirname/basename 의존이 없다.
#
# **멱등 자가치유 배포**: install_protected_hook 이 매 호출 이 본문을 덮어쓰므로(repo add·
# worktree add), 엔진 update 후 다음 재설치가 이 신 버전을 자동 배포한다.
_PROTECTED_PRE_PUSH_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-push 가드 — PM 이 보호 브랜치(main 등)에 자율 push 못 하게 +
# 승인(PM_ALLOW_PROTECTED_PUSH=1)된 protected push 도 릴리즈 라이브 게이트 green 을 추가 요구.
# install_protected_hook() 가 설치. 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치).
case "$0" in
    */*) hook_dir=${0%/*} ;;
    *) hook_dir=. ;;
esac
protected_file="$hook_dir/protected"
engine_root_file="$hook_dir/engine-root"
gate_contract_file="$hook_dir/gate-contract"
protected_loaded=0
protected_branches="
"

# tag/non-branch ref는 이름만으로 비보호임이 확정된다. 보호목록은 첫 branch ref에서만 읽어,
# 판정에 필요 없는 sidecar 손상이 tag-only push 계약을 막지 않게 한다. branch ref는 목록을
# 해소하지 못하면 비보호로 추측하지 않고 fail-closed한다.
pm_load_protected_branches() {
    if [ ! -f "$protected_file" ]; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 찾을 수 없어 push 거부: $protected_file" >&2
        echo "  sidecar를 직접 편집하지 말고 보호목록 설정 후 훅을 재설치하라:" >&2
        echo "    pm-config repo protected ${hook_dir##*/} <목록>" >&2
        echo "    pm-update" >&2
        return 1
    fi
    if ! command -v cat >/dev/null 2>&1; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 읽을 cat 명령을 찾을 수 없어 push 거부." >&2
        echo "  실행 환경의 PATH를 복구한 뒤 다시 push하라." >&2
        return 1
    fi

    # 파일은 cat 한 번으로만 읽고, 종료코드와 그때 얻은 문자열을 같은 스냅샷의 두 축으로 쓴다.
    # 명령 치환이 후행 개행을 지우므로 셸 builtin printf가 종단 바이트 하나를 붙여 보존한 뒤
    # 위치로 제거한다. 이 바이트는 성공 표식이 아니다(cat rc는 $?가 단독 담당) — 파일 내용이
    # 같은 바이트를 포함해도 마지막에 훅이 붙인 한 바이트만 제거하므로 위조 채널이 없다.
    protected_snapshot=$(
        cat "$protected_file"
        protected_read_rc=$?
        printf .
        exit "$protected_read_rc"
    )
    protected_read_rc=$?
    protected_snapshot=${protected_snapshot%.}

    protected_entries=0
    protected_malformed=0
    # 정상 sidecar는 각 행(마지막 행 포함)이 개행으로 끝난다. 위 프레이밍 덕분에 명령 치환
    # 뒤에도 원본의 마지막 개행 유무가 남아 있어, 여러 완성 행 뒤의 마지막 부분행도 잡는다.
    case "$protected_snapshot" in
        *"
") ;;
        *) protected_malformed=1 ;;
    esac
    while IFS= read -r protected_branch; do
        [ -n "$protected_branch" ] || continue
        case "$protected_branch" in
            *[[:space:]]*) protected_malformed=1 ;;
        esac
        protected_entries=$((protected_entries + 1))
        protected_branches="${protected_branches}${protected_branch}
"
    done <<PM_PROTECTED_SNAPSHOT
$protected_snapshot
PM_PROTECTED_SNAPSHOT

    protected_invalid=0
    [ "$protected_read_rc" -eq 0 ] || protected_invalid=1
    [ "$protected_entries" -gt 0 ] || protected_invalid=1
    [ "$protected_malformed" -eq 0 ] || protected_invalid=1
    if [ "$protected_invalid" -ne 0 ]; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 확인할 수 없어 push 거부 — 손상됐거나 훅 재설치 중일 수 있음: $protected_file" >&2
        echo "  sidecar를 직접 편집하지 말고 보호목록 설정 후 훅을 재설치하라:" >&2
        echo "    pm-config repo protected ${hook_dir##*/} <목록>" >&2
        echo "    pm-update" >&2
        return 1
    fi
    protected_loaded=1
    unset protected_branch protected_entries protected_malformed
    unset protected_snapshot protected_read_rc protected_invalid
    return 0
}

# stdin(<localref> <localsha> <remoteref> <remotesha> 줄들)의 각 push ref 를 순회한다. pre-push 는
# push 전체에 한 번 발화(all-or-nothing) — 보호 ref 를 *전부* 검증하고, 하나라도 실패하면(하드 차단
# or 라이브 게이트 미green) exit 1 로 push 전체를 거부한다(한 push 에 보호 ref 여러 개면 각 ref 의
# localsha 로 각각 게이트·미검증 ref 가 green ref 에 편승해 올라가는 것 차단). board.py/인터프리터는
# 첫 게이트 검증 때 1회 지연 해소한다. stdin 은 파이프 아닌 fd0 직독이라 루프가 현재 셸에서 돌아
# exit·변수 누적이 훅 전체에 반영된다(서브셸 아님).
board=""
py=""
python_floor=""
found_versions=""
resolved=0
self_tested_shas=""

# 채택자 명령을 실행할 때 제거할 repository-local Git 환경은 현재 Git 자체에서 유도한다.
# rev-parse가 고장난/구형 Git에서도 훅 자체가 죽지 않도록, 폴백은 이 기능 도입 당시 훅이
# 명시적으로 지우던 보수적 7개 목록을 유지한다. 정상 경로 목록을 코드에 복제하지 않는다.
pm_unset_git_local_env() {
    _pm_git_local_env=$(git rev-parse --local-env-vars 2>/dev/null)
    _pm_git_local_env_rc=$?
    if [ "$_pm_git_local_env_rc" -ne 0 ] || [ -z "$_pm_git_local_env" ]; then
        _pm_git_local_env="GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES"
    fi
    unset $_pm_git_local_env
    unset _pm_git_local_env _pm_git_local_env_rc
}

while read -r _local_ref local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
        refs/heads/*) branch=${remote_ref#refs/heads/} ;;
        *) continue ;;
    esac

    if [ "$protected_loaded" != "1" ]; then
        pm_load_protected_branches || exit 1
    fi

    # 이 ref 가 첫 branch ref 시점에 읽어 둔 sidecar 보호목록에 있나?
    is_protected=0
    case "$protected_branches" in
        *"
$branch
"*) is_protected=1 ;;
    esac
    [ "$is_protected" = "1" ] || continue   # 비보호 ref(feature)·tag → 이 ref 통과·다음 ref.

    # 보호 ref — 승인(PM_ALLOW_PROTECTED_PUSH=1) 없으면 하드 차단 (즉시 push 전체 거부).
    if [ "$PM_ALLOW_PROTECTED_PUSH" != "1" ]; then
        echo "[pm 보호 가드] 보호 브랜치 '$branch' 로의 push 거부." >&2
        echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — feature 브랜치로 작업하고" >&2
        echo "  main 갱신은 사용자에게 맡긴다(PR/머지). 사용자 명시 OK 면:" >&2
        echo "    PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi

    # 승인됨(PM_ALLOW=1) — repo 형상별 증거 계약. mode sidecar 부재/미해소는 추측 없이 거부한다.
    gate_mode=""
    self_test_cmd=""
    if [ -f "$gate_contract_file" ]; then
        {
            IFS= read -r gate_mode
            IFS= read -r self_test_cmd
        } < "$gate_contract_file"
    fi
    case "$gate_mode" in
        release)
            # PM_SKIP_LIVE_GATE는 framework release livegate만 생략한다. adopter self-test에는
            # 적용하지 않아 skip 상시화가 자기 검증까지 무력화하지 않게 한다.
            if [ "$PM_SKIP_LIVE_GATE" = "1" ]; then
                # 우회 사유는 **라이브 축**(오프라인·라이브 무관 변경·긴급 hotfix)이다. 미해소
                # must-fix 는 다른 축이라 이 우회의 대상이 아니다. livegate 마지막 reason 은 green
                # 뒤 새 반려를 못 보므로, 장부 writer가 매 변경에 원자 갱신하는 현행 잔여 표식만
                # 읽는다. Python 해소는 불요지만 `clear` 정확 한 줄 외에는 전부 fail-closed다.
                mf_engine_root=""
                [ -f "$engine_root_file" ] && IFS= read -r mf_engine_root < "$engine_root_file"
                mf_flag="$mf_engine_root/.project_manager/.local/release-must-fix"
                mf_state=""
                mf_extra=""
                mf_read_rc=1
                mf_extra_rc=1
                if [ -n "$mf_engine_root" ] && [ -f "$mf_flag" ]; then
                    {
                        IFS= read -r mf_state
                        mf_read_rc=$?
                        IFS= read -r mf_extra
                        mf_extra_rc=$?
                    } < "$mf_flag"
                fi
                if [ "$mf_read_rc" -ne 0 ] || [ "$mf_extra_rc" -eq 0 ] || [ -n "$mf_extra" ]; then
                    echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — must-fix 잔여 판정 표식을 읽을 수 없음." >&2
                    echo "  PM_SKIP_LIVE_GATE 는 잔여가 없다는 현행 표식(clear)이 명확할 때만 허용된다 — 부재·손상·판독 실패는 fail-closed." >&2
                    echo "  장부/환경을 복구하고 표식을 다시 생성하라:" >&2
                    echo "    board.py livegate record" >&2
                    exit 1
                fi
                case "$mf_state" in
                    clear) continue ;;
                    blocked)
                        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 미해소 must-fix 잔여." >&2
                        echo "  PM_SKIP_LIVE_GATE 는 라이브 축 우회이고 이 차단은 리뷰 잔여 축이다 — 우회 대상이 아니다." >&2
                        echo "  현재 티켓 fix의 기계 확인 증거로 게이트마다 처분을 선언한 뒤 다시 기록하라:" >&2
                        echo "    additional_reviewer.py --resolve-gate <게이트> --pm-verified" >&2
                        echo "    board.py livegate record" >&2
                        exit 1
                        ;;
                    *)
                        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — must-fix 잔여 판정 표식 형식 오류('$mf_state')." >&2
                        echo "  허용 형식은 writer가 원자 기록한 clear 또는 blocked 정확 한 줄뿐이다. board.py livegate record를 다시 실행하라." >&2
                        exit 1
                        ;;
                esac
            fi
            ;;
        self-test)
            # 채택자 전용 한정 우회는 증거 생성(명령 해소·HEAD pin·clean 검사)보다 앞선다.
            # dirty는 거부 조건이 아니라 감사 메타데이터다. status 자체가 실패해도 우회는
            # 사용할 수 있어야 하므로 unknown을 기록한다. 반면 시각·사유·append는 누가 왜 언제
            # 우회했는지 남기는 감사 증거 자체라 하나라도 만들지 못하면 fail-closed한다.
            if [ "$PM_SKIP_SELF_TEST" = "1" ]; then
                if ! command -v tr >/dev/null 2>&1 || ! command -v date >/dev/null 2>&1; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사에 필요한 tr/date 명령을 찾을 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                bypass_reason=$(printf '%s' "$PM_SELF_TEST_BYPASS_REASON" | tr '\\r\\n\\t' '   ')
                bypass_reason_rc=$?
                if [ "$bypass_reason_rc" -ne 0 ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 사유를 안전하게 정규화할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                case "$bypass_reason" in
                    *[![:space:]]*) ;;
                    *)
                        echo "[pm 보호 가드] 채택자 자기 검증 우회 거부 — PM_SELF_TEST_BYPASS_REASON 사유가 필요하다." >&2
                        exit 1
                        ;;
                esac
                bypass_status=$(
                    pm_unset_git_local_env
                    git status --porcelain --untracked-files=normal 2>/dev/null
                )
                bypass_status_rc=$?
                if [ "$bypass_status_rc" -ne 0 ]; then
                    bypass_dirty=unknown
                elif [ -n "$bypass_status" ]; then
                    bypass_dirty=true
                else
                    bypass_dirty=false
                fi
                bypass_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' </dev/null 2>/dev/null)
                bypass_at_rc=$?
                if [ "$bypass_at_rc" -ne 0 ] || [ -z "$bypass_at" ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 시각을 기록할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                bypass_log="$hook_dir/self-test-bypass.log"
                if [ -L "$bypass_log" ] || { [ -e "$bypass_log" ] && [ ! -f "$bypass_log" ]; }; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 로그가 일반 파일이 아님 — 보호 push 거부." >&2
                    exit 1
                fi
                repo_name=${hook_dir##*/}
                if [ -z "$repo_name" ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 repo 이름을 해소할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                if ! printf '%s\\trepo=%s\\tbranch=%s\\tsha=%s\\tdirty=%s\\treason=%s\\n' \
                        "$bypass_at" "$repo_name" "$branch" "$local_sha" \
                        "$bypass_dirty" "$bypass_reason" >> "$bypass_log"; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 로그 기록 실패 — 보호 push 거부." >&2
                    exit 1
                fi
                echo "[pm 보호 가드·감사] 채택자 자기 검증 우회 사용: branch=$branch sha=$local_sha dirty=$bypass_dirty" >&2
                echo "  사유: $bypass_reason" >&2
                echo "  기록: $bypass_log" >&2
                continue
            fi

            if ! command -v git >/dev/null 2>&1; then
                echo "[pm 보호 가드] 채택자 자기 검증에 필요한 git 명령을 찾을 수 없어 보호 push 거부." >&2
                exit 1
            fi
            if [ -z "$self_test_cmd" ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 명령 미해소 — fail-closed 거부." >&2
                echo "  areas.md test_cmd 또는 PM 홈 local.conf test.cmd를 설정하고 훅을 재설치하라." >&2
                exit 1
            fi
            if ! command -v sh >/dev/null 2>&1; then
                echo "[pm 보호 가드] 채택자 자기 검증 명령을 실행할 sh를 찾을 수 없어 보호 push 거부." >&2
                exit 1
            fi

            # 한 push에 같은 SHA의 보호 ref가 여러 개여도 clean checkout에서 같은
            # repo 계약을 다시 돌릴 필요가 없다. SHA는 git hex라 공백 구분이 안전하다.
            case " $self_tested_shas " in
                *" $local_sha "*) continue ;;
            esac
            repo_root=$(git rev-parse --show-toplevel 2>/dev/null)
            current_head=""
            [ -n "$repo_root" ] && current_head=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)
            if [ -z "$repo_root" ] || [ -z "$current_head" ] || [ "$current_head" != "$local_sha" ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 기준을 push SHA에 고정할 수 없어 거부." >&2
                echo "  push 대상 브랜치를 checkout한 뒤 같은 HEAD에서 다시 push하라." >&2
                echo "  checkout HEAD=${current_head:-미해소} · push=${local_sha}" >&2
                exit 1
            fi

            # pre-push는 git이 GIT_DIR/GIT_WORK_TREE 등을 훅 환경에 주입할 수 있다. 그 상태로
            # 채택자 테스트의 git fixture를 실행하면 임시 repo 대신 사용자 슬롯 index/gitdir을
            # 읽거나 변경한다. repo_root/HEAD 핀은 위에서 먼저 해소하고, clean 판정과 실제 테스트는
            # git 저장소 지시 환경을 제거한 별도 subshell에서 실행한다.
            worktree_status=$(
                pm_unset_git_local_env
                git -C "$repo_root" status --porcelain --untracked-files=normal
            )
            status_rc=$?
            if [ "$status_rc" -ne 0 ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 전 워킹트리 clean 상태를 확인할 수 없어 거부." >&2
                echo "  push 대상 checkout의 git status를 복구한 뒤 다시 push하라." >&2
                exit 1
            fi
            if [ -n "$worktree_status" ]; then
                echo "[pm 보호 가드] push SHA와 다른 dirty 워킹트리에서는 자기 검증할 수 없어 거부." >&2
                echo "  tracked/staged/untracked 변경을 commit·stash·정리한 clean checkout에서 다시 push하라." >&2
                exit 1
            fi

            echo "[pm 보호 가드] 채택자 자기 검증: $self_test_cmd" >&2
            (
                pm_unset_git_local_env
                cd "$repo_root" && sh -c "$self_test_cmd"
            ) </dev/null
            self_test_rc=$?
            if [ "$self_test_rc" -ne 0 ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 RED(rc=$self_test_rc) — 보호 push 거부." >&2
                echo "  areas.md test_cmd 또는 PM 홈 local.conf test.cmd를 현재 repo 명령으로 설정하라." >&2
                echo "  긴급 우회(감사 기록 필수): PM_SKIP_SELF_TEST=1 PM_SELF_TEST_BYPASS_REASON='사유' PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
                exit 1
            fi
            self_tested_shas="$self_tested_shas $local_sha"
            continue
            ;;
        *)
            echo "[pm 보호 가드] repo 게이트 스코프 미해소('$gate_mode') — fail-closed 거부." >&2
            echo "  PM 홈 local.conf upstream.path를 확인하고 pm-update 또는 pm-config repo add로 훅을 재설치하라." >&2
            exit 1
            ;;
    esac

    # framework release 라이브 게이트 검증 자원 1회 지연 해소. board.py 는 **PM 홈 엔진**에 있다 — 슬롯 worktree(회사/
    # family checkout)엔 PM 엔진 파일이 없으므로(회사 repo 무영향), 설치자가 훅 옆에 쓴 sidecar
    # `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다. livegate.json 도 그 PM 홈 .local
    # 소유라 기록 위치와 정합. 인터프리터는 실행검증 폴백(python3->python->py·WindowsApps 가짜
    # shim 회피) + Python 3.11 하한 검증(pm_engine.MIN_PYTHON).
    # sidecar 부재/경로 무효/인터프리터 부재 = fail-closed 거부(무력화 방지).
    if [ "$resolved" != "1" ]; then
        engine_root=""
        [ -f "$engine_root_file" ] && IFS= read -r engine_root < "$engine_root_file"
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/board.py" ]; then
            board="$engine_root/.project_manager/tools/board.py"
        fi
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/python_floor.py" ]; then
            python_floor="$engine_root/.project_manager/tools/python_floor.py"
        fi
        for _cand in python3 python py; do
            if command -v "$_cand" >/dev/null 2>&1 &&
                    "$_cand" --version >/dev/null 2>&1 &&
                    [ -n "$python_floor" ]; then
                probe_output=$("$_cand" "$python_floor" 2>&1)
                probe_rc=$?
                case "$probe_output" in
                    Python*) probe_version=${probe_output#Python } ;;
                    *) probe_version="확인 실패" ;;
                esac
                [ -z "$found_versions" ] || found_versions="$found_versions, "
                found_versions="${found_versions}${_cand}=${probe_version}"
                if [ "$probe_rc" -eq 0 ]; then
                    py="$_cand"
                    break
                fi
            fi
        done
        if [ -z "$board" ] || [ -z "$py" ]; then
            echo "[pm 라이브 게이트] 게이트 검증 실행 불가 — fail-closed 거부." >&2
            if [ -z "$board" ]; then
                echo "  PM 엔진 board.py 를 못 찾았다 (engine-root sidecar='${engine_root}')." >&2
            elif [ -z "$python_floor" ]; then
                echo "  Python 하한 probe를 못 찾았다 (engine-root sidecar='${engine_root}')." >&2
            else
                echo "  Python 3.11+ 필요, 발견: ${found_versions:-없음}." >&2
            fi
            echo "  보호 브랜치 push 는 라이브 게이트 green 을 요구한다. 게이트를 못 돌리면 무력화 방지로 거부한다." >&2
            echo "  라이브-무관 변경(docs 등)·긴급 hotfix 면 우회(현행 must-fix 잔여-무 표식 clear 가 있어야 함 — 부재면 record 1회 선행):" >&2
            echo "    PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
            exit 1
        fi
        resolved=1
    fi

    # 이 보호 ref 의 push sha 로 라이브 게이트 green 검증 (기록 존재·pass·HEAD 일치=rc0·board.py 사유 surface).
    # </dev/null — 다중 ref 루프가 stdin(fd0) 직독이라 board.py 가 stdin 을 소비하면
    # all-or-nothing 순회가 깨진다(현재 안 읽지만 향후 변경 방어).
    "$py" "$board" livegate check --rev "$local_sha" </dev/null
    gate_rc=$?
    if [ "$gate_rc" -ne 0 ]; then
        # rc!=0 — 이 보호 ref 가 미green → push 전체 거부 + 2분기 안내 (환경 복구 vs 우회).
        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 라이브 게이트 미green." >&2
        echo "  릴리즈(main 머지)는 릴리즈 라이브 wave 가 이 커밋에서 green 이어야 한다(위 사유 참조)." >&2
        echo "  - 환경 문제(오프라인·LLM 서비스 장애/한도·게이트 오작동)면 — 우회 아님·환경 복구 후 재실행:" >&2
        echo "      board.py livegate record" >&2
        echo "  - 라이브-무관 변경(docs 등)·긴급 hotfix 면 — 우회(현행 must-fix 잔여-무 표식 clear 필요·부재면 record 1회 선행):" >&2
        echo "      PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi
done

# 모든 보호 ref 가 게이트 통과(또는 skip)·또는 보호 ref 없음(비보호/tag) → push 통과.
exit 0
"""

# ── 보호 브랜치 pre-commit 훅 (commit-time 강제) ──────────────
# pre-push 훅과 **같은 배선**을 탄다 — 같은 훅 디렉토리(`.local/repo-hooks/<repo>/`)·같은
# sidecar `protected`(줄당 1브랜치)·같은 bare `core.hooksPath`(신규 seam 0). 파일 하나가
# 더 놓일 뿐이라 슬롯 worktree 는 재설치 즉시 이 훅을 탄다. 단, sidecar 부재 처리는 의도적으로
# 비대칭이다: pre-commit은 전 commit 차단의 blast radius를 피하려 통과시키고, 하드 백스톱인
# pre-push만 보호목록 미해소를 fail-closed로 거부한다.
#
# 왜 pre-push 만으론 부족한가: pm_role §보호 브랜치 가드의 "보호 브랜치에 자율 commit 하지
# 않는다" 는 강제 수단이 push 단계뿐이었고, 부트스트랩 0단계 main-참조 검사는 세션 *진입
# 시점*만 본다 — 세션 중 보호 브랜치로 checkout 해서 커밋하는 것을 아무 기계도 안 봤다
# (실측: v1.4.0 wave 11커밋이 전부 `main` 에서).
#
# 로직: sidecar 보호목록에 `git symbolic-ref --short HEAD`(= 현재 브랜치)가 있으면
# `PM_ALLOW_PROTECTED_COMMIT=1` 이 아닌 한 거부(exit 1). **detached HEAD 는 통과** —
# symbolic-ref 가 rc≠0 이라 브랜치가 없다(readonly 공유 슬롯 시맨틱·0단계 main-참조 판정
# 의 readonly carve-out 과 일치). 비보호 브랜치·sidecar 부재도 통과.
#
# **비커버(정직한 한계)**: `git commit --no-verify`(실측 통과)·merge 커밋
# (`pre-merge-commit` 소관·실측 미발화)·rebase/cherry-pick/revert(sequencer 클래스·실측 통과).
# 이건 우발 방지 가드지 적대적 통제가 아니고, 하드 백스톱은 pre-push(라이브 게이트 포함)가
# 그대로 맡는다. 또 훅은 bare `core.hooksPath` 를 공유하는 **풀 슬롯 worktree** 에만 걸린다
# (PM 홈 clone 자신은 `.git/hooks` 라 가드 밖). merge 미발화는
# 설계상 이득 — "release 브랜치에서 커밋 → main 으로 merge → push" 라는 정합 릴리즈 flow 가
# escape 없이 통과한다(pm_role §릴리즈 절차).
_PROTECTED_PRE_COMMIT_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-commit 가드 — PM 이 보호 브랜치(main 등)에서 자율로
# commit 하지 못하게 한다. install_protected_hook() 가 pre-push 훅과 같은 디렉토리에 설치하고,
# 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치·pre-push 와 공유).
case "$0" in
    */*) hook_dir=${0%/*} ;;
    *) hook_dir=. ;;
esac
protected_file="$hook_dir/protected"
# 의도적 비대칭: 이 commit-time 우발 방지 가드는 sidecar 부재 시 전 commit 차단의 blast radius를
# 피하려 통과한다. 보호목록 미해소의 하드 fail-closed 백스톱은 pre-push가 담당한다.
[ -f "$protected_file" ] || exit 0

# 현재 브랜치. **detached HEAD 는 통과** — symbolic-ref 가 rc≠0(브랜치 없음)이고, readonly
# 공유 슬롯 시맨틱(0단계 readonly carve-out)과 일치시킨다.
if ! command -v git >/dev/null 2>&1; then
    echo "[pm 보호 가드] 현재 브랜치 판정에 필요한 git 명령을 찾을 수 없어 commit 거부." >&2
    exit 1
fi
branch=$(git symbolic-ref -q --short HEAD 2>/dev/null)
branch_rc=$?
if [ "$branch_rc" -eq 1 ]; then
    # symbolic-ref rc1은 정상 detached HEAD일 때만 통과한다. 저장소/HEAD까지 읽을 수 없는
    # 손상을 detached로 오인하지 않도록 commit 객체 해소를 추가 확인한다.
    git rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1 || {
        echo "[pm 보호 가드] detached HEAD 여부를 확인할 수 없어 commit 거부." >&2
        exit 1
    }
    exit 0
fi
if [ "$branch_rc" -ne 0 ] || [ -z "$branch" ]; then
    echo "[pm 보호 가드] 현재 브랜치를 판정할 수 없어 commit 거부." >&2
    exit 1
fi

# 이 브랜치가 sidecar 보호목록에 있나? (pre-commit이 직접 순회해 정확히 일치하는 행을 찾는다)
is_protected=0
while IFS= read -r protected_branch; do
    [ -n "$protected_branch" ] || continue
    if [ "$branch" = "$protected_branch" ]; then
        is_protected=1
        break
    fi
done < "$protected_file"
[ "$is_protected" = "1" ] || exit 0   # 비보호 브랜치(feature) → 통과.

# 보호 브랜치 — 승인(PM_ALLOW_PROTECTED_COMMIT=1) 없으면 거부.
if [ "$PM_ALLOW_PROTECTED_COMMIT" != "1" ]; then
    echo "[pm 보호 가드] 보호 브랜치 '$branch' 에서의 commit 거부." >&2
    echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — 작업 브랜치로 옮겨 커밋하라:" >&2
    echo "    git switch -c <작업-브랜치>      # 새로 파거나" >&2
    echo "    git switch <기존-작업-브랜치>    # 이미 있으면 그리로" >&2
    echo "  릴리즈도 같다 — 릴리즈 커밋은 release 브랜치에서 하고 '$branch' 은 merge 로 받는다" >&2
    echo "  (merge 커밋은 이 훅이 안 보므로 escape 불요)." >&2
    echo "  사용자 명시 OK 면:" >&2
    echo "    PM_ALLOW_PROTECTED_COMMIT=1 git commit ..." >&2
    exit 1
fi

exit 0
"""

# ── 설치 산출물 명세 — install ↔ 정합 판정의 단일 진실 ────────────────
# **`install_protected_hook` 이 *쓰는 것*이 곧 drift 축이다.** 설치와 판정(pm_update 의
# `_protected_hook_in_sync`)이 각자 목록을 들고 있으면 한쪽만 자라 조용히 갈라진다 — 실제로 그
# 클래스가 연달아 났다(읽기 실패 축 누락 → 다음 라운드 실행권한 축 누락). 그래서 산출물 전수를
# 이 명세 하나가 소유하고, 설치는 이걸 **써서** 쓰고 판정은 이걸 **읽어서** 본다(축을 열거하지
# 않고 유도). 새 산출물을 설치에 추가하려면 이 목록에 넣어야 하고(설치 루프가 순회한다), 그러면
# 판정도 자동으로 따라간다.
#
# 실행권한: 훅 파일은 0755 여야 한다 — **없으면 git 이 훅을 조용히 건너뛴다**(보호 침묵 비활성).
# sidecar(`protected`·`engine-root`·`gate-contract`)는 훅이 `read` 로만 읽는 데이터라 실행권한 요구가 없다
# (설치는 atomic replace 전에 0644 설정) → `executable=False`. 나중에 실행 파일 sidecar가
# 생기면 그 플래그만 True로 바꾸면 판정이 따라온다.
_PROTECTED_HOOK_EXECUTABLE_MODE = 0o755
_PROTECTED_SIDECAR_MODE = 0o644
_PROTECTED_GATE_MODES = frozenset({"release", "self-test"})
PROTECTED_GATE_CONTRACT_NAME = "gate-contract"


class ProtectedHookArtifact(NamedTuple):
    """보호 훅 설치 산출물 1개 — 경로·기대 내용·실행권한 필요 여부."""

    path: Path
    content: str
    executable: bool


def _atomic_write_protected_artifact(artifact: ProtectedHookArtifact) -> None:
    """훅/sidecar 하나를 같은 디렉터리 임시파일에서 완성한 뒤 원자 교체한다.

    재설치와 push가 겹쳐도 독자는 이전 완성본 또는 새 완성본만 본다. 특히 빈/부분
    ``protected``를 잠깐 노출해 보호 ref를 비보호로 통과시키는 제자리 truncate 창을 없앤다.
    실행권한도 교체 전에 임시파일에 설정하므로 새 훅이 보이는데 아직 실행 불가인 창이 없다.
    """
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{artifact.path.name}.", suffix=".tmp", dir=artifact.path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(artifact.content)
        tmp.chmod(
            _PROTECTED_HOOK_EXECUTABLE_MODE
            if artifact.executable else _PROTECTED_SIDECAR_MODE)
        fs.atomic_replace(tmp, artifact.path)
    except BaseException:
        # fdopen 전/중 실패도 descriptor와 임시파일을 남기지 않는다. 원래 산출물은 replace
        # 전까지 건드리지 않았으므로 그대로 유효하다.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def protected_hook_artifacts(home,
    repo: str, protected: list[str], *, gate_mode: str = "release",
    test_cmd: str = "pytest -q",
) -> list[ProtectedHookArtifact]:
    """`install_protected_hook` 이 쓰는 **파일 전수** + 각 파일의 기대 내용/실행권한.

    설치·정합 판정 공용 단일 진실(위 주석). 반환 순서 = 설치 순서:
    데이터 sidecar를 먼저 완성하고 generic 훅 본문을 나중에 노출한다. 배선은 이 목록
    순회 *뒤에* 수행되므로 초기 설치의 미완성 본문이 발화하지 않는다.

    ⚠ bare `core.hooksPath` **배선**은 파일이 아니라 git config 라 이 목록 밖이다 — 판정은
    `protected_hook_wired()`(공용 헬퍼)로 그 축을 따로 본다."""
    if gate_mode not in _PROTECTED_GATE_MODES:
        raise ValueError(f"알 수 없는 보호 push gate mode: {gate_mode!r}")
    if not test_cmd.strip() or "\n" in test_cmd or "\r" in test_cmd:
        raise ValueError("보호 push test_cmd는 비어 있지 않은 한 줄이어야 한다")
    if not protected or any(
            not isinstance(branch, str) or not branch or
            any(char.isspace() for char in branch)
            for branch in protected):
        raise ValueError("보호 브랜치 목록은 비어 있지 않은 공백 없는 이름이어야 한다")
    hook_dir = pm_paths.repo_hooks_dir(home) / repo
    gate_contract = f"{gate_mode}\n{test_cmd}\n"
    return [
        ProtectedHookArtifact(
            hook_dir / "protected", "".join(f"{b}\n" for b in protected), False),
        ProtectedHookArtifact(hook_dir / "engine-root", f"{home.root.resolve()}\n", False),
        ProtectedHookArtifact(
            hook_dir / PROTECTED_GATE_CONTRACT_NAME, gate_contract, False),
        ProtectedHookArtifact(hook_dir / "pre-push", _PROTECTED_PRE_PUSH_HOOK, True),
        ProtectedHookArtifact(hook_dir / "pre-commit", _PROTECTED_PRE_COMMIT_HOOK, True),
    ]


def install_protected_hook(
    home,
    repo: str,
    protected: list[str],
    *,
    gate_mode: str,
    test_cmd: str,
    git_runner: worktree_records.GitRunner | None = None,
) -> bool:
    """보호 브랜치 pre-push + pre-commit 훅 + sidecar 를 (재)설치하고 bare `core.hooksPath` 를 wiring 한다.

    **멱등·자가치유** — `pm-config repo add`·`worktree add`·`pm_update` sync 가 매번 호출(이미
    있으면 갱신). 세 가지를 한다:
      1. 훅 디렉토리 `.project_manager/.local/repo-hooks/<repo>/` 생성(프레임워크 소유·gitignore).
      2. sidecar 3종 + `pre-push`·`pre-commit`훅(generic·POSIX sh·LF):
         `protected`(보호목록·줄당 1브랜치·**두 훅 공용**)와 `engine-root`(PM 홈 REPO 절대경로
         1줄·라이브 게이트 board.py 해소용), `gate-contract`(1행 mode,
         2행 adopter 자기 검증 명령). mode/command는 **한 파일·한 번의 원자 교체**로
         교체되어 재설치 중에도 서로 다른 세대가 결합하지 않는다.
      3. bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리(절대경로)로 set
         → 슬롯 push/commit 이 이 훅들에 게이트된다.

    **bare 부재 = no-op·False**(가드) — bare 가 없으면 게이트할 대상이 없다(repo add 가 아직
    clone 안 함·bare 원 미생성). 훅/sidecar 도 쓰지 않고 조용히 False(설치 안 함). bare 존재면 설치
    후 True. **회사 repo 무영향** — 모든 write 는 `.project_manager/.local/` + bare config 1줄
    (client-side)·서버 ref/사용자 클론 무변경.

    `git_runner` 주입 시 `core.hooksPath` config 호출을 그 runner 로(테스트 hermetic·`git -C
    <bare>` 컨텍스트는 `_real_git_runner(bare)` 가 묶는다). LF 줄바꿈 명시(Windows 에서도 sh
    가 읽도록·newline="\\n").

    `gate_mode`/`test_cmd`는 의도적으로 기본값 없는 keyword-only다. 설치 경로가 repo별 계약
    해소를 잊으면 즉시 TypeError로 실패해야 하며, framework `release/pytest`로 조용히 되돌아가면
    안 된다. 프로덕션 호출은 `repo_protection._install_protected_hook` 한 깔때기를 탄다.
    """
    bare = pm_paths.bare_repo_path(home, repo)
    if not bare.exists():
        return False  # 게이트할 bare 없음 — no-op(repo add 선행 전).

    hook_dir = pm_paths.repo_hooks_dir(home) / repo
    hook_dir.mkdir(parents=True, exist_ok=True)

    # 1~2) 훅·sidecar **전수** write(+실행권한). 멱등 — 매 호출 덮어쓰기(엔진 update 자가치유).
    # 산출물 목록·내용·실행권한은 `protected_hook_artifacts` 단일 진실이다 — 여기서 개별 파일을
    # 직접 쓰지 않는다(설치와 정합 판정이 갈라지는 클래스 폐쇄·위 명세 주석). 순회 순서 = 명세
    # 순서(protected → engine-root → gate-contract → pre-push → pre-commit)이고,
    # 배선(3)은 그 뒤다. 데이터를 본문보다 먼저 완성해 초기 설치 중
    # "신 본문 + mode 부재" 하드 차단 창도 만들지 않는다.
    #   - `pre-push`·`pre-commit`: generic 훅 본문·POSIX sh·LF·0755.
    #   - sidecar `protected`: 보호목록(줄당 1브랜치). 목록 변경 시 재설치가 갱신.
    #   - sidecar `engine-root`: PM 홈 REPO 절대경로 1줄(라이브 게이트 board.py 해소용).
    #   - sidecar `gate-contract`: repo 형상별 mode + adopter 자기 검증 명령의
    #     분할 불가 원자 계약.
    #     설치자는 PM 홈 컨텍스트에서 도므로 REPO 를 안다 — 훅은 슬롯 worktree(회사/family
    #     checkout·PM 엔진 파일 없음)에서 발화해 self-locate 가 불가하다.
    for artifact in protected_hook_artifacts(home,
            repo, protected, gate_mode=gate_mode, test_cmd=test_cmd):
        _atomic_write_protected_artifact(artifact)

    # 3) bare core.hooksPath wiring (절대경로) — client-side·우리 미러 config 1줄.
    # **rc 검사**: config 실패면 훅이 실제로 wiring 안 됐는데 성공 보고하면 보호
    # 가드가 *침묵 무력화* 된다(하드 차단 보장 위반). rc≠0 → False 반환(호출부가 경고 surface).
    runner = git_runner or worktree_git._real_git_runner(bare)
    rc, _out = runner(["config", "core.hooksPath", str(hook_dir.resolve())])
    return rc == 0


def _protected_sidecar_path(home, repo: str) -> Path:
    return pm_paths.repo_hooks_dir(home) / repo / "protected"


def _read_protected_sidecar(home, repo: str) -> list[str] | None:
    """훅 sidecar 의 실 보호목록(줄당 1브랜치). 미설치/읽기실패 → None (fail-soft)."""
    path = _protected_sidecar_path(home, repo)
    if not path.is_file():
        return None
    try:
        text = fs.read_text_shared(path, encoding="utf-8")
    except OSError:
        return None
    return [line.strip() for line in text.splitlines() if line.strip()]


def protected_hook_wired(home,
    repo: str,
    *,
        git_runner: worktree_records.GitRunner | None = None,
) -> bool | None:
    """bare `core.hooksPath` 가 이 repo 훅 디렉토리를 가리키는가 — 판정 불가면 None.

    **왜 sidecar 만으로는 부족한가**: `install_protected_hook` 은  훅 본문  sidecar 기록
    bare `core.hooksPath` 배선 순서다. 이 실패하면 **sidecar 는 최신인데 훅은 아예 안 걸린**
    상태가 된다 — 그런데 sidecar 내용만 보면 "정합" 이라 조회는 `✓` 를 내고 bootstrap reconcile 은
    영구 침묵한다. 이 티켓이 닫으려던 "값-연결 끊김을 정합하다고 거짓 보고" 와 같은 클래스라
    **표시/판정 층에서** 이 축을 함께 본다.

    **읽기 전용** — `git -C <bare> config --get core.hooksPath` 를 읽어 `pm_paths.repo_hooks_dir(home)/<repo>` 와
    같은 경로인지 비교만 한다(`install_protected_hook` 은 호출하지 않는다·시그니처/본문 불변).
    경로 비교는 양쪽 `Path.resolve()` — 설치측이 `hook_dir.resolve()` 를 기록하므로 심링크/상대
    표기 차이를 흡수한다.

    반환:
      - `True`  — 배선됨(훅이 실제로 발화한다).
      - `False` — hooksPath 가 비었거나 **다른 디렉토리**를 가리킨다(= 훅 미배선·보호 꺼짐).
      - `None`  — 판정 불가(bare 부재·git 실패). 오탐 금지 — 호출부는
        None 을 "모름" 으로 표시하고 `False` 로 단정하지 않는다.

    """
    bare = pm_paths.bare_repo_path(home, repo)
    if not bare.exists():
        return None            # 게이트할 bare 없음 — 배선 여부를 논할 수 없다.
    runner = git_runner or git._real_clone_runner()
    rc, out = runner(["-C", str(bare), "config", "--get", "core.hooksPath"])
    if rc != 0:
        # `--get` 은 키 부재에도 rc≠0(=1) 이다. 키 부재 = 미배선이 맞지만, git 자체가 못
        # 도는 경우(바이너리 부재 등)와 구별이 안 되므로 bare 프로브로 갈라낸다.
        probe_rc, _probe = runner(
            ["-C", str(bare), "rev-parse", "--is-bare-repository"])
        return False if probe_rc == 0 else None
    configured = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not configured:
        return False
    return Path(configured).resolve() == (pm_paths.repo_hooks_dir(home) / repo).resolve()
