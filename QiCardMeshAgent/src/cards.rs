use crate::proto::CardInfo;
use anyhow::Result;
use std::{collections::HashMap, sync::Arc, time::Duration};
use thiserror::Error;
use tokio::{
    sync::{mpsc, Mutex, RwLock},
    task::spawn_blocking,
    time,
};
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

#[derive(Debug, Error)]
pub enum CardError {
    #[error("card not found: {0}")]
    NotFound(String),
    #[error("card backend error: {0}")]
    Backend(String),
}

/// Synchronous by design: most PC/SC implementations block while a reader is
/// being contacted. CardMonitor and ApduExecutor call it on blocking workers,
/// so the async MQTT loop is never held up by a reader.
pub trait CardBackend: Send + Sync + 'static {
    fn list_cards(&self) -> Result<Vec<CardInfo>, CardError>;
    fn transmit(&self, card_id: &str, apdu: &[u8]) -> Result<Vec<u8>, CardError>;
}

/// Portable fallback used when the binary is built without native PC/SC.
/// Deployments can inject a backend in tests or enable the `pcsc` feature.
#[derive(Default)]
pub struct UnavailableBackend;

impl CardBackend for UnavailableBackend {
    fn list_cards(&self) -> Result<Vec<CardInfo>, CardError> {
        Ok(Vec::new())
    }
    fn transmit(&self, _: &str, _: &[u8]) -> Result<Vec<u8>, CardError> {
        Err(CardError::Backend(
            "PC/SC backend is not enabled".to_owned(),
        ))
    }
}

#[cfg(feature = "pcsc")]
pub struct PcscBackend;

#[cfg(feature = "pcsc")]
impl CardBackend for PcscBackend {
    fn list_cards(&self) -> Result<Vec<CardInfo>, CardError> {
        let context = pcsc::Context::establish(pcsc::Scope::User)
            .map_err(|e| CardError::Backend(e.to_string()))?;
        let mut readers_buf = [0; 2048];
        let readers = context
            .list_readers(&mut readers_buf)
            .map_err(|e| CardError::Backend(e.to_string()))?;
        let mut cards = Vec::new();
        for reader in readers {
            let reader_name = reader.to_string_lossy().into_owned();
            // Listing readers is deliberately non-blocking with respect to a
            // card. Connecting here only for present cards gives us ATR data.
            if let Ok(card) = context.connect(reader, pcsc::ShareMode::Shared, pcsc::Protocols::ANY)
            {
                let mut atr = [0u8; 36];
                let atr_slice = card
                    .get_attribute(pcsc::Attribute::AtrString, &mut atr)
                    .unwrap_or(&[]);
                cards.push(CardInfo {
                    card_id: stable_card_id(&reader_name, atr_slice),
                    reader_name,
                    atr: atr_slice.to_vec(),
                    protocol: "pcsc".to_owned(),
                    card_name: String::new(),
                    metadata: HashMap::new(),
                });
            }
        }
        Ok(cards)
    }

    fn transmit(&self, card_id: &str, apdu: &[u8]) -> Result<Vec<u8>, CardError> {
        let context = pcsc::Context::establish(pcsc::Scope::User)
            .map_err(|e| CardError::Backend(e.to_string()))?;
        let mut readers_buf = [0; 2048];
        let readers = context
            .list_readers(&mut readers_buf)
            .map_err(|e| CardError::Backend(e.to_string()))?;
        for reader in readers {
            let reader_name = reader.to_string_lossy().into_owned();
            let card = match context.connect(reader, pcsc::ShareMode::Shared, pcsc::Protocols::ANY)
            {
                Ok(v) => v,
                Err(_) => continue,
            };
            let mut atr = [0u8; 36];
            let atr_slice = card
                .get_attribute(pcsc::Attribute::AtrString, &mut atr)
                .unwrap_or(&[]);
            if stable_card_id(&reader_name, atr_slice) != card_id {
                continue;
            }
            let mut response = [0u8; 4096];
            let used = card
                .transmit(apdu, &mut response)
                .map_err(|e| CardError::Backend(e.to_string()))?;
            return Ok(used.to_vec());
        }
        Err(CardError::NotFound(card_id.to_owned()))
    }
}

#[cfg(feature = "pcsc")]
fn stable_card_id(reader: &str, atr: &[u8]) -> String {
    let mut hasher = sha2::Sha256::new();
    use sha2::Digest;
    hasher.update(reader.as_bytes());
    hasher.update([0]);
    hasher.update(atr);
    hex::encode(&hasher.finalize()[..12])
}

#[derive(Debug, Clone)]
pub enum CardEvent {
    Connected(CardInfo),
    Disconnected(CardInfo),
}

#[derive(Clone)]
pub struct CardSessionManager {
    sessions: Arc<RwLock<HashMap<String, CardInfo>>>,
}

