"""Project-wide paths and scope. Change scope here, not in individual modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
WAREHOUSE = DATA / "warehouse.duckdb"
ONTOLOGY_YAML = ROOT / "ontology" / "ontology.yaml"

# Scope: the four HS chapters our manufacturer cares about.
HS_CHAPTERS = ["29", "72", "84", "85"]  # organic chemicals, iron & steel, machinery, electrical

# Trailing 12 months of trade statistics (MonthId format YYYYMM). None = auto-detect latest.
OTS_END_MONTH: int | None = None

HMRC_API = "https://api.uktradeinfo.com"
GDACS_API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
SANCTIONS_CSV = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"
COMPANIES_HOUSE_INDEX = "https://download.companieshouse.gov.uk/en_output.html"
HMRC_BULK_INDEX = "https://www.uktradeinfo.com/trade-data/latest-bulk-datasets/"

RANDOM_SEED = 42
