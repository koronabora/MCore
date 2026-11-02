# MApp update distribution

Authenticated endpoints on the existing pinned HTTPS API:

- `GET /v1/app-update`: current manifest, or `version_code: 0` before publication.
- `GET /v1/app-update/{sha256}.apk`: immutable APK file.

Files live in `/var/lib/mcore/app-updates`. Publish signed APKs with
`tools/publish-mapp.py APK --notes TEXT` from the workspace tooling (deployed
copy `/home/kot/.local/publish-mapp.py`). The publisher checks the existing
MApp signing certificate, package ID and increasing versionCode. It installs
the APK first, then atomically replaces `latest.json`; no service restart is needed.
Keep older content-addressed APKs for clients which already read a manifest.

Deployment approved by the user; backup:
`/var/backups/mcore/20261102-160028-update-app-updates`.
101 backend tests passed, including authorization, missing artifacts,
manifest validation and immutable APK delivery. Live 0.5 manifest and full
APK were fetched with temporary authentication; size and SHA-256 matched.
Temporary session was removed.
