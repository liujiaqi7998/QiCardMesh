use crate::{
    apdu,
    identity::{self, Info},
};
use anyhow::{Result, bail};
use pcsc::{Context, Error, ReaderState, Scope, State};
use std::{
    collections::BTreeMap,
    ffi::{CStr, CString},
    time::Duration,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reader {
    pub name: CString,
    pub state: State,
    pub generation: u32,
    pub atr: Vec<u8>,
}

impl Reader {
    pub fn present(&self) -> bool {
        self.state.contains(State::PRESENT)
            && !self
                .state
                .intersects(State::UNKNOWN | State::UNAVAILABLE | State::EMPTY | State::MUTE)
    }
}

pub trait Connection {
    fn check(&self) -> Result<()>;
    fn info(&mut self, reader: &Reader) -> Result<(String, Info)>;
    fn transmit(&mut self, commands: &[Vec<u8>]) -> Result<Vec<Vec<u8>>>;
}

pub trait Backend {
    type Card: Connection;
    fn scan(&mut self, timeout: Duration) -> Result<Vec<Reader>>;
    fn connect(&mut self, reader: &CStr) -> Result<Self::Card>;
    fn reset(&mut self);
}

#[derive(Default)]
pub struct Pcsc {
    context: Option<Context>,
    states: Vec<ReaderState>,
}

impl Backend for Pcsc {
    type Card = Card;

    fn scan(&mut self, timeout: Duration) -> Result<Vec<Reader>> {
        if self.context.is_none() {
            self.context = Some(Context::establish(Scope::User)?);
        }
        let ctx = self
            .context
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("PC/SC context unavailable"))?;
        let names = match ctx.list_readers_owned() {
            Ok(names) => names,
            Err(Error::NoReadersAvailable) => Vec::new(),
            Err(error) => return Err(error.into()),
        };
        self.states
            .retain(|state| names.iter().any(|name| name.as_c_str() == state.name()));
        for state in &mut self.states {
            state.sync_current_state();
        }
        for name in names {
            if !self
                .states
                .iter()
                .any(|state| state.name() == name.as_c_str())
            {
                self.states.push(ReaderState::new(name, State::UNAWARE));
            }
        }
        // Re-enumeration each bounded wait also discovers USB readers on macOS,
        // where the Windows/Linux PnP pseudo-reader is not universally supported.
        if self.states.is_empty() {
            std::thread::sleep(timeout);
        } else {
            match ctx.get_status_change(timeout, &mut self.states) {
                Ok(()) | Err(Error::Timeout) => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(self
            .states
            .iter()
            .map(|state| Reader {
                name: state.name().to_owned(),
                state: state.event_state() & !State::CHANGED,
                generation: state.event_count(),
                atr: state.atr().to_vec(),
            })
            .collect())
    }

    fn connect(&mut self, reader: &CStr) -> Result<Card> {
        let ctx = self
            .context
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("PC/SC context unavailable"))?;
        Ok(Card(Some(ctx.connect(
            reader,
            pcsc::ShareMode::Shared,
            pcsc::Protocols::ANY,
        )?)))
    }

    fn reset(&mut self) {
        self.states.clear();
        self.context = None;
    }
}

// Explicit LeaveCard avoids pcsc::Card's default reset on ordinary shutdown.
pub struct Card(Option<pcsc::Card>);
impl Drop for Card {
    fn drop(&mut self) {
        if let Some(card) = self.0.take() {
            let _ = card.disconnect(pcsc::Disposition::LeaveCard);
        }
    }
}

impl Card {
    fn get(&mut self) -> Result<&mut pcsc::Card> {
        self.0
            .as_mut()
            .ok_or_else(|| anyhow::anyhow!("card connection has been released"))
    }
}

impl Connection for Card {
    fn check(&self) -> Result<()> {
        self.0
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("card connection has been released"))?
            .status2_owned()?;
        Ok(())
    }

    fn info(&mut self, reader: &Reader) -> Result<(String, Info)> {
        let card = self.get()?;
        let status = card.status2_owned()?;
        let protocol = format!("{:?}", status.protocol());
        let transaction = card.transaction()?;
        let result = identity::inspect(&reader.name.to_string_lossy(), status.atr(), |cmd| {
            let mut response = vec![0; pcsc::MAX_BUFFER_SIZE_EXTENDED];
            Ok(transaction.transmit(cmd, &mut response)?.to_vec())
        });
        // End explicitly so transaction-release errors are reported as well.
        let end = transaction
            .end(pcsc::Disposition::LeaveCard)
            .map_err(|(_, error)| error);
        let info = result?;
        end?;
        Ok((protocol, info))
    }

    fn transmit(&mut self, commands: &[Vec<u8>]) -> Result<Vec<Vec<u8>>> {
        let transaction = self.get()?.transaction()?;
        let result: Result<Vec<Vec<u8>>> = (|| {
            let mut replies = Vec::new();
            for command in commands {
                let mut response = vec![0; pcsc::MAX_BUFFER_SIZE_EXTENDED];
                let response = transaction.transmit(command, &mut response)?.to_vec();
                let (_, sw) = apdu::split_response(&response)?;
                replies.push(response);
                // Stop a batch after a card error, but return the raw response.
                if sw != 0x9000 {
                    break;
                }
            }
            Ok(replies)
        })();
        let end = transaction
            .end(pcsc::Disposition::LeaveCard)
            .map_err(|(_, error)| error);
        let replies = result?;
        end?;
        Ok(replies)
    }
}

