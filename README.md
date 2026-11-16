# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/nes0/new-bbms/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                                            |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|---------------------------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| apps/access/activity.py                                         |       68 |        0 |       20 |        1 |     99% | 122-\>124 |
| apps/access/admin.py                                            |      292 |       30 |       54 |        9 |     88% |87-88, 123-124, 135, 142-143, 152, 159-161, 165-\>169, 256, 259-261, 265, 269, 273, 278, 282, 286, 290-294, 298-300, 301-\>303, 355, 396, 434-\>441 |
| apps/access/api\_v2.py                                          |      319 |       17 |      110 |       10 |     93% |70-72, 75-76, 78, 97, 173, 199-\>201, 228-\>230, 337, 345-346, 371-372, 378-379, 384, 405 |
| apps/access/attendance.py                                       |      141 |        0 |       40 |        3 |     98% |67-\>70, 123-\>exit, 134-\>137 |
| apps/access/decisions.py                                        |       92 |        3 |       40 |        3 |     95% |34, 58-\>64, 93, 133 |
| apps/access/firmware.py                                         |       70 |        4 |       30 |        5 |     91% |34, 39, 48, 62, 90-\>exit |
| apps/access/fobs.py                                             |      112 |        2 |       44 |        3 |     97% |152, 187, 209-\>exit |
| apps/access/forms.py                                            |       37 |        2 |        8 |        3 |     89% |35, 46, 61-\>exit |
| apps/access/management/commands/check\_devices.py               |        6 |        6 |        0 |        0 |      0% |      1-10 |
| apps/access/models.py                                           |      154 |        3 |       20 |        2 |     97% |101, 102-\>exit, 181, 217 |
| apps/access/screen.py                                           |       56 |        2 |       14 |        1 |     96% |32-33, 95-\>97 |
| apps/billing/admin.py                                           |       89 |       10 |        4 |        1 |     88% |56, 74-75, 84-85, 89-92, 118, 128-129 |
| apps/billing/billing.py                                         |      230 |       12 |       78 |        8 |     94% |141-142, 148, 155-156, 260-261, 292, 295, 319, 332, 383 |
| apps/billing/checks.py                                          |       19 |        1 |        8 |        3 |     85% |10-\>17, 17-\>25, 26 |
| apps/billing/emails.py                                          |       57 |        6 |       12 |        3 |     87% |72-73, 84-85, 87-88 |
| apps/billing/gocardless.py                                      |      214 |       48 |       44 |        4 |     75% |45-47, 50-58, 62-81, 85-88, 91-103, 106, 109, 113-135, 250, 258, 298-305, 312-\>310, 329, 332, 335, 338, 363 |
| apps/billing/jobs.py                                            |       49 |        3 |       12 |        1 |     93% |50-51, 59-\>61, 72 |
| apps/billing/management/commands/process\_gocardless\_events.py |        9 |        9 |        0 |        0 |      0% |      1-14 |
| apps/billing/membership.py                                      |      173 |        8 |       60 |        7 |     94% |86-\>88, 92, 103-104, 108-110, 119, 158-\>exit, 180, 219 |
| apps/billing/models.py                                          |       69 |        2 |        0 |        0 |     97% |  103, 125 |
| apps/billing/views.py                                           |      179 |       15 |       46 |       12 |     88% |89-90, 111, 113, 116-117, 132, 141-142, 154, 182, 183-\>195, 207-208, 219-\>221, 235, 238, 257-\>274 |
| apps/billing/webhooks.py                                        |      174 |       11 |       64 |       13 |     90% |49, 56, 60-\>62, 62-\>64, 70-73, 136, 158, 168, 171, 186-\>185, 192, 218-\>211, 229-\>242 |
| apps/core/admin.py                                              |       14 |        1 |        0 |        0 |     93% |        16 |
| apps/core/checks.py                                             |       14 |        0 |        8 |        1 |     95% |   27-\>38 |
| apps/core/management/commands/seed\_demo.py                     |      244 |        1 |       88 |        3 |     99% |404, 529-\>532, 777-\>780 |
| apps/core/models.py                                             |       12 |        1 |        0 |        0 |     92% |        20 |
| apps/core/policies.py                                           |       13 |        1 |        2 |        1 |     87% |        20 |
| apps/core/views.py                                              |       75 |        2 |       16 |        0 |     98% |     46-47 |
| apps/discord/admin.py                                           |       15 |        3 |        2 |        0 |     82% |     17-19 |
| apps/discord/checks.py                                          |        8 |        1 |        2 |        1 |     80% |        11 |
| apps/discord/client.py                                          |      197 |       25 |       44 |        9 |     84% |93, 128-129, 131-132, 135-139, 187, 225-229, 233-234, 258, 292-\>294, 297-\>299, 304-308, 319, 325 |
| apps/discord/linking.py                                         |      129 |       12 |       38 |        5 |     90% |90, 141-142, 166, 171-\>173, 185, 198-199, 210-213, 217 |
| apps/discord/management/commands/sync\_discord.py               |       10 |        2 |        2 |        1 |     75% |     11-12 |
| apps/discord/views.py                                           |       62 |        2 |       10 |        1 |     96% |     41-42 |
| apps/equipment/admin.py                                         |      283 |       16 |       80 |       16 |     90% |66-\>68, 72-73, 270-\>282, 275, 313-317, 378, 388-\>392, 421, 432, 433-\>443, 442, 474-\>477, 479, 481-\>485, 483-484, 486-\>exit, 489-\>491, 495 |
| apps/equipment/api\_v2.py                                       |      185 |        5 |       66 |        6 |     96% |202, 208-\>214, 259, 303, 305, 308 |
| apps/equipment/device\_api.py                                   |       40 |        2 |       16 |        2 |     93% |    41, 48 |
| apps/equipment/lifetime.py                                      |       46 |        1 |        4 |        2 |     94% |46, 57-\>72 |
| apps/equipment/management/commands/tidy\_equipment\_sessions.py |        7 |        7 |        0 |        0 |      0% |      7-17 |
| apps/equipment/models.py                                        |      233 |        6 |       12 |        0 |     98% |243, 331, 349, 369, 385, 407 |
| apps/equipment/pages.py                                         |      152 |        2 |       66 |        3 |     98% |145, 215-\>224, 271 |
| apps/equipment/photos.py                                        |       45 |        4 |        8 |        4 |     85% |38, 42, 44, 48, 64-\>67 |
| apps/equipment/rules.py                                         |      193 |        6 |       72 |        6 |     95% |159, 208, 210, 218, 223, 266 |
| apps/equipment/views.py                                         |      106 |        1 |       24 |        1 |     98% |       134 |
| apps/expenses/claims.py                                         |      115 |        2 |       26 |        3 |     96% |88, 99, 158-\>exit |
| apps/expenses/views.py                                          |      105 |        3 |       24 |        3 |     95% |46, 124, 148 |
| apps/finance/management/commands/import\_payouts.py             |       34 |       10 |       12 |        4 |     61% |36-39, 42-45, 49, 51 |
| apps/finance/models.py                                          |      108 |        5 |        0 |        0 |     95% |34, 103, 125, 156, 186 |
| apps/finance/money.py                                           |      112 |        3 |       30 |        2 |     96% |135-136, 191 |
| apps/finance/payouts.py                                         |       95 |        4 |       30 |        3 |     94% |58, 118-\>124, 134, 173-174 |
| apps/finance/statements.py                                      |      277 |       12 |      108 |        9 |     95% |111-112, 133, 139, 305, 361, 389-\>392, 410, 415, 418-\>427, 451-453, 480 |
| apps/finance/views.py                                           |      363 |       28 |       98 |       17 |     89% |115, 194, 202, 204, 206, 354, 368-\>370, 371, 374, 376-377, 402, 451-\>454, 491-\>493, 494, 497, 518-\>532, 525, 545-566 |
| apps/group\_email/admin.py                                      |       36 |        1 |        2 |        1 |     95% |        27 |
| apps/group\_email/audiences.py                                  |       87 |        5 |       42 |        4 |     93% |77, 79, 81, 135, 147 |
| apps/group\_email/models.py                                     |       67 |        6 |       20 |        5 |     87% |70, 77, 85, 89, 93, 116 |
| apps/group\_email/sending.py                                    |       49 |        9 |        8 |        2 |     81% |46, 49-51, 73-75, 78-79 |
| apps/group\_email/views.py                                      |       33 |        2 |       14 |        2 |     91% |    37, 41 |
| apps/legacy/importer.py                                         |      869 |       75 |      354 |       65 |     89% |131, 180-\>exit, 269-270, 278-279, 285-286, 301, 339-\>338, 342-343, 355-\>354, 363-\>exit, 378-379, 421-422, 439-440, 467, 468-\>472, 479-\>481, 500-\>exit, 510, 515-\>520, 579, 599-\>exit, 669-670, 679-\>683, 693-\>702, 713, 748-749, 751-752, 759, 764-765, 792-793, 797-798, 836, 844, 879-880, 882, 889, 902-904, 911-\>exit, 921-922, 937-938, 965-\>969, 984-\>988, 1014-1015, 1018-1019, 1044-1046, 1049-\>1053, 1053-\>1055, 1055-\>exit, 1085-1086, 1098-1099, 1119-1121, 1126-\>1131, 1132, 1136-\>1141, 1141-\>1143, 1144, 1182, 1204, 1219-1221, 1224-\>1226, 1226-\>exit, 1257-\>1259, 1273-1274, 1276-1277, 1305-\>1293, 1310-\>exit, 1339-1340, 1353, 1374-\>1381, 1401 |
| apps/legacy/management/commands/import\_legacy.py               |       41 |       41 |       12 |        0 |      0% |     17-79 |
| apps/legacy/photos.py                                           |       58 |        6 |       14 |        3 |     88% |42-44, 66, 72, 84 |
| apps/legacy/source.py                                           |       25 |       14 |        0 |        0 |     44% |20, 27-44, 48-50, 53-55 |
| apps/members/account\_forms.py                                  |       80 |        1 |       20 |        1 |     98% |        34 |
| apps/members/account\_views.py                                  |      162 |        4 |       36 |        1 |     97% |146, 209-211 |
| apps/members/admin.py                                           |      283 |        8 |       76 |        6 |     96% |207, 319, 343, 380, 466-\>468, 481-482, 495-496 |
| apps/members/checks.py                                          |       22 |        1 |        6 |        1 |     93% |        45 |
| apps/members/email\_confirmation.py                             |       60 |        2 |       12 |        3 |     93% |81, 93, 98-\>100 |
| apps/members/forms.py                                           |       98 |        0 |       22 |        1 |     99% |   81-\>92 |
| apps/members/hashers.py                                         |       31 |        7 |        0 |        0 |     77% |32, 36-37, 53-54, 58, 61 |
| apps/members/management/commands/rotate\_id\_document\_key.py   |       12 |        1 |        2 |        1 |     86% |        17 |
| apps/members/models.py                                          |      258 |        6 |       10 |        1 |     97% |23, 278, 414, 438, 454, 468 |
| apps/members/test\_induction.py                                 |      245 |        1 |       32 |        1 |     99% |       203 |
| apps/moodle/checks.py                                           |       17 |        2 |        6 |        0 |     91% |     29-30 |
| apps/moodle/client.py                                           |       98 |        2 |       14 |        1 |     97% |  137, 144 |
| apps/moodle/completion.py                                       |       73 |        0 |       26 |        1 |     99% |  102-\>97 |
| apps/moodle/models.py                                           |        9 |        1 |        0 |        0 |     89% |        18 |
| apps/moodle/oidc.py                                             |       18 |        1 |        6 |        1 |     92% |        42 |
| apps/notifications/admin.py                                     |       16 |        1 |        0 |        0 |     94% |        18 |
| apps/notifications/models.py                                    |       19 |        1 |        0 |        0 |     95% |        33 |
| apps/notifications/views.py                                     |       43 |        0 |        6 |        1 |     98% |   40-\>43 |
| apps/payments/admin.py                                          |      141 |       13 |       16 |        3 |     89% |44, 68, 77-78, 96-\>100, 113, 117, 130-131, 137-139, 164, 175 |
| apps/payments/cards.py                                          |      165 |       58 |       42 |        7 |     60% |49, 59-62, 65-69, 72-87, 90-104, 107-111, 116-141, 155-158, 182-186, 190-191, 224, 266-272, 276-277, 281-\>279, 300, 309 |
| apps/payments/checkout.py                                       |      167 |       15 |       66 |        9 |     90% |54-\>52, 76, 109, 143, 157-158, 221, 229-230, 237, 283-287, 298 |
| apps/payments/checks.py                                         |       15 |        1 |        6 |        3 |     81% |10-\>14, 14-\>22, 28 |
| apps/payments/forms.py                                          |      105 |        5 |       36 |        6 |     92% |70, 75, 77, 80, 104, 150-\>152 |
| apps/payments/models.py                                         |      147 |        5 |        6 |        1 |     96% |111, 117, 155, 193, 211 |
| apps/payments/paypal.py                                         |       65 |        2 |        8 |        0 |     97% |   143-144 |
| apps/payments/paypal\_ipn.py                                    |      198 |       15 |       94 |       18 |     89% |48-49, 94, 102, 122-\>124, 127, 145, 152-\>156, 159, 168, 169-\>171, 200, 202, 205, 213-\>215, 278, 296-\>298, 315, 317, 321 |
| apps/payments/services.py                                       |      140 |        5 |       40 |        6 |     94% |74, 119, 171, 190, 252, 254-\>261, 258-\>261 |
| apps/payments/views.py                                          |      260 |       25 |       58 |        6 |     89% |167-168, 186-187, 199-200, 217-218, 223-224, 238, 263-264, 275-285, 290-291, 346, 367, 417-420 |
| apps/profiles/admin.py                                          |      115 |        8 |       16 |        2 |     91% |46, 59, 82-86, 121 |
| apps/profiles/forms.py                                          |       53 |        1 |        8 |        1 |     97% |        90 |
| apps/profiles/models.py                                         |       55 |        3 |       10 |        1 |     94% |44, 61, 86 |
| apps/profiles/photos.py                                         |      100 |        3 |       22 |        4 |     94% |45, 51, 98, 115-\>exit |
| apps/profiles/views.py                                          |      153 |        9 |       54 |        7 |     92% |55-56, 91, 93, 97, 99, 119, 240, 258 |
| apps/proposals/admin.py                                         |       64 |        1 |       14 |        2 |     96% |83-\>85, 94 |
| apps/proposals/models.py                                        |       86 |        1 |       10 |        0 |     99% |       140 |
| apps/proposals/voting.py                                        |      114 |        6 |       42 |        6 |     92% |50, 102, 109-\>115, 113-114, 130, 156, 180-\>179 |
| apps/signin/admin.py                                            |       29 |        1 |        2 |        0 |     97% |        20 |
| apps/signin/middleware.py                                       |       34 |        1 |       16 |        1 |     96% |        40 |
| apps/signin/models.py                                           |       31 |        4 |        0 |        0 |     87% |27, 45, 49, 60 |
| apps/signin/throttle.py                                         |       34 |        0 |        8 |        1 |     98% | 68-\>exit |
| apps/signin/two\_factor.py                                      |       61 |        3 |       14 |        3 |     92% |30, 59, 73 |
| apps/signin/views.py                                            |      125 |        3 |       36 |        4 |     96% |35, 79, 81, 84-\>110 |
| apps/site\_settings/admin.py                                    |       77 |        2 |       16 |        2 |     96% |    53, 96 |
| apps/site\_settings/models.py                                   |       91 |        2 |        8 |        0 |     98% |  346, 371 |
| apps/storage/admin.py                                           |      283 |       12 |       64 |        9 |     93% |83-\>exit, 86, 92, 115, 117, 119, 280-\>286, 283, 306-307, 359-360, 390-391 |
| apps/storage/models.py                                          |      104 |        1 |        0 |        0 |     99% |       203 |
| apps/storage/rules.py                                           |      255 |        8 |       86 |        9 |     95% |55, 119, 162, 221, 355, 395, 441, 443, 452-\>438 |
| apps/storage/views.py                                           |       59 |        4 |       10 |        3 |     90% |63-\>67, 66, 96-97, 101 |
| apps/vouching/models.py                                         |       39 |        3 |        0 |        0 |     92% |35, 57, 68 |
| apps/vouching/vouching.py                                       |      115 |        1 |       34 |        1 |     99% |       101 |
| bbms/asgi.py                                                    |        4 |        4 |        0 |        0 |      0% |     10-16 |
| bbms/settings.py                                                |       90 |        3 |        6 |        3 |     94% |41, 149, 208 |
| bbms/wsgi.py                                                    |        4 |        4 |        0 |        0 |      0% |     10-16 |
| **TOTAL**                                                       | **15292** |  **792** | **3440** |  **415** | **93%** |           |

142 files skipped due to complete coverage.


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/nes0/new-bbms/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/nes0/new-bbms/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nes0/new-bbms/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/nes0/new-bbms/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fnes0%2Fnew-bbms%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/nes0/new-bbms/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.