"""상류 git 실행·URL 캐시·rev·추가 하네스 소스 해소 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.domain import upstream_rules
from pm_engine.infra import fs
from pm_engine.infra import install_git
from pm_engine.infra import install_source
import hashlib
import os
import shutil
import subprocess
import sys


# upstream git 호출(ls-remote·remote get-url·rev-parse) 타임아웃 (초). ls-remote 는
# 네트워크라 ls-files 보다 넉넉히 — pm_config.GIT_TIMEOUT_SECONDS(clone·600)보단 짧게(도달성
# 체크는 clone 만큼 길 필요 없음·과대 대기 방지).
UPSTREAM_GIT_TIMEOUT_SECONDS = 60


# URL upstream 캐시 clone/fetch 타임아웃 (초). 조회(ls-remote)와 비용 축이 다르다 — 이 제품
# repo 를 `file://` 로 전체 clone 하면 로컬 디스크에서도 2.87초(트리 40MB)고, 원격은 회선에
# 달렸다. 60초로 자르면 정상 원격 clone 이 중간에 끊긴다(pm_config 의 bare clone 상한과 같은 축).
UPSTREAM_CLONE_TIMEOUT_SECONDS = 600


# upstream(네트워크-facing) git 호출의 config 격리 키 (codex MF4·worktree_pool GIT_CONFIG_*
# 선례). untrusted URL 의 ls-remote/rev-parse 에 사용자/global git config 의 `insteadOf`
# rewrite·credential helper 가 끼어드는 것을 막는다(defense-in-depth). GIT_CONFIG_GLOBAL/
# SYSTEM=/dev/null 로 global·system config 를 통째 무력화하고, GIT_CONFIG_COUNT 패턴으로
# protocol allowlist(https/ssh/file 만 always·나머지 never) + credential.helper=(빈값·helper
# 미경유)를 강제한다. (분류 검증[validate]은 *형태* 안전, 이 env 는 *실행* 격리 — 이중 방어.)
_UPSTREAM_GIT_CONFIG_KV = (
    ("credential.helper", ""),          # credential helper 미경유(자격증명 자동주입 차단).
    ("protocol.allow", "never"),        # 기본 거부 — 아래 명시 protocol 만 허용(allowlist).
    ("protocol.https.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("protocol.file.allow", "always"),
    ("http.followRedirects", "false"),  # redirect 추적 차단(잔여 SSRF 표면).
)


def _real_upstream_git_runner(*, timeout: int = UPSTREAM_GIT_TIMEOUT_SECONDS) -> install_git.GitRunner:
    """upstream git 호출(ls-remote·remote get-url·rev-parse·캐시 clone/fetch)용 GitRunner (fail-soft).

    `_real_git_runner` 와 달리 `-C <dest>` 로 고정하지 않는다 — 호출부가 `-C <checkout>`·
    `ls-remote <url>` 등 컨텍스트를 argv 로 직접 준다. URL 안전 계약 강제:
      - argv-list(no shell) — subprocess 가 리스트로 받아 셸 해석 0.
      - `GIT_TERMINAL_PROMPT=0` — 비대화 auth(자격증명 프롬프트로 멈추지 않게·CI 안전).
      - **config 격리(MF4)** — `GIT_CONFIG_GLOBAL/SYSTEM=os.devnull` 로 global·system config 를
        무력화(사용자 `insteadOf` rewrite·credential helper 가 untrusted URL 호출에 끼는 것 차단)
        + `GIT_CONFIG_COUNT` 패턴으로 protocol allowlist(https/ssh/file 만)·credential.helper=
        (빈값) 강제. `_real_git_runner`(로컬 추적집합 조회)와 달리 *네트워크-facing* 이라 격리한다.
      - timeout — 기본값은 조회(ls-remote·rev-parse) 기준 상한이고, 캐시 clone/fetch 처럼
        비용 축이 다른 호출은 호출부가 `timeout=` 으로 자기 상한을 준다(runner 사본 0).
    git 바이너리 부재·예외는 (1, stderr) 로 감싼다(호출부가 rc 로 판정).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # 비대화 auth — 자격증명 프롬프트로 멈추지 않는다.
        # MF4: global/system git config 무력화 — insteadOf rewrite·credential helper 가
        # untrusted URL 의 ls-remote/rev-parse 에 끼는 것을 막는다(os.devnull = Win/POSIX 공통).
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        # hardening(codex): 상속된 protocol 우회 env 제거(방어층) — GIT_ALLOW_PROTOCOL 은
        # protocol.allow allowlist 를 우회하고, GIT_PROTOCOL_FROM_USER 는 user-given protocol
        # 게이트를 푼다. 둘 다 pop 해 우리 GIT_CONFIG allowlist 가 단일 권위가 되게 한다.
        env.pop("GIT_ALLOW_PROTOCOL", None)
        env.pop("GIT_PROTOCOL_FROM_USER", None)
        # GIT_CONFIG_COUNT 패턴으로 protocol allowlist·credential.helper=(빈값) 주입(`-c` 동치·
        # worktree_pool 선례). 기존 env 의 GIT_CONFIG_* 잔여는 우리 카운트로 덮어 결정론 보장
        # (env 격리는 sub-process[ssh transport]까지 전파돼 argv `-c` 보다 안전 — env 단일 채널).
        config_kv = _UPSTREAM_GIT_CONFIG_KV
        if os.name == "nt":
            # Windows Git의 pack 임시 파일도 긴 캐시 경로에서 만들 수 있어야 한다.
            config_kv += (("core.longpaths", "true"),)
        for idx, (key, val) in enumerate(config_kv):
            env[f"GIT_CONFIG_KEY_{idx}"] = key
            env[f"GIT_CONFIG_VALUE_{idx}"] = val
        env["GIT_CONFIG_COUNT"] = str(len(config_kv))
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부 위임.
            return 1, str(exc)

    return runner


