# QiCardMesh Agent

跨平台 Rust PC/SC CLI，可枚举 Reader、读取 ATR 和卡片基本信息、识别 YubiKey / CanoKey、发送 APDU，并在监听期间自动释放失效连接及重新连接。

当前目录还包含可长期运行的 MQTT 守护进程。它由 `CardMonitor` 发现本地卡，
`CardSessionManager` 维护会话，`ApduExecutor` 在独占的 PC/SC worker 上串行执行
APDU，并通过 protobuf MQTT 主题向其他设备提供查询和卡片通讯：

```sh
qicardmesh daemon                 # 首次运行会生成 qicardmesh-agent.toml
qicardmesh daemon --config ./agent.toml
```

完整的配置、Protobuf 方法/通知、线程模型、重连和硬件验收流程见
[`docs/QiCardMeshAgent-daemon.md`](../docs/QiCardMeshAgent-daemon.md)。共享协议源在
[`protocol/mqtt`](../protocol/mqtt)，构建时通过 `prost` 生成 Rust 类型。

## 构建与运行

使用当前 Rust stable（最低 1.88），在本目录运行：

```sh
cargo build --release --locked
cargo install --path . --locked
qicardmesh readers
qicardmesh card-status
qicardmesh card-status --watch
qicardmesh transmit
```

也可用 `cargo run -- readers` 等命令直接运行；Windows 产物为 `target/release/qicardmesh.exe`。

| 平台 | 原生后端 | 环境要求 |
| --- | --- | --- |
| Windows | WinSCard.dll | Windows SDK / MSVC 构建工具；系统 Smart Card（SCardSvr）服务可用 |
| macOS | PCSC.framework | Xcode Command Line Tools；系统自带 PC/SC framework |
| Linux | pcsc-lite | 构建时需要 pkg-config、libpcsclite 开发包；运行时需要 pcscd 和 CCID 驱动 |

Debian / Ubuntu：

```sh
sudo apt install pkg-config libpcsclite-dev pcscd libccid
sudo systemctl enable --now pcscd.socket
```

只支持设备开放的 **CCID / PC/SC 接口**。纯 FIDO/HID 模式不会出现在 Reader 列表；设备需要启用智能卡功能。

## 命令

### Reader 与卡片信息

```sh
qicardmesh readers
qicardmesh card-status --reader CanoKey
qicardmesh card-status --watch --reader YubiKey
```

`readers` 仅枚举，不连接、不发送 APDU。`card-status` 默认执行一次快照；`--watch` 持续监听，Ctrl+C 退出。`--reader` 优先匹配完整 Reader 名称，否则按不区分大小写的子串匹配。

输出包括 Reader 名称、PC/SC 状态、ATR、设备类型、识别依据、协商协议，以及可读取的固件版本、序列号和 OpenPGP AID。固件/应用不支持的信息显示 `unavailable`；不认识的设备显示 `Unknown`，仍可连接和发送 APDU。

识别使用 Reader 名称 / ATR 的品牌提示，以及 OpenPGP AID 中的厂商标识（Yubico `0006`、CanoKey `F1D0`）。品牌提示会标注为 hint；这些标识用于兼容性判断，不提供硬件真伪认证。通用 NFC Reader 上若 OpenPGP 不可用且 ATR 没有品牌信息，可能无法确定型号。

元信息查询只使用 SELECT 和无需 PIN 的读取指令。CanoKey 从 Admin 应用读取版本、序列号；YubiKey 从 PIV 扩展读取版本、序列号，旧设备或禁用应用允许缺项。查询在短事务内执行，会改变所选 applet；退出事务时使用 `LeaveCard`。OpenPGP/CanoKey 序列号显示十六进制，YubiKey PIV 序列号显示十进制。

### APDU

单条 APDU，或将多个带引号的 APDU 放在同一事务中依次发送：

```sh
# 选择 OpenPGP 并读取 AID（适用于启用了 OpenPGP 的卡）
qicardmesh transmit --reader CanoKey "00 A4 04 00 06 D2 76 00 01 24 01" "00 CA 00 4F 00"

# CanoKey：选择 Admin 应用并读取固件版本
qicardmesh transmit --reader CanoKey "00A4040005F000000000" "0031000000"

# YubiKey：选择 PIV 应用并读取版本
qicardmesh transmit --reader YubiKey "00A4040005A000000308" "00FD0000"
```

