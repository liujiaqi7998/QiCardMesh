use sha2::{Digest, Sha256};
#[cfg(not(windows))]
use std::fs;
use std::process::Command;

/// Return a stable, non-secret fingerprint of the machine's primary storage.
/// Windows uses the physical disk serial exposed by CIM; Unix uses the machine
/// id (which is stored with the root volume). A hostname fallback keeps first
/// boot and constrained containers usable.
pub fn disk_id() -> String {
    let source = disk_identity_source();
    let digest = Sha256::digest(source.as_bytes());
    hex::encode(&digest[..16])
}

fn disk_identity_source() -> String {
    #[cfg(windows)]
    {
        if let Ok(output) = Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_DiskDrive | Select-Object -First 1 -ExpandProperty SerialNumber)"])
            .output()
        {
            let value = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            if !value.is_empty() { return value; }
        }
    }
    #[cfg(not(windows))]
    {
        for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"] {
            if let Ok(value) = fs::read_to_string(path) {
                if !value.trim().is_empty() {
                    return value;
                }
            }
        }
    }
    let hostname = std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "qicardmesh-host".to_owned());
    format!("{hostname}:{}", std::env::consts::OS)
}

pub fn effective_user_id(configured_user_id: &str) -> String {
    let prefix = if configured_user_id.trim().is_empty() {
        "user_id"
    } else {
        configured_user_id.trim()
    };
    format!("{prefix}:{}", disk_id())
}

pub fn mqtt_client_id(user_id: &str) -> String {
    user_id.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn client_id_has_protocol_prefix() {
        assert_eq!(mqtt_client_id("user_id:abc"), "user_id:abc");
    }

    #[test]
    fn disk_id_is_a_stable_hex_fingerprint() {
        let id = disk_id();
        assert_eq!(id.len(), 32);
        assert!(id.bytes().all(|c| c.is_ascii_hexdigit()));
    }
}
