# QiCardMesh Server

Django 后端服务。开发阶段使用 **SQLite**，依赖安装在项目本地的 `.venv` 虚拟环境中。

## 环境要求

- Python 3.12+

## 目录结构

```text
Server/
├── config/           # Django 项目配置（settings / urls / wsgi / asgi）
├── manage.py         # Django 管理入口
├── requirements.txt  # Python 依赖
├── .venv/            # 本机虚拟环境（已加入 gitignore）
├── db.sqlite3        # 迁移后生成的 SQLite 数据库（已 gitignore）
└── README.md
```

## 一、安装依赖

在 `Server` 目录下执行：

```powershell
cd Server
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

首次部署时复制环境变量示例并按实际环境修改：

```powershell
Copy-Item .env.example .env
```

`config/settings.py` 会自动读取 `Server/.env`；已存在的系统环境变量优先级更高。

项目通过 `qicardmesh.middleware.DisableCsrfChecksMiddleware` 全局禁用 CSRF 校验，
包括 Django 管理后台的登录、退出和修改操作。客户端无需提交 CSRF token；
用户认证与权限检查仍然有效。CSRF 与 CORS（跨域访问）是独立机制；
此配置不会开放 CORS，并且使用 Cookie 认证的后台不再具备 CSRF 防护。
部署配置变更后需重启所有 Uvicorn worker。

上述命令会创建 `.venv` 并安装 `requirements.txt` 中的依赖。

## 二、数据库迁移（SQLite）

首次启动或模型变更后，执行迁移：

```powershell
# 根据 models 生成迁移文件（有自定义 app / model 时需要）
python manage.py makemigrations

# 应用迁移，创建/更新 db.sqlite3
python manage.py migrate
```

默认数据库配置在 `config/settings.py`：

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
```

`db.sqlite3` 位于 `Server/` 根目录，已加入 `.gitignore`，不要提交到版本库。

### 常用数据库相关命令

```powershell
# 查看迁移状态
python manage.py showmigrations

# 查看 SQL（不执行）
python manage.py sqlmigrate <app_name> <migration_name>

# 打开 Django shell（可检查 ORM）
python manage.py shell
```

## 三、创建管理员账号（可选）

管理后台使用 Django SimpleUI，默认主题为 `Enterprise blue`（配置值 `e-blue.css`），静态资源使用本地文件。
首页快捷入口、操作区、信息区及分析功能在 `config/settings.py` 中关闭。
部署或升级后执行 `python manage.py collectstatic --noinput`，再重启 Uvicorn。
Nginx 的 `/static/` 应映射到 `STATIC_ROOT`（当前为 `Server/static/`）。

访问后台 `/admin/` 前需要超级用户：

```powershell
python manage.py createsuperuser
```

按提示输入用户名、邮箱、密码即可。

## 四、启动开发服务器

```powershell
python -m uvicorn config.asgi:application --reload --lifespan off
```

默认地址：

- 首页：<http://127.0.0.1:8000/>
- 管理后台：<http://127.0.0.1:8000/admin/>
- Swagger UI：<http://127.0.0.1:8000/api/docs/>
- OpenAPI JSON：<http://127.0.0.1:8000/api/openapi.json>

指定端口：

```powershell
python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload --lifespan off
```

### Linux ASGI 部署（Uvicorn）

先在部署面板停止旧的 uWSGI 服务并关闭其自动重启，释放 `13231` 端口。
在服务器安装依赖，然后使用 Uvicorn 加载 `config.asgi:application`：

```bash
cd /data/QiCardMesh/Server
# 尚无虚拟环境时执行：python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn config.asgi:application --host 0.0.0.0 --port 13231 --workers 2 --lifespan off
```

在部署面板的自定义启动命令 / 进程守护中设置：

- 工作目录：`/data/QiCardMesh/Server`
- 启动命令：`/data/QiCardMesh/Server/.venv/bin/python -m uvicorn config.asgi:application --host 0.0.0.0 --port 13231 --workers 2 --lifespan off`
- 由面板管理日志和进程重启；不再加载原来的 `[uwsgi]` INI 配置。
- 如果服务器虚拟环境位于其他目录，将命令中的 `.venv/bin/python` 替换为实际解释器路径。

Uvicorn 使用 HTTP 服务；Nginx 应通过 `proxy_pass http://127.0.0.1:13231` 转发，不能使用 `uwsgi_pass`。
管理后台的 `/static/` 文件需要单独由 Nginx 提供，Uvicorn 不自动托管 Django 静态文件。
`--lifespan off` 用于 Django ASGI 应用；生产环境使用两个 worker，不启用 `--reload`。

## 五、日常开发流程（简版）

