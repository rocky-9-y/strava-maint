import streamlit as st
import gspread
import json
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

# ==========================================
# 初期設定
# ==========================================
load_dotenv()
st.set_page_config(page_title="Strava メンテナンス管理", layout="wide", initial_sidebar_state="collapsed")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ローカル(.env)とクラウド(st.secrets)の両方に対応する関数
def get_env(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key)

ACCOUNTS = [
    {
        "id": "sl8",
        "tab_name": "SL8",
        "athlete_id": get_env("INTERVALS_SL8_ATHLETE_ID"),
        "api_key": get_env("INTERVALS_SL8_API_KEY"),
        "target_bike_name": "sl8",
        "data_file": os.path.join(BASE_DIR, "maintenance_data.json")
    },
    {
        "id": "merida",
        "tab_name": "Merida",
        "athlete_id": get_env("INTERVALS_MERIDA_ATHLETE_ID"),
        "api_key": get_env("INTERVALS_MERIDA_API_KEY"),
        "target_bike_name": "merida",
        "data_file": os.path.join(BASE_DIR, "maintenance_data_merida.json")
    }
]

PART_ORDER = ["タイヤ(F)", "タイヤ(R)", "ブレーキパッド(F)", "ブレーキパッド(R)", "チェーン", "シフトワイヤー"]

# ==========================================
# スプレッドシート連携 (クラウドDB)
# ==========================================
@st.cache_resource
def init_gspread():
    gc = None
    try:
        if "gcp_service_account" in st.secrets:
            # Streamlit Cloud環境
            gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
        
    if gc is None:
        # ローカル環境
        key_path = os.path.join(BASE_DIR, "service_account.json")
        gc = gspread.service_account(filename=key_path)
        
    sh = gc.open_by_key(get_env("SPREADSHEET_KEY"))
    return sh.get_worksheet(0)

try:
    worksheet = init_gspread()
except Exception as e:
    st.error(f"スプレッドシートの接続に失敗しました。設定を確認してください。\n詳細: {e}")
    st.stop()

def migrate_local_data(account):
    acc_id = account["id"]
    if os.path.exists(account["data_file"]):
        cell = worksheet.find(acc_id, in_column=1)
        if cell is None:
            with open(account["data_file"], 'r', encoding='utf-8') as f:
                local_data = json.load(f)
            worksheet.append_row([acc_id, json.dumps(local_data, ensure_ascii=False)])
            st.toast(f"{acc_id} のデータをクラウドに移行しました！", icon="☁️")

def load_maintenance_data(account, bike_id, bike_name, current_distance):
    acc_id = account["id"]
    cell = worksheet.find(acc_id, in_column=1)
    
    if cell is not None:
        data = json.loads(worksheet.cell(cell.row, 2).value)
        data["bike_id"] = bike_id
        data["bike_name"] = bike_name
    else:
        data = None

    if data is None:
        data = {
            "bike_id": bike_id, "bike_name": bike_name,
            "parts": {p: {"last_maint_km": current_distance, "default_interval": 3000, "current_interval": 3000, "enabled": True, "history": []} for p in PART_ORDER}
        }
        data["parts"]["チェーン"]["default_interval"] = 500
        data["parts"]["チェーン"]["current_interval"] = 500
        data["parts"]["シフトワイヤー"]["enabled"] = False
        save_maintenance_data(account, data)
        return data

    parts = data["parts"]
    needs_save = False
    for p in PART_ORDER:
        if p not in parts:
            parts[p] = {"last_maint_km": current_distance, "default_interval": 3000, "current_interval": 3000, "enabled": True, "history": []}
            needs_save = True
    if needs_save:
        save_maintenance_data(account, data)
    return data

def save_maintenance_data(account, data):
    acc_id = account["id"]
    json_str = json.dumps(data, ensure_ascii=False)
    cell = worksheet.find(acc_id, in_column=1)
    
    if cell is not None:
        worksheet.update_cell(cell.row, 2, json_str)
    else:
        worksheet.append_row([acc_id, json_str])

