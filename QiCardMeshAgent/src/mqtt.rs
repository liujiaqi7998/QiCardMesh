//! MQTT transport for the long-running agent.
//!
//! One thread owns the MQTT event loop and invokes the cloneable card service
//! facade. PC/SC handles stay on the card-monitor thread; a malformed or slow
//! MQTT request therefore cannot race a card removal or issue a second APDU.

use crate::{
    apdu,
    config::Config,
    daemon::{ApduExecutor, CardEvent, CardMonitor, CardSessionManager, CardSummary},
};
use anyhow::{Context, Result, bail};
use prost::Message;
use qicardmesh_mqtt_protocol::{PROTOCOL_VERSION, generated::*, method_topic, notify_topic};
use rumqttc::{Client, Connection, Event, Incoming, MqttOptions, QoS};
use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

pub fn run(config: Config, shutdown: Arc<AtomicBool>) -> Result<()> {
    let monitor = CardMonitor::start(crate::daemon::CardMonitorConfig {
        poll_interval: Duration::from_millis(config.agent.poll_interval_ms.max(10)),
        reader: None,
        inspect: true,
    });
    let sessions = monitor.sessions();
    let executor = monitor.apdu_executor();
    let result = run_mqtt(&config, &shutdown, &monitor, sessions, executor);
    monitor.shutdown();
    result
}

fn run_mqtt(
    config: &Config,
    shutdown: &Arc<AtomicBool>,
    monitor: &CardMonitor,
    sessions: CardSessionManager,
    executor: ApduExecutor,
) -> Result<()> {
    let method = method_topic(&config.mqtt.user_id);
    let notify = notify_topic(&config.mqtt.user_id);
    let client_id = if config.mqtt.client_id.trim().is_empty() {
        format!("qicardmesh-agent-{}", config.mqtt.user_id)
    } else {
        config.mqtt.client_id.clone()
    };
    let qos = match config.mqtt.qos {
        0 => QoS::AtMostOnce,
        1 => QoS::AtLeastOnce,
        _ => QoS::ExactlyOnce,
    };
    let mut backoff = Duration::from_secs(1);
    while shutdown.load(Ordering::Relaxed) {
        // rumqttc's URL parser requires a client_id query parameter even when
        // it is set through the API afterwards. Add one without changing the
        // user's broker scheme, host, port, or TLS selection.
        let mut broker_url = url::Url::parse(&config.mqtt.url)
            .map_err(|e| anyhow::anyhow!("invalid MQTT URL: {e}"))?;
        if !broker_url.query_pairs().any(|(key, _)| key == "client_id") {
            broker_url
                .query_pairs_mut()
                .append_pair("client_id", &client_id);
        }
        let mut options = MqttOptions::parse_url(broker_url.as_str())
            .map_err(|e| anyhow::anyhow!("invalid MQTT URL: {e}"))?;
        options.set_client_id(client_id.clone());
        options.set_keep_alive(Duration::from_secs(config.mqtt.keep_alive_secs.max(1)));
        let packet_limit = 10
            * 1024usize
                .max(
                    config
                        .agent
                        .max_apdu_bytes
                        .saturating_mul(config.agent.max_commands_per_request)
                        .saturating_add(4096),
                )
                .min(16 * 1024 * 1024);
        options.set_max_packet_size(packet_limit, packet_limit);
        if !config.mqtt.username.is_empty() {
            options.set_credentials(config.mqtt.username.clone(), config.mqtt.password.clone());
        }
        let (client, mut connection) = Client::new(options, 64);
        // `Client::new` only creates a local event loop.  Wait for the broker's
        // CONNACK before advertising DEVICE_ONLINE; otherwise a refused or
        // unreachable broker would produce a false online notification.
        match wait_for_connack(&mut connection, shutdown) {
            Ok(true) => {}
            Ok(false) => break,
            Err(error) => {
                eprintln!(
                    "MQTT connection failed: {error}; retrying in {}s",
                    backoff.as_secs()
                );
                thread::sleep(backoff);
                backoff = (backoff * 2).min(Duration::from_secs(30));
                continue;
            }
        }
        if let Err(error) = client.subscribe(method.clone(), qos) {
            eprintln!(
                "MQTT subscribe failed: {error}; retrying in {}s",
                backoff.as_secs()
            );
            thread::sleep(backoff);
            backoff = (backoff * 2).min(Duration::from_secs(30));
            continue;
        }
        let _ = publish_notify(&client, &notify, qos, online_notification(config));
        backoff = Duration::from_secs(1);
        if let Err(error) = mqtt_loop(
            config,
            shutdown,
            monitor,
            &client,
            &mut connection,
            &method,
            &notify,
            qos,
            &sessions,
            &executor,
        ) {
            if shutdown.load(Ordering::Relaxed) {
                break;
            }
            eprintln!(
                "MQTT connection stopped: {error}; retrying in {}s",
                backoff.as_secs()
            );
            thread::sleep(backoff);
            backoff = (backoff * 2).min(Duration::from_secs(30));
        }
    }
    Ok(())
}

