//! Multi-threaded card service used by the MQTT daemon.
//!
//! PC/SC handles are deliberately kept on one worker thread.  Apart from
//! making the API portable, this is important on Windows where a card handle
//! may not be used concurrently by multiple threads.  MQTT code can call the
//! cloneable [`CardSessionManager`] from any thread; requests are serialized by
//! the worker and APDUs are never replayed after an error.

use crate::monitor::{Backend, Monitor, Pcsc, Reader};
use anyhow::{Result, anyhow};
use std::collections::BTreeMap;
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

/// Configuration for the card monitor worker.
#[derive(Clone, Debug)]
pub struct CardMonitorConfig {
    /// Time spent waiting for PC/SC status changes.  A bounded wait lets the
    /// worker process shutdown and MQTT requests promptly.
    pub poll_interval: Duration,
    /// Optional reader selector, using the same exact/substring semantics as
    /// the CLI monitor.
    pub reader: Option<String>,
    /// Read the vendor metadata when a card is connected.
    pub inspect: bool,
}

impl Default for CardMonitorConfig {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_millis(500),
            reader: None,
            inspect: true,
        }
    }
}

/// A serializable snapshot suitable for MQTT list/status responses.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CardSummary {
    pub reader: String,
    /// PC/SC event counter. A change invalidates any previous card handle,
    /// even when a card is removed and an identical card is inserted quickly.
    pub generation: u32,
    pub present: bool,
    pub connected: bool,
    pub atr: Vec<u8>,
    pub device: String,
    pub description: Option<String>,
    pub protocol: Option<String>,
    pub info: Option<crate::identity::Info>,
}

/// Notification emitted by the monitor worker.  The MQTT layer can map these
/// directly to its protobuf `notify` messages.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CardEvent {
    ReaderState(CardSummary),
    CardConnected(CardSummary),
    CardDisconnected { reader: String, reason: String },
    ServiceUnavailable(String),
    ServiceRecovered,
}

enum Request {
    List {
        reply: Sender<Result<Vec<CardSummary>>>,
    },
    Get {
        reader: Option<String>,
        reply: Sender<Result<Option<CardSummary>>>,
    },
    Transmit {
        reader: Option<String>,
        commands: Vec<Vec<u8>>,
        reply: Sender<Result<Vec<Vec<u8>>>>,
    },
    Shutdown,
}

/// Cloneable command facade used by MQTT request handlers.
#[derive(Clone)]
pub struct CardSessionManager {
    requests: Sender<Request>,
}

impl CardSessionManager {
    fn request<T>(&self, make: impl FnOnce(Sender<Result<T>>) -> Request) -> Result<T> {
        let (reply, response) = mpsc::channel();
        self.requests
            .send(make(reply))
            .map_err(|_| anyhow!("card monitor worker has stopped"))?;
        response
            .recv()
            .map_err(|_| anyhow!("card monitor worker dropped the response"))?
    }

    /// Return snapshots of all readers matching the configured selector.
    pub fn list_cards(&self) -> Result<Vec<CardSummary>> {
        self.request(|reply| Request::List { reply })
    }

    /// Return one card by exact reader name (or `None` if it is absent).
    pub fn get_card(&self, reader: Option<&str>) -> Result<Option<CardSummary>> {
        self.request(|reply| Request::Get {
            reader: reader.map(str::to_owned),
            reply,
        })
    }

    /// Send a batch of APDUs.  The worker owns the PC/SC card and therefore
    /// all callers are serialized. A failed batch is never replayed.
    pub fn transmit(&self, reader: Option<&str>, commands: Vec<Vec<u8>>) -> Result<Vec<Vec<u8>>> {
        self.request(|reply| Request::Transmit {
            reader: reader.map(str::to_owned),
            commands,
            reply,
        })
    }
}

/// Explicit APDU facade. Keeping this type separate makes the intended
/// single-writer property obvious to callers and leaves room for APDU policy
/// (limits, authorization, tracing) without changing session management.
#[derive(Clone)]
pub struct ApduExecutor {
    sessions: CardSessionManager,
}

impl ApduExecutor {
    pub fn new(sessions: CardSessionManager) -> Self {
        Self { sessions }
    }

    pub fn execute(&self, reader: Option<&str>, commands: Vec<Vec<u8>>) -> Result<Vec<Vec<u8>>> {
        self.sessions.transmit(reader, commands)
    }
}

