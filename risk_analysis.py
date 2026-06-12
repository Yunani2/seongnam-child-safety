"""
risk_analysis.py
성남시 어린이보호구역 위험도 지수 고도화

RQ1: slope vs accident_count → Spearman rho = -0.178, p = 0.025 (유의, 음의 상관)
RQ2: bus   vs accident_count → Spearman rho = +0.026, p = 0.747 (비유의)

Poisson GLM 계수를 가중치로 활용하여 보호구역별 위험도 지수를 산출한다.
음의 slope 계수를 그대로 반영하므로, '예측 사고 건수'가 위험도 지수가 된다.
"""

import sys, warnings, zipfile
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.patches as mpatches
import statsmodels.api as sm
from pathlib import Path
from scipy import stats
from pyproj import Transformer

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

# ── 한글 폰트 ──────────────────────────────────────────────────────────────────
_FONT_SET = False
for _fp in fm.findSystemFonts(fontext="ttf"):
    if any(k in _fp for k in ["Malgun", "malgun", "NanumGothic", "AppleGothic"]):
        plt.rcParams["font.family"] = fm.FontProperties(fname=_fp).get_name()
        _FONT_SET = True
        break
if not _FONT_SET:
    plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

# ── 설정 ───────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
ENCODINGS  = ["utf-8-sig", "cp949", "euc-kr", "utf-8"]
TARGET_GUS = ["수정구", "분당구"]
CHILD_TYPES = {"초등학교", "유치원", "어린이집", "특수학교", "학원", "도시공원"}
ACCIDENT_RADIUS_M = 200   # 보호구역 중심에서 사고 집계 반경
SEP = "=" * 65

# ── 실제 분석 결과 (제공된 통계값) ────────────────────────────────────────────
SPEARMAN_SLOPE_RHO = -0.177738
SPEARMAN_SLOPE_P   =  0.025001   # 유의 (p < 0.05)
SPEARMAN_BUS_RHO   =  0.025798
SPEARMAN_BUS_P     =  0.746857   # 비유의


# ══════════════════════════════════════════════════════════════════════════════
# STEP 0: 유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def read_csv(fname: str) -> pd.DataFrame | None:
    for enc in ENCODINGS:
        try:
            return pd.read_csv(ROOT / fname, encoding=enc)
        except Exception:
            continue
    return None


def haversine_m(lat1, lon1, lat2_arr, lon2_arr):
    """lat1/lon1 스칼라 → lat2_arr/lon2_arr 배열까지의 거리(m)"""
    R = 6_371_000
    φ1, φ2 = np.radians(lat1), np.radians(lat2_arr)
    dφ = np.radians(lat2_arr - lat1)
    dλ = np.radians(lon2_arr - lon1)
    a  = np.sin(dφ/2)**2 + np.cos(φ1)*np.cos(φ2)*np.sin(dλ/2)**2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: 데이터 로드 및 보호구역 데이터셋 구축
# ══════════════════════════════════════════════════════════════════════════════

print(SEP)
print("STEP 1. 데이터 로드")
print(SEP)

# ── 1-A. 어린이보호구역 ────────────────────────────────────────────────────────
zone_raw = read_csv("경기도 성남시_교통약자 보호구역 파일데이터.csv")
if zone_raw is None:
    raise FileNotFoundError("경기도 성남시_교통약자 보호구역 파일데이터.csv 없음")

zones = zone_raw[zone_raw["시군구명"].str.contains("수정구|분당구", na=False)].copy()
zones = zones[zones["시설종류"].isin(CHILD_TYPES)].copy()
zones["구"] = zones["시군구명"].apply(
    lambda x: next((g for g in TARGET_GUS if g in str(x)), "기타")
)
zones["위도"] = pd.to_numeric(zones["위도"], errors="coerce")
zones["경도"] = pd.to_numeric(zones["경도"], errors="coerce")
zones = zones.dropna(subset=["위도","경도"]).reset_index(drop=True)
zones["zone_id"] = zones.index
print(f"  보호구역: {len(zones)}개 (수정구 {(zones['구']=='수정구').sum()}, 분당구 {(zones['구']=='분당구').sum()})")

