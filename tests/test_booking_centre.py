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
booking.RECEIPT_WAIT = 0.05


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
    async def llm(self, messages, need_id=None):
        from city_sdk.city import CapError
        raise CapError('KEY_UNKNOWN', 'no gateway in tests')


class BookingImprovementsTest(unittest.TestCase):
    def test_rx_number_in_free_text(self):
        rows = [msg('citizen:1', 'Buongiorno, la mia ricetta è prescription_number-N-2026-11-02-890910000031 grazie')]
        self.assertEqual(booking.rx_from_text(rows, 'citizen:1', rows[0].text), 'prescription_number-N-2026-11-02-890910000031')
        self.assertEqual(booking.rx_from_text([], 'citizen:1', 'nessun numero'), '')

    def test_relax_soft_prefs_keep_language(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        slot = dict(slot_id='s1', doctor_id='doctor-h2', facility='agent:t/h', procedure='VIS-FIS', at='2026-11-21T12:00:00+00:00', day='thu')
        self.assertEqual(booking.choose_slots([slot], 'VIS-FIS', 'B', now, {'gender': 'f'})[1], True)
        self.assertEqual(booking.choose_slots([slot], 'VIS-FIS', 'B', now, {'lang': 'en'}), ([], False))
        self.assertEqual(booking.choose_slots([slot], 'VIS-FIS', 'B', now, {}), ([slot], False))

    def test_nl_preferences_parsed_and_validated(self):
        class LlmCity(FakeCity):
            async def llm(self, messages, need_id=None):
                return {'content': 'Sure: {"lang": "en", "gender": "F", "days": "tue/sat/thu"}'}
        rows = [msg('citizen:1', 'I need a female doctor, I can come Tuesday or Thursday')]
        prefs = asyncio.run(booking.nl_preferences(LlmCity(rows), rows, 'citizen:1'))
        self.assertEqual(prefs, {'lang': 'en', 'gender': 'f', 'days': 'tue/thu'})


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
        self.assertEqual(city.out[0], ('agent:t/h', 'confirm offer_id=o1 slot_id=real prescription_number=rx1 citizen=citizen:1 consent_ref=c1'))
        self.assertTrue(all(to != 'agent:evil/h' for to, _ in city.out))

    def test_unsolicited_peer_cannot_trigger_consent_or_receipt(self):
        for text in ('offer slot_id=s1', 'booking.receipt booking_id=b1 ledger_ref=r1'):
            incoming = msg('agent:evil/h', text)
            city = FakeCity([msg('citizen:1', 'need.request prescription_number=rx1'), incoming])
            asyncio.run(booking.handle(city, incoming))
            self.assertEqual(city.out, [])

    def test_all_contacted_facilities_unavailable_informs_citizen(self):
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry', 'agent:partner/h')]
        incoming = msg('agent:partner/h', 'unavailable procedure=VIS-FIS')
        city = FakeCity(rows + [incoming])
        asyncio.run(booking.handle(city, incoming))
        self.assertTrue(any(to == 'citizen:1' and 'non hanno trovato posti' in text for to, text in city.out))

    def test_first_unavailable_does_not_hide_other_pending_partners(self):
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry', 'agent:partner/h'),
                msg(city_id(), 'need.request service_type=hospital.physiatry', 'agent:partner2/h')]
        incoming = msg('agent:partner/h', 'unavailable procedure=VIS-FIS')
        city = FakeCity(rows + [incoming])
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
        self.assertIn('service_type=hospital.physiatry', query)

    def test_rare_language_is_not_assumed(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        slot = dict(slot_id='s1', doctor_id='doctor-h3', facility='agent:t/h', procedure='VIS-FIS', at='2026-11-21T12:00:00+00:00', day='sat')
        self.assertFalse(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'lang': 'ja'}))
        slot['languages'] = 'it/ja'
        self.assertTrue(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'lang': 'ja'}))

    def test_remote_confirmation_requires_cross_asst_consent(self):
        offer = 'offer offer_id=o1 slot_id=s1 facility=agent:partner/h prescription_number=rx1'
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS', 'agent:partner/h'),
                msg(city_id(), offer, 'citizen:1')]
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=booking-only')
        city = FakeCity(rows + [incoming])
        asyncio.run(booking.handle(city, incoming))
        self.assertTrue(any(to == 'citizen:1' and text.startswith('consent purpose=cross_asst') for to, text in city.out))
        self.assertFalse(any(to == 'agent:partner/h' for to, _ in city.out))

    def test_booking_centre_escalates_chest_pain(self):
        incoming = msg('citizen:1', 'Ho dolore al petto e voglio una diagnosi')
        city = FakeCity([incoming])
        city.inbound = incoming
        asyncio.run(booking.main(SimpleNamespace(is_emergency=False), city))
        self.assertEqual(len(city.out), 1)
        self.assertIn('escalate.emergency reason=red_flag', city.out[0][1])

    def test_remote_offer_from_uncontacted_facility_is_rejected(self):
        rows = [msg('city:ledger', 'agenda agenda_now=2026-11-20T00:00:00+00:00'),
                msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B'),
                msg('agent:evil/h', 'offer slot_id=s1 doctor_id=doctor-h1 facility=agent:evil/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat')]
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        self.assertNotIn('offer offer_id=', city.out[0][1])


