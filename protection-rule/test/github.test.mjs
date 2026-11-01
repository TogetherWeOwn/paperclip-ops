// Offline suite for the installation-token mint (src/github.js).
//
// The app suite stubs `mintToken`, so this is the only place the real JWT and
// mint path runs. Keys are generated per run with node:crypto; the GitHub
// transport is a recording stub. The rows that carry this suite:
//
//   * the JWT is RS256, signed by the configured key, issued by the configured
//     App, backdated 60 s and valid for 10 minutes — GitHub refuses anything
//     else, and a JWT signed by another key must never verify;
//   * PKCS#8 and SEC1/RSA armor both import; any other armor refuses before
//     the transport is touched;
//   * only HTTP 201 with a clean token string is a mint — any other status or
//     body throws, so the caller fails closed;
//   * a missing or malformed identity refuses before any request is built.

import { generateKeyPairSync, createVerify } from 'node:crypto'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { mintInstallationToken } from '../src/github.js'

const APP_ID = '424242'
const INSTALLATION_ID = 155772577
const NOW_MS = Date.UTC(2026, 9, 31, 12, 0, 0)

function rsaKeys(modulusLength = 2048) {
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength })
  return {
    pkcs8: privateKey.export({ type: 'pkcs8', format: 'pem' }),
    pkcs1: privateKey.export({ type: 'pkcs1', format: 'pem' }),
    publicKey,
  }
}

const KEYS = rsaKeys()

// Assembled at runtime so no literal key block sits in the source.
function armor(label, body) {
  return `-----BEGIN ${label}-----\n${body}\n-----END ${label}-----\n`
}

function transport({ status = 201, body = { token: 'ghs_minted' } } = {}) {
  const requests = []
  const fetchImpl = async (url, init = {}) => {
    requests.push({ url: String(url), init })
    return Response.json(body, { status })
  }
  return { fetchImpl, requests }
}

function decodeSegment(segment) {
  return JSON.parse(Buffer.from(segment, 'base64url').toString('utf8'))
}

function jwtFrom(request) {
  const auth = request.init.headers.authorization
  assert.match(auth, /^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/)
  const [header, body, signature] = auth.slice('Bearer '.length).split('.')
  return { header, body, signature }
}

function verifies(jwt, publicKey) {
  const verifier = createVerify('RSA-SHA256')
  verifier.update(`${jwt.header}.${jwt.body}`)
  return verifier.verify(publicKey, Buffer.from(jwt.signature, 'base64url'))
}

async function mint(overrides = {}, stub = transport()) {
  const token = await mintInstallationToken({
    fetchImpl: stub.fetchImpl, appId: APP_ID, privateKeyPem: KEYS.pkcs8,
    installationId: INSTALLATION_ID, nowMs: NOW_MS, ...overrides,
  })
  return { token, requests: stub.requests }
}

test('PKCS#8 key mints one token for exactly the claimed installation', async () => {
  const { token, requests } = await mint()
  assert.equal(token, 'ghs_minted')
  assert.equal(requests.length, 1)
  assert.equal(requests[0].url, `https://api.github.com/app/installations/${INSTALLATION_ID}/access_tokens`)
  assert.equal(requests[0].init.method, 'POST')
})

test('JWT is RS256, issued by the App, backdated 60 s and valid for 10 minutes', async () => {
  const { requests } = await mint()
  const jwt = jwtFrom(requests[0])
  assert.deepEqual(decodeSegment(jwt.header), { alg: 'RS256', typ: 'JWT' })
  const claims = decodeSegment(jwt.body)
  const issued = Math.floor(NOW_MS / 1000) - 60
  assert.deepEqual(claims, { iss: APP_ID, iat: issued, exp: issued + 600 })
})

test('JWT signature verifies under the configured key and no other', async () => {
  const { requests } = await mint()
  const jwt = jwtFrom(requests[0])
  assert.equal(verifies(jwt, KEYS.publicKey), true)
  assert.equal(verifies(jwt, rsaKeys().publicKey), false)
})

test('numeric App id is issued as its decimal string', async () => {
  const { requests } = await mint({ appId: 424242 })
  assert.equal(decodeSegment(jwtFrom(requests[0]).body).iss, '424242')
})

