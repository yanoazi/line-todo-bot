# LINE 待辦事項機器人 (LINE To-Do Bot)

一個運行在 LINE 群組中，協助成員管理待辦事項、進行簡單抽籤和擲筊的 Python 機器人。整合 n8n 可進行自動提醒，並計畫整合 OpenAI 進行自然語言處理。

## ✨ 功能特色

* **待辦事項管理 (使用 `#` 指令)：**
    * `#新增 @成員 內容 [YYYY/MM/DD]`: 新增任務給指定成員 (日期可選)。
    * `#完成 T-ID`: 將指定 ID 的任務標記為完成。
    * `#列表 [@成員]`: 列出群組全部或指定成員的待辦任務。
    * `#修改 T-ID 新內容 [YYYY/MM/DD]`: 修改任務內容或日期。
    * `#刪除 T-ID`: 刪除指定任務。
    * `#詳情 T-ID`: 查看任務詳細資訊。
* **小工具：**
    * `#擲筊 問題`: 進行擲筊。
    * `#抽籤 選項1 選項2 ...`: 從選項中隨機抽取。
* **自動提醒 (需搭配 n8n)：**
    * 透過 n8n Workflow 定時發送待辦事項提醒。
    * (可選) 整合 OpenAI 產生更智慧的提醒文字。
* **幫助指令：**
    * `#幫助`: 顯示所有可用指令。
* **(計畫中) 自然語言處理：**
    * 未來計畫整合 OpenAI，允許使用自然語言新增任務。

## 🚀 技術棧

* **後端：** Python, Flask
* **資料庫：** PostgreSQL (使用 SQLAlchemy ORM)
* **LINE Bot SDK：** line-bot-sdk-python (v2.x)
* **部署平台：** Render (Web Service + PostgreSQL Free Tier)
* **自動化：** n8n (用於排程提醒)
* **(可選) AI：** OpenAI API

## ⚙️ 設定與安裝 (供參考)

1.  **Clone Repository:**
    ```bash
    git clone [https://github.com/yanoazi/line-todo-bot.git](https://github.com/yanoazi/line-todo-bot.git)
    cd line-todo-bot
    ```
2.  **建立虛擬環境:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # Linux/macOS
    # venv\Scripts\activate  # Windows
    ```
3.  **安裝依賴:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **設定環境變數:**
    * 建立一個 `.env` 檔案在專案根目錄。
    * 填入以下必要的環境變數
        ```dotenv
        LINE_CHANNEL_ACCESS_TOKEN=xxx
        LINE_CHANNEL_SECRET=xxx
        DATABASE_URL=postgres://user:password@host:port/dbname  # PostgreSQL 連線 URL
        TARGET_GROUP_ID=Cxxxxxxxxxxxxxx # n8n 預設提醒的群組 ID
        API_KEY=YourSecretKeyForN8nAccess # 給 n8n 呼叫 API 用的 Key
        # OPENAI_API_KEY=sk-xxxxxxxxxxxx # 如果未來使用 OpenAI
        ```
5.  **初始化資料庫:** (首次執行或資料庫表格不存在時)
    * 程式啟動時 (`app.py` 中的 `init_db()`) 會嘗試自動建立表格。
6.  **執行應用程式 (本地測試):**
    ```bash
    python app.py
    ```
    * (需要搭配 ngrok 等工具接收 LINE Webhook)

## 部署到 Render

本專案已設定為可直接部署到 Render：
1.  在 Render 建立一個 PostgreSQL (Free) 服務。
2.  在 Render 建立一個 Web Service，連接到此 GitHub Repository。
3.  設定 Build Command: `pip install -r requirements.txt`
4.  設定 Start Command: `gunicorn app:app`
5.  在 Render Web Service 的 Environment Variables 中設定上面提到的所有環境變數 (使用 Internal Database URL)。
6.  將 Render 提供的 Web Service URL (`https://your-app-name.onrender.com`) 加上 `/callback` 設定到 LINE Developer Console 的 Webhook URL。

## 📝 使用方法

將 LINE Bot 加入您的群組後，即可透過輸入 `#` 開頭的指令與機器人互動。輸入 `#幫助` 查看完整指令列表。

## 📄 授權 (License)

本專案採用 MIT 授權條款。詳情請參閱 [LICENSE](LICENSE) 檔案。
