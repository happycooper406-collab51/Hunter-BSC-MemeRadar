#!/usr/bin/env python3
"""
Four.meme 早期買家分析器 - Etherscan API V2 專用版
使用 Etherscan API V2 分析 BSC 代幣的早期買家
"""

from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
import requests
import time
import csv
import io
from datetime import datetime
from typing import Dict, List
import json
import uuid
import os
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# ==================== Session 管理（文件存儲，支援多 Workers）====================
SESSION_DIR = '/tmp/analysis_sessions'
os.makedirs(SESSION_DIR, exist_ok=True)
cleanup_lock = Lock()

# ==================== 付費系統配置 ====================

# 付費 API Key Pool（隱藏，用戶看不到）
PAID_API_KEY_POOL = [
    'ARYE15PZYTC77DK4S5JC4ZHIP4GRX9DFFT',
    '5RI4PWM4ZWDBB937DD71ZXSJEWX5UTB81U',
    'AHAE4UIUJMXYR8STF7C9R8ZYQTXG96J5AH',
    'ZT8JEKHWVS8R542RK2HRMB8ERXNY2QFVSD',
    'ESV8RZU94RBXYDP7NBYQ6PKTTHYJU7CBZR'
]

# 收款地址
PAYMENT_RECEIVER = '0xe0b7e35556731872B3CE18c9645D69290F428C1C'

# 付費配置
PAYMENT_AMOUNT_BNB = 0.01  # BNB
PAYMENT_TOKEN_EXPIRY = 3600  # 1 小時（秒）
PAYMENT_TOKEN_USES = 3  # 3 次使用

# 付費憑證存儲
PAYMENT_TOKENS_FILE = '/tmp/payment_tokens.json'
USED_TX_HASHES_FILE = '/tmp/used_tx_hashes.json'

