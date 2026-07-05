import os
import sys
import json
import time
import hashlib
import discord
from discord.ext import commands
from discord import app_commands

# 嘗試載入加密套件（若正式環境要串藍新 API，需在 requirements.txt 加入 pycryptodome）
try:
    from Crypto.Cipher import Cipher, algorithms, modes
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ==============================================================================
# 1. 基礎設定與環境變數讀取 (針對 Railway 雲端環境優化)
# ==============================================================================
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID_RAW = os.getenv("ADMIN_ROLE_ID")

# 檢查必要的環境變數是否存在
if not TOKEN:
    print("❌ 錯誤：找不到環境變數 DISCORD_TOKEN。請在 Railway 的 Variables 頁面中設定。")
    sys.exit(1)
if not ADMIN_ROLE_ID_RAW:
    print("❌ 錯誤：找不到環境變數 ADMIN_ROLE_ID。請在 Railway 的 Variables 頁面中設定。")
    sys.exit(1)

try:
    ADMIN_ROLE_ID = int(ADMIN_ROLE_ID_RAW)
except ValueError:
    print("❌ 錯誤：環境變數 ADMIN_ROLE_ID 必須全部為數字。")
    sys.exit(1)

# 藍新金流測試設定（預留給未來真實串接使用）
NEWEBPAY_MERCHANT_ID = os.getenv("NEWEBPAY_MERCHANT_ID", "MS12345678")
NEWEBPAY_HASH_KEY = os.getenv("NEWEBPAY_HASH_KEY", "YOUR_HASH_KEY_1234567890123456")
NEWEBPAY_HASH_IV = os.getenv("NEWEBPAY_HASH_IV", "YOUR_HASH_IV_1234")

# ==============================================================================
# 2. 記憶體資料庫模擬 (儲存商品、購物車、訂單與營收)
# ==============================================================================
PRODUCTS = {
    "P001": {"name": "測試商品 A", "price": 100, "desc": "這是一項虛擬測試商品"},
    "P002": {"name": "進階會員卡", "price": 299, "desc": "開通一個月進階權限"}
}
CARTS = {}    # 格式: {user_id: {product_id: count}}
ORDERS = {}   # 格式: {order_id: {user_id: int, items: dict, total: int, status: str}}
REVENUE = 0   # 累計營收

# ==============================================================================
# 3. 藍新金流核心加密演算法 (AES/CBC/PKCS7 & SHA256)
# ==============================================================================
def aes_encrypt_cbc(data_str: str, key: str, iv: str) -> str:
    """將字串進行藍新金流規範的 AES CBC PKCS7 加密並轉為十六進位"""
    if not HAS_CRYPTO:
        # 若未安裝加密庫，返回模擬字串，不影響 Discord 介面運作
        return "SIMULATED_ENCRYPTED_DATA_FOR_TESTING"
    
    key_bytes = key.encode('utf-8')
    iv_bytes = iv.encode('utf-8')
    data_bytes = data_str.encode('utf-8')
    
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes))
    encryptor = cipher.encryptor()
    padded_data = pad(data_bytes, 32, style='pkcs7') # 藍新特殊規範通常採 32 bytes 區塊填充
    encrypted_bytes = encryptor.update(padded_data) + encryptor.finalize()
    return encrypted_bytes.hex().upper()

def generate_newebpay_trade_info(order_id: str, amount: int, desc: str, email: str):
    """建立藍新 MPG 金流所需的 TradeInfo 與 TradeSha"""
    # 串接參數字串 (包含藍新28天/即時支付所需的基本欄位)
    trade_info_param = (
        f"MerchantID={NEWEBPAY_MERCHANT_ID}&RespondType=JSON&TimeStamp={int(time.time())}"
        f"&Version=2.0&MerchantOrderNo={order_id}&Amt={amount}&ItemDesc={desc}"
        f"&Email={email}&LoginType=0&TradeLimit=2419200" # TradeLimit 設定 2419200 秒即為 28 天
    )
    
    trade_info = aes_encrypt_cbc(trade_info_param, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV)
    sha_str = f"HashKey={NEWEBPAY_HASH_KEY}&{trade_info}&HashIV={NEWEBPAY_HASH_IV}"
    trade_sha = hashlib.sha256(sha_str.encode('utf-8')).hexdigest().upper()
    
    return trade_info, trade_sha

# ==============================================================================
# 4. Bot 主程式初始化設定
# ==============================================================================
class MallBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 將斜線指令同步至全域伺服器
        await self.tree.sync()
        print("💡 [系統提示] 斜線指令已同步完成！")

