import json
import re
import os
import unicodedata
import itertools
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import streamlit as st
from typing import List, Dict

# Playwrightブラウザのセットアップ
import sys
os.system(f"{sys.executable} -m playwright install chromium")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config_data.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

CONFIG = load_config()

PARAMS = CONFIG.get("PARAMS", {})
JOCKEY_RANKS = CONFIG.get("JOCKEY_RANKS", {})
GRADE_SCORES = CONFIG.get("GRADE_SCORES", {})
GRADE_RACE_MAP = CONFIG.get("GRADE_RACE_MAP", {})

JRA_TRACKS = ["東京", "中山", "阪神", "京都", "中京", "小倉", "新潟", "福島", "札幌", "函館"]

RACE_GRADE_DICT = {}
for g_level, r_list in GRADE_RACE_MAP.items():
    for r_name in r_list:
        RACE_GRADE_DICT[r_name] = g_level

KANJI_REPLACE_MAP = str.maketrans({
    "戶": "戸", "⺠": "民", "櫻": "桜", "髙": "高", "﨑": "崎",
    "廐": "厩", "真": "真", "實": "実", "榮": "栄", "國": "国",
    "萬": "万", "廣": "広", "島": "島", "澤": "沢"
})

def normalize_text(text):
    if not text:
        return ""
    text = str(text).translate(KANJI_REPLACE_MAP)
    text = unicodedata.normalize("NFKC", text)
    return text.upper().strip()

def log_debug(msg, is_simple=False):
    if not is_simple:
        st.text(msg)

def detect_grade(run, default="1勝C"):
    explicit_grade = run.get("grade")
    if explicit_grade:
        norm_grade = normalize_text(explicit_grade)
        if norm_grade in GRADE_SCORES:
            return norm_grade

    r_name_raw = str(run.get("race_name", "") or run.get("race_title", "")).strip()
    r_name = normalize_text(r_name_raw)

    for g_level, r_list in GRADE_RACE_MAP.items():
        for race_keyword in r_list:
            norm_keyword = normalize_text(race_keyword)
            if norm_keyword and norm_keyword in r_name:
                return g_level

    if re.search(r'(\bJPN1\b|\bJPN2\b|\bJPN3\b|\bJPN\b|JPNⅠ|JPNⅡ|JPNⅢ)', r_name):
        return "JPN"
    elif re.search(r'(\bG1\b|GⅠ|\bJ\.?G1\b)', r_name):
        return "G1"
    elif re.search(r'(\bG2\b|GⅡ|\bJ\.?G2\b)', r_name):
        return "G2"
    elif re.search(r'(\bG3\b|GⅢ|\bJ\.?G3\b)', r_name):
        return "G3"
    elif re.search(r'(\(L\)|リステッド|\bL\b)', r_name):
        return "L"
    elif re.search(r'(オープン|\bOP\b)', r_name):
        return "OP"
    elif re.search(r'(3勝|3勝C|3勝クラス|1600万|1600万下)', r_name):
        return "3勝C"
    elif re.search(r'(2勝|2勝C|2勝クラス|1000万|1000万下)', r_name):
        return "2勝C"
    elif re.search(r'(1勝|1勝C|1勝クラス|500万|500万下)', r_name):
        return "1勝C"
    elif "新馬" in r_name:
        return "新馬"
    elif ("未勝利" in r_name) or ("未勝" in r_name):
        return "未勝利"

    return default

def get_jockey_score_and_rank(jockey_name):
    j_str = str(jockey_name).strip()
    
    if any(symbol in j_str for symbol in ['☆', '△', '▲', '◇']):
        return -2, "D", "減点記号付き騎手"
    
    for rank, j_list in JOCKEY_RANKS.items():
        if j_str in j_list:
            if rank == "SS": return 5, "SS", f"SSランク（{j_str}）"
            elif rank == "S": return 3, "S", f"Sランク（{j_str}）"
            elif rank == "A": return 1, "A", f"Aランク（{j_str}）"
            elif rank == "B": return 0, "B", f"Bランク（{j_str}）"
            elif rank == "C": return -1, "C", f"Cランク（{j_str}）"
            
    is_foreign_pattern = bool(re.fullmatch(r'^[\u30A0-\u30FF A-Za-z・ー\.\-]+$', j_str))
    
    if is_foreign_pattern:
        return 1, "A", f"外国人・未登録（外人表記判定: Aランク [+1点] {j_str}）"

    return -2, "D", f"その他（Dランク: {j_str}）"