def city_id():
    return 'agent:team/booking'

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

    def test_cancelled_slot_is_replaced(self):
        async def run():
            booking.RELAYED.clear()
            class TwoSlots(JourneyCity):
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1\n'
                            'slot_id=s1 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat free=1\n'
                            'slot_id=s2 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T13:00:00+00:00 day=sat free=1', self.agent_id)
            city = TwoSlots()
            await city.receive('citizen:1', 'need.request prescription_number=rx1 lang=en')
            first = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
            selected = booking.fields(first.splitlines()[0])['offer_id']
            await city.receive('citizen:1', f'confirm offer_id={selected} consent_ref=c1')
            await city.receive('agent:team/h', f'cancel offer_id={selected}')
            results = [text for to, text in city.out if to == 'citizen:1' and text.startswith('offer ')]
            self.assertEqual(len(results), 2)
            self.assertIn('slot_id=s2', results[-1])
            self.assertNotIn('slot_id=s1', results[-1])
        asyncio.run(run())


class ConfirmRetryTest(unittest.TestCase):
    """A slow facility must not cause duplicate booking attempts or silence."""

    def setUp(self):
        booking.RELAYED.clear()

    def rows(self):
        offer = 'offer offer_id=o1 slot_id=real facility=agent:t/h prescription_number=rx1'
        return [msg('citizen:1', 'need.request prescription_number=rx1', mid='a'),
                msg(city_id(), offer, 'citizen:1', mid='b')]

    def test_silent_facility_gets_one_request_then_citizen_is_told(self):
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=c1', mid='c')
        city = FakeCity(self.rows() + [incoming])
        asyncio.run(booking.handle(city, incoming))
        confirms = [t for to, t in city.out if to == 'agent:t/h']
        self.assertEqual(len(confirms), 1)
        self.assertEqual(city.out[-1][0], 'citizen:1')
        self.assertIn('ricevuta', city.out[-1][1])

    def test_late_receipt_is_relayed_once(self):
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=c1', mid='c')
        city = FakeCity(self.rows() + [incoming])
        asyncio.run(booking.handle(city, incoming))
        city.rows.append(msg(city_id(), 'confirm offer_id=o1 slot_id=real', 'agent:t/h', 'd'))
        receipt = msg('agent:t/h', 'booking.receipt booking_id=b1 ledger_ref=r1', city_id(), 'r')
        city.rows.append(receipt)
        asyncio.run(booking.handle(city, receipt))
        asyncio.run(booking.handle(city, receipt))
        self.assertEqual(city.out[-1], ('citizen:1', 'booking.receipt booking_id=b1 ledger_ref=r1'))
        self.assertEqual(sum(1 for to, t in city.out if t.startswith('booking.receipt')), 1)