fn wait_for_connack(connection: &mut Connection, shutdown: &Arc<AtomicBool>) -> Result<bool> {
    while shutdown.load(Ordering::Relaxed) {
        match connection.recv_timeout(Duration::from_millis(100)) {
            Ok(Ok(Event::Incoming(Incoming::ConnAck(_)))) => return Ok(true),
            Ok(Ok(_)) => {}
            Ok(Err(error)) => bail!("{error}"),
            Err(rumqttc::RecvTimeoutError::Timeout) => {}
            Err(rumqttc::RecvTimeoutError::Disconnected) => {
                bail!("MQTT event loop disconnected")
            }
        }
    }
    Ok(false)
}

#[allow(clippy::too_many_arguments)]
fn mqtt_loop(
    config: &Config,
    shutdown: &Arc<AtomicBool>,
    monitor: &CardMonitor,
    client: &Client,
    connection: &mut Connection,
    method: &str,
    notify: &str,
    qos: QoS,
    sessions: &CardSessionManager,
    executor: &ApduExecutor,
) -> Result<()> {
    while shutdown.load(Ordering::Relaxed) {
        for event in monitor.try_events() {
            publish_event(client, notify, qos, config, event)?;
        }
        match connection.recv_timeout(Duration::from_millis(100)) {
            Ok(Ok(Event::Incoming(Incoming::Publish(publish)))) => {
                if publish.topic == method
                    && let Some(response) =
                        handle_request(config, sessions, executor, &publish.payload)
                {
                    client
                        .publish(method, qos, false, response.encode_to_vec())
                        .context("publish MQTT method response")?;
                }
            }
            Ok(Ok(_)) => {}
            Ok(Err(error)) => bail!("{error}"),
            Err(rumqttc::RecvTimeoutError::Timeout) => {}
            Err(rumqttc::RecvTimeoutError::Disconnected) => bail!("MQTT event loop disconnected"),
        }
    }
    let _ = publish_notify(
        client,
        notify,
        qos,
        offline_notification(config, "daemon stopped"),
    );
    Ok(())
}

fn handle_request(
    config: &Config,
    sessions: &CardSessionManager,
    executor: &ApduExecutor,
    payload: &[u8],
) -> Option<MethodResponse> {
    let request = match MethodRequest::decode(payload) {
        Ok(request) => request,
        Err(error) => {
            eprintln!("ignoring malformed MQTT protobuf request: {error}");
            return None;
        }
    };
    // Requests and responses share the method topic. Never execute a response
    // echoed by a broker or another subscriber as a fresh card command.
    if request.is_response {
        return None;
    }
    let mut response = MethodResponse {
        protocol_version: PROTOCOL_VERSION,
        request_id: request.request_id.clone(),
        method: request.method,
        status: StatusCode::StatusOk as i32,
        error_message: String::new(),
        is_response: true,
        payload: None,
    };
    if request.protocol_version != PROTOCOL_VERSION || request.request_id.is_empty() {
        response.status = StatusCode::InvalidArgument as i32;
        response.error_message = "protocol_version or request_id is invalid".into();
        return Some(response);
    }
    let payload_matches_method = matches!(
        (&request.payload, request.method()),
        (
            Some(method_request::Payload::ListCards(_)),
            Method::ListCards
        ) | (Some(method_request::Payload::GetCard(_)), Method::GetCard)
            | (
                Some(method_request::Payload::TransmitApdu(_)),
                Method::TransmitApdu
            )
    );
    if !payload_matches_method {
        response.status = StatusCode::InvalidArgument as i32;
        response.error_message = "method does not match protobuf payload".into();
        return Some(response);
    }
    if request.payload.is_none() {
        response.status = StatusCode::InvalidArgument as i32;
        response.error_message = "request payload is missing".into();
        return Some(response);
    }
    let result = match request.payload {
        Some(method_request::Payload::ListCards(request)) => {
            list_cards(sessions, request.include_empty_readers)
                .map(|cards| method_response::Payload::ListCards(ListCardsResponse { cards }))
        }
        Some(method_request::Payload::GetCard(request)) => get_card(sessions, &request.card_id)
            .map(|card| method_response::Payload::GetCard(GetCardResponse { card: Some(card) })),
        Some(method_request::Payload::TransmitApdu(request)) => {
            transmit(config, executor, &request)
        }
        None => Err(anyhow::anyhow!("request payload is missing")),
    };
    match result {
        Ok(payload) => response.payload = Some(payload),
        Err(error) => {
            response.status = status_for_error(&error) as i32;
            response.error_message = error.to_string();
        }
    }
    Some(response)
}

