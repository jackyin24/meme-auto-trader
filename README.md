# Meme 自动跟单策略

XXYY 趋势榜自动跟单工具，Python 编写。

## 功能

- 每 5 秒监控 XXYY `trending-list`（1分钟热门榜）
- 7 项过滤条件全部满足 → 自动买入 0.01 BNB
- 持仓代币跌出前5 → 自动卖出
- 风险控制：总亏损≥30%暂停、连续3次止损暂停60分钟

## 过滤条件

| 条件 | 阈值 |
|------|------|
| 发射平台 | four.meme |
| Top10 持仓 | ≤20% |
| 捆绑持仓 | ≤20% |
| 新钱包持仓 | ≤20% |
| 24h 涨跌 | >-50% |
| 持币人数 | >50 |
| 代币年龄 | ≤30min |

## 配置

```bash
# 1. 克隆
git clone https://github.com/jackyin24/meme-auto-trader.git
cd meme-auto-trader

# 2. 编辑脚本，填入你的信息
# XXYY_API_KEY = "你的API Key"
# WALLET_ADDRESS = "你的钱包地址"

# 3. 运行
python3 meme_auto_trader_clean.py
```

## 状态文件

`meme_trader_state.json` 会自动生成，**不要上传到 GitHub**（已在 .gitignore 中忽略）。
