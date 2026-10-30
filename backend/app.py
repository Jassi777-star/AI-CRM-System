import os, json, re, logging, threading
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
# python-multipart installed for WhatsApp file upload

from fastapi import FastAPI, Depends, HTTPException, Query, Header, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).parent))

from models import init_db, get_db, Customer, Email, EmailAnalysis, EmailAccount, WhatsAppMessage, SessionLocal, AIKnowledge, FollowUpRule, FollowUp, Conversation, ConversationMessage, AIMemory
from ai_deepseek import build_customer_profile, analyze_email, analyze_whatsapp, get_ai_status, chat_with_ai, agent_plan, generate_reminders, AI_LIGHT_ANALYSIS
from email_fetcher import fetch_new_emails, test_imap_connection, clear_all_data, clear_account_data, delete_email, delete_customer
from email_providers import list_providers, get_provider, detect_provider
import auth
import ai_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI CRM Assistant", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization.strip()


def _sanitize_priority(value) -> str:
    """规范化优先级值，确保只返回 hot/warm/cold"""
    if not value or not isinstance(value, str):
        return "cold"
    v = value.strip().lower()
    if v in ("hot", "warm", "cold"):
        return v
    # 处理 AI 可能返回的中文或长文本
    if "高" in value or "hot" in v or "urgent" in v or "立即" in value:
        return "hot"
    if "中" in value or "warm" in v:
        return "warm"
    return "cold"


def _recalc_customer_priority(customer, db: Session):
    """根据客户所有邮件的分析结果重新计算客户优先级（取最高优先级）"""
    if not customer:
        return
    emails = db.query(Email).filter(Email.customer_id == customer.id).all()
    max_priority = "cold"
    for e in emails:
        analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == e.id).first()
        if analysis and analysis.priority:
            if analysis.priority == "hot":
                max_priority = "hot"
                break
            elif analysis.priority == "warm" and max_priority != "hot":
                max_priority = "warm"
    if customer.priority != max_priority:
        customer.priority = max_priority
        customer.updated_at = datetime.utcnow()


# 按账号隔离的 AI 分析状态：{account_id: bool}
# 避免一个用户拉取邮件时影响其他用户的优先级显示
_ai_analysis_in_progress: dict[int, bool] = {}
_ai_analysis_lock = threading.Lock()


def _is_analyzing(account_id=None) -> bool:
    """检查指定账号是否正在分析中"""
    with _ai_analysis_lock:
        if account_id is None:
            return any(_ai_analysis_in_progress.values())
        return _ai_analysis_in_progress.get(account_id, False)


def _set_analyzing(account_id, value: bool):
    """设置指定账号的分析状态"""
    with _ai_analysis_lock:
        if account_id is not None:
            _ai_analysis_in_progress[account_id] = value


def get_current_session(authorization: Optional[str] = Header(None)) -> Optional[auth.Session]:
    token = _extract_token(authorization)
    return auth.get_session(token)


# ── Pydantic 模型 ──
class CustomerProfileRequest(BaseModel):
    customer_id: int
    email_history: str = ""

class EmailAnalysisRequest(BaseModel):
    email_id: int
    customer_profile: str = ""
    recent_emails: str = ""
    language: str = "en"  # 回复草稿语言: en=英文(默认) / zh=中文

class LoginRequest(BaseModel):
    email: str
    password: str
    provider_id: str = "auto"
    imap_host: str = ""
    imap_port: int = 993
    save_account: bool = True   # 是否保存账号供后续切换

class SwitchAccountRequest(BaseModel):
    account_id: int
    my_account_ids: list = []

class EmailMarkRepliedRequest(BaseModel):
    email_id: int

class WhatsAppImportRequest(BaseModel):
    text: str
    customer_id: int = 0  # 可选：指定关联客户ID

class WhatsAppAnalyzeRequest(BaseModel):
    customer_id: int


class CustomerUpdateRequest(BaseModel):
    priority: Optional[str] = None
    grade: Optional[str] = None
    profile: Optional[dict] = None


class ChatRequest(BaseModel):
    message: str
    customer_id: Optional[int] = None
    history: Optional[list] = None


class AIFeedRequest(BaseModel):
    content: str
    source: str = "manual"


class AgentChatRequest(BaseModel):
    message: str
    customer_id: Optional[int] = None
    email_id: Optional[int] = None
    history: Optional[list] = None
    conversation_id: Optional[int] = None
    conv_type: Optional[str] = "general"


class FollowUpRulesRequest(BaseModel):
    rules: list[dict]


class FollowUpStatusRequest(BaseModel):
    status: str


class AgentRequest(BaseModel):
    message: str
    customer_id: Optional[int] = None
    email_id: Optional[int] = None
    history: Optional[list] = None
    conversation_id: Optional[int] = None
    conv_type: Optional[str] = "general"


# ═══════════════════════════════════════════════════════════
# 会话管理 API
# ═══════════════════════════════════════════════════════════
@app.get("/api/conversations")
def api_conversations(conv_type: str = "general", email_id: Optional[int] = None, db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    """获取会话列表（未删除的）"""
    acc = session.account_id if session else None
    q = db.query(Conversation).filter(Conversation.is_deleted == 0, Conversation.conv_type == conv_type)
    if acc is not None:
        q = q.filter(Conversation.account_id == acc)
    if email_id is not None and conv_type == "email":
        q = q.filter(Conversation.email_id == email_id)
    q = q.order_by(Conversation.updated_at.desc())
    items = q.all()
    return {"items": [{"id": c.id, "title": c.title, "conv_type": c.conv_type, "email_id": c.email_id, "customer_id": c.customer_id, "created_at": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "", "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M") if c.updated_at else ""} for c in items]}


@app.post("/api/conversations")
def api_conversation_create(req: dict, db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    """创建新会话"""
    acc = session.account_id if session else None
    conv = Conversation(
        title=req.get("title", "新对话"),
        conv_type=req.get("conv_type", "general"),
        email_id=req.get("email_id"),
        customer_id=req.get("customer_id"),
        account_id=acc,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"id": conv.id, "title": conv.title, "conv_type": conv.conv_type}


@app.get("/api/conversations/{conv_id}/messages")
def api_conversation_messages(conv_id: int, db: Session = Depends(get_db)):
    """获取会话的消息历史"""
    conv = db.query(Conversation).filter(Conversation.id == conv_id, Conversation.is_deleted == 0).first()
    if not conv:
        raise HTTPException(404, "会话不存在")
    msgs = db.query(ConversationMessage).filter(ConversationMessage.conversation_id == conv_id).order_by(ConversationMessage.id.asc()).all()
    return {
        "conversation": {"id": conv.id, "title": conv.title, "conv_type": conv.conv_type, "email_id": conv.email_id, "customer_id": conv.customer_id},
        "messages": [{"id": m.id, "role": m.role, "content": m.content, "action_results": m.action_results or "", "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""} for m in msgs]
    }


@app.put("/api/conversations/{conv_id}")
def api_conversation_update(conv_id: int, req: dict, db: Session = Depends(get_db)):
    """更新会话（标题等）"""
    conv = db.query(Conversation).filter(Conversation.id == conv_id, Conversation.is_deleted == 0).first()
    if not conv:
        raise HTTPException(404, "会话不存在")
    if "title" in req:
        conv.title = str(req["title"])[:200]
    conv.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "id": conv.id, "title": conv.title}


@app.delete("/api/conversations/{conv_id}")
def api_conversation_delete(conv_id: int, db: Session = Depends(get_db)):
    """软删除会话"""
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(404, "会话不存在")
    conv.is_deleted = 1
    conv.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "id": conv.id}


@app.post("/api/conversations/{conv_id}/restore")
def api_conversation_restore(conv_id: int, db: Session = Depends(get_db)):
    """恢复已删除的会话"""
    conv = db.query(Conversation).filter(Conversation.id == conv_id, Conversation.is_deleted == 1).first()
    if not conv:
        raise HTTPException(404, "会话不存在或未删除")
    conv.is_deleted = 0
    conv.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "id": conv.id}