# ==========================================
# Intervals.icu API
# ==========================================
@st.cache_data(ttl=300)
def get_bike_distance(athlete_id, api_key, target_bike_name, acc_id):
    if not athlete_id or not api_key:
        raise Exception("APIキーが設定されていません。")
    auth = ("API_KEY", api_key)
    res = requests.get(f"https://intervals.icu/api/v1/athlete/{athlete_id}/gear", auth=auth)
    if res.status_code != 200:
        raise Exception(f"データ取得失敗: {res.text}")
    gears = res.json()
    target_bike = next((g for g in gears if target_bike_name.lower() in g["name"].lower()), None)
    if not target_bike and acc_id == "merida":
        target_bike = next((g for g in gears if any(n in g["name"].lower() for n in ["scultura", "スクルトゥーラ", "メリダ"])), None)
    if not target_bike:
        raise Exception("対象のバイクが見つかりません。")
    return target_bike["id"], target_bike["name"], target_bike.get("distance", 0) / 1000.0

# ==========================================
# ダイアログ (ポップアップUI)
# ==========================================
@st.dialog("🔧 メンテ実施の確認")
def confirm_maintenance(acc_id, part, app_data):
    st.write(f"**【{part}】** のメンテを実施として記録しますか？")
    if st.button("はい、記録する", type="primary", use_container_width=True):
        info = app_data["data"]["parts"][part]
        current_odo = app_data["current_distance"]
        actual_interval = current_odo - info["last_maint_km"]
        
        if actual_interval > 0:
            info["default_interval"] = actual_interval
            info["current_interval"] = actual_interval
        info["last_maint_km"] = current_odo
        
        memo_text = f"メンテ実施 (使用距離: {actual_interval:.1f}km)" if actual_interval > 0 else "メンテ実施"
        history_list = info.setdefault("history", [])
        history_list.append({"date": datetime.now().strftime("%Y-%m-%d"), "odo": current_odo, "memo": memo_text})
        history_list.sort(key=lambda x: (x.get("odo", 0), x.get("date", "")), reverse=True)
        
        save_maintenance_data(app_data["account"], app_data["data"])
        st.rerun()

@st.dialog("📜 履歴管理")
def history_dialog(acc_id, part, app_data):
    history_list = app_data["data"]["parts"][part].get("history", [])
    for i, h in enumerate(history_list):
        c1, c2, c3, c4 = st.columns([2, 2, 4, 1])
        c1.write(h.get("date", ""))
        c2.write(f"{h.get('odo', 0):.1f} km")
        c3.write(h.get("memo", ""))
        if c4.button("🗑️", key=f"del_{acc_id}_{part}_{i}"):
            del history_list[i]
            save_maintenance_data(app_data["account"], app_data["data"])
            st.rerun()
            
    st.divider()
    st.write("#### ➕ 手動追加")
    with st.form(key=f"add_form_{acc_id}_{part}"):
        new_date = st.text_input("日付 (YYYY-MM-DD)", value=datetime.now().strftime("%Y-%m-%d"))
        new_odo = st.number_input("ODO (km)", value=float(app_data["current_distance"]))
        new_memo = st.text_input("メモ")
        if st.form_submit_button("追加", use_container_width=True):
            history_list.append({"date": new_date, "odo": new_odo, "memo": new_memo})
            history_list.sort(key=lambda x: (x.get("odo", 0), x.get("date", "")), reverse=True)
            save_maintenance_data(app_data["account"], app_data["data"])
            st.rerun()

@st.dialog("🛠️ 設定変更")
def edit_dialog(acc_id, part, app_data):
    info = app_data["data"]["parts"][part]
    with st.form(key=f"edit_form_{acc_id}_{part}"):
        new_last = st.number_input("前回メンテ時のバイクODO (km)", value=float(info["last_maint_km"]))
        new_int = st.number_input("メンテナンス周期 (km)", value=float(info["default_interval"]))
        if st.form_submit_button("保存して閉じる", use_container_width=True):
            info["last_maint_km"] = new_last
            info["default_interval"] = new_int
            info["current_interval"] = new_int
            save_maintenance_data(app_data["account"], app_data["data"])
            st.rerun()

