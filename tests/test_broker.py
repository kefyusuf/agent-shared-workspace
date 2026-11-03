"""Black-box broker tests: real loopback HTTP and real SQLite state."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_workspace.core import Workspace
from agent_workspace.broker import create_server


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ws = Workspace(root / 'workspace.db')
        self.initial = self.ws.bootstrap('sample', root)
        self.operator = self.ws.open_session(self.initial['operator_credential'])['session_token']
        self.a = self.ws.enroll_agent(self.operator, 'codex', 'test')
        self.b = self.ws.enroll_agent(self.operator, 'claude', 'test')
        self.srv = create_server(self.ws, port=0)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)

    def request(self, path, data=None, *, bearer=None, headers=None, method='POST', raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_port, timeout=5)
        try:
            payload = raw if raw is not None else json.dumps(data or {}).encode()
            h = {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{self.srv.server_port}'}
            if bearer is not None: h['Authorization'] = f'Bearer {bearer}'
            h.update(headers or {})
            conn.request(method, path, body=payload if method=='POST' else None, headers=h)
            response=conn.getresponse()
            body=response.read()
            return response.status, json.loads(body)
        finally:
            conn.close()

    def login(self, credential):
        status, response=self.request('/v1/session', {'credential':credential})
        self.assertEqual(status,200)
        return response['result']['session_token']

    def call(self, token, op, arguments=None):
        return self.request('/v1/call', {'operation':op,'arguments':arguments or {}}, bearer=token)

    def test_health_does_not_expose_workspace(self):
        status, r=self.request('/v1/health',method='GET')
        self.assertEqual((status,r), (200,{'ok':True,'status':'healthy'}))

    def test_agent_login_and_read_tasks(self):
        token=self.login(self.a['credential'])
        status,r=self.call(token,'list_tasks')
        self.assertEqual(status,200)
        self.assertEqual(r['result'],[])

    def test_missing_and_invalid_session_rejected(self):
        for token in (None,'invalid'):
            status,r=self.call(token,'list_tasks')
            self.assertEqual((status,r['error']['code']),(401,'UNAUTHENTICATED'))

    def test_operator_enforces_task_creation_permission(self):
        t=self.login(self.a['credential'])
        status,r=self.call(t,'create_task',{'title':'x','acceptance_criteria':'y','request_key':'task-a'})
        self.assertEqual((status,r['error']['code']),(403,'FORBIDDEN'))
        status,r=self.call(self.operator,'create_task',{'title':'x','acceptance_criteria':'y','request_key':'task-a'})
        self.assertEqual(status,200)
        task=r['result']['task_id']
        status,r=self.call(t,'get_task',{'task_id':task})
        self.assertEqual((status,r['result']['status']),(200,'Ready'))

    def test_agent_cannot_read_others_private_message(self):
        a=self.login(self.a['credential']); b=self.login(self.b['credential'])
        status,r=self.call(a,'send_message',{'recipient_agent_id':self.b['agent_id'],'body':'private','request_key':'message-x'})
        self.assertEqual(status,200)
        mid=r['result']['message_id']
        status,r=self.call(b,'get_message',{'message_id':mid})
        self.assertEqual(status,200)
        outsider=self.ws.enroll_agent(self.operator,'third','test')
        c=self.login(outsider['credential'])
        status,r=self.call(c,'get_message',{'message_id':mid})
        self.assertIn(status,(403,404))
        status,r=self.call(b,'inbox')
        self.assertEqual((status,r['result']['messages'][0]['body']),(200,'private'))

    def test_invalid_token_rejected_before_operation_dispatch(self):
        status,r=self.call('invalid','not_an_operation')
        self.assertEqual((status,r['error']['code']),(401,'UNAUTHENTICATED'))

    def test_unknown_operation_is_rejected(self):
        for op in ('bootstrap','_connect','inject_failpoint','enroll_agent','__class__','finish_task'):
            status,r=self.call(self.operator,op)
            self.assertEqual((status,r['error']['code']),(400,'UNKNOWN_OPERATION'))

    def test_unexpected_arguments_rejected_without_invocation(self):
        status,r=self.call(self.operator,'list_tasks',{'limit':1,'session_token':'override'})
        self.assertEqual((status,r['error']['code']),(400,'INVALID_ARGUMENTS'))
        status,r=self.call(self.operator,'list_tasks',{'limit':False})
        self.assertEqual((status,r['error']['code']),(400,'INVALID_ARGUMENTS'))

    def test_json_envelope_strictness(self):
        status,r=self.request('/v1/session',{'credential':self.a['credential'],'extra':'x'})
        self.assertEqual((status,r['error']['code']),(400,'INVALID_REQUEST'))
        status,r=self.request('/v1/call',{'operation':'list_tasks','arguments':[]},bearer=self.operator)
        self.assertEqual((status,r['error']['code']),(400,'INVALID_REQUEST'))

    def test_invalid_credential_and_no_credentials_in_error(self):
        credential='wrong-password-private'
        status,r=self.request('/v1/session',{'credential':credential})
        self.assertEqual((status,r['error']['code']),(401,'UNAUTHENTICATED'))
        self.assertNotIn(credential,json.dumps(r))

    def test_revoked_session_immediately_stops_requests(self):
        t=self.login(self.a['credential'])
        self.ws.close_session(t)
        status,r=self.call(t,'list_tasks')
        self.assertEqual((status,r['error']['code']),(401,'UNAUTHENTICATED'))

    def test_origin_header_rejected(self):
        status,r=self.request('/v1/session',{'credential':self.a['credential']},headers={'Origin':'https://attacker.example'})
        self.assertEqual((status,r['error']['code']),(403,'ORIGIN_REJECTED'))

    def test_host_header_rejected(self):
        status,r=self.request('/v1/session',{'credential':self.a['credential']},headers={'Host':'attacker.example'})
        self.assertEqual((status,r['error']['code']),(400,'INVALID_HOST'))

    def test_oversized_body_rejected(self):
        data=b'x'*32769
        status,r=self.request('/v1/session',raw=data)
        self.assertEqual((status,r['error']['code']),(413,'PAYLOAD_TOO_LARGE'))

    def test_bad_content_type_rejected(self):
        status,r=self.request('/v1/session',{'credential':self.a['credential']},headers={'Content-Type':'text/plain'})
        self.assertEqual((status,r['error']['code']),(415,'UNSUPPORTED_MEDIA_TYPE'))

    def test_non_loopback_binding_rejected(self):
        with self.assertRaises(ValueError):
            create_server(self.ws,host='0.0.0.0',port=0)

    def test_unknown_url_method_rejected(self):
        status,r=self.request('/v1/admin',{},bearer=self.operator)
        self.assertEqual(status,404)
        status,r=self.request('/v1/call',method='GET')
        self.assertEqual(status,404)

    def test_login_response_is_not_cached(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.srv.server_port)
        try:
            conn.request('POST','/v1/session',body=json.dumps({'credential':self.a['credential']}).encode(),headers={'Content-Type':'application/json'})
            r=conn.getresponse();r.read()
            self.assertEqual(r.getheader('Cache-Control'),'no-store')
        finally:conn.close()

    def test_missing_required_argument_rejected(self):
        status,r=self.call(self.operator,'get_task')
        self.assertEqual((status,r['error']['code']),(400,'INVALID_ARGUMENTS'))

    def test_duplicate_json_keys_rejected(self):
        payload=(b'{"credential":"wrong",' + b'"credential":"' + self.a['credential'].encode() + b'"}')
        status,r=self.request('/v1/session',raw=payload)
        self.assertEqual((status,r['error']['code']),(400,'INVALID_JSON'))

    def test_invalid_json_rejected(self):
        status,r=self.request('/v1/session',raw=b'{"credential":')
        self.assertEqual((status,r['error']['code']),(400,'INVALID_JSON'))

    def test_idempotent_message_retry_only_written_once(self):
        token=self.login(self.a['credential'])
        kwargs={'recipient_agent_id':self.b['agent_id'],'body':'one','request_key':'retry-key'}
        first=self.call(token,'send_message',kwargs)
        second=self.call(token,'send_message',kwargs)
        self.assertEqual(first,second)
        recipient=self.login(self.b['credential'])
        status,reply=self.call(recipient,'inbox')
        self.assertEqual(status,200)
        self.assertEqual(len(reply['result']['messages']),1)

    def test_concurrent_claim_only_one_succeeds(self):
        from concurrent.futures import ThreadPoolExecutor
        _,task=self.call(self.operator,'create_task',{'title':'race','acceptance_criteria':'single owner','request_key':'race-task'})
        task_id=task['result']['task_id']
        a=self.login(self.a['credential']);b=self.login(self.b['credential'])
        def claim(tok,key):
            return self.call(tok,'claim_task',{'task_id':task_id,'expected_version':0,'request_key':key})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(claim,a,'claim-a'),pool.submit(claim,b,'claim-b')]
            statuses=[f.result()[0] for f in futures]
        self.assertEqual(sorted(statuses),[200,409])

    def test_query_string_is_not_a_supported_endpoint(self):
        status,r=self.request('/v1/health?token=SECRET', method='GET')
        self.assertEqual(status,404)


if __name__ == '__main__':
    unittest.main()
