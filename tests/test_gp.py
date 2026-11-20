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
