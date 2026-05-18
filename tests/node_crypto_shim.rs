//! Unit tests for the node:crypto shim (bd-1av0.3).
//!
//! Tests verify that `createHash`, `createHmac`, `randomUUID`, `randomBytes`,
//! `randomInt`, `timingSafeEqual`, and `getHashes` produce output matching
//! Node.js semantics. Crypto operations delegate to real Rust crates (sha2,
//! sha1, md-5, hmac) via hostcalls registered on the `QuickJS` runtime.

mod common;

use pi::extensions::{
    ExtensionEventName, ExtensionManager, JsExtensionLoadSpec, JsExtensionRuntimeHandle,
};
use pi::extensions_js::PiJsRuntimeConfig;
use pi::tools::ToolRegistry;
use std::sync::Arc;

// ─── Helpers ────────────────────────────────────────────────────────────────

/// Load a JS extension and return the manager.
fn load_ext(harness: &common::TestHarness, source: &str) -> ExtensionManager {
    let cwd = harness.temp_dir().to_path_buf();
    let ext_entry_path = harness.create_file("extensions/crypto_test.mjs", source.as_bytes());
    let spec = JsExtensionLoadSpec::from_entry_path(&ext_entry_path).expect("load spec");

    let manager = ExtensionManager::new();
    let tools = Arc::new(ToolRegistry::new(&[], &cwd, None));
    let js_config = PiJsRuntimeConfig {
        cwd: cwd.display().to_string(),
        ..Default::default()
    };

    let runtime = common::run_async({
        let manager = manager.clone();
        let tools = Arc::clone(&tools);
        async move {
            JsExtensionRuntimeHandle::start(js_config, tools, manager)
                .await
                .expect("start js runtime")
        }
    });
    manager.set_js_runtime(runtime);

    common::run_async({
        let manager = manager.clone();
        async move {
            manager
                .load_js_extensions(vec![spec])
                .await
                .expect("load extension");
        }
    });

    manager
}

/// Build an extension that computes a crypto expression and returns it via
/// the `agent_start` event handler.
fn crypto_ext_source(js_expr: &str) -> String {
    format!(
        r#"
import crypto from "node:crypto";
const {{
  createHash,
  createHmac,
  randomUUID,
  randomBytes,
  randomInt,
  timingSafeEqual,
  getHashes,
  pbkdf2Sync,
  pbkdf2,
  scryptSync,
  createCipheriv,
  createDecipheriv,
  sign,
  verify,
}} = crypto;

export default function activate(pi) {{
  pi.on("agent_start", (event, ctx) => {{
    let result;
    try {{
      result = String({js_expr});
    }} catch (e) {{
      result = "ERROR:" + e.message;
    }}
    return {{ result }};
  }});
}}
"#
    )
}

/// Evaluate a crypto JS expression: loads extension, fires `agent_start`, returns result.
fn eval_crypto(js_expr: &str) -> String {
    let harness = common::TestHarness::new("crypto_shim");
    let source = crypto_ext_source(js_expr);
    let mgr = load_ext(&harness, &source);

    let response = common::run_async(async move {
        mgr.dispatch_event_with_response(ExtensionEventName::AgentStart, None, 10000)
            .await
            .expect("dispatch agent_start")
    });

    response
        .and_then(|v| v.get("result").and_then(|r| r.as_str()).map(String::from))
        .unwrap_or_else(|| "NO_RESPONSE".to_string())
}

// ─── SHA-256 Tests ──────────────────────────────────────────────────────────

