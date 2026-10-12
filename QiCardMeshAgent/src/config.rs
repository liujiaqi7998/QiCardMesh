use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

const DEFAULT_CONFIG: &str = r#"# QiCardMesh Agent runtime configuration.
# The file is created automatically on first start. Fill in mqtt.url,
# mqtt.username, mqtt.password and mqtt.user_id, then start `qicardmesh daemon`.

[mqtt]
url = "mqtts://broker.example.com:8883"
username = ""
password = ""
user_id = "replace-me"
client_id = ""
keep_alive_secs = 30
qos = 1

[agent]
poll_interval_ms = 250
max_apdu_bytes = 65544
max_commands_per_request = 32
"#;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Config {
    pub mqtt: MqttConfig,
    #[serde(default)]
    pub agent: AgentConfig,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct MqttConfig {
    pub url: String,
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub password: String,
    pub user_id: String,
    #[serde(default)]
    pub client_id: String,
    #[serde(default = "default_keep_alive")]
    pub keep_alive_secs: u64,
    #[serde(default = "default_qos")]
    pub qos: u8,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AgentConfig {
    #[serde(default = "default_poll_interval")]
    pub poll_interval_ms: u64,
    #[serde(default = "default_max_apdu")]
    pub max_apdu_bytes: usize,
    #[serde(default = "default_max_commands")]
    pub max_commands_per_request: usize,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            poll_interval_ms: 250,
            max_apdu_bytes: 65_544,
            max_commands_per_request: 32,
        }
    }
}
fn default_keep_alive() -> u64 {
    30
}
fn default_qos() -> u8 {
    1
}
fn default_poll_interval() -> u64 {
    250
}
fn default_max_apdu() -> usize {
    65_544
}
fn default_max_commands() -> usize {
    32
}

pub fn default_path() -> PathBuf {
    PathBuf::from("qicardmesh-agent.toml")
}

pub fn load_or_create(path: &Path) -> Result<(Config, bool)> {
    if !path.exists() {
        fs::write(path, DEFAULT_CONFIG)
            .with_context(|| format!("create config {}", path.display()))?;
        bail!(
            "created {}. Fill in MQTT credentials and run the daemon again",
            path.display()
        );
    }
    let source =
        fs::read_to_string(path).with_context(|| format!("read config {}", path.display()))?;
    let config: Config =
        toml::from_str(&source).with_context(|| format!("parse config {}", path.display()))?;
    validate(&config)?;
    Ok((config, false))
}

pub fn validate(config: &Config) -> Result<()> {
    if config.mqtt.url.trim().is_empty() {
        bail!("mqtt.url is required");
    }
    if config.mqtt.user_id.trim().is_empty() || config.mqtt.user_id == "replace-me" {
        bail!("mqtt.user_id must identify this device/user");
    }
    if !(config.mqtt.url.starts_with("mqtt://")
        || config.mqtt.url.starts_with("mqtts://")
        || config.mqtt.url.starts_with("tcp://")
        || config.mqtt.url.starts_with("ssl://"))
    {
        bail!("mqtt.url must use mqtt://, mqtts://, tcp:// or ssl://");
    }
    if config.mqtt.qos > 2 {
        bail!("mqtt.qos must be 0, 1 or 2");
    }
    if config.agent.poll_interval_ms == 0 {
        bail!("agent.poll_interval_ms must be greater than zero");
    }
    if !(4..=65_544).contains(&config.agent.max_apdu_bytes) {
        bail!("agent.max_apdu_bytes must be between 4 and 65544");
    }
    if config.agent.max_commands_per_request == 0 || config.agent.max_commands_per_request > 256 {
        bail!("agent.max_commands_per_request must be between 1 and 256");
    }
    if config.mqtt.user_id.contains('/')
        || config.mqtt.user_id.contains('#')
        || config.mqtt.user_id.contains('+')
        || config.mqtt.user_id.contains('\0')
    {
        bail!("mqtt.user_id may not contain MQTT topic separators");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_deserialize_without_agent_section() {
        let c: Config =
            toml::from_str("[mqtt]\nurl='mqtt://localhost:1883'\nuser_id='alice'\n").unwrap();
        assert_eq!(c.agent.poll_interval_ms, 250);
    }
    #[test]
    fn rejects_topic_injection_and_invalid_scheme() {
        let mut c = Config {
            mqtt: MqttConfig {
                url: "mqtt://localhost".into(),
                username: String::new(),
                password: String::new(),
                user_id: "a/b".into(),
                client_id: String::new(),
                keep_alive_secs: 30,
                qos: 1,
            },
            agent: AgentConfig::default(),
        };
        assert!(validate(&c).is_err());
        c.mqtt.user_id = "alice".into();
        c.mqtt.url = "http://localhost".into();
        assert!(validate(&c).is_err());
    }
}
