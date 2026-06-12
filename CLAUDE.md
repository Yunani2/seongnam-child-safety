# CLAUDE.md

이 파일은 이 저장소에서 코드를 작업할 때 Claude Code(claude.ai/code)에게 제공하는 가이드라인입니다.

# 성남시 어린이보호구역 교통안전 분석 프로젝트 (2020-2022년)

## 📋 프로젝트 개요

**주제:** 경사도와 버스정류장 위치가 어린이보호구역 교통사고에 미치는 영향 분석  
**분석 기간:** 2020년 ~ 2022년 (3개년)  
**비교 대상:** 수정구 (급경사 구도심) vs 분당구 (평지 신도시)  
**최종 목표:** 위험도 예측 모델 개발 + 정책 제언

---

## 🗂️ 실제 사용 데이터 파일

1. **수정구 어린이 보호구역 내 교통사고(20-22).xlsx** (61건)  
   **분당구 어린이 보호구역 내 교통사고(20-22).xlsx** (22건)
   - 경찰청 TAAS에서 다운로드한 어린이보호구역 내 사고 원본 (좌표 없음)
   - 컬럼: 구분번호, 발생년월, 시군구, 사고내용, 사망자수, 중상자수, 경상자수

2. **수정구 보호구역 폴리곤 데이터.json** / **분당구 보호구역 폴리곤 데이터.json**
   - 경찰청 보호구역 API (공식 신청) 에서 받은 어린이보호구역 폴리곤 경계 데이터
   - 수정구: 63개 폴리곤 / 분당구: 95개 폴리곤
   - 구조: `response.body.items.item[]` 배열, 필드: ptznMngNo, trgtFcltNm, fturGeomVl

3. **seongnam_accidents.geojson** (11,339건)
   - 성남시 전체 교통사고 좌표 데이터 (2020-2022년)
   - 각 사고마다 WGS84 좌표 포함 (wgs84_x_crd, wgs84_y_crd)
   - 법정동코드(bjd_cd), 사고위치(acc_plc), 사고일자(acc_ymd) 포함

4. **경기도_성남시_교통약자_보호구역_파일데이터.csv**
   - 어린이보호구역 위치 정보 (중심점 좌표)
   - 초등학교, 유치원, 어린이집 등

5. **경기도_성남시_버스정류장_현황_20260408.csv** (1,309개소)
   - 버스정류장 좌표 정보

6. **DEM 데이터** (경사도 추출용)
   - 수치표고모델 래스터 데이터

### ✅ Step 1-2 완료 산출물 (이미 생성됨)

- **sujung_final_accidents.geojson** — 수정구 사고 60건 (정확 좌표 포함)
- **bundang_final_accidents.geojson** — 분당구 사고 22건 (정확 좌표 포함)
- **outputs/accidents_with_coords.csv** — 위 두 파일을 통합한 CSV (대시보드 자동 생성)

---

## 🎯 분석 목표

### 연구 질문 (Research Questions)

**RQ1:** 경사도가 높을수록 사고 건수와 심각도가 증가하는가?  
**RQ2:** 버스정류장에 가까울수록 사고 건수와 심각도가 증가하는가?  
**RQ3:** 경사도와 버스정류장의 복합 효과가 존재하는가? (교차효과)

---

## 📊 변수 정의

### 독립변수

#### 1. 경사도 범주 (slope_category): 순서형 0-4
```
0 = 평지 (0-3도)
1 = 완만한 경사 (3-5도)
2 = 보통 경사 (5-8도)
3 = 급경사 (8-12도)
4 = 매우 급경사 (12도+)
```

#### 2. 버스정류장 근접도 (bus_category): 순서형 0-3
```
0 = 멀리 (100m 초과)
1 = 중간 (50-100m)
2 = 인접 (30-50m)
3 = 매우 인접 (30m 이내)
```

### 종속변수

#### 1. 사고 건수 (accident_count): 연속형 (count data)
- 각 보호구역별 사고 발생 건수 (2020-2022년 합계)

#### 2. 사고 심각도 (severity): 범주형
```
경상 (minor): 58건
중상 (serious): 25건
사망 (fatal): 0건
```

---

## 🔬 분석 단계

### STEP 1: 데이터 전처리 (6단계)

#### 1-1. 어린이보호구역 필터링
```
입력: 경기도_성남시_교통약자_보호구역_파일데이터.csv

필터링:
  - 시설종류 IN ['초등학교', '유치원', '어린이집', '특수학교']
  - 시군구명 IN ['성남시 수정구', '성남시 분당구']
  - 노인시설 제외

출력: schoolzones.csv
  컬럼: zone_id, name, type, district, lat, lng, address
```

