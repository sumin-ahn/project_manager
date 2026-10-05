"""JS 브리지 — 화면이 보는 표면과 오류 dict (T-0910 AT-004 · T-0921 AT-002).

정상은 엔진 JSON 그대로이고(감싸지 않는다), 실패는 `{"error": {kind, message, stderr, rc}}` 다.
합성 클래스 `Api` 의 MRO 에 탭별 mixin 셋이 들어 있는지도 여기서 고정한다(묶음 분할의 계약).

손님이 둘(창·브라우저)이어도 브리지 인스턴스는 하나다 — 그 사실과 폴더 고르기의 창 전용
거절, 저장 뒤 알림을 이 파일 끝에서 잰다.
"""
from __future__ import annotations

import inspect
import json
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

from _manager_home import (
    git_required, install_old_generation_engine, make_bare_with_tags, make_pm_home, set_conf_key,
)

from manager import CallError, registry
from manager.server import BROWSER_CLIENT, CLIENT_HEADER, WINDOW_CLIENT, ManagerServer
from manager.bridge import Api, BaseApi
from manager.bridge_run import RunApi
from manager.bridge_settings import SettingsApi
from manager.bridge_update import UpdateApi

pytestmark = git_required

BASE_API_METHODS = {
    "list_projects", "add_project", "remove_project", "status", "latest_tags", "pick_folder",
}


@pytest.fixture(scope="module")
def pm_home(tmp_path_factory) -> Path:
    return make_pm_home(tmp_path_factory.mktemp("bridge-home") / "demo")


@pytest.fixture
def picked(tmp_path):
    """폴더 선택 대화 자리 — 창이 주입한다(브리지는 pywebview 를 import 하지 않는다)."""
    return {"value": None}


@pytest.fixture
def api(tmp_path, picked) -> Api:
    return Api(folder_picker=lambda: picked["value"],
               registry_file=tmp_path / "config" / "projects.json")


# ── 묶음 분할의 계약 ─────────────────────────────────────────────────────────


def test_the_composed_api_carries_every_tab_mixin():
    assert Api.__mro__[:5] == (Api, BaseApi, SettingsApi, UpdateApi, RunApi)


def test_the_skeleton_keeps_exactly_six_methods():
    public = {name for name, _ in inspect.getmembers(BaseApi, inspect.isfunction)
              if not name.startswith("_")}

    assert public == BASE_API_METHODS


def test_the_bridge_does_not_import_the_window_toolkit():
    """뒷단은 창 없이 돈다 — pywebview import 는 `window.py` 하나뿐이다."""
    source = (Path(__file__).resolve().parents[1] / "manager" / "bridge.py").read_text(
        encoding="utf-8")

    assert "import webview" not in source


# ── 등록부 표면 ──────────────────────────────────────────────────────────────


def test_list_add_remove_round_trip(api, pm_home):
    assert api.list_projects() == {"projects": []}

    added = api.add_project(str(pm_home))
    assert added == {"projects": [{"path": str(pm_home.resolve()), "name": pm_home.name}]}
    assert api.list_projects() == added

    assert api.remove_project(str(pm_home)) == {"projects": []}


