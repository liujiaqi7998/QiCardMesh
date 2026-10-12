use anyhow::{Context, Result};
use clap::Parser;
#[cfg(feature = "pcsc")]
use qicardmesh_agent::cards::PcscBackend;
#[cfg(not(feature = "pcsc"))]
use qicardmesh_agent::cards::UnavailableBackend;
use qicardmesh_agent::{
    cards::{ApduExecutor, CardBackend, CardMonitor, CardSessionManager},
    config::Config,
    identity,
    mqtt::MqttService,
};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::info;

#[derive(Debug, Parser)]
#[command(
    name = "QiCardMeshAgent",
    about = "QiCardMesh local smart-card MQTT daemon"
)]
struct Args {
    /// Explicit YAML path. By default config.yaml beside the executable is used.
    #[arg(long)]
    config: Option<PathBuf>,
    /// Test TLS and MQTT authentication only, without subscribing or publishing.
    #[arg(long)]
    check_mqtt: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "qicardmesh_agent=info".into()),
        )
        .with_target(false)
        .init();

    let args = Args::parse();
    let config_path = match args.config {
        Some(path) => std::path::absolute(path).context("resolve configuration path")?,
        None => Config::beside_executable()?,
    };
    let config = Config::load_or_create(&config_path)?;
    info!(path = %config_path.display(), "configuration loaded");
    config
        .validate_credentials()
        .with_context(|| format!("invalid MQTT configuration: {}", config_path.display()))?;

    let effective_user_id = identity::effective_user_id(&config.user_id);
    let client_id = identity::mqtt_client_id(&effective_user_id);
    info!(user_id = %effective_user_id, client_id = %client_id, "device identity ready");

    if args.check_mqtt {
        qicardmesh_agent::mqtt::check_connection(&config, &client_id)
            .await
            .with_context(|| {
                format!(
                    "MQTT connection check failed (configuration: {})",
                    config_path.display()
                )
            })?;
        info!("MQTT TLS and authentication succeeded; no topics subscribed or messages published");
        return Ok(());
    }

    let backend: Arc<dyn CardBackend> = {
        #[cfg(feature = "pcsc")]
        {
            Arc::new(PcscBackend)
        }
        #[cfg(not(feature = "pcsc"))]
        {
            Arc::new(UnavailableBackend)
        }
    };

    let sessions = CardSessionManager::default();
    let executor = ApduExecutor::new(Arc::clone(&backend), sessions.clone());
    let monitor = CardMonitor::new(
        backend,
        Duration::from_millis(config.monitor_interval_ms.max(100)),
        sessions.clone(),
    );
    let (card_tx, card_rx) = mpsc::channel(config.request_queue_capacity.max(8));
    let cancel = CancellationToken::new();
    let monitor_cancel = cancel.clone();
    let monitor_task = tokio::spawn(async move { monitor.run(card_tx, monitor_cancel).await });

    let mqtt = MqttService::new(config, effective_user_id, client_id, sessions, executor);
    let mqtt_cancel = cancel.clone();
    let mut mqtt_task = tokio::spawn(async move { mqtt.run(card_rx, mqtt_cancel).await });

    tokio::select! {
        signal = tokio::signal::ctrl_c() => { signal.context("wait for Ctrl-C")?; info!("shutdown requested"); }
        result = &mut mqtt_task => {
            result.context("MQTT task join")??;
            cancel.cancel();
            monitor_task.await.context("card monitor task join")?;
            return Ok(());
        }
    }
    cancel.cancel();
    monitor_task.await.context("card monitor task join")?;
    mqtt_task.await.context("MQTT task join")??;
    Ok(())
}