#### 1-2. 사고 데이터에 좌표 추가 ⭐ 핵심! (✅ 완료)
```
[실제 수행한 방법 — 공간 조인 (Point-in-Polygon)]

입력:
  - 수정구/분당구 xlsx (83건, 좌표 없음)
  - 수정구 보호구역 폴리곤 데이터.json (경찰청 공식 신청, 63개 폴리곤)
  - 분당구 보호구역 폴리곤 데이터.json (경찰청 공식 신청, 95개 폴리곤)
  - seongnam_accidents.geojson (11,339건, WGS84 좌표 포함)

매칭 방법:
  Step 1: 경찰청 폴리곤 데이터로 어린이보호구역 경계 획득
    - fturGeomVl 필드에 WKT/좌표 형식의 폴리곤 경계 포함

  Step 2: seongnam_accidents.geojson 사고 좌표 추출
    - 수정구: bjd_cd 시작 '411311' → 약 3,089건
    - 분당구: bjd_cd 시작 '411351' → 약 4,702건

  Step 3: 공간 조인 (Point-in-Polygon)
    - 각 사고 좌표가 보호구역 폴리곤 내부에 있는지 판별
    - 폴리곤 내부 사고만 선별
    - xlsx의 구분번호·발생년월·사상자 정보와 매칭

결과:
  - sujung_final_accidents.geojson: 수정구 60건 (원본 61건 중 1건 폴리곤 매칭 불가)
  - bundang_final_accidents.geojson: 분당구 22건 (100% 매칭)
  - 합계 82건 / 매칭률 98.8%

출력: outputs/accidents_with_coords.csv (대시보드 실행 시 자동 생성)
  컬럼: accident_id, district, date, address, lat, lng,
        severity, deaths, serious, minor, match_quality

※ 미매칭 1건: 구분번호 2022035846 (수정구, 경상 4명)
   — 해당 사고 발생지가 보호구역 폴리곤 외부 또는 POINT만 존재하는 구역
```

**좌표 검증 결과 (2026-06-04):**
```
수정구 60건: 위도 37.435~37.474, 경도 127.125~127.162 → 수정구 범위 내 ✅
분당구 22건: 위도 37.360~37.421, 경도 127.097~127.143 → 분당구 범위 내 ✅
```

#### 1-3. DEM 기반 경사도 추출
```
입력: DEM 래스터 데이터

처리:
  FOR 각 어린이보호구역:
    1. 보호구역 중심점 좌표 추출
    2. DEM에서 해당 위치의 경사도 계산
       공식: slope = arctan(√((dz/dx)² + (dz/dy)²)) × 180/π
    3. 도(degree) 단위 변환
    4. 범주화 (0-4)

출력: slope.csv
  컬럼: zone_id, slope_degrees, slope_category

※ DEM 처리 어려운 경우 대안:
  - TAAS 통계 참고
  - 수정구: 경사 높음 (범주 3-4 비율 높게)
  - 분당구: 경사 낮음 (범주 0-1 비율 높게)
```

#### 1-4. 버스정류장 거리 계산
```
입력: 
  - schoolzones.csv
  - 경기도_성남시_버스정류장_현황.csv

처리:
  FOR 각 보호구역:
    1. Haversine 거리 계산 (모든 버스정류장)
       공식:
       a = sin²(Δlat/2) + cos(lat1)·cos(lat2)·sin²(Δlon/2)
       c = 2·atan2(√a, √(1-a))
       d = R·c (R = 6,371km)
    
    2. 최소 거리 선택
    3. 범주화 (0-3)

출력: bus_proximity.csv
  컬럼: zone_id, nearest_bus_distance_m, bus_category
```

#### 1-5. 보호구역별 사고 집계
```
입력: 
  - accidents_with_coords.csv (좌표 포함)
  - schoolzones.csv

처리:
  FOR 각 보호구역:
    1. 보호구역 중심에서 반경 200m 이내 사고 필터
    2. 사고 건수 집계
    3. 심각도별 집계
    4. 2020+2021+2022 합계

출력: accident_aggregated.csv
  컬럼: zone_id, accident_count, fatal_count, serious_count, minor_count
```

#### 1-6. 최종 통합
```
LEFT JOIN:
  schoolzones 
  + slope 
  + bus_proximity 
  + accident_aggregated
  ON zone_id

출력: final_dataset.csv
  컬럼:
    zone_id, name, type, district, lat, lng,
    slope_category, bus_category,
    accident_count, fatal_count, serious_count, minor_count
```

