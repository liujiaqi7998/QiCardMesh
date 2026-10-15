# API 登录设备管理

每个用户与 `X-Device-ID` 的组合对应一条 `qicardmesh_logindevice` 记录和一组有效的 access/refresh token。该表仅由 API 登录维护。Django `/admin/` 的 Cookie 登录不要求设备 ID，也不会产生设备记录。

## 登录与设备标识

客户端首次安装或创建客户端实例时生成 UUID，并在本地持久保存。每次调用 `/api/auth/login`，无论密码登录还是邮箱验证码登录，都必须发送：

```http
X-Device-ID: 96ec772a-3661-47c4-b5b3-040bfe8e1118
X-Platform: android
```

`X-Device-ID` 大小写敏感，去除首尾空格后长度为 1–128，只允许字母、数字、`.`、`_`、`:`、`-`。缺失或非法返回 HTTP 400 / 业务码 `40006`，且不消耗邮箱验证码。`X-Platform` 可选，最多 64 个字符，不能包含控制字符；非法时返回 `40002`。平台和设备 ID 都是客户端声明的信息，不能用作身份认证或硬件真实性证明。

服务端通过 `config.context` 读取设备、平台和 `get_client_ip()` 解析的登录 IP。登录成功才写表；失败登录不新增记录。登录响应仍只包含 token 信息。用户再次以相同设备 ID 登录，会保留记录 ID 和首次创建时间，更新登录信息并替换旧 token。不同设备、不同用户之间互不影响。

## 查看设备

```http
GET /api/auth/devices?page=1&page_size=20
Authorization: Bearer access_...
```

只返回当前用户 `expires_at` 尚未到期的设备。`page_size` 范围 1–100，默认 20；按最近活跃时间降序排列，并以记录 ID 排序消除并列歧义。

```json
{
  "code": 200,
  "message": "成功",
  "data": {
    "total": 1,
    "page": 1,
    "page_size": 20,
    "items": [{
      "id": "4a794c36-ea84-4a73-8f6a-9356d9f439d1",
      "device_id": "96ec772a-3661-47c4-b5b3-040bfe8e1118",
      "platform": "android",
      "client_ip": "192.0.2.1",
      "created_at": "2026-10-15T03:00:00Z",
      "login_at": "2026-10-15T03:00:00Z",
      "last_seen_at": "2026-10-15T03:01:00Z",
      "expires_at": "2026-11-14T03:00:00Z",
      "is_current": true
    }]
  }
}
```

`id` 是服务端设备记录 UUID，用于删除；`device_id` 是客户端提交的标识。`is_current` 根据经过认证的 token 判断，不能通过请求头伪造。`client_ip` 是最近一次登录的 IP；`last_seen_at` 在有效 API/MQTT 鉴权时最多每 60 秒写入一次。它表示最近活动，不表示实时在线状态。Redis 被清空时 token 无法使用，表中记录仍可能保留到过期或重新登录。

## 踢出设备

```http
DELETE /api/auth/devices/4a794c36-ea84-4a73-8f6a-9356d9f439d1
Authorization: Bearer access_...
```

无请求体。返回 `{"code":200,"message":"设备已退出登录","data":null}`。仅可删除自己的记录；目标不存在、已被删除或属于其他用户时统一返回 HTTP 404 / `40401`。支持删除当前设备；删除成功的响应返回后，该设备需要重新登录。`POST /api/auth/logout` 也会删除当前设备记录。

删除记录后，旧 access token 返回 `40102`，旧 refresh token 返回 `40104`，MQTT HTTP 鉴权返回 `deny`。已经通过认证并开始执行的请求不会被中途取消。已建立的 MQTT 长连接无法仅靠 HTTP 鉴权接口主动断开；如需立刻断开连接，还需接入 broker 的连接管理 API，并建立设备与 MQTT client ID 的映射。当前实现覆盖后续 MQTT 鉴权请求。

## 存储与并发

数据库保存用户、设备标识、平台、IP、时间戳，以及当前会话代次和 token 的 HMAC 摘要，不保存原始 token。`(user, device_id)` 有唯一约束；`session_id` 每次登录和刷新都会更换。

Redis 存储 token 对应的会话缓存，TTL 保持 access 7 天、refresh 30 天的现有配置。每次 Bearer/MQTT 鉴权和刷新都会核对数据库中的会话代次、摘要和有效期。数据库记录是撤销依据，因此旧 Redis 缓存不能绕过踢出、退出或重新登录。

刷新只条件更新原记录，不新增记录。同一 refresh token 并发请求时，只有匹配旧代次的更新能成功。刷新后旧 token 对失效，新 refresh 有效期从刷新时重新计算；设备登录时间、平台、IP不因刷新而改变。若删除先成功，后续刷新不能重新创建记录；若刷新先成功，删除仍会移除同一记录。延迟到达的旧 logout 不会删除重新登录后的会话。

登录/刷新时，数据库更新和 Redis 写入位于数据库事务内，Redis 写失败会回滚数据库更新并尝试清理未发出的 token。跨数据库与 Redis 不具备分布式事务保证，进程崩溃或缓存丢失可能要求重新登录；任何孤立缓存都不能单独通过鉴权。已撤销 token 的 Redis 清理采用尽力执行，失败时由 TTL 回收，不影响数据库撤销。Redis 不可用返回 `50302`；数据库不可用返回 `50303`；MQTT 均拒绝鉴权。

不要求普通 API 请求重复携带设备 ID；设备身份以 Bearer token 的服务器绑定为准。仅持有设备 ID 或 session Cookie 不能访问设备列表或删除设备。

## 部署与清理

先发布支持 `X-Device-ID` 的客户端，再停止旧服务进程、部署新代码并运行：

```console
python manage.py migrate
python manage.py test
```

完成迁移后启动所有服务进程。不要保留不检查设备记录的旧版本进程。历史 token 没有设备绑定记录，升级后全部需要重新登录；不会把它们自动绑定到任意客户端。已有 Django session 不因此被清理。

数据库过期记录不再通过鉴权，也不出现在设备列表中。可定期运行以下命令回收记录（不会删除有效设备，也不创建系统定时任务）：

```console
python manage.py prune_login_devices
```

Django 管理后台提供只读的「API 登录设备」列表；具有相应删除权限的管理员可删除设备记录以撤销 API 会话。该管理操作不会将管理员的网页登录记录为 API 设备。
