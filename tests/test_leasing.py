from __future__ import annotations

import unittest
from datetime import date, datetime

from app import leasing

LIST_HTML = """
<table><tr><th> Tenants </th><th> Unit Name </th><th> Lease Generation Date </th><th> Status </th><th> Action </th></tr>
<tr data-href="/lease_documents/2284" data-href-target="_blank" class="js-lease-document-row">
 <td> Philip B. Sample III<br>Hannah E. Whitlock </td> <td> 1253 Juniper Street </td>
 <td> 03/12/2026 02:31 PM </td> <td> Out for Signing </td>
 <td> <a class='btn btn-secondary btn-sm lease-document-row-action-button'>View</a> </td></tr>
<tr data-href="/lease_documents/2363" data-href-target="_blank" class="js-lease-document-row">
 <td> Marco J. Pereira<br>Eva Sol Ortega </td> <td> 24 Hawthorn Ave, - First Floor </td>
 <td> 09/24/2026 01:23 PM </td> <td> Out for Signing </td>
 <td> <a class='btn'>View</a> </td></tr>
</table>"""

DETAIL_TEXT = """Lease Status: Out for Signing
Sent on 06/25/2026 at 02:40 pm
Last resent on 06/27/2026 at 06:35 pm
Resend to Tenant Print to PDF Cancel Lease"""


def _box(title, signature="&nbsp;", stamp="", ip=False):
    ip_html = '<span class="signature-box__ip">IP Address: 10.0.0.1</span><br>' if ip else ""
    return (f'<div class="signature-box"> <span class="signature-box__x">X</span> '
            f'<div class="signature-box__signature-container"> <span class="signature-box__signature">{signature}</span> '
            f'<span class="signature-box__label"> <span class="signer-title" style="font-weight: normal">{title}</span> '
            f'{ip_html}<span class="signature-box__timestamp">{stamp}</span> </span> </div> </div>')


def _page(*tenant_boxes, blocks=3):
    block = ('<div class="js-lease-signatures text-end"> <!-- Lessee/co-signer area --> '
             + " ".join(tenant_boxes) + " " + _box("Lessor") + ' <br> </div>')
    return "<div>Lease Status: Out for Signing</div>" + "".join(block for _ in range(blocks))


PARTLY_SIGNED = _page(_box("Lessee", stamp="Hannah E. Whitlock"),
                      _box("Lessee", "Philip Sample III", "03/31/2026 11:58pm EDT", ip=True))


def tenant(last, first, unit, street, status="Current", email="x@example.com", **extra):
    row = {"LastName": last, "FirstName": first, "Unit": unit, "PropertyStreet": street,
           "Status": status, "Emails": email, "PrimaryTenant": "Yes", "Tenant": f"{last}, {first}"}
    row.update(extra)
    return row


CFG = {"first_reminder_after_days": 2, "reminder_every_days": 3, "max_reminders": 3, "escalate_after_days": 14,
       "summary_include_expiring_days": 90, "summary_include_applications_days": 30}


