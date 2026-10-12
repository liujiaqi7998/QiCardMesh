# QiCardMesh MQTT protobuf 协议

本目录是二进制 MQTT 协议的权威定义。`qicardmesh-mqtt-protocol` crate 在编译
时运行 `prost-build`，并使用随 crate 提供的 `protoc`，因此构建不依赖系统安装
的协议编译器。Agent 和所有客户端都应依赖这个 crate，不要复制生成的 Rust 代码。

## 主题和通信方向

当配置的用户 ID 为 `alice` 时，主题如下：

* `QiCardMesh/device/alice/method` — method 请求和响应。客户端发布
  `MethodRequest`，Agent 在同一主题发布 `MethodResponse`；响应会原样回填
  `request_id` 以便关联。`MethodResponse` 带有 `is_response=true`；如果 broker
  将响应回送到订阅端，Agent 应忽略该标记，避免把响应再次当成请求。
* `QiCardMesh/device/alice/notify` — Agent 发给客户端的 `Notify` 事件。MQTT
  连接建立后立即发布 `DEVICE_ONLINE`，随后发布卡片连接、变化和断开事件；正常
  关闭时可能发布 `DEVICE_OFFLINE`。

消息负载使用 `prost::Message::encode_to_vec()` 编码得到的二进制字节。method 请求
和通知建议使用 MQTT QoS 1；是否将 online 通知设置为 retained 由部署策略决定。

## 方法

`LIST_CARDS` 返回 `CardSummary` 条目（阅读器、ATR、卡片类型、插入状态和连接
状态）。`GET_CARD` 返回指定 `card_id` 的 `CardDetails`。`TRANSMIT_APDU` 接收
一个或多个 APDU 命令字节串。Agent 的 `ApduExecutor` 按卡片串行化调用，在第一
个非 `9000` 状态字后停止批量执行，并返回实际已经执行的响应。传输或拔卡错误后
不会重放命令。

成功时 `MethodResponse.status` 为 `STATUS_OK`；失败时返回对应枚举值和可读的
`error_message`。空或未知的卡片 ID 返回 `INVALID_ARGUMENT`/`NOT_FOUND`；执行
期间卡片被拔出返回 `CARD_UNAVAILABLE`。

## 重新生成和检查

在本目录运行：

```text
cargo test
```

生成的文件放在 Cargo 的 `OUT_DIR` 中，不提交到版本库。修改 `qicardmesh.proto`
后运行测试，并提交 `.proto` 文件和本 README。