# ── 1-B. 버스정류장 ────────────────────────────────────────────────────────────
bus_raw = read_csv("경기도 성남시_버스정류장_현황_20260408.csv")
if bus_raw is None:
    raise FileNotFoundError("버스정류장 CSV 없음")

b_lat = pd.to_numeric(bus_raw["위도"].astype(str).str.strip(), errors="coerce")
b_lon = pd.to_numeric(bus_raw["경도"].astype(str).str.strip(), errors="coerce")
valid = b_lat.notna() & b_lon.notna()
bus_lat = b_lat[valid].values
bus_lon = b_lon[valid].values
print(f"  버스정류장: {valid.sum()}개")

# ── 1-C. 사고 데이터 ───────────────────────────────────────────────────────────
acc = read_csv("outputs/accidents_with_coords.csv")
if acc is None:
    raise FileNotFoundError("outputs/accidents_with_coords.csv 없음 — 대시보드 실행 후 재시도")

# 인코딩 문제 대비: 한글 컬럼이 깨진 경우에도 위치·심각도로 처리 가능한 형태
acc["lat"] = pd.to_numeric(acc["lat"], errors="coerce")
acc["lng"] = pd.to_numeric(acc["lng"], errors="coerce")
acc = acc.dropna(subset=["lat","lng"]).reset_index(drop=True)

# severity 컬럼 정규화 (영문/한글 혼재 대응)
if "severity" in acc.columns:
    acc["is_serious"] = acc["severity"].astype(str).str.contains("중상|serious", case=False).astype(int)
    acc["is_minor"]   = acc["severity"].astype(str).str.contains("경상|minor",   case=False).astype(int)
else:
    acc["is_serious"] = 0
    acc["is_minor"]   = 1

print(f"  사고 데이터: {len(acc)}건")

# ── 1-D. DEM 경사도 ────────────────────────────────────────────────────────────
slope_lookup: pd.DataFrame | None = None
try:
    import rasterio
    from rasterio.transform import xy as rio_xy

    # ZIP 내 .img 파일 자동 추출
    dem_dir = ROOT / "_dem_cache"
    img_path = None
    if dem_dir.exists():
        imgs = list(dem_dir.glob("**/*.img"))
        if imgs:
            img_path = imgs[0]

    if img_path is None:
        for zf_path in ROOT.glob("*.zip"):
            try:
                with zipfile.ZipFile(zf_path) as zf:
                    if any(n.endswith(".img") for n in zf.namelist()):
                        dem_dir.mkdir(exist_ok=True)
                        zf.extractall(dem_dir)
                        imgs = list(dem_dir.glob("**/*.img"))
                        if imgs:
                            img_path = imgs[0]
                        break
            except Exception:
                pass

    if img_path:
        with rasterio.open(img_path) as src:
            elev = src.read(1).astype(np.float64)
            if src.nodata is not None:
                elev[elev == src.nodata] = np.nan
            dy, dx = np.gradient(np.nan_to_num(elev),
                                  abs(src.transform.e), abs(src.transform.a))
            slope_grid = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
            crs_str = src.crs.to_string() if src.crs else "EPSG:5179"
            tr_dem = Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)
            r_idx, c_idx = np.where(slope_grid > 0)
            if len(r_idx) > 10_000:
                sel = np.random.default_rng(42).choice(len(r_idx), 10_000, replace=False)
                r_idx, c_idx = r_idx[sel], c_idx[sel]
            xs, ys = rio_xy(src.transform, r_idx, c_idx)
            lons, lats = tr_dem.transform(xs, ys)
            slope_lookup = pd.DataFrame({"lat": lats, "lon": lons,
                                          "slope_deg": slope_grid[r_idx, c_idx]})
        print(f"  DEM 경사도: {len(slope_lookup):,}개 격자 로드")
    else:
        print("  DEM 파일 미발견 — slope_category 추정치 사용")
except ImportError:
    print("  rasterio 미설치 — slope_category 추정치 사용")
