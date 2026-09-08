import streamlit as st
import pandas as pd
import re
import unicodedata
import time
from logic import run_pipeline, RACE_GRADE_DICT, detect_grade

# 画面上のヘッダーや右上のメニューを非表示にする設定
st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    header {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.set_page_config(page_title="Ver1.62 競馬分析システム", layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
    div[data-testid="stMarkdownContainer"] table {
        font-size: 0.82rem !important;
        width: 100% !important;
    }
    div[data-testid="stMarkdownContainer"] table th,
    div[data-testid="stMarkdownContainer"] table td {
        padding: 4px 6px !important;
        white-space: nowrap !important;
    }
    </style>
""", unsafe_allow_html=True)

st.markdown("### 🏇 競馬分析システム Ver1.62")
st.write("提示された定義書に基づき、過去バイアスゼロ・完全固定ロジックで自動分析を実行します。")

# --- セッション状態の初期化 ---
if "race_text" not in st.session_state:
    st.session_state.race_text = ""
if "good_input_str" not in st.session_state:
    st.session_state.good_input_str = ""
if "bad_input_str" not in st.session_state:
    st.session_state.bad_input_str = ""
if "is_simple" not in st.session_state:
    st.session_state.is_simple = True  
if "trend_input" not in st.session_state:
    st.session_state.trend_input = "フラット"
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None
if "df_parsed" not in st.session_state:
    st.session_state.df_parsed = None

def clear_all():
    st.session_state.race_text = ""
    st.session_state.good_input_str = ""
    st.session_state.bad_input_str = ""
    st.session_state.is_simple = True  
    st.session_state.trend_input = "フラット"
    st.session_state.analysis_result = None
    st.session_state.df_parsed = None

# 1. 出馬表テキストエリア
raw_input = st.text_area(
    "出馬表テキストをここに貼り付けてください",
    key="race_text",
    height=140,
    placeholder="出馬表テキストをペースト...",
    label_visibility="visible"
)

# 2. 当日状態（調子）の設定
st.subheader("⚙️ 設定・補正")

trend_input = st.selectbox(
    "📊 当日傾向",
    options=["フラット", "前有利", "後有利"],
    key="trend_input",
    help="フラット: 加点なし / 前有利: 逃げ・先行 +5点 / 後有利: 差し・追い +5点"
)

col_good, col_bad = st.columns(2)

with col_good:
    good_input_str = st.text_input(
        "🟢 調子のいい馬（能力スコア +5点）",
        key="good_input_str",
        placeholder="例: 1, 3, 10",
        help="馬番をカンマ区切りで入力してください（例: 1,3,10）"
    )

with col_bad:
    bad_input_str = st.text_input(
        "🔴 調子の悪い馬（能力スコア -5点）",
        key="bad_input_str",
        placeholder="例: 2, 5, 12",
        help="馬番をカンマ区切りで入力してください（例: 2,5,12）"
    )

# 3. ボタン2つ（分析実行・クリア）
col_run, col_clear = st.columns([1, 1])

with col_run:
    run_clicked = st.button("🚀 分析を実行", type="primary", use_container_width=True)

with col_clear:
    st.button("🗑️ クリア", on_click=clear_all, use_container_width=True)

# 4. ボタンの下に簡易表示チェックボックスを配置
is_simple = st.checkbox("⚡ 簡易表示（計算過程を省略し、最終結果のみ表示）", key="is_simple")

# 入力されたカンマ区切り文字列を整数のリストに変換するヘルパー関数
def parse_horse_numbers(input_str):
    if not input_str.strip():
        return []
    horses = []
    for item in input_str.replace("，", ",").split(","):
        item = item.strip()
        if item.isdigit():
            horses.append(int(item))
    return horses

KANJI_REPLACE_MAP = str.maketrans({
    "戶": "戸", "⺠": "民", "櫻": "桜", "髙": "高", "﨑": "崎",
    "廐": "厩", "眞": "真", "實": "実", "榮": "栄", "國": "国",
    "萬": "万", "廣": "広", "島": "島", "澤": "沢"
})

# 【前処理】 入力テキスト自体のノイズを強制的に補正する関数
def preprocess_raw_text(text):
    text = text.translate(KANJI_REPLACE_MAP)
    text_norm = unicodedata.normalize("NFKC", text)
    for race_name, grade in RACE_GRADE_DICT.items():
        if race_name in text_norm:
            text_norm = re.sub(
                re.escape(race_name) + r'[\s\u3000]*[\(（]?(1勝C|1勝クラス|2勝C|2勝クラス|3勝C|3勝クラス|未勝利|新馬|OP)[\)）]?',
                f"{race_name}（{grade}）",
                text_norm
            )
    return text_norm

# --- レース基本情報解析関数 ---
def parse_race_info(text):
    first_few_lines = "\n".join([line.strip() for line in text.split("\n")[:10] if line.strip()])
    
    # 競馬場名の取得
    track_match = re.search(r'(東京|中山|阪神|京都|中京|小倉|新潟|福島|札幌|函館)', first_few_lines)
    track_name = track_match.group(1) if track_match else "不明"
    
    # コース種別（芝・ダート・障害）の取得
    surface_match = re.search(r'(芝|ダート|ダ|障害)', first_few_lines)
    if surface_match:
        surface = surface_match.group(1)
        if surface == "ダ":
            surface = "ダート"
    else:
        surface = "不明"  
        
    track = f"{surface}"
    
    dist_match = re.search(r'(\d{3,4})m', first_few_lines)
    distance = int(dist_match.group(1)) if dist_match else 1800
    
    try:
        grade = detect_grade({"race_name": first_few_lines})
    except Exception:
        grade = "1勝C"  

    # --- レース名整形処理 ---
    raw_name = first_few_lines.split('\n')[0] if first_few_lines else "レース情報"
    cleaned_name = re.sub(r'\d{4}年\s*\d{1,2}月\d{1,2}日[\(（][^\)）]+[\)）]', '', raw_name)
    cleaned_name = re.sub(r'\d+回[^\s\d]+\d+日\d+R?', '', cleaned_name)
    cleaned_name = re.sub(r'\s+', ' ', cleaned_name).strip()

    return {
        "track": track,
        "distance": distance,
        "grade": grade,
        "race_name": cleaned_name,
        "raw_header": first_few_lines
    }

def parse_past_runs(block_text):
    past_runs = []
    
    # 日付(YYYY.MM.DD または YYYY/MM/DD)の位置を特定して過去走ブロックに分割
    date_matches = list(re.finditer(r'\b\d{4}[\.\/]\d{1,2}[\.\/]\d{1,2}\b', block_text))
    
    # ヘッダー行以降にある日付を対象とする
    lines = block_text.split('\n')
    header_len = len(lines[0]) if lines else 0
    valid_matches = [m for m in date_matches if m.start() >= header_len]
    
    matches = []
    for i in range(len(valid_matches)):
        start = valid_matches[i].start()
        end = valid_matches[i+1].start() if i + 1 < len(valid_matches) else len(block_text)
        matches.append(block_text[start:end])

    local_and_foreign_keywords = [
        "地方", "海外", "大井", "川崎", "船橋", "浦和", "門別", "盛岡", "水沢",
        "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
        "ロンシャン", "メイダン", "シャティン", "ハッピーバレー", "デルマー", "サンタアニタ"
    ]
    
    for run_str in matches:
        date_m = re.search(r'(\d{4})[\.\/](\d{1,2})[\.\/](\d{1,2})', run_str)
        if date_m:
            run_date = f"{date_m.group(1)}.{int(date_m.group(2)):02d}.{int(date_m.group(3)):02d}"
        else:
            run_date = ""

        dist_m = re.search(r'(\d{3,4})\s*m?\s*([芝ダ])', run_str)
        dist = int(dist_m.group(1)) if dist_m else 1800
        surface = dist_m.group(2) if dist_m else "芝"
        
        rank_m = re.search(r'(\d+)着\s*[\/／\s]*(\d+)頭', run_str)
        if not rank_m:
            rank_m = re.search(r'(\d+)着', run_str)
            rank = int(rank_m.group(1)) if rank_m else 99
        else:
            rank = int(rank_m.group(1))
            
        pop_m = re.search(r'(\d+)番人気', run_str)
        pop = int(pop_m.group(1)) if pop_m else 99
        
        f3_m = re.search(r'3F\s*([\d\.]+)', run_str)
        f3 = float(f3_m.group(1)) if f3_m else 36.0
        
        diff_m = re.search(r'[\(（]([\d\.]+)[\)）]', run_str)
        diff = float(diff_m.group(1)) if diff_m else 0.0
        
        # 通過順の取得
        pass_m = re.search(r'(\d{1,2}(?:-\d{1,2})+)', run_str)
        pass_order = pass_m.group(1) if pass_m else ""
        
        run_lines = [l.strip() for l in run_str.split('\n') if l.strip()]
        race_name = run_lines[1] if len(run_lines) > 1 else "過去走"
        
        is_foreign_or_local = any(kw in run_str for kw in local_and_foreign_keywords)
        
        past_runs.append({
            "date": run_date,
            "race_name": race_name,
            "rank": rank,
            "diff": diff,
            "pop": pop,
            "distance": dist,
            "surface": surface,
            "f3_time": f3,
            "pass_order": pass_order,
            "is_foreign_or_local": is_foreign_or_local,
            "track": surface
        })
        
    return past_runs

def parse_text_to_df(text):
    horses = []
    
    pattern = re.compile(
        r'(?:(?P<frame>\d+)\s+枠\s*)?(?P<num>\d{1,2})\s*番?\s*\n?\s*'
        r'(?P<name>[^\n\d]+?)\s+(?P<odds>[\d\.]+)\s*\((?:単勝)?(?P<pop>\d+)番人気\)',
        re.MULTILINE
    )
    
    matches = list(pattern.finditer(text))
    
    if not matches:
        pattern = re.compile(
            r'(?:(?P<frame>\d+)\s*枠\s*)?(?P<num>\d{1,2})\s*番\s+(?P<name>[^\s\d]+(?:\s+[^\s\d]+)?)\s+(?P<odds>[\d\.]+)\s*\((?:単勝)?(?P<pop>\d+)番人気\)',
            re.MULTILINE
        )
        matches = list(pattern.finditer(text))

    for k, m in enumerate(matches):
        h_num = int(m.group("num"))
        h_name = m.group("name").strip()
        odds = float(m.group("odds"))
        pop = int(m.group("pop"))
        
        # 枠番判定
        f_num = int(m.group("frame")) if m.group("frame") else None
        if f_num is None:
            if h_num <= 1: f_num = 1
            elif h_num <= 2: f_num = 2
            elif h_num <= 4: f_num = 3
            elif h_num <= 6: f_num = 4
            elif h_num <= 8: f_num = 5
            elif h_num <= 10: f_num = 6
            elif h_num <= 13: f_num = 7
            else: f_num = 8

        # 当該馬のブロック範囲を抽出
        start_idx = m.start()
        end_idx = matches[k + 1].start() if k + 1 < len(matches) else len(text)
        horse_block_text = text[start_idx:end_idx]

        # 斤量・騎手
        weight, jockey = 55.0, "未定"
        wj_match = re.search(r'(\d{2}\.\d)kg[\s\u3000]+([^\s\u3000\n]+)', horse_block_text)
        if wj_match:
            weight = float(wj_match.group(1))
            jockey = wj_match.group(2).strip()

        # 過去走・脚質
        past_runs = parse_past_runs(horse_block_text)
        
        pos_type = "差" 
        if past_runs and past_runs[0].get("pass_order"):
            try:
                first_pos = int(past_runs[0]["pass_order"].split("-")[0])
                if first_pos == 1: pos_type = "逃"
                elif first_pos <= 4: pos_type = "先"
                elif first_pos <= 9: pos_type = "差"
                else: pos_type = "追"
            except (ValueError, IndexError):
                pass

        horses.append({
            "枠番": f_num,
            "馬番": h_num,
            "馬名": h_name,
            "単勝オッズ": odds,
            "人気": pop,
            "斤量": weight,
            "騎手": jockey,
            "位置取り": pos_type,
            "取得過去走数": f"{len(past_runs)}走分",
            "past_runs": past_runs
        })

    return pd.DataFrame(horses)

st.divider()

if run_clicked:
    if not raw_input.strip():
        st.warning("⚠️ 出馬表テキストを入力してください。")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        status_text.text("⚙️ 1/3 レース情報および過去4走データを解析中...")
        progress_bar.progress(25)
        time.sleep(0.3)
        
        cleaned_input = preprocess_raw_text(raw_input)
        
        race_info = parse_race_info(cleaned_input)
        df_parsed = parse_text_to_df(cleaned_input)
        
        if df_parsed.empty:
            progress_bar.empty()
            status_text.empty()
            st.error("⚠️ テキストから馬データを読み取れませんでした。入力テキストのフォーマットを確認してください。")
        else:
            st.session_state.df_parsed = df_parsed
            
            status_text.text("🔍 2/3 スコアリング・フィルター処理を実行中...")
            progress_bar.progress(65)
            time.sleep(0.4)
            
            good_horses = parse_horse_numbers(good_input_str)
            bad_horses = parse_horse_numbers(bad_input_str)
            
            result = run_pipeline(
                df_parsed, 
                race_info, 
                good_horses=good_horses, 
                bad_horses=bad_horses,
                is_simple=is_simple,
                trend=trend_input
            )
            st.session_state.analysis_result = result
            
            status_text.text("✅ 3/3 分析が正常に完了しました！")
            progress_bar.progress(100)
            time.sleep(0.3)
            
            progress_bar.empty()
            status_text.empty()
            st.success("分析完了！")

if st.session_state.df_parsed is not None and not st.session_state.df_parsed.empty and not st.session_state.is_simple:
    st.subheader(f"📋 解析された出馬表 （検出馬数: {len(st.session_state.df_parsed)}頭）")
    
    df_display = st.session_state.df_parsed.copy()
    
    md_table_lines = [
        "| 枠番 | 馬番 | 馬名 | 単勝オッズ | 人気 | 斤量 | 騎手 | 位置取り | 取得過去走数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    ]
    
    for _, r in df_display.iterrows():
        past_runs_count = f"{len(r['past_runs'])}走分" if len(r['past_runs']) > 0 else "初出走"
        pos_type = r.get("位置取り", "差")
        odds_str = f"{r['単勝オッズ']:.1f}"
        
        md_table_lines.append(
            f"| {r['枠番']} | {r['馬番']} | {r['馬名']} | {odds_str} | {r['人気']} | {r['斤量']} | {r['騎手']} | {pos_type} | {past_runs_count} |"
        )
        
    st.markdown("\n".join(md_table_lines))

if st.session_state.analysis_result:
    st.markdown("---")
    st.markdown(st.session_state.analysis_result)