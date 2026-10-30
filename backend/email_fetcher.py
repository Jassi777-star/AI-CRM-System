import os, email, logging, imaplib
from datetime import datetime, timedelta, timezone
from imap_tools import MailBox
from dotenv import load_dotenv
from models import SessionLocal, Customer, Email, EmailAnalysis, DeletedEmail
from auth import Session as EmailSession

load_dotenv()
logger = logging.getLogger(__name__)

# ── 注册 IMAP ID 命令（RFC 2971），网易163/126/yeah邮箱要求 ──
# 网易邮箱会拒绝没有发送客户端身份信息的连接（报 Unsafe Login）
if 'ID' not in imaplib.Commands:
    imaplib.Commands['ID'] = ('AUTH',)

CLIENT_ID_ARGS = '("name" "AI CRM" "version" "3.0.0" "vendor" "AI-CRM" "contact" "support@ai-crm.com")'

# 需要发送 ID 命令的邮箱服务商
NETEASE_HOSTS = {'imap.163.com', 'imap.126.com', 'imap.yeah.net'}


def _send_imap_id_if_needed(host: str, mailbox):
    """对网易邮箱发送 IMAP ID 命令，解决 Unsafe Login 问题。
    注意：必须在 login(initial_folder=None) 之后、folder.set() 之前调用，
    因为 imap_tools 的 login() 如果指定 initial_folder 会内部执行 SELECT，
    而网易要求先发 ID 再 SELECT。
    """
    if host in NETEASE_HOSTS:
        try:
            mailbox.client._simple_command('ID', CLIENT_ID_ARGS)
            logger.info(f"[IMAP] 已向 {host} 发送客户端 ID 信息")
        except Exception as e:
            logger.warning(f"[IMAP] 发送 ID 命令失败（不影响登录）: {e}")


def _login_mailbox(host: str, port: int, user: str, pwd: str):
    """安全登录 IMAP 邮箱，对网易邮箱自动处理 ID 命令。
    返回已登录且选中 INBOX 的 MailBox 上下文管理器。
    """
    mb = MailBox(host, port=port)
    # 先不选文件夹登录（initial_folder=None），避免 SELECT 在 ID 之前执行
    mb.login(user, pwd, initial_folder=None)
    # 发送 ID 命令（仅对网易邮箱）
    _send_imap_id_if_needed(host, mb)
    # 手动选择 INBOX
    mb.folder.set("INBOX")
    return mb

MAX_FETCH_LIMIT = 50


def _normalize_date(dt) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fetch_new_emails(session: EmailSession | None = None):
    """从已登录的邮箱拉取新邮件，写入数据库。

    必须传入 session（已登录的邮箱凭证）。
    邮件和客户数据关联到 session.account_id 实现账号隔离。
    返回 (count, new_email_ids) 元组，new_email_ids 为本次新拉取的邮件 ID 列表。
    """
    if not session:
        raise RuntimeError("请先登录邮箱后再拉取邮件")
    return _fetch_with_imap(
        session.imap_host, session.imap_port,
        session.email, session.password,
        session.account_id,
    )


def _fetch_with_imap(host: str, port: int, user: str, pwd: str,
                     account_id: int | None = None) -> tuple[int, list[int]]:
    """使用 IMAP 拉取邮件（包括已读和未读），写入数据库。

    拉取收件箱中最近 MAX_FETCH_LIMIT 封邮件，
    通过 message_id + account_id 去重，避免重复入库。
    跳过已删除的邮件（用户主动删除后不再拉取）。
    过滤掉自己发送的邮件（发件人 == 登录邮箱）。
    返回 (新邮件数, 新邮件 ID 列表)。
    """
    db = SessionLocal()
    new_email_ids: list[int] = []
    mb = None
    try:
        mb = _login_mailbox(host, port, user, pwd)
        count = 0
        for msg in mb.fetch(limit=MAX_FETCH_LIMIT, reverse=True):
            msg_uid = str(msg.uid)

            # 通过 message_id + account_id 去重
            query = db.query(Email).filter(Email.message_id == msg_uid)
            if account_id is not None:
                query = query.filter(Email.account_id == account_id)
            existing = query.first()
            if existing:
                continue

            # 跳过已删除的邮件（用户主动删除后不再拉取）
            deleted_query = db.query(DeletedEmail).filter(DeletedEmail.message_id == msg_uid)
            if account_id is not None:
                deleted_query = deleted_query.filter(DeletedEmail.account_id == account_id)
            if deleted_query.first():
                continue

            sender = msg.from_ or ""
            if sender.lower() == user.lower():
                continue

            domain = sender.split("@")[-1] if "@" in sender else ""

            # 查找或创建客户（按 account_id 隔离）
            cust_query = db.query(Customer).filter(Customer.email == sender)
            if account_id is not None:
                cust_query = cust_query.filter(Customer.account_id == account_id)
            customer = cust_query.first()
            if not customer:
                customer = Customer(
                    email=sender,
                    domain=domain,
                    name=sender.split("@")[0] if "@" in sender else sender,
                    priority="cold",
                    account_id=account_id,
                )
                db.add(customer)
                db.flush()

            received_dt = _normalize_date(msg.date) or datetime.utcnow()

            email_record = Email(
                customer_id=customer.id,
                message_id=msg_uid,
                subject=msg.subject or "(No Subject)",
                sender=sender,
                recipients=", ".join(msg.to or []),
                body_text=msg.text or "",
                body_html=msg.html or "",
                received_at=received_dt,
                account_id=account_id,
            )
            db.add(email_record)
            db.flush()
            count += 1
            new_email_ids.append(email_record.id)

            if not customer.last_contact_at or (received_dt > customer.last_contact_at):
                customer.last_contact_at = received_dt

        db.commit()
        logger.info(f"[FETCH] Fetched {count} new emails from {user} (account_id={account_id})")
        return count, new_email_ids
    except Exception as e:
        db.rollback()
        logger.error(f"[FETCH] IMAP error: {e}")
        raise RuntimeError(f"IMAP 连接失败: {e}")
    finally:
        db.close()
        if mb:
            try:
                mb.logout()
            except Exception:
                pass


