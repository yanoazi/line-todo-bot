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
import inspect

# --- Database Imports (SQLAlchemy) ---
# Assuming models.py is in the same directory
from models import (
    init_db, get_db, Member, Task,
    get_member_by_name_and_group, get_member_by_id, get_task_by_id,
    get_pending_tasks_by_member_id, get_pending_tasks_by_group_id,
    create_member, create_task # Import necessary helpers
)
from sqlalchemy import text
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

# --- Replit Specific Configuration ---
# 檢測是否在 Replit 環境中運行
IN_REPLIT = os.environ.get('REPL_ID') is not None
REPLIT_DB_URL = os.environ.get('REPLIT_DB_URL')

# 若在 Replit 中且未設置 DATABASE_URL，自動配置將在 models.py 中處理
# 這裡只做日誌記錄
if IN_REPLIT:
    logger.info("在 Replit 環境中運行，資料庫配置將在 models.py 中處理。")

# --- LINE API Initialization (v2) ---
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- Database Initialization ---
# Call init_db on startup to ensure tables exist in PostgreSQL
# SQLAlchemy's create_all is safe to call multiple times
init_db()

# --- Regex Patterns (Added new commands) ---
ADD_TASK_PATTERN = r'#新增\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)\s+(\d{4}/\d{1,2}/\d{1,2})?$'
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DELETE_TASK_PATTERN = r'#刪除\s+T-(\d+)$' # New pattern for delete
EDIT_TASK_PATTERN = r'#修改\s+T-(\d+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)\s*(\d{4}/\d{1,2}/\d{1,2})?$' # New pattern for edit with priority
DETAIL_TASK_PATTERN = r'#詳情\s+T-(\d+)$' # New pattern for details
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'
# 新增批量任務模式
BATCH_ADD_TASK_PATTERN = r'#批量新增\s+@(\S+)\s+(.+)$'
# 定期任務相關模式
RECURRING_TASK_PATTERN = r'#定期\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)\s+每(週[一二三四五六日]|月\d{1,2}日|年\d{1,2}月\d{1,2}日)$'
CANCEL_RECURRING_PATTERN = r'#取消定期\s+T-(\d+)$'
# 表單填寫相關模式
PRE_ADD_PATTERN = r'#要新增\s+(?:@(\S+)|!(?:低|普通|高)|每(週[一二三四五六日]|月\d{1,2}日|年\d{1,2}月\d{1,2}日))?$'
PRE_RECURRING_PATTERN = r'#要新增定期\s+(?:@(\S+)|!(?:低|普通|高)|每(週[一二三四五六日]|月\d{1,2}日|年\d{1,2}月\d{1,2}日))?$'


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
            db.execute(text("SELECT 1"))
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
            batch_add_match = re.match(BATCH_ADD_TASK_PATTERN, text)
            recurring_match = re.match(RECURRING_TASK_PATTERN, text)
            cancel_recurring_match = re.match(CANCEL_RECURRING_PATTERN, text)
            pre_add_match = re.match(PRE_ADD_PATTERN, text)
            pre_recurring_match = re.match(PRE_RECURRING_PATTERN, text)

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
            elif batch_add_match:
                handle_batch_add_tasks(reply_token, batch_add_match, group_id, user_id, db)
            elif recurring_match:
                handle_recurring_task(reply_token, recurring_match, group_id, user_id, db)
            elif cancel_recurring_match:
                handle_cancel_recurring_task(reply_token, cancel_recurring_match, group_id, user_id, db)
            elif pre_add_match:
                handle_pre_add_task(reply_token, pre_add_match, group_id, user_id, db)
            elif pre_recurring_match:
                handle_pre_recurring_task(reply_token, pre_recurring_match, group_id, user_id, db)
            elif text == "#幫助":
                send_help_message(reply_token) # No db needed
            elif text == "#幫助新增":
                send_add_help_message(reply_token)
            elif text.startswith("#編輯幫助 T-"):
                task_id = text.split("T-")[1]
                send_edit_help_message(reply_token, task_id)
            elif text.startswith("#新增模板"):
                parts = text.split()
                if len(parts) >= 2:
                    priority = parts[1]
                    send_add_template(reply_token, priority)
                else:
                    send_add_template(reply_token, "!普通")
            elif text == "#定期模板":
                send_recurring_template(reply_token)
            elif text == "#新增":
                send_add_task_form(reply_token, db, group_id)
            elif text == "#新增定期":
                send_recurring_task_form(reply_token, db, group_id)
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
    priority_tag = match.group(2)
    task_content = match.group(3)
    due_date_str = match.group(4)

    # 處理優先級標籤
    priority = "normal"  # 預設為普通優先級
    if priority_tag:
        if "低" in priority_tag:
            priority = "low"
        elif "高" in priority_tag:
            priority = "high"

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
        task = create_task(db, member_id=member.id, content=task_content, due_date=due_date, priority=priority)
        task_id_str = f"T-{task.id}"
        
        # 根據優先級添加表情符號
        priority_emoji = "🟢" if priority == "low" else "🟡" if priority == "normal" else "🔴"
        priority_text = "低" if priority == "low" else "普通" if priority == "normal" else "高"
        
        reply_text = f"✅ 已為 {member.name} 新增任務：\n內容：{task.content}\n任務ID：{task_id_str}\n"
        reply_text += f"優先級：{priority_emoji} {priority_text}\n"
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
    priority_tag = match.group(2)
    new_content = match.group(3).strip()
    new_due_date_str = match.group(4)

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

        # 處理優先級標籤
        if priority_tag:
            if "低" in priority_tag:
                task.priority = "low"
            elif "高" in priority_tag:
                task.priority = "high"
            else:
                task.priority = "normal"

        try:
            task.content = new_content
            task.due_date = new_due_date # Can be None to remove due date
            # Maybe update an 'updated_at' field if you add one to the model
            db.commit()
            
            # 根據優先級添加表情符號
            priority_emoji = "🟢" if task.priority == "low" else "🟡" if task.priority == "normal" else "🔴"
            priority_text = "低" if task.priority == "low" else "普通" if task.priority == "normal" else "高"
            
            due_date_text = f"截止：{new_due_date.strftime('%Y/%m/%d')}" if new_due_date else "截止：無"
            reply_text = f"✏️ 已更新任務 T-{task_id_num}：\n內容：{task.content}\n優先級：{priority_emoji} {priority_text}\n{due_date_text}"
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
        
        # 處理優先級
        priority_emoji = "🟢" if task.priority == "low" else "🟡" if task.priority == "normal" else "🔴"
        priority_text = "低" if task.priority == "low" else "普通" if task.priority == "normal" else "高"
        
        # 處理定期任務信息
        recurring_text = ""
        if task.is_recurring:
            pattern_text = "未知"
            if task.recurrence_pattern:
                if task.recurrence_pattern.startswith("weekly_"):
                    day = task.recurrence_pattern.split("_")[1]
                    day_map = {"monday": "週一", "tuesday": "週二", "wednesday": "週三", 
                              "thursday": "週四", "friday": "週五", "saturday": "週六", "sunday": "週日"}
                    pattern_text = f"每{day_map.get(day, day)}"
                elif task.recurrence_pattern.startswith("monthly_"):
                    day = task.recurrence_pattern.split("_")[1]
                    pattern_text = f"每月{day}日"
                elif task.recurrence_pattern.startswith("yearly_"):
                    parts = task.recurrence_pattern.split("_")
                    if len(parts) >= 3:
                        month, day = parts[1], parts[2]
                        pattern_text = f"每年{month}月{day}日"
            recurring_text = f"⏰ 定期任務：{pattern_text} (已重複 {task.recurrence_count} 次)\n"
        elif task.parent_task_id:
            parent_task = get_task_by_id(db, task_id=task.parent_task_id)
            if parent_task:
                recurring_text = f"🔄 定期任務衍生：來自 T-{parent_task.id}\n"

        reply_text = f"🔍 任務詳情 T-{task_id_num} 🔍\n"
        reply_text += f"內容：{task.content}\n"
        reply_text += f"負責人：{task.member.name}\n"
        reply_text += f"優先級：{priority_emoji} {priority_text}\n"
        if recurring_text:
            reply_text += recurring_text
        reply_text += f"狀態：{status_str}"
        if task.status == 'completed' and completed_at_str:
            reply_text += f" (於 {completed_at_str})\n"
        else:
            reply_text += "\n"
        reply_text += f"建立時間：{created_at_str}\n"
        reply_text += f"截止日期：{due_date_str}"
        
        # 創建 Flex 訊息以添加快捷操作按鈕
        try:
            contents = {
                "type": "bubble",
                "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"任務詳情 T-{task_id_num}", "weight": "bold", "size": "lg"}]},
                "body": {
                    "type": "box", "layout": "vertical", 
                    "contents": [
                        {"type": "text", "text": task.content, "wrap": True, "weight": "bold", "size": "md"},
                        {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                            {"type": "text", "text": "負責人:", "size": "sm", "color": "#888888", "flex": 2},
                            {"type": "text", "text": task.member.name, "size": "sm", "color": "#1DB446", "flex": 3}
                        ]},
                        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                            {"type": "text", "text": "優先級:", "size": "sm", "color": "#888888", "flex": 2},
                            {"type": "text", "text": f"{priority_emoji} {priority_text}", "size": "sm", 
                             "color": "#28a745" if task.priority == "low" else "#ffc107" if task.priority == "normal" else "#dc3545", 
                             "flex": 3}
                        ]},
                        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                            {"type": "text", "text": "狀態:", "size": "sm", "color": "#888888", "flex": 2},
                            {"type": "text", "text": status_str, "size": "sm", "color": "#28a745" if task.status == "completed" else "#ffc107", "flex": 3}
                        ]},
                        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                            {"type": "text", "text": "截止日期:", "size": "sm", "color": "#888888", "flex": 2},
                            {"type": "text", "text": due_date_str, "size": "sm", "color": "#888888", "flex": 3}
                        ]}
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "box", "layout": "horizontal", "contents": [
                            {
                                "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                                "action": {"type": "message", "label": "完成任務", "text": f"#完成 T-{task_id_num}"}
                            },
                            {
                                "type": "button", "style": "secondary", "color": "#ffc107", "height": "sm", "flex": 1, "margin": "md",
                                "action": {"type": "message", "label": "編輯任務", "text": f"#編輯幫助 T-{task_id_num}"}
                            }
                        ]},
                        {"type": "button", "style": "secondary", "color": "#dc3545", "margin": "md",
                         "action": {"type": "message", "label": "刪除任務", "text": f"#刪除 T-{task_id_num}"}}
                    ]
                }
            }
            
            # 如果是定期任務，添加取消定期按鈕
            if task.is_recurring:
                contents["footer"]["contents"].append({
                    "type": "button", "style": "secondary", "color": "#9C27B0", "margin": "md",
                    "action": {"type": "message", "label": "取消定期任務", "text": f"#取消定期 T-{task_id_num}"}
                })
            
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=f"任務 T-{task_id_num} 詳情", contents=contents))
            return
        except Exception as e:
            logger.exception(f"創建任務詳情 Flex 訊息失敗: {e}")
            # 如果 Flex 訊息失敗，使用純文字訊息

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


