# QiCardMeshAgent 守护进程

`QiCardMeshAgent` 可作为常驻进程运行。它通过操作系统的 PC/SC 服务维护本机
Reader/卡片连接，并通过 MQTT 将卡片能力提供给其他 QiCardMesh 设备。PC/SC
调用始终在卡片会话线程中串行执行；MQTT 网络线程只负责收发消息和转发工作，
因此并发请求不会同时占用同一张卡。

## 启动与配置

在可执行文件所在目录（也是服务的工作目录）首次执行 daemon 命令时，程序会
生成 `qicardmesh-agent.toml`。生成的文件只包含字段名和安全的空值，不会写入密码。
编辑后再次启动即可连接 MQTT：

```toml
# MQTT broker 地址。支持 mqtt://、mqtts://（TLS）、tcp:// 和 ssl://。
[mqtt]
url = "mqtts://broker.example.com:8883"
username = ""
password = ""
# 此电脑在 QiCardMesh 网络中的稳定 ID。建议使用服务端分配的 UUID。
user_id = "replace-me"
client_id = ""
keep_alive_secs = 30
qos = 1

# PC/SC 轮询和请求上限。
[agent]
poll_interval_ms = 250
max_apdu_bytes = 65544
max_commands_per_request = 32
```

配置文件缺失时 daemon 会创建模板并退出，并在日志中给出路径；这样可以避免
误以为已经上线。生产环境应限制配置文件权限，避免把 MQTT 密码暴露给其他
用户。`user_id` 不能包含 `/`、`+` 或 `#`，否则会被配置校验拒绝。

启动、前台运行和停止示例：

```sh
qicardmesh daemon                 # 读取当前目录的 qicardmesh-agent.toml
qicardmesh daemon --config ./agent.toml
# `run` 是 daemon 的兼容别名：qicardmesh run --config ./agent.toml
# Windows: Ctrl+C；Linux/macOS: SIGTERM 或 Ctrl+C
```

服务收到停止信号后先停止接收新的 MQTT 请求，再等待当前 APDU 返回，释放所有
PC/SC 连接，最后断开 MQTT。正在执行的 APDU 不会被自动重放。

## MQTT 主题与 protobuf

每个设备使用两个主题。`<userID>` 应使用稳定且不含 `/`、`+`、`#` 或 NUL
的字符串；实际值与配置中的 `user_id` 完全一致：

```
QiCardMesh/device/<userID>/method   # 请求和响应（protobuf 二进制）
QiCardMesh/device/<userID>/notify   # 设备/卡片事件（protobuf 二进制）
```

协议源文件位于 [`protocol/mqtt`](../protocol/mqtt)。构建时 `prost-build` 生成
Rust 类型；调用方不要把 protobuf 当作 JSON 或 UTF-8 文本。所有请求/响应都
带有 `request_id`，响应必须原样回填该 ID，便于请求端在共享主题上匹配响应。
建议 method 和 notify 使用 MQTT QoS 1；客户端应按 `request_id`/`event_id`
去重，因为重连或 broker 重投可能造成重复报文。
method 响应带有 `is_response=true`，订阅同一主题的 agent/client 必须忽略该标记，
避免 broker 回环时把响应当成新的请求。

### method 请求

`MethodRequest` 的 `method` 枚举定义以下行为：

| method | 请求字段 | 成功响应 |
| --- | --- | --- |
| `LIST_CARDS` | 可选 `include_empty_readers` | 当前 Reader 上的 `CardSummary` 列表（reader、卡片 ID、类型、ATR、连接状态） |
| `GET_CARD` | `card_id` | 指定卡的 `CardDetails`（摘要、PC/SC 协议、固件/序列号/OpenPGP AID；不可用字段为空） |
| `TRANSMIT_APDU` | `card_id`、按顺序排列的 APDU 字节串 | 与输入顺序一致的响应字节串及每条 APDU 的 `SW1/SW2` |

`TRANSMIT_APDU` 在 `ApduExecutor` 中按队列顺序执行；一张卡同一时刻最多
有一个事务。请求必须携带明确 `card_id`，当本机有多张卡时 daemon 不会猜测
目标。卡拔出、Reader 消失、PC/SC 服务重启或 APDU 传输失败时，当前连接被
释放，响应返回错误码；请求不会在卡重新插入后重放。

可解码但字段无效的 protobuf 请求、未知 method、未知 `card_id`、空 APDU 或超过
实现限制的批量请求会收到带错误码的 `MethodResponse`，MQTT 连接本身不会因此断开。
完全无法解码的 protobuf 没有可回填的 `request_id`，会被记录并丢弃。

