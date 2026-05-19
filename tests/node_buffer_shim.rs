//! Unit tests for the node:buffer (`Buffer`) shim (bd-1av0.6).
//!
//! Tests verify that `Buffer` follows Node.js semantics: `from`/`alloc`/`concat`
//! factory methods, encoding/decoding (utf8, base64, hex, latin1), `isBuffer`,
//! `byteLength`, `write`, `copy`, `compare`, `equals`, `indexOf`, `lastIndexOf`,
//! `includes`, `fill`, `toJSON`, `slice`, byte swaps, and integer read/write methods.

mod common;

use pi::extensions::{
    ExtensionEventName, ExtensionManager, JsExtensionLoadSpec, JsExtensionRuntimeHandle,
};
use pi::extensions_js::PiJsRuntimeConfig;
use pi::tools::ToolRegistry;
use std::sync::Arc;

// ─── Helpers ────────────────────────────────────────────────────────────────

fn load_ext(harness: &common::TestHarness, source: &str) -> ExtensionManager {
    let cwd = harness.temp_dir().to_path_buf();
    let ext_entry_path = harness.create_file("extensions/buffer_test.mjs", source.as_bytes());
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

fn buffer_ext_source(js_expr: &str) -> String {
    format!(
        r#"
import {{ Buffer }} from "node:buffer";

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

fn global_buffer_ext_source(js_expr: &str) -> String {
    format!(
        r#"
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

fn eval_buffer(js_expr: &str) -> String {
    let harness = common::TestHarness::new("buffer_shim");
    let source = buffer_ext_source(js_expr);
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

fn eval_global_buffer(js_expr: &str) -> String {
    let harness = common::TestHarness::new("global_buffer_shim");
    let source = global_buffer_ext_source(js_expr);
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

// ─── Buffer.from + toString: UTF-8 ─────────────────────────────────────────

#[test]
fn from_string_utf8_roundtrip() {
    let result = eval_buffer(r#"Buffer.from("hello").toString()"#);
    assert_eq!(result, "hello");
}

#[test]
fn from_string_utf8_explicit() {
    let result = eval_buffer(r#"Buffer.from("hello", "utf8").toString("utf8")"#);
    assert_eq!(result, "hello");
}

// ─── Buffer.from + toString: base64 ────────────────────────────────────────

#[test]
fn from_string_base64_encode() {
    let result = eval_buffer(r#"Buffer.from("hello").toString("base64")"#);
    assert_eq!(result, "aGVsbG8=");
}

#[test]
fn from_base64_decode() {
    let result = eval_buffer(r#"Buffer.from("aGVsbG8=", "base64").toString("utf8")"#);
    assert_eq!(result, "hello");
}

// ─── Buffer.from + toString: hex ────────────────────────────────────────────

#[test]
fn from_string_hex_encode() {
    let result = eval_buffer(r#"Buffer.from("hello").toString("hex")"#);
    assert_eq!(result, "68656c6c6f");
}

#[test]
fn from_hex_decode() {
    let result = eval_buffer(r#"Buffer.from("68656c6c6f", "hex").toString("utf8")"#);
    assert_eq!(result, "hello");
}

#[test]
fn from_hex_truncates_at_invalid_or_incomplete_pair_like_node() {
    let result = eval_buffer(
        r#"(() => {
        const cases = ["1ag123", "1a7", "zz", "61 62", "61xz62", "0"];
        return cases.map((input) => [
            input,
            Buffer.from(input, "hex").toString("hex"),
            Buffer.byteLength(input, "hex"),
        ].join(":")).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "1ag123:1a:3|1a7:1a:1|zz::1|61 62:61:2|61xz62:61:3|0::0"
    );
}

// ─── Buffer.from + toString: latin1 ────────────────────────────────────────

#[test]
fn from_string_latin1_encode() {
    let result = eval_buffer(r#"Buffer.from("hello", "latin1").toString("latin1")"#);
    assert_eq!(result, "hello");
}

#[test]
fn single_byte_encodings_match_node_vectors() {
    let result = eval_buffer(
        r#"(() => {
        const latin1 = Buffer.from("\u00ffA", "latin1");
        const binary = Buffer.from("\u00ffA", "binary");
        const ascii = Buffer.from("\u00ffA", "ascii");
        const latin1Codes = Array.from(latin1.toString("latin1")).map((ch) => ch.charCodeAt(0).toString(16)).join(",");
        const binaryCodes = Array.from(binary.toString("binary")).map((ch) => ch.charCodeAt(0).toString(16)).join(",");
        const asciiCodes = Array.from(ascii.toString("ascii")).map((ch) => ch.charCodeAt(0).toString(16)).join(",");
        return [
            latin1.toString("hex"),
            binary.toString("hex"),
            ascii.toString("hex"),
            latin1Codes,
            binaryCodes,
            asciiCodes,
            Buffer.byteLength("\u00ffA", "latin1"),
            Buffer.byteLength("\u00ffA", "binary"),
            Buffer.byteLength("\u00ffA", "ascii"),
            Buffer.byteLength("\u00ffA", "utf8"),
        ].join("|");
    })()"#,
    );
    assert_eq!(result, "ff41|ff41|ff41|ff,41|ff,41|7f,41|2|2|2|3");
}

#[test]
fn utf16le_alias_encodings_match_node_vectors() {
    let result = eval_buffer(
        r#"(() => {
        const aliases = ["utf16le", "utf-16le", "ucs2", "ucs-2"];
        return aliases.map((enc) => {
            const from = Buffer.from("A\u2603", enc);
            const written = Buffer.alloc(4);
            const bytesWritten = written.write("A\u2603", 0, 4, enc);
            const codes = from.toString(enc).split("").map((ch) => ch.charCodeAt(0).toString(16)).join(",");
            return [enc, from.toString("hex"), codes, Buffer.byteLength("A\u2603", enc), Buffer.isEncoding(enc), bytesWritten, written.toString("hex")].join(":");
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "utf16le:41000326:41,2603:4:true:4:41000326|utf-16le:41000326:41,2603:4:true:4:41000326|ucs2:41000326:41,2603:4:true:4:41000326|ucs-2:41000326:41,2603:4:true:4:41000326"
    );
}

// ─── Buffer.from(array) ────────────────────────────────────────────────────

#[test]
fn from_array() {
    let result = eval_buffer(r"Buffer.from([104, 101, 108, 108, 111]).toString()");
    assert_eq!(result, "hello");
}

// ─── Buffer.alloc ──────────────────────────────────────────────────────────

#[test]
fn alloc_zero_filled() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(5);
        return buf.every(b => b === 0) && buf.length === 5;
    })()",
    );
    assert_eq!(result, "true");
}

#[test]
fn alloc_with_fill() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(3, 0x41);
        return buf.toString();
    })()",
    );
    assert_eq!(result, "AAA");
}

// ─── Buffer.isBuffer ───────────────────────────────────────────────────────

#[test]
fn is_buffer_true() {
    let result = eval_buffer(r"Buffer.isBuffer(Buffer.alloc(0))");
    assert_eq!(result, "true");
}

#[test]
fn is_buffer_false_for_uint8array() {
    let result = eval_buffer(r"Buffer.isBuffer(new Uint8Array(0))");
    assert_eq!(result, "false");
}

// ─── Buffer.byteLength ────────────────────────────────────────────────────

#[test]
fn byte_length_utf8() {
    let result = eval_buffer(r#"Buffer.byteLength("hello", "utf8")"#);
    assert_eq!(result, "5");
}

#[test]
fn byte_length_base64() {
    let result = eval_buffer(r#"Buffer.byteLength("aGVsbG8=", "base64")"#);
    assert_eq!(result, "5");
}

// ─── Buffer.concat ─────────────────────────────────────────────────────────

#[test]
fn concat_two_buffers() {
    let result =
        eval_buffer(r#"Buffer.concat([Buffer.from("hel"), Buffer.from("lo")]).toString()"#);
    assert_eq!(result, "hello");
}

#[test]
fn concat_with_total_length() {
    let result =
        eval_buffer(r#"Buffer.concat([Buffer.from("hello"), Buffer.from("world")], 5).toString()"#);
    assert_eq!(result, "hello");
}

// ─── buf.write ─────────────────────────────────────────────────────────────

#[test]
fn write_into_buffer() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.alloc(5);
        buf.write("hi");
        return buf.toString();
    })()"#,
    );
    // "hi" + 3 null bytes renders as "hi\0\0\0" but toString utf8 stops at the nulls
    // Actually Node.js returns "hi\0\0\0" — let's test the first 2 bytes
    assert!(result.starts_with("hi"), "expected 'hi...' got: {result}");
}

#[test]
fn write_accepts_encoding_overload_for_single_byte_strings() {
    let result = eval_buffer(
        r#"(() => {
        const latin1 = Buffer.alloc(2);
        const ascii = Buffer.alloc(2);
        return [
            latin1.write("\u00ffA", "latin1"),
            latin1.toString("hex"),
            ascii.write("\u00ffA", "ascii"),
            ascii.toString("hex"),
        ].join("|");
    })()"#,
    );
    assert_eq!(result, "2|ff41|2|ff41");
}

// ─── buf.slice ─────────────────────────────────────────────────────────────

#[test]
fn slice_returns_buffer() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.from("hello world");
        const sliced = buf.slice(0, 5);
        return Buffer.isBuffer(sliced) + ":" + sliced.toString();
    })()"#,
    );
    assert_eq!(result, "true:hello");
}

