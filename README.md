# Verblijfkosten

*Verblijfkosten* is Dutch for the costs of staying somewhere during a work trip: meals, accommodation (*logies*) and other expenses.

When you declare these costs, you do not hand in receipts for every meal and other small expenses. 
Instead, you declare a number of meals and hours which are reimbursed at a fixed rate that depends on the country and region you visited. 
Accommodation is reimbursed on receipts, up to a maximum per night that also depends on the region.
Both amounts are set in [10.3 of the CAO Rijk](https://www.caorijk.nl/cao-rijk/1.-algemeen/hoofdstuk-10/dienstreizen-buitenland) in the *Tarieflijst verblijfkosten buitenlandse dienstreizen*.

If you simply declare everything you are entitled to, you can get back more than you actually spent.
Therefore, this tool calculates how many meals and nights to declare so that the reimbursement matches what you actually spent.

## Usage

Run it without installing using [uv](https://docs.astral.sh/uv/):

```shell
uvx verblijfkosten
```

Or with [pipx](https://pipx.pypa.io/):

```shell
pipx run verblijfkosten
```

Or from a clone of [the repository](https://git.liacs.nl/caboqmj1/verblijfkosten):

```shell
./verblijfkosten.py
```

The program asks for everything it needs. If you already know some of the answers, pass them as flags, and it will only ask for the rest:

```shell
uvx verblijfkosten --country Duitsland --nights 3 --logies-cost 420 --costs 137
```

## Flags

| Flag                           | Meaning                                                       |
|--------------------------------|---------------------------------------------------------------|
| `--country COUNTRY`            | The country you went to.                                      |
| `--region REGION`              | The region you went to (see `verblijf.toml`).                 |
| `--logies-cost LOGIES_COST`    | Your total accommodation costs.                               |
| `--costs COSTS`                | Your total other costs (meals and other expenses).            |
| `--nights NIGHTS`              | How many nights you were away.                                |
| `--force-download`             | Download the PDF with the rates again, even if a copy exists. |
| `-h`, `--help`                 | Show the help message and exit.                               |

## Freshness of reimbursement data

The rates per country and region are revised regularly (usually twice a year), but you do not need to update anything yourself.
The tool downloads the PDF with the rates from the Leiden University website when it is missing or has changed, so you always calculate with the current rates.
Use `--force-download` to download it again manually.