@app.get("/api/conversations/deleted")
def api_conversations_deleted(db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    """获取已删除的会话列表（回收站）"""
    acc = session.account_id if session else None
    q = db.query(Conversation).filter(Conversation.is_deleted == 1)
    if acc is not None:
        q = q.filter(Conversation.account_id == acc)
    q = q.order_by(Conversation.updated_at.desc())
    items = q.all()
    return {"items": [{"id": c.id, "title": c.title, "conv_type": c.conv_type, "deleted_at": c.updated_at.strftime("%Y-%m-%d %H:%M") if c.updated_at else ""} for c in items]}


# ═══════════════════════════════════════════════════════════
# AI 记忆管理
# ═══════════════════════════════════════════════════════════
@app.get("/api/ai/memories")
def api_get_memories(db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    """获取 AI 记忆列表"""
    acc = session.account_id if session else None
    q = db.query(AIMemory).filter(AIMemory.is_active == 1)
    if acc is not None:
        q = q.filter((AIMemory.account_id == acc) | (AIMemory.account_id == None))
    items = q.order_by(AIMemory.created_at.desc()).all()
    return {"items": [{"id": m.id, "key": m.key, "content": m.content, "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""} for m in items]}


@app.delete("/api/ai/memories/{mem_id}")
def api_delete_memory(mem_id: int, db: Session = Depends(get_db)):
    """删除一条 AI 记忆"""
    mem = db.query(AIMemory).filter(AIMemory.id == mem_id).first()
    if not mem:
        raise HTTPException(404, "记忆不存在")
    mem.is_active = 0
    db.commit()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# AI 助手：喂养知识 / Agent 执行 / 跟进计划
# ═══════════════════════════════════════════════════════════
@app.post("/api/ai/feed")
def api_ai_feed(req: AIFeedRequest, db: Session = Depends(get_db)):
    ok = ai_agent.add_knowledge(db, req.content, req.source)
    if not ok:
        raise HTTPException(400, "内容不能为空")
    return {"status": "ok", "message": "已喂入 AI 知识库"}


@app.get("/api/ai/knowledge")
def api_ai_knowledge(db: Session = Depends(get_db)):
    return {"items": ai_agent.get_knowledge(db)}


@app.delete("/api/ai/knowledge/{knowledge_id}")
def api_ai_knowledge_delete(knowledge_id: int, db: Session = Depends(get_db)):
    item = db.query(AIKnowledge).filter(AIKnowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "未找到该知识条目")
    db.delete(item)
    db.commit()
    return {"status": "ok", "message": "知识条目已删除"}


def _find_customer(db, keyword):
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    # 1. 精确匹配
    for c in db.query(Customer).all():
        if (c.name or "").lower() == kw or (c.email or "").lower() == kw:
            return c
    # 2. 模糊匹配：keyword 包含 name/email 或反过来
    for c in db.query(Customer).all():
        cname = (c.name or "").lower()
        cemail = (c.email or "").lower()
        if kw in cname or kw in cemail or cname in kw or cemail in kw:
            return c
    # 3. 提取邮箱地址再匹配
    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', kw)
    if email_match:
        email = email_match.group().lower()
        for c in db.query(Customer).all():
            if (c.email or "").lower() == email:
                return c
    # 4. 按空格分词，任一 token 匹配 name 或 email
    tokens = [t for t in kw.split() if len(t) >= 2]
    if tokens:
        for c in db.query(Customer).all():
            cname = (c.name or "").lower()
            cemail = (c.email or "").lower()
            for t in tokens:
                if t in cname or t in cemail:
                    return c
    return None


@app.post("/api/ai/agent")
def api_ai_agent(req: AgentChatRequest, db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")
    # 构建上下文（控制 token 用量）
    ctx_parts = []
    # 1. 知识库 — AI 的"业务知识"
    knowledge = ai_agent.knowledge_text(db, limit=10)
    if knowledge:
        ctx_parts.append("【用户喂养的知识/规则（你必须遵循这些业务知识）】\n" + knowledge)
    # 1.5 AI 记忆 — 用户的重要指令和偏好（跨会话持久化）
    try:
        acc_id = session.account_id if session else None
        mem_q = db.query(AIMemory).filter(AIMemory.is_active == 1)
        if acc_id is not None:
            mem_q = mem_q.filter((AIMemory.account_id == acc_id) | (AIMemory.account_id == None))
        memories = mem_q.order_by(AIMemory.created_at.desc()).limit(20).all()
        if memories:
            mem_lines = []
            for m in memories:
                mem_lines.append(f"- [{m.key or '记忆'}] {m.content}")
            ctx_parts.append("【AI 记忆（用户的重要指令和偏好，你必须在后续操作中遵循和执行）】\n" + "\n".join(mem_lines))
    except Exception as e:
        logger.info(f"[AGENT] memory load error: {e}")
    # 2. 跟进规则
    rules = ai_agent.get_rules(db)
    if rules:
        ctx_parts.append("【跟进规则】\n" + "\n".join([f"- {r.name or chr(35268)}：等级 {r.grade or chr(19981)} / 优先级 {r.priority or chr(19981)}，间隔 {r.interval_days} 天" for r in rules if r.active]))
    # 3. CRM 数据摘要 — 让 AI 了解全局
    try:
        total_customers = db.query(Customer).count()
        hot_customers = db.query(Customer).filter(Customer.priority == "hot").count()
        warm_customers = db.query(Customer).filter(Customer.priority == "warm").count()
        cold_customers = db.query(Customer).filter(Customer.priority == "cold").count()
        total_emails = db.query(Email).count()
        unread_emails = db.query(Email).filter(Email.is_read == False).count()
        analyzed_emails = db.query(Email).filter(Email.ai_analyzed == True).count()
        ctx_parts.append(f"【CRM 数据摘要】\n客户总数: {total_customers} (高优先 {hot_customers} / 中优先 {warm_customers} / 低优先 {cold_customers})\n邮件总数: {total_emails} (未读 {unread_emails} / 已分析 {analyzed_emails})")
    except Exception as e:
        logger.info(f"[AGENT] stats error: {e}")
    # 4. 全部客户列表（带关键信息）— 让 AI 能引用任何客户
    try:
        all_customers = db.query(Customer).order_by(Customer.updated_at.desc()).limit(20).all()
        if all_customers:
            cust_list = []
            for c in all_customers:
                info = f"- {c.name or c.email}"
                if c.email and c.email != c.name: info += f" <{c.email}>"
                if c.company: info += f" [{c.company}]"
                info += f" 优先级={c.priority or '未设'}"
                if c.grade: info += f" 等级={c.grade}"
                cust_list.append(info)
            ctx_parts.append("【客户列表（你可以操作这些客户）】\n" + "\n".join(cust_list))
    except Exception as e:
        logger.info(f"[AGENT] customer list error: {e}")
    # 5. 最近邮件 — 让 AI 能总结邮件
    try:
        recent_emails = db.query(Email).order_by(Email.received_at.desc()).limit(10).all()
        if recent_emails:
            email_list = []
            for e in recent_emails:
                info = f"- {'未读' if not e.is_read else '已读'} {e.sender}：{e.subject[:60]}"
                if e.ai_priority: info += f" [{e.ai_priority}]"
                if e.ai_analyzed:
                    info += " (已分析)"
                email_list.append(info)
            ctx_parts.append("【最近邮件（10封）】\n" + "\n".join(email_list))
    except Exception as e:
        logger.info(f"[AGENT] email list error: {e}")
    # 6. 待跟进计划 — 让 AI 能提醒用户
    try:
        pending_followups = db.query(FollowUp).filter(FollowUp.status == "pending").order_by(FollowUp.next_date.asc()).limit(10).all()
        if pending_followups:
            fu_list = []
            for fu in pending_followups:
                c = db.query(Customer).filter(Customer.id == fu.customer_id).first()
                name = c.name or c.email if c else "未知"
                fu_list.append(f"- {name}：{fu.action or '跟进'} 计划日期={fu.next_date.strftime('%Y-%m-%d') if fu.next_date else '未定'}")
            ctx_parts.append("【待跟进计划（你需要提醒用户关注）】\n" + "\n".join(fu_list))
    except Exception as e:
        logger.info(f"[AGENT] followup list error: {e}")
    # 6.5 跟进规则 — 让 AI 了解当前规则配置
    try:
        rules = db.query(FollowUpRule).filter(FollowUpRule.active == 1).order_by(FollowUpRule.id).all()
        if rules:
            rule_list = []
            for r in rules:
                cond = []
                if r.grade: cond.append(f"等级{r.grade}")
                if r.priority: cond.append(f"优先级{r.priority}")
                cond_str = " + ".join(cond) if cond else "全部客户"
                rule_list.append(f"- {r.name}：{cond_str} → 每{r.interval_days}天跟进")
            ctx_parts.append("【跟进规则】\n" + "\n".join(rule_list))
    except Exception as e:
        logger.info(f"[AGENT] followup rules error: {e}")
    # 7. 当前查看的客户（如果指定了）
    if req.customer_id:
        c = db.query(Customer).filter(Customer.id == req.customer_id).first()
        if c:
            parts = [f"客户: {c.name or c.email}"]
            if c.company: parts.append(f"公司: {c.company}")
            if c.priority: parts.append(f"优先级: {c.priority}")
            if c.grade: parts.append(f"等级: {c.grade}")
            if c.profile_summary:
                try:
                    p = json.loads(c.profile_summary)
                    if isinstance(p, dict):
                        for k, v in p.items():
                            if isinstance(v, str) and v: parts.append(f"{k}: {v}")
                except (json.JSONDecodeError, TypeError):
                    pass
            ctx_parts.append("【当前客户】\n" + "\n".join(parts))
    # 8. 当前查看的邮件（邮件专属AI助手）
    if req.email_id:
        try:
            e = db.query(Email).filter(Email.id == req.email_id).first()
            if e:
                email_parts = [
                    f"主题: {e.subject or '(无主题)'}",
                    f"发件人: {e.sender or '未知'}",
                    f"收件时间: {e.received_at.strftime('%Y-%m-%d %H:%M') if e.received_at else '未知'}",
                ]
                # 获取分析结果
                analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == e.id).first()
                if analysis:
                    if analysis.intent: email_parts.append(f"意图: {analysis.intent}")
                    if analysis.priority: email_parts.append(f"优先级: {analysis.priority}")
                    if analysis.summary: email_parts.append(f"摘要: {analysis.summary}")
                    if analysis.next_action: email_parts.append(f"建议动作: {analysis.next_action}")
                # 邮件正文（截取前2000字）
                body = e.body_text or ""
                if len(body) > 2000:
                    body = body[:2000] + "...（已截断）"
                email_parts.append(f"正文:\n{body}")
                ctx_parts.append("【当前邮件（用户正在查看的邮件，你的回答需要围绕这封邮件）】\n" + "\n".join(email_parts))
        except Exception as e:
            logger.info(f"[AGENT] email context error: {e}")
    ctx = "\n\n".join(ctx_parts)

    # 关键修复：传入对话历史，让 AI 有记忆
    plan = agent_plan(message, ctx, history=req.history)
    reply = plan.get("reply", "")
    actions = plan.get("actions", []) or []
    results = []
    for act in actions[:3]:
        typ = act.get("type", "")
        try:
            if typ == "update_priority":
                c = _find_customer(db, act.get("customer"))
                if c:
                    c.priority = _sanitize_priority(act.get("value", "cold"))
                    c.updated_at = datetime.utcnow()
                    db.commit()
                    results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 优先级已改为 " + {"hot": "高优先级", "warm": "中优先级", "cold": "低优先级"}.get(c.priority, c.priority)})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "update_grade":
                c = _find_customer(db, act.get("customer"))
                if c:
                    c.grade = str(act.get("value", "")).strip().upper()[:20]
                    c.updated_at = datetime.utcnow()
                    db.commit()
                    results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 等级已改为 {c.grade or chr(26410)}"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "update_profile":
                c = _find_customer(db, act.get("customer"))
                if c:
                    field = act.get("field", "")
                    value = act.get("value", "")
                    try:
                        p = json.loads(c.profile_summary) if c.profile_summary else {}
                        if not isinstance(p, dict): p = {}
                    except (json.JSONDecodeError, TypeError):
                        p = {}
                    if field:
                        p[field] = value
                        c.profile_summary = json.dumps(p, ensure_ascii=False)
                        c.updated_at = datetime.utcnow()
                        db.commit()
                        results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 的 {field} 已更新"})
                    else:
                        results.append({"type": typ, "ok": False, "msg": "缺少字段"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "generate_followups":
                acc = None
                if req.customer_id:
                    cc = db.query(Customer).filter(Customer.id == req.customer_id).first()
                    if cc: acc = cc.account_id
                cust = None
                kw = act.get("customer")
                if kw:
                    cc = _find_customer(db, kw)
                    if cc: cust = cc.id
                items = ai_agent.generate_followups(db, acc, cust)
                results.append({"type": typ, "ok": True, "msg": f"已生成/更新 {len(items)} 条跟进计划"})
            elif typ == "complete_followup":
                c = _find_customer(db, act.get("customer"))
                if c:
                    fu = db.query(FollowUp).filter(FollowUp.customer_id == c.id, FollowUp.status == "pending").first()
                    if fu:
                        fu.status = "done"
                        fu.updated_at = datetime.utcnow()
                        db.commit()
                        results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 的跟进已标记完成"})
                    else:
                        results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 暂无待跟进计划"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "get_followup_rules":
                rules = db.query(FollowUpRule).filter(FollowUpRule.active == 1).order_by(FollowUpRule.id).all()
                if rules:
                    rule_lines = []
                    for r in rules:
                        cond = []
                        if r.grade: cond.append(f"等级 {r.grade}")
                        if r.priority: cond.append(f"优先级 {r.priority}")
                        cond_str = " + ".join(cond) if cond else "全部客户"
                        rule_lines.append(f"- {r.name}: {cond_str} → 每 {r.interval_days} 天跟进一次")
                    results.append({"type": typ, "ok": True, "msg": "当前跟进规则：\n" + "\n".join(rule_lines)})
                else:
                    results.append({"type": typ, "ok": True, "msg": "暂无跟进规则"})
            elif typ == "update_followup_rules":
                new_rules = act.get("rules", [])
                if not new_rules or not isinstance(new_rules, list):
                    results.append({"type": typ, "ok": False, "msg": "rules 参数必须是非空数组"})
                    continue
                # 停用现有规则
                for r in db.query(FollowUpRule).filter(FollowUpRule.active == 1).all():
                    r.active = 0
                    r.updated_at = datetime.utcnow()
                # 添加新规则
                added = 0
                for r in new_rules:
                    name = r.get("name", "")
                    if not name:
                        name = f"规则{added+1}"
                    rule = FollowUpRule(
                        name=name,
                        grade=(r.get("grade") or "").upper() if r.get("grade") else "",
                        priority=(r.get("priority") or "").lower() if r.get("priority") else "",
                        interval_days=int(r.get("interval_days", 7)),
                        active=1,
                    )
                    db.add(rule)
                    added += 1
                db.commit()
                results.append({"type": typ, "ok": True, "msg": f"已更新 {added} 条跟进规则，新规则将在下次生成跟进计划时生效"})
            elif typ == "search_customers":
                kw = (act.get("keyword") or act.get("customer") or "").strip()
                if kw:
                    matches = []
                    for c in db.query(Customer).all():
                        if kw.lower() in (c.name or "").lower() or kw.lower() in (c.email or "").lower() or kw.lower() in (c.company or "").lower():
                            matches.append(f"{c.name or c.email} (优先级={c.priority or '未设'}, 等级={c.grade or '未设'})")
                    if matches:
                        results.append({"type": typ, "ok": True, "msg": "找到 " + str(len(matches)) + " 个匹配客户：\n" + "\n".join(matches[:10])})
                    else:
                        results.append({"type": typ, "ok": True, "msg": "未找到匹配 '" + kw + "' 的客户"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "缺少搜索关键词"})
            elif typ == "get_customer_detail":
                c = _find_customer(db, act.get("customer"))
                if c:
                    detail_parts = [f"客户: {c.name or c.email}"]
                    if c.email: detail_parts.append(f"邮箱: {c.email}")
                    if c.company: detail_parts.append(f"公司: {c.company}")
                    detail_parts.append(f"优先级: {c.priority or '未设'}")
                    detail_parts.append(f"等级: {c.grade or '未设'}")
                    if c.profile_summary:
                        try:
                            p = json.loads(c.profile_summary)
                            if isinstance(p, dict):
                                for k, v in p.items():
                                    if isinstance(v, str) and v: detail_parts.append(f"{k}: {v}")
                        except (json.JSONDecodeError, TypeError):
                            pass
                    email_count = db.query(Email).filter(Email.customer_id == c.id).count()
                    detail_parts.append(f"邮件数: {email_count}")
                    pending_fu = db.query(FollowUp).filter(FollowUp.customer_id == c.id, FollowUp.status == "pending").first()
                    if pending_fu:
                        detail_parts.append(f"待跟进: {pending_fu.next_date.strftime('%Y-%m-%d') if pending_fu.next_date else '未定'} - {pending_fu.action or '跟进'}")
                    results.append({"type": typ, "ok": True, "msg": "\n".join(detail_parts)})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "mark_email_read":
                subject_kw = (act.get("email_subject") or act.get("subject") or "").strip()
                sender_kw = (act.get("sender") or "").strip()
                q = db.query(Email).filter(Email.is_read == False)
                if subject_kw:
                    q = q.filter(Email.subject.contains(subject_kw))
                if sender_kw:
                    q = q.filter(Email.sender.contains(sender_kw))
                unread = q.all()
                if unread:
                    for e in unread:
                        e.is_read = True
                    db.commit()
                    names = [e.subject[:40] for e in unread[:5]]
                    results.append({"type": typ, "ok": True, "msg": f"已标记 {len(unread)} 封邮件为已读：\n" + "\n".join(names)})
                else:
                    results.append({"type": typ, "ok": True, "msg": "没有匹配的未读邮件"})
            elif typ == "reply_email_draft":
                results.append({"type": typ, "ok": True, "msg": "回复草稿已在上方消息中生成"})
            elif typ == "analyze_customer":
                c = _find_customer(db, act.get("customer"))
                if c:
                    results.append({"type": typ, "ok": True, "msg": f"{c.name or c.email} 的分析已在上方消息中展示"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到客户 " + str(act.get("customer"))})
            elif typ == "summarize_emails":
                results.append({"type": typ, "ok": True, "msg": "邮件总结已在上方消息中展示"})
            elif typ == "chat":
                results.append({"type": typ, "ok": True, "msg": ""})
            elif typ == "save_memory":
                # 保存用户的重要指令到 AI 记忆
                key = act.get("key") or act.get("title") or "用户指令"
                content = act.get("content") or act.get("value") or act.get("instruction") or ""
                if content:
                    acc_id = session.account_id if session else None
                    mem = AIMemory(key=str(key)[:100], content=str(content), account_id=acc_id, is_active=1)
                    db.add(mem)
                    db.commit()
                    results.append({"type": typ, "ok": True, "msg": f"已记住：{key}"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "记忆内容不能为空"})
            elif typ == "delete_memory":
                # 删除指定的记忆
                key = act.get("key") or ""
                if key:
                    acc_id = session.account_id if session else None
                    mems = db.query(AIMemory).filter(AIMemory.is_active == 1, AIMemory.key.contains(str(key)))
                    if acc_id is not None:
                        mems = mems.filter((AIMemory.account_id == acc_id) | (AIMemory.account_id == None))
                    count = mems.count()
                    for m in mems.all():
                        m.is_active = 0
                    db.commit()
                    results.append({"type": typ, "ok": True, "msg": f"已忘记 {count} 条关于「{key}」的记忆"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "需要指定要忘记的内容关键词"})
            elif typ == "update_email_priority":
                email_kw = act.get("email_subject") or act.get("subject") or ""
                sender_kw = act.get("sender") or ""
                new_prio = _sanitize_priority(act.get("value", "cold"))
                q = db.query(Email)
                if email_kw:
                    q = q.filter(Email.subject.contains(email_kw))
                if sender_kw:
                    q = q.filter(Email.sender.contains(sender_kw))
                emails = q.order_by(Email.received_at.desc()).limit(5).all()
                if emails:
                    for em in emails:
                        em.ai_priority = new_prio
                        analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == em.id).first()
                        if analysis:
                            analysis.priority = new_prio
                        if em.customer_id:
                            cust = db.query(Customer).filter(Customer.id == em.customer_id).first()
                            if cust:
                                _recalc_customer_priority(cust, db)
                    db.commit()
                    prio_label = {"hot": "高优先级", "warm": "中优先级", "cold": "低优先级"}.get(new_prio, new_prio)
                    results.append({"type": typ, "ok": True, "msg": f"已将 {len(emails)} 封邮件优先级改为{prio_label}"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到匹配的邮件"})
            elif typ == "mark_email_replied":
                email_kw = act.get("email_subject") or act.get("subject") or ""
                sender_kw = act.get("sender") or ""
                q = db.query(Email)
                if email_kw:
                    q = q.filter(Email.subject.contains(email_kw))
                if sender_kw:
                    q = q.filter(Email.sender.contains(sender_kw))
                emails = q.order_by(Email.received_at.desc()).limit(5).all()
                if emails:
                    for em in emails:
                        analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == em.id).first()
                        if analysis:
                            analysis.replied = 1
                            analysis.replied_at = datetime.utcnow()
                    db.commit()
                    results.append({"type": typ, "ok": True, "msg": f"已标记 {len(emails)} 封邮件为已回复"})
                else:
                    results.append({"type": typ, "ok": False, "msg": "未找到匹配的邮件"})
            elif typ == "email_reply":
                results.append({"type": typ, "ok": True, "msg": "邮件回复已在上方消息中生成"})
            else:
                results.append({"type": typ, "ok": True, "msg": ""})
        except Exception as e:
            logger.info(f"[AGENT] action {typ} error: {e}")
            results.append({"type": typ, "ok": False, "msg": str(e)})
    # 将动作结果追加到回复中
    action_msgs = [r["msg"] for r in results if r.get("msg")]
    if action_msgs:
        reply += "\n\n" + "\n".join(["✅ " + m for m in action_msgs if m])
    # 保存到会话（如果提供了 conversation_id 或需要创建新会话）
    conv_id = req.conversation_id
    try:
        if conv_id:
            # 更新现有会话
            conv = db.query(Conversation).filter(Conversation.id == conv_id, Conversation.is_deleted == 0).first()
            if conv:
                conv.updated_at = datetime.utcnow()
                # 如果是第一条用户消息，用消息内容的前20字作为标题
                msg_count = db.query(ConversationMessage).filter(ConversationMessage.conversation_id == conv_id).count()
                if msg_count == 0 and message:
                    conv.title = message[:30] + ("..." if len(message) > 30 else "")
        else:
            # 创建新会话
            acc_id = session.account_id if session else None
            conv = Conversation(
                title=message[:30] + ("..." if len(message) > 30 else "") or "新对话",
                conv_type=req.conv_type or "general",
                email_id=req.email_id,
                customer_id=req.customer_id,
                account_id=acc_id,
            )
            db.add(conv)
            db.flush()
            conv_id = conv.id
        # 保存用户消息
        if conv_id:
            user_msg = ConversationMessage(
                conversation_id=conv_id,
                role="user",
                content=message,
                action_results="",
            )
            db.add(user_msg)
            # 保存AI回复消息
            ai_msg = ConversationMessage(
                conversation_id=conv_id,
                role="assistant",
                content=reply,
                action_results=json.dumps(results, ensure_ascii=False) if results else "",
            )
            db.add(ai_msg)
        db.commit()
    except Exception as e:
        logger.info(f"[AGENT] save conversation error: {e}")
        db.rollback()
    return {"reply": reply, "actions": results, "conversation_id": conv_id}


@app.get("/api/ai/reminders")
def api_ai_reminders(db: Session = Depends(get_db)):
    """主动提醒：检查逾期跟进、未读重要邮件、长期未联系客户"""
    reminder_parts = []
    today = datetime.utcnow().date()
    # 1. 逾期跟进
    try:
        overdue = db.query(FollowUp).filter(FollowUp.status == "pending", FollowUp.next_date < today).all()
        if overdue:
            fu_list = []
            for fu in overdue[:10]:
                c = db.query(Customer).filter(Customer.id == fu.customer_id).first()
                name = c.name or c.email if c else "未知"
                days_overdue = (today - fu.next_date.date()).days if fu.next_date else 0
                fu_list.append(f"- {name}：逾期 {days_overdue} 天，计划：{fu.action or '跟进'}")
            reminder_parts.append("【逾期跟进计划（需立即处理）】\n" + "\n".join(fu_list))
    except Exception as e:
        logger.info(f"[REMINDER] overdue error: {e}")
    # 2. 今日跟进
    try:
        today_followups = db.query(FollowUp).filter(FollowUp.status == "pending", FollowUp.next_date >= today, FollowUp.next_date < today + timedelta(days=1)).all()
        if today_followups:
            fu_list = []
            for fu in today_followups:
                c = db.query(Customer).filter(Customer.id == fu.customer_id).first()
                name = c.name or c.email if c else "未知"
                fu_list.append(f"- {name}：{fu.action or '跟进'}")
            reminder_parts.append("【今日跟进】\n" + "\n".join(fu_list))
    except Exception as e:
        logger.info(f"[REMINDER] today error: {e}")
    # 3. 未读重要邮件
    try:
        unread_hot = db.query(Email).filter(Email.is_read == False, Email.ai_priority == "hot").order_by(Email.received_at.desc()).limit(10).all()
        if unread_hot:
            email_list = [f"- {e.sender}：{e.subject[:50]}" for e in unread_hot]
            reminder_parts.append(f"【未读高优先级邮件（{len(unread_hot)} 封）】\n" + "\n".join(email_list))
    except Exception as e:
        logger.info(f"[REMINDER] unread error: {e}")
    # 4. 长期未联系客户（30天无邮件）
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old_customers = db.query(Customer).filter(Customer.updated_at < cutoff).limit(10).all()
        if old_customers:
            cust_list = [f"- {c.name or c.email}（最后更新：{c.updated_at.strftime('%Y-%m-%d') if c.updated_at else '未知'}）" for c in old_customers]
            reminder_parts.append("【长期未联系客户（30天+）】\n" + "\n".join(cust_list))
    except Exception as e:
        logger.info(f"[REMINDER] old customers error: {e}")
    if not reminder_parts:
        return {"reminders": "", "has_urgent": False}
    data = "\n\n".join(reminder_parts)
    text = generate_reminders(data)
    has_urgent = bool(reminder_parts and ("逾期" in reminder_parts[0] or "未读高优先" in str(reminder_parts)))
    return {"reminders": text, "has_urgent": has_urgent, "raw_count": len(reminder_parts)}


# ── 跟进计划 ──
@app.get("/api/followups")
def api_followups(db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    acc = session.account_id if session else None
    return {"items": ai_agent.list_followups(db, acc)}


@app.post("/api/followups/generate")
def api_followups_generate(db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    acc = session.account_id if session else None
    items = ai_agent.generate_followups(db, acc)
    return {"status": "ok", "generated": len(items), "items": items}


@app.post("/api/followups/generate/{customer_id}")
def api_followups_generate_one(customer_id: int, db: Session = Depends(get_db)):
    items = ai_agent.generate_followups(db, None, customer_id)
    return {"status": "ok", "generated": len(items), "items": items}


@app.get("/api/followups/rules")
def api_followups_rules(db: Session = Depends(get_db)):
    return {"rules": ai_agent.rules_to_dict(ai_agent.get_rules(db))}


@app.post("/api/followups/rules")
def api_followups_set_rules(req: FollowUpRulesRequest, db: Session = Depends(get_db)):
    rules = ai_agent.set_rules(db, req.rules)
    return {"status": "ok", "rules": rules}


@app.post("/api/followups/{followup_id}/status")
def api_followups_status(followup_id: int, req: FollowUpStatusRequest, db: Session = Depends(get_db)):
    f = db.query(FollowUp).filter(FollowUp.id == followup_id).first()
    if not f:
        raise HTTPException(404, "未找到该跟进计划")
    f.status = req.status if req.status in ("pending", "done", "skipped") else "pending"
    f.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "id": f.id, "status": f.status}



# ── 基础 API ──
@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/api/email/providers")
def api_providers():
    return list_providers()


@app.get("/api/email/detect-provider")
def api_detect_provider(email: str = Query(...)):
    pid = detect_provider(email)
    p = get_provider(pid)
    return {
        "provider_id": pid,
        "name": p.name if p else "自定义",
        "imap_host": p.imap_host if p else "",
        "imap_port": p.imap_port if p else 993,
        "auth_code_label": p.auth_code_label if p else "密码",
        "help_url": p.help_url if p else "",
    }


# ── 邮箱登录（登录时自动保存账号） ──
@app.post("/api/auth/login")
def api_login(req: LoginRequest, db: Session = Depends(get_db)):
    email = req.email.strip()
    password = req.password

    if not email or not password:
        raise HTTPException(400, "邮箱和密码不能为空")

    provider_id = req.provider_id
    if provider_id in ("", "auto"):
        provider_id = detect_provider(email)

    provider = get_provider(provider_id)
    if not provider:
        raise HTTPException(400, f"未知的服务商: {provider_id}")

    imap_host = req.imap_host or provider.imap_host
    imap_port = req.imap_port or provider.imap_port

    if not imap_host:
        raise HTTPException(400, "无法确定 IMAP 服务器地址，请手动填写")

    # 测试 IMAP 连接
    try:
        test_imap_connection(imap_host, imap_port, email, password)
    except Exception as e:
        raise HTTPException(401, f"邮箱连接失败: {e}")

    # 保存或更新账号到数据库
    account_id = None
    if req.save_account:
        account = db.query(EmailAccount).filter(EmailAccount.email == email).first()
        if account:
            # 更新已有账号的凭证
            account.provider_id = provider_id
            account.provider_name = provider.name
            account.imap_host = imap_host
            account.imap_port = imap_port
            account.auth_code = auth.encrypt(password)
            account.last_login_at = datetime.utcnow()
            db.commit()
            account_id = account.id
        else:
            # 创建新账号
            account = EmailAccount(
                email=email,
                provider_id=provider_id,
                provider_name=provider.name,
                imap_host=imap_host,
                imap_port=imap_port,
                auth_code=auth.encrypt(password),
                last_login_at=datetime.utcnow(),
            )
            db.add(account)
            db.commit()
            db.refresh(account)
            account_id = account.id

    # 创建会话
    token = auth.create_session(
        email=email,
        password=password,
        provider_id=provider_id,
        imap_host=imap_host,
        imap_port=imap_port,
        account_id=account_id,
    )

    return {
        "status": "ok",
        "token": token,
        "email": auth.mask_email(email),
        "provider": provider.name,
        "provider_id": provider_id,
        "account_id": account_id,
        "message": "登录成功",
    }


@app.post("/api/auth/logout")
def api_logout(session: Optional[auth.Session] = Depends(get_current_session),
               authorization: Optional[str] = Header(None)):
    token = _extract_token(authorization)
    auth.destroy_session(token)
    return {"status": "ok", "message": "已退出登录"}


@app.get("/api/auth/status")
def api_auth_status(session: Optional[auth.Session] = Depends(get_current_session)):
    if session:
        provider_name = None
        p = get_provider(session.provider_id)
        if p:
            provider_name = p.name
        return {
            "logged_in": True,
            "email": auth.mask_email(session.email),
            "raw_email": session.email,
            "provider_id": session.provider_id,
            "provider_name": provider_name,
            "account_id": session.account_id,
        }
    return {"logged_in": False}


# ── 账号管理 API ──

@app.post("/api/accounts/list")
def list_accounts(req: dict, db: Session = Depends(get_db)):
    """只返回当前浏览器本地保存的账号，确保数据隔离"""
    account_ids = req.get("my_account_ids", [])
    if not account_ids:
        return []
    accounts = db.query(EmailAccount).filter(EmailAccount.id.in_(account_ids)).order_by(desc(EmailAccount.last_login_at)).all()
    result = []
    for a in accounts:
        result.append({
            "id": a.id,
            "email": a.email,
            "provider_id": a.provider_id,
            "provider_name": a.provider_name or "",
            "imap_host": a.imap_host,
            "imap_port": a.imap_port,
            "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
        })
    return result


@app.post("/api/accounts/switch")
def api_switch_account(req: SwitchAccountRequest,
                       db: Session = Depends(get_db)):
    """切换到已保存的账号（验证 account_id 在本地账号列表中）"""
    if req.account_id not in (req.my_account_ids or []):
        raise HTTPException(403, "无权切换到此账号")
    account = db.query(EmailAccount).filter(EmailAccount.id == req.account_id).first()
    if not account:
        raise HTTPException(404, "账号不存在")

    password = auth.decrypt(account.auth_code)
    if not password:
        raise HTTPException(400, "账号凭证已失效，请重新登录")

    # 创建会话
    token = auth.create_session(
        email=account.email,
        password=password,
        provider_id=account.provider_id,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        account_id=account.id,
    )

    # 更新最后登录时间
    account.last_login_at = datetime.utcnow()
    db.commit()

    return {
        "status": "ok",
        "token": token,
        "email": auth.mask_email(account.email),
        "provider": account.provider_name or "",
        "provider_id": account.provider_id,
        "account_id": account.id,
        "message": "切换成功",
    }


@app.post("/api/accounts/delete")
def api_delete_account(req: dict, db: Session = Depends(get_db)):
    """删除账号（验证 account_id 在本地账号列表中）"""
    account_id = req.get("account_id")
    my_account_ids = req.get("my_account_ids", [])
    if account_id not in my_account_ids:
        raise HTTPException(403, "无权删除此账号")
    account = db.query(EmailAccount).filter(EmailAccount.id == account_id).first()
    if not account:
        raise HTTPException(404, "账号不存在")

    # 清空该账号的客户和邮件数据
    clear_account_data(account_id)

    db.delete(account)
    db.commit()
    return {"status": "ok", "message": "账号已删除"}


# ── AI 状态 ──
@app.get("/api/ai/status")
def api_ai_status():
    return get_ai_status()


# ── 客户列表（按当前账号隔离，按优先级排序） ──
@app.get("/api/customers")
def list_customers(db: Session = Depends(get_db),
                   session: Optional[auth.Session] = Depends(get_current_session)):
    # 未登录时不返回任何数据
    if not session or session.account_id is None:
        return []
    query = db.query(Customer).filter(Customer.account_id == session.account_id)
    customers = query.all()

    # 优先级排序映射：待分析(pending)排在 hot/warm 和 cold 之间
    priority_order = {"hot": 0, "warm": 1, "pending": 2, "cold": 3}

    result = []
    for c in customers:
        email_query = db.query(func.count(Email.id)).filter(Email.customer_id == c.id)
        email_count = email_query.scalar()
        has_analysis = db.query(EmailAnalysis).join(Email, EmailAnalysis.email_id == Email.id).filter(
            Email.customer_id == c.id
        ).count()

        # 获取最新一封邮件的分析信息
        latest_email = db.query(Email).filter(Email.customer_id == c.id).order_by(desc(Email.received_at)).first()
        latest_summary = ""
        latest_no_reply = False
        latest_intent = ""
        if latest_email:
            latest_analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == latest_email.id).first()
            if latest_analysis:
                latest_summary = latest_analysis.summary or ""
                latest_no_reply = bool(latest_analysis.no_reply_needed)
                latest_intent = latest_analysis.intent or ""

        # 统计 WhatsApp 消息数
        wa_count = db.query(WhatsAppMessage).filter(WhatsAppMessage.customer_id == c.id).count()

        # 判断是否为待分析状态
        # 1. 有邮件但没有任何分析记录 → pending
        # 2. AI 分析进行中，且有未分析的邮件 → pending（防止提前显示低优先级）
        unanalyzed_count = email_count - has_analysis
        if email_count > 0 and has_analysis == 0:
            p = "pending"
            is_pending = True
        elif _is_analyzing(session.account_id) and unanalyzed_count > 0:
            p = "pending"
            is_pending = True
        else:
            p = c.priority or "cold"
            is_pending = False
        result.append({
            "id": c.id,
            "email": c.email,
            "domain": c.domain or "",
            "company": c.company or "",
            "name": c.name or "",
            "country": c.country or "",
            "priority": p,
            "priority_order": priority_order.get(p, 3),
            "grade": c.grade or "",
            "last_contact_at": c.last_contact_at.isoformat() if c.last_contact_at else None,
            "email_count": email_count,
            "has_analysis": has_analysis > 0,
            "is_pending": is_pending,
            "latest_summary": latest_summary,
            "latest_no_reply": latest_no_reply,
            "latest_intent": latest_intent,
            "wa_message_count": wa_count,
        })

    # 按优先级排序：高优先级在前，同优先级按最近联系时间倒序
    from itertools import groupby
    # groupby 要求先按 key 排序
    result.sort(key=lambda x: x["priority_order"])
    sorted_result = []
    for prio, group in groupby(result, key=lambda x: x["priority_order"]):
        group_list = sorted(group, key=lambda x: x["last_contact_at"] or "", reverse=True)
        sorted_result.extend(group_list)

    return sorted_result


# ── 客户详情 ──
@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(404, "未找到该客户")

    emails = db.query(Email).filter(Email.customer_id == customer_id).order_by(desc(Email.received_at)).all()
    email_list = []
    for e in emails:
        analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == e.id).first()
        email_list.append({
            "id": e.id,
            "subject": e.subject,
            "sender": e.sender,
            "body_text_preview": (e.body_text or "")[:200],
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "analysis": {
                "priority": analysis.priority if analysis else None,
                "summary": analysis.summary if analysis else None,
                "intent": analysis.intent if analysis else None,
            } if analysis else None,
        })

    # 获取 WhatsApp 消息
    wa_messages = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.customer_id == customer_id
    ).order_by(WhatsAppMessage.timestamp.asc()).all()
    wa_list = []
    for m in wa_messages:
        wa_list.append({
            "id": m.id,
            "remote_id": m.remote_id,
            "sender": m.sender,
            "message_text": m.message_text,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            "is_from_me": bool(m.is_from_me),
        })

    # 获取 WA 分析结果
    wa_analysis = None
    if c.profile_summary:
        try:
            profile = json.loads(c.profile_summary)
            if isinstance(profile, dict) and "wa_analysis" in profile:
                wa_analysis = profile["wa_analysis"]
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": c.id,
        "email": c.email,
        "domain": c.domain or "",
        "company": c.company or "",
        "name": c.name or "",
        "country": c.country or "",
        "priority": c.priority or "cold",
        "grade": c.grade or "",
        "profile_summary": c.profile_summary or "",
        "last_contact_at": c.last_contact_at.isoformat() if c.last_contact_at else None,
        "emails": email_list,
        "wa_messages": wa_list,
        "wa_analysis": wa_analysis,
    }


# ── 更新客户（优先等级 / 客户等级 / 画像摘要） ──
@app.post("/api/customers/{customer_id}/update")
def update_customer(customer_id: int, req: CustomerUpdateRequest, db: Session = Depends(get_db)):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(404, "未找到该客户")

    if req.priority is not None:
        c.priority = _sanitize_priority(req.priority)
    if req.grade is not None:
        c.grade = str(req.grade).strip()[:20]

    if req.profile is not None and isinstance(req.profile, dict):
        try:
            profile = json.loads(c.profile_summary) if c.profile_summary else {}
            if not isinstance(profile, dict):
                profile = {}
        except (json.JSONDecodeError, TypeError):
            profile = {}
        for k, v in req.profile.items():
            if v is None:
                continue
            profile[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        c.profile_summary = json.dumps(profile, ensure_ascii=False)

    c.updated_at = datetime.utcnow()
    db.commit()

    return {
        "status": "ok",
        "id": c.id,
        "priority": c.priority,
        "grade": c.grade or "",
        "profile_summary": c.profile_summary or "",
    }


# ── AI 对话 ──
@app.post("/api/ai/chat")
def ai_chat(req: ChatRequest, db: Session = Depends(get_db)):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")

    customer_context = ""
    if req.customer_id:
        c = db.query(Customer).filter(Customer.id == req.customer_id).first()
        if c:
            parts = [f"客户: {c.name or c.email}"]
            if c.company: parts.append(f"公司: {c.company}")
            if c.country: parts.append(f"国家: {c.country}")
            if c.priority: parts.append(f"当前优先级: {c.priority}")
            if c.grade: parts.append(f"客户等级: {c.grade}级")
            if c.profile_summary:
                try:
                    p = json.loads(c.profile_summary)
                    if isinstance(p, dict):
                        for k, v in p.items():
                            if isinstance(v, str) and v:
                                parts.append(f"{k}: {v}")
                except (json.JSONDecodeError, TypeError):
                    parts.append(f"画像摘要: {str(c.profile_summary)[:800]}")
            emails = db.query(Email).filter(Email.customer_id == c.id).order_by(desc(Email.received_at)).limit(5).all()
            if emails:
                parts.append("最近邮件:")
                for e in emails:
                    parts.append(f"- [{e.received_at}] {e.subject}: {(e.body_text or '')[:200]}")
            customer_context = "\n".join(parts)

    reply = chat_with_ai(message, customer_context=customer_context, history=req.history or [])
    return {"reply": reply}


# ── AI 分析状态查询（必须在 /api/emails/{email_id} 之前定义） ──
@app.get("/api/emails/analyze-status")
def api_analyze_status(session: Optional[auth.Session] = Depends(get_current_session)):
    """查询 AI 分析是否仍在进行中，以及各优先级客户数量"""
    db = SessionLocal()
    try:
        # 统计当前账号各优先级客户数
        account_id = session.account_id if session else None
        if account_id is None:
            return {"analyzing": _is_analyzing(), "hot": 0, "warm": 0, "cold": 0}
        hot = db.query(Customer).filter(Customer.account_id == account_id, Customer.priority == "hot").count()
        warm = db.query(Customer).filter(Customer.account_id == account_id, Customer.priority == "warm").count()
        cold = db.query(Customer).filter(Customer.account_id == account_id, Customer.priority == "cold").count()
        return {"analyzing": _is_analyzing(account_id), "hot": hot, "warm": warm, "cold": cold}
    finally:
        db.close()


# ── 邮件详情 ──
@app.get("/api/emails/{email_id}")
def get_email(email_id: int, db: Session = Depends(get_db)):
    e = db.query(Email).filter(Email.id == email_id).first()
    if not e:
        raise HTTPException(404, "未找到该邮件")

    customer = db.query(Customer).filter(Customer.id == e.customer_id).first()
    analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == e.id).first()

    return {
        "id": e.id,
        "customer_id": e.customer_id,
        "customer_name": customer.name if customer else "",
        "customer_email": customer.email if customer else "",
        "subject": e.subject,
        "sender": e.sender,
        "recipients": e.recipients or "",
        "body_text": e.body_text or "",
        "body_html": e.body_html or "",
        "received_at": e.received_at.isoformat() if e.received_at else None,
        "analysis": {
            "id": analysis.id if analysis else None,
            "intent": analysis.intent if analysis else "",
            "priority": analysis.priority if analysis else "",
            "summary": analysis.summary if analysis else "",
            "next_action": analysis.next_action if analysis else "",
            "reply_draft": analysis.reply_draft if analysis else "",
            "no_reply_needed": bool(analysis.no_reply_needed) if analysis else False,
            "replied": bool(analysis.replied) if analysis else False,
            "replied_at": analysis.replied_at.isoformat() if (analysis and analysis.replied_at) else None,
        } if analysis else None,
    }


# ── 构建客户画像 ──
@app.post("/api/profile/build")
def api_build_profile(req: CustomerProfileRequest, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == req.customer_id).first()
    if not customer:
        raise HTTPException(404, "未找到该客户")

    emails = db.query(Email).filter(Email.customer_id == req.customer_id).order_by(desc(Email.received_at)).all()
    email_history = "\n---\n".join([
        f"[{e.received_at}] {e.subject}: {(e.body_text or '')[:500]}"
        for e in emails
    ])
    customer_info = f"{customer.name or customer.email}, {customer.company or ''}, {customer.country or ''}"

    result = build_customer_profile(customer_info, email_history)
    customer.profile_summary = json.dumps(result, ensure_ascii=False)
    if result.get("suggested_priority"):
        customer.priority = _sanitize_priority(result["suggested_priority"])
    customer.updated_at = datetime.utcnow()
    db.commit()

    return {"status": "ok", "profile": result}


# ── 分析单封邮件 ──
@app.post("/api/email/analyze")
def api_analyze_email(req: EmailAnalysisRequest, db: Session = Depends(get_db)):
    email_record = db.query(Email).filter(Email.id == req.email_id).first()
    if not email_record:
        raise HTTPException(404, "未找到该邮件")

    customer = db.query(Customer).filter(Customer.id == email_record.customer_id).first()
    profile = customer.profile_summary if customer else ""

    recent = db.query(Email).filter(
        Email.customer_id == email_record.customer_id,
        Email.id != email_record.id
    ).order_by(desc(Email.received_at)).limit(10).all()
    recent_text = "\n".join([
        f"[{e.received_at}] {e.subject}: {(e.body_text or '')[:300]}"
        for e in reversed(recent)
    ])

    new_email = {"subject": email_record.subject, "body": email_record.body_text or ""}
    result = analyze_email(profile, recent_text, new_email, language=req.language)

    analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == req.email_id).first()
    if not analysis:
        analysis = EmailAnalysis(email_id=req.email_id)
        db.add(analysis)

    analysis.intent = result.get("intent", "")
    analysis.priority = _sanitize_priority(result.get("priority", "cold"))
    analysis.summary = result.get("summary", "")
    analysis.next_action = result.get("next_action", "")
    analysis.reply_draft = result.get("reply_draft", "")
    analysis.raw_response = json.dumps(result, ensure_ascii=False)
    analysis.no_reply_needed = 1 if result.get("no_reply_needed") else 0
    # 同步更新 Email 表的 ai_priority 和 ai_analyzed 字段
    email_record.ai_priority = analysis.priority
    email_record.ai_analyzed = 1
    db.commit()

    if customer:
        _recalc_customer_priority(customer, db)
        db.commit()

    return {"status": "ok", "analysis": result}


# ── 标记邮件为已回复 ──
@app.post("/api/email/mark-replied")
def api_mark_email_replied(req: EmailMarkRepliedRequest, db: Session = Depends(get_db)):
    """标记邮件为已回复，并将高/中优先级降为低优先级（无需回复）"""
    analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == req.email_id).first()
    if not analysis:
        raise HTTPException(404, "未找到该邮件的 AI 分析记录")

    # 标记为已回复
    analysis.replied = 1
    analysis.replied_at = datetime.utcnow()

    old_priority = analysis.priority
    new_priority = old_priority

    # 高/中优先级自动降为低优先级
    if analysis.priority in ("hot", "warm"):
        analysis.priority = "cold"
        new_priority = "cold"

    # 同步更新 Email 表的 ai_priority
    email = db.query(Email).filter(Email.id == req.email_id).first()
    if email:
        email.ai_priority = analysis.priority
    customer_updated = False
    if email:
        customer = db.query(Customer).filter(Customer.id == email.customer_id).first()
        if customer:
            old_cust_priority = customer.priority
            _recalc_customer_priority(customer, db)
            if customer.priority != old_cust_priority:
                customer_updated = True

    db.commit()

    return {
        "status": "ok",
        "email_id": req.email_id,
        "old_priority": old_priority,
        "new_priority": new_priority,
        "replied": True,
        "customer_updated": customer_updated,
    }


# ── 取消已回复标记 ──
@app.post("/api/email/unmark-replied")
def api_unmark_email_replied(req: EmailMarkRepliedRequest, db: Session = Depends(get_db)):
    """取消已回复标记（误操作时使用）"""
    analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == req.email_id).first()
    if not analysis:
        raise HTTPException(404, "未找到该邮件的 AI 分析记录")

    analysis.replied = 0
    analysis.replied_at = None

    # 恢复 Email 表的 ai_priority 并重新计算客户优先级
    email = db.query(Email).filter(Email.id == req.email_id).first()
    if email:
        email.ai_priority = analysis.priority
        customer = db.query(Customer).filter(Customer.id == email.customer_id).first()
        if customer:
            _recalc_customer_priority(customer, db)

    db.commit()

    return {"status": "ok", "email_id": req.email_id, "replied": False}


# ── 手动修改邮件优先级 ──
class EmailPriorityUpdateRequest(BaseModel):
    email_id: int
    priority: str

@app.post("/api/email/update-priority")
def api_update_email_priority(req: EmailPriorityUpdateRequest, db: Session = Depends(get_db)):
    """手动修改邮件优先级（同时更新AI分析记录和客户优先级）"""
    email = db.query(Email).filter(Email.id == req.email_id).first()
    if not email:
        raise HTTPException(404, "邮件不存在")

    new_priority = _sanitize_priority(req.priority)
    old_priority = email.ai_priority
    email.ai_priority = new_priority

    # 同步更新AI分析记录
    analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == req.email_id).first()
    if analysis:
        analysis.priority = new_priority
    else:
        # 如果没有分析记录，创建一条
        analysis = EmailAnalysis(
            email_id=req.email_id,
            priority=new_priority,
            intent="手动设置",
            summary="用户手动修改优先级",
            next_action="",
            reply_draft="",
            replied=0,
        )
        db.add(analysis)

    # 重新计算客户优先级
    if email.customer_id:
        customer = db.query(Customer).filter(Customer.id == email.customer_id).first()
        if customer:
            _recalc_customer_priority(customer, db)

    db.commit()
    return {"status": "ok", "email_id": req.email_id, "old_priority": old_priority, "new_priority": new_priority}


# ── 拉取邮件 ──
@app.post("/api/emails/fetch")
def api_fetch_emails(db: Session = Depends(get_db), session: Optional[auth.Session] = Depends(get_current_session)):
    if not session:
        return JSONResponse(
            status_code=401,
            content={"status": "error", "message": "请先登录邮箱后再拉取邮件"},
        )
    try:
        count, new_email_ids = fetch_new_emails(session)
        # 返回新邮件清单供用户筛选（保留/删除，广告/提醒类可删除避免进客户名单）
        new_emails = []
        if new_email_ids:
            rows = db.query(Email).filter(Email.id.in_(new_email_ids)).all()
            for e in rows:
                cname = ""
                if e.customer_id:
                    c = db.query(Customer).filter(Customer.id == e.customer_id).first()
                    if c: cname = c.name or c.email or ""
                new_emails.append({"id": e.id, "subject": e.subject or "", "sender": e.sender or "", "customer_name": cname})
        # Token 用量控制：轻量模式下拉取后不自动深度分析，交由用户筛选后再分析
        analyzed = 0
        analyzing = False
        if new_email_ids and not AI_LIGHT_ANALYSIS:
            _auto_analyze_emails_async(new_email_ids, session.account_id)
            analyzed = len(new_email_ids)
            analyzing = True
        return {
            "status": "ok",
            "fetched": count,
            "analyzed": analyzed,
            "analyzing": analyzing,
            "light_mode": AI_LIGHT_ANALYSIS,
            "new_emails": new_emails,
            "message": (f"拉取 {count} 封邮件" + ("，AI 正在后台分析中..." if analyzing else "，请筛选保留/删除")) if count > 0 else "没有新邮件",
        }
    except RuntimeError as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


def _auto_analyze_emails(email_ids: list[int], account_id=None) -> int:
    """批量自动分析邮件，返回成功分析的数量。
    每分析完一封邮件就 commit，确保前端可以增量看到优先级更新。
    account_id 用于按账号隔离分析状态标志。"""
    db = SessionLocal()
    analyzed = 0
    _set_analyzing(account_id, True)
    try:
        for eid in email_ids:
            email_record = db.query(Email).filter(Email.id == eid).first()
            if not email_record:
                continue
            # 跳过已分析的
            existing = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == eid).first()
            if existing:
                continue

            customer = db.query(Customer).filter(Customer.id == email_record.customer_id).first()
            profile = customer.profile_summary if customer else ""

            recent = db.query(Email).filter(
                Email.customer_id == email_record.customer_id,
                Email.id != eid
            ).order_by(desc(Email.received_at)).limit(5).all()
            recent_text = "\n".join([
                f"[{e.received_at}] {e.subject}: {(e.body_text or '')[:200]}"
                for e in reversed(recent)
            ])

            new_email = {"subject": email_record.subject, "body": email_record.body_text or "", "sender": email_record.sender or ""}
            try:
                result = analyze_email(profile, recent_text, new_email)
                analysis = EmailAnalysis(email_id=eid)
                analysis.intent = result.get("intent", "")
                analysis.priority = _sanitize_priority(result.get("priority", "cold"))
                analysis.summary = result.get("summary", "")
                analysis.next_action = result.get("next_action", "")
                analysis.reply_draft = result.get("reply_draft", "")
                analysis.raw_response = json.dumps(result, ensure_ascii=False)
                analysis.no_reply_needed = 1 if result.get("no_reply_needed") else 0
                db.add(analysis)

                # 同步更新 Email 表的 ai_priority 和 ai_analyzed 字段
                email_record.ai_priority = analysis.priority
                email_record.ai_analyzed = 1

                # 更新客户优先级
                if customer:
                    _recalc_customer_priority(customer, db)
                # 每封邮件分析完后立即提交，让前端可以增量看到更新
                db.commit()
                analyzed += 1
                logger.info(f"[AUTO-ANALYZE] Email {eid} analyzed ({analyzed}/{len(email_ids)})")
            except Exception as e:
                logger.error(f"[AUTO-ANALYZE] Failed for email {eid}: {e}")
                db.rollback()
                continue
        logger.info(f"[AUTO-ANALYZE] Analyzed {analyzed}/{len(email_ids)} emails")
    except Exception as e:
        db.rollback()
        logger.error(f"[AUTO-ANALYZE] Error: {e}")
    finally:
        db.close()
        _set_analyzing(account_id, False)
    return analyzed


def _auto_analyze_emails_async(email_ids: list[int], account_id=None):
    """在后台线程中运行 AI 分析，不阻塞 fetch 请求的返回。
    关键：在启动线程前就设置分析状态为 True，
    避免前端轮询时因竞态条件读到 False 而提前显示低优先级。
    """
    if not email_ids:
        return
    # 先标记为分析中，防止前端在后台线程启动前读到 False
    _set_analyzing(account_id, True)
    thread = threading.Thread(target=_auto_analyze_emails, args=(email_ids, account_id), daemon=True)
    thread.start()
    logger.info(f"[AUTO-ANALYZE] Background thread started for {len(email_ids)} emails (account_id={account_id})")


# ── 清空数据 ──
@app.post("/api/data/clear")
def api_clear_data(db: Session = Depends(get_db),
                   session: Optional[auth.Session] = Depends(get_current_session)):
    # 必须登录才能清空数据
    if not session or session.account_id is None:
        raise HTTPException(401, "请先登录后再操作")
    ok = clear_account_data(session.account_id)
    if ok:
        return {"status": "ok", "message": "当前账号数据已清空"}
    return JSONResponse(status_code=500, content={"status": "error", "message": "清空数据失败"})


# ── 删除单封邮件 ──
@app.delete("/api/emails/{email_id}")
def api_delete_email(email_id: int,
                     session: Optional[auth.Session] = Depends(get_current_session)):
    """删除单封邮件，记录到已删除表防止再次拉取。
    如果该客户没有其他邮件，则自动删除客户。"""
    account_id = session.account_id if session else None
    ok = delete_email(email_id, account_id)
    if not ok:
        raise HTTPException(404, "邮件不存在或删除失败")
    return {"status": "ok", "message": "邮件已删除"}


# ── 删除单个客户 ──
@app.delete("/api/customers/{customer_id}")
def api_delete_customer(customer_id: int):
    """删除单个客户及其所有邮件"""
    ok = delete_customer(customer_id)
    if not ok:
        raise HTTPException(404, "客户不存在或删除失败")
    return {"status": "ok", "message": "客户及关联邮件已删除"}


# ── 仪表盘统计（按当前账号隔离） ──
@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db),
              session: Optional[auth.Session] = Depends(get_current_session)):
    # 未登录时返回零值
    if not session or session.account_id is None:
        return {
            "total_customers": 0,
            "total_emails": 0,
            "analyzed_emails": 0,
            "hot_customers": 0,
            "warm_customers": 0,
            "cold_customers": 0,
            "pending_customers": 0,
        }
    cust_query = db.query(Customer).filter(Customer.account_id == session.account_id)
    email_query = db.query(Email).filter(Email.account_id == session.account_id)

    total_customers = cust_query.count()
    total_emails = email_query.count()

    # analyzed 统计需要关联账号
    analyzed_query = db.query(EmailAnalysis).join(Email, EmailAnalysis.email_id == Email.id)
    analyzed_query = analyzed_query.filter(Email.account_id == session.account_id)
    analyzed = analyzed_query.count()

    # 找出有邮件但尚未分析的客户 ID（这些客户不应计入低优先级，而是"分析中"）
    # 子查询：有邮件的客户
    customers_with_emails = db.query(Email.customer_id).filter(
        Email.account_id == session.account_id
    ).distinct().subquery().select()
    # 子查询：有分析记录的客户
    customers_with_analysis = db.query(Email.customer_id).join(
        EmailAnalysis, EmailAnalysis.email_id == Email.id
    ).filter(
        Email.account_id == session.account_id
    ).distinct().subquery().select()

    # pending = 有邮件但没有分析记录的客户
    pending_customer_ids = db.query(Customer.id).filter(
        Customer.account_id == session.account_id,
        Customer.id.in_(customers_with_emails),
        ~Customer.id.in_(customers_with_analysis)
    ).all()
    pending_ids_set = {cid[0] for cid in pending_customer_ids}

    hot = 0
    warm = 0
    cold = 0
    pending = 0
    all_customers = cust_query.all()
    for c in all_customers:
        if c.id in pending_ids_set:
            pending += 1  # 待分析的客户不计入任何优先级
            continue
        # AI 分析进行中时，有未分析邮件的客户也不计入低优先级
        if _is_analyzing(session.account_id):
            email_count = db.query(Email).filter(Email.customer_id == c.id, Email.account_id == session.account_id).count()
            analysis_count = db.query(EmailAnalysis).join(Email, EmailAnalysis.email_id == Email.id).filter(
                Email.customer_id == c.id, Email.account_id == session.account_id
            ).count()
            if email_count > 0 and analysis_count < email_count:
                pending += 1
                continue
        p = c.priority or "cold"
        if p == "hot":
            hot += 1
        elif p == "warm":
            warm += 1
        else:
            cold += 1

    return {
        "total_customers": total_customers,
        "total_emails": total_emails,
        "analyzed_emails": analyzed,
        "hot_customers": hot,
        "warm_customers": warm,
        "cold_customers": cold,
        "pending_customers": pending,
        "today_new_customers": db.query(Customer).filter(Customer.account_id == session.account_id, Customer.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)).count() if session and session.account_id else 0,
    }


