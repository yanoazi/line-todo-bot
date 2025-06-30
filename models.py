# models.py (v2.5.1 - No Guided Flow, No Priority, Private Task Records)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
    UniqueConstraint, Boolean, Table, CheckConstraint
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload
from typing import Optional, List
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime, date 
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
DATABASE_URL = os.environ.get('DATABASE_URL')

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

task_assignments_table = Table(
    'task_assignments', Base.metadata,
    Column('task_id', Integer, ForeignKey('tasks.id'), primary_key=True),
    Column('member_id', Integer, ForeignKey('members.id'), primary_key=True)
)

class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    line_user_id = Column(String, unique=True, index=True, nullable=True)
    group_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tasks = relationship("Task", secondary=task_assignments_table, back_populates="members")
    __table_args__ = (UniqueConstraint('name', 'group_id', name='_member_name_group_uc'),)

    def __repr__(self):
        return f"<Member(id={self.id}, name='{self.name}', group_id='{self.group_id}')>"

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    status = Column(String, default='pending', index=True) # 'pending', 'completed'
    due_date = Column(DateTime(timezone=True), nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # This is the column definition that corresponds to the database error if missing in DB
    completed_on_time = Column(Boolean, nullable=True)

    group_id = Column(String, nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)

    members = relationship("Member", secondary=task_assignments_table, back_populates="tasks")

    def __repr__(self):
        owner_info = ""
        if self.group_id:
            member_names = ', '.join([m.name for m in self.members]) if self.members else 'No Members'
            owner_info = f"group='{self.group_id}', members='{member_names}'"
        elif self.user_id:
            owner_info = f"user='{self.user_id}'"
        else:
            owner_info = "NO OWNER!"

        completion_info = ""
        if self.status == 'completed' and self.user_id:
            completion_info = f", on_time={self.completed_on_time}"

        return f"<Task(id={self.id}, {owner_info}, content='{self.content[:20]}...', status='{self.status}'{completion_info})>"

def init_db():
    logger.info("初始化資料庫，嘗試建立表格...")
    try:
        # This creates tables if they don't exist.
        # IT DOES NOT MODIFY EXISTING TABLES (e.g., add new columns).
        # For schema changes like adding 'completed_on_time', you need migrations
        # or manual ALTER TABLE statements.
        Base.metadata.create_all(bind=engine)
        logger.info("表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        logger.exception(f"初始化資料庫時發生錯誤: {e}")

def get_member_by_name_and_group(db: Session, name: str, group_id: str) -> Optional[Member]:
    return db.query(Member).filter(Member.name == name, Member.group_id == group_id).first()

def get_member_by_id(db: Session, member_id: int) -> Optional[Member]:
    return db.query(Member).filter(Member.id == member_id).first()

def get_task_by_id(db: Session, task_id: int, options: Optional[List] = None) -> Optional[Task]:
    query = db.query(Task)
    if options:
        query = query.options(*options)
    return query.filter(Task.id == task_id).first()

def get_pending_tasks_by_group_id(db: Session, group_id: str) -> List[Task]:
    return db.query(Task).options(joinedload(Task.members)).filter(
        Task.status == 'pending',
        Task.group_id == group_id
    ).order_by(Task.due_date.asc().nulls_last(), Task.created_at.asc()).all()

def get_pending_tasks_by_user_id(db: Session, user_id: str) -> List[Task]:
    return db.query(Task).filter(
        Task.status == 'pending',
        Task.user_id == user_id
    ).order_by(Task.due_date.asc().nulls_last(), Task.created_at.asc()).all()

def get_completed_tasks_by_user_id(db: Session, user_id: str) -> List[Task]:
    return db.query(Task).filter(
        Task.user_id == user_id,
        Task.status == 'completed'
    ).order_by(Task.completed_at.desc().nulls_last(), Task.created_at.desc()).all()


def create_member(db: Session, name: str, group_id: str, line_user_id: Optional[str] = None) -> Member:
    db_member = Member(name=name, group_id=group_id, line_user_id=line_user_id)
    try:
        db.add(db_member)
        db.commit()
        db.refresh(db_member)
        logger.info(f"Created member {db_member}")
        return db_member
    except Exception as e:
        logger.error(f"Error creating member {name} in group {group_id}: {e}")
        db.rollback()
        raise

def create_task(
    db: Session, 
    content: str,
    group_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    members: Optional[List[Member]] = None,
    due_date: Optional[datetime] = None, 
    status: str = 'pending'
) -> Task:
    if not (group_id or owner_user_id):
        raise ValueError("Task must have either a group_id or an owner_user_id.")
    if group_id and owner_user_id:
        raise ValueError("Task cannot have both group_id and owner_user_id.")

    db_task_args = {
        "content": content,
        "status": status,
        "due_date": due_date,
        # completed_on_time will default to NULL in the database as it's nullable
        # and not explicitly provided here. It's set upon task completion.
    }
    log_msg_owner = ""

    if group_id:
        if not members:
            raise ValueError("Group task requires at least one member.")
        db_task_args["group_id"] = group_id
        db_task = Task(**db_task_args) # Create Task instance
        db_task.members.extend(members) # Add members
        log_msg_owner = f"in group {group_id} assigned to {[m.name for m in members]}"
    elif owner_user_id:
        db_task_args["user_id"] = owner_user_id
        db_task = Task(**db_task_args) # Create Task instance
        log_msg_owner = f"for user {owner_user_id}"
    else: # Should not be reached due to initial checks
        raise Exception("Internal error: Task ownership could not be determined.")

    try:
        db.add(db_task)
        db.commit()
        db.refresh(db_task)
        if db_task.group_id: 
            db.refresh(db_task, attribute_names=['members']) # Eager load for log message
        logger.info(f"Created task T-{db_task.id} {log_msg_owner}")
        return db_task
    except Exception as e:
        logger.error(f"Error creating task '{content}' ({log_msg_owner}): {e}")
        db.rollback()
        raise