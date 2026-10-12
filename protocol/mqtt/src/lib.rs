//! Wire protocol shared by a QiCardMesh agent and its MQTT clients.
//!
//! The generated protobuf module is deliberately kept in a small standalone
//! crate so clients can depend on exactly the same schema as the agent. MQTT
//! transports the encoded messages as binary payloads; no JSON conversion is
//! performed by this crate.

#![forbid(unsafe_code)]
// `prost` oneof enums are generated with inline message variants. Some
// responses (notably APDU batches) are necessarily larger than the others;
// boxing generated fields would make the wire API cumbersome for callers.
#![allow(clippy::large_enum_variant)]

/// Schema version carried in every request, response and notification.
pub const PROTOCOL_VERSION: u32 = 1;

/// MQTT topic prefix. The user id is escaped/validated by the application;
/// this helper only joins the stable protocol segments.
pub const TOPIC_PREFIX: &str = "QiCardMesh/device";

/// Return the method request/response topic for a user id.
pub fn method_topic(user_id: &str) -> String {
    format!("{TOPIC_PREFIX}/{user_id}/method")
}

/// Return the asynchronous notification topic for a user id.
pub fn notify_topic(user_id: &str) -> String {
    format!("{TOPIC_PREFIX}/{user_id}/notify")
}

/// Generated `prost` messages and enums.
pub mod generated {
    include!(concat!(env!("OUT_DIR"), "/qicardmesh.mqtt.v1.rs"));
}

#[cfg(test)]
mod tests {
    use super::{generated::*, method_topic, notify_topic, PROTOCOL_VERSION};
    use prost::Message;

    #[test]
    fn topics_follow_contract() {
        assert_eq!(method_topic("alice"), "QiCardMesh/device/alice/method");
        assert_eq!(notify_topic("alice"), "QiCardMesh/device/alice/notify");
    }

    #[test]
    fn method_round_trip_is_binary_protobuf() {
        let request = MethodRequest {
            protocol_version: PROTOCOL_VERSION,
            request_id: "req-1".into(),
            method: Method::TransmitApdu as i32,
            is_response: false,
            payload: Some(method_request::Payload::TransmitApdu(TransmitApduRequest {
                card_id: "card-1".into(),
                commands: vec![vec![0x00, 0xA4, 0x04, 0x00]],
                timeout_ms: 5000,
            })),
        };
        let bytes = request.encode_to_vec();
        assert!(!bytes.is_empty());
        let decoded = MethodRequest::decode(bytes.as_slice()).expect("decode request");
        assert_eq!(decoded.request_id, "req-1");
        assert_eq!(decoded.method(), Method::TransmitApdu);
        assert!(matches!(
            decoded.payload,
            Some(method_request::Payload::TransmitApdu(_))
        ));
    }

    #[test]
    fn response_marker_survives_round_trip() {
        let response = MethodResponse {
            protocol_version: PROTOCOL_VERSION,
            request_id: "req-1".into(),
            method: Method::ListCards as i32,
            status: StatusCode::StatusOk as i32,
            error_message: String::new(),
            is_response: true,
            payload: None,
        };
        let decoded = MethodResponse::decode(response.encode_to_vec().as_slice()).unwrap();
        assert!(decoded.is_response);
    }
}
