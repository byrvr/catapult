# IPA Signing

Catapult signs IPAs using macOS's `codesign` tool with a temporary isolated keychain. This replicates what Xcode does when running an app on a connected device.

## Why Re-Sign?

App Store IPAs are signed with the original developer's distribution certificate. Installing them on a device not in their provisioning profile fails. Re-signing with:
- A development certificate from **your** Apple ID
- A provisioning profile that includes **your** device UDID
- A **modified bundle ID** to avoid conflicts with App Store versions

...makes the app installable on your registered device.

## Signing Flow

```
ipa_path (original IPA)
      │
      ├─ 1. Extract ZIP → work_dir/Payload/App.app/
      │
      ├─ 2. Patch Info.plist: CFBundleIdentifier → sideload bundle ID
      │        com.stremio.ios → com.catapult.E7AUFR7897.com-stremio-ios
      │
      ├─ 3. Embed provisioning profile
      │        work_dir/Payload/App.app/embedded.mobileprovision
      │
      ├─ 4. Extract entitlements from profile
      │        Parse CMS blob → find XML plist → extract Entitlements dict
      │        Write to work_dir/entitlements.plist
      │
      ├─ 5. Create P12 keychain package
      │        openssl pkcs12 -export -legacy -passout pass:catapult
      │
      ├─ 6. Set up isolated keychain
      │        security create-keychain -p catapult-tmp /tmp/.../catapult.keychain-db
      │        security import cert.p12 -k {keychain} -P catapult -T /usr/bin/codesign
      │        security set-key-partition-list -S apple-tool:,apple: -s -k catapult-tmp
      │        security list-keychains -d user -s {keychain} {existing...}
      │
      ├─ 7. Sign all components
      │        Frameworks/*.framework → codesign --preserve-metadata=identifier,entitlements,flags
      │        PlugIns/*.appex → codesign --entitlements entitlements.plist
      │        App.app/ → codesign --entitlements entitlements.plist
      │
      ├─ 8. Repack ZIP → work_dir/App_signed.ipa
      │        Paths must be: Payload/App.app/Info.plist (not App.app/Info.plist)
      │
      └─ 9. Cleanup keychain
             Restore original search list
             security delete-keychain {keychain}
```

## Bundle ID Namespace

The sideload bundle ID format prevents conflicts:
```
com.catapult.{TEAM_ID}.{safe_bundle_id}
```

Where `safe_bundle_id` replaces `.` with `-`:
```
com.stremio.ios → com-stremio-ios
→ com.catapult.E7AUFR7897.com-stremio-ios
```

This namespace (`com.catapult.*`) is registered as an app ID with a wildcard on Apple's portal. Each specific bundle ID is then registered individually.

## Entitlements Extraction

The provisioning profile is a CMS (Cryptographic Message Syntax) blob. The embedded plist is located between the first `<?xml` and the last `</plist>` marker:

```python
raw = profile_bytes.decode("latin-1")
start = raw.find("<?xml")
end = raw.find("</plist>") + len("</plist>")
plist_data = plistlib.loads(raw[start:end].encode("latin-1"))
entitlements = plist_data["Entitlements"]
```

Typical entitlements for a free developer profile:
```xml
<dict>
    <key>application-identifier</key>
    <string>E7AUFR7897.com.catapult.E7AUFR7897.com-stremio-ios</string>
    <key>com.apple.developer.team-identifier</key>
    <string>E7AUFR7897</string>
    <key>get-task-allow</key>
    <true/>
    <key>keychain-access-groups</key>
    <array>
        <string>E7AUFR7897.*</string>
    </array>
</dict>
```

`get-task-allow: true` marks the build as a **development** build (debuggable).

## P12 and Keychain Notes

**OpenSSL `-legacy` flag**: OpenSSL 3.x uses new PKCS12 encryption by default. macOS's `security import` can't read it. The `-legacy` flag uses the older RC2/3DES encryption that macOS accepts.

**Non-empty password**: macOS rejects PKCS12 files with empty passwords. Catapult uses `"catapult"` as the P12 password and `"catapult-tmp"` as the keychain password.

**Keychain search list**: `codesign` can only find identities in keychains that are in the active search list. Catapult adds its temp keychain to the front of the list, then restores the original list on cleanup.

## Codesign Command

Main binary and app extensions (with entitlements):
```bash
codesign --force --sign {identity_hash} \
         --keychain {keychain_path} \
         --generate-entitlement-der \
         --entitlements {entitlements.plist} \
         {App.app}
```

Frameworks (preserve original metadata):
```bash
codesign --force --sign {identity_hash} \
         --keychain {keychain_path} \
         --generate-entitlement-der \
         --preserve-metadata=identifier,entitlements,flags \
         {Framework.framework}
```

`--generate-entitlement-der` creates a DER-encoded entitlements blob alongside the XML entitlements for iOS 15+/tvOS 15+ compatibility.

## IPA Structure

Valid IPA structure (paths inside the zip):
```
Payload/
└── App.app/
    ├── Info.plist
    ├── App (binary)
    ├── embedded.mobileprovision
    ├── _CodeSignature/
    │   └── CodeResources
    └── Frameworks/
        └── ...
```

**Critical**: All paths in the zip must start with `Payload/`. If the `Payload/` prefix is missing, `installd` on the device extracts the app but reports "Missing bundle ID" because it looks for `Payload/*.app/Info.plist`.