1. `python -m pip install -r requirements.txt` — 安装依赖
2. 修改 / 新增 app 与 model
3. `python manage.py makemigrations`
4. `python manage.py migrate`
5. `python -m uvicorn config.asgi:application --reload --lifespan off`

## 六、新增 Django App

```powershell
python manage.py startapp <app_name>
```

然后在 `config/settings.py` 的 `INSTALLED_APPS` 中注册该 app。

## 七、添加 Python 依赖

```powershell
python -m pip install <package>
python -m pip freeze > requirements.txt
```

安装后更新并提交 `requirements.txt`。

## 八、静态文件（后续部署用）

开发环境一般无需额外处理。若收集静态文件：

```powershell
python manage.py collectstatic
```

## 九、常见问题

| 现象 | 处理 |
|------|------|
| `No module named django` | 激活 `.venv` 后执行 `python -m pip install -r requirements.txt` |
| 改了 model 页面报错 | 先 `makemigrations` 再 `migrate` |
| 端口被占用 | `python -m uvicorn config.asgi:application --port 8001 --reload --lifespan off` |
| 想清空本地库重来 | 删除 `db.sqlite3` 后重新 `migrate`（会丢本地数据） |

## 十、快速命令速查

```powershell
cd Server
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # 可选
python -m uvicorn config.asgi:application --reload --lifespan off
```

## 十一、认证 API

### OpenAPI / Swagger 调试

访问 `/api/docs/` 查看全部接口的参数、响应、错误码，并通过 **Try it out** 发起真实请求。
`/api/openapi.json` 提供 OpenAPI 3.1.0 文档，可导入 Postman、Apifox 等工具。
文档与 JSON 无需登录即可访问；接口本身仍执行原有的认证检查。

1. 展开 `POST /api/auth/login`，点击 **Try it out**，输入用户名（或邮箱）及客户端计算出的 `sha256$...` 密码摘要，点击 **Execute**。
   登录必须填写 `X-Device-ID` 请求头（建议使用客户端持久保存的 UUID），可选 `X-Platform`。
2. 复制响应的 `data.access_token`，点击页面的 **Authorize**，仅粘贴 token，无需添加 `Bearer ` 前缀。
3. 即可调试 `/api`、`/api/auth/me`、`/api/auth/logout`；MQTT 鉴权需要将 token 填入 JSON 请求体。
4. 刷新令牌后需用新 access token 重新 **Authorize**；刷新页面不会保留授权。

服务端统一使用 Pydantic v2，所有请求、响应、令牌会话和验证码缓存模型集中在 `qicardmesh/schemas.py`。
`ENDPOINTS` 注册每个接口的方法、请求模型、响应模型和文档说明；
原生 Django 视图通过 `qicardmesh/api.py` 的 `@api_endpoint("路由名")` 自动校验请求并序列化响应。
视图接收已校验的模型对象、返回对应响应模型，业务错误继续使用 `response_base()`。
校验使用严格类型，保留原有业务错误码，以及 MQTT 拒绝时 HTTP 200 的原始响应格式。
OpenAPI 的字段定义由这些 Pydantic 模型自动生成，由 `qicardmesh/openapi.py` 提供 JSON 和 Swagger UI 视图。
新增接口时，在 `schemas.py` 定义模型并注册 `ENDPOINTS`，在视图添加装饰器和 Django 命名路由；
修改字段只需更新模型，无需再手写一份 OpenAPI schema。路径通过 Django 命名路由解析。
Swagger UI 的 JS/CSS 使用固定版本 `5.33.2` 的 jsDelivr CDN，浏览器需能访问该 CDN；
OpenAPI JSON 端点不依赖 CDN，也不依赖 Redis。实际认证接口仍需要 Redis。

认证接口统一返回以下结构：

```json
{"code": 200, "message": "success", "data": {}}
```

`code` 为业务码：`200` 表示成功；错误码由 `config/error.py` 统一维护，使用五位数字。

`/api/auth/login` 支持通过 `X-Sessionid` 请求头传递 `/api/auth/captcha` 返回的会话 ID，
也支持原有的 `sessionid` Cookie；两者同时存在时优先使用 Cookie。
会话 ID 不替代登录凭据，仍需提交密码或邮箱验证码。

每条请求都会由 `RequestIdMiddleware` 生成 UUID 请求标识，handler 可通过
`request.request_id` 或 `config.context.get_request_context()` 读取。中间件还会将
`config.utils.get_client_ip(request)` 的结果保存为 `client_ip`，并读取 `X-Device-ID`、
`X-Platform` 保存为 `device_id`、`platform`。响应会统一返回相同的 `X-Request-ID` Header，便于日志关联和问题排查。

