import asyncio
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from city_sdk.city import Message, Need

spec = importlib.util.spec_from_file_location('hospital_brain', Path(__file__).parents[1] / 'asst/hospital/agent/agent.py')
hospital = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hospital)
hospital._PULL_TRIES = 3
hospital._PULL_PAUSE = 0


def msg(sender, text, recipient='agent:team/hospital', mid='m1'):
    return Message(type='chat', sender=sender, recipient=recipient, msg_id=mid,
                   need_id='N-case', body={'text': text})


class City:
    agent_id = 'agent:team/hospital'
    address = ''
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.out = []
        self.inbound = None
    async def history(self, **kwargs): return list(self.rows)
    async def find(self, query):
        return [SimpleNamespace(agent_id='agent:team/booking', role='booking_centre')]
    async def say(self, to, text):
        self.out.append((to, text))
        self.rows.append(msg(self.agent_id, text, to, f'out-{len(self.rows)}'))
        if to == hospital.LEDGER and text.startswith('hold '):
            self.rows.append(msg(hospital.LEDGER, 'held hold_id=H1 slot_id=S1 fence=1', mid='held1'))
        elif to == hospital.LEDGER and text.startswith('commit '):
            self.rows.append(msg(hospital.LEDGER, 'committed hold_id=H1 booking_id=B1 ledger_ref=L1 slot_id=S1', mid='committed1'))


def slots(facility='agent:team/hospital'):
    return msg(hospital.LEDGER, f'slots page=1/1\nslot_id=S1 facility={facility} doctor_id=D1 procedure=VIS-FIS at=2026-11-23T12:00:00+00:00', mid='world1')


def confirm(sender='agent:team/booking'):
    return msg(sender, 'confirm offer_id=O1 slot_id=S1 citizen=citizen:1 prescription_number=RX1 consent_ref=C1', mid='confirm1')


