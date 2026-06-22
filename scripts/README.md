# scripts

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

Every SKS diagnosis code is the ICD-10 code prefixed with a literal `D`
(*Diagnose*), e.g. SKS `DA022` == ICD-10 `A02.2`. After stripping the `D`
each code falls into one of three buckets:

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
  `.lastmod` sidecar. SKScomplete has no internal version, and this date only
  changes when SKS actually releases, so re-runs are byte-identical (no spurious
  diffs) and `version` tells consumers exactly which SKS edition a snapshot is.
  `--sks-version` / `--supplement-version` override it with a custom label.
- `CodeSystem-sks-diagnoses.json` — a **FHIR CodeSystem** for the legacy SKS
  diagnosis register (the "D-hierarchy"): the Danish edition of ICD-10 where
  each code is the ICD-10 code prefixed with a literal `D` (e.g. `DA022` ==
  ICD-10 `A02.2`), with the Danish display. `content: complete` under the SKS
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

Both `.sks-cache/` and `sks-icd10-out/` are git-ignored. The four generated
CodeSystems are committed under [`../fhir/`](../fhir/) and **uploaded to the
Nordic terminology server** (manual step) so the DK Core IG resolves them by
canonical URL — they are not committed into DK Core:

```bash
cp sks-icd10-out/CodeSystem-icd10-danish-extensions.json ../fhir/
cp sks-icd10-out/CodeSystem-sks.json                  ../fhir/
cp sks-icd10-out/CodeSystem-sks-diagnoses.json        ../fhir/
cp sks-icd10-out/CodeSystem-icd10-da.json             ../fhir/
```

## `icpc2_icd10_conceptmap.py`

Builds a FHIR R4 **ConceptMap** mapping Danish **ICPC-2 → ICD-10**, harvested
from the public sundhed.dk / dudal ICPC tool
(<https://dake2.dudal.com/icpc/>). It enumerates the 17 ICPC-2 chapters via the
tool's `icpcserver.php` backend, converts every rubric to its SKS `D`-prefixed
ICD-10 codes, turns those back into ICD-10 form (displays resolved against the
ICD-10 CodeSystem cached by `sks_icd10_diff.py`, if present), and emits one
ConceptMap (`source` `http://hl7.org/fhir/sid/icpc-2`, `target`
`http://hl7.org/fhir/sid/icd-10`).

```bash
python3 scripts/icpc2_icd10_conceptmap.py \
  --codes-file scripts/icpc2-extra-codes.tsv \
  --out fhir/ConceptMap-icpc2-icd10.json
python3 scripts/icpc2_icd10_conceptmap.py --force   # re-harvest from the source
```

Equivalence: a rubric mapping to exactly one ICD-10 code is `equivalent`; one
mapping to several is `narrower` (the ICPC-2 rubric is broader than each
ICD-10 code). The chapter search misses a few codes, so `--codes-file`
supplements it from the official ICPC-2 code card (`icpc2-extra-codes.tsv`):
this adds the real diagnosis `D86` plus the ICPC-1 *process* codes (emitted
`unmatched`). Result: 695 ICPC-2 codes, 686 mapped, ~9.3k ICD-10 targets. HTTP
responses are cached under `.icpc-cache/` (git-ignored) so re-runs are offline
and the source is hit only once; the run is polite (`--sleep`, identifying
User-Agent).

> ⚠️ **Licensing.** ICPC-2 is copyright **WONCA**; the Danish **ICPC-2-DK**
> rights are held by **DSAM**, and the mapping data originates from sundhed.dk.
> The generated ConceptMap is a *convenience* mapping, **not** an authoritative
> or openly-licensed artifact — only use/redistribute it within the terms under
> which you hold ICPC-2-DK rights. For that reason it is **not** part of the
> automated quarterly workflow; generate and upload it deliberately.
