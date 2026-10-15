# Settings file-bind source contract

This public packet contains reusable validation source, not an installation
record or permission to change a service. Historical private host evidence and
maintenance-window details are intentionally excluded.

Use `settings-bind-rehearsal.mjs` with a reviewed immutable image and a newly
created disposable fixture home. Its baseline, read-only bound-file, kill-switch,
and rollback phases must all pass. Keep networking disabled, mount no Docker
socket or live home, supply no production database or credentials, and retain
the original image entrypoint. The tests use synthetic inputs and stub Docker;
a passing unit suite is not evidence of a kernel mount or a live deployment.

## Guarded file-bind validation source

The container name `paperclip` below is a syntax-contract example for an isolated
fixture only. Do not run this against a production container. Confirm the exact
settings FILE mount is read-only before either write-vector probe; a read-only
parent or a missing mount is not evidence. The script also checks this boundary
before opening settings. It never prints settings contents.

The direct-write probe opens without truncation. The rename probe uses identical
bytes and removes its sibling temporary file on denial. Session storage must
remain writable, and the installed guard must remain present. Both settings
bytes and inode must survive; the measured kill-switch state is reported rather
than changed. A separately authorized deployment requires its own backup,
review, maintenance and rollback evidence; this source packet authorizes none.

```sh
docker exec -u 1000:1000 -i paperclip node <<'NODE'
const fs = require('node:fs'); const cp = require('node:child_process');
const crypto = require('node:crypto'); const assert = require('node:assert/strict');
const file = '/paperclip/.claude/settings.json';
const lf = String.fromCharCode(10);
const mount = () => fs.readFileSync('/proc/self/mountinfo','utf8').split(lf).filter(l => l.split(' ')[4] === file);
assert.equal(process.getuid(),1000); assert.equal(process.getgid(),1000);
const m = mount(); assert.equal(m.length,1); assert(m[0].split(' ')[5].split(',').includes('ro'));
const snap = () => { const s=fs.statSync(file); return { uid:s.uid,gid:s.gid,mode:s.mode&511,size:s.size,inode:s.ino,
  sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') }; };
const before=snap(); const out={ before };
try { const fd=fs.openSync(file,'r+'); fs.closeSync(fd); throw Error('V1 remained writable'); }
catch(e) { assert(['EROFS','EACCES','EPERM'].includes(e.code)); out.directWriteDenied=e.code; }
assert.deepEqual(snap(),before); assert.deepEqual(mount(),m);
const temp='/paperclip/.claude/.bind-rename-'+crypto.randomUUID();
fs.writeFileSync(temp,fs.readFileSync(file),{mode:before.mode,flag:'wx'});
try { fs.renameSync(temp,file); throw Error('V2 rename succeeded'); }
catch(e) { assert(['EBUSY','EROFS','EACCES','EPERM'].includes(e.code)); out.renameDenied=e.code; }
finally { if(fs.existsSync(temp)) fs.unlinkSync(temp); }
assert.deepEqual(snap(),before); assert.deepEqual(mount(),m);
const session='/paperclip/.claude/.bind-session-'+crypto.randomUUID();
const payload='session-fixture'+lf;
try { fs.writeFileSync(session,payload,{mode:384,flag:'wx'}); assert.equal(fs.readFileSync(session,'utf8'),payload); out.sessionWrite=true; }
finally { if(fs.existsSync(session)) fs.unlinkSync(session); }
const guard=cp.spawnSync('node',['/paperclip/muse-stop-guard/install-muse-stop-guard.mjs','status'],{encoding:'utf8'});
assert.equal(guard.status,0); const status=JSON.parse(guard.stdout); assert.equal(status.installed,true); assert.equal(status.scriptExists,true);
out.guardInstalled=true; out.killSwitchPresent=status.killSwitchPresent; out.after=snap();
assert.deepEqual(out.after,before); console.log(JSON.stringify(out));
NODE
```

CI extracts this heredoc and runs `node --check`, never `docker exec`. Negative
controls replace the newline construction with invalid JavaScript to prove the
syntax check is load-bearing. Rehearsal rollback removes the fixture's exact file
mount and must recover both direct-write and rename positive controls without
changing the settings bytes.
