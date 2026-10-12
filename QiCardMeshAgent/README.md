# QiCardMeshAgent

`QiCardMeshAgent` is the long-running local daemon that exposes PC/SC cards to
QiCardMesh peers over MQTT. It uses one Tokio runtime with separate blocking
workers for card I/O, a shared `CardSessionManager` for state, and one global
`ApduExecutor` mutex so APDUs are always serialized.

## Run

```powershell
cargo run -p qicardmesh-agent -- --config QiCardMeshAgent/config.yaml
```

The command above explicitly uses `QiCardMeshAgent/config.yaml` from the
repository root. Fill in its MQTT username and password. Without `--config`,
the daemon uses `config.yaml` beside the executable (for `cargo run` this is
normally `target/debug/config.yaml`, **not** the source directory). A missing
file is created there; empty credentials stop startup with the absolute path
in the error, so you can fill in the correct file and restart.

To verify TLS and authentication without subscribing or publishing:

```powershell
cargo run -p qicardmesh-agent -- --config QiCardMeshAgent/config.yaml --check-mqtt
```

Stop any running agent with the same client ID before this check: MQTT permits
one connection per client ID. A successful check confirms CONNECT acceptance,
not topic permissions. The endpoint is fixed to
`mqtts://mqtt.cyqi.top:8883` by the protocol. For a machine with PC/SC support,
build with `--features pcsc`; the default build is portable and reports no
cards until a backend is enabled.

The effective device identity is `<user_id>:<disk_fingerprint>` (with the
default prefix `user_id`). MQTT uses that exact value as `client_id` and
subscribes/publishes on:

* `QiCardMesh/device/<effective identity>/method`
* `QiCardMesh/device/<effective identity>/notify`

Both topics carry the protobuf messages in [`../protobuf/qicardmesh.proto`](../protobuf/qicardmesh.proto).
Requests and responses share the method topic and are correlated by
`request_id`.

## Operations

`LIST_CARDS` returns card summaries, `GET_CARD` returns one complete
`CardInfo`, and `EXECUTE_APDU` executes one serialized APDU and returns its
response data plus SW1/SW2. Notifications announce device online, card connect
and card disconnect events.