class ParsingTests(unittest.TestCase):
    def test_parses_list_rows(self):
        leases = leasing.parse_lease_list(LIST_HTML)
        self.assertEqual([l.document_id for l in leases], ["2284", "2363"])
        self.assertEqual(leases[0].tenant_names, ["Philip B. Sample III", "Hannah E. Whitlock"])
        self.assertEqual(leases[0].unit, "1253 Juniper Street")
        self.assertEqual(leases[0].generated_on, date(2026, 3, 12))
        self.assertEqual(leases[0].status, "Out for Signing")

    def test_parses_detail_dates(self):
        detail = leasing.parse_lease_detail(DETAIL_TEXT)
        self.assertEqual(detail["sent_on"], date(2026, 6, 25))
        self.assertEqual(detail["last_resent_on"], date(2026, 6, 27))
        self.assertEqual(detail["status"], "Out for Signing")

    def test_parses_detail_html(self):
        page = """<div>Lease Status:<br class="desk-hidden">
      <span class="js-lease-status lease-status">Out for Signing</span></div>
    <div class="js-lease-sent-at-container">
      Sent on<br class="desk-hidden tablet-hidden">
      <span class="js-lease-sent-at">
          06/25/2026 at 02:40 pm
      </span></div>
    <div class="js-lease-last-resent-at-container">
      Last resent on<br class="desk-hidden tablet-hidden">
      <span class="js-lease-last-resent-at">06/27/2026 at 06:35 pm</span></div>"""
        detail = leasing.parse_lease_detail(page)
        self.assertEqual(detail["sent_on"], date(2026, 6, 25))
        self.assertEqual(detail["last_resent_on"], date(2026, 6, 27))
        self.assertEqual(detail["status"], "Out for Signing")

    def test_detail_without_resend(self):
        detail = leasing.parse_lease_detail("Lease Status: Out for Signing\nSent on 01/02/2026 at 10:00 am")
        self.assertIsNone(detail["last_resent_on"])


    def test_signers_partly_signed(self):
        signers = leasing.parse_signers(PARTLY_SIGNED)
        self.assertEqual([x["name"] for x in signers], ["Hannah E. Whitlock", "Philip Sample III"])
        self.assertIsNone(signers[0]["signed_at"])
        self.assertEqual(signers[1]["signed_at"], datetime(2026, 3, 31, 23, 58))

    def test_signers_from_detail_and_summary(self):
        lease = leasing.LeaseDocument("2284", ["Philip Sample III", "Hannah E. Whitlock"], "1253 Juniper Street",
                                      None, leasing.OUT_FOR_SIGNING)
        lease.signers = leasing.parse_lease_detail(PARTLY_SIGNED)["signers"]
        self.assertEqual(lease.unsigned_names, ["Hannah E. Whitlock"])
        self.assertEqual(lease.signer_summary(),
                         "Hannah E. Whitlock: not signed · Philip Sample III: signed 03/31/2026")

    def test_signer_counts_only_when_every_page_signed(self):
        signed = _box("Lessee", "Eva Sol", "11/11/2026 02:44pm EST", ip=True)
        unsigned = _box("Lessee", stamp="Eva Sol Ortega")
        block = lambda box: ('<div class="js-lease-signatures"> ' + box + " " + _box("Lessor") + " </div>")
        signers = leasing.parse_signers(block(signed) + block(signed) + block(unsigned))
        self.assertEqual(signers, [{"name": "Eva Sol Ortega", "signed_at": None}])

    def test_nobody_signed(self):
        signers = leasing.parse_signers(_page(_box("Lessee", stamp="A One"), _box("Lessee", stamp="B Two")))
        self.assertEqual([x["signed_at"] for x in signers], [None, None])

    def test_mismatched_blocks_mean_unknown(self):
        one = '<div class="js-lease-signatures"> ' + _box("Lessee", stamp="A One") + " </div>"
        two = '<div class="js-lease-signatures"> ' + _box("Lessee", stamp="A One") + _box("Lessee", stamp="B Two") + " </div>"
        self.assertEqual(leasing.parse_signers(one + two), [])

    def test_plain_text_detail_has_no_signers(self):
        self.assertEqual(leasing.parse_lease_detail(DETAIL_TEXT)["signers"], [])

    def test_vacancies_rent_ready_only_with_address(self):
        rows = leasing.vacancy_rows([
            {"PropertyStreet": "115 S Harbor Ave", "PropertyCity": "Brookfield", "PropertyState": "FL",
             "PropertyZip": "33815", "Unit": "Suite 500-501", "RentReady": "Yes", "DaysVacant_raw": 147},
            {"PropertyStreet": "654-658 Linden Avenue  NJ", "PropertyCity": "Linden", "PropertyState": "NJ",
             "PropertyZip": "07206", "Unit": "1", "RentReady": "No", "DaysVacant_raw": 300},
        ])
        self.assertEqual(len(rows), 1, "not rent ready is left out")
        self.assertEqual({k: rows[0][k] for k in ("street", "unit", "city", "state", "zip")},
                         {"street": "115 S Harbor Ave", "unit": "Suite 500-501", "city": "Brookfield",
                          "state": "FL", "zip": "33815"})


    def _partly_signed_lease(self):
        lease = leasing.LeaseDocument("2284", ["Philip Sample III", "Hannah E. Whitlock"], "1253 Juniper Street",
                                      None, leasing.OUT_FOR_SIGNING)
        lease.signers = leasing.parse_signers(PARTLY_SIGNED)
        lease.tenants = [
            tenant("Sample", "Philip", "1253 Juniper Street", "1253 Juniper Street", email="philip@example.com",
                   PhoneNumbers="Mobile: (732) 555-0101", TenantPortalActivated="Yes"),
            tenant("Whitlock", "Hannah", "1253 Juniper Street", "1253 Juniper Street", email="hannah@example.com",
                   PhoneNumbers="Home: (732) 555-0200, Mobile: (732) 555-0202", TenantPortalActivated="No"),
        ]
        return lease

    def test_facts_target_only_the_unsigned_tenant(self):
        facts = leasing.reminder_facts(self._partly_signed_lease())
        self.assertTrue(facts["known"])
        self.assertEqual(facts["issue"], "")
        self.assertEqual(facts["signed"], ["Philip"])
        self.assertEqual([(t["first"], t["emails"], t["phone"], t["portal"]) for t in facts["toSign"]],
                         [("Hannah", ["hannah@example.com"], "(732) 555-0202", "No")])

    def test_facts_unknown_without_signature_boxes(self):
        lease = self._partly_signed_lease()
        lease.signers = []
        self.assertFalse(leasing.reminder_facts(lease)["known"])

    def test_facts_flag_same_person_signed_and_unsigned(self):
        lease = self._partly_signed_lease()
        # Philip signed in Hannah's place: both boxes point at Philip.
        lease.signers = [{"name": "Philip Sample", "signed_at": None},
                         {"name": "Philip sample", "signed_at": datetime(2026, 10, 23, 9, 0)}]
        self.assertIn("check who signed", leasing.reminder_facts(lease)["issue"])

    def test_unmatched_signer_has_no_email(self):
        lease = self._partly_signed_lease()
        lease.tenants = lease.tenants[:1]
        to_sign = leasing.reminder_facts(lease)["toSign"]
        self.assertEqual((to_sign[0]["first"], to_sign[0]["emails"], to_sign[0]["matched"]), ("Hannah", [], False))

    def test_renewing_tenant_with_two_rows_still_matches(self):
        lease = self._partly_signed_lease()
        future = dict(lease.tenants[1], Status="Future", Emails="", PhoneNumbers="")
        lease.tenants.append(future)
        to_sign = leasing.reminder_facts(lease)["toSign"]
        self.assertEqual((to_sign[0]["emails"], to_sign[0]["matched"]), (["hannah@example.com"], True))

    def test_signed_in_someone_elses_box_with_renewal_rows(self):
        lease = self._partly_signed_lease()
        lease.tenants.append(dict(lease.tenants[0], Status="Future"))
        lease.signers = [{"name": "Philip Sample", "signed_at": None},
                         {"name": "philip sample", "signed_at": datetime(2026, 10, 23, 9, 0)}]
        self.assertIn("check who signed", leasing.reminder_facts(lease)["issue"])

    def test_first_name_drops_middle_initial(self):
        self.assertEqual(leasing._first_name({"FirstName": "Priya R."}, ""), "Priya")
        self.assertEqual(leasing._first_name({"FirstName": "Maria Jose"}, ""), "Maria Jose")
        self.assertEqual(leasing._first_name({"FirstName": "Noah & Robin"}, ""), "Noah & Robin")

    def test_household_prefers_exact_full_name(self):
        rows = [tenant("Rivera", "Victor", "4 Quiet Street", "4 Quiet Street", email="o@example.com"),
                tenant("Rivera jones", "Victor", "4 Quiet Street", "4 Quiet Street", email=""),
                tenant("Rivera-jones", "Victor", "4 Quiet Street", "4 Quiet Street", email=""),
                tenant("Jones", "Lucia", "4 Quiet Street", "4 Quiet Street", email="a@example.com")]
        self.assertEqual(leasing._signer_row("Victor Rivera", rows)["Emails"], "o@example.com")

    def test_empty_list(self):
        self.assertEqual(leasing.parse_lease_list("There are currently no lease documents"), [])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.lease = leasing.parse_lease_list(LIST_HTML)[1]

    def test_matches_by_address_and_last_name(self):
        rows = [
            tenant("Pereira", "Marco", "24 Hawthorn Ave - First Floor", "24 Hawthorn Avenue", email="marco@example.com"),
            tenant("Ortega", "Eva", "24 Hawthorn Ave - First Floor", "24 Hawthorn Avenue", email="eva@example.com; eva2@example.com"),
            tenant("Pereira", "Someone", "99 Other Rd", "99 Other Road"),          # wrong address
            tenant("Stranger", "Bob", "24 Hawthorn Ave - Second Floor", "24 Hawthorn Avenue"),  # wrong name
            tenant("Ortega", "Old", "24 Hawthorn Ave", "24 Hawthorn Avenue", status="Past"),     # past tenant
        ]
        self.lease.tenants = leasing.match_tenants(self.lease, rows)
        self.assertEqual(len(self.lease.tenants), 2)
        self.assertEqual(self.lease.emails, ["marco@example.com", "eva@example.com", "eva2@example.com"])

    def test_no_match_gives_no_emails(self):
        self.lease.tenants = leasing.match_tenants(self.lease, [tenant("Nobody", "X", "1 Elm St", "1 Elm Street")])
        self.assertEqual(self.lease.emails, [])

    def test_address_normalization(self):
        self.assertEqual(leasing._street_key("24 Hawthorn Avenue"), leasing._street_key("24 Hawthorn Ave, - First Floor"))