test('SEC1/RSA armor is wrapped and signs with the same key', async () => {
  for (const bits of [2048, 4096]) {
    const keys = bits === 2048 ? KEYS : rsaKeys(bits)
    const { token, requests } = await mint({ privateKeyPem: keys.pkcs1 })
    assert.equal(token, 'ghs_minted', `${bits}-bit`)
    assert.equal(verifies(jwtFrom(requests[0]), keys.publicKey), true, `${bits}-bit`)
  }
})

test('CRLF line endings in the configured key still import', async () => {
  const { token } = await mint({ privateKeyPem: KEYS.pkcs8.replace(/\n/g, '\r\n') })
  assert.equal(token, 'ghs_minted')
})

test('unsupported or broken key armor refuses before any request', async () => {
  const ec = generateKeyPairSync('ec', { namedCurve: 'P-256' }).privateKey
  const encrypted = generateKeyPairSync('rsa', {
    modulusLength: 2048,
    privateKeyEncoding: { type: 'pkcs8', format: 'pem', cipher: 'aes-256-cbc', passphrase: 'not-a-real-one' },
    publicKeyEncoding: { type: 'spki', format: 'pem' },
  }).privateKey
  const cases = {
    'EC PRIVATE KEY': ec.export({ type: 'sec1', format: 'pem' }),
    'ENCRYPTED PRIVATE KEY': encrypted,
    'OPENSSH PRIVATE KEY': armor('OPENSSH PRIVATE KEY', 'AAAA'),
    'no armor': 'PRIVATE KEY without any armor',
    'corrupt body': armor('PRIVATE KEY', '!!!not base64!!!'),
    // A valid key body under an unsupported label: only the label refuses it.
    'PKCS#8 body under OPENSSH label': KEYS.pkcs8.replace(/PRIVATE KEY/g, 'OPENSSH PRIVATE KEY'),
    'PKCS#8 body under RSA label':KEYS.pkcs8.replace(/PRIVATE KEY/g, 'RSA PRIVATE KEY'),
  }
  for (const [name, privateKeyPem] of Object.entries(cases)) {
    const stub = transport()
    await assert.rejects(mint({ privateKeyPem }, stub), { message: 'GitHub App key is invalid' }, name)
    assert.equal(stub.requests.length, 0, name)
  }
})

test('any status other than 201 is a failed mint, including other 2xx', async () => {
  for (const status of [200, 202, 401, 403, 404, 422, 500]) {
    await assert.rejects(mint({}, transport({ status })),
      { message: 'GitHub installation token mint failed' }, `status ${status}`)
  }
})

test('a 201 without one clean token string is a failed mint', async () => {
  const bodies = [
    null,
    {},
    { token: '' },
    { token: 42 },
    { token: ['ghs_minted'] },
    { token: 'ghs_minted\n' },
    { token: 'ghs minted' },
  ]
  for (const body of bodies) {
    await assert.rejects(mint({}, transport({ body })),
      { message: 'GitHub installation token response is invalid' }, JSON.stringify(body))
  }
})

test('a missing or malformed identity refuses before any request', async () => {
  const cases = [
    [{ fetchImpl: undefined }, 'GitHub transport is required'],
    [{ appId: '' }, 'GitHub App identity is not configured'],
    [{ appId: undefined }, 'GitHub App identity is not configured'],
    [{ appId: 1.5 }, 'GitHub App identity is not configured'],
    [{ appId: Number.NaN }, 'GitHub App identity is not configured'],
    [{ privateKeyPem: '' }, 'GitHub App key is not configured'],
    [{ privateKeyPem: '-----BEGIN PUBLIC KEY-----' }, 'GitHub App key is not configured'],
    [{ installationId: 0 }, 'GitHub installation is invalid'],
    [{ installationId: -1 }, 'GitHub installation is invalid'],
    [{ installationId: 1.5 }, 'GitHub installation is invalid'],
    [{ installationId: String(INSTALLATION_ID) }, 'GitHub installation is invalid'],
    [{ installationId: '1/../../user' }, 'GitHub installation is invalid'],
  ]
  for (const [overrides, message] of cases) {
    const stub = transport()
    await assert.rejects(mint(overrides, stub), { message }, JSON.stringify(overrides))
    assert.equal(stub.requests.length, 0, JSON.stringify(overrides))
  }
})
