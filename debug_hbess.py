import pandas as pd
from db import query

pd.set_option("display.max_rows", 60)
pd.set_option("display.width", 160)

# 1. Registration: what DUIDs, types, capacities exist for HBESS
reg = query("""
    SELECT DUID, DISPATCHTYPE, MAXSTORAGECAPACITY, REGISTEREDCAPACITY, LASTCHANGED
    FROM TESTER.DUDETAIL
    WHERE DUID LIKE 'HBESS%'
    ORDER BY DUID, LASTCHANGED
""")
reg.columns = [c.upper() for c in reg.columns]
print("\n=== DUDETAIL (HBESS%) ===")
print(reg.to_string(index=False))

# 2. Distinct dispatch types across ALL batteries (to confirm the values)
types = query("""
    SELECT DISTINCT DISPATCHTYPE
    FROM TESTER.DUDETAIL
    WHERE MAXSTORAGECAPACITY > 0
""")
types.columns = [c.upper() for c in types.columns]
print("\n=== Distinct DISPATCHTYPE for storage units ===")
print(types.to_string(index=False))

# 3. Raw SCADA, last 3 hours, for every HBESS DUID — shows sign convention live
scada = query("""
    SELECT SETTLEMENTDATE, DUID, SCADAVALUE
    FROM TESTER.DISPATCH_UNIT_SCADA
    WHERE DUID LIKE 'HBESS%'
      AND SETTLEMENTDATE >= SYSDATE - 3/24
    ORDER BY SETTLEMENTDATE, DUID
""")
scada.columns = [c.upper() for c in scada.columns]
print("\n=== Raw SCADA last 3h (HBESS%) ===")
print(scada.to_string(index=False))

# 4. Min/Max SCADA over 7 days per DUID — tells us the operating sign range
rng = query("""
    SELECT DUID,
           MIN(SCADAVALUE) AS MIN_MW,
           MAX(SCADAVALUE) AS MAX_MW,
           COUNT(*) AS N
    FROM TESTER.DISPATCH_UNIT_SCADA
    WHERE DUID LIKE 'HBESS%'
      AND SETTLEMENTDATE >= SYSDATE - 7
    GROUP BY DUID
    ORDER BY DUID
""")
rng.columns = [c.upper() for c in rng.columns]
print("\n=== 7-day SCADA range per DUID ===")
print(rng.to_string(index=False))
