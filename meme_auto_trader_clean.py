#!/usr/bin/env python3
"""
XXYY Meme 自动跟单策略 v2
- 每 5 秒请求 trending-list 1min
- 过滤：four.meme + Top10≤20% + 捆绑≤20% + 新钱包≤20% + 24h涨跌>-50%
- 榜一且未买过 → 自动买入 0.01 BNB，gas=1
- 跌出前5名 → 自动卖出
- 最多同时4个仓位，每个代币只买一次
- 风险控制：
  (1) 总亏损≥30%初始资金 → 暂停程序
  (2) 连续3次止损 → 暂停60分钟
"""

import requests, json, time, logging, sys, os
from datetime import datetime

# ==================== 配置 ====================

XXYY_API_KEY = "YOUR_XXYY_API_KEY"
WALLET_ADDRESS = "YOUR_WALLET_ADDRESS"
TRADING_URL = "https://www.xxyy.io/api/trade/open/api/trending-list"
SWAP_URL = "https://www.xxyy.io/api/trade/open/api/swap"
TRADE_STATUS_URL = "https://www.xxyy.io/api/trade/open/api/trade"
TOKEN_QUERY_URL = "https://www.xxyy.io/api/trade/open/api/query"
WALLET_INFO_URL = "https://www.xxyy.io/api/trade/open/api/wallet/info"
PNL_URL = "https://www.xxyy.io/api/trade/open/api/pnl"

BUY_AMOUNT_BNB = 0.04
GAS_TIP = 1
POLL_INTERVAL = 5
MAX_POSITIONS = 4
INITIAL_BALANCE = None   # 程序启动时获取
INITIAL_BALANCE_SET = False

# 亏损止损线
MAX_LOSS_PCT = 30        # 总亏损≥30%初始资金 → 暂停
CONSECUTIVE_SL_MAX = 3   # 连续3次止损 → 暂停60min
PAUSE_DURATION_MIN = 60  # 暂停时长（分钟）

# 过滤阈值
FILTERS = {
    "top10_pct_max": 25,
    "bundle_pct_max": 20,
    "new_wallet_pct_max": 20,
    "price_change_24h_min": -50,
    "holder_min": 50,
    "max_age_min": 30,
}

HEADERS = {
    "Authorization": f"Bearer {XXYY_API_KEY}",
    "Content-Type": "application/json",
}

# ==================== 文件路径 ====================

STATE_FILE = "/root/.openclaw/workspace/meme_trader_state.json"
LOG_FILE = "/root/.openclaw/workspace/meme_trader.log"

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ==================== 状态管理 ====================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "bought": [],
        "positions": [],
        "consecutive_sl": 0,
        "total_loss": 0,
        "paused_until": None,
        "initial_balance": None,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ==================== 工具函数 ====================

