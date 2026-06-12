"""
성남시 어린이보호구역 교통안전 분석 대시보드 v2
격자형 위험도 지도 | 레이어 토글 | 클릭 팝업
2026 가천대학교 스마트시티학과 캡스톤디자인
"""
import json
import re
import warnings
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

try:
    from pyproj import Transformer
    HAS_PYPROJ = True
except Exception:
    HAS_PYPROJ = False

warnings.filterwarnings("ignore")

try:
    import folium
    from folium.plugins import Fullscreen, HeatMap, MiniMap
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

try:
    import plotly.express as px
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

ROOT = Path(__file__).parent

TARGET_GUS = ["수정구", "분당구"]

# 법정동 코드 prefix (행정안전부 기준)
# 수정구: 4113110100~4113111700 → 상위 6자리 '411311'
# 분당구: 4113510100~4113511800 → 상위 6자리 '411351'
BJD_PREFIX = {"수정구": "411311", "분당구": "411351"}
CHILD_FACILITY_TYPES = {"초등학교", "유치원", "어린이집", "특수학교", "학원", "도시공원"}
_ZONE_SIG = {"보호구역아이디", "시설종류", "시군구명", "경도", "위도"}
_BUS_SIG = {"정류장번호(ID)", "정류장명", "경도", "위도", "시내버스_경유노선번호"}
_BUMP_SIG = {"과속방지턱관리번호", "경도", "위도"}
_ENCODINGS = ["utf-8-sig", "cp949", "euc-kr", "utf-8"]

# 실제 분석 결과 (2020-2022년 83건, Spearman + Poisson GLM)
# RQ1: slope vs accident_count → rho=-0.178, p=0.025 (유의, 음의 상관)
# RQ2: bus   vs accident_count → rho=+0.026, p=0.747 (비유의 → 가중치 0)
_SPEARMAN_SLOPE_RHO = -0.177738
_SPEARMAN_BUS_RHO   =  0.025798
_SPEARMAN_BUS_SIG   = False   # p=0.747

RISK_COLOR = {"고위험": "#d62728", "중위험": "#ff7f0e", "저위험": "#2ca02c"}
RISK_FILL  = {"고위험": 0.78, "중위험": 0.65, "저위험": 0.55}
RISK_BG    = {"고위험": "#fff0f0", "중위험": "#fff8ee", "저위험": "#f0fff4"}
RISK_EMOJI = {"고위험": "🔴", "중위험": "🟠", "저위험": "🟢"}

SLOPE_LABEL = {
    0: "평지 (0–3°)", 1: "완만 (3–5°)",
    2: "보통 (5–8°)", 3: "급경사 (8–12°)", 4: "매우 급경사 (12°+)"
}
BUS_LABEL = {
    0: "멀리 (100m 초과)", 1: "중간 (50–100m)",
    2: "인접 (30–50m)",   3: "매우 인접 (30m 이내)"
}

# 주요 랜드마크 (챗봇 거리 질문 응답용)
LANDMARKS = {
    "가천대학교": (37.4486, 127.1296),          # 수정구 성남대로 1342
    "가천대학교 글로벌캠퍼스": (37.4486, 127.1296),
    "성남시청": (37.4203, 127.1265),
    "수정구청": (37.4341, 127.1378),
    "분당구청": (37.3784, 127.1223),
    "모란역": (37.4294, 127.1288),
    "태평역": (37.4410, 127.1338),
    "신흥역": (37.4471, 127.1378),
    "수진역": (37.4521, 127.1436),
}

# 격자 크기: 위·경도 오프셋 (약 130m — 300m에서 축소하여 겹침 감소)
GRID_DLAT = 0.00120
GRID_DLON = 0.00150


# ── 파일 탐색 ──────────────────────────────────────────────────────────────────

def find_source_files(root: Path) -> dict:
    found: dict = {}
    for f in sorted(root.glob("*.csv")):
        for enc in _ENCODINGS:
            try:
                cols = set(pd.read_csv(f, encoding=enc, nrows=0).columns)
            except Exception:
                continue
            if _ZONE_SIG <= cols and "zone" not in found:
                found["zone"] = (f, enc)
            elif _BUS_SIG <= cols and "bus" not in found:
                found["bus"] = (f, enc)
            elif _BUMP_SIG <= cols and "bump" not in found:
                found["bump"] = (f, enc)
            break
    geojsons = list(root.glob("*.geojson"))
    if geojsons:
        found["geojson"] = geojsons[0]
    for f in sorted(root.glob("*.xlsx")):
        try:
            cols = set(pd.read_excel(f, nrows=0).columns)
            if {"구분번호", "발생년월", "시군구", "사망자수", "중상자수"} <= cols:
                found.setdefault("accident", []).append(f)
        except Exception:
            pass
    return found


# ── DEM / 경사도 ───────────────────────────────────────────────────────────────

def _extract_dem(root: Path):
    dem_dir = root / "dem_unzipped"
    if dem_dir.exists():
        imgs = list(dem_dir.glob("**/*.img"))
        if imgs:
            return imgs[0]
    for z in root.glob("*.zip"):
        try:
            with zipfile.ZipFile(z) as zf:
                if any(n.endswith(".img") for n in zf.namelist()):
                    dem_dir.mkdir(exist_ok=True)
                    zf.extractall(dem_dir)
                    imgs = list(dem_dir.glob("**/*.img"))
                    if imgs:
                        return imgs[0]
        except Exception:
            pass
    return None


def build_slope_df(root: Path) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["lat", "lon", "slope_deg"])
    if not HAS_RASTERIO:
        return empty
    img = _extract_dem(root)
    if img is None:
        return empty
    try:
        with rasterio.open(img) as src:
            elev = src.read(1).astype(np.float64)
            if src.nodata is not None:
                elev[elev == src.nodata] = np.nan
            dy, dx = np.gradient(np.nan_to_num(elev), abs(src.transform.e), abs(src.transform.a))
            slope = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
            r, c = np.where(slope > 3)
            if len(r) > 8000:
                idx = np.random.default_rng(42).choice(len(r), 8000, replace=False)
                r, c = r[idx], c[idx]
            xs, ys = rasterio.transform.xy(src.transform, r, c)
            crs = src.crs.to_string() if src.crs else "EPSG:5179"
            tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            lons, lats = tr.transform(xs, ys)
            return pd.DataFrame({"lat": lats, "lon": lons, "slope_deg": slope[r, c]})
    except Exception:
        return empty


def assign_zone_slope(lat, lon, slope_df, radius=0.004):
    if slope_df.empty:
        return None, None
    mask = (np.abs(slope_df["lat"] - lat) < radius) & (np.abs(slope_df["lon"] - lon) < radius)
    nearby = slope_df[mask]
    if nearby.empty:
        return None, None
    deg = float(nearby["slope_deg"].mean())
    cat = 0 if deg < 3 else 1 if deg < 5 else 2 if deg < 8 else 3 if deg < 12 else 4
    return deg, cat


# ── 사고 좌표 매핑 ────────────────────────────────────────────────────────────

def _parse_ym(s) -> int | None:
    """'2020년 1월' → 202001"""
    m = re.search(r'(\d{4})\D+(\d{1,2})', str(s))
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def snap_accidents_to_geojson(
    ma: pd.DataFrame,
    geo_points: pd.DataFrame,
    zones: pd.DataFrame,
    radius_deg: float = 0.003,   # ~300m
) -> pd.DataFrame:
    """좌표 CSV에서 로드한 83건 사고 좌표를 seongnam_accidents.geojson에서
    같은 연월 + 같은 구 + 보호구역 반경 내 가장 가까운 실제 사고 좌표로 교체."""
    if geo_points.empty or zones.empty or ma.empty:
        return ma

    z_lats = zones["위도"].astype(float).values
    z_lons = zones["경도"].astype(float).values

    # geo_points 중 보호구역 반경 내 사고만 사전 필터링
    gp = geo_points.copy()
    gp["bjd_str"] = gp["bjd_cd"].fillna("").astype(str)
    g_lats = gp["lat"].values
    g_lons = gp["lon"].values
    in_zone = np.zeros(len(gp), dtype=bool)
    for z_lat, z_lon in zip(z_lats, z_lons):
        in_zone |= (np.abs(g_lats - z_lat) < radius_deg) & (np.abs(g_lons - z_lon) < radius_deg)
    gp_zone = gp[in_zone].copy()

    if gp_zone.empty:
        return ma

    result = ma.copy()
    for idx, row in ma.iterrows():
        gu = str(row.get("구", ""))
        prefix = BJD_PREFIX.get(gu)

        # 발생년월 → acc_ym 정수 변환
        ym_str = str(row.get("발생년월", ""))
        m = re.search(r'(\d{4})년\s*(\d{1,2})월', ym_str)
        acc_ym = (int(m.group(1)) * 100 + int(m.group(2))) if m else None

        # 같은 구 + 보호구역 반경 내 후보
        cands = gp_zone[gp_zone["bjd_str"].str.startswith(prefix)] if prefix else gp_zone

        # 같은 연월 추가 필터 (후보가 있을 때만)
        if acc_ym is not None and "acc_ym" in cands.columns:
            cands_ym = cands[cands["acc_ym"] == acc_ym]
            if not cands_ym.empty:
                cands = cands_ym

        if cands.empty:
            continue

        # 보호구역과 가장 가까운 후보 선택
        c_lats = cands["lat"].values
        c_lons = cands["lon"].values
        min_d, best_i = np.inf, 0
        for i, (clat, clon) in enumerate(zip(c_lats, c_lons)):
            d = float(np.min(np.abs(z_lats - clat) + np.abs(z_lons - clon)))
            if d < min_d:
                min_d, best_i = d, i

        result.at[idx, "lat"] = float(c_lats[best_i])
        result.at[idx, "lon"] = float(c_lons[best_i])
        result.at[idx, "match_quality"] = "geojson_zone"

    return result


def match_accidents_to_coords(accidents: pd.DataFrame,
                               geo_points: pd.DataFrame,
                               zones: pd.DataFrame) -> pd.DataFrame:
    """xlsx 83건 사고를 geojson 좌표에 매핑. 매칭 우선순위: ym+구+사망+중상 → ym+구+중상 → ym+구 → 구 평균"""
    if accidents.empty or geo_points.empty or zones.empty:
        return pd.DataFrame()

    z_lats = zones["위도"].astype(float).values
    z_lons = zones["경도"].astype(float).values

    geo = geo_points.copy()
    geo["bjd_str"] = geo["bjd_cd"].fillna("").astype(str)

    acc = accidents.copy()
    acc["_ym"] = acc["발생년월"].apply(_parse_ym)

    results = []
    for _, row in acc.iterrows():
        ym    = row["_ym"]
        gu    = str(row.get("구", ""))
        death = int(row.get("사망자수", 0) or 0)
        seri  = int(row.get("중상자수", 0) or 0)
        minor = int(row.get("경상자수", 0) or 0)

        prefix = BJD_PREFIX.get(gu)
        if not prefix:
            continue
        bjd_mask = geo["bjd_str"].str.startswith(prefix)

        ym_mask = geo["acc_ym"] == ym

        # 매칭 단계: ym+구+사망+중상 → ym+구+중상 → ym+구 → 구 centroid
        for quality, cand_mask in [
            ("exact",       bjd_mask & ym_mask & (geo["death_cnt"] == death) & (geo["seri_cnt"] == seri)),
            ("approx_seri", bjd_mask & ym_mask & (geo["seri_cnt"] == seri)),
            ("approx_ym",   bjd_mask & ym_mask),
        ]:
            cands = geo[cand_mask]
            if not cands.empty:
                break
        else:
            # fallback: 구 보호구역 평균 좌표 + 미세 랜덤 오프셋
            sub_z = zones[zones["구"] == gu]
            if sub_z.empty:
                continue
            rng = np.random.default_rng(int(row.get("구분번호", ym or 0)) % (2**31))
            clat = float(sub_z["위도"].astype(float).mean()) + rng.uniform(-0.004, 0.004)
            clon = float(sub_z["경도"].astype(float).mean()) + rng.uniform(-0.004, 0.004)
            results.append(dict(lat=clat, lon=clon, 발생년월=row["발생년월"], 구=gu,
                                사망자수=death, 중상자수=seri, 경상자수=minor,
                                심각도="중상" if seri > 0 else "경상",
                                match_quality="fallback", location="구 평균 위치(추정)"))
            continue

        # 후보 중 어린이보호구역 가장 가까운 지점 선택
        c_lats = cands["lat"].values
        c_lons = cands["lon"].values
        c_locs = cands["location"].values
        min_d, best_i = np.inf, 0
        for i, (clat, clon) in enumerate(zip(c_lats, c_lons)):
            d = np.min(np.abs(z_lats - clat) + np.abs(z_lons - clon))
            if d < min_d:
                min_d, best_i = d, i

        results.append(dict(
            lat=float(c_lats[best_i]), lon=float(c_lons[best_i]),
            발생년월=row["발생년월"], 구=gu,
            사망자수=death, 중상자수=seri, 경상자수=minor,
            심각도="중상" if seri > 0 else "경상",
            match_quality=quality,
            location=str(c_locs[best_i]) if quality == "exact" else "근사 위치",
        ))

    return pd.DataFrame(results) if results else pd.DataFrame()


# ── 데이터 보강 ────────────────────────────────────────────────────────────────

def _norm(s: pd.Series) -> pd.Series:
    lo, hi = s.min(), s.max()
    return (s - lo) / (hi - lo) if hi > lo else pd.Series(0.0, index=s.index)


