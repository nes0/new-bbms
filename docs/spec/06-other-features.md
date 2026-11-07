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
- Each locker has a **size**, from a managed list of locker sizes, each with a **default monthly rent**.
- Admins add, move, renumber and resize lockers. A locker can't be removed while it's rented.

- **Status** of each locker: **available**, **rented**, **reserved for trustees**, or **out of use**. Members can
  only rent available lockers. Admins can mark a locker reserved (with a note of what for) or out of use (e.g.
  broken), but only while it isn't rented.
- **Limit:** members may rent up to a **maximum number of lockers** (a setting; ❓ suggested 1). Admins can set a
  **different limit for an individual member** at their discretion, with a reason. The override is audit-logged and
  shown on the member's admin page.
- **Rent:** charged **monthly** while the member holds the locker. Price = the locker's price override if set,
  otherwise its size's default monthly rent.
    - **Price override:** an admin can set an individual locker's rent in exceptional circumstances (including £0),
      with a reason. It's audit-logged and **cleared automatically when that rental ends**, so the next renter pays
      the size's default.
    - ❓ When an admin changes a size's default rent, should existing renters pay the new rent? Suggested: yes, from
      the first charge at least 30 days after they've been emailed about it.
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
  refund for the rest of the month already paid. The price override is cleared and the locker becomes available.
- **When a member leaves**, rent stops when their membership ends, and the storage team is told to clear and release
  any lockers they held (and, if they'd paid for a box, to look for a box labelled with their name).

### Pages
- **Members:** available lockers by location and size with their monthly rent; their own lockers and locker limit;
  whether they've paid for a box, with a button to pay if not.
- **Storage team and admins:** every locker with its status, holder, rent (overrides marked) and start date;
  filters by location and status; lockers with unpaid rent; lockers held by members who have left; members with
  the box flag, and flagged members who have left.

❓ Should the **storage team** also manage the locker inventory (locations, numbers, sizes, rents, reservations), or
admins only?

Related policies: *member storage*, *3-week bins*.

## 6.3 Proposals and voting

- **Create/edit** (admin): title, description (markdown), start date (today or later), end date (after today).
  Default window: today + 3 days. Editable only until voting starts.
- **Vote** (any logged-in member): **For (+1)**, **Neutral (0)**, **Against (−1)** or **Abstain**. Members can change their vote
  while the proposal is open. Members with the `storage-box-user` role cannot vote.
- **Results** (hourly job, after end date): counts for/neutral/against/abstain, `votes_cast`, `result = for − against`.
  The proposal page lists how each member voted.

⚠️ Legacy issues to decide on:
- Any logged-in user can vote, **including `setting-up`, `left` and `suspended` accounts**. ❓ Restrict to current members.
- `quorum` is always recorded as true. ❓ Define the quorum rule (from the constitution).
- Vote values are stored as strings `+1`/`0`/`−1`; store as integers.

## 6.4 Expenses (reimbursement)

- **Submit** (member, for themselves only): category, description, amount (**pence**, max £1,000), date of expense
  (not in the future), receipt (**required**; JPEG, PNG or PDF).
- Trustees are emailed about each new expense.
- **Approve/decline** (admin): sets `approved_by_user`, sends an in-app notification and an email.
  **Approval credits the amount to the member's credit** (`balance` payment from `reimbursement`), so the member can
  spend it or request a withdrawal.
- Expense list: filter by member, sort, paginate.

⚠️ Legacy lets any member list **all** members' expenses (and receipts) via the JSON endpoint. **New:** members see only
their own; admins/finance see all.

## 6.5 In-app notifications

- Created by the system (trusted status granted, expense approved/declined, device offline, …), deduplicated by a hash.
- `/notifications`: member's list; mark read/unread (own notifications only).

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