def load_payment_tokens():
    """加載付費憑證"""
    try:
        with open(PAYMENT_TOKENS_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_payment_tokens(tokens):
    """保存付費憑證"""
    with open(PAYMENT_TOKENS_FILE, 'w') as f:
        json.dump(tokens, f)

def load_used_tx_hashes():
    """加載已使用的交易 Hash"""
    try:
        with open(USED_TX_HASHES_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_used_tx_hashes(hashes):
    """保存已使用的交易 Hash"""
    with open(USED_TX_HASHES_FILE, 'w') as f:
        json.dump(hashes, f)

def get_paid_api_key():
    """隨機獲取一個付費 API Key"""
    import random
    return random.choice(PAID_API_KEY_POOL)

# ==================== Session 管理繼續 ====================

# ==================== 並發控制系統 ====================

# 最大並發分析數
MAX_CONCURRENT_ANALYSIS = 4  # 根據 Render 資源使用率調整：512MB 可支援 4-6 個並發

# 線程池
analysis_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ANALYSIS)

# 活躍任務追蹤
active_tasks = []  # 存儲 {session_id, start_time, token_address}
active_tasks_lock = threading.Lock()

# 排隊系統
analysis_queue_data = []  # 存儲 {session_id, queued_at, position}
queue_lock = threading.Lock()

def get_system_status():
    """獲取系統狀態"""
    with active_tasks_lock:
        active_count = len(active_tasks)
        active_list = [
            {
                'session_id': task['session_id'],
                'token': task['token_address'][:10] + '...',
                'elapsed': int(time.time() - task['start_time'])
            }
            for task in active_tasks
        ]
    
    with queue_lock:
        queue_count = len(analysis_queue_data)
        queue_list = [
            {
                'session_id': q['session_id'],
                'position': q['position'],
                'wait_time': int(time.time() - q['queued_at'])
            }
            for q in analysis_queue_data
        ]
    
    return {
        'active_count': active_count,
        'active_tasks': active_list,
        'queue_count': queue_count,
        'queue_list': queue_list,
        'max_concurrent': MAX_CONCURRENT_ANALYSIS,
        'slots_available': MAX_CONCURRENT_ANALYSIS - active_count,
        'total_users': active_count + queue_count,  # 總使用人數
        'is_busy': active_count >= MAX_CONCURRENT_ANALYSIS  # 是否繁忙
    }

def add_active_task(session_id, token_address):
    """添加活躍任務"""
    with active_tasks_lock:
        active_tasks.append({
            'session_id': session_id,
            'token_address': token_address,
            'start_time': time.time()
        })
        print(f"✅ 添加活躍任務: {session_id}, 當前活躍: {len(active_tasks)}")

def remove_active_task(session_id):
    """移除活躍任務"""
    with active_tasks_lock:
        active_tasks[:] = [t for t in active_tasks if t['session_id'] != session_id]
        print(f"✅ 移除活躍任務: {session_id}, 剩餘活躍: {len(active_tasks)}")

def add_to_queue(session_id):
    """添加到排隊"""
    with queue_lock:
        position = len(analysis_queue_data) + 1
        analysis_queue_data.append({
            'session_id': session_id,
            'position': position,
            'queued_at': time.time()
        })
        print(f"📥 添加到排隊: {session_id}, 位置: {position}")
        return position

def remove_from_queue(session_id):
    """從排隊移除"""
    with queue_lock:
        analysis_queue_data[:] = [q for q in analysis_queue_data if q['session_id'] != session_id]
        # 重新計算位置
        for i, q in enumerate(analysis_queue_data):
            q['position'] = i + 1

def can_start_analysis():
    """檢查是否可以開始分析"""
    with active_tasks_lock:
        return len(active_tasks) < MAX_CONCURRENT_ANALYSIS

# ==================== Session 管理繼續 ====================

def get_session_path(session_id):
    """獲取 session 文件路徑"""
    return os.path.join(SESSION_DIR, f"{session_id}.json")

def create_analysis_session():
    """創建新的分析會話（文件存儲）"""
    session_id = str(uuid.uuid4())
    session_data = {
        'status': 'processing',
        'stage': '初始化',
        'progress': 0,
        'message': '分析即將開始...',
        'total': 0,
        'completed': 0,
        'estimated_time': 0,
        'start_time': time.time(),
        'created_at': time.time()
    }
    
    with open(get_session_path(session_id), 'w') as f:
        json.dump(session_data, f)
    
    print(f"✅ Session 創建（文件）: {session_id}")
    return session_id

def get_session(session_id):
    """從文件讀取 session"""
    try:
        with open(get_session_path(session_id), 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None

def update_session_progress(session_id, stage='', progress=0, message='', total=0, completed=0):
    """更新 session 進度（文件存儲）"""
    session_path = get_session_path(session_id)
    
    try:
        with open(session_path, 'r') as f:
            session = json.load(f)
    except FileNotFoundError:
        return
    
    if stage:
        session['stage'] = stage
    if progress >= 0:
        session['progress'] = progress
    if message:
        session['message'] = message
    if total > 0:
        session['total'] = total
    if completed >= 0:
        session['completed'] = completed
    
    if session['start_time'] > 0 and progress > 5 and progress < 95:
        elapsed = time.time() - session['start_time']
        total_estimated = elapsed / (progress / 100)
        remaining = total_estimated - elapsed
        # 限制最小值，避免顯示負數或太小的數字
        session['estimated_time'] = max(5, int(remaining))
    else:
        session['estimated_time'] = 0
    
    with open(session_path, 'w') as f:
        json.dump(session, f)

def cleanup_old_sessions():
    """清理超過 1 小時的舊會話文件"""
    with cleanup_lock:
        current_time = time.time()
        for filename in os.listdir(SESSION_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(SESSION_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        session = json.load(f)
                    if current_time - session['created_at'] > 3600:
                        os.remove(filepath)
                        print(f"🗑️ 清理舊 session: {filename}")
                except:
                    pass

def complete_session(session_id, status='completed', result=None):
    """標記會話為完成或錯誤（文件存儲）"""
    session_path = get_session_path(session_id)
    
    try:
        with open(session_path, 'r') as f:
            session = json.load(f)
        
        session['status'] = status
        session['progress'] = 100
        if result:
            session['result'] = result
        
        with open(session_path, 'w') as f:
            json.dump(session, f)
        print(f"✅ Session 完成: {session_id}, status: {status}")
    except FileNotFoundError:
        print(f"⚠️ Session 不存在: {session_id}")
        pass
# ==================== 進度追蹤結束 ====================

# 排除的系統地址
EXCLUDE_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}


class FourMemeAnalyzer:
    def __init__(self):
        self.session = requests.Session()
    
    def _get_bnb_amount_from_tx(self, api_key: str, tx_hash: str, address: str) -> dict:
        """從交易 hash 獲取該地址的 BNB 流入/流出"""
        try:
            address = address.lower()
            
            # 首先檢查主交易的 value
            params = {
                "module": "proxy",
                "action": "eth_getTransactionByHash",
                "txhash": tx_hash
            }
            
            main_tx_bnb_out = 0
            main_tx_bnb_in = 0
            
            data = self._call_etherscan_v2_api(api_key, params)
            if data.get("result"):
                tx = data["result"]
                
                # 檢查 result 是否為字典（可能是錯誤訊息字串）
                if not isinstance(tx, dict):
                    print(f"      ⚠️ 交易數據格式錯誤: {tx}")
                    return {'bnb_in': 0, 'bnb_out': 0, 'net_bnb': 0}
                
                from_addr = tx.get('from', '').lower()
                to_addr = tx.get('to', '').lower()
                value_hex = tx.get('value', '0x0')
                
                # 處理十六進制值
                if isinstance(value_hex, str):
                    value = int(value_hex, 16) if value_hex.startswith('0x') else int(value_hex)
                else:
                    value = int(value_hex)
                
                # 如果用戶是交易發起者且有 value，說明用戶支付了 BNB
                if from_addr == address and value > 0:
                    main_tx_bnb_out = value / 1e18
                # 如果用戶是接收者且有 value，說明用戶收到了 BNB
                elif to_addr == address and value > 0:
                    main_tx_bnb_in = value / 1e18
            
            # 然後檢查內部交易
            params = {
                "module": "account",
                "action": "txlistinternal",
                "txhash": tx_hash,
                "sort": "asc"
            }
            
            internal_bnb_in = 0
            internal_bnb_out = 0
            
            data = self._call_etherscan_v2_api(api_key, params)
            if data.get("status") == "1" and data.get("result"):
                internal_txs = data["result"]
                
                for tx in internal_txs:
                    from_addr = tx.get('from', '').lower()
                    to_addr = tx.get('to', '').lower()
                    value = int(tx.get('value', 0))
                    
                    if to_addr == address:
                        internal_bnb_in += value / 1e18
                    if from_addr == address:
                        internal_bnb_out += value / 1e18
            
            # 合併主交易和內部交易的結果
            total_bnb_in = main_tx_bnb_in + internal_bnb_in
            total_bnb_out = main_tx_bnb_out + internal_bnb_out
            
            return {
                'bnb_in': total_bnb_in,
                'bnb_out': total_bnb_out,
                'net_bnb': total_bnb_in - total_bnb_out
            }
            
        except Exception as e:
            print(f"      獲取 BNB 金額失敗: {e}")
            return {'bnb_in': 0, 'bnb_out': 0, 'net_bnb': 0}
    
    def _get_bnb_price_usd(self) -> float:
        """獲取 BNB 當前 USD 價格"""
        try:
            print(f"   正在獲取 BNB 價格...")
            
            # 方案 1: Binance API（最可靠）
            try:
                url = "https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT"
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    price = float(data.get('price', 0))
                    if price > 0:
                        print(f"   ✅ BNB 價格: ${price:.2f} USD (Binance)")
                        return price
            except Exception as e:
                print(f"   Binance API 失敗: {e}")
            
            # 方案 2: CoinGecko API（備用）
            try:
                url = "https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd"
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    price = float(data.get('binancecoin', {}).get('usd', 0))
                    if price > 0:
                        print(f"   ✅ BNB 價格: ${price:.2f} USD (CoinGecko)")
                        return price
            except Exception as e:
                print(f"   CoinGecko API 失敗: {e}")
            
            print(f"   ⚠️  無法獲取 BNB USD 價格，將使用 BNB 作為本位")
            return 0.0
            
        except Exception as e:
            print(f"   ⚠️  獲取 BNB 價格錯誤: {e}")
            return 0.0
    
    def _call_etherscan_v2_api(self, api_key: str, params: dict) -> dict:
        """調用 Etherscan API V2（支持多鏈）"""
        base_url = "https://api.etherscan.io/v2/api"
        
        # 添加 BSC Chain ID (56) 和 API Key
        params["chainid"] = "56"  # BNB Smart Chain
        params["apikey"] = api_key
        
        try:
            response = self.session.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            # 打印詳細錯誤信息
            if data.get("status") == "0":
                print(f"   ❌ API 錯誤詳情:")
                print(f"      Status: {data.get('status')}")
                print(f"      Message: {data.get('message')}")
                print(f"      Result: {data.get('result')}")
            
            return data
        except Exception as e:
            print(f"Etherscan API V2 Error: {e}")
            return {"status": "0", "result": [], "message": str(e)}
    
    def analyze_token(self, api_key: str, token_address: str, start_seconds: int, end_seconds: int, max_txs_per_buyer: int = 100, session_id: str = None) -> dict:
        """分析代幣在指定時間區間內的買家"""
        token_address = token_address.lower().strip()
        
        # 如果沒有 session_id，創建一個（用於非 API 調用）
        if session_id is None:
            session_id = create_analysis_session()
        
        # 定義進度更新函數（綁定 session_id）
        def update_progress(stage='', progress=0, message='', total=0, completed=0):
            update_session_progress(session_id, stage, progress, message, total, completed)
        
        print(f"\n[Etherscan API V2] 分析代幣: {token_address}")
        print(f"   Chain ID: 56 (BNB Smart Chain)")
        
        # 初始化進度
        update_progress(stage='初始化', progress=0, message='正在初始化分析...')
        
        # 格式化顯示起始時間
        start_minutes = start_seconds // 60
        start_secs = start_seconds % 60
        if start_minutes > 0 and start_secs > 0:
            start_display = f"{start_minutes} 分 {start_secs} 秒"
        elif start_minutes > 0:
            start_display = f"{start_minutes} 分"
        else:
            start_display = f"{start_secs} 秒"
        
        # 格式化顯示結束時間
        end_minutes = end_seconds // 60
        end_secs = end_seconds % 60
        if end_minutes > 0 and end_secs > 0:
            end_display = f"{end_minutes} 分 {end_secs} 秒"
        elif end_minutes > 0:
            end_display = f"{end_minutes} 分"
        else:
            end_display = f"{end_secs} 秒"
        
        print(f"   時間區間: 開盤後 {start_display} ~ {end_display}")
        print(f"   機器人閾值: {max_txs_per_buyer} 筆")
        
        # 使用默認代幣信息（tokeninfo 端點需要 API Pro，跳過）
        token_info = {"name": "Unknown", "symbol": "Unknown", "decimals": 18}
        
        # 獲取所有交易
        all_transfers = []
        page = 1
        
        while True:
            params = {
                "module": "account",
                "action": "tokentx",
                "contractaddress": token_address,
                "startblock": 0,
                "endblock": 99999999,
                "page": page,
                "offset": 10000,
                "sort": "asc",
            }
            
            data = self._call_etherscan_v2_api(api_key, params)
            
            if data.get("status") == "0":
                if not all_transfers:
                    return {"success": False, "error": f"API 錯誤: {data.get('message', '')}", "token_info": token_info}
                break
            
            if not data.get("result"):
                break
            
            transfers = data["result"]
            if isinstance(transfers, str):
                return {"success": False, "error": f"API 錯誤: {transfers}", "token_info": token_info}
            
            all_transfers.extend(transfers)
            print(f"   已獲取 {len(all_transfers)} 筆交易...")
            
            if len(transfers) < 10000:
                break
            
            page += 1
            time.sleep(0.25)
        
        if not all_transfers:
            return {"success": False, "error": "找不到任何交易記錄", "token_info": token_info}
        
        # 從第一筆交易中提取代幣信息
        if all_transfers:
            first_tx = all_transfers[0]
            token_info = {
                "name": first_tx.get("tokenName", "Unknown"),
                "symbol": first_tx.get("tokenSymbol", "Unknown"),
                "decimals": int(first_tx.get("tokenDecimal", 18)),
            }
            print(f"   代幣: {token_info['name']} ({token_info['symbol']})")
        
        # 獲取 BNB 價格（用於計算 USD）
        print(f"   正在獲取 BNB 價格...")
        bnb_price_usd = self._get_bnb_price_usd()
        
        # 設定價格信息
        if bnb_price_usd > 0:
            # 使用 BNB 作為本位
            print(f"   將使用 BNB 作為計價單位")
            token_info["bnb_price_usd"] = bnb_price_usd
            token_info["use_bnb"] = True
            token_info["price_usd"] = 0  # 不使用代幣價格
        else:
            # 無法獲取 BNB 價格
            print(f"   ⚠️  無法獲取 BNB 價格，將只顯示代幣數量")
            token_info["price_usd"] = 0
            token_info["use_bnb"] = False
        
        # 傳遞機器人閾值
        token_info["max_txs_per_buyer"] = max_txs_per_buyer
        
        return self._analyze_transfers(all_transfers, token_info, start_seconds, end_seconds, api_key, update_progress=update_progress)
    
    def _analyze_transfers(self, transfers: List[dict], token_info: dict, start_seconds: int, end_seconds: int, api_key: str = None, update_progress=None) -> dict:
        """分析交易數據（時間區間版本）"""
        
        # 如果沒有傳入 update_progress，使用空函數
        if update_progress is None:
            update_progress = lambda **kwargs: None
        
        if not transfers:
            return {"success": False, "error": "沒有交易數據", "token_info": token_info}
        
        # 確保所有時間戳和數值字段都是正確的類型
        for tx in transfers:
            if 'timeStamp' in tx and isinstance(tx['timeStamp'], str):
                tx['timeStamp'] = int(tx['timeStamp'])
            if 'value' in tx and isinstance(tx['value'], str):
                tx['value'] = int(tx['value'])
            if 'tokenDecimal' in tx and isinstance(tx['tokenDecimal'], str):
                tx['tokenDecimal'] = int(tx['tokenDecimal'])
            if 'blockNumber' in tx and isinstance(tx['blockNumber'], str):
                tx['blockNumber'] = int(tx['blockNumber'])
        
        creation_time = min(tx['timeStamp'] for tx in transfers)
        start_cutoff_time = creation_time + start_seconds  # 區間起始
        end_cutoff_time = creation_time + end_seconds      # 區間結束
        
        print(f"   開盤時間: {datetime.fromtimestamp(creation_time).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   區間起始: {datetime.fromtimestamp(start_cutoff_time).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   區間結束: {datetime.fromtimestamp(end_cutoff_time).strftime('%Y-%m-%d %H:%M:%S')}")
        
        early_buyers = {}
        all_buyers = {}
        
        # 用於記錄每個地址的所有交易 hash
        address_txs = {}  # {address: [(tx_hash, 'buy'/'sell', timestamp)]}
        
        for tx in transfers:
            from_addr = tx['from'].lower()
            to_addr = tx['to'].lower()
            value = tx['value']
            timestamp = tx['timeStamp']
            decimal = tx.get('tokenDecimal', token_info['decimals'])
            tx_hash = tx.get('hash', '')
            
            # 排除系統地址
            if from_addr in EXCLUDE_ADDRESSES or to_addr in EXCLUDE_ADDRESSES:
                continue
            
            # 記錄所有買家
            if to_addr not in all_buyers:
                all_buyers[to_addr] = {
                    'first_buy_time': timestamp,
                    'buy_amount': 0,
                    'sell_amount': 0,
                    'buy_count': 0,
                    'sell_count': 0,
                    'last_sell_time': 0
                }
                address_txs[to_addr] = []
            
            # 買入
            token_amount = value / (10 ** decimal)  # 轉換為真實數量
            all_buyers[to_addr]['buy_amount'] += token_amount
            all_buyers[to_addr]['buy_count'] += 1
            if tx_hash:
                address_txs[to_addr].append((tx_hash, 'buy', timestamp))
            
            # 賣出（from）
            if from_addr in all_buyers:
                all_buyers[from_addr]['sell_amount'] += token_amount
                all_buyers[from_addr]['sell_count'] += 1
                all_buyers[from_addr]['last_sell_time'] = timestamp
                if tx_hash and from_addr in address_txs:
                    address_txs[from_addr].append((tx_hash, 'sell', timestamp))
            
            # 識別區間內的買家（修改：在 start_seconds 到 end_seconds 之間）
            if start_cutoff_time <= timestamp <= end_cutoff_time and to_addr not in early_buyers:
                early_buyers[to_addr] = {
                    'address': to_addr,
                    'first_buy_time': timestamp,
                    'buy_amount': 0,
                    'sell_amount': 0,
                    'buy_count': 0,
                    'sell_count': 0,
                    'last_sell_time': 0
                }
        
        # 計算早期買家的完整交易統計
        for addr in early_buyers:
            if addr in all_buyers:
                early_buyers[addr].update(all_buyers[addr])
        
        # 【新增】精準計算 BNB 成本和利潤
        use_bnb_calculation = api_key and token_info.get('bnb_price_usd', 0) > 0
        
        if use_bnb_calculation and api_key:
            print(f"\n   🔍 正在精準計算 BNB 成本和利潤...")
            print(f"   早期買家數量: {len(early_buyers)} 人")
            
            # 從 token_info 獲取自訂閾值
            max_txs_per_buyer = token_info.get('max_txs_per_buyer', 100)
            print(f"   機器人篩選閾值: {max_txs_per_buyer} 筆")
            
            # ===== 優化：第一階段 - 收集所有需要查詢的 tx_hash =====
            update_progress(stage='收集交易', progress=20, message='收集交易列表中...')
            print(f"\n   📦 階段 1/2: 收集交易列表...")
            all_tx_hashes = set()
            valid_buyers = {}  # 過濾後的買家
            skipped_buyers = 0
            
            for addr in early_buyers:
                # 沒有交易記錄
                if addr not in address_txs or len(address_txs[addr]) == 0:
                    early_buyers[addr]['bnb_spent'] = 0
                    early_buyers[addr]['bnb_received'] = 0
                    early_buyers[addr]['bnb_profit'] = 0
                    early_buyers[addr]['is_bot'] = False
                    continue
                
                buyer_txs = address_txs[addr]
                
                # 跳過機器人
                if len(buyer_txs) > max_txs_per_buyer:
                    skipped_buyers += 1
                    print(f"      ⚠️  跳過 {addr[:8]}... ({len(buyer_txs)} 筆 - 疑似機器人)")
                    early_buyers[addr]['bnb_spent'] = 0
                    early_buyers[addr]['bnb_received'] = 0
                    early_buyers[addr]['bnb_profit'] = 0
                    early_buyers[addr]['is_bot'] = True
                    continue
                
                # 記錄有效買家
                valid_buyers[addr] = buyer_txs
                
                # 收集所有 tx_hash
                for tx_hash, tx_type, timestamp in buyer_txs:
                    all_tx_hashes.add(tx_hash)
            
            print(f"   ✅ 收集完成")
            print(f"      有效買家: {len(valid_buyers)} 人")
            print(f"      機器人: {skipped_buyers} 個")
            print(f"      不重複交易: {len(all_tx_hashes)} 筆")
            
            update_progress(stage='查詢交易', progress=40, message=f'需要查詢 {len(all_tx_hashes)} 筆交易', total=len(all_tx_hashes), completed=0)
            
            # ===== 優化：第二階段 - 批次查詢所有交易（按地址快取） =====
            print(f"\n   💰 階段 2/2: 批次查詢 BNB 流動...")
            # 使用二維快取：tx_cache[address][tx_hash] = bnb_data
            tx_cache = {}
            queried_count = 0
            total_queries_needed = sum(len(txs) for txs in valid_buyers.values())
            
            for addr, buyer_txs in valid_buyers.items():
                if addr not in tx_cache:
                    tx_cache[addr] = {}
                
                for tx_hash, tx_type, timestamp in buyer_txs:
                    # 只查詢該地址還沒查過的交易
                    if tx_hash not in tx_cache[addr]:
                        tx_cache[addr][tx_hash] = self._get_bnb_amount_from_tx(api_key, tx_hash, addr)
                        queried_count += 1
                        
                        # 每秒 5 次（你的付費版限制）
                        time.sleep(0.2)
                        
                        # 進度提示
                        if queried_count % 50 == 0:
                            progress_pct = 40 + int(40 * queried_count / total_queries_needed)  # 40-80%
                            update_progress(
                                stage='查詢交易',
                                progress=progress_pct,
                                message=f'已查詢 {queried_count}/{total_queries_needed} 筆交易',
                                total=total_queries_needed,
                                completed=queried_count
                            )
                            print(f"      ✅ 已查詢 {queried_count}/{total_queries_needed} 筆 ({queried_count/total_queries_needed*100:.1f}%)")
            
            print(f"   ✅ 查詢完成！共 {queried_count} 筆交易")
            
            update_progress(stage='計算利潤', progress=80, message='開始計算利潤...')
            # ===== 第三階段 - 使用快取計算利潤（快速，不調用 API） =====
            print(f"\n   🧮 計算利潤中...")
            processed_buyers = 0
            
            for addr, buyer_txs in valid_buyers.items():
                processed_buyers += 1
                
                bnb_spent = 0
                bnb_received = 0
                
                for tx_hash, tx_type, timestamp in buyer_txs:
                    # 從快取讀取（不調用 API，瞬間完成）
                    bnb_data = tx_cache[addr].get(tx_hash, {'bnb_out': 0, 'bnb_in': 0})
                    
                    if tx_type == 'buy':
                        # 買入：用戶支付 BNB
                        bnb_spent += bnb_data['bnb_out']
                    else:  # sell
                        # 賣出：用戶收到 BNB
                        bnb_received += bnb_data['bnb_in']
                
                early_buyers[addr]['bnb_spent'] = bnb_spent
                early_buyers[addr]['bnb_received'] = bnb_received
                early_buyers[addr]['bnb_profit'] = bnb_received - bnb_spent
                early_buyers[addr]['is_bot'] = False
                
                # 進度提示（計算很快）
                if processed_buyers % 20 == 0:
                    print(f"      ✅ 已計算 {processed_buyers}/{len(valid_buyers)} 人")
            
            print(f"   ✅ 計算完成！")
            print(f"\n   📊 統計摘要:")
            print(f"      分析了 {processed_buyers} 人")
            print(f"      跳過了 {skipped_buyers} 個疑似機器人")
            print(f"      查詢了 {queried_count} 筆交易")
        
        # 計算早期買家的完整交易統計
        for addr in early_buyers:
            if addr in all_buyers:
                early_buyers[addr].update(all_buyers[addr])
        
        # 轉換為列表並計算持倉、利潤、倍數
        early_buyers_list = []
        current_time = int(time.time())
        price_usd = token_info.get('price_usd', 0.0)
        
        for addr, data in early_buyers.items():
            decimal = token_info['decimals']
            buy_amount = data['buy_amount'] / (10 ** decimal)
            sell_amount = data['sell_amount'] / (10 ** decimal)
            holding = buy_amount - sell_amount
            
            sell_ratio = (sell_amount / buy_amount * 100) if buy_amount > 0 else 0
            
            # 計算持倉時間
            first_buy_timestamp = data['first_buy_time']
            last_sell_timestamp = data.get('last_sell_time', current_time)
            
            # 如果還持有，持倉時間到現在；如果已清倉，持倉時間到最後賣出
            if holding > 0:
                holding_duration = current_time - first_buy_timestamp
                is_holding = True
            else:
                holding_duration = last_sell_timestamp - first_buy_timestamp
                is_holding = False
            
            # 格式化持倉時間
            hours = holding_duration // 3600
            minutes = (holding_duration % 3600) // 60
            if hours > 24:
                days = hours // 24
                remaining_hours = hours % 24
                holding_time_str = f"{days}天{remaining_hours}小時"
            elif hours > 0:
                holding_time_str = f"{hours}小時{minutes}分"
            else:
                holding_time_str = f"{minutes}分鐘"
            
            # 計算利潤和倍數
            bnb_price_usd = token_info.get('bnb_price_usd', 0)
            
            # 獲取 BNB 數據（可能不存在）
            bnb_spent = data.get('bnb_spent', 0)
            bnb_received = data.get('bnb_received', 0)
            bnb_profit = bnb_received - bnb_spent
            
            # 判斷是否有有效的 BNB 數據
            has_valid_bnb_data = (bnb_spent > 0 or bnb_received > 0) and bnb_price_usd > 0
            
            if has_valid_bnb_data:
                # 使用精準的 BNB 數據
                
                # 計算還持有的代幣價值（用 BNB）
                holding_value_bnb = 0
                if sell_amount > 0 and bnb_received > 0:
                    # 平均賣出價格（BNB per token）
                    avg_sell_price_bnb = bnb_received / sell_amount
                    holding_value_bnb = holding * avg_sell_price_bnb
                elif buy_amount > 0 and bnb_spent > 0:
                    # 平均買入價格（BNB per token）
                    avg_buy_price_bnb = bnb_spent / buy_amount
                    holding_value_bnb = holding * avg_buy_price_bnb
                
                # 總價值 = 已賣出的 BNB + 還持有的代幣價值
                total_value_bnb = bnb_received + holding_value_bnb
                
                # BNB 倍數計算
                if bnb_spent > 0:
                    profit_multiple = total_value_bnb / bnb_spent
                elif bnb_received > 0 and bnb_spent == 0:
                    # 無成本獲得（空投/獎勵），設為特殊值
                    profit_multiple = 999.99  # 表示極高倍數
                else:
                    profit_multiple = 0
                
                # 轉換為 USD
                buy_value_usd = bnb_spent * bnb_price_usd
                sell_value_usd = bnb_received * bnb_price_usd
                holding_value_usd = holding_value_bnb * bnb_price_usd
                total_profit_usd = (total_value_bnb - bnb_spent) * bnb_price_usd
                
                # 記錄 BNB 數據
                bnb_spent_display = bnb_spent
                bnb_received_display = bnb_received
                bnb_profit_display = bnb_profit
                
            elif price_usd > 0:
                # 使用代幣價格估算
                buy_value_usd = buy_amount * price_usd
                sell_value_usd = sell_amount * price_usd
                holding_value_usd = holding * price_usd
                
                # 總利潤 = 已賣出的價值 + 還持有的價值 - 買入成本
                total_profit_usd = (sell_value_usd + holding_value_usd) - buy_value_usd
                
                # 投資倍數 = (賣出價值 + 持有價值) / 買入成本
                profit_multiple = ((sell_value_usd + holding_value_usd) / buy_value_usd) if buy_value_usd > 0 else 0
                
                # 沒有 BNB 數據
                bnb_spent_display = 0
                bnb_received_display = 0
                bnb_profit_display = 0
            else:
                buy_value_usd = 0
                sell_value_usd = 0
                holding_value_usd = 0
                total_profit_usd = 0
                profit_multiple = 0
                bnb_spent_display = 0
                bnb_received_display = 0
                bnb_profit_display = 0
            
            early_buyers_list.append({
                'address': addr,
                'first_buy_time': datetime.fromtimestamp(first_buy_timestamp).strftime('%Y-%m-%d %H:%M:%S'),
                'buy_amount': buy_amount,
                'sell_amount': sell_amount,
                'holding': holding,
                'sell_ratio': sell_ratio,
                'status': '仍持倉' if is_holding else '已清倉',
                'buy_count': data['buy_count'],
                'sell_count': data['sell_count'],
                'holding_time': holding_time_str,
                'holding_duration_seconds': holding_duration,
                'buy_value_usd': buy_value_usd,
                'sell_value_usd': sell_value_usd,
                'holding_value_usd': holding_value_usd,
                'total_profit_usd': total_profit_usd,
                'profit_multiple': profit_multiple,
                'bnb_spent': bnb_spent_display,
                'bnb_received': bnb_received_display,
                'bnb_profit': bnb_profit_display,
                'is_bot': data.get('is_bot', False)
            })
        
        # 按買入時間排序
        early_buyers_list.sort(key=lambda x: x['first_buy_time'])
        
        # 統計
        total_buyers = len(early_buyers_list)
        cleared_buyers = sum(1 for b in early_buyers_list if b['holding'] <= 0)
        holding_buyers = total_buyers - cleared_buyers
        
        total_buy = sum(b['buy_amount'] for b in early_buyers_list)
        cleared_ratio = (cleared_buyers / total_buyers * 100) if total_buyers > 0 else 0
        holding_ratio = (holding_buyers / total_buyers * 100) if total_buyers > 0 else 0
        
        # 標記進度完成
        update_progress(stage='完成', progress=100, message='分析完成！')
        
        print(f"   ✅ 分析完成！")
        print(f"      區間買家: {total_buyers} 人")
        print(f"      已清倉: {cleared_buyers} 人 ({cleared_ratio:.1f}%)")
        print(f"      仍持倉: {holding_buyers} 人 ({holding_ratio:.1f}%)")
        
        return {
            "success": True,
            "token_info": token_info,
            "stats": {
                "total_buyers": total_buyers,
                "total_buy": total_buy,
                "cleared_buyers": cleared_buyers,
                "holding_buyers": holding_buyers,
                "cleared_ratio": cleared_ratio,
                "holding_ratio": holding_ratio,
            },
            "buyers": early_buyers_list,
        }


# 全局分析器實例
analyzer = FourMemeAnalyzer()


@app.route("/")
def index():
    return render_template("index.html")



@app.route('/health', methods=['GET'])
def health_check():
    """健康檢查端點"""
    cleanup_old_sessions()  # 順便清理舊會話
    return jsonify({
        'status': 'ok',
        'timestamp': time.time(),
        'active_sessions': len(all_analysis_sessions)
    }), 200


@app.route('/api/system-status', methods=['GET'])
def get_status():
    """獲取系統狀態"""
    status = get_system_status()
    return jsonify(status)

@app.route('/api/verify-payment', methods=['POST'])
def verify_payment():
    """驗證付費並生成憑證"""
    try:
        from web3 import Web3
        
        data = request.json
        tx_hash = data.get('tx_hash') or ""
        if isinstance(tx_hash, str):
            tx_hash = tx_hash.strip()
        
        if not tx_hash:
            return jsonify({'success': False, 'error': '缺少交易 Hash'})
        
        # 檢查是否已使用過此交易
        used_hashes = load_used_tx_hashes()
        if tx_hash in used_hashes:
            return jsonify({'success': False, 'error': '此交易已被使用'})
        
        # 連接 BSC 網路
        w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed.binance.org/'))
        
        # 獲取交易詳情
        try:
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:
            return jsonify({'success': False, 'error': f'無法獲取交易: {str(e)}'})
        
        # 驗證收款地址
        if tx['to'].lower() != PAYMENT_RECEIVER.lower():
            return jsonify({'success': False, 'error': '收款地址錯誤'})
        
        # 驗證金額（允許 ±1% 誤差）
        amount_bnb = float(w3.from_wei(tx['value'], 'ether'))
        expected = PAYMENT_AMOUNT_BNB
        if not (expected * 0.99 <= amount_bnb <= expected * 1.01):
            return jsonify({
                'success': False, 
                'error': f'金額錯誤：收到 {amount_bnb} BNB，應為 {expected} BNB'
            })
        
        # 驗證交易成功
        if receipt['status'] != 1:
            return jsonify({'success': False, 'error': '交易失敗'})
        
        # 生成付費憑證
        payment_token = str(uuid.uuid4())
        
        # 保存憑證
        tokens = load_payment_tokens()
        tokens[payment_token] = {
            'tx_hash': tx_hash,
            'from_address': tx['from'],
            'amount': amount_bnb,
            'created_at': time.time(),
            'expiry': time.time() + PAYMENT_TOKEN_EXPIRY,
            'uses_left': PAYMENT_TOKEN_USES,
            'used_times': []
        }
        save_payment_tokens(tokens)
        
        # 標記交易已使用
        used_hashes.append(tx_hash)
        save_used_tx_hashes(used_hashes)
        
        print(f"✅ 付費驗證成功: {tx_hash[:10]}... → Token: {payment_token[:8]}...")
        
        return jsonify({
            'success': True,
            'token': payment_token,
            'expiry_seconds': PAYMENT_TOKEN_EXPIRY,
            'uses_left': PAYMENT_TOKEN_USES,
            'amount_paid': amount_bnb
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'驗證失敗: {str(e)}'})

@app.route('/api/check-payment-token', methods=['POST'])
def check_payment_token():
    """檢查付費憑證狀態"""
    try:
        data = request.json
        payment_token = data.get('token') or ""
        if isinstance(payment_token, str):
            payment_token = payment_token.strip()
        
        if not payment_token:
            return jsonify({'valid': False, 'error': '缺少憑證'})
        
        tokens = load_payment_tokens()
        
        if payment_token not in tokens:
            return jsonify({'valid': False, 'error': '憑證不存在'})
        
        token_data = tokens[payment_token]
        
        # 檢查過期
        if time.time() > token_data['expiry']:
            return jsonify({'valid': False, 'error': '憑證已過期'})
        
        # 檢查次數
        if token_data['uses_left'] <= 0:
            return jsonify({'valid': False, 'error': '使用次數已用完'})
        
        # 計算剩餘時間
        time_left = int(token_data['expiry'] - time.time())
        
        return jsonify({
            'valid': True,
            'uses_left': token_data['uses_left'],
            'time_left_seconds': time_left,
            'time_left_minutes': time_left // 60
        })
        
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)})

