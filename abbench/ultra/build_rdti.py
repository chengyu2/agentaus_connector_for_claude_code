"""Build the multi-workbook R&D tax-incentive task and its answer key from the public ATO file."""
import json, random, sys
import pandas as pd
SRC, OUT = sys.argv[1], sys.argv[2]
df = pd.read_excel(SRC, sheet_name='2022-23 Report')
df.columns = ['name', 'abn', 'exp', 'amended', 'year']
df['abn'] = df['abn'].map(lambda x: None if pd.isna(x) else f"{int(x):011d}")
spaced = lambda a: None if not isinstance(a, str) else f"{a[:2]} {a[2:5]} {a[5:8]} {a[8:]}"
cur, late = df[df.year == '2022-23'].copy(), df[df.year == '2021-22'].copy()
# Workbook 1: 2022-23 claims, ABN written with spaces as the ATO prints it, no amendment column.
pd.DataFrame({'Company name': cur.name, 'ABN': cur.abn.map(spaced),
              'R&D expenditure $': cur.exp}).to_excel(f'{OUT}/rdti_2022_23.xlsx', sheet_name='Claims', index=False)
# Workbook 2: late lodgers for 2021-22, ABN as a plain number.
pd.DataFrame({'Company': late.name, 'ABN': late.abn.map(lambda a: int(a) if isinstance(a, str) else None),
              'Expenditure': late.exp, 'Income year': late.year}).to_excel(f'{OUT}/rdti_2021_22_late.xlsx', sheet_name='Late lodgers', index=False)
# CSV 3: amendments for both years.
am = df[df.amended.notna()]
pd.DataFrame({'abn': am.abn, 'income_year': am.year, 'amended_expenditure': am.amended.astype(int)}).to_csv(f'{OUT}/amendments.csv', index=False)
# Workbook 4: a sector watchlist of 60 2022-23 claimants, ABN as text without spaces, plus a notes sheet.
rng = random.Random(2026)
pool = sorted(a for a in cur.abn if isinstance(a, str))
watch = rng.sample(pool, 60)
sectors = ['Medtech', 'Agrifood', 'Software', 'Advanced manufacturing', 'Energy']
wl = pd.DataFrame({'ABN': watch, 'Sector': [sectors[i % 5] for i in range(60)]})
with pd.ExcelWriter(f'{OUT}/sector_watchlist.xlsx') as xw:
    wl.to_excel(xw, sheet_name='Watchlist', index=False)
    pd.DataFrame({'Note': ['Sectors were assigned by the analyst team.', 'ABNs are 11 digits, no spaces.']}).to_excel(xw, sheet_name='Notes', index=False)

# ---- answer key, computed independently of any model ----
key = lambda r: r.abn if isinstance(r.abn, str) else 'NAME:' + r['name']
cur['id'] = cur.apply(key, axis=1)
amend_cur = {a: v for a, y, v in zip(am.abn, am.year, am.amended) if y == '2022-23'}
cur['eff'] = [int(amend_cur[a]) if a in amend_cur else int(e) for a, e in zip(cur.abn, cur.exp)]
eff = cur.set_index('id').eff
ans = {
  'q1_companies_2022_23': int(cur.id.nunique()),
  'q2_total_effective_2022_23': int(eff.sum()),
  'q3_median_effective_2022_23': int(round(float(eff.median()))),
  'q4_count_at_least_10m': int((eff >= 10_000_000).sum()),
  'q5_top_abn': str(eff.idxmax()),
  'q6_sector_totals': {s: int(eff.reindex(wl[wl.Sector == s].ABN).sum()) for s in sectors},
  'q7_in_both_years': int(len(set(cur.abn.dropna()) & set(late.abn.dropna()))),
  'q8_net_amendment_change_2022_23': int(sum(int(amend_cur[a]) - int(e) for a, e in zip(cur.abn, cur.exp) if a in amend_cur)),
}
json.dump(ans, open(f'{OUT}/../rdti_answer_key.json', 'w'), indent=1)
print(json.dumps(ans, indent=1))
