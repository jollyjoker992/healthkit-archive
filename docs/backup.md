# Cloud backup of the day folders

`hka backup` copies each user's final days to AWS S3, Cloudflare R2, Google Drive or any rclone
remote. A target is turned on by putting its credentials in the processor's `.env` (or the
environment): every target whose credentials are set gets a copy, and `hka run` backs up after each
build. It follows garmin-archive's backup (manifest, one archive per final day, upload state per
target), with one difference: **everything is encrypted before it leaves the computer**.

## What is uploaded

```
<target root>/users/<user_id>/
  archives/YYYY/YYYY-MM-DD.<key>.tar.gz.age   users/<u>/archives/… of each final day
  extras/profile.ndjson.<key>.age             and extras/undated/…, extras/other/…: files outside the days
  manifest.json.age                           the user's manifest.json, plus the newest version of
                                              each day and extra file that this target holds
```

- Every file is [age](https://age-encryption.org)-encrypted to the processor's own key
  (`keys/age.txt`) and to each key in `HKA_BACKUP_RECIPIENTS`. The provider sees only ciphertext and
  file sizes, and the folder and day names (the user ids and dates).
- Only **final** days are uploaded: days no batch has changed for `HKA_SETTLE_DAYS` days (default 7,
  allowed 3 to 365). `hka build` finalizes and archives them; a provisional day is never uploaded.
  Recent days are still in the relay.

  ```sh
  HKA_SETTLE_DAYS=14   # in .env: wait longer, e.g. for a phone that syncs Health late
  ```

  Below 3 days is refused: the app re-sends the last 48 hours every day, which would reopen,
  re-archive and re-upload days again and again. A longer period only delays the upload.
- `<key>` changes with the file's content and with the recipients, and binds the user and the day. It is an HMAC keyed with a secret
  derived from `keys/age.txt`, so the provider cannot test a guess at a file against its name.
  A file on a target is never overwritten: a reopened day that becomes final again, a changed extra file or a new recipient
  gets a new file, and the old one stays. `manifest.json.age` is the only file that changes. It is
  written last and names only files the target holds, so a run that fails part way leaves the
  previous, complete backup. A reopened day keeps its last final archive in it until the new one is
  uploaded.
- If someone else changes the target, the next run repairs what it can see: a `manifest.json.age`
  that is not the one this processor uploaded is uploaded again, and a file under one of this
  processor's names that it cannot read is written again with the right content. A writer to the
  bucket can still damage files between runs; restore checks every file's sha256 and says which
  days it could not restore, so keep more than one target.
- Nothing is ever deleted from a target. Old versions and days whose records were all deleted in
  Health stay there; restore follows `manifest.json.age` and ignores them.
- Each run lists the target first: a file deleted there is uploaded again. `data/backup/state.json`
  only caches what each target holds; if it is lost or damaged (it is then moved aside), the next run
  rebuilds it from the listing and the target's previous `manifest.json.age` without uploading again.
- With `HKA_AWS_STORAGE_CLASS`, only the day archives go to that class; `manifest.json.age` and the
  extras stay in STANDARD so restore can read them at once.
- `inbox/` (the relay's ciphertext) is not part of this backup; it can be rebuilt from the relay
  while the relay keeps it.

Add the offline recovery identity as a recipient, so the backup survives the loss of the processor.
After a change of recipients the next run uploads everything again for the new set, including the
last final archive of a reopened day (re-encrypted; in Glacier/Deep Archive that one cannot be read
back and stays readable with the old recipients only):

```sh
HKA_BACKUP_RECIPIENTS=age1…   # the "public key" line of the recovery folder's age.txt; several: comma-separated
```

## Install rclone

Uploads go through [rclone](https://rclone.org/install/) (`brew install rclone`, `apt install
rclone`); set `HKA_RCLONE_BIN` if it is not on `PATH`. You do not need `rclone config`: hka defines
the vendor remotes from the variables below (in rclone's environment, never on a command line) and
keeps a private `data/backup/rclone.conf`, so your own rclone configuration is not touched.

## AWS S3

1. Create a bucket (S3 console, Block all public access on, default encryption on). Bucket names
   are global: if creation fails with `BucketAlreadyExists`, pick another name.
2. Create an IAM user with an access key and only this policy (replace `BUCKET`):

   ```json
   {"Version": "2012-10-17", "Statement": [
     {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::BUCKET"},
     {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::BUCKET/*"}]}
   ```

3. In `.env`:

   ```sh
   HKA_AWS_ACCESS_KEY_ID=AKIA…
   HKA_AWS_SECRET_ACCESS_KEY=…
   HKA_AWS_BUCKET=my-health-backup          # or bucket/prefix
   HKA_AWS_REGION=ap-southeast-1            # the bucket's region (default us-east-1)
   #HKA_AWS_STORAGE_CLASS=DEEP_ARCHIVE      # archives only; the manifest stays STANDARD
   ```

With `GLACIER` or `DEEP_ARCHIVE` storage is very cheap, but a restore needs an S3 restore request
first (hours), and an archive replaced within 90/180 days is still charged for the rest of that time.
`STANDARD_IA` or the default `STANDARD` restore immediately.

## Cloudflare R2

Use a bucket of its own, not the relay's.

1. R2 → Create bucket (e.g. `hka-backup`).
2. R2 → Manage API tokens → Create API token: permission **Object Read & Write**, applied to that
   bucket only. Copy the Access Key ID and Secret Access Key (shown once).
3. In `.env` (the account id is in the R2 overview, 32 hex characters):

   ```sh
   HKA_R2_ACCOUNT_ID=0123456789abcdef0123456789abcdef
   HKA_R2_ACCESS_KEY_ID=…
   HKA_R2_SECRET_ACCESS_KEY=…
   HKA_R2_BUCKET=hka-backup                 # or bucket/prefix
   ```

## Google Drive

rclone's shared Google client id is being retired during 2026, so Drive needs your own sign-in app
(an OAuth client). It is free and takes about 10 minutes, once. Google renames console menus from
time to time; rclone keeps the current names in <https://rclone.org/drive/#making-your-own-client-id>.

**A. Create the sign-in app** (any Google account; it does not have to be the one that stores the backup)

1. Open <https://console.cloud.google.com/>, then the project picker at the top → **New project**
   (e.g. `hka-backup`) → Create, and select it.
2. **APIs & Services → Enable APIs and services**, search **Google Drive API**, open it, **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform) → **Get started**:
   app name `hka-backup`, your email as user support email → Next → Audience **External** → your
   email as contact → agree → **Create**. (A Google Workspace account can pick **Internal** and skip
   step 6, if the backup's Drive is in the same Workspace.)
4. **Data Access → Add or remove scopes**, tick `https://www.googleapis.com/auth/drive` (or paste it in
   "Manually add scopes" → Add to table) → Update → **Save**.
5. **Audience → Add users**: add the Google account that will hold the backup as a test user → Save.
6. **Audience → Publish app** → Confirm. Without this the app stays in "Testing" and Google expires
   its tokens after 7 days, so backups would stop every week. If the button is greyed out, fill in an
   application home page and privacy policy link under **Branding** (any page you own, e.g. a GitHub
   Pages page), add its domain to Authorized domains, Save, and try again. You do not need Google's
   verification for personal use (under 100 users).
7. **Clients → Create client** (or Overview → Create OAuth client): application type **Desktop app**,
   default name → **Create**. Copy the **Client ID** and **Client secret** (download the JSON to keep
   them; treat the secret like a password).

**B. Get a token** (on a computer with a browser and rclone)

```sh
rclone authorize "drive" "CLIENT_ID" "CLIENT_SECRET"
```

A browser opens. Sign in with the account that should hold the backup. Google shows **"Google hasn't
verified this app"**: it is your own app, so click **Advanced → Go to hka-backup (unsafe)**, allow
access to Drive, and return to the terminal. rclone prints a JSON token between `--->` and `<---`.
On a headless server run this on your laptop and copy only the JSON.

**C. Put it in the processor's `.env`** (the token on one line, exactly as printed)

```sh
HKA_GDRIVE_CLIENT_ID=….apps.googleusercontent.com
HKA_GDRIVE_CLIENT_SECRET=…
HKA_GDRIVE_TOKEN={"access_token":"…","token_type":"Bearer","refresh_token":"…","expiry":"…"}
HKA_GDRIVE_FOLDER=healthkit-archive      # folder in My Drive, created on first upload
```

Then `hka backup --target gdrive`. rclone renews the access token on each run with the refresh token
and keeps the renewed one in `data/backup/rclone.conf`. If Google revokes the token (password change,
access removed under Google Account → Security → Third-party access, app back in Testing), repeat B
and replace `HKA_GDRIVE_TOKEN`.

## Any other rclone remote

```sh
HKA_BACKUP_RCLONE_REMOTE=nas:backups/hka    # a remote from your own rclone config (or a local path)
```

## Run and check

```sh
hka backup                 # every configured target; --target aws (repeatable) for one
hka run                    # pull, build, backup
hka status                 # per user and target: "backup r2: 412/412 final days, up to date, checked …"
```

The first run uploads the whole history (one file per final day; a few MB per year per person).
A target that cannot be reached is reported (exit 1) and retried on the next run; the others go on.
A backup setting that is wrong (a target with only some of its variables set, a bad
`HKA_BACKUP_RECIPIENTS`, no rclone) makes `hka backup` exit 2 before it starts; `hka run` reports
it, still pulls and builds, skips the backup and exits 2.

## Restore

```sh
hka restore --from r2 --out ~/restored                              # every user, with keys/age.txt
hka restore --from aws --user u_… --out ~/restored --identity /Volumes/USB/hka-recovery/age.txt
```

It downloads the user folders, decrypts them, checks every file against the sha256 in the
target's `manifest.json.age`, and writes `<out>/<user_id>/` with `days/YYYY/YYYY-MM-DD/…`,
`archives/YYYY/YYYY-MM-DD.tar.gz`, the extra files (`profile.ndjson`, `undated/`, `other/`) and
`restored.json` (what was restored, and the user's manifest as it was at the last backup). `--out`
must be a new or empty folder. Only the files `manifest.json.age` names are downloaded, never older
versions. A day is written whole or not at all. Restore takes no lock, so scheduled `hka run`s go on
meanwhile. Days with no final archive on the target are counted in the output.
age proves only that a file is readable by your key, not who wrote it. Restoring with the
processor's own key, restore checks that every file name in `manifest.json.age` was made by this
processor for that user and day (an HMAC under a secret derived from `keys/age.txt`), so files
planted by someone with write access to the bucket are refused. With the recovery identity that
check is not possible and restore says so; it still checks every file's sha256 and pattern and keeps
archive contents inside their day. The restored folder holds decrypted health data.

Without hka, with stock tools only:

```sh
rclone copy r2remote:hka-backup/users/u_… ./u_…
age -d -i keys/age.txt u_…/manifest.json.age | less          # "archives" names each day's file
age -d -i keys/age.txt u_…/archives/2026/2026-11-10.<key>.tar.gz.age | tar xz
```

## Known limits

- Someone who can write to the bucket can damage or replace files between runs. The next run repairs
  what it can see, and restore checks every file and names the days it could not restore, but
  nothing can stop the damage itself: keep more than one target.
- If `data/backup/state.json` is lost, the order of older versions of a reopened day is rebuilt from
  the target's previous `manifest.json.age` and the provider's modification times. Two rare cases
  can then name an older final version of a day that is reopened at that moment: a day whose content
  had gone back to an earlier version, and clocks of this computer and the provider that disagree.
  The day's current content is uploaded as soon as it is final again.
- A file under one of this processor's names that was found unreadable on the target is written
  again. If that upload fails part way in the same run, it can be recorded as uploaded; restore
  reports the day as not restorable, and a second target still holds it.
- Files in S3 Glacier or Deep Archive that the local state does not know cannot be checked without
  an S3 restore request. The run output and `hka status` count them; `manifest.json.age` names only
  what could be checked.
- Restoring with the recovery identity cannot verify who wrote the file names (that needs the
  processor's key); it still checks every file's sha256.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `aws backup: set HKA_AWS_BUCKET …` | Only some of the target's variables are set. Set the rest or remove them. |
| `rclone ('rclone') not found` | Install rclone or set `HKA_RCLONE_BIN`. |
| `cannot read the target: … AccessDenied` | The key lacks `s3:ListBucket`/`GetObject`/`PutObject` on that bucket (AWS), or the R2 token is not for that bucket. |
| `cannot read the target: … invalid_grant` | The Google token was revoked or expired (an app in "Testing"). Get a new one (Google Drive, step 2). |
| `… rateLimitExceeded` | Google throttling; the next run continues where this one stopped. |
| `N file(s) not uploaded; retried next run` | The upload did not complete. The target keeps its previous complete backup; the rest goes up next run. |
| `archive missing locally and not yet on this target` | A final day's archive was deleted from `users/<u>/archives/` before it was uploaded. Only for targets that lack it; the day gets a new archive if a late batch reopens it. |
