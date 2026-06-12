"""
성남시 어린이 보호구역 위험 분석 — 통계 결과 출력
"""
import sys, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats
import statsmodels.api as sm
from pyproj import Transformer

sys.stdout.reconfigure(encoding='utf-8')
warnings.filterwarnings('ignore')

ROOT = Path(__file__).parent
ENCODINGS = ['utf-8-sig', 'cp949', 'euc-kr', 'utf-8']

# ── 데이터 로드 ────────────────────────────────────────────────────────────────
def read_csv(fname):
    for enc in ENCODINGS:
        try:
            return pd.read_csv(ROOT / fname, encoding=enc)
        except Exception:
            continue

CHILD_TYPES = {"초등학교", "유치원", "어린이집", "특수학교", "학원", "도시공원"}
TARGET_GUS  = ["수정구", "분당구"]

zone_raw = read_csv("경기도 성남시_교통약자 보호구역 파일데이터.csv")
zone_raw = zone_raw[zone_raw["시군구명"].str.contains("수정구|분당구", na=False)]
zone_raw = zone_raw[zone_raw["시설종류"].isin(CHILD_TYPES)].copy()
zone_raw["구"] = zone_raw["시군구명"].apply(
    lambda x: next((g for g in TARGET_GUS if g in str(x)), "기타")
)

bus_raw = read_csv("경기도 성남시_버스정류장_현황_20260408.csv")

acc_frames = []
for f in ROOT.glob("*.xlsx"):
    try:
        df = pd.read_excel(f)
        if {"구분번호","사망자수","중상자수"} <= set(df.columns):
            df["발생년도"] = df["발생년월"].astype(str).str.extract(r"(\d{4})")[0].astype(int)
            df["구"] = df["시군구"].apply(lambda x: next((g for g in TARGET_GUS if g in str(x)), "기타"))
            acc_frames.append(df)
    except Exception:
        pass
accidents = pd.concat(acc_frames, ignore_index=True) if acc_frames else pd.DataFrame()

# ── 보호구역별 위험 지표 계산 ────────────────────────────────────────────────
tr = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)

z_x, z_y = tr.transform(zone_raw["경도"].astype(float).values, zone_raw["위도"].astype(float).values)
zone_xy   = np.column_stack([z_x, z_y])

b_lon = pd.to_numeric(bus_raw["경도"].astype(str).str.strip(), errors="coerce")
b_lat = pd.to_numeric(bus_raw["위도"].astype(str).str.strip(), errors="coerce")
valid = b_lon.notna() & b_lat.notna()
b_x, b_y  = tr.transform(b_lon[valid].values, b_lat[valid].values)
bus_xy     = np.column_stack([b_x, b_y])

route_counts = (
    bus_raw.loc[valid, "시내버스_경유노선번호"].fillna("").astype(str)
    .apply(lambda x: len([r for r in x.split(",") if r.strip()])).values
)

diffs = zone_xy[:, np.newaxis, :] - bus_xy[np.newaxis, :, :]
dists = np.sqrt((diffs**2).sum(axis=2))
nearest_idx = dists.argmin(axis=1)

zones = zone_raw.copy().reset_index(drop=True)
zones["dist_nearest_stop_m"] = dists.min(axis=1)
zones["stops_in_300m"]       = (dists < 300).sum(axis=1)
zones["nearest_route_cnt"]   = route_counts[nearest_idx]
zones["cctv_gap"]            = (zones["CCTV설치여부"].str.strip().str.upper() != "Y").astype(int)

# 정규화 및 위험점수
def norm(s):
    lo, hi = s.min(), s.max()
    return (s - lo) / (hi - lo) if hi > lo else pd.Series(0.0, index=s.index)

W = dict(slope=0.28, bus_prox=0.22, synergy=0.24, density=0.10, route=0.06, bump=0.06, cctv=0.04)
zones["n_bus_prox"] = 1 - norm(zones["dist_nearest_stop_m"])
zones["n_density"]  = norm(zones["stops_in_300m"])
zones["n_route"]    = norm(zones["nearest_route_cnt"])
zones["n_cctv"]     = zones["cctv_gap"].astype(float)
zones["n_slope"]    = 0.0
zones["n_synergy"]  = 0.0
zones["risk_score"] = (
    W["bus_prox"] * zones["n_bus_prox"] +
    W["density"]  * zones["n_density"]  +
    W["route"]    * zones["n_route"]    +
    W["cctv"]     * zones["n_cctv"]
)
p33 = zones["risk_score"].quantile(0.33)
p67 = zones["risk_score"].quantile(0.67)
zones["위험등급"] = pd.cut(zones["risk_score"], bins=[-np.inf, p33, p67, np.inf],
                           labels=["저위험","중위험","고위험"]).astype(str)
zones["위험등급_num"] = zones["위험등급"].map({"저위험":0,"중위험":1,"고위험":2})

