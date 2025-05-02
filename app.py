# app.py (SQLAlchemy + Refactored Features - v2 SDK compatible)
from flask import Flask, request, abort, jsonify
import os
import json
import random
import re
from typing import List, Optional, Dict, Any # Added Dict, Any
from datetime import datetime, timezone, date # Import timezone, date
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
from sqlalchemy import text, or_ # Import or_
from sqlalchemy.orm import Session # Import Session for type hinting
from sqlalchemy.orm import joinedload # 新增這行
from sqlalchemy.exc import SQLAlchemyError # Import SQLAlchemyError

# --- LINE SDK Imports (v2) ---
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction # Added QuickReply etc.
)

# --- Standard Python Imports ---
# Removed redundant Optional import

# --- Application Initialization ---
app = Flask(__name__)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log', encoding='utf-8')
    ]
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
try:
    line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(CHANNEL_SECRET)
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

# --- Database Initialization ---
# Call init_db on startup to ensure tables exist in PostgreSQL
# SQLAlchemy's create_all is safe to call multiple times
try:
    init_db()
    logger.info("資料庫初始化檢查完成。")
except Exception as e:
    logger.exception(f"資料庫初始化失敗: {e}")
    # Depending on severity, you might want to exit(1) here

# --- Regex Patterns (Updated) ---
ADD_TASK_PATTERN = r'#新增\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)(?:\s+(\d{4}/\d{1,2}/\d{1,2}))?$' # Adjusted date capture
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DELETE_TASK_PATTERN = r'#刪除\s+T-(\d+)$'
EDIT_TASK_PATTERN = r'#修改\s+T-(\d+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)(?:\s*(\d{4}/\d{1,2}/\d{1,2}))?$' # Adjusted date capture
DETAIL_TASK_PATTERN = r'#詳情\s+T-(\d+)$'
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'
BATCH_ADD_TASK_PATTERN = r'#批量新增\s+@(\S+)\s*\n(.+)$' # Ensure newline for tasks
# 定期任務相關模式 (Added '天')
RECURRING_TASK_PATTERN = r'#定期\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)\s+每(週[一二三四五六日]|月\d{1,2}日|年\d{1,2}月\d{1,2}日|天)$'
CANCEL_RECURRING_PATTERN = r'#取消定期\s+T-(\d+)$'
# 新增任務引導指令
NEW_TASK_GUIDE_PATTERN = r'^#新任務$' # Simple trigger for guided flow
# 移除 PRE_ADD patterns, as forms now guide to use main commands or guided flow

# --- User Session Management (In-Memory) ---
# WARNING: This state is lost on application restart. Consider persistent storage (Redis, DB) for production.
class UserSessions:
    _sessions: Dict[str, Dict[str, Any]] = {} # Use type hints

    @classmethod
    def get_session(cls, key: str) -> Optional[Dict[str, Any]]:
        """獲取用戶會話"""
        # TODO: Add session expiration logic if needed
        return cls._sessions.get(key)

    @classmethod
    def set_session(cls, key: str, data: Dict[str, Any]):
        """設置用戶會話"""
        cls._sessions[key] = data
        logger.debug(f"Session set for {key}: {data}")

    @classmethod
    def clear_session(cls, key: str):
        """清除用戶會話"""
        if key in cls._sessions:
            del cls._sessions[key]
            logger.debug(f"Session cleared for {key}")

    @classmethod
    def update_session(cls, key: str, update_data: Dict[str, Any]):
        """更新用戶會話中的特定鍵值"""
        if key in cls._sessions:
            cls._sessions[key].update(update_data)
            logger.debug(f"Session updated for {key}: {cls._sessions[key]}")
        else:
            # Or create a new session if it doesn't exist? Depends on desired behavior.
            # cls.set_session(key, update_data)
            logger.warning(f"Attempted to update non-existent session: {key}")


# --- Flask Routes ---

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook Callback Handler"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
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
    db_ok = False
    db_error = None
    try:
        with get_db() as db:
            db.execute(text("SELECT 1"))
            db_ok = True
    except Exception as e:
        logger.error(f"Ping DB check failed: {e}")
        db_error = str(e)

    return jsonify({
        "status": "ok",
        "message": "LINE Bot is running (v2 SDK + SQLAlchemy + Refactored)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db_connection": "ok" if db_ok else "error",
        "db_error": db_error if db_error else None
    })


# --- LINE Event Handlers ---

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    """Handles incoming text messages, routes to commands or conversation"""
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    group_id = None

    if event.source.type == 'group':
        group_id = event.source.group_id
    elif event.source.type == 'room':
        group_id = event.source.room_id # Handle rooms too
    # Ignore direct messages to the bot for now
    if not group_id:
        logger.info(f"Ignoring message from non-group/room source (User ID: {user_id})")
        # try:
        #     line_bot_api.reply_message(reply_token, TextSendMessage(text="請在群組或房間內使用此機器人。"))
        # except Exception:
        #     pass # Ignore reply error in non-group context
        return

    logger.info(f"Received from Group/Room ID {group_id} by User ID {user_id}: {text}")

    session_key = f"{user_id}_{group_id}"
    user_session = UserSessions.get_session(session_key)

    try:
        with get_db() as db:
            # --- 1. Check for Active Conversation ---
            if user_session and user_session.get('state'):
                logger.debug(f"Handling conversation state for {session_key}: {user_session}")
                handled_in_conversation = handle_conversation_state(text, user_session, group_id, user_id, db, reply_token)
                if handled_in_conversation:
                    return # Stop further processing if handled by conversation logic

            # --- 2. Match Standard Commands ---
            add_match = re.match(ADD_TASK_PATTERN, text)
            complete_match = re.match(COMPLETE_TASK_PATTERN, text)
            list_match = re.match(LIST_TASK_PATTERN, text)
            delete_match = re.match(DELETE_TASK_PATTERN, text)
            edit_match = re.match(EDIT_TASK_PATTERN, text)
            detail_match = re.match(DETAIL_TASK_PATTERN, text)
            draw_match = re.match(DRAW_LOTS_PATTERN, text)
            pick_match = re.match(RANDOM_PICK_PATTERN, text)
            batch_add_match = re.match(BATCH_ADD_TASK_PATTERN, text, re.DOTALL) # Use DOTALL for multiline tasks
            recurring_match = re.match(RECURRING_TASK_PATTERN, text)
            cancel_recurring_match = re.match(CANCEL_RECURRING_PATTERN, text)
            new_task_guide_match = re.match(NEW_TASK_GUIDE_PATTERN, text) # Match #新任務

            if add_match:
                handle_add_task(reply_token, add_match, group_id, user_id, db)
            elif complete_match:
                handle_complete_task(reply_token, complete_match, user_id, db)
            elif list_match:
                handle_list_tasks(reply_token, list_match, group_id, db)
            elif delete_match:
                handle_delete_task(reply_token, delete_match, group_id, user_id, db)
            elif edit_match:
                handle_edit_task(reply_token, edit_match, group_id, user_id, db)
            elif detail_match:
                handle_task_details(reply_token, detail_match, db)
            elif draw_match:
                handle_draw_lots(reply_token, draw_match)
            elif pick_match:
                handle_random_pick(reply_token, pick_match)
            elif batch_add_match:
                handle_batch_add_tasks(reply_token, batch_add_match, group_id, user_id, db)
            elif recurring_match:
                handle_recurring_task(reply_token, recurring_match, group_id, user_id, db)
            elif cancel_recurring_match:
                handle_cancel_recurring_task(reply_token, cancel_recurring_match, group_id, user_id, db)
            elif new_task_guide_match:
                # Start the guided task creation flow
                UserSessions.set_session(session_key, {
                    'state': 'creating_task',
                    'step': 'get_content'
                })
                line_bot_api.reply_message(reply_token, TextSendMessage(text="好的，請輸入要新增的任務內容："))
            elif text == "#幫助":
                send_help_message(reply_token)
            elif text == "#幫助新增":
                send_add_help_message(reply_token)
            elif text.startswith("#編輯幫助 T-"):
                task_id_str_match = re.match(r'#編輯幫助 T-(\d+)', text)
                if task_id_str_match:
                    send_edit_help_message(reply_token, task_id_str_match.group(1))
                else:
                     line_bot_api.reply_message(reply_token, TextSendMessage(text="指令格式錯誤，請使用 #編輯幫助 T-任務ID"))
            elif text == "#新增表單": # Keep this to show the form for reference
                 send_add_task_form(reply_token, db, group_id)
            elif text == "#定期表單": # Keep this to show the form for reference
                 send_recurring_task_form(reply_token, db, group_id)
            # Removed simple template commands, forms are now informational
            # elif text.startswith("@"): # Removed ambiguous @ trigger
            #     pass
            else:
                # --- Placeholder for future OpenAI NLP ---
                logger.info(f"Message from {user_id} in {group_id} did not match known command or active conversation.")
                # Optionally send a reply only if it's a direct mention or specific pattern
                # line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別指令，請輸入 #幫助 查看可用指令，或使用 #新任務 引導式新增。"))
                pass # Avoid replying to every message

    except SQLAlchemyError as db_err:
        logger.exception(f"資料庫操作時發生錯誤 (User: {user_id}, Group: {group_id}, Text: {text}): {db_err}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生資料庫錯誤，請稍後再試或聯繫管理員。"))
        except Exception as reply_err:
            logger.error(f"回覆資料庫錯誤訊息時也發生錯誤: {str(reply_err)}")
    except Exception as e:
        logger.exception(f"處理指令 '{text}' 時發生未預期錯誤 (User: {user_id}, Group: {group_id}): {str(e)}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生內部錯誤，請稍後再試或聯繫管理員。"))
        except Exception as reply_err:
            logger.error(f"回覆內部錯誤訊息時也發生錯誤: {str(reply_err)}")

# --- Conversation Handling Logic ---