// ─── buf.copy ──────────────────────────────────────────────────────────────

#[test]
fn copy_between_buffers() {
    let result = eval_buffer(
        r#"(() => {
        const src = Buffer.from("hello");
        const dst = Buffer.alloc(5);
        src.copy(dst);
        return dst.toString();
    })()"#,
    );
    assert_eq!(result, "hello");
}

// ─── buf.compare / buf.equals ──────────────────────────────────────────────

#[test]
fn compare_equal() {
    let result = eval_buffer(r#"Buffer.from("abc").compare(Buffer.from("abc"))"#);
    assert_eq!(result, "0");
}

#[test]
fn compare_less() {
    let result = eval_buffer(r#"Buffer.from("abc").compare(Buffer.from("abd"))"#);
    assert_eq!(result, "-1");
}

#[test]
fn equals_true() {
    let result = eval_buffer(r#"Buffer.from("hello").equals(Buffer.from("hello"))"#);
    assert_eq!(result, "true");
}

#[test]
fn equals_false() {
    let result = eval_buffer(r#"Buffer.from("hello").equals(Buffer.from("world"))"#);
    assert_eq!(result, "false");
}

// ─── buf.indexOf / buf.includes ────────────────────────────────────────────

#[test]
fn index_of_byte() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.from([1, 2, 3, 4, 5]);
        return buf.indexOf(3);
    })()",
    );
    assert_eq!(result, "2");
}

