import os, json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = "https://api.deepseek.com/v1"
MODEL_FLASH = "deepseek-v4-flash"
MODEL_PRO = "deepseek-v4-pro"

# ── Token 用量控制 ──
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "3000"))       # 单次生成上限
AI_MAX_HISTORY = int(os.getenv("AI_MAX_HISTORY", "10"))       # 对话历史条数
AI_MAX_KNOWLEDGE = int(os.getenv("AI_MAX_KNOWLEDGE", "5"))    # 注入知识条数
AI_MAX_CTX_CHARS = int(os.getenv("AI_MAX_CTX_CHARS", "10000")) # 上下文最大字符数
AI_LIGHT_ANALYSIS = os.getenv("AI_LIGHT_ANALYSIS", "0") == "1"  # 轻量模式：拉取后不自动深度分析

# 检测占位符值（用户未修改 .env 中的模板内容）
_PLACEHOLDER_VALUES = {"", "your_deepseek_api_key_here", "your_api_key_here", "sk-your-key-here"}
USE_MOCK = (not API_KEY) or (API_KEY.lower() in _PLACEHOLDER_VALUES)

if not USE_MOCK:
    try:
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    except Exception as e:
        print(f"[AI] 初始化客户端失败: {e}, 回退到 Mock 模式")
        client = None
        USE_MOCK = True
else:
    client = None
    print("[AI] Mock 模式: 未配置 API Key，使用模拟响应")


