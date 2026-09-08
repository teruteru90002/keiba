import json
import re
import os
import unicodedata
import itertools
import pandas as pd
import numpy as np
from datetime import datetime

# ==============================================================================
# 設定ファイル (config_data.json) の読み込み
# ==============================================================================
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

JRA_TRACKS = ["東京", "中山", "阪神", "京都", "中京", "新潟", "福島", "札幌", "函館"]

RACE_GRADE_DICT = {}
for g_level, r_list in GRADE_RACE_MAP.items():
    for r_name in r_list:
        RACE_GRADE_DICT[r_name] = g_level

KANJI_REPLACE_MAP = str.maketrans({
    "戶": "戸", "⺠": "民", "櫻": "桜", "髙": "高", "﨑": "崎",
    "廐": "厩", "眞": "真", "實": "実", "榮": "栄", "國": "国",
    "萬": "万", "廣": "広", "島": "島", "澤": "沢"
})

def normalize_text(text):
    if not text:
        return ""
    text = str(text).translate(KANJI_REPLACE_MAP)
    text = unicodedata.normalize("NFKC", text)
    return text.upper().strip()

def detect_grade(run):
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

    raise ValueError(f"【エラー】レース格付を特定できませんでした。対象レース情報: '{r_name_raw}' (run: {run})")

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

    try:
        grade = detect_grade(run)
    except ValueError:
        grade = None

    is_jpn_race = (grade == "JPN")

    if run.get("is_foreign_or_local", False) and not is_jpn_race:
        return 0.0, "地方・海外競馬のため対象外", False

    LOCAL_TRACKS = ["大井", "川崎", "船橋", "浦和", "門別", "盛岡", "水沢", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀", "韓"]
    is_local_or_foreign = any(lt in run_track or lt in race_title for lt in LOCAL_TRACKS)
    is_valid_track = (any(t in run_track for t in JRA_TRACKS) or any(t in race_title for t in JRA_TRACKS)) and not is_local_or_foreign

    if not is_valid_track and not is_jpn_race:
        return 0.0, f"対象外の競馬場（{race_title}）のため計算対象外", False

    if grade is None:
        grade = detect_grade(run)
        
    g_score = GRADE_SCORES.get(grade)
    if g_score is None:
        raise ValueError(f"【エラー】格付 '{grade}' に対応する GRADE_SCORES の定義が見つかりません。")

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

def run_pipeline(df, race_info, good_horses=None, bad_horses=None, is_simple=False, trend="フラット"):
    if good_horses is None:
        good_horses = []
    if bad_horses is None:
        bad_horses = []

    raw_text = str(race_info.get("raw_header", "")) + str(race_info.get("race_name", "")) + str(race_info.get("track", ""))
    
    track = "東京" 
    for t in JRA_TRACKS:
        if t in raw_text:
            track = t
            break
            
    if "ダート" in raw_text:
        surface = "ダート"
    elif "芝" in raw_text:
        surface = "芝"
    else:
        surface = str(race_info.get("track", ""))
        
    dist_raw = race_info.get("distance", 1800)
    try:
        distance = int(re.sub(r'\D', '', str(dist_raw)))
    except ValueError:
        distance = 1800

    race_name = race_info.get("race_name", "第XX回 レース")
    
    default_param = PARAMS.get("東京", {"Odds_W": 0.30, "Ability_W": 0.70, "Max_Odds": 999})
    param = PARAMS.get(track, default_param)
    odds_w = param["Odds_W"]
    ability_w = param["Ability_W"]

    df_target = df.copy().reset_index(drop=True)
    df_target["is_good_condition"] = df_target["馬番"].isin(good_horses)
    df_target["is_bad_condition"] = df_target["馬番"].isin(bad_horses)

    avg_weight = df_target["斤量"].mean()

    f3_averages = []
    for idx, row in df_target.iterrows():
        f3_list = [r["f3_time"] for r in row.get("past_runs", [])[:4] if not r.get("is_foreign_or_local", False) and "f3_time" in r]
        avg_f3 = sum(f3_list) / len(f3_list) if len(f3_list) > 0 else 999.0
        f3_averages.append((idx, avg_f3))

    f3_averages.sort(key=lambda x: x[1])
    f3_ranks = {}
    for rank_idx, (orig_idx, avg_val) in enumerate(f3_averages):
        f3_ranks[orig_idx] = rank_idx + 1

    phase1_lines = ["#### ■ PHASE 1：全頭「補正前」R_Score計算"]
    phase2_lines = ["#### ■ PHASE 2：全頭「補正後」Adj_R_Score＆最終スコア計算\n"]

    weights = [1.00, 0.90, 0.90, 0.90]
    calculated_horses = []

    for idx, row in df_target.iterrows():
        h_num = row["馬番"]
        h_name = row["馬名"]
        pos_type = row.get("位置取り", "差") 
        past_runs = row.get("past_runs", [])[:4]

        phase1_lines.append(f"##### **【馬番{h_num}：{h_name}】")
        
        adj_scores = []
        raw_scores_info = []
        jra_valid_count = 0
        
        for run_i, run in enumerate(past_runs):
            run_label = ["前走", "2走前", "3走前", "4走前"][run_i]
            raw_score, detail, is_jra = calc_raw_r_score(run)
            
            if is_jra:
                jra_valid_count += 1
                adj_val = round(raw_score * weights[run_i], 2)
                adj_scores.append((adj_val, run.get("distance", distance), run_label))
                phase1_lines.append(f"* {run_label}：{detail}")
                raw_scores_info.append((run_label, raw_score, weights[run_i], adj_val))
            else:
                phase1_lines.append(f"* {run_label}：{detail}")

        phase1_lines.append("")

        N = jra_valid_count if jra_valid_count > 0 else 1
        phase2_lines.append(f"##### **【馬番{h_num}：{h_name}（実出走数 N = {N}）】")

        tot_adj = 0.0
        for r_label, r_raw, w, a_val in raw_scores_info:
            phase2_lines.append(f"* {r_label}：{r_raw} × {w:.2f} ＝ Adj_R_Score [{a_val:.2f}]")
            tot_adj += a_val
        tot_adj = round(tot_adj, 2)

        phase2_lines.append(f"Adj_R_Score合計 ＝ {tot_adj:.2f}")

        var_A = round(tot_adj / N, 2)

        distance_scores = []
        for run_i, run in enumerate(past_runs):
            raw_score, _, is_jra = calc_raw_r_score(run)
            if not is_jra:
                continue

            past_distance = run.get("distance", distance)
            try:
                past_distance = int(past_distance)
            except (ValueError, TypeError):
                past_distance = distance

            distance_diff = abs(past_distance - distance)
            if distance_diff <= 200:
                distance_factor = 1.00
            elif distance_diff <= 400:
                distance_factor = 0.85
            elif distance_diff <= 600:
                distance_factor = 0.45
            else:
                distance_factor = 0.45

            distance_scores.append(raw_score * distance_factor)

        if distance_scores:
            distance_aptitude = sum(distance_scores) / len(distance_scores)
        else:
            distance_aptitude = var_A

        distance_raw_adj = (distance_aptitude - var_A) * 0.20
        dist_adj = round(max(-5.0, min(5.0, distance_raw_adj)), 2)

        phase2_lines.append("\n###### 距離適性補正判定プロセス")
        phase2_lines.append(f"* 今回距離＝{distance}m")
        phase2_lines.append(f"* 距離適性R_Score＝{distance_aptitude:.2f}")
        phase2_lines.append(f"* 通常R_Score平均＝{var_A:.2f}")
        phase2_lines.append(f"* 距離適性差＝{distance_aptitude - var_A:+.2f}")
        phase2_lines.append(f"* Dist_補正＝{dist_adj:+.2f}")

        w_diff = round(row["斤量"] - avg_weight, 2)
        if w_diff <= -2.0: weight_adj = 3
        elif w_diff <= -1.0: weight_adj = 2
        elif w_diff <= -0.5: weight_adj = 1
        elif -0.5 < w_diff < 0.5: weight_adj = 0
        elif w_diff < 1.0: weight_adj = -1
        elif w_diff < 2.0: weight_adj = -2
        else: weight_adj = -3

        phase2_lines.append("\n###### 斤量補正判定プロセス")
        phase2_lines.append(f"* 平均斤量＝{avg_weight:.2f}kg | 当該馬斤量＝{row['斤量']}kg | 差＝{w_diff:+.2f}kg → Weight_補正＝{weight_adj:+d}")

        j_score, j_rank, j_desc = get_jockey_score_and_rank(row["騎手"])
        oversea_adj = 2 if len(past_runs) > 0 and past_runs[0].get("is_overseas", False) else 0
        
        f3_rank = f3_ranks.get(idx, 99)
        f3_adj = 0
        if pos_type in ["逃", "先"]:
            if f3_rank == 1: f3_adj = 4
            elif f3_rank == 2: f3_adj = 3
            elif f3_rank == 3: f3_adj = 2
            elif f3_rank in [4, 5]: f3_adj = 1
        else:
            if f3_rank == 1: f3_adj = 2
            elif f3_rank in [2, 3]: f3_adj = 1

        cond_adj = 5 if row.get("is_good_condition", False) else (-5 if row.get("is_bad_condition", False) else 0)

        rest_adj = 0
        rest_weeks_str = "対象外/前走データなし"
        if len(past_runs) > 0 and past_runs[0].get("date"):
            try:
                prev_date_str = past_runs[0]["date"].replace("/", ".")
                prev_date = datetime.strptime(prev_date_str, "%Y.%m.%d")
                
                race_date_raw = race_info.get("date")
                if race_date_raw:
                    current_date = datetime.strptime(race_date_raw.replace("/", "."), "%Y.%m.%d")
                else:
                    current_date = datetime.now()

                delta_days = (current_date - prev_date).days
                weeks = delta_days // 7
                
                if weeks >= 40: rest_adj = -4
                elif weeks >= 20: rest_adj = -2
                elif 2 <= weeks <= 12: rest_adj = 0
                else: rest_adj = 0
                
                rest_weeks_str = f"前走から{weeks}週（{rest_adj:+d}点）"
            except Exception:
                rest_weeks_str = "日付解析エラー（0点）"

        OUTER_TRACKS = {
            "新潟": [1600, 1800, 2000],
            "京都": [1400, 1600, 1800, 2200, 2400, 3000, 3200],
            "阪神": [1600, 1800, 2400]
        }

        pos_adj = 0
        is_sashi_favored = (track == "東京") or (track in OUTER_TRACKS and distance in OUTER_TRACKS[track])
        is_nige_favored = (track in ["中山", "福島", "小倉", "函館", "札幌"]) or (track == "阪神" and distance not in OUTER_TRACKS["阪神"])

        if is_sashi_favored and pos_type in ["差", "追"]:
            pos_adj = 3
        elif is_nige_favored and pos_type in ["逃", "先"]:
            pos_adj = 3

        try: frame_num = int(row.get("枠番", 0))
        except (ValueError, TypeError): frame_num = 0

        frame_type = "中枠"
        if frame_num in [1, 2]: frame_type = "内枠"
        elif frame_num in [7, 8]: frame_type = "外枠"

        is_outer_favored = (
            (track == "新潟" and "芝" in surface and distance == 1000) or
            (track == "東京" and "ダート" in surface and distance == 1600) or
            (track == "中山" and "ダート" in surface and distance == 1200) or
            (track == "阪神" and "ダート" in surface and distance == 1400) or
            (track == "京都" and "ダート" in surface and distance == 1400) or
            (track == "阪神" and "芝" in surface and distance == 1600) or
            (track == "中京" and "ダート" in surface and distance == 1200)
        )

        is_inner_favored = (
            (track == "東京" and "芝" in surface and distance == 2000) or
            (track == "中山" and "芝" in surface and distance == 2000) or
            (track == "中山" and "芝" in surface and distance == 1800) or
            (track == "中山" and "芝" in surface and distance == 2200) or
            (track == "阪神" and "芝" in surface and distance == 1400) or
            (track == "京都" and "芝" in surface and distance == 1400) or
            (track == "小倉" and "芝" in surface and distance == 1200) or
            (track == "函館" and "芝" in surface and distance == 1200) or
            (track == "福島" and "芝" in surface and distance == 1200) or
            (track == "札幌" and "芝" in surface and distance == 1500)
        )

        frame_adj = 0
        favored_desc = "フラット（バイアスなし）"
        if is_outer_favored:
            favored_desc = "外枠有利コース"
            if frame_type == "外枠": frame_adj = 2
        elif is_inner_favored:
            favored_desc = "内枠有利コース"
            if frame_type == "内枠": frame_adj = 2

        trend_adj = 0
        trend_desc = "フラット（加点なし）"
        if trend == "前有利" and pos_type in ["逃", "先"]:
            trend_adj = 5
            trend_desc = "前有利（+5点）"
        elif trend == "後有利" and pos_type in ["差", "追"]:
            trend_adj = 5
            trend_desc = "後有利（+5点）"

        etc_total = j_score + oversea_adj + f3_adj + cond_adj + pos_adj + frame_adj + rest_adj + trend_adj

        phase2_lines.append("\n####### その他補正判定プロセス")
        phase2_lines.append(f"* 騎手名：{row['騎手']}（{j_desc} → {j_score:+d}点）")
        phase2_lines.append(
            f"* 海外出走：{oversea_adj}点 | 上上がり3F：{f3_adj}点 | 当日補填：{cond_adj}点 | "
            f"脚質補正：{pos_adj}点 | 当日傾向：{trend_adj}点（{trend_desc}） | 休養補正：{rest_weeks_str}"
        )
        phase2_lines.append(f"* 枠順補正：{frame_num}枠（{frame_type}）/ {favored_desc} → {frame_adj}点")
        phase2_lines.append(f"* Etc_補正合計 ＝ {etc_total:+d}")

        final_ability_score = round(var_A + dist_adj + weight_adj + etc_total, 2)
        phase2_lines.append("\n###### 最終計算ステップ")
        phase2_lines.append(f"* 変数A[{var_A:.2f}] ＋ Dist_補正[{dist_adj:+2f}] ＋ Weight_補正[{weight_adj:+d}] ＋ Etc_補正[{etc_total:+d}] ＝ 最終能力スコア[{final_ability_score:.2f}]\n")

        calculated_horses.append({
            "馬番": h_num,
            "馬名": h_name,
            "単勝オッズ": row["単勝オッズ"],
            "最終能力スコア": final_ability_score,
            "位置取り": pos_type,
            "走数": jra_valid_count
        })

    df_calc = pd.DataFrame(calculated_horses)

    max_ability_score = df_calc["最終能力スコア"].max()
    if max_ability_score > 0:
        df_calc["能力スコア"] = (df_calc["最終能力スコア"] / max_ability_score * 100).round(1)
    else:
        df_calc["能力スコア"] = 0.0

    min_odds = df_calc["単勝オッズ"].min()
    max_odds_val = df_calc["単勝オッズ"].max()
    odds_range = max_odds_val - min_odds

    for idx, r in df_calc.iterrows():
        if odds_range > 0:
            norm_odds_score = round(100.0 * (max_odds_val - r["単勝オッズ"]) / odds_range, 1)
        else:
            norm_odds_score = 100.0
        df_calc.loc[idx, "オッズスコア"] = norm_odds_score

    df_calc["能力順位"] = df_calc["能力スコア"].rank(ascending=False, method="min").astype(int)
    df_calc["オッズ順位"] = df_calc["オッズスコア"].rank(ascending=False, method="min").astype(int)

    phase5_lines = ["#### ■ PHASE 5：全頭合成値（馬番順リスト）\n", "【1段階目：正規化スコアベースの掛け算展開】"]
    
    for idx, r in df_calc.iterrows():
        ab_score = r["能力スコア"]
        odds_score = r["オッズスコア"]

        ab_term = round(ab_score * ability_w, 2)
        odds_term = round(odds_score * odds_w, 2)
        syn_val = round(ab_term + odds_term, 2)

        df_calc.loc[idx, "能力項"] = ab_term
        df_calc.loc[idx, "オッズ項"] = odds_term
        df_calc.loc[idx, "合成値"] = syn_val

        phase5_lines.append(f"馬番{r['馬番']} [{r['馬名']}]（単勝オッズ: {r['単勝オッズ']}倍）：")
        phase5_lines.append(f"・能力項 （正規化能力スコア {ab_score:.1f} × {ability_w:.2f}） ＝ [{ab_term:.2f}]")
        phase5_lines.append(f"・オッズ項（正規化オッズスコア {odds_score:.1f} × {odds_w:.2f}） ＝ [{odds_term:.2f}]")

    phase5_lines.append("\n【2段階目：足し算の実行と合成値の確定】")
    for idx, r in df_calc.iterrows():
        phase5_lines.append(f"馬番{r['馬番']} [{r['馬名']}]：{r['能力項']:.2f} ＋ {r['オッズ項']:.2f} ＝ 合成値[{r['合成値']:.2f}]")

    phase5_lines.append("\n全頭の合成値について、(能力項掛け算結果) ＋ (オッズ項掛け算結果) ＝ 合成値 の算術的整合性を確認しました。\n")

    df_sorted = df_calc.sort_values(
        by=["合成値", "能力スコア", "オッズスコア", "馬番"],
        ascending=[False, False, False, True]
    ).reset_index(drop=True)
    df_sorted["合成順位"] = range(1, len(df_sorted) + 1)

    TRACK_STD_DEV = {
        "東京": 15.0, "京都": 15.0, "阪神": 15.0, "中山": 18.0,
        "中京": 18.0, "新潟": 20.0, "福島": 20.0, "函館": 20.0,
        "札幌": 20.0, "小倉": 20.0,
    }
    std_dev = TRACK_STD_DEV.get(track, 15.0)
    NUM_SIMS = 10000

    syn_scores = df_sorted["合成値"].values
    num_horses = len(syn_scores)

    np.random.seed(42)
    noise = np.random.normal(loc=0.0, scale=std_dev, size=(NUM_SIMS, num_horses))
    sim_scores = syn_scores + noise

    ranks = np.argsort(np.argsort(-sim_scores, axis=1), axis=1) + 1

    df_sorted["勝率(MC)"] = (np.sum(ranks == 1, axis=0) / NUM_SIMS * 100).round(1)
    df_sorted["複勝率(MC)"] = (np.sum(ranks <= 3, axis=0) / NUM_SIMS * 100).round(1)

    df_sorted["期待値"] = (df_sorted["単勝オッズ"] * (df_sorted["勝率(MC)"] / 100.0)).round(2)

    top_odds_list = df_sorted.sort_values(by="単勝オッズ")["単勝オッズ"].tolist()
    o1 = top_odds_list[0] if len(top_odds_list) > 0 else 99.0
    o2 = top_odds_list[1] if len(top_odds_list) > 1 else 99.0
    o3 = top_odds_list[2] if len(top_odds_list) > 2 else 99.0

    est_min_odds = (o1 * o2 * o3) * 0.20
    top3_place_rate_sum = df_sorted.sort_values(by="単勝オッズ")["複勝率(MC)"].head(3).sum()

    if (est_min_odds < 12.0 and top3_place_rate_sum >= 150.0) or (o1 <= 2.2 and o2 <= 4.5 and top3_place_rate_sum >= 180.0):
        race_pattern = "堅い"
        pattern_desc = "軸を強く信頼・絞る。上位人気馬の信頼度が高く、3連複30倍未満の本命決着が期待されるレースです。"
    elif est_min_odds < 20.0 and top3_place_rate_sum >= 140.0:
        race_pattern = "やや堅い"
        pattern_desc = "軸信頼度高め。上位人気馬が比較的安定しており、3連複30～50倍程度の決着が想定されるレースです。"
    elif est_min_odds >= 35.0 or (o1 >= 4.0 and top3_place_rate_sum < 125.0):
        race_pattern = "混戦"
        pattern_desc = "穴馬・相手広め。人気が割れており、3連複80倍以上の波乱決着が期待されるレースです。"
    else:
        race_pattern = "やや混戦"
        pattern_desc = "通常より注意。上位人気の信頼度がやや低く、3連複50～80倍程度のやや波乱の決着が想定されるレースです。"

    # --------------------------------------------------------------------------
    # 1. 軸馬決定判定プロセス
    # --------------------------------------------------------------------------
    df_odds_sorted = df_sorted.sort_values(by="オッズ順位", ascending=True).reset_index(drop=True)

    if race_pattern in ["堅い", "やや堅い"]:
        jiku_horse = df_odds_sorted.iloc[0]
        jiku_reason = f"レース判定が「{race_pattern}」のため、オッズ1位を選出"
    else:
        # やや混戦、混戦の場合はオッズ順位上位2頭のうち合成順位上位1頭を選出
        jiku_candidates = df_odds_sorted.head(2)
        jiku_horse = jiku_candidates.sort_values(by=["合成順位", "オッズ順位"]).iloc[0]
        jiku_reason = f"レース判定が「{race_pattern}」のため、オッズ順位上位2頭のうち合成順位上位1頭を選出"

    jiku_log_lines = ["\n#### ■ 3-3. 軸馬決定判定プロセス"]
    jiku_log_lines.append(f"【判定】：{jiku_reason}、馬番{jiku_horse['馬番']}（{jiku_horse['馬名']}）を軸馬として選定")
    jiku_log_lines.append(
        f"→ 馬番[{jiku_horse['馬番']}] {jiku_horse['馬名']}（オッズ: {jiku_horse['単勝オッズ']}倍 / オッズ順位: {jiku_horse['オッズ順位']}位 / 合成順位: {jiku_horse['合成順位']}位）を軸馬として確定。"
    )

    # --------------------------------------------------------------------------
    # 2. 相手馬選定ロジック（共通除外：オッズ30倍以上 または オッズ順位10位以上）
    # --------------------------------------------------------------------------
    # 除外条件判定（オッズ30倍以上 または オッズ順位10位以上（10位以降）を除外）
    valid_aite_df = df_sorted[
        (df_sorted["馬番"] != jiku_horse["馬番"]) &
        (df_sorted["単勝オッズ"] < 30.0) &
        (df_sorted["オッズ順位"] < 10)
    ].copy()

    # --- 相手1選定 ---
    # 軸馬を除きオッズ順位上位3頭のうち合成順位上位2頭を選出
    aite1_candidates = valid_aite_df.sort_values(by=["オッズ順位", "馬番"]).head(3)
    aite1_df = aite1_candidates.sort_values(by=["合成順位", "オッズ順位", "馬番"]).head(2)
    aite1_horses = aite1_df["馬番"].tolist()

    # --- 相手2選定 ---
    # 軸馬、相手1を除外した候補リスト
    aite2_candidates = valid_aite_df[~valid_aite_df["馬番"].isin(aite1_horses)].copy()

    # パターン別の複勝率(MC)基準および選出頭数の設定
    if race_pattern == "堅い":
        mc_threshold = 20.0
        target_count = 4
    elif race_pattern == "やや堅い":
        mc_threshold = 18.0
        target_count = 5
    elif race_pattern == "やや混戦":
        mc_threshold = 15.0
        target_count = 6
    elif race_pattern == "混戦":
        mc_threshold = 15.0
        target_count = 7
    else:
        mc_threshold = 0.0
        target_count = 0

    # 複勝率条件でフィルタリング後、能力順位上位から所定頭数を選出
    aite2_filtered = aite2_candidates[aite2_candidates["複勝率(MC)"] >= mc_threshold]
    aite2_df = aite2_filtered.sort_values(by=["能力順位", "合成順位", "馬番"]).head(target_count)
    aite2_horses = aite2_df["馬番"].tolist()

    # 相手1 ＋ 相手2 の結合（重複除外）
    all_aite_set = set(aite1_horses + aite2_horses)

    # 買い目（表示用）：合成順位昇順でソート
    aite_df_display = df_sorted[df_sorted["馬番"].isin(all_aite_set)].sort_values(by=["合成順位", "馬番"])
    display_aite_horses = aite_df_display["馬番"].tolist()

    # 入力用買い目用：馬番順（昇順）でソート
    input_aite_horses = sorted(list(all_aite_set))

    # --------------------------------------------------------------------------
    # 3. 買い目点数の算出（3連複1頭軸流し）
    # --------------------------------------------------------------------------
    jiku_val = jiku_horse["馬番"]
    sanrenpuku_combos = list(itertools.combinations(input_aite_horses, 2))
    fmt_points = len(sanrenpuku_combos)

    ODDS_RANGE_MAP = {
        "堅い": "購入目安 10～50倍 見送り",
        "やや堅い": "購入目安 20～80倍 5点",
        "やや混戦": "購入目安 30～120倍 7点",
        "混戦": "購入目安 50～150倍 見送り"
    }
    target_odds_range = ODDS_RANGE_MAP.get(race_pattern, "")

    phase6_lines = [
        "#### ■ PHASE 6：最終ランキングと買い目\n",
        f"#### 1. レース情報\n[{race_name} / {track} / {distance}m]\n",
        f"**【レース判定結果】：{race_pattern}** （{pattern_desc}）\n",
        "#### 2. 最終ランキング\n",
        "| 順位 | 馬(オッズ) | 合成値(順位) | オッズ(順位) | 能力(順位) | 勝率 | 複勝率 | 期待値 | 位置 | 走数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    ]

    selected_horse_numbers = set([jiku_horse["馬番"]] + display_aite_horses)

    for idx, r in df_sorted.iterrows():
        rank_num = idx + 1
        pos = r.get("位置取り", "先") 
        valid_runs = r.get("走数", 0)
        win_mc = f"{r['勝率(MC)']:.1f}%"
        place_mc = f"{r['複勝率(MC)']:.1f}%"
        ev_val = f"{r['期待値']:.2f}"
        
        if r["馬番"] in selected_horse_numbers:
            phase6_lines.append(
                f"| **{rank_num}** | **{r['馬番']} {r['馬name'] if '馬name' in r else r['馬名']}({r['単勝オッズ']}倍)** | **{r['合成値']:.2f} ({r['合成順位']}位)** | **{r['オッズスコア']:.1f} ({r['オッズ順位']}位)** | **{r['能力スコア']:.1f} ({r['能力順位']}位)** | **{win_mc}** | **{place_mc}** | **{ev_val}** | **{pos}** | **{valid_runs}** |"
            )
        else:
            phase6_lines.append(
                f"| {rank_num} | {r['馬番']} {r['馬名']}({r['単勝オッズ']}倍) | {r['合成値']:.2f} ({r['合成順位']}位) | {r['オッズスコア']:.1f} ({r['オッズ順位']}位) | {r['能力スコア']:.1f} ({r['能力順位']}位) | {win_mc} | {place_mc} | {ev_val} | {pos} | {valid_runs} |"
            )

    phase6_lines.append(f"\n#### 3. 買い目（判定：【{race_pattern}】 {target_odds_range}）\n")

    phase6_lines.append("**【３連複１頭軸流し】**")
    phase6_lines.append(f"* 軸  ：{jiku_horse['馬番']}")
    phase6_lines.append(f"* 相手：{', '.join(map(str, display_aite_horses))} （{len(display_aite_horses)}頭 / 合成順位昇順）\n")

    # --------------------------------------------------------------------------
    # 4. 入力用買い目の生成（馬番順）
    # --------------------------------------------------------------------------
    phase6_lines.append("#### 4. 入力用買い目\n")
    
    aite_str = ",".join(map(str, input_aite_horses))
    phase6_lines.append(f"* ３連複１頭軸流し：{jiku_val} - {aite_str}（{fmt_points}点）")

    full_report = []
    if is_simple:
        full_report.extend(phase6_lines)
    else:
        full_report.extend(phase1_lines)
        full_report.extend(phase2_lines)
        full_report.extend(phase5_lines)
        full_report.extend(jiku_log_lines)
        full_report.extend(phase6_lines)

    return "\n".join(full_report)