"""
Generates N synthetic emails matching the inbox.json schema in the challenge
brief (§3.1). Used by the frontend's "generate sample emails" button and for
local testing when you don't have the real inbox.json. Mirrors the 12 worked
examples' patterns/traps so the routing logic has realistic signal to chew on.
"""
import json
import random
import sys
from datetime import datetime, timedelta

random.seed(42)

COMPANIES = ["Meridian Steel", "Railyard Logistics", "Halcyon Retail", "Zenith Cloud Partners",
             "Vantage Cloud Services", "Bharat Heavy Electricals Limited", "Orbit Analytics",
             "Northbridge Traders", "Sundar Textiles", "Prime Freight Co", "Lakeview Hospitality"]

TEMPLATES = [
    # (subject, body_template, category_hint)
    ("RFP - Enterprise Document Management System",
     "Dear Team,\n\n{company} invites proposals for an enterprise DMS covering 4 plants and ~1,200 users. "
     "Indicative budget is Rs. {lakhs} lakhs. Proposals must reach us by {deadline}.\n\nRegards,\n{sender}",
     "rfp_high"),
    ("Quick demo request",
     "Hi, we're a {size}-person team at {company}. Could we get a demo sometime next week? "
     "Nothing urgent.\n\n— {sender}",
     "smb"),
    ("Tender Notice - Government Procurement",
     "Tender Notice No. GOV/PROC/2026/{num}. {company} (a PSU) invites bids for supply of analytics "
     "software licences. Estimated value: Rs. {small_amt}. Last date for bid submission: {deadline}.",
     "psu"),
    ("Sponsorship confirmation needed",
     "We're finalising sponsors for a conference in Bengaluru. Gold tier is Rs. {sponsor_amt} and includes "
     "a keynote slot. We need confirmation by {near_deadline}.\n\n— {sender}, Sponsorship Lead",
     "marketing"),
    ("Invoice for services rendered",
     "Please find attached invoice INV-2026-{num} for Rs. {invoice_amt} against PO-{num}. "
     "Kindly process — this is now overdue.",
     "finance"),
    ("Reseller partnership enquiry",
     "We're an implementation partner with 40+ enterprise clients. We'd like to explore reselling your "
     "platform, or a technical integration at minimum. Who handles partnerships?\n\n— {sender}",
     "alliances"),
    ("Out of Office",
     "I am out of office until {far_deadline} with limited access to email. For urgent matters please "
     "contact my colleague.\n\nSent from Outlook",
     "ooo"),
    ("Grow your organic traffic 3x",
     "Hi, I noticed your website isn't ranking on page 1. We've helped 200+ SaaS companies 3x their "
     "organic traffic. We do content marketing, PR outreach, and webinar promotion. Free audit attached "
     "— interested in a quick 15 min call?",
     "spam"),
    ("The B2B Growth Weekly — Issue #{num}",
     "In this edition: why PLG is stalling, 5 pricing experiments that worked, and a teardown of a "
     "famous onboarding flow. [Unsubscribe]",
     "newsletter"),
    ("Product enquiry",
     "Bhai, humko aapka product chahiye for our dealer network. Around {size} users honge. "
     "Budget approx {cr} cr allocated hai for this FY. Kab connect kar sakte hain? Board review {deadline} ko hai.",
     "hinglish_rfp"),
]

FIRST = ["Suresh", "Ankit", "Nandita", "Farhan", "Priya", "Rohan", "Divya", "Karan", "Meera", "Vikram"]
LAST = ["Kulkarni", "Bose", "Reddy", "Qureshi", "Sharma", "Iyer", "Rao", "Doshi", "Menon", "Nair"]


def rand_person():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"


def rand_email(name, company):
    domain = company.lower().replace(" ", "").replace(".", "")[:14]
    local = name.lower().replace(" ", ".")
    return f"{local}@{domain}.co.in"


def gen_emails(n=250):
    emails = []
    base_time = datetime(2026, 8, 1, 9, 0, 0)
    for i in range(n):
        subj_t, body_t, hint = random.choice(TEMPLATES)
        company = random.choice(COMPANIES)
        sender = rand_person()
        received = base_time + timedelta(hours=random.randint(0, 200), minutes=random.randint(0, 59))
        deadline = (received + timedelta(days=random.randint(4, 20))).strftime("%d %B %Y")
        near_deadline = (received + timedelta(hours=random.randint(6, 48))).strftime("%d %B %Y, %H:%M")
        far_deadline = (received + timedelta(days=random.randint(10, 20))).strftime("%d %B")

        body = body_t.format(
            company=company, sender=sender, size=random.choice([25, 40, 80, 150, 800]),
            lakhs=random.choice([12, 25, 40, 60]), small_amt=f"{random.randint(3,9)},50,000",
            sponsor_amt=f"{random.randint(2,6)},00,000", invoice_amt=f"{random.randint(50,199)},000",
            num=str(random.randint(100, 999)), deadline=deadline, near_deadline=near_deadline,
            far_deadline=far_deadline, cr=round(random.uniform(0.5, 2.5), 1),
        )
        thread_id = f"th_{1000+i//3:04d}"  # occasional thread reuse to simulate replies
        emails.append({
            "email_id": f"em_{i:05d}",
            "thread_id": thread_id,
            "message_index": i % 3,
            "from_name": sender,
            "from_email": rand_email(sender, company),
            "to": "sales@company.com",
            "cc": [],
            "subject": subj_t,
            "body": body,
            "received_at": received.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "attachments": [],
            "is_reply": (i % 3 != 0),
        })
    return emails


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    out = sys.argv[2] if len(sys.argv) > 2 else "sample_inbox.json"
    with open(out, "w") as f:
        json.dump(gen_emails(n), f, indent=2)
    print(f"wrote {n} emails to {out}")
