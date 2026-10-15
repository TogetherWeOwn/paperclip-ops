// Evidence JSON keeps Rust i64s and GitHub int64 IDs losslessly. JSON.parse
// alone rounds them before a reviver can see them on Node 20. Reject duplicate
// keys (including escaped aliases) rather than choosing one binding silently.
export function parseDocument(text) {
  let offset = 0
  const whitespace = () => { while (/[\t\n\r ]/.test(text[offset] ?? '\0')) offset++ }
  const fail = () => { throw new Error('evidence JSON is invalid or ambiguous') }
  function string() {
    const start = offset++
    while (offset < text.length) {
      const ch = text[offset++]
      if (ch === '"') return JSON.parse(text.slice(start, offset))
      if (ch === '\\') offset++
    }
    return fail()
  }
  function value(depth) {
    if (depth > 40) return fail()
    whitespace()
    const ch = text[offset]
    if (ch === '"') return string()
    if (ch === '{' || ch === '[') {
      offset++
      const object = ch === '{'
      const out = object ? {} : []
      const keys = new Set()
      const end = object ? '}' : ']'
      whitespace()
      if (text[offset] === end) { offset++; return out }
      for (;;) {
        whitespace()
        let key
        if (object) {
          if (text[offset] !== '"') return fail()
          key = string()
          if (keys.has(key)) return fail()
          keys.add(key)
          whitespace()
          if (text[offset++] !== ':') return fail()
        }
        const entry = value(depth + 1)
        if (object) Object.defineProperty(out, key, { value: entry, enumerable: true, writable: true, configurable: true })
        else out.push(entry)
        whitespace()
        if (text[offset] === end) { offset++; return out }
        if (text[offset++] !== ',') return fail()
      }
    }
    for (const [literal, parsed] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(literal, offset)) { offset += literal.length; return parsed }
    }
    const number = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(offset))
    if (!number || number[0].length > 20) return fail()
    offset += number[0].length
    if (/[.eE]/.test(number[0])) return fail() // Rust i64 / GitHub int64, not JSON floats.
    const integer = BigInt(number[0])
    if (integer > BigInt(Number.MAX_SAFE_INTEGER) || integer < BigInt(Number.MIN_SAFE_INTEGER)) return integer
    return Number(number[0])
  }
  const result = value(0)
  whitespace()
  if (offset !== text.length) return fail()
  return result
}

const MAX_I64 = 9223372036854775807n
const MIN_I64 = -9223372036854775808n
export function isRunId(value) {
  return typeof value === 'string' && /^[1-9]\d{0,18}$/.test(value) && BigInt(value) <= MAX_I64
}

export function runId(value) {
  if (typeof value === 'bigint') value = value.toString()
  if (typeof value === 'number' && Number.isSafeInteger(value)) value = String(value)
  if (!isRunId(value)) throw new Error('run identity is invalid')
  return value
}

// Manifest versions are JSON integers, claim versions are canonical strings.
// Do not accept a manifest string as an integer or round an unsafe Number.
export function versionString(value, claim = false) {
  if (claim) {
    if (typeof value !== 'string' || !/^(?:0|-?[1-9]\d{0,18})$/.test(value)) throw new Error('version is invalid')
  } else {
    if (typeof value !== 'bigint' && !(typeof value === 'number' && Number.isSafeInteger(value) && !Object.is(value, -0))) {
      throw new Error('version must be a lossless integer')
    }
    value = String(value)
  }
  const integer = BigInt(value)
  if (integer < MIN_I64 || integer > MAX_I64) throw new Error('version is out of range')
  return value
}
