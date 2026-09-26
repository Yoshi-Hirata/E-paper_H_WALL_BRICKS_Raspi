/*
 * conductor/web/sim/model.js
 *
 * Classic script (no ES modules - file:// blocks module CORS). A JS port,
 * as they are on main commit cd22872 (2026-09-24), of:
 *   conductor/look.py       LookMap.parse/Design.parse/check/default_shift/
 *                           PALETTE/dip_sheet/unit_board_ids (single-map
 *                           form only - the sim never shares a bus between
 *                           items, since it never assigns units)
 *   conductor/sequence.py   SEQUENCES/LABELS/ranks/span_s
 *   conductor/timeline.py   clean/resolve/apply_transitions/times/ends/
 *                           effective_refresh/validate/min_interval/
 *                           parse_clock/format_clock
 * plus SIM.fmt (Python's float formatting, exact on the double's own
 * binary value) and SIM.mmss (the designer-facing mm.ss clock grammar,
 * plan section 3.1).
 *
 * Every operator-visible string and ordering rule from those modules is
 * reproduced exactly, including punctuation, plurals and Python's own
 * dict/repr rendering inside a couple of error messages. Nothing in this
 * file may call Math.round, Number.prototype.toFixed, toPrecision,
 * String.prototype.localeCompare or Intl - see SIM.fmt below for why
 * (tests/test_sim_goldens.py greps for these).
 *
 * Owned by Coder P (conductor/web/sim/model.js, state.js, selftest.js,
 * goldens.js). Q (render/flicker/looks/transport/designer-app) and R
 * (server.py/index.html) read this file; they do not edit it.
 *
 * Known, deliberately-unfixed parity gaps (adversarial review round 2 -
 * F11, flagged unreachable in practice - noted here rather than risked as
 * an edit, per the review's own "or, better, leave it and note it"):
 *   - validate()'s item lookup uses a plain {} keyed by item name. An item
 *     literally named "constructor" (or another Object.prototype member)
 *     would collide with the prototype chain and could misbehave instead
 *     of the "no such item" a real lookup miss gives everywhere else. Real
 *     item names come from CSV filenames (a garment or bag's model code),
 *     so this needs a deliberately hostile filename to reach at all.
 *   - JS has no distinct bool type Python's json/repr formatting would
 *     treat differently from 0/1 the way Python's `isinstance(x, bool)`
 *     can - a project file's JSON encodes true/false as JSON booleans
 *     either way, so this only matters for a value that arrived as a raw
 *     0/1 through a path Python would have rejected as "not a bool" first.
 */
