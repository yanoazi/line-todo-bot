# app.py (SQLAlchemy + New Features - v2 SDK compatible)
from flask import Flask, request, abort, jsonify
import os
import json
import random
import re
from typing import List, Optional
from datetime import datetime, timezone # Import timezone
import logging
from dotenv import load_dotenv

# --- Database Imports (SQLAlchemy) ---
# Assuming models.py is in the same directory
from models import (
    init_db, get_db, Member, Task,
    get_member_by_name_and_group, get_member_by_id, get_task_by_id,
    get_pending_tasks_by_member_id, get_pending_tasks_by_group_id,
    create_member, create_task # Import necessary helpers
)
from sqlalchemy.orm import Session # Import Session for type hinting

# --- LINE SDK Imports (v2) ---
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
)

# --- Standard Python Imports ---
from typing import Optional # For type hinting

# --- Application Initialization ---
app = Flask(__name__)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configuration Loading ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
TARGET_GROUP_ID = os.environ.get('LINE_GROUP_ID')
N8N_API_KEY = os.environ.get('API_KEY', 'default_key')
DATABASE_URL = os.environ.get('DATABASE_URL') # Loaded by models.py too, but good to have here
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY') # Prepare for future use

# --- Configuration Checks ---
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.error("環境變數 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET 未設定")
    exit(1)
if not TARGET_GROUP_ID:
    logger.warning("環境變數 LINE_GROUP_ID 未設定。n8n 推播等功能可能無法指定預設群組。")
if not DATABASE_URL:
    logger.error("環境變數 DATABASE_URL 未設定！應用程式無法連接資料庫。")
    # exit(1) # Or handle differently, maybe allow startup but fail on DB access
if not OPENAI_API_KEY:
    logger.warning("環境變數 OPENAI_API_KEY 未設定。未來 OpenAI 功能將無法使用。")


# --- LINE API Initialization (v2) ---
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- Database Initialization ---
# Call init_db on startup to ensure tables exist in PostgreSQL
# SQLAlchemy's create_all is safe to call multiple times
init_db()

# --- Regex Patterns (Added new commands) ---
ADD_TASK_PATTERN = r'#新增\s+@(\S+)\s+(.+?)\s+(\d{4}/\d{1,2}/\d{1,2})?$'
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DELETE_TASK_PATTERN = r'#刪除\s+T-(\d+)$' # New pattern for delete
EDIT_TASK_PATTERN = r'#修改\s+T-(\d+)\s+(.+?)\s*(\d{4}/\d{1,2}/\d{1,2})?$' # New pattern for edit (content mandatory, date optional)
DETAIL_TASK_PATTERN = r'#詳情\s+T-(\d+)$' # New pattern for details
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'


# --- Flask Routes ---

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook Callback Handler"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    # logger.info(f"Request body: {body}") # Log body only if needed for debugging

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
    # Check basic DB connection as part of health check
    db_ok = False
    try:
        with get_db() as db:
            # Simple query to check connection
            db.execute("SELECT 1")
            db_ok = True
    except Exception as e:
        logger.error(f"Ping DB check failed: {e}")

    return jsonify({
        "status": "ok",
        "message": "LINE Bot is running (v2 SDK + SQLAlchemy)",
        "timestamp": datetime.now().isoformat(),
        "db_connection": "ok" if db_ok else "error"
    })