def _haversine_matrix(z_lats, z_lons, b_lats, b_lons):
    """위경도 배열 두 세트 간 거리 행렬(m) 반환 — pyproj 없을 때 폴백."""
    R = 6_371_000
    zla = np.radians(np.asarray(z_lats, dtype=float))[:, np.newaxis]
    zlo = np.radians(np.asarray(z_lons, dtype=float))[:, np.newaxis]
    bla = np.radians(np.asarray(b_lats, dtype=float))[np.newaxis, :]
    blo = np.radians(np.asarray(b_lons, dtype=float))[np.newaxis, :]
    a = np.sin((bla - zla) / 2) ** 2 + np.cos(zla) * np.cos(bla) * np.sin((blo - zlo) / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def enrich_zones(zones: pd.DataFrame, bus: pd.DataFrame, bump, slope_df: pd.DataFrame) -> pd.DataFrame:
    df = zones.copy()

    b_lon = pd.to_numeric(bus["경도"].astype(str).str.strip(), errors="coerce")
    b_lat = pd.to_numeric(bus["위도"].astype(str).str.strip(), errors="coerce")
    valid = b_lon.notna() & b_lat.notna()
    bus_c = bus[valid].reset_index(drop=True)

    if HAS_PYPROJ:
        tr = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
        z_x, z_y = tr.transform(df["경도"].astype(float).values, df["위도"].astype(float).values)
        zone_xy = np.column_stack([z_x, z_y])
        b_x, b_y = tr.transform(b_lon[valid].values, b_lat[valid].values)
        bus_xy = np.column_stack([b_x, b_y])
        diffs = zone_xy[:, np.newaxis, :] - bus_xy[np.newaxis, :, :]
        dists = np.sqrt((diffs**2).sum(axis=2))
    else:
        dists = _haversine_matrix(
            df["위도"].astype(float).values, df["경도"].astype(float).values,
            b_lat[valid].values, b_lon[valid].values,
        )

    route_cnt = (
        bus_c["시내버스_경유노선번호"].fillna("").astype(str)
        .apply(lambda x: len([r for r in x.split(",") if r.strip()])).values
    )

    nearest_idx = dists.argmin(axis=1)

    df["dist_nearest_stop_m"] = dists.min(axis=1)
    df["stops_in_300m"]       = (dists < 300).sum(axis=1)
    df["nearest_route_cnt"]   = route_cnt[nearest_idx]
    df["cctv_gap"]            = 0.0  # CCTV 지표 미사용

    if bump is not None and len(bump) > 0:
        bp_lon = pd.to_numeric(bump["경도"], errors="coerce").dropna()
        bp_lat = pd.to_numeric(bump["위도"], errors="coerce").dropna()
        if HAS_PYPROJ:
            bp_x, bp_y = tr.transform(bp_lon.values, bp_lat.values)
            bp_diffs = zone_xy[:, np.newaxis, :] - np.column_stack([bp_x, bp_y])[np.newaxis, :, :]
            bp_dists = np.sqrt((bp_diffs**2).sum(axis=2))
        else:
            bp_dists = _haversine_matrix(
                df["위도"].astype(float).values, df["경도"].astype(float).values,
                bp_lat.values, bp_lon.values,
            )
        df["bump_gap"] = (bp_dists.min(axis=1) > 200).astype(float)
    else:
        df["bump_gap"] = 0.0

    # 경사도 (DEM)
    slope_degs, slope_cats = [], []
    for _, row in df.iterrows():
        deg, cat = assign_zone_slope(float(row["위도"]), float(row["경도"]), slope_df)
        slope_degs.append(deg if deg is not None else 0.0)
        slope_cats.append(cat if cat is not None else 0)
    df["slope_deg"]      = slope_degs
    df["slope_category"] = slope_cats
    df["has_slope_data"] = not slope_df.empty

    # 버스 근접도 범주
    df["bus_category"] = pd.cut(
        df["dist_nearest_stop_m"],
        bins=[-np.inf, 30, 50, 100, np.inf],
        labels=[3, 2, 1, 0],
    ).astype(float)

    # 통계 기반 위험도 지수 (risk_analysis.py 결과 적용)
    # 방법 A: 사전 계산된 위험도 지수 파일 로드 (risk_analysis.py 실행 후 생성)
    _risk_merged = False
    _risk_csv = ROOT / "outputs" / "final_dataset_with_risk.csv"
    if _risk_csv.exists():
        try:
            _rdf = pd.read_csv(_risk_csv, encoding="utf-8-sig")
            _id_col = next((c for c in ["보호구역아이디", "zone_id"] if c in _rdf.columns and c in df.columns), None)
            if _id_col:
                _rdf = _rdf[[_id_col, "risk_index_scaled", "위험등급"]].rename(
                    columns={"risk_index_scaled": "risk_score"}
                )
                df = df.merge(_rdf, on=_id_col, how="left")
                df["risk_score"] = pd.to_numeric(df["risk_score"], errors="coerce").fillna(50.0)
                df["위험등급"]   = df["위험등급"].fillna("중위험")
                _risk_merged = True
        except Exception:
            pass

    # 방법 B: Spearman 가중합 폴백 (유의한 변수만 반영)
    if not _risk_merged:
        def _norm01(s: pd.Series) -> pd.Series:
            lo, hi = s.min(), s.max()
            return (s - lo) / (hi - lo) if hi > lo else pd.Series(0.5, index=s.index)

        _s = df["slope_category"].astype(float)
        _b = df["bus_category"].astype(float).fillna(0)
        _raw = _SPEARMAN_SLOPE_RHO * _norm01(_s)   # bus 비유의 → 제외
        _lo, _hi = _raw.min(), _raw.max()
        df["risk_score"] = (
            (_raw - _lo) / (_hi - _lo) * 100 if _hi > _lo
            else pd.Series(50.0, index=df.index)
        )
        p33 = df["risk_score"].quantile(0.33)
        p67 = df["risk_score"].quantile(0.67)
        df["위험등급"] = pd.cut(
            df["risk_score"],
            bins=[-np.inf, p33, p67, np.inf],
            labels=["저위험", "중위험", "고위험"],
        ).astype(str)

    return df


# ── 사고 좌표 CSV 내보내기 ──────────────────────────────────────────────────────

def export_accidents_with_coords(matched_accidents: pd.DataFrame, root: Path) -> Path | None:
    """matched_accidents → outputs/accidents_with_coords.csv (CLAUDE.md Step 1-2 명세)"""
    if matched_accidents.empty:
        return None
    rename_map = {
        "구분번호":      "accident_id",
        "구":           "district",
        "발생년월":      "date",
        "보호구역":      "address",
        "lat":          "lat",
        "lon":          "lng",
        "심각도":        "severity",
        "사망자수":      "deaths",
        "중상자수":      "serious",
        "경상자수":      "minor",
        "match_quality": "match_quality",
    }
    cols = [c for c in rename_map if c in matched_accidents.columns]
    out = matched_accidents[cols].rename(columns=rename_map)
    # 보호구역 컬럼이 없으면 location 컬럼으로 address 대체
    if "address" not in out.columns and "location" in matched_accidents.columns:
        out.insert(3, "address", matched_accidents["location"].values)
    out_dir = root / "outputs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "accidents_with_coords.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


# ── 데이터 로딩 ────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_data(root_str: str) -> dict:
    root = Path(root_str)
    files = find_source_files(root)

    zones = pd.DataFrame()
    if "zone" in files:
        f, enc = files["zone"]
        zones = pd.read_csv(f, encoding=enc)
        zones = zones[zones["시군구명"].str.contains("수정구|분당구", na=False)].copy()
        zones = zones[zones["시설종류"].isin(CHILD_FACILITY_TYPES)].copy()
        zones["구"] = zones["시군구명"].apply(
            lambda x: next((g for g in TARGET_GUS if g in str(x)), "기타")
        )

    bus = pd.DataFrame()
    if "bus" in files:
        f, enc = files["bus"]
        bus = pd.read_csv(f, encoding=enc)

    bump = None
    if "bump" in files:
        f, enc = files["bump"]
        bump = pd.read_csv(f, encoding=enc)

    slope_df = build_slope_df(root)

    if not zones.empty and not bus.empty:
        zones = enrich_zones(zones, bus, bump, slope_df)

    acc_frames = []
    for f in files.get("accident", []):
        try:
            df = pd.read_excel(f)
            df["발생년도"] = df["발생년월"].astype(str).str.extract(r"(\d{4})")[0].astype(int)
            df["구"] = df["시군구"].apply(
                lambda x: next((g for g in TARGET_GUS if g in str(x)), "기타")
            )
            acc_frames.append(df)
        except Exception:
            pass
    accidents = pd.concat(acc_frames, ignore_index=True) if acc_frames else pd.DataFrame()

    geo_points = pd.DataFrame()
    if "geojson" in files:
        try:
            with open(files["geojson"], encoding="utf-8") as fh:
                geo = json.load(fh)
            rows = []
            for feat in geo.get("features", []):
                p = feat.get("properties", {})
                lat, lon = p.get("wgs84_y_crd"), p.get("wgs84_x_crd")
                if lat and lon:
                    rows.append({
                        "lat": float(lat), "lon": float(lon),
                        "location": p.get("acc_plc", ""),
                        "death_cnt": int(p.get("death_cnt", 0) or 0),
                        "seri_cnt": int(p.get("seri_cnt", 0) or 0),
                        "acc_ym": int(p.get("acc_ym", 0) or 0),
                        "bjd_cd": str(p.get("bjd_cd", "") or ""),
                    })
            geo_points = pd.DataFrame(rows)
        except Exception:
            pass

    # 83건 사고 좌표: bundang/sujung_final_accidents.geojson 직접 로드 (정확한 좌표)
    _FINAL_ACC_FILES = [
        root / "bundang_final_accidents.geojson",
        root / "sujung_final_accidents.geojson",
    ]
    coord_frames = []
    for fpath in _FINAL_ACC_FILES:
        if not fpath.exists():
            continue
        try:
            with open(fpath, encoding="utf-8") as fh:
                gdata = json.load(fh)
            rows = []
            for feat in gdata.get("features", []):
                props = feat.get("properties", {})
                geom  = feat.get("geometry", {})
                coords = geom.get("coordinates", [None, None])
                if coords[0] is None or coords[1] is None:
                    continue
                lon, lat = float(coords[0]), float(coords[1])
                death = int(props.get("사망", 0) or 0)
                seri  = int(props.get("중상", 0) or 0)
                minor = int(props.get("경상", 0) or 0)
                acc_content = str(props.get("사고내용", ""))
                if death > 0 or "사망" in acc_content:
                    sev = "사망"
                elif seri > 0 or "중상" in acc_content:
                    sev = "중상"
                else:
                    sev = "경상"
                rows.append({
                    "lat": lat,
                    "lon": lon,
                    "발생년월": props.get("발생년월", ""),
                    "구": props.get("구", ""),
                    "사망자수": death,
                    "중상자수": seri,
                    "경상자수": minor,
                    "심각도": sev,
                    "match_quality": "exact",
                    "location": props.get("보호구역", ""),
                    "보호구역": props.get("보호구역", ""),
                    "구분번호": props.get("구분번호", ""),
                })
            if rows:
                coord_frames.append(pd.DataFrame(rows))
        except Exception:
            continue

    matched_accidents = pd.DataFrame()
    if coord_frames:
        matched_accidents = pd.concat(coord_frames, ignore_index=True)
        matched_accidents = matched_accidents.dropna(subset=["lat", "lon"])
    elif not accidents.empty and not geo_points.empty and not zones.empty:
        matched_accidents = match_accidents_to_coords(accidents, geo_points, zones)

    hotspots = pd.DataFrame()
    if not geo_points.empty and not zones.empty:
        gp = geo_points.copy()

        # 어린이보호구역 반경 200m 이내 사고만 필터링
        z_lats = zones["위도"].astype(float).values
        z_lons = zones["경도"].astype(float).values
        g_lats = gp["lat"].values
        g_lons = gp["lon"].values
        RADIUS = 0.0018  # 약 200m
        in_zone = np.zeros(len(gp), dtype=bool)
        for z_lat, z_lon in zip(z_lats, z_lons):
            in_zone |= (np.abs(g_lats - z_lat) < RADIUS) & (np.abs(g_lons - z_lon) < RADIUS)
        gp = gp[in_zone].copy()

        if not gp.empty:
            gp["lat_g"] = gp["lat"].round(3)
            gp["lon_g"] = gp["lon"].round(3)
            agg = (
                gp.groupby(["lat_g", "lon_g"])
                .agg(발생건수=("lat", "count"), 사망자수=("death_cnt", "sum"), 중상자수=("seri_cnt", "sum"))
                .reset_index().rename(columns={"lat_g": "위도", "lon_g": "경도"})
            )
            agg["사고지역위치명"] = "어린이보호구역 내 사고"
            hotspots = agg[agg["발생건수"] >= 1].sort_values("발생건수", ascending=False).head(200)

    # 구 행정경계 근사 (geojson 전체 사고 포인트 → convex hull)
    # 보호구역 점(47/79개)보다 사고 전체(3089/4702개)를 쓰면 훨씬 정확함
    boundaries: dict = {}
    if not geo_points.empty:
        from scipy.spatial import ConvexHull as _CHull
        GU_BJD = {gu: (lambda p: lambda b: b.startswith(p))(pfx)
                  for gu, pfx in BJD_PREFIX.items()}
        gp_b = geo_points.copy()
        gp_b["bjd_str"] = gp_b["bjd_cd"].fillna("").astype(str)
        for gu, pred in GU_BJD.items():
            sub = gp_b[gp_b["bjd_str"].apply(pred)]
            if len(sub) < 10:
                continue
            lats = sub["lat"].values
            lons = sub["lon"].values
            # 이상치 제거: 5~95 percentile
            lat_lo, lat_hi = np.percentile(lats, 2), np.percentile(lats, 98)
            lon_lo, lon_hi = np.percentile(lons, 2), np.percentile(lons, 98)
            mask = (lats >= lat_lo) & (lats <= lat_hi) & (lons >= lon_lo) & (lons <= lon_hi)
            sub_clean = sub[mask]
            if len(sub_clean) < 10:
                continue
            try:
                pts_xy = np.column_stack([sub_clean["lon"].values, sub_clean["lat"].values])
                hull = _CHull(pts_xy)
                hull_coords = [[sub_clean["lat"].values[i], sub_clean["lon"].values[i]]
                               for i in hull.vertices]
                hull_coords.append(hull_coords[0])
                boundaries[gu] = hull_coords
            except Exception:
                pass

    # CLAUDE.md Step 1-2: accidents_with_coords.csv 자동 생성
    if not matched_accidents.empty:
        export_accidents_with_coords(matched_accidents, root)

    return dict(zones=zones, bus=bus, accidents=accidents,
                hotspots=hotspots, slope=slope_df,
                matched_accidents=matched_accidents,
                boundaries=boundaries)


# ── 팝업 HTML 생성 ─────────────────────────────────────────────────────────────

def make_popup_html(row: pd.Series) -> str:
    grade     = str(row.get("위험등급", "저위험"))
    color     = RISK_COLOR.get(grade, "#2ca02c")
    bg        = RISK_BG.get(grade, "#f0fff4")
    score     = float(row.get("risk_score", 0))
    name      = str(row.get("대상시설명", "알 수 없음"))
    ftype     = str(row.get("시설종류", ""))
    gu        = str(row.get("구", ""))
    slope_cat = row.get("slope_category")
    has_slope = bool(row.get("has_slope_data", False))
    if has_slope and slope_cat is not None:
        slope_txt = SLOPE_LABEL.get(int(slope_cat), "알 수 없음")
        slope_deg = row.get("slope_deg", 0)
        slope_sub = f"{slope_deg:.1f}°"
    else:
        slope_txt = "데이터 없음"
        slope_sub = "DEM 미적용"

    dist      = float(row.get("dist_nearest_stop_m", 999))
    bus_cat   = int(row.get("bus_category", 0))
    bus_txt   = BUS_LABEL.get(bus_cat, "알 수 없음")
    stops_300 = int(row.get("stops_in_300m", 0))

    # 위험도 지수는 0~100 스케일 (risk_analysis.py 기준)
    score_pct = min(int(score), 100)
    bar_color = color

    html = f"""
<div style="font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
            width:290px; border-radius:10px; overflow:hidden;
            box-shadow:0 4px 16px rgba(0,0,0,0.18);">

  <!-- 헤더 -->
  <div style="background:{color}; padding:12px 14px;">
    <div style="font-size:15px; font-weight:700; color:#fff; line-height:1.3;">{name}</div>
    <div style="font-size:11px; color:rgba(255,255,255,0.85); margin-top:2px;">{ftype} &nbsp;|&nbsp; 성남시 {gu}</div>
  </div>

  <!-- 위험 점수 바 -->
  <div style="background:{bg}; padding:12px 14px 8px;">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
      <span style="background:{color}; color:#fff; font-size:12px; font-weight:700;
                   padding:3px 12px; border-radius:20px;">{grade}</span>
      <span style="font-size:20px; font-weight:800; color:{color};">{score:.3f}</span>
    </div>
    <div style="background:#e0e0e0; border-radius:4px; height:6px; overflow:hidden;">
      <div style="width:{score_pct}%; background:{bar_color}; height:100%;
                  border-radius:4px; transition:width 0.3s;"></div>
    </div>
    <div style="font-size:10px; color:#888; margin-top:3px; text-align:right;">
      위험도 지수 {score:.1f} / 100
    </div>
  </div>

  <!-- 상세 정보 -->
  <div style="background:#fff; padding:10px 14px; border-top:1px solid #f0f0f0;">
    <table style="width:100%; border-collapse:collapse; font-size:12px;">
      <tr style="border-bottom:1px solid #f5f5f5;">
        <td style="padding:7px 4px; color:#555; width:38%;">🏔️ 경사도</td>
        <td style="padding:7px 4px; font-weight:600; color:#333;">{slope_txt}</td>
        <td style="padding:7px 4px; color:#888; font-size:11px;">{slope_sub}</td>
      </tr>
      <tr style="border-bottom:1px solid #f5f5f5;">
        <td style="padding:7px 4px; color:#555;">🚌 버스정류장</td>
        <td style="padding:7px 4px; font-weight:600; color:#333;" colspan="2">{bus_txt}<br>
          <span style="font-size:11px; color:#888;">{dist:.0f}m · 300m내 {stops_300}개소</span>
        </td>
      </tr>
    </table>
  </div>
</div>
"""
    return html


# ── 구 경계 레이어 ─────────────────────────────────────────────────────────────

# 행정동 GeoJSON (hangjeongdong_경기도.geojson) 로드 — 앱 시작 시 1회만 실행
_HANGJEONG_DATA: dict | None = None

def _load_hangjeong() -> dict | None:
    global _HANGJEONG_DATA
    if _HANGJEONG_DATA is not None:
        return _HANGJEONG_DATA
    p = Path(__file__).parent / "hangjeongdong_경기도.geojson"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            _HANGJEONG_DATA = json.load(f)
    except Exception:
        _HANGJEONG_DATA = None
    return _HANGJEONG_DATA


def build_district_boundary_layer(zones: pd.DataFrame,
                                   boundaries: dict = None) -> folium.FeatureGroup:
    """수정구·분당구 경계를 그린다.
    hangjeongdong_경기도.geojson 행정 경계(정확)를 우선 사용하고,
    파일이 없으면 geojson 사고 convex hull → 보호구역 점 convex hull 순으로 fallback."""
    layer = folium.FeatureGroup(name="🗺️ 구 경계선", show=True)
    GU_STYLE = {
        "수정구": {"color": "#1565C0", "sgg": "41131"},
        "분당구": {"color": "#E91E63", "sgg": "41135"},
    }

    hangjeong = _load_hangjeong()

    for gu, style in GU_STYLE.items():
        color = style["color"]

        # ── 1순위: 행정동 GeoJSON 실제 경계 ──────────────────────────────────
        if hangjeong:
            gu_features = [
                feat for feat in hangjeong["features"]
                if str(feat["properties"].get("sgg", "")) == style["sgg"]
            ]
            if gu_features:
                fc = {"type": "FeatureCollection", "features": gu_features}

                folium.GeoJson(
                    fc,
                    style_function=lambda x, c=color: {
                        "color": c,
                        "weight": 2.5,
                        "dashArray": "6 4",
                        "fillColor": c,
                        "fillOpacity": 0.06,
                        "opacity": 0.9,
                    },
                    tooltip=f"성남시 {gu}",
                ).add_to(layer)

                # 레이블 중심 계산 (모든 polygon 좌표 평균)
                all_lats, all_lons = [], []
                for feat in gu_features:
                    geom = feat["geometry"]
                    rings = []
                    if geom["type"] == "Polygon":
                        rings = geom["coordinates"]
                    elif geom["type"] == "MultiPolygon":
                        for poly in geom["coordinates"]:
                            rings.extend(poly)
                    for ring in rings:
                        for lon, lat in ring:
                            all_lats.append(lat)
                            all_lons.append(lon)

                if all_lats:
                    clat = float(np.mean(all_lats))
                    clon = float(np.mean(all_lons))
                    folium.Marker(
                        location=[clat, clon],
                        icon=folium.DivIcon(
                            html=(
                                f'<div style="font-size:15px; font-weight:800; '
                                f'color:{color}; white-space:nowrap; '
                                f'text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,'
                                f'1px -1px 0 #fff,-1px 1px 0 #fff; '
                                f'border:2px solid {color}; '
                                f'background:rgba(255,255,255,0.75); '
                                f'padding:3px 8px; border-radius:6px;">'
                                f'성남시 {gu}</div>'
                            ),
                            icon_size=(110, 30),
                            icon_anchor=(55, 15),
                        ),
                    ).add_to(layer)
                continue  # 다음 구

        # ── 2순위: 사고 데이터 convex hull / 보호구역 점 convex hull (fallback) ──
        hull_coords = None

        if boundaries and gu in boundaries:
            hull_coords = boundaries[gu]

        if hull_coords is None:
            sub = zones[zones["구"] == gu].dropna(subset=["위도", "경도"])
            if len(sub) < 3:
                continue
            lats = sub["위도"].astype(float).values
            lons = sub["경도"].astype(float).values
            pts  = np.column_stack([lons, lats])
            try:
                from scipy.spatial import ConvexHull
                hull = ConvexHull(pts)
                hull_coords = [[lats[i], lons[i]] for i in hull.vertices]
                hull_coords.append(hull_coords[0])
            except Exception:
                hull_coords = [
                    [lats.min() - 0.005, lons.min() - 0.005],
                    [lats.min() - 0.005, lons.max() + 0.005],
                    [lats.max() + 0.005, lons.max() + 0.005],
                    [lats.max() + 0.005, lons.min() - 0.005],
                    [lats.min() - 0.005, lons.min() - 0.005],
                ]

        folium.Polygon(
            locations=hull_coords,
            color=color,
            weight=2.5,
            dash_array="6 4",
            fill=True,
            fill_color=color,
            fill_opacity=0.04,
            tooltip=f"성남시 {gu}",
        ).add_to(layer)

        c_lats = [c[0] for c in hull_coords]
        c_lons = [c[1] for c in hull_coords]
        clat = float(np.mean(c_lats))
        clon = float(np.mean(c_lons))
        folium.Marker(
            location=[clat, clon],
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:15px; font-weight:800; '
                    f'color:{color}; white-space:nowrap; '
                    f'text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,'
                    f'1px -1px 0 #fff,-1px 1px 0 #fff; '
                    f'border:2px solid {color}; '
                    f'background:rgba(255,255,255,0.75); '
                    f'padding:3px 8px; border-radius:6px;">'
                    f'성남시 {gu}</div>'
                ),
                icon_size=(110, 30),
                icon_anchor=(55, 15),
            ),
        ).add_to(layer)

    return layer


