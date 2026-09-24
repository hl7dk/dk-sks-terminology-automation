# dk-sks-terminology-automation

Generation of Danish **SKS and ICPC-2** FHIR terminology consumed by the
[DK Core IG](https://github.com/hl7dk/dk-core). The scripts digest the upstream
Danish classifications into FHIR `CodeSystem`s and a `ConceptMap`; the generated
resources are committed under [`fhir/`](fhir/).

This repo deliberately holds the **generation** (and its output) so that DK Core
itself only carries ValueSets and profiles that *reference* these code systems
by canonical URL.

## How DK Core consumes this

DK Core references terminology by canonical URL and resolves it through the
terminology servers. Publishing the generated resources is a manual step after
review and merge; the update workflows create PRs in this repository.

The SKS CodeSystems, Danish ICD-10 extensions and ICPC-to-ICD-10 ConceptMap
are published to the [Nordic terminology server](https://tx-nordics.fhir.org/fhir).
The Danish ICD-10 and ICPC-2 supplements are distributed through
[`packages/fhir.tx.support/package/` in FHIR/packages](https://github.com/FHIR/packages/tree/master/packages/fhir.tx.support/package),
alongside their international base systems on `tx.fhir.org`.

### Registering a new code system with the terminology servers

When a **new** code system is added here (a new OID or canonical URL), it must
be registered so the terminology servers know which server hosts it:

- Add the OID / canonical URL to the Nordic tx-server registry:
  [`hl7-nordics-tx-servers.json`](https://github.com/FHIR/ig-registry/blob/master/hl7-nordics-tx-servers.json).
- Verify resolution works using the tx registry resolver:
  <https://tx.fhir.org/tx-reg/resolve>.

**Supplements must be available on the same terminology server as their base
system.** Use the publishing destinations below when reviewing an update.

## Generated resources (`fhir/`)

| File | What it is | Publish through |
|------|------------|-----------------|
| `CodeSystem-sks.json` | The SKS classification (non-diagnosis registers), rooted under the SKS *hovedgrupper*. `content: fragment` under `urn:oid:1.2.208.176.2.4`. | Nordic terminology server |
| `CodeSystem-sks-diagnoses.json` | The legacy SKS diagnosis register (the "D-hierarchy", Danish ICD-10 with `D`-prefixed codes). `content: complete` under `urn:oid:1.2.208.176.2.4.12`. Keeps the 3.7.0-style SKS diagnosis codings resolvable alongside the ICD-10-based model. | Nordic terminology server |
| `CodeSystem-icd10-danish-extensions.json` | Danish diagnosis codes that are not plain ICD-10 (extensions + Danish-only blocks). | Nordic terminology server |
| `CodeSystem-icd10-da.json` | A **supplement** adding Danish `da` designations to the international ICD-10 codes Denmark reuses. | `FHIR/packages`: `packages/fhir.tx.support/package/` |
| `CodeSystem-icpc2E-DK.json` | A **supplement** adding KiAP Danish symptom/diagnosis labels to international ICPC-2. | `FHIR/packages`: `packages/fhir.tx.support/package/` |
| `ConceptMap-icpc2-icd10.json` | ICPC-2 → ICD-10 mapping from KiAP's forward conversion table, with international and Danish-extension targets under their respective systems. | Nordic terminology server |

By default, SKS-derived resources use the `SKScomplete.txt` `Last-Modified`
date for `version` and `date`; without that metadata the generator falls back
to the run date. KiAP-derived resources use the release date in the Access
filename. The update workflows retain the previous snapshot when only ignored
metadata changes, so resources can legitimately retain different source dates.
See [versioning and change detection](scripts/README.md) for the exact rules.

## Scripts

Run commands from the repository root. Use Python 3.10+; ICPC generation also
requires `mdbtools` (`brew install mdbtools` or `sudo apt-get install mdbtools`).

See [`scripts/README.md`](scripts/README.md) for full detail and
[`scripts/SKS-UPDATES.md`](scripts/SKS-UPDATES.md) for the SKS update cadence.

```bash
# SKS / ICD-10 CodeSystems (no third-party deps; Python 3 stdlib only)
python3 scripts/sks_icd10_diff.py --out-dir sks-icd10-out
cp sks-icd10-out/CodeSystem-sks.json \
   sks-icd10-out/CodeSystem-sks-diagnoses.json \
   sks-icd10-out/CodeSystem-icd10-danish-extensions.json \
   sks-icd10-out/CodeSystem-icd10-da.json fhir/

# ICPC supplement and ConceptMap from KiAP (requires mdbtools)
python3 scripts/icpc2_icd10_conceptmap.py \
  --supplement-out fhir/CodeSystem-icpc2E-DK.json --only-if-changed
```

## Automation

`.github/workflows/sks-update.yml` runs on the quarterly SKS cadence (and on
demand), regenerates the four SKS/ICD-10 CodeSystems, and opens a PR **only
when the content actually changed**. After merging, publish each changed
resource through the destination listed above.

The **ICPC-2 Danish supplement and ConceptMap are automated** by
[`.github/workflows/icpc-update.yml`](.github/workflows/icpc-update.yml).
Weekly and on manual dispatch it discovers the latest KiAP Access database,
generates both resources from its forward `ICPCKON2` table, and opens or
updates one PR for substantive changes. KiAP 4.4 (2026-06-29) supplies 686
Danish designations and 2,476 forward mapping pairs. WONCA/DSAM licensing
terms remain attached.

Run the offline validation tests with:

```bash
python3 -m unittest discover -s tests -v
```

See [ICPC generation](scripts/README.md#icpc2_icd10_conceptmappy) and
[workflow setup](scripts/README.md#scheduled-pull-requests)
for the reviewed source conflicts, source-date versioning, offline runs, and
the GitHub setting that allows Actions to create PRs. Merge the workflow into
the default branch to enable its schedule. After merging an update PR, use the
publishing destinations above. The workflows do not deploy resources or open
cross-repository PRs.

### Planned: PRs to hl7dk/packages

Extend this repository's automation to open or update a pull request in
[`hl7dk/packages`](https://github.com/hl7dk/packages) with updated Danish ICD-10
and ICPC-2 supplements under `packages/fhir.tx.support/package/`. This should
follow review and merge of the generated updates here, and create a PR only
when the destination content differs. Cross-repository PR creation is planned;
the current workflows create PRs only in this repository.
