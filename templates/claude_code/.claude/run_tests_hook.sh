#!/usr/bin/env bash
# PostToolUse hook: 프로젝트 소스 파일이 Write/Edit 되면 회귀 테스트를 자동 실행한다.
# stdin: Claude Code hook JSON. stdout: 선택적 systemMessage JSON.
#
# 멀티-유저 안전(clone-and-go): 프로젝트 루트를 스크립트 위치에서 self-resolve 하고(절대경로
# 박제 금지 — 다른 PC 에서 재-import 불필요), 인터프리터는 python3→python 런타임 폴백으로
# OS 무관하게 고른다. 이 파일은 치환 토큰이 없어 모든 머신에서 byte-identical 하다.
# 테스트 러너 명령은 이 훅이 사는 **체크아웃 루트**의 `.project_manager/local.conf` 하나에서만
# 읽는다(`test.cmd`). 그 파일에 test.cmd 가 없으면 엔진 폴백(`pytest tests/`)이 정상 경로다 —
# PM 홈과 코드 worktree 가 분리된 형상에선 worktree 쪽 local.conf 가 test.cmd 를 안 담을 수 있다.
# 이 파일은 엔진 소유(manifest 등재)라 여기서 러너 줄을 고쳐도 다음 엔진 동기에 덮인다. 발화 게이트는
# .py 편집으로 고정이다(파이썬 외 스택에선 훅이 발화하지 않을 뿐, 회귀는 엔진 도구로 돌린다).
#
# stdin JSON 파싱·systemMessage 출력은 외부 JSON 프로세서(과거 의존) 없이 python 으로 한다 —
# 그 프로세서는 Windows 에 기본 부재라(command not found → 파일 경로 빈값 → case 불일치 → rc0
# silent no-op, 회귀 자동실행이 조용히 죽었다). 훅은 어차피 python(pytest) 필수라 별도 의존이 아니다.
set -u

# 스크립트 위치(.claude/)에서 프로젝트 루트 self-resolve — 훅 파일이 사는 디렉토리의 부모가 곧
# 프로젝트 루트다(`.claude/` 는 루트 직하). 형제 훅 파일에 의존하지 않는 자족 패턴.
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
repo_root=$(CDPATH= cd -- "$hook_dir/.." && pwd) || exit 0

# 인터프리터 선택 — 후보를 순회하며 *실행검증*(--version rc)으로 채택(엔진 _detect_py·T-0022 시맨틱과
# 동형·python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim(command -v 통과·
# 실행 시 Permission denied rc126)을 못 거른다. 전부 실패 시 rc0 조용 통과(훅은 정상 작업을 막지 않음).
# stdin JSON 파싱에도 이 인터프리터를 쓰므로 파일 상단에서 먼저 고른다(외부 프로세서 제거 — T-0210).
py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || exit 0

# hook stdin 에서 편집된 파일 경로를 해소하고, 이 프로젝트 안의 .py 인지까지 python 안에서 판정한다.
# '.tool_input.file_path // .tool_response.filePath // empty' 필터 시맨틱을 python 으로 옮기고
# (부재·비-dict·malformed JSON 은 빈 출력으로 graceful), 경로 형식 정규화 + containment + .py 게이트도
# python 이 한다 — repo_root 를 argv 로 넘긴다. bash case 의 리터럴 접두 매칭은 경로 *형식* 에
# 민감해서, 하네스가 native Windows 경로(C:\...)를 보내면 Git Bash pwd 형(/c/...)인 repo_root 와
# 불일치→rc0 silent skip 했다(T-0210 must-fix). python 이 세 형식(native C:\ · 드라이브 C:/ · mount
# /c/)을 단일 canonical 로 수렴시켜 판정하고, 통과 시에만 경로를 emit → bash 는 비어있음만 검사한다.
# stdin 은 여기서 한 번만 소비하며(이전 구현과 동일 위치), 이후 단계(pytest)는 stdin 을 안 쓴다.
target=$("$py" -c 'import json, os, re, sys

repo_root = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    data = json.load(sys.stdin)
except Exception:
    data = None


def _pick(container, key):
    if isinstance(container, dict):
        value = container.get(key)
        if isinstance(value, str):
            return value
    return None


def _canon(p):
    # 경로를 플랫폼 canonical 형으로 수렴. Windows 는 Git Bash mount(/c/...)를 드라이브형으로
    # 바꾼 뒤 normcase/normpath(대소문자 무시)로, POSIX 는 normpath(대소문자 유지)로 정규화한다.
    # mount 변환·normcase 는 os.name == "nt" 에서만 적용 → POSIX 실경로(/c/... 존재 가능)에 항등.
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])/", p)
        if m:
            p = m.group(1) + ":/" + p[3:]
        return os.path.normcase(os.path.normpath(p))
    return os.path.normpath(p)


