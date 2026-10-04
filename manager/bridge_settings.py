"""설정 탭의 브리지 메서드 — local.conf 편집과 어댑터 파일 절.

경계 두 줄(패키지 docstring)이 이 파일에서 구체화된다.

  - **쓰는 파일은 `local.conf` 하나**이고, 그 쓰기는 번들 엔진의 `pm_import._write_conf_keys`
    한 함수를 통한다(원자 교체 + 대상 conf 에서 유도한 배타락 + 실효값 검증). 두 번째 conf
    writer 를 만들지 않는다. 어댑터 수용·upstream 전환은 **그 사본의 명령**이 한다.
    사용 한도·압축 비율 키(`pm_import.is_harness_ctx_input_key`)가 든 저장도 그 사본의 명령
    (`pm-config ctx-apply --json --set 키=값 …`)이 한다 — 값 검사·local.conf 기록·하네스 설정 파일
    반영이 한 번에 돌고, 검사를 통과하지 못한 값은 어디에도 쓰이지 않는다.
  - 항목 목록의 정본은 **그 프로젝트 사본의** 키 레지스트리다
    (`<interpreter> <pm_home>/.project_manager/tools/local_conf.py --keys --json`).
    번들 레지스트리로 대신 그리지 않는다 — 세대가 다르면 키 집합이 다르다.

저장 직후에는 같은 사본을 `status --json` 으로 다시 읽어 그 판정을 돌려준다(낙관적 갱신 없음).
저장이 어댑터 카드에 박히는 키를 건드려도 여기서 렌더를 돌리지 않는다 — 재렌더 경로는
`pm-config update` 하나이고, 그 전량 동기는 갱신 탭의 일이다.

어댑터 절과 매트릭스는 **버튼으로만** 돈다(`adapters`·`adapter_matrix`). URL upstream 프로젝트는
소스 해소가 전체 clone 이라 탭을 여는 것만으로 네트워크가 나가면 안 된다.
"""
from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import Future
from pathlib import Path

from . import CallError, ENGINE_LOCK, load_engine_module
from . import registry, runner


# 이 키만 값 자체가 도달성 계약이라 conf writer 로 곧장 쓰지 않는다 — 전환은 그 사본의
# `pm-config upstream set` 이고(URL→ls-remote·경로→git checkout 확인·fail-closed), 그 명령이
# 같은 writer 로 커밋한다.
UPSTREAM_PATH_KEY = "upstream.path"


def _conf_path(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "local.conf"


def _pm_home(path: Path | str) -> Path:
    """등록된 PM 홈으로 해소한다 — 사본을 부르기 전에 절대화·표식 확인(runner 와 같은 규칙)."""
    home = Path(path).resolve()
    if not (home / ".project_manager").is_dir():
        raise CallError("invalid-path", f"PM 홈이 아니다 (.project_manager/ 없음): {home}")
    return home


def _run(home: Path, argv: list[str], *, timeout: int, input: str | None = None) -> subprocess.CompletedProcess:
    """그 사본의 스크립트 하나를 부른다 — rc 는 판정하지 않고 그대로 돌려준다.

    rc≠0 이 곧 실패인 자리(`--keys --json`)와 rc 자체가 산출인 자리(수용 거부·upstream 거부)를
    한 함수로 덮으려면 판정은 호출부가 해야 한다. 스폰 실패·응답 없음만 여기서 `CallError` 다.
    """
    try:
        return subprocess.run(
            argv, cwd=str(home), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=runner.CREATION_FLAGS,
            **({"input":input} if input is not None else {}))
    except subprocess.TimeoutExpired as exc:
        raise CallError("timeout", f"{timeout}초 안에 응답이 없다: {' '.join(argv)}") from exc
    except OSError as exc:
        raise CallError("spawn", f"인터프리터를 실행하지 못했다: {' '.join(argv)} ({exc})") from exc


def _tool(home: Path, filename: str) -> str:
    return str(home / ".project_manager" / "tools" / filename)


def _completed(completed: subprocess.CompletedProcess) -> dict:
    """rc·stdout·stderr 를 그대로 화면에 넘기는 자리 — 거부 문구는 엔진 원문이다."""
    return {"rc": completed.returncode, "stdout": completed.stdout,
            "stderr": completed.stderr}


# 사본의 `ctx-apply` 검사 거부 rc — 화면 오류 종류 `invalid-value` 로 넘기는 것은 이 값뿐이다.
# 처리하지 못한 예외로 끝난 파이썬의 1 과 겹치지 않는 전용 값이라, 그 밖의 rc(사본 안의 실패·그
# 명령을 모르는 옛 세대·인자 오류)는 저장 오류로 넘긴다.
CTX_APPLY_REJECTED_RC = 3


def _ctx_apply(home: Path, updates: dict) -> dict:
    """사용 한도 키가 든 저장 — 그 사본의 `pm-config ctx-apply --json --set 키=값 …` 한 번.

    값이 None 인 키는 `키=`(줄 삭제)로 보낸다. rc 0 이면 `{changed, conf, applied}`(conf 는 직후
    재조회), 검사 거부(rc 3)는 오류 종류 `invalid-value` 로 사본의 문구를 그대로 넘긴다. 그 밖의
    0 이 아닌 rc 는 `old-generation` 저장 오류이고 사본의 stderr 를 싣는다. 사본 명령은 관리자
    프로세스의 env 를 물려받으므로 opencode 전역 설정 위치는 사용자 env 로 해소된다."""
    argv = [*runner.interpreter(home), _tool(home, "pm_config.py"), "ctx-apply", "--json"]
    for key, value in updates.items():
        argv += ["--set", f"{key}={'' if value is None else value}"]
    completed = _run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS)
    if completed.returncode == CTX_APPLY_REJECTED_RC:
        return {"error": {"kind": "invalid-value", "message": completed.stderr.strip(),
                          "stderr": completed.stderr, "rc": completed.returncode}}
    if completed.returncode != 0:
        raise CallError(
            "old-generation",
            "이 사본이 사용 한도·압축 비율 저장을 끝내지 못했다 "
            "(`pm-config ctx-apply` 를 모르는 세대면 먼저 갱신하라).",
            stderr=completed.stderr, rc=completed.returncode)
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CallError(
            "not-json", f"사용 한도 반영 출력이 JSON 이 아니다 ({exc})",
            stderr=completed.stderr or completed.stdout, rc=completed.returncode) from exc
    conf = runner.status(home)["conf"]
    return {"changed": bool(document["changed"]), "conf": conf,
            "applied": list(document["applied"])}