#[test]
fn index_of_string() {
    let result = eval_buffer(r#"Buffer.from("hello world").indexOf("world")"#);
    assert_eq!(result, "6");
}

#[test]
fn index_of_negative_offset_matches_node() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.from("abc");
        return [buf.indexOf("a", -1), buf.indexOf("c", -1), buf.indexOf(97, -1)].join(",");
    })()"#,
    );
    assert_eq!(result, "-1,2,-1");
}

#[test]
fn index_of_string_encoding_overload() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.from("hello");
        return [buf.indexOf("6c6c", "hex"), buf.includes("6c6c", "hex")].join(",");
    })()"#,
    );
    assert_eq!(result, "2,true");
}

#[test]
fn includes_true() {
    let result = eval_buffer(r#"Buffer.from("hello world").includes("world")"#);
    assert_eq!(result, "true");
}

#[test]
fn includes_false() {
    let result = eval_buffer(r#"Buffer.from("hello").includes("xyz")"#);
    assert_eq!(result, "false");
}

#[test]
fn includes_negative_offset_matches_node() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.from("abc");
        return [buf.includes("a", -1), buf.includes("c", -1)].join(",");
    })()"#,
    );
    assert_eq!(result, "false,true");
}

#[test]
fn last_index_of_vectors_match_node() {
    let result = eval_buffer(
        r#"(() => {
        const b = Buffer.from("abcabc");
        const hello = Buffer.from("hello");
        const cases = [
            ["str_default", () => b.lastIndexOf("bc")],
            ["str_offset3", () => b.lastIndexOf("bc", 3)],
            ["str_neg1", () => b.lastIndexOf("bc", -1)],
            ["num", () => b.lastIndexOf(0x61)],
            ["num_offset2", () => b.lastIndexOf(0x61, 2)],
            ["hex", () => hello.lastIndexOf("6c6c", "hex")],
            ["empty_default", () => b.lastIndexOf("")],
            ["empty_offset2", () => b.lastIndexOf("", 2)],
            ["uint8", () => b.lastIndexOf(new Uint8Array([98, 99]))],
            ["missing", () => b.lastIndexOf("zz")],
        ];
        return cases.map(([label, run]) => label + ":" + run()).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "str_default:4|str_offset3:1|str_neg1:4|num:3|num_offset2:0|hex:2|empty_default:6|empty_offset2:2|uint8:4|missing:-1"
    );
}

