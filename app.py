# app.py (Compatible with line-bot-sdk v2.x)
from flask import Flask, request, abort, jsonify
import os
import json
import random
import re
from datetime import datetime, timedelta

# v2.x Imports
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, # Core message types
    FlexSendMessage # Flex message for v2
    # JoinEvent is not needed in this version
)

# Assuming your models.py is compatible or correctly imported
from models import init_db, Task, Member, get_db_connection
import logging
from dotenv import load_dotenv

# --- Application Initialization ---
app = Flask(__name__)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- LINE API Configuration (v2 style) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
TARGET_GROUP_ID = os.environ.get('LINE_GROUP_ID') # Pre-configured Group ID
N8N_API_KEY = os.environ.get('API_KEY', 'default_key')

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.error("環境變數 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET 未設定")
    exit(1)
if not TARGET_GROUP_ID:
    logger.warning("環境變數 LINE_GROUP_ID 未設定。n8n 推播等功能可能無法指定預設群組。")

# v2 API Initialization
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- Database Initialization ---
init_db()

# --- Regex Patterns (Keep as is) ---
ADD_TASK_PATTERN = r'#新增\s+@(\S+)\s+(.+?)\s+(\d{4}/\d{1,2}/\d{1,2})?$'
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'

# --- Flask Routes ---

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook Callback Handler"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.info(f"Request body: {body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        logger.exception(f"處理回調時發生未預期錯誤: {str(e)}")
        abort(500)
    return 'OK'

@app.route("/ping", methods=['GET'])
def ping():
    """Health Check Endpoint"""
    return jsonify({
        "status": "ok",
        "message": "LINE Bot is running (v2 SDK)",
        "timestamp": datetime.now().isoformat()
    })

# --- LINE Event Handlers ---

@handler.add(MessageEvent, message=TextMessage) # Use TextMessage for v2
def handle_text_message(event):
    """Handles incoming text messages"""
    text = event.message.text
    reply_token = event.reply_token
    user_id = event.source.user_id
    group_id = None

    if event.source.type == 'group':
        group_id = event.source.group_id
    elif event.source.type == 'room':
        # Optional: handle room messages if needed
        pass

    if not group_id:
        logger.info(f"Ignoring message from non-group source (User ID: {user_id})")
        return

    logger.info(f"Received message from Group ID {group_id}: {text}")

    try:
        if re.match(ADD_TASK_PATTERN, text):
            handle_add_task(reply_token, text, group_id)
        elif re.match(COMPLETE_TASK_PATTERN, text):
            handle_complete_task(reply_token, text)
        elif re.match(LIST_TASK_PATTERN, text):
            handle_list_tasks(reply_token, text, group_id)
        elif re.match(DRAW_LOTS_PATTERN, text):
            handle_draw_lots(reply_token, text)
        elif re.match(RANDOM_PICK_PATTERN, text):
            handle_random_pick(reply_token, text)
        elif text == "#幫助":
            send_help_message(reply_token)
    except Exception as e:
        logger.exception(f"處理指令 '{text}' 時發生錯誤: {str(e)}")
        try:
            # Use v2 reply_message syntax
            line_bot_api.reply_message(
                reply_token,
                messages=[TextMessage(text="處理指令時發生內部錯誤，請稍後再試。")]
            )
        except Exception as reply_err:
            logger.error(f"回覆錯誤訊息時也發生錯誤: {str(reply_err)}")

# --- Command Handling Functions (Using v2 SDK syntax) ---

def handle_add_task(reply_token, text, group_id):
    """Handles add task command"""
    match = re.match(ADD_TASK_PATTERN, text)
    if match:
        member_name = match.group(1)
        task_content = match.group(2)
        due_date_str = match.group(3)
        due_date = None

        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, "%Y/%m/%d")
            except ValueError:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式"))
                return

        member = Member.get_by_name_and_group(member_name, group_id)
        if not member:
            member = Member(name=member_name, group_id=group_id)
            member.save()

        task = Task(member_id=member.id, content=task_content, status='pending', due_date=due_date)
        task.save()
        task_id = f"T-{task.id}"

        reply_text = f"已為 {member_name} 新增任務：{task_content}\n任務ID：{task_id}\n"
        reply_text += (f"截止日期：{due_date_str}" if due_date else "無截止日期")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_complete_task(reply_token, text):
    """Handles complete task command"""
    match = re.match(COMPLETE_TASK_PATTERN, text)
    if match:
        task_id_num = int(match.group(1))
        task = Task.get_by_id(task_id_num)
        reply_text = ""

        if not task:
            reply_text = f"找不到ID為 T-{task_id_num} 的任務"
        elif task.status == 'completed':
            reply_text = f"任務 T-{task_id_num} 已經標記為完成"
        else:
            member = Member.get_by_id(task.member_id)
            task.status = 'completed'
            task.completed_at = datetime.now()
            task.save()
            reply_text = f"已將 {member.name if member else '未知成員'} 的任務 T-{task_id_num} 標記為完成！\n任務內容：{task.content}"

        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_list_tasks(reply_token, text, group_id):
    """Handles list tasks command"""
    match = re.match(LIST_TASK_PATTERN, text)
    if match:
        member_name = match.group(1)
        tasks = []
        title = ""

        if member_name:
            member = Member.get_by_name_and_group(member_name, group_id)
            if not member:
                line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到成員：{member_name}"))
                return
            tasks = Task.get_by_member_id(member.id, 'pending')
            title = f"{member_name} 的待辦事項"
        else:
            tasks = Task.get_by_group_id(group_id, 'pending')
            title = "本群組待辦事項"

        if not tasks:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"{title}：目前沒有待辦任務"))
            return

        try:
            # Assume create_task_list_bubble returns the correct dictionary structure
            bubble_json = create_task_list_bubble(title, tasks)
            # Use v2 FlexSendMessage structure
            flex_message = FlexSendMessage(alt_text=title, contents=bubble_json)
            line_bot_api.reply_message(reply_token, messages=[flex_message]) # Pass as a list
        except Exception as e:
            logger.exception(f"創建或發送 Flex 消息失敗: {str(e)}")
            task_list_text = create_task_list_text(title, tasks)
            line_bot_api.reply_message(reply_token, TextSendMessage(text=task_list_text))


