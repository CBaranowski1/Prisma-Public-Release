use rand::RngCore;

/// Generate a cryptographically random, memory-only auth token: 32 random
/// bytes rendered as 64 lowercase hex chars. Never persisted to disk.
pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_is_64_hex_chars() {
        let t = generate_token();
        assert_eq!(t.len(), 64, "expected 32 bytes => 64 hex chars");
        assert!(t
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn tokens_are_unique() {
        assert_ne!(generate_token(), generate_token());
    }
}