class HospitalTest(unittest.TestCase):
    def setUp(self):
        hospital.SLOT_SEEN.clear()
    def run_confirm(self, city, incoming):
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._on_confirm(city, incoming, list(city.rows), hold_self=False))
    def test_hold_commit_and_receipt_with_distinct_ids(self):
        incoming = confirm(); city = City([incoming, slots()])
        self.run_confirm(city, incoming)
        self.assertTrue(any(to == hospital.LEDGER and text.startswith('hold slot_id=S1') for to, text in city.out))
        self.assertTrue(any(to == hospital.LEDGER and 'commit hold_id=H1 consent_ref=C1' in text for to, text in city.out))
        self.assertTrue(any(to == 'agent:team/booking' and 'booking.receipt booking_id=B1 ledger_ref=L1' in text for to, text in city.out))
    def test_no_commit_without_our_hold_or_real_consent(self):
        incoming = msg(hospital.LEDGER, 'held hold_id=H1 slot_id=S1 fence=1 need_id=N-123', mid='held')
        city = City([incoming])
        asyncio.run(hospital._on_held(city, incoming, city.rows))
        self.assertEqual(city.out, [])
    def test_cross_asst_hold_and_booking_consent_remain_separate(self):
        incoming = msg('agent:team/booking', 'confirm offer_id=O1 slot_id=S1 citizen=citizen:1 prescription_number=RX1 consent_ref=BOOK cross_asst_consent_ref=CROSS')
        city = City([incoming, slots()])
        self.run_confirm(city, incoming)
        self.assertTrue(any(text.startswith('hold ') and 'consent_ref=CROSS' in text for to, text in city.out if to == hospital.LEDGER))
        self.assertTrue(any(text.startswith('commit ') and 'consent_ref=BOOK' in text for to, text in city.out if to == hospital.LEDGER))
    def test_duplicate_confirm_does_not_send_another_hold(self):
        incoming = confirm(); city = City([incoming, slots()])
        self.run_confirm(city, incoming)
        self.run_confirm(city, incoming)
        self.assertEqual(sum(text.startswith('hold ') for to, text in city.out if to == hospital.LEDGER), 1)
    def test_rejects_unknown_sender(self):
        incoming = confirm('agent:evil/other'); city = City([incoming, slots()])
        self.run_confirm(city, incoming)
        self.assertFalse(any(text.startswith('hold ') for _, text in city.out))
    def test_rejects_another_hospitals_slot(self):
        incoming = confirm(); city = City([incoming, slots('agent:other/hospital')])
        self.run_confirm(city, incoming)
        self.assertFalse(any(text.startswith('hold ') for _, text in city.out))
    def test_silent_world_read_still_holds_instead_of_cancelling(self):
        """09:32 CET: every confirm came back 'facility_cancelled' because the world page timed out.
        A world read that never answered is not 'slot unavailable': the Ledger hold decides."""
        incoming = confirm(); city = City([incoming])
        self.run_confirm(city, incoming)
        self.assertFalse(any('reason=slot_unavailable' in text for _, text in city.out), city.out)
        self.assertTrue(any(to == hospital.LEDGER and text.startswith('hold slot_id=S1') for to, text in city.out))
        self.assertTrue(any(to == 'agent:team/booking' and 'booking.receipt booking_id=B1' in text for to, text in city.out))
    def test_slow_held_still_ends_with_the_receipt(self):
        """A Ledger at 8-12 s: the held line came after the normal wait and no receipt went out."""
        class Slow(City):
            pending = None
            reads = 0
            async def history(self, **kwargs):
                self.reads += 1
                if self.pending and self.reads >= 12:
                    self.rows.append(self.pending); self.pending = None
                return list(self.rows)
            async def say(self, to, text):
                self.out.append((to, text))
                self.rows.append(msg(self.agent_id, text, to, f'out-{len(self.rows)}'))
                if to == hospital.LEDGER and text.startswith('hold '):
                    self.reads = 0
                    self.pending = msg(hospital.LEDGER, 'held hold_id=H1 slot_id=S1 fence=1', mid='held1')
                elif to == hospital.LEDGER and text.startswith('commit '):
                    self.rows.append(msg(hospital.LEDGER, 'committed hold_id=H1 booking_id=B1 ledger_ref=L1 slot_id=S1', mid='committed1'))
        incoming = confirm(); city = Slow([incoming, slots()])
        self.run_confirm(city, incoming)
        self.assertTrue(any(to == 'agent:team/booking' and 'booking.receipt booking_id=B1' in text for to, text in city.out), city.out)
    def test_slow_ledger_declines_ring_requests_without_queueing_reads(self):
        """15:30: our ASST's Ledger answered 30+ min late; each ring request added two reads."""
        hospital.hosp_ack._LEDGER_SLOW_UNTIL[0] = 0.0
        try:
            hospital.hosp_ack.mark_ledger_slow()
            incoming = msg('agent:other/bc', 'need.request procedure=VIS-FIS priority=B request_id=Q7', mid='slow1')
            city = City([incoming])
            asyncio.run(hospital._partner_request(city, incoming, list(city.rows)))
            self.assertFalse([t for to, t in city.out if to == hospital.LEDGER], city.out)
            self.assertTrue(any(to == 'agent:other/bc' and t.startswith('cancel reason=no_availability')
                                and 'request_id=Q7' in t for to, t in city.out), city.out)
        finally:
            hospital.hosp_ack._LEDGER_SLOW_UNTIL[0] = 0.0

    def test_partner_request_is_acknowledged_before_the_history_read(self):
        """city:clock +1 for a reply to another agent within 10 s: the ack waited for a history
        read that took longer than that on a loaded relay (2689 ratings, 0.07 points each)."""
        from city_sdk.city import Message, Need
        events = []
        class Slow(City):
            async def history(self, **kwargs):
                events.append('history')
                return list(self.rows)
            async def say(self, to, text):
                events.append(('say', to, text[:20]))
                await super().say(to, text)
        hospital.hosp_ack.AGENT_ACKED.clear()
        ring = Message(type='need.request', sender='agent:other/bc', recipient='agent:team/hospital',
                       msg_id='ring-ack-1', need_id='N-RA', body={'procedure': 'VIS-FIS', 'priority': 'B', 'request_id': 'r9'})
        city = Slow([ring]); city.inbound = ring
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital.main(Need(service='hospital.physiatry', summary='', message=ring), city))
        first_say = next(i for i, e in enumerate(events) if e != 'history')
        self.assertLess(first_say, events.index('history'), events)
        self.assertEqual(events[first_say][1], 'agent:other/bc')
        acks = [t for to, t in city.out if to == 'agent:other/bc' and t.startswith('Ricevuto')]
        self.assertEqual(len(acks), 1, city.out)

    def test_untargeted_typed_request_ends_without_a_relay_read(self):
        """Deploy conformance custody.ack (12:27, 12:32, 12:58): the probe need.request for
        service=lab got no reply anyway, but its one history read on a loaded relay took > 5 s,
        so the lease renew came too late. A typed request for a service we do not run ends now."""
        from city_sdk.city import Message, Need
        reads = []
        class Probe(City):
            async def history(self, **kwargs):
                reads.append(1)
                return list(self.rows)
        probe = Message(type='need.request', sender='agent:t05/osp-x', recipient='agent:team/hospital',
                        msg_id='p1', need_id='N-P', body={'service': 'lab', 'summary': 'visit', 'district': 8})
        city = Probe([probe]); city.inbound = probe
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital.main(Need(service='lab', summary='visit', district=8, message=probe), city))
        self.assertEqual(reads, [])
        ring = Message(type='need.request', sender='agent:t05/osp-x', recipient='agent:team/hospital',
                       msg_id='p2', need_id='N-R', body={'procedure': 'VIS-FIS', 'priority': 'B', 'request_id': 'r1'})
        city = Probe([ring]); city.inbound = ring
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital.main(Need(service='hospital.physiatry', summary='', message=ring), city))
        self.assertTrue(reads)
        # The body is a mappingproxy: an isinstance(dict) check read it as empty and refused the
        # typed VIS-FIS request of another booking centre with "Qui solo visite fisiatriche".
        self.assertFalse(any('Qui solo visite fisiatriche' in text for _, text in city.out), city.out)
    def test_confirm_for_a_recently_seen_own_slot_holds_without_rereading_the_world(self):
        """Journey 2920005: 12 minutes between the confirm and facility_cancelled, spent re-reading
        up to 8 world pages from a slow Ledger. Our slots were read seconds before for the ring."""
        hospital.SLOT_SEEN.clear()
        hospital._remember_slots([slots()], 'agent:team/hospital')
        incoming = confirm(); city = City([incoming])
        self.run_confirm(city, incoming)
        self.assertFalse(any(to == hospital.LEDGER and text.startswith('world ') for to, text in city.out), city.out)
        self.assertTrue(any(to == hospital.LEDGER and text.startswith('hold slot_id=S1') for to, text in city.out))
        hospital.SLOT_SEEN.clear()
        hospital._remember_slots([slots('agent:other/hospital')], 'agent:team/hospital')
        incoming = confirm(); city = City([incoming])
        self.run_confirm(city, incoming)
        self.assertFalse(any(text.startswith('hold ') for _, text in city.out), city.out)
        hospital.SLOT_SEEN.clear()
    def test_unsolicited_committed_does_not_create_receipt(self):
        incoming = msg(hospital.LEDGER, 'committed hold_id=H1 booking_id=B1 ledger_ref=L1 slot_id=S1')
        city = City([msg('citizen:1', 'hello'), incoming])
        asyncio.run(hospital._on_committed(city, incoming, city.rows))
        self.assertEqual(city.out, [])
    def test_checkin_response_is_correlated_and_contains_ref(self):
        request = msg('citizen:1', 'checkin booking_id=B1 ledger_ref=L1')
        write = msg('agent:team/hospital', 'checkin booking_id=B1 citizen=citizen:1', hospital.LEDGER)
        incoming = msg(hospital.LEDGER, 'checked_in booking_id=B1')
        city = City([request, write, incoming])
        asyncio.run(hospital._on_checked_in(city, incoming, city.rows, reports=False))
        self.assertTrue(any(to == 'citizen:1' and 'checkin booking_id=B1 ledger_ref=L1' in text for to, text in city.out))
    def test_emergency_before_clinical_refusal(self):
        incoming = msg('citizen:1', 'Ho dolore al petto, desidero una diagnosi')
        city = City([incoming]); city.inbound = incoming
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital.main(Need(), city))
        self.assertEqual(len(city.out), 1)
        self.assertIn('escalate.emergency reason=red_flag', city.out[0][1])
    def test_new_partner_request_is_not_blocked_by_an_old_answer(self):
        incoming = msg('agent:team/booking', 'need.request procedure=VIS-FIS priority=B request_id=Q2')
        agenda = msg(hospital.LEDGER, 'agenda agenda_now=2026-11-23T00:00:00+00:00', mid='agenda')
        world = msg(hospital.LEDGER, 'slots page=1/1\nslot_id=S1 facility=agent:team/hospital doctor_id=doctor-h1 procedure=VIS-FIS at=2026-11-23T12:00:00+00:00 day=mon free=1', mid='world')
        previous = msg('agent:team/hospital', 'unavailable procedure=VIS-FIS request_id=Q1', 'agent:team/booking')
        city = City([previous, incoming, agenda, world])
        asyncio.run(hospital._partner_request(city, incoming, city.rows))
        self.assertTrue(any(to == 'agent:team/booking' and 'request_id=Q2' in text for to, text in city.out))

    def test_partner_rare_language_comes_from_the_ledger(self):
        incoming = msg('agent:team/booking', 'need.request procedure=VIS-FIS priority=B lang=ka request_id=Q1')
        agenda = msg(hospital.LEDGER, 'agenda agenda_now=2026-11-23T00:00:00+00:00', mid='agenda')
        slots = ('slots page=1/1\n'
                 'slot_id=S1 facility=agent:team/hospital doctor_id=doctor-h3 procedure=VIS-FIS '
                 'at=2026-11-24T12:00:00+00:00 day=tue free=1')
        city = City([incoming, agenda, msg(hospital.LEDGER, slots)])
        asyncio.run(hospital._partner_request(city, incoming, city.rows))
        self.assertEqual(city.out[-1], ('agent:team/booking', 'cancel mach=1.3 reason=no_availability request_id=Q1'))
        city = City([incoming, agenda, msg(hospital.LEDGER, slots + '\ndoctor_id=doctor-h3 languages=it/ka')])
        asyncio.run(hospital._partner_request(city, incoming, city.rows))
        replies = [text for recipient, text in city.out if recipient == incoming.sender]
        self.assertEqual(len(replies), 1)
        self.assertIn('doctor_lang=it/ka', replies[0])