def calc_raw_r_score(run):
    run_track = normalize_text(run.get("track", ""))
    race_title = normalize_text(run.get('race_name', '') or run.get('race_title', ''))

    grade = detect_grade(run, default="1勝C")
    is_jpn_race = (grade == "JPN")

    if run.get("is_foreign_or_local", False) and not is_jpn_race:
        return 0.0, "地方・海外競馬のため対象外", False

    LOCAL_TRACKS = ["大井", "川崎", "船橋", "浦和", "門別", "盛岡", "水沢", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀", "韓"]
    is_local_or_foreign = any(lt in run_track or lt in race_title for lt in LOCAL_TRACKS)
    is_valid_track = (any(t in run_track for t in JRA_TRACKS) or any(t in race_title for t in JRA_TRACKS)) and not is_local_or_foreign

    if not is_valid_track and not is_jpn_race:
        return 0.0, f"対象外の競馬場（{race_title}）のため計算対象外", False

    g_score = GRADE_SCORES.get(grade, 10)

    rank = run.get("rank", 99)
    if rank == 1: r_score = 10
    elif rank == 2: r_score = 8
    elif rank == 3: r_score = 5
    elif rank == 4: r_score = 3
    elif rank == 5: r_score = 1
    else: r_score = 0

    diff = run.get("diff", 9.9)
    if rank == 1:
        if diff <= 0.2: diff_score = 20
        elif diff <= 0.4: diff_score = 25
        else: diff_score = 30
    else:
        if diff == 0.0: diff_score = 20
        elif diff == 0.1: diff_score = 15
        elif diff == 0.2: diff_score = 12
        elif diff == 0.3: diff_score = 10
        elif diff == 0.4: diff_score = 7
        elif diff == 0.5: diff_score = 3
        else: diff_score = 0

    pop = run.get("pop", 99)
    if pop == 1: pop_score = 3
    elif pop == 2: pop_score = 2
    elif pop == 3: pop_score = 1
    else: pop_score = 0

    total = g_score + r_score + diff_score + pop_score
    detail = f"（{race_title} / 判定格付:[{grade}]）: 格点{g_score} ＋ 着順点{r_score} ＋ 着差点{diff_score} ＋ 人気点{pop_score} ＝ 生R_Score[{total}]"
    return total, detail, True

def estimate_position_type_final_corner(past_runs):
    if not past_runs:
        return "差"

    ratios = []
    for run in past_runs:
        pass_4 = run.get("final_corner_rank") or run.get("pass_4") or run.get("corner_4")
        total_horses = run.get("total_horses") or run.get("head_count") or run.get("head_num")

        if pass_4 and total_horses and total_horses > 0:
            try:
                ratio = float(pass_4) / float(total_horses)
                ratios.append(ratio)
            except (ValueError, TypeError):
                continue

    if not ratios:
        return "差"

    avg_ratio = sum(ratios) / len(ratios)

    if avg_ratio <= 0.15: return "逃"
    elif avg_ratio <= 0.40: return "先"
    elif avg_ratio <= 0.75: return "差"
    else: return "追"

# ============================================================
# スコアベースの荒れ度判定ロジック
# ============================================================
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))

def calc_support_rates(odds: List[float]) -> List[float]:
    inverse = [1.0 / x for x in odds if x > 0]
    total = sum(inverse)
    if total == 0:
        return [0.0 for _ in odds]
    return [x / total for x in inverse]

def calculate_race_features(odds: List[float]) -> Dict:
    odds = sorted(odds)
    if len(odds) < 5:
        raise ValueError("最低5頭分の単勝オッズが必要です")
    support = calc_support_rates(odds)
    
    odds_1 = odds[0]
    odds_2 = odds[1]
    odds_3 = odds[2]
    odds_5 = odds[4] if len(odds) >= 5 else odds[-1]
    
    ratio_2_1 = odds_2 / odds_1 if odds_1 > 0 else 0
    ratio_3_1 = odds_3 / odds_1 if odds_1 > 0 else 0
    ratio_5_1 = odds_5 / odds_1 if odds_1 > 0 else 0
    
    support_1 = support[0] if len(support) > 0 else 0
    support_top2 = sum(support[:2])
    support_top3 = sum(support[:3])
    support_top5 = sum(support[:5])
    
    count_under5 = sum(x < 5 for x in odds)
    count_5_10 = sum(5 <= x < 10 for x in odds)
    count_10_20 = sum(10 <= x < 20 for x in odds)
    count_20_50 = sum(20 <= x < 50 for x in odds)
    count_over50 = sum(x >= 50 for x in odds)
    
    return {
        "出走頭数": len(odds),
        "1番人気オッズ": odds_1,
        "2番人気オッズ": odds_2,
        "3番人気オッズ": odds_3,
        "5番人気オッズ": odds_5,
        "2人気_1人気比": ratio_2_1,
        "3人気_1人気比": ratio_3_1,
        "5人気_1人気比": ratio_5_1,
        "1番人気支持率": support_1,
        "上位2頭支持率": support_top2,
        "上位3頭支持率": support_top3,
        "上位5頭支持率": support_top5,
        "5倍未満頭数": count_under5,
        "5_10倍頭数": count_5_10,
        "10_20倍頭数": count_10_20,
        "20_50倍頭数": count_20_50,
        "50倍以上頭数": count_over50,
    }

