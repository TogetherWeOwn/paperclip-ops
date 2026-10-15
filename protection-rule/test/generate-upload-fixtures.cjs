// Offline regeneration only. Supply archiver@7.0.1 from disposable scratch;
// it is NOT a runtime/CI dependency. This matches the upload-artifact v7.0.1
// toolkit's archiver.create('zip', { zlib: { level: 0 } }) + zip.file stream.
const { createRequire } = require('node:module')
const { createWriteStream } = require('node:fs')
const { join } = require('node:path')
const archiver = createRequire(join(process.argv[2], 'package.json'))('archiver')
const directory = join(__dirname, 'fixtures')
async function generate(name) {
  const output = createWriteStream(join(directory, name + '.upload-artifact.zip'), { flags: 'wx' })
  const zip = archiver.create('zip', { highWaterMark: 8 * 1024 * 1024, zlib: { level: 0 } })
  const done = new Promise((resolve, reject) => {
    output.on('close', resolve)
    output.on('error', reject)
    zip.on('error', reject)
    zip.on('warning', reject)
  })
  const filename = name === 'staging-migrate-plan' ? 'staging-migrate-manifest.json' : name + '.json'
  zip.file(join(directory, name + '.json'), { name: filename, date: new Date('2026-10-04T10:00:00Z') })
  zip.pipe(output)
  await zip.finalize()
  await done
}
Promise.all(['staging-migrate-plan', 'staging-migrate-apply-claim'].map(generate)).catch((error) => { console.error(error.message); process.exitCode = 1 })