impl Default for CardSessionManager {
    fn default() -> Self {
        Self {
            sessions: Arc::new(RwLock::new(HashMap::new())),
        }
    }
}

impl CardSessionManager {
    pub async fn apply(&self, event: &CardEvent) {
        let mut sessions = self.sessions.write().await;
        match event {
            CardEvent::Connected(card) => {
                sessions.insert(card.card_id.clone(), card.clone());
            }
            CardEvent::Disconnected(card) => {
                sessions.remove(&card.card_id);
            }
        }
    }

    pub async fn list(&self) -> Vec<CardInfo> {
        let mut cards: Vec<_> = self.sessions.read().await.values().cloned().collect();
        cards.sort_by(|a, b| a.card_id.cmp(&b.card_id));
        cards
    }

    pub async fn get(&self, card_id: &str) -> Option<CardInfo> {
        self.sessions.read().await.get(card_id).cloned()
    }
}

#[derive(Clone)]
pub struct ApduExecutor {
    backend: Arc<dyn CardBackend>,
    serial: Arc<Mutex<()>>,
    sessions: CardSessionManager,
}

impl ApduExecutor {
    pub fn new(backend: Arc<dyn CardBackend>, sessions: CardSessionManager) -> Self {
        Self {
            backend,
            serial: Arc::new(Mutex::new(())),
            sessions,
        }
    }

    pub async fn execute(&self, card_id: String, apdu: Vec<u8>) -> Result<Vec<u8>, CardError> {
        if self.sessions.get(&card_id).await.is_none() {
            return Err(CardError::NotFound(card_id));
        }
        let _serial = self.serial.lock().await;
        let backend = Arc::clone(&self.backend);
        spawn_blocking(move || backend.transmit(&card_id, &apdu))
            .await
            .map_err(|e| CardError::Backend(e.to_string()))?
    }
}

pub struct CardMonitor {
    backend: Arc<dyn CardBackend>,
    interval: Duration,
    sessions: CardSessionManager,
}

impl CardMonitor {
    pub fn new(
        backend: Arc<dyn CardBackend>,
        interval: Duration,
        sessions: CardSessionManager,
    ) -> Self {
        Self {
            backend,
            interval,
            sessions,
        }
    }

    pub async fn run(self, tx: mpsc::Sender<CardEvent>, cancel: CancellationToken) {
        let mut ticker = time::interval(self.interval);
        let mut known: HashMap<String, CardInfo> = HashMap::new();
        loop {
            tokio::select! {
                _ = cancel.cancelled() => break,
                _ = ticker.tick() => {
                    let backend = Arc::clone(&self.backend);
                    let result = spawn_blocking(move || backend.list_cards()).await;
                    let cards = match result {
                        Ok(Ok(cards)) => cards,
                        Ok(Err(error)) => { warn!(%error, "card scan failed"); continue; }
                        Err(error) => { warn!(%error, "card scan worker failed"); continue; }
                    };
                    let current: HashMap<_, _> = cards.into_iter().map(|c| (c.card_id.clone(), c)).collect();
                    for (id, card) in &current {
                        if !known.contains_key(id) {
                            debug!(card_id = %id, "card connected");
                            let event = CardEvent::Connected(card.clone());
                            self.sessions.apply(&event).await;
                            if tx.send(event).await.is_err() { return; }
                        }
                    }
                    for (id, card) in &known {
                        if !current.contains_key(id) {
                            debug!(card_id = %id, "card disconnected");
                            let event = CardEvent::Disconnected(card.clone());
                            self.sessions.apply(&event).await;
                            if tx.send(event).await.is_err() { return; }
                        }
                    }
                    known = current;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct FakeBackend {
        calls: AtomicUsize,
    }
    impl CardBackend for FakeBackend {
        fn list_cards(&self) -> Result<Vec<CardInfo>, CardError> {
            Ok(vec![CardInfo {
                card_id: "card-1".into(),
                reader_name: "fake".into(),
                atr: vec![0x3b],
                protocol: "test".into(),
                card_name: "Test".into(),
                metadata: HashMap::new(),
            }])
        }
        fn transmit(&self, _: &str, apdu: &[u8]) -> Result<Vec<u8>, CardError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(apdu.to_vec())
        }
    }

    #[tokio::test]
    async fn manager_and_executor_preserve_card_and_apdu_state() {
        let backend = Arc::new(FakeBackend {
            calls: AtomicUsize::new(0),
        });
        let sessions = CardSessionManager::default();
        let card = backend.list_cards().unwrap().pop().unwrap();
        let event = CardEvent::Connected(card);
        sessions.apply(&event).await;
        let executor = ApduExecutor::new(backend.clone(), sessions);
        assert_eq!(
            executor
                .execute("card-1".into(), vec![0x00, 0xa4])
                .await
                .unwrap(),
            vec![0x00, 0xa4]
        );
        assert_eq!(backend.calls.load(Ordering::SeqCst), 1);
    }
}