except Exception as e:
    print(f"  DEM 오류({e}) — slope_category 추정치 사용")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: 보호구역별 특성 변수 계산
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{SEP}")
print("STEP 2. 보호구역별 특성 변수 계산")
print(SEP)

tr_proj = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
z_x, z_y = tr_proj.transform(zones["경도"].values, zones["위도"].values)
b_x, b_y  = tr_proj.transform(bus_lon, bus_lat)

# ── 버스정류장 거리 (투영 좌표계) ──────────────────────────────────────────────
print("  버스정류장 거리 계산 중...")
diffs = (np.stack([z_x, z_y], axis=1)[:, np.newaxis, :]
         - np.stack([b_x, b_y], axis=1)[np.newaxis, :, :])
dists_bus = np.sqrt((diffs**2).sum(axis=2))   # shape: (N_zones, N_bus)

zones["dist_nearest_stop_m"] = dists_bus.min(axis=1)
zones["stops_in_300m"]       = (dists_bus < 300).sum(axis=1)

# bus_category: 0=100m초과, 1=50-100m, 2=30-50m, 3=30m이내
zones["bus_category"] = pd.cut(
    zones["dist_nearest_stop_m"],
    bins=[-np.inf, 30, 50, 100, np.inf],
    labels=[3, 2, 1, 0],
).astype(float)

# ── 경사도 ─────────────────────────────────────────────────────────────────────
def get_slope_cat(lat, lon, lookup, radius=0.004):
    if lookup is None:
        return None, None
    mask = (np.abs(lookup["lat"] - lat) < radius) & (np.abs(lookup["lon"] - lon) < radius)
    nb = lookup[mask]
    if nb.empty:
        return None, None
    deg = float(nb["slope_deg"].mean())
    cat = 0 if deg < 3 else 1 if deg < 5 else 2 if deg < 8 else 3 if deg < 12 else 4
    return deg, cat

slope_degs, slope_cats, has_slope = [], [], []
for _, row in zones.iterrows():
    deg, cat = get_slope_cat(float(row["위도"]), float(row["경도"]), slope_lookup)
    slope_degs.append(deg if deg is not None else 0.0)
    slope_cats.append(cat if cat is not None else 0)
    has_slope.append(deg is not None)

zones["slope_deg"]      = slope_degs
zones["slope_category"] = slope_cats
zones["has_slope"]      = has_slope

slope_coverage = sum(has_slope) / len(has_slope) * 100
print(f"  경사도 데이터 커버리지: {slope_coverage:.1f}%  (없으면 0으로 대체)")

# ── 보호구역별 사고 집계 (반경 200m) ──────────────────────────────────────────
print(f"  사고 집계 (반경 {ACCIDENT_RADIUS_M}m)...")
acc_lat = acc["lat"].values
acc_lng = acc["lng"].values
acc_seri = acc["is_serious"].values

accident_counts, serious_counts, minor_counts = [], [], []
for _, row in zones.iterrows():
    dists_acc = haversine_m(float(row["위도"]), float(row["경도"]), acc_lat, acc_lng)
    inside = dists_acc <= ACCIDENT_RADIUS_M
    accident_counts.append(int(inside.sum()))
    serious_counts.append(int(acc_seri[inside].sum()))
    minor_counts.append(int(inside.sum()) - int(acc_seri[inside].sum()))

zones["accident_count"] = accident_counts
zones["serious_count"]  = serious_counts
zones["minor_count"]    = minor_counts

# 심각도 점수: 중상=2, 경상=1
zones["severity_score"] = zones["serious_count"] * 2 + zones["minor_count"] * 1

print(f"  집계 완료 — 사고 있는 구역: {(zones['accident_count']>0).sum()}개 / {len(zones)}개")
print(f"  총 매칭 사고건수: {zones['accident_count'].sum()} (중복 집계 가능)")

# 교차항
zones["slope_x_bus"] = zones["slope_category"] * zones["bus_category"]