def create_task_list_bubble(title, tasks):
    """Creates Flex Message dictionary (same structure as before)"""
    # --- This function's internal logic remains the same as it just returns a dictionary ---
    # --- Ensure Member.get_by_id and date handling inside work correctly ---
    contents = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": title,
                    "weight": "bold",
                    "size": "lg"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [] # Will be populated below
        }
    }
    for task in tasks:
        member = Member.get_by_id(task.member_id)
        task_header = {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1},
                {"type": "text", "text": member.name if member else '?', "size": "sm", "color": "#1DB446", "align": "end"}
            ]
        }
        task_content_text = {
            "type": "text",
            "text": task.content,
            "wrap": True,
            "weight": "bold",
            "margin": "sm"
        }
        task_box_contents = [task_header, task_content_text]
        if task.due_date:
            try:
                if isinstance(task.due_date, str):
                    due_date_obj = datetime.fromisoformat(task.due_date)
                else:
                    due_date_obj = task.due_date # Assume it's already datetime
                days_left = (due_date_obj - datetime.now()).days
                color = "#FF5555" if days_left < 0 else ("#FFAA00" if days_left < 2 else "#888888")
                due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                status_text = f"({days_left}天)" if days_left >= 0 else "(已逾期)"
                due_date_text_el = {
                    "type": "text",
                    "text": f"截止: {due_date_str_display} {status_text}",
                    "size": "xs",
                    "color": color,
                    "margin": "sm"
                }
                task_box_contents.append(due_date_text_el)
            except Exception as date_err:
                logger.error(f"處理任務 T-{task.id} 的截止日期時出錯 (Flex): {date_err}")

        complete_button = {
            "type": "button",
            "style": "primary", "color": "#DDDDDD", "height": "sm", "margin": "md",
            "action": {"type": "message", "label": "標記完成", "text": f"#完成 T-{task.id}"}
        }
        task_box_contents.append(complete_button)
        contents["body"]["contents"].append({
            "type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "md",
            "backgroundColor": "#FAFAFA", "cornerRadius": "md", "contents": task_box_contents
        })
    return contents


def create_task_list_text(title, tasks):
    """Creates fallback text message (same logic as before)"""
    # --- This function's internal logic remains the same ---
    # --- Ensure Member.get_by_id and date handling inside work correctly ---
    result = f"📋 {title} 📋\n\n"
    for i, task in enumerate(tasks, 1):
        member = Member.get_by_id(task.member_id)
        result += f"【任務 T-{task.id}】\n"
        result += f"👤 負責人: {member.name if member else '未知成員'}\n"
        result += f"📝 內容: {task.content}\n"
        if task.due_date:
            try:
                if isinstance(task.due_date, str):
                    due_date_obj = datetime.fromisoformat(task.due_date)
                else:
                    due_date_obj = task.due_date # Assume it's already datetime
                days_left = (due_date_obj - datetime.now()).days
                due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                status = ("⚠️ 已逾期" if days_left < 0 else
                          "⚠️ 今天到期" if days_left == 0 else
                          f"⚠️ 即將到期 ({days_left}天)" if days_left < 2 else
                          f"還有 {days_left} 天")
                result += f"📅 截止: {due_date_str_display} {status}\n"
            except Exception as date_err:
                 logger.error(f"處理任務 T-{task.id} 的截止日期時出錯 (Text): {date_err}")
        result += f"✅ 輸入「#完成 T-{task.id}」標記完成\n"
        if i < len(tasks):
            result += "\n" + "-" * 25 + "\n\n"
    return result


