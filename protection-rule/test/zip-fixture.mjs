// Independent stored-ZIP builder, including streamed entries with descriptors.
// The production reader shares no parsing/CRC code with this test encoder.
export function zipOfTexts(texts, { name = 'staging-migrate-manifest.json', descriptor = false, descriptorSignature = true } = {}) {
  const parts = []
  const centrals = []
  let offset = 0
  texts.forEach((text, index) => {
    const data = new TextEncoder().encode(text)
    const filename = new TextEncoder().encode(index === 0 ? name : `entry-${index}.json`)
    const crcTable = Array.from({ length: 256 }, (_, n) => {
      for (let i = 0; i < 8; i++) n = n & 1 ? (n >>> 1) ^ 0xedb88320 : n >>> 1
      return n >>> 0
    })
    let crc = 0xffffffff
    for (const byte of data) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 255]
    crc = (crc ^ 0xffffffff) >>> 0
    const flags = 0x800 | (descriptor ? 8 : 0)
    const local = new Uint8Array(30 + filename.length + data.length)
    const view = new DataView(local.buffer)
    view.setUint32(0, 0x04034b50, true)
    view.setUint16(4, 20, true)
    view.setUint16(6, flags, true)
    if (!descriptor) {
      view.setUint32(14, crc, true)
      view.setUint32(18, data.length, true)
      view.setUint32(22, data.length, true)
    }
    view.setUint16(26, filename.length, true)
    local.set(filename, 30)
    local.set(data, 30 + filename.length)
    parts.push(local)
    let tail = new Uint8Array()
    if (descriptor) {
      tail = new Uint8Array(descriptorSignature ? 16 : 12)
      const t = new DataView(tail.buffer)
      const start = descriptorSignature ? 4 : 0
      if (descriptorSignature) t.setUint32(0, 0x08074b50, true)
      t.setUint32(start, crc, true)
      t.setUint32(start + 4, data.length, true)
      t.setUint32(start + 8, data.length, true)
      parts.push(tail)
    }
    const central = new Uint8Array(46 + filename.length)
    const cview = new DataView(central.buffer)
    cview.setUint32(0, 0x02014b50, true)
    cview.setUint16(4, 0x314, true)
    cview.setUint16(6, 20, true)
    cview.setUint16(8, flags, true)
    cview.setUint32(16, crc, true)
    cview.setUint32(20, data.length, true)
    cview.setUint32(24, data.length, true)
    cview.setUint16(28, filename.length, true)
    cview.setUint32(38, 0x81a40000, true)
    cview.setUint32(42, offset, true)
    central.set(filename, 46)
    centrals.push(central)
    offset += local.length + tail.length
  })
  const centralDir = Buffer.concat(centrals)
  const end = new Uint8Array(22)
  const eview = new DataView(end.buffer)
  eview.setUint32(0, 0x06054b50, true)
  eview.setUint16(8, texts.length, true)
  eview.setUint16(10, texts.length, true)
  eview.setUint32(12, centralDir.length, true)
  eview.setUint32(16, offset, true)
  return Uint8Array.from(Buffer.concat([...parts, centralDir, end]))
}
export function zipOf(text, options = {}) { return zipOfTexts([text], options) }