令牌会写入 Redis（默认 `redis://127.0.0.1:6379/12`，可用 `REDIS_URL` 修改）。
`ACCESS_TOKEN_TTL` 默认 7 天（604800 秒），`REFRESH_TOKEN_TTL` 默认 30 天。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 支持 `{"username": "用户名或邮箱", "password": "sha256$..."}` 或 `{"email": "邮箱", "email_code": "验证码"}`，返回 Bearer access/refresh token |
| GET | `/api/auth/captcha` | 返回绑定当前 session 的 PNG 图形验证码 |
| POST | `/api/auth/email-send` | `{"email": "ex@a.com", "captcha": "验证码"}`，发送 6 位邮箱验证码 |
| POST | `/api/auth/change-password` | Bearer 登录后提交 `{"email_code": "验证码", "password": "sha256$..."}`，使用 Django 哈希修改密码 |
| POST | `/api/auth/change-email` | Bearer 登录后提交 `{"email_code": "验证码", "email": "新邮箱"}`，修改邮箱 |
| POST | `/api/auth/refresh` | `{"refresh_token": "..."}`，轮换并返回新的 token 对 |
| POST | `/api/auth/mqtt-auth` | `{"access_token": "..."}`，返回 MQTT broker 所需的 allow/deny 与 ACL |
| GET | `/api/auth/me` | Header `Authorization: Bearer <access_token>`，返回当前用户信息 |
| POST | `/api/auth/logout` | Header `Authorization: Bearer <access_token>`，撤销当前会话 |
| GET | `/api/auth/devices` | Bearer 登录后分页查看自己的登录设备，标记当前设备 |
| DELETE | `/api/auth/devices/{device_id}` | Bearer 登录后删除设备列表中的记录 `id`，踢出该设备 |
| GET | `/api` | 需要 Bearer access token 的示例受保护接口 |

登录响应只返回 token 信息，不包含用户对象。`access_token` 统一使用 `access_` 前缀，
`refresh_token` 统一使用 `refresh_` 前缀。登录和刷新返回的 `refresh_token` 为单次使用令牌；
刷新成功后旧的 access/refresh token 会失效。

API 两种登录方式均必须携带 `X-Device-ID`。同一用户、同一设备重新登录会替换该设备旧 token；
退出或删除设备后，该设备的 access/refresh token 均失效。Django 管理后台网页登录不要求设备 ID、
也不记录为 API 设备。部署需运行 `python manage.py migrate`，升级前的 API token 需要重新登录。
完整接口、数据结构、并发处理、MQTT 适用范围及部署步骤见 [登录设备管理](docs/login-devices.md)。

密码登录或邮箱验证码登录连续失败 5 次后，会分别锁定对应登录标识 1 分钟，锁定期间返回业务码 `42902`。

登录也支持邮箱验证码：先调用 `/api/auth/email-send`，再提交
`{"email": "ex@a.com", "email_code": "123456"}`。验证码 5 分钟有效、只能使用一次，
同一邮箱 30 秒内只能发送一次，最多尝试 5 次。SMTP SSL 465 配置读取 `SMTP_HOST`、
`SMTP_USERNAME`、`SMTP_PASSWORD` 和 `SMTP_FROM_EMAIL`。

调用 `/api/auth/captcha` 获取 PNG 图形验证码时，响应会同时设置 `sessionid` Cookie，
并通过 `X-sessionid` 响应头返回相同的会话 ID。随后携带同一 Cookie，将验证码通过
`captcha`（也兼容 `captcha_code`）提交给 `/api/auth/email-send`。验证码为 5 位数字和字母组合，
字母不区分大小写，验证成功或失败后都会消耗当前验证码。

修改密码和用户名密码登录的 `password` 必须由客户端先计算为 `sha256$` 加 64 位小写 SHA-256 摘要，例如：

```text
sha256$ + SHA256(原始密码的 UTF-8 字节).hexdigest()
```

服务端不会接收原始密码，只会把客户端摘要交给 Django `set_password()`，数据库最终保存为 Django 标准密码哈希。固定摘要本身可以被重放，仍必须使用 HTTPS；不能传入客户端自行生成的 Django 数据库哈希值。修改密码的验证码发送到当前邮箱；修改邮箱的验证码发送到待设置的新邮箱。

`/api/auth/mqtt-auth` 返回 MQTT 鉴权原始结构，不使用 `ResponseBase` 包装：

```json
{
  "result": "allow",
  "is_superuser": false,
  "expire_at": 1654254601,
  "acl": [
    {"permission": "allow", "action": "subscribe", "topic": "QiCardMesh/device/<用户ID>/#"},
    {"permission": "allow", "action": "publish", "topic": "QiCardMesh/device/<用户ID>/#"},
    {"permission": "deny", "action": "all", "topic": "#"}
  ]
}
```
