"""
Cycle Count Audit Ledger Constants & Domain Taxonomy
====================================================
Department: 01_DEPT_Inventory / 00_JOB_StockAccuracy
File: audit_constants.py

Standardized enumeration for audit list types, selection methods, and data origin.
Guarantees consistent persistence in AUDIT_ASSIGNMENT and unbiased ML evaluation.
"""

# ------------------------------------------------------------------------------
# 1. Audit List Types (Sheet 1 / AUDIT_ASSIGNMENT.list_type)
# ------------------------------------------------------------------------------
LIST_TYPE_RAPID = "RAPID_FLOOR"          # High-risk top 15 priority floor targets
LIST_TYPE_EXTENDED = "EXTENDED_SECONDARY" # Secondary 22 suspected variance bins
LIST_TYPE_RANDOM = "RANDOM"              # 3 random validation bins for IPW exploration
LIST_TYPE_UNSELECTED = "UNSELECTED"      # All remaining warehouse floor bins
LIST_TYPE_FORCED_F_CHECK = "FORCED_F_CHECK"  # Forced top-pinned SAP GR missing check (#199)

# Backward compatibility alias
LIST_TYPE_RANDOM_VALIDATION = "RANDOM"

# ------------------------------------------------------------------------------
# 2. Selection Methods (AUDIT_ASSIGNMENT.selection_method)
# ------------------------------------------------------------------------------
SELECTION_RULE_SCORE = "RULE_SCORE"      # Selected via domain rule heuristic / ML rank
SELECTION_RANDOM_SAMPLE = "RANDOM_SAMPLE" # Selected via reproducible daily seeded random sampling
SELECTION_FORCED_WEEKLY_GR_CHECK = "FORCED_WEEKLY_GR_CHECK" # Forced Weekly top-pinned SAP GR verification (#199)

# ------------------------------------------------------------------------------
# 3. Data Origin (AUDIT_RESULT.data_origin)
# ------------------------------------------------------------------------------
DATA_ORIGIN_PHYSICAL = "PHYSICAL"        # Genuine physical warehouse floor count
DATA_ORIGIN_SYNTHETIC = "SYNTHETIC_SEED" # Synthetic seed data for pipeline testing
DATA_ORIGIN_TEST = "TEST"                # Isolated unit test fixture data

# ------------------------------------------------------------------------------
# 4. Audit Origin (AUDIT_RESULT.audit_origin)
# ------------------------------------------------------------------------------
AUDIT_ORIGIN_PHYSICAL = "PHYSICAL_CYCLE_COUNT"      # Counted during physical floor audit
AUDIT_ORIGIN_WMS_ADMIN = "WMS_ADMIN_ADJUSTMENT"    # Administrative WMS stock adjustment