# --- LINE Event Handlers ---

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """Handles incoming text messages"""
    text = event.message.text.strip() # Add strip() to remove leading/trailing whitespace
    reply_token = event.reply_token
    user_id = event.source.user_id # Sender's LINE User ID
    group_id = None

    if event.source.type == 'group':
        group_id = event.source.group_id
    # Add handling for room if needed later
    # elif event.source.type == 'room':
    #     group_id = event.source.room_id

    if not group_id:
        logger.info(f"Ignoring message from non-group/room source (User ID: {user_id})")
        return

    logger.info(f"Received from Group/Room ID {group_id} by User ID {user_id}: {text}")

    # --- Use Database Session Context Manager ---
    try:
        with get_db() as db: # Get SQLAlchemy session
            # --- Match Commands ---
            add_match = re.match(ADD_TASK_PATTERN, text)
            complete_match = re.match(COMPLETE_TASK_PATTERN, text)
            list_match = re.match(LIST_TASK_PATTERN, text)
            delete_match = re.match(DELETE_TASK_PATTERN, text)
            edit_match = re.match(EDIT_TASK_PATTERN, text)
            detail_match = re.match(DETAIL_TASK_PATTERN, text)
            draw_match = re.match(DRAW_LOTS_PATTERN, text)
            pick_match = re.match(RANDOM_PICK_PATTERN, text)

            if add_match:
                handle_add_task(reply_token, add_match, group_id, user_id, db)
            elif complete_match:
                handle_complete_task(reply_token, complete_match, user_id, db) # Pass user_id for potential permission checks
            elif list_match:
                handle_list_tasks(reply_token, list_match, group_id, db)
            elif delete_match:
                handle_delete_task(reply_token, delete_match, group_id, user_id, db) # Pass user_id
            elif edit_match:
                handle_edit_task(reply_token, edit_match, group_id, user_id, db) # Pass user_id
            elif detail_match:
                handle_task_details(reply_token, detail_match, db)
            elif draw_match:
                handle_draw_lots(reply_token, draw_match) # No db needed
            elif pick_match:
                handle_random_pick(reply_token, pick_match) # No db needed
            elif text == "#幫助":
                send_help_message(reply_token) # No db needed
            else:
                # --- Placeholder for future OpenAI NLP ---
                logger.info("Message did not match any known command format.")
                # Optionally send a reply:
                # line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別指令，請輸入 #幫助 查看可用指令。"))
                pass

    except Exception as e:
        logger.exception(f"處理指令 '{text}' 或資料庫操作時發生錯誤: {str(e)}")
        try:
            line_bot_api.reply_message(
                reply_token,
                messages=[TextMessage(text="處理您的請求時發生內部錯誤，請稍後再試或聯繫管理員。")]
            )
        except Exception as reply_err:
            logger.error(f"回覆錯誤訊息時也發生錯誤: {str(reply_err)}")

