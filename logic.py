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

# Playwrightブラウザのセットアップ
os.system("playwright install chromium")

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

def calculate_payout_probabilities(o1, o10, o20, o30, o50):
    if o1 is None:
        return None

    v_o1 = float(o1)
    v_o10 = float(o10) if o10 is not None else v_o1 * 3.2
    v_o20 = float(o20) if o20 is not None else v_o10 * 1.8
    v_o30 = float(o30) if o30 is not None else v_o20 * 1.5
    v_o50 = float(o50) if o50 is not None else v_o30 * 1.4

    est_center = v_o1 * 1.6 + (v_o10 / 2.0)
    zones = [10.0, 25.0, 35.0, 45.0, 55.0, 70.0, 100.0]
    
    # 推定配当確率が全体的に5%ほど上目（高配当寄り）に出るよう、インデックスに応じて高配当側のウェイトを底上げ
    scores = [max(1.0, (50.0 - abs(z - est_center) * 0.7) * (1.0 + i * 0.015)) for i, z in enumerate(zones)]
    total_score = sum(scores)

    if total_score > 0:
        p = [round(s / total_score * 100) for s in scores]
        p[0] += 100 - sum(p)
    else:
        p = [14, 14, 14, 14, 14, 15, 15]

    return {
        "～20倍": max(0, p[0]),
        "20～30倍": max(0, p[1]),
        "30～40倍": max(0, p[2]),
        "40～50倍": max(0, p[3]),
        "50～60倍": max(0, p[4]),
        "60～80倍": max(0, p[5]),
        "80倍以上": max(0, p[6])
    }

PLACE_CODE_MAP = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"
}

async def get_race_list(page, date_str, is_simple=False):
    log_debug(f"[DEBUG] get_race_list開始: 日付={date_str}", is_simple)
    races = []
    url_db = f"https://db.netkeiba.com/race/list/{date_str}/"
    try:
        await page.goto(url_db, wait_until="domcontentloaded", timeout=15000)
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"/race/\d{12}/")):
            match = re.search(r"/race/(\d{12})/", a.get("href", ""))
            if match:
                race_id = match.group(1)
                p_code = race_id[4:6]
                if p_code in PLACE_CODE_MAP:
                    race_no = int(race_id[10:12])
                    if 1 <= race_no <= 12:
                        races.append({"race_id": race_id, "場所": PLACE_CODE_MAP[p_code], "R": race_no})
    except Exception as e:
        log_debug(f"[WARN] DB取得エラー: {e}", is_simple)

    if not races:
        url_race = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_str}"
        try:
            await page.goto(url_race, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_selector("a[href*='race_id=']", timeout=5000)
            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"race_id=\d{12}")):
                match = re.search(r"race_id=(\d{12})", a.get("href", ""))
                if match:
                    race_id = match.group(1)
                    p_code = race_id[4:6]
                    if p_code in PLACE_CODE_MAP:
                        race_no = int(race_id[10:12])
                        if 1 <= race_no <= 12:
                            races.append({"race_id": race_id, "場所": PLACE_CODE_MAP[p_code], "R": race_no})
        except Exception as e:
            log_debug(f"[WARN] 当日ページ取得エラー: {e}", is_simple)

    unique_races = {f"{r['場所']}_{r['R']}": r for r in races}
    return sorted(list(unique_races.values()), key=lambda x: (x["場所"], x["R"]))