/// Handle for the card discovery/session worker and its event stream.
pub struct CardMonitor {
    sessions: CardSessionManager,
    events: Receiver<CardEvent>,
    join: Option<JoinHandle<()>>,
}

impl CardMonitor {
    /// Start a worker that owns the PC/SC context and all card connections.
    pub fn start(config: CardMonitorConfig) -> Self {
        Self::start_with_backend(config, Pcsc::default())
    }

    /// Testable constructor. Production code normally uses [`Self::start`].
    pub fn start_with_backend<B>(config: CardMonitorConfig, backend: B) -> Self
    where
        B: Backend + Send + 'static,
        B::Card: Send + 'static,
    {
        let (request_tx, request_rx) = mpsc::channel();
        let (event_tx, event_rx) = mpsc::channel();
        let join = thread::Builder::new()
            .name("qicardmesh-card-monitor".into())
            .spawn(move || worker(config, backend, request_rx, event_tx))
            .expect("failed to spawn card monitor worker");
        Self {
            sessions: CardSessionManager {
                requests: request_tx,
            },
            events: event_rx,
            join: Some(join),
        }
    }

    pub fn sessions(&self) -> CardSessionManager {
        self.sessions.clone()
    }

    pub fn apdu_executor(&self) -> ApduExecutor {
        ApduExecutor::new(self.sessions())
    }

    /// Drain notifications without blocking. MQTT loops can call this after
    /// processing inbound messages.
    pub fn try_events(&self) -> Vec<CardEvent> {
        let mut result = Vec::new();
        while let Ok(event) = self.events.try_recv() {
            result.push(event);
        }
        result
    }