if __name__ == '__main__': unittest.main()


class HospitalProactiveTest(unittest.TestCase):
    def test_our_booking_centre_is_trusted_when_the_index_hides_booking_centres(self):
        class HiddenIndex(City):
            async def find(self, query):
                return []
        incoming = confirm('agent:gdrf9b9kxxss')
        city = HiddenIndex([incoming, slots()])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._on_confirm(city, incoming, list(city.rows), hold_self=False))
        self.assertTrue(any(to == hospital.LEDGER and text.startswith('hold slot_id=S1') for to, text in city.out))

    def test_unknown_agent_is_refused_when_the_index_lists_booking_centres(self):
        self.assertFalse(asyncio.run(hospital._trusted_booking_centre(City(), 'agent:impostor/bc')))
        self.assertTrue(asyncio.run(hospital._trusted_booking_centre(City(), 'agent:team/booking')))

    def test_check_in_in_words_uses_the_booking_of_the_case(self):
        class CheckinCity(City):
            async def say(self, to, text):
                await super().say(to, text)
                if to == hospital.LEDGER and text.startswith('checkin '):
                    self.rows.append(msg(hospital.LEDGER, 'checked_in booking_id=B1 ledger_ref=L1', mid='ci1'))
        rows = [msg(hospital.LEDGER, 'committed hold_id=H1 booking_id=B1 ledger_ref=L1 slot_id=S1', mid='c1')]
        arrival = msg('citizen:1', 'Buongiorno, sono arrivato in ospedale per la visita', mid='a1')
        city = CheckinCity(rows + [arrival])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._facility(city, arrival, list(city.rows), reports=False, hold_self=False))
        self.assertIn((hospital.LEDGER, 'checkin booking_id=B1 citizen=citizen:1'), city.out)
        self.assertTrue(any(to == 'citizen:1' and text.startswith('checkin booking_id=B1') for to, text in city.out))

    def test_other_citizen_words_always_get_a_helpful_answer(self):
        question = msg('citizen:1', 'Scusi, come funziona?', mid='q1')
        city = City([question])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._facility(city, question, list(city.rows), reports=False, hold_self=False))
        self.assertEqual(len(city.out), 1)
        self.assertIn('ricetta', city.out[0][1])