fn list_cards(
    sessions: &CardSessionManager,
    include_empty: bool,
) -> Result<Vec<qicardmesh_mqtt_protocol::generated::CardSummary>> {
    Ok(sessions
        .list_cards()?
        .into_iter()
        .filter(|card| include_empty || card.present)
        .map(proto_summary)
        .collect())
}

fn get_card(
    sessions: &CardSessionManager,
    card_id: &str,
) -> Result<qicardmesh_mqtt_protocol::generated::CardDetails> {
    if card_id.is_empty() {
        bail!("card_id is required");
    }
    let card = sessions
        .get_card(Some(card_id))?
        .ok_or_else(|| anyhow::anyhow!("card not found: {card_id}"))?;
    let summary = proto_summary(card.clone());
    let version = card
        .info
        .as_ref()
        .and_then(|info| info.version.clone())
        .unwrap_or_default();
    let openpgp_aid = card
        .info
        .as_ref()
        .and_then(|info| info.openpgp_aid.clone())
        .unwrap_or_default();
    let mut attributes = HashMap::new();
    if let Some(description) = card.description {
        attributes.insert("description".into(), description);
    }
    let vendor = card
        .info
        .as_ref()
        .map(|info| info.kind.to_string())
        .unwrap_or_else(|| card.device.clone());
    Ok(qicardmesh_mqtt_protocol::generated::CardDetails {
        summary: Some(summary),
        vendor,
        model: String::new(),
        version,
        openpgp_aid,
        attributes,
    })
}

fn transmit(
    config: &Config,
    executor: &ApduExecutor,
    request: &TransmitApduRequest,
) -> Result<method_response::Payload> {
    if request.card_id.is_empty() {
        bail!("card_id is required for APDU execution");
    }
    if request.commands.is_empty() {
        bail!("at least one APDU command is required");
    }
    if request.commands.len() > config.agent.max_commands_per_request {
        bail!("too many APDU commands in one request");
    }
    for command in &request.commands {
        if command.len() > config.agent.max_apdu_bytes || command.len() < 4 {
            bail!("APDU command length is outside configured limits");
        }
        apdu::parse(&apdu::hex(command)).context("invalid APDU")?;
    }
    let responses = executor.execute(Some(request.card_id.as_str()), request.commands.clone())?;
    let mut results = Vec::with_capacity(responses.len());
    let mut complete = true;
    for response in responses {
        let status_word = apdu::split_response(&response)
            .map(|(_, sw)| sw)
            .unwrap_or(0);
        if status_word != 0x9000 {
            complete = false;
        }
        results.push(ApduResult {
            response,
            status_word: status_word as u32,
            error_message: String::new(),
        });
    }
    Ok(method_response::Payload::TransmitApdu(
        TransmitApduResponse {
            responses: results,
            transaction_complete: complete,
        },
    ))
}

fn status_for_error(error: &anyhow::Error) -> StatusCode {
    let text = error.to_string().to_ascii_lowercase();
    if text.contains("invalid")
        || text.contains("required")
        || text.contains("too many")
        || text.contains("length")
    {
        StatusCode::InvalidArgument
    } else if text.contains("not found") {
        StatusCode::NotFound
    } else if text.contains("no card")
        || text.contains("smartcard")
        || text.contains("not present")
        || text.contains("unavailable")
        || text.contains("removed")
        || text.contains("disconnected")
        || text.contains("connection released")
    {
        StatusCode::CardUnavailable
    } else if text.contains("apdu") || text.contains("status") {
        StatusCode::ApduError
    } else if text.contains("pc/sc") || text.contains("pcsc") {
        StatusCode::PcscError
    } else {
        StatusCode::InternalError
    }
}