# ==========================================
# メインUI描画
# ==========================================
def main():
    st.title("🚲 Strava メンテナンス管理")
    
    for acc in ACCOUNTS:
        migrate_local_data(acc)

    tabs = st.tabs([acc["tab_name"] for acc in ACCOUNTS])
    
    for i, account in enumerate(ACCOUNTS):
        with tabs[i]:
            acc_id = account["id"]
            try:
                bike_id, bike_name, current_distance = get_bike_distance(account["athlete_id"], account["api_key"], account["target_bike_name"], acc_id)
                data = load_maintenance_data(account, bike_id, bike_name, current_distance)
                app_data = {"account": account, "current_distance": current_distance, "data": data}
                
                st.subheader(f"バイク: {bike_name} ｜ 現在のODO: {current_distance:.1f} km")
                st.divider()
                
                # ヘッダー行の追加
                hcol1, hcol2, hcol3, hcol4, hcol5, hcol6, hcol7 = st.columns([2, 1.5, 1.5, 1.5, 1.5, 1.5, 2])
                hcol1.write("**パーツ名**")
                hcol2.write("**状態**")
                hcol3.write("**使用距離**")
                hcol4.write("**残り距離**")
                hcol5.write("**メンテ周期**")
                hcol6.write("**前回ODO**")
                hcol7.write("**アクション**")
                st.divider()
                
                for part in PART_ORDER:
                    if part not in data["parts"]: continue
                    info = data["parts"][part]
                    
                    col1, col2, col3, col4, col5, col6, col7 = st.columns([2, 1.5, 1.5, 1.5, 1.5, 1.5, 2])
                    col1.write(f"**{part}**")
                    
                    if not info["enabled"]:
                        col2.write("⚪ 無効")
                        col3.write("-")
                        col4.write("-")
                        col5.write(f"{info['current_interval']:.1f} km")
                        col6.write(f"{info['last_maint_km']:.1f} km")
                        with col7:
                            if st.button("有効化", key=f"en_{acc_id}_{part}", use_container_width=True):
                                info["enabled"] = True
                                info["last_maint_km"] = current_distance
                                info["current_interval"] = info["default_interval"]
                                save_maintenance_data(account, data)
                                st.rerun()
                    else:
                        run_distance = current_distance - info["last_maint_km"]
                        remain_km = info["current_interval"] - run_distance
                        status = "🔴 警告" if remain_km <= 0 else "🟡 注意" if remain_km <= 200 else "🟢 OK"
                        
                        col2.write(status)
                        col3.write(f"{run_distance:.1f} km")
                        col4.write(f"{remain_km:.1f} km")
                        col5.write(f"{info['current_interval']:.1f} km")
                        col6.write(f"{info['last_maint_km']:.1f} km")
                        
                        with col7:
                            with st.popover("⚙️ 操作", use_container_width=True):
                                if st.button("🔧 メンテ実施", key=f"maint_{acc_id}_{part}", use_container_width=True):
                                    confirm_maintenance(acc_id, part, app_data)
                                if st.button("➕ +500km延長", key=f"ext_{acc_id}_{part}", use_container_width=True):
                                    info["current_interval"] += 500
                                    save_maintenance_data(account, data)
                                    st.rerun()
                                if st.button("📜 履歴管理", key=f"hist_{acc_id}_{part}", use_container_width=True):
                                    history_dialog(acc_id, part, app_data)
                                if st.button("🛠️ 設定変更", key=f"set_{acc_id}_{part}", use_container_width=True):
                                    edit_dialog(acc_id, part, app_data)
                                if st.button("🚫 無効化", key=f"dis_{acc_id}_{part}", use_container_width=True):
                                    info["enabled"] = False
                                    save_maintenance_data(account, data)
                                    st.rerun()
                    st.divider()
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")

if __name__ == "__main__":
    main()