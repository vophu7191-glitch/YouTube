# Jion operations: backup and recovery

This repository does not contain database credentials or a backup destination. Do not commit MongoDB connection strings, database dumps, or `.env` files.

## MongoDB backup

1. In MongoDB Atlas, check which backup options are available for the cluster tier.
2. If automated Cloud Backup is available, enable a schedule and retention period that match the project's needs.
3. If scheduled backups are unavailable, run `mongodump` from a trusted machine with a database user that has read access:
   ```sh
   mongodump --uri "$MONGODB_URI" --archive="jion-$(date -u +%Y%m%dT%H%M%SZ).archive.gz" --gzip
   ```
4. Store the archive in private encrypted storage, separate from this GitHub repository and the Render service filesystem.
5. Restrict access to the backup and keep the MongoDB URI out of shell history where possible.

## Restore drill

Restore only into a separate test database first:

```sh
mongorestore --uri "$MONGODB_RESTORE_URI" --archive="jion-backup.archive.gz" --gzip --nsFrom="superchat.*" --nsTo="superchat_restore_test.*"
```

Verify guild settings, YouTube channel mappings, donation counts, timestamps, and known donation IDs before considering a production restore. Never use `--drop` against the live database as a routine recovery step.

## Runtime checks

- Use `/bot-info` to inspect Discord connectivity, MongoDB ping, active watchers, monitor status, and estimated YouTube API quota.
- Check Render logs when the monitor or Replay reader reports errors.
- HTTP 403/429 from YouTube is an access/rate limit, not proof that a replay is disabled. Do not retry aggressively or attempt to bypass the limit.
- After a code deploy, confirm the service becomes healthy and run `/bot-info` in Discord.

## Automated checks

GitHub Actions compiles Python modules and runs offline unit tests in `tests/`. These checks do not prove live Discord, MongoDB, or YouTube integration works; those still need a controlled runtime check.