#[test]
fn byte_swap_vectors_match_node() {
    let result = eval_buffer(
        r#"(() => {
        const cases = [
            ["swap16_ok", () => Buffer.from([1, 2, 3, 4]).swap16().toString("hex")],
            ["swap16_self", () => { const b = Buffer.from([1, 2]); return b.swap16() === b; }],
            ["swap16_bad", () => Buffer.from([1, 2, 3]).swap16()],
            ["swap32_ok", () => Buffer.from([1, 2, 3, 4, 5, 6, 7, 8]).swap32().toString("hex")],
            ["swap32_bad", () => Buffer.from([1, 2]).swap32()],
            ["swap64_ok", () => Buffer.from([1, 2, 3, 4, 5, 6, 7, 8]).swap64().toString("hex")],
            ["swap64_bad", () => Buffer.from([1, 2, 3, 4]).swap64()],
        ];
        return cases.map(([label, run]) => {
            try {
                return label + ":" + run();
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "swap16_ok:02010403|swap16_self:true|swap16_bad:RangeError|swap32_ok:0403020108070605|swap32_bad:RangeError|swap64_ok:0807060504030201|swap64_bad:RangeError"
    );
}

// ─── buf.fill ──────────────────────────────────────────────────────────────

#[test]
fn fill_with_byte() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(3);
        buf.fill(65);
        return buf.toString();
    })()",
    );
    assert_eq!(result, "AAA");
}

#[test]
fn fill_with_string() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.alloc(6);
        buf.fill("ab");
        return buf.toString();
    })()"#,
    );
    assert_eq!(result, "ababab");
}

// ─── buf.toJSON ────────────────────────────────────────────────────────────

#[test]
fn to_json_format() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.from([1, 2, 3]);
        const json = buf.toJSON();
        return json.type + ':' + JSON.stringify(json.data);
    })()",
    );
    assert_eq!(result, "Buffer:[1,2,3]");
}

// ─── Integer read/write ────────────────────────────────────────────────────

#[test]
fn read_write_uint8() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(1);
        buf.writeUInt8(42, 0);
        return buf.readUInt8(0);
    })()",
    );
    assert_eq!(result, "42");
}

#[test]
fn read_write_uint16_le() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(2);
        buf.writeUInt16LE(0x0102, 0);
        return buf.readUInt16LE(0);
    })()",
    );
    assert_eq!(result, "258");
}

#[test]
fn read_write_uint32_be() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.alloc(4);
        buf.writeUInt32BE(0x01020304, 0);
        return buf.readUInt32BE(0);
    })()",
    );
    assert_eq!(result, "16909060");
}