def handle_draw_lots(reply_token, text):
    """Handles draw lots command"""
    match = re.match(DRAW_LOTS_PATTERN, text)
    if match:
        question = match.group(1)
        results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]
        result = random.choice(results)
        reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_random_pick(reply_token, text):
    """Handles random pick command"""
    match = re.match(RANDOM_PICK_PATTERN, text)
    if match:
        options_text = match.group(1)
        options = [opt.strip() for opt in options_text.split() if opt.strip()]
        if not options:
            reply_text = "請提供至少一個抽籤選項！ (用空格分隔)"
        else:
            chosen = random.choice(options)
            reply_text = f"從 [{', '.join(options)}] {len(options)} 個選項中抽出：\n🎉 {chosen} 🎉"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def send_help_message(reply_token):
    """Sends help message"""
    help_text = (
        "📋 代辦事項機器人指令 (v2 SDK) 📋\n\n" # Added SDK marker for clarity
        "🔸 新增任務:\n"
        "   #新增 @成員 任務內容 [YYYY/MM/DD]\n"
        "   (截止日期可選)\n"
        "   例: #新增 @小明 買晚餐 2025/04/17\n\n"
        "🔸 完成任務:\n"
        "   #完成 T-任務ID\n"
        "   例: #完成 T-12\n\n"
        "🔸 查看任務:\n"
        "   #列表          (看本群組全部待辦)\n"
        "   #列表 @成員   (看指定成員待辦)\n\n"
        "🔸 其他功能:\n"
        "   #擲筊 問題\n"
        "   #抽籤 選項1 選項2 ...\n"
        "   #幫助 (顯示本說明)"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))


# --- n8n Integration API Endpoints (Using v2 SDK for push) ---

@app.route("/api/pending-tasks", methods=['GET'])
def api_pending_tasks():
    """API Endpoint: Get pending tasks for the default group"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        logger.warning(f"未經授權的 API 請求 /api/pending-tasks (Key: {api_key})")
        return jsonify({"error": "Unauthorized"}), 401
    if not TARGET_GROUP_ID:
        logger.error("API 錯誤：環境變數 LINE_GROUP_ID 未設定")
        return jsonify({"error": "Target Group ID is not configured on the server."}), 500
    try:
        tasks = Task.get_by_group_id(TARGET_GROUP_ID, 'pending')
        result = []
        for task in tasks:
            member = Member.get_by_id(task.member_id)
            due_date_str, days_left = None, None
            if task.due_date:
                 try:
                    if isinstance(task.due_date, str):
                        due_date_obj = datetime.fromisoformat(task.due_date)
                    else:
                        due_date_obj = task.due_date
                    due_date_str = due_date_obj.strftime('%Y/%m/%d')
                    days_left = (due_date_obj - datetime.now()).days
                 except Exception: due_date_str = "日期格式錯誤" # Simplified error handle
            result.append({
                "id": task.id, "task_id": f"T-{task.id}",
                "member": member.name if member else '?', "content": task.content,
                "due_date": due_date_str, "days_left": days_left,
                "created_at": task.created_at.isoformat() if task.created_at and hasattr(task.created_at, 'isoformat') else None
            })
        return jsonify({ "tasks": result, "count": len(result), "group_id": TARGET_GROUP_ID })
    except Exception as e:
        logger.exception(f"獲取待辦任務 API (/api/pending-tasks) 發生錯誤: {str(e)}")
        return jsonify({"error": "Internal server error fetching tasks."}), 500


@app.route("/api/send-reminder", methods=['POST'])
def api_send_reminder():
    """API Endpoint: Send reminder message to the default group"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        logger.warning(f"未經授權的 API 請求 /api/send-reminder (Key: {api_key})")
        return jsonify({"error": "Unauthorized"}), 401
    if not TARGET_GROUP_ID:
        logger.error("API 錯誤：環境變數 LINE_GROUP_ID 未設定")
        return jsonify({"error": "Target Group ID is not configured on the server."}), 500
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({"error": "Missing 'message' in request body"}), 400
    message = data['message']
    try:
        # Use v2 push_message syntax
        line_bot_api.push_message(
            TARGET_GROUP_ID,
            messages=[TextMessage(text=message)] # Pass message as a list
        )
        logger.info(f"已成功透過 API 發送提醒至 Group ID: {TARGET_GROUP_ID}")
        return jsonify({"success": True, "message": "Reminder sent successfully"})
    except Exception as e:
        logger.exception(f"透過 API 發送提醒訊息時發生錯誤: {str(e)}")
        return jsonify({"success": False, "error": f"Failed to send reminder: {str(e)}"}), 500

# --- Main Execution Block ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    # For production, use Waitress or Gunicorn, e.g.,
    # waitress-serve --host=0.0.0.0 --port=5000 app:app
    # For development:
    app.run(host='0.0.0.0', port=port, debug=False) # Keep debug=False unless actively debugging