# 사고건수 매핑 (구 단위)
acc_by_gu = accidents.groupby("구").agg(
    사고건수=("구분번호","count"),
    사망자수=("사망자수","sum"),
    중상자수=("중상자수","sum"),
).reset_index()
zones = zones.merge(acc_by_gu.rename(columns={"사고건수":"구_사고건수","사망자수":"구_사망자수","중상자수":"구_중상자수"}), on="구", how="left").fillna(0)

SEP = "=" * 70

# ═══════════════════════════════════════════════════════════════
# 1. 기술통계
# ═══════════════════════════════════════════════════════════════
print(SEP)
print("1. 기술통계 (Descriptive Statistics)  N =", len(zones))
print(SEP)

desc_vars = {
    "위험점수 (risk_score)":        "risk_score",
    "최근접 정류장 거리 (m)":        "dist_nearest_stop_m",
    "300m내 정류장 수 (개)":         "stops_in_300m",
    "최근접 정류장 노선 수 (개)":     "nearest_route_cnt",
    "CCTV 미설치 (0=설치,1=미설치)": "cctv_gap",
}

rows = []
for label, col in desc_vars.items():
    s = zones[col].dropna()
    rows.append({
        "변수": label,
        "N": len(s),
        "평균": round(s.mean(), 4),
        "표준편차": round(s.std(), 4),
        "최솟값": round(s.min(), 4),
        "Q1": round(s.quantile(0.25), 4),
        "중앙값": round(s.median(), 4),
        "Q3": round(s.quantile(0.75), 4),
        "최댓값": round(s.max(), 4),
    })

desc_df = pd.DataFrame(rows).set_index("변수")
print(desc_df.to_string())

# 구별 기술통계
print("\n[구별 기술통계]")
grp = zones.groupby("구")["risk_score"].agg(["count","mean","std","median"])
grp.columns = ["N","평균","표준편차","중앙값"]
print(grp.round(4).to_string())

# ═══════════════════════════════════════════════════════════════
# 2. 상관분석 (Spearman ρ)
# ═══════════════════════════════════════════════════════════════
print("\n" + SEP)
print("2. 상관분석 (Spearman Rank Correlation with risk_score)")
print(SEP)

corr_vars = {
    "최근접 정류장 거리 (m)":    "dist_nearest_stop_m",
    "300m내 정류장 수 (개)":     "stops_in_300m",
    "최근접 정류장 노선 수 (개)": "nearest_route_cnt",
    "CCTV 미설치":               "cctv_gap",
}

corr_rows = []
for label, col in corr_vars.items():
    rho, pval = stats.spearmanr(zones["risk_score"], zones[col])
    sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else "n.s."
    corr_rows.append({"변수": label, "ρ": round(rho, 4), "p-value": f"{pval:.4f}", "유의성": sig})

corr_df = pd.DataFrame(corr_rows).set_index("변수")
print(corr_df.to_string())
print("※ *** p<.001  ** p<.01  * p<.05  n.s. 유의하지 않음")

# 상관행렬 (주요 변수 간)
print("\n[상관행렬 — Spearman ρ]")
mat_cols = ["risk_score","dist_nearest_stop_m","stops_in_300m","nearest_route_cnt","cctv_gap"]
mat_labels = ["위험점수","정류장거리","300m정류장수","노선수","CCTV미설치"]
mat_data = np.zeros((len(mat_cols), len(mat_cols)))
p_data   = np.zeros((len(mat_cols), len(mat_cols)))
for i, c1 in enumerate(mat_cols):
    for j, c2 in enumerate(mat_cols):
        r, p = stats.spearmanr(zones[c1], zones[c2])
        mat_data[i,j] = round(r, 3)
        p_data[i,j]   = p

mat_df = pd.DataFrame(mat_data, index=mat_labels, columns=mat_labels)
print(mat_df.to_string())

# ═══════════════════════════════════════════════════════════════
# 3. 카이제곱 검정
# ═══════════════════════════════════════════════════════════════
print("\n" + SEP)
print("3. 카이제곱 검정 (Chi-square Test)")
print(SEP)

chi_tests = [
    ("위험등급 × 구",        "위험등급", "구"),
    ("위험등급 × CCTV미설치", "위험등급", "cctv_gap"),
]

# 시설종류 대분류
zones["시설대분류"] = zones["시설종류"].apply(
    lambda x: "학교급" if x in ("초등학교","특수학교","학원") else "유아기관"
)
chi_tests.append(("위험등급 × 시설대분류", "위험등급", "시설대분류"))

for name, r_var, c_var in chi_tests:
    ct = pd.crosstab(zones[r_var], zones[c_var])
    chi2, p, dof, expected = stats.chi2_contingency(ct)
    sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."
    n = ct.values.sum()
    cramers_v = np.sqrt(chi2 / (n * (min(ct.shape) - 1)))
    print(f"\n▶ {name}")
    print(ct.to_string())
    print(f"  χ²={chi2:.4f}  df={dof}  p={p:.4f}  {sig}  Cramér's V={cramers_v:.4f}")