# ── 邮件列表（按优先级排序，带分析信息） ──
@app.get("/api/emails")
def list_emails(db: Session = Depends(get_db),
                session: Optional[auth.Session] = Depends(get_current_session),
                priority: Optional[str] = Query(None),
                limit: int = Query(100, le=500)):
    """获取邮件列表，按优先级排序（hot > warm > cold），包含 AI 分析结果"""
    # 未登录时不返回任何数据
    if not session or session.account_id is None:
        return []
    query = db.query(Email).filter(Email.account_id == session.account_id)

    emails = query.order_by(desc(Email.received_at)).limit(limit).all()

    # 优先级排序映射
    priority_order = {"hot": 0, "warm": 1, "pending": 2, "cold": 3}

    result = []
    for e in emails:
        analysis = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == e.id).first()
        customer = db.query(Customer).filter(Customer.id == e.customer_id).first()

        # 如果 AI 分析进行中且此邮件没有分析记录，标记为 pending 而非 cold
        if not analysis:
            if _is_analyzing(session.account_id):
                p = "pending"
            else:
                p = "cold"
        else:
            p = analysis.priority
        no_reply = bool(analysis.no_reply_needed) if analysis else False
        replied = bool(analysis.replied) if analysis else False

        result.append({
            "id": e.id,
            "subject": e.subject or "(No Subject)",
            "sender": e.sender,
            "customer_id": e.customer_id,
            "customer_name": customer.name if customer else (e.sender or "").split("@")[0],
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "priority": p,
            "priority_order": priority_order.get(p, 3),
            "intent": analysis.intent if analysis else "",
            "summary": analysis.summary if analysis else "",
            "next_action": analysis.next_action if analysis else "",
            "no_reply_needed": no_reply,
            "replied": replied,
            "has_analysis": analysis is not None,
            "is_pending": p == "pending",
        })

    # 按优先级排序：高优先级在前，同优先级按时间倒序
    from itertools import groupby
    # groupby 要求先按 key 排序
    result.sort(key=lambda x: x["priority_order"])
    sorted_result = []
    for prio, group in groupby(result, key=lambda x: x["priority_order"]):
        group_list = sorted(group, key=lambda x: x["received_at"] or "", reverse=True)
        sorted_result.extend(group_list)

    # 如果指定了优先级过滤
    if priority:
        sorted_result = [r for r in sorted_result if r["priority"] == priority]

    return sorted_result