---

### STEP 2: 기술통계

#### 2-1. 수정구 vs 분당구 비교표
```
생성할 표:

┌──────────────┬─────────┬─────────┬─────────┐
│ 항목         │ 수정구  │ 분당구  │ 비율    │
├──────────────┼─────────┼─────────┼─────────┤
│ 보호구역 수  │   41개  │   74개  │  1:1.8  │
│ 총 사고      │   61건  │   22건  │  2.8:1  │
│ 구역당 사고  │  1.49건 │  0.30건 │  5.0:1  │
└──────────────┴─────────┴─────────┴─────────┘

심각도 분포:
- 수정구: 경상 XX건, 중상 XX건, 사망 0건
- 분당구: 경상 XX건, 중상 XX건, 사망 0건
```

---

### STEP 3: RQ1 분석 (경사도 영향)

#### 3-1. 경사도 vs 사고 건수

**Spearman 상관분석**
```python
from scipy.stats import spearmanr

rho, p_value = spearmanr(
    final_dataset['slope_category'], 
    final_dataset['accident_count']
)

해석:
  ρ > 0.6, p < 0.001 → "강한 양의 상관"
```

**시각화: 산점도**
```
- X축: slope_category (0-4)
- Y축: accident_count
- 점 색상: 수정구(빨강), 분당구(초록)
- 추세선 추가
```

#### 3-2. 경사도 vs 사고 심각도

**Chi-square Test**
```python
from scipy.stats import chi2_contingency

table = pd.crosstab(
    final_dataset['slope_category'],
    final_dataset['dominant_severity']
)

chi2, p, dof, expected = chi2_contingency(table)
```

**시각화: 누적 막대그래프**
```
- X축: slope_category
- Y축: 비율 (%)
- 색상: 경상(초록), 중상(주황), 사망(빨강)
```

---

### STEP 4: RQ2 분석 (버스정류장 영향)

#### 4-1. 버스정류장 vs 사고 건수

**Spearman 상관분석**
```python
rho, p = spearmanr(
    final_dataset['bus_category'],
    final_dataset['accident_count']
)
```

**시각화: 산점도**

#### 4-2. 버스정류장 vs 사고 심각도

**Chi-square Test**
```python
table = pd.crosstab(
    final_dataset['bus_category'],
    final_dataset['dominant_severity']
)
chi2, p, dof, expected = chi2_contingency(table)
```

**시각화: 누적 막대그래프**

---

### STEP 5: RQ3 분석 (Poisson Regression) ⭐ 핵심!

#### 5-1. 모델 구축

**Model 1: 주효과만**
```python
import statsmodels.api as sm

X = final_dataset[['slope_category', 'bus_category']]
X = sm.add_constant(X)
y = final_dataset['accident_count']

model1 = sm.GLM(y, X, family=sm.families.Poisson()).fit()

print(model1.summary())
```

**Model 2: 교차효과 포함**
```python
X['slope_x_bus'] = (
    final_dataset['slope_category'] * 
    final_dataset['bus_category']
)

model2 = sm.GLM(y, X, family=sm.families.Poisson()).fit()

print(model2.summary())
```

#### 5-2. 모델 비교

**Likelihood Ratio Test**
```python
from scipy.stats import chi2

LR = -2 * (model1.llf - model2.llf)
p_value = 1 - chi2.cdf(LR, df=1)

print(f"LR = {LR:.2f}, p = {p_value:.4f}")

if p_value < 0.05:
    print("→ Model 2 선택 (교차효과 유의미)")
else:
    print("→ Model 1 선택 (교차효과 없음)")
```

**AIC 비교**
```python
print(f"Model 1 AIC: {model1.aic:.2f}")
print(f"Model 2 AIC: {model2.aic:.2f}")
```

#### 5-3. 계수 해석

```python
import numpy as np

# IRR (Incidence Rate Ratio) 계산
irr = np.exp(model2.params)

print("\n=== 계수 해석 ===")
print(f"β₀ (절편) = {model2.params[0]:.3f}")
print(f"β₁ (경사도) = {model2.params[1]:.3f} → IRR = {irr[1]:.2f}")
print(f"  → 경사 1단계 ↑ = 사고 {(irr[1]-1)*100:.1f}% 증가")

print(f"β₂ (버스) = {model2.params[2]:.3f} → IRR = {irr[2]:.2f}")
print(f"  → 버스 1단계 ↑ = 사고 {(irr[2]-1)*100:.1f}% 증가")

print(f"β₃ (교차) = {model2.params[3]:.3f} → IRR = {irr[3]:.2f}")
print(f"  → 복합 효과 {(irr[3]-1)*100:.1f}% 추가 증가")
```

