# Security Policy

## Reporting Issues

Please report security issues privately by opening a GitHub security advisory
for this repository, or by contacting the maintainer directly if advisories are
not available.

Do not open public issues that contain Apple ID credentials, app-specific
passwords, R2 access keys, sync keys, provisioning profiles, signing identities,
private keys, IPA files, or device identifiers.

## Secrets And Local Data

Catapult stores local state under:

```text
~/.catapult/
~/Library/Application Support/Catapult/
```

Apple auth material is stored in the macOS Keychain where possible. Optional
cross-device sync encrypts manifests and IPA blobs before upload, but the sync
key still needs to be protected like a password.

Public releases must not include user-specific sync configuration, even if the
configuration is encrypted. Personal encrypted-sync DMGs are for trusted private
handoff only.