@app.route('/api/progress/<session_id>', methods=['GET'])
def get_progress_api(session_id):
    """獲取特定會話的進度（從文件讀取）"""
    session = get_session(session_id)
    if session:
        return jsonify(session)
    else:
        return jsonify({
            'status': 'error',
            'message': 'Session not found'
        }), 404

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    # 創建新的分析會話
    session_id = create_analysis_session()
    
    try:
        data = request.json
        
        # 檢查是否為付費用戶
        is_paid = data.get("is_paid", False)
        payment_token = data.get("payment_token") or ""
        if isinstance(payment_token, str):
            payment_token = payment_token.strip()
        
        # 處理 API Key
        if is_paid and payment_token:
            # 付費用戶：驗證憑證並使用付費 API Key
            tokens = load_payment_tokens()
            
            if payment_token not in tokens:
                return jsonify({"success": False, "error": "付費憑證無效"})
            
            token_data = tokens[payment_token]
            
            # 檢查過期
            if time.time() > token_data['expiry']:
                return jsonify({"success": False, "error": "付費憑證已過期"})
            
            # 檢查次數
            if token_data['uses_left'] <= 0:
                return jsonify({"success": False, "error": "使用次數已用完"})
            
            # 扣除使用次數
            token_data['uses_left'] -= 1
            token_data['used_times'].append(time.time())
            save_payment_tokens(tokens)
            
            # 使用付費 API Key（隨機選擇）
            api_key = get_paid_api_key()
            
            print(f"💰 付費用戶使用: Token {payment_token[:8]}... 剩餘 {token_data['uses_left']} 次")
            
        else:
            # 免費用戶：使用自己的 API Key
            api_key = data.get("api_key") or ""
            if isinstance(api_key, str):
                api_key = api_key.strip()
            if not api_key:
                return jsonify({"success": False, "error": "需要 Etherscan API Key"})
        
        token_address = data.get("token_address") or ""
        if isinstance(token_address, str):
            token_address = token_address.strip()
        
        # 新增：支援時間區間
        start_minutes = int(data.get("start_minutes", 0))
        start_seconds = int(data.get("start_seconds", 0))
        end_minutes = int(data.get("end_minutes", 0))
        end_seconds = int(data.get("end_seconds", 0))
        
        max_txs = int(data.get("max_txs", 100))
        
        # 計算總秒數
        start_total_seconds = (start_minutes * 60) + start_seconds
        end_total_seconds = (end_minutes * 60) + end_seconds
        
        # 計算查詢區間
        query_duration = end_total_seconds - start_total_seconds
        
        # 驗證
        if end_total_seconds <= 0:
            return jsonify({"success": False, "error": "結束時間必須大於 0"})
        
        if start_total_seconds >= end_total_seconds:
            return jsonify({"success": False, "error": "起始時間必須小於結束時間"})
        
        # 限制最多 5 分鐘（300 秒）
        if query_duration > 300:
            return jsonify({
                "success": False, 
                "error": f"查詢區間過長！最多 5 分鐘（300 秒），您的設定為 {query_duration} 秒"
            })
        
        if not api_key:
            return jsonify({"success": False, "error": "需要 Etherscan API Key"})
        
        if not token_address or not token_address.startswith("0x") or len(token_address) != 42:
            return jsonify({"success": False, "error": "無效的合約地址格式"})
        
        if max_txs < 0:
            return jsonify({"success": False, "error": "機器人閾值必須 >= 0"})
        
        # 檢查是否可以立即開始
        if not can_start_analysis():
            # 檢查排隊是否已滿
            with queue_lock:
                if len(analysis_queue_data) >= 5:
                    return jsonify({
                        "success": False,
                        "error": "系統繁忙，排隊已滿，請稍後再試",
                        "queue_full": True,
                        "system_status": get_system_status()
                    }), 503
                
                # 加入排隊
                position = add_to_queue(session_id)
                estimated_wait = position * 5  # 每個任務約5分鐘
                
                return jsonify({
                    "success": True,
                    "session_id": session_id,
                    "status": "queued",
                    "queue_position": position,
                    "estimated_wait_minutes": estimated_wait,
                    "message": f"排隊中...位置: {position}，預計等待: {estimated_wait} 分鐘",
                    "system_status": get_system_status()
                })
        
        # 可以立即開始，添加到活躍任務
        add_active_task(session_id, token_address)
        
        print(f"🚀 準備啟動異步分析，Session ID: {session_id}")
        
        def run_analysis():
            try:
                print(f"🔧 線程開始執行，Session ID: {session_id}")
                result = analyzer.analyze_token(
                    api_key, 
                    token_address, 
                    start_total_seconds, 
                    end_total_seconds, 
                    max_txs, 
                    session_id=session_id
                )
                print(f"✅ 分析完成，準備標記 session")
                complete_session(session_id, 'completed', result=result)
                print(f"✅ Session 標記完成")
            except Exception as e:
                print(f"❌ 分析錯誤: {str(e)}")
                complete_session(session_id, 'error')
                import traceback
                traceback.print_exc()
            finally:
                # 移除活躍任務
                remove_active_task(session_id)
                # 從排隊移除（如果在隊列中）
                remove_from_queue(session_id)
        
        # 使用線程池提交任務
        analysis_executor.submit(run_analysis)
        print(f"✅ 任務已提交到線程池")
        
        # 立即返回 session_id
        return jsonify({
            "success": True,
            "session_id": session_id,
            "status": "processing",
            "message": "分析已開始，請等待...",
            "system_status": get_system_status()
        })
        
    except Exception as e:
        # 清理資源
        remove_active_task(session_id)
        remove_from_queue(session_id)
        complete_session(session_id, 'error')
        
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": f"分析錯誤: {str(e)}",
            "session_id": session_id
        })