# ── 행정동 라벨 레이어 ────────────────────────────────────────────────────────

def build_dong_label_layer() -> folium.FeatureGroup:
    """수정구·분당구 행정동 이름 라벨을 지도에 표시한다."""
    layer = folium.FeatureGroup(name="🏘️ 행정동 라벨", show=True)
    hangjeong = _load_hangjeong()
    if not hangjeong:
        return layer

    GU_SGG = {"수정구": ("41131", "#1565C0"), "분당구": ("41135", "#C2185B")}

    for gu, (sgg_code, color) in GU_SGG.items():
        gu_features = [
            feat for feat in hangjeong["features"]
            if str(feat["properties"].get("sgg", "")) == sgg_code
        ]
        for feat in gu_features:
            adm_nm = feat["properties"].get("adm_nm", "")
            dong_name = adm_nm.split()[-1] if adm_nm.split() else adm_nm

            # 가장 큰 폴리곤의 exterior ring 중심 계산
            geom = feat["geometry"]
            best_ring = None
            best_len = 0
            if geom["type"] == "Polygon":
                rings = [geom["coordinates"][0]]
            else:  # MultiPolygon
                rings = [poly[0] for poly in geom["coordinates"]]
            for ring in rings:
                if len(ring) > best_len:
                    best_len = len(ring)
                    best_ring = ring

            if not best_ring:
                continue
            clat = float(np.mean([pt[1] for pt in best_ring]))
            clon = float(np.mean([pt[0] for pt in best_ring]))

            folium.Marker(
                location=[clat, clon],
                icon=folium.DivIcon(
                    html=(
                        f'<div style="font-size:10px; font-weight:600; '
                        f'color:{color}; white-space:nowrap; '
                        f'text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,'
                        f'1px -1px 0 #fff,-1px 1px 0 #fff; '
                        f'background:rgba(255,255,255,0.65); '
                        f'padding:1px 5px; border-radius:3px; '
                        f'pointer-events:none;">'
                        f'{dong_name}</div>'
                    ),
                    icon_size=(90, 20),
                    icon_anchor=(45, 10),
                ),
                tooltip=f"성남시 {gu} {dong_name}",
            ).add_to(layer)

    return layer


# ── Folium 지도 생성 ───────────────────────────────────────────────────────────

