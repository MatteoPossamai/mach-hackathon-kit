# Booking centre - first implementation

The centre checks the prescription with the Ledger, reads the agenda and local
slots, filters by priority and stated preferences, then sends the choices in one
message. The chosen facility performs the booking; the centre forwards its receipt.
When local slots do not fit, the centre asks discovered facilities for availability
without sending citizen details and asks the citizen for cross-ASST consent before
sharing them. It searches again after a facility cancels, excluding the cancelled
slot, and responds to detected emergencies. Each case is reconstructed from history.

The citizen's selected offer is looked up in the centre's own messages. A citizen
cannot replace its slot or facility in a confirmation. Unsolicited peer offers and
receipts are ignored. A peer's reply is treated as a claim, not an instruction.

## Known limits

- This is a first implementation, not a fully verified contest-ready agent. The
  local simulator has no agenda or rules and cannot simulate partner bookings.
- The facility protocol in the supplied example books only after confirmation. This
  implementation cannot ensure slots are held before they are offered. Partner
  offers must include enough fields to check procedure, facility, time and needs.
- The Ledger's `rules` response is requested but is not yet interpreted. Prescription
  expiry and other live rule changes need implementation before use in the event.
- A receipt is accepted only from the contacted facility, but its Ledger reference
  is not independently verified. Partner interoperability needs a live integration
  test, including consent and receipt formats, timeouts, cancellations and retries.
- Doctor attributes fall back to the published example's seeded table. Unknown
  attributes are not assumed to satisfy a stated preference; the table must be
  replaced by authoritative registry data if the city exposes it.
- Cancellation triggers a new search, but there is no persistent follow-up if a
  facility never responds. A timed-out confirmation is not resent with a fresh
  message ID, because that could create a duplicate booking.
- Partner offers are sent from those received before the citizen grants consent;
  later replies may not be included in the citizen's initial choice. Remote offer
  fields and consent formats still require live interoperability testing.
- A wildcard language in the sample doctor table represents one unknown local
  language, not all foreign languages; unknown languages are excluded until the
  registry supplies the actual language.

Run the focused tests from the repository root with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```