class HospitalCollaborationTest(unittest.TestCase):
    def setUp(self):
        hospital._NOTIFIED.clear()

    def ledger_city(self):
        class C(City):
            agent_id = 'agent:team/hospital'
            async def say(self, to, text):
                await super().say(to, text)
                if to == hospital.LEDGER and text == 'agenda':
                    self.rows.append(msg(hospital.LEDGER, 'agenda agenda_now=2026-11-20T08:00:00+00:00', mid=f'ag{len(self.rows)}'))
                if to == hospital.LEDGER and text.startswith('world '):
                    self.rows.append(msg(hospital.LEDGER, 'slots page=1/1\nslot_id=S1 facility=agent:team/hospital doctor_id=asst-x-h2 procedure=VIS-FIS at=2026-11-20T14:30:00+00:00 day=fri', mid=f'w{len(self.rows)}'))
        return C

    def test_partner_naming_only_the_service_gets_offers(self):
        C = self.ledger_city()
        incoming = msg('agent:other/bc', 'need.request service_type=hospital.physiatry priority=B request_id=r1', mid='p1')
        city = C([incoming])
        asyncio.run(hospital._partner_request(city, incoming, list(city.rows)))
        self.assertTrue(any(to == 'agent:other/bc' and 'slot_id=S1' in text for to, text in city.out))

    def test_partner_asking_in_words_is_answered(self):
        C = self.ledger_city()
        incoming = msg('agent:other/bc', 'Hello, do you have physiatry availability this week?', mid='p2')
        city = C([incoming])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._facility(city, incoming, list(city.rows), reports=False, hold_self=False))
        self.assertTrue(any(to == 'agent:other/bc' and 'slot_id=S1' in text for to, text in city.out))

    def test_ack_from_a_peer_is_never_answered(self):
        incoming = msg('agent:c2ewegr2ea3v', 'ack booking_id=B1 Grazie, preso nota.', mid='a1')
        city = City([incoming])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._facility(city, incoming, list(city.rows), reports=False, hold_self=False))
        self.assertEqual(city.out, [])

    def test_gp_is_told_once_after_the_commit(self):
        incoming = confirm(); city = City([incoming, slots()])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._on_confirm(city, incoming, list(city.rows), hold_self=False))
        notices = [text for to, text in city.out if to == hospital._TEAM_GP]
        self.assertEqual(len(notices), 0)  # RX1 is not one of our GP's numbers
        hospital._NOTIFIED.clear()
        incoming = msg('agent:team/booking', 'confirm offer_id=O1 slot_id=S1 citizen=citizen:1 prescription_number=prescription_number-N-1 consent_ref=C1', mid='confirm2')
        city = City([incoming, slots()])
        with patch.dict(os.environ, {'CITY_ROLE': 'hospital'}):
            asyncio.run(hospital._on_confirm(city, incoming, list(city.rows), hold_self=False))
        notices = [text for to, text in city.out if to == hospital._TEAM_GP]
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0].startswith('booking.notice booking_id=B1 prescription_number=prescription_number-N-1'))


