"""상류 설정 조회·검증·저장 진입 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
import sys
from pm_engine.app import repo_protection
from pm_engine.domain import conf_schema
from pm_engine.domain import upstream_rules
from pm_engine.domain import worktree_records
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import upstream_git


# ── upstream show/set  ───────────────────────────────────
# upstream 값(URL|경로 self-describing)을 조회/전환한다. set 은 검증(URL→ls-remote 도달성·
# 경로→존재+checkout)을 통과해야 local.conf 에 atomic 재기록(타 키 보존)된다 — fail-closed
# (나쁜 값 silently 기록 금지). URL 안전 계약(scheme allowlist·credential 거부·leading-dash·
# argv-list·timeout)은 domain.upstream_rules·infra.upstream_git, local.conf set-or-replace 는
# infra.install_conf 를 재사용한다(중복 구현 0).


def _validate_upstream_reachable(
    value: str,
    *,
    git_runner: worktree_records.GitRunner | None = None,
) -> tuple[bool, str]:
    """upstream 값 도달성 검증 — URL→`git ls-remote`·경로→존재+git checkout (fail-closed).

    upstream_rules.validate_upstream_value(순수 형태 안전·scheme/credential/dash)를 먼저 통과시킨 뒤
    *도달성*을 본다:
      - URL  → `git ls-remote <url>` rc==0(원격 reachable·argv-list·timeout·GIT_TERMINAL_PROMPT=0).
      - 경로 → 디렉토리 존재 + `git -C <path> rev-parse --is-inside-work-tree` rc==0(git checkout).
    git 호출은 upstream_git._real_upstream_git_runner(URL 안전 계약)를 기본으로, 테스트는 git_runner
    주입(라이브 git 0). (ok, reason). 실패는 모두 거부(fail-closed) — silently 기록 금지.
    """
    ok, reason = upstream_rules.validate_upstream_value(value)
    if not ok:
        return False, reason
    runner = git_runner if git_runner is not None else upstream_git._real_upstream_git_runner()
    kind = upstream_rules.classify_upstream(value)
    if kind == "url":
        # `--` 로 위치인자 종결(value 가 옵션으로 오인되지 않게·leading-dash 는 이미 거부됨이나 방어).
        rc, out = runner(["ls-remote", "--", value])
        if rc != 0:
            return False, (
                f"URL upstream 도달 불가 (`git ls-remote {value}` rc={rc}): {out.strip()[:200]} "
                "— URL/네트워크/접근권한 확인."
            )
        return True, ""
    # 경로 — 존재 + git work tree 검증.
    path = Path(value).expanduser()
    if not path.is_dir():
        return False, f"경로 upstream 이 디렉토리가 아니거나 존재하지 않음: {path}"
    rc, _out = runner(["-C", str(path), "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return False, (
            f"경로 upstream 이 git checkout 이 아님: {path} "
            "(`git rev-parse --is-inside-work-tree` 실패)."
        )
    return True, ""


def cmd_upstream(
    home,
    args: argparse.Namespace,
    *,
    git_runner: worktree_records.GitRunner | None = None,
) -> int:
    """`upstream show | set <value>` — upstream 값 조회/전환.

    - show: local.conf 의 현재 `upstream.path=` 값을 surface(미등록이면 안내).
    - set <value>: 검증(URL→ls-remote 도달성·경로→존재+checkout·fail-closed) 통과 후 local.conf
      `upstream.path=` atomic 재기록(타 키 보존). 나쁜 값은 거부(rc 1·기록 안 함). 값 self-describing
      이라 path↔URL 전환이 자동(스킬 freshness 분기가 모양으로 적응).

    local.conf set-or-replace(conf_schema._set_conf_keys·타 키·주석 보존)·URL 안전 검증(upstream_rules.
    validate_upstream_value)을 재사용한다(중복 0). git_runner 주입으로 hermetic 테스트
    (라이브 git/실 conf-write 없이 배선·검증 분기 검증).
    """
    local_conf = home.local_conf
    action = getattr(args, "upstream_action", None)

    if action == "show":
        if not local_conf.is_file():
            print(f"upstream: (local.conf 없음 — {local_conf})")
            return 0
        conf = conf_schema.parse(fs.read_text_shared(local_conf, encoding="utf-8"))
        value = conf.get("upstream.path", "").strip()
        if not value:
            print("upstream: (미등록) — `pm-config upstream set <url|path>` 로 설정하라.")
            return 0
        kind = upstream_rules.classify_upstream(value)
        print(f"upstream: {value}  ({kind})")
        return 0

    # set <value> — 검증 후 atomic 재기록(타 키 보존). fail-closed.
    value = args.value

    # suggestion(codex): local.conf 부재 확인을 *네트워크(reachability) 전* 으로 옮긴다 —
    #   기록 불가 상태(conf 없음)에서 굳이 ls-remote 로 네트워크를 치지 않게(낭비·지연 회피).
    if not local_conf.is_file():
        print(
            f"[중단] local.conf 없음 ({local_conf}) — 먼저 `pm-config init` 으로 셋업하라.",
            file=sys.stderr,
        )
        return 1

    ok, reason = _validate_upstream_reachable(value, git_runner=git_runner)
    if not ok:
        print(f"[중단] upstream 값 거부 (기록 안 함) — {reason}", file=sys.stderr)
        return 1

    try:
        install_conf._write_conf_keys(local_conf, {"upstream.path": value})
    except RuntimeError as exc:
        print(
            f"[중단] {exc}",
            file=sys.stderr,
        )
        return 1
    # upstream은 보호 push gate의 provenance 입력이다. 값이 같아도 끊긴/stale sidecar를
    # 자가치유하도록 등록 repo 전수를 중앙 설치 깔때기로 보낸다.
    repo_protection._refresh_protected_gate_contracts(home)
    kind = upstream_rules.classify_upstream(value)
    print(f"✓ upstream 설정: {value}  ({kind}) — pm_update 가 --from 생략 시 이 값을 쓴다.")
    return 0
