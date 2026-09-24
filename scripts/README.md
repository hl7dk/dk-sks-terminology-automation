# scripts

Run all command examples from the repository root with Python 3.10+.

> **SKS is updated quarterly.** See [`SKS-UPDATES.md`](SKS-UPDATES.md) for the
> upstream update cadence (announced ~the 17th of the month before each quarter
> change; live on the 1st of Jan/Apr/Jul/Oct) and the historical archive. The
> regeneration is automated by
> [`.github/workflows/sks-update.yml`](../.github/workflows/sks-update.yml),
> which runs on that schedule and opens a PR only when the SKS content actually
> changes.

## `sks_icd10_diff.py`

Maps out **where the Danish SKS additions sit relative to plain ICD-10**.

It downloads:

- the Danish SKS complete file `SKScomplete.txt` from
  <https://filer.sundhedsdata.dk/sks/data/skscomplete/> (see the
  [SKS download page](https://sundhedsdatastyrelsen.dk/indberetning/klassifikationer/sks-klassifikationer/download-sks)),
- the international ICD-10 `CodeSystem`
  (`http://hl7.org/fhir/sid/icd-10`) from the FHIR terminology server
  `tx.fhir.org/r4`,

and diffs the SKS diagnosis register (`dia`) against ICD-10.

### How the mapping works

SKS diagnosis codes use a leading `D` (*Diagnose*). Some correspond directly
to international ICD-10, e.g. `DA022` → `A02.2`; others are Danish extensions
or Danish-only categories. After stripping the `D`, the generator compares
candidate codes against the international CodeSystem and assigns one of three
classifications:

| status              | meaning                                                        | example                |
|---------------------|----------------------------------------------------------------|------------------------|
| `icd10_standard`    | plain ICD-10 code Denmark reuses                               | `DA000` → `A00.0`      |
| `danish_extension`  | extra granularity **under a real ICD-10 category**             | `DA022A` → `A02.2A`    |
| `danish_only_block` | the 3-char category does **not** exist in ICD-10 at all        | `DUB`, `DVRA01` (U/V…) |

### Usage

```bash
python3 scripts/sks_icd10_diff.py            # uses ./.sks-cache and ./sks-icd10-out
python3 scripts/sks_icd10_diff.py --force-download
python3 scripts/sks_icd10_diff.py --help
```

No third-party dependencies — Python 3 standard library only. The two source
files (~23 MB SKS, ~4.6 MB ICD-10) are cached under `.sks-cache/` and reused on
subsequent runs.

### Output (`sks-icd10-out/`)

- `sks-icd10-mapping.csv` — every SKS diagnosis code with its `danish_code`
  (ICD-10-style form), status, exact `icd10_code` (when reused), ICD-10
  category, chapter, `active`, `valid_from` / `valid_to` and the Danish
  description.
- `danish-addons.csv` — the additions only (`danish_extension` +
  `danish_only_block`).
- `summary.json` — totals plus the additions grouped by ICD-10 chapter and the
  top categories receiving Danish additions (i.e. *where* the additions are).
- `CodeSystem-icd10-danish-extensions.json` — a standalone **FHIR CodeSystem**
  enumerating every Danish deviation/extension (14k+ concepts) as its own
  codes. Default `content: complete` (these are genuinely new codes absent from
  ICD-10, so they form their own code system, not an ICD-10 supplement). Each
  concept carries properties: `deviationType` (`extension`/`deviation`),
  `sksCode`, `baseCategory` (the related ICD-10 category), `icd10Chapter`,
  `status` (`active`/`retired`), and `validFrom` / `validTo` (`dateTime`,
  spanning the code's full SKS history; an **absent** `validTo` means
  open-ended — the SKScomplete `25000101` sentinel is dropped on emit since
  `status` already conveys active/retired).

  Hierarchy is modelled exactly as ICD-10 models it — `hierarchyMeaning:
  is-a` on a flat concept list with `parent`/`child` code properties (ICD-10
  does **not** nest `concept.concept`). The Danish hierarchy is positional:
  each extra character is one level deeper (`A02` → `A02.2` → `A02.2A` →
  `A02.2A0`).
  - In the standalone default, `parent` only links to a shallower **Danish**
    code, because a CodeSystem's parents must exist in the same system
    (~1.2k internal links). Codes whose real parent is a plain ICD-10 code are
    roots; their `baseCategory` records the ICD-10 anchor.
  - In `--content supplement`, ICD-10 is the base system, so `parent` reaches
    plain ICD-10 codes too, giving a fully integrated tree.

  Customise with `--supplement-canonical` (the canonical url),
  `--supplement-version` and `--content {complete,fragment,supplement}`.
  `--content supplement` instead links to ICD-10 via `supplements:
  http://hl7.org/fhir/sid/icd-10|<version>` and adds an is-a `parent` to each
  extension — but note a terminology server may reject a supplement that
  introduces codes not present in the base system, which is why `complete` is
  the default.
- `CodeSystem-sks.json` — a **FHIR CodeSystem** for the rest of the Danish SKS
  classification: every SKScomplete register **except** diagnoses (`dia`, which
  are covered by ICD-10 + the deviations CodeSystem) and ATC (`atc`, WHO
  international). ~38k concepts spanning the SKScomplete registers `opr`, `pro`,
  `til`, `uly`, `adm`, `res`, `und` and `spc`. `content: fragment` under the SKS
  root OID `urn:oid:1.2.208.176.2.4`, so the existing `$SKS` profile slices
  resolve to it with no profile edits. Each concept carries:
  - `register` (multi-valued — codes shared across registers, e.g. the `KZ…`
    codes in `opr`+`til`, are merged) — the SKScomplete register.
  - `mainGroup` — the SKS *hovedgruppe* (the code's leading letter), an
    **official top-level classification axis** that cross-cuts the register.
    Its meaning per the [hovedgrupper page](https://sundhedsdatastyrelsen.dk/indberetning/klassifikationer/sks-klassifikationer/hovedgrupper):
    `A` administrative, `B` treatment/care (*Behandlings- og Plejeklassifikation*),
    `E` external causes, `F` functioning (ICF), `K` surgical operations (NCSP),
    `N` anaesthesia/intensive/pre-hospital, `R` result reporting,
    `U` examinations, `W` clinical physiology/nuclear medicine,
    `Z` supplementary codes and miscellaneous procedures (`D` diagnoses and
    `M` ATC live in the other two CodeSystems). The same register can hold
    several hovedgrupper — e.g. `pro` spans `B`/`F`/`N`/`U`/`W`/`Z` and `til`
    spans nine — so `mainGroup` is the cleaner semantic classifier. The `ZZ…`
    measurement codes used in `DkCoreObservation` are hovedgruppe `Z`
    (supplementary/diverse procedures), filed in the `pro` register. A few
    leading letters (`T`/`V`/`Y`) are not official hovedgrupper and carry no
    `mainGroup`.
  - `status`, `validFrom` / `validTo`, and `parent`/`child` is-a links derived
    positionally (the SKS prefix hierarchy, e.g. `K` → `KA` → `KAA` → `KAAA`
    → `KAAA00`).

  The tree is **rooted under the hovedgrupper**: every branch terminates at a
  single-letter main-group concept. For `A`/`B`/`F`/`K`/`N`/`R`/`U`/`W` that
  letter is a real SKS code (with its own Danish display) and already sits at
  the top of its branch; for `D`/`E`/`Z` — which SKS publishes only as deeper
  codes — the generator adds a **synthetic** single-letter root concept (display
  = the official hovedgruppe name, no `register`/validity, `mainGroup` = itself)
  and reparents that branch's former roots under it. So there are 11 hovedgruppe
  roots (`A B D E F K N R U W Z`); `M` (ATC) is out of scope, and the three
  non-hovedgruppe letters `T`/`V`/`Y` remain as their own small roots. This adds
  3 concepts to the count (the synthetic `D`/`E`/`Z`).

  Customise with `--sks-canonical`, `--sks-version`, and
  `--sks-exclude-registers` (default `dia,atc`).

  **Versioning:** by default the CodeSystems stamp `version` *and* `date` with
  the SKS source revision date — the `Last-Modified` of `SKScomplete.txt`
  (`YYYY-MM-DD`, e.g. `2026-03-16` for the Q2-2026 release), cached in a
  `.lastmod` sidecar. SKScomplete has no internal version, so the date
  identifies the source revision. If the header/sidecar is unavailable, the
  generator falls back to the run date. Active/retired status is evaluated at
  run time, and the international ICD-10 input also affects the output; a
  stable source date alone does not guarantee byte-identical regeneration.
  `--sks-version`, `--supplement-version`, `--sks-diagnoses-version` and
  `--icd10-da-version` override the respective resource versions. When present,
  the source revision still supplies `date`.

  The SKS workflow ignores `version`, `date` and `meta` when comparing snapshots;
  other resource metadata and concept changes count. An unchanged resource
  keeps its previously committed version/date, even if another resource is
  updated from a newer SKS release.
- `CodeSystem-sks-diagnoses.json` — a **FHIR CodeSystem** for the legacy SKS
  diagnosis register (the "D-hierarchy"), including international ICD-10-derived
  codes and Danish additions, with their D-prefixed codes and Danish displays
  (e.g. `DA022` corresponds to ICD-10 `A02.2`). `content: complete` under the SKS
  diagnosis sub-OID `urn:oid:1.2.208.176.2.4.12`, so the 3.7.0-style legacy
  bindings in DK Core (`Condition.code.coding[SKS-D]`,
  `ServiceRequest.reasonCode.coding[SKS]`) resolve to it with no profile edits.
  The ICD-10-based model (plain ICD-10 + `icd10-danish-extensions` + the `icd10-da`
  supplement) is the modern, non-breaking alternative offered alongside it. Each
  concept carries `status`, `validFrom`/`validTo`, and `parent`/`child` is-a
  links derived positionally from the D-prefixed code structure. Customise with
  `--sks-diagnoses-canonical` / `--sks-diagnoses-version`.
- `CodeSystem-icd10-da.json` — a **FHIR CodeSystem supplement** that adds the
  **Danish display** to the international ICD-10 codes Denmark reuses unchanged.
  `content: supplement`, `supplements: http://hl7.org/fhir/sid/icd-10|<version>`.
  It contains **only** the SKS diagnosis codes classified `icd10_standard`
  (~10.5k codes that genuinely exist in ICD-10 — a supplement must not introduce
  codes absent from the base system); the Danish text is added as a `da`
  `designation` (not `concept.display` — a supplement must not override the base
  ICD-10 English display; clients get Danish via `displayLanguage=da`). Danish
  extensions and Danish-only
  blocks are *not* here (they are not ICD-10 codes — see
  `CodeSystem-icd10-danish-extensions.json`). Customise with `--icd10-da-canonical`
  and `--icd10-da-version`.

  The Danish designations are the official SKS short texts, limited to 60
  characters. The generator preserves SKS attribution, usage conditions and
  links to the full-text SKS tools in the resource metadata.

Both `.sks-cache/` and `sks-icd10-out/` are git-ignored. Copy the four generated
CodeSystems into [`fhir/`](../fhir/) for review and commit:

```bash
cp sks-icd10-out/CodeSystem-icd10-danish-extensions.json fhir/
cp sks-icd10-out/CodeSystem-sks.json                  fhir/
cp sks-icd10-out/CodeSystem-sks-diagnoses.json        fhir/
cp sks-icd10-out/CodeSystem-icd10-da.json             fhir/
```

After merge, publish the SKS CodeSystems and Danish ICD-10 extensions to the
Nordic terminology server. Contribute `CodeSystem-icd10-da.json` to
`packages/fhir.tx.support/package/` in `FHIR/packages`, alongside its
international base system. See the [publishing destinations](../README.md#generated-resources-fhir).

## `icpc2_icd10_conceptmap.py`

Builds the FHIR R4 **ICPC-2 → ICD-10 ConceptMap** from KiAP's published
ICPC-2-DK Access database. Both the mapping and the Danish labels come from
its **forward `ICPCKON2` table**. The separate `ICD10-ICPC` reverse register
has different coverage; it is neither inverted nor combined with this table.

Requires Python 3.10+ and `mdbtools` (installation below). Generate both ICPC
resources from one validated source snapshot:

```bash
python3 scripts/icpc2_icd10_conceptmap.py \
  --supplement-out fhir/CodeSystem-icpc2E-DK.json \
  --only-if-changed --report .icpc-kiap-cache/report.json

# Reproduce using local inputs, with no network requests.
python3 scripts/icpc2_icd10_conceptmap.py \
  --source-url https://web.kiap.dk/resources/files/icpc/systemhuse/ICPC_v4_4_20260629.accdb \
  --source-file .icpc-kiap-cache/ICPC_v4_4_20260629.accdb \
  --reference-file .icpc-kiap-cache/ICPC-2e-v7.0.zip \
  --icd10-file .icpc-kiap-cache/icd10.json \
  --supplement-out fhir/CodeSystem-icpc2E-DK.json
```

Without `--supplement-out`, only the ConceptMap is written. Online runs
discover and download the latest KiAP database and fetch the international
ICD-10 CodeSystem from `tx.fhir.org/r4`. The Danish extensions and SKS diagnoses
are read from the committed `fhir/` snapshots; `--extensions-file` and
`--sks-file` can override these. The source loader, ICPC code validation and
reviewed label resolutions are shared with the supplement generator.

The KiAP 4.4 snapshot has **2,476 mapping pairs covering all 686 ICPC codes**,
including W91. Process and grouping codes are outside the symptom/diagnosis
scope.

- Every target is resolved against an actual CodeSystem: international ICD-10
  first, Danish extensions through their exact `sksCode` property next, and
  the SKS diagnosis register as a fallback. Unresolved targets fail generation.
  Each system has its own ConceptMap group with a separate `targetVersion`.
  The current snapshot has 1,910 international and 566 Danish-extension pairs.
- Source and target displays use KiAP's Danish texts, normalizing whitespace.
- Relationships are conservatively `relatedto`. KiAP's `Udl=1` preferred match
  and `Udl=0` additional match are retained in target comments. Neither the
  number of targets nor the preferred flag establishes clinical equivalence.
- The forward table's `Gyldig fra dato` contains creation, change and closing
  dates. Include mappings active on the source release date (inclusive start,
  exclusive closing date), rather than changing output with today's date.
  KiAP 4.4 has one reviewed typo in the middle date for A01/DR521. Its intact
  start/end dates are used and the defect is recorded in the resource and
  report. Other malformed dates fail; no date is guessed.
- Require active mappings for all expected ICPC symptom/diagnosis codes.
  Unknown flags, conflicting duplicate pairs, schema changes and unknown
  codes fail before either resource is written.
- Version/date identify the KiAP source release. With `--only-if-changed`,
  date/filename-only re-exports preserve the existing snapshot. Mapping,
  label, flag and target-system/version changes cause an update.

The resource remains `draft` and `experimental`. WONCA/DSAM attribution and
usage conditions are preserved; automation does not grant additional rights.
After reviewing and merging an update, publish the ConceptMap to the Nordic
terminology server. The workflow creates a PR but does not deploy resources.

## `icpc2_da_supplement.py`

Builds `fhir/CodeSystem-icpc2E-DK.json` directly from KiAP's ICPC-2-DK Access
release. The source is the `ICPC-kode` and `ICPC-diagnose` columns of the
`ICPCKON2-...` mapping table. Repeated ICD-10 mapping rows are deduplicated by ICPC code. This standalone command writes
only the supplement; the ConceptMap command above and the scheduled workflow
generate both resources together.

### Run locally

Requires Python 3.10+ and [mdbtools](https://github.com/mdbtools/mdbtools):

```bash
# macOS
brew install mdbtools
# Debian / Ubuntu
sudo apt-get install mdbtools

# Discover the latest dated .accdb link on the KiAP page and regenerate.
python3 scripts/icpc2_da_supplement.py --only-if-changed

# Select a specific published release.
python3 scripts/icpc2_da_supplement.py \
  --source-url https://web.kiap.dk/resources/files/icpc/systemhuse/ICPC_v4_4_20260629.accdb

# Reproduce offline with previously downloaded files.
python3 scripts/icpc2_da_supplement.py \
  --source-url https://web.kiap.dk/resources/files/icpc/systemhuse/ICPC_v4_4_20260629.accdb \
  --source-file .icpc-kiap-cache/ICPC_v4_4_20260629.accdb \
  --reference-file .icpc-kiap-cache/ICPC-2e-v7.0.zip \
  --out /tmp/CodeSystem-icpc2E-DK.json

python3 -m unittest discover -s tests -v
```

There are no Python package dependencies. `mdb-tables` and `mdb-export` read the
Access database; Access macros and queries are not run. Source data is refreshed
on every online run (including updates under the same filename), and the
international reference ZIP is cached in `.icpc-kiap-cache/`. The latest KiAP
release is selected by numeric release version, then the date in its filename;
it is not selected by page order. Changed naming conventions require review.

### Content, provenance and versioning

- Emit a `da` designation for each international symptom/diagnosis code. The
  initial KiAP 4.4 snapshot contains 686 codes.
- Normalize whitespace, keeping KiAP's wording, abbreviations and punctuation.
- Validate codes against the pinned international ICPC-2e v7.0 ClaML archive
  published by Helsedirektoratet on behalf of WICC. A new KiAP code absent from
  that reference fails generation, so the reference must be reviewed before
  expanding the supplement. Missing expected codes and empty labels also fail.
- Exclude `*00` grouping/unknown entries and process codes. They are outside
  this supplement's symptom/diagnosis scope.
- KiAP 4.4 has conflicting labels across mapping rows. The reviewed selections
  are `P70: Demens`, `R83: Infektion i luftveje IKA`, and `S12: Insektstik`.
  These are not unconditional overrides: only the exact reviewed sets of
  conflicting labels are accepted. A new conflict or changed set fails with
  its code and labels. If KiAP resolves a conflict to a single label, use it.
- Set `version` and `date` to the source filename's release date, initially
  `2026-06-29`. The KiAP business version (`4.4`), exact URL, table, and applied
  conflict resolutions are recorded in the description. Identical inputs
  produce identical output.
- Preserve WONCA/DSAM attribution and licensing references. Automation does
  not grant additional usage or redistribution rights.

`--only-if-changed` compares the generated resource with the existing output,
ignoring `version`, `date`, `meta` and `description`. A date/filename-only
re-export therefore leaves the previous snapshot and provenance untouched;
label changes and other resource metadata changes are written atomically.
Without this flag, regenerate the full artifact including its current source
metadata. `--report PATH` writes source/validation details separately.

### Scheduled pull requests

[`.github/workflows/icpc-update.yml`](../.github/workflows/icpc-update.yml)
runs weekly and on manual dispatch, independently of the SKS workflow. It:

1. Runs the offline tests, discovers the latest KiAP release, and generates
   both the supplement and ConceptMap from the same database.
2. Opens or updates one PR on `chore/icpc-supplement-update` when substantive
   content differs from the default branch. It commits only the ICPC supplement
   and ConceptMap.
3. Leaves unchanged runs without a new PR, and reconciles an existing PR if
   the upstream change is reverted. It never merges or deploys the resources.

Enable **Settings → Actions → General → Workflow permissions → Allow GitHub
Actions to create and approve pull requests** in GitHub (or the corresponding
organization setting). The workflow uses `GITHUB_TOKEN` with `contents: write`
and `pull-requests: write`; no extra secret is needed. Tests on pull requests
have read-only permissions, and the update job runs only on schedule/dispatch.
The schedule becomes active when the workflow is on the default branch.

After reviewing and merging the generated PR, contribute the updated supplement
to `packages/fhir.tx.support/package/` in `FHIR/packages`, alongside the base
ICPC-2 system. Publish the updated ConceptMap to the Nordic terminology server.
This workflow creates PRs in this automation repository; it does not open
cross-repository PRs or upload resources to terminology servers.
