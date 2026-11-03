"""M3.2 black-box MCP stdio tests over a real authenticated HTTP broker."""
import http.client
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_workspace.broker import create_server
from agent_workspace.core import Workspace

INIT = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2025-11-25","capabilities":{},
    "clientInfo":{"name":"test-client","version":"1.0"}}}
NOTIFY = {"jsonrpc":"2.0","method":"notifications/initialized"}


class Client:
    def __init__(self, port, credential, *, env=None):
        settings=os.environ.copy()
        settings.update({'PYTHONPATH':str(Path(__file__).resolve().parents[1] / 'src'),
                         'AW_BROKER_PORT':str(port),'AW_AGENT_CREDENTIAL':credential})
        settings.update(env or {})
        self.proc=subprocess.Popen([sys.executable,'-m','agent_workspace.mcp_stdio'],
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,text=True,encoding='utf-8',
                                   bufsize=1,env=settings)
        self.out=queue.Queue()
        self.reader=threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        self.serial=5

    def _read(self):
        for line in self.proc.stdout:
            self.out.put(line)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj,separators=(',',':'))+'\n')
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.serial+=1
        request={'jsonrpc':'2.0','id':self.serial,'method':method}
        if params is not None: request['params']=params
        self.send(request)
        line=self.out.get(timeout=5)
        return json.loads(line)

    def init(self):
        response=self.request('initialize',INIT['params'])
        self.send(NOTIFY)
        return response

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill();self.proc.wait(timeout=3)
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.reader.join(timeout=2)

    def call(self, name, arguments=None):
        response=self.request('tools/call',{'name':name,'arguments':arguments or {}})
        if 'error' in response: return response
        result=response['result']
        if result.get('isError'): return result
        return json.loads(result['content'][0]['text'])


class McpStdioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.ws=Workspace(Path(cls.temp.name)/'workspace.db')
        boot=cls.ws.bootstrap('mcp pilot',cls.temp.name)
        cls.operator_credential = boot['operator_credential']
        cls.operator=cls.ws.open_session(boot['operator_credential'])['session_token']
        cls.a=cls.ws.enroll_agent(cls.operator,'alpha','codex')
        cls.b=cls.ws.enroll_agent(cls.operator,'beta','claude')
        cls.c=cls.ws.enroll_agent(cls.operator,'outsider','test')
        cls.server=create_server(cls.ws,port=0)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def client(self, agent=None, **kwargs):
        agent=agent or self.a
        c=Client(self.server.server_port,agent['credential'],**kwargs)
        self.addCleanup(c.close)
        return c

    def test_initialize_protocol_and_list_discoverable_tools(self):
        c=self.client()
        response=c.init()
        self.assertEqual(response['result']['protocolVersion'],'2025-11-25')
        self.assertEqual(response['result']['capabilities'],{'tools':{}})
        listing=c.request('tools/list')['result']['tools']
        self.assertGreaterEqual(len(listing),12)
        self.assertEqual(len(listing),len({t['name'] for t in listing}))
        for tool in listing:
            schema=tool['inputSchema']
            self.assertEqual(schema['type'],'object')
            self.assertEqual(schema['additionalProperties'],False)
        self.assertNotIn('bootstrap',{t['name'] for t in listing})
        self.assertEqual(c.request('ping')['result'],{})

    def test_client_requires_initialize(self):
        c=self.client()
        self.assertEqual(c.request('tools/list')['error']['code'],-32002)
        self.assertIn('result',c.init())
        self.assertIn('tools',c.request('tools/list')['result'])

    def test_unsupported_method_and_arguments_are_rejected(self):
        c=self.client();c.init()
        self.assertEqual(c.request('system/exec')['error']['code'],-32601)
        self.assertEqual(c.request('tools/call',{'name':'bootstrap','arguments':{}})['error']['code'],-32602)
        self.assertEqual(c.request('tools/call',{'name':'task_get','arguments':{'task_id':False}})['error']['code'],-32602)
        self.assertEqual(c.request('tools/call',{'name':'task_get','arguments':{'task_id':'t','db_path':'secret'}})['error']['code'],-32602)

    def test_agent_read_and_role_bounded_operations(self):
        c=self.client();c.init()
        result=c.call('workspace_identity')
        self.assertEqual(result['role'],'agent')
        self.assertEqual(result['agent_id'],self.a['agent_id'])
        self.assertIsInstance(c.call('task_list'),list)
        self.assertIn('decisions',c.call('brain_brief'))

    def test_invalid_credential_and_operator_credential_rejected(self):
        for cred in ('BAD-SECRET',self.operator_credential):
            c=self.client(env={'AW_AGENT_CREDENTIAL':cred});c.init()
            result=c.call('task_list')
            self.assertTrue(result['isError'])
            self.assertNotIn(cred,json.dumps(result))
            if cred == self.operator_credential:
                self.assertIn('AGENT_ROLE_REQUIRED',json.dumps(result))

    def test_two_real_stdio_clients_exchange_message(self):
        a=self.client(self.a); b=self.client(self.b)
        a.init();b.init()
        sent=a.call('mail_send',{'recipient_agent_id':self.b['agent_id'],
                                  'body':'From MCP Alpha','request_key':'mcp-message-'+self.id()})
        self.assertIn('message_id',sent)
        inbox=b.call('mail_inbox',{'after_seq':0,'limit':100})
        self.assertIn(sent['message_id'],[m['message_id'] for m in inbox['messages']])
        outsider=self.client(self.c);outsider.init()
        self.assertEqual(outsider.call('mail_inbox',{'after_seq':0,'limit':100})['messages'],[])

    def test_two_real_stdio_clients_task_context_handoff_and_lease(self):
        a=self.client(self.a);b=self.client(self.b);outsider=self.client(self.c)
        for client in (a,b,outsider): client.init()
        task=self.ws.create_task(self.operator,'MCP handoff proof','Share verified handoff',
                                'mcp-task-'+self.id())
        claimed=a.call('task_claim',{'task_id':task['task_id'],'expected_version':0,
                                    'request_key':'mcp-claim-'+self.id()})
        self.assertIn('fence_token',claimed)
        denied=b.call('task_claim',{'task_id':task['task_id'],'expected_version':0,
                                   'request_key':'mcp-denied-'+self.id()})
        self.assertTrue(denied['isError'])
        self.assertIn('VERSION_CONFLICT',denied['content'][0]['text'])
        snap=a.call('context_publish',{'task_id':task['task_id'],
               'expected_version':claimed['version'],'fence_token':claimed['fence_token'],
               'summary':'The change was inspected','next_steps':'Review tests',
               'source_refs':[{'kind':'task','ref':task['task_id']}],
               'changed_paths':['src/auth.py'],'request_key':'mcp-snapshot-'+self.id()})
        self.assertIn('snapshot_id',snap)
        self.assertTrue(b.call('context_get',{'snapshot_id':snap['snapshot_id']})['isError'])
        handoff=a.call('context_handoff',{'snapshot_id':snap['snapshot_id'],
                'recipient_agent_id':self.b['agent_id'],'action_required':'Review auth tests',
                'request_key':'mcp-handoff-'+self.id()})
        self.assertIn('handoff_id',handoff)
        delivered=b.call('context_inbox')
        self.assertIn(handoff['handoff_id'],[h['handoff_id'] for h in delivered['handoffs']])
        self.assertEqual(b.call('context_get',{'snapshot_id':snap['snapshot_id']})['summary'],
                         'The change was inspected')
        self.assertTrue(outsider.call('context_get',{'snapshot_id':snap['snapshot_id']})['isError'])
        released=a.call('task_release',{'task_id':task['task_id'],'expected_version':claimed['version'],
                     'fence_token':claimed['fence_token'],'request_key':'mcp-release-'+self.id()})
        self.assertEqual(released['status'],'Ready')
        new=b.call('task_claim',{'task_id':task['task_id'],'expected_version':released['version'],
                                'request_key':'mcp-reclaim-'+self.id()})
        self.assertGreater(new['fence_token'],claimed['fence_token'])

    def test_revocation_enforced_even_for_cached_session(self):
        c=self.client(self.a);c.init();self.assertEqual(c.call('workspace_identity')['role'],'agent')
        # Revoke the live adapter's session. Domain must reject subsequent calls.
        identity=c.call('workspace_identity')
        self.ws.revoke_session(self.operator,identity['session_id'],
                               'mcp-revoke-'+self.id(),'Security test')
        self.assertTrue(c.call('task_list')['isError'])

    def test_modern_probe_falls_back_to_handshake(self):
        c=self.client()
        self.assertEqual(c.request('server/discover')['error']['code'],-32601)
        self.assertIn('result',c.init())

    def test_valid_request_id_zero_and_unknown_fields(self):
        c=self.client();c.init()
        c.send({'jsonrpc':'2.0','id':0,'method':'ping'})
        reply=json.loads(c.out.get(timeout=5))
        self.assertEqual((reply['id'],reply['result']),(0,{}))
        self.assertEqual(c.request('tools/list',{'unknown':1})['error']['code'],-32602)
        self.assertEqual(c.request('tools/call',{'name':'task_list','arguments':{},
                                              'operation':'__class__'})['error']['code'],-32602)

    def test_invalid_json_and_duplicate_fields_do_not_crash(self):
        c=self.client()
        c.proc.stdin.write('{invalid json\n')
        c.proc.stdin.flush()
        self.assertEqual(json.loads(c.out.get(timeout=5))['error']['code'],-32700)
        c.proc.stdin.write('{"jsonrpc":"2.0","id":7,"id":8,"method":"ping"}\n')
        c.proc.stdin.flush()
        self.assertEqual(json.loads(c.out.get(timeout=5))['error']['code'],-32700)
        self.assertIn('result',c.init())

    def test_memory_findings_use_broker_and_do_not_promote_to_brain(self):
        c=self.client(self.a);c.init()
        task=self.ws.create_task(self.operator,'Memory proof','Learning is not a decision',
                                'memory-task-'+self.id())
        record=c.call('memory_record',{'kind':'finding','title':'MCP discovery '+self.id(),
                          'content':'Agent observed a necessary test for timeout handling',
                          'source_refs':[{'kind':'task','ref':task['task_id']}],
                          'request_key':'mcp-memory-'+self.id(),'task_id':task['task_id']})
        self.assertIn('memory_id',record)
        listing=c.call('memory_search',{'query':'timeout handling','limit':50})
        self.assertIn(record['memory_id'],[x['memory_id'] for x in listing['memories']])
        brain=c.call('brain_brief')
        self.assertNotIn(record['memory_id'],json.dumps(brain))

    def test_normal_stdio_shutdown_revokes_session_and_releases_lease(self):
        client=self.client(self.a)
        client.init()
        task=self.ws.create_task(self.operator, 'Graceful shutdown', 'Do not leave leases held',
                                 'shutdown-task-'+self.id())
        acquired=client.call('task_claim', {'task_id':task['task_id'],
                    'expected_version':0, 'request_key':'shutdown-claim-'+self.id()})
        self.assertIn('fence_token', acquired)
        client.close()  # Closing stdin must log out of broker and release task.
        current=self.ws.get_task(self.operator, task['task_id'])
        self.assertEqual(current['status'], 'Ready')
        self.assertGreater(current['version'], acquired['version'])

    def test_only_loopback_and_port_range_allowed(self):
        for port in ('99999','garbage','-1'):
            c=self.client(env={'AW_BROKER_PORT':port});c.init()
            self.assertTrue(c.call('task_list')['isError'])


if __name__=='__main__':
    unittest.main()