def handle_conversation_state(text: str, user_session: Dict[str, Any], group_id: str, user_id: str, db: Session, reply_token: str) -> bool:
    """
    處理對話狀態下的用戶輸入 (e.g., for guided task creation).
    Returns True if the message was handled within the conversation, False otherwise.
    """
    state = user_session.get('state')
    step = user_session.get('step')
    session_key = f"{user_id}_{group_id}"

    logger.debug(f"Handling conversation: state={state}, step={step}, input='{text}'")

    if state == 'creating_task':
        if step == 'get_content':
            # User entered the task content
            user_session['content'] = text
            user_session['step'] = 'get_member'
            UserSessions.set_session(session_key, user_session)
            # Ask for member (potentially show buttons later)
            line_bot_api.reply_message(reply_token, TextSendMessage(text="收到內容！請 @提及 負責人 或直接輸入成員名稱："))
            return True

        elif step == 'get_member':
            # User entered member name (might start with @)
            member_name = text.lstrip('@').strip()
            if not member_name:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="成員名稱不可為空，請重新輸入："))
                return True

            # Check if member exists, create if not (or ask for confirmation?)
            member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
            if not member:
                 logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，將於任務創建時自動建立。")
                 # We store the name now, and create the member record just before creating the task
                 # This avoids creating members if the user cancels the flow later.
            user_session['member_name'] = member_name
            user_session['step'] = 'get_priority'
            UserSessions.set_session(session_key, user_session)
            # Ask for priority using buttons
            send_priority_selection(reply_token, member_name, user_session['content'])
            return True

        elif step == 'get_priority':
            # User selected priority via button or text
            priority = "normal" # Default
            priority_map = {"低": "low", "普通": "normal", "高": "high"}
            selected_priority = None
            for key, value in priority_map.items():
                if key in text:
                    selected_priority = value
                    break

            if selected_priority:
                user_session['priority'] = selected_priority
                user_session['step'] = 'get_due_date'
                UserSessions.set_session(session_key, user_session)
                # Ask for due date
                send_due_date_inquiry(reply_token, user_session['member_name'], user_session['content'], selected_priority)
            else:
                # Invalid input for priority
                line_bot_api.reply_message(reply_token, TextSendMessage(text="請點擊按鈕或輸入有效優先級 (低 / 普通 / 高):"))
            return True # Still handled within conversation

        elif step == 'get_due_date':
            # User entered due date or "無"
            due_date = None
            if text.lower() in ["無", "沒有", "skip", "跳過", "no", "-"]:
                # No due date provided
                pass
            else:
                # Try parsing date
                try:
                    due_date = datetime.strptime(text, "%Y/%m/%d")
                except ValueError:
                    # Invalid date format
                    line_bot_api.reply_message(
                        reply_token,
                        TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式，或輸入「無」跳過。")
                    )
                    return True # Still handled

            # All info gathered, create the task
            create_conversation_task(reply_token, user_session, group_id, db, due_date)
            UserSessions.clear_session(session_key) # End conversation
            return True

    # --- Add handlers for other states like 'creating_recurring_task' if needed ---
    # elif state == 'creating_recurring_task':
    #     # ... logic for recurring task steps ...
    #     pass

    logger.debug(f"Input '{text}' did not match active conversation state/step for {session_key}")
    return False # Input didn't match expected conversation step


# --- Helper Functions for Conversation Flow ---

def send_priority_selection(reply_token: str, member_name: str, task_content: str):
    """發送優先級選擇 Flex 訊息 (Quick Reply buttons might be better for mobile)"""
    # Using QuickReply for better mobile experience
    priority_text = "普通"
    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(
                text=f"好的，任務內容：\n「{task_content}」\n負責人：@{member_name}\n\n請選擇任務優先級：",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🟢 低", text="低")),
                    QuickReplyButton(action=MessageAction(label="🟡 普通", text="普通")),
                    QuickReplyButton(action=MessageAction(label="🔴 高", text="高")),
                ])
            )
        )
    except Exception as e:
        logger.exception(f"發送優先級選擇 QuickReply 失敗: {e}")
        # Fallback to text? Or just log the error.


def send_due_date_inquiry(reply_token: str, member_name: str, task_content: str, priority: str):
    """發送截止日期詢問訊息 (using Quick Reply)"""
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    priority_display = priority_map_display.get(priority, priority)

    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(
                text=f"任務內容：\n「{task_content}」\n負責人：@{member_name}\n優先級：{priority_display}\n\n請輸入截止日期 (格式：YYYY/MM/DD)，或點擊下方按鈕。",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="無截止日期", text="無")),
                    # TODO: Add Quick Reply Buttons for common dates like "Today", "Tomorrow"?
                    # Requires date calculation logic. Example:
                    # QuickReplyButton(action=MessageAction(label="今天", text=date.today().strftime('%Y/%m/%d'))),
                    # QuickReplyButton(action=MessageAction(label="明天", text=(date.today() + timedelta(days=1)).strftime('%Y/%m/%d'))),
                ])
            )
        )
    except Exception as e:
         logger.exception(f"發送截止日期詢問 QuickReply 失敗: {e}")


def create_conversation_task(reply_token: str, user_session: Dict[str, Any], group_id: str, db: Session, due_date: Optional[datetime]):
    """根據對話狀態 (user_session) 創建任務"""
    member_name = user_session.get('member_name')
    task_content = user_session.get('content')
    priority = user_session.get('priority', 'normal') # Default to normal if somehow missed

    if not member_name or not task_content:
        logger.error(f"會話狀態不完整，無法創建任務: {user_session}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，任務資訊不完整，無法新增。請重新開始。"))
        return

    # Get or create member just before task creation
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        try:
            member = create_member(db, name=member_name, group_id=group_id)
            # Need to flush or commit here if create_member doesn't commit itself
            # Assuming create_member adds and commits/flushes
            logger.info(f"自動建立成員成功: ID {member.id}")
        except Exception as create_err:
            logger.exception(f"自動建立成員 '{member_name}' 失敗: {create_err}")
            db.rollback()
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"自動建立成員 '{member_name}' 失敗，無法新增任務。"))
            return

    try:
        task = create_task(db, member_id=member.id, content=task_content, due_date=due_date, priority=priority)
        task_id_str = f"T-{task.id}"

        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
        priority_display = priority_map_display.get(priority, priority)

        reply_text = f"✅ 已為 @{member.name} 新增任務！\n"
        reply_text += f"內容：{task.content}\n"
        reply_text += f"任務ID：{task_id_str}\n"
        reply_text += f"優先級：{priority_display}\n"
        reply_text += f"截止：{due_date.strftime('%Y/%m/%d') if due_date else '無'}"

        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

    except SQLAlchemyError as db_err:
         logger.exception(f"從會話新增任務到資料庫時失敗: {db_err}")
         db.rollback()
         line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (資料庫錯誤)，請稍後再試。"))
    except Exception as e:
        logger.exception(f"從會話創建任務時發生未知錯誤: {e}")
        # Rollback might be needed if create_task doesn't handle its own transaction fully
        try:
            db.rollback()
        except Exception:
            pass
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (內部錯誤)，請稍後再試。"))


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

# --- handle_add_task, handle_complete_task etc. remain largely the same ---
# ... (Keep existing handle_add_task, handle_complete_task, handle_list_tasks, handle_delete_task, handle_edit_task, handle_task_details)
# ... (Keep existing handle_draw_lots, handle_random_pick)

# --- Minor adjustments potentially needed in handle_... functions if model interactions changed ---
# Example: Ensure member lookup/creation is robust if needed outside conversational flow

