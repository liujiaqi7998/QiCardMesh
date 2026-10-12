use crate::{
    cards::{ApduExecutor, CardError, CardEvent, CardSessionManager},
    config::{Config, DEFAULT_MQTT_URL},
    proto,
};
use anyhow::{Context, Result};
use prost::Message;
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS, Transport};
use std::time::Duration;
use tokio::{sync::mpsc, time::sleep};
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

fn connection_options(config: &Config, client_id: &str) -> Result<MqttOptions> {
    config.validate_credentials()?;
    let mut options = MqttOptions::new(client_id, "mqtt.cyqi.top", 8883);
    options.set_keep_alive(Duration::from_secs(30));
    options.set_credentials(&config.mqtt.username, &config.mqtt.password);
    options.set_transport(Transport::tls_with_default_config());
    Ok(options)
}

/// Exercise the same CONNECT parameters as the daemon without business traffic.
pub async fn check_connection(config: &Config, client_id: &str) -> Result<()> {
    let options = connection_options(config, client_id)?;
    let (_client, mut eventloop) = AsyncClient::new(options, 8);
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            if matches!(eventloop.poll().await?, Event::Incoming(Packet::ConnAck(_))) {
                return Ok::<(), rumqttc::ConnectionError>(());
            }
        }
    })
    .await
    .context("MQTT connection timed out after 15 seconds")?
    .context("broker connection/authentication failed")
}

pub fn method_topic(user_id: &str) -> String {
    format!("QiCardMesh/device/{user_id}/method")
}
pub fn notify_topic(user_id: &str) -> String {
    format!("QiCardMesh/device/{user_id}/notify")
}

pub struct MqttService {
    config: Config,
    user_id: String,
    client_id: String,
    sessions: CardSessionManager,
    executor: ApduExecutor,
}

impl MqttService {
    pub fn new(
        config: Config,
        user_id: String,
        client_id: String,
        sessions: CardSessionManager,
        executor: ApduExecutor,
    ) -> Self {
        Self {
            config,
            user_id,
            client_id,
            sessions,
            executor,
        }
    }

    pub async fn run(
        self,
        mut card_events: mpsc::Receiver<CardEvent>,
        cancel: CancellationToken,
    ) -> Result<()> {
        let (client, mut eventloop) = self.connect().await?;
        let methods = method_topic(&self.user_id);
        let notify = notify_topic(&self.user_id);
        client
            .subscribe(&methods, QoS::AtLeastOnce)
            .await
            .context("subscribe method topic")?;
        let mut online = false;
        loop {
            tokio::select! {
                _ = cancel.cancelled() => {
                    if online { let _ = self.publish_notify(&client, &notify, proto::NotifyEvent::DeviceOffline, None, "daemon stopping").await; }
                    break;
                }
                event = eventloop.poll() => {
                    match event {
                        Ok(Event::Incoming(Packet::ConnAck(_))) => {
                            if !online {
                                online = true;
                                info!(client_id = %self.client_id, "MQTT connected");
                                self.publish_notify(&client, &notify, proto::NotifyEvent::DeviceOnline, None, "mqtt connected").await?;
                                // Replay the current card snapshot after every
                                // reconnect so events observed while MQTT was
                                // unavailable are not lost.
                                for card in self.sessions.list().await {
                                    self.publish_notify(&client, &notify, proto::NotifyEvent::CardConnected, Some(card), "card snapshot").await?;
                                }
                            }
                        }
                        Ok(Event::Incoming(Packet::Publish(publish))) => {
                            if publish.topic == methods {
                                self.handle_method(&client, &methods, &publish.payload).await;
                            }
                        }
                        Ok(_) => {}
                        Err(error) => {
                            online = false;
                            warn!(%error, "MQTT event loop error; rumqttc will reconnect");
                            // Avoid a tight loop when DNS or credentials are unavailable.
                            sleep(Duration::from_millis(250)).await;
                        }
                    }
                }
                maybe_event = card_events.recv() => {
                    let Some(event) = maybe_event else { break; };
                    let (kind, card) = match event {
                        CardEvent::Connected(card) => (proto::NotifyEvent::CardConnected, Some(card)),
                        CardEvent::Disconnected(card) => (proto::NotifyEvent::CardDisconnected, Some(card)),
                    };
                    if online { self.publish_notify(&client, &notify, kind, card, "card state changed").await?; }
                }
            }
        }
        Ok(())
    }

