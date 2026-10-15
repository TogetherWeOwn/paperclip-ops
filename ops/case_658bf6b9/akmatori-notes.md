# Read-only diagnosis source boundary

The proposed pilot is limited to exactly one SSH host, selected through a
separately authorized private deployment. This public source does not identify
that host or claim installation. A server-side forced command must dispatch only
`ro-shell.sh` allowlisted diagnostics. Disable forwarding and interactive access;
reject chaining, unknown verbs and arbitrary arguments. Application-level
approval gates do not substitute for this server-side enforcement.

An alert may initiate read-only investigation, not automatic remediation.
No fix proposal is approved by a successful diagnosis or by this document.
Evidence must remain redacted and tied to the exact source revision. Changing
the remote scope or installing a key requires separate authorization and a
recorded rollback. The public fixture tests exercise rejection boundaries without
contacting a real host.