#### 5-4. 위험도 히트맵

```python
# 5×4 매트릭스 생성
risk_matrix = np.zeros((5, 4))

for slope in range(5):
    for bus in range(4):
        linear_pred = (
            model2.params[0] +           # 절편
            model2.params[1] * slope +   # 경사도
            model2.params[2] * bus +     # 버스
            model2.params[3] * slope * bus  # 교차항
        )
        risk_matrix[slope, bus] = np.exp(linear_pred)

# 히트맵 시각화
import seaborn as sns
import matplotlib.pyplot as plt

plt.figure(figsize=(8, 6))
sns.heatmap(
    risk_matrix,
    annot=True,
    fmt='.2f',
    xticklabels=['100m+', '50-100m', '30-50m', '<30m'],
    yticklabels=['평지', '완만', '보통', '급경사', '매우급'],
    cmap='YlOrRd',
    cbar_kws={'label': 'Expected Accidents'}
)
plt.xlabel('버스정류장 근접도')
plt.ylabel('경사도 범주')
plt.title('위험도 히트맵 (2020-2022)')
plt.tight_layout()
plt.savefig('risk_heatmap.png', dpi=300)
```

---

### STEP 6: 결과 종합

#### 6-1. 통계 결과 요약표

```markdown
| 분석 | 독립변수 | 종속변수 | 통계량 | p-value | 결과 |
|------|---------|---------|--------|---------|------|
| Spearman | 경사도 | 사고건수 | ρ=0.XX | <0.001 | 유의 ✓ |
| Spearman | 버스정류장 | 사고건수 | ρ=0.XX | <0.001 | 유의 ✓ |
| χ² test | 경사도 | 심각도 | χ²=XX.X | <0.05 | 유의 ✓ |
| χ² test | 버스정류장 | 심각도 | χ²=XX.X | <0.05 | 유의 ✓ |
| Poisson | 교차효과 | 사고건수 | β₃=0.XX | <0.05 | 유의 ✓ |
```

#### 6-2. 최종 회귀 모델

```
log(사고) = β₀ + β₁·경사도 + β₂·버스정류장 + β₃·(경사×버스)

결론:
1. 경사도가 사고에 가장 큰 영향
2. 버스정류장도 유의미한 영향
3. 두 요인의 교차효과 존재
4. 수정구 > 분당구 차이의 주요 원인은 경사도
```

---

## 📊 최종 출력물

### 1. 데이터 파일 (4개)
```
- final_dataset.csv (통합 데이터)
- accidents_with_coords.csv (좌표 추가)
- analysis_results.xlsx (통계 결과)
- risk_heatmap.png (히트맵)
```

### 2. 통계 결과
```
- 기술통계 표
- 상관분석 결과 (ρ, p-value)
- 카이제곱 검정 결과
- Poisson Regression 계수표
- IRR 해석표
```

### 3. 시각화 (5개)
```
1. 산점도 - 경사도 vs 사고
2. 산점도 - 버스 vs 사고
3. 누적 막대그래프 - 경사도별 심각도
4. 누적 막대그래프 - 버스별 심각도
5. 히트맵 - 위험도 매트릭스
```

---

## ⚠️ 주의사항

### 좌표 매칭률이 낮은 경우
```
대안:
1. 주소 기반 Geocoding (Kakao/Naver API)
2. 동 단위 평균 좌표 사용
3. 매칭된 데이터만 사용 (한계점 명시)
```

### DEM 처리 어려운 경우
```
대안:
1. TAAS 통계 참고하여 경사도 추정
2. 수정구: 경사 높음 (3-4)
3. 분당구: 경사 낮음 (0-1)
4. 한계점 명시: "실측 경사도 대신 추정치 사용"
```

---

## ✅ 완료 체크리스트

- [ ] 83건 중 70건 이상 좌표 매칭 (85%+)
- [ ] 모든 보호구역 경사도 값 보유
- [ ] 모든 보호구역 버스정류장 거리 계산
- [ ] Poisson Regression 수렴
- [ ] 히트맵 생성 (5×4)
- [ ] 시각화 5개 완성
- [ ] 모든 p-value 확인
- [ ] 해석 문장 작성

---