#[cfg(test)]
mod tests;

pub struct Session<C> {
    pub reader: Reader,
    pub card: C,
    pub description: String,
    pub protocol: Option<String>,
    pub info: Option<identity::Info>,
}

pub struct Monitor<B: Backend> {
    pub backend: B,
    pub readers: Vec<Reader>,
    pub sessions: BTreeMap<CString, Session<B::Card>>,
    failures: BTreeMap<CString, String>,
    service_error: Option<String>,
    selector: Option<String>,
    inspect: bool,
}

impl<B: Backend> Monitor<B> {
    pub fn new(backend: B, selector: Option<String>, inspect: bool) -> Self {
        Self {
            backend,
            readers: Vec::new(),
            sessions: BTreeMap::new(),
            failures: BTreeMap::new(),
            service_error: None,
            selector,
            inspect,
        }
    }

    pub fn tick(&mut self, timeout: Duration) -> Result<Vec<String>> {
        let readers = match self.backend.scan(timeout) {
            Ok(readers) => readers,
            Err(error) => {
                // Invalidate all handles before re-establishing a service context.
                self.sessions.clear();
                self.readers.clear();
                self.failures.clear();
                self.backend.reset();
                return Err(error);
            }
        };
        let mut events = Vec::new();
        if self.service_error.take().is_some() {
            events.push("PC/SC service recovered".into());
        }
        let exact = self
            .selector
            .as_ref()
            .is_some_and(|s| readers.iter().any(|r| r.name.to_string_lossy() == *s));
        let readers: Vec<_> = readers
            .into_iter()
            .filter(|r| {
                self.selector.as_ref().is_none_or(|s| {
                    let name = r.name.to_string_lossy();
                    if exact {
                        name == *s
                    } else {
                        name.to_lowercase().contains(&s.to_lowercase())
                    }
                })
            })
            .collect();

        for previous in &self.readers {
            if !readers.iter().any(|r| r.name == previous.name) {
                events.push(format!(
                    "reader-removed: {} (connection released)",
                    previous.name.to_string_lossy()
                ));
            }
        }
        self.sessions.retain(|name, session| {
            let keep = readers.iter().any(|r| {
                r.name == *name
                    && r.present()
                    && r.generation == session.reader.generation
                    && r.atr == session.reader.atr
            });
            if !keep {
                events.push(format!(
                    "disconnected: {} (connection released)",
                    name.to_string_lossy()
                ));
            }
            keep
        });
        self.failures
            .retain(|name, _| readers.iter().any(|r| r.name == *name && r.present()));
        for reader in &readers {
            let previous = self.readers.iter().find(|r| r.name == reader.name);
            if previous.is_none_or(|r| {
                r.state != reader.state || r.generation != reader.generation || r.atr != reader.atr
            }) {
                events.push(format!(
                    "reader: {} | state={:?} | device={} | ATR={}",
                    reader.name.to_string_lossy(),
                    reader.state,
                    identity::detect(&reader.name.to_string_lossy(), &reader.atr),
                    apdu::hex(&reader.atr)
                ));
            }
            if !reader.present() {
                continue;
            }
            if let Some(session) = self.sessions.get(&reader.name)
                && let Err(error) = session.card.check()
            {
                self.sessions.remove(&reader.name);
                events.push(format!(
                    "disconnected: {} ({error}; connection released)",
                    reader.name.to_string_lossy()
                ));
            }
            if self.sessions.contains_key(&reader.name) {
                continue;
            }
            let connected = (|| {
                let mut card = self.backend.connect(&reader.name)?;
                let mut protocol = None;
                let mut inspected = None;
                let description = if self.inspect {
                    let (card_protocol, info) = card.info(reader)?;
                    protocol = Some(card_protocol.clone());
                    inspected = Some(info.clone());
                    format!(
                        "device={} | identified-by={} | protocol={card_protocol} | firmware={} | serial={} | OpenPGP AID={}",
                        info.kind,
                        info.evidence,
                        info.version.as_deref().unwrap_or("unavailable"),
                        info.serial.as_deref().unwrap_or("unavailable"),
                        info.openpgp_aid.as_deref().unwrap_or("unavailable")
                    )
                } else {
                    "ready for APDU".into()
                };
                Ok::<_, anyhow::Error>(Session {
                    reader: reader.clone(),
                    card,
                    description,
                    protocol,
                    info: inspected,
                })
            })();
            match connected {
                Ok(session) => {
                    events.push(format!(
                        "connected: {} | {}",
                        reader.name.to_string_lossy(),
                        session.description
                    ));
                    self.sessions.insert(reader.name.clone(), session);
                    self.failures.remove(&reader.name);
                }
                Err(error) => {
                    let message = error.to_string();
                    if self.failures.get(&reader.name) != Some(&message) {
                        events.push(format!(
                            "unavailable: {} | {message} (retried in watch/interactive mode)",
                            reader.name.to_string_lossy()
                        ));
                        self.failures.insert(reader.name.clone(), message);
                    }
                }
            }
        }
        self.readers = readers;
        Ok(events)
    }