path = None
if isinstance(data, dict):
    path = _pick(data.get("tool_input"), "file_path")
    if path is None:
        path = _pick(data.get("tool_response"), "filePath")

emit = ""
if path and repo_root:
    canon_path = _canon(path)
    canon_root = _canon(repo_root)
    inside = canon_path == canon_root or canon_path.startswith(canon_root + os.sep)
    if inside and canon_path.endswith(".py"):
        emit = path
sys.stdout.write(emit)' "$repo_root")

# 프로젝트 안의 .py 로 판정된 경우에만 target 이 비어있지 않다(위 python 이 형식정규화+게이트 수행).
[ -n "$target" ] || exit 0

cd "$repo_root" || exit 0

# 러너 실행 결과를 systemMessage 한 줄로 요약한다 (인자: 종료코드·전체 출력).
# 정상 종료면 마지막 줄 그대로(현행 메시지 무변경), 비정상이면 rc 를 덧붙인다. 출력이 아예 없는
# 실패(rc≠0 + 빈 stdout/stderr — `false`·일부 러너의 조용한 abort)는 빈 메시지가 되면 성공과
# 구분되지 않으므로 rc 만 담은 문구로 대체한다.
summarize_run() {
    _rc=$1
    _last=$(printf '%s\n' "$2" | tail -1)
    if [ "$_rc" -eq 0 ]; then
        printf '%s' "$_last"
    elif [ -n "$_last" ]; then
        printf '%s (rc %s)' "$_last" "$_rc"
    else
        printf '러너가 출력 없이 실패했다 (rc %s)' "$_rc"
    fi
}

# 러너는 채택자 소유 — `.project_manager/local.conf` 의 `test.cmd` 를 해소해 *그대로* 실행한다
# (플래그를 덧붙이지 않는다: 실값이 이미 `-q` 등 자기 플래그를 담고 있어 중복·충돌한다). 테스트 루트가
# `tests/` 가 아닌 채택자(`… -m pytest .project_manager/checks/tests -q`)에서 하드코딩이 수집 0 이나
# 엉뚱한 스위트를 돌리던 클래스를 닫는다. 해소는 엔진 `board.local_config()` 와 동형 — 주석/빈 줄 무시,
# 키·값 주변 공백 무시, 같은 키 중복 시 last-wins(마지막 값이 비면 앞 값을 해제한 것으로 보고 폴백).
# 실행이 `sh -c` 인 이유: test.cmd 는 인터프리터 경로 + 인자로 된 명령줄이라 변수 직접 실행으론 워드
# 스플리팅이 깨져 셸 파싱이 필요한데, `eval` 은 **이 훅의 셸 컨텍스트**에서 돌아 위의 `set -u` 를
# 상속한다 — `PYTHONPATH="$PYTHONPATH:src" pytest` 처럼 미설정 변수를 참조하는 정상 명령이 unbound
# variable 로 죽는다(엔진에선 안 죽는다). 신선한 자식 셸에 넘겨 엔진 `subprocess.run(shell=True)`
# (POSIX = `/bin/sh -c`) 과 같은 의미로 맞춘다 — 보호훅 `worktree_pool` 의 `sh -c "$self_test_cmd"`
# 선례와 동형. local.conf 는 채택자 자신의 로컬 파일이라 신뢰 경계 문제 없음.
# 지정돼 있는데 실행이 실패하면 폴백하지 않고 실패를 그대로 표기한다 — 설정 오류를 조용히
# 삼키면 채택자가 회귀가 안 도는 줄 모른다(훅의 조용한 실패 금지).
# 스코프(의도): 엔진 `board._test_cmd` 의 4층 체인(--cmd override → 슬롯 lease → areas.md →
# local.conf) 중 **최하층 하나만** 본다 — 다층 해소는 board 회귀 게이트 소관이고, 이 훅은 편집마다
# 도는 경량 advisory 표면이라 장부 조회를 의도적으로 하지 않는다.
#
# **미지정과 읽기 실패를 구분한다.** 파일이 없으면 미지정(폴백이 정상 경로)이지만, 파일이 있는데
# 읽지 못하면(디렉토리·권한·I/O) 어느 스위트를 돌아야 하는지 알 수 없다 — 그때 폴백으로 넘어가면
# 채택자는 자기 러너가 돈 줄 안다. sed 를 파이프에 물리면 rc 가 tail 것으로 덮여 이 구분이 불가능하므로
# 파이프 없이 실행해 sed 자신의 rc 를 받는다.
# --8<-- test_cmd 해소 시작 (tests/test_run_tests_hook_test_cmd.py 파서 동형성 가드가 이 구간을 떼어 돌린다)
read_test_cmd_lines() {
    sed -n -e 's/[[:space:]]*$//' \
        -e 's/^[[:space:]]*test\.cmd[[:space:]]*=[[:space:]]*//p' \
        "$1"
}