// ─── Buffer.isEncoding ─────────────────────────────────────────────────────

#[test]
fn is_encoding_valid() {
    let result = eval_buffer(
        r#"[Buffer.isEncoding("utf8"), Buffer.isEncoding("hex"), Buffer.isEncoding("base64")].join(",")"#,
    );
    assert_eq!(result, "true,true,true");
}

#[test]
fn is_encoding_invalid() {
    let result = eval_buffer(r#"Buffer.isEncoding("foobar")"#);
    assert_eq!(result, "false");
}

#[test]
fn unknown_encoding_strict_entrypoints_match_node() {
    let result = eval_buffer(
        r#"(() => {
        const outcomes = [];
        try { Buffer.from("abc", "bogus"); outcomes.push("from:ok"); }
        catch (e) { outcomes.push("from:" + e.message); }
        try { Buffer.from([0x61]).toString("bogus"); outcomes.push("toString:ok"); }
        catch (e) { outcomes.push("toString:" + e.message); }
        try { Buffer.alloc(3).write("abc", 0, 3, "bogus"); outcomes.push("write:ok"); }
        catch (e) { outcomes.push("write:" + e.message); }
        outcomes.push("byteLength:" + Buffer.byteLength("abc", "bogus"));
        outcomes.push("isEncoding:" + Buffer.isEncoding("bogus"));
        return outcomes.join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "from:Unknown encoding: bogus|toString:Unknown encoding: bogus|write:Unknown encoding: bogus|byteLength:3|isEncoding:false"
    );
}

// ─── Import styles ─────────────────────────────────────────────────────────

#[test]
fn default_import_works() {
    let harness = common::TestHarness::new("buffer_default_import");
    let source = r#"
import buffer from "node:buffer";
const { Buffer } = buffer;

export default function activate(pi) {
  pi.on("agent_start", (event, ctx) => {
    return { result: Buffer.from("test").toString("hex") };
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
    assert_eq!(result, "74657374");
}

#[test]
fn bare_buffer_import_works() {
    let harness = common::TestHarness::new("buffer_bare_import");
    let source = r#"
import { Buffer } from "buffer";

export default function activate(pi) {
  pi.on("agent_start", (event, ctx) => {
    return { result: Buffer.from("hi").toString("base64") };
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
    assert_eq!(result, "aGk=");
}

// ─── Global Buffer availability ────────────────────────────────────────────

#[test]
fn global_buffer_available() {
    let result = eval_buffer(r"typeof globalThis.Buffer === 'function'");
    assert_eq!(result, "true");
}

#[test]
fn global_buffer_search_semantics_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const abc = Buffer.from("abc");
        const hello = Buffer.from("hello");
        return [abc.indexOf("a", -1), hello.indexOf("6c6c", "hex"), abc.includes("a", -1)].join(",");
    })()"#,
    );
    assert_eq!(result, "-1,2,false");
}

#[test]
fn global_buffer_last_index_of_vectors_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const b = Buffer.from("abcabc");
        const hello = Buffer.from("hello");
        const cases = [
            ["str_default", () => b.lastIndexOf("bc")],
            ["str_offset3", () => b.lastIndexOf("bc", 3)],
            ["str_neg1", () => b.lastIndexOf("bc", -1)],
            ["num", () => b.lastIndexOf(0x61)],
            ["num_offset2", () => b.lastIndexOf(0x61, 2)],
            ["hex", () => hello.lastIndexOf("6c6c", "hex")],
            ["empty_default", () => b.lastIndexOf("")],
            ["empty_offset2", () => b.lastIndexOf("", 2)],
            ["uint8", () => b.lastIndexOf(new Uint8Array([98, 99]))],
            ["missing", () => b.lastIndexOf("zz")],
        ];
        return cases.map(([label, run]) => label + ":" + run()).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "str_default:4|str_offset3:1|str_neg1:4|num:3|num_offset2:0|hex:2|empty_default:6|empty_offset2:2|uint8:4|missing:-1"
    );
}

#[test]
fn global_buffer_hex_truncates_at_invalid_or_incomplete_pair_like_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = ["1ag123", "1a7", "zz", "61 62", "61xz62", "0"];
        return cases.map((input) => [
            input,
            Buffer.from(input, "hex").toString("hex"),
            Buffer.byteLength(input, "hex"),
        ].join(":")).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "1ag123:1a:3|1a7:1a:1|zz::1|61 62:61:2|61xz62:61:3|0::0"
    );
}