def test_a_refused_registration_comes_back_as_an_error_dict(api, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    response = api.add_project(str(plain))

    assert set(response) == {"error"}
    assert set(response["error"]) == {"kind", "message", "stderr", "rc"}
    assert response["error"]["kind"] == "invalid-path"


def test_folder_choice_and_cancel(api, picked, pm_home):
    picked["value"] = str(pm_home)
    assert api.pick_folder() == {"path": str(pm_home)}

    picked["value"] = None
    assert api.pick_folder() == {"path": None}


# ── 현황 표면 ────────────────────────────────────────────────────────────────


def test_status_is_the_engine_document_unwrapped(api, pm_home):
    response = api.status(str(pm_home))

    assert response["schema"] == 1
    assert "error" not in response
    assert "ok" not in response and "data" not in response


def test_status_with_board_carries_the_heavy_section(api, pm_home):
    response = api.status(str(pm_home), True, False)

    assert response["board"] is not None


def test_an_old_generation_copy_keeps_its_stderr_in_the_error_dict(api, pm_home, tmp_path):
    copy = tmp_path / "old"
    shutil.copytree(pm_home, copy, symlinks=True)
    install_old_generation_engine(copy)

    response = api.status(str(copy))

    assert response["error"]["kind"] == "old-generation"
    assert response["error"]["rc"] == 2
    assert "unrecognized arguments: --json" in response["error"]["stderr"]


def test_failures_are_never_answered_with_none_or_an_empty_value(api, tmp_path):
    response = api.status(str(tmp_path / "nowhere"))

    assert response is not None and response != {}
    assert response["error"]["message"]


# ── 최신 태그 표면 ───────────────────────────────────────────────────────────


def test_latest_tags_maps_projects_to_upstreams(api, pm_home, tmp_path):
    bare = make_bare_with_tags(tmp_path / "remote")
    second = tmp_path / "second"
    shutil.copytree(pm_home, second, symlinks=True)
    for home in (pm_home, second):
        set_conf_key(home, "upstream.path", str(bare))
        api.add_project(str(home))

    response = api.latest_tags()

    assert response["projects"][str(pm_home.resolve())] == {"upstream": str(bare)}
    assert response["projects"][str(second.resolve())] == {"upstream": str(bare)}
    assert response["upstreams"] == {str(bare): {"tag": "v2.0.0"}}


def test_a_project_without_upstream_has_no_tag_axis(api, pm_home, tmp_path):
    without = tmp_path / "no-upstream"
    shutil.copytree(pm_home, without, symlinks=True)
    set_conf_key(without, "upstream.path", "")
    api.add_project(str(without))

    response = api.latest_tags()

    assert response["projects"][str(without.resolve())] == {"upstream": None}
    assert response["upstreams"] == {}


def test_an_unreadable_registry_surfaces_as_an_error_dict(api, tmp_path):
    path = tmp_path / "config" / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    assert api.list_projects()["error"]["kind"] == "invalid-path"
    assert api.latest_tags()["error"]["kind"] == "invalid-path"


# ── 오류 종류의 계약 ─────────────────────────────────────────────────────────


def test_the_error_kinds_are_a_closed_set():
    assert CallError.KINDS == (
        "old-generation", "spawn", "timeout", "not-json", "invalid-path")

    with pytest.raises(ValueError):
        CallError("something-else", "새 종류를 브리지 밖에서 만들 수 없다")


# ── 손님이 둘일 때의 브리지 하나 (T-0921) ───────────────────────────────────

EVENT_TIMEOUT_SECONDS = 10


@pytest.fixture
def served(api, tmp_path):
    """같은 프로세스 서버 하나 — 창이 붙는 그 `Api` 인스턴스에 그대로 붙는다."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>화면</html>", encoding="utf-8")
    server = ManagerServer(api, dist=dist)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _post(server: ManagerServer, name: str, *args, client: str = BROWSER_CLIENT) -> dict:
    request = urllib.request.Request(
        f"{server.url}/api/{name}",
        data=json.dumps({"args": list(args)}).encode("utf-8"),
        headers={"Content-Type": "application/json", CLIENT_HEADER: client})
    with urllib.request.urlopen(request, timeout=EVENT_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _listen(server: ManagerServer, seen: list, ready: threading.Event) -> None:
    with urllib.request.urlopen(f"{server.url}/events", timeout=EVENT_TIMEOUT_SECONDS) as stream:
        ready.set()
        for raw in stream:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[len("data: "):]))
                return


def test_the_window_and_the_http_guest_share_one_bridge_instance(served, api, pm_home):
    """HTTP 로 등록한 프로젝트를 창 쪽 브리지가 그 자리에서 본다(값이 갈리지 않는다)."""
    added = _post(served, "add_project", str(pm_home))

    assert added == {"projects": [{"path": str(pm_home.resolve()), "name": pm_home.name}]}
    assert api.list_projects() == added


def test_the_folder_dialog_is_refused_for_a_guest_that_is_not_the_window(served, picked, pm_home):
    picked["value"] = str(pm_home)

    refused = _post(served, "pick_folder")

    assert refused["error"]["kind"] == "not-allowed"
    assert refused["error"]["message"]
    assert _post(served, "pick_folder", client=WINDOW_CLIENT) == {"path": str(pm_home)}


def test_the_folder_dialog_name_stays_in_the_set_even_though_it_is_window_only(served, api):
    from manager.server import api_names

    assert "pick_folder" in api_names(api)


def test_a_finished_save_shows_up_on_the_one_event_stream(served, api, tmp_path, monkeypatch):
    monkeypatch.setattr(type(api), "conf_save",
                        lambda _self, path, updates: {"changed": True, "conf": {}}, raising=True)
    seen: list = []
    ready = threading.Event()
    listener = threading.Thread(target=_listen, args=(served, seen, ready), daemon=True)
    listener.start()
    assert ready.wait(EVENT_TIMEOUT_SECONDS)

    _post(served, "conf_save", str(tmp_path), {"delegate.model": "opus"})

    listener.join(EVENT_TIMEOUT_SECONDS)
    assert seen == [{"name": "conf_save", "path": str(tmp_path)}]


def test_the_server_does_not_add_a_second_lock_around_the_engine_call():
    """엔진 호출 직렬화는 `manager/__init__.py` 의 잠금 하나다 — 서버는 그것을 다시 감싸지 않는다."""
    source = (Path(__file__).resolve().parents[1] / "manager" / "server.py").read_text(
        encoding="utf-8")

    assert "with ENGINE_LOCK" not in source and "import ENGINE_LOCK" not in source
    assert "RLock" not in source


@pytest.mark.parametrize('case',['private','missing','singleflight','late-check','query-failure','check-failure','expiry','source-change','isolation'])
def test_t0938_at2_backend_private_receipt_check(tmp_path, monkeypatch, case):
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, Future
    from datetime import datetime,timedelta,timezone
    from manager import bridge_settings as module
    from manager import ENGINE_LOCK
    home=tmp_path/'capacity';(home/'.project_manager').mkdir(parents=True)
    api=SettingsApi();calls=[];started=threading.Event();release=threading.Event()
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    phase={'C':967000,'fail':False}
    waiting=threading.Event()
    class ObservedFuture(Future):
        def result(self,timeout=None):
            waiting.set();return super().result(timeout)
    monkeypatch.setattr(module,'Future',ObservedFuture)
    def run(home,argv,*,timeout,input=None):
        request=json.loads(input);calls.append(request)
        if (case=='singleflight' and 'receipt' not in request) or (case=='late-check' and 'receipt' in request):
            started.set();assert release.wait(5)
        if 'receipt' in request:
            result={'mode':'check','schema':1,'harness':request['harness'],
                'status':'unknown' if phase['fail'] or case in {'late-check','expiry','source-change'} else 'known','reason':None}
        else:
            result={'mode':'query','schema':1,'harness':request['harness'],
                'status':'unknown' if phase['fail'] else 'known','reason':None,'C':None if phase['fail'] else phase['C'],
                'observed_at':datetime.now(timezone.utc).isoformat(),
                'valid_until':(datetime.now(timezone.utc)+timedelta(seconds=300)).isoformat(),
                'receipt':None if phase['fail'] else {'private':'SECRET_CREDENTIAL_SENTINEL'}}
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    monkeypatch.setattr(module,'_run',run)
    if case=='missing':
        assert api.ctx_capacity(str(home),'claude',True)['status']=='unknown' and calls==[];return
    if case=='singleflight':
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(api.ctx_capacity,str(home),'claude');assert started.wait(5)
            second=pool.submit(api.ctx_capacity,str(home),'claude');assert waiting.wait(5)
            # Unrelated engine work can proceed while the native protocol waits.
            assert ENGINE_LOCK.acquire(blocking=False);ENGINE_LOCK.release()
            release.set();a,b=first.result(5),second.result(5)
        assert a==b and a['C']==967000 and len(calls)==1;return
    result=api.ctx_capacity(str(home),'claude')
    assert result['C']==967000 and 'receipt' not in result
    original_times=(result['observed_at'],result['valid_until'])
    if case=='late-check':
        with ThreadPoolExecutor(max_workers=2) as pool:
            old=pool.submit(api.ctx_capacity,str(home),'claude',True);assert started.wait(5)
            phase['C']=570000
            assert api.ctx_capacity(str(home),'claude')['C']==570000
            release.set();assert old.result(5)['status']=='unknown'
        assert api._capacities[(str(home),'claude')]['C']==570000;return
    if case in {'query-failure','check-failure'}:phase['fail']=True
    if case=='isolation':
        phase['fail']=True;assert api.ctx_capacity(str(home),'codex')['status']=='unknown';phase['fail']=False
    checked=api.ctx_capacity(str(home),'claude',case!='query-failure')
    if case in {'query-failure','check-failure','expiry','source-change'}:
        assert checked['status']=='unknown' and checked['C'] is None
        count=len(calls);assert api.ctx_capacity(str(home),'claude',True)['status']=='unknown';assert len(calls)==count
    else:
        assert checked['C']==967000 and (checked['observed_at'],checked['valid_until'])==original_times
        assert 'receipt' in calls[-1]
    assert 'SECRET_CREDENTIAL_SENTINEL' not in json.dumps(checked)


def test_t0938_at4_apply_status_wins(tmp_path, monkeypatch):
    import subprocess
    from manager import bridge_settings as module
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    monkeypatch.setattr(module,'_run',lambda *a,**kw:subprocess.CompletedProcess([],0,json.dumps({'changed':True,'applied':[],
        'ctx_budget':{'claude':{'status':'known','E':600000}}}),''))
    monkeypatch.setattr(module.runner,'status',lambda *a:{'conf':{'ctx_budget':{'claude':{'status':'unknown','E':None}}}})
    result=module._ctx_apply(tmp_path,{})
    assert result['conf']['ctx_budget']['claude']['status']=='unknown'


def test_t0939_at4_apply_status_proof_and_capacity_separation(tmp_path,monkeypatch):
    import subprocess
    from manager import bridge_settings as module
    applied={'status':'known','reason':None,'W':600000,'E':570000,'P':535800,'native_window':535800,'applied_at':'2026-10-05T00:00:00Z'}
    conf={'ctx_budget':{'codex':{'status':'unknown','W':600000,'C':None,'E':None,'P':None}},'ctx_applied':{'codex':applied}}
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    monkeypatch.setattr(module,'_run',lambda *a,**kw:subprocess.CompletedProcess([],0,json.dumps({'changed':False,'applied':[],
        'ctx_budget':{'codex':{'status':'known','C':570000,'E':570000}}}),''))
    monkeypatch.setattr(module.runner,'status',lambda *a:{'conf':conf})
    result=module._ctx_apply(tmp_path,{})
    assert result['conf']==conf and result['conf']['ctx_applied']['codex']==applied
    assert result['conf']['ctx_budget']['codex']['C'] is None and 'C' not in applied


@pytest.mark.parametrize('harness',['claude','codex','opencode'])
def test_t0941_at3_actual_engine_private_check(tmp_path,monkeypatch,harness):
    import subprocess
    from manager import bridge_settings as module
    from test_harness_ctx_limits import _t0941_fixture, _snapshot_fixture_bytes
    f=_t0941_fixture(tmp_path,monkeypatch)
    api=SettingsApi();calls=[]
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    def run(home,argv,*,timeout,input=None):
        request=json.loads(input);calls.append(request)
        response=f.importer.query_ctx_capacity(home,request['harness'],receipt=request.get('receipt'),check='receipt' in request)
        return subprocess.CompletedProcess(argv,0,json.dumps(response),'')
    monkeypatch.setattr(module,'_run',run)
    response=api.ctx_capacity(str(f.root),harness)
    assert response['schema']==2 and response['verification']['status']=='verified'
    assert response['budget']['status']=='known'
    assert response['budget']==f.importer._ctx_public_budget(f.leaf.ctx_budget_status(f.root,f.values)[harness])
    assert not set(response)&{'receipt','binding','models','environment','fingerprints'}
    before=_snapshot_fixture_bytes(tmp_path);external=list(f.calls)
    checked=api.ctx_capacity(str(f.root),harness,True)
    assert checked['verification']['status']=='verified'
    assert checked['observed_at']==response['observed_at'] and checked['valid_until']==response['valid_until']
    assert checked['budget']==response['budget']
    assert before==_snapshot_fixture_bytes(tmp_path) and external==f.calls
    assert 'receipt' in calls[-1]
    assert all(secret not in json.dumps([response,checked]) for secret in ('DO-NOT-OUTPUT','apiKey','access_token','fixture-account','header.'))


def test_t0941_at3_actual_singleflight_and_legacy(tmp_path,monkeypatch):
    import subprocess
    from concurrent.futures import ThreadPoolExecutor,Future
    from manager import bridge_settings as module
    from manager import ENGINE_LOCK
    from test_harness_ctx_limits import _t0941_fixture
    f=_t0941_fixture(tmp_path,monkeypatch);api=SettingsApi()
    started=threading.Event();release=threading.Event();joined=threading.Event();calls=[]
    class WaitingFuture(Future):
        def result(self,timeout=None):joined.set();return super().result(timeout)
    monkeypatch.setattr(module,'Future',WaitingFuture)
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    def run(home,argv,*,timeout,input=None):
        calls.append(json.loads(input));started.set();assert release.wait(5)
        response=f.importer.query_ctx_capacity(home,'codex')
        return subprocess.CompletedProcess(argv,0,json.dumps(response),'')
    monkeypatch.setattr(module,'_run',run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(api.ctx_capacity,str(f.root),'codex');assert started.wait(5)
        second=pool.submit(api.ctx_capacity,str(f.root),'codex');assert joined.wait(5)
        assert ENGINE_LOCK.acquire(blocking=False);ENGINE_LOCK.release()
        release.set();a,b=first.result(5),second.result(5)
    assert a is b and a['verification']['status']=='verified' and len(calls)==1
    old=f.importer.preview_ctx_capacity(f.root,'codex')
    monkeypatch.setattr(module,'_run',lambda *a,**kw:subprocess.CompletedProcess([],0,json.dumps(old),''))
    legacy=api.ctx_capacity(str(f.root),'codex')
    assert legacy['C']==570000 and legacy['verification']['status']=='update-required'
    assert legacy['budget']['status']=='unknown' and legacy['budget']['E'] is None


def test_t0941_at3_cache_bound(tmp_path,monkeypatch):
    import subprocess
    from manager import bridge_settings as module
    api=SettingsApi();monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    # Transport failure creates no proof and must not produce unbounded failed pair state.
    monkeypatch.setattr(module,'_run',lambda *a,**kw:subprocess.CompletedProcess([],1,'','DO-NOT-OUTPUT'))
    for i in range(100):
        home=tmp_path/str(i);(home/'.project_manager').mkdir(parents=True)
        result=api.ctx_capacity(str(home),'codex')
        assert result['status']=='unknown' and 'DO-NOT-OUTPUT' not in json.dumps(result)
    assert len(api._capacity_generations)==96 and not api._capacity_queries and not api._capacities


@pytest.mark.parametrize('persist',['success','writer-failed'])
def test_t0941_at3_missing_receipt_uses_pure_persisted_status(tmp_path,monkeypatch,persist):
    import subprocess
    from manager import bridge_settings as module
    from test_harness_ctx_limits import _t0941_fixture,_snapshot_fixture_bytes
    f=_t0941_fixture(tmp_path,monkeypatch);api=SettingsApi();calls=[];status_calls=[]
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    monkeypatch.setattr(f.importer,'_ctx_native_protocol',lambda *a,**kw:(_ for _ in ()).throw(ValueError('unavailable')))
    if persist=='writer-failed':monkeypatch.setattr(f.importer,'_atomic_write_dest_bytes',lambda *a,**kw:False)
    def run(home,argv,*,timeout,input=None):
        request=json.loads(input);calls.append(request)
        result=f.importer.query_ctx_capacity(home,request['harness'])
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    def status(home):
        status_calls.append(home)
        return {'conf':{'ctx_budget':{h:f.importer._ctx_public_budget(row) for h,row in f.leaf.ctx_budget_status(home,f.values).items()},
            'ctx_applied':f.importer.ctx_applied_status(home,f.values)}}
    monkeypatch.setattr(module,'_run',run);monkeypatch.setattr(module.runner,'status',status)
    result=api.ctx_capacity(str(f.root),'claude')
    expected='known' if persist=='writer-failed' else 'unknown'
    assert result['budget']['status']==expected and result['status']=='unknown'
    before=_snapshot_fixture_bytes(tmp_path);external=list(f.calls)
    checked=api.ctx_capacity(str(f.root),'claude',True)
    assert checked['budget']==result['budget'] and checked['verification']==result['verification']
    assert checked['applied']==result['applied'] and len(calls)==1 and len(status_calls)==1
    assert before==_snapshot_fixture_bytes(tmp_path) and external==f.calls
    assert 'receipt' not in checked and 'DO-NOT-OUTPUT' not in json.dumps(checked)


def test_t0941_at3_check_preserves_latest_failed_matching_write(tmp_path,monkeypatch):
    import subprocess
    from manager import bridge_settings as module
    from test_harness_ctx_limits import _t0941_fixture,_t0941_remote_claude
    f=_t0941_fixture(tmp_path,monkeypatch);_t0941_remote_claude(f,monkeypatch)
    monkeypatch.setattr(f.importer,'_atomic_write_dest_bytes',lambda *a,**kw:False)
    monkeypatch.setattr(module.runner,'interpreter',lambda home:['python3'])
    def run(home,argv,*,timeout,input=None):
        request=json.loads(input)
        result=f.importer.query_ctx_capacity(home,request['harness'],receipt=request.get('receipt'),check='receipt' in request)
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    monkeypatch.setattr(module,'_run',run);api=SettingsApi()
    result=api.ctx_capacity(str(f.root),'claude')
    assert result['status']=='known' and result['verification']['status']=='write-failed'
    assert result['budget']['status']=='known'
    checked=api.ctx_capacity(str(f.root),'claude',True)
    assert checked['status']=='known' and checked['verification']==result['verification']
    assert checked['budget']==result['budget']