    pub fn report_service_error(&mut self, error: &anyhow::Error) {
        let message = error.to_string();
        if self.service_error.as_ref() != Some(&message) {
            eprintln!(
                "PC/SC unavailable: {message}; connections released; retrying (check Smart Card / pcscd service)"
            );
            self.service_error = Some(message);
        }
    }

    pub fn transmit(&mut self, commands: &[Vec<u8>]) -> Result<Vec<Vec<u8>>> {
        self.transmit_for(None, commands)
    }

    /// Send APDUs to one explicitly selected reader, or to the sole present
    /// card when `reader` is `None`. This is used by the daemon worker so MQTT
    /// requests can identify a card without changing the monitor selector.
    pub fn transmit_for(
        &mut self,
        reader: Option<&str>,
        commands: &[Vec<u8>],
    ) -> Result<Vec<Vec<u8>>> {
        let name = if let Some(selector) = reader {
            let exact = self
                .readers
                .iter()
                .find(|r| r.name.to_string_lossy() == selector)
                .or_else(|| {
                    self.readers.iter().find(|r| {
                        r.name
                            .to_string_lossy()
                            .to_ascii_lowercase()
                            .contains(&selector.to_ascii_lowercase())
                    })
                });
            let Some(selected) = exact else {
                bail!("reader not found: {selector}");
            };
            if !selected.present() {
                bail!("card is not present in reader: {selector}");
            }
            selected.name.clone()
        } else {
            // Count physical cards, including busy ones, to avoid selecting the
            // wrong device simply because a different application's lock changed.
            let mut present = self.readers.iter().filter(|r| r.present());
            let Some(reader) = present.next() else {
                bail!("no card present; insert a card or check --reader");
            };
            if present.next().is_some() {
                bail!("multiple cards match; select one using --reader with its exact name");
            }
            reader.name.clone()
        };
        let session = self.sessions.get_mut(&name).ok_or_else(|| {
            anyhow::anyhow!("card is not connected; it may be busy or unavailable")
        })?;
        let result = session.card.transmit(commands);
        if result.is_err() {
            self.sessions.remove(&name);
        }
        // Never replay a failed command: the card may have executed it already.
        result
    }
}
