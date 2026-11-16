"""Readiness reports configuration and enabled retrieval paths, never invented availability."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from .. import __version__
from ..web import api


def main() -> int:
    failed = 0

    def check(name, condition):
        nonlocal failed
        print(f"[{'PASS' if condition else 'FAIL'}] {name}")
        failed += not condition

    with tempfile.TemporaryDirectory(prefix='tianshu-ready-') as folder:
        cfg = SimpleNamespace(cases_root=folder, llm_enabled=False,
            llm_base_url='http://fixture.invalid/v1', llm_model='fixture-model',
            engine_backend='com', rag_enabled=False, power_rag_enabled=False)
        local = {'enabled': True, 'knowledge_exists': True, 'index_exists': True,
                 'num_sources': 1, 'num_chunks': 2, 'power_rag': {'enabled': False}}

        def run(status):
            pipeline = mock.Mock()
            pipeline.status.return_value = status
            with mock.patch.object(api, 'AgentConfig', return_value=cfg), \
                 mock.patch.object(api, '_probe_engine', return_value=(True, 'registered only')), \
                 mock.patch.object(api, '_probe_storage', return_value=(True, 'fixture writable')), \
                 mock.patch.object(api, '_probe_skills', return_value=(True, 'fixture tools')), \
                 mock.patch('dpf_agent.rag.pipeline.get_pipeline', return_value=pipeline), \
                 mock.patch.object(api, '_probe_llm', side_effect=AssertionError('No inference/probe allowed')):
                data = json.loads(asyncio.run(api.ready()).body)
            return data, pipeline

        data, pipeline = run(local)
        check('disabled retrieval is not reported available despite files/index',
              data['checks']['knowledge']['enabled'] is False and not data['checks']['knowledge']['ok'])
        check('disabled retrieval performs no pipeline/external status probe', not pipeline.status.called)
        check('model configured is distinct from enabled and reachable',
              data['checks']['model']['configured'] is True and data['checks']['model']['enabled'] is False
              and data['checks']['model']['ok'] is False)
        check('backend reports loaded code version and selected engine',
              data['backend_version'] == __version__ and data['checks']['engine']['backend'] == 'com')

        cfg.rag_enabled = True
        data, _ = run({**local, 'index_exists': False})
        check('directory without index is not a ready knowledge base',
              not data['checks']['knowledge']['ok'] and '索引未就绪' in data['checks']['knowledge']['detail'])
        data, _ = run(local)
        check('enabled local index can be ready', data['checks']['knowledge']['ok'] is True)

        cfg.rag_enabled = False
        cfg.power_rag_enabled = True
        power = {**local, 'enabled': False, 'power_rag': {'enabled': True, 'ok': False, 'detail': 'offline'}}
        data, _ = run(power)
        check('disabled local files cannot masquerade as fallback for offline Power-RAG',
              data['checks']['knowledge']['enabled'] is True and not data['checks']['knowledge']['ok'])
        data, _ = run({**power, 'power_rag': {'enabled': True, 'ok': True, 'points_count': 3, 'collection': 'fixture'}})
        check('enabled available external path can be ready without local retrieval',
              data['checks']['knowledge']['ok'] is True and data['checks']['knowledge']['local_enabled'] is False)
    print(f"Readiness: {8-failed}/8 checks passed; controlled status providers, no native engine or network")
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