def calculate_rough_score(features: Dict) -> Dict:
    score_odds1 = clamp((features["1番人気オッズ"] - 1.5) / (6.0 - 1.5))
    score_odds2 = clamp((features["2番人気オッズ"] - 3.0) / (10.0 - 3.0))
    score_odds3 = clamp((features["3番人気オッズ"] - 4.0) / (15.0 - 4.0))
    score_odds5 = clamp((features["5番人気オッズ"] - 6.0) / (25.0 - 6.0))
    odds_score = (
        score_odds1 * 8 +
        score_odds2 * 5 +
        score_odds3 * 4 +
        score_odds5 * 3
    )
    
    ratio21_score = 1 - clamp((features["2人気_1人気比"] - 1.0) / 2.0)
    ratio31_score = 1 - clamp((features["3人気_1人気比"] - 1.0) / 3.0)
    ratio51_score = 1 - clamp((features["5人気_1人気比"] - 1.0) / 5.0)
    ratio_score = (
        ratio21_score * 8 +
        ratio31_score * 7 +
        ratio51_score * 5
    )
    
    support1 = features["1番人気支持率"]
    support1_score = (1 - clamp((support1 - 0.15) / (0.40 - 0.15))) * 15
    
    top2_score = (1 - clamp((features["上位2頭支持率"] - 0.30) / (0.65 - 0.30)))
    top3_score = (1 - clamp((features["上位3頭支持率"] - 0.40) / (0.75 - 0.40)))
    top5_score = (1 - clamp((features["上位5頭支持率"] - 0.55) / (0.90 - 0.55)))
    concentration_score = (
        top2_score * 6 +
        top3_score * 8 +
        top5_score * 6
    )
    
    n = features["出走頭数"]
    if n > 0:
        middle_ratio = (
            features["5_10倍頭数"] +
            features["10_20倍頭数"] +
            features["20_50倍頭数"]
        ) / n
        favorite_ratio = features["5倍未満頭数"] / n
    else:
        middle_ratio = 0
        favorite_ratio = 0
        
    distribution_score = (
        clamp(middle_ratio / 0.60) * 15 +
        clamp((0.30 - favorite_ratio) / 0.30) * 5
    )
    
    horse_count_score = clamp((features["出走頭数"] - 10) / 8) * 5
    
    total_score = (
        odds_score +
        ratio_score +
        support1_score +
        concentration_score +
        distribution_score +
        horse_count_score
    )
    total_score = round(total_score, 1)
    
    if total_score < 20:
        category = "堅"
    elif total_score < 35:
        category = "並"
    elif total_score < 60:
        category = "小荒"
    elif total_score < 75:
        category = "中荒"
    else:
        category = "大荒"
        
    return {
        "荒れスコア": total_score,
        "荒れ度": category,
        "上位人気オッズ点": round(odds_score, 1),
        "オッズ比点": round(ratio_score, 1),
        "1番人気支持率点": round(support1_score, 1),
        "支持集中度点": round(concentration_score, 1),
        "オッズ分布点": round(distribution_score, 1),
        "出走頭数点": round(horse_count_score, 1),
    }

def analyze_race(odds: List[float]) -> Dict:
    features = calculate_race_features(odds)
    result = calculate_rough_score(features)
    return {
        **features,
        **result
    }

PLACE_CODE_MAP = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"
}