#[test]
fn global_buffer_string_and_buffer_fill_match_node_vectors() {
    let result = eval_global_buffer(
        r#"(() => {
        const allocString = Buffer.alloc(5, "ab").toString("hex");
        const allocHex = Buffer.alloc(4, "61", "hex").toString("hex");
        const fillString = Buffer.alloc(5);
        fillString.fill("ab", 1, 5);
        const fillHex = Buffer.alloc(5);
        fillHex.fill("61", 1, 5, "hex");
        const fillBuffer = Buffer.alloc(5);
        fillBuffer.fill(Buffer.from([1, 2]), 1, 5);
        return [
            allocString,
            allocHex,
            fillString.toString("hex"),
            fillHex.toString("hex"),
            fillBuffer.toString("hex"),
        ].join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "6162616261|61616161|0061626162|0061616161|0001020102"
    );
}

#[test]
fn global_buffer_arraybuffer_offset_length_match_node_vectors() {
    let result = eval_global_buffer(
        r#"(() => {
        const ab = new Uint8Array([1, 2, 3, 4, 5]).buffer;
        const cases = [];
        for (const [label, args] of [
            ["all", [ab]],
            ["offset", [ab, 1]],
            ["offset_len", [ab, 1, 2]],
            ["oversize_len", [ab, 4, 99]],
            ["negative_offset", [ab, -1]],
            ["oob_offset", [ab, 6]],
        ]) {
            try {
                cases.push(label + ":" + Buffer.from(...args).toString("hex"));
            } catch (e) {
                cases.push(label + ":" + e.name);
            }
        }
        return cases.join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "all:0102030405|offset:02030405|offset_len:0203|oversize_len:RangeError|negative_offset:RangeError|oob_offset:RangeError"
    );
}

#[test]
fn global_buffer_array_like_inputs_match_node_vectors() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["plain", { 0: 65, 1: 66, length: 2 }],
            ["mask", { 0: 257, 1: -1, length: 2 }],
            ["empty", { length: 0 }],
        ];
        return cases.map(([label, value]) => {
            try {
                return label + ":" + Buffer.from(value).toString("hex");
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(result, "plain:4142|mask:01ff|empty:");
}

#[test]
fn global_buffer_concat_rejects_non_arrays_like_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["empty", () => Buffer.concat([]).toString("hex")],
            ["not_array_string", () => Buffer.concat("abc").toString("hex")],
            ["not_array_uint8", () => Buffer.concat(new Uint8Array([1, 2])).toString("hex")],
        ];
        return cases.map(([label, run]) => {
            try {
                return label + ":" + run();
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "empty:|not_array_string:TypeError|not_array_uint8:TypeError"
    );
}

#[test]
fn global_buffer_slice_and_subarray_are_shared_buffer_views_like_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const buf = Buffer.from([1, 2, 3, 4]);
        const sliced = buf.slice(1, 3);
        sliced[0] = 9;
        const sub = buf.subarray(2, 4);
        sub[0] = 8;
        return [
            sliced.toString("hex"),
            buf.toString("hex"),
            Buffer.isBuffer(sliced),
            Buffer.isBuffer(sub),
        ].join("|");
    })()"#,
    );
    assert_eq!(result, "0908|01090804|true|true");
}

