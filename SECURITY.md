# Security

This project handles personal health data and the keys that protect it. Please report a vulnerability
privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a public
issue for it.

Useful to include: the component (app, relay, processor, FHIR loader), what an attacker needs (a stolen
phone, control of the relay, a processor's key file, network position) and what they gain.

## Threat model in short

The full model is in [docs/design.md](docs/design.md) (section 4.4) and [spec/README.md](spec/README.md).

- The relay (Cloudflare) is not trusted with data: it stores only `age` ciphertext and signed
  manifests. It can withhold or delete batches; processors detect gaps, rollbacks and silence.
- Devices sign with a non-exportable Secure Enclave key. Processors pin each device key after the owner
  compares fingerprints, and verify every batch, grant and chain link against it.
- Processors and recovery identities hold the decryption keys. Whoever has a processor's `keys/` folder
  can read every batch encrypted to it.
- The admin key can invite, retire and purge, but cannot read data or add a recipient to a phone.

## Design rules the code follows

- Secrets live only in key files (`data/keys/`, `~/.config/hka-admin/`, mode 600) and in `.env`,
  `.env.test` and `medplum/.env`, all ignored by git. No secret is printed or logged.
- CI scans the full git history with gitleaks on every push and pull request (`.gitleaks.toml`; the only
  exceptions are the published, made-up keys in `spec/vectors/`).
- Tests use made-up data and a separate Medplum test project, never real data or the real project.
- The local Medplum stack listens on 127.0.0.1 only; agents get a read-only client.
- The cloud backup (`hka backup`) uploads only files age-encrypted to the processor's key and
  `HKA_BACKUP_RECIPIENTS`; cloud credentials go to rclone in its environment, never on a command line.

## If a key or secret leaks

| What | Do |
|---|---|
| A processor's `keys/` | Make a new processor identity (`hka init` in a new data folder). On every phone that lists the old one, scan the new card first, then open the old processor and tap Remove Processor (the last processor cannot be removed). Batches the old key could read stay readable to whoever has it. |
| A phone (lost or stolen) | `hka-admin retire d_…`; the phone holds no decryption key. Enrol the new phone with a new invite. |
| The admin key | Move the old key aside (`~/.config/hka-admin/admin.pem`, or the file in `$HKA_ADMIN_DIR`; `hka-admin init` reuses an existing key), run `hka-admin init` for a new one, put its public key in the Worker's `ADMIN_KEYS` and redeploy. Then check `hka-admin users` for devices you did not invite and retire them. |
| A cloud backup credential (`HKA_AWS_*`, `HKA_R2_*`, `HKA_GDRIVE_*`) | Revoke it at the provider (IAM access key, R2 API token, Google account → Third-party access) and put a new one in `.env`. The files there are ciphertext; whoever had the key could still delete them, so check the target with `hka backup` (it uploads anything missing). |
| A Medplum client secret | Delete the client in Medplum and run `python3 medplum/setup.py project` again. |

Removing a secret from git history is not enough on its own: replace it at its source first.