# 各会場ごとの購入目安（オッズレンジ）定義
VENUE_ODDS_RANGES = {
    "東京": {"堅": "10～30倍", "並": "20～40倍", "小荒": "20～40倍", "中荒": "30～60倍", "大荒": "60～90倍"},
    "京都": {"堅": "10～30倍", "並": "10～30倍", "小荒": "20～40倍", "中荒": "20～50倍", "大荒": "50～80倍"},
    "阪神": {"堅": "10～30倍", "並": "10～30倍", "小荒": "20～40倍", "中荒": "20～50倍", "大荒": "50～80倍"},
    "中山": {"堅": "10～30倍", "並": "10～30倍", "小荒": "30～50倍", "中荒": "30～60倍", "大荒": "60～90倍"},
    "福島": {"堅": "10～30倍", "並": "10～30倍", "小荒": "10～30倍", "中荒": "30～60倍", "大荒": "60～90倍"},
    "新潟": {"堅": "10～30倍", "並": "10～30倍", "小荒": "20～40倍", "中荒": "20～50倍", "大荒": "50～80倍"},
    "函館": {"堅": "10～30倍", "並": "10～30倍", "小荒": "10～30倍", "中荒": "20～50倍", "大荒": "50～80倍"},
    "札幌": {"堅": "10～30倍", "並": "10～30倍", "小荒": "30～50倍", "中荒": "30～60倍", "大荒": "60～90倍"},
    "中京": {"堅": "10～30倍", "並": "10～30倍", "小荒": "10～30倍", "中荒": "30～60倍", "大荒": "60～90倍"},
    "小倉": {"堅": "10～30倍", "並": "20～40倍", "小荒": "30～50倍", "中荒": "40～70倍", "大荒": "60～90倍"}
}

# ご提示いただいた「購入に○がついている組み合わせ」の定義
VALID_PURCHASE_COMBINATIONS = {
    "東京": {"堅": True,  "並": True,  "小荒": True,  "中荒": True,  "大荒": False},
    "京都": {"堅": True,  "並": True,  "小荒": True, "中荒": False, "大荒": False},
    "阪神": {"堅": True,  "並": True,  "小荒": True,  "中荒": False,  "大荒": False},
    "中山": {"堅": False,  "並": True, "小荒": True, "中荒": False, "大荒": False},
    "福島": {"堅": False,  "並": True,  "小荒": True,  "中荒": True,  "大荒": False},
    "新潟": {"堅": False,  "並": True,  "小荒": True, "中荒": True,  "大荒": False},
    "函館": {"堅": True,  "並": True,  "小荒": True,  "中荒": False, "大荒": False},
    "札幌": {"堅": True,  "並": True,  "小荒": True, "中荒": True,  "大荒": False},
    "中京": {"堅": True,  "並": True,  "小荒": True,  "中荒": False, "大荒": False},
    "小倉": {"堅": False,  "並": True,  "小荒": True, "中荒": False, "大荒": False}
}

