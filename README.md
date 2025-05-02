# LINE待辦事項機器人 (Todo Bot)

這是一個用於LINE群組的待辦事項管理機器人，支持任務的新增、修改、刪除、完成等功能，以及定期任務、優先級管理等進階功能。

## 主要功能

- 🔸 任務管理:
  - 新增單一任務，支持優先級和截止日期
  - 批量新增多個任務
  - 新增定期重複任務（每週、每月、每年）
  - 查看任務詳情
  - 修改任務內容、優先級和截止日期
  - 完成任務
  - 刪除任務
  - 取消定期任務
  - 查看任務列表（全部或指定成員）

- 🔸 其他功能:
  - 擲筊功能（提供問題，得到聖筊/陰筊/笑筊結果）
  - 隨機抽籤功能（從多個選項中隨機選擇）

## 如何使用

### 基本指令

```text
🔸 任務管理:
   #新增 @成員 [!優先級] 內容 [YYYY/MM/DD]
     (優先級可為 !低、!普通、!高)
     (截止日可選)
   #批量新增 @成員
     [!優先級] 任務1 [YYYY/MM/DD]
     [!優先級] 任務2 [YYYY/MM/DD]
     (每行一個任務，優先級、日期可選)
   #定期 @成員 [!優先級] 內容 每週一
     (週一至週日、月DD日、年MM月DD日)
   #取消定期 T-ID
   #完成 T-ID
   #列表 [@成員]
     (成員可選，預設列全部)
   #修改 T-ID [!優先級] 新內容 [YYYY/MM/DD]
     (優先級、截止日可選)
   #刪除 T-ID
   #詳情 T-ID

🔸 其他功能:
   #擲筊 問題
   #抽籤 選項1 選項2 ...
   #幫助 (顯示本說明)
```

## 如何部署在Replit上

1. 在Replit中創建一個新的Python專案
2. 將本專案的所有文件上傳到Replit
3. 在Replit的Secrets設置以下環境變量:
   - `LINE_CHANNEL_ACCESS_TOKEN`: LINE機器人的Channel Access Token
   - `LINE_CHANNEL_SECRET`: LINE機器人的Channel Secret
   - `LINE_GROUP_ID`: 預設群組ID (可選)
   - `API_KEY`: 用於API調用的金鑰
   - `PGUSER`: PostgreSQL用戶名
   - `PGPASSWORD`: PostgreSQL密碼
   - `PGHOST`: PostgreSQL主機地址
   - `PGDATABASE`: PostgreSQL數據庫名稱
   - `PGPORT`: PostgreSQL端口 (預設5432)

4. 點擊Run按鈕啟動應用
5. 使用Replit提供的URL作為LINE Bot的Webhook URL (需要加上 `/callback` 路徑)

## 定期任務自動生成

定期任務需要設置一個外部觸發器來定期調用API:

```http
POST /api/generate-recurring-tasks
Headers: 
  X-API-KEY: your_api_key
```

可以使用Replit的Scheduled Jobs或其他定時任務服務來每天觸發此API。
