# On-Site App: launch checklist

This PR combines the On-Site work that was built and tried on the test bot as
#67–#77 and #80. It is the base for the On-Site Specialist App. Nothing here is
live until this PR is merged ("ship").

## What's in it

| Area | What the specialist / team gets |
|---|---|
| Five tabs (specialists) | Today · Visits · Handovers · Requests · Me |
| Today | Next visit (Directions, Open), the day's visits, What's new |
| Visit screen | Arrived, checklist (Set up / Troubleshoot), optional photos, customer sign-off, Leave |
| Leave | Resolved (charge, Cash / PayNow / Other) · Not finished (reason, urgency, next date, pinned in DM) · Needs a replacement |
| Follow-ups | Red border and FOLLOW-UP tag, pinned on top, most urgent first |
| PayNow | Shows TC's own QR (`/paynowqr`), then a photo of the customer's payment (required) |
| Replacement / payment request | No charge / PayNow / Payment link → SH x Ops chat (`/requestchat`); payment link → Finance chat (`/financechat`), name and email not stored |
| Cab claims | Trip, fare and receipt photo in the App → On-Site admin approves / rejects |
| Sign-off | Customer reads a summary, types name, ticks consent, signs on the phone → sent to SH |
| Me | Pay this month (SGD 50 per finished visit, SG), own stats, past visits (no phone / address) |
| Admin → Team → On-Site | Each specialist's visits and pay per month |
| Typed dates and times | 23112026 → 23/11/2026; 230 → 02:30 PM |

## Before shipping

- [ ] **AI lead reviews the database changes** (all created on startup, no manual steps):
  - `appointments`: new columns `urgency TEXT`, `followup_of INTEGER`
  - new table `onsite_requests`
  - new table `cab_claims`
  - new Python package `segno` (QR images) in requirements.txt
- [ ] **Checklists**: replace the draft steps in `CHECKLISTS` (onsite.py) with the specialist's real Set up and Troubleshoot steps (Sonos + Marshall Heston 120/60, Sub 200), then set `CHECKLIST_DRAFT = False`.
- [ ] **Sign-off wording**: manager / legal confirm `SIGNOFF_TEXT` (onsite.py), then set `SIGNOFF_DRAFT = False`. Decide whether anything only the Zoho form collects (proof of purchase, order no.) is still needed.
- [ ] **Finance**: the official TC PayNow QR image; who is in the Finance chat; cab claim rules (limit, receipt, approver, payout).
- [ ] **Ops**: which chat gets replacement / payment requests.
- [ ] **Rates**: visit pay for MY / HK / TH (`VISIT_PAY` in onsite.py) and visit prices (`PRICING`), or launch SG only.
- [ ] Optional: review link (`REVIEW_URL` env var) for the review QR.
- [ ] Dry run on the test bot with one specialist, one SH / FS person and the On-Site admin: a resolved PayNow visit (payment photo + sign-off), a not-finished visit, a replacement request + cab claim.

## Ship and set up (live)

1. Merge after 8pm when the bot is quiet; watch the Railway deploy.
2. In a DM with the live bot (admin): `/paynowqr`, then send TC's QR image.
3. In the SH x Ops chat: `/requestchat`. In the Finance chat: `/financechat`. In the On-Site report chat: `/visitchat`.
4. Make the On-Site admin an admin with Admin for = On-Site (People in the App).
5. SG first for 1–2 weeks; MY / HK / TH once rates are set.
6. Backup if the bot is down: the Zoho form and chat, as before.

## Known limits

- The App runs inside Telegram, which can't open phone links, so there is no Call button. Numbers are tap-to-select.
- Photos taken in the App are also sent to the person's own chat with the bot (Telegram stores photos by message).
- Specialists who are also in another team keep the older two-tab layout.