# final_dataset 저장
out_dir = ROOT / "outputs"
out_dir.mkdir(exist_ok=True)
zones.to_csv(out_dir / "final_dataset.csv", index=False, encoding="utf-8-sig")
print(f"\n  → outputs/final_dataset.csv 저장")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Poisson GLM 피팅
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{SEP}")
print("STEP 3. Poisson GLM 피팅")
print(SEP)

X_cols  = ["slope_category", "bus_category", "slope_x_bus"]
X_label = ["slope_category (경사도)", "bus_category (버스근접도)", "slope×bus (교차항)"]

# 결측 제거
df_model = zones[X_cols + ["accident_count"]].dropna().copy()
X = sm.add_constant(df_model[X_cols].astype(float))
y = df_model["accident_count"].astype(float)

print(f"  분석 대상 보호구역: {len(df_model)}개")
print(f"  사고 건수 분포: 0건={( y==0).sum()}  1건={(y==1).sum()}  2건+={(y>=2).sum()}")

try:
    poisson_full = sm.GLM(y, X, family=sm.families.Poisson()).fit()

    print("\n  [Poisson GLM — 전체 모델 (slope + bus + 교차항)]")
    coef_rows = []
    for col, label in zip(["const"] + X_cols, ["상수항"] + X_label):
        b    = poisson_full.params[col]
        se   = poisson_full.bse[col]
        z    = poisson_full.tvalues[col]
        p    = poisson_full.pvalues[col]
        irr  = np.exp(b)
        sig  = "***" if p<0.001 else "**" if p<0.01 else "*" if p<0.05 else "n.s."
        coef_rows.append({"변수": label, "β": round(b,4), "SE": round(se,4),
                           "z": round(z,3), "p-value": f"{p:.4f}",
                           "IRR": round(irr,3), "유의성": sig})
        print(f"  {label:<35}  β={b:+.4f}  IRR={irr:.3f}  p={p:.4f}  {sig}")

    print(f"\n  AIC={poisson_full.aic:.2f}  Deviance={poisson_full.deviance:.2f}")
    pseudo_r2 = 1 - poisson_full.llf / poisson_full.llnull
    print(f"  Pseudo R² (McFadden) = {pseudo_r2:.4f}")

    # Model 1: 주효과만
    X_main = sm.add_constant(df_model[["slope_category","bus_category"]].astype(float))
    poisson_main = sm.GLM(y, X_main, family=sm.families.Poisson()).fit()

    # Likelihood Ratio Test
    LR = -2 * (poisson_main.llf - poisson_full.llf)
    p_lr = 1 - stats.chi2.cdf(LR, df=1)
    print(f"\n  [LRT — 교차항 추가 효과]  LR={LR:.3f}  p={p_lr:.4f}",
          "→ 교차항 유의" if p_lr < 0.05 else "→ 교차항 비유의 (주효과 모델 채택)")

    # 사용할 모델 선택
    if p_lr < 0.05:
        best_model = poisson_full
        best_X     = X
        model_name = "전체 모델 (slope + bus + slope×bus)"
    else:
        best_model = poisson_main
        best_X     = X_main
        model_name = "주효과 모델 (slope + bus)"

    print(f"\n  → 채택 모델: {model_name}")

    # 유의한 계수만 추출
    sig_coefs = {
        col: best_model.params[col]
        for col in best_model.params.index
        if col != "const" and best_model.pvalues[col] < 0.05
    }
    print(f"  유의한 변수 (p<0.05): {list(sig_coefs.keys()) if sig_coefs else '없음'}")

    GLM_OK = True
except Exception as e:
    print(f"  Poisson 회귀 오류: {e}")
    best_model = None
    best_X     = None
    sig_coefs  = {}
    GLM_OK     = False


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: 위험도 지수 계산
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{SEP}")
print("STEP 4. 위험도 지수 계산")
print(SEP)

df_risk = zones.dropna(subset=["slope_category", "bus_category"]).copy()