def clear_all_data():
    """清空数据库中所有客户、邮件和分析数据"""
    db = SessionLocal()
    try:
        db.query(EmailAnalysis).delete()
        db.query(Email).delete()
        db.query(Customer).delete()
        db.commit()
        logger.info("[DB] All customer/email data cleared")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Clear error: {e}")
        return False
    finally:
        db.close()


def clear_account_data(account_id: int):
    """清空指定账号的客户、邮件和分析数据"""
    db = SessionLocal()
    try:
        # 先找到该账号的所有邮件 ID
        email_ids = [e.id for e in db.query(Email).filter(Email.account_id == account_id).all()]
        if email_ids:
            db.query(EmailAnalysis).filter(EmailAnalysis.email_id.in_(email_ids)).delete(synchronize_session=False)
        db.query(Email).filter(Email.account_id == account_id).delete(synchronize_session=False)
        db.query(Customer).filter(Customer.account_id == account_id).delete(synchronize_session=False)
        db.commit()
        logger.info(f"[DB] Cleared data for account_id={account_id}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Clear account error: {e}")
        return False
    finally:
        db.close()


def test_imap_connection(host: str, port: int, user: str, pwd: str) -> bool:
    mb = None
    try:
        mb = _login_mailbox(host, port, user, pwd)
        logger.info(f"[TEST] IMAP connection OK for {user}")
        return True
    except Exception as e:
        logger.error(f"[TEST] IMAP connection failed for {user}: {e}")
        raise RuntimeError(str(e))
    finally:
        if mb:
            try:
                mb.logout()
            except Exception:
                pass


def delete_email(email_id: int, account_id: int | None = None) -> bool:
    """删除单封邮件，并记录到 deleted_emails 表防止再次拉取。
    如果该客户没有其他邮件，则自动删除客户。"""
    db = SessionLocal()
    try:
        email_record = db.query(Email).filter(Email.id == email_id).first()
        if not email_record:
            return False
        msg_uid = email_record.message_id
        cust_id = email_record.customer_id
        # 删除分析记录
        db.query(EmailAnalysis).filter(EmailAnalysis.email_id == email_id).delete()
        # 删除邮件
        db.delete(email_record)
        # 记录到已删除表，防止再次拉取
        if msg_uid:
            existing_del = db.query(DeletedEmail).filter(
                DeletedEmail.message_id == msg_uid,
                DeletedEmail.account_id == account_id
            ).first()
            if not existing_del:
                db.add(DeletedEmail(message_id=msg_uid, account_id=account_id))
        db.commit()
        logger.info(f"[DELETE] Email {email_id} deleted, message_id={msg_uid} recorded as deleted")
        # 检查该客户是否还有邮件，如果没有则删除客户
        remaining = db.query(Email).filter(Email.customer_id == cust_id).count()
        if remaining == 0:
            db.query(Customer).filter(Customer.id == cust_id).delete()
            db.commit()
            logger.info(f"[DELETE] Customer {cust_id} has no emails left, auto-deleted")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[DELETE] Error deleting email {email_id}: {e}")
        return False
    finally:
        db.close()


def delete_customer(customer_id: int) -> bool:
    """删除单个客户及其所有邮件和分析记录，并记录已删除邮件防止再次拉取"""
    db = SessionLocal()
    try:
        # 找到该客户所有邮件的 message_id，记录到已删除表
        emails = db.query(Email).filter(Email.customer_id == customer_id).all()
        for em in emails:
            if em.message_id:
                existing_del = db.query(DeletedEmail).filter(
                    DeletedEmail.message_id == em.message_id,
                    DeletedEmail.account_id == em.account_id
                ).first()
                if not existing_del:
                    db.add(DeletedEmail(message_id=em.message_id, account_id=em.account_id))
        # 删除分析记录
        email_ids = [e.id for e in emails]
        if email_ids:
            db.query(EmailAnalysis).filter(EmailAnalysis.email_id.in_(email_ids)).delete(synchronize_session=False)
        # 删除邮件
        db.query(Email).filter(Email.customer_id == customer_id).delete(synchronize_session=False)
        # 删除客户
        db.query(Customer).filter(Customer.id == customer_id).delete(synchronize_session=False)
        db.commit()
        logger.info(f"[DELETE] Customer {customer_id} and {len(emails)} emails deleted")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[DELETE] Error deleting customer {customer_id}: {e}")
        return False
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("[FETCH] 请通过前端登录邮箱后拉取邮件")