class ReminderPolicyTests(unittest.TestCase):
    def make(self, sent, resent=None, emails=True, status=leasing.OUT_FOR_SIGNING):
        lease = leasing.LeaseDocument("1", ["A B"], "1 Elm St", sent, status, sent_on=sent, last_resent_on=resent)
        if emails:
            lease.tenants = [{"Emails": "a@example.com"}]
        return lease

    def test_due_after_first_wait(self):
        d = leasing.decide_reminders([self.make(date(2026, 1, 1))], {"leases": {}}, date(2026, 1, 5), CFG)[0]
        self.assertTrue(d.send)
        self.assertEqual(d.reminder_number, 1)

    def test_not_due_when_just_sent(self):
        d = leasing.decide_reminders([self.make(date(2026, 1, 4))], {"leases": {}}, date(2026, 1, 5), CFG)[0]
        self.assertFalse(d.send)

    def test_appfolio_resend_counts_as_contact(self):
        d = leasing.decide_reminders([self.make(date(2026, 1, 1), resent=date(2026, 1, 4))], {"leases": {}}, date(2026, 1, 5), CFG)[0]
        self.assertFalse(d.send)

    def test_interval_after_our_reminder(self):
        state = {"leases": {"1": {"reminders": [{"at": "2026-01-05T12:00:00+00:00", "status": "sent"}]}}}
        lease = self.make(date(2026, 1, 1))
        self.assertFalse(leasing.decide_reminders([lease], state, date(2026, 1, 7), CFG)[0].send)
        d = leasing.decide_reminders([lease], state, date(2026, 1, 8), CFG)[0]
        self.assertTrue(d.send)
        self.assertEqual(d.reminder_number, 2)

    def test_failed_reminders_do_not_count(self):
        state = {"leases": {"1": {"reminders": [{"at": "2026-01-05T12:00:00+00:00", "status": "failed"}]}}}
        self.assertTrue(leasing.decide_reminders([self.make(date(2026, 1, 1))], state, date(2026, 1, 6), CFG)[0].send)

    def test_stops_at_max(self):
        sent = [{"at": f"2026-01-0{i}T12:00:00+00:00", "status": "sent"} for i in (2, 3, 4)]
        d = leasing.decide_reminders([self.make(date(2026, 1, 1))], {"leases": {"1": {"reminders": sent}}}, date(2026, 2, 1), CFG)[0]
        self.assertFalse(d.send)
        self.assertIn("max", d.reason)

    def test_no_email_goes_to_staff(self):
        d = leasing.decide_reminders([self.make(date(2026, 1, 1), emails=False)], {"leases": {}}, date(2026, 2, 1), CFG)[0]
        self.assertFalse(d.send)
        self.assertIn("staff", d.reason)

    def test_stale_lease_goes_to_staff_only(self):
        cfg = dict(CFG, stale_after_days=60)
        d = leasing.decide_reminders([self.make(date(2026, 1, 1))], {"leases": {}}, date(2026, 4, 1), cfg)[0]
        self.assertFalse(d.send)
        self.assertIn("stale", d.reason)

    def test_countersign_never_reminds_tenant(self):
        lease = self.make(date(2026, 1, 1), status=leasing.READY_TO_COUNTERSIGN)
        self.assertFalse(leasing.decide_reminders([lease], {"leases": {}}, date(2026, 2, 1), CFG)[0].send)


