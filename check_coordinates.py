import json

# 수정구 좌표 범위 (위도 37.42~37.48, 경도 127.12~127.17)
sujung_lat_min, sujung_lat_max = 37.42, 37.48
sujung_lon_min, sujung_lon_max = 127.12, 127.17

# 분당구 좌표 범위 (위도 37.33~37.42, 경도 127.09~127.15)
bundang_lat_min, bundang_lat_max = 37.33, 37.42
bundang_lon_min, bundang_lon_max = 127.09, 127.15

# 수정구 데이터 분석
with open('sujung_final_accidents.geojson', 'r', encoding='utf-8') as f:
    sujung_data = json.load(f)

sujung_features = sujung_data['features']
sujung_in_range = 0
sujung_out_range = []

print("=" * 80)
print("수정구 사고 데이터 (sujung_final_accidents.geojson) 분석")
print("=" * 80)
print(f"총 Feature 수: {len(sujung_features)}")
print(f"\n예상 좌표 범위: 위도 {sujung_lat_min}~{sujung_lat_max}, 경도 {sujung_lon_min}~{sujung_lon_max}")
print(f"\nProperties 필드 샘플 (첫 번째 Feature):")
print(f"  {sujung_features[0]['properties']}")

print(f"\n좌표 샘플 (처음 5개 Feature):")
for i in range(min(5, len(sujung_features))):
    lon, lat = sujung_features[i]['geometry']['coordinates']
    print(f"  #{i+1}: lon={lon:.6f}, lat={lat:.6f}")

print(f"\n범위 검증:")
for i, feature in enumerate(sujung_features):
    lon, lat = feature['geometry']['coordinates']
    if sujung_lon_min <= lon <= sujung_lon_max and sujung_lat_min <= lat <= sujung_lat_max:
        sujung_in_range += 1
    else:
        sujung_out_range.append((i, feature['properties'].get('보호구역', 'N/A'), lon, lat))

print(f"  범위 내: {sujung_in_range}개")
print(f"  범위 외: {len(sujung_out_range)}개")
if sujung_out_range:
    print(f"\n범위 외 데이터:")
    for idx, name, lon, lat in sujung_out_range[:10]:  # 처음 10개만 표시
        print(f"    #{idx+1} {name}: lon={lon:.6f}, lat={lat:.6f}")

# 분당구 데이터 분석
with open('bundang_final_accidents.geojson', 'r', encoding='utf-8') as f:
    bundang_data = json.load(f)

bundang_features = bundang_data['features']
bundang_in_range = 0
bundang_out_range = []

print("\n" + "=" * 80)
print("분당구 사고 데이터 (bundang_final_accidents.geojson) 분석")
print("=" * 80)
print(f"총 Feature 수: {len(bundang_features)}")
print(f"\n예상 좌표 범위: 위도 {bundang_lat_min}~{bundang_lat_max}, 경도 {bundang_lon_min}~{bundang_lon_max}")
print(f"\nProperties 필드 샘플 (첫 번째 Feature):")
print(f"  {bundang_features[0]['properties']}")

print(f"\n좌표 샘플 (처음 5개 Feature):")
for i in range(min(5, len(bundang_features))):
    lon, lat = bundang_features[i]['geometry']['coordinates']
    print(f"  #{i+1}: lon={lon:.6f}, lat={lat:.6f}")

print(f"\n범위 검증:")
for i, feature in enumerate(bundang_features):
    lon, lat = feature['geometry']['coordinates']
    if bundang_lon_min <= lon <= bundang_lon_max and bundang_lat_min <= lat <= bundang_lat_max:
        bundang_in_range += 1
    else:
        bundang_out_range.append((i, feature['properties'].get('보호구역', 'N/A'), lon, lat))

print(f"  범위 내: {bundang_in_range}개")
print(f"  범위 외: {len(bundang_out_range)}개")
if bundang_out_range:
    print(f"\n범위 외 데이터:")
    for idx, name, lon, lat in bundang_out_range:
        print(f"    #{idx+1} {name}: lon={lon:.6f}, lat={lat:.6f}")

# 전체 요약
print("\n" + "=" * 80)
print("전체 요약")
print("=" * 80)
total_features = len(sujung_features) + len(bundang_features)
print(f"수정구 GeoJSON Feature 수: {len(sujung_features)}")
print(f"분당구 GeoJSON Feature 수: {len(bundang_features)}")
print(f"전체 Feature 수: {total_features}")
print(f"\n검증 기준: 총 83건")
print(f"결과: {'PASS' if total_features == 83 else 'FAIL'} ({total_features}건)")

print(f"\n좌표 범위 검증:")
print(f"  수정구: {sujung_in_range}/{len(sujung_features)} 범위 내")
print(f"  분당구: {bundang_in_range}/{len(bundang_features)} 범위 내")