fn proto_summary(card: CardSummary) -> qicardmesh_mqtt_protocol::generated::CardSummary {
    let detected_kind = card
        .info
        .as_ref()
        .map(|info| info.kind.to_string())
        .unwrap_or_else(|| card.device.clone());
    let kind = match detected_kind.to_ascii_lowercase().as_str() {
        "yubikey" => CardKind::Yubikey,
        "canokey" => CardKind::Canokey,
        "unknown" => CardKind::Unknown,
        _ => CardKind::SmartCard,
    };
    let protocol = card.protocol.clone().unwrap_or_default();
    let firmware = card
        .info
        .as_ref()
        .and_then(|info| info.version.clone())
        .unwrap_or_default();
    let serial = card
        .info
        .as_ref()
        .and_then(|info| info.serial.clone())
        .unwrap_or_default();
    qicardmesh_mqtt_protocol::generated::CardSummary {
        card_id: card.reader.clone(),
        reader_name: card.reader,
        kind: kind as i32,
        atr: card.atr,
        present: card.present,
        connected: card.connected,
        generation: card.generation,
        protocol,
        firmware,
        serial,
    }
}

fn publish_event(
    client: &Client,
    topic: &str,
    qos: QoS,
    config: &Config,
    event: CardEvent,
) -> Result<()> {
    let (kind, reason, card) = match event {
        CardEvent::CardConnected(card) => (
            NotifyEvent::CardConnected,
            "card connected".to_string(),
            Some(proto_summary(card)),
        ),
        CardEvent::CardDisconnected { reader, reason } => (
            NotifyEvent::CardDisconnected,
            reason,
            Some(proto_summary(CardSummary {
                reader,
                generation: 0,
                present: false,
                connected: false,
                atr: Vec::new(),
                device: "Unknown".into(),
                description: None,
                protocol: None,
                info: None,
            })),
        ),
        CardEvent::ReaderState(card) => (
            NotifyEvent::CardChanged,
            "reader state changed".into(),
            Some(proto_summary(card)),
        ),
        CardEvent::ServiceUnavailable(reason) => (NotifyEvent::CardChanged, reason, None),
        CardEvent::ServiceRecovered => (
            NotifyEvent::CardChanged,
            "PC/SC service recovered".into(),
            None,
        ),
    };
    let message = Notify {
        protocol_version: PROTOCOL_VERSION,
        event_id: Uuid::new_v4().to_string(),
        event: kind as i32,
        timestamp_unix_ms: now_ms(),
        user_id: config.mqtt.user_id.clone(),
        reason,
        payload: card.map(notify::Payload::Card),
    };
    publish_notify(client, topic, qos, message)
}

fn online_notification(config: &Config) -> Notify {
    Notify {
        protocol_version: PROTOCOL_VERSION,
        event_id: Uuid::new_v4().to_string(),
        event: NotifyEvent::DeviceOnline as i32,
        timestamp_unix_ms: now_ms(),
        user_id: config.mqtt.user_id.clone(),
        reason: "MQTT connected".into(),
        payload: Some(notify::Payload::Device(DeviceInfo {
            agent_version: env!("CARGO_PKG_VERSION").into(),
            hostname: hostname(),
            platform: std::env::consts::OS.into(),
        })),
    }
}

fn offline_notification(config: &Config, reason: &str) -> Notify {
    Notify {
        protocol_version: PROTOCOL_VERSION,
        event_id: Uuid::new_v4().to_string(),
        event: NotifyEvent::DeviceOffline as i32,
        timestamp_unix_ms: now_ms(),
        user_id: config.mqtt.user_id.clone(),
        reason: reason.into(),
        payload: None,
    }
}
fn publish_notify(client: &Client, topic: &str, qos: QoS, message: Notify) -> Result<()> {
    client
        .publish(topic, qos, false, message.encode_to_vec())
        .context("publish MQTT notification")?;
    Ok(())
}
fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}
fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "unknown".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AgentConfig, MqttConfig};
    #[test]
    fn online_event_contains_device_info() {
        let config = Config {
            mqtt: MqttConfig {
                url: "mqtt://localhost".into(),
                username: String::new(),
                password: String::new(),
                user_id: "alice".into(),
                client_id: String::new(),
                keep_alive_secs: 30,
                qos: 1,
            },
            agent: AgentConfig::default(),
        };
        let event = online_notification(&config);
        assert_eq!(
            NotifyEvent::try_from(event.event).unwrap(),
            NotifyEvent::DeviceOnline
        );
        assert!(matches!(event.payload, Some(notify::Payload::Device(_))));
    }
}
