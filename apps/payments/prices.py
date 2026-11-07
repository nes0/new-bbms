"""Fixed prices and credit limits, in pence (spec 02 §2.8, 04 §4.1)."""

# TODO(settings app): these become admin-editable settings (02 §2.8).
KEY_DEPOSIT_PENCE = 1500
BOX_FEE_PENCE = 1500

# Paying from credit may not take it below this (04 §4.1). Equipment fees have their own rule (apps.equipment.rules).
MINIMUM_CREDIT_PENCE = 0