# ── 방법 A: Poisson 예측값 기반 ───────────────────────────────────────────────
if GLM_OK:
    # 최적 모델의 독립변수 준비
    if "slope_x_bus" in best_X.columns:
        X_pred_cols = ["slope_category", "bus_category", "slope_x_bus"]
    else:
        X_pred_cols = ["slope_category", "bus_category"]

    X_pred = sm.add_constant(df_risk[X_pred_cols].astype(float),
                              has_constant="add")
    # 컬럼 순서 맞추기
    X_pred = X_pred[[c for c in best_X.columns if c in X_pred.columns]]

    df_risk["risk_score_poisson"] = best_model.predict(X_pred).values
    print("  방법 A: Poisson 모델 예측값 → risk_score_poisson")
    print(f"          범위: {df_risk['risk_score_poisson'].min():.4f} ~ "
          f"{df_risk['risk_score_poisson'].max():.4f}")

# ── 방법 B: Spearman 상관계수 기반 가중합 ─────────────────────────────────────
#   유의한 변수만 반영 (p < 0.05). bus_category는 p=0.747 → 가중치 0
SIG_THRESHOLD = 0.05
w_slope = SPEARMAN_SLOPE_RHO if SPEARMAN_SLOPE_P < SIG_THRESHOLD else 0.0
w_bus   = SPEARMAN_BUS_RHO   if SPEARMAN_BUS_P   < SIG_THRESHOLD else 0.0

# 0~1 정규화
def norm01(s: pd.Series) -> pd.Series:
    lo, hi = s.min(), s.max()
    return (s - lo) / (hi - lo) if hi > lo else pd.Series(0.5, index=s.index)

df_risk["n_slope"] = norm01(df_risk["slope_category"].astype(float))
df_risk["n_bus"]   = norm01(df_risk["bus_category"].astype(float))

# 가중합: 음수 가중치 → 경사 높을수록 위험도 낮아짐 (데이터 기반)
df_risk["risk_score_spearman"] = (w_slope * df_risk["n_slope"]
                                  + w_bus   * df_risk["n_bus"])

print(f"\n  방법 B: Spearman 가중합 → risk_score_spearman")
print(f"          가중치: slope={w_slope:+.4f} (p={SPEARMAN_SLOPE_P:.3f}), "
      f"bus={w_bus:+.4f} (p={SPEARMAN_BUS_P:.3f})")
print(f"          범위: {df_risk['risk_score_spearman'].min():.4f} ~ "
      f"{df_risk['risk_score_spearman'].max():.4f}")

# ── 최종 위험도 지수 선택 ─────────────────────────────────────────────────────
# Poisson 모델이 성공했으면 A 사용, 아니면 B
if GLM_OK:
    df_risk["risk_index"] = df_risk["risk_score_poisson"]
    score_method = "Poisson GLM 예측값"
else:
    df_risk["risk_index"] = df_risk["risk_score_spearman"]
    score_method = "Spearman 가중합"

# 0~100 스케일 변환 (시각화 가독성)
ri_min, ri_max = df_risk["risk_index"].min(), df_risk["risk_index"].max()
df_risk["risk_index_scaled"] = (
    (df_risk["risk_index"] - ri_min) / (ri_max - ri_min) * 100
    if ri_max > ri_min else pd.Series(50.0, index=df_risk.index)
)

# 위험등급 (3분위)
p33 = df_risk["risk_index_scaled"].quantile(0.33)
p67 = df_risk["risk_index_scaled"].quantile(0.67)
df_risk["위험등급"] = pd.cut(
    df_risk["risk_index_scaled"],
    bins=[-np.inf, p33, p67, np.inf],
    labels=["저위험", "중위험", "고위험"],
).astype(str)

print(f"\n  최종 위험도 지수: {score_method} (0~100 스케일)")

# 결과 저장
df_risk.to_csv(out_dir / "final_dataset_with_risk.csv", index=False, encoding="utf-8-sig")
print(f"  → outputs/final_dataset_with_risk.csv 저장")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Top 10 위험 지역 출력 및 시각화
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{SEP}")
print("STEP 5. Top 10 위험 지역")
print(SEP)

top10 = (df_risk
         .sort_values("risk_index_scaled", ascending=False)
         .head(10)
         [["대상시설명", "시설종류", "구", "risk_index_scaled",
           "위험등급", "accident_count", "slope_category", "bus_category",
           "dist_nearest_stop_m"]]
         .reset_index(drop=True))
