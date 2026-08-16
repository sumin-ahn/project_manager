"""T-0426 — PM 에게 지시하는 git mutation 문서에 **경로 스코프**가 살아 있는지 (ADR-0074).

엔진을 좁혀도(T-0425) **문서가 bare `git commit` 을 지시하면 사람 손이 그대로 샌다** — 유출 경로가
add 와 commit 두 갈래이듯, 표면도 엔진과 문서 두 갈래다. T-0426 이 스킬 문서·방법론 문서·진입 doc
전 사본의 지시문을 pathspec 형으로 바꿨는데 **그 변경을 무는 테스트가 0 건이었다**: reviewer 가
pm-wave-finish SKILL 4사본 + pm_playbook 4사본을 옛 버전(bare commit·`git add -A` 명문)으로 되돌리고
전체 회귀를 돌렸더니 **숫자가 그대로였다**. T-0098(폐기 용어 sweep 을 `test_terminology` 로 못박은
선례)과 같은 패턴으로 닫는다 — 재발하는 규칙은 지식이 아니라 테스트로 못박는다.

**가드 자신도 한 번 무뎠다**(reviewer 실측 우회 4종·라운드 3). 그 교훈이 지금 형태를 정한다:

  A. *"문단에 ` -- ` 가 있으면 그 문단의 bare 지시를 면제"* → **면제가 너무 헐거웠다**. 올바른 예시
     한 줄이 같은 문단의 bare 지시를 통째로 사면했다. 지금은 문단이 아니라 **그 언급 자신의 좁은
     창** 만 보고, 산문 증거로 ` -- ` 를 **받지 않는다**(같은 창의 다른 커맨드 것일 수 있다).
  B/C. blanket stage 어휘가 `-A`/`--all`/`.` 뿐이라 **`git add -u`**(추적분 전량)와
     **`git add -A -- .`**(pathspec 형식만 갖춘 blanket)가 통과했다. 지금은 옵션 축과 pathspec
     **대상** 축을 함께 봐서 *실제로 blanket 인 형태*를 전부 담는다.
  D. 스캔이 `SKILL.md` 한정이라 스킬 디렉토리의 다른 `.md`(reference·예시)가 사각이었다. 지금은
     스킬 디렉토리 **전 `.md`** 를 본다.

정당한 예외는 **구조적으로만** 둔다(문자열 allow-list 로 개별 문장을 봐주지 않는다):
  - **frontmatter** — `description:` 은 트리거 문장이지 실행 지시가 아니다(YAML 블록 통째 제외).
  - **`--no-verify`** — 보호훅 *비커버* 범위 서술(ADR-0071). 커밋하라는 지시가 아니다.
  - **``bare `git commit` ``** — 금지 대상을 *가리키는* 규정문("bare commit 은 …까지 싣는다").
    지시가 아니라 지시의 반례라 규칙 언급을 따로 요구하지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ── 스캔 표면 ─────────────────────────────────────────────────────────────
# PM 이 읽고 **그대로 따라 치는** 문서 전량 — canonical(루트) + 하네스 템플릿 3종.
# 스킬은 `SKILL.md` 한정이 아니라 **디렉토리 전 `.md`**(reference·예시도 PM 이 읽는다·우회 D).
# codex 는 스킬 네임스페이스가 `.agents/skills`(ADR-0054 remap)라 양쪽을 다 본다.
_SURFACE_GLOBS = (
    ".claude/skills/**/*.md",
    ".claude/agents/*.md",
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    "templates/*/.claude/skills/**/*.md",
    "templates/*/.agents/skills/**/*.md",
    "templates/*/.claude/agents/*.md",
    "templates/*/.project_manager/wiki/pm_role.md",
    "templates/*/.project_manager/wiki/pm_playbook.md",
    "templates/*/CLAUDE.md",
    "templates/*/CLAUDE.lite.md",
    "templates/*/AGENTS.md",
    "templates/*/AGENTS.lite.md",
)

# 사본이 늘거나(하네스 추가) 글롭이 어긋나 **아무것도 안 읽는 false-green** 을 막는 하한.
_MIN_SURFACE_FILES = 60

# 산문 언급의 증거를 찾는 창 — 줄바꿈으로 접힌 한 문장을 덮되(실측 최대 ~50자), 옆 문단의
# 무관한 규칙 언급까지 끌어오지 않을 만큼 좁게. 넓히면 우회 A 가 되살아난다.
_MENTION_WINDOW = 60

_PATHSPEC_SEP = re.compile(r"\s--(\s|$)")
# 커맨드 호출 = `git commit` 뒤에 곧바로 옵션이 오는 형태. 백틱/펜스 어디에 있든 잡는다.
_COMMIT_INVOCATION = re.compile(r"git commit\s+-[^\n`]*")
_COMMIT_MENTION = re.compile(r"git commit")
# 산문이 "경로 스코프"를 실제로 말하고 있다는 증거 어휘. ` -- ` 는 **증거로 치지 않는다** —
# 같은 창의 다른 커맨드(`git add -A -- <경로>`)의 것일 수 있다(우회 A 의 정확한 수법).
_SCOPE_RULE_TOKENS = ("pathspec", "경로를 명시", "경로 명시", "선언한 경로", "선언된 경로")
# 규정문 마커 — "bare `git commit` 은 …" 은 금지 대상을 가리키는 반례라 지시가 아니다.
_PROHIBITION_PREFIX = re.compile(r"bare\s+`?$")

# `git add` 뒤에 **커맨드 경계**(공백·백틱·줄끝)를 요구한다 — 그래야 산문의 문장부호
# (`… 산출만 git add.`)를 인자 `.` 로 오독하지 않는다(spike-new 실측 오탐).
# `git stage` 는 `git add` 의 **정식 git 별칭**이라 같은 축으로 본다 — 별칭으로 우회되면
# 가드가 막으려는 blanket 이 그대로 통과한다(PM 적대 probe 실측·T-0426 라운드 3).
_ADD_INVOCATION = re.compile(r"git (?:add|stage)(?=[\s`]|$)[^\n`]*")
# blanket = 대상을 열거하지 않고 "전부" 를 올리는 형태. 옵션 축과 pathspec 축을 함께 본다.
_BLANKET_FLAGS = re.compile(r"(?:^|\s)(-A|--all|-u|--update)(?=\s|$)")
# "트리 전체" 를 가리키는 pathspec 토큰. `git add -A -- .` 처럼 **형식만 pathspec** 인 형태를
# 잡으려면 옵션이 아니라 이 대상 축을 봐야 한다(우회 C).
_EVERYTHING_TOKENS = frozenset({".", "./", "*", ":/", ":/*"})
# 쉘 brace expansion — bash 는 전개하지만 dash/sh·PowerShell 은 **미전개**라 그대로
# `pathspec did not match` rc=1 로 커밋 전체가 죽는다(reviewer 실측·Windows 명시 지원 프로젝트).
_BRACE_EXPANSION = re.compile(r"\{[^}\s]*,[^}\s]*\}")


def surface_files(root: Path = REPO) -> list[Path]:
    files: list[Path] = []
    for pattern in _SURFACE_GLOBS:
        files += root.glob(pattern)
    return sorted(set(files))


def strip_frontmatter(text: str) -> str:
    """선두 YAML frontmatter 를 **줄 수를 보존하며** 비운다 (행번호 보고 유지)."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    head = text[: end + len("\n---\n")]
    return "\n" * head.count("\n") + text[end + len("\n---\n"):]