def get_ai_status() -> dict:
    """检查 DeepSeek AI 是否可用，返回状态信息"""
    if USE_MOCK or client is None:
        return {
            "connected": False,
            "mock_mode": True,
            "message": "未配置 DEEPSEEK_API_KEY，AI 使用模拟模式",
            "model": None,
        }
    try:
        resp = client.chat.completions.create(
            model=MODEL_FLASH,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            stream=False,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        return {
            "connected": True,
            "mock_mode": False,
            "message": "DeepSeek API 连接正常",
            "model": MODEL_FLASH,
            "test_response": text[:20],
        }
    except Exception as e:
        return {
            "connected": False,
            "mock_mode": False,
            "message": f"DeepSeek API 连接失败: {e}",
            "model": MODEL_FLASH,
        }

# ── 客户画像构建提示词（中文输出） ──
CUSTOMER_PROFILE_SYSTEM_PROMPT = """你是一个专业的CRM销售助手。请根据客户的邮件往来历史，分析并生成一份结构化的客户画像。所有输出必须使用中文。

请严格输出以下JSON格式，不要输出任何其他内容：
{
  "customer_type": "客户类型（如：经销商、终端用户、代理商、批发商、政府机构等）",
  "company_size": "公司规模（如：大型企业、中型企业、小微企业、个人等）",
  "history_summary": "历史合作摘要（基于邮件往来总结客户的采购历史、合作情况、关注产品等）",
  "communication_style": "沟通风格（如：专业正式、热情友好、直接高效、价格敏感等）",
  "risks_and_opportunities": "风险与机会（分析当前合作中的潜在风险和业务机会）",
  "suggested_priority": "建议优先级（hot/warm/cold）"
}"""

# ── 邮件分析提示词（中文输出） ──
EMAIL_SYSTEM_PROMPT = """你是一个专业的CRM销售助手。根据客户画像和最近的邮件，分析新收到的邮件。摘要和意图分析使用中文，但回复草稿(reply_draft)的语言根据用户指定的语言要求撰写。

请严格按照以下优先级规则判断邮件的优先级：

【优先级规则 - 必须严格遵守】
- hot（高优先级）：需要回复处理的邮件。包括但不限于：询价、订单确认、价格谈判、产品咨询、投诉/问题反馈、售后请求、合作意向、会议邀请、合同讨论、物流异常、付款相关等任何需要人工回复的商业邮件。
- warm（中优先级）：状态信息类邮件。包括但不限于：订单状态更新、发货通知、物流跟踪、付款确认、系统维护通知、账户变更通知等。这些邮件需要关注但通常不需要主动回复。
- cold（低优先级）：无需回复的邮件。包括但不限于：验证码、推广/营销邮件、广告、系统自动通知（无回复地址noreply）、新闻订阅、活动邀请、收据确认、安全提醒（密码修改等）、垃圾邮件等。

请严格输出以下JSON格式，不要输出任何其他内容：
{
  "intent": "邮件意图（如：询价、订单确认、投诉、物流咨询、售后服务、通知、推广等）",
  "priority": "优先级（hot/warm/cold，严格按照上面的规则判断）",
  "summary": "邮件摘要（用中文简要概括邮件核心内容）",
  "next_action": "下一步建议（如：立即回复、发送报价、确认订单、关注即可、无需处理等）",
  "reply_draft": "回复草稿（用要求的语言撰写一封合适的回复邮件，专业且礼貌；如果无需回复则为空字符串）",
  "no_reply_needed": false
}

注意：
1. 如果是cold优先级的邮件，请将 no_reply_needed 设为 true
2. 如果发件人地址包含 noreply、no-reply、notification 等无回复关键词，通常为 cold 优先级
3. 如果邮件内容涉及具体的业务合作、报价、产品问题，必须设为 hot 优先级
4. 宁可把需要回复的邮件判为 hot，也不要漏判"""


CHAT_SYSTEM_PROMPT = (
    "你是一个专业的CRM销售助理，帮助业务员管理客户、分析邮件、撰写回复、跟进商机。"
    "回答要求：始终使用中文；专业、简洁、可操作；"
    "当客户信息不足时，基于常识给出合理建议并说明假设。"
)


def chat_with_ai(user_message, customer_context="", history=None):
    """与 AI 对话，返回纯文本回答（非 JSON）"""
    if USE_MOCK or client is None:
        return _mock_chat(user_message)
    system = CHAT_SYSTEM_PROMPT
    if customer_context:
        system += "\n\n当前正在查看的客户信息：\n" + customer_context[:4000]
    messages = [{"role": "system", "content": system}]
    if history:
        for h in history[-8:]:
            role = h.get("role")
            content = str(h.get("content", ""))[:2000]
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": str(user_message)[:4000]})
    try:
        resp = client.chat.completions.create(
            model=MODEL_FLASH, messages=messages, stream=False,
            temperature=0.5, max_tokens=1200,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[AI Chat Error] {e}")
        return "AI 服务暂时不可用，请稍后重试。"


def _mock_chat(user_message):
    return (
        "（当前为模拟模式，未配置 DeepSeek API Key）\n\n"
        f"收到您的问题：「{str(user_message)[:120]}」\n"
        "配置 API Key 后即可获得真实的 AI 对话回复。"
    )


AGENT_SYSTEM_PROMPT = """你是「AI 助手」，一个内置在 CRM 系统里的智能助理与 Agent。
你不是一个普通的聊天机器人——你是一个有记忆、能理解用户需求、能直接操作 CRM 系统的智能体。
你就像一个经验丰富的销售助理，有脑子能理解用户意图，有手能直接在系统里执行操作。

## 你的核心原则
1. **果断执行**：用户说"把XX改为高优先级"，你直接执行 update_priority，不反复确认。只有在信息严重缺失（如找不到客户）时才追问。
2. **理解上下文**：你会看到之前的对话历史。用户可能说"他"、"那个客户"、"刚才提到的"——你要从历史中推断指代。
3. **遵循知识**：用户喂养的知识是你的业务指南，必须遵循。如果用户教过你产品信息、报价规则，回答时要引用。
4. **主动提醒**：看到逾期跟进、未读重要邮件、客户长期未联系等情况，主动在回复中提醒用户。
5. **多步执行**：用户说"帮我整理一下所有高优先客户并生成跟进计划"，你可以一次输出多个 action。
6. **对话语言**：你与用户的对话始终使用中文，优先级用"高/中/低"，等级用"A/B/C"。但**邮件回复草稿**的语言由用户指定或根据原邮件语言判断（见邮件回复规则）。
7. **邮件场景**：当上下文中有"当前邮件"时，用户的问题默认是关于这封邮件的。你必须先理解邮件内容（上下文中已提供邮件主题、发件人、正文、AI分析结果），然后根据用户需求回复或执行操作。如果邮件还没有AI分析结果，你应该主动分析邮件内容并给出意图、优先级建议、摘要和回复草稿。
8. **专业回复**：帮用户写邮件回复时，语气专业、礼貌、简洁。英文回复用标准商务英语。如果用户指定语言，按指定语言写。
9. **回答要具体**：不要说"您可以这样做"，而是直接帮用户做。不要说"建议您查看"，而是直接查出来给用户看。
10. **格式清晰**：回复中用**粗体**标记重点，用•做列表，用代码块包裹邮件草稿。让用户一眼看到关键信息。

## 动作类型（actions）

### 客户操作
- **update_priority**：修改客户优先级。参数: customer(客户名/邮箱), value: hot/warm/cold
- **update_grade**：修改客户等级。参数: customer, value: A/B/C
- **update_profile**：修改客户画像。参数: customer, field(history_summary/communication_style/risks_and_opportunities/customer_type/company_size), value
- **search_customers**：搜索客户。参数: keyword。返回匹配的客户列表
- **get_customer_detail**：查看客户详情。参数: customer。返回该客户完整信息
- **analyze_customer**：分析客户。参数: customer。返回客户画像分析

### 邮件操作
- **update_email_priority**：修改邮件优先级。参数: email_subject 或 sender(用于定位邮件), value: hot/warm/cold。如果有"当前邮件"，不需要传参数，直接修改当前邮件。
- **mark_email_read**：标记邮件已读。参数: email_subject 或 sender。如果有"当前邮件"，不需要传参数。
- **mark_email_replied**：标记邮件已回复。参数: email_subject 或 sender。如果有"当前邮件"，不需要传参数。
- **reply_email_draft**：生成邮件回复草稿。参数: customer, subject, points(回复要点)。在 reply 中给出草稿
- **email_reply**：针对当前邮件直接生成回复。参数: points(回复要点，可选), language(en/zh，可选)。如果用户说"英文回复"或"English"则language=en，如果用户说"中文回复"则language=zh，未指定时根据原邮件语言判断。在 reply 中给出草稿。必须有"当前邮件"上下文才能用此动作。
- **summarize_emails**：总结邮件。参数: sender(可选)。在 reply 中给出总结

### 跟进计划
- **generate_followups**：生成跟进计划。参数: customer(可选，不传则为全部客户生成)
- **complete_followup**：完成跟进。参数: customer。将该客户待跟进标记为已完成
- **update_followup_rules**：修改跟进规则。参数: rules（规则数组，每项包含 name, grade(可选), priority(可选), interval_days）。替换全部规则
- **get_followup_rules**：查看当前跟进规则。不需要参数，在 reply 中展示规则列表

### 记忆管理
- **save_memory**：记住用户的重要指令或偏好。参数: key(简短标签), content(完整指令内容)。例如用户说"记住：A级客户每7天跟进一次" → key="跟进规则-A级客户", content="A级客户每7天跟进一次"
- **delete_memory**：忘记某条记忆。参数: key(关键词)

### 其他
- **chat**：纯对话/建议/分析（不修改数据时用这个）

## 何时用 action vs chat — 完整示例
- "把XX改为高优先级" → update_priority（直接执行）
- "把XX设为A级" → update_grade（直接执行）
- "修改XX的画像/摘要" → update_profile（直接执行）
- "把这封邮件改为高优先级" → update_email_priority（直接执行，当前邮件无需传参数）
- "标记这封邮件已回复" → mark_email_replied（直接执行）
- "标记XX的邮件已读" → mark_email_read（直接执行）
- "生成跟进计划" → generate_followups（直接执行）
- "XX的跟进做完了" → complete_followup（直接执行）
- "帮我搜一下XX客户" → search_customers（直接执行）
- "帮我看一下XX的详情" → get_customer_detail（直接执行）
- "查看跟进规则" → get_followup_rules（直接执行）
- "把A级客户跟进间隔改为10天" → update_followup_rules（直接执行）
- "记住..." / "帮我记住..." → save_memory（直接执行，提取 key 和 content）
- "忘记..." → delete_memory（直接执行）
- "帮我写封回复给XX" → reply_email_draft（在 reply 中给出草稿）
- "回复这封邮件" / "帮我回复" → email_reply（在 reply 中给出草稿，必须有当前邮件）
- "总结一下最近的邮件" → summarize_emails（在 reply 中给出总结）
- "分析一下这个客户" → analyze_customer（在 reply 中给出分析）
- "翻译这封邮件" → chat（在 reply 中给出翻译，当前邮件正文在上下文中）
- "这封邮件讲了什么" → chat（在 reply 中给出摘要，基于当前邮件上下文）
- "润色回复" / "帮我改一下回复" → email_reply（在 reply 中给出润色后的草稿）
- 用户问问题/要建议 → chat

## 上下文理解规则
- 用户说"他/她/这个客户"：从对话历史最后提到的客户推断
- 用户说"那封邮件/这封邮件"：从上下文中的"当前邮件"推断
- 用户说的客户名可能不完整：用模糊匹配，如"张"可能指"张三"
- 优先级映射：高=hot，中=warm，低=cold
- 等级映射：A级=A，B级=B，C级=C
- 当上下文包含"当前邮件"信息时，用户说"回复"默认指回复这封邮件
- 用户说"翻译"时，默认翻译当前邮件正文
- 当用户说"分析"且上下文有"当前邮件"时，分析当前邮件的意图、优先级、摘要，并给出回复建议
- 当用户说"分析"且上下文有"当前客户"时，分析客户的画像和跟进建议

## 邮件回复规则
### 语言判断（最重要）
- **用户明确指定语言时**：用户说"英文回复""English reply""翻译成英文"→ 邮件草稿必须用英文；用户说"中文回复""翻译成中文"→ 邮件草稿必须用中文
- **用户未指定时**：根据原邮件语言判断——原邮件是英文→草稿用英文，原邮件是中文→草稿用中文，原邮件是其他语言→草稿用英文
- **你与用户的对话**始终用中文，但**邮件草稿**必须遵循上述语言规则
- 邮件草稿用代码块包裹，方便用户复制

### 格式要求
- 英文回复使用标准商务英语，语气专业礼貌
- 中文回复使用正式书面语
- 回复开头：Dear [发件人名], / 尊敬的[发件人名]：
- 回复结尾：Best regards, / 此致敬礼
- 如果用户给了回复要点，按要点组织内容
- 如果用户没给要点，根据邮件内容自动判断回复要点
- 如果是询价邮件，回复中要体现专业性和合作意愿
- 如果是投诉邮件，回复中要先表示歉意再给出解决方案
- 如果是通知类邮件，回复中确认收到并表示理解

## 邮件分析规则（当上下文有当前邮件时）
- 如果当前邮件已有AI分析结果（意图、优先级、摘要），直接引用并基于此回答用户问题
- 如果当前邮件没有AI分析结果，你需要主动分析：
  1. 判断邮件意图（询价/订单/投诉/通知/推广等）
  2. 判断优先级（hot=需回复/warm=需关注/cold=无需回复）
  3. 生成摘要（用中文简要概括）
  4. 给出下一步建议
  5. 如果需要回复，生成回复草稿
- 将分析结果融入你的回复中，让用户一目了然

## 输出格式（严格 JSON，不要 markdown 包裹）
{
  "reply": "中文回复（做了什么+结果+建议。用**粗体**标记重点，用•做列表。如果有草稿用代码块）",
  "actions": [{"type": "动作类型", "customer": "客户标识", "value": "值", ...}]
}

一次最多 3 个 action。英文草稿用代码块包裹。""" + "\n\n"


def _truncate(s, n):
    s = str(s or "")
    return s[:n]


def agent_plan(user_message, context="", history=None):
    """让 AI 判断用户意图并输出动作计划（JSON）。支持多轮对话历史。"""
    if USE_MOCK or client is None:
        return {"reply": "（模拟模式）我已理解您的需求，但当前为模拟模式无法真正执行操作。", "actions": []}
    system = AGENT_SYSTEM_PROMPT + _truncate(context, AI_MAX_CTX_CHARS)
    messages = [
        {"role": "system", "content": system},
    ]
    # 注入对话历史 — 这是 AI 的"记忆"
    # 去重：如果历史最后一条是当前消息的重复（前端推送导致），跳过它
    if history:
        clean_history = list(history[-AI_MAX_HISTORY:])
        # 如果最后一条 user 消息和当前消息相同，移除它避免重复
        if clean_history and clean_history[-1].get("role") == "user" and clean_history[-1].get("content", "").strip() == user_message.strip():
            clean_history = clean_history[:-1]
        for h in clean_history:
            role = h.get("role")
            content = str(h.get("content", ""))[:2000]
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _truncate(user_message, 4000)})
    text = ""
    try:
        resp = client.chat.completions.create(
            model=MODEL_FLASH, messages=messages, stream=False,
            temperature=0.3, max_tokens=AI_MAX_TOKENS,
        )
        text = (resp.choices[0].message.content or "").strip()
        # 清理可能的 markdown 代码块包裹
        if text.startswith("`" + "`" + "`"):
            text = text.split("\n", 1)[-1]
        if text.endswith("`" + "`" + "`"):
            text = text.rsplit("\n", 1)[0]
        text = text.replace("`" + "`" + "`json", "").replace("`" + "`" + "`", "").strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取 JSON 部分
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
            else:
                raise
        if not isinstance(result, dict):
            raise ValueError("not dict")
        result.setdefault("reply", "")
        result.setdefault("actions", [])
        return result
    except Exception as e:
        print(f"[AI Agent Error] {e}")
        # 如果是 JSON 解析失败，尝试用更简单的提示重试一次
        if "JSONDecodeError" in str(type(e).__name__) or "json" in str(e).lower():
            try:
                retry_msg = messages + [{"role": "assistant", "content": text}, {"role": "user", "content": "请重新输出，确保是严格的 JSON 格式，不要有任何多余的文字。"}]
                resp2 = client.chat.completions.create(
                    model=MODEL_FLASH, messages=retry_msg, stream=False,
                    temperature=0.1, max_tokens=AI_MAX_TOKENS,
                )
                text2 = (resp2.choices[0].message.content or "").strip()
                if text2.startswith("`" + "`" + "`"):
                    text2 = text2.split("\n", 1)[-1]
                if text2.endswith("`" + "`" + "`"):
                    text2 = text2.rsplit("\n", 1)[0]
                text2 = text2.replace("`" + "`" + "`json", "").replace("`" + "`" + "`", "").strip()
                result2 = json.loads(text2)
                if isinstance(result2, dict):
                    result2.setdefault("reply", "")
                    result2.setdefault("actions", [])
                    return result2
            except Exception:
                pass
        return {"reply": "我暂时无法理解这个指令，请换个说法，或直接告诉我要做什么（例如：把某客户的优先级改为高）。", "actions": []}