#[test]
fn sha256_hello_hex() {
    let result = eval_crypto(r#"createHash("sha256").update("hello").digest("hex")"#);
    assert_eq!(
        result, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "SHA-256 of 'hello' must match Node.js"
    );
}

#[test]
fn sha256_empty_hex() {
    let result = eval_crypto(r#"createHash("sha256").update("").digest("hex")"#);
    assert_eq!(
        result,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
}

#[test]
fn sha256_base64() {
    let result = eval_crypto(r#"createHash("sha256").update("hello").digest("base64")"#);
    assert_eq!(result, "LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ=");
}

#[test]
fn sha256_chained_update() {
    let result =
        eval_crypto(r#"createHash("sha256").update("hello").update(" world").digest("hex")"#);
    assert_eq!(
        result,
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    );
}

// ─── SHA-1 Tests ────────────────────────────────────────────────────────────

#[test]
fn sha1_hello_hex() {
    let result = eval_crypto(r#"createHash("sha1").update("hello").digest("hex")"#);
    assert_eq!(result, "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d");
}

// ─── MD5 Tests ──────────────────────────────────────────────────────────────

#[test]
fn md5_hello_hex() {
    let result = eval_crypto(r#"createHash("md5").update("hello").digest("hex")"#);
    assert_eq!(result, "5d41402abc4b2a76b9719d911017c592");
}

// ─── SHA-384/SHA-512 Tests ─────────────────────────────────────────────────

#[test]
fn sha384_hello_hex() {
    let result = eval_crypto(r#"createHash("sha384").update("hello").digest("hex")"#);
    assert_eq!(
        result,
        "59e1748777448c69de6b800d7a33bbfb9ff1b463e44354c3553bcdb9c666fa90125a3c79f90397bdf5f6a13de828684f"
    );
}

#[test]
fn sha512_hello_hex() {
    let result = eval_crypto(r#"createHash("sha512").update("hello").digest("hex")"#);
    assert_eq!(
        result,
        "9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caadae2dff72519673ca72323c3d99ba5c11d7c7acc6e14b8c5da0c4663475c2e5c3adef46f73bcdec043"
    );
}

// ─── HMAC Tests ─────────────────────────────────────────────────────────────

#[test]
fn hmac_sha256_hex() {
    let result = eval_crypto(r#"createHmac("sha256", "secret").update("hello").digest("hex")"#);
    assert_eq!(
        result,
        "88aab3ede8d3adf94d26ab90d3bafd4a2083070c3bcce9c014ee04a443847c0b"
    );
}

#[test]
fn hmac_sha384_hex() {
    let result = eval_crypto(r#"createHmac("sha384", "secret").update("hello").digest("hex")"#);
    assert_eq!(
        result,
        "7e1e620ca0068fd1fce00c1ad3f5c6dbb12874dd2fb9c26502d09d0d804f2c0ba1d921b9458416cba480417571001e18"
    );
}

#[test]
fn hmac_sha1_hex() {
    let result = eval_crypto(r#"createHmac("sha1", "key").update("data").digest("hex")"#);
    assert_eq!(result, "104152c5bfdca07bc633eebd46199f0255c9f49d");
}

// ─── randomUUID Tests ───────────────────────────────────────────────────────

#[test]
fn random_uuid_format() {
    let result = eval_crypto("randomUUID()");
    let re =
        regex::Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
            .unwrap();
    assert!(
        re.is_match(&result),
        "UUID should be valid v4 format, got: {result}"
    );
}

#[test]
fn random_uuid_uniqueness() {
    let result = eval_crypto("randomUUID() + '|' + randomUUID()");
    let parts: Vec<&str> = result.split('|').collect();
    assert_eq!(parts.len(), 2);
    assert_ne!(parts[0], parts[1], "Two UUIDs should differ");
}

// ─── randomBytes Tests ──────────────────────────────────────────────────────

#[test]
fn random_bytes_length() {
    let result = eval_crypto("randomBytes(16).length");
    assert_eq!(result, "16");
}

#[test]
fn random_bytes_hex_encoding() {
    let result = eval_crypto("randomBytes(4).toString('hex').length");
    assert_eq!(result, "8");
}

#[test]
fn random_bytes_hex_valid() {
    let result = eval_crypto("randomBytes(16).toString('hex')");
    let re = regex::Regex::new(r"^[0-9a-f]{32}$").unwrap();
    assert!(
        re.is_match(&result),
        "randomBytes hex should be 32 hex chars, got: {result}"
    );
}

#[test]
fn random_bytes_rejects_non_integer() {
    let result = eval_crypto(
        r#"(() => {
        try {
            randomBytes(3.5);
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("non-negative integer"),
        "Expected randomBytes to reject non-integer size, got: {result}"
    );
}

// ─── randomInt Tests ────────────────────────────────────────────────────────

#[test]
fn random_int_range() {
    let result = eval_crypto(
        r#"(() => {
        const vals = [];
        for (let i = 0; i < 100; i++) vals.push(randomInt(10, 20));
        return vals.every(v => v >= 10 && v < 20) ? "ok" : "fail:" + JSON.stringify(vals);
    })()"#,
    );
    assert_eq!(result, "ok");
}

#[test]
fn random_int_single_arg() {
    let result = eval_crypto(
        r#"(() => {
        const vals = [];
        for (let i = 0; i < 100; i++) vals.push(randomInt(5));
        return vals.every(v => v >= 0 && v < 5) ? "ok" : "fail:" + JSON.stringify(vals);
    })()"#,
    );
    assert_eq!(result, "ok");
}

#[test]
fn random_int_rejects_min_ge_max() {
    let result = eval_crypto(
        r#"(() => {
        try {
            randomInt(5, 5);
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("min must be less than max"),
        "Expected min>=max to throw, got: {result}"
    );
}

// ─── timingSafeEqual Tests ──────────────────────────────────────────────────

#[test]
fn timing_safe_equal_same() {
    let result = eval_crypto(
        r"(() => {
        const a = new Uint8Array([1, 2, 3, 4]);
        const b = new Uint8Array([1, 2, 3, 4]);
        return timingSafeEqual(a, b);
    })()",
    );
    assert_eq!(result, "true");
}

#[test]
fn timing_safe_equal_different() {
    let result = eval_crypto(
        r"(() => {
        const a = new Uint8Array([1, 2, 3, 4]);
        const b = new Uint8Array([1, 2, 3, 5]);
        return timingSafeEqual(a, b);
    })()",
    );
    assert_eq!(result, "false");
}

#[test]
fn timing_safe_equal_length_mismatch() {
    let result = eval_crypto(
        r#"(() => {
        const a = new Uint8Array([1, 2, 3]);
        const b = new Uint8Array([1, 2, 3, 4]);
        try { timingSafeEqual(a, b); return "no-throw"; } catch(e) { return "threw:" + e.message; }
    })()"#,
    );
    assert!(
        result.contains("threw:"),
        "Should throw on length mismatch, got: {result}"
    );
}

// ─── getHashes Tests ────────────────────────────────────────────────────────

#[test]
fn get_hashes_includes_standard() {
    let result = eval_crypto("JSON.stringify(getHashes().sort())");
    let hashes: Vec<String> = serde_json::from_str(&result).expect("parse JSON");
    assert!(hashes.contains(&"sha256".to_string()));
    assert!(hashes.contains(&"sha384".to_string()));
    assert!(hashes.contains(&"sha1".to_string()));
    assert!(hashes.contains(&"md5".to_string()));
    assert!(hashes.contains(&"sha512".to_string()));
}

// ─── Import style tests ────────────────────────────────────────────────────

#[test]
fn named_import_works() {
    let harness = common::TestHarness::new("crypto_named_import");
    let source = r#"
import { createHash } from "node:crypto";

export default function activate(pi) {
  pi.on("agent_start", (event, ctx) => {
    return { result: createHash("sha256").update("test").digest("hex") };
  });
}
"#;
    let mgr = load_ext(&harness, source);
    let response = common::run_async(async move {
        mgr.dispatch_event_with_response(ExtensionEventName::AgentStart, None, 10000)
            .await
            .expect("dispatch")
    });
    let result = response
        .and_then(|v| v.get("result").and_then(|r| r.as_str()).map(String::from))
        .unwrap_or_default();
    assert_eq!(
        result, "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "SHA-256 of 'test'"
    );
}

#[test]
fn bare_crypto_import_works() {
    let harness = common::TestHarness::new("crypto_bare_import");
    let source = r#"
import crypto from "crypto";

export default function activate(pi) {
  pi.on("agent_start", (event, ctx) => {
    return { result: crypto.createHash("md5").update("abc").digest("hex") };
  });
}
"#;
    let mgr = load_ext(&harness, source);
    let response = common::run_async(async move {
        mgr.dispatch_event_with_response(ExtensionEventName::AgentStart, None, 10000)
            .await
            .expect("dispatch")
    });
    let result = response
        .and_then(|v| v.get("result").and_then(|r| r.as_str()).map(String::from))
        .unwrap_or_default();
    assert_eq!(result, "900150983cd24fb0d6963f7d28e17f72");
}

// ─── Digest returns Buffer-like object ──────────────────────────────────────

#[test]
fn digest_no_encoding_returns_buffer() {
    let result = eval_crypto(
        r#"(() => {
        const buf = createHash("sha256").update("hello").digest();
        return typeof buf.toString === "function" ? buf.toString("hex") : "no-toString";
    })()"#,
    );
    assert_eq!(
        result,
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    );
}

#[test]
fn create_hash_unsupported_encoding_throws() {
    let result = eval_crypto(
        r#"(() => {
        try {
            createHash("sha256").update("hello").digest("utf16le");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("unsupported encoding"),
        "Expected unsupported-encoding error, got: {result}"
    );
}

#[test]
fn create_hmac_unsupported_encoding_throws() {
    let result = eval_crypto(
        r#"(() => {
        try {
            createHmac("sha256", "secret").update("hello").digest("utf16le");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("unsupported encoding"),
        "Expected unsupported-encoding error, got: {result}"
    );
}

#[test]
fn create_hash_second_digest_throws() {
    let result = eval_crypto(
        r#"(() => {
        const hash = createHash("sha256").update("hello");
        hash.digest("hex");
        try {
            hash.digest("hex");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("digest() already called"),
        "Expected repeated digest() to throw, got: {result}"
    );
}

#[test]
fn create_hash_update_after_digest_throws() {
    let result = eval_crypto(
        r#"(() => {
        const hash = createHash("sha256").update("hello");
        hash.digest("hex");
        try {
            hash.update("world");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("digest() already called"),
        "Expected update() after digest() to throw, got: {result}"
    );
}

#[test]
fn create_hmac_second_digest_throws() {
    let result = eval_crypto(
        r#"(() => {
        const hmac = createHmac("sha256", "secret").update("hello");
        hmac.digest("hex");
        try {
            hmac.digest("hex");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("digest() already called"),
        "Expected repeated HMAC digest() to throw, got: {result}"
    );
}

#[test]
fn create_hmac_update_after_digest_throws() {
    let result = eval_crypto(
        r#"(() => {
        const hmac = createHmac("sha256", "secret").update("hello");
        hmac.digest("hex");
        try {
            hmac.update("world");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("digest() already called"),
        "Expected HMAC update() after digest() to throw, got: {result}"
    );
}

#[test]
fn pbkdf2_sync_derives_expected_key() {
    let result =
        eval_crypto(r#"pbkdf2Sync("password", "salt", 1000, 16, "sha256").toString("hex")"#);
    assert_eq!(
        result, "632c2812e46d4604102ba7618e9d6d7d",
        "pbkdf2Sync derived key mismatch"
    );
}

#[test]
fn pbkdf2_sync_accepts_digest_variants() {
    let result =
        eval_crypto(r#"pbkdf2Sync("password", "salt", 1000, 16, "SHA-256").toString("hex")"#);
    assert_eq!(
        result, "632c2812e46d4604102ba7618e9d6d7d",
        "pbkdf2Sync should accept digest variants"
    );
}

#[test]
fn pbkdf2_async_derives_expected_key() {
    let result = eval_crypto(
        r#"(() => {
        let outcome = "no-callback";
        pbkdf2("password", "salt", 1000, 16, "sha256", (err, value) => {
            outcome = err ? "threw:" + err.message : value.toString("hex");
        });
        return outcome;
    })()"#,
    );
    assert_eq!(
        result, "632c2812e46d4604102ba7618e9d6d7d",
        "pbkdf2 async derived key mismatch"
    );
}

#[test]
fn pbkdf2_sync_rejects_large_iterations() {
    let result = eval_crypto(
        r#"(() => {
        try {
            pbkdf2Sync("password", "salt", 1000001, 16, "sha256");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("iterations must be <= 1000000"),
        "Expected pbkdf2Sync to reject large iterations, got: {result}"
    );
}

#[test]
fn scrypt_sync_derives_expected_key() {
    let result = eval_crypto(r#"scryptSync("password", "salt", 16).toString("hex")"#);
    assert_eq!(
        result, "745731af4484f323968969eda289aeee",
        "scryptSync derived key mismatch"
    );
}

#[test]
fn scrypt_sync_accepts_encoding_string() {
    let result = eval_crypto(r#"scryptSync("password", "salt", 16, "hex")"#);
    assert_eq!(
        result, "745731af4484f323968969eda289aeee",
        "scryptSync hex encoding mismatch"
    );
}

#[test]
fn scrypt_sync_accepts_options_encoding() {
    let direct = eval_crypto(r#"scryptSync("password", "salt", 16, "hex")"#);
    let result = eval_crypto(r#"scryptSync("password", "salt", 16, { encoding: "hex" })"#);
    assert_eq!(
        result, direct,
        "scryptSync options encoding should match direct encoding string"
    );
}

#[test]
fn scrypt_sync_rejects_large_n() {
    let result = eval_crypto(
        r#"(() => {
        try {
            scryptSync("password", "salt", 16, { N: 2097152 });
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("N must be <= 2^20"),
        "Expected scryptSync to reject large N, got: {result}"
    );
}

#[test]
fn scrypt_sync_rejects_excessive_memory() {
    let result = eval_crypto(
        r#"(() => {
        try {
            scryptSync("password", "salt", 16, { N: 262144, r: 16, p: 1 });
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("memory limit"),
        "Expected scryptSync to reject excessive memory, got: {result}"
    );
}

#[test]
fn scrypt_sync_rejects_non_positive_rp() {
    let result = eval_crypto(
        r#"(() => {
        try {
            scryptSync("password", "salt", 16, { r: 0 });
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("r/p must be positive"),
        "Expected scryptSync to reject r/p <= 0, got: {result}"
    );
}

#[test]
fn create_cipheriv_unimplemented_throws() {
    let result = eval_crypto(
        r#"(() => {
        try {
            createCipheriv("aes-256-gcm", randomBytes(32), randomBytes(16));
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("createCipheriv is not implemented"),
        "Expected createCipheriv to fail loudly, got: {result}"
    );
}

#[test]
fn create_decipheriv_unimplemented_throws() {
    let result = eval_crypto(
        r#"(() => {
        try {
            createDecipheriv("aes-256-gcm", randomBytes(32), randomBytes(16));
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("createDecipheriv is not implemented"),
        "Expected createDecipheriv to fail loudly, got: {result}"
    );
}

#[test]
fn sign_ed25519_pkcs8_matches_node_vector() {
    let result = eval_crypto(
        r#"(() => {
        const privateKey = `-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIKdNGarTy3x3BLKmHN/4JHUxgXyGLoYEwCQk5lMDT0Wc
-----END PRIVATE KEY-----`;
        return sign(null, "hello", privateKey).toString("hex");
    })()"#,
    );
    assert_eq!(
        result,
        "eb5553cfbe1b7a3acf1c13577e7c1685dddb4193dd089d62123261ba4d1adf7086762ed6b53c4708ee984f6e4b6cd7788fb066d552a97ca5c25fb00efd6d3f05",
        "Ed25519 signature must match Node.js for the fixed PKCS#8 key"
    );
}

#[test]
fn verify_ed25519_spki_accepts_node_vector() {
    let result = eval_crypto(
        r#"(() => {
        const publicKey = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAnXpFnYYkKVmCrFuByPcsUZW6Wno7zMOjk9xlzkYRpoo=
-----END PUBLIC KEY-----`;
        const signature = Uint8Array.from("eb5553cfbe1b7a3acf1c13577e7c1685dddb4193dd089d62123261ba4d1adf7086762ed6b53c4708ee984f6e4b6cd7788fb066d552a97ca5c25fb00efd6d3f05".match(/../g).map((byte) => parseInt(byte, 16)));
        return verify(null, "hello", publicKey, signature);
    })()"#,
    );
    assert_eq!(
        result, "true",
        "Ed25519 verify must accept the Node.js vector"
    );
}

#[test]
fn verify_ed25519_spki_rejects_bad_signature() {
    let result = eval_crypto(
        r#"(() => {
        const publicKey = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAnXpFnYYkKVmCrFuByPcsUZW6Wno7zMOjk9xlzkYRpoo=
-----END PUBLIC KEY-----`;
        const signature = Uint8Array.from("eb5553cfbe1b7a3acf1c13577e7c1685dddb4193dd089d62123261ba4d1adf7086762ed6b53c4708ee984f6e4b6cd7788fb066d552a97ca5c25fb00efd6d3f05".match(/../g).map((byte) => parseInt(byte, 16)));
        signature[0] ^= 1;
        return verify(null, "hello", publicKey, signature);
    })()"#,
    );
    assert_eq!(
        result, "false",
        "Ed25519 verify must reject modified signatures"
    );
}

#[test]
fn sign_rejects_digest_algorithms_for_ed25519_subset() {
    let result = eval_crypto(
        r#"(() => {
        const privateKey = `-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIKdNGarTy3x3BLKmHN/4JHUxgXyGLoYEwCQk5lMDT0Wc
-----END PRIVATE KEY-----`;
        try {
            sign("sha256", "hello", privateKey);
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("only Ed25519"),
        "Expected sign() to fail closed for digest algorithms, got: {result}"
    );
}

#[test]
fn create_hash_without_hostcall_throws() {
    let result = eval_crypto(
        r#"(() => {
        globalThis.__pi_crypto_hash_native = undefined;
        try {
            createHash("sha256").update("hello").digest("hex");
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("createHash not available"),
        "Expected clear missing-hostcall error, got: {result}"
    );
}

#[test]
fn random_bytes_without_hostcall_throws() {
    let result = eval_crypto(
        r#"(() => {
        globalThis.__pi_crypto_random_bytes_native = undefined;
        try {
            randomBytes(8);
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("randomBytes not available"),
        "Expected clear missing-hostcall error, got: {result}"
    );
}

#[test]
fn random_uuid_without_hostcall_throws() {
    let result = eval_crypto(
        r#"(() => {
        globalThis.__pi_crypto_random_uuid_native = undefined;
        try {
            randomUUID();
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("randomUUID not available"),
        "Expected clear missing-hostcall error, got: {result}"
    );
}

#[test]
fn random_int_without_hostcall_throws() {
    let result = eval_crypto(
        r#"(() => {
        globalThis.__pi_crypto_random_int_native = undefined;
        try {
            randomInt(1, 3);
            return "no-throw";
        } catch (e) {
            return "threw:" + e.message;
        }
    })()"#,
    );
    assert!(
        result.contains("randomInt not available"),
        "Expected clear missing-hostcall error, got: {result}"
    );
}
