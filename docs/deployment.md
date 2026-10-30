# 部署指南

本文档详细说明 AI CRM 系统的各种部署方式。

---

## 方式一：一键启动（推荐）

### Windows

1. 下载并解压项目压缩包
2. 双击运行 `start.bat`
3. 首次运行会自动：
   - 检测并安装 Python（如果没有）
   - 从 `.env.example` 创建 `.env` 配置文件
   - 弹出记事本让你填入 DeepSeek API Key
   - 安装 Python 依赖包
   - 启动服务
   - 8秒后自动打开浏览器

### Linux / macOS

```bash
# 解压后进入项目目录
cd AI-CRM-System

# 赋予执行权限
chmod +x start.sh

# 运行
./start.sh
```

---

## 方式二：手动部署

### 1. 环境准备

```bash
# 检查 Python 版本（需要 3.9+）
python --version

# 安装 pip（通常 Python 自带）
pip --version
```

### 2. 获取代码

```bash
# 方式一：Git 克隆
git clone <你的仓库地址>
cd AI-CRM-System

# 方式二：下载压缩包并解压
# 下载后进入项目目录
```

### 3. 配置 API Key

```bash
cd backend

# 复制配置模板
cp .env.example .env

# 编辑配置文件
# Windows: notepad .env
# Linux/macOS: vi .env 或 nano .env

# 填入你的 DeepSeek API Key
# DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
```

### 4. 安装依赖

```bash
pip install -r requirements.txt
```

### 5. 启动服务

```bash
# 开发模式（带自动重载）
python -m uvicorn app:app --reload --host 0.0.0.0 --port 8000

# 生产模式
python app.py
```

### 6. 访问系统

浏览器打开 `http://127.0.0.1:8000`

---

## 方式三：后台运行（生产环境）

### 使用 systemd（Linux）

创建服务文件 `/etc/systemd/system/ai-crm.service`：

```ini
[Unit]
Description=AI CRM System
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/ai-crm/backend
Environment=PATH=/opt/ai-crm/venv/bin
ExecStart=/opt/ai-crm/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-crm
sudo systemctl start ai-crm
sudo systemctl status ai-crm
```

### 使用 NSSM（Windows）

```powershell
# 下载 NSSM: https://nssm.cc/download
nssm install AI-CRM "C:\path\to\python.exe" "app.py"
nssm set AI-CRM AppDirectory "C:\path\to\backend"
nssm start AI-CRM
```

---

## 方式四：Docker 部署

### 创建 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY backend/ .
COPY frontend/ /app/frontend/

# 环境变量
ENV PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 8000

# 启动
CMD ["python", "app.py"]
```

### 构建并运行

```bash
# 构建镜像
docker build -t ai-crm .

# 运行容器（注意：数据卷需要挂载，否则重启后数据丢失）
docker run -d \
  --name ai-crm \
  -p 8000:8000 \
  -e DEEPSEEK_API_KEY=your_api_key_here \
  -v $(pwd)/data:/app/data \
  ai-crm
```

---

## 反向代理配置（可选）

### Nginx

```nginx
server {
    listen 80;
    server_name crm.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket 支持（如果需要）
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### HTTPS（使用 Let's Encrypt）

```bash
# 安装 certbot
sudo apt install certbot python3-certbot-nginx

# 申请证书
sudo certbot --nginx -d crm.yourdomain.com
```

---

## 数据备份

### 手动备份

```bash
# 备份数据库（直接复制文件即可）
cp backend/crm.db backups/crm-$(date +%Y%m%d).db

# 备份配置
cp backend/.env backups/.env-$(date +%Y%m%d)
```

### 自动备份（Linux cron）

```bash
# 编辑 crontab
crontab -e

# 每天凌晨 3 点备份
0 3 * * * cp /opt/ai-crm/backend/crm.db /opt/ai-crm/backups/crm-$(date +\%Y\%m\%d).db

# 保留最近 30 天的备份
0 4 * * * find /opt/ai-crm/backups/ -name "crm-*.db" -mtime +30 -delete
```

---

## 常见问题

### Q: 端口 8000 被占用怎么办？

A: 修改启动端口：

```bash
# 方式一：修改 app.py 中的端口
# 方式二：使用命令行参数
python -m uvicorn app:app --host 0.0.0.0 --port 8080
```

### Q: 如何升级到新版本？

A: 

```bash
# 1. 备份数据
cp backend/crm.db crm.db.backup

# 2. 拉取最新代码
git pull origin main

# 3. 更新依赖
pip install -r requirements.txt --upgrade

# 4. 重启服务
# systemctl restart ai-crm
```

### Q: AI 功能不工作？

A: 检查以下几点：

1. `.env` 文件中的 `DEEPSEEK_API_KEY` 是否正确
2. 网络是否能访问 `api.deepseek.com`
3. API Key 是否还有余额
4. 查看服务器日志中的错误信息

### Q: 数据存储在哪里？

A: 所有数据存储在 `backend/crm.db`（SQLite 数据库文件）。备份只需复制这个文件。

### Q: 支持多人使用吗？

A: 当前版本为单人本地使用模式。多用户协作功能在 v4.0 规划中。
