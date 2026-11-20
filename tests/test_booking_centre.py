import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from city_sdk.city import Message

spec = importlib.util.spec_from_file_location('booking', Path(__file__).parents[1] / 'asst/booking_centre/agent/agent.py')
booking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(booking)


def msg(sender, text, recipient='agent:team/booking', mid='x'):
    return Message(type='chat', sender=sender, recipient=recipient, msg_id=mid, need_id='case-1', body={'text': text})


class FakeCity:
    agent_id = 'agent:team/booking'
    def __init__(self, rows):
        self.rows = rows
        self.out = []
    async def history(self, *, need_id):
        return self.rows
    async def say(self, to, text):
        self.out.append((to, text))


class BookingTest(unittest.TestCase):
    def test_filters_priority_preference_and_reserve(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        slot = dict(slot_id='s1', doctor_id='doctor-h1', facility='agent:t/h', procedure='VIS-FIS', at='2026-11-21T12:00:00+00:00', day='sat')
        self.assertTrue(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'lang': 'en', 'gender': 'f'}))
        self.assertFalse(booking.valid_slot(slot, 'VIS-FIS', 'U', now, {}))
        self.assertFalse(booking.valid_slot({**slot, 'urgent_reserve': 'true'}, 'VIS-FIS', 'B', now, {}))
        self.assertFalse(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'gender': 'm'}))

    def test_confirm_uses_our_offer_not_citizen_overrides(self):
        offer = 'offer offer_id=o1 slot_id=real facility=agent:t/h prescription_number=rx1'
        incoming = msg('citizen:1', 'confirm offer_id=o1 slot_id=evil facility=agent:evil/h consent_ref=c1')
        city = FakeCity([msg('citizen:1', 'need.request prescription_number=rx1'), msg(city_id(), offer, 'citizen:1'), incoming])
        asyncio.run(booking.handle(city, incoming))
        self.assertEqual(city.out, [('agent:t/h', 'confirm offer_id=o1 slot_id=real prescription_number=rx1 citizen=citizen:1 consent_ref=c1')])

    def test_unsolicited_peer_cannot_trigger_consent_or_receipt(self):
        for text in ('offer slot_id=s1', 'booking.receipt booking_id=b1 ledger_ref=r1'):
            incoming = msg('agent:evil/h', text)
            city = FakeCity([msg('citizen:1', 'need.request prescription_number=rx1'), incoming])
            asyncio.run(booking.handle(city, incoming))
            self.assertEqual(city.out, [])

    def test_offer_parser_handles_multiple_lines(self):
        city = city_id()
        rows = [msg(city, 'offer offer_id=a slot_id=s1\noffer offer_id=b slot_id=s2', 'citizen:1')]
        self.assertEqual(set(booking.offers(rows, city, 'citizen:1')), {'a', 'b'})

    def test_partner_search_does_not_share_citizen_data(self):
        class SearchingCity(FakeCity):
            async def find(self, query):
                return [SimpleNamespace(agent_id='agent:partner/h', role='facility')]
        city = SearchingCity([])
        asyncio.run(booking.find_partners(city, 'case-1', 'citizen:secret', 'VIS-FIS', 'B', []))
        query = city.out[0][1]
        self.assertNotIn('citizen:secret', query)
        self.assertNotIn('prescription_number', query)

    def test_remote_offer_from_uncontacted_facility_is_rejected(self):
        rows = [msg('city:ledger', 'agenda agenda_now=2026-11-20T00:00:00+00:00'),
                msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B'),
                msg('agent:evil/h', 'offer slot_id=s1 doctor_id=doctor-h1 facility=agent:evil/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat')]
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        self.assertNotIn('offer offer_id=', city.out[0][1])


def city_id():
    return 'agent:team/booking'

if __name__ == '__main__':
    unittest.main()

class JourneyCity(FakeCity):
    """Simulated mailbox: Ledger responses are delivered into the case history."""
    def __init__(self):
        super().__init__([])
        self.inbound = None
    async def say(self, to, text):
        self.out.append((to, text))
        self.rows.append(msg(self.agent_id, text, to, f'out-{len(self.rows)}'))
        replies = {
            'prescription': 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B',
            'agenda': 'agenda agenda_now=2026-11-20T08:00:00+00:00',
            'rules': 'rules version=1',
            'world': ('slots page=1/1\nslot_id=s1 doctor_id=doctor-h1 facility=agent:team/h '
                      'procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat free=1'),
        }
        if to == 'city:ledger' and booking.act(text) in replies:
            self.rows.append(msg('city:ledger', replies[booking.act(text)], self.agent_id, f'ledger-{len(self.rows)}'))
    async def receive(self, sender, text):
        incoming = msg(sender, text, self.agent_id, f'in-{len(self.rows)}')
        self.rows.append(incoming)
        self.inbound = incoming
        await booking.main(SimpleNamespace(), self)


class JourneyTest(unittest.TestCase):
    def test_local_booking_through_receipt(self):
        async def run():
            city = JourneyCity()
            await city.receive('citizen:1', 'need.request prescription_number=rx1 lang=en')
            offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
            offer_id = booking.fields(offer)['offer_id']
            await city.receive('citizen:1', f'confirm offer_id={offer_id} consent_ref=c1')
            self.assertTrue(any(to == 'agent:team/h' and text.startswith('confirm ') for to, text in city.out))
            await city.receive('agent:team/h', 'booking.receipt booking_id=b1 ledger_ref=r1')
            self.assertIn(('citizen:1', 'booking.receipt booking_id=b1 ledger_ref=r1'), city.out)
        asyncio.run(run())

    def test_partner_offer_consent_and_confirmation(self):
        async def run():
            class PartnerCity(JourneyCity):
                async def find(self, query):
                    return [SimpleNamespace(agent_id='agent:partner/h', role='facility')]
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1', self.agent_id)
            city = PartnerCity()
            await city.receive('citizen:1', 'need.request prescription_number=rx1 lang=en')
            search = next(text for to, text in city.out if to == 'agent:partner/h')
            self.assertNotIn('citizen:1', search)
            await city.receive('agent:partner/h', 'offer slot_id=s2 doctor_id=doctor-h1 facility=agent:partner/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat')
            self.assertTrue(any(text.startswith('consent purpose=cross_asst') for to, text in city.out if to == 'citizen:1'))
            await city.receive('citizen:1', 'consent purpose=cross_asst consent_ref=cross1')
            offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
            await city.receive('citizen:1', f"confirm offer_id={booking.fields(offer)['offer_id']}")
            self.assertTrue(any(to == 'agent:partner/h' and 'consent_ref=cross1' in text and 'citizen=citizen:1' in text for to, text in city.out))
        asyncio.run(run())