class Mach1Test(unittest.TestCase):
    def test_partner_gets_a_plain_ack_first_then_offers(self):
        C = HospitalCollaborationTest().ledger_city()
        incoming = msg('agent:other/bc', 'need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=r9', mid='p9')
        city = C([incoming])
        asyncio.run(hospital._partner_request(city, incoming, list(city.rows)))
        to_peer = [text for to, text in city.out if to == 'agent:other/bc']
        self.assertTrue(to_peer[0].startswith('Ricevuto, verifico'))
        self.assertNotIn('=', to_peer[0])
        self.assertTrue(any('slot_id=S1' in text for text in to_peer[1:]))

    def test_receipt_ids_name_offer_and_slot(self):
        rows = [msg('agent:team/booking', 'confirm offer_id=O7 slot_id=S7 citizen=citizen:1', mid='c7')]
        rows[0] = Message(type='chat', sender='agent:team/booking', recipient='agent:team/hospital', msg_id='c7', need_id='N-1', body={'text': rows[0].text})
        self.assertEqual(hospital._receipt_ids(rows, 'agent:team/hospital', 'S7'), ' offer_id=O7 slot_id=S7')


class RepeatHoldTest(unittest.TestCase):
    def test_refused_second_hold_does_not_cancel_a_standing_hold(self):
        rows = [msg('agent:team/booking', 'confirm offer_id=O1 slot_id=S1 citizen=citizen:1 consent_ref=C1', mid='c'),
                Message(type='chat', sender='agent:team/hospital', recipient=hospital.LEDGER, msg_id='h1', need_id='N-1', body={'text': 'hold slot_id=S1 citizen=citizen:1'}),
                msg(hospital.LEDGER, 'held hold_id=H1 slot_id=S1', mid='held'),
                Message(type='chat', sender='agent:team/hospital', recipient=hospital.LEDGER, msg_id='h2', need_id='N-1', body={'text': 'hold slot_id=S1 citizen=citizen:1'}),
                msg(hospital.LEDGER, 'refused hold code=SLOT_NOT_FREE', mid='ref')]
        city = City(rows)
        asyncio.run(hospital._on_refused(city, rows[-1], rows, hold_self=False))
        self.assertFalse(any(text.startswith('cancel') for _, text in city.out))
