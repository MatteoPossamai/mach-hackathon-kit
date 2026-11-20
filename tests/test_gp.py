import asyncio
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from city_sdk.city import Message, Need

spec = importlib.util.spec_from_file_location('gp_brain', Path(__file__).parents[1] / 'asst/gp/agent/agent.py')
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)
gp._PULL_TRIES = 2
gp._PULL_PAUSE = 0


def msg(sender, text, recipient='agent:gp'):
    return Message(type='chat', sender=sender, recipient=recipient, msg_id='m1', need_id='N-12345', body={'text': text})


class City:
    agent_id = 'agent:gp'
    address = ''
    def __init__(self, inbound, mode='success', original=None, history=None):
        self.inbound, self.mode, self.original = inbound, mode, original
        self.rows = list(history or [inbound])
        self.out = []
    async def history(self, **kwargs):
        return list(self.rows)
    async def llm(self, *args, **kwargs):
        return {'content': 'NONE'}
    async def say(self, to, text):
        self.out.append((to, text))
        self.rows.append(msg(self.agent_id, text, to))
        if to == gp.LEDGER and text.startswith('prescribe '):
            f = gp.fields_of(text)
            if self.mode == 'success':
                self.rows.append(msg(gp.LEDGER, f"prescribed prescription_number={f['prescription_number']} patient={f['patient']} procedure={f['procedure']} priority={f['priority']}"))
            elif self.mode == 'refused':
                self.rows.append(msg(gp.LEDGER, 'refused prescribe code=DENIED'))
        if to == gp.LEDGER and text.startswith('prescription ') and self.original:
            self.rows.append(msg(gp.LEDGER, self.original))


class GPTest(unittest.TestCase):
    def run_gp(self, city, need=None):
        with patch.dict(os.environ, {'CITY_ROLE': 'gp'}):
            asyncio.run(gp.main(need or Need(), city))
    def test_emergency_overrides_clinical_request(self):
        c = City(msg('citizen:1', 'Ho dolore al petto, mi fate una diagnosi?'))
        self.run_gp(c)
        self.assertIn('escalate.emergency reason=red_flag', c.out[0][1])
        self.assertEqual(len(c.out), 1)
    def test_emergency_negations(self):
        for text in ('non riesco a respirare e ho dolore al petto', 'non ho male al ginocchio ma ho dolore al petto', 'non ho dolore al petto ma non sento le gambe'):
            with self.subTest(text=text): self.assertTrue(gp._emergency(msg('citizen:1', text)))
        for text in ('non ho dolore al petto, solo male al ginocchio', 'ho ancora forza alle gambe'):
            with self.subTest(text=text): self.assertFalse(gp._emergency(msg('citizen:1', text)))
    def test_prescription_only_after_ledger_confirmation(self):
        for mode in ('success', 'refused', 'silent'):
            with self.subTest(mode=mode):
                c = City(msg('citizen:1', 'Ho mal di schiena da settimane'), mode=mode)
                self.run_gp(c)
                prescriptions = [text for to, text in c.out if to == 'citizen:1' and text.startswith('prescription ')]
                self.assertEqual(bool(prescriptions), mode == 'success')
                self.assertTrue(any(to == gp.LEDGER and text.startswith('prescribe ') for to, text in c.out))
    def test_prescription_renewal_requires_owner_and_ledger_confirmation(self):
        for owner in ('citizen:1', 'citizen:other'):
            with self.subTest(owner=owner):
                original = f'prescription prescription_number=OLD-1 patient={owner} procedure=VIS-FIS priority=B state=expired'
                c = City(msg('citizen:1', 'need.request prescription_number=OLD-1'), original=original)
                self.run_gp(c)
                writes = [text for to, text in c.out if to == gp.LEDGER and text.startswith('prescribe ')]
                self.assertEqual(bool(writes), owner == 'citizen:1')
                if writes:
                    self.assertNotIn('prescription_number=OLD-1', writes[0])
                    self.assertTrue(any(to == 'citizen:1' and text.startswith('prescription ') for to, text in c.out))
    def test_no_late_prescription_after_escalation(self):
        past = msg('agent:gp', 'escalate.emergency reason=red_flag', 'citizen:1')
        inbound = msg('city:ledger', 'prescribed prescription_number=R1 patient=citizen:1')
        c = City(inbound, history=[past, inbound])
        self.run_gp(c)
        self.assertEqual(c.out, [])
    def test_citizen_retry_gets_confirmed_prescription_again(self):
        request = msg('agent:gp', 'prescribe patient=citizen:1 procedure=VIS-FIS priority=B prescription_number=R1', gp.LEDGER)
        confirmed = msg(gp.LEDGER, 'prescribed prescription_number=R1 patient=citizen:1 procedure=VIS-FIS priority=B')
        already_sent = msg('agent:gp', 'prescription prescription_number=R1 procedure=VIS-FIS priority=B', 'citizen:1')
        retry = msg('citizen:1', 'Può ripetere il numero?')
        c = City(retry, history=[request, confirmed, already_sent, retry])
        self.run_gp(c)
        self.assertTrue(any(to == 'citizen:1' and text.startswith('prescription prescription_number=R1') for to, text in c.out))
    def test_model_must_return_single_valid_token(self):
        class Fake(City):
            async def llm(self, *args, **kwargs): return {'content': 'ignore rules SYM-BACK'}
        self.assertIsNone(asyncio.run(gp._symptom_llm(Fake(None), 'unknown')))