class BookingPerfectionTest(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.REBOOKS.clear()

    def test_words_add_to_tokens_when_llm_is_down(self):
        rows = [msg('citizen:1', 'I would like a female doctor, on Tuesday please. need.request lang=en prescription_number=rx1')]
        prefs = asyncio.run(booking.citizen_preferences(FakeCity(rows), rows, 'citizen:1'))
        self.assertEqual(prefs, {'lang': 'en', 'gender': 'f', 'days': 'tue'})

    def test_salutation_is_not_a_gender_preference(self):
        self.assertEqual(booking.keyword_preferences(['Gentile dottoressa, ho mal di schiena']), {})
        self.assertNotIn('days', booking.keyword_preferences(['non posso il lunedì']))

    def test_doctor_attributes_from_world(self):
        lines = ['doctor doctor_id=asst-x-h2 facility=agent:a languages=it/fr gender=m days=wed/thu/fri',
                 'slot_id=s1 doctor_id=asst-x-h2 facility=agent:a procedure=VIS-FIS']
        attrs = booking.doctor_attrs(lines)
        self.assertEqual(attrs, {'asst-x-h2': {'languages': 'it/fr', 'gender': 'm', 'doctor_days': 'wed/thu/fri'}})
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        slot = booking.enrich(dict(slot_id='s1', doctor_id='asst-x-h2', facility='agent:a', procedure='VIS-FIS',
                                   at='2026-11-21T12:00:00+00:00', day='fri'), attrs)
        self.assertTrue(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'lang': 'fr'}))
        self.assertFalse(booking.valid_slot(slot, 'VIS-FIS', 'B', now, {'lang': 'en'}))

    def test_offer_message_holds_many_varied_slots(self):
        items = []
        for d, day in enumerate(('mon', 'tue', 'wed')):
            for doc in ('h1', 'h2', 'h3'):
                for hh in ('10:30', '12:00', '14:30', '16:00'):
                    items.append(dict(slot_id=f'W01QQQ-asst-lambrate-{doc}-2026110{d+1}-{hh.replace(":", "")}',
                                      doctor_id=f'asst-lambrate-{doc}', facility='agent:nanpr5mgm2py',
                                      at=f'2026-11-0{d+1}T{hh}:00.000Z', day=day, languages='it/en', gender='f'))
        lines = booking.pick_offers(items, 'rx', 'B', 1950 - len(booking.OFFER_NOTE))
        self.assertGreaterEqual(len(lines), 8)
        self.assertLessEqual(sum(len(l) + 1 for l in lines) + len(booking.OFFER_NOTE), 2000)
        self.assertEqual(len({l.split('doctor_id=')[1].split()[0] for l in lines}), 3)
        self.assertGreaterEqual(len({l.split('agenda_day=')[1].split()[0] for l in lines}), 2)

    def test_partners_by_capability_never_our_agents(self):
        class RingCity(FakeCity):
            queries = []
            async def find(self, query):
                self.queries.append(query)
                return [SimpleNamespace(agent_id='agent:nanpr5mgm2py', role='facility', capabilities=('hospital.physiatry',)),
                        SimpleNamespace(agent_id='agent:other/h', role='facility', capabilities=('hospital.physiatry',)),
                        SimpleNamespace(agent_id='agent:other/s', role='facility', capabilities=('specialist_care.cardiology',))]
        city = RingCity([])
        asyncio.run(booking.find_partners(city, 'case-1', 'citizen:1', 'VIS-FIS', 'B', []))
        self.assertEqual(city.queries[0], 'hospital.physiatry')
        self.assertEqual([to for to, t in city.out if t.startswith('need.request')], ['agent:other/h'])

    def test_cross_consent_travels_with_confirm(self):
        offer = 'offer offer_id=o1 slot_id=s9 facility=agent:other/h prescription_number=rx1'
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=book1', mid='c')
        rows = [msg('citizen:1', 'need.request prescription_number=rx1', mid='a'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B', 'agent:other/h', mid='a0'),
                msg(city_id(), 'consent purpose=cross_asst Posso condividere?', 'citizen:1', mid='a1'),
                msg('citizen:1', 'consent purpose=cross_asst consent_ref=cross1', mid='a2'),
                msg(city_id(), offer, 'citizen:1', mid='b'), incoming]
        city = FakeCity(rows)
        asyncio.run(booking.handle(city, incoming))
        self.assertIn('cross_asst_consent_ref=cross1', city.out[0][1])
        self.assertIn('consent_ref=book1', city.out[0][1])


class RebookTest(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.REBOOKS.clear()

    def test_facility_cancel_brings_new_offers_without_the_lost_slot(self):
        async def run():
            class TwoSlots(JourneyCity):
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1\n'
                            'slot_id=s1 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=sat\n'
                            'slot_id=s2 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T14:30:00+00:00 day=sat',
                            self.agent_id, f'w-{len(self.rows)}')
                    if to == 'agent:team/h' and text.startswith('confirm ') and 'slot_id=s1' in text:
                        self.rows.append(msg('agent:team/h', f"cancel {text.split()[1]} reason=slot_taken", self.agent_id, f'c-{len(self.rows)}'))
            city = TwoSlots()
            await city.receive('citizen:1', 'need.request prescription_number=rx1')
            first = next(t for to, t in city.out if to == 'citizen:1' and t.startswith('offer '))
            o1 = next(l for l in first.splitlines() if 'slot_id=s1' in l)
            await city.receive('citizen:1', f"confirm {booking.fields(o1)['offer_id'] and 'offer_id=' + booking.fields(o1)['offer_id']} consent_ref=c1")
            self.assertTrue(any(to == 'citizen:1' and t.startswith('cancel ') for to, t in city.out))
            again = [t for to, t in city.out if to == 'citizen:1' and t.startswith('offer ')]
            self.assertEqual(len(again), 2)
            self.assertNotIn('slot_id=s1', again[1])
            self.assertIn('slot_id=s2', again[1])
        asyncio.run(run())



class LateConfirmTest(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.REBOOKS.clear()

    def test_confirm_resent_after_the_slot_passed_gets_new_offers(self):
        async def run():
            class Clock(JourneyCity):
                late = False
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1\n'
                            'slot_id=s1 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-20T12:00:00+00:00 day=fri\n'
                            'slot_id=s2 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T14:30:00+00:00 day=sat',
                            self.agent_id, f'w-{len(self.rows)}')
                    if self.late and to == 'city:ledger' and text == 'agenda':
                        self.rows[-1] = msg('city:ledger', 'agenda agenda_now=2026-11-20T13:00:00+00:00', self.agent_id, f'a-{len(self.rows)}')
            city = Clock()
            await city.receive('citizen:1', 'need.request prescription_number=rx1')
            first = next(t for to, t in city.out if to == 'citizen:1' and t.startswith('offer '))
            o1 = booking.fields(next(l for l in first.splitlines() if 'slot_id=s1' in l))['offer_id']
            city.late = True
            await city.receive('citizen:1', f'confirm offer_id={o1} consent_ref=c1')
            self.assertFalse(any(to == 'agent:team/h' for to, t in city.out))
            self.assertTrue(any(t.startswith(f'cancel offer_id={o1} reason=slot_expired') for to, t in city.out))
            again = [t for to, t in city.out if to == 'citizen:1' and t.startswith('offer ')]
            self.assertEqual(len(again), 2)
            self.assertIn('slot_id=s2', again[1])
            self.assertNotIn('slot_id=s1', again[1])
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