@app.route('/api/export', methods=['POST'])
def export_csv():
    """匯出為 CSV 文件"""
    data = request.json
    buyers = data.get('buyers', [])
    token_info = data.get('token_info', {})
    
    # 使用 UTF-8 with BOM 編碼，讓 Excel 正確顯示中文
    output = io.StringIO()
    # 寫入 BOM (Byte Order Mark) 讓 Excel 識別 UTF-8
    output.write('\ufeff')
    
    writer = csv.writer(output)
    
    # 寫入表頭
    writer.writerow([
        "地址",
        "首次買入",
        "BNB成本",
        "BNB收益",
        "BNB利潤",
        "總利潤(USD)",
        "倍數",
        "持倉時間",
        "狀態",
        "買入次數",
        "賣出次數"
    ])
    
    # 寫入數據
    for buyer in buyers:
        writer.writerow([
            buyer['address'],
            buyer['first_buy_time'],
            f"{buyer.get('bnb_spent', 0):.4f}",
            f"{buyer.get('bnb_received', 0):.4f}",
            f"{buyer.get('bnb_profit', 0):.4f}",
            f"{buyer.get('total_profit_usd', 0):.2f}",
            f"{buyer.get('profit_multiple', 0):.2f}",
            buyer.get('holding_time', '-'),
            buyer['status'],
            buyer['buy_count'],
            buyer['sell_count']
        ])
    
    output.seek(0)
    
    # 獲取內容並轉換為 UTF-8 with BOM
    csv_content = output.getvalue()
    
    return Response(
        csv_content.encode('utf-8-sig'),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment;filename=early_buyers_{token_info.get('symbol', 'token')}.csv",
            "Content-Type": "text/csv; charset=utf-8"
        }
    )


if __name__ == "__main__":
    print("\n" + "="*70)
    print("  🔥 Four.meme 早期買家分析器")
    print("="*70)
    print("\n  使用 Etherscan API V2")
    print("  支持 BSC (Chain ID: 56)")
    print("\n  📝 註冊免費 API Key: https://bscscan.com/register")
    print("\n  啟動中...")
    
    # 支援雲端平台的端口配置
    import os
    port = int(os.environ.get("PORT", 5000))
    
    if port == 5000:
        print("  請在瀏覽器打開: http://localhost:5000")
        print("\n  按 Ctrl+C 停止服務")
    else:
        print(f"  運行在端口: {port}")
    
    print("="*70 + "\n")
    
    app.run(debug=False, host="0.0.0.0", port=port)