def generate_reminders(reminder_data):
    """根据 CRM 数据生成主动提醒（纯文本，不走 JSON action 流程）"""
    if USE_MOCK or client is None:
        return _mock_reminders(reminder_data)
    system = """你是 CRM 系统的主动提醒助手。根据系统数据，生成简洁的中文提醒。
格式要求：每条提醒一行，用•开头。最多5条。只提醒重要/紧急的事项。
逾期跟进、未读重要邮件、长期未联系客户是最高优先级。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": reminder_data[:6000]},
    ]
    try:
        resp = client.chat.completions.create(
            model=MODEL_FLASH, messages=messages, stream=False,
            temperature=0.3, max_tokens=800,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[AI Reminders Error] {e}")
        return ""


def _mock_reminders(data):
    lines = []
    if "逾期" in data:
        lines.append("• 有逾期跟进计划，请尽快处理")
    if "未读" in data:
        lines.append("• 有未读重要邮件，请查看")
    if not lines:
        lines.append("• 暂无紧急提醒")
    return "\n".join(lines)


def build_customer_profile(customer_info, email_history):
    if USE_MOCK:
        return _mock_customer_profile()
    messages = [
        {"role": "system", "content": CUSTOMER_PROFILE_SYSTEM_PROMPT},
        {"role": "user", "content": f"客户信息: {customer_info}\n邮件记录: {email_history[:8000]}"},
    ]
    return _call_ai(messages)


def analyze_email(customer_profile, recent_emails, new_email, language="en"):
    """分析单封邮件。language: 'en' 回复草稿用英文（默认），'zh' 用中文"""
    if USE_MOCK:
        return _mock_email_analysis(new_email)
    lang_text = "英文 (English)" if language != "zh" else "中文"
    content = (
        f"客户画像: {customer_profile}\n"
        f"最近邮件: {recent_emails[:5000]}\n"
        f"待分析邮件 - 主题: {new_email.get('subject','')} - 正文: {new_email.get('body','')[:3000]}\n"
        f"\n【语言要求】本次邮件的回复草稿 reply_draft 请使用：{lang_text}"
    )
    messages = [
        {"role": "system", "content": EMAIL_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    return _call_ai(messages)


def _call_ai(messages):
    try:
        response = client.chat.completions.create(
            model=MODEL_FLASH, messages=messages,
            stream=False, temperature=0.3,
        )
        text = response.choices[0].message.content.strip()
        # 清理可能的 markdown 代码块包裹
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("\n", 1)[0]
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        # 确保 no_reply_needed 字段存在
        if "no_reply_needed" not in result:
            result["no_reply_needed"] = False
        return result
    except json.JSONDecodeError as e:
        print(f"[AI Error] JSON解析失败: {e}, 原始文本: {text[:200]}")
        return {"intent": "分析失败", "priority": "cold",
                "summary": "AI分析结果解析失败，请重试",
                "next_action": "manual_review", "reply_draft": "",
                "no_reply_needed": False}
    except Exception as e:
        print(f"[AI Error] {e}")
        return {"intent": "未知", "priority": "cold",
                "summary": "AI分析失败，请稍后重试",
                "next_action": "manual_review", "reply_draft": "",
                "no_reply_needed": False}


def _mock_customer_profile():
    return {
        "customer_type": "经销商/采购商",
        "company_size": "中型企业",
        "history_summary": "该客户对执法记录仪和充电底座产品表现出持续兴趣，曾进行多次询价和订单确认，合作意向明确。",
        "communication_style": "专业、礼貌、直接，对价格和交货期较为关注",
        "risks_and_opportunities": "机会：有持续采购潜力，可发展为长期合作客户。风险：BC-500订单延期可能影响客户信任度，需及时沟通。",
        "suggested_priority": "warm",
    }


def _mock_email_analysis(email):
    subj = (email.get("subject") or "").lower()
    body = (email.get("body") or "").lower()
    sender = (email.get("sender") or "").lower()

    # ── 无需回复类（cold）──
    cold_keywords = ["noreply", "no-reply", "do not reply", "automated",
                     "unsubscribe", "newsletter", "promo", "promotion", "优惠", "退订",
                     "verification code", "验证码", "双重验证", "2fa",
                     "security alert", "安全提醒", "password changed", "密码修改",
                     "广告", "垃圾", "spam", "rewards", "奖励"]
    is_cold = any(w in subj or w in sender for w in cold_keywords)
    # 检查正文是否类似纯验证码
    if not is_cold and body:
        import re
        code_match = re.search(r'\b\d{4,8}\b', body)
        if code_match and len(body.strip()) < 200:
            is_cold = True

    if is_cold:
        return {
            "intent": "系统通知/推广",
            "priority": "cold",
            "summary": "验证码、推广或系统自动通知邮件，无需回复",
            "next_action": "无需处理",
            "reply_draft": "",
            "no_reply_needed": True,
        }

    # ── 需要回复类（hot）──
    hot_keywords = ["urgent", "complaint", "problem", "issue", "broken", "defect", "not working",
                    "紧急", "投诉", "问题", "故障", "坏了",
                    "quote", "quotation", "order", "price", "purchase", "buy", "inquiry", "rfq",
                    "报价", "询价", "订单", "价格", "采购", "合作意向",
                    "meeting", "会议", "contract", "合同", "payment", "付款"]
    is_hot = any(w in subj for w in hot_keywords)

    # 正文包含业务相关内容也判为hot
    if not is_hot and body:
        biz_keywords = ["price", "cost", "quotation", "sample", "catalog", "specification",
                       "价格", "报价", "样品", "产品", "规格", "数量", "交期", "发货",
                       "interested in", "want to buy", "please send", "could you"]
        if any(w in body for w in biz_keywords):
            is_hot = True

    if is_hot:
        return {
            "intent": "业务邮件（需回复）",
            "priority": "hot",
            "summary": "客户发送的业务相关邮件，需要及时回复处理",
            "next_action": "立即回复处理",
            "reply_draft": "尊敬的客户：\n\n感谢您的来信，我们已收到您的邮件，会尽快为您处理并回复。\n\n此致敬礼",
            "no_reply_needed": False,
        }

    # ── 状态信息类（warm）──
    warm_keywords = ["shipping", "shipped", "delivered", "dispatched", "tracking",
                     "发货", "已发货", "物流", "运单", "签收",
                     "order status", "订单状态", "payment confirmed", "付款确认",
                     "receipt", "收据", "invoice", "发票"]
    is_warm = any(w in subj or w in body for w in warm_keywords)

    if is_warm:
        return {
            "intent": "状态通知",
            "priority": "warm",
            "summary": "订单/物流/付款状态更新通知，建议关注",
            "next_action": "关注即可",
            "reply_draft": "",
            "no_reply_needed": True,
        }

    # ── 默认：无法判断时，如果来自已知客户且非noreply，偏向hot ──
    if "noreply" not in sender and "no-reply" not in sender and "notification" not in sender:
        return {
            "intent": "一般沟通",
            "priority": "hot",
            "summary": "客户发来的邮件，建议查看并回复",
            "next_action": "立即回复处理",
            "reply_draft": "尊敬的客户：\n\n感谢您的邮件，我们会尽快给您回复。\n\n此致敬礼",
            "no_reply_needed": False,
        }
    else:
        return {
            "intent": "系统通知",
            "priority": "cold",
            "summary": "系统自动通知邮件，无需回复",
            "next_action": "无需处理",
            "reply_draft": "",
            "no_reply_needed": True,
        }

WHATSAPP_SYSTEM_PROMPT = """你是一个专业的CRM销售助手。分析客户最近的WhatsApp聊天记录和客户画像摘要。所有输出必须使用中文。