#[test]
fn global_buffer_compare_and_equals_type_validation_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["equals_buf", () => Buffer.from("a").equals(Buffer.from("a"))],
            ["equals_uint8", () => Buffer.from("a").equals(new Uint8Array([97]))],
            ["compare_uint8", () => Buffer.from("a").compare(new Uint8Array([97]))],
            ["static_compare_uint8", () => Buffer.compare(Buffer.from("a"), new Uint8Array([98]))],
            ["equals_string", () => Buffer.from("a").equals("a")],
            ["compare_string", () => Buffer.from("a").compare("a")],
            ["static_compare_string", () => Buffer.compare(Buffer.from("a"), "a")],
        ];
        return cases.map(([label, run]) => {
            try {
                return label + ":" + run();
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "equals_buf:true|equals_uint8:true|compare_uint8:0|static_compare_uint8:-1|equals_string:TypeError|compare_string:TypeError|static_compare_string:TypeError"
    );
}

#[test]
fn global_buffer_compare_range_vectors_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["range_equal", () => Buffer.from("abcdef").compare(Buffer.from("zzbcdzz"), 2, 5, 1, 4)],
            ["range_less", () => Buffer.from("abc").compare(Buffer.from("abd"), 0, 3, 0, 3)],
            ["range_greater", () => Buffer.from("abd").compare(Buffer.from("abc"), 0, 3, 0, 3)],
            ["empty_slices", () => Buffer.from("abc").compare(Buffer.from("xyz"), 1, 1, 2, 2)],
            ["uint8_range", () => Buffer.from("abc").compare(new Uint8Array([120, 97, 98, 99]), 1, 4, 0, 3)],
        ];
        return cases.map(([label, run]) => label + ":" + run()).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "range_equal:0|range_less:-1|range_greater:1|empty_slices:0|uint8_range:0"
    );
}

#[test]
fn global_buffer_byte_swap_vectors_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["swap16_ok", () => Buffer.from([1, 2, 3, 4]).swap16().toString("hex")],
            ["swap16_self", () => { const b = Buffer.from([1, 2]); return b.swap16() === b; }],
            ["swap16_bad", () => Buffer.from([1, 2, 3]).swap16()],
            ["swap32_ok", () => Buffer.from([1, 2, 3, 4, 5, 6, 7, 8]).swap32().toString("hex")],
            ["swap32_bad", () => Buffer.from([1, 2]).swap32()],
            ["swap64_ok", () => Buffer.from([1, 2, 3, 4, 5, 6, 7, 8]).swap64().toString("hex")],
            ["swap64_bad", () => Buffer.from([1, 2, 3, 4]).swap64()],
        ];
        return cases.map(([label, run]) => {
            try {
                return label + ":" + run();
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "swap16_ok:02010403|swap16_self:true|swap16_bad:RangeError|swap32_ok:0403020108070605|swap32_bad:RangeError|swap64_ok:0807060504030201|swap64_bad:RangeError"
    );
}

#[test]
fn global_buffer_signed_integer_vectors_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const b = Buffer.alloc(12);
        const parts = [];
        parts.push("writeInt8:" + b.writeInt8(-1, 0));
        parts.push("writeInt16LE:" + b.writeInt16LE(-2, 1));
        parts.push("writeInt16BE:" + b.writeInt16BE(-3, 3));
        parts.push("writeInt32LE:" + b.writeInt32LE(-4, 5));
        parts.push("writeInt32BE:" + b.writeInt32BE(-5, 8));
        parts.push("hex:" + b.toString("hex"));
        parts.push("readInt8:" + b.readInt8(0));
        parts.push("readInt16LE:" + b.readInt16LE(1));
        parts.push("readInt16BE:" + b.readInt16BE(3));
        parts.push("readInt32LE:" + b.readInt32LE(5));
        parts.push("readInt32BE:" + b.readInt32BE(8));
        return parts.join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "writeInt8:1|writeInt16LE:3|writeInt16BE:5|writeInt32LE:9|writeInt32BE:12|hex:fffefffffdfcfffffffffffb|readInt8:-1|readInt16LE:-2|readInt16BE:-3|readInt32LE:-4|readInt32BE:-5"
    );
}