    async fn connect(&self) -> Result<(AsyncClient, rumqttc::EventLoop)> {
        // Endpoint is deliberately fixed by the protocol. Username/password
        // remain configurable and are never included in logs or topic names.
        if self.config.mqtt.url != DEFAULT_MQTT_URL {
            warn!(configured = %self.config.mqtt.url, enforced = DEFAULT_MQTT_URL, "overriding non-standard MQTT URL");
        }
        let options = connection_options(&self.config, &self.client_id)?;
        Ok(AsyncClient::new(
            options,
            self.config.request_queue_capacity.max(8),
        ))
    }

    async fn handle_method(&self, client: &AsyncClient, method_topic: &str, payload: &[u8]) {
        let request = match proto::MethodRequest::decode(payload) {
            Ok(request) => request,
            Err(error) => {
                warn!(%error, "invalid protobuf method request");
                return;
            }
        };
        let response = match proto::Operation::try_from(request.operation) {
            Ok(proto::Operation::ListCards) => proto::MethodResponse {
                request_id: request.request_id,
                success: true,
                error: String::new(),
                payload: Some(proto::method_response::Payload::CardList(proto::CardList {
                    cards: self
                        .sessions
                        .list()
                        .await
                        .into_iter()
                        .map(|card| proto::CardSummary {
                            card_id: card.card_id,
                            reader_name: card.reader_name,
                            protocol: card.protocol,
                            card_name: card.card_name,
                        })
                        .collect(),
                })),
            },
            Ok(proto::Operation::GetCard) => match self.sessions.get(&request.card_id).await {
                Some(card) => proto::MethodResponse {
                    request_id: request.request_id,
                    success: true,
                    error: String::new(),
                    payload: Some(proto::method_response::Payload::Card(card)),
                },
                None => error_response(
                    request.request_id,
                    format!("card not found: {}", request.card_id),
                ),
            },
            Ok(proto::Operation::ExecuteApdu) => {
                match self.executor.execute(request.card_id, request.apdu).await {
                    Ok(data) => {
                        let (body, sw1, sw2) = if data.len() >= 2 {
                            (
                                data[..data.len() - 2].to_vec(),
                                data[data.len() - 2] as u32,
                                data[data.len() - 1] as u32,
                            )
                        } else {
                            (data, 0, 0)
                        };
                        proto::MethodResponse {
                            request_id: request.request_id,
                            success: true,
                            error: String::new(),
                            payload: Some(proto::method_response::Payload::Apdu(
                                proto::ApduResponse {
                                    data: body,
                                    sw1,
                                    sw2,
                                },
                            )),
                        }
                    }
                    Err(error) => error_response(request.request_id, error.to_string()),
                }
            }
            _ => error_response(request.request_id, "unsupported operation".to_owned()),
        };
        let mut encoded = Vec::with_capacity(response.encoded_len());
        if response.encode(&mut encoded).is_ok() {
            if let Err(error) = client
                .publish(method_topic, QoS::AtLeastOnce, false, encoded)
                .await
            {
                warn!(%error, "publish method response failed");
            }
        }
    }

    async fn publish_notify(
        &self,
        client: &AsyncClient,
        topic: &str,
        event: proto::NotifyEvent,
        card: Option<proto::CardInfo>,
        reason: &str,
    ) -> Result<()> {
        let message = proto::Notify {
            event: event as i32,
            user_id: self.user_id.clone(),
            card_id: card.as_ref().map(|v| v.card_id.clone()).unwrap_or_default(),
            card,
            reason: reason.to_owned(),
        };
        let mut encoded = Vec::with_capacity(message.encoded_len());
        message
            .encode(&mut encoded)
            .context("encode protobuf notification")?;
        client
            .publish(topic, QoS::AtLeastOnce, false, encoded)
            .await
            .context("publish notification")?;
        Ok(())
    }
}

fn error_response(request_id: String, error: String) -> proto::MethodResponse {
    proto::MethodResponse {
        request_id,
        success: false,
        error,
        payload: None,
    }
}

#[allow(dead_code)]
fn _card_error(error: CardError) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn topics_are_scoped_by_effective_user_id() {
        assert_eq!(
            method_topic("u:device"),
            "QiCardMesh/device/u:device/method"
        );
        assert_eq!(
            notify_topic("u:device"),
            "QiCardMesh/device/u:device/notify"
        );
    }

    #[test]
    fn protobuf_method_round_trip() {
        let request = proto::MethodRequest {
            request_id: "r1".into(),
            operation: proto::Operation::ListCards as i32,
            card_id: String::new(),
            apdu: Vec::new(),
        };
        let mut bytes = Vec::new();
        request.encode(&mut bytes).unwrap();
        let decoded = proto::MethodRequest::decode(bytes.as_slice()).unwrap();
        assert_eq!(decoded.request_id, "r1");
        assert_eq!(
            proto::Operation::try_from(decoded.operation),
            Ok(proto::Operation::ListCards)
        );
    }
}