# --- Other Command Handlers (No DB access needed) ---

def handle_draw_lots(reply_token: str, match: re.Match):
    """Handles draw lots command"""
    question = match.group(1)
    results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]
    result = random.choice(results)
    reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"
    
    # 創建擲筊結果的 Flex 訊息
    try:
        result_emoji = "👍" if "聖筊" in result else "👎" if "陰筊" in result else "🤔"
        result_color = "#28a745" if "聖筊" in result else "#dc3545" if "陰筊" in result else "#ffc107"
        
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "擲筊結果", "weight": "bold", "size": "lg"}]},
            "body": {
                "type": "box", "layout": "vertical", 
                "contents": [
                    {"type": "text", "text": f"問題: {question}", "wrap": True, "weight": "bold", "size": "md"},
                    {"type": "box", "layout": "vertical", "margin": "xl", "contents": [
                        {"type": "text", "text": result, "size": "xxl", "align": "center", "color": result_color, "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                        {
                            "type": "button", "style": "primary", "color": result_color, "height": "sm",
                            "action": {"type": "message", "label": f"再擲一次 {result_emoji}", "text": f"#擲筊 {question}"}
                        }
                    ]}
                ]
            }
        }
        
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建擲筊 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_random_pick(reply_token: str, match: re.Match):
    """Handles random pick command"""
    options_text = match.group(1)
    options = [opt.strip() for opt in options_text.split() if opt.strip()]
    if not options:
        reply_text = "請提供至少一個抽籤選項！ (用空格分隔)"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        return
        
    chosen = random.choice(options)
    reply_text = f"從 [{', '.join(options)}] {len(options)} 個選項中抽出：\n🎉 {chosen} 🎉"
    
    # 創建抽籤結果的 Flex 訊息
    try:
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "抽籤結果", "weight": "bold", "size": "lg"}]},
            "body": {
                "type": "box", "layout": "vertical", 
                "contents": [
                    {"type": "text", "text": f"從 {len(options)} 個選項中", "size": "sm", "color": "#888888"},
                    {"type": "box", "layout": "vertical", "margin": "md", "contents": [
                        {"type": "text", "text": chosen, "size": "xxl", "align": "center", "weight": "bold", "wrap": True}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "xl", "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#2196F3", "height": "sm",
                            "action": {"type": "message", "label": "再抽一次", "text": f"#抽籤 {options_text}"}
                        }
                    ]}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", 
                "contents": [
                    {"type": "text", "text": f"選項: {', '.join(options)}", "size": "xs", "color": "#888888", "wrap": True}
                ]
            }
        }
        
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建抽籤 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def send_help_message(reply_token: str):
    """Sends help message including new commands"""
    help_text = (
        "📋 代辦事項機器人指令 📋\n\n"
        "🔸 任務管理:\n"
        "   #新增 @成員 [!優先級] 內容 [YYYY/MM/DD]\n"
        "     (優先級可為 !低、!普通、!高)\n"
        "     (截止日可選)\n"
        "   #批量新增 @成員\n"
        "     [!優先級] 任務1 [YYYY/MM/DD]\n"
        "     [!優先級] 任務2 [YYYY/MM/DD]\n"
        "     (每行一個任務，優先級、日期可選)\n"
        "   #定期 @成員 [!優先級] 內容 每週一\n"
        "     (週一至週日、月DD日、年MM月DD日)\n"
        "   #取消定期 T-ID\n"
        "   #完成 T-ID\n"
        "   #列表 [@成員]\n"
        "     (成員可選，預設列全部)\n"
        "   #修改 T-ID [!優先級] 新內容 [YYYY/MM/DD]\n"
        "     (優先級、截止日可選)\n"
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
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "contents": []},
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "md",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                    "action": {"type": "message", "label": "新增任務", "text": "#幫助新增"}
                },
                {
                    "type": "button", "style": "secondary", "color": "#6c757d", "height": "sm", "flex": 1,
                    "action": {"type": "message", "label": "幫助", "text": "#幫助"}
                }
            ]
        }
    }
    for task in tasks:
        # Access member directly through relationship
        member_name = task.member.name if task.member else '未知成員'
        
        # 處理優先級表示
        priority_emoji = "🟢" if task.priority == "low" else "🟡" if task.priority == "normal" else "🔴"
        priority_text = "低" if task.priority == "low" else "普通" if task.priority == "normal" else "高"
        priority_color = "#28a745" if task.priority == "low" else "#ffc107" if task.priority == "normal" else "#dc3545"

        task_header = {
            "type": "box", "layout": "horizontal",
            "contents": [
                {"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1},
                {"type": "text", "text": f"{priority_emoji} {priority_text}", "size": "sm", "color": priority_color, "align": "center", "flex": 1},
                {"type": "text", "text": member_name, "size": "sm", "color": "#1DB446", "align": "end", "flex": 1}
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

        # 按鈕區塊 - 更多選項
        buttons_box = {
            "type": "box", "layout": "horizontal", "margin": "md", 
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#4CAF50", "height": "sm", "flex": 1,
                    "action": {"type": "message", "label": "完成", "text": f"#完成 T-{task.id}"}
                },
                {
                    "type": "button", "style": "secondary", "color": "#2196F3", "height": "sm", "flex": 1, "margin": "md",
                    "action": {"type": "message", "label": "詳情", "text": f"#詳情 T-{task.id}"}
                }
            ]
        }
        
        # 第二排按鈕（編輯、刪除）
        buttons_box2 = {
            "type": "box", "layout": "horizontal", "margin": "md", 
            "contents": [
                {
                    "type": "button", "style": "secondary", "color": "#FFC107", "height": "sm", "flex": 1,
                    "action": {"type": "message", "label": "編輯", "text": f"#編輯幫助 T-{task.id}"}
                },
                {
                    "type": "button", "style": "secondary", "color": "#F44336", "height": "sm", "flex": 1, "margin": "md",
                    "action": {"type": "message", "label": "刪除", "text": f"#刪除 T-{task.id}"}
                }
            ]
        }
        
        task_box_contents.append(buttons_box)
        task_box_contents.append(buttons_box2)
        
        # 若是定期任務，添加取消定期按鈕
        if task.is_recurring:
            recurring_text = {"type": "text", "text": f"⏰ 定期任務", "size": "xs", "color": "#9C27B0", "margin": "md"}
            cancel_recurring_button = {
                "type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "margin": "md",
                "action": {"type": "message", "label": "取消定期", "text": f"#取消定期 T-{task.id}"}
            }
            task_box_contents.append(recurring_text)
            task_box_contents.append(cancel_recurring_button)

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
        
        # 處理優先級表示
        priority_emoji = "🟢" if task.priority == "low" else "🟡" if task.priority == "normal" else "🔴"
        priority_text = "低" if task.priority == "low" else "普通" if task.priority == "normal" else "高"
        
        result += f"【任務 T-{task.id}】 {priority_emoji}\n"
        result += f"👤 負責人: {member_name}\n"
        result += f"📝 內容: {task.content}\n"
        result += f"⚡ 優先級: {priority_text}\n"
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

def handle_batch_add_tasks(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """處理批量添加任務的命令"""
    member_name = match.group(1)
    tasks_text = match.group(2).strip()
    
    # 按行分割任務列表，忽略空行
    task_lines = [line.strip() for line in tasks_text.split('\n') if line.strip()]
    
    if not task_lines:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="未提供任何任務內容。格式應為：\n#批量新增 @成員\n[!優先級] 任務1 [日期]\n[!優先級] 任務2 [日期]\n..."))
        return
    
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        # 自動建立成員
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        member = create_member(db, name=member_name, group_id=group_id)
    
    success_count = 0
    task_summaries = []
    
    for task_line in task_lines:
        # 嘗試解析每一行
        priority = "normal"  # 預設優先級
        content = task_line
        due_date = None
        
        # 檢查優先級標籤 !低、!普通、!高
        priority_match = re.match(r'^!(?:低|普通|高)\s+(.+)$', task_line)
        if priority_match:
            if "!低" in task_line:
                priority = "low"
            elif "!高" in task_line:
                priority = "high"
            content = priority_match.group(1)
        
        # 檢查日期
        parts = content.split()
        if parts and re.match(r'\d{4}/\d{1,2}/\d{1,2}$', parts[-1]):
            due_date_str = parts[-1]
            content = ' '.join(parts[:-1])
            due_date = parse_date(due_date_str)
        
        if not content:
            continue
            
        try:
            task = create_task(db, member_id=member.id, content=content, due_date=due_date, priority=priority)
            success_count += 1
            
            # 根據優先級添加表情符號
            priority_emoji = "🟢" if priority == "low" else "🟡" if priority == "normal" else "🔴"
            
            task_summary = f"{priority_emoji} T-{task.id}: {task.content}"
            if due_date:
                task_summary += f" (截止：{due_date.strftime('%Y/%m/%d')})"
            task_summaries.append(task_summary)
            
        except Exception as e:
            logger.exception(f"批量新增任務失敗: {e}")
            # 繼續處理其他任務
    
    if success_count > 0:
        db.commit()  # 提交所有成功的任務
        summary_text = f"✅ 已為 {member.name} 新增 {success_count} 個任務：\n" + "\n".join(task_summaries)
        
        # 如果摘要太長，截斷它
        if len(summary_text) > 2000:  # LINE 訊息長度限制
            summary_text = summary_text[:1950] + "...\n(顯示部分任務，共新增 " + str(success_count) + " 個)"
        
        line_bot_api.reply_message(reply_token, TextSendMessage(text=summary_text))
    else:
        db.rollback()  # 如果沒有成功，回滾事務
        line_bot_api.reply_message(reply_token, TextSendMessage(text="❌ 批量新增任務失敗，請檢查任務格式。"))

