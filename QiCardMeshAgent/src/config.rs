use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub const DEFAULT_MQTT_URL: &str = "mqtts://mqtt.cyqi.top:8883";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub mqtt: MqttConfig,
    /// The account/user part of the identity. The disk fingerprint is appended
    /// at runtime to make the identity unique per computer.
    pub user_id: String,
    pub monitor_interval_ms: u64,
    pub request_queue_capacity: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct MqttConfig {
    /// Kept in the file for clarity, but the daemon validates and enforces the
    /// protocol endpoint required by QiCardMesh.
    pub url: String,
    pub username: String,
    pub password: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mqtt: MqttConfig::default(),
            user_id: "user_id".to_owned(),
            monitor_interval_ms: 1_000,
            request_queue_capacity: 128,
        }
    }
}

impl Default for MqttConfig {
    fn default() -> Self {
        Self {
            url: DEFAULT_MQTT_URL.to_owned(),
            username: String::new(),
            password: String::new(),
        }
    }
}

impl Config {
    pub fn validate_credentials(&self) -> Result<()> {
        ensure!(
            !self.mqtt.username.trim().is_empty() && !self.mqtt.password.is_empty(),
            "MQTT username/password are empty in the loaded configuration; fill mqtt.username and mqtt.password in that file, or select your configuration with --config <PATH>"
        );
        Ok(())
    }

    pub fn load_or_create(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        if !path.exists() {
            let config = Self::default();
            let text = serde_yaml::to_string(&config).context("serialize default YAML config")?;
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)
                    .with_context(|| format!("create config directory {}", parent.display()))?;
            }
            fs::write(path, text).with_context(|| format!("write config {}", path.display()))?;
            return Ok(config);
        }
        let text =
            fs::read_to_string(path).with_context(|| format!("read config {}", path.display()))?;
        let mut config: Self = serde_yaml::from_str(&text).context("parse YAML config")?;
        // The broker is part of the wire contract and cannot be redirected by
        // an accidental stale config file.
        config.mqtt.url = DEFAULT_MQTT_URL.to_owned();
        Ok(config)
    }

    pub fn beside_executable() -> Result<PathBuf> {
        let exe = std::env::current_exe().context("find executable path")?;
        Ok(exe
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("config.yaml"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn empty_credentials_fail_before_connecting() {
        let mut config = Config::default();
        assert!(config.validate_credentials().is_err());
        config.mqtt.username = "test-user".into();
        assert!(config.validate_credentials().is_err());
        config.mqtt.password = " a password with spaces ".into();
        assert!(config.validate_credentials().is_ok());
        assert_eq!(config.mqtt.password, " a password with spaces ");
    }

    #[test]
    fn creates_and_loads_default_yaml() {
        let path = std::env::temp_dir().join(format!(
            "qicardmesh-config-{}.yaml",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let config = Config::load_or_create(&path).unwrap();
        assert_eq!(config.mqtt.url, DEFAULT_MQTT_URL);
        assert!(path.exists());
        let _ = std::fs::remove_file(path);
    }
}