def join_continuations(text: str) -> str:
    """쉘 줄바꿈 이음(`\\` + 개행)을 한 줄로 접는다 — 여러 줄 pathspec 이 잘려 보이지 않게."""
    return re.sub(r"\\\n\s*", " ", text)


def _lineno(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ══════════════════════════════════════════════════════════════════════════
# 판정 (순수 함수 — 우회 회귀 케이스가 파일 없이 직접 호출한다)
# ══════════════════════════════════════════════════════════════════════════

def commit_invocation_offenders(rel: str, text: str) -> list[str]:
    """축 1 — `git commit -…` **호출마다** ` -- <경로>` 가 붙어 있어야 한다.

    per-chunk 가 아니라 per-invocation 이다: 같은 문단의 다른 올바른 커맨드가 bare 호출을
    가려주면 가드가 무뎌진다.
    """
    joined = join_continuations(strip_frontmatter(text))
    out: list[str] = []
    for match in _COMMIT_INVOCATION.finditer(joined):
        command = match.group(0)
        if "--no-verify" in command:
            continue                       # 보호훅 비커버 서술(ADR-0071) — 지시 아님
        if not _PATHSPEC_SEP.search(command):
            out.append(f"{rel}:{_lineno(joined, match.start())}: {command.strip()[:110]}")
    return out


def prose_mention_offenders(rel: str, text: str) -> list[str]:
    """축 2 — 커맨드 형태가 아닌 `git commit` 지시도 **그 언급 자신이** 경로 규칙을 말해야 한다.

    옛 문서가 정확히 이 형태였다(`3. **git commit** — 메시지: …`) — 커맨드가 아니라 축 1 에
    안 잡히면서 PM 에겐 "그냥 커밋해라" 로 읽힌다. 판정 단위는 **문단이 아니라 언급 주변의
    좁은 창**이다: 문단 단위 면제는 올바른 예시 한 줄이 같은 문단의 bare 지시를 통째로
    사면해 버린다(reviewer 우회 A). 증거로 ` -- ` 는 받지 않는다 — 다른 커맨드의 것일 수 있다.
    """
    stripped = strip_frontmatter(text)
    out: list[str] = []
    for match in _COMMIT_MENTION.finditer(stripped):
        start, end = match.span()
        if _COMMIT_INVOCATION.match(stripped, start):
            continue                       # 축 1 관할(호출 형태)
        before = stripped[max(0, start - _MENTION_WINDOW):start]
        if _PROHIBITION_PREFIX.search(before):
            continue                       # "bare `git commit` 은 …" = 금지 대상 서술
        window = before + stripped[end:end + _MENTION_WINDOW]
        if any(token in window for token in _SCOPE_RULE_TOKENS):
            continue
        if "--no-verify" in window:
            continue
        line = stripped[:end].rsplit("\n", 1)[-1] + stripped[end:].split("\n", 1)[0]
        out.append(f"{rel}:{_lineno(stripped, start)}: {line.strip()[:110]}")
    return out


def blanket_add_offenders(rel: str, text: str) -> list[str]:
    """축 3 — 대상을 열거하지 않는 **blanket stage** 를 문서가 명문화하지 않는다.

    blanket 은 두 축으로 성립한다 — ① 옵션(`-A`·`--all`·`-u`·`--update`: 전량) ②
    pathspec 대상이 `.`/`./`/`*`/`:/`(트리 전체). 어느 한 축만 보면 새는데, 실제로
    `git add -u`(옵션 축 누락)와 `git add -A -- .`(pathspec 형식만 갖춘 blanket)가
    옛 가드를 통과했다(reviewer 우회 B·C).
    """
    joined = join_continuations(strip_frontmatter(text))
    out: list[str] = []
    for match in _ADD_INVOCATION.finditer(joined):
        # 후행 구두점만 턴다 — `.` 은 **절대 안 턴다**(`-- .` 의 대상 자체가 `.` 이라
        # 털면 우회 C 가 그대로 통과한다·실측으로 걸린 함정).
        command = match.group(0).strip().rstrip("`,)")
        head, sep, target = command.partition(" -- ")
        args = head[len("git add"):]
        tokens = target.split() if sep else args.split()
        if sep:
            # `--` 뒤가 비었거나(`git add -A --`) 트리 전체를 가리키면 blanket 이다.
            offending = not tokens or any(tok in _EVERYTHING_TOKENS for tok in tokens)
        else:
            offending = bool(tokens) and (bool(_BLANKET_FLAGS.search(args))
                                          or any(tok in _EVERYTHING_TOKENS for tok in tokens))
        if offending:
            out.append(f"{rel}:{_lineno(joined, match.start())}: {command[:110]}")
    return out


def brace_expansion_offenders(rel: str, text: str) -> list[str]:
    """축 4 — 문서 커맨드에 쉘 brace expansion 을 쓰지 않는다 (이식성).

    bash 는 `{claimed,done}` 을 전개하지만 dash/`sh`·PowerShell 은 **미전개**로 그대로 넘겨
    `pathspec did not match` rc=1 로 커밋 전체가 죽는다(reviewer 실측). 이 프로젝트는
    Windows 를 명시 지원한다(진입 doc Windows 노트) — 두 경로로 풀어써야 한다.
    """
    joined = join_continuations(strip_frontmatter(text))
    out: list[str] = []
    for match in re.finditer(r"git (?:commit|add)\b[^\n`]*", joined):
        command = match.group(0)
        if _BRACE_EXPANSION.search(command):
            out.append(f"{rel}:{_lineno(joined, match.start())}: {command.strip()[:110]}")
    return out


_CHECKS = (
    ("pathspec 없는 `git commit` 호출", commit_invocation_offenders),
    ("경로 규칙을 말하지 않는 `git commit` 지시", prose_mention_offenders),
    ("blanket `git add` 명문", blanket_add_offenders),
    ("이식성 없는 brace expansion", brace_expansion_offenders),
)


@pytest.fixture(scope="module")
def surfaces() -> list[tuple[str, str]]:
    files = surface_files()
    assert len(files) >= _MIN_SURFACE_FILES, (
        f"스캔 표면이 {len(files)}개뿐 — 글롭이 어긋났거나 트리가 바뀌었다(false-green 방지 하한 "
        f"{_MIN_SURFACE_FILES}). 글롭: {_SURFACE_GLOBS}")
    return [(p.relative_to(REPO).as_posix(), p.read_text(encoding="utf-8")) for p in files]


# ── 표면 커버리지 ─────────────────────────────────────────────────────────

def test_surface_covers_every_copy_of_the_pathspec_skills(surfaces):
    """지시문을 담은 스킬 3종이 **모든 사본**에서 스캔된다 — 한 사본만 보면 sweep 을 못 지킨다.

    `>= 4`(루트 + 하네스 3종)로 둔다 — `== 4` 면 **네 번째 하네스를 추가하는 순간 무조건
    red** 라 가드가 정당한 확장을 막는다(reviewer should-fix).
    """
    scanned = {rel for rel, _ in surfaces}
    for skill in ("pm-wave-finish", "pm-handoff", "pm-adr"):
        copies = sorted(rel for rel in scanned if f"/{skill}/SKILL.md" in rel)
        assert len(copies) >= 4, (
            f"{skill} 사본이 4개(루트 + 하네스 3종) 미만 — {len(copies)}개: {copies}")
    for entry in ("templates/claude_code/CLAUDE.md", "templates/opencode/AGENTS.md",
                  "templates/codex/AGENTS.md", "templates/claude_code/CLAUDE.lite.md",
                  "templates/opencode/AGENTS.lite.md"):
        assert entry in scanned, f"진입 doc 미스캔: {entry}"


def test_surface_scans_non_skill_md_inside_skill_dirs(tmp_path):
    """스킬 디렉토리의 **`SKILL.md` 아닌 `.md`** 도 스캔된다 (reviewer 우회 D).

    reference·예시 문서도 PM 이 읽는다. `SKILL.md` 한정이면 옆 파일에 bare 지시를 두는
    것으로 가드 전체가 무력화된다.
    """
    for rel in (".claude/skills/pm-fake/SKILL.md",
                ".claude/skills/pm-fake/reference.md",
                "templates/codex/.agents/skills/pm-fake/examples.md"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    found = {p.relative_to(tmp_path).as_posix() for p in surface_files(tmp_path)}
    assert ".claude/skills/pm-fake/reference.md" in found, found
    assert "templates/codex/.agents/skills/pm-fake/examples.md" in found, found


# ── 실 표면 판정 ──────────────────────────────────────────────────────────

def test_git_commit_invocations_declare_a_pathspec(surfaces):
    offenders = [o for rel, text in surfaces for o in commit_invocation_offenders(rel, text)]
    assert not offenders, (
        "pathspec 없는 `git commit` 지시 — bare commit 은 *남이 stage 해 둔 것*까지 싣는다"
        "(ADR-0074). `-- <그 단계가 만든 경로>` 를 붙여라:\n  " + "\n  ".join(offenders))


def test_git_commit_prose_instructions_state_the_scope_rule(surfaces):
    offenders = [o for rel, text in surfaces for o in prose_mention_offenders(rel, text)]
    assert not offenders, (
        "`git commit` 을 언급하면서 그 자리에서 경로 스코프를 말하지 않는 지시 — PM 은 이걸 "
        "bare commit 지시로 읽는다(ADR-0074). 실값 경로를 주거나 pathspec 규칙을 명시하라:\n  "
        + "\n  ".join(offenders))


def test_no_blanket_git_add_in_pm_facing_docs(surfaces):
    offenders = [o for rel, text in surfaces for o in blanket_add_offenders(rel, text)]
    assert not offenders, (
        "blanket `git add` 명문 — 그 아래 남의 미완성 편집까지 stage 된다(ADR-0074). "
        "`-- <선언 경로>` 를 붙이거나 스코프 서술로 바꿔라:\n  " + "\n  ".join(offenders))


def test_documented_git_commands_are_portable(surfaces):
    offenders = [o for rel, text in surfaces for o in brace_expansion_offenders(rel, text)]
    assert not offenders, (
        "쉘 brace expansion — dash/sh·PowerShell 은 미전개라 `pathspec did not match` rc=1 로 "
        "커밋 전체가 죽는다. 경로를 풀어써라:\n  " + "\n  ".join(offenders))


# ══════════════════════════════════════════════════════════════════════════
# 우회 회귀 (reviewer 실측 4종 — 각각이 red 여야 한다)
# ══════════════════════════════════════════════════════════════════════════

# 우회 A 는 **면제를 훔치는** 수법이라 정직하게 재현한다: 올바른 pathspec 규칙 서술과 올바른
# 예시 커맨드가 이미 있는 문단 **바로 옆**에 bare 지시를 끼워 넣는다. 문단 단위 면제였던 옛
# 가드는 이 배치에서 통째로 침묵했다.
_BYPASS_A = """\
8. **PM 손 잔여** — **git commit — pathspec 명시**. bare `git commit` 은 남이 stage 해 둔
   변경까지 싣는다:
   ```
   git commit -m "T-NNNN — <요약>" -- <touches> .project_manager/wiki/log/current.md
   ```
- 부기 끝나면 git commit 으로 마무리한다 (규칙은 `git add -A -- <경로>` 참고).
"""
_BYPASS_B = "- git stage — 변경 파일 전량 `git add -u` 로 올린다.\n"
_BYPASS_C = "- git stage — `git add -A -- .` 로 전부 올린다.\n"
_BYPASS_D_DOC = '- 부기 끝나면 `git commit -m "T-NNNN"` 으로 마무리한다.\n'


def test_bypass_a_bare_instruction_next_to_a_correct_example_is_red():
    """우회 A — 문단에 ` -- ` 가 있어도 **bare 지시 자신**이 규칙을 안 말하면 red."""
    offenders = prose_mention_offenders("doc.md", _BYPASS_A)
    assert any("부기 끝나면 git commit" in o for o in offenders), offenders
    # 같은 본문의 *정당한* 것(pathspec 명시 지시·bare 규정문·pathspec 붙은 호출)은 red 아님.
    assert len(offenders) == 1, offenders
    assert not commit_invocation_offenders("doc.md", _BYPASS_A)


def test_bypass_b_git_add_u_is_red():
    """우회 B — `git add -u` 는 추적분 **전량** blanket 이다(옛 가드는 `-A`/`.` 만 봤다)."""
    assert blanket_add_offenders("doc.md", _BYPASS_B), "git add -u 미검출"


def test_bypass_c_pathspec_shaped_blanket_is_red():
    """우회 C — `git add -A -- .` 는 pathspec **형식만** 갖춘 blanket 이다."""
    assert blanket_add_offenders("doc.md", _BYPASS_C), "git add -A -- . 미검출"


def test_bypass_d_bare_commit_in_non_skill_md_is_red(tmp_path):
    """우회 D — 스킬 디렉토리의 `reference.md` 에 둔 bare 지시도 스캔·검출된다."""
    path = tmp_path / ".claude/skills/pm-fake/reference.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BYPASS_D_DOC, encoding="utf-8")
    scanned = surface_files(tmp_path)
    assert path in scanned, [str(p) for p in scanned]
    offenders = [o for p in scanned
                 for o in commit_invocation_offenders(str(p.relative_to(tmp_path)),
                                                      p.read_text(encoding="utf-8"))]
    assert offenders, "reference.md 의 bare commit 미검출"


def test_brace_expansion_is_red():
    """lite 진입 doc 이 실제로 갖고 있던 형태 — sh/PowerShell 에서 rc=1 로 죽는다."""
    doc = ("`git commit -m \"T-NNNN\" -- .project_manager/wiki/tickets/"
           "{claimed,done}/T-NNNN-<slug>.md`\n")
    assert brace_expansion_offenders("doc.md", doc), "brace expansion 미검출"


def test_scoped_forms_stay_green():
    """정당한 형태는 green — 가드가 올바른 문서를 막지 않는다(과차단 방지)."""
    ok = (
        'git commit -m "T-0001 — x" -- src/a.py .project_manager/wiki/log/current.md\n'
        "git add .project_manager/wiki/decisions/0001-x.md\n"
        "git add <신규/변경 경로>\n"
        "git add -A -- src/a.py\n"
        "미리 `git add` 한 staged 상태를 스냅샷한다\n"
        "- **비커버**: `git commit --no-verify` · merge 커밋\n"
        "- **git commit — pathspec 명시**: 아래 경로만 싣는다\n"
    )
    for label, check in _CHECKS:
        assert not check("doc.md", ok), (label, check("doc.md", ok))
