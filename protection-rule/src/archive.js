import { parseDocument } from './document.js'

export const MAX_MANIFEST_BYTES = 1 * 1024 * 1024
export const MAX_ZIP_BYTES = 16 * 1024 * 1024

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

function crc32(bytes) {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0)
  }
  return (crc ^ 0xffffffff) >>> 0
}

// upload-artifact uses archiver's streaming ZIP writer, even at compression 0:
// https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/upload/zip.ts
// Stored entries may have flag 3 and a trailing data descriptor. Bound data by
// the central directory, then validate the descriptor, local header AND CRC.
// Exactly one named regular JSON file; no extraction to disk, Zip64, deflate,
// symlinks, paths, encryption, extra entries or unaccounted archive bytes.
export function extractManifest(zip, filename = 'staging-migrate-manifest.json') {
  requireValue(zip instanceof Uint8Array && zip.length >= 22 && zip.length <= MAX_ZIP_BYTES, 'artifact size is invalid')
  const view = new DataView(zip.buffer, zip.byteOffset, zip.length)
  const eocd = zip.length - 22
  requireValue(view.getUint32(eocd, true) === 0x06054b50, 'artifact end directory is invalid')
  requireValue(view.getUint16(eocd + 4, true) === 0 && view.getUint16(eocd + 6, true) === 0, 'artifact is multi-disk')
  requireValue(view.getUint16(eocd + 8, true) === 1 && view.getUint16(eocd + 10, true) === 1, 'artifact must hold one file')
  requireValue(view.getUint16(eocd + 20, true) === 0, 'artifact comments are unsupported')
  const centralSize = view.getUint32(eocd + 12, true)
  const central = view.getUint32(eocd + 16, true)
  requireValue(central + centralSize === eocd && centralSize >= 46 && central > 30, 'artifact central bounds are invalid')
  requireValue(view.getUint32(central, true) === 0x02014b50, 'artifact central header is invalid')
  const flags = view.getUint16(central + 8, true)
  const size = view.getUint32(central + 24, true)
  const checksum = view.getUint32(central + 16, true)
  const nameLength = view.getUint16(central + 28, true)
  const extraLength = view.getUint16(central + 30, true)
  const commentLength = view.getUint16(central + 32, true)
  requireValue((flags & ~0x0808) === 0 && view.getUint16(central + 10, true) === 0, 'artifact must be stored and unencrypted')
  requireValue(size > 0 && size <= MAX_MANIFEST_BYTES && view.getUint32(central + 20, true) === size, 'artifact entry size is invalid')
  requireValue(46 + nameLength + extraLength + commentLength === centralSize, 'artifact central length is invalid')
  requireValue(view.getUint16(central + 34, true) === 0 && view.getUint32(central + 42, true) === 0, 'artifact disk or local offset is invalid')
  const fileType = (view.getUint32(central + 38, true) >>> 16) & 0xf000
  requireValue(fileType === 0 || fileType === 0x8000, 'artifact entry is not a regular file')
  const decode = (bytes) => new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  requireValue(decode(zip.subarray(central + 46, central + 46 + nameLength)) === filename, 'artifact filename is invalid')
  function extras(start, length) {
    const end = start + length
    while (start < end) {
      requireValue(start + 4 <= end, 'artifact extra field is truncated')
      requireValue(view.getUint16(start, true) !== 1, 'Zip64 is unsupported')
      start += 4 + view.getUint16(start + 2, true)
      requireValue(start <= end, 'artifact extra field is invalid')
    }
  }
  extras(central + 46 + nameLength, extraLength)
  requireValue(view.getUint32(0, true) === 0x04034b50 && view.getUint16(6, true) === flags && view.getUint16(8, true) === 0, 'artifact local header disagrees')
  const localNameLength = view.getUint16(26, true)
  const localExtraLength = view.getUint16(28, true)
  const dataStart = 30 + localNameLength + localExtraLength
  requireValue(dataStart + size <= central, 'artifact entry is out of bounds')
  requireValue(decode(zip.subarray(30, 30 + localNameLength)) === filename, 'artifact local filename disagrees')
  extras(30 + localNameLength, localExtraLength)
  const descriptor = (flags & 8) !== 0
  for (const [offset, expected] of [[14, checksum], [18, size], [22, size]]) {
    requireValue(view.getUint32(offset, true) === expected || (descriptor && view.getUint32(offset, true) === 0), 'artifact local size or CRC disagrees')
  }
  let end = dataStart + size
  if (descriptor) {
    requireValue(end + 12 <= central, 'artifact data descriptor is absent')
    if (view.getUint32(end, true) === 0x08074b50) end += 4
    requireValue(end + 12 === central, 'artifact data descriptor bounds are invalid')
    requireValue(view.getUint32(end, true) === checksum && view.getUint32(end + 4, true) === size && view.getUint32(end + 8, true) === size, 'artifact data descriptor disagrees')
    end += 12
  }
  requireValue(end === central, 'artifact has unaccounted entry bytes')
  const bytes = zip.subarray(dataStart, dataStart + size)
  requireValue(crc32(bytes) === checksum, 'artifact CRC is invalid')
  const manifest = parseDocument(decode(bytes))
  requireValue(manifest && typeof manifest === 'object' && !Array.isArray(manifest), 'artifact document is invalid')
  return { bytes, manifest }
}
