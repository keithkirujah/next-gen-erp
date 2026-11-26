# NextGen ERP hardening and data portability notes

## Included in this build

- Nested operations transactions use SQLite savepoints, so nested transactional calls can roll back without breaking the outer transaction.
- Decimal input rejects excess precision instead of silently rounding values such as `0.004` to zero.
- A sale return that requests restocking while stock tracking is disabled fails clearly instead of silently losing inventory traceability.
- Partial returns allocate the stored sale-line total proportionally; final returns reconcile to the exact remaining cents.
- Unauthenticated first-run admin setup is available only when the user table is empty. Existing installations must authenticate as Admin before changing administrator identity.
- Login dialog throttles repeated failed attempts for 30 seconds after five failures in that dialog session.
- Cloud sync has been replaced by **Real-time Backups**. The change queue now counts work not yet inside a verified backup; rows are removed only after the backup ZIP was written, re-opened and integrity-checked.
- Theme toggling no longer crashes non-admin users due to a protected setting write; non-admin theme choice is session-only.
- Database export/import controls explicitly require the built-in Admin role.
- Database backup export includes the system database and all workspace databases, checks SQLite integrity, and writes a manifest. Restore validates the archive and workspace identities and creates a safety backup before replacement.
- The website catalogue remains read-only: importing/restoring ERP data does not send writes to the website.

## Export / import behavior

Use **Database & Backup → Export full database package** to create a ZIP containing the installation's system database and all business workspaces. Store it somewhere separate from the computer, and protect it because it contains business records and user credential hashes.

Use **Import / restore database package** to replace the local database set from a previously exported package. It creates a safety backup first. **This is a full restore, not a merge of two databases.** Never import an untrusted archive. Export before importing. Opening databases with a newer supported application version runs the app's schema initialization/migration code; this is not a universal plugin system and cannot safely apply arbitrary SQL from third parties.

## Verification status and known limitations

- All Python source files passed `py_compile` in the build environment.
- Focused decimal validation tests passed.
- GUI runtime tests were not run because PySide6 is not installed in the build environment.
- The full 105-item source audit has not been completely remediated or independently reproduced. In particular, invoice reconciliation for returned/cancelled sales, all workspace-migration crash windows, full payment/tender audit fields, schema migration completeness, and all domain workflows still require dedicated tests and fixes.
- Backups are verified (ZIP test + manifest + SQLite quick_check) before they replace the previous copy; the previous good copy is kept. The optional GitHub token is encrypted with Windows DPAPI (current Windows user only). The backup ZIP itself is NOT yet password-encrypted: keep the backup folder/repository private.
- Dependency hashes/lockfile and a Windows end-to-end installer test are not included in this build.

Do not describe this build as completely vulnerability-free or production-certified. Test on a copy of real data before deployment.
