use anyhow::{Result, bail, ensure};

pub fn hex(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|b| format!("{b:02X}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Validate ISO 7816 short/extended cases before touching the reader.
pub fn parse(text: &str) -> Result<Vec<u8>> {
    let compact: String = text
        .chars()
        .filter(|c| !c.is_ascii_whitespace() && *c != ':')
        .collect();
    ensure!(
        compact.is_ascii() && compact.len().is_multiple_of(2),
        "APDU must contain pairs of hexadecimal digits"
    );
    let bytes = compact
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair)?, 16).map_err(Into::into))
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        (4..=65544).contains(&bytes.len()),
        "APDU length must be 4..65544 bytes"
    );
    if bytes.len() <= 5 {
        return Ok(bytes);
    }
    let valid = if bytes[4] != 0 {
        let lc = bytes[4] as usize;
        bytes.len() == 5 + lc || bytes.len() == 6 + lc
    } else if bytes.len() == 7 {
        true // Extended case 2: two-byte Le.
    } else if bytes.len() > 7 {
        let lc = u16::from_be_bytes([bytes[5], bytes[6]]) as usize;
        lc != 0 && (bytes.len() == 7 + lc || bytes.len() == 9 + lc)
    } else {
        false
    };
    ensure!(valid, "APDU Lc/Le does not match its length");
    Ok(bytes)
}

pub fn split_response(response: &[u8]) -> Result<(&[u8], u16)> {
    ensure!(
        response.len() >= 2,
        "card returned a response without SW1/SW2"
    );
    let n = response.len();
    Ok((
        &response[..n - 2],
        u16::from_be_bytes([response[n - 2], response[n - 1]]),
    ))
}

/// Used only for our read-only metadata queries. User APDUs are always sent once.
pub fn read_data(
    mut send: impl FnMut(&[u8]) -> Result<Vec<u8>>,
    command: &[u8],
) -> Result<Option<Vec<u8>>> {
    let mut command = command.to_vec();
    let mut data = Vec::new();
    for _ in 0..32 {
        let response = send(&command)?;
        let (part, sw) = split_response(&response)?;
        if sw >> 8 == 0x6c && command.len() == 5 {
            command[4] = sw as u8;
            continue;
        }
        ensure!(
            data.len() + part.len() <= 65536,
            "metadata response exceeds 64 KiB"
        );
        data.extend_from_slice(part);
        match sw {
            0x9000 => return Ok(Some(data)),
            0x6100..=0x61ff => command = vec![0, 0xc0, 0, 0, sw as u8],
            _ => return Ok(None), // Unsupported/disabled applets are optional.
        }
    }
    bail!("metadata response exceeded the APDU continuation limit")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_short_and_extended_cases() {
        for apdu in [
            "00 A4 04 00",
            "00:CA:00:4F:00",
            "00A4040002D276",
            "00A4040002D27600",
            "00CA004F000100",
            "00DA0000000001AA",
            "00DA0000000001AA0000",
        ] {
            assert!(parse(apdu).is_ok(), "{apdu}");
        }
        for apdu in [
            "",
            "00",
            "00A4040",
            "00GG0400",
            "00A4040002AA",
            "00CA004F0000",
            "00DA000000000000",
            "你好你好",
        ] {
            assert!(parse(apdu).is_err(), "{apdu}");
        }
    }
    #[test]
    fn follows_read_continuations_and_corrected_le() {
        let mut replies = [vec![0x6c, 2], vec![1, 0x61, 1], vec![2, 0x90, 0]].into_iter();
        let mut commands = Vec::new();
        let data = read_data(
            |cmd| {
                commands.push(cmd.to_vec());
                Ok(replies.next().unwrap())
            },
            &[0, 0xca, 0, 0x4f, 0],
        )
        .unwrap();
        assert_eq!(data, Some(vec![1, 2]));
        assert_eq!(commands[1], [0, 0xca, 0, 0x4f, 2]);
        assert_eq!(commands[2], [0, 0xc0, 0, 0, 1]);
    }
    #[test]
    fn bounds_broken_card_responses() {
        assert!(read_data(|_| Ok(vec![0x61, 1]), &[0; 5]).is_err());
        assert!(split_response(&[0x90]).is_err());
        assert_eq!(read_data(|_| Ok(vec![0x6a, 0x82]), &[0; 5]).unwrap(), None);
    }
}
