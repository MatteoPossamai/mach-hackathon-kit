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

if __name__ == '__main__': unittest.main()