def handle_recurring_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """處理新增定期任務"""
    member_name = match.group(1)
    priority_tag = match.group(2)
    task_content = match.group(3)
    recurrence_pattern = match.group(4)
    
    # 處理優先級標籤
    priority = "normal"  # 預設為普通優先級
    if priority_tag:
        if "低" in priority_tag:
            priority = "low"
        elif "高" in priority_tag:
            priority = "high"
    
    # 解析重複模式文字為系統格式
    pattern_map = {
        "週一": "weekly_monday",
        "週二": "weekly_tuesday",
        "週三": "weekly_wednesday",
        "週四": "weekly_thursday",
        "週五": "weekly_friday",
        "週六": "weekly_saturday",
        "週日": "weekly_sunday"
    }
    
    system_pattern = None
    if recurrence_pattern in pattern_map:
        system_pattern = pattern_map[recurrence_pattern]
    elif recurrence_pattern.startswith("月") and recurrence_pattern.endswith("日"):
        day = recurrence_pattern[1:-1]
        if day.isdigit() and 1 <= int(day) <= 31:
            system_pattern = f"monthly_{day}"
    elif recurrence_pattern.startswith("年") and "月" in recurrence_pattern and recurrence_pattern.endswith("日"):
        parts = recurrence_pattern[1:-1].split("月")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            month, day = int(parts[0]), int(parts[1])
            if 1 <= month <= 12 and 1 <= day <= 31:
                system_pattern = f"yearly_{month}_{day}"
    
    if not system_pattern:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別的重複模式。請使用「每週一」、「每月1日」或「每年1月1日」等格式。"))
        return
    
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        member = create_member(db, name=member_name, group_id=group_id)
    
    try:
        # 創建定期任務的主任務
        task = Task(
            member_id=member.id,
            content=task_content,
            status='pending',
            priority=priority,
            is_recurring=True,
            recurrence_pattern=system_pattern,
            recurrence_count=0
        )
        db.add(task)
        db.flush()  # 獲取主任務 ID 但還不提交
        
        # 提交任務
        db.commit()
        
        # 根據優先級添加表情符號
        priority_emoji = "🟢" if priority == "low" else "🟡" if priority == "normal" else "🔴"
        priority_text = "低" if priority == "low" else "普通" if priority == "normal" else "高"
        
        # 將系統格式轉換為用戶友好的文字
        user_friendly_pattern = recurrence_pattern
        
        reply_text = f"✅ 已為 {member.name} 新增定期任務：\n內容：{task.content}\n任務ID：T-{task.id}\n"
        reply_text += f"優先級：{priority_emoji} {priority_text}\n"
        reply_text += f"重複模式：每{user_friendly_pattern}\n"
        reply_text += f"輸入「#取消定期 T-{task.id}」可取消定期任務"
        
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        logger.exception(f"新增定期任務到資料庫時失敗: {e}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗，請稍後再試。"))