top10.index += 1

SLOPE_LABEL = {0:"평지(0-3°)", 1:"완만(3-5°)", 2:"보통(5-8°)", 3:"급경사(8-12°)", 4:"매우급(12°+)"}
BUS_LABEL   = {0:"100m초과", 1:"50-100m", 2:"30-50m", 3:"30m이내"}

top10["경사도"] = top10["slope_category"].map(lambda x: SLOPE_LABEL.get(int(x),"알수없음"))
top10["버스거리"] = top10["bus_category"].map(lambda x: BUS_LABEL.get(int(x),"알수없음"))
top10["위험지수"] = top10["risk_index_scaled"].round(1)

display_cols = ["대상시설명","구","위험등급","위험지수","accident_count","경사도","버스거리"]
print(top10[display_cols].to_string())

# 구별 위험도 통계
print(f"\n[구별 평균 위험도]")
grp = df_risk.groupby("구").agg(
    N=("risk_index_scaled","count"),
    평균위험지수=("risk_index_scaled","mean"),
    평균사고건수=("accident_count","mean"),
    고위험구역수=("위험등급", lambda x: (x=="고위험").sum()),
).round(3)
print(grp.to_string())


# ── 시각화 ────────────────────────────────────────────────────────────────────
RISK_COLOR_MAP = {"고위험": "#d62728", "중위험": "#ff7f0e", "저위험": "#2ca02c"}
top10_colors   = [RISK_COLOR_MAP.get(g, "#999") for g in top10["위험등급"]]

fig, axes = plt.subplots(2, 2, figsize=(16, 12))
fig.suptitle(
    f"성남시 어린이보호구역 위험도 지수 분석 (2020-2022)\n"
    f"[{score_method} 기반 — slope: rho={SPEARMAN_SLOPE_RHO:.3f}(p={SPEARMAN_SLOPE_P:.3f}), "
    f"bus: rho={SPEARMAN_BUS_RHO:.3f}(p={SPEARMAN_BUS_P:.3f})]",
    fontsize=12, y=1.01
)

# ── 차트 1: Top 10 가로 막대그래프 ───────────────────────────────────────────
ax1 = axes[0, 0]
names = top10["대상시설명"].str[:12] + "\n(" + top10["구"] + ")"
bars = ax1.barh(range(10, 0, -1), top10["위험지수"], color=top10_colors, edgecolor="white", height=0.7)
ax1.set_yticks(range(10, 0, -1))
ax1.set_yticklabels([f"{i}. {n}" for i, n in zip(range(1,11), names)], fontsize=8)
ax1.set_xlabel("위험도 지수 (0~100)")
ax1.set_title("Top 10 위험 어린이보호구역")
ax1.axvline(x=p67, color="gray", linestyle="--", alpha=0.5, label=f"고위험 임계값 ({p67:.1f})")
ax1.legend(fontsize=8)
for bar, score, cnt in zip(bars, top10["위험지수"], top10["accident_count"]):
    ax1.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
             f"{score:.1f} ({cnt}건)", va="center", fontsize=7.5)
patches = [mpatches.Patch(color=c, label=l) for l, c in RISK_COLOR_MAP.items()]
ax1.legend(handles=patches, loc="lower right", fontsize=8)

# ── 차트 2: 구별 위험도 분포 ─────────────────────────────────────────────────
ax2 = axes[0, 1]
colors_gu = {"수정구": "#1565C0", "분당구": "#E91E63"}
for gu, grp_data in df_risk.groupby("구"):
    ax2.hist(grp_data["risk_index_scaled"], bins=15, alpha=0.6,
             color=colors_gu.get(gu, "gray"), label=gu, edgecolor="white")
ax2.axvline(p33, color="green",  linestyle="--", alpha=0.7, label=f"저위험 임계({p33:.1f})")
ax2.axvline(p67, color="orange", linestyle="--", alpha=0.7, label=f"고위험 임계({p67:.1f})")
ax2.set_xlabel("위험도 지수 (0~100)")
ax2.set_ylabel("구역 수")
ax2.set_title("구별 위험도 지수 분포")
ax2.legend(fontsize=9)

