# -*- coding: utf-8 -*-
"""AI 助手 Agent 支持：知识库喂养、跟进规则、跟进计划生成"""
import json
from datetime import datetime, timedelta

from models import AIKnowledge, FollowUpRule, FollowUp, Customer, Email, EmailAnalysis, SessionLocal


DEFAULT_RULES = [
    {"name": "A级客户（重点）", "grade": "A", "priority": "", "interval_days": 7},
    {"name": "B级客户（潜力）", "grade": "B", "priority": "", "interval_days": 14},
    {"name": "C级客户（普通）", "grade": "C", "priority": "", "interval_days": 30},
    {"name": "高优先级", "grade": "", "priority": "hot", "interval_days": 7},
    {"name": "中优先级", "grade": "", "priority": "warm", "interval_days": 14},
    {"name": "低优先级", "grade": "", "priority": "cold", "interval_days": 30},
]


def ensure_default_rules(db):
    if db.query(FollowUpRule).count() == 0:
        for r in DEFAULT_RULES:
            db.add(FollowUpRule(name=r["name"], grade=r["grade"], priority=r["priority"], interval_days=r["interval_days"]))
        db.commit()


def get_rules(db):
    ensure_default_rules(db)
    return db.query(FollowUpRule).order_by(FollowUpRule.grade, FollowUpRule.priority).all()


def rules_to_dict(rules):
    return [{"id": r.id, "name": r.name, "grade": r.grade, "priority": r.priority, "interval_days": r.interval_days, "active": r.active} for r in rules]


def set_rules(db, rules_payload):
    """用新规则列表整体替换（前端/AI 都可调用）"""
    db.query(FollowUpRule).delete()
    for r in rules_payload:
        db.add(FollowUpRule(name=r.get("name", "规则"), grade=r.get("grade", ""), priority=r.get("priority", ""), interval_days=int(r.get("interval_days", 7)), active=1))
    db.commit()
    return rules_to_dict(get_rules(db))


def interval_for(customer, rules):
    """按客户等级/优先级返回跟进间隔天数"""
    grade = (customer.grade or "").strip().upper()
    priority = customer.priority or ""
    for r in rules:
        if not r.active:
            continue
        if r.grade and r.grade.upper() == grade:
            return r.interval_days
    for r in rules:
        if not r.active:
            continue
        if r.priority and r.priority == priority:
            return r.interval_days
    return 7


def _customer_summary(customer, db):
    """生成跟进摘要：取画像 history_summary 或最新邮件摘要"""
    if customer.profile_summary:
        try:
            p = json.loads(customer.profile_summary)
            if isinstance(p, dict) and p.get("history_summary"):
                return p["history_summary"][:120]
        except (json.JSONDecodeError, TypeError):
            pass
    latest = db.query(Email).filter(Email.customer_id == customer.id).order_by(Email.received_at.desc()).first()
    if latest:
        an = db.query(EmailAnalysis).filter(EmailAnalysis.email_id == latest.id).first()
        if an and an.summary:
            return an.summary[:120]
    return "日常跟进维护客户关系"


def generate_followups(db, account_id, customer_id=None):
    """为（指定/全部）客户按规则生成跟进计划，返回生成/更新的条目"""
    ensure_default_rules(db)
    rules = get_rules(db)
    q = db.query(Customer)
    if account_id is not None:
        q = q.filter(Customer.account_id == account_id)
    if customer_id is not None:
        q = q.filter(Customer.id == customer_id)
    customers = q.all()
    today = datetime.utcnow().date()
    result = []
    for c in customers:
        days = interval_for(c, rules)
        next_date = datetime.combine(today + timedelta(days=days), datetime.min.time())
        # 已存在的 pending 计划则更新，否则新建
        fu = db.query(FollowUp).filter(FollowUp.customer_id == c.id, FollowUp.status == "pending").first()
        if not fu:
            fu = FollowUp(customer_id=c.id, account_id=c.account_id or account_id)
            db.add(fu)
        fu.summary = _customer_summary(c, db)
        fu.action = "按规则跟进：等级 " + (c.grade or "未分级") + "，间隔 " + str(days) + " 天"
        fu.next_date = next_date
        fu.status = "pending"
        fu.updated_at = datetime.utcnow()
        result.append({"customer_id": c.id, "customer_name": c.name or c.email, "grade": c.grade or "", "priority": c.priority or "", "interval_days": days, "next_date": next_date.strftime("%Y-%m-%d")})
    db.commit()
    return result


def list_followups(db, account_id=None, customer_id=None, status=None):
    ensure_default_rules(db)
    q = db.query(FollowUp, Customer).join(Customer, FollowUp.customer_id == Customer.id)
    if account_id is not None:
        q = q.filter(FollowUp.account_id == account_id)
    if customer_id is not None:
        q = q.filter(FollowUp.customer_id == customer_id)
    if status is not None and status != "":
        q = q.filter(FollowUp.status == status)
    rows = q.order_by(FollowUp.next_date.asc()).all()
    return [{"id": f.id, "customer_id": f.customer_id, "customer_name": c.name or c.email, "grade": c.grade or "", "priority": c.priority or "", "summary": f.summary or "", "action": f.action or "", "next_date": f.next_date.strftime("%Y-%m-%d") if f.next_date else "", "status": f.status or "pending"} for f, c in rows]


def add_knowledge(db, content, source="manual"):
    if not content or not content.strip():
        return False
    db.add(AIKnowledge(content=content.strip(), source=source))
    db.commit()
    return True


def get_knowledge(db, limit=None):
    from ai_deepseek import AI_MAX_KNOWLEDGE
    n = limit or AI_MAX_KNOWLEDGE
    rows = db.query(AIKnowledge).order_by(AIKnowledge.id.desc()).limit(n).all()
    return [{"id": r.id, "content": r.content, "source": r.source, "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""} for r in rows]


def knowledge_text(db, limit=None):
    items = get_knowledge(db, limit)
    if not items:
        return ""
    return "\n".join(["- " + i["content"][:800] for i in items])