请严格输出以下JSON格式，不要输出任何其他内容：
{
  "sentiment": "情感倾向，必须输出中文值：积极 / 中性 / 消极",
  "intent": "对话意图（如：询价、投诉、产品咨询、物流咨询、一般沟通等）",
  "priority": "优先级（hot/warm/cold）",
  "summary": "对话摘要（用中文简要概括对话核心内容）",
  "key_topics": ["话题1", "话题2"],
  "suggested_reply": "建议回复（使用与对话相同的语言）",
  "next_action": "下一步建议"
}"""


def analyze_whatsapp(customer_profile, recent_messages):
    """分析客户最近的 WhatsApp 消息"""
    if USE_MOCK:
        return _mock_whatsapp_analysis(recent_messages)
    content = (
        f"客户画像: {customer_profile}\n"
        f"最近的WhatsApp聊天记录:\n{recent_messages[:8000]}"
    )
    messages = [
        {"role": "system", "content": WHATSAPP_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    return _call_ai(messages)


def _mock_whatsapp_analysis(recent_messages):
    msg_lower = (recent_messages or "").lower()
    if any(w in msg_lower for w in ["price", "quote", "cost", "how much", "价格", "报价", "多少钱"]):
        return {
            "sentiment": "积极",
            "intent": "询价",
            "priority": "hot",
            "summary": "客户在询价，对产品价格有明确兴趣，建议尽快回复报价",
            "key_topics": ["价格", "报价", "产品规格"],
            "suggested_reply": "您好！感谢您的咨询。我来为您准备一份详细的报价，请问您需要的数量是多少？",
            "next_action": "发送报价单",
        }
    elif any(w in msg_lower for w in ["delivery", "shipping", "when", "track", "发货", "物流", "什么时候"]):
        return {
            "sentiment": "中性",
            "intent": "物流咨询",
            "priority": "warm",
            "summary": "客户询问物流和发货时间，建议确认库存后回复",
            "key_topics": ["物流", "发货时间"],
            "suggested_reply": "好的，我来核实一下库存和发货安排，稍后给您回复。",
            "next_action": "核实库存",
        }
    elif any(w in msg_lower for w in ["problem", "issue", "broken", "defect", "not working", "问题", "坏了", "故障"]):
        return {
            "sentiment": "消极",
            "intent": "投诉",
            "priority": "hot",
            "summary": "客户反馈产品问题，需要立即跟进处理",
            "key_topics": ["售后", "产品问题"],
            "suggested_reply": "非常抱歉给您带来不便！请您发送一下问题照片或视频，我们会尽快为您处理。",
            "next_action": "立即跟进",
        }
    else:
        return {
            "sentiment": "中性",
            "intent": "一般咨询",
            "priority": "cold",
            "summary": "常规沟通，客户在了解产品信息，持续跟进",
            "key_topics": ["产品咨询"],
            "suggested_reply": "感谢您的咨询！请问有什么具体想了解的产品信息吗？",
            "next_action": "持续跟进",
        }


if __name__ == "__main__":
    print("[AI] 模块已加载。Mock 模式:", USE_MOCK)