bot = MallBot()

@bot.event
async def on_ready():
    print(f"🤖 [上線通知] 商城機器人已啟動！登入身分：{bot.user.name}")
    print(f"👑 [管理權限] 綁定的管理員身分組 ID 為: {ADMIN_ROLE_ID}")

# 權限檢查裝飾器：確保只有擁有指定管理員身分組的人可以執行
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        has_role = any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message("❌ 權限不足：你沒有管理員身分組，無法使用此指令！", ephemeral=True)
        return has_role
    return app_commands.check(predicate)

# ==============================================================================
# 5. 管理員專屬指令區 (彈出視窗、商品上架、後台數據)
# ==============================================================================

class AddProductModal(discord.ui.Modal, title="新增 / 修改商城商品"):
    pid = discord.ui.TextInput(label="商品編號 (如: P003)", placeholder="請輸入唯一的商品代碼", min_length=2, max_length=10)
    pname = discord.ui.TextInput(label="商品名稱", placeholder="請輸入要顯示的商品名稱", max_length=50)
    price = discord.ui.TextInput(label="商品價格 (TWD)", placeholder="請輸入正整數金額")
    desc = discord.ui.TextInput(label="商品描述 / 規格", style=discord.TextStyle.long, required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            p_price = int(self.price.value)
            if p_price <= 0:
                raise ValueError
                
            # 寫入或蓋掉原本的商品資訊
            PRODUCTS[self.pid.value] = {
                "name": self.pname.value,
                "price": p_price,
                "desc": self.desc.value if self.desc.value else "暫無描述。"
            }
            await interaction.response.send_message(
                f"✅ **商品上架成功！**\n"
                f"• 編號：`{self.pid.value}`\n"
                f"• 名稱：**{self.pname.value}**\n"
                f"• 價格：`${p_price} TWD`", 
                ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message("❌ 錯誤：商品價格必須是填入大於 0 的正整數！", ephemeral=True)

@bot.tree.command(name="add_product", description="[管理員] 使用彈出式面板手動上架或修改商品")
@is_admin()
async def add_product(interaction: discord.Interaction):
    await interaction.response.send_modal(AddProductModal())

@bot.tree.command(name="dashboard", description="[管理員] 直接在 Discord 內查看商城後台數據與記帳系統")
@is_admin()
async def dashboard(interaction: discord.Interaction):
    global REVENUE
    embed = discord.Embed(title="📊 小揖商城 ‧ 後台管理面板", color=discord.Color.gold())
    embed.add_field(name="💰 累計總營業額", value=f"**${REVENUE}** TWD", inline=False)
    embed.add_field(name="📦 上架商品總數", value=f"`{len(PRODUCTS)}` 項", inline=True)
    embed.add_field(name="📑 系統總訂單數", value=f"`{len(ORDERS)}` 筆", inline=True)
    
    # 擷取最後 5 筆訂單記錄進行渲染
    order_logs = ""
    for oid, odata in list(ORDERS.items())[-5:]:
        order_logs += f"• 訂單 `{oid}` | 買家: <@{odata['user_id']}> | 金額: `${odata['total']}` | 狀態: {odata['status']}\n"
    
    embed.add_field(name="📝 最新五筆訂單動態", value=order_logs if order_logs else "目前暫無任何訂單紀錄。", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==============================================================================
# 6. 買家專屬互動介面 (瀏覽、購物車、藍新金流模擬結帳)
# ==============================================================================

@bot.tree.command(name="store", description="瀏覽目前商城上架的所有商品清單")
async def store(interaction: discord.Interaction):
    if not PRODUCTS:
        await interaction.response.send_message("🏪 目前商城尚未上架任何商品！", ephemeral=True)
        return

    embed = discord.Embed(title="🏪 小揖線上商城 ‧ 商品目錄", description="請使用 `/add_to_cart` 指令將商品加入購物車結帳", color=discord.Color.blue())
    for pid, info in PRODUCTS.items():
        embed.add_field(
            name=f"📦 {info['name']} (編號: `{pid}`)", 
            value=f"**價格:** `${info['price']} TWD`\n**詳情:** {info['desc']}", 
            inline=False
        )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="add_to_cart", description="將指定的商品編號與數量加入您的購物車")
@app_commands.describe(product_id="請輸入商品編號 (例如: P001)", quantity="請輸入購買數量")
async def add_to_cart(interaction: discord.Interaction, product_id: str, quantity: int = 1):
    if product_id not in PRODUCTS:
        await interaction.response.send_message("❌ 錯誤：找不到此商品編號！請使用 `/store` 確認代碼。", ephemeral=True)
        return
    if quantity <= 0:
        await interaction.response.send_message("❌ 錯誤：購買數量必須大於 0！", ephemeral=True)
        return
    
    uid = interaction.user.id
    if uid not in CARTS:
        CARTS[uid] = {}
    
    CARTS[uid][product_id] = CARTS[uid].get(product_id, 0) + quantity
    await interaction.response.send_message(f"🛒 成功加入！**{PRODUCTS[product_id]['name']}** x{quantity} 已放入您的購物車。", ephemeral=True)

class CartView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="💳 模擬藍新金流結帳", style=discord.ButtonStyle.success)
    async def checkout(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in CARTS or not CARTS[uid]:
            await interaction.response.send_message("🛒 購物車目前是空的，無法進行結帳！", ephemeral=True)
            return
        
        # 計算帳單總金額
        total_amt = 0
        item_names = []
        for pid, qty in CARTS[uid].items():
            total_amt += PRODUCTS[pid]['price'] * qty
            item_names.append(f"{PRODUCTS[pid]['name']}x{qty}")
            
        order_id = f"ORD{int(time.time())}"
        item_desc = ", ".join(item_names)
        
        # 寫入訂單模擬庫
        ORDERS[order_id] = {
            "user_id": uid,
            "items": CARTS[uid].copy(),
            "total": total_amt,
            "status": "⏳ 藍新28天內未付款"
        }
        
        # 清空購物車
        CARTS[uid] = {}
        
        # 生成前端模擬藍新交易通知的 UI 視窗
        sim_view = SimulatePaymentView(order_id, total_amt)
        
        embed = discord.Embed(title="🧾 藍新 MPG 金流訂單已建立", color=discord.Color.orange())
        embed.add_field(name="訂單流水號", value=f"`{order_id}`", inline=True)
        embed.add_field(name="應付總金額", value=f"**${total_amt}** TWD", inline=True)
        embed.add_field(name="商品明細", value=item_desc, inline=False)
        embed.description = "系統已透過藍新金流協定封裝加密參數。\n請點擊下方按鈕，模擬買家前往藍新頁面並於期限內完成支付："
        
        await interaction.response.send_message(embed=embed, view=sim_view, ephemeral=True)
        self.stop()

class SimulatePaymentView(discord.ui.View):
    def __init__(self, order_id, amount):
        super().__init__(timeout=120)
        self.order_id = order_id
        self.amount = amount

    @discord.ui.button(label="👍 模擬付款成功 (觸發藍新 Webhook)", style=discord.ButtonStyle.primary)
    async def success_pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        global REVENUE
        if ORDERS[self.order_id]["status"] == "✅ 已完成付款":
            await interaction.response.send_message("⚠️ 提示：這筆訂單先前已收到藍新回傳的扣款成功訊號！", ephemeral=True)
            return
            
        # 更新狀態，並撥入營收
        ORDERS[self.order_id]["status"] = "✅ 已完成付款"
        REVENUE += self.amount
        
        await interaction.response.send_message(
            f"🎉 **支付對帳成功！**\n"
            f"模擬藍新金流後台已向本機器人發送 `NotifyURL` 成功扣款訊號。\n"
            f"訂單 `{self.order_id}` 狀態已更新為 **已付款**，後台已同步記帳！", 
            ephemeral=True
        )
        self.stop()

@bot.tree.command(name="cart", description="查看您個人購物車內的商品、總金額並進行結帳")
async def view_cart(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in CARTS or not CARTS[uid]:
        await interaction.response.send_message("🛒 您的購物車內目前沒有任何商品！", ephemeral=True)
        return
        
    embed = discord.Embed(title="🛒 您的個人購物車明細", color=discord.Color.green())
    total = 0
    for pid, qty in CARTS[uid].items():
        p_info = PRODUCTS[pid]
        subtotal = p_info['price'] * qty
        total += subtotal
        embed.add_field(name=p_info['name'], value=f"數量: `{qty}`\n小計: `${subtotal} TWD`", inline=True)
        
    embed.add_field(name="💰 當前預計總結帳金額", value=f"**${total} TWD**", inline=False)
    
    view = CartView(user_id=uid)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ==============================================================================
# 7. 啟動機器人
# ==============================================================================
if __name__ == "__main__":
    bot.run(TOKEN)
