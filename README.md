# WeReadGears：微信读书自动阅读打卡
[![Auto Reading Bot](https://img.shields.io/github/actions/workflow/status/funnyzak/weread-bot/auto-reading.yml?style=flat-square&label=Auto%20Reading)](https://github.com/funnyzak/weread-bot/actions/workflows/auto-reading.yml)
[![Docker Tags](https://img.shields.io/docker/v/funnyzak/weread-bot?sort=semver&style=flat-square&label=docker%20image)](https://hub.docker.com/r/funnyzak/weread-bot/)
[![Python](https://img.shields.io/badge/python-3.9+-blue?style=flat-square)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

微信读书自动阅读工具，支持 7x24 小时不间断运行，模拟真人阅读行为，可视化配置，自定义通知。

## 适合哪些用户

- 想在本地快速跑通微信读书自动阅读的个人用户
- 需要多账号同时运行的用户
- 希望 Docker 一键部署，开箱即用的用户
- 需要 Web 界面可视化配置的用户

## 特性

- **可视化配置**：完全可视化配置机器人，直接扫码登录就能靠模拟浏览器自动截取运行所需参数，新手友好，门槛极低！
- **热力图**：web端快速查看阅读时长热力图
- **API阅读**：HTTP API 请求 + 签名计算，高速稳定（默认）
- **多用户支持**：独立凭证管理，支持同时多个账号
- **定时任务**：Cron 表达式定时执行
- **守护进程**：长期运行，自动管理会话
- **多平台通知**：支持 Bark、PushPlus、Telegram、Ntfy、飞书、企业微信、钉钉、Gotify、Server酱、PushDeer、WxPusher
- **书籍自动化配置**：搜索书籍/导入书架中书籍可自动完成配置，自动获取章节
- **人类行为模拟**：随机滚动、休息间隔、书籍切换

## 演示截图

<img width="1727" height="1264" alt="image" src="https://github.com/user-attachments/assets/e94e82a9-30a9-40da-b137-996a3944e94f" />

## 更新计划

### 下一版（v2.0）:压缩一下浏览器内核

当前镜像基于 `mcr.microsoft.com/playwright/python`，**默认就带一个完整的 Chromium 浏览器内核**——这主要是为了实现web界面无感配置。


但代价是：


- 镜像体积约 **1.2GB**（Chromium + 依赖 + Python）

- 运行时内存占用 **500MB ~ 1GB**

- 1GB 小内存的 NAS / VPS / OpenWrt 路由不一定带的动

目标是：

- 把整体运行内存压缩到90mb

## 快速开始（Docker Compose 部署）

只需 3 步即可跑起来。

### 第 1 步：克隆项目

```bash
git clone https://github.com/GaviZhao/WeReadGears.git
cd WeReadGears
```

### 第 2 步：启动容器

```bash
docker-compose up -d
```

程序首次启动时会自动生成配置文件 `shared/config.yaml`。

首次启动需构建镜像（约 5~10 分钟），后续启动秒级完成。

查看启动日志确认正常：

```bash
docker logs -f weread
```

看到 `Web 服务启动在端口 8000` 即代表成功。

### 第 3 步：扫码登录

1. 浏览器打开 `http://你的IP:8080`
2. 点击右侧「登录」按钮
3. 用微信扫描弹出的二维码
4. 扫码成功后页面自动刷新，状态变为「已登录」

登录成功后凭证保存在 `shared/credentials/` 下，重启容器不丢失。

**搞定！** 程序会按照默认配置每天 9:00 和 18:00 自动阅读。你可以在 Web 界面实时查看状态、调整配置、手动触发阅读。

## 配置说明

配置文件位于 `shared/config.yaml`，首次启动时自动生成。所有配置项都有默认值，可根据需要修改。

### 快速配置示例

```yaml
app:
  port: 8000

reading:
  target_duration: "30-60"       # 每次阅读 30~60 分钟
  mode: "smart_random"

schedule:
  enabled: true
  times: ["09:00", "18:00"]      # 每天 9 点和 18 点自动阅读
  timezone: "Asia/Shanghai"

notification:
  enabled: false                  # 如需通知改为 true
```

> **提示**：通知渠道、书籍配置等都可以启动后在 Web 界面里配置，不一定要手动编辑 YAML。

## 阅读模式

| 模式 | 说明 |
|------|------|
| **API 模式**（默认） | HTTP API 请求 + 签名计算，高速稳定 |
| **浏览器模式** | Playwright 滚动模拟，作为 API 失败时的备用 |

API 模式失败时会自动切换到浏览器模式，无需手动干预。

**切换方式**：Web 界面顶部模式切换按钮

## 多用户配置

### 通过 Web 界面

1. 进入「用户」标签页
2. 点击「添加用户」
3. 输入用户名并保存
4. 点击「登录」为用户扫码授权

### 通过配置文件

```yaml
users:
  - name: "用户1"
    display_name: "Alice"
    books: []
    reading_overrides:
      target_duration: "45-90"
      mode: "smart_random"
  - name: "用户2"
    display_name: "Bob"
    books: []
    reading_overrides:
      target_duration: "30-60"
      mode: "sequential"
```

## 运行模式

### 立即执行（默认）

```bash
python src/main.py --mode immediate
```

程序启动后立即开始一次阅读会话，完成后切换到定时任务模式。

### 定时任务

```bash
python src/main.py --mode scheduled
```

按配置的 Cron 表达式定时执行。

### 守护进程

```bash
python src/main.py --mode daemon
```

程序持续运行，自动管理会话间隔，支持每日最大会话数限制。

## 目录结构

```
WeReadGears/
├── config.example.yaml      # 配置模板
├── requirements.txt         # Python 依赖
├── Dockerfile               # Docker 镜像
├── docker-compose.yml       # Docker Compose 部署
├── .env.example             # 环境变量模板
├── README.md
├── src/
│   ├── main.py              # 程序入口
│   ├── config.py            # 配置管理
│   ├── api_reader.py        # API 阅读引擎
│   ├── reader.py            # 浏览器阅读引擎
│   ├── credential_manager.py # 凭证管理
│   ├── session_manager.py   # 会话管理
│   ├── daemon.py            # 守护进程
│   ├── http_client.py       # HTTP 客户端
│   ├── scheduler.py         # 定时任务
│   ├── notifier.py          # 通知服务
│   ├── history_manager.py   # 历史记录
│   ├── cookie_manager.py    # Cookie 管理
│   ├── browser.py           # 浏览器管理
│   ├── utils/
│   │   ├── logger.py
│   │   └── signature.py     # 签名计算
│   └── web/
│       ├── app.py           # FastAPI Web
│       └── templates/
│           └── index.html   # Web UI
├── shared/                  # 持久化存储（Docker Volume）
│   ├── config.yaml          # 用户配置（首次启动自动生成）
│   ├── credentials/         # 用户凭证
│   └── logs/
└── .github/
    └── workflows/
        └── auto-reading.yml # GitHub Actions
```

## Docker 部署详解

### docker-compose.yml 说明

```yaml
services:
  wereadgears:
    image: gavizhao/wereadgears:latest
    hostname: wereadgears
    ports:
      - "8080:8000"   # 宿主机:容器,访问 http://localhost:8080
    volumes:
      # 必挂:数据/凭证/日志/配置
      - ./shared:/app/shared
      # 时区(让容器内时间和宿主机一致)
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    environment:
      - TZ=Asia/Shanghai
      - CONFIG_FILE=/app/shared/config.yaml
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s
    deploy:
      resources:
        limits:
          memory: 2G
        reservations:
          memory: 1G
```

**常见自定义**：

| 需求 | 修改位置 |
|------|---------|
| 换端口（如改为 9090） | `ports: - "9090:8000"` |
| 换时区 | `TZ=America/New_York` |
| 限制内存 | 调整 `deploy.resources` |

### 目录结构

部署后 `shared/` 目录会自动生成以下结构：

```
shared/
├── config.yaml          # 配置文件（首次启动自动生成）
├── credentials/         # 用户凭证（扫码登录后自动生成）
│   └── default.json
└── logs/
    └── weread.log
```

### 常用命令

```bash
# 启动
docker-compose up -d

# 查看日志
docker logs -f weread

# 重启（修改配置后）
docker-compose restart

# 停止
docker-compose down

# 重新构建（代码更新后）
docker-compose up -d --build
```

### 单次运行（不用 Compose）

```bash
# 构建镜像
docker build -t weread-auto-reader .

# 运行
docker run -d \
  --name weread \
  -p 8080:8000 \
  -v $(pwd)/shared:/app/shared \
  -e TZ=Asia/Shanghai \
  -e CONFIG_FILE=/app/shared/config.yaml \
  --restart unless-stopped \
  weread-auto-reader
```

## 命令行参数

```bash
python src/main.py --mode [immediate|scheduled|daemon]
python src/main.py --mode immediate --user 用户名
python src/main.py --validate-config  # 校验配置
python src/main.py --show-last-run    # 查看上次结果
```

## 常见问题

### Q: 如何防止被识别为机器人？

**A:** 项目内置多层防检测机制：
- 速率限制（默认 10 次/分钟）
- 随机延迟和休息
- 随机滚动速度和方向
- Cookie 自动管理

### Q: 支持多账号同时运行吗？

**A:** 支持。可以通过 Web 界面添加多个用户，每个用户独立登录、独立凭证、独立配置。

### Q: API 模式和浏览器模式有什么区别？

| 方面 | API 模式 | 浏览器模式 |
|------|---------|-----------|
| 速度 | 快 | 慢 |
| 稳定性 | 高 | 中 |
| 资源占用 | 低 | 高 |
| 适用场景 | 日常使用 | 备用/调试 |

API 模式失败时会自动切换到浏览器模式。

### Q: 如何查看运行状态？

**A:**
- Web 界面状态面板和统计数据
- `docker logs -f weread`
- `/history` API 端点

## 致谢

本项目参考了以下项目：

- [funnyzak/weread-bot](https://github.com/funnyzak/weread-bot) - 微信读书自动阅读机器人
- [findmover/wxread](https://github.com/findmover/wxread) - 微信读书自动阅读原理解析

## 免责声明

本项目仅供学习和研究目的，不得用于任何商业活动。用户在使用本项目时应遵守所在地区的法律法规，对于违法使用所导致的后果，本项目及作者不承担任何责任。

本项目可能存在未知的缺陷和风险（包括但不限于账号封禁等），使用者应自行承担使用本项目所产生的所有风险及责任。

## License

MIT License