(function () {
  "use strict";

  // ============================================================
  // SIM.fmt - Python's float formatting, exact on the double's own
  // binary value (round-half-to-even), never Math.round/toFixed/
  // toPrecision.
  //
  // Python's round()/format(":.Nf") do not round the *decimal literal*
  // you typed - they round the *exact binary64 value* the literal
  // became, to the nearest representable decimal at N digits, ties to
  // even. 2.25 is exact in binary (9/4), so round(2.25, 1) is "2.2" in
  // Python (2.2's last digit is even) but "2.3" from a naive
  // Math.round-based toFixed. The one_board_map fixture's sweep term is
  // exactly 2.25 s - this is the case that catches a naive JS port.
  //
  // Method: read the double's IEEE-754 bits to get its EXACT value as
  // num/10**k (num a BigInt, k>=0 - no float arithmetic past this
  // point), then round `num` with plain BigInt division and a tie-to-
  // even check on the remainder. This is the same "exact value, then
  // correctly-rounded decimal" two-step CPython's own float-to-string
  // (David Gay's dtoa, mode 3) uses for both round() and "%.Nf".
  // ============================================================

  const VIEW64 = new DataView(new ArrayBuffer(8));

  function bitsOf(nonNegativeX) {
    VIEW64.setFloat64(0, nonNegativeX, false);
    return VIEW64.getBigUint64(0, false);
  }

  // |x| (x finite) -> {num, k}: |x| === num / 10n**k exactly, num a
  // non-negative BigInt, k a non-negative int.
  function exactDecimal(absX) {
    if (absX === 0) return { num: 0n, k: 0 };
    const bits = bitsOf(absX);
    const rawExp = Number((bits >> 52n) & 0x7ffn);
    const mantissa = bits & 0xfffffffffffffn;
    let sig, e;
    if (rawExp === 0) {                       // subnormal
      sig = mantissa;
      e = -1074;
    } else {
      sig = mantissa | 0x10000000000000n;     // the implicit leading 1
      e = rawExp - 1075;
    }
    if (e >= 0) return { num: sig << BigInt(e), k: 0 };
    const k = -e;
    return { num: sig * (5n ** BigInt(k)), k };
  }

  function decompose(x) {
    const sign = x < 0 || Object.is(x, -0);
    const { num, k } = exactDecimal(Math.abs(x));
    return { sign, num, k };
  }

  // Round the exact non-negative decimal num/10**k to n fractional
  // digits, ties to even -> a BigInt q with value q/10**n.
  function roundFractional(num, k, n) {
    if (k <= n) return num * (10n ** BigInt(n - k));
    const diff = BigInt(k - n);
    const divisor = 10n ** diff;
    const q = num / divisor;
    const r = num % divisor;
    const half = divisor / 2n;
    if (r > half || (r === half && (q & 1n) === 1n)) return q + 1n;
    return q;
  }

  function qToFixedString(q, n) {
    let s = q.toString();
    if (n === 0) return s;
    while (s.length <= n) s = "0" + s;
    const cut = s.length - n;
    return s.slice(0, cut) + "." + s.slice(cut);
  }

  function fixed(x, n) {
    if (!Number.isFinite(x)) return Number.isNaN(x) ? "nan" : (x > 0 ? "inf" : "-inf");
    const { sign, num, k } = decompose(x);
    const q = roundFractional(num, k, n);
    return (sign ? "-" : "") + qToFixedString(q, n);
  }

  function round(x, n) {
    if (!Number.isFinite(x)) return x;
    const { sign, num, k } = decompose(x);
    const q = roundFractional(num, k, n);
    const value = Number(qToFixedString(q, n));
    return sign && value !== 0 ? -value : (sign ? -0 : value);
  }

  function roundInt(x) {
    if (!Number.isFinite(x)) return x;
    const { sign, num, k } = decompose(x);
    const q = roundFractional(num, k, 0);
    const value = Number(q.toString());
    return sign && value !== 0 ? -value : value;
  }

  // Python's "%g" (used once, in timeline.validate's own-refresh
  // message): `precision` significant digits (default 6), trailing
  // zeros stripped, scientific notation outside [1e-4, 1e{precision}).
  function sigRound(x, precision) {
    if (x === 0) return { digits: "0", exp: 0 };
    const { num, k } = exactDecimal(Math.abs(x));
    const numStr = num.toString();
    const ld = numStr.length;
    let exp = ld - 1 - k;
    let digits;
    if (ld <= precision) {
      digits = numStr + "0".repeat(precision - ld);
    } else {
      const dropped = BigInt(ld - precision);
      const divisor = 10n ** dropped;
      const q = num / divisor;
      const r = num % divisor;
      const half = divisor / 2n;
      let rounded = (r > half || (r === half && (q & 1n) === 1n)) ? q + 1n : q;
      let roundedStr = rounded.toString();
      if (roundedStr.length > precision) {      // carried over: 999 -> 1000
        roundedStr = roundedStr.slice(0, precision);
        exp += 1;
      }
      digits = roundedStr;
    }
    return { digits, exp };
  }

  function g(x, precision) {
    if (precision === undefined) precision = 6;
    if (!Number.isFinite(x)) return Number.isNaN(x) ? "nan" : (x > 0 ? "inf" : "-inf");
    const sign = x < 0 || Object.is(x, -0);
    if (x === 0) return sign ? "-0" : "0";
    const { digits, exp } = sigRound(Math.abs(x), precision);
    let trimmed = digits.replace(/0+$/, "");
    if (trimmed === "") trimmed = "0";
    let body;
    if (exp < -4 || exp >= precision) {
      const mantissa = trimmed.length > 1 ? trimmed[0] + "." + trimmed.slice(1) : trimmed;
      const esign = exp < 0 ? "-" : "+";
      const eabs = Math.abs(exp);
      body = `${mantissa}e${esign}${eabs < 10 ? "0" + eabs : String(eabs)}`;
    } else if (exp >= 0) {
      body = trimmed.length > exp + 1
        ? trimmed.slice(0, exp + 1) + "." + trimmed.slice(exp + 1)
        : trimmed + "0".repeat(exp + 1 - trimmed.length);
    } else {
      body = "0." + "0".repeat(-exp - 1) + trimmed;
    }
    return (sign ? "-" : "") + body;
  }

  // Value-based canonicalisation for digesting: a number that IS a
  // whole number prints as an int, any other number as fixed(x,3) -
  // "2" not "2.000", but "2.5" -> "2.500". Objects: keys sorted by
  // code point (never localeCompare), no spaces. Strings: JSON-style
  // escaping, non-ASCII as \uXXXX (matches Python's json.dumps(...,
  // ensure_ascii=True), which is exactly what canonical() imitates -
  // this is only ever compared byte-for-byte against the same function
  // written in Python (tools/make_goldens.py), never parsed back.
  function canonicalString(s) {
    let out = '"';
    for (let i = 0; i < s.length; i++) {
      const code = s.charCodeAt(i);
      const ch = s[i];
      if (ch === "\\") out += "\\\\";
      else if (ch === '"') out += '\\"';
      else if (code === 0x08) out += "\\b";
      else if (code === 0x0c) out += "\\f";
      else if (code === 0x0a) out += "\\n";
      else if (code === 0x0d) out += "\\r";
      else if (code === 0x09) out += "\\t";
      else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
      else out += ch;
    }
    return out + '"';
  }

  function canonical(v) {
    if (v === null || v === undefined) return "null";
    if (v === true) return "true";
    if (v === false) return "false";
    if (typeof v === "number") {
      // Matches tools/make_goldens.py's canonical() exactly: a whole
      // number under 1e15 prints as an int, anything else as
      // fixed(x,3) - and, like Python's int(v) on inf/nan, a
      // non-finite number is refused rather than silently printed.
      if (!Number.isFinite(v)) throw new Error(`canonical: not a finite number: ${v}`);
      return (Number.isInteger(v) && Math.abs(v) < 1e15) ? String(v === 0 ? 0 : v) : fixed(v, 3);
    }
    if (typeof v === "string") return canonicalString(v);
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (typeof v === "object") {
      // Object.keys().sort() compares by UTF-16 code unit, which is
      // code-point order only within the BMP - every key this model
      // ever sorts (item/design names, "side|row|col" position keys)
      // is ASCII, so this never matters in practice; never
      // localeCompare regardless.
      const keys = Object.keys(v).sort();
      return "{" + keys.map(k => canonicalString(k) + ":" + canonical(v[k])).join(",") + "}";
    }
    return canonicalString(String(v));
  }

  // FNV-1a, 64-bit, over the UTF-8 bytes of `text` - returned as 16
  // lower-case hex characters (a plain BigInt is not JSON-safe, and a
  // Number would lose precision past 2**53).
  const FNV_OFFSET = 0xcbf29ce484222325n;
  const FNV_PRIME = 0x100000001b3n;
  const MASK64 = (1n << 64n) - 1n;
  const UTF8 = typeof TextEncoder !== "undefined" ? new TextEncoder() : null;

  function utf8Bytes(text) {
    if (UTF8) return UTF8.encode(text);
    // A minimal fallback for a host with no TextEncoder (none expected -
    // every target browser has one - kept only so a stray environment
    // fails softly rather than throwing ReferenceError).
    const bytes = [];
    for (let i = 0; i < text.length; i++) {
      let code = text.codePointAt(i);
      if (code > 0xffff) i++;
      if (code < 0x80) bytes.push(code);
      else if (code < 0x800) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
      else if (code < 0x10000) bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
      else bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
    return Uint8Array.from(bytes);
  }

  function digest64(text) {
    let hash = FNV_OFFSET;
    const bytes = utf8Bytes(text);
    for (let i = 0; i < bytes.length; i++) {
      hash ^= BigInt(bytes[i]);
      hash = (hash * FNV_PRIME) & MASK64;
    }
    return hash.toString(16).padStart(16, "0");
  }

  const fmt = { round, roundInt, fixed, g, canonical, digest64 };

  // ============================================================
  // SIM.mmss - the designer-facing mm.ss clock grammar (plan 3.1).
  // ============================================================

  const MMSS_RE = /^\s*(\d{1,3})(?:[.:](\d{1,2}))?\s*$/;

  function mmssParse(text) {
    const m = MMSS_RE.exec(String(text));
    if (!m) return null;
    const minutes = parseInt(m[1], 10);
    let seconds = 0;
    if (m[2] !== undefined) {
      seconds = parseInt(m[2], 10);
      if (seconds >= 60) return null;
    }
    return minutes * 60 + seconds;
  }

  function splitMinSec(sec) {
    const total = fmt.roundInt(sec);
    const m = Math.trunc(total / 60);
    const s = total - m * 60;
    return { m, s };
  }

  function mmssFormat(sec) {
    const { m, s } = splitMinSec(sec);
    return `${m}.${s < 10 ? "0" + s : s}`;
  }

  function mmssHuman(sec) {
    const { m, s } = splitMinSec(sec);
    return `${m} min ${s < 10 ? "0" + s : s} s`;
  }

  const mmss = { parse: mmssParse, format: mmssFormat, human: mmssHuman };

  // ============================================================
  // Shared helpers: Python-ish repr (for the handful of error messages
  // that print one) and position ordering ((side,row,col) by code point
  // then numeric - never localeCompare).
  // ============================================================

  function pyStrRepr(s) {
    const hasSingle = s.indexOf("'") !== -1;
    const hasDouble = s.indexOf('"') !== -1;
    const quote = hasSingle && !hasDouble ? '"' : "'";
    let out = quote;
    for (const ch of s) {
      if (ch === "\\") out += "\\\\";
      else if (ch === quote) out += "\\" + quote;
      else if (ch === "\n") out += "\\n";
      else if (ch === "\r") out += "\\r";
      else if (ch === "\t") out += "\\t";
      else {
        const code = ch.codePointAt(0);
        out += (code < 0x20 || code === 0x7f) ? "\\x" + code.toString(16).padStart(2, "0") : ch;
      }
    }
    return out + quote;
  }

  // Python's str(v), for a raw JSON value that might be null/bool -
  // str(None) is "None", not JS's String(null) === "null".
  function pyStr(v) {
    if (v === null || v === undefined) return "None";
    if (v === true) return "True";
    if (v === false) return "False";
    return String(v);
  }

  function pyRepr(v) {
    if (v === null || v === undefined) return "None";
    if (v === true) return "True";
    if (v === false) return "False";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") return pyStrRepr(v);
    if (Array.isArray(v)) return "[" + v.map(pyRepr).join(", ") + "]";
    if (typeof v === "object") {
      const parts = Object.keys(v).map(k => `${pyStrRepr(k)}: ${pyRepr(v[k])}`);
      return "{" + parts.join(", ") + "}";
    }
    return String(v);
  }

  function posKeyOf(pos) { return `${pos.side}|${pos.row}|${pos.col}`; }
  function rowKeyOf(side, row) { return `${side}|${row}`; }
  function parsePosKey(key) {
    const parts = key.split("|");
    return { side: parts[0], row: Number(parts[1]), col: Number(parts[2]) };
  }
  function posText(pos) { return `${pos.side} row ${pos.row} col ${pos.col}`; }

  function comparePosition(a, b) {
    if (a.side < b.side) return -1;
    if (a.side > b.side) return 1;
    if (a.row !== b.row) return a.row - b.row;
    return a.col - b.col;
  }

  function sortedPositionKeys(keys) {
    return Array.from(keys).map(parsePosKey).sort(comparePosition);
  }

  function pad2(n) { return String(n).padStart(2, "0"); }
  function pad3(n) { return String(n).padStart(3, "0"); }

  // Minimal RFC4180 CSV reader (Python's csv module default "excel"
  // dialect): comma-separated, doubled quotes escape a literal quote,
  // an embedded newline inside a quoted field is part of the field.
  // Good enough for the small, hand-authored map/grid CSVs this reads.
  function parseCsvRows(text) {
    if (text.length && text.charCodeAt(0) === 0xfeff) text = text.slice(1);   // utf-8-sig
    const rows = [];
    let row = [];
    let field = "";
    let inQuotes = false;
    let i = 0;
    const n = text.length;
    while (i < n) {
      const c = text[i];
      if (inQuotes) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
          inQuotes = false; i += 1; continue;
        }
        field += c; i += 1; continue;
      }
      if (c === '"') { inQuotes = true; i += 1; continue; }
      if (c === ",") { row.push(field); field = ""; i += 1; continue; }
      if (c === "\r") { i += 1; continue; }
      if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; i += 1; continue; }
      field += c; i += 1;
    }
    row.push(field);
    if (row.length > 1 || row[0] !== "") rows.push(row);
    return rows;
  }

  function pyIntStrict(s) {
    if (typeof s !== "string") return null;
    const t = s.trim();
    if (!/^[+-]?\d+$/.test(t)) return null;
    return parseInt(t, 10);
  }

  function pyFloatStrict(s) {
    if (typeof s !== "string") return null;
    const t = s.trim();
    if (t === "") return null;
    if (/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(t)) return Number(t);
    if (/^[+-]?(inf|infinity)$/i.test(t)) return t[0] === "-" ? -Infinity : Infinity;
    if (/^[+-]?nan$/i.test(t)) return NaN;
    return null;
  }

  // Python float(value) - accepts a real number as-is, a numeric string,
  // rejects anything else (None, {}, [], a non-numeric string) as null.
  function toNumber(v) {
    if (typeof v === "number") return v;
    if (typeof v === "boolean") return v ? 1 : 0;
    if (typeof v === "string") return pyFloatStrict(v);
    return null;
  }

  // Python's two-argument max(a,b)/min(a,b) are `b if b>a else a` / `b if
  // b<a else a` - order-dependent and NOT the same as Math.max/Math.min
  // when NaN is involved (Math.max/min(NaN, x) is always NaN, regardless
  // of argument order; Python's is not). Used at every boundary clamp
  // that touches a value which could be junk (an unparsed "at"/span/
  // refresh from hostile JSON).
  function pyMax2(a, b) { return b > a ? b : a; }
  function pyMin2(a, b) { return b < a ? b : a; }

  // ============================================================
  // SIM.look - port of conductor/look.py.
  //
  // map    = {name, item, warnings:[...] (mutated by check()),
  //           shifts:{"side|row":0|0.5}, scales:[{side,row,col,board_no,
  //           socket,label,position}], sides:[...], boardNos:[...],
  //           byPosition:{"side|row|col": scale}}
  // design = {name, item, pattern, label, colors:{"side|row|col":code},
  //           shifts:{"side|row":0|0.5}, undecided:["side|row|col", ...]}
  // ============================================================

  const ARRAY_LEN = 64;
  const NO_REFRESH = 0xff;
  const MAX_BOARDS = 60;
  const COLOR_COUNT = 16;

  // production site's colour chart 260921 - see conductor/look.py for
  // the full provenance note; codes/names are the FW_260917 chart.
  const PALETTE = [
    ["White", [137, 173, 195]], ["Yellow", [180, 174, 64]],
    ["Blue", [0, 92, 182]], ["Red", [114, 71, 59]],
    ["Black", [26, 55, 87]], ["Green", [67, 131, 114]],
    ["Turquoise", [118, 148, 76]], ["Almond", [119, 122, 101]],
    ["Light Pink", [112, 112, 112]], ["Sky Blue", [36, 115, 179]],
    ["Orange", [129, 82, 66]], ["Yellow Green", [134, 174, 89]],
    ["Olive Gray", [60, 131, 116]], ["Brown", [126, 85, 63]],
    ["Dark Brown", [108, 99, 75]], ["Smoke Blue", [56, 119, 147]],
  ];

  const _MAP_COLUMNS = ["side", "row", "col", "board_no", "socket"];
  const _SHIFT_COLUMN = "shift";
  const _EMPTY_CELLS = ["", "0"];
  const _UNDECIDED = "-";
  const _IS_GRID = /_color_.+grid/i;
  const _IS_MAP = /_map$/i;
  const _MAP_NAME = /^(.+?)_map/i;
  const _GRID_NAME = /^(.+?)_color_(.+?)(?:_grid(?![A-Za-z0-9]).*)?$/i;
  // [0-9], never \d: Python's \d takes a FULL-WIDTH digit and JS's does
  // not, so "pattern１" was P01 on one side and the literal "pattern１" on
  // the other (conductor/look.py's _PATTERN_NO has the same note).
  const _PATTERN_NO = /^pattern\s*0*([0-9]+)$/i;
  // The production site's own "HW 用 CSV" name for the same grid:
  // <item>_<配色案名>_HW.csv (conductor/look.py's _HW_NAME). Case-
  // SENSITIVE, so "my_notes_hw.csv" is not design "notes" of "my".
  const _HW_NAME = /^(.+?)_(.+)_HW$/;
  const _HW_SUFFIX = "_HW";
  const _HW_RESERVED_DESIGNS = ["map"];
  // conductor/look.py's shared file-name rule - see its own comment for
  // why this has to be the same on both sides, character for character.
  const _IDEOGRAPHIC_SPACE = "　";
  const _NAME_SEPARATORS = "/\\／＼";
  const _NAME_RESERVED = ":*?\"<>|";
  // eslint-disable-next-line no-control-regex
  // U+2028/U+2029 with the ASCII line breaks: they are LINE TERMINATORS to
  // a JS regex, so "." matches them in Python and not here, and _HW_NAME
  // would read one name two ways (conductor/look.py's _NAME_CONTROL).
  const _NAME_CONTROL = /[\x00-\x1f\x7f-\x9f\u2028\u2029]/;
  // Trimmed explicitly, and only the ordinary space - never String.trim():
  // trim() and Python's str.strip() do not agree on the edges (strip() eats
  // U+0085 and U+001C-U+001F, trim() eats U+FEFF), so each side used to
  // accept a name the other refused. Every other whitespace character is a
  // control character and nameProblem() refuses it before trimming.
  // conductor/look.py's _NAME_TRIM.
  const _NAME_TRIM = " ";
  // geometry_problem()'s thresholds, verbatim from conductor/look.py.
  const _GEOM_MIN_SHORT_ROWS = 2;
  const _GEOM_SHORT_TENTHS = 1;

  function defaultShift(row) { return row % 2 !== 0 ? 0.5 : 0; }

  function stemOf(filename) {
    const base = String(filename).replace(/^.*[\\/]/, "");
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.slice(0, dot) : base;
  }

  // conductor/look.py's normalize_name(): NFC, U+3000 as an ordinary
  // space, no leading or trailing whitespace. Never NFKC - the 配線ナビ
  // writes 配色案名 with full-width characters and those ARE the name.
  function trimName(text) {
    let from = 0, to = text.length;
    while (from < to && _NAME_TRIM.indexOf(text.charAt(from)) !== -1) from += 1;
    while (to > from && _NAME_TRIM.indexOf(text.charAt(to - 1)) !== -1) to -= 1;
    return text.slice(from, to);
  }

  function nfcOf(name) {
    const text = String(name);
    try { return text.normalize("NFC"); } catch (e) { return text; }
  }

  function normalizeName(name) {
    return trimName(nfcOf(name).split(_IDEOGRAPHIC_SPACE).join(" "));
  }

  // conductor/look.py's name_problem(), same checks in the same order so
  // both sides refuse the same names and say the same thing about them.
  // The control-character check runs BEFORE any trimming, or a control
  // character at either end would be trimmed away by one side and refused
  // by the other.
  function nameProblem(name) {
    const raw = nfcOf(name);
    if (_NAME_CONTROL.test(raw)) return "a file name cannot contain a line break or a control character";
    const text = trimName(raw.split(_IDEOGRAPHIC_SPACE).join(" "));
    if (!text) return "a file name cannot be empty";
    for (const char of text) {
      if (_NAME_SEPARATORS.indexOf(char) !== -1) {
        return `a file name cannot contain "${char}" (a path separator)`;
      }
    }
    for (const char of text) {
      if (_NAME_RESERVED.indexOf(char) !== -1) {
        return `a file name cannot contain "${char}" (Windows keeps it)`;
      }
    }
    if (text.startsWith(".") || text.endsWith(".")) {
      return "a file name cannot start or end with a dot";
    }
    return null;
  }

  // conductor/look.py's _hw_body(): the <item>_<配色案名> of an _HW stem,
  // or null. "<item>_map_HW" is a muddle, so it is neither file.
  function hwBody(stem) {
    if (!_HW_NAME.test(stem)) return null;
    const body = stem.slice(0, stem.length - _HW_SUFFIX.length);
    const last = body.slice(body.lastIndexOf("_") + 1);
    return _HW_RESERVED_DESIGNS.indexOf(last.toLowerCase()) === -1 ? body : null;
  }

  function kind(filename) {
    // nameProblem() on the name as GIVEN (see conductor/look.py's kind()).
    if (nameProblem(filename)) return null;
    const name = normalizeName(filename);
    if (!/\.csv$/i.test(name)) return null;
    const stem = stemOf(name);
    if (_IS_GRID.test(stem)) return "grid";
    if (_IS_MAP.test(stem)) return "map";
    if (hwBody(stem) !== null) return "grid";
    return null;
  }

  // <item>_<配色案名>_HW -> [item, 配色案名], conductor/look.py's
  // _split_hw(): the longest garment the caller already knows about wins
  // (the design name may hold underscores of its own), and with no such
  // list the item is whatever precedes the FIRST underscore.
  function splitHw(stem, items) {
    const body = stem.slice(0, stem.length - _HW_SUFFIX.length);
    const known = (items || []).filter(Boolean).slice()
      .sort((a, b) => b.length - a.length);
    for (const k of known) {
      if (body.toLowerCase().startsWith(k.toLowerCase() + "_")) {
        return [body.slice(0, k.length), body.slice(k.length + 1)];
      }
    }
    const cut = body.indexOf("_");
    return cut === -1 ? [body, ""] : [body.slice(0, cut), body.slice(cut + 1)];
  }

  function mapItem(filename) {
    const m = _MAP_NAME.exec(stemOf(normalizeName(filename)));
    return m ? m[1] : null;
  }

  function nameParts(filename, items) {
    const stem = stemOf(normalizeName(filename));
    // A name nameProblem() refuses has no item and no design - see
    // conductor/look.py's name_parts() for why reading one anyway made
    // the two sides disagree.
    if (nameProblem(filename)) return [null, null, stem];
    const m = _GRID_NAME.exec(stem);
    let item, name;
    if (m) {
      item = m[1]; name = m[2];
    } else if (hwBody(stem) !== null) {
      const hw = splitHw(stem, items);
      item = hw[0]; name = hw[1];
    } else {
      return [null, null, stem];
    }
    const num = _PATTERN_NO.exec(name);
    if (num) {
      const n = parseInt(num[1], 10);
      return [item, n, "P" + pad2(n)];
    }
    return [item, null, name];
  }

  function deriveMapFields(base) {
    const boardSet = new Set(base.scales.map(s => s.board_no));
    const boardNos = Array.from(boardSet).sort((a, b) => a - b);
    const sides = [];
    for (const s of base.scales) if (sides.indexOf(s.side) === -1) sides.push(s.side);
    const byPosition = {};
    for (const s of base.scales) byPosition[s.key] = s;
    return Object.assign({}, base, { boardNos, sides, byPosition });
  }

  function parseMap(text, opts) {
    opts = opts || {};
    const name = opts.name || "map";
    const item = opts.item !== undefined ? opts.item : null;
    const rows = parseCsvRows(text);
    const headerRow = rows.length ? rows[0] : [];
    const header = headerRow.map(h => (h || "").trim());
    const seenNames = {};
    header.forEach(h => { if (h) seenNames[h] = (seenNames[h] || 0) + 1; });
    const doubled = Object.keys(seenNames).filter(h => seenNames[h] > 1).sort();
    if (doubled.length) {
      return { ok: false, problems: [`${name}: column(s) ${doubled.join(", ")} appear more than once`] };
    }
    const missing = _MAP_COLUMNS.filter(c => header.indexOf(c) === -1);
    if (missing.length) {
      return { ok: false, problems: [`${name}: missing column(s) ${missing.join(", ")} - expected ${_MAP_COLUMNS.join(", ")},label`] };
    }
    const hasShift = header.indexOf(_SHIFT_COLUMN) !== -1;
    const problems = [];
    const warnings = [];
    const scales = [];
    const shifts = {};
    const seenPos = {};
    const seenSocket = {};
    const allRows = new Map();       // "side|row" -> {side,row}

    for (let r = 1; r < rows.length; r++) {
      const cells = rows[r];
      const lineNo = r + 1;
      const row = {};
      header.forEach((h, i) => { row[h] = ((cells[i] !== undefined ? cells[i] : "") || "").trim(); });
      if (Object.keys(row).every(k => row[k] === "")) continue;
      const where = `${name}:${lineNo}`;
      const side = (row.side || "").toLowerCase();
      const rowNum = pyIntStrict(row.row);
      const col = pyIntStrict(row.col);
      const boardNo = pyIntStrict(row.board_no);
      const socket = pyIntStrict(row.socket);
      if (rowNum === null || col === null || boardNo === null || socket === null) {
        problems.push(`${where}: row/col/board_no/socket must be whole numbers: ${pyRepr(row)}`);
        continue;
      }
      const label = row.label || "";
      if (!side) problems.push(`${where}: side is empty`);
      if (!(socket >= 1 && socket <= 60)) problems.push(`${where}: socket ${socket} is not 1-60`);
      if (boardNo < 1) problems.push(`${where}: board_no ${boardNo} must be 1 or more`);
      const position = { side, row: rowNum, col };
      const posKey = posKeyOf(position);
      if (posKey in seenPos) {
        problems.push(`${where}: ${posText(position)} already holds a scale (line ${seenPos[posKey]})`);
      } else {
        seenPos[posKey] = lineNo;
      }
      const socketKey = `${boardNo}|${socket}`;
      if (socketKey in seenSocket) {
        problems.push(`${where}: board ${boardNo} socket ${socket} is already used (line ${seenSocket[socketKey]})`);
      } else {
        seenSocket[socketKey] = lineNo;
      }
      const expected = `${pad3(boardNo)}-${pad2(socket)}`;
      if (label && label !== expected) {
        warnings.push(`${where}: label ${pyStrRepr(label)} does not match board/socket (${expected})`);
      }
      allRows.set(rowKeyOf(side, rowNum), { side, row: rowNum });
      if (hasShift && row[_SHIFT_COLUMN]) {
        const rawShift = row[_SHIFT_COLUMN];
        const shiftVal = pyFloatStrict(rawShift);
        if (shiftVal !== 0 && shiftVal !== 0.5) {
          problems.push(`${where}: shift must be 0 or 0.5, not ${pyStrRepr(rawShift)}`);
        } else {
          const shiftKey = rowKeyOf(side, rowNum);
          if (shiftKey in shifts && shifts[shiftKey] !== shiftVal) {
            problems.push(`${where}: shift ${shiftVal === 0 ? "0.0" : "0.5"} for ${side} row ${rowNum} does not match ${shifts[shiftKey] === 0 ? "0.0" : "0.5"} already seen for that row`);
          } else {
            shifts[shiftKey] = shiftVal;
          }
        }
      }
      // `key` is the §2.3 field name; `position` is kept as an alias
      // (same string) in case anything still reads the older name.
      scales.push({ side, row: rowNum, col, board_no: boardNo, socket, label,
                    key: posKey, position: posKey });
    }

    if (!scales.length && !problems.length) problems.push(`${name}: no scales`);
    if (hasShift) {
      const blankKeys = [];
      allRows.forEach((pos, key) => { if (!(key in shifts)) blankKeys.push(pos); });
      if (blankKeys.length) {
        blankKeys.sort((a, b) => (a.side < b.side ? -1 : a.side > b.side ? 1 : a.row - b.row));
        const listed = blankKeys.slice(0, 8).map(p => `${p.side} row ${p.row}`).join(", ");
        const more = blankKeys.length > 8 ? ` (+${blankKeys.length - 8} more)` : "";
        warnings.push(`${name}: shift is blank for ${listed}${more} - falls back to the odd/even rule there`);
      }
    }
    const boardsUsed = new Set(scales.map(s => s.board_no));
    if (boardsUsed.size > MAX_BOARDS) {
      problems.push(`${name}: ${boardsUsed.size} boards, but one unit drives at most ${MAX_BOARDS}`);
    }
    if (problems.length) return { ok: false, problems };
    const map = deriveMapFields({ name, item, scales, warnings, shifts });
    return { ok: true, map };
  }

  function shiftAt(map, side, row) {
    const key = rowKeyOf(side, row);
    return key in map.shifts ? map.shifts[key] : defaultShift(row);
  }

  function colorOf(cell) {
    if (!/^0[xX][0-9a-fA-F]{1,2}$/.test(cell)) return null;
    const code = parseInt(cell, 16);
    return code < COLOR_COUNT ? code : null;
  }

  function parseDesign(text, opts) {
    opts = opts || {};
    const name = opts.name || "grid";
    const item = opts.item !== undefined ? opts.item : null;
    const pattern = opts.pattern !== undefined ? opts.pattern : null;
    const rows = parseCsvRows(text);
    const header = (rows.length ? rows[0] : []).map(h => (h || "").trim());
    if (!(header[0] === "side" && header[1] === "row" && header[2] === "shift")) {
      const got = header.slice(0, 3).join(",") || "nothing";
      return { ok: false, problems: [`${name}: header must start with side,row,shift - got ${got}`] };
    }
    const colsRaw = header.slice(3);
    const cols = [];
    for (const h of colsRaw) {
      const n = pyIntStrict(h);
      if (n === null) return { ok: false, problems: [`${name}: the columns after shift must be position numbers (1,2,3...)`] };
      cols.push(n);
    }
    if (!cols.length) return { ok: false, problems: [`${name}: no position columns after shift`] };
    const seenCols = {};
    cols.forEach(c => { seenCols[c] = (seenCols[c] || 0) + 1; });
    const doubled = Object.keys(seenCols).map(Number).filter(c => seenCols[c] > 1).sort((a, b) => a - b);
    if (doubled.length) {
      return { ok: false, problems: [`${name}: position column(s) ${doubled.join(", ")} appear more than once - one would silently overwrite the other`] };
    }

    const problems = [];
    const colors = {};
    const undecided = [];
    const shifts = {};
    for (let r = 1; r < rows.length; r++) {
      const raw = rows[r];
      const lineNo = r + 1;
      const cells = raw.map(c => (c || "").trim());
      if (!cells.some(c => c !== "")) continue;
      const where = `${name}:${lineNo}`;
      if (cells.length < 3) {
        problems.push(`${where}: needs side, a whole-number row and a shift`);
        continue;
      }
      const side = cells[0].toLowerCase();
      const rowNum = pyIntStrict(cells[1]);
      const shiftText = cells[2];
      const shiftVal = shiftText === "" ? 0 : pyFloatStrict(shiftText);
      if (rowNum === null || shiftVal === null) {
        problems.push(`${where}: needs side, a whole-number row and a shift`);
        continue;
      }
      const rk = rowKeyOf(side, rowNum);
      if (rk in shifts) { problems.push(`${where}: ${side} row ${rowNum} appears twice`); continue; }
      shifts[rk] = shiftVal;
      if (cells.length - 3 > cols.length) {
        problems.push(`${where}: ${cells.length - 3} cells but only ${cols.length} position columns`);
      }
      const limit = Math.min(cols.length, cells.length - 3);
      for (let i = 0; i < limit; i++) {
        const col = cols[i];
        const cell = cells[i + 3];
        if (_EMPTY_CELLS.indexOf(cell) !== -1) continue;
        if (cell === _UNDECIDED) { undecided.push(posKeyOf({ side, row: rowNum, col })); continue; }
        const code = colorOf(cell);
        if (code === null) {
          problems.push(`${where}: ${posText({ side, row: rowNum, col })} has ${pyStrRepr(cell)} - write colours as 0x00-0x0F (0 = no hole, - = not decided)`);
          continue;
        }
        colors[posKeyOf({ side, row: rowNum, col })] = code;
      }
    }
    if (problems.length) return { ok: false, problems };
    return { ok: true, design: { name, item, pattern, label: "", colors, shifts, undecided,
                                 cols: cols.slice() } };
  }

  function designShiftAt(design, side, row) {
    const key = rowKeyOf(side, row);
    return key in design.shifts ? design.shifts[key] : defaultShift(row);
  }

  // conductor/look.py's _rows_by_side()/_and_list()/_rows_text(): sides in
  // first-seen order, rows as a min-max range per side.
  function rowsBySide(pairs) {
    const out = new Map();
    for (const [side, row] of pairs) {
      if (!out.has(side)) out.set(side, new Set());
      out.get(side).add(row);
    }
    return out;
  }
  function andList(parts) {
    if (parts.length < 2) return parts.length ? parts[0] : "";
    return parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
  }
  function rowsText(sides, rowsMap) {
    return andList(sides.map(side => {
      const rows = rowsMap.get(side);
      if (!rows || !rows.size) return `none (${side})`;
      const list = Array.from(rows);
      return `${Math.min.apply(null, list)}-${Math.max.apply(null, list)} (${side})`;
    }));
  }
  // conductor/look.py's geometry_problem() - see its docstring for why the
  // trigger is the grid's rows and width and never its empty cells.
  function geometryProblem(map, design) {
    if (!map.scales.length || !Object.keys(design.shifts).length) return null;
    const mapRows = rowsBySide(map.scales.map(s => [s.side, s.row]));
    const designRows = rowsBySide(Object.keys(design.shifts).map(k => {
      const sep = k.indexOf("|");
      return [k.slice(0, sep), Number(k.slice(sep + 1))];
    }));
    const mapCols = Math.max.apply(null, map.scales.map(s => s.col));
    const designCols = design.cols && design.cols.length
      ? Math.max.apply(null, design.cols) : null;
    let short = false;
    mapRows.forEach((rows, side) => {
      const mine = designRows.get(side);
      if (!mine || !mine.size) return;
      const list = Array.from(rows);
      const top = Math.max.apply(null, list);
      const missing = top - Math.max.apply(null, Array.from(mine));
      const span = top - Math.min.apply(null, list) + 1;
      if (missing >= _GEOM_MIN_SHORT_ROWS && missing * 10 > span * _GEOM_SHORT_TENTHS) short = true;
    });
    let over = false;
    designRows.forEach((rows, side) => {
      const theirs = mapRows.get(side);
      if (!theirs || Math.max.apply(null, Array.from(rows)) > Math.max.apply(null, Array.from(theirs))) over = true;
    });
    const narrow = designCols !== null && designCols < mapCols;
    if (!(short || over || narrow)) return null;
    const sides = map.sides.slice();
    designRows.forEach((_rows, side) => { if (sides.indexOf(side) === -1) sides.push(side); });
    let covers = rowsText(sides, designRows);
    if (narrow) covers += ` with only ${designCols} columns`;
    return `${design.name} covers rows ${covers} but this garment's wiring `
      + `has rows ${rowsText(sides, mapRows)} with ${mapCols} columns`
      + ` - the design was made for another layout of ${map.item || map.name};`
      + ` export it again from the current 配線ナビ (配色) page`;
  }

  function check(map, design, partial) {
    const problems = [];
    if (map.item && design.item && map.item.toLowerCase() !== design.item.toLowerCase()) {
      problems.push(`${design.name} is for ${design.item} but ${map.name} is ${map.item}`);
    } else {
      const geometry = geometryProblem(map, design);
      if (geometry) problems.push(geometry);
    }
    const keySet = new Set(Object.keys(design.colors));
    design.undecided.forEach(k => keySet.add(k));
    const askedPositions = sortedPositionKeys(keySet);
    for (const pos of askedPositions) {
      if (!(posKeyOf(pos) in map.byPosition)) {
        problems.push(`${design.name}: ${posText(pos)} is a hole in the grid but ${map.name} has no scale there`);
      }
    }
    if (!partial) {
      const allPositions = sortedPositionKeys(Object.keys(map.byPosition));
      for (const pos of allPositions) {
        const key = posKeyOf(pos);
        if (key in design.colors) continue;
        const scale = map.byPosition[key];
        const wiring = `(board ${scale.board_no} socket ${scale.socket})`;
        if (design.undecided.indexOf(key) !== -1) {
          problems.push(`${design.name}: colour not decided (-) for ${posText(pos)} ${wiring}`);
        } else {
          problems.push(`${design.name}: no colour for ${posText(pos)} ${wiring} - 0 means no hole, white is 0x00`);
        }
      }
    }
    const mismatchedRows = new Set();
    for (const s of map.scales) {
      if (designShiftAt(design, s.side, s.row) !== shiftAt(map, s.side, s.row)) {
        mismatchedRows.add(rowKeyOf(s.side, s.row));
      }
    }
    if (mismatchedRows.size) {
      const rows = mismatchedRows.size;
      const note = `${design.name}: the design's row shift differs from the map on ${rows} row${rows !== 1 ? "s" : ""}`;
      if (map.warnings.indexOf(note) === -1) map.warnings.push(note);
    }
    return problems;
  }

  function boardIds(map) {
    const ids = {};
    map.boardNos.forEach((no, i) => { ids[no] = i + 1; });
    return ids;
  }

  function dipSheet(map, ids) {
    const counts = {};
    map.scales.forEach(s => { counts[s.board_no] = (counts[s.board_no] || 0) + 1; });
    // `ids or self.board_ids` in Python: an empty dict is falsy there,
    // so {} (not just null/undefined) must also fall back - a plain
    // JS `ids || boardIds(map)` would keep an empty object (objects are
    // always truthy in JS).
    const useIds = (ids && Object.keys(ids).length) ? ids : boardIds(map);
    return map.boardNos.map(boardNo => {
      const address = useIds[boardNo];
      const switches = [];
      for (let n = 0; n < 8; n++) if ((address >> n) & 1) switches.push(n + 1);
      return { board_no: boardNo, dip_id: address, switches_on: switches.join(" "), scales: counts[boardNo] };
    });
  }

  function renumber(map, boardsForItem) {
    const own = {};
    Object.keys(boardsForItem || {}).forEach(oldStr => {
      const old = Number(oldStr);
      const neu = Number(boardsForItem[oldStr]);
      if (map.boardNos.indexOf(old) !== -1 && neu !== old) own[old] = neu;
    });
    if (!Object.keys(own).length) return map;
    const result = map.boardNos.map(no => (no in own ? own[no] : no));
    if (new Set(result).size !== result.length) {
      return Object.assign({}, map, {
        warnings: map.warnings.concat(["the board numbers set on the page no longer fit this map and are ignored"]),
      });
    }
    const scales = map.scales.map(s => {
      if (!(s.board_no in own)) return s;
      const newNo = own[s.board_no];
      return Object.assign({}, s, { board_no: newNo, label: s.label ? `${pad3(newNo)}-${pad2(s.socket)}` : "" });
    });
    return deriveMapFields(Object.assign({}, map, { scales }));
  }

  const look = {
    PALETTE, ARRAY_LEN, COLOR_COUNT, MAX_BOARDS,
    defaultShift, kind, nameParts, mapItem, normalizeName, nameProblem,
    parseMap, parseDesign, shiftAt, designShiftAt, check, geometryProblem,
    boardIds, dipSheet, renumber,
  };

  // ============================================================
  // SIM.sequence - port of conductor/sequence.py.
  // ============================================================

  const SEQUENCES = ["natural", "center", "top_down", "bottom_up", "left_right", "right_left"];
  const LABELS = {
    natural: "Socket order (P01 to P60)", center: "Centre outward",
    top_down: "Top to bottom", bottom_up: "Bottom to top",
    left_right: "Left to right (audience)", right_left: "Right to left (audience)",
  };
  const MAX_DELAY_S = 30.0;
  const SPAN_HARD_MAX_S = 120.0;

  function cleanSequence(value) { return SEQUENCES.indexOf(value) !== -1 ? value : "natural"; }

  function cleanSpan(value) {
    const num = toNumber(value);
    if (num === null) return 0.0;
    const span = fmt.round(num, 2);
    if (Number.isNaN(span)) return 0.0;
    return pyMin2(SPAN_HARD_MAX_S, pyMax2(0.0, span));
  }

  // position key ("side|row|col") -> rank. This is conductor/sequence.py's
  // own ranks() return shape (a dict); SIM.sequence.ranks() (below) is the
  // §2.3 JS-facing form, an array in map.scales order, which is what
  // render.js/flicker.js actually want to zip against map.scales.
  // Rebased, so the lowest rank of a garment is always 0 and the sweep gets
  // its whole span (conductor/sequence.py's ranks()/_rebased()). The row and
  // column sequences already count from their own extreme scale; `center`
  // measures a distance from a centroid that on a real map falls BETWEEN
  // scales, so its lowest rank is normally 1, not 0.
  function ranksByKey(map, sequence) {
    const result = rawRanksByKey(map, sequence);
    const values = Object.values(result);
    const lowest = values.length ? Math.min(...values) : 0;
    if (lowest === 0) return result;
    Object.keys(result).forEach(key => { result[key] -= lowest; });
    return result;
  }

  function rawRanksByKey(map, sequence) {
    const scales = map.scales;
    const result = {};
    if (sequence === "natural" || !scales.length) {
      scales.forEach(s => { result[s.key] = 0; });
      return result;
    }
    if (sequence === "top_down") {
      const top = Math.max(...scales.map(s => s.row));
      scales.forEach(s => { result[s.key] = top - s.row; });
      return result;
    }
    if (sequence === "bottom_up") {
      const hem = Math.min(...scales.map(s => s.row));
      scales.forEach(s => { result[s.key] = s.row - hem; });
      return result;
    }
    if (sequence === "left_right" || sequence === "right_left") {
      map.sides.forEach(side => {
        const cols = scales.filter(s => s.side === side).map(s => s.col);
        const first = Math.min(...cols), last = Math.max(...cols);
        const fromWearersRight = (side === "front") === (sequence === "left_right");
        scales.forEach(s => {
          if (s.side === side) result[s.key] = fromWearersRight ? last - s.col : s.col - first;
        });
      });
      return result;
    }
    if (sequence === "center") {
      let front = scales.filter(s => s.side === "front");
      if (!front.length) front = scales;
      const xs = front.map(s => s.col + shiftAt(map, s.side, s.row));
      const ys = front.map(s => s.row);
      const cx = xs.reduce((a, b) => a + b, 0) / xs.length;
      const cy = ys.reduce((a, b) => a + b, 0) / ys.length;
      scales.forEach(s => {
        const dx = s.col + shiftAt(map, s.side, s.row) - cx;
        const dy = s.row - cy;
        result[s.key] = fmt.roundInt(Math.hypot(dx, dy));
      });
      return result;
    }
    throw new Error(`unknown sequence ${sequence}`);
  }

  function ranks(map, sequence) {
    const byKey = ranksByKey(map, sequence);
    return map.scales.map(s => byKey[s.key]);
  }

  function spanS(map, sequence, span) {
    if (sequence === "natural") return 0.0;
    const values = Object.values(ranksByKey(map, sequence));
    const maxRank = values.length ? Math.max(...values) : 0;
    if (maxRank === 0) return 0.0;
    // Python's `round(float(span), 2)` never catches float()'s own
    // TypeError/ValueError here - junk `span` must throw, not silently
    // become 0 or NaN.
    const num = toNumber(span);
    if (num === null) throw new Error(`not a number: ${JSON.stringify(span)}`);
    return fmt.round(num, 2);
  }

  const sequenceApi = {
    SEQUENCES, LABELS, MAX_DELAY_S, SPAN_HARD_MAX_S,
    cleanSequence, cleanSpan, ranks, ranksByKey, spanS,
  };

  // ============================================================
  // SIM.timeline - port of conductor/timeline.py.
  // ============================================================

  const REFRESH_S = 7.0;
  const REFRESH_RANGE_S = [1.0, 60.0];
  const GAP_AFTER_REFRESH_S = 1.0;
  // Pre-burn conductor (main, 2026-09-25+): every picture is written to
  // its on-board slot at Upload time (showfile.py), so nothing is
  // written any more while the show runs - a running send is one
  // broadcast trigger, not a write, and the unit's old per-board write
  // time no longer bounds the timeline. SLOT_CAPACITY/MAX_CUES_PER_UNIT
  // replace it: a board has 20 slots (0 the standby white, 19 the
  // manual one-shot), so at most 18 distinct sends fit a show.
  const SLOT_CAPACITY = 20;
  const MAX_CUES_PER_UNIT = 18;
  const DEFAULT_DURATION_S = 600.0;

  const _CLOCK = /^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$/;

  function parseClock(value) {
    if (typeof value === "number") return value;
    const s = String(value);
    const m = _CLOCK.exec(s);
    if (m) {
      const hours = m[1] ? parseInt(m[1], 10) : 0;
      const minutes = parseInt(m[2], 10);
      const seconds = parseFloat(m[3]);
      return hours * 3600 + minutes * 60 + seconds;
    }
    const num = toNumber(value);
    if (num === null) throw new Error(`not a time: ${pyRepr(value)} (write m:ss)`);
    return num;
  }

  function formatClock(seconds) {
    const sign = seconds < 0 ? "-" : "";
    const total = fmt.roundInt(Math.abs(seconds));
    const m = Math.trunc(total / 60), s = total - m * 60;
    return `${sign}${m}:${pad2(s)}`;
  }

  // `boards` is kept (unused) so callers built for the old, per-board
  // write term do not need to change - matching conductor/timeline.py's
  // own min_interval() signature note.
  function minInterval(boards, refresh, gap) {
    if (refresh === undefined) refresh = REFRESH_S;
    if (gap === undefined) gap = GAP_AFTER_REFRESH_S;
    return refresh + gap;
  }

  function spanOf(cue) {
    const raw = cue.span;
    if (!raw) return 0.0;             // falsy: undefined/null/0/""/false, same as Python's `or 0.0`
    const num = toNumber(raw);
    if (num === null) return 0.0;
    return pyMax2(0.0, num);
  }

  function cleanRefresh(value) {
    if (value === null || value === undefined) return null;
    const num = toNumber(value);
    if (num === null) return null;
    const refresh = fmt.round(num, 1);
    return Number.isNaN(refresh) ? null : refresh;
  }

  function effectiveRefresh(cue, refresh) {
    if (refresh === undefined) refresh = REFRESH_S;
    const own = cue.refresh_s;
    return typeof own === "number" && !Number.isNaN(own) ? own : refresh;
  }

  function times(cue, refresh) {
    if (refresh === undefined) refresh = REFRESH_S;
    const at = Number(cue.at);
    const eff = effectiveRefresh(cue, refresh);
    const sent = at > 0 ? fmt.round(at, 3) : fmt.round(-eff, 3);
    const complete = fmt.round(sent + eff + spanOf(cue), 3);
    return [sent, complete];
  }

  function ends(cues, refresh, duration) {
    if (refresh === undefined) refresh = REFRESH_S;
    if (duration === undefined) duration = DEFAULT_DURATION_S;
    const result = Object.create(null);     // cue ids are attacker-influenced; never a plain {}
    const byItem = Object.create(null);
    cues.forEach(c => { const k = c.item.toLowerCase(); (byItem[k] = byItem[k] || []).push(c); });
    Object.keys(byItem).forEach(k => {
      const ordered = byItem[k].slice().sort((a, b) => times(a, refresh)[0] - times(b, refresh)[0]);
      ordered.forEach((c, i) => {
        result[c.id] = i + 1 < ordered.length ? [times(ordered[i + 1], refresh)[0], "next"] : [duration, "show"];
      });
    });
    return result;
  }

  function sweeps(cue) {
    const sweep = cue.sweep || {};
    const rawSpan = sweep.span_s === undefined || sweep.span_s === null ? 0.0 : sweep.span_s;
    const span = toNumber(rawSpan);
    if (span === null) return false;
    const seq = sweep.sequence !== undefined ? sweep.sequence : "natural";
    return seq !== "natural" && span > 0;
  }

  function clean(cuesRaw) {
    const result = [];
    (cuesRaw || []).forEach(raw => {
      if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return;
      const transition = raw.transition;
      const id = pyStr(raw.id || `c${result.length}`).slice(0, 40);
      const item = pyStr(raw.item !== undefined ? raw.item : "");
      const atParsed = parseClock(raw.at !== undefined ? raw.at : 0);
      const at = pyMax2(0.0, fmt.round(atParsed, 1));
      result.push({
        id, item, at,
        design: pyStr(raw.design !== undefined ? raw.design : ""),
        partial: Boolean(raw.partial !== undefined ? raw.partial : false),
        refresh_s: cleanRefresh(raw.refresh_s),
        transition: (transition === "design" || transition === "custom") ? transition : "design",
        sequence: cleanSequence(raw.sequence !== undefined ? raw.sequence : "natural"),
        span_s: cleanSpan(raw.span_s !== undefined ? raw.span_s : 0.0),
      });
    });
    result.sort((a, b) => (a.at - b.at) || (a.item.toLowerCase() < b.item.toLowerCase() ? -1 : a.item.toLowerCase() > b.item.toLowerCase() ? 1 : 0));
    return result;
  }

  function resolve(cue, transitions) {
    const t = cue.transition !== undefined ? cue.transition : "design";
    if (t === "custom") {
      return {
        sequence: cleanSequence(cue.sequence !== undefined ? cue.sequence : "natural"),
        span_s: cleanSpan(cue.span_s !== undefined ? cue.span_s : 0.0),
        source: "cue",
      };
    }
    let entry = transitions ? transitions[cue.design !== undefined ? cue.design : ""] : undefined;
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) entry = {};
    return {
      sequence: cleanSequence(entry.sequence !== undefined ? entry.sequence : "natural"),
      span_s: cleanSpan(entry.span_s !== undefined ? entry.span_s : 0.0),
      source: "design",
    };
  }

  function applyTransitions(cues, transitions) {
    cues.forEach(c => { c.sweep = resolve(c, transitions); });
  }

  function validate(cues, items, duration, refresh, gap) {
    if (duration === undefined) duration = DEFAULT_DURATION_S;
    if (refresh === undefined) refresh = REFRESH_S;
    if (gap === undefined) gap = GAP_AFTER_REFRESH_S;
    const problems = Object.create(null);   // cue ids are attacker-influenced; never a plain {}
    cues.forEach(c => { problems[c.id] = []; });
    const warnings = [];

    cues.forEach(cue => {
      const mine = problems[cue.id];
      const item = items[cue.item.toLowerCase()];
      if (!item) { mine.push(`${cue.item}: no such item (is its map loaded?)`); return; }
      const design = item.designs[cue.design];
      if (!design) {
        mine.push(`design ${cue.design || "(none)"} is not loaded`);
      } else if (!design[cue.partial ? "partial" : "full"]) {
        mine.push(`${cue.design} has problems (see the Designs tab)` +
          ((cue.partial || !design.partial) ? "" : " - it has undecided scales: make this a partial cue"));
      }
      if (cue.at > duration) {
        mine.push(`${formatClock(cue.at)} is after the end of the show (${formatClock(duration)})`);
      }
      const ownRefresh = cue.refresh_s;
      if (ownRefresh !== null && ownRefresh !== undefined) {
        const low = REFRESH_RANGE_S[0], high = REFRESH_RANGE_S[1];
        if (!(low <= ownRefresh && ownRefresh <= high)) {
          mine.push(`this cue's refresh time (${fmt.g(ownRefresh)} s) must be ${fmt.fixed(low, 0)}-${fmt.fixed(high, 0)} s`);
        }
      }
      const sweep = cue.sweep || { sequence: "natural", span_s: 0.0 };
      if (sweep.sequence !== "natural") {
        if (cue.span === undefined || cue.span === null) mine.push("the sweep needs the item's map to be timed");
        if (sweep.span_s > MAX_DELAY_S) mine.push("a sweep is at most 30 s from the first scale to the last (the firmware's limit)");
      }
    });

    const seen = {};
    cues.forEach(cue => {
      const key = JSON.stringify([cue.item.toLowerCase(), times(cue, refresh)[0]]);
      if (key in seen) problems[cue.id].push(`${cue.item} already has a cue sent at the same moment`);
      else seen[key] = cue.id;
    });

    const overlapped = new Set();
    const byItem2 = Object.create(null);
    cues.forEach(c => { const k = c.item.toLowerCase(); (byItem2[k] = byItem2[k] || []).push(c); });
    Object.keys(byItem2).forEach(k => {
      const ordered = byItem2[k].slice().sort((a, b) => times(a, refresh)[0] - times(b, refresh)[0]);
      for (let i = 0; i < ordered.length - 1; i++) {
        const prev = ordered[i], cur = ordered[i + 1];
        const prevTimes = times(prev, refresh);
        const curSent = times(cur, refresh)[0];
        if (curSent !== prevTimes[0] && curSent < prevTimes[1]) {
          problems[cur.id].push(`starts before the previous picture is complete (${formatClock(prevTimes[1])})`);
          overlapped.add(cur.id);
        }
      }
    });

    // Each unit's bus: refreshes need room between their send times, and
    // a board holds only MAX_CUES_PER_UNIT pictures.
    const byUnit = Object.create(null);
    cues.forEach(cue => {
      const item = items[cue.item.toLowerCase()];
      if (item) {
        const unit = item.unit || `(${item.item})`;
        (byUnit[unit] = byUnit[unit] || []).push(cue);
      }
    });
    Object.keys(byUnit).forEach(unit => {
      // Grouped by send instant, matching showfile.py's own broadcasts:
      // items sharing a unit and an instant (Look 20's top and skirt)
      // are ONE send, and the room needed after it is set by ALL of
      // them together (the slowest refresh, the longest sweep).
      const momentCues = Object.create(null);
      byUnit[unit].forEach(cue => {
        const sent = times(cue, refresh)[0];
        (momentCues[sent] = momentCues[sent] || []).push(cue);
      });
      const moments = Object.keys(momentCues).map(Number).sort((a, b) => a - b);

      // A board has MAX_CUES_PER_UNIT usable slots for the show (0 is
      // the standby white, 19 the manual one-shot): more distinct sends
      // than that do not fit, whatever their spacing.
      if (moments.length > MAX_CUES_PER_UNIT) {
        const order = Object.create(null);
        moments.forEach((sent, index) => { order[sent] = index; });
        byUnit[unit].forEach(cue => {
          if (order[times(cue, refresh)[0]] >= MAX_CUES_PER_UNIT) {
            problems[cue.id].push(`${unit} carries ${moments.length} pictures but a board `
              + `holds ${MAX_CUES_PER_UNIT} show pictures (slot 0 is the white standby, `
              + "slot 19 the manual one-shot) - merge or remove cues");
          }
        });
      }

      let previousSent = null, previousGroup = null;
      moments.forEach(sent => {
        const group = momentCues[sent];
        if (previousSent !== null) {
          const spacing = sent - previousSent;
          // Every picture is already burned into its slot at Upload
          // time: a running send is one broadcast trigger, nothing is
          // written - so the only floor left is the director's gap
          // after the PREVIOUS send's own refresh (plus its sweep's
          // span, if any of its cues had one), for every pair.
          const beforeRefresh = Math.max(...previousGroup.map(c => effectiveRefresh(c, refresh)));
          const beforeSpan = Math.max(...previousGroup.map(c => spanOf(c)));
          const need = beforeRefresh + beforeSpan + gap;
          if (spacing < need) {
            let detail = `${fmt.fixed(beforeRefresh, 1)} s refresh`;
            if (beforeSpan) detail += ` + ${fmt.fixed(beforeSpan, 1)} s sweep`;
            detail += ` + ${fmt.fixed(gap, 1)} s gap`;
            group.forEach(cue => {
              const sameItem = previousGroup.some(prev => cue.item.toLowerCase() === prev.item.toLowerCase());
              if (sameItem && overlapped.has(cue.id)) return;
              problems[cue.id].push(`only ${fmt.fixed(spacing, 1)} s after the previous send on ${unit}; `
                + `at least ${fmt.fixed(need, 1)} s is needed (${detail})`);
            });
          }
        }
        previousSent = sent; previousGroup = group;
      });
    });

    Object.keys(items).sort().forEach(key => {
      const item = items[key];
      const track = cues.filter(c => c.item.toLowerCase() === key);
      if (track.length && !track.some(c => c.at <= 0)) {
        warnings.push(`${item.item}: no preset at 0:00 - it opens on whatever it showed before the show`);
      }
    });
    return { problems, warnings };
  }

  const timeline = {
    REFRESH_S, REFRESH_RANGE_S, GAP_AFTER_REFRESH_S,
    SLOT_CAPACITY, MAX_CUES_PER_UNIT, DEFAULT_DURATION_S,
    parseClock, formatClock, cleanRefresh, effectiveRefresh, spanOf,
    sweeps, clean, resolve, applyTransitions, times, ends, minInterval, validate,
  };

  globalThis.SIM = Object.assign(globalThis.SIM || {}, { fmt, mmss, look, sequence: sequenceApi, timeline });
  // Exposed for state.js/selftest.js within this same script-load order;
  // not part of the frozen §2.3 surface, but shared rather than
  // reimplemented.
  globalThis.SIM._internal = Object.assign(globalThis.SIM._internal || {}, {
    pyRepr, pyStrRepr, posKeyOf, rowKeyOf, parsePosKey, posText,
    comparePosition, sortedPositionKeys, pad2, pad3, parseCsvRows,
    pyIntStrict, pyFloatStrict, toNumber,
  });
})();
