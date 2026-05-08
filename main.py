import os
import time
import datetime
import sqlite3
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

DB_PATH = "data/astrbot_plugin_accounting.db"

@register("astrbot_plugin_accounting", "沈菀", "智能记账助手", "1.0.0")
class AccountingPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # Ensure data directory exists
        db_dir = os.path.dirname(DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            
        # Initialize SQLite database and tables
        with sqlite3.connect(DB_PATH) as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    balance REAL DEFAULT 0,
                    fixed_deduction REAL DEFAULT 0,
                    monthly_budget REAL DEFAULT 0,
                    last_deduction_month TEXT
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    timestamp INTEGER,
                    date TEXT,
                    description TEXT,
                    amount REAL
                )
            ''')
            db.commit()
        logger.info("astrbot_plugin_accounting: Database initialized successfully.")

    def _check_and_apply_monthly_deduction(self, user_id: str):
        """检查并应用每月的固定扣除额度"""
        now = datetime.datetime.now()
        current_month = now.strftime('%Y-%m')
        
        with sqlite3.connect(DB_PATH) as db:
            cursor = db.execute("SELECT fixed_deduction, last_deduction_month FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                fixed_deduction, last_deduction_month = row
                # 只有当设定了固定支出且当月未扣除过才执行扣款
                if fixed_deduction > 0 and last_deduction_month != current_month:
                    # 记录一笔扣除流水
                    db.execute('''
                        INSERT INTO transactions (user_id, timestamp, date, description, amount)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (user_id, int(now.timestamp()), now.strftime('%Y-%m-%d %H:%M:%S'), f"每月固定支出扣除 ({current_month})", -fixed_deduction))
                    
                    # 更新余额和上一次扣除月份
                    db.execute('''
                        UPDATE users
                        SET balance = balance - ?, last_deduction_month = ?
                        WHERE user_id = ?
                    ''', (fixed_deduction, current_month, user_id))
                    db.commit()
                    logger.info(f"astrbot_plugin_accounting: Applied monthly fixed deduction {-fixed_deduction} for user {user_id}")

    @filter.llm_tool(name="init_account")
    async def init_account(self, event: AstrMessageEvent, initial_balance: float) -> str:
        '''初始化账户并设定起始总余额。当用户首次使用或想重置总余额时使用。
        Args:
            initial_balance(number): 起始余额金额
        '''
        user_id = event.get_sender_id()
        with sqlite3.connect(DB_PATH) as db:
            # 使用 INSERT OR REPLACE (SQLite 用法) 或 ON CONFLICT
            db.execute('''
                INSERT INTO users (user_id, balance, last_deduction_month) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance
            ''', (user_id, initial_balance, datetime.datetime.now().strftime('%Y-%m')))
            db.commit()
            
        return f"账户初始化成功，起始余额已设置为 {initial_balance} 元。"

    @filter.llm_tool(name="record_transaction")
    async def record_transaction(self, event: AstrMessageEvent, description: str, amount: float) -> str:
        '''记录一笔具体的进出账。消费请传入负数金额，收入请传入正数金额。
        Args:
            description(string): 用途说明或入账说明
            amount(number): 金额（正数为入账，负数为支出）
        '''
        user_id = event.get_sender_id()
        # 在变动资金前，检查是否需要触发本月的固定扣款
        self._check_and_apply_monthly_deduction(user_id)
        
        now = datetime.datetime.now()
        timestamp = int(now.timestamp())
        date_str = now.strftime('%Y-%m-%d %H:%M:%S')
        current_month = now.strftime('%Y-%m')
        month_start_timestamp = int(datetime.datetime(now.year, now.month, 1).timestamp())
        
        with sqlite3.connect(DB_PATH) as db:
            # 确保用户记录存在
            db.execute('''
                INSERT OR IGNORE INTO users (user_id, balance, last_deduction_month)
                VALUES (?, 0, ?)
            ''', (user_id, current_month))
            
            # 插入流水
            db.execute('''
                INSERT INTO transactions (user_id, timestamp, date, description, amount)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, timestamp, date_str, description, amount))
            
            # 更新余额
            db.execute('''
                UPDATE users SET balance = balance + ? WHERE user_id = ?
            ''', (amount, user_id))
            
            # 获取最新余额
            cursor = db.execute("SELECT balance, monthly_budget FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            balance = row[0] if row else 0
            monthly_budget = row[1] if row else 0
                
            # 计算当月日常消费（总计负数流水，排除固定支出扣除）
            cursor = db.execute('''
                SELECT SUM(amount) FROM transactions 
                WHERE user_id = ? AND timestamp >= ? AND amount < 0 AND description NOT LIKE '每月固定支出扣除%'
            ''', (user_id, month_start_timestamp))
            spent_row = cursor.fetchone()
            spent_this_month = abs(spent_row[0]) if spent_row and spent_row[0] else 0
                
            db.commit()

        res = f"记录成功：{date_str} | {description} | {amount}元 | 当前总余额变为 {balance}元。"
        if monthly_budget > 0:
            remaining_budget = monthly_budget - spent_this_month
            res += f" 当月日常消费已用 {spent_this_month}元，日常预算剩余 {remaining_budget}元。"
            if spent_this_month >= monthly_budget:
                res += " ⚠️警告：当月日常消费已达到或超出预算！"
            elif spent_this_month >= monthly_budget * 0.8:
                res += " ⚠️提示：当月日常消费已达到预算的80%。"
        return res

    @filter.llm_tool(name="set_fixed_deduction")
    async def set_fixed_deduction(self, event: AstrMessageEvent, amount: float) -> str:
        '''设置每月的固定扣除额度（如房租、分期等）。设定后每个月会有一次自动扣除。
        Args:
            amount(number): 每月固定支出的金额
        '''
        user_id = event.get_sender_id()
        with sqlite3.connect(DB_PATH) as db:
            db.execute('''
                INSERT INTO users (user_id, fixed_deduction, last_deduction_month) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET fixed_deduction = excluded.fixed_deduction
            ''', (user_id, amount, ''))
            db.commit()
        return f"已成功设置每月固定支出为 {amount} 元。"

    @filter.llm_tool(name="set_monthly_budget")
    async def set_monthly_budget(self, event: AstrMessageEvent, amount: float) -> str:
        '''设置或更新当月的日常消费上限预算。
        Args:
            amount(number): 月度日常消费预算金额
        '''
        user_id = event.get_sender_id()
        with sqlite3.connect(DB_PATH) as db:
            db.execute('''
                INSERT INTO users (user_id, monthly_budget, last_deduction_month) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET monthly_budget = excluded.monthly_budget
            ''', (user_id, amount, ''))
            db.commit()
        return f"已成功设置月度日常消费预算为 {amount} 元。"

    @filter.llm_tool(name="query_transactions")
    async def query_transactions(self, event: AstrMessageEvent, start_date: str, end_date: str) -> str:
        '''根据给定的时间范围查询流水明细。
        Args:
            start_date(string): 开始日期，格式为 YYYY-MM-DD
            end_date(string): 结束日期，格式为 YYYY-MM-DD
        '''
        user_id = event.get_sender_id()
        
        try:
            start_ts = int(datetime.datetime.strptime(start_date, '%Y-%m-%d').timestamp())
            end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
            end_ts = int(end_dt.timestamp())
        except ValueError:
            return "日期格式错误，请确保使用 YYYY-MM-DD 格式（例如 2026-05-08）。"

        records = []
        total_income = 0
        total_expense = 0
        
        with sqlite3.connect(DB_PATH) as db:
            cursor = db.execute('''
                SELECT date, description, amount FROM transactions
                WHERE user_id = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
            ''', (user_id, start_ts, end_ts))
            for row in cursor:
                date, desc, amount = row
                records.append(f"{date} | {desc} | {amount}元")
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += abs(amount)

        if not records:
            return f"{start_date} 到 {end_date} 期间没有账单流水。"
            
        summary = f"{start_date} 到 {end_date} 账单明细：\n"
        summary += "\n".join(records)
        summary += f"\n---\n总收入: {total_income}元, 总支出: {total_expense}元"
        return summary

    @filter.llm_tool(name="get_account_status")
    async def get_account_status(self, event: AstrMessageEvent) -> str:
        '''获取当前的财务状态，包括总余额、当月已用、当月剩余额度、月度总预算、固定支出等。可用于回答“我还剩多少钱”或在给出消费建议前拉取数据。
        '''
        user_id = event.get_sender_id()
        self._check_and_apply_monthly_deduction(user_id)
        
        now = datetime.datetime.now()
        month_start_timestamp = int(datetime.datetime(now.year, now.month, 1).timestamp())
        
        with sqlite3.connect(DB_PATH) as db:
            cursor = db.execute("SELECT balance, fixed_deduction, monthly_budget FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return "数据库中没有找到账本，请先调用 init_account 告诉我你的起始余额。"
            balance, fixed_deduction, monthly_budget = row
                
            cursor = db.execute('''
                SELECT SUM(amount) FROM transactions 
                WHERE user_id = ? AND timestamp >= ? AND amount < 0 AND description NOT LIKE '每月固定支出扣除%'
            ''', (user_id, month_start_timestamp))
            spent_row = cursor.fetchone()
            spent_this_month = abs(spent_row[0]) if spent_row and spent_row[0] else 0

        res = f"【账户状态】\n总余额：{balance}元\n每月固定扣除：{fixed_deduction}元\n"
        if monthly_budget > 0:
            remaining_budget = monthly_budget - spent_this_month
            res += f"当月日常消费预算：{monthly_budget}元\n当月已用：{spent_this_month}元\n当月剩余可用预算：{remaining_budget}元\n"
            
            # 计算距离月底的天数
            next_month = now.month % 12 + 1
            next_month_year = now.year + (now.month // 12)
            last_day_of_month = datetime.datetime(next_month_year, next_month, 1) - datetime.timedelta(days=1)
            days_left = (last_day_of_month - now).days
            res += f"距离月底还有 {days_left} 天。"
        else:
            res += "尚未设置月度消费预算。"
            
        return res
