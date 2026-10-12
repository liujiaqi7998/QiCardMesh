mod apdu;
mod config;
mod daemon;
mod identity;
mod monitor;
mod mqtt;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use monitor::{Backend, Monitor, Pcsc};
use std::{
    io::{self, BufRead},
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};

#[derive(Parser)]
#[command(
    name = "qicardmesh",
    version,
    about = "QiCardMesh PC/SC smart-card CLI"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List PC/SC readers without connecting or sending APDUs.
    Readers,
    /// Show card ATR, device identity, protocol and available firmware/serial.
    CardStatus {
        /// Keep monitoring insertion/removal and automatically reconnect.
        #[arg(short, long)]
        watch: bool,
        /// Reader name (exact match preferred; otherwise case-insensitive substring).
        #[arg(short, long)]
        reader: Option<String>,
    },
    /// Send raw APDUs; without arguments, read one hex APDU per stdin line.
    Transmit {
        /// Quoted hex APDU(s), executed in order within one transaction.
        #[arg(value_name = "APDU")]
        apdus: Vec<String>,
        /// Reader name (required if multiple cards match).
        #[arg(short, long)]
        reader: Option<String>,
    },
    /// Run the long-lived PC/SC + MQTT daemon.
    #[command(alias = "run")]
    Daemon {
        /// Runtime TOML configuration. It is generated when absent.
        #[arg(short, long, default_value_os_t = config::default_path())]
        config: std::path::PathBuf,
    },
}

const INTERVAL: Duration = Duration::from_millis(250);

fn print_events(events: Vec<String>) {
    for event in events {
        println!("{event}");
    }
}

fn print_responses(responses: &[Vec<u8>]) -> Result<bool> {
    let mut success = true;
    for response in responses {
        let (data, sw) = apdu::split_response(response)?;
        println!("response: {}", apdu::hex(response));
        println!("data: {}", apdu::hex(data));
        println!("SW1/SW2: {sw:04X}");
        success &= sw == 0x9000;
    }
    Ok(success)
}

fn run() -> Result<bool> {
    let cli = Cli::parse();
    let running = Arc::new(AtomicBool::new(true));
    let signal = running.clone();
    ctrlc::set_handler(move || signal.store(false, Ordering::Relaxed))?;
    match cli.command {
        Command::Readers => {
            let mut backend = Pcsc::default();
            let readers = backend
                .scan(Duration::ZERO)
                .context("cannot enumerate PC/SC readers; check Smart Card / pcscd service")?;
            if readers.is_empty() {
                println!("No PC/SC readers found.");
            }
            for (index, reader) in readers.iter().enumerate() {
                println!("{index}: {}", reader.name.to_string_lossy());
            }
        }
        Command::CardStatus { watch, reader } => {
            let mut monitor = Monitor::new(Pcsc::default(), reader, true);
            let mut first = true;
            while running.load(Ordering::Relaxed) {
                match monitor.tick(if first { Duration::ZERO } else { INTERVAL }) {
                    Ok(events) => {
                        print_events(events);
                        if first && monitor.readers.is_empty() {
                            println!("No matching PC/SC readers found.");
                        }
                    }
                    Err(error) if watch => {
                        monitor.report_service_error(&error);
                        std::thread::sleep(INTERVAL);
                    }
                    Err(error) => {
                        return Err(error
                            .context("cannot read card status; check Smart Card / pcscd service"));
                    }
                }
                if !watch {
                    break;
                }
                first = false;
            }
        }
        Command::Transmit { apdus, reader } => {
            let mut monitor = Monitor::new(Pcsc::default(), reader, false);
            if !apdus.is_empty() {
                // Validate the whole batch before opening any connections.
                let commands = apdus
                    .iter()
                    .map(|s| apdu::parse(s))
                    .collect::<Result<Vec<_>>>()?;
                monitor.tick(Duration::ZERO)?;
                let replies = monitor
                    .transmit(&commands)
                    .context("APDU failed; connection released; commands were not replayed")?;
                return print_responses(&replies);
            }
            eprintln!("Interactive APDU mode: one hex APDU per line; 'quit' or Ctrl+C to exit.");
            eprintln!(
                "Insert/remove cards freely. After reconnection, SELECT the applet again. Failed commands are not replayed."
            );
            let (sender, receiver) = mpsc::sync_channel(1);
            std::thread::spawn(move || {
                for line in io::stdin().lock().lines() {
                    let failed = line.is_err();
                    if sender.send(line).is_err() || failed {
                        break;
                    }
                }
            });
            let mut first = true;
            while running.load(Ordering::Relaxed) {
                match monitor.tick(if first { Duration::ZERO } else { INTERVAL }) {
                    Ok(events) => {
                        for event in events {
                            eprintln!("{event}");
                        }
                    }
                    Err(error) => {
                        monitor.report_service_error(&error);
                        std::thread::sleep(INTERVAL);
                    }
                }
                first = false;
                match receiver.try_recv() {
                    Ok(line) => {
                        let line = line?;
                        let line = line.trim();
                        if line.eq_ignore_ascii_case("quit") || line.eq_ignore_ascii_case("exit") {
                            break;
                        }
                        if line.is_empty() {
                            continue;
                        }
                        let result =
                            apdu::parse(line).and_then(|command| monitor.transmit(&[command]));
                        match result {
                            Ok(replies) => {
                                print_responses(&replies)?;
                            }
                            Err(error) => eprintln!("error: {error:#}; command not replayed"),
                        }
                    }
                    Err(mpsc::TryRecvError::Empty) => {}
                    Err(mpsc::TryRecvError::Disconnected) => break,
                }
            }
        }
        Command::Daemon { config } => {
            let (config, _) = config::load_or_create(&config)?;
            mqtt::run(config, running.clone())?;
        }
    }
    Ok(true)
}

fn main() -> ExitCode {
    let result = run();
    match result {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(2),
        Err(error) => {
            eprintln!("error: {error:#}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cli_accepts_all_modes_and_batches() {
        for args in [
            vec!["qicardmesh", "readers"],
            vec!["qicardmesh", "card-status", "--watch"],
            vec!["qicardmesh", "transmit"],
            vec![
                "qicardmesh",
                "transmit",
                "--reader",
                "CanoKey",
                "00A4040005F000000000",
                "0031000000",
            ],
        ] {
            assert!(Cli::try_parse_from(args).is_ok());
        }
    }
}
