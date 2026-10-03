import json
import re
import os
import unicodedata
import itertools
import asyncio
import sys
import pandas as pd
import numpy as np
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import streamlit as st  # ← 画面表示用にStreamlitをインポート

# Streamlit Cloud環境などでPlaywrightを動かすためのブラウザインストール処理
os.system("playwright install chromium")

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
    """簡易表示(is_simple=True)のときはデバッグログを出力しない"""
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

# ==============================================================================
# 位置取り（脚質）自動判定ロジック（最終コーナーベース）
# ==============================================================================
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

    if avg_ratio <= 0.15:
        return "逃"
    elif avg_ratio <= 0.40:
        return "先"
    elif avg_ratio <= 0.75:
        return "差"
    else:
        return "追"

# ==============================================================================
# 配当帯確率計算ロジック（5区分化：30倍以下、30～50倍、50～80倍、80～120倍、120倍以上）
# ==============================================================================
def calculate_payout_probabilities(o1, o10, o20, o30, o50):
    """
    3連複各順位のオッズ値から、配当帯（30倍以下、30～50倍、50～80倍、80～120倍、120倍以上）の推定確率を算出する
    """
    if o1 is None:
        return None

    if o1 <= 8.0 and (o20 is None or o20 <= 50.0):
        p_under_30 = max(10, min(85, int(90 - (o1 * 4) - (o10 * 0.5 if o10 else 10))))
        rem = 100 - p_under_30
        p_30_50 = round(rem * 0.45)
        p_50_80 = round(rem * 0.30)
        p_80_120 = round(rem * 0.15)
        p_over_120 = rem - p_30_50 - p_50_80 - p_80_120
    elif o1 >= 15.0 or (o10 is None or o10 >= 50.0):
        p_under_30 = max(2, min(15, int(25 - o1)))
        p_30_50 = max(5, min(20, int(30 - (o1 * 0.6))))
        p_50_80 = max(10, min(30, int(35 - (o10 * 0.2 if o10 else 10))))
        p_80_120 = max(15, min(35, int(40 - (o20 * 0.1 if o20 else 10))))
        p_over_120 = max(20, 100 - p_under_30 - p_30_50 - p_50_80 - p_80_120)
    else:
        p_under_30 = max(5, min(60, int(65 - (o1 * 2.5) - (o10 * 0.3 if o10 else 5))))
        rem = 100 - p_under_30
        p_30_50 = round(rem * 0.35)
        p_50_80 = round(rem * 0.30)
        p_80_120 = round(rem * 0.20)
        p_over_120 = rem - p_30_50 - p_50_80 - p_80_120

    total = p_under_30 + p_30_50 + p_50_80 + p_80_120 + p_over_120
    if total > 0:
        p_under_30 = round(p_under_30 / total * 100)
        p_30_50 = round(p_30_50 / total * 100)
        p_50_80 = round(p_50_80 / total * 100)
        p_80_120 = round(p_80_120 / total * 100)
        p_over_120 = 100 - (p_under_30 + p_30_50 + p_50_80 + p_80_120)

    return {
        "30倍以下": p_under_30,
        "30～50倍": p_30_50,
        "50～80倍": p_50_80,
        "80～120倍": p_80_120,
        "120倍以上": p_over_120
    }

# ==============================================================================
# 3連複オッズ取得ロジック
# ==============================================================================
PLACE_CODE_MAP = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"
}