def derive_origin_url(checkout_root: Path, *, git_runner: install_git.GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git remote get-url origin` 을 읽어 URL 을 도출한다.

    로컬 clone 을 `--from` 으로 받았을 때, future update 기록(`--upstream` 생략 시)을 *그
    checkout 경로* 대신 origin URL 로 자동도출하는 데 쓴다(릴리스 추적 기본).
    rev-parse 와 동일 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0)을 `_real_upstream_
    git_runner` 가 강제한다. git repo 아님·origin 부재·도출 URL 이 검증 실패 → None(graceful·
    호출부가 경로 fallback). 도출 URL 도 `validate_upstream_value` 로 fail-closed 검증.
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not url:
        return None
    ok, _reason = upstream_rules.validate_upstream_value(url)
    if not ok:
        return None
    return url


def read_upstream_rev(checkout_root: Path, *, git_runner: install_git.GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git rev-parse HEAD` 를 읽는다 — drift baseline.

    `upstream.rev=<commit>` baseline 기록의 입력이다. checkout_root 가 가리키는
    로컬 git work tree 의 현재 HEAD commit 을 읽는다 — git repo 아님·HEAD 해소 실패는 None
    (graceful·기록 생략). 호출부는 이미 로컬 트리로 해소된 소스를 준다(URL upstream 이면 이
    실행이 clone/fetch 한 캐시 checkout).
    안전 계약은 `_real_upstream_git_runner`(argv-list·timeout·GIT_TERMINAL_PROMPT=0).
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "rev-parse", "HEAD"])
    if rc != 0:
        return None
    rev = out.strip().splitlines()[0].strip() if out.strip() else ""
    return rev or None


# ── URL upstream 의 로컬 캐시 checkout (엔진 소유) ──────────
# 엔진은 파일을 *로컬 트리에서* 복사한다. upstream 이 URL 이면 그 로컬 트리를 엔진이 직접 만든다 —
# 최초 실행이 clone, 이후 실행이 fetch + 작업트리 전진이다. 캐시가 채택자 트리 밖으로 나가지
# 않도록 자리는 dest PM 홈 안 `.project_manager/.local/`(git-ignored) 하나로 고정한다.
#
# `fetch` 만으로는 부족하다 — fetch 는 remote-tracking ref 만 옮기고 작업트리 HEAD 는 그대로다.
# 복사 소스도 baseline rev(`read_upstream_rev`)도 작업트리 HEAD 를 보므로, 전진시키지 않으면
# 최초 clone 시점에 영원히 묶인 트리를 "최신 upstream" 이라 부르게 된다. 캐시는 엔진 소유라
# 보존할 로컬 편집이 없어 tracking ref 로 hard reset 한다(force-push 된 상류도 그대로 따라간다).
UPSTREAM_CACHE_RELDIR = Path(".project_manager") / ".local" / "upstream-cache"


