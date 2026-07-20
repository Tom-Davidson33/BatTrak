import pandas as pd
from db import query

pd.set_option("display.max_rows", 200)
pd.set_option("display.width", 180)

# 1. Every storage-capacity DUID in the NEM: type, capacity, region
inc = query("""
    SELECT d.DUID, d.DISPATCHTYPE, d.MAXSTORAGECAPACITY,
           d.REGISTEREDCAPACITY, s.REGIONID
    FROM TESTER.DUDETAIL d
    JOIN (
        SELECT DUID, MAX(LASTCHANGED) AS LC
        FROM TESTER.DUDETAIL
        WHERE MAXSTORAGECAPACITY > 0
        GROUP BY DUID
    ) l ON d.DUID = l.DUID AND d.LASTCHANGED = l.LC
    LEFT JOIN (
        SELECT DUID, REGIONID, MAX(END_DATE) ED
        FROM TESTER.DUDETAILSUMMARY GROUP BY DUID, REGIONID
    ) s ON d.DUID = s.DUID
    ORDER BY s.REGIONID, d.DUID
""")
inc.columns = [c.upper() for c in inc.columns]
print(f"\n=== INCLUDED in dashboard ({len(inc)} units) ===")
print(inc.to_string(index=False))
print("\nDispatch types present:", inc["DISPATCHTYPE"].unique())

# 2. Battery-looking DUIDs that have NO storage capacity on ANY of their
#    registration rows but DO have recent SCADA -> potentially MISSED batteries.
#    Heuristic: DUIDs matching common BESS naming with SCADA in last 7 days.
missed = query("""
    SELECT sc.DUID,
           MIN(sc.SCADAVALUE) AS MIN_MW,
           MAX(sc.SCADAVALUE) AS MAX_MW,
           COUNT(*) AS N
    FROM TESTER.DISPATCH_UNIT_SCADA sc
    WHERE sc.SETTLEMENTDATE >= SYSDATE - 7
      AND (sc.DUID LIKE '%BESS%' OR sc.DUID LIKE '%BATT%'
           OR sc.DUID LIKE '%BAT1' OR sc.DUID LIKE '%_B_%')
      AND sc.DUID NOT IN (
          SELECT DUID FROM TESTER.DUDETAIL WHERE MAXSTORAGECAPACITY > 0
      )
    GROUP BY sc.DUID
    HAVING MIN(sc.SCADAVALUE) < 0          -- charges => almost certainly a battery
    ORDER BY sc.DUID
""")
missed.columns = [c.upper() for c in missed.columns]
print(f"\n=== POSSIBLY MISSED (charge but no capacity registered) ({len(missed)}) ===")
print(missed.to_string(index=False) if len(missed) else "  none found")

# 3. Sanity: any included unit whose 7-day SCADA never goes negative?
#    (a bidirectional unit that never charges is suspect / may be misconfigured)
inc_duids = inc["DUID"].tolist()
binds = {f"d{i}": d for i, d in enumerate(inc_duids)}
ph = ",".join(f":{k}" for k in binds)
rng = query(f"""
    SELECT DUID, MIN(SCADAVALUE) MIN_MW, MAX(SCADAVALUE) MAX_MW, COUNT(*) N
    FROM TESTER.DISPATCH_UNIT_SCADA
    WHERE DUID IN ({ph}) AND SETTLEMENTDATE >= SYSDATE - 7
    GROUP BY DUID ORDER BY MIN_MW
""", binds)
rng.columns = [c.upper() for c in rng.columns]
print("\n=== 7-day SCADA range for INCLUDED units (check all charge & discharge) ===")
print(rng.to_string(index=False))
print("\nIncluded units that NEVER charged (MIN_MW >= 0):")
print(rng[rng["MIN_MW"] >= 0]["DUID"].tolist() or "  none — good")