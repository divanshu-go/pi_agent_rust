//! Node.js `Buffer` shim — pure-JS implementation for the QuickJS extension runtime.
//!
//! Provides a `Buffer` class that extends `Uint8Array` with Node.js-compatible
//! encoding/decoding (utf8, base64, hex, ascii, latin1), static factory methods
//! (`from`, `alloc`, `concat`, `isBuffer`, `byteLength`), and instance methods
//! (`toString`, `write`, `slice`, `copy`, `compare`, `equals`, `indexOf`,
//! `includes`, `fill`, `toJSON`).

/// The JS source for the `node:buffer` virtual module.
pub const NODE_BUFFER_JS: &str = r#"
// ─── Encoding helpers ────────────────────────────────────────────────────────

function utf8Encode(str) {
  return new TextEncoder().encode(str);
}

function utf8Decode(bytes, start, end) {
  return new TextDecoder().decode(bytes.subarray(start, end));
}

function hexEncode(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, '0');
  }
  return out;
}

function hexDecode(str) {
  const len = str.length >>> 1;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = parseInt(str.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function base64Encode(bytes) {
  let binary = '';
  let chunk = [];
  for (let i = 0; i < bytes.length; i++) {
    chunk.push(bytes[i]);
    if (chunk.length >= 4096) {
      binary += String.fromCharCode.apply(null, chunk);
      chunk.length = 0;
    }
  }
  if (chunk.length > 0) {
    binary += String.fromCharCode.apply(null, chunk);
  }
  return globalThis.btoa(binary);
}

function base64Decode(str) {
  const binary = globalThis.atob(str);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function latin1Encode(str) {
  const bytes = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) {
    bytes[i] = str.charCodeAt(i) & 0xFF;
  }
  return bytes;
}

function latin1Decode(bytes, start, end, stripHighBit) {
  let out = '';
  let chunk = [];
  const len = end - start;
  for (let i = 0; i < len; i++) {
    const byte = bytes[start + i];
    chunk.push(stripHighBit ? (byte & 0x7F) : byte);
    if (chunk.length >= 4096) {
      out += String.fromCharCode.apply(null, chunk);
      chunk.length = 0;
    }
  }
  if (chunk.length > 0) {
    out += String.fromCharCode.apply(null, chunk);
  }
  return out;
}

function normalizeEncoding(enc, allowUnknownUtf8) {
  if (!enc || enc === 'utf8' || enc === 'utf-8') return 'utf8';
  const lower = String(enc).toLowerCase();
  if (lower === 'utf8' || lower === 'utf-8') return 'utf8';
  if (lower === 'hex') return 'hex';
  if (lower === 'base64') return 'base64';
  if (lower === 'ascii') return 'ascii';
  if (lower === 'binary' || lower === 'latin1') return 'latin1';
  if (allowUnknownUtf8) return 'utf8';
  throw new TypeError(`Unknown encoding: ${enc}`);
}

function encodeString(str, encoding, allowUnknownUtf8) {
  switch (normalizeEncoding(encoding, allowUnknownUtf8)) {
    case 'hex': return hexDecode(str);
    case 'base64': return base64Decode(str);
    case 'ascii':
    case 'latin1': return latin1Encode(str);
    default: return utf8Encode(str);
  }
}

function decodeBytes(bytes, encoding, start, end) {
  start = start || 0;
  end = end != null ? end : bytes.length;
  switch (normalizeEncoding(encoding)) {
    case 'hex': return hexEncode(bytes.subarray(start, end));
    case 'base64': return base64Encode(bytes.subarray(start, end));
    case 'ascii': return latin1Decode(bytes, start, end, true);
    case 'latin1': return latin1Decode(bytes, start, end, false);
    default: return utf8Decode(bytes, start, end);
  }
}

function normalizeSearchOffset(length, byteOffset) {
  if (byteOffset == null) return 0;
  const number = Number(byteOffset);
  if (Number.isNaN(number)) return 0;
  if (number === Infinity) return length;
  if (number === -Infinity) return 0;
  const offset = Math.trunc(number);
  if (offset < 0) return Math.max(length + offset, 0);
  if (offset > length) return length;
  return offset;
}

// ─── Buffer class ────────────────────────────────────────────────────────────

class Buffer extends Uint8Array {
  // ── Static factory methods ─────────────────────────────────────────────
  static from(input, encodingOrOffset, length) {
    if (typeof input === 'string') {
      const bytes = encodeString(input, encodingOrOffset);
      const buf = new Buffer(bytes.length);
      buf.set(bytes);
      return buf;
    }
    if (input instanceof ArrayBuffer || input instanceof SharedArrayBuffer) {
      const offset = encodingOrOffset || 0;
      const len = length != null ? length : input.byteLength - offset;
      return new Buffer(input, offset, len);
    }
    if (ArrayBuffer.isView(input)) {
      const buf = new Buffer(input.length);
      buf.set(input);
      return buf;
    }
    if (Array.isArray(input)) {
      const buf = new Buffer(input.length);
      buf.set(input);
      return buf;
    }
    if (input && typeof input === 'object' && typeof input.length === 'number') {
      const buf = new Buffer(input.length);
      for (let i = 0; i < input.length; i++) buf[i] = input[i] & 0xFF;
      return buf;
    }
    throw new TypeError('The first argument must be a string, Buffer, ArrayBuffer, Array, or array-like object.');
  }

  static alloc(size, fill, encoding) {
    const buf = new Buffer(size);
    if (fill !== undefined && fill !== 0) {
      buf.fill(fill, 0, size, encoding);
    }
    return buf;
  }

  static allocUnsafe(size) {
    return new Buffer(size);
  }

  static allocUnsafeSlow(size) {
    return new Buffer(size);
  }

  static isBuffer(obj) {
    return obj instanceof Buffer;
  }

  static isEncoding(encoding) {
    return ['utf8', 'utf-8', 'hex', 'base64', 'ascii', 'binary', 'latin1']
      .includes(String(encoding || '').toLowerCase());
  }

  static byteLength(string, encoding) {
    if (typeof string !== 'string') {
      if (ArrayBuffer.isView(string)) return string.byteLength;
      if (string instanceof ArrayBuffer) return string.byteLength;
      throw new TypeError('The "string" argument must be a string, Buffer, or ArrayBuffer');
    }
    return encodeString(string, encoding, true).length;
  }

  static concat(list, totalLength) {
    if (!Array.isArray(list)) throw new TypeError('"list" argument must be an Array of Buffers');
    if (list.length === 0) return Buffer.alloc(0);
    const total = totalLength != null
      ? totalLength
      : list.reduce((acc, b) => acc + b.length, 0);
    const result = Buffer.alloc(total);
    let offset = 0;
    for (const buf of list) {
      const src = buf instanceof Uint8Array ? buf : Buffer.from(buf);
      const copyLen = Math.min(src.length, total - offset);
      result.set(src.subarray(0, copyLen), offset);
      offset += copyLen;
      if (offset >= total) break;
    }
    return result;
  }

  static compare(a, b) {
    if (!(a instanceof Uint8Array) || !(b instanceof Uint8Array)) {
      throw new TypeError('Arguments must be Buffers');
    }
    const len = Math.min(a.length, b.length);
    for (let i = 0; i < len; i++) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    if (a.length < b.length) return -1;
    if (a.length > b.length) return 1;
    return 0;
  }

  // ── Instance methods ───────────────────────────────────────────────────
  toString(encoding, start, end) {
    return decodeBytes(this, encoding, start, end);
  }

  write(string, offset, length, encoding) {
    if (typeof offset === 'string') { encoding = offset; offset = 0; length = this.length; }
    else if (typeof length === 'string') { encoding = length; length = this.length - (offset || 0); }
    offset = offset || 0;
    length = length != null ? length : this.length - offset;
    const bytes = encodeString(string, encoding);
    const writeLen = Math.min(bytes.length, length, this.length - offset);
    this.set(bytes.subarray(0, writeLen), offset);
    return writeLen;
  }

  toJSON() {
    return { type: 'Buffer', data: Array.from(this) };
  }

  equals(other) {
    if (!(other instanceof Uint8Array)) throw new TypeError('Argument must be a Buffer');
    return Buffer.compare(this, other) === 0;
  }

  compare(other, targetStart, targetEnd, sourceStart, sourceEnd) {
    if (!(other instanceof Uint8Array)) throw new TypeError('Argument must be a Buffer');
    targetStart = targetStart || 0;
    targetEnd = targetEnd != null ? targetEnd : other.length;
    sourceStart = sourceStart || 0;
    sourceEnd = sourceEnd != null ? sourceEnd : this.length;
    return Buffer.compare(
      this.subarray(sourceStart, sourceEnd),
      other.subarray(targetStart, targetEnd)
    );
  }

  copy(target, targetStart, sourceStart, sourceEnd) {
    targetStart = targetStart || 0;
    sourceStart = sourceStart || 0;
    sourceEnd = sourceEnd != null ? sourceEnd : this.length;
    const len = Math.min(sourceEnd - sourceStart, target.length - targetStart);
    target.set(this.subarray(sourceStart, sourceStart + len), targetStart);
    return len;
  }

  indexOf(value, byteOffset, encoding) {
    let offset = normalizeSearchOffset(this.length, byteOffset);
    let searchEncoding = encoding;
    if (typeof byteOffset === 'string') {
      offset = 0;
      searchEncoding = byteOffset;
    }
    if (typeof value === 'number') {
      for (let i = offset; i < this.length; i++) {
        if (this[i] === (value & 0xFF)) return i;
      }
      return -1;
    }
    if (typeof value === 'string') {
      value = Buffer.from(value, searchEncoding);
    }
    if (value instanceof Uint8Array) {
      if (value.length === 0) return offset;
      outer: for (let i = offset; i <= this.length - value.length; i++) {
        for (let j = 0; j < value.length; j++) {
          if (this[i + j] !== value[j]) continue outer;
        }
        return i;
      }
    }
    return -1;
  }

  includes(value, byteOffset, encoding) {
    return this.indexOf(value, byteOffset, encoding) !== -1;
  }

  fill(value, offset, end, encoding) {
    offset = offset || 0;
    end = end != null ? end : this.length;
    if (typeof value === 'number') {
      for (let i = offset; i < end; i++) this[i] = value & 0xFF;
    } else if (typeof value === 'string') {
      const bytes = encodeString(value, encoding);
      if (bytes.length === 0) return this;
      for (let i = offset; i < end; i++) {
        this[i] = bytes[(i - offset) % bytes.length];
      }
    } else if (value instanceof Uint8Array) {
      if (value.length === 0) return this;
      for (let i = offset; i < end; i++) {
        this[i] = value[(i - offset) % value.length];
      }
    }
    return this;
  }

  slice(start, end) {
    return this.subarray(start, end);
  }

  subarray(start, end) {
    // Override to return a Buffer, not a plain Uint8Array
    const sub = super.subarray(start, end);
    Object.setPrototypeOf(sub, Buffer.prototype);
    return sub;
  }

  swap16() {
    if (this.length % 2 !== 0) throw new RangeError('Buffer size must be a multiple of 16-bits');
    for (let i = 0; i < this.length; i += 2) {
      const t = this[i]; this[i] = this[i + 1]; this[i + 1] = t;
    }
    return this;
  }

  swap32() {
    if (this.length % 4 !== 0) throw new RangeError('Buffer size must be a multiple of 32-bits');
    for (let i = 0; i < this.length; i += 4) {
      const t0 = this[i], t1 = this[i + 1];
      this[i] = this[i + 3]; this[i + 1] = this[i + 2];
      this[i + 2] = t1; this[i + 3] = t0;
    }
    return this;
  }

  // Read/write integers (LE/BE) — commonly used by extensions
  readUInt8(offset) {
    offset = offset >>> 0;
    if (offset >= this.length) throw new RangeError('Index out of range');
    return this[offset];
  }

  readUInt16LE(offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    return this[offset] | (this[offset + 1] << 8);
  }

  readUInt16BE(offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    return (this[offset] << 8) | this[offset + 1];
  }

  readUInt32LE(offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    return (this[offset] | (this[offset+1] << 8) | (this[offset+2] << 16)) + (this[offset+3] * 0x1000000);
  }

  readUInt32BE(offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    return (this[offset] * 0x1000000) + ((this[offset+1] << 16) | (this[offset+2] << 8) | this[offset+3]);
  }

  readInt8(offset) {
    offset = offset >>> 0;
    if (offset >= this.length) throw new RangeError('Index out of range');
    const v = this[offset];
    return v > 127 ? v - 256 : v;
  }

  readInt16LE(offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    const v = this[offset] | (this[offset + 1] << 8);
    return v > 32767 ? v - 65536 : v;
  }

  readInt16BE(offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    const v = (this[offset] << 8) | this[offset + 1];
    return v > 32767 ? v - 65536 : v;
  }

  readInt32LE(offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    return this[offset] | (this[offset+1] << 8) | (this[offset+2] << 16) | (this[offset+3] << 24);
  }

  readInt32BE(offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    return (this[offset] << 24) | (this[offset+1] << 16) | (this[offset+2] << 8) | this[offset+3];
  }

  writeUInt8(value, offset) {
    offset = offset >>> 0;
    if (offset >= this.length) throw new RangeError('Index out of range');
    this[offset] = value & 0xFF;
    return offset + 1;
  }

  writeInt8(value, offset) {
    return this.writeUInt8(value, offset);
  }

  writeUInt16LE(value, offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    this[offset] = value & 0xFF;
    this[offset + 1] = (value >>> 8) & 0xFF;
    return offset + 2;
  }

  writeInt16LE(value, offset) {
    return this.writeUInt16LE(value, offset);
  }

  writeUInt16BE(value, offset) {
    offset = offset >>> 0;
    if (offset + 2 > this.length) throw new RangeError('Index out of range');
    this[offset] = (value >>> 8) & 0xFF;
    this[offset + 1] = value & 0xFF;
    return offset + 2;
  }

  writeInt16BE(value, offset) {
    return this.writeUInt16BE(value, offset);
  }

  writeUInt32LE(value, offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    this[offset] = value & 0xFF;
    this[offset+1] = (value >>> 8) & 0xFF;
    this[offset+2] = (value >>> 16) & 0xFF;
    this[offset+3] = (value >>> 24) & 0xFF;
    return offset + 4;
  }

  writeInt32LE(value, offset) {
    return this.writeUInt32LE(value, offset);
  }

  writeUInt32BE(value, offset) {
    offset = offset >>> 0;
    if (offset + 4 > this.length) throw new RangeError('Index out of range');
    this[offset] = (value >>> 24) & 0xFF;
    this[offset+1] = (value >>> 16) & 0xFF;
    this[offset+2] = (value >>> 8) & 0xFF;
    this[offset+3] = value & 0xFF;
    return offset + 4;
  }

  writeInt32BE(value, offset) {
    return this.writeUInt32BE(value, offset);
  }
}

// Make Buffer available globally (Node.js compatibility)
globalThis.Buffer = Buffer;

export { Buffer };
export default { Buffer };
"#;
