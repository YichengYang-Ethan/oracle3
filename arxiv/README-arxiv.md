# arXiv Preprint Submission Guide

This folder contains everything needed to submit the Oracle3 preprint to
[arXiv](https://arxiv.org). arXiv requires `.edu` authentication, so the user
must perform the upload manually.

## Files in this folder

| File             | Purpose                                            |
|------------------|----------------------------------------------------|
| `paper.tex`      | Main LaTeX source (article class, ~4 pages)        |
| `paper.bib`      | BibTeX bibliography (11 entries, copied from root) |
| `README-arxiv.md`| This document                                      |

## Local compile test (sanity check)

```bash
cd arxiv/
pdflatex paper.tex
bibtex   paper
pdflatex paper.tex
pdflatex paper.tex
open paper.pdf      # macOS
```

Required LaTeX packages (all available in TeX Live / MacTeX / TinyTeX):
`article`, `inputenc`, `fontenc`, `lmodern`, `geometry`, `amsmath`, `amssymb`,
`amsthm`, `graphicx`, `xcolor`, `natbib`, `booktabs`, `hyperref`.

If a package is missing in TinyTeX, install with
`tlmgr install <package>` (one-time).

This source compiled cleanly to a 4-page PDF on the author's machine on
2026-05-06 (TeX Live 2025, TinyTeX). One cosmetic `Overfull \hbox` warning is
present and is non-blocking for arXiv.

## Submission steps

### 1. Login

- Go to <https://arxiv.org/submit>
- Use your **illinois.edu** account (UIUC is auto-endorsed for q-fin and
  econ; no separate endorser required for first-time q-fin submissions from
  `illinois.edu`).

### 2. Start a new submission

Click **Start new submission**. Choose:

- **License**: `CC BY 4.0`
  (`Creative Commons Attribution 4.0 International`)
- **Submission type**: `Article`

### 3. Categories

| Field          | Value                                |
|----------------|--------------------------------------|
| Primary        | `q-fin.PR` (Pricing of Securities)   |
| Cross-list 1   | `q-fin.TR` (Trading and Microstructure) |
| Cross-list 2   | `econ.GN` (General Economics)        |

### 4. Metadata

- **Title** (copy verbatim):

  > Oracle3: An open-source autonomous trading agent for prediction markets
  > with a Wang Transform pricing engine

- **Authors**:

  ```
  Yicheng Yang
  ```

  Affiliation: `University of Illinois Urbana-Champaign`
  ORCID: `0009-0000-7973-6931` (link in author profile)

- **Abstract** (paste in the abstract box, ~1640 chars, well under arXiv's
  1920-char limit):

  > Prediction markets price binary contracts at systematically biased
  > levels: a contract whose objective probability is 50% trades on average
  > around 57 cents, an empirical regularity known as the favorite-longshot
  > bias. Despite a long literature documenting this distortion, most
  > open-source trading bots ignore it, treating market prices as unbiased
  > estimates of probability. Oracle3 is a Python framework that
  > operationalizes a peer-reviewed risk-neutral pricing model for binary
  > outcome contracts and uses it to drive automated trading across multiple
  > venues (Kalshi, Polymarket, and Solana-based DFlow). At its core is a
  > Wang Transform calibrated by maximum-likelihood estimation on 291,309
  > resolved contracts spanning six platforms, with hierarchical covariates
  > for volume, days-to-expiry, and contract moneyness. The library exposes
  > the model as a fair-value engine and pairs it with eight constraint-based
  > arbitrage strategies, statistical-arbitrage strategies, model-Greek-
  > driven sizing, and a risk manager - all wired into an event-driven async
  > trading core with snapshot persistence, killswitch support, and on-chain
  > audit trails. Source code is available at
  > https://github.com/YichengYang-Ethan/oracle3 under the Apache 2.0
  > license, with archived release at Zenodo
  > (DOI 10.5281/zenodo.20062549).

- **Comments** (paste in the comments field exactly):

  ```
  Code: https://github.com/YichengYang-Ethan/oracle3 (Apache 2.0); Zenodo DOI: 10.5281/zenodo.20062549
  ```

- **Report-no / DOI / Journal-ref**: leave blank.
- **MSC class / ACM class**: leave blank.

### 5. Upload files

In the **Files** step, upload:

1. `paper.tex`
2. `paper.bib`

Optionally include any figures (none are required for this preprint).

arXiv's AutoTeX compiler will automatically run `pdflatex` -> `bibtex` ->
`pdflatex` x2 and produce the PDF. Verify the resulting PDF preview before
submitting.

### 6. Submit

Click **Submit**. arXiv moderation typically takes 12-24 hours. Once
accepted, the preprint receives an `arXiv:YYMM.NNNNN` identifier and
becomes publicly searchable.

### 7. Post-acceptance

- Add the arXiv ID and DOI to:
  - `README.md` (top-level)
  - `CITATION.cff`
  - `paper.bib` (update the `@article{yang:2026, ...}` entry to add an
    `eprint`/`archiveprefix` field)
  - SSRN abstract page (optional)
- Tweet/announce as desired.

## Notes

- arXiv does not accept the JOSS-flavored Markdown directly; this `paper.tex`
  reformats the same content as a standalone preprint with explicit numbered
  sections (Introduction, Methodology, Implementation, Conclusion).
- The `paper.bib` here is identical to the repository root's `paper.bib`. If
  the root bibliography is updated, copy the new file over.
- License compatibility: arXiv `CC BY 4.0` is compatible with the Apache 2.0
  code license. The companion code stays Apache 2.0; only the preprint
  manuscript is CC BY 4.0.