class SettingsApi:
    """설정 탭(local.conf 편집·어댑터 절) 동작."""

    _capacity_lock = threading.Lock()

    def ctx_capacity(self, path: str, harness: str, validate_only: bool = False) -> dict:
        """fresh query 영수증은 private memory에만 보관하며 check는 그 C만 검증한다."""
        unknown={"schema":1,"harness":harness,"status":"unknown","reason":"현재 metadata 미보유",
                 "C":None,"observed_at":None,"valid_until":None}
        if harness not in ("claude","codex","opencode") or type(validate_only) is not bool:
            return {**unknown,"reason":"capacity 인자 오류"}
        try:
            home=_pm_home(path); key=(str(home),harness)
            with self._capacity_lock:
                if not hasattr(self,"_capacities"):
                    self._capacities={}; self._capacity_queries={}; self._capacity_generations={}
                stored=self._capacities.get(key)
                generation=self._capacity_generations.get(key,0)
                if validate_only:
                    if stored is None:return unknown
                    future=None; owner=False
                else:
                    future=self._capacity_queries.get(key)
                    owner=future is None
                    if owner:
                        future=Future();self._capacity_queries[key]=future
                        generation+=1;self._capacity_generations[key]=generation
                        self._capacities.pop(key,None)
            if not validate_only and not owner:
                return future.result()
            request={"harness":harness}
            if validate_only:request["receipt"]=stored["receipt"]
            try:
                argv=[*runner.interpreter(home),_tool(home,"pm_config.py"),"ctx-apply","--json","--preview"]
                completed=_run(home,argv,timeout=runner.HEAVY_TIMEOUT_SECONDS,input=json.dumps(request,ensure_ascii=False))
                if completed.returncode!=0:raise ValueError("capacity child 오류")
                document=json.loads(completed.stdout)
                if not isinstance(document,dict) or document.get("schema")!=1 or document.get("harness")!=harness:
                    raise ValueError("capacity child 응답 오류")
                if validate_only:
                    if set(document)!={"mode","schema","harness","status","reason"} or document["mode"]!="check":
                        raise ValueError("capacity check 응답 오류")
                    response={k:v for k,v in stored.items() if k not in ("mode","receipt")}
                    if document["status"]!="known":response={**unknown,"reason":document["reason"]}
                else:
                    if set(document)!={"mode","schema","harness","status","reason","C","observed_at","valid_until","receipt"} or document["mode"]!="query":
                        raise ValueError("capacity query 응답 오류")
                    if document["status"]=="known" and (type(document["C"]) is not int or document["C"]<=0 or not isinstance(document["receipt"],dict)):
                        raise ValueError("capacity query 근거 오류")
                    response={k:v for k,v in document.items() if k not in ("mode","receipt")}
            except (CallError,ValueError,TypeError,KeyError):
                document=None;response={**unknown,"reason":"현재 metadata 조회/검증 실패"}
            with self._capacity_lock:
                if self._capacity_generations[key]!=generation:
                    return unknown
                if validate_only:
                    if self._capacities.get(key) is not stored:return unknown
                    if response["status"]!="known":self._capacities.pop(key,None)
                elif document is not None and response["status"]=="known":
                    self._capacities[key]=document
                if not validate_only:
                    self._capacity_queries.pop(key,None);future.set_result(response)
            return response
        except CallError:
            return {**unknown,"reason":"현재 metadata 프로젝트 확인 실패"}

    # ── local.conf ────────────────────────────────────────────────────────
    def conf_keys(self, path: str) -> dict:
        """그 사본의 키 레지스트리 — `local_conf.py --keys --json` 산출 그대로."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "local_conf.py"),
                    "--keys", "--json"]
            completed = _run(home, argv, timeout=runner.LIGHT_TIMEOUT_SECONDS)
            if completed.returncode != 0:
                raise CallError(
                    "old-generation",
                    "이 사본의 엔진은 `local_conf.py --keys --json` 을 모른다 — 먼저 갱신하라.",
                    stderr=completed.stderr, rc=completed.returncode)
            try:
                document = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise CallError(
                    "not-json", f"키 목록 출력이 JSON 이 아니다 ({exc})",
                    stderr=completed.stderr or completed.stdout,
                    rc=completed.returncode) from exc
            # 화면은 행마다 선택지(`choices`)와 권장값(`recommended`)을 읽는다 — 그 필드가 없는
            # 세대의 산출로 절반만 그리지 않고, 무엇이 모자란지 말한다. 기준 세대는 번들 엔진이
            # 선언한 값이다(관리자가 숫자를 따로 들지 않는다).
            with ENGINE_LOCK:
                expected = load_engine_module("local_conf.py").KEY_JSON_SCHEMA
            schema = document.get("schema")
            if not isinstance(schema, int) or schema < expected:
                raise CallError(
                    "old-generation",
                    f"이 사본의 키 레지스트리는 schema {schema} 다 — 화면이 읽는 것은 "
                    f"schema {expected}(선택지·권장값·안 채웠을 때 일어나는 일·한 세트 묶음·절 스위치)이니 먼저 갱신하라.",
                    stderr=completed.stderr, rc=completed.returncode)
            return document
        except CallError as exc:
            return exc.as_error_dict()

    def conf_values(self, path: str) -> dict:
        """현재 값 — 번들 엔진의 `local_conf.load(...).values`(runner 와 같은 자리) 하나다.

        설정 완전성 다섯 목록은 여기서 내지 않는다. 그 판정은 화면이 이미 보관하는 가벼운 현황
        조회(`status --json` 의 `conf` 절)에 실려 있어서, 이 조회가 같은 명령을 다시 띄우면 설정
        탭을 열 때마다 사본 프로세스가 하나 더 뜬다(실측 0.43초 · Windows 1.5초). 서브프로세스
        없이 conf 파일만 읽는다.
        """
        try:
            return {"values": runner.conf_values(_pm_home(path))}
        except CallError as exc:
            return exc.as_error_dict()

    def conf_save(self, path: str, updates: dict) -> dict:
        """바뀐 키만 쓰고 **직후 재조회**한 판정을 돌려준다 — `{changed, conf, applied}`.

        사용 한도·압축 비율 키가 하나라도 들면 저장 전체를 그 사본의 `ctx-apply` 한 번으로 보낸다
        (`_ctx_apply`) — 검사를 통과해야 쓰고, 통과하면 하네스 설정 파일까지 반영한 줄(`applied`)이
        온다. 그 밖의 저장은 번들 엔진의 유일한 conf writer 한 함수이고 `applied` 는 빈 목록이다.
        요청값과 실효값이 다르면 그 엔진이 `RuntimeError` 로 멈추고, 여기서 잡지 않는다(성공으로
        흡수할 수 없는 실패다).
        """
        if UPSTREAM_PATH_KEY in updates:
            raise ValueError(
                f"{UPSTREAM_PATH_KEY} 는 이 경로로 쓰지 않는다 — 도달성 검증을 포함한 "
                "`upstream_set(path, value)` 을 부른다")
        try:
            home = _pm_home(path)
            # 잠금은 모듈 해소까지다 — 엔진 모듈과 그것이 쓰기 중에 지연 로드하는 형제
            # (`file_lock`)를 여기서 끝내 놓는다.
            with ENGINE_LOCK:
                engine = load_engine_module("pm_import.py")
                engine._load_file_lock()
                goes_through_copy = any(engine.is_harness_ctx_input_key(key) for key in updates)
            if goes_through_copy:
                return _ctx_apply(home, updates)
            # 쓰기는 잠금 **밖**이다. 이 함수는 대상 conf 에서 유도한 교차 프로세스 배타락을
            # 기다리는데, 그 대기를 ENGINE_LOCK 을 쥔 채 하면 같은 잠금을 쓰는
            # `runner.conf_values`·`interpreter` 가 막혀 등록 프로젝트 전체의 주기 조회가 선다.
            changed = engine._write_conf_keys(_conf_path(home), dict(updates))
            return {"changed": bool(changed), "conf": runner.status(home)["conf"], "applied": []}
        except CallError as exc:
            return exc.as_error_dict()

    def delegate_models(self, path: str, harness: str) -> dict:
        """그 사본의 `pm-config delegate models --harness <h> --json` 산출 그대로.

        모델 목록의 정본은 그 사본의 모델 원천 표(`pm_relay.MODEL_SOURCE`)다 — 번들 목록으로
        대신 답하지 않는다. 상한이 `HEAVY_TIMEOUT_SECONDS` 인 이유는 `kind="live"` 인 하네스가
        실제 CLI 를 부르기 때문이다(가벼운 절의 10초를 넘긴다).

        rc≠0 은 전부 error dict 로 올린다 — 사본이 이 명령을 모르는 경우(rc 2)와 조회가 실패한
        경우(rc 3)를 여기서 가르지 않고 사본의 사유를 그대로 나른다. **빈 목록 성공은 없다**:
        0개면 사본이 rc 3 으로 답하므로 이 자리에서 빈 목록을 성공으로 접을 길이 없다.
        """
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "delegate", "models", "--harness", harness, "--json"]
            completed = _run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS)
            if completed.returncode != 0:
                raise CallError(
                    "old-generation",
                    f"이 사본이 {harness} 모델 목록을 내지 못했다 "
                    "(`delegate models` 를 모르는 세대면 먼저 갱신하라).",
                    stderr=completed.stderr, rc=completed.returncode)
            try:
                return json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise CallError(
                    "not-json", f"모델 목록 출력이 JSON 이 아니다 ({exc})",
                    stderr=completed.stderr or completed.stdout,
                    rc=completed.returncode) from exc
        except CallError as exc:
            return exc.as_error_dict()

    def conf_reset(self, path: str, key: str) -> dict:
        """[기본값으로] — 레지스트리 기본값을 쓴다. 기본값이 없는 키는 되돌릴 자리가 없다."""
        with ENGINE_LOCK:
            default = load_engine_module("local_conf.py").default_for(key)
        if default is None:
            raise ValueError(
                f"기본값이 선언되지 않은 키는 되돌릴 수 없다: {key} "
                "(화면은 이 키에 [기본값으로] 버튼을 두지 않는다)")
        return self.conf_save(path, {key: default})

    # ── 어댑터 파일 ───────────────────────────────────────────────────────
    def adapters(self, path: str) -> dict:
        """`status --json --with-adapters` 의 `adapters` 절 + **같은 호출의** `conf` 절.

        소스는 그 사본이 자기 `upstream.path` 로 해소한다(URL 이면 엔진 캐시 clone/fetch).
        설정 완전성 다섯 목록 중 `missing_for_latest` 는 소스가 해소된 호출에서만 채워지므로
        (엔진 `_status_json_conf`), 그 값이 실린 `conf` 절을 내는 조회는 이것 하나다 — 같은
        응답에 함께 싣는다(그 목록 때문에 조회를 새로 만들지 않는다 · 어댑터 절은 버튼 전용).
        """
        try:
            document = runner.status(_pm_home(path), with_adapters=True)
            return {**document["adapters"], "conf": document["conf"]}
        except CallError as exc:
            return exc.as_error_dict()

    def adapter_accept(self, path: str, relpath: str) -> dict:
        """상류 값 수용 — 그 사본의 `sync-adapter-config --accept <relpath>` 한 번.

        관리자가 파일을 복사하거나 백업하지 않는다(백업·원장 기록은 엔진이 한다).
        """
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "sync-adapter-config", "--accept", relpath]
            return _completed(_run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()

    def adapter_matrix(self) -> dict:
        """등록 프로젝트 전부의 어댑터 절 — 프로젝트별로 독립 실패한다(버튼으로만)."""
        try:
            homes = registry.load(self._registry_file)
        except CallError as exc:
            return exc.as_error_dict()
        return {str(home): self.adapters(str(home)) for home in homes}

    # ── upstream ──────────────────────────────────────────────────────────
    def upstream_show(self, path: str) -> dict:
        """`pm-config upstream show` — 미등록 안내도 그 사본의 문구 그대로다."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "upstream", "show"]
            return _completed(_run(home, argv, timeout=runner.LIGHT_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()

    def upstream_set(self, path: str, value: str) -> dict:
        """`pm-config upstream set <값>` — 도달성 검증은 그 사본이 한다(거부 문구 그대로)."""
        try:
            home = _pm_home(path)
            argv = [*runner.interpreter(home), _tool(home, "pm_config.py"),
                    "upstream", "set", value]
            return _completed(_run(home, argv, timeout=runner.HEAVY_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()
