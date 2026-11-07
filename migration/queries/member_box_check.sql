-- Member Box check (read-only). Run on the production BBMS database (MySQL 5.7):
--   mysql -t bbms < member_box_check.sql
-- Returns only counts and settings: no names, emails or other personal data.
-- Purpose: confirm the "Member Box" equipment item and size the "box paid" flag migration (spec 08 §8.2).

-- 1. The equipment item(s) named "Member Box" (case-insensitive, ignoring stray spaces).
SELECT id, name, slug, induction_category, requires_induction, access_fee AS access_fee_pounds,
       archive, working, created_at
FROM equipment
WHERE TRIM(name) = 'Member Box';

-- 2. Any OTHER equipment sharing its induction category (would also count as a box payment).
SELECT e2.id, e2.name, e2.slug, e2.induction_category
FROM equipment e
JOIN equipment e2 ON e2.induction_category = e.induction_category AND e2.id <> e.id
WHERE TRIM(e.name) = 'Member Box';

-- 3. Inductions for that category, by induction paid flag and the linked payment's status, source and amount.
SELECT i.paid                          AS induction_paid,
       IFNULL(p.status, '(no payment)') AS payment_status,
       IFNULL(p.source, '-')            AS payment_source,
       p.amount                         AS amount_pounds,
       COUNT(*)                         AS inductions,
       COUNT(DISTINCT i.user_id)        AS members,
       MIN(i.created_at)                AS earliest,
       MAX(i.created_at)                AS latest
FROM inductions i
LEFT JOIN payments p ON p.id = i.payment_id
WHERE i.`key` IN (SELECT induction_category FROM equipment WHERE TRIM(name) = 'Member Box')
GROUP BY i.paid, IFNULL(p.status, '(no payment)'), IFNULL(p.source, '-'), p.amount
ORDER BY inductions DESC;

-- 4. Members who would get the "box paid" flag (paid induction AND payment status 'paid'), by membership status.
SELECT u.status, COUNT(DISTINCT u.id) AS members
FROM inductions i
JOIN payments p ON p.id = i.payment_id AND p.status = 'paid'
JOIN users u    ON u.id = i.user_id
WHERE i.paid = 1
  AND i.`key` IN (SELECT induction_category FROM equipment WHERE TRIM(name) = 'Member Box')
GROUP BY u.status
ORDER BY members DESC;

-- 5. Members with a "paid" induction whose payment is NOT paid (would be listed for the storage team to check).
SELECT IFNULL(p.status, '(no payment)') AS payment_status, COUNT(DISTINCT i.user_id) AS members
FROM inductions i
LEFT JOIN payments p ON p.id = i.payment_id
WHERE i.paid = 1
  AND (p.id IS NULL OR p.status <> 'paid')
  AND i.`key` IN (SELECT induction_category FROM equipment WHERE TRIM(name) = 'Member Box')
GROUP BY IFNULL(p.status, '(no payment)');

-- 6. Members with more than one paid Member Box induction (paid twice?).
SELECT COUNT(*) AS members_with_multiple
FROM (
  SELECT i.user_id
  FROM inductions i
  WHERE i.paid = 1
    AND i.`key` IN (SELECT induction_category FROM equipment WHERE TRIM(name) = 'Member Box')
  GROUP BY i.user_id
  HAVING COUNT(*) > 1
) t;

-- 7. Induction payments referencing the category that have NO induction record (e.g. cancelled/refunded),
--    by status and reason. Cancelled inductions are refunded by changing the payment reason to 'balance'.
SELECT p.reason, p.status, COUNT(*) AS payments
FROM payments p
LEFT JOIN inductions i ON i.payment_id = p.id
WHERE p.reference IN (SELECT induction_category FROM equipment WHERE TRIM(name) = 'Member Box')
  AND i.id IS NULL
GROUP BY p.reason, p.status;