def run_pipeline(df, race_info, good_horses=None, bad_horses=None, is_simple=False, trend="フラット"):
    if good_horses is None: good_horses = []
    if bad_horses is None: bad_horses = []

    raw_text = str(race_info.get("raw_header", "")) + str(race_info.get("race_name", "")) + str(race_info.get("track", ""))
    
    track = next((t for t in JRA_TRACKS if t in raw_text), race_info.get("track_name", "東京"))
    race_no = race_info.get("race_no") or (int(re.search(r'(\d{1,2})\s*R', raw_text, re.IGNORECASE).group(1)) if re.search(r'(\d{1,2})\s*R', raw_text, re.IGNORECASE) else 11)

    df_target = df.copy().reset_index(drop=True)
    df_target["is_good_condition"] = df_target["馬番"].isin(good_horses)
    df_target["is_bad_condition"] = df_target["馬番"].isin(bad_horses)

    try:
        tansho_odds = sorted([float(o) for o in df_target["単勝オッズ"].dropna() if float(o) > 0])
    except Exception:
        tansho_odds = []

    if len(tansho_odds) >= 5:
        try:
            analysis_result = analyze_race(tansho_odds)
            race_pattern = analysis_result["荒れ度"]
            rough_score = analysis_result["荒れスコア"]
            
            # 会場・荒れ度の組み合わせに基づいて購入条件（○）に合致するか判定
            track_purchase_map = VALID_PURCHASE_COMBINATIONS.get(track, {})
            is_marked_circle = track_purchase_map.get(race_pattern, False)
            display_race_pattern = f"{race_pattern}{'★' if is_marked_circle else ''}"

            analysis_table_md = (
                "\n| 荒れ度判定 | 荒れスコア | 1番人気オッズ | 2番人気オッズ | 3番人気オッズ | 1番人気支持率 |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                f"| **{display_race_pattern}** | **{rough_score}点** | {analysis_result['1番人気オッズ']:.1f}倍 | {analysis_result['2番人気オッズ']:.1f}倍 | {analysis_result['3番人気オッズ']:.1f}倍 | {analysis_result['1番人気支持率']*100:.1f}% |"
            )
        except Exception:
            race_pattern = "データ不足"
            display_race_pattern = "データ不足"
            rough_score = 0.0
            analysis_table_md = "\n* **荒れ度分析**: 計算エラー"
    else:
        race_pattern = "データ不足"
        display_race_pattern = "データ不足"
        rough_score = 0.0
        analysis_table_md = "\n* **荒れ度分析**: データ不足のため算出不可"

    surface = "ダート" if "ダート" in raw_text else ("芝" if "芝" in raw_text else str(race_info.get("track", "")))
    
    try:
        distance = int(re.sub(r'\D', '', str(race_info.get("distance", 1800))))
    except ValueError:
        distance = 1800

    race_name = race_info.get("race_name", "第XX回 レース")
    
    param = PARAMS.get(track, PARAMS.get("東京", {"Odds_W": 0.70, "Ability_W": 0.30}))
    odds_w, ability_w = param.get("Odds_W", 0.70), param.get("Ability_W", 0.30)

    avg_weight = df_target["斤量"].mean()

    f3_averages = []
    for idx, row in df_target.iterrows():
        f3_list = [r["f3_time"] for r in row.get("past_runs", [])[:4] if not r.get("is_foreign_or_local", False) and "f3_time" in r]
        avg_f3 = sum(f3_list) / len(f3_list) if f3_list else 999.0
        f3_averages.append((idx, avg_f3))

    f3_averages.sort(key=lambda x: x[1])
    f3_ranks = {orig_idx: rank_idx + 1 for rank_idx, (orig_idx, _) in enumerate(f3_averages)}

    phase1_lines = ["#### ■ PHASE 1：全頭「補正前」R_Score計算"]
    phase2_lines = ["#### ■ PHASE 2：全頭「補正後」Adj_R_Score＆最終スコア計算\n"]

    weights = [1.00, 0.90, 0.90, 0.90]
    calculated_horses = []

    for idx, row in df_target.iterrows():
        h_num, h_name = row["馬番"], row["馬名"]
        past_runs = row.get("past_runs", [])[:4]
        
        raw_pos = row.get("位置取り")
        pos_type = str(raw_pos).strip() if raw_pos and not pd.isna(raw_pos) and str(raw_pos).strip() not in ["", "nan"] else estimate_position_type_final_corner(past_runs)

        phase1_lines.append(f"##### **【馬番{h_num}：{h_name}】")
        
        adj_scores, raw_scores_info, jra_valid_count = [], [], 0
        
        for run_i, run in enumerate(past_runs):
            run_label = ["前走", "2走前", "3走前", "4走前"][run_i]
            raw_score, detail, is_jra = calc_raw_r_score(run)
            
            if is_jra:
                jra_valid_count += 1
                adj_val = round(raw_score * weights[run_i], 2)
                adj_scores.append((adj_val, run.get("distance", distance), run_label))
                raw_scores_info.append((run_label, raw_score, weights[run_i], adj_val))
            phase1_lines.append(f"* {run_label}：{detail}")

        phase1_lines.append("")

        N = jra_valid_count if jra_valid_count > 0 else 1
        phase2_lines.append(f"##### **【馬番{h_num}：{h_name}（実出走数 N = {N}）】")

        tot_adj = round(sum(a_val for _, _, _, a_val in raw_scores_info), 2)
        for r_label, r_raw, w, a_val in raw_scores_info:
            phase2_lines.append(f"* {r_label}：{r_raw} × {w:.2f} ＝ Adj_R_Score [{a_val:.2f}]")
        phase2_lines.append(f"Adj_R_Score合計 ＝ {tot_adj:.2f}")

        var_A = round(tot_adj / N, 2)

        distance_scores = [
            calc_raw_r_score(run)[0] * (1.00 if abs(int(run.get("distance", distance)) - distance) <= 200 else (0.85 if abs(int(run.get("distance", distance)) - distance) <= 400 else 0.45))
            for run in past_runs if calc_raw_r_score(run)[2]
        ]
        distance_aptitude = sum(distance_scores) / len(distance_scores) if distance_scores else var_A
        dist_adj = round(max(-5.0, min(5.0, (distance_aptitude - var_A) * 0.20)), 2)

        phase2_lines.append("\n###### 距離適性補正判定プロセス")
        phase2_lines.append(f"* 今回距離＝{distance}m | 距離適性R_Score＝{distance_aptitude:.2f} | 通常R_Score平均＝{var_A:.2f} | Dist_補正＝{dist_adj:+.2f}")

        w_diff = round(row["斤量"] - avg_weight, 2)
        weight_adj = 5 if w_diff <= -2.0 else (3 if w_diff <= -1.0 else (1 if w_diff <= -0.5 else (0 if -0.5 < w_diff < 0.5 else (-1 if w_diff < 1.0 else (-3 if w_diff < 2.0 else -5)))))

        phase2_lines.append("\n###### 斤量補正判定プロセス")
        phase2_lines.append(f"* 平均斤量＝{avg_weight:.2f}kg | 当該馬斤量＝{row['斤量']}kg | 差＝{w_diff:+.2f}kg → Weight_補正＝{weight_adj:+d}")

        j_score, j_rank, j_desc = get_jockey_score_and_rank(row["騎手"])
        oversea_adj = 2 if past_runs and past_runs[0].get("is_foreign_or_local", False) else 0
        
        f3_rank = f3_ranks.get(idx, 99)
        f3_adj = (4 if f3_rank == 1 else (3 if f3_rank == 2 else (2 if f3_rank == 3 else (1 if f3_rank in [4, 5] else 0)))) if pos_type in ["逃", "先"] else (2 if f3_rank == 1 else (1 if f3_rank in [2, 3] else 0))
        cond_adj = 5 if row.get("is_good_condition", False) else (-5 if row.get("is_bad_condition", False) else 0)

        rest_adj = 0
        rest_weeks_str = "対象外/前走データなし"
        if past_runs and past_runs[0].get("date"):
            try:
                prev_date = datetime.strptime(past_runs[0]["date"].replace("/", "."), "%Y.%m.%d")
                current_date = datetime.strptime(race_info.get("date", "").replace("/", "."), "%Y%m%d") if race_info.get("date") else datetime.now()
                weeks = (current_date - prev_date).days // 7
                rest_weeks_str = f"前走から{weeks}週（{rest_adj:+d}点）"
            except Exception:
                rest_weeks_str = "日付解析エラー（0点）"

        OUTER_TRACKS = {"新潟": [1600, 1800, 2000], "京都": [1400, 1600, 1800, 2200, 2400, 3000, 3200], "阪神": [1600, 1800, 2400]}
        is_sashi_favored = (track == "東京") or (track in OUTER_TRACKS and distance in OUTER_TRACKS[track])
        pos_adj = 3 if (is_sashi_favored and pos_type in ["差", "追"]) or (not is_sashi_favored and pos_type in ["逃", "先"]) else 0

        try: frame_num = int(row.get("枠番", 0))
        except (ValueError, TypeError): frame_num = 0

        frame_type = "内枠" if frame_num in [1, 2] else ("外枠" if frame_num in [7, 8] else "中枠")
        is_outer_favored = (track, surface, distance) in [("新潟", "芝", 1000), ("東京", "ダート", 1600), ("中山", "ダート", 1200), ("阪神", "ダート", 1400), ("京都", "ダート", 1400), ("阪神", "芝", 1600), ("中京", "ダート", 1200)]
        is_inner_favored = (track, surface, distance) in [("東京", "芝", 2000), ("中山", "芝", 2000), ("中山", "芝", 1800), ("中山", "芝", 2200), ("阪神", "芝", 1400), ("京都", "芝", 1400), ("小倉", "芝", 1200), ("函館", "芝", 1200), ("福島", "芝", 1200), ("札幌", "芝", 1500)]

        frame_adj = 2 if (is_outer_favored and frame_type == "外枠") or (is_inner_favored and frame_type == "内枠") else 0
        trend_adj = 5 if (trend == "前有利" and pos_type in ["逃", "先"]) or (trend == "後有利" and pos_type in ["差", "追"]) else 0
        trend_desc = f"{trend}（+5点）" if trend_adj else "フラット（加点なし）"

        etc_total = j_score + oversea_adj + f3_adj + cond_adj + pos_adj + frame_adj + rest_adj + trend_adj

        phase2_lines.append("\n####### その他補正判定プロセス")
        phase2_lines.append(f"* 騎手名：{row['騎手']}（{j_desc} → {j_score:+d}点） | Etc_補正合計 ＝ {etc_total:+d}")

        final_ability_score = round(var_A + dist_adj + weight_adj + etc_total, 2)
        phase2_lines.append(f"* 変数A[{var_A:.2f}] ＋ Dist_補正[{dist_adj:+2f}] ＋ Weight_補正[{weight_adj:+d}] ＋ Etc_補正[{etc_total:+d}] ＝ 最終能力スコア[{final_ability_score:.2f}]\n")

        calculated_horses.append({"馬番": h_num, "馬名": h_name, "単勝オッズ": row["単勝オッズ"], "最終能力スコア": final_ability_score, "位置取り": pos_type, "走数": jra_valid_count})

    df_calc = pd.DataFrame(calculated_horses)
    max_ability_score = df_calc["最終能力スコア"].max()
    df_calc["能力スコア"] = (df_calc["最終能力スコア"] / max_ability_score * 100).round(1) if max_ability_score > 0 else 0.0

    min_odds, max_odds_val = df_calc["単勝オッズ"].min(), df_calc["単勝オッズ"].max()
    odds_range = max_odds_val - min_odds

    df_calc["オッズスコア"] = [round(100.0 * (max_odds_val - o) / odds_range, 1) if odds_range > 0 else 100.0 for o in df_calc["単勝オッズ"]]
    df_calc["能力順位"] = df_calc["能力スコア"].rank(ascending=False, method="min").astype(int)
    df_calc["オッズ順位"] = df_calc["オッズスコア"].rank(ascending=False, method="min").astype(int)

    phase5_lines = ["#### ■ PHASE 5：全頭合成値（馬番順リスト）\n"]
    for idx, r in df_calc.iterrows():
        ab_term, odds_term = round(r["能力スコア"] * ability_w, 2), round(r["オッズスコア"] * odds_w, 2)
        syn_val = round(ab_term + odds_term, 2)
        df_calc.loc[idx, ["能力項", "オッズ項", "合成値"]] = [ab_term, odds_term, syn_val]
        phase5_lines.append(f"馬番{r['馬番']} [{r['馬名']}]：能力項[{ab_term:.2f}] ＋ オッズ項[{odds_term:.2f}] ＝ 合成値[{syn_val:.2f}]")

    df_sorted = df_calc.sort_values(by=["合成値", "能力スコア", "オッズ順位", "馬番"], ascending=[False, False, False, True]).reset_index(drop=True)
    df_sorted["合成順位"] = range(1, len(df_sorted) + 1)

    TRACK_STD_DEV = {"東京": 15.0, "京都": 15.0, "阪神": 15.0, "中山": 18.0, "中京": 18.0, "新潟": 20.0, "福島": 20.0, "函館": 20.0, "札幌": 20.0, "小倉": 20.0}
    NUM_SIMS = 10000

    np.random.seed(42)
    sim_scores = df_sorted["合成値"].values + np.random.normal(loc=0.0, scale=TRACK_STD_DEV.get(track, 15.0), size=(NUM_SIMS, len(df_sorted)))
    ranks = np.argsort(np.argsort(-sim_scores, axis=1), axis=1) + 1

    df_sorted["勝率(MC)"] = (np.sum(ranks == 1, axis=0) / NUM_SIMS * 100).round(1)
    df_sorted["複勝率(MC)"] = (np.sum(ranks <= 3, axis=0) / NUM_SIMS * 100).round(1)
    df_sorted["期待値"] = (df_sorted["単勝オッズ"] * (df_sorted["勝率(MC)"] / 100.0)).round(2)

    PATTERN_DESCS = {
        "堅": "オッズが上位に集中しており、堅い決着が予想されるパターンです",
        "並": "標準的なオッズ分布で、堅実から中位人気の決着になりやすいパターンです",
        "小荒": "中位人気馬にもチャンスがあり、少し荒れる可能性のある領域です",
        "中荒": "人気が割れており、中穴層の台頭で中程度荒れる傾向のパターンです",
        "大荒": "オッズが大きく分散し、大波乱が十分に想定されるハイリスク・ハイリターンなパターンです",
        "データ不足": "条件を満たさず、レンジを無理に決めない"
    }

    pattern_desc = PATTERN_DESCS.get(race_pattern, "")

    df_valid = df_sorted[(df_sorted["オッズ順位"] <= 10) | (df_sorted["単勝オッズ"] < 30.0)]
    if len(df_valid) < 5: df_valid = df_sorted.head(10)

    jiku_horse = df_valid.sort_values(by="オッズ順位").iloc[0] if not df_valid.empty else df_sorted.iloc[0]
    df_without_jiku = df_valid[df_valid["馬番"] != jiku_horse["馬番"]]
    aite1_df = df_without_jiku.sort_values(by="オッズ順位").head(2)

    aite2_count = 5 if race_pattern in ["中荒", "大荒"] else 4
    
    exclude_horses = {jiku_horse["馬番"]} | set(aite1_df["馬番"].tolist())
    aite2_df = (df_valid[~df_valid["馬番"].isin(exclude_horses)] if not df_valid[~df_valid["馬番"].isin(exclude_horses)].empty else df_sorted[~df_sorted["馬番"].isin(exclude_horses)]).sort_values(by="合成順位").head(aite2_count)

    all_aite_nums = sorted(pd.concat([aite1_df, aite2_df]).drop_duplicates(subset=["馬番"])["馬番"].tolist())
    fmt_points = len(list(itertools.combinations(all_aite_nums, 2)))

    # 会場・荒れ度に応じた購入目安レンジを取得し、○がついていれば末尾に「★」を付与
    track_ranges = VENUE_ODDS_RANGES.get(track, {"堅": "～20倍", "並": "20～40倍", "小荒": "40～60倍", "中荒": "60～80倍", "大荒": "80倍以上"})
    raw_range_str = track_ranges.get(race_pattern, '20～40倍')
    
    track_purchase_map = VALID_PURCHASE_COMBINATIONS.get(track, {})
    is_marked_circle = track_purchase_map.get(race_pattern, False)
    star_mark = "★" if is_marked_circle else ""
    target_odds_range = f"購入目安 {raw_range_str}{star_mark}"

    phase6_lines = [
        "#### ■ PHASE 6：最終ランキングと買い目\n",
        f"#### 1. レース情報\n[{race_name} / {track}{race_no}R / {surface} / {distance}m]",
        f"\n* **荒れ度分析（スコア判定）**:\n{analysis_table_md}",
        f"\n**【レース判定結果】：{display_race_pattern}** （荒れスコア: **{rough_score}点** / {pattern_desc}）\n"
    ]

    phase6_lines.append("\n#### 2. 最終ランキング\n| 順位 | 馬(オッズ) | 合成値(順位) | オッズ(順位) | 能力(順位) | 勝率 | 複勝率 | 期待値 | 位置 | 走数 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    selected_horse_numbers = {jiku_horse["馬番"]} | set(all_aite_nums)
    for idx, r in df_sorted.iterrows():
        prefix = "**" if r["馬番"] in selected_horse_numbers else ""
        phase6_lines.append(f"| {prefix}{idx + 1}{prefix} | {prefix}{r['馬番']} {r['馬名']}({r['単勝オッズ']}倍){prefix} | {prefix}{r['合成値']:.2f} ({r['合成順位']}位){prefix} | {prefix}{r['オッズスコア']:.1f} ({r['オッズ順位']}位){prefix} | {prefix}{r['能力スコア']:.1f} ({r['能力順位']}位){prefix} | {prefix}{r['勝率(MC)']:.1f}%{prefix} | {prefix}{r['複勝率(MC)']:.1f}%{prefix} | {prefix}{r['期待値']:.2f}{prefix} | {prefix}{r.get('位置取り', '差')}{prefix} | {prefix}{r.get('走数', 0)}{prefix} |")

    aite1_nums = ", ".join([str(h) for h in aite1_df["馬番"]])
    aite1_pcts = ",".join([f"{df_sorted.loc[df_sorted['馬番'] == h, '複勝率(MC)'].values[0]:.1f}%" for h in aite1_df["馬番"]])
    aite1_display_str = f"{aite1_nums}({aite1_pcts})"
    
    aite2_str = ", ".join(f"{h:>2}" for h in aite2_df["馬番"])

    phase6_lines.extend([
        f"\n#### 3. 買い目（判定：【{display_race_pattern}】 {target_odds_range}）\n",
        "**【３連複１頭軸流し（馬番表記）】**  ",
        f"軸  ：{jiku_horse['馬番']}（{jiku_horse['単勝オッズ']}倍）  ",
        f"相手1：{aite1_display_str}  ",
        f"相手2：{aite2_str}  \n",
        "#### 4. 入力用買い目\n",
        f"* 基本購入：{jiku_horse['馬番']} - {','.join(map(str, all_aite_nums))}（{fmt_points}点）"
    ])

    return "\n".join(phase6_lines if is_simple else phase1_lines + phase2_lines + phase5_lines + ["\n#### ■ 3-3. 軸馬・相手馬決定判定プロセス"] + phase6_lines)