class StateTests(unittest.TestCase):
    def test_signed_lease_closes_and_is_reported_once(self):
        lease = leasing.LeaseDocument("7", ["A"], "1 Elm", date(2026, 1, 1), leasing.OUT_FOR_SIGNING)
        state = {"leases": {}}
        leasing.reconcile_state(state, [lease], [], "t1")
        self.assertEqual(leasing.unreported_closed(state), [])
        closed = leasing.reconcile_state(state, [], [], "t2")
        self.assertEqual([c["document_id"] for c in closed], ["7"])
        self.assertEqual(len(leasing.unreported_closed(state)), 1)
        state["leases"]["7"]["closed_reported"] = True
        self.assertEqual(leasing.reconcile_state(state, [], [], "t3"), [])

    def test_reopened_lease_clears_closed(self):
        lease = leasing.LeaseDocument("7", ["A"], "1 Elm", date(2026, 1, 1), leasing.OUT_FOR_SIGNING)
        state = {"leases": {"7": {"reminders": [], "closed_at": "t0"}}}
        leasing.reconcile_state(state, [lease], [], "t1")
        self.assertIsNone(state["leases"]["7"]["closed_at"])


class SummaryTests(unittest.TestCase):
    def test_expiring_and_renewal_flag(self):
        today = date(2026, 1, 1)
        rows = [
            tenant("A", "a", "1 Elm St", "1 Elm Street", LeaseTo_raw="2026-02-01"),
            tenant("B", "b", "2 Oak Ave", "2 Oak Avenue", LeaseTo_raw="2026-12-01"),  # outside window
            tenant("C", "c", "3 Pine Rd", "3 Pine Road", LeaseTo_raw="2025-12-01"),   # month-to-month
        ]
        exp = leasing.expiring_leases(rows, today, 90, {leasing._street_key("1 Elm Street")})
        self.assertEqual([r["tenant"] for r in exp], ["A, a"])
        self.assertTrue(exp[0]["renewal_out"])
        self.assertEqual(leasing.month_to_month(rows, today), 1)

    def test_render_summary_and_reminder(self):
        lease = leasing.parse_lease_list(LIST_HTML)[0]
        lease.tenants = [{"FirstName": "Philip", "Emails": "p@example.com"}]
        html = leasing.render_summary(today=date(2026, 11, 23), pending=[lease], countersign=[], decisions={},
                                      state={"leases": {}}, closed=[], expiring=[], mtm=0, notices=[],
                                      vacancies=[], apps={"total": 0, "by_status": {}, "open": []}, cfg=CFG)
        self.assertIn("1253 Juniper Street", html)
        self.assertIn("OVERDUE", html)
        subject, body, text = leasing.render_tenant_reminder(lease, 1, {"reminder_subject": "Sign {unit}"})
        self.assertEqual(subject, "Sign 1253 Juniper Street")
        self.assertIn("Hi Philip,", body)
        self.assertIn("/connect", body)


if __name__ == "__main__":
    unittest.main()
