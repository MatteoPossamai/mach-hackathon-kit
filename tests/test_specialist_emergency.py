import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from city_sdk.city import Message, Need

spec = importlib.util.spec_from_file_location('specialist_brain_test', Path(__file__).parents[1] / 'asst/specialist/agent/agent.py')
specialist = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = specialist
spec.loader.exec_module(specialist)


class City:
    agent_id = 'agent:team/specialist'
    def __init__(self):
        self.inbound = Message(type='chat', sender='citizen:1', msg_id='m1', need_id='N-1', body={'text': 'Ho dolore al petto, vorrei una diagnosi'})
        self.out = []
    async def history(self, **kwargs): return [self.inbound]
    async def say(self, to, text): self.out.append((to, text))


class SpecialistEmergencyTest(unittest.TestCase):
    def test_emergency_takes_priority_over_diagnosis_request(self):
        city = City()
        asyncio.run(specialist.main(Need(), city))
        self.assertEqual(len(city.out), 1)
        self.assertIn('escalate.emergency reason=red_flag', city.out[0][1])

    def test_initial_visit_does_not_automatically_create_follow_up(self):
        self.assertFalse(specialist._needs_follow_up([], {
            'procedure': 'VIS-CARD', 'priority': 'B', 'symptom_code': 'SYM-PALPITATIONS'
        }))

    def test_explicit_care_team_follow_up_is_honored(self):
        clinician = Message(type='chat', sender='agent:c2ewegr2ea3v', msg_id='m2', need_id='N-2',
                            body={'text': 'follow_up=true'})
        self.assertTrue(specialist._needs_follow_up([clinician], {
            'procedure': 'VIS-CARD', 'priority': 'B'
        }))

    def test_citizen_request_cannot_trigger_prescription(self):
        citizen = Message(type='chat', sender='citizen:1', msg_id='m3', need_id='N-3',
                          body={'text': 'Vorrei una ricetta di controllo'})
        self.assertFalse(specialist._needs_follow_up([citizen], {
            'procedure': 'VIS-CARD', 'priority': 'B'
        }))

    def test_slow_ledger_declines_ring_requests_without_queueing_reads(self):
        """15:43: 19 msgs/min, each ring request added Ledger reads to our ASST's slow queue."""
        import specialist_guard as guard
        guard._LEDGER_SLOW_UNTIL[0] = 0.0
        try:
            guard.mark_ledger_slow()
            ring = Message(type='chat', sender='agent:other/bc', msg_id='ring-s1', need_id='N-RS',
                           body={'text': 'need.request service_type=specialist_care.cardiology procedure=VIS-CARD priority=B request_id=R5'})
            city = City(); city.inbound = ring
            async def history(**kwargs): return [ring]
            city.history = history
            asyncio.run(specialist._on_agent_availability_request(city, ring, [ring], 'N-RS'))
            self.assertFalse([t for to, t in city.out if to == 'city:ledger'], city.out)
            self.assertTrue(any(to == 'agent:other/bc' and t.startswith('cancel request_id=R5 reason=no_availability')
                                for to, t in city.out), city.out)
        finally:
            guard._LEDGER_SLOW_UNTIL[0] = 0.0

if __name__ == '__main__': unittest.main()
