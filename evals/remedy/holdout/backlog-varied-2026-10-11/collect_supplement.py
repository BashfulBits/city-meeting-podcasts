import concurrent.futures
import importlib.util
import json
from pathlib import Path

path = Path(__file__).resolve().parent / "collect_packet_v2.py"
spec = importlib.util.spec_from_file_location("collector", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
if (m.OUT / "supplement-retrieval.json").exists():
    raise SystemExit("Refusing existing supplementary evidence")
rows = json.load(open(m.OUT / "source-records.json"))["records"]
requests = {}
for r in rows:
    link = (r.get("links") or {}).get("canonical_video") or next(
        (s.get("watch_url") for s in r.get("sources") or [] if s.get("watch_url")), None
    )
    if link:
        requests[r["uid"] + "-provider-player"] = link
requests.update(
    {
        "citizen-budget-2007": (
            "https://www.addisontx.gov/files/sharedassets/main/v/1/finance/do"
            "cuments/fy-2007-2008-addison-annual-budget.pdf"
        ),
        "citizen-council-june2007": (
            "https://agendas.addisontx.gov/docs/2007/CM/20070612_376/373_2007"
            "-06-12%20Agenda_Meeting%20Combined%20Meeting.pdf"
        ),
        "dallas-streets-may25": (
            "https://dallascityhall.com/departments/bond-construction-managem"
            "ent/Lists/DepartmentNavigation/Attachments/99/Streets%20and%20Tr"
            "ansportation%20Meeting_052523_Meeting%20Minutes.pdf"
        ),
        "dallas-streets-june13": (
            "https://dallascityhall.com/departments/bond-construction-managem"
            "ent/Lists/DepartmentNavigation/Attachments/119/Streets%20and%20T"
            "ransportation%20Meeting%20061323_Meeting%20Minutes.pdf"
        ),
        "dallas-cbtf-aug15": (
            "https://dallascityhall.com/departments/bond-construction-managem"
            "ent/Lists/DepartmentNavigation/Attachments/163/CBTF%20Meeting_08"
            "1523_Meeting%20Minutes.pdf"
        ),
        "dallas-streets-archive": (
            "https://dallascityhall.com/departments/bond-construction-managem"
            "ent/Pages/Streets-and-Transportation-Subcommittee.aspx"
        ),
        "addison-bond-program": (
            "https://www.addisontx.gov/Government/Departments/Finance/Bond-El"
            "ection-History/2026-Bond-Election"
        ),
    }
)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    docs = list(pool.map(m.retrieve, requests.items()))
with (m.OUT / "supplement-retrieval.json").open("x") as h:
    json.dump({"documents": docs}, h, indent=2)
print("Supplement", len(docs), "retrieved", sum(d["status"] != "unavailable" for d in docs))