def build_map(zones: pd.DataFrame, bus: pd.DataFrame,
              hotspots: pd.DataFrame, slope_df: pd.DataFrame,
              filter_gu: str = "전체", filter_grade: list = None,
              show_boundary: bool = True, show_grid: bool = True,
              matched_accidents: pd.DataFrame = None,
              show_accidents: bool = False,
              boundaries: dict = None,
              show_dong_labels: bool = False) -> folium.Map:

    sub = zones.copy()
    if filter_gu != "전체":
        sub = sub[sub["구"] == filter_gu]
    if filter_grade:
        sub = sub[sub["위험등급"].isin(filter_grade)]

    if sub.empty:
        clat, clon = 37.43, 127.14
    else:
        clat = sub["위도"].astype(float).mean()
        clon = sub["경도"].astype(float).mean()

    # CartoDB Voyager: 한국어 지명 선명, 도로 상세, 데이터 레이어와 조화
    _TILE_URL  = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
    _TILE_ATTR = "© <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors © <a href='https://carto.com/attributions'>CARTO</a>"

    m = folium.Map(
        location=[clat, clon],
        zoom_start=14 if filter_gu != "전체" else 13,
        tiles=_TILE_URL,
        attr=_TILE_ATTR,
        max_zoom=19,
    )

    # ── 레이어 0: 구 경계선 ──────────────────────────────────────────────────
    if show_boundary:
        boundary_layer = build_district_boundary_layer(zones, boundaries=boundaries)
        boundary_layer.control = False
        boundary_layer.add_to(m)

    # ── 레이어 0b: 행정동 라벨 ───────────────────────────────────────────────
    if show_dong_labels:
        dong_layer = build_dong_label_layer()
        dong_layer.control = False
        dong_layer.add_to(m)

    # ── 레이어 1: 보호구역 위험도 격자 ────────────────────────────────────────
    if show_grid:
        layer_grid = folium.FeatureGroup(name="🟥 보호구역 위험도 격자", show=True, control=False)

        for _, row in sub.iterrows():
            lat  = float(row["위도"])
            lon  = float(row["경도"])
            grade = str(row.get("위험등급", "저위험"))
            color = RISK_COLOR.get(grade, "#2ca02c")
            fill_opacity = RISK_FILL.get(grade, 0.55)
            popup_html = make_popup_html(row)

            folium.Rectangle(
                bounds=[[lat - GRID_DLAT, lon - GRID_DLON],
                        [lat + GRID_DLAT, lon + GRID_DLON]],
                color=color,
                weight=1.5,
                fill=True,
                fill_color=color,
                fill_opacity=fill_opacity,
                popup=folium.Popup(popup_html, max_width=310),
                tooltip=folium.Tooltip(
                    f"<b>{row.get('대상시설명', '')}</b><br>"
                    f"{RISK_EMOJI.get(grade, '')} {grade} &nbsp; 점수: {float(row.get('risk_score', 0)):.3f}",
                    sticky=False,
                ),
            ).add_to(layer_grid)

            # 중심 아이콘
            folium.CircleMarker(
                location=[lat, lon],
                radius=3,
                color="#fff",
                fill=True,
                fill_color=color,
                fill_opacity=1.0,
                weight=1,
            ).add_to(layer_grid)

        layer_grid.add_to(m)

    # ── 레이어 3: 어린이보호구역 사고 83건 ──────────────────────────────────────
    if show_accidents and matched_accidents is not None and not matched_accidents.empty:
        ma = matched_accidents.copy()
        if filter_gu != "전체":
            ma = ma[ma["구"] == filter_gu]

        layer_acc = folium.FeatureGroup(name="📍 어린이보호구역 사고", show=True, control=False)

        SEV_COLOR  = {"사망": "#1a1a1a", "중상": "#d62728", "경상": "#ff7f0e"}
        SEV_EMOJI  = {"사망": "💀", "중상": "🚨", "경상": "⚠️"}
        SEV_SIZE   = {"사망": 30, "중상": 28, "경상": 24}

        for _, ar in ma.iterrows():
            sev    = str(ar.get("심각도", "경상"))
            color  = SEV_COLOR.get(sev, "#ff7f0e")
            emoji  = SEV_EMOJI.get(sev, "⚠️")
            sz     = SEV_SIZE.get(sev, 24)
            death  = int(ar.get("사망자수", 0) or 0)
            seri   = int(ar.get("중상자수", 0) or 0)
            minor  = int(ar.get("경상자수", 0) or 0)
            ym_str = str(ar.get("발생년월", ""))
            gu_str = str(ar.get("구", ""))
            mq     = str(ar.get("match_quality", ""))
            mq_label = {"exact": "정밀매칭", "approx_seri": "근사매칭", "approx_ym": "월별추정", "fallback": "구 평균추정"}.get(mq, mq)

            school_zone = str(ar.get("보호구역", "") or ar.get("location", "")).strip()
            acc_type = str(ar.get("사고유형", "")).strip()
            road_row = f'<tr><td style="color:#555; padding:3px 0; width:38%;">🏫 보호구역</td><td style="font-weight:600; font-size:11px;">{school_zone}</td></tr>' if school_zone else ""
            type_row = f'<tr><td style="color:#555; padding:3px 0;">🚗 사고유형</td><td style="font-size:11px;">{acc_type}</td></tr>' if acc_type else ""
            popup_html = f"""
<div style="font-family:'Malgun Gothic',sans-serif; width:240px; font-size:12px; line-height:1.6;">
  <div style="background:{color}; color:#fff; padding:8px 12px; border-radius:6px 6px 0 0;
              font-weight:700; font-size:13px;">{emoji} 어린이보호구역 사고</div>
  <div style="padding:10px 12px; border:1px solid #eee; border-top:none; border-radius:0 0 6px 6px;">
    <table style="width:100%; border-collapse:collapse;">
      <tr><td style="color:#555; padding:3px 0; width:38%;">📅 발생년월</td><td style="font-weight:600;">{ym_str}</td></tr>
      <tr><td style="color:#555; padding:3px 0;">📍 구</td><td style="font-weight:600;">성남시 {gu_str}</td></tr>
      {road_row}
      {type_row}
      <tr><td style="color:#555; padding:3px 0;">⚠️ 심각도</td>
          <td><span style="background:{color}; color:#fff; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:700;">{sev}</span></td></tr>
      <tr><td style="color:#555; padding:3px 0;">💀 사망자</td><td>{death}명</td></tr>
      <tr><td style="color:#555; padding:3px 0;">🏥 중상자</td><td>{seri}명</td></tr>
      <tr><td style="color:#555; padding:3px 0;">🩹 경상자</td><td>{minor}명</td></tr>
      <tr><td colspan="2" style="color:#aaa; font-size:10px; padding-top:6px; border-top:1px solid #f0f0f0;">
        위치 정확도: {mq_label}</td></tr>
    </table>
  </div>
</div>"""

            icon_html = (
                f'<div style="'
                f'width:{sz}px; height:{sz}px; '
                f'background:{color}; border-radius:50% 50% 50% 0; '
                f'transform:rotate(-45deg); '
                f'border:2px solid #fff; '
                f'box-shadow:0 2px 6px rgba(0,0,0,0.35); '
                f'display:flex; align-items:center; justify-content:center;">'
                f'<span style="transform:rotate(45deg); font-size:{sz - 10}px; line-height:1;">{emoji}</span>'
                f'</div>'
            )
            folium.Marker(
                location=[float(ar["lat"]), float(ar["lon"])],
                icon=folium.DivIcon(
                    html=icon_html,
                    icon_size=(sz, sz),
                    icon_anchor=(sz // 2, sz),
                ),
                popup=folium.Popup(popup_html, max_width=240),
                tooltip=folium.Tooltip(
                    f"{emoji} {sev} | {ym_str} | 성남시 {gu_str}",
                    sticky=False,
                ),
            ).add_to(layer_acc)

        layer_acc.add_to(m)

        # 사고 범례 추가
        acc_legend = f"""
<div style="position:fixed; bottom:30px; right:60px; z-index:9999;
            background:rgba(255,255,255,0.95); border-radius:10px;
            padding:10px 14px; box-shadow:0 2px 12px rgba(0,0,0,0.18);
            font-family:'Malgun Gothic',sans-serif; font-size:11px; min-width:110px;">
  <div style="font-weight:700; margin-bottom:6px; color:#333;">사고 심각도</div>
  <div style="display:flex; align-items:center; gap:7px; margin-bottom:4px;">
    <span style="font-size:16px;">🚨</span><span>중상</span>
  </div>
  <div style="display:flex; align-items:center; gap:7px;">
    <span style="font-size:16px;">⚠️</span><span>경상</span>
  </div>
  <div style="color:#888; font-size:10px; margin-top:6px; border-top:1px solid #eee; padding-top:4px;">
    총 {len(ma)}/83건 표시<br>
    <span style="font-size:9px;">(1건 미표시: 구분번호 2022035846,<br>경상 4명 — POINT만 있는 보호구역 근처)</span>
  </div>
</div>"""
        m.get_root().html.add_child(folium.Element(acc_legend))

    # 범례 HTML
    if show_grid:
        legend_html = """
<div style="position:fixed; bottom:30px; left:12px; z-index:9999;
            background:rgba(255,255,255,0.95); border-radius:10px;
            padding:12px 16px; box-shadow:0 2px 12px rgba(0,0,0,0.18);
            font-family:'Malgun Gothic',sans-serif; font-size:12px; min-width:130px;">
  <div style="font-weight:700; margin-bottom:8px; color:#333;">위험도 범례</div>
  <div style="display:flex; align-items:center; gap:8px; margin-bottom:5px;">
    <div style="width:20px; height:14px; background:#d62728; border-radius:3px; opacity:0.78;"></div>
    <span>고위험</span>
  </div>
  <div style="display:flex; align-items:center; gap:8px; margin-bottom:5px;">
    <div style="width:20px; height:14px; background:#ff7f0e; border-radius:3px; opacity:0.65;"></div>
    <span>중위험</span>
  </div>
  <div style="display:flex; align-items:center; gap:8px;">
    <div style="width:20px; height:14px; background:#2ca02c; border-radius:3px; opacity:0.55;"></div>
    <span>저위험</span>
  </div>
</div>
"""
        m.get_root().html.add_child(folium.Element(legend_html))

    Fullscreen(position="topright").add_to(m)
    # Use default MiniMap with built‑in OpenStreetMap tiles (has attribution)
    MiniMap(position="bottomright", toggle_display=True).add_to(m)

    return m


# ── Plotly 차트 ────────────────────────────────────────────────────────────────

def chart_risk_dist(zones: pd.DataFrame):
    df = (
        zones.assign(위험등급=lambda d: d["위험등급"].astype(str))
        .groupby(["구", "위험등급"]).size().reset_index(name="구역수")
    )
    fig = px.bar(
        df, x="위험등급", y="구역수", color="구", barmode="group",
        category_orders={"위험등급": ["고위험", "중위험", "저위험"]},
        color_discrete_map={"수정구": "#1565C0", "분당구": "#E91E63"},
        text_auto=True, title="수정구 · 분당구 위험등급 분포",
    )
    fig.update_layout(height=300, margin=dict(t=40, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=-0.2))
    return fig


def chart_score_hist(zones: pd.DataFrame):
    fig = px.histogram(
        zones, x="risk_score", color="구", nbins=20, barmode="overlay",
        color_discrete_map={"수정구": "#1565C0", "분당구": "#E91E63"},
        title="위험점수 분포",
        labels={"risk_score": "위험점수", "count": "구역 수"},
    )
    fig.update_layout(height=300, margin=dict(t=40, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=-0.2))
    return fig


def chart_accident_year(accidents: pd.DataFrame):
    if accidents.empty or "발생년도" not in accidents.columns:
        return go.Figure()
    by_year = accidents.groupby(["발생년도", "구"]).size().reset_index(name="건수")
    fig = px.bar(
        by_year, x="발생년도", y="건수", color="구", barmode="group",
        color_discrete_map={"수정구": "#1565C0", "분당구": "#E91E63"},
        text_auto=True, title="연도별 사고 건수",
    )
    fig.update_layout(height=280, margin=dict(t=40, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=-0.2))
    return fig


def chart_bus_vs_risk(zones: pd.DataFrame):
    fig = px.scatter(
        zones, x="dist_nearest_stop_m", y="risk_score",
        color="위험등급", size="stops_in_300m",
        color_discrete_map=RISK_COLOR,
        hover_name="대상시설명",
        hover_data={"구": True, "시설종류": True},
        labels={"dist_nearest_stop_m": "최근접 버스정류장 거리 (m)", "risk_score": "위험점수"},
        title="버스정류장 거리 vs 위험점수",
    )
    fig.update_layout(height=300, margin=dict(t=40, b=10, l=10, r=10))
    return fig


# ── 사이드바 ───────────────────────────────────────────────────────────────────

def render_sidebar(zones: pd.DataFrame, matched_accidents: pd.DataFrame = None):
    st.sidebar.markdown("""
    <div style="padding:12px 4px 14px; border-bottom:1px solid rgba(255,255,255,0.15); margin-bottom:8px;">
      <div style="font-size:0.65rem; letter-spacing:0.12em; text-transform:uppercase;
                  color:#7fc8a9; font-weight:700; margin-bottom:4px;">
        가천대학교 스마트시티학과
      </div>
      <div style="font-size:0.92rem; font-weight:800; color:#ffffff; line-height:1.3;">
        🚸 어린이보호구역<br>교통안전 위험 분석
      </div>
      <div style="font-size:0.68rem; color:#94b3d1; margin-top:4px;">
        수정구 · 분당구 &nbsp;|&nbsp; 2020–2022
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.sidebar.markdown("## 🔍 필터")

    filter_gu = st.sidebar.radio(
        "행정구 선택",
        options=["전체", "수정구", "분당구"],
        horizontal=True,
    )

    all_grades = ["고위험", "중위험", "저위험"]
    filter_grade = st.sidebar.multiselect(
        "위험등급 필터",
        options=all_grades,
        default=all_grades,
    )

    st.sidebar.divider()
    st.sidebar.markdown("## 🗺️ 지도 레이어 설정")
    show_boundary = st.sidebar.toggle("구 경계선", value=True, key="toggle_boundary")
    show_dong_labels = st.sidebar.toggle("행정동 라벨", value=False, key="toggle_dong_labels")
    show_grid = st.sidebar.toggle("어린이 보호구역 위험도 격자", value=True, key="toggle_grid")

    n_loaded = len(matched_accidents) if matched_accidents is not None and not matched_accidents.empty else 0
    acc_label = f"📍 공식 사고 표시 ({n_loaded}/83건)" if n_loaded > 0 else "📍 공식 사고 표시 (파일 없음)"
    show_accidents = st.sidebar.toggle(acc_label, value=True, key="toggle_accidents83")
    if n_loaded > 0 and n_loaded < 83:
        st.sidebar.caption(
            f"※ 총 83건 중 {83 - n_loaded}건 미표시 "
            f"(구분번호 2022035846, 경상 4명 — POINT만 존재하는 보호구역 근처 발생으로 격자 미매칭)"
        )

    st.sidebar.divider()
    st.sidebar.markdown("## 📊 통계 요약")

    sub = zones.copy()
    if filter_gu != "전체":
        sub = sub[sub["구"] == filter_gu]
    if filter_grade:
        sub_f = sub[sub["위험등급"].isin(filter_grade)]
    else:
        sub_f = sub

    for grade in ["고위험", "중위험", "저위험"]:
        cnt = (sub["위험등급"].astype(str) == grade).sum()
        color = RISK_COLOR[grade]
        st.sidebar.markdown(
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">'
            f'<div style="width:12px;height:12px;background:{color};border-radius:2px;"></div>'
            f'<span style="font-size:13px;">{grade}: <b>{cnt}개소</b></span></div>',
            unsafe_allow_html=True,
        )

    st.sidebar.divider()
    st.sidebar.markdown("## 🤖 AI 분석 설정")
    # secrets.toml / Streamlit Cloud Secrets에서 읽기
    try:
        _secret_naver_id     = st.secrets.get("NAVER_CLIENT_ID", "")
        _secret_naver_secret = st.secrets.get("NAVER_CLIENT_SECRET", "")
        _secret_claude_key   = st.secrets.get("CLAUDE_API_KEY", "")
    except Exception:
        _secret_naver_id = _secret_naver_secret = _secret_claude_key = ""

    _all_secrets_loaded = bool(_secret_claude_key and _secret_naver_id and _secret_naver_secret)

    if _all_secrets_loaded:
        # secrets에 모든 키가 있으면 입력창 숨기고 자동 사용
        naver_client_id     = _secret_naver_id
        naver_client_secret = _secret_naver_secret
        claude_api_key      = _secret_claude_key
        st.sidebar.success("🔑 API 키 자동 연결됨", icon="✅")
        st.sidebar.caption("Claude · Naver Maps 키가 서버에서 자동으로 로드되었습니다.")
    else:
        # secrets 없으면 수동 입력창 표시 (로컬 개발용)
        naver_client_id = st.sidebar.text_input(
            "Naver Maps Client ID",
            value=st.session_state.get("_val_naver_id", _secret_naver_id),
            placeholder="ncpKeyId...",
            help="NCP 콘솔에서 발급받은 Maps API Client ID.",
        )
        st.session_state["_val_naver_id"] = naver_client_id

        naver_client_secret = st.sidebar.text_input(
            "Naver Maps Client Secret",
            value=st.session_state.get("_val_naver_secret", _secret_naver_secret),
            placeholder="Secret Key...",
            help="NCP 콘솔에서 발급받은 Maps API Client Secret.",
        )
        st.session_state["_val_naver_secret"] = naver_client_secret

        claude_api_key = st.sidebar.text_input(
            "Claude API Key",
            value=st.session_state.get("_val_claude", _secret_claude_key),
            placeholder="sk-ant-...",
            type="password",
            help="Anthropic Claude API Key. 하단 안전이 AI 상담챗봇에서 사용됩니다.",
        )
        st.session_state["_val_claude"] = claude_api_key

    st.sidebar.divider()
    st.sidebar.markdown("**💡 사용 방법**")
    st.sidebar.caption("지도의 색상 격자를 **클릭**하면 보호구역 상세 정보가 팝업으로 나타납니다.")
    st.sidebar.caption("사이드바의 '지도 레이어 설정' 토글을 통해 원하는 레이어를 켜고 끌 수 있습니다.")

    return filter_gu, filter_grade, claude_api_key, naver_client_id, naver_client_secret, claude_api_key, show_boundary, show_dong_labels, show_grid, show_accidents


# ── 선택된 구역 패널 ───────────────────────────────────────────────────────────

def render_selected_zone(click_data, zones: pd.DataFrame):
    """st_folium 클릭 반환값으로 구역 상세 패널 표시."""
    if not click_data or not click_data.get("last_object_clicked_tooltip"):
        st.info("지도의 격자 셀을 클릭하면 여기에 상세 정보가 표시됩니다.")
        return

    tooltip = click_data["last_object_clicked_tooltip"]
    # 툴팁에서 이름 파싱
    name = tooltip.split("<b>")[-1].split("</b>")[0] if "<b>" in tooltip else ""
    if not name:
        return

    match = zones[zones["대상시설명"].str.contains(name, na=False, regex=False)]
    if match.empty:
        return

    row = match.iloc[0]
    grade = str(row.get("위험등급", "저위험"))
    color = RISK_COLOR.get(grade, "#2ca02c")
    score = float(row.get("risk_score", 0))

    st.markdown(
        f'<div style="border-left:4px solid {color}; padding:8px 14px; '
        f'background:{RISK_BG.get(grade,"#fff")}; border-radius:0 8px 8px 0; margin-bottom:8px;">'
        f'<b style="font-size:15px;">{row.get("대상시설명","")}</b><br>'
        f'<span style="color:#666;font-size:12px;">{row.get("시설종류","")} · 성남시 {row.get("구","")}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("위험등급", f"{RISK_EMOJI.get(grade,'')} {grade}")
    c2.metric("위험점수", f"{score:.3f}")
    c3.metric("300m내 정류장", f"{int(row.get('stops_in_300m', 0))}개")

    slope_cat = row.get("slope_category")
    has_slope = bool(row.get("has_slope_data", False))
    slope_txt = SLOPE_LABEL.get(int(slope_cat), "알 수 없음") if (has_slope and slope_cat is not None) else "데이터 없음"
    bus_cat = int(row.get("bus_category", 0)) if pd.notna(row.get("bus_category")) else 0
    bus_txt = BUS_LABEL.get(bus_cat, "알 수 없음")
    dist = float(row.get("dist_nearest_stop_m", 0))

    st.markdown("**세부 지표**")
    info_cols = st.columns(2)
    with info_cols[0]:
        st.markdown(f"🏔️ **경사도** `{slope_txt}`")
        st.markdown(f"🚌 **버스** `{bus_txt}`")
    with info_cols[1]:
        st.markdown(f"📏 **거리** `{dist:.0f}m`")
        st.markdown(f"📍 **경유노선** `{int(row.get('nearest_route_cnt', 0))}개`")


# ── 선택 구역 파싱 헬퍼 ───────────────────────────────────────────────────────

def _parse_selected_zone(click_data, zones: pd.DataFrame) -> pd.Series | None:
    if not click_data:
        return None

    # 1) tooltip 텍스트로 매칭
    tooltip = click_data.get("last_object_clicked_tooltip") or ""
    if tooltip and "<b>" in tooltip:
        name = tooltip.split("<b>")[-1].split("</b>")[0]
        if name:
            match = zones[zones["대상시설명"].str.contains(name, na=False, regex=False)]
            if not match.empty:
                return match.iloc[0]

    # 2) 클릭 좌표로 가장 가까운 구역 매칭 (fallback)
    clicked = click_data.get("last_object_clicked") or {}
    if clicked.get("lat") and clicked.get("lng"):
        clat, clng = float(clicked["lat"]), float(clicked["lng"])
        z = zones.copy()
        z["_d"] = ((z["위도"].astype(float) - clat) ** 2 +
                   (z["경도"].astype(float) - clng) ** 2) ** 0.5
        nearest = z.nsmallest(1, "_d")
        if not nearest.empty and nearest.iloc[0]["_d"] < 0.02:
            return nearest.iloc[0]

    return None


# ── 안전이 자동 코멘트 (API 없을 때 템플릿) ────────────────────────────────────

def _template_comment(row: pd.Series) -> str:
    grade  = str(row.get("위험등급", "저위험"))
    name   = str(row.get("대상시설명", "이 구역"))
    score  = float(row.get("risk_score", 0))
    dist   = float(row.get("dist_nearest_stop_m", 999))
    stops  = int(row.get("stops_in_300m", 0))
    gu     = str(row.get("구", ""))
    bus_cat = int(row.get("bus_category", 0)) if pd.notna(row.get("bus_category")) else 0

    grade_msg = {
        "고위험": f"**{name}**은 위험점수 **{score:.3f}**로 즉각적인 안전 개선이 필요한 곳이에요! ⚠️",
        "중위험": f"**{name}**은 위험점수 **{score:.3f}**로 지속 모니터링이 필요한 구역이에요.",
        "저위험": f"**{name}**은 위험점수 **{score:.3f}**로 현재 상대적으로 안전한 편이에요. ✅",
    }.get(grade, f"**{name}**을 분석했어요.")

    bus_msg = {
        3: f"버스정류장이 **{dist:.0f}m** 거리에 매우 가까워 등하굣길 보행자 혼잡이 우려돼요.",
        2: f"버스정류장이 **{dist:.0f}m**로 인접해 있어요.",
        1: f"버스정류장까지 **{dist:.0f}m**예요.",
        0: f"버스정류장은 **{dist:.0f}m**로 비교적 멀어요.",
    }.get(bus_cat, "")

    gu_note = "수정구 구도심 특성상 경사가 가파를 수 있어요." if gu == "수정구" else "분당구 신도시 지역으로 지형은 평탄한 편이에요."

    return f"{grade_msg}\n\n300m 내 정류장 **{stops}개** — {bus_msg}\n\n{gu_note}"


# ── 안전이 캐릭터 챗봇 ─────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=300)
def fetch_naver_pano_image(lat: float, lon: float, client_id: str, client_secret: str) -> bytes | None:
    """Naver 파노라마 Static API로 로드뷰 이미지를 서버에서 직접 가져온다."""
    try:
        import requests as _req
        resp = _req.get(
            "https://naveropenapi.apigw.naver.com/map-pano/v2/pano",
            params={"lat": lat, "lng": lon, "w": 640, "h": 360, "pan": 0, "tlt": 0, "fov": 100},
            headers={
                "X-NCP-APIGW-API-KEY-ID": client_id,
                "X-NCP-APIGW-API-KEY": client_secret,
            },
            timeout=8,
        )
        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", ""):
            return resp.content
    except Exception:
        pass
    return None


def _unused_noop():
    pass  # 이전 render_character_chatbot v1 제거됨

    # ── (이하 제거됨) ────────────────────────────────────────────────────────
    if False:
        st.markdown("""placeholder""")

    # ── 말풍선 (자동 코멘트) ────────────────────────────────────────────────
    auto_text = st.session_state.safetybot_auto_comment or \
        "안녕하세요! 저는 **안전이**예요 🚸\n\n지도에서 보호구역 격자를 클릭하면\n제가 바로 분석해드릴게요!"
    st.markdown(
        f'<div style="background:#EEF6FF; border-radius:4px 14px 14px 14px; '
        f'padding:10px 13px; font-size:12.5px; line-height:1.6; color:#1a1a2e; '
        f'border:1px solid #BBDEFB; margin-bottom:10px;">{auto_text.replace(chr(10),"<br>")}</div>',
        unsafe_allow_html=True,
    )

    # ── 로드뷰 이미지 (자동 or 업로드) ──────────────────────────────────────
    pano_bytes = None
    uploaded = None
    if selected_row is not None and naver_client_id and naver_client_secret:
        with st.spinner("📸 로드뷰 이미지 로딩 중…"):
            pano_bytes = fetch_naver_pano_image(
                float(selected_row["위도"]), float(selected_row["경도"]),
                naver_client_id, naver_client_secret,
            )
        if pano_bytes:
            st.image(pano_bytes, caption="📸 네이버 로드뷰 (자동 분석 가능)", use_container_width=True)
        else:
            st.caption("⚠️ 로드뷰 이미지를 가져오지 못했습니다. NCP 콘솔 설정을 확인하세요.")
            uploaded = st.file_uploader("📸 스크린샷 직접 업로드", type=["png","jpg","jpeg"],
                                        label_visibility="collapsed")
            if uploaded:
                st.image(uploaded, use_container_width=True)
    else:
        uploaded = st.file_uploader("📸 로드뷰 스크린샷 업로드", type=["png","jpg","jpeg"],
                                    label_visibility="collapsed",
                                    help="왼쪽 로드뷰를 스크린샷 찍어 올리면 안전이가 분석해요.\n"
                                         "사이드바에 Naver Client ID·Secret을 입력하면 자동으로 가져옵니다.")
        if uploaded:
            st.image(uploaded, use_container_width=True)

    # ── 채팅 기록 ────────────────────────────────────────────────────────────
    chat_container = st.container(height=220)
    with chat_container:
        for msg in st.session_state.safetybot_history:
            if msg["role"] == "user":
                st.markdown(
                    f'<div style="text-align:right; margin:4px 0;">'
                    f'<span style="background:#1565C0; color:#fff; padding:6px 11px; '
                    f'border-radius:14px 14px 4px 14px; font-size:12px; display:inline-block; '
                    f'max-width:85%;">{msg["text"]}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div style="display:flex; gap:6px; margin:4px 0;">'
                    f'<span style="font-size:18px; flex-shrink:0;">🚸</span>'
                    f'<span style="background:#F1F8FF; padding:6px 11px; border-radius:4px 14px 14px 14px; '
                    f'font-size:12px; line-height:1.5; display:inline-block; max-width:85%; '
                    f'border:1px solid #BBDEFB;">{msg["text"]}</span></div>',
                    unsafe_allow_html=True,
                )

    # ── 입력창 ───────────────────────────────────────────────────────────────
    if not claude_api_key or len(claude_api_key.strip()) < 10:
        st.caption("💡 사이드바에 Claude API Key를 입력하면 안전이에게 직접 질문할 수 있어요.")
        return

    if not HAS_ANTHROPIC:
        st.error("`anthropic` 패키지가 없습니다. `pip install anthropic` 후 재시작하세요.")
        return

    user_input = st.chat_input("안전이에게 질문하세요…")
    if user_input and user_input.strip():
        # 기존 이력 → API 메시지 형식 변환 (현재 사용자 입력 추가 전)
        claude_messages = [{"role": m["role"], "content": m["text"]}
                           for m in st.session_state.safetybot_history]

        # 현재 사용자 메시지 (이미지 포함 시 멀티모달)
        if pano_bytes:
            img_b64 = base64.standard_b64encode(pano_bytes).decode()
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": user_input},
            ]
        elif uploaded:
            uploaded.seek(0)
            img_b64 = base64.standard_b64encode(uploaded.read()).decode()
            ext = uploaded.name.rsplit(".", 1)[-1].lower()
            media_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": user_input},
            ]
        else:
            user_content = user_input
        claude_messages.append({"role": "user", "content": user_content})

        # 사용자 메시지를 이력에 등록
        st.session_state.safetybot_history.append({"role": "user", "text": user_input})

        # 스트리밍 응답 표시 영역
        stream_placeholder = st.empty()
        full_response = ""

        try:
            client = anthropic.Anthropic(api_key=claude_api_key.strip())

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=600,
                system=_build_system_prompt(zones, selected_row),
                messages=claude_messages,
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    stream_placeholder.markdown(
                        f'<div style="display:flex;gap:6px;margin:4px 0;">'
                        f'<span style="font-size:18px;flex-shrink:0;">🚸</span>'
                        f'<span style="background:#F1F8FF;padding:6px 11px;'
                        f'border-radius:4px 14px 14px 14px;font-size:12px;'
                        f'line-height:1.5;display:inline-block;max-width:85%;'
                        f'border:1px solid #BBDEFB;">{full_response}▌</span></div>',
                        unsafe_allow_html=True,
                    )

            st.session_state.safetybot_history.append({"role": "assistant", "text": full_response})

        except Exception as e:
            err = str(e)
            st.session_state.safetybot_history.pop()  # 실패한 사용자 메시지 제거
            stream_placeholder.empty()
            if "401" in err or "authentication" in err.lower() or "invalid x-api-key" in err.lower():
                st.error("❌ API 인증 실패: 사이드바에서 Claude API Key를 다시 확인해주세요.")
            elif "429" in err or "rate" in err.lower():
                st.error("❌ 요청 제한 초과: 잠시 후 다시 시도해주세요.")
            elif "connection" in err.lower() or "network" in err.lower():
                st.error("❌ 네트워크 오류: 인터넷 연결을 확인해주세요.")
            elif "credit" in err.lower() or "billing" in err.lower():
                st.error("❌ 크레딧 부족: Anthropic 콘솔에서 크레딧을 확인해주세요.")
            else:
                st.error(f"❌ 오류: {err}")
            st.info("💡 API Key가 `sk-ant-`로 시작하는지, 크레딧이 남아있는지 확인해주세요.")

        st.rerun()


# ── Claude 챗봇 ───────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@st.cache_data(show_spinner=False)
def _precompute_zone_data(
    names: tuple, lats: tuple, lons: tuple,
    grades: tuple, districts: tuple, types: tuple,
) -> dict:
    """zones 분석을 앱 시작 시 한 번만 계산해서 캐싱.
    DataFrame은 해싱 불가이므로 tuple로 변환해서 전달."""
    import math

    def hav(la1, lo1, la2, lo2):
        R = 6_371_000
        φ1, φ2 = math.radians(la1), math.radians(la2)
        a = math.sin(math.radians(la2-la1)/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(math.radians(lo2-lo1)/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    n = len(names)
    nearby: dict = {}
    for lname, (l_lat, l_lon) in LANDMARKS.items():
        tmp = []
        for i in range(n):
            try:
                d = hav(lats[i], lons[i], l_lat, l_lon)
                tmp.append((d, names[i], districts[i], grades[i], types[i]))
            except Exception:
                pass
        tmp.sort()
        nearby[lname] = tmp          # 전체 정렬 보관 (요청 반경별 슬라이싱용)

    high_risk = [(names[i], districts[i]) for i in range(n) if grades[i] == "고위험"]
    mid_risk  = [(names[i], districts[i]) for i in range(n) if grades[i] == "중위험"]
    return {"nearby": nearby, "high_risk": high_risk, "mid_risk": mid_risk}


def _get_precomputed(zones: pd.DataFrame) -> dict:
    """캐싱 함수 호출 헬퍼 (DataFrame → tuple 변환)."""
    return _precompute_zone_data(
        tuple(zones["대상시설명"].astype(str)),
        tuple(zones["위도"].astype(float)),
        tuple(zones["경도"].astype(float)),
        tuple(zones["위험등급"].astype(str)),
        tuple(zones["구"].astype(str)),
        tuple(zones["시설종류"].astype(str)),
    )


def _build_system_prompt(zones: pd.DataFrame, selected_row: pd.Series | None) -> str:
    n_sujung  = len(zones[zones["구"] == "수정구"])
    n_bundang = len(zones[zones["구"] == "분당구"])
    pre       = _get_precomputed(zones)

    # 장소별 인근 구역 블록 (각 top-7, 700m 이내)
    nearby_lines = []
    for lname, entries in pre["nearby"].items():
        within = [(d, nm, gu, gr) for d, nm, gu, gr, *_ in entries if d <= 700][:7]
        if not within:
            continue
        rows = " / ".join(f"{nm}({gu},{gr},{d:.0f}m)" for d, nm, gu, gr in within)
        nearby_lines.append(f"  {lname}: {rows}")

    # 고위험 구역 나열
    high_str = ", ".join(f"{nm}({gu})" for nm, gu in pre["high_risk"]) or "없음"

    # 선택된 구역
    zone_ctx = ""
    if selected_row is not None:
        grade    = str(selected_row.get("위험등급", ""))
        score    = float(selected_row.get("risk_score", 0))
        dist_bus = float(selected_row.get("dist_nearest_stop_m", 0))
        stops    = int(selected_row.get("stops_in_300m", 0))
        slope_c  = selected_row.get("slope_category")
        slope_txt = SLOPE_LABEL.get(int(slope_c), "알 수 없음") if slope_c is not None else "데이터 없음"
        bus_c    = int(selected_row.get("bus_category", 0)) if pd.notna(selected_row.get("bus_category")) else 0
        bus_txt  = BUS_LABEL.get(bus_c, "알 수 없음")
        zone_ctx = (
            f"\n▶ 현재 선택: {selected_row.get('대상시설명','')} "
            f"({selected_row.get('시설종류','')}, 성남시 {selected_row.get('구','')})\n"
            f"  위험등급: {grade} / 점수: {score:.3f} / 경사: {slope_txt} / "
            f"버스: {bus_txt} ({dist_bus:.0f}m, {stops}개소)\n"
        )

    nearby_block = "\n".join(nearby_lines)

    return f"""당신은 AI "안전이" — 가천대학교 스마트시티학과 캡스톤디자인(2026) 어린이 교통안전 전문 AI 어시스턴트.
분석 배경: 성남시 수정구(급경사·구도심)·분당구(평지·신도시) 어린이보호구역 교통사고 원인 연구 (2020–2022).

▶ 현황: 총 {len(zones)}개 (수정구 {n_sujung}·분당구 {n_bundang}) / 고위험 {len(pre["high_risk"])}·중위험 {len(pre["mid_risk"])}개
▶ 고위험 구역: {high_str}

【주요 장소별 인근 보호구역 (700m 이내, 거리 오름차순)】
{nearby_block}
{zone_ctx}
【답변 지침】
- 어떤 질문이든 성심껏 답변하라. 교통안전, 도시공학, 일반 상식, 캡스톤 관련 질문 모두 환영.
- 시스템에 데이터가 있으면 그 데이터를 인용하고, 없으면 일반 지식과 전문성을 바탕으로 최선을 다해 답변하라.
- "데이터 없음" 경고나 "데이터 한계 안내" 문구는 절대 사용하지 말 것. 데이터가 없어도 유용한 답변을 제공하라.
- 로드뷰 이미지가 첨부되면 보도 폭·상태, 경사, 방호울타리, 신호등·횡단보도, 불법주정차, 시야 확보 등을 분석하라.
- 한국어로 답변. 간결하되 충분히 설명. 목록은 번호 또는 bullet 사용."""


def _call_claude(messages: list, api_key: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=messages,
    )
    return response.content[0].text


def render_policy_panel(zones: pd.DataFrame, claude_api_key: str, selected_row) -> None:
    """우측 패널: 선택 구역 정보 카드 + AI 정책 제언"""
    if selected_row is None:
        st.markdown(
            '<div style="text-align:center;padding:50px 10px;color:#aaa;">'
            '<div style="font-size:40px;margin-bottom:14px;">🗺️</div>'
            '<div style="font-size:13px;line-height:1.7;">지도의 색상 격자를<br>클릭하면 구역 정보가<br>표시됩니다</div>'
            '</div>', unsafe_allow_html=True,
        )
        return

    grade     = str(selected_row.get("위험등급", "저위험"))
    color     = RISK_COLOR.get(grade, "#2ca02c")
    bg        = RISK_BG.get(grade, "#fff")
    score     = float(selected_row.get("risk_score", 0))
    dist      = float(selected_row.get("dist_nearest_stop_m", 0))
    stops     = int(selected_row.get("stops_in_300m", 0))
    slope_cat = selected_row.get("slope_category")
    has_slope = bool(selected_row.get("has_slope_data", False))
    slope_txt = SLOPE_LABEL.get(int(slope_cat), "알 수 없음") if (has_slope and slope_cat is not None) else "데이터 없음"
    slope_deg = float(selected_row.get("slope_deg", 0)) if has_slope else 0.0
    bus_cat   = int(selected_row.get("bus_category", 0)) if pd.notna(selected_row.get("bus_category")) else 0
    bus_txt   = BUS_LABEL.get(bus_cat, "알 수 없음")
    routes    = int(selected_row.get("nearest_route_cnt", 0))

    st.markdown(
        f'<div style="border-left:4px solid {color};padding:10px 12px;'
        f'background:{bg};border-radius:0 8px 8px 0;margin-bottom:10px;">'
        f'<b style="font-size:14px;color:#111;">{selected_row.get("대상시설명","")}</b><br>'
        f'<span style="color:#666;font-size:11px;">{selected_row.get("시설종류","")} · 성남시 {selected_row.get("구","")}</span>'
        f'</div>', unsafe_allow_html=True,
    )

    m1, m2 = st.columns(2)
    m1.metric("위험등급", f"{RISK_EMOJI.get(grade,'')} {grade}")
    m2.metric("위험점수", f"{score:.3f}")

    deg_str = f" ({slope_deg:.1f}°)" if (has_slope and slope_deg > 0) else ""
    st.markdown(
        f'<div style="background:#f8f9fa;border-radius:8px;padding:10px 12px;'
        f'font-size:12px;line-height:1.9;margin-top:6px;">'
        f'<div>🏔️ <b>경사도</b>: {slope_txt}{deg_str}</div>'
        f'<div>🚌 <b>버스정류장</b>: {bus_txt}</div>'
        f'<div>📏 <b>최근접 거리</b>: {dist:.0f}m · 300m내 {stops}개</div>'
        f'<div>🗺️ <b>경유노선</b>: {routes}개</div>'
        f'</div>', unsafe_allow_html=True,
    )

    st.divider()
    st.markdown("#### 🏛️ AI 정책 제언")

    policy_key = f"policy_{selected_row.get('대상시설명', '')}"
    if policy_key not in st.session_state:
        st.session_state[policy_key] = None

    if st.session_state[policy_key] is None:
        if claude_api_key and HAS_ANTHROPIC:
            with st.spinner("AI 분석 중…"):
                try:
                    client = anthropic.Anthropic(api_key=claude_api_key.strip())
                    system = _build_system_prompt(zones, selected_row)
                    prompt = (
                        f"'{selected_row.get('대상시설명','')}' 어린이보호구역:\n"
                        f"- 위험등급: {grade} (점수 {score:.3f})\n"
                        f"- 경사도: {slope_txt}\n"
                        f"- 버스정류장: {bus_txt} ({dist:.0f}m)\n\n"
                        "①주요 위험 요인 ②단기 개선 방안 ③장기 정책 제언을 각 2줄 이내 bullet로 작성."
                    )
                    resp = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=800,
                        system=system,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    st.session_state[policy_key] = resp.content[0].text
                except Exception:
                    st.session_state[policy_key] = _template_comment(selected_row)
        else:
            st.session_state[policy_key] = _template_comment(selected_row)

    policy_text = st.session_state.get(policy_key, "")
    st.markdown(
        f'<div style="background:#EEF6FF;border-radius:8px;padding:12px 14px;'
        f'font-size:12px;line-height:1.7;color:#1a1a2e;border:1px solid #BBDEFB;'
        f'max-height:320px;overflow-y:auto;">'
        f'{policy_text.replace(chr(10), "<br>")}</div>',
        unsafe_allow_html=True,
    )

    if claude_api_key:
        if st.button("🔄 재분석", use_container_width=True, key="refresh_policy"):
            st.session_state[policy_key] = None
            st.rerun()
    else:
        st.caption("💡 사이드바에 Claude API Key 입력 시 AI 정책 제언이 자동 생성됩니다.")


def render_character_chatbot(zones: pd.DataFrame, api_key: str, selected_row,
                              naver_client_id: str = "", naver_client_secret: str = ""):
    """하단 확장 패널 AI 챗봇 (로드뷰 이미지 분석 포함)"""
    import base64 as _b64

    if not api_key or len(api_key.strip()) < 10:
        st.warning("⚠️ 사이드바에 Claude API Key를 입력하면 안전이와 대화할 수 있어요.")
        return

    if not HAS_ANTHROPIC:
        st.error("`anthropic` 패키지 필요: `pip install anthropic`")
        return

    if "char_messages" not in st.session_state:
        st.session_state.char_messages = []
    if "chatbot_img_bytes" not in st.session_state:
        st.session_state.chatbot_img_bytes = None
    if "chatbot_img_mime" not in st.session_state:
        st.session_state.chatbot_img_mime = "image/jpeg"

    col_info, col_clear = st.columns([3, 1])
    if selected_row is not None:
        col_info.caption(f"📍 {selected_row.get('대상시설명','')} ({selected_row.get('위험등급','')}) 컨텍스트 활성")
    else:
        col_info.caption("💡 지도에서 구역을 클릭하면 해당 구역 기반 답변을 드려요.")
    if col_clear.button("🗑️", key="clear_chat", help="대화 초기화"):
        st.session_state.char_messages = []
        st.session_state.chatbot_img_bytes = None
        st.rerun()

    zone_context = "현재 선택된 구역 없음."
    if selected_row is not None:
        grade     = str(selected_row.get("위험등급", "저위험"))
        slope_cat = selected_row.get("slope_category")
        has_slope = bool(selected_row.get("has_slope_data", False))
        slope_txt = SLOPE_LABEL.get(int(slope_cat), "데이터 없음") if (has_slope and slope_cat is not None) else "데이터 없음"
        bus_cat   = int(selected_row.get("bus_category", 0)) if pd.notna(selected_row.get("bus_category")) else 0
        bus_txt   = BUS_LABEL.get(bus_cat, "알 수 없음")
        zone_context = (
            f"시설: {selected_row.get('대상시설명','')} ({selected_row.get('시설종류','')})\n"
            f"위치: 성남시 {selected_row.get('구','')}\n"
            f"위험등급: {grade} (점수: {float(selected_row.get('risk_score',0)):.3f})\n"
            f"경사도: {slope_txt} / 버스정류장: {bus_txt}"
        )

    # ── 로드뷰 이미지 컨텍스트 ──────────────────────────────────────────────
    st.markdown("**📸 로드뷰 이미지 첨부** (AI 분석에 포함됩니다)")

    # 1순위: Naver Static API 자동 fetch
    auto_bytes = None
    if selected_row is not None and naver_client_id and naver_client_secret:
        with st.spinner("🗺️ 현장 로드뷰 이미지 불러오는 중…"):
            auto_bytes = fetch_naver_pano_image(
                float(selected_row["위도"]), float(selected_row["경도"]),
                naver_client_id, naver_client_secret,
            )

    if auto_bytes:
        st.image(auto_bytes, caption="📸 현장 로드뷰 (자동 로드 — AI에 자동 포함)", use_container_width=True)
        st.session_state.chatbot_img_bytes = auto_bytes
        st.session_state.chatbot_img_mime  = "image/jpeg"
        img_source_label = "자동 로드"
    else:
        # 2순위: 수동 업로드
        hint = (
            "위 로드뷰 화면에서 **📷 캡처** 버튼을 누르면 PNG가 저장됩니다. 그 파일을 여기에 업로드하세요."
            if naver_client_id else
            "사이드바에 Naver 키를 입력하면 자동 로드, 또는 아래에서 직접 업로드하세요."
        )
        st.caption(hint)
        uploaded_img = st.file_uploader(
            "로드뷰 스크린샷 업로드 (PNG/JPG)",
            type=["png", "jpg", "jpeg"],
            key="chatbot_img_upload",
        )
        if uploaded_img is not None:
            raw = uploaded_img.read()
            ext = uploaded_img.name.rsplit(".", 1)[-1].lower()
            st.session_state.chatbot_img_bytes = raw
            st.session_state.chatbot_img_mime  = "image/png" if ext == "png" else "image/jpeg"
            st.image(raw, caption="📸 업로드된 로드뷰 (AI에 포함)", use_container_width=True)
            img_source_label = "업로드"
        elif st.session_state.chatbot_img_bytes:
            st.image(st.session_state.chatbot_img_bytes, caption="📸 이전 이미지 유지 중", use_container_width=True)
            img_source_label = "유지 중"
        else:
            img_source_label = None

    pano_bytes = st.session_state.chatbot_img_bytes
    if pano_bytes:
        col_status, col_remove = st.columns([4, 1])
        col_status.caption(f"✅ 이미지 첨부됨 ({img_source_label}) — 다음 질문에 함께 전송됩니다.")
        if col_remove.button("❌ 제거", key="remove_img", help="이미지 첨부 해제"):
            st.session_state.chatbot_img_bytes = None
            st.rerun()
    else:
        st.caption("ℹ️ 이미지 없이 텍스트만으로 답변합니다.")

    st.divider()

    # ── 채팅 히스토리 ────────────────────────────────────────────────────────
    for msg in st.session_state.char_messages:
        with st.chat_message(msg["role"]):
            if isinstance(msg.get("content"), list):
                for part in msg["content"]:
                    if part.get("type") == "text":
                        st.write(part["text"])
            else:
                st.write(msg.get("content", ""))

    user_input = st.chat_input("안전이에게 질문하세요… (예: 이 구역 위험 요인은?)", key="char_chat_input")

    if user_input and user_input.strip():
        # 이미지 포함 여부에 따라 content 구성
        if pano_bytes:
            img_b64 = _b64.standard_b64encode(pano_bytes).decode()
            user_content = [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": st.session_state.chatbot_img_mime,
                    "data": img_b64,
                }},
                {"type": "text", "text": user_input},
            ]
            display_content = user_input
        else:
            user_content = user_input
            display_content = user_input

        st.session_state.char_messages.append({"role": "user", "content": user_content})

        with st.chat_message("user"):
            st.write(display_content)
            if pano_bytes:
                st.caption("📎 이미지 첨부됨")

        system_prompt = _build_system_prompt(zones, selected_row) + f"\n\n【현재 선택 구역】\n{zone_context}"

        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_response = ""
            try:
                client = anthropic.Anthropic(api_key=api_key.strip())
                # 이미지는 가장 최신 user 메시지에만 포함, 이전 메시지에선 텍스트만 추출
                # (이미지 중복 전송 시 토큰 폭발로 응답 잘림 방지)
                api_messages = []
                all_msgs = st.session_state.char_messages
                for i, m in enumerate(all_msgs):
                    content = m["content"]
                    is_latest_user = (m["role"] == "user" and i == len(all_msgs) - 1)
                    if not is_latest_user and isinstance(content, list):
                        # 이전 메시지의 이미지 블록 제거, 텍스트만 유지
                        text_only = " ".join(
                            p["text"] for p in content if p.get("type") == "text"
                        )
                        content = text_only
                    api_messages.append({"role": m["role"], "content": content})
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=api_messages,
                ) as stream:
                    for text in stream.text_stream:
                        full_response += text
                        placeholder.write(full_response + "▌")
                placeholder.write(full_response)
                st.session_state.char_messages.append({"role": "assistant", "content": full_response})
            except Exception as e:
                err = str(e)
                if "401" in err or "authentication" in err.lower():
                    placeholder.error("❌ API 인증 실패: API Key를 확인해주세요.")
                elif "429" in err or "rate" in err.lower():
                    placeholder.error("❌ 요청 제한 초과: 잠시 후 다시 시도해주세요.")
                else:
                    placeholder.error(f"❌ 오류: {err}")


def render_floating_chatbot(claude_api_key: str, selected_row, zones: pd.DataFrame = None):
    """오른쪽 하단 플로팅 Claude 챗봇을 부모 Streamlit DOM에 주입한다."""

    zone_info: dict = {}
    if selected_row is not None:
        slope_cat = selected_row.get("slope_category")
        bus_cat = int(selected_row.get("bus_category", 0)) if pd.notna(selected_row.get("bus_category")) else 0
        zone_info = {
            "name":     str(selected_row.get("대상시설명", "")),
            "type":     str(selected_row.get("시설종류", "")),
            "district": str(selected_row.get("구", "")),
            "grade":    str(selected_row.get("위험등급", "")),
            "score":    float(selected_row.get("risk_score", 0)),
            "slope":    SLOPE_LABEL.get(int(slope_cat), "알 수 없음") if slope_cat is not None else "데이터 없음",
            "bus":      BUS_LABEL.get(bus_cat, "알 수 없음"),
            "bus_dist": float(selected_row.get("dist_nearest_stop_m", 0)),
        }

    # pre-computed 데이터로 JS 시스템 프롬프트 미리 구성 (토큰 최소화)
    if zones is not None and len(zones) > 0:
        pre = _get_precomputed(zones)
        n_zones = len(zones)
        # 장소별 인근 구역 (700m, top-7) → compact string
        nearby_lines_js = []
        for lname, entries in pre["nearby"].items():
            within = [(d, nm, gu, gr) for d, nm, gu, gr, *_ in entries if d <= 700][:7]
            if not within:
                continue
            rows = " / ".join(f"{nm}({gu},{gr},{d:.0f}m)" for d, nm, gu, gr in within)
            nearby_lines_js.append(f"  {lname}: {rows}")
        high_str_js = ", ".join(f"{nm}({gu})" for nm, gu in pre["high_risk"]) or "없음"
        n_high = len(pre["high_risk"])
        n_mid  = len(pre["mid_risk"])
        nearby_block_js = "\n".join(nearby_lines_js)
    else:
        n_zones = 0
        high_str_js = "없음"
        n_high = n_mid = 0
        nearby_block_js = ""

    # JS 시스템 프롬프트 문자열을 Python에서 미리 완성
    n_sujung_js  = len(zones[zones["구"] == "수정구"]) if zones is not None else 0
    n_bundang_js = len(zones[zones["구"] == "분당구"]) if zones is not None else 0
    js_sys_static = (
        f'당신은 AI "안전이" — 가천대학교 스마트시티학과 캡스톤디자인(2026) 교통안전 전문가.\\n'
        f'분석: 성남시 수정구(급경사·구도심)·분당구(평지·신도시) 어린이보호구역 (2020–2022).\\n'
        f'\\n▶ 현황: 총 {n_zones}개 (수정구 {n_sujung_js}·분당구 {n_bundang_js}) / '
        f'고위험 {n_high}·중위험 {n_mid}개\\n'
        f'▶ 고위험: {high_str_js}\\n'
        f'\\n【주요 장소별 인근 보호구역 (700m 이내)】\\n'
        f'{nearby_block_js}\\n'
        f'\\n【답변 지침】한국어, 간결하게. 목록은 번호/bullet. 데이터 없으면 "확인 불가".'
    )

    zone_json      = json.dumps(zone_info, ensure_ascii=False)
    key_json       = json.dumps(claude_api_key or "")
    sys_static_json = json.dumps(js_sys_static)

    # 표지판 캐릭터 SVG (눈·코·입 달린 귀여운 도로 표지판)
    sign_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 54 56" width="40" height="40">'
        '<rect x="24" y="48" width="6" height="8" rx="3" fill="#9E9E9E"/>'
        '<rect x="3" y="4" width="48" height="44" rx="10" fill="#FFD600" stroke="#F9A825" stroke-width="2.5"/>'
        '<ellipse cx="18" cy="20" rx="4.5" ry="5" fill="#212121"/>'
        '<ellipse cx="36" cy="20" rx="4.5" ry="5" fill="#212121"/>'
        '<circle cx="20" cy="18" r="1.8" fill="white"/>'
        '<circle cx="38" cy="18" r="1.8" fill="white"/>'
        '<ellipse cx="27" cy="28" rx="3.5" ry="2.5" fill="#F9A825"/>'
        '<path d="M 16 37 Q 27 45 38 37" stroke="#212121" stroke-width="2.8" fill="none" stroke-linecap="round"/>'
        '<ellipse cx="11" cy="32" rx="5" ry="3.5" fill="#FF8F00" opacity="0.3"/>'
        '<ellipse cx="43" cy="32" rx="5" ry="3.5" fill="#FF8F00" opacity="0.3"/>'
        '</svg>'
    )

    html = f"""<script>
(function() {{
    var CLAUDE_KEY = {key_json};
    var ZONE_INFO  = {zone_json};
    var SYS_STATIC = {sys_static_json};
    var SIGN_SVG   = {json.dumps(sign_svg)};

    // ── 부모 문서 접근 시도 ────────────────────────────────────────────────
    var doc, par, hasParent = false;
    try {{
        doc = window.parent.document;
        par = window.parent;
        // 접근 가능 여부 확인 (cross-origin 시 여기서 예외 발생)
        void doc.body;
        hasParent = true;
    }} catch(e) {{
        // 부모 접근 불가 → 현재 iframe 내 렌더링으로 폴백
        doc = document;
        par = window;
    }}

    // iframe 자체를 투명 고정 오버레이로 변환 시도 (hasParent인 경우만)
    if (hasParent) {{
        try {{
            var fe = window.frameElement;
            if (fe) {{
                fe.style.cssText = [
                    'position:fixed!important',
                    'bottom:0!important',
                    'right:0!important',
                    'width:440px!important',
                    'height:620px!important',
                    'border:none!important',
                    'background:transparent!important',
                    'z-index:2147483630!important',
                    'pointer-events:none!important',
                    'overflow:visible!important',
                ].join(';');
            }}
        }} catch(e) {{ /* frameElement 접근 실패 무시 */ }}
    }}

    // ── 재렌더 시: 컨텍스트·키만 갱신하고 즉시 종료 ─────────────────────
    if (doc.getElementById('safebot-root')) {{
        par.__sbKey  = CLAUDE_KEY;
        par.__sbZone = ZONE_INFO;
        var ctxEl = doc.getElementById('safebot-ctx');
        if (ctxEl) {{
            ctxEl.textContent = ZONE_INFO.name
                ? '📍 ' + ZONE_INFO.name + '  ·  ' + ZONE_INFO.grade + '  ·  ' + ZONE_INFO.slope
                : '💡 지도에서 격자를 클릭하면 구역 정보가 표시됩니다';
        }}
        var wEl = doc.getElementById('safebot-warn');
        if (wEl) wEl.style.display = CLAUDE_KEY ? 'none' : 'block';
        return;
    }}

    // ── CSS ───────────────────────────────────────────────────────────────
    var css = doc.createElement('style');
    css.textContent =
        '#safebot-root * {{box-sizing:border-box;font-family:"Segoe UI","Noto Sans KR",sans-serif;margin:0;padding:0}}' +
        '#safebot-fab {{' +
            'position:fixed;bottom:28px;right:28px;' +
            'width:66px;height:66px;border-radius:50%;' +
            'background:linear-gradient(145deg,#1e88e5,#43a047);' +
            'border:3px solid #fff;cursor:pointer;' +
            'box-shadow:0 6px 22px rgba(30,136,229,.55);' +
            'z-index:2147483647;display:flex;align-items:center;justify-content:center;' +
            'transition:transform .2s,box-shadow .2s;pointer-events:auto;' +
        '}}' +
        '#safebot-fab:hover{{transform:scale(1.13) rotate(-4deg);box-shadow:0 8px 28px rgba(30,136,229,.7)}}' +
        '#safebot-win {{' +
            'position:fixed;bottom:108px;right:28px;' +
            'width:380px;height:540px;' +
            'background:#fff;border-radius:22px;' +
            'box-shadow:0 16px 50px rgba(0,0,0,.2);' +
            'display:none;flex-direction:column;' +
            'z-index:2147483646;overflow:hidden;pointer-events:auto;' +
        '}}' +
        '#safebot-win.open{{display:flex;animation:sbUp .24s cubic-bezier(.34,1.56,.64,1)}}' +
        '@keyframes sbUp{{from{{opacity:0;transform:translateY(20px) scale(.95)}}to{{opacity:1;transform:translateY(0) scale(1)}}}}' +
        '#safebot-hdr {{' +
            'background:linear-gradient(135deg,#1e88e5 0%,#43a047 100%);' +
            'padding:12px 14px;' +
            'display:flex;align-items:center;justify-content:space-between;flex-shrink:0;' +
        '}}' +
        '#safebot-hdr-l {{display:flex;align-items:center;gap:8px}}' +
        '#safebot-hdr-avatar {{width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.22);' +
            'display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0}}' +
        '#safebot-hdr-title {{color:#fff;font-size:14px;font-weight:700;line-height:1.2}}' +
        '#safebot-hdr-sub {{color:rgba(255,255,255,.78);font-size:10px}}' +
        '#safebot-x {{background:rgba(255,255,255,.18);border:none;color:#fff;' +
            'font-size:14px;cursor:pointer;border-radius:50%;' +
            'width:28px;height:28px;display:flex;align-items:center;justify-content:center;' +
            'transition:background .15s;flex-shrink:0}}' +
        '#safebot-x:hover{{background:rgba(255,255,255,.38)}}' +
        '#safebot-ctx {{' +
            'background:#e8f4fd;border-bottom:1px solid #c5e1f7;' +
            'padding:6px 14px;font-size:11.5px;color:#1565c0;flex-shrink:0;' +
            'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' +
        '}}' +
        '#safebot-warn {{' +
            'background:#fff8e1;border-bottom:1px solid #ffe082;' +
            'padding:6px 14px;font-size:11px;color:#7a5000;flex-shrink:0;display:none' +
        '}}' +
        '#safebot-msgs {{' +
            'flex:1;overflow-y:auto;padding:14px;' +
            'display:flex;flex-direction:column;gap:10px;' +
        '}}' +
        '#safebot-msgs::-webkit-scrollbar{{width:4px}}' +
        '#safebot-msgs::-webkit-scrollbar-thumb{{background:#ddd;border-radius:2px}}' +
        '.sb-m {{max-width:85%;padding:10px 14px;border-radius:18px;font-size:13px;line-height:1.65;word-break:break-word}}' +
        '.sb-m.u {{background:#1e88e5;color:#fff;align-self:flex-end;border-bottom-right-radius:5px}}' +
        '.sb-m.b {{background:#f0f2f5;color:#1a1a1a;align-self:flex-start;border-bottom-left-radius:5px}}' +
        '.sb-m.dots {{background:#f0f2f5;align-self:flex-start;border-bottom-left-radius:5px;padding:10px 16px}}' +
        '.sb-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#999;' +
            'margin:0 2px;animation:sbBounce 1.2s infinite ease-in-out}}' +
        '.sb-dot:nth-child(2){{animation-delay:.2s}}.sb-dot:nth-child(3){{animation-delay:.4s}}' +
        '@keyframes sbBounce{{0%,80%,100%{{transform:scale(0.6)}}40%{{transform:scale(1)}}}}' +
        '#safebot-inp-row {{' +
            'display:flex;padding:10px 12px;border-top:1px solid #e8eaed;' +
            'gap:8px;flex-shrink:0;background:#fff;align-items:center;' +
        '}}' +
        '#safebot-inp {{' +
            'flex:1;border:1.5px solid #dadce0;border-radius:24px;' +
            'padding:9px 16px;font-size:13px;outline:none;' +
            'transition:border-color .15s;background:#fafafa;' +
        '}}' +
        '#safebot-inp:focus{{border-color:#1e88e5;background:#fff}}' +
        '#safebot-snd {{' +
            'background:#1e88e5;color:#fff;border:none;border-radius:50%;' +
            'width:40px;height:40px;min-width:40px;cursor:pointer;font-size:17px;' +
            'display:flex;align-items:center;justify-content:center;' +
            'transition:background .15s,transform .1s;padding:0;' +
        '}}' +
        '#safebot-snd:hover{{background:#1565c0;transform:scale(1.08)}}' +
        '#safebot-snd:disabled{{background:#c0c0c0;cursor:not-allowed;transform:none}}';
    doc.head.appendChild(css);

    // ── DOM ───────────────────────────────────────────────────────────────
    var root = doc.createElement('div');
    root.id = 'safebot-root';
    root.innerHTML =
        '<button id="safebot-fab" title="안전이 AI 상담">' + SIGN_SVG + '</button>' +
        '<div id="safebot-win">' +
          '<div id="safebot-hdr">' +
            '<div id="safebot-hdr-l">' +
              '<div id="safebot-hdr-avatar">' + SIGN_SVG + '</div>' +
              '<div><div id="safebot-hdr-title">안전이</div>' +
                  '<div id="safebot-hdr-sub">Powered by Claude · 교통안전 AI</div></div>' +
            '</div>' +
            '<button id="safebot-x">✕</button>' +
          '</div>' +
          '<div id="safebot-ctx">💡 지도에서 격자를 클릭하면 구역 정보가 표시됩니다</div>' +
          '<div id="safebot-warn">⚠️ Claude API 키를 사이드바에 입력해주세요</div>' +
          '<div id="safebot-msgs"></div>' +
          '<div id="safebot-inp-row">' +
            '<input id="safebot-inp" placeholder="이 구역에 대해 질문하세요…" maxlength="500" />' +
            '<button id="safebot-snd" title="전송">&#9658;</button>' +
          '</div>' +
        '</div>';
    doc.body.appendChild(root);

    // ── 전역 상태 ──────────────────────────────────────────────────────────
    par.__sbKey  = CLAUDE_KEY;
    par.__sbZone = ZONE_INFO;
    par.__sbHist = [];

    // ── 부모 윈도우에 함수 등록 (iframe 교체 후에도 유지됨) ───────────────
    // par.document 를 직접 사용하므로 iframe 재로드 후에도 올바르게 동작함

    par.__sbRefCtx = function() {{
        var d = par.document;
        var z = par.__sbZone;
        var ctx = d.getElementById('safebot-ctx');
        var warn = d.getElementById('safebot-warn');
        if (ctx) ctx.textContent = (z && z.name)
            ? '📍 ' + z.name + '  ·  ' + z.grade + '  ·  ' + z.slope
            : '💡 지도에서 격자를 클릭하면 구역 정보가 표시됩니다';
        if (warn) warn.style.display = par.__sbKey ? 'none' : 'block';
    }};

    par.__sbAppend = function(role, html) {{
        var d = par.document;
        var msgs = d.getElementById('safebot-msgs');
        if (!msgs) return null;
        var div = d.createElement('div');
        div.className = 'sb-m ' + role;
        div.innerHTML = html;
        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
        return div;
    }};

    par.__sbDots = function() {{
        var d = par.document;
        var msgs = d.getElementById('safebot-msgs');
        if (!msgs) return null;
        var div = d.createElement('div');
        div.className = 'sb-m dots';
        div.innerHTML = '<span class="sb-dot"></span><span class="sb-dot"></span><span class="sb-dot"></span>';
        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
        return div;
    }};

    par.__sbWelcome = function() {{
        par.__sbAppend('b',
            '안녕하세요! 저는 성남시 어린이보호구역 AI 안전 분석사 <b>안전이</b>예요 😊<br><br>' +
            '지도에서 보호구역 격자를 클릭하면 해당 구역의 위험 요인을 분석해드려요.<br>' +
            '경사도, 버스정류장 근접성, 정책 제언 등 자유롭게 질문하세요!'
        );
    }};

    par.__sbMd = function(t) {{
        return t
            .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/\\*\\*(.+?)\\*\\*/gs,'<b>$1</b>')
            .replace(/\\*(.+?)\\*/gs,'<i>$1</i>')
            .replace(/^#{1,3} (.+)$/gm,'<b>$1</b>')
            .replace(/^[-*] (.+)$/gm,'• $1')
            .replace(/\\n\\n/g,'<br><br>')
            .replace(/\\n/g,'<br>');
    }};

    par.__sbToggle = function() {{
        var d = par.document;
        var win = d.getElementById('safebot-win');
        if (!win) return;
        var isOpen = win.classList.toggle('open');
        if (isOpen) {{
            par.__sbRefCtx();
            var msgs = d.getElementById('safebot-msgs');
            if (msgs && msgs.children.length === 0) par.__sbWelcome();
            setTimeout(function() {{
                var inp = par.document.getElementById('safebot-inp');
                if (inp) inp.focus();
            }}, 100);
        }}
    }};

    par.__sbClose = function() {{
        var win = par.document.getElementById('safebot-win');
        if (win) win.classList.remove('open');
    }};

    par.__sbSend = function() {{
        var d = par.document;
        var inp = d.getElementById('safebot-inp');
        var snd = d.getElementById('safebot-snd');
        if (!inp || !snd) return;
        var text = inp.value.trim();
        if (!text || snd.disabled) return;

        var key = par.__sbKey;
        if (!key) {{ par.__sbRefCtx(); return; }}

        inp.value = '';
        snd.disabled = true;

        par.__sbAppend('u', text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'));
        par.__sbHist.push({{ role: 'user', content: text }});

        var z = par.__sbZone || {{}};
        var zCtx = z.name
            ? '선택 구역: ' + z.name + ' (' + z.type + ', 성남시 ' + z.district + ')\\n'
              + '위험등급: ' + z.grade + ' (점수: ' + (+(z.score||0)).toFixed(3) + ')\\n'
              + '경사도: ' + z.slope + '\\n'
              + '버스정류장: ' + z.bus + ' (' + Math.round(z.bus_dist||0) + 'm)'
            : '선택된 구역 없음';

        // SYS_STATIC: Python에서 미리 계산된 정적 시스템 프롬프트
        // 현재 선택 구역 정보만 동적으로 추가
        var sys = SYS_STATIC + '\\n\\n【현재 선택 구역】\\n' + zCtx;

        var dots = par.__sbDots();
        var url  = 'https://api.anthropic.com/v1/messages';
        var ctrl = new AbortController();
        var timer = setTimeout(function() {{ ctrl.abort(); }}, 30000);

        fetch(url, {{
            method: 'POST',
            signal: ctrl.signal,
            headers: {{
                'Content-Type': 'application/json',
                'x-api-key': key,
                'anthropic-version': '2023-06-01',
                'anthropic-dangerous-direct-browser-access': 'true'
            }},
            body: JSON.stringify({{
                model: 'claude-sonnet-4-6',
                max_tokens: 2048,
                system: sys,
                messages: par.__sbHist
            }})
        }})
        .then(function(r) {{
            if (!r.ok) {{
                return r.json().then(function(e) {{
                    var msg = (e.error && e.error.message) ? e.error.message : JSON.stringify(e);
                    throw new Error('HTTP ' + r.status + ': ' + msg);
                }}).catch(function(err) {{
                    if (err.message && err.message.indexOf('HTTP') === 0) throw err;
                    throw new Error('HTTP ' + r.status);
                }});
            }}
            return r.json();
        }})
        .then(function(data) {{
            clearTimeout(timer);
            var reply;
            if (data.content && data.content[0] && data.content[0].text) {{
                reply = data.content[0].text;
            }} else if (data.error) {{
                reply = '❌ API 오류: ' + (data.error.message || JSON.stringify(data.error));
            }} else {{
                reply = '(응답 없음)';
            }}
            if (dots) dots.remove();
            par.__sbAppend('b', par.__sbMd(reply));
            par.__sbHist.push({{ role: 'assistant', content: reply }});
        }})
        .catch(function(e) {{
            clearTimeout(timer);
            if (dots) dots.remove();
            var msg;
            if (e.name === 'AbortError') {{
                msg = '⏱️ 응답 시간 초과 (30초). 다시 시도해주세요.';
            }} else if (e.message && e.message.indexOf('401') !== -1) {{
                msg = '❌ API 인증 실패: 사이드바에서 Claude API Key를 확인해주세요.';
            }} else if (e.message && e.message.indexOf('429') !== -1) {{
                msg = '❌ 요청 제한 초과: 잠시 후 다시 시도해주세요.';
            }} else {{
                msg = '❌ 오류: ' + e.message;
            }}
            par.__sbAppend('b', msg);
        }})
        .finally(function() {{
            var s = par.document.getElementById('safebot-snd');
            if (s) s.disabled = false;
            var i = par.document.getElementById('safebot-inp');
            if (i) i.focus();
        }});
    }};

    // onclick 속성 방식으로 연결 – iframe 교체 후에도 parent window 함수가 유효
    var fab = doc.getElementById('safebot-fab');
    if (fab) fab.setAttribute('onclick', 'window.__sbToggle()');
    var xBtn = doc.getElementById('safebot-x');
    if (xBtn) xBtn.setAttribute('onclick', 'window.__sbClose()');
    var sndBtn = doc.getElementById('safebot-snd');
    if (sndBtn) sndBtn.setAttribute('onclick', 'window.__sbSend()');
    var inpEl = doc.getElementById('safebot-inp');
    if (inpEl) inpEl.setAttribute('onkeydown',
        'if(event.key==="Enter"&&!event.shiftKey){{event.preventDefault();window.__sbSend();}}');
}})();
</script>"""

    st.components.v1.html(html, height=1, scrolling=False)


def render_chatbot_tab(zones: pd.DataFrame, api_key: str):
    import base64

    st.markdown("### 🤖 Claude AI 로드뷰·데이터 분석")
    st.caption("로드뷰 스크린샷을 업로드하거나 데이터에 대해 질문하세요.")

    if not HAS_ANTHROPIC:
        st.error("`anthropic` 패키지가 설치되지 않았습니다. `py -m pip install anthropic` 실행 후 재시작하세요.")
        return
    if not api_key:
        st.warning("사이드바에서 Anthropic API Key를 입력해주세요.")
        return

    # ── 구역 선택 + 로드뷰 링크 ──────────────────────────────────────────────
    zone_names = ["(없음)"] + zones["대상시설명"].dropna().tolist()
    selected_name = st.selectbox("분석할 보호구역 선택 (선택 시 AI에 컨텍스트 전달)", zone_names)

    selected_row = None
    if selected_name != "(없음)":
        match = zones[zones["대상시설명"] == selected_name]
        if not match.empty:
            selected_row = match.iloc[0]
            lat = float(selected_row["위도"])
            lon = float(selected_row["경도"])

            st.caption(f"📍 {selected_row.get('대상시설명','')} | {selected_row.get('구','')} | 로드뷰를 캡처해서 아래에 업로드하면 AI가 분석합니다.")

    # ── 이미지 업로드 ────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "로드뷰 이미지 업로드 (선택)",
        type=["png", "jpg", "jpeg"],
        help="Kakao/Naver 로드뷰 스크린샷을 업로드하세요.",
    )
    if uploaded:
        st.image(uploaded, caption="업로드된 로드뷰", use_container_width=True)

    # ── 채팅 기록 초기화 ─────────────────────────────────────────────────────
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    if st.button("🗑️ 대화 초기화", use_container_width=False):
        st.session_state.chat_history = []
        st.rerun()

    # ── 채팅 기록 표시 ────────────────────────────────────────────────────────
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            if isinstance(msg["content"], list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        st.markdown(block["text"])
            else:
                st.markdown(msg["content"])

    # ── 입력창 ───────────────────────────────────────────────────────────────
    default_prompt = "이 로드뷰 이미지를 교통안전 관점에서 분석해주세요." if uploaded else ""
    user_input = st.chat_input("질문을 입력하세요 (예: 이 구역의 위험 요인은 무엇인가요?)")

    if user_input:
        # 사용자 메시지 구성
        if uploaded:
            uploaded.seek(0)
            img_b64 = base64.standard_b64encode(uploaded.read()).decode("utf-8")
            ext = uploaded.name.rsplit(".", 1)[-1].lower()
            media_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": user_input},
            ]
        else:
            user_content = user_input

        st.session_state.chat_history.append({"role": "user", "content": user_content})

        with st.chat_message("user"):
            if uploaded:
                st.image(uploaded)
            st.markdown(user_input)

        # Claude 호출
        system_prompt = _build_system_prompt(zones, selected_row)
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.chat_history
        ]

        with st.chat_message("assistant"):
            with st.spinner("분석 중…"):
                try:
                    client = anthropic.Anthropic(api_key=api_key)
                    response = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1500,
                        system=system_prompt,
                        messages=api_messages,
                    )
                    reply = response.content[0].text
                    st.markdown(reply)
                    st.session_state.chat_history.append({"role": "assistant", "content": reply})
                except anthropic.AuthenticationError:
                    st.error("API Key가 올바르지 않습니다. 사이드바에서 다시 확인해주세요.")
                except Exception as e:
                    st.error(f"오류 발생: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="성남시 어린이보호구역 안전 분석",
        page_icon="🚸",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    /* ══ 전체 레이아웃 ══════════════════════════════════════════════════════════ */
    html, body, [data-testid="stAppViewContainer"] {
        font-family:'Inter','-apple-system','Malgun Gothic',sans-serif;
        background:#eef2f7;
    }
    .block-container { padding-top:0; padding-bottom:1.5rem; max-width:1700px; }

    /* ══ 사이드바 ════════════════════════════════════════════════════════════════ */
    [data-testid="stSidebar"] {
        background:linear-gradient(175deg,#0a1f3c 0%,#0d2a4e 45%,#0f3460 100%) !important;
        border-right:none !important;
        box-shadow:4px 0 20px rgba(0,0,0,.18);
    }
    [data-testid="stSidebar"] > div { padding-top:0 !important; }
    [data-testid="stSidebar"] * { color:#dce8f5 !important; }
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span { font-size:12.5px; }
    [data-testid="stSidebar"] h2 {
        color:#ffffff !important; font-size:0.72rem !important; font-weight:700 !important;
        letter-spacing:0.08em; text-transform:uppercase;
        border-bottom:1px solid rgba(255,255,255,0.12);
        padding-bottom:6px; margin-top:2px; margin-bottom:8px;
    }
    [data-testid="stSidebar"] hr { border-color:rgba(255,255,255,0.1) !important; margin:10px 0; }
    [data-testid="stSidebar"] label { color:#a8c4e0 !important; font-size:11.5px !important; font-weight:500 !important; }
    [data-testid="stSidebar"] input {
        background:rgba(255,255,255,0.08) !important;
        border:1px solid rgba(255,255,255,0.18) !important;
        border-radius:8px !important;
        color:#ffffff !important;
        font-size:12px !important;
        font-family:monospace !important;
    }
    [data-testid="stSidebar"] input:focus {
        background:rgba(255,255,255,0.14) !important;
        border-color:rgba(127,200,169,0.6) !important;
        box-shadow:0 0 0 2px rgba(127,200,169,0.15) !important;
    }
    [data-testid="stSidebar"] .stCaption p { color:#7a9abf !important; font-size:10.5px !important; line-height:1.5; }
    [data-testid="stSidebar"] .stRadio > div { gap:6px; }
    [data-testid="stSidebar"] .stRadio label { font-size:12.5px !important; color:#c5d8ec !important; }
    [data-testid="stSidebar"] .stMultiSelect span { background:rgba(127,200,169,0.2) !important; color:#7fc8a9 !important; }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] b { color:#ffffff !important; }

    /* ══ KPI 카드 ═══════════════════════════════════════════════════════════════ */
    [data-testid="metric-container"] {
        background:#ffffff;
        border-radius:16px;
        padding:18px 20px !important;
        box-shadow:0 2px 8px rgba(0,0,0,.06), 0 0 0 1px rgba(0,0,0,.04);
        transition:transform .18s cubic-bezier(.4,0,.2,1), box-shadow .18s;
        position:relative; overflow:hidden;
    }
    [data-testid="metric-container"]:hover {
        transform:translateY(-3px);
        box-shadow:0 8px 28px rgba(0,0,0,.13);
    }
    [data-testid="metric-container"]::before {
        content:''; position:absolute; top:0; left:0; right:0;
        height:3px; background:linear-gradient(90deg,#1e3a5f,#0ea5e9);
        border-radius:16px 16px 0 0;
    }
    [data-testid="stMetricValue"] {
        font-size:1.65rem !important; font-weight:800 !important;
        color:#0f172a !important; letter-spacing:-0.02em; line-height:1.1;
    }
    [data-testid="stMetricLabel"] {
        font-size:0.68rem !important; color:#64748b !important;
        font-weight:700 !important; letter-spacing:0.06em; text-transform:uppercase;
    }
    [data-testid="stMetricDelta"] { font-size:0.67rem !important; color:#94a3b8 !important; }

    /* ══ 섹션 헤더 ══════════════════════════════════════════════════════════════ */
    .section-header {
        display:flex; align-items:center; gap:10px;
        padding:10px 16px; margin:6px 0 12px;
        background:#ffffff; border-radius:12px;
        border-left:4px solid #1e3a5f;
        box-shadow:0 1px 4px rgba(0,0,0,.06);
    }
    .section-header-icon { font-size:1.15rem; }
    .section-header-title {
        font-size:0.92rem; font-weight:700; color:#0f172a; letter-spacing:-0.01em;
    }
    .section-header-sub { font-size:0.72rem; color:#64748b; margin-top:1px; }

    /* ══ 구분선 ═════════════════════════════════════════════════════════════════ */
    hr { border:none; border-top:1px solid #e2e8f0; margin:0.8rem 0; }

    /* ══ 탭 ════════════════════════════════════════════════════════════════════ */
    .stTabs [data-baseweb="tab-list"] {
        gap:3px; background:#f1f5f9; border-radius:12px; padding:5px;
        border:1px solid #e2e8f0;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius:9px; font-size:12.5px; font-weight:600;
        padding:8px 18px; color:#64748b; transition:all .15s;
    }
    .stTabs [data-baseweb="tab"]:hover { color:#1e3a5f; background:rgba(30,58,95,.06); }
    .stTabs [aria-selected="true"] {
        background:#1e3a5f !important; color:#ffffff !important;
        box-shadow:0 2px 8px rgba(30,58,95,.3) !important;
    }

    /* ══ 버튼 ════════════════════════════════════════════════════════════════════ */
    .stButton > button {
        border-radius:10px; font-weight:600; font-size:13px;
        border:1.5px solid #e2e8f0; padding:8px 16px;
        transition:all .15s cubic-bezier(.4,0,.2,1);
        background:#ffffff; color:#374151;
    }
    .stButton > button:hover {
        background:#1e3a5f; color:#fff; border-color:#1e3a5f;
        transform:translateY(-1px); box-shadow:0 4px 12px rgba(30,58,95,.25);
    }

    /* ══ 링크 버튼 ═══════════════════════════════════════════════════════════════ */
    [data-testid="stLinkButton"] a {
        border-radius:10px !important; font-weight:600 !important;
        border:1.5px solid #e2e8f0 !important; transition:all .15s !important;
    }

    /* ══ expander ════════════════════════════════════════════════════════════════ */
    [data-testid="stExpander"] {
        border:1px solid #e2e8f0 !important;
        border-radius:14px !important;
        background:#ffffff !important;
        box-shadow:0 1px 4px rgba(0,0,0,.05) !important;
        overflow:hidden;
    }
    [data-testid="stExpander"] summary {
        font-weight:600 !important; color:#1e3a5f !important; font-size:0.88rem !important;
        padding:14px 18px !important;
    }

    /* ══ info/warning/error 박스 ════════════════════════════════════════════════ */
    [data-testid="stAlert"] {
        border-radius:12px !important; border-width:1px !important;
        font-size:13px !important;
    }

    /* ══ 스크롤바 ════════════════════════════════════════════════════════════════ */
    ::-webkit-scrollbar { width:5px; height:5px; }
    ::-webkit-scrollbar-thumb { background:#cbd5e1; border-radius:4px; }
    ::-webkit-scrollbar-track { background:transparent; }

    /* ══ 지도 컨테이너 ══════════════════════════════════════════════════════════ */
    iframe { border-radius:12px; }
    </style>
    """, unsafe_allow_html=True)

    # ── 헤더 배너 ─────────────────────────────────────────────────────────────
    st.markdown("""
    <div style="
        background:linear-gradient(120deg,#0a1f3c 0%,#0d2a4e 40%,#0f3d6b 70%,#0e5c3a 100%);
        border-radius:20px; padding:28px 36px 24px; margin-bottom:20px;
        display:flex; align-items:center; justify-content:space-between;
        box-shadow:0 8px 32px rgba(10,31,60,.30), 0 2px 8px rgba(0,0,0,.12);
        position:relative; overflow:hidden;">
      <!-- 배경 장식 원 -->
      <div style="position:absolute;top:-40px;right:220px;width:200px;height:200px;
                  background:rgba(127,200,169,.07);border-radius:50%;pointer-events:none;"></div>
      <div style="position:absolute;bottom:-60px;right:80px;width:280px;height:280px;
                  background:rgba(14,165,233,.06);border-radius:50%;pointer-events:none;"></div>
      <div style="position:relative; z-index:1;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
          <div style="background:rgba(127,200,169,.2); border:1px solid rgba(127,200,169,.4);
                      border-radius:20px; padding:3px 12px; font-size:0.65rem; font-weight:700;
                      color:#7fc8a9; letter-spacing:0.1em; text-transform:uppercase;">
            가천대학교 스마트시티학과 · 캡스톤디자인 2026
          </div>
          <div style="background:rgba(239,68,68,.25); border:1px solid rgba(239,68,68,.4);
                      border-radius:20px; padding:3px 10px; font-size:0.63rem; font-weight:700;
                      color:#fca5a5; letter-spacing:0.05em;">
            LIVE DASHBOARD
          </div>
        </div>
        <div style="font-size:1.7rem; font-weight:800; color:#ffffff; line-height:1.2;
                    margin-bottom:8px; letter-spacing:-0.02em;">
          🚸 성남시 어린이보호구역 교통안전 위험 분석
        </div>
        <div style="font-size:0.83rem; color:#94b8d8; line-height:1.6;">
          경사도 · 버스정류장이 어린이보호구역 교통사고에 미치는 영향 분석
          &nbsp;|&nbsp; 수정구 · 분당구 &nbsp;|&nbsp; 분석기간 2020 – 2022
        </div>
      </div>
      <div style="position:relative; z-index:1; display:flex; gap:20px; margin-left:24px; flex-shrink:0;">
        <div style="text-align:center; background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12);
                    border-radius:14px; padding:14px 20px; min-width:90px;">
          <div style="font-size:1.5rem; font-weight:800; color:#ffffff; line-height:1;">115</div>
          <div style="font-size:0.65rem; color:#7fc8a9; font-weight:600; margin-top:4px;
                      letter-spacing:0.06em; text-transform:uppercase;">보호구역</div>
        </div>
        <div style="text-align:center; background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12);
                    border-radius:14px; padding:14px 20px; min-width:90px;">
          <div style="font-size:1.5rem; font-weight:800; color:#ffffff; line-height:1;">83</div>
          <div style="font-size:0.65rem; color:#fca5a5; font-weight:600; margin-top:4px;
                      letter-spacing:0.06em; text-transform:uppercase;">사고건수</div>
        </div>
        <div style="text-align:center; background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12);
                    border-radius:14px; padding:14px 20px; min-width:90px;">
          <div style="font-size:1.5rem; font-weight:800; color:#ffffff; line-height:1;">3년</div>
          <div style="font-size:0.65rem; color:#93c5fd; font-weight:600; margin-top:4px;
                      letter-spacing:0.06em; text-transform:uppercase;">분석기간</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if not HAS_FOLIUM:
        st.error("streamlit-folium이 설치되지 않았습니다.  `py -m pip install streamlit-folium` 실행 후 재시작하세요.")
        st.stop()

    with st.spinner("데이터 로딩 중…"):
        data = load_data(str(ROOT))

    zones             = data["zones"]
    bus               = data["bus"]
    accidents         = data["accidents"]
    hotspots          = data["hotspots"]
    slope_df          = data["slope"]
    matched_accidents = data.get("matched_accidents", pd.DataFrame())
    boundaries        = data.get("boundaries", {})

    if zones.empty:
        st.error("보호구역 CSV 파일을 찾을 수 없습니다. 같은 폴더에 파일이 있는지 확인해 주세요.")
        st.stop()

    # 사이드바
    filter_gu, filter_grade, claude_api_key, naver_client_id, naver_client_secret, _, show_boundary, show_dong_labels, show_grid, show_accidents = render_sidebar(zones, matched_accidents)

    # ── KPI ──────────────────────────────────────────────────────────────────
    sub = zones.copy()
    if filter_gu != "전체":
        sub = sub[sub["구"] == filter_gu]

    n_total = len(sub)
    n_high  = (sub["위험등급"].astype(str) == "고위험").sum()
    n_mid   = (sub["위험등급"].astype(str) == "중위험").sum()
    n_low   = (sub["위험등급"].astype(str) == "저위험").sum()
    n_acc   = 83  # 공식 집계 총 사고 건수 (수정구 61 + 분당구 22)

    n_sj = len(sub[sub["구"] == "수정구"])
    n_bd = len(sub[sub["구"] == "분당구"])
    high_sj = ((sub["구"] == "수정구") & (sub["위험등급"].astype(str) == "고위험")).sum()
    high_bd = ((sub["구"] == "분당구") & (sub["위험등급"].astype(str) == "고위험")).sum()
    mid_sj  = ((sub["구"] == "수정구") & (sub["위험등급"].astype(str) == "중위험")).sum()
    mid_bd  = ((sub["구"] == "분당구") & (sub["위험등급"].astype(str) == "중위험")).sum()
    low_sj  = ((sub["구"] == "수정구") & (sub["위험등급"].astype(str) == "저위험")).sum()
    low_bd  = ((sub["구"] == "분당구") & (sub["위험등급"].astype(str) == "저위험")).sum()

    # 구역당 사고건수 계산
    acc_sj = 61
    acc_bd = 22
    rate_sj = round(acc_sj / n_sj, 2) if n_sj > 0 else 0
    rate_bd = round(acc_bd / n_bd, 2) if n_bd > 0 else 0

    ratio_str = f"{int(rate_sj/rate_bd*10)/10 if rate_bd>0 else '—'}배"
    kpi_html = f"""
    <div style="display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:18px;">
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #1e3a5f; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(30,58,95,.05);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#64748b; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">전체 보호구역</div>
        <div style="font-size:1.8rem; font-weight:800; color:#0f172a; line-height:1; margin-bottom:4px;">{n_total}<span style="font-size:0.9rem; font-weight:600; color:#64748b;">개소</span></div>
        <div style="font-size:0.7rem; color:#94a3b8;">수정구 {n_sj} · 분당구 {n_bd}</div>
      </div>
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #ef4444; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(239,68,68,.06);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#ef4444; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">🔴 고위험 구역</div>
        <div style="font-size:1.8rem; font-weight:800; color:#ef4444; line-height:1; margin-bottom:4px;">{n_high}<span style="font-size:0.9rem; font-weight:600; color:#fca5a5;">개</span></div>
        <div style="font-size:0.7rem; color:#94a3b8;">수정구 {high_sj} · 분당구 {high_bd}</div>
      </div>
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #f59e0b; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(245,158,11,.06);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#d97706; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">🟠 중위험 구역</div>
        <div style="font-size:1.8rem; font-weight:800; color:#d97706; line-height:1; margin-bottom:4px;">{n_mid}<span style="font-size:0.9rem; font-weight:600; color:#fcd34d;">개</span></div>
        <div style="font-size:0.7rem; color:#94a3b8;">수정구 {mid_sj} · 분당구 {mid_bd}</div>
      </div>
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #22c55e; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(34,197,94,.06);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#16a34a; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">🟢 저위험 구역</div>
        <div style="font-size:1.8rem; font-weight:800; color:#16a34a; line-height:1; margin-bottom:4px;">{n_low}<span style="font-size:0.9rem; font-weight:600; color:#86efac;">개</span></div>
        <div style="font-size:0.7rem; color:#94a3b8;">수정구 {low_sj} · 분당구 {low_bd}</div>
      </div>
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #7c3aed; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(124,58,237,.06);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#7c3aed; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">💥 총 사고건수</div>
        <div style="font-size:1.8rem; font-weight:800; color:#7c3aed; line-height:1; margin-bottom:4px;">{n_acc}<span style="font-size:0.9rem; font-weight:600; color:#c4b5fd;">건</span></div>
        <div style="font-size:0.7rem; color:#94a3b8;">수정구 61 · 분당구 22</div>
      </div>
      <div style="background:#fff; border-radius:16px; padding:18px 16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06); border-top:3px solid #0ea5e9; position:relative; overflow:hidden;">
        <div style="position:absolute;top:0;right:0;width:60px;height:60px;
                    background:rgba(14,165,233,.06);border-radius:0 16px 0 60px;"></div>
        <div style="font-size:0.62rem; font-weight:700; color:#0284c7; letter-spacing:0.07em;
                    text-transform:uppercase; margin-bottom:6px;">📈 구역당 사고</div>
        <div style="font-size:1.4rem; font-weight:800; color:#0284c7; line-height:1.1; margin-bottom:4px;">수정 {rate_sj}건</div>
        <div style="font-size:0.7rem; color:#94a3b8;">분당 {rate_bd}건 · <b style="color:#0284c7;">{ratio_str}</b> 차이</div>
      </div>
    </div>
    """
    st.markdown(kpi_html, unsafe_allow_html=True)

    # ── 지도 + 우측 패널 ──────────────────────────────────────────────────────
    st.markdown("""
    <div style="display:flex; align-items:center; gap:10px; padding:10px 16px 10px;
                background:#ffffff; border-radius:12px; margin-bottom:12px;
                border-left:4px solid #1e3a5f; box-shadow:0 1px 4px rgba(0,0,0,.06);">
      <span style="font-size:1.1rem;">🗺️</span>
      <div>
        <div style="font-size:0.92rem; font-weight:700; color:#0f172a;">위험도 분포 지도</div>
        <div style="font-size:0.72rem; color:#64748b;">격자를 클릭하면 해당 보호구역의 상세 정보와 현장 로드뷰를 확인할 수 있습니다.</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    fmap = build_map(zones, bus, hotspots, slope_df, filter_gu, filter_grade,
                     show_boundary, show_grid,
                     matched_accidents=matched_accidents,
                     show_accidents=show_accidents,
                     boundaries=boundaries,
                     show_dong_labels=show_dong_labels)

    col_roadview, col_map, col_right = st.columns([1.5, 2.5, 1.2])

    with col_map:
        map_data = st_folium(
            fmap, use_container_width=True, height=620,
            returned_objects=["last_object_clicked_tooltip", "last_object_clicked"],
        )

    selected_row = _parse_selected_zone(map_data, zones)

    with col_roadview:
        st.markdown("""
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;
                    padding:8px 12px; background:#fff; border-radius:10px;
                    border-left:3px solid #0ea5e9; box-shadow:0 1px 4px rgba(0,0,0,.05);">
          <span style="font-size:1rem;">📸</span>
          <div>
            <div style="font-size:0.82rem; font-weight:700; color:#0f172a;">현장 로드뷰</div>
            <div style="font-size:0.65rem; color:#64748b;">보호구역 클릭 시 해당 위치 로드뷰 표시</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        def _naver_pano_html(lat: float, lon: float, client_id: str) -> str:
            return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#1a1a1a}}
  #wrap {{position:relative;width:100%;height:455px}}
  #pano {{width:100%;height:100%}}
  :fullscreen        #wrap, :fullscreen        #pano,
  :-webkit-full-screen #wrap, :-webkit-full-screen #pano {{width:100vw;height:100vh}}
  :fullscreen body, :-webkit-full-screen body {{background:#000}}
  .ctrl-btn {{
    position:absolute;top:8px;z-index:999;
    background:rgba(0,0,0,0.58);color:#fff;border:none;
    border-radius:7px;padding:6px 12px;cursor:pointer;
    font-size:13px;font-family:sans-serif;backdrop-filter:blur(4px);
    transition:background 0.2s, transform 0.1s;line-height:1.2;
  }}
  .ctrl-btn:hover {{background:rgba(0,0,0,0.85);transform:scale(1.04)}}
  .ctrl-btn:active {{transform:scale(0.97)}}
  #fs-btn  {{right:8px}}
  #cam-btn {{right:118px}}
  #toast {{
    display:none;position:absolute;bottom:16px;left:50%;transform:translateX(-50%);
    background:rgba(0,0,0,0.75);color:#fff;padding:7px 18px;border-radius:20px;
    font-size:12px;font-family:sans-serif;pointer-events:none;z-index:9999;white-space:nowrap;
  }}
  .err {{display:flex;align-items:center;justify-content:center;
         height:100%;color:#fff;font-family:sans-serif;font-size:13px;
         flex-direction:column;gap:10px;background:#222;text-align:center}}
</style>
<script src="https://openapi.map.naver.com/openapi/v3/maps.js?ncpKeyId={client_id}&submodules=panorama"
        onerror="document.getElementById('pano').innerHTML=
          '<div class=err>⚠️ API 로드 실패<br><small>NCP 콘솔 → 서비스 URL에 localhost 추가 필요</small></div>'">
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
</head><body>
<div id="wrap">
  <div id="pano"></div>
  <button class="ctrl-btn" id="cam-btn" onclick="takeShot()" title="로드뷰 캡처">📷 캡처</button>
  <button class="ctrl-btn" id="fs-btn"  onclick="toggleFS()" title="전체화면">⛶ 전체화면</button>
  <div id="toast"></div>
</div>
<script>
  window.addEventListener('load', function() {{
    try {{
      new naver.maps.Panorama('pano', {{
        position: new naver.maps.LatLng({lat}, {lon}),
        pov: {{ pan: 0, tilt: 0, fov: 100 }}
      }});
    }} catch(e) {{
      document.getElementById('pano').innerHTML =
        '<div class=err>⚠️ 로드뷰 로딩 실패<br><small>' + e.message + '</small></div>';
    }}
  }});

  /* ── 전체화면 ── */
  function toggleFS() {{
    var isFS = !!(document.fullscreenElement || document.webkitFullscreenElement);
    if (isFS) {{
      (document.exitFullscreen || document.webkitExitFullscreen || function(){{}}).call(document);
    }} else {{
      var el = document.documentElement;
      var req = el.requestFullscreen || el.webkitRequestFullscreen || el.mozRequestFullScreen;
      if (req) {{
        var p = req.call(el);
        if (p && p.catch) p.catch(function(err) {{ showToast('전체화면 전환 실패: ' + err.message); }});
      }} else {{
        showToast('이 브라우저에서는 전체화면이 지원되지 않습니다.');
      }}
    }}
  }}
  document.addEventListener('fullscreenchange', syncFSBtn);
  document.addEventListener('webkitfullscreenchange', syncFSBtn);
  function syncFSBtn() {{
    var isFS = !!(document.fullscreenElement || document.webkitFullscreenElement);
    document.getElementById('fs-btn').textContent = isFS ? '✕ 닫기' : '⛶ 전체화면';
  }}

  /* ── 스크린샷 캡처 ── */
  function takeShot() {{
    var btn = document.getElementById('cam-btn');
    btn.disabled = true; btn.textContent = '⏳';
    // 1순위: WebGL 캔버스 직접 캡처
    var glCanvas = document.querySelector('#pano canvas');
    if (glCanvas) {{
      try {{
        var url = glCanvas.toDataURL('image/png');
        if (url && url.length > 100) {{ download(url); btn.textContent = '📷 캡처'; btn.disabled = false; return; }}
      }} catch(e) {{}}
    }}
    // 2순위: html2canvas 폴백
    if (typeof html2canvas !== 'undefined') {{
      html2canvas(document.getElementById('wrap'), {{useCORS:true, allowTaint:true, logging:false}})
        .then(function(c) {{ download(c.toDataURL('image/png')); }})
        .catch(function() {{ showToast('캡처 실패 — 브라우저 화면 캡처를 이용해 주세요.'); }})
        .finally(function() {{ btn.textContent = '📷 캡처'; btn.disabled = false; }});
    }} else {{
      showToast('캡처 라이브러리 로딩 중입니다. 잠시 후 다시 시도해 주세요.');
      btn.textContent = '📷 캡처'; btn.disabled = false;
    }}
  }}
  function download(url) {{
    var a = document.createElement('a');
    a.href = url;
    a.download = 'roadview_' + new Date().toISOString().slice(0,19).replace(/[:T]/g,'-') + '.png';
    a.click();
    showToast('📷 저장되었습니다!');
  }}

  /* ── 토스트 메시지 ── */
  function showToast(msg) {{
    var t = document.getElementById('toast');
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._tid);
    t._tid = setTimeout(function() {{ t.style.display = 'none'; }}, 3000);
  }}
</script>
</body></html>"""

        if naver_client_id:
            if selected_row is not None:
                lat  = float(selected_row["위도"])
                lon  = float(selected_row["경도"])
                zone_name = selected_row.get("대상시설명", "")
                st.components.v1.html(_naver_pano_html(lat, lon, naver_client_id), height=460, scrolling=False)
                st.caption(f"📍 {zone_name} 주변 네이버 로드뷰")
            else:
                default_lat, default_lon = 37.4201, 127.1265
                st.components.v1.html(_naver_pano_html(default_lat, default_lon, naver_client_id), height=460, scrolling=False)
                st.caption("💡 지도에서 보호구역 격자를 클릭하면 해당 지점의 로드뷰가 표시됩니다.")
        else:
            st.warning("⚠️ 사이드바에 Naver Maps Client ID를 입력하면 여기서 바로 로드뷰를 볼 수 있습니다.")
            if selected_row is not None:
                lat = float(selected_row["위도"])
                lon = float(selected_row["경도"])
                st.markdown(f"**{selected_row.get('대상시설명', '')}**")
                st.link_button("🗺️ Naver 로드뷰 (새 창)",
                               f"https://map.naver.com/v5/?c={lon},{lat},17,0,0,0,dh",
                               use_container_width=True)
                st.link_button("🗺️ Kakao 로드뷰 (새 창)",
                               f"https://map.kakao.com/link/roadview/{lat},{lon}",
                               use_container_width=True)
            else:
                st.info("💡 지도에서 보호구역 격자를 클릭하면 해당 구역의 로드뷰 링크가 활성화됩니다.")

    with col_right:
        render_policy_panel(zones, claude_api_key, selected_row)

    st.divider()

    with st.expander("🤖 안전이 AI 상담챗봇 — Claude 기반 교통안전 분석 도우미", expanded=False):
        render_character_chatbot(zones, claude_api_key, selected_row, naver_client_id, naver_client_secret)

    st.markdown("""
    <div style="text-align:center; padding:16px 0 8px; margin-top:12px;
                border-top:1px solid #e2e8f0;">
      <div style="font-size:11.5px; color:#94a3b8; line-height:2;">
        <b style="color:#64748b;">가천대학교 스마트시티학과</b> 캡스톤디자인 2026
        &nbsp;·&nbsp;
        데이터 출처: 경기도 공공데이터포털, 경찰청 TAAS
        &nbsp;·&nbsp;
        분석 기간: 2020 – 2022
      </div>
      <div style="font-size:10.5px; color:#cbd5e1; margin-top:2px;">
        본 대시보드는 학술 연구 목적으로 제작되었습니다.
      </div>
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