def get_wallet_balance():
    """获取当前钱包 BNB 余额"""
    try:
        resp = requests.get(
            f"{WALLET_INFO_URL}?walletAddress={WALLET_ADDRESS}&chain=bsc",
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return float(data.get("data", {}).get("balance", 0))
    except Exception as e:
        logger.error(f"获取余额失败: {e}")
    return None

def get_pnl(token_address):
    """查询单个代币的 PnL"""
    try:
        resp = requests.get(
            f"{PNL_URL}?walletAddress={WALLET_ADDRESS}&tokenAddress={token_address}&chain=bsc",
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", {})
    except Exception as e:
        logger.error(f"查询PnL失败 {token_address}: {e}")
    return {}

def fetch_trending():
    try:
        resp = requests.post(
            TRADING_URL + "?chain=bsc",
            headers=HEADERS,
            json={"period": "1M"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", [])
    except Exception as e:
        logger.error(f"获取趋势榜失败: {e}")
    return []

def swap(token_address, is_buy=True, amount_bnb=BUY_AMOUNT_BNB):
    try:
        payload = {
            "chain": "bsc",
            "walletAddress": WALLET_ADDRESS,
            "tokenAddress": token_address,
            "isBuy": is_buy,
            "amount": amount_bnb,
            "tip": GAS_TIP,
            "slippage": 20,
        }
        resp = requests.post(SWAP_URL, headers=HEADERS, json=payload, timeout=30)
        result = resp.json()
        if result.get("code") == 200:
            tx = result.get("data", {}).get("signature", "")
            return True, tx
        else:
            logger.warning(f"Swap失败: {result}")
            return False, None
    except Exception as e:
        logger.error(f"Swap异常: {e}")
        return False, None

def wait_for_confirm(tx_id, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                TRADE_STATUS_URL,
                params={"txId": tx_id},
                headers=HEADERS,
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 200:
                status = data.get("data", {}).get("status")
                if status == 2:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False

def get_token_info_full(token_address):
    """查询代币完整信息（用于止损判断）"""
    try:
        resp = requests.get(
            f"{TOKEN_QUERY_URL}?ca={token_address}&chain=bsc",
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", {})
    except Exception as e:
        logger.error(f"查询代币详情失败 {token_address}: {e}")
    return {}

# ==================== 过滤逻辑 ====================

def apply_filters(token, state):
    symbol = token.get("symbol", "?")
    address = token.get("tokenAddress", "")
    price_change_24h = float(token.get("priceChange24H", 0) or 0)
    launch_from = (token.get("launchFrom") or "").lower()

    sec = token.get("security", {}) or {}; top10_pct = float(sec.get("topHolder", {}).get("value", 0) or 0)
    audit = token.get("auditInfo", {}) or {}
    bundle_pct = float(audit.get("bundleHp", 0) or 0)
    new_wallet_pct = float(audit.get("newHp", 0) or 0)

    if address in state["bought"]:
        return False, "已买过，跳过"
    if launch_from != "four":
        return False, f"非four发射({launch_from})"
    if top10_pct > FILTERS["top10_pct_max"]:
        return False, f"Top10持仓{top10_pct:.1f}%>{FILTERS['top10_pct_max']}%"
    if bundle_pct > FILTERS["bundle_pct_max"]:
        return False, f"捆绑持仓{bundle_pct:.1f}%>{FILTERS['bundle_pct_max']}%"
    if new_wallet_pct > FILTERS["new_wallet_pct_max"]:
        return False, f"新钱包持仓{new_wallet_pct:.1f}%>{FILTERS['new_wallet_pct_max']}%"
    if price_change_24h <= FILTERS["price_change_24h_min"]:
        return False, f"24h涨跌{price_change_24h:.1f}%≤{-50}%"
    holder_count = int(token.get("holders", 0) or 0)
    if holder_count < FILTERS["holder_min"]:
        return False, f"持币人数{holder_count}<{FILTERS["holder_min"]}"
    create_time_ms = int(token.get("createTime", 0) or 0)
    if create_time_ms > 0:
        age_ms = time.time() * 1000 - create_time_ms
        age_min = age_ms / 60000
        if age_min > FILTERS["max_age_min"]:
            return False, f"代币创建{age_min:.1f}min>{FILTERS["max_age_min"]}min"
        return False, f'持币人数{holder_count}<{FILTERS["holder_min"]}'

    return True, "通过"

# ==================== 风险检查 ====================

def check_risk_pause(state, current_balance):
    """
    检查是否需要暂停：
    1. 总亏损≥30%初始资金
    2. 连续3次止损
    返回 (是否暂停, 原因)
    """
    import time as time_module

    # 检查是否在暂停中
    if state.get("paused_until"):
        paused_until = float(state["paused_until"])
        if time.time() < paused_until:
            remaining = int(paused_until - time.time())
            return True, f"暂停中，剩余 {remaining} 秒"
        else:
            # 暂停结束，恢复
            state["paused_until"] = None
            state["consecutive_sl"] = 0
            save_state(state)
            logger.info("⏸ 暂停结束，恢复运行")

    if not state.get("initial_balance") or not current_balance:
        return False, ""

    initial = state["initial_balance"]
    loss = (initial - current_balance) / initial * 100

    if loss >= MAX_LOSS_PCT:
        reason = f"总亏损 {loss:.1f}% ≥ {MAX_LOSS_PCT}% 初始资金"
        logger.critical(f"🚨 触发风险止损: {reason}")
        return True, reason

    return False, ""

def record_stop_loss(state):
    """记录一次止损，增加连续止损计数"""
    state["consecutive_sl"] = state.get("consecutive_sl", 0) + 1
    count = state["consecutive_sl"]
    logger.warning(f"⚠️ 止损记录 +1，连续 {count} 次")
    if count >= CONSECUTIVE_SL_MAX:
        import time
        pause_until = time.time() + PAUSE_DURATION_MIN * 60
        state["paused_until"] = pause_until
        logger.critical(f"🚨 连续 {CONSECUTIVE_SL_MAX} 次止损，暂停 {PAUSE_DURATION_MIN} 分钟")
    save_state(state)
    return state

def record_profit(state):
    """盈利时重置连续止损计数"""
    if state.get("consecutive_sl", 0) > 0:
        state["consecutive_sl"] = 0
        save_state(state)
        logger.info("✅ 盈利，重置连续止损计数")

# ==================== PnL 统计 ====================

def calc_total_pnl(state):
    """计算当前总 PnL（USDT）"""
    total_pnl = 0.0
    for pos in state.get("positions", []):
        pnl_data = get_pnl(pos["address"])
        if pnl_data:
            pnl_usd = float(pnl_data.get("pnlusd", 0) or 0)
            total_pnl += pnl_usd
    return total_pnl

def print_pnl_report(state, current_balance):
    """打印盈亏报告"""
    if not state.get("initial_balance"):
        return

    initial = state["initial_balance"]
    loss_pct = (initial - current_balance) / initial * 100
    total_invested = len(state.get("positions", [])) * BUY_AMOUNT_BNB

    lines = []
    lines.append(f"💰 当前 BNB 余额: {current_balance:.4f}")
    lines.append(f"📊 初始资金: {initial:.4f} BNB")
    lines.append(f"📉 当前亏损: {loss_pct:+.2f}%")
    lines.append(f"🔢 持仓数: {len(state.get('positions', []))}/{MAX_POSITIONS}")
    lines.append(f"⏸ 连续止损: {state.get('consecutive_sl', 0)}/{CONSECUTIVE_SL_MAX}")

    logger.info(" | ".join(lines))

# ==================== 主循环 ====================

def main():
    global INITIAL_BALANCE, INITIAL_BALANCE_SET

    logger.info("=" * 60)
    logger.info("XXYY Meme 自动跟单策略 v2")
    logger.info(f"购买金额: {BUY_AMOUNT_BNB} BNB | GAS: {GAS_TIP}")
    logger.info(f"过滤: four.meme | Top10≤{FILTERS['top10_pct_max']}% | 捆绑≤{FILTERS['bundle_pct_max']}% | 新钱包≤{FILTERS['new_wallet_pct_max']}% | 24h>-50% | 持币>{FILTERS['holder_min']}")
    logger.info(f"风险: 亏损≥{MAX_LOSS_PCT}%初始资金暂停 | 连续{CONSECUTIVE_SL_MAX}次止损暂停{PAUSE_DURATION_MIN}min")
    logger.info(f"钱包: {WALLET_ADDRESS}")
    logger.info("=" * 60)

    state = load_state()

    # 初始化资金
    if not state.get("initial_balance"):
        bal = get_wallet_balance()
        if bal:
            state["initial_balance"] = bal
            state["paused_until"] = None
            state["consecutive_sl"] = 0
            save_state(state)
            logger.info(f"📌 初始资金记录: {bal:.4f} BNB")
        else:
            logger.error("无法获取初始余额，退出")
            sys.exit(1)

    logger.info(f"初始资金: {state['initial_balance']:.4f} BNB")
    logger.info(f"加载状态: 已买{len(state['bought'])}个 | 持仓{len(state['positions'])}个 | 连续止损{state.get('consecutive_sl',0)}次")

    while True:
        try:
            # ---------- 风险检查 ----------
            current_balance = get_wallet_balance()
            if current_balance is None:
                time.sleep(POLL_INTERVAL)
                continue

            should_pause, risk_reason = check_risk_pause(state, current_balance)
            if should_pause:
                import time as time_module
                remaining = max(0, int((float(state["paused_until"]) - time_module.time())))
                logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 风险暂停中，剩余 {remaining} 秒 | {risk_reason}")
                time.sleep(POLL_INTERVAL)
                continue

            # ---------- 获取趋势榜 ----------
            trending = fetch_trending()
            if not trending:
                time.sleep(POLL_INTERVAL)
                continue

            # ---------- 应用过滤 ----------
            filtered = []
            for t in trending:
                passed, reason = apply_filters(t, state)
                rank = trending.index(t) + 1
                t["_rank"] = rank
                t["_pass"] = passed
                t["_reason"] = reason
                if passed:
                    filtered.append(t)

            if not filtered:
                logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 趋势榜为空，等待...")
                time.sleep(POLL_INTERVAL)
                continue

            # ---------- 榜一信号 ----------
            top1 = filtered[0]
            top5 = filtered[:5]
            top1_rank = top1["_rank"]

            logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 趋势榜 #{top1_rank} | {top1.get('symbol')} | "
                        f"24h {float(top1.get('priceChange24H',0)):+.1f}% | "
                        f"Top10 {float(top1.get('security',{}).get('topHolder',{}).get('value',0)):.1f}% | "
                        f"捆绑 {float(top1.get('auditInfo',{}).get('bundleHp',0)):.1f}% | 持币 {int(top1.get('holders',0) or 0)}")

            # ---------- 买入 filtered[0]，但需原始榜 top5 ----------
            if top1_rank <= 5:
                addr = top1.get("tokenAddress", "")
                sym = top1.get("symbol", "")
                state = load_state()

                if len(state["positions"]) >= MAX_POSITIONS:
                    logger.warning(f"仓位已满({MAX_POSITIONS})，暂停买入")
                else:
                    logger.info(f"🎯 买入信号! {sym} ({addr})")
                    ok, tx = swap(addr, is_buy=True)
                    if ok:
                        confirmed = wait_for_confirm(tx)
                        if confirmed:
                            state["bought"].append(addr)
                            state["positions"].append({
                                "address": addr,
                                "symbol": sym,
                                "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "tx": tx,
                                "buy_price": float(top1.get("priceUSD", 0)),
                            })
                            record_profit(state)  # 重置连续止损
                            save_state(state)
                            logger.info(f"✅ 买入成功 {sym} | TX: {tx}")
                        else:
                            logger.warning(f"⚠️ 交易未确认 {tx}")
                    else:
                        logger.error(f"❌ 买入失败 {sym}")

            # ---------- 检查持仓是否跌出前5 ----------
            state = load_state()
            current_addresses = [p["address"] for p in state["positions"]]
            top5_addresses = [t.get("tokenAddress") for t in top5]

            for pos in state["positions"][:]:
                addr = pos["address"]
                sym = pos.get("symbol", "")
                if addr not in top5_addresses:
                    logger.info(f"📤 {sym} 跌出前5，执行卖出!")
                    ok, tx = swap(addr, is_buy=False)
                    if ok:
                        confirmed = wait_for_confirm(tx)
                        # 检查这笔交易是盈利还是亏损
                        pnl_data = get_pnl(addr)
                        pnl_usd = float(pnl_data.get("pnlusd", 0) or 0)
                        if confirmed:
                            state["positions"].remove(pos)
                            if pnl_usd < 0:
                                record_stop_loss(state)
                            else:
                                record_profit(state)
                            save_state(state)
                            logger.info(f"{'✅ 卖出成功' if confirmed else '⚠️ 卖出未确认'} {sym} | PnL: {pnl_usd:+.2f} USDT")
                        else:
                            logger.warning(f"⚠️ 卖出未确认 {sym}")
                    else:
                        logger.error(f"❌ 卖出失败 {sym}")

            # ---------- 盈亏报告（每轮）----------
            print_pnl_report(state, current_balance)

        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
