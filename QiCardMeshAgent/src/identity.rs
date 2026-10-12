use crate::apdu::{hex, read_data};
use anyhow::Result;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    YubiKey,
    CanoKey,
    Unknown,
}

impl std::fmt::Display for Kind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::YubiKey => "YubiKey",
            Self::CanoKey => "CanoKey",
            Self::Unknown => "Unknown",
        })
    }
}

pub fn detect(reader: &str, atr: &[u8]) -> Kind {
    let hint = format!("{reader} {}", String::from_utf8_lossy(atr)).to_ascii_lowercase();
    if hint.contains("canokey") {
        Kind::CanoKey
    } else if hint.contains("yubikey") || hint.contains("yubico") {
        Kind::YubiKey
    } else {
        Kind::Unknown
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Info {
    pub kind: Kind,
    pub evidence: &'static str,
    pub version: Option<String>,
    pub serial: Option<String>,
    pub openpgp_aid: Option<String>,
}

fn openpgp_identity(aid: &[u8]) -> Option<(Kind, String)> {
    if aid.len() != 16 || !aid.starts_with(&[0xd2, 0x76, 0, 1, 0x24, 1]) {
        return None;
    }
    let kind = match &aid[8..10] {
        [0, 6] => Kind::YubiKey,
        [0xf1, 0xd0] => Kind::CanoKey,
        _ => Kind::Unknown,
    };
    Some((kind, hex(&aid[10..14])))
}

pub fn inspect(
    reader: &str,
    atr: &[u8],
    mut send: impl FnMut(&[u8]) -> Result<Vec<u8>>,
) -> Result<Info> {
    let mut info = Info {
        kind: detect(reader, atr),
        evidence: "reader name / ATR (hint)",
        version: None,
        serial: None,
        openpgp_aid: None,
    };
    // Standard read-only OpenPGP discovery also works through generic NFC readers.
    if read_data(&mut send, &[0, 0xa4, 4, 0, 6, 0xd2, 0x76, 0, 1, 0x24, 1])?.is_some()
        && let Some(aid) = read_data(&mut send, &[0, 0xca, 0, 0x4f, 0])?
        && let Some((kind, serial)) = openpgp_identity(&aid)
    {
        if kind != Kind::Unknown {
            info.kind = kind;
            info.evidence = "OpenPGP manufacturer ID";
        }
        info.serial = Some(serial);
        info.openpgp_aid = Some(hex(&aid));
    }
    match info.kind {
        Kind::CanoKey => {
            if read_data(&mut send, &[0, 0xa4, 4, 0, 5, 0xf0, 0, 0, 0, 0])?.is_some() {
                info.version = read_data(&mut send, &[0, 0x31, 0, 0, 0])?
                    .and_then(|v| String::from_utf8(v).ok());
                if let Some(serial) = read_data(&mut send, &[0, 0x32, 0, 0, 0])? {
                    info.serial = Some(hex(&serial));
                }
            }
        }
        Kind::YubiKey => {
            if read_data(&mut send, &[0, 0xa4, 4, 0, 5, 0xa0, 0, 0, 3, 8])?.is_some() {
                info.version = read_data(&mut send, &[0, 0xfd, 0, 0])?
                    .filter(|v| v.len() == 3)
                    .map(|v| format!("{}.{}.{}", v[0], v[1], v[2]));
                if let Some(v) = read_data(&mut send, &[0, 0xf8, 0, 0])?
                    && v.len() == 4
                {
                    info.serial = Some(u32::from_be_bytes([v[0], v[1], v[2], v[3]]).to_string());
                }
            }
        }
        Kind::Unknown => info.evidence = "unrecognized reader / ATR / OpenPGP manufacturer",
    }
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifies_usb_names_and_atr_markers() {
        assert_eq!(detect("Yubico YubiKey OTP+FIDO+CCID 0", &[]), Kind::YubiKey);
        assert_eq!(detect("Canokeys CanoKey 0", &[]), Kind::CanoKey);
        assert_eq!(detect("Generic", b"\x3b\xfdYubiKey"), Kind::YubiKey);
        assert_eq!(detect("Generic", b"\x3bCanoKey"), Kind::CanoKey);
        assert_eq!(detect("Generic", &[0x3b, 0]), Kind::Unknown);
    }
    #[test]
    fn identifies_both_vendors_on_generic_reader() {
        for (manufacturer, kind) in [([0, 6], Kind::YubiKey), ([0xf1, 0xd0], Kind::CanoKey)] {
            let mut aid = vec![0xd2, 0x76, 0, 1, 0x24, 1, 3, 4];
            aid.extend(manufacturer);
            aid.extend([0, 0, 0, 42, 0, 0]);
            let mut responses = vec![
                vec![0x90, 0],
                [aid, vec![0x90, 0]].concat(),
                vec![0x6a, 0x82],
            ]
            .into_iter();
            let info =
                inspect("Generic NFC", &[0x3b, 0], |_| Ok(responses.next().unwrap())).unwrap();
            assert_eq!(info.kind, kind);
            assert_eq!(info.serial.as_deref(), Some("00 00 00 2A"));
        }
    }
    #[test]
    fn unsupported_applets_preserve_name_detection() {
        let info = inspect("CanoKey", &[], |_| Ok(vec![0x6a, 0x82])).unwrap();
        assert_eq!(info.kind, Kind::CanoKey);
        assert!(info.version.is_none());
        assert!(openpgp_identity(&[0; 16]).is_none());
    }
}
