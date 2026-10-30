import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# ── 数据库配置（开发环境使用 SQLite）──
DB_PATH = BASE_DIR / 'crm.db'
DATABASE_URL = f'sqlite:///{DB_PATH}?check_same_thread=False'
engine = create_engine(DATABASE_URL, echo=False)
print(f'[DB] Using SQLite: {DB_PATH}')

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class EmailAccount(Base):
    """已保存的邮箱账号，支持多账号切换"""
    __tablename__ = 'email_accounts'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    provider_id = Column(String(50), nullable=False)
    provider_name = Column(String(100))
    imap_host = Column(String(255), nullable=False)
    imap_port = Column(Integer, default=993)
    # IMAP 授权码/密码，加密存储
    auth_code = Column(Text, nullable=False)
    display_name = Column(String(100))
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Customer(Base):
    __tablename__ = 'customers'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), index=True, nullable=False)
    domain = Column(String(255), index=True)
    company = Column(String(255))
    name = Column(String(255))
    country = Column(String(100))
    priority = Column(String(20), default='cold')
    grade = Column(String(20), default='')  # 客户等级: A/B/C
    profile_summary = Column(Text)
    last_contact_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # 关联的邮箱账号 ID（数据隔离）
    account_id = Column(Integer, index=True, nullable=True)


class Email(Base):
    __tablename__ = 'emails'
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, index=True, nullable=False)
    message_id = Column(String(255), index=True)
    subject = Column(String(500))
    sender = Column(String(255))
    recipients = Column(Text)
    body_text = Column(Text)
    body_html = Column(Text)
    received_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    # 关联的邮箱账号 ID（数据隔离）
    account_id = Column(Integer, index=True, nullable=True)
    is_read = Column(Integer, default=0)  # 0=未读, 1=已读
    ai_priority = Column(String(20), default='')  # AI 分析的优先级: hot/warm/cold
    ai_analyzed = Column(Integer, default=0)  # 0=未分析, 1=已分析


class EmailAnalysis(Base):
    __tablename__ = 'email_analysis'
    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, unique=True, index=True, nullable=False)
    intent = Column(String(100))
    priority = Column(String(20))
    summary = Column(Text)
    next_action = Column(String(500))
    reply_draft = Column(Text)
    raw_response = Column(Text)
    no_reply_needed = Column(Integer, default=0)  # 1 = 无需回复（通知类邮件）
    replied = Column(Integer, default=0)  # 1 = 已回复（用户已处理）
    replied_at = Column(DateTime)  # 标记已回复的时间
    created_at = Column(DateTime, default=datetime.utcnow)


class WhatsAppMessage(Base):
    __tablename__ = 'whatsapp_messages'
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, index=True, nullable=False)
    remote_id = Column(String(255), index=True)
    sender = Column(String(255))
    message_text = Column(Text)
    timestamp = Column(DateTime)
    is_from_me = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class AIKnowledge(Base):
    """用户喂养给 AI 的知识/规则（养 AI 模型的语料）"""
    __tablename__ = 'ai_knowledge'
    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    source = Column(String(50), default='manual')
    created_at = Column(DateTime, default=datetime.utcnow)


class FollowUpRule(Base):
    """客户跟进计划规则（按等级/优先级 -> 跟进间隔天数）"""
    __tablename__ = 'follow_up_rules'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100))
    grade = Column(String(20), default='')      # A/B/C，空表示不限定
    priority = Column(String(20), default='')  # hot/warm/cold，空表示不限定
    interval_days = Column(Integer, default=7)
    active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FollowUp(Base):
    """客户跟进计划条目"""
    __tablename__ = 'follow_ups'
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, index=True, nullable=False)
    account_id = Column(Integer, index=True, nullable=True)
    summary = Column(Text)          # 跟进摘要
    action = Column(String(255))    # 建议动作
    next_date = Column(DateTime)    # 计划跟进日期
    status = Column(String(20), default='pending')  # pending/done/skipped
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DeletedEmail(Base):
    """已删除邮件的记录，防止再次拉取时重复入库"""
    __tablename__ = 'deleted_emails'
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(255), index=True, nullable=False)
    account_id = Column(Integer, index=True, nullable=True)
    deleted_at = Column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    """AI 对话会话"""
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), default="新对话")
    conv_type = Column(String(20), default="general")  # general / email
    email_id = Column(Integer, index=True, nullable=True)  # 邮件专属会话关联的邮件ID
    customer_id = Column(Integer, index=True, nullable=True)
    account_id = Column(Integer, index=True, nullable=True)
    is_deleted = Column(Integer, default=0)  # 软删除
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationMessage(Base):
    """AI 对话消息"""
    __tablename__ = 'conversation_messages'
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, index=True, nullable=False)
    role = Column(String(20))  # user / assistant / system
    content = Column(Text)
    action_results = Column(Text)  # JSON 格式存储动作执行结果
    created_at = Column(DateTime, default=datetime.utcnow)