def handle_add_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """Handles add task command using SQLAlchemy"""
    try:
        member_name = match.group(1)
        priority_tag = match.group(2)
        task_content = match.group(3).strip()
        due_date_str = match.group(4)

        if not member_name or not task_content:
            logger.warning(f"新增任務時缺少必要參數: member_name={member_name}, task_content={task_content}")
            line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗：缺少必要參數"))
            return

        # 處理優先級標籤
        priority = "normal"
        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
        if priority_tag:
            if "低" in priority_tag:
                priority = "low"
            elif "高" in priority_tag:
                priority = "high"

        due_date = parse_date(due_date_str)
        if due_date_str and due_date is None:
            logger.warning(f"日期格式不正確: {due_date_str}")
            line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式。"))
            return

        member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
        if not member:
            logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
            try:
                member = create_member(db, name=member_name, group_id=group_id)
            except Exception as create_err:
                logger.exception(f"自動建立成員 '{member_name}' 失敗: {create_err}")
                db.rollback()
                line_bot_api.reply_message(reply_token, TextSendMessage(text=f"自動建立成員 '{member_name}' 失敗，無法新增任務。"))
                return

        try:
            task = create_task(db, member_id=member.id, content=task_content, due_date=due_date, priority=priority)
            task_id_str = f"T-{task.id}"
            priority_display = priority_map_display.get(priority, priority)

            reply_text = f"✅ 已為 @{member.name} 新增任務：\n"
            reply_text += f"內容：{task.content}\n"
            reply_text += f"任務ID：{task_id_str}\n"
            reply_text += f"優先級：{priority_display}\n"
            reply_text += f"截止：{due_date.strftime('%Y/%m/%d') if due_date else '無'}"

            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

        except SQLAlchemyError as db_err:
            logger.exception(f"新增任務到資料庫時失敗: {db_err}")
            db.rollback()
            line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (資料庫錯誤)，請稍後再試。"))
        except Exception as e:
            logger.exception(f"新增任務時發生未知錯誤: {e}")
            db.rollback()
            line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (內部錯誤)，請稍後再試。"))

    except Exception as e:
        logger.exception(f"處理新增任務指令時發生錯誤: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="處理指令時發生錯誤，請稍後再試。"))

def handle_complete_task(reply_token: str, match: re.Match, completer_user_id: str, db: Session):
    """Handles complete task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # Optional: Add permission check - e.g., only assigned member or adder can complete?
    # elif task.member.line_user_id != completer_user_id: # Requires line_user_id in Member model
    #     reply_text = f"❌ 您無法完成指派給 {task.member.name} 的任務。"
    elif task.status == 'completed':
        reply_text = f"ℹ️ 任務 T-{task_id_num} ({task.content[:15]}...) 已經是完成狀態。"
    else:
        try:
            task.status = 'completed'
            # Store timezone-aware datetime UTC
            task.completed_at = datetime.now(timezone.utc)
            db.commit() # Commit the change for this task
            reply_text = f"🎉 已將 {task.member.name} 的任務 T-{task_id_num} 標記為完成！\n內容：{task.content}"
        except SQLAlchemyError as e:
            logger.exception(f"更新任務 T-{task_id_num} 狀態時失敗 (DB): {e}")
            db.rollback()
            reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗 (資料庫錯誤)。"
        except Exception as e:
            logger.exception(f"更新任務 T-{task_id_num} 狀態時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗 (內部錯誤)。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_list_tasks(reply_token: str, match: re.Match, group_id: str, db: Session):
    """Handles list tasks command using SQLAlchemy"""
    member_name = match.group(1)
    tasks: List[Task] = []
    title = ""

    try:
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
            logger.exception(f"創建或發送 Flex 消息失敗，將使用文字列表: {str(e)}")
            task_list_text = create_task_list_text(title, tasks, db) # Pass db if needed
            # Split long text messages if necessary
            max_len = 4900 # LINE message length limit is 5000
            messages_to_send = []
            while len(task_list_text) > max_len:
                 split_pos = task_list_text.rfind('\n\n', 0, max_len)
                 if split_pos == -1: # Cannot split nicely, just cut
                     split_pos = max_len
                 messages_to_send.append(TextSendMessage(text=task_list_text[:split_pos]))
                 task_list_text = task_list_text[split_pos:].lstrip()
            messages_to_send.append(TextSendMessage(text=task_list_text))
            line_bot_api.reply_message(reply_token, messages=messages_to_send)

    except SQLAlchemyError as e:
         logger.exception(f"列出任務時資料庫查詢失敗: {e}")
         line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢任務列表時發生資料庫錯誤。"))
    except Exception as e:
         logger.exception(f"列出任務時發生未知錯誤: {e}")
         line_bot_api.reply_message(reply_token, TextSendMessage(text="處理列表請求時發生內部錯誤。"))


def handle_delete_task(reply_token: str, match: re.Match, group_id: str, deleter_user_id: str, db: Session):
    """Handles delete task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # Optional: Add permission check (e.g., only creator or admins?)
    elif task.member.group_id != group_id: # Basic check: task belongs to this group
         reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    else:
        try:
            task_content_preview = task.content[:20] # For confirmation message
            member_name = task.member.name
            db.delete(task) # Delete the task object
            db.commit()
            reply_text = f"🗑️ 已成功刪除 @{member_name} 的任務 T-{task_id_num} ({task_content_preview}...)。"
        except SQLAlchemyError as e:
            logger.exception(f"刪除任務 T-{task_id_num} 時失敗 (DB): {e}")
            db.rollback()
            reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗 (資料庫錯誤)。"
        except Exception as e:
            logger.exception(f"刪除任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗 (內部錯誤)。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_edit_task(reply_token: str, match: re.Match, group_id: str, editor_user_id: str, db: Session):
    """Handles edit task command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    priority_tag = match.group(2)
    new_content = match.group(3).strip()
    new_due_date_str = match.group(4)

    task = get_task_by_id(db, task_id=task_id_num)
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}


    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif task.member.group_id != group_id: # Basic check: task belongs to this group
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    # Optional: Add permission check
    else:
        updates = {}
        if new_content:
             updates['content'] = new_content
        else:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="❌ 修改任務時，任務內容不能為空。"))
            return

        # Handle optional due date
        new_due_date = None
        if new_due_date_str:
             new_due_date = parse_date(new_due_date_str)
             if new_due_date is None:
                 line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD 格式。"))
                 return
             updates['due_date'] = new_due_date
        elif new_due_date_str is None and len(match.groups()) > 3: # Check if date group was matched at all
             # If date group exists but is empty implicitly (e.g. user provided space but no date), handle potentially?
             # Current regex makes date optional at the end, so empty match means no date update or remove date?
             # Let's assume omitting the date means no change, adding '無' or similar could remove it.
             # For simplicity now: If date_str is None from regex, don't update date.
             pass
             # If explicit removal is needed, add a keyword like '無日期'
             # elif new_content.endswith(" 無日期"): ... updates['due_date'] = None ... remove tag from content
        else:
            # No date string provided, keep existing due date
             pass


        # 處理優先級標籤
        if priority_tag:
            if "低" in priority_tag:
                updates['priority'] = "low"
            elif "高" in priority_tag:
                updates['priority'] = "high"
            else: # "!普通"
                updates['priority'] = "normal"

        if not updates:
             line_bot_api.reply_message(reply_token, TextSendMessage(text="ℹ️ 沒有提供任何有效的修改內容。"))
             return

        try:
            original_content = task.content
            original_priority = task.priority
            original_due_date = task.due_date

            if 'content' in updates: task.content = updates['content']
            if 'priority' in updates: task.priority = updates['priority']
            # Handle date update carefully: only update if explicitly parsed
            if 'due_date' in updates: task.due_date = updates['due_date']
            # Add updated_at timestamp if model has it
            # task.updated_at = datetime.now(timezone.utc)

            db.commit()

            priority_display = priority_map_display.get(task.priority, task.priority)
            due_date_text = f"截止：{task.due_date.strftime('%Y/%m/%d')}" if task.due_date else "截止：無"

            reply_text = f"✏️ 已更新任務 T-{task_id_num} (@{task.member.name})：\n"
            reply_text += f"內容：{task.content}\n"
            reply_text += f"優先級：{priority_display}\n"
            reply_text += f"{due_date_text}"

        except SQLAlchemyError as e:
            logger.exception(f"修改任務 T-{task_id_num} 時失敗 (DB): {e}")
            db.rollback()
            reply_text = f"❌ 修改任務 T-{task_id_num} 失敗 (資料庫錯誤)。"
        except Exception as e:
            logger.exception(f"修改任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 修改任務 T-{task_id_num} 失敗 (內部錯誤)。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_task_details(reply_token: str, match: re.Match, db: Session):
    """Handles show task details command using SQLAlchemy"""
    task_id_num = int(match.group(1))
    try:
        # Use joinedload to efficiently fetch the member
        task = db.query(Task).options(joinedload(Task.member)).filter(Task.id == task_id_num).first()

        if not task:
            reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            return

        # Use timezone-aware formatting if available
        local_tz = timezone.utc # Default to UTC, consider making this configurable
        created_at_str = task.created_at.astimezone(local_tz).strftime('%Y/%m/%d %H:%M') if task.created_at else "未知"
        due_date_str = task.due_date.strftime('%Y/%m/%d') if task.due_date else "無"
        status_str = "✅ 已完成" if task.status == 'completed' else "⏳ 待辦中"
        completed_at_str = task.completed_at.astimezone(local_tz).strftime('%Y/%m/%d %H:%M') if task.completed_at else ""

        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
        priority_display = priority_map_display.get(task.priority, task.priority)
        priority_color = "#28a745" if task.priority == "low" else "#ffc107" if task.priority == "normal" else "#dc3545"
        status_color = "#28a745" if task.status == "completed" else "#ffc107"

        # 處理定期任務信息
        recurring_info = []
        if task.is_recurring:
            pattern_text = format_recurrence_pattern(task.recurrence_pattern) # Use helper
            recurring_info.append({"type": "separator", "margin": "md"})
            recurring_info.append({"type": "text", "text": f"⏰ 定期任務 ({pattern_text})", "size": "sm", "color": "#9C27B0", "margin": "sm"})
            recurring_info.append({"type": "text", "text": f"(已生成 {task.recurrence_count} 次)", "size": "xs", "color": "#9C27B0", "margin": "none"})
        elif task.parent_task_id:
            parent_task = get_task_by_id(db, task_id=task.parent_task_id) # Maybe cache this?
            if parent_task:
                 parent_pattern_text = format_recurrence_pattern(parent_task.recurrence_pattern)
                 recurring_info.append({"type": "separator", "margin": "md"})
                 recurring_info.append({"type": "text", "text": f"🔄 定期任務衍生 (來自 T-{parent_task.id})", "size": "sm", "color": "#757575", "margin": "sm", "wrap": True})
                 recurring_info.append({"type": "text", "text": f"({parent_pattern_text})", "size": "xs", "color": "#757575", "margin": "none"})


        # 創建 Flex 訊息以添加快捷操作按鈕
        try:
            contents = {
                "type": "bubble",
                "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"任務詳情 T-{task_id_num}", "weight": "bold", "size": "lg"}]},
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "md",
                    "contents": [
                        {"type": "text", "text": task.content, "wrap": True, "weight": "bold", "size": "xl"},
                        {"type": "box", "layout": "baseline", "margin": "md", "contents": [
                            {"type": "text", "text": "負責人:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"},
                            {"type": "text", "text": f"@{task.member.name}", "size": "sm", "color": "#1DB446", "flex": 4, "weight":"bold"}
                        ]},
                        {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                            {"type": "text", "text": "優先級:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"},
                            {"type": "text", "text": priority_display, "size": "sm", "color": priority_color, "flex": 4, "weight":"bold"}
                        ]},
                        {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                            {"type": "text", "text": "狀態:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"},
                            {"type": "text", "text": status_str + (f" ({completed_at_str})" if task.status == 'completed' and completed_at_str else ""), "size": "sm", "color": status_color, "flex": 4, "weight":"bold", "wrap":True}
                        ]},
                        {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                            {"type": "text", "text": "截止日期:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"},
                            {"type": "text", "text": due_date_str, "size": "sm", "color": "#888888", "flex": 4}
                        ]},
                        {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                            {"type": "text", "text": "建立時間:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"},
                            {"type": "text", "text": created_at_str, "size": "sm", "color": "#888888", "flex": 4}
                        ]},
                        # Add recurring info here if it exists
                        *recurring_info
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        # Action Buttons
                    ]
                }
            }

            footer_buttons = contents["footer"]["contents"]

            # Add Complete button only if task is pending
            if task.status == 'pending':
                 footer_buttons.append({
                     "type": "button", "style": "primary", "color": "#28a745", "height": "sm",
                     "action": {"type": "message", "label": "✅ 完成任務", "text": f"#完成 T-{task_id_num}"}
                 })

            # Add Edit/Delete buttons
            footer_buttons.append({
                "type": "box", "layout":"horizontal", "spacing":"sm", "contents":[
                     {
                        "type": "button", "style": "secondary", "color": "#ffc107", "height": "sm", "flex": 1,
                        "action": {"type": "message", "label": "✏️ 編輯", "text": f"#編輯幫助 T-{task_id_num}"} # Link to help first
                     },
                     {
                        "type": "button", "style": "secondary", "color": "#dc3545", "height": "sm", "flex": 1,
                        "action": {"type": "message", "label": "🗑️ 刪除", "text": f"#刪除 T-{task_id_num}"}
                     }
                ]
            })


            # If it's a recurring master task, add Cancel Recurring button
            if task.is_recurring:
                footer_buttons.append({
                    "type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm",
                    "action": {"type": "message", "label": "🚫 取消定期", "text": f"#取消定期 T-{task_id_num}"}
                })

            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=f"任務 T-{task_id_num} 詳情", contents=contents))
            return # Successfully sent Flex message

        except Exception as e:
            logger.exception(f"創建任務詳情 Flex 訊息失敗: {e}")
            # Fallback to text message if Flex fails
            reply_text = f"🔍 任務詳情 T-{task_id_num} 🔍\n"
            reply_text += f"內容：{task.content}\n"
            reply_text += f"負責人：@{task.member.name}\n"
            reply_text += f"優先級：{priority_display}\n"
            if task.is_recurring:
                pattern_text = format_recurrence_pattern(task.recurrence_pattern)
                reply_text += f"⏰ 定期任務：{pattern_text} (已重複 {task.recurrence_count} 次)\n"
            elif task.parent_task_id:
                 reply_text += f"🔄 定期任務衍生：來自 T-{task.parent_task_id}\n"
            reply_text += f"狀態：{status_str}"
            if task.status == 'completed' and completed_at_str:
                reply_text += f" (於 {completed_at_str})\n"
            else:
                reply_text += "\n"
            reply_text += f"建立時間：{created_at_str}\n"
            reply_text += f"截止日期：{due_date_str}\n\n"
            reply_text += f"操作：#完成 T-{task_id_num} | #編輯幫助 T-{task_id_num} | #刪除 T-{task_id_num}"
            if task.is_recurring:
                 reply_text += f" | #取消定期 T-{task_id_num}"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

    except SQLAlchemyError as e:
        logger.exception(f"獲取任務詳情 T-{task_id_num} 時失敗 (DB): {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"查詢任務 T-{task_id_num} 詳情時發生資料庫錯誤。"))
    except Exception as e:
        logger.exception(f"獲取任務詳情 T-{task_id_num} 時失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"查詢任務 T-{task_id_num} 詳情時發生內部錯誤。"))


def format_recurrence_pattern(system_pattern: Optional[str]) -> str:
    """Converts internal recurrence pattern string to user-friendly text."""
    if not system_pattern:
        return "無"

    day_map_reverse = {
        "monday": "週一", "tuesday": "週二", "wednesday": "週三",
        "thursday": "週四", "friday": "週五", "saturday": "週六", "sunday": "週日"
    }

    if system_pattern == "daily":
        return "每天"
    elif system_pattern.startswith("weekly_"):
        day_en = system_pattern.split("_")[1]
        return f"每{day_map_reverse.get(day_en, day_en)}"
    elif system_pattern.startswith("monthly_"):
        day = system_pattern.split("_")[1]
        return f"每月{day}日"
    elif system_pattern.startswith("yearly_"):
        parts = system_pattern.split("_")
        if len(parts) >= 3:
            month, day = parts[1], parts[2]
            return f"每年{month}月{day}日"
    return system_pattern # Fallback


def handle_draw_lots(reply_token: str, match: re.Match):
    """Handles draw lots command"""
    question = match.group(1)
    results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]
    result = random.choice(results)
    reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"

    # 創建擲筊結果的 Flex 訊息 (Keep existing Flex logic)
    # ... (Flex message generation as before) ...
    try:
        result_emoji = "👍" if "聖筊" in result else "👎" if "陰筊" in result else "🤔"
        result_color = "#28a745" if "聖筊" in result else "#dc3545" if "陰筊" in result else "#ffc107"

        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "擲筊結果", "weight": "bold", "size": "lg"}]},
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"問題: {question}", "wrap": True, "weight": "bold", "size": "md", "margin":"md"},
                    {"type": "box", "layout": "vertical", "margin": "xl", "contents": [
                        {"type": "text", "text": result, "size": "xxl", "align": "center", "color": result_color, "weight": "bold"}
                    ]},
                ]
            },
            "footer": {
                 "type": "box", "layout": "vertical", "spacing":"sm", "contents": [
                     {
                        "type": "button", "style": "primary", "color": result_color, "height": "sm",
                        "action": {"type": "message", "label": f"再擲一次 {result_emoji}", "text": f"#擲筊 {question}"}
                     }
                ]
            }
        }
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建擲筊 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text)) # Fallback

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

    # 創建抽籤結果的 Flex 訊息 (Keep existing Flex logic)
    # ... (Flex message generation as before) ...
    try:
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "抽籤結果", "weight": "bold", "size": "lg"}]},
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"從 {len(options)} 個選項中抽出：", "size": "md", "color": "#555555", "wrap":True, "margin":"md"},
                    {"type": "box", "layout": "vertical", "margin": "xl", "contents": [
                        {"type": "text", "text": chosen, "size": "xxl", "align": "center", "weight": "bold", "wrap": True, "color":"#2196F3"}
                    ]},
                 ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "spacing":"sm",
                "contents": [
                     {"type": "text", "text": f"選項: {', '.join(options)}", "size": "xs", "color": "#888888", "wrap": True, "margin":"md"},
                     {"type": "separator", "margin":"md"},
                     {
                        "type": "button", "style": "primary", "color": "#2196F3", "height": "sm",
                        "action": {"type": "message", "label": "再抽一次", "text": f"#抽籤 {options_text}"}
                     }
                ]
            }
        }
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建抽籤 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text)) # Fallback


# --- Batch Add Task Handling (Improved Parsing & Feedback) ---
def handle_batch_add_tasks(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """處理批量添加任務的命令 (Improved Parsing & Error Handling)"""
    member_name = match.group(1)
    tasks_text = match.group(2).strip()

    task_lines = [line.strip() for line in tasks_text.split('\n') if line.strip()]

    if not task_lines:
        reply_text = (
            "📝 批量新增任務格式說明：\n\n"
            "`#批量新增 @成員名稱`\n"
            "`[!優先級] 任務內容1 [YYYY/MM/DD]`\n"
            "`[!優先級] 任務內容2 [YYYY/MM/DD]`\n"
            "`任務內容3`\n"
            "...\n\n"
            "說明：\n"
            "- 請將指令和任務列表分開，指令獨佔一行。\n"
            "- 每行一個任務。\n"
            "- 優先級 (!低/!普通/!高) 和 截止日期 (YYYY/MM/DD) 可選。\n"
            "- 優先級標籤必須在行首。\n"
            "- 截止日期必須在行尾，且與內容用空格隔開。\n\n"
            "📋 範例：\n"
            "`#批量新增 @小明`\n"
            "`!高 完成專案報告 2025/12/31`\n"
            "`!普通 整理文件`\n"
            "`安排會議 2025/12/15`\n"
            "`!低 訂購下午茶`"
        )
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        return

    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        try:
             member = create_member(db, name=member_name, group_id=group_id)
        except Exception as create_err:
            logger.exception(f"自動建立成員 '{member_name}' 失敗: {create_err}")
            db.rollback()
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"自動建立成員 '{member_name}' 失敗，無法新增任務。"))
            return

    created_tasks_info = [] # Store dicts: {'id':task.id, 'summary': str}
    failed_lines_info = [] # Store dicts: {'line': str, 'error': str}
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}

    tasks_to_add = [] # Collect Task objects before adding to DB

    for i, task_line in enumerate(task_lines):
        logger.debug(f"Processing batch line {i+1}: '{task_line}'")
        priority = "normal"
        content = task_line
        due_date_str = None
        due_date = None
        error_msg = None

        # 1. Check for priority tag at the beginning
        priority_match = re.match(r'^!(低|普通|高)\s+(.+)$', task_line)
        if priority_match:
            p_tag = priority_match.group(1)
            content = priority_match.group(2).strip() # Content after priority tag
            if p_tag == "低": priority = "low"
            elif p_tag == "高": priority = "high"
            else: priority = "normal"
            logger.debug(f"  Priority found: {priority}, Remaining content: '{content}'")
        else:
             # No priority tag, content is the whole line for now
             logger.debug("  No priority tag found.")
             content = task_line.strip()


        # 2. Check for date at the end of the *remaining* content
        # Use regex to find date at the very end, preceded by space or start of string
        date_match = re.search(r'(?:^|\s)(\d{4}/\d{1,2}/\d{1,2})$', content)
        if date_match:
            due_date_str = date_match.group(1)
            # Remove date and preceding space (if any) from content
            content = content[:date_match.start()].strip()
            logger.debug(f"  Date found: {due_date_str}, Remaining content: '{content}'")
            due_date = parse_date(due_date_str)
            if due_date is None:
                error_msg = f"日期格式錯誤 ({due_date_str})"
                logger.warning(f"  Invalid date format: {due_date_str}")
        else:
             logger.debug("  No date found at the end.")

        # 3. Validate content
        if not content:
            error_msg = "任務內容為空"
            logger.warning("  Empty content after parsing.")

        # 4. Create Task object if no errors so far
        if error_msg:
            failed_lines_info.append({'line': task_line, 'error': error_msg})
        else:
            try:
                # Create Task object but don't add to session yet
                task_obj = Task(
                    member_id=member.id,
                    content=content,
                    due_date=due_date,
                    priority=priority,
                    status='pending'
                 )
                tasks_to_add.append(task_obj)
                # Prepare summary for successful preview (ID will be assigned after commit)
                priority_display = priority_map_display.get(priority, priority)
                task_summary = f"{priority_display} {content}"
                if due_date:
                    task_summary += f" (截止: {due_date.strftime('%Y/%m/%d')})"
                # Store summary temporarily; ID added later
                created_tasks_info.append({'summary_no_id': task_summary, 'obj': task_obj})

            except Exception as e:
                 # Should not happen here if validation is good, but as fallback
                 logger.exception(f"批量任務對象創建時未知錯誤: {e}")
                 failed_lines_info.append({'line': task_line, 'error': f"內部錯誤 ({type(e).__name__})"})


    # 5. Add all valid tasks to DB in one transaction
    final_summaries = []
    if tasks_to_add:
        try:
            db.add_all(tasks_to_add)
            db.flush() # Assign IDs to objects
            # Now retrieve IDs and build final summaries
            for info in created_tasks_info:
                 task_obj = info['obj']
                 if task_obj.id: # Check if ID was assigned
                      final_summaries.append(f"T-{task_obj.id}: {info['summary_no_id']}")
                 else: # Should not happen if flush worked
                      failed_lines_info.append({'line': info['summary_no_id'], 'error': "無法獲取任務ID"})

            db.commit()
            logger.info(f"批量新增 {len(final_summaries)} 個任務成功 for {member.name}.")

        except SQLAlchemyError as e:
            db.rollback()
            logger.exception(f"批量新增任務到資料庫時失敗 (DB): {e}")
            # Mark all attempted tasks as failed for this batch
            for info in created_tasks_info:
                 failed_lines_info.append({'line': info['summary_no_id'], 'error': "資料庫儲存失敗"})
            final_summaries = [] # Clear successful summaries as commit failed
        except Exception as e:
            db.rollback()
            logger.exception(f"批量新增任務到資料庫時失敗 (Unknown): {e}")
            for info in created_tasks_info:
                 failed_lines_info.append({'line': info['summary_no_id'], 'error': f"內部儲存錯誤 ({type(e).__name__})"})
            final_summaries = []

    # 6. Send Reply (Flex or Text)
    success_count = len(final_summaries)
    failure_count = len(failed_lines_info)

    if success_count == 0 and failure_count == 0:
         # This case should ideally not happen if input validation catches empty lines
         line_bot_api.reply_message(reply_token, TextSendMessage(text="未提供有效的任務內容。"))
         return

    alt_text = f"批量新增結果：成功 {success_count}, 失敗 {failure_count} (為 @{member_name})"
    try:
        bubble_contents = create_batch_add_result_bubble(member.name, final_summaries, failed_lines_info)
        line_bot_api.reply_message(
            reply_token,
            FlexSendMessage(alt_text=alt_text, contents=bubble_contents)
        )
    except Exception as flex_err:
        logger.error(f"創建批量新增結果 Flex 訊息失敗: {flex_err}")
        # Fallback to text
        reply_text = f"批量新增任務結果 (@{member.name})：\n"
        reply_text += f"✅ 成功新增 {success_count} 個任務。\n"
        if final_summaries:
            reply_text += "--- 成功列表 ---\n"
            for i, summary in enumerate(final_summaries[:15], 1): # Limit display
                reply_text += f"{i}. {summary}\n"
            if len(final_summaries) > 15:
                reply_text += f"... (共 {success_count} 個)\n"

        if failed_lines_info:
            reply_text += f"\n❌ 失敗 {failure_count} 行：\n"
            for i, failed in enumerate(failed_lines_info[:10], 1): # Limit display
                reply_text += f"- 行: \"{failed['line'][:50]}{'...' if len(failed['line']) > 50 else ''}\" -> 原因: {failed['error']}\n"
            if len(failed_lines_info) > 10:
                reply_text += f"... (共 {failure_count} 行失敗)\n"

        # Split long messages
        max_len = 4900
        messages_to_send = []
        while len(reply_text) > max_len:
             split_pos = reply_text.rfind('\n', 0, max_len)
             if split_pos == -1: split_pos = max_len
             messages_to_send.append(TextSendMessage(text=reply_text[:split_pos]))
             reply_text = reply_text[split_pos:].lstrip()
        messages_to_send.append(TextSendMessage(text=reply_text))
        line_bot_api.reply_message(reply_token, messages=messages_to_send)


def create_batch_add_result_bubble(member_name: str, success_summaries: List[str], failed_lines_info: List[Dict[str, str]]):
    """創建批量新增結果的Flex消息 (Improved)"""
    success_count = len(success_summaries)
    failure_count = len(failed_lines_info)

    header_text = f"批量新增結果 (@{member_name})"
    header_color = "#1DB446" if success_count > 0 and failure_count == 0 else \
                   "#ffc107" if success_count > 0 and failure_count > 0 else \
                   "#dc3545"

    contents = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": header_text, "weight": "bold", "size": "lg", "color": header_color}]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": f"✅ 成功: {success_count}  |  ❌ 失敗: {failure_count}", "weight": "bold", "size": "md", "wrap": True}
            ]
        },
        "footer": { # Add footer for view list button
             "type": "box",
             "layout": "vertical",
             "contents": [
                 {"type": "button", "action": {"type": "message", "label": "查看我的任務列表", "text": f"#列表 @{member_name}"}, "style": "primary", "color":"#1DB446", "height":"sm"}
            ]
        }
    }

    body_contents = contents["body"]["contents"]

    # Add successful tasks (limited)
    if success_summaries:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({"type": "text", "text": "成功新增列表:", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "md"})
        success_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for summary in success_summaries[:8]: # Limit display
            success_box["contents"].append({"type": "text", "text": f"• {summary}", "size": "sm", "wrap": True})
        if len(success_summaries) > 8:
            success_box["contents"].append({"type": "text", "text": f"... (共 {success_count} 個)", "size": "xs", "color": "#555555", "margin": "sm"})
        body_contents.append(success_box)

    # Add failed tasks (limited)
    if failed_lines_info:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({"type": "text", "text": "失敗行與原因:", "weight": "bold", "size": "sm", "color": "#dc3545", "margin": "md"})
        failed_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for failed in failed_lines_info[:5]: # Limit display
            line_preview = failed['line'][:60] + ('...' if len(failed['line']) > 60 else '')
            failed_box["contents"].append({
                "type": "box", "layout":"vertical", "margin":"xxs", "contents":[
                     {"type": "text", "text": f"行: \"{line_preview}\"", "size": "xs", "wrap": True, "color": "#555555"},
                     {"type": "text", "text": f"原因: {failed['error']}", "size": "xs", "wrap": True, "color": "#dc3545", "weight":"bold"}
                 ]
             })
        if len(failed_lines_info) > 5:
            failed_box["contents"].append({"type": "text", "text": f"... (共 {failure_count} 行失敗)", "size": "xs", "color": "#dc3545", "margin": "sm"})
        body_contents.append(failed_box)

    return contents

# --- Recurring Task Handling (Added "daily" support) ---
def handle_recurring_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    """處理新增定期任務 (Added 'daily' support)"""
    member_name = match.group(1)
    priority_tag = match.group(2)
    task_content = match.group(3).strip()
    recurrence_input = match.group(4) # e.g., 週一, 月15日, 天

    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    priority = "normal"
    if priority_tag:
        if "低" in priority_tag: priority = "low"
        elif "高" in priority_tag: priority = "high"

    system_pattern = None
    user_friendly_pattern = None

    pattern_map_week = { "週一": "weekly_monday", "週二": "weekly_tuesday", "週三": "weekly_wednesday", "週四": "weekly_thursday", "週五": "weekly_friday", "週六": "weekly_saturday", "週日": "weekly_sunday" }

    if recurrence_input == "天":
        system_pattern = "daily"
        user_friendly_pattern = "每天"
    elif recurrence_input in pattern_map_week:
        system_pattern = pattern_map_week[recurrence_input]
        user_friendly_pattern = f"每{recurrence_input}"
    elif recurrence_input.startswith("月") and recurrence_input.endswith("日"):
        day_str = recurrence_input[1:-1]
        if day_str.isdigit() and 1 <= int(day_str) <= 31:
            system_pattern = f"monthly_{int(day_str)}" # Store as number
            user_friendly_pattern = f"每月{int(day_str)}日"
    elif recurrence_input.startswith("年") and "月" in recurrence_input and recurrence_input.endswith("日"):
        parts = recurrence_input[1:-1].split("月")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            month, day = int(parts[0]), int(parts[1])
            # Basic validation, could add checks for days in month
            if 1 <= month <= 12 and 1 <= day <= 31:
                system_pattern = f"yearly_{month}_{day}" # Store as numbers
                user_friendly_pattern = f"每年{month}月{day}日"

    if not system_pattern:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別的重複模式。請使用「每天」、「每週一」、「每月15日」或「每年12月25日」等格式。"))
        return

    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在於群組 {group_id}，自動建立。")
        try:
            member = create_member(db, name=member_name, group_id=group_id)
        except Exception as create_err:
            logger.exception(f"自動建立成員 '{member_name}' 失敗: {create_err}")
            db.rollback()
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"自動建立成員 '{member_name}' 失敗，無法新增定期任務。"))
            return

    try:
        # 創建定期任務的主任務 (master task)
        task = Task(
            member_id=member.id,
            content=task_content,
            status='recurring_master', # Use a distinct status for master tasks
            priority=priority,
            is_recurring=True,
            recurrence_pattern=system_pattern,
            recurrence_count=0 # Initialize count
        )
        db.add(task)
        db.commit() # Commit the master task

        priority_display = priority_map_display.get(priority, priority)

        reply_text = f"✅ 已為 @{member.name} 新增定期任務：\n"
        reply_text += f"內容：{task.content}\n"
        reply_text += f"任務ID：T-{task.id} (此為定期模板)\n" # Clarify it's a template
        reply_text += f"優先級：{priority_display}\n"
        reply_text += f"重複模式：{user_friendly_pattern}\n"
        reply_text += f"👉 系統將在指定時間自動生成待辦任務。\n"
        reply_text += f"👉 使用「#取消定期 T-{task.id}」可停止此定期任務。"

        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

    except SQLAlchemyError as e:
        logger.exception(f"新增定期任務到資料庫時失敗 (DB): {e}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (資料庫錯誤)，請稍後再試。"))
    except Exception as e:
        logger.exception(f"新增定期任務時發生未知錯誤: {e}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (內部錯誤)，請稍後再試。"))


def handle_cancel_recurring_task(reply_token: str, match: re.Match, group_id: str, user_id: str, db: Session):
    """處理取消定期任務"""
    task_id_num = int(match.group(1))
    task = get_task_by_id(db, task_id=task_id_num)

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif not task.is_recurring:
        reply_text = f"❌ 任務 T-{task_id_num} 不是一個進行中的定期任務模板。"
    elif task.member.group_id != group_id:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    else:
        try:
            task_content_preview = task.content[:20]
            member_name = task.member.name
            # Mark as no longer recurring
            task.is_recurring = False
            task.status = 'cancelled_recurring' # Optional: mark status
            db.commit()

            reply_text = f"✅ 已取消 @{member_name} 的定期任務模板 T-{task_id_num}。\n內容：{task_content_preview}...\n將不再自動生成新任務。"
        except SQLAlchemyError as e:
            logger.exception(f"取消定期任務 T-{task_id_num} 時失敗 (DB): {e}")
            db.rollback()
            reply_text = f"❌ 取消定期任務 T-{task_id_num} 失敗 (資料庫錯誤)。"
        except Exception as e:
            logger.exception(f"取消定期任務 T-{task_id_num} 時失敗: {e}")
            db.rollback()
            reply_text = f"❌ 取消定期任務 T-{task_id_num} 失敗 (內部錯誤)。"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


# --- Help Messages (Updated) ---

def send_help_message(reply_token: str):
    """Sends updated help message"""
    help_text = (
        "📋 代辦事項機器人指令 v2 📋\n\n"
        "✨ **常用指令** ✨\n"
        "`#新任務` - 引導式新增單一任務 (推薦)\n"
        "`#列表 [@成員]` - 顯示自己或指定成員的待辦 (成員可選)\n"
        "`#完成 T-任務ID` - 標記任務完成\n"
        "`#詳情 T-任務ID` - 查看任務詳細資訊\n\n"
        "🔸 **進階新增** 🔸\n"
        "`#新增 @成員 [!優先級] 內容 [日期]`\n"
        "  (優先級: !低,!普通,!高 / 日期: YYYY/MM/DD)\n"
        "`#批量新增 @成員`\n"
        "`  [!優先級] 內容1 [日期]`\n"
        "`  內容2`\n"
        "  (換行分隔多個任務, 優先級/日期可選)\n"
        "`#定期 @成員 [!優先級] 內容 每週期`\n"
        "  (週期: 每天, 每週一~日, 每月5日, 每年12月25日)\n\n"
        "🔹 **管理任務** 🔹\n"
        "`#修改 T-ID [!優先級] 新內容 [日期]`\n"
        "`#刪除 T-ID`\n"
        "`#取消定期 T-ID` (取消定期任務模板)\n\n"
        "🕹️ **其他功能** 🕹️\n"
        "`#擲筊 問題`\n"
        "`#抽籤 選項1 選項2 ...`\n\n"
        "❓ **獲取幫助** ❓\n"
        "`#幫助` (本訊息)\n"
        "`#幫助新增` (新增指令說明)\n"
        "`#編輯幫助 T-ID` (修改指令說明)\n"
        "`#新增表單` / `#定期表單` (顯示範例表單)"

    )
    # Add Quick Reply buttons for common actions?
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text,
          quick_reply=QuickReply(items=[
              QuickReplyButton(action=MessageAction(label="#新任務", text="#新任務")),
              QuickReplyButton(action=MessageAction(label="#列表", text="#列表")),
              QuickReplyButton(action=MessageAction(label="#幫助新增", text="#幫助新增")),
          ])))
    except Exception as e:
         logger.warning(f"無法發送帶有 QuickReply 的幫助訊息: {e}")
         line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))


def send_add_help_message(reply_token: str):
    """發送新增任務幫助訊息 (Updated)"""
    help_text = (
        "📝 **如何新增任務** 📝\n\n"
        "1️⃣ **引導式新增 (推薦):**\n"
        "   輸入 `#新任務`，機器人會一步步問你內容、負責人、優先級和截止日期。\n\n"
        "2️⃣ **指令式新增 (單一任務):**\n"
        "   `#新增 @成員名稱 [!優先級] 任務內容 [截止日期]`\n"
        "   - `!優先級`: 可選 (!低, !普通, !高), 預設普通。\n"
        "   - `截止日期`: 可選 (格式 YYYY/MM/DD)。\n"
        "   *範例:* `#新增 @小明 !高 完成報告 2025/12/31`\n"
        "   *範例:* `#新增 @小華 買咖啡`\n\n"
        "3️⃣ **批量新增 (多個任務):**\n"
        "   `#批量新增 @成員名稱`\n"
        "   (換行後，每行輸入一個任務)\n"
        "   `[!優先級] 任務1 [日期]`\n"
        "   `任務2`\n"
        "   *範例:*\n"
        "   `#批量新增 @工讀生`\n"
        "   `!低 訂便當 2025/05/05`\n"
        "   `整理倉庫`\n\n"
        "4️⃣ **定期任務:**\n"
        "   `#定期 @成員 [!優先級] 內容 每週期`\n"
        "   - `週期`: `每天`, `每週一`~`每週日`, `每月15日`, `每年12月25日`\n"
        "   *範例:* `#定期 @值日生 !普通 倒垃圾 每週五`\n"
        "   *範例:* `#定期 @會計 !高 報帳 每月25日`"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_edit_help_message(reply_token: str, task_id: str):
    """發送編輯任務幫助訊息 (Updated)"""
    help_text = (
        f"✏️ **如何編輯任務 T-{task_id}** ✏️\n\n"
        "使用以下格式 (至少提供新內容)：\n"
        f"`#修改 T-{task_id} [!優先級] 新任務內容 [新截止日期]`\n\n"
        "說明:\n"
        " - `!優先級`: 可選 (!低, !普通, !高)。若省略，則優先級不變。\n"
        " - `新任務內容`: **必填**。\n"
        " - `新截止日期`: 可選 (YYYY/MM/DD)。若省略，則截止日期不變。若要移除截止日期，可能需特定指令或未來功能。\n\n"
        "*範例 1 (修改內容和優先級):*\n"
        f"`#修改 T-{task_id} !高 更新後的報告內容`\n\n"
        "*範例 2 (修改內容和日期):*\n"
        f"`#修改 T-{task_id} 最終版簡報 2025/06/01`\n\n"
        "*範例 3 (只修改內容):*\n"
        f"`#修改 T-{task_id} 把咖啡買好`"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

# --- Flex/Text Message Helpers ---
# create_task_list_bubble, create_task_list_text need review based on model changes if any
# ... (Keep existing create_task_list_bubble - check member access, date handling)
# ... (Keep existing create_task_list_text - check member access, date handling)
# Ensure task.member.name access is valid (depends on SQLAlchemy relationship loading)

def create_task_list_bubble(title: str, tasks: List[Task], db: Session):
    """Creates Flex Message bubble using SQLAlchemy Task objects (Review Recommended)"""
    # Ensure lazy loading works or use joinedload in the query that calls this
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    priority_color_map = {"low": "#28a745", "normal": "#ffc107", "high": "#dc3545"}

    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": []}, # Added spacing
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "md",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1E88E5", "height": "sm", "flex": 1, "action": {"type": "message", "label": "✨ 新增任務", "text": "#新任務"}},
                {"type": "button", "style": "secondary", "color": "#6c757d", "height": "sm", "flex": 1, "action": {"type": "message", "label": "❓ 幫助", "text": "#幫助"}}
            ]
        }
    }
    body_contents = contents["body"]["contents"]

    for task in tasks:
        try:
            member_name = task.member.name if task.member else '未知成員' # Critical: relies on relationship being loaded
            priority = task.priority or "normal"
            priority_display = priority_map_display.get(priority, priority)
            priority_color = priority_color_map.get(priority, "#888888")

            task_item_elements = [
                # Header: ID, Priority, Member
                {"type": "box", "layout": "horizontal",
                 "contents": [
                    {"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1, "weight":"bold"},
                    {"type": "text", "text": priority_display, "size": "xs", "color": priority_color, "align": "center", "flex": 1, "weight":"bold"},
                    {"type": "text", "text": f"@{member_name}", "size": "sm", "color": "#1DB446", "align": "end", "flex": 2, "weight":"bold"}
                 ]},
                # Content
                {"type": "text", "text": task.content, "wrap": True, "weight": "regular", "margin": "md", "size":"md"},
             ]

            # Due Date Handling
            if task.due_date:
                try:
                    due_date_obj = task.due_date # Assume it's a date/datetime object
                    today = date.today() # Use date for comparison
                    days_left = (due_date_obj - today).days # Calculate days difference

                    if days_left < 0:
                         due_date_status = f"(已逾期 {-days_left} 天)"
                         color = "#dc3545" # Red
                    elif days_left == 0:
                         due_date_status = "(今天截止!)"
                         color = "#ffc107" # Orange
                    elif days_left == 1:
                         due_date_status = "(明天截止!)"
                         color = "#ffc107" # Orange
                    elif days_left < 4:
                         due_date_status = f"({days_left} 天後截止)"
                         color = "#ffc107" # Orange
                    else:
                         due_date_status = f"({days_left} 天)"
                         color = "#888888" # Grey

                    due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                    task_item_elements.append({
                         "type": "text", "text": f"截止: {due_date_str_display} {due_date_status}",
                         "size": "xs", "color": color, "margin": "sm"
                    })
                except Exception as date_err:
                    logger.error(f"處理任務 T-{task.id} 的截止日期時出錯 (Flex): {date_err}")
                    task_item_elements.append({"type": "text", "text": f"截止: 日期處理錯誤", "size": "xs", "color": "#dc3545", "margin": "sm"})

            # Buttons Box
            buttons_box = {
                "type": "box", "layout": "horizontal", "margin": "lg", "spacing":"sm",
                "contents": [
                    {"type": "button", "style": "primary", "color": "#4CAF50", "height": "sm", "flex": 1, "action": {"type": "message", "label": "完成", "text": f"#完成 T-{task.id}"}},
                    {"type": "button", "style": "secondary", "color": "#2196F3", "height": "sm", "flex": 1, "action": {"type": "message", "label": "詳情", "text": f"#詳情 T-{task.id}"}}
                 ]
            }
            task_item_elements.append(buttons_box)

            # Add recurring info text if applicable
            if task.is_recurring:
                 pattern_text = format_recurrence_pattern(task.recurrence_pattern)
                 task_item_elements.append({"type": "text", "text": f"⏰ 定期模板 ({pattern_text})", "size": "xs", "color": "#9C27B0", "margin": "md"})
            elif task.parent_task_id:
                 # Querying parent here is inefficient, details view is better
                 task_item_elements.append({"type": "text", "text": f"🔄 定期衍生 (來自 T-{task.parent_task_id})", "size": "xs", "color": "#757575", "margin": "md"})


            # Add the whole task item as a box
            body_contents.append({
                "type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md",
                "backgroundColor": "#FAFAFA", "cornerRadius": "md",
                "contents": task_item_elements
            })
            # Add separator between tasks, except for the last one
            if task != tasks[-1]:
                body_contents.append({"type":"separator", "margin":"lg"})

        except AttributeError as ae:
             logger.error(f"處理任務 T-{task.id} 時出錯 (可能未加載 member): {ae}")
             # Add a placeholder or skip the task in the list
             body_contents.append({
                "type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#EEEEEE", "cornerRadius": "md",
                "contents": [{"type": "text", "text": f"❌ 無法顯示任務 T-{task.id} (加載錯誤)", "color": "#dc3545", "size":"sm", "wrap":True}]
            })
        except Exception as task_err:
             logger.error(f"處理任務 T-{task.id} 時發生未知錯誤: {task_err}")
             body_contents.append({
                "type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#EEEEEE", "cornerRadius": "md",
                "contents": [{"type": "text", "text": f"❌ 無法顯示任務 T-{task.id} ({type(task_err).__name__})", "color": "#dc3545", "size":"sm", "wrap":True}]
            })

    return contents

def create_task_list_text(title: str, tasks: List[Task], db: Session):
    """Creates fallback text message using SQLAlchemy Task objects (Review Recommended)"""
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    result = f"📋 {title} 📋\n\n"
    for i, task in enumerate(tasks, 1):
        try:
            member_name = task.member.name if task.member else '未知成員'
            priority = task.priority or "normal"
            priority_display = priority_map_display.get(priority, priority)

            result += f"【任務 T-{task.id}】 {priority_display}\n"
            result += f"👤 負責人: @{member_name}\n"
            result += f"📝 內容: {task.content}\n"

            if task.due_date:
                try:
                    due_date_obj = task.due_date
                    today = date.today()
                    days_left = (due_date_obj - today).days
                    due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                    status = ("(⚠️ 已逾期)" if days_left < 0 else
                              "(⚠️ 今天截止!)" if days_left == 0 else
                              f"(⚠️ {days_left}天後截止)" if days_left < 4 else
                              f"(還有 {days_left} 天)")
                    result += f"📅 截止: {due_date_str_display} {status}\n"
                except Exception as date_err:
                    logger.error(f"處理任務 T-{task.id} 的截止日期時出錯 (Text): {date_err}")
                    result += f"📅 截止: 日期錯誤\n"
            else:
                 result += f"📅 截止: 無\n"

            # Add recurring info text if applicable
            if task.is_recurring:
                pattern_text = format_recurrence_pattern(task.recurrence_pattern)
                result += f"⏰ 定期模板 ({pattern_text})\n"
            elif task.parent_task_id:
                 result += f"🔄 定期衍生 (來自 T-{task.parent_task_id})\n"

            result += f"👉 操作: #完成 T-{task.id} | #詳情 T-{task.id}\n"

            if i < len(tasks):
                result += "\n" + ("-" * 20) + "\n\n"
        except Exception as e:
             logger.error(f"生成任務 T-{task.id} 的文字描述時出錯: {e}")
             result += f"【任務 T-{task.id}】\n❌ 無法顯示此任務詳情 ({type(e).__name__})\n\n"
             if i < len(tasks):
                result += "\n" + ("-" * 20) + "\n\n"
    return result


# --- n8n Integration API Endpoints (Using SQLAlchemy) ---
# ... (Keep existing /api/pending-tasks, /api/send-reminder)
# Review /api/pending-tasks date formatting if needed

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
            # Fetch pending tasks (status='pending' or other active statuses)
            # Use joinedload for efficiency
            tasks = db.query(Task).options(joinedload(Task.member))\
                      .filter(Task.member.has(group_id=TARGET_GROUP_ID), # Filter by group ID via member
                              Task.status == 'pending')\
                      .order_by(Task.due_date.asc().nulls_last(), Task.priority.desc(), Task.created_at.asc())\
                      .all() # Added sorting

            result = []
            today = date.today()
            for task in tasks:
                due_date_str, days_left = None, None
                if task.due_date:
                    try:
                        # Assuming task.due_date is date or datetime from DB
                        due_date_obj = task.due_date
                        # If it's datetime, convert to date for comparison
                        if isinstance(due_date_obj, datetime):
                             due_date_obj = due_date_obj.date()

                        days_left = (due_date_obj - today).days
                        due_date_str = due_date_obj.strftime('%Y/%m/%d')
                    except Exception as e:
                         logger.warning(f"Error processing due date for task {task.id} in API: {e}")
                         due_date_str = "日期錯誤"

                result.append({
                    "id": task.id,
                    "task_id": f"T-{task.id}",
                    "member": task.member.name if task.member else '未知',
                    "member_id": task.member_id,
                    "content": task.content,
                    "priority": task.priority,
                    "status": task.status,
                    "due_date": due_date_str,
                    "days_left": days_left, # Can be negative, zero, or positive
                    "is_recurring": task.is_recurring, # Indicate if it's a master template
                    "parent_task_id": task.parent_task_id, # Indicate if derived from recurring
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                 })
            return jsonify({"tasks": result, "count": len(result), "group_id": TARGET_GROUP_ID})
    except SQLAlchemyError as e:
        logger.exception(f"API /api/pending-tasks 發生 DB 錯誤: {str(e)}")
        return jsonify({"error": "Internal server error fetching tasks (DB)."}), 500
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

    message_text = data['message']
    target_id = data.get('target_id', TARGET_GROUP_ID) # Allow overriding target_id

    if not message_text:
         return jsonify({"error": "Message content cannot be empty"}), 400

    try:
        line_bot_api.push_message(target_id, messages=[TextSendMessage(text=message_text)])
        logger.info(f"已成功透過 API 發送提醒至 ID: {target_id}")
        return jsonify({"success": True, "message": "Reminder sent successfully", "target_id": target_id})
    except Exception as e:
        logger.exception(f"透過 API 發送提醒訊息至 {target_id} 時發生錯誤: {str(e)}")
        return jsonify({"success": False, "error": f"Failed to send reminder: {str(e)}"}), 500


# API Endpoint for Recurring Task Generation (Added "daily", logging)
@app.route("/api/generate-recurring-tasks", methods=['POST'])
def api_generate_recurring_tasks():
    """API Endpoint: 生成定期任務 (Added "daily" support and logging)"""
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY:
        logger.warning("未經授權的定期任務生成請求被拒絕。")
        return jsonify({"error": "Unauthorized"}), 401

    logger.info("開始生成定期任務...")
    current_date = datetime.now().date() # Use date object
    day_of_week = current_date.strftime('%A').lower() # 'monday', 'tuesday', ...
    day_of_month = current_date.day
    month_day = f"{current_date.month}_{current_date.day}" # e.g., "5_2", "12_25"

    # Define patterns to match for today
    weekly_pattern = f"weekly_{day_of_week}"
    monthly_pattern = f"monthly_{day_of_month}"
    yearly_pattern = f"yearly_{month_day}"
    daily_pattern = "daily"

    logger.info(f"當前日期: {current_date}, 匹配模式: daily='{daily_pattern}', weekly='{weekly_pattern}', monthly='{monthly_pattern}', yearly='{yearly_pattern}'")

    created_tasks_report = []
    processed_master_ids = set()

    try:
        with get_db() as db:
            # Find all active recurring master tasks matching today's patterns
            recurring_master_tasks = db.query(Task).options(joinedload(Task.member)).filter(
                Task.is_recurring == True,
                Task.status == 'recurring_master', # Ensure we only process master tasks
                or_(
                    Task.recurrence_pattern == daily_pattern,
                    Task.recurrence_pattern == weekly_pattern,
                    Task.recurrence_pattern == monthly_pattern,
                    Task.recurrence_pattern == yearly_pattern
                )
            ).all()

            logger.info(f"找到 {len(recurring_master_tasks)} 個符合今日條件的定期任務模板。")
            if not recurring_master_tasks:
                 return jsonify({"success": True, "created_count": 0, "message":"沒有符合條件的定期任務需要生成。","tasks": []})

            new_tasks_to_add = []
            notifications = {} # group_id -> list of messages

            for master_task in recurring_master_tasks:
                logger.debug(f"處理模板 T-{master_task.id} (內容: {master_task.content[:20]}..., 模式: {master_task.recurrence_pattern})")
                # Check if we already processed this master task (e.g., if a task matches multiple criteria - unlikely but possible)
                if master_task.id in processed_master_ids:
                    logger.debug(f"  模板 T-{master_task.id} 已處理過，跳過。")
                    continue

                # Create the new pending task instance
                new_task = Task(
                    member_id=master_task.member_id,
                    content=master_task.content,
                    status='pending', # New task is pending
                    priority=master_task.priority,
                    due_date=None, # Or set to today? Or end of day? Defaulting to None.
                    parent_task_id=master_task.id, # Link back to master
                    is_recurring=False # This instance is not a master
                )
                new_tasks_to_add.append(new_task)

                # Prepare notification for the group
                group_id = master_task.member.group_id # Assumes member relationship loaded
                if group_id:
                     member_name = master_task.member.name
                     priority_map = {"low": "🟢", "normal": "🟡", "high": "🔴"}
                     p_emoji = priority_map.get(master_task.priority, "")
                     # We don't have the new ID yet, add placeholders
                     task_info = f"{p_emoji} @{member_name}: {new_task.content}"
                     if group_id not in notifications:
                         notifications[group_id] = []
                     notifications[group_id].append({'info': task_info, 'obj': new_task}) # Store obj to get ID later

                # Increment count on master task
                master_task.recurrence_count = (master_task.recurrence_count or 0) + 1
                processed_master_ids.add(master_task.id)


            if not new_tasks_to_add:
                logger.info("沒有新的待辦任務需要創建。")
                return jsonify({"success": True, "created_count": 0, "message":"處理完成，沒有新任務生成。","tasks": []})


            # Add new tasks and update master tasks in one commit
            db.add_all(new_tasks_to_add)
            db.flush() # Assign IDs to new tasks

            # Now build final report and notification messages
            for task_report in new_tasks_to_add:
                 if task_report.id: # Check ID assignment
                      created_tasks_report.append({
                          "new_task_id": f"T-{task_report.id}",
                          "master_task_id": f"T-{task_report.parent_task_id}" if task_report.parent_task_id else None,
                          "member_id": task_report.member_id,
                          "content": task_report.content,
                      })
                 else:
                      logger.error(f"新任務未能獲取ID (來自 T-{task_report.parent_task_id})")


            # Send notifications
            for group_id, task_infos in notifications.items():
                 if not group_id: continue
                 try:
                     # Build notification text with actual new IDs
                     notif_text = "🔄 已自動生成今日定期任務：\n"
                     count = 0
                     for item in task_infos:
                          task_obj = item['obj']
                          if task_obj.id: # Check ID
                              notif_text += f"• T-{task_obj.id} {item['info']}\n"
                              count += 1
                          else: # Fallback if ID missing
                              notif_text += f"• (新) {item['info']}\n"
                              count += 1
                          if count >= 15: # Limit lines per message
                              notif_text += f"... (等共計 {len(task_infos)} 個任務)"
                              break

                     if count > 0: # Only send if there are tasks
                         logger.info(f"發送定期任務通知到 Group ID: {group_id} ({count} 個任務)")
                         line_bot_api.push_message(group_id, TextSendMessage(text=notif_text))
                     else:
                          logger.info(f"沒有為 Group ID: {group_id} 生成有效的任務通知。")

                 except Exception as push_err:
                     logger.exception(f"發送定期任務通知訊息到 {group_id} 失敗: {push_err}")


            db.commit() # Commit all changes (new tasks and updated counts)
            logger.info(f"成功生成並提交 {len(created_tasks_report)} 個新任務。")


            return jsonify({
                "success": True,
                "created_count": len(created_tasks_report),
                "tasks": created_tasks_report
            })

    except SQLAlchemyError as e:
        logger.exception(f"生成定期任務時發生 DB 錯誤: {e}")
        db.rollback() # Rollback any partial changes
        return jsonify({"success": False, "error": f"Database error during recurring task generation: {e}"}), 500
    except Exception as e:
        logger.exception(f"生成定期任務時發生未知錯誤: {e}")
        db.rollback()
        return jsonify({"success": False, "error": f"Internal server error during recurring task generation: {e}"}), 500

# --- Informational Forms (No direct action buttons for partial state) ---

def send_add_task_form(reply_token: str, db: Session, group_id: str):
    """發送任務新增表單 (Informational)"""
    # This form primarily shows users the available commands / guided flow
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "新增任務選項", "weight": "bold", "size": "xl", "color": "#2196F3"}]},
        "body": {
            "type": "box", "layout": "vertical", "spacing":"lg",
            "contents": [
                {"type": "text", "text": "你可以使用以下方式新增任務：", "wrap": True},
                {"type": "button", "style": "primary", "color": "#1E88E5", "action": {"type": "message", "label": "引導式新增 (#新任務)", "text": "#新任務"}},
                {"type": "button", "style": "secondary", "action": {"type": "message", "label": "查看指令說明 (#幫助新增)", "text": "#幫助新增"}},
                {"type": "box", "layout":"vertical", "margin":"lg", "contents":[
                     {"type":"text", "text":"或者直接輸入完整指令，例如：", "size":"sm", "color":"#888888", "wrap":True},
                     {"type":"text", "text":"#新增 @成員 !優先級 內容 日期", "size":"xs", "color":"#555555", "wrap":True},
                     {"type":"text", "text":"#批量新增 @成員\n任務1\n任務2", "size":"xs", "color":"#555555", "wrap":True},
                  ]}
            ]
        }
     }
    try:
        line_bot_api.reply_message(
            reply_token,
            FlexSendMessage(alt_text="新增任務選項", contents=contents)
        )
    except Exception as e:
        logger.exception(f"發送任務新增表單失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法顯示新增選項，請輸入「#幫助新增」查看說明。"))

def send_recurring_task_form(reply_token: str, db: Session, group_id: str):
    """發送定期任務新增表單 (Informational, added '每天')"""
    # This form primarily shows users the available commands / guided flow
    contents = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "新增定期任務說明", "weight": "bold", "size": "xl", "color": "#9C27B0"}]},
        "body": {
            "type": "box", "layout": "vertical", "spacing":"lg",
            "contents": [
                {"type": "text", "text": "請使用指令新增定期任務：", "wrap": True},
                {"type": "box", "layout":"vertical", "margin":"md", "contents":[
                    {"type":"text", "text":"`#定期 @成員 [!優先級] 內容 每週期`", "wrap":True, "size":"sm"},
                    {"type":"text", "text":"週期範例:", "size":"sm", "margin":"sm", "weight":"bold"},
                    {"type":"text", "text":"• `每天`\n• `每週一` (或 週二 到 週日)\n• `每月15日` (或 1 到 31)\n• `每年12月25日` (或 X月X日)", "wrap":True, "size":"xs", "color":"#555555"},
                 ]},
                 {"type": "separator"},
                 {"type": "text", "text":"範例指令:", "size":"sm", "weight":"bold"},
                 {"type":"text", "text":"`#定期 @清潔工 !低 打掃 每週五`\n`#定期 @管理員 !普通 月報 每月1日`\n`#定期 @老闆 !高 生日提醒 每年8月8日`", "wrap":True, "size":"xs", "color":"#555555"},
                 {"type": "separator"},
                 {"type": "button", "style": "secondary", "action": {"type": "message", "label": "查看完整說明 (#幫助)", "text": "#幫助"}},
            ]
        }
    }
    try:
        line_bot_api.reply_message(
            reply_token,
            FlexSendMessage(alt_text="新增定期任務說明", contents=contents)
        )
    except Exception as e:
        logger.exception(f"發送定期任務新增表單失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法顯示定期任務說明，請輸入「#幫助」查看指令。"))


# --- Main Execution Block ---
if __name__ == "__main__":
    # Get port from environment variable or default
    port = int(os.environ.get('PORT', 8080)) # Changed default to 8080
    logger.info(f"讀取到的端口配置為: {port}")

    # Special handling for Replit environment (if needed, modify host/port detection)
    host = '0.0.0.0' # Listen on all interfaces
    if IN_REPLIT:
        logger.info(f"在 Replit 環境中運行，將使用 host='0.0.0.0' 和 port={port}")
        # Replit typically sets the PORT env var and expects 0.0.0.0 host

    # Start Flask application
    logger.info(f"Flask 應用啟動於 host={host}, port={port}")
    try:
        # Use debug=False for production/stable environments
        # Set debug=True for development to get auto-reloading and detailed error pages
        app.run(host=host, port=port, debug=False)
    except OSError as e:
        logger.error(f"無法在端口 {port} 上啟動 Flask: {e}")
        logger.error("請檢查該端口是否已被其他程序佔用，或嘗試修改 PORT 環境變數。")
    except Exception as e:
        logger.exception(f"啟動 Flask 應用時發生未預期錯誤: {e}")