#[test]
fn global_buffer_integer_bounds_vectors_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const cases = [
            ["readUInt8_oob", () => Buffer.from([1]).readUInt8(1)],
            ["readUInt16LE_short", () => Buffer.from([1]).readUInt16LE(0)],
            ["readInt32BE_short", () => Buffer.from([1, 2, 3]).readInt32BE(0)],
            ["writeUInt8_oob", () => Buffer.alloc(1).writeUInt8(1, 1)],
            ["writeUInt16LE_short", () => Buffer.alloc(1).writeUInt16LE(1, 0)],
            ["writeInt32BE_short", () => Buffer.alloc(3).writeInt32BE(-1, 0)],
            ["readUInt8_negative", () => Buffer.from([1]).readUInt8(-1)],
            ["writeUInt8_negative", () => Buffer.alloc(1).writeUInt8(1, -1)],
        ];
        return cases.map(([label, run]) => {
            try {
                return label + ":" + run();
            } catch (e) {
                return label + ":" + e.name;
            }
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "readUInt8_oob:RangeError|readUInt16LE_short:RangeError|readInt32BE_short:RangeError|writeUInt8_oob:RangeError|writeUInt16LE_short:RangeError|writeInt32BE_short:RangeError|readUInt8_negative:RangeError|writeUInt8_negative:RangeError"
    );
}

#[test]
fn global_buffer_unknown_encoding_strict_entrypoints_match_node() {
    let result = eval_global_buffer(
        r#"(() => {
        const outcomes = [];
        try { Buffer.from("abc", "bogus"); outcomes.push("from:ok"); }
        catch (e) { outcomes.push("from:" + e.message); }
        try { Buffer.from([0x61]).toString("bogus"); outcomes.push("toString:ok"); }
        catch (e) { outcomes.push("toString:" + e.message); }
        try { Buffer.alloc(3).write("abc", 0, 3, "bogus"); outcomes.push("write:ok"); }
        catch (e) { outcomes.push("write:" + e.message); }
        outcomes.push("byteLength:" + Buffer.byteLength("abc", "bogus"));
        outcomes.push("isEncoding:" + Buffer.isEncoding("bogus"));
        return outcomes.join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "from:Unknown encoding: bogus|toString:Unknown encoding: bogus|write:Unknown encoding: bogus|byteLength:3|isEncoding:false"
    );
}

#[test]
fn global_buffer_utf16le_alias_encodings_match_node_vectors() {
    let result = eval_global_buffer(
        r#"(() => {
        const aliases = ["utf16le", "utf-16le", "ucs2", "ucs-2"];
        return aliases.map((enc) => {
            const from = Buffer.from("A\u2603", enc);
            const written = Buffer.alloc(4);
            const bytesWritten = written.write("A\u2603", 0, 4, enc);
            const codes = from.toString(enc).split("").map((ch) => ch.charCodeAt(0).toString(16)).join(",");
            return [enc, from.toString("hex"), codes, Buffer.byteLength("A\u2603", enc), Buffer.isEncoding(enc), bytesWritten, written.toString("hex")].join(":");
        }).join("|");
    })()"#,
    );
    assert_eq!(
        result,
        "utf16le:41000326:41,2603:4:true:4:41000326|utf-16le:41000326:41,2603:4:true:4:41000326|ucs2:41000326:41,2603:4:true:4:41000326|ucs-2:41000326:41,2603:4:true:4:41000326"
    );
}

// ─── Edge cases ────────────────────────────────────────────────────────────

#[test]
fn empty_buffer() {
    let result = eval_buffer(
        r#"(() => {
        const buf = Buffer.alloc(0);
        return buf.length + ":" + buf.toString();
    })()"#,
    );
    assert_eq!(result, "0:");
}

#[test]
fn allocunsafe_returns_buffer() {
    let result = eval_buffer(
        r"(() => {
        const buf = Buffer.allocUnsafe(10);
        return Buffer.isBuffer(buf) && buf.length === 10;
    })()",
    );
    assert_eq!(result, "true");
}

#[test]
fn static_compare() {
    let result = eval_buffer(r#"Buffer.compare(Buffer.from("a"), Buffer.from("b"))"#);
    assert_eq!(result, "-1");
}
