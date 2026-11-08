# 6. Other features

## 6.1 Member profiles and directory

**Profile** (member edits their own): social links (Twitter, Facebook, GitHub, IRC, website; Google+ ⇒ drop),
tagline, description, **skills** chosen from a fixed list with icons (MIDI, 3D printing, Arduino, coding, electronics,
laser cutter, welding, woodworking, audio, CAD, arts & crafts, retro computing, PCB design, PC building, social media,
networking, graphic design, biology, sewing, printing), and a **photo**.

**Photo workflow:** upload → cropped to a 500 px square plus a 200 px thumbnail → stored as *pending* → an admin approves
(replaces the live photo) or declines (member is emailed). Members can mark the photo private (only members see it).
Members in `active`/`payment-warning`/`honorary` status may see protected photos.

**Directory** (`/members`):
- logged-out visitors see active, non-leaving members who have **not** chosen `profile_private`;
- logged-in members also see private ones;
- a member page shows the public profile; private profiles are hidden from logged-out visitors.

⚠️ Verify: is the logged-out public directory still wanted? It reveals who is a member.

**Groups** (`/groups`): lists each role with its title, description, contact email and members.

### Build status (2026-11-08)

Built in `apps/profiles`. Choices made while building:
- **Profile** (`/account/profile/`): tagline, "about you", skills (the legacy list and keys, 21 skills), website,
  GitHub, X (Twitter), Facebook and IRC (a nickname only: legacy linked to freenode, which has closed; Google+ is
  dropped). Pasted profile addresses and "@name" are trimmed to the username. "Keep my profile private" and "only
  members see my photo" are on the same page. Every member has a profile (made with the member; the import makes
  them from `profile_data`).
- **Photos:** JPEG, PNG or WebP up to 15 MB, turned the right way up, cropped to a square from the middle (no
  cropping tool), stored as 500 px and 200 px PNGs with the camera's metadata (e.g. location) removed. A new upload
  replaces one waiting. Approving and declining are in the admin (Members → Member photos, which opens on the
  waiting ones), for anyone with the "change member photo" permission; approving sets "photo approved" (logged in
  the audit log) and declining emails the member. A member can delete their own photo (a key holder is warned it
  stops their fob opening the door). Photos are served only through BBMS, after checking the viewer may see them.
  The optional photo at registration (03 §3.1) became a "next step" on the account page instead: a file on the long
  registration form is lost whenever another field has an error.
- **Who sees photos marked "only members":** members who may be in the space (active, leaving, honorary), and
  admins who approve photos. Legacy also allowed payment-warning, which no longer exists.
