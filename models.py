# models.py (v2.3.0 - Removed Recurring, Added M2M Task-Member Relationship)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
    UniqueConstraint, Boolean, Table, PrimaryKeyConstraint # Added Table, PrimaryKeyConstraint
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload # Added joinedload here
from typing import Optional, List
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime
from dotenv import load_dotenv
import logging

# 設置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
DATABASE_URL = os.environ.get('DATABASE_URL')

# Replit PostgreSQL 自動配置 (保持不變)
IN_REPLIT = os.environ.get('REPL_ID') is not None
if IN_REPLIT and not DATABASE_URL:
    PGUSER = os.environ.get('PGUSER')
    PGPASSWORD = os.environ.get('PGPASSWORD')
    PGHOST = os.environ.get('PGHOST')
    PGDATABASE = os.environ.get('PGDATABASE')
    PGPORT = os.environ.get('PGPORT', '5432')
    if PGUSER and PGPASSWORD and PGHOST and PGDATABASE:
        DATABASE_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
        os.environ['DATABASE_URL'] = DATABASE_URL
        logger.info("已從 Replit Secrets 設置 PostgreSQL 連接。")
    else:
        logger.error("在 Replit 環境中，但未設置完整 PostgreSQL 連接信息。")

if not DATABASE_URL:
    raise ValueError("環境變數 DATABASE_URL 未設定！")

# Render 兼容性修復 (保持不變)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("已將 postgres:// 修正為 postgresql://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
Base = declarative_base()

@contextmanager
def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# --- Many-to-Many Association Table ---
# 定義任務與成員之間的多對多關聯表
task_assignments_table = Table(
    'task_assignments', Base.metadata,
    Column('task_id', Integer, ForeignKey('tasks.id'), primary_key=True),
    Column('member_id', Integer, ForeignKey('members.id'), primary_key=True)
    # 複合主鍵確保同一個任務不會重複指派給同一個成員
)
# --- End Association Table ---

class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    line_user_id = Column(String, unique=True, index=True, nullable=True) # unique=True might cause issues if users are in multiple groups without different names? Reconsider if needed.
    group_id = Column(String, nullable=False, index=True) # Still useful to know which group the member record belongs to
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- Updated Relationship for M2M ---
    # 指向 Task 模型，使用 task_assignments_table 作為中介表
    # back_populates 與 Task.members 建立雙向關係
    tasks = relationship(
        "Task",
        secondary=task_assignments_table,
        back_populates="members"
    )
    # --- End Updated Relationship ---

    # Constraint to ensure member name is unique within a group
    __table_args__ = (UniqueConstraint('name', 'group_id', name='_member_name_group_uc'),)

    def __repr__(self):
        return f"<Member(id={self.id}, name='{self.name}', group_id='{self.group_id}')>"

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    # Removed: member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, default='pending', index=True) # 'pending', 'completed'
    due_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    priority = Column(String, default='normal', index=True) # 'low', 'normal', 'high'

    # Removed Recurring Task Fields:
    # is_recurring = Column(Boolean, default=False)
    # recurrence_pattern = Column(String, nullable=True)
    # recurrence_count = Column(Integer, default=0)
    # parent_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)

    # --- Updated Relationship for M2M ---
    # 指向 Member 模型，使用 task_assignments_table 作為中介表
    # back_populates 與 Member.tasks 建立雙向關係
    members = relationship(
        "Member",
        secondary=task_assignments_table,
        back_populates="tasks"
    )
    # --- End Updated Relationship ---

    # Removed child_tasks relationship

    def __repr__(self):
        # Display members in repr if loaded, otherwise just ID/content
        member_names = ', '.join([m.name for m in self.members]) if self.members else 'No Members Loaded'
        return f"<Task(id={self.id}, content='{self.content[:20]}...', status='{self.status}', members='{member_names}')>"


def init_db():
    """Creates database tables based on the defined models."""
    logger.info("初始化資料庫，嘗試建立表格...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        logger.exception(f"初始化資料庫時發生錯誤: {e}")
        # Propagate error for startup failure if needed
        # raise

# --- CRUD Helper Functions (Updated for M2M Relationship) ---

def get_member_by_name_and_group(db: Session, name: str, group_id: str) -> Optional[Member]:
    """Gets a member by name within a specific group."""
    # This function remains useful for finding/creating members
    return db.query(Member).filter(Member.name == name, Member.group_id == group_id).first()

def get_member_by_id(db: Session, member_id: int) -> Optional[Member]:
    """Gets a member by their primary key ID."""
    return db.query(Member).filter(Member.id == member_id).first()

def get_task_by_id(db: Session, task_id: int, options: Optional[List] = None) -> Optional[Task]:
    """Gets a task by its primary key ID, optionally loading relationships."""
    query = db.query(Task)
    if options:
        query = query.options(*options) # Apply options like joinedload(Task.members)
    return query.filter(Task.id == task_id).first()

def get_pending_tasks_by_member_id(db: Session, member_id: int) -> List[Task]:
    """Gets all pending tasks assigned to a specific member."""
    # Updated query using the 'members' relationship and 'any()'
    return db.query(Task).options(joinedload(Task.members)).filter(
        Task.status == 'pending',
        Task.members.any(id=member_id) # Check if member is in the Task's members list
    ).order_by(Task.due_date.asc().nulls_last(), Task.priority.desc(), Task.created_at.asc()).all()

def get_pending_tasks_by_group_id(db: Session, group_id: str) -> List[Task]:
    """Gets all pending tasks where at least one assigned member belongs to the specified group."""
    # Updated query using 'any()' on the members relationship
    return db.query(Task).options(joinedload(Task.members)).filter(
        Task.status == 'pending',
        Task.members.any(Member.group_id == group_id) # Check if any member belongs to the group
    ).order_by(Task.due_date.asc().nulls_last(), Task.priority.desc(), Task.created_at.asc()).all()

def create_member(db: Session, name: str, group_id: str, line_user_id: Optional[str] = None) -> Member:
    """Creates a new member in the database."""
    # Logic remains the same, but ensure commit/refresh are handled appropriately
    # Consider adding checks or merging logic if line_user_id should be unique globally
    db_member = Member(name=name, group_id=group_id, line_user_id=line_user_id)
    try:
        db.add(db_member)
        db.commit()
        db.refresh(db_member)
        logger.info(f"Created member: {db_member}")
        return db_member
    except Exception as e:
        logger.error(f"Error creating member {name} in group {group_id}: {e}")
        db.rollback()
        raise # Re-raise the exception

def create_task(db: Session, members: List[Member], content: str, due_date: Optional[datetime] = None, priority: str = "normal", status: str = "pending") -> Task:
    """Creates a new task and assigns it to the provided list of members."""
    if not members:
        raise ValueError("Cannot create task without at least one member.")

    db_task = Task(
        content=content,
        status=status,
        due_date=due_date,
        priority=priority
        # members relationship will be populated below
    )
    try:
        # Assign the list of member objects to the relationship
        db_task.members.extend(members) # Use extend or assign directly depending on relationship config

        db.add(db_task)
        db.commit()
        db.refresh(db_task) # Refresh to get updated state, including members if configured
        logger.info(f"Created task T-{db_task.id} assigned to {[m.name for m in members]}")
        return db_task
    except Exception as e:
        member_ids = [m.id for m in members if m.id]
        logger.error(f"Error creating task '{content}' for members {member_ids}: {e}")
        db.rollback()
        raise # Re-raise the exception