# conf 의 **키 이름만** 뽑는다(주석 줄 제외·값은 보지 않는다) — 구표기 판정의 입력.
read_conf_keys() {
    sed -n -e '/^[[:space:]]*#/d' \
        -e 's/^[[:space:]]*\([^=]*[^=[:space:]]\)[[:space:]]*=.*$/\1/p' \
        "$1"
}

# 차단 구키 목록은 엔진이 **생성**한다(손으로 복제하면 표와 훅이 갈린다):
#   python3 .project_manager/tools/local_conf.py --render-adapter-block sh
# 생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)
legacy_conf_keys='additional_reviewer_enabled additional_reviewer_incomplete_round_limit additional_reviewer_round_limit additional_reviewer_wave_budget ctx_nudge_pct ctx_stop_pct ctx_window_tokens date delegate_enabled delegate_idle_timeout delegate_timeout external_review_enabled external_review_idle_timeout external_review_incomplete_round_limit external_review_progress_signal external_review_round_limit external_review_timeout external_review_wave_budget opencode_pro_model project_name project_root project_tagline py regression_min_collected review_denylist_extra review_paths review_rounds_max reviewer_cmd reviewer_env_keep_extra reviewer_home_artifacts_extra test_cmd upstream upstream_rev upstream_seen_rev user'
legacy_conf_key_prefix='ctx_window_tokens_'
# 생성 끝 — 차단 구키

conf_file="$repo_root/.project_manager/local.conf"
conf_error=""
legacy_found=""
test_cmd=""
if [ -e "$conf_file" ]; then
    # 1패스: 읽기 가능성 판정. `2>&1 >/dev/null` 로 stderr 만 받아(값은 버린다) sed 자신의 rc 를 얻는다
    #   — 파이프를 물리면 rc 가 파이프라인 마지막 명령 것으로 덮여 이 판정이 불가능하다.
    conf_stderr=$(read_test_cmd_lines "$conf_file" 2>&1 >/dev/null)
    conf_rc=$?
    if [ "$conf_rc" -ne 0 ]; then
        conf_error=$(printf '%s\n' "$conf_stderr" | tail -1)
        [ -n "$conf_error" ] || conf_error="rc $conf_rc"
    else
        # 2패스: 구표기 판정을 **값 해소 앞**에 둔다. 뒤에 두면 `test_cmd=` 가 신표기가 아니라는
        #   이유로 빈 값이 되고, 훅은 그것을 "미지정" 으로 읽어 고정 폴백 러너를 조용히 고른다 —
        #   채택자는 자기 러너가 돈 줄 안다(엔진 소비 지점의 fail-loud 와 같은 규율).
        for _key in $(read_conf_keys "$conf_file" 2>/dev/null); do
            case " $legacy_conf_keys " in
                *" $_key "*) legacy_found="$legacy_found $_key" ;;
            esac
            case "$_key" in
                "$legacy_conf_key_prefix"?*) legacy_found="$legacy_found $_key" ;;
            esac
        done
        # 3패스: 값 추출. `tail` 은 반드시 명령치환 *안*에서 돌린다 — 밖에서 돌리면 치환이 후행 개행을
        #   먼저 삼켜 "마지막 값이 빈 값"(해제) 케이스가 직전 값으로 되살아난다.
        if [ -z "$legacy_found" ]; then
            test_cmd=$(read_test_cmd_lines "$conf_file" 2>/dev/null | tail -1)
        fi
    fi
fi
# --8<-- test_cmd 해소 끝

# 러너 출력은 변수로 받고 종료코드를 *따로* 캡처한다. `러너 | tail -1` 파이프로 받으면 파이프라인 rc 가
# tail 의 것(항상 0)이라 러너 종료코드가 버려진다 — `test.cmd=false` 같은 무출력 실패가 빈 메시지 +
# 정상으로 위장돼 위의 '조용한 실패 금지'를 스스로 깬다. 요약(마지막 줄 추출)은 rc 를 확보한 뒤 한다.
if [ -n "$conf_error" ]; then
    result="local.conf 를 읽지 못해 러너를 해소하지 못했다 ($conf_error)"
elif [ -n "$legacy_found" ]; then
    result="local.conf 에 구표기 키가 남아 러너를 해소하지 못했다 (${legacy_found# }) — 새 표기로 교체하라"
else
    if [ -n "$test_cmd" ]; then
        output=$(sh -c "$test_cmd" 2>&1)
        rc=$?
    else
        # 폴백은 채택자 문자열이 아니라 고정 argv 라 셸 재파싱 대상이 아니다(그대로 실행).
        output=$("$py" -m pytest tests/ -q --no-header 2>&1)
        rc=$?
    fi
    result=$(summarize_run "$rc" "$output")
fi

# systemMessage JSON 출력 — 외부 프로세서 대체(json.dumps 가 escape 안전 보장·result 는 argv 로 전달).
"$py" -c 'import json, sys; sys.stdout.write(json.dumps({"systemMessage": "tests: " + sys.argv[1]}))' "$result"