# ── 차트 3: 경사도 범주별 평균 위험도 ────────────────────────────────────────
ax3 = axes[1, 0]
slope_grp = df_risk.groupby("slope_category")["risk_index_scaled"].agg(["mean","std","count"])
slope_labels_x = [SLOPE_LABEL.get(int(i), str(i)) for i in slope_grp.index]
bar_colors_slope = ["#1a9641","#a6d96a","#fdae61","#d7191c","#7b2d00"][:len(slope_grp)]
bars3 = ax3.bar(range(len(slope_grp)), slope_grp["mean"], color=bar_colors_slope,
                yerr=slope_grp["std"], capsize=4, edgecolor="white")
ax3.set_xticks(range(len(slope_grp)))
ax3.set_xticklabels(slope_labels_x, fontsize=8)
ax3.set_ylabel("평균 위험도 지수")
ax3.set_title(f"경사도 범주별 평균 위험도\n(Spearman ρ={SPEARMAN_SLOPE_RHO:.3f}, p={SPEARMAN_SLOPE_P:.3f})")
for bar, (_, row_s) in zip(bars3, slope_grp.iterrows()):
    ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
             f"n={int(row_s['count'])}", ha="center", fontsize=8)

# ── 차트 4: 버스 범주별 평균 위험도 ──────────────────────────────────────────
ax4 = axes[1, 1]
bus_grp = df_risk.groupby("bus_category")["risk_index_scaled"].agg(["mean","std","count"])
bus_labels_x = [BUS_LABEL.get(int(i), str(i)) for i in bus_grp.index]
bar_colors_bus = ["#4575b4","#74add1","#fdae61","#d73027"][:len(bus_grp)]
bars4 = ax4.bar(range(len(bus_grp)), bus_grp["mean"], color=bar_colors_bus,
                yerr=bus_grp["std"], capsize=4, edgecolor="white")
ax4.set_xticks(range(len(bus_grp)))
ax4.set_xticklabels(bus_labels_x, fontsize=8)
ax4.set_ylabel("평균 위험도 지수")
ax4.set_title(f"버스정류장 근접도별 평균 위험도\n(Spearman ρ={SPEARMAN_BUS_RHO:.3f}, p={SPEARMAN_BUS_P:.3f}, 비유의)")
for bar, (_, row_b) in zip(bars4, bus_grp.iterrows()):
    ax4.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
             f"n={int(row_b['count'])}", ha="center", fontsize=8)

plt.tight_layout()
out_fig = out_dir / "risk_analysis_top10.png"
plt.savefig(out_fig, dpi=150, bbox_inches="tight")
print(f"\n  → {out_fig} 저장")
plt.show()

# ── IRR 해석 요약 ─────────────────────────────────────────────────────────────
if GLM_OK:
    print(f"\n{SEP}")
    print("STEP 6. IRR 해석 (Incidence Rate Ratio = exp(β))")
    print(SEP)
    print("  IRR > 1: 해당 변수 증가 → 예측 사고 건수 증가")
    print("  IRR < 1: 해당 변수 증가 → 예측 사고 건수 감소 (음의 효과)\n")
    for col, label in zip(best_model.params.index[1:], X_label):
        b   = best_model.params[col]
        irr = np.exp(b)
        p   = best_model.pvalues[col]
        sig = "***" if p<0.001 else "**" if p<0.01 else "*" if p<0.05 else "n.s."
        direction = ("↑ 사고 증가" if irr > 1.05 else
                     "↓ 사고 감소" if irr < 0.95 else "→ 효과 미미")
        print(f"  {label:<38} IRR={irr:.3f}  {direction}  {sig}")

print(f"\n{SEP}")
print("분석 완료")
print(f"  final_dataset.csv          → outputs/final_dataset.csv")
print(f"  위험도 포함 데이터셋       → outputs/final_dataset_with_risk.csv")
print(f"  시각화                     → outputs/risk_analysis_top10.png")
print(SEP)