# ── 获取邮箱网页版写邮件地址 ──
@app.get("/api/email/compose-url")
def get_compose_url(session: Optional[auth.Session] = Depends(get_current_session)):
    """返回当前登录邮箱的网页版写邮件地址"""
    if not session:
        raise HTTPException(401, "请先登录邮箱")
    provider = get_provider(session.provider_id)
    if not provider:
        raise HTTPException(400, "未知的服务商")
    return {
        "webmail_url": provider.webmail_url,
        "compose_url": provider.compose_url,
        "provider_name": provider.name,
        "provider_id": session.provider_id,
    }


# ── WhatsApp API ──

# WhatsApp 聊天记录正则: 支持多种导出格式
# 覆盖 Android、iOS、WhatsApp Web、不同语言区域、12/24小时制、带时区等
_WA_MSG_PATTERNS = [
    # ── 带方括号格式 (iOS / WhatsApp Web) ──
    # [2026-07-25 14:30:00] Sender: Message  或  [2026-07-25, 14:30:00] Sender: Message
    re.compile(r'\[(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*(.+?):\s+(.*)'),
    # [25/07/2026, 14:30:00] Sender: Message (iOS 欧式)
    re.compile(r'\[(\d{1,2}[/]\d{1,2}[/]\d{4})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*(.+?):\s+(.*)'),
    # 带时区: [2026-07-25 14:30:00 UTC+8] Sender: Message
    re.compile(r'\[(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[A-Za-z]{2,5}(?:[+-]\d{1,2}(?::?\d{2})?)?\]\s*(.+?):\s+(.*)'),

    # ── 横线分隔格式 (Android 默认) ──
    # 2026-07-25 14:30 - Sender: Message  或  2026-07-25, 14:30 - Sender: Message
    re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),
    # 带时区: 2026-07-25 14:30 UTC - Sender: Message
    re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[A-Za-z]{2,5}(?:[+-]\d{1,2}(?::?\d{2})?)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 美式日期 MM/DD/YYYY 或 DD/MM/YYYY ──
    # 07/25/2026, 14:30 - Sender: Message
    re.compile(r'(\d{1,2}[/]\d{1,2}[/]\d{4})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 两位年份格式 MM/DD/YY 或 DD/MM/YY ──
    # 7/25/26, 2:30 PM - Sender: Message
    re.compile(r'(\d{1,2}[/]\d{1,2}[/]\d{2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 德式日期 DD.MM.YYYY ──
    # 25.07.2026, 14:30 - Sender: Message
    re.compile(r'(\d{1,2}[.]\d{1,2}[.]\d{4})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 英文月份格式 ──
    # Jul 25, 2026 2:30 PM - Sender: Message
    re.compile(r'([A-Z][a-z]{2}\s+\d{1,2},?\s+\d{4})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 中文日期 ──
    # 2026年7月25日 14:30 - Sender: Message
    re.compile(r'(\d{4}年\d{1,2}月\d{1,2}日)[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*[-]\s*(.+?):\s+(.*)'),

    # ── 群聊波浪号格式 ──
    # 2026-07-25 14:30 - ~Name~: Message  或  2026-07-25 14:30 - ‪+86...‪: Message
    re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*[~\u200e\u200f]*([^:]+?)[~\u200e\u200f]*:\s+(.*)'),
    # [2026-07-25 14:30:00] ~Name~: Message
    re.compile(r'\[(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*[~\u200e\u200f]*([^:]+?)[~\u200e\u200f]*:\s+(.*)'),

    # ── 无分隔符格式：日期时间后直接跟发送者 ──
    # 2026-07-25 14:30 Sender: Message (没有横线分隔)
    re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s+(.+?):\s+(.*)'),
    # [25.07.2026, 14:30] Sender: Message (点号日期带方括号)
    re.compile(r'\[(\d{1,2}[.]\d{1,2}[.]\d{4})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*(.+?):\s+(.*)'),

    # ── 中文冒号格式 (有些导出使用中文冒号) ──
    # 2026-07-25 14:30 - 名称：消息
    re.compile(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?)：\s+(.*)'),
    # [日期时间] 名称：消息
    re.compile(r'\[(\d{4}[-/]\d{1,2}[-/]\d{1,2})[,\s]+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*(.+?)：\s+(.*)'),

    # ── 更多格式：日期和时间为空格分隔，无横线 ──
    # 25/07/2026 14:30 - Name: Message (DD/MM/YYYY with space before dash)
    re.compile(r'(\d{1,2}[/]\d{1,2}[/]\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),
    # 07/25/2026 2:30 PM - Name: Message (MM/DD/YYYY 12hr)
    re.compile(r'(\d{1,2}[/]\d{1,2}[/]\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(AM|PM|am|pm)\s*[-]\s*(.+?):\s+(.*)'),

    # ── 纯时间格式（无日期，只有时间和发送者） ──
    # 14:30 - Name: Message
    re.compile(r'(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\s*[-]\s*(.+?):\s+(.*)'),

    # ── 带方括号的纯时间格式 ──
    # [14:30] Name: Message
    re.compile(r'\[(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:AM|PM|am|pm)?\]\s*(.+?):\s+(.*)'),
]

# 支持格式说明（用于错误提示）
_WA_SUPPORTED_FORMATS = (
    "支持的 WhatsApp 导出格式包括：\n"
    "  - Android: 2026-07-25 14:30 - 名称: 消息\n"
    "  - iOS: [25/07/2026, 14:30:00] 名称: 消息\n"
    "  - WhatsApp Web: [2026-07-25 14:30:00] 名称: 消息\n"
    "  - 12小时制: 7/25/26, 2:30 PM - 名称: 消息\n"
    "  - 带时区: 2026-07-25 14:30 UTC - 名称: 消息\n"
    "  - 群聊格式: 2026-07-25 14:30 - ~名称~: 消息\n"
    "  - 英文月份: Jul 25, 2026 2:30 PM - 名称: 消息\n"
    "  - 中文日期: 2026年7月25日 14:30 - 名称: 消息\n"
    "  - 也支持简单的 '名称: 消息' 格式（自动兜底解析）"
)

# 英文月份名映射
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# WhatsApp 导出中的系统消息关键词（这些行不是聊天消息，应该跳过）
_WA_SYSTEM_KEYWORDS = [
    "messages and calls are end-to-end encrypted",
    "security code changed",
    "created group",
    "changed the subject",
    "added you",
    "left",
    "was added",
    "changed their phone number",
    "this message was deleted",
    "message deleted",
    "omitted",
    "media omitted",
    "sticker omitted",
    "image omitted",
    "video omitted",
    "audio omitted",
    "document omitted",
    "contact card omitted",
    "gif omitted",
    "location shared",
    "voice message",
    "<media omitted>",
    "此对话",
    "端到端加密",
    "安全代码",
    "创建群组",
    "退出群组",
    "修改了群名称",
    "添加了你",
    "更换了号码",
    "此消息已删除",
    "消息已删除",
    "图片省略",
    "视频省略",
    "音频省略",
    "文件省略",
    "位置共享",
    "语音消息",
    "名片省略",
    "贴纸省略",
    "gif省略",
]


def _is_wa_system_message(line: str) -> bool:
    """判断一行是否为 WhatsApp 系统消息（非聊天内容）"""
    line_lower = line.lower().strip()
    if not line_lower:
        return True
    # 检查是否以系统关键词开头或包含
    for kw in _WA_SYSTEM_KEYWORDS:
        if kw in line_lower:
            return True
    # 检查是否是 WhatsApp 头部说明行
    if line_lower.startswith("whatsapp chat with"):
        return True
    return False


def _parse_date_string(date_str: str, time_str: str) -> datetime | None:
    """尝试用多种日期格式解析，返回 datetime 对象。
    支持中文日期、英文月份、YYYY-MM-DD、MM/DD/YYYY、DD/MM/YYYY、
    DD.MM.YYYY、两位年份(YY)、12/24小时制等。
    """
    date_str = date_str.strip()
    time_str = time_str.strip()

    # 处理 AM/PM 时间（可能作为独立分组传入，也可能嵌入在 time_str 中）
    is_pm = "pm" in time_str.lower()
    is_am = "am" in time_str.lower()
    time_clean = re.sub(r'\s*(AM|PM|am|pm)', '', time_str).strip()

    # 尝试各种日期格式
    formats = []

    # 中文日期: 2026年7月25日
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_str)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        formats.append((y, mo, d))

    # 英文月份: Jul 25, 2026
    m = re.match(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', date_str)
    if m:
        month_name = m.group(1).lower()
        if month_name in _MONTH_MAP:
            y, mo, d = int(m.group(3)), _MONTH_MAP[month_name], int(m.group(2))
            formats.append((y, mo, d))

    # YYYY-MM-DD 或 YYYY/MM/DD
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', date_str)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        formats.append((y, mo, d))

    # DD.MM.YYYY (德式)
    m = re.match(r'(\d{1,2})[.](\d{1,2})[.](\d{4})', date_str)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        formats.append((y, mo, d))

    # DD/MM/YYYY 或 MM/DD/YYYY (4位年份，模糊处理)
    m = re.match(r'(\d{1,2})[/](\d{1,2})[/](\d{4})', date_str)
    if m:
        p1, p2, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 如果第一个数>12，一定是日；如果第二个数>12，一定是月
        if p1 > 12:
            d, mo = p1, p2
        elif p2 > 12:
            mo, d = p1, p2
        else:
            # 都<=12，默认美式 MM/DD
            mo, d = p1, p2
        formats.append((y, mo, d))

    # DD/MM/YY 或 MM/DD/YY (两位年份，如 7/25/26)
    m = re.match(r'(\d{1,2})[/](\d{1,2})[/](\d{2})\b', date_str)
    if m:
        p1, p2, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 两位年份转四位: 0-50 → 20xx, 51-99 → 19xx
        y = 2000 + yy if yy <= 50 else 1900 + yy
        if p1 > 12:
            d, mo = p1, p2
        elif p2 > 12:
            mo, d = p1, p2
        else:
            mo, d = p1, p2
        formats.append((y, mo, d))

    if not formats:
        return None

    y, mo, d = formats[0]

    # 解析时间
    parts = time_clean.split(':')
    try:
        h = int(parts[0])
        mi = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None

    # 处理 12 小时制
    if is_pm and h < 12:
        h += 12
    if is_am and h == 12:
        h = 0

    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _parse_whatsapp_text(text: str):
    """解析 WhatsApp 导出的文本聊天记录，返回解析后的消息列表。
    支持多种 WhatsApp 导出格式（Android、iOS、WhatsApp Web、不同语言区域）。
    如果所有正则都无法匹配，会尝试兜底解析（按冒号分割发送者和消息）。
    自动跳过系统消息和附件标记行。
    """
    # 清理 WhatsApp 导出中常见的不可见字符（LRM/RLM/方向控制标记）
    text = text.replace('\u200e', '').replace('\u200f', '').replace('\u202a', '').replace('\u202c', '')
    # 统一零宽字符
    text = text.replace('\u200b', '').replace('\ufeff', '')
    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    messages = []
    lines = text.split("\n")

    # 存储多行消息的缓冲区
    current_msg = None

    def _build_msg(sender, message_text, timestamp=None):
        """构造消息字典，自动判断是否为自己发的"""
        sender = sender.strip().strip('~').strip('\u200e\u200f').strip()
        if not message_text or not sender:
            return None
        is_from_me = 1 if sender.lower() in ("me", "我", "you", "本人") else 0
        return {
            "timestamp": timestamp or datetime.now(),
            "sender": sender,
            "remote_id": "Me" if is_from_me else sender,
            "message_text": message_text.strip(),
            "is_from_me": is_from_me,
            "phone": sender if not is_from_me else "",
        }

    def _finish_current():
        """保存当前缓冲的消息"""
        nonlocal current_msg
        if current_msg:
            current_msg["message_text"] = current_msg["message_text"].strip()
            if current_msg["message_text"]:
                messages.append(current_msg)
            current_msg = None

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            if current_msg:
                current_msg["message_text"] += "\n"
            continue

        # 跳过 WhatsApp 导出文件的头部说明行和系统消息
        if _is_wa_system_message(line_stripped):
            # 系统消息不中断当前消息的续行
            continue

        # ── 第一轮：严格正则匹配（带日期时间） ──
        matched = False
        for pattern in _WA_MSG_PATTERNS:
            m = pattern.match(line_stripped)
            if m:
                _finish_current()

                num_groups = m.lastindex or 0
                if num_groups >= 5:
                    # 5分组: date, time, ampm, sender, message
                    date_str = m.group(1)
                    time_str = m.group(2) + " " + m.group(3)
                    sender = m.group(4).strip()
                    message_text = m.group(5).strip()
                elif num_groups == 4:
                    # 4分组: date, time, sender, message
                    date_str = m.group(1)
                    time_str = m.group(2)
                    sender = m.group(3).strip()
                    message_text = m.group(4).strip()
                elif num_groups == 3:
                    # 3分组: time, sender, message（纯时间格式，无日期）
                    date_str = ""
                    time_str = m.group(1)
                    sender = m.group(2).strip()
                    message_text = m.group(3).strip()
                else:
                    continue

                if not message_text:
                    current_msg = None
                    matched = True
                    break

                # 跳过附件标记行（如 "<media omitted>" 等）
                if _is_wa_system_message(message_text):
                    current_msg = None
                    matched = True
                    break

                if date_str:
                    timestamp = _parse_date_string(date_str, time_str)
                else:
                    # 纯时间格式，使用今天日期
                    timestamp = datetime.now()
                if not timestamp:
                    # 日期解析失败，使用当前时间兜底
                    timestamp = datetime.now()

                is_from_me = 1 if sender.lower() in ("me", "我", "you", "本人") else 0
                remote_id = "Me" if is_from_me else sender

                current_msg = {
                    "timestamp": timestamp,
                    "sender": sender,
                    "remote_id": remote_id,
                    "message_text": message_text,
                    "is_from_me": is_from_me,
                    "phone": sender if not is_from_me else "",
                }
                matched = True
                break

        if not matched:
            # ── 第二轮：兜底解析（无日期时间前缀的行） ──
            # 尝试按第一个冒号分割为 "发送者: 消息"
            # 也检查中文冒号
            colon_idx = line_stripped.find(':')
            cn_colon_idx = line_stripped.find('：')
            if cn_colon_idx > 0 and (colon_idx < 0 or cn_colon_idx < colon_idx):
                colon_idx = cn_colon_idx

            if colon_idx >= 1:
                possible_sender = line_stripped[:colon_idx].strip()
                possible_msg = line_stripped[colon_idx + 1:].strip()
                # 发送者不应太长，且不应包含换行或特殊分隔符
                if (possible_sender
                        and len(possible_sender) <= 80
                        and '\n' not in possible_sender
                        and '\t' not in possible_sender
                        and possible_msg):
                    # 检查发送者是否看起来像时间戳（纯数字和冒号/斜杠）
                    # 例如 "14:30:" 不应被当作发送者
                    sender_clean = possible_sender.replace(' ', '')
                    if not re.match(r'^[\d/:.APMapm,]+$', sender_clean):
                        # 跳过附件标记行
                        if not _is_wa_system_message(possible_msg):
                            msg_obj = _build_msg(possible_sender, possible_msg)
                            if msg_obj:
                                _finish_current()
                                current_msg = msg_obj
                                matched = True

            if not matched:
                # 非消息行：可能是上一条消息的续行，或系统消息
                if current_msg:
                    # 追加到当前消息
                    if current_msg["message_text"].endswith("\n"):
                        current_msg["message_text"] += line_stripped
                    else:
                        current_msg["message_text"] += "\n" + line_stripped
                # 如果没有 current_msg，跳过（可能是文件头、系统消息等）

    # 保存最后一条消息
    _finish_current()

    # ── 兜底：如果正则一条都没匹配到，尝试全文本宽松解析 ──
    if not messages:
        messages = _fallback_parse(text)

    return messages


def _fallback_parse(text: str):
    """兜底解析：当所有正则都无法匹配时，用更宽松的方式逐行解析。
    只要行中包含冒号且左侧看起来像发送者名称，就尝试分割。
    支持各种 WhatsApp 导出格式，包括没有日期前缀的简单格式。
    自动跳过系统消息和附件标记行。
    """
    messages = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 清理不可见字符
    lines = [l.replace('\u200e', '').replace('\u200f', '').replace('\u202a', '').replace('\u202c', '').replace('\u200b', '').replace('\ufeff', '') for l in lines]

    current_sender = None
    current_msg_lines = []  # 用于多行消息

    def _flush_current():
        nonlocal current_sender, current_msg_lines
        if current_sender and current_msg_lines:
            msg_text = "\n".join(current_msg_lines).strip()
            if msg_text:
                is_me = 1 if current_sender.lower() in ("me", "我", "you", "本人") else 0
                messages.append({
                    "timestamp": datetime.now(),
                    "sender": current_sender,
                    "remote_id": "Me" if is_me else current_sender,
                    "message_text": msg_text,
                    "is_from_me": is_me,
                    "phone": current_sender if not is_me else "",
                })
        current_sender = None
        current_msg_lines = []

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            if current_sender and current_msg_lines:
                # 空行不中断消息，但标记换行
                current_msg_lines.append("")
            continue

        # 跳过系统消息和头部
        if _is_wa_system_message(line_stripped):
            continue
        if line_stripped.startswith('<') and line_stripped.endswith('>'):
            continue  # 系统消息 <...>

        # 尝试找冒号分割（优先找第一个冒号，但也考虑中文冒号）
        colon_idx = line_stripped.find(':')
        cn_colon_idx = line_stripped.find('：')
        if cn_colon_idx > 0 and (colon_idx < 0 or cn_colon_idx < colon_idx):
            colon_idx = cn_colon_idx

        if colon_idx < 1:
            # 没有冒号，可能是上一条消息的续行
            if current_sender:
                current_msg_lines.append(line_stripped)
            continue

        possible_sender = line_stripped[:colon_idx].strip().strip('~').strip('\u200e\u200f')
        possible_msg = line_stripped[colon_idx + 1:].strip()

        if not possible_sender or not possible_msg:
            if current_sender:
                current_msg_lines.append(line_stripped)
            continue

        # 跳过附件标记行
        if _is_wa_system_message(possible_msg):
            continue

        # 过滤掉看起来像时间戳、日期、URL 的"发送者"
        if re.match(r'^[\d/:.APMapm\-+\s,]{2,40}$', possible_sender):
            if current_sender:
                current_msg_lines.append(line_stripped)
            continue
        if '://' in possible_sender or possible_sender.startswith('http'):
            continue
        if len(possible_sender) > 80:
            if current_sender:
                current_msg_lines.append(line_stripped)
            continue

        # 新的消息发送者
        _flush_current()
        current_sender = possible_sender
        current_msg_lines = [possible_msg]

    # 保存最后一条消息
    _flush_current()

    return messages


def _find_or_create_customer_by_phone(db: Session, phone: str, name: str, account_id=None) -> Customer:
    """按手机号或名称匹配客户，找不到则创建新客户。
    如果匹配到的已有客户 account_id 为 None，则自动补充当前账号 ID。
    """
    # 清理手机号（去掉 + 前缀和空格）
    clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
    # 1. 按手机号匹配客户 email 中包含该手机号
    customer = db.query(Customer).filter(Customer.email.contains(phone)).first()
    if not customer and clean_phone:
        customer = db.query(Customer).filter(Customer.email.contains(clean_phone)).first()
    # 2. 按客户 name 匹配
    if not customer and name:
        customer = db.query(Customer).filter(Customer.name == name).first()
    # 2.5 如果匹配到已有客户但 account_id 为空，补充当前账号 ID
    if customer and customer.account_id is None and account_id is not None:
        customer.account_id = account_id
        db.flush()
        logger.info(f"[WHATSAPP] Updated account_id for existing customer: {customer.name} (id={customer.id}) -> account_id={account_id}")
    # 3. 创建新客户
    if not customer:
        display_name = name if name and not name.startswith("+") else ("WhatsApp: " + phone)
        customer = Customer(
            email=phone + "@whatsapp.local",
            name=display_name,
            priority="cold",
            account_id=account_id,
        )
        db.add(customer)
        db.flush()
        logger.info(f"[WHATSAPP] Created new customer: {customer.name} (id={customer.id})")
    return customer


@app.post("/api/whatsapp/import")
async def api_whatsapp_import_text(req: WhatsAppImportRequest, db: Session = Depends(get_db),
                                     session: Optional[auth.Session] = Depends(get_current_session)):
    """通过文本内容导入 WhatsApp 聊天记录"""
    if not req.text or not req.text.strip():
        raise HTTPException(400, "聊天记录内容不能为空")
    
    messages = _parse_whatsapp_text(req.text)
    if not messages:
        raise HTTPException(400, "无法解析聊天记录，未找到有效的消息内容。请确保粘贴的是 WhatsApp 导出的聊天记录文本。系统支持多种格式，只要是 WhatsApp 导出的文本即可。")
    
    account_id = session.account_id if session else None
    
    # 确定关联客户
    customer = None
    if req.customer_id:
        customer = db.query(Customer).filter(Customer.id == req.customer_id).first()
    
    if not customer:
        # 从消息中提取对方身份（第一个非 "Me" 的发送者）
        remote_name = ""
        for msg in messages:
            if not msg["is_from_me"]:
                remote_name = msg["sender"]
                break
        if remote_name:
            customer = _find_or_create_customer_by_phone(db, remote_name, remote_name, account_id)
    
    if not customer:
        raise HTTPException(400, "无法确定关联客户，请指定 customer_id 或确保聊天记录中包含对方消息")
    
    # 写入消息
    imported = 0
    for msg in messages:
        existing = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.customer_id == customer.id,
            WhatsAppMessage.timestamp == msg["timestamp"],
            WhatsAppMessage.message_text == msg["message_text"]
        ).first()
        if existing:
            continue
        wa_msg = WhatsAppMessage(
            customer_id=customer.id,
            remote_id=msg["remote_id"],
            sender=msg["sender"],
            message_text=msg["message_text"],
            timestamp=msg["timestamp"],
            is_from_me=msg["is_from_me"],
        )
        db.add(wa_msg)
        imported += 1
    
    db.commit()
    
    return {
        "status": "ok",
        "customer_id": customer.id,
        "customer_name": customer.name or customer.email,
        "imported": imported,
        "total_messages": len(messages),
        "message": f"成功导入 {imported} 条 WhatsApp 消息"
    }


@app.post("/api/whatsapp/import/file")
async def api_whatsapp_import_file(file: UploadFile = File(...),
                                     customer_id: int = Query(0),
                                     db: Session = Depends(get_db),
                                     session: Optional[auth.Session] = Depends(get_current_session)):
    """通过上传的文本文件导入 WhatsApp 聊天记录，支持指定客户ID"""
    content = await file.read()
    # 尝试多种编码解码（UTF-8, UTF-8-BOM, GBK, Latin-1）
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        text = content.decode("utf-8", errors="ignore")

    if not text.strip():
        raise HTTPException(400, "文件内容为空")

    messages = _parse_whatsapp_text(text)
    if not messages:
        raise HTTPException(400, "无法解析文件中的聊天记录。请确保上传的是 WhatsApp 导出的文本文件。系统支持多种格式，只要是 WhatsApp 导出的文本即可。")

    account_id = session.account_id if session else None

    # 如果指定了 customer_id，使用该客户
    customer = None
    if customer_id:
        customer = db.query(Customer).filter(Customer.id == customer_id).first()

    if not customer:
        # 从消息中提取对方身份
        remote_name = ""
        for msg in messages:
            if not msg["is_from_me"]:
                remote_name = msg["sender"]
                break
        if remote_name:
            customer = _find_or_create_customer_by_phone(db, remote_name, remote_name, account_id)

    if not customer:
        raise HTTPException(400, "无法确定关联客户，请指定客户或确保聊天记录中包含对方消息")

    imported = 0
    for msg in messages:
        existing = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.customer_id == customer.id,
            WhatsAppMessage.timestamp == msg["timestamp"],
            WhatsAppMessage.message_text == msg["message_text"]
        ).first()
        if existing:
            continue
        wa_msg = WhatsAppMessage(
            customer_id=customer.id,
            remote_id=msg["remote_id"],
            sender=msg["sender"],
            message_text=msg["message_text"],
            timestamp=msg["timestamp"],
            is_from_me=msg["is_from_me"],
        )
        db.add(wa_msg)
        imported += 1

    db.commit()
    return {
        "status": "ok",
        "customer_id": customer.id,
        "customer_name": customer.name or customer.email,
        "imported": imported,
        "total_messages": len(messages),
        "message": f"成功导入 {imported} 条 WhatsApp 消息"
    }


@app.get("/api/whatsapp/conversations")
def api_whatsapp_conversations(db: Session = Depends(get_db),
                                session: Optional[auth.Session] = Depends(get_current_session)):
    """获取 WhatsApp 聊天记录列表（按账号隔离）。
    返回有 WhatsApp 消息的客户列表，包含消息数和最后一条消息时间。"""
    if not session or session.account_id is None:
        return []

    # 查询有 WhatsApp 消息的客户
    wa_customer_ids = db.query(WhatsAppMessage.customer_id).distinct().all()
    customer_ids = [cid[0] for cid in wa_customer_ids]

    result = []
    for cid in customer_ids:
        customer = db.query(Customer).filter(Customer.id == cid).first()
        if not customer:
            continue
        # 只返回当前账号的客户
        if customer.account_id != session.account_id:
            continue

        msg_count = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.customer_id == cid
        ).count()

        last_msg = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.customer_id == cid
        ).order_by(desc(WhatsAppMessage.timestamp)).first()

        # 获取最后一条消息预览
        last_preview = ""
        last_time = None
        if last_msg:
            last_preview = (last_msg.message_text or "")[:50]
            last_time = last_msg.timestamp.isoformat() if last_msg.timestamp else None

        # 获取WA分析结果（如果已有）
        wa_analysis = None
        if customer.profile_summary:
            try:
                profile = json.loads(customer.profile_summary)
                if isinstance(profile, dict) and "wa_analysis" in profile:
                    wa_analysis = profile["wa_analysis"]
            except (json.JSONDecodeError, TypeError):
                pass

        result.append({
            "customer_id": cid,
            "customer_name": customer.name or customer.email,
            "customer_email": customer.email,
            "priority": customer.priority or "cold",
            "message_count": msg_count,
            "last_message_preview": last_preview,
            "last_message_time": last_time,
            "has_analysis": wa_analysis is not None,
        })

    # 按最后消息时间倒序排列
    result.sort(key=lambda x: x["last_message_time"] or "", reverse=True)
    return result