- **Directory** (`/members/`): photo (or initials), name, key holder and trustee (the `trustees` role) badges, with
  a name search and a skill filter. Listed: active and honorary members who aren't banned. **Members** (active,
  leaving, suspended, honorary, or staff) see everyone listed; people who are still setting up or have left don't
  see it. **Visitors** see it only if the new setting "show the member list to visitors" is on (**off** to start
  with, pending the trustees' decision, 01 §1.7 Q7), and never private profiles. A member can always see their own
  page.
- **Member page** (`/members/<id>/`): photo, tagline, description (plain text with line breaks), links, groups
  (members only), skills with icons. The icons are legacy's (The Noun Project, CC BY), credited at `/credits/`;
  "arts and crafts" has no icon, as legacy's had no licence file.
- **Groups** (`/groups/`, members only): each role's title, description, contact email and Discord channels
  (linked on Discord, 06 §6.14; a group can have several, as groups are broader than the server's topics), edited in
  the admin (Users → Groups), where a role can also be hidden from the page. The import fills them from legacy's
  roles (its private role email isn't shown, so isn't kept; its Slack channel names are dropped with Slack).
- Admins are told in their notifications when a photo is waiting for approval (06 §6.5).

## 6.2 Storage: boxes and lockers

Boxes and lockers follow **different rules** (decided 2026-11-06).

### Today
- Boxes are all the same size (**19 L**). The legacy code still has volume units, a 19-unit allowance, £5 deposits
  and box assignments, but these are obsolete: the £5 deposit is hidden from members, and nobody tracks which box belongs to whom.
  Members label their box clearly with their name.
- The box fee is charged through a **virtual equipment item, "Member Box"**, marked as requiring an induction, with a
  one-off access fee. Paying that "induction fee" records a payment (reason `induction`) and a paid induction for the
  item. Confirmed on production (2026-11-07): equipment id 41, slug `19l-box`, induction category `Box`, access fee £15,
  created 2023-04-02. The first box payment recorded through it is dated 2024-09-04; box fees paid before then were
  taken outside BBMS, so those members won't be flagged automatically and the storage team ticks them.
  Production check (2026-11-07, `migration/queries/member_box_check.sql`): 15 members would be flagged (8 active,
  1 leaving, 6 left), all £15 and all paid (Direct Debit 8, card 4, credit 3); no failed payments, duplicates or
  other equipment sharing the `Box` category.
- Lockers aren't in BBMS today.

### Boxes (decided 2026-11-06: a simple flag)
- BBMS records only **whether a member has paid for a box**: a "box paid" flag on the member, with the date and the
  payment it came from. There is **no box inventory**: no box numbers, locations, assignments, claiming or releasing.
  Members label their own box with their name, as now.
- **Fee:** one-off and **non-refundable** (a setting; currently £15). Paying it sets the flag.
- **Paying:** a member without the flag can pay the box fee themselves, by credit, Direct Debit or card (the amount
  is worked out by the server). The storage team or an admin can also set the flag and record a fee paid outside
  BBMS (cash or bank transfer), or clear the flag with a reason (audit-logged).
- ❓ Does the flag stay if a member leaves and later rejoins? Suggested: yes, a box is paid for once per member.
- **At cutover** (decided 2026-11-07): members with a **paid** induction for "Member Box" get the flag (08 §8.2), and
  the "Member Box" equipment item is removed. The box fee setting starts from that item's access fee.
- The storage team can see a list of members with the flag, and of flagged members who have **left**, so they can
  check the space for boxes labelled with those names.

### Lockers (inventory admin-editable)
- Each locker has a **location** in the space (a managed list, e.g. "Workshop wall") and a **number**. Location +
  number is unique and is how people refer to it ("Workshop wall locker 12").
- Each locker has a **size**, from a managed list of locker sizes (physical sizes, e.g. "Large").
- **Default rents are set per size and location** (changed 2026-11-07): each combination of location and size has
  its own **default monthly rent**, e.g. "Large lockers on the Workshop wall: £8; Large lockers in the Office: £10".
  A locker can only be added once its size and location have a default rent (or it has its own advertised price).
- Admins add, move, renumber and resize lockers. A locker can't be removed while it's rented; a locker that has ever
  been rented is **retired** rather than deleted, so its history is kept.

- **Status** of each locker: **available**, **rented**, **reserved for trustees**, or **out of use**. Members can
  only rent available lockers. Admins can mark a locker reserved (with a note of what for) or out of use (e.g.
  broken), but only while it isn't rented. This is how a trustee **overrides the availability advertised for an
  empty locker**: reserved and out-of-use lockers aren't shown to members. "Rented" isn't stored: it means the
  locker has an open rental.
- **Limit:** members may rent up to a **maximum number of lockers** (a setting; ❓ suggested 1). Admins can set a
  **different limit for an individual member** at their discretion, with a reason. The override is audit-logged and
  shown on the member's admin page.
- **Rent:** charged **monthly** while the member holds the locker. There are two kinds of price override (changed
  2026-11-07), both set by a trustee in exceptional circumstances (including £0), each with a reason and
  audit-logged:
    - **What one member pays for their locker** (an override on the **rental**): e.g. a reduced rent for a volunteer.
      It belongs to that rental, so it **ends automatically when the rental ends** and the next renter pays the
      default.
    - **The price advertised for an empty locker** (an override on the **locker**): e.g. a cheaper price for a
      locker with a dented door. Members see it on the lockers page, and whoever rents that locker next pays it: it
      moves onto their rental (as above) and is cleared from the locker.
    - So the price a renter pays = their rental's override if set, otherwise the default rent for the locker's size
      and location.
    - ❓ When an admin changes a default rent, should existing renters pay the new rent? Suggested: yes, from
      the first charge at least 30 days after they've been emailed about it. (Built so far: renters without an
      override simply follow the current default, and the admin warns when a change affects current renters.)
- **Renting (self-service):**
    1. The member picks an **available** locker, within their limit.
    2. They need a way to pay monthly: a Direct Debit mandate, or member credit (with or without Direct Debit as a
       backup). Card-only payers can't rent until they set one up.
    3. The first payment is taken immediately. ❓ Suggested: a pro-rata amount for the days until their next
       payment day, then the full rent on each payment day.
    4. The locker becomes **rented** to them straight away.
- **Monthly billing:** locker rent is billed on the member's **payment day** by the nightly billing job (03 §3.6),
  alongside their subscription. A Direct Debit member gets **one** GoCardless payment per payment day covering
  their subscription and rent, so they see one debit and pay at most one failure fee. Honorary members, who pay no
  subscription, still pay locker rent.
- **Unpaid rent:** if a rent payment fails, the member and the storage team are notified and it's retried as the
  Direct Debit retry policy says (03 §3.6). ❓ Suggested: after **30 days** unpaid, the storage team may empty the
  locker and release it.
- **Giving it up:** by the member, or by the storage team or an admin. Rent stops from that day. ❓ Suggested: no
  refund for the rest of the month already paid. The rental's price override ends with it and the locker becomes
  available at its default rent.
- **When a member leaves**, rent stops when their membership ends, and the storage team is told to clear and release
  any lockers they held (and, if they'd paid for a box, to look for a box labelled with their name).

### Pages
- **Members:** available lockers by location and size with their monthly rent; their own lockers and locker limit;
  whether they've paid for a box, with a button to pay if not.
- **Storage team and admins:** every locker with its status, holder, rent (overrides marked) and start date;
  filters by location and status; lockers with unpaid rent; lockers held by members who have left; members with
  the box flag, and flagged members who have left.

❓ Should the **storage team** also manage the locker inventory (locations, numbers, sizes, rents, reservations), or
admins only? (Built either way: it's ordinary admin permissions, given to the storage role or not.)

### Lockers: build status (2026-11-07)

Built (`apps/storage`):
- **Inventory** in the admin (Storage): locations, sizes, the **default rent for each size in each location** (in
  pounds), and lockers with their number, size, availability and advertised price. A locker with a rental history
  can't be deleted, only retired.
- **Both price overrides** and **availability** as described above. Changes go through one set of rules, which
  refuse what isn't allowed (e.g. reserving a rented locker, an override without a reason) and record every change,
  with who, when, old and new value and the reason, in a read-only "locker changes" log.
- **Limits:** the usual limit is a setting (02 §2.8, starting at 1); a trustee can give a member their own limit,
  with a reason, which shows on the member's admin page. Lowering a limit doesn't take lockers away.
- **Rentals in the admin:** the storage team or an admin gives an available locker to a current member (within their
  limit), changes what that member pays, and ends the rental.
- **Pages:** members see available lockers by location with their monthly rent, and their own lockers, rent and
  limit (`/lockers/`, linked from the account page). The admin's locker list shows status, holder, rent (overrides
  marked) and start date, filtered by status (including "rented by someone who has left"), location and size.

Built with billing (03 §3.6): **monthly rent charges** with the subscription (one Direct Debit for both), unpaid
rent (the storage team is emailed when a rent payment finally fails), rent charges not yet billed are cancelled when
a rental ends, and rentals end when a member leaves. Still waiting: **self-service renting** with a Direct Debit or
credit to pay from and the first (pro-rata) payment, and rent notice periods. Members ask the storage team to rent or give up a locker until then. The box
flag (above) is still to build.

Related policies: *member storage*, *3-week bins*.

## 6.3 Proposals and voting

**Built** (`apps/proposals`). Decided with the user 2026-11-08: current members vote; a quorum setting; a secret
ballot; new proposals announced by email and on Discord.

- **Write** (trustees, in the admin: Proposals and voting → Proposals): title, description (markdown), start date
  (today or later) and end date (on or after the start). Default window: today to three days from now. Voting is open
  from the start of the start date to the end of the end date (UK time). A proposal can be changed or deleted only
  until voting starts. The list page links to the admin for those allowed to add proposals.
- **Read**: members (anyone who sees the member directory: active, leaving, suspended or honorary, and admins), at
  `/proposals/`. The description's markdown is shown safely: raw HTML is shown as text and only http(s), mailto and
  on-site links are kept (`apps/core/markup.py`).
- **Vote** (current members: **active, leaving or honorary**, not banned, and not storage-only accounts with the
  `storage-box-user` role): **For (+1)**, **Neutral (0)**, **Against (−1)** or **Abstain**. Members can change their vote
  while voting is open. Votes are stored as integers (abstain: no value, flagged). The account page lists open
  proposals the member hasn't voted on.
- **Secret ballot**: a member sees their own vote and how many members have voted so far; everyone sees the totals
  once voting has closed. The admin shows who voted, never how.
- **Opening** (`update_proposals`, hourly): when voting opens, the number of members who can vote and the quorum
  setting are fixed on the proposal, every voter is emailed, and the bot posts in the Discord announcement channel
  (`DISCORD_ANNOUNCEMENT_CHANNEL_ID`).
- **Counting** (the same job, after the end date): for, neutral, against, abstentions. **Quorum** (Settings →
  Proposals, `proposal_quorum_percent`, default 0 = none): at least this percentage of the members who could vote
  when it opened must vote, abstentions included. A proposal **passes** if the quorum is met and more members voted
  for than against (`result = for − against`). The result is posted on Discord; a Discord failure is retried by the
  next run.

⚠️ Still open: the constitution's quorum rule (01 §1.7, Q2): set the quorum setting once it is known.

## 6.4 Expenses (reimbursement)

**Built** (`apps/expenses`, 2026-11-08).

- **Claim** (`/expenses/claim/`; members, for themselves only): category (legacy's list plus "other"), description,
  amount (in pence; at most the "largest expense" setting, £1,000 like legacy), date of purchase (not in the future),
  receipt (**required**: JPEG, PNG or PDF, up to 10 MB). Photos of receipts are re-saved, which removes the camera's
  metadata (e.g. where it was taken); PDFs are kept as sent. Receipts are private files with random names, served
  only through BBMS (`/expenses/<id>/receipt/`, sandboxed).
- Everyone who can approve expenses is told about each new claim, in BBMS and by email (legacy emailed a fixed
  trustees@ address).
- **Approve** (the "approve expenses" permission: admins, or a role given it): **credits the amount to the member's
  credit** (a `balance` payment from `reimbursement`, reference = the expense id), so the member can spend it or request
  a withdrawal. It can only happen once; **nobody decides on their own claim**.
- **Decline**: needs a reason, which the member is sent. Approved and declined are final: a mistake is put right
  with a correcting payment.
- The member is told in BBMS (legacy's keys `<id>-expense_approved` / `<id>-expense_declined`) and by email.
- A member can **withdraw** a claim still waiting; it's deleted with its receipt.
- **Lists** (`/expenses/`): members see only their own claims; those who can approve, and the `finance` role ("see
  all expenses" permission, given by the import), see everyone's, filtered by member and status, sorted, 25 a page.
  The admin (Expenses) is a read-only record; decisions are made on the expense's page, next to the receipt.

## 6.5 In-app notifications

- Created by the system (trusted status granted, expense approved/declined, device offline, …), deduplicated by a hash.
- `/notifications`: member's list; mark read/unread (own notifications only).

### Build status (2026-11-08)

Built in `apps/notifications`. Anything that wants to tell a member something calls one function, `notify(member,
message, kind=, key=, url=)`; `notify_role(role, ..., admins=True)` tells everyone in a role (and the admins:
superusers and the `admin` role). Choices made while building:
- **A notification** is plain text (escaped when shown), with an optional link to a page on this site (anything else is
  refused, so a notification can't send a member elsewhere). It's unread until the member reads it, or follows its link.
- **`key` stops repeats** (legacy's `hash`): a member can have only one notification with a given key, and a second
  is quietly dropped. Leave it out for something that can happen again.
- **`/notifications/`** lists the member's own, newest first (25 a page, "show only unread", "mark all as read"),
  with mark read or unread on each. The page header shows how many are unread. The admin has a read-only list.
- **Who is told what:**

  | When | Who is told | Key |
  |---|---|---|
  | Made trusted | the member (with the email) | `trusted_status`: once ever, as legacy |
  | A member changes their address | the `trustees` role and the admins | one per change |
  | An address change is approved or declined (by an admin, in the queue or on the member's page) | the member | none |
  | A trustee checks a proof of identity | the member, and by email (03 §3.1) | none |
  | A member uploads a photo | the admins | one per photo |
  | An admin adjusts a machine session's times | the member: old and new times and any refund or extra charge (05 §5.6) | none |
  | A machine's team is told (reported out of order or fixed, due for a service) | the machine's role; the admins if it has none | none |
  | A monitored device is quiet for over an hour (05 §5.5) | the `acs` role and the admins | one per outage |

- **Not built yet:** expenses (waiting for their app) and proposals; both will call `notify`. Notifications aren't
  emailed or sent to Discord; the emails in §6.6 are separate.
- The import brings in legacy's notifications (08 §8.2), read and unread, keeping their `hash` as the key.

## 6.6 Email

| Email | Trigger |
|---|---|
| Welcome | Status leaves `setting-up` |
| Payment warning | Status → `payment-warning` (legacy, see 03) |
| Suspended | Status → `suspended` |
| Left | Status → `left` (mentions any lockers to empty, and their box if they paid for one) |
| Subscription paid from credit failed — trying Direct Debit | Nightly billing |
| Subscription payment failed (insufficient credit) | Nightly billing |
| You are now a trusted member | Admin grants trusted |
| Proof of identity checked / please send again (with the trustee's note) | A trustee records an ID check (03 §3.1); new |
| Photo declined | Admin declines photo |
| New expense (to trustees) / expense approved / declined | Expenses |
| Feedback (to trustees) | Member submits feedback form |
| Withdrawal request (to trustees) | Member requests credit withdrawal — **change: don't email bank details** |
| Password reset | Forgotten password |
| Email confirmation | ⚠️ Verify when sent |
| Group notification | See below |

**Group email** (`/notification_email/create`): admins can email **all active members**; `laser` role holders can email
**all laser-trained members**; anyone with a role can send a test to themselves. ❓ Generalise to "any equipment team can
email its trained members"?

## 6.7 Activity

- `/activity`: door and equipment activity for a chosen day (previous/next day navigation), visible to **every member**.
  This is agreed policy (confirmed 2026-11-05).
- Offline entries uploaded by v2 devices appear at the time they happened, marked as offline (05 §5.4).
- Non-keyholders' door **check-ins** appear too (05 §5.2), so the page shows everyone who tapped in, not just keyholders.
- **Live occupancy** (added 2026-11-07): a "who's in now" view. A member counts as **in the space** from their entry
  tap (keyholder entry or check-in) until they tap the **exit reader** (05 §5.2), or until they've had no fob activity
  (taps or machine sessions) for a set time. Because tapping out is a courtesy, the count is a best estimate.
  The timeout is an **admin setting** (decided 2026-11-07; default 4 hours after their last fob activity, with
  everyone cleared at 4am). Logged-in members see the names (agreed policy: members may see who's in the space).
  Showing a **public count** on the website (e.g. "3 people in the space", never names) is an **admin on/off
  setting** (decided 2026-11-07).
- Dropped: the "reveal door key" button (the emergency PIN is defunct) and the Pusher real-time feed.

## 6.8 Resources and policies

- `/resources`: index of policy documents; `/resources/policy/{name}` renders a markdown policy (public).
- Current policies: rules, code of conduct, grievance procedure, member agreement, member storage, 3-week bins,
  permaloan, trusted member.
- **New:** keep policies as markdown files in the repo (reviewed via pull request), or make them editable by admins ❓.

## 6.9 Feedback

A member-only form whose comments are emailed to the trustees.

## 6.10 Admin tools

- **Members list** (`/account`, admin): paginated, sortable, toggle "show left members".
- **Roles** (admin): create/edit roles; add/remove members.
- **Key fobs** (admin today; a permission a role can be given): issue free to an active member, mark lost, found
  again.
- **Physical keys** (admin): record the key deposit and the key issued and returned.
- **Trusted-status review list** (admin): members who meet the attendance threshold but aren't trusted (05 §5.10).
- **Log viewer**: dropped; replaced by hosted logging and error tracking (01 §1.8).
- **Settings** (admin only): prices, limits, grace periods and names listed in 02 §2.8.

In the new system, most of these should be the framework's **admin interface** rather than hand-built pages.

## 6.11 Authentication

- Login with email + password; "remember me".
- Forgotten password → emailed reset link.
- Banned users can log in but are refused on every protected page. **New:** refuse at login with a clear message.
- **New:** rate-limit login and password reset; optional 2FA for admins.

- BBMS is also the **single sign-on provider for Moodle** ("Log in with BBMS", 6.12).

## 6.12 Moodle (online training) integration

Added 2026-11-07. The space runs **Moodle** on a separate server, with online training courses for some of the more
complex or fragile machines.

### Today
Moodle's only link to BBMS is its **"External database" login**: it reads BBMS's `users` table directly over MySQL
(a `moodle` MySQL account and a firewall rule for Moodle's server, <moodle-server-ip>), so members use the same email and
password on both. Nothing in the BBMS code knows about Moodle. This **can't continue** with the new system (different
database, different password format, no open database port), so Moodle's login must change at cutover (08).

### Single sign-on: "Log in with BBMS" (decided 2026-11-07)
- BBMS acts as an **OpenID Connect provider** (Django: `django-oauth-toolkit` with OIDC). Moodle uses its built-in
  **OAuth 2** login with a custom "BBMS" service, shown as a "Log in with BBMS" button. Passwords live only in BBMS.
- Claims sent to Moodle: `sub` (the member's BBMS id, which never changes), `email`, `email_verified`,
  `given_name`, `family_name`. Moodle maps `sub` to the user's **ID number** so BBMS can find them later.
- Moodle's own sign-up and password login are switched off (admins keep a local fallback login).
- ❓ Who may log in to Moodle? Suggested: any BBMS account that isn't banned, including members still setting up,
  so they can do online training before or soon after joining.
- ⚠️ Verify on the space's Moodle version: OAuth 2 login (Moodle 3.3+), mapping a claim to ID number, and how
  existing accounts (created by the database login) are switched to OAuth 2 and linked. Rehearse on a copy of Moodle.

### Linking equipment to courses
- Each piece of equipment can have a **Moodle course** (its course id). The equipment page and the member's training
  list show a link straight to that course on Moodle, and whether the member has completed it.
- Machines that share an induction category (5.8) normally share a course.
- The Moodle site address is a setting.

### Checking completion
- BBMS asks Moodle through its **web services API** (`core_completion_get_course_completion_status` for a user and
  course; `core_user_get_users_by_field` to find the Moodle user by ID number, falling back to email). BBMS uses a
  dedicated Moodle web-service account and token with the minimum permissions; the token is a BBMS secret.
- ⚠️ Each linked course must have **course completion** set up in Moodle.
- BBMS **stores** each completion (member, course, completed at, last checked). **Machine access and sign-off use
  the stored record only; BBMS never calls Moodle while a fob is being checked**, so a Moodle outage can't block
  machines.
- When BBMS checks:
    1. when a member presses **"I've finished the course, check now"** on the equipment page;
    2. when a trainer opens their sign-off list (5.8);
    3. every **15 minutes**, a scheduled job checks members waiting for sign-off on machines with a course.
- Later, optionally: a Moodle webhook plugin could notify BBMS the moment a course is completed.
- Once recorded, a completion stays until an admin clears it. ❓ Should online training expire and need redoing
  (e.g. every 2 years) for some machines? Suggested: no, not at first.
- ❓ Members already signed off on a machine before its course was added: suggested, they **keep access** without the
  course, unless an admin ticks "require the course for existing users" on that equipment.

## 6.13 Membership statistics (requested by a trustee, 2026-11-07)

A trustee asked for a member total and a picture of its history, to see the trend.

- **Now:** totals by status (active, leaving, honorary, suspended, setting up), plus joiners and leavers this month.
- **Trend:** a chart of members month by month, with joiners and leavers per month.
- **How history is kept:** the new system records **every status change** with its date, and takes a **daily
  snapshot** of the totals, so the trend is exact from cutover onwards.
- **Before cutover:** the legacy system doesn't record status history, so the trend is **reconstructed** from paid
  subscription charges (members who paid each month), join dates and the last payment of members who left. It's
  labelled as estimated on the chart.
- The legacy stats page (members by payment method, spread of subscription amounts) stays.
- ❓ Who can see membership statistics? Suggested: totals and the trend for every member (as the stats page today);
  money figures (4.9) for finance and admins only.
- Charts are drawn in the browser with a small charting library; no extra service is needed.

## 6.14 Discord (requested 2026-11-08)

Discord is now the space's **main way of talking**, and every new member must join the server. Topic channels there
(3D printing, audio, laser, kiln, metalwork, woodwork, …, mostly private) mirror the old BBMS groups; members choose
them on Discord itself (Channels & Roles). The server has a public welcome channel and a channel only former
members can see.

Decided with the requester (2026-11-08):
- **Linked accounts.** A member links their Discord account from BBMS (Discord's OAuth2, scopes `identify` and
  `guilds.join`); BBMS adds them to the server if they aren't on it. One BBMS account per Discord account.
- **It blocks activation.** A member who is setting up can't set up their payment, so can't become active, until
  they've linked Discord and are on the server. Trustees can tick "Discord not needed" for someone, sparingly.
  Members who joined before this aren't held up.
- **Roles follow membership.** BBMS gives the **member** role while someone is a member (active, leaving,
  suspended, honorary) and takes it away when they leave, giving the **former member** role instead (it opens the
  left-members channel). Topic roles are left to Discord.
- **Nickname.** Set to the member's real name when they link, so members know who's who. After that it's theirs to
  change (some members are known by another name at the space).
- **Welcome message.** When BBMS adds someone to the server, its bot posts a welcome for them in the welcome
  channel. The text is a setting (Settings → Discord) with `{mention}`, `{name}`, `{first_name}` and `{space}`; only
  the new member is notified (no @everyone or role pings from the text).

### Build status (2026-11-08)

Built in `apps/discord`:
- `/account/discord/`: link, rejoin the server (if they've left it) or unlink. The account page shows "Join our
  Discord server" as the first next step, and the payment page waits for it.
- The roles are updated when a member's status changes (after it's saved), and the nightly `sync_discord` job puts
  right anything else: an admin's edit, a role changed by hand, a call that failed, a welcome that couldn't be
  posted. It also notices members who have left the server. Admins can run it for chosen members from the members
  list ("Update their Discord roles now").
- The groups page links each group to its topic channels: a group can have several (e.g. a woodwork group with
  #woodwork, #wood-lathe and #tablesaw), each with its name and, to make it a link, its channel id (Users → Groups).
- Members see each other's Discord username on member pages.
- BBMS keeps the member's Discord id and username, never their Discord token (used once, to add them).
- Configuration (environment): `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` (the application's OAuth2 page; add
  `https://<site>/account/discord/done/` as a redirect), `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
  `DISCORD_MEMBER_ROLE_ID`, `DISCORD_FORMER_MEMBER_ROLE_ID`, `DISCORD_WELCOME_CHANNEL_ID`, and
  `DISCORD_ANNOUNCEMENT_CHANNEL_ID` (new proposals and their results, §6.3). The bot needs "Create
  Invite", "Manage Roles", "Manage Nicknames" and "Send Messages" in those two channels, with its role above the
  member and former-member roles. Without the settings, development and tests use a fake (`/dev/discord/`), and a
  live site doesn't ask members to link Discord (a deploy check warns).
- ⚠️ At cutover: existing members aren't linked. They can link from their account page; the trustees may want to ask
  them to, so the member / former-member roles become BBMS's job.