# 캐시 디렉터리 이름 = URL 다이제스트 앞 16자. 경로 조립 입력이 URL 이므로 이름에 URL 문자를
# 그대로 쓰지 않는다(경로 탈출·플랫폼 금지문자 표면 0).
_UPSTREAM_CACHE_NAME_LEN = 16


class UpstreamCacheError(RuntimeError):
    """URL upstream 캐시 clone/fetch 실패 — 호출부가 rc≠0 으로 멈춘다.

    옛 캐시로 조용히 진행하지 않기 위해 별도 타입이다: 소스 해소의 다른 실패(FileNotFoundError)
    는 다음 후보로 넘어가도 되지만, 이 실패는 "받아야 할 상류를 못 받았다" 라 넘어갈 후보가 없다.
    """


def upstream_cache_dir(dest_root: Path, url: str) -> Path:
    """URL upstream 하나가 쓰는 캐시 checkout 자리 (dest PM 홈 안·git-ignored)."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:_UPSTREAM_CACHE_NAME_LEN]
    return Path(dest_root).resolve() / UPSTREAM_CACHE_RELDIR / digest


def resolve_upstream_url_cache(
    dest_root: Path, url: str, *, git_runner: install_git.GitRunner | None = None,
) -> Path:
    """URL upstream → 복사 소스로 쓸 로컬 캐시 checkout 경로 (clone 최초·fetch 이후).

    `pm-config update`(pm_update)·`add-harness`·`sync-adapter-config` 가 같은 자리를 부른다 —
    URL 을 로컬 트리로 바꾸는 코드는 엔진에 하나뿐이다(스킬·관리자·명령줄이 한 자리).

    git 호출은 `_real_upstream_git_runner`(argv-list·protocol allowlist·credential 미경유·
    `GIT_TERMINAL_PROMPT=0`)에 clone/fetch 상한을 준 것 하나다. 값 자체는 그 전에
    `validate_upstream_value` 로 다시 거른다(형태 안전은 호출부마다 다시 세우지 않는다).

    실패(clone·fetch·작업트리 전진 어느 단계든)는 `UpstreamCacheError` 로 올린다 — 옛 캐시로
    미끄러지면 낡은 트리를 상류라 부르며 baseline 까지 전진시킨다. `--filter` 없는 전체 clone 인
    이유는 어댑터 기준 diff 가 `template_rev` 시점 파일을 읽어야 하기 때문이다(이력 필요).
    """
    ok, reason = upstream_rules.validate_upstream_value(url)
    if not ok:
        raise UpstreamCacheError(f"upstream URL 을 캐시로 받을 수 없다: {reason}")
    runner = git_runner if git_runner is not None else _real_upstream_git_runner(
        timeout=UPSTREAM_CLONE_TIMEOUT_SECONDS)
    cache = upstream_cache_dir(dest_root, url)

    if (cache / ".git").is_dir():
        print(f"upstream URL 캐시 fetch: {url} → {cache}", file=sys.stderr)
        rc, out = runner(["-C", str(cache), "fetch", "origin"])
        if rc != 0:
            raise UpstreamCacheError(
                f"upstream 캐시 fetch 실패 (rc={rc}): {url} → {cache} — {out.strip()[:400]}")
        # 작업트리를 이번 fetch 결과로 전진시킨다(fetch 는 ref 만 옮긴다).
        rc, out = runner(["-C", str(cache), "reset", "--hard", "@{upstream}"])
        if rc != 0:
            raise UpstreamCacheError(
                f"upstream 캐시 작업트리 전진 실패 (rc={rc}): {cache} — {out.strip()[:400]}")
        return cache

    # 실패한 clone 의 잔재는 git 이 스스로 지운다(자기가 만든 목적지를 되돌린다 — 실측). 그래도
    # 남은 것(중단된 프로세스 등)이 있으면 clone 이 "빈 디렉터리 아님" 으로 loud 하게 실패하고
    # 그 문구가 아래 예외에 실린다 — 엔진이 대신 지우지 않는다(삭제는 사람이 한다).
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"upstream URL 캐시 clone: {url} → {cache} (최초·전체 clone)", file=sys.stderr)
    rc, out = runner(["clone", "--", url, str(cache)])
    if rc != 0:
        raise UpstreamCacheError(
            f"upstream 캐시 clone 실패 (rc={rc}): {url} → {cache} — {out.strip()[:400]}")
    return cache


def _resolve_add_harness_source(
    dest_root: Path, harness: str, explicit: Path | None,
) -> Path:
    """add_harness 의 어댑터 소스 checkout 을 해소한다.

    imported 인스턴스(scoped-core 사본·`templates/` 부재)는 dest 안에 어댑터
    소스 트리가 없다 — 소스는 그 인스턴스의 **upstream 프레임워크 checkout** 이다(add-harness 를
    *라이브 인스턴스*에 걸면 소스는 항상 그 인스턴스의 upstream 이다). 해소 우선순위:
      1. explicit(`--from`)      → 그대로 (기존 계약·override).
      2. dest local.conf upstream.path(경로) → 그 경로에 templates/<harness>/ 가 있으면 소스.
      3. dest 자신              → dest 에 templates/ 가 있으면 dest (framework-checkout 자기전환·
                                    REPO 하드 기본이 맞던 유일 케이스·현행 회귀 보존).
      4. dest local.conf upstream.path(URL) → 로컬 캐시 checkout(`resolve_upstream_url_cache` —
                                    clone/fetch)에 templates/<harness>/ 가 있으면 소스.
      5. 전부 실패              → 친화 FileNotFoundError (actionable).

    URL 캐시가 **로컬 소스보다 뒤**인 이유는 네트워크가 마지막 수단이기 때문이다 — dest 자신이
    프레임워크 checkout 이면 templates/ 가 이미 손 안에 있고, 그 형상에서 clone 을 먼저 걸면
    오프라인에서 되던 add-harness 를 새로 막는다. 캐시 clone/fetch 실패는 `UpstreamCacheError`
    로 그대로 올린다(다음 후보로 미끄러지지 않는다 — 받아야 할 상류를 못 받은 것이다).
    """
    if explicit is not None:
        return Path(explicit).resolve()
    dest_root = Path(dest_root).resolve()

    # dest 는 엔진 사본(templates 부재) — 소스는 upstream 프레임워크 checkout.
    local_conf = dest_root / ".project_manager" / "local.conf"
    upstream = ""
    if local_conf.is_file():
        try:
            upstream = conf_schema.parse(
                fs.read_text_shared(local_conf, encoding="utf-8")).get("upstream.path", "").strip()
        except (UnicodeDecodeError, OSError):
            upstream = ""
    kind = upstream_rules.classify_upstream(upstream) if upstream else ""
    if kind == "path":
        # 상대 경로는 인스턴스 루트(dest) 기준·절대면 그대로(pathlib: 절대 우변 승).
        candidate = (dest_root / upstream).resolve()
        if install_source._has_harness_templates(candidate, harness):
            return candidate

    # framework-checkout 자기전환(dest 에 templates/ 보유) — 현행 REPO 하드 기본의 회귀 보존.
    if install_source._has_harness_templates(dest_root, harness):
        return dest_root

    if kind == "url":
        cache = resolve_upstream_url_cache(dest_root, upstream)
        if install_source._has_harness_templates(cache, harness):
            return cache

    raise FileNotFoundError(
        f"add_harness 소스 미해소: {dest_root} 에 templates/ 가 없고, local.conf upstream.path "
        f"({upstream or '미등록'}) 에서도 templates/<harness> 를 찾지 못했다. "
        f"`--from <프레임워크 checkout>` 를 주거나 local.conf 의 upstream.path= 을 그 하네스 "
        f"어댑터를 가진 프레임워크 경로/URL 로 두라."
    )
