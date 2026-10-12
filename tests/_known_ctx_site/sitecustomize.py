"""테스트 고정 모델 한도 — 이 폴더를 PYTHONPATH 앞에 두면 그 환경의 모든 Python 프로세스가 읽는다.

모델 한도 조회 교체 지점 두 곳(`pm_import._read_harness_ctx_metadata`·`_ctx_claude_threshold`)을 고정 값으로
바꾼다. 갱신은 받은 갱신기를 새 프로세스(완료 프로세스)로 띄우므로 인자 감싸기로는 그 프로세스에 닿지
않는다 — 환경으로 넘겨야 자식의 자식까지 같은 값을 쓴다(`tests/_manager_home.known_ctx_env`).

교체 대상은 pm_import 하나다. 파일 로더로 읽힌 `pm_import.py` 는 실행 직후에, `pm_import.py` 를 스크립트로
직접 실행한 프로세스는 두 교체 지점이 정의된 직후(첫 호출 전)에 바꾼다. 다른 모듈은 건드리지 않는다.

고정 값은 C=200000·모델 창 1000000 이고 `_KNOWN_CTX_CAPACITY`·`_KNOWN_CTX_CONTEXT` 로 바꿀 수 있다.
아래 함수들은 표준 라이브러리만 쓴다 — `_manager_home._KNOWN_CTX_READER_PY` 가 이 파일 본문을 그대로 쓴다
(`-c` 자식·같은 프로세스 교체의 단일 출처). 교체 설치는 `sitecustomize` 로 읽힐 때만 한다.
"""
from pathlib import Path
import os
import sys
from contextlib import contextmanager

_FIXTURE_COMMANDS = ('claude', 'codex', 'opencode')


def write_native_commands(folder):
    # 실제 claude·codex·opencode 자리에 바로 끝나는 실행 파일을 둔다 — 교체하지 않은 조회가 실제
    # CLI 를 부르지 않고 실패한다.
    native = Path(folder)
    native.mkdir(parents=True, exist_ok=True)
    for harness in _FIXTURE_COMMANDS:
        command = native / (harness + '.exe' if os.name == 'nt' else harness)
        if not command.exists():
            command.write_text('#!/bin/sh\nexit 91\n', encoding='utf-8')
            command.chmod(0o755)
    return native


@contextmanager
def fixture_native_installation(folder):
    from unittest.mock import patch
    native = write_native_commands(folder)
    with patch.dict(os.environ, {'PATH': str(native) + os.pathsep + os.environ.get('PATH', ''),
                                 '_KNOWN_CTX_FIXTURE_BIN': str(native)}):
        yield native


def fixture_reader(capacity=200000, context=1000000, model='fixture-model'):
    # 모델 행 [{id, provider, C, basis}] — Claude 압축 예약 d 는 0(autoCompactWindow = P). d 를 쓰는
    # 계산은 test_harness_ctx_limits 의 t0943_claude 가 실측 값으로 잰다. opencode 는 대상 모델마다 한 행.
    def reader(root, harness, documents, models):
        if harness == 'claude':
            return [{'id': model, 'provider': 'firstParty', 'C': capacity,
                     'basis': {'model_window': capacity, 'auto_compact_threshold': capacity}}]
        if harness == 'codex':
            return [{'id': model, 'provider': 'openai', 'C': capacity,
                     'basis': {'context_window': context, 'max_context_window': None,
                               'model_context_window': None, 'percent': 100}}]
        return [{'id': target, 'provider': provider, 'C': capacity,
                 'basis': {'context': context, 'output': output, 'source': 'fixture'}}
                for (provider, target), output in models.items()]
    return reader


def fixture_threshold(root, window):
    return window


def install_fixture_capacity(module, capacity=200000, context=1000000, model='fixture-model'):
    module._read_harness_ctx_metadata = fixture_reader(capacity, context, model)
    module._ctx_claude_threshold = fixture_threshold
    return module


def _environment_capacity():
    return (int(os.environ.get('_KNOWN_CTX_CAPACITY', '200000')),
            int(os.environ.get('_KNOWN_CTX_CONTEXT', '1000000')))


def _install_known_capacity():
    """파일 로더 실행 뒤와 스크립트 실행 중 `pm_import` 의 두 교체 지점을 고정 값으로 바꾼다."""
    import importlib.machinery
    capacity, context = _environment_capacity()
    loader = importlib.machinery.SourceFileLoader
    original = loader.exec_module

    def exec_module(self, module):
        original(self, module)
        if Path(str(getattr(module, '__file__', '') or '')).name == 'pm_import.py':
            install_fixture_capacity(module, capacity, context)

    loader.exec_module = exec_module
    if not sys.argv or Path(sys.argv[0]).name != 'pm_import.py':
        return
    # 스크립트로 실행한 pm_import 는 파일 로더를 지나지 않는다 — 두 교체 지점이 정의된 직후(그 뒤 첫
    #   호출 전) 한 번 바꾸고 감시를 끈다.
    names = sys.modules['__main__'].__dict__

    def watch(_frame, _event, _arg):
        if '_read_harness_ctx_metadata' in names and '_ctx_claude_threshold' in names:
            sys.setprofile(None)
            install_fixture_capacity(sys.modules['__main__'], capacity, context)

    sys.setprofile(watch)


if __name__ == 'sitecustomize':
    _install_known_capacity()
