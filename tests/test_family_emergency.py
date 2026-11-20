import asyncio
import importlib.util
import unittest
from pathlib import Path
from city_sdk.city import Message, Need

spec = importlib.util.spec_from_file_location('family_brain', Path(__file__).parents[1] / 'asst/family_counseling/agent/agent.py')
family = importlib.util.module_from_spec(spec)
spec.loader.exec_module(family)


class FamilyCity:
    agent_id = 'agent:team/family'
    def __init__(self, text):
        self.inbound = Message(type='chat', sender='citizen:1', msg_id='m1', need_id='N-1', body={'text': text})
        self.out = []
    async def history(self, **kwargs): return [self.inbound]
    async def say(self, to, text): self.out.append((to, text))


class FamilyEmergencyTest(unittest.TestCase):
    def test_explicit_emergency_flag_and_chest_pain(self):
        for text, flag in [('Aiuto', True), ('Ho dolore al petto e chiedo una diagnosi', False)]:
            with self.subTest(text=text):
                city = FamilyCity(text)
                asyncio.run(family.main(Need(is_emergency=flag), city))
                self.assertEqual(len(city.out), 1)
                self.assertIn('escalate.emergency reason=red_flag', city.out[0][1])
    def test_denied_chest_pain_is_not_a_red_flag(self):
        city = FamilyCity('Non ho dolore al petto')
        self.assertFalse(family._family_emergency(city.inbound))

if __name__ == '__main__': unittest.main()