def handle_cancel_recurring_task(reply_token: str, match: re.Match, group_id: str, user_id: str, db: Session):
    """處理取消定期任務"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)
    
    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif not task.is_recurring:
        reply_text = f"❌ 任務 T-{task_id_num} 不是定期任務。"
    elif task.member.group_id != group_id:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組。"
    else:
        try:
            # 取消定期任務標記
            task.is_recurring = False
            db.commit()
            
            reply_text = f"✅ 已取消 {task.member.name} 的定期任務 T-{task_id_num}：\n內容：{task.content}"
        except Exception as e:
            logger.exception(f"取消定期任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 取消定期任務 T-{task_id_num} 失敗。"
    
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def send_add_help_message(reply_token: str):
    """發送新增任務幫助訊息"""
    help_text = (
        "📝 如何新增任務 📝\n\n"
        "🔹 單一任務：\n"
        "  #新增 @成員名稱 !優先級 任務內容 截止日期\n"
        "  例如：\n"
        "  #新增 @小明 !高 完成報告 2023/12/31\n\n"
        "🔹 批量任務：\n"
        "  #批量新增 @成員名稱\n"
        "  !優先級 任務1 截止日期\n"
        "  !優先級 任務2 截止日期\n"
        "  (每行一個任務，優先級和日期可選)\n\n"
        "🔹 定期任務：\n"
        "  #定期 @成員名稱 !優先級 任務內容 每週一\n"
        "  (可用：每週一~日、每月1日、每年1月1日)\n\n"
        "🔸 優先級可選項：!低、!普通、!高"
    )
    
    # 創建 Flex 訊息用於快速新增
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "快速新增任務", "weight": "bold", "size": "lg"}]},
        "body": {
            "type": "box", "layout": "vertical", 
            "contents": [
                {
                    "type": "text", "text": "選擇成員並點擊優先級按鈕",
                    "size": "md", "weight": "bold", "margin": "md"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": "!低優先級", "text": "#新增模板 !低"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#ffc107", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!普通優先級", "text": "#新增模板 !普通"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#dc3545", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!高優先級", "text": "#新增模板 !高"}
                        }
                    ]
                },
                {
                    "type": "text", "text": "定期任務",
                    "size": "md", "weight": "bold", "margin": "xl"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": "定期任務模板", "text": "#定期模板"}
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        messages = [
            TextSendMessage(text=help_text),
            FlexSendMessage(alt_text="快速新增任務", contents=contents)
        ]
        line_bot_api.reply_message(reply_token, messages=messages)
    except Exception as e:
        logger.exception(f"發送新增幫助 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_edit_help_message(reply_token: str, task_id: str):
    """發送編輯任務幫助訊息"""
    help_text = (
        f"✏️ 如何編輯任務 T-{task_id} ✏️\n\n"
        "使用以下格式編輯任務：\n"
        f"#修改 T-{task_id} !優先級 新內容 新截止日期\n\n"
        "例如：\n"
        f"#修改 T-{task_id} !高 更新後的任務內容 2023/12/31\n\n"
        "🔸 優先級可選項：!低、!普通、!高\n"
        "🔸 截止日期格式：YYYY/MM/DD (可選)\n"
        "🔸 若不修改優先級，可省略優先級部分\n"
        "🔸 若要移除截止日期，請省略日期部分"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_add_template(reply_token: str, priority: str):
    """發送新增模板幫助訊息"""
    help_text = (
        "📝 如何新增任務 📝\n\n"
        "🔹 單一任務：\n"
        "  #新增 @成員名稱 !優先級 任務內容 截止日期\n"
        "  例如：\n"
        f"  #新增 @小明 {priority} 完成報告 2023/12/31\n\n"
        "🔹 批量任務：\n"
        "  #批量新增 @成員名稱\n"
        "  !優先級 任務1 截止日期\n"
        "  !優先級 任務2 截止日期\n"
        "  (每行一個任務，優先級和日期可選)\n\n"
        "🔹 定期任務：\n"
        "  #定期 @成員名稱 !優先級 任務內容 每週一\n"
        "  (可用：每週一~日、每月1日、每年1月1日)\n\n"
        "🔸 優先級可選項：!低、!普通、!高"
    )
    
    # 創建 Flex 訊息用於快速新增
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "快速新增任務", "weight": "bold", "size": "lg"}]},
        "body": {
            "type": "box", "layout": "vertical", 
            "contents": [
                {
                    "type": "text", "text": "選擇成員並點擊優先級按鈕",
                    "size": "md", "weight": "bold", "margin": "md"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": f"{priority}優先級", "text": f"#新增模板 {priority}"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#ffc107", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!普通優先級", "text": "#新增模板 !普通"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#dc3545", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!高優先級", "text": "#新增模板 !高"}
                        }
                    ]
                },
                {
                    "type": "text", "text": "定期任務",
                    "size": "md", "weight": "bold", "margin": "xl"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": "定期任務模板", "text": "#定期模板"}
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        messages = [
            TextSendMessage(text=help_text),
            FlexSendMessage(alt_text="快速新增任務", contents=contents)
        ]
        line_bot_api.reply_message(reply_token, messages=messages)
    except Exception as e:
        logger.exception(f"發送新增幫助 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_recurring_template(reply_token: str):
    """發送定期模板幫助訊息"""
    help_text = (
        "📝 如何新增定期任務 📝\n\n"
        "🔹 定期任務：\n"
        "  #定期 @成員名稱 !優先級 任務內容 每週一\n"
        "  (可用：每週一~日、每月1日、每年1月1日)\n\n"
        "🔸 優先級可選項：!低、!普通、!高"
    )
    
    # 創建 Flex 訊息用於快速新增
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "快速新增定期任務", "weight": "bold", "size": "lg"}]},
        "body": {
            "type": "box", "layout": "vertical", 
            "contents": [
                {
                    "type": "text", "text": "選擇成員並點擊優先級按鈕",
                    "size": "md", "weight": "bold", "margin": "md"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": "!低優先級", "text": "#新增模板 !低"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#ffc107", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!普通優先級", "text": "#新增模板 !普通"}
                        },
                        {
                            "type": "button", "style": "primary", "color": "#dc3545", "height": "sm", "flex": 1, "margin": "md",
                            "action": {"type": "message", "label": "!高優先級", "text": "#新增模板 !高"}
                        }
                    ]
                },
                {
                    "type": "text", "text": "定期任務",
                    "size": "md", "weight": "bold", "margin": "xl"
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "flex": 1,
                            "action": {"type": "message", "label": "定期任務模板", "text": "#定期模板"}
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        messages = [
            TextSendMessage(text=help_text),
            FlexSendMessage(alt_text="快速新增定期任務", contents=contents)
        ]
        line_bot_api.reply_message(reply_token, messages=messages)
    except Exception as e:
        logger.exception(f"發送新增幫助 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

@app.route("/api/generate-recurring-tasks", methods=['POST'])
def api_generate_recurring_tasks():
    """API Endpoint: 生成定期任務"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    
    current_date = datetime.now().date()
    day_of_week = current_date.strftime('%A').lower()  # 'monday', 'tuesday', ...
    day_of_month = current_date.day
    month_and_day = current_date.strftime('%-m_%-d')  # '1_1' for January 1st
    
    # 映射英文星期到對應的模式
    day_map = {
        'monday': 'weekly_monday',
        'tuesday': 'weekly_tuesday', 
        'wednesday': 'weekly_wednesday',
        'thursday': 'weekly_thursday',
        'friday': 'weekly_friday',
        'saturday': 'weekly_saturday',
        'sunday': 'weekly_sunday'
    }
    
    weekly_pattern = day_map.get(day_of_week)
    monthly_pattern = f"monthly_{day_of_month}"
    yearly_pattern = f"yearly_{month_and_day}"
    
    created_tasks = []
    
    try:
        with get_db() as db:
            # 尋找所有符合條件的定期任務
            recurring_tasks = db.query(Task).filter(
                Task.is_recurring == True,
                (
                    (Task.recurrence_pattern == weekly_pattern) |
                    (Task.recurrence_pattern == monthly_pattern) |
                    (Task.recurrence_pattern == yearly_pattern)
                )
            ).all()
            
            for task in recurring_tasks:
                # 建立新的任務實例
                new_task = Task(
                    member_id=task.member_id,
                    content=task.content,
                    status='pending',
                    priority=task.priority,
                    due_date=None,
                    parent_task_id=task.id
                )
                
                # 更新計數
                task.recurrence_count += 1
                
                db.add(new_task)
                db.flush()  # 取得新ID
                
                created_tasks.append({
                    "id": new_task.id,
                    "task_id": f"T-{new_task.id}",
                    "member_id": task.member_id,
                    "member_name": task.member.name,
                    "content": new_task.content,
                    "pattern": task.recurrence_pattern
                })
            
            db.commit()
        
        if created_tasks and TARGET_GROUP_ID:
            try:
                # 發送通知訊息
                notification = "🔄 已生成今日定期任務：\n"
                for task in created_tasks[:10]:  # 最多顯示10個
                    notification += f"· T-{task['id']} ({task['member_name']}): {task['content']}\n"
                
                if len(created_tasks) > 10:
                    notification += f"...(等共計 {len(created_tasks)} 個任務)"
                
                line_bot_api.push_message(TARGET_GROUP_ID, TextSendMessage(text=notification))
            except Exception as e:
                logger.exception(f"發送定期任務通知訊息失敗: {e}")
        
        return jsonify({
            "success": True, 
            "created_count": len(created_tasks),
            "tasks": created_tasks
        })
        
    except Exception as e:
        logger.exception(f"生成定期任務時發生錯誤: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# --- Main Execution Block ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    
    # 特別處理 Replit 環境
    if IN_REPLIT:
        # 使用端口 5001
        port = 5001
        logger.info(f"在 Replit 環境中運行，使用端口 {port}")
        
        # 導入 Replit 特有的模塊
        try:
            from threading import Thread
            import socket
            
            def keep_alive():
                """保持 Replit 程序不休眠的函數"""
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('0.0.0.0', port))
                sock.listen(5)
                
                while True:
                    client, addr = sock.accept()
                    client.close()
            
            # 啟動保持活躍的線程
            Thread(target=keep_alive, daemon=True).start()
        except ImportError:
            logger.warning("無法導入 threading 或 socket 模塊，可能導致 Replit 休眠。")
    
    # 啟動 Flask 應用
    logger.info(f"Flask 應用啟動於端口 {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

def send_add_task_form(reply_token: str, db: Session, group_id: str):
    """發送任務新增表單"""
    try:
        # 獲取群組所有成員供選擇
        members = []
        try:
            # 查詢該群組的所有成員
            if group_id:
                members = db.query(Member).filter(Member.group_id == group_id).all()
                logger.info(f"已從資料庫獲取群組 {group_id} 的 {len(members)} 名成員")
            else:
                logger.warning("傳入的group_id為空，將使用空成員列表")
        except Exception as e:
            logger.exception(f"獲取成員列表失敗: {e}")
            
        # 創建Flex消息用於任務新增表單
        contents = {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "新增任務", "weight": "bold", "size": "xl", "color": "#2196F3"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {
                        "type": "text", "text": "請選擇成員並設定任務內容",
                        "weight": "bold", "size": "md", "wrap": True, "margin": "md"
                    },
                    {
                        "type": "separator", "margin": "md"
                    },
                    # 成員選擇區
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "選擇成員", "weight": "bold", "size": "sm", "color": "#888888"},
                        ]
                    },
                    # 優先級選擇區
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "選擇優先級", "weight": "bold", "size": "sm", "color": "#888888"},
                            {
                                "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "低", "text": "#要新增 !低"}
                                    },
                                    {
                                        "type": "button", "style": "primary", "color": "#ffc107", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "普通", "text": "#要新增 !普通"}
                                    },
                                    {
                                        "type": "button", "style": "primary", "color": "#dc3545", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "高", "text": "#要新增 !高"}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#2196F3",
                        "action": {"type": "message", "label": "批量新增任務", "text": "#批量新增 @"}
                    },
                    {
                        "type": "button", "style": "secondary",
                        "action": {"type": "message", "label": "查看說明", "text": "#幫助新增"}
                    }
                ]
            }
        }
        
        # 動態生成成員按鈕
        member_buttons_contents = []
        
        # 在資料庫中有成員的情況下生成按鈕
        if members:
            # 計算每行顯示的按鈕數量
            buttons_per_row = 2
            
            # 將成員分組，每行最多buttons_per_row個按鈕
            member_groups = [members[i:i + buttons_per_row] for i in range(0, len(members), buttons_per_row)]
            
            for member_group in member_groups:
                row_buttons = []
                for member in member_group:
                    row_buttons.append({
                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                        "action": {"type": "message", "label": member.name, "text": f"#要新增 @{member.name}"}
                    })
                    # 如果一行的按鈕不足buttons_per_row個，添加空白元素補齊
                    while len(row_buttons) < buttons_per_row:
                        row_buttons.append({
                            "type": "filler"
                        })
                
                # 添加一行按鈕
                member_buttons_contents.append({
                    "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm", "flex": 1,
                    "contents": row_buttons
                })
        else:
            # 如果沒有獲取到成員，提供一個輸入提示
            member_buttons_contents.append({
                "type": "text", "text": "請輸入: #要新增 @成員名稱", 
                "size": "sm", "color": "#555555", "align": "center"
            })
        
        # 添加成員輸入按鈕
        member_buttons_contents.append({
            "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
            "action": {"type": "message", "label": "手動輸入成員", "text": "#要新增 @"}
        })
        
        # 將生成的按鈕添加到成員選擇區
        member_section = contents["body"]["contents"][2]
        member_section["contents"].extend(member_buttons_contents)

        # 發送Flex消息
        line_bot_api.reply_message(
            reply_token,
            FlexSendMessage(alt_text="新增任務表單", contents=contents)
        )
    except Exception as e:
        logger.exception(f"發送任務新增表單失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="很抱歉，無法顯示任務新增表單。請直接輸入「#幫助新增」查看新增任務說明。"))

def send_recurring_task_form(reply_token: str, db: Session, group_id: str):
    """發送定期任務新增表單"""
    try:
        # 獲取群組所有成員供選擇
        members = []
        try:
            # 查詢該群組的所有成員
            if group_id:
                members = db.query(Member).filter(Member.group_id == group_id).all()
                logger.info(f"已從資料庫獲取群組 {group_id} 的 {len(members)} 名成員")
            else:
                logger.warning("傳入的group_id為空，將使用空成員列表")
        except Exception as e:
            logger.exception(f"獲取成員列表失敗: {e}")
            
        # 創建Flex消息用於定期任務新增表單
        contents = {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "新增定期任務", "weight": "bold", "size": "xl", "color": "#9C27B0"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {
                        "type": "text", "text": "請選擇成員、優先級和重複模式",
                        "weight": "bold", "size": "md", "wrap": True, "margin": "md"
                    },
                    {
                        "type": "separator", "margin": "md"
                    },
                    # 成員選擇區
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "選擇成員", "weight": "bold", "size": "sm", "color": "#888888"},
                        ]
                    },
                    # 優先級選擇區
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "選擇優先級", "weight": "bold", "size": "sm", "color": "#888888"},
                            {
                                "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button", "style": "primary", "color": "#28a745", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "低", "text": "#要新增定期 !低"}
                                    },
                                    {
                                        "type": "button", "style": "primary", "color": "#ffc107", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "普通", "text": "#要新增定期 !普通"}
                                    },
                                    {
                                        "type": "button", "style": "primary", "color": "#dc3545", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "高", "text": "#要新增定期 !高"}
                                    }
                                ]
                            }
                        ]
                    },
                    # 重複模式選擇區
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "選擇重複模式", "weight": "bold", "size": "sm", "color": "#888888"},
                            {
                                "type": "text", "text": "每週", "weight": "bold", "size": "sm", "margin": "md"
                            },
                            {
                                "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週一", "text": "#要新增定期 每週一"}
                                    },
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週二", "text": "#要新增定期 每週二"}
                                    },
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週三", "text": "#要新增定期 每週三"}
                                    }
                                ]
                            },
                            {
                                "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週四", "text": "#要新增定期 每週四"}
                                    },
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週五", "text": "#要新增定期 每週五"}
                                    },
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "週六", "text": "#要新增定期 每週六"}
                                    }
                                ]
                            },
                            {
                                "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
                                "action": {"type": "message", "label": "週日", "text": "#要新增定期 每週日"}
                            },
                            {
                                "type": "text", "text": "每月/每年", "weight": "bold", "size": "sm", "margin": "md"
                            },
                            {
                                "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "每月1日", "text": "#要新增定期 每月1日"}
                                    },
                                    {
                                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                                        "action": {"type": "message", "label": "每月15日", "text": "#要新增定期 每月15日"}
                                    }
                                ]
                            },
                            {
                                "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
                                "action": {"type": "message", "label": "每年1月1日", "text": "#要新增定期 每年1月1日"}
                            }
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#9C27B0",
                        "action": {"type": "message", "label": "查看說明", "text": "#幫助"}
                    }
                ]
            }
        }
        
        # 動態生成成員按鈕
        member_buttons_contents = []
        
        # 在資料庫中有成員的情況下生成按鈕
        if members:
            # 計算每行顯示的按鈕數量
            buttons_per_row = 2
            
            # 將成員分組，每行最多buttons_per_row個按鈕
            member_groups = [members[i:i + buttons_per_row] for i in range(0, len(members), buttons_per_row)]
            
            for member_group in member_groups:
                row_buttons = []
                for member in member_group:
                    row_buttons.append({
                        "type": "button", "style": "secondary", "height": "sm", "flex": 1,
                        "action": {"type": "message", "label": member.name, "text": f"#要新增定期 @{member.name}"}
                    })
                    # 如果一行的按鈕不足buttons_per_row個，添加空白元素補齊
                    while len(row_buttons) < buttons_per_row:
                        row_buttons.append({
                            "type": "filler"
                        })
                
                # 添加一行按鈕
                member_buttons_contents.append({
                    "type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm", "flex": 1,
                    "contents": row_buttons
                })
        else:
            # 如果沒有獲取到成員，提供一個輸入提示
            member_buttons_contents.append({
                "type": "text", "text": "請輸入: #要新增定期 @成員名稱", 
                "size": "sm", "color": "#555555", "align": "center"
            })
        
        # 添加成員輸入按鈕
        member_buttons_contents.append({
            "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
            "action": {"type": "message", "label": "手動輸入成員", "text": "#要新增定期 @"}
        })
        
        # 將生成的按鈕添加到成員選擇區
        member_section = contents["body"]["contents"][2]
        member_section["contents"].extend(member_buttons_contents)

        # 發送Flex消息
        line_bot_api.reply_message(
            reply_token,
            FlexSendMessage(alt_text="新增定期任務表單", contents=contents)
        )
    except Exception as e:
        logger.exception(f"發送定期任務新增表單失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="很抱歉，無法顯示定期任務新增表單。請直接輸入「#幫助」查看相關說明。"))

def handle_pre_add_task(reply_token: str, match: re.Match, group_id: str, user_id: str, db: Session):
    """處理表單任務新增的第一步"""
    member_name = match.group(1)
    recurrence_pattern = match.group(2)  # 這個在一般任務中應該始終為None
    
    # 獲取匹配到的完整文本
    matched_text = match.string
    
    # 獲取目前的對話狀態
    state = {}
    state_key = f"pre_add_{user_id}_{group_id}"
    
    # 狀態更新
    if member_name:
        state['member'] = member_name
        reply_text = f"已選擇成員：@{member_name}\n"
        reply_text += "請選擇任務優先級 (!低 / !普通 / !高) 或直接輸入任務內容"
    elif "!" in matched_text:
        # 解析優先級
        if "!低" in matched_text:
            state['priority'] = "low"
            priority_text = "低"
        elif "!高" in matched_text:
            state['priority'] = "high" 
            priority_text = "高"
        else:
            state['priority'] = "normal"
            priority_text = "普通"
        
        reply_text = f"已設置優先級：{priority_text}\n"
        if 'member' in state:
            reply_text += f"成員：@{state['member']}\n"
        else:
            reply_text += "請選擇或輸入成員名稱，格式：@成員名稱"
    else:
        # 如果什麼都沒選，顯示表單
        send_add_task_form(reply_token, db, group_id)
        return
    
    # 儲存狀態
    # 注意：實際應用中，你需要實現狀態儲存機制，這裡只是示例
    # storage[state_key] = state
    
    # 回覆用戶
    if 'member' in state and 'priority' in state:
        reply_text += "\n請輸入任務內容，可選擇性添加截止日期 (YYYY/MM/DD)"
        reply_text += "\n例如：完成報告 2023/12/31"
        reply_text += "\n或使用「#新增 @" + state['member'] + " !" + priority_text + " 任務內容 日期」一次性創建"
    
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_pre_recurring_task(reply_token: str, match: re.Match, group_id: str, user_id: str, db: Session):
    """處理表單定期任務新增的第一步"""
    member_name = match.group(1)
    recurrence_pattern = match.group(2)
    
    # 獲取匹配到的完整文本
    matched_text = match.string
    
    # 獲取目前的對話狀態
    state = {}
    state_key = f"pre_recurring_{user_id}_{group_id}"
    
    # 狀態更新
    if member_name:
        state['member'] = member_name
        reply_text = f"已選擇成員：@{member_name}\n"
        reply_text += "請選擇任務優先級 (!低 / !普通 / !高) 或選擇重複模式"
    elif "!" in matched_text:
        # 解析優先級
        if "!低" in matched_text:
            state['priority'] = "low"
            priority_text = "低"
        elif "!高" in matched_text:
            state['priority'] = "high" 
            priority_text = "高"
        else:
            state['priority'] = "normal"
            priority_text = "普通"
        
        reply_text = f"已設置優先級：{priority_text}\n"
        if 'member' in state:
            reply_text += f"成員：@{state['member']}\n"
        else:
            reply_text += "請選擇或輸入成員名稱，格式：@成員名稱"
    elif recurrence_pattern:
        state['recurrence'] = recurrence_pattern
        reply_text = f"已設置重複模式：每{recurrence_pattern}\n"
        if 'member' in state:
            reply_text += f"成員：@{state['member']}\n"
        else:
            reply_text += "請選擇或輸入成員名稱，格式：@成員名稱"
    else:
        # 如果什麼都沒選，顯示表單
        send_recurring_task_form(reply_token, db, group_id)
        return
    
    # 儲存狀態
    # 注意：實際應用中，你需要實現狀態儲存機制，這裡只是示例
    # storage[state_key] = state
    
    # 回覆用戶
    if 'member' in state and 'priority' in state and 'recurrence' in state:
        reply_text += "\n請輸入任務內容"
        reply_text += "\n例如：週會準備"
        reply_text += "\n或使用「#定期 @" + state['member'] + " !" + priority_text + " 任務內容 每" + state['recurrence'] + "」一次性創建"
    elif 'member' in state and 'priority' in state:
        reply_text += "\n請選擇重複模式 (每週一、每月1日等)"
    elif 'member' in state and 'recurrence' in state:
        reply_text += "\n請選擇優先級 (!低 / !普通 / !高)"
    
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