class AIMemory(Base):
    """AI 记忆 — 持久化用户的重要指令和偏好"""
    __tablename__ = 'ai_memories'
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, index=True, nullable=True)
    key = Column(String(100))  # 简短标签，如 "跟进规则-A级客户"
    content = Column(Text)  # 完整的指令内容
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _migrate_db():
    """数据库迁移：为已存在的表添加新字段（如果不存在）"""
    inspector = inspect(engine)
    if not inspector.has_table('email_analysis'):
        return  # 表还不存在，init_db 会创建

    columns = [c['name'] for c in inspector.get_columns('email_analysis')]
    with engine.connect() as conn:
        if 'no_reply_needed' not in columns:
            conn.execute(text("ALTER TABLE email_analysis ADD COLUMN no_reply_needed INTEGER DEFAULT 0"))
            conn.commit()
            print('[DB] Migrated: added no_reply_needed column')
        if 'replied' not in columns:
            conn.execute(text("ALTER TABLE email_analysis ADD COLUMN replied INTEGER DEFAULT 0"))
            conn.commit()
            print('[DB] Migrated: added replied column')
        if 'replied_at' not in columns:
            conn.execute(text("ALTER TABLE email_analysis ADD COLUMN replied_at DATETIME"))
            conn.commit()
            print('[DB] Migrated: added replied_at column')

    # 客户表迁移：新增 grade 等级字段
    if inspector.has_table('customers'):
        cust_cols = [c['name'] for c in inspector.get_columns('customers')]
        with engine.connect() as conn:
            if 'grade' not in cust_cols:
                conn.execute(text("ALTER TABLE customers ADD COLUMN grade VARCHAR(20) DEFAULT ''"))
                conn.commit()
                print('[DB] Migrated: added grade column to customers')

    # WhatsApp 表迁移：移除 remote_id 的 UNIQUE 约束
    if inspector.has_table('whatsapp_messages'):
        indexes = inspector.get_indexes('whatsapp_messages')
        with engine.connect() as conn:
            for idx in indexes:
                if idx['name'] and 'remote_id' in idx['name'] and idx['unique']:
                    try:
                        conn.execute(text(f"DROP INDEX IF EXISTS {idx['name']}"))
                        conn.commit()
                        print(f'[DB] Migrated: dropped unique index {idx["name"]} on whatsapp_messages')
                    except Exception as e:
                        print(f'[DB] Migration warning: {e}')

    # Email 表迁移：新增 is_read, ai_priority, ai_analyzed 字段
    if inspector.has_table('emails'):
        email_cols = [c['name'] for c in inspector.get_columns('emails')]
        with engine.connect() as conn:
            if 'is_read' not in email_cols:
                conn.execute(text("ALTER TABLE emails ADD COLUMN is_read INTEGER DEFAULT 0"))
                conn.commit()
                print('[DB] Migrated: added is_read column to emails')
            if 'ai_priority' not in email_cols:
                conn.execute(text("ALTER TABLE emails ADD COLUMN ai_priority VARCHAR(20) DEFAULT ''"))
                conn.commit()
                print('[DB] Migrated: added ai_priority column to emails')
            if 'ai_analyzed' not in email_cols:
                conn.execute(text("ALTER TABLE emails ADD COLUMN ai_analyzed INTEGER DEFAULT 0"))
                conn.commit()
                print('[DB] Migrated: added ai_analyzed column to emails')


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    print(f'[DB] Database initialized: {DB_PATH}')


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_sync():
    return SessionLocal()