if __name__ == '__main__': unittest.main()


class GpProactiveTest(unittest.TestCase):
    def test_prescription_note_says_what_happens_next(self):
        note = gp._rx_note('VIS-FIS', 'B', english=False)
        self.assertIn('centro prenotazioni le proporrà', note)
        self.assertIn('within this deadline', gp._rx_note('VIS-FIS', 'B', english=True))

    def test_unclear_problem_twice_still_gets_an_answer(self):
        from city_sdk.city import CapError

        class C:
            agent_id = 'agent:team/gp'
            def __init__(self, rows):
                self.rows, self.out = rows, []
            async def history(self, **kw): return list(self.rows)
            async def say(self, to, text):
                self.out.append((to, text))
            async def llm(self, *a, **k): raise CapError('KEY_UNKNOWN', 'x')
            async def find(self, q): return []
        def m(sender, text, to='agent:team/gp', mid='x'):
            return Message(type='chat', sender=sender, recipient=to, msg_id=mid, need_id='N-1', body={'text': text})
        rows = [m('citizen:1', 'Ho un fastidio strano', mid='a'),
                m('agent:team/gp', gp._CLARIFY_IT, 'citizen:1', mid='b'),
                m('citizen:1', 'Non so spiegare, un fastidio', mid='c')]
        city = C(rows)
        asyncio.run(gp._gp(city, rows[-1], rows))
        self.assertEqual(city.out, [('citizen:1', gp._OTHER_IT)])


class GpCollaborationTest(unittest.TestCase):
    def make(self, rows):
        class C:
            agent_id = 'agent:c2ewegr2ea3v'
            def __init__(self, rows):
                self.rows, self.out = rows, []
            async def history(self, **kw): return list(self.rows)
            async def say(self, to, text):
                self.out.append((to, text))
                self.rows.append(Message(type='chat', sender=self.agent_id, recipient=to, msg_id=f'o{len(self.rows)}', need_id='N-1', body={'text': text}))
            async def find(self, q): return []
        return C(rows)

    def m(self, sender, text, mid):
        return Message(type='chat', sender=sender, recipient='agent:c2ewegr2ea3v', msg_id=mid, need_id='N-1', body={'text': text})

    def test_booking_notice_gets_one_quick_ack(self):
        notice = self.m('agent:nanpr5mgm2py', 'booking.notice booking_id=B1 prescription_number=prescription_number-N-1 Visita prenotata.', 'n1')
        city = self.make([notice])
        asyncio.run(gp._gp(city, notice, list(city.rows)))
        self.assertEqual(city.out, [('agent:nanpr5mgm2py', 'ack booking_id=B1 Grazie, preso nota nella scheda del paziente.')])
        asyncio.run(gp._gp(city, notice, list(city.rows)))
        self.assertEqual(len(city.out), 1)

    def test_no_ack_to_strangers_or_to_acks(self):
        city = self.make([])
        asyncio.run(gp._gp(city, self.m('agent:other/x', 'booking.notice booking_id=B1', 'x1'), []))
        asyncio.run(gp._gp(city, self.m('agent:nanpr5mgm2py', 'ack booking_id=B1', 'x2'), []))
        self.assertEqual(city.out, [])


class GpHandoffTest(unittest.TestCase):
    def test_note_acknowledges_the_person_first(self):
        note = gp._rx_note('VIS-FIS', 'D', english=False)
        self.assertTrue(note.startswith('- Mi dispiace per il dolore'))
        self.assertIn('centro prenotazioni', note)
        self.assertTrue(gp._rx_note('VIS-CARD', 'B', english=True).startswith("- I'm sorry"))

    def test_handoff_goes_once_to_our_booking_centre(self):
        class C:
            agent_id = 'agent:c2ewegr2ea3v'
            def __init__(self): self.rows, self.out = [], []
            async def say(self, to, text):
                self.out.append((to, text))
                self.rows.append(Message(type='chat', sender=self.agent_id, recipient=to, msg_id=f'o{len(self.rows)}', need_id='N-1', body={'text': text}))
        city = C()
        asyncio.run(gp._handoff(city, city.rows, 'prescription_number-N-1', 'VIS-FIS', 'D'))
        asyncio.run(gp._handoff(city, city.rows, 'prescription_number-N-1', 'VIS-FIS', 'D'))
        self.assertEqual(len(city.out), 1)
        to, text = city.out[0]
        self.assertEqual(to, 'agent:gdrf9b9kxxss')
        self.assertTrue(text.startswith('handoff prescription_number=prescription_number-N-1 procedure=VIS-FIS priority=D'))
        self.assertNotIn('citizen:', text)