async def get_race_list(page, date_str, is_simple=False):
    log_debug(f"[DEBUG] get_race_list開始: 日付={date_str}", is_simple)
    races = []
    url_db = f"https://db.netkeiba.com/race/list/{date_str}/"
    try:
        log_debug(f"[DEBUG] DBアクセスURL: {url_db}", is_simple)
        await page.goto(url_db, wait_until="domcontentloaded", timeout=15000)
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"/race/\d{12}/")):
            href = a.get("href", "")
            match = re.search(r"/race/(\d{12})/", href)
            if match:
                race_id = match.group(1)
                p_code = race_id[4:6]
                if p_code in PLACE_CODE_MAP:
                    race_no = int(race_id[10:12])
                    if 1 <= race_no <= 12:
                        races.append({"race_id": race_id, "場所": PLACE_CODE_MAP[p_code], "R": race_no})
        log_debug(f"[DEBUG] DBページから取得したレース数: {len(races)}", is_simple)
    except Exception as e:
        log_debug(f"[WARN] DB取得エラー: {e}", is_simple)

    if not races:
        url_race = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_str}"
        try:
            log_debug(f"[DEBUG] 当日アクセスURL: {url_race}", is_simple)
            await page.goto(url_race, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_selector("a[href*='race_id=']", timeout=5000)
            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"race_id=\d{12}")):
                href = a.get("href", "")
                match = re.search(r"race_id=(\d{12})", href)
                if match:
                    race_id = match.group(1)
                    p_code = race_id[4:6]
                    if p_code in PLACE_CODE_MAP:
                        race_no = int(race_id[10:12])
                        if 1 <= race_no <= 12:
                            races.append({"race_id": race_id, "場所": PLACE_CODE_MAP[p_code], "R": race_no})
            log_debug(f"[DEBUG] 当日ページから取得したレース数: {len(races)}", is_simple)
        except Exception as e:
            log_debug(f"[WARN] 当日ページ取得エラー: {e}", is_simple)

    unique_races = {}
    for r in races:
        key = f"{r['場所']}_{r['R']}"
        if key not in unique_races:
            unique_races[key] = r
            
    log_debug(f"[DEBUG] get_race_list完了: 重複排除後のレース数={len(unique_races)}", is_simple)
    return sorted(list(unique_races.values()), key=lambda x: (x["場所"], x["R"]))

async def fetch_race_odds(place_name, race_no, date_str=None, is_simple=False):
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
        
    log_debug(f"[DEBUG] fetch_race_odds開始: 場所={place_name}, レース={race_no}, 日付={date_str}", is_simple)

    async with async_playwright() as p:
        log_debug(f"[DEBUG] Playwrightブラウザ起動中...", is_simple)
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        races = await get_race_list(page, date_str, is_simple=is_simple)
        
        target_race_id = None
        for r in races:
            if r["場所"] == place_name and r["R"] == int(race_no):
                target_race_id = r["race_id"]
                break
                
        log_debug(f"[DEBUG] ターゲットレースID: {target_race_id}", is_simple)
                
        if not target_race_id:
            log_debug(f"[DEBUG] レースIDが特定できませんでした（{place_name} {race_no}R が一覧に見つからない）", is_simple)
            await browser.close()
            return None

        odds_list = []
        urls = [
            f"https://race.netkeiba.com/odds/index.html?type=b7&race_id={target_race_id}&housiki=c99",
            f"https://race.netkeiba.com/odds/index.html?type=b7&race_id={target_race_id}",
            f"https://db.netkeiba.com/race/odds/index.html?type=b7&race_id={target_race_id}"
        ]

        for url in urls:
            if len(odds_list) > 0: 
                break
            for retry in range(2):
                try:
                    log_debug(f"[DEBUG] オッズURLアクセス: {url} (retry: {retry})", is_simple)
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(1.2)
                    
                    html_content = await page.content()
                    log_debug(f"[DEBUG] HTML取得成功 (文字数: {len(html_content)})", is_simple)
                    
                    soup = BeautifulSoup(html_content, "html.parser")
                    
                    # 親要素と子要素の重複取得を防ぐため、一番確実な要素に絞って取得する
                    elements = soup.select("span[id^='odds-']")
                    if not elements:
                        elements = soup.select("td.Odds_Value")
                    if not elements:
                        elements = soup.select("td.Odds")
                        
                    log_debug(f"[DEBUG] 取得できたオッズ要素数(DOM): {len(elements)}", is_simple)
                    
                    # 全オッズを取得（途中breakしないことで全組み合わせを網羅し、ソート後の順位を正確にする）
                    for el in elements:
                        try:
                            val = float(el.get_text(strip=True))
                            if val > 0: odds_list.append(val)
                        except ValueError: 
                            continue
                            
                    log_debug(f"[DEBUG] 現在の取得オッズ数(変換成功数): {len(odds_list)}", is_simple)
                    
                    if len(odds_list) > 0: 
                        break
                except Exception as e:
                    log_debug(f"[DEBUG] URLアクセスエラー: {e}", is_simple)
                    await asyncio.sleep(1.0)
            
            # このURLで1件でも取得できたら、次のフォールバックURLには行かない
            if len(odds_list) > 0:
                break

        await browser.close()

        # 重複削除(set)を行わず、純粋に昇順ソートして本来の人気順位を確保する
        odds = sorted(odds_list) if odds_list else []
        log_debug(f"[DEBUG] 最終的に取得したオッズ数(ソート済み): {len(odds)}", is_simple)
        
        if len(odds) < 30:
            log_debug("[DEBUG] オッズデータが30件未満のため、取得失敗と判定", is_simple)
            return None

        o1 = odds[0]
        o10 = odds[9] if len(odds) >= 10 else None
        o20 = odds[19] if len(odds) >= 20 else None
        o30 = odds[29] if len(odds) >= 30 else None
        o50 = odds[49] if len(odds) >= 50 else None

        odds_info = {"o1": o1, "o10": o10, "o20": o20, "o30": o30, "o50": o50}
        log_debug(f"[DEBUG] オッズ判定情報: o1={o1}, o10={o10}, o20={o20}, o30={o30}, o50={o50}", is_simple)

        return odds_info

