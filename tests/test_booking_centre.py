import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from city_sdk.city import Message, Peer

PRECOTTO_HOSPITAL = 'agent:s1twcdzg3296'
GIAMBELLINO_HOSPITAL = 'agent:h6r1v45esk77'
PRECOTTO_SPECIALIST = 'agent:f2s88vwd3jnf'
PRECOTTO_GP = 'agent:n4yjr7kjbmrx'
BICOCCA_HOSPITAL = 'agent:bb01ggxymm05'


spec = importlib.util.spec_from_file_location('booking', Path(__file__).parents[1] / 'asst/booking_centre/agent/agent.py')
booking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(booking)
booking.bc_flow.RECEIPT_WAIT = 0.05
booking.bc_flow.RING_WAIT = 0.05
booking.bc_common.DEDUP_SECONDS = 0.0  # tests reuse texts across cases; DedupTest covers it


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
    def test_priority_windows_count_agenda_days_across_the_weekend(self):
        friday = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        slot = dict(slot_id='s1', doctor_id='doctor-h1', facility='agent:t/h', procedure='VIS-FIS')
        for priority, cutoff_date in [('B', '2026-11-24'), ('D', '2026-11-27')]:
            with self.subTest(priority=priority):
                at_cutoff = {**slot, 'at': f'{cutoff_date}T10:00:00+00:00'}
                after_cutoff = {**slot, 'at': f'{cutoff_date}T10:01:00+00:00'}
                self.assertTrue(booking.valid_slot(at_cutoff, 'VIS-FIS', priority, friday, {}))
                self.assertFalse(booking.valid_slot(after_cutoff, 'VIS-FIS', priority, friday, {}))

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
        booking.bc_flow.FALLEN_BACK.clear()
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q',
                    PRECOTTO_HOSPITAL)]
        incoming = msg(PRECOTTO_HOSPITAL, 'unavailable procedure=VIS-FIS')
        city = FakeCity(rows + [incoming])
        asyncio.run(booking.handle(city, incoming))
        self.assertTrue(any(to == 'citizen:1' and 'strutture partner' in text for to, text in city.out))

    def test_first_unavailable_does_not_hide_other_pending_partners(self):
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q',
                    PRECOTTO_HOSPITAL),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q',
                    GIAMBELLINO_HOSPITAL)]
        incoming = msg(PRECOTTO_HOSPITAL, 'unavailable procedure=VIS-FIS')
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
                return [Peer(PRECOTTO_HOSPITAL, role='facility', capabilities=('hospital.physiatry',))]
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
        offer = f'offer offer_id=o1 slot_id=s1 facility={PRECOTTO_HOSPITAL} prescription_number=rx1'
        rows = [msg('citizen:1', 'need.request prescription_number=rx1'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS', PRECOTTO_HOSPITAL),
                msg(city_id(), offer, 'citizen:1')]
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=booking-only')
        city = FakeCity(rows + [incoming])
        asyncio.run(booking.handle(city, incoming))
        self.assertTrue(any(to == 'citizen:1' and text.startswith('consent purpose=cross_asst') for to, text in city.out))
        self.assertFalse(any(to == PRECOTTO_HOSPITAL for to, _ in city.out))

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
                msg(PRECOTTO_HOSPITAL, f'offer offer_id=unasked slot_id=s1 doctor_id=asst-precotto-h1 '
                    f'facility={PRECOTTO_HOSPITAL} procedure=VIS-FIS agenda_date=2026-11-20T12:00:00+00:00 request_id=q')]
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        self.assertFalse(any('offer offer_id=' in text for _, text in city.out))

    def test_partner_offer_requesting_private_data_does_not_trigger_consent(self):
        own = city_id()
        incoming = msg(PRECOTTO_HOSPITAL, f'offer offer_id=o1 slot_id=s1 doctor_id=asst-precotto-h1 '
                       f'facility={PRECOTTO_HOSPITAL} procedure=VIS-FIS request_id=q '
                       'agenda_date=2026-11-20T12:00:00 prescription_number=required')
        history = [msg('citizen:1', 'need.request prescription_number=rx1'),
                   msg('city:ledger', 'agenda agenda_now=2026-11-20T08:00:00+00:00'),
                   msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B'),
                   msg(own, 'need.request v=1.2 procedure=VIS-FIS priority=B request_id=q', PRECOTTO_HOSPITAL),
                   incoming]
        city = FakeCity(history)
        asyncio.run(booking.handle(city, incoming))
        self.assertEqual(city.out, [])


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
            self.assertTrue(any(to == 'citizen:1' and t.startswith('booking.receipt booking_id=b1 ledger_ref=r1') for to, t in city.out))
        asyncio.run(run())

    def test_partner_offer_consent_and_confirmation(self):
        async def run():
            class PartnerCity(JourneyCity):
                async def find(self, query):
                    return [Peer(PRECOTTO_HOSPITAL, role='facility', capabilities=('hospital.physiatry',))]
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1', self.agent_id)
            city = PartnerCity()
            booking.RELAYED.clear()
            await city.receive('citizen:1', 'need.request prescription_number=rx1 lang=en days=fri')
            search = next(text for to, text in city.out if to == PRECOTTO_HOSPITAL)
            request_id = booking.fields(search)['request_id']
            self.assertNotIn('citizen:1', search)
            self.assertNotIn('rx1', search)
            self.assertIn('lang=en days=fri', search)
            common = (f'facility={PRECOTTO_HOSPITAL} procedure=VIS-FIS agenda_date=2026-11-20T12:00:00+00:00 '
                      f'request_id={request_id}')
            await city.receive(PRECOTTO_HOSPITAL,
                               f'offer offer_id=partner1 slot_id=s2 doctor_id=asst-precotto-h2 doctor_lang=it {common}\n'
                               f'offer offer_id=partner2 slot_id=s3 doctor_id=asst-precotto-h1 doctor_lang=it/en {common}')
            self.assertTrue(any(text.startswith('consent purpose=cross_asst') for to, text in city.out if to == 'citizen:1'))
            self.assertFalse(any(text.startswith('confirm ') for _, text in city.out))
            await city.receive('citizen:1', 'consent purpose=cross_asst consent_ref=cross1')
            offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer ')
                         and 'waiting_list=true' not in text)
            self.assertEqual(booking.fields(offer)['offer_id'], 'partner2')
            await city.receive('citizen:1', 'confirm offer_id=partner2 consent_ref=book1')
            self.assertIn((PRECOTTO_HOSPITAL, 'confirm offer_id=partner2 slot_id=s3 prescription_number=rx1 '
                           'citizen=citizen:1 consent_ref=book1 cross_asst_consent_ref=cross1'), city.out)
            await city.receive(PRECOTTO_HOSPITAL, 'booking.receipt booking_id=b2 ledger_ref=r2')
            self.assertTrue(any(to == 'citizen:1' and text.startswith('booking.receipt booking_id=b2 ledger_ref=r2')
                                for to, text in city.out))
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
        self.assertTrue(city.out[-1][0] == 'citizen:1' and city.out[-1][1].startswith('booking.receipt booking_id=b1 ledger_ref=r1'))
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
                        SimpleNamespace(agent_id=PRECOTTO_HOSPITAL, role='facility', capabilities=('hospital.physiatry',)),
                        SimpleNamespace(agent_id=PRECOTTO_SPECIALIST, role='facility', capabilities=('specialist_care.cardiology',))]
        city = RingCity([])
        asyncio.run(booking.find_partners(city, 'case-1', 'citizen:1', 'VIS-FIS', 'B', []))
        self.assertEqual(city.queries[0], 'hospital.physiatry')
        self.assertEqual([to for to, t in city.out if t.startswith('need.request')], [PRECOTTO_HOSPITAL])

    def test_cross_consent_travels_with_confirm(self):
        offer = f'offer offer_id=o1 slot_id=s9 facility={PRECOTTO_HOSPITAL} prescription_number=rx1'
        incoming = msg('citizen:1', 'confirm offer_id=o1 consent_ref=book1', mid='c')
        rows = [msg('citizen:1', 'need.request prescription_number=rx1', mid='a'),
                msg(city_id(), 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B', PRECOTTO_HOSPITAL, mid='a0'),
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

    def test_facility_cancel_rebooks_within_the_original_deadline(self):
        async def run():
            class MovingAgenda(JourneyCity):
                agenda = '2026-11-16T08:00:00+00:00'

                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text == 'agenda':
                        self.rows[-1] = msg('city:ledger', f'agenda agenda_now={self.agenda}',
                                            self.agent_id, f'agenda-{len(self.rows)}')
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1\n'
                            'slot_id=s1 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-17T12:00:00+00:00 day=tue\n'
                            'slot_id=s2 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-18T08:00:00+00:00 day=wed\n'
                            'slot_id=s3 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-18T10:00:00+00:00 day=wed\n'
                            'slot_id=s4 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-16T10:00:00+00:00 day=mon',
                            self.agent_id, f'w-{len(self.rows)}')
                    if to == 'agent:team/h' and text.startswith('confirm ') and 'slot_id=s1' in text:
                        self.agenda = '2026-11-16T12:00:00+00:00'
                        self.rows.append(msg('agent:team/h', f"cancel {text.split()[1]} reason=slot_taken", self.agent_id, f'c-{len(self.rows)}'))
            city = MovingAgenda()
            await city.receive('citizen:1', 'need.request prescription_number=rx1')
            first = next(t for to, t in city.out if to == 'citizen:1' and t.startswith('offer '))
            self.assertIn('slot_id=s4', first)
            o1 = next(l for l in first.splitlines() if 'slot_id=s1' in l)
            await city.receive('citizen:1', f"confirm offer_id={booking.fields(o1)['offer_id']} consent_ref=c1")
            self.assertTrue(any(to == 'citizen:1' and t.startswith('cancel ') for to, t in city.out))
            again = [t for to, t in city.out if to == 'citizen:1' and t.startswith('offer ')]
            self.assertEqual(len(again), 2)
            self.assertNotIn('slot_id=s1', again[1])
            self.assertNotIn('slot_id=s3', again[1])
            self.assertNotIn('slot_id=s4', again[1])
            replacement = next(line for line in again[1].splitlines() if 'slot_id=s2' in line)
            offer_id = booking.fields(replacement)['offer_id']
            await city.receive('citizen:1', f'confirm offer_id={offer_id} consent_ref=c1')
            await city.receive('agent:team/h', f'booking.receipt booking_id=rebook-1 ledger_ref=r1 offer_id={offer_id}')
            self.assertTrue(any(to == 'citizen:1' and text.startswith('booking.receipt booking_id=rebook-1 ')
                                and 'slot_id=s2' in text for to, text in city.out))
        asyncio.run(run())

    def test_partner_offers_keep_the_original_deadline_across_a_weekend(self):
        me = FakeCity.agent_id
        partner = PRECOTTO_HOSPITAL
        rows = [
            msg(me, 'prescription prescription_number=rx1', 'city:ledger'),
            msg('city:ledger', 'agenda agenda_now=2026-11-20T08:00:00+00:00'),
            msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B'),
            msg('city:ledger', 'agenda agenda_now=2026-11-23T12:00:00+00:00'),
            msg(me, 'need.request procedure=VIS-FIS priority=B request_id=req1', partner),
            msg(partner, f'offer offer_id=early slot_id=partner-early doctor_id=h1 facility={partner} '
                'procedure=VIS-FIS request_id=req1 agenda_date=2026-11-24T08:00:00+00:00\n'
                f'offer offer_id=late slot_id=partner-late doctor_id=h1 facility={partner} '
                'procedure=VIS-FIS request_id=req1 agenda_date=2026-11-24T10:00:00+00:00'),
        ]
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
        self.assertIn('slot_id=partner-early', offer)
        self.assertNotIn('slot_id=partner-late', offer)



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



class ProactiveTest(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.REBOOKS.clear()

    def rows(self, facility):
        offer = (f'offer offer_id=o1 slot_id=s1 doctor_id=d-h1 facility={facility} procedure=VIS-FIS '
                 'agenda_date=2026-11-09T10:30:00.000Z agenda_day=mon')
        return [msg('citizen:1', 'Good morning, I need an English-speaking doctor. need.request prescription_number=rx1 lang=en', mid='a'),
                msg(city_id(), offer, 'citizen:1', mid='b'),
                msg('citizen:1', 'confirm offer_id=o1 consent_ref=c1', mid='c'),
                msg(city_id(), 'confirm offer_id=o1 slot_id=s1', facility, mid='d')]

    def test_receipt_has_details_and_hospital_gets_an_ack(self):
        hospital = 'agent:nanpr5mgm2py'
        rows = self.rows(hospital)
        receipt = msg(hospital, 'booking.receipt booking_id=b1 ledger_ref=r1', city_id(), 'r')
        city = FakeCity(rows + [receipt])
        asyncio.run(booking.handle(city, receipt))
        to_citizen = [t for to, t in city.out if to == 'citizen:1']
        self.assertTrue(to_citizen[0].startswith('booking.receipt booking_id=b1 ledger_ref=r1 '))
        self.assertIn('Monday 9 November at 10:30', to_citizen[0])
        self.assertIn((hospital, 'ack booking_id=b1 Ricevuta inoltrata al cittadino, grazie.'), city.out)

    def test_specialist_never_gets_an_ack(self):
        spec = 'agent:pky3vqyqbepa'
        receipt = msg(spec, 'booking.receipt booking_id=b1 ledger_ref=r1', city_id(), 'r')
        city = FakeCity(self.rows(spec) + [receipt])
        asyncio.run(booking.handle(city, receipt))
        self.assertFalse(any(to == spec for to, t in city.out))

    def test_thanks_after_booking_gets_a_recap_not_a_data_request(self):
        rows = self.rows('agent:nanpr5mgm2py') + [
            msg(city_id(), 'booking.receipt booking_id=b1 ledger_ref=r1 ok', 'citizen:1', mid='e')]
        thanks = msg('citizen:1', 'Thank you so much!', mid='f')
        city = FakeCity(rows + [thanks])
        asyncio.run(booking.handle(city, thanks))
        self.assertEqual(len(city.out), 1)
        self.assertIn('b1 is confirmed', city.out[0][1])
        self.assertNotIn('data.request', city.out[0][1])

    def test_offer_note_names_the_needs_kept(self):
        note = booking.offer_note({'lang': 'en', 'gender': 'f'}, False, True)
        self.assertIn('speaks English', note)
        self.assertIn('female doctor', note)
        self.assertIn('accessibility', note)
        self.assertIn('medico che parla inglese', booking.offer_note({'lang': 'en'}, False, False))



class Level3Test(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.REBOOKS.clear()
        booking.CHOSEN.clear()

    def test_bare_case_code_and_guess_from_need_id(self):
        rows = [msg('citizen:1', 'la mia ricetta è la N-2026-11-02-890910000081, grazie')]
        self.assertEqual(booking.rx_from_text(rows, 'citizen:1', rows[0].text), 'prescription_number-N-2026-11-02-890910000081')
        self.assertEqual(booking.rx_guess('N-2026-11-02-890910000081'), 'prescription_number-N-2026-11-02-890910000081')

    def items(self):
        return [dict(offer_id='a', slot_id='s1', agenda_date='2026-11-09T10:30:00.000Z', agenda_day='mon'),
                dict(offer_id='b', slot_id='s2', agenda_date='2026-11-10T12:00:00.000Z', agenda_day='tue'),
                dict(offer_id='c', slot_id='s3', agenda_date='2026-11-10T14:30:00.000Z', agenda_day='tue')]

    def test_choice_in_words(self):
        items = self.items()
        self.assertEqual(booking.match_choice('Va bene la seconda, grazie', items)['offer_id'], 'b')
        self.assertEqual(booking.match_choice('martedì alle 14:30 è perfetto', items)['offer_id'], 'c')
        self.assertEqual(booking.match_choice('I will take Monday', items)['offer_id'], 'a')
        self.assertIsNone(booking.match_choice('martedì va bene', items))
        self.assertIsNone(booking.match_choice('non posso lunedì', items))
        self.assertIsNone(booking.match_choice('prima possibile per favore', items))

    def test_first_message_in_words_books_without_asking_the_number(self):
        async def run():
            class EchoLedger(JourneyCity):
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('prescription '):
                        number = booking.fields(text)['prescription_number']
                        self.rows[-1] = msg('city:ledger', f'prescription prescription_number={number} state=issued procedure=VIS-FIS priority=B',
                                            self.agent_id, f'rx-{len(self.rows)}')
            city = EchoLedger()
            await city.receive('citizen:1', 'Buongiorno, ho mal di schiena e vorrei una visita il prima possibile.')
            self.assertTrue(any(to == 'city:ledger' and 'prescription_number=prescription_number-CASE-1' in t for to, t in city.out))
            self.assertTrue(any(to == 'citizen:1' and t.startswith('offer ') for to, t in city.out))
            self.assertFalse(any('data.request' in t for to, t in city.out))
            await city.receive('citizen:1', 'Perfetto, prendo la prima.')
            self.assertTrue(any(to == 'citizen:1' and t.startswith('consent purpose=booking') for to, t in city.out))
            await city.receive('citizen:1', 'consent purpose=booking consent_ref=c9')
            self.assertTrue(any(to == 'agent:team/h' and t.startswith('confirm ') and 'consent_ref=c9' in t for to, t in city.out))
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()


class RingFixTest(unittest.TestCase):
    def test_ring_prioritizes_the_rare_language_and_filters_ineligible_agents(self):
        class SearchingCity(FakeCity):
            async def find(self, query):
                return [Peer(GIAMBELLINO_HOSPITAL, role='hospital', capabilities=('hospital.physiatry',)),
                        Peer('agent:xxxxxxxxxxxx', role='hospital', capabilities=('hospital.physiatry',)),
                        Peer(BICOCCA_HOSPITAL, role='hospital', capabilities=('specialist_care.cardiology',)),
                        Peer(PRECOTTO_SPECIALIST, role='hospital', capabilities=('hospital.physiatry',)),
                        Peer(PRECOTTO_HOSPITAL, role='hospital', capabilities=('hospital.physiatry',))]

        rows = [msg('citizen:1', 'need.request prescription_number=rx1 lang=ro gender=f days=tue'),
                msg(city_id(), 'prescription prescription_number=rx1', 'city:ledger'),
                msg('city:ledger', 'agenda agenda_now=2026-11-20T10:00:00+00:00'),
                msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B')]
        city = SearchingCity(rows)
        asyncio.run(booking.find_partners(city, 'case-1', 'citizen:1', 'VIS-FIS', 'B', rows))
        requests = [(to, booking.fields(text)) for to, text in city.out if text.startswith('need.request ')]
        self.assertEqual([to for to, _ in requests], [PRECOTTO_HOSPITAL, GIAMBELLINO_HOSPITAL, 'agent:xxxxxxxxxxxx'])
        for _, criteria in requests:
            self.assertEqual(criteria['v'], '1.2')
            self.assertEqual(criteria['lang'], 'ro')
            self.assertEqual(criteria['gender_preference'], 'f')
            self.assertEqual(criteria['days'], 'tue')
            self.assertEqual(criteria['deadline'], '2026-11-24T10:00:00')
            self.assertNotIn('prescription_number', criteria)
            self.assertNotIn('citizen', criteria)

    def test_ring_contacts_peers_the_index_names_by_family(self):
        class SearchingCity(FakeCity):
            async def find(self, query):
                return [Peer(PRECOTTO_HOSPITAL, role='hospital', capabilities=('hospital.physiatry',)),
                        Peer(PRECOTTO_SPECIALIST, role='specialist_care', capabilities=('specialist_care.cardiology',)),
                        Peer(PRECOTTO_GP, role='gp', capabilities=('gp',))]

        for procedure, expected in [('VIS-FIS', PRECOTTO_HOSPITAL), ('VIS-CARD', PRECOTTO_SPECIALIST)]:
            with self.subTest(procedure=procedure):
                city = SearchingCity([])
                asyncio.run(booking.find_partners(city, 'case-1', 'citizen:1', procedure, 'B', []))
                targets = [to for to, text in city.out if text.startswith('need.request')]
                self.assertEqual(targets, [expected])
                self.assertFalse(any('no suitable availability' in text.lower() for _, text in city.out))

    def test_deadline_refusal_caps_later_slots_and_is_never_reoffered(self):
        me = 'agent:team/booking'
        sent = msg(me, 'offer offer_id=o2 slot_id=S2 at=2026-11-17T10:30:00+00:00\n'
                       'offer offer_id=o3 slot_id=S3 at=2026-11-18T10:30:00+00:00', 'citizen:1', mid='s')
        refuse = msg('citizen:1', 'No, grazie. cancel offer_id=o2 reason=deadline', me, mid='r')
        history = [sent, refuse]
        refused, cutoff = booking.bc_flow.citizen_refusals(history, 'citizen:1', booking.offers(history, me, 'citizen:1'))
        self.assertEqual(refused, {'S2'})
        self.assertTrue(booking.bc_flow.before_cutoff({'at': '2026-11-16T12:00:00+00:00'}, cutoff))
        self.assertFalse(booking.bc_flow.before_cutoff({'at': '2026-11-18T10:30:00+00:00'}, cutoff))

    def test_english_receipt_still_reads_as_a_receipt(self):
        from city_sdk.city import understood
        text = booking.bc_common.clean_prose('booking.receipt booking_id=B1 ' + booking.receipt_note({'procedure': 'VIS-FIS'}, True))
        self.assertEqual(understood(msg('agent:x', text)).type, 'booking.receipt')
        offers = booking.bc_common.clean_prose('offer offer_id=a slot_id=s1\noffer offer_id=b slot_id=s2\nThese fit your prescription.')
        self.assertEqual(offers.splitlines()[1], 'offer offer_id=b slot_id=s2')


class CitizenCancelTest(unittest.TestCase):
    def test_past_cancel_does_not_restart_search_when_another_slot_is_confirmed(self):
        me = city_id()
        citizen = 'citizen:1'
        stamp = '2026-11-21T02:39:00.000Z'
        cancel = Message(type='chat', sender=citizen, recipient=me, msg_id='cancel', need_id='case-1',
                         sent_at=stamp, body={'text': 'cancel offer_id=o1 reason=past'})
        confirm = Message(type='chat', sender=citizen, recipient=me, msg_id='confirm', need_id='case-1',
                          sent_at=stamp, body={'text': 'confirm offer_id=o2 consent_ref=ok'})
        rows = [msg(citizen, 'need.request prescription_number=rx1', me),
                msg(me, 'offer offer_id=o1 slot_id=s1 facility=agent:t/h\n'
                        'offer offer_id=o2 slot_id=s2 facility=agent:t/h prescription_number=rx1', citizen),
                cancel, confirm]
        city = FakeCity(rows)
        city.inbound = cancel
        asyncio.run(booking.main(SimpleNamespace(is_emergency=False), city))
        self.assertEqual(city.out, [])
        asyncio.run(booking.handle(city, confirm))
        self.assertIn(('agent:t/h', 'confirm offer_id=o2 slot_id=s2 prescription_number=rx1 '
                       'citizen=citizen:1 consent_ref=ok'), city.out)

    def test_free_text_can_choose_an_offer_via_the_model(self):
        class ModelCity(FakeCity):
            async def llm(self, messages, need_id=None):
                return {'content': '{"intent":"choose","option":2}'}

        me = city_id()
        citizen = 'citizen:1'
        incoming = msg(citizen, 'I can make it after lunch, go ahead with that one.', me, mid='choice')
        city = ModelCity([msg(citizen, 'need.request prescription_number=rx1', me, mid='request'),
                          msg(me, 'offer offer_id=o1 slot_id=s1 facility=agent:t/h\n'
                                  'offer offer_id=o2 slot_id=s2 facility=agent:t/h', citizen, mid='offers'),
                          incoming])
        city.inbound = incoming
        asyncio.run(booking.handle(city, incoming))
        self.assertTrue(any(to == citizen and text.startswith('consent purpose=booking offer_id=o2 ')
                            for to, text in city.out))
        self.assertFalse(any(to == 'agent:t/h' for to, _ in city.out))

    def test_a_refusal_is_never_read_as_a_choice(self):
        me = 'agent:team/booking'
        rows = [msg(me, 'offer offer_id=o2 slot_id=S2 at=2026-11-17T10:30:00+00:00', 'citizen:1', mid='s'),
                msg('citizen:1', 'Questa data non va bene. cancel offer_id=o2 reason=deadline', me, mid='r')]
        city = FakeCity(rows)
        city.inbound = rows[-1]
        booked = []
        orig = booking.book_choice
        async def fake_book(*a, **k): booked.append(a)
        booking.book_choice = fake_book
        try:
            asyncio.run(booking.follow_up(city, 'case-1', rows, 'citizen:1'))
        finally:
            booking.book_choice = orig
        self.assertEqual(booked, [])


class NoticeTest(unittest.TestCase):
    def test_gp_handoff_never_reaches_the_citizen(self):
        rows = [msg('citizen:1', 'Ho mal di schiena', 'agent:c2ewegr2ea3v', mid='a'),
                msg('agent:c2ewegr2ea3v', 'handoff prescription_number=prescription_number-N-1 procedure=VIS-FIS priority=B Ricetta emessa.', mid='h')]
        city = FakeCity(rows)
        city.inbound = rows[-1]
        asyncio.run(booking.main(SimpleNamespace(is_emergency=False), city))
        self.assertFalse(any(to.startswith('citizen:') for to, _ in city.out))
        self.assertEqual([text.split()[0] for to, text in city.out if to == 'agent:c2ewegr2ea3v'], ['ack'])
        self.assertFalse(any(text.split()[0] != 'ack' or len(text.split('=')) > 2 for _, text in city.out))


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        booking.bc_flow.CLAIMED.clear(); booking.bc_flow.OFFERED.clear()

    def test_slot_confirmed_elsewhere_is_dropped_and_offered_elsewhere_goes_last(self):
        flow = booking.bc_flow
        flow.CLAIMED['S1'] = ('other-need', flow._clock.monotonic())
        flow.OFFERED['S2'] = ('other-need', flow._clock.monotonic())
        out = flow.spread([{'slot_id': 'S1'}, {'slot_id': 'S2'}, {'slot_id': 'S3'}], 'my-need')
        self.assertEqual([c['slot_id'] for c in out], ['S3', 'S2'])
        self.assertEqual(len(flow.spread([{'slot_id': 'S1'}], 'other-need')), 1)

    def test_no_second_booking_after_a_receipt(self):
        me = 'agent:team/booking'
        rows = [msg('citizen:1', 'need.request prescription_number=prescription_number-N-1', me, mid='a'),
                msg(me, 'offer offer_id=o1 slot_id=S1\\noffer offer_id=o2 slot_id=S2', 'citizen:1', mid='o'),
                msg(me, 'booking.receipt booking_id=B1 ledger_ref=L1', 'citizen:1', mid='r'),
                msg('citizen:1', 'confirm offer_id=o2 consent_ref=C2', me, mid='c2')]
        city = FakeCity(rows); city.inbound = rows[-1]
        asyncio.run(booking.handle(city, rows[-1]))
        self.assertFalse(any(text.startswith('confirm') or text.startswith('consent') for _, text in city.out))


class LanguageFallbackTest(unittest.TestCase):
    def setUp(self):
        booking.bc_flow.FALLEN_BACK.clear()

    def test_ring_empty_reruns_local_search_without_the_language(self):
        calls = []
        orig = booking.bc_flow.search
        async def fake_search(city, need_id, history, citizen, number, **kw): calls.append(kw)
        async def prefs(city, history, citizen): return {'lang': 'en'}
        orig_prefs = booking.bc_flow.citizen_preferences
        booking.bc_flow.search, booking.bc_flow.citizen_preferences = fake_search, prefs
        try:
            rows = [msg('citizen:1', 'need.request lang=en prescription_number=prescription_number-N-1', mid='a'),
                    msg('city:ledger', 'prescription prescription_number=prescription_number-N-1 state=issued', mid='l')]
            city = FakeCity(rows)
            asyncio.run(booking.bc_flow.language_fallback(city, 'case-1', rows, 'citizen:1'))
            asyncio.run(booking.bc_flow.language_fallback(city, 'case-1', rows, 'citizen:1'))
        finally:
            booking.bc_flow.search, booking.bc_flow.citizen_preferences = orig, orig_prefs
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['relax_lang'])
        self.assertIn('English', calls[0]['lead'])

    def test_without_a_language_the_answer_is_helpful_not_passive(self):
        async def prefs(city, history, citizen): return {}
        orig_prefs = booking.bc_flow.citizen_preferences
        booking.bc_flow.citizen_preferences = prefs
        try:
            city = FakeCity([])
            asyncio.run(booking.bc_flow.language_fallback(city, 'case-2', [], 'citizen:1'))
        finally:
            booking.bc_flow.citizen_preferences = orig_prefs
        self.assertIn('altri giorni o orari', city.out[0][1])


class RingStallTest(unittest.TestCase):
    def setUp(self):
        booking.bc_flow.FALLEN_BACK.clear()

    def rows(self, offer_line):
        me = 'agent:team/booking'
        return [msg('citizen:1', 'I need a doctor who speaks English need.request lang=en prescription_number=rx1', me, mid='c'),
                msg('city:ledger', 'agenda agenda_now=2026-11-20T00:00:00+00:00', mid='ag'),
                msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B', mid='rx'),
                msg(me, 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q lang=en', BICOCCA_HOSPITAL, mid='q'),
                msg(BICOCCA_HOSPITAL, offer_line, me, mid='o')]

    def test_partner_offer_without_language_is_not_trusted(self):
        rows = self.rows(f'offer offer_id=x slot_id=s1 doctor_id=asst-bicocca-h2 facility={BICOCCA_HOSPITAL} '
                         'procedure=VIS-FIS agenda_date=2026-11-20T12:00:00+00:00 request_id=q')
        city = FakeCity(rows)
        orig = booking.bc_flow.language_fallback
        called = []
        async def fake(*a, **k): called.append(1)
        booking.bc_flow.language_fallback = fake
        try:
            asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        finally:
            booking.bc_flow.language_fallback = orig
        self.assertFalse(any('offer offer_id=' in text for _, text in city.out))
        self.assertEqual(called, [1])

    def test_partner_offer_in_the_language_comes_with_a_sentence(self):
        rows = self.rows(f'offer offer_id=x slot_id=s1 doctor_id=asst-bicocca-h1 facility={BICOCCA_HOSPITAL} '
                         'procedure=VIS-FIS agenda_date=2026-11-20T12:00:00+00:00 doctor_lang=it/en request_id=q')
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        text = city.out[0][1]
        self.assertIn('offer offer_id=', text)
        self.assertIn('partner facility', text)
        self.assertIn('Bicocca', text)

    def test_standard_offer_accepts_local_agenda_times_and_filters_urgent_reserve(self):
        base = (f'doctor_id=asst-bicocca-h1 facility={BICOCCA_HOSPITAL} procedure=VIS-FIS '
                'doctor_lang=it/en doctor_gender=f request_id=q')
        rows = self.rows(f'offer v=1.2 offer_id=regular slot_id=s1 agenda_date=2026-11-20T12:00:00 {base}\n'
                         f'offer v=1.2 offer_id=reserved slot_id=s2 agenda_date=2026-11-20T13:00:00 reserve=U {base}')
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
        self.assertIn('offer_id=regular', offer)
        self.assertNotIn('offer_id=reserved', offer)
        for attribute in ('doctor_lang=', 'doctor_gender=', 'languages=', 'gender=', 'reserve='):
            self.assertNotIn(attribute, offer)

    def test_overlapping_partner_offer_ids_do_not_make_a_choice_ambiguous(self):
        rows = self.rows(f'offer offer_id=shared slot_id=s1 facility={BICOCCA_HOSPITAL} '
                         'doctor_id=asst-bicocca-h1 procedure=VIS-FIS request_id=q '
                         'doctor_lang=it/en agenda_date=2026-11-20T12:00:00')
        rows.extend([msg(city_id(), 'need.request procedure=VIS-FIS priority=B request_id=q', PRECOTTO_HOSPITAL),
                     msg(PRECOTTO_HOSPITAL, f'offer offer_id=shared slot_id=s2 facility={PRECOTTO_HOSPITAL} '
                         'doctor_id=asst-precotto-h1 procedure=VIS-FIS request_id=q '
                         'doctor_lang=it/en agenda_date=2026-11-20T12:00:00')])
        city = FakeCity(rows)
        asyncio.run(booking.remote_offers(city, 'case-1', rows, 'citizen:1'))
        offer = next(text for to, text in city.out if to == 'citizen:1' and text.startswith('offer '))
        self.assertEqual(offer.count('offer_id=shared'), 1)
        self.assertIn(f'facility={BICOCCA_HOSPITAL}', offer)


class CrossAsstConsentTest(unittest.TestCase):
    def setUp(self):
        booking.bc_flow.FALLEN_BACK.clear()
        booking.RELAYED.clear()

    def test_data_response_with_consent_ref_unlocks_remote_offers(self):
        me = 'agent:team/booking'
        rows = [
            msg('citizen:1', 'I need an English doctor. need.request lang=en prescription_number=rx1', me, mid='a'),
            msg('city:ledger', 'agenda agenda_now=2026-11-20T00:00:00+00:00', me, mid='ag'),
            msg('city:ledger', 'prescription prescription_number=rx1 state=issued procedure=VIS-FIS priority=B', me, mid='rx'),
            msg(me, 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q lang=en', BICOCCA_HOSPITAL, mid='q'),
            msg(BICOCCA_HOSPITAL, f'offer offer_id=p1 slot_id=s1 doctor_id=asst-bicocca-h1 facility={BICOCCA_HOSPITAL} '
                'procedure=VIS-FIS agenda_date=2026-11-20T12:00:00+00:00 doctor_lang=it/en request_id=q', me, mid='o'),
            msg(me, 'consent purpose=cross_asst May I share?', 'citizen:1', mid='dr'),
            msg('citizen:1', 'data.response consent_ref=cx-99', me, mid='resp'),
        ]
        city = FakeCity(rows)
        asyncio.run(booking.handle(city, rows[-1]))
        self.assertTrue(any(to == 'citizen:1' and 'offer offer_id=p1' in t for to, t in city.out))
        self.assertEqual(booking.cross_consent(rows, me, 'citizen:1'), 'cx-99')

    def test_missing_consent_ref_or_refusal_triggers_local_fallback_never_hangs(self):
        me = 'agent:team/booking'
        for reply_text in ('data.response', 'No, non voglio condividere i miei dati.', 'consent granted=false'):
            booking.bc_flow.FALLEN_BACK.clear()
            rows = [
                msg('citizen:1', 'need.request lang=en prescription_number=rx1', me, mid='a'),
                msg(me, 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=q lang=en', BICOCCA_HOSPITAL, mid='q'),
                msg(me, 'data.request purpose=cross_asst fields=consent_ref May I share?', 'citizen:1', mid='dr'),
                msg('citizen:1', reply_text, me, mid='resp'),
            ]
            city = FakeCity(rows)
            called = []
            orig = booking.bc_flow.language_fallback
            async def fake_fb(*a, **k): called.append(1)
            booking.bc_flow.language_fallback = fake_fb
            booking.language_fallback = fake_fb
            try:
                asyncio.run(booking.handle(city, rows[-1]))
            finally:
                booking.bc_flow.language_fallback = orig
                booking.language_fallback = orig
            self.assertEqual(called, [1], f'Failed to fall back for reply: {reply_text}')
            self.assertFalse(any(to == BICOCCA_HOSPITAL for to, _ in city.out))

    def test_consent_question_retries_without_sharing(self):
        me = city_id()
        incoming = msg('citizen:1', 'Which consent_ref?', me, mid='question')
        rows = [msg('citizen:1', 'need.request prescription_number=rx1', me),
                msg(me, 'consent purpose=cross_asst May I share?', 'citizen:1'), incoming]
        city = FakeCity(rows)
        asyncio.run(booking.handle(city, incoming))
        self.assertEqual([to for to, _ in city.out], ['citizen:1'])
        self.assertTrue(city.out[0][1].startswith('data.request purpose=cross_asst fields=consent_ref '))
        self.assertEqual(booking.cross_consent(rows, me, 'citizen:1'), '')

    def test_nested_consent_reference_is_scoped_to_the_pending_request(self):
        me = city_id()
        reply = Message(type='data.response', sender='citizen:1', recipient=me, msg_id='reply',
                        body={'provided': {'consent_ref': 'actual-ref'}})
        cross = msg(me, 'data.request purpose=cross_asst fields=consent_ref', 'citizen:1')
        local = msg(me, 'consent purpose=booking', 'citizen:1')
        self.assertEqual(booking.cross_consent([cross, reply], me, 'citizen:1'), 'actual-ref')
        self.assertEqual(booking.cross_consent([cross, local, reply], me, 'citizen:1'), '')


class ReadWorldTest(unittest.TestCase):
    def test_retries_only_missing_pages_and_deduplicates_slots(self):
        orig_wait = booking.bc_flow.LEDGER_WAIT
        booking.bc_flow.LEDGER_WAIT = 0.05
        try:
            class FlakyWorldCity(FakeCity):
                def __init__(self):
                    super().__init__([])
                    self.page_calls = []
                async def say(self, to, text):
                    self.out.append((to, text))
                    if to == 'city:ledger' and text.startswith('world '):
                        page = booking.fields(text).get('page', '1')
                        self.page_calls.append(page)
                        if page == '1':
                            self.rows.append(msg('city:ledger', 'slots page=1/3\nslot_id=s1 doctor_id=d-h1', self.agent_id, f'p1-{len(self.page_calls)}'))
                        elif page == '2':
                            self.rows.append(msg('city:ledger', 'slots page=2/3\nslot_id=s2 doctor_id=d-h1', self.agent_id, f'p2-{len(self.page_calls)}'))
                        elif page == '3' and self.page_calls.count('3') >= 2:
                            self.rows.append(msg('city:ledger', 'slots page=3/3\nslot_id=s3 doctor_id=d-h1\nslot_id=s1 doctor_id=d-h1', self.agent_id, f'p3-{len(self.page_calls)}'))
            city = FlakyWorldCity()
            items, _, _, ok = asyncio.run(booking.bc_flow.read_world(city, 'case-1', [], 'VIS-FIS'))
            self.assertTrue(ok)
            self.assertEqual(sorted(i['slot_id'] for i in items), ['s1', 's2', 's3'])
            self.assertEqual(city.page_calls.count('1'), 1)
            self.assertEqual(city.page_calls.count('2'), 1)
            self.assertEqual(city.page_calls.count('3'), 2)
        finally:
            booking.bc_flow.LEDGER_WAIT = orig_wait


class VagueDaysTest(unittest.TestCase):
    def setUp(self):
        booking.RELAYED.clear()
        booking.bc_prefs._PREFS_CACHE.clear()

    def test_vague_days_asks_once_then_proceeds_with_answer_or_refusal(self):
        async def run():
            class DaysCity(JourneyCity):
                async def say(self, to, text):
                    await super().say(to, text)
                    if to == 'city:ledger' and text.startswith('world '):
                        self.rows[-1] = msg('city:ledger', 'slots page=1/1\n'
                            'slot_id=s1 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T10:30:00+00:00 day=mon free=1\n'
                            'slot_id=s2 doctor_id=doctor-h1 facility=agent:team/h procedure=VIS-FIS at=2026-11-21T12:00:00+00:00 day=wed free=1',
                            self.agent_id, f'w-{len(self.rows)}')
            city = DaysCity()
            await city.receive('citizen:1', 'Posso solo in certi giorni. need.request prescription_number=rx1')
            self.assertTrue(any(to == 'citizen:1' and t.startswith('data.request purpose=booking fields=days') for to, t in city.out))
            self.assertFalse(any(to == 'citizen:1' and t.startswith('offer ') for to, t in city.out))
            await city.receive('citizen:1', 'data.response days=wed')
            offers_out = [t for to, t in city.out if to == 'citizen:1' and t.startswith('offer ')]
            self.assertEqual(len(offers_out), 1)
            self.assertIn('slot_id=s2', offers_out[0])
            self.assertNotIn('slot_id=s1', offers_out[0])
        asyncio.run(run())

    def test_vague_days_refusal_proceeds_without_looping(self):
        async def run():
            city = JourneyCity()
            await city.receive('citizen:1', 'I can only come on certain days. need.request prescription_number=rx1 lang=en')
            self.assertTrue(any(to == 'citizen:1' and t.startswith('data.request purpose=booking fields=days') for to, t in city.out))
            await city.receive('citizen:1', 'Actually any day is fine.')
            self.assertTrue(any(to == 'citizen:1' and t.startswith('offer ') for to, t in city.out))
            self.assertEqual(sum(1 for to, t in city.out if to == 'citizen:1' and t.startswith('data.request purpose=booking fields=days')), 1)
        asyncio.run(run())


class LanguageClarificationTest(unittest.TestCase):
    def test_language_conflict_pauses_search_until_citizen_answers(self):
        class LanguageCity(JourneyCity):
            async def llm(self, messages, need_id=None):
                if 'English, please.' in messages[-1]['content']:
                    return {'content': '{"lang": "en", "requested_lang": "en", "confirmed_lang": "en"}'}
                return {'content': '{"lang": "en", "requested_lang": "en"}'}

        async def run(typed):
            city = LanguageCity()
            citizen = 'citizen:1'
            await city.receive(citizen, 'I would like a doctor who speaks English. '
                               'need.request prescription_number=rx1 lang=am')
            question = [text for to, text in city.out if to == citizen and 'fields=lang' in text]
            self.assertEqual(len(question), 1)
            self.assertIn('English or Amharic', question[0])
            self.assertFalse(any(text.startswith('world ') for _, text in city.out))
            if typed:
                city.inbound = Message(type='data.response', sender=citizen, recipient=city.agent_id,
                                       msg_id='language-reply', need_id='case-1', body={'provided': {'lang': 'en'}})
                city.rows.append(city.inbound)
                await booking.main(SimpleNamespace(), city)
            else:
                await city.receive(citizen, 'English, please.')
            self.assertTrue(any(to == citizen and text.startswith('offer ') for to, text in city.out))
            self.assertEqual(sum('fields=lang' in text for _, text in city.out), 1)

        for typed in (False, True):
            with self.subTest(typed=typed):
                asyncio.run(run(typed))

    def test_conversation_language_does_not_conflict_with_visit_language(self):
        class EnglishTextCity(FakeCity):
            async def llm(self, messages, need_id=None):
                return {'content': '{"lang": "en"}'}

        rows = [msg('citizen:2', 'Can you find an appointment? need.request lang=am')]
        result = asyncio.run(booking.citizen_preferences(EnglishTextCity(rows), rows, 'citizen:2'))
        self.assertEqual(result, {'lang': 'am'})


class DedupTest(unittest.TestCase):
    def test_same_text_to_same_citizen_is_sent_once(self):
        common = booking.bc_common
        common._RECENT_SENDS.clear()
        old, common.DEDUP_SECONDS = common.DEDUP_SECONDS, 120.0
        try:
            city = FakeCity([])
            for _ in range(4):
                asyncio.run(common.send(city, 'citizen:9', 'cancel offer_id=o1 reason=facility_cancelled'))
            asyncio.run(common.send(city, 'citizen:8', 'cancel offer_id=o1 reason=facility_cancelled'))
            self.assertEqual([to for to, _ in city.out], ['citizen:9', 'citizen:8'])
        finally:
            common.DEDUP_SECONDS = old
            common._RECENT_SENDS.clear()

    def test_rotation_differs_between_cases_and_is_stable(self):
        items = [{'slot_id': f's{i}', 'at': f'2026-11-24T{10 + i:02d}:00:00'} for i in range(10)]
        a = booking.bc_flow.rotate_for_case(items, 'N-1')
        self.assertEqual(a, booking.bc_flow.rotate_for_case(items, 'N-1'))
        self.assertEqual(len(a), 6)
        starts = {booking.bc_flow.rotate_for_case(items, f'N-{i}')[0]['slot_id'] for i in range(20)}
        self.assertGreater(len(starts), 3)
