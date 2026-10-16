"""Read real saved task state after a backend restart; no engine/network needed."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from ..application import service as service_module
from ..application.service import ApplicationService
from ..config import AgentConfig
from ..web import api


class TaskRecoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='task-recovery-',dir=os.environ.get('DPF_TEST_ARTIFACT_ROOT'))
        root=Path(self.temp.name)
        self.service=ApplicationService(AgentConfig(workspace_root=str(root/'tasks'),outputs_root=str(root/'outputs'),llm_enabled=False))
        self.task_id='pf-restart-test'
        self.folder=root/'tasks'/self.task_id
        self.folder.mkdir()
        self.getter=patch.object(api,'_get_service',return_value=self.service)
        self.getter.start()
        self.owner=patch.object(service_module,'current_task_lock',return_value={'locked':False,'task_id':None})
        self.lock=self.owner.start()

    def tearDown(self):
        self.owner.stop();self.getter.stop();self.temp.cleanup()

    def state(self,status=None,**extra):
        path=self.folder/'state.json'
        path.write_text(json.dumps({'task_id':self.task_id,'phase':'CASE_LOADED','status':status,'error':None,
            '_persisted_stage':'validate_case',**extra}),encoding='utf-8')
        return path

    def test_crashed_nullable_state_is_interrupted_and_read_only(self):
        path=self.state()
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        result=api.get_task(self.task_id)
        self.assertEqual(result.status,'interrupted')
        self.assertEqual(result.error['code'],'INTERRUPTED')
        self.assertEqual(result.error['stage'],'validate_case')
        self.assertIsNone(result.report_url)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_current_owner_nullable_state_is_running(self):
        self.state()
        self.lock.return_value={'locked':True,'task_id':self.task_id}
        result=api.get_task(self.task_id)
        self.assertEqual(result.status,'running')
        self.assertIsNone(result.error)

    def test_other_active_task_does_not_keep_old_task_running(self):
        self.state()
        self.lock.return_value={'locked':True,'task_id':'pf-new-task'}
        self.assertEqual(api.get_task(self.task_id).status,'interrupted')

    def test_missing_state_with_existing_trace_is_interrupted(self):
        (self.folder/'trace.json').write_text('[]',encoding='utf-8')
        self.assertEqual(api.get_task(self.task_id).status,'interrupted')

    def test_completed_task_keeps_success_and_report(self):
        self.state('success')
        reports=self.folder/'reports';reports.mkdir()
        (reports/'report.md').write_text('completed '+self.task_id,encoding='utf-8')
        self.lock.return_value={'locked':True,'task_id':self.task_id}
        result=api.get_task(self.task_id)
        self.assertEqual(result.status,'success')
        self.assertEqual(result.report_url,f'/api/tasks/{self.task_id}/report')

    def test_cancelled_task_keeps_authoritative_reason(self):
        error={'code':'CANCELLED','message':'user stopped','stage':'run_powerflow'}
        self.state('cancelled',error=error)
        result=api.get_task(self.task_id)
        self.assertEqual(result.status,'cancelled')
        self.assertEqual(result.error,error)

    def test_nonexistent_task_is_404(self):
        with self.assertRaises(HTTPException) as caught:api.get_task('pf-not-created')
        self.assertEqual(caught.exception.status_code,404)

    def test_nonterminal_invalid_scalar_never_raises_response_validation(self):
        for status in ['',None,0,'unknown']:
            with self.subTest(status=status):
                self.state(status)
                self.assertEqual(api.get_task(self.task_id).status,'interrupted')


def main():
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TaskRecoveryApiTests))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
