import threading
import time
import unittest
import uuid

import server
from ai_history import SessionHistory
from ai_runtime import AiRuntime
from ai_service import AiFailure
from chat_core import ChatCore
from tests.support import Events
from tests.test_f8_resource_limits import AdmissionServer


class FakeService:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.failure = None

    def generate(self, **kw):
        # Proves HTTP/model work runs outside the global routing lock.
        if not server.lock.acquire(timeout=.2):
            raise AssertionError('server state lock held during AI work')
        server.lock.release()
        self.calls.append(kw)
        self.entered.set()
        self.gate.wait(3)
        if self.failure: raise self.failure
        return 'Private AI result'

    def close(self): self.gate.set()


class AiServerTests(unittest.TestCase):
    def setUp(self):
        self.assertFalse(server.clients)
        self.service = FakeService()
        self.runtime = AiRuntime(self.service)
        self.peer = AdmissionServer(ai_runtime=self.runtime)
        self.cores = []

    def tearDown(self):
        self.service.gate.set()
        for core in self.cores: core.close()
        self.peer.close()
        self.runtime.close()
        self.assertFalse(server.clients)
        self.assertFalse(server.groups)

    def connect(self, name):
        core = ChatCore('127.0.0.1', self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True,name))
        events = Events()
        core.start(events.put)
        return core, events

    def ai(self, core, events, room_type='pm', room='Bob', action='summary', instruction=''):
        request_id = uuid.uuid4().hex
        self.assertTrue(core.request_ai(request_id, action, room_type, room, instruction))
        return events.wait(lambda m:m.get('request_id') == request_id)

    def reset_cooldown(self, name):
        with server.lock: server.clients[name].next_ai_at = 0

    def test_private_authoritative_snapshot_result_and_normal_chat_during_generation(self):
        a, ae = self.connect('Alice'); b, be = self.connect('Bob'); c, ce = self.connect('Carol')
        a.pm('Bob','ไทย hello'); be.wait(lambda m:m.get('text')=='ไทย hello')
        c.pm('Bob','other secret'); be.wait(lambda m:m.get('text')=='other secret')
        self.service.gate.clear()
        rid = uuid.uuid4().hex
        a.request_ai(rid,'draft','pm','Bob','reply politely')
        self.assertTrue(self.service.entered.wait(1))
        b.pm('Alice','chat still works'); ae.wait(lambda m:m.get('text')=='chat still works')
        self.service.gate.set()
        result = ae.wait(lambda m:m.get('request_id')==rid)
        self.assertEqual(result['type'],'ai_result')
        self.assertEqual(result['message_count'],1)
        call = self.service.calls[0]
        self.assertEqual(call['requester'],'Alice')
        self.assertEqual([r.text for r in call['snapshot'].messages], ['ไทย hello'])
        self.assertEqual(call['instruction'],'reply politely')
        for core, events in [(b,be),(c,ce)]:
            core.pm(core.name,'barrier'); events.wait(lambda m:m.get('text')=='barrier')
            self.assertFalse(any(m.get('type') in ('ai_result','ai_error') for m in events.seen))
        with server.lock:
            history = server.clients['Alice'].history.snapshot(('pm','Bob'))
        self.assertEqual(len(history.messages),2)  # AI output is never transcript.

    def test_group_postjoin_only_and_nonmember_cannot_request(self):
        a, ae = self.connect('Alice'); b, be = self.connect('Bob'); c, ce = self.connect('Carol')
        a.create_group('G'); ae.wait(lambda m:'G' in m.get('groups',{}))
        a.group_msg('G','before join'); ae.wait(lambda m:m.get('text')=='before join')
        self.assertEqual(self.ai(c,ce,'group','G')['code'],'not_allowed')
        b.join_group('G'); be.wait(lambda m:'Bob' in m.get('groups',{}).get('G',[]))
        self.assertEqual(self.ai(b,be,'group','G')['code'],'no_history')
        a.group_msg('G','after join'); be.wait(lambda m:m.get('text')=='after join')
        self.assertEqual(self.ai(b,be,'group','G')['type'],'ai_result')
        self.assertEqual([r.text for r in self.service.calls[0]['snapshot'].messages],['after join'])

    def test_pending_duplicate_cooldown_budget_and_overflow(self):
        a, ae = self.connect('Alice')
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'no_history')
        a.pm('Alice','self once'); ae.wait(lambda m:m.get('text')=='self once')
        self.service.gate.clear()
        rid = uuid.uuid4().hex
        a.request_ai(rid,'summary','pm','Alice')
        self.assertTrue(self.service.entered.wait(1))
        a.request_ai(rid,'summary','pm','Alice')
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'busy')
        self.service.gate.set()
        self.assertEqual(ae.wait(lambda m:m.get('request_id')==rid)['message_count'],1)
        self.assertEqual(len(self.service.calls),1)
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'cooldown')
        self.reset_cooldown('Alice')
        self.service.failure = AiFailure('budget')
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'budget')
        self.reset_cooldown('Alice')
        with server.lock:
            history = server.clients['Alice'].history = SessionHistory(max_messages=1)
            history.record(('pm','Alice'),'Alice','1'); history.record(('pm','Alice'),'Alice','2')
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'history_limit')

    def test_old_session_result_not_delivered_to_reused_username(self):
        a, ae = self.connect('Alice')
        a.pm('Alice','old'); ae.wait(lambda m:m.get('text')=='old')
        self.service.gate.clear()
        rid = uuid.uuid4().hex
        a.request_ai(rid,'summary','pm','Alice'); self.assertTrue(self.service.entered.wait(1))
        a.close()
        deadline=time.monotonic()+2
        while 'Alice' in server.clients and time.monotonic()<deadline: time.sleep(.005)
        a2, e2 = self.connect('Alice')
        self.service.gate.set()
        self.assertEqual(self.ai(a2,e2,room='Alice')['code'],'no_history')
        a2.pm('Alice','new barrier'); e2.wait(lambda m:m.get('text')=='new barrier')
        self.assertFalse(any(m.get('request_id')==rid for m in e2.seen))

    def test_case_insensitive_retry_uses_fresh_connection(self):
        a, ae = self.connect('Alice')
        retry = ChatCore('127.0.0.1',self.peer.port); self.cores.append(retry)
        self.assertFalse(retry.login('aLiCe')[0]); self.assertIsNone(retry.sock)
        self.assertEqual(retry.login('Bob'),(True,'Bob'))
        ae.wait(lambda m:m.get('users')==['Alice','Bob'])

    def test_global_capacity_and_controlled_provider_exception(self):
        people=[self.connect(name) for name in ('Alice','Bob','Carol')]
        for core, events in people:
            core.pm(core.name,'context'); events.wait(lambda m:m.get('text')=='context')
        self.service.gate.clear()
        ids=[]
        for core, events in people[:2]:
            rid=uuid.uuid4().hex; ids.append(rid)
            core.request_ai(rid,'summary','pm',core.name)
        deadline=time.monotonic()+1
        while len(self.service.calls)<2 and time.monotonic()<deadline: time.sleep(.005)
        c,ce=people[2]
        self.assertEqual(self.ai(c,ce,room='Carol')['code'],'busy')
        self.service.failure=RuntimeError('Never expose provider headers or secret keys')
        self.service.gate.set()
        for (_,events), rid in zip(people,ids):
            result=events.wait(lambda m:m.get('request_id')==rid)
            self.assertEqual(result['code'],'provider')
            self.assertNotIn('secret',result['text'])
        self.assertEqual(self.ai(c,ce,room='Carol')['code'],'provider')

    def test_group_generation_change_and_history_forget(self):
        a,ae=self.connect('Alice')
        a.create_group('G'); ae.wait(lambda m:'G' in m.get('groups',{}))
        a.group_msg('G','old generation'); ae.wait(lambda m:m.get('text')=='old generation')
        self.service.gate.clear()
        rid=uuid.uuid4().hex; a.request_ai(rid,'summary','group','G')
        self.assertTrue(self.service.entered.wait(1))
        # Simulate authorization-generation change while same username remains.
        with server.lock:
            server.groups['G']=['Alice']
            server.clients['Alice'].history.forget_room(('group','G'))
        self.service.gate.set()
        self.assertEqual(ae.wait(lambda m:m.get('request_id')==rid)['code'],'not_allowed')
        self.reset_cooldown('Alice')
        self.assertEqual(self.ai(a,ae,'group','G')['code'],'no_history')

    def test_disabled_ai_and_offline_pm_context(self):
        a,ae=self.connect('Alice'); b,be=self.connect('Bob')
        b.pm('Alice','remember me'); ae.wait(lambda m:m.get('text')=='remember me')
        b.close(); ae.wait(lambda m:m.get('users')==['Alice'])
        self.assertEqual(self.ai(a,ae)['type'],'ai_result')
        # Disabled main/legacy fixtures do not instantiate a service implicitly.
        a.close()
        self.peer.close()
        self.peer=AdmissionServer()
        a,ae=self.connect('Alice')
        self.assertEqual(self.ai(a,ae,room='Alice')['code'],'unavailable')
        a.pm('Alice','disabled PM'); ae.wait(lambda m:m.get('text')=='disabled PM')
        a.create_group('Disabled'); ae.wait(lambda m:'Disabled' in m.get('groups',{}))
        a.group_msg('Disabled','disabled group'); ae.wait(lambda m:m.get('text')=='disabled group')
        with server.lock:
            self.assertEqual(server.clients['Alice'].history.message_count,0)

    def test_runtime_close_clears_running_and_queued_live_requests(self):
        self.peer.close(); self.runtime.close()
        self.runtime=AiRuntime(self.service,workers=1)
        self.peer=AdmissionServer(ai_runtime=self.runtime)
        a,ae=self.connect('Alice'); b,be=self.connect('Bob')
        for core,events in [(a,ae),(b,be)]:
            core.pm(core.name,'context'); events.wait(lambda m:m.get('text')=='context')
        self.service.gate.clear()
        ra,rb=uuid.uuid4().hex,uuid.uuid4().hex
        a.request_ai(ra,'summary','pm','Alice'); self.assertTrue(self.service.entered.wait(1))
        b.request_ai(rb,'summary','pm','Bob')
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            with server.lock:
                if server.clients['Bob'].pending_ai==rb: break
            time.sleep(.005)
        self.runtime.close()
        for events,rid in [(ae,ra),(be,rb)]:
            event=events.wait(lambda m:m.get('request_id')==rid)
            self.assertEqual((event['type'],event.get('code')),('ai_error','unavailable'))
        with server.lock:
            self.assertIsNone(server.clients['Alice'].pending_ai)
            self.assertIsNone(server.clients['Bob'].pending_ai)
        self.assertEqual(self.ai(b,be,room='Bob')['code'],'unavailable')