@app.get("/api/whatsapp/messages/{customer_id}")
def api_whatsapp_messages(customer_id: int, db: Session = Depends(get_db)):
    """获取指定客户的 WhatsApp 消息列表"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(404, "未找到该客户")

    messages = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.customer_id == customer_id
    ).order_by(WhatsAppMessage.timestamp.asc()).all()

    result = []
    for m in messages:
        result.append({
            "id": m.id,
            "remote_id": m.remote_id,
            "sender": m.sender,
            "message_text": m.message_text,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            "is_from_me": bool(m.is_from_me),
        })
    return result


@app.post("/api/whatsapp/analyze")
def api_whatsapp_analyze(req: WhatsAppAnalyzeRequest, db: Session = Depends(get_db)):
    """使用 AI 分析客户最近的 WhatsApp 消息，并保存分析结果到客户画像"""
    customer = db.query(Customer).filter(Customer.id == req.customer_id).first()
    if not customer:
        raise HTTPException(404, "未找到该客户")

    messages = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.customer_id == req.customer_id
    ).order_by(WhatsAppMessage.timestamp.desc()).limit(20).all()

    if not messages:
        raise HTTPException(400, "该客户没有 WhatsApp 消息")

    # 构建消息文本
    recent_text = "\n".join([
        f"[{m.timestamp}] {'Me' if m.is_from_me else m.sender}: {m.message_text}"
        for m in reversed(messages)
    ])

    # 获取已有的客户画像（不含WA分析）
    existing_profile = customer.profile_summary or ""
    profile_for_ai = existing_profile
    # 如果已有WA分析结果，提取原始画像部分
    try:
        parsed = json.loads(existing_profile)
        if "wa_analysis" in parsed:
            profile_for_ai = json.dumps({k: v for k, v in parsed.items() if k != "wa_analysis"}, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

    result = analyze_whatsapp(profile_for_ai, recent_text)

    # 保存分析结果到客户画像
    # 合并：保留原有画像数据，添加/更新 wa_analysis 字段
    merged_profile = {}
    try:
        if existing_profile:
            merged_profile = json.loads(existing_profile)
            if not isinstance(merged_profile, dict):
                merged_profile = {}
    except (json.JSONDecodeError, TypeError):
        merged_profile = {}

    merged_profile["wa_analysis"] = result
    customer.profile_summary = json.dumps(merged_profile, ensure_ascii=False)

    # 同步更新客户优先级（取WA分析结果和现有优先级的最高值）
    if result.get("priority"):
        wa_priority = _sanitize_priority(result["priority"])
        priority_order = {"hot": 0, "warm": 1, "cold": 2}
        current_p = customer.priority or "cold"
        if priority_order.get(wa_priority, 2) < priority_order.get(current_p, 2):
            customer.priority = wa_priority

    customer.updated_at = datetime.utcnow()
    db.commit()

    return {"status": "ok", "analysis": result}


# ── 前端页面路由 ──
@app.get("/")
def serve_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"error": "前端页面未找到"}

@app.get("/login")
def serve_login():
    login_path = FRONTEND_DIR / "login.html"
    if login_path.exists():
        return FileResponse(str(login_path))
    return {"error": "登录页未找到"}

@app.get("/customer/{customer_id}")
def serve_customer_detail(customer_id: int):
    detail_path = FRONTEND_DIR / "customer_detail.html"
    if detail_path.exists():
        return FileResponse(str(detail_path))
    return {"error": "前端页面未找到"}

@app.get("/email/{email_id}")
def serve_email_detail(email_id: int):
    email_path = FRONTEND_DIR / "email_detail.html"
    if email_path.exists():
        return FileResponse(str(email_path))
    return {"error": "前端页面未找到"}


if __name__ == "__main__":
    import uvicorn
    # 仅绑定本地地址，确保安全性
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