print("\n※ *** p<.001  ** p<.01  * p<.05  n.s. 유의하지 않음")

# ═══════════════════════════════════════════════════════════════
# 4. Poisson Regression
# ═══════════════════════════════════════════════════════════════
print("\n" + SEP)
print("4. Poisson Regression (종속변수: 구_사고건수 / 독립변수: 위험 지표)")
print("   ※ 사고건수가 구 단위이므로 보호구역 수를 offset으로 보정")
print(SEP)

# 구 단위 집계
gu_stats = zones.groupby("구").agg(
    n_zones    =("risk_score","count"),
    mean_risk  =("risk_score","mean"),
    mean_dist  =("dist_nearest_stop_m","mean"),
    mean_stops =("stops_in_300m","mean"),
    mean_route =("nearest_route_cnt","mean"),
    cctv_rate  =("cctv_gap","mean"),
    accidents  =("구_사고건수","first"),
).reset_index()

print("\n[구 단위 집계]")
print(gu_stats.round(3).to_string(index=False))

# 보호구역 단위 Poisson (구_사고건수를 공유 → 반복 데이터 경고 있음)
# 더 유의미한 분석: 위험점수 → 위험등급_num Poisson
print("\n[Poisson Regression — 종속변수: 위험등급_num (0저/1중/2고)]")
print("   독립변수: 정류장거리, 300m정류장수, 노선수, CCTV미설치\n")

X_cols = ["dist_nearest_stop_m", "stops_in_300m", "nearest_route_cnt", "cctv_gap"]
X_labels = ["정류장거리(m)", "300m정류장수", "노선수", "CCTV미설치"]

X = zones[X_cols].copy()
# 표준화 (계수 비교 위해)
X_std = (X - X.mean()) / X.std()
X_std = sm.add_constant(X_std)
y = zones["위험등급_num"]

try:
    poisson_model = sm.GLM(y, X_std, family=sm.families.Poisson()).fit()

    coef_rows = []
    for i, (col, label) in enumerate(zip(["const"] + X_cols, ["상수항"] + X_labels)):
        idx = i
        coef = poisson_model.params.iloc[idx]
        se   = poisson_model.bse.iloc[idx]
        z    = poisson_model.tvalues.iloc[idx]
        p    = poisson_model.pvalues.iloc[idx]
        irr  = np.exp(coef)
        sig  = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."
        coef_rows.append({
            "변수": label,
            "β (계수)": round(coef, 4),
            "SE": round(se, 4),
            "z값": round(z, 4),
            "p-value": f"{p:.4f}",
            "유의성": sig,
        })

    coef_df = pd.DataFrame(coef_rows).set_index("변수")
    print(coef_df.to_string())

    print(f"\n  모델 적합도: AIC={poisson_model.aic:.2f}  Deviance={poisson_model.deviance:.2f}  df={poisson_model.df_resid:.0f}")
    print(f"  Pseudo R² (McFadden) = {1 - poisson_model.llf / poisson_model.llnull:.4f}")

    # ═══════════════════════════════════════════════════════════════
    # 5. IRR 해석표
    # ═══════════════════════════════════════════════════════════════
    print("\n" + SEP)
    print("5. IRR 해석표 (Incidence Rate Ratio = exp(β))")
    print("   IRR > 1: 위험등급 증가 방향 / IRR < 1: 위험등급 감소 방향")
    print(SEP)

    irr_rows = []
    for i, (col, label) in enumerate(zip(["const"] + X_cols, ["상수항"] + X_labels)):
        coef = poisson_model.params.iloc[i]
        ci_lo = poisson_model.conf_int().iloc[i, 0]
        ci_hi = poisson_model.conf_int().iloc[i, 1]
        p     = poisson_model.pvalues.iloc[i]
        irr   = np.exp(coef)
        irr_lo = np.exp(ci_lo)
        irr_hi = np.exp(ci_hi)
        sig   = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."

        if label == "상수항":
            direction = "-"
        elif irr > 1.05:
            direction = "↑ 위험등급 증가"
        elif irr < 0.95:
            direction = "↓ 위험등급 감소"
        else:
            direction = "→ 효과 미미"

        irr_rows.append({
            "변수": label,
            "IRR": round(irr, 4),
            "95% CI 하한": round(irr_lo, 4),
            "95% CI 상한": round(irr_hi, 4),
            "p-value": f"{p:.4f}",
            "유의성": sig,
            "해석": direction,
        })

    irr_df = pd.DataFrame(irr_rows).set_index("변수")
    print(irr_df.to_string())
    print("\n  ※ 독립변수는 표준화(Z-score)됨 → IRR: 1 SD 증가 시 위험등급 배율 변화")

except Exception as e:
    print(f"  Poisson 회귀 오류: {e}")

print("\n" + SEP)
print("분석 완료")
print(SEP)
