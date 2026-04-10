# 簡易抽獎 (Discord Giveaway Bot)

一個使用 Python 與 discord.py 製作的 Discord 抽獎機器人。

這個專案主打「可視化操作」與「資格門檻完整」，透過 Slash Commands 與按鈕面板就能完成建立抽獎、資格驗證、提前結束、重抽等流程。

## 官方機器人邀請

不想自己部署的話，可以直接邀請官方機器人：

- [點我邀請官方機器人](https://discord.com/oauth2/authorize?client_id=1438829305420578938&scope=bot%20applications.commands&permissions=274878221312)

## 目錄

- [官方機器人邀請](#官方機器人邀請)
- [專案特色](#專案特色)
- [功能總覽](#功能總覽)
- [技術棧與需求](#技術棧與需求)
- [快速開始](#快速開始)
- [Discord Bot 設定步驟](#discord-bot-設定步驟)
- [環境變數](#環境變數)
- [執行方式](#執行方式)
- [Slash 指令說明](#slash-指令說明)
- [抽獎資格規則](#抽獎資格規則)
- [資料儲存](#資料儲存)
- [專案結構](#專案結構)
- [開源與安全建議](#開源與安全建議)
- [常見問題 (FAQ)](#常見問題-faq)
- [開發與貢獻](#開發與貢獻)
- [授權](#授權)

## 專案特色

- 使用 Slash Command，管理者不需要記憶複雜參數。
- 使用互動面板建立抽獎，支援預覽與分段設定。
- 支援進階資格條件：
  - 指定必須說過某句話
  - 指定最少入群天數
  - 指定抽獎期間最少發言數
  - 指定必要/排除身分組
  - 指定跨群組加入資格
- 參加者以按鈕加入，可再次點擊進入「離開抽獎」流程。
- 抽獎結束後自動公告結果，並私訊通知主辦與得獎者。
- 支援提前結束 (`/gend`) 與重抽 (`/greroll`)。
- 具備 JSON 持久化、原子寫入、訊息更新節流，降低資料毀損與 API 壓力。

## 功能總覽

### 建立抽獎流程

1. 管理者執行 `/gstart`。
2. 在互動面板中設定：
   - 基本資料（獎品、時間、名額、附圖、自訂訊息）
   - 資格條件（指定訊息、最少入群天數、最少發言數）
   - 身分組條件（必要/排除）
   - 跨群資格（必須加入的群組 ID）
   - 發布頻道（可選文字頻道、討論串、Forum）
3. 送出後機器人發布抽獎卡片，參加者可按按鈕加入。

### 抽獎期間

- 機器人即時記錄參加名單。
- 若有設定資格門檻，會在參加與結算時驗證資格。
- 名單面板可顯示目前參加者與資格狀態。

### 抽獎結束

- 到期自動抽獎，或由管理者提前結束。
- 若無符合資格者，會標示「無人符合資格」。
- 抽獎訊息會切換為「已結束」狀態卡。

## 技術棧與需求

- Python 3.10+
- discord.py（需支援 `LayoutView` / `Container` 元件）
- python-dotenv

建議建立虛擬環境再安裝套件。

## 快速開始

### 1) 下載專案

```bash
git clone https://github.com/kusanagi-akane/Giveaway.git
cd Giveaway
```

### 2) 建立虛擬環境

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) 安裝依賴

```bash
pip install -U pip
pip install -U discord.py python-dotenv
```

## Discord Bot 設定步驟

1. 到 Discord Developer Portal 建立 Application。
2. 在 `Bot` 頁籤建立 Bot，複製 Token。
3. 啟用必要 Privileged Gateway Intents：
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT`
4. 透過 OAuth2 邀請 Bot 進伺服器。
   - Scopes: `bot`, `applications.commands`
   - Bot 權限建議至少包含：
     - View Channels
     - Send Messages
     - Read Message History
     - Embed Links
     - Use Slash Commands
     - Manage Threads（若要發在 Forum）
     - Send Messages in Threads

> 注意：若使用「跨群資格」功能，Bot 必須同時存在於所有被要求加入的群組中，否則無法驗證該條件。

## 環境變數

本專案使用 `.env`，可由 `.env.example` 建立：

```bash
cp .env.example .env
```

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
```

目前需要的變數：

```env
TOKEN=your_discord_bot_token_here
```

## 執行方式

```bash
python main.py
```

啟動成功後，Bot 會：

- 載入本地 `giveaways.json`（若存在）
- 還原仍在進行中的抽獎倒數任務
- 同步 Slash Commands

## Slash 指令說明

以下指令都限制「管理伺服器 (Manage Guild)」權限：

| 指令 | 作用 | 備註 |
| --- | --- | --- |
| `/gstart` | 開啟抽獎建立面板 | 使用互動介面配置抽獎 |
| `/glist` | 查看目前進行中抽獎 | 顯示訊息 ID、獎品、頻道、剩餘時間 |
| `/gend message_id` | 提前結束抽獎 | `message_id` 支援自動完成 |
| `/greroll message_id winners` | 重抽已結束/進行中的抽獎 | 會排除已中獎者；`winners` 範圍 1~50 |

## 抽獎資格規則

參加者按下「加入抽獎」後，系統會檢查以下條件：

1. 必要身分組：至少擁有其中一個指定角色。
2. 排除身分組：不得擁有任一排除角色。
3. 跨群資格：必須加入所有指定群組。
4. 入群天數：加入目前伺服器需達指定天數。
5. 指定訊息：需在抽獎期間傳送完全符合的文字。
6. 最少發言數：抽獎期間訊息數需達門檻。

補充：

- 指定訊息預設為「大小寫不敏感」比對。
- 目前比對模式為 `equals`（完全相同），可在程式常數調整。
- 參加人數與合格人數會顯示在抽獎卡上。

## 資料儲存

- 檔案：`giveaways.json`
- 格式：以 `message_id` 為 key 的 JSON 物件
- 寫入策略：先寫暫存檔再替換（原子寫入）

儲存內容包含：

- 抽獎基本資訊（獎品、名額、主持人、開始/結束時間）
- 條件設定（角色、跨群、發言、指定訊息等）
- 參加者與得獎者資料
- 歷史抽獎（已結束）


## 專案結構

```text
.
├─ main.py           # Bot 主程式（指令、互動 UI、抽獎邏輯）
├─ giveaways.json    # 抽獎狀態資料（執行時產生）
├─ .env.example      # 環境變數範本
└─ .gitignore
```

### 隨機性說明

本專案使用 Python 內建 `random.sample` 抽取得獎者，適合一般社群活動；不屬於密碼學等級隨機。

## 常見問題 (FAQ)

### 1) 指令沒有出現？

- 確認 Bot 已上線且有 `applications.commands` scope。
- 確認 `setup_hook` 已成功同步指令（查看啟動 log）。
- 新增指令後可能需要一點同步時間。

### 2) 為什麼有人按加入卻不符合資格？

- 可能缺少必要身分組。
- 可能有排除身分組。
- 可能未達入群天數或發言數。
- 若有跨群限制，可能未加入指定群組或 Bot 不在該群組。

### 3) Bot 重啟後抽獎會消失嗎？

不會。進行中的抽獎會從 `giveaways.json` 還原，並重新掛回倒數結束任務。

### 4) 為什麼抽獎卡不會每秒更新？

為了避免 API 過度編輯與速率限制，程式有做訊息同步節流與去抖動。


## 授權

本專案採用 MIT License。

完整授權內容請見 [LICENSE](LICENSE)。
