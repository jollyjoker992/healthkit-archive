# Continuous integration

Two workflows in `.github/workflows/`:

| Workflow | Runs on | When | What |
|---|---|---|---|
| `ci.yml` (required) | GitHub-hosted Ubuntu | every push to main/master, every pull request, by hand | secret scan, protocol vectors, relay tests and type check, CLI tests on Python 3.11 to 3.14, FHIR terminology and examples, end to end with a real Medplum, workflow and compose checks |
| `macos.yml` | your self-hosted Mac | only by hand (Actions → "macOS (self-hosted)" → Run workflow) | Swift core tests, app builds, translation check, Swift device → relay → processor, and the Simulator test with real HealthKit data |

Make the `ci.yml` jobs required checks in the branch protection rules. `macos.yml` is never required:
it needs your Mac to be on.

When the repository goes public, also enable Settings → Code security → **Private vulnerability
reporting**: [SECURITY.md](../SECURITY.md) sends reporters to Security → Report a vulnerability, which
only appears once it is on.

## Jobs in `ci.yml`

| Job | Checks |
|---|---|
| Secret scan | gitleaks over the full history, with `.gitleaks.toml` (default rules; the published test keys in `spec/vectors/` and one false positive in `Keys.swift` are allowed). |
| Protocol test vectors | `spec/tools/gen_vectors.py --check`: the vectors regenerate identically. |
| Relay | `vitest` (spec vectors and relay behaviour) and `tsc --noEmit` in `worker/`. |
| CLIs | `python -m unittest discover -s cli/tests` on 3.11, 3.12, 3.13 and 3.14, with Medplum's validator installed so the mapping is validated too. Includes the checks that the FHIR terminology, examples and `docs/fhir-mapping.md` match the mapper. |
| FHIR | `tools/fhir_terminology.py --check`, `tools/fhir_examples.py --check`, and every terminology and example file through the validator. |
| End to end | Starts the local Medplum stack (`medplum/`, throwaway passwords) and its test project, then `cli/tests/test_e2e_local.py`: invite, enrol, uploads (out of order, repeated), pull and verify, build, FHIR map, validate, load, read back, a deletion travelling to Medplum, retiring a device. The Swift device part is skipped on Linux. |
| Checks | actionlint (with shellcheck) on the workflows, JSON files parse, the Medplum compose file resolves. |

No job needs a secret. Third-party actions are pinned to commit SHAs.

## The self-hosted Mac runner

1. In the repository: Settings → Actions → Runners → New self-hosted runner → macOS, ARM64. Follow the
   download and `./config.sh` steps; keep the default labels (`self-hosted`, `macOS`, `ARM64`).
2. Run it as your user: `./run.sh` in Terminal, or `./svc.sh install` (a LaunchAgent). The Simulator
   belongs to the user the runner runs as.
3. The Mac needs Xcode with an iOS Simulator runtime, **with its first-launch components installed**:
   `sudo xcodebuild -runFirstLaunch` once, and again after every Xcode or macOS update
   (`xcodebuild -checkFirstLaunchStatus` exits 0 when they are current). Without them Xcode cannot start
   the UI test runner: the run ends with "failed to bless service hub" and "The test runner hung before
   establishing connection". The workflow checks this first.
4. Python 3.11 or later as `python3`, and Docker only if you want the FHIR steps. Node is installed by
   the workflow.
5. Optional FHIR steps: create the Medplum test project once on that Mac
   (`cd medplum && python3 setup.py init && docker compose up -d && python3 setup.py project --test`),
   then set the repository variable `HKA_E2E_FHIR_ENV` (Settings → Secrets and variables → Actions →
   Variables) to the full path of the `.env.test` it wrote. Without it, the FHIR steps are skipped.

The Simulator test installs the app fresh, writes a known day of data into that Simulator's Health store
(the app's own samples only; earlier seed data is removed first) and runs for about ten minutes.

## Running the same checks locally

```sh
.venv/bin/python spec/tools/gen_vectors.py --check
(cd worker && npx vitest run && npx tsc --noEmit)
.venv/bin/python -m unittest discover -s cli/tests
.venv/bin/python tools/fhir_terminology.py --check && .venv/bin/python tools/fhir_examples.py --check
(cd ios/HKACore && swift test)

# end to end; the FHIR part needs the Medplum test project (see medplum/README.md)
HKA_E2E=1 HKA_NODE_BIN="$(dirname "$(command -v node)")" \
  .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_local.py

# Simulator with real HealthKit data (needs `xcodebuild -checkFirstLaunchStatus` to exit 0)
HKA_E2E_SIM=1 HKA_NODE_BIN="$(dirname "$(command -v node)")" \
  .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_simulator.py
```
