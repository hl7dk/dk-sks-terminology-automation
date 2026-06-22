# dk-sks-terminology-automation

Generation of the Danish **SKS** FHIR terminology consumed by the
[DK Core IG](https://github.com/hl7dk/dk-core). The scripts digest the upstream
Danish classifications into FHIR `CodeSystem`s and a `ConceptMap`; the generated
resources are committed under [`fhir/`](fhir/).

This repo deliberately holds the **generation** (and its output) so that DK Core
itself only carries ValueSets and profiles that *reference* these code systems
by canonical URL.

## How DK Core consumes this

The generated resources in `fhir/` are **uploaded to the Nordic terminology
server as a manual step**. DK Core's profiles/ValueSets reference them by their
canonical URLs (`urn:oid:1.2.208.176.2.4`, `http://hl7.org/fhir/sid/icd-10`,
`http://hl7.dk/fhir/core/CodeSystem/icd10-danish-extensions`,
`http://hl7.dk/fhir/core/CodeSystem/icd10-da`,
`http://hl7.org/fhir/sid/icpc-2`) and resolve them from that server at build
time — the resources are **not** committed into DK Core.

### Registering a new code system with the terminology servers

When a **new** code system is added here (a new OID or canonical URL), it must
be registered so the terminology servers know which server hosts it:

- Add the OID / canonical URL to the Nordic tx-server registry:
  [`hl7-nordics-tx-servers.json`](https://github.com/FHIR/ig-registry/blob/master/hl7-nordics-tx-servers.json).
- Verify resolution works using the tx registry resolver:
  <https://tx.fhir.org/tx-reg/resolve>.

Separate / own / **DK-defined** code systems that are *not* hosted on
`tx.fhir.org` should be uploaded to the Nordic terminology server,
<https://tx-nordics.fhir.org/fhir>.

**Supplements must live on the same tx server that hosts the master code
system.** For example, ICD-10 (`http://hl7.org/fhir/sid/icd-10`) is hosted on
`tx.fhir.org`, so the Danish ICD-10 supplement (`CodeSystem-icd10-da.json`)
must be added there too — for `tx.fhir.org` that means contributing it to
[`fhir.tx.support.r4`](https://github.com/FHIR/packages/tree/master/packages/fhir.tx.support.r4).

#### Where each resource lives

```
                          DK Core IG  (references by canonical URL)
                                       │
                  ┌────────────────────┴────────────────────┐
                  │ resolves via                            │
                  ▼                                          ▼
   ┌──────────────────────────────────┐       ┌──────────────────────────────────┐
   │            tx.fhir.org           │       │      tx-nordics.fhir.org/fhir    │
   │     (upstream / international)   │       │   (DK-defined, our own systems)  │
   ├──────────────────────────────────┤       ├──────────────────────────────────┤
   │ ICD-10  (master)                 │       │ CodeSystem-sks.json              │
   │   http://hl7.org/fhir/sid/icd-10 │       │   urn:oid:1.2.208.176.2.4        │
   │                                  │       │                                  │
   │ ── supplement on the SAME server │       │ CodeSystem-sks-icd10-            │
   │ CodeSystem-icd10-da.json         │       │   deviations.json                │
   │   via fhir.tx.support.r4 package │       │                                  │
   │   (FHIR/packages repo)           │       │ ConceptMap-icpc2-icd10.json      │
   └──────────────────────────────────┘       └──────────────────────────────────┘

   Register OID / URL ──▶ FHIR/ig-registry  hl7-nordics-tx-servers.json
   Test resolution    ──▶ https://tx.fhir.org/tx-reg/resolve
```

## Generated resources (`fhir/`)

| File | What it is |
|------|------------|
| `CodeSystem-sks.json` | The SKS classification (non-diagnosis registers), rooted under the SKS *hovedgrupper*. `content: fragment` under `urn:oid:1.2.208.176.2.4`. |
| `CodeSystem-sks-diagnoses.json` | The legacy SKS diagnosis register (the "D-hierarchy", Danish ICD-10 with `D`-prefixed codes). `content: complete` under `urn:oid:1.2.208.176.2.4.12`. Keeps the 3.7.0-style SKS diagnosis codings resolvable alongside the ICD-10-based model. |
| `CodeSystem-icd10-danish-extensions.json` | Danish diagnosis codes that are not plain ICD-10 (extensions + Danish-only blocks). |
| `CodeSystem-icd10-da.json` | A **supplement** adding Danish `da` designations to the international ICD-10 codes Denmark reuses. |
| `ConceptMap-icpc2-icd10.json` | ICPC-2 → ICD-10 mapping (harvested from the public sundhed.dk/dudal tool). |

`version`/`date` on the SKS-derived resources track the **source revision date**
(`SKScomplete.txt` `Last-Modified`), so they change only on a real SKS release.

## Scripts

See [`scripts/README.md`](scripts/README.md) for full detail and
[`scripts/SKS-UPDATES.md`](scripts/SKS-UPDATES.md) for the SKS update cadence.

```bash
# SKS / ICD-10 CodeSystems (no third-party deps; Python 3 stdlib only)
python3 scripts/sks_icd10_diff.py --out-dir sks-icd10-out
cp sks-icd10-out/CodeSystem-sks.json \
   sks-icd10-out/CodeSystem-sks-diagnoses.json \
   sks-icd10-out/CodeSystem-icd10-danish-extensions.json \
   sks-icd10-out/CodeSystem-icd10-da.json fhir/

# ICPC-2 -> ICD-10 ConceptMap (harvests the dudal tool; run deliberately)
python3 scripts/icpc2_icd10_conceptmap.py \
  --codes-file scripts/icpc2-extra-codes.tsv \
  --out fhir/ConceptMap-icpc2-icd10.json
```

## Automation

`.github/workflows/sks-update.yml` runs on the quarterly SKS cadence (and on
demand), regenerates the three SKS/ICD-10 CodeSystems, and opens a PR **only
when the content actually changed**. After merging, upload `fhir/` to the
Nordic terminology server.

The **ICPC-2 ConceptMap is not automated** — it is harvested from a third-party
tool and ICPC-2 is copyright WONCA / DSAM (see the licensing note in
`scripts/README.md`); regenerate it deliberately.
