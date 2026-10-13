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

认证接口统一返回以下结构：

```json
{"code": "0", "message": "success", "data": {}}
```

令牌会写入 Redis（默认 `redis://127.0.0.1:6379/12`，可用 `REDIS_URL` 修改）。
`ACCESS_TOKEN_TTL` 默认 7 天（604800 秒），`REFRESH_TOKEN_TTL` 默认 30 天。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | `{"username": "用户名或邮箱", "password": "密码"}`，返回 Bearer access/refresh token |
| POST | `/api/auth/refresh` | `{"refresh_token": "..."}`，轮换并返回新的 token 对 |
| POST | `/api/auth/mqtt-auth` | `{"access_token": "..."}`，返回 MQTT broker 所需的 allow/deny 与 ACL |
| GET | `/api/auth/me` | Header `Authorization: Bearer <access_token>`，返回当前用户信息 |
| POST | `/api/auth/logout` | Header `Authorization: Bearer <access_token>`，撤销当前会话 |
| GET | `/api` | 需要 Bearer access token 的示例受保护接口 |

登录和刷新返回的 `refresh_token` 为单次使用令牌；刷新成功后旧的 access/refresh token 会失效。

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