不带 APDU 参数时进入交互模式，从 stdin 每行读取一条十六进制 APDU：

```text
qicardmesh transmit --reader CanoKey
00 A4 04 00 05 F0 00 00 00 00
00 31 00 00 00
quit
```

- 支持连续十六进制、空白或冒号分隔，校验短 / 扩展 APDU 的 Lc/Le。扩展 APDU 是否可用仍取决于卡片、驱动和传输协议。
- 输出原始响应、数据与 `SW1/SW2`。用户 APDU 原样发送一次；`61xx` / `6Cxx` 也原样返回，由调用者决定 GET RESPONSE 或修正 Le。仅内部只读元信息查询自动处理有上限的响应续读。
- 批量命令遇到第一个非 `9000` 停止，返回该响应；单次模式退出码：`0` 成功、`1` 参数/连接/传输错误、`2` 卡片返回非 `9000`（CLI 参数解析错误也由 clap 返回 `2`）。交互模式中的单条错误打印后继续运行。
- 多张卡匹配时拒绝发送；用 `--reader` 指定完整名称。连接不执行元信息探测，避免在用户 APDU 之间切换 applet。
- 每条交互命令有独立事务。需要 SELECT + 后续操作的原子性时用批量参数；其他应用可能在交互命令之间切换 applet。
- 交互模式同时监听插拔，卡拔出后释放连接，插回后重新连接。重连后需重新 SELECT / 建立应用状态。传输错误不自动重放，未发送的批量命令也不会在插回后补发。
- 不要同时运行 `card-status --watch` 和需要保持 applet 状态的交互会话；信息探测可能改变同一张卡的应用状态。

## 插拔与恢复

使用 `SCardGetStatusChange` 监听已有 Reader，每次等待最长 250 ms，并重新枚举 Reader，以兼容整个 USB 密钥消失以及 macOS 不支持 PnP 伪 Reader 的情况。无 Reader 时有同样的等待，避免空转。卡事件计数和 ATR 用于发现两次采样之间的快速拔插；已有连接也通过 `SCardStatus` 检查。

移除、Reader 消失、传输失败、服务断开时释放连接。监听 / 交互模式会对忙卡重试连接，PC/SC 服务重启后重建 Context。一次性命令遇到错误退出。普通结束、Ctrl+C 和 stdin EOF 会清理持有的连接。

250 ms 是状态监听的等待上限，不是 APDU 超时；事务获取和 APDU 的阻塞时间由平台驱动、其他应用及卡片控制。Ctrl+C 会在当前原生调用返回后退出。

## 验证

```sh
cargo fmt --check
cargo test --locked
cargo clippy --all-targets --locked -- -D warnings
```

自动化测试不依赖硬件，包括两种设备识别、通用 Reader 厂商标识识别、APDU 格式/响应续读，以及使用可注入后端验证的拔卡释放、USB Reader 消失、快速拔插、未上电卡连接、忙卡重试、卡重置、服务重启和发送中拔卡不重放。

实机验收步骤（YubiKey / CanoKey、每个平台分别执行）：

1. `readers` 能列出设备；`card-status` 显示对应类型、ATR 和协商协议。
2. 启动 `card-status --watch`，拔掉设备，应出现断开 / Reader 移除信息且进程保持运行；重新插入应再次出现 `connected`，无需重启。
3. 启动交互 `transmit`，发送上面的 SELECT / 读取指令；拔出再插入后重新发送 SELECT / 读取，应该恢复。
4. 在发送期间拔卡，允许返回明确的传输错误；进程应继续运行，旧命令不自动重发。重复拔插验证。

本次 Windows 本地验证实际连接的是 `MeXdiy MeXkey3 0`：成功获取 ATR、T=1 协议和 OpenPGP AID，批量及交互 APDU 返回 `9000`。该设备厂商标识为 `FFFE`，正确保留为 `Unknown`。YubiKey / CanoKey 的实物识别与物理拔插，以及 macOS / Linux 实机验收仍需对应硬件环境；模拟测试不替代实机验收。

## 参考

- [pcsc Rust API 与平台映射](https://docs.rs/pcsc/2.9.0/pcsc/)
- [CanoKey OpenPGP](https://docs.canokeys.org/development/protocols/openpgp/)
- [CanoKey Admin 只读信息命令](https://docs.canokeys.org/development/protocols/admin/)
- [Yubico PIV 扩展与 ATR](https://developers.yubico.com/PIV/Introduction/Yubico_extensions.html)