# --- Command Handling Functions (Using SQLAlchemy Session 'db') ---

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Helper to parse YYYY/MM/DD string to datetime object (naive)"""
    if not date_str:
        return None
    try:
        # Return naive datetime object. PostgreSQL will handle timezone based on column type.
        return datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        return None # Indicate parsing failure

def handle_add_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """Handles add task command using SQLAlchemy"""
    member_name = match.group(1)
    task_content = match.group(2)
    due_date_str = match.group(3)

    due_date = parse_date(due_date_str)
    if due_date_str and due_date is None:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式。"))
        return

    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        # Option 1: Auto-create member
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        member = create_member(db, name=member_name, group_id=group_id)
        # Option 2: Reply error
        # line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到成員 '{member_name}'，請先確認成員名稱或請該成員發言一次。"))
        # return

    try:
        task = create_task(db, member_id=member.id, content=task_content, due_date=due_date)
        task_id_str = f"T-{task.id}"
        reply_text = f"✅ 已為 {member.name} 新增任務：\n內容：{task.content}\n任務ID：{task_id_str}\n"
        reply_text += (f"截止：{due_date.strftime('%Y/%m/%d')}" if due_date else "截止：無")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        logger.exception(f"新增任務到資料庫時失敗: {e}")
        db.rollback() # Rollback on error
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗，請稍後再試。"))


def handle_complete_task(reply_token: str, match: re.Match, completer_user_id: str, db: Session):
    """Handles complete task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # Optional: Add permission check - e.g., only assigned member or adder can complete?
    # elif task.member.line_user_id != completer_user_id:
    #     reply_text = f"❌ 您無法完成指派給 {task.member.name} 的任務。"
    elif task.status == 'completed':
        reply_text = f"ℹ️ 任務 T-{task_id_num} ({task.content[:10]}...) 已經是完成狀態。"
    else:
        try:
            task.status = 'completed'
            # Store timezone-aware datetime if possible, otherwise naive UTC
            task.completed_at = datetime.now(timezone.utc) # Use UTC for completion time
            db.commit() # Commit the change for this task
            reply_text = f"🎉 已將 {task.member.name} 的任務 T-{task_id_num} 標記為完成！\n內容：{task.content}"
        except Exception as e:
            logger.exception(f"更新任務 T-{task_id_num} 狀態時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_list_tasks(reply_token: str, match: re.Match, group_id: str, db: Session):
    """Handles list tasks command using SQLAlchemy"""
    member_name = match.group(1)
    tasks = []
    title = ""

    if member_name:
        member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
        if not member:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到成員：{member_name}"))
            return
        tasks = get_pending_tasks_by_member_id(db, member_id=member.id)
        title = f"{member_name} 的待辦事項"
    else:
        tasks = get_pending_tasks_by_group_id(db, group_id=group_id)
        title = "本群組待辦事項"

    if not tasks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ {title}：目前沒有待辦任務！"))
        return

    # Try Flex Message, fallback to Text
    try:
        bubble_json = create_task_list_bubble(title, tasks, db) # Pass db if needed by helper
        flex_message = FlexSendMessage(alt_text=title, contents=bubble_json)
        line_bot_api.reply_message(reply_token, messages=[flex_message])
    except Exception as e:
        logger.exception(f"創建或發送 Flex 消息失敗: {str(e)}")
        task_list_text = create_task_list_text(title, tasks, db) # Pass db if needed
        line_bot_api.reply_message(reply_token, TextSendMessage(text=task_list_text))

# --- NEW Command Handlers ---

def handle_delete_task(reply_token: str, match: re.Match, group_id: str, deleter_user_id: str, db: Session):
    """Handles delete task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # Optional: Add permission check (e.g., only creator or admins?)
    # For now, allow anyone in group to delete
    elif task.member.group_id != group_id: # Basic check: task belongs to this group
         reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組。"
    else:
        try:
            task_content_preview = task.content[:20] # For confirmation message
            db.delete(task) # Delete the task object
            db.commit()
            reply_text = f"🗑️ 已成功刪除任務 T-{task_id_num} ({task_content_preview}...)。"
        except Exception as e:
            logger.exception(f"刪除任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_edit_task(reply_token: str, match: re.Match, group_id: str, editor_user_id: str, db: Session):
    """Handles edit task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    new_content = match.group(2).strip()
    new_due_date_str = match.group(3)

    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif task.member.group_id != group_id: # Basic check: task belongs to this group
         reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組。"
    # Optional: Add permission check
    else:
        new_due_date = parse_date(new_due_date_str)
        if new_due_date_str and new_due_date is None:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式。"))
            return

        try:
            task.content = new_content
            task.due_date = new_due_date # Can be None to remove due date
            # Maybe update an 'updated_at' field if you add one to the model
            db.commit()
            due_date_text = f"截止：{new_due_date.strftime('%Y/%m/%d')}" if new_due_date else "截止：無"
            reply_text = f"✏️ 已更新任務 T-{task_id_num}：\n內容：{task.content}\n{due_date_text}"
        except Exception as e:
            logger.exception(f"修改任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 修改任務 T-{task_id_num} 失敗。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_task_details(reply_token: str, match: re.Match, db: Session):
    """Handles show task details command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    else:
        created_at_str = task.created_at.strftime('%Y/%m/%d %H:%M') if task.created_at else "未知"
        due_date_str = task.due_date.strftime('%Y/%m/%d') if task.due_date else "無"
        status_str = "✅ 已完成" if task.status == 'completed' else "⏳ 待辦中"
        completed_at_str = task.completed_at.strftime('%Y/%m/%d %H:%M') if task.completed_at else ""

        reply_text = f"🔍 任務詳情 T-{task_id_num} 🔍\n"
        reply_text += f"內容：{task.content}\n"
        reply_text += f"負責人：{task.member.name}\n"
        reply_text += f"狀態：{status_str}"
        if task.status == 'completed' and completed_at_str:
            reply_text += f" (於 {completed_at_str})\n"
        else:
            reply_text += "\n"
        reply_text += f"建立時間：{created_at_str}\n"
        reply_text += f"截止日期：{due_date_str}"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


# --- Other Command Handlers (No DB access needed) ---

def handle_draw_lots(reply_token: str, match: re.Match):
    """Handles draw lots command"""
    question = match.group(1)
    results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]
    result = random.choice(results)
    reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_random_pick(reply_token: str, match: re.Match):
    """Handles random pick command"""
    options_text = match.group(1)
    options = [opt.strip() for opt in options_text.split() if opt.strip()]
    if not options:
        reply_text = "請提供至少一個抽籤選項！ (用空格分隔)"
    else:
        chosen = random.choice(options)
        reply_text = f"從 [{', '.join(options)}] {len(options)} 個選項中抽出：\n🎉 {chosen} 🎉"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def send_help_message(reply_token: str):
    """Sends help message including new commands"""
    help_text = (
        "📋 代辦事項機器人指令 📋\n\n"
        "🔸 任務管理:\n"
        "   #新增 @成員 內容 [YYYY/MM/DD]\n"
        "     (截止日可選)\n"
        "   #完成 T-ID\n"
        "   #列表 [@成員]\n"
        "     (成員可選，預設列全部)\n"
        "   #修改 T-ID 新內容 [YYYY/MM/DD]\n"
        "     (截止日可選，不填會移除)\n"
        "   #刪除 T-ID\n"
        "   #詳情 T-ID\n\n"
        "🔸 其他功能:\n"
        "   #擲筊 問題\n"
        "   #抽籤 選項1 選項2 ...\n"
        "   #幫助 (顯示本說明)"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))


# --- Flex/Text Message Helpers (Need to accept db potentially, use SQLAlchemy objects) ---

def create_task_list_bubble(title: str, tasks: List[Task], db: Session):
    """Creates Flex Message bubble using SQLAlchemy Task objects"""
    # This function needs to be adapted slightly to use task.member.name
    # instead of calling Member.get_by_id(task.member_id) again.
    # The core JSON structure remains the same.
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "contents": []}
    }
    for task in tasks:
        # Access member directly through relationship
        member_name = task.member.name if task.member else '未知成員'

        task_header = {
            "type": "box", "layout": "horizontal",
            "contents": [
                {"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1},
                {"type": "text", "text": member_name, "size": "sm", "color": "#1DB446", "align": "end"}
            ]
        }
        task_content_text = {"type": "text", "text": task.content, "wrap": True, "weight": "bold", "margin": "sm"}
        task_box_contents = [task_header, task_content_text]

        if task.due_date:
            try:
                # Ensure due_date is datetime object
                due_date_obj = task.due_date
                if isinstance(due_date_obj, str): # Should not happen if model loads correctly
                     due_date_obj = datetime.fromisoformat(due_date_obj)

                # Use UTC now for comparison if due_date is timezone-aware
                now_aware = datetime.now(timezone.utc) if due_date_obj.tzinfo else datetime.now()
                days_left = (due_date_obj.date() - now_aware.date()).days # Compare dates only
                color = "#FF5555" if days_left < 0 else ("#FFAA00" if days_left < 2 else "#888888")
                due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                status_text = f"({days_left}天)" if days_left >= 0 else "(已逾期)"

                due_date_text_el = {
                    "type": "text", "text": f"截止: {due_date_str_display} {status_text}",
                    "size": "xs", "color": color, "margin": "sm"
                }
                task_box_contents.append(due_date_text_el)
            except Exception as date_err:
                 logger.error(f"處理任務 T-{task.id} 的截止日期時出錯 (Flex): {date_err}")

        complete_button = {
            "type": "button", "style": "primary", "color": "#DDDDDD", "height": "sm", "margin": "md",
            "action": {"type": "message", "label": "標記完成", "text": f"#完成 T-{task.id}"}
        }
        task_box_contents.append(complete_button)

        contents["body"]["contents"].append({
            "type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "md",
            "backgroundColor": "#FAFAFA", "cornerRadius": "md", "contents": task_box_contents
        })
    return contents

def create_task_list_text(title: str, tasks: List[Task], db: Session):
    """Creates fallback text message using SQLAlchemy Task objects"""
    # Adapt to use task.member.name
    result = f"📋 {title} 📋\n\n"
    for i, task in enumerate(tasks, 1):
        member_name = task.member.name if task.member else '未知成員'
        result += f"【任務 T-{task.id}】\n"
        result += f"👤 負責人: {member_name}\n"
        result += f"📝 內容: {task.content}\n"
        if task.due_date:
            try:
                due_date_obj = task.due_date
                if isinstance(due_date_obj, str):
                    due_date_obj = datetime.fromisoformat(due_date_obj)

                now_aware = datetime.now(timezone.utc) if due_date_obj.tzinfo else datetime.now()
                days_left = (due_date_obj.date() - now_aware.date()).days
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


# --- n8n Integration API Endpoints (Using SQLAlchemy) ---

@app.route("/api/pending-tasks", methods=['GET'])
def api_pending_tasks():
    """API Endpoint: Get pending tasks for the default group using SQLAlchemy"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    if not TARGET_GROUP_ID:
        return jsonify({"error": "Target Group ID is not configured."}), 500

    try:
        with get_db() as db:
            tasks = get_pending_tasks_by_group_id(db, group_id=TARGET_GROUP_ID)
            result = []
            for task in tasks:
                due_date_str, days_left = None, None
                if task.due_date:
                    try:
                        due_date_obj = task.due_date # Already datetime from DB with timezone=True
                        now_aware = datetime.now(timezone.utc) if due_date_obj.tzinfo else datetime.now()
                        days_left = (due_date_obj.date() - now_aware.date()).days
                        due_date_str = due_date_obj.strftime('%Y/%m/%d')
                    except Exception: due_date_str = "日期錯誤"

                result.append({
                    "id": task.id, "task_id": f"T-{task.id}",
                    "member": task.member.name if task.member else '未知',
                    "content": task.content, "due_date": due_date_str, "days_left": days_left,
                    "created_at": task.created_at.isoformat() if task.created_at else None
                })
        return jsonify({"tasks": result, "count": len(result), "group_id": TARGET_GROUP_ID})
    except Exception as e:
        logger.exception(f"API /api/pending-tasks 發生錯誤: {str(e)}")
        return jsonify({"error": "Internal server error fetching tasks."}), 500

@app.route("/api/send-reminder", methods=['POST'])
def api_send_reminder():
    """API Endpoint: Send reminder message to the default group"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    if not TARGET_GROUP_ID:
        return jsonify({"error": "Target Group ID is not configured."}), 500
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({"error": "Missing 'message' in request body"}), 400
    message = data['message']
    try:
        line_bot_api.push_message(TARGET_GROUP_ID, messages=[TextMessage(text=message)])
        logger.info(f"已成功透過 API 發送提醒至 Group ID: {TARGET_GROUP_ID}")
        return jsonify({"success": True, "message": "Reminder sent successfully"})
    except Exception as e:
        logger.exception(f"透過 API 發送提醒訊息時發生錯誤: {str(e)}")
        return jsonify({"success": False, "error": f"Failed to send reminder: {str(e)}"}), 500

# --- Main Execution Block ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    # For production, use Gunicorn as specified in Render's Start Command
    # For local development:
    # app.run(host='0.0.0.0', port=port, debug=True) # Enable debug for local dev if needed
    app.run(host='0.0.0.0', port=port)