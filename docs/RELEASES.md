# Release governance

EyeOfSauron uses Semantic Versioning. A release is an immutable source state,
not a copy of a developer working tree or the live `/opt` directory.

## Release artifacts

A successful CI run produces:

- a deterministic source release archive and SHA-256 checksum;
- Python wheel and source distribution;
- backend test coverage XML;
- a CycloneDX JSON SBOM for the installed Python artifact;
- a CycloneDX JSON SBOM for production frontend dependencies.

`RELEASE.json` inside the server archive records the product, component, version,
Git commit, release ID, database schema, and source timestamp. Credentials and
production data are never release artifacts.

## Release checklist

1. Confirm CI passes on the exact commit and the working tree is clean.
2. Update `CHANGELOG.md` and the version in `pyproject.toml` and
   `src/argus/__init__.py` together.
3. Review dependency updates and both SBOMs; resolve high-severity findings or
   document a time-bounded exception.
4. Build the archive with `scripts/release/package-release.sh` and verify its
   checksum on the target host.
5. Run `install-release.sh --prepare-only` before the maintenance window. The
   activation command accepts the same verified archive and reuses the prepared
   directory only when its content is identical.
6. Confirm disk space, current service health, database integrity, and the most
   recent verified backup.
7. Activate the prepared release. The installer stops both SQLite writers,
   creates another verified backup, switches the `current` symlink, installs
   matching units, and runs the readiness, revision, source, and outbox health
   gate.
8. Retain the prior release and pre-release backup until the new version has
   completed at least one normal collection and notification cycle.
9. Create an annotated `vMAJOR.MINOR.PATCH` tag only after the production result
   is accepted.

## Rollback policy

Code rollback is an atomic symlink change. If the target release supports the
live database schema, the live state may be retained. A schema downgrade must
use the pre-release backup associated with that earlier schema. The rollback
script first creates a safety backup and automatically restores the previously
active release if the rollback itself fails its health gate.

Never restore only `state.db` while a writer is running. Stop both `argus` and
`argus-admin`, account for WAL/SHM files, and use the verified backup CLI.

## Initial layout migration

Existing installations that use `/opt/eyeofsauron` as a normal code directory
must be migrated once to `/opt/eyeofsauron/releases/<release-id>` plus the
`/opt/eyeofsauron/current` symlink. This is a manual maintenance operation with
a verified backup. The release installer refuses to replace a normal `current`
path or guess how legacy files should be moved.
Activation also requires an existing valid `current` target: preparing a release
is allowed before migration, but an upgrade must have a recoverable previous
code release before it stops either service.

### Returning to legacy 0.6 installations

A legacy 0.6 directory has no `RELEASE.json` or release tools, so the normal
`rollback-release.sh` cannot preflight it. Automatic failure recovery during the
first 0.7 activation still restores its database and exact saved unit files.
After an accepted 0.7 deployment, a deliberate return to 0.6 is manual:

1. Select the verified pre-0.7 backup bundle and the preserved 0.6 code directory.
   Record both paths before stopping services; retain access to the 0.7 backup
   tool outside the `current` symlink.
2. Stop `argus-admin` and `argus`. Create and verify a safety backup of the
   current database with the retained 0.7 backup tool.
3. Move the stopped current database, any WAL/SHM companions, and the managed
   JSON export into a new private recovery directory. Restore the selected
   pre-0.7 bundle using the 0.7 backup CLI. Restore its managed JSON alongside it:
   unlike 0.7, the legacy runtime uses that JSON as configuration authority.
4. Restore the saved unit files from the pre-release bundle's `units/` directory,
   including removing units absent from that snapshot. Restore the preserved
   code to the paths those exact unit files reference. A legacy unit can still
   reference `/opt/eyeofsauron/src`; changing `current` alone cannot redirect it.
5. Make the restored database and managed JSON owned by `argus:argus`, mode
   `0600`. Reload systemd and start the two services. Confirm active status,
   loopback-only admin access, source collection, delivery and database integrity.
6. Keep the displaced 0.7 state and code until the return has been verified.
   Data created after the pre-0.7 snapshot is preserved in the safety backup but
   is not automatically merged into the older schema.

## Retention

Keep at least the two most recent known-good releases, their pre-release backup
bundles, and the latest daily backup. Longer retention is determined by the data
recovery policy in `docs/OPERATIONS.md`. Remove an old artifact only after its
replacement has been verified and the associated schema no longer needs it.