def get_race_odds(place_name, race_no, date_str=None, is_simple=False):
    try:
        return asyncio.run(fetch_race_odds(place_name, race_no, date_str, is_simple=is_simple))
    except Exception as e:
        log_debug(f"[ERROR] get_race_odds内部例外: {e}", is_simple)
        return None

# ==============================================================================
# メインパイプライン
# ==============================================================================
def run_pipeline(df, race_info, good_horses=None, bad_horses=None, is_simple=False, trend="フラット"):
    if good_horses is None:
        good_horses = []
    if bad_horses is None:
        bad_horses = []

    raw_text = str(race_info.get("raw_header", "")) + str(race_info.get("race_name", "")) + str(race_info.get("track", ""))
    
    track = race_info.get("track_name", "東京")
    for t in JRA_TRACKS:
        if t in raw_text:
            track = t
            break

    race_no = race_info.get("race_no")
    if not race_no:
        match_r = re.search(r'(\d{1,2})\s*R', raw_text, re.IGNORECASE)
        race_no = int(match_r.group(1)) if match_r else 11

    race_date_raw = race_info.get("date", "")
    date_str = re.sub(r'\D', '', str(race_date_raw)) if race_date_raw else None

    log_debug(f"[DEBUG] run_pipeline -> get_race_odds呼び出し: 競馬場={track}, レース={race_no}, 日付={date_str}", is_simple)
    odds_info = get_race_odds(track, race_no, date_str, is_simple=is_simple)

    if odds_info and odds_info.get("o1") is not None:
        o1 = odds_info.get("o1")
        o10 = odds_info.get("o10")
        o20 = odds_info.get("o20")
        o30 = odds_info.get("o30")
        o50 = odds_info.get("o50")

        o1_str = f"{o1:.1f}倍"
        o10_str = f"{o10:.1f}倍" if o10 is not None else "-"
        o20_str = f"{o20:.1f}倍" if o20 is not None else "-"
        o30_str = f"{o30:.1f}倍" if o30 is not None else "-"
        o50_str = f"{o50:.1f}倍" if o50 is not None else "-"

        # 推定確率の計算（5区分）
        payout_probs = calculate_payout_probabilities(o1, o10, o20, o30, o50)
    else:
        o1 = o10 = o20 = o30 = o50 = None
        o1_str = "未取得"
        o10_str = "未取得"
        o20_str = "未取得"
        o30_str = "未取得"
        o50_str = "未取得"
        payout_probs = None

    odds_table_md = (
        "\n| 3連複1位 | 3連複10位 | 3連複20位 | 3連複30位 | 3連複50位 |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| {o1_str} | {o10_str} | {o20_str} | {o30_str} | {o50_str} |"
    )

    if payout_probs:
        prob_table_md = (
            "\n| 30倍以下 | 30～60倍 | 50～80倍 | 80～130倍 | 120倍以上 |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| **約{payout_probs['30倍以下']}%** | **約{payout_probs['30～50倍']}%** | **約{payout_probs['50～80倍']}%** | **約{payout_probs['80～120倍']}%** | **約{payout_probs['120倍以上']}%** |"
        )
    else:
        prob_table_md = "\n* **推定配当確率**: データ不足のため算出不可"

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
    
    default_param = PARAMS.get("東京", {"Odds_W": 0.70, "Ability_W": 0.30})
    param = PARAMS.get(track, default_param)
    odds_w = param.get("Odds_W", 0.70)
    ability_w = param.get("Ability_W", 0.30)

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
        past_runs = row.get("past_runs", [])[:4]
        
        raw_pos = row.get("位置取り")
        if not raw_pos or pd.isna(raw_pos) or str(raw_pos).strip() in ["", "nan"]:
            pos_type = estimate_position_type_final_corner(past_runs)
        else:
            pos_type = str(raw_pos).strip()

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
        if w_diff <= -2.0: weight_adj = 5
        elif w_diff <= -1.0: weight_adj = 3
        elif w_diff <= -0.5: weight_adj = 1
        elif -0.5 < w_diff < 0.5: weight_adj = 0
        elif w_diff < 1.0: weight_adj = -1
        elif w_diff < 2.0: weight_adj = -3
        else: weight_adj = -5

        phase2_lines.append("\n###### 斤量補正判定プロセス")
        phase2_lines.append(f"* 平均斤量＝{avg_weight:.2f}kg | 当該馬斤量＝{row['斤量']}kg | 差＝{w_diff:+.2f}kg → Weight_補正＝{weight_adj:+d}")

        j_score, j_rank, j_desc = get_jockey_score_and_rank(row["騎手"])
        oversea_adj = 2 if len(past_runs) > 0 and past_runs[0].get("is_foreign_or_local", False) else 0
        
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
                    current_date = datetime.strptime(race_date_raw.replace("/", "."), "%Y%m%d")
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

        is_sashi_favored = (track == "東京") or (track in OUTER_TRACKS and distance in OUTER_TRACKS[track])
        is_nige_favored = not is_sashi_favored

        pos_adj = 0
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
            f"* 海外出走：{oversea_adj}点 | 上がり3F：{f3_adj}点 | 当日補填：{cond_adj}点 | "
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

    # ==========================================================================
    # 3連複荒れ度判定ロジック（5段階化：堅い・小荒・中荒・大荒・並）
    # ==========================================================================
    if payout_probs:
        p_under_30 = payout_probs.get("30倍以下", 0)
        p_30_50    = payout_probs.get("30～50倍", 0)
        p_50_80    = payout_probs.get("50～80倍", 0)
        p_80_120   = payout_probs.get("80～120倍", 0)
        p_over_120 = payout_probs.get("120倍以上", 0)

        if p_under_30 >= 35 or (p_under_30 + p_30_50) >= 65:
            race_pattern = "堅い"
            pattern_desc = "30倍以下の低配当確率が高く、本命・人気決着が濃厚なレースです。"
        elif p_30_50 >= 35 or (p_under_30 + p_30_50) >= 50:
            race_pattern = "並"
            pattern_desc = "30～60倍の中配当が中心となる標準的なレースです。"
        elif p_50_80 >= 35 or (p_30_50 + p_50_80) >= 50:
            race_pattern = "小荒"
            pattern_desc = "50～80倍の中高配当が想定されるやや波乱含みのレースです。"
        elif p_80_120 >= 35 or (p_50_80 + p_80_120) >= 50:
            race_pattern = "中荒"
            pattern_desc = "80～130倍の高配当を中心に想定される波乱含みのレースです。"
        elif p_over_120 >= 35 or (p_80_120 + p_over_120) >= 65:
            race_pattern = "大荒"
            pattern_desc = "120倍以上の超高配当確率が高く、大波乱が警戒されるレースです。"
        else:
            race_pattern = "不明"
            pattern_desc = "この配当データでは推定できません。"
    else:
        race_pattern = "データ不足"
        pattern_desc = "配当データ不足のため不明を適用します。"

    top3_odds_indices = df_sorted.sort_values(by="単勝オッズ").index[:3]
    top3_in_place_counts = np.sum(ranks[:, top3_odds_indices] <= 3, axis=1)

    prob_top3_3 = (np.sum(top3_in_place_counts == 3) / NUM_SIMS) * 100
    prob_top3_2 = (np.sum(top3_in_place_counts == 2) / NUM_SIMS) * 100
    prob_top3_1 = (np.sum(top3_in_place_counts == 1) / NUM_SIMS) * 100
    prob_top3_0 = (np.sum(top3_in_place_counts == 0) / NUM_SIMS) * 100

    prob_top3_2_or_more = prob_top3_2 + prob_top3_3

    # ==========================================================================
    # 買い目選定ロジック
    # ==========================================================================
    df_valid = df_sorted[
        (df_sorted["オッズ順位"] < 10) & 
        (df_sorted["単勝オッズ"] < 30.0)
    ].copy()

    if len(df_valid) < 5:
        df_valid = df_sorted.head(10).copy()

    df_odds_sorted = df_valid.sort_values(by="オッズ順位")
    
    if not df_odds_sorted.empty:
        o1_val = df_odds_sorted.iloc[0]["単勝オッズ"]
        
        if o1_val < 3.0:
            jiku_horse = df_odds_sorted.iloc[0]
            jiku_reason = f"単勝オッズ1位が3倍未満（{o1_val}倍）のため単勝オッズ1位を選出"
        else:
            if len(df_odds_sorted) >= 2:
                o2_val = df_odds_sorted.iloc[1]["単勝オッズ"]
                odds_diff = abs(o2_val - o1_val)
                top2_df = df_odds_sorted.head(2)
                
                if odds_diff < 0.3:
                    jiku_horse = top2_df.sort_values(by="合成順位").iloc[1]
                    jiku_reason = f"単勝オッズ1位が3倍以上で上位2頭のオッズ差が0.3未満（{odds_diff:.2f}）のため合成順位上位2位を選出"
                else:
                    jiku_horse = top2_df.sort_values(by="合成順位").iloc[0]
                    jiku_reason = f"単勝オッズ1位が3倍以上で上位2頭のオッズ差が0.3以上（{odds_diff:.2f}）のため合成順位上位1位を選出"
            else:
                jiku_horse = df_odds_sorted.iloc[0]
                jiku_reason = f"単勝オッズ1位が3倍以上（{o1_val}倍）だが対象馬が1頭のみのため選出"
    else:
        jiku_horse = df_sorted.iloc[0]
        jiku_reason = "条件該当馬不在のため全体上位馬を選出"

    df_without_jiku = df_valid[df_valid["馬番"] != jiku_horse["馬番"]].copy()
    aite1_df = df_without_jiku.sort_values(by="オッズ順位").head(2)

    if (prob_top3_2_or_more < 30.0) and (race_pattern in ["中荒", "大荒"]):
        aite2_count = 5
        aite_reason_str = "上位3頭から2頭入る確率が30%未満かつ荒れ予想（中荒・大荒）のため、相手2は軸馬・相手1を除き合成順位上位5頭選出"
    else:
        aite2_count = 4
        aite_reason_str = "通常条件のため、相手2は軸馬・相手1を除き合成順位上位4頭選出"

    exclude_horses = set([jiku_horse["馬番"]] + aite1_df["馬番"].tolist())
    df_aite2_pool = df_valid[~df_valid["馬番"].isin(exclude_horses)].copy()
    if df_aite2_pool.empty:
        df_aite2_pool = df_sorted[~df_sorted["馬番"].isin(exclude_horses)].copy()
        
    aite2_df = df_aite2_pool.sort_values(by="合成順位").head(aite2_count)

    aite1_horses = aite1_df["馬番"].tolist()
    aite2_horses = aite2_df["馬番"].tolist()

    selected_aite_df = pd.concat([aite1_df, aite2_df]).drop_duplicates(subset=["馬番"])
    
    all_aite_nums = sorted(selected_aite_df["馬番"].tolist())
    sanrenpuku_combos = list(itertools.combinations(all_aite_nums, 2))
    fmt_points = len(sanrenpuku_combos)

    jiku_log_lines = ["\n#### ■ 3-3. 軸馬・相手馬決定判定プロセス"]
    jiku_log_lines.append(f"【軸馬判定】：{jiku_reason} → 馬番{jiku_horse['馬番']}（{jiku_horse['馬名']}）")
    jiku_log_lines.append(f"【相手判定】：{aite_reason_str}")

    ODDS_RANGE_MAP = {
        "堅い": "配当目安 ～30倍",
        "並": "配当目安 30～60倍",
        "小荒": "配当目安 50～80倍",
        "中荒": "配当目安 80～130倍",
        "大荒": "配当目安 120倍～"
    }
    target_odds_range = ODDS_RANGE_MAP.get(race_pattern, "")

    phase6_lines = [
        "#### ■ PHASE 6：最終ランキングと買い目\n",
        f"#### 1. レース情報\n[{race_name} / {track}{race_no}R / {distance}m]",
        f"* **取得3連複オッズ**:\n{odds_table_md}",
        f"\n* **推定配当確率**:\n{prob_table_md}",
        f"\n**【レース判定結果】：{race_pattern}** （{pattern_desc}）\n",
        f"  * 単勝1〜3番人気の複勝(3着以内)入着シミュレーション:",
        f"    0頭入る確率: **{prob_top3_0:.1f}%**  ",
        f"    1頭入る確率: **{prob_top3_1:.1f}%**  ",
        f"    2頭入る確率: **{prob_top3_2:.1f}%**  ",
        f"    3頭入る確率: **{prob_top3_3:.1f}%**  ",
        f"    ★2頭以上入る合計確率: **{prob_top3_2_or_more:.1f}%**  ",
        f"    　70%以上はフォーメーション検討  ",
        f"    　30%以下は軸注意\n",
        "#### 2. 最終ランキング\n",
        "| 順位 | 馬(オッズ) | 合成値(順位) | オッズ(順位) | 能力(順位) | 勝率 | 複勝率 | 期待値 | 位置 | 走数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    ]

    selected_horse_numbers = set([jiku_horse["馬番"]] + all_aite_nums)

    for idx, r in df_sorted.iterrows():
        rank_num = idx + 1
        pos = r.get("位置取り", "差") 
        valid_runs = r.get("走数", 0)
        win_mc = f"{r['勝率(MC)']:.1f}%"
        place_mc = f"{r['複勝率(MC)']:.1f}%"
        ev_val = f"{r['期待値']:.2f}"
        
        if r["馬番"] in selected_horse_numbers:
            phase6_lines.append(
                f"| **{rank_num}** | **{r['馬番']} {r['馬名']}({r['単勝オッズ']}倍)** | **{r['合成値']:.2f} ({r['合成順位']}位)** | **{r['オッズスコア']:.1f} ({r['オッズ順位']}位)** | **{r['能力スコア']:.1f} ({r['能力順位']}位)** | **{win_mc}** | **{place_mc}** | **{ev_val}** | **{pos}** | **{valid_runs}** |"
            )
        else:
            phase6_lines.append(
                f"| {rank_num} | {r['馬番']} {r['馬名']}({r['単勝オッズ']}倍) | {r['合成値']:.2f} ({r['合成順位']}位) | {r['オッズスコア']:.1f} ({r['オッズ順位']}位) | {r['能力スコア']:.1f} ({r['能力順位']}位) | {win_mc} | {place_mc} | {ev_val} | {pos} | {valid_runs} |"
            )

    phase6_lines.append(f"\n#### 3. 買い目（判定：【{race_pattern}】 {target_odds_range}）\n")

    aite1_formatted_parts = []
    for h in aite1_horses:
        p_val = df_sorted.loc[df_sorted["馬番"] == h, "複勝率(MC)"]
        p_str = f"{p_val.values[0]:.1f}%" if not p_val.empty else "0.0%"
        aite1_formatted_parts.append((str(h), p_str))

    aite1_nums_str = ", ".join([item[0] for item in aite1_formatted_parts])
    aite1_rates_str = ",".join([item[1] for item in aite1_formatted_parts])
    aite1_display_str = f"{aite1_nums_str}（{aite1_rates_str}）"

    aite2_str = ", ".join(f"{h:>2}" for h in aite2_horses)

    phase6_lines.append("**【３連複１頭軸流し（馬番表記）】**  ")
    phase6_lines.append(f"軸  ：{jiku_horse['馬番']}（{jiku_horse['単勝オッズ']}倍）  ")
    phase6_lines.append(f"相手1：{aite1_display_str}  ")
    phase6_lines.append(f"相手2：{aite2_str}  \n")

    jiku_odds_rank = int(jiku_horse['オッズ順位'])
    aite1_odds_ranks = [int(df_sorted[df_sorted['馬番'] == h]['オッズ順位'].values[0]) for h in aite1_horses]
    aite2_odds_ranks = [int(df_sorted[df_sorted['馬番'] == h]['オッズ順位'].values[0]) for h in aite2_horses]
    
    phase6_lines.append("**【３連複１頭軸流し（オッズ順位表記）】**  ")
    phase6_lines.append(f"軸  ：{jiku_odds_rank}  ")
    phase6_lines.append(f"相手1：{', '.join([str(x) for x in aite1_odds_ranks])}  ")
    phase6_lines.append(f"相手2：{', '.join([str(x) for x in aite2_odds_ranks])}\n")

    phase6_lines.append("#### 4. 入力用買い目\n")
    
    jiku_val = jiku_horse["馬番"]
    aite_str = ",".join(map(str, all_aite_nums))
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