async def fetch_race_odds(place_name, race_no, date_str=None, is_simple=False):
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        races = await get_race_list(page, date_str, is_simple=is_simple)
        target_race_id = next((r["race_id"] for r in races if r["場所"] == place_name and r["R"] == int(race_no)), None)
                
        if not target_race_id:
            await browser.close()
            return None

        odds_list = []
        urls = [
            f"https://race.netkeiba.com/odds/index.html?type=b7&race_id={target_race_id}&housiki=c99",
            f"https://race.netkeiba.com/odds/index.html?type=b7&race_id={target_race_id}",
            f"https://db.netkeiba.com/race/odds/index.html?type=b7&race_id={target_race_id}"
        ]

        for url in urls:
            if odds_list: break
            for retry in range(2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(1.2)
                    soup = BeautifulSoup(await page.content(), "html.parser")
                    
                    elements = soup.select("span[id^='odds-']") or soup.select("td.Odds_Value") or soup.select("td.Odds")
                    for el in elements:
                        try:
                            val = float(el.get_text(strip=True))
                            if val > 0: odds_list.append(val)
                        except ValueError: 
                            continue
                    if odds_list: break
                except Exception:
                    await asyncio.sleep(1.0)
            if odds_list: break

        await browser.close()

        odds = sorted(odds_list) if odds_list else []
        if len(odds) < 30:
            return None

        return {
            "o1": odds[0],
            "o10": odds[9] if len(odds) >= 10 else None,
            "o20": odds[19] if len(odds) >= 20 else None,
            "o30": odds[29] if len(odds) >= 30 else None,
            "o50": odds[49] if len(odds) >= 50 else None,
            "o100": odds[99] if len(odds) >= 100 else (odds[-1] if len(odds) > 0 else None)
        }

def get_race_odds(place_name, race_no, date_str=None, is_simple=False):
    try:
        return asyncio.run(fetch_race_odds(place_name, race_no, date_str, is_simple=is_simple))
    except Exception as e:
        log_debug(f"[ERROR] get_race_odds内部例外: {e}", is_simple)
        return None

def run_pipeline(df, race_info, good_horses=None, bad_horses=None, is_simple=False, trend="フラット"):
    if good_horses is None: good_horses = []
    if bad_horses is None: bad_horses = []

    raw_text = str(race_info.get("raw_header", "")) + str(race_info.get("race_name", "")) + str(race_info.get("track", ""))
    
    track = next((t for t in JRA_TRACKS if t in raw_text), race_info.get("track_name", "東京"))
    race_no = race_info.get("race_no") or (int(re.search(r'(\d{1,2})\s*R', raw_text, re.IGNORECASE).group(1)) if re.search(r'(\d{1,2})\s*R', raw_text, re.IGNORECASE) else 11)

    race_date_raw = race_info.get("date", "")
    date_str = re.sub(r'\D', '', str(race_date_raw)) if race_date_raw else None

    odds_info = get_race_odds(track, race_no, date_str, is_simple=is_simple)

    if odds_info and odds_info.get("o1") is not None:
        o1, o10, o20, o30, o50, o100 = odds_info["o1"], odds_info["o10"], odds_info["o20"], odds_info["o30"], odds_info["o50"], odds_info["o100"]
        o1_str, o10_str, o20_str, o30_str, o50_str, o100_str = f"{o1:.1f}倍", (f"{o10:.1f}倍" if o10 else "-"), (f"{o20:.1f}倍" if o20 else "-"), (f"{o30:.1f}倍" if o30 else "-"), (f"{o50:.1f}倍" if o50 else "-"), (f"{o100:.1f}倍" if o100 else "-")
        payout_probs = calculate_payout_probabilities(o1, o10, o20, o30, o50)
    else:
        o1_str = o10_str = o20_str = o30_str = o50_str = o100_str = "未取得"
        payout_probs = None

    odds_table_md = (
        "\n| 3連複1位 | 3連複10位 | 3連複20位 | 3連複30位 | 3連複50位 | 3連複100位 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| {o1_str} | {o10_str} | {o20_str} | {o30_str} | {o50_str} | {o100_str} |"
    )

    if payout_probs:
        prob_table_md = (
            "\n| ～20倍 | 20～30倍 | 30～40倍 | 40～50倍 | 50～60倍 | 60～80倍 | 80倍以上 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            f"| **約{payout_probs['～20倍']}%** | **約{payout_probs['20～30倍']}%** | **約{payout_probs['30～40倍']}%** | **約{payout_probs['40～50倍']}%** | **約{payout_probs['50～60倍']}%** | **約{payout_probs['60～80倍']}%** | **約{payout_probs['80倍以上']}%** |"
        )
    else:
        prob_table_md = "\n* **推定配当確率**: データ不足のため算出不可"

    surface = "ダート" if "ダート" in raw_text else ("芝" if "芝" in raw_text else str(race_info.get("track", "")))
    
    try:
        distance = int(re.sub(r'\D', '', str(race_info.get("distance", 1800))))
    except ValueError:
        distance = 1800

    race_name = race_info.get("race_name", "第XX回 レース")
    
    param = PARAMS.get(track, PARAMS.get("東京", {"Odds_W": 0.70, "Ability_W": 0.30}))
    odds_w, ability_w = param.get("Odds_W", 0.70), param.get("Ability_W", 0.30)

    df_target = df.copy().reset_index(drop=True)
    df_target["is_good_condition"] = df_target["馬番"].isin(good_horses)
    df_target["is_bad_condition"] = df_target["馬番"].isin(bad_horses)

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

    df_sorted = df_calc.sort_values(by=["合成値", "能力スコア", "オッズスコア", "馬番"], ascending=[False, False, False, True]).reset_index(drop=True)
    df_sorted["合成順位"] = range(1, len(df_sorted) + 1)

    TRACK_STD_DEV = {"東京": 15.0, "京都": 15.0, "阪神": 15.0, "中山": 18.0, "中京": 18.0, "新潟": 20.0, "福島": 20.0, "函館": 20.0, "札幌": 20.0, "小倉": 20.0}
    NUM_SIMS = 10000

    np.random.seed(42)
    sim_scores = df_sorted["合成値"].values + np.random.normal(loc=0.0, scale=TRACK_STD_DEV.get(track, 15.0), size=(NUM_SIMS, len(df_sorted)))
    ranks = np.argsort(np.argsort(-sim_scores, axis=1), axis=1) + 1

    df_sorted["勝率(MC)"] = (np.sum(ranks == 1, axis=0) / NUM_SIMS * 100).round(1)
    df_sorted["複勝率(MC)"] = (np.sum(ranks <= 3, axis=0) / NUM_SIMS * 100).round(1)
    df_sorted["期待値"] = (df_sorted["単勝オッズ"] * (df_sorted["勝率(MC)"] / 100.0)).round(2)

    top3_in_place_counts = np.sum(ranks[:, df_sorted.sort_values(by="単勝オッズ").index[:3]] <= 3, axis=1)
    prob_top3_3, prob_top3_2, prob_top3_1, prob_top3_0 = [(np.sum(top3_in_place_counts == i) / NUM_SIMS) * 100 for i in [3, 2, 1, 0]]
    prob_top3_2_or_more = prob_top3_2 + prob_top3_3

    PATTERNS_ORDER = ["堅1", "堅2", "並1", "並2", "荒1", "荒2", "大荒"]
    PATTERN_DESCS = {
        "堅1": "配当目安 ～20倍の確率が高い", "堅2": "配当目安 20～30倍の確率が高い",
        "並1": "配当目安 30～40倍の確率が高い", "並2": "配当目安 40～50倍の確率が高い",
        "荒1": "配当目安 50～60倍の確率が高い", "荒2": "配当目安 60～80倍の確率が高い",
        "大荒": "配当目安 80倍以上の確率が高い", "判定保留": "条件を満たさず、レンジを無理に決めない"
    }

    if payout_probs:
        p_u20, p_20, p_30, p_40, p_50, p_60, p_80 = payout_probs.values()
        cum_30, cum_40, cum_50, cum_60, cum_80 = p_u20 + p_20, p_u20 + p_20 + p_30, p_u20 + p_20 + p_30 + p_40, p_u20 + p_20 + p_30 + p_40 + p_50, sum(list(payout_probs.values())[:6]) + p_80

        race_pattern = next((pat for pat, cond in [
            ("堅1", p_u20 >= 20), ("堅2", p_20 >= 25), ("並1", p_30 >= 25), ("並2", p_40 >= 25),
            ("荒1", p_50 >= 25), ("荒2", p_60 >= 30), ("大荒", p_80 >= 30),
            ("堅2", cum_30 >= 35), ("並1", cum_40 >= 50), ("並2", cum_50 >= 60),
            ("荒1", cum_60 >= 70), ("荒2", cum_80 >= 85)
        ] if cond), "大荒")

        if prob_top3_2_or_more >= 60.0 and race_pattern in PATTERNS_ORDER and PATTERNS_ORDER.index(race_pattern) > 0:
            new_idx = max(0, PATTERNS_ORDER.index(race_pattern) - 1)
            race_pattern = f"{PATTERNS_ORDER[new_idx]} ※"
    else:
        race_pattern = "判定保留"

    pattern_desc = PATTERN_DESCS.get(race_pattern.split()[0], "")

    df_valid = df_sorted[(df_sorted["オッズ順位"] < 10) & (df_sorted["単勝オッズ"] < 30.0)]
    if len(df_valid) < 5: df_valid = df_sorted.head(10)

    jiku_horse = df_valid.sort_values(by="オッズ順位").iloc[0] if not df_valid.empty else df_sorted.iloc[0]
    df_without_jiku = df_valid[df_valid["馬番"] != jiku_horse["馬番"]]
    aite1_df = df_without_jiku.sort_values(by="オッズ順位").head(2)

    clean_race_pattern = race_pattern.split()[0]
    aite2_count = 5 if clean_race_pattern in ["荒2", "大荒"] else 4
    
    exclude_horses = {jiku_horse["馬番"]} | set(aite1_df["馬番"].tolist())
    aite2_df = (df_valid[~df_valid["馬番"].isin(exclude_horses)] if not df_valid[~df_valid["馬番"].isin(exclude_horses)].empty else df_sorted[~df_sorted["馬番"].isin(exclude_horses)]).sort_values(by="合成順位").head(aite2_count)

    all_aite_nums = sorted(pd.concat([aite1_df, aite2_df]).drop_duplicates(subset=["馬番"])["馬番"].tolist())
    fmt_points = len(list(itertools.combinations(all_aite_nums, 2)))

    ODDS_RANGE_MAP = {"堅1": "購入目安 ～20倍", "堅2": "購入目安 15～35倍", "並1": "購入目安 25～45倍", "並2": "購入目安 35～55倍", "荒1": "購入目安 45～65倍", "荒2": "購入目安 55～85倍", "大荒": "購入目安 75倍以上"}
    target_odds_range = ODDS_RANGE_MAP.get(clean_race_pattern, "")

    phase6_lines = [
        "#### ■ PHASE 6：最終ランキングと買い目\n",
        f"#### 1. レース情報\n[{race_name} / {track}{race_no}R / {surface} / {distance}m]",
        f"* **取得3連複オッズ**:\n{odds_table_md}",
        f"\n* **推定配当確率**:\n{prob_table_md}",
        f"\n**【レース判定結果】：{race_pattern}** （{pattern_desc}）\n",
        f"  * 単勝1〜3番人気の複勝入着確率: 2頭以上入る合計確率: **{prob_top3_2_or_more:.1f}%**"
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
        f"\n#### 3. 買い目（判定：【{race_pattern}】 {target_odds_range}）\n",
        "**【３連複１頭軸流し（馬番表記）】**  ",
        f"軸  ：{jiku_horse['馬番']}（{jiku_horse['単勝オッズ']}倍）  ",
        f"相手1：{aite1_display_str}  ",
        f"相手2：{aite2_str}  \n",
        "#### 4. 入力用買い目\n",
        f"* 基本購入：{jiku_horse['馬番']} - {','.join(map(str, all_aite_nums))}（{fmt_points}点）"
    ])

    return "\n".join(phase6_lines if is_simple else phase1_lines + phase2_lines + phase5_lines + ["\n#### ■ 3-3. 軸馬・相手馬決定判定プロセス"] + phase6_lines)