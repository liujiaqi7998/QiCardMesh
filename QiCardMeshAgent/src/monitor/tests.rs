use super::*;
use std::{cell::Cell, rc::Rc};

#[derive(Default)]
struct Counts {
    connects: Cell<u32>,
    drops: Cell<u32>,
    sends: Cell<u32>,
    resets: Cell<u32>,
}
struct FakeCard {
    counts: Rc<Counts>,
    stale: Rc<Cell<bool>>,
    fail_send: Rc<Cell<bool>>,
}
impl Drop for FakeCard {
    fn drop(&mut self) {
        self.counts.drops.set(self.counts.drops.get() + 1);
    }
}
impl Connection for FakeCard {
    fn check(&self) -> Result<()> {
        if self.stale.replace(false) {
            bail!("card reset");
        }
        Ok(())
    }
    fn info(&mut self, reader: &Reader) -> Result<(String, Info)> {
        Ok((
            "T1".into(),
            identity::inspect(&reader.name.to_string_lossy(), &reader.atr, |_| {
                Ok(vec![0x6a, 0x82])
            })?,
        ))
    }
    fn transmit(&mut self, _: &[Vec<u8>]) -> Result<Vec<Vec<u8>>> {
        self.counts.sends.set(self.counts.sends.get() + 1);
        if self.fail_send.get() {
            bail!("card removed during transmit");
        }
        Ok(vec![vec![0x90, 0]])
    }
}
#[derive(Default)]
struct FakeBackend {
    readers: Vec<Reader>,
    counts: Rc<Counts>,
    fail_scan: bool,
    busy: bool,
    stale: Rc<Cell<bool>>,
    fail_send: Rc<Cell<bool>>,
}
impl Backend for FakeBackend {
    type Card = FakeCard;
    fn scan(&mut self, _: Duration) -> Result<Vec<Reader>> {
        if self.fail_scan {
            bail!("service stopped");
        }
        Ok(self.readers.clone())
    }
    fn connect(&mut self, _: &CStr) -> Result<FakeCard> {
        if self.busy {
            bail!("sharing violation");
        }
        self.counts.connects.set(self.counts.connects.get() + 1);
        Ok(FakeCard {
            counts: self.counts.clone(),
            stale: self.stale.clone(),
            fail_send: self.fail_send.clone(),
        })
    }
    fn reset(&mut self) {
        self.counts.resets.set(self.counts.resets.get() + 1);
    }
}
fn reader(name: &str) -> Reader {
    Reader {
        name: CString::new(name).unwrap(),
        state: State::PRESENT,
        generation: 1,
        atr: vec![0x3b, 0],
    }
}
fn setup() -> Monitor<FakeBackend> {
    Monitor::new(
        FakeBackend {
            readers: vec![reader("CanoKey")],
            ..Default::default()
        },
        None,
        true,
    )
}
fn tick(m: &mut Monitor<FakeBackend>) {
    m.tick(Duration::ZERO).unwrap();
}

#[test]
fn unpowered_card_is_connected_so_pcsc_can_power_it_up() {
    let mut m = setup();
    m.backend.readers[0].state |= State::UNPOWERED;
    tick(&mut m);
    assert_eq!(m.backend.counts.connects.get(), 1);
    assert_eq!(m.sessions.len(), 1);
}

#[test]
fn card_removal_releases_handle_and_reinsertion_reconnects() {
    let mut m = setup();
    tick(&mut m);
    assert_eq!(m.backend.counts.connects.get(), 1);
    m.backend.readers[0].state = State::EMPTY;
    tick(&mut m);
    assert!(m.sessions.is_empty());
    assert_eq!(m.backend.counts.drops.get(), 1);
    m.backend.readers[0].state = State::PRESENT;
    tick(&mut m);
    assert_eq!(m.backend.counts.connects.get(), 2);
    assert_eq!(m.transmit(&[vec![0; 4]]).unwrap(), vec![vec![0x90, 0]]);
}

#[test]
fn usb_reader_removal_and_return_are_handled() {
    let mut m = setup();
    tick(&mut m);
    m.backend.readers.clear();
    tick(&mut m);
    assert_eq!(m.backend.counts.drops.get(), 1);
    m.backend.readers.push(reader("CanoKey"));
    tick(&mut m);
    assert_eq!(m.backend.counts.connects.get(), 2);
}

#[test]
fn fast_reinsert_with_identical_atr_invalidates_old_handle() {
    let mut m = setup();
    tick(&mut m);
    m.backend.readers[0].generation += 2;
    tick(&mut m);
    assert_eq!(m.backend.counts.drops.get(), 1);
    assert_eq!(m.backend.counts.connects.get(), 2);
}

#[test]
fn busy_card_retries_without_requiring_a_new_insertion_event() {
    let mut m = setup();
    m.backend.busy = true;
    tick(&mut m);
    assert!(m.sessions.is_empty());
    m.backend.busy = false;
    tick(&mut m);
    assert_eq!(m.sessions.len(), 1);
}

#[test]
fn service_restart_drops_connections_then_recovers() {
    let mut m = setup();
    tick(&mut m);
    m.backend.fail_scan = true;
    assert!(m.tick(Duration::ZERO).is_err());
    assert_eq!(m.backend.counts.drops.get(), 1);
    assert_eq!(m.backend.counts.resets.get(), 1);
    assert!(m.readers.is_empty());
    m.backend.fail_scan = false;
    tick(&mut m);
    assert_eq!(m.backend.counts.connects.get(), 2);
}

#[test]
fn reset_connection_is_replaced() {
    let mut m = setup();
    tick(&mut m);
    m.backend.stale.set(true);
    tick(&mut m);
    assert_eq!(m.backend.counts.drops.get(), 1);
    assert_eq!(m.backend.counts.connects.get(), 2);
}

#[test]
fn removal_during_transmit_never_replays_command() {
    let mut m = setup();
    tick(&mut m);
    m.backend.fail_send.set(true);
    assert!(m.transmit(&[vec![0; 4]]).is_err());
    assert!(m.sessions.is_empty());
    assert_eq!(m.backend.counts.sends.get(), 1);
    assert_eq!(m.backend.counts.drops.get(), 1);
    m.backend.fail_send.set(false);
    tick(&mut m);
    assert_eq!(m.backend.counts.sends.get(), 1);
    assert!(m.transmit(&[vec![0; 4]]).is_ok());
    assert_eq!(m.backend.counts.sends.get(), 2);
}

#[test]
fn refuses_ambiguous_cards_and_prefers_exact_reader_name() {
    let mut m = setup();
    m.backend.readers.push(reader("CanoKey 2"));
    tick(&mut m);
    assert!(m.transmit(&[vec![0; 4]]).is_err());
    assert_eq!(m.backend.counts.sends.get(), 0);
    m.selector = Some("CanoKey".into());
    tick(&mut m);
    assert_eq!(m.sessions.len(), 1);
    assert!(m.transmit(&[vec![0; 4]]).is_ok());
}

#[test]
fn unaffected_card_keeps_connection_and_shutdown_releases_it() {
    let mut m = setup();
    m.backend.readers.push(reader("YubiKey"));
    tick(&mut m);
    let counts = m.backend.counts.clone();
    m.backend.readers.remove(0);
    tick(&mut m);
    tick(&mut m);
    assert_eq!(counts.connects.get(), 2);
    assert_eq!(counts.drops.get(), 1);
    drop(m);
    assert_eq!(counts.drops.get(), 2);
}