### notify 事件

`Notify` 事件发布到 `/notify`：

| 事件 | 产生时机 |
| --- | --- |
| `DEVICE_ONLINE` | MQTT 连接和订阅完成后立即发布一次 |
| `CARD_CONNECTED` | Reader 中检测到卡并建立 PC/SC 会话后 |
| `CARD_CHANGED` | 同一 Reader 的卡片代际/ATR 发生变化，旧会话已释放并重新连接后 |
| `CARD_DISCONNECTED` | 卡拔出、Reader 消失、连接检查失败或 PC/SC 服务重启后 |
| `DEVICE_OFFLINE` | 进程开始关闭（若 broker 仍可达则尽力发布） |

每个事件含 `user_id`、`card_id`（设备事件可为空）、Reader 名、时间戳以及可选
卡片摘要。事件是幂等状态通知；接收端应以最新状态为准，并允许 MQTT 重连
后重复收到通知。daemon 使用稳定的 MQTT client ID，连接丢失后自动重连并重新
订阅；重连成功后再次发布 `DEVICE_ONLINE`，卡片状态也会重新扫描。

## 内部模块和线程模型

- **CardMonitor**：阻塞等待 PC/SC Reader 状态变化，发现插入、拔出、Reader
  消失和服务重启。事件通过线程安全 channel 发送，轮询等待有时间上限，便于
  处理停止信号和 MQTT 请求。
- **CardSessionManager**：维护 `card_id -> Reader/PCSC session` 映射，负责建立、
  检查、替换和释放连接。卡片代际或 ATR 变化会使旧会话失效。
- **ApduExecutor**：每张卡一个串行队列（或全局串行锁），验证 APDU 后在 PC/SC
  事务中按顺序发送；出错后丢弃本次连接，不自动重放。
- **MQTT worker**：解析 protobuf、校验 `request_id`/`card_id`，把请求放入
  `ApduExecutor` 或状态查询队列，并将响应发布回 `/method`。它不直接调用 PC/SC。

所有跨线程消息都使用拥有所有权的 Rust 值；关闭流程先停止 MQTT 请求入口，再
排空/取消队列，最后由 `CardSessionManager` 在所属线程释放 PC/SC 句柄，避免
拔卡后在 MQTT 回调线程中访问失效句柄。

## 验收与故障排查

以下测试不依赖硬件的部分可在 CI 中执行：

```sh
cargo fmt --check
cargo test --locked
cargo clippy --all-targets --locked -- -D warnings
```

手工 MQTT 验收需要一个可发布/订阅 protobuf 的 MQTT 客户端和 YubiKey、CanoKey
各一枚。建议按顺序记录 `/notify` 和 `/method` 报文：

1. 删除 `qicardmesh-agent.toml` 启动 daemon，确认只生成模板并退出；填入测试 broker
   和唯一 `user_id` 后启动，确认订阅 `/method` 并收到 `DEVICE_ONLINE`。
2. 插入 YubiKey，确认收到 `CARD_CONNECTED`，`LIST_CARDS` 返回 YubiKey、ATR
   和 reader 名；`GET_CARD` 能返回可用的版本/序列号字段。
3. 用 `TRANSMIT_APDU` 发送只读 SELECT/GET DATA，确认响应顺序和 `SW1/SW2`，
   再发送格式错误 APDU，确认仅该请求失败且 daemon 仍在线。
4. 拔出卡，确认收到 `CARD_DISCONNECTED`，进程不崩溃；拔出期间发送 APDU 应
   返回明确错误，不应重复执行。
5. 在相同 Reader 重新插入卡，确认收到 `CARD_CONNECTED` 或 `CARD_CHANGED`，
   后续 `TRANSMIT_APDU` 可成功；应用状态（SELECT）需要由调用方重新建立。
6. 对 CanoKey 重复步骤 2–5；随后停止/重启 pcscd（或 Windows Smart Card
   服务），确认收到断开事件，服务恢复后自动重新连接并发布新的卡片状态/连接通知。
7. 断开 MQTT broker 网络 30 秒后恢复，确认 MQTT 自动重连、重新订阅，且卡片
   会话仍由 PC/SC 线程安全维护；重连期间请求按错误响应处理，不阻塞监控线程。

若 `LIST_CARDS` 为空，先运行 `qicardmesh readers` 确认 Reader 已由 PC/SC 发现；
Linux 检查 `pcscd` 和 CCID 驱动，Windows 检查 Smart Card 服务，macOS 检查
系统 PC/SC framework。纯 FIDO/HID 模式不会出现在 PC/SC Reader 列表。