    /// Ask the worker to stop and wait for it. Dropping a monitor also performs
    /// this shutdown, so card handles are released even on normal exits.
    pub fn shutdown(mut self) {
        let _ = self.sessions.requests.send(Request::Shutdown);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

impl Drop for CardMonitor {
    fn drop(&mut self) {
        let _ = self.sessions.requests.send(Request::Shutdown);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

fn worker<B>(
    config: CardMonitorConfig,
    backend: B,
    requests: Receiver<Request>,
    events: Sender<CardEvent>,
) where
    B: Backend,
{
    let mut monitor = Monitor::new(backend, config.reader, config.inspect);
    let mut previous = BTreeMap::<String, CardSummary>::new();
    let mut service_failed = false;
    let poll = config.poll_interval.max(Duration::from_millis(10));
    let mut running = true;

    // Populate the initial snapshot before serving the first request. This is
    // a zero-time scan and therefore never delays daemon startup.
    if monitor.tick(Duration::ZERO).is_ok() {
        emit_changes(&monitor, &mut previous, &events);
    }

    while running {
        // Handle all commands queued since the previous poll. Commands are
        // serviced before waiting for a status change to keep MQTT latency low.
        running = process_requests(&mut monitor, &requests);
        if !running {
            break;
        }

        match monitor.tick(poll) {
            Ok(_) => {
                if service_failed {
                    let _ = events.send(CardEvent::ServiceRecovered);
                    service_failed = false;
                }
                emit_changes(&monitor, &mut previous, &events);
            }
            Err(error) => {
                if !service_failed {
                    let _ = events.send(CardEvent::ServiceUnavailable(error.to_string()));
                    service_failed = true;
                }
                // Monitor::tick resets its PC/SC context and drops every card
                // handle on an error. Clear the previous snapshot as well so
                // recovery emits a fresh CardConnected notification instead
                // of treating a newly-created handle as the old session.
                for (reader, card) in std::mem::take(&mut previous) {
                    if card.connected {
                        let _ = events.send(CardEvent::CardDisconnected {
                            reader,
                            reason: "PC/SC service unavailable; connection released".into(),
                        });
                    }
                }
                // A failed PC/SC context is reset by Monitor::tick. Keep a
                // bounded retry loop and remain responsive to shutdown.
                thread::sleep(poll.min(Duration::from_millis(500)));
            }
        }
    }
}

fn process_requests<B: Backend>(monitor: &mut Monitor<B>, requests: &Receiver<Request>) -> bool {
    loop {
        let request = match requests.try_recv() {
            Ok(request) => request,
            Err(TryRecvError::Empty) => return true,
            Err(TryRecvError::Disconnected) => return false,
        };
        match request {
            Request::Shutdown => return false,
            Request::List { reply } => {
                let _ = reply.send(Ok(summaries(monitor)));
            }
            Request::Get { reader, reply } => {
                let cards = summaries(monitor);
                let result = select_summary(&cards, reader.as_deref());
                let _ = reply.send(Ok(result.cloned()));
            }
            Request::Transmit {
                reader,
                commands,
                reply,
            } => {
                // A zero-time poll refreshes presence before touching a card.
                let result = monitor
                    .tick(Duration::ZERO)
                    .and_then(|_| monitor.transmit_for(reader.as_deref(), &commands));
                let _ = reply.send(result);
            }
        }
    }
}

fn summary(reader: &Reader, monitor: &Monitor<impl Backend>) -> CardSummary {
    CardSummary {
        reader: reader.name.to_string_lossy().into_owned(),
        generation: reader.generation,
        present: reader.present(),
        connected: monitor.sessions.contains_key(&reader.name),
        atr: reader.atr.clone(),
        device: crate::identity::detect(&reader.name.to_string_lossy(), &reader.atr).to_string(),
        description: monitor
            .sessions
            .get(&reader.name)
            .map(|session| session.description.clone()),
        protocol: monitor
            .sessions
            .get(&reader.name)
            .and_then(|session| session.protocol.clone()),
        info: monitor
            .sessions
            .get(&reader.name)
            .and_then(|session| session.info.clone()),
    }
}

fn summaries<B: Backend>(monitor: &Monitor<B>) -> Vec<CardSummary> {
    monitor
        .readers
        .iter()
        .map(|reader| summary(reader, monitor))
        .collect()
}

fn select_summary<'a>(
    summaries: &'a [CardSummary],
    reader: Option<&str>,
) -> Option<&'a CardSummary> {
    match reader {
        Some(name) => summaries
            .iter()
            .find(|card| card.reader == name)
            .or_else(|| {
                summaries
                    .iter()
                    .find(|card| card.reader.eq_ignore_ascii_case(name))
            }),
        None => {
            let mut present = summaries.iter().filter(|card| card.present);
            let first = present.next();
            if present.next().is_some() {
                None
            } else {
                first
            }
        }
    }
}

fn emit_changes<B: Backend>(
    monitor: &Monitor<B>,
    previous: &mut BTreeMap<String, CardSummary>,
    events: &Sender<CardEvent>,
) {
    let current: BTreeMap<_, _> = summaries(monitor)
        .into_iter()
        .map(|card| (card.reader.clone(), card))
        .collect();
    for (reader, old) in previous.iter() {
        if !current.contains_key(reader) || (old.connected && !current[reader].connected) {
            let _ = events.send(CardEvent::CardDisconnected {
                reader: reader.clone(),
                reason: "card removed or connection released".into(),
            });
        }
    }
    for (reader, card) in &current {
        match previous.get(reader) {
            None => {
                let _ = events.send(CardEvent::ReaderState(card.clone()));
                if card.connected {
                    let _ = events.send(CardEvent::CardConnected(card.clone()));
                }
            }
            Some(old) => {
                if old != card {
                    let _ = events.send(CardEvent::ReaderState(card.clone()));
                }
                if !old.connected && card.connected {
                    let _ = events.send(CardEvent::CardConnected(card.clone()));
                }
            }
        }
    }
    *previous = current;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn card_selection_requires_single_present_card() {
        let cards = vec![
            CardSummary {
                reader: "one".into(),
                generation: 1,
                present: true,
                connected: true,
                atr: vec![],
                device: "Unknown".into(),
                description: None,
                protocol: None,
                info: None,
            },
            CardSummary {
                reader: "two".into(),
                generation: 1,
                present: true,
                connected: true,
                atr: vec![],
                device: "Unknown".into(),
                description: None,
                protocol: None,
                info: None,
            },
        ];
        assert!(select_summary(&cards, None).is_none());
        assert_eq!(select_summary(&cards, Some("ONE")).unwrap().reader, "one");
    }

    #[test]
    fn defaults_are_safe_for_daemon() {
        let config = CardMonitorConfig::default();
        assert!(config.inspect);
        assert!(config.poll_interval >= Duration::from_millis(10